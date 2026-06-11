import streamlit as st
from supabase import create_client

# App-wide paid status value
PAID_STATUS = "active"
FREE_STATUS = "free"


def get_supabase_client():
    """Service-role client for server-side profile/subscription lookups."""
    url = st.secrets.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        st.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


def get_current_user_email():
    """Return the current logged-in/saved user email from session state."""
    # New Account/Auth page should set user_email after login.
    email = st.session_state.get("user_email", "")

    # Backward compatibility with older Account page versions.
    if not email:
        email = st.session_state.get("account_email", "")

    email = str(email).strip().lower()
    if email and "@" in email:
        return email
    return None


def get_current_user():
    """Return a simple current user dict, or None if not logged in."""
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
    """Fetch the app_users profile for an email."""
    email = (email or get_current_user_email() or "").strip().lower()
    if not email:
        return None

    supabase = get_supabase_client()
    result = (
        supabase.table("app_users")
        .select("*")
        .eq("email", email)
        .limit(1)
        .execute()
    )

    if result.data:
        return result.data[0]
    return None


def get_subscription_status(email=None):
    """Return subscription_status for the user. Defaults to free."""
    profile = get_user_profile(email=email)
    if not profile:
        return FREE_STATUS
    return str(profile.get("subscription_status") or FREE_STATUS).strip().lower()


# Backward-compatible function name used by older app.py versions.
def get_user_subscription_status(email=None):
    return get_subscription_status(email=email)


def is_paid_user(email=None):
    return get_subscription_status(email=email) == PAID_STATUS


def get_preferred_language_code(email=None):
    profile = get_user_profile(email=email)
    if profile:
        return str(profile.get("preferred_language_code") or "en").strip().lower()
    return str(st.session_state.get("preferred_language_code", "en") or "en").strip().lower()


def require_login():
    email = get_current_user_email()
    if not email:
        st.warning("Please go to the Account page and log in first.")
        st.stop()
    return email


def require_paid_access(feature_name="This feature"):
    email = require_login()
    status = get_subscription_status(email=email)

    if status != PAID_STATUS:
        st.error(f"{feature_name} is available for paid users only.")
        st.info("Please upgrade your account to unlock this feature.")
        st.stop()

    return True
    
# Backward compatibility for older app.py imports
PAID_STATUS = "active"
PAID_STATUS_VALUES = ["active", "paid", "trialing"]

def get_user_subscription_status(email=None):
    return get_subscription_status(email=email)


# =========================================================
# Admin-only access helpers
# =========================================================

def _normalize_email(value):
    return str(value or "").strip().lower()


def get_admin_emails():
    """Read ADMIN_EMAILS from Streamlit secrets.

    Supported formats in Streamlit secrets:
      ADMIN_EMAILS = "admin@example.com, second@example.com"
    or
      ADMIN_EMAILS = ["admin@example.com", "second@example.com"]

    Security note: the secrets-based allowlist is the strongest admin control here.
    Do not store ADMIN_EMAILS in code.
    """
    try:
        raw = st.secrets.get("ADMIN_EMAILS", "")
    except Exception:
        raw = ""

    emails = []
    if isinstance(raw, (list, tuple, set)):
        emails = list(raw)
    else:
        # Streamlit secrets may return a string. Support comma or newline separated values.
        raw_text = str(raw or "")
        emails = raw_text.replace("\n", ",").split(",")

    return {_normalize_email(email) for email in emails if _normalize_email(email)}


def is_admin_user(email=None):
    """Return True only for users explicitly allowed as admins.

    Primary control: ADMIN_EMAILS in Streamlit secrets.
    Optional DB fallback: app_users.is_admin = true or app_users.role = 'admin'
    if those columns exist. If the columns do not exist, this safely falls back
    to the ADMIN_EMAILS allowlist only.
    """
    email = _normalize_email(email or get_current_user_email())
    if not email:
        return False

    if email in get_admin_emails():
        return True

    # Optional database role support. This is intentionally secondary.
    try:
        profile = get_user_profile(email=email) or {}
        if profile.get("is_admin") is True:
            return True
        if str(profile.get("role") or "").strip().lower() == "admin":
            return True
    except Exception:
        pass

    return False


def require_admin_access(page_name="this admin page"):
    """Hard stop for admin pages.

    Add this near the top of every admin page after st.set_page_config().
    It prevents non-admin users from using admin pages even if they open the
    page URL directly.
    """
    email = require_login()

    if not is_admin_user(email):
        st.error("Admin access required.")
        st.info("You are signed in, but this page is restricted to administrators only.")
        st.stop()

    return email
