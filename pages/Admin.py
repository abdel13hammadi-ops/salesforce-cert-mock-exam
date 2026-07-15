from __future__ import annotations

import streamlit as st

from utils.access_control import require_admin, render_app_chrome, render_session_page_link
from utils.dashboard_components import inject_certbound_theme, render_page_header
from utils.navigation import admin_routes
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.version import APP_VERSION

st.set_page_config(page_title="Admin Hub", page_icon="🔐", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()
require_admin()

enforce_session_timeout()
show_session_expired_notice()

inject_certbound_theme()
render_page_header(
    "Admin Hub",
    description="Internal CertBound administration tools. Page-level authorization remains required on every admin route.",
    badge="Admin unlocked",
)
st.caption(f"App Version: {APP_VERSION}")
st.success("Admin access is unlocked for this session.")

st.subheader("Admin tools")
cols = st.columns(3)
admin_links = [route for route in admin_routes() if route.key != "admin_hub"]
for idx, route in enumerate(admin_links):
    with cols[idx % 3]:
        render_session_page_link(route.page_path, label=route.label, icon=route.icon)

st.caption("Authorization is enforced on each admin page with require_admin().")
