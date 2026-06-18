from __future__ import annotations

from datetime import datetime, timezone
import os
import secrets
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.access_control import get_supabase_admin_client, get_supabase_auth_client, require_admin, render_app_chrome


APP_VERSION = "ADMIN_USERS_CREATE_MISSING_USER_SPLIT_NAME_V4"

st.set_page_config(page_title="Admin Users", page_icon="👥", layout="wide")
render_app_chrome()
require_admin()


# SESSION_TIMEOUT_APPLIED
enforce_session_timeout()
show_session_expired_notice()

supabase = get_supabase_admin_client()

st.title("👥 Admin Users")
st.caption(
    "Search users, create missing app/Auth users, grant/revoke premium access, send reset links, "
    f"view attempts, and remove app-level profiles. Version: {APP_VERSION}"
)


# -----------------------------
# Basic helpers
# -----------------------------

def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_app_base_url() -> str:
    """Read production base URL from Render env first, then Streamlit secrets fallback."""
    base = str(os.environ.get("APP_BASE_URL", "") or "").strip()
    if not base:
        try:
            base = str(st.secrets.get("APP_BASE_URL", "") or "").strip()
        except Exception:
            base = ""
    return base.rstrip("/")


def is_duplicate_auth_user_error(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    return any(
        marker in msg
        for marker in (
            "already registered",
            "already exists",
            "user already",
            "duplicate",
            "email_exists",
            "email exists",
        )
    )


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
    return f"password_reset_sent_at::{normalize_email(email)}"


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


# -----------------------------
# Supabase Auth helpers
# -----------------------------

def send_password_reset_email(email: str) -> None:
    email = normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("Enter a valid email address.")

    auth_client = get_supabase_auth_client()
    base = get_app_base_url()
    redirect_to = f"{base}/Reset_Password" if base else None

    # Supabase Python versions differ. Try v2 redirect first, then older method names/signatures.
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
        except Exception:
            raise
    raise RuntimeError(f"Password reset is not supported by the installed Supabase client: {last_error}")


def create_auth_user_if_needed(email: str, full_name: str) -> Tuple[Optional[str], str]:
    """Create a Supabase Auth user with a random temporary password.

    Returns (auth_user_id, status_message). If the Auth user already exists, this is treated as non-fatal
    because the app profile can still be created and a reset email can be sent.
    """
    email = normalize_email(email)
    full_name = str(full_name or "").strip()
    temporary_password = secrets.token_urlsafe(32)

    try:
        response = supabase.auth.admin.create_user({
            "email": email,
            "password": temporary_password,
            "email_confirm": True,
            "user_metadata": {"full_name": full_name},
        })
        user = getattr(response, "user", None)
        auth_user_id = getattr(user, "id", None) if user else None
        return str(auth_user_id) if auth_user_id else None, "Supabase Auth user created."
    except Exception as exc:
        if is_duplicate_auth_user_error(exc):
            return None, "Supabase Auth user already exists. App profile was created/updated."
        raise


# -----------------------------
# Database fetch/write helpers
# -----------------------------

def fetch_app_user(email: str) -> Optional[Dict[str, Any]]:
    result = (
        supabase.table("app_users")
        .select("*")
        .eq("email", normalize_email(email))
        .limit(1)
        .execute()
    )
    return (result.data or [None])[0]


def fetch_languages() -> List[Dict[str, Any]]:
    try:
        result = (
            supabase.table("languages")
            .select("language_code, language_name, native_name, is_active, display_order")
            .eq("is_active", True)
            .order("display_order")
            .execute()
        )
        rows = result.data or []
        return rows or [{"language_code": "en", "language_name": "English", "native_name": "English"}]
    except Exception:
        return [{"language_code": "en", "language_name": "English", "native_name": "English"}]


def language_label(row: Dict[str, Any]) -> str:
    native = row.get("native_name") or row.get("language_name") or row.get("language_code")
    name = row.get("language_name") or native
    code = row.get("language_code")
    return f"{name} ({code})" if native == name else f"{name} / {native} ({code})"


def fetch_active_certifications() -> List[Dict[str, Any]]:
    try:
        result = (
            supabase.table("certifications")
            .select("exam_name, display_name, certification_code, is_active")
            .eq("is_active", True)
            .order("display_name")
            .execute()
        )
        return result.data or []
    except Exception:
        return []


def fetch_cert_access(email: str) -> List[Dict[str, Any]]:
    result = (
        supabase.table("user_certification_access")
        .select("id,user_email,exam_name,access_status,access_source,created_at,updated_at")
        .eq("user_email", normalize_email(email))
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
        .eq("user_email", normalize_email(email))
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
        "email": normalize_email(email),
        "subscription_status": str(status or "free").strip().lower(),
        "updated_at": utc_now_iso(),
    }
    if existing:
        supabase.table("app_users").update(payload).eq("email", normalize_email(email)).execute()
    else:
        payload["created_at"] = utc_now_iso()
        supabase.table("app_users").insert(payload).execute()


def create_app_user_profile(
    email: str,
    full_name: str,
    preferred_language_code: str,
    subscription_status: str,
    auth_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    email = normalize_email(email)
    payload: Dict[str, Any] = {
        "email": email,
        "full_name": str(full_name or "").strip(),
        "preferred_language_code": str(preferred_language_code or "en").strip().lower() or "en",
        "subscription_status": str(subscription_status or "free").strip().lower(),
        "updated_at": utc_now_iso(),
    }
    if auth_user_id:
        payload["auth_user_id"] = auth_user_id

    existing = fetch_app_user(email)
    if existing:
        supabase.table("app_users").update(payload).eq("email", email).execute()
    else:
        payload["created_at"] = utc_now_iso()
        supabase.table("app_users").insert(payload).execute()

    return fetch_app_user(email) or payload


def create_or_update_cert_access(email: str, exam_names: List[str], access_status: str = "active") -> None:
    email = normalize_email(email)
    selected = [str(name or "").strip() for name in exam_names if str(name or "").strip()]
    if not selected:
        return

    existing_rows = fetch_cert_access(email)
    existing_by_exam = {row.get("exam_name"): row for row in existing_rows if row.get("exam_name")}

    for exam_name in selected:
        payload = {
            "user_email": email,
            "exam_name": exam_name,
            "access_status": access_status,
            "access_source": "admin_created",
            "updated_at": utc_now_iso(),
        }
        existing = existing_by_exam.get(exam_name)
        if existing and existing.get("id"):
            supabase.table("user_certification_access").update(payload).eq("id", existing["id"]).execute()
        else:
            payload["created_at"] = utc_now_iso()
            supabase.table("user_certification_access").insert(payload).execute()


def delete_app_profile(email: str) -> None:
    email = normalize_email(email)
    supabase.table("user_certification_access").delete().eq("user_email", email).execute()
    supabase.table("app_users").delete().eq("email", email).execute()


# -----------------------------
# Search UI
# -----------------------------
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
            st.rerun()
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
except Exception as exc:
    st.error("Failed to load user data from Supabase.")
    st.exception(exc)
    st.stop()


# -----------------------------
# Missing user path: no fake actions, no attempts, no reset panel.
# -----------------------------
if not user:
    st.divider()
    st.subheader("User does not exist")
    st.warning(f"No app user profile exists for **{email}**.")
    st.info("Do you want to create it?")

    languages = fetch_languages()
    language_codes = [row.get("language_code") for row in languages if row.get("language_code")] or ["en"]
    label_by_code = {row.get("language_code"): language_label(row) for row in languages if row.get("language_code")}

    certifications = fetch_active_certifications()
    cert_names = [row.get("exam_name") for row in certifications if row.get("exam_name")]
    cert_label_by_name = {
        row.get("exam_name"): (row.get("display_name") or row.get("exam_name"))
        for row in certifications
        if row.get("exam_name")
    }

    with st.form("admin_create_missing_user_form", clear_on_submit=False):
        st.markdown("### Create New User")
        st.caption("This creates the app profile. It can also create the Supabase Auth login and send the user a reset link.")
        st.caption("First and last name are captured separately in the admin UI, then stored as app_users.full_name for backward compatibility.")

        name_col1, name_col2 = st.columns(2)
        with name_col1:
            first_name = st.text_input("First name", key="admin_create_first_name")
        with name_col2:
            last_name = st.text_input("Last name", key="admin_create_last_name")

        preferred_language = st.selectbox(
            "Preferred language",
            options=language_codes,
            index=language_codes.index("en") if "en" in language_codes else 0,
            format_func=lambda code: label_by_code.get(code, code),
            key="admin_create_language",
        )
        subscription_status = st.selectbox(
            "Subscription status",
            options=["free", "active", "expired"],
            index=0,
            help="Use active for paid/premium users. Use free for unpaid users.",
            key="admin_create_subscription_status",
        )

        selected_certifications: List[str] = []
        if cert_names:
            default_certs = cert_names if subscription_status == "active" else []
            selected_certifications = st.multiselect(
                "Certification access rows to create",
                options=cert_names,
                default=default_certs,
                format_func=lambda name: cert_label_by_name.get(name, name),
                help="Optional. Paid users can also access all active certifications by subscription status, but rows are useful for explicit tracking.",
                key="admin_create_certifications",
            )
        else:
            st.warning("No active certification rows found. User can still be created, but no certification access rows will be created.")

        create_auth_login = st.checkbox(
            "Create Supabase Auth login if missing",
            value=True,
            help="Creates the login account using a random temporary password. Admin never sees the password.",
            key="admin_create_auth_login",
        )
        send_reset_after_create = st.checkbox(
            "Send password reset email after create",
            value=True,
            help="Recommended. This lets the user set their own password securely.",
            key="admin_create_send_reset",
        )

        submitted = st.form_submit_button("Create User", type="primary", use_container_width=True)

    if submitted:
        first_name = str(first_name or "").strip()
        last_name = str(last_name or "").strip()
        full_name = f"{first_name} {last_name}".strip()

        if not first_name or not last_name:
            st.error("First name and last name are required.")
            st.stop()

        try:
            auth_user_id: Optional[str] = None
            auth_message = "Supabase Auth login was not requested."

            if create_auth_login:
                auth_user_id, auth_message = create_auth_user_if_needed(email, full_name)

            created_profile = create_app_user_profile(
                email=email,
                full_name=full_name,
                preferred_language_code=preferred_language,
                subscription_status=subscription_status,
                auth_user_id=auth_user_id,
            )

            if selected_certifications:
                create_or_update_cert_access(email, selected_certifications, access_status="active")

            reset_message = "Password reset email was not requested."
            if send_reset_after_create:
                try:
                    send_password_reset_email(email)
                    mark_password_reset_sent(email)
                    reset_message = "Password reset email sent."
                except Exception as reset_exc:
                    reset_message = f"User created, but reset email failed: {format_password_reset_error(reset_exc)}"

            st.success(f"Created app user profile for {email}.")
            st.info(auth_message)
            st.info(reset_message)
            with st.expander("Created profile", expanded=False):
                st.json(created_profile)

            st.session_state["admin_users_search_email"] = email
            st.rerun()
        except Exception as exc:
            st.error("Failed to create user.")
            st.exception(exc)

    st.caption("No password reset, access actions, certification rows, exam attempts, or danger-zone actions are shown until the user exists.")
    st.stop()


# -----------------------------
# Existing user path: original admin actions.
# -----------------------------
try:
    cert_access = fetch_cert_access(email)
    attempts = fetch_attempts(email)
except Exception as exc:
    st.error("Failed to load user detail rows from Supabase.")
    st.exception(exc)
    st.stop()

st.divider()
st.subheader("User Summary")

left, mid, right = st.columns(3)
with left:
    st.metric("Email", email)
with mid:
    st.metric("Subscription", str(user.get("subscription_status") or "no app profile"))
with right:
    st.metric("Exam Attempts", len(attempts))

with st.expander("App user row", expanded=False):
    st.json(user)

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
