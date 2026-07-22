"""SIM-VSLICE-03: focused tests for the persisted BA-201 completion-results
experience in `pages/Scenario_Simulator.py`.

Follows the same established precedent as
`tests/test_scenario_decision_submission_page.py` /
`tests/test_scenario_simulator_page_access.py`: inject a fake `streamlit`
module via `sys.modules`, patch the access-control / navigation /
session-timeout / controller entry points the page imports via
`from ... import ...`, then load and execute the real page file with
`importlib.util.spec_from_file_location`.

Scope: ONLY the completion-marker-to-results-view flow added by
SIM-VSLICE-03 -- i.e. what happens on a script pass that finds a stored
`ScenarioAttemptCompletionMarker` in `st.session_state`. Decision-submission
and idempotency behavior (which stores that marker in the first place)
remains covered exclusively by `tests/test_scenario_decision_submission_page.py`,
and `load_ba201_completion_result(...)`'s own field-mapping/validation rules
remain covered exclusively by `tests/test_scenario_learner_controller.py`.
Both are patched here as pure boundaries.
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
import utils.navigation  # noqa: F401
import utils.session_timeout  # noqa: F401
from utils.scenario_learner_controller import (
    ScenarioAttemptCompletionMarker,
    ScenarioAttemptView,
    ScenarioCompletionResultView,
    ScenarioLearnerAttemptNotCompletedError,
    ScenarioLearnerAttemptNotFoundError,
    ScenarioLearnerBackendError,
    ScenarioLearnerStateError,
    ScenarioLearnerVersionUnavailableError,
    ScenarioOptionView,
    ScenarioSceneView,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = REPO_ROOT / "pages" / "Scenario_Simulator.py"

_LEARNER_EMAIL = "learner@example.com"
_OTHER_LEARNER_EMAIL = "someone.else@example.com"
_LEARNER_EMAIL_NORMALIZED = "learner@example.com"
_ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
_COMPLETED_STATE_KEY = "ba201_completed_attempt"
_PENDING_STATE_KEY = "ba201_pending_decision"


def _make_marker(
    *, attempt_id: str = _ATTEMPT_ID, normalized_email: str = _LEARNER_EMAIL_NORMALIZED, status: str = "completed"
) -> ScenarioAttemptCompletionMarker:
    return ScenarioAttemptCompletionMarker(
        normalized_email=normalized_email, attempt_id=attempt_id, status=status
    )


def _make_completion_result(
    *,
    scenario_title: str = "The Meridian Health Salesforce Rollout",
    certification_exam_name: str = "Salesforce Certified Business Analyst",
    ending_title: str = "Pass with Distinction",
    recommended_review_domains: tuple = (),
) -> ScenarioCompletionResultView:
    return ScenarioCompletionResultView(
        scenario_title=scenario_title,
        certification_exam_name=certification_exam_name,
        completion_heading="Scenario complete",
        ending_title=ending_title,
        ending_narrative="Meridian Health goes live on schedule.",
        decisions_correct=15,
        decisions_total=24,
        accuracy_percentage=62.5,
        domain_breakdown=(),
        recommended_review_domains=recommended_review_domains,
    )


def _make_attempt_view(*, attempt_id: str = _ATTEMPT_ID) -> ScenarioAttemptView:
    return ScenarioAttemptView(
        attempt_id=attempt_id,
        is_new_attempt=False,
        is_complete=False,
        scenario_title="The Meridian Health Salesforce Rollout",
        certification_exam_name="Salesforce Certified Business Analyst",
        progress_label="Decision 1",
        current_scene=ScenarioSceneView(
            domain_label="Customer Discovery",
            narrative="Week 1 narrative.",
            decision_prompt="What do you do?",
            options=(ScenarioOptionView(option_id="A", label="Option A"),),
        ),
    )


class _FakeFormContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _make_fake_streamlit(*, session_state: dict):
    return types.SimpleNamespace(
        set_page_config=MagicMock(),
        info=MagicMock(),
        warning=MagicMock(),
        error=MagicMock(),
        markdown=MagicMock(),
        caption=MagicMock(),
        write=MagicMock(),
        page_link=MagicMock(),
        button=MagicMock(return_value=False),
        form=MagicMock(return_value=_FakeFormContext()),
        radio=MagicMock(return_value="A"),
        form_submit_button=MagicMock(return_value=False),
        rerun=MagicMock(side_effect=lambda: (_ for _ in ()).throw(SystemExit())),
        session_state=session_state,
        stop=MagicMock(side_effect=lambda: (_ for _ in ()).throw(SystemExit())),
    )


def _exec_page_completion(
    *,
    session_state: dict,
    learner_email: str = _LEARNER_EMAIL,
    completion_side_effect=None,
    completion_return: Optional[ScenarioCompletionResultView] = None,
    start_resume_return: Optional[ScenarioAttemptView] = None,
):
    fake_st = _make_fake_streamlit(session_state=session_state)

    if completion_side_effect is not None:
        completion_mock = MagicMock(side_effect=completion_side_effect)
    else:
        completion_mock = MagicMock(return_value=completion_return or _make_completion_result())

    start_resume_mock = MagicMock(return_value=start_resume_return or _make_attempt_view())

    with patch.dict(sys.modules, {"streamlit": fake_st}):
        with patch("utils.access_control.require_paid_access", return_value=True), \
             patch("utils.access_control.get_current_user_email", return_value=learner_email), \
             patch("utils.access_control.render_app_chrome"), \
             patch("utils.session_timeout.enforce_session_timeout"), \
             patch("utils.session_timeout.show_session_expired_notice"), \
             patch("utils.navigation.is_feature_flag_enabled", return_value=True), \
             patch("utils.scenario_learner_controller.load_ba201_completion_result", completion_mock), \
             patch("utils.scenario_learner_controller.start_or_resume_ba201_attempt", start_resume_mock):
            spec = importlib.util.spec_from_file_location(
                "scenario_simulator_completion_page_under_test", PAGE_PATH
            )
            module = importlib.util.module_from_spec(spec)
            exec_exc = None
            try:
                spec.loader.exec_module(module)
            except SystemExit as exc:
                exec_exc = exc
            return exec_exc, fake_st, completion_mock, start_resume_mock


class CompletionResultPageTests(unittest.TestCase):
    # -- 17: a valid completion marker never calls start_or_resume ----------

    def test_valid_marker_never_calls_start_or_resume(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker()}
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state
        )
        self.assertIsNone(exec_exc)
        start_resume_mock.assert_not_called()
        completion_mock.assert_called_once()
        _args, kwargs = completion_mock.call_args
        self.assertEqual(kwargs.get("attempt_id") or _args[-1], _ATTEMPT_ID)
        fake_st.form.assert_not_called()

    def test_valid_marker_renders_results_fields(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker()}
        result_view = _make_completion_result(ending_title="Pass with Distinction")
        exec_exc, fake_st, _completion_mock, _start_resume_mock = _exec_page_completion(
            session_state=session_state, completion_return=result_view
        )
        self.assertIsNone(exec_exc)
        rendered_markdown = " ".join(str(call) for call in fake_st.markdown.call_args_list)
        self.assertIn("Scenario complete", rendered_markdown)
        self.assertIn("Pass with Distinction", rendered_markdown)
        fake_st.page_link.assert_called_once()
        self.assertIn(_COMPLETED_STATE_KEY, session_state)  # never cleared on success

    # -- 18: refresh continues showing the SAME completed attempt -----------

    def test_refresh_continues_showing_same_completed_attempt(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker(attempt_id=_ATTEMPT_ID)}

        exec_exc_1, _fake_st_1, completion_mock_1, start_resume_mock_1 = _exec_page_completion(
            session_state=session_state
        )
        self.assertIsNone(exec_exc_1)
        completion_mock_1.assert_called_once()
        start_resume_mock_1.assert_not_called()
        self.assertIn(_COMPLETED_STATE_KEY, session_state)
        self.assertEqual(session_state[_COMPLETED_STATE_KEY].attempt_id, _ATTEMPT_ID)

        # A second, independent script rerun (e.g. a browser refresh) with
        # the SAME session_state must reach the controller again with the
        # SAME attempt_id -- never a different one, never start/resume.
        exec_exc_2, _fake_st_2, completion_mock_2, start_resume_mock_2 = _exec_page_completion(
            session_state=session_state
        )
        self.assertIsNone(exec_exc_2)
        completion_mock_2.assert_called_once()
        _args, kwargs = completion_mock_2.call_args
        self.assertEqual(kwargs.get("attempt_id") or _args[-1], _ATTEMPT_ID)
        start_resume_mock_2.assert_not_called()
        self.assertIn(_COMPLETED_STATE_KEY, session_state)

    # -- 19: a marker for a different learner is cleared ---------------------

    def test_marker_for_different_learner_is_cleared_and_start_resume_proceeds(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker(normalized_email=_LEARNER_EMAIL_NORMALIZED)}
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state, learner_email=_OTHER_LEARNER_EMAIL
        )
        self.assertIsNone(exec_exc)
        # The marker is discarded by _get_completed_marker(...) BEFORE ever
        # calling load_ba201_completion_result(...) -- ownership is a
        # display-layer check independent of the controller.
        completion_mock.assert_not_called()
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)
        start_resume_mock.assert_called_once()
        fake_st.form.assert_called_once()

    # -- 20: a temporary backend failure preserves a valid marker -----------

    def test_backend_failure_preserves_marker_and_shows_unavailable(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker()}
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state,
            completion_side_effect=ScenarioLearnerBackendError("backend unavailable"),
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_called_once()
        start_resume_mock.assert_not_called()
        self.assertIn(_COMPLETED_STATE_KEY, session_state)  # preserved for retry

    def test_version_unavailable_preserves_marker_and_shows_unavailable(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker()}
        exec_exc, _fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state,
            completion_side_effect=ScenarioLearnerVersionUnavailableError("pinned version unavailable"),
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_called_once()
        start_resume_mock.assert_not_called()
        self.assertIn(_COMPLETED_STATE_KEY, session_state)

    def test_malformed_state_preserves_marker_and_shows_unavailable(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker()}
        exec_exc, _fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state,
            completion_side_effect=ScenarioLearnerStateError("malformed persisted state"),
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_called_once()
        start_resume_mock.assert_not_called()
        self.assertIn(_COMPLETED_STATE_KEY, session_state)

    # -- 21: an invalid/non-completed marker is handled safely --------------

    def test_marker_referencing_missing_attempt_is_cleared_and_start_resume_proceeds(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker()}
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state,
            completion_side_effect=ScenarioLearnerAttemptNotFoundError("attempt not found"),
        )
        self.assertIsNone(exec_exc)
        completion_mock.assert_called_once()
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)  # cleared, never retried
        start_resume_mock.assert_called_once()
        fake_st.form.assert_called_once()

    def test_marker_referencing_not_completed_attempt_is_cleared_and_start_resume_proceeds(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker()}
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state,
            completion_side_effect=ScenarioLearnerAttemptNotCompletedError("still in progress"),
        )
        self.assertIsNone(exec_exc)
        completion_mock.assert_called_once()
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)
        start_resume_mock.assert_called_once()
        fake_st.form.assert_called_once()

    def test_corrupt_marker_value_falls_back_safely_without_calling_controller(self):
        for corrupt_value in ({"attempt_id": _ATTEMPT_ID}, "garbage", 0, None):
            with self.subTest(corrupt_value=corrupt_value):
                session_state = {_COMPLETED_STATE_KEY: corrupt_value} if corrupt_value is not None else {}
                exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
                    session_state=session_state
                )
                self.assertIsNone(exec_exc)
                completion_mock.assert_not_called()
                start_resume_mock.assert_called_once()
                fake_st.form.assert_called_once()

    # -- 22: no pending-decision retry control is shown once completed ------

    def test_pending_decision_state_is_never_shown_once_a_valid_marker_exists(self):
        """A stray pending-decision key left over from a previous execution
        must never surface a "Retry submission" control once a completed
        marker is present -- the completed branch takes priority and the
        pending-decision branch is never even reached."""
        session_state = {
            _COMPLETED_STATE_KEY: _make_marker(),
            _PENDING_STATE_KEY: object(),  # any leftover value; never inspected on this path
        }
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state
        )
        self.assertIsNone(exec_exc)
        completion_mock.assert_called_once()
        start_resume_mock.assert_not_called()
        fake_st.button.assert_not_called()
        fake_st.form.assert_not_called()


if __name__ == "__main__":
    unittest.main()
