"""Centralized auth, access control, and navigation helpers for the Streamlit app.

This project runs on Streamlit Cloud, where `st.session_state` is wiped on a full
browser refresh. To make refresh persistence reliable without depending on flaky
component cookies, this module stores a short signed session token in the URL query
parameter `fr_session`. The token is HMAC-signed with COOKIE_PASSWORD and expires.
It contains no password or Supabase service key.

This is still an MVP approach, not bank-grade auth. For a production SaaS app,
move to a framework with first-class Supabase Auth session handling.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

import streamlit as st
from supabase import create_client

PAID_STATUS_VALUES = {"active", "paid", "premium", "subscribed"}
EXPIRED_STATUS_VALUES = {"expired", "cancelled", "canceled", "past_due", "unpaid"}
FREE_STATUS = "free"
SESSION_PARAM = "fr_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30


def _secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default) or "").strip()
    except Exception:
        return default


def _signing_secret() -> str:
    # COOKIE_PASSWORD is intentionally reused as the signing secret for the URL session token.
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
    st.session_state["user_email"] = str(payload.get("user_email") or payload.get("email") or "").strip().lower()
    st.session_state["auth_user_id"] = str(payload.get("auth_user_id") or "")
    st.session_state["full_name"] = str(payload.get("full_name") or "")
    st.session_state["preferred_language_code"] = str(payload.get("preferred_language_code") or "en").strip().lower() or "en"
    st.session_state["subscription_status"] = str(payload.get("subscription_status") or "free").strip().lower()
    st.session_state["auth_restored_from_url"] = True
    return True


def persist_login_to_signed_url(profile: Dict[str, Any]) -> None:
    email = str(profile.get("email") or profile.get("user_email") or "").strip().lower()
    if not email:
        return
    payload = {
        "user_email": email,
        "auth_user_id": str(profile.get("auth_user_id") or ""),
        "full_name": str(profile.get("full_name") or ""),
        "preferred_language_code": str(profile.get("preferred_language_code") or "en").strip().lower() or "en",
        "subscription_status": str(profile.get("subscription_status") or "free").strip().lower(),
    }
    _set_query_param(SESSION_PARAM, make_signed_session(payload))


def clear_persisted_login() -> None:
    _clear_query_param(SESSION_PARAM)


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
    admin_emails = [e.strip().lower() for e in _secret("ADMIN_EMAILS").split(",") if e.strip()]
    return bool(email and email in admin_emails)


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


def unlock_admin(password: str) -> bool:
    if not is_admin_user():
        return False
    expected = _secret("ADMIN_PASSWORD")
    if expected and hmac.compare_digest(str(password or ""), expected):
        st.session_state["admin_unlocked"] = True
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

        st.page_link("app.py", label="Mock Exam / Free Preview", icon="📝")
        st.page_link("pages/Account.py", label="Account", icon="👤")
        st.page_link("pages/Support.py", label="Support", icon="🛟")

        st.divider()
        st.markdown("### Premium")
        st.page_link("pages/Practice_By_Category.py", label="Practice By Category", icon="📚")
        st.page_link("pages/Weak_Areas_Practice.py", label="Weak Areas Practice", icon="🎯")
        st.page_link("pages/My_Progress.py", label="My Progress", icon="📈")
        if level not in {"paid", "admin"}:
            st.caption("Premium access required")

        if email and is_admin_user(email):
            st.divider()
            st.page_link("pages/Account.py", label="Admin Unlock", icon="🔐")
            if is_admin_unlocked():
                st.page_link("pages/Admin_Import.py", label="Admin Import", icon="⬆️")
                st.page_link("pages/Admin_Question_Review.py", label="Admin Question Review", icon="✅")
                st.page_link("pages/Admin_Support_Tickets.py", label="Admin Support Tickets", icon="🎫")


def render_app_chrome() -> None:
    restore_login_from_signed_url()
    render_sidebar_navigation()
