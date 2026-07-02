"""
V48 smoke-batch evidence retrieval and deterministic evidence-set freezing.

Uses existing RPCs only:
  - get_question_version_blind_context_v1 (question + certification context)
  - list_audit_candidate_resource_chunks_v1 (certification candidate pool)

Question-specific ranking is performed in Python using a small internal BM25
(Okapi-style, per-field-weighted) scorer against the certification's candidate
pool for each question. Corpus statistics (document frequency / IDF, average
field lengths) are computed once per question from that question's own
candidate pool, so generic terms that recur across most candidates are
automatically down-weighted while terms distinctive to a smaller subset of
candidates carry more discriminating power. Vector/semantic chunk retrieval is
not available yet (embedding_generation is stubbed; resource_chunks have no
embedding columns).

BM25 query construction:
  The query is built from the question stem and question domain/category only
  (see ``_build_question_query``). Answer-option text is intentionally excluded
  from the BM25 query; option content is evaluated separately as an alignment
  signal only (``_MATCH_REASON_OPTION``), never blended into base term
  overlap/IDF scoring.

BM25 document construction (per candidate chunk), scored as three weighted
fields sharing corpus-wide IDF:
  - title field: resource/chunk title
  - metadata field: official_resources.metadata topic/domain/domains/
    exam_domain/category/feature/features values
  - body field: chunk_text
Title and metadata fields carry more weight than the body field so that a
document's stated topic/domain matters more than incidental body overlap.

Qualification rule (conservative, precision-first):
  A candidate must reach MIN_RELEVANCE_SCORE (or GENERIC_EXAM_GUIDE_MIN_SCORE
  for exam_guide resources, which are additionally penalized), have
  non-generic, discriminating question-to-content overlap (i.e. overlap terms
  that are not near-universal within the candidate pool), and satisfy at least
  one credible alignment signal (exact domain-metadata match, title match,
  metadata/feature match, or option overlap). Ranking first is not sufficient;
  weak candidates are excluded even when top-K has unused capacity.

Freezing itself happens through create_or_get_ai_quality_audit_run_v1.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from workers.ai_quality_audit_context import load_blind_audit_context

LIST_CANDIDATE_CHUNKS_RPC = "list_audit_candidate_resource_chunks_v1"

RETRIEVAL_METHOD = "bm25_question_match_v1"
CANDIDATE_POOL_MAX = 200
DEFAULT_MAX_EVIDENCE_CHUNKS = 8
DEFAULT_MAX_EVIDENCE_CHARACTERS = 16_000
DEFAULT_MAX_CHUNKS_PER_RESOURCE = 2

# BM25 tuning (standard Okapi BM25 defaults; fixed constants, not tuned per
# question). Corpus statistics (IDF, average field lengths) are still computed
# fresh per question from that question's own candidate pool.
BM25_K1 = 1.5
BM25_B = 0.75
BM25_FIELD_WEIGHT_TITLE = 3.0
BM25_FIELD_WEIGHT_METADATA = 2.5
BM25_FIELD_WEIGHT_BODY = 1.0
# Normalization reference: realistic best-case single-field saturation bound.
# Using the title weight directly assumes a focused document can strongly match
# via its title (or another single high-weight field), but not that it
# simultaneously saturates title, metadata, and body. Calibrated slightly below
# the raw title weight so marginally focused docs (e.g. strong body overlap
# without title saturation) can reach MIN_RELEVANCE_SCORE without lowering
# qualification guards.
BM25_NORMALIZATION_FIELD_WEIGHT = 2.60

# Exact domain/metadata-field match classification thresholds. Domain matching
# is a match-reason (alignment signal) and a deterministic tie-break signal
# ONLY — it is never added to relevance_score and can never independently
# push a candidate over the qualification threshold.
DOMAIN_EXACT_MATCH_BOOST = 0.18
DOMAIN_TEXT_MENTION_BOOST = 0.06

# Multiplicative penalty applied to generic exam-guide resources on top of the
# stricter GENERIC_EXAM_GUIDE_MIN_SCORE threshold below.
EXAM_GUIDE_SCORE_MULTIPLIER = 0.7

MIN_RELEVANCE_SCORE = 0.20
GENERIC_EXAM_GUIDE_MIN_SCORE = 0.30

# Every candidate's query/content overlap must include terms that are not
# near-universal within the candidate pool (i.e. carry real IDF weight). This
# guard applies unconditionally — including to exact-domain-match candidates —
# so domain metadata alone can never substitute for genuine content overlap.
MIN_DISCRIMINATIVE_OVERLAP_IDF = 0.35

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

_EMPTY_EVIDENCE_CANONICAL_JSON = "[]"


class AiQualityAuditEvidenceError(RuntimeError):
    """Raised when evidence retrieval or freezing preparation fails."""


@dataclass(frozen=True)
class Bm25CorpusStats:
    """Per-question BM25 corpus statistics computed from that question's own
    certification candidate pool (deterministic given the same candidate list).
    """

    document_count: int
    idf: Dict[str, float]
    avg_title_len: float
    avg_metadata_len: float
    avg_body_len: float


@dataclass(frozen=True)
class EvidenceScoreBreakdown:
    relevance_score: float
    bm25_score: float
    domain_boost: float
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

    corpus_stats = build_bm25_corpus_stats(candidates, resource_by_id=resource_by_id)

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
            corpus_stats=corpus_stats,
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
            0 if _MATCH_REASON_DOMAIN in item["match_reasons"] else 1,
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


def _resolve_document_fields(
    candidate: Mapping[str, Any],
    *,
    resource_metadata: Mapping[str, Any],
    resource_title: str,
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    """Return (title_tokens, metadata_tokens, body_tokens) for BM25 fields.

    Tokens preserve repeats (term frequency matters for BM25); ``_content_tokens``
    already strips stopwords and normalizes case/punctuation.
    """
    title = str(candidate.get("title") or resource_title or "")
    chunk_text = str(candidate.get("chunk_text") or "")
    title_tokens = _content_tokens(title)
    metadata_tokens: List[str] = []
    for value in sorted(_metadata_text_values(resource_metadata)):
        metadata_tokens.extend(_content_tokens(value))
    body_tokens = _content_tokens(chunk_text)
    return title_tokens, tuple(metadata_tokens), body_tokens


def build_bm25_corpus_stats(
    candidates: Sequence[Mapping[str, Any]],
    *,
    resource_by_id: Mapping[str, Mapping[str, Any]],
) -> Bm25CorpusStats:
    """Compute document-frequency/IDF and average field lengths for one question's
    candidate pool. Statistics are local to this call's candidate list only, so
    generic terms recurring across most candidates are naturally down-weighted.
    """
    document_count = len(candidates)
    document_frequency: Dict[str, int] = {}
    title_lengths: List[int] = []
    metadata_lengths: List[int] = []
    body_lengths: List[int] = []

    for candidate in candidates:
        resource_id = str(candidate.get("resource_id"))
        resource_row = resource_by_id.get(resource_id) or {}
        resource_metadata = resource_row.get("metadata") or {}
        resource_title = str(resource_row.get("title") or candidate.get("title") or "")
        title_tokens, metadata_tokens, body_tokens = _resolve_document_fields(
            candidate,
            resource_metadata=resource_metadata,
            resource_title=resource_title,
        )
        title_lengths.append(len(title_tokens))
        metadata_lengths.append(len(metadata_tokens))
        body_lengths.append(len(body_tokens))

        document_terms = set(title_tokens) | set(metadata_tokens) | set(body_tokens)
        for term in document_terms:
            document_frequency[term] = document_frequency.get(term, 0) + 1

    idf = {
        term: math.log(1 + (document_count - freq + 0.5) / (freq + 0.5))
        for term, freq in document_frequency.items()
    }

    def _avg(values: Sequence[int]) -> float:
        return (sum(values) / len(values)) if values else 1.0

    return Bm25CorpusStats(
        document_count=document_count,
        idf=idf,
        avg_title_len=_avg(title_lengths) or 1.0,
        avg_metadata_len=_avg(metadata_lengths) or 1.0,
        avg_body_len=_avg(body_lengths) or 1.0,
    )


def _bm25_field_score(
    query_terms: Sequence[str],
    field_tokens: Sequence[str],
    *,
    idf: Mapping[str, float],
    avg_field_len: float,
) -> float:
    """Okapi BM25 term-frequency-saturated score for one document field."""
    if not field_tokens or not query_terms:
        return 0.0
    counts = Counter(field_tokens)
    field_len = len(field_tokens)
    avg_len = avg_field_len if avg_field_len > 0 else 1.0
    score = 0.0
    for term in query_terms:
        tf = counts.get(term, 0)
        if tf == 0:
            continue
        term_idf = idf.get(term, 0.0)
        denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * (field_len / avg_len))
        score += term_idf * (tf * (BM25_K1 + 1)) / denom
    return score


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
    corpus_stats: Bm25CorpusStats,
    resource_type: str = "",
) -> EvidenceScoreBreakdown:
    """BM25 relevance score (per-field-weighted) with precision-first qualification."""
    del query_text  # retained in signature for call-site symmetry/debuggability
    chunk_text = str(candidate.get("chunk_text") or "")
    title = str(candidate.get("title") or resource_title or "")
    min_score = _applicable_min_score(resource_type)
    if not (title.strip() or chunk_text.strip()):
        return EvidenceScoreBreakdown(
            relevance_score=0.0,
            bm25_score=0.0,
            domain_boost=0.0,
            content_overlap_count=0,
            content_overlap_tokens=(),
            match_reasons=(),
            qualifies=False,
            applicable_threshold=min_score,
            rejection_reason="empty chunk text and title",
        )

    title_tokens, metadata_tokens, body_tokens = _resolve_document_fields(
        candidate,
        resource_metadata=resource_metadata,
        resource_title=resource_title,
    )
    document_tokens = set(title_tokens) | set(metadata_tokens) | set(body_tokens)
    unique_query_terms = tuple(sorted(set(query_tokens)))

    overlap_tokens = tuple(sorted(set(query_tokens) & document_tokens))
    question_overlap = set(question_tokens) & document_tokens
    option_overlap = set(option_tokens) & document_tokens

    title_field_score = _bm25_field_score(
        unique_query_terms,
        title_tokens,
        idf=corpus_stats.idf,
        avg_field_len=corpus_stats.avg_title_len,
    )
    metadata_field_score = _bm25_field_score(
        unique_query_terms,
        metadata_tokens,
        idf=corpus_stats.idf,
        avg_field_len=corpus_stats.avg_metadata_len,
    )
    body_field_score = _bm25_field_score(
        unique_query_terms,
        body_tokens,
        idf=corpus_stats.idf,
        avg_field_len=corpus_stats.avg_body_len,
    )

    raw_bm25 = (
        BM25_FIELD_WEIGHT_TITLE * title_field_score
        + BM25_FIELD_WEIGHT_METADATA * metadata_field_score
        + BM25_FIELD_WEIGHT_BODY * body_field_score
    )
    total_query_idf = sum(corpus_stats.idf.get(term, 0.0) for term in unique_query_terms)
    normalizer = total_query_idf * (BM25_K1 + 1) * BM25_NORMALIZATION_FIELD_WEIGHT
    bm25_score = (raw_bm25 / normalizer) if normalizer > 0 else 0.0
    bm25_score = max(0.0, min(1.0, bm25_score))

    # Domain match is an alignment/tie-break signal only (see module docstring
    # and DOMAIN_EXACT_MATCH_BOOST comment) — it is deliberately NOT added to
    # relevance_score, so domain metadata alone can never qualify a candidate.
    domain_boost = _domain_match_boost(
        question_domain=question_domain,
        resource_metadata=resource_metadata,
        title=title,
        chunk_text=chunk_text,
    )

    raw_relevance = bm25_score
    if str(resource_type).strip().lower() == "exam_guide":
        raw_relevance *= EXAM_GUIDE_SCORE_MULTIPLIER
    relevance_score = max(0.0, min(1.0, raw_relevance))

    match_reasons: List[str] = []
    if domain_boost >= DOMAIN_EXACT_MATCH_BOOST:
        match_reasons.append(_MATCH_REASON_DOMAIN)
    if title_field_score > 0.0 or _option_in_title(option_tokens, title):
        match_reasons.append(_MATCH_REASON_TITLE)
    if len(question_overlap) >= 2:
        match_reasons.append(_MATCH_REASON_QUESTION)
    if option_overlap:
        match_reasons.append(_MATCH_REASON_OPTION)
    if metadata_field_score > 0.0:
        match_reasons.append(_MATCH_REASON_METADATA)

    discriminative_overlap_idf = sum(
        corpus_stats.idf.get(term, 0.0) for term in overlap_tokens
    )

    qualifies, rejection_reason = _candidate_qualifies(
        relevance_score=relevance_score,
        match_reasons=match_reasons,
        overlap_count=len(overlap_tokens),
        discriminative_overlap_idf=discriminative_overlap_idf,
        resource_type=resource_type,
        document_count=corpus_stats.document_count,
    )

    return EvidenceScoreBreakdown(
        relevance_score=relevance_score,
        bm25_score=bm25_score,
        domain_boost=domain_boost,
        content_overlap_count=len(overlap_tokens),
        content_overlap_tokens=overlap_tokens,
        match_reasons=tuple(match_reasons),
        qualifies=qualifies,
        applicable_threshold=min_score,
        rejection_reason=rejection_reason,
    )


def _effective_min_discriminative_overlap_idf(document_count: int) -> float:
    """Scale the discriminative-overlap floor to the candidate-pool size.

    In a one-document pool every overlapping term carries the maximum IDF
    achievable for that pool, so the global floor would reject valid evidence.
    For multi-document pools the configured floor applies unchanged.
    """
    if document_count <= 1:
        return 0.0
    max_unique_term_idf = math.log(1 + (document_count - 1 + 0.5) / (1 + 0.5))
    return min(MIN_DISCRIMINATIVE_OVERLAP_IDF, max_unique_term_idf)


def _applicable_min_score(resource_type: str) -> float:
    if str(resource_type).strip().lower() == "exam_guide":
        return GENERIC_EXAM_GUIDE_MIN_SCORE
    return MIN_RELEVANCE_SCORE


def _candidate_qualifies(
    *,
    relevance_score: float,
    match_reasons: Sequence[str],
    overlap_count: int,
    discriminative_overlap_idf: float,
    resource_type: str,
    document_count: int,
) -> Tuple[bool, str]:
    """Content-overlap and discriminative-IDF guards apply unconditionally.

    Domain metadata matching alone (``_MATCH_REASON_DOMAIN``) is never exempt
    from these guards: every candidate, including an exact-domain-match
    candidate, must independently demonstrate meaningful, non-generic
    question-to-document overlap and clear the relevance-score threshold.
    """
    if not match_reasons:
        return False, (
            "no alignment signal (requires domain, title, question-text, option, "
            "or metadata/feature match)"
        )
    if overlap_count == 0:
        return False, "zero query-token overlap with document content"
    min_discriminative_idf = _effective_min_discriminative_overlap_idf(document_count)
    if discriminative_overlap_idf < min_discriminative_idf:
        return False, (
            "question-to-content overlap lacks non-generic discriminating terms "
            f"(discriminative overlap {discriminative_overlap_idf:.6f} below "
            f"{min_discriminative_idf:.2f})"
        )
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
    """Build the base BM25 query from stem and domain only.

    Answer options are excluded here because they are scored separately via
    ``_option_content_tokens`` and the option-overlap match reason. Including
    full option strings would inflate the query and dilute BM25 term weighting.
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
        return DOMAIN_EXACT_MATCH_BOOST
    haystack = _normalize_text(f"{title} {chunk_text}")
    if normalized_domain in haystack:
        return DOMAIN_TEXT_MENTION_BOOST
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
