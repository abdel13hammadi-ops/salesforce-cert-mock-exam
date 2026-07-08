"""
V48 AI quality audit worker orchestration.

Drives Pass A/B/C execution through the exact V48 RPC contract.  The database
claim action is the sole source of truth for sequencing, retries, and
completion eligibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from workers.ai_quality_audit_context import (
    load_blind_audit_context,
    load_comparison_audit_context,
)
from workers.ai_quality_audit_prompts import (
    PASS_A_RESPONSE_SCHEMA,
    PASS_B_CORRECTNESS_RESPONSE_SCHEMA,
    PASS_B_RESPONSE_SCHEMA,
    PASS_C_RESPONSE_SCHEMA,
    build_pass_a_prompt,
    build_pass_b_correctness_prompt,
    build_pass_b_prompt,
    build_pass_c_prompt,
)
from workers.ai_quality_audit_schemas import (
    AiQualityAuditValidationError,
    ANSWER_CORRECTNESS_CODES,
    derive_correctness_finding,
    merge_pass_b_findings,
    validate_pass_a_result,
    validate_pass_b_correctness_result,
    validate_pass_b_result,
    validate_pass_c_result,
)
from workers.llm_providers import (
    LlmProviderError,
    LlmResponse,
    SKIP_LEGACY_LLM_AUDIT_VALIDATION_METADATA_KEY,
)
from workers.llm_audit import LlmAuditValidationError

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_RAW_RESPONSE_MAX_LEN = 20000

DEFAULT_WAIT_POLL_SECONDS = 2.0
DEFAULT_MAX_WAIT_POLLS = 60

_SUBSTITUTION_REASON_CODES = frozenset({
    "PASS_A_SCHEMA_INVALID",
    "PASS_B_SCHEMA_INVALID",
})

_NORMAL_DISPUTE_REASON_CODES = frozenset({
    "BLIND_ANSWER_MISMATCH",
    "BLOCKING_DEFECT_PROPOSED",
    "AMBIGUITY_PROPOSED",
    "EVIDENCE_STORED_ANSWER_CONFLICT",
})


class AiQualityAuditWorkerError(RuntimeError):
    """Raised when the worker cannot safely advance an audit run."""


@dataclass(frozen=True)
class AiQualityAuditProviders:
    """Primary and dispute LLM callables matching ``workers.llm_providers``.

    ``pass_b_sub_call_timeout_seconds`` (V60) independently bounds each of
    the two sequential provider calls that compose Pass B (the specialized
    answer-correctness detector, then the narrowed general-quality judge).
    It defaults to ``timeout_seconds`` -- i.e. no behavior change unless a
    caller explicitly configures a distinct Pass B sub-call budget -- since
    Pass B now makes two provider calls per attempt instead of one and a
    single shared ``timeout_seconds`` would otherwise silently double the
    worst-case wall-clock time of one attempt without callers being able to
    tune it independently of Pass A/C.
    """

    primary: Callable[..., LlmResponse]
    dispute: Callable[..., LlmResponse]
    timeout_seconds: Optional[float] = None
    pass_b_sub_call_timeout_seconds: Optional[float] = None


def validate_job_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize an ai_quality audit job payload."""
    if not isinstance(payload, dict):
        raise AiQualityAuditWorkerError("job payload must be a JSON object")

    audit_run_id = _require_non_empty_string(payload.get("audit_run_id"), "audit_run_id")
    question_version_id = _require_non_empty_string(
        payload.get("question_version_id"),
        "question_version_id",
    )
    _validate_uuid(audit_run_id, "audit_run_id")
    _validate_uuid(question_version_id, "question_version_id")

    normalized: Dict[str, Any] = {
        "audit_run_id": audit_run_id.lower(),
        "question_version_id": question_version_id.lower(),
    }
    metadata = payload.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise AiQualityAuditWorkerError("metadata must be a JSON object when provided")
        normalized["metadata"] = dict(metadata)
    return normalized


