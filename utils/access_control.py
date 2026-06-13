import sys
from pathlib import Path

_file = Path(__file__).resolve()
_root = _file.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import path_setup

path_setup.ensure_project_root(__file__)

import urllib.parse
import json
from datetime import datetime, timedelta

import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client

try:
    from streamlit_js_eval import streamlit_js_eval

    HAS_JS_EVAL = True
except Exception:
    streamlit_js_eval = None
    HAS_JS_EVAL = False

try:
    import extra_streamlit_components as stx

    HAS_EXTRA_COOKIE_MANAGER = True
except Exception:
    stx = None
    HAS_EXTRA_COOKIE_MANAGER = False

# Cookie names written to document.cookie (no extra_streamlit_components).
FR_COOKIE_USER_EMAIL = "fr_user_email"
FR_COOKIE_AUTH_USER_ID = "fr_auth_user_id"
FR_COOKIE_REFRESH_TOKEN = "fr_refresh_token"
FR_COOKIE_SUBSCRIPTION_STATUS = "fr_subscription_status"
FR_LOCAL_STORAGE_KEY = "fr_auth_session_v1"
FR_SESSION_COOKIE = "fr_auth_session_v2"


def ensure_project_root_on_path():
    """Backward-compatible alias used by older imports."""
    return path_setup.ensure_project_root(__file__)


ensure_project_root_on_path()

# --- Access level constants ---
FREE_STATUS = "free"
PAID_STATUS = "paid"
ADMIN_STATUS = "admin"
EXPIRED_STATUS = "expired"
TRIAL_STATUS = "trialing"

PAID_STATUS_VALUES = {"active", "paid", "premium", "subscribed"}
EXPIRED_STATUS_VALUES = {"expired", "cancelled", "canceled", "past_due", "inactive", "unpaid"}
TRIALING_EXCLUDED_BY_DEFAULT = True

ADMIN_SESSION_KEY = "admin_unlocked"
ACCESS_TOKEN_KEY = "supabase_access_token"
REFRESH_TOKEN_KEY = "supabase_refresh_token"
COOKIE_SYNCED_KEY = "_login_cookie_synced"
BROWSER_COOKIES_CACHE_KEY = "_browser_cookies_cache"
BROWSER_COOKIES_FETCHED_RUN_KEY = "_browser_cookies_fetched_run"
BROWSER_RESTORE_KEY = "_browser_login_restore_attempted"

ADMIN_EXAM_NAME = "Salesforce Certified Platform Administrator"
BA_EXAM_NAME = "Salesforce Certified Business Analyst"

FALLBACK_CERTIFICATIONS = [
    {
        "exam_name": ADMIN_EXAM_NAME,
        "display_name": ADMIN_EXAM_NAME,
        "passing_score": 68,
        "time_limit_minutes": 105,
        "question_count": 60,
    },
    {
        "exam_name": BA_EXAM_NAME,
        "display_name": BA_EXAM_NAME,
        "passing_score": 72,
        "time_limit_minutes": 70,
        "question_count": 60,
    },
]

LAUNCH_PRICE_TEXT = "$29.99 for 3 months"
REGULAR_PRICE_TEXT = "$49.99 regular price"


def get_secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def allow_trial_as_paid() -> bool:
    """Trialing is not paid unless explicitly enabled in Streamlit secrets."""
    raw = get_secret("ALLOW_TRIAL_AS_PAID", "false").lower()
    return raw in {"1", "true", "yes", "on"}


def _current_script_run_id():
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        ctx = get_script_run_ctx()
        if ctx is not None:
            return str(ctx.script_run_id)
    except Exception:
        pass
    return None


def _cookie_js_escape(value: str) -> str:
    return urllib.parse.quote(str(value or ""), safe="")


def _parse_document_cookies(raw: str) -> dict:
    cookies = {}
    for part in str(raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = urllib.parse.unquote(value.strip())
    return cookies


def _map_browser_cookies(parsed: dict) -> dict:
    return {
        "user_email": str(parsed.get(FR_COOKIE_USER_EMAIL) or "").strip().lower(),
        "auth_user_id": str(parsed.get(FR_COOKIE_AUTH_USER_ID) or "").strip(),
        "supabase_refresh_token": str(parsed.get(FR_COOKIE_REFRESH_TOKEN) or "").strip(),
        "subscription_status": str(parsed.get(FR_COOKIE_SUBSCRIPTION_STATUS) or "").strip().lower(),
    }


def _map_local_storage_session(raw_payload) -> dict:
    """Map the localStorage auth payload into the same shape as cookie restore."""
    if not raw_payload:
        return {}
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "user_email": str(payload.get("user_email") or "").strip().lower(),
        "auth_user_id": str(payload.get("auth_user_id") or "").strip(),
        "supabase_refresh_token": str(payload.get("supabase_refresh_token") or payload.get("refresh_token") or "").strip(),
        "subscription_status": str(payload.get("subscription_status") or FREE_STATUS).strip().lower(),
    }


