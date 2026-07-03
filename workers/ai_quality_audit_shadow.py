"""Offline shadow classification for hybrid_question_match_v2 (Stage 1 only).

Consumes frozen or live V1 candidate-analysis replay records and produces a
deterministic question-level confidence classification plus per-candidate L1/L2
decision payloads. Does not alter V1 scoring, ranking, or qualification.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from workers.ai_quality_audit_evidence import (
    RETRIEVAL_METHOD,
    _effective_min_discriminative_overlap_idf,
    replay_bm25_candidate_from_record,
)

SHADOW_CLASSIFICATION_SCHEMA_VERSION = "shadow_classification_v1"
PROPOSED_RETRIEVAL_METHOD = "hybrid_question_match_v2"

CONFIDENCE_CLASS_V1_SUFFICIENT = "v1_sufficient"
CONFIDENCE_CLASS_SEMANTIC_REVIEW = "semantic_review_candidate"
CONFIDENCE_CLASS_NO_STRUCTURAL = "no_structural_candidate"


def _l1_structural_guards_pass(
    *,
    match_reasons: Sequence[str],
    overlap_count: int,
    discriminative_overlap_idf: float,
    document_count: int,
) -> bool:
    if not match_reasons:
        return False
    if overlap_count <= 0:
        return False
    min_discriminative_idf = _effective_min_discriminative_overlap_idf(document_count)
    return discriminative_overlap_idf >= min_discriminative_idf


def _l2_relevance_gate_pass(*, relevance_score: float, applicable_threshold: float) -> bool:
    return relevance_score >= applicable_threshold


def _discriminative_overlap_idf(
    candidate_record: Mapping[str, Any],
    query_token_idf: Mapping[str, float],
) -> float:
    overlap_tokens = list(candidate_record.get("query_content_overlap_tokens") or [])
    return sum(float(query_token_idf.get(term, 0.0)) for term in overlap_tokens)


def _classify_confidence(
    *,
    qualified_count_v1: int,
    structural_candidate_count: int,
) -> str:
    if qualified_count_v1 > 0:
        return CONFIDENCE_CLASS_V1_SUFFICIENT
    if structural_candidate_count > 0:
        return CONFIDENCE_CLASS_SEMANTIC_REVIEW
    return CONFIDENCE_CLASS_NO_STRUCTURAL


def classify_question_shadow_from_replay_record(
    question_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify one question's V1 replay export into a shadow decision payload."""
    question_version_id = str(question_record["question_version_id"])
    document_count = int(question_record["corpus_document_count"])
    query_token_idf = question_record["query_token_idf"]
    candidate_records = list(question_record["candidates"])

    candidates: list[dict[str, Any]] = []
    qualified_count_v1 = 0
    structural_candidate_count = 0

    for candidate_record in candidate_records:
        replay = replay_bm25_candidate_from_record(question_record, candidate_record)
        overlap_count = len(candidate_record.get("query_content_overlap_tokens") or [])
        discriminative_overlap_idf = _discriminative_overlap_idf(
            candidate_record, query_token_idf
        )
        l1_pass = _l1_structural_guards_pass(
            match_reasons=replay["match_reasons"],
            overlap_count=overlap_count,
            discriminative_overlap_idf=discriminative_overlap_idf,
            document_count=document_count,
        )
        l2_pass = _l2_relevance_gate_pass(
            relevance_score=float(replay["relevance_score"]),
            applicable_threshold=float(replay["applicable_threshold"]),
        )
        qualified_v1 = bool(replay["qualified"])

        if qualified_v1:
            qualified_count_v1 += 1
        if l1_pass:
            structural_candidate_count += 1

        candidates.append(
            {
                "title": str(candidate_record["title"]),
                "resource_type": str(candidate_record.get("resource_type") or ""),
                "relevance_score": replay["relevance_score"],
                "applicable_threshold": replay["applicable_threshold"],
                "l1_structural_guards_pass": l1_pass,
                "l2_relevance_gate_pass": l2_pass,
                "qualified_v1": qualified_v1,
                "rejection_reason": str(replay["rejection_reason"]),
                "match_reasons": list(replay["match_reasons"]),
            }
        )

    confidence_class = _classify_confidence(
        qualified_count_v1=qualified_count_v1,
        structural_candidate_count=structural_candidate_count,
    )

    return {
        "schema_version": SHADOW_CLASSIFICATION_SCHEMA_VERSION,
        "question_version_id": question_version_id,
        "baseline_retrieval_method": RETRIEVAL_METHOD,
        "proposed_retrieval_method": PROPOSED_RETRIEVAL_METHOD,
        "confidence_class": confidence_class,
        "candidate_count": len(candidates),
        "qualified_count_v1": qualified_count_v1,
        "structural_candidate_count": structural_candidate_count,
        "candidates": candidates,
    }


def dumps_shadow_classification(classification: Mapping[str, Any]) -> str:
    """Serialize one shadow classification deterministically for replay/compare."""
    return json.dumps(classification, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
