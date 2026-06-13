import sys
from pathlib import Path

import streamlit as st

_file = Path(__file__).resolve()
_root = _file.parent.parent if _file.parent.name == "pages" else _file.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import path_setup

path_setup.ensure_project_root(__file__)

from utils.access_control import render_admin_login_page

st.set_page_config(page_title="Admin", layout="wide", initial_sidebar_state="expanded")
render_admin_login_page()
