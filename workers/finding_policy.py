"""
Finding materiality and canonical-code policy for CertBound audits (V45 Phase 3).

Materiality is assigned deterministically in code after LLM response validation.
The model must not control publication impact. Canonical finding codes replace
arbitrary LLM-generated codes; the original code is preserved in metadata.
"""

from __future__ import annotations

from typing import Dict, List

ALLOWED_MATERIALITY = frozenset({"blocking", "warning", "informational"})

MATERIALITY_RANK: Dict[str, int] = {
    "informational": 0,
    "warning":     1,
    "blocking":    2,
}

CANONICAL_FINDING_CODES = frozenset({
    "WRONG_ANSWER_KEY",
    "AMBIGUOUS_QUESTION",
    "MULTIPLE_DEFENSIBLE_ANSWERS",
    "UNSUPPORTED_ANSWER",
    "EXPLANATION_MISSING",
    "EXPLANATION_INCOMPLETE",
    "WEAK_DISTRACTORS",
    "LOW_COGNITIVE_LEVEL",
    "DIFFICULTY_MISMATCH",
    "OUTDATED_CONTENT",
    "SOURCE_SUPPORT_WEAK",
    "OTHER_REVIEW_NEEDED",
    # Reused deterministic structural codes
    "EMPTY_QUESTION_TEXT",
    "INVALID_SELECT_COUNT",
    "TOO_FEW_OPTIONS",
    "EMPTY_OPTION_TEXT",
    "DUPLICATE_OPTION_LABELS",
    "DUPLICATE_OPTION_TEXT",
    "CORRECT_COUNT_MISMATCH",
    "SINGLE_SELECT_COUNT_MISMATCH",
    "DUPLICATE_CORRECT_OPTIONS",
    "OPTION_DISPLAY_ORDER_ISSUES",
})

DETERMINISTIC_TO_CANONICAL: Dict[str, str] = {
    "MISSING_EXPLANATION": "EXPLANATION_MISSING",
}

BLOCKING_CODES = frozenset({
    "WRONG_ANSWER_KEY",
    "UNSUPPORTED_ANSWER",
    "MULTIPLE_DEFENSIBLE_ANSWERS",
    "EXPLANATION_MISSING",
    "OUTDATED_CONTENT",
    "EMPTY_QUESTION_TEXT",
    "INVALID_SELECT_COUNT",
    "TOO_FEW_OPTIONS",
    "EMPTY_OPTION_TEXT",
    "DUPLICATE_OPTION_LABELS",
    "DUPLICATE_OPTION_TEXT",
    "CORRECT_COUNT_MISMATCH",
    "SINGLE_SELECT_COUNT_MISMATCH",
    "DUPLICATE_CORRECT_OPTIONS",
    "OPTION_DISPLAY_ORDER_ISSUES",
})

WARNING_CODES = frozenset({
    "AMBIGUOUS_QUESTION",
    "EXPLANATION_INCOMPLETE",
    "WEAK_DISTRACTORS",
    "SOURCE_SUPPORT_WEAK",
    "LOW_COGNITIVE_LEVEL",
    "DIFFICULTY_MISMATCH",
})

INFORMATIONAL_CODES = frozenset()


def _signal_text(finding: dict) -> str:
    parts = [
        str(finding.get("finding_code", "")),
        str(finding.get("title", "")),
        str(finding.get("description", "")),
    ]
    return " ".join(parts).lower()


def canonicalize_llm_finding_code(finding: dict) -> str:
    """Map an LLM finding to a stable canonical code."""
    original = str(finding.get("finding_code", "")).strip()
    if original in CANONICAL_FINDING_CODES:
        return original

    text = _signal_text(finding)
    finding_type = finding.get("finding_type", "")

    if finding_type == "correctness":
        if any(token in text for token in (
            "wrong answer", "incorrect answer", "answer key", "marked correct",
            "incorrect option", "wrong option", "incorrect key",
        )):
            return "WRONG_ANSWER_KEY"
        if any(token in text for token in ("unsupported", "contradict", "not supported")):
            return "UNSUPPORTED_ANSWER"
        if any(token in text for token in (
            "correct count", "answer count", "select count", "too many correct",
        )):
            return "CORRECT_COUNT_MISMATCH"
        return "OTHER_REVIEW_NEEDED"

    if finding_type == "ambiguity":
        if any(token in text for token in (
            "multiple", "defensible", "more than one", "several approaches",
        )):
            return "MULTIPLE_DEFENSIBLE_ANSWERS"
        return "AMBIGUOUS_QUESTION"

    if finding_type == "answer_quality":
        if any(token in text for token in (
            "distractor", "weak option", "implausible", "unrealistic",
        )):
            return "WEAK_DISTRACTORS"
        return "OTHER_REVIEW_NEEDED"

    if finding_type == "explanation_quality":
        if any(token in text for token in ("missing", "empty", "no explanation")):
            return "EXPLANATION_MISSING"
        return "EXPLANATION_INCOMPLETE"

    if finding_type == "cognitive_level":
        return "LOW_COGNITIVE_LEVEL"

    if finding_type == "difficulty":
        return "DIFFICULTY_MISMATCH"

    if finding_type == "outdated":
        return "OUTDATED_CONTENT"

    if finding_type == "source_support":
        return "SOURCE_SUPPORT_WEAK"

    if finding_type == "coverage":
        if any(token in text for token in ("not wrong", "not technically wrong", "incomplete")):
            return "SOURCE_SUPPORT_WEAK"
        return "SOURCE_SUPPORT_WEAK"

    if finding_type == "formatting":
        return "OTHER_REVIEW_NEEDED"

    if finding_type == "duplication":
        return "OTHER_REVIEW_NEEDED"

    if finding_type == "policy":
        return "OTHER_REVIEW_NEEDED"

    return "OTHER_REVIEW_NEEDED"


