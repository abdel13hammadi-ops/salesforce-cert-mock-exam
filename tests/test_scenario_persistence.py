"""Focused tests for the V68 Scenario Simulator attempt-persistence adapter.

Uses fakes only -- no live database, no Supabase network calls. Mirrors this
repository's existing `FakeSupabase` RPC-fake pattern (see
tests/test_publication_gate.py) rather than inventing a new one.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_persistence import (
    ScenarioAttemptAbandonResult,
    ScenarioAttemptNotFoundError,
    ScenarioAttemptNotInProgressError,
    ScenarioAttemptSnapshot,
    ScenarioAttemptStartResult,
    ScenarioDecisionSubmissionResult,
    ScenarioIdempotencyConflictError,
    ScenarioInsertGuardViolationError,
    ScenarioPersistenceBackendError,
    ScenarioPersistenceValidationError,
    ScenarioSceneConflictError,
    ScenarioSequenceConflictError,
    ScenarioSnapshotConsistencyError,
    ScenarioStateConflictError,
    ScenarioVersionMismatchError,
    abandon_attempt,
    compute_request_fingerprint,
    generate_idempotency_key,
    get_attempt,
    normalize_scenario_persistence_email,
    start_or_resume_attempt,
    submit_decision,
    validate_serialized_engine_state,
)

_UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
_SCENARIO_ID = "22222222-2222-4222-8222-222222222222"
_SCENARIO_VERSION_ID = "33333333-3333-4333-8333-333333333333"
_OTHER_ATTEMPT_ID = "44444444-4444-4444-8444-444444444444"
_EMAIL = "Learner@Example.COM "
_NORMALIZED_EMAIL = "learner@example.com"
_ENGINE_VERSION = "SCENARIO_ENGINE_V1"
_CONTENT_HASH = "a" * 64


def _valid_serialized_state(**overrides) -> dict:
    payload = {
        "simulationId": "sim-001",
        "version": "1.0.0",
        "canonicalContentSha256": _CONTENT_HASH,
        "engineVersion": _ENGINE_VERSION,
        "currentSceneId": "scene-1",
        "state": {"projectHealth": 100},
        "flags": [],
        "decisionHistory": [],
        "isComplete": False,
        "terminalResult": None,
    }
    payload.update(overrides)
    return payload


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


class FakeSupabase:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._responses: dict[str, object] = {}
        self._result_errors: dict[str, str] = {}
        self._raise: dict[str, Exception] = {}

    def set_response(self, name: str, data) -> None:
        self._responses[name] = data

    def set_result_error(self, name: str, message: str) -> None:
        self._result_errors[name] = message

    def set_raise(self, name: str, message: str) -> None:
        self._raise[name] = _FakeException(message)

    def rpc(self, name, params):
        self.calls.append((name, dict(params)))
        if name in self._raise:
            return _FakeRpcBuilder(exception=self._raise[name])
        if name in self._result_errors:
            return _FakeRpcBuilder(data=[], error=self._result_errors[name])
        return _FakeRpcBuilder(data=self._responses.get(name, []))

    def table(self, *_args, **_kwargs):  # pragma: no cover - must never be called.
        raise AssertionError(
            "utils.scenario_persistence must never call client.table(...) -- "
            "every mutation must go through client.rpc(...)"
        )


def _start_row(**overrides) -> dict:
    row = {
        "attempt_id": _ATTEMPT_ID,
        "created": True,
        "scenario_id": _SCENARIO_ID,
        "scenario_version_id": _SCENARIO_VERSION_ID,
        "status": "in_progress",
        "current_scene_id": "scene-1",
        "next_sequence_number": 1,
        "serialized_engine_state": _valid_serialized_state(),
        "engine_version": _ENGINE_VERSION,
        "scenario_content_sha256": _CONTENT_HASH,
        "started_at": "2026-07-19T13:00:00Z",
        "completed_at": None,
        "abandoned_at": None,
        "terminal_ending_id": None,
        "terminal_result_snapshot": None,
    }
    row.update(overrides)
    return row


def _attempt_row(**overrides) -> dict:
    row = {
        "attempt_id": _ATTEMPT_ID,
        "scenario_id": _SCENARIO_ID,
        "scenario_version_id": _SCENARIO_VERSION_ID,
        "status": "in_progress",
        "current_scene_id": "scene-1",
        "next_sequence_number": 1,
        "serialized_engine_state": _valid_serialized_state(),
        "engine_version": _ENGINE_VERSION,
        "scenario_content_sha256": _CONTENT_HASH,
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


def _decision_row(**overrides) -> dict:
    row = {
        "decision_id": "55555555-5555-4555-8555-555555555555",
        "attempt_id": _ATTEMPT_ID,
        "sequence_number": 1,
        "idempotent_replay": False,
        "attempt_status": "in_progress",
        "current_scene_id": "scene-2",
        "next_sequence_number": 2,
        "serialized_engine_state": _valid_serialized_state(currentSceneId="scene-2"),
        "completed_at": None,
        "terminal_ending_id": None,
        "terminal_result_snapshot": None,
    }
    row.update(overrides)
    return row


def _abandon_row(**overrides) -> dict:
    row = {
        "attempt_id": _ATTEMPT_ID,
        "status": "abandoned",
        "abandoned_at": "2026-07-19T13:05:00Z",
    }
    row.update(overrides)
    return row


class EmailNormalizationTests(unittest.TestCase):
    def test_lowercases_and_trims(self):
        self.assertEqual(normalize_scenario_persistence_email(_EMAIL), _NORMALIZED_EMAIL)

    def test_rejects_empty(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            normalize_scenario_persistence_email("   ")

    def test_rejects_none(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            normalize_scenario_persistence_email(None)

    def test_rejects_missing_at_sign(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            normalize_scenario_persistence_email("not-an-email")


class RequestFingerprintTests(unittest.TestCase):
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before={"a": 1, "b": 2},
            state_after={"a": 2, "b": 2},
            resulting_scene_id="scene-2",
            is_terminal=False,
            terminal_ending_id=None,
        )
        kwargs.update(overrides)
        return kwargs

    def test_deterministic_for_identical_inputs(self):
        first = compute_request_fingerprint(**self._base_kwargs())
        second = compute_request_fingerprint(**self._base_kwargs())
        self.assertEqual(first, second)

    def test_is_64_lowercase_hex(self):
        fingerprint = compute_request_fingerprint(**self._base_kwargs())
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")

    def test_indifferent_to_dict_key_order(self):
        first = compute_request_fingerprint(**self._base_kwargs(state_before={"a": 1, "b": 2}))
        second = compute_request_fingerprint(**self._base_kwargs(state_before={"b": 2, "a": 1}))
        self.assertEqual(first, second)

    def test_changes_when_a_covered_field_changes(self):
        first = compute_request_fingerprint(**self._base_kwargs())
        second = compute_request_fingerprint(**self._base_kwargs(selected_option_id="opt-b"))
        self.assertNotEqual(first, second)

    def test_changes_when_sequence_number_changes(self):
        first = compute_request_fingerprint(**self._base_kwargs())
        second = compute_request_fingerprint(**self._base_kwargs(expected_sequence_number=2))
        self.assertNotEqual(first, second)

    def test_changes_when_terminal_flag_changes(self):
        first = compute_request_fingerprint(**self._base_kwargs())
        second = compute_request_fingerprint(
            **self._base_kwargs(is_terminal=True, resulting_scene_id=None, terminal_ending_id="ending-x")
        )
        self.assertNotEqual(first, second)

    def test_changes_when_terminal_result_snapshot_changes(self):
        """SIM-PERSIST-04C: terminal_result_snapshot must be explicitly
        covered by the fingerprint, independently of state_after."""
        first = compute_request_fingerprint(
            **self._base_kwargs(terminal_result_snapshot={"endingId": "ending-a"})
        )
        second = compute_request_fingerprint(
            **self._base_kwargs(terminal_result_snapshot={"endingId": "ending-b"})
        )
        self.assertNotEqual(first, second)

    def test_changes_from_none_terminal_result_snapshot_to_a_value(self):
        first = compute_request_fingerprint(**self._base_kwargs(terminal_result_snapshot=None))
        second = compute_request_fingerprint(
            **self._base_kwargs(terminal_result_snapshot={"endingId": "ending-a"})
        )
        self.assertNotEqual(first, second)

    def test_terminal_result_snapshot_defaults_to_none_and_is_deterministic(self):
        first = compute_request_fingerprint(**self._base_kwargs())
        second = compute_request_fingerprint(**self._base_kwargs())
        self.assertEqual(first, second)

    # SIM-PERSIST-04E Correction 5: compute_request_fingerprint(...) is a
    # public helper and must use this module's own strict helpers, never
    # permissive int(...)/bool(...) coercions, for the scalar inputs it
    # covers.
    def test_string_sequence_number_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(expected_sequence_number="1"))

    def test_bool_is_rejected_as_sequence_number(self):
        """`bool` is a subclass of `int` in Python -- `int(True) == 1` must
        not silently produce a fingerprint as if sequence 1 had been
        supplied."""
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(expected_sequence_number=True))

    def test_zero_sequence_number_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(expected_sequence_number=0))

    def test_string_false_is_rejected_as_is_terminal(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(is_terminal="false"))

    def test_integer_zero_is_rejected_as_is_terminal(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(is_terminal=0))

    def test_integer_one_is_rejected_as_is_terminal(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(is_terminal=1))

    def test_invalid_attempt_id_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(attempt_id="not-a-uuid"))

    def test_empty_expected_scene_id_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(expected_scene_id="   "))

    def test_empty_selected_option_id_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(selected_option_id=""))

    # SIM-PERSIST-04F Correction 4: _require_nonempty_str (used here for
    # expected_scene_id/selected_option_id) must reject an actual non-string
    # value outright, never silently stringify it via str(value or "").
    def test_non_string_expected_scene_id_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(expected_scene_id=12345))

    def test_non_string_selected_option_id_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            compute_request_fingerprint(**self._base_kwargs(selected_option_id=True))


class IdempotencyKeyGenerationTests(unittest.TestCase):
    def test_generates_a_valid_uuid4(self):
        key = generate_idempotency_key()
        self.assertRegex(key, _UUID4_PATTERN)
        parsed = uuid.UUID(key)
        self.assertEqual(parsed.version, 4)

    def test_generates_distinct_keys_across_calls(self):
        keys = {generate_idempotency_key() for _ in range(50)}
        self.assertEqual(len(keys), 50)


class SerializedStateValidationTests(unittest.TestCase):
    def test_accepts_a_well_formed_snapshot(self):
        result = validate_serialized_engine_state(_valid_serialized_state())
        self.assertEqual(result["simulationId"], "sim-001")

    def test_rejects_non_mapping(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(["not", "a", "mapping"])

    def test_rejects_missing_required_key(self):
        payload = _valid_serialized_state()
        del payload["decisionHistory"]
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(payload)

    def test_rejects_malformed_content_hash(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(canonicalContentSha256="not-a-hash"))

    def test_rejects_non_list_decision_history(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(decisionHistory="oops"))

    def test_rejects_non_bool_is_complete(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(isComplete="yes"))

    def test_rejects_non_string_current_scene_id(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(currentSceneId=123))

    def test_allows_null_current_scene_id(self):
        result = validate_serialized_engine_state(_valid_serialized_state(currentSceneId=None))
        self.assertIsNone(result["currentSceneId"])

    def test_rejects_non_json_serializable_content(self):
        payload = _valid_serialized_state()
        payload["state"] = {"cycle": object()}
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(payload)

    # SIM-PERSIST-04E Correction 7: strict local snapshot normalization --
    # this function validates shape, it never silently trims/lowercases a
    # caller-supplied value to make it pass.
    def test_rejects_whitespace_padded_simulation_id(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(simulationId="  sim-001  "))

    def test_rejects_whitespace_padded_version(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(version=" 1.0.0"))

    def test_rejects_whitespace_padded_engine_version(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(engineVersion=_ENGINE_VERSION + " "))

    def test_rejects_uppercase_content_hash(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(canonicalContentSha256=_CONTENT_HASH.upper()))

    def test_rejects_mixed_case_content_hash(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(
                _valid_serialized_state(canonicalContentSha256=("a" * 63) + "A")
            )

    def test_rejects_empty_current_scene_id(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(currentSceneId=""))

    def test_rejects_whitespace_only_current_scene_id(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(currentSceneId="   "))

    def test_rejects_whitespace_padded_current_scene_id(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            validate_serialized_engine_state(_valid_serialized_state(currentSceneId=" scene-1 "))


class StartOrResumeAttemptTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()

    def test_calls_expected_rpc_name_and_parameter_mapping(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row()])
        start_or_resume_attempt(
            self.client,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            initial_current_scene_id="scene-1",
            initial_serialized_state=_valid_serialized_state(),
            engine_version=_ENGINE_VERSION,
            scenario_content_sha256=_CONTENT_HASH,
        )
        self.assertEqual(len(self.client.calls), 1)
        name, params = self.client.calls[0]
        self.assertEqual(name, "start_or_resume_scenario_attempt_v1")
        self.assertEqual(
            set(params.keys()),
            {
                "p_user_email",
                "p_scenario_version_id",
                "p_initial_current_scene_id",
                "p_initial_serialized_state",
                "p_engine_version",
                "p_scenario_content_sha256",
            },
        )
        self.assertEqual(params["p_user_email"], _NORMALIZED_EMAIL)
        self.assertEqual(params["p_scenario_version_id"], _SCENARIO_VERSION_ID)
        self.assertEqual(params["p_initial_current_scene_id"], "scene-1")
        self.assertEqual(params["p_engine_version"], _ENGINE_VERSION)
        self.assertEqual(params["p_scenario_content_sha256"], _CONTENT_HASH)

    def test_created_result_is_parsed(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row(created=True)])
        result = start_or_resume_attempt(
            self.client,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            initial_current_scene_id="scene-1",
            initial_serialized_state=_valid_serialized_state(),
            engine_version=_ENGINE_VERSION,
            scenario_content_sha256=_CONTENT_HASH,
        )
        self.assertIsInstance(result, ScenarioAttemptStartResult)
        self.assertTrue(result.created)
        self.assertEqual(result.attempt_id, _ATTEMPT_ID)
        self.assertEqual(result.status, "in_progress")

    def test_resumed_result_is_parsed(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row(created=False)])
        result = start_or_resume_attempt(
            self.client,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            initial_current_scene_id="scene-1",
            initial_serialized_state=_valid_serialized_state(),
            engine_version=_ENGINE_VERSION,
            scenario_content_sha256=_CONTENT_HASH,
        )
        self.assertFalse(result.created)

    def test_scenario_version_id_identity_mismatch_is_rejected(self):
        self.client.set_response(
            "start_or_resume_scenario_attempt_v1",
            [_start_row(scenario_version_id=_OTHER_ATTEMPT_ID)],
        )
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )

    def test_engine_version_identity_mismatch_is_rejected(self):
        self.client.set_response(
            "start_or_resume_scenario_attempt_v1",
            [_start_row(engine_version="SOME_OTHER_ENGINE")],
        )
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )

    def test_content_hash_identity_mismatch_is_rejected(self):
        self.client.set_response(
            "start_or_resume_scenario_attempt_v1",
            [_start_row(scenario_content_sha256="b" * 64)],
        )
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )

    def test_version_not_found_error_is_mapped(self):
        self.client.set_raise(
            "start_or_resume_scenario_attempt_v1",
            f"scenario_version_not_found: scenario_versions {_SCENARIO_VERSION_ID} does not exist",
        )
        with self.assertRaises(ScenarioVersionMismatchError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )

    def test_version_not_published_error_is_mapped(self):
        self.client.set_raise(
            "start_or_resume_scenario_attempt_v1",
            "scenario_version_not_published: scenario_versions ... is not published (status=draft)",
        )
        with self.assertRaises(ScenarioVersionMismatchError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )

    def test_engine_version_mismatch_rpc_error_is_mapped(self):
        self.client.set_raise("start_or_resume_scenario_attempt_v1", "engine_version_mismatch: nope")
        with self.assertRaises(ScenarioVersionMismatchError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )

    def test_content_hash_mismatch_rpc_error_is_mapped(self):
        self.client.set_raise("start_or_resume_scenario_attempt_v1", "content_hash_mismatch: nope")
        with self.assertRaises(ScenarioVersionMismatchError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )

    def test_invalid_scenario_version_id_is_rejected_locally(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id="not-a-uuid",
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )
        self.assertEqual(self.client.calls, [])

    def test_non_string_initial_current_scene_id_is_rejected_locally(self):
        """SIM-PERSIST-04F Correction 4: initial_current_scene_id goes
        through _require_nonempty_str, which must reject an actual
        non-string value outright rather than silently stringifying it."""
        with self.assertRaises(ScenarioPersistenceValidationError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id=999,
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )
        self.assertEqual(self.client.calls, [])

    def test_malformed_response_missing_field_is_rejected(self):
        row = _start_row()
        del row["engine_version"]
        self.client.set_response("start_or_resume_scenario_attempt_v1", [row])
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )

    def test_malformed_response_empty_rows_is_rejected(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [])
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(
                self.client,
                user_email=_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                initial_current_scene_id="scene-1",
                initial_serialized_state=_valid_serialized_state(),
                engine_version=_ENGINE_VERSION,
                scenario_content_sha256=_CONTENT_HASH,
            )

    def test_never_touches_client_table(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row()])
        start_or_resume_attempt(
            self.client,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            initial_current_scene_id="scene-1",
            initial_serialized_state=_valid_serialized_state(),
            engine_version=_ENGINE_VERSION,
            scenario_content_sha256=_CONTENT_HASH,
        )
        # No assertion needed beyond "no exception" -- FakeSupabase.table()
        # raises AssertionError if it is ever invoked.


class GetAttemptTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()

    def test_calls_expected_rpc_name_and_parameter_mapping(self):
        self.client.set_response("get_scenario_attempt_v1", [_attempt_row()])
        get_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)
        name, params = self.client.calls[0]
        self.assertEqual(name, "get_scenario_attempt_v1")
        self.assertEqual(params, {"p_user_email": _NORMALIZED_EMAIL, "p_attempt_id": _ATTEMPT_ID})

    def test_result_includes_ordered_decisions(self):
        decisions = [
            {"sequenceNumber": 1, "isTerminal": False},
            {"sequenceNumber": 2, "isTerminal": True},
        ]
        self.client.set_response("get_scenario_attempt_v1", [_attempt_row(decisions=decisions)])
        result = get_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)
        self.assertIsInstance(result, ScenarioAttemptSnapshot)
        self.assertEqual(len(result.decisions), 2)
        self.assertEqual(result.decisions[0]["sequenceNumber"], 1)
        self.assertEqual(result.decisions[1]["sequenceNumber"], 2)

    def test_not_found_error_is_mapped(self):
        self.client.set_raise(
            "get_scenario_attempt_v1",
            f"attempt_not_found: no scenario_attempts row {_ATTEMPT_ID} is owned by the requesting learner",
        )
        with self.assertRaises(ScenarioAttemptNotFoundError):
            get_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)

    def test_ownership_mismatch_raises_the_identical_error_as_not_found(self):
        """The RPC intentionally cannot distinguish "no such attempt" from
        "not yours" -- this adapter must not either."""
        self.client.set_raise(
            "get_scenario_attempt_v1",
            f"attempt_not_found: no scenario_attempts row {_ATTEMPT_ID} is owned by the requesting learner",
        )
        try:
            get_attempt(self.client, user_email="someone-else@example.com", attempt_id=_ATTEMPT_ID)
            self.fail("expected ScenarioAttemptNotFoundError")
        except ScenarioAttemptNotFoundError as exc_owned:
            pass
        self.client.set_raise("get_scenario_attempt_v1", "attempt_not_found: unrelated id")
        with self.assertRaises(ScenarioAttemptNotFoundError):
            get_attempt(self.client, user_email=_EMAIL, attempt_id=_OTHER_ATTEMPT_ID)

    def test_decisions_field_must_be_a_list(self):
        row = _attempt_row()
        row["decisions"] = "not-a-list"
        self.client.set_response("get_scenario_attempt_v1", [row])
        with self.assertRaises(ScenarioPersistenceBackendError):
            get_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)

    def test_returned_attempt_id_mismatch_is_rejected(self):
        self.client.set_response("get_scenario_attempt_v1", [_attempt_row(attempt_id=_OTHER_ATTEMPT_ID)])
        with self.assertRaises(ScenarioPersistenceBackendError):
            get_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)

    def test_invalid_attempt_id_is_rejected_locally(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            get_attempt(self.client, user_email=_EMAIL, attempt_id="not-a-uuid")
        self.assertEqual(self.client.calls, [])


class SubmitDecisionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()
        self.state_before = _valid_serialized_state(currentSceneId="scene-1")
        self.state_after = _valid_serialized_state(currentSceneId="scene-2")

    def _submit(self, **overrides):
        kwargs = dict(
            client=self.client,
            user_email=_EMAIL,
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=self.state_before,
            state_after=self.state_after,
            is_terminal=False,
            resulting_scene_id="scene-2",
        )
        kwargs.update(overrides)
        return submit_decision(**kwargs)

    def test_non_terminal_calls_expected_rpc_name_and_parameter_mapping(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row()])
        self._submit()
        name, params = self.client.calls[0]
        self.assertEqual(name, "submit_scenario_decision_v1")
        self.assertEqual(
            set(params.keys()),
            {
                "p_user_email",
                "p_attempt_id",
                "p_idempotency_key",
                "p_expected_sequence_number",
                "p_expected_scene_id",
                "p_selected_option_id",
                "p_request_fingerprint",
                "p_state_before",
                "p_state_after",
                "p_is_terminal",
                "p_resulting_scene_id",
                "p_terminal_ending_id",
                "p_terminal_result_snapshot",
            },
        )
        self.assertEqual(params["p_user_email"], _NORMALIZED_EMAIL)
        self.assertEqual(params["p_attempt_id"], _ATTEMPT_ID)
        self.assertEqual(params["p_is_terminal"], False)
        self.assertEqual(params["p_resulting_scene_id"], "scene-2")
        self.assertIsNone(params["p_terminal_ending_id"])
        self.assertIsNone(params["p_terminal_result_snapshot"])
        self.assertRegex(params["p_idempotency_key"], _UUID4_PATTERN)
        self.assertRegex(params["p_request_fingerprint"], _HEX64_PATTERN)

    def test_terminal_calls_expected_rpc_name_and_parameter_mapping(self):
        self.client.set_response(
            "submit_scenario_decision_v1",
            [
                _decision_row(
                    idempotent_replay=False,
                    attempt_status="completed",
                    current_scene_id=None,
                    completed_at="2026-07-19T13:10:00Z",
                    terminal_ending_id="ending_x",
                    terminal_result_snapshot={"endingId": "ending_x"},
                )
            ],
        )
        result = self._submit(
            is_terminal=True,
            resulting_scene_id=None,
            terminal_ending_id="ending_x",
            terminal_result_snapshot={"endingId": "ending_x"},
            state_after=_valid_serialized_state(
                currentSceneId=None, isComplete=True, terminalResult={"endingId": "ending_x"}
            ),
        )
        name, params = self.client.calls[0]
        self.assertEqual(params["p_is_terminal"], True)
        self.assertIsNone(params["p_resulting_scene_id"])
        self.assertEqual(params["p_terminal_ending_id"], "ending_x")
        self.assertEqual(params["p_terminal_result_snapshot"], {"endingId": "ending_x"})
        self.assertIsInstance(result, ScenarioDecisionSubmissionResult)
        self.assertEqual(result.attempt_status, "completed")
        self.assertIsNone(result.current_scene_id)
        self.assertEqual(result.terminal_ending_id, "ending_x")

    def test_explicit_idempotency_key_is_forwarded_unchanged(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row()])
        explicit_key = str(uuid.uuid4())
        self._submit(idempotency_key=explicit_key)
        _, params = self.client.calls[0]
        self.assertEqual(params["p_idempotency_key"], explicit_key)

    def test_matching_explicit_request_fingerprint_is_accepted(self):
        """SIM-PERSIST-04F: a caller-supplied request_fingerprint that
        actually matches the fingerprint computed from this request's own
        inputs is accepted (and is what is sent to the RPC -- there is only
        ever one correct value, so this is indistinguishable from omitting
        it)."""
        self.client.set_response("submit_scenario_decision_v1", [_decision_row()])
        expected_fp = compute_request_fingerprint(
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=self.state_before,
            state_after=self.state_after,
            resulting_scene_id="scene-2",
            is_terminal=False,
            terminal_ending_id=None,
            terminal_result_snapshot=None,
        )
        self._submit(request_fingerprint=expected_fp)
        _, params = self.client.calls[0]
        self.assertEqual(params["p_request_fingerprint"], expected_fp)

    def test_mismatched_explicit_request_fingerprint_is_rejected(self):
        """SIM-PERSIST-04F: a caller-supplied request_fingerprint is no
        longer forwarded unchanged just because it is correctly formatted --
        it must also match the fingerprint this module itself computes from
        the request. A format-valid but content-inconsistent value raises
        request_fingerprint_mismatch and never reaches the RPC."""
        wrong_fp = "9" * 64
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(request_fingerprint=wrong_fp)
        self.assertEqual(self.client.calls, [])

    def test_safe_idempotent_retry_result_is_parsed(self):
        self.client.set_response(
            "submit_scenario_decision_v1",
            [_decision_row(idempotent_replay=True)],
        )
        result = self._submit()
        self.assertTrue(result.idempotent_replay)

    def test_conflicting_idempotency_error_is_mapped(self):
        self.client.set_raise(
            "submit_scenario_decision_v1",
            f"idempotency_key_conflict: idempotency_key ... was already used on attempt {_ATTEMPT_ID} "
            "with a different request fingerprint",
        )
        with self.assertRaises(ScenarioIdempotencyConflictError):
            self._submit()

    def test_sequence_conflict_is_mapped(self):
        self.client.set_raise(
            "submit_scenario_decision_v1",
            "sequence_mismatch: expected sequence 1 but attempt ... is at sequence 3",
        )
        with self.assertRaises(ScenarioSequenceConflictError):
            self._submit()

    def test_scene_conflict_is_mapped(self):
        self.client.set_raise(
            "submit_scenario_decision_v1",
            "scene_mismatch: expected current scene scene-1 but attempt ... is at scene scene-9",
        )
        with self.assertRaises(ScenarioSceneConflictError):
            self._submit()

    def test_state_before_conflict_is_mapped(self):
        self.client.set_raise(
            "submit_scenario_decision_v1",
            "state_before_mismatch: supplied state_before does not match attempt ...",
        )
        with self.assertRaises(ScenarioStateConflictError):
            self._submit()

    def test_attempt_not_found_is_mapped(self):
        self.client.set_raise("submit_scenario_decision_v1", "attempt_not_found: ...")
        with self.assertRaises(ScenarioAttemptNotFoundError):
            self._submit()

    def test_attempt_not_in_progress_is_mapped(self):
        self.client.set_raise("submit_scenario_decision_v1", "attempt_not_in_progress: ...")
        with self.assertRaises(ScenarioAttemptNotInProgressError):
            self._submit()

    def test_unmapped_rpc_error_is_wrapped_as_backend_error(self):
        self.client.set_raise("submit_scenario_decision_v1", "some_unexpected_database_error: boom")
        with self.assertRaises(ScenarioPersistenceBackendError):
            self._submit()

    def test_result_error_attribute_path_is_also_mapped(self):
        self.client.set_result_error("submit_scenario_decision_v1", "sequence_mismatch: via result.error")
        with self.assertRaises(ScenarioSequenceConflictError):
            self._submit()

    def test_terminal_decision_rejects_a_resulting_scene_id(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(is_terminal=True, resulting_scene_id="scene-2", terminal_ending_id="ending_x",
                          terminal_result_snapshot={"endingId": "ending_x"})
        self.assertEqual(self.client.calls, [])

    def test_terminal_decision_requires_ending_id(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(is_terminal=True, resulting_scene_id=None, terminal_ending_id=None,
                          terminal_result_snapshot={"endingId": "ending_x"})
        self.assertEqual(self.client.calls, [])

    def test_terminal_decision_requires_result_snapshot(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(is_terminal=True, resulting_scene_id=None, terminal_ending_id="ending_x",
                          terminal_result_snapshot=None)
        self.assertEqual(self.client.calls, [])

    def test_non_terminal_decision_requires_resulting_scene_id(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(resulting_scene_id=None)
        self.assertEqual(self.client.calls, [])

    def test_non_terminal_decision_rejects_terminal_ending_id(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(terminal_ending_id="ending_x")
        self.assertEqual(self.client.calls, [])

    def test_non_terminal_decision_rejects_terminal_result_snapshot(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(terminal_result_snapshot={"endingId": "ending_x"})
        self.assertEqual(self.client.calls, [])

    def test_invalid_sequence_number_is_rejected_locally(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(expected_sequence_number=0)
        self.assertEqual(self.client.calls, [])

    def test_malformed_state_before_is_rejected_locally(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(state_before={"not": "a valid engine snapshot"})
        self.assertEqual(self.client.calls, [])

    def test_returned_attempt_id_mismatch_is_rejected(self):
        self.client.set_response(
            "submit_scenario_decision_v1", [_decision_row(attempt_id=_OTHER_ATTEMPT_ID)]
        )
        with self.assertRaises(ScenarioPersistenceBackendError):
            self._submit()

    def test_malformed_response_missing_field_is_rejected(self):
        row = _decision_row()
        del row["decision_id"]
        self.client.set_response("submit_scenario_decision_v1", [row])
        with self.assertRaises(ScenarioPersistenceBackendError):
            self._submit()


class AbandonAttemptTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()

    def test_calls_expected_rpc_name_and_parameter_mapping(self):
        self.client.set_response("abandon_scenario_attempt_v1", [_abandon_row()])
        abandon_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)
        name, params = self.client.calls[0]
        self.assertEqual(name, "abandon_scenario_attempt_v1")
        self.assertEqual(params, {"p_user_email": _NORMALIZED_EMAIL, "p_attempt_id": _ATTEMPT_ID})

    def test_result_is_parsed(self):
        self.client.set_response("abandon_scenario_attempt_v1", [_abandon_row()])
        result = abandon_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)
        self.assertIsInstance(result, ScenarioAttemptAbandonResult)
        self.assertEqual(result.status, "abandoned")
        self.assertIsNotNone(result.abandoned_at)

    def test_not_found_is_mapped(self):
        self.client.set_raise("abandon_scenario_attempt_v1", "attempt_not_found: ...")
        with self.assertRaises(ScenarioAttemptNotFoundError):
            abandon_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)

    def test_not_in_progress_is_mapped(self):
        self.client.set_raise("abandon_scenario_attempt_v1", "attempt_not_in_progress: ...")
        with self.assertRaises(ScenarioAttemptNotInProgressError):
            abandon_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)

    def test_invalid_attempt_id_is_rejected_locally(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            abandon_attempt(self.client, user_email=_EMAIL, attempt_id="not-a-uuid")
        self.assertEqual(self.client.calls, [])


class NoDirectTableMutationTests(unittest.TestCase):
    """`FakeSupabase.table(...)` raises unconditionally -- these tests pass
    only if every adapter function reaches the end of a successful call
    without ever invoking it."""

    def setUp(self):
        self.client = FakeSupabase()

    def test_start_or_resume_never_touches_table(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row()])
        start_or_resume_attempt(
            self.client,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            initial_current_scene_id="scene-1",
            initial_serialized_state=_valid_serialized_state(),
            engine_version=_ENGINE_VERSION,
            scenario_content_sha256=_CONTENT_HASH,
        )

    def test_get_attempt_never_touches_table(self):
        self.client.set_response("get_scenario_attempt_v1", [_attempt_row()])
        get_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)

    def test_submit_decision_never_touches_table(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row()])
        submit_decision(
            self.client,
            user_email=_EMAIL,
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=_valid_serialized_state(currentSceneId="scene-1"),
            state_after=_valid_serialized_state(currentSceneId="scene-2"),
            is_terminal=False,
            resulting_scene_id="scene-2",
        )

    def test_abandon_attempt_never_touches_table(self):
        self.client.set_response("abandon_scenario_attempt_v1", [_abandon_row()])
        abandon_attempt(self.client, user_email=_EMAIL, attempt_id=_ATTEMPT_ID)


class StrictInputTypeTests(unittest.TestCase):
    """SIM-PERSIST-04C Correction 6: strict Python input types."""

    def setUp(self):
        self.client = FakeSupabase()
        self.state_before = _valid_serialized_state(currentSceneId="scene-1")
        self.state_after = _valid_serialized_state(currentSceneId="scene-2")

    def _submit(self, **overrides):
        kwargs = dict(
            client=self.client,
            user_email=_EMAIL,
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=self.state_before,
            state_after=self.state_after,
            is_terminal=False,
            resulting_scene_id="scene-2",
        )
        kwargs.update(overrides)
        return submit_decision(**kwargs)

    def test_string_is_terminal_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(is_terminal="false")
        self.assertEqual(self.client.calls, [])

    def test_integer_is_terminal_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(is_terminal=0)
        self.assertEqual(self.client.calls, [])

    def test_bool_is_rejected_as_sequence_number(self):
        """`bool` is a subclass of `int` in Python -- `expected_sequence_number=True`
        must still be rejected, not silently accepted as `1`."""
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(expected_sequence_number=True)
        self.assertEqual(self.client.calls, [])

    def test_string_sequence_number_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(expected_sequence_number="1")
        self.assertEqual(self.client.calls, [])

    def test_float_sequence_number_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(expected_sequence_number=1.0)
        self.assertEqual(self.client.calls, [])

    def test_non_v4_uuid_idempotency_key_is_rejected(self):
        # Third group's leading hex digit '1' makes this a version-1 UUID.
        non_v4_uuid = "12345678-1234-1234-8234-123456789abc"
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(idempotency_key=non_v4_uuid)
        self.assertEqual(self.client.calls, [])

    def test_v4_uuid_idempotency_key_is_accepted(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row()])
        v4_uuid = str(uuid.uuid4())
        self._submit(idempotency_key=v4_uuid)
        _, params = self.client.calls[0]
        self.assertEqual(params["p_idempotency_key"], v4_uuid)

    def test_uppercase_request_fingerprint_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(request_fingerprint="A" * 64)
        self.assertEqual(self.client.calls, [])

    def test_mixed_case_request_fingerprint_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(request_fingerprint=("a" * 63) + "F")
        self.assertEqual(self.client.calls, [])

    # SIM-PERSIST-04F Correction 4: selected_option_id goes through
    # _require_nonempty_str, which must reject an actual non-string value
    # outright rather than silently stringifying it.
    def test_non_string_selected_option_id_is_rejected(self):
        with self.assertRaises(ScenarioPersistenceValidationError):
            self._submit(selected_option_id=42)
        self.assertEqual(self.client.calls, [])

    def test_whitespace_padded_lowercase_request_fingerprint_is_stripped_and_accepted(self):
        """SIM-PERSIST-04F: stripping happens before the computed-fingerprint
        comparison, so a whitespace-padded value that, once stripped,
        actually matches the fingerprint computed from this request's own
        inputs is still accepted."""
        self.client.set_response("submit_scenario_decision_v1", [_decision_row()])
        expected_fp = compute_request_fingerprint(
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=self.state_before,
            state_after=self.state_after,
            resulting_scene_id="scene-2",
            is_terminal=False,
            terminal_ending_id=None,
            terminal_result_snapshot=None,
        )
        padded_fp = "  " + expected_fp + "  "
        self._submit(request_fingerprint=padded_fp)
        _, params = self.client.calls[0]
        self.assertEqual(params["p_request_fingerprint"], expected_fp)


class InitialStateConsistencyTests(unittest.TestCase):
    """SIM-PERSIST-04C Correction 4: snapshot IDENTITY/LIFECYCLE integrity
    for `start_or_resume_attempt(...)`'s `initial_serialized_state`."""

    def setUp(self):
        self.client = FakeSupabase()

    def _start(self, **state_overrides):
        state = _valid_serialized_state(**state_overrides)
        return start_or_resume_attempt(
            self.client,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            initial_current_scene_id="scene-1",
            initial_serialized_state=state,
            engine_version=_ENGINE_VERSION,
            scenario_content_sha256=_CONTENT_HASH,
        )

    def test_consistent_initial_state_is_accepted(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row()])
        self._start()
        self.assertEqual(len(self.client.calls), 1)

    def test_engine_version_mismatch_is_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._start(engineVersion="SOME_OTHER_ENGINE")
        self.assertEqual(self.client.calls, [])

    def test_content_hash_mismatch_is_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._start(canonicalContentSha256="b" * 64)
        self.assertEqual(self.client.calls, [])

    def test_current_scene_id_mismatch_is_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._start(currentSceneId="scene-999")
        self.assertEqual(self.client.calls, [])

    def test_is_complete_true_is_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._start(isComplete=True)
        self.assertEqual(self.client.calls, [])

    def test_non_null_terminal_result_is_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._start(terminalResult={"endingId": "premature"})
        self.assertEqual(self.client.calls, [])


