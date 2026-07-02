"""
V48 smoke-batch evidence retrieval and deterministic evidence-set freezing.

Uses existing RPCs only:
  - get_question_version_blind_context_v1 (question + certification context)
  - list_audit_candidate_resource_chunks_v1 (certification candidate pool)

Question-specific ranking is performed in Python using bounded lexical
matching against blind question context. Vector/semantic chunk retrieval is
not available yet (embedding_generation is stubbed; resource_chunks have no
embedding columns).

Freezing itself happens through create_or_get_ai_quality_audit_run_v1.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence

from workers.ai_quality_audit_context import load_blind_audit_context

LIST_CANDIDATE_CHUNKS_RPC = "list_audit_candidate_resource_chunks_v1"

RETRIEVAL_METHOD = "lexical_question_match_v1"
CANDIDATE_POOL_MAX = 200
DEFAULT_MAX_EVIDENCE_CHUNKS = 12
DEFAULT_MAX_EVIDENCE_CHARACTERS = 32_000
ESTIMATED_CHARS_PER_TOKEN = 4

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

_EMPTY_EVIDENCE_CANONICAL_JSON = "[]"


class AiQualityAuditEvidenceError(RuntimeError):
    """Raised when evidence retrieval or freezing preparation fails."""


@dataclass(frozen=True)
class PreparedEvidenceSet:
    question_version_id: str
    certification_exam_name: str
    evidence_chunks: List[Dict[str, Any]]
    evidence_set_hash: str
    ranked_candidates: List[Dict[str, Any]]
    retrieval_method: str
    total_evidence_characters: int
    estimated_tokens: int
    chunk_previews: List[Dict[str, Any]]

    @property
    def evidence_count(self) -> int:
        return len(self.evidence_chunks)

    def to_summary_dict(self) -> Dict[str, Any]:
        return {
            "question_version_id": self.question_version_id,
            "certification_exam_name": self.certification_exam_name,
            "chunk_count": self.evidence_count,
            "evidence_count": self.evidence_count,
            "evidence_set_hash": self.evidence_set_hash,
            "evidence_chunks": self.evidence_chunks,
            "retrieval_method": self.retrieval_method,
            "total_evidence_characters": self.total_evidence_characters,
            "estimated_tokens": self.estimated_tokens,
            "selected_chunk_ids": [
                item["resource_chunk_id"] for item in self.chunk_previews
            ],
            "source_titles": [item["title"] for item in self.chunk_previews],
            "chunk_previews": list(self.chunk_previews),
        }


def empty_evidence_set_hash() -> str:
    """SHA-256 of the canonical empty evidence array (matches PostgreSQL jsonb::text)."""
    return hashlib.sha256(_EMPTY_EVIDENCE_CANONICAL_JSON.encode("utf-8")).hexdigest()


def compute_evidence_set_hash(
    ranked_chunks: Sequence[Mapping[str, Any]],
) -> str:
    """Compute the create-run evidence hash from ranked chunk rows."""
    entries = sorted(
        (
            {
                "retrieval_rank": int(item["retrieval_rank"]),
                "resource_chunk_id": str(item["resource_chunk_id"]).strip().lower(),
                "content_hash": str(item["content_hash"]).strip(),
            }
            for item in ranked_chunks
        ),
        key=lambda item: item["retrieval_rank"],
    )
    canonical = [
        [
            item["retrieval_rank"],
            item["resource_chunk_id"],
            item["content_hash"],
        ]
        for item in entries
    ]
    text = json.dumps(canonical, separators=(", ", ": "), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_create_run_evidence_payload(
    ranked_chunks: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Return ``p_evidence_chunks`` entries for create_or_get_ai_quality_audit_run_v1."""
    ordered = sorted(ranked_chunks, key=lambda item: int(item["retrieval_rank"]))
    payload: List[Dict[str, Any]] = []
    for item in ordered:
        entry: Dict[str, Any] = {
            "resource_chunk_id": str(item["resource_chunk_id"]).strip().lower(),
            "retrieval_rank": int(item["retrieval_rank"]),
        }
        relevance = item.get("relevance_score")
        if relevance is not None:
            entry["relevance_score"] = float(relevance)
        payload.append(entry)
    return payload


