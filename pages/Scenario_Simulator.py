"""SIM-VSLICE-01 / SIM-VSLICE-01B / SIM-VSLICE-01C / SIM-VSLICE-02 /
SIM-VSLICE-02A / SIM-VSLICE-02B: BA-201 Scenario Simulator learner vertical
slice.

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
client is created for scenario work, or any V68 RPC is called.
Navigation-registry hiddenness alone is never treated as authorization: a
logged-in, non-premium learner who reaches this exact URL directly is
stopped here, not merely kept out of the sidebar.

Decision submission and idempotency (SIM-VSLICE-02A / SIM-VSLICE-02B)
------------------------------------------------------------------------
Decision processing is a two-stage, retry-safe pipeline (see
`utils.scenario_learner_controller`'s own "Decision submission" module
docstring section for the full rationale):

1. `prepare_ba201_decision(...)` is called exactly once per INTENTIONAL
   learner submission (i.e. only when the learner presses "Submit
   Decision" while no submission is already pending). It mints exactly one
   UUIDv4 idempotency key, resolves "what is true right now", and returns
   an immutable, deeply-frozen, pickle-serializable `PreparedScenarioDecision`
   (every JSON request payload is bound as an already-canonicalized string,
   never as a mutable dict/list -- see that class's own docstring).
2. That object is stored in `st.session_state[_PENDING_DECISION_STATE_KEY]`
   BEFORE `submit_prepared_ba201_decision(...)` is ever called -- so if the
   result of that call is uncertain (`ScenarioLearnerBackendError`), the
   EXACT SAME prepared request is already safely recorded and is reused
   verbatim by a Streamlit rerun or an explicit "Retry submission" click,
   with NOTHING recomputed (not the expected sequence number, not the
   expected scene, not the before/after state, not any terminal field).

This page therefore ALWAYS checks for a pending prepared submission BEFORE
calling `start_or_resume_ba201_attempt(...)` -- calling start/resume first
could otherwise create (or select) a different attempt while an uncertain
submission for the original attempt is still unresolved, and
`start_or_resume_scenario_attempt_v1` never resumes a completed/abandoned
attempt, so calling it after an uncertain TERMINAL submission would create
an entirely new replacement attempt instead of ever learning the true
outcome of the original one.

The pending prepared request is cleared only after
`submit_prepared_ba201_decision(...)` returns a conclusive outcome
(a confirmed `ScenarioDecisionPersistenceOutcome`, or a definite rejection);
an uncertain backend/integrity failure deliberately leaves it in place. See
`_submit_prepared_decision(...)`'s own docstring for the full
outcome-to-action mapping, and `_start_new_decision(...)`'s own docstring
for why a PREPARATION failure (steps before any V68 write is ever
attempted) never leaves anything pending at all.

SIM-VSLICE-02C: `_get_pending_prepared_decision(...)` also enforces
ownership -- a pending `PreparedScenarioDecision` whose `normalized_email`
does not match the CURRENT verified learner email is cleared immediately
and never shown as "Retry submission", and is never passed to
`submit_prepared_ba201_decision(...)`. This is display-layer defense in
depth on top of that function's own identity check (`ScenarioLearnerAccessError`
for a mismatched `user_email`), which remains unchanged.

While a submission is pending, the normal option-selection form is
replaced by a single "Retry submission" control, and the current scene is
not re-rendered from a freshly-fetched attempt (see above) -- this page
never shows two active submission controls for the same scene, and never
relies on merely disabling a button as its idempotency mechanism (the
idempotency key itself, bound inside the immutable prepared request, is
what makes a repeated call safe).

Persistence confirmation vs. view reconstruction (SIM-VSLICE-02B)
--------------------------------------------------------------------
`submit_prepared_ba201_decision(...)` deliberately returns only a small
`ScenarioDecisionPersistenceOutcome` -- NOT a renderable scene -- because it
deliberately never reloads scenario content (see that function's own
docstring: a transient local content problem must never be able to block a
retry from reaching V68). This page therefore never tries to render a scene
directly from that outcome:

- A CONFIRMED NONTERMINAL outcome clears the pending request and reruns;
  the NEXT script pass falls through to the normal
  `start_or_resume_ba201_attempt(...)` call below, which legitimately
  reloads content and renders the authoritative advanced attempt.
- A CONFIRMED TERMINAL outcome clears the pending request, stores a small,
  immutable, EMAIL-BOUND completion marker
  (`st.session_state[_COMPLETED_ATTEMPT_STATE_KEY]`), and reruns; the NEXT
  script pass loads and renders the full persisted results experience (see
  "Completion results" below) directly from that marker's `attempt_id`, and
  deliberately never calls `start_or_resume_ba201_attempt(...)` again (that
  RPC never resumes a completed attempt, so calling it here would silently
  create a brand-new replacement BA-201 attempt instead of leaving the
  learner's completed attempt as this session's terminal state). The
  marker is checked against the CURRENT verified learner email on every
  read (`_get_completed_marker(...)`); a marker left over for a different
  learner is discarded rather than shown.

Completion results (SIM-VSLICE-03)
-----------------------------------
The completion marker is transient navigation/session coordination ONLY --
it identifies WHICH `attempt_id` to load a result for, it is never itself
the result authority, and none of its fields are ever rendered directly.
Every script pass that finds a marker calls
`utils.scenario_learner_controller.load_ba201_completion_result(...)`
fresh, which re-fetches and independently re-validates the persisted
attempt from V68 (see that function's own docstring for the full
validation chain, including resolving content via the attempt's PINNED
`scenario_version_id`, never whichever version happens to be current) and
returns a small, immutable `ScenarioCompletionResultView` -- this page
renders ONLY that view's fields, never a raw snapshot, backend identifier,
or session-state value.

Marker-clearing vs. marker-preserving on a completion-result failure:

- `ScenarioLearnerAttemptNotFoundError` / `ScenarioLearnerAttemptNotCompletedError`
  mean the marker's claim is simply WRONG (a missing/foreign attempt, or
  one that is actually still in-progress/abandoned) -- the marker is
  cleared immediately and this same script pass falls through to the
  normal pending-decision / `start_or_resume_ba201_attempt(...)` flow
  below, exactly as if no marker had ever been stored.
- Every other `ScenarioLearnerError` (a pinned version temporarily
  unavailable, a malformed persisted terminal state, or an uncertain
  backend/network failure) is treated as a TEMPORARY rendering problem,
  not proof the marker is wrong -- the marker is deliberately left in
  place and a safe "temporarily unavailable" state is shown instead,
  never falling through to `start_or_resume_ba201_attempt(...)` (which
  would otherwise silently create a brand-new replacement attempt for an
  attempt that may well still be completed).

All persistence, catalog resolution, runtime restoration, decision
validation, scoring, and scene transition are delegated entirely to
`utils.scenario_learner_controller` (which itself delegates scoring/graph
logic to `utils.scenario_engine` and persistence to
`utils.scenario_persistence`); this page never calls either of those two
modules directly, never inspects a serialized engine snapshot itself, and
never renders a backend identifier (attempt id, idempotency key, sequence
number, content hash, etc.) to the learner.
"""

