from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import streamlit as st

from utils.access_control import get_supabase_auth_client, render_sidebar_navigation

try:
    from streamlit_js_eval import streamlit_js_eval
except Exception:  # package missing or failed import
    streamlit_js_eval = None


st.set_page_config(page_title="Reset Password", page_icon="🔐", layout="wide")
render_sidebar_navigation()

st.title("🔐 Reset Password")
st.caption("Use this page after clicking the password reset link sent by email.")


def parse_recovery_tokens_from_url(url: str) -> dict:
    """Extract Supabase recovery tokens from URL query string or fragment.

    Supabase commonly returns recovery tokens in the URL fragment:
    #access_token=...&refresh_token=...&type=recovery

    Browsers do not send fragments to the server, so we read the full browser URL
    using streamlit-js-eval and parse it here.
    """
    parsed = urlparse(str(url or ""))
    values = {}
    for source in (parsed.query, parsed.fragment):
        if not source:
            continue
        for key, val in parse_qs(source).items():
            if val:
                values[key] = val[0]
    return values


# Read full browser URL so we can capture Supabase tokens from the fragment.
current_url = ""
if streamlit_js_eval is not None:
    try:
        current_url = streamlit_js_eval(
            js_expressions="window.location.href",
            key="reset_password_current_url",
            want_output=True,
        ) or ""
    except Exception:
        current_url = ""

params = parse_recovery_tokens_from_url(current_url)
access_token = params.get("access_token") or st.query_params.get("access_token", "")
refresh_token = params.get("refresh_token") or st.query_params.get("refresh_token", "")
recovery_type = params.get("type") or st.query_params.get("type", "")

with st.expander("Reset link status", expanded=False):
    st.write("Reset link detected:", bool(access_token and refresh_token))
    st.write("Recovery type:", recovery_type or "not found")
    st.write("Browser URL reader installed:", streamlit_js_eval is not None)

if streamlit_js_eval is None:
    st.error("Password reset page is missing the streamlit-js-eval package.")
    st.info("Add streamlit-js-eval to requirements.txt, deploy, and reboot the app.")
    st.stop()

if not access_token or not refresh_token:
    st.warning("No valid password reset session was found on this page.")
    st.info("Go to Account → Forgot Password and send yourself a fresh reset link, then open that link in the same browser.")
    st.page_link("pages/Account.py", label="Go to Account", icon="👤")
    st.stop()

new_password = st.text_input("New password", type="password")
confirm_password = st.text_input("Confirm new password", type="password")

if st.button("Update Password", type="primary"):
    if len(new_password) < 8:
        st.warning("Password must be at least 8 characters.")
    elif new_password != confirm_password:
        st.warning("Passwords do not match.")
    else:
        try:
            client = get_supabase_auth_client()
            # Establish recovery session, then update the authenticated user's password.
            try:
                client.auth.set_session(access_token, refresh_token)
            except TypeError:
                client.auth.set_session({"access_token": access_token, "refresh_token": refresh_token})

            client.auth.update_user({"password": new_password})
            st.success("Password updated. You can now log in with your new password.")
            st.page_link("pages/Account.py", label="Go to Login", icon="👤")

            # Remove sensitive tokens from URL after successful reset.
            st.markdown(
                """
                <script>
                if (window.history && window.history.replaceState) {
                    window.history.replaceState({}, document.title, window.location.pathname);
                }
                </script>
                """,
                unsafe_allow_html=True,
            )
        except Exception as exc:
            st.error("Could not update password. The reset link may be expired or already used.")
            st.caption(str(exc))

st.divider()
st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
