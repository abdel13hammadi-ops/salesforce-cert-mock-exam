"""Tests for offline hybrid_question_match_v2 Stage 2 semantic scoring."""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import unittest
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_semantic import (
    SEMANTIC_SCORING_SCHEMA_VERSION,
    STATUS_COMPLETED,
    STATUS_SKIPPED_NO_STRUCTURAL,
    STATUS_SKIPPED_V1_SUFFICIENT,
    SemanticEvaluationConfigError,
    SemanticEvaluationError,
    _cosine_similarity,
    dumps_semantic_scoring_result,
    evaluate_question_semantic_scoring,
)
from workers.ai_quality_audit_shadow import (
    CONFIDENCE_CLASS_NO_STRUCTURAL,
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    CONFIDENCE_CLASS_V1_SUFFICIENT,
    PROPOSED_RETRIEVAL_METHOD,
)
from workers.embedding_cache import (
    CONTENT_SCOPE_CHUNK,
    CONTENT_SCOPE_QUERY,
    EmbeddingCacheIdentity,
    EmbeddingCacheRecord,
    EmbeddingProviderResponse,
)

_PROVIDER = "fake_provider"
_MODEL = "fake-model"
_VERSION = "v1"
_DIMENSIONS = 3
_RESPONSE_HASH = "a" * 64
_QUESTION_ID = "11111111-1111-1111-1111-111111111111"
_SENSITIVE_QUESTION_TEXT = "SECRET QUESTION TEXT MUST NOT LEAK"
_SENSITIVE_CANDIDATE_TEXT = "SECRET CANDIDATE TEXT MUST NOT LEAK"
_API_KEY = "sk-test-key-not-real"


def _vector(*values: float) -> Tuple[float, ...]:
    return tuple(float(value) for value in values)


def _candidate(
    *,
    identity: str,
    text: str,
    relevance_score: float,
    l1_pass: bool = True,
    qualified_v1: bool = False,
    resource_type: str = "official_resource",
) -> dict[str, Any]:
    return {
        "candidate_identity": identity,
        "candidate_embedding_text": text,
        "relevance_score": relevance_score,
        "l1_structural_guards_pass": l1_pass,
        "qualified_v1": qualified_v1,
        "resource_type": resource_type,
    }


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


def _evaluate(
    *,
    confidence_class: str = CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    candidate_limit: int = 2,
    candidates: Optional[List[dict[str, Any]]] = None,
    provider: Optional[FakeEmbeddingProvider] = None,
    repository: Optional[FakeEmbeddingCacheRepository] = None,
    question_text: str = "question text",
) -> dict[str, Any]:
    if candidates is None:
        candidates = [
            _candidate(
                identity="candidate-b",
                text="candidate b text",
                relevance_score=0.19,
            ),
            _candidate(
                identity="candidate-a",
                text="candidate a text",
                relevance_score=0.19,
            ),
        ]
    return evaluate_question_semantic_scoring(
        question_version_id=_QUESTION_ID,
        question_embedding_text=question_text,
        confidence_class=confidence_class,
        candidate_limit=candidate_limit,
        candidates=candidates,
        embedding_provider_name=_PROVIDER,
        embedding_model_name=_MODEL,
        embedding_model_version=_VERSION,
        embedding_dimensions=_DIMENSIONS,
        repository=repository or FakeEmbeddingCacheRepository(),
        provider=provider or FakeEmbeddingProvider(),
    )


