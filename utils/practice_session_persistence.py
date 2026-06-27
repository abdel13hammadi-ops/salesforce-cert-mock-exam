"""Browser-refresh recovery for active practice sessions (URL query persistence)."""

from __future__ import annotations

import base64
import copy
import json
import time
from typing import Any, Dict, List, Optional

try:
    import streamlit as st
except Exception:  # pragma: no cover - import guard for unit tests
    st = None

PRACTICE_STATE_QUERY_KEY = "practice_state"
WEAK_STATE_QUERY_KEY = "weak_state"
STATE_VERSION = 1
MAX_AGE_SECONDS = 8 * 60 * 60


def _query_param_value(name: str) -> str:
    if st is None:
        return ""
    try:
        value = st.query_params.get(name, "")
        if isinstance(value, list):
            return value[0] if value else ""
        return value or ""
    except Exception:
        return ""


def _set_query_param(name: str, value: str) -> None:
    if st is None:
        return
    try:
        current = _query_param_value(name)
        if current != value:
            st.query_params[name] = value
    except Exception:
        pass


def _clear_query_param(name: str) -> None:
    if st is None:
        return
    try:
        if name in st.query_params:
            del st.query_params[name]
    except Exception:
        pass


def _encode_state(state: Dict[str, Any]) -> str:
    payload = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")