def process_ai_quality_audit_job(
    client,
    job_payload: Mapping[str, Any],
    providers: AiQualityAuditProviders,
    *,
    worker_id: Optional[str] = None,
    lease_seconds: int = 300,
    schema_version: str = "v48.1",
    heartbeat_fn: Optional[Callable[[], None]] = None,
    wait_poll_seconds: float = DEFAULT_WAIT_POLL_SECONDS,
    max_wait_polls: int = DEFAULT_MAX_WAIT_POLLS,
) -> Dict[str, Any]:
    """Execute one ai_quality audit run to completion via V48 RPCs."""
    validated = validate_job_payload(job_payload)
    audit_run_id = validated["audit_run_id"]
    question_version_id = validated["question_version_id"]
    metadata = validated.get("metadata") or {}

    worker = (worker_id or "ai-quality-audit-worker").strip()
    if not worker:
        raise AiQualityAuditWorkerError("worker_id must not be empty")

    passes_executed: List[str] = []
    completion_shape: Optional[str] = None
    wait_polls = 0
    pass_b_retry_errors: Optional[List[str]] = None

    while True:
        if heartbeat_fn is not None:
            heartbeat_fn()

        claim = _call_rpc(
            client,
            "claim_ai_quality_audit_pass_v1",
            {
                "p_audit_run_id": audit_run_id,
                "p_worker_id": worker,
                "p_lease_seconds": lease_seconds,
            },
        )
        action = str(claim.get("action") or "")
        run_status = str(claim.get("run_status") or "")

        logger.info(
            "ai_quality claim: audit_run_id=%s action=%s pass=%s status=%s",
            audit_run_id,
            action,
            claim.get("pass_code"),
            run_status,
        )

        if action == "RUN_COMPLETE":
            return _build_terminal_summary(
                client,
                audit_run_id=audit_run_id,
                run_status=run_status or "completed",
                passes_executed=passes_executed,
                completion_shape=completion_shape,
            )

        if action == "RUN_INCONCLUSIVE":
            return _build_terminal_summary(
                client,
                audit_run_id=audit_run_id,
                run_status=run_status or "inconclusive",
                passes_executed=passes_executed,
                completion_shape=completion_shape,
            )

        if action == "WAIT":
            wait_polls += 1
            if wait_polls > max_wait_polls:
                raise AiQualityAuditWorkerError(
                    f"audit run {audit_run_id!r} exceeded bounded WAIT polling "
                    f"({max_wait_polls} polls)"
                )
            logger.info(
                "ai_quality waiting on active pass lease: audit_run_id=%s poll=%s/%s",
                audit_run_id,
                wait_polls,
                max_wait_polls,
            )
            time.sleep(wait_poll_seconds)
            continue

        wait_polls = 0

        if action == "NEEDS_DISPUTE_TRIGGER_A":
            _persist_dispute_trigger(
                client,
                audit_run_id=audit_run_id,
                reason_code="PASS_A_SCHEMA_INVALID",
                source_pass_code="A",
                trigger_reason=(
                    "Pass A response failed schema validation after two attempts"
                ),
                finding_refs=[],
            )
            continue

        if action == "NEEDS_DISPUTE_TRIGGER_B":
            _persist_dispute_trigger(
                client,
                audit_run_id=audit_run_id,
                reason_code="PASS_B_SCHEMA_INVALID",
                source_pass_code="B",
                trigger_reason=(
                    "Pass B response failed schema validation after two attempts"
                ),
                finding_refs=[],
            )
            continue

        if action == "SKIP_PASS_C":
            continue

        if action == "RUN_READY_TO_COMPLETE":
            completion_shape = _detect_completion_shape(client, audit_run_id)
            confirmed = build_confirmed_findings_for_completion(
                client,
                audit_run_id=audit_run_id,
                completion_shape=completion_shape,
            )
            complete_row = _call_rpc(
                client,
                "complete_ai_quality_audit_run_v1",
                {
                    "p_audit_run_id": audit_run_id,
                    "p_confirmed_findings": confirmed,
                    "p_metadata": metadata,
                },
            )
            return {
                "audit_run_id": audit_run_id,
                "run_status": str(complete_row.get("run_status", "completed")),
                "finding_count": complete_row.get("finding_count", 0),
                "evidence_count": complete_row.get("evidence_count", 0),
                "passes_executed": passes_executed,
                "completion_shape": completion_shape,
            }

        if action == "EXECUTE_PASS_A":
            _execute_pass_a(
                client,
                audit_run_id=audit_run_id,
                question_version_id=question_version_id,
                claim=claim,
                providers=providers,
                schema_version=schema_version,
            )
            passes_executed.append("A")
            continue

        if action == "EXECUTE_PASS_B":
            pass_b_result, schema_errors = _execute_pass_b(
                client,
                audit_run_id=audit_run_id,
                question_version_id=question_version_id,
                claim=claim,
                providers=providers,
                schema_version=schema_version,
                retry_schema_errors=pass_b_retry_errors,
            )
            passes_executed.append("B")
            if schema_errors:
                pass_b_retry_errors = schema_errors
            elif pass_b_result is not None:
                pass_b_retry_errors = None
                _maybe_persist_blocking_defect_trigger(
                    client,
                    audit_run_id=audit_run_id,
                    pass_b_result=pass_b_result,
                )
            continue

        if action == "EXECUTE_PASS_C":
            _execute_pass_c(
                client,
                audit_run_id=audit_run_id,
                question_version_id=question_version_id,
                claim=claim,
                providers=providers,
                schema_version=schema_version,
            )
            passes_executed.append("C")
            continue

        raise AiQualityAuditWorkerError(
            f"claim_ai_quality_audit_pass_v1 returned unsupported action {action!r}"
        )


