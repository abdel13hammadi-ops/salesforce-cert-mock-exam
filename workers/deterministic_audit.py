"""
CertBound deterministic audit engine (Phase 8D).

Pure functions only — no Supabase calls, no side effects.
Accepts a question snapshot dict and returns a list of finding dicts
compatible with ``complete_audit_run_v1``.

Finding codes
-------------
  EMPTY_QUESTION_TEXT          question_text is empty or whitespace
  INVALID_SELECT_COUNT         select_count is not a positive integer
  TOO_FEW_OPTIONS              fewer than 2 answer options
  EMPTY_OPTION_TEXT            one or more options have empty text
  DUPLICATE_OPTION_LABELS      duplicate option_label values
  DUPLICATE_OPTION_TEXT        duplicate option_text (normalized)
  CORRECT_COUNT_MISMATCH       number of correct answers ≠ select_count
  MISSING_EXPLANATION          explanation is missing or whitespace
  SINGLE_SELECT_COUNT_MISMATCH question_type='single' with select_count ≠ 1
  DUPLICATE_CORRECT_OPTIONS    two+ correct options with identical text
  OPTION_DISPLAY_ORDER_ISSUES  display_order has duplicates or gaps
"""

from __future__ import annotations

from typing import List

from workers.finding_policy import normalize_deterministic_finding

DETECTOR_NAME = "certbound-deterministic-audit"
DETECTOR_VERSION = "1.0.0"


# ===========================================================================
# Finding factory
# ===========================================================================

