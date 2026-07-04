"""Tests for offline hybrid_question_match_v2 end-to-end replay harness."""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from typing import Any, Dict, List, Mapping, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_evidence import RETRIEVAL_METHOD
from workers.ai_quality_audit_hybrid_replay import (
    HYBRID_REPLAY_SCHEMA_VERSION,
    HybridReplayConfigError,
    HybridReplayEmbeddingTextError,
    HybridReplayStage2Error,
    build_replay_candidate_identity,
    dumps_hybrid_replay_result,
    run_hybrid_replay_from_records,
)
from workers.ai_quality_audit_semantic import (
    STATUS_COMPLETED,
    STATUS_SKIPPED_V1_SUFFICIENT,
    SemanticEvaluationError,
)
from workers.ai_quality_audit_shadow import (
    CONFIDENCE_CLASS_NO_STRUCTURAL,
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    CONFIDENCE_CLASS_V1_SUFFICIENT,
    PROPOSED_RETRIEVAL_METHOD,
    classify_question_shadow_from_replay_record,
)
from workers.embedding_cache import (
    CONTENT_SCOPE_CHUNK,
    CONTENT_SCOPE_QUERY,
    EmbeddingCacheIdentity,
    EmbeddingCacheRecord,
    EmbeddingProviderResponse,
)

_FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "v48_retrieval_replay_v1.json",
)

_STRICT_FUNCTIONAL = frozenset(
    {
        "39b3fd46-a448-49c5-bc26-303b0a4f4497",
        "a8b305ed-b342-4e12-9ee2-47ab29db6ea2",
        "3cb5b76e-c803-4a44-b621-e38fafe56211",
    }
)

_PROVIDER = "fake_provider"
_MODEL = "fake-model"
_VERSION = "v1"
_DIMENSIONS = 3
_RESPONSE_HASH = "a" * 64
_CANDIDATE_LIMIT = 2
_SENSITIVE_QUESTION_TEXT = "SECRET QUESTION TEXT MUST NOT LEAK"
_SENSITIVE_CANDIDATE_TEXT = "SECRET CANDIDATE TEXT MUST NOT LEAK"
_API_KEY = "sk-test-key-not-real"
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_FIRST_RUN_PROVIDER_CALLS = 7 * (1 + _CANDIDATE_LIMIT)


def _vector(*values: float) -> Tuple[float, ...]:
    return tuple(float(value) for value in values)


