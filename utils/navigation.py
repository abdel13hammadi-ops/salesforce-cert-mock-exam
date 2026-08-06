"""Minimum navigation helpers for BA Scenario Simulator V2 integration.

This is NOT the full learner navigation shell. It only provides:
- the Scenario Simulator feature-flag constant/helper
- route lookup used by Return-to-Practice in scenario_streamlit_v2
- a minimal route registry entry for Scenario_Simulator_V2

Production discoverability is wired as a single feature-flagged Premium
sidebar link inside the existing origin/main ``render_sidebar_navigation``
(access_control.py). This module does not replace the application chrome.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple


SESSION_QUERY_PARAM = "fr_session"
CERTBOUND_ENABLE_SCENARIO_SIMULATOR = "CERTBOUND_ENABLE_SCENARIO_SIMULATOR"


@dataclass(frozen=True)
class NavRoute:
    key: str
    label: str
    page_path: str
    icon: str = ""
    group: str = "hidden"
    requires_auth: bool = False
    requires_premium: bool = False
    requires_admin_email: bool = False
    requires_admin_unlock: bool = False
    preserve_session: bool = True
    feature_flag_env: Optional[str] = None
    parent_key: Optional[str] = None


NAV_ROUTES: Tuple[NavRoute, ...] = (
    NavRoute(
        "scenario_simulator_v2",
        "Scenario Simulator V2",
        "pages/Scenario_Simulator_V2.py",
        "🧭",
        "hidden",
        requires_premium=True,
        feature_flag_env=CERTBOUND_ENABLE_SCENARIO_SIMULATOR,
        parent_key="practice",
    ),
    # Used by prepare_return_to_practice_navigation() in scenario_streamlit_v2.
    NavRoute(
        "practice",
        "Practice",
        "pages/Practice.py",
        "📚",
        "primary",
    ),
)


def is_feature_flag_enabled(env_name: Optional[str]) -> bool:
    if not env_name:
        return True
    return str(os.environ.get(env_name, "")).strip().lower() in {"1", "true", "yes", "on"}


def route_for_page_path(page_path: str) -> Optional[NavRoute]:
    normalized = str(page_path or "").strip().replace("\\", "/")
    for route in NAV_ROUTES:
        if route.page_path.replace("\\", "/") == normalized:
            return route
    return None


def route_for_key(key: str) -> Optional[NavRoute]:
    for route in NAV_ROUTES:
        if route.key == key:
            return route
    return None
