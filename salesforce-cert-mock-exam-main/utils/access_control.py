from pathlib import Path
import sys
import streamlit as st
from supabase import create_client

# Keep the project root importable when code runs from Streamlit's pages/ directory.
def ensure_project_root_on_path():
    current = Path(__file__).resolve()
    project_root = current.parents[1]
    root = str(project_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    return project_root

ensure_project_root_on_path()

PAID_STATUS = "active"
FREE_STATUS = "free"
PAID_STATUS_VALUES = {"active", "paid", "premium", "subscribed", "trialing"}
ADMIN_SESSION_KEY = "admin_unlocked"


def get_secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def get_admin_password():
    return get_secret("ADMIN_PASSWORD", "")


def get_admin_emails():
    raw = get_secret("ADMIN_EMAILS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def get_supabase_client():
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit Secrets.")
        st.stop()
    return create_client(url, key)


def get_current_user_email():
    email = st.session_state.get("user_email", "") or st.session_state.get("account_email", "")
    email = str(email).strip().lower()
    if email and "@" in email and "." in email.split("@")[-1]:
        return email
    return None


def get_current_user():
    email = get_current_user_email()
    if not email:
        return None
    return {
        "email": email,
        "auth_user_id": st.session_state.get("auth_user_id"),
        "full_name": st.session_state.get("full_name", ""),
        "preferred_language_code": st.session_state.get("preferred_language_code", "en"),
    }


def get_user_profile(email=None):
    email = (email or get_current_user_email() or "").strip().lower()
    if not email:
        return None
    try:
        result = (
            get_supabase_client()
            .table("app_users")
            .select("*")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def get_subscription_status(email=None):
    profile = get_user_profile(email=email)
    if not profile:
        return FREE_STATUS
    return str(profile.get("subscription_status") or FREE_STATUS).strip().lower()


def get_user_subscription_status(email=None):
    return get_subscription_status(email=email)


def is_paid_user(email=None):
    return get_subscription_status(email=email) in PAID_STATUS_VALUES


def get_preferred_language_code(email=None):
    profile = get_user_profile(email=email)
    if profile:
        return str(profile.get("preferred_language_code") or "en").strip().lower()
    return str(st.session_state.get("preferred_language_code", "en") or "en").strip().lower()


def require_login():
    email = get_current_user_email()
    if not email:
        render_sidebar_navigation()
        st.warning("Please go to the Account page and log in first.")
        st.stop()
    return email


def require_paid_access(feature_name="This feature"):
    email = require_login()
    status = get_subscription_status(email=email)
    if status not in PAID_STATUS_VALUES:
        st.error(f"{feature_name} is available for paid users only.")
        st.info("Please upgrade your account to unlock this feature.")
        st.stop()
    return True


def is_admin_email(email=None):
    email = (email or get_current_user_email() or "").strip().lower()
    admins = get_admin_emails()
    return bool(email and admins and email in admins)


def is_admin_unlocked():
    return bool(st.session_state.get(ADMIN_SESSION_KEY, False)) and is_admin_email()


def lock_admin():
    st.session_state[ADMIN_SESSION_KEY] = False


def unlock_admin(password):
    email = get_current_user_email()
    if not email:
        return False, "Please log in on the Account page first."
    if not is_admin_email(email):
        return False, "This account is not listed as an admin."
    expected = get_admin_password()
    if not expected:
        return False, "ADMIN_PASSWORD is missing in Streamlit Secrets."
    if str(password or "") != expected:
        return False, "Incorrect admin password."
    st.session_state[ADMIN_SESSION_KEY] = True
    return True, None


def hide_default_streamlit_pages():
    st.markdown(
        """
        <style>
        [data-testid="stSidebarNav"] {display: none !important;}
        section[data-testid="stSidebar"] nav {display: none !important;}
        div[data-testid="stSidebarNav"] {display: none !important;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def safe_page_link(page, label, icon=None):
    try:
        st.sidebar.page_link(page, label=label, icon=icon)
    except Exception:
        # If Streamlit page_link is unavailable, still show the label.
        # Admin pages remain protected by require_admin().
        st.sidebar.write(f"{icon or ''} {label}")


def render_sidebar_navigation(current_page=None):
    """Custom sidebar. Admin pages show only after Admin is unlocked."""
    hide_default_streamlit_pages()
    st.sidebar.markdown("### Salesforce Prep")

    email = get_current_user_email()
    if email:
        st.sidebar.caption(f"Signed in: {email}")
    else:
        st.sidebar.caption("Not signed in")

    st.sidebar.markdown("#### User Pages")
    safe_page_link("app.py", "Mock Exam", "📝")
    safe_page_link("pages/Practice_By_Category.py", "Practice by Category", "📚")
    safe_page_link("pages/Weak_Areas_Practice.py", "Weak Areas Practice", "🎯")
    safe_page_link("pages/My_Progress.py", "My Progress", "📈")
    safe_page_link("pages/Support.py", "Support", "💬")
    safe_page_link("pages/Account.py", "Account", "👤")

    st.sidebar.divider()
    st.sidebar.markdown("#### Admin")
    safe_page_link("pages/Admin.py", "Admin", "🔐")

    if is_admin_unlocked():
        st.sidebar.success("Admin unlocked")
        safe_page_link("pages/Admin_Import.py", "Admin Import", "⬆️")
        safe_page_link("pages/Admin_Question_Review.py", "Admin Question Review", "✅")
        safe_page_link("pages/Admin_Support_Tickets.py", "Admin Support Tickets", "🎫")
        if st.sidebar.button("Lock Admin", key="lock_admin_sidebar"):
            lock_admin()
            st.rerun()
    else:
        st.sidebar.caption("Admin pages are hidden until admin is unlocked.")


def require_admin():
    if is_admin_unlocked():
        render_sidebar_navigation()
        return True
    render_sidebar_navigation()
    st.error("Admin access required.")
    st.info("Click Admin in the sidebar and unlock admin mode with the admin password.")
    st.stop()

# Alias for any older files that might call this name.
def require_admin_access():
    return require_admin()


def render_admin_login_page():
    render_sidebar_navigation("Admin")
    st.title("Admin")
    st.caption("Unlock admin pages for this browser session.")

    email = get_current_user_email()
    if not email:
        st.warning("Log in on the Account page first, then return here.")
        safe_page_link("pages/Account.py", "Go to Account", "👤")
        st.stop()

    if not is_admin_email(email):
        st.error("This account is not authorized as an admin.")
        st.info("Add this email to ADMIN_EMAILS in Streamlit Secrets if it should be an admin.")
        st.stop()

    if is_admin_unlocked():
        st.success("Admin mode is already unlocked.")
        st.write("Admin pages are now visible in the sidebar.")
        if st.button("Lock Admin"):
            lock_admin()
            st.rerun()
        return

    password = st.text_input("Admin password", type="password")
    if st.button("Unlock Admin", type="primary"):
        ok, error = unlock_admin(password)
        if ok:
            st.success("Admin unlocked.")
            st.rerun()
        else:
            st.error(error)
