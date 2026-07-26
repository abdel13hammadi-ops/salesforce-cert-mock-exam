"""
CertBound session timeout helper.

MVP rule:
- Non-exam pages expire after 30 minutes of inactivity.
- Active exam pages can opt out because the exam countdown timer is the authority.

True logout means:
- Streamlit session auth keys cleared (via clear_login_state)
- Signed URL fr_session query param cleared (via clear_persisted_login → _clear_query_param)
- Browser localStorage session token cleared on the next render (via _render_browser_session_bridge
  consuming the clear_browser_session_storage flag set by clear_login_state)

On expiry the page calls st.rerun() (not st.stop()) so that the sidebar is immediately
rebuilt without the stale fr_session token.  Raw HTML <a> sidebar links are full-browser
navigations; if we called st.stop() instead, the already-rendered sidebar hrefs would
carry the old token to the next page and re-authenticate the user, causing the timeout
to fire again on every page the user tries to navigate to.
"""

from __future__ import annotations

import time
import streamlit as st

SESSION_TIMEOUT_VERSION = "SESSION_TIMEOUT_V5_RERUN_ON_EXPIRY"

DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60

# Maximum frequency at which the session token is re-signed with a new timestamp.
# The effective throttle is capped to half the active timeout so the token is
# always fresher than the expiry window regardless of the timeout value chosen.
# Examples: timeout=30*60 → throttle=60 s; timeout=20 → throttle=10 s.
_STAMP_THROTTLE_SECONDS = 60

_NOTICE = "Your session expired after 30 minutes of inactivity. Please sign in again."


def _should_stamp(now: float, throttle: float) -> bool:
    """Return True if enough time has passed since the last token stamp."""
    last = st.session_state.get("_last_activity_stamp_at")
    return last is None or (now - float(last)) >= throttle


def _record_stamp(now: float) -> None:
    st.session_state["_last_activity_stamp_at"] = now


def _classify_activity_state(last_activity_at, timeout_seconds: int, now: float) -> str:
    """Pure classification helper used only to feed the SIM-SMOKE-02H
    auth-smoke diagnostics marker below -- never changes control flow.
    Returns one of the fixed enum values `_auth_smoke_trace` accepts for the
    `timeout_activity_check` event."""
    if last_activity_at is None:
        return "no_timestamp"
    try:
        idle_seconds = now - float(last_activity_at)
    except (TypeError, ValueError):
        return "invalid"
    return "stale" if idle_seconds > timeout_seconds else "fresh"


def enforce_session_timeout(
    *,
    timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
    exempt_active_exam: bool = False,
) -> bool:
    """Enforce idle session timeout.

    Returns True when the session is still active (or exempted).
    On expiry: clears auth state, marks browser localStorage for clearing, then calls
    st.rerun() so the sidebar is immediately rebuilt without the stale fr_session token.
    The expired notice is stored in session_state and displayed on the rerun by
    show_session_expired_notice().

    Activity is defined as a Streamlit page run caused by user interaction.
    Pass exempt_active_exam=True while an exam is actively running so the
    exam countdown timer remains the authority for the session lifecycle.
    """
    # Avoid circular import at module load time.
    from utils.access_control import _auth_smoke_trace, clear_login_state, stamp_activity_to_token

    now = time.time()

    # Adaptive throttle: always at most half the timeout so the token is always
    # fresher than the expiry window (e.g. 10 s for 20 s test, 60 s for 30 min prod).
    effective_throttle = min(_STAMP_THROTTLE_SECONDS, max(1, timeout_seconds // 2))

    # Exam is active — reset the clock, but only re-sign the token at most once
    # per effective_throttle to avoid per-second URL churn during autorefresh.
    if exempt_active_exam:
        st.session_state["last_activity_at"] = now
        if _should_stamp(now, effective_throttle):
            stamp_activity_to_token()
            _record_stamp(now)
        return True

    # The session was already expired in a prior rerun; the user is logged out.
    # Do not loop by firing the expiry logic again. save_logged_in_user() clears
    # this flag when the user successfully logs back in.
    if st.session_state.get("user_session_expired"):
        return True

    # last_activity_at is restored from the signed URL token by
    # restore_login_from_signed_url() (called inside render_app_chrome()) so it
    # survives full browser navigation even when session_state is wiped.
    last_activity_at = st.session_state.get("last_activity_at")
    _auth_smoke_trace(
        "timeout_activity_check",
        state=_classify_activity_state(last_activity_at, timeout_seconds, now),
    )

    if last_activity_at is not None:
        idle_seconds = now - float(last_activity_at)
        if idle_seconds > timeout_seconds:
            # Clear Python auth state and URL token.  Sets the
            # clear_browser_session_storage flag so _render_browser_session_bridge()
            # clears localStorage on the next render (the st.rerun() below).
            clear_login_state()
            _auth_smoke_trace("timeout_cleanup", ran=True)

            # Mark expiry so restore_login_from_signed_url() cannot re-authenticate
            # during the same Streamlit session.
            st.session_state["user_session_expired"] = True
            # Store the notice for show_session_expired_notice() to display on the rerun.
            st.session_state["session_expired_notice"] = _NOTICE

            # Rerun instead of st.stop() so the sidebar is rebuilt immediately without
            # the stale fr_session token.  Sidebar links are raw HTML <a> tags that
            # cause full browser navigations; calling st.stop() would leave the
            # already-rendered hrefs carrying the old token, which re-authenticates the
            # user on the next page and re-fires the timeout in a loop.
            st.rerun()
            return False

    # Not expired — update the timestamp in session_state and re-sign the URL
    # token with the fresh time so the next navigation carries it forward.
    _auth_smoke_trace("timeout_cleanup", ran=False)
    st.session_state["last_activity_at"] = now
    if _should_stamp(now, effective_throttle):
        stamp_activity_to_token()
        _record_stamp(now)
    return True


def show_session_expired_notice() -> None:
    """Render and clear any pending session-expired notice.

    Call this immediately after enforce_session_timeout() on every page so that
    the notice persists across the first rerun after logout.
    """
    notice = st.session_state.pop("session_expired_notice", None)
    if notice:
        st.warning(notice)
