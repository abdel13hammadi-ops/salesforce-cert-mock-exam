"""Centralized auth, access control, and navigation helpers for the Streamlit app.

Streamlit wipes `st.session_state` on a hard browser refresh, so login cannot live
only in session_state. This module persists a short HMAC-signed session token
durably in browser localStorage, and reads it back via an acknowledged
`streamlit_js_eval` round trip on every page load (`bootstrap_signed_session()`).

The URL query parameter `fr_session` is a ONE-TIME direct-link/bootstrap
mechanism only (SIM-SMOKE-02B): a token may arrive via `?fr_session=...` (e.g.
an emailed deep link), gets verified and hydrated into session_state
immediately, and is removed from the URL only after the browser explicitly
confirms it has been persisted to localStorage (`_finalize_url_bootstrap_handoff()`).
After that point, `fr_session` must never be reintroduced -- not by ordinary
reruns, not by idle-activity token stamping (`stamp_activity_to_token()`), and
not by normal authenticated navigation (`_clean_nav_href()`). All of those
paths refresh only the in-memory token and browser localStorage.

The token contains no password and no Supabase service key. It is still a bearer
session token, so COOKIE_PASSWORD must be strong in production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from typing import Any, Dict, Optional, Sequence
from urllib.parse import quote

import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client

PAID_STATUS_VALUES = {"active", "paid", "premium", "subscribed", "trialing"}
EXPIRED_STATUS_VALUES = {"expired", "cancelled", "canceled", "past_due", "unpaid"}
FREE_STATUS = "free"
SESSION_PARAM = "fr_session"
BROWSER_SESSION_STORAGE_KEY = "salesforce_cert_mock_fr_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
SESSION_REFRESH_WINDOW_SECONDS = 60 * 60 * 24 * 7

# SIM-SMOKE-02H: opt-in, environment-gated auth-bootstrap diagnostics.
#
# These markers exist ONLY to let a disposable smoke-test run capture which
# branch of the signed-session bootstrap / idle-timeout / access-gate
# sequence actually executed, without ever being able to leak a token,
# signature, secret, URL, or email. Every event name and every field
# name/value is checked against the fixed allowlist below before anything is
# written -- there is no code path that accepts an arbitrary caller-supplied
# value. Completely disabled unless CERTBOUND_AUTH_SMOKE_DIAGNOSTICS=1.
_AUTH_SMOKE_DIAGNOSTICS_ENV_VAR = "CERTBOUND_AUTH_SMOKE_DIAGNOSTICS"

# Maps each allowed event name to its allowed fields. Each field value is
# either the literal type `bool` (only True/False accepted) or a tuple of
# the exact literal values accepted (ints and/or fixed enum strings).
_AUTH_SMOKE_EVENT_FIELDS: Dict[str, Dict[str, Any]] = {
    "render_app_chrome_started": {},
    "bootstrap_started": {"user_email_present": bool},
    "fr_session_query_state": {"present": bool, "count": (0, 1, "more_than_one")},
    "query_inspection_failed": {"failed": bool},
    "token_verified": {"result": ("valid", "invalid")},
    "token_verification_detail": {
        "reason": (
            "missing_separator",
            "signature_decode_failed",
            "signature_mismatch",
            "payload_decode_failed",
            "expired",
            "invalid_email",
            "valid",
        )
    },
    "session_hydrated": {"completed": bool, "user_email_present": bool},
    "browser_storage_write": {"result": ("pending", "confirmed", "failed")},
    "fr_session_handoff": {"retained": bool, "user_email_present": bool},
    "timeout_activity_check": {"state": ("no_timestamp", "fresh", "stale", "invalid")},
    "timeout_cleanup": {"ran": bool},
    "require_login_check": {"state": ("authenticated", "restoration_pending", "unauthenticated")},
}


def _auth_smoke_diagnostics_enabled() -> bool:
    return os.environ.get(_AUTH_SMOKE_DIAGNOSTICS_ENV_VAR) == "1"


def _auth_smoke_value_allowed(spec: Any, value: Any) -> bool:
    if spec is bool:
        return isinstance(value, bool)
    if isinstance(spec, tuple):
        # bool is a subclass of int in Python (True == 1), so an accidental
        # bool must never be accepted by an int/enum-string field spec.
        if isinstance(value, bool):
            return False
        return value in spec
    return False


def _auth_smoke_trace(event: str, **safe_fields: Any) -> None:
    """Emit one fixed, allowlisted auth-bootstrap state-transition marker.

    A no-op unless `CERTBOUND_AUTH_SMOKE_DIAGNOSTICS=1`. `event` must be a
    key of `_AUTH_SMOKE_EVENT_FIELDS`; any field name not declared for that
    event, or any value that is not exactly `bool` or one of the fixed
    literal values declared for that field, is silently dropped rather than
    printed -- there is no fallback path that stringifies or logs an
    unrecognized value. Never accepts (and therefore can never leak) a
    token, email, URL, secret, or raw payload. Never raises.
    """
    if not _auth_smoke_diagnostics_enabled():
        return
    try:
        allowed_fields = _AUTH_SMOKE_EVENT_FIELDS.get(event)
        if allowed_fields is None:
            return
        parts = [f"event={event}"]
        for name in sorted(allowed_fields):
            if name not in safe_fields:
                continue
            value = safe_fields[name]
            if not _auth_smoke_value_allowed(allowed_fields[name], value):
                continue
            parts.append(f"{name}={value}")
        sys.stderr.write("[certbound_auth_smoke] " + " ".join(parts) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _secret(name: str, default: str = "") -> str:
    """Read config from Render environment variables first, then Streamlit secrets."""
    env_value = str(os.environ.get(name, "") or "").strip()
    if env_value:
        return env_value
    try:
        return str(st.secrets.get(name, default) or "").strip()
    except Exception:
        return default


SUPABASE_ADMIN_CONFIG_HELP = (
    "Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY using environment variables "
    "or .streamlit/secrets.toml (the same secure sources used by the CertBound admin app)."
)


class SupabaseAdminConfigError(RuntimeError):
    """Raised when Supabase admin credentials are not configured."""


def create_supabase_admin_client():
    """Create a service-role Supabase client from shared CertBound configuration."""
    url = _secret("SUPABASE_URL")
    key = _secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise SupabaseAdminConfigError(
            f"Missing Supabase admin configuration. {SUPABASE_ADMIN_CONFIG_HELP}"
        )
    return create_client(url, key)


def _signing_secret() -> str:
    # COOKIE_PASSWORD is the intended signing secret. SUPABASE_SERVICE_ROLE_KEY is
    # accepted as a legacy fallback so existing deploys do not break, but there is
    # no insecure dev fallback anymore. Missing signing secrets must fail closed.
    secret = _secret("COOKIE_PASSWORD") or _secret("SUPABASE_SERVICE_ROLE_KEY")
    if not secret:
        st.error("Missing COOKIE_PASSWORD or SUPABASE_SERVICE_ROLE_KEY. Session signing cannot run safely.")
        st.stop()
    return secret


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + pad).encode("utf-8"))


def make_signed_session(payload: Dict[str, Any]) -> str:
    safe_payload = dict(payload or {})
    safe_payload["exp"] = int(time.time()) + SESSION_TTL_SECONDS
    raw = json.dumps(safe_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64url_encode(raw)
    sig = hmac.new(_signing_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def verify_signed_session(token: str) -> Optional[Dict[str, Any]]:
    """Verify a signed session token.

    SIM-SMOKE-02I: each existing `return` statement below now emits exactly
    one allowlisted `token_verification_detail` diagnostic marker
    immediately before returning, carrying only a fixed rejection-category
    enum -- never the token, body, signature, payload, email, timestamp, or
    any derived value. `_auth_smoke_trace(...)` is itself an unconditional
    no-op unless `CERTBOUND_AUTH_SMOKE_DIAGNOSTICS=1`, and never raises, so
    this cannot change this function's return value or introduce a new
    exception path for any input, including malformed/non-integer `exp`
    values (that comparison is untouched and can still raise exactly as it
    always could -- no diagnostic is reachable on that pre-existing path).
    """
    token = str(token or "").strip()
    if "." not in token:
        _auth_smoke_trace("token_verification_detail", reason="missing_separator")
        return None
    body, sig = token.split(".", 1)
    expected = hmac.new(_signing_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    try:
        actual = _b64url_decode(sig)
    except Exception:
        _auth_smoke_trace("token_verification_detail", reason="signature_decode_failed")
        return None
    if not hmac.compare_digest(expected, actual):
        _auth_smoke_trace("token_verification_detail", reason="signature_mismatch")
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception:
        _auth_smoke_trace("token_verification_detail", reason="payload_decode_failed")
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        _auth_smoke_trace("token_verification_detail", reason="expired")
        return None
    email = str(payload.get("user_email") or payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        _auth_smoke_trace("token_verification_detail", reason="invalid_email")
        return None
    _auth_smoke_trace("token_verification_detail", reason="valid")
    return payload


def _get_query_param(name: str) -> str:
    try:
        value = st.query_params.get(name, "")
        if isinstance(value, list):
            return str(value[-1] if value else "")
        return str(value or "")
    except Exception:
        return ""


def _read_signed_session_query_token() -> tuple[Optional[str], bool, bool]:
    """Return `(token, key_present, read_failed)`.

    `key_present` is true when `fr_session` exists in the query collection,
    even if the reference is ambiguous, empty, or malformed. `read_failed`
    is true only when the query-parameter API itself could not be inspected.
    """
    try:
        key_present = SESSION_PARAM in st.query_params
    except Exception:
        _auth_smoke_trace("query_inspection_failed", failed=True)
        return None, True, True
    _auth_smoke_trace("query_inspection_failed", failed=False)

    if not key_present:
        _auth_smoke_trace("fr_session_query_state", present=False, count=0)
        return None, False, False

    try:
        raw_values = st.query_params.get_all(SESSION_PARAM)
    except Exception:
        _auth_smoke_trace("query_inspection_failed", failed=True)
        return None, True, True

    count = len(raw_values)
    _auth_smoke_trace(
        "fr_session_query_state",
        present=True,
        count=count if count in (0, 1) else "more_than_one",
    )

    if len(raw_values) != 1:
        return None, True, False

    token = str(raw_values[0] or "").strip()
    if not token:
        return None, True, False

    return token, True, False


def _clear_signed_session_query_token() -> None:
    _clear_query_param(SESSION_PARAM)


def _set_query_param(name: str, value: str) -> None:
    try:
        st.query_params[name] = value
    except Exception:
        pass


def _clear_query_param(name: str) -> None:
    try:
        if name in st.query_params:
            del st.query_params[name]
    except Exception:
        pass


def _email_is_configured_admin(email: Optional[str]) -> bool:
    email = str(email or "").strip().lower()
    admin_emails = [e.strip().lower() for e in _secret("ADMIN_EMAILS").split(",") if e.strip()]
    return bool(email and email in admin_emails)


def _session_payload_from_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    email = str(profile.get("email") or profile.get("user_email") or "").strip().lower()
    admin_unlocked = bool(st.session_state.get("admin_unlocked") and _email_is_configured_admin(email))
    return {
        "user_email": email,
        "auth_user_id": str(profile.get("auth_user_id") or ""),
        "full_name": str(profile.get("full_name") or ""),
        "preferred_language_code": str(profile.get("preferred_language_code") or "en").strip().lower() or "en",
        "preferred_timezone": str(profile.get("preferred_timezone") or "UTC").strip() or "UTC",
        "subscription_status": str(profile.get("subscription_status") or "free").strip().lower(),
        "admin_unlocked": admin_unlocked,
        # last_activity_at rides inside the signed token so it survives full browser
        # navigation without requiring session_state to persist.
        "last_activity_at": float(st.session_state.get("last_activity_at") or time.time()),
    }


def _session_payloads_match(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    keys = ["user_email", "auth_user_id", "full_name", "preferred_language_code", "preferred_timezone", "subscription_status", "admin_unlocked"]
    for key in keys:
        if str(left.get(key, "")).strip().lower() != str(right.get(key, "")).strip().lower():
            return False
    return True


def _current_signed_session_token() -> str:
    """The authoritative in-memory signed token for this Streamlit session.

    SIM-SMOKE-02B: `fr_session` is a one-time direct-link bootstrap value
    only, never an ongoing store -- this deliberately does NOT fall back to
    reading the URL query parameter. Once a token has been hydrated (from a
    URL bootstrap or from browser localStorage), it lives only in
    `st.session_state` and, best-effort, in browser localStorage.
    """
    return str(st.session_state.get("signed_session_token") or "").strip()


def _persist_token_to_browser_only(token: str) -> None:
    """Update the authoritative in-memory token and best-effort push it to
    browser localStorage. Never writes `fr_session` into the URL -- see
    `_finalize_url_bootstrap_handoff()` for the one place a URL token is
    ever removed after a confirmed browser write, and the module docstring
    for why ordinary reruns/navigation must never reintroduce it."""
    st.session_state["signed_session_token"] = token
    _write_browser_session_token_via_js_eval(token)


def _mark_browser_session_clear_needed() -> None:
    st.session_state["clear_browser_session_storage"] = True


def _render_browser_session_clearer() -> None:
    components.html(
        f"""
        <script>
        (function () {{
            const paramName = {json.dumps(SESSION_PARAM)};
            const storageKey = {json.dumps(BROWSER_SESSION_STORAGE_KEY)};
            try {{
                const storage = (window.parent && window.parent.localStorage) ? window.parent.localStorage : window.localStorage;
                storage.removeItem(storageKey);
            }} catch (e) {{}}

            try {{
                const loc = (window.parent && window.parent.location) ? window.parent.location : window.location;
                const url = new URL(loc.href);
                if (url.searchParams.has(paramName)) {{
                    url.searchParams.delete(paramName);
                    loc.replace(url.toString());
                }}
            }} catch (e) {{}}
        }})();
        </script>
        """,
        height=0,
    )


def _render_pending_browser_storage_clear_if_needed() -> None:
    """Render the fire-and-forget localStorage/URL clearer if logout or an
    invalid browser token marked one as needed. Clearing does not require an
    acknowledged round trip -- worst case a stale/invalid entry is later
    rejected again by `verify_signed_session(...)`."""
    if st.session_state.pop("clear_browser_session_storage", False):
        _render_browser_session_clearer()


def restore_login_from_signed_url() -> bool:
    """Restore session_state from signed query token, if present and valid."""
    # Do not re-authenticate a session that was explicitly expired by the timeout.
    # The flag is cleared by save_logged_in_user() when the user logs back in.
    if st.session_state.get("user_session_expired"):
        return False
    if st.session_state.get("user_email"):
        return True

    token, key_present, read_failed = _read_signed_session_query_token()
    if read_failed:
        st.session_state["_session_query_read_failed"] = True
        return False
    if key_present and not token:
        _clear_signed_session_query_token()
        return False
    if not token:
        return False

    payload = verify_signed_session(token)
    _auth_smoke_trace("token_verified", result="valid" if payload else "invalid")
    if not payload:
        _clear_signed_session_query_token()
        return False

    _hydrate_session_from_payload(payload, token)
    _auth_smoke_trace(
        "session_hydrated",
        completed=True,
        user_email_present=bool(st.session_state.get("user_email")),
    )
    return True


def _hydrate_session_from_payload(payload: Dict[str, Any], token: str) -> None:
    email = str(payload.get("user_email") or payload.get("email") or "").strip().lower()
    st.session_state["user_email"] = email
    st.session_state["auth_user_id"] = str(payload.get("auth_user_id") or "")
    st.session_state["full_name"] = str(payload.get("full_name") or "")
    st.session_state["preferred_language_code"] = str(
        payload.get("preferred_language_code") or "en"
    ).strip().lower() or "en"
    st.session_state["preferred_timezone"] = str(payload.get("preferred_timezone") or "UTC").strip() or "UTC"
    st.session_state["subscription_status"] = str(payload.get("subscription_status") or "free").strip().lower()
    st.session_state["signed_session_token"] = token
    if bool(payload.get("admin_unlocked")) and _email_is_configured_admin(email):
        st.session_state["admin_unlocked"] = True
    st.session_state["auth_restored_from_url"] = True
    stored_activity = payload.get("last_activity_at")
    st.session_state["last_activity_at"] = (
        float(stored_activity) if stored_activity is not None else time.time()
    )


def _read_browser_session_token_via_js_eval() -> Optional[str]:
    """Read the signed session token from browser localStorage.

    Returns None while streamlit-js-eval is still waiting on the browser callback.
    """
    try:
        from streamlit_js_eval import streamlit_js_eval  # noqa: PLC0415
    except Exception:
        return ""

    js = f"""
    (function() {{
        try {{
            const storage = window.localStorage;
            if (!storage) return '';
            return storage.getItem({json.dumps(BROWSER_SESSION_STORAGE_KEY)}) || '';
        }} catch (e) {{
            return '';
        }}
    }})()
    """
    try:
        value = streamlit_js_eval(js_expressions=js, key="certbound_fr_session_read_v1")
    except Exception:
        return ""
    if value is None:
        return None
    return str(value or "").strip()


def _write_browser_session_token_via_js_eval(token: str) -> Optional[bool]:
    """Persist `token` to browser localStorage with an explicit acknowledged
    round trip (SIM-SMOKE-02B).

    Rendering an async HTML component is not proof the browser stored
    anything -- this uses the same `streamlit_js_eval` component protocol
    already relied on for reads (a real request/response round trip, not
    fire-and-forget `components.html`), so the caller can distinguish:

    - True: the browser confirmed the write succeeded;
    - None: the component callback has not returned yet (still pending);
    - False: `streamlit_js_eval` is unavailable, or the browser explicitly
      reported the write failed (e.g. localStorage disabled/blocked).

    The component key is derived from a fingerprint of the token, not the
    raw token, so a new token value always gets a fresh round trip while an
    unchanged token keeps returning its already-cached acknowledgment on
    later reruns without re-executing JS.
    """
    token = str(token or "").strip()
    if not token:
        return False
    try:
        from streamlit_js_eval import streamlit_js_eval  # noqa: PLC0415
    except Exception:
        return False

    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    js = f"""
    (function() {{
        try {{
            const storage = window.localStorage;
            if (!storage) return 'error';
            storage.setItem({json.dumps(BROWSER_SESSION_STORAGE_KEY)}, {json.dumps(token)});
            return 'ok';
        }} catch (e) {{
            return 'error';
        }}
    }})()
    """
    try:
        value = streamlit_js_eval(js_expressions=js, key=f"certbound_fr_session_write_{fingerprint}")
    except Exception:
        return False
    if value is None:
        return None
    return str(value).strip() == "ok"


def is_session_restoration_pending() -> bool:
    """True while a direct page load is waiting on browser session hydration."""
    if st.session_state.get("user_email"):
        return False
    if st.session_state.get("user_session_expired"):
        return False
    return bool(st.session_state.get("_session_restoration_pending"))


def bootstrap_signed_session() -> bool:
    """Restore signed session from URL or browser storage before page auth UI renders.

    SIM-SMOKE-02B: a token that just arrived via the `fr_session` URL query
    parameter is a one-time direct-link bootstrap event -- `auth_restored_from_url`
    stays set (and the URL param stays in place) until
    `_finalize_url_bootstrap_handoff()` (called by `render_app_chrome()`)
    confirms the browser has actually persisted it. A token that was
    already read FROM browser localStorage needs no such handoff: it is
    already durable, so `auth_restored_from_url` is never set for that path
    and nothing is ever written back to the URL.
    """
    _auth_smoke_trace(
        "bootstrap_started",
        user_email_present=bool(st.session_state.get("user_email")),
    )
    if st.session_state.get("user_session_expired"):
        st.session_state.pop("_session_restoration_pending", None)
        st.session_state.pop("_session_query_read_failed", None)
        return False
    if st.session_state.get("user_email"):
        # Already authenticated. If a URL-bootstrap handoff is still pending
        # from an earlier run (auth_restored_from_url still set), leave it
        # alone so render_app_chrome() can keep retrying the acknowledged
        # browser write; otherwise there is nothing left to reconcile here.
        if not st.session_state.get("auth_restored_from_url"):
            st.session_state.pop("_session_restoration_pending", None)
            st.session_state.pop("_session_query_read_failed", None)
        return True

    if restore_login_from_signed_url():
        st.session_state.pop("_session_restoration_pending", None)
        st.session_state.pop("_session_query_read_failed", None)
        return True

    if st.session_state.get("_session_query_read_failed"):
        st.session_state.pop("_session_restoration_pending", None)
        return False

    token = _read_browser_session_token_via_js_eval()
    if token is None:
        st.session_state["_session_restoration_pending"] = True
        return False

    st.session_state.pop("_session_restoration_pending", None)
    if not token:
        return False

    payload = verify_signed_session(token)
    if not payload:
        _mark_browser_session_clear_needed()
        return False

    _hydrate_session_from_payload(payload, token)
    _auth_smoke_trace(
        "session_hydrated",
        completed=True,
        user_email_present=bool(st.session_state.get("user_email")),
    )
    # This token was already read FROM localStorage, so it is already
    # durable -- this is not a URL-bootstrap event, and nothing should be
    # written back to the URL or treated as pending browser persistence.
    st.session_state.pop("auth_restored_from_url", None)
    return True


def _finalize_url_bootstrap_handoff() -> None:
    """Explicit, acknowledged browser-storage handoff for a token that just
    arrived via the one-time `fr_session` URL bootstrap parameter (SIM-SMOKE-02B).

    Called by `render_app_chrome()` only when `bootstrap_signed_session()`
    hydrated session_state THIS run from a URL token (`auth_restored_from_url`
    is set). Rendering an async browser-storage write is not itself proof of
    success, so the URL token is removed ONLY once the browser explicitly
    confirms the write:

    - confirmed (True): remove `fr_session` from the URL; the session stays
      hydrated and durable in browser localStorage from here on.
    - pending (None) or failed (False): deliberately KEEP the URL token (it
      is still the only durable copy of this session) and leave
      `auth_restored_from_url` set so the next rerun retries the exact same
      acknowledged write. The already-hydrated session_state is left alone
      either way, so no login/premium denial is shown and no redirect loop
      is created while this resolves.
    """
    token = str(st.session_state.get("signed_session_token") or "").strip()
    if not token:
        st.session_state.pop("auth_restored_from_url", None)
        return

    ack = _write_browser_session_token_via_js_eval(token)
    _auth_smoke_trace(
        "browser_storage_write",
        result={True: "confirmed", False: "failed", None: "pending"}[ack],
    )
    if ack is True:
        _clear_signed_session_query_token()
        st.session_state.pop("auth_restored_from_url", None)
        st.session_state.pop("_session_restoration_pending", None)
        st.session_state.pop("_session_query_read_failed", None)
        _auth_smoke_trace(
            "fr_session_handoff",
            retained=False,
            user_email_present=bool(st.session_state.get("user_email")),
        )
    else:
        # ack is None (pending) or False (failed): keep the URL token and
        # the auth_restored_from_url flag in place; never silently drop the
        # only recoverable copy of the session, and never log/display the
        # token.
        _auth_smoke_trace(
            "fr_session_handoff",
            retained=True,
            user_email_present=bool(st.session_state.get("user_email")),
        )


def persist_login_to_signed_url(profile: Dict[str, Any]) -> None:
    """Establish (or refresh) the authoritative signed session token for a
    freshly authenticated learner.

    SIM-SMOKE-02B: despite the historical name, this NEVER writes `fr_session`
    into the URL. `fr_session` is reserved exclusively for a one-time
    incoming direct-link bootstrap token (see `_finalize_url_bootstrap_handoff()`);
    an ordinary login, admin unlock, or passive profile refresh persists the
    token to `st.session_state` plus (best-effort, acknowledged) browser
    localStorage only.
    """
    payload = _session_payload_from_profile(profile)
    email = payload.get("user_email")
    if not email:
        return

    # Reuse the existing signed token if the user/session payload is unchanged and
    # the token is not close to expiring. This avoids churning browser storage
    # writes on every rerun.
    existing_token = _current_signed_session_token()
    existing_payload = verify_signed_session(existing_token) if existing_token else None
    if existing_payload and _session_payloads_match(existing_payload, payload):
        expires_in = int(existing_payload.get("exp") or 0) - int(time.time())
        if expires_in > SESSION_REFRESH_WINDOW_SECONDS:
            _persist_token_to_browser_only(existing_token)
            return

    token = make_signed_session(payload)
    _persist_token_to_browser_only(token)


def clear_persisted_login() -> None:
    st.session_state.pop("signed_session_token", None)
    _clear_query_param(SESSION_PARAM)
    _mark_browser_session_clear_needed()


def save_logged_in_user(profile: Dict[str, Any], persist: bool = True) -> None:
    email = str(profile.get("email") or profile.get("user_email") or "").strip().lower()
    if not email:
        return
    # Clear the session-expired flag only when it was actually set (real login after
    # timeout), and reset the idle clock at the same time.  Passive profile refreshes
    # called from get_subscription_status() do not set the flag, so they are unaffected.
    if st.session_state.pop("user_session_expired", None):
        st.session_state["last_activity_at"] = time.time()
        st.session_state.pop("_last_activity_stamp_at", None)
    st.session_state["user_email"] = email
    st.session_state["auth_user_id"] = str(profile.get("auth_user_id") or "")
    st.session_state["full_name"] = str(profile.get("full_name") or "")
    st.session_state["preferred_language_code"] = str(profile.get("preferred_language_code") or "en").strip().lower() or "en"
    st.session_state["preferred_timezone"] = str(profile.get("preferred_timezone") or "UTC").strip() or "UTC"
    st.session_state["subscription_status"] = str(profile.get("subscription_status") or "free").strip().lower()
    if persist:
        persist_login_to_signed_url(profile)


def clear_login_and_flush_browser() -> None:
    """Clear Python auth state AND immediately render the localStorage-clearing JS.

    Use this instead of clear_login_state() whenever you need the browser
    localStorage token removed in the same script run (e.g. session timeout).
    clear_login_state() marks localStorage for clearing on the next bridge render;
    this function renders it right away so st.stop() does not skip it.
    """
    clear_login_state()
    _render_browser_session_clearer()


def stamp_activity_to_token() -> None:
    """Re-sign the current session token with the latest last_activity_at timestamp.

    Called by enforce_session_timeout() on every non-expired rerun so that the
    browser-persisted token carries an up-to-date activity timestamp. This is
    what allows idle timeout to survive full browser navigation and hard
    refresh: the token restored from localStorage preserves last_activity_at
    even when session_state is wiped.

    SIM-SMOKE-02B: this deliberately NEVER writes `fr_session` into the URL --
    only the authoritative in-memory token and (best-effort, acknowledged)
    browser localStorage are refreshed. `fr_session` is a one-time direct-link
    bootstrap value only; an ordinary activity-driven token refresh must never
    reintroduce it.
    """
    token = _current_signed_session_token()
    if not token:
        return
    payload = verify_signed_session(token)
    if not payload:
        return
    payload["last_activity_at"] = float(st.session_state.get("last_activity_at") or time.time())
    payload.pop("exp", None)  # make_signed_session sets a fresh 30-day expiry
    new_token = make_signed_session(payload)
    _persist_token_to_browser_only(new_token)


def clear_login_state() -> None:
    for key in [
        "user_email",
        "auth_user_id",
        "full_name",
        "preferred_language_code",
        "preferred_timezone",
        "subscription_status",
        "admin_unlocked",
        "auth_restored_from_url",
        "signed_session_token",
        # Must clear last_activity_at so a stale timestamp cannot immediately
        # re-trigger timeout the moment the user logs back in after expiry.
        "last_activity_at",
        "_last_activity_stamp_at",
        "_session_restoration_pending",
        "_session_query_read_failed",
        # Practice/weak-areas/paid-mock retry-safety ids must not survive a
        # logout or session timeout: a stored id left behind could otherwise
        # be re-verified against a *different* user who logs into the same
        # browser tab afterward. (Ownership verification in
        # resolve_or_create_exam_attempt_id / verify_exam_attempt_ownership
        # already rejects a mismatched owner, but clearing here removes the
        # stale id at its source instead of relying solely on that guard.)
        "practice_exam_attempt_id",
        "weak_exam_attempt_id",
        "current_exam_attempt_id",
    ]:
        st.session_state.pop(key, None)
    clear_persisted_login()


def get_supabase_auth_client():
    url = _secret("SUPABASE_URL")
    anon_key = _secret("SUPABASE_ANON_KEY")
    if not url or not anon_key:
        st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, anon_key)


@st.cache_resource(show_spinner=False)
def get_supabase_admin_client():
    try:
        return create_supabase_admin_client()
    except SupabaseAdminConfigError:
        st.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()


# Compatibility name used across older pages. This is server-side Streamlit; service role is kept centralized.
def get_supabase_client():
    return get_supabase_admin_client()


def get_current_user_email() -> Optional[str]:
    restore_login_from_signed_url()
    email = str(st.session_state.get("user_email") or st.session_state.get("account_email") or "").strip().lower()
    if email and "@" in email:
        return email
    return None


def is_logged_in() -> bool:
    return bool(get_current_user_email())


def get_user_profile(email: Optional[str] = None) -> Optional[Dict[str, Any]]:
    email = str(email or get_current_user_email() or "").strip().lower()
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


def get_subscription_status(email: Optional[str] = None) -> str:
    email = str(email or get_current_user_email() or "").strip().lower()
    if not email:
        return FREE_STATUS
    session_status = str(st.session_state.get("subscription_status") or "").strip().lower()
    profile = get_user_profile(email)
    status = str((profile or {}).get("subscription_status") or session_status or FREE_STATUS).strip().lower()
    if profile:
        # Keep persisted signed session current after DB lookup.
        merged = dict(profile)
        merged["email"] = email
        save_logged_in_user(merged, persist=True)
    return status or FREE_STATUS


def get_user_subscription_status(email: Optional[str] = None) -> str:
    return get_subscription_status(email=email)


def is_admin_user(email: Optional[str] = None) -> bool:
    email = str(email or get_current_user_email() or "").strip().lower()
    return _email_is_configured_admin(email)


def is_admin_unlocked() -> bool:
    return bool(st.session_state.get("admin_unlocked") and is_admin_user())


def get_user_access_level(email: Optional[str] = None) -> str:
    email = email or get_current_user_email()
    if not email:
        return "logged_out"
    if is_admin_unlocked():
        return "admin"
    status = get_subscription_status(email)
    if status in PAID_STATUS_VALUES:
        return "paid"
    if status in EXPIRED_STATUS_VALUES:
        return "expired"
    return "free"


def has_premium_access(email: Optional[str] = None) -> bool:
    return get_user_access_level(email) in {"paid", "admin"}


def is_paid_user(email: Optional[str] = None) -> bool:
    return has_premium_access(email)


def get_preferred_language_code(email: Optional[str] = None) -> str:
    profile = get_user_profile(email)
    if profile:
        return str(profile.get("preferred_language_code") or "en").strip().lower() or "en"
    return str(st.session_state.get("preferred_language_code") or "en").strip().lower() or "en"


def get_preferred_timezone(email: Optional[str] = None) -> str:
    profile = get_user_profile(email)
    if profile:
        return str(profile.get("preferred_timezone") or "UTC").strip() or "UTC"
    return str(st.session_state.get("preferred_timezone") or "UTC").strip() or "UTC"


def require_login() -> str:
    if is_session_restoration_pending():
        _auth_smoke_trace("require_login_check", state="restoration_pending")
        st.info("Restoring your session...")
        st.stop()
    email = get_current_user_email()
    if not email:
        _auth_smoke_trace("require_login_check", state="unauthenticated")
        st.warning("Please log in from the Account page before continuing.")
        st.page_link("pages/Account.py", label="Go to Account", icon="👤")
        st.stop()
    _auth_smoke_trace("require_login_check", state="authenticated")
    return email


def show_locked_premium_message(feature_name: str = "This feature") -> None:
    st.warning(f"{feature_name} is available with Premium Access.")
    st.info("Free users can use Account, Support, and the Free Preview. Upgrade to unlock full exams, practice, progress, and readiness.")


def require_paid_access(feature_name: str = "This feature") -> bool:
    email = require_login()
    if not has_premium_access(email):
        show_locked_premium_message(feature_name)
        st.stop()
    return True


def _current_session_profile_for_persistence() -> Dict[str, Any]:
    return {
        "email": str(st.session_state.get("user_email") or "").strip().lower(),
        "auth_user_id": str(st.session_state.get("auth_user_id") or ""),
        "full_name": str(st.session_state.get("full_name") or ""),
        "preferred_language_code": str(st.session_state.get("preferred_language_code") or "en").strip().lower() or "en",
        "preferred_timezone": str(st.session_state.get("preferred_timezone") or "UTC").strip() or "UTC",
        "subscription_status": str(st.session_state.get("subscription_status") or "free").strip().lower(),
    }


def unlock_admin(password: str) -> bool:
    if not is_admin_user():
        return False
    expected = _secret("ADMIN_PASSWORD")
    if expected and hmac.compare_digest(str(password or ""), expected):
        st.session_state["admin_unlocked"] = True
        persist_login_to_signed_url(_current_session_profile_for_persistence())
        return True
    return False


def require_admin() -> bool:
    email = require_login()
    if not is_admin_user(email):
        st.error("Admin access required.")
        st.stop()
    if not is_admin_unlocked():
        st.warning("Admin password required before accessing this page.")
        st.page_link("pages/Account.py", label="Go to Account / Admin Unlock", icon="👤")
        st.stop()
    return True


def _hide_native_sidebar_nav_css() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stSidebarNav"] {display: none !important;}
        section[data-testid="stSidebar"] nav {display: none !important;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _streamlit_route_for_page(page_path: str) -> str:
    """Return Streamlit multipage route path for a script path."""
    from utils.navigation import streamlit_route_for_page

    return streamlit_route_for_page(page_path)


def _clean_nav_href(page_path: str, *, extra_params: Optional[Dict[str, str]] = None) -> str:
    """Build a clean internal navigation URL.

    SIM-SMOKE-02B: `fr_session` is a one-time direct-link bootstrap value
    only -- ordinary authenticated navigation (sidebar links, page links)
    must never carry it. The destination page instead restores the
    authenticated session from browser localStorage via
    `bootstrap_signed_session()`, showing a brief "Restoring your
    session..." state (see `is_session_restoration_pending()`) rather than a
    login denial while that resolves. Unrelated parameters (e.g.
    `completed_attempt`) are still preserved via `extra_params`.
    """
    from utils.navigation import build_nav_href

    return build_nav_href(page_path, extra_params=extra_params)


def _sidebar_nav_link(
    page_path: str,
    label: str,
    icon: str = "",
    *,
    is_active: bool = False,
) -> None:
    """Render a clean sidebar link (no `fr_session`) -- see `_clean_nav_href`."""
    href = _clean_nav_href(page_path)
    safe_label = str(label).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_icon = str(icon).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    active_class = " cb-nav-link-active" if is_active else ""
    current_attr = ' aria-current="page"' if is_active else ""
    st.markdown(
        f"""
        <a href="{href}" target="_self" class="cb-nav-link{active_class}"{current_attr}
           style="color: inherit; text-decoration: none;">
            {safe_icon} {safe_label}
        </a>
        """,
        unsafe_allow_html=True,
    )


def render_session_page_link(
    page_path: str,
    label: str,
    icon: str = "",
    *,
    extra_params: Optional[Dict[str, str]] = None,
) -> None:
    """Render a clean authenticated page link (no `fr_session`) -- see
    `_clean_nav_href`.

    Do not use st.page_link for authenticated navigation because it drops
    unrelated query parameters (e.g. `completed_attempt`) that some
    destination pages rely on; this anchor preserves those via
    `extra_params` while never carrying the one-time session bootstrap
    token.
    """
    href = _clean_nav_href(page_path, extra_params=extra_params)
    safe_label = str(label).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_icon = str(icon).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    st.markdown(
        f"""
        <a href="{href}" target="_self" class="cb-nav-link"
           style="display: inline-block; padding: 0.45rem 0.70rem; margin: 0.15rem 0;
                  text-decoration: none; color: inherit; border: 1px solid rgba(49, 51, 63, 0.20);
                  border-radius: 0.45rem;">
            {safe_icon} {safe_label}
        </a>
        """,
        unsafe_allow_html=True,
    )


def _render_navigation_group(
    title: str,
    routes: Sequence[Any],
    *,
    current_page_path: str,
) -> None:
    from utils.navigation import is_route_active

    if not routes:
        return
    st.markdown(f'<div class="cb-nav-section">{title}</div>', unsafe_allow_html=True)
    for route in routes:
        _sidebar_nav_link(
            route.page_path,
            route.label,
            route.icon,
            is_active=is_route_active(route, current_page_path),
        )


def render_sidebar_navigation() -> None:
    from utils.navigation import (
        admin_routes,
        detect_current_page_path,
        is_route_visible,
        legal_routes,
        practice_routes,
        primary_learner_routes,
    )

    restore_login_from_signed_url()
    _hide_native_sidebar_nav_css()
    email = get_current_user_email()
    level = get_user_access_level(email) if email else "logged_out"
    admin_email = is_admin_user(email)
    admin_unlocked = is_admin_unlocked()
    current_page_path = detect_current_page_path()

    with st.sidebar:
        st.markdown('<div class="cb-shell-brand">CertBound</div>', unsafe_allow_html=True)
        if email:
            st.markdown(
                f'<div class="cb-shell-caption">Signed in: {email}<br/>Access: {level}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown('<div class="cb-shell-caption">Not signed in</div>', unsafe_allow_html=True)

        primary = [
            route
            for route in primary_learner_routes()
            if is_route_visible(
                route,
                access_level=level,
                is_admin_email=admin_email,
                admin_unlocked=admin_unlocked,
            )
        ]
        _render_navigation_group("Learner", primary, current_page_path=current_page_path)

        practice = [
            route
            for route in practice_routes()
            if is_route_visible(
                route,
                access_level=level,
                is_admin_email=admin_email,
                admin_unlocked=admin_unlocked,
            )
        ]
        if practice:
            st.divider()
            _render_navigation_group("Practice destinations", practice, current_page_path=current_page_path)
            if level not in {"paid", "admin"}:
                st.caption("Premium access required")

        if admin_email:
            st.divider()
            if not admin_unlocked:
                _sidebar_nav_link("pages/Account.py", "Admin Unlock", "🔐", is_active=False)
            else:
                admin_visible = [
                    route
                    for route in admin_routes()
                    if is_route_visible(
                        route,
                        access_level=level,
                        is_admin_email=admin_email,
                        admin_unlocked=admin_unlocked,
                    )
                ]
                _render_navigation_group("Admin", admin_visible, current_page_path=current_page_path)

        st.divider()
        legal_visible = [
            route
            for route in legal_routes()
            if is_route_visible(
                route,
                access_level=level,
                is_admin_email=admin_email,
                admin_unlocked=admin_unlocked,
            )
        ]
        _render_navigation_group("Legal", legal_visible, current_page_path=current_page_path)


def render_public_chrome() -> None:
    """Public-safe shared shell for legal and password-recovery pages."""
    from utils.dashboard_components import inject_shell_theme

    inject_shell_theme()
    render_sidebar_navigation()


def render_app_chrome() -> None:
    """Establish the authenticated session, then render the shared shell.

    SIM-SMOKE-02B ordering contract for a URL-bootstrapped token:
    read exactly one `fr_session` -> verify -> hydrate session state
    (`bootstrap_signed_session()`) -> attempt an acknowledged browser
    localStorage write -> only once that write is confirmed, remove
    `fr_session` from the URL (`_finalize_url_bootstrap_handoff()`) ->
    continue normal authenticated execution. Ordinary reruns, activity
    stamping, and navigation never write `fr_session` back into the URL.
    """
    from utils.dashboard_components import inject_shell_theme

    _auth_smoke_trace("render_app_chrome_started")
    try:
        from utils.sentry_config import init_sentry  # noqa: PLC0415
        init_sentry()
    except Exception:
        pass
    bootstrap_signed_session()
    _render_pending_browser_storage_clear_if_needed()
    if st.session_state.get("auth_restored_from_url"):
        _finalize_url_bootstrap_handoff()
    inject_shell_theme()
    render_sidebar_navigation()
