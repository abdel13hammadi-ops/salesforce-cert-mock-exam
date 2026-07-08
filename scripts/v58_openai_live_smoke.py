#!/usr/bin/env python3
"""
V58 Day 8 — one-shot live OpenAI Pass A/B/C compatibility smoke test.

Makes exactly three real, billed calls through ``OpenAIAuditProvider`` (one
Pass A, one Pass B, one Pass C invocation) against a single synthetic,
certification-neutral question. This validates live Responses API
compatibility, structured-output compatibility, response extraction, real
Pass A/B/C schema validation, token accounting, and request-id/model
provenance mapping.

This is a transport/compatibility smoke test, not a quality benchmark. It
does not compute any benchmark metric, does not touch any database or
benchmark fixture, and does not use SME-reviewed ground truth.

Safety
------
Refuses to run (zero provider calls, exit code 2) unless the environment
explicitly sets ``CERTBOUND_ALLOW_LIVE_AI_TEST=1`` and a non-empty
``CERTBOUND_OPENAI_API_KEY`` is present. The API key value is never printed,
logged, hashed, or included in any error message or artifact.

Cost controls
-------------
Before constructing the provider, safe smoke defaults are applied via
``os.environ.setdefault`` (only when the corresponding variable is not
already set by the caller):

    CERTBOUND_OPENAI_MODEL             = gpt-5.5
    CERTBOUND_OPENAI_REASONING_EFFORT  = low
    CERTBOUND_OPENAI_MAX_RETRIES       = 0
    CERTBOUND_OPENAI_MAX_OUTPUT_TOKENS = 2000
    CERTBOUND_OPENAI_TIMEOUT_SECONDS   = 120

``MAX_RETRIES=0`` is intentional: a transient failure must not generate
additional paid calls during this smoke test. Retry behavior already has
dedicated offline unit test coverage (``tests/test_openai_provider.py``).

Output and artifact
--------------------
Prints a sanitized per-pass summary (provider, model, request id, token
counts, cost, validated result) to stdout. On completion (success or first
failure) writes a sanitized JSON artifact to
``.local/v58_openai_smoke/<UTC_TIMESTAMP>/result.json``. Nothing written or
printed ever includes the API key, authorization headers, raw SDK/HTTP
objects, full prompt text, or stack traces.

On the first failed pass, the script stops immediately (later passes are
never invoked), writes a best-effort sanitized failure artifact, and exits
nonzero with the failure classified as one of: configuration, provider
request, refusal, incomplete response, response parsing, Pass A validation,
Pass B validation, Pass C validation, artifact writing, or unexpected
sanitized failure.

For "provider request"/"configuration" failures, ``OpenAIAuditProvider``
already embeds a compact, sanitized diagnostic string (HTTP status, error
type/code/param, request id, and a safe message when available -- see
``workers.openai_provider.describe_openai_error``) directly in the
``LlmProviderError`` message. This script re-parses that same bounded string
(never the raw SDK exception) to also store those fields separately in the
artifact under ``failure_diagnostics`` and print them individually.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

ENV_ALLOW_LIVE = "CERTBOUND_ALLOW_LIVE_AI_TEST"
ENV_API_KEY = "CERTBOUND_OPENAI_API_KEY"

# Applied via os.environ.setdefault() only -- never overrides a value the
# caller already set.
SMOKE_DEFAULTS: Dict[str, str] = {
    "CERTBOUND_OPENAI_MODEL": "gpt-5.5",
    "CERTBOUND_OPENAI_REASONING_EFFORT": "low",
    "CERTBOUND_OPENAI_MAX_RETRIES": "0",
    "CERTBOUND_OPENAI_MAX_OUTPUT_TOKENS": "2000",
    "CERTBOUND_OPENAI_TIMEOUT_SECONDS": "120",
}

ARTIFACT_ROOT = _REPO_ROOT / ".local" / "v58_openai_smoke"


class SmokeTestFailure(Exception):
    """Raised for any classified smoke-test failure. Message must already be sanitized."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def _check_safety_gate() -> None:
    """Refuse to proceed unless explicitly authorized. Makes zero provider calls."""
    if os.environ.get(ENV_ALLOW_LIVE, "").strip() != "1":
        print(
            f"Refusing to run: {ENV_ALLOW_LIVE} must be set to exactly '1' to "
            "authorize a live OpenAI smoke test that makes real, billed API "
            f"calls. Set {ENV_ALLOW_LIVE}=1 explicitly in this shell and re-run "
            "to proceed. No provider was constructed and no network call was made.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not os.environ.get(ENV_API_KEY, "").strip():
        print(
            f"Refusing to run: {ENV_API_KEY} is not set. This script never "
            "prints, logs, hashes, or infers the key; set it in this shell "
            "session before running. No provider was constructed and no "
            "network call was made.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _apply_smoke_defaults() -> None:
    for name, value in SMOKE_DEFAULTS.items():
        os.environ.setdefault(name, value)


def _sanitize_message(message: str) -> str:
    """Best-effort redaction of the API key value if it ever appears in text."""
    api_key = os.environ.get(ENV_API_KEY, "")
    if api_key and api_key in message:
        message = message.replace(api_key, "[REDACTED]")
    return message


def _classify_provider_error(message: str) -> str:
    """Map an LlmProviderError message (openai_provider.py's own wording) to a
    failure category. Matches the exact phrasing used in workers/openai_provider.py."""
    lower = message.lower()
    if "refused" in lower:
        return "refusal"
    if "incomplete" in lower:
        return "incomplete response"
    if "not valid json" in lower or "must be a json object" in lower or "no output text" in lower:
        return "response parsing"
    return "provider request"


# Matches one ``name=value`` diagnostic field emitted by
# workers.openai_provider.describe_openai_error(), where every field except
# the (always-last) "message" field is comma-terminated and comma-free. The
# diagnostics block is embedded in a larger sentence (e.g. "OpenAI request
# failed: status=400, ..." or "... check X (status=401, ...)"), so field
# names are matched by a not-preceded-by-word-character lookbehind rather
# than requiring a specific literal prefix or string start.
_DIAGNOSTIC_FIELD_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(status|type|code|param|request_id)=([^,]*)")

_DIAGNOSTIC_FIELD_TO_ARTIFACT_KEY = {
    "status": "http_status",
    "type": "provider_error_type",
    "code": "provider_error_code",
    "param": "provider_error_param",
    "request_id": "provider_request_id",
}


def _parse_openai_diagnostic_fields(message: str) -> Dict[str, Optional[str]]:
    """Best-effort extraction of the structured fields already embedded (by
    ``workers.openai_provider.describe_openai_error``) in a sanitized
    ``LlmProviderError`` message, for separate artifact storage.

    This re-parses the same bounded, already-sanitized diagnostic string
    that is printed and stored anyway -- it never touches the original SDK
    exception, request/response objects, or any additional data. Returns all
    ``None`` values when the message does not contain this shape (e.g.
    configuration or validation failures), which is safe and expected.
    """
    fields: Dict[str, Optional[str]] = {
        "http_status": None,
        "provider_error_type": None,
        "provider_error_code": None,
        "provider_error_param": None,
        "provider_request_id": None,
    }
    if not message:
        return fields
    # The "message" field is always last and may itself contain commas;
    # only scan the portion before it for the fixed comma-free fields.
    prefix = message.split("message=", 1)[0]
    for match in _DIAGNOSTIC_FIELD_PATTERN.finditer(prefix):
        key, value = match.group(1), match.group(2).strip()
        if value:
            fields[_DIAGNOSTIC_FIELD_TO_ARTIFACT_KEY[key]] = value
    return fields


# ---------------------------------------------------------------------------
# Synthetic, certification-neutral scenario (publication-safety theme)
# ---------------------------------------------------------------------------

def _build_synthetic_scenario() -> Dict[str, Any]:
    """One synthetic single-select question with four options, a concise
    explanation, and two synthetic evidence chunks. Not copied from any
    fixture; no proprietary certification content; no personal data."""
    chunk_id_1 = str(uuid.uuid4())
    chunk_id_2 = str(uuid.uuid4())

    options = [
        {
            "option_label": "A",
            "option_text": (
                "Keep the question published as-is, since an automated review "
                "already looked at it once."
            ),
            "display_order": 1,
            "is_correct": False,
        },
        {
            "option_label": "B",
            "option_text": (
                "Block the question from publication and route it to mandatory "
                "human review until the blocking finding is resolved."
            ),
            "display_order": 2,
            "is_correct": True,
        },
        {
            "option_label": "C",
            "option_text": (
                "Ignore the unresolved finding and proceed, since automated "
                "review only flagged it once."
            ),
            "display_order": 3,
            "is_correct": False,
        },
        {
            "option_label": "D",
            "option_text": (
                "Silently remove the evidence chunk that triggered the finding "
                "so it no longer applies."
            ),
            "display_order": 4,
            "is_correct": False,
        },
    ]

    return {
        "certification_exam_name": "Synthetic Smoke-Test Certification (non-production)",
        "domain_name": "Content Governance & Publication Safety (synthetic)",
        "question_type": "single",
        "required_selection_count": 1,
        "question_text": (
            "During a certification content-quality audit, an automated review "
            "pipeline reports an unresolved blocking finding against a "
            "published exam question after a dispute step failed to reach "
            "consensus. Under standard content-governance policy, what is the "
            "correct next action for this question?"
        ),
        "options": options,
        "stored_correct_option_labels": ["B"],
        "explanation": (
            "Content-governance policy requires that any unresolved blocking "
            "audit finding halts publication and mandates human review; "
            "automated systems must never auto-approve a question or silently "
            "discard a blocking signal."
        ),
        "frozen_evidence": [
            {
                "rank": 1,
                "chunk_id": chunk_id_1,
                "title": "Content Governance Policy, Section 4.2 (synthetic)",
                "source_label": "Synthetic Governance Handbook",
                "chunk_text": (
                    "An audit run classified as inconclusive due to an "
                    "unresolved blocking dispute must remain unpublished until "
                    "a qualified human reviewer resolves the finding. "
                    "Automated resolution alone is not sufficient to clear a "
                    "blocking classification."
                ),
            },
            {
                "rank": 2,
                "chunk_id": chunk_id_2,
                "title": "Content Governance Policy, Section 4.5 (synthetic)",
                "source_label": "Synthetic Governance Handbook",
                "chunk_text": (
                    "Evidence or findings associated with a blocking audit "
                    "trigger must not be deleted or suppressed; they must be "
                    "preserved for human review and audit traceability."
                ),
            },
        ],
    }


# ---------------------------------------------------------------------------
# Pass execution
#
# Real production prompt builders (build_pass_a_prompt / build_pass_b_prompt /
# build_pass_c_prompt) and real validators (validate_pass_a/b/c_result) are
# used directly -- see "Prompt-builder decision" in the completion report.
# All dependencies are passed in explicitly (imported once in main(), after
# the safety gate) rather than imported at module scope, so an unauthorized
# invocation never touches provider/config/prompt code at all.
# ---------------------------------------------------------------------------

def _invoke_provider(
    provider: Any,
    *,
    pass_code: str,
    run_id: str,
    system_prompt: str,
    user_prompt: str,
    response_schema: dict,
    llm_provider_error_cls: type,
) -> Any:
    try:
        return provider(
            model_name="",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
            metadata={
                "skip_legacy_llm_audit_validation": True,
                "smoke_test_run_id": run_id,
                "pass_code": pass_code,
            },
        )
    except llm_provider_error_cls as exc:
        message = _sanitize_message(str(exc))
        raise SmokeTestFailure(_classify_provider_error(message), message) from exc


def _run_pass_a(
    provider: Any,
    scenario: Dict[str, Any],
    *,
    run_id: str,
    build_pass_a_prompt: Callable,
    pass_a_schema: dict,
    validate_pass_a_result: Callable,
    validation_error_cls: type,
    llm_provider_error_cls: type,
) -> Dict[str, Any]:
    blind_context = {
        "certification_exam_name": scenario["certification_exam_name"],
        "domain_name": scenario["domain_name"],
        "question_type": scenario["question_type"],
        "required_selection_count": scenario["required_selection_count"],
        "question_text": scenario["question_text"],
        "options": scenario["options"],
    }
    system_prompt, user_prompt = build_pass_a_prompt(blind_context)

    llm_response = _invoke_provider(
        provider,
        pass_code="A",
        run_id=run_id,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=pass_a_schema,
        llm_provider_error_cls=llm_provider_error_cls,
    )

    parsed = llm_response.parsed_response
    if not isinstance(parsed, dict):
        raise SmokeTestFailure(
            "response parsing",
            f"Pass A parsed_response was not a JSON object (got {type(parsed).__name__})",
        )

    allowed_labels = {opt["option_label"] for opt in scenario["options"]}
    try:
        validated = validate_pass_a_result(
            parsed,
            allowed_option_labels=allowed_labels,
            required_selection_count=scenario["required_selection_count"],
        )
    except validation_error_cls as exc:
        raise SmokeTestFailure("Pass A validation", _sanitize_message(str(exc))) from exc

    return {"llm_response": llm_response, "validated": validated}


def _run_pass_b(
    provider: Any,
    scenario: Dict[str, Any],
    pass_a_result: Dict[str, Any],
    *,
    run_id: str,
    build_pass_b_prompt: Callable,
    pass_b_schema: dict,
    validate_pass_b_result: Callable,
    validation_error_cls: type,
    llm_provider_error_cls: type,
) -> Dict[str, Any]:
    comparison_context = {
        "certification_exam_name": scenario["certification_exam_name"],
        "domain_name": scenario["domain_name"],
        "question_type": scenario["question_type"],
        "required_selection_count": scenario["required_selection_count"],
        "question_text": scenario["question_text"],
        "options": scenario["options"],
        "pass_a_selected_option_labels": pass_a_result["validated"]["selected_option_labels"],
        "stored_correct_option_labels": scenario["stored_correct_option_labels"],
        "explanation": scenario["explanation"],
        "frozen_evidence": scenario["frozen_evidence"],
    }
    system_prompt, user_prompt = build_pass_b_prompt(comparison_context)

    llm_response = _invoke_provider(
        provider,
        pass_code="B",
        run_id=run_id,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=pass_b_schema,
        llm_provider_error_cls=llm_provider_error_cls,
    )

    parsed = llm_response.parsed_response
    if not isinstance(parsed, dict):
        raise SmokeTestFailure(
            "response parsing",
            f"Pass B parsed_response was not a JSON object (got {type(parsed).__name__})",
        )

    allowed_labels = {opt["option_label"] for opt in scenario["options"]}
    frozen_chunk_ids = {item["chunk_id"] for item in scenario["frozen_evidence"]}
    try:
        validated = validate_pass_b_result(
            parsed,
            allowed_option_labels=allowed_labels,
            required_selection_count=scenario["required_selection_count"],
            frozen_evidence_chunk_ids=frozen_chunk_ids,
        )
    except validation_error_cls as exc:
        raise SmokeTestFailure("Pass B validation", _sanitize_message(str(exc))) from exc

    return {
        "llm_response": llm_response,
        "validated": validated,
        "comparison_context": comparison_context,
    }


def _run_pass_c(
    provider: Any,
    pass_b_result: Dict[str, Any],
    *,
    run_id: str,
    build_pass_c_prompt: Callable,
    pass_c_schema: dict,
    validate_pass_c_result: Callable,
    validation_error_cls: type,
    llm_provider_error_cls: type,
) -> Dict[str, Any]:
    proposed_findings = list(pass_b_result["validated"].get("proposed_findings") or [])
    finding_refs = [
        str(item.get("finding_ref")) for item in proposed_findings if item.get("finding_ref")
    ]

    dispute_context = {
        "reason_code": "SMOKE_TEST_DISPUTE_REVIEW",
        "finding_refs": finding_refs,
        "trigger_reason": (
            "Synthetic smoke-test dispute review of Pass B output, used only "
            "to exercise the real Pass C schema and validator."
        ),
        "resolution_hints": {
            "expected_resolution_type": "NORMAL_DISPUTE",
            "expected_substituted_for_passes": [],
            "allowed_confirmed_finding_refs": finding_refs,
            "trigger_reason": (
                "Synthetic smoke-test dispute review of Pass B output, used "
                "only to exercise the real Pass C schema and validator."
            ),
        },
    }

    system_prompt, user_prompt = build_pass_c_prompt(
        pass_b_result["comparison_context"],
        proposed_findings,
        dispute_context,
    )

    llm_response = _invoke_provider(
        provider,
        pass_code="C",
        run_id=run_id,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=pass_c_schema,
        llm_provider_error_cls=llm_provider_error_cls,
    )

    parsed = llm_response.parsed_response
    if not isinstance(parsed, dict):
        raise SmokeTestFailure(
            "response parsing",
            f"Pass C parsed_response was not a JSON object (got {type(parsed).__name__})",
        )

    try:
        validated = validate_pass_c_result(
            parsed,
            pass_b_proposed_finding_refs=set(finding_refs),
        )
    except validation_error_cls as exc:
        raise SmokeTestFailure("Pass C validation", _sanitize_message(str(exc))) from exc

    return {"llm_response": llm_response, "validated": validated}


# ---------------------------------------------------------------------------
# Output / artifact
# ---------------------------------------------------------------------------

def _summarize_pass(pass_code: str, pass_result: Dict[str, Any]) -> Dict[str, Any]:
    llm_response = pass_result["llm_response"]
    return {
        "pass_code": pass_code,
        "provider_name": llm_response.provider_name,
        "model_name": llm_response.model_name,
        "provider_request_id": llm_response.provider_request_id,
        "input_tokens": llm_response.input_tokens,
        "output_tokens": llm_response.output_tokens,
        "actual_cost_usd": llm_response.actual_cost_usd,
        "validated_result": pass_result["validated"],
    }


def _print_pass_summary(pass_code: str, pass_result: Dict[str, Any]) -> Dict[str, Any]:
    summary = _summarize_pass(pass_code, pass_result)
    cost = summary["actual_cost_usd"]
    print(f"Pass {pass_code}:")
    print(f"  provider_name:        {summary['provider_name']}")
    print(f"  model_name:           {summary['model_name']}")
    print(f"  provider_request_id:  {summary['provider_request_id']}")
    print(f"  input_tokens:         {summary['input_tokens']}")
    print(f"  output_tokens:        {summary['output_tokens']}")
    print(f"  actual_cost_usd:      {cost if cost is not None else 'n/a (pricing not configured)'}")
    print(f"  validated_result:     {json.dumps(summary['validated_result'], sort_keys=True)}")
    print()
    return summary


def _write_artifact(started_at: datetime, artifact: Dict[str, Any]) -> Path:
    timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    out_dir = ARTIFACT_ROOT / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "result.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(artifact, fh, indent=2, sort_keys=True, default=str)
    return out_path


def _write_artifact_best_effort(started_at: datetime, artifact: Dict[str, Any]) -> Optional[Path]:
    try:
        return _write_artifact(started_at, artifact)
    except OSError as exc:
        print(
            f"(failed to write sanitized artifact: {_sanitize_message(str(exc))})",
            file=sys.stderr,
        )
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    _check_safety_gate()
    _apply_smoke_defaults()

    # Deferred imports: only reached once the safety gate has passed, so an
    # unauthorized invocation never touches provider/config/prompt code.
    from workers.ai_quality_audit_prompts import (  # noqa: E402
        PASS_A_RESPONSE_SCHEMA,
        PASS_B_RESPONSE_SCHEMA,
        PASS_C_RESPONSE_SCHEMA,
        build_pass_a_prompt,
        build_pass_b_prompt,
        build_pass_c_prompt,
    )
    from workers.ai_quality_audit_schemas import (  # noqa: E402
        AiQualityAuditValidationError,
        validate_pass_a_result,
        validate_pass_b_result,
        validate_pass_c_result,
    )
    from workers.llm_providers import LlmProviderError, MissingProviderError  # noqa: E402
    from workers.openai_provider import (  # noqa: E402
        OpenAIAuditProvider,
        resolve_openai_model_from_env,
    )

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    effective_model = resolve_openai_model_from_env()
    effective_reasoning_effort = os.environ.get("CERTBOUND_OPENAI_REASONING_EFFORT", "")
    effective_max_retries = os.environ.get("CERTBOUND_OPENAI_MAX_RETRIES", "")
    effective_max_output_tokens = os.environ.get("CERTBOUND_OPENAI_MAX_OUTPUT_TOKENS", "")
    effective_timeout = os.environ.get("CERTBOUND_OPENAI_TIMEOUT_SECONDS", "")

    print("V58 Day 8 OpenAI live smoke test")
    print(f"  run_id:                {run_id}")
    print(f"  effective_model:       {effective_model}")
    print(f"  reasoning_effort:      {effective_reasoning_effort}")
    print(f"  max_retries:           {effective_max_retries}")
    print(f"  max_output_tokens:     {effective_max_output_tokens}")
    print(f"  timeout_seconds:       {effective_timeout}")
    print("  intended live calls:   3 (Pass A, Pass B, Pass C)")
    print()

    artifact: Dict[str, Any] = {
        "run_id": run_id,
        "started_at_utc": started_at.isoformat(),
        "provider": "openai",
        "effective_model": effective_model,
        "reasoning_effort": effective_reasoning_effort,
        "max_retries": effective_max_retries,
        "max_output_tokens": effective_max_output_tokens,
        "timeout_seconds": effective_timeout,
        "passes": {},
        "success": False,
    }

    try:
        try:
            provider = OpenAIAuditProvider.from_env()
        except (MissingProviderError, LlmProviderError) as exc:
            # from_env() only loads/validates local configuration; it never
            # makes a network call, so any failure here is a configuration
            # problem (missing key, bad model/effort/timeout/token values).
            raise SmokeTestFailure("configuration", _sanitize_message(str(exc))) from exc

        scenario = _build_synthetic_scenario()

        pass_a_result = _run_pass_a(
            provider,
            scenario,
            run_id=run_id,
            build_pass_a_prompt=build_pass_a_prompt,
            pass_a_schema=PASS_A_RESPONSE_SCHEMA,
            validate_pass_a_result=validate_pass_a_result,
            validation_error_cls=AiQualityAuditValidationError,
            llm_provider_error_cls=LlmProviderError,
        )
        artifact["passes"]["A"] = _print_pass_summary("A", pass_a_result)

        pass_b_result = _run_pass_b(
            provider,
            scenario,
            pass_a_result,
            run_id=run_id,
            build_pass_b_prompt=build_pass_b_prompt,
            pass_b_schema=PASS_B_RESPONSE_SCHEMA,
            validate_pass_b_result=validate_pass_b_result,
            validation_error_cls=AiQualityAuditValidationError,
            llm_provider_error_cls=LlmProviderError,
        )
        artifact["passes"]["B"] = _print_pass_summary("B", pass_b_result)

        pass_c_result = _run_pass_c(
            provider,
            pass_b_result,
            run_id=run_id,
            build_pass_c_prompt=build_pass_c_prompt,
            pass_c_schema=PASS_C_RESPONSE_SCHEMA,
            validate_pass_c_result=validate_pass_c_result,
            validation_error_cls=AiQualityAuditValidationError,
            llm_provider_error_cls=LlmProviderError,
        )
        artifact["passes"]["C"] = _print_pass_summary("C", pass_c_result)

        artifact["success"] = True
        out_path = _write_artifact(started_at, artifact)
        print("SUCCESS: all three passes completed and validated.")
        print(f"Artifact: {out_path}")
        return 0

    except SmokeTestFailure as exc:
        artifact["success"] = False
        artifact["failure_category"] = exc.category
        artifact["failure_message"] = _sanitize_message(str(exc))
        print(f"FAILED at category={exc.category!r}: {artifact['failure_message']}", file=sys.stderr)

        # Re-parse the same bounded, sanitized diagnostic string (produced by
        # workers.openai_provider.describe_openai_error) into separate
        # fields for the artifact. No additional/raw data is consulted; this
        # is a no-op (all fields None) for non-provider-request failures.
        diagnostic_fields = _parse_openai_diagnostic_fields(artifact["failure_message"])
        if any(diagnostic_fields.values()):
            artifact["failure_diagnostics"] = diagnostic_fields
            print("Diagnostic fields:", file=sys.stderr)
            for key, value in diagnostic_fields.items():
                if value:
                    print(f"  {key}: {value}", file=sys.stderr)

        out_path = _write_artifact_best_effort(started_at, artifact)
        if out_path is not None:
            print(f"Failure artifact: {out_path}", file=sys.stderr)
        return 1

    except Exception as exc:  # noqa: BLE001 - defensive, sanitized catch-all
        artifact["success"] = False
        artifact["failure_category"] = "unexpected sanitized failure"
        artifact["failure_message"] = _sanitize_message(f"{type(exc).__name__}: {exc}")
        print(
            f"FAILED (unexpected): {type(exc).__name__}: {artifact['failure_message']}",
            file=sys.stderr,
        )
        out_path = _write_artifact_best_effort(started_at, artifact)
        if out_path is not None:
            print(f"Failure artifact: {out_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
