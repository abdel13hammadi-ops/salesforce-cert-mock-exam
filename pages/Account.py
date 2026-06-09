import streamlit as st

# Simple, stable Account page
APP_VERSION = "ACCOUNT_V2_VISIBLE_EMAIL_SETUP"

st.title("Account")
st.caption(f"App version: {APP_VERSION}")

st.info("Save the email address you want to use for progress tracking and future paid subscription access.")

if "user_email" not in st.session_state:
    st.session_state["user_email"] = ""

email_input = st.text_input(
    "Email address",
    value=st.session_state.get("user_email", ""),
    placeholder="you@example.com",
)

if st.button("Save Account", type="primary"):
    email = email_input.strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        st.error("Please enter a valid email address.")
    else:
        st.session_state["user_email"] = email
        st.success(f"Account saved ✅ Progress will be saved under: {email}")

st.divider()

st.subheader("Current Account")
current_email = st.session_state.get("user_email", "")

if current_email:
    st.success(f"Current email: {current_email}")
else:
    st.warning("No email saved yet.")

st.divider()

st.subheader("Future Paid Subscription")
st.write("Later, this email setup can be upgraded to Supabase Auth + Stripe subscription access.")

with st.expander("Developer check"):
    st.write("If you can see this, the Account page is loading correctly.")
    st.json({"app_version": APP_VERSION, "user_email": st.session_state.get("user_email", "")})