def prepare_smoke_evidence_set(
    client,
    question_version_id: str,
    *,
    max_chunks: int = DEFAULT_MAX_EVIDENCE_CHUNKS,
    max_characters: int = DEFAULT_MAX_EVIDENCE_CHARACTERS,
    allow_no_evidence: bool = False,
) -> PreparedEvidenceSet:
    """Retrieve, question-rank, bound, and hash evidence for one smoke audit run."""
    qvid = _validate_uuid(question_version_id, "question_version_id")
    if max_chunks < 1 or max_chunks > 200:
        raise AiQualityAuditEvidenceError(
            f"max_chunks must be between 1 and 200, got {max_chunks}"
        )
    if max_characters < 1:
        raise AiQualityAuditEvidenceError(
            f"max_characters must be positive, got {max_characters}"
        )

    blind_context = load_blind_audit_context(client, qvid)
    certification = str(blind_context["certification_exam_name"]).strip()
    resources = _load_active_resources(client, certification_exam_name=certification)
    resource_ids = [item["id"] for item in resources]
    resource_by_id = {item["id"]: item for item in resources}

    if not resource_ids:
        if allow_no_evidence:
            ranked: List[Dict[str, Any]] = []
            previews: List[Dict[str, Any]] = []
        else:
            raise AiQualityAuditEvidenceError(
                f"no active official_resources found for certification {certification!r}; "
                f"cannot freeze evidence for question_version {qvid!r}"
            )
    else:
        candidates = _list_candidate_chunks(
            client,
            certification_exam_name=certification,
            resource_ids=resource_ids,
            max_chunks=CANDIDATE_POOL_MAX,
        )
        ranked, previews = rank_question_evidence_candidates(
            candidates,
            blind_context=blind_context,
            resource_by_id=resource_by_id,
            max_chunks=max_chunks,
            max_characters=max_characters,
        )

    if not ranked and not allow_no_evidence:
        raise AiQualityAuditEvidenceError(
            f"evidence retrieval returned zero chunks for question_version {qvid!r} "
            f"(certification={certification!r}); refusing to enqueue an empty evidence set"
        )

    total_chars = sum(len(item.get("chunk_text") or "") for item in ranked)
    evidence_set_hash = compute_evidence_set_hash(ranked)
    evidence_chunks = build_create_run_evidence_payload(ranked)
    return PreparedEvidenceSet(
        question_version_id=qvid,
        certification_exam_name=certification,
        evidence_chunks=evidence_chunks,
        evidence_set_hash=evidence_set_hash,
        ranked_candidates=list(ranked),
        retrieval_method=RETRIEVAL_METHOD,
        total_evidence_characters=total_chars,
        estimated_tokens=max(1, total_chars // ESTIMATED_CHARS_PER_TOKEN)
        if total_chars
        else 0,
        chunk_previews=previews,
    )


def rank_question_evidence_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    blind_context: Mapping[str, Any],
    resource_by_id: Mapping[str, Mapping[str, Any]],
    max_chunks: int,
    max_characters: int,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Rank certification candidate chunks for one question and apply bounds."""
    query_text = _build_question_query(blind_context)
    query_tokens = _tokenize(query_text)
    question_domain = str(blind_context.get("domain_name") or "").strip()

    scored: List[Dict[str, Any]] = []
    for candidate in candidates:
        resource_id = str(candidate["resource_id"])
        resource_row = resource_by_id.get(resource_id) or {}
        score = score_evidence_candidate(
            candidate,
            query_text=query_text,
            query_tokens=query_tokens,
            question_domain=question_domain,
            resource_metadata=resource_row.get("metadata") or {},
            resource_title=str(resource_row.get("title") or candidate.get("title") or ""),
        )
        scored.append(
            {
                **dict(candidate),
                "relevance_score": round(score, 6),
            }
        )

    scored.sort(
        key=lambda item: (
            -float(item["relevance_score"]),
            str(item["resource_id"]),
            int(item["chunk_index"]),
            str(item["resource_chunk_id"]),
        )
    )

    top_ranked = scored[:max_chunks]
    bounded = _apply_character_budget(top_ranked, max_characters=max_characters)

    ranked: List[Dict[str, Any]] = []
    previews: List[Dict[str, Any]] = []
    for rank, item in enumerate(bounded, start=1):
        ranked.append(
            {
                "retrieval_rank": rank,
                "resource_chunk_id": str(item["resource_chunk_id"]).lower(),
                "content_hash": str(item["content_hash"]),
                "relevance_score": item["relevance_score"],
                "chunk_text": str(item.get("chunk_text") or ""),
                "title": str(item.get("title") or ""),
            }
        )
        previews.append(
            {
                "resource_chunk_id": str(item["resource_chunk_id"]).lower(),
                "retrieval_rank": rank,
                "title": str(item.get("title") or ""),
                "relevance_score": item["relevance_score"],
            }
        )
    return ranked, previews


def score_evidence_candidate(
    candidate: Mapping[str, Any],
    *,
    query_text: str,
    query_tokens: tuple[str, ...],
    question_domain: str,
    resource_metadata: Mapping[str, Any],
    resource_title: str,
) -> float:
    """Lexical relevance score in [0, 1] with domain/feature preference boosts."""
    chunk_text = str(candidate.get("chunk_text") or "")
    title = str(candidate.get("title") or resource_title or "")
    combined = f"{title} {chunk_text}".strip()
    if not combined:
        return 0.0

    norm_query = _normalize_text(query_text)
    norm_combined = _normalize_text(combined)
    chunk_tokens = _tokenize(combined)

    token_score = _token_jaccard(query_tokens, chunk_tokens)
    similarity_score = difflib.SequenceMatcher(
        None,
        norm_query,
        norm_combined,
    ).ratio()

    domain_boost = _domain_match_boost(
        question_domain=question_domain,
        resource_metadata=resource_metadata,
        title=title,
        chunk_text=chunk_text,
    )
    feature_boost = _feature_match_boost(
        query_tokens=query_tokens,
        resource_metadata=resource_metadata,
        title=title,
        chunk_text=chunk_text,
    )

    raw = (0.40 * token_score) + (0.40 * similarity_score) + domain_boost + feature_boost
    return max(0.0, min(1.0, raw))


def _load_active_resources(client, *, certification_exam_name: str) -> List[Dict[str, Any]]:
    try:
        result = (
            client.table("official_resources")
            .select("id, certification_exam_name, title, metadata, is_active")
            .eq("certification_exam_name", certification_exam_name)
            .eq("is_active", True)
            .execute()
        )
    except Exception as exc:
        raise AiQualityAuditEvidenceError(
            f"official_resources lookup failed: {exc}"
        ) from exc

    if getattr(result, "error", None):
        raise AiQualityAuditEvidenceError(
            f"official_resources lookup failed: {result.error}"
        )

    rows = result.data or []
    resources: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AiQualityAuditEvidenceError(
                f"official_resources row {index} is malformed"
            )
        resource_id = _validate_uuid(row.get("id"), f"official_resources[{index}].id")
        row_cert = str(row.get("certification_exam_name") or "").strip()
        if row_cert != certification_exam_name:
            raise AiQualityAuditEvidenceError(
                f"official_resources[{index}] certification mismatch: "
                f"{row_cert!r} != {certification_exam_name!r}"
            )
        if resource_id in seen:
            continue
        seen.add(resource_id)
        resources.append(
            {
                "id": resource_id,
                "title": str(row.get("title") or "").strip(),
                "metadata": dict(row.get("metadata") or {}),
            }
        )
    resources.sort(key=lambda item: item["id"])
    return resources


def _list_candidate_chunks(
    client,
    *,
    certification_exam_name: str,
    resource_ids: Sequence[str],
    max_chunks: int,
) -> List[Dict[str, Any]]:
    try:
        result = client.rpc(
            LIST_CANDIDATE_CHUNKS_RPC,
            {
                "p_certification_exam_name": certification_exam_name,
                "p_resource_ids": list(resource_ids),
                "p_max_chunks": max_chunks,
            },
        ).execute()
    except Exception as exc:
        raise AiQualityAuditEvidenceError(
            f"RPC {LIST_CANDIDATE_CHUNKS_RPC!r} call failed: {exc}"
        ) from exc

    if getattr(result, "error", None):
        raise AiQualityAuditEvidenceError(
            f"RPC {LIST_CANDIDATE_CHUNKS_RPC!r} failed: {result.error}"
        )

    rows = result.data or []
    normalized: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AiQualityAuditEvidenceError(
                f"{LIST_CANDIDATE_CHUNKS_RPC} row {index} is malformed"
            )
        chunk_id = _validate_uuid(
            row.get("resource_chunk_id"),
            f"{LIST_CANDIDATE_CHUNKS_RPC}[{index}].resource_chunk_id",
        )
        row_cert = str(row.get("certification_exam_name") or "").strip()
        if row_cert != certification_exam_name:
            raise AiQualityAuditEvidenceError(
                f"{LIST_CANDIDATE_CHUNKS_RPC}[{index}] certification mismatch: "
                f"{row_cert!r} != {certification_exam_name!r}"
            )
        content_hash = str(row.get("content_hash") or "").strip()
        if not content_hash:
            raise AiQualityAuditEvidenceError(
                f"{LIST_CANDIDATE_CHUNKS_RPC}[{index}] missing content_hash"
            )
        normalized.append(
            {
                "resource_chunk_id": chunk_id,
                "resource_id": _validate_uuid(
                    row.get("resource_id"),
                    f"{LIST_CANDIDATE_CHUNKS_RPC}[{index}].resource_id",
                ),
                "content_hash": content_hash,
                "chunk_index": int(row.get("chunk_index") or 0),
                "certification_exam_name": row_cert,
                "chunk_text": str(row.get("chunk_text") or ""),
                "title": str(row.get("title") or ""),
            }
        )
    return normalized


def _apply_character_budget(
    ranked_candidates: Sequence[Mapping[str, Any]],
    *,
    max_characters: int,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    total_chars = 0
    for candidate in ranked_candidates:
        chunk_len = len(str(candidate.get("chunk_text") or ""))
        if selected and total_chars + chunk_len > max_characters:
            continue
        selected.append(dict(candidate))
        total_chars += chunk_len
    return selected


def _build_question_query(blind_context: Mapping[str, Any]) -> str:
    parts = [
        str(blind_context.get("question_text") or "").strip(),
        str(blind_context.get("domain_name") or "").strip(),
    ]
    for option in blind_context.get("options") or []:
        if isinstance(option, dict):
            parts.append(str(option.get("option_text") or "").strip())
    return " ".join(part for part in parts if part)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().strip())


def _tokenize(value: str) -> tuple[str, ...]:
    return tuple(sorted(set(_TOKEN_RE.findall(_normalize_text(value)))))


def _token_jaccard(tokens_a: Sequence[str], tokens_b: Sequence[str]) -> float:
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _metadata_text_values(metadata: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("domain", "domains", "exam_domain", "category", "feature", "features"):
        raw = metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            values.add(_normalize_text(raw))
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    values.add(_normalize_text(item))
    return values


def _domain_match_boost(
    *,
    question_domain: str,
    resource_metadata: Mapping[str, Any],
    title: str,
    chunk_text: str,
) -> float:
    normalized_domain = _normalize_text(question_domain)
    if not normalized_domain:
        return 0.0

    metadata_domains = _metadata_text_values(resource_metadata)
    if normalized_domain in metadata_domains:
        return 0.20
    haystack = _normalize_text(f"{title} {chunk_text}")
    if normalized_domain in haystack:
        return 0.10
    return 0.0


def _feature_match_boost(
    *,
    query_tokens: Sequence[str],
    resource_metadata: Mapping[str, Any],
    title: str,
    chunk_text: str,
) -> float:
    feature_values = _metadata_text_values(resource_metadata)
    features = feature_values | set(_tokenize(f"{title} {chunk_text}"))
    if not query_tokens or not features:
        return 0.0
    overlap = len(set(query_tokens) & features)
    if overlap == 0:
        return 0.0
    return min(0.10, overlap * 0.02)


def _validate_uuid(value: object, field_name: str) -> str:
    if value is None:
        raise AiQualityAuditEvidenceError(f"{field_name} is required")
    text = str(value).strip().lower()
    if not _UUID_RE.match(text):
        raise AiQualityAuditEvidenceError(
            f"{field_name} must be a UUID string, got {value!r}"
        )
    return text