from __future__ import annotations

import uuid
from typing import Optional

import streamlit as st

from utils.access_control import (
    get_current_user_email,
    render_app_chrome,
    require_paid_access,
)
from utils.dashboard_components import inject_certbound_theme, render_empty_state, render_page_header
from utils.navigation import CERTBOUND_ENABLE_SCENARIO_SIMULATOR, is_feature_flag_enabled
from utils.scenario_learner_controller import (
    PreparedScenarioDecision,
    ScenarioAttemptCompletionMarker,
    ScenarioCompletionResultView,
    ScenarioDecisionPersistenceOutcome,
    ScenarioLearnerAccessError,
    ScenarioLearnerAttemptNotActiveError,
    ScenarioLearnerAttemptNotCompletedError,
    ScenarioLearnerAttemptNotFoundError,
    ScenarioLearnerBackendError,
    ScenarioLearnerConflictError,
    ScenarioLearnerContentError,
    ScenarioLearnerError,
    ScenarioLearnerInvalidOptionError,
    ScenarioLearnerStateError,
    ScenarioLearnerVersionUnavailableError,
    load_ba201_completion_result,
    prepare_ba201_decision,
    start_or_resume_ba201_attempt,
    submit_prepared_ba201_decision,
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

# SIM-VSLICE-02A/02B: transient UI coordination only, never an authoritative
# source of attempt state -- see module docstring's "Decision submission and
# idempotency" / "Persistence confirmation vs. view reconstruction" sections.
_PENDING_DECISION_STATE_KEY = "ba201_pending_decision"
_COMPLETED_ATTEMPT_STATE_KEY = "ba201_completed_attempt"


def _render_unavailable(message: str) -> None:
    render_empty_state(
        "Scenario unavailable",
        message,
        action_label="Return to Practice",
        action_href="pages/Practice.py",
    )


def _render_completion_result(view: ScenarioCompletionResultView) -> None:
    """SIM-VSLICE-03: render the persisted, validated results of the
    learner's completed BA-201 attempt.

    Every value rendered here comes directly from `view` -- never from
    `st.session_state`, never recomputed on this page, and never a raw
    backend identifier, snapshot, or hash (see
    `ScenarioCompletionResultView`'s own docstring for the full field
    list/rationale). A score/percentage/domain row is rendered only when
    the corresponding `view` field is not `None` -- nothing here invents a
    number the controller did not already supply. The only navigation
    control offered is a single, safe "Return to Practice" link; no
    restart/new-attempt control is offered (a restart workflow is
    explicitly out of scope for this task).
    """
    st.markdown(f"### {view.completion_heading}")
    st.caption(f"{view.scenario_title} · {view.certification_exam_name}")

    st.markdown(f"**{view.ending_title}**")
    st.write(view.ending_narrative)

    if view.decisions_correct is not None and view.decisions_total is not None:
        summary = f"{view.decisions_correct} of {view.decisions_total} decisions scored as correct"
        if view.accuracy_percentage is not None:
            summary += f" ({view.accuracy_percentage:.0f}%)"
        st.write(summary)

    if view.domain_breakdown:
        st.markdown("**Domain performance**")
        for domain in view.domain_breakdown:
            if domain.accuracy_percentage is not None:
                st.write(
                    f"- {domain.domain_label}: {domain.correct_count} of "
                    f"{domain.total_count} correct ({domain.accuracy_percentage:.0f}%)"
                )
            else:
                st.write(f"- {domain.domain_label}: no decisions recorded")

    if view.recommended_review_domains:
        st.markdown("**Recommended review areas**")
        for domain_label in view.recommended_review_domains:
            st.write(f"- {domain_label}")

    st.page_link("pages/Practice.py", label="Return to Practice", icon="📚")


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


def _normalize_email_for_ownership_check(email: Optional[str]) -> str:
    """A trivial, purely local case/whitespace fold used ONLY to compare
    the current verified session email against a stored
    `ScenarioAttemptCompletionMarker.normalized_email` or
    `PreparedScenarioDecision.normalized_email` -- this is display-layer
    ownership bookkeeping, never a re-implementation of any
    `utils.scenario_persistence` validation, and this page still never
    imports that module directly."""
    return str(email or "").strip().lower()


def _clear_pending_decision() -> None:
    st.session_state.pop(_PENDING_DECISION_STATE_KEY, None)


def _get_pending_prepared_decision(user_email: str) -> Optional[PreparedScenarioDecision]:
    """Return the in-flight `PreparedScenarioDecision` for THIS learner, or
    `None`.

    Any value that is not actually a `PreparedScenarioDecision` instance --
    corrupt/obsolete session state, e.g. left over from a previous version
    of this page, or from a bug elsewhere -- is discarded outright rather
    than partially trusted. This never raises `KeyError` and never inspects
    (let alone renders) whatever the stored value actually was.

    SIM-VSLICE-02C: PLUS an ownership check, using the exact same local
    display-layer email fold already used by `_get_completed_marker(...)` --
    a pending request whose `normalized_email` does not match the CURRENT
    verified learner is cleared IMMEDIATELY and never shown as "Retry
    submission", and never passed to `submit_prepared_ba201_decision(...)`.
    `submit_prepared_ba201_decision(...)` itself already rejects a
    mismatched identity (`ScenarioLearnerAccessError`) -- that check remains
    unchanged as defense in depth -- but this page must never even display
    or attempt a retry for another learner's pending request in the first
    place (e.g. a shared/reused `st.session_state` across sessions).
    """
    pending = st.session_state.get(_PENDING_DECISION_STATE_KEY)
    if not isinstance(pending, PreparedScenarioDecision):
        if pending is not None:
            _clear_pending_decision()
        return None
    if pending.normalized_email != _normalize_email_for_ownership_check(user_email):
        _clear_pending_decision()
        return None
    return pending


def _clear_completed_marker() -> None:
    st.session_state.pop(_COMPLETED_ATTEMPT_STATE_KEY, None)


def _get_completed_marker(user_email: str) -> Optional[ScenarioAttemptCompletionMarker]:
    """Return the stored `ScenarioAttemptCompletionMarker` for THIS learner, or
    `None` -- with the same safe, type-checked handling of corrupt/obsolete
    session state as `_get_pending_prepared_decision(...)`, PLUS an
    ownership check: a marker that does not belong to the CURRENT verified
    learner email is discarded (never shown) rather than trusted."""
    marker = st.session_state.get(_COMPLETED_ATTEMPT_STATE_KEY)
    if not isinstance(marker, ScenarioAttemptCompletionMarker):
        if marker is not None:
            _clear_completed_marker()
        return None
    if marker.normalized_email != _normalize_email_for_ownership_check(user_email):
        # Stale marker belonging to a different currently-authenticated
        # learner (e.g. a different session reusing this session-state
        # slot) -- never shown, and cleared so it cannot leak further.
        _clear_completed_marker()
        return None
    return marker


def _submit_prepared_decision(user_email: str, prepared: PreparedScenarioDecision) -> None:
    """Submit (or safely replay) exactly one already-prepared decision and
    resolve `st.session_state` according to whether the outcome was
    conclusive or uncertain.

    `ScenarioLearnerBackendError` is the ONE outcome treated as uncertain
    (SIM-VSLICE-02B: this now also covers a successful RPC call whose
    persisted response could not be confirmed to match `prepared` -- see
    `submit_prepared_ba201_decision(...)`'s own docstring) -- `prepared` is
    deliberately left in `st.session_state` UNCHANGED so an explicit retry
    (the "Retry submission" control rendered below) resends the EXACT same
    request, with nothing recomputed, potentially reaching V68's own stable
    idempotent-replay path even if the underlying attempt has, in the
    meantime, already advanced past (a committed but unacknowledged
    nonterminal decision) or completed at (a committed but unacknowledged
    terminal decision) the exact state this request was originally
    prepared against. Every other exception is a conclusive rejection (or,
    for the success case, a conclusive success), safe to clear pending
    state for.

    On a CONFIRMED outcome, this function deliberately does NOT try to
    render a scene itself (SIM-VSLICE-02B: `submit_prepared_ba201_decision(...)`
    never reloads scenario content, so its outcome never carries a
    renderable scene) -- it only updates `st.session_state` and reruns; the
    NEXT script pass renders the result (either via a fresh
    `start_or_resume_ba201_attempt(...)` call for a nonterminal outcome, or
    via the stored completion marker for a terminal one).
    """
    try:
        outcome: ScenarioDecisionPersistenceOutcome = submit_prepared_ba201_decision(user_email, prepared)
    except ScenarioLearnerBackendError as exc:
        message = log_and_get_user_message(
            "Scenario Simulator: decision submission backend/integrity failure (uncertain outcome)",
            "We couldn't confirm your last submission. Please select Retry submission to try again.",
            exc=exc,
        )
        st.warning(message)
        return
    except ScenarioLearnerAccessError as exc:
        _clear_pending_decision()
        message = log_and_get_user_message(
            "Scenario Simulator: learner session no longer matches the prepared decision",
            "Please log in again to continue.",
            exc=exc,
        )
        st.warning(message)
        st.stop()
    except ScenarioLearnerVersionUnavailableError as exc:
        _clear_pending_decision()
        message = log_and_get_user_message(
            "Scenario Simulator: scenario version unavailable during decision submission",
            SAFE_UNAVAILABLE_MESSAGE,
            exc=exc,
        )
        _render_unavailable(message)
        st.stop()
    except ScenarioLearnerAttemptNotFoundError as exc:
        _clear_pending_decision()
        message = log_and_get_user_message(
            "Scenario Simulator: decision submitted against an unknown/unowned attempt",
            SAFE_UNAVAILABLE_MESSAGE,
            exc=exc,
        )
        _render_unavailable(message)
        st.stop()
    except ScenarioLearnerAttemptNotActiveError as exc:
        _clear_pending_decision()
        message = log_and_get_user_message(
            "Scenario Simulator: decision submitted against an already-ended attempt",
            "This scenario attempt has already ended.",
            exc=exc,
        )
        st.info(message)
        return
    except ScenarioLearnerConflictError as exc:
        _clear_pending_decision()
        message = log_and_get_user_message(
            "Scenario Simulator: decision submission conflict",
            "This scenario has moved on since it was last loaded. Please try again.",
            exc=exc,
        )
        st.info(message)
        return
    except ScenarioLearnerError as exc:
        # Defensive catch-all for any future controller exception this page
        # has not been explicitly updated to handle -- treated as a
        # conclusive rejection, never as a silently-retried uncertain one.
        _clear_pending_decision()
        message = log_and_get_user_message(
            "Scenario Simulator: unmapped decision-submission controller error",
            SAFE_UNAVAILABLE_MESSAGE,
            exc=exc,
        )
        _render_unavailable(message)
        st.stop()
    else:
        _clear_pending_decision()
        if outcome.is_complete:
            # Never call start_or_resume_ba201_attempt(...) again this
            # session after a confirmed terminal result -- it would create
            # a brand new replacement attempt (see module docstring). The
            # email-bound completion marker becomes this session's
            # authoritative "scenario complete" display.
            st.session_state[_COMPLETED_ATTEMPT_STATE_KEY] = ScenarioAttemptCompletionMarker(
                normalized_email=prepared.normalized_email,
                attempt_id=outcome.attempt_id,
                status=outcome.attempt_status,
            )
        st.rerun()


def _start_new_decision(user_email: str, *, attempt_id: str, selected_option_id: str) -> None:
    """Prepare exactly one NEW, intentional learner decision, store the
    resulting immutable request BEFORE persistence is attempted, then
    submit it.

    A PREPARATION failure (every exception below) means
    `utils.scenario_persistence.submit_decision(...)` was NEVER called --
    there is nothing uncertain to preserve, so no pending state is ever
    left behind for any of these, including `ScenarioLearnerBackendError`
    (which, here, can only mean the read-only attempt lookup failed, not an
    uncertain write). This is the deliberate distinction from
    `_submit_prepared_decision(...)`'s own outcome handling, per the
    SIM-VSLICE-02A "ERROR SEMANTICS" requirement.
    """
    idempotency_key = str(uuid.uuid4())
    try:
        prepared = prepare_ba201_decision(
            user_email,
            attempt_id=attempt_id,
            selected_option_id=selected_option_id,
            idempotency_key=idempotency_key,
        )
    except ScenarioLearnerAccessError as exc:
        message = log_and_get_user_message(
            "Scenario Simulator: missing/invalid learner email during decision preparation",
            "Please log in again to continue.",
            exc=exc,
        )
        st.warning(message)
        st.stop()
        return
    except ScenarioLearnerAttemptNotActiveError as exc:
        message = log_and_get_user_message(
            "Scenario Simulator: decision prepared against an already-ended attempt",
            "This scenario attempt has already ended.",
            exc=exc,
        )
        st.info(message)
        return
    except ScenarioLearnerInvalidOptionError as exc:
        message = log_and_get_user_message(
            "Scenario Simulator: invalid decision option selected",
            "That option is no longer available for the current scene. Please choose again.",
            exc=exc,
        )
        st.warning(message)
        return
    except ScenarioLearnerError as exc:
        # Defensive catch-all covering ScenarioLearnerContentError,
        # ScenarioLearnerVersionUnavailableError,
        # ScenarioLearnerAttemptNotFoundError, ScenarioLearnerStateError,
        # ScenarioLearnerBackendError (a READ failure here, never a write),
        # and any future/unmapped preparation failure -- ALL of these are
        # no-write-attempted failures, so all are shown as a conclusive,
        # safe-to-stop-on message with nothing left pending.
        message = log_and_get_user_message(
            "Scenario Simulator: decision preparation failure",
            SAFE_UNAVAILABLE_MESSAGE,
            exc=exc,
        )
        _render_unavailable(message)
        st.stop()
        return

    # Stored BEFORE persistence is attempted, so any Streamlit rerun
    # triggered by what happens next (including st.rerun() on success)
    # always finds the exact prepared request.
    st.session_state[_PENDING_DECISION_STATE_KEY] = prepared
    _submit_prepared_decision(user_email, prepared)


user_email = _require_premium_learner_email()

render_page_header(
    "Scenario Simulator",
    description="A temporary, in-development learner scenario preview. Select an option and submit your decision.",
    badge="Development preview",
    certification_name="Salesforce Certified Business Analyst",
)

_completed_marker = _get_completed_marker(user_email)
_completion_result: Optional[ScenarioCompletionResultView] = None
if _completed_marker is not None:
    try:
        _completion_result = load_ba201_completion_result(
            user_email, attempt_id=_completed_marker.attempt_id
        )
    except (ScenarioLearnerAttemptNotFoundError, ScenarioLearnerAttemptNotCompletedError) as exc:
        # SIM-VSLICE-03: the marker's claim is simply WRONG -- a
        # missing/foreign attempt, or one that is actually still
        # in-progress/abandoned. Clear it and fall through to the normal
        # pending-decision / start-or-resume flow below, exactly as if no
        # marker had ever been stored (see module docstring).
        log_and_get_user_message(
            "Scenario Simulator: completion marker referenced an invalid or non-completed attempt",
            "",
            exc=exc,
        )
        _clear_completed_marker()
    except ScenarioLearnerAccessError as exc:
        message = log_and_get_user_message(
            "Scenario Simulator: missing/invalid learner email while loading completion result",
            "Please log in again to continue.",
            exc=exc,
        )
        st.warning(message)
        st.stop()
    except ScenarioLearnerError as exc:
        # SIM-VSLICE-03: a pinned version temporarily unavailable, a
        # malformed persisted terminal state, or an uncertain
        # backend/network failure -- all treated as a TEMPORARY rendering
        # problem, never as proof the marker itself is wrong. The marker
        # is deliberately preserved so the learner can retry (e.g. by
        # refreshing) once the underlying problem clears, and this page
        # never falls through to start_or_resume_ba201_attempt(...) here
        # (which would otherwise silently create a brand-new replacement
        # attempt for what may still be a genuinely completed one).
        message = log_and_get_user_message(
            "Scenario Simulator: completion result could not be loaded",
            SAFE_UNAVAILABLE_MESSAGE,
            exc=exc,
        )
        _render_unavailable(message)
        st.stop()

if _completion_result is not None:
    _render_completion_result(_completion_result)
else:
    _pending_decision = _get_pending_prepared_decision(user_email)
    if _pending_decision is not None:
        # A pending prepared request always takes priority over
        # start_or_resume_ba201_attempt(...) -- see module docstring.
        st.warning("Your last submission has not been confirmed yet. Select Retry submission to check its status.")
        if st.button("Retry submission", key=f"scenario_retry_{_pending_decision.attempt_id}"):
            _submit_prepared_decision(user_email, _pending_decision)
    else:
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
            # Defensive only (see module docstring): start_or_resume never
            # actually returns a completed attempt in practice, but if it
            # ever did, record it exactly like a decision-driven completion
            # so the NEXT rerun never calls start/resume again either.
            st.session_state[_COMPLETED_ATTEMPT_STATE_KEY] = ScenarioAttemptCompletionMarker(
                normalized_email=_normalize_email_for_ownership_check(user_email),
                attempt_id=attempt_view.attempt_id,
                status="completed",
            )
            render_empty_state(
                "Scenario complete",
                "You've reached the end of this scenario. Detailed results are not part of this preview yet.",
                action_label="Return to Practice",
                action_href="pages/Practice.py",
            )
        else:
            scene = attempt_view.current_scene
            st.markdown(f"**Domain:** {scene.domain_label}")
            st.write(scene.narrative)
            st.markdown(f"**{scene.decision_prompt}**")

            # SIM-VSLICE-02A: option IDs are the radio VALUES; labels are
            # rendered only via format_func, so two options that happen to
            # share the same visible label can never collapse into one
            # selectable identity (unlike the previous {label: option_id}
            # dict, where a duplicate label would silently discard one of
            # the options).
            option_ids = [option.option_id for option in scene.options]
            option_labels_by_id = {option.option_id: option.label for option in scene.options}
            with st.form(key=f"scenario_decision_form_{attempt_view.attempt_id}"):
                selected_option_id = st.radio(
                    "Choose one option:",
                    option_ids,
                    format_func=lambda option_id: option_labels_by_id.get(option_id, option_id),
                    key=f"scenario_decision_choice_{attempt_view.attempt_id}",
                )
                submitted = st.form_submit_button("Submit Decision")
            if submitted:
                if not selected_option_id:
                    st.warning("Please select an option before submitting.")
                else:
                    _start_new_decision(
                        user_email,
                        attempt_id=attempt_view.attempt_id,
                        selected_option_id=selected_option_id,
                    )

st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
