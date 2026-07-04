"""Tests for V48 local relevance review packet builder (offline only)."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List, Mapping, Optional, Tuple
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.v48_build_relevance_review_packet import (
    ALLOWED_RELEVANCE_LABELS,
    DEFAULT_CANDIDATE_LIMIT,
    ENV_SUPABASE_SERVICE_ROLE_KEY,
    ENV_SUPABASE_URL,
    FROZEN_REPLAY_FIXTURE_PATH,
    LOCAL_REVIEW_ROOT,
    REFERENCE_REPLAY_CONTENT_SET_HASH,
    RelevanceReviewPacketCacheError,
    RelevanceReviewPacketConfigError,
    RelevanceReviewPacketContentSetError,
    RelevanceReviewPacketEnvironmentError,
    RelevanceReviewPacketOutputError,
    CacheOnlyForbiddenProvider,
    compute_packet_hash,
    compute_pair_id,
    compute_review_packet_plan,
    format_dry_run_plan,
    format_redacted_console_summary,
    main,
    parse_args,
    run_build_relevance_review_packet,
    validate_execute_configuration,
    validate_output_path,
)
from scripts.v48_real_hybrid_replay import FROZEN_QUESTION_COUNT, load_frozen_replay_fixture
from workers.v48_hybrid_replay_authoritative_text import (
    DEFAULT_VALIDATED_MODEL_VERSION,
    AuthoritativeCandidateMatch,
    AuthoritativeEmbeddingTextResolver,
    compute_authoritative_content_hash,
    compute_replay_content_set_hash,
    _all_semantic_review_bindings,
)
from workers.ai_quality_audit_shadow import (
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    classify_question_shadow_from_replay_record,
)
from workers.embedding_cache import (
    CONTENT_SCOPE_CHUNK,
    CONTENT_SCOPE_QUERY,
    EmbeddingCacheIdentity,
    EmbeddingCacheRecord,
    build_cache_identity,
)
from workers.embedding_providers import OPENAI_PROVIDER_NAME

_MODEL = "text-embedding-3-small"
_VERSION = DEFAULT_VALIDATED_MODEL_VERSION
_DIMENSIONS = 3
_VECTOR = (0.11, 0.22, 0.33)
_RESPONSE_HASH = "c" * 64
_SERVICE_ROLE = "service-role-key-not-real"
_SUPABASE_URL = "https://example.supabase.co"
_SENSITIVE_QUERY = "SECRET QUERY TEXT MUST NOT LEAK"
_SENSITIVE_CHUNK = "SECRET CHUNK TEXT MUST NOT LEAK"

_REQUIRED_ENV = {
    ENV_SUPABASE_URL: _SUPABASE_URL,
    ENV_SUPABASE_SERVICE_ROLE_KEY: _SERVICE_ROLE,
}


def _load_fixture() -> dict[str, Any]:
    return load_frozen_replay_fixture(fixture_path=FROZEN_REPLAY_FIXTURE_PATH)


def _execute_config(**overrides: Any) -> Any:
    from scripts.v48_build_relevance_review_packet import RelevanceReviewPacketConfig

    output_dir = tempfile.mkdtemp(dir=_ensure_local_review_root())
    base = {
        "execute": True,
        "expected_content_set_hash": REFERENCE_REPLAY_CONTENT_SET_HASH,
        "model_name": _MODEL,
        "model_version": _VERSION,
        "dimensions": _DIMENSIONS,
        "candidate_limit": DEFAULT_CANDIDATE_LIMIT,
        "fixture_path": FROZEN_REPLAY_FIXTURE_PATH,
        "output_path": os.path.join(output_dir, "packet.json"),
        "overwrite": False,
    }
    base.update(overrides)
    return RelevanceReviewPacketConfig(**base)


def _ensure_local_review_root() -> str:
    os.makedirs(LOCAL_REVIEW_ROOT, exist_ok=True)
    return LOCAL_REVIEW_ROOT


def _authoritative_resolver_with_metadata(
    _client: Any,
    fixture: Mapping[str, Any],
    candidate_limit: int,
) -> AuthoritativeEmbeddingTextResolver:
    question_text_by_id: dict[str, str] = {}
    candidate_text_by_identity: dict[str, str] = {}
    for record in fixture["questions"]:
        question_version_id = str(record["question_version_id"])
        shadow = classify_question_shadow_from_replay_record(record)
        if shadow["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
            continue
        question_text_by_id[question_version_id] = (
            f"Authority query payload qvid={question_version_id}"
        )
        for binding in _all_semantic_review_bindings(record):
            candidate_text_by_identity[binding.candidate_identity] = (
                f"Authority chunk payload identity={binding.candidate_identity}"
            )
    resolver = AuthoritativeEmbeddingTextResolver.from_resolved_texts(
        fixture,
        candidate_limit=candidate_limit,
        question_text_by_id=question_text_by_id,
        candidate_text_by_identity=candidate_text_by_identity,
    )
    resolver._candidate_match_by_identity = {
        binding.candidate_identity: AuthoritativeCandidateMatch(
            resource_id="22222222-2222-2222-2222-222222222222",
            resource_chunk_id="33333333-3333-3333-3333-333333333333",
            title=binding.title,
            resource_type=binding.resource_type,
        )
        for record in fixture["questions"]
        for binding in _all_semantic_review_bindings(record)
    }
    return resolver


class _MockSupabaseClient:
    def table(self, _name: str) -> Any:
        raise AssertionError("review packet builder must not write cache rows")


class _PreloadedCacheRepository:
    def __init__(self, rows: List[EmbeddingCacheRecord]) -> None:
        self._rows = {
            (
                row.content_scope,
                row.content_hash,
                row.embedding_provider_name,
                row.embedding_model_name,
                row.embedding_model_version,
                row.embedding_dimensions,
            ): row
            for row in rows
        }
        self.insert_calls = 0

    def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
        return self._rows.get(identity.as_tuple())

    def insert(self, _record: EmbeddingCacheRecord) -> None:
        self.insert_calls += 1
        raise AssertionError("cache insert is not allowed")


def _preload_cache_rows(
    fixture: Mapping[str, Any],
    resolver: AuthoritativeEmbeddingTextResolver,
) -> _PreloadedCacheRepository:
    shared = {
        "embedding_provider_name": OPENAI_PROVIDER_NAME,
        "embedding_model_name": _MODEL,
        "embedding_model_version": _VERSION,
        "embedding_dimensions": _DIMENSIONS,
    }
    rows: List[EmbeddingCacheRecord] = []
    for record in fixture["questions"]:
        question_version_id = str(record["question_version_id"])
        shadow = classify_question_shadow_from_replay_record(record)
        if shadow["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
            continue
        query_text = resolver.resolve_question_embedding_text(question_version_id)
        assert query_text is not None
        query_identity = build_cache_identity(
            text=query_text,
            content_scope=CONTENT_SCOPE_QUERY,
            **shared,
        )
        rows.append(
            EmbeddingCacheRecord(
                content_scope=query_identity.content_scope,
                content_hash=query_identity.content_hash,
                embedding_provider_name=query_identity.embedding_provider_name,
                embedding_model_name=query_identity.embedding_model_name,
                embedding_model_version=query_identity.embedding_model_version,
                embedding_dimensions=query_identity.embedding_dimensions,
                embedding_vector=_VECTOR,
                provider_response_hash=_RESPONSE_HASH,
            )
        )
        for binding in _all_semantic_review_bindings(record):
            chunk_text = resolver.resolve_candidate_embedding_text(binding.candidate_identity)
            assert chunk_text is not None
            chunk_identity = build_cache_identity(
                text=chunk_text,
                content_scope=CONTENT_SCOPE_CHUNK,
                **shared,
            )
            rows.append(
                EmbeddingCacheRecord(
                    content_scope=chunk_identity.content_scope,
                    content_hash=chunk_identity.content_hash,
                    embedding_provider_name=chunk_identity.embedding_provider_name,
                    embedding_model_name=chunk_identity.embedding_model_name,
                    embedding_model_version=chunk_identity.embedding_model_version,
                    embedding_dimensions=chunk_identity.embedding_dimensions,
                    embedding_vector=_VECTOR,
                    provider_response_hash=_RESPONSE_HASH,
                )
            )
    return _PreloadedCacheRepository(rows)


class TestRelevanceReviewPacketDryRun(unittest.TestCase):
    def test_dry_run_makes_zero_external_calls(self):
        config = _execute_config(execute=False)
        summary = run_build_relevance_review_packet(
            config,
            env=_REQUIRED_ENV,
            client_factory=lambda: (_ for _ in ()).throw(AssertionError("no client")),
        )
        self.assertEqual(summary["final_status"], "planned")
        self.assertEqual(summary["provider_request_count"], 0)

    def test_dry_run_reports_expected_counts(self):
        fixture = _load_fixture()
        plan = compute_review_packet_plan(fixture, candidate_limit=DEFAULT_CANDIDATE_LIMIT)
        config = _execute_config(execute=False)
        output = format_dry_run_plan(config, plan=plan)

        self.assertEqual(plan.question_count, FROZEN_QUESTION_COUNT)
        self.assertEqual(plan.question_count, 10)
        self.assertEqual(plan.semantic_review_question_count, 7)
        self.assertEqual(plan.pair_count, 14)
        self.assertIn("cache_only: true", output)
        self.assertIn("provider_requests_allowed: 0", output)
        self.assertIn(REFERENCE_REPLAY_CONTENT_SET_HASH, output)
        self.assertNotIn(_SENSITIVE_QUERY, output)

    def test_openai_api_key_is_not_required(self):
        config = _execute_config(execute=True)
        env = dict(_REQUIRED_ENV)
        validate_execute_configuration(config, env=env)


class TestRelevanceReviewPacketExecuteGates(unittest.TestCase):
    def test_execute_requires_expected_content_set_hash(self):
        config = _execute_config(expected_content_set_hash=None)
        with self.assertRaises(RelevanceReviewPacketConfigError):
            validate_execute_configuration(config, env=_REQUIRED_ENV)

    def test_missing_supabase_env_fails_before_external_calls(self):
        env = dict(_REQUIRED_ENV)
        env.pop(ENV_SUPABASE_URL)
        with self.assertRaises(RelevanceReviewPacketEnvironmentError):
            validate_execute_configuration(_execute_config(), env=env)

    def test_output_path_outside_local_directory_is_rejected(self):
        with self.assertRaises(RelevanceReviewPacketOutputError):
            validate_output_path("/tmp/not_allowed.json", overwrite=False)


class TestRelevanceReviewPacketCacheOnlyExecution(unittest.TestCase):
    def _run_successful_build(self, **overrides: Any) -> tuple[dict[str, Any], _PreloadedCacheRepository]:
        fixture = _load_fixture()
        resolver = _authoritative_resolver_with_metadata(None, fixture, DEFAULT_CANDIDATE_LIMIT)
        content_set_hash = resolver.replay_content_set_hash
        repository = _preload_cache_rows(fixture, resolver)

        class _RepositoryFactory:
            def __init__(self) -> None:
                self.repo = repository

        factory = _RepositoryFactory()

        def _run_with_repo(*args: Any, **kwargs: Any) -> dict[str, Any]:
            kwargs["client_factory"] = _MockSupabaseClient
            kwargs["resolver_factory"] = lambda *_a, **_k: resolver
            with mock.patch(
                "scripts.v48_build_relevance_review_packet.SupabaseEmbeddingCacheRepository",
                lambda _client: factory.repo,
            ):
                return run_build_relevance_review_packet(*args, **kwargs)

        config = _execute_config(
            expected_content_set_hash=content_set_hash,
            **overrides,
        )
        summary = _run_with_repo(config, env=_REQUIRED_ENV, fixture=fixture)
        return summary, repository

    def test_cache_only_build_succeeds_with_preloaded_rows(self):
        summary, repository = self._run_successful_build()
        self.assertEqual(summary["final_status"], "success")
        self.assertEqual(summary["pair_count"], 14)
        self.assertEqual(summary["provider_request_count"], 0)
        self.assertEqual(summary["cache_miss_count"], 0)
        self.assertEqual(repository.insert_calls, 0)
        self.assertTrue(os.path.isfile(summary["output_path"]))

        with open(summary["output_path"], encoding="utf-8") as handle:
            packet = json.load(handle)
        self.assertEqual(packet["pair_count"], 14)
        self.assertTrue(packet["cache_only"])
        self.assertEqual(packet["allowed_relevance_labels"], list(ALLOWED_RELEVANCE_LABELS))
        for pair in packet["pairs"]:
            self.assertIsNone(pair["relevance_label"])
            self.assertEqual(pair["reviewer_notes"], "")
            self.assertIn("authoritative_query_text", pair)
            self.assertIn("authoritative_candidate_chunk_text", pair)

    def test_cache_miss_fails_closed_without_provider(self):
        fixture = _load_fixture()
        resolver = _authoritative_resolver_with_metadata(None, fixture, DEFAULT_CANDIDATE_LIMIT)
        empty_repo = _PreloadedCacheRepository([])

        with mock.patch(
            "scripts.v48_build_relevance_review_packet.SupabaseEmbeddingCacheRepository",
            lambda _client: empty_repo,
        ):
            with self.assertRaises(RelevanceReviewPacketCacheError):
                run_build_relevance_review_packet(
                    _execute_config(expected_content_set_hash=resolver.replay_content_set_hash),
                    env=_REQUIRED_ENV,
                    fixture=fixture,
                    client_factory=_MockSupabaseClient,
                    resolver_factory=lambda *_a, **_k: resolver,
                )
        self.assertEqual(empty_repo.insert_calls, 0)

    def test_forbidden_provider_raises_on_embed(self):
        provider = CacheOnlyForbiddenProvider()
        with self.assertRaises(Exception):
            provider.embed(text="x", embedding_provider_name=OPENAI_PROVIDER_NAME, embedding_model_name=_MODEL, embedding_model_version=_VERSION, embedding_dimensions=_DIMENSIONS)
        self.assertEqual(provider.provider_request_count, 1)

    def test_content_set_hash_mismatch_fails_before_packet_write(self):
        fixture = _load_fixture()
        resolver = _authoritative_resolver_with_metadata(None, fixture, DEFAULT_CANDIDATE_LIMIT)
        output_dir = tempfile.mkdtemp(dir=_ensure_local_review_root())
        output_path = os.path.join(output_dir, "packet.json")
        repository = _preload_cache_rows(fixture, resolver)

        with mock.patch(
            "scripts.v48_build_relevance_review_packet.SupabaseEmbeddingCacheRepository",
            lambda _client: repository,
        ):
            with self.assertRaises(RelevanceReviewPacketContentSetError):
                run_build_relevance_review_packet(
                    _execute_config(
                        expected_content_set_hash="0" * 64,
                        output_path=output_path,
                    ),
                    env=_REQUIRED_ENV,
                    fixture=fixture,
                    client_factory=_MockSupabaseClient,
                    resolver_factory=lambda *_a, **_k: resolver,
                )
        self.assertFalse(os.path.exists(output_path))

    def test_overwrite_is_required_to_replace_existing_packet(self):
        summary, _repository = self._run_successful_build()
        with self.assertRaises(RelevanceReviewPacketOutputError):
            self._run_successful_build(output_path=summary["output_path"], overwrite=False)
        replaced = self._run_successful_build(
            output_path=summary["output_path"],
            overwrite=True,
        )
        self.assertEqual(replaced[0]["output_path"], summary["output_path"])


class TestRelevanceReviewPacketDeterminismAndPrivacy(unittest.TestCase):
    def test_pair_id_and_packet_hash_are_deterministic(self):
        pair_id = compute_pair_id(
            question_version_id="11111111-1111-1111-1111-111111111111",
            candidate_identity="abc",
        )
        self.assertEqual(
            pair_id,
            compute_pair_id(
                question_version_id="11111111-1111-1111-1111-111111111111",
                candidate_identity="abc",
            ),
        )
        body = {
            "schema_version": "v48_relevance_review_packet_v1",
            "pair_count": 1,
            "pairs": [{"pair_id": pair_id, "relevance_label": None}],
        }
        self.assertEqual(compute_packet_hash(body), compute_packet_hash(body))

    def test_changed_authoritative_content_changes_content_set_hash(self):
        first = compute_replay_content_set_hash(
            compute_authoritative_content_hash("query A"),
            compute_authoritative_content_hash("chunk A"),
        )
        second = compute_replay_content_set_hash(
            compute_authoritative_content_hash("query B"),
            compute_authoritative_content_hash("chunk A"),
        )
        self.assertNotEqual(first, second)

    def test_console_output_is_redacted(self):
        summary, _repository = TestRelevanceReviewPacketCacheOnlyExecution()._run_successful_build()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            print(format_redacted_console_summary(summary))
        output = stdout.getvalue()
        self.assertNotIn(_SENSITIVE_QUERY, output)
        self.assertNotIn(_SENSITIVE_CHUNK, output)
        self.assertNotIn("Authority query payload", output)
        self.assertNotIn("Authority chunk payload", output)
        self.assertIn("provider_request_count", output)

    def test_main_dry_run_exits_successfully(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("dry-run", stdout.getvalue())


class TestRelevanceReviewPacketIsolation(unittest.TestCase):
    def test_no_live_worker_imports_review_packet_builder(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        workers_dir = os.path.join(repo_root, "workers")
        offenders = []
        for name in os.listdir(workers_dir):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            path = os.path.join(workers_dir, name)
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
            if "v48_build_relevance_review_packet" in contents:
                offenders.append(name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
