"""
Dual-engine quality benchmark harness (V58-QUALITY-03A).

Evaluates labeled benchmark cases against the legacy deterministic + LLM + merge
path or the V48 grounded audit path using mock engine outputs. Does not call
live providers, write audit rows, or mutate production questions.

The legacy five-case calibration pilot in ``audit_calibration`` remains
unchanged; this module generalizes the same dry-run patterns for versioned
fixtures and cross-engine metrics.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from workers.ai_quality_audit_schemas import (
    AiQualityAuditValidationError,
    validate_pass_b_result,
)
from workers.deterministic_audit import run_deterministic_checks
from workers.finding_merge import merge_findings
from workers.finding_policy import count_materiality
from workers.llm_audit import LlmAuditValidationError, validate_llm_response

BENCHMARK_VERSION = "harness-v0"
ENGINE_LEGACY = "legacy"
ENGINE_V48 = "v48"
SUPPORTED_ENGINES = frozenset({ENGINE_LEGACY, ENGINE_V48})

DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "quality_benchmark_harness_v0.json"
)

REQUIRED_CASE_FIELDS = (
    "case_id",
    "benchmark_version",
    "certification",
    "domain",
    "defect_category",
    "question",
    "expected_correct_option_labels",
    "expected_finding_codes",
    "known_good",
    "resource_snapshot",
    "reviewer_label",
)

REQUIRED_REVIEWER_LABEL_FIELDS = ("known_good", "expected_finding_codes")


class BenchmarkFixtureError(ValueError):
    """Raised when a benchmark fixture or case fails schema validation."""


@dataclass(frozen=True)
class BenchmarkCaseResult:
    case_id: str
    engine: str
    benchmark_version: str
    certification: str
    domain: str
    defect_category: str
    known_good: bool
    expected_finding_codes: List[str]
    expected_materiality: Optional[str]
    finding_codes: List[str]
    blocking_count: int
    warning_count: int
    informational_count: int
    detection_success: bool
    false_approval: bool
    false_rejection: bool
    error_message: Optional[str] = None


@dataclass
class BenchmarkMetrics:
    total_cases: int = 0
    known_good_cases: int = 0
    defective_cases: int = 0
    false_approvals: int = 0
    false_approval_rate: Optional[float] = None
    false_approval_note: str = ""
    false_rejections: int = 0
    false_rejection_rate: Optional[float] = None
    false_rejection_note: str = ""
    finding_precision: Optional[float] = None
    finding_precision_true_positives: int = 0
    finding_precision_false_positives: int = 0
    finding_precision_total_findings: int = 0
    finding_precision_note: str = ""
    overall_recall: Optional[float] = None
    overall_recall_detected: int = 0
    overall_recall_total: int = 0
    overall_recall_note: str = ""
    recall_by_defect_category: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    blocking_category_recall: Optional[float] = None
    blocking_category_detected: int = 0
    blocking_category_total: int = 0
    blocking_category_note: str = ""
    reviewer_agreement_cases: int = 0
    reviewer_agreement_matches: int = 0
    reviewer_agreement_rate: Optional[float] = None
    reviewer_agreement_note: str = ""


@dataclass
class BenchmarkRunReport:
    benchmark_version: str
    engine: str
    ruleset_version: str
    prompt_version: str
    model_name: str
    case_count: int
    execution_timestamp: str
    case_results: List[BenchmarkCaseResult] = field(default_factory=list)
    metrics: BenchmarkMetrics = field(default_factory=BenchmarkMetrics)


def _rate(numerator: int, denominator: int, label: str) -> Tuple[Optional[float], str]:
    note = f"{numerator}/{denominator} {label}"
    if denominator == 0:
        return None, note
    return round(numerator / denominator, 6), note


def _finding_codes(findings: Sequence[Mapping[str, Any]]) -> List[str]:
    return [str(item.get("finding_code", "")) for item in findings if item.get("finding_code")]


def _blocking_findings(findings: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return [item for item in findings if item.get("materiality") == "blocking"]


def _allowed_option_labels(question: Mapping[str, Any]) -> List[str]:
    options = question.get("options")
    if not isinstance(options, list):
        raise BenchmarkFixtureError("question.options must be a JSON array")
    labels: List[str] = []
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            raise BenchmarkFixtureError(f"question.options[{index}] must be a JSON object")
        label = option.get("option_label")
        if not isinstance(label, str) or not label.strip():
            raise BenchmarkFixtureError(
                f"question.options[{index}].option_label must be a non-empty string"
            )
        labels.append(label.strip())
    if not labels:
        raise BenchmarkFixtureError("question.options must contain at least one option")
    return labels


def _required_selection_count(question: Mapping[str, Any]) -> int:
    select_count = question.get("select_count")
    if isinstance(select_count, int) and select_count > 0:
        return select_count
    question_type = str(question.get("question_type", "single")).lower()
    if question_type == "single":
        return 1
    raise BenchmarkFixtureError(
        "question.select_count must be a positive integer for multi-select questions"
    )


def _frozen_chunk_ids(resource_snapshot: Mapping[str, Any]) -> Set[str]:
    chunks = resource_snapshot.get("chunks")
    if not isinstance(chunks, list):
        raise BenchmarkFixtureError("resource_snapshot.chunks must be a JSON array")
    chunk_ids: Set[str] = set()
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise BenchmarkFixtureError(
                f"resource_snapshot.chunks[{index}] must be a JSON object"
            )
        chunk_id = chunk.get("resource_chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            raise BenchmarkFixtureError(
                f"resource_snapshot.chunks[{index}].resource_chunk_id "
                "must be a non-empty string"
            )
        chunk_ids.add(chunk_id.strip().lower())
    return chunk_ids


def _validate_reviewer_label(raw: object, *, prefix: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise BenchmarkFixtureError(f"{prefix} must be a JSON object")
    for key in REQUIRED_REVIEWER_LABEL_FIELDS:
        if key not in raw:
            raise BenchmarkFixtureError(f"{prefix} missing required field {key!r}")
    known_good = raw.get("known_good")
    if not isinstance(known_good, bool):
        raise BenchmarkFixtureError(f"{prefix}.known_good must be a boolean")
    expected_codes = raw.get("expected_finding_codes")
    if not isinstance(expected_codes, list):
        raise BenchmarkFixtureError(
            f"{prefix}.expected_finding_codes must be a JSON array"
        )
    normalized_codes = []
    for index, code in enumerate(expected_codes):
        if not isinstance(code, str) or not code.strip():
            raise BenchmarkFixtureError(
                f"{prefix}.expected_finding_codes[{index}] must be a non-empty string"
            )
        normalized_codes.append(code.strip())
    return {
        "known_good": known_good,
        "expected_finding_codes": normalized_codes,
    }


def validate_benchmark_case(case: Mapping[str, Any], *, fixture_version: str) -> Dict[str, Any]:
    """Validate one benchmark case and return a normalized copy."""
    if not isinstance(case, dict):
        raise BenchmarkFixtureError("Each benchmark case must be a JSON object")

    for key in REQUIRED_CASE_FIELDS:
        if key not in case:
            raise BenchmarkFixtureError(f"Benchmark case missing required field {key!r}")

    case_id = case["case_id"]
    if not isinstance(case_id, str) or not case_id.strip():
        raise BenchmarkFixtureError("case_id must be a non-empty string")

    benchmark_version = case["benchmark_version"]
    if not isinstance(benchmark_version, str) or not benchmark_version.strip():
        raise BenchmarkFixtureError(f"case {case_id!r} benchmark_version must be non-empty")
    if benchmark_version.strip() != fixture_version:
        raise BenchmarkFixtureError(
            f"case {case_id!r} benchmark_version={benchmark_version!r} "
            f"does not match fixture benchmark_version={fixture_version!r}"
        )

    for scalar_key in ("certification", "domain", "defect_category"):
        value = case[scalar_key]
        if not isinstance(value, str) or not value.strip():
            raise BenchmarkFixtureError(
                f"case {case_id!r} {scalar_key} must be a non-empty string"
            )

    question = case["question"]
    if not isinstance(question, dict):
        raise BenchmarkFixtureError(f"case {case_id!r} question must be a JSON object")

    expected_labels = case["expected_correct_option_labels"]
    if not isinstance(expected_labels, list) or not expected_labels:
        raise BenchmarkFixtureError(
            f"case {case_id!r} expected_correct_option_labels must be a non-empty array"
        )
    normalized_labels: List[str] = []
    allowed_labels = set(_allowed_option_labels(question))
    for index, label in enumerate(expected_labels):
        if not isinstance(label, str) or not label.strip():
            raise BenchmarkFixtureError(
                f"case {case_id!r} expected_correct_option_labels[{index}] "
                "must be a non-empty string"
            )
        normalized_label = label.strip()
        if normalized_label not in allowed_labels:
            raise BenchmarkFixtureError(
                f"case {case_id!r} expected_correct_option_labels[{index}]={normalized_label!r} "
                f"is not present in question.options"
            )
        normalized_labels.append(normalized_label)

    expected_finding_codes = case["expected_finding_codes"]
    if not isinstance(expected_finding_codes, list):
        raise BenchmarkFixtureError(
            f"case {case_id!r} expected_finding_codes must be a JSON array"
        )
    normalized_expected_codes: List[str] = []
    for index, code in enumerate(expected_finding_codes):
        if not isinstance(code, str) or not code.strip():
            raise BenchmarkFixtureError(
                f"case {case_id!r} expected_finding_codes[{index}] must be a non-empty string"
            )
        normalized_expected_codes.append(code.strip())

    known_good = case["known_good"]
    if not isinstance(known_good, bool):
        raise BenchmarkFixtureError(f"case {case_id!r} known_good must be a boolean")

    expected_materiality = case.get("expected_materiality")
    if expected_materiality is not None:
        if not isinstance(expected_materiality, str) or not expected_materiality.strip():
            raise BenchmarkFixtureError(
                f"case {case_id!r} expected_materiality must be a non-empty string when set"
            )
        expected_materiality = expected_materiality.strip()
    elif not known_good and normalized_expected_codes:
        expected_materiality = "blocking"

    resource_snapshot = case["resource_snapshot"]
    if not isinstance(resource_snapshot, dict):
        raise BenchmarkFixtureError(
            f"case {case_id!r} resource_snapshot must be a JSON object"
        )
    _frozen_chunk_ids(resource_snapshot)

    reviewer_label = _validate_reviewer_label(
        case["reviewer_label"],
        prefix=f"case {case_id!r} reviewer_label",
    )

    second_reviewer_label: Optional[Dict[str, Any]] = None
    if "second_reviewer_label" in case:
        second_reviewer_label = _validate_reviewer_label(
            case["second_reviewer_label"],
            prefix=f"case {case_id!r} second_reviewer_label",
        )

    normalized = dict(case)
    normalized["case_id"] = case_id.strip()
    normalized["benchmark_version"] = benchmark_version.strip()
    normalized["certification"] = case["certification"].strip()
    normalized["domain"] = case["domain"].strip()
    normalized["defect_category"] = case["defect_category"].strip()
    normalized["expected_correct_option_labels"] = normalized_labels
    normalized["expected_finding_codes"] = normalized_expected_codes
    normalized["expected_materiality"] = expected_materiality
    normalized["reviewer_label"] = reviewer_label
    if second_reviewer_label is not None:
        normalized["second_reviewer_label"] = second_reviewer_label
    return normalized


def load_benchmark_fixture(path: Path | str) -> dict:
    """Load and validate a versioned benchmark fixture file."""
    fixture_path = Path(path)
    with fixture_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise BenchmarkFixtureError("Benchmark fixture root must be a JSON object")

    benchmark_version = data.get("benchmark_version")
    if not isinstance(benchmark_version, str) or not benchmark_version.strip():
        raise BenchmarkFixtureError("Benchmark fixture missing benchmark_version")

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BenchmarkFixtureError("Benchmark fixture must contain a non-empty 'cases' array")

    seen_case_ids: Set[str] = set()
    normalized_cases: List[Dict[str, Any]] = []
    for index, case in enumerate(cases):
        try:
            normalized = validate_benchmark_case(case, fixture_version=benchmark_version.strip())
        except BenchmarkFixtureError as exc:
            raise BenchmarkFixtureError(f"cases[{index}]: {exc}") from exc
        if normalized["case_id"] in seen_case_ids:
            raise BenchmarkFixtureError(
                f"Duplicate case_id {normalized['case_id']!r} in benchmark fixture"
            )
        seen_case_ids.add(normalized["case_id"])
        normalized_cases.append(normalized)

    normalized_fixture = dict(data)
    normalized_fixture["benchmark_version"] = benchmark_version.strip()
    normalized_fixture["cases"] = normalized_cases
    return normalized_fixture


def _evaluate_detection(
    *,
    known_good: bool,
    expected_finding_codes: Sequence[str],
    expected_materiality: Optional[str],
    findings: Sequence[Mapping[str, Any]],
) -> Tuple[bool, bool, bool]:
    """Return (detection_success, false_approval, false_rejection)."""
    blocking = _blocking_findings(findings)

    if known_good:
        false_rejection = len(blocking) > 0
        return (not false_rejection, False, false_rejection)

    if not expected_finding_codes:
        detection_success = len(findings) > 0
        return (detection_success, not detection_success, False)

    matched_codes = {
        item.get("finding_code")
        for item in findings
        if item.get("finding_code") in expected_finding_codes
    }
    if expected_materiality:
        matched_codes = {
            item.get("finding_code")
            for item in findings
            if item.get("finding_code") in expected_finding_codes
            and item.get("materiality") == expected_materiality
        }

    detection_success = set(expected_finding_codes) <= matched_codes
    false_approval = not detection_success
    return (detection_success, false_approval, False)


def _build_case_result(
    case: Mapping[str, Any],
    *,
    engine: str,
    findings: Sequence[Mapping[str, Any]],
    error_message: Optional[str] = None,
) -> BenchmarkCaseResult:
    counts = count_materiality(list(findings))
    detection_success, false_approval, false_rejection = _evaluate_detection(
        known_good=bool(case["known_good"]),
        expected_finding_codes=case["expected_finding_codes"],
        expected_materiality=case.get("expected_materiality"),
        findings=findings,
    )
    return BenchmarkCaseResult(
        case_id=str(case["case_id"]),
        engine=engine,
        benchmark_version=str(case["benchmark_version"]),
        certification=str(case["certification"]),
        domain=str(case["domain"]),
        defect_category=str(case["defect_category"]),
        known_good=bool(case["known_good"]),
        expected_finding_codes=list(case["expected_finding_codes"]),
        expected_materiality=case.get("expected_materiality"),
        finding_codes=_finding_codes(findings),
        blocking_count=counts["blocking"],
        warning_count=counts["warning"],
        informational_count=counts["informational"],
        detection_success=detection_success,
        false_approval=false_approval,
        false_rejection=false_rejection,
        error_message=error_message,
    )


class LegacyBenchmarkAdapter:
    """Evaluate a case through deterministic checks + mocked LLM + merge."""

    def evaluate(
        self,
        case: Mapping[str, Any],
        *,
        ruleset_version: str,
    ) -> BenchmarkCaseResult:
        question = case["question"]
        ruleset = case.get("ruleset_version") or ruleset_version
        det_findings = run_deterministic_checks(question, ruleset)

        legacy_mock = case.get("legacy_mock")
        if not isinstance(legacy_mock, dict):
            raise BenchmarkFixtureError(
                f"case {case['case_id']!r} missing legacy_mock for mock legacy evaluation"
            )
        llm_raw = legacy_mock.get("llm_findings")
        if llm_raw is None:
            llm_raw = []
        if not isinstance(llm_raw, list):
            raise BenchmarkFixtureError(
                f"case {case['case_id']!r} legacy_mock.llm_findings must be a JSON array"
            )

        try:
            llm_findings = validate_llm_response({"findings": llm_raw})
        except LlmAuditValidationError as exc:
            return _build_case_result(
                case,
                engine=ENGINE_LEGACY,
                findings=merge_findings(det_findings, []),
                error_message=str(exc),
            )

        merged = merge_findings(det_findings, llm_findings)
        return _build_case_result(case, engine=ENGINE_LEGACY, findings=merged)


class V48BenchmarkAdapter:
    """Evaluate a case using validated V48 Pass B mock output."""

    def evaluate(self, case: Mapping[str, Any]) -> BenchmarkCaseResult:
        v48_mock = case.get("v48_mock")
        if not isinstance(v48_mock, dict):
            raise BenchmarkFixtureError(
                f"case {case['case_id']!r} missing v48_mock for mock V48 evaluation"
            )
        pass_b = v48_mock.get("pass_b")
        if not isinstance(pass_b, dict):
            raise BenchmarkFixtureError(
                f"case {case['case_id']!r} v48_mock.pass_b must be a JSON object"
            )

        question = case["question"]
        allowed_labels = _allowed_option_labels(question)
        frozen_ids = _frozen_chunk_ids(case["resource_snapshot"])
        required_count = _required_selection_count(question)

        try:
            validated = validate_pass_b_result(
                pass_b,
                allowed_option_labels=allowed_labels,
                required_selection_count=required_count,
                frozen_evidence_chunk_ids=frozen_ids,
            )
        except AiQualityAuditValidationError as exc:
            return _build_case_result(
                case,
                engine=ENGINE_V48,
                findings=[],
                error_message=str(exc),
            )

        findings = validated["proposed_findings"]
        return _build_case_result(case, engine=ENGINE_V48, findings=findings)


def _reviewer_labels_agree(case: Mapping[str, Any]) -> Optional[bool]:
    second = case.get("second_reviewer_label")
    if second is None:
        return None
    primary = case["reviewer_label"]
    return (
        primary["known_good"] == second["known_good"]
        and set(primary["expected_finding_codes"]) == set(second["expected_finding_codes"])
    )


def compute_benchmark_metrics(
    cases: Sequence[Mapping[str, Any]],
    case_results: Sequence[BenchmarkCaseResult],
) -> BenchmarkMetrics:
    """Aggregate benchmark metrics from per-case engine results."""
    results_by_id = {result.case_id: result for result in case_results}
    metrics = BenchmarkMetrics(total_cases=len(case_results))

    precision_tp = 0
    precision_fp = 0
    precision_total = 0

    recall_detected = 0
    recall_total = 0

    blocking_detected = 0
    blocking_total = 0

    category_stats: Dict[str, Dict[str, int]] = {}
    reviewer_cases = 0
    reviewer_matches = 0

    for case in cases:
        case_id = case["case_id"]
        result = results_by_id[case_id]
        known_good = bool(case["known_good"])
        if known_good:
            metrics.known_good_cases += 1
        else:
            metrics.defective_cases += 1

        if result.false_approval:
            metrics.false_approvals += 1
        if result.false_rejection:
            metrics.false_rejections += 1

        expected_codes = set(case["expected_finding_codes"])
        for finding_code in result.finding_codes:
            precision_total += 1
            if known_good:
                precision_fp += 1
            elif finding_code in expected_codes:
                precision_tp += 1
            else:
                precision_fp += 1

        if not known_good:
            recall_total += 1
            if result.detection_success:
                recall_detected += 1

            category = str(case["defect_category"])
            bucket = category_stats.setdefault(category, {"detected": 0, "total": 0})
            bucket["total"] += 1
            if result.detection_success:
                bucket["detected"] += 1

            if case.get("expected_materiality") == "blocking":
                blocking_total += 1
                if result.detection_success:
                    blocking_detected += 1

        agreement = _reviewer_labels_agree(case)
        if agreement is not None:
            reviewer_cases += 1
            if agreement:
                reviewer_matches += 1

    metrics.false_approval_rate, metrics.false_approval_note = _rate(
        metrics.false_approvals,
        metrics.defective_cases,
        "defective cases missed",
    )
    metrics.false_rejection_rate, metrics.false_rejection_note = _rate(
        metrics.false_rejections,
        metrics.known_good_cases,
        "known-good cases blocked",
    )

    metrics.finding_precision_true_positives = precision_tp
    metrics.finding_precision_false_positives = precision_fp
    metrics.finding_precision_total_findings = precision_total
    metrics.finding_precision, metrics.finding_precision_note = _rate(
        precision_tp,
        precision_total,
        "findings matched expected defect signal",
    )

    metrics.overall_recall_detected = recall_detected
    metrics.overall_recall_total = recall_total
    metrics.overall_recall, metrics.overall_recall_note = _rate(
        recall_detected,
        recall_total,
        "defective cases detected",
    )

    metrics.blocking_category_detected = blocking_detected
    metrics.blocking_category_total = blocking_total
    metrics.blocking_category_recall, metrics.blocking_category_note = _rate(
        blocking_detected,
        blocking_total,
        "blocking-expected defective cases detected",
    )

    metrics.recall_by_defect_category = {}
    for category, bucket in sorted(category_stats.items()):
        detected = bucket["detected"]
        total = bucket["total"]
        recall, note = _rate(detected, total, f"{category} defective cases detected")
        metrics.recall_by_defect_category[category] = {
            "detected": detected,
            "total": total,
            "recall": recall,
            "note": note,
        }

    metrics.reviewer_agreement_cases = reviewer_cases
    metrics.reviewer_agreement_matches = reviewer_matches
    metrics.reviewer_agreement_rate, metrics.reviewer_agreement_note = _rate(
        reviewer_matches,
        reviewer_cases,
        "dual-reviewer label pairs agreeing",
    )
    return metrics


def run_quality_benchmark(
    fixture: Mapping[str, Any],
    engine: str,
    *,
    execution_timestamp: Optional[str] = None,
) -> BenchmarkRunReport:
    """Run all benchmark cases for one engine using mock adapters."""
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"Unsupported engine {engine!r}; expected one of {sorted(SUPPORTED_ENGINES)}")

    ruleset_version = str(fixture.get("ruleset_version", "1.0.0"))
    prompt_version = str(fixture.get("prompt_version", "benchmark-v0"))
    model_name = str(fixture.get("model_name", "mock-model"))
    timestamp = execution_timestamp or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    adapter: LegacyBenchmarkAdapter | V48BenchmarkAdapter
    if engine == ENGINE_LEGACY:
        adapter = LegacyBenchmarkAdapter()
    else:
        adapter = V48BenchmarkAdapter()

    case_results: List[BenchmarkCaseResult] = []
    for case in fixture["cases"]:
        if engine == ENGINE_LEGACY:
            case_results.append(adapter.evaluate(case, ruleset_version=ruleset_version))
        else:
            case_results.append(adapter.evaluate(case))

    metrics = compute_benchmark_metrics(fixture["cases"], case_results)
    return BenchmarkRunReport(
        benchmark_version=str(fixture["benchmark_version"]),
        engine=engine,
        ruleset_version=ruleset_version,
        prompt_version=prompt_version,
        model_name=model_name,
        case_count=len(case_results),
        execution_timestamp=timestamp,
        case_results=sorted(case_results, key=lambda item: item.case_id),
        metrics=metrics,
    )


def serialize_run_report(report: BenchmarkRunReport) -> Dict[str, Any]:
    """Return a deterministic JSON-serializable benchmark run report."""
    payload = asdict(report)
    payload["case_results"] = sorted(payload["case_results"], key=lambda item: item["case_id"])
    return payload


def dumps_run_report(report: BenchmarkRunReport) -> str:
    """Serialize a benchmark run report to stable JSON text."""
    return json.dumps(serialize_run_report(report), indent=2, sort_keys=True)
