"""Authoritative embedding text resolution for V48 real hybrid replay.

Resolves production-equivalent query and candidate chunk text for the frozen
10-question replay set using blind audit context and narrowly scoped candidate
chunk reads. Does not use synthetic placeholder text on the execute path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from workers.ai_quality_audit_context import (
    AiQualityAuditContextError,
    load_blind_audit_context,
)
from workers.ai_quality_audit_evidence import (
    AiQualityAuditEvidenceError,
    _build_question_query,
    _content_tokens,
    _load_active_resources,
    _list_candidate_chunks,
    _option_content_tokens,
    analyze_evidence_candidate,
    build_bm25_corpus_stats,
)
from workers.ai_quality_audit_hybrid_replay import (
    HybridReplayEmbeddingTextError,
    build_replay_candidate_identity,
)
from workers.ai_quality_audit_shadow import (
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    classify_question_shadow_from_replay_record,
)
from workers.resource_chunking import sha256_hex

AUTHORITATIVE_TEXT_SCHEMA_VERSION = "v48_authoritative_embedding_text_v1"
DEFAULT_VALIDATED_MODEL_VERSION = "openai-text-embedding-3-small-2026-07-03"
STALE_MODEL_VERSION_TAGS = frozenset({"2024-01-15", "v1"})
REPLAY_RELEVANCE_SCORE_TOLERANCE = 1e-4
# Matches list_audit_candidate_resource_chunks_v1 (1..200). The frozen replay
# resolves only Stage 1 fixture candidates by title/resource_type within the
# certification-scoped active-resource pool; production evidence uses the same
# 200-chunk bound and does not require a full-table scan or pagination.
MIN_AUTHORITATIVE_CANDIDATE_CHUNKS = 1
MAX_AUTHORITATIVE_CANDIDATE_CHUNKS = 200
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_ERROR_DETAIL_LEN = 240
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+"),
    re.compile(r"https?://[^\s/]+:[^\s@/]+@[^\s]+"),
    re.compile(r"(?i)service[_-]?role[_-]?key[^\s]*"),
)
_DICT_BLOB_RE = re.compile(r"\{[^{}]*\}")

FAILURE_STAGE_EXECUTE_PREFLIGHT = "execute_preflight"
FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION = "authoritative_question_resolution"
FAILURE_STAGE_AUTHORITATIVE_RESOURCE_RESOLUTION = "authoritative_resource_resolution"
FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION = "authoritative_chunk_resolution"
FAILURE_STAGE_AUTHORITATIVE_MATCHING = "authoritative_matching"
FAILURE_STAGE_EMBEDDING_EXECUTION = "embedding_execution"
FAILURE_STAGE_RESULT_AGGREGATION = "result_aggregation"

ALLOWED_ERROR_TYPES = frozenset(
    {
        "authoritative_resolution",
        "context_loader",
        "evidence_loader",
        "rpc_response",
        "supabase_auth",
        "network_transport",
        "configuration",
        "unexpected",
        "embedding_provider",
        "embedding_cache",
        "budget",
        "input_validation",
        "rpc_validation",
    }
)


class AuthoritativeEmbeddingTextError(RuntimeError):
    """Raised when authoritative embedding text cannot be resolved safely."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "authoritative_text_resolution_failed",
        failure_stage: str = FAILURE_STAGE_AUTHORITATIVE_MATCHING,
        error_type: str = "authoritative_resolution",
        error_detail: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        if error_type not in ALLOWED_ERROR_TYPES:
            raise ValueError(f"unsupported error_type {error_type!r}")
        self.error_code = str(error_code)
        self.failure_stage = str(failure_stage)
        self.error_type = str(error_type)
        self.error_detail = (
            sanitize_error_detail(error_detail)
            if error_detail is not None
            else sanitize_error_detail(message)
        )


