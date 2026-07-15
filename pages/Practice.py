from __future__ import annotations

import streamlit as st

from utils.access_control import (
    get_current_user_email,
    get_user_access_level,
    has_premium_access,
    render_app_chrome,
    render_session_page_link,
)
from utils.certification_context import resolve_learner_exam_context, supported_exam_names
from utils.dashboard_components import inject_certbound_theme, render_page_header
from utils.certification_context import EXAM_NAME_QUERY_PARAM
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.version import APP_VERSION

st.set_page_config(page_title="Practice", page_icon="📚", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()
enforce_session_timeout()
show_session_expired_notice()

inject_certbound_theme()
supported = supported_exam_names([])
selected_exam = resolve_learner_exam_context(supported)
token = str(st.session_state.get("signed_session_token") or "").strip()

render_page_header(
    "Practice",
    description="Choose a targeted practice workflow for your current certification focus.",
    certification_name=selected_exam,
    badge=f"Access: {get_user_access_level(get_current_user_email() or '')}",
)
st.caption(f"App Version: {APP_VERSION}")

st.markdown("### Practice destinations")
col1, col2 = st.columns(2)
with col1:
    st.markdown("**Practice By Category**")
    st.write("Drill one certification domain at a time.")
    render_session_page_link(
        "pages/Practice_By_Category.py",
        label="Open Practice By Category",
        icon="📚",
        extra_params={EXAM_NAME_QUERY_PARAM: selected_exam},
    )
with col2:
    st.markdown("**Weak Areas Practice**")
    st.write("Focus on verified weak domains from your attempt history.")
    render_session_page_link(
        "pages/Weak_Areas_Practice.py",
        label="Open Weak Areas Practice",
        icon="🎯",
        extra_params={EXAM_NAME_QUERY_PARAM: selected_exam},
    )

st.markdown("**Daily Sprint**")
st.write(
    "Start a 10-question sprint from Home when Daily Sprint is available for your certification. "
    "Deep links preserve sprint parameters and your signed session."
)
render_session_page_link("pages/Dashboard.py", label="Return to Home", icon="🏠")

if not has_premium_access():
    st.info("Premium access is required to run full practice sessions. Free users can still preview locked workflows.")

st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
