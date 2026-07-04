"""Offline Stage 2 semantic scoring for hybrid_question_match_v2.

Integrates Stage 1 confidence classification with the embedding cache and
provider protocols to compute deterministic cosine-similarity scores for
bounded semantic-review candidates. Does not implement qualification,
thresholds, or live worker wiring.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping, Optional, Sequence, Tuple

from workers.ai_quality_audit_shadow import (
    CONFIDENCE_CLASS_NO_STRUCTURAL,
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    CONFIDENCE_CLASS_V1_SUFFICIENT,
    PROPOSED_RETRIEVAL_METHOD,
)
from workers.embedding_cache import (
    CONTENT_SCOPE_CHUNK,
    CONTENT_SCOPE_QUERY,
    EmbeddingCacheRepository,
    EmbeddingProvider,
    EmbeddingCacheError,
    get_or_compute_embedding,
)

SEMANTIC_SCORING_SCHEMA_VERSION = "semantic_scoring_v1"
STAGE_SEMANTIC_SCORING = "semantic_scoring"

STATUS_COMPLETED = "completed"
STATUS_SKIPPED_V1_SUFFICIENT = "skipped_v1_sufficient"
STATUS_SKIPPED_NO_STRUCTURAL = "skipped_no_structural_candidate"
STATUS_FAILED = "failed"

_DETERMINISTIC_FLOAT_PLACES = 9


class SemanticEvaluationError(RuntimeError):
    """Raised when Stage 2 semantic scoring fails closed."""


class SemanticEvaluationConfigError(SemanticEvaluationError):
    """Raised when semantic-evaluation inputs are invalid."""


def evaluate_question_semantic_scoring(
    *,
    question_version_id: str,
    question_embedding_text: str,
    confidence_class: str,
    candidate_limit: int,
    candidates: Sequence[Mapping[str, Any]],
    embedding_provider_name: str,
    embedding_model_name: str,
    embedding_model_version: str,
    embedding_dimensions: int,
    repository: EmbeddingCacheRepository,
    provider: EmbeddingProvider,
) -> dict[str, Any]:
    """Evaluate bounded semantic similarity for one question."""
    _validate_evaluation_config(
        question_version_id=question_version_id,
        candidate_limit=candidate_limit,
        embedding_provider_name=embedding_provider_name,
        embedding_model_name=embedding_model_name,
        embedding_model_version=embedding_model_version,
        embedding_dimensions=embedding_dimensions,
    )
    parsed_candidates = [_parse_candidate_record(record) for record in candidates]
    eligible_candidates = [
        candidate
        for candidate in parsed_candidates
        if candidate["l1_structural_guards_pass"]
    ]
    eligible_candidate_count = len(eligible_candidates)

    if confidence_class == CONFIDENCE_CLASS_V1_SUFFICIENT:
        return _build_skipped_result(
            question_version_id=question_version_id,
            status=STATUS_SKIPPED_V1_SUFFICIENT,
            candidate_limit=candidate_limit,
            eligible_candidate_count=eligible_candidate_count,
        )
    if confidence_class == CONFIDENCE_CLASS_NO_STRUCTURAL:
        return _build_skipped_result(
            question_version_id=question_version_id,
            status=STATUS_SKIPPED_NO_STRUCTURAL,
            candidate_limit=candidate_limit,
            eligible_candidate_count=eligible_candidate_count,
        )
    if confidence_class != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
        raise SemanticEvaluationConfigError(
            f"unsupported confidence_class: {confidence_class!r}"
        )

    selected_candidates = _select_candidates_for_evaluation(
        eligible_candidates,
        candidate_limit=candidate_limit,
    )

    try:
        query_embedding = get_or_compute_embedding(
            text=question_embedding_text,
            content_scope=CONTENT_SCOPE_QUERY,
            embedding_provider_name=embedding_provider_name,
            embedding_model_name=embedding_model_name,
            embedding_model_version=embedding_model_version,
            embedding_dimensions=embedding_dimensions,
            repository=repository,
            provider=provider,
        )
        query_vector = query_embedding.record.embedding_vector

        evaluated_candidates: list[dict[str, Any]] = []
        for candidate in selected_candidates:
            candidate_embedding = get_or_compute_embedding(
                text=candidate["candidate_embedding_text"],
                content_scope=CONTENT_SCOPE_CHUNK,
                embedding_provider_name=embedding_provider_name,
                embedding_model_name=embedding_model_name,
                embedding_model_version=embedding_model_version,
                embedding_dimensions=embedding_dimensions,
                repository=repository,
                provider=provider,
            )
            similarity = _cosine_similarity(
                query_vector,
                candidate_embedding.record.embedding_vector,
            )
            evaluated_candidates.append(
                {
                    "candidate_identity": candidate["candidate_identity"],
                    "relevance_score": _round_deterministic(
                        candidate["relevance_score"]
                    ),
                    "qualified_v1": candidate["qualified_v1"],
                    "semantic_similarity": _round_deterministic(similarity),
                    "embedding_cache_hit": candidate_embedding.cache_hit,
                }
            )
    except EmbeddingCacheError as exc:
        raise SemanticEvaluationError(
            "semantic scoring failed during embedding lookup or persistence"
        ) from exc
    except SemanticEvaluationError:
        raise
    except Exception as exc:
        raise SemanticEvaluationError(
            "semantic scoring failed during embedding or similarity evaluation"
        ) from exc

    evaluated_candidates.sort(
        key=lambda item: (
            -item["semantic_similarity"],
            -item["relevance_score"],
            item["candidate_identity"],
        )
    )

    return {
        "schema_version": SEMANTIC_SCORING_SCHEMA_VERSION,
        "question_version_id": str(question_version_id),
        "proposed_retrieval_method": PROPOSED_RETRIEVAL_METHOD,
        "stage": STAGE_SEMANTIC_SCORING,
        "status": STATUS_COMPLETED,
        "candidate_limit": candidate_limit,
        "eligible_candidate_count": eligible_candidate_count,
        "evaluated_candidate_count": len(evaluated_candidates),
        "query_embedding_cache_hit": query_embedding.cache_hit,
        "candidates": evaluated_candidates,
    }


def dumps_semantic_scoring_result(result: Mapping[str, Any]) -> str:
    """Serialize one semantic-scoring result deterministically for replay/compare."""
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_evaluation_config(
    *,
    question_version_id: str,
    candidate_limit: int,
    embedding_provider_name: str,
    embedding_model_name: str,
    embedding_model_version: str,
    embedding_dimensions: int,
) -> None:
    if not str(question_version_id).strip():
        raise SemanticEvaluationConfigError("question_version_id must be nonempty")
    if not isinstance(candidate_limit, int) or isinstance(candidate_limit, bool):
        raise SemanticEvaluationConfigError("candidate_limit must be a positive integer")
    if candidate_limit <= 0:
        raise SemanticEvaluationConfigError("candidate_limit must be a positive integer")
    if embedding_dimensions <= 0:
        raise SemanticEvaluationConfigError("embedding_dimensions must be positive")
    for field_name, value in (
        ("embedding_provider_name", embedding_provider_name),
        ("embedding_model_name", embedding_model_name),
        ("embedding_model_version", embedding_model_version),
    ):
        if not str(value or "").strip():
            raise SemanticEvaluationConfigError(f"{field_name} must be nonempty")


def _parse_candidate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    candidate_identity = str(record["candidate_identity"])
    if not candidate_identity.strip():
        raise SemanticEvaluationConfigError("candidate_identity must be nonempty")
    return {
        "candidate_identity": candidate_identity,
        "candidate_embedding_text": str(record["candidate_embedding_text"]),
        "relevance_score": float(record["relevance_score"]),
        "l1_structural_guards_pass": bool(record["l1_structural_guards_pass"]),
        "qualified_v1": bool(record["qualified_v1"]),
        "resource_type": str(record.get("resource_type") or ""),
    }


def _select_candidates_for_evaluation(
    eligible_candidates: Sequence[Mapping[str, Any]],
    *,
    candidate_limit: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        eligible_candidates,
        key=lambda candidate: (
            -float(candidate["relevance_score"]),
            str(candidate["candidate_identity"]),
        ),
    )
    return list(ordered[:candidate_limit])


def _build_skipped_result(
    *,
    question_version_id: str,
    status: str,
    candidate_limit: int,
    eligible_candidate_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": SEMANTIC_SCORING_SCHEMA_VERSION,
        "question_version_id": str(question_version_id),
        "proposed_retrieval_method": PROPOSED_RETRIEVAL_METHOD,
        "stage": STAGE_SEMANTIC_SCORING,
        "status": status,
        "candidate_limit": candidate_limit,
        "eligible_candidate_count": eligible_candidate_count,
        "evaluated_candidate_count": 0,
        "query_embedding_cache_hit": False,
        "candidates": [],
    }


def _cosine_similarity(
    query_vector: Sequence[float],
    candidate_vector: Sequence[float],
) -> float:
    if len(query_vector) != len(candidate_vector):
        raise SemanticEvaluationError("embedding vectors have unequal dimensions")
    if len(query_vector) == 0:
        raise SemanticEvaluationError("embedding vectors must not be empty")

    dot_product = 0.0
    query_norm_sq = 0.0
    candidate_norm_sq = 0.0
    for index, (query_value, candidate_value) in enumerate(
        zip(query_vector, candidate_vector)
    ):
        query_component = float(query_value)
        candidate_component = float(candidate_value)
        if math.isnan(query_component) or math.isinf(query_component):
            raise SemanticEvaluationError(
                f"query embedding contains non-finite value at index {index}"
            )
        if math.isnan(candidate_component) or math.isinf(candidate_component):
            raise SemanticEvaluationError(
                f"candidate embedding contains non-finite value at index {index}"
            )
        dot_product += query_component * candidate_component
        query_norm_sq += query_component * query_component
        candidate_norm_sq += candidate_component * candidate_component

    if query_norm_sq == 0.0 or candidate_norm_sq == 0.0:
        raise SemanticEvaluationError("embedding vectors must not have zero norm")

    similarity = dot_product / math.sqrt(query_norm_sq * candidate_norm_sq)
    if math.isnan(similarity) or math.isinf(similarity):
        raise SemanticEvaluationError("semantic similarity is non-finite")
    return similarity


def _round_deterministic(value: float) -> float:
    return round(float(value), _DETERMINISTIC_FLOAT_PLACES)
