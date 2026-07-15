from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from utils.access_control import (
    get_current_user_email,
    get_supabase_client,
    get_user_access_level,
    has_premium_access,
    render_app_chrome,
    render_session_page_link,
)
from utils.certification_context import (
    DEFAULT_ADMIN_EXAM,
    resolve_learner_exam_context,
    set_learner_exam_context,
    supported_exam_names,
)
from utils.dashboard_components import inject_certbound_theme, render_page_header
from utils.certification_context import EXAM_NAME_QUERY_PARAM
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.version import APP_VERSION

st.set_page_config(page_title="Certifications", page_icon="📘", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()
enforce_session_timeout()
show_session_expired_notice()


@st.cache_data(ttl=60, show_spinner=False)
def fetch_active_certifications() -> List[Dict[str, Any]]:
    try:
        result = (
            get_supabase_client()
            .table("certifications")
            .select(
                "exam_name,display_name,certification_code,passing_score,time_limit_minutes,question_count,is_active"
            )
            .eq("is_active", True)
            .order("display_name")
            .execute()
        )
        rows = result.data or []
        if rows:
            return rows
    except Exception:
        pass
    return [
        {
            "exam_name": DEFAULT_ADMIN_EXAM,
            "display_name": DEFAULT_ADMIN_EXAM,
            "passing_score": 68,
            "time_limit_minutes": 105,
            "question_count": 60,
        }
    ]


inject_certbound_theme()
cert_rows = fetch_active_certifications()
supported = supported_exam_names(cert_rows)
selected_exam = resolve_learner_exam_context(supported)
token = str(st.session_state.get("signed_session_token") or "").strip()

render_page_header(
    "Certifications",
    description="Choose your certification focus and jump into practice, mock exams, or progress tracking.",
    certification_name=selected_exam,
    badge=f"Access: {get_user_access_level(get_current_user_email() or '')}",
)
st.caption(f"App Version: {APP_VERSION}")

labels = [str(row.get("display_name") or row.get("exam_name") or "") for row in cert_rows]
if labels:
    current_index = next(
        (idx for idx, row in enumerate(cert_rows) if row.get("exam_name") == selected_exam),
        0,
    )
    picked = st.selectbox(
        "Certification",
        labels,
        index=current_index,
        key="certifications_selected_exam",
    )
    picked_row = cert_rows[labels.index(picked)]
    if set_learner_exam_context(str(picked_row.get("exam_name") or ""), supported):
        selected_exam = str(picked_row.get("exam_name") or selected_exam)

st.markdown("### Supported activities")
col1, col2, col3 = st.columns(3)
with col1:
    st.markdown("**Practice**")
    st.write("Target one domain or your verified weak areas.")
    render_session_page_link(
        "pages/Practice.py",
        label="Open Practice",
        icon="📚",
        extra_params={EXAM_NAME_QUERY_PARAM: selected_exam},
    )
with col2:
    st.markdown("**Mock Exams**")
    st.write("Run the free preview or a full verified mock exam.")
    render_session_page_link("app.py", label="Open Mock Exams", icon="📝")
with col3:
    st.markdown("**Progress**")
    st.write("Review readiness, score trends, and domain mastery.")
    render_session_page_link("pages/My_Progress.py", label="Open Progress", icon="📈")

if not has_premium_access():
    st.caption("Premium access is required for full practice and progress analytics.")

st.divider()
st.caption("Page-level certification selectors remain available as fallback on practice and progress pages.")