def canonicalize_deterministic_finding_code(finding: dict) -> str:
    """Map a deterministic finding code to its canonical equivalent."""
    original = str(finding.get("finding_code", "")).strip()
    return DETERMINISTIC_TO_CANONICAL.get(original, original)


def assign_materiality(finding: dict) -> str:
    """Assign publication materiality using deterministic policy."""
    code = str(finding.get("finding_code", "")).strip()
    if code in BLOCKING_CODES:
        return "blocking"
    if code in INFORMATIONAL_CODES:
        return "informational"
    if code in WARNING_CODES:
        return "warning"

    finding_type = finding.get("finding_type", "")
    text = _signal_text(finding)
    severity = finding.get("severity", "")

    if finding_type == "correctness":
        return "blocking"

    if finding_type == "ambiguity":
        if code == "MULTIPLE_DEFENSIBLE_ANSWERS":
            return "blocking"
        if severity in {"high", "critical"}:
            return "blocking"
        return "warning"

    if finding_type in {"answer_quality", "explanation_quality", "coverage",
                        "source_support", "difficulty", "cognitive_level"}:
        if code == "EXPLANATION_MISSING":
            return "blocking"
        return "warning"

    if finding_type == "outdated":
        if any(token in text for token in ("wrong", "incorrect", "invalid answer")):
            return "blocking"
        return "warning"

    if finding_type == "formatting":
        return "informational"

    if finding_type == "other":
        if any(token in text for token in ("style", "stylistic", "scenario-based", "enrichment")):
            return "informational"
        return "warning"

    return "warning"


def _apply_code_and_materiality(
    finding: dict,
    *,
    canonical_code: str,
    preserve_original: bool,
) -> dict:
    normalized = dict(finding)
    original_code = str(normalized.get("finding_code", "")).strip()
    meta = dict(normalized.get("metadata") or {})
    if preserve_original and original_code and original_code != canonical_code:
        meta["original_finding_code"] = original_code
    normalized["finding_code"] = canonical_code
    normalized["metadata"] = meta
    normalized["materiality"] = assign_materiality(normalized)
    return normalized


def normalize_llm_finding(finding: dict) -> dict:
    """Canonicalize and assign materiality to one validated LLM finding."""
    canonical = canonicalize_llm_finding_code(finding)
    return _apply_code_and_materiality(
        finding,
        canonical_code=canonical,
        preserve_original=True,
    )


def normalize_deterministic_finding(finding: dict) -> dict:
    """Canonicalize and assign materiality to one deterministic finding."""
    canonical = canonicalize_deterministic_finding_code(finding)
    return _apply_code_and_materiality(
        finding,
        canonical_code=canonical,
        preserve_original=True,
    )


def normalize_findings(findings: List[dict], *, source: str) -> List[dict]:
    """Normalize a list of findings from the given *source*."""
    if source == "llm":
        return [normalize_llm_finding(f) for f in findings]
    if source == "deterministic":
        return [normalize_deterministic_finding(f) for f in findings]
    raise ValueError(f"unsupported finding source: {source!r}")


def count_materiality(findings: List[dict]) -> Dict[str, int]:
    """Return counts keyed by materiality level."""
    counts = {"blocking": 0, "warning": 0, "informational": 0}
    for finding in findings:
        level = finding.get("materiality", "warning")
        if level in counts:
            counts[level] += 1
    return counts


def original_llm_codes(findings: List[dict]) -> List[str]:
    """Return original LLM codes preserved in metadata, when present."""
    codes: List[str] = []
    for finding in findings:
        meta = finding.get("metadata") or {}
        original = meta.get("original_finding_code")
        if original:
            codes.append(str(original))
    return codes
