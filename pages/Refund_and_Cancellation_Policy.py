from __future__ import annotations

import streamlit as st

from utils.access_control import render_public_chrome
from utils.legal_policy_pages import render_public_policy_page_header, render_refund_content
from utils.secondary_components import inject_secondary_theme, render_legal_document_end, render_legal_document_start

st.set_page_config(page_title="Refund and Cancellation Policy", page_icon="💳", layout="wide")
render_public_chrome()
inject_secondary_theme()

render_public_policy_page_header("Refund and Cancellation Policy", "💳")
render_legal_document_start()
render_refund_content()
render_legal_document_end()

st.divider()
st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
