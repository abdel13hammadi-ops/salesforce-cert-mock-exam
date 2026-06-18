"""
CertBound session timeout helper.

MVP rule:
- Non-exam pages expire after 30 minutes of inactivity.
- Active exam pages can opt out because the exam countdown timer is the authority.
"""

from __future__ import annotations

import time
import streamlit as st

SESSION_TIMEOUT_VERSION = "SESSION_TIMEOUT_V1_30_MIN_IDLE"

DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60


def enforce_session_timeout(
    *,
    timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
    exempt_active_exam: bool = False,
) -> bool:
    """Enforce idle session timeout.

    Returns True when the session is still active.
    Returns False after clearing session state and rendering an expiry message.

    Activity is defined as a Streamlit page run caused by user interaction/navigation.
    For active exams, pass exempt_active_exam=True so the exam timer remains responsible.
    """

    now = time.time()

    if exempt_active_exam:
        st.session_state["last_activity_at"] = now
        return True

    last_activity_at = st.session_state.get("last_activity_at")

    if last_activity_at is not None:
        idle_seconds = now - float(last_activity_at)
        if idle_seconds > timeout_seconds:
            preserve_keys = {
                "session_expired_notice",
            }
            preserved = {
                key: st.session_state.get(key)
                for key in preserve_keys
                if key in st.session_state
            }

            st.session_state.clear()
            st.session_state.update(preserved)
            st.session_state["session_expired_notice"] = (
                "Your session expired after 30 minutes of inactivity. Please sign in again."
            )

            st.warning(st.session_state["session_expired_notice"])
            st.stop()
            return False

    st.session_state["last_activity_at"] = now
    return True


def show_session_expired_notice() -> None:
    """Render and clear any pending session-expired notice."""
    notice = st.session_state.pop("session_expired_notice", None)
    if notice:
        st.warning(notice)