def _finding(
    finding_code: str,
    finding_type: str,
    severity: str,
    title: str,
    description: str,
    field_path: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Build a finding dict compatible with complete_audit_run_v1."""
    return {
        "finding_code":     finding_code,
        "finding_type":     finding_type,
        "severity":         severity,
        "title":            title,
        "description":      description,
        "field_path":       field_path,
        "detector_name":    DETECTOR_NAME,
        "detector_version": DETECTOR_VERSION,
        "metadata":         metadata or {},
        "evidence":         [],
    }


# ===========================================================================
# Individual checks
# ===========================================================================

def check_question_text(question: dict) -> List[dict]:
    """EMPTY_QUESTION_TEXT — question_text is empty or whitespace."""
    if not (question.get("question_text") or "").strip():
        return [_finding(
            finding_code="EMPTY_QUESTION_TEXT",
            finding_type="formatting",
            severity="critical",
            title="Question text is empty or whitespace",
            description=(
                "The question_text field must not be empty or consist "
                "only of whitespace."
            ),
            field_path="question.question_text",
        )]
    return []


def check_select_count(question: dict) -> List[dict]:
    """INVALID_SELECT_COUNT — select_count is not a positive integer."""
    sc = question.get("select_count")
    if not isinstance(sc, int) or sc < 1:
        return [_finding(
            finding_code="INVALID_SELECT_COUNT",
            finding_type="correctness",
            severity="critical",
            title="Invalid select_count",
            description=f"select_count must be a positive integer. Got: {sc!r}.",
            field_path="question.select_count",
            metadata={"select_count": sc},
        )]
    return []


def check_option_count(question: dict) -> List[dict]:
    """TOO_FEW_OPTIONS — fewer than 2 answer options."""
    options = question.get("options") or []
    if len(options) < 2:
        return [_finding(
            finding_code="TOO_FEW_OPTIONS",
            finding_type="answer_quality",
            severity="critical",
            title="Fewer than 2 answer options",
            description=f"Questions must have at least 2 options. Found {len(options)}.",
            field_path="question.options",
            metadata={"option_count": len(options)},
        )]
    return []


def check_empty_option_text(question: dict) -> List[dict]:
    """EMPTY_OPTION_TEXT — one or more options have empty or whitespace text."""
    options = question.get("options") or []
    findings = []
    for i, opt in enumerate(options):
        if not (opt.get("option_text") or "").strip():
            findings.append(_finding(
                finding_code="EMPTY_OPTION_TEXT",
                finding_type="formatting",
                severity="high",
                title=f"Option {i} has empty text",
                description=(
                    f"option_text for option at index {i} "
                    f"(label={opt.get('option_label', '?')!r}) "
                    "is empty or whitespace."
                ),
                field_path=f"question.options[{i}].option_text",
                metadata={
                    "option_index": i,
                    "option_label": opt.get("option_label"),
                },
            ))
    return findings


def check_duplicate_option_labels(question: dict) -> List[dict]:
    """DUPLICATE_OPTION_LABELS — two or more options share an option_label."""
    options = question.get("options") or []
    seen: set = set()
    duplicates: list = []
    for opt in options:
        label = opt.get("option_label", "")
        if label in seen and label not in duplicates:
            duplicates.append(label)
        seen.add(label)
    if duplicates:
        return [_finding(
            finding_code="DUPLICATE_OPTION_LABELS",
            finding_type="duplication",
            severity="high",
            title="Duplicate option labels",
            description=f"Duplicate option_label value(s): {sorted(duplicates)}.",
            field_path="question.options",
            metadata={"duplicate_labels": sorted(duplicates)},
        )]
    return []


def check_duplicate_option_text(question: dict) -> List[dict]:
    """DUPLICATE_OPTION_TEXT — two or more options share normalized text."""
    options = question.get("options") or []
    seen: set = set()
    for opt in options:
        normalized = (opt.get("option_text") or "").strip().lower()
        if normalized and normalized in seen:
            return [_finding(
                finding_code="DUPLICATE_OPTION_TEXT",
                finding_type="duplication",
                severity="high",
                title="Duplicate option text (normalized)",
                description=(
                    "Two or more options have the same text after normalization "
                    "(lowercase, trimmed whitespace)."
                ),
                field_path="question.options",
            )]
        seen.add(normalized)
    return []


def check_correct_count(question: dict) -> List[dict]:
    """CORRECT_COUNT_MISMATCH — correct answer count ≠ select_count."""
    sc = question.get("select_count")
    if not isinstance(sc, int):
        return []  # handled by check_select_count
    options = question.get("options") or []
    correct_count = sum(1 for opt in options if opt.get("is_correct"))
    if correct_count != sc:
        return [_finding(
            finding_code="CORRECT_COUNT_MISMATCH",
            finding_type="correctness",
            severity="critical",
            title="Correct answer count does not match select_count",
            description=(
                f"Found {correct_count} correct option(s) but "
                f"select_count is {sc}."
            ),
            field_path="question.options",
            metadata={"correct_count": correct_count, "select_count": sc},
        )]
    return []


def check_explanation(question: dict) -> List[dict]:
    """MISSING_EXPLANATION — explanation is missing or whitespace."""
    if not (question.get("explanation") or "").strip():
        return [_finding(
            finding_code="MISSING_EXPLANATION",
            finding_type="explanation_quality",
            severity="medium",
            title="Explanation is missing or empty",
            description=(
                "The explanation field must not be empty or consist "
                "only of whitespace."
            ),
            field_path="question.explanation",
        )]
    return []


def check_single_select_count(question: dict) -> List[dict]:
    """SINGLE_SELECT_COUNT_MISMATCH — question_type='single' with select_count ≠ 1."""
    if question.get("question_type") == "single":
        sc = question.get("select_count")
        if sc != 1:
            return [_finding(
                finding_code="SINGLE_SELECT_COUNT_MISMATCH",
                finding_type="correctness",
                severity="high",
                title="Single-select question has invalid select_count",
                description=(
                    f"question_type='single' requires select_count=1. "
                    f"Got {sc!r}."
                ),
                field_path="question.select_count",
                metadata={"question_type": "single", "select_count": sc},
            )]
    return []


def check_duplicate_correct_options(question: dict) -> List[dict]:
    """DUPLICATE_CORRECT_OPTIONS — two+ correct options with identical text."""
    options = question.get("options") or []
    seen: set = set()
    for opt in options:
        if not opt.get("is_correct"):
            continue
        normalized = (opt.get("option_text") or "").strip().lower()
        if normalized and normalized in seen:
            return [_finding(
                finding_code="DUPLICATE_CORRECT_OPTIONS",
                finding_type="duplication",
                severity="high",
                title="Duplicate correct options",
                description=(
                    "Two or more correct answer options have identical text "
                    "(after normalization)."
                ),
                field_path="question.options",
            )]
        seen.add(normalized)
    return []


def check_display_order(question: dict) -> List[dict]:
    """OPTION_DISPLAY_ORDER_ISSUES — display_order has duplicates or gaps."""
    options = question.get("options") or []
    orders = [
        opt["display_order"]
        for opt in options
        if opt.get("display_order") is not None
    ]
    if not orders:
        return []

    issues: list = []
    if len(orders) != len(set(orders)):
        issues.append("duplicate display_order values")

    sorted_orders = sorted(orders)
    expected = list(range(sorted_orders[0], sorted_orders[0] + len(sorted_orders)))
    if sorted_orders != expected:
        issues.append("non-contiguous display_order values (gaps detected)")

    if issues:
        return [_finding(
            finding_code="OPTION_DISPLAY_ORDER_ISSUES",
            finding_type="formatting",
            severity="low",
            title="Option display order has issues",
            description=f"display_order issues: {'; '.join(issues)}.",
            field_path="question.options",
            metadata={"display_orders": sorted_orders, "issues": issues},
        )]
    return []


# ===========================================================================
# Check pipeline
# ===========================================================================

_CHECKS = [
    check_question_text,
    check_select_count,
    check_option_count,
    check_empty_option_text,
    check_duplicate_option_labels,
    check_duplicate_option_text,
    check_correct_count,
    check_explanation,
    check_single_select_count,
    check_duplicate_correct_options,
    check_display_order,
]

_FINDING_CODES = frozenset(
    "EMPTY_QUESTION_TEXT INVALID_SELECT_COUNT TOO_FEW_OPTIONS "
    "EMPTY_OPTION_TEXT DUPLICATE_OPTION_LABELS DUPLICATE_OPTION_TEXT "
    "CORRECT_COUNT_MISMATCH MISSING_EXPLANATION SINGLE_SELECT_COUNT_MISMATCH "
    "DUPLICATE_CORRECT_OPTIONS OPTION_DISPLAY_ORDER_ISSUES".split()
)


def run_deterministic_checks(question: dict, ruleset_version: str = "1.0.0") -> List[dict]:
    """Run all deterministic structural checks against a question snapshot.

    Parameters
    ----------
    question:
        Question snapshot dict from the job payload.
    ruleset_version:
        Version string recorded in each finding's metadata.

    Returns a list of finding dicts compatible with ``complete_audit_run_v1``.
    An individual check failure is swallowed so other checks still run.
    """
    findings: List[dict] = []
    for check_fn in _CHECKS:
        try:
            results = check_fn(question)
            for f in results:
                f.setdefault("metadata", {})["ruleset_version"] = ruleset_version
                findings.append(normalize_deterministic_finding(f))
        except Exception:  # noqa: BLE001
            pass
    return findings
