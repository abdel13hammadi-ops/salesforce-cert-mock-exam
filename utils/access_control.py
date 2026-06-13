import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import streamlit as st
from supabase import create_client

# App-wide paid status values
PAID_STATUS = "active"
FREE_STATUS = "free"
PAID_STATUS_VALUES = {"active", "paid", "premium", "subscribed"}
TRIAL_STATUS_VALUES = {"trialing", "trial"}
AUTH_COOKIE_NAME = "fr_auth_session_v3"
AUTH_SESSION_DAYS = 30


def hide_streamlit_native_navigation() -> None:
    """Hide Streamlit's automatic multipage navigation/sidebar page list."""
    st.markdown(
        """
        <style>
        [data-testid="stSidebarNav"] {display: none !important;}
        section[data-testid="stSidebar"] nav {display: none !important;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def get_secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def get_supabase_client():
    """Service-role client for server-side profile/subscription lookups.

    Existing app pages still rely on this. Longer-term, user-facing reads should move
    to anon/JWT + RLS, but Phase 2 must first stabilize auth/session behavior.
    """
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


def get_supabase_auth_client():
    """Fresh anon Supabase client used only for sign-in/sign-up."""
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_ANON_KEY")
    if not url or not key:
        st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


def _cookie_password() -> str:
    return get_secret("COOKIE_PASSWORD") or get_secret("ADMIN_PASSWORD") or ""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("utf-8"))


def _sign(payload_b64: str) -> str:
    secret = _cookie_password()
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()


def make_signed_session_token(payload: Dict[str, Any]) -> str:
    """Create a signed session token for browser cookie persistence.

    The token contains no password and no service-role secret. It is HMAC-signed
    using COOKIE_PASSWORD so users cannot edit the email/status without invalidating
    the signature.
    """
    if not _cookie_password():
        return ""
    payload = dict(payload or {})
    payload["exp"] = int((datetime.now(timezone.utc) + timedelta(days=AUTH_SESSION_DAYS)).timestamp())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(payload_b64)
    if not signature:
        return ""
    return f"{payload_b64}.{signature}"


def verify_signed_session_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or "." not in token or not _cookie_password():
        return None
    try:
        payload_b64, signature = token.split(".", 1)
        expected = _sign(payload_b64)
        if not expected or not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        exp = int(payload.get("exp") or 0)
        if exp and exp < int(datetime.now(timezone.utc).timestamp()):
            return None
        email = str(payload.get("user_email") or "").strip().lower()
        if not email or "@" not in email:
            return None
        return payload
    except Exception:
        return None


@st.cache_resource(show_spinner=False)
def get_cookie_manager():
    """Return one CookieManager resource, if available.

    This does NOT call get_all(), which previously caused duplicate key crashes.
    """
    try:
        import extra_streamlit_components as stx
        return stx.CookieManager()
    except Exception:
        return None


def _cookie_get_once(name: str) -> str:
    cache_key = f"_cookie_read_once_{name}"
    if cache_key in st.session_state:
        return st.session_state.get(cache_key) or ""

    value = ""

    # First try Streamlit's server-visible cookies on refresh.
    try:
        value = st.context.cookies.get(name, "") or ""
    except Exception:
        value = ""

    # Fallback to CookieManager.get(name) once per run. Avoid get_all().
    if not value:
        try:
            manager = get_cookie_manager()
            if manager is not None:
                value = manager.get(name) or ""
        except Exception:
            value = ""

    st.session_state[cache_key] = value or ""
    return st.session_state[cache_key]


def persist_login_to_browser(email: str, auth_user_id: str = "", profile: Optional[Dict[str, Any]] = None) -> None:
    """Persist a signed auth summary to a browser cookie.

    This is intentionally minimal: enough to restore the app session after a hard
    refresh. It does not store a password or service-role key.
    """
    email = str(email or "").strip().lower()
    if not email:
        return

    profile = profile or {}
    token = make_signed_session_token({
        "user_email": email,
        "auth_user_id": auth_user_id or profile.get("auth_user_id") or "",
        "full_name": profile.get("full_name") or "",
        "preferred_language_code": profile.get("preferred_language_code") or "en",
        "subscription_status": profile.get("subscription_status") or "free",
    })
    if not token:
        return

    expires = datetime.now() + timedelta(days=AUTH_SESSION_DAYS)
    try:
        manager = get_cookie_manager()
        if manager is not None:
            manager.set(AUTH_COOKIE_NAME, token, expires_at=expires, key="set_fr_auth_session_v3")
    except Exception:
        pass

    # Clear stale cookie read cache so same run can see current value if needed.
    st.session_state.pop(f"_cookie_read_once_{AUTH_COOKIE_NAME}", None)


def clear_browser_login() -> None:
    try:
        manager = get_cookie_manager()
        if manager is not None:
            manager.delete(AUTH_COOKIE_NAME, key="delete_fr_auth_session_v3")
    except Exception:
        pass
    st.session_state.pop(f"_cookie_read_once_{AUTH_COOKIE_NAME}", None)


def restore_login_from_browser() -> bool:
    """Restore st.session_state from signed browser cookie after hard refresh."""
    hide_streamlit_native_navigation()

    if st.session_state.get("user_email"):
        return True

    token = _cookie_get_once(AUTH_COOKIE_NAME)
    payload = verify_signed_session_token(token)
    if not payload:
        return False

    st.session_state["user_email"] = str(payload.get("user_email") or "").strip().lower()
    st.session_state["auth_user_id"] = payload.get("auth_user_id") or ""
    st.session_state["full_name"] = payload.get("full_name") or ""
    st.session_state["preferred_language_code"] = payload.get("preferred_language_code") or "en"
    st.session_state["subscription_status"] = payload.get("subscription_status") or "free"
    return bool(st.session_state.get("user_email"))


def save_logged_in_user(email: str, auth_user_id: str = "", profile: Optional[Dict[str, Any]] = None) -> None:
    email = str(email or "").strip().lower()
    st.session_state["user_email"] = email
    st.session_state["auth_user_id"] = auth_user_id or ""

    profile = profile or {}
    st.session_state["full_name"] = profile.get("full_name") or ""
    st.session_state["preferred_language_code"] = profile.get("preferred_language_code") or "en"
    st.session_state["subscription_status"] = profile.get("subscription_status") or "free"
    persist_login_to_browser(email, auth_user_id, profile)


def clear_login_session() -> None:
    for key in [
        "user_email",
        "auth_user_id",
        "full_name",
        "preferred_language_code",
        "subscription_status",
        "admin_unlocked",
    ]:
        st.session_state.pop(key, None)
    clear_browser_login()


def get_current_user_email():
    """Return the current logged-in/saved user email from session state/cookie."""
    restore_login_from_browser()
    email = st.session_state.get("user_email", "") or st.session_state.get("account_email", "")
    email = str(email).strip().lower()
    if email and "@" in email:
        return email
    return None


def get_current_user():
    email = get_current_user_email()
    if not email:
        return None
    return {
        "email": email,
        "auth_user_id": st.session_state.get("auth_user_id"),
        "full_name": st.session_state.get("full_name", ""),
        "preferred_language_code": st.session_state.get("preferred_language_code", "en"),
    }


def get_user_profile(email=None):
    email = (email or get_current_user_email() or "").strip().lower()
    if not email:
        return None
    supabase = get_supabase_client()
    result = supabase.table("app_users").select("*").eq("email", email).limit(1).execute()
    if result.data:
        profile = result.data[0]
        # Keep session/cookie status fresh after restore.
        save_logged_in_user(email, profile.get("auth_user_id") or st.session_state.get("auth_user_id", ""), profile)
        return profile
    return None


def get_subscription_status(email=None):
    profile = get_user_profile(email=email)
    if profile:
        return str(profile.get("subscription_status") or FREE_STATUS).strip().lower()
    return str(st.session_state.get("subscription_status") or FREE_STATUS).strip().lower()


# Backward-compatible function name used by older app.py versions.
def get_user_subscription_status(email=None):
    return get_subscription_status(email=email)


def is_paid_user(email=None):
    status = get_subscription_status(email=email)
    if status in PAID_STATUS_VALUES:
        return True
    allow_trial = get_secret("ALLOW_TRIAL_AS_PAID", "false").lower() in {"true", "1", "yes"}
    return allow_trial and status in TRIAL_STATUS_VALUES


def get_preferred_language_code(email=None):
    profile = get_user_profile(email=email)
    if profile:
        return str(profile.get("preferred_language_code") or "en").strip().lower()
    return str(st.session_state.get("preferred_language_code", "en") or "en").strip().lower()


def require_login():
    email = get_current_user_email()
    if not email:
        st.warning("Please log in from the Account page first.")
        st.stop()
    return email


def require_paid_access(feature_name="This feature"):
    email = require_login()
    if not is_paid_user(email=email):
        st.warning(f"{feature_name} is available for paid users only.")
        st.info("Please upgrade your account to unlock this feature.")
        st.stop()
    return True


def is_admin_user(email=None):
    email = (email or get_current_user_email() or "").strip().lower()
    admin_emails = [e.strip().lower() for e in get_secret("ADMIN_EMAILS", "").replace(";", ",").split(",") if e.strip()]
    return bool(email and email in admin_emails)


def is_admin_unlocked():
    return bool(st.session_state.get("admin_unlocked")) and is_admin_user()


def require_admin():
    email = require_login()
    if not is_admin_user(email):
        st.error("Admin access required.")
        st.stop()
    if not st.session_state.get("admin_unlocked"):
        st.warning("Unlock admin access from the Admin page first.")
        st.stop()
    return True