def _map_session_payload(raw_payload) -> dict:
    """Map JSON auth payload from cookie/localStorage into restore shape."""
    if not raw_payload:
        return {}
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "user_email": str(payload.get("user_email") or "").strip().lower(),
        "auth_user_id": str(payload.get("auth_user_id") or "").strip(),
        "supabase_refresh_token": str(payload.get("supabase_refresh_token") or payload.get("refresh_token") or "").strip(),
        "subscription_status": str(payload.get("subscription_status") or FREE_STATUS).strip().lower(),
    }


def get_extra_cookie_manager():
    """Return one CookieManager instance per Streamlit browser session.

    The previous duplicate-key crash came from calling CookieManager.get_all()
    repeatedly with the package's default key. This object is created once and
    all reads go through get_browser_cookies_once().
    """
    if not HAS_EXTRA_COOKIE_MANAGER:
        return None
    if "_fr_cookie_manager" not in st.session_state:
        try:
            st.session_state["_fr_cookie_manager"] = stx.CookieManager(key="fr_cookie_manager")
        except Exception:
            st.session_state["_fr_cookie_manager"] = None
    return st.session_state.get("_fr_cookie_manager")


def get_browser_cookies_once():
    """Read persisted browser login once per Streamlit script run.

    Primary path: st.context.cookies, which sees cookies sent with the request.
    Secondary path: extra_streamlit_components.CookieManager, with a unique key.
    Fallback path: streamlit-js-eval reading localStorage/document.cookie.

    There must be only one direct get_all() call, here.
    """
    st.session_state.pop("_browser_cookie_manager", None)

    run_id = _current_script_run_id() or "default"
    if (
        st.session_state.get(BROWSER_COOKIES_FETCHED_RUN_KEY) == run_id
        and BROWSER_COOKIES_CACHE_KEY in st.session_state
    ):
        return st.session_state[BROWSER_COOKIES_CACHE_KEY]

    st.session_state["_browser_restore_attempted"] = True
    restored = {}

    # 1) Request cookies on Streamlit Cloud after browser refresh.
    try:
        context_cookies = getattr(getattr(st, "context", None), "cookies", None)
        if context_cookies:
            raw_payload = context_cookies.get(FR_SESSION_COOKIE, "")
            restored = _map_session_payload(urllib.parse.unquote(raw_payload))
            if not restored.get("user_email"):
                parsed = {
                    FR_COOKIE_USER_EMAIL: context_cookies.get(FR_COOKIE_USER_EMAIL, ""),
                    FR_COOKIE_AUTH_USER_ID: context_cookies.get(FR_COOKIE_AUTH_USER_ID, ""),
                    FR_COOKIE_REFRESH_TOKEN: context_cookies.get(FR_COOKIE_REFRESH_TOKEN, ""),
                    FR_COOKIE_SUBSCRIPTION_STATUS: context_cookies.get(FR_COOKIE_SUBSCRIPTION_STATUS, ""),
                }
                restored = _map_browser_cookies(parsed)
    except Exception:
        restored = {}

    # 2) CookieManager fallback. It writes real browser cookies better than iframe JS.
    if not restored.get("user_email"):
        try:
            manager = get_extra_cookie_manager()
            if manager is not None:
                all_cookies = manager.get_all(key=f"fr_get_all_{run_id}") or {}
                raw_payload = all_cookies.get(FR_SESSION_COOKIE, "")
                restored = _map_session_payload(raw_payload)
        except Exception:
            restored = restored or {}

    # 3) JS fallback. Usually needs a rerun before data arrives.
    if not restored.get("user_email") and HAS_JS_EVAL:
        try:
            raw = streamlit_js_eval(
                js_expressions=f"""
                JSON.stringify({{
                    local_storage: window.localStorage.getItem('{FR_LOCAL_STORAGE_KEY}'),
                    cookies: document.cookie || ''
                }})
                """,
                key=f"forceready_read_browser_auth_{run_id}",
                want_output=True,
            )
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(data, dict):
                    restored = _map_local_storage_session(data.get("local_storage"))
                    if not restored.get("user_email"):
                        parsed_cookies = _parse_document_cookies(data.get("cookies") or "")
                        raw_payload = parsed_cookies.get(FR_SESSION_COOKIE, "")
                        restored = _map_session_payload(raw_payload) or _map_browser_cookies(parsed_cookies)
        except Exception:
            restored = restored or {}

    st.session_state[BROWSER_COOKIES_CACHE_KEY] = restored or {}
    st.session_state[BROWSER_COOKIES_FETCHED_RUN_KEY] = run_id
    st.session_state["_browser_restore_success"] = bool((restored or {}).get("user_email"))
    return st.session_state[BROWSER_COOKIES_CACHE_KEY]