def _decode_state(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        padding = "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode((raw + padding).encode("utf-8")).decode("utf-8")
        state = json.loads(decoded)
        if not isinstance(state, dict) or state.get("v") != STATE_VERSION:
            return None
        return state
    except Exception:
        return None


def decode_pending_category_practice_state() -> Optional[Dict[str, Any]]:
    state = _decode_state(_query_param_value(PRACTICE_STATE_QUERY_KEY))
    if not state or state.get("kind") != "category":
        return None
    return state


def decode_pending_weak_practice_state() -> Optional[Dict[str, Any]]:
    state = _decode_state(_query_param_value(WEAK_STATE_QUERY_KEY))
    if not state or state.get("kind") != "weak":
        return None
    return state


def clear_category_practice_state() -> None:
    _clear_query_param(PRACTICE_STATE_QUERY_KEY)


def clear_weak_practice_state() -> None:
    _clear_query_param(WEAK_STATE_QUERY_KEY)


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def validate_practice_state(state: Optional[Dict[str, Any]], *, user_email: str, kind: str) -> bool:
    if not state or state.get("kind") != kind:
        return False
    if _normalize_email(state.get("user_email")) != _normalize_email(user_email):
        return False
    if bool(state.get("submitted")):
        return False
    if bool(state.get("saved")):
        return False
    updated_at = state.get("updated_at")
    try:
        if updated_at is None or (time.time() - float(updated_at)) > MAX_AGE_SECONDS:
            return False
    except Exception:
        return False
    question_ids = state.get("question_ids") or []
    if not question_ids:
        return False
    return True


def capture_option_orders(questions: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    return {
        str(index): [str(opt.get("id")) for opt in (question.get("options") or [])]
        for index, question in enumerate(questions or [])
    }


def restore_questions_from_state(
    question_bank: List[Dict[str, Any]],
    question_ids: List[Any],
    option_orders: Dict[str, List[str]],
) -> Optional[List[Dict[str, Any]]]:
    bank_by_id = {str(question.get("id")): question for question in question_bank}
    restored: List[Dict[str, Any]] = []
    for index, raw_id in enumerate(question_ids):
        source = bank_by_id.get(str(raw_id))
        if source is None:
            return None
        question = copy.deepcopy(source)
        order = option_orders.get(str(index)) or [str(opt.get("id")) for opt in question.get("options", [])]
        options_by_id = {str(opt.get("id")): opt for opt in question.get("options", [])}
        question["options"] = [options_by_id[str(option_id)] for option_id in order if str(option_id) in options_by_id]
        if len(question["options"]) < 2:
            return None
        restored.append(question)
    return restored


def _restore_answers(raw_answers: Dict[str, Any]) -> Dict[int, List[str]]:
    restored: Dict[int, List[str]] = {}
    for raw_index, selected in (raw_answers or {}).items():
        try:
            index = int(raw_index)
        except Exception:
            continue
        values = [str(value) for value in (selected or [])]
        if values:
            restored[index] = values
    return restored


def _restore_time_spent(raw_times: Dict[str, Any]) -> Dict[int, float]:
    restored: Dict[int, float] = {}
    for raw_index, seconds in (raw_times or {}).items():
        try:
            restored[int(raw_index)] = float(seconds or 0)
        except Exception:
            continue
    return restored


def build_category_practice_state(session_state: Any, user_email: str) -> Optional[Dict[str, Any]]:
    questions = session_state.get("practice_questions") or []
    if not questions:
        return None
    if not session_state.get("practice_started"):
        return None
    if session_state.get("practice_submitted"):
        return None

    answers = {}
    for index, selected in (session_state.get("practice_answers") or {}).items():
        answers[str(index)] = [str(value) for value in (selected or [])]

    return {
        "v": STATE_VERSION,
        "kind": "category",
        "user_email": _normalize_email(user_email),
        "updated_at": time.time(),
        "started_at": float(session_state.get("practice_started_at") or time.time()),
        "submitted": False,
        "saved": bool(session_state.get("practice_saved")),
        "exam_name": session_state.get("practice_exam_name"),
        "language_code": session_state.get("practice_language_code"),
        "category": session_state.get("practice_category"),
        "mode_label": session_state.get("practice_mode_label"),
        "count": session_state.get("practice_count"),
        "question_ids": [str(question.get("id")) for question in questions if question.get("id") is not None],
        "option_orders": session_state.get("practice_option_orders") or capture_option_orders(questions),
        "current_index": int(session_state.get("practice_current_index") or 0),
        "answers": answers,
        "feedback_shown": bool(session_state.get("practice_feedback_shown")),
        "question_time_spent": {
            str(index): float(seconds)
            for index, seconds in (session_state.get("practice_question_time_spent") or {}).items()
        },
    }


def build_weak_practice_state(session_state: Any, user_email: str) -> Optional[Dict[str, Any]]:
    questions = session_state.get("weak_questions") or []
    if not questions:
        return None
    if not session_state.get("weak_started"):
        return None
    if session_state.get("weak_submitted"):
        return None

    answers = {}
    for index, selected in (session_state.get("weak_answers") or {}).items():
        answers[str(index)] = [str(value) for value in (selected or [])]

    return {
        "v": STATE_VERSION,
        "kind": "weak",
        "user_email": _normalize_email(user_email),
        "updated_at": time.time(),
        "started_at": float(session_state.get("weak_started_at") or time.time()),
        "submitted": False,
        "saved": bool(session_state.get("weak_saved")),
        "exam_name": session_state.get("weak_exam_name"),
        "language_code": session_state.get("weak_language_code"),
        "categories": list(session_state.get("weak_categories") or []),
        "question_ids": [str(question.get("id")) for question in questions if question.get("id") is not None],
        "option_orders": session_state.get("weak_option_orders") or capture_option_orders(questions),
        "current_index": int(session_state.get("weak_current_index") or 0),
        "answers": answers,
        "feedback_shown": bool(session_state.get("weak_feedback_shown")),
        "question_time_spent": {
            str(index): float(seconds)
            for index, seconds in (session_state.get("weak_question_time_spent") or {}).items()
        },
    }


def persist_category_practice_state(session_state: Any, user_email: str) -> None:
    state = build_category_practice_state(session_state, user_email)
    if not state:
        clear_category_practice_state()
        return
    _set_query_param(PRACTICE_STATE_QUERY_KEY, _encode_state(state))


def persist_weak_practice_state(session_state: Any, user_email: str) -> None:
    state = build_weak_practice_state(session_state, user_email)
    if not state:
        clear_weak_practice_state()
        return
    _set_query_param(WEAK_STATE_QUERY_KEY, _encode_state(state))


def restore_category_practice_session(
    state: Dict[str, Any],
    question_bank: List[Dict[str, Any]],
    user_email: str,
    session_state: Any,
) -> bool:
    if not validate_practice_state(state, user_email=user_email, kind="category"):
        return False

    restored_questions = restore_questions_from_state(
        question_bank,
        state.get("question_ids") or [],
        state.get("option_orders") or {},
    )
    if not restored_questions:
        return False

    current_index = max(0, min(int(state.get("current_index") or 0), len(restored_questions) - 1))
    session_state["practice_questions"] = restored_questions
    session_state["practice_option_orders"] = state.get("option_orders") or capture_option_orders(restored_questions)
    session_state["practice_exam_name"] = state.get("exam_name")
    session_state["practice_language_code"] = state.get("language_code")
    session_state["practice_category"] = state.get("category")
    session_state["practice_mode_label"] = state.get("mode_label")
    session_state["practice_count"] = state.get("count")
    session_state["practice_started"] = True
    session_state["practice_submitted"] = False
    session_state["practice_saved"] = False
    session_state["practice_current_index"] = current_index
    session_state["practice_answers"] = _restore_answers(state.get("answers") or {})
    current_question = restored_questions[current_index]
    current_answer = session_state["practice_answers"].get(current_index, [])
    from utils.question_answer_key import effective_explanation_feedback_shown  # noqa: PLC0415

    session_state["practice_feedback_shown"] = effective_explanation_feedback_shown(
        bool(state.get("feedback_shown")),
        current_answer,
        current_question,
    )
    session_state["practice_question_time_spent"] = _restore_time_spent(state.get("question_time_spent") or {})
    session_state["practice_question_entered_at"] = time.time()
    session_state["practice_timing_index"] = current_index
    session_state["practice_started_at"] = float(state.get("started_at") or time.time())
    session_state["_category_practice_restored_once"] = True
    return True


def restore_weak_practice_session(
    state: Dict[str, Any],
    question_bank: List[Dict[str, Any]],
    user_email: str,
    session_state: Any,
) -> bool:
    if not validate_practice_state(state, user_email=user_email, kind="weak"):
        return False

    restored_questions = restore_questions_from_state(
        question_bank,
        state.get("question_ids") or [],
        state.get("option_orders") or {},
    )
    if not restored_questions:
        return False

    current_index = max(0, min(int(state.get("current_index") or 0), len(restored_questions) - 1))
    session_state["weak_questions"] = restored_questions
    session_state["weak_option_orders"] = state.get("option_orders") or capture_option_orders(restored_questions)
    session_state["weak_exam_name"] = state.get("exam_name")
    session_state["weak_language_code"] = state.get("language_code")
    session_state["weak_categories"] = list(state.get("categories") or [])
    session_state["weak_started"] = True
    session_state["weak_submitted"] = False
    session_state["weak_saved"] = False
    session_state["weak_current_index"] = current_index
    session_state["weak_answers"] = _restore_answers(state.get("answers") or {})
    current_question = restored_questions[current_index]
    current_answer = session_state["weak_answers"].get(current_index, [])
    from utils.question_answer_key import effective_explanation_feedback_shown  # noqa: PLC0415

    session_state["weak_feedback_shown"] = effective_explanation_feedback_shown(
        bool(state.get("feedback_shown")),
        current_answer,
        current_question,
    )
    session_state["weak_question_time_spent"] = _restore_time_spent(state.get("question_time_spent") or {})
    session_state["weak_question_entered_at"] = time.time()
    session_state["weak_timing_index"] = current_index
    session_state["weak_started_at"] = float(state.get("started_at") or time.time())
    session_state["_weak_practice_restored_once"] = True
    return True
