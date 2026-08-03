"""Focused tests for Engine V2 start/resume/submit orchestration.

Uses deterministic fakes only for unit tests (A-AJ). Optional disposable
PostgreSQL smoke (``TestScenarioOrchestrationV2DisposableSmoke``) runs only
when Docker is available and never touches production.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_engine_v2 import (
    ENGINE_VERSION,
    ScenarioDecisionInputV2,
    apply_decision_v2,
    build_learner_scene_view,
    build_learner_terminal_view,
    build_scenario_content_v2,
    start_scenario_run_v2,
)
from utils.scenario_orchestration_v2 import (
    LearnerAttemptSummaryV2,
    ScenarioOrchestrationV2CanonicalDecisionSequenceError,
    ScenarioOrchestrationV2IdempotencyConflictError,
    ScenarioOrchestrationV2IdentityMismatchError,
    ScenarioOrchestrationV2InvalidRequestError,
    ScenarioOrchestrationV2MalformedPersistenceResponseError,
    ScenarioOrchestrationV2PersistenceDependencyError,
    ScenarioOrchestrationV2ReplayMismatchError,
    ScenarioOrchestrationV2SceneConflictError,
    ScenarioOrchestrationV2SequenceConflictError,
    ScenarioOrchestrationV2StaleRunError,
    StartOrResumeScenarioRunResultV2,
    ScenarioOrchestrationSubmissionContextV2,
    load_canonical_scenario_decisions_v2,
    resume_and_replay_scenario_run_v2,
    start_or_resume_scenario_run_v2,
    submit_scenario_decision_v2,
)
from utils.scenario_persistence import (
    ScenarioPersistenceValidationError,
    compute_request_fingerprint,
)
from utils.scenario_persistence_v2 import (
    serialize_learner_scene_view_v2,
    serialize_run_snapshot_v2,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "scenario_engine_v2_vslice_1_1_0.json"

_EMAIL = "learner@example.com"
_SCENARIO_VERSION_ID = "33333333-3333-4333-8333-333333333333"
_SCENARIO_ID = "22222222-2222-4222-8222-222222222222"

HAPPY_PATH_DECISIONS = (
    (1, "SC001-C01", "opt-sc001-c01-a"),
    (2, "SC001-C02", "opt-sc001-c02-a"),
    (3, "SC001-C03", "opt-sc001-c03-a"),
    (4, "SC001-C04", "opt-sc001-c04-a"),
)

_FROZEN_ENVELOPE_KEYS = frozenset(
    {
        "envelopeVersion",
        "simulationId",
        "version",
        "schemaVersion",
        "engineVersion",
        "canonicalContentSha256",
        "currentSceneId",
        "expectedSequenceNumber",
        "isComplete",
        "state",
        "counters",
        "flags",
        "decisionHistory",
        "optionDisplayOrderByScene",
        "selectedVariantIdByScene",
        "routingResolutions",
        "terminalResult",
    }
)

_START_RPC_KEYS = frozenset(
    {
        "p_user_email",
        "p_scenario_version_id",
        "p_initial_current_scene_id",
        "p_initial_serialized_state",
        "p_engine_version",
        "p_scenario_content_sha256",
        "p_attempt_id",
    }
)

_SUBMIT_RPC_KEYS = frozenset(
    {
        "p_user_email",
        "p_attempt_id",
        "p_idempotency_key",
        "p_expected_sequence_number",
        "p_expected_scene_id",
        "p_selected_option_id",
        "p_state_before",
        "p_state_after",
        "p_resulting_scene_id",
        "p_is_terminal",
        "p_terminal_ending_id",
        "p_terminal_result_snapshot",
        "p_request_fingerprint",
    }
)

_UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_HIDDEN_LEARNER_FIELDS = frozenset(
    {
        "state",
        "counters",
        "flags",
        "decisionHistory",
        "routingResolutions",
        "evaluationTier",
        "debriefSeed",
        "stateDelta",
        "canonicalContentSha256",
        "contentHash",
        "formula",
        "classifier",
    }
)


class _FakeException(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _load_document() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _new_attempt_id() -> str:
    return str(uuid.uuid4())


def _decision_id() -> str:
    return str(uuid.uuid4())


def _json_default(value: Any) -> Any:
    import datetime

    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


class FakeOrchestrationPersistence:
    """In-memory persistence fake that keeps canonical rows replay-consistent."""

    def __init__(
        self,
        *,
        content,
        scenario_version_id: str = _SCENARIO_VERSION_ID,
        scenario_id: str = _SCENARIO_ID,
    ) -> None:
        self.content = content
        self.scenario_version_id = scenario_version_id
        self.scenario_id = scenario_id
        self.attempts: Dict[str, Dict[str, Any]] = {}
        self.decisions: Dict[str, List[Dict[str, Any]]] = {}
        self.idempotency: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.start_calls: List[Dict[str, Any]] = []
        self.submit_calls: List[Dict[str, Any]] = []
        self.load_calls: List[Tuple[str, str]] = []
        self.list_calls: List[Tuple[str, str]] = []
        self.submit_raise: Optional[str] = None
        self.load_raise: Optional[str] = None
        self.cache_corrupt_for: Optional[str] = None
        self.identity_override: Optional[Dict[str, Any]] = None

    def list_learner_attempt_summaries_v2(
        self,
        *,
        user_email: str,
        scenario_version_id: str,
    ) -> Tuple[LearnerAttemptSummaryV2, ...]:
        email = str(user_email).strip().lower()
        version_id = str(scenario_version_id)
        self.list_calls.append((email, version_id))
        rows: List[LearnerAttemptSummaryV2] = []
        for row in self.attempts.values():
            if str(row.get("user_email") or "").strip().lower() != email:
                continue
            if str(row.get("scenario_version_id")) != version_id:
                continue
            rows.append(
                LearnerAttemptSummaryV2(
                    attempt_id=str(row["attempt_id"]),
                    status=str(row["status"]),
                    started_at=row.get("started_at"),
                    completed_at=row.get("completed_at"),
                )
            )
        return tuple(rows)

    def call_start_or_resume_scenario_attempt_v1(self, params: Mapping[str, Any]) -> List[Dict[str, Any]]:
        self.start_calls.append(copy.deepcopy(dict(params)))
        user_email = str(params["p_user_email"]).strip().lower()
        version_id = str(params["p_scenario_version_id"])
        requested_attempt_id = params.get("p_attempt_id")
        envelope = copy.deepcopy(dict(params["p_initial_serialized_state"]))

        existing_in_progress = None
        for row in self.attempts.values():
            if (
                str(row.get("user_email") or "").strip().lower() == user_email
                and str(row.get("scenario_version_id")) == version_id
                and row.get("status") == "in_progress"
            ):
                existing_in_progress = row
                break

        if existing_in_progress is not None:
            if requested_attempt_id is not None and str(requested_attempt_id) != str(
                existing_in_progress["attempt_id"]
            ):
                raise _FakeException(
                    "attempt_id_conflict: supplied p_attempt_id does not match the "
                    "caller's existing in_progress attempt for this scenario version"
                )
            row = existing_in_progress
            created = False
        elif requested_attempt_id is not None and str(requested_attempt_id) in self.attempts:
            # Explicit id already known (e.g. completed row resumed via start_or_resume).
            row = self.attempts[str(requested_attempt_id)]
            created = False
        else:
            attempt_id = str(requested_attempt_id) if requested_attempt_id is not None else str(uuid.uuid4())
            if attempt_id in self.attempts:
                raise _FakeException("attempt_id_collision: the supplied p_attempt_id is already in use")
            created = True
            self.attempts[attempt_id] = {
                "attempt_id": attempt_id,
                "user_email": user_email,
                "scenario_id": self.scenario_id,
                "scenario_version_id": self.scenario_version_id,
                "status": "in_progress",
                "current_scene_id": params["p_initial_current_scene_id"],
                "next_sequence_number": envelope.get("expectedSequenceNumber", 1),
                "serialized_engine_state": envelope,
                "engine_version": params["p_engine_version"],
                "scenario_content_sha256": params["p_scenario_content_sha256"],
                "started_at": "2026-08-01T00:00:00Z",
                "completed_at": None,
                "abandoned_at": None,
                "terminal_ending_id": None,
                "terminal_result_snapshot": None,
            }
            self.decisions[attempt_id] = []
            row = self.attempts[attempt_id]

        return [
            {
                "attempt_id": row["attempt_id"],
                "created": created,
                "scenario_id": row["scenario_id"],
                "scenario_version_id": row["scenario_version_id"],
                "status": row["status"],
                "current_scene_id": row["current_scene_id"],
                "next_sequence_number": row["next_sequence_number"],
                "serialized_engine_state": copy.deepcopy(row["serialized_engine_state"]),
                "engine_version": row["engine_version"],
                "scenario_content_sha256": row["scenario_content_sha256"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "abandoned_at": row["abandoned_at"],
                "terminal_ending_id": row["terminal_ending_id"],
                "terminal_result_snapshot": row["terminal_result_snapshot"],
            }
        ]

    def call_submit_scenario_decision_v1(self, params: Mapping[str, Any]) -> List[Dict[str, Any]]:
        if self.submit_raise:
            raise _FakeException(self.submit_raise)
        self.submit_calls.append(copy.deepcopy(dict(params)))
        attempt_id = params["p_attempt_id"]
        if attempt_id not in self.attempts:
            raise _FakeException("attempt_not_found: missing")
        row = self.attempts[attempt_id]
        if row["status"] != "in_progress":
            raise _FakeException("attempt_not_in_progress: done")

        expected_seq = params["p_expected_sequence_number"]
        idem_key = params["p_idempotency_key"]
        idem_store_key = (attempt_id, idem_key)

        fingerprint = compute_request_fingerprint(
            attempt_id=attempt_id,
            expected_sequence_number=expected_seq,
            expected_scene_id=params["p_expected_scene_id"],
            selected_option_id=params["p_selected_option_id"],
            state_before=params["p_state_before"],
            state_after=params["p_state_after"],
            resulting_scene_id=params["p_resulting_scene_id"],
            is_terminal=params["p_is_terminal"],
            terminal_ending_id=params["p_terminal_ending_id"],
            terminal_result_snapshot=params["p_terminal_result_snapshot"],
        )
        if fingerprint != params["p_request_fingerprint"]:
            raise _FakeException("invalid_request_fingerprint: mismatch")

        if idem_store_key in self.idempotency:
            stored = self.idempotency[idem_store_key]
            if stored["fingerprint"] != fingerprint:
                raise _FakeException("idempotency_key_conflict: different request")
            replay_response = copy.deepcopy(stored["response"])
            replay_response["idempotent_replay"] = True
            return [replay_response]

        if expected_seq != row["next_sequence_number"]:
            raise _FakeException(f"sequence_mismatch: expected {row['next_sequence_number']}, got {expected_seq}")
        if params["p_expected_scene_id"] != row["current_scene_id"]:
            raise _FakeException(f"scene_mismatch: expected {row['current_scene_id']!r}")

        state_before = copy.deepcopy(row["serialized_engine_state"])
        if params["p_state_before"] != state_before:
            raise _FakeException("state_before_mismatch: envelope changed")

        decision_id = _decision_id()
        self.decisions[attempt_id].append(
            {
                "sequenceNumber": expected_seq,
                "expectedSceneId": params["p_expected_scene_id"],
                "selectedOptionId": params["p_selected_option_id"],
                "stateBefore": copy.deepcopy(params["p_state_before"]),
                "stateAfter": copy.deepcopy(params["p_state_after"]),
                "resultingSceneId": params["p_resulting_scene_id"],
                "isTerminal": params["p_is_terminal"],
            }
        )
        row["serialized_engine_state"] = copy.deepcopy(dict(params["p_state_after"]))
        row["current_scene_id"] = params["p_resulting_scene_id"]
        row["next_sequence_number"] = expected_seq + 1
        if params["p_is_terminal"]:
            row["status"] = "completed"
            row["completed_at"] = "2026-08-01T00:05:00Z"
            row["terminal_ending_id"] = params["p_terminal_ending_id"]
            row["terminal_result_snapshot"] = copy.deepcopy(params["p_terminal_result_snapshot"])
        response = {
            "decision_id": decision_id,
            "attempt_id": attempt_id,
            "sequence_number": expected_seq,
            "idempotent_replay": False,
            "attempt_status": row["status"],
            "current_scene_id": row["current_scene_id"],
            "next_sequence_number": row["next_sequence_number"],
            "serialized_engine_state": copy.deepcopy(row["serialized_engine_state"]),
            "completed_at": row["completed_at"],
            "terminal_ending_id": row["terminal_ending_id"],
            "terminal_result_snapshot": row["terminal_result_snapshot"],
        }
        self.idempotency[idem_store_key] = {"fingerprint": fingerprint, "response": response}
        return [copy.deepcopy(response)]

    def load_attempt_snapshot(self, *, user_email: str, attempt_id: str) -> Dict[str, Any]:
        if self.load_raise:
            raise _FakeException(self.load_raise)
        self.load_calls.append((user_email, attempt_id))
        if attempt_id not in self.attempts:
            raise _FakeException("attempt_not_found: missing")
        row = copy.deepcopy(self.attempts[attempt_id])
        if self.identity_override:
            row.update(copy.deepcopy(self.identity_override))
        if self.cache_corrupt_for == attempt_id:
            row["serialized_engine_state"] = copy.deepcopy(row["serialized_engine_state"])
            existing_state = dict(row["serialized_engine_state"].get("state") or {})
            for key in existing_state:
                existing_state[key] = existing_state[key] + 1
                break
            row["serialized_engine_state"]["state"] = existing_state
        row["decisions"] = copy.deepcopy(self.decisions.get(attempt_id, []))
        row["updated_at"] = "2026-08-01T00:01:00Z"
        return row

    def seed_existing_attempt(
        self,
        *,
        attempt_id: str,
        run,
        decisions: Sequence[Tuple[int, str, str]] = (),
    ) -> None:
        """Seed a resumed attempt with canonical decision history."""
        envelope = serialize_run_snapshot_v2(run)
        self.attempts[attempt_id] = {
            "attempt_id": attempt_id,
            "scenario_id": self.scenario_id,
            "scenario_version_id": self.scenario_version_id,
            "status": "completed" if run.is_complete else "in_progress",
            "current_scene_id": run.current_scene_id,
            "next_sequence_number": run.expected_sequence_number,
            "serialized_engine_state": envelope,
            "engine_version": ENGINE_VERSION,
            "scenario_content_sha256": self.content.canonical_content_sha256,
            "started_at": "2026-08-01T00:00:00Z",
            "completed_at": "2026-08-01T00:05:00Z" if run.is_complete else None,
            "abandoned_at": None,
            "terminal_ending_id": run.terminal_result.outcome_id if run.terminal_result else None,
            "terminal_result_snapshot": envelope.get("terminalResult"),
        }
        self.decisions[attempt_id] = [
            {
                "sequenceNumber": seq,
                "expectedSceneId": scene,
                "selectedOptionId": opt,
            }
            for seq, scene, opt in decisions
        ]


class OrchestrationV2TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _load_document()
        self.content = build_scenario_content_v2(copy.deepcopy(self.document))
        self.attempt_id = _new_attempt_id()
        self.persistence = FakeOrchestrationPersistence(content=self.content)

    def _start(self, *, attempt_id: Optional[str] = None) -> StartOrResumeScenarioRunResultV2:
        return start_or_resume_scenario_run_v2(
            self.content,
            persistence=self.persistence,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=attempt_id or self.attempt_id,
        )

    def _first_visible_option(self, result: StartOrResumeScenarioRunResultV2) -> str:
        assert result.learner_view.scene_view is not None
        return result.learner_view.scene_view.options[0].id


class TestStartFlow(OrchestrationV2TestCase):
    def test_a_new_attempt_uses_stable_preselected_uuid(self):
        fixed = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        result = self._start(attempt_id=fixed)
        self.assertEqual(result.attempt_id, fixed)

    def test_b_engine_and_rpc_share_same_uuid(self):
        fixed = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self._start(attempt_id=fixed)
        self.assertEqual(self.persistence.start_calls[0]["p_attempt_id"], fixed)
        run = start_scenario_run_v2(self.content, attempt_id=fixed)
        self.assertEqual(run.attempt_id, fixed)

    def test_c_start_rpc_receives_exactly_seven_parameters(self):
        self._start()
        self.assertEqual(len(self.persistence.start_calls), 1)
        params = self.persistence.start_calls[0]
        self.assertEqual(frozenset(params.keys()), _START_RPC_KEYS)
        self.assertEqual(len(params), 7)

    def test_d_initial_envelope_has_exact_seventeen_key_shape(self):
        self._start()
        envelope = self.persistence.start_calls[0]["p_initial_serialized_state"]
        self.assertEqual(frozenset(envelope.keys()), _FROZEN_ENVELOPE_KEYS)
        self.assertEqual(len(envelope), 17)

    def test_e_new_attempt_returns_created_true(self):
        result = self._start()
        self.assertTrue(result.created)

    def test_f_existing_attempt_returns_created_false(self):
        self._start()
        result = self._start()
        self.assertFalse(result.created)


class TestResumeFlow(OrchestrationV2TestCase):
    def test_g_resume_loads_canonical_decisions(self):
        run = start_scenario_run_v2(self.content, attempt_id=self.attempt_id)
        run = apply_decision_v2(
            run,
            ScenarioDecisionInputV2(1, HAPPY_PATH_DECISIONS[0][1], HAPPY_PATH_DECISIONS[0][2]),
        )
        self.persistence.seed_existing_attempt(
            attempt_id=self.attempt_id,
            run=run,
            decisions=HAPPY_PATH_DECISIONS[:1],
        )
        replayed, snapshot = resume_and_replay_scenario_run_v2(
            self.content,
            persistence=self.persistence,
            user_email=_EMAIL,
            attempt_id=self.attempt_id,
        )
        self.assertEqual(len(snapshot.decisions), 1)
        self.assertEqual(replayed.expected_sequence_number, 2)

    def test_h_resume_replay_ignores_cache_as_authority(self):
        run = start_scenario_run_v2(self.content, attempt_id=self.attempt_id)
        self.persistence.seed_existing_attempt(attempt_id=self.attempt_id, run=run)
        self.persistence.cache_corrupt_for = self.attempt_id
        with self.assertRaises(ScenarioOrchestrationV2ReplayMismatchError):
            resume_and_replay_scenario_run_v2(
                self.content,
                persistence=self.persistence,
                user_email=_EMAIL,
                attempt_id=self.attempt_id,
            )

    def test_i_resume_rejects_cache_mismatch(self):
        self.test_h_resume_replay_ignores_cache_as_authority()

    def test_j_resume_rejects_trusted_identity_mismatch(self):
        run = start_scenario_run_v2(self.content, attempt_id=self.attempt_id)
        self.persistence.seed_existing_attempt(attempt_id=self.attempt_id, run=run)
        self.persistence.identity_override = {"scenario_content_sha256": "f" * 64}
        with self.assertRaises(ScenarioOrchestrationV2IdentityMismatchError):
            resume_and_replay_scenario_run_v2(
                self.content,
                persistence=self.persistence,
                user_email=_EMAIL,
                attempt_id=self.attempt_id,
            )


class TestCanonicalDecisionLoading(OrchestrationV2TestCase):
    def test_k_rejects_sequence_gaps(self):
        rows = [
            {"sequenceNumber": 1, "expectedSceneId": "SC001-C01", "selectedOptionId": "opt-sc001-c01-a"},
            {"sequenceNumber": 3, "expectedSceneId": "SC001-C02", "selectedOptionId": "opt-sc001-c02-a"},
        ]
        with self.assertRaises(ScenarioOrchestrationV2CanonicalDecisionSequenceError):
            load_canonical_scenario_decisions_v2(rows, attempt_id=self.attempt_id)

    def test_l_rejects_duplicate_sequences(self):
        rows = [
            {"sequenceNumber": 1, "expectedSceneId": "SC001-C01", "selectedOptionId": "opt-sc001-c01-a"},
            {"sequenceNumber": 1, "expectedSceneId": "SC001-C02", "selectedOptionId": "opt-sc001-c02-a"},
        ]
        with self.assertRaises(ScenarioOrchestrationV2CanonicalDecisionSequenceError):
            load_canonical_scenario_decisions_v2(rows, attempt_id=self.attempt_id)

    def test_m_rejects_bool_sequence(self):
        rows = [{"sequenceNumber": True, "expectedSceneId": "SC001-C01", "selectedOptionId": "opt-a"}]
        with self.assertRaises(ScenarioOrchestrationV2CanonicalDecisionSequenceError):
            load_canonical_scenario_decisions_v2(rows, attempt_id=self.attempt_id)

    def test_n_rejects_wrong_attempt_id(self):
        rows = [
            {
                "attemptId": "99999999-9999-4999-8999-999999999999",
                "sequenceNumber": 1,
                "expectedSceneId": "SC001-C01",
                "selectedOptionId": "opt-sc001-c01-a",
            }
        ]
        with self.assertRaises(ScenarioOrchestrationV2CanonicalDecisionSequenceError):
            load_canonical_scenario_decisions_v2(rows, attempt_id=self.attempt_id)

    def test_o_decision_rows_are_not_mutated(self):
        rows = [
            {"sequenceNumber": 1, "expectedSceneId": "SC001-C01", "selectedOptionId": "opt-sc001-c01-a"},
        ]
        before = copy.deepcopy(rows)
        load_canonical_scenario_decisions_v2(rows, attempt_id=self.attempt_id)
        self.assertEqual(rows, before)


class TestMalformedPersistedDecisionElements(OrchestrationV2TestCase):
    """HIGH-01 regression: a non-mapping element inside the *persisted*
    ``decisions`` array (as returned by the injected persistence port, before
    ``load_canonical_scenario_decisions_v2`` ever sees it) must fail closed
    through ``_parse_attempt_snapshot_row`` with the malformed-persistence
    domain error -- never a raw ``TypeError`` from ``dict(item)``."""

    def _seed_with_raw_decisions(self, decisions_raw: List[Any]) -> None:
        run = start_scenario_run_v2(self.content, attempt_id=self.attempt_id)
        self.persistence.seed_existing_attempt(attempt_id=self.attempt_id, run=run)
        self.persistence.decisions[self.attempt_id] = decisions_raw

    def _resume(self):
        return resume_and_replay_scenario_run_v2(
            self.content,
            persistence=self.persistence,
            user_email=_EMAIL,
            attempt_id=self.attempt_id,
        )

    def test_decisions_list_containing_integer_raises_malformed_response_error(self):
        self._seed_with_raw_decisions([123])
        with self.assertRaises(ScenarioOrchestrationV2MalformedPersistenceResponseError):
            self._resume()

    def test_decisions_list_containing_string_raises_malformed_response_error(self):
        self._seed_with_raw_decisions(["not-a-mapping"])
        with self.assertRaises(ScenarioOrchestrationV2MalformedPersistenceResponseError):
            self._resume()

    def test_decisions_list_containing_list_raises_malformed_response_error(self):
        self._seed_with_raw_decisions([[1, "SC001-C01", "opt-sc001-c01-a"]])
        with self.assertRaises(ScenarioOrchestrationV2MalformedPersistenceResponseError):
            self._resume()

    def test_decisions_list_containing_null_raises_malformed_response_error(self):
        self._seed_with_raw_decisions([None])
        with self.assertRaises(ScenarioOrchestrationV2MalformedPersistenceResponseError):
            self._resume()

    def test_decisions_list_containing_bool_raises_malformed_response_error(self):
        self._seed_with_raw_decisions([True])
        with self.assertRaises(ScenarioOrchestrationV2MalformedPersistenceResponseError):
            self._resume()

    def test_malformed_element_is_not_silently_skipped(self):
        # A valid row at index 0 followed by a malformed row at index 1 must
        # still fail closed -- proving the malformed element is never simply
        # dropped from the collection while the valid ones are kept.
        valid_row = {
            "sequenceNumber": 1,
            "expectedSceneId": "SC001-C01",
            "selectedOptionId": "opt-sc001-c01-a",
        }
        self._seed_with_raw_decisions([valid_row, 999])
        with self.assertRaises(ScenarioOrchestrationV2MalformedPersistenceResponseError):
            self._resume()

    def test_malformed_decisions_input_is_not_mutated(self):
        raw_decisions: List[Any] = [123, "also-bad"]
        self._seed_with_raw_decisions(raw_decisions)
        before = copy.deepcopy(self.persistence.decisions[self.attempt_id])
        with self.assertRaises(ScenarioOrchestrationV2MalformedPersistenceResponseError):
            self._resume()
        self.assertEqual(self.persistence.decisions[self.attempt_id], before)

    def test_raw_type_error_does_not_escape_malformed_decision_parsing(self):
        self._seed_with_raw_decisions([123])
        try:
            self._resume()
        except ScenarioOrchestrationV2MalformedPersistenceResponseError:
            pass
        except TypeError:
            self.fail("raw TypeError escaped _parse_attempt_snapshot_row via dict(item)")
        else:
            self.fail("expected ScenarioOrchestrationV2MalformedPersistenceResponseError")


class TestSubmitFlow(OrchestrationV2TestCase):
    def test_p_visible_valid_option_can_be_submitted(self):
        start = self._start()
        option_id = self._first_visible_option(start)
        result = submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=option_id,
        )
        self.assertEqual(result.sequence_number, 1)
        self.assertFalse(result.idempotent_replay)

    def test_q_hidden_unknown_option_is_rejected(self):
        start = self._start()
        with self.assertRaises(ScenarioOrchestrationV2InvalidRequestError):
            submit_scenario_decision_v2(
                self.content,
                persistence=self.persistence,
                submission_context=start.submission_context,
                selected_option_id="opt-not-on-scene",
            )

    def test_r_submit_rpc_receives_exactly_thirteen_parameters(self):
        start = self._start()
        submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=self._first_visible_option(start),
        )
        params = self.persistence.submit_calls[0]
        self.assertEqual(frozenset(params.keys()), _SUBMIT_RPC_KEYS)
        self.assertEqual(len(params), 13)

    def test_s_before_and_after_envelopes_match_frozen_contract(self):
        start = self._start()
        submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=self._first_visible_option(start),
        )
        params = self.persistence.submit_calls[0]
        self.assertEqual(frozenset(params["p_state_before"].keys()), _FROZEN_ENVELOPE_KEYS)
        self.assertEqual(frozenset(params["p_state_after"].keys()), _FROZEN_ENVELOPE_KEYS)

    def test_t_expected_sequence_is_correct(self):
        start = self._start()
        submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=self._first_visible_option(start),
        )
        self.assertEqual(self.persistence.submit_calls[0]["p_expected_sequence_number"], 1)

    def test_u_expected_scene_is_correct(self):
        start = self._start()
        submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=self._first_visible_option(start),
        )
        self.assertEqual(self.persistence.submit_calls[0]["p_expected_scene_id"], "SC001-C01")

    def test_v_idempotency_key_is_uuidv4(self):
        start = self._start()
        result = submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=self._first_visible_option(start),
        )
        self.assertRegex(result.idempotency_key, _UUID4_PATTERN)

    def test_w_same_key_identical_retry_succeeds_idempotently(self):
        start = self._start()
        option = self._first_visible_option(start)
        first = submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=option,
        )
        retry = submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=option,
            idempotency_key=first.idempotency_key,
        )
        self.assertTrue(retry.idempotent_replay)
        self.assertEqual(len(self.persistence.decisions[self.attempt_id]), 1)

    def test_x_same_key_changed_request_fails_closed(self):
        start = self._start()
        option = self._first_visible_option(start)
        first = submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=option,
        )
        alt_option = next(
            oid for oid in start.submission_context.visible_option_ids if oid != option
        )
        tampered = ScenarioOrchestrationSubmissionContextV2(
            user_email=start.submission_context.user_email,
            attempt_id=start.submission_context.attempt_id,
            scenario_version_id=start.submission_context.scenario_version_id,
            expected_sequence_number=start.submission_context.expected_sequence_number,
            expected_scene_id=start.submission_context.expected_scene_id,
            cached_envelope=copy.deepcopy(start.submission_context.cached_envelope),
            visible_option_ids=start.submission_context.visible_option_ids + (alt_option,),
            run=start.submission_context.run,
        )
        with self.assertRaises(ScenarioOrchestrationV2IdempotencyConflictError):
            submit_scenario_decision_v2(
                self.content,
                persistence=self.persistence,
                submission_context=tampered,
                selected_option_id=alt_option,
                idempotency_key=first.idempotency_key,
            )

    def test_y_stale_sequence_becomes_typed_conflict(self):
        start = self._start()
        self.persistence.submit_raise = "sequence_mismatch: stale"
        with self.assertRaises(ScenarioOrchestrationV2SequenceConflictError):
            submit_scenario_decision_v2(
                self.content,
                persistence=self.persistence,
                submission_context=start.submission_context,
                selected_option_id=self._first_visible_option(start),
            )

    def test_z_stale_scene_becomes_typed_conflict(self):
        start = self._start()
        self.persistence.submit_raise = "scene_mismatch: stale"
        with self.assertRaises(ScenarioOrchestrationV2SceneConflictError):
            submit_scenario_decision_v2(
                self.content,
                persistence=self.persistence,
                submission_context=start.submission_context,
                selected_option_id=self._first_visible_option(start),
            )

    def test_aa_rpc_success_followed_by_canonical_reload_and_replay(self):
        start = self._start()
        result = submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=self._first_visible_option(start),
        )
        self.assertGreaterEqual(len(self.persistence.load_calls), 1)
        self.assertEqual(result.run.expected_sequence_number, 2)

    def test_ab_rpc_success_but_persisted_replay_mismatch_fails_closed(self):
        start = self._start()
        original_load = self.persistence.load_attempt_snapshot

        def corrupt_after_submit(*, user_email, attempt_id):
            row = original_load(user_email=user_email, attempt_id=attempt_id)
            if self.persistence.submit_calls:
                state = dict(row["serialized_engine_state"]["state"])
                for key in state:
                    state[key] = state[key] + 1
                    break
                row["serialized_engine_state"]["state"] = state
            return row

        self.persistence.load_attempt_snapshot = corrupt_after_submit  # type: ignore[method-assign]
        with self.assertRaises(ScenarioOrchestrationV2ReplayMismatchError):
            submit_scenario_decision_v2(
                self.content,
                persistence=self.persistence,
                submission_context=start.submission_context,
                selected_option_id=self._first_visible_option(start),
            )


class TestLearnerSafeResults(OrchestrationV2TestCase):
    def test_ac_active_next_scene_returns_learner_safe_view(self):
        start = self._start()
        self.assertIsNotNone(start.learner_view.scene_view)
        self.assertIsNone(start.learner_view.terminal_view)
        scene = start.learner_view.scene_view
        assert scene is not None
        self.assertTrue(scene.scene_id)
        self.assertGreater(len(scene.options), 0)

    def test_ad_terminal_completion_returns_learner_safe_terminal_view(self):
        start = self._start()
        ctx = start.submission_context
        for seq, scene_id, option_id in HAPPY_PATH_DECISIONS:
            if ctx.run.is_complete:
                break
            visible = {option.id for option in build_learner_scene_view(ctx.run).options}
            chosen = option_id if option_id in visible else next(iter(visible))
            submitted = submit_scenario_decision_v2(
                self.content,
                persistence=self.persistence,
                submission_context=ctx,
                selected_option_id=chosen,
            )
            ctx = submitted.submission_context
        self.assertTrue(submitted.run.is_complete)
        self.assertIsNotNone(submitted.learner_view.terminal_view)
        self.assertIsNone(submitted.learner_view.scene_view)

    def test_ae_hidden_engine_fields_are_not_exposed(self):
        start = self._start()
        assert start.learner_view.scene_view is not None
        payload = json.dumps(serialize_learner_scene_view_v2(start.learner_view.scene_view))
        lowered = payload.lower()
        for token in _HIDDEN_LEARNER_FIELDS:
            self.assertNotIn(token.lower(), lowered)

    def test_af_raw_rpc_dictionaries_never_escape(self):
        start = self._start()
        self.assertIsInstance(start, StartOrResumeScenarioRunResultV2)
        self.assertNotIsInstance(start.attempt_id, dict)

    def test_ag_nested_input_response_aliases_do_not_escape(self):
        start = self._start()
        envelope = self.persistence.start_calls[0]["p_initial_serialized_state"]
        envelope["state"]["probe"] = "mutated"
        self.assertNotIn("probe", start.run.state)


class TestErrorContractAndImmutability(OrchestrationV2TestCase):
    def test_ah_persistence_dependency_errors_are_wrapped(self):
        self.persistence.load_raise = "connection reset"
        with self.assertRaises(ScenarioOrchestrationV2PersistenceDependencyError):
            self._start()

    def test_ai_inputs_remain_immutable(self):
        doc_copy = copy.deepcopy(self.document)
        content_copy = build_scenario_content_v2(copy.deepcopy(doc_copy))
        self._start()
        self.assertEqual(self.document, doc_copy)


class TestInvalidEmailTranslation(OrchestrationV2TestCase):
    """MEDIUM-01 regression: the reused V1 email validator's
    ``ScenarioPersistenceValidationError`` must never escape a V2
    orchestration entry point -- it must be translated into
    ``ScenarioOrchestrationV2InvalidRequestError`` with the original
    exception preserved as ``__cause__``."""

    _INVALID_EMAIL = "not-an-email-address"

    def test_invalid_email_during_start_raises_orchestration_invalid_request_error(self):
        with self.assertRaises(ScenarioOrchestrationV2InvalidRequestError):
            start_or_resume_scenario_run_v2(
                self.content,
                persistence=self.persistence,
                user_email=self._INVALID_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                attempt_id=self.attempt_id,
            )

    def test_invalid_email_during_start_never_leaks_v1_exception_type(self):
        try:
            start_or_resume_scenario_run_v2(
                self.content,
                persistence=self.persistence,
                user_email=self._INVALID_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                attempt_id=self.attempt_id,
            )
        except ScenarioPersistenceValidationError:
            self.fail("raw V1 ScenarioPersistenceValidationError escaped start_or_resume_scenario_run_v2")
        except ScenarioOrchestrationV2InvalidRequestError:
            pass
        else:
            self.fail("expected ScenarioOrchestrationV2InvalidRequestError")

    def test_invalid_email_during_resume_raises_orchestration_invalid_request_error(self):
        run = start_scenario_run_v2(self.content, attempt_id=self.attempt_id)
        self.persistence.seed_existing_attempt(attempt_id=self.attempt_id, run=run)
        with self.assertRaises(ScenarioOrchestrationV2InvalidRequestError):
            resume_and_replay_scenario_run_v2(
                self.content,
                persistence=self.persistence,
                user_email=self._INVALID_EMAIL,
                attempt_id=self.attempt_id,
            )

    def test_invalid_email_during_submit_raises_orchestration_invalid_request_error(self):
        start = self._start()
        option_id = self._first_visible_option(start)
        tampered_context = ScenarioOrchestrationSubmissionContextV2(
            user_email=self._INVALID_EMAIL,
            attempt_id=start.submission_context.attempt_id,
            scenario_version_id=start.submission_context.scenario_version_id,
            expected_sequence_number=start.submission_context.expected_sequence_number,
            expected_scene_id=start.submission_context.expected_scene_id,
            cached_envelope=copy.deepcopy(start.submission_context.cached_envelope),
            visible_option_ids=start.submission_context.visible_option_ids,
            run=start.submission_context.run,
        )
        with self.assertRaises(ScenarioOrchestrationV2InvalidRequestError):
            submit_scenario_decision_v2(
                self.content,
                persistence=self.persistence,
                submission_context=tampered_context,
                selected_option_id=option_id,
            )

    def test_original_validation_exception_is_available_through_cause(self):
        try:
            start_or_resume_scenario_run_v2(
                self.content,
                persistence=self.persistence,
                user_email=self._INVALID_EMAIL,
                scenario_version_id=_SCENARIO_VERSION_ID,
                attempt_id=self.attempt_id,
            )
            self.fail("expected ScenarioOrchestrationV2InvalidRequestError")
        except ScenarioOrchestrationV2InvalidRequestError as exc:
            self.assertIsInstance(exc.__cause__, ScenarioPersistenceValidationError)

    def test_valid_email_behavior_remains_unchanged(self):
        # Whitespace/casing normalization must still behave exactly as before
        # this correction -- only the *invalid*-email error type changed.
        result = start_or_resume_scenario_run_v2(
            self.content,
            persistence=self.persistence,
            user_email="  Learner@Example.COM  ",
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=self.attempt_id,
        )
        self.assertEqual(result.submission_context.user_email, _EMAIL)


class TestControlFlowExceptionsNotSwallowed(OrchestrationV2TestCase):
    """The error-mapping boundary catches ``Exception``, never
    ``BaseException`` -- ``KeyboardInterrupt``/``SystemExit`` and other
    control-flow signals raised by the injected persistence dependency must
    propagate unchanged, not be wrapped or swallowed."""

    def test_keyboard_interrupt_is_not_swallowed(self):
        def _raise_keyboard_interrupt(params):
            raise KeyboardInterrupt()

        self.persistence.call_start_or_resume_scenario_attempt_v1 = _raise_keyboard_interrupt  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            self._start()

    def test_system_exit_is_not_swallowed(self):
        def _raise_system_exit(params):
            raise SystemExit(1)

        self.persistence.call_start_or_resume_scenario_attempt_v1 = _raise_system_exit  # type: ignore[method-assign]
        with self.assertRaises(SystemExit):
            self._start()


class TestEngineV1Isolation(unittest.TestCase):
    def test_aj_engine_v1_tests_remain_importable(self):
        import tests.test_scenario_persistence as v1_persistence  # noqa: F401
        import tests.test_scenario_learner_controller as v1_controller  # noqa: F401


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@unittest.skipUnless(_docker_available(), "docker not available")
class TestScenarioOrchestrationV2DisposableSmoke(unittest.TestCase):
    CONTAINER = "certbound-v2-orchestration-smoke"
    HOST_PORT = 55432
    MIGRATIONS = (
        "20260718170000_v66_scenario_definition_persistence_foundation.sql",
        "20260719003000_v67_harden_scenario_definition_security.sql",
        "20260719130000_v68_scenario_attempt_persistence_foundation.sql",
        "20260719140000_v69_scenario_v2_attempt_identity_support.sql",
    )

    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(["docker", "rm", "-f", cls.CONTAINER], check=False, capture_output=True)
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                cls.CONTAINER,
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-p",
                f"{cls.HOST_PORT}:5432",
                "postgres:16",
            ],
            check=True,
            capture_output=True,
        )
        cls._wait_for_ready()
        cls._psql(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon NOLOGIN; END IF; "
            "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF; "
            "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'service_role') THEN CREATE ROLE service_role NOLOGIN; END IF; "
            "END $$;"
        )
        for migration in cls.MIGRATIONS:
            path = REPO_ROOT / "supabase" / "migrations" / migration
            cls._psql_file(path)
        cls._seed_scenario_fixture()

    @classmethod
    def _wait_for_ready(cls) -> None:
        import time

        deadline = time.time() + 30
        last_error: Optional[Exception] = None
        while time.time() < deadline:
            try:
                cls._docker_exec("pg_isready", "-U", "postgres")
                return
            except subprocess.CalledProcessError as exc:
                last_error = exc
                time.sleep(1)
        raise RuntimeError(f"disposable postgres container never became ready: {last_error}")

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(["docker", "rm", "-f", cls.CONTAINER], check=True, capture_output=True)

    @classmethod
    def _docker_exec(cls, *args: str, input: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "exec", "-i", cls.CONTAINER, *args],
            check=True,
            capture_output=True,
            text=True,
            input=input,
        )

    @classmethod
    def _psql(cls, sql: str) -> str:
        result = cls._docker_exec("psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", sql)
        return result.stdout

    @classmethod
    def _psql_scalar(cls, sql: str) -> str:
        result = cls._docker_exec(
            "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-t", "-A", "-c", sql
        )
        return result.stdout.strip()

    @classmethod
    def _psql_file(cls, path: Path) -> None:
        content = path.read_text(encoding="utf-8")
        cls._docker_exec("psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-", input=content)

    @classmethod
    def _seed_scenario_fixture(cls) -> None:
        document = _load_document()
        content = build_scenario_content_v2(document)
        sim_id = content.simulation_id.replace("'", "''")
        doc_json = json.dumps(document).replace("'", "''")
        hash_val = content.canonical_content_sha256
        cls._psql(
            f"""
            INSERT INTO public.scenarios (simulation_id, certification_exam_name, title)
            VALUES ('{sim_id}', 'Business Analyst', 'Orchestration Smoke Scenario')
            RETURNING id;
            """
        )
        cls._psql(
            f"""
            WITH s AS (
              SELECT id FROM public.scenarios WHERE simulation_id = '{sim_id}' LIMIT 1
            )
            INSERT INTO public.scenario_versions (scenario_id, version, schema_version, engine_version, source_repository_path)
            SELECT id, '{content.version}', '{content.schema_version}', '{ENGINE_VERSION}', 'tests/fixtures/scenario_engine_v2_vslice_1_1_0.json'
            FROM s
            RETURNING id;
            """
        )
        cls._psql(
            f"""
            WITH v AS (
              SELECT sv.id
              FROM public.scenario_versions sv
              JOIN public.scenarios s ON s.id = sv.scenario_id
              WHERE s.simulation_id = '{sim_id}'
              LIMIT 1
            )
            SELECT public.publish_scenario_version_v1(
              (SELECT id FROM v),
              '{doc_json}'::jsonb,
              '{hash_val}'
            );
            """
        )
        cls.scenario_version_id = cls._psql_scalar(
            f"""
            SELECT sv.id::text
            FROM public.scenario_versions sv
            JOIN public.scenarios s ON s.id = sv.scenario_id
            WHERE s.simulation_id = '{sim_id}' AND sv.lifecycle_status = 'published'
            LIMIT 1;
            """
        )
        cls.content = content

    def setUp(self) -> None:
        self.persistence = _PostgresOrchestrationPersistence(self.HOST_PORT, self.scenario_version_id)
        self.attempt_id = _new_attempt_id()
        self.email = "smoke-learner@example.com"

    def test_disposable_start_submit_resume_idempotency_and_conflict(self):
        start = start_or_resume_scenario_run_v2(
            self.content,
            persistence=self.persistence,
            user_email=self.email,
            scenario_version_id=self.scenario_version_id,
            attempt_id=self.attempt_id,
        )
        self.assertEqual(start.attempt_id, self.attempt_id)
        self.assertTrue(start.created)

        option = start.learner_view.scene_view.options[0].id if start.learner_view.scene_view else ""
        submitted = submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=option,
        )
        self.assertEqual(len(self.persistence.loaded_decisions(self.attempt_id)), 1)

        resumed, _ = resume_and_replay_scenario_run_v2(
            self.content,
            persistence=self.persistence,
            user_email=self.email,
            attempt_id=self.attempt_id,
        )
        self.assertEqual(resumed.expected_sequence_number, submitted.run.expected_sequence_number)

        retry = submit_scenario_decision_v2(
            self.content,
            persistence=self.persistence,
            submission_context=start.submission_context,
            selected_option_id=option,
            idempotency_key=submitted.idempotency_key,
        )
        self.assertTrue(retry.idempotent_replay)
        self.assertEqual(len(self.persistence.loaded_decisions(self.attempt_id)), 1)

        with self.assertRaises(ScenarioOrchestrationV2SequenceConflictError):
            submit_scenario_decision_v2(
                self.content,
                persistence=self.persistence,
                submission_context=start.submission_context,
                selected_option_id=option,
            )


class _PostgresOrchestrationPersistence:
    """Minimal psycopg2-backed port for disposable Docker smoke only."""

    def __init__(self, host_port: int, scenario_version_id: str) -> None:
        self.host_port = host_port
        self.scenario_version_id = scenario_version_id.strip()

    def _rpc(self, name: str, params: Mapping[str, Any]) -> Any:
        import datetime
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(host="127.0.0.1", port=self.host_port, user="postgres", dbname="postgres")
        conn.autocommit = True
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                placeholders = ", ".join(f"{key} := %({key})s" for key in params)
                cur.execute(f"SELECT * FROM public.{name}({placeholders})", dict(params))
                rows = cur.fetchall()
                # Supabase's real client returns JSON over HTTP (PostgREST), so
                # timestamptz/date columns always arrive as ISO-8601 strings, never
                # native datetime objects. Round-trip through JSON here to
                # faithfully reproduce that wire shape against this raw psycopg2
                # connection.
                return json.loads(json.dumps([dict(row) for row in rows], default=_json_default))
        finally:
            conn.close()

    def call_start_or_resume_scenario_attempt_v1(self, params: Mapping[str, Any]) -> Any:
        import psycopg2.extras

        payload = dict(params)
        payload["p_initial_serialized_state"] = psycopg2.extras.Json(payload["p_initial_serialized_state"])
        return self._rpc("start_or_resume_scenario_attempt_v1", payload)

    def call_submit_scenario_decision_v1(self, params: Mapping[str, Any]) -> Any:
        import psycopg2.extras

        payload = dict(params)
        for key in ("p_state_before", "p_state_after", "p_terminal_result_snapshot"):
            if payload.get(key) is not None:
                payload[key] = psycopg2.extras.Json(payload[key])
        return self._rpc("submit_scenario_decision_v1", payload)

    def load_attempt_snapshot(self, *, user_email: str, attempt_id: str) -> Mapping[str, Any]:
        rows = self._rpc(
            "get_scenario_attempt_v1",
            {"p_user_email": user_email.strip().lower(), "p_attempt_id": attempt_id},
        )
        if not rows:
            raise _FakeException("attempt_not_found: missing")
        return rows[0]

    def loaded_decisions(self, attempt_id: str) -> List[Dict[str, Any]]:
        row = self.load_attempt_snapshot(user_email="smoke-learner@example.com", attempt_id=attempt_id)
        return list(row.get("decisions") or [])


if __name__ == "__main__":
    unittest.main()
