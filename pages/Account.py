import streamlit as st
from supabase import create_client

APP_VERSION = "ACCOUNT_V3_APP_USERS"

st.set_page_config(page_title="Account", layout="wide")


def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error("Supabase secrets are missing. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    email = normalize_email(email)
    return "@" in email and "." in email.split("@")[-1]


def save_user(email: str, full_name: str = ""):
    supabase = get_supabase_client()
    payload = {
        "email": normalize_email(email),
        "full_name": str(full_name or "").strip() or None,
        "subscription_status": "free",
    }
    return supabase.table("app_users").upsert(payload, on_conflict="email").execute()


st.title("Account")
st.caption(f"App version: {APP_VERSION}")

st.markdown(
    """
    Use this page to save your email for progress tracking. Later, this same account email can be connected to paid subscription access.
    """
)

existing_email = st.session_state.get("user_email", "")
existing_name = st.session_state.get("full_name", "")

email = st.text_input("Email", value=existing_email, placeholder="you@example.com")
full_name = st.text_input("Full name (optional)", value=existing_name, placeholder="Your name")

if st.button("Save Account", type="primary"):
    clean_email = normalize_email(email)

    if not is_valid_email(clean_email):
        st.error("Please enter a valid email address.")
    else:
        try:
            save_user(clean_email, full_name)
            st.session_state["user_email"] = clean_email
            st.session_state["full_name"] = str(full_name or "").strip()
            st.success("Account saved ✅")
            st.info(f"Current account email: {clean_email}")
        except Exception as e:
            st.error("Account could not be saved. Check the app_users table and Streamlit secrets.")
            st.exception(e)

st.divider()

current_email = st.session_state.get("user_email")
if current_email:
    st.success(f"Signed in locally as: {current_email}")
else:
    st.warning("No account email saved yet on this device/session.")

st.markdown(
    """
    **Current access level:** Free  
    Paid subscription status will be connected later using Stripe.
    """
)
