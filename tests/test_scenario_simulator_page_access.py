"""SIM-VSLICE-01B / SIM-VSLICE-01C: focused entitlement-gate tests for
`pages/Scenario_Simulator.py`.

Follows the one established precedent in this repository for executing a
Streamlit page module directly under test
(`tests/test_admin_question_review_readonly.py::test_page_imports_under_mock_streamlit`):
inject a minimal fake `streamlit` module via `sys.modules`, patch the
access-control / navigation / session-timeout / controller entry points the
page imports via `from ... import ...` (patched on their OWNING module so the
page's own `from`-imports pick up the patched object when the module is
freshly exec'd), then load and execute the real page file with
`importlib.util.spec_from_file_location`.

SIM-VSLICE-01C correction: the page now calls the CENTRALIZED
`utils.access_control.require_paid_access(...)` helper directly instead of
manually reproducing its internal `require_login()` +
`has_premium_access()` + `show_locked_premium_message()` sequence. These
tests therefore patch `require_paid_access` itself (never its internals) and
prove the actual, dynamic call order with a shared `events` list rather than
a brittle static `source.index(...)` search over the page's source text
(which could previously match a docstring instead of executable code).

SIM-VSLICE-01C controller-identity finding (see also the completion report):
`utils.scenario_learner_controller.start_or_resume_ba201_attempt` already
rejects a falsy/`"@"`-free `user_email` with `ScenarioLearnerAccessError`
*before* calling `utils.scenario_persistence.normalize_scenario_persistence_email`.
Given that guard, `normalize_scenario_persistence_email` cannot itself raise
for any value that reaches it at that call site (strip/lower can never
remove an already-confirmed `"@"` character or turn an already-confirmed
non-empty string empty), so no additional catch/conversion or focused
malformed-identity test was needed there -- this file accordingly adds no
test for that path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.access_control  # noqa: F401 -- ensure patch targets below are resolvable
import utils.dashboard_components as dashboard_components
import utils.navigation  # noqa: F401
import utils.session_timeout  # noqa: F401
from utils.scenario_learner_controller import (
    ScenarioAttemptView,
    ScenarioOptionView,
    ScenarioSceneView,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = REPO_ROOT / "pages" / "Scenario_Simulator.py"

_LEARNER_EMAIL = "learner@example.com"
_FEATURE_NAME = "Scenario Simulator"


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


class _FakeFormContext:
    """Minimal stand-in for the `with st.form(...):` context manager --
    SIM-VSLICE-02's decision form is now reached on the successful
    entitlement path, so this fake streamlit must support it even though
    these tests never exercise actual decision submission."""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _make_fake_streamlit():
    return types.SimpleNamespace(
        set_page_config=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        markdown=MagicMock(),
        caption=lambda *args, **kwargs: None,
        write=lambda *args, **kwargs: None,
        button=lambda *args, **kwargs: False,
        switch_page=lambda *args, **kwargs: None,
        form=lambda *args, **kwargs: _FakeFormContext(),
        radio=lambda _label, options, **kwargs: (list(options)[0] if options else None),
        form_submit_button=lambda *args, **kwargs: False,
        rerun=lambda: (_ for _ in ()).throw(SystemExit()),
        session_state={},
        query_params=_FakeQueryParams(),
        stop=lambda: (_ for _ in ()).throw(SystemExit()),
    )


def _fake_attempt_view() -> ScenarioAttemptView:
    return ScenarioAttemptView(
        attempt_id="11111111-1111-4111-8111-111111111111",
        is_new_attempt=True,
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


def _exec_page(
    *,
    events: List[Tuple[str, object]],
    paid_access_stops: bool,
    authenticated_email: str,
    fake_st_capture: Optional[list] = None,
):
    """Load and execute the real page file under a fully faked
    streamlit/access-control/navigation/controller boundary, recording each
    boundary call into the shared `events` list in the order it actually
    happens during the real page's top-to-bottom execution.

    `require_paid_access` is patched directly (never reimplemented) --
    `paid_access_stops=True` simulates it stopping the page itself (exactly
    what the real helper does for either an unauthenticated OR a
    non-premium learner; which of the two is irrelevant here, since this
    page must behave identically either way: stop before touching anything
    else). `authenticated_email=""` simulates `get_current_user_email()`
    returning no verified email even though `require_paid_access` itself
    succeeded.
    """
    fake_st = _make_fake_streamlit()
    if fake_st_capture is not None:
        fake_st_capture.append(fake_st)

    def _require_paid_access_side_effect(feature_name):
        events.append(("paid_access", feature_name))
        if paid_access_stops:
            fake_st.stop()
        return True

    def _get_current_user_email_side_effect():
        events.append(("email", authenticated_email))
        return authenticated_email or None

    def _controller_side_effect(email):
        events.append(("controller", email))
        return _fake_attempt_view()

    require_paid_access_mock = MagicMock(side_effect=_require_paid_access_side_effect)
    get_current_user_email_mock = MagicMock(side_effect=_get_current_user_email_side_effect)
    controller_mock = MagicMock(side_effect=_controller_side_effect)

    # SIM-SMOKE-02E: `utils.dashboard_components` (imported at module load
    # time above, while the real `streamlit` is still active) has its own
    # `import streamlit as st` binding that is completely unaffected by the
    # `patch.dict(sys.modules, {"streamlit": fake_st})` below -- that patch
    # only changes how *new* imports of `streamlit` resolve, not an
    # already-cached module's existing attribute. `pages/Scenario_Simulator.py`
    # calls `inject_certbound_theme()`/`render_page_header()`/
    # `render_empty_state()` from that same cached module, so without this
    # additional patch those calls would silently reach the REAL Streamlit
    # module (which then breaks internally, because `sys.modules["streamlit"]`
    # itself has been swapped to `fake_st`) instead of this test's own fake.
    with patch.dict(sys.modules, {"streamlit": fake_st}), \
         patch.object(dashboard_components, "st", fake_st):
        with patch("utils.access_control.require_paid_access", require_paid_access_mock), \
             patch("utils.access_control.get_current_user_email", get_current_user_email_mock), \
             patch("utils.access_control.render_app_chrome"), \
             patch("utils.session_timeout.enforce_session_timeout"), \
             patch("utils.session_timeout.show_session_expired_notice"), \
             patch("utils.navigation.is_feature_flag_enabled", return_value=True), \
             patch("utils.scenario_learner_controller.start_or_resume_ba201_attempt", controller_mock):
            spec = importlib.util.spec_from_file_location("scenario_simulator_page_under_test", PAGE_PATH)
            module = importlib.util.module_from_spec(spec)
            exec_exc = None
            try:
                spec.loader.exec_module(module)
            except SystemExit as exc:  # expected for every gated/denied path
                exec_exc = exc
            return exec_exc, require_paid_access_mock, get_current_user_email_mock, controller_mock


class ScenarioSimulatorEntitlementGateTests(unittest.TestCase):
    def test_require_paid_access_stopping_prevents_controller_access(self):
        """Requirement 1: `require_paid_access` stopping (unauthenticated OR
        non-premium -- the helper owns that distinction, not this page)
        prevents any learner-controller access."""
        events: List[Tuple[str, object]] = []
        exec_exc, paid_access_mock, email_mock, controller_mock = _exec_page(
            events=events, paid_access_stops=True, authenticated_email=_LEARNER_EMAIL
        )
        self.assertIsInstance(exec_exc, SystemExit)
        paid_access_mock.assert_called_once_with(_FEATURE_NAME)
        email_mock.assert_not_called()
        controller_mock.assert_not_called()

    def test_premium_access_with_verified_email_calls_controller_once(self):
        """Requirement 2: once `require_paid_access` succeeds and a verified
        email exists, the controller is called exactly once with that
        email."""
        events: List[Tuple[str, object]] = []
        exec_exc, paid_access_mock, email_mock, controller_mock = _exec_page(
            events=events, paid_access_stops=False, authenticated_email=_LEARNER_EMAIL
        )
        self.assertIsNone(exec_exc)
        paid_access_mock.assert_called_once_with(_FEATURE_NAME)
        email_mock.assert_called_once_with()
        controller_mock.assert_called_once_with(_LEARNER_EMAIL)

    def test_missing_email_after_paid_access_stops_before_controller(self):
        """Requirement 3: `require_paid_access` succeeds but
        `get_current_user_email()` unexpectedly returns no email -- the page
        must stop, and the controller must never be called."""
        events: List[Tuple[str, object]] = []
        exec_exc, paid_access_mock, email_mock, controller_mock = _exec_page(
            events=events, paid_access_stops=False, authenticated_email=""
        )
        self.assertIsInstance(exec_exc, SystemExit)
        paid_access_mock.assert_called_once_with(_FEATURE_NAME)
        email_mock.assert_called_once_with()
        controller_mock.assert_not_called()

    def test_require_paid_access_called_exactly_once_with_feature_name(self):
        """Requirement 4: `require_paid_access` is called exactly once, with
        the literal feature name `"Scenario Simulator"` -- on both the
        successful and the denied path."""
        for paid_access_stops in (False, True):
            with self.subTest(paid_access_stops=paid_access_stops):
                events: List[Tuple[str, object]] = []
                _exec_exc, paid_access_mock, _email_mock, _controller_mock = _exec_page(
                    events=events, paid_access_stops=paid_access_stops, authenticated_email=_LEARNER_EMAIL
                )
                paid_access_mock.assert_called_once_with(_FEATURE_NAME)

    def test_controller_never_called_before_entitlement_helper(self):
        """Requirement 5: the controller is never called before
        `require_paid_access` has already run, on any denied path."""
        for paid_access_stops, authenticated_email in ((True, _LEARNER_EMAIL), (False, "")):
            with self.subTest(paid_access_stops=paid_access_stops, authenticated_email=authenticated_email):
                events: List[Tuple[str, object]] = []
                _exec_exc, _paid_access_mock, _email_mock, controller_mock = _exec_page(
                    events=events, paid_access_stops=paid_access_stops, authenticated_email=authenticated_email
                )
                controller_mock.assert_not_called()
                event_names = [event[0] for event in events]
                self.assertIn("paid_access", event_names)
                self.assertNotIn("controller", event_names)

    def test_dynamic_call_order_is_paid_access_then_email_then_controller(self):
        """Requirement 6: on the successful path, the actual, dynamically
        recorded call order is exactly
        paid_access -> email -> controller."""
        events: List[Tuple[str, object]] = []
        exec_exc, _paid_access_mock, _email_mock, _controller_mock = _exec_page(
            events=events, paid_access_stops=False, authenticated_email=_LEARNER_EMAIL
        )
        self.assertIsNone(exec_exc)
        self.assertEqual([event[0] for event in events], ["paid_access", "email", "controller"])
        self.assertEqual(events[0], ("paid_access", _FEATURE_NAME))
        self.assertEqual(events[1], ("email", _LEARNER_EMAIL))
        self.assertEqual(events[2], ("controller", _LEARNER_EMAIL))


class DashboardComponentsModuleCacheIsolationTests(unittest.TestCase):
    """SIM-SMOKE-02E: proves `_exec_page` correctly routes calls made through
    the already-imported, module-cached `utils.dashboard_components` (e.g.
    `inject_certbound_theme()` / `render_page_header()`, both called
    unconditionally by the real page) to each test's own `fake_st`, instead
    of leaking through to the real, installed Streamlit module."""

    def test_pre_imported_dashboard_components_module_is_the_real_one_before_exec(self):
        """Requirement 1 setup check: this test file's module-level
        `import utils.dashboard_components as dashboard_components` (run
        while the real `streamlit` was active, long before any
        `patch.dict(sys.modules, ...)` in this file ever executes) is
        exactly the same cached module object every other test in this file
        already relies on -- i.e. the exact scenario the task describes as
        "pre-imported and cached"."""
        self.assertIn("utils.dashboard_components", sys.modules)
        self.assertIs(sys.modules["utils.dashboard_components"], dashboard_components)

    def test_dashboard_rendering_uses_the_exact_same_fake_st_as_the_page(self):
        """Requirements 1 & 2: with a pre-imported, cached
        `utils.dashboard_components` module already bound to the real
        `streamlit`, running the page must still route
        `inject_certbound_theme()` (called unconditionally at module import
        time by `pages/Scenario_Simulator.py`) through this test's own
        `fake_st.markdown`, never through the real Streamlit API."""
        real_streamlit_markdown = dashboard_components.st.markdown
        captured: list = []
        events: List[Tuple[str, object]] = []
        exec_exc, *_rest = _exec_page(
            events=events,
            paid_access_stops=True,
            authenticated_email=_LEARNER_EMAIL,
            fake_st_capture=captured,
        )
        self.assertIsInstance(exec_exc, SystemExit)
        self.assertEqual(len(captured), 1)
        fake_st = captured[0]
        fake_st.markdown.assert_called()
        self.assertIsNot(fake_st.markdown, real_streamlit_markdown)

    def test_dashboard_components_st_restored_to_exact_prior_object_after_exec(self):
        """Requirement 3: `utils.dashboard_components.st` must be restored to
        the exact object it pointed to before `_exec_page` ran, once
        `_exec_page` returns."""
        prior_dashboard_st = dashboard_components.st
        events: List[Tuple[str, object]] = []
        _exec_page(events=events, paid_access_stops=False, authenticated_email=_LEARNER_EMAIL)
        self.assertIs(dashboard_components.st, prior_dashboard_st)

    def test_dashboard_components_st_restored_even_when_page_raises_systemexit(self):
        """Requirement 4: the denied/gated path is the common case where the
        page itself raises `SystemExit` (via `st.stop()`) -- restoration of
        `utils.dashboard_components.st` must not depend on the page
        returning normally."""
        prior_dashboard_st = dashboard_components.st
        events: List[Tuple[str, object]] = []
        exec_exc, *_rest = _exec_page(
            events=events, paid_access_stops=True, authenticated_email=_LEARNER_EMAIL
        )
        self.assertIsInstance(exec_exc, SystemExit)
        self.assertIs(dashboard_components.st, prior_dashboard_st)

    def test_entitlement_test_never_invokes_real_streamlit(self):
        """Requirement 6: the entitlement-gate path never reaches the real,
        installed Streamlit module -- every call the page makes (directly,
        or indirectly through `utils.dashboard_components`) is captured by
        this test's own fake."""
        real_streamlit = sys.modules.get("streamlit")
        real_markdown = getattr(real_streamlit, "markdown", None)
        events: List[Tuple[str, object]] = []
        captured: list = []
        exec_exc, *_rest = _exec_page(
            events=events,
            paid_access_stops=True,
            authenticated_email=_LEARNER_EMAIL,
            fake_st_capture=captured,
        )
        self.assertIsInstance(exec_exc, SystemExit)
        self.assertIs(sys.modules.get("streamlit"), real_streamlit)
        self.assertIs(getattr(real_streamlit, "markdown", None), real_markdown)
        self.assertNotIsInstance(real_markdown, MagicMock)

    def test_result_independent_of_prior_dashboard_components_import_state(self):
        """Requirement 7: running this test class's checks twice in a row
        (simulating two different file-execution orders both observing an
        already-imported `utils.dashboard_components`) must behave
        identically both times -- no leftover state from the first run
        affects the second."""
        for _ in range(2):
            events: List[Tuple[str, object]] = []
            captured: list = []
            exec_exc, _paid_access_mock, _email_mock, controller_mock = _exec_page(
                events=events,
                paid_access_stops=False,
                authenticated_email=_LEARNER_EMAIL,
                fake_st_capture=captured,
            )
            self.assertIsNone(exec_exc)
            controller_mock.assert_called_once_with(_LEARNER_EMAIL)
            captured[0].markdown.assert_called()


if __name__ == "__main__":
    unittest.main()
