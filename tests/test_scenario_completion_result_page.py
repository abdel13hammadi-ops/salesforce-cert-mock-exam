"""SIM-VSLICE-03 / SIM-UI-04: focused tests for the persisted BA-201
completion-results experience in `pages/Scenario_Simulator.py`.

Follows the same established precedent as
`tests/test_scenario_decision_submission_page.py` /
`tests/test_scenario_simulator_page_access.py`: inject a fake `streamlit`
module via `sys.modules`, patch the access-control / navigation /
session-timeout / controller entry points the page imports via
`from ... import ...`, then load and execute the real page file with
`importlib.util.spec_from_file_location`.

Scope: the completion-marker / completed-attempt query-parameter to
results-view flow. Decision-submission and idempotency behavior (which
stores the marker and query reference in the first place) remains covered
by `tests/test_scenario_decision_submission_page.py`, and
`load_ba201_completion_result(...)`'s own field-mapping/validation rules
remain covered by `tests/test_scenario_learner_controller.py`.
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
import utils.dashboard_components as dashboard_components
import utils.navigation  # noqa: F401
import utils.session_timeout  # noqa: F401
from utils.scenario_learner_controller import (
    ScenarioAttemptCompletionMarker,
    ScenarioAttemptView,
    ScenarioCompletionResultView,
    ScenarioLearnerAccessError,
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
_OTHER_ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
_COMPLETED_STATE_KEY = "ba201_completed_attempt"
_PENDING_STATE_KEY = "ba201_pending_decision"
_COMPLETED_QUERY_PARAM = "completed_attempt"


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


class _BrokenQueryParams:
    def __contains__(self, _key) -> bool:
        raise RuntimeError("query params unavailable")

    def get_all(self, _key):  # noqa: ANN001
        raise RuntimeError("query params unavailable")

    def get(self, _key, default=""):  # noqa: ANN001
        raise RuntimeError("query params unavailable")


class _FakeFormContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _make_fake_streamlit(
    *,
    session_state: dict,
    query_params: Optional[dict] = None,
    query_params_object=None,
    button_returns=None,
):
    if button_returns is None:
        button_returns = {}

    def _button(_label, *args, **kwargs):
        key = kwargs.get("key", "")
        return bool(button_returns.get(key, False))

    resolved_query_params = (
        query_params_object
        if query_params_object is not None
        else _FakeQueryParams(query_params or {})
    )

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
        radio=MagicMock(return_value="A"),
        form_submit_button=MagicMock(return_value=False),
        rerun=MagicMock(side_effect=lambda: (_ for _ in ()).throw(SystemExit())),
        session_state=session_state,
        query_params=resolved_query_params,
        stop=MagicMock(side_effect=lambda: (_ for _ in ()).throw(SystemExit())),
    )


def _visible_text(fake_st) -> str:
    parts = []
    for mock in (fake_st.markdown, fake_st.caption, fake_st.write, fake_st.warning, fake_st.info):
        parts.extend(str(call) for call in mock.call_args_list)
    return " ".join(parts)


def _exec_page_completion(
    *,
    session_state: dict,
    query_params: Optional[dict] = None,
    query_params_object=None,
    learner_email: str = _LEARNER_EMAIL,
    button_returns=None,
    completion_side_effect=None,
    completion_return: Optional[ScenarioCompletionResultView] = None,
    start_resume_return: Optional[ScenarioAttemptView] = None,
):
    fake_st = _make_fake_streamlit(
        session_state=session_state,
        query_params=query_params,
        query_params_object=query_params_object,
        button_returns=button_returns,
    )

    if completion_side_effect is not None:
        completion_mock = MagicMock(side_effect=completion_side_effect)
    else:
        completion_mock = MagicMock(return_value=completion_return or _make_completion_result())

    start_resume_mock = MagicMock(return_value=start_resume_return or _make_attempt_view())

    # SIM-SMOKE-02E: `utils.dashboard_components` (imported at module load
    # time above, while the real `streamlit` was still active) keeps its own
    # `import streamlit as st` binding regardless of the `patch.dict` below,
    # which only affects *new* imports of `streamlit`. `pages/Scenario_Simulator.py`
    # calls `inject_certbound_theme()`/`render_page_header()`/
    # `render_empty_state()` through that same cached module -- including the
    # recovery-state rendering this file's `_visible_text()` helper asserts
    # against -- so it must be patched here too, or those calls silently
    # reach the real Streamlit module (and, since `sys.modules["streamlit"]`
    # has been swapped to `fake_st`, break internally) instead of landing in
    # this test's own fake.
    with patch.dict(sys.modules, {"streamlit": fake_st}), \
         patch.object(dashboard_components, "st", fake_st):
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
    # -- SIM-VSLICE-03: session marker path ---------------------------------

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
        fake_st.button.assert_called()
        self.assertIn(_COMPLETED_STATE_KEY, session_state)

    def test_refresh_continues_showing_same_completed_attempt(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker(attempt_id=_ATTEMPT_ID)}

        exec_exc_1, _fake_st_1, completion_mock_1, start_resume_mock_1 = _exec_page_completion(
            session_state=session_state
        )
        self.assertIsNone(exec_exc_1)
        completion_mock_1.assert_called_once()
        start_resume_mock_1.assert_not_called()

        exec_exc_2, _fake_st_2, completion_mock_2, start_resume_mock_2 = _exec_page_completion(
            session_state=session_state
        )
        self.assertIsNone(exec_exc_2)
        completion_mock_2.assert_called_once()
        _args, kwargs = completion_mock_2.call_args
        self.assertEqual(kwargs.get("attempt_id") or _args[-1], _ATTEMPT_ID)
        start_resume_mock_2.assert_not_called()

    def test_marker_for_different_learner_is_cleared_and_start_resume_proceeds(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker(normalized_email=_LEARNER_EMAIL_NORMALIZED)}
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state, learner_email=_OTHER_LEARNER_EMAIL
        )
        self.assertIsNone(exec_exc)
        completion_mock.assert_not_called()
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)
        start_resume_mock.assert_called_once()
        fake_st.form.assert_called_once()

    def test_backend_failure_preserves_marker_and_shows_unavailable(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker()}
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state,
            completion_side_effect=ScenarioLearnerBackendError("backend unavailable"),
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_called_once()
        start_resume_mock.assert_not_called()
        self.assertIn(_COMPLETED_STATE_KEY, session_state)

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

    def test_marker_referencing_missing_attempt_is_cleared_and_start_resume_proceeds(self):
        session_state = {_COMPLETED_STATE_KEY: _make_marker()}
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state,
            completion_side_effect=ScenarioLearnerAttemptNotFoundError("attempt not found"),
        )
        self.assertIsNone(exec_exc)
        completion_mock.assert_called_once()
        self.assertNotIn(_COMPLETED_STATE_KEY, session_state)
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

    def test_pending_decision_state_is_never_shown_once_a_valid_marker_exists(self):
        session_state = {
            _COMPLETED_STATE_KEY: _make_marker(),
            _PENDING_STATE_KEY: object(),
        }
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state
        )
        self.assertIsNone(exec_exc)
        completion_mock.assert_called_once()
        start_resume_mock.assert_not_called()
        retry_button_calls = [
            call for call in fake_st.button.call_args_list
            if call.kwargs.get("key", "").startswith("scenario_retry_")
        ]
        self.assertEqual(retry_button_calls, [])
        fake_st.form.assert_not_called()

    # -- SIM-UI-04: completed_attempt query parameter path -------------------

    def test_valid_query_reference_loads_completion_without_start_or_resume(self):
        query_params = {_COMPLETED_QUERY_PARAM: _ATTEMPT_ID}
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params=query_params,
        )
        self.assertIsNone(exec_exc)
        completion_mock.assert_called_once()
        _args, kwargs = completion_mock.call_args
        self.assertEqual(kwargs.get("attempt_id") or _args[-1], _ATTEMPT_ID)
        start_resume_mock.assert_not_called()
        fake_st.form.assert_not_called()

    def test_hard_refresh_with_query_reference_only_shows_same_completion(self):
        query_params = {_COMPLETED_QUERY_PARAM: _ATTEMPT_ID}
        result_view = _make_completion_result(ending_title="Pass with Distinction")

        exec_exc_1, fake_st_1, completion_mock_1, start_resume_mock_1 = _exec_page_completion(
            session_state={},
            query_params=query_params,
            completion_return=result_view,
        )
        self.assertIsNone(exec_exc_1)
        completion_mock_1.assert_called_once()
        start_resume_mock_1.assert_not_called()

        exec_exc_2, fake_st_2, completion_mock_2, start_resume_mock_2 = _exec_page_completion(
            session_state={},
            query_params=query_params,
            completion_return=result_view,
        )
        self.assertIsNone(exec_exc_2)
        completion_mock_2.assert_called_once()
        start_resume_mock_2.assert_not_called()
        rendered_markdown = " ".join(str(call) for call in fake_st_2.markdown.call_args_list)
        self.assertIn("Pass with Distinction", rendered_markdown)

    def test_malformed_query_reference_rejected_without_backend_or_new_attempt(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: "not-a-uuid"},
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_not_called()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)
        self.assertNotIn(_ATTEMPT_ID, visible)
        self.assertNotIn("not-a-uuid", visible)

    def test_backend_rejection_shows_generic_message_without_new_attempt_or_id_leak(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: _ATTEMPT_ID},
            completion_side_effect=ScenarioLearnerAttemptNotFoundError("missing"),
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_called_once()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)
        self.assertNotIn(_ATTEMPT_ID, visible)

    def test_non_completed_query_reference_shows_generic_message_without_new_attempt(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: _ATTEMPT_ID},
            completion_side_effect=ScenarioLearnerAttemptNotCompletedError("in progress"),
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_called_once()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)

    def test_inaccessible_query_reference_shows_generic_message_without_new_attempt(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: _ATTEMPT_ID},
            completion_side_effect=ScenarioLearnerAccessError("forbidden"),
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_called_once()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)
        self.assertNotIn(_ATTEMPT_ID, visible)

    def test_recovery_action_clears_query_reference(self):
        exec_exc_1, fake_st_1, completion_mock_1, start_resume_mock_1 = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: "not-a-uuid"},
            button_returns={"scenario_clear_completed_attempt_reference": True},
        )
        self.assertIsInstance(exec_exc_1, SystemExit)
        self.assertNotIn(_COMPLETED_QUERY_PARAM, fake_st_1.query_params)
        completion_mock_1.assert_not_called()
        start_resume_mock_1.assert_not_called()

        exec_exc_2, _fake_st_2, completion_mock_2, start_resume_mock_2 = _exec_page_completion(
            session_state={},
            query_params={},
        )
        self.assertIsNone(exec_exc_2)
        completion_mock_2.assert_not_called()
        start_resume_mock_2.assert_called_once()

    def test_return_to_practice_clears_query_reference(self):
        exec_exc, fake_st, _completion_mock, _start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: _ATTEMPT_ID},
            button_returns={"scenario_completion_return_to_practice": True},
        )
        self.assertIsNone(exec_exc)
        self.assertNotIn(_COMPLETED_QUERY_PARAM, fake_st.query_params)
        fake_st.switch_page.assert_called_once_with("pages/Practice.py")

    def test_query_reference_takes_priority_over_session_marker(self):
        session_state = {
            _COMPLETED_STATE_KEY: _make_marker(attempt_id="22222222-2222-4222-8222-222222222222"),
        }
        exec_exc, _fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state=session_state,
            query_params={_COMPLETED_QUERY_PARAM: _ATTEMPT_ID},
        )
        self.assertIsNone(exec_exc)
        completion_mock.assert_called_once()
        _args, kwargs = completion_mock.call_args
        self.assertEqual(kwargs.get("attempt_id") or _args[-1], _ATTEMPT_ID)
        start_resume_mock.assert_not_called()

    def test_no_attempt_id_appears_in_learner_visible_text(self):
        exec_exc, fake_st, _completion_mock, _start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: _ATTEMPT_ID},
        )
        self.assertIsNone(exec_exc)
        visible = _visible_text(fake_st)
        self.assertNotIn(_ATTEMPT_ID, visible)

    # -- SIM-UI-04A: exact-one-value / repeated-key parsing ------------------

    def test_fake_query_params_get_returns_last_repeated_value(self):
        params = _FakeQueryParams({_COMPLETED_QUERY_PARAM: ["first-value", "second-value"]})
        self.assertEqual(params.get(_COMPLETED_QUERY_PARAM), "second-value")

    def test_fake_query_params_get_all_returns_all_values_in_order(self):
        params = _FakeQueryParams({_COMPLETED_QUERY_PARAM: [_ATTEMPT_ID, _OTHER_ATTEMPT_ID]})
        self.assertEqual(params.get_all(_COMPLETED_QUERY_PARAM), [_ATTEMPT_ID, _OTHER_ATTEMPT_ID])

    def test_two_different_valid_query_values_are_rejected(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: [_ATTEMPT_ID, _OTHER_ATTEMPT_ID]},
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_not_called()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)
        self.assertNotIn(_ATTEMPT_ID, visible)
        self.assertNotIn(_OTHER_ATTEMPT_ID, visible)

    def test_two_identical_valid_query_values_are_rejected(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: [_ATTEMPT_ID, _ATTEMPT_ID]},
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_not_called()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)
        self.assertNotIn(_ATTEMPT_ID, visible)

    def test_valid_value_followed_by_malformed_value_is_rejected(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: [_ATTEMPT_ID, "not-a-uuid"]},
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_not_called()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)
        self.assertNotIn(_ATTEMPT_ID, visible)
        self.assertNotIn("not-a-uuid", visible)

    def test_malformed_value_followed_by_valid_value_is_rejected(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: ["not-a-uuid", _ATTEMPT_ID]},
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_not_called()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)
        self.assertNotIn(_ATTEMPT_ID, visible)
        self.assertNotIn("not-a-uuid", visible)

    def test_empty_repeated_query_value_is_rejected(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: [""]},
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_not_called()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)

    def test_query_params_api_failure_does_not_fall_through_to_start_or_resume(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params_object=_BrokenQueryParams(),
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_not_called()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)

    def test_ambiguous_query_values_do_not_appear_in_learner_visible_text(self):
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: [_ATTEMPT_ID, "not-a-uuid"]},
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_not_called()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertNotIn(_ATTEMPT_ID, visible)
        self.assertNotIn("not-a-uuid", visible)


class DashboardComponentsModuleCacheIsolationTests(unittest.TestCase):
    """SIM-SMOKE-02E: proves `_exec_page_completion` correctly routes calls
    made through the already-imported, module-cached
    `utils.dashboard_components` -- including `render_empty_state()`, which
    renders the exact recovery message `_visible_text()` asserts against --
    to each test's own `fake_st`, instead of leaking through to the real,
    installed Streamlit module. This is a direct regression test for the
    failure mode described in SIM-SMOKE-02E: a pre-imported
    `utils.dashboard_components` (e.g. from `tests/test_session_restoration.py`
    running first) previously caused the recovery UI to be rendered through
    the wrong Streamlit object, leaving `_visible_text(fake_st)` empty."""

    def test_pre_imported_dashboard_components_module_is_the_real_one_before_exec(self):
        self.assertIn("utils.dashboard_components", sys.modules)
        self.assertIs(sys.modules["utils.dashboard_components"], dashboard_components)

    def test_dashboard_rendering_uses_the_exact_same_fake_st_as_the_page(self):
        real_streamlit_markdown = dashboard_components.st.markdown
        exec_exc, fake_st, _completion_mock, _start_resume_mock = _exec_page_completion(
            session_state={}, query_params={_COMPLETED_QUERY_PARAM: _ATTEMPT_ID}
        )
        self.assertIsNone(exec_exc)
        fake_st.markdown.assert_called()
        self.assertIsNot(fake_st.markdown, real_streamlit_markdown)

    def test_completion_recovery_message_is_captured_in_the_page_tests_own_fake(self):
        """Requirement 5: this reproduces
        `test_malformed_query_reference_rejected_without_backend_or_new_attempt`
        directly, proving the recovery message (rendered via
        `utils.dashboard_components.render_empty_state`) lands in THIS
        test's own `fake_st`, regardless of whether `utils.dashboard_components`
        was already cached and bound to the real Streamlit module beforehand."""
        exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: "not-a-uuid"},
        )
        self.assertIsInstance(exec_exc, SystemExit)
        completion_mock.assert_not_called()
        start_resume_mock.assert_not_called()
        visible = _visible_text(fake_st)
        self.assertIn("Scenario result unavailable", visible)

    def test_dashboard_components_st_restored_to_exact_prior_object_after_exec(self):
        prior_dashboard_st = dashboard_components.st
        _exec_page_completion(session_state={}, query_params={_COMPLETED_QUERY_PARAM: _ATTEMPT_ID})
        self.assertIs(dashboard_components.st, prior_dashboard_st)

    def test_dashboard_components_st_restored_even_when_page_raises_systemexit(self):
        prior_dashboard_st = dashboard_components.st
        exec_exc, _fake_st, _completion_mock, _start_resume_mock = _exec_page_completion(
            session_state={},
            query_params={_COMPLETED_QUERY_PARAM: "not-a-uuid"},
        )
        self.assertIsInstance(exec_exc, SystemExit)
        self.assertIs(dashboard_components.st, prior_dashboard_st)

    def test_real_streamlit_never_invoked_by_completion_page_tests(self):
        real_streamlit = sys.modules.get("streamlit")
        real_markdown = getattr(real_streamlit, "markdown", None)
        exec_exc, _fake_st, _completion_mock, _start_resume_mock = _exec_page_completion(
            session_state={}, query_params={_COMPLETED_QUERY_PARAM: _ATTEMPT_ID}
        )
        self.assertIsNone(exec_exc)
        self.assertIs(sys.modules.get("streamlit"), real_streamlit)
        self.assertIs(getattr(real_streamlit, "markdown", None), real_markdown)
        self.assertNotIsInstance(real_markdown, MagicMock)

    def test_result_independent_of_prior_dashboard_components_import_state(self):
        for _ in range(2):
            exec_exc, fake_st, completion_mock, start_resume_mock = _exec_page_completion(
                session_state={},
                query_params={_COMPLETED_QUERY_PARAM: "not-a-uuid"},
            )
            self.assertIsInstance(exec_exc, SystemExit)
            completion_mock.assert_not_called()
            start_resume_mock.assert_not_called()
            self.assertIn("Scenario result unavailable", _visible_text(fake_st))


if __name__ == "__main__":
    unittest.main()
