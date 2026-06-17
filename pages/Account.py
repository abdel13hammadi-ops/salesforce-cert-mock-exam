from datetime import datetime, timezone
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import streamlit as st
import streamlit.components.v1 as components

from utils.access_control import (
    clear_login_state,
    get_current_user_email,
    get_supabase_admin_client,
    get_supabase_auth_client,
    get_subscription_status,
    is_admin_user,
    is_admin_unlocked,
    render_app_chrome,
    save_logged_in_user,
    unlock_admin,
)

APP_VERSION = "ACCOUNT_IDENTITY_GUARD_V1"

st.set_page_config(page_title="Account", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()


DUPLICATE_ACCOUNT_MESSAGE = "An account already exists for this email. Please log in or use a different email."


def normalize_email(email: str | None) -> str:
    return str(email or "").strip().lower()


def get_secret_value(name: str, default: str = "") -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, default) or "").strip()
    except Exception:
        return default


def get_existing_profile(email: str):
    email = normalize_email(email)
    if not email:
        return None
    try:
        result = (
            get_supabase_admin_client()
            .table("app_users")
            .select("*")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        return (result.data or [None])[0]
    except Exception:
        return None


def get_existing_profile_strict(email: str) -> dict | None:
    """Find an app_users profile by normalized email and fail closed on DB errors."""
    email = normalize_email(email)
    if not email:
        return None

    admin = get_supabase_admin_client()

    result = (
        admin
        .table("app_users")
        .select("id,email,auth_user_id")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]

    # Defensive fallback for legacy rows that may not have been stored normalized.
    result = admin.table("app_users").select("id,email,auth_user_id").execute()
    for row in result.data or []:
        if normalize_email(row.get("email")) == email:
            return row
    return None


def find_auth_user_by_email(email: str) -> dict | None:
    """Find a Supabase Auth user by normalized email using the service-role Auth API.

    Signup must fail closed if this lookup cannot run. Supabase Auth can hide
    duplicate signup attempts for security, so relying only on auth.sign_up is
    not enough.
    """
    email = normalize_email(email)
    if not email:
        return None

    base_url = get_secret_value("SUPABASE_URL").rstrip("/")
    service_role_key = get_secret_value("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing Supabase service-role configuration for duplicate email check.")

    per_page = 1000
    for page in range(1, 11):
        query = urlencode({"page": page, "per_page": per_page})
        request = Request(
            f"{base_url}/auth/v1/admin/users?{query}",
            headers={
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Content-Type": "application/json",
            },
            method="GET",
        )
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")

        users = payload.get("users") if isinstance(payload, dict) else payload
        users = users or []
        for user in users:
            if normalize_email(user.get("email")) == email:
                return user
        if len(users) < per_page:
            break
    return None


def email_already_has_account(email: str) -> bool:
    email = normalize_email(email)
    if not email:
        return False
    if get_existing_profile_strict(email):
        return True
    if find_auth_user_by_email(email):
        return True
    return False


def repair_profile_auth_user_id(email: str, auth_user_id: str, preferred_timezone: str | None = None) -> dict:
    """Repair app_users.auth_user_id after a successful login with the same email."""
    email = normalize_email(email)
    auth_user_id = str(auth_user_id or "").strip()
    if not email or not auth_user_id:
        raise ValueError("Cannot repair profile identity link without email and auth user ID.")

    payload = {"auth_user_id": auth_user_id}
    if preferred_timezone:
        payload["preferred_timezone"] = normalize_timezone(preferred_timezone)

    result = (
        get_supabase_admin_client()
        .table("app_users")
        .update(payload)
        .eq("email", email)
        .execute()
    )
    updated = (result.data or [None])[0]
    if not updated:
        existing = get_existing_profile(email) or {}
        updated = {**existing, **payload, "email": email}
    return updated


def upsert_profile(
    email: str,
    full_name: str = "",
    language_code: str = "en",
    auth_user_id: str | None = None,
    preferred_timezone: str | None = None,
):
    email = normalize_email(email)
    if not email:
        return {
            "email": "",
            "subscription_status": "free",
            "preferred_language_code": "en",
            "preferred_timezone": "UTC",
        }

    existing = get_existing_profile(email) or {}
    payload = {
        "email": email,
        "full_name": str(full_name or existing.get("full_name") or "").strip(),
        "preferred_language_code": str(language_code or existing.get("preferred_language_code") or "en").strip().lower() or "en",
        "preferred_timezone": normalize_timezone(preferred_timezone or existing.get("preferred_timezone") or "UTC"),
        "subscription_status": str(existing.get("subscription_status") or "free").strip().lower(),
    }
    if auth_user_id or existing.get("auth_user_id"):
        payload["auth_user_id"] = auth_user_id or existing.get("auth_user_id")
    if existing.get("stripe_customer_id"):
        payload["stripe_customer_id"] = existing.get("stripe_customer_id")

    try:
        result = get_supabase_admin_client().table("app_users").upsert(payload, on_conflict="email").execute()
        return (result.data or [payload])[0]
    except Exception:
        # Login must not fail just because profile sync failed.
        return payload


def update_profile_timezone(email: str, preferred_timezone: str) -> dict:
    email = normalize_email(email)
    preferred_timezone = normalize_timezone(preferred_timezone)
    if not email:
        return {}
    try:
        result = (
            get_supabase_admin_client()
            .table("app_users")
            .update({"preferred_timezone": preferred_timezone})
            .eq("email", email)
            .execute()
        )
        return (result.data or [{}])[0]
    except Exception:
        return {}


def load_languages():
    try:
        result = (
            get_supabase_admin_client()
            .table("languages")
            .select("language_code, language_name, native_name, is_active, display_order")
            .eq("is_active", True)
            .order("display_order")
            .execute()
        )
        rows = result.data or []
        return rows or [{"language_code": "en", "language_name": "English", "native_name": "English"}]
    except Exception:
        return [{"language_code": "en", "language_name": "English", "native_name": "English"}]



def get_query_param(name: str) -> str:
    try:
        value = st.query_params.get(name, "")
        if isinstance(value, list):
            return str(value[0] if value else "")
        return str(value or "")
    except Exception:
        return ""


def render_browser_locale_probe() -> None:
    """Capture browser timezone/language by writing safe query params once.

    Python cannot read navigator.language or Intl timezone directly. This tiny
    client-side probe preserves existing query params, including fr_session.
    """
    components.html(
        """
        <script>
        (function () {
            function getLoc() {
                try {
                    if (window.parent && window.parent.location) return window.parent.location;
                } catch (e) {}
                return window.location;
            }

            const loc = getLoc();
            const url = new URL(loc.href);
            let changed = false;

            if (!url.searchParams.get("cb_tz")) {
                try {
                    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
                    if (tz) {
                        url.searchParams.set("cb_tz", tz);
                        changed = true;
                    }
                } catch (e) {}
            }

            if (!url.searchParams.get("cb_lang")) {
                try {
                    const lang = navigator.language || (navigator.languages && navigator.languages[0]) || "en";
                    if (lang) {
                        url.searchParams.set("cb_lang", lang);
                        changed = true;
                    }
                } catch (e) {}
            }

            if (changed) {
                loc.replace(url.toString());
            }
        })();
        </script>
        """,
        height=0,
    )


def normalize_timezone(value: str | None) -> str:
    value = str(value or "").strip()
    if not value:
        return "UTC"
    try:
        ZoneInfo(value)
        return value
    except ZoneInfoNotFoundError:
        return "UTC"
    except Exception:
        return "UTC"


def detected_browser_timezone() -> str:
    return normalize_timezone(get_query_param("cb_tz") or "UTC")


def normalize_browser_language(value: str | None) -> str:
    value = str(value or "").strip().lower()
    if not value:
        return "en"
    return value.replace("_", "-").split("-", 1)[0] or "en"


def load_question_language_codes() -> set[str]:
    try:
        result = (
            get_supabase_admin_client()
            .table("questions")
            .select("language_code")
            .eq("is_active", True)
            .execute()
        )
        return {str(row.get("language_code") or "").strip().lower() for row in (result.data or []) if row.get("language_code")}
    except Exception:
        return {"en"}


def default_language_from_browser(language_codes: list[str], question_language_codes: set[str]) -> str:
    detected = normalize_browser_language(get_query_param("cb_lang") or "en")
    active = {str(code or "").strip().lower() for code in language_codes}
    available = {str(code or "").strip().lower() for code in question_language_codes}
    if detected in active and detected in available:
        return detected
    if "en" in active and "en" in available:
        return "en"
    if "en" in active:
        return "en"
    return language_codes[0] if language_codes else "en"

def language_label(row: dict) -> str:
    native = row.get("native_name") or row.get("language_name") or row.get("language_code")
    name = row.get("language_name") or native
    code = row.get("language_code")
    return f"{name} ({code})" if native == name else f"{name} / {native} ({code})"


def get_app_base_url() -> str:
    """Return deployed app base URL for auth redirects."""
    return get_secret_value("APP_BASE_URL").rstrip("/")


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
            "Supabase is blocking password reset emails right now because the Auth email send limit was reached. "
            "This limit is project-wide, not just this user. Wait, or configure Custom SMTP in Supabase for production."
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


render_browser_locale_probe()


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

st.title("Account")
st.caption(f"App version: {APP_VERSION}")

languages = load_languages()
language_codes = [row["language_code"] for row in languages] or ["en"]
label_by_code = {row["language_code"]: language_label(row) for row in languages}
question_language_codes = load_question_language_codes()
detected_timezone = detected_browser_timezone()
detected_default_language = default_language_from_browser(language_codes, question_language_codes)

current_email = get_current_user_email()

if current_email:
    profile = get_existing_profile(current_email) or {}
    if profile and detected_timezone and profile.get("preferred_timezone") != detected_timezone:
        timezone_update = update_profile_timezone(current_email, detected_timezone)
        profile = {**profile, **timezone_update, "preferred_timezone": detected_timezone}
    if profile:
        merged = dict(profile)
        merged["email"] = current_email
        save_logged_in_user(merged, persist=True)

    st.success(f"Signed in as {current_email}")
    st.caption("Login persistence enabled: refresh should keep you signed in on this browser.")
    status = get_subscription_status(current_email)
    st.write(f"Subscription status: **{status}**")

    st.subheader("Profile")
    saved_language = profile.get("preferred_language_code") or st.session_state.get("preferred_language_code", detected_default_language)
    if saved_language not in language_codes:
        saved_language = "en" if "en" in language_codes else language_codes[0]

    full_name = st.text_input("Full name", value=profile.get("full_name") or st.session_state.get("full_name", ""))
    selected_language = st.selectbox(
        "Preferred language",
        language_codes,
        index=language_codes.index(saved_language),
        format_func=lambda code: label_by_code.get(code, code),
    )
    st.caption(f"Timezone: {detected_timezone} — detected automatically from this browser.")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Save Profile", type="primary"):
            updated = upsert_profile(
                email=current_email,
                full_name=full_name,
                language_code=selected_language,
                auth_user_id=st.session_state.get("auth_user_id") or profile.get("auth_user_id"),
                preferred_timezone=detected_timezone,
            )
            save_logged_in_user(updated, persist=True)
            st.success("Profile saved ✅")
            st.rerun()
    with c2:
        if st.button("Log Out"):
            clear_login_state()
            st.success("Logged out.")
            st.rerun()

    if is_admin_user(current_email):
        st.divider()
        st.subheader("Admin Unlock")
        if is_admin_unlocked():
            st.success("Admin access unlocked for this session.")
        else:
            admin_password = st.text_input("Admin password", type="password")
            if st.button("Unlock Admin"):
                if unlock_admin(admin_password):
                    st.success("Admin unlocked ✅")
                    st.rerun()
                else:
                    st.error("Invalid admin password or email is not allowed.")

else:
    st.info("Create an account or log in to access the platform.")
    sign_in_tab, sign_up_tab, reset_tab = st.tabs(["Log In", "Create Account", "Forgot Password"])

    with sign_in_tab:
        st.subheader("Log In")
        login_email = st.text_input("Email", key="login_email").strip().lower()
        login_password = st.text_input("Password", type="password", key="login_password")

        if st.button("Log In", type="primary"):
            if not login_email or not login_password:
                st.warning("Enter your email and password.")
            else:
                try:
                    response = get_supabase_auth_client().auth.sign_in_with_password({
                        "email": login_email,
                        "password": login_password,
                    })
                    user = response.user
                    if not user:
                        st.error("Login failed. Please check your email and password.")
                    else:
                        profile = get_existing_profile(login_email)
                        current_auth_user_id = str(getattr(user, "id", "") or "").strip()
                        if not profile:
                            profile = upsert_profile(login_email, "", detected_default_language, current_auth_user_id, detected_timezone)
                        else:
                            stored_auth_user_id = str(profile.get("auth_user_id") or "").strip()
                            if stored_auth_user_id != current_auth_user_id:
                                # A successful Supabase login proves this auth user owns this email.
                                # Repair missing or stale app_users.auth_user_id instead of preserving corruption.
                                repaired = repair_profile_auth_user_id(login_email, current_auth_user_id, detected_timezone)
                                profile = {**profile, **repaired, "auth_user_id": current_auth_user_id}
                        profile = profile or {"email": login_email, "auth_user_id": current_auth_user_id, "subscription_status": "free", "preferred_language_code": detected_default_language, "preferred_timezone": detected_timezone}
                        if detected_timezone and profile.get("preferred_timezone") != detected_timezone:
                            timezone_update = update_profile_timezone(login_email, detected_timezone)
                            profile = {**profile, **timezone_update, "preferred_timezone": detected_timezone}
                        profile["email"] = login_email
                        profile["auth_user_id"] = current_auth_user_id
                        save_logged_in_user(profile, persist=True)
                        st.success("Logged in ✅")
                        st.info("If the page does not refresh automatically, click Account again in the sidebar.")
                        st.rerun()
                except Exception as exc:
                    st.error("Login failed. Please check your credentials or reset your password.")
                    st.code(f"{type(exc).__name__}: {repr(exc)}")

    with sign_up_tab:
        st.subheader("Create Account")
        name_col1, name_col2 = st.columns(2)
        with name_col1:
            first_name = st.text_input("First name", key="signup_first_name").strip()
        with name_col2:
            last_name = st.text_input("Last name", key="signup_last_name").strip()

        full_name = " ".join(part for part in [first_name, last_name] if part).strip()

        signup_email = st.text_input("Email", key="signup_email").strip().lower()
        signup_password = st.text_input("Password", type="password", key="signup_password")
        confirm_password = st.text_input("Confirm password", type="password", key="confirm_password")
        selected_language = st.selectbox(
            "Preferred language",
            language_codes,
            index=language_codes.index(detected_default_language) if detected_default_language in language_codes else 0,
            format_func=lambda code: label_by_code.get(code, code),
            key="signup_language",
        )

        if st.button("Create Account", type="primary"):
            if not first_name:
                st.warning("Enter your first name.")
            elif not last_name:
                st.warning("Enter your last name.")
            elif not signup_email or "@" not in signup_email:
                st.warning("Enter a valid email address.")
            elif len(signup_password) < 8:
                st.warning("Password must be at least 8 characters.")
            elif signup_password != confirm_password:
                st.warning("Passwords do not match.")
            else:
                try:
                    if email_already_has_account(signup_email):
                        st.error(DUPLICATE_ACCOUNT_MESSAGE)
                    else:
                        response = get_supabase_auth_client().auth.sign_up({
                            "email": signup_email,
                            "password": signup_password,
                            "options": {"data": {"full_name": full_name}},
                        })
                        user = response.user
                        auth_user_id = str(getattr(user, "id", "") or "").strip() if user else ""
                        if not auth_user_id:
                            st.error("Account creation failed. Supabase did not return a user ID. Please try logging in or resetting your password.")
                        else:
                            profile = upsert_profile(signup_email, full_name, selected_language, auth_user_id, detected_timezone)
                            save_logged_in_user(profile, persist=True)
                            st.success("Account created ✅")
                            st.info("If email confirmation is enabled in Supabase, check your inbox to confirm your account.")
                            st.rerun()
                except Exception as exc:
                    msg = str(exc).lower()
                    if "already" in msg or "duplicate" in msg or "unique" in msg or "registered" in msg:
                        st.error(DUPLICATE_ACCOUNT_MESSAGE)
                    else:
                        st.error("Account creation is temporarily unavailable because the existing-email check failed.")
                        st.caption(str(exc))


    with reset_tab:
        st.subheader("Reset Password")
        st.caption("Enter your account email. If the email exists, Supabase will send a secure password reset link.")
        reset_email = st.text_input("Email", key="reset_email").strip().lower()
        on_cooldown, remaining = reset_on_cooldown(reset_email)
        if on_cooldown:
            st.info(f"A reset email was recently requested. Wait about {remaining} seconds before trying again.")

        if st.button("Send Password Reset Email", type="primary", disabled=on_cooldown):
            if not reset_email or "@" not in reset_email:
                st.warning("Enter a valid email address.")
            else:
                try:
                    send_password_reset_email(reset_email)
                    mark_password_reset_sent(reset_email)
                    st.success("If this email exists, a password reset link has been sent.")
                    if not get_app_base_url():
                        st.info("Admin note: set APP_BASE_URL in Render Environment so reset links return to the Reset Password page.")
                except Exception as exc:
                    st.error(format_password_reset_error(exc))
                    if not is_auth_email_rate_limit_error(exc):
                        st.caption(str(exc))


st.divider()
st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