def _load_fixture() -> dict[str, Any]:
    with open(_FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


class FakeEmbeddingProvider:
    def __init__(
        self,
        *,
        vectors_by_text: Optional[Dict[str, Tuple[float, ...]]] = None,
        default_vector: Tuple[float, ...] = _vector(1.0, 0.0, 0.0),
    ) -> None:
        self.vectors_by_text = vectors_by_text or {}
        self.default_vector = default_vector
        self.calls: List[dict[str, Any]] = []

    def embed(self, **kwargs: Any) -> EmbeddingProviderResponse:
        self.calls.append(
            {
                "embedding_provider_name": kwargs["embedding_provider_name"],
                "embedding_model_name": kwargs["embedding_model_name"],
                "embedding_model_version": kwargs["embedding_model_version"],
                "embedding_dimensions": kwargs["embedding_dimensions"],
            }
        )
        vector = self.vectors_by_text.get(kwargs["text"], self.default_vector)
        return EmbeddingProviderResponse(
            embedding_vector=vector,
            provider_response_hash=_RESPONSE_HASH,
        )


class FakeEmbeddingCacheRepository:
    def __init__(
        self,
        rows: Optional[List[EmbeddingCacheRecord]] = None,
    ) -> None:
        self._rows: Dict[Tuple[str, str, str, str, str, int], EmbeddingCacheRecord] = {}
        self.lookup_calls = 0
        self.insert_calls = 0
        for row in rows or []:
            key = (
                row.content_scope,
                row.content_hash,
                row.embedding_provider_name,
                row.embedding_model_name,
                row.embedding_model_version,
                row.embedding_dimensions,
            )
            self._rows[key] = row

    def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
        self.lookup_calls += 1
        return self._rows.get(identity.as_tuple())

    def insert(self, record: EmbeddingCacheRecord) -> None:
        self.insert_calls += 1
        key = (
            record.content_scope,
            record.content_hash,
            record.embedding_provider_name,
            record.embedding_model_name,
            record.embedding_model_version,
            record.embedding_dimensions,
        )
        self._rows[key] = record


class SyntheticEmbeddingTextResolver:
    def __init__(
        self,
        *,
        question_text_by_id: Mapping[str, str],
        candidate_text_by_identity: Mapping[str, str],
    ) -> None:
        self.question_text_by_id = dict(question_text_by_id)
        self.candidate_text_by_identity = dict(candidate_text_by_identity)

    def resolve_question_embedding_text(self, question_version_id: str) -> Optional[str]:
        return self.question_text_by_id.get(question_version_id)

    def resolve_candidate_embedding_text(self, candidate_identity: str) -> Optional[str]:
        return self.candidate_text_by_identity.get(candidate_identity)


def _identity_for_shadow_candidate(
    *,
    question_version_id: str,
    candidate_position: int,
    candidate: Mapping[str, Any],
) -> str:
    return build_replay_candidate_identity(
        question_version_id=question_version_id,
        candidate_position=candidate_position,
        title=str(candidate["title"]),
        resource_type=str(candidate.get("resource_type") or ""),
    )


def _top_stage2_selected_identity(
    record: Mapping[str, Any],
    *,
    candidate_limit: int,
) -> str:
    question_version_id = str(record["question_version_id"])
    shadow = classify_question_shadow_from_replay_record(record)
    eligible: list[tuple[float, str]] = []
    for candidate_position, candidate in enumerate(shadow["candidates"]):
        if not candidate["l1_structural_guards_pass"]:
            continue
        eligible.append(
            (
                float(candidate["relevance_score"]),
                _identity_for_shadow_candidate(
                    question_version_id=question_version_id,
                    candidate_position=candidate_position,
                    candidate=candidate,
                ),
            )
        )
    eligible.sort(key=lambda item: (-item[0], item[1]))
    return eligible[0][1]


def _build_fixture_resolver(fixture: Mapping[str, Any]) -> SyntheticEmbeddingTextResolver:
    question_text_by_id: dict[str, str] = {}
    candidate_text_by_identity: dict[str, str] = {}

    for record in fixture["questions"]:
        question_version_id = str(record["question_version_id"])
        question_text_by_id[question_version_id] = (
            f"synthetic-question-{question_version_id[:8]}"
        )
        shadow = classify_question_shadow_from_replay_record(record)
        for candidate_position, candidate in enumerate(shadow["candidates"]):
            identity = _identity_for_shadow_candidate(
                question_version_id=question_version_id,
                candidate_position=candidate_position,
                candidate=candidate,
            )
            candidate_text_by_identity[identity] = f"synthetic-candidate-{identity}"

    return SyntheticEmbeddingTextResolver(
        question_text_by_id=question_text_by_id,
        candidate_text_by_identity=candidate_text_by_identity,
    )


def _run_fixture_replay(
    *,
    fixture: Optional[Mapping[str, Any]] = None,
    candidate_limit: int = _CANDIDATE_LIMIT,
    provider: Optional[FakeEmbeddingProvider] = None,
    repository: Optional[FakeEmbeddingCacheRepository] = None,
    resolver: Optional[SyntheticEmbeddingTextResolver] = None,
) -> dict[str, Any]:
    loaded = fixture or _load_fixture()
    return run_hybrid_replay_from_records(
        replay_records=loaded["questions"],
        candidate_limit=candidate_limit,
        embedding_text_resolver=resolver or _build_fixture_resolver(loaded),
        embedding_provider_name=_PROVIDER,
        embedding_model_name=_MODEL,
        embedding_model_version=_VERSION,
        embedding_dimensions=_DIMENSIONS,
        repository=repository or FakeEmbeddingCacheRepository(),
        provider=provider or FakeEmbeddingProvider(),
    )


class TestHybridReplayFromFrozenFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _load_fixture()
        cls.question_records = {
            str(record["question_version_id"]): record
            for record in cls.fixture["questions"]
        }
        cls.baseline_classifications = {
            question_version_id: classify_question_shadow_from_replay_record(record)
            for question_version_id, record in cls.question_records.items()
        }
        cls.provider = FakeEmbeddingProvider()
        cls.repository = FakeEmbeddingCacheRepository()
        cls.result = _run_fixture_replay(
            fixture=cls.fixture,
            provider=cls.provider,
            repository=cls.repository,
        )

    def test_all_ten_questions_represented_once(self):
        self.assertEqual(self.result["question_count"], 10)
        question_ids = [item["question_version_id"] for item in self.result["questions"]]
        self.assertEqual(len(question_ids), len(set(question_ids)))
        self.assertEqual(set(question_ids), set(self.question_records))

    def test_output_shape(self):
        self.assertEqual(self.result["schema_version"], HYBRID_REPLAY_SCHEMA_VERSION)
        self.assertEqual(self.result["baseline_retrieval_method"], RETRIEVAL_METHOD)
        self.assertEqual(self.result["proposed_retrieval_method"], PROPOSED_RETRIEVAL_METHOD)
        self.assertEqual(self.result["candidate_limit"], _CANDIDATE_LIMIT)
        required_question_keys = {
            "question_version_id",
            "confidence_class",
            "qualified_count_v1",
            "structural_candidate_count",
            "semantic_result",
        }
        for item in self.result["questions"]:
            with self.subTest(question_version_id=item["question_version_id"]):
                self.assertEqual(required_question_keys, set(item.keys()))
                self.assertIn("schema_version", item["semantic_result"])
                self.assertIn("status", item["semantic_result"])

    def test_stage1_matches_frozen_baseline(self):
        for item in self.result["questions"]:
            question_version_id = item["question_version_id"]
            baseline = self.baseline_classifications[question_version_id]
            with self.subTest(question_version_id=question_version_id):
                self.assertEqual(item["confidence_class"], baseline["confidence_class"])
                self.assertEqual(item["qualified_count_v1"], baseline["qualified_count_v1"])
                self.assertEqual(
                    item["structural_candidate_count"],
                    baseline["structural_candidate_count"],
                )

    def test_frozen_fixture_classification_counts(self):
        self.assertEqual(
            self.result["stage1_classification_counts"][CONFIDENCE_CLASS_V1_SUFFICIENT],
            3,
        )
        self.assertEqual(
            self.result["stage1_classification_counts"][CONFIDENCE_CLASS_SEMANTIC_REVIEW],
            7,
        )
        self.assertEqual(
            self.result["stage1_classification_counts"].get(CONFIDENCE_CLASS_NO_STRUCTURAL, 0),
            0,
        )

    def test_stage2_invocation_counts(self):
        semantic_completed = [
            item
            for item in self.result["questions"]
            if item["confidence_class"] == CONFIDENCE_CLASS_SEMANTIC_REVIEW
        ]
        skipped_v1 = [
            item
            for item in self.result["questions"]
            if item["confidence_class"] == CONFIDENCE_CLASS_V1_SUFFICIENT
        ]
        self.assertEqual(len(semantic_completed), 7)
        self.assertEqual(len(skipped_v1), 3)
        for item in semantic_completed:
            self.assertEqual(item["semantic_result"]["status"], STATUS_COMPLETED)
            self.assertGreater(item["semantic_result"]["evaluated_candidate_count"], 0)
        for item in skipped_v1:
            self.assertEqual(
                item["semantic_result"]["status"],
                STATUS_SKIPPED_V1_SUFFICIENT,
            )
            self.assertEqual(item["semantic_result"]["evaluated_candidate_count"], 0)

    def test_strict_functional_questions_are_v1_sufficient(self):
        for question_version_id in sorted(_STRICT_FUNCTIONAL):
            item = next(
                question
                for question in self.result["questions"]
                if question["question_version_id"] == question_version_id
            )
            with self.subTest(question_version_id=question_version_id):
                self.assertEqual(item["confidence_class"], CONFIDENCE_CLASS_V1_SUFFICIENT)
                self.assertGreater(item["qualified_count_v1"], 0)

    def test_v1_sufficient_questions_make_zero_embedding_calls(self):
        for item in self.result["questions"]:
            if item["confidence_class"] != CONFIDENCE_CLASS_V1_SUFFICIENT:
                continue
            with self.subTest(question_version_id=item["question_version_id"]):
                self.assertEqual(
                    item["semantic_result"]["status"],
                    STATUS_SKIPPED_V1_SUFFICIENT,
                )
                self.assertEqual(item["semantic_result"]["evaluated_candidate_count"], 0)

    def test_provider_calls_obey_candidate_limit_bounds(self):
        expected_calls = 0
        for item in self.result["questions"]:
            if item["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
                continue
            evaluated = item["semantic_result"]["evaluated_candidate_count"]
            self.assertLessEqual(evaluated, _CANDIDATE_LIMIT)
            expected_calls += 1 + evaluated
        self.assertEqual(len(self.provider.calls), expected_calls)
        self.assertEqual(len(self.provider.calls), _EXPECTED_FIRST_RUN_PROVIDER_CALLS)

    def test_query_embedding_requested_once_per_stage2_question(self):
        for item in self.result["questions"]:
            if item["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
                continue
            with self.subTest(question_version_id=item["question_version_id"]):
                self.assertFalse(item["semantic_result"]["query_embedding_cache_hit"])
                self.assertGreaterEqual(item["semantic_result"]["evaluated_candidate_count"], 1)


class TestReplayCandidateIdentity(unittest.TestCase):
    _QUESTION_ID = "11111111-1111-1111-1111-111111111111"
    _OTHER_QUESTION_ID = "22222222-2222-2222-2222-222222222222"
    _TITLE = "Shared Candidate Title"
    _RESOURCE_TYPE = "official_resource"

    def test_identical_title_and_resource_type_receive_different_identities(self):
        first = build_replay_candidate_identity(
            question_version_id=self._QUESTION_ID,
            candidate_position=0,
            title=self._TITLE,
            resource_type=self._RESOURCE_TYPE,
        )
        second = build_replay_candidate_identity(
            question_version_id=self._QUESTION_ID,
            candidate_position=1,
            title=self._TITLE,
            resource_type=self._RESOURCE_TYPE,
        )
        self.assertNotEqual(first, second)

    def test_same_inputs_reproduce_same_identity(self):
        kwargs = {
            "question_version_id": self._QUESTION_ID,
            "candidate_position": 4,
            "title": self._TITLE,
            "resource_type": self._RESOURCE_TYPE,
        }
        self.assertEqual(
            build_replay_candidate_identity(**kwargs),
            build_replay_candidate_identity(**kwargs),
        )

    def test_changing_question_version_id_changes_identity(self):
        shared = {
            "candidate_position": 2,
            "title": self._TITLE,
            "resource_type": self._RESOURCE_TYPE,
        }
        first = build_replay_candidate_identity(
            question_version_id=self._QUESTION_ID,
            **shared,
        )
        second = build_replay_candidate_identity(
            question_version_id=self._OTHER_QUESTION_ID,
            **shared,
        )
        self.assertNotEqual(first, second)

    def test_changing_candidate_position_changes_identity(self):
        shared = {
            "question_version_id": self._QUESTION_ID,
            "title": self._TITLE,
            "resource_type": self._RESOURCE_TYPE,
        }
        first = build_replay_candidate_identity(candidate_position=0, **shared)
        second = build_replay_candidate_identity(candidate_position=1, **shared)
        self.assertNotEqual(first, second)

    def test_all_two_hundred_fifty_fixture_candidates_have_unique_identities(self):
        fixture = _load_fixture()
        for record in fixture["questions"]:
            question_version_id = str(record["question_version_id"])
            shadow = classify_question_shadow_from_replay_record(record)
            identities = [
                _identity_for_shadow_candidate(
                    question_version_id=question_version_id,
                    candidate_position=candidate_position,
                    candidate=candidate,
                )
                for candidate_position, candidate in enumerate(shadow["candidates"])
            ]
            with self.subTest(question_version_id=question_version_id):
                self.assertEqual(len(identities), len(set(identities)))
                self.assertEqual(
                    list(range(len(identities))),
                    list(range(len(shadow["candidates"]))),
                )


class TestHybridReplayCandidateLimitAndCache(unittest.TestCase):
    def test_l1_failing_candidates_never_request_embeddings(self):
        fixture = _load_fixture()
        record = next(
            item
            for item in fixture["questions"]
            if item["question_version_id"] == "1f181e6e-28dc-41d9-a31b-5512b5948f7d"
        )
        shadow = classify_question_shadow_from_replay_record(record)
        question_version_id = str(record["question_version_id"])
        l1_identities = {
            _identity_for_shadow_candidate(
                question_version_id=question_version_id,
                candidate_position=candidate_position,
                candidate=candidate,
            )
            for candidate_position, candidate in enumerate(shadow["candidates"])
            if candidate["l1_structural_guards_pass"]
        }
        resolver = _build_fixture_resolver({"questions": [record]})
        provider = FakeEmbeddingProvider()

        result = run_hybrid_replay_from_records(
            replay_records=[record],
            candidate_limit=1,
            embedding_text_resolver=resolver,
            embedding_provider_name=_PROVIDER,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            repository=FakeEmbeddingCacheRepository(),
            provider=provider,
        )

        evaluated_identities = {
            candidate["candidate_identity"]
            for candidate in result["questions"][0]["semantic_result"]["candidates"]
        }
        self.assertEqual(len(evaluated_identities), 1)
        self.assertTrue(evaluated_identities.issubset(l1_identities))
        self.assertEqual(len(provider.calls), 2)

    def test_no_selected_candidates_are_silently_merged(self):
        fixture = _load_fixture()
        result = _run_fixture_replay(fixture=fixture, candidate_limit=_CANDIDATE_LIMIT)
        for item in result["questions"]:
            if item["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
                continue
            semantic_candidates = item["semantic_result"]["candidates"]
            evaluated_identities = [candidate["candidate_identity"] for candidate in semantic_candidates]
            with self.subTest(question_version_id=item["question_version_id"]):
                self.assertEqual(
                    item["semantic_result"]["evaluated_candidate_count"],
                    len(evaluated_identities),
                )
                self.assertEqual(len(evaluated_identities), len(set(evaluated_identities)))

    def test_first_run_provider_calls_equal_twenty_one(self):
        fixture = _load_fixture()
        provider = FakeEmbeddingProvider()
        _run_fixture_replay(fixture=fixture, provider=provider, candidate_limit=_CANDIDATE_LIMIT)
        self.assertEqual(len(provider.calls), _EXPECTED_FIRST_RUN_PROVIDER_CALLS)

    def test_cache_reuse_prevents_provider_calls_on_second_run(self):
        fixture = _load_fixture()
        resolver = _build_fixture_resolver(fixture)
        provider = FakeEmbeddingProvider()
        repository = FakeEmbeddingCacheRepository()

        first = _run_fixture_replay(
            fixture=fixture,
            candidate_limit=_CANDIDATE_LIMIT,
            provider=provider,
            repository=repository,
        )
        first_calls = len(provider.calls)
        self.assertEqual(first_calls, _EXPECTED_FIRST_RUN_PROVIDER_CALLS)

        second = _run_fixture_replay(
            fixture=fixture,
            candidate_limit=_CANDIDATE_LIMIT,
            provider=provider,
            repository=repository,
            resolver=resolver,
        )

        self.assertEqual(len(provider.calls), first_calls)
        for item in second["questions"]:
            if item["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
                continue
            self.assertTrue(item["semantic_result"]["query_embedding_cache_hit"])
            for candidate in item["semantic_result"]["candidates"]:
                self.assertTrue(candidate["embedding_cache_hit"])

    def test_repeated_cached_runs_produce_byte_identical_json(self):
        fixture = _load_fixture()
        resolver = _build_fixture_resolver(fixture)
        repository = FakeEmbeddingCacheRepository()
        provider = FakeEmbeddingProvider()

        _run_fixture_replay(
            fixture=fixture,
            provider=provider,
            repository=repository,
            resolver=resolver,
        )
        second = _run_fixture_replay(
            fixture=fixture,
            provider=provider,
            repository=repository,
            resolver=resolver,
        )
        third = _run_fixture_replay(
            fixture=fixture,
            provider=provider,
            repository=repository,
            resolver=resolver,
        )

        second_payload = dumps_hybrid_replay_result(second)
        third_payload = dumps_hybrid_replay_result(third)
        self.assertEqual(second_payload, third_payload)


class TestHybridReplayFailureBehavior(unittest.TestCase):
    def test_missing_replay_records_fail_closed(self):
        with self.assertRaises(HybridReplayConfigError):
            run_hybrid_replay_from_records(
                replay_records=[],
                candidate_limit=_CANDIDATE_LIMIT,
                embedding_text_resolver=SyntheticEmbeddingTextResolver(
                    question_text_by_id={},
                    candidate_text_by_identity={},
                ),
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=FakeEmbeddingCacheRepository(),
                provider=FakeEmbeddingProvider(),
            )

    def test_duplicate_question_ids_fail_closed(self):
        fixture = _load_fixture()
        duplicate_records = [fixture["questions"][0], fixture["questions"][0]]
        with self.assertRaises(HybridReplayConfigError):
            run_hybrid_replay_from_records(
                replay_records=duplicate_records,
                candidate_limit=_CANDIDATE_LIMIT,
                embedding_text_resolver=_build_fixture_resolver(fixture),
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=FakeEmbeddingCacheRepository(),
                provider=FakeEmbeddingProvider(),
            )

    def test_invalid_candidate_limit_fail_closed(self):
        fixture = _load_fixture()
        with self.assertRaises(HybridReplayConfigError):
            _run_fixture_replay(fixture=fixture, candidate_limit=0)

    def test_missing_question_text_fail_closed(self):
        fixture = _load_fixture()
        semantic_id = next(
            record["question_version_id"]
            for record in fixture["questions"]
            if classify_question_shadow_from_replay_record(record)["confidence_class"]
            == CONFIDENCE_CLASS_SEMANTIC_REVIEW
        )
        resolver = _build_fixture_resolver(fixture)
        resolver.question_text_by_id.pop(semantic_id)

        with self.assertRaises(HybridReplayEmbeddingTextError) as ctx:
            _run_fixture_replay(fixture=fixture, resolver=resolver)

        self.assertNotIn(_SENSITIVE_QUESTION_TEXT, str(ctx.exception))
        self.assertNotIn(_API_KEY, str(ctx.exception))

    def test_missing_selected_candidate_text_fail_closed(self):
        fixture = _load_fixture()
        record = next(
            item
            for item in fixture["questions"]
            if classify_question_shadow_from_replay_record(item)["confidence_class"]
            == CONFIDENCE_CLASS_SEMANTIC_REVIEW
        )
        missing_identity = _top_stage2_selected_identity(record, candidate_limit=5)
        resolver = _build_fixture_resolver({"questions": [record]})
        resolver.candidate_text_by_identity.pop(missing_identity)

        with self.assertRaises(HybridReplayEmbeddingTextError) as ctx:
            run_hybrid_replay_from_records(
                replay_records=[record],
                candidate_limit=5,
                embedding_text_resolver=resolver,
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=FakeEmbeddingCacheRepository(),
                provider=FakeEmbeddingProvider(),
            )

        self.assertNotIn(_SENSITIVE_CANDIDATE_TEXT, str(ctx.exception))
        self.assertNotIn(_API_KEY, str(ctx.exception))

    def test_stage2_errors_fail_closed(self):
        class BrokenProvider(FakeEmbeddingProvider):
            def embed(self, **kwargs: Any) -> EmbeddingProviderResponse:
                raise RuntimeError("provider exploded")

        fixture = _load_fixture()
        record = next(
            item
            for item in fixture["questions"]
            if classify_question_shadow_from_replay_record(item)["confidence_class"]
            == CONFIDENCE_CLASS_SEMANTIC_REVIEW
        )

        with self.assertRaises(HybridReplayStage2Error) as ctx:
            run_hybrid_replay_from_records(
                replay_records=[record],
                candidate_limit=1,
                embedding_text_resolver=_build_fixture_resolver({"questions": [record]}),
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=FakeEmbeddingCacheRepository(),
                provider=BrokenProvider(),
            )

        self.assertIsInstance(ctx.exception.__cause__, SemanticEvaluationError)
        self.assertNotIn(_SENSITIVE_QUESTION_TEXT, str(ctx.exception))
        self.assertNotIn(_API_KEY, str(ctx.exception))


class TestHybridReplayPrivacyAndIsolation(unittest.TestCase):
    def test_output_and_errors_exclude_sensitive_data(self):
        fixture = _load_fixture()
        resolver = _build_fixture_resolver(fixture)
        for question_version_id in resolver.question_text_by_id:
            resolver.question_text_by_id[question_version_id] = _SENSITIVE_QUESTION_TEXT
        for identity in list(resolver.candidate_text_by_identity):
            resolver.candidate_text_by_identity[identity] = _SENSITIVE_CANDIDATE_TEXT

        result = _run_fixture_replay(fixture=fixture, resolver=resolver)
        serialized = dumps_hybrid_replay_result(result)

        self.assertNotIn(_SENSITIVE_QUESTION_TEXT, serialized)
        self.assertNotIn(_SENSITIVE_CANDIDATE_TEXT, serialized)
        self.assertNotIn(_API_KEY, serialized)
        self.assertNotIn("embedding_vector", serialized)
        self.assertNotIn("qualified_v2", serialized)

    def test_output_contains_only_opaque_identity_hashes(self):
        fixture = _load_fixture()
        result = _run_fixture_replay(fixture=fixture)
        serialized = dumps_hybrid_replay_result(result)

        self.assertNotIn("synthetic-question", serialized)
        self.assertNotIn("synthetic-candidate", serialized)
        self.assertNotIn(_SENSITIVE_QUESTION_TEXT, serialized)
        self.assertNotIn(_SENSITIVE_CANDIDATE_TEXT, serialized)

        for record in fixture["questions"]:
            shadow = classify_question_shadow_from_replay_record(record)
            for candidate in shadow["candidates"]:
                self.assertNotIn(str(candidate["title"]), serialized)

        for item in result["questions"]:
            for candidate in item["semantic_result"].get("candidates") or []:
                identity = candidate["candidate_identity"]
                self.assertRegex(identity, _SHA256_HEX_RE)

    def test_no_live_worker_imports_replay_harness(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        workers_dir = os.path.join(repo_root, "workers")
        offenders: List[str] = []
        for name in os.listdir(workers_dir):
            if not name.endswith(".py") or name in {
                "ai_quality_audit_hybrid_replay.py",
                "v48_hybrid_replay_authoritative_text.py",
                "__init__.py",
            }:
                continue
            path = os.path.join(workers_dir, name)
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
            if "ai_quality_audit_hybrid_replay" in contents:
                offenders.append(name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
