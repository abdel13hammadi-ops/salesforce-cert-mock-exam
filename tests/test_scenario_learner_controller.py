"""Focused tests for the SIM-VSLICE-01 BA-201 learner start/resume controller.

Uses fakes only -- no live database, no Supabase network calls, no Streamlit
runtime. Loads the real, already-approved BA-201 catalog/content from disk
(via the same `utils.scenario_catalog` / `utils.scenario_schema` path the
controller itself uses) so these tests also prove integration with the
actual repository content, not a synthetic stand-in.
"""

from __future__ import annotations

import dataclasses
import os
import re
import sys
import json
import pickle
import unittest
import uuid
from types import MappingProxyType
from typing import Optional
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_catalog import resolve_default_scenario_version_path
from utils.scenario_engine import (
    ENGINE_VERSION,
    apply_decision,
    get_current_scene,
    serialize_run_snapshot,
    serialize_terminal_result,
    start_scenario_run,
)
from utils.scenario_learner_controller import (
    BA201_CERTIFICATION_EXAM_NAME,
    BA201_SIMULATION_ID,
    PreparedScenarioDecision,
    ScenarioAttemptView,
    ScenarioDecisionPersistenceOutcome,
    ScenarioLearnerAccessError,
    ScenarioLearnerAttemptNotActiveError,
    ScenarioLearnerAttemptNotFoundError,
    ScenarioLearnerBackendError,
    ScenarioLearnerConflictError,
    ScenarioLearnerContentError,
    ScenarioLearnerInvalidOptionError,
    ScenarioLearnerStateError,
    ScenarioLearnerVersionUnavailableError,
    ScenarioOptionView,
    ScenarioSceneView,
    prepare_ba201_decision,
    start_or_resume_ba201_attempt,
    submit_ba201_decision,
    submit_prepared_ba201_decision,
)
from utils.scenario_schema import load_scenario_content

_UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_CONTENT_PATH = resolve_default_scenario_version_path(
    certification_exam_name=BA201_CERTIFICATION_EXAM_NAME,
    simulation_id=BA201_SIMULATION_ID,
)
_CONTENT = load_scenario_content(_CONTENT_PATH)

_LEARNER_EMAIL_RAW = "  Learner@Example.COM  "
_LEARNER_EMAIL_NORMALIZED = "learner@example.com"


def _initial_serialized_state() -> dict:
    return serialize_run_snapshot(start_scenario_run(_CONTENT))


def _resumed_serialized_state() -> dict:
    """A validly-replayable, one-decision-advanced snapshot: s01_kickoff
    option "B" -> s02a_cio_response (a real transition declared in the
    BA-201 content, not invented)."""
    return {
        "simulationId": _CONTENT.simulation_id,
        "version": _CONTENT.version,
        "canonicalContentSha256": _CONTENT.canonical_content_sha256,
        "engineVersion": ENGINE_VERSION,
        "currentSceneId": "s02a_cio_response",
        "state": {"projectHealth": 102.0, "stakeholderTrust": 102.0, "scheduleRisk": 0.0},
        "flags": ["validated_scope_early"],
        "decisionHistory": [
            {"sequenceNumber": 1, "sceneId": "s01_kickoff", "optionId": "B"},
        ],
        "isComplete": False,
        "terminalResult": None,
    }


class _FakeRpcResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


class _FakeRpcBuilder:
    def __init__(self, data=None, error=None, exception=None):
        self._data = data
        self._error = error
        self._exception = exception

    def execute(self):
        if self._exception is not None:
            raise self._exception
        return _FakeRpcResult(self._data, self._error)


class _FakeException(Exception):
    """Simulates a postgrest-py APIError carrying a `.message` attribute."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class _FakeTableResult:
    def __init__(self, data):
        self.data = data


class _FakeTableQuery:
    def __init__(self, rows, *, raise_exc=None):
        self._rows = list(rows)
        self._raise_exc = raise_exc

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self._rows = [row for row in self._rows if row.get(key) == value]
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeTableResult(list(self._rows))


_DECISION_REQUEST_FIELD_KEYS = (
    "p_expected_sequence_number",
    "p_expected_scene_id",
    "p_selected_option_id",
    "p_state_before",
    "p_state_after",
    "p_resulting_scene_id",
    "p_is_terminal",
    "p_terminal_ending_id",
    "p_terminal_result_snapshot",
)


def _decision_request_fields(params: dict) -> dict:
    """The subset of `submit_scenario_decision_v1` RPC params that V68's own
    request fingerprint covers -- used by `_StatefulDecisionStore` below to
    decide whether a repeated `(attempt_id, idempotency_key)` call is a
    genuine safe retry (identical fields) or an `idempotency_key_conflict:`
    (same key, different request)."""
    return {key: params.get(key) for key in _DECISION_REQUEST_FIELD_KEYS}


class _StatefulDecisionStore:
    """SIM-VSLICE-02B: a fake `submit_scenario_decision_v1` that actually
    models an attempt's server-side state advancing (or completing) on a
    genuine first commit, and actually persists an idempotency record for
    `(attempt_id, idempotency_key)` -- unlike a static canned RPC response,
    this lets a test simulate "the database already committed this exact
    decision, but the first call's response was lost", and then prove a
    RETRY (using the identical prepared request) reaches a genuinely stable
    idempotent replay of that ALREADY-COMMITTED state, rather than merely
    receiving the same canned row regardless of whether a real commit ever
    happened.

    `raise_after_first_commit=True` makes the FIRST successful commit raise
    an exception to the caller (simulating a lost response) AFTER already
    updating internal state and recording the idempotency entry -- exactly
    modeling an uncertain-but-actually-committed write.
    """

    def __init__(self, *, attempt_id: str, initial_sequence_number: int, initial_scene_id: str):
        self.attempt_id = attempt_id
        self.sequence_number = initial_sequence_number
        self.scene_id: Optional[str] = initial_scene_id
        self.status = "in_progress"
        self.commit_count = 0
        self._records: dict[str, dict] = {}

    def submit(self, params: dict, *, raise_after_first_commit: bool = False) -> dict:
        idempotency_key = params["p_idempotency_key"]
        if idempotency_key in self._records:
            stored = self._records[idempotency_key]
            if stored["request"] != _decision_request_fields(params):
                raise _FakeException(
                    "idempotency_key_conflict: idempotency key already used for a different request"
                )
            row = dict(stored["response"])
            row["idempotent_replay"] = True
            return row

        if self.status != "in_progress":
            raise _FakeException(f"attempt_not_in_progress: scenario_attempts {self.attempt_id} has already ended")
        if params["p_expected_sequence_number"] != self.sequence_number:
            raise _FakeException(
                f"sequence_mismatch: expected sequence {params['p_expected_sequence_number']} but attempt "
                f"is at sequence {self.sequence_number}"
            )
        if params["p_expected_scene_id"] != self.scene_id:
            raise _FakeException("scene_mismatch: expected scene does not match the attempt's current scene")

        is_terminal = bool(params["p_is_terminal"])
        response = {
            "decision_id": str(uuid.uuid4()),
            "attempt_id": self.attempt_id,
            "sequence_number": self.sequence_number,
            "idempotent_replay": False,
            "attempt_status": "completed" if is_terminal else "in_progress",
            "current_scene_id": None if is_terminal else params["p_resulting_scene_id"],
            "next_sequence_number": self.sequence_number + 1,
            "serialized_engine_state": params["p_state_after"],
            "completed_at": "2026-07-20T00:00:00Z" if is_terminal else None,
            "terminal_ending_id": params.get("p_terminal_ending_id"),
            "terminal_result_snapshot": params.get("p_terminal_result_snapshot"),
        }

        # COMMIT first (advance/complete the modeled attempt, record the
        # idempotency entry) -- exactly matching "the database already
        # committed" even though this call may then still raise below.
        self._records[idempotency_key] = {"request": _decision_request_fields(params), "response": dict(response)}
        self.sequence_number = response["next_sequence_number"]
        self.scene_id = response["current_scene_id"]
        self.status = response["attempt_status"]
        self.commit_count += 1

        if raise_after_first_commit and self.commit_count == 1:
            raise _FakeException("upstream_timeout: response lost after commit")

        return response


class FakeSupabase:
    """Supports both `.table(...)` (V66/V67 version-id resolution) and
    `.rpc(...)` (V68 attempt persistence) -- the two boundaries this
    controller crosses."""

    def __init__(self):
        self.rpc_calls: list[tuple[str, dict]] = []
        self.table_calls: list[str] = []
        self._rpc_responses: dict[str, object] = {}
        self._rpc_raise: dict[str, Exception] = {}
        self._tables: dict[str, list[dict]] = {}
        self._table_raise: dict[str, Exception] = {}
        self._decision_store: Optional[_StatefulDecisionStore] = None
        self._decision_store_raise_after_first_commit = False

    def set_rpc_response(self, name: str, data) -> None:
        self._rpc_responses[name] = data

    def set_rpc_raise(self, name: str, message: str) -> None:
        self._rpc_raise[name] = _FakeException(message)

    def set_table_rows(self, table_name: str, rows: list[dict]) -> None:
        self._tables[table_name] = rows

    def set_table_raise(self, table_name: str, exc: Exception) -> None:
        self._table_raise[table_name] = exc

    def install_stateful_decision_store(
        self, store: _StatefulDecisionStore, *, raise_after_first_commit: bool = False
    ) -> None:
        """Route `submit_scenario_decision_v1` calls through `store`
        instead of the static `set_rpc_response`/`set_rpc_raise`
        mechanism -- see `_StatefulDecisionStore`'s own docstring."""
        self._decision_store = store
        self._decision_store_raise_after_first_commit = raise_after_first_commit

    def table(self, name: str):
        self.table_calls.append(name)
        return _FakeTableQuery(self._tables.get(name, []), raise_exc=self._table_raise.get(name))

    def rpc(self, name: str, params: dict):
        self.rpc_calls.append((name, dict(params)))
        if name == "submit_scenario_decision_v1" and self._decision_store is not None:
            try:
                row = self._decision_store.submit(
                    params, raise_after_first_commit=self._decision_store_raise_after_first_commit
                )
            except Exception as exc:  # noqa: BLE001 - simulating a raw backend/RPC exception
                return _FakeRpcBuilder(exception=exc)
            return _FakeRpcBuilder(data=[row])
        if name in self._rpc_raise:
            return _FakeRpcBuilder(exception=self._rpc_raise[name])
        return _FakeRpcBuilder(data=self._rpc_responses.get(name, []))