def build_confirmed_findings_for_completion(
    client,
    *,
    audit_run_id: str,
    completion_shape: str,
) -> List[Dict[str, Any]]:
    """Convert validated upstream proposals into complete-RPC finding rows."""
    if completion_shape == "NORMAL_NO_DISPUTE":
        # No dispute trigger fired, so Pass B proposed no blocking findings
        # (any blocking proposal would have triggered BLOCKING_DEFECT_PROPOSED
        # and routed the run through Pass C instead). Non-blocking findings
        # were never reviewed by Pass C and must still be preserved rather
        # than silently discarded. Defensively exclude any blocking-materiality
        # proposal here as well: the completion RPC independently rejects
        # blocking findings under NORMAL_NO_DISPUTE, so surfacing this locally
        # avoids failing the whole completion call over a single bad entry.
        pass_b_result = _load_pass_result_json(client, audit_run_id, "B")
        proposed_findings = list((pass_b_result or {}).get("proposed_findings") or [])
        non_blocking = [
            item
            for item in proposed_findings
            if isinstance(item, dict) and item.get("materiality") != "blocking"
        ]
        return [_proposed_to_confirmed_finding(item) for item in non_blocking]

    pass_b_result = _load_pass_result_json(client, audit_run_id, "B")
    pass_c_result = _load_pass_result_json(client, audit_run_id, "C")
    confirmed_refs = list((pass_c_result or {}).get("confirmed_finding_refs") or [])
    if not confirmed_refs:
        return []

    if completion_shape in ("PASS_A_SUBSTITUTION", "PASS_B_SUBSTITUTION"):
        source_findings = list((pass_c_result or {}).get("proposed_findings") or [])
    else:
        source_findings = list((pass_b_result or {}).get("proposed_findings") or [])

    by_ref = {
        str(item.get("finding_ref")): item
        for item in source_findings
        if isinstance(item, dict) and item.get("finding_ref")
    }

    confirmed: List[Dict[str, Any]] = []
    for ref in confirmed_refs:
        proposed = by_ref.get(str(ref))
        if proposed is None:
            raise AiQualityAuditWorkerError(
                f"confirmed finding_ref {ref!r} is missing from upstream proposed_findings"
            )
        confirmed.append(_proposed_to_confirmed_finding(proposed))
    return confirmed


def _execute_pass_a(
    client,
    *,
    audit_run_id: str,
    question_version_id: str,
    claim: Mapping[str, Any],
    providers: AiQualityAuditProviders,
    schema_version: str,
) -> None:
    blind_context = load_blind_audit_context(
        client,
        question_version_id,
        audit_run_id=audit_run_id,
    )
    system_prompt, user_prompt = build_pass_a_prompt(blind_context)
    allowed_labels = {
        option["option_label"] for option in blind_context.get("options") or []
    }
    required_count = int(blind_context["required_selection_count"])
    input_hash = _hash_prompts(system_prompt, user_prompt)

    _invoke_and_record_pass(
        client,
        audit_run_id=audit_run_id,
        pass_code="A",
        claim=claim,
        provider=providers.primary,
        timeout_seconds=providers.timeout_seconds,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=PASS_A_RESPONSE_SCHEMA,
        schema_version=schema_version,
        input_hash=input_hash,
        validate=lambda raw: validate_pass_a_result(
            raw,
            allowed_option_labels=allowed_labels,
            required_selection_count=required_count,
        ),
    )


