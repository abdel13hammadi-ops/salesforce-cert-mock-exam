"""Shared learner certification context helpers with catalog validation."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import streamlit as st

from utils.navigation import SESSION_QUERY_PARAM, build_nav_href

SELECTED_EXAM_SESSION_KEY = "selected_exam_name"
EXAM_NAME_QUERY_PARAM = "exam_name"

DEFAULT_ADMIN_EXAM = "Salesforce Certified Platform Administrator"
DEFAULT_BA_EXAM = "Salesforce Certified Business Analyst"

FALLBACK_EXAM_NAMES: Tuple[str, ...] = (DEFAULT_ADMIN_EXAM, DEFAULT_BA_EXAM)


def _get_query_param(name: str) -> str:
    try:
        value = st.query_params.get(name, "")
        if isinstance(value, list):
            return str(value[0] if value else "")
        return str(value or "")
    except Exception:
        return ""


def exam_names_from_catalog_rows(rows: Sequence[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for row in rows or []:
        name = str(row.get("exam_name") or row.get("certification") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def supported_exam_names(catalog_rows: Sequence[Dict[str, Any]]) -> List[str]:
    names = exam_names_from_catalog_rows(catalog_rows)
    if names:
        return names
    return list(FALLBACK_EXAM_NAMES)


def validate_exam_name(exam_name: str, supported_names: Sequence[str]) -> Optional[str]:
    candidate = str(exam_name or "").strip()
    if not candidate:
        return None
    for supported in supported_names:
        if candidate == supported:
            return supported
    return None


def get_validated_exam_from_query(supported_names: Sequence[str]) -> Optional[str]:
    return validate_exam_name(_get_query_param(EXAM_NAME_QUERY_PARAM), supported_names)


def get_validated_exam_from_session(supported_names: Sequence[str]) -> Optional[str]:
    stored = st.session_state.get(SELECTED_EXAM_SESSION_KEY)
    return validate_exam_name(str(stored or ""), supported_names)


def resolve_learner_exam_context(
    supported_names: Sequence[str],
    *,
    default_exam: str = DEFAULT_ADMIN_EXAM,
) -> str:
    """Resolve certification context from query param, session, or safe default."""
    supported = list(supported_names) or list(FALLBACK_EXAM_NAMES)
    from_query = get_validated_exam_from_query(supported)
    if from_query:
        st.session_state[SELECTED_EXAM_SESSION_KEY] = from_query
        return from_query
    from_session = get_validated_exam_from_session(supported)
    if from_session:
        return from_session
    fallback = validate_exam_name(default_exam, supported) or supported[0]
    st.session_state[SELECTED_EXAM_SESSION_KEY] = fallback
    return fallback


def set_learner_exam_context(exam_name: str, supported_names: Sequence[str]) -> bool:
    validated = validate_exam_name(exam_name, supported_names)
    if not validated:
        return False
    st.session_state[SELECTED_EXAM_SESSION_KEY] = validated
    return True


def build_exam_context_href(
    page_path: str,
    exam_name: str,
    *,
    session_token: str = "",
    extra_params: Optional[Dict[str, str]] = None,
) -> str:
    params = dict(extra_params or {})
    params[EXAM_NAME_QUERY_PARAM] = exam_name
    return build_nav_href(page_path, session_token=session_token, extra_params=params)


def certification_context_label(exam_name: str, *, passing_score: Optional[float] = None) -> str:
    if passing_score is None:
        return exam_name
    return f"{exam_name} · Passing score {passing_score:.0f}%"
