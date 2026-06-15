from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from utils.access_control import get_supabase_admin_client, get_supabase_auth_client, require_admin, render_app_chrome


st.set_page_config(page_title="Admin Users", page_icon="👥", layout="wide")
render_app_chrome()
require_admin()

supabase = get_supabase_admin_client()

st.title("👥 Admin Users")
st.caption("Search users, grant/revoke premium access, send reset links, view attempts, and remove app-level profiles. Version: ADMIN_USERS_RESET_REDIRECT_ENV_V2")


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_app_base_url() -> str:
    """Read the production base URL from Render env first, then Streamlit secrets fallback."""
    base = str(os.environ.get("APP_BASE_URL", "") or "").strip()
    if not base:
        try:
            base = str(st.secrets.get("APP_BASE_URL", "") or "").strip()
        except Exception:
            base = ""
    return base.rstrip("/")


def is_auth_email_rate_limit_error(exc: Exception) -> bool:
    """Detect Supabase Auth email-rate-limit errors without exposing internals."""
    msg = str(exc or "").lower()
    return (
        "email rate limit" in msg
        or "rate limit exceeded" in msg
        or "for security purposes" in msg
        or "too many requests" in msg
        or "429" in msg
    )


def format_password_reset_error(exc: Exception) -> str:
    if is_auth_email_rate_limit_error(exc):
        return (
            "Password reset email limit reached. Wait 30–60 minutes before sending another reset email. "
            "Do not keep clicking the button; Supabase will keep blocking repeated email sends."
        )
    return "Could not send password reset email. Check Supabase Auth settings and the user email."


def password_reset_cooldown_key(email: str) -> str:
    return f"password_reset_sent_at::{str(email or '').strip().lower()}"


def reset_on_cooldown(email: str, cooldown_seconds: int = 300) -> tuple[bool, int]:
    """Return (is_on_cooldown, remaining_seconds). Prevents repeated clicks from hammering Supabase."""
    key = password_reset_cooldown_key(email)
    last_sent = st.session_state.get(key)
    if not last_sent:
        return False, 0
    try:
        elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds()
    except Exception:
        return False, 0
    remaining = int(cooldown_seconds - elapsed)
    return remaining > 0, max(remaining, 0)


def mark_password_reset_sent(email: str) -> None:
    st.session_state[password_reset_cooldown_key(email)] = datetime.now(timezone.utc)


