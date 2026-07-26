"""SIM-VSLICE-02A/02B: focused tests for the two-stage prepare/submit-prepared
decision-submission and idempotency lifecycle in
`pages/Scenario_Simulator.py`, including the SIM-VSLICE-02B separation of
persistence confirmation (`ScenarioDecisionPersistenceOutcome`) from view
reconstruction (a fresh `start_or_resume_ba201_attempt(...)` call for a
nonterminal outcome; an email-bound completion marker for a terminal one).

Follows the same established precedent as
`tests/test_scenario_simulator_page_access.py`: inject a fake `streamlit`
module via `sys.modules`, patch the access-control / navigation /
session-timeout / controller entry points the page imports via
`from ... import ...`, then load and execute the real page file with
`importlib.util.spec_from_file_location`.

This file is scoped ONLY to the decision-submission/idempotency behavior
added by SIM-VSLICE-02 / corrected by SIM-VSLICE-02A / SIM-VSLICE-02B --
entitlement-gate behavior (which is unaffected by this task) remains covered
exclusively by `tests/test_scenario_simulator_page_access.py`.

Multiple calls to `_exec_page_decision(...)` sharing the SAME `session_state`
dict simulate successive Streamlit script reruns of the identical page,
exactly like a real browser session repeatedly re-executing the script.

SIM-VSLICE-02B architecture note: `submit_prepared_ba201_decision(...)` now
returns a small `ScenarioDecisionPersistenceOutcome` (`attempt_id`,
`attempt_status`, `is_complete`, `current_scene_id`, `idempotent_replay`) --
NEVER a `ScenarioAttemptView` -- and the page never tries to render a scene
from it directly. These tests patch `prepare_ba201_decision(...)` and
`submit_prepared_ba201_decision(...)` to return/raise accordingly, and
`start_or_resume_ba201_attempt(...)` separately for the content-driven
view-reconstruction pass.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.access_control  # noqa: F401 -- ensure patch targets below are resolvable
import utils.dashboard_components as dashboard_components
import utils.navigation  # noqa: F401
import utils.session_timeout  # noqa: F401
from utils.scenario_learner_controller import (
    BA201_CERTIFICATION_EXAM_NAME,
    BA201_SIMULATION_ID,
    ENGINE_VERSION,
    PreparedScenarioDecision,
    ScenarioAttemptView,
    ScenarioCompletionResultView,
    ScenarioDecisionPersistenceOutcome,
    ScenarioLearnerBackendError,
    ScenarioLearnerConflictError,
    ScenarioLearnerContentError,
    ScenarioOptionView,
    ScenarioSceneView,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = REPO_ROOT / "pages" / "Scenario_Simulator.py"

_LEARNER_EMAIL = "learner@example.com"
_OTHER_LEARNER_EMAIL = "someone.else@example.com"
_ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
_VERSION_ID = "22222222-2222-4222-8222-222222222222"
_PENDING_STATE_KEY = "ba201_pending_decision"
_COMPLETED_STATE_KEY = "ba201_completed_attempt"
_COMPLETED_QUERY_PARAM = "completed_attempt"


class _FakeQueryParams(dict):
    def get(self, key, default=""):  # noqa: ANN001
        if key not in self:
            return default
        value = super().get(key, default)
        if isinstance(value, list):
            return str(value[-1] if value else default)
        return str(value or default)

    def get_all(self, key):  # noqa: ANN001
        if key not in self:
            return []
        value = super().get(key)
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]


def _make_attempt_view(
    *,
    attempt_id: str = _ATTEMPT_ID,
    is_complete: bool = False,
    option_a_label: str = "Option A",
    option_b_label: str = "Option B",
    progress_label: str = "Decision 1",
    domain_label: str = "Customer Discovery",
) -> ScenarioAttemptView:
    if is_complete:
        current_scene = None
    else:
        current_scene = ScenarioSceneView(
            domain_label=domain_label,
            narrative="Week 1 narrative.",
            decision_prompt="What do you do?",
            options=(
                ScenarioOptionView(option_id="A", label=option_a_label),
                ScenarioOptionView(option_id="B", label=option_b_label),
            ),
        )
    return ScenarioAttemptView(
        attempt_id=attempt_id,
        is_new_attempt=False,
        is_complete=is_complete,
        scenario_title="The Meridian Health Salesforce Rollout",
        certification_exam_name="Salesforce Certified Business Analyst",
        progress_label=progress_label if not is_complete else "Scenario complete",
        current_scene=current_scene,
    )


def _make_prepared(
    *,
    attempt_id: str = _ATTEMPT_ID,
    selected_option_id: str = "A",
    idempotency_key: str = "33333333-3333-4333-8333-333333333333",
    is_terminal: bool = False,
    normalized_email: str = _LEARNER_EMAIL,
) -> PreparedScenarioDecision:
    return PreparedScenarioDecision(
        normalized_email=normalized_email,
        certification_exam_name=BA201_CERTIFICATION_EXAM_NAME,
        simulation_id=BA201_SIMULATION_ID,
        scenario_version_id=_VERSION_ID,
        scenario_version="1.0.0",
        canonical_content_sha256="a" * 64,
        engine_version=ENGINE_VERSION,
        attempt_id=attempt_id,
        selected_option_id=selected_option_id,
        idempotency_key=idempotency_key,
        expected_sequence_number=1,
        expected_scene_id="s01_kickoff",
        state_before_json='{"currentSceneId":"s01_kickoff"}',
        state_after_json='{"currentSceneId":"s02a_cio_response"}',
        resulting_scene_id=None if is_terminal else "s02a_cio_response",
        is_terminal=is_terminal,
        terminal_ending_id="ending-1" if is_terminal else None,
        terminal_result_snapshot_json='{"endingId":"ending-1"}' if is_terminal else None,
    )


def _make_completion_result(
    *,
    scenario_title: str = "The Meridian Health Salesforce Rollout",
    certification_exam_name: str = "Salesforce Certified Business Analyst",
) -> ScenarioCompletionResultView:
    """A minimal, valid `ScenarioCompletionResultView` -- this file is
    scoped to decision-submission/idempotency/marker-lifecycle behavior,
    never to `load_ba201_completion_result(...)`'s own field-mapping rules
    (see `tests/test_scenario_learner_controller.py` for those)."""
    return ScenarioCompletionResultView(
        scenario_title=scenario_title,
        certification_exam_name=certification_exam_name,
        completion_heading="Scenario complete",
        ending_title="Pass",
        ending_narrative="The project goes live on schedule.",
        decisions_correct=3,
        decisions_total=4,
        accuracy_percentage=75.0,
        domain_breakdown=(),
        recommended_review_domains=(),
    )


def _make_outcome(
    *,
    attempt_id: str = _ATTEMPT_ID,
    is_complete: bool = False,
    current_scene_id: Optional[str] = "s02a_cio_response",
    attempt_status: Optional[str] = None,
    idempotent_replay: bool = False,
) -> ScenarioDecisionPersistenceOutcome:
    """SIM-VSLICE-02B: `submit_prepared_ba201_decision(...)`'s actual return
    type -- deliberately NOT a `ScenarioAttemptView`."""
    resolved_status = attempt_status or ("completed" if is_complete else "in_progress")
    return ScenarioDecisionPersistenceOutcome(
        attempt_id=attempt_id,
        attempt_status=resolved_status,
        is_complete=is_complete,
        current_scene_id=None if is_complete else current_scene_id,
        idempotent_replay=idempotent_replay,
    )


class _FakeFormContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _make_fake_streamlit(
    *,
    session_state: dict,
    query_params: Optional[dict] = None,
    submitted: bool,
    retry_clicked: bool,
    selected_option_id: Optional[str],
):
    def _radio(_label, options, **_kwargs):
        options = list(options)
        if selected_option_id is not None and selected_option_id in options:
            return selected_option_id
        return options[0] if options else None

    def _button(_label, *args, **kwargs):
        key = kwargs.get("key", "")
        if key.startswith("scenario_retry_"):
            return retry_clicked
        return False

    return types.SimpleNamespace(
        set_page_config=MagicMock(),
        info=MagicMock(),
        warning=MagicMock(),
        error=MagicMock(),
        markdown=MagicMock(),
        caption=MagicMock(),
        write=MagicMock(),
        page_link=MagicMock(),
        button=MagicMock(side_effect=_button),
        switch_page=MagicMock(),
        form=MagicMock(return_value=_FakeFormContext()),
        radio=MagicMock(side_effect=_radio),
        form_submit_button=MagicMock(return_value=submitted),
        rerun=MagicMock(side_effect=lambda: (_ for _ in ()).throw(SystemExit())),
        session_state=session_state,
        query_params=_FakeQueryParams(query_params or {}),
        stop=MagicMock(side_effect=lambda: (_ for _ in ()).throw(SystemExit())),
    )


def _exec_page_decision(
    *,
    session_state: dict,
    attempt_view: ScenarioAttemptView,
    query_params: Optional[dict] = None,
    submitted: bool = False,
    retry_clicked: bool = False,
    selected_option_id: Optional[str] = None,
    learner_email: str = _LEARNER_EMAIL,
    start_resume_return: Optional[ScenarioAttemptView] = None,
    prepare_side_effect=None,
    prepare_return: Optional[PreparedScenarioDecision] = None,
    submit_side_effect=None,
    submit_return: Optional[ScenarioDecisionPersistenceOutcome] = None,
    completion_result_side_effect=None,
    completion_result_return: Optional[ScenarioCompletionResultView] = None,
):
    fake_st = _make_fake_streamlit(
        session_state=session_state,
        query_params=query_params,
        submitted=submitted,
        retry_clicked=retry_clicked,
        selected_option_id=selected_option_id,
    )

    start_resume_mock = MagicMock(return_value=start_resume_return or attempt_view)

    if prepare_side_effect is not None:
        prepare_mock = MagicMock(side_effect=prepare_side_effect)
    else:
        prepare_mock = MagicMock(return_value=prepare_return or _make_prepared())

    if submit_side_effect is not None:
        submit_mock = MagicMock(side_effect=submit_side_effect)
    else:
        submit_mock = MagicMock(
            return_value=submit_return if submit_return is not None else _make_outcome(attempt_id=attempt_view.attempt_id)
        )

    # SIM-VSLICE-03: this file remains scoped to decision-submission /
    # idempotency / marker-lifecycle behavior -- `load_ba201_completion_result(...)`'s
    # own field-mapping/validation rules are covered exclusively by
    # `tests/test_scenario_learner_controller.py`. This mock's ONLY job here
    # is to let a completion-marker-bearing rerun render successfully (or
    # exercise a specific marker-preserving/clearing error path) without
    # ever reaching a real Supabase client.
    if completion_result_side_effect is not None:
        completion_result_mock = MagicMock(side_effect=completion_result_side_effect)
    else:
        completion_result_mock = MagicMock(return_value=completion_result_return or _make_completion_result())

    # SIM-SMOKE-02E: `utils.dashboard_components` (imported at module load
    # time above, while the real `streamlit` was still active) keeps its own
    # `import streamlit as st` binding regardless of the `patch.dict` below,
    # which only affects *new* imports of `streamlit`. `pages/Scenario_Simulator.py`
    # calls `inject_certbound_theme()`/`render_page_header()`/
    # `render_empty_state()` through that same cached module, so it must be
    # patched here too or those calls silently reach the real Streamlit
    # module instead of this test's own `fake_st`.
    with patch.dict(sys.modules, {"streamlit": fake_st}), \
         patch.object(dashboard_components, "st", fake_st):
        with patch("utils.access_control.require_paid_access", return_value=True), \
             patch("utils.access_control.get_current_user_email", return_value=learner_email), \
             patch("utils.access_control.render_app_chrome"), \
             patch("utils.session_timeout.enforce_session_timeout"), \
             patch("utils.session_timeout.show_session_expired_notice"), \
             patch("utils.navigation.is_feature_flag_enabled", return_value=True), \
             patch("utils.scenario_learner_controller.start_or_resume_ba201_attempt", start_resume_mock), \
             patch("utils.scenario_learner_controller.prepare_ba201_decision", prepare_mock), \
             patch("utils.scenario_learner_controller.submit_prepared_ba201_decision", submit_mock), \
             patch("utils.scenario_learner_controller.load_ba201_completion_result", completion_result_mock):
            spec = importlib.util.spec_from_file_location("scenario_simulator_decision_page_under_test", PAGE_PATH)
            module = importlib.util.module_from_spec(spec)
            exec_exc = None
            try:
                spec.loader.exec_module(module)
            except SystemExit as exc:
                exec_exc = exc
            return exec_exc, fake_st, start_resume_mock, prepare_mock, submit_mock, completion_result_mock


class DecisionSubmissionIdempotencyTests(unittest.TestCase):
    def test_no_pending_no_completed_renders_form_not_retry_control(self):
        session_state: dict = {}
        _exec_exc, fake_st, start_resume_mock, prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state, attempt_view=_make_attempt_view(), submitted=False
        )
        start_resume_mock.assert_called_once()
        fake_st.form.assert_called_once()
        fake_st.button.assert_not_called()
        prepare_mock.assert_not_called()
        submit_mock.assert_not_called()
        self.assertNotIn(_PENDING_STATE_KEY, session_state)

    def test_intentional_submit_prepares_before_submitting_with_fresh_key(self):
        session_state: dict = {}
        attempt_view = _make_attempt_view()
        prepared = _make_prepared(idempotency_key="44444444-4444-4444-8444-444444444444")

        # The submit mock inspects session_state DURING the call, proving
        # the prepared request was stored BEFORE submit_prepared is invoked
        # (requirement 3).
        stored_before_submit = {}

        def _submit_side_effect(_email, prepared_arg, **_kwargs):
            stored_before_submit["value"] = session_state.get(_PENDING_STATE_KEY)
            stored_before_submit["is_same_object"] = prepared_arg is session_state.get(_PENDING_STATE_KEY)
            return _make_outcome(attempt_id=attempt_view.attempt_id)

        exec_exc, _fake_st, _start_mock, prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            prepare_return=prepared,
            submit_side_effect=_submit_side_effect,
        )
        self.assertIsInstance(exec_exc, SystemExit)  # success path reruns
        prepare_mock.assert_called_once()
        prepare_kwargs = prepare_mock.call_args.kwargs
        self.assertEqual(prepare_kwargs["attempt_id"], attempt_view.attempt_id)
        self.assertEqual(prepare_kwargs["selected_option_id"], "A")
        self.assertTrue(prepare_kwargs["idempotency_key"])
        submit_mock.assert_called_once()
        self.assertTrue(stored_before_submit["is_same_object"])
        # Success is a CONCLUSIVE outcome -- pending state must be cleared.
        self.assertNotIn(_PENDING_STATE_KEY, session_state)

    def test_uncertain_backend_failure_preserves_exact_prepared_object_across_rerun_and_retry(self):
        """Items 12/13/19: a Streamlit rerun (no learner action) must not
        mint a replacement request, and an uncertain backend failure must
        not clear the pending prepared request."""
        session_state: dict = {}
        attempt_view = _make_attempt_view()
        prepared = _make_prepared()

        # Pass 1: learner intentionally submits; backend result is uncertain.
        exec_exc_1, _fake_st_1, start_mock_1, _prepare_mock_1, submit_mock_1, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            prepare_return=prepared,
            submit_side_effect=ScenarioLearnerBackendError("uncertain network failure"),
        )
        self.assertIsNone(exec_exc_1)  # uncertain outcome never calls st.stop()/st.rerun()
        self.assertIn(_PENDING_STATE_KEY, session_state)
        self.assertIs(session_state[_PENDING_STATE_KEY], prepared)

        # Pass 2: a plain Streamlit rerun -- no learner action at all. Must
        # show the retry control (never the form/start_or_resume), and must
        # NOT mint a new prepared request.
        exec_exc_2, fake_st_2, start_mock_2, prepare_mock_2, submit_mock_2, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=False,
            retry_clicked=False,
        )
        self.assertIsNone(exec_exc_2)
        start_mock_2.assert_not_called()
        fake_st_2.form.assert_not_called()
        prepare_mock_2.assert_not_called()
        submit_mock_2.assert_not_called()
        self.assertIs(session_state[_PENDING_STATE_KEY], prepared)

        # Pass 3: the learner explicitly retries -- the EXACT SAME prepared
        # object must be resubmitted, and this time it succeeds.
        exec_exc_3, _fake_st_3, start_mock_3, prepare_mock_3, submit_mock_3, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=False,
            retry_clicked=True,
            submit_return=_make_outcome(attempt_id=attempt_view.attempt_id),
        )
        self.assertIsInstance(exec_exc_3, SystemExit)  # success reruns
        start_mock_3.assert_not_called()
        prepare_mock_3.assert_not_called()
        submit_mock_3.assert_called_once()
        retry_call_args = submit_mock_3.call_args.args
        self.assertIs(retry_call_args[1], prepared)
        self.assertNotIn(_PENDING_STATE_KEY, session_state)

    def test_enabling_diagnostics_does_not_change_uncertain_retry_or_rerun_orchestration(self):
        """SIM-RUNTIME-03A: `CERTBOUND_SCENARIO_SMOKE_DIAGNOSTICS=1` must
        never change the page's pending-state, rerun, or retry
        orchestration -- it may only add stderr-only diagnostic markers
        (see `tests/test_scenario_learner_controller.py`'s
        `ScenarioSmokeDiagnosticsTests` for direct coverage of the marker
        content itself). Repeats the uncertain -> plain-rerun -> retry
        sequence above with diagnostics enabled and asserts byte-for-byte
        identical pending-state/rerun/mock-call-shaped behavior."""

        def _run_sequence():
            session_state: dict = {}
            attempt_view = _make_attempt_view()
            prepared = _make_prepared()

            exec_exc_1, _fake_st_1, _start_mock_1, _prepare_mock_1, _submit_mock_1, _completion_mock = (
                _exec_page_decision(
                    session_state=session_state,
                    attempt_view=attempt_view,
                    submitted=True,
                    selected_option_id="A",
                    prepare_return=prepared,
                    submit_side_effect=ScenarioLearnerBackendError("uncertain network failure"),
                )
            )
            pending_after_uncertain = session_state.get(_PENDING_STATE_KEY)

            exec_exc_2, _fake_st_2, start_mock_2, prepare_mock_2, submit_mock_2, _completion_mock = (
                _exec_page_decision(
                    session_state=session_state,
                    attempt_view=attempt_view,
                    submitted=False,
                    retry_clicked=False,
                )
            )

            exec_exc_3, _fake_st_3, start_mock_3, prepare_mock_3, submit_mock_3, _completion_mock = (
                _exec_page_decision(
                    session_state=session_state,
                    attempt_view=attempt_view,
                    submitted=False,
                    retry_clicked=True,
                    submit_return=_make_outcome(attempt_id=attempt_view.attempt_id),
                )
            )

            return (
                isinstance(exec_exc_1, SystemExit),
                isinstance(exec_exc_2, SystemExit),
                isinstance(exec_exc_3, SystemExit),
                pending_after_uncertain is not None,
                start_mock_2.called,
                prepare_mock_2.called,
                submit_mock_2.called,
                start_mock_3.called,
                prepare_mock_3.called,
                submit_mock_3.call_count,
                _PENDING_STATE_KEY not in session_state,
            )

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CERTBOUND_SCENARIO_SMOKE_DIAGNOSTICS", None)
            result_disabled = _run_sequence()

            os.environ["CERTBOUND_SCENARIO_SMOKE_DIAGNOSTICS"] = "1"
            try:
                result_enabled = _run_sequence()
            finally:
                os.environ.pop("CERTBOUND_SCENARIO_SMOKE_DIAGNOSTICS", None)

        self.assertEqual(result_disabled, result_enabled)

    def test_pending_submission_shows_only_retry_control_never_calls_start_or_resume(self):
        """Requirement 13: a pending prepared submission is checked and
        rendered BEFORE `start_or_resume_ba201_attempt(...)` is ever
        called."""
        prepared = _make_prepared()
        session_state = {_PENDING_STATE_KEY: prepared}
        _exec_exc, fake_st, start_resume_mock, prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=_make_attempt_view(),
            submitted=False,
            retry_clicked=False,
        )
        start_resume_mock.assert_not_called()
        fake_st.form.assert_not_called()
        fake_st.button.assert_called_once()
        prepare_mock.assert_not_called()
        submit_mock.assert_not_called()

    def test_page_preserves_pending_decision_when_submit_client_initialization_fails(self):
        """SIM-VSLICE-02C requirement 4: a default-Supabase-client
        initialization failure inside `submit_prepared_ba201_decision(...)`
        is mapped by the controller to `ScenarioLearnerBackendError` --
        from this page's perspective that is indistinguishable from any
        other uncertain submit/replay failure, so the exact pending
        `PreparedScenarioDecision` must be preserved for Retry, never
        cleared."""
        session_state: dict = {}
        attempt_view = _make_attempt_view()
        prepared = _make_prepared()

        exec_exc, _fake_st, _start_mock, _prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            prepare_return=prepared,
            submit_side_effect=ScenarioLearnerBackendError("The scenario service is temporarily unavailable."),
        )
        self.assertIsNone(exec_exc)
        submit_mock.assert_called_once()
        self.assertIn(_PENDING_STATE_KEY, session_state)
        self.assertIs(session_state[_PENDING_STATE_KEY], prepared)

    def test_pending_decision_for_different_learner_is_cleared_and_never_reaches_controller(self):
        """SIM-VSLICE-02C requirements 12/13: a pending `PreparedScenarioDecision`
        bound to one learner's normalized email must never be shown as
        "Retry submission", and must never be passed to
        `submit_prepared_ba201_decision(...)`, under a DIFFERENT currently
        authenticated learner's session -- it is discarded immediately, and
        the page falls back to the normal start/resume + form flow for that
        different learner."""
        other_learners_prepared = _make_prepared(normalized_email=_LEARNER_EMAIL)
        session_state = {_PENDING_STATE_KEY: other_learners_prepared}
        attempt_view = _make_attempt_view()

        exec_exc, fake_st, start_resume_mock, prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=False,
            learner_email=_OTHER_LEARNER_EMAIL,
        )

        self.assertIsNone(exec_exc)
        self.assertNotIn(_PENDING_STATE_KEY, session_state)  # cleared, never shown
        submit_mock.assert_not_called()
        prepare_mock.assert_not_called()
        start_resume_mock.assert_called_once()
        fake_st.form.assert_called_once()
        fake_st.button.assert_not_called()

    def test_pending_decision_for_matching_learner_is_unaffected_by_ownership_check(self):
        """SIM-VSLICE-02C requirement 14: the new ownership check must not
        change behavior at all for the (normal) case where the pending
        request's email matches the current verified learner."""
        matching_prepared = _make_prepared(normalized_email=_LEARNER_EMAIL)
        session_state = {_PENDING_STATE_KEY: matching_prepared}
        attempt_view = _make_attempt_view()

        exec_exc, fake_st, start_resume_mock, prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=False,
            learner_email=_LEARNER_EMAIL,
        )

        self.assertIsNone(exec_exc)
        self.assertIs(session_state[_PENDING_STATE_KEY], matching_prepared)
        start_resume_mock.assert_not_called()
        fake_st.form.assert_not_called()
        fake_st.button.assert_called_once()
        prepare_mock.assert_not_called()
        submit_mock.assert_not_called()

    def test_pending_state_not_discarded_when_start_or_resume_would_return_different_attempt(self):
        """Requirement 14: even if `start_or_resume_ba201_attempt(...)`
        would return a completely different attempt id, the pending
        prepared request for the ORIGINAL attempt must never be silently
        discarded -- proven here by the fact start_or_resume is never even
        called while a pending request exists."""
        prepared = _make_prepared(attempt_id=_ATTEMPT_ID)
        session_state = {_PENDING_STATE_KEY: prepared}
        different_attempt_view = _make_attempt_view(attempt_id="99999999-9999-4999-8999-999999999999")

        _exec_exc, _fake_st, start_resume_mock, _prepare_mock, _submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=different_attempt_view,
            start_resume_return=different_attempt_view,
            submitted=False,
            retry_clicked=False,
        )

        start_resume_mock.assert_not_called()
        self.assertIs(session_state[_PENDING_STATE_KEY], prepared)
        self.assertEqual(session_state[_PENDING_STATE_KEY].attempt_id, _ATTEMPT_ID)

    def test_corrupt_pending_state_falls_back_safely(self):
        """Requirement 15: a corrupt/obsolete pending value (e.g. the old
        SIM-VSLICE-02 plain-dict shape, or any other garbage) must never
        raise `KeyError`/`AttributeError` and must never be rendered -- it
        is discarded and the page falls back to the normal start/resume +
        form flow."""
        for corrupt_value in (
            {"attempt_id": _ATTEMPT_ID, "selected_option_id": "A", "idempotency_key": "not-a-real-object"},
            "just-a-string",
            12345,
            [],
        ):
            with self.subTest(corrupt_value=corrupt_value):
                session_state = {_PENDING_STATE_KEY: corrupt_value}
                exec_exc, fake_st, start_resume_mock, prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
                    session_state=session_state,
                    attempt_view=_make_attempt_view(),
                    submitted=False,
                )
                self.assertIsNone(exec_exc)
                start_resume_mock.assert_called_once()
                fake_st.form.assert_called_once()
                fake_st.button.assert_not_called()
                prepare_mock.assert_not_called()
                submit_mock.assert_not_called()
                self.assertNotIn(_PENDING_STATE_KEY, session_state)

    def test_corrupt_completed_state_falls_back_safely(self):
        for corrupt_value in ({"is_complete": True}, "garbage", 0):
            with self.subTest(corrupt_value=corrupt_value):
                session_state = {_COMPLETED_STATE_KEY: corrupt_value}
                exec_exc, fake_st, start_resume_mock, _prepare_mock, _submit_mock, _completion_mock = _exec_page_decision(
                    session_state=session_state,
                    attempt_view=_make_attempt_view(),
                    submitted=False,
                )
                self.assertIsNone(exec_exc)
                start_resume_mock.assert_called_once()
                fake_st.form.assert_called_once()
                self.assertNotIn(_COMPLETED_STATE_KEY, session_state)

    def test_definitive_rejection_clears_pending_state_without_retry(self):
        """A conclusive rejection (not merely uncertain) must clear pending
        state so the learner is never stuck retrying a request that can
        never succeed."""
        session_state: dict = {}
        attempt_view = _make_attempt_view()
        _exec_exc, _fake_st, _start_mock, prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            submit_side_effect=ScenarioLearnerConflictError("moved on"),
        )
        prepare_mock.assert_called_once()
        submit_mock.assert_called_once()
        self.assertNotIn(_PENDING_STATE_KEY, session_state)

    def test_preparation_failure_leaves_no_pending_state(self):
        """Requirement 18: a PREPARATION failure never attempted any V68
        write, so it must never leave a pending key/request behind, and
        `submit_prepared_ba201_decision(...)` must never even be called."""
        session_state: dict = {}
        attempt_view = _make_attempt_view()
        _exec_exc, _fake_st, _start_mock, prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            prepare_side_effect=ScenarioLearnerContentError("content unavailable"),
        )
        prepare_mock.assert_called_once()
        submit_mock.assert_not_called()
        self.assertNotIn(_PENDING_STATE_KEY, session_state)

    def test_terminal_success_stores_completion_and_never_autostarts_replacement_attempt(self):
        """Requirement 16 / SIM-UI-04: a confirmed terminal
        `ScenarioDecisionPersistenceOutcome` must record both the session
        completion marker and the `completed_attempt` query reference, and
        must never cause a subsequent rerun to call
        `start_or_resume_ba201_attempt(...)` again."""
        session_state: dict = {}
        attempt_view = _make_attempt_view()
        completed_outcome = _make_outcome(attempt_id=attempt_view.attempt_id, is_complete=True)

        exec_exc_1, fake_st_1, start_mock_1, _prepare_mock_1, submit_mock_1, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            submit_return=completed_outcome,
        )
        self.assertIsInstance(exec_exc_1, SystemExit)  # success path reruns
        self.assertIn(_COMPLETED_STATE_KEY, session_state)
        self.assertNotIn(_PENDING_STATE_KEY, session_state)
        self.assertEqual(fake_st_1.query_params.get(_COMPLETED_QUERY_PARAM), attempt_view.attempt_id)

        exec_exc_2, fake_st_2, start_mock_2, prepare_mock_2, submit_mock_2, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=False,
        )
        self.assertIsNone(exec_exc_2)
        start_mock_2.assert_not_called()
        fake_st_2.form.assert_not_called()
        prepare_mock_2.assert_not_called()
        submit_mock_2.assert_not_called()

    def test_mid_scenario_refresh_still_resumes_existing_attempt(self):
        session_state: dict = {}
        attempt_view = _make_attempt_view()

        exec_exc_1, fake_st_1, start_mock_1, _prepare_mock_1, _submit_mock_1, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=False,
        )
        self.assertIsNone(exec_exc_1)
        start_mock_1.assert_called_once()
        fake_st_1.form.assert_called_once()
        self.assertNotIn(_COMPLETED_QUERY_PARAM, fake_st_1.query_params)

        exec_exc_2, fake_st_2, start_mock_2, _prepare_mock_2, _submit_mock_2, _completion_mock = _exec_page_decision(
            session_state={},
            attempt_view=attempt_view,
            submitted=False,
        )
        self.assertIsNone(exec_exc_2)
        start_mock_2.assert_called_once()
        fake_st_2.form.assert_called_once()

    def test_completion_marker_for_different_learner_is_cleared_and_start_resume_proceeds(self):
        """SIM-VSLICE-02B: a completion marker bound to one learner's
        normalized email must never be shown to a DIFFERENT currently
        authenticated learner -- it is discarded, and the page falls back
        to the normal start/resume flow for that different learner."""
        session_state: dict = {}
        attempt_view = _make_attempt_view()
        completed_outcome = _make_outcome(attempt_id=attempt_view.attempt_id, is_complete=True)

        exec_exc_1, *_rest_1 = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            learner_email=_LEARNER_EMAIL,
            prepare_return=_make_prepared(normalized_email=_LEARNER_EMAIL),
            submit_return=completed_outcome,
        )
        self.assertIsInstance(exec_exc_1, SystemExit)
        self.assertIn(_COMPLETED_STATE_KEY, session_state)

        # A DIFFERENT learner's session reaches this page next (same
        # session_state dict only for test convenience -- in production
        # each learner has their own Streamlit session).
        exec_exc_2, fake_st_2, start_mock_2, _prepare_mock_2, _submit_mock_2, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=False,
            learner_email=_OTHER_LEARNER_EMAIL,
        )
        self.assertIsNone(exec_exc_2)
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)  # cleared, never shown
        start_mock_2.assert_called_once()
        fake_st_2.form.assert_called_once()

    def test_nonterminal_success_reruns_and_next_pass_resumes_advanced_attempt(self):
        """Requirement 17: a confirmed NONTERMINAL outcome clears pending
        state, reruns, and the FOLLOWING execution resumes/renders the
        advanced attempt via a fresh `start_or_resume_ba201_attempt(...)`
        call (since nothing is pending and nothing is completed) --
        SIM-VSLICE-02B: the page never tries to build a scene directly from
        the outcome itself."""
        session_state: dict = {}
        attempt_view = _make_attempt_view(progress_label="Decision 1")
        advanced_view = _make_attempt_view(progress_label="Decision 2")
        advanced_outcome = _make_outcome(attempt_id=attempt_view.attempt_id, current_scene_id="s02a_cio_response")

        exec_exc_1, _fake_st_1, start_mock_1, _prepare_mock_1, submit_mock_1, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            submit_return=advanced_outcome,
        )
        self.assertIsInstance(exec_exc_1, SystemExit)
        self.assertNotIn(_PENDING_STATE_KEY, session_state)
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)

        exec_exc_2, fake_st_2, start_mock_2, _prepare_mock_2, _submit_mock_2, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=advanced_view,
            submitted=False,
        )
        self.assertIsNone(exec_exc_2)
        start_mock_2.assert_called_once()
        fake_st_2.form.assert_called_once()

    def test_second_intentional_decision_gets_new_key_only_after_first_resolved(self):
        session_state: dict = {}
        first_view = _make_attempt_view()
        first_prepared = _make_prepared(idempotency_key="55555555-5555-4555-8555-555555555555")

        exec_exc_1, _fake_st_1, _start_mock_1, prepare_mock_1, _submit_mock_1, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=first_view,
            submitted=True,
            selected_option_id="A",
            prepare_return=first_prepared,
            submit_return=_make_outcome(attempt_id=first_view.attempt_id),
        )
        self.assertIsInstance(exec_exc_1, SystemExit)
        first_key = prepare_mock_1.call_args.kwargs["idempotency_key"]
        self.assertNotIn(_PENDING_STATE_KEY, session_state)

        second_view = _make_attempt_view()
        second_prepared = _make_prepared(idempotency_key="66666666-6666-4666-8666-666666666666")
        exec_exc_2, _fake_st_2, _start_mock_2, prepare_mock_2, _submit_mock_2, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=second_view,
            submitted=True,
            selected_option_id="B",
            prepare_return=second_prepared,
            submit_return=_make_outcome(attempt_id=second_view.attempt_id),
        )
        self.assertIsInstance(exec_exc_2, SystemExit)
        second_key = prepare_mock_2.call_args.kwargs["idempotency_key"]

        self.assertNotEqual(first_key, second_key)

    def test_duplicate_option_labels_remain_independently_selectable_by_option_id(self):
        """Requirement 22: two options sharing an identical visible label
        must never collapse into one selectable identity -- the radio
        VALUES are option ids, never labels."""
        session_state: dict = {}
        attempt_view = _make_attempt_view(option_a_label="Continue", option_b_label="Continue")

        _exec_exc, fake_st, _start_mock, prepare_mock, _submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="B",
        )

        radio_call = fake_st.radio.call_args
        radio_options = list(radio_call.args[1])
        self.assertEqual(radio_options, ["A", "B"])
        self.assertIn("format_func", radio_call.kwargs)
        format_func = radio_call.kwargs["format_func"]
        self.assertEqual(format_func("A"), "Continue")
        self.assertEqual(format_func("B"), "Continue")
        # The learner's actual selection ("B") must be exactly what gets
        # prepared, even though both options render the identical label.
        prepare_mock.assert_called_once()
        self.assertEqual(prepare_mock.call_args.kwargs["selected_option_id"], "B")

    def test_hard_refresh_at_real_decision_five_resumes_and_one_submission_advances(self):
        """SIM-RUNTIME-03A correction: the real BA-201 fifth decision
        (scene `s05_workshop_setup`, domain d2 "Collaboration with
        Stakeholders") is NONTERMINAL -- a successful submission there
        advances to `s06_conflict`, never to completion. The actual
        terminal scene is `s24_golive_readiness` (covered separately by
        `test_terminal_success_stores_completion_and_never_autostarts_replacement_attempt`
        and by the controller-level `_advance_to_scene("s24_golive_readiness")`
        fixtures in `tests/test_scenario_learner_controller.py`).

        `ScenarioSceneView` is deliberately learner-safe and has no
        `scene_id` field (see its own docstring in
        `utils/scenario_learner_controller.py`), so this page-orchestration
        test cannot assert the exact persisted scene id -- it approximates
        "Decision 5" via `progress_label` and the real domain-d2 label,
        and proves the ORCHESTRATION contract (prepare once, submit once,
        clear pending, never store a completion marker, never write
        `completed_attempt`, rerun once, and resume as an ADVANCED
        in-progress attempt on the next pass) using a `PreparedScenarioDecision`
        /`ScenarioDecisionPersistenceOutcome` pair shaped exactly like the
        real nonterminal `s05_workshop_setup` -> `s06_conflict` transition.

        This is a MOCKED page-orchestration test, not a reproduction of the
        live backend failure -- it locks in the correct expected UI
        behavior for a clean nonterminal fifth-decision response."""
        session_state: dict = {}
        decision_five_view = _make_attempt_view(
            progress_label="Decision 5",
            domain_label="Collaboration with Stakeholders",  # real domain d2 label
            option_a_label="Invite all 14 to one large kickoff workshop.",
            option_b_label="Use a Power/Interest assessment for a focused initial session.",
        )

        # Pass 1: hard refresh -- fresh session_state, nothing pending/completed.
        exec_exc_1, fake_st_1, start_mock_1, prepare_mock_1, submit_mock_1, _completion_mock_1 = _exec_page_decision(
            session_state=session_state,
            attempt_view=decision_five_view,
            submitted=False,
        )
        self.assertIsNone(exec_exc_1)
        start_mock_1.assert_called_once()
        fake_st_1.form.assert_called_once()
        prepare_mock_1.assert_not_called()
        submit_mock_1.assert_not_called()
        self.assertNotIn(_PENDING_STATE_KEY, session_state)
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)

        # Pass 2: one intentional, NONTERMINAL submission -- real
        # s05_workshop_setup(B) -> s06_conflict shape.
        nonterminal_prepared = PreparedScenarioDecision(
            normalized_email=_LEARNER_EMAIL,
            certification_exam_name=BA201_CERTIFICATION_EXAM_NAME,
            simulation_id=BA201_SIMULATION_ID,
            scenario_version_id=_VERSION_ID,
            scenario_version="1.0.0",
            canonical_content_sha256="a" * 64,
            engine_version=ENGINE_VERSION,
            attempt_id=decision_five_view.attempt_id,
            selected_option_id="B",
            idempotency_key="77777777-7777-4777-8777-777777777777",
            expected_sequence_number=5,
            expected_scene_id="s05_workshop_setup",
            state_before_json='{"currentSceneId":"s05_workshop_setup"}',
            state_after_json='{"currentSceneId":"s06_conflict"}',
            resulting_scene_id="s06_conflict",
            is_terminal=False,
            terminal_ending_id=None,
            terminal_result_snapshot_json=None,
        )
        advanced_outcome = _make_outcome(
            attempt_id=decision_five_view.attempt_id,
            is_complete=False,
            current_scene_id="s06_conflict",
        )
        exec_exc_2, fake_st_2, start_mock_2, prepare_mock_2, submit_mock_2, _completion_mock_2 = _exec_page_decision(
            session_state=session_state,
            attempt_view=decision_five_view,
            submitted=True,
            selected_option_id="B",
            prepare_return=nonterminal_prepared,
            submit_return=advanced_outcome,
        )
        self.assertIsInstance(exec_exc_2, SystemExit)  # nonterminal success reruns exactly once
        prepare_mock_2.assert_called_once()
        submit_mock_2.assert_called_once()
        self.assertNotIn(_PENDING_STATE_KEY, session_state)  # requirement 5
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)  # requirement 6: never a completion marker
        self.assertNotIn(_COMPLETED_QUERY_PARAM, fake_st_2.query_params)  # requirement 7

        # Pass 3: the NEXT render resumes an ADVANCED in-progress attempt
        # (never the completion view) -- requirement 9.
        advanced_view = _make_attempt_view(progress_label="Decision 6")
        exec_exc_3, fake_st_3, start_mock_3, prepare_mock_3, submit_mock_3, _completion_mock_3 = _exec_page_decision(
            session_state=session_state,
            attempt_view=advanced_view,
            submitted=False,
        )
        self.assertIsNone(exec_exc_3)
        start_mock_3.assert_called_once()
        fake_st_3.form.assert_called_once()  # the decision form renders again -- not the completion view
        prepare_mock_3.assert_not_called()
        submit_mock_3.assert_not_called()
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)

    def test_terminal_uncertain_retry_reuses_key_and_reaches_completion_no_duplicate(self):
        """SIM-RUNTIME-03 reproduction 4 (terminal case): a TERMINAL
        prepared decision whose first submission is uncertain must retain
        the exact pending prepared request (same idempotency key) and,
        once retried, reach a confirmed completion -- never a second
        preparation, never a second distinct submission attempt beyond the
        explicit retry."""
        session_state: dict = {}
        attempt_view = _make_attempt_view()
        terminal_prepared = _make_prepared(is_terminal=True)

        exec_exc_1, _fake_st_1, _start_mock_1, prepare_mock_1, submit_mock_1, _completion_mock_1 = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            prepare_return=terminal_prepared,
            submit_side_effect=ScenarioLearnerBackendError("uncertain network failure"),
        )
        self.assertIsNone(exec_exc_1)  # uncertain outcome never calls st.stop()/st.rerun()
        prepare_mock_1.assert_called_once()
        submit_mock_1.assert_called_once()
        self.assertIn(_PENDING_STATE_KEY, session_state)
        self.assertIs(session_state[_PENDING_STATE_KEY], terminal_prepared)

        completed_outcome = _make_outcome(
            attempt_id=attempt_view.attempt_id, is_complete=True, idempotent_replay=True
        )
        exec_exc_2, _fake_st_2, start_mock_2, prepare_mock_2, submit_mock_2, _completion_mock_2 = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=False,
            retry_clicked=True,
            submit_return=completed_outcome,
        )
        self.assertIsInstance(exec_exc_2, SystemExit)  # confirmed completion reruns
        start_mock_2.assert_not_called()
        prepare_mock_2.assert_not_called()
        submit_mock_2.assert_called_once()
        retry_call_args = submit_mock_2.call_args.args
        self.assertIs(retry_call_args[1], terminal_prepared)  # exact same prepared object/idempotency key
        self.assertNotIn(_PENDING_STATE_KEY, session_state)
        self.assertIn(_COMPLETED_STATE_KEY, session_state)

    def test_page_never_imports_scenario_persistence_directly(self):
        source = PAGE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import utils.scenario_persistence", source)
        self.assertNotIn("from utils.scenario_persistence", source)
        self.assertNotIn("from utils import scenario_persistence", source)