class TestSemanticScoringEntryRules(unittest.TestCase):
    def test_v1_sufficient_makes_zero_embedding_calls(self):
        provider = FakeEmbeddingProvider()
        repository = FakeEmbeddingCacheRepository()

        result = _evaluate(
            confidence_class=CONFIDENCE_CLASS_V1_SUFFICIENT,
            provider=provider,
            repository=repository,
        )

        self.assertEqual(result["status"], STATUS_SKIPPED_V1_SUFFICIENT)
        self.assertEqual(result["evaluated_candidate_count"], 0)
        self.assertEqual(provider.calls, [])
        self.assertEqual(repository.lookup_calls, 0)
        self.assertEqual(repository.insert_calls, 0)

    def test_no_structural_candidate_makes_zero_embedding_calls(self):
        provider = FakeEmbeddingProvider()
        repository = FakeEmbeddingCacheRepository()

        result = _evaluate(
            confidence_class=CONFIDENCE_CLASS_NO_STRUCTURAL,
            provider=provider,
            repository=repository,
        )

        self.assertEqual(result["status"], STATUS_SKIPPED_NO_STRUCTURAL)
        self.assertEqual(result["evaluated_candidate_count"], 0)
        self.assertEqual(provider.calls, [])
        self.assertEqual(repository.lookup_calls, 0)


class TestSemanticScoringCandidateSelection(unittest.TestCase):
    def test_semantic_review_embeds_query_once(self):
        provider = FakeEmbeddingProvider()
        candidates = [
            _candidate(identity="c1", text="t1", relevance_score=0.3),
            _candidate(identity="c2", text="t2", relevance_score=0.2),
        ]

        result = _evaluate(
            candidate_limit=2,
            candidates=candidates,
            provider=provider,
        )

        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(len(provider.calls), 3)
        self.assertFalse(result["query_embedding_cache_hit"])

    def test_only_l1_passing_candidates_are_eligible(self):
        candidates = [
            _candidate(identity="eligible", text="eligible text", relevance_score=0.5, l1_pass=True),
            _candidate(identity="blocked", text="blocked text", relevance_score=0.9, l1_pass=False),
        ]
        provider = FakeEmbeddingProvider()

        result = _evaluate(candidate_limit=5, candidates=candidates, provider=provider)

        self.assertEqual(result["eligible_candidate_count"], 1)
        self.assertEqual(result["evaluated_candidate_count"], 1)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(result["candidates"][0]["candidate_identity"], "eligible")

    def test_candidate_limit_is_enforced(self):
        candidates = [
            _candidate(identity=f"c{i}", text=f"text{i}", relevance_score=0.1 * i)
            for i in range(1, 6)
        ]
        provider = FakeEmbeddingProvider()

        result = _evaluate(candidate_limit=2, candidates=candidates, provider=provider)

        self.assertEqual(result["eligible_candidate_count"], 5)
        self.assertEqual(result["evaluated_candidate_count"], 2)
        self.assertEqual(len(provider.calls), 3)

    def test_candidate_selection_is_deterministic_with_tie_break(self):
        candidates = [
            _candidate(identity="candidate-z", text="z text", relevance_score=0.25),
            _candidate(identity="candidate-a", text="a text", relevance_score=0.25),
            _candidate(identity="candidate-m", text="m text", relevance_score=0.30),
        ]
        provider = FakeEmbeddingProvider()

        result = _evaluate(candidate_limit=2, candidates=candidates, provider=provider)

        self.assertEqual(
            [item["candidate_identity"] for item in result["candidates"]],
            ["candidate-m", "candidate-a"],
        )


