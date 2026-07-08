#!/usr/bin/env python3
"""
V59 offline combined-provider policy evaluator.

Deterministically combines existing OpenAI and Sonnet prediction artifacts
under seven fixed decision policies and scores outcomes against the SME-reviewed
benchmark fixture. Read-only; makes no provider or database calls.

Detection and disposition are measured separately:
- Detection correctness uses canonical ``_evaluate_detection`` semantics from
  ``workers.quality_benchmark`` (expected finding code + materiality match).
- Disposition metrics reflect policy routing (APPROVE / REJECT / HUMAN_REVIEW).
- A correctly detected warning finding may legitimately remain approved per
  ``_summarize_findings`` (``approved = overall_materiality != 'blocking'``).

Routing / correctness outcome labels (per case, per policy)
-------------------------------------------------------------
- known_good_auto_approved
- known_good_FALSE_REJECTION
- known_good_human_review
- defective_detected_and_rejected
- defective_detected_but_approved
- defective_detected_but_human_review
- defective_missed_and_approved
- defective_missed_but_rejected_other_reason
- defective_missed_and_human_review
- defective_detection_unscored

Policy decision functions consume ONLY provider-normalized decision objects
(disposition, has_blocking_finding, has_warning_finding, available, finding_codes).
SME ground truth is applied only after all policy dispositions are computed.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

EVALUATOR_VERSION = "v59-evaluate-provider-policies-v2"
EXPECTED_FIXTURE_SHA256 = "8dad069126d84e826f7edcc180773f2278583e41ec964cdb86c4e6d503cb9fa6"

DETECTION_FINDING_SOURCE_BY_POLICY: Dict[str, str] = {
    "SONNET_SINGLE": "sonnet",
    "OPENAI_SINGLE": "openai",
    "REJECT_ON_EITHER": "merged",
    "REJECT_ON_BOTH": "merged",
    "SONNET_REJECTION_VALIDATED_BY_OPENAI": "sonnet",
    "DISAGREEMENT_TO_HUMAN_REVIEW": "merged",
    "MATERIALITY_GATED": "merged",
}

DISPOSITIONS = frozenset({"APPROVE", "REJECT", "HUMAN_REVIEW"})

POLICY_NAMES: tuple[str, ...] = (
    "SONNET_SINGLE",
    "OPENAI_SINGLE",
    "REJECT_ON_EITHER",
    "REJECT_ON_BOTH",
    "SONNET_REJECTION_VALIDATED_BY_OPENAI",
    "DISAGREEMENT_TO_HUMAN_REVIEW",
    "MATERIALITY_GATED",
)

FORBIDDEN_IMPORTS = frozenset(
    {
        "requests",
        "httpx",
        "urllib3",
        "psycopg",
        "psycopg2",
        "supabase",
        "sqlalchemy",
        "asyncpg",
    }
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workers.quality_benchmark import (  # noqa: E402
    BenchmarkMetrics,
    _build_case_result,
    compute_benchmark_metrics,
)
_DEFAULT_OPENAI = _REPO_ROOT / ".local/v58_openai_baseline/20260708T033458Z/result.json"
_DEFAULT_OPENAI_SCORECARD = _REPO_ROOT / ".local/v58_openai_baseline/20260708T033458Z/scorecard.json"
_DEFAULT_RECONCILED = (
    _REPO_ROOT / ".local/v58_openai_baseline/20260708T033458Z/provider_comparison_reconciled.json"
)
_DEFAULT_SONNET = Path(
    r"C:\Users\Abdel\AppData\Local\Temp\v58_day7_rerun_20260708T002827Z\v48_day7_rerun_predictions.json"
)
_DEFAULT_FIXTURE = _REPO_ROOT / "workers/fixtures/quality_benchmark_v1_sme_reviewed.json"
_DEFAULT_OUTPUT_PARENT = _REPO_ROOT / ".local/v59_policy_evaluation"


class PolicyEvaluatorError(Exception):
    """Raised when integrity checks or artifact validation fail."""


@dataclass(frozen=True)
class ProviderNormalizedDecision:
    disposition: str
    has_blocking_finding: bool
    has_warning_finding: bool
    available: bool
    finding_codes: tuple[str, ...]


PolicyFunction = Callable[[ProviderNormalizedDecision, ProviderNormalizedDecision], str]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_fixture_sha256(path: Path, expected_sha256: str) -> None:
    payload = load_json(path)
    source_hash = payload.get("source_fixture_sha256")
    if source_hash != expected_sha256:
        raise PolicyEvaluatorError(
            f"SME fixture source_fixture_sha256 mismatch for {path}: "
            f"expected {expected_sha256}, got {source_hash!r}"
        )


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_prediction_map(path: Path) -> Dict[str, dict]:
    payload = load_json(path)
    predictions = payload.get("predictions", payload)
    if not isinstance(predictions, list):
        raise PolicyEvaluatorError(f"Prediction artifact {path} must contain a predictions list")
    by_id: Dict[str, dict] = {}
    for record in predictions:
        case_id = record.get("case_id")
        if not case_id:
            raise PolicyEvaluatorError(f"Prediction record in {path} missing case_id")
        if case_id in by_id:
            raise PolicyEvaluatorError(f"Duplicate case_id {case_id!r} in {path}")
        by_id[case_id] = record
    return by_id


def load_sme_cases(path: Path) -> List[dict]:
    payload = load_json(path)
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise PolicyEvaluatorError(f"SME fixture {path} must contain a cases list")
    return cases


def _normalize_materiality(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"blocking", "warning"}:
        return text
    return None


def _extract_finding_materialities(prediction: Mapping[str, Any]) -> tuple[bool, bool, tuple[str, ...]]:
    has_blocking = False
    has_warning = False
    codes: list[str] = []

    top_materiality = _normalize_materiality(prediction.get("materiality"))
    if top_materiality == "blocking":
        has_blocking = True
    elif top_materiality == "warning":
        has_warning = True

    for code in prediction.get("finding_codes") or []:
        if code and code not in codes:
            codes.append(str(code))

    raw_output = prediction.get("raw_output") or {}
    findings = raw_output.get("findings") or prediction.get("findings") or []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        finding_code = finding.get("finding_code")
        if finding_code and finding_code not in codes:
            codes.append(str(finding_code))
        materiality = _normalize_materiality(finding.get("materiality"))
        if materiality == "blocking":
            has_blocking = True
        elif materiality == "warning":
            has_warning = True

    return has_blocking, has_warning, tuple(codes)


def _prediction_requires_human_review(prediction: Mapping[str, Any]) -> bool:
    if prediction.get("error"):
        return True

    raw_output = prediction.get("raw_output") or {}
    run_status = str(raw_output.get("run_status") or "").strip().lower()
    if run_status in {"inconclusive", "error", "failed"}:
        return True

    if raw_output.get("requires_human_review") is True:
        return True

    for finding in raw_output.get("findings") or prediction.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        metadata = finding.get("metadata") or {}
        dispute_status = str(metadata.get("dispute_resolution_status") or "").strip().upper()
        if dispute_status == "UNRESOLVED":
            return True
        if metadata.get("requires_human_review") is True:
            return True

    return False


def normalize_provider_prediction(prediction: Optional[Mapping[str, Any]]) -> ProviderNormalizedDecision:
    if prediction is None:
        return ProviderNormalizedDecision(
            disposition="HUMAN_REVIEW",
            has_blocking_finding=False,
            has_warning_finding=False,
            available=False,
            finding_codes=(),
        )

    has_blocking, has_warning, finding_codes = _extract_finding_materialities(prediction)

    if _prediction_requires_human_review(prediction):
        return ProviderNormalizedDecision(
            disposition="HUMAN_REVIEW",
            has_blocking_finding=has_blocking,
            has_warning_finding=has_warning,
            available=False,
            finding_codes=finding_codes,
        )

    approved = prediction.get("approved")
    if approved is False:
        return ProviderNormalizedDecision(
            disposition="REJECT",
            has_blocking_finding=has_blocking,
            has_warning_finding=has_warning,
            available=True,
            finding_codes=finding_codes,
        )

    if approved is True:
        return ProviderNormalizedDecision(
            disposition="APPROVE",
            has_blocking_finding=has_blocking,
            has_warning_finding=has_warning,
            available=True,
            finding_codes=finding_codes,
        )

    return ProviderNormalizedDecision(
        disposition="HUMAN_REVIEW",
        has_blocking_finding=has_blocking,
        has_warning_finding=has_warning,
        available=False,
        finding_codes=finding_codes,
    )


def _prediction_findings(prediction: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    return list((prediction.get("raw_output") or {}).get("findings") or [])


def _merge_findings(*finding_lists: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    merged: Dict[tuple[str, str], Mapping[str, Any]] = {}
    for findings in finding_lists:
        for finding in findings:
            key = (str(finding.get("finding_code")), str(finding.get("materiality")))
            merged.setdefault(key, finding)
    return list(merged.values())


def _provider_has_error(prediction: Optional[Mapping[str, Any]]) -> bool:
    if prediction is None:
        return True
    return bool(prediction.get("error"))


def _evaluate_provider_detection(
    case: Mapping[str, Any],
    prediction: Optional[Mapping[str, Any]],
) -> tuple[Optional[bool], bool]:
    """Return (detection_success, detection_unscored) for one provider."""
    if _provider_has_error(prediction):
        return None, True
    findings = _prediction_findings(prediction or {})
    result = _build_case_result(case, engine="v59-provider-detection", findings=findings)
    return result.detection_success, False


def _findings_for_policy_detection(
    policy_name: str,
    *,
    sonnet_prediction: Optional[Mapping[str, Any]],
    openai_prediction: Optional[Mapping[str, Any]],
) -> tuple[List[Mapping[str, Any]], bool]:
    source = DETECTION_FINDING_SOURCE_BY_POLICY[policy_name]
    sonnet_err = _provider_has_error(sonnet_prediction)
    openai_err = _provider_has_error(openai_prediction)

    if source == "sonnet":
        if sonnet_err:
            return [], True
        return _prediction_findings(sonnet_prediction or {}), False
    if source == "openai":
        if openai_err:
            return [], True
        return _prediction_findings(openai_prediction or {}), False

    if sonnet_err and openai_err:
        return [], True
    return _merge_findings(
        [] if sonnet_err else _prediction_findings(sonnet_prediction or {}),
        [] if openai_err else _prediction_findings(openai_prediction or {}),
    ), False


def _compute_policy_detection_metrics(
    policy_name: str,
    sme_cases: Sequence[Mapping[str, Any]],
    sonnet_predictions: Mapping[str, Mapping[str, Any]],
    openai_predictions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    scored_cases: List[Mapping[str, Any]] = []
    case_results = []
    unscored_ids: List[str] = []
    detected_by_case_id: Dict[str, bool] = {}

    for case in sme_cases:
        case_id = str(case["case_id"])
        findings, detection_unscored = _findings_for_policy_detection(
            policy_name,
            sonnet_prediction=sonnet_predictions.get(case_id),
            openai_prediction=openai_predictions.get(case_id),
        )
        if detection_unscored:
            unscored_ids.append(case_id)
            continue
        scored_cases.append(case)
        result = _build_case_result(case, engine=f"v59-policy:{policy_name}", findings=findings)
        case_results.append(result)
        detected_by_case_id[case_id] = result.detection_success

    metrics: BenchmarkMetrics = (
        compute_benchmark_metrics(scored_cases, case_results) if scored_cases else BenchmarkMetrics()
    )

    warning_results = [
        result
        for result in case_results
        if not result.known_good and result.expected_materiality == "warning"
    ]
    warning_detected = sum(1 for result in warning_results if result.detection_success)
    warning_total = len(warning_results)
    correct_detections = sum(1 for result in case_results if result.detection_success)
    missed_detections = len(case_results) - correct_detections

    return {
        "detection_finding_source": DETECTION_FINDING_SOURCE_BY_POLICY[policy_name],
        "scored_cases_for_detection": len(scored_cases),
        "unscored_cases_for_detection": sorted(unscored_ids),
        "correct_detections": correct_detections,
        "missed_detections": missed_detections,
        "blocking_recall": metrics.blocking_category_recall,
        "blocking_recall_detected": metrics.blocking_category_detected,
        "blocking_recall_total": metrics.blocking_category_total,
        "warning_recall": _rate(warning_detected, warning_total),
        "warning_recall_detected": warning_detected,
        "warning_recall_total": warning_total,
        "overall_recall": metrics.overall_recall,
        "overall_recall_detected": metrics.overall_recall_detected,
        "overall_recall_total": metrics.overall_recall_total,
        "detection_false_approvals": metrics.false_approvals,
        "detection_false_approval_rate": metrics.false_approval_rate,
        "_detected_by_case_id": detected_by_case_id,
        "_unscored_case_ids": set(unscored_ids),
    }


def policy_sonnet_single(
    sonnet: ProviderNormalizedDecision,
    openai: ProviderNormalizedDecision,
) -> str:
    _ = openai
    if sonnet.disposition == "APPROVE":
        return "APPROVE"
    if sonnet.disposition == "REJECT":
        return "REJECT"
    return "HUMAN_REVIEW"


def policy_openai_single(
    sonnet: ProviderNormalizedDecision,
    openai: ProviderNormalizedDecision,
) -> str:
    _ = sonnet
    if openai.disposition == "APPROVE":
        return "APPROVE"
    if openai.disposition == "REJECT":
        return "REJECT"
    return "HUMAN_REVIEW"


def policy_reject_on_either(
    sonnet: ProviderNormalizedDecision,
    openai: ProviderNormalizedDecision,
) -> str:
    if sonnet.disposition == "REJECT" or openai.disposition == "REJECT":
        return "REJECT"
    if sonnet.disposition == "HUMAN_REVIEW" or openai.disposition == "HUMAN_REVIEW":
        return "HUMAN_REVIEW"
    return "APPROVE"


def policy_reject_on_both(
    sonnet: ProviderNormalizedDecision,
    openai: ProviderNormalizedDecision,
) -> str:
    if sonnet.disposition == "REJECT" and openai.disposition == "REJECT":
        return "REJECT"
    if sonnet.disposition == "HUMAN_REVIEW" or openai.disposition == "HUMAN_REVIEW":
        return "HUMAN_REVIEW"
    return "APPROVE"


def policy_sonnet_rejection_validated_by_openai(
    sonnet: ProviderNormalizedDecision,
    openai: ProviderNormalizedDecision,
) -> str:
    if sonnet.disposition == "HUMAN_REVIEW":
        return "HUMAN_REVIEW"
    if sonnet.disposition == "APPROVE":
        return "APPROVE"
    if sonnet.disposition == "REJECT" and openai.disposition == "REJECT":
        return "REJECT"
    return "HUMAN_REVIEW"


def policy_disagreement_to_human_review(
    sonnet: ProviderNormalizedDecision,
    openai: ProviderNormalizedDecision,
) -> str:
    if sonnet.disposition == "HUMAN_REVIEW" or openai.disposition == "HUMAN_REVIEW":
        return "HUMAN_REVIEW"
    if sonnet.disposition == "APPROVE" and openai.disposition == "APPROVE":
        return "APPROVE"
    if sonnet.disposition == "REJECT" and openai.disposition == "REJECT":
        return "REJECT"
    return "HUMAN_REVIEW"


def policy_materiality_gated(
    sonnet: ProviderNormalizedDecision,
    openai: ProviderNormalizedDecision,
) -> str:
    if sonnet.has_blocking_finding or openai.has_blocking_finding:
        return "REJECT"
    if sonnet.has_warning_finding or openai.has_warning_finding:
        return "HUMAN_REVIEW"
    if sonnet.disposition == "HUMAN_REVIEW" or openai.disposition == "HUMAN_REVIEW":
        return "HUMAN_REVIEW"
    if sonnet.disposition == "APPROVE" and openai.disposition == "APPROVE":
        return "APPROVE"
    return "HUMAN_REVIEW"


POLICY_FUNCTIONS: Dict[str, PolicyFunction] = {
    "SONNET_SINGLE": policy_sonnet_single,
    "OPENAI_SINGLE": policy_openai_single,
    "REJECT_ON_EITHER": policy_reject_on_either,
    "REJECT_ON_BOTH": policy_reject_on_both,
    "SONNET_REJECTION_VALIDATED_BY_OPENAI": policy_sonnet_rejection_validated_by_openai,
    "DISAGREEMENT_TO_HUMAN_REVIEW": policy_disagreement_to_human_review,
    "MATERIALITY_GATED": policy_materiality_gated,
}


def policy_function_has_no_sme_parameters(function: PolicyFunction) -> bool:
    signature = inspect.signature(function)
    for parameter in signature.parameters.values():
        if parameter.name.startswith("sme_") or parameter.name in {
            "known_good",
            "expected_finding_codes",
            "expected_materiality",
        }:
            return False
    return True


def compute_routing_outcome_label(
    sme_case_type: str,
    disposition: str,
    *,
    detection_success: Optional[bool] = None,
    detection_unscored: bool = False,
) -> str:
    if sme_case_type == "known_good":
        if disposition == "APPROVE":
            return "known_good_auto_approved"
        if disposition == "REJECT":
            return "known_good_FALSE_REJECTION"
        return "known_good_human_review"

    if detection_unscored or detection_success is None:
        return "defective_detection_unscored"

    if detection_success:
        if disposition == "APPROVE":
            return "defective_detected_but_approved"
        if disposition == "REJECT":
            return "defective_detected_and_rejected"
        return "defective_detected_but_human_review"

    if disposition == "APPROVE":
        return "defective_missed_and_approved"
    if disposition == "REJECT":
        return "defective_missed_but_rejected_other_reason"
    return "defective_missed_and_human_review"


def validate_case_alignment(
    sme_cases: Sequence[Mapping[str, Any]],
    sonnet_predictions: Mapping[str, Mapping[str, Any]],
    openai_predictions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    sme_ids = [case["case_id"] for case in sme_cases]
    if len(sme_ids) != len(set(sme_ids)):
        raise PolicyEvaluatorError("SME fixture contains duplicate case_id values")

    sme_id_set = set(sme_ids)
    sonnet_ids = set(sonnet_predictions)
    openai_ids = set(openai_predictions)

    unknown_sonnet = sorted(sonnet_ids - sme_id_set)
    unknown_openai = sorted(openai_ids - sme_id_set)
    if unknown_sonnet:
        raise PolicyEvaluatorError(f"Sonnet artifact contains unknown case IDs: {unknown_sonnet}")
    if unknown_openai:
        raise PolicyEvaluatorError(f"OpenAI artifact contains unknown case IDs: {unknown_openai}")

    missing_sonnet = sorted(sme_id_set - sonnet_ids)
    missing_openai = sorted(sme_id_set - openai_ids)

    known_good_count = sum(1 for case in sme_cases if case.get("known_good") is True)
    defective_count = sum(1 for case in sme_cases if case.get("known_good") is False)
    blocking_count = sum(
        1
        for case in sme_cases
        if case.get("known_good") is False and case.get("expected_materiality") == "blocking"
    )
    warning_count = sum(
        1
        for case in sme_cases
        if case.get("known_good") is False and case.get("expected_materiality") == "warning"
    )

    if len(sme_ids) != 40:
        raise PolicyEvaluatorError(f"Expected exactly 40 SME cases, found {len(sme_ids)}")
    if known_good_count != 9:
        raise PolicyEvaluatorError(f"Expected exactly 9 known-good cases, found {known_good_count}")
    if defective_count != 31:
        raise PolicyEvaluatorError(f"Expected exactly 31 defective cases, found {defective_count}")
    if blocking_count != 18:
        raise PolicyEvaluatorError(f"Expected exactly 18 blocking defective cases, found {blocking_count}")
    if warning_count != 13:
        raise PolicyEvaluatorError(f"Expected exactly 13 warning defective cases, found {warning_count}")
    if blocking_count + warning_count != defective_count:
        raise PolicyEvaluatorError(
            "Defective severity counts do not sum: "
            f"{blocking_count} blocking + {warning_count} warning != {defective_count} defective"
        )

    return {
        "sme_case_count": len(sme_ids),
        "known_good_count": known_good_count,
        "defective_count": defective_count,
        "blocking_defective_count": blocking_count,
        "warning_defective_count": warning_count,
        "sonnet_case_count": len(sonnet_ids),
        "openai_case_count": len(openai_ids),
        "missing_sonnet_case_ids": missing_sonnet,
        "missing_openai_case_ids": missing_openai,
        "unknown_sonnet_case_ids": unknown_sonnet,
        "unknown_openai_case_ids": unknown_openai,
    }


def build_case_matrix(
    sme_cases: Sequence[Mapping[str, Any]],
    sonnet_predictions: Mapping[str, Mapping[str, Any]],
    openai_predictions: Mapping[str, Mapping[str, Any]],
) -> List[dict[str, Any]]:
    policy_detection = {
        policy_name: _compute_policy_detection_metrics(
            policy_name, sme_cases, sonnet_predictions, openai_predictions
        )
        for policy_name in POLICY_NAMES
    }

    rows: List[dict[str, Any]] = []
    for case in sorted(sme_cases, key=lambda item: item["case_id"]):
        case_id = case["case_id"]
        sonnet_prediction = sonnet_predictions.get(case_id)
        openai_prediction = openai_predictions.get(case_id)
        sonnet_normalized = normalize_provider_prediction(sonnet_prediction)
        openai_normalized = normalize_provider_prediction(openai_prediction)
        sme_case_type = "known_good" if case.get("known_good") is True else "defective"

        sonnet_detection_success, sonnet_detection_unscored = _evaluate_provider_detection(
            case, sonnet_prediction
        )
        openai_detection_success, openai_detection_unscored = _evaluate_provider_detection(
            case, openai_prediction
        )

        row: dict[str, Any] = {
            "case_id": case_id,
            "sme_case_type": sme_case_type,
            "sme_expected_materiality": case.get("expected_materiality"),
            "sme_expected_finding_codes": list(case.get("expected_finding_codes") or []),
            "sonnet_normalized_disposition": sonnet_normalized.disposition,
            "sonnet_has_blocking_finding": sonnet_normalized.has_blocking_finding,
            "sonnet_has_warning_finding": sonnet_normalized.has_warning_finding,
            "sonnet_finding_codes": list(sonnet_normalized.finding_codes),
            "sonnet_detection_success": sonnet_detection_success,
            "sonnet_detection_unscored": sonnet_detection_unscored,
            "openai_normalized_disposition": openai_normalized.disposition,
            "openai_has_blocking_finding": openai_normalized.has_blocking_finding,
            "openai_has_warning_finding": openai_normalized.has_warning_finding,
            "openai_finding_codes": list(openai_normalized.finding_codes),
            "openai_detection_success": openai_detection_success,
            "openai_detection_unscored": openai_detection_unscored,
        }

        for policy_name, policy_fn in POLICY_FUNCTIONS.items():
            disposition = policy_fn(sonnet_normalized, openai_normalized)
            if disposition not in DISPOSITIONS:
                raise PolicyEvaluatorError(
                    f"Policy {policy_name} returned invalid disposition {disposition!r} for {case_id}"
                )
            detection = policy_detection[policy_name]
            detection_unscored = case_id in detection["_unscored_case_ids"]
            detection_success = None if detection_unscored else detection["_detected_by_case_id"].get(case_id)

            row[f"{policy_name}_disposition"] = disposition
            row[f"{policy_name}_detection_success"] = detection_success
            row[f"{policy_name}_detection_unscored"] = detection_unscored
            row[f"{policy_name}_outcome"] = compute_routing_outcome_label(
                sme_case_type,
                disposition,
                detection_success=detection_success,
                detection_unscored=detection_unscored,
            )

        rows.append(row)

    return rows


def _rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def derive_policy_metrics(
    case_matrix: Sequence[Mapping[str, Any]],
    policy_name: str,
    *,
    detection_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    disposition_key = f"{policy_name}_disposition"
    detection_success_key = f"{policy_name}_detection_success"
    total_cases = len(case_matrix)

    approve_count = sum(1 for row in case_matrix if row[disposition_key] == "APPROVE")
    reject_count = sum(1 for row in case_matrix if row[disposition_key] == "REJECT")
    human_review_count = sum(1 for row in case_matrix if row[disposition_key] == "HUMAN_REVIEW")

    if approve_count + reject_count + human_review_count != total_cases:
        raise PolicyEvaluatorError(
            f"Policy {policy_name} disposition counts do not sum to {total_cases}"
        )

    defective_rows = [row for row in case_matrix if row["sme_case_type"] == "defective"]
    known_good_rows = [row for row in case_matrix if row["sme_case_type"] == "known_good"]
    blocking_rows = [
        row for row in defective_rows if row["sme_expected_materiality"] == "blocking"
    ]
    warning_rows = [
        row for row in defective_rows if row["sme_expected_materiality"] == "warning"
    ]

    defective_auto_approved = sum(1 for row in defective_rows if row[disposition_key] == "APPROVE")
    blocking_auto_approved = sum(1 for row in blocking_rows if row[disposition_key] == "APPROVE")
    warning_auto_approved = sum(1 for row in warning_rows if row[disposition_key] == "APPROVE")

    undetected_defective_auto_approved = sum(
        1
        for row in defective_rows
        if row[disposition_key] == "APPROVE" and row.get(detection_success_key) is False
    )
    detected_warning_auto_approved = sum(
        1
        for row in warning_rows
        if row[disposition_key] == "APPROVE" and row.get(detection_success_key) is True
    )

    blocking_auto_reject = sum(1 for row in blocking_rows if row[disposition_key] == "REJECT")
    warning_auto_reject = sum(1 for row in warning_rows if row[disposition_key] == "REJECT")
    overall_auto_reject = sum(1 for row in defective_rows if row[disposition_key] == "REJECT")

    blocking_protected = sum(
        1 for row in blocking_rows if row[disposition_key] in {"REJECT", "HUMAN_REVIEW"}
    )
    warning_protected = sum(
        1 for row in warning_rows if row[disposition_key] in {"REJECT", "HUMAN_REVIEW"}
    )
    overall_protected = sum(
        1 for row in defective_rows if row[disposition_key] in {"REJECT", "HUMAN_REVIEW"}
    )

    publication_blocking_count = sum(
        1
        for row in case_matrix
        if row[disposition_key] == "REJECT"
        and (row.get("sonnet_has_blocking_finding", False) or row.get("openai_has_blocking_finding", False))
    )

    known_good_auto_approved = sum(1 for row in known_good_rows if row[disposition_key] == "APPROVE")
    known_good_auto_rejected = sum(1 for row in known_good_rows if row[disposition_key] == "REJECT")
    known_good_human_review = sum(
        1 for row in known_good_rows if row[disposition_key] == "HUMAN_REVIEW"
    )

    if known_good_auto_approved + known_good_auto_rejected + known_good_human_review != len(
        known_good_rows
    ):
        raise PolicyEvaluatorError(f"Known-good counts for {policy_name} do not sum to 9")

    detection_public = {
        key: value
        for key, value in detection_metrics.items()
        if not key.startswith("_")
    }

    return {
        "detection": detection_public,
        "disposition": {
            "automatic_approval_count": approve_count,
            "automatic_approval_rate": _rate(approve_count, total_cases),
            "automatic_rejection_count": reject_count,
            "automatic_rejection_rate": _rate(reject_count, total_cases),
            "human_review_count": human_review_count,
            "human_review_rate": _rate(human_review_count, total_cases),
            "publication_blocking_count": publication_blocking_count,
            "publication_blocking_rate": _rate(publication_blocking_count, total_cases),
        },
        "safety": {
            "defective_cases_automatically_approved": defective_auto_approved,
            "disposition_auto_approval_rate": _rate(defective_auto_approved, len(defective_rows)),
            "undetected_defective_cases_automatically_approved": undetected_defective_auto_approved,
            "undetected_defective_auto_approval_rate": _rate(
                undetected_defective_auto_approved, len(defective_rows)
            ),
            "detected_warning_cases_automatically_approved": detected_warning_auto_approved,
            "blocking_defects_automatically_approved": blocking_auto_approved,
            "warning_defects_automatically_approved": warning_auto_approved,
            "blocking_auto_reject_recall": _rate(blocking_auto_reject, len(blocking_rows)),
            "warning_auto_reject_recall": _rate(warning_auto_reject, len(warning_rows)),
            "overall_auto_reject_recall": _rate(overall_auto_reject, len(defective_rows)),
            "false_approval_rate": detection_metrics.get("detection_false_approval_rate"),
            "detection_false_approvals": detection_metrics.get("detection_false_approvals"),
        },
        "protected_routing": {
            "blocking_protected_routing_recall": _rate(blocking_protected, len(blocking_rows)),
            "warning_protected_routing_recall": _rate(warning_protected, len(warning_rows)),
            "overall_protected_routing_recall": _rate(overall_protected, len(defective_rows)),
        },
        "known_good_impact": {
            "known_good_automatically_approved": known_good_auto_approved,
            "known_good_automatically_rejected": known_good_auto_rejected,
            "known_good_routed_to_human_review": known_good_human_review,
            "known_good_automatic_rejection_rate": _rate(known_good_auto_rejected, len(known_good_rows)),
            "known_good_human_review_rate": _rate(known_good_human_review, len(known_good_rows)),
            "known_good_straight_through_approval_rate": _rate(
                known_good_auto_approved, len(known_good_rows)
            ),
        },
        "operational_workload": {
            "cases_requiring_human_review": human_review_count,
            "fully_automatic_decision_rate": _rate(approve_count + reject_count, total_cases),
        },
    }


def compute_provider_disagreement_count(case_matrix: Sequence[Mapping[str, Any]]) -> int:
    return sum(
        1
        for row in case_matrix
        if row["sonnet_normalized_disposition"] != row["openai_normalized_disposition"]
    )


def _pareto_compare(left: float, right: float, *, higher_is_better: bool) -> bool:
    if higher_is_better:
        return right >= left
    return right <= left


def _pareto_strictly_better(left: float, right: float, *, higher_is_better: bool) -> bool:
    if higher_is_better:
        return right > left
    return right < left


def analyze_pareto_dominance(policy_metrics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    axes = [
        ("undetected_defective_auto_approval_rate", False),
        ("blocking_protected_routing_recall", True),
        ("warning_protected_routing_recall", True),
        ("known_good_automatic_rejection_rate", False),
        ("human_review_rate", False),
    ]

    def axis_value(policy_name: str, axis_name: str) -> float:
        metrics = policy_metrics[policy_name]
        if axis_name == "undetected_defective_auto_approval_rate":
            return metrics["safety"]["undetected_defective_auto_approval_rate"] or 0.0
        if axis_name == "blocking_protected_routing_recall":
            return metrics["protected_routing"]["blocking_protected_routing_recall"] or 0.0
        if axis_name == "warning_protected_routing_recall":
            return metrics["protected_routing"]["warning_protected_routing_recall"] or 0.0
        if axis_name == "known_good_automatic_rejection_rate":
            return metrics["known_good_impact"]["known_good_automatic_rejection_rate"] or 0.0
        return metrics["disposition"]["human_review_rate"] or 0.0

    dominated: List[dict[str, Any]] = []
    for candidate in POLICY_NAMES:
        for dominator in POLICY_NAMES:
            if candidate == dominator:
                continue
            at_least_as_good = True
            strict_axes: List[str] = []
            for axis_name, higher_is_better in axes:
                candidate_value = axis_value(candidate, axis_name)
                dominator_value = axis_value(dominator, axis_name)
                if not _pareto_compare(candidate_value, dominator_value, higher_is_better=higher_is_better):
                    at_least_as_good = False
                    break
                if _pareto_strictly_better(
                    candidate_value, dominator_value, higher_is_better=higher_is_better
                ):
                    strict_axes.append(axis_name)
            if at_least_as_good and strict_axes:
                dominated.append(
                    {
                        "dominated_policy": candidate,
                        "dominating_policy": dominator,
                        "strict_improvement_axes": strict_axes,
                    }
                )

    non_dominated = [
        policy_name
        for policy_name in POLICY_NAMES
        if not any(entry["dominated_policy"] == policy_name for entry in dominated)
    ]
    return {
        "dominated_policies": dominated,
        "non_dominated_policies": non_dominated,
    }


def compute_non_dominated_tradeoffs(
    case_matrix: Sequence[Mapping[str, Any]],
    non_dominated_policies: Sequence[str],
) -> List[dict[str, Any]]:
    tradeoffs: List[dict[str, Any]] = []
    if len(non_dominated_policies) < 2:
        return tradeoffs

    for index, left_policy in enumerate(non_dominated_policies):
        for right_policy in non_dominated_policies[index + 1 :]:
            differing_case_ids = sorted(
                row["case_id"]
                for row in case_matrix
                if row[f"{left_policy}_disposition"] != row[f"{right_policy}_disposition"]
            )
            if differing_case_ids:
                tradeoffs.append(
                    {
                        "policy_a": left_policy,
                        "policy_b": right_policy,
                        "differing_case_ids": differing_case_ids,
                    }
                )
    return tradeoffs


def choose_conclusion(policy_metrics: Mapping[str, Mapping[str, Any]], pareto: Mapping[str, Any]) -> str:
    non_dominated = pareto["non_dominated_policies"]
    if not non_dominated:
        return "NO POLICY IS SAFE ENOUGH — ENGINE WORK REQUIRED"

    perfect_candidates = [
        policy_name
        for policy_name in non_dominated
        if policy_metrics[policy_name]["safety"]["undetected_defective_cases_automatically_approved"] == 0
        and policy_metrics[policy_name]["known_good_impact"]["known_good_automatically_rejected"] == 0
    ]
    if len(perfect_candidates) == 1:
        return f"FREEZE POLICY: {perfect_candidates[0]}"

    zero_undetected_auto_approval_policies = [
        policy_name
        for policy_name in POLICY_NAMES
        if policy_metrics[policy_name]["safety"]["undetected_defective_cases_automatically_approved"] == 0
    ]
    if zero_undetected_auto_approval_policies:
        return "POLICY TRADEOFF REQUIRES SONNET HIGH DECISION REVIEW"

    return "NO POLICY IS SAFE ENOUGH — ENGINE WORK REQUIRED"


def passive_reconciled_sanity_check(
    reconciled_path: Path,
    case_matrix: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    payload = load_json(reconciled_path)
    rows = payload.get("rows") or payload.get("cases") or payload
    if not isinstance(rows, list):
        return {"checked": False, "reason": "unrecognized reconciled artifact shape"}

    mismatches: List[str] = []
    matrix_by_id = {row["case_id"]: row for row in case_matrix}
    for row in rows:
        case_id = row.get("case_id")
        if case_id not in matrix_by_id:
            continue
        matrix_row = matrix_by_id[case_id]
        for provider_key, disposition_key in (
            ("sonnet", "sonnet_normalized_disposition"),
            ("openai", "openai_normalized_disposition"),
        ):
            reconciled_value = row.get(f"{provider_key}_normalized_disposition") or row.get(
                f"{provider_key}_disposition"
            )
            if reconciled_value and reconciled_value != matrix_row[disposition_key]:
                mismatches.append(
                    f"{case_id}:{provider_key} reconciled={reconciled_value} rebuilt={matrix_row[disposition_key]}"
                )
    return {"checked": True, "mismatch_count": len(mismatches), "mismatches": mismatches[:20]}


def render_markdown_report(payload: Mapping[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# V59 Provider Policy Evaluation")
    lines.append("")
    lines.append(f"- Evaluator version: `{payload['run_metadata']['evaluator_version']}`")
    lines.append(f"- Executed at: `{payload['run_metadata']['executed_at_utc']}`")
    lines.append(f"- Conclusion: `{payload['conclusion']}`")
    lines.append("")

    lines.append("## Provider disagreement")
    lines.append("")
    lines.append(
        f"- Provider disagreement count: {payload['operational_context']['provider_disagreement_count']}"
    )
    lines.append("")

    lines.append("## Policy comparison (disposition)")
    lines.append("")
    header = (
        "| Policy | Approve | Reject | HR | Undetected auto-approve | Blocking protected | "
        "Warning protected | KG auto-reject | HR rate |"
    )
    lines.append(header)
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for policy_name in POLICY_NAMES:
        metrics = payload["policies"][policy_name]
        lines.append(
            "| {policy} | {approve}/{approve_rate:.0%} | {reject}/{reject_rate:.0%} | "
            "{hr}/{hr_rate:.0%} | {undetected:.0%} | {blocking_pr:.0%} | "
            "{warning_pr:.0%} | {kg_reject:.0%} | {hr_rate:.0%} |".format(
                policy=policy_name,
                approve=metrics["disposition"]["automatic_approval_count"],
                approve_rate=metrics["disposition"]["automatic_approval_rate"],
                reject=metrics["disposition"]["automatic_rejection_count"],
                reject_rate=metrics["disposition"]["automatic_rejection_rate"],
                hr=metrics["disposition"]["human_review_count"],
                hr_rate=metrics["disposition"]["human_review_rate"],
                undetected=metrics["safety"]["undetected_defective_auto_approval_rate"] or 0,
                blocking_pr=metrics["protected_routing"]["blocking_protected_routing_recall"] or 0,
                warning_pr=metrics["protected_routing"]["warning_protected_routing_recall"] or 0,
                kg_reject=metrics["known_good_impact"]["known_good_automatic_rejection_rate"] or 0,
            )
        )
    lines.append("")

    lines.append("## Detection metrics (canonical scorer; independent of disposition)")
    lines.append("")
    lines.append(
        "| Policy | Source | Scored | Blocking recall | Warning recall | Overall recall | "
        "Detection FA | Detection FA rate |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for policy_name in POLICY_NAMES:
        detection = payload["policies"][policy_name]["detection"]
        lines.append(
            "| {policy} | {source} | {scored} | {blocking:.0%} | {warning:.0%} | {overall:.0%} | "
            "{fa} | {fa_rate:.0%} |".format(
                policy=policy_name,
                source=detection["detection_finding_source"],
                scored=detection["scored_cases_for_detection"],
                blocking=detection["blocking_recall"] or 0,
                warning=detection["warning_recall"] or 0,
                overall=detection["overall_recall"] or 0,
                fa=detection["detection_false_approvals"],
                fa_rate=detection["detection_false_approval_rate"] or 0,
            )
        )
    lines.append("")

    lines.append("## Pareto analysis")
    lines.append("")
    pareto = payload["pareto_analysis"]
    if pareto["dominated_policies"]:
        for entry in pareto["dominated_policies"]:
            axes = ", ".join(entry["strict_improvement_axes"])
            lines.append(
                f"- `{entry['dominated_policy']}` dominated by `{entry['dominating_policy']}` "
                f"on: {axes}"
            )
    else:
        lines.append("- No dominated policies")
    lines.append(
        "- Non-dominated policies: "
        + ", ".join(f"`{name}`" for name in pareto["non_dominated_policies"])
    )
    lines.append("")

    lines.append("## Non-dominated tradeoffs")
    lines.append("")
    if payload["non_dominated_tradeoffs"]:
        for tradeoff in payload["non_dominated_tradeoffs"]:
            case_ids = ", ".join(tradeoff["differing_case_ids"])
            lines.append(
                f"- `{tradeoff['policy_a']}` vs `{tradeoff['policy_b']}`: {case_ids}"
            )
    else:
        lines.append("- No differing case IDs among non-dominated policies")
    lines.append("")

    lines.append("## Input integrity")
    lines.append("")
    for label, digest in payload["input_file_sha256"].items():
        lines.append(f"- {label}: `{digest}`")
    lines.append("")
    return "\n".join(lines)


def run_policy_evaluation(
    *,
    fixture_path: Path,
    openai_artifact_path: Path,
    sonnet_artifact_path: Path,
    output_parent: Path,
    openai_scorecard_path: Path,
    reconciled_comparison_path: Path,
    expected_fixture_sha256: str = EXPECTED_FIXTURE_SHA256,
    executed_at_utc: Optional[str] = None,
) -> dict[str, Any]:
    for policy_fn in POLICY_FUNCTIONS.values():
        if not policy_function_has_no_sme_parameters(policy_fn):
            raise PolicyEvaluatorError("Policy function exposes SME parameters")

    verify_fixture_sha256(fixture_path, expected_fixture_sha256)

    input_hashes = {
        "openai_predictions": sha256_file(openai_artifact_path),
        "openai_scorecard": sha256_file(openai_scorecard_path),
        "reconciled_comparison": sha256_file(reconciled_comparison_path),
        "sonnet_predictions": sha256_file(sonnet_artifact_path),
        "sme_fixture_file": sha256_file(fixture_path),
        "source_fixture_sha256": expected_fixture_sha256,
    }

    sme_cases = load_sme_cases(fixture_path)
    sonnet_predictions = load_prediction_map(sonnet_artifact_path)
    openai_predictions = load_prediction_map(openai_artifact_path)

    alignment = validate_case_alignment(sme_cases, sonnet_predictions, openai_predictions)
    case_matrix = build_case_matrix(sme_cases, sonnet_predictions, openai_predictions)
    if len(case_matrix) != 40:
        raise PolicyEvaluatorError(f"Expected 40 case-matrix rows, found {len(case_matrix)}")

    policy_detection_metrics = {
        policy_name: _compute_policy_detection_metrics(
            policy_name, sme_cases, sonnet_predictions, openai_predictions
        )
        for policy_name in POLICY_NAMES
    }
    policy_metrics = {
        policy_name: derive_policy_metrics(
            case_matrix,
            policy_name,
            detection_metrics=policy_detection_metrics[policy_name],
        )
        for policy_name in POLICY_NAMES
    }
    provider_disagreement_count = compute_provider_disagreement_count(case_matrix)
    pareto = analyze_pareto_dominance(policy_metrics)
    tradeoffs = compute_non_dominated_tradeoffs(case_matrix, pareto["non_dominated_policies"])
    conclusion = choose_conclusion(policy_metrics, pareto)
    reconciled_check = passive_reconciled_sanity_check(reconciled_comparison_path, case_matrix)

    executed_at = executed_at_utc or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = output_parent / executed_at
    output_dir.mkdir(parents=True, exist_ok=False)

    payload = {
        "run_metadata": {
            "evaluator_version": EVALUATOR_VERSION,
            "executed_at_utc": executed_at,
            "fixture_path": str(fixture_path),
            "openai_predictions_path": str(openai_artifact_path),
            "sonnet_predictions_path": str(sonnet_artifact_path),
            "openai_scorecard_path": str(openai_scorecard_path),
            "reconciled_comparison_path": str(reconciled_comparison_path),
        },
        "input_file_sha256": input_hashes,
        "integrity_checks": {
            **alignment,
            "source_fixture_sha256_verified": True,
            "expected_source_fixture_sha256": expected_fixture_sha256,
        },
        "reconciled_sanity_check": reconciled_check,
        "operational_context": {
            "provider_disagreement_count": provider_disagreement_count,
        },
        "policies": policy_metrics,
        "pareto_analysis": pareto,
        "non_dominated_tradeoffs": tradeoffs,
        "conclusion": conclusion,
    }

    case_matrix_path = output_dir / "case_matrix.json"
    evaluation_json_path = output_dir / "policy_evaluation.json"
    evaluation_md_path = output_dir / "policy_evaluation.md"

    with case_matrix_path.open("w", encoding="utf-8") as handle:
        json.dump(case_matrix, handle, indent=2)
        handle.write("\n")

    with evaluation_json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")

    with evaluation_md_path.open("w", encoding="utf-8") as handle:
        handle.write(render_markdown_report(payload))

    return {
        "output_directory": str(output_dir),
        "case_matrix_path": str(case_matrix_path),
        "policy_evaluation_json_path": str(evaluation_json_path),
        "policy_evaluation_md_path": str(evaluation_md_path),
        "payload": payload,
        "case_matrix": case_matrix,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline V59 provider policy evaluation")
    parser.add_argument("--fixture", default=str(_DEFAULT_FIXTURE))
    parser.add_argument("--openai-predictions", default=str(_DEFAULT_OPENAI))
    parser.add_argument("--sonnet-predictions", default=str(_DEFAULT_SONNET))
    parser.add_argument("--openai-scorecard", default=str(_DEFAULT_OPENAI_SCORECARD))
    parser.add_argument("--reconciled-comparison", default=str(_DEFAULT_RECONCILED))
    parser.add_argument("--output-parent", default=str(_DEFAULT_OUTPUT_PARENT))
    parser.add_argument("--expected-fixture-sha256", default=EXPECTED_FIXTURE_SHA256)
    args = parser.parse_args(argv)

    try:
        result = run_policy_evaluation(
            fixture_path=Path(args.fixture),
            openai_artifact_path=Path(args.openai_predictions),
            sonnet_artifact_path=Path(args.sonnet_predictions),
            output_parent=Path(args.output_parent),
            openai_scorecard_path=Path(args.openai_scorecard),
            reconciled_comparison_path=Path(args.reconciled_comparison),
            expected_fixture_sha256=args.expected_fixture_sha256,
        )
    except PolicyEvaluatorError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    except FileExistsError:
        print("BLOCKED: output directory already exists for this timestamp", file=sys.stderr)
        return 1

    print(f"output_directory: {result['output_directory']}")
    print(f"conclusion: {result['payload']['conclusion']}")
    print("provider_calls_made: false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