class DashboardComponentsModuleCacheIsolationTests(unittest.TestCase):
    """SIM-SMOKE-02E: proves `_exec_page_decision` correctly routes calls
    made through the already-imported, module-cached
    `utils.dashboard_components` (e.g. `inject_certbound_theme()` /
    `render_page_header()`, both called unconditionally by the real page) to
    each test's own `fake_st`, instead of leaking through to the real,
    installed Streamlit module."""

    def test_pre_imported_dashboard_components_module_is_the_real_one_before_exec(self):
        self.assertIn("utils.dashboard_components", sys.modules)
        self.assertIs(sys.modules["utils.dashboard_components"], dashboard_components)

    def test_dashboard_rendering_uses_the_exact_same_fake_st_as_the_page(self):
        real_streamlit_markdown = dashboard_components.st.markdown
        session_state: dict = {}
        _exec_exc, fake_st, *_rest = _exec_page_decision(
            session_state=session_state, attempt_view=_make_attempt_view(), submitted=False
        )
        fake_st.markdown.assert_called()
        self.assertIsNot(fake_st.markdown, real_streamlit_markdown)

    def test_dashboard_components_st_restored_to_exact_prior_object_after_exec(self):
        prior_dashboard_st = dashboard_components.st
        session_state: dict = {}
        _exec_page_decision(session_state=session_state, attempt_view=_make_attempt_view(), submitted=False)
        self.assertIs(dashboard_components.st, prior_dashboard_st)

    def test_dashboard_components_st_restored_even_when_page_raises_systemexit(self):
        prior_dashboard_st = dashboard_components.st
        session_state: dict = {}
        attempt_view = _make_attempt_view()
        exec_exc, _fake_st, _start_mock, prepare_mock, submit_mock, _completion_mock = _exec_page_decision(
            session_state=session_state,
            attempt_view=attempt_view,
            submitted=True,
            selected_option_id="A",
            submit_return=_make_outcome(attempt_id=attempt_view.attempt_id),
        )
        self.assertIsInstance(exec_exc, SystemExit)  # success path reruns
        self.assertIs(dashboard_components.st, prior_dashboard_st)

    def test_result_independent_of_prior_dashboard_components_import_state(self):
        for _ in range(2):
            session_state: dict = {}
            _exec_exc, fake_st, start_resume_mock, _prepare_mock, _submit_mock, _completion_mock = _exec_page_decision(
                session_state=session_state, attempt_view=_make_attempt_view(), submitted=False
            )
            start_resume_mock.assert_called_once()
            fake_st.markdown.assert_called()


if __name__ == "__main__":
    unittest.main()