def save_browser_login_session(
    email: str,
    auth_user_id: str = "",
    refresh_token: str = "",
    subscription_status: str = "",
    auto_reload: bool = False,
):
    """Persist login in real browser cookie + localStorage fallback."""
    email = str(email or "").strip().lower()
    if not email:
        return False

    payload = {
        "user_email": email,
        "auth_user_id": str(auth_user_id or ""),
        "supabase_refresh_token": str(refresh_token or ""),
        "subscription_status": str(subscription_status or FREE_STATUS).strip().lower(),
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_cookie_value = urllib.parse.quote(payload_json, safe="")
    max_age = 30 * 24 * 60 * 60

    # CookieManager path: real browser cookie. One JSON cookie avoids duplicate set keys.
    try:
        manager = get_extra_cookie_manager()
        if manager is not None:
            counter = int(st.session_state.get("_fr_cookie_set_counter", 0)) + 1
            st.session_state["_fr_cookie_set_counter"] = counter
            manager.set(
                FR_SESSION_COOKIE,
                payload_json,
                expires_at=datetime.now() + timedelta(days=30),
                key=f"fr_set_session_{counter}",
            )
    except Exception:
        pass

    # JS fallback: localStorage + legacy cookies.
    reload_js = "setTimeout(function(){ try { window.parent.location.reload(); } catch(e) { window.location.reload(); } }, 900);" if auto_reload else ""
    html = f"""
    <script>
    (function() {{
        const payload = {json.dumps(payload_json)};
        try {{ window.parent.localStorage.setItem('{FR_LOCAL_STORAGE_KEY}', payload); }} catch(e1) {{
            try {{ window.localStorage.setItem('{FR_LOCAL_STORAGE_KEY}', payload); }} catch(e2) {{}}
        }}
        try {{
            const opts = "path=/; max-age={max_age}; SameSite=Lax";
            document.cookie = "{FR_SESSION_COOKIE}={payload_cookie_value}; " + opts;
            document.cookie = "{FR_COOKIE_USER_EMAIL}={_cookie_js_escape(email)}; " + opts;
            document.cookie = "{FR_COOKIE_AUTH_USER_ID}={_cookie_js_escape(auth_user_id)}; " + opts;
            document.cookie = "{FR_COOKIE_REFRESH_TOKEN}={_cookie_js_escape(refresh_token)}; " + opts;
            document.cookie = "{FR_COOKIE_SUBSCRIPTION_STATUS}={_cookie_js_escape(subscription_status)}; " + opts;
        }} catch(e3) {{}}
        {reload_js}
    }})();
    </script>
    """
    write_counter = int(st.session_state.get("_browser_auth_write_counter", 0)) + 1
    st.session_state["_browser_auth_write_counter"] = write_counter
    components.html(html, height=0, key=f"forceready_browser_auth_write_{write_counter}")

    st.session_state[COOKIE_SYNCED_KEY] = True
    st.session_state[BROWSER_COOKIES_CACHE_KEY] = dict(payload)
    return True


def clear_browser_login_session(auto_reload: bool = False):
    try:
        manager = get_extra_cookie_manager()
        if manager is not None:
            counter = int(st.session_state.get("_fr_cookie_delete_counter", 0)) + 1
            st.session_state["_fr_cookie_delete_counter"] = counter
            manager.delete(FR_SESSION_COOKIE, key=f"fr_delete_session_{counter}")
    except Exception:
        pass

    reload_js = "setTimeout(function(){ try { window.parent.location.reload(); } catch(e) { window.location.reload(); } }, 700);" if auto_reload else ""
    html = f"""
    <script>
    (function() {{
        try {{ window.parent.localStorage.removeItem('{FR_LOCAL_STORAGE_KEY}'); }} catch(e1) {{
            try {{ window.localStorage.removeItem('{FR_LOCAL_STORAGE_KEY}'); }} catch(e2) {{}}
        }}
        try {{
            const names = [
                "{FR_SESSION_COOKIE}",
                "{FR_COOKIE_USER_EMAIL}",
                "{FR_COOKIE_AUTH_USER_ID}",
                "{FR_COOKIE_REFRESH_TOKEN}",
                "{FR_COOKIE_SUBSCRIPTION_STATUS}",
            ];
            names.forEach((name) => {{ document.cookie = name + "=; path=/; max-age=0; SameSite=Lax"; }});
        }} catch(e3) {{}}
        {reload_js}
    }})();
    </script>
    """
    clear_counter = int(st.session_state.get("_browser_auth_clear_counter", 0)) + 1
    st.session_state["_browser_auth_clear_counter"] = clear_counter
    components.html(html, height=0, key=f"forceready_browser_auth_clear_{clear_counter}")
    st.session_state.pop(BROWSER_COOKIES_CACHE_KEY, None)
    st.session_state.pop(BROWSER_COOKIES_FETCHED_RUN_KEY, None)


def try_restore_login_from_browser(cookies=None) -> bool:
    """Restore login from browser cookies after refresh."""
    if st.session_state.get("user_email") or st.session_state.get("account_email"):
        return False

    if cookies is None:
        cookies = get_browser_cookies_once()

    email = str(cookies.get("user_email") or "").strip().lower()
    if not email or "@" not in email:
        return False

    auth_user_id = str(cookies.get("auth_user_id") or "").strip()
    refresh_token = str(cookies.get("supabase_refresh_token") or "").strip()
    saved_status = str(cookies.get("subscription_status") or "").strip().lower()

    _apply_restored_login(email, auth_user_id, refresh_token)
    if saved_status:
        st.session_state["subscription_status"] = saved_status
    return True


def save_auth_session(access_token: str = "", refresh_token: str = ""):
    """Persist Supabase Auth tokens in session_state for RLS-aware requests."""
    if access_token:
        st.session_state[ACCESS_TOKEN_KEY] = str(access_token)
    if refresh_token:
        st.session_state[REFRESH_TOKEN_KEY] = str(refresh_token)


def clear_auth_session():
    for key in [ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY]:
        st.session_state.pop(key, None)


def _restore_auth_from_refresh_token(refresh_token: str) -> bool:
    if not refresh_token:
        return False
    try:
        client = _create_anon_supabase_client()
        response = client.auth.refresh_session(refresh_token)
        session = getattr(response, "session", None)
        if session is None and isinstance(response, dict):
            session = response.get("session")
        if not session:
            return False
        access_token = getattr(session, "access_token", None) or (session.get("access_token") if isinstance(session, dict) else None)
        new_refresh = getattr(session, "refresh_token", None) or (session.get("refresh_token") if isinstance(session, dict) else None)
        if access_token and new_refresh:
            save_auth_session(access_token, new_refresh)
            return True
    except Exception:
        return False
    return False


def _create_anon_supabase_client():
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_ANON_KEY")
    if not url or not key:
        st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


def _create_admin_supabase_client():
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


@st.cache_resource(show_spinner=False)
def get_supabase_public_client():
    """Anonymous client for public metadata reads (certifications, languages)."""
    return _create_anon_supabase_client()


def get_supabase_auth_client():
    """Fresh anon Supabase client used only for auth actions like login/signup.

    Do not cache this client.
    """
    supabase_url = st.secrets.get("SUPABASE_URL")
    supabase_anon_key = st.secrets.get("SUPABASE_ANON_KEY")

    if not supabase_url or not supabase_anon_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_ANON_KEY in Streamlit secrets.")

    return create_client(supabase_url, supabase_anon_key)


def get_supabase_client():
    """RLS-aware user client. Uses anon key + stored Supabase Auth session when available.

    Do not cache: Streamlit cache_resource is shared across browser sessions.
    """
    client = _create_anon_supabase_client()
    access = st.session_state.get(ACCESS_TOKEN_KEY)
    refresh = st.session_state.get(REFRESH_TOKEN_KEY)
    if access and refresh:
        try:
            client.auth.set_session(access, refresh)
        except Exception:
            pass
    return client


@st.cache_resource(show_spinner=False)
def get_supabase_admin_client():
    """Privileged client for admin/import operations only. Call require_admin() first."""
    return _create_admin_supabase_client()


def get_admin_password():
    return get_secret("ADMIN_PASSWORD", "")


def get_admin_emails():
    raw = get_secret("ADMIN_EMAILS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _apply_restored_login(email, auth_user_id, refresh_token):
    if refresh_token and not st.session_state.get(ACCESS_TOKEN_KEY):
        if _restore_auth_from_refresh_token(refresh_token):
            st.session_state.pop(COOKIE_SYNCED_KEY, None)

    st.session_state["user_email"] = email
    st.session_state["account_email"] = email
    st.session_state["auth_user_id"] = auth_user_id or ""
    st.session_state.setdefault(COOKIE_SYNCED_KEY, True)

    profile = get_user_profile(email)
    if profile:
        st.session_state["full_name"] = profile.get("full_name") or ""
        st.session_state["preferred_language_code"] = profile.get("preferred_language_code") or "en"
        st.session_state["subscription_status"] = profile.get("subscription_status") or FREE_STATUS
    else:
        st.session_state.setdefault("full_name", "")
        st.session_state.setdefault("preferred_language_code", "en")
        st.session_state.setdefault("subscription_status", FREE_STATUS)


def ensure_browser_auth_ready():
    """Restore login from browser cookies when session_state is empty."""
    if get_current_user_email():
        return

    cookies = get_browser_cookies_once()
    try_restore_login_from_browser(cookies)


def try_restore_login_from_cookie() -> bool:
    """Backward-compatible alias."""
    return try_restore_login_from_browser(get_browser_cookies_once())


def init_persistent_login():
    """Backward-compatible alias."""
    try_restore_login_from_browser(get_browser_cookies_once())


def get_current_user_email():
    email = st.session_state.get("user_email", "") or st.session_state.get("account_email", "")
    email = str(email).strip().lower()
    if email and "@" in email and "." in email.split("@")[-1]:
        return email
    return None


def is_logged_in() -> bool:
    return get_current_user_email() is not None


def restore_login_from_cookie():
    """Backward-compatible alias."""
    try_restore_login_from_cookie()
    return st.session_state.get("user_email")


def get_user_profile(email=None):
    email = (email or get_current_user_email() or "").strip().lower()
    if not email:
        return None
    for client_factory in (get_supabase_client, get_supabase_admin_client):
        try:
            result = (
                client_factory()
                .table("app_users")
                .select("*")
                .eq("email", email)
                .limit(1)
                .execute()
            )
            rows = result.data or []
            if rows:
                return rows[0]
        except Exception:
            continue
    return None


def get_subscription_status(email=None):
    cached = st.session_state.get("subscription_status")
    if cached:
        return str(cached).strip().lower()
    profile = get_user_profile(email=email)
    if not profile:
        return FREE_STATUS
    status = str(profile.get("subscription_status") or FREE_STATUS).strip().lower()
    st.session_state["subscription_status"] = status
    return status


def is_admin_user(email=None):
    email = (email or get_current_user_email() or "").strip().lower()
    admins = get_admin_emails()
    return bool(email and admins and email in admins)


def is_admin_email(email=None):
    """Backward-compatible alias."""
    return is_admin_user(email=email)


def is_admin_unlocked():
    return bool(st.session_state.get(ADMIN_SESSION_KEY, False)) and is_admin_user()


def get_user_access_level(email=None) -> str:
    email = email or get_current_user_email()
    if not email:
        return FREE_STATUS
    if is_admin_unlocked():
        return ADMIN_STATUS

    status = get_subscription_status(email)
    if status in PAID_STATUS_VALUES:
        return PAID_STATUS
    if status == TRIAL_STATUS and allow_trial_as_paid():
        return PAID_STATUS
    if status in EXPIRED_STATUS_VALUES or status == TRIAL_STATUS:
        return EXPIRED_STATUS if status in EXPIRED_STATUS_VALUES else FREE_STATUS
    return FREE_STATUS


def is_paid_user(email=None):
    return get_user_access_level(email) == PAID_STATUS


def has_premium_access(email=None):
    level = get_user_access_level(email)
    return level in {PAID_STATUS, ADMIN_STATUS}


def lock_admin():
    st.session_state[ADMIN_SESSION_KEY] = False


def unlock_admin(password):
    email = get_current_user_email()
    if not email:
        return False, "Please log in on the Account page first."
    if not is_admin_user(email):
        return False, "This account is not listed as an admin."
    expected = get_admin_password()
    if not expected:
        return False, "ADMIN_PASSWORD is missing in Streamlit Secrets."
    if str(password or "") != expected:
        return False, "Incorrect admin password."
    st.session_state[ADMIN_SESSION_KEY] = True
    return True, None


def hide_default_streamlit_pages():
    """Hide Streamlit's native multipage sidebar navigation.

    Streamlit Cloud can still render the automatic pages/ navigation even when
    .streamlit/config.toml is present, depending on version/cache/deployment.
    We hide only the native nav container, not our custom ForceReady sidebar.
    """
    st.markdown(
        """
        <style>
        /* Hide Streamlit's built-in multipage navigation only. */
        [data-testid="stSidebarNav"] {
            display: none !important;
            visibility: hidden !important;
            height: 0 !important;
            overflow: hidden !important;
        }
        [data-testid="stSidebarNav"] * {
            display: none !important;
            visibility: hidden !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )



def safe_page_link(page, label, icon=None):
    try:
        st.sidebar.page_link(page, label=label, icon=icon)
    except Exception:
        st.sidebar.write(f"{icon or ''} {label}")


def render_top_navigation():
    """Main-area navigation filtered by current access state."""
    email = get_current_user_email()
    links = [
        ("app.py", "Free Preview", "📝"),
        ("pages/Account.py", "Account", "👤"),
        ("pages/Support.py", "Support", "💬"),
    ]

    if has_premium_access(email):
        links.extend([
            ("pages/Practice_By_Category.py", "Practice", "📚"),
            ("pages/Weak_Areas_Practice.py", "Weak Areas", "🎯"),
            ("pages/My_Progress.py", "My Progress", "📈"),
        ])

    if email and is_admin_user(email):
        links.append(("pages/Admin.py", "Admin", "🔐"))

    cols = st.columns(max(1, len(links)))
    for col, (page, label, icon) in zip(cols, links):
        with col:
            try:
                st.page_link(page, label=label, icon=icon)
            except Exception:
                st.write(f"{icon or ''} {label}")
    st.divider()

def render_app_chrome(current_page=None):
    """Sidebar + always-visible top navigation."""
    ensure_browser_auth_ready()
    render_sidebar_navigation(current_page=current_page)
    render_top_navigation()


def render_sidebar_navigation(current_page=None):
    hide_default_streamlit_pages()
    st.sidebar.markdown("### Salesforce Prep")

    email = get_current_user_email()
    if email:
        level = get_user_access_level(email)
        st.sidebar.caption(f"Signed in: {email}")
        if level == ADMIN_STATUS:
            st.sidebar.success("Admin access")
        elif level == PAID_STATUS:
            st.sidebar.success("Premium access")
        elif level == EXPIRED_STATUS:
            st.sidebar.warning("Subscription expired")
        else:
            st.sidebar.info("Free Preview")
    else:
        st.sidebar.caption("Not signed in")

    st.sidebar.markdown("#### Main")
    safe_page_link("app.py", "Free Preview / Mock Exam", "📝")
    safe_page_link("pages/Account.py", "Account", "👤")
    safe_page_link("pages/Support.py", "Support", "💬")

    if has_premium_access(email):
        st.sidebar.markdown("#### Premium")
        safe_page_link("pages/Practice_By_Category.py", "Practice by Category", "📚")
        safe_page_link("pages/Weak_Areas_Practice.py", "Weak Areas Practice", "🎯")
        safe_page_link("pages/My_Progress.py", "My Progress & Readiness", "📈")

    if email and is_admin_user(email):
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
            st.sidebar.caption("Unlock admin mode from the Admin page.")

def require_login():
    ensure_browser_auth_ready()
    email = get_current_user_email()
    if not email:
        render_app_chrome()
        st.warning("Please log in from the Account page first.")
        st.stop()
    return email


def show_locked_premium_message(feature_name="this premium feature"):
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


def render_upgrade_card(feature_name="this premium feature"):
    """Backward-compatible alias."""
    show_locked_premium_message(feature_name)


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
        render_app_chrome()
        show_locked_premium_message(feature_name)
        st.stop()
    return email


def require_admin():
    require_login()
    if not is_admin_user():
        render_app_chrome()
        st.error("This account is not authorized as an admin.")
        st.stop()
    if not is_admin_unlocked():
        render_app_chrome()
        st.error("Admin access required.")
        st.info("Click Admin in the sidebar and unlock admin mode with the admin password.")
        st.stop()
    render_app_chrome()
    return True


def require_admin_access():
    return require_admin()


@st.cache_data(ttl=120, show_spinner=False)
def fetch_active_certifications():
    try:
        result = (
            get_supabase_public_client()
            .table("certifications")
            .select("exam_name, display_name, certification_code, passing_score, time_limit_minutes, question_count, is_active")
            .eq("is_active", True)
            .order("display_name")
            .execute()
        )
        rows = result.data or []
        if rows:
            return rows
        result = (
            get_supabase_public_client()
            .table("certifications")
            .select("exam_name, display_name, certification_code, passing_score, time_limit_minutes, question_count, is_active")
            .order("display_name")
            .execute()
        )
        return result.data or []
    except Exception:
        return []


def get_available_certifications():
    """Active certifications from Supabase, or Admin + Business Analyst defaults."""
    rows = fetch_active_certifications()
    if rows:
        return rows
    return [dict(cert) for cert in FALLBACK_CERTIFICATIONS]


def render_admin_login_page():
    render_app_chrome("Admin")
    st.title("Admin")
    st.caption("Unlock admin pages for this browser session.")

    email = get_current_user_email()
    if not email:
        st.warning("Log in on the Account page first, then return here.")
        safe_page_link("pages/Account.py", "Go to Account", "👤")
        st.stop()

    if not is_admin_user(email):
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


def extract_auth_session(response):
    """Return the Supabase session object from an auth response when present."""
    if response is None:
        return None
    session = getattr(response, "session", None)
    if session is not None:
        return session
    if isinstance(response, dict):
        return response.get("session")
    return None


def default_free_profile(email: str) -> dict:
    return {
        "email": str(email).strip().lower(),
        "full_name": "",
        "preferred_language_code": "en",
        "subscription_status": FREE_STATUS,
    }


def save_logged_in_user(
    email: str,
    auth_user_id: str | None = None,
    profile: dict | None = None,
    session=None,
    write_browser_cookies: bool | None = None,
    auto_reload_browser: bool = False,
):
    """Centralized post-login session persistence."""
    email = str(email).strip().lower()
    st.session_state["user_email"] = email
    st.session_state["account_email"] = email
    st.session_state["auth_user_id"] = auth_user_id or ""

    refresh_token = st.session_state.get(REFRESH_TOKEN_KEY, "")
    if session is not None:
        access_token = getattr(session, "access_token", None)
        refresh_token = getattr(session, "refresh_token", None)
        if isinstance(session, dict):
            access_token = access_token or session.get("access_token")
            refresh_token = refresh_token or session.get("refresh_token")
        save_auth_session(access_token or "", refresh_token or "")

    subscription_status = FREE_STATUS
    if profile:
        subscription_status = str(profile.get("subscription_status") or FREE_STATUS).strip().lower()

    should_write_cookies = write_browser_cookies if write_browser_cookies is not None else session is not None
    if should_write_cookies:
        save_browser_login_session(
            email,
            auth_user_id or "",
            refresh_token,
            subscription_status,
            auto_reload=auto_reload_browser,
        )

    if profile:
        st.session_state["full_name"] = profile.get("full_name") or ""
        st.session_state["preferred_language_code"] = profile.get("preferred_language_code") or "en"
        st.session_state["subscription_status"] = profile.get("subscription_status") or FREE_STATUS
    else:
        st.session_state.setdefault("full_name", "")
        st.session_state.setdefault("preferred_language_code", "en")
        st.session_state.setdefault("subscription_status", FREE_STATUS)


def clear_logged_in_user(auto_reload_browser: bool = False):
    clear_auth_session()
    clear_browser_login_session(auto_reload=auto_reload_browser)
    for key in [
        "user_email",
        "account_email",
        "auth_user_id",
        "full_name",
        "preferred_language_code",
        "subscription_status",
        COOKIE_SYNCED_KEY,
        BROWSER_COOKIES_CACHE_KEY,
        BROWSER_COOKIES_FETCHED_RUN_KEY,
        "_browser_cookie_manager",
    ]:
        st.session_state.pop(key, None)
    lock_admin()
