import sys
from pathlib import Path

import streamlit as st

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.access_control import render_admin_login_page

st.set_page_config(page_title="Admin", layout="wide", initial_sidebar_state="expanded")
render_admin_login_page()
