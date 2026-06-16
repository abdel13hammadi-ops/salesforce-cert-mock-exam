from __future__ import annotations

import streamlit as st

from utils.access_control import render_app_chrome, require_admin

APP_VERSION = "ADMIN_LANDING_V1"

st.set_page_config(page_title="Admin", page_icon="🔐", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()
require_admin()

st.title("🔐 Admin")
st.caption(f"App version: {APP_VERSION}")
st.success("Admin access is unlocked for this session.")

st.subheader("Admin tools")
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.page_link("pages/Admin_Users.py", label="Admin Users", icon="👥")
with col2:
    st.page_link("pages/Admin_Import.py", label="Admin Import", icon="⬆️")
with col3:
    st.page_link("pages/Admin_Question_Review.py", label="Question Review", icon="✅")
with col4:
    st.page_link("pages/Admin_Support_Tickets.py", label="Support Tickets", icon="🎫")