def send_password_reset_email(email: str) -> None:
    email = str(email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("Enter a valid email address.")

    auth_client = get_supabase_auth_client()
    base = get_app_base_url()
    redirect_to = f"{base}/Reset_Password" if base else None

    # Supabase Python versions differ. Try the v2 method with redirect first,
    # then fall back to method names/signatures used by older clients.
    last_error = None
    for call_style in ("v2_with_redirect", "v2_no_redirect", "legacy"):
        try:
            if call_style == "v2_with_redirect" and redirect_to:
                auth_client.auth.reset_password_for_email(email, {"redirect_to": redirect_to})
                return
            if call_style == "v2_no_redirect":
                auth_client.auth.reset_password_for_email(email)
                return
            if call_style == "legacy":
                auth_client.auth.reset_password_email(email)
                return
        except AttributeError as exc:
            last_error = exc
            continue
        except TypeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            raise
    raise RuntimeError(f"Password reset is not supported by the installed Supabase client: {last_error}")

def fetch_app_user(email: str) -> Optional[Dict[str, Any]]:
    result = (
        supabase.table("app_users")
        .select("*")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    return (result.data or [None])[0]


def fetch_cert_access(email: str) -> List[Dict[str, Any]]:
    result = (
        supabase.table("user_certification_access")
        .select("id,user_email,exam_name,access_status,access_source,created_at,updated_at")
        .eq("user_email", email)
        .order("updated_at", desc=True)
        .execute()
    )
    return result.data or []


def fetch_attempts(email: str, limit: int = 50) -> List[Dict[str, Any]]:
    result = (
        supabase.table("exam_attempts")
        .select(
            "id,user_email,exam_name,mode,category,score,total_questions,correct_answers,correct_count,started_at,completed_at,language_code"
        )
        .eq("user_email", email)
        .order("completed_at", desc=True)
        .order("started_at", desc=True)
        .order("id", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def upsert_app_user(email: str, status: str) -> None:
    existing = fetch_app_user(email)
    payload = {
        "email": email,
        "subscription_status": status,
        "updated_at": utc_now_iso(),
    }
    if existing:
        supabase.table("app_users").update(payload).eq("email", email).execute()
    else:
        payload["created_at"] = utc_now_iso()
        supabase.table("app_users").insert(payload).execute()


def delete_app_profile(email: str) -> None:
    supabase.table("user_certification_access").delete().eq("user_email", email).execute()
    supabase.table("app_users").delete().eq("email", email).execute()


with st.container(border=True):
    st.subheader("Search User")
    search_email = st.text_input(
        "User email",
        value=st.session_state.get("admin_users_search_email", ""),
        placeholder="user@example.com",
        key="admin_users_email_input",
    )
    col_search, col_clear = st.columns([1, 4])
    with col_search:
        if st.button("Search", type="primary", use_container_width=True):
            st.session_state["admin_users_search_email"] = normalize_email(search_email)
    with col_clear:
        if st.button("Clear", use_container_width=False):
            st.session_state.pop("admin_users_search_email", None)
            st.rerun()

email = normalize_email(st.session_state.get("admin_users_search_email", ""))

if not email:
    st.info("Enter an email and click Search.")
    st.stop()

if "@" not in email:
    st.error("Enter a valid email address.")
    st.stop()

try:
    user = fetch_app_user(email)
    cert_access = fetch_cert_access(email)
    attempts = fetch_attempts(email)
except Exception as exc:
    st.error("Failed to load user data from Supabase.")
    st.exception(exc)
    st.stop()

st.divider()
st.subheader("User Summary")

left, mid, right = st.columns(3)
with left:
    st.metric("Email", email)
with mid:
    st.metric("Subscription", str((user or {}).get("subscription_status") or "no app profile"))
with right:
    st.metric("Exam Attempts", len(attempts))

if user:
    with st.expander("App user row", expanded=False):
        st.json(user)
else:
    st.warning("No app_users profile exists for this email. Granting premium or setting free will create one.")

st.divider()
st.subheader("Password Reset")
st.caption("This sends a secure Supabase password reset email. Admins do not see or set the user's password.")
on_cooldown, remaining = reset_on_cooldown(email)
if on_cooldown:
    st.info(f"A reset email was recently requested for this user. Wait about {remaining} seconds before trying again.")

if st.button("🔐 Send Password Reset Email", use_container_width=True, disabled=on_cooldown):
    try:
        send_password_reset_email(email)
        mark_password_reset_sent(email)
        st.success(f"Password reset email sent to {email} if the Auth account exists.")
        if not get_app_base_url():
            st.info("Admin note: set APP_BASE_URL in Render Environment so reset links return to the Reset Password page.")
    except Exception as exc:
        st.error(format_password_reset_error(exc))
        if not is_auth_email_rate_limit_error(exc):
            st.caption(str(exc))

st.divider()
st.subheader("Access Actions")

col1, col2, col3 = st.columns(3)

with col1:
    if st.button("💎 Grant Premium", use_container_width=True):
        try:
            upsert_app_user(email, "active")
            st.success(f"Premium access granted to {email}.")
            st.session_state["admin_users_search_email"] = email
            st.rerun()
        except Exception as exc:
            st.error("Failed to grant premium access.")
            st.exception(exc)

with col2:
    if st.button("🆓 Set Free", use_container_width=True):
        try:
            upsert_app_user(email, "free")
            st.warning(f"{email} was set to free access.")
            st.session_state["admin_users_search_email"] = email
            st.rerun()
        except Exception as exc:
            st.error("Failed to set user to free.")
            st.exception(exc)

with col3:
    if st.button("⏳ Mark Expired", use_container_width=True):
        try:
            upsert_app_user(email, "expired")
            st.warning(f"{email} was marked expired.")
            st.session_state["admin_users_search_email"] = email
            st.rerun()
        except Exception as exc:
            st.error("Failed to mark user expired.")
            st.exception(exc)

st.divider()
st.subheader("Certification Access Rows")

if cert_access:
    st.dataframe(pd.DataFrame(cert_access), use_container_width=True, hide_index=True)
else:
    st.info("No rows found in user_certification_access for this user.")

st.divider()
st.subheader("Exam Attempts")

if attempts:
    attempts_df = pd.DataFrame(attempts)
    if "correct_answers" in attempts_df.columns and "correct_count" in attempts_df.columns:
        attempts_df["correct_display"] = attempts_df["correct_answers"].fillna(attempts_df["correct_count"])
    st.dataframe(attempts_df, use_container_width=True, hide_index=True)
else:
    st.info("No exam attempts found for this email.")

st.divider()
st.subheader("Danger Zone")
st.warning("This deletes app-level access rows only. It does not delete Supabase Auth login or exam_attempts history.")
confirm_email = st.text_input("Type the user email to confirm app-profile deletion", key="admin_users_delete_confirm")

if st.button("🗑️ Delete App Profile", type="secondary"):
    if normalize_email(confirm_email) != email:
        st.error("Confirmation email does not match. Nothing was deleted.")
    else:
        try:
            delete_app_profile(email)
            st.error(f"Deleted app profile/access rows for {email}. Supabase Auth login was not deleted.")
            st.session_state["admin_users_search_email"] = email
            st.rerun()
        except Exception as exc:
            st.error("Failed to delete app profile.")
            st.exception(exc)

st.caption("Auth user deletion is intentionally not handled here. Delete actual Supabase Auth users from Supabase Dashboard → Authentication → Users.")