def _execute_pass_b(
    client,
    *,
    audit_run_id: str,
    question_version_id: str,
    claim: Mapping[str, Any],
    providers: AiQualityAuditProviders,
    schema_version: str,
    retry_schema_errors: Optional[Sequence[str]] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[List[str]]]:
    """Execute the composite V60 Pass B step.

    Internally runs two sequential provider calls -- the specialized
    answer-correctness detector, then the narrowed general-quality judge --
    through the same timeout/retry/error-handling primitive
    (``_call_and_validate``) as every other pass, deterministically merges
    their outputs (``derive_correctness_finding`` + ``merge_pass_b_findings``),
    and persists the result through exactly one ``record_audit_pass_result_v1``
    call, preserving the existing single-persisted-row Pass B contract.
    Both sub-calls' full telemetry is recorded in ``p_metadata`` (a jsonb
    column already present and unused by any other caller), so nothing is
    silently lost even though only one row is written.

    A failure (provider error or schema-invalid output) in *either* sub-call
    fails the whole Pass B attempt using that sub-call's own status/error --
    exactly the same failure contract a single failed Pass B call already
    had -- so the existing bounded-retry-then-substitution machinery
    (``NEEDS_DISPUTE_TRIGGER_B`` / ``PASS_B_SUBSTITUTION``) is reused
    unmodified rather than needing a new failure path.
    """
    comparison_context = load_comparison_audit_context(
        client,
        question_version_id,
        audit_run_id,
    )
    lease_token = claim.get("lease_token")
    model_name = str(claim.get("model_name") or "")
    allowed_labels = {
        option["option_label"] for option in comparison_context.get("options") or []
    }
    required_count = int(comparison_context["required_selection_count"])
    stored_correct = comparison_context.get("stored_correct_option_labels") or []
    frozen_ids = {
        item["chunk_id"] for item in comparison_context.get("frozen_evidence") or []
    }
    sub_call_timeout_seconds = (
        providers.pass_b_sub_call_timeout_seconds
        if providers.pass_b_sub_call_timeout_seconds is not None
        else providers.timeout_seconds
    )

    correctness_system, correctness_user = build_pass_b_correctness_prompt(
        comparison_context,
        retry_schema_errors=retry_schema_errors,
    )
    general_system, general_user = build_pass_b_prompt(
        comparison_context,
        retry_schema_errors=retry_schema_errors,
        exclude_finding_codes=ANSWER_CORRECTNESS_CODES,
    )
    input_hash = _hash_prompts(
        f"{correctness_system}\n===\n{general_system}",
        f"{correctness_user}\n===\n{general_user}",
    )

    correctness_outcome = _call_and_validate(
        provider=providers.primary,
        timeout_seconds=sub_call_timeout_seconds,
        model_name=model_name,
        system_prompt=correctness_system,
        user_prompt=correctness_user,
        response_schema=PASS_B_CORRECTNESS_RESPONSE_SCHEMA,
        metadata={
            "audit_run_id": audit_run_id,
            "pass_code": "B",
            "pass_b_sub_call": "correctness_detector",
        },
        validate=lambda raw: validate_pass_b_correctness_result(
            raw,
            allowed_option_labels=allowed_labels,
            frozen_evidence_chunk_ids=frozen_ids,
        ),
    )

    if correctness_outcome.status is not None:
        response = correctness_outcome.response
        _record_pass_result(
            client,
            audit_run_id=audit_run_id,
            pass_code="B",
            lease_token=lease_token,
            status=correctness_outcome.status,
            schema_version=schema_version,
            input_hash=input_hash,
            raw_response_text=correctness_outcome.raw_response_text,
            schema_validation_errors=correctness_outcome.schema_validation_errors,
            last_error=correctness_outcome.last_error,
            provider_request_id=response.provider_request_id if response else None,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            actual_cost_usd=response.actual_cost_usd if response else None,
            metadata={
                "pass_b_composite": {
                    "correctness_detector": _telemetry_from_outcome(
                        correctness_outcome, label="correctness_detector"
                    ),
                }
            },
        )
        return None, correctness_outcome.errors

    general_outcome = _call_and_validate(
        provider=providers.primary,
        timeout_seconds=sub_call_timeout_seconds,
        model_name=model_name,
        system_prompt=general_system,
        user_prompt=general_user,
        response_schema=PASS_B_RESPONSE_SCHEMA,
        metadata={
            "audit_run_id": audit_run_id,
            "pass_code": "B",
            "pass_b_sub_call": "general_quality_judge",
        },
        validate=lambda raw: validate_pass_b_result(
            raw,
            allowed_option_labels=allowed_labels,
            required_selection_count=required_count,
            frozen_evidence_chunk_ids=frozen_ids,
        ),
    )

    composite_telemetry = {
        "pass_b_composite": {
            "correctness_detector": _telemetry_from_outcome(
                correctness_outcome, label="correctness_detector"
            ),
            "general_quality_judge": _telemetry_from_outcome(
                general_outcome, label="general_quality_judge"
            ),
        }
    }

    if general_outcome.status is not None:
        response = general_outcome.response
        _record_pass_result(
            client,
            audit_run_id=audit_run_id,
            pass_code="B",
            lease_token=lease_token,
            status=general_outcome.status,
            schema_version=schema_version,
            input_hash=input_hash,
            raw_response_text=general_outcome.raw_response_text,
            schema_validation_errors=general_outcome.schema_validation_errors,
            last_error=general_outcome.last_error,
            provider_request_id=response.provider_request_id if response else None,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            actual_cost_usd=response.actual_cost_usd if response else None,
            metadata=composite_telemetry,
        )
        return None, general_outcome.errors

    correctness_finding = derive_correctness_finding(
        correctness_result=correctness_outcome.validated,
        stored_correct_option_labels=stored_correct,
        required_selection_count=required_count,
    )
    merge_result = merge_pass_b_findings(
        correctness_finding=correctness_finding,
        general_proposed_findings=general_outcome.validated.get("proposed_findings") or [],
        frozen_evidence_chunk_ids=frozen_ids,
    )
    if merge_result["dropped_general_findings"]:
        composite_telemetry["pass_b_composite"]["dropped_general_findings"] = (
            merge_result["dropped_general_findings"]
        )

    final_result = {
        "selected_option_labels": general_outcome.validated["selected_option_labels"],
        "proposed_findings": merge_result["proposed_findings"],
    }

    correctness_response = correctness_outcome.response
    general_response = general_outcome.response
    combined_input_tokens = _sum_optional(
        correctness_response.input_tokens if correctness_response else None,
        general_response.input_tokens if general_response else None,
    )
    combined_output_tokens = _sum_optional(
        correctness_response.output_tokens if correctness_response else None,
        general_response.output_tokens if general_response else None,
    )
    combined_cost = _sum_optional(
        correctness_response.actual_cost_usd if correctness_response else None,
        general_response.actual_cost_usd if general_response else None,
    )

    _record_pass_result(
        client,
        audit_run_id=audit_run_id,
        pass_code="B",
        lease_token=lease_token,
        status="completed",
        result_json=final_result,
        schema_version=schema_version,
        input_hash=input_hash,
        raw_response_text=general_outcome.raw_response_text,
        provider_request_id=(
            general_response.provider_request_id if general_response else None
        ),
        input_tokens=combined_input_tokens,
        output_tokens=combined_output_tokens,
        actual_cost_usd=combined_cost,
        metadata=composite_telemetry,
    )
    return final_result, None