class DecisionSnapshotConsistencyTests(unittest.TestCase):
    """SIM-PERSIST-04C Correction 4: snapshot IDENTITY/LIFECYCLE integrity
    for `submit_decision(...)`'s `state_before`/`state_after`."""

    def setUp(self):
        self.client = FakeSupabase()

    def _submit(self, *, state_before=None, state_after=None, **overrides):
        kwargs = dict(
            client=self.client,
            user_email=_EMAIL,
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=state_before or _valid_serialized_state(currentSceneId="scene-1"),
            state_after=state_after or _valid_serialized_state(currentSceneId="scene-2"),
            is_terminal=False,
            resulting_scene_id="scene-2",
        )
        kwargs.update(overrides)
        return submit_decision(**kwargs)

    def test_consistent_non_terminal_decision_is_accepted(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row()])
        self._submit()
        self.assertEqual(len(self.client.calls), 1)

    def test_identity_field_mismatch_between_before_and_after_is_rejected(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(state_after=_valid_serialized_state(currentSceneId="scene-2", simulationId="different-sim"))
        self.assertEqual(self.client.calls, [])

    def test_state_before_current_scene_id_must_match_expected_scene_id(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(
                state_before=_valid_serialized_state(currentSceneId="not-scene-1"),
                expected_scene_id="scene-1",
            )
        self.assertEqual(self.client.calls, [])

    def test_state_before_is_complete_must_be_false(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(state_before=_valid_serialized_state(currentSceneId="scene-1", isComplete=True))
        self.assertEqual(self.client.calls, [])

    def test_non_terminal_state_after_current_scene_id_must_match_resulting_scene_id(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(
                state_after=_valid_serialized_state(currentSceneId="scene-9"),
                resulting_scene_id="scene-2",
            )
        self.assertEqual(self.client.calls, [])

    def test_non_terminal_state_after_is_complete_must_be_false(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(state_after=_valid_serialized_state(currentSceneId="scene-2", isComplete=True))
        self.assertEqual(self.client.calls, [])

    def test_non_terminal_state_after_terminal_result_must_be_null(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(
                state_after=_valid_serialized_state(
                    currentSceneId="scene-2", terminalResult={"endingId": "premature"}
                )
            )
        self.assertEqual(self.client.calls, [])

    def test_terminal_state_after_current_scene_id_must_be_null(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(
                is_terminal=True,
                resulting_scene_id=None,
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"endingId": "ending_x"},
                state_after=_valid_serialized_state(
                    currentSceneId="scene-2", isComplete=True, terminalResult={"endingId": "ending_x"}
                ),
            )
        self.assertEqual(self.client.calls, [])

    def test_terminal_state_after_is_complete_must_be_true(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(
                is_terminal=True,
                resulting_scene_id=None,
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"endingId": "ending_x"},
                state_after=_valid_serialized_state(
                    currentSceneId=None, isComplete=False, terminalResult={"endingId": "ending_x"}
                ),
            )
        self.assertEqual(self.client.calls, [])

    def test_terminal_state_after_terminal_result_must_be_an_object(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(
                is_terminal=True,
                resulting_scene_id=None,
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"endingId": "ending_x"},
                state_after=_valid_serialized_state(
                    currentSceneId=None, isComplete=True, terminalResult="not-an-object"
                ),
            )
        self.assertEqual(self.client.calls, [])

    def test_terminal_result_snapshot_must_equal_state_after_terminal_result(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit(
                is_terminal=True,
                resulting_scene_id=None,
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"endingId": "ending_x"},
                state_after=_valid_serialized_state(
                    currentSceneId=None, isComplete=True, terminalResult={"endingId": "a-different-ending"}
                ),
            )
        self.assertEqual(self.client.calls, [])

    def test_consistent_terminal_decision_is_accepted(self):
        self.client.set_response(
            "submit_scenario_decision_v1",
            [
                _decision_row(
                    attempt_status="completed",
                    current_scene_id=None,
                    terminal_ending_id="ending_x",
                    terminal_result_snapshot={"endingId": "ending_x"},
                )
            ],
        )
        self._submit(
            is_terminal=True,
            resulting_scene_id=None,
            terminal_ending_id="ending_x",
            terminal_result_snapshot={"endingId": "ending_x"},
            state_after=_valid_serialized_state(
                currentSceneId=None, isComplete=True, terminalResult={"endingId": "ending_x"}
            ),
        )
        self.assertEqual(len(self.client.calls), 1)


class StableIdempotentReplayTests(unittest.TestCase):
    """SIM-PERSIST-04C Correction 3: the adapter must faithfully pass through
    whatever stable, decision-derived result the RPC returns for an
    idempotent replay -- never overriding or re-deriving it client-side."""

    def setUp(self):
        self.client = FakeSupabase()
        self.state_before = _valid_serialized_state(currentSceneId="scene-1")
        self.state_after = _valid_serialized_state(currentSceneId="scene-2")

    def test_replay_reflects_the_original_decisions_own_post_state_not_a_later_one(self):
        # Simulates what the corrected SQL now returns for a late retry of an
        # OLDER, non-terminal decision after the attempt has since been
        # completed by a LATER decision: attempt_status is still
        # 'in_progress' (that decision's own outcome), current_scene_id is
        # still that decision's own resulting scene ('scene-2'), NOT null /
        # 'completed' (which is what the attempt's CURRENT state would say).
        self.client.set_response(
            "submit_scenario_decision_v1",
            [
                _decision_row(
                    idempotent_replay=True,
                    attempt_status="in_progress",
                    current_scene_id="scene-2",
                    next_sequence_number=2,
                    serialized_engine_state=self.state_after,
                    completed_at=None,
                    terminal_ending_id=None,
                    terminal_result_snapshot=None,
                )
            ],
        )
        result = submit_decision(
            self.client,
            user_email=_EMAIL,
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=self.state_before,
            state_after=self.state_after,
            is_terminal=False,
            resulting_scene_id="scene-2",
        )
        self.assertTrue(result.idempotent_replay)
        self.assertEqual(result.attempt_status, "in_progress")
        self.assertEqual(result.current_scene_id, "scene-2")
        self.assertEqual(result.next_sequence_number, 2)
        self.assertIsNone(result.completed_at)
        self.assertIsNone(result.terminal_ending_id)
        self.assertIsNone(result.terminal_result_snapshot)


class NewExceptionPrefixMappingTests(unittest.TestCase):
    """SIM-PERSIST-04C Correction 8: every new SQL exception prefix this
    correction introduced maps to a focused Python exception class."""

    def setUp(self):
        self.client = FakeSupabase()

    def _valid_start_kwargs(self):
        return dict(
            client=self.client,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            initial_current_scene_id="scene-1",
            initial_serialized_state=_valid_serialized_state(),
            engine_version=_ENGINE_VERSION,
            scenario_content_sha256=_CONTENT_HASH,
        )

    def _valid_submit_kwargs(self):
        return dict(
            client=self.client,
            user_email=_EMAIL,
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=_valid_serialized_state(currentSceneId="scene-1"),
            state_after=_valid_serialized_state(currentSceneId="scene-2"),
            is_terminal=False,
            resulting_scene_id="scene-2",
        )

    def test_invalid_initial_state_identity_maps_to_snapshot_consistency_error(self):
        self.client.set_raise(
            "start_or_resume_scenario_attempt_v1",
            "invalid_initial_state_identity: initial_serialized_state.engineVersion does not match",
        )
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            start_or_resume_attempt(**self._valid_start_kwargs())

    def test_invalid_initial_state_lifecycle_maps_to_snapshot_consistency_error(self):
        self.client.set_raise(
            "start_or_resume_scenario_attempt_v1",
            "invalid_initial_state_lifecycle: initial_serialized_state.isComplete must be false",
        )
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            start_or_resume_attempt(**self._valid_start_kwargs())

    def test_attempt_insert_guard_violation_maps_to_insert_guard_violation_error(self):
        self.client.set_raise(
            "start_or_resume_scenario_attempt_v1",
            "attempt_insert_guard_violation: scenario_attempts ... insert guard not set",
        )
        with self.assertRaises(ScenarioInsertGuardViolationError):
            start_or_resume_attempt(**self._valid_start_kwargs())

    def test_state_identity_mismatch_maps_to_snapshot_consistency_error(self):
        self.client.set_raise(
            "submit_scenario_decision_v1",
            "state_identity_mismatch: state_before and state_after immutable identity fields do not match",
        )
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            submit_decision(**self._valid_submit_kwargs())

    def test_state_lifecycle_mismatch_maps_to_snapshot_consistency_error(self):
        self.client.set_raise(
            "submit_scenario_decision_v1",
            "state_lifecycle_mismatch: state_before.currentSceneId does not match p_expected_scene_id",
        )
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            submit_decision(**self._valid_submit_kwargs())

    def test_terminal_result_mismatch_maps_to_snapshot_consistency_error(self):
        self.client.set_raise(
            "submit_scenario_decision_v1",
            "terminal_result_mismatch: state_after.terminalResult does not equal p_terminal_result_snapshot",
        )
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            submit_decision(**self._valid_submit_kwargs())

    def test_decision_insert_guard_violation_maps_to_insert_guard_violation_error(self):
        self.client.set_raise(
            "submit_scenario_decision_v1",
            "decision_insert_guard_violation: scenario_decisions ... insert guard not set",
        )
        with self.assertRaises(ScenarioInsertGuardViolationError):
            submit_decision(**self._valid_submit_kwargs())

    def test_terminal_ending_mismatch_maps_to_snapshot_consistency_error(self):
        """SIM-PERSIST-04E Correction 4: the new terminal_ending_mismatch
        SQL exception prefix maps to ScenarioSnapshotConsistencyError."""
        self.client.set_raise(
            "submit_scenario_decision_v1",
            "terminal_ending_mismatch: p_terminal_result_snapshot.endingId does not equal p_terminal_ending_id",
        )
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            submit_decision(
                client=self.client,
                user_email=_EMAIL,
                attempt_id=_ATTEMPT_ID,
                expected_sequence_number=1,
                expected_scene_id="scene-1",
                selected_option_id="opt-a",
                state_before=_valid_serialized_state(currentSceneId="scene-1"),
                state_after=_valid_serialized_state(
                    currentSceneId=None, isComplete=True, terminalResult={"endingId": "ending_x"}
                ),
                is_terminal=True,
                resulting_scene_id=None,
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"endingId": "ending_x"},
            )


class TerminalEndingConsistencyTests(unittest.TestCase):
    """SIM-PERSIST-04E Correction 4: terminal_result_snapshot.endingId must
    be a normalized, non-empty string EXACTLY equal to terminal_ending_id --
    checked locally, before any RPC call, mirroring
    submit_scenario_decision_v1's own terminal_ending_mismatch check."""

    def setUp(self):
        self.client = FakeSupabase()

    def _submit_terminal(self, *, terminal_ending_id, terminal_result_snapshot):
        return submit_decision(
            client=self.client,
            user_email=_EMAIL,
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=_valid_serialized_state(currentSceneId="scene-1"),
            state_after=_valid_serialized_state(
                currentSceneId=None, isComplete=True, terminalResult=terminal_result_snapshot
            ),
            is_terminal=True,
            resulting_scene_id=None,
            terminal_ending_id=terminal_ending_id,
            terminal_result_snapshot=terminal_result_snapshot,
        )

    def test_matching_ending_identities_are_accepted(self):
        self.client.set_response(
            "submit_scenario_decision_v1",
            [
                _decision_row(
                    attempt_status="completed",
                    current_scene_id=None,
                    terminal_ending_id="ending_x",
                    terminal_result_snapshot={"endingId": "ending_x"},
                )
            ],
        )
        self._submit_terminal(terminal_ending_id="ending_x", terminal_result_snapshot={"endingId": "ending_x"})
        self.assertEqual(len(self.client.calls), 1)

    def test_contradictory_ending_identities_are_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit_terminal(
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"endingId": "ending_mismatch"},
            )
        self.assertEqual(self.client.calls, [])

    def test_missing_ending_id_in_snapshot_is_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit_terminal(
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"scoreBand": "distinction"},
            )
        self.assertEqual(self.client.calls, [])

    def test_non_string_ending_id_in_snapshot_is_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit_terminal(
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"endingId": 12345},
            )
        self.assertEqual(self.client.calls, [])

    def test_whitespace_padded_ending_id_in_snapshot_is_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit_terminal(
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"endingId": "  ending_x  "},
            )
        self.assertEqual(self.client.calls, [])

    def test_empty_ending_id_in_snapshot_is_rejected_locally(self):
        with self.assertRaises(ScenarioSnapshotConsistencyError):
            self._submit_terminal(
                terminal_ending_id="ending_x",
                terminal_result_snapshot={"endingId": "   "},
            )
        self.assertEqual(self.client.calls, [])


class StrictRpcResponseParsingTests(unittest.TestCase):
    """SIM-PERSIST-04E Correction 6: every RPC response field this module
    depends on is parsed with a strict helper, never a permissive
    bool(...)/int(...)/dict(value or {}) coercion. A malformed response
    raises ScenarioPersistenceBackendError, never silently coerced into
    something that happens to still parse."""

    def setUp(self):
        self.client = FakeSupabase()

    def _start_kwargs(self):
        return dict(
            client=self.client,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            initial_current_scene_id="scene-1",
            initial_serialized_state=_valid_serialized_state(),
            engine_version=_ENGINE_VERSION,
            scenario_content_sha256=_CONTENT_HASH,
        )

    def _submit_kwargs(self):
        return dict(
            client=self.client,
            user_email=_EMAIL,
            attempt_id=_ATTEMPT_ID,
            expected_sequence_number=1,
            expected_scene_id="scene-1",
            selected_option_id="opt-a",
            state_before=_valid_serialized_state(currentSceneId="scene-1"),
            state_after=_valid_serialized_state(currentSceneId="scene-2"),
            is_terminal=False,
            resulting_scene_id="scene-2",
        )

    def test_string_created_is_rejected(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row(created="false")])
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(**self._start_kwargs())

    def test_string_idempotent_replay_is_rejected(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row(idempotent_replay="false")])
        with self.assertRaises(ScenarioPersistenceBackendError):
            submit_decision(**self._submit_kwargs())

    def test_bool_next_sequence_number_is_rejected(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row(next_sequence_number=True)])
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(**self._start_kwargs())

    def test_string_sequence_number_is_rejected(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row(sequence_number="1")])
        with self.assertRaises(ScenarioPersistenceBackendError):
            submit_decision(**self._submit_kwargs())

    def test_list_serialized_engine_state_is_rejected(self):
        """Previously `dict(value or {})` would have silently coerced this
        falsy-but-malformed `[]` into an empty object instead of rejecting
        it."""
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row(serialized_engine_state=[])])
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(**self._start_kwargs())

    def test_invalid_uuid_response_attempt_id_is_rejected(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row(attempt_id="not-a-uuid")])
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(**self._start_kwargs())

    def test_invalid_uuid_response_decision_id_is_rejected(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row(decision_id="not-a-uuid")])
        with self.assertRaises(ScenarioPersistenceBackendError):
            submit_decision(**self._submit_kwargs())

    def test_invalid_lifecycle_status_is_rejected(self):
        self.client.set_response("start_or_resume_scenario_attempt_v1", [_start_row(status="bogus_status")])
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(**self._start_kwargs())

    def test_invalid_lifecycle_attempt_status_is_rejected(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row(attempt_status="bogus_status")])
        with self.assertRaises(ScenarioPersistenceBackendError):
            submit_decision(**self._submit_kwargs())

    def test_uppercase_scenario_content_sha256_response_is_rejected(self):
        self.client.set_response(
            "start_or_resume_scenario_attempt_v1",
            [_start_row(scenario_content_sha256=_CONTENT_HASH.upper())],
        )
        with self.assertRaises(ScenarioPersistenceBackendError):
            start_or_resume_attempt(**self._start_kwargs())

    def test_non_object_terminal_result_snapshot_response_is_rejected(self):
        self.client.set_response(
            "submit_scenario_decision_v1",
            [_decision_row(terminal_result_snapshot="not-an-object")],
        )
        with self.assertRaises(ScenarioPersistenceBackendError):
            submit_decision(**self._submit_kwargs())

    def test_null_terminal_result_snapshot_response_is_accepted(self):
        self.client.set_response("submit_scenario_decision_v1", [_decision_row(terminal_result_snapshot=None)])
        result = submit_decision(**self._submit_kwargs())
        self.assertIsNone(result.terminal_result_snapshot)


if __name__ == "__main__":
    unittest.main()
