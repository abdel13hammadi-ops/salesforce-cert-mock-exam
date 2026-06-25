"""
Dry-run audit calibration pilot for CertBound (V45 Phase 2/3).

Runs exactly five labeled calibration cases through deterministic checks,
an injected LLM provider, and finding merge logic. Does not call audit
lifecycle RPCs, publish, promote, or mutate live questions.

Phase 3 pass criteria use canonical finding codes and materiality:
known-good cases fail only on blocking findings; warnings and informational
findings are reported separately.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from workers.deterministic_audit import run_deterministic_checks
from workers.finding_merge import merge_findings
from workers.finding_policy import count_materiality, original_llm_codes
from workers.llm_audit import AUDIT_RESPONSE_SCHEMA, LlmAuditValidationError, validate_llm_response
from workers.llm_providers import LlmProviderError

CALIBRATION_CASE_COUNT = 5
REQUIRED_CASE_LABELS = (
    "known-good",
    "ambiguous",
    "wrong-answer-key",
    "weak-distractors",
    "incomplete-explanation",
)

DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "audit_calibration_cases.json"
)


@dataclass
class CalibrationCaseResult:
    label: str
    expected_defect_category: str
    deterministic_finding_count: int
    llm_finding_count: int
    merged_finding_count: int
    finding_codes: List[str]
    blocking_count: int
    warning_count: int
    informational_count: int
    original_llm_codes: List[str]
    duration_seconds: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Optional[float]
    passed: bool
    false_positive: bool
    provider_failure: bool = False
    invalid_response: bool = False
    error_message: Optional[str] = None


@dataclass
class CalibrationPilotSummary:
    case_results: List[CalibrationCaseResult] = field(default_factory=list)
    cases_passed: int = 0
    known_defects_detected: int = 0
    false_positives: int = 0
    blocking_false_positives: int = 0
    warning_only_on_known_good: int = 0
    canonical_code_coverage: int = 0
    total_cost_usd: float = 0.0
    average_duration_seconds: float = 0.0
    invalid_responses: int = 0
    provider_failures: int = 0


def load_calibration_fixture(path: Path | str) -> dict:
    """Load and validate a calibration fixture file."""
    fixture_path = Path(path)
    with fixture_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("Calibration fixture must contain a 'cases' array")

    if len(cases) != CALIBRATION_CASE_COUNT:
        raise ValueError(
            f"Calibration fixture must contain exactly {CALIBRATION_CASE_COUNT} "
            f"cases, got {len(cases)}"
        )

    labels = [case.get("label") for case in cases]
    missing = [label for label in REQUIRED_CASE_LABELS if label not in labels]
    if missing:
        raise ValueError(
            f"Calibration fixture missing required case labels: {missing}"
        )

    for index, case in enumerate(cases):
        for key in ("label", "expected_defect_category", "question", "user_prompt"):
            if key not in case:
                raise ValueError(
                    f"Calibration case {index} missing required field {key!r}"
                )
        if not isinstance(case.get("resource_snapshot"), dict):
            raise ValueError(
                f"Calibration case {case['label']!r} must include resource_snapshot"
            )

    return data


def _finding_codes(findings: List[dict]) -> List[str]:
    return [str(f.get("finding_code", "")) for f in findings if f.get("finding_code")]


def _blocking_findings(findings: List[dict]) -> List[dict]:
    return [f for f in findings if f.get("materiality") == "blocking"]


def _evaluate_case(case: dict, merged_findings: List[dict]) -> tuple[bool, bool, bool]:
    """Return (passed, false_positive, warning_only_on_known_good)."""
    expect_detection = bool(case.get("expect_detection", True))
    expected_category = case.get("expected_defect_category", "")
    expected_codes = case.get("expected_canonical_codes") or case.get("expected_finding_codes") or []
    expected_materiality = case.get("expected_materiality")
    merged_codes = _finding_codes(merged_findings)
    merged_types = {f.get("finding_type") for f in merged_findings}
    blocking = _blocking_findings(merged_findings)
    counts = count_materiality(merged_findings)

    if not expect_detection or expected_category == "none":
        false_positive = len(blocking) > 0
        warning_only = counts["warning"] > 0 or counts["informational"] > 0
        passed = not false_positive
        return (passed, false_positive, warning_only and passed)

    if expected_codes:
        matched = [f for f in merged_findings if f.get("finding_code") in expected_codes]
        if not matched:
            return (False, False, False)
        if expected_materiality:
            passed = any(f.get("materiality") == expected_materiality for f in matched)
            return (passed, False, False)
        return (True, False, False)

    if expected_category:
        passed = expected_category in merged_types or len(merged_findings) > 0
        return (passed, False, False)

    passed = len(merged_findings) > 0
    return (passed, False, False)


def run_calibration_case(
    case: dict,
    provider,
    *,
    ruleset_version: str,
    model_name: str,
    system_prompt: str,
) -> CalibrationCaseResult:
    """Run one calibration case through deterministic + LLM + merge (dry-run)."""
    started = time.perf_counter()
    question = case["question"]
    ruleset = case.get("ruleset_version") or ruleset_version

    det_findings = run_deterministic_checks(question, ruleset)
    llm_findings: List[dict] = []
    input_tokens = 0
    output_tokens = 0
    estimated_cost: Optional[float] = None
    provider_failure = False
    invalid_response = False
    error_message: Optional[str] = None

    try:
        response = provider(
            model_name=case.get("model_name") or model_name,
            system_prompt=case.get("system_prompt") or system_prompt,
            user_prompt=case["user_prompt"],
            response_schema=AUDIT_RESPONSE_SCHEMA,
            metadata={
                "question": question,
                "resource_snapshot": case.get("resource_snapshot"),
                "calibration_label": case["label"],
            },
        )
        llm_findings = validate_llm_response(response.parsed_response)
        input_tokens = int(response.input_tokens or 0)
        output_tokens = int(response.output_tokens or 0)
        estimated_cost = response.actual_cost_usd
    except LlmAuditValidationError as exc:
        invalid_response = True
        error_message = str(exc)
    except LlmProviderError as exc:
        provider_failure = True
        error_message = str(exc)
    except Exception as exc:  # noqa: BLE001
        provider_failure = True
        error_message = f"{type(exc).__name__}: {exc}"

    merged_findings = merge_findings(det_findings, llm_findings)
    passed, false_positive, warning_only = _evaluate_case(case, merged_findings)
    counts = count_materiality(merged_findings)
    duration = time.perf_counter() - started

    return CalibrationCaseResult(
        label=str(case["label"]),
        expected_defect_category=str(case["expected_defect_category"]),
        deterministic_finding_count=len(det_findings),
        llm_finding_count=len(llm_findings),
        merged_finding_count=len(merged_findings),
        finding_codes=_finding_codes(merged_findings),
        blocking_count=counts["blocking"],
        warning_count=counts["warning"],
        informational_count=counts["informational"],
        original_llm_codes=original_llm_codes(merged_findings),
        duration_seconds=duration,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost,
        passed=passed,
        false_positive=false_positive,
        provider_failure=provider_failure,
        invalid_response=invalid_response,
        error_message=error_message,
    )


def run_calibration_pilot(fixture: dict, provider) -> CalibrationPilotSummary:
    """Run all five calibration cases and aggregate summary metrics."""
    ruleset_version = fixture.get("ruleset_version", "1.0.0")
    model_name = fixture.get("model_name", "claude-sonnet-4-6")
    system_prompt = fixture.get(
        "system_prompt",
        "You are a CertBound certification question auditor.",
    )

    results: List[CalibrationCaseResult] = []
    for case in fixture["cases"]:
        results.append(
            run_calibration_case(
                case,
                provider,
                ruleset_version=ruleset_version,
                model_name=model_name,
                system_prompt=system_prompt,
            )
        )

    total_cost = sum(r.estimated_cost_usd or 0.0 for r in results)
    avg_duration = (
        sum(r.duration_seconds for r in results) / len(results)
        if results else 0.0
    )

    blocking_false_positives = sum(
        1 for r in results if r.label == "known-good" and r.false_positive
    )
    warning_only_on_known_good = sum(
        1 for r in results
        if r.label == "known-good" and r.warning_count + r.informational_count > 0 and r.passed
    )
    canonical_code_coverage = sum(
        1 for r in results
        if r.label != "known-good" and r.passed and r.finding_codes
    )

    return CalibrationPilotSummary(
        case_results=results,
        cases_passed=sum(1 for r in results if r.passed),
        known_defects_detected=sum(
            1 for r in results
            if r.label != "known-good" and r.merged_finding_count > 0
        ),
        false_positives=sum(1 for r in results if r.false_positive),
        blocking_false_positives=blocking_false_positives,
        warning_only_on_known_good=warning_only_on_known_good,
        canonical_code_coverage=canonical_code_coverage,
        total_cost_usd=total_cost,
        average_duration_seconds=avg_duration,
        invalid_responses=sum(1 for r in results if r.invalid_response),
        provider_failures=sum(1 for r in results if r.provider_failure),
    )


def format_case_report(result: CalibrationCaseResult) -> str:
    """Return a human-readable report block for one case."""
    lines = [
        f"case: {result.label}",
        f"expected_defect_category: {result.expected_defect_category}",
        f"deterministic_findings: {result.deterministic_finding_count}",
        f"llm_findings: {result.llm_finding_count}",
        f"merged_findings: {result.merged_finding_count}",
        f"blocking_count: {result.blocking_count}",
        f"warning_count: {result.warning_count}",
        f"informational_count: {result.informational_count}",
        f"finding_codes: {result.finding_codes}",
        f"original_llm_codes: {result.original_llm_codes}",
        f"duration_sec: {result.duration_seconds:.2f}",
        f"input_tokens: {result.input_tokens}",
        f"output_tokens: {result.output_tokens}",
        f"estimated_cost_usd: {result.estimated_cost_usd}",
        f"passed: {result.passed}",
        f"false_positive: {result.false_positive}",
    ]
    if result.error_message:
        lines.append(f"error: {result.error_message}")
    return "\n".join(lines)


def format_pilot_summary(summary: CalibrationPilotSummary) -> str:
    """Return a human-readable final totals block."""
    blocks = [format_case_report(result) for result in summary.case_results]
    blocks.append(
        "\n".join([
            "totals:",
            f"cases_passed: {summary.cases_passed}/{CALIBRATION_CASE_COUNT}",
            f"known_defects_detected: {summary.known_defects_detected}",
            f"false_positives: {summary.false_positives}",
            f"blocking_false_positives: {summary.blocking_false_positives}",
            f"warning_only_on_known_good: {summary.warning_only_on_known_good}",
            f"canonical_code_coverage: {summary.canonical_code_coverage}",
            f"total_cost_usd: {summary.total_cost_usd:.6f}",
            f"average_duration_sec: {summary.average_duration_seconds:.2f}",
            f"invalid_responses: {summary.invalid_responses}",
            f"provider_failures: {summary.provider_failures}",
        ])
    )
    return "\n\n".join(blocks)
