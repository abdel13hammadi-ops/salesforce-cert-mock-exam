import streamlit as st

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

APP_VERSION = "ACCOUNT_STABLE_AUTH_V1"

st.set_page_config(page_title="Account", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()


def get_existing_profile(email: str):
    email = str(email or "").strip().lower()
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


def upsert_profile(email: str, full_name: str = "", language_code: str = "en", auth_user_id: str | None = None):
    email = str(email or "").strip().lower()
    if not email:
        return {"email": "", "subscription_status": "free", "preferred_language_code": "en"}

    existing = get_existing_profile(email) or {}
    payload = {
        "email": email,
        "full_name": str(full_name or existing.get("full_name") or "").strip(),
        "preferred_language_code": str(language_code or existing.get("preferred_language_code") or "en").strip().lower() or "en",
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


def language_label(row: dict) -> str:
    native = row.get("native_name") or row.get("language_name") or row.get("language_code")
    name = row.get("language_name") or native
    code = row.get("language_code")
    return f"{name} ({code})" if native == name else f"{name} / {native} ({code})"


def get_app_base_url() -> str:
    """Return deployed app base URL for auth redirects.

    Set APP_BASE_URL in Streamlit Secrets for reliable password reset redirects.
    Example: https://your-app.streamlit.app
    """
    try:
        base = str(st.secrets.get("APP_BASE_URL", "") or "").strip()
    except Exception:
        base = ""
    return base.rstrip("/")


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

current_email = get_current_user_email()

if current_email:
    profile = get_existing_profile(current_email) or {}
    if profile:
        merged = dict(profile)
        merged["email"] = current_email
        save_logged_in_user(merged, persist=True)

    st.success(f"Signed in as {current_email}")
    status = get_subscription_status(current_email)
    st.write(f"Subscription status: **{status}**")

    st.subheader("Profile")
    saved_language = profile.get("preferred_language_code") or st.session_state.get("preferred_language_code", "en")
    if saved_language not in language_codes:
        saved_language = "en" if "en" in language_codes else language_codes[0]

    full_name = st.text_input("Full name", value=profile.get("full_name") or st.session_state.get("full_name", ""))
    selected_language = st.selectbox(
        "Preferred language",
        language_codes,
        index=language_codes.index(saved_language),
        format_func=lambda code: label_by_code.get(code, code),
    )

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Save Profile", type="primary"):
            updated = upsert_profile(
                email=current_email,
                full_name=full_name,
                language_code=selected_language,
                auth_user_id=st.session_state.get("auth_user_id") or profile.get("auth_user_id"),
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
                        if not profile:
                            profile = upsert_profile(login_email, "", "en", user.id)
                        elif not profile.get("auth_user_id"):
                            profile = upsert_profile(
                                login_email,
                                profile.get("full_name") or "",
                                profile.get("preferred_language_code") or "en",
                                user.id,
                            )
                        profile = profile or {"email": login_email, "auth_user_id": user.id, "subscription_status": "free", "preferred_language_code": "en"}
                        profile["email"] = login_email
                        profile["auth_user_id"] = profile.get("auth_user_id") or user.id
                        save_logged_in_user(profile, persist=True)
                        st.success("Logged in ✅")
                        st.info("If the page does not refresh automatically, click Account again in the sidebar.")
                        st.rerun()
                except Exception as exc:
                    st.error("Login failed. Please check your credentials or reset your password.")
                    st.caption(str(exc))

    with sign_up_tab:
        st.subheader("Create Account")
        full_name = st.text_input("Full name", key="signup_full_name")
        signup_email = st.text_input("Email", key="signup_email").strip().lower()
        signup_password = st.text_input("Password", type="password", key="signup_password")
        confirm_password = st.text_input("Confirm password", type="password", key="confirm_password")
        selected_language = st.selectbox(
            "Preferred language",
            language_codes,
            index=language_codes.index("en") if "en" in language_codes else 0,
            format_func=lambda code: label_by_code.get(code, code),
            key="signup_language",
        )

        if st.button("Create Account", type="primary"):
            if not full_name.strip():
                st.warning("Enter your full name.")
            elif not signup_email or "@" not in signup_email:
                st.warning("Enter a valid email address.")
            elif len(signup_password) < 8:
                st.warning("Password must be at least 8 characters.")
            elif signup_password != confirm_password:
                st.warning("Passwords do not match.")
            else:
                try:
                    response = get_supabase_auth_client().auth.sign_up({
                        "email": signup_email,
                        "password": signup_password,
                        "options": {"data": {"full_name": full_name.strip()}},
                    })
                    user = response.user
                    auth_user_id = user.id if user else None
                    profile = upsert_profile(signup_email, full_name, selected_language, auth_user_id)
                    save_logged_in_user(profile, persist=True)
                    st.success("Account created ✅")
                    st.info("If email confirmation is enabled in Supabase, check your inbox to confirm your account.")
                    st.rerun()
                except Exception as exc:
                    st.error("Account creation failed. The email may already be registered.")
                    st.caption(str(exc))


    with reset_tab:
        st.subheader("Reset Password")
        st.caption("Enter your account email. If the email exists, Supabase will send a secure password reset link.")
        reset_email = st.text_input("Email", key="reset_email").strip().lower()
        if st.button("Send Password Reset Email", type="primary"):
            if not reset_email or "@" not in reset_email:
                st.warning("Enter a valid email address.")
            else:
                try:
                    send_password_reset_email(reset_email)
                    st.success("If this email exists, a password reset link has been sent.")
                    if not get_app_base_url():
                        st.info("Admin note: set APP_BASE_URL in Streamlit Secrets so reset links return to the Reset Password page.")
                except Exception as exc:
                    st.error("Could not send password reset email.")
                    st.caption(str(exc))


st.divider()
st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