class TestSemanticScoringCacheAndSimilarity(unittest.TestCase):
    def test_cache_hits_avoid_provider_calls(self):
        from workers.embedding_cache import build_cache_identity

        question_text = "cached question"
        candidate_text = "cached candidate"
        query_identity = build_cache_identity(
            text=question_text,
            content_scope=CONTENT_SCOPE_QUERY,
            embedding_provider_name=_PROVIDER,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
        )
        chunk_identity = build_cache_identity(
            text=candidate_text,
            content_scope=CONTENT_SCOPE_CHUNK,
            embedding_provider_name=_PROVIDER,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
        )
        repository = FakeEmbeddingCacheRepository(
            [
                EmbeddingCacheRecord(
                    content_scope=query_identity.content_scope,
                    content_hash=query_identity.content_hash,
                    embedding_provider_name=query_identity.embedding_provider_name,
                    embedding_model_name=query_identity.embedding_model_name,
                    embedding_model_version=query_identity.embedding_model_version,
                    embedding_dimensions=query_identity.embedding_dimensions,
                    embedding_vector=_vector(1.0, 0.0, 0.0),
                    provider_response_hash=_RESPONSE_HASH,
                ),
                EmbeddingCacheRecord(
                    content_scope=chunk_identity.content_scope,
                    content_hash=chunk_identity.content_hash,
                    embedding_provider_name=chunk_identity.embedding_provider_name,
                    embedding_model_name=chunk_identity.embedding_model_name,
                    embedding_model_version=chunk_identity.embedding_model_version,
                    embedding_dimensions=chunk_identity.embedding_dimensions,
                    embedding_vector=_vector(1.0, 0.0, 0.0),
                    provider_response_hash=_RESPONSE_HASH,
                ),
            ]
        )
        provider = FakeEmbeddingProvider()
        candidates = [
            _candidate(identity="cached", text=candidate_text, relevance_score=0.2),
        ]

        result = _evaluate(
            candidate_limit=1,
            candidates=candidates,
            provider=provider,
            repository=repository,
            question_text=question_text,
        )

        self.assertEqual(provider.calls, [])
        self.assertTrue(result["query_embedding_cache_hit"])
        self.assertTrue(result["candidates"][0]["embedding_cache_hit"])
        self.assertAlmostEqual(result["candidates"][0]["semantic_similarity"], 1.0, places=9)

    def test_cosine_similarity_is_calculated_correctly(self):
        self.assertAlmostEqual(
            _cosine_similarity(_vector(1.0, 0.0, 0.0), _vector(1.0, 0.0, 0.0)),
            1.0,
            places=9,
        )
        self.assertAlmostEqual(
            _cosine_similarity(_vector(1.0, 0.0, 0.0), _vector(0.0, 1.0, 0.0)),
            0.0,
            places=9,
        )

    def test_result_ordering_is_deterministic(self):
        provider = FakeEmbeddingProvider(
            vectors_by_text={
                "question text": _vector(1.0, 0.0, 0.0),
                "high text": _vector(1.0, 0.0, 0.0),
                "mid text": _vector(0.0, 1.0, 0.0),
                "low text": _vector(-1.0, 0.0, 0.0),
            }
        )
        candidates = [
            _candidate(identity="c-low", text="low text", relevance_score=0.10),
            _candidate(identity="c-high", text="high text", relevance_score=0.30),
            _candidate(identity="c-mid", text="mid text", relevance_score=0.20),
        ]

        result = _evaluate(
            candidate_limit=3,
            candidates=candidates,
            provider=provider,
        )

        self.assertEqual(
            [item["candidate_identity"] for item in result["candidates"]],
            ["c-high", "c-mid", "c-low"],
        )
        self.assertGreater(
            result["candidates"][0]["semantic_similarity"],
            result["candidates"][1]["semantic_similarity"],
        )


class TestSemanticScoringFailureBehavior(unittest.TestCase):
    def test_unequal_dimensions_fail_closed(self):
        with self.assertRaises(SemanticEvaluationError):
            _cosine_similarity(_vector(1.0, 0.0), _vector(1.0, 0.0, 0.0))

    def test_zero_norm_vectors_fail_closed(self):
        with self.assertRaises(SemanticEvaluationError):
            _cosine_similarity(_vector(0.0, 0.0, 0.0), _vector(1.0, 0.0, 0.0))

    def test_nan_values_fail_closed(self):
        with self.assertRaises(SemanticEvaluationError):
            _cosine_similarity(_vector(float("nan"), 0.0, 0.0), _vector(1.0, 0.0, 0.0))

    def test_infinite_values_fail_closed(self):
        with self.assertRaises(SemanticEvaluationError):
            _cosine_similarity(_vector(float("inf"), 0.0, 0.0), _vector(1.0, 0.0, 0.0))

    def test_provider_failure_wraps_as_semantic_evaluation_error(self):
        class BrokenProvider(FakeEmbeddingProvider):
            def embed(self, **kwargs: Any) -> EmbeddingProviderResponse:
                raise RuntimeError("provider exploded")

        with self.assertRaises(SemanticEvaluationError) as ctx:
            _evaluate(provider=BrokenProvider())

        self.assertNotIn(_SENSITIVE_QUESTION_TEXT, str(ctx.exception))
        self.assertNotIn(_API_KEY, str(ctx.exception))

    def test_nonpositive_candidate_limit_rejected(self):
        with self.assertRaises(SemanticEvaluationConfigError):
            _evaluate(candidate_limit=0)


