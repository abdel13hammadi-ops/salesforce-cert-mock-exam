from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import streamlit as st
import streamlit.components.v1 as components

from utils.access_control import get_supabase_auth_client, render_sidebar_navigation

try:
    from streamlit_js_eval import streamlit_js_eval
except Exception:
    streamlit_js_eval = None


APP_VERSION = "RESET_PASSWORD_PARENT_HASH_V2"

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


def parse_recovery_tokens_from_url(url: str) -> dict[str, str]:
    """Extract Supabase recovery tokens from query string or fragment."""
    parsed = urlparse(str(url or ""))
    values: dict[str, str] = {}
    for source in (parsed.query, parsed.fragment):
        if not source:
            continue
        for key, val in parse_qs(source).items():
            if val:
                values[key] = val[0]
    return values


def normalize_supabase_recovery_hash_to_query_string() -> None:
    """
    Supabase password reset links usually arrive as:
      /Reset_Password#access_token=...&refresh_token=...&type=recovery

    Streamlit/Python cannot read URL fragments. A Streamlit component runs inside
    an iframe, so window.location is the iframe URL and usually does not contain
    the real browser hash. This script reads window.parent.location and rewrites
    the top-level URL to:
      /Reset_Password?access_token=...&refresh_token=...&type=recovery

    After the rewrite, st.query_params can read the tokens.
    """
    components.html(
        """
        <script>
        (function () {
            function getTopLocation() {
                try {
                    if (window.parent && window.parent.location) {
                        return window.parent.location;
                    }
                } catch (e) {}
                return window.location;
            }

            const loc = getTopLocation();
            const hash = loc.hash || "";
            const search = loc.search || "";
            const path = loc.pathname || "/Reset_Password";

            const hasRecoveryToken =
                hash.includes("access_token=") ||
                hash.includes("refresh_token=") ||
                hash.includes("type=recovery");

            const alreadyHasQueryToken =
                search.includes("access_token=") ||
                search.includes("refresh_token=") ||
                search.includes("type=recovery");

            if (hasRecoveryToken && !alreadyHasQueryToken) {
                const nextUrl = path + "?" + hash.substring(1);
                loc.replace(nextUrl);
            }
        })();
        </script>
        """,
        height=0,
    )


normalize_supabase_recovery_hash_to_query_string()

# Primary path: tokens after JS rewrite.
access_token = get_query_value("access_token")
refresh_token = get_query_value("refresh_token")
recovery_type = get_query_value("type")

# Fallback path: streamlit-js-eval can read the browser URL in some environments.
current_url = ""
if streamlit_js_eval is not None and not (access_token and refresh_token):
    try:
        current_url = streamlit_js_eval(
            js_expressions="window.parent.location.href || window.location.href",
            key="reset_password_current_url_parent_v3",
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
    st.write("Reset page version:", APP_VERSION)

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

            components.html(
                """
                <script>
                (function () {
                    try {
                        if (window.parent && window.parent.history && window.parent.location) {
                            window.parent.history.replaceState({}, document.title, window.parent.location.pathname);
                        } else if (window.history && window.history.replaceState) {
                            window.history.replaceState({}, document.title, window.location.pathname);
                        }
                    } catch (e) {}
                })();
                </script>
                """,
                height=0,
            )
        except Exception as exc:
            st.error("Could not update password. The reset link may be expired or already used.")
            st.caption(str(exc))

st.divider()
st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
