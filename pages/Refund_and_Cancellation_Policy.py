from __future__ import annotations

import streamlit as st

from utils.access_control import render_sidebar_navigation
from utils.legal_policy_pages import render_public_policy_page_header, render_refund_content

st.set_page_config(page_title="Refund and Cancellation Policy", page_icon="💳", layout="wide")
render_sidebar_navigation()

render_public_policy_page_header("Refund and Cancellation Policy", "💳")
render_refund_content()

st.divider()
st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
