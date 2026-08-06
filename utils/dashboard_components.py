"""Minimum presentation helpers required by Scenario_Simulator_V2.

This is intentionally NOT the full Dashboard component library. It only
exposes the two symbols imported by ``pages/Scenario_Simulator_V2.py`` so the
Simulator can run on origin/main without importing charts, analytics, or
Dashboard redesign modules.
"""

from __future__ import annotations

import html
from typing import Any, Optional

import streamlit as st

from utils.ui_theme import theme_css


def inject_certbound_theme() -> None:
    """Inject the minimum shared CSS used by Simulator empty states."""
    st.markdown(theme_css(), unsafe_allow_html=True)


def render_empty_state(
    title: str,
    body: str,
    *,
    action_label: Optional[str] = None,
    action_href: Optional[str] = None,
) -> None:
    """Render a simple empty/unavailable state card."""

    def _esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    action_html = ""
    if action_label and action_href:
        action_html = (
            f'<a class="cb-action-link" href="{_esc(action_href)}" target="_self">'
            f"{_esc(action_label)}</a>"
        )
    st.markdown(
        f"""
        <div class="cb-empty-state">
            <div class="cb-card-heading" style="font-size:1.05rem;">{_esc(title)}</div>
            <p class="cb-body">{_esc(body)}</p>
            {action_html}
        </div>
        """,
        unsafe_allow_html=True,
    )