def _sum_optional(*values: Optional[float]) -> Optional[float]:
    present = [value for value in values if value is not None]
    if not present:
        return None
    total = sum(present)
    return int(total) if all(isinstance(v, int) for v in present) else total


def _execute_pass_c(
    client,
    *,
    audit_run_id: str,
    question_version_id: str,
    claim: Mapping[str, Any],
    providers: AiQualityAuditProviders,
    schema_version: str,
) -> None:
    comparison_context = load_comparison_audit_context(
        client,
        question_version_id,
        audit_run_id,
    )
    pass_b_result = _load_pass_result_json(client, audit_run_id, "B") or {}
    pass_b_proposed = list(pass_b_result.get("proposed_findings") or [])
    dispute_context = _load_dispute_context(client, audit_run_id, pass_b_proposed)

    system_prompt, user_prompt = build_pass_c_prompt(
        comparison_context,
        pass_b_proposed,
        dispute_context,
    )
    frozen_ids = {
        item["chunk_id"] for item in comparison_context.get("frozen_evidence") or []
    }
    pass_b_refs = {
        str(item.get("finding_ref"))
        for item in pass_b_proposed
        if isinstance(item, dict) and item.get("finding_ref")
    }
    input_hash = _hash_prompts(system_prompt, user_prompt)

    reason_code = dispute_context["reason_code"]
    is_substitution = reason_code in _SUBSTITUTION_REASON_CODES

    def _validate_pass_c(raw: object) -> Dict[str, Any]:
        if is_substitution:
            return validate_pass_c_result(
                raw,
                frozen_evidence_chunk_ids=frozen_ids,
            )
        return validate_pass_c_result(
            raw,
            pass_b_proposed_finding_refs=pass_b_refs,
        )

    _invoke_and_record_pass(
        client,
        audit_run_id=audit_run_id,
        pass_code="C",
        claim=claim,
        provider=providers.dispute,
        timeout_seconds=providers.timeout_seconds,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=PASS_C_RESPONSE_SCHEMA,
        schema_version=schema_version,
        input_hash=input_hash,
        validate=_validate_pass_c,
    )


@dataclass(frozen=True)
class _PassCallOutcome:
    """Result of one provider call + validation attempt (V60).

    ``status`` is ``None`` on success (validated is populated) and one of
    ``"failed"``/``"schema_invalid"`` on failure, mirroring the persisted
    pass-result status vocabulary exactly. Used both by the single-call A/C
    path and by the two internal sub-calls composing Pass B, so every call
    -- specialist or general -- goes through the same timeout, retry-budget,
    and error-handling primitives.
    """

    response: Optional[LlmResponse]
    validated: Optional[Dict[str, Any]]
    status: Optional[str]
    raw_response_text: Optional[str]
    schema_validation_errors: Optional[Dict[str, Any]]
    last_error: Optional[Dict[str, Any]]
    errors: Optional[List[str]]
    duration_ms: Optional[int]


