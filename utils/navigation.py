"""Centralized CertBound navigation registry and session-preserving link helpers."""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode

from utils.legal_policy_pages import PRIVACY_PAGE, REFUND_PAGE, TERMS_PAGE

SESSION_QUERY_PARAM = "fr_session"

CERTBOUND_ENABLE_SCENARIO_SIMULATOR = "CERTBOUND_ENABLE_SCENARIO_SIMULATOR"

NAV_GROUP_PRIMARY = "primary"
NAV_GROUP_PRACTICE = "practice"
NAV_GROUP_ACCOUNT = "account"
NAV_GROUP_LEGAL = "legal"
NAV_GROUP_ADMIN = "admin"
NAV_GROUP_HIDDEN = "hidden"

PRIMARY_LEARNER_LABELS: Tuple[str, ...] = (
    "Home",
    "Certifications",
    "Practice",
    "Mock Exams",
    "Progress",
    "Account",
    "Support",
)


@dataclass(frozen=True)
class NavRoute:
    key: str
    label: str
    page_path: str
    icon: str = ""
    group: str = NAV_GROUP_PRIMARY
    requires_auth: bool = False
    requires_premium: bool = False
    requires_admin_email: bool = False
    requires_admin_unlock: bool = False
    preserve_session: bool = True
    feature_flag_env: Optional[str] = None
    parent_key: Optional[str] = None


NAV_ROUTES: Tuple[NavRoute, ...] = (
    NavRoute("home", "Home", "pages/Dashboard.py", "🏠", NAV_GROUP_PRIMARY),
    NavRoute("certifications", "Certifications", "pages/Certifications.py", "📘", NAV_GROUP_PRIMARY),
    NavRoute("practice", "Practice", "pages/Practice.py", "📚", NAV_GROUP_PRIMARY),
    NavRoute("mock_exams", "Mock Exams", "app.py", "📝", NAV_GROUP_PRIMARY),
    NavRoute(
        "progress",
        "Progress",
        "pages/My_Progress.py",
        "📈",
        NAV_GROUP_PRIMARY,
        requires_premium=True,
    ),
    NavRoute("account", "Account", "pages/Account.py", "👤", NAV_GROUP_PRIMARY),
    NavRoute("support", "Support", "pages/Support.py", "🛟", NAV_GROUP_PRIMARY),
    NavRoute(
        "practice_by_category",
        "Practice By Category",
        "pages/Practice_By_Category.py",
        "📚",
        NAV_GROUP_PRACTICE,
        requires_premium=True,
        parent_key="practice",
    ),
    NavRoute(
        "weak_areas_practice",
        "Weak Areas Practice",
        "pages/Weak_Areas_Practice.py",
        "🎯",
        NAV_GROUP_PRACTICE,
        requires_premium=True,
        parent_key="practice",
    ),
    NavRoute("terms", "Terms of Service", TERMS_PAGE, "", NAV_GROUP_LEGAL),
    NavRoute("privacy", "Privacy Policy", PRIVACY_PAGE, "", NAV_GROUP_LEGAL),
    NavRoute("refund", "Refund and Cancellation Policy", REFUND_PAGE, "", NAV_GROUP_LEGAL),
    NavRoute(
        "admin_hub",
        "Admin Hub",
        "pages/Admin.py",
        "🔐",
        NAV_GROUP_ADMIN,
        requires_admin_email=True,
        requires_admin_unlock=True,
    ),
    NavRoute(
        "admin_users",
        "Users",
        "pages/Admin_Users.py",
        "👥",
        NAV_GROUP_ADMIN,
        requires_admin_email=True,
        requires_admin_unlock=True,
    ),
    NavRoute(
        "admin_import",
        "Import",
        "pages/Admin_Import.py",
        "⬆️",
        NAV_GROUP_ADMIN,
        requires_admin_email=True,
        requires_admin_unlock=True,
    ),
    NavRoute(
        "admin_question_review",
        "Question Review",
        "pages/Admin_Question_Review.py",
        "✅",
        NAV_GROUP_ADMIN,
        requires_admin_email=True,
        requires_admin_unlock=True,
    ),
    NavRoute(
        "admin_free_mock_curation",
        "Free Mock Curation",
        "pages/Admin_Free_Mock_Curation.py",
        "📋",
        NAV_GROUP_ADMIN,
        requires_admin_email=True,
        requires_admin_unlock=True,
    ),
    NavRoute(
        "admin_audit_review",
        "Audit Review",
        "pages/Admin_Audit_Review.py",
        "🔍",
        NAV_GROUP_ADMIN,
        requires_admin_email=True,
        requires_admin_unlock=True,
    ),
    NavRoute(
        "admin_support_tickets",
        "Support Tickets",
        "pages/Admin_Support_Tickets.py",
        "🎫",
        NAV_GROUP_ADMIN,
        requires_admin_email=True,
        requires_admin_unlock=True,
    ),
    NavRoute(
        "scenario_simulator",
        "Scenario Simulator",
        "pages/Scenario_Simulator.py",
        "🧪",
        NAV_GROUP_HIDDEN,
        requires_premium=True,
        feature_flag_env=CERTBOUND_ENABLE_SCENARIO_SIMULATOR,
        parent_key="practice",
    ),
    NavRoute(
        "scenario_simulator_v2",
        "Scenario Simulator V2",
        "pages/Scenario_Simulator_V2.py",
        "🧭",
        NAV_GROUP_HIDDEN,
        requires_premium=True,
        feature_flag_env=CERTBOUND_ENABLE_SCENARIO_SIMULATOR,
        parent_key="practice",
    ),
)


