"""Centralized auth, access control, and navigation helpers for the Streamlit app.

Streamlit wipes `st.session_state` on a hard browser refresh, so login cannot live
only in session_state. This module persists a short HMAC-signed session token in
both:

1. the URL query parameter `fr_session`, so Python can read it; and
2. browser localStorage, so refresh/navigation can restore the query parameter.

The token contains no password and no Supabase service key. It is still a bearer
session token, so COOKIE_PASSWORD must be strong in production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client

PAID_STATUS_VALUES = {"active", "paid", "premium", "subscribed"}
EXPIRED_STATUS_VALUES = {"expired", "cancelled", "canceled", "past_due", "unpaid"}
FREE_STATUS = "free"
SESSION_PARAM = "fr_session"
BROWSER_SESSION_STORAGE_KEY = "salesforce_cert_mock_fr_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
SESSION_REFRESH_WINDOW_SECONDS = 60 * 60 * 24 * 7


def _secret(name: str, default: str = "") -> str:
    """Read config from Render environment variables first, then Streamlit secrets."""
    env_value = str(os.environ.get(name, "") or "").strip()
    if env_value:
        return env_value
    try:
        return str(st.secrets.get(name, default) or "").strip()
    except Exception:
        return default


def _signing_secret() -> str:
    # COOKIE_PASSWORD is the intended signing secret. SUPABASE_SERVICE_ROLE_KEY is a fallback
    # so existing Render deploys keep working, but set COOKIE_PASSWORD explicitly before launch.
    return _secret("COOKIE_PASSWORD") or _secret("SUPABASE_SERVICE_ROLE_KEY") or "dev-only-unsafe-secret"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + pad).encode("utf-8"))


def make_signed_session(payload: Dict[str, Any]) -> str:
    safe_payload = dict(payload or {})
    safe_payload["exp"] = int(time.time()) + SESSION_TTL_SECONDS
    raw = json.dumps(safe_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64url_encode(raw)
    sig = hmac.new(_signing_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def verify_signed_session(token: str) -> Optional[Dict[str, Any]]:
    token = str(token or "").strip()
    if "." not in token:
        return None
    body, sig = token.split(".", 1)
    expected = hmac.new(_signing_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    try:
        actual = _b64url_decode(sig)
    except Exception:
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    email = str(payload.get("user_email") or payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return None
    return payload


def _get_query_param(name: str) -> str:
    try:
        value = st.query_params.get(name, "")
        if isinstance(value, list):
            return str(value[0] if value else "")
        return str(value or "")
    except Exception:
        return ""


def _set_query_param(name: str, value: str) -> None:
    try:
        st.query_params[name] = value
    except Exception:
        pass


def _clear_query_param(name: str) -> None:
    try:
        if name in st.query_params:
            del st.query_params[name]
    except Exception:
        pass


def _email_is_configured_admin(email: Optional[str]) -> bool:
    email = str(email or "").strip().lower()
    admin_emails = [e.strip().lower() for e in _secret("ADMIN_EMAILS").split(",") if e.strip()]
    return bool(email and email in admin_emails)


def _session_payload_from_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    email = str(profile.get("email") or profile.get("user_email") or "").strip().lower()
    admin_unlocked = bool(st.session_state.get("admin_unlocked") and _email_is_configured_admin(email))
    return {
        "user_email": email,
        "auth_user_id": str(profile.get("auth_user_id") or ""),
        "full_name": str(profile.get("full_name") or ""),
        "preferred_language_code": str(profile.get("preferred_language_code") or "en").strip().lower() or "en",
        "subscription_status": str(profile.get("subscription_status") or "free").strip().lower(),
        "admin_unlocked": admin_unlocked,
    }


def _session_payloads_match(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    keys = ["user_email", "auth_user_id", "full_name", "preferred_language_code", "subscription_status", "admin_unlocked"]
    for key in keys:
        if str(left.get(key, "")).strip().lower() != str(right.get(key, "")).strip().lower():
            return False
    return True


def _current_signed_session_token() -> str:
    return str(st.session_state.get("signed_session_token") or _get_query_param(SESSION_PARAM) or "").strip()


def _persist_token_to_url(token: str) -> None:
    st.session_state["signed_session_token"] = token
    if _get_query_param(SESSION_PARAM) != token:
        _set_query_param(SESSION_PARAM, token)

def _sync_existing_session_token_to_url() -> None:
    """If Streamlit kept session_state during multipage navigation, force the URL
    to carry fr_session too so a hard browser refresh can restore login.
    """
    token = str(st.session_state.get("signed_session_token") or "").strip()
    if not token:
        return
    if verify_signed_session(token) and _get_query_param(SESSION_PARAM) != token:
        _set_query_param(SESSION_PARAM, token)


def _mark_browser_session_clear_needed() -> None:
    st.session_state["clear_browser_session_storage"] = True


def _render_browser_session_clearer() -> None:
    components.html(
        f"""
        <script>
        (function () {{
            const paramName = {json.dumps(SESSION_PARAM)};
            const storageKey = {json.dumps(BROWSER_SESSION_STORAGE_KEY)};
            try {{
                const storage = (window.parent && window.parent.localStorage) ? window.parent.localStorage : window.localStorage;
                storage.removeItem(storageKey);
            }} catch (e) {{}}

            try {{
                const loc = (window.parent && window.parent.location) ? window.parent.location : window.location;
                const url = new URL(loc.href);
                if (url.searchParams.has(paramName)) {{
                    url.searchParams.delete(paramName);
                    loc.replace(url.toString());
                }}
            }} catch (e) {{}}
        }})();
        </script>
        """,
        height=0,
    )


def _render_browser_session_bridge() -> None:
    """Bridge signed session token between URL query params and browser localStorage.

    Python can read query params but not localStorage. JavaScript can read localStorage
    and restore the query param after a browser refresh or Streamlit multipage navigation.
    """
    if st.session_state.pop("clear_browser_session_storage", False):
        _render_browser_session_clearer()
        return

    components.html(
        f"""
        <script>
        (function () {{
            const paramName = {json.dumps(SESSION_PARAM)};
            const storageKey = {json.dumps(BROWSER_SESSION_STORAGE_KEY)};

            function getLoc() {{
                try {{
                    if (window.parent && window.parent.location) return window.parent.location;
                }} catch (e) {{}}
                return window.location;
            }}

            function getStorage() {{
                try {{
                    if (window.parent && window.parent.localStorage) return window.parent.localStorage;
                }} catch (e) {{}}
                try {{ return window.localStorage; }} catch (e) {{ return null; }}
            }}

            const loc = getLoc();
            const storage = getStorage();
            if (!storage) return;

            const url = new URL(loc.href);
            const tokenFromUrl = url.searchParams.get(paramName);

            if (tokenFromUrl) {{
                storage.setItem(storageKey, tokenFromUrl);
                return;
            }}

            const tokenFromStorage = storage.getItem(storageKey);
            if (tokenFromStorage) {{
                url.searchParams.set(paramName, tokenFromStorage);
                loc.replace(url.toString());
            }}
        }})();
        </script>
        """,
        height=0,
    )


def restore_login_from_signed_url() -> bool:
    """Restore session_state from signed query token, if present and valid."""
    if st.session_state.get("user_email"):
        return True
    token = _get_query_param(SESSION_PARAM)
    if not token:
        return False
    payload = verify_signed_session(token)
    if not payload:
        _clear_query_param(SESSION_PARAM)
        return False
    email = str(payload.get("user_email") or payload.get("email") or "").strip().lower()
    st.session_state["user_email"] = email
    st.session_state["auth_user_id"] = str(payload.get("auth_user_id") or "")
    st.session_state["full_name"] = str(payload.get("full_name") or "")
    st.session_state["preferred_language_code"] = str(payload.get("preferred_language_code") or "en").strip().lower() or "en"
    st.session_state["subscription_status"] = str(payload.get("subscription_status") or "free").strip().lower()
    st.session_state["signed_session_token"] = token
    if bool(payload.get("admin_unlocked")) and _email_is_configured_admin(email):
        st.session_state["admin_unlocked"] = True
    st.session_state["auth_restored_from_url"] = True
    return True


def persist_login_to_signed_url(profile: Dict[str, Any]) -> None:
    payload = _session_payload_from_profile(profile)
    email = payload.get("user_email")
    if not email:
        return

    # Reuse the existing signed token if the user/session payload is unchanged and
    # the token is not close to expiring. This prevents query-param churn on every rerun.
    existing_token = _current_signed_session_token()
    existing_payload = verify_signed_session(existing_token) if existing_token else None
    if existing_payload and _session_payloads_match(existing_payload, payload):
        expires_in = int(existing_payload.get("exp") or 0) - int(time.time())
        if expires_in > SESSION_REFRESH_WINDOW_SECONDS:
            _persist_token_to_url(existing_token)
            return

    token = make_signed_session(payload)
    _persist_token_to_url(token)


def clear_persisted_login() -> None:
    st.session_state.pop("signed_session_token", None)
    _clear_query_param(SESSION_PARAM)
    _mark_browser_session_clear_needed()


def save_logged_in_user(profile: Dict[str, Any], persist: bool = True) -> None:
    email = str(profile.get("email") or profile.get("user_email") or "").strip().lower()
    if not email:
        return
    st.session_state["user_email"] = email
    st.session_state["auth_user_id"] = str(profile.get("auth_user_id") or "")
    st.session_state["full_name"] = str(profile.get("full_name") or "")
    st.session_state["preferred_language_code"] = str(profile.get("preferred_language_code") or "en").strip().lower() or "en"
    st.session_state["subscription_status"] = str(profile.get("subscription_status") or "free").strip().lower()
    if persist:
        persist_login_to_signed_url(profile)


def clear_login_state() -> None:
    for key in [
        "user_email",
        "auth_user_id",
        "full_name",
        "preferred_language_code",
        "subscription_status",
        "admin_unlocked",
        "auth_restored_from_url",
        "signed_session_token",
    ]:
        st.session_state.pop(key, None)
    clear_persisted_login()


def get_supabase_auth_client():
    url = _secret("SUPABASE_URL")
    anon_key = _secret("SUPABASE_ANON_KEY")
    if not url or not anon_key:
        st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, anon_key)


@st.cache_resource(show_spinner=False)
def get_supabase_admin_client():
    url = _secret("SUPABASE_URL")
    key = _secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


# Compatibility name used across older pages. This is server-side Streamlit; service role is kept centralized.
def get_supabase_client():
    return get_supabase_admin_client()


def get_current_user_email() -> Optional[str]:
    restore_login_from_signed_url()
    email = str(st.session_state.get("user_email") or st.session_state.get("account_email") or "").strip().lower()
    if email and "@" in email:
        return email
    return None


def is_logged_in() -> bool:
    return bool(get_current_user_email())


def get_user_profile(email: Optional[str] = None) -> Optional[Dict[str, Any]]:
    email = str(email or get_current_user_email() or "").strip().lower()
    if not email:
        return None
    try:
        result = (
            get_supabase_admin_client()
            .table("app_users")
            .select("*")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        return (result.data or [None])[0]
    except Exception:
        return None


def get_subscription_status(email: Optional[str] = None) -> str:
    email = str(email or get_current_user_email() or "").strip().lower()
    if not email:
        return FREE_STATUS
    session_status = str(st.session_state.get("subscription_status") or "").strip().lower()
    profile = get_user_profile(email)
    status = str((profile or {}).get("subscription_status") or session_status or FREE_STATUS).strip().lower()
    if profile:
        # Keep persisted signed session current after DB lookup.
        merged = dict(profile)
        merged["email"] = email
        save_logged_in_user(merged, persist=True)
    return status or FREE_STATUS


def get_user_subscription_status(email: Optional[str] = None) -> str:
    return get_subscription_status(email=email)


def is_admin_user(email: Optional[str] = None) -> bool:
    email = str(email or get_current_user_email() or "").strip().lower()
    return _email_is_configured_admin(email)


def is_admin_unlocked() -> bool:
    return bool(st.session_state.get("admin_unlocked") and is_admin_user())


def get_user_access_level(email: Optional[str] = None) -> str:
    email = email or get_current_user_email()
    if not email:
        return "logged_out"
    if is_admin_unlocked():
        return "admin"
    status = get_subscription_status(email)
    if status in PAID_STATUS_VALUES:
        return "paid"
    if status in EXPIRED_STATUS_VALUES:
        return "expired"
    return "free"


def has_premium_access(email: Optional[str] = None) -> bool:
    return get_user_access_level(email) in {"paid", "admin"}


def is_paid_user(email: Optional[str] = None) -> bool:
    return has_premium_access(email)


def get_preferred_language_code(email: Optional[str] = None) -> str:
    profile = get_user_profile(email)
    if profile:
        return str(profile.get("preferred_language_code") or "en").strip().lower() or "en"
    return str(st.session_state.get("preferred_language_code") or "en").strip().lower() or "en"


def require_login() -> str:
    email = get_current_user_email()
    if not email:
        st.warning("Please log in from the Account page before continuing.")
        st.page_link("pages/Account.py", label="Go to Account", icon="👤")
        st.stop()
    return email


def show_locked_premium_message(feature_name: str = "This feature") -> None:
    st.warning(f"{feature_name} is available with Premium Access.")
    st.info("Free users can use Account, Support, and the Free Preview. Upgrade to unlock full exams, practice, progress, and readiness.")


def require_paid_access(feature_name: str = "This feature") -> bool:
    email = require_login()
    if not has_premium_access(email):
        show_locked_premium_message(feature_name)
        st.stop()
    return True


def _current_session_profile_for_persistence() -> Dict[str, Any]:
    return {
        "email": str(st.session_state.get("user_email") or "").strip().lower(),
        "auth_user_id": str(st.session_state.get("auth_user_id") or ""),
        "full_name": str(st.session_state.get("full_name") or ""),
        "preferred_language_code": str(st.session_state.get("preferred_language_code") or "en").strip().lower() or "en",
        "subscription_status": str(st.session_state.get("subscription_status") or "free").strip().lower(),
    }


def unlock_admin(password: str) -> bool:
    if not is_admin_user():
        return False
    expected = _secret("ADMIN_PASSWORD")
    if expected and hmac.compare_digest(str(password or ""), expected):
        st.session_state["admin_unlocked"] = True
        persist_login_to_signed_url(_current_session_profile_for_persistence())
        return True
    return False


def require_admin() -> bool:
    email = require_login()
    if not is_admin_user(email):
        st.error("Admin access required.")
        st.stop()
    if not is_admin_unlocked():
        st.warning("Admin password required before accessing this page.")
        st.page_link("pages/Account.py", label="Go to Account / Admin Unlock", icon="👤")
        st.stop()
    return True


def _hide_native_sidebar_nav_css() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stSidebarNav"] {display: none !important;}
        section[data-testid="stSidebar"] nav {display: none !important;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _streamlit_route_for_page(page_path: str) -> str:
    """Return Streamlit multipage route path for a script path."""
    page_path = str(page_path or "").strip()
    if page_path == "app.py":
        return "/"
    if page_path.startswith("pages/") and page_path.endswith(".py"):
        return "/" + page_path.rsplit("/", 1)[-1][:-3]
    return "/"


def _session_preserving_href(page_path: str) -> str:
    """Build a navigation URL that keeps the signed session query parameter."""
    route = _streamlit_route_for_page(page_path)
    token = _current_signed_session_token()
    if token:
        return f"{route}?{SESSION_PARAM}={quote(token, safe='')}"
    return route


def _sidebar_nav_link(page_path: str, label: str, icon: str = "") -> None:
    """Render a sidebar link that preserves fr_session across Streamlit pages."""
    href = _session_preserving_href(page_path)
    safe_label = str(label).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_icon = str(icon).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    st.markdown(
        f"""
        <a href="{href}" target="_self" style="
            display: block;
            padding: 0.35rem 0.25rem;
            text-decoration: none;
            color: inherit;
            border-radius: 0.35rem;
        ">{safe_icon} {safe_label}</a>
        """,
        unsafe_allow_html=True,
    )




def render_session_page_link(page_path: str, label: str, icon: str = "") -> None:
    """Render a page link that preserves fr_session across hard refreshes.

    Do not use st.page_link for authenticated navigation because it drops query
    parameters. This anchor keeps the signed session token in the URL.
    """
    href = _session_preserving_href(page_path)
    safe_label = str(label).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_icon = str(icon).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    st.markdown(
        f"""
        <a href="{href}" target="_self" style="
            display: inline-block;
            padding: 0.45rem 0.70rem;
            margin: 0.15rem 0;
            text-decoration: none;
            color: inherit;
            border: 1px solid rgba(49, 51, 63, 0.20);
            border-radius: 0.45rem;
        ">{safe_icon} {safe_label}</a>
        """,
        unsafe_allow_html=True,
    )

def render_sidebar_navigation() -> None:
    restore_login_from_signed_url()
    _hide_native_sidebar_nav_css()
    email = get_current_user_email()
    level = get_user_access_level(email) if email else "logged_out"

    with st.sidebar:
        st.markdown("### Certification Prep")
        if email:
            st.caption(f"Signed in: {email}")
            st.caption(f"Access: {level}")
        else:
            st.caption("Not signed in")

        _sidebar_nav_link("pages/Dashboard.py", label="Dashboard", icon="🏠")
        _sidebar_nav_link("app.py", label="Mock Exam / Free Preview", icon="📝")
        _sidebar_nav_link("pages/Account.py", label="Account", icon="👤")
        _sidebar_nav_link("pages/Support.py", label="Support", icon="🛟")

        st.divider()
        st.markdown("### Premium")
        _sidebar_nav_link("pages/Practice_By_Category.py", label="Practice By Category", icon="📚")
        _sidebar_nav_link("pages/Weak_Areas_Practice.py", label="Weak Areas Practice", icon="🎯")
        _sidebar_nav_link("pages/My_Progress.py", label="My Progress", icon="📈")
        if level not in {"paid", "admin"}:
            st.caption("Premium access required")

        if email and is_admin_user(email):
            st.divider()
            _sidebar_nav_link("pages/Account.py", label="Admin Unlock", icon="🔐")
            if is_admin_unlocked():
                _sidebar_nav_link("pages/Admin_Users.py", label="Admin Users", icon="👥")
                _sidebar_nav_link("pages/Admin_Import.py", label="Admin Import", icon="⬆️")
                _sidebar_nav_link("pages/Admin_Question_Review.py", label="Admin Question Review", icon="✅")
                _sidebar_nav_link("pages/Admin_Support_Tickets.py", label="Admin Support Tickets", icon="🎫")


def render_app_chrome() -> None:
    restore_login_from_signed_url()
    _sync_existing_session_token_to_url()
    _render_browser_session_bridge()
    render_sidebar_navigation()
