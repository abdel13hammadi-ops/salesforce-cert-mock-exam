"""Offline end-to-end replay harness for hybrid_question_match_v2.

Chains Stage 1 shadow classification with Stage 2 bounded semantic scoring
using injected embedding text, provider, and cache dependencies. Does not
implement qualification, call real providers, or touch live workers.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8 fallback
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

from workers.ai_quality_audit_evidence import RETRIEVAL_METHOD
from workers.ai_quality_audit_semantic import (
    SemanticEvaluationConfigError,
    SemanticEvaluationError,
    evaluate_question_semantic_scoring,
)
from workers.ai_quality_audit_shadow import (
    CONFIDENCE_CLASS_NO_STRUCTURAL,
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    CONFIDENCE_CLASS_V1_SUFFICIENT,
    PROPOSED_RETRIEVAL_METHOD,
    classify_question_shadow_from_replay_record,
)
from workers.embedding_cache import EmbeddingCacheRepository, EmbeddingProvider
from workers.resource_chunking import sha256_hex

HYBRID_REPLAY_SCHEMA_VERSION = "hybrid_replay_v1"


class HybridReplayError(RuntimeError):
    """Base error for offline hybrid replay orchestration."""


class HybridReplayConfigError(HybridReplayError):
    """Raised when replay inputs or configuration are invalid."""


class HybridReplayEmbeddingTextError(HybridReplayError):
    """Raised when required embedding text cannot be resolved."""


class HybridReplayStage1Error(HybridReplayError):
    """Raised when Stage 1 shadow classification fails closed."""


class HybridReplayStage2Error(HybridReplayError):
    """Raised when Stage 2 semantic scoring fails closed."""


class HybridReplayCandidateIdentityError(HybridReplayError):
    """Raised when replay candidate identity construction is invalid or collides."""


@runtime_checkable
class HybridReplayEmbeddingTextResolver(Protocol):
    """Provides synthetic or offline embedding text keyed by stable identities."""

    def resolve_question_embedding_text(self, question_version_id: str) -> Optional[str]:
        """Return embedding text for one question, or None when unavailable."""

    def resolve_candidate_embedding_text(self, candidate_identity: str) -> Optional[str]:
        """Return embedding text for one candidate identity, or None when unavailable."""


def build_replay_candidate_identity(
    *,
    question_version_id: str,
    candidate_position: int,
    title: str,
    resource_type: str,
) -> str:
    """Build a question-scoped opaque candidate identity from deterministic replay fields."""
    if not str(question_version_id).strip():
        raise HybridReplayCandidateIdentityError("question_version_id must be nonempty")
    if not isinstance(candidate_position, int) or isinstance(candidate_position, bool):
        raise HybridReplayCandidateIdentityError("candidate_position must be a nonnegative integer")
    if candidate_position < 0:
        raise HybridReplayCandidateIdentityError("candidate_position must be a nonnegative integer")

    canonical_payload = json.dumps(
        {
            "candidate_position": int(candidate_position),
            "question_version_id": str(question_version_id),
            "resource_type": str(resource_type),
            "title": str(title),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256_hex(canonical_payload)


def run_hybrid_replay_from_records(
    *,
    replay_records: Sequence[Mapping[str, Any]],
    candidate_limit: int,
    embedding_text_resolver: HybridReplayEmbeddingTextResolver,
    embedding_provider_name: str,
    embedding_model_name: str,
    embedding_model_version: str,
    embedding_dimensions: int,
    repository: EmbeddingCacheRepository,
    provider: EmbeddingProvider,
) -> dict[str, Any]:
    """Run Stage 1 and Stage 2 for each replay record and return one combined result."""
    _validate_replay_config(replay_records=replay_records, candidate_limit=candidate_limit)

    question_results: list[dict[str, Any]] = []
    stage1_classification_counts: dict[str, int] = {
        CONFIDENCE_CLASS_V1_SUFFICIENT: 0,
        CONFIDENCE_CLASS_SEMANTIC_REVIEW: 0,
        CONFIDENCE_CLASS_NO_STRUCTURAL: 0,
    }
    semantic_status_counts: dict[str, int] = {}

    for replay_record in _ordered_replay_records(replay_records):
        question_version_id = str(replay_record["question_version_id"])
        try:
            shadow_classification = classify_question_shadow_from_replay_record(
                replay_record
            )
        except Exception as exc:
            raise HybridReplayStage1Error(
                f"Stage 1 shadow classification failed for question {question_version_id!r}"
            ) from exc

        confidence_class = str(shadow_classification["confidence_class"])
        stage1_classification_counts[confidence_class] = (
            stage1_classification_counts.get(confidence_class, 0) + 1
        )

        require_embedding_text = confidence_class == CONFIDENCE_CLASS_SEMANTIC_REVIEW
        semantic_candidates = _build_semantic_candidates_from_shadow(
            shadow_classification,
            question_version_id=question_version_id,
            require_l1_embedding_text=require_embedding_text,
            embedding_text_resolver=embedding_text_resolver,
        )

        if require_embedding_text:
            question_embedding_text = embedding_text_resolver.resolve_question_embedding_text(
                question_version_id
            )
            if question_embedding_text is None or not str(question_embedding_text).strip():
                raise HybridReplayEmbeddingTextError(
                    f"missing question embedding text for semantic-review question "
                    f"{question_version_id!r}"
                )
        else:
            question_embedding_text = ""

        try:
            semantic_result = evaluate_question_semantic_scoring(
                question_version_id=question_version_id,
                question_embedding_text=str(question_embedding_text),
                confidence_class=confidence_class,
                candidate_limit=candidate_limit,
                candidates=semantic_candidates,
                embedding_provider_name=embedding_provider_name,
                embedding_model_name=embedding_model_name,
                embedding_model_version=embedding_model_version,
                embedding_dimensions=embedding_dimensions,
                repository=repository,
                provider=provider,
            )
        except SemanticEvaluationConfigError as exc:
            raise HybridReplayStage2Error(
                f"Stage 2 semantic scoring configuration failed for question "
                f"{question_version_id!r}"
            ) from exc
        except SemanticEvaluationError as exc:
            raise HybridReplayStage2Error(
                f"Stage 2 semantic scoring failed for question {question_version_id!r}"
            ) from exc

        semantic_status = str(semantic_result["status"])
        semantic_status_counts[semantic_status] = (
            semantic_status_counts.get(semantic_status, 0) + 1
        )

        question_results.append(
            {
                "question_version_id": question_version_id,
                "confidence_class": confidence_class,
                "qualified_count_v1": int(shadow_classification["qualified_count_v1"]),
                "structural_candidate_count": int(
                    shadow_classification["structural_candidate_count"]
                ),
                "semantic_result": semantic_result,
            }
        )

    return {
        "schema_version": HYBRID_REPLAY_SCHEMA_VERSION,
        "baseline_retrieval_method": RETRIEVAL_METHOD,
        "proposed_retrieval_method": PROPOSED_RETRIEVAL_METHOD,
        "candidate_limit": candidate_limit,
        "question_count": len(question_results),
        "stage1_classification_counts": stage1_classification_counts,
        "semantic_status_counts": semantic_status_counts,
        "questions": question_results,
    }


def dumps_hybrid_replay_result(result: Mapping[str, Any]) -> str:
    """Serialize one hybrid replay result deterministically for replay/compare."""
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_replay_config(
    *,
    replay_records: Sequence[Mapping[str, Any]],
    candidate_limit: int,
) -> None:
    if not replay_records:
        raise HybridReplayConfigError("replay_records must not be empty")

    if not isinstance(candidate_limit, int) or isinstance(candidate_limit, bool):
        raise HybridReplayConfigError("candidate_limit must be a positive integer")
    if candidate_limit <= 0:
        raise HybridReplayConfigError("candidate_limit must be a positive integer")

    seen_question_ids: set[str] = set()
    for replay_record in replay_records:
        question_version_id = str(replay_record.get("question_version_id") or "").strip()
        if not question_version_id:
            raise HybridReplayConfigError("each replay record must include question_version_id")
        if question_version_id in seen_question_ids:
            raise HybridReplayConfigError(
                f"duplicate question_version_id in replay records: {question_version_id!r}"
            )
        seen_question_ids.add(question_version_id)


def _ordered_replay_records(
    replay_records: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return sorted(
        replay_records,
        key=lambda record: str(record["question_version_id"]),
    )


def _build_semantic_candidates_from_shadow(
    shadow_classification: Mapping[str, Any],
    *,
    question_version_id: str,
    require_l1_embedding_text: bool,
    embedding_text_resolver: HybridReplayEmbeddingTextResolver,
) -> list[dict[str, Any]]:
    shadow_candidates = list(shadow_classification["candidates"])
    semantic_candidates: list[dict[str, Any]] = []
    seen_identities: set[str] = set()

    for candidate_position, candidate in enumerate(shadow_candidates):
        if candidate_position != len(semantic_candidates):
            raise HybridReplayCandidateIdentityError(
                "Stage 1 candidate positions must be deterministic and contiguous"
            )

        candidate_identity = build_replay_candidate_identity(
            question_version_id=question_version_id,
            candidate_position=candidate_position,
            title=str(candidate["title"]),
            resource_type=str(candidate.get("resource_type") or ""),
        )
        if candidate_identity in seen_identities:
            raise HybridReplayCandidateIdentityError(
                f"duplicate replay candidate identity at position {candidate_position} "
                f"for question {question_version_id!r}"
            )
        seen_identities.add(candidate_identity)

        if require_l1_embedding_text and bool(candidate["l1_structural_guards_pass"]):
            candidate_embedding_text = embedding_text_resolver.resolve_candidate_embedding_text(
                candidate_identity
            )
            if (
                candidate_embedding_text is None
                or not str(candidate_embedding_text).strip()
            ):
                raise HybridReplayEmbeddingTextError(
                    "missing candidate embedding text for semantic-review candidate "
                    f"{candidate_identity!r}"
                )
            embedding_text = str(candidate_embedding_text)
        else:
            embedding_text = ""

        semantic_candidates.append(
            {
                "candidate_identity": candidate_identity,
                "candidate_embedding_text": embedding_text,
                "relevance_score": float(candidate["relevance_score"]),
                "l1_structural_guards_pass": bool(candidate["l1_structural_guards_pass"]),
                "qualified_v1": bool(candidate["qualified_v1"]),
                "resource_type": str(candidate.get("resource_type") or ""),
            }
        )
    return semantic_candidates