def sanitize_error_detail(raw: str) -> str:
    """Return a bounded, redacted error detail safe for redacted replay summaries."""
    text = _WHITESPACE_RE.sub(" ", str(raw or "").strip())
    text = _DICT_BLOB_RE.sub("{...}", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    if len(text) > _MAX_ERROR_DETAIL_LEN:
        text = text[: _MAX_ERROR_DETAIL_LEN - 3] + "..."
    return text or "unspecified execution failure"


def _is_rpc_parameter_validation_message(message: str) -> bool:
    lowered = str(message or "").lower()
    if "22023" in lowered:
        return True
    if "p_max_chunks must be between" in lowered:
        return True
    if "rejected:" in lowered and "p_max_chunks" in lowered:
        return True
    if "invalid parameter" in lowered or "invalid input value" in lowered:
        return True
    return False


def validate_authoritative_candidate_chunk_limit(max_chunks: int) -> int:
    """Validate p_max_chunks against the RPC contract before any Supabase call."""
    if isinstance(max_chunks, bool) or not isinstance(max_chunks, int):
        raise AuthoritativeEmbeddingTextError(
            "authoritative candidate chunk limit must be an integer",
            error_code="authoritative_chunk_request_invalid",
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
            error_type="input_validation",
            error_detail=(
                f"p_max_chunks must be an integer between "
                f"{MIN_AUTHORITATIVE_CANDIDATE_CHUNKS} and {MAX_AUTHORITATIVE_CANDIDATE_CHUNKS}"
            ),
        )
    if (
        max_chunks < MIN_AUTHORITATIVE_CANDIDATE_CHUNKS
        or max_chunks > MAX_AUTHORITATIVE_CANDIDATE_CHUNKS
    ):
        raise AuthoritativeEmbeddingTextError(
            "authoritative candidate chunk limit is outside the RPC contract",
            error_code="authoritative_chunk_request_invalid",
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
            error_type="input_validation",
            error_detail=(
                f"p_max_chunks must be between {MIN_AUTHORITATIVE_CANDIDATE_CHUNKS} "
                f"and {MAX_AUTHORITATIVE_CANDIDATE_CHUNKS}, got {max_chunks}"
            ),
        )
    return max_chunks


def _classify_supabase_loader_message(message: str) -> tuple[str, str]:
    lowered = str(message or "").lower()
    if _is_rpc_parameter_validation_message(message):
        return "rpc_parameter_invalid", "rpc_validation"
    if any(token in lowered for token in ("unauthorized", "forbidden", "401", "403", "jwt")):
        return "supabase_unauthorized", "supabase_auth"
    if any(
        token in lowered
        for token in (
            "timeout",
            "connection refused",
            "connection reset",
            "network",
            "call failed",
            "transport",
        )
    ):
        return "supabase_transport_failed", "network_transport"
    if any(
        token in lowered
        for token in ("malformed", "no rows", "expected exactly", "missing content_hash")
    ):
        return "malformed_rpc_response", "rpc_response"
    return "loader_failed", "context_loader"


def _wrap_context_loader_error(exc: AiQualityAuditContextError) -> AuthoritativeEmbeddingTextError:
    code, error_type = _classify_supabase_loader_message(str(exc))
    if code == "malformed_rpc_response":
        error_code = "authoritative_question_context_malformed"
    elif code == "supabase_unauthorized":
        error_code = "supabase_unauthorized"
    elif code == "supabase_transport_failed":
        error_code = "supabase_transport_failed"
    else:
        error_code = "authoritative_question_context_failed"
    return AuthoritativeEmbeddingTextError(
        "authoritative question context could not be loaded",
        error_code=error_code,
        failure_stage=FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION,
        error_type=error_type,
        error_detail=sanitize_error_detail(str(exc)),
    )


def _wrap_evidence_loader_error(
    exc: AiQualityAuditEvidenceError,
    *,
    failure_stage: str,
    default_error_code: str,
) -> AuthoritativeEmbeddingTextError:
    code, error_type = _classify_supabase_loader_message(str(exc))
    if code == "rpc_parameter_invalid":
        error_code = "rpc_parameter_invalid"
    elif code == "malformed_rpc_response":
        if failure_stage == FAILURE_STAGE_AUTHORITATIVE_RESOURCE_RESOLUTION:
            error_code = "authoritative_active_resource_malformed"
        else:
            error_code = "authoritative_candidate_chunk_malformed"
    elif code == "supabase_unauthorized":
        error_code = "supabase_unauthorized"
    elif code == "supabase_transport_failed":
        error_code = "supabase_transport_failed"
    else:
        error_code = default_error_code
    loader_type = error_type if error_type != "context_loader" else "evidence_loader"
    return AuthoritativeEmbeddingTextError(
        "authoritative candidate evidence could not be loaded",
        error_code=error_code,
        failure_stage=failure_stage,
        error_type=loader_type,
        error_detail=sanitize_error_detail(str(exc)),
    )

@dataclass(frozen=True)
class CandidateTextBinding:
    question_version_id: str
    candidate_position: int
    candidate_identity: str
    title: str
    resource_type: str
    expected_relevance_score: float


def normalize_embedding_input_text(text: str) -> str:
    """Normalize embedding input text deterministically for hashing."""
    return _WHITESPACE_RE.sub(" ", str(text).strip())


def compute_authoritative_content_hash(text: str) -> str:
    """Return lowercase SHA-256 hex digest of normalized embedding input text."""
    normalized = normalize_embedding_input_text(text)
    if not normalized:
        raise AuthoritativeEmbeddingTextError("authoritative content hash requires nonempty text")
    return sha256_hex(normalized)


def compute_replay_content_set_hash(*content_hashes: str) -> str:
    """Hash the ordered set of authoritative content hashes for one replay run."""
    if not content_hashes:
        raise AuthoritativeEmbeddingTextError(
            "replay content set hash requires at least one content hash"
        )
    payload = json.dumps(
        sorted(content_hashes),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256_hex(payload)


def _normalize_label(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(value).strip().lower())


def _round_replay_score(value: float) -> float:
    return round(float(value), 4)


def _all_semantic_review_bindings(
    question_record: Mapping[str, Any],
) -> list[CandidateTextBinding]:
    question_version_id = str(question_record["question_version_id"])
    shadow = classify_question_shadow_from_replay_record(question_record)
    if shadow["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
        return []

    bindings: list[CandidateTextBinding] = []
    for candidate_position, candidate in enumerate(shadow["candidates"]):
        if not bool(candidate["l1_structural_guards_pass"]):
            continue
        bindings.append(
            CandidateTextBinding(
                question_version_id=question_version_id,
                candidate_position=candidate_position,
                candidate_identity=build_replay_candidate_identity(
                    question_version_id=question_version_id,
                    candidate_position=candidate_position,
                    title=str(candidate["title"]),
                    resource_type=str(candidate.get("resource_type") or ""),
                ),
                title=str(candidate["title"]),
                resource_type=str(candidate.get("resource_type") or ""),
                expected_relevance_score=_round_replay_score(
                    float(candidate["relevance_score"])
                ),
            )
        )
    return bindings


def _selected_semantic_review_bindings(
    question_record: Mapping[str, Any],
    *,
    candidate_limit: int,
) -> list[CandidateTextBinding]:
    question_version_id = str(question_record["question_version_id"])
    shadow = classify_question_shadow_from_replay_record(question_record)
    if shadow["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
        return []

    eligible: list[CandidateTextBinding] = []
    for candidate_position, candidate in enumerate(shadow["candidates"]):
        if not bool(candidate["l1_structural_guards_pass"]):
            continue
        eligible.append(
            CandidateTextBinding(
                question_version_id=question_version_id,
                candidate_position=candidate_position,
                candidate_identity=build_replay_candidate_identity(
                    question_version_id=question_version_id,
                    candidate_position=candidate_position,
                    title=str(candidate["title"]),
                    resource_type=str(candidate.get("resource_type") or ""),
                ),
                title=str(candidate["title"]),
                resource_type=str(candidate.get("resource_type") or ""),
                expected_relevance_score=_round_replay_score(
                    float(candidate["relevance_score"])
                ),
            )
        )

    eligible.sort(
        key=lambda item: (
            -item.expected_relevance_score,
            item.candidate_identity,
        )
    )
    return eligible[:candidate_limit]


def _match_live_candidate_chunk_text(
    *,
    binding: CandidateTextBinding,
    question_record: Mapping[str, Any],
    blind_context: Mapping[str, Any],
    live_candidates: Sequence[Mapping[str, Any]],
    resource_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    title_matches = [
        candidate
        for candidate in live_candidates
        if _normalize_label(str(candidate.get("title") or "")) == _normalize_label(binding.title)
        and _normalize_label(str(candidate.get("resource_type") or ""))
        == _normalize_label(binding.resource_type)
    ]
    if not title_matches:
        raise AuthoritativeEmbeddingTextError(
            "authoritative candidate chunk not found for replay identity "
            f"{binding.candidate_identity}",
            error_code="authoritative_candidate_not_found",
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_MATCHING,
            error_detail="no live candidate chunk matched the frozen replay identity",
        )

    query_text = _build_question_query(blind_context)
    question_tokens = _content_tokens(str(blind_context.get("question_text") or ""))
    option_tokens = _option_content_tokens(blind_context)
    query_tokens = _content_tokens(query_text)
    question_domain = str(blind_context.get("domain_name") or "").strip()
    corpus_stats = build_bm25_corpus_stats(live_candidates, resource_by_id=resource_by_id)

    scored_matches: list[tuple[float, Mapping[str, Any]]] = []
    for candidate in title_matches:
        resource_id = str(candidate["resource_id"])
        resource_row = resource_by_id.get(resource_id) or {}
        _breakdown, replay_record = analyze_evidence_candidate(
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
        del _breakdown
        score_delta = abs(
            _round_replay_score(float(replay_record["relevance_score"]))
            - binding.expected_relevance_score
        )
        scored_matches.append((score_delta, candidate))

    scored_matches.sort(key=lambda item: (item[0], str(item[1]["resource_chunk_id"])))
    best_delta, best_candidate = scored_matches[0]
    if best_delta > REPLAY_RELEVANCE_SCORE_TOLERANCE:
        raise AuthoritativeEmbeddingTextError(
            "authoritative candidate chunk relevance mismatch for replay identity "
            f"{binding.candidate_identity}",
            error_code="authoritative_score_mismatch",
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_MATCHING,
            error_detail="live BM25 relevance score did not match the frozen replay score",
        )

    if len(scored_matches) > 1:
        second_delta, second_candidate = scored_matches[1]
        if (
            second_delta <= REPLAY_RELEVANCE_SCORE_TOLERANCE
            and str(second_candidate["resource_chunk_id"])
            != str(best_candidate["resource_chunk_id"])
        ):
            raise AuthoritativeEmbeddingTextError(
                "authoritative candidate chunk resolution is ambiguous for replay identity "
                f"{binding.candidate_identity}",
                error_code="authoritative_ambiguous_match",
                failure_stage=FAILURE_STAGE_AUTHORITATIVE_MATCHING,
                error_detail="multiple live candidate chunks matched the frozen replay identity",
            )

    chunk_text = normalize_embedding_input_text(str(best_candidate.get("chunk_text") or ""))
    if not chunk_text:
        raise AuthoritativeEmbeddingTextError(
            "authoritative candidate chunk text is empty for replay identity "
            f"{binding.candidate_identity}",
            error_code="authoritative_empty_chunk_text",
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_MATCHING,
            error_detail="matched live candidate chunk has empty chunk_text",
        )
    return chunk_text


class AuthoritativeEmbeddingTextResolver:
    """Resolve authoritative query and chunk text for one frozen replay fixture."""

    authoritative_text_used = True

    def __init__(
        self,
        fixture: Mapping[str, Any],
        *,
        candidate_limit: int,
        question_text_loader: Callable[[str], str],
        candidate_pool_loader: Callable[
            [str],
            tuple[
                list[dict[str, Any]],
                dict[str, dict[str, Any]],
                Mapping[str, Any],
            ],
        ],
    ) -> None:
        if candidate_limit <= 0:
            raise AuthoritativeEmbeddingTextError("candidate_limit must be positive")
        self._fixture_questions = {
            str(record["question_version_id"]): record for record in fixture["questions"]
        }
        if len(self._fixture_questions) != len(fixture["questions"]):
            raise AuthoritativeEmbeddingTextError(
                "duplicate question_version_id in frozen replay fixture"
            )
        self._candidate_limit = int(candidate_limit)
        self._question_text_loader = question_text_loader
        self._candidate_pool_loader = candidate_pool_loader
        self._question_text_by_id: dict[str, str] = {}
        self._candidate_text_by_identity: dict[str, str] = {}
        self._content_hashes: list[str] = []
        self._prepared = False

    @property
    def replay_content_set_hash(self) -> str:
        if not self._prepared:
            raise AuthoritativeEmbeddingTextError(
                "authoritative embedding text has not been prepared"
            )
        return compute_replay_content_set_hash(*self._content_hashes)

    def prepare(self) -> None:
        """Resolve all required authoritative texts before any embedding request."""
        if self._prepared:
            return

        seen_query_ids: set[str] = set()
        seen_candidate_identities: set[str] = set()
        content_hashes: list[str] = []

        for question_version_id in sorted(self._fixture_questions):
            question_record = self._fixture_questions[question_version_id]
            shadow = classify_question_shadow_from_replay_record(question_record)
            if shadow["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
                continue

            if question_version_id in seen_query_ids:
                raise AuthoritativeEmbeddingTextError(
                    f"duplicate authoritative query resolution for {question_version_id!r}"
                )
            seen_query_ids.add(question_version_id)

            query_text = normalize_embedding_input_text(
                self._question_text_loader(question_version_id)
            )
            if not query_text:
                raise AuthoritativeEmbeddingTextError(
                    f"authoritative query text is empty for question {question_version_id!r}",
                    error_code="authoritative_empty_query_text",
                    failure_stage=FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION,
                    error_detail="blind audit context produced empty query text",
                )
            self._question_text_by_id[question_version_id] = query_text
            content_hashes.append(compute_authoritative_content_hash(query_text))

            bindings = _all_semantic_review_bindings(question_record)
            live_candidates, resource_by_id, blind_context = self._candidate_pool_loader(
                question_version_id
            )
            for binding in bindings:
                if binding.candidate_identity in seen_candidate_identities:
                    raise AuthoritativeEmbeddingTextError(
                        "duplicate authoritative candidate resolution for replay identity "
                        f"{binding.candidate_identity!r}"
                    )
                seen_candidate_identities.add(binding.candidate_identity)
                chunk_text = _match_live_candidate_chunk_text(
                    binding=binding,
                    question_record=question_record,
                    blind_context=blind_context,
                    live_candidates=live_candidates,
                    resource_by_id=resource_by_id,
                )
                self._candidate_text_by_identity[binding.candidate_identity] = chunk_text
                content_hashes.append(compute_authoritative_content_hash(chunk_text))

        self._content_hashes = content_hashes
        self._prepared = True

    @classmethod
    def from_resolved_texts(
        cls,
        fixture: Mapping[str, Any],
        *,
        candidate_limit: int,
        question_text_by_id: Mapping[str, str],
        candidate_text_by_identity: Mapping[str, str],
    ) -> AuthoritativeEmbeddingTextResolver:
        """Build a prepared resolver from explicit authoritative texts (tests only)."""
        resolver = cls(
            fixture,
            candidate_limit=candidate_limit,
            question_text_loader=lambda _question_version_id: "",
            candidate_pool_loader=lambda _question_version_id: ([], {}, {}),
        )
        content_hashes: list[str] = []
        resolver._question_text_by_id = {}
        for question_version_id, text in question_text_by_id.items():
            normalized = normalize_embedding_input_text(text)
            if not normalized:
                raise AuthoritativeEmbeddingTextError(
                    f"authoritative query text is empty for question {question_version_id!r}"
                )
            resolver._question_text_by_id[str(question_version_id).strip().lower()] = normalized
            content_hashes.append(compute_authoritative_content_hash(normalized))

        resolver._candidate_text_by_identity = {}
        for identity, text in candidate_text_by_identity.items():
            normalized = normalize_embedding_input_text(text)
            if not normalized:
                raise AuthoritativeEmbeddingTextError(
                    "authoritative candidate text is empty for replay identity "
                    f"{identity!r}"
                )
            resolver._candidate_text_by_identity[str(identity)] = normalized
            content_hashes.append(compute_authoritative_content_hash(normalized))

        resolver._content_hashes = content_hashes
        resolver._prepared = True
        return resolver

    def resolve_question_embedding_text(self, question_version_id: str) -> Optional[str]:
        if not self._prepared:
            raise AuthoritativeEmbeddingTextError(
                "authoritative embedding text must be prepared before resolution"
            )
        normalized_id = str(question_version_id).strip().lower()
        if normalized_id not in self._fixture_questions:
            raise AuthoritativeEmbeddingTextError(
                f"question {question_version_id!r} is outside the frozen replay scope"
            )
        return self._question_text_by_id.get(normalized_id)

    def resolve_candidate_embedding_text(self, candidate_identity: str) -> Optional[str]:
        if not self._prepared:
            raise AuthoritativeEmbeddingTextError(
                "authoritative embedding text must be prepared before resolution"
            )
        return self._candidate_text_by_identity.get(candidate_identity)


class FixtureEmbeddingTextResolver:
    """Synthetic offline-only resolver for unit tests and mock replay paths."""

    authoritative_text_used = False

    def __init__(
        self,
        *,
        question_text_by_id: Mapping[str, str],
        candidate_text_by_identity: Mapping[str, str],
    ) -> None:
        self._question_text_by_id = dict(question_text_by_id)
        self._candidate_text_by_identity = dict(candidate_text_by_identity)

    def prepare(self) -> None:
        return None

    def resolve_question_embedding_text(self, question_version_id: str) -> Optional[str]:
        return self._question_text_by_id.get(question_version_id)

    def resolve_candidate_embedding_text(self, candidate_identity: str) -> Optional[str]:
        return self._candidate_text_by_identity.get(candidate_identity)


def build_fixture_embedding_text_resolver(
    fixture: Mapping[str, Any],
) -> FixtureEmbeddingTextResolver:
    """Build synthetic embedding text for offline tests only."""
    question_text_by_id: dict[str, str] = {}
    candidate_text_by_identity: dict[str, str] = {}

    for record in fixture["questions"]:
        question_version_id = str(record["question_version_id"])
        question_text_by_id[question_version_id] = (
            f"synthetic-question-{question_version_id[:8]}"
        )
        shadow = classify_question_shadow_from_replay_record(record)
        for candidate_position, candidate in enumerate(shadow["candidates"]):
            identity = build_replay_candidate_identity(
                question_version_id=question_version_id,
                candidate_position=candidate_position,
                title=str(candidate["title"]),
                resource_type=str(candidate.get("resource_type") or ""),
            )
            candidate_text_by_identity[identity] = f"synthetic-candidate-{identity}"

    return FixtureEmbeddingTextResolver(
        question_text_by_id=question_text_by_id,
        candidate_text_by_identity=candidate_text_by_identity,
    )


def assert_execute_resolver_is_authoritative(resolver: Any) -> None:
    """Reject synthetic or unknown resolvers on the real execute path."""
    if isinstance(resolver, FixtureEmbeddingTextResolver):
        raise AuthoritativeEmbeddingTextError(
            "execute path cannot use synthetic FixtureEmbeddingTextResolver"
        )
    if not getattr(resolver, "authoritative_text_used", False):
        raise AuthoritativeEmbeddingTextError(
            "execute path requires an authoritative embedding text resolver"
        )


def build_supabase_authoritative_embedding_text_resolver(
    client: Any,
    fixture: Mapping[str, Any],
    *,
    candidate_limit: int,
) -> AuthoritativeEmbeddingTextResolver:
    """Build a resolver backed by narrowly scoped read-only Supabase loaders."""

    blind_context_cache: dict[str, Mapping[str, Any]] = {}
    candidate_pool_cache: dict[
        str,
        tuple[list[dict[str, Any]], dict[str, dict[str, Any]], Mapping[str, Any]],
    ] = {}

    def load_query_text(question_version_id: str) -> str:
        blind_context = blind_context_cache.get(question_version_id)
        if blind_context is None:
            try:
                blind_context = load_blind_audit_context(client, question_version_id)
            except AiQualityAuditContextError as exc:
                raise _wrap_context_loader_error(exc) from exc
            blind_context_cache[question_version_id] = blind_context
        return _build_question_query(blind_context)

    def load_candidate_pool(
        question_version_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], Mapping[str, Any]]:
        cached = candidate_pool_cache.get(question_version_id)
        if cached is not None:
            return cached

        blind_context = blind_context_cache.get(question_version_id)
        if blind_context is None:
            try:
                blind_context = load_blind_audit_context(client, question_version_id)
            except AiQualityAuditContextError as exc:
                raise _wrap_context_loader_error(exc) from exc
            blind_context_cache[question_version_id] = blind_context

        certification = str(blind_context["certification_exam_name"]).strip()
        try:
            resources = _load_active_resources(client, certification_exam_name=certification)
        except AiQualityAuditEvidenceError as exc:
            raise _wrap_evidence_loader_error(
                exc,
                failure_stage=FAILURE_STAGE_AUTHORITATIVE_RESOURCE_RESOLUTION,
                default_error_code="authoritative_active_resource_failed",
            ) from exc
        resource_by_id = {item["id"]: item for item in resources}
        resource_ids = [item["id"] for item in resources]
        if not resource_ids:
            raise AuthoritativeEmbeddingTextError(
                f"no active official_resources found for question {question_version_id!r}",
                error_code="authoritative_active_resource_missing",
                failure_stage=FAILURE_STAGE_AUTHORITATIVE_RESOURCE_RESOLUTION,
                error_detail="active official_resources row set is empty for certification scope",
            )
        try:
            chunk_limit = validate_authoritative_candidate_chunk_limit(
                MAX_AUTHORITATIVE_CANDIDATE_CHUNKS
            )
            live_candidates = _list_candidate_chunks(
                client,
                certification_exam_name=certification,
                resource_ids=resource_ids,
                max_chunks=chunk_limit,
            )
        except AiQualityAuditEvidenceError as exc:
            raise _wrap_evidence_loader_error(
                exc,
                failure_stage=FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
                default_error_code="authoritative_candidate_chunk_rpc_failed",
            ) from exc
        pool = (live_candidates, resource_by_id, blind_context)
        candidate_pool_cache[question_version_id] = pool
        return pool

    resolver = AuthoritativeEmbeddingTextResolver(
        fixture,
        candidate_limit=candidate_limit,
        question_text_loader=load_query_text,
        candidate_pool_loader=load_candidate_pool,
    )
    return resolver


def map_authoritative_text_error(exc: AuthoritativeEmbeddingTextError) -> HybridReplayEmbeddingTextError:
    return HybridReplayEmbeddingTextError(str(exc))
