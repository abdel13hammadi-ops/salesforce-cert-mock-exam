"""Tests for V48 real hybrid replay runner (mocked HTTP and Supabase only)."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Mapping, Optional, Tuple
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.v48_real_hybrid_replay import (
    DEFAULT_CANDIDATE_LIMIT,
    ENV_OPENAI_API_KEY,
    ENV_SUPABASE_SERVICE_ROLE_KEY,
    ENV_SUPABASE_URL,
    FROZEN_QUESTION_COUNT,
    FROZEN_REPLAY_FIXTURE_PATH,
    RealHybridReplayBudgetError,
    RealHybridReplayConfig,
    RealHybridReplayEnvironmentError,
    RealHybridReplayProviderError,
    RealHybridReplayTextResolutionError,
    BudgetEnforcingEmbeddingProvider,
    ProviderRequestBudget,
    compute_replay_execution_plan,
    format_dry_run_plan,
    format_redacted_summary,
    load_frozen_replay_fixture,
    main,
    parse_args,
    run_real_hybrid_replay,
    validate_execute_configuration,
)
from workers.v48_hybrid_replay_authoritative_text import (
    DEFAULT_VALIDATED_MODEL_VERSION,
    FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
    FAILURE_STAGE_AUTHORITATIVE_MATCHING,
    FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION,
    FAILURE_STAGE_EMBEDDING_EXECUTION,
    FAILURE_STAGE_EXECUTE_PREFLIGHT,
    AuthoritativeEmbeddingTextError,
    AuthoritativeEmbeddingTextResolver,
    build_fixture_embedding_text_resolver,
    build_supabase_authoritative_embedding_text_resolver,
    compute_authoritative_content_hash,
    _selected_semantic_review_bindings,
    _all_semantic_review_bindings,
)
from workers.ai_quality_audit_hybrid_replay import run_hybrid_replay_from_records
from workers.ai_quality_audit_shadow import (
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    classify_question_shadow_from_replay_record,
)
from workers.embedding_cache import (
    CONTENT_SCOPE_CHUNK,
    CONTENT_SCOPE_QUERY,
    TABLE_NAME,
    EmbeddingCacheIdentity,
    EmbeddingCacheRecord,
    EmbeddingProviderResponse,
    build_cache_identity,
)
from workers.embedding_providers import OPENAI_PROVIDER_NAME, HttpResponse

_MODEL = "text-embedding-3-small"
_VERSION = DEFAULT_VALIDATED_MODEL_VERSION
_DIMENSIONS = 3
_VECTOR = (0.11, 0.22, 0.33)
_RESPONSE_HASH = "c" * 64
_API_KEY = "sk-test-key-not-real"
_SERVICE_ROLE = "service-role-key-not-real"
_SUPABASE_URL = "https://example.supabase.co"
_RUN_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_SENSITIVE_TEXT = "SECRET REPLAY TEXT MUST NOT LEAK"

_REQUIRED_ENV = {
    ENV_OPENAI_API_KEY: _API_KEY,
    ENV_SUPABASE_URL: _SUPABASE_URL,
    ENV_SUPABASE_SERVICE_ROLE_KEY: _SERVICE_ROLE,
}


def _load_fixture() -> dict[str, Any]:
    return load_frozen_replay_fixture(fixture_path=FROZEN_REPLAY_FIXTURE_PATH)


def _execute_config(**overrides: Any) -> RealHybridReplayConfig:
    base = {
        "execute": True,
        "model_name": _MODEL,
        "model_version": _VERSION,
        "dimensions": _DIMENSIONS,
        "max_provider_requests": 21,
        "candidate_limit": DEFAULT_CANDIDATE_LIMIT,
        "timeout_seconds": 12.0,
        "run_id": _RUN_ID,
        "fixture_path": FROZEN_REPLAY_FIXTURE_PATH,
    }
    base.update(overrides)
    return RealHybridReplayConfig(**base)


def _authoritative_resolver_factory(
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
                "Authority chunk payload "
                f"identity={binding.candidate_identity}"
            )
    return AuthoritativeEmbeddingTextResolver.from_resolved_texts(
        fixture,
        candidate_limit=candidate_limit,
        question_text_by_id=question_text_by_id,
        candidate_text_by_identity=candidate_text_by_identity,
    )


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def post_json(self, **kwargs: Any) -> HttpResponse:
        self.calls.append(kwargs)
        return HttpResponse(
            status_code=200,
            body=json.dumps(
                {
                    "object": "list",
                    "data": [{"embedding": list(_VECTOR)}],
                    "model": _MODEL,
                }
            ),
        )


class _MockSupabaseResult:
    def __init__(self, data: List[dict[str, Any]]) -> None:
        self.data = data


class _MockSupabaseQuery:
    def __init__(self, client: "_MockSupabaseClient", table_name: str) -> None:
        self._client = client
        self._table_name = table_name
        self._operation: Optional[str] = None
        self._filters: List[Tuple[str, str, Any]] = []
        self._limit: Optional[int] = None
        self._insert_payload: Optional[dict[str, Any]] = None

    def select(self, fields: str) -> "_MockSupabaseQuery":
        self._operation = "select"
        return self

    def delete(self) -> "_MockSupabaseQuery":
        self._operation = "delete"
        return self

    def eq(self, field: str, value: Any) -> "_MockSupabaseQuery":
        self._filters.append(("eq", field, value))
        return self

    def limit(self, count: int) -> "_MockSupabaseQuery":
        self._limit = count
        return self

    def insert(self, payload: dict[str, Any]) -> "_MockSupabaseQuery":
        self._operation = "insert"
        self._insert_payload = payload
        return self

    def update(self, _payload: dict[str, Any]) -> "_MockSupabaseQuery":
        raise AssertionError("cache update is not allowed")

    def upsert(self, _payload: dict[str, Any]) -> "_MockSupabaseQuery":
        raise AssertionError("cache upsert is not allowed")

    def execute(self) -> _MockSupabaseResult:
        self._client.queries.append(self)
        return self._client._execute_query(self)


class _MockSupabaseClient:
    def __init__(self) -> None:
        self.rows: Dict[Tuple[Any, ...], dict[str, Any]] = {}
        self.queries: List[_MockSupabaseQuery] = []
        self.tables_touched: List[str] = []

    def table(self, name: str) -> _MockSupabaseQuery:
        self.tables_touched.append(name)
        return _MockSupabaseQuery(self, name)

    def _row_key(self, row: dict[str, Any]) -> Tuple[Any, ...]:
        return (
            row["content_scope"],
            row["content_hash"],
            row["embedding_provider_name"],
            row["embedding_model_name"],
            row["embedding_model_version"],
            row["embedding_dimensions"],
        )

    def _execute_query(self, query: _MockSupabaseQuery) -> _MockSupabaseResult:
        if query._operation == "select":
            matches = [
                row
                for row in self.rows.values()
                if all(row.get(field) == value for _, field, value in query._filters)
            ]
            if query._limit is not None:
                matches = matches[: query._limit]
            return _MockSupabaseResult(matches)
        if query._operation == "insert":
            assert query._insert_payload is not None
            key = self._row_key(query._insert_payload)
            self.rows[key] = dict(query._insert_payload)
            return _MockSupabaseResult([])
        raise AssertionError(f"unexpected operation {query._operation!r}")


class SimpleCountingProvider:
    def __init__(self) -> None:
        self.calls: List[dict[str, Any]] = []

    def embed(self, **kwargs: Any) -> EmbeddingProviderResponse:
        self.calls.append(kwargs)
        return EmbeddingProviderResponse(
            embedding_vector=_VECTOR,
            provider_response_hash=_RESPONSE_HASH,
        )


class TestRealHybridReplayDryRun(unittest.TestCase):
    def test_dry_run_performs_zero_external_calls(self):
        config = _execute_config(execute=False)
        transport = RecordingTransport()
        client = _MockSupabaseClient()

        summary = run_real_hybrid_replay(
            config,
            env=_REQUIRED_ENV,
            client_factory=lambda: client,
            transport_factory=lambda: transport,
            resolver_factory=_authoritative_resolver_factory,
        )

        self.assertEqual(summary["final_status"], "planned")
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.queries, [])

    def test_dry_run_reports_exactly_ten_frozen_questions(self):
        config = _execute_config(execute=False)
        fixture = _load_fixture()
        plan = compute_replay_execution_plan(
            fixture,
            candidate_limit=DEFAULT_CANDIDATE_LIMIT,
        )
        output = format_dry_run_plan(config, plan=plan)

        self.assertEqual(plan.question_count, FROZEN_QUESTION_COUNT)
        self.assertEqual(plan.question_count, 10)
        self.assertIn("question_count: 10", output)
        self.assertIn("cold_cache_max_provider_requests:", output)
        self.assertIn("distinct_query_identity_count:", output)
        self.assertIn("distinct_chunk_identity_count:", output)

    def test_dry_run_prints_redacted_plan_and_exits_successfully(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["--run-id", _RUN_ID])
        output = stdout.getvalue()

        self.assertEqual(exit_code, 0)
        self.assertIn("dry-run", output)
        self.assertIn(_RUN_ID, output)
        self.assertIn("qualified_v2_applied: false", output)
        self.assertIn("authoritative_text_resolution: planned_not_executed", output)
        self.assertIn("semantic_evidence_collected: false", output)
        self.assertIn("semantic_threshold_applied: false", output)
        self.assertNotIn(_API_KEY, output)
        self.assertNotIn(_SERVICE_ROLE, output)

    def test_execute_rejects_fixture_embedding_text_resolver(self):
        config = _execute_config()

        def synthetic_factory(_client: Any, fixture: Mapping[str, Any], _limit: int) -> Any:
            return build_fixture_embedding_text_resolver(fixture)

        with self.assertRaises(RealHybridReplayTextResolutionError):
            run_real_hybrid_replay(
                config,
                env=_REQUIRED_ENV,
                client_factory=_MockSupabaseClient,
                transport_factory=lambda: RecordingTransport(),
                resolver_factory=synthetic_factory,
            )

    def test_stale_model_version_is_rejected(self):
        config = _execute_config(model_version="2024-01-15")
        plan = compute_replay_execution_plan(
            _load_fixture(),
            candidate_limit=DEFAULT_CANDIDATE_LIMIT,
        )
        with self.assertRaises(Exception) as ctx:
            validate_execute_configuration(config, env=_REQUIRED_ENV, plan=plan)
        self.assertIn("stale", str(ctx.exception))

    def test_max_provider_requests_above_ceiling_is_rejected(self):
        config = _execute_config(max_provider_requests=22)
        plan = compute_replay_execution_plan(
            _load_fixture(),
            candidate_limit=DEFAULT_CANDIDATE_LIMIT,
        )
        with self.assertRaises(Exception) as ctx:
            validate_execute_configuration(config, env=_REQUIRED_ENV, plan=plan)
        self.assertIn("cold-cache bound", str(ctx.exception))

    def test_cold_cache_upper_bound_matches_fixture_semantics(self):
        fixture = _load_fixture()
        plan = compute_replay_execution_plan(
            fixture,
            candidate_limit=DEFAULT_CANDIDATE_LIMIT,
        )
        self.assertEqual(plan.semantic_review_question_count, 7)
        self.assertEqual(plan.cold_cache_max_provider_requests, 21)
        self.assertEqual(plan.distinct_query_identity_count, 7)


class TestRealHybridReplayExecutionGates(unittest.TestCase):
    def test_missing_environment_variables_fail_before_external_calls(self):
        config = _execute_config()
        transport = RecordingTransport()
        client = _MockSupabaseClient()
        env = dict(_REQUIRED_ENV)
        env.pop(ENV_OPENAI_API_KEY)

        with self.assertRaises(RealHybridReplayEnvironmentError) as ctx:
            run_real_hybrid_replay(
                config,
                env=env,
                client_factory=lambda: client,
                transport_factory=lambda: transport,
            )

        self.assertIn(ENV_OPENAI_API_KEY, str(ctx.exception))
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.queries, [])

    def test_execute_requires_max_provider_requests(self):
        config = _execute_config(max_provider_requests=None)
        plan = compute_replay_execution_plan(
            _load_fixture(),
            candidate_limit=DEFAULT_CANDIDATE_LIMIT,
        )
        with self.assertRaises(Exception) as ctx:
            validate_execute_configuration(
                config,
                env=_REQUIRED_ENV,
                plan=plan,
            )
        self.assertIn("--max-provider-requests", str(ctx.exception))

    def test_fixture_with_wrong_question_count_is_rejected(self):
        bad_fixture = {"questions": [{"question_version_id": "only-one"}]}
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(bad_fixture, handle)
            temp_path = handle.name
        try:
            with self.assertRaises(Exception) as ctx:
                load_frozen_replay_fixture(fixture_path=temp_path)
            self.assertIn("exactly", str(ctx.exception))
        finally:
            os.remove(temp_path)


class TestRealHybridReplayProviderBudget(unittest.TestCase):
    def test_provider_request_ceiling_is_checked_before_next_request(self):
        budget = ProviderRequestBudget(max_provider_requests=2)
        provider = BudgetEnforcingEmbeddingProvider(
            inner=SimpleCountingProvider(),
            budget=budget,
        )

        provider.embed(
            text="one",
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
        )
        provider.embed(
            text="two",
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
        )
        with self.assertRaises(RealHybridReplayBudgetError):
            provider.embed(
                text="three",
                embedding_provider_name=OPENAI_PROVIDER_NAME,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
            )
        self.assertEqual(budget.provider_request_count, 2)

    def test_runtime_ceiling_aborts_before_exceeding_user_max(self):
        config = _execute_config(max_provider_requests=5)
        transport = RecordingTransport()
        client = _MockSupabaseClient()

        with self.assertRaises(RealHybridReplayBudgetError):
            run_real_hybrid_replay(
                config,
                env=_REQUIRED_ENV,
                client_factory=lambda: client,
                transport_factory=lambda: transport,
                resolver_factory=_authoritative_resolver_factory,
            )

        self.assertEqual(len(transport.calls), 5)
        self.assertNotIn("retrieval_shadow_evaluations", client.tables_touched)

    def test_cache_hits_do_not_increment_provider_request_count(self):
        fixture = _load_fixture()
        resolver = build_fixture_embedding_text_resolver(fixture)
        shared = {
            "embedding_provider_name": OPENAI_PROVIDER_NAME,
            "embedding_model_name": _MODEL,
            "embedding_model_version": _VERSION,
            "embedding_dimensions": _DIMENSIONS,
        }
        preloaded_rows: List[EmbeddingCacheRecord] = []
        for record in fixture["questions"]:
            question_version_id = str(record["question_version_id"])
            shadow = classify_question_shadow_from_replay_record(record)
            if shadow["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
                continue
            question_text = resolver.resolve_question_embedding_text(question_version_id)
            assert question_text is not None
            query_identity = build_cache_identity(
                text=question_text,
                content_scope=CONTENT_SCOPE_QUERY,
                **shared,
            )
            preloaded_rows.append(
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

        class PreloadedRepository:
            def __init__(self) -> None:
                self._rows = {
                    (
                        row.content_scope,
                        row.content_hash,
                        row.embedding_provider_name,
                        row.embedding_model_name,
                        row.embedding_model_version,
                        row.embedding_dimensions,
                    ): row
                    for row in preloaded_rows
                }

            def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
                return self._rows.get(identity.as_tuple())

            def insert(self, record: EmbeddingCacheRecord) -> None:
                self._rows[record.content_scope, record.content_hash, record.embedding_provider_name, record.embedding_model_name, record.embedding_model_version, record.embedding_dimensions] = record

        budget = ProviderRequestBudget(max_provider_requests=100)
        inner = SimpleCountingProvider()
        provider = BudgetEnforcingEmbeddingProvider(inner=inner, budget=budget)

        run_hybrid_replay_from_records(
            replay_records=fixture["questions"],
            candidate_limit=DEFAULT_CANDIDATE_LIMIT,
            embedding_text_resolver=resolver,
            repository=PreloadedRepository(),
            provider=provider,
            **shared,
        )

        self.assertEqual(budget.provider_request_count, 14)


class TestRealHybridReplayScopeAndPrivacy(unittest.TestCase):
    def test_execution_is_limited_to_frozen_fixture_questions(self):
        fixture = _load_fixture()
        self.assertEqual(len(fixture["questions"]), FROZEN_QUESTION_COUNT)

        config = _execute_config(max_provider_requests=21)
        transport = RecordingTransport()
        client = _MockSupabaseClient()

        summary = run_real_hybrid_replay(
            config,
            env=_REQUIRED_ENV,
            fixture=fixture,
            client_factory=lambda: client,
            transport_factory=lambda: transport,
            resolver_factory=_authoritative_resolver_factory,
        )

        self.assertEqual(summary["question_count"], FROZEN_QUESTION_COUNT)
        self.assertEqual(len(summary["questions"]), FROZEN_QUESTION_COUNT)
        self.assertEqual(summary["provider_request_count"], 21)

    def test_no_qualification_threshold_is_applied(self):
        config = _execute_config(max_provider_requests=21)
        summary = run_real_hybrid_replay(
            config,
            env=_REQUIRED_ENV,
            client_factory=_MockSupabaseClient,
            transport_factory=lambda: RecordingTransport(),
            resolver_factory=_authoritative_resolver_factory,
        )
        serialized = format_redacted_summary(summary)

        self.assertFalse(summary["qualified_v2_applied"])
        self.assertFalse(summary["semantic_threshold_applied"])
        self.assertTrue(summary["authoritative_text_used"])
        self.assertNotIn('"qualified_v2":', serialized)
        for item in summary["questions"]:
            self.assertNotIn("qualified_v2", item)

    def test_json_output_is_redacted(self):
        config = _execute_config(max_provider_requests=21)
        summary = run_real_hybrid_replay(
            config,
            env=_REQUIRED_ENV,
            client_factory=_MockSupabaseClient,
            transport_factory=lambda: RecordingTransport(),
            resolver_factory=_authoritative_resolver_factory,
        )
        serialized = format_redacted_summary(summary)

        self.assertNotIn(_API_KEY, serialized)
        self.assertNotIn(_SERVICE_ROLE, serialized)
        self.assertNotIn(_SENSITIVE_TEXT, serialized)
        self.assertNotIn("Authority query payload", serialized)
        self.assertNotIn("Authority chunk payload", serialized)
        self.assertNotIn("embedding_vector", serialized)
        self.assertNotIn("synthetic-question", serialized)
        self.assertNotIn("synthetic-candidate", serialized)
        self.assertIn(_RUN_ID, serialized)

        fixture = _load_fixture()
        for record in fixture["questions"]:
            shadow = classify_question_shadow_from_replay_record(record)
            for candidate in shadow["candidates"]:
                self.assertNotIn(str(candidate["title"]), serialized)

    def test_no_retrieval_shadow_evaluations_write_occurs(self):
        client = _MockSupabaseClient()
        run_real_hybrid_replay(
            _execute_config(max_provider_requests=21),
            env=_REQUIRED_ENV,
            client_factory=lambda: client,
            transport_factory=lambda: RecordingTransport(),
            resolver_factory=_authoritative_resolver_factory,
        )
        self.assertTrue(all(name == TABLE_NAME for name in client.tables_touched))

    def test_structural_guards_are_preserved_in_redacted_summary(self):
        summary = run_real_hybrid_replay(
            _execute_config(max_provider_requests=21),
            env=_REQUIRED_ENV,
            client_factory=_MockSupabaseClient,
            transport_factory=lambda: RecordingTransport(),
            resolver_factory=_authoritative_resolver_factory,
        )
        for item in summary["questions"]:
            self.assertIn("confidence_class", item)
            self.assertIn("qualified_count_v1", item)
            self.assertIn("structural_candidate_count", item)

    def test_provider_failure_returns_redacted_failed_summary_without_secrets(self):
        class ExplodingTransport(RecordingTransport):
            def post_json(self, **kwargs: Any) -> HttpResponse:
                raise RuntimeError("transport exploded with secret " + _API_KEY)

        from scripts.v48_real_hybrid_replay import build_initial_summary, compute_replay_execution_plan

        config = _execute_config(max_provider_requests=21)
        plan = compute_replay_execution_plan(
            _load_fixture(),
            candidate_limit=DEFAULT_CANDIDATE_LIMIT,
        )
        summary = build_initial_summary(config, plan=plan)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(RealHybridReplayProviderError):
                run_real_hybrid_replay(
                    config,
                    env=_REQUIRED_ENV,
                    client_factory=_MockSupabaseClient,
                    transport_factory=lambda: ExplodingTransport(),
                    resolver_factory=_authoritative_resolver_factory,
                    summary=summary,
                )

        combined = stderr.getvalue() + format_redacted_summary(summary)
        self.assertNotIn(_API_KEY, combined)
        self.assertNotIn(_SERVICE_ROLE, combined)
        self.assertIn(_RUN_ID, combined)


class TestRealHybridReplayIsolation(unittest.TestCase):
    def test_no_live_worker_imports_real_replay_runner(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        workers_dir = os.path.join(repo_root, "workers")
        offenders = []
        for name in os.listdir(workers_dir):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            path = os.path.join(workers_dir, name)
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
            if "v48_real_hybrid_replay" in contents:
                offenders.append(name)
        self.assertEqual(offenders, [])

    def test_runner_does_not_reference_job_queue_or_shadow_evaluations(self):
        runner_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
            "v48_real_hybrid_replay.py",
        )
        with open(runner_path, encoding="utf-8") as handle:
            contents = handle.read()
        self.assertNotIn("retrieval_shadow_evaluations", contents.replace(
            "retrieval_shadow_evaluations_written", ""
        ))
        self.assertNotIn("job_queue", contents)
        self.assertNotIn("audit_runs", contents)


class TestRealHybridReplayHelpers(unittest.TestCase):
    def test_parse_args_defaults_to_dry_run(self):
        config = parse_args(["--run-id", _RUN_ID])
        self.assertFalse(config.execute)
        self.assertEqual(config.run_id, _RUN_ID)

    def test_passed_summary_dict_is_updated_in_place(self):
        from scripts.v48_real_hybrid_replay import build_initial_summary, compute_replay_execution_plan

        config = _execute_config(max_provider_requests=21)
        plan = compute_replay_execution_plan(
            _load_fixture(),
            candidate_limit=DEFAULT_CANDIDATE_LIMIT,
        )
        summary = build_initial_summary(config, plan=plan)
        returned = run_real_hybrid_replay(
            config,
            env=_REQUIRED_ENV,
            client_factory=_MockSupabaseClient,
            transport_factory=lambda: RecordingTransport(),
            resolver_factory=_authoritative_resolver_factory,
            summary=summary,
        )
        self.assertIs(returned, summary)
        self.assertEqual(summary["final_status"], "success")
        self.assertTrue(summary["authoritative_text_used"])
        self.assertTrue(summary["semantic_evidence_collected"])
        self.assertTrue(summary["replay_content_set_hash"])


class TestRealHybridReplayFailureClassification(unittest.TestCase):
    def _run_execute_with_summary(self, **kwargs: Any) -> dict[str, Any]:
        from scripts.v48_real_hybrid_replay import build_initial_summary, compute_replay_execution_plan

        config = kwargs.pop("config", _execute_config(max_provider_requests=21))
        summary = build_initial_summary(
            config,
            plan=compute_replay_execution_plan(
                _load_fixture(),
                candidate_limit=DEFAULT_CANDIDATE_LIMIT,
            ),
        )
        with self.assertRaises(Exception):
            run_real_hybrid_replay(
                config,
                env=_REQUIRED_ENV,
                client_factory=kwargs.pop("client_factory", _MockSupabaseClient),
                transport_factory=kwargs.pop("transport_factory", lambda: RecordingTransport()),
                summary=summary,
                **kwargs,
            )
        return summary

    def test_execute_preflight_supabase_client_failure(self):
        def broken_client_factory() -> Any:
            from utils.access_control import SupabaseAdminConfigError

            raise SupabaseAdminConfigError("Missing Supabase admin configuration.")

        summary = self._run_execute_with_summary(
            client_factory=broken_client_factory,
            resolver_factory=_authoritative_resolver_factory,
        )
        self.assertEqual(summary["final_status"], "failed")
        self.assertEqual(summary["failure_stage"], FAILURE_STAGE_EXECUTE_PREFLIGHT)
        self.assertEqual(summary["error_code"], "execute_environment_invalid")
        self.assertEqual(summary["provider_request_count"], 0)
        self.assertEqual(summary["authoritative_text_resolution"], "failed")
        self.assertFalse(summary["authoritative_text_used"])

    def test_question_context_failure_reports_specific_stage_and_code(self):
        def failing_resolver_factory(
            _client: Any,
            fixture: Mapping[str, Any],
            candidate_limit: int,
        ) -> AuthoritativeEmbeddingTextResolver:
            resolver = build_supabase_authoritative_embedding_text_resolver(
                _client,
                fixture,
                candidate_limit=candidate_limit,
            )
            original_prepare = resolver.prepare

            def _prepare() -> None:
                raise AuthoritativeEmbeddingTextError(
                    "authoritative question context could not be loaded",
                    error_code="authoritative_question_context_failed",
                    failure_stage=FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION,
                    error_type="context_loader",
                    error_detail="RPC call failed: timeout",
                )

            resolver.prepare = _prepare  # type: ignore[method-assign]
            return resolver

        summary = self._run_execute_with_summary(
            resolver_factory=failing_resolver_factory,
        )
        self.assertEqual(summary["failure_stage"], FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION)
        self.assertEqual(summary["error_code"], "authoritative_question_context_failed")
        self.assertEqual(summary["provider_request_count"], 0)
        self.assertEqual(summary["authoritative_text_resolution"], "failed")
        self.assertEqual(summary["replay_content_set_hash"], "")

    def test_authoritative_matching_failure_keeps_provider_count_zero(self):
        def failing_resolver_factory(
            _client: Any,
            fixture: Mapping[str, Any],
            candidate_limit: int,
        ) -> AuthoritativeEmbeddingTextResolver:
            resolver = _authoritative_resolver_factory(_client, fixture, candidate_limit)

            def _prepare() -> None:
                raise AuthoritativeEmbeddingTextError(
                    "authoritative candidate chunk not found",
                    error_code="authoritative_candidate_not_found",
                    failure_stage=FAILURE_STAGE_AUTHORITATIVE_MATCHING,
                    error_type="authoritative_resolution",
                    error_detail="no live candidate chunk matched the frozen replay identity",
                )

            resolver.prepare = _prepare  # type: ignore[method-assign]
            return resolver

        summary = self._run_execute_with_summary(
            resolver_factory=failing_resolver_factory,
        )
        self.assertEqual(summary["failure_stage"], FAILURE_STAGE_AUTHORITATIVE_MATCHING)
        self.assertEqual(summary["error_code"], "authoritative_candidate_not_found")
        self.assertEqual(summary["provider_request_count"], 0)

    def test_preparation_failure_performs_no_cache_writes(self):
        client = _MockSupabaseClient()

        def failing_resolver_factory(
            _client: Any,
            fixture: Mapping[str, Any],
            candidate_limit: int,
        ) -> AuthoritativeEmbeddingTextResolver:
            resolver = _authoritative_resolver_factory(_client, fixture, candidate_limit)

            def _prepare() -> None:
                raise AuthoritativeEmbeddingTextError(
                    "authoritative question context could not be loaded",
                    error_code="authoritative_question_context_failed",
                    failure_stage=FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION,
                    error_type="context_loader",
                    error_detail="RPC call failed: timeout",
                )

            resolver.prepare = _prepare  # type: ignore[method-assign]
            return resolver

        self._run_execute_with_summary(
            client_factory=lambda: client,
            resolver_factory=failing_resolver_factory,
        )
        self.assertEqual(client.rows, {})

    def test_successful_mocked_preparation_reports_completed_resolution(self):
        from scripts.v48_real_hybrid_replay import build_initial_summary, compute_replay_execution_plan

        config = _execute_config(max_provider_requests=21)
        summary = build_initial_summary(
            config,
            plan=compute_replay_execution_plan(
                _load_fixture(),
                candidate_limit=DEFAULT_CANDIDATE_LIMIT,
            ),
        )
        run_real_hybrid_replay(
            config,
            env=_REQUIRED_ENV,
            client_factory=_MockSupabaseClient,
            transport_factory=lambda: RecordingTransport(),
            resolver_factory=_authoritative_resolver_factory,
            summary=summary,
        )
        self.assertEqual(summary["authoritative_text_resolution"], "completed")
        self.assertTrue(summary["authoritative_text_used"])
        self.assertTrue(summary["replay_content_set_hash"])

    def test_planned_not_executed_only_in_dry_run(self):
        dry_summary = run_real_hybrid_replay(
            _execute_config(execute=False),
            env=_REQUIRED_ENV,
        )
        self.assertEqual(dry_summary["authoritative_text_resolution"], "planned_not_executed")

        from scripts.v48_real_hybrid_replay import build_initial_summary, compute_replay_execution_plan

        execute_summary = build_initial_summary(
            _execute_config(max_provider_requests=21),
            plan=compute_replay_execution_plan(
                _load_fixture(),
                candidate_limit=DEFAULT_CANDIDATE_LIMIT,
            ),
        )
        self.assertEqual(execute_summary["authoritative_text_resolution"], "pending")

    def test_unexpected_exception_produces_sanitized_diagnostic(self):
        def exploding_client_factory() -> Any:
            raise RuntimeError(f"unexpected with secret {_API_KEY}")

        summary = self._run_execute_with_summary(
            client_factory=exploding_client_factory,
            resolver_factory=_authoritative_resolver_factory,
        )
        serialized = format_redacted_summary(summary)
        self.assertEqual(summary["failure_stage"], FAILURE_STAGE_EXECUTE_PREFLIGHT)
        self.assertEqual(summary["error_code"], "unexpected_execution_failure")
        self.assertEqual(summary["error_type"], "unexpected")
        self.assertNotIn(_API_KEY, serialized)
        self.assertNotIn(_API_KEY, summary["error_detail"])

    def test_provider_failure_after_prepare_reports_embedding_stage(self):
        class ExplodingTransport(RecordingTransport):
            def post_json(self, **kwargs: Any) -> HttpResponse:
                raise RuntimeError("transport exploded with secret " + _API_KEY)

        from scripts.v48_real_hybrid_replay import build_initial_summary, compute_replay_execution_plan

        config = _execute_config(max_provider_requests=21)
        summary = build_initial_summary(
            config,
            plan=compute_replay_execution_plan(
                _load_fixture(),
                candidate_limit=DEFAULT_CANDIDATE_LIMIT,
            ),
        )
        with self.assertRaises(RealHybridReplayProviderError):
            run_real_hybrid_replay(
                config,
                env=_REQUIRED_ENV,
                client_factory=_MockSupabaseClient,
                transport_factory=lambda: ExplodingTransport(),
                resolver_factory=_authoritative_resolver_factory,
                summary=summary,
            )
        self.assertEqual(summary["failure_stage"], FAILURE_STAGE_EMBEDDING_EXECUTION)
        self.assertEqual(summary["error_code"], "embedding_provider_failed")
        self.assertEqual(summary["authoritative_text_resolution"], "completed")
        self.assertTrue(summary["authoritative_text_used"])
        self.assertTrue(summary["replay_content_set_hash"])

    def test_redacted_failure_summary_has_no_raw_authoritative_text(self):
        def failing_resolver_factory(
            _client: Any,
            fixture: Mapping[str, Any],
            candidate_limit: int,
        ) -> AuthoritativeEmbeddingTextResolver:
            resolver = _authoritative_resolver_factory(_client, fixture, candidate_limit)

            def _prepare() -> None:
                raise AuthoritativeEmbeddingTextError(
                    "authoritative candidate chunk not found",
                    error_code="authoritative_candidate_not_found",
                    failure_stage=FAILURE_STAGE_AUTHORITATIVE_MATCHING,
                    error_type="authoritative_resolution",
                    error_detail="no live candidate chunk matched the frozen replay identity",
                )

            resolver.prepare = _prepare  # type: ignore[method-assign]
            return resolver

        summary = self._run_execute_with_summary(
            resolver_factory=failing_resolver_factory,
        )
        serialized = format_redacted_summary(summary)
        self.assertNotIn("Authority query payload", serialized)
        self.assertNotIn("Authority chunk payload", serialized)
        self.assertIn("authoritative_candidate_not_found", serialized)

    def test_chunk_limit_validation_failure_before_rpc(self):
        fixture = _load_fixture()
        record = next(
            question
            for question in fixture["questions"]
            if classify_question_shadow_from_replay_record(question)["confidence_class"]
            == CONFIDENCE_CLASS_SEMANTIC_REVIEW
        )
        blind_context = {
            "question_version_id": str(record["question_version_id"]),
            "certification_exam_name": "Salesforce Certified Administrator",
            "question_text": "Sample stem",
            "domain_name": "Configuration",
            "options": [],
        }
        client = _MockSupabaseClient()

        def supabase_resolver_factory(
            injected_client: Any,
            loaded_fixture: Mapping[str, Any],
            candidate_limit: int,
        ) -> AuthoritativeEmbeddingTextResolver:
            return build_supabase_authoritative_embedding_text_resolver(
                injected_client,
                loaded_fixture,
                candidate_limit=candidate_limit,
            )

        with mock.patch(
            "workers.v48_hybrid_replay_authoritative_text.load_blind_audit_context",
            return_value=blind_context,
        ), mock.patch(
            "workers.v48_hybrid_replay_authoritative_text._load_active_resources",
            return_value=[
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "title": "Resource",
                    "metadata": {},
                    "resource_type": "doc",
                }
            ],
        ), mock.patch(
            "workers.v48_hybrid_replay_authoritative_text.validate_authoritative_candidate_chunk_limit",
            side_effect=AuthoritativeEmbeddingTextError(
                "authoritative candidate chunk limit is outside the RPC contract",
                error_code="authoritative_chunk_request_invalid",
                failure_stage=FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
                error_type="input_validation",
                error_detail="p_max_chunks must be between 1 and 200, got 250",
            ),
        ), mock.patch(
            "workers.v48_hybrid_replay_authoritative_text._list_candidate_chunks",
        ) as list_mock:
            summary = self._run_execute_with_summary(
                client_factory=lambda: client,
                resolver_factory=supabase_resolver_factory,
            )
            list_mock.assert_not_called()

        self.assertEqual(summary["failure_stage"], FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION)
        self.assertEqual(summary["error_code"], "authoritative_chunk_request_invalid")
        self.assertEqual(summary["error_type"], "input_validation")
        self.assertEqual(summary["authoritative_text_resolution"], "failed")
        self.assertFalse(summary["authoritative_text_used"])
        self.assertEqual(summary["provider_request_count"], 0)
        self.assertEqual(summary["replay_content_set_hash"], "")
        self.assertFalse(summary["semantic_evidence_collected"])
        self.assertEqual(client.rows, {})


if __name__ == "__main__":
    unittest.main()
