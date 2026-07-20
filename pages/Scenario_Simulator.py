"""SIM-VSLICE-01 / SIM-VSLICE-01B / SIM-VSLICE-01C: BA-201 Scenario
Simulator learner vertical slice.

Temporary/development page, gated by the existing
`CERTBOUND_ENABLE_SCENARIO_SIMULATOR` feature flag and, in enforcement (not
just navigation-visibility) terms, by the same `requires_premium=True`
already declared for this route in `utils/navigation.py`.

Enforcement order (SIM-VSLICE-01C): a single call to the existing,
CENTRALIZED `utils.access_control.require_paid_access(...)` helper --
which itself owns and already tests the authentication-then-premium
sequence (`require_login()` then `has_premium_access()`, showing the
existing `show_locked_premium_message(...)` upgrade message for a
logged-in-but-non-premium learner) -- followed by a single, fresh read of
the verified session email via `get_current_user_email()`. This page never
reproduces `require_paid_access(...)`'s internals, never calls
`require_login()` a second time, and never compares two separate identity
reads against each other. All of this happens BEFORE any scenario content
is loaded, any `scenario_versions` row is resolved, any Supabase admin
client is created for scenario work, or `start_or_resume_scenario_attempt_v1`
is called. Navigation-registry hiddenness alone is never treated as
authorization: a logged-in, non-premium learner who reaches this exact URL
directly is stopped here, not merely kept out of the sidebar.

This page stops before decision submission -- option labels are rendered as
disabled controls only; no option here ever mutates a persisted attempt.

All persistence, catalog resolution, and runtime restoration is delegated to
`utils.scenario_learner_controller`; this page never calls
`utils.scenario_persistence` directly and never inspects a serialized engine
snapshot itself.
"""

from __future__ import annotations

import streamlit as st

from utils.access_control import (
    get_current_user_email,
    render_app_chrome,
    require_paid_access,
)
from utils.dashboard_components import inject_certbound_theme, render_empty_state, render_page_header
from utils.navigation import CERTBOUND_ENABLE_SCENARIO_SIMULATOR, is_feature_flag_enabled
from utils.scenario_learner_controller import (
    ScenarioLearnerAccessError,
    ScenarioLearnerBackendError,
    ScenarioLearnerContentError,
    ScenarioLearnerError,
    ScenarioLearnerStateError,
    ScenarioLearnerVersionUnavailableError,
    start_or_resume_ba201_attempt,
)
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.user_errors import log_and_get_user_message

st.set_page_config(page_title="Scenario Simulator", page_icon="🧪", layout="wide", initial_sidebar_state="expanded")

# Defense in depth: NAV_GROUP_HIDDEN already keeps this route out of the
# sidebar, but a direct URL visit would otherwise reach this page even while
# the feature is disabled -- self-gate on the same existing flag.
if not is_feature_flag_enabled(CERTBOUND_ENABLE_SCENARIO_SIMULATOR):
    render_app_chrome()
    st.info("The Scenario Simulator is not available yet.")
    st.stop()

render_app_chrome()
enforce_session_timeout()
show_session_expired_notice()
inject_certbound_theme()

SAFE_UNAVAILABLE_MESSAGE = "The Scenario Simulator is temporarily unavailable. Please try again shortly."


def _render_unavailable(message: str) -> None:
    render_empty_state(
        "Scenario unavailable",
        message,
        action_label="Return to Practice",
        action_href="pages/Practice.py",
    )


def _require_premium_learner_email() -> str:
    """Enforce authentication, then premium entitlement, using the single
    existing CENTRALIZED access-control helper -- before this module does
    anything else (no scenario content load, no `scenario_versions`
    resolution, no Supabase admin client creation, no V68 RPC call happens
    before this function returns).

    `require_paid_access(...)` already owns the entire
    authenticate-then-check-premium sequence (including which login prompt
    or upgrade message to show, and calling `st.stop()`) -- this function
    never reimplements any part of that. Once it returns, the verified
    session email is read exactly once via `get_current_user_email()`; that
    single read is the SOLE ownership identity used anywhere below.
    """
    require_paid_access("Scenario Simulator")
    email = get_current_user_email()
    if not email:
        # Defensive only: require_paid_access() already guarantees a
        # verified, premium session at this point, so this should be
        # unreachable in practice -- but never proceed without a verified
        # email even if that contract ever changes.
        st.warning("Please log in again to continue.")
        st.stop()
    return email


user_email = _require_premium_learner_email()

render_page_header(
    "Scenario Simulator",
    description="A temporary, in-development learner scenario preview. Options are not yet submittable.",
    badge="Development preview",
    certification_name="Salesforce Certified Business Analyst",
)

try:
    attempt_view = start_or_resume_ba201_attempt(user_email)
except ScenarioLearnerAccessError as exc:
    message = log_and_get_user_message(
        "Scenario Simulator: missing/invalid learner email at controller boundary",
        "Please log in again to continue.",
        exc=exc,
    )
    st.warning(message)
    st.stop()
except ScenarioLearnerContentError as exc:
    message = log_and_get_user_message(
        "Scenario Simulator: scenario catalog/content load failure",
        SAFE_UNAVAILABLE_MESSAGE,
        exc=exc,
    )
    _render_unavailable(message)
    st.stop()
except ScenarioLearnerVersionUnavailableError as exc:
    message = log_and_get_user_message(
        "Scenario Simulator: scenario version unavailable or not published",
        SAFE_UNAVAILABLE_MESSAGE,
        exc=exc,
    )
    _render_unavailable(message)
    st.stop()
except ScenarioLearnerStateError as exc:
    message = log_and_get_user_message(
        "Scenario Simulator: persisted engine state failed restoration",
        SAFE_UNAVAILABLE_MESSAGE,
        exc=exc,
    )
    _render_unavailable(message)
    st.stop()
except (ScenarioLearnerBackendError, ScenarioLearnerError) as exc:
    message = log_and_get_user_message(
        "Scenario Simulator: start/resume backend failure",
        SAFE_UNAVAILABLE_MESSAGE,
        exc=exc,
    )
    _render_unavailable(message)
    st.stop()

st.markdown(f"### {attempt_view.scenario_title}")
st.caption(attempt_view.progress_label)

if attempt_view.is_complete or attempt_view.current_scene is None:
    render_empty_state(
        "Scenario complete",
        "You've reached the end of this scenario preview. Decision submission and full results are not part of "
        "this development preview yet.",
        action_label="Return to Practice",
        action_href="pages/Practice.py",
    )
else:
    scene = attempt_view.current_scene
    st.markdown(f"**Domain:** {scene.domain_label}")
    st.write(scene.narrative)
    st.markdown(f"**{scene.decision_prompt}**")
    for option in scene.options:
        st.button(option.label, key=f"scenario_option_{option.option_id}", disabled=True)
    st.caption("Decision submission is not available in this preview.")

st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
