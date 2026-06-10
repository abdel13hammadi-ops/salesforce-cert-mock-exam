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
