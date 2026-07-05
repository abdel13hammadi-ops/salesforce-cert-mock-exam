"""
Single-candidate question generation vertical slice (V57 Phase 1).

Provides a narrow, synchronous, service-level entry point that:

  1. Accepts one bounded generation request (certification, domain,
     difficulty, prompt/model identifiers, provenance/evidence metadata).
  2. Calls the existing LLM provider abstraction (``workers.llm_providers``)
     to generate exactly one structured question.
  3. Strictly validates the model output using the same option-shape rules
     enforced by ``promote_question_candidate_v1`` (label/text/order/
     correctness), before any database mutation.
  4. Persists the validated question into ``public.question_candidates``
     only (never into live ``questions``/``answer_options``).
  5. Enqueues the existing ``deterministic_audit`` and ``llm_audit``
     background jobs for that exact candidate via
     ``enqueue_background_job_v1``, targeting ``target_candidate_id``.

Design notes
------------
* This module is a *service*, not a background-job handler: unlike
  ``workers.job_handlers`` (which never calls ``client.table()`` by design),
  this module uses direct ``client.table()`` access with a service-role
  client — the same pattern already used by
  ``workers.embedding_cache.SupabaseEmbeddingCacheRepository`` and
  ``workers.quality_audit_pilot``. No new RPC or migration is introduced.
* Candidate + options atomicity is structural: ``candidate_payload`` (which
  contains ``options``) lives in the *same row* as the rest of the question
  fields, so a single ``INSERT`` is atomic by Postgres semantics — there is
  no separate options table to roll back.
* The exact audit subject is anchored two ways, both using pre-existing
  mechanisms: (a) ``target_candidate_id`` (the FK already supported by
  ``audit_runs``/``create_audit_run_v1``), and (b) a ``question`` snapshot
  dict built directly from the just-validated in-memory payload — not
  re-fetched from the database — passed into the ``deterministic_audit``/
  ``llm_audit`` job payload exactly like ``workers.run_hybrid_audit_pilot``
  already does. The candidate's deterministic ``content_hash`` is also
  frozen into the job/audit-run metadata so drift is detectable even though
  the table itself has no immutability trigger.
* Reuses ``workers.llm_providers.LlmProvider`` for both generation and audit
  calls. No second provider framework is introduced.

Concurrency limitation (read before calling this from more than one process)
------------------------------------------------------------------------
Duplicate-candidate detection (``QuestionCandidateRepository.find_by_content_hash``)
is a plain ``SELECT``-then-``INSERT``. ``question_candidates.content_hash`` has a
non-unique index only (see
``supabase/migrations/20260623193600_v44_question_candidates.sql``), so this is
**not** safe against concurrent writers: two processes generating the same
content_hash at the same time can both pass the SELECT check and both INSERT,
producing two candidate rows for identical content. This is a classic
time-of-check-to-time-of-use (TOCTOU) gap.

* This is acceptable for the V57 scope: a single bounded, single-process,
  local generation request run at a time (e.g. via
  ``scripts/v57_generate_one_candidate.py``).
* It is explicitly **not** acceptable as-is for concurrent/production
  generation (e.g. multiple workers or a queue draining in parallel).
  Making it safe requires a database-level uniqueness/idempotency constraint
  (e.g. a unique index on ``(certification_exam_name, content_hash)`` plus an
  ``ON CONFLICT`` upsert), which is a schema migration.
* That migration is intentionally **out of scope** for this task per the
  safety gate against introducing new migrations/RLS changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from workers.llm_providers import SKIP_LEGACY_LLM_AUDIT_VALIDATION_METADATA_KEY

logger = logging.getLogger(__name__)


# ===========================================================================
# Exceptions
# ===========================================================================

class CandidateGenerationError(Exception):
    """Base class for all errors raised by this module."""


class CandidateValidationError(CandidateGenerationError):
    """Raised for any malformed request or model output.

    Raised strictly *before* any database mutation.
    """


class CandidatePersistenceError(CandidateGenerationError):
    """Raised when a database read/write required for persistence fails."""


class CandidateProvenanceEventError(CandidatePersistenceError):
    """Raised when the candidate row was inserted but its 'created' event
    could not be recorded.

    ``candidate_id`` is preserved on the exception so callers can inspect or
    retry the provenance write without losing the fact that the candidate
    row itself was successfully persisted.
    """

    def __init__(self, candidate_id: str, message: str) -> None:
        self.candidate_id = candidate_id
        super().__init__(message)


class AuditInitiationError(CandidateGenerationError):
    """Raised when one or more required audit jobs failed to enqueue.

    Candidate persistence is never rolled back for this failure. The
    exception carries ``candidate_id`` and the per-job ``outcomes`` so a
    caller can retry only the failed job type(s) via
    ``enqueue_candidate_audits(..., job_types={...})`` without generating a
    new candidate.
    """

    def __init__(self, candidate_id: str, outcomes: "List[AuditEnqueueOutcome]") -> None:
        self.candidate_id = candidate_id
        self.outcomes = outcomes
        failed = [o for o in outcomes if not o.enqueued]
        detail = "; ".join(f"{o.job_type}: {o.error}" for o in failed)
        super().__init__(
            f"audit initiation failed for candidate {candidate_id}: {detail}"
        )


# ===========================================================================
# Constants
# ===========================================================================

ALLOWED_QUESTION_TYPES = frozenset({"single", "multiple"})
ALLOWED_DIFFICULTIES = frozenset({"easy", "medium", "hard"})
ALLOWED_COGNITIVE_LEVELS = frozenset(
    {"recall", "understanding", "application", "analysis", "judgment"}
)

MIN_OPTIONS = 2
MAX_OPTIONS = 8
MAX_QUESTION_TEXT_LEN = 4000
MAX_OPTION_TEXT_LEN = 1000
MAX_OPTION_LABEL_LEN = 16
MAX_EXPLANATION_LEN = 4000

DEFAULT_RULESET_VERSION = "1.0.0"
DEFAULT_LLM_AUDIT_PROMPT_VERSION = "candidate-audit-v1"
SOURCE_TYPE_GENERATED = "generated"

_AUDIT_JOB_TYPES = ("deterministic_audit", "llm_audit")

# JSON Schema hint passed to providers that support structured output.
GENERATION_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "question_text": {"type": "string"},
        "explanation": {"type": "string"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "option_label": {"type": "string"},
                    "option_text": {"type": "string"},
                    "is_correct": {"type": "boolean"},
                    "display_order": {"type": "integer"},
                },
                "required": ["option_label", "option_text", "is_correct", "display_order"],
            },
        },
        "correct_option_labels": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["question_text", "explanation", "options"],
    "additionalProperties": True,
}


# ===========================================================================
# Request / result data structures
# ===========================================================================

@dataclass
class GenerationRequest:
    """Bounded, server-controlled inputs for one candidate-generation call.

    Only identifiers and generation attributes explicitly supported by the
    current schema are accepted here. The model never controls
    ``certification_exam_name``, ``domain``, ``question_type``, or
    ``select_count`` — those are fixed by the caller before any provider
    call is made.
    """

    certification_exam_name: str
    domain: str
    prompt_template_id: str
    prompt_version: str
    model_name: str
    created_by: str
    source_evidence: Dict[str, Any]
    question_type: str = "single"
    select_count: int = 1
    difficulty: Optional[str] = None
    cognitive_level: Optional[str] = None
    concept_key: Optional[str] = None
    language_code: str = "en"
    source_reference: Optional[str] = None
    generation_request_id: Optional[str] = None
    request_metadata: Optional[Dict[str, Any]] = None


@dataclass
class AuditEnqueueOutcome:
    job_type: str
    job_id: Optional[str] = None
    error: Optional[str] = None

    @property
    def enqueued(self) -> bool:
        return self.job_id is not None and self.error is None


@dataclass
class GenerationResult:
    candidate_id: str
    content_hash: str
    deduplicated: bool
    candidate_row: Dict[str, Any]
    question_snapshot: Dict[str, Any]
    provider_request_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    actual_cost_usd: Optional[float] = None
    audit_outcomes: List[AuditEnqueueOutcome] = field(default_factory=list)


# ===========================================================================
# Small helpers
# ===========================================================================

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_text(value: str) -> str:
    collapsed = re.sub(r"\s+", " ", value.strip())
    return unicodedata.normalize("NFKC", collapsed).casefold()


def _require_nonblank(value: Any, field_name: str) -> str:
    text = _clean_text(value)
    if not text:
        raise CandidateValidationError(f"{field_name} must not be blank")
    return text


def _require_json_serializable(value: Any, field_name: str) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise CandidateValidationError(
            f"{field_name} is not JSON-serializable: {exc}"
        ) from exc


# ===========================================================================
# Request-level validation
# ===========================================================================

def validate_generation_request(request: GenerationRequest) -> None:
    """Validate the bounded caller-supplied request before any provider call.

    Raises ``CandidateValidationError`` on any violation. Performs no I/O.
    """
    _require_nonblank(request.certification_exam_name, "certification_exam_name")
    _require_nonblank(request.domain, "domain")
    _require_nonblank(request.prompt_template_id, "prompt_template_id")
    _require_nonblank(request.prompt_version, "prompt_version")
    _require_nonblank(request.model_name, "model_name")
    _require_nonblank(request.created_by, "created_by")

    if request.question_type not in ALLOWED_QUESTION_TYPES:
        raise CandidateValidationError(
            f"invalid question_type: {request.question_type!r}"
        )

    if (
        not isinstance(request.select_count, int)
        or isinstance(request.select_count, bool)
        or request.select_count <= 0
    ):
        raise CandidateValidationError("select_count must be a positive integer")

    if request.question_type == "single" and request.select_count != 1:
        raise CandidateValidationError(
            "question_type='single' requires select_count == 1"
        )
    if request.question_type == "multiple" and request.select_count < 2:
        raise CandidateValidationError(
            "question_type='multiple' requires select_count >= 2"
        )
    if request.select_count > MAX_OPTIONS - 1:
        raise CandidateValidationError(
            f"select_count must be less than {MAX_OPTIONS}"
        )

    if request.difficulty is not None and request.difficulty not in ALLOWED_DIFFICULTIES:
        raise CandidateValidationError(f"invalid difficulty: {request.difficulty!r}")

    if (
        request.cognitive_level is not None
        and request.cognitive_level not in ALLOWED_COGNITIVE_LEVELS
    ):
        raise CandidateValidationError(
            f"invalid cognitive_level: {request.cognitive_level!r}"
        )

    if not isinstance(request.source_evidence, dict) or not request.source_evidence:
        raise CandidateValidationError(
            "source_evidence must be a non-empty JSON object"
        )
    _require_json_serializable(request.source_evidence, "source_evidence")

    if request.request_metadata is not None:
        if not isinstance(request.request_metadata, dict):
            raise CandidateValidationError("request_metadata must be a JSON object")
        _require_json_serializable(request.request_metadata, "request_metadata")


# ===========================================================================
# Structured generation-output validation
#
# Mirrors the option-shape rules enforced by promote_question_candidate_v1
# (supabase/migrations/20260623233200_v44_promote_question_candidate_rpc.sql):
# non-empty option_label/option_text, positive unique display_order, unique
# labels, correct-option count matching select_count. Adds stricter checks
# (duplicate normalized text, oversized fields) as required by this task.
# ===========================================================================

def validate_generated_payload(raw: Any, *, request: GenerationRequest) -> Dict[str, Any]:
    """Validate raw model output into a normalized candidate structure.

    Raises ``CandidateValidationError`` on any violation. Performs no I/O and
    causes no database mutation.
    """
    if not isinstance(raw, dict):
        raise CandidateValidationError("model output must be a JSON object")

    question_text = _clean_text(raw.get("question_text") or raw.get("stem"))
    if not question_text:
        raise CandidateValidationError("question_text/stem must not be blank")
    if len(question_text) > MAX_QUESTION_TEXT_LEN:
        raise CandidateValidationError(
            f"question_text exceeds {MAX_QUESTION_TEXT_LEN} characters"
        )

    explanation = _clean_text(raw.get("explanation") or raw.get("rationale"))
    if not explanation:
        raise CandidateValidationError("explanation/rationale must not be blank")
    if len(explanation) > MAX_EXPLANATION_LEN:
        raise CandidateValidationError(
            f"explanation exceeds {MAX_EXPLANATION_LEN} characters"
        )

    raw_options = raw.get("options")
    if not isinstance(raw_options, list):
        raise CandidateValidationError("options must be a JSON array")
    if len(raw_options) < MIN_OPTIONS:
        raise CandidateValidationError(
            f"options must contain at least {MIN_OPTIONS} entries, found {len(raw_options)}"
        )
    if len(raw_options) > MAX_OPTIONS:
        raise CandidateValidationError(
            f"options must contain at most {MAX_OPTIONS} entries, found {len(raw_options)}"
        )

    correct_labels_hint = raw.get("correct_option_labels")
    if correct_labels_hint is not None and not isinstance(correct_labels_hint, list):
        raise CandidateValidationError(
            "correct_option_labels must be a JSON array when provided"
        )
    hinted_labels = (
        {str(v).strip() for v in correct_labels_hint} if correct_labels_hint else None
    )

    seen_labels: set = set()
    seen_texts: set = set()
    seen_orders: set = set()
    normalized_options: List[Dict[str, Any]] = []
    correct_count = 0

    for index, opt in enumerate(raw_options):
        if not isinstance(opt, dict):
            raise CandidateValidationError(f"options[{index}] must be a JSON object")

        label = _clean_text(opt.get("option_label"))
        if not label:
            raise CandidateValidationError(
                f"options[{index}].option_label must not be blank"
            )
        if len(label) > MAX_OPTION_LABEL_LEN:
            raise CandidateValidationError(
                f"options[{index}].option_label exceeds {MAX_OPTION_LABEL_LEN} characters"
            )
        if label in seen_labels:
            raise CandidateValidationError(f"duplicate option_label: {label!r}")
        seen_labels.add(label)

        text = _clean_text(opt.get("option_text"))
        if not text:
            raise CandidateValidationError(
                f"options[{index}].option_text must not be blank"
            )
        if len(text) > MAX_OPTION_TEXT_LEN:
            raise CandidateValidationError(
                f"options[{index}].option_text exceeds {MAX_OPTION_TEXT_LEN} characters"
            )
        normalized_text = _normalize_text(text)
        if normalized_text in seen_texts:
            raise CandidateValidationError(
                f"duplicate normalized option text: {text!r}"
            )
        seen_texts.add(normalized_text)

        display_order_raw = opt.get("display_order", index + 1)
        if isinstance(display_order_raw, bool):
            raise CandidateValidationError(
                f"options[{index}].display_order must be an integer"
            )
        try:
            display_order = int(display_order_raw)
        except (TypeError, ValueError):
            raise CandidateValidationError(
                f"options[{index}].display_order must be an integer"
            )
        if display_order <= 0:
            raise CandidateValidationError(
                f"options[{index}].display_order must be > 0"
            )
        if display_order in seen_orders:
            raise CandidateValidationError(f"duplicate display_order: {display_order}")
        seen_orders.add(display_order)

        if hinted_labels is not None:
            is_correct = label in hinted_labels
        else:
            raw_is_correct = opt.get("is_correct")
            if not isinstance(raw_is_correct, bool):
                raise CandidateValidationError(
                    f"options[{index}].is_correct must be a boolean"
                )
            is_correct = raw_is_correct

        if is_correct:
            correct_count += 1

        normalized_options.append(
            {
                "option_label": label,
                "option_text": text,
                "is_correct": bool(is_correct),
                "display_order": display_order,
            }
        )

    if hinted_labels is not None:
        unknown = hinted_labels - seen_labels
        if unknown:
            raise CandidateValidationError(
                "correct_option_labels references unknown option label(s): "
                f"{sorted(unknown)}"
            )

    if correct_count == 0:
        raise CandidateValidationError("no option is marked as correct")

    if request.question_type == "single" and correct_count != 1:
        raise CandidateValidationError(
            f"question_type='single' requires exactly 1 correct option, found {correct_count}"
        )
    if correct_count != request.select_count:
        raise CandidateValidationError(
            "expected exactly "
            f"{request.select_count} correct option(s) for select_count, found {correct_count}"
        )

    normalized_options.sort(key=lambda o: o["display_order"])

    return {
        "question_text": question_text,
        "explanation": explanation,
        "question_type": request.question_type,
        "select_count": request.select_count,
        "options": normalized_options,
        "certification_exam_name": request.certification_exam_name,
        "domain": request.domain,
        "raw_model_output": raw,
    }


# ===========================================================================
# Deterministic content hash — reuses question_candidates.content_hash,
# the schema's existing deduplication mechanism (idx_qc_content_hash).
# ===========================================================================

def compute_candidate_content_hash(validated: Dict[str, Any]) -> str:
    """Return a stable sha256 hash over the normalized candidate content."""
    canonical_options = [
        {
            "option_label": opt["option_label"].strip(),
            "option_text": _normalize_text(opt["option_text"]),
            "is_correct": bool(opt["is_correct"]),
        }
        for opt in sorted(validated["options"], key=lambda o: o["display_order"])
    ]
    canonical = {
        "certification_exam_name": _normalize_text(validated["certification_exam_name"]),
        "question_text": _normalize_text(validated["question_text"]),
        "question_type": validated["question_type"],
        "select_count": validated["select_count"],
        "options": canonical_options,
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ===========================================================================
# Persistence — direct service-role table access.
#
# Consistent with existing service-level modules (not job handlers), e.g.
# workers.embedding_cache.SupabaseEmbeddingCacheRepository and
# workers.quality_audit_pilot, both of which already use client.table()
# directly with a service-role client. No new RPC/migration is introduced.
# ===========================================================================

class QuestionCandidateRepository:
    """Service-role persistence for question_candidates / question_candidate_events."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def certification_domain_exists(self, exam_name: str, domain_name: str) -> bool:
        try:
            result = (
                self._client.table("certification_domains")
                .select("domain_name")
                .eq("exam_name", exam_name)
                .eq("domain_name", domain_name)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise CandidatePersistenceError(
                f"certification_domains lookup failed: {exc}"
            ) from exc
        if getattr(result, "error", None):
            raise CandidatePersistenceError(
                f"certification_domains lookup failed: {result.error}"
            )
        return bool(result.data)

    def get_by_id(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Read-only fetch of one question_candidates row by id.

        Public on purpose: this is the supported way for external callers
        (e.g. a retry runner) to safely rebuild an audit snapshot for an
        already-persisted candidate without reaching into private helpers.
        """
        try:
            result = (
                self._client.table("question_candidates")
                .select("*")
                .eq("id", candidate_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise CandidatePersistenceError(
                f"question_candidates lookup by id failed: {exc}"
            ) from exc
        if getattr(result, "error", None):
            raise CandidatePersistenceError(
                f"question_candidates lookup by id failed: {result.error}"
            )
        rows = result.data or []
        return rows[0] if rows else None

    def find_by_content_hash(self, exam_name: str, content_hash: str) -> Optional[Dict[str, Any]]:
        # NOTE (concurrency): this SELECT-then-INSERT pattern is only safe for
        # a single bounded, single-process caller. See the module docstring
        # "Concurrency limitation" section — content_hash has no unique
        # constraint, so concurrent callers can race past this check.
        try:
            result = (
                self._client.table("question_candidates")
                .select("*")
                .eq("certification_exam_name", exam_name)
                .eq("content_hash", content_hash)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise CandidatePersistenceError(
                f"question_candidates dedup lookup failed: {exc}"
            ) from exc
        if getattr(result, "error", None):
            raise CandidatePersistenceError(
                f"question_candidates dedup lookup failed: {result.error}"
            )
        rows = result.data or []
        return rows[0] if rows else None

    def insert_candidate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self._client.table("question_candidates").insert(payload).execute()
        except Exception as exc:  # noqa: BLE001
            raise CandidatePersistenceError(
                f"question_candidates insert failed: {exc}"
            ) from exc
        if getattr(result, "error", None):
            raise CandidatePersistenceError(
                f"question_candidates insert failed: {result.error}"
            )
        rows = result.data or []
        if not rows:
            raise CandidatePersistenceError(
                "question_candidates insert returned no row"
            )
        return rows[0]

    def insert_event(
        self,
        candidate_id: str,
        *,
        event_type: str,
        event_data: Optional[Dict[str, Any]] = None,
        actor_email: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "candidate_id": candidate_id,
            "event_type": event_type,
            "event_data": event_data or {},
        }
        if actor_email:
            payload["actor_email"] = actor_email
        if reason:
            payload["reason"] = reason
        try:
            result = self._client.table("question_candidate_events").insert(payload).execute()
        except Exception as exc:  # noqa: BLE001
            raise CandidateProvenanceEventError(
                candidate_id,
                f"question_candidate_events insert failed: {exc}",
            ) from exc
        if getattr(result, "error", None):
            raise CandidateProvenanceEventError(
                candidate_id,
                f"question_candidate_events insert failed: {result.error}",
            )
        rows = result.data or []
        return rows[0] if rows else {}


# ===========================================================================
# Candidate payload / snapshot builders
# ===========================================================================

def _build_provenance(
    request: GenerationRequest,
    response: Any,
    content_hash: str,
) -> Dict[str, Any]:
    return {
        "model_name": request.model_name,
        "provider_name": getattr(response, "provider_name", None),
        "prompt_template_id": request.prompt_template_id,
        "prompt_version": request.prompt_version,
        "generation_request_id": request.generation_request_id,
        "requested_by": request.created_by,
        "generated_at": _utc_now_iso(),
        "provider_request_id": getattr(response, "provider_request_id", None),
        "input_tokens": getattr(response, "input_tokens", None),
        "output_tokens": getattr(response, "output_tokens", None),
        "actual_cost_usd": getattr(response, "actual_cost_usd", None),
        "source_evidence": request.source_evidence,
        "content_hash": content_hash,
        "request_metadata": request.request_metadata or {},
    }


def build_candidate_insert_payload(
    validated: Dict[str, Any],
    request: GenerationRequest,
    provenance: Dict[str, Any],
    content_hash: str,
) -> Dict[str, Any]:
    return {
        "certification_exam_name": request.certification_exam_name,
        "target_question_id": None,
        "candidate_status": "draft",
        "question_text": validated["question_text"],
        "explanation": validated["explanation"],
        "category": request.domain,
        "difficulty": request.difficulty,
        "cognitive_level": request.cognitive_level,
        "concept_key": request.concept_key,
        "question_type": validated["question_type"],
        "select_count": validated["select_count"],
        "language_code": request.language_code,
        "source_type": SOURCE_TYPE_GENERATED,
        "source_reference": (
            request.source_reference
            or f"{request.model_name}:{request.prompt_template_id}:{request.prompt_version}"
        ),
        "content_hash": content_hash,
        "candidate_payload": {
            "options": validated["options"],
            "raw_model_output": validated["raw_model_output"],
            "provenance": provenance,
        },
        "created_by": request.created_by,
        "metadata": {
            "domain": request.domain,
            "generation_request_id": request.generation_request_id,
            "prompt_template_id": request.prompt_template_id,
            "prompt_version": request.prompt_version,
            "model_name": request.model_name,
        },
    }


def _question_snapshot_from_validated(validated: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "question_text": validated["question_text"],
        "explanation": validated["explanation"],
        "question_type": validated["question_type"],
        "select_count": validated["select_count"],
        "options": [dict(opt) for opt in validated["options"]],
        "certification_exam_name": validated["certification_exam_name"],
        "domain": validated["domain"],
    }


def question_snapshot_from_candidate_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the immutable audit snapshot from a persisted candidate row.

    Public on purpose: this is the same snapshot shape used at generation
    time (``_question_snapshot_from_validated``) and is safe for external
    callers (e.g. an audit-retry runner) to reuse directly, so nobody needs
    to depend on a private helper to retry audits for an existing candidate.
    """
    payload = row.get("candidate_payload") or {}
    metadata = row.get("metadata") or {}
    return {
        "question_text": row.get("question_text"),
        "explanation": row.get("explanation"),
        "question_type": row.get("question_type"),
        "select_count": row.get("select_count"),
        "options": payload.get("options") or [],
        "certification_exam_name": row.get("certification_exam_name"),
        "domain": metadata.get("domain") or row.get("category"),
    }


def generation_request_from_candidate_row(
    row: Dict[str, Any],
    *,
    created_by: Optional[str] = None,
) -> "GenerationRequest":
    """Rebuild a ``GenerationRequest`` from a persisted candidate row.

    Used only to retry audit initiation (``retry_candidate_audits``) — never
    to regenerate or re-validate model output. All fields are sourced from
    data already persisted on the row itself (``candidate_payload.provenance``
    and ``metadata``), so this never depends on external state.

    ``created_by`` may be overridden to reflect the identity of the caller
    performing the retry (which can differ from the original generator).
    """
    payload = row.get("candidate_payload") or {}
    provenance = payload.get("provenance") or {}
    metadata = row.get("metadata") or {}
    source_evidence = provenance.get("source_evidence")
    if not isinstance(source_evidence, dict) or not source_evidence:
        # Guaranteed non-empty by validate_generation_request at generation
        # time; this fallback only guards against pre-existing/legacy rows.
        source_evidence = {"candidate_id": str(row.get("id"))}
    return GenerationRequest(
        certification_exam_name=row.get("certification_exam_name") or "",
        domain=metadata.get("domain") or row.get("category") or "",
        prompt_template_id=(
            provenance.get("prompt_template_id") or metadata.get("prompt_template_id") or ""
        ),
        prompt_version=provenance.get("prompt_version") or metadata.get("prompt_version") or "",
        model_name=provenance.get("model_name") or metadata.get("model_name") or "",
        created_by=created_by or provenance.get("requested_by") or row.get("created_by") or "",
        source_evidence=source_evidence,
        question_type=row.get("question_type") or "single",
        select_count=row.get("select_count") or 1,
        difficulty=row.get("difficulty"),
        cognitive_level=row.get("cognitive_level"),
        concept_key=row.get("concept_key"),
        language_code=row.get("language_code") or "en",
        source_reference=row.get("source_reference"),
        generation_request_id=(
            provenance.get("generation_request_id") or metadata.get("generation_request_id")
        ),
        request_metadata=provenance.get("request_metadata"),
    )


# ===========================================================================
# Prompt builders
#
# Generation prompt text is intentionally the caller's responsibility here
# (mirrors workers.run_hybrid_audit_pilot, which requires the enqueuer to
# supply system_prompt/user_prompt for llm_audit jobs). No audit *logic*
# (detection, scoring, evidence-contract construction) is duplicated —
# that remains entirely inside workers.llm_audit / workers.audit_orchestration.
# ===========================================================================

def build_generation_prompt(request: GenerationRequest) -> Tuple[str, str]:
    system_prompt = (
        "You are an expert item writer for Salesforce certification exams. "
        "Generate exactly one exam question as strict JSON matching this schema "
        "(return ONLY the JSON object, no prose): "
        + json.dumps(GENERATION_RESPONSE_SCHEMA, sort_keys=True)
    )
    lines = [
        f"Certification: {request.certification_exam_name}",
        f"Domain: {request.domain}",
        f"Question type: {request.question_type} "
        f"(exactly {request.select_count} correct option(s))",
    ]
    if request.difficulty:
        lines.append(f"Difficulty: {request.difficulty}")
    if request.cognitive_level:
        lines.append(f"Cognitive level: {request.cognitive_level}")
    if request.concept_key:
        lines.append(f"Concept: {request.concept_key}")
    lines.append(
        "Provide answer options with unique option_label and option_text "
        "values, sequential display_order starting at 1, and exactly the "
        "required number of options marked is_correct=true."
    )
    return system_prompt, "\n".join(lines)


def build_llm_audit_prompt(
    question_snapshot: Dict[str, Any],
    request: GenerationRequest,
) -> Tuple[str, str]:
    from workers.llm_audit import AUDIT_RESPONSE_SCHEMA  # noqa: PLC0415

    system_prompt = (
        "You are a strict quality auditor for Salesforce certification exam "
        "questions. Review the supplied question for correctness, ambiguity, "
        "duplication, formatting, answer quality, and explanation quality. "
        "Respond ONLY with JSON matching this schema: "
        + json.dumps(AUDIT_RESPONSE_SCHEMA, sort_keys=True)
    )
    user_prompt = (
        f"Certification: {request.certification_exam_name}\n"
        f"Domain: {request.domain}\n"
        "Question snapshot (JSON):\n"
        f"{json.dumps(question_snapshot, sort_keys=True)}\n\n"
        'Identify defects as findings. If none, return {"findings": []}.'
    )
    return system_prompt, user_prompt


# ===========================================================================
# Audit initiation — enqueues existing job types only. No audit logic
# (checks, scoring, evidence contracts) lives here.
# ===========================================================================

def _enqueue_job(
    client: Any,
    *,
    job_type: str,
    payload: Dict[str, Any],
    created_by: str,
    model_name: Optional[str] = None,
    prompt_version: Optional[str] = None,
    priority: int = 100,
    max_attempts: int = 3,
    metadata: Optional[Dict[str, Any]] = None,
) -> AuditEnqueueOutcome:
    params = {
        "p_job_type": job_type,
        "p_payload": payload,
        "p_priority": priority,
        "p_max_attempts": max_attempts,
        "p_available_at": _utc_now_iso(),
        "p_created_by": created_by,
        "p_model_name": model_name,
        "p_prompt_version": prompt_version,
        "p_estimated_cost_usd": None,
        "p_metadata": metadata or {},
    }
    try:
        result = client.rpc("enqueue_background_job_v1", params).execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("enqueue_background_job_v1 raised for job_type=%s", job_type)
        return AuditEnqueueOutcome(job_type=job_type, error=f"{type(exc).__name__}: {exc}")
    if getattr(result, "error", None):
        return AuditEnqueueOutcome(job_type=job_type, error=str(result.error))
    rows = result.data or []
    if not rows:
        return AuditEnqueueOutcome(
            job_type=job_type, error="enqueue_background_job_v1 returned no rows"
        )
    return AuditEnqueueOutcome(job_type=job_type, job_id=str(rows[0].get("job_id")))


def enqueue_candidate_audits(
    client: Any,
    *,
    candidate_id: str,
    question_snapshot: Dict[str, Any],
    request: GenerationRequest,
    content_hash: str,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
    llm_prompt_version: str = DEFAULT_LLM_AUDIT_PROMPT_VERSION,
    job_types: Optional[Sequence[str]] = None,
) -> List[AuditEnqueueOutcome]:
    """Enqueue the existing deterministic_audit and/or llm_audit jobs.

    ``job_types`` defaults to both. Pass a narrower set (e.g.
    ``{"llm_audit"}``) to retry only a previously-failed job type without
    re-enqueuing one that already succeeded and without generating another
    candidate.

    Raises ``AuditInitiationError`` (never returns a false "success") if any
    requested job type fails to enqueue. Never mutates or rolls back the
    candidate row.
    """
    selected = set(job_types) if job_types is not None else set(_AUDIT_JOB_TYPES)
    unknown = selected - set(_AUDIT_JOB_TYPES)
    if unknown:
        raise CandidateValidationError(f"unsupported audit job_types: {sorted(unknown)}")

    shared_metadata = {
        "candidate_content_hash": content_hash,
        "certification_exam_name": request.certification_exam_name,
        "domain": request.domain,
    }

    outcomes: List[AuditEnqueueOutcome] = []

    if "deterministic_audit" in selected:
        outcomes.append(
            _enqueue_job(
                client,
                job_type="deterministic_audit",
                payload={
                    "target_candidate_id": candidate_id,
                    "created_by": request.created_by,
                    "question": question_snapshot,
                    "ruleset_version": ruleset_version,
                    "metadata": shared_metadata,
                },
                created_by=request.created_by,
                metadata=shared_metadata,
            )
        )

    if "llm_audit" in selected:
        system_prompt, user_prompt = build_llm_audit_prompt(question_snapshot, request)
        outcomes.append(
            _enqueue_job(
                client,
                job_type="llm_audit",
                payload={
                    "target_candidate_id": candidate_id,
                    "created_by": request.created_by,
                    "model_name": request.model_name,
                    "prompt_version": llm_prompt_version,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "question": question_snapshot,
                    "metadata": shared_metadata,
                },
                created_by=request.created_by,
                model_name=request.model_name,
                prompt_version=llm_prompt_version,
                metadata=shared_metadata,
            )
        )

    if any(not outcome.enqueued for outcome in outcomes):
        raise AuditInitiationError(candidate_id, outcomes)

    return outcomes


# ===========================================================================
# Audit retry — public, self-contained interface.
#
# This is the ONE supported way to retry audit initiation for a candidate
# that already exists (whether because a previous enqueue attempt failed, or
# because a generation retry deduplicated and skipped audit enqueue by
# default — see generate_and_persist_candidate's initiate_audits_on_duplicate
# parameter below). It never regenerates or duplicates the candidate row: it
# only re-reads the immutable candidate content and re-attempts
# enqueue_candidate_audits, which is the same mechanism used at generation
# time. No parallel/alternate audit mechanism is introduced.
# ===========================================================================

@dataclass
class AuditRetryContext:
    """Everything needed to retry audit initiation for one candidate.

    Rebuilt entirely from the persisted ``question_candidates`` row, so it is
    valid to construct this in a fresh process (e.g. a standalone retry CLI
    invocation) without any in-memory state from the original generation
    call.
    """

    candidate_id: str
    content_hash: str
    question_snapshot: Dict[str, Any]
    request: GenerationRequest


def load_audit_retry_context(
    client: Any,
    candidate_id: str,
    *,
    created_by: Optional[str] = None,
) -> AuditRetryContext:
    """Safely rebuild the exact audit snapshot for an existing candidate.

    Public repository-level entry point for retry callers — no private
    helper functions are required. Raises ``CandidatePersistenceError`` if
    the candidate cannot be found or read.
    """
    repo = QuestionCandidateRepository(client)
    row = repo.get_by_id(candidate_id)
    if row is None:
        raise CandidatePersistenceError(f"question_candidates row not found: {candidate_id}")
    return AuditRetryContext(
        candidate_id=str(row["id"]),
        content_hash=row["content_hash"],
        question_snapshot=question_snapshot_from_candidate_row(row),
        request=generation_request_from_candidate_row(row, created_by=created_by),
    )


def retry_candidate_audits(
    client: Any,
    candidate_id: str,
    *,
    job_types: Optional[Sequence[str]] = None,
    created_by: Optional[str] = None,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
    llm_prompt_version: str = DEFAULT_LLM_AUDIT_PROMPT_VERSION,
) -> List[AuditEnqueueOutcome]:
    """Retry audit initiation for an already-persisted candidate.

    This is the self-contained retry entry point: callers only need a
    ``candidate_id`` (e.g. from a previous ``AuditInitiationError`` or from a
    deduplicated ``GenerationResult``) — the exact question snapshot and
    content hash are re-derived from the persisted row, never regenerated.

    ``job_types`` narrows the retry to specific job type(s) (e.g.
    ``{"llm_audit"}`` after only that one failed). Defaults to both audit
    types.

    Never creates or duplicates a ``question_candidates`` row. Raises
    ``AuditInitiationError`` (never a false "success") if any requested job
    type fails to enqueue, exactly like ``enqueue_candidate_audits``.
    """
    context = load_audit_retry_context(client, candidate_id, created_by=created_by)
    return enqueue_candidate_audits(
        client,
        candidate_id=context.candidate_id,
        question_snapshot=context.question_snapshot,
        request=context.request,
        content_hash=context.content_hash,
        ruleset_version=ruleset_version,
        llm_prompt_version=llm_prompt_version,
        job_types=job_types,
    )


# ===========================================================================
# Top-level orchestrator
# ===========================================================================

def generate_and_persist_candidate(
    client: Any,
    llm_provider: Any,
    request: GenerationRequest,
    *,
    initiate_audits: bool = True,
    initiate_audits_on_duplicate: bool = False,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
    llm_prompt_version: str = DEFAULT_LLM_AUDIT_PROMPT_VERSION,
) -> GenerationResult:
    """Generate, validate, and persist exactly one question candidate.

    On success, enqueues the existing deterministic_audit and llm_audit
    background jobs for the persisted candidate (unless
    ``initiate_audits=False``) — but ONLY when a genuinely new candidate row
    was inserted (``result.deduplicated is False``).

    When an identical candidate is found via content-hash deduplication
    (``result.deduplicated is True``), audits are NOT automatically
    re-enqueued by default. This prevents an idempotent generation retry
    from silently creating duplicate ``audit_runs`` for a candidate that may
    already have audits in flight or completed. Pass
    ``initiate_audits_on_duplicate=True`` to opt into re-enqueueing a full
    set of audits for a deduplicated hit; the recommended path for
    retrying only previously *failed* audits is
    ``retry_candidate_audits(client, result.candidate_id, job_types={...})``.

    Raises
    ------
    CandidateValidationError
        For any malformed request or model output. No database mutation
        occurs in this case.
    CandidatePersistenceError
        If a required database read/write fails.
    AuditInitiationError
        If candidate persistence succeeded but audit enqueue failed. The
        candidate is NOT rolled back; retry via ``retry_candidate_audits``
        (or ``enqueue_candidate_audits`` directly with an in-memory
        snapshot).
    """
    validate_generation_request(request)

    repo = QuestionCandidateRepository(client)
    if not repo.certification_domain_exists(request.certification_exam_name, request.domain):
        raise CandidateValidationError(
            "no active certification_domains row for "
            f"exam_name={request.certification_exam_name!r} domain_name={request.domain!r}"
        )

    system_prompt, user_prompt = build_generation_prompt(request)
    response = llm_provider(
        model_name=request.model_name,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=GENERATION_RESPONSE_SCHEMA,
        metadata={
            "certification_exam_name": request.certification_exam_name,
            "domain": request.domain,
            "prompt_template_id": request.prompt_template_id,
            "generation_request_id": request.generation_request_id,
            SKIP_LEGACY_LLM_AUDIT_VALIDATION_METADATA_KEY: True,
        },
    )

    validated = validate_generated_payload(response.parsed_response, request=request)
    content_hash = compute_candidate_content_hash(validated)

    existing = repo.find_by_content_hash(request.certification_exam_name, content_hash)
    if existing is not None:
        logger.info(
            "candidate generation deduplicated: content_hash=%s candidate_id=%s",
            content_hash,
            existing.get("id"),
        )
        result = GenerationResult(
            candidate_id=str(existing["id"]),
            content_hash=content_hash,
            deduplicated=True,
            candidate_row=existing,
            question_snapshot=question_snapshot_from_candidate_row(existing),
            provider_request_id=getattr(response, "provider_request_id", None),
            input_tokens=getattr(response, "input_tokens", None),
            output_tokens=getattr(response, "output_tokens", None),
            actual_cost_usd=getattr(response, "actual_cost_usd", None),
        )
    else:
        provenance = _build_provenance(request, response, content_hash)
        insert_payload = build_candidate_insert_payload(
            validated, request, provenance, content_hash
        )
        candidate_row = repo.insert_candidate(insert_payload)
        candidate_id = str(candidate_row["id"])

        repo.insert_event(
            candidate_id,
            event_type="created",
            actor_email=request.created_by,
            event_data={
                "content_hash": content_hash,
                "source_type": SOURCE_TYPE_GENERATED,
                "model_name": request.model_name,
            },
        )

        result = GenerationResult(
            candidate_id=candidate_id,
            content_hash=content_hash,
            deduplicated=False,
            candidate_row=candidate_row,
            question_snapshot=_question_snapshot_from_validated(validated),
            provider_request_id=getattr(response, "provider_request_id", None),
            input_tokens=getattr(response, "input_tokens", None),
            output_tokens=getattr(response, "output_tokens", None),
            actual_cost_usd=getattr(response, "actual_cost_usd", None),
        )

    should_initiate_audits = initiate_audits and (
        not result.deduplicated or initiate_audits_on_duplicate
    )
    if should_initiate_audits:
        result.audit_outcomes = enqueue_candidate_audits(
            client,
            candidate_id=result.candidate_id,
            question_snapshot=result.question_snapshot,
            request=request,
            content_hash=result.content_hash,
            ruleset_version=ruleset_version,
            llm_prompt_version=llm_prompt_version,
        )

    return result
