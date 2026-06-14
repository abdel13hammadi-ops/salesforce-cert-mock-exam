from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from utils.access_control import get_supabase_admin_client, get_supabase_auth_client, require_admin, render_sidebar_navigation


st.set_page_config(page_title="Admin Users", page_icon="👥", layout="wide")
render_sidebar_navigation()
require_admin()

supabase = get_supabase_admin_client()

st.title("👥 Admin Users")
st.caption("Search users, grant/revoke premium access, view attempts, and remove app-level profiles.")


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def send_password_reset_email(email: str) -> None:
    """Send a Supabase password recovery email. Admin never sees or sets passwords."""
    auth_client = get_supabase_auth_client()
    auth_api = auth_client.auth

    # supabase-py method name differs by version. Try the modern method first,
    # then fallback to older versions.
    if hasattr(auth_api, "reset_password_for_email"):
        auth_api.reset_password_for_email(email)
        return

    if hasattr(auth_api, "reset_password_email"):
        auth_api.reset_password_email(email)
        return

    raise RuntimeError(
        "This installed Supabase Python client does not expose a password-reset method. "
        "Upgrade supabase-py or send the reset from Supabase Dashboard → Authentication → Users."
    )


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
st.subheader("Password Reset")
st.caption(
    "Sends a secure Supabase password recovery email. "
    "The admin never sees or sets the user's password."
)

if st.button("🔐 Send Password Reset Email", use_container_width=False):
    try:
        send_password_reset_email(email)
        st.success(f"Password reset email sent to {email}.")
        st.info("If the user does not receive it, check Supabase Auth email settings and redirect URLs.")
    except Exception as exc:
        st.error("Could not send password reset email.")
        st.caption(str(exc))

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
