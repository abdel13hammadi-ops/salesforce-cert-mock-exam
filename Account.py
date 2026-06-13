import streamlit as st

import sys
from pathlib import Path

_file = Path(__file__).resolve()
_root = _file.parent.parent if _file.parent.name == "pages" else _file.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import path_setup

path_setup.ensure_project_root(__file__)

from utils.access_control import (
    render_app_chrome,
    has_premium_access,
    render_locked_premium_previews,
    render_upgrade_card,
    get_subscription_status,
    get_available_certifications,
    get_supabase_client,
    get_supabase_public_client,
    get_supabase_auth_client,
    save_logged_in_user,
    clear_logged_in_user,
    get_current_user_email,
    extract_auth_session,
    default_free_profile,
    FREE_STATUS,
    ADMIN_EXAM_NAME,
    BA_EXAM_NAME,
)
from utils.readiness import calculate_readiness, readiness_methodology_text
APP_VERSION = "ACCOUNT_V7_BOTH_CERT_READINESS"

st.set_page_config(page_title="Account", layout="wide")
render_app_chrome()


def get_auth_client():
    return get_supabase_auth_client()


def get_existing_profile(email: str):
    try:
        result = (
            get_supabase_client()
            .table("app_users")
            .select("*")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        data = result.data or []
        return data[0] if data else None
    except Exception:
        return None


@st.cache_data(ttl=300)
def load_languages():
    result = (
        get_supabase_public_client()
        .table("languages")
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

    result = get_supabase_client().table("app_users").upsert(payload, on_conflict="email").execute()
    return (result.data or [payload])[0]


def try_upsert_profile(email: str, full_name: str, language_code: str, auth_user_id: str | None = None):
    try:
        return upsert_profile(email, full_name, language_code, auth_user_id)
    except Exception:
        return default_free_profile(email)


def complete_login(
    email: str,
    auth_user_id: str | None,
    auth_session=None,
    full_name: str = "",
    language_code: str = "en",
    auto_reload_browser: bool = False,
):
    """Save auth session first, then best-effort profile sync without blocking login."""
    save_logged_in_user(
        email,
        auth_user_id,
        profile=None,
        session=auth_session,
        auto_reload_browser=auto_reload_browser,
    )

    profile = get_existing_profile(email)
    if not profile:
        profile = try_upsert_profile(email, full_name, language_code, auth_user_id)
    elif auth_user_id and not profile.get("auth_user_id"):
        updated = try_upsert_profile(
            email,
            profile.get("full_name") or full_name,
            profile.get("preferred_language_code") or language_code,
            auth_user_id,
        )
        profile = updated or profile

    save_logged_in_user(email, auth_user_id, profile=profile or default_free_profile(email))
    return profile or default_free_profile(email)


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


@st.cache_data(ttl=60)
def load_all_attempts_for_readiness(email):
    normalized_email = normalize_email(email)
    if not normalized_email:
        return []
    result = (
        get_supabase_client()
        .table("exam_attempts")
        .select(
            "id,user_email,mode,category,score,correct_answers,total_questions,"
            "domain_breakdown,completed_at,exam_name,language_code"
        )
        .ilike("user_email", normalized_email)
        .order("id", desc=True)
        .execute()
    )
    rows = result.data or []
    return [
        row
        for row in rows
        if normalize_email(row.get("user_email")) == normalized_email
    ]


def attempts_for_cert(all_attempts: list, exam_name: str) -> list:
    matched = [row for row in all_attempts if row.get("exam_name") == exam_name]
    if exam_name == ADMIN_EXAM_NAME:
        legacy = [row for row in all_attempts if not row.get("exam_name")]
        matched = matched + legacy
    return matched


def passing_score_for_cert(cert: dict) -> float:
    exam_name = cert.get("exam_name")
    if cert.get("passing_score") is not None:
        return float(cert["passing_score"])
    if exam_name == BA_EXAM_NAME:
        return 72.0
    if exam_name == ADMIN_EXAM_NAME:
        return 65.0
    return 65.0


def render_cert_readiness_snapshot(cert: dict, attempts: list):
    display_name = cert.get("display_name") or cert.get("exam_name") or "Certification"
    exam_name = cert.get("exam_name")
    st.markdown(f"**{display_name}**")

    if not attempts:
        st.caption("No mock exams or practice attempts saved yet for this certification.")
        return

    readiness = calculate_readiness(
        attempts=attempts,
        passing_score=passing_score_for_cert(cert),
        domain_weights=fetch_domain_weights(exam_name),
        expected_question_count=int(cert.get("question_count") or 60),
        question_bank_total=None,
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("Readiness", f"{readiness['score']}%")
    c2.metric("Status", readiness["label"])
    c3.metric("Attempts", len(attempts))
    st.progress(max(0, min(float(readiness["score"]) / 100, 1)))
    st.caption(readiness["recommendation"])


@st.cache_data(ttl=60)
def fetch_domain_weights(exam_name):
    result = (
        get_supabase_public_client().table("certification_domains")
        .select("domain_name, weight")
        .eq("exam_name", exam_name)
        .eq("is_active", True)
        .execute()
    )
    return {row.get("domain_name"): float(row.get("weight") or 0) for row in (result.data or []) if row.get("domain_name")}


def render_premium_offer_box():
    st.subheader("Premium Launch Plan")
    st.markdown(
        """
        <div style="border:1px solid #d8dde6;border-radius:10px;padding:18px;background:#f8fafc;">
            <h3 style="margin-top:0;">Complete Salesforce Prep Access</h3>
            <p><strong>Launch price:</strong> $29.99 for 3 months <span style="color:#64748b;">(regular price $49.99)</span></p>
            <ul>
                <li>Salesforce Administrator + Business Analyst included</li>
                <li>Full 60-question timed mock exams</li>
                <li>Full question bank</li>
                <li>Practice by Category</li>
                <li>Weak Areas Practice</li>
                <li>Visual Progress Dashboard</li>
                <li>Visual Readiness Score with domain colors</li>
            </ul>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.title("Account")
st.caption(f"App version: {APP_VERSION}")

languages = load_languages()
language_codes = [row["language_code"] for row in languages]
label_by_code = {row["language_code"]: language_label(row) for row in languages}

current_email = get_current_user_email() or ""
current_auth_user_id = str(st.session_state.get("auth_user_id", "")).strip()

if current_email:
    profile = get_existing_profile(current_email) or default_free_profile(current_email)
    save_logged_in_user(
        current_email,
        current_auth_user_id or profile.get("auth_user_id"),
        profile,
        write_browser_cookies=False,
    )

    st.success(f"Signed in as {current_email}")

    profile = get_existing_profile(current_email) or default_free_profile(current_email)
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
            save_logged_in_user(
                current_email,
                updated.get("auth_user_id"),
                updated,
                write_browser_cookies=False,
            )
            st.success("Profile saved ✅")
            st.rerun()

    with c2:
        if st.button("Log Out"):
            try:
                get_auth_client().auth.sign_out()
            except Exception:
                pass
            clear_logged_in_user(auto_reload_browser=True)
            st.success("Logged out. The page will refresh automatically.")
            st.stop()

    st.divider()
    st.write("Current access:")
    current_status = st.session_state.get('subscription_status', 'free')
    st.write(f"Subscription status: **{current_status}**")
    st.write(f"Preferred language: **{label_by_code.get(st.session_state.get('preferred_language_code', 'en'), 'English (en)')}**")

    st.divider()
    if has_premium_access(current_email):
        st.subheader("Overall Readiness Snapshot")
        st.caption("Readiness is an estimate based on saved mock exam history. It is not a guarantee of passing.")
        certs = get_available_certifications()
        if certs:
            all_attempts = load_all_attempts_for_readiness(current_email)
            cert_cols = st.columns(len(certs))
            for col, cert in zip(cert_cols, certs):
                exam_name = cert.get("exam_name")
                if not exam_name:
                    continue
                cert_attempts = attempts_for_cert(all_attempts, exam_name)
                with col:
                    render_cert_readiness_snapshot(cert, cert_attempts)
            st.caption("Open My Progress for full charts, domain breakdown, and attempt history.")
            st.page_link("pages/My_Progress.py", label="View full progress", icon="📈")
        else:
            st.info("No certifications are configured yet.")
    else:
        render_premium_offer_box()
        render_locked_premium_previews()

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
                        complete_login(
                            login_email,
                            user.id,
                            extract_auth_session(response),
                            auto_reload_browser=True,
                        )
                        st.success("Logged in ✅")
                        st.info("Finalizing login. The page will refresh automatically. If it does not, refresh once manually.")
                        st.stop()
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
                    complete_login(
                        signup_email,
                        auth_user_id,
                        extract_auth_session(response),
                        full_name=full_name.strip(),
                        language_code=selected_language,
                        auto_reload_browser=True,
                    )
                    st.success("Account created ✅")
                    st.info("Finalizing account session. The page will refresh automatically. If email confirmation is enabled in Supabase, check your inbox too.")
                    st.stop()
                except Exception as exc:
                    st.error("Account creation failed. The email may already be registered.")
                    st.caption(str(exc))

    st.divider()
    st.caption("Passwords are handled by Supabase Auth. They are not stored in the app_users profile table.")
    render_premium_offer_box()
