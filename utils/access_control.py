import streamlit as st
from supabase import create_client

APP_VERSION = "ACCESS_CONTROL_V2_SUPABASE_AUTH"


def get_supabase_client(use_service_role=True):
    """Create Supabase client.

    use_service_role=True is used for trusted server-side reads/writes from Streamlit.
    The anon key can be used for auth pages, but service role is better for profile lookups.
    """
    url = st.secrets.get("SUPABASE_URL", "")
    key_name = "SUPABASE_SERVICE_ROLE_KEY" if use_service_role else "SUPABASE_ANON_KEY"
    key = st.secrets.get(key_name, "")

    if not url or not key:
        st.error(f"Missing Supabase secret: SUPABASE_URL or {key_name}")
        st.stop()

    return create_client(url, key)


def get_current_user_email():
    """Return the logged-in user's email from session state.

    Account.py should set st.session_state['user_email'] after successful login/signup.
    This function also supports older session keys to avoid breaking existing pages.
    """
    email = (
        st.session_state.get("user_email")
        or st.session_state.get("account_email")
        or st.session_state.get("auth_user_email")
        or ""
    )
    email = str(email).strip().lower()

    if email and "@" in email:
        return email

    return None


def get_current_user_id():
    """Return Supabase Auth user id if Account.py stored it in session state."""
    user_id = st.session_state.get("auth_user_id") or st.session_state.get("user_id")
    if user_id:
        return str(user_id)
    return None


def is_logged_in():
    return get_current_user_email() is not None


def require_login():
    """Stop page unless user is logged in."""
    if not is_logged_in():
        st.warning("Please log in from the Account page to continue.")
        st.stop()


def get_user_profile(email=None):
    """Load app_users profile for current/supplied email."""
    email = (email or get_current_user_email() or "").strip().lower()
    if not email:
        return None

    supabase = get_supabase_client(use_service_role=True)
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
    """Return subscription status from app_users. Defaults to free."""
    profile = get_user_profile(email)
    if not profile:
        return "free"

    status = profile.get("subscription_status") or "free"
    return str(status).strip().lower()


def is_paid_user(email=None):
    return get_subscription_status(email) in ["active", "paid", "trialing"]


def require_paid_access(feature_name="This feature"):
    """Require login and active subscription."""
    require_login()

    if not is_paid_user():
        st.error(f"{feature_name} is available for paid users only.")
        st.info("Your free account includes the fixed sample mock exam and limited free content.")
        st.stop()


def get_preferred_language(email=None):
    """Return user's preferred language code. Defaults to English."""
    profile = get_user_profile(email)
    if not profile:
        return "en"

    language = profile.get("preferred_language_code") or "en"
    return str(language).strip().lower()


def show_account_status():
    """Optional status banner for pages."""
    email = get_current_user_email()
    if not email:
        st.info("Not logged in. Open Account to sign in.")
        return

    status = get_subscription_status(email)
    language = get_preferred_language(email)

    if status in ["active", "paid", "trialing"]:
        st.success(f"Logged in as {email} | Access: {status} | Language: {language}")
    else:
        st.info(f"Logged in as {email} | Access: {status} | Language: {language}")