def _call_and_validate(
    *,
    provider: Callable[..., LlmResponse],
    timeout_seconds: Optional[float],
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    response_schema: dict,
    metadata: Optional[Mapping[str, Any]],
    validate: Callable[[object], Dict[str, Any]],
) -> _PassCallOutcome:
    """Call a provider (with the existing timeout enforcement) and validate
    its response, without persisting anything. This is the shared telemetry
    /error-handling primitive underneath both ``_invoke_and_record_pass``
    (single-call Pass A/C) and the two sequential sub-calls that compose
    Pass B (V60): every provider call -- specialist or general -- goes
    through this exact path, so none can bypass bounded timeout, structured
    -output validation, or sanitized error capture.
    """
    started = time.monotonic()
    try:
        response = _call_provider_with_timeout(
            provider,
            timeout_seconds=timeout_seconds,
            model_name=model_name,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
            metadata=metadata,
        )
    except LlmProviderError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return _PassCallOutcome(
            response=None,
            validated=None,
            status="failed",
            raw_response_text=None,
            schema_validation_errors=None,
            last_error={
                "error_code": "LLM_PROVIDER_ERROR",
                "message": str(exc),
                "error_type": type(exc).__name__,
            },
            errors=None,
            duration_ms=duration_ms,
        )
    except LlmAuditValidationError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        errors = [str(exc)]
        return _PassCallOutcome(
            response=None,
            validated=None,
            status="schema_invalid",
            raw_response_text=_safe_truncate_raw_response(
                getattr(exc, "parsed_response", None)
            ),
            schema_validation_errors={"errors": errors},
            last_error=None,
            errors=errors,
            duration_ms=duration_ms,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    raw_text = _safe_truncate_raw_response(response.parsed_response)

    try:
        validated = validate(response.parsed_response)
    except AiQualityAuditValidationError as exc:
        errors = [str(exc)]
        return _PassCallOutcome(
            response=response,
            validated=None,
            status="schema_invalid",
            raw_response_text=raw_text,
            schema_validation_errors={"errors": errors},
            last_error=None,
            errors=errors,
            duration_ms=duration_ms,
        )

    return _PassCallOutcome(
        response=response,
        validated=validated,
        status=None,
        raw_response_text=raw_text,
        schema_validation_errors=None,
        last_error=None,
        errors=None,
        duration_ms=duration_ms,
    )


def _telemetry_from_outcome(outcome: _PassCallOutcome, *, label: str) -> Dict[str, Any]:
    """Sanitized sub-call telemetry record (V60) for embedding in Pass B's
    ``p_metadata`` -- provider/model, request id, duration, token counts,
    status, and a sanitized error, for either the specialist or the general
    judge sub-call.
    """
    response = outcome.response
    record: Dict[str, Any] = {
        "call": label,
        "status": "completed" if outcome.status is None else outcome.status,
        "duration_ms": outcome.duration_ms,
        "provider_request_id": response.provider_request_id if response else None,
        "model_name": response.model_name if response else None,
        "provider_name": response.provider_name if response else None,
        "input_tokens": response.input_tokens if response else None,
        "output_tokens": response.output_tokens if response else None,
        "actual_cost_usd": response.actual_cost_usd if response else None,
    }
    if outcome.last_error is not None:
        record["error"] = outcome.last_error
    elif outcome.errors:
        record["error"] = {"error_code": "SCHEMA_INVALID", "message": outcome.errors[0]}
    return record


def _invoke_and_record_pass(
    client,
    *,
    audit_run_id: str,
    pass_code: str,
    claim: Mapping[str, Any],
    provider: Callable[..., LlmResponse],
    timeout_seconds: Optional[float],
    system_prompt: str,
    user_prompt: str,
    response_schema: dict,
    schema_version: str,
    input_hash: str,
    validate: Callable[[object], Dict[str, Any]],
) -> tuple[Optional[Dict[str, Any]], Optional[List[str]]]:
    lease_token = claim.get("lease_token")
    model_name = str(claim.get("model_name") or "")

    outcome = _call_and_validate(
        provider=provider,
        timeout_seconds=timeout_seconds,
        model_name=model_name,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=response_schema,
        metadata={
            "audit_run_id": audit_run_id,
            "pass_code": pass_code,
        },
        validate=validate,
    )

    response = outcome.response
    if outcome.status is not None:
        _record_pass_result(
            client,
            audit_run_id=audit_run_id,
            pass_code=pass_code,
            lease_token=lease_token,
            status=outcome.status,
            schema_version=schema_version,
            input_hash=input_hash,
            raw_response_text=outcome.raw_response_text,
            schema_validation_errors=outcome.schema_validation_errors,
            last_error=outcome.last_error,
            provider_request_id=response.provider_request_id if response else None,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            actual_cost_usd=response.actual_cost_usd if response else None,
        )
        return None, outcome.errors

    _record_pass_result(
        client,
        audit_run_id=audit_run_id,
        pass_code=pass_code,
        lease_token=lease_token,
        status="completed",
        result_json=outcome.validated,
        schema_version=schema_version,
        input_hash=input_hash,
        raw_response_text=outcome.raw_response_text,
        provider_request_id=response.provider_request_id,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        actual_cost_usd=response.actual_cost_usd,
    )
    return outcome.validated, None


def _call_provider_with_timeout(
    provider: Callable[..., LlmResponse],
    *,
    timeout_seconds: Optional[float],
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    response_schema: dict,
    metadata: Optional[Mapping[str, Any]] = None,
) -> LlmResponse:
    """Invoke a provider, enforcing ``timeout_seconds`` when configured."""
    call_metadata = dict(metadata or {})
    call_metadata[SKIP_LEGACY_LLM_AUDIT_VALIDATION_METADATA_KEY] = True
    if timeout_seconds is not None:
        call_metadata["timeout_seconds"] = timeout_seconds

    kwargs = {
        "model_name": model_name,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "response_schema": response_schema,
        "metadata": call_metadata,
    }

    if timeout_seconds is None or timeout_seconds <= 0:
        return provider(**kwargs)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider, **kwargs)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exc:
            raise LlmProviderError(
                f"provider call timed out after {timeout_seconds} seconds"
            ) from exc


def _maybe_persist_blocking_defect_trigger(
    client,
    *,
    audit_run_id: str,
    pass_b_result: Mapping[str, Any],
) -> None:
    blocking_refs = [
        str(item["finding_ref"])
        for item in pass_b_result.get("proposed_findings") or []
        if isinstance(item, dict)
        and item.get("materiality") == "blocking"
        and item.get("finding_ref")
    ]
    if not blocking_refs:
        return

    _persist_dispute_trigger(
        client,
        audit_run_id=audit_run_id,
        reason_code="BLOCKING_DEFECT_PROPOSED",
        source_pass_code="B",
        trigger_reason="Pass B proposed one or more blocking findings",
        finding_refs=sorted(set(blocking_refs)),
    )


