from pathlib import Path
import sys
import streamlit as st
from supabase import create_client

try:
    from streamlit_cookies_manager import EncryptedCookieManager
except Exception:
    EncryptedCookieManager = None


def ensure_project_root_on_path():
    """Keep the project root importable from Streamlit pages."""
    current = Path(__file__).resolve()
    project_root = current.parents[1]
    root = str(project_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    return project_root


ensure_project_root_on_path()

FREE_STATUS = "free"
PAID_STATUS_VALUES = {"active", "paid", "premium", "subscribed"}  # trialing intentionally excluded
ADMIN_SESSION_KEY = "admin_unlocked"
LAUNCH_PRICE_TEXT = "$29.99 for 3 months"
REGULAR_PRICE_TEXT = "$49.99 regular price"


def get_secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def get_cookie_password():
    """Stable password used to encrypt the browser remember-me cookie.
    Add COOKIE_PASSWORD in Streamlit Secrets. If missing, login will still work
    for the current session, but it will not survive a hard browser refresh.
    """
    return get_secret("COOKIE_PASSWORD", "")


def get_cookie_manager():
    """Return an encrypted cookie manager, or None if not configured.

    This is intentionally optional so the app does not crash if the dependency
    or COOKIE_PASSWORD is missing. Persistent login simply disables itself.
    """
    if EncryptedCookieManager is None:
        return None
    password = get_cookie_password()
    if not password:
        return None
    try:
        cookies = EncryptedCookieManager(prefix="forceready_", password=password)
        if not cookies.ready():
            st.stop()
        return cookies
    except Exception:
        return None


def save_login_cookie(email, auth_user_id=""):
    cookies = get_cookie_manager()
    if cookies is None:
        return False
    email = str(email or "").strip().lower()
    if not email:
        return False
    cookies["user_email"] = email
    cookies["auth_user_id"] = str(auth_user_id or "")
    cookies.save()
    return True


def clear_login_cookie():
    cookies = get_cookie_manager()
    if cookies is None:
        return False
    for key in ["user_email", "auth_user_id"]:
        try:
            del cookies[key]
        except Exception:
            pass
    cookies.save()
    return True


def load_login_cookie():
    cookies = get_cookie_manager()
    if cookies is None:
        return None, None
    try:
        email = str(cookies.get("user_email", "") or "").strip().lower()
        auth_user_id = str(cookies.get("auth_user_id", "") or "").strip()
    except Exception:
        return None, None
    if email and "@" in email and "." in email.split("@")[-1]:
        return email, auth_user_id
    return None, None


def restore_login_from_cookie():
    """Restore user_email/auth_user_id to session_state after browser refresh."""
    if st.session_state.get("user_email"):
        return st.session_state.get("user_email")
    email, auth_user_id = load_login_cookie()
    if not email:
        return None
    st.session_state["user_email"] = email
    st.session_state["account_email"] = email
    st.session_state["auth_user_id"] = auth_user_id or ""

    profile = get_user_profile(email)
    if profile:
        st.session_state["full_name"] = profile.get("full_name") or ""
        st.session_state["preferred_language_code"] = profile.get("preferred_language_code") or "en"
        st.session_state["subscription_status"] = profile.get("subscription_status") or "free"
    return email


def get_admin_password():
    return get_secret("ADMIN_PASSWORD", "")


def get_admin_emails():
    raw = get_secret("ADMIN_EMAILS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


@st.cache_resource(show_spinner=False)
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

    # Browser refresh clears Streamlit session_state. Restore from encrypted cookie
    # when COOKIE_PASSWORD is configured.
    restored = restore_login_from_cookie()
    if restored:
        return restored
    return None


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


def is_paid_user(email=None):
    return get_subscription_status(email=email) in PAID_STATUS_VALUES


def is_admin_email(email=None):
    email = (email or get_current_user_email() or "").strip().lower()
    admins = get_admin_emails()
    return bool(email and admins and email in admins)


def is_admin_unlocked():
    return bool(st.session_state.get(ADMIN_SESSION_KEY, False)) and is_admin_email()


def has_premium_access(email=None):
    return is_paid_user(email=email) or is_admin_unlocked()


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
        st.sidebar.write(f"{icon or ''} {label}")


def render_sidebar_navigation(current_page=None):
    """Custom sidebar. Admin pages show only after Admin is unlocked."""
    hide_default_streamlit_pages()
    st.sidebar.markdown("### Salesforce Prep")

    email = get_current_user_email()
    if email:
        status = get_subscription_status(email)
        st.sidebar.caption(f"Signed in: {email}")
        if has_premium_access(email):
            st.sidebar.success("Premium access")
        else:
            st.sidebar.info("Free Preview")
    else:
        st.sidebar.caption("Not signed in")

    st.sidebar.markdown("#### Main")
    safe_page_link("app.py", "Free Preview / Mock Exam", "📝")
    safe_page_link("pages/Account.py", "Account", "👤")
    safe_page_link("pages/Support.py", "Support", "💬")

    st.sidebar.markdown("#### Premium")
    safe_page_link("pages/Practice_By_Category.py", "Practice by Category", "📚")
    safe_page_link("pages/Weak_Areas_Practice.py", "Weak Areas Practice", "🎯")
    safe_page_link("pages/My_Progress.py", "My Progress & Readiness", "📈")

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


def require_login():
    email = get_current_user_email()
    if not email:
        render_sidebar_navigation()
        st.warning("Please log in from the Account page first.")
        st.stop()
    return email


def render_upgrade_card(feature_name="this premium feature"):
    st.warning(f"{feature_name} is available with Premium Access.")
    st.markdown(
        f"""
        <div style="border:1px solid #d8dde6;border-radius:10px;padding:18px;background:#f8fafc;margin-top:8px;">
            <h3 style="margin-top:0;">Unlock Complete Salesforce Prep Access</h3>
            <p><strong>Launch Offer:</strong> {LAUNCH_PRICE_TEXT} <span style="color:#64748b;">({REGULAR_PRICE_TEXT})</span></p>
            <ul>
                <li>Salesforce Administrator + Business Analyst included</li>
                <li>Full 60-question timed mock exams</li>
                <li>Full question bank</li>
                <li>Practice by Category</li>
                <li>Weak Areas Practice</li>
                <li>Visual Progress Dashboard</li>
                <li>Visual Readiness Score with domain colors</li>
            </ul>
            <p style="color:#475569;">Free Preview includes 10 fixed sample questions with full explanations. Premium unlocks the full preparation system.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_locked_premium_previews():
    st.subheader("Premium features locked")
    cards = [
        ("Overall Readiness Score", "Unlock a personalized readiness estimate based on mock exam performance, weighted domain scores, consistency, and practice volume."),
        ("Weak Areas Practice", "Unlock targeted practice sessions based on the Salesforce domains where your scores are weakest."),
        ("Visual Progress Dashboard", "Track score trends, domain performance, attempt history, and improvement over time."),
        ("Full Mock Exams", "Take full 60-question timed exams for Salesforce Administrator and Salesforce Business Analyst."),
    ]
    for title, body in cards:
        st.markdown(
            f"""
            <div style="border:1px solid #d8dde6;border-radius:10px;padding:14px;margin:10px 0;background:#ffffff;">
                <strong>🔒 {title}</strong><br>
                <span style="color:#475569;">{body}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )


def require_premium_access(feature_name="This feature"):
    email = require_login()
    if not has_premium_access(email):
        render_upgrade_card(feature_name)
        st.stop()
    return True


def require_admin():
    if is_admin_unlocked():
        render_sidebar_navigation()
        return True
    render_sidebar_navigation()
    st.error("Admin access required.")
    st.info("Click Admin in the sidebar and unlock admin mode with the admin password.")
    st.stop()


def require_admin_access():
    return require_admin()


@st.cache_data(ttl=120, show_spinner=False)
def fetch_active_certifications():
    try:
        result = (
            get_supabase_client()
            .table("certifications")
            .select("exam_name, display_name, certification_code, passing_score, time_limit_minutes, question_count, is_active")
            .eq("is_active", True)
            .order("display_name")
            .execute()
        )
        return result.data or []
    except Exception:
        return []


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
