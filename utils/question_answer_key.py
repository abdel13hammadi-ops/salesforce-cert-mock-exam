"""Canonical answer-key resolution and validation for exam/practice questions."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

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


EXPLANATION_GATE_HINT = "Select your answer before viewing the explanation."


def is_answer_selection_complete(user_selection: Any, question: Dict[str, Any]) -> bool:
    """Return True when the learner selected exactly the required number of options."""
    required = resolve_required_select_count(question)
    selected = _dedupe_selection([str(value) for value in (user_selection or [])])
    return len(selected) == required


def effective_explanation_feedback_shown(
    feedback_shown: bool,
    user_selection: Any,
    question: Dict[str, Any],
) -> bool:
    """Return True only when feedback was requested and the current answer is complete."""
    if not feedback_shown:
        return False
    return is_answer_selection_complete(user_selection, question)


def cap_multi_select_selection(selected: List[Any], required_count: int) -> List[Any]:
    """Keep at most the canonical number of multi-select answers."""
    return reconcile_multi_select_selection(selected, [], required_count)


def _dedupe_selection(values: List[Any]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for value in values or []:
        normalized = str(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def normalize_option_entries(options: List[Any]) -> List[Dict[str, str]]:
    """Normalize exam text options and practice dict options to a shared shape."""
    if not options:
        return []
    if isinstance(options[0], str):
        return [{"id": text, "label": text} for text in options]
    entries: List[Dict[str, str]] = []
    for opt in options:
        if isinstance(opt, dict):
            option_id = opt.get("id", opt.get("text") or opt.get("option_text"))
            label = opt.get("text") or opt.get("option_text") or str(option_id)
        else:
            option_id = opt
            label = str(opt)
        entries.append({"id": str(option_id), "label": str(label)})
    return entries


def reconcile_multi_select_selection(
    checked_ids: List[Any],
    previous_ids: List[Any],
    required_count: int,
) -> List[str]:
    """Keep at most required_count selections, rejecting new extras over a valid prior set."""
    if required_count < 1:
        return []

    checked = _dedupe_selection(checked_ids)
    previous = _dedupe_selection(previous_ids)

    if len(checked) <= required_count:
        return checked

    previous_set = set(previous)
    if (
        len(previous) <= required_count
        and len(previous) > 0
        and previous_set.issubset(set(checked))
        and len(checked) > len(previous)
    ):
        return previous

    return checked[:required_count]


def build_multi_select_checkbox_plan(
    options: List[Any],
    selected_ids: List[Any],
    required_count: int,
) -> List[Dict[str, Any]]:
    """Return per-option checkbox metadata for enforcing the selection cap in the UI."""
    selected = set(_dedupe_selection(selected_ids))
    at_limit = len(selected) >= required_count
    plan: List[Dict[str, Any]] = []
    for entry in normalize_option_entries(options):
        checked = entry["id"] in selected
        plan.append(
            {
                "id": entry["id"],
                "label": entry["label"],
                "checked": checked,
                "disabled": at_limit and not checked,
            }
        )
    return plan


def _checkbox_widget_key(key_prefix: str, option_id: str) -> str:
    return f"{key_prefix}_{option_id}"


def read_multi_select_widget_selection(
    session_state: Any,
    key_prefix: str,
    options: List[Any],
) -> List[str]:
    selected: List[str] = []
    for entry in normalize_option_entries(options):
        if session_state.get(_checkbox_widget_key(key_prefix, entry["id"])):
            selected.append(entry["id"])
    return selected


def sync_multi_select_widget_selection(
    session_state: Any,
    key_prefix: str,
    options: List[Any],
    selected_ids: List[Any],
) -> None:
    selected = set(_dedupe_selection(selected_ids))
    for entry in normalize_option_entries(options):
        session_state[_checkbox_widget_key(key_prefix, entry["id"])] = entry["id"] in selected


def apply_multi_select_answer_ui(
    question: Dict[str, Any],
    *,
    previous_selection: List[Any],
    key_prefix: str,
    session_state: Any,
    checkbox_fn: Callable[..., bool],
    warning_fn: Optional[Callable[[str], Any]] = None,
    limit_message_fn: Optional[Callable[[int], Any]] = None,
) -> List[str]:
    """Render capped multi-select checkboxes and return the canonical stored selection."""
    required_count = resolve_required_select_count(question)
    options = question.get("options") or []
    canonical_ids = reconcile_multi_select_selection(
        previous_selection or [],
        previous_selection or [],
        required_count,
    )

    widget_checked = read_multi_select_widget_selection(session_state, key_prefix, options)
    if widget_checked:
        display_ids = reconcile_multi_select_selection(widget_checked, canonical_ids, required_count)
    else:
        display_ids = list(canonical_ids)

    rejected_extra = len(widget_checked) > required_count
    if widget_checked != display_ids:
        rejected_extra = True
        # Streamlit only allows widget-key writes before the widget is instantiated.
        sync_multi_select_widget_selection(session_state, key_prefix, options, display_ids)
    elif canonical_ids and not widget_checked:
        sync_multi_select_widget_selection(session_state, key_prefix, options, display_ids)

    if warning_fn is not None:
        warning_fn(f"Choose {required_count} answers.")

    plan = build_multi_select_checkbox_plan(options, display_ids, required_count)
    checked_now: List[str] = []
    for item in plan:
        widget_key = _checkbox_widget_key(key_prefix, item["id"])
        if checkbox_fn(
            item["label"],
            value=item["checked"],
            disabled=item["disabled"],
            key=widget_key,
        ):
            checked_now.append(item["id"])

    reconciled = reconcile_multi_select_selection(checked_now, display_ids, required_count)
    if len(checked_now) > required_count or reconciled != checked_now:
        rejected_extra = True

    if rejected_extra and limit_message_fn is not None:
        limit_message_fn(required_count)

    return reconciled