def _persist_dispute_trigger(
    client,
    *,
    audit_run_id: str,
    reason_code: str,
    source_pass_code: str,
    trigger_reason: str,
    finding_refs: Sequence[str],
) -> None:
    _call_rpc(
        client,
        "persist_audit_run_dispute_trigger_v1",
        {
            "p_audit_run_id": audit_run_id,
            "p_reason_code": reason_code,
            "p_source_pass_code": source_pass_code,
            "p_trigger_reason": trigger_reason,
            "p_finding_refs": list(finding_refs),
        },
    )


def _load_dispute_context(
    client,
    audit_run_id: str,
    pass_b_proposed_findings: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    rows = _call_table(
        client,
        table_name="audit_run_dispute_triggers",
        build_query=lambda table: (
            table.select(
                "reason_code, source_pass_code, trigger_reason, finding_refs"
            )
            .eq("audit_run_id", audit_run_id)
            .limit(1)
        ),
    )
    if not rows:
        raise AiQualityAuditWorkerError(
            f"dispute trigger for audit run {audit_run_id!r} was not found"
        )

    row = rows[0]
    reason_code = str(row.get("reason_code") or "").strip()
    finding_refs = list(row.get("finding_refs") or [])
    trigger_reason = str(row.get("trigger_reason") or "").strip()

    if reason_code in _SUBSTITUTION_REASON_CODES:
        expected_resolution_type = {
            "PASS_A_SCHEMA_INVALID": "PASS_A_SUBSTITUTION",
            "PASS_B_SCHEMA_INVALID": "PASS_B_SUBSTITUTION",
        }[reason_code]
        allowed_refs = [
            str(item.get("finding_ref"))
            for item in pass_b_proposed_findings
            if item.get("finding_ref")
        ]
    elif reason_code in _NORMAL_DISPUTE_REASON_CODES:
        expected_resolution_type = "NORMAL_DISPUTE"
        allowed_refs = [str(ref) for ref in finding_refs if str(ref).strip()]
        if not allowed_refs:
            allowed_refs = [
                str(item.get("finding_ref"))
                for item in pass_b_proposed_findings
                if item.get("finding_ref")
            ]
    else:
        raise AiQualityAuditWorkerError(
            f"unsupported dispute trigger reason_code {reason_code!r}"
        )

    return {
        "reason_code": reason_code,
        "finding_refs": finding_refs,
        "trigger_reason": trigger_reason,
        "resolution_hints": {
            "expected_resolution_type": expected_resolution_type,
            "expected_substituted_for_passes": _substituted_for_passes(
                expected_resolution_type
            ),
            "allowed_confirmed_finding_refs": allowed_refs,
            "trigger_reason": trigger_reason,
        },
    }


def _detect_completion_shape(client, audit_run_id: str) -> str:
    pass_a = _load_pass_row(client, audit_run_id, "A")
    pass_b = _load_pass_row(client, audit_run_id, "B")
    pass_c = _load_pass_row(client, audit_run_id, "C")
    trigger = _load_dispute_trigger_row(client, audit_run_id)

    pass_a_status = str((pass_a or {}).get("status") or "")
    pass_b_status = str((pass_b or {}).get("status") or "")
    pass_c_status = str((pass_c or {}).get("status") or "")

    if (
        pass_a_status == "completed"
        and pass_b_status == "completed"
        and pass_c_status == "skipped"
        and trigger is None
    ):
        return "NORMAL_NO_DISPUTE"

    if trigger is None:
        raise AiQualityAuditWorkerError(
            f"audit run {audit_run_id!r} is ready to complete but has no dispute trigger"
        )

    reason_code = str(trigger.get("reason_code") or "")
    pass_c_result = (pass_c or {}).get("result_json") or {}
    resolution_type = str(pass_c_result.get("resolution_type") or "")

    if reason_code == "PASS_A_SCHEMA_INVALID":
        return "PASS_A_SUBSTITUTION"
    if reason_code == "PASS_B_SCHEMA_INVALID":
        return "PASS_B_SUBSTITUTION"
    if reason_code in _NORMAL_DISPUTE_REASON_CODES and resolution_type == "NORMAL_DISPUTE":
        return "NORMAL_DISPUTE"

    raise AiQualityAuditWorkerError(
        f"audit run {audit_run_id!r} has unsupported completion shape "
        f"(trigger={reason_code!r}, resolution_type={resolution_type!r})"
    )


def _proposed_to_confirmed_finding(proposed: Mapping[str, Any]) -> Dict[str, Any]:
    evidence = [
        {
            "resource_chunk_id": chunk_id,
            "evidence_role": "supporting",
        }
        for chunk_id in proposed.get("evidence_chunk_ids") or []
    ]
    confirmed = {
        "finding_ref": proposed["finding_ref"],
        "finding_code": proposed["finding_code"],
        "finding_type": proposed["finding_type"],
        "severity": proposed["severity"],
        "materiality": proposed["materiality"],
        "title": proposed["title"],
        "description": proposed["description"],
        "metadata": dict(proposed.get("metadata") or {}),
        "evidence": evidence,
    }
    return confirmed


def _record_pass_result(
    client,
    *,
    audit_run_id: str,
    pass_code: str,
    lease_token: Any,
    status: str,
    schema_version: str,
    input_hash: str,
    result_json: Optional[dict] = None,
    raw_response_text: Optional[str] = None,
    schema_validation_errors: Optional[dict] = None,
    last_error: Optional[dict] = None,
    provider_request_id: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    actual_cost_usd: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    params = {
        "p_audit_run_id": audit_run_id,
        "p_pass_code": pass_code,
        "p_lease_token": lease_token,
        "p_status": status,
        "p_result_json": result_json,
        "p_raw_response_text": raw_response_text,
        "p_schema_validation_errors": schema_validation_errors,
        "p_last_error": last_error,
        "p_provider_request_id": provider_request_id,
        "p_input_tokens": input_tokens,
        "p_output_tokens": output_tokens,
        "p_actual_cost_usd": actual_cost_usd,
        "p_schema_version": schema_version,
        "p_input_hash": input_hash,
        "p_metadata": metadata or {},
    }
    try:
        _call_rpc(client, "record_audit_pass_result_v1", params)
    except RuntimeError as exc:
        message = str(exc)
        if "lease token mismatch" in message.lower() or "stale token" in message.lower():
            raise AiQualityAuditWorkerError(message) from exc
        raise


def _build_terminal_summary(
    client,
    *,
    audit_run_id: str,
    run_status: str,
    passes_executed: List[str],
    completion_shape: Optional[str],
) -> Dict[str, Any]:
    finding_count = 0
    evidence_count = 0
    if run_status == "completed":
        findings = _call_table(
            client,
            table_name="audit_findings",
            build_query=lambda table: (
                table.select("id").eq("audit_run_id", audit_run_id)
            ),
        )
        finding_count = len(findings)
        if finding_count:
            finding_ids = [row["id"] for row in findings]
            evidence_rows = _call_table(
                client,
                table_name="audit_finding_evidence",
                build_query=lambda table: (
                    table.select("id").in_("finding_id", finding_ids)
                ),
            )
            evidence_count = len(evidence_rows)

    return {
        "audit_run_id": audit_run_id,
        "run_status": run_status,
        "finding_count": finding_count,
        "evidence_count": evidence_count,
        "passes_executed": passes_executed,
        "completion_shape": completion_shape,
    }


def _load_pass_result_json(client, audit_run_id: str, pass_code: str) -> Optional[dict]:
    row = _load_pass_row(client, audit_run_id, pass_code)
    if row is None:
        return None
    result_json = row.get("result_json")
    return result_json if isinstance(result_json, dict) else None


def _load_pass_row(client, audit_run_id: str, pass_code: str) -> Optional[dict]:
    rows = _call_table(
        client,
        table_name="audit_run_pass_results",
        build_query=lambda table: (
            table.select("pass_code, status, result_json, attempt_count")
            .eq("audit_run_id", audit_run_id)
            .eq("pass_code", pass_code)
            .limit(1)
        ),
    )
    return rows[0] if rows else None


def _load_dispute_trigger_row(client, audit_run_id: str) -> Optional[dict]:
    rows = _call_table(
        client,
        table_name="audit_run_dispute_triggers",
        build_query=lambda table: (
            table.select("reason_code, source_pass_code, trigger_reason, finding_refs")
            .eq("audit_run_id", audit_run_id)
            .limit(1)
        ),
    )
    return rows[0] if rows else None


def _substituted_for_passes(resolution_type: str) -> List[str]:
    if resolution_type == "PASS_A_SUBSTITUTION":
        return ["A", "B"]
    if resolution_type == "PASS_B_SUBSTITUTION":
        return ["B"]
    return []


def _safe_truncate_raw_response(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        text = raw
    else:
        text = json.dumps(raw, separators=(",", ":"), ensure_ascii=False)
    if len(text) <= _RAW_RESPONSE_MAX_LEN:
        return text
    return text[:_RAW_RESPONSE_MAX_LEN]


def _hash_prompts(system_prompt: str, user_prompt: str) -> str:
    payload = f"{system_prompt}\n---\n{user_prompt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _call_rpc(client, name: str, params: dict) -> dict:
    """Invoke a Supabase RPC and return the first result row."""
    result = client.rpc(name, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {name!r} failed: {result.error}")
    rows = result.data or []
    if not rows:
        raise RuntimeError(f"RPC {name!r} returned no rows")
    return rows[0]


def _call_table(client, *, table_name: str, build_query):
    result = build_query(client.table(table_name)).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"table read {table_name!r} failed: {result.error}")
    return result.data or []


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AiQualityAuditWorkerError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise AiQualityAuditWorkerError(f"{field_name} must not be empty")
    return text


def _validate_uuid(value: str, field_name: str) -> None:
    if not _UUID_RE.match(value):
        raise AiQualityAuditWorkerError(
            f"{field_name} must be a UUID string, got {value!r}"
        )