class TestSemanticScoringDeterminismAndPrivacy(unittest.TestCase):
    def test_canonical_json_is_byte_for_byte_deterministic(self):
        provider = FakeEmbeddingProvider()
        candidates = [
            _candidate(identity="c1", text="t1", relevance_score=0.21),
            _candidate(identity="c2", text="t2", relevance_score=0.19),
        ]
        kwargs = {
            "candidate_limit": 2,
            "candidates": candidates,
            "provider": provider,
            "question_text": "deterministic question",
        }

        first = dumps_semantic_scoring_result(_evaluate(**kwargs))
        second = dumps_semantic_scoring_result(_evaluate(**kwargs))

        self.assertEqual(first, second)

    def test_output_excludes_raw_text_vectors_and_credentials(self):
        provider = FakeEmbeddingProvider()
        candidates = [
            _candidate(
                identity="secret-candidate",
                text=_SENSITIVE_CANDIDATE_TEXT,
                relevance_score=0.18,
            )
        ]

        result = _evaluate(
            candidate_limit=1,
            candidates=candidates,
            provider=provider,
            question_text=_SENSITIVE_QUESTION_TEXT,
        )
        serialized = dumps_semantic_scoring_result(result)

        self.assertNotIn(_SENSITIVE_QUESTION_TEXT, serialized)
        self.assertNotIn(_SENSITIVE_CANDIDATE_TEXT, serialized)
        self.assertNotIn(_API_KEY, serialized)
        self.assertNotIn("embedding_vector", serialized)
        self.assertNotIn("qualified_v2", serialized)
        self.assertEqual(result["schema_version"], SEMANTIC_SCORING_SCHEMA_VERSION)
        self.assertEqual(result["proposed_retrieval_method"], PROPOSED_RETRIEVAL_METHOD)
        self.assertEqual(result["stage"], "semantic_scoring")

    def test_logs_and_exceptions_contain_no_sensitive_data(self):
        provider = FakeEmbeddingProvider(
            vectors_by_text={
                _SENSITIVE_QUESTION_TEXT: _vector(float("nan"), 0.0, 0.0),
            }
        )
        logger = logging.getLogger("test.ai_quality_audit_semantic.privacy")
        logger.propagate = True

        with self.assertLogs("test.ai_quality_audit_semantic.privacy", level="INFO") as captured:
            logger.info("semantic_scoring.test.begin")
            with self.assertRaises(SemanticEvaluationError) as ctx:
                _evaluate(
                    provider=provider,
                    question_text=_SENSITIVE_QUESTION_TEXT,
                )
            logger.info("semantic_scoring.test.end")

        combined = "\n".join(captured.output) + str(ctx.exception)
        self.assertNotIn(_SENSITIVE_QUESTION_TEXT, combined)
        self.assertNotIn(_SENSITIVE_CANDIDATE_TEXT, combined)
        self.assertNotIn(_API_KEY, combined)
        self.assertNotIn("0.1", combined)

    def test_no_live_worker_imports_semantic_module(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        workers_dir = os.path.join(repo_root, "workers")
        offenders: List[str] = []
        for name in os.listdir(workers_dir):
            if not name.endswith(".py") or name in {"ai_quality_audit_semantic.py", "__init__.py"}:
                continue
            path = os.path.join(workers_dir, name)
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
            if "ai_quality_audit_semantic" in contents:
                offenders.append(name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
