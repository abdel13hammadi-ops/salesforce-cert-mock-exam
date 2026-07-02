"""
V48 smoke-batch evidence retrieval and deterministic evidence-set freezing.

Uses existing RPCs only:
  - get_question_version_blind_context_v1 (question + certification context)
  - list_audit_candidate_resource_chunks_v1 (certification candidate pool)

Question-specific ranking is performed in Python using precision-first bounded
lexical matching against blind question context. Vector/semantic chunk retrieval
is not available yet (embedding_generation is stubbed; resource_chunks have no
embedding columns).

Qualification rule (conservative, precision-first):
  A candidate must reach MIN_RELEVANCE_SCORE and satisfy at least one alignment
  signal (domain metadata, title overlap, question/option content overlap,
  metadata/feature overlap, or strong content similarity). Weak candidates are
  excluded even when top-K has unused capacity.

Freezing itself happens through create_or_get_ai_quality_audit_run_v1.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from workers.ai_quality_audit_context import load_blind_audit_context

LIST_CANDIDATE_CHUNKS_RPC = "list_audit_candidate_resource_chunks_v1"

RETRIEVAL_METHOD = "lexical_question_match_v2"
CANDIDATE_POOL_MAX = 200
DEFAULT_MAX_EVIDENCE_CHUNKS = 8
DEFAULT_MAX_EVIDENCE_CHARACTERS = 16_000
DEFAULT_MAX_CHUNKS_PER_RESOURCE = 2
MIN_RELEVANCE_SCORE = 0.20
GENERIC_EXAM_GUIDE_MIN_SCORE = 0.30
ESTIMATED_CHARS_PER_TOKEN = 4

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "also",
        "and",
        "any",
        "are",
        "best",
        "can",
        "choose",
        "could",
        "during",
        "following",
        "for",
        "from",
        "has",
        "have",
        "how",
        "into",
        "most",
        "not",
        "one",
        "select",
        "should",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "two",
        "use",
        "used",
        "using",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_MATCH_REASON_DOMAIN = "exact domain match"
_MATCH_REASON_TITLE = "title match"
_MATCH_REASON_QUESTION = "question-text overlap"
_MATCH_REASON_OPTION = "option overlap"
_MATCH_REASON_METADATA = "metadata/feature match"
_MATCH_REASON_STRONG = "strong content similarity"

_EMPTY_EVIDENCE_CANONICAL_JSON = "[]"


class AiQualityAuditEvidenceError(RuntimeError):
    """Raised when evidence retrieval or freezing preparation fails."""


@dataclass(frozen=True)
class EvidenceScoreBreakdown:
    relevance_score: float
    token_score: float
    similarity_score: float
    domain_boost: float
    feature_boost: float
    content_overlap_count: int
    content_overlap_tokens: Tuple[str, ...]
    match_reasons: Tuple[str, ...]
    qualifies: bool
    applicable_threshold: float
    rejection_reason: str


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
    rejected_previews: List[Dict[str, Any]]
    candidate_count: int
    qualified_candidate_count: int
    selected_count: int
    rejected_below_threshold_count: int

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
            "candidate_count": self.candidate_count,
            "qualified_candidate_count": self.qualified_candidate_count,
            "selected_count": self.selected_count,
            "rejected_below_threshold_count": self.rejected_below_threshold_count,
            "selected_chunk_ids": [
                item["resource_chunk_id"] for item in self.chunk_previews
            ],
            "source_titles": [item["title"] for item in self.chunk_previews],
            "chunk_previews": list(self.chunk_previews),
            "rejected_previews": list(self.rejected_previews),
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
    max_chunks_per_resource: int = DEFAULT_MAX_CHUNKS_PER_RESOURCE,
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
    if max_chunks_per_resource < 1:
        raise AiQualityAuditEvidenceError(
            f"max_chunks_per_resource must be positive, got {max_chunks_per_resource}"
        )

    blind_context = load_blind_audit_context(client, qvid)
    certification = str(blind_context["certification_exam_name"]).strip()
    resources = _load_active_resources(client, certification_exam_name=certification)
    resource_ids = [item["id"] for item in resources]
    resource_by_id = {item["id"]: item for item in resources}

    candidate_count = 0
    rejected_previews: List[Dict[str, Any]] = []
    if not resource_ids:
        if allow_no_evidence:
            ranked: List[Dict[str, Any]] = []
            previews: List[Dict[str, Any]] = []
            qualified_count = 0
            rejected_count = 0
            rejected_previews = []
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
        candidate_count = len(candidates)
        (
            ranked,
            previews,
            qualified_count,
            rejected_count,
            rejected_previews,
        ) = rank_question_evidence_candidates(
            candidates,
            blind_context=blind_context,
            resource_by_id=resource_by_id,
            max_chunks=max_chunks,
            max_characters=max_characters,
            max_chunks_per_resource=max_chunks_per_resource,
        )

    if not ranked and not allow_no_evidence:
        raise AiQualityAuditEvidenceError(
            f"evidence retrieval returned zero qualified chunks for question_version {qvid!r} "
            f"(certification={certification!r}, candidates={candidate_count}, "
            f"qualified={qualified_count}, rejected={rejected_count}); "
            "refusing to enqueue weak or empty evidence"
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
        rejected_previews=rejected_previews,
        candidate_count=candidate_count,
        qualified_candidate_count=qualified_count,
        selected_count=len(ranked),
        rejected_below_threshold_count=rejected_count,
    )


def rank_question_evidence_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    blind_context: Mapping[str, Any],
    resource_by_id: Mapping[str, Mapping[str, Any]],
    max_chunks: int,
    max_characters: int,
    max_chunks_per_resource: int = DEFAULT_MAX_CHUNKS_PER_RESOURCE,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int, int, List[Dict[str, Any]]]:
    """Rank certification candidate chunks for one question and apply bounds."""
    query_text = _build_question_query(blind_context)
    question_text = str(blind_context.get("question_text") or "").strip()
    question_tokens = _content_tokens(question_text)
    option_tokens = _option_content_tokens(blind_context)
    query_tokens = _content_tokens(query_text)
    question_domain = str(blind_context.get("domain_name") or "").strip()

    scored: List[Dict[str, Any]] = []
    for candidate in candidates:
        resource_id = str(candidate["resource_id"])
        resource_row = resource_by_id.get(resource_id) or {}
        breakdown = score_evidence_candidate(
            candidate,
            query_text=query_text,
            query_tokens=query_tokens,
            question_tokens=question_tokens,
            option_tokens=option_tokens,
            question_domain=question_domain,
            resource_metadata=resource_row.get("metadata") or {},
            resource_title=str(resource_row.get("title") or candidate.get("title") or ""),
            resource_type=str(
                resource_row.get("resource_type")
                or candidate.get("resource_type")
                or ""
            ),
        )
        scored.append(
            {
                **dict(candidate),
                "relevance_score": round(breakdown.relevance_score, 6),
                "match_reasons": list(breakdown.match_reasons),
                "qualifies": breakdown.qualifies,
                "applicable_threshold": breakdown.applicable_threshold,
                "rejection_reason": breakdown.rejection_reason,
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

    qualified = [item for item in scored if item["qualifies"]]
    rejected_count = len(scored) - len(qualified)

    diversified = _apply_per_resource_cap(qualified, max_chunks_per_resource=max_chunks_per_resource)
    top_ranked = diversified[:max_chunks]
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
                "match_reasons": list(item.get("match_reasons") or []),
            }
        )
        previews.append(
            {
                "resource_chunk_id": str(item["resource_chunk_id"]).lower(),
                "retrieval_rank": rank,
                "title": str(item.get("title") or ""),
                "relevance_score": item["relevance_score"],
                "match_reasons": list(item.get("match_reasons") or []),
            }
        )

    rejected_previews: List[Dict[str, Any]] = []
    for rank, item in enumerate(scored, start=1):
        if item["qualifies"]:
            continue
        rejected_previews.append(
            {
                "retrieval_rank": rank,
                "resource_chunk_id": str(item["resource_chunk_id"]).lower(),
                "relevance_score": item["relevance_score"],
                "applicable_threshold": item["applicable_threshold"],
                "title": str(item.get("title") or ""),
                "match_reasons": list(item.get("match_reasons") or []),
                "rejection_reason": str(item.get("rejection_reason") or ""),
            }
        )
        if len(rejected_previews) >= 3:
            break

    return ranked, previews, len(qualified), rejected_count, rejected_previews


def score_evidence_candidate(
    candidate: Mapping[str, Any],
    *,
    query_text: str,
    query_tokens: Sequence[str],
    question_tokens: Sequence[str],
    option_tokens: Sequence[str],
    question_domain: str,
    resource_metadata: Mapping[str, Any],
    resource_title: str,
    resource_type: str = "",
) -> EvidenceScoreBreakdown:
    """Lexical relevance score with precision-first qualification."""
    chunk_text = str(candidate.get("chunk_text") or "")
    title = str(candidate.get("title") or resource_title or "")
    combined = f"{title} {chunk_text}".strip()
    min_score = _applicable_min_score(resource_type)
    if not combined:
        return EvidenceScoreBreakdown(
            relevance_score=0.0,
            token_score=0.0,
            similarity_score=0.0,
            domain_boost=0.0,
            feature_boost=0.0,
            content_overlap_count=0,
            content_overlap_tokens=(),
            match_reasons=(),
            qualifies=False,
            applicable_threshold=min_score,
            rejection_reason="empty chunk text and title",
        )

    chunk_tokens = _content_tokens(combined)
    title_tokens = _content_tokens(title)
    overlap_tokens = tuple(sorted(set(query_tokens) & set(chunk_tokens)))
    question_overlap = set(question_tokens) & set(chunk_tokens)
    option_overlap = set(option_tokens) & (set(chunk_tokens) | set(title_tokens))

    token_score = _token_jaccard(query_tokens, chunk_tokens)
    content_query = " ".join(query_tokens)
    content_combined = " ".join(chunk_tokens)
    similarity_score = difflib.SequenceMatcher(
        None,
        content_query,
        content_combined,
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

    raw = (0.35 * token_score) + (0.35 * similarity_score) + domain_boost + feature_boost
    if str(resource_type).strip().lower() == "exam_guide":
        raw -= 0.05
    relevance_score = max(0.0, min(1.0, raw))

    match_reasons: List[str] = []
    if domain_boost >= 0.20:
        match_reasons.append(_MATCH_REASON_DOMAIN)
    if len(set(title_tokens) & set(query_tokens)) >= 2 or _option_in_title(option_tokens, title):
        match_reasons.append(_MATCH_REASON_TITLE)
    if len(question_overlap) >= 2:
        match_reasons.append(_MATCH_REASON_QUESTION)
    if option_overlap:
        match_reasons.append(_MATCH_REASON_OPTION)
    if feature_boost >= 0.04:
        match_reasons.append(_MATCH_REASON_METADATA)
    if similarity_score >= 0.40 and overlap_tokens:
        match_reasons.append(_MATCH_REASON_STRONG)

    qualifies, rejection_reason = _candidate_qualifies(
        relevance_score=relevance_score,
        match_reasons=match_reasons,
        overlap_count=len(overlap_tokens),
        resource_type=resource_type,
    )

    return EvidenceScoreBreakdown(
        relevance_score=relevance_score,
        token_score=token_score,
        similarity_score=similarity_score,
        domain_boost=domain_boost,
        feature_boost=feature_boost,
        content_overlap_count=len(overlap_tokens),
        content_overlap_tokens=overlap_tokens,
        match_reasons=tuple(match_reasons),
        qualifies=qualifies,
        applicable_threshold=min_score,
        rejection_reason=rejection_reason,
    )


def _applicable_min_score(resource_type: str) -> float:
    if str(resource_type).strip().lower() == "exam_guide":
        return GENERIC_EXAM_GUIDE_MIN_SCORE
    return MIN_RELEVANCE_SCORE


def _candidate_qualifies(
    *,
    relevance_score: float,
    match_reasons: Sequence[str],
    overlap_count: int,
    resource_type: str,
) -> Tuple[bool, str]:
    if not match_reasons:
        return False, (
            "no alignment signal (requires domain, title, question-text, option, "
            "metadata/feature, or strong content similarity match)"
        )
    if overlap_count == 0 and _MATCH_REASON_DOMAIN not in match_reasons:
        return False, "zero query-token overlap without exact domain match"
    min_score = _applicable_min_score(resource_type)
    if relevance_score < min_score:
        return (
            False,
            f"relevance score {relevance_score:.6f} below threshold {min_score:.2f}",
        )
    return True, ""


def _apply_per_resource_cap(
    qualified_candidates: Sequence[Mapping[str, Any]],
    *,
    max_chunks_per_resource: int,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    per_resource: Dict[str, int] = {}
    for candidate in qualified_candidates:
        resource_id = str(candidate["resource_id"])
        count = per_resource.get(resource_id, 0)
        if count >= max_chunks_per_resource:
            continue
        selected.append(dict(candidate))
        per_resource[resource_id] = count + 1
    return selected


def _load_active_resources(client, *, certification_exam_name: str) -> List[Dict[str, Any]]:
    try:
        result = (
            client.table("official_resources")
            .select("id, certification_exam_name, title, metadata, resource_type, is_active")
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
                "resource_type": str(row.get("resource_type") or "").strip(),
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
                "resource_type": str(row.get("resource_type") or ""),
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
    """Build the base lexical query from stem and domain only.

    Answer options are excluded here because they are scored separately via
    ``_option_content_tokens`` and option-overlap match reasons. Including full
    option strings inflates the query and dilutes Jaccard/similarity scoring.
    """
    parts = [
        str(blind_context.get("question_text") or "").strip(),
        str(blind_context.get("domain_name") or "").strip(),
    ]
    return " ".join(part for part in parts if part)


def _option_content_tokens(blind_context: Mapping[str, Any]) -> tuple[str, ...]:
    tokens: set[str] = set()
    for option in blind_context.get("options") or []:
        if isinstance(option, dict):
            tokens.update(_content_tokens(str(option.get("option_text") or "")))
    return tuple(sorted(tokens))


def _option_in_title(option_tokens: Sequence[str], title: str) -> bool:
    norm_title = _normalize_text(title)
    for token in option_tokens:
        if len(token) >= 4 and token in norm_title:
            return True
    return False


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().strip())


def _content_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            token
            for token in _TOKEN_RE.findall(_normalize_text(value))
            if token not in _STOPWORDS
        )
    )


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
    for key in ("domain", "domains", "exam_domain", "category", "feature", "features", "topic"):
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
    metadata_values = _metadata_text_values(resource_metadata)
    metadata_tokens = set()
    for value in metadata_values:
        metadata_tokens.update(_content_tokens(value))
    overlap = len(set(query_tokens) & metadata_tokens)
    if overlap >= 2:
        return 0.10
    if overlap == 1:
        return 0.04
    title_overlap = len(set(query_tokens) & set(_content_tokens(title)))
    if title_overlap >= 2:
        return 0.06
    body_overlap = len(set(query_tokens) & set(_content_tokens(chunk_text)))
    if body_overlap >= 3:
        return 0.05
    return 0.0


def _validate_uuid(value: object, field_name: str) -> str:
    if value is None:
        raise AiQualityAuditEvidenceError(f"{field_name} is required")
    text = str(value).strip().lower()
    if not _UUID_RE.match(text):
        raise AiQualityAuditEvidenceError(
            f"{field_name} must be a UUID string, got {value!r}"
        )
    return text
