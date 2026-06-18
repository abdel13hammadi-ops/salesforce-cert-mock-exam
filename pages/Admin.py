from __future__ import annotations

import streamlit as st

from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.access_control import render_app_chrome, require_admin, render_session_page_link

APP_VERSION = "ADMIN_LANDING_V1"

st.set_page_config(page_title="Admin", page_icon="🔐", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()
require_admin()


# SESSION_TIMEOUT_APPLIED
enforce_session_timeout()
show_session_expired_notice()

st.title("🔐 Admin")
st.caption(f"App version: {APP_VERSION}")
st.success("Admin access is unlocked for this session.")

st.subheader("Admin tools")
col1, col2, col3, col4 = st.columns(4)
with col1:
    render_session_page_link("pages/Admin_Users.py", label="Admin Users", icon="👥")
with col2:
    render_session_page_link("pages/Admin_Import.py", label="Admin Import", icon="⬆️")
with col3:
    render_session_page_link("pages/Admin_Question_Review.py", label="Question Review", icon="✅")
with col4:
    render_session_page_link("pages/Admin_Support_Tickets.py", label="Support Tickets", icon="🎫")