def _make_scenario_row(
    *,
    scenario_id: str,
    is_active: bool = True,
    current_published_version_id=None,
) -> dict:
    return {
        "id": scenario_id,
        "simulation_id": _CONTENT.simulation_id,
        "is_active": is_active,
        "current_published_version_id": current_published_version_id,
    }


def _make_client_with_published_version(version_id: str) -> FakeSupabase:
    """An active scenario whose `current_published_version_id` points at
    `version_id`, and exactly one matching `scenario_versions` row."""
    client = FakeSupabase()
    scenario_id = str(uuid.uuid4())
    client.set_table_rows(
        "scenarios",
        [_make_scenario_row(scenario_id=scenario_id, current_published_version_id=version_id)],
    )
    client.set_table_rows(
        "scenario_versions",
        [{"id": version_id, "scenario_id": scenario_id, "version": _CONTENT.version}],
    )
    return client


def _advance_to_scene(target_scene_id: str):
    """Deterministically drive the REAL BA-201 engine/content from the start
    scene to `target_scene_id`, always taking the scene's first declared
    option. Used so SIM-VSLICE-02 fixtures needing a persisted attempt
    several decisions in (including the terminal test, which needs to reach
    `s24_golive_readiness`) are built from the actual scenario graph rather
    than a hand-authored decision history that could silently drift from it."""
    run = start_scenario_run(_CONTENT)
    while run.current_scene_id != target_scene_id:
        scene = get_current_scene(run)
        run = apply_decision(run, scene.decision.options[0].id)
    return run


def _attempt_row(*, attempt_id: str, version_id: str, run, status: str = "in_progress", **overrides) -> dict:
    row = {
        "attempt_id": attempt_id,
        "scenario_id": str(uuid.uuid4()),
        "scenario_version_id": version_id,
        "status": status,
        "current_scene_id": run.current_scene_id,
        "next_sequence_number": len(run.decisions) + 1,
        "serialized_engine_state": serialize_run_snapshot(run),
        "engine_version": ENGINE_VERSION,
        "scenario_content_sha256": _CONTENT.canonical_content_sha256,
        "started_at": "2026-07-19T13:00:00Z",
        "updated_at": "2026-07-19T13:00:00Z",
        "completed_at": None,
        "abandoned_at": None,
        "terminal_ending_id": None,
        "terminal_result_snapshot": None,
        "decisions": [],
    }
    row.update(overrides)
    return row


def _submit_row(
    *,
    attempt_id: str,
    run_after,
    sequence_number: int,
    idempotent_replay: bool = False,
    **overrides,
) -> dict:
    is_complete = run_after.is_complete
    row = {
        "decision_id": str(uuid.uuid4()),
        "attempt_id": attempt_id,
        "sequence_number": sequence_number,
        "idempotent_replay": idempotent_replay,
        "attempt_status": "completed" if is_complete else "in_progress",
        "current_scene_id": run_after.current_scene_id,
        "next_sequence_number": sequence_number + 1,
        "serialized_engine_state": serialize_run_snapshot(run_after),
        "completed_at": "2026-07-19T14:00:00Z" if is_complete else None,
        "terminal_ending_id": run_after.terminal_result.ending_id if is_complete else None,
        "terminal_result_snapshot": (
            serialize_terminal_result(run_after.terminal_result) if is_complete else None
        ),
    }
    row.update(overrides)
    return row


def _start_row(*, version_id: str, **overrides) -> dict:
    row = {
        "attempt_id": str(uuid.uuid4()),
        "created": True,
        "scenario_id": str(uuid.uuid4()),
        "scenario_version_id": version_id,
        "status": "in_progress",
        "current_scene_id": _CONTENT.start_scene,
        "next_sequence_number": 1,
        "serialized_engine_state": _initial_serialized_state(),
        "engine_version": ENGINE_VERSION,
        "scenario_content_sha256": _CONTENT.canonical_content_sha256,
        "started_at": "2026-07-19T13:00:00Z",
        "completed_at": None,
        "abandoned_at": None,
        "terminal_ending_id": None,
        "terminal_result_snapshot": None,
    }
    row.update(overrides)
    return row


