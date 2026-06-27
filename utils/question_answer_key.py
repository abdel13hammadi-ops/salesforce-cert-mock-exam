"""Canonical answer-key resolution and validation for exam/practice questions."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from workers.deterministic_audit import (
    check_correct_count,
    check_select_count,
    check_single_select_count,
)

_RUNTIME_ANSWER_KEY_CHECKS = (
    check_select_count,
    check_single_select_count,
    check_correct_count,
)


def get_question_type(question: Dict[str, Any]) -> str:
    qtype = str(question.get("type") or question.get("question_type") or "single").strip().lower()
    return qtype if qtype in {"single", "multiple"} else "single"


def is_multiple_select(question: Dict[str, Any]) -> bool:
    return get_question_type(question) == "multiple"


def to_audit_snapshot(question: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt runtime question shapes to deterministic audit check input."""
    qtype = get_question_type(question)
    select_count = question.get("select_count")
    if qtype == "single" and not isinstance(select_count, int):
        select_count = 1

    options_raw = question.get("options") or []
    if options_raw and isinstance(options_raw[0], dict):
        options = [
            {
                "option_text": opt.get("text") or opt.get("option_text") or "",
                "is_correct": bool(opt.get("is_correct")),
            }
            for opt in options_raw
        ]
    else:
        correct_texts = {str(text) for text in (question.get("answers") or [])}
        options = [
            {"option_text": str(text), "is_correct": str(text) in correct_texts}
            for text in options_raw
        ]

    return {
        "question_type": qtype,
        "select_count": select_count,
        "options": options,
    }


def validate_question_answer_key(question: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Return validity and deterministic finding codes for contradictory answer keys."""
    snapshot = to_audit_snapshot(question)
    codes: List[str] = []
    for check in _RUNTIME_ANSWER_KEY_CHECKS:
        for finding in check(snapshot):
            code = finding.get("finding_code")
            if code:
                codes.append(str(code))
    return (not codes, codes)


def is_answer_key_valid(question: Dict[str, Any]) -> bool:
    return validate_question_answer_key(question)[0]


def resolve_required_select_count(question: Dict[str, Any]) -> int:
    """Canonical required selection count; structured select_count is the source of truth."""
    qtype = get_question_type(question)
    if qtype == "single":
        return 1

    select_count = question.get("select_count")
    if isinstance(select_count, int) and select_count > 0:
        return select_count
    raise ValueError("multiple-select question is missing a positive select_count")


def get_correct_selection(question: Dict[str, Any]) -> List[str]:
    """Return canonical correct selections in the same shape callers store user answers."""
    if question.get("correct_ids"):
        return [str(value) for value in question["correct_ids"]]
    if question.get("answers"):
        return [str(value) for value in question["answers"]]

    options = question.get("options") or []
    if options and isinstance(options[0], dict):
        if any("id" in opt for opt in options):
            return [str(opt["id"]) for opt in options if opt.get("is_correct")]
        return [str(opt.get("text") or opt.get("option_text") or "") for opt in options if opt.get("is_correct")]

    return []


def is_answer_correct(user_selection: Any, question: Dict[str, Any]) -> bool:
    """Score using the same canonical required count and correct answer key."""
    if not is_answer_key_valid(question):
        return False

    required = resolve_required_select_count(question)
    correct = get_correct_selection(question)
    selected = [str(value) for value in (user_selection or [])]

    if get_question_type(question) == "single":
        return len(selected) == 1 and set(selected) == set(correct)

    return len(selected) == required and set(selected) == set(correct)


def cap_multi_select_selection(selected: List[Any], required_count: int) -> List[Any]:
    """Keep at most the canonical number of multi-select answers."""
    if required_count < 1:
        return []
    return list(selected or [])[:required_count]