def is_feature_flag_enabled(env_name: Optional[str]) -> bool:
    if not env_name:
        return True
    return str(os.environ.get(env_name, "")).strip().lower() in {"1", "true", "yes", "on"}


def streamlit_route_for_page(page_path: str) -> str:
    page_path = str(page_path or "").strip()
    if page_path == "app.py":
        return "/"
    if page_path.startswith("pages/") and page_path.endswith(".py"):
        return "/" + page_path.rsplit("/", 1)[-1][:-3]
    return "/"


def detect_current_page_path() -> str:
    """Best-effort detection of the active Streamlit script path."""
    try:
        for frame_info in inspect.stack():
            filename = str(frame_info.filename or "").replace("\\", "/")
            if "/pages/" in filename and filename.endswith(".py"):
                idx = filename.rfind("pages/")
                return filename[idx:]
            if filename.endswith("/app.py") or filename.endswith("\\app.py"):
                return "app.py"
    except Exception:
        pass
    return ""


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


def validate_unique_route_paths() -> None:
    seen: Dict[str, str] = {}
    for route in NAV_ROUTES:
        path = route.page_path
        if path in seen and seen[path] != route.key:
            raise ValueError(f"Duplicate navigation path {path!r} for keys {seen[path]!r} and {route.key!r}")
        seen[path] = route.key


def primary_learner_routes() -> Tuple[NavRoute, ...]:
    return tuple(route for route in NAV_ROUTES if route.group == NAV_GROUP_PRIMARY)


def practice_routes() -> Tuple[NavRoute, ...]:
    return tuple(route for route in NAV_ROUTES if route.group == NAV_GROUP_PRACTICE)


def admin_routes() -> Tuple[NavRoute, ...]:
    return tuple(route for route in NAV_ROUTES if route.group == NAV_GROUP_ADMIN)


def legal_routes() -> Tuple[NavRoute, ...]:
    return tuple(route for route in NAV_ROUTES if route.group == NAV_GROUP_LEGAL)


def hidden_routes(*, include_disabled: bool = False) -> Tuple[NavRoute, ...]:
    routes = [route for route in NAV_ROUTES if route.group == NAV_GROUP_HIDDEN]
    if include_disabled:
        return tuple(routes)
    return tuple(route for route in routes if is_feature_flag_enabled(route.feature_flag_env))


def is_route_visible(
    route: NavRoute,
    *,
    access_level: str,
    is_admin_email: bool,
    admin_unlocked: bool,
) -> bool:
    if route.group == NAV_GROUP_HIDDEN:
        return False
    if route.feature_flag_env and not is_feature_flag_enabled(route.feature_flag_env):
        return False
    if route.requires_admin_email and not is_admin_email:
        return False
    if route.requires_admin_unlock and not admin_unlocked:
        return False
    if route.requires_auth and access_level == "logged_out":
        return False
    return True


def navigation_definitions_for_user(
    *,
    access_level: str,
    is_admin_email: bool,
    admin_unlocked: bool,
) -> List[NavRoute]:
    validate_unique_route_paths()
    return [
        route
        for route in NAV_ROUTES
        if is_route_visible(
            route,
            access_level=access_level,
            is_admin_email=is_admin_email,
            admin_unlocked=admin_unlocked,
        )
    ]


def is_route_active(route: NavRoute, current_page_path: str) -> bool:
    current = str(current_page_path or "").strip().replace("\\", "/")
    target = route.page_path.replace("\\", "/")
    if current == target:
        return True
    if route.key == "home" and current in {"", "pages/Dashboard.py"}:
        return current == "pages/Dashboard.py"
    return False


def build_nav_href(
    page_path: str,
    *,
    session_token: str = "",
    extra_params: Optional[Mapping[str, str]] = None,
    preserve_session: bool = True,
) -> str:
    route = streamlit_route_for_page(page_path)
    params: Dict[str, str] = {}
    if extra_params:
        for key, value in extra_params.items():
            if value is not None and str(value) != "":
                params[str(key)] = str(value)
    if preserve_session and session_token:
        params[SESSION_QUERY_PARAM] = session_token
    if not params:
        return route
    return f"{route}?{urlencode(params, quote_via=quote)}"


def merge_query_params(existing_href: str, extra_params: Mapping[str, str]) -> str:
    if "?" in existing_href:
        base, query = existing_href.split("?", 1)
        current = dict(parse_qsl_safe(query))
        current.update({k: str(v) for k, v in extra_params.items() if v is not None and str(v) != ""})
        return f"{base}?{urlencode(current, quote_via=quote)}"
    return build_nav_href(existing_href.lstrip("/") or "app.py", extra_params=extra_params, preserve_session=False)


def parse_qsl_safe(query: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for part in str(query or "").split("&"):
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            pairs.append((key, value))
        else:
            pairs.append((part, ""))
    return pairs


def build_practice_domain_href(
    exam_name: str,
    category: str,
    *,
    session_token: str = "",
    page_path: str = "pages/Practice_By_Category.py",
) -> str:
    return build_nav_href(
        page_path,
        session_token=session_token,
        extra_params={"exam_name": exam_name, "category": category},
    )


def build_daily_sprint_href(
    exam_name: str,
    category: str,
    *,
    count: int = 10,
    session_token: str = "",
    page_path: str = "pages/Practice_By_Category.py",
) -> str:
    return build_nav_href(
        page_path,
        session_token=session_token,
        extra_params={
            "daily_sprint": "1",
            "exam_name": exam_name,
            "category": category,
            "count": str(count),
        },
    )


def scenario_simulator_route_when_enabled() -> Optional[NavRoute]:
    route = route_for_key("scenario_simulator")
    if route and is_feature_flag_enabled(route.feature_flag_env):
        return route
    return None
