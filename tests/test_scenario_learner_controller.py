"""Focused tests for the SIM-VSLICE-01 BA-201 learner start/resume controller.

Uses fakes only -- no live database, no Supabase network calls, no Streamlit
runtime. Loads the real, already-approved BA-201 catalog/content from disk
(via the same `utils.scenario_catalog` / `utils.scenario_schema` path the
controller itself uses) so these tests also prove integration with the
actual repository content, not a synthetic stand-in.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_catalog import resolve_default_scenario_version_path
from utils.scenario_engine import ENGINE_VERSION, serialize_run_snapshot, start_scenario_run
from utils.scenario_learner_controller import (
    BA201_CERTIFICATION_EXAM_NAME,
    BA201_SIMULATION_ID,
    ScenarioAttemptView,
    ScenarioLearnerAccessError,
    ScenarioLearnerBackendError,
    ScenarioLearnerContentError,
    ScenarioLearnerStateError,
    ScenarioLearnerVersionUnavailableError,
    ScenarioOptionView,
    ScenarioSceneView,
    start_or_resume_ba201_attempt,
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

    def set_rpc_response(self, name: str, data) -> None:
        self._rpc_responses[name] = data

    def set_rpc_raise(self, name: str, message: str) -> None:
        self._rpc_raise[name] = _FakeException(message)

    def set_table_rows(self, table_name: str, rows: list[dict]) -> None:
        self._tables[table_name] = rows

    def set_table_raise(self, table_name: str, exc: Exception) -> None:
        self._table_raise[table_name] = exc

    def table(self, name: str):
        self.table_calls.append(name)
        return _FakeTableQuery(self._tables.get(name, []), raise_exc=self._table_raise.get(name))

    def rpc(self, name: str, params: dict):
        self.rpc_calls.append((name, dict(params)))
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


if __name__ == "__main__":
    unittest.main()
