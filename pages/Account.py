import streamlit as st
from supabase import create_client

APP_VERSION = "ACCOUNT_V5_SUPABASE_AUTH_LOGIN"

st.set_page_config(page_title="Account", layout="wide")


def get_secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def get_auth_client():
    url = get_secret("SUPABASE_URL")
    anon_key = get_secret("SUPABASE_ANON_KEY")
    if not url or not anon_key:
        st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, anon_key)


def get_admin_client():
    url = get_secret("SUPABASE_URL")
    service_key = get_secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not service_key:
        st.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, service_key)


@st.cache_data(ttl=300)
def load_languages():
    admin = get_admin_client()
    result = (
        admin.table("languages")
        .select("language_code, language_name, native_name, is_active, display_order")
        .eq("is_active", True)
        .order("display_order")
        .execute()
    )
    rows = result.data or []
    if not rows:
        rows = [
            {"language_code": "en", "language_name": "English", "native_name": "English"}
        ]
    return rows


def language_label(row: dict) -> str:
    native = row.get("native_name") or row.get("language_name") or row.get("language_code")
    name = row.get("language_name") or native
    code = row.get("language_code")
    if native == name:
        return f"{name} ({code})"
    return f"{name} / {native} ({code})"


def get_existing_profile(email: str):
    admin = get_admin_client()
    result = (
        admin.table("app_users")
        .select("*")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    data = result.data or []
    return data[0] if data else None


def upsert_profile(email: str, full_name: str, language_code: str, auth_user_id: str | None = None):
    email = str(email).strip().lower()
    full_name = str(full_name).strip()
    language_code = str(language_code).strip().lower() or "en"

    existing = get_existing_profile(email)

    # Preserve paid/free status. Do not overwrite active subscription when user edits profile.
    subscription_status = "free"
    stripe_customer_id = None
    if existing:
        subscription_status = existing.get("subscription_status") or "free"
        stripe_customer_id = existing.get("stripe_customer_id")

    payload = {
        "email": email,
        "full_name": full_name,
        "preferred_language_code": language_code,
        "subscription_status": subscription_status,
    }

    if auth_user_id:
        payload["auth_user_id"] = auth_user_id
    elif existing and existing.get("auth_user_id"):
        payload["auth_user_id"] = existing.get("auth_user_id")

    if stripe_customer_id:
        payload["stripe_customer_id"] = stripe_customer_id

    admin = get_admin_client()
    result = admin.table("app_users").upsert(payload, on_conflict="email").execute()
    return (result.data or [payload])[0]


def save_logged_in_user_to_session(email: str, auth_user_id: str | None, profile: dict | None = None):
    email = str(email).strip().lower()
    st.session_state["user_email"] = email
    st.session_state["auth_user_id"] = auth_user_id or ""

    if profile:
        st.session_state["full_name"] = profile.get("full_name") or ""
        st.session_state["preferred_language_code"] = profile.get("preferred_language_code") or "en"
        st.session_state["subscription_status"] = profile.get("subscription_status") or "free"


def clear_login_session():
    for key in [
        "user_email",
        "auth_user_id",
        "full_name",
        "preferred_language_code",
        "subscription_status",
    ]:
        st.session_state.pop(key, None)


st.title("Account")
st.caption(f"App version: {APP_VERSION}")

languages = load_languages()
language_codes = [row["language_code"] for row in languages]
label_by_code = {row["language_code"]: language_label(row) for row in languages}

current_email = str(st.session_state.get("user_email", "")).strip().lower()
current_auth_user_id = str(st.session_state.get("auth_user_id", "")).strip()

if current_email:
    profile = get_existing_profile(current_email)
    if profile:
        save_logged_in_user_to_session(current_email, profile.get("auth_user_id") or current_auth_user_id, profile)

    st.success(f"Signed in as {current_email}")

    profile = get_existing_profile(current_email) or {}
    saved_language = profile.get("preferred_language_code") or st.session_state.get("preferred_language_code", "en")
    if saved_language not in language_codes:
        saved_language = "en" if "en" in language_codes else language_codes[0]

    st.subheader("Profile")
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
                auth_user_id=current_auth_user_id or profile.get("auth_user_id"),
            )
            save_logged_in_user_to_session(current_email, updated.get("auth_user_id"), updated)
            st.success("Profile saved ✅")
            st.rerun()

    with c2:
        if st.button("Log Out"):
            try:
                get_auth_client().auth.sign_out()
            except Exception:
                pass
            clear_login_session()
            st.success("Logged out.")
            st.rerun()

    st.divider()
    st.write("Current access:")
    st.write(f"Subscription status: **{st.session_state.get('subscription_status', 'free')}**")
    st.write(f"Preferred language: **{label_by_code.get(st.session_state.get('preferred_language_code', 'en'), 'English (en)')}**")

else:
    st.info("Create an account or log in to access the platform.")

    sign_in_tab, sign_up_tab = st.tabs(["Log In", "Create Account"])

    with sign_in_tab:
        st.subheader("Log In")
        login_email = st.text_input("Email", key="login_email").strip().lower()
        login_password = st.text_input("Password", type="password", key="login_password")

        if st.button("Log In", type="primary"):
            if not login_email or not login_password:
                st.warning("Enter your email and password.")
            else:
                auth = get_auth_client()
                try:
                    response = auth.auth.sign_in_with_password({
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
                        save_logged_in_user_to_session(login_email, user.id, profile)
                        st.success("Logged in ✅")
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
                auth = get_auth_client()
                try:
                    response = auth.auth.sign_up({
                        "email": signup_email,
                        "password": signup_password,
                        "options": {"data": {"full_name": full_name.strip()}},
                    })
                    user = response.user
                    auth_user_id = user.id if user else None
                    profile = upsert_profile(signup_email, full_name, selected_language, auth_user_id)
                    save_logged_in_user_to_session(signup_email, auth_user_id, profile)
                    st.success("Account created ✅")
                    st.info("If email confirmation is enabled in Supabase, check your inbox to confirm your account.")
                    st.rerun()
                except Exception as exc:
                    st.error("Account creation failed. The email may already be registered.")
                    st.caption(str(exc))

    st.divider()
    st.caption("Passwords are handled by Supabase Auth. They are not stored in the app_users profile table.")