class StartOrResumeControllerTests(unittest.TestCase):
    def test_verified_email_is_forwarded_to_start_or_resume(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_response("start_or_resume_scenario_attempt_v1", [_start_row(version_id=version_id)])

        start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        rpc_calls = [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"]
        self.assertEqual(len(rpc_calls), 1)
        self.assertEqual(rpc_calls[0][1]["p_user_email"], _LEARNER_EMAIL_NORMALIZED)

    def test_missing_email_rejected_before_persistence_access(self):
        client = FakeSupabase()
        with self.assertRaises(ScenarioLearnerAccessError):
            start_or_resume_ba201_attempt(None, client=client)
        self.assertEqual(client.rpc_calls, [])
        self.assertEqual(client.table_calls, [])

    def test_empty_unauthenticated_email_rejected_before_persistence_access(self):
        client = FakeSupabase()
        with self.assertRaises(ScenarioLearnerAccessError):
            start_or_resume_ba201_attempt("   ", client=client)
        self.assertEqual(client.rpc_calls, [])
        self.assertEqual(client.table_calls, [])

    def test_catalog_scenario_loaded_through_catalog_path(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_response("start_or_resume_scenario_attempt_v1", [_start_row(version_id=version_id)])

        view = start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertEqual(view.scenario_title, _CONTENT.title)
        self.assertEqual(view.certification_exam_name, _CONTENT.certification_exam_name)
        self.assertEqual(view.certification_exam_name, BA201_CERTIFICATION_EXAM_NAME)

    def test_initial_state_constructed_but_ignored_once_attempt_is_resumed(self):
        """SIM-VSLICE-01 requirement #4/#5: even though a fresh initial
        snapshot is always (cheaply) built before calling start/resume, the
        VIEW returned for a RESUMED attempt must reflect the persisted
        resumed scene, never the freshly-built start scene."""
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_response(
            "start_or_resume_scenario_attempt_v1",
            [
                _start_row(
                    version_id=version_id,
                    created=False,
                    current_scene_id="s02a_cio_response",
                    next_sequence_number=2,
                    serialized_engine_state=_resumed_serialized_state(),
                )
            ],
        )

        view = start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertFalse(view.is_new_attempt)
        self.assertIsNotNone(view.current_scene)
        self.assertNotEqual(view.current_scene.narrative, "")
        self.assertEqual(view.progress_label, "Decision 2")
        # The resumed scene's own domain label, not the start scene's.
        self.assertIn(view.current_scene.domain_label, {domain.label for domain in _CONTENT.domains})

    def test_persisted_state_restored_on_resume(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_response(
            "start_or_resume_scenario_attempt_v1",
            [
                _start_row(
                    version_id=version_id,
                    created=False,
                    current_scene_id="s02a_cio_response",
                    next_sequence_number=2,
                    serialized_engine_state=_resumed_serialized_state(),
                )
            ],
        )

        view = start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertFalse(view.is_complete)
        self.assertIsInstance(view.current_scene, ScenarioSceneView)
        option_ids = {option.option_id for option in view.current_scene.options}
        self.assertTrue(option_ids)

    def test_repeated_start_or_resume_returns_same_attempt_model(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        fixed_row = _start_row(version_id=version_id)
        client.set_rpc_response("start_or_resume_scenario_attempt_v1", [fixed_row])

        first = start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)
        second = start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertEqual(first.attempt_id, second.attempt_id)
        self.assertEqual(first.scenario_title, second.scenario_title)
        rpc_calls = [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"]
        self.assertEqual(len(rpc_calls), 2)

    def test_malformed_persisted_state_rejected_safely(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        bad_state = _resumed_serialized_state()
        bad_state["simulationId"] = "not-the-real-simulation-id"
        client.set_rpc_response(
            "start_or_resume_scenario_attempt_v1",
            [_start_row(version_id=version_id, created=False, serialized_engine_state=bad_state)],
        )

        with self.assertRaises(ScenarioLearnerStateError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

    def test_backend_exception_converted_to_application_safe_error(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_raise("start_or_resume_scenario_attempt_v1", "some_unmapped_backend_failure: boom")

        with self.assertRaises(ScenarioLearnerBackendError) as ctx:
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)
        # The safe application-level message must not leak the raw backend text.
        self.assertNotIn("boom", str(ctx.exception))

    def test_unpublished_or_unavailable_scenario_version_is_rejected_before_rpc(self):
        client = FakeSupabase()  # no "scenarios"/"scenario_versions" rows configured
        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)
        self.assertEqual(
            [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"],
            [],
        )

    def test_scenario_version_not_published_reported_by_rpc_is_rejected(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_raise(
            "start_or_resume_scenario_attempt_v1",
            f"scenario_version_not_published: scenario_versions {version_id} is not published (status=draft)",
        )
        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

    def test_unknown_scenario_raises_content_error(self):
        client = FakeSupabase()
        with self.assertRaises(ScenarioLearnerContentError):
            start_or_resume_ba201_attempt(
                _LEARNER_EMAIL_RAW,
                client=client,
                certification_exam_name=BA201_CERTIFICATION_EXAM_NAME,
                simulation_id="not-a-real-simulation-id",
            )
        self.assertEqual(client.table_calls, [])
        self.assertEqual(client.rpc_calls, [])

    def test_no_decision_submission_rpc_is_ever_called(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_response("start_or_resume_scenario_attempt_v1", [_start_row(version_id=version_id)])

        start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        called_rpc_names = {name for name, _params in client.rpc_calls}
        self.assertEqual(called_rpc_names, {"start_or_resume_scenario_attempt_v1"})
        self.assertNotIn("submit_scenario_decision_v1", called_rpc_names)
        self.assertNotIn("abandon_scenario_attempt_v1", called_rpc_names)
        self.assertNotIn("get_scenario_attempt_v1", called_rpc_names)

    def test_presentation_model_does_not_expose_backend_identifiers_unnecessarily(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        row = _start_row(version_id=version_id)
        client.set_rpc_response("start_or_resume_scenario_attempt_v1", [row])

        view = start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertIsInstance(view, ScenarioAttemptView)
        self.assertIsNotNone(view.current_scene)
        # None of the learner-rendered text fields may leak a raw UUID.
        rendered_text_fields = [
            view.scenario_title,
            view.certification_exam_name,
            view.progress_label,
            view.current_scene.domain_label,
            view.current_scene.narrative,
            view.current_scene.decision_prompt,
        ] + [option.label for option in view.current_scene.options]
        for field_value in rendered_text_fields:
            self.assertNotRegex(field_value, _UUID_PATTERN)
        # ScenarioOptionView only carries a short content-defined option id
        # (e.g. "A"/"B"/"C"), never a backend UUID.
        for option in view.current_scene.options:
            self.assertIsInstance(option, ScenarioOptionView)
            self.assertNotRegex(option.option_id, _UUID_PATTERN)


class DefaultClientInitializationErrorTests(unittest.TestCase):
    """SIM-VSLICE-02C: `_default_client()` (used by all three public entry
    points whenever no `client=` is explicitly supplied) must never let a
    Supabase admin-client construction/configuration failure escape as a
    raw exception -- it must always become `ScenarioLearnerBackendError`.
    These tests deliberately never pass `client=...` so `_default_client()`
    is actually exercised, and patch
    `utils.access_control.get_supabase_admin_client` (the exact function
    `_default_client()` imports and calls) to simulate that failure.
    """

    def test_start_or_resume_client_init_failure_maps_to_backend_error(self):
        with patch(
            "utils.access_control.get_supabase_admin_client",
            side_effect=RuntimeError("missing service-role credentials"),
        ):
            with self.assertRaises(ScenarioLearnerBackendError) as ctx:
                start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW)
        self.assertNotIn("missing service-role credentials", str(ctx.exception))

    def test_prepare_client_init_failure_maps_to_backend_error_before_get_attempt(self):
        with patch(
            "utils.access_control.get_supabase_admin_client",
            side_effect=RuntimeError("missing service-role credentials"),
        ), patch("utils.scenario_learner_controller.get_attempt") as get_attempt_spy:
            with self.assertRaises(ScenarioLearnerBackendError):
                prepare_ba201_decision(
                    _LEARNER_EMAIL_RAW,
                    attempt_id=str(uuid.uuid4()),
                    selected_option_id="A",
                    idempotency_key=_IDEMPOTENCY_KEY,
                )
        get_attempt_spy.assert_not_called()

    def test_submit_prepared_client_init_failure_maps_to_backend_error(self):
        run = start_scenario_run(_CONTENT)
        version_id = str(uuid.uuid4())
        setup_client = _make_client_with_published_version(version_id)
        attempt_id = str(uuid.uuid4())
        setup_client.set_rpc_response(
            "get_scenario_attempt_v1",
            [_attempt_row(attempt_id=attempt_id, version_id=version_id, run=run)],
        )
        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=setup_client,
        )

        with patch(
            "utils.access_control.get_supabase_admin_client",
            side_effect=RuntimeError("missing service-role credentials"),
        ), patch("utils.scenario_learner_controller.submit_decision") as submit_decision_spy:
            with self.assertRaises(ScenarioLearnerBackendError) as ctx:
                submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared)
        submit_decision_spy.assert_not_called()
        self.assertNotIn("missing service-role credentials", str(ctx.exception))


class CurrentScenarioVersionResolutionTests(unittest.TestCase):
    """SIM-VSLICE-01D: `scenarios.current_published_version_id` -- and
    nothing else -- must determine which `scenario_versions` row this
    controller offers to a new learner."""

    def test_active_scenario_with_current_pointer_sends_exact_pointer_uuid(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_response("start_or_resume_scenario_attempt_v1", [_start_row(version_id=version_id)])

        start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        rpc_calls = [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"]
        self.assertEqual(len(rpc_calls), 1)
        self.assertEqual(rpc_calls[0][1]["p_scenario_version_id"], version_id)

    def test_nonexistent_scenario_fails_before_rpc(self):
        client = FakeSupabase()  # no "scenarios" row at all
        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)
        self.assertEqual(
            [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"],
            [],
        )

    def test_inactive_scenario_fails_before_scenario_versions_lookup_or_rpc(self):
        client = FakeSupabase()
        scenario_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        client.set_table_rows(
            "scenarios",
            [_make_scenario_row(scenario_id=scenario_id, is_active=False, current_published_version_id=version_id)],
        )
        # A perfectly valid pointer target exists, but must never be reached.
        client.set_table_rows(
            "scenario_versions",
            [{"id": version_id, "scenario_id": scenario_id, "version": _CONTENT.version}],
        )

        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertNotIn("scenario_versions", client.table_calls)
        self.assertEqual(
            [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"],
            [],
        )

    def test_null_current_published_version_id_fails_before_scenario_versions_lookup_or_rpc(self):
        client = FakeSupabase()
        scenario_id = str(uuid.uuid4())
        client.set_table_rows(
            "scenarios",
            [_make_scenario_row(scenario_id=scenario_id, current_published_version_id=None)],
        )

        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertNotIn("scenario_versions", client.table_calls)
        self.assertEqual(
            [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"],
            [],
        )

    def test_pointer_with_no_matching_scenario_versions_row_fails_before_rpc(self):
        client = FakeSupabase()
        scenario_id = str(uuid.uuid4())
        pointer_id = str(uuid.uuid4())
        client.set_table_rows(
            "scenarios",
            [_make_scenario_row(scenario_id=scenario_id, current_published_version_id=pointer_id)],
        )
        client.set_table_rows("scenario_versions", [])  # pointer resolves to nothing

        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertEqual(
            [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"],
            [],
        )

    def test_pointer_row_belonging_to_different_scenario_fails_before_rpc(self):
        client = FakeSupabase()
        scenario_id = str(uuid.uuid4())
        other_scenario_id = str(uuid.uuid4())
        pointer_id = str(uuid.uuid4())
        client.set_table_rows(
            "scenarios",
            [_make_scenario_row(scenario_id=scenario_id, current_published_version_id=pointer_id)],
        )
        # The pointer id exists in scenario_versions, but it belongs to a
        # DIFFERENT scenario_id -- the (id, scenario_id) filter must reject it.
        client.set_table_rows(
            "scenario_versions",
            [{"id": pointer_id, "scenario_id": other_scenario_id, "version": _CONTENT.version}],
        )

        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertEqual(
            [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"],
            [],
        )

    def test_pointer_version_mismatch_with_repository_content_fails_before_rpc(self):
        client = FakeSupabase()
        scenario_id = str(uuid.uuid4())
        pointer_id = str(uuid.uuid4())
        client.set_table_rows(
            "scenarios",
            [_make_scenario_row(scenario_id=scenario_id, current_published_version_id=pointer_id)],
        )
        client.set_table_rows(
            "scenario_versions",
            [{"id": pointer_id, "scenario_id": scenario_id, "version": "999.0.0-not-the-repo-version"}],
        )

        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        self.assertEqual(
            [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"],
            [],
        )

    def test_older_version_matching_row_is_never_selected_over_a_mismatched_current_pointer(self):
        """An older row's `version` string equals the local repository
        content's version -- but it is NOT the current pointer. The
        resolver must never fall back to a `(scenario_id, version)` string
        match; it must fail exactly like any other pointer mismatch, and
        the older row's id must never reach the RPC."""
        client = FakeSupabase()
        scenario_id = str(uuid.uuid4())
        older_version_id = str(uuid.uuid4())
        current_pointer_id = str(uuid.uuid4())
        client.set_table_rows(
            "scenarios",
            [_make_scenario_row(scenario_id=scenario_id, current_published_version_id=current_pointer_id)],
        )
        client.set_table_rows(
            "scenario_versions",
            [
                # Older, still-published row: version string matches the
                # local repository content, but it is not the current
                # pointer target.
                {"id": older_version_id, "scenario_id": scenario_id, "version": _CONTENT.version},
                # The actual current pointer target: exists, belongs to the
                # right scenario, but its own version does not match.
                {"id": current_pointer_id, "scenario_id": scenario_id, "version": "2.0.0-newer-mismatched"},
            ],
        )

        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)

        rpc_calls = [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"]
        self.assertEqual(rpc_calls, [])
        for _name, params in client.rpc_calls:
            self.assertNotEqual(params.get("p_scenario_version_id"), older_version_id)

    def test_backend_failure_resolving_scenario_row_maps_to_backend_error(self):
        client = FakeSupabase()
        client.set_table_raise("scenarios", RuntimeError("connection reset"))

        with self.assertRaises(ScenarioLearnerBackendError) as ctx:
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)
        self.assertNotIn("connection reset", str(ctx.exception))
        self.assertEqual(
            [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"],
            [],
        )

    def test_backend_failure_resolving_pointer_row_maps_to_backend_error(self):
        client = FakeSupabase()
        scenario_id = str(uuid.uuid4())
        pointer_id = str(uuid.uuid4())
        client.set_table_rows(
            "scenarios",
            [_make_scenario_row(scenario_id=scenario_id, current_published_version_id=pointer_id)],
        )
        client.set_table_raise("scenario_versions", RuntimeError("connection reset"))

        with self.assertRaises(ScenarioLearnerBackendError) as ctx:
            start_or_resume_ba201_attempt(_LEARNER_EMAIL_RAW, client=client)
        self.assertNotIn("connection reset", str(ctx.exception))
        self.assertEqual(
            [call for call in client.rpc_calls if call[0] == "start_or_resume_scenario_attempt_v1"],
            [],
        )


_IDEMPOTENCY_KEY = "22222222-2222-4222-8222-222222222222"


class SubmitDecisionControllerTests(unittest.TestCase):
    """SIM-VSLICE-02: focused tests for `submit_ba201_decision(...)`.

    Every fixture uses the REAL BA-201 engine (`start_scenario_run` /
    `apply_decision` / `_advance_to_scene`) against the REAL loaded content,
    never a hand-authored decision history, so these tests also prove the
    controller stays consistent with the actual scenario graph.
    """

    def _client_with_attempt(self, *, run, attempt_id=None, status: str = "in_progress", version_id=None):
        version_id = version_id or str(uuid.uuid4())
        attempt_id = attempt_id or str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_response(
            "get_scenario_attempt_v1",
            [_attempt_row(attempt_id=attempt_id, version_id=version_id, run=run, status=status)],
        )
        return client, version_id, attempt_id

    def _submit_calls(self, client):
        return [call for call in client.rpc_calls if call[0] == "submit_scenario_decision_v1"]

    # -- 1/2/3: engine application + exact request construction ----------

    def test_valid_decision_applies_through_engine_and_advances_scene(self):
        run = start_scenario_run(_CONTENT)  # s01_kickoff
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        view = submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        self.assertIsInstance(view, ScenarioAttemptView)
        self.assertFalse(view.is_complete)
        self.assertIsNotNone(view.current_scene)

    def test_exact_selected_option_is_forwarded_to_submit_decision(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        submit_calls = self._submit_calls(client)
        self.assertEqual(len(submit_calls), 1)
        self.assertEqual(submit_calls[0][1]["p_selected_option_id"], "B")

    def test_resulting_serialized_state_is_passed_to_submit_decision(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        submit_calls = self._submit_calls(client)
        self.assertEqual(submit_calls[0][1]["p_state_before"], serialize_run_snapshot(run))
        self.assertEqual(submit_calls[0][1]["p_state_after"], serialize_run_snapshot(run_after))
        self.assertEqual(submit_calls[0][1]["p_resulting_scene_id"], run_after.current_scene_id)
        self.assertFalse(submit_calls[0][1]["p_is_terminal"])
        self.assertEqual(submit_calls[0][1]["p_idempotency_key"], _IDEMPOTENCY_KEY)

    # -- 4: expected sequence/scene come from the persisted attempt --------

    def test_expected_sequence_and_scene_come_from_persisted_attempt(self):
        run = _advance_to_scene("s03_data_landscape")  # 2 decisions already recorded
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "A")
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=len(run.decisions) + 1)],
        )

        submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        submit_calls = self._submit_calls(client)
        self.assertEqual(submit_calls[0][1]["p_expected_sequence_number"], len(run.decisions) + 1)
        self.assertEqual(submit_calls[0][1]["p_expected_scene_id"], "s03_data_landscape")

    # -- 5/11: rebuilt from the CONFIRMED persisted outcome ------------------
    # SIM-VSLICE-02B: `submit_prepared_ba201_decision(...)` now validates the
    # persisted response against the prepared request's own fields, so a
    # response that diverges from what was requested is no longer silently
    # accepted -- it is an UNCERTAIN integrity outcome (see
    # `PreparedDecisionControllerTests.test_response_diverging_from_prepared_state_after_is_uncertain`).
    # This test instead proves the wrapper's rebuilt view reflects the
    # confirmed (matching) persisted state.

    def test_wrapper_view_reflects_confirmed_persisted_state(self):
        run = start_scenario_run(_CONTENT)  # s01_kickoff
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")  # -> s02a_cio_response
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        view = submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        expected_scene = get_current_scene(run_after)
        self.assertEqual(view.current_scene.decision_prompt, expected_scene.decision.prompt)
        self.assertEqual(view.current_scene.narrative, expected_scene.narrative)

    def test_response_diverging_from_prepared_state_after_is_uncertain_via_wrapper(self):
        run = start_scenario_run(_CONTENT)  # s01_kickoff
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        # This call's own locally-computed transition ("B" -> s02a_cio_response)
        # is deliberately NOT what the fake RPC responds with below -- the
        # response no longer matches what was prepared, so the wrapper must
        # raise an uncertain backend error rather than silently render it.
        divergent_run_after = apply_decision(run, "A")  # -> s02b_pushback
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=divergent_run_after, sequence_number=1)],
        )

        with self.assertRaises(ScenarioLearnerBackendError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )

    # -- 6: terminal result ------------------------------------------------

    def test_terminal_persistence_result_produces_completed_view_with_no_current_scene(self):
        run = _advance_to_scene("s24_golive_readiness")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "A")
        self.assertTrue(run_after.is_complete)
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=len(run.decisions) + 1)],
        )

        view = submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        self.assertTrue(view.is_complete)
        self.assertIsNone(view.current_scene)
        self.assertEqual(view.progress_label, "Scenario complete")

    # -- 7/18: invalid/unavailable option ----------------------------------

    def test_invalid_option_fails_before_persistence_submission(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        with self.assertRaises(ScenarioLearnerInvalidOptionError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="not-a-real-option",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )
        self.assertEqual(self._submit_calls(client), [])

    def test_option_valid_elsewhere_but_not_on_current_scene_is_rejected(self):
        """`s01_kickoff` has an option "C"; `s03_data_landscape` does not --
        proves an option id that IS valid somewhere in the graph is still
        rejected when it does not belong to the attempt's actual persisted
        current scene."""
        run = _advance_to_scene("s03_data_landscape")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        with self.assertRaises(ScenarioLearnerInvalidOptionError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="C",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )
        self.assertEqual(self._submit_calls(client), [])

    # -- 8: stale sequence / scene / state conflicts -----------------------

    def test_stale_sequence_conflict_becomes_safe_controller_error(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        client.set_rpc_raise(
            "submit_scenario_decision_v1",
            f"sequence_mismatch: expected sequence 1 but attempt {attempt_id} is at sequence 2",
        )

        with self.assertRaises(ScenarioLearnerConflictError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )

    # -- 9: completed/abandoned attempts ------------------------------------

    def test_completed_attempt_cannot_accept_another_decision(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run, status="completed")

        with self.assertRaises(ScenarioLearnerAttemptNotActiveError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )
        self.assertEqual(self._submit_calls(client), [])

    def test_abandoned_attempt_cannot_accept_another_decision(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run, status="abandoned")

        with self.assertRaises(ScenarioLearnerAttemptNotActiveError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )
        self.assertEqual(self._submit_calls(client), [])

    # -- 10/11: idempotency-key replay/conflict -----------------------------

    def test_same_idempotency_key_and_same_request_replay_safely(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1, idempotent_replay=True)],
        )

        view = submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        self.assertFalse(view.is_complete)
        submit_calls = self._submit_calls(client)
        self.assertEqual(submit_calls[0][1]["p_idempotency_key"], _IDEMPOTENCY_KEY)

    def test_idempotency_key_reuse_with_different_inputs_is_rejected(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        client.set_rpc_raise(
            "submit_scenario_decision_v1",
            "idempotency_key_conflict: idempotency key already used for a different request",
        )

        with self.assertRaises(ScenarioLearnerConflictError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )

    # -- Access / lookup / state-integrity error mapping --------------------

    def test_missing_email_rejected_before_attempt_lookup(self):
        client = FakeSupabase()
        with self.assertRaises(ScenarioLearnerAccessError):
            submit_ba201_decision(
                None,
                attempt_id=str(uuid.uuid4()),
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )
        self.assertEqual(client.rpc_calls, [])
        self.assertEqual(client.table_calls, [])

    def test_unknown_or_unowned_attempt_id_raises_attempt_not_found(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        attempt_id = str(uuid.uuid4())
        client.set_rpc_raise(
            "get_scenario_attempt_v1",
            f"attempt_not_found: scenario_attempts {attempt_id} not found or not owned",
        )

        with self.assertRaises(ScenarioLearnerAttemptNotFoundError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )
        self.assertEqual(self._submit_calls(client), [])

    def test_backend_failure_during_attempt_lookup_maps_to_backend_error(self):
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_raise("get_scenario_attempt_v1", "some_unmapped_failure: boom")

        with self.assertRaises(ScenarioLearnerBackendError) as ctx:
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=str(uuid.uuid4()),
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )
        self.assertNotIn("boom", str(ctx.exception))

    def test_malformed_persisted_state_rejected_safely_before_decision(self):
        run = start_scenario_run(_CONTENT)
        version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        attempt_id = str(uuid.uuid4())
        bad_row = _attempt_row(attempt_id=attempt_id, version_id=version_id, run=run)
        bad_row["serialized_engine_state"]["simulationId"] = "not-the-real-simulation-id"
        client.set_rpc_response("get_scenario_attempt_v1", [bad_row])

        with self.assertRaises(ScenarioLearnerStateError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )
        self.assertEqual(self._submit_calls(client), [])

    # -- 19: current published-version selection remains enforced ----------

    def test_attempt_pinned_to_non_current_version_is_rejected(self):
        current_version_id = str(uuid.uuid4())
        other_version_id = str(uuid.uuid4())
        client = _make_client_with_published_version(current_version_id)
        run = start_scenario_run(_CONTENT)
        attempt_id = str(uuid.uuid4())
        client.set_rpc_response(
            "get_scenario_attempt_v1",
            [_attempt_row(attempt_id=attempt_id, version_id=other_version_id, run=run)],
        )

        with self.assertRaises(ScenarioLearnerVersionUnavailableError):
            submit_ba201_decision(
                _LEARNER_EMAIL_RAW,
                attempt_id=attempt_id,
                selected_option_id="B",
                idempotency_key=_IDEMPOTENCY_KEY,
                client=client,
            )
        self.assertEqual(self._submit_calls(client), [])

    # -- 17: no direct/unexpected RPC calls ---------------------------------

    def test_only_expected_rpcs_are_called(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        called_rpc_names = {name for name, _params in client.rpc_calls}
        self.assertEqual(called_rpc_names, {"get_scenario_attempt_v1", "submit_scenario_decision_v1"})
        self.assertNotIn("start_or_resume_scenario_attempt_v1", called_rpc_names)
        self.assertNotIn("abandon_scenario_attempt_v1", called_rpc_names)

    # -- presentation safety -------------------------------------------------

    def test_presentation_model_does_not_expose_backend_identifiers(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        view = submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        rendered_text_fields = [
            view.scenario_title,
            view.certification_exam_name,
            view.progress_label,
            view.current_scene.domain_label,
            view.current_scene.narrative,
            view.current_scene.decision_prompt,
        ] + [option.label for option in view.current_scene.options]
        for field_value in rendered_text_fields:
            self.assertNotRegex(field_value, _UUID_PATTERN)


class PreparedDecisionControllerTests(unittest.TestCase):
    """SIM-VSLICE-02A/02B: focused tests for the two-stage
    `prepare_ba201_decision(...)` / `submit_prepared_ba201_decision(...)`
    API -- specifically that:

    - a retry reproduces the EXACT original V68 request and never
      re-derives it from a freshly-fetched attempt;
    - `PreparedScenarioDecision` is deeply immutable (every JSON payload is
      an already-canonicalized string, never a mutable dict/list) and
      `pickle`-serializable;
    - `submit_prepared_ba201_decision(...)` never loads scenario content,
      calls `get_attempt`, re-resolves the current-version pointer, or
      re-applies the engine, and validates the persisted response against
      the prepared request itself.
    """

    def _client_with_attempt(self, *, run, attempt_id=None, status: str = "in_progress", version_id=None):
        version_id = version_id or str(uuid.uuid4())
        attempt_id = attempt_id or str(uuid.uuid4())
        client = _make_client_with_published_version(version_id)
        client.set_rpc_response(
            "get_scenario_attempt_v1",
            [_attempt_row(attempt_id=attempt_id, version_id=version_id, run=run, status=status)],
        )
        return client, version_id, attempt_id

    def _submit_calls(self, client):
        return [call for call in client.rpc_calls if call[0] == "submit_scenario_decision_v1"]

    def _get_attempt_calls(self, client):
        return [call for call in client.rpc_calls if call[0] == "get_scenario_attempt_v1"]

    # -- 1: prepared request contains all V68 fingerprint/request inputs ---

    def test_prepared_request_contains_all_v68_fields(self):
        run = start_scenario_run(_CONTENT)
        client, version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        run_after = apply_decision(run, "B")
        self.assertIsInstance(prepared, PreparedScenarioDecision)
        self.assertEqual(prepared.normalized_email, _LEARNER_EMAIL_NORMALIZED)
        self.assertEqual(prepared.certification_exam_name, BA201_CERTIFICATION_EXAM_NAME)
        self.assertEqual(prepared.simulation_id, BA201_SIMULATION_ID)
        self.assertEqual(prepared.attempt_id, attempt_id)
        self.assertEqual(prepared.scenario_version_id, version_id)
        self.assertEqual(prepared.scenario_version, _CONTENT.version)
        self.assertEqual(prepared.canonical_content_sha256, _CONTENT.canonical_content_sha256)
        self.assertEqual(prepared.engine_version, ENGINE_VERSION)
        self.assertEqual(prepared.selected_option_id, "B")
        self.assertEqual(prepared.idempotency_key, _IDEMPOTENCY_KEY)
        self.assertEqual(prepared.expected_sequence_number, 1)
        self.assertEqual(prepared.expected_scene_id, run.current_scene_id)
        self.assertEqual(prepared.reconstruct_state_before(), serialize_run_snapshot(run))
        self.assertEqual(prepared.reconstruct_state_after(), serialize_run_snapshot(run_after))
        self.assertEqual(prepared.resulting_scene_id, run_after.current_scene_id)
        self.assertFalse(prepared.is_terminal)
        self.assertIsNone(prepared.terminal_ending_id)
        self.assertIsNone(prepared.terminal_result_snapshot_json)
        self.assertIsNone(prepared.reconstruct_terminal_result_snapshot())

    # -- 2/SIM-VSLICE-02B: prepared request is DEEPLY immutable --------------

    def test_prepared_request_is_immutable(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            prepared.selected_option_id = "A"  # type: ignore[misc]
        self.assertIsInstance(prepared.state_before_json, str)
        self.assertIsInstance(prepared.state_after_json, str)

    def test_prepared_terminal_result_snapshot_is_immutable_json_string(self):
        run = _advance_to_scene("s24_golive_readiness")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        self.assertTrue(prepared.is_terminal)
        self.assertIsInstance(prepared.terminal_result_snapshot_json, str)

    # -- SIM-VSLICE-02B test 1: no mutable dict/list request fields ---------

    def test_prepared_payload_contains_no_mutable_dict_or_list_fields(self):
        run = _advance_to_scene("s24_golive_readiness")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        for field in dataclasses.fields(prepared):
            value = getattr(prepared, field.name)
            self.assertNotIsInstance(
                value, (dict, list, MappingProxyType), f"field {field.name!r} is a mutable container: {value!r}"
            )

    # -- SIM-VSLICE-02B test 2: pickle-serializable --------------------------

    def test_prepared_request_is_pickle_serializable(self):
        run = _advance_to_scene("s24_golive_readiness")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        restored = pickle.loads(pickle.dumps(prepared))
        self.assertEqual(restored, prepared)
        self.assertEqual(restored.reconstruct_state_after(), prepared.reconstruct_state_after())

    # -- SIM-VSLICE-02B tests 3/4/5/6: nested payloads cannot be mutated -----

    def test_nested_state_dictionaries_cannot_be_mutated(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        reconstructed = prepared.reconstruct_state_after()
        reconstructed["state"]["projectHealth"] = -999.0
        reconstructed["currentSceneId"] = "tampered"
        self.assertEqual(prepared.reconstruct_state_after(), serialize_run_snapshot(apply_decision(run, "B")))

    def test_nested_flags_and_decision_history_lists_cannot_be_mutated(self):
        run = _advance_to_scene("s03_data_landscape")  # has decisionHistory + flags entries
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        expected_state_before = serialize_run_snapshot(run)
        reconstructed_before = prepared.reconstruct_state_before()
        reconstructed_before["flags"].append("tampered-flag")
        reconstructed_before["decisionHistory"].append({"sequenceNumber": 999, "sceneId": "x", "optionId": "y"})
        if reconstructed_before["decisionHistory"]:
            reconstructed_before["decisionHistory"][0]["optionId"] = "tampered"
        self.assertEqual(prepared.reconstruct_state_before(), expected_state_before)

    def test_nested_terminal_result_values_cannot_be_mutated(self):
        run = _advance_to_scene("s24_golive_readiness")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        expected_snapshot = prepared.reconstruct_terminal_result_snapshot()
        reconstructed = prepared.reconstruct_terminal_result_snapshot()
        reconstructed["endingId"] = "tampered"
        self.assertEqual(prepared.reconstruct_terminal_result_snapshot(), expected_snapshot)

    # -- SIM-VSLICE-02B test 7: parsing twice returns equivalent-but-distinct -

    def test_reconstructing_prepared_json_twice_returns_equivalent_but_independent_objects(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        first = prepared.reconstruct_state_after()
        second = prepared.reconstruct_state_after()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["state"]["projectHealth"] = -1.0
        self.assertNotEqual(first, second)

    # -- 3: prepared request is stored before submit_decision is called ----
    # (proven at the PAGE level in test_scenario_decision_submission_page.py;
    # at the controller level, prepare_ba201_decision itself never calls
    # submit_decision at all -- proven below.)

    def test_prepare_never_calls_submit_decision(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        self.assertEqual(self._submit_calls(client), [])

    # -- 4/5/6/7: retry uses exactly the original request fields ------------

    def test_retry_sends_byte_for_byte_identical_request_fields(self):
        run = _advance_to_scene("s03_data_landscape")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "A")
        expected_sequence = len(run.decisions) + 1

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=expected_sequence)],
        )
        submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)
        first_call_params = self._submit_calls(client)[0][1]

        # Retry: same prepared object, resent verbatim.
        submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)
        second_call_params = self._submit_calls(client)[1][1]

        for field in (
            "p_expected_sequence_number",
            "p_expected_scene_id",
            "p_state_before",
            "p_state_after",
            "p_resulting_scene_id",
            "p_is_terminal",
            "p_terminal_ending_id",
            "p_terminal_result_snapshot",
            "p_idempotency_key",
            "p_selected_option_id",
        ):
            self.assertEqual(
                first_call_params.get(field), second_call_params.get(field), f"field {field} differed on retry"
            )
        self.assertEqual(first_call_params["p_expected_sequence_number"], expected_sequence)
        self.assertEqual(first_call_params["p_expected_scene_id"], "s03_data_landscape")
        self.assertEqual(first_call_params["p_state_before"], serialize_run_snapshot(run))
        self.assertEqual(first_call_params["p_state_after"], serialize_run_snapshot(run_after))

    # -- 8/10: retry never calls get_attempt or re-resolves the pointer -----

    def test_retry_never_calls_get_attempt_or_reresolves_pointer(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        get_attempt_calls_after_prepare = len(self._get_attempt_calls(client))
        table_calls_after_prepare = list(client.table_calls)

        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )
        submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)
        submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

        self.assertEqual(len(self._get_attempt_calls(client)), get_attempt_calls_after_prepare)
        self.assertEqual(client.table_calls, table_calls_after_prepare)

    # -- 9: retry never re-applies the engine --------------------------------

    def test_retry_never_reapplies_the_engine(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        with patch(
            "utils.scenario_learner_controller.apply_decision", wraps=apply_decision
        ) as apply_decision_spy:
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)
            apply_decision_spy.assert_not_called()

    # -- 11/14: nonterminal STATEFUL lost-response retry reaches replay -----

    def test_nonterminal_lost_response_retry_reaches_idempotent_replay(self):
        """SIM-VSLICE-02B: uses a STATEFUL fake (`_StatefulDecisionStore`)
        that genuinely commits/advances internally on the first call, and
        genuinely records an idempotency entry, before raising (simulating
        a lost response) -- so the retry's stable success is a real replay
        of already-committed state, never merely the same canned row
        returned regardless of whether a commit happened."""
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        store = _StatefulDecisionStore(
            attempt_id=attempt_id, initial_sequence_number=1, initial_scene_id=run.current_scene_id
        )
        client.install_stateful_decision_store(store, raise_after_first_commit=True)

        with self.assertRaises(ScenarioLearnerBackendError):
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)
        # The store already committed/advanced internally even though the
        # caller only observed an uncertain failure.
        self.assertEqual(store.commit_count, 1)
        self.assertEqual(store.status, "in_progress")
        self.assertEqual(store.scene_id, run_after.current_scene_id)

        outcome = submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

        self.assertFalse(outcome.is_complete)
        self.assertTrue(outcome.idempotent_replay)
        self.assertEqual(outcome.current_scene_id, run_after.current_scene_id)
        self.assertEqual(store.commit_count, 1)  # retry did NOT commit again
        self.assertEqual(len(self._get_attempt_calls(client)), 1)  # only from prepare

    # -- 12/15: terminal STATEFUL lost-response retry reaches replay --------

    def test_terminal_lost_response_retry_reaches_idempotent_replay(self):
        """SIM-VSLICE-02B stateful equivalent of the nonterminal test above:
        the store genuinely COMPLETES the modeled attempt on the first
        (lost-response) call -- the retry must still reach V68 (never a
        local `attempt_not_in_progress` rejection, since the retry never
        re-fetches the attempt) and receive the stable completed replay."""
        run = _advance_to_scene("s24_golive_readiness")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "A")
        self.assertTrue(run_after.is_complete)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        store = _StatefulDecisionStore(
            attempt_id=attempt_id, initial_sequence_number=len(run.decisions) + 1, initial_scene_id=run.current_scene_id
        )
        client.install_stateful_decision_store(store, raise_after_first_commit=True)

        with self.assertRaises(ScenarioLearnerBackendError):
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)
        self.assertEqual(store.commit_count, 1)
        self.assertEqual(store.status, "completed")
        self.assertIsNone(store.scene_id)

        outcome = submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

        self.assertTrue(outcome.is_complete)
        self.assertTrue(outcome.idempotent_replay)
        self.assertIsNone(outcome.current_scene_id)
        self.assertEqual(store.commit_count, 1)
        self.assertEqual(len(self._get_attempt_calls(client)), 1)

    # -- SIM-VSLICE-02B test 16: persisted response mismatching state_after -

    def test_response_diverging_from_prepared_state_after_is_uncertain(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        divergent_run_after = apply_decision(run, "A")  # NOT what was prepared below ("B")

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=divergent_run_after, sequence_number=1)],
        )

        with self.assertRaises(ScenarioLearnerBackendError):
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

    # -- SIM-VSLICE-02C tests 5/6/7/8/9/10/11: exact response-integrity ------

    def test_nonterminal_response_must_have_exact_status_in_progress(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        # Otherwise fully matching response, but the lifecycle status is
        # NOT the exact "in_progress" a nonterminal prepared request requires.
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1, attempt_status="completed")],
        )

        with self.assertRaises(ScenarioLearnerBackendError):
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

    def test_terminal_response_must_have_exact_status_completed(self):
        run = _advance_to_scene("s24_golive_readiness")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "A")
        self.assertTrue(run_after.is_complete)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        # SIM-VSLICE-02C item 7: "abandoned" is a legitimate V68 lifecycle
        # status, but it is NEVER a valid match for a terminal prepared
        # request -- only an EXACT "completed" is. The prior implementation
        # only checked `attempt_status != "in_progress"`, which "abandoned"
        # would have satisfied, silently accepting it as a completion.
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [
                _submit_row(
                    attempt_id=attempt_id,
                    run_after=run_after,
                    sequence_number=len(run.decisions) + 1,
                    attempt_status="abandoned",
                )
            ],
        )

        with self.assertRaises(ScenarioLearnerBackendError):
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

    def test_nonterminal_response_must_have_null_terminal_result_snapshot(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        # attempt_status/current_scene_id/terminal_ending_id all otherwise
        # correctly reflect a nonterminal response, but a stray non-null
        # terminal_result_snapshot must still be rejected.
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [
                _submit_row(
                    attempt_id=attempt_id,
                    run_after=run_after,
                    sequence_number=1,
                    terminal_result_snapshot={"endingId": "unexpected-ending"},
                )
            ],
        )

        with self.assertRaises(ScenarioLearnerBackendError):
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

    def test_terminal_response_snapshot_must_exactly_equal_prepared_snapshot(self):
        run = _advance_to_scene("s24_golive_readiness")
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "A")
        self.assertTrue(run_after.is_complete)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="A",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        # attempt_status/current_scene_id/terminal_ending_id all correctly
        # reflect the prepared terminal decision, but the returned snapshot
        # object itself does not value-equal the prepared one.
        mismatched_snapshot = dict(serialize_terminal_result(run_after.terminal_result))
        mismatched_snapshot["scoreSummary"] = {"tampered": True}
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [
                _submit_row(
                    attempt_id=attempt_id,
                    run_after=run_after,
                    sequence_number=len(run.decisions) + 1,
                    terminal_result_snapshot=mismatched_snapshot,
                )
            ],
        )

        with self.assertRaises(ScenarioLearnerBackendError):
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

    def test_sequence_fields_are_validated_when_exposed_by_result_type(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        # sequence_number matches, but next_sequence_number does not equal
        # expected_sequence_number + 1.
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [
                _submit_row(
                    attempt_id=attempt_id,
                    run_after=run_after,
                    sequence_number=1,
                    next_sequence_number=99,
                )
            ],
        )

        with self.assertRaises(ScenarioLearnerBackendError):
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

    # -- SIM-VSLICE-02B tests 9/10/11/12/13: submit_prepared does no extra --
    # ---- catalog/content/get_attempt/engine/pointer work -------------------

    def test_submit_prepared_performs_no_catalog_or_content_loading(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        with patch(
            "utils.scenario_learner_controller._load_default_scenario_content"
        ) as load_content_spy:
            submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)
            load_content_spy.assert_not_called()

    def test_submit_prepared_performs_no_current_pointer_resolution(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )
        table_calls_after_prepare = list(client.table_calls)

        submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

        self.assertEqual(client.table_calls, table_calls_after_prepare)

    # -- SIM-VSLICE-02B test 13: a content-load failure cannot block retry --

    def test_content_load_failure_after_preparation_cannot_prevent_retry(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        # Simulates the local scenario-content file becoming unreadable
        # AFTER preparation -- submit_prepared_ba201_decision must still
        # reach V68 successfully, since it never loads content at all.
        with patch(
            "utils.scenario_learner_controller._load_default_scenario_content",
            side_effect=ScenarioLearnerContentError("boom"),
        ):
            outcome = submit_prepared_ba201_decision(_LEARNER_EMAIL_RAW, prepared, client=client)

        self.assertFalse(outcome.is_complete)
        self.assertEqual(outcome.current_scene_id, run_after.current_scene_id)

    # -- ownership: submit_prepared rejects a mismatched learner identity ---

    def test_submit_prepared_rejects_mismatched_learner_identity(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        with self.assertRaises(ScenarioLearnerAccessError):
            submit_prepared_ba201_decision("someone.else@example.com", prepared, client=client)
        self.assertEqual(self._submit_calls(client), [])

    def test_submit_prepared_rejects_missing_email(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)

        prepared = prepare_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        with self.assertRaises(ScenarioLearnerAccessError):
            submit_prepared_ba201_decision(None, prepared, client=client)
        self.assertEqual(self._submit_calls(client), [])

    # -- wrapper equivalence --------------------------------------------------

    def test_submit_ba201_decision_wrapper_equals_prepare_then_submit(self):
        run = start_scenario_run(_CONTENT)
        client, _version_id, attempt_id = self._client_with_attempt(run=run)
        run_after = apply_decision(run, "B")
        client.set_rpc_response(
            "submit_scenario_decision_v1",
            [_submit_row(attempt_id=attempt_id, run_after=run_after, sequence_number=1)],
        )

        view = submit_ba201_decision(
            _LEARNER_EMAIL_RAW,
            attempt_id=attempt_id,
            selected_option_id="B",
            idempotency_key=_IDEMPOTENCY_KEY,
            client=client,
        )

        self.assertFalse(view.is_complete)
        submit_calls = self._submit_calls(client)
        self.assertEqual(len(submit_calls), 1)
        self.assertEqual(submit_calls[0][1]["p_selected_option_id"], "B")
        self.assertEqual(submit_calls[0][1]["p_idempotency_key"], _IDEMPOTENCY_KEY)


if __name__ == "__main__":
    unittest.main()
