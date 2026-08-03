"""Focused tests for Engine V2 CB-SC-001 Streamlit vertical slice (corrections)."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import types
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.access_control  # noqa: F401
import utils.dashboard_components as dashboard_components
import utils.navigation  # noqa: F401
import utils.session_timeout  # noqa: F401
from utils.scenario_controller_v2 import (
    LearnerIdentityContextV2,
    LearnerScenarioControllerResultV2,
    ScenarioControllerV2AttemptNotFoundError,
    ScenarioControllerV2InvalidRequestError,
    ScenarioControllerV2PersistenceUnavailableError,
    ScenarioControllerV2StaleSessionError,
    ScenarioControllerV2TerminalAttemptError,
    start_or_resume_learner_scenario_v2,
    submit_learner_scenario_choice_v2,
)
from utils.scenario_streamlit_v2 import (
    ALLOWED_SESSION_KEYS,
    CB_SC001_CANONICAL_CONTENT_SHA256,
    CB_SC001_CONTENT_PATH,
    CB_SC001_SEMANTIC_VERSION,
    CB_SC001_SIMULATION_ID,
    MSG_PROGRESS_IN_PROGRESS,
    MSG_SCENARIO_UNAVAILABLE,
    MSG_STALE_SESSION,
    RETURN_TO_PRACTICE_PAGE_PATH,
    SESSION_KEY_ATTEMPT_ID,
    SESSION_KEY_PENDING_IDEMPOTENCY_KEY,
    SESSION_KEY_PENDING_OPTION_ID,
    SESSION_KEY_SCENARIO_VERSION_ID,
    SESSION_KEY_UI_MESSAGE,
    WIDGET_KEYS,
    UiMessageKind,
    assert_option_b_session_state_compliant,
    assert_return_to_practice_route_registered,
    assert_widget_keys_exclude_attempt_id,
    build_trusted_identity_v2,
    clear_v2_session_keys,
    collect_cb_sc001_v2_session_keys,
    controller_state_is_intentionally_not_serializable,
    diagnose_cb_sc001_publication_readiness,
    extract_progress_label,
    fetch_authoritative_cb_sc001_view,
    has_pending_submission,
    learner_safe_json_blob,
    load_cb_sc001_v2_content,
    prepare_return_to_practice_navigation,
    production_content_path_is_non_test,
    read_session_attempt_id,
    read_session_scenario_version_id,
    resolve_cb_sc001_scenario_version_id,
    start_new_cb_sc001_attempt_v2,
    streamlit_widget_keys,
    submit_cb_sc001_v2_choice,
    WIDGET_KEY_START_NEW,
)
from utils.navigation import route_for_page_path
from utils.scenario_orchestration_v2 import resolve_authoritative_attempt_ref_v2
from tests.test_scenario_orchestration_v2 import (
    HAPPY_PATH_DECISIONS,
    FakeOrchestrationPersistence,
    _FakeException,
    _SCENARIO_VERSION_ID,
    _new_attempt_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = REPO_ROOT / "pages" / "Scenario_Simulator_V2.py"
_LEARNER_EMAIL = "learner@example.com"
_LEARNER_B_EMAIL = "learner-b@example.com"
_SENSITIVE_SUBSTRINGS = (
    "sequence_mismatch",
    "scene_mismatch",
    "attempt_not_found",
    "postgresql://",
    "service_role",
    "eyJ",
    "33333333-3333-4333-8333-333333333333",
)


def _new_identity(*, email: str = _LEARNER_EMAIL, client: Any = "dummy-supabase-client") -> LearnerIdentityContextV2:
    return LearnerIdentityContextV2(user_email=email, supabase_client=client)


def _load_canonical_document() -> dict:
    return json.loads(CB_SC001_CONTENT_PATH.read_text(encoding="utf-8"))


def _new_content():
    from utils.scenario_engine_v2 import build_scenario_content_v2

    return build_scenario_content_v2(
        copy.deepcopy(_load_canonical_document()),
        source_path=CB_SC001_CONTENT_PATH,
    )


def _install_ownership_guard(persistence: FakeOrchestrationPersistence) -> None:
    """Make the in-memory fake enforce attempt ownership like production RPCs."""
    owners: Dict[str, str] = {}
    original_start = persistence.call_start_or_resume_scenario_attempt_v1
    original_load = persistence.load_attempt_snapshot

    def start_with_owner(params: Mapping[str, Any]):
        result = original_start(params)
        row = result[0] if isinstance(result, list) and result else {}
        attempt_key = row.get("attempt_id") or params.get("p_attempt_id")
        if attempt_key is not None:
            owners[str(attempt_key)] = str(params["p_user_email"])
        return result

    def load_with_owner(*, user_email: str, attempt_id: str):
        owner = owners.get(str(attempt_id))
        if owner is not None and owner != user_email:
            raise _FakeException("attempt_not_found: ownership")
        return original_load(user_email=user_email, attempt_id=attempt_id)

    persistence.call_start_or_resume_scenario_attempt_v1 = start_with_owner  # type: ignore[method-assign]
    persistence.load_attempt_snapshot = load_with_owner  # type: ignore[method-assign]


class _FakeFormContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _make_fake_streamlit(session_state: Optional[dict] = None):
    rendered: List[str] = []

    def _markdown(text, *args, **kwargs):
        rendered.append(str(text))

    def _write(text, *args, **kwargs):
        rendered.append(str(text))

    def _caption(text, *args, **kwargs):
        rendered.append(str(text))

    fake = types.SimpleNamespace(
        set_page_config=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        markdown=_markdown,
        caption=_caption,
        write=_write,
        button=lambda *args, **kwargs: False,
        switch_page=lambda *args, **kwargs: None,
        form=lambda *args, **kwargs: _FakeFormContext(),
        radio=lambda _label, options, **kwargs: (list(options)[0] if options else None),
        form_submit_button=lambda *args, **kwargs: False,
        rerun=lambda: (_ for _ in ()).throw(SystemExit()),
        session_state=session_state if session_state is not None else {},
        stop=lambda: (_ for _ in ()).throw(SystemExit()),
        _rendered=rendered,
        _widget_keys=[],
    )

    def _button(*args, **kwargs):
        key = kwargs.get("key")
        if key is not None:
            fake._widget_keys.append(str(key))
        return False

    def _form(*args, **kwargs):
        key = kwargs.get("key")
        if key is not None:
            fake._widget_keys.append(str(key))
        return _FakeFormContext()

    def _radio(_label, options, **kwargs):
        key = kwargs.get("key")
        if key is not None:
            fake._widget_keys.append(str(key))
        return list(options)[0] if options else None

    fake.button = _button
    fake.form = _form
    fake.radio = _radio
    return fake


class TestCanonicalProductionContent(unittest.TestCase):
    def test_1_runtime_path_not_under_tests_fixtures(self):
        self.assertTrue(production_content_path_is_non_test(CB_SC001_CONTENT_PATH))
        path_text = str(CB_SC001_CONTENT_PATH).replace("\\", "/")
        self.assertNotIn("/tests/fixtures/", path_text)
        self.assertNotIn("\\tests\\fixtures\\", str(CB_SC001_CONTENT_PATH))

    def test_2_canonical_asset_exists(self):
        self.assertTrue(CB_SC001_CONTENT_PATH.is_file())

    def test_3_4_5_6_canonical_identity_and_hash(self):
        content = load_cb_sc001_v2_content()
        self.assertEqual(content.simulation_id, CB_SC001_SIMULATION_ID)
        self.assertEqual(content.version, CB_SC001_SEMANTIC_VERSION)
        self.assertEqual(content.canonical_content_sha256, CB_SC001_CANONICAL_CONTENT_SHA256)

    def test_7_missing_canonical_asset_fails_closed(self):
        from utils.scenario_streamlit_v2 import ScenarioStreamlitV2ScenarioUnavailableError

        with patch("utils.scenario_streamlit_v2.CB_SC001_CONTENT_PATH") as fake_path:
            fake_path.is_file.return_value = False
            fake_path.resolve.return_value = CB_SC001_CONTENT_PATH.resolve()
            with self.assertRaises(ScenarioStreamlitV2ScenarioUnavailableError):
                # bypass production_content_path_is_non_test by patching loader internals
                with patch(
                    "utils.scenario_streamlit_v2.production_content_path_is_non_test",
                    return_value=True,
                ):
                    load_cb_sc001_v2_content()

    def test_source_module_has_no_tests_fixtures_path_literal(self):
        source = (REPO_ROOT / "utils" / "scenario_streamlit_v2.py").read_text(encoding="utf-8")
        self.assertNotIn("tests/fixtures", source)
        self.assertNotIn("tests\\fixtures", source)
        page = PAGE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("tests/fixtures", page)


class TestPublicationValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.content = load_cb_sc001_v2_content()
        self.client = MagicMock()

    def _rows(self, data):
        result = MagicMock()
        result.data = data
        return result

    def test_8_published_version_missing_fails_safely(self):
        from utils.scenario_streamlit_v2 import ScenarioStreamlitV2ScenarioUnavailableError

        table = MagicMock()
        self.client.table.return_value = table
        table.select.return_value = table
        table.eq.return_value = table
        table.limit.return_value = table
        table.execute.return_value = self._rows(
            [{"id": "sc-1", "is_active": True, "current_published_version_id": None, "simulation_id": CB_SC001_SIMULATION_ID}]
        )
        with self.assertRaises(ScenarioStreamlitV2ScenarioUnavailableError) as ctx:
            resolve_cb_sc001_scenario_version_id(self.client, content=self.content)
        self.assertEqual(str(ctx.exception), MSG_SCENARIO_UNAVAILABLE)

    def test_9_version_mismatch_fails_safely(self):
        from utils.scenario_streamlit_v2 import ScenarioStreamlitV2ScenarioUnavailableError

        scenario_exec = self._rows(
            [
                {
                    "id": "sc-1",
                    "is_active": True,
                    "current_published_version_id": _SCENARIO_VERSION_ID,
                    "simulation_id": CB_SC001_SIMULATION_ID,
                }
            ]
        )
        version_exec = self._rows(
            [
                {
                    "id": _SCENARIO_VERSION_ID,
                    "version": "9.9.9-wrong",
                    "canonical_content_sha256": CB_SC001_CANONICAL_CONTENT_SHA256,
                    "lifecycle_status": "published",
                    "scenario_id": "sc-1",
                }
            ]
        )
        calls = {"n": 0}

        def _execute():
            calls["n"] += 1
            return scenario_exec if calls["n"] == 1 else version_exec

        table = MagicMock()
        self.client.table.return_value = table
        table.select.return_value = table
        table.eq.return_value = table
        table.limit.return_value = table
        table.execute.side_effect = _execute
        with self.assertRaises(ScenarioStreamlitV2ScenarioUnavailableError):
            resolve_cb_sc001_scenario_version_id(self.client, content=self.content)

    def test_10_hash_mismatch_fails_safely(self):
        from utils.scenario_streamlit_v2 import ScenarioStreamlitV2ScenarioUnavailableError

        scenario_exec = self._rows(
            [
                {
                    "id": "sc-1",
                    "is_active": True,
                    "current_published_version_id": _SCENARIO_VERSION_ID,
                    "simulation_id": CB_SC001_SIMULATION_ID,
                }
            ]
        )
        version_exec = self._rows(
            [
                {
                    "id": _SCENARIO_VERSION_ID,
                    "version": CB_SC001_SEMANTIC_VERSION,
                    "canonical_content_sha256": "0" * 64,
                    "lifecycle_status": "published",
                    "scenario_id": "sc-1",
                }
            ]
        )
        calls = {"n": 0}

        def _execute():
            calls["n"] += 1
            return scenario_exec if calls["n"] == 1 else version_exec

        table = MagicMock()
        self.client.table.return_value = table
        table.select.return_value = table
        table.eq.return_value = table
        table.limit.return_value = table
        table.execute.side_effect = _execute
        with self.assertRaises(ScenarioStreamlitV2ScenarioUnavailableError):
            resolve_cb_sc001_scenario_version_id(self.client, content=self.content)

    def test_owner_diagnostic_reports_hash_mismatch_without_secrets(self):
        scenario_exec = self._rows(
            [
                {
                    "id": "sc-1",
                    "is_active": True,
                    "current_published_version_id": _SCENARIO_VERSION_ID,
                    "simulation_id": CB_SC001_SIMULATION_ID,
                }
            ]
        )
        version_exec = self._rows(
            [
                {
                    "id": _SCENARIO_VERSION_ID,
                    "version": CB_SC001_SEMANTIC_VERSION,
                    "canonical_content_sha256": "a" * 64,
                    "lifecycle_status": "published",
                    "scenario_id": "sc-1",
                }
            ]
        )
        calls = {"n": 0}

        def _execute():
            calls["n"] += 1
            return scenario_exec if calls["n"] == 1 else version_exec

        table = MagicMock()
        self.client.table.return_value = table
        table.select.return_value = table
        table.eq.return_value = table
        table.limit.return_value = table
        table.execute.side_effect = _execute
        result = diagnose_cb_sc001_publication_readiness(
            self.client,
            content=self.content,
            supabase_url="http://127.0.0.1:54321",
        )
        self.assertFalse(result.ready)
        self.assertIn("canonical_content_hash_mismatch", result.findings)
        blob = json.dumps(result.findings)
        self.assertNotIn("service_role", blob)
        self.assertTrue(result.target_appears_non_production)


class TestOptionBSessionAndWidgets(unittest.TestCase):
    def test_f_only_allowed_keys(self):
        session: Dict[str, Any] = {
            SESSION_KEY_ATTEMPT_ID: _new_attempt_id(),
            SESSION_KEY_SCENARIO_VERSION_ID: _SCENARIO_VERSION_ID,
            SESSION_KEY_PENDING_IDEMPOTENCY_KEY: str(uuid.uuid4()),
            SESSION_KEY_PENDING_OPTION_ID: "opt-a",
        }
        keys = collect_cb_sc001_v2_session_keys(session)
        self.assertTrue(set(keys).issubset(ALLOWED_SESSION_KEYS))

    def test_11_tampered_session_scenario_version_id_ignored(self):
        content = _new_content()
        persistence = FakeOrchestrationPersistence(content=content)
        identity = _new_identity()
        session: Dict[str, Any] = {}
        view = fetch_authoritative_cb_sc001_view(
            content=content,
            identity=identity,
            session_state=session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=persistence,
        )
        session[SESSION_KEY_SCENARIO_VERSION_ID] = "00000000-0000-4000-8000-000000000099"
        recovered = fetch_authoritative_cb_sc001_view(
            content=content,
            identity=identity,
            session_state=session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=persistence,
        )
        self.assertEqual(recovered.attempt_id, view.attempt_id)
        self.assertEqual(read_session_scenario_version_id(session), _SCENARIO_VERSION_ID)
        self.assertEqual(len(persistence.start_calls), 1)

    def test_12_attempt_id_absent_from_widget_keys(self):
        attempt_id = _new_attempt_id()
        assert_widget_keys_exclude_attempt_id(attempt_id, streamlit_widget_keys())
        for key in WIDGET_KEYS:
            self.assertNotIn(attempt_id, key)

    def test_14_expected_sequence_absent_from_progress_display(self):
        label = extract_progress_label(
            {"isComplete": False, "currentScene": {"title": "X"}, "expectedSequenceNumber": 3}
        )
        self.assertEqual(label, MSG_PROGRESS_IN_PROGRESS)
        self.assertNotIn("3", label)
        self.assertNotIn("Decision", label)
        self.assertNotIn("expectedSequence", label)


class TestAuthoritativeFetchAndSubmit(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _new_content()
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.identity = _new_identity()
        self.session: Dict[str, Any] = {}

    def test_d_first_visit_starts_one_attempt(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertTrue(view.is_new_attempt)
        self.assertEqual(len(self.persistence.start_calls), 1)
        self.assertEqual(read_session_attempt_id(self.session), view.attempt_id)

    def test_e_rerun_with_attempt_id_resumes(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.persistence.start_calls.clear()
        second = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertFalse(second.is_new_attempt)
        self.assertEqual(len(self.persistence.start_calls), 0)
        self.assertEqual(first.attempt_id, second.attempt_id)

    def test_m_one_submit_one_orchestration_call(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        option_id = view.serialized["currentScene"]["options"][0]["id"]
        self.persistence.submit_calls.clear()
        submit_cb_sc001_v2_choice(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            selected_option_id=option_id,
            persistence=self.persistence,
        )
        self.assertEqual(len(self.persistence.submit_calls), 1)

    def test_n_idempotency_preserved_for_retry(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        option_id = view.serialized["currentScene"]["options"][0]["id"]
        self.persistence.submit_raise = "connection reset"
        first = submit_cb_sc001_v2_choice(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            selected_option_id=option_id,
            persistence=self.persistence,
        )
        self.assertFalse(first.conclusive)
        self.persistence.submit_raise = None
        retry = submit_cb_sc001_v2_choice(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            retry_pending=True,
            persistence=self.persistence,
        )
        self.assertEqual(retry.idempotency_key, first.idempotency_key)

    def test_l_unknown_option_rejected_before_submit(self):
        fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.persistence.submit_calls.clear()
        with self.assertRaises(ScenarioControllerV2InvalidRequestError):
            submit_cb_sc001_v2_choice(
                content=self.content,
                identity=self.identity,
                session_state=self.session,
                scenario_version_id=_SCENARIO_VERSION_ID,
                selected_option_id="totally-unknown-option",
                persistence=self.persistence,
            )
        self.assertEqual(len(self.persistence.submit_calls), 0)

    def test_p_stale_conflict_does_not_auto_resubmit(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        option_id = view.serialized["currentScene"]["options"][0]["id"]
        with patch(
            "utils.scenario_streamlit_v2.submit_learner_scenario_choice_v2",
            side_effect=ScenarioControllerV2StaleSessionError("stale"),
        ):
            outcome = submit_cb_sc001_v2_choice(
                content=self.content,
                identity=self.identity,
                session_state=self.session,
                scenario_version_id=_SCENARIO_VERSION_ID,
                selected_option_id=option_id,
                persistence=self.persistence,
            )
        self.assertTrue(outcome.stale_session)
        self.assertFalse(has_pending_submission(self.session))
        self.assertEqual(outcome.ui_message.text, MSG_STALE_SESSION)

    def test_20_same_option_concurrent_submit_cannot_duplicate(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        option_id = view.serialized["currentScene"]["options"][0]["id"]
        session_a = dict(self.session)
        session_b = dict(self.session)
        first = submit_cb_sc001_v2_choice(
            content=self.content,
            identity=self.identity,
            session_state=session_a,
            scenario_version_id=_SCENARIO_VERSION_ID,
            selected_option_id=option_id,
            persistence=self.persistence,
        )
        self.assertTrue(first.conclusive)
        # Second tab retries same option with a new idempotency key after first advanced:
        # resume sees new scene; original option is no longer visible → rejected before persistence.
        self.persistence.submit_calls.clear()
        with self.assertRaises(ScenarioControllerV2InvalidRequestError):
            submit_cb_sc001_v2_choice(
                content=self.content,
                identity=self.identity,
                session_state=session_b,
                scenario_version_id=_SCENARIO_VERSION_ID,
                selected_option_id=option_id,
                persistence=self.persistence,
            )
        self.assertEqual(len(self.persistence.submit_calls), 0)

    def test_21_different_option_concurrent_submit_maps_stale_safely(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        options = [o["id"] for o in view.serialized["currentScene"]["options"]]
        self.assertGreaterEqual(len(options), 2)
        session_a = dict(self.session)
        session_b = dict(self.session)
        submit_cb_sc001_v2_choice(
            content=self.content,
            identity=self.identity,
            session_state=session_a,
            scenario_version_id=_SCENARIO_VERSION_ID,
            selected_option_id=options[0],
            persistence=self.persistence,
        )
        with patch(
            "utils.scenario_streamlit_v2.submit_learner_scenario_choice_v2",
            side_effect=ScenarioControllerV2StaleSessionError("stale"),
        ):
            # Force stale after resume validation by using a currently visible option
            # on the advanced scene for tab B after re-fetch would happen; simulate
            # mid-flight stale from CAS when both tabs still held scene-1 state.
            session_b[SESSION_KEY_ATTEMPT_ID] = session_a[SESSION_KEY_ATTEMPT_ID]
            current = fetch_authoritative_cb_sc001_view(
                content=self.content,
                identity=self.identity,
                session_state=session_b,
                scenario_version_id=_SCENARIO_VERSION_ID,
                persistence=self.persistence,
            )
            visible = current.serialized["currentScene"]["options"][0]["id"]
            outcome = submit_cb_sc001_v2_choice(
                content=self.content,
                identity=self.identity,
                session_state=session_b,
                scenario_version_id=_SCENARIO_VERSION_ID,
                selected_option_id=visible,
                persistence=self.persistence,
            )
        self.assertTrue(outcome.stale_session)
        self.assertFalse(has_pending_submission(session_b))
        self.assertEqual(outcome.ui_message.text, MSG_STALE_SESSION)

    def test_18_identity_swap_fails_closed(self):
        _install_ownership_guard(self.persistence)
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        retained_attempt = view.attempt_id
        other = _new_identity(email=_LEARNER_B_EMAIL)
        with self.assertRaises(ScenarioControllerV2AttemptNotFoundError):
            fetch_authoritative_cb_sc001_view(
                content=self.content,
                identity=other,
                session_state=self.session,
                scenario_version_id=_SCENARIO_VERSION_ID,
                persistence=self.persistence,
            )
        self.assertIsNone(read_session_attempt_id(self.session))
        self.assertNotIn(retained_attempt, json.dumps(dict(self.session)))

    def test_19_cross_user_submit_clears_session_and_does_not_persist(self):
        _install_ownership_guard(self.persistence)
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        option_id = view.serialized["currentScene"]["options"][0]["id"]
        other = _new_identity(email=_LEARNER_B_EMAIL)
        self.persistence.submit_calls.clear()
        with self.assertRaises(ScenarioControllerV2AttemptNotFoundError):
            submit_cb_sc001_v2_choice(
                content=self.content,
                identity=other,
                session_state=self.session,
                scenario_version_id=_SCENARIO_VERSION_ID,
                selected_option_id=option_id,
                persistence=self.persistence,
            )
        self.assertEqual(len(self.persistence.submit_calls), 0)
        self.assertIsNone(read_session_attempt_id(self.session))

    def test_t_process_loss_with_attempt_id_only(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        option_id = view.serialized["currentScene"]["options"][0]["id"]
        submit_cb_sc001_v2_choice(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            selected_option_id=option_id,
            persistence=self.persistence,
        )
        retained = {SESSION_KEY_ATTEMPT_ID: self.session[SESSION_KEY_ATTEMPT_ID]}
        recovered = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=retained,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertFalse(recovered.serialized["isComplete"])
        self.assertEqual(recovered.serialized["expectedSequenceNumber"], 2)

    def test_u_completed_attempt_resumes_terminal_view(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        current = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=view.attempt_id,
            persistence=self.persistence,
        )
        for _, _, option in HAPPY_PATH_DECISIONS:
            current = submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=current.state,
                selected_option_id=option,
                persistence=self.persistence,
            )
        self.session[SESSION_KEY_ATTEMPT_ID] = current.state.attempt_id
        terminal_view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertTrue(terminal_view.serialized["isComplete"])
        self.assertIn("terminalResult", terminal_view.serialized)

    def test_v_terminal_view_cannot_submit(self):
        controller_result = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=self.persistence,
        )
        current = controller_result
        for _, _, option in HAPPY_PATH_DECISIONS:
            current = submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=current.state,
                selected_option_id=option,
                persistence=self.persistence,
            )
        self.session[SESSION_KEY_ATTEMPT_ID] = current.state.attempt_id
        self.persistence.submit_calls.clear()
        with self.assertRaises(ScenarioControllerV2TerminalAttemptError):
            submit_cb_sc001_v2_choice(
                content=self.content,
                identity=self.identity,
                session_state=self.session,
                scenario_version_id=_SCENARIO_VERSION_ID,
                selected_option_id="opt-sc001-c01-a",
                persistence=self.persistence,
            )
        self.assertEqual(len(self.persistence.submit_calls), 0)


class TestLearnerOutputSafety(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _new_content()
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.identity = _new_identity()
        self.session: Dict[str, Any] = {}

    def test_j_hidden_fields_never_in_serialized_output(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        blob = learner_safe_json_blob(view.serialized)
        for hidden in (
            "evaluationTier",
            "stateChanges",
            "debriefSeed",
            "routing",
            "content_hash",
            "engineVersion",
            "attemptId",
        ):
            self.assertNotIn(hidden, blob)
        self.assertNotIn(self.content.canonical_content_sha256, blob)

    def test_w_attempt_id_not_in_learner_blob(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        blob = learner_safe_json_blob(view.serialized)
        self.assertNotIn(view.attempt_id, blob)

    def test_r_persistence_error_displays_safe_message(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        option_id = view.serialized["currentScene"]["options"][0]["id"]
        self.persistence.submit_raise = "connection reset at postgresql://host:5432"
        outcome = submit_cb_sc001_v2_choice(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            selected_option_id=option_id,
            persistence=self.persistence,
        )
        self.assertFalse(outcome.conclusive)
        self.assertNotIn("postgresql://", outcome.ui_message.text)


class TestControllerRegressionHelpers(unittest.TestCase):
    def test_y_controller_state_not_serializable_helper(self):
        content = _new_content()
        persistence = FakeOrchestrationPersistence(content=content)
        identity = _new_identity()
        result = start_or_resume_learner_scenario_v2(
            content,
            identity=identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=persistence,
        )
        ok, _reason = controller_state_is_intentionally_not_serializable(result.state)
        self.assertTrue(ok)


class TestEngineV1Isolation(unittest.TestCase):
    def test_x_v1_session_keys_remain_untouched(self):
        session = {"ba201_pending_decision": "legacy", "cb_sc001_v2_attempt_id": _new_attempt_id()}
        keys = collect_cb_sc001_v2_session_keys(session)
        self.assertEqual(keys, ("cb_sc001_v2_attempt_id",))

    def test_x_v1_controller_not_imported_by_streamlit_helper(self):
        import utils.scenario_streamlit_v2 as streamlit_v2

        self.assertNotIn("scenario_learner_controller", vars(streamlit_v2))


def _exec_v2_page(
    *,
    paid_access_stops: bool,
    authenticated_email: str,
    session_state: Optional[dict] = None,
    feature_flag_enabled: bool = True,
    track_admin_client: Optional[list] = None,
) -> Tuple[Optional[SystemExit], Any]:
    fake_st = _make_fake_streamlit(session_state=session_state or {})

    def _require_paid_access(_feature_name):
        if paid_access_stops:
            fake_st.stop()
        return True

    def _get_current_user_email():
        return authenticated_email or None

    def _get_admin():
        if track_admin_client is not None:
            track_admin_client.append(True)
        return "supabase-client"

    with patch.dict(sys.modules, {"streamlit": fake_st}), patch.object(dashboard_components, "st", fake_st):
        with patch("utils.access_control.require_paid_access", side_effect=_require_paid_access), \
             patch("utils.access_control.get_current_user_email", side_effect=_get_current_user_email), \
             patch("utils.access_control.render_app_chrome"), \
             patch("utils.session_timeout.enforce_session_timeout"), \
             patch("utils.session_timeout.show_session_expired_notice"), \
             patch("utils.navigation.is_feature_flag_enabled", return_value=feature_flag_enabled), \
             patch("utils.scenario_streamlit_v2.load_cb_sc001_v2_content") as load_content, \
             patch("utils.scenario_streamlit_v2.resolve_cb_sc001_scenario_version_id", return_value=_SCENARIO_VERSION_ID), \
             patch("utils.scenario_streamlit_v2.fetch_authoritative_cb_sc001_view") as fetch_view, \
             patch("utils.access_control.get_supabase_admin_client", side_effect=_get_admin):
            content = _new_content()
            load_content.return_value = content
            fetch_view.return_value = types.SimpleNamespace(
                serialized={
                    "isComplete": False,
                    "currentScene": {
                        "title": "Scene 1",
                        "setting": "Office",
                        "dialogueExchanges": [{"speakerDisplayName": "Elena", "text": "Hello"}],
                        "decisionPrompt": "What do you do?",
                        "options": [{"id": "opt-a", "title": "Option A", "text": "Do A"}],
                        "progressMetadata": {"progressLabel": "Step 1"},
                    },
                    "expectedSequenceNumber": 1,
                },
                attempt_id="11111111-1111-4111-8111-111111111111",
                is_new_attempt=True,
                scenario_title="Customer Onboarding Handoff (Vertical Slice)",
            )
            spec = importlib.util.spec_from_file_location("scenario_simulator_v2_page_under_test", PAGE_PATH)
            module = importlib.util.module_from_spec(spec)
            exec_exc = None
            try:
                spec.loader.exec_module(module)
            except SystemExit as exc:
                exec_exc = exc
            return exec_exc, fake_st


class TestScenarioSimulatorV2PageAccess(unittest.TestCase):
    def test_a_unauthenticated_page_access_fails_closed(self):
        exec_exc, fake_st = _exec_v2_page(paid_access_stops=True, authenticated_email=_LEARNER_EMAIL)
        self.assertIsInstance(exec_exc, SystemExit)
        self.assertNotIn(SESSION_KEY_ATTEMPT_ID, fake_st.session_state)

    def test_15_feature_flag_off_prevents_initialization(self):
        admin_calls: list = []
        exec_exc, fake_st = _exec_v2_page(
            paid_access_stops=False,
            authenticated_email=_LEARNER_EMAIL,
            feature_flag_enabled=False,
            track_admin_client=admin_calls,
        )
        self.assertIsInstance(exec_exc, SystemExit)
        self.assertEqual(admin_calls, [])
        self.assertNotIn(SESSION_KEY_ATTEMPT_ID, fake_st.session_state)

    def test_16_direct_route_cannot_bypass_feature_flag(self):
        # Same page module is the direct-route target; flag off still stops first.
        self.test_15_feature_flag_off_prevents_initialization()

    def test_17_premium_access_still_required_when_flag_on(self):
        exec_exc, fake_st = _exec_v2_page(
            paid_access_stops=True,
            authenticated_email=_LEARNER_EMAIL,
            feature_flag_enabled=True,
        )
        self.assertIsInstance(exec_exc, SystemExit)
        self.assertNotIn(SESSION_KEY_ATTEMPT_ID, fake_st.session_state)

    def test_13_attempt_id_absent_from_rendered_text_and_widget_keys(self):
        attempt_id = "11111111-1111-4111-8111-111111111111"
        exec_exc, fake_st = _exec_v2_page(
            paid_access_stops=False,
            authenticated_email=_LEARNER_EMAIL,
            feature_flag_enabled=True,
        )
        self.assertIsNone(exec_exc)
        rendered = "\n".join(fake_st._rendered)
        self.assertNotIn(attempt_id, rendered)
        for key in fake_st._widget_keys:
            self.assertNotIn(attempt_id, key)
        assert_widget_keys_exclude_attempt_id(attempt_id, fake_st._widget_keys)


class TestBrowserRefreshAndReturnToPractice(unittest.TestCase):
    """SIM-STREAMLIT-V2-CB-SC-001-BROWSER-REFRESH-FIX-01 regressions."""

    def setUp(self) -> None:
        self.content = _new_content()
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.identity = _new_identity()
        self.session: Dict[str, Any] = {}

    def test_active_rerun_resumes_same_scene(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertFalse(first.serialized.get("isComplete"))
        scene_before = first.serialized["currentScene"]["sceneId"]
        self.persistence.start_calls.clear()
        second = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertEqual(second.attempt_id, first.attempt_id)
        self.assertEqual(second.serialized["currentScene"]["sceneId"], scene_before)
        self.assertEqual(len(self.persistence.start_calls), 0)
        self.assertEqual(len(self.persistence.submit_calls), 0)

    def test_browser_refresh_without_session_attempt_id_does_not_stale(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        attempt_id = first.attempt_id
        scene_id = first.serialized["currentScene"]["sceneId"]
        # Simulate browser session loss: only cosmetic widget residue remains.
        lost_session: Dict[str, Any] = {"cb_sc001_v2_widget_choice": "stale-cosmetic"}
        self.persistence.start_calls.clear()
        recovered = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=lost_session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertEqual(recovered.attempt_id, attempt_id)
        self.assertEqual(recovered.serialized["currentScene"]["sceneId"], scene_id)
        self.assertNotEqual(recovered.serialized.get("uiMessage"), MSG_STALE_SESSION)
        self.assertEqual(len(self.persistence.attempts), 1)
        self.assertEqual(len(self.persistence.submit_calls), 0)
        self.assertEqual(read_session_attempt_id(lost_session), attempt_id)
        assert_option_b_session_state_compliant(lost_session)

    def test_refresh_creates_no_duplicate_attempt_or_decision(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        option_id = first.serialized["currentScene"]["options"][0]["id"]
        submit_cb_sc001_v2_choice(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            selected_option_id=option_id,
            persistence=self.persistence,
        )
        decision_count = len(self.persistence.decisions[first.attempt_id])
        for _ in range(2):
            empty: Dict[str, Any] = {}
            view = fetch_authoritative_cb_sc001_view(
                content=self.content,
                identity=self.identity,
                session_state=empty,
                scenario_version_id=_SCENARIO_VERSION_ID,
                persistence=self.persistence,
            )
            self.assertEqual(view.attempt_id, first.attempt_id)
            self.assertEqual(len(self.persistence.attempts), 1)
            self.assertEqual(len(self.persistence.decisions[first.attempt_id]), decision_count)

    def test_attempt_id_only_resume_succeeds(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        retained = {SESSION_KEY_ATTEMPT_ID: first.attempt_id}
        resumed = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=retained,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertEqual(resumed.attempt_id, first.attempt_id)
        self.assertFalse(resumed.serialized.get("isComplete"))

    def test_stale_cosmetic_selection_cleared_on_authoritative_load(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.session[SESSION_KEY_UI_MESSAGE] = MSG_STALE_SESSION
        self.session[SESSION_KEY_PENDING_OPTION_ID] = "opt-stale-cosmetic"
        # Pending without idempotency key is incomplete; authoritative load clears banners.
        fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertNotIn(SESSION_KEY_UI_MESSAGE, self.session)
        self.assertEqual(read_session_attempt_id(self.session), first.attempt_id)

    def test_real_stale_submit_still_maps_and_does_not_auto_retry(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        option_id = view.serialized["currentScene"]["options"][0]["id"]
        with patch(
            "utils.scenario_streamlit_v2.submit_learner_scenario_choice_v2",
            side_effect=ScenarioControllerV2StaleSessionError("stale"),
        ) as mocked_submit:
            outcome = submit_cb_sc001_v2_choice(
                content=self.content,
                identity=self.identity,
                session_state=self.session,
                scenario_version_id=_SCENARIO_VERSION_ID,
                selected_option_id=option_id,
                persistence=self.persistence,
            )
        self.assertTrue(outcome.stale_session)
        self.assertEqual(outcome.ui_message.text, MSG_STALE_SESSION)
        self.assertEqual(mocked_submit.call_count, 1)
        self.assertFalse(has_pending_submission(self.session))

    def test_completed_attempt_refresh_resumes_terminal(self):
        view = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        current = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=view.attempt_id,
            persistence=self.persistence,
        )
        for _, _, option in HAPPY_PATH_DECISIONS:
            current = submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=current.state,
                selected_option_id=option,
                persistence=self.persistence,
            )
        attempts_before = len(self.persistence.attempts)
        decisions_before = len(self.persistence.decisions[view.attempt_id])
        empty: Dict[str, Any] = {}
        terminal = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=empty,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertEqual(terminal.attempt_id, view.attempt_id)
        self.assertTrue(terminal.serialized.get("isComplete"))
        self.assertIn("terminalResult", terminal.serialized)
        self.assertEqual(len(self.persistence.attempts), attempts_before)
        self.assertEqual(len(self.persistence.decisions[view.attempt_id]), decisions_before)
        blob = learner_safe_json_blob(terminal.serialized)
        self.assertNotIn(view.attempt_id, blob)
        for sensitive in ("sequence_mismatch", "postgresql://", "service_role"):
            self.assertNotIn(sensitive, blob)

    def test_return_to_practice_clears_only_v2_keys_and_targets_registered_page(self):
        auth_marker = "auth_session_should_remain"
        self.session[SESSION_KEY_ATTEMPT_ID] = str(uuid.uuid4())
        self.session[SESSION_KEY_SCENARIO_VERSION_ID] = _SCENARIO_VERSION_ID
        self.session[SESSION_KEY_PENDING_IDEMPOTENCY_KEY] = str(uuid.uuid4())
        self.session[SESSION_KEY_PENDING_OPTION_ID] = "opt-x"
        self.session[auth_marker] = "keep-me"
        destination = prepare_return_to_practice_navigation(self.session)
        self.assertEqual(destination, RETURN_TO_PRACTICE_PAGE_PATH)
        assert_return_to_practice_route_registered()
        self.assertIsNotNone(route_for_page_path(RETURN_TO_PRACTICE_PAGE_PATH))
        self.assertEqual(collect_cb_sc001_v2_session_keys(self.session), ())
        self.assertEqual(self.session.get(auth_marker), "keep-me")
        # Navigation helper never deletes persistence rows.
        self.assertEqual(len(self.persistence.attempts), 0)
        self.assertEqual(len(self.persistence.decisions), 0)


class TestTerminalRefreshAndNewAttempt(unittest.TestCase):
    """SIM-STREAMLIT-V2-CB-SC-001-TERMINAL-REFRESH-FIX-02 regressions."""

    def setUp(self) -> None:
        self.content = _new_content()
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.identity = _new_identity()
        self.session: Dict[str, Any] = {}

    def _complete_current(self, attempt_id: str) -> None:
        current = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=attempt_id,
            persistence=self.persistence,
        )
        for _, _, option in HAPPY_PATH_DECISIONS:
            current = submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=current.state,
                selected_option_id=option,
                persistence=self.persistence,
            )
        self.assertTrue(current.state.is_complete)

    def test_completed_session_loss_creates_no_attempt_or_decision(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self._complete_current(first.attempt_id)
        before_attempts = len(self.persistence.attempts)
        before_decisions = sum(len(v) for v in self.persistence.decisions.values())
        self.persistence.start_calls.clear()
        empty: Dict[str, Any] = {}
        terminal = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=empty,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertTrue(terminal.serialized.get("isComplete"))
        self.assertEqual(terminal.attempt_id, first.attempt_id)
        self.assertEqual(len(self.persistence.attempts), before_attempts)
        self.assertEqual(sum(len(v) for v in self.persistence.decisions.values()), before_decisions)
        self.assertEqual(len(self.persistence.start_calls), 0)  # session-loss path resumes via lookup

    def test_no_attempt_id_in_progress_preferred_over_completed(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self._complete_current(first.attempt_id)
        # Deliberate new attempt leaves one completed + one in_progress.
        active = start_new_cb_sc001_attempt_v2(
            content=self.content,
            identity=self.identity,
            session_state={},
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertFalse(active.serialized.get("isComplete"))
        empty: Dict[str, Any] = {}
        resumed = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=empty,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertEqual(resumed.attempt_id, active.attempt_id)
        self.assertFalse(resumed.serialized.get("isComplete"))

    def test_multiple_completed_resumes_most_recent(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state={},
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self._complete_current(first.attempt_id)
        self.persistence.attempts[first.attempt_id]["completed_at"] = "2026-08-01T00:05:00Z"
        second = start_new_cb_sc001_attempt_v2(
            content=self.content,
            identity=self.identity,
            session_state={},
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self._complete_current(second.attempt_id)
        self.persistence.attempts[second.attempt_id]["completed_at"] = "2026-08-01T01:00:00Z"
        empty: Dict[str, Any] = {}
        latest = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=empty,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertEqual(latest.attempt_id, second.attempt_id)
        self.assertTrue(latest.serialized.get("isComplete"))

    def test_explicit_start_new_creates_one_and_preserves_completed(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self._complete_current(first.attempt_id)
        completed_status = self.persistence.attempts[first.attempt_id]["status"]
        self.assertEqual(completed_status, "completed")
        created = start_new_cb_sc001_attempt_v2(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertNotEqual(created.attempt_id, first.attempt_id)
        self.assertTrue(created.is_new_attempt)
        self.assertFalse(created.serialized.get("isComplete"))
        self.assertEqual(self.persistence.attempts[first.attempt_id]["status"], "completed")
        self.assertEqual(len(self.persistence.attempts), 2)

    def test_start_new_double_invocation_does_not_duplicate(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state={},
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self._complete_current(first.attempt_id)
        session: Dict[str, Any] = {}
        one = start_new_cb_sc001_attempt_v2(
            content=self.content,
            identity=self.identity,
            session_state=session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        two = start_new_cb_sc001_attempt_v2(
            content=self.content,
            identity=self.identity,
            session_state=session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertEqual(one.attempt_id, two.attempt_id)
        in_progress = [
            row for row in self.persistence.attempts.values() if row["status"] == "in_progress"
        ]
        self.assertEqual(len(in_progress), 1)

    def test_start_new_while_in_progress_resumes_existing(self):
        active = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state={},
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        again = start_new_cb_sc001_attempt_v2(
            content=self.content,
            identity=self.identity,
            session_state={},
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self.assertEqual(again.attempt_id, active.attempt_id)
        self.assertEqual(len(self.persistence.attempts), 1)

    def test_multiple_in_progress_fails_closed(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state={},
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        # Force an integrity-damaged second in_progress row.
        twin_id = str(uuid.uuid4())
        self.persistence.attempts[twin_id] = copy.deepcopy(self.persistence.attempts[first.attempt_id])
        self.persistence.attempts[twin_id]["attempt_id"] = twin_id
        self.persistence.attempts[twin_id]["user_email"] = _LEARNER_EMAIL
        self.persistence.decisions[twin_id] = []
        from utils.scenario_streamlit_v2 import ScenarioStreamlitV2ScenarioUnavailableError

        with self.assertRaises(ScenarioStreamlitV2ScenarioUnavailableError):
            fetch_authoritative_cb_sc001_view(
                content=self.content,
                identity=self.identity,
                session_state={},
                scenario_version_id=_SCENARIO_VERSION_ID,
                persistence=self.persistence,
            )

    def test_return_to_practice_does_not_create_attempt(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state=self.session,
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        before = len(self.persistence.attempts)
        destination = prepare_return_to_practice_navigation(self.session)
        self.assertEqual(destination, RETURN_TO_PRACTICE_PAGE_PATH)
        self.assertEqual(len(self.persistence.attempts), before)
        self.assertEqual(first.attempt_id, first.attempt_id)
        self.assertNotIn(WIDGET_KEY_START_NEW, collect_cb_sc001_v2_session_keys(self.session))

    def test_start_new_widget_key_excludes_attempt_id(self):
        assert_widget_keys_exclude_attempt_id(str(uuid.uuid4()), WIDGET_KEYS)
        self.assertIn(WIDGET_KEY_START_NEW, WIDGET_KEYS)

    def test_resolve_authoritative_ref_completed_only(self):
        first = fetch_authoritative_cb_sc001_view(
            content=self.content,
            identity=self.identity,
            session_state={},
            scenario_version_id=_SCENARIO_VERSION_ID,
            persistence=self.persistence,
        )
        self._complete_current(first.attempt_id)
        ref = resolve_authoritative_attempt_ref_v2(
            self.persistence,
            user_email=_LEARNER_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
        )
        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.attempt_id, first.attempt_id)
        self.assertEqual(ref.status, "completed")


class TestTrustedIdentity(unittest.TestCase):
    def test_b_trusted_identity_derived_server_side(self):
        identity = build_trusted_identity_v2(user_email=_LEARNER_EMAIL, supabase_client="client")
        self.assertEqual(identity.user_email, _LEARNER_EMAIL)

    def test_c_browser_email_not_accepted_as_identity(self):
        from utils.scenario_streamlit_v2 import ScenarioStreamlitV2UnauthenticatedError

        with self.assertRaises(ScenarioStreamlitV2UnauthenticatedError):
            build_trusted_identity_v2(user_email="", supabase_client="client")


try:
    from tests.test_scenario_supabase_port_v2 import (
        TestSupabasePortDisposablePostgrestSmoke as _PortDisposableSmokeBase,
        _docker_available,
    )

    _DISPOSABLE_BASE_AVAILABLE = True
except Exception:  # pragma: no cover
    _DISPOSABLE_BASE_AVAILABLE = False


if _DISPOSABLE_BASE_AVAILABLE:

    @unittest.skipUnless(
        _docker_available(), "docker CLI not found or daemon not responding -- genuine environment gap"
    )
    class TestScenarioStreamlitV2DisposableIntegration(_PortDisposableSmokeBase):
        NETWORK = "certbound-v2-streamlit-smoke-net"
        PG_CONTAINER = "certbound-v2-streamlit-smoke-pg"
        POSTGREST_CONTAINER = "certbound-v2-streamlit-smoke-postgrest"
        PG_HOST_PORT = 55437
        POSTGREST_HOST_PORT = 33005

        def setUp(self) -> None:
            from postgrest import SyncPostgrestClient

            token = self._mint_service_role_jwt()
            self.client = SyncPostgrestClient(
                f"http://127.0.0.1:{self.POSTGREST_HOST_PORT}",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.identity = LearnerIdentityContextV2(
                user_email=f"streamlit-smoke-{uuid.uuid4().hex[:8]}@example.com",
                supabase_client=self.client,
            )
            self.session: Dict[str, Any] = {}
            self.content = load_cb_sc001_v2_content()

        def _decision_count(self, attempt_id: str) -> int:
            import psycopg2

            conn = psycopg2.connect(
                host="127.0.0.1",
                port=self.PG_HOST_PORT,
                user="postgres",
                dbname="postgres",
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM public.scenario_decisions WHERE attempt_id = %s",
                        (attempt_id,),
                    )
                    return int(cur.fetchone()[0])
            finally:
                conn.close()

        def test_real_streamlit_helper_full_terminal_flow(self):
            from utils.scenario_supabase_port_v2 import (
                ScenarioSupabasePortV2RpcError,
                SupabaseScenarioOrchestrationV2Port,
            )

            port = SupabaseScenarioOrchestrationV2Port(self.client)
            view = fetch_authoritative_cb_sc001_view(
                content=self.content,
                identity=self.identity,
                session_state=self.session,
                scenario_version_id=self.scenario_version_id,
                persistence=port,
            )
            self.assertFalse(view.serialized["isComplete"])
            attempt_id = self.session[SESSION_KEY_ATTEMPT_ID]
            scene1_blob = learner_safe_json_blob(view.serialized)
            self.assertNotIn(attempt_id, scene1_blob)
            del view

            recovered = fetch_authoritative_cb_sc001_view(
                content=self.content,
                identity=self.identity,
                session_state=self.session,
                scenario_version_id=self.scenario_version_id,
                persistence=port,
            )
            self.assertEqual(recovered.attempt_id, attempt_id)
            first_option = recovered.serialized["currentScene"]["options"][0]["id"]

            with patch(
                "utils.scenario_supabase_port_v2.SupabaseScenarioOrchestrationV2Port.call_submit_scenario_decision_v1",
                side_effect=ScenarioSupabasePortV2RpcError("connection reset"),
            ):
                uncertain = submit_cb_sc001_v2_choice(
                    content=self.content,
                    identity=self.identity,
                    session_state=self.session,
                    scenario_version_id=self.scenario_version_id,
                    selected_option_id=first_option,
                    persistence=port,
                )
            self.assertFalse(uncertain.conclusive)
            retry = submit_cb_sc001_v2_choice(
                content=self.content,
                identity=self.identity,
                session_state=self.session,
                scenario_version_id=self.scenario_version_id,
                retry_pending=True,
                persistence=port,
            )
            self.assertEqual(retry.idempotency_key, uncertain.idempotency_key)
            self.assertFalse(retry.serialized["isComplete"])

            current_option = retry.serialized["currentScene"]["options"][0]["id"]
            with patch(
                "utils.scenario_streamlit_v2.submit_learner_scenario_choice_v2",
                side_effect=ScenarioControllerV2StaleSessionError("stale"),
            ):
                stale_outcome = submit_cb_sc001_v2_choice(
                    content=self.content,
                    identity=self.identity,
                    session_state=self.session,
                    scenario_version_id=self.scenario_version_id,
                    selected_option_id=current_option,
                    persistence=port,
                )
            self.assertTrue(stale_outcome.stale_session)
            self.assertFalse(has_pending_submission(self.session))

            # Continue through remaining happy-path decisions to actual terminal.
            while True:
                current = fetch_authoritative_cb_sc001_view(
                    content=self.content,
                    identity=self.identity,
                    session_state=self.session,
                    scenario_version_id=self.scenario_version_id,
                    persistence=port,
                )
                if current.serialized.get("isComplete"):
                    break
                visible = {o["id"] for o in current.serialized["currentScene"]["options"]}
                seq = current.serialized.get("expectedSequenceNumber")
                preferred = None
                for expected_seq, _scene, option_id in HAPPY_PATH_DECISIONS:
                    if expected_seq == seq and option_id in visible:
                        preferred = option_id
                        break
                chosen = preferred or next(iter(visible))
                submit_cb_sc001_v2_choice(
                    content=self.content,
                    identity=self.identity,
                    session_state=self.session,
                    scenario_version_id=self.scenario_version_id,
                    selected_option_id=chosen,
                    persistence=port,
                )

            terminal = fetch_authoritative_cb_sc001_view(
                content=self.content,
                identity=self.identity,
                session_state=self.session,
                scenario_version_id=self.scenario_version_id,
                persistence=port,
            )
            self.assertTrue(terminal.serialized["isComplete"])
            self.assertIn("terminalResult", terminal.serialized)
            terminal_blob = learner_safe_json_blob(terminal.serialized)
            for sensitive in _SENSITIVE_SUBSTRINGS:
                self.assertNotIn(sensitive, terminal_blob)
            self.assertNotIn(attempt_id, terminal_blob)
            assert_option_b_session_state_compliant(self.session)

            retained = {SESSION_KEY_ATTEMPT_ID: attempt_id}
            del terminal
            resumed_terminal = fetch_authoritative_cb_sc001_view(
                content=self.content,
                identity=self.identity,
                session_state=retained,
                scenario_version_id=self.scenario_version_id,
                persistence=port,
            )
            self.assertTrue(resumed_terminal.serialized["isComplete"])
            self.assertEqual(
                resumed_terminal.serialized["terminalResult"].get("outcomeTitle"),
                json.loads(terminal_blob)["terminalResult"].get("outcomeTitle"),
            )

            self.session = retained
            submit_calls_before = 0
            with self.assertRaises(ScenarioControllerV2TerminalAttemptError):
                submit_cb_sc001_v2_choice(
                    content=self.content,
                    identity=self.identity,
                    session_state=self.session,
                    scenario_version_id=self.scenario_version_id,
                    selected_option_id="opt-sc001-c01-a",
                    persistence=port,
                )
            self.assertEqual(self._decision_count(attempt_id), len(HAPPY_PATH_DECISIONS))
            _ = submit_calls_before  # clarity: pre-persistence rejection

        def test_real_postgrest_start_submit_resume_idempotency_and_conflict(self):
            self.skipTest("covered by port smoke; streamlit integration uses dedicated full-terminal test")

        def test_real_postgrest_unknown_function_error_is_sanitized(self):
            self.skipTest("covered by port smoke; streamlit integration uses dedicated full-terminal test")

    del _PortDisposableSmokeBase


if __name__ == "__main__":
    unittest.main()
