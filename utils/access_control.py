import streamlit as st
from supabase import create_client

FREE_STATUS_VALUES = {"free", "trial", "inactive", "cancelled", "past_due", ""}
PAID_STATUS_VALUES = {"active", "paid", "premium", "subscribed"}


def get_supabase_client():
    """Create Supabase client from Streamlit secrets."""
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        st.error("Supabase secrets are missing. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()

    return create_client(url, key)


def get_current_user_email():
    """Return the email saved from the Account page."""
    email = str(st.session_state.get("user_email", "")).strip().lower()

    if email and "@" in email and "." in email.split("@")[-1]:
        return email

    return None


def get_user_subscription_status(email=None):
    """Read subscription_status from app_users. Defaults to free if no user exists."""
    if email is None:
        email = get_current_user_email()

    if not email:
        return "free"

    supabase = get_supabase_client()
    result = (
        supabase.table("app_users")
        .select("subscription_status")
        .eq("email", email)
        .limit(1)
        .execute()
    )

    if not result.data:
        return "free"

    status = str(result.data[0].get("subscription_status") or "free").strip().lower()
    return status or "free"


def is_paid_user(email=None):
    """Return True when user's subscription_status is active/paid."""
    status = get_user_subscription_status(email)
    return status in PAID_STATUS_VALUES


def require_account():
    """Block page until user saves an email on Account page."""
    email = get_current_user_email()

    if not email:
        st.warning("Please open the Account page and save your email first.")
        st.stop()

    return email


def require_paid_access(feature_name="this premium feature"):
    """Block page unless user has paid/active subscription."""
    email = require_account()
    status = get_user_subscription_status(email)

    if status not in PAID_STATUS_VALUES:
        st.warning(f"{feature_name} is available for paid users only.")
        st.info("Your current plan is Free. Later, Stripe will upgrade this automatically after payment. For now, you can manually set subscription_status = 'active' in Supabase for testing.")
        st.stop()

    return email
