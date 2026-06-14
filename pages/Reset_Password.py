from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import streamlit as st
import streamlit.components.v1 as components

from utils.access_control import get_supabase_auth_client, render_sidebar_navigation

try:
    from streamlit_js_eval import streamlit_js_eval
except Exception:
    streamlit_js_eval = None


st.set_page_config(page_title="Reset Password", page_icon="🔐", layout="wide")
render_sidebar_navigation()

st.title("🔐 Reset Password")
st.caption("Use this page after clicking the password reset link sent by email.")


def get_query_value(key: str) -> str:
    """Read Streamlit query params across Streamlit versions."""
    try:
        value = st.query_params.get(key, "")
    except Exception:
        value = ""
    if isinstance(value, list):
        return str(value[0] if value else "")
    return str(value or "")


def parse_recovery_tokens_from_url(url: str) -> dict:
    """Extract Supabase recovery tokens from query string or fragment."""
    parsed = urlparse(str(url or ""))
    values = {}
    for source in (parsed.query, parsed.fragment):
        if not source:
            continue
        for key, val in parse_qs(source).items():
            if val:
                values[key] = val[0]
    return values


# Critical fix:
# Supabase puts recovery tokens after # in the URL. Streamlit's Python backend cannot see
# browser fragments, so this script rewrites:
# /Reset_Password#access_token=...&refresh_token=...&type=recovery
# into:
# /Reset_Password?access_token=...&refresh_token=...&type=recovery
# Then Streamlit can read the tokens from st.query_params.
components.html(
    """
    <script>
    (function () {
      const hash = window.location.hash;
      const search = window.location.search;
      if (hash && hash.length > 1 && hash.includes('access_token=') && !search.includes('access_token=')) {
        const newUrl = window.location.pathname + '?' + hash.substring(1);
        window.location.replace(newUrl);
      }
    })();
    </script>
    """,
    height=0,
)

# Primary path: tokens after JS rewrite.
access_token = get_query_value("access_token")
refresh_token = get_query_value("refresh_token")
recovery_type = get_query_value("type")

# Fallback path: streamlit-js-eval can read full browser URL in some environments.
current_url = ""
if streamlit_js_eval is not None and not (access_token and refresh_token):
    try:
        current_url = streamlit_js_eval(
            js_expressions="window.location.href",
            key="reset_password_current_url_v2",
            want_output=True,
        ) or ""
        params = parse_recovery_tokens_from_url(current_url)
        access_token = access_token or params.get("access_token", "")
        refresh_token = refresh_token or params.get("refresh_token", "")
        recovery_type = recovery_type or params.get("type", "")
    except Exception:
        current_url = ""

with st.expander("Reset link status", expanded=False):
    st.write("Reset link detected:", bool(access_token and refresh_token))
    st.write("Recovery type:", recovery_type or "not found")
    st.write("Browser URL reader installed:", streamlit_js_eval is not None)
    st.write("Query-token detected:", bool(get_query_value("access_token")))

if not access_token or not refresh_token:
    st.warning("No valid password reset session was found on this page.")
    st.info("Open the newest password reset email link in the same browser. Do not copy only the base URL — use the full email link.")
    st.page_link("pages/Account.py", label="Go to Account", icon="👤")
    st.stop()

if recovery_type and recovery_type != "recovery":
    st.warning(f"This link type is '{recovery_type}', not 'recovery'. Request a fresh password reset link.")
    st.stop()

st.success("Valid password reset session detected. Enter a new password below.")

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
            try:
                client.auth.set_session(access_token, refresh_token)
            except TypeError:
                client.auth.set_session({"access_token": access_token, "refresh_token": refresh_token})

            client.auth.update_user({"password": new_password})
            st.success("Password updated. You can now log in with your new password.")
            st.page_link("pages/Account.py", label="Go to Login", icon="👤")

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
