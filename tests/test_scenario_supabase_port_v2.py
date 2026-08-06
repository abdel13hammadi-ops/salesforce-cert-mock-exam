"""Focused tests for the Supabase-client-backed V2 persistence port.

Uses deterministic fake Supabase/PostgREST client doubles only (A-AH).
``TestPortSatisfiesOrchestrationProtocolEndToEnd`` additionally drives the
REAL public orchestration API (``utils.scenario_orchestration_v2``) through
this port to prove genuine ``ScenarioOrchestrationV2PersistencePort``
protocol compatibility, reusing the already-validated CAS/idempotency
business logic from ``tests.test_scenario_orchestration_v2.
FakeOrchestrationPersistence`` behind a raw ``.rpc(...).execute()`` surface
shaped like a real Supabase client.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_engine_v2 import ENGINE_VERSION, build_scenario_content_v2
from utils.scenario_orchestration_v2 import (
    ScenarioOrchestrationV2SequenceConflictError,
    _RPC_ERROR_PREFIX_MAP,
    resume_and_replay_scenario_run_v2,
    start_or_resume_scenario_run_v2,
    submit_scenario_decision_v2,
)
from utils.scenario_supabase_port_v2 import (
    ScenarioSupabasePortV2AuthenticationError,
    ScenarioSupabasePortV2Error,
    ScenarioSupabasePortV2MalformedResponseError,
    ScenarioSupabasePortV2MultipleAttemptRowsError,
    ScenarioSupabasePortV2NoAttemptRowError,
    ScenarioSupabasePortV2PermissionError,
    ScenarioSupabasePortV2RpcError,
    ScenarioSupabasePortV2TransportError,
    ScenarioSupabasePortV2UnknownError,
    SupabaseScenarioOrchestrationV2Port,
    _APPROVED_BUSINESS_ERROR_PREFIXES,
    _classify_rpc_exception,
)
from tests.test_scenario_orchestration_v2 import (
    _EMAIL,
    _SCENARIO_VERSION_ID,
    FIXTURE_PATH,
    FakeOrchestrationPersistence,
    _load_document,
    _new_attempt_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_START_RPC = "start_or_resume_scenario_attempt_v1"
_SUBMIT_RPC = "submit_scenario_decision_v1"
_GET_ATTEMPT_RPC = "get_scenario_attempt_v1"

_START_PARAMS: Dict[str, Any] = {
    "p_user_email": "learner@example.com",
    "p_scenario_version_id": "33333333-3333-4333-8333-333333333333",
    "p_initial_current_scene_id": "SC001",
    "p_initial_serialized_state": {"simulationId": "sim-1", "state": {"a": 1}},
    "p_engine_version": "SCENARIO_ENGINE_V2",
    "p_scenario_content_sha256": "0" * 64,
    "p_attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
}

_SUBMIT_PARAMS: Dict[str, Any] = {
    "p_user_email": "learner@example.com",
    "p_attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "p_idempotency_key": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "p_expected_sequence_number": 1,
    "p_expected_scene_id": "SC001",
    "p_selected_option_id": "opt-a",
    "p_request_fingerprint": "1" * 64,
    "p_state_before": {"state": {"a": 1}},
    "p_state_after": {"state": {"a": 2}},
    "p_is_terminal": False,
    "p_resulting_scene_id": "SC002",
    "p_terminal_ending_id": None,
    "p_terminal_result_snapshot": None,
}


# ---------------------------------------------------------------------------
# Fake Supabase/PostgREST client infrastructure
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: Any = None, error: Any = None) -> None:
        self.data = data
        self.error = error


class _FakeRpcRequest:
    def __init__(self, response: Optional[_FakeResponse] = None, exception: Optional[BaseException] = None) -> None:
        self._response = response
        self._exception = exception

    def execute(self) -> _FakeResponse:
        if self._exception is not None:
            raise self._exception
        assert self._response is not None
        return self._response


class _FakeSupabaseClient:
    """Deterministic double shaped like ``client.rpc(name, params).execute()``.

    Responses/exceptions are queued per RPC name (FIFO); every call is
    recorded verbatim (as the exact object the port passed in) for
    fidelity/immutability assertions.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._queues: Dict[str, List[_FakeRpcRequest]] = {}

    def queue_response(self, fn: str, *, data: Any = None, error: Any = None) -> None:
        self._queues.setdefault(fn, []).append(_FakeRpcRequest(response=_FakeResponse(data=data, error=error)))

    def queue_exception(self, fn: str, exc: BaseException) -> None:
        self._queues.setdefault(fn, []).append(_FakeRpcRequest(exception=exc))

    def rpc(self, fn: str, params: Mapping[str, Any]) -> _FakeRpcRequest:
        self.calls.append((fn, params))  # not copied -- identity matters for immutability tests
        queue = self._queues.get(fn)
        if not queue:
            raise AssertionError(f"_FakeSupabaseClient: no queued response for rpc {fn!r} (no auto-retry allowed)")
        return queue.pop(0)


class _CodedException(Exception):
    """A raised exception carrying a PostgREST-style ``.code``/``.message``."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor(unittest.TestCase):
    def test_none_client_rejected(self):
        with self.assertRaises(ValueError):
            SupabaseScenarioOrchestrationV2Port(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A-F: start RPC
# ---------------------------------------------------------------------------


class TestStartRpc(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeSupabaseClient()
        self.port = SupabaseScenarioOrchestrationV2Port(self.client)

    def test_a_method_name_is_exact(self):
        self.client.queue_response(_START_RPC, data=[{"attempt_id": "x"}])
        self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertEqual(self.client.calls[0][0], "start_or_resume_scenario_attempt_v1")

    def test_b_receives_exactly_seven_keys(self):
        self.client.queue_response(_START_RPC, data=[{"attempt_id": "x"}])
        self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        sent = self.client.calls[0][1]
        self.assertEqual(len(sent), 7)
        self.assertEqual(set(sent.keys()), set(_START_PARAMS.keys()))

    def test_c_params_not_mutated(self):
        original = copy.deepcopy(_START_PARAMS)
        self.client.queue_response(_START_RPC, data=[{"attempt_id": "x"}])
        self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertEqual(_START_PARAMS, original)
        # The port must forward an independent copy, not the caller's own object.
        self.assertIsNot(self.client.calls[0][1], _START_PARAMS)
        self.assertIsNot(self.client.calls[0][1]["p_initial_serialized_state"], _START_PARAMS["p_initial_serialized_state"])

    def test_d_list_response_normalized(self):
        row = {"attempt_id": "x", "created": True}
        self.client.queue_response(_START_RPC, data=[row])
        result = self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertEqual(result, [row])
        self.assertIsNot(result, self.client._queues)  # sanity: not the internal queue object

    def test_e_empty_response_preserved_for_adapter_rejection(self):
        self.client.queue_response(_START_RPC, data=[])
        result = self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertEqual(result, [])

    def test_f_multi_row_response_preserved_for_adapter_rejection(self):
        rows = [{"attempt_id": "x"}, {"attempt_id": "y"}]
        self.client.queue_response(_START_RPC, data=rows)
        result = self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertEqual(result, rows)
        self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# G-J: submit RPC
# ---------------------------------------------------------------------------


class TestSubmitRpc(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeSupabaseClient()
        self.port = SupabaseScenarioOrchestrationV2Port(self.client)

    def test_g_method_name_is_exact(self):
        self.client.queue_response(_SUBMIT_RPC, data=[{"decision_id": "d"}])
        self.port.call_submit_scenario_decision_v1(_SUBMIT_PARAMS)
        self.assertEqual(self.client.calls[0][0], "submit_scenario_decision_v1")

    def test_h_receives_exactly_thirteen_keys(self):
        self.client.queue_response(_SUBMIT_RPC, data=[{"decision_id": "d"}])
        self.port.call_submit_scenario_decision_v1(_SUBMIT_PARAMS)
        sent = self.client.calls[0][1]
        self.assertEqual(len(sent), 13)
        self.assertEqual(set(sent.keys()), set(_SUBMIT_PARAMS.keys()))

    def test_i_params_not_mutated(self):
        original = copy.deepcopy(_SUBMIT_PARAMS)
        self.client.queue_response(_SUBMIT_RPC, data=[{"decision_id": "d"}])
        self.port.call_submit_scenario_decision_v1(_SUBMIT_PARAMS)
        self.assertEqual(_SUBMIT_PARAMS, original)
        self.assertIsNot(self.client.calls[0][1], _SUBMIT_PARAMS)
        self.assertIsNot(self.client.calls[0][1]["p_state_before"], _SUBMIT_PARAMS["p_state_before"])

    def test_j_mapping_response_normalized(self):
        # Realistic single-row Supabase responses can arrive as either a
        # one-item list or (less commonly) a bare mapping -- the port must
        # not assume/enforce one particular shape (that is the downstream
        # adapter parser's job).
        row = {"decision_id": "d", "sequence_number": 1}
        self.client.queue_response(_SUBMIT_RPC, data=row)
        result = self.port.call_submit_scenario_decision_v1(_SUBMIT_PARAMS)
        self.assertEqual(result, row)
        self.assertIsNot(result, row)


# ---------------------------------------------------------------------------
# K-P: error classification
# ---------------------------------------------------------------------------


class TestErrorClassification(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeSupabaseClient()
        self.port = SupabaseScenarioOrchestrationV2Port(self.client)

    def test_k_known_error_prefix_preserved_verbatim_raised_exception(self):
        self.client.queue_exception(_START_RPC, Exception("sequence_mismatch: expected 3, got 2"))
        with self.assertRaises(ScenarioSupabasePortV2RpcError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertEqual(str(ctx.exception), "sequence_mismatch: expected 3, got 2")

    def test_k_known_error_prefix_preserved_verbatim_error_field(self):
        self.client.queue_response(_SUBMIT_RPC, error={"message": "attempt_not_found: no such attempt"})
        with self.assertRaises(ScenarioSupabasePortV2RpcError) as ctx:
            self.port.call_submit_scenario_decision_v1(_SUBMIT_PARAMS)
        self.assertEqual(str(ctx.exception), "attempt_not_found: no such attempt")

    def test_l_unknown_error_becomes_typed_port_error(self):
        self.client.queue_exception(_START_RPC, Exception("something completely unrecognized happened"))
        with self.assertRaises(ScenarioSupabasePortV2Error):
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)

    def test_m_raw_supabase_exception_does_not_escape(self):
        class _FakePostgrestApiError(Exception):
            pass

        original = _FakePostgrestApiError("raw sdk internals should never escape")
        self.client.queue_exception(_START_RPC, original)
        with self.assertRaises(ScenarioSupabasePortV2Error) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertNotIsInstance(ctx.exception, _FakePostgrestApiError)
        self.assertIs(ctx.exception.__cause__, original)

    def test_n_timeout_becomes_transport_error(self):
        self.client.queue_exception(_START_RPC, _CodedException("Request timed out after 30s"))
        with self.assertRaises(ScenarioSupabasePortV2TransportError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertNotIn("30s", str(ctx.exception))

    def test_o_permission_denied_becomes_permission_error(self):
        self.client.queue_exception(_START_RPC, _CodedException("permission denied for function foo", code="42501"))
        with self.assertRaises(ScenarioSupabasePortV2PermissionError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertNotIn("for function foo", str(ctx.exception))

    def test_p_authentication_failure_becomes_authentication_error(self):
        self.client.queue_exception(_START_RPC, _CodedException("JWT expired for user role"))
        with self.assertRaises(ScenarioSupabasePortV2AuthenticationError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertNotIn("user role", str(ctx.exception))

    def test_control_flow_exceptions_not_swallowed(self):
        self.client.queue_exception(_START_RPC, KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)

    def test_system_exit_not_swallowed(self):
        self.client.queue_exception(_START_RPC, SystemExit(1))
        with self.assertRaises(SystemExit):
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)


# ---------------------------------------------------------------------------
# Q-X: load_attempt_snapshot
# ---------------------------------------------------------------------------


def _full_attempt_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "scenario_id": "22222222-2222-4222-8222-222222222222",
        "scenario_version_id": "33333333-3333-4333-8333-333333333333",
        "status": "in_progress",
        "current_scene_id": "SC001",
        "next_sequence_number": 2,
        "serialized_engine_state": {"state": {"a": 1}},
        "engine_version": "SCENARIO_ENGINE_V2",
        "scenario_content_sha256": "0" * 64,
        # Deliberately unapproved/sensitive-looking extra columns:
        "user_email": "secret-owner@example.com",
        "started_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:01:00Z",
        "completed_at": None,
        "abandoned_at": None,
        "terminal_ending_id": None,
        "terminal_result_snapshot": None,
        "decisions": [
            {
                "sequenceNumber": 1,
                "expectedSceneId": "SC001",
                "selectedOptionId": "opt-a",
                "stateBefore": {"state": {"a": 0}},
                "stateAfter": {"state": {"a": 1}},
                "resultingSceneId": "SC001",
                "isTerminal": False,
                "terminalEndingId": None,
                "createdAt": "2026-08-01T00:00:30Z",
                "idempotencyKey": "should-never-be-exposed",
            }
        ],
    }
    row.update(overrides)
    return row


class TestLoadAttemptSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeSupabaseClient()
        self.port = SupabaseScenarioOrchestrationV2Port(self.client)

    def test_q_filters_by_trusted_attempt_id(self):
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[_full_attempt_row()])
        self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        fn, params = self.client.calls[0]
        self.assertEqual(fn, "get_scenario_attempt_v1")
        self.assertEqual(params["p_attempt_id"], "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.assertEqual(params["p_user_email"], "learner@example.com")

    def test_r_requests_only_approved_attempt_columns(self):
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[_full_attempt_row()])
        snapshot = self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")
        approved = {
            "attempt_id",
            "scenario_id",
            "scenario_version_id",
            "status",
            "current_scene_id",
            "next_sequence_number",
            "serialized_engine_state",
            "engine_version",
            "scenario_content_sha256",
            "decisions",
        }
        self.assertEqual(set(snapshot.keys()), approved)
        self.assertNotIn("user_email", snapshot)
        self.assertNotIn("started_at", snapshot)
        self.assertNotIn("terminal_result_snapshot", snapshot)

    def test_s_zero_attempt_rows_rejected(self):
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[])
        with self.assertRaises(ScenarioSupabasePortV2NoAttemptRowError):
            self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")

    def test_t_multiple_attempt_rows_rejected(self):
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[_full_attempt_row(), _full_attempt_row()])
        with self.assertRaises(ScenarioSupabasePortV2MultipleAttemptRowsError):
            self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")

    def test_malformed_response_shape_rejected(self):
        self.client.queue_response(_GET_ATTEMPT_RPC, data="not-a-list-or-mapping")
        with self.assertRaises(ScenarioSupabasePortV2MalformedResponseError):
            self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")

    def test_u_decision_query_filters_by_trusted_attempt_id_via_same_call(self):
        # Decisions are bundled inside the single get_scenario_attempt_v1
        # call keyed by p_attempt_id -- there is no separate decision query
        # to filter independently.
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[_full_attempt_row()])
        snapshot = self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(snapshot["decisions"]), 1)

    def test_v_decisions_preserve_ascending_order(self):
        row = _full_attempt_row(
            decisions=[
                {"sequenceNumber": 1, "expectedSceneId": "SC001", "selectedOptionId": "opt-a"},
                {"sequenceNumber": 2, "expectedSceneId": "SC002", "selectedOptionId": "opt-b"},
                {"sequenceNumber": 3, "expectedSceneId": "SC003", "selectedOptionId": "opt-c"},
            ]
        )
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[row])
        snapshot = self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")
        self.assertEqual([d["sequenceNumber"] for d in snapshot["decisions"]], [1, 2, 3])

    def test_w_decision_rows_expose_only_approved_columns(self):
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[_full_attempt_row()])
        snapshot = self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")
        decision = snapshot["decisions"][0]
        self.assertEqual(set(decision.keys()), {"sequenceNumber", "expectedSceneId", "selectedOptionId"})
        self.assertNotIn("idempotencyKey", decision)
        self.assertNotIn("stateBefore", decision)
        self.assertNotIn("stateAfter", decision)

    def test_x_empty_decision_list_accepted_for_new_attempt(self):
        row = _full_attempt_row(decisions=[])
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[row])
        snapshot = self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")
        self.assertEqual(snapshot["decisions"], [])


# ---------------------------------------------------------------------------
# Y-AC: alias isolation
# ---------------------------------------------------------------------------


class TestAliasIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeSupabaseClient()
        self.port = SupabaseScenarioOrchestrationV2Port(self.client)

    def test_y_nested_serialized_state_alias_isolation(self):
        nested_state = {"state": {"counter": 1}, "flags": {"seen": False}}
        row = _full_attempt_row(serialized_engine_state=nested_state)
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[row])
        snapshot = self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")

        nested_state["state"]["counter"] = 999  # mutate original after the call
        self.assertEqual(snapshot["serialized_engine_state"]["state"]["counter"], 1)

        snapshot["serialized_engine_state"]["state"]["counter"] = -1  # mutate the copy
        self.assertEqual(nested_state["state"]["counter"], 999)

    def test_z_nested_terminal_result_alias_isolation_on_submit_response(self):
        terminal_snapshot = {"endingId": "end-a", "displayScore": 100, "nested": {"tier": "gold"}}
        response_row = {
            "decision_id": "d",
            "terminal_result_snapshot": terminal_snapshot,
        }
        self.client.queue_response(_SUBMIT_RPC, data=[response_row])
        result = self.port.call_submit_scenario_decision_v1(_SUBMIT_PARAMS)

        terminal_snapshot["nested"]["tier"] = "mutated-after-return"
        self.assertEqual(result[0]["terminal_result_snapshot"]["nested"]["tier"], "gold")

        result[0]["terminal_result_snapshot"]["nested"]["tier"] = "mutated-on-copy"
        self.assertEqual(terminal_snapshot["nested"]["tier"], "mutated-after-return")

    def test_aa_decision_row_alias_isolation(self):
        decisions = [{"sequenceNumber": 1, "expectedSceneId": "SC001", "selectedOptionId": "opt-a"}]
        row = _full_attempt_row(decisions=decisions)
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[row])
        snapshot = self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")

        decisions[0]["selectedOptionId"] = "mutated-after-return"
        self.assertEqual(snapshot["decisions"][0]["selectedOptionId"], "opt-a")

        snapshot["decisions"][0]["selectedOptionId"] = "mutated-on-copy"
        self.assertEqual(decisions[0]["selectedOptionId"], "mutated-after-return")

    def test_ab_sdk_response_mutation_after_return_cannot_alter_port_output(self):
        row = {"attempt_id": "x", "serialized_engine_state": {"state": {"a": 1}}}
        self.client.queue_response(_START_RPC, data=[row])
        result = self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)

        row["serialized_engine_state"]["state"]["a"] = 999
        self.assertEqual(result[0]["serialized_engine_state"]["state"]["a"], 1)

    def test_ac_port_output_mutation_cannot_alter_sdk_response(self):
        row = {"attempt_id": "x", "serialized_engine_state": {"state": {"a": 1}}}
        self.client.queue_response(_START_RPC, data=[row])
        result = self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)

        result[0]["serialized_engine_state"]["state"]["a"] = -1
        self.assertEqual(row["serialized_engine_state"]["state"]["a"], 1)


# ---------------------------------------------------------------------------
# AD-AF: dependency-boundary hygiene
# ---------------------------------------------------------------------------


class TestDependencyBoundaryHygiene(unittest.TestCase):
    def test_ad_module_never_reads_environment_variables(self):
        source = Path(
            __import__("utils.scenario_supabase_port_v2", fromlist=["_"]).__file__
        ).read_text(encoding="utf-8")
        self.assertNotIn("import os", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("os.getenv", source)

    def test_ae_no_global_client_two_instances_stay_independent(self):
        client_one = _FakeSupabaseClient()
        client_two = _FakeSupabaseClient()
        port_one = SupabaseScenarioOrchestrationV2Port(client_one)
        port_two = SupabaseScenarioOrchestrationV2Port(client_two)

        client_one.queue_response(_START_RPC, data=[{"attempt_id": "one"}])
        client_two.queue_response(_START_RPC, data=[{"attempt_id": "two"}])
        port_one.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        port_two.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)

        self.assertEqual(len(client_one.calls), 1)
        self.assertEqual(len(client_two.calls), 1)
        self.assertIsNot(port_one._client, port_two._client)  # noqa: SLF001

    def test_af_no_automatic_retry_on_failure(self):
        client = _FakeSupabaseClient()
        port = SupabaseScenarioOrchestrationV2Port(client)
        client.queue_exception(_START_RPC, Exception("sequence_mismatch: boom"))
        with self.assertRaises(ScenarioSupabasePortV2Error):
            port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertEqual(len(client.calls), 1)


# ---------------------------------------------------------------------------
# AG: Engine V1 isolation
# ---------------------------------------------------------------------------


class TestEngineV1Isolation(unittest.TestCase):
    def test_ag_v1_modules_do_not_reference_the_new_port(self):
        repo_root = Path(__file__).resolve().parents[1]
        checked = 0
        for relative in ("utils/scenario_persistence.py", "utils/scenario_learner_controller.py"):
            path = repo_root / relative
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("scenario_supabase_port_v2", text)
            checked += 1
        if checked == 0:
            self.skipTest("Engine V1 modules excluded from narrow production candidate")

    def test_ag_v1_tests_remain_importable(self):
        import importlib.util

        if importlib.util.find_spec("tests.test_scenario_persistence") is None:
            self.skipTest("Engine V1 persistence tests excluded from narrow production candidate")
        if importlib.util.find_spec("tests.test_scenario_learner_controller") is None:
            self.skipTest("Engine V1 learner-controller tests excluded from narrow production candidate")

        import tests.test_scenario_persistence  # noqa: F401
        import tests.test_scenario_learner_controller  # noqa: F401


# ---------------------------------------------------------------------------
# AH: error-message safety
# ---------------------------------------------------------------------------


class TestErrorMessageSafety(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeSupabaseClient()
        self.port = SupabaseScenarioOrchestrationV2Port(self.client)

    def test_ah_transport_error_scrubs_raw_detail(self):
        secret = "postgres://svc_role:s3cr3tPassw0rd@internal-db-host:5432/postgres"
        self.client.queue_exception(_START_RPC, _CodedException(f"Connection refused: {secret}"))
        with self.assertRaises(ScenarioSupabasePortV2TransportError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn("s3cr3tPassw0rd", str(ctx.exception))

    def test_ah_authentication_error_scrubs_raw_token(self):
        secret_token = "eyJhbGciOiJIUzI1NiJ9.super-secret-jwt-payload.signature"
        self.client.queue_exception(_START_RPC, _CodedException(f"invalid JWT: {secret_token}"))
        with self.assertRaises(ScenarioSupabasePortV2AuthenticationError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertNotIn(secret_token, str(ctx.exception))

    def test_ah_permission_error_scrubs_raw_detail(self):
        self.client.queue_exception(
            _START_RPC, _CodedException("permission denied for relation scenario_attempts", code="42501")
        )
        with self.assertRaises(ScenarioSupabasePortV2PermissionError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertNotIn("scenario_attempts", str(ctx.exception))

    def test_ah_business_error_verbatim_preservation_contains_no_secret_by_construction(self):
        # Business-error text originates exclusively from this repository's
        # own committed migration SQL RAISE EXCEPTION strings -- never from
        # caller-controlled or credential-bearing content -- so verbatim
        # preservation here is intentional and safe.
        self.client.queue_exception(_START_RPC, Exception("attempt_id_conflict: attempt already exists"))
        with self.assertRaises(ScenarioSupabasePortV2RpcError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertEqual(str(ctx.exception), "attempt_id_conflict: attempt already exists")


# ---------------------------------------------------------------------------
# HIGH-01 correction: fail-closed business-error allowlist
#
# Prior to this correction, `_classify_rpc_exception`'s fallback treated ANY
# message that did not match a timeout/connection/permission/authentication
# BLOCKLIST marker as a "safe" business error and preserved it verbatim --
# empirically shown (see SCENARIO_ENGINE_V2_SUPABASE_PORT_FOCUSED_REVIEW.md,
# finding HIGH-01, reproduced against a REAL disposable PostgREST server) to
# leak schema/function/relation names for a genuine, realistic PostgREST
# `PGRST202` "function not found in schema cache" error. This section proves
# the corrected fail-closed ALLOWLIST design: verbatim preservation now
# requires a positive, exact, case-sensitive prefix match against a closed
# set; everything else -- however it is shaped -- is sanitized.
# ---------------------------------------------------------------------------


class TestApprovedPrefixSetSync(unittest.TestCase):
    """Structurally proves the port's own hardcoded allowlist copy can never
    silently drift from `utils.scenario_orchestration_v2._RPC_ERROR_PREFIX_MAP`
    -- the two modules remain deliberately uncoupled at import time (neither
    imports the other in production code), so this cross-check exists only
    here, in the test suite, as a standing regression guard."""

    def test_prefix_sets_match_exactly(self):
        orchestration_prefixes = {prefix for prefix, _exc_cls in _RPC_ERROR_PREFIX_MAP}
        port_prefixes = set(_APPROVED_BUSINESS_ERROR_PREFIXES)
        self.assertEqual(port_prefixes, orchestration_prefixes)

    def test_no_duplicate_prefixes_in_port_allowlist(self):
        self.assertEqual(len(_APPROVED_BUSINESS_ERROR_PREFIXES), len(set(_APPROVED_BUSINESS_ERROR_PREFIXES)))


class TestFailClosedBusinessErrorAllowlist(unittest.TestCase):
    """Requirement checklist items 1-16, 21-22 (see task SIM-PERSIST-V2-06B)."""

    def setUp(self) -> None:
        self.client = _FakeSupabaseClient()
        self.port = SupabaseScenarioOrchestrationV2Port(self.client)

    # -- 1-2: known approved prefixes remain verbatim on both RPCs ---------

    def test_1_known_approved_start_rpc_prefix_preserved(self):
        self.client.queue_exception(
            _START_RPC, Exception("scenario_version_not_found: scenario_versions abc does not exist")
        )
        with self.assertRaises(ScenarioSupabasePortV2RpcError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertEqual(str(ctx.exception), "scenario_version_not_found: scenario_versions abc does not exist")

    def test_2_known_approved_submit_rpc_prefix_preserved(self):
        self.client.queue_exception(
            _SUBMIT_RPC, Exception("idempotency_key_conflict: idempotency_key x was already used")
        )
        with self.assertRaises(ScenarioSupabasePortV2RpcError) as ctx:
            self.port.call_submit_scenario_decision_v1(_SUBMIT_PARAMS)
        self.assertEqual(str(ctx.exception), "idempotency_key_conflict: idempotency_key x was already used")

    # -- 3: every approved prefix, individually -----------------------------

    def test_3_every_approved_business_prefix_preserved_verbatim(self):
        for prefix in _APPROVED_BUSINESS_ERROR_PREFIXES:
            with self.subTest(prefix=prefix):
                message = f"{prefix} synthetic detail for {prefix!r}"
                classified = _classify_rpc_exception("some_rpc", Exception(message))
                self.assertIsInstance(classified, ScenarioSupabasePortV2RpcError)
                self.assertEqual(str(classified), message)

    # -- 4: near-match prefixes are rejected --------------------------------

    def test_4_near_match_prefix_wrong_case_sanitized(self):
        classified = _classify_rpc_exception("rpc", Exception("Sequence_Mismatch: expected 1 got 2"))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn("Sequence_Mismatch", str(classified))

    def test_4_near_match_prefix_extra_character_sanitized(self):
        classified = _classify_rpc_exception("rpc", Exception("sequence_mismatchx: expected 1 got 2"))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)

    def test_4_near_match_prefix_missing_underscore_sanitized(self):
        classified = _classify_rpc_exception("rpc", Exception("sequencemismatch: expected 1 got 2"))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)

    def test_4_prefix_embedded_mid_message_not_accepted(self):
        # The real prefix text appears in the message, but NOT at position 0
        # -- must not be accepted as a startswith match.
        classified = _classify_rpc_exception(
            "rpc", Exception("unexpected wrapper around sequence_mismatch: expected 1 got 2")
        )
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn("sequence_mismatch", str(classified))

    # -- 5: a plausible-but-unapproved prefix is sanitized ------------------

    def test_5_unknown_prefix_sanitized(self):
        classified = _classify_rpc_exception("rpc", Exception("totally_unrecognized_business_error: details here"))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn("totally_unrecognized_business_error", str(classified))
        self.assertNotIn("details here", str(classified))

    # -- 6-7: the real HIGH-01 reproduction case -----------------------------

    def test_6_pgrst202_function_not_found_sanitized(self):
        classified = _classify_rpc_exception(
            "start_or_resume_scenario_attempt_v1",
            _CodedException(
                "Could not find the function public.definitely_does_not_exist_xyz_v1(p_foo) in the schema cache",
                code="PGRST202",
            ),
        )
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn("definitely_does_not_exist_xyz_v1", str(classified))
        self.assertNotIn("schema cache", str(classified))
        self.assertNotIn("public.", str(classified))

    def test_7_pgrst_schema_cache_text_sanitized_without_code(self):
        # Same shape, but without a recognizable `.code` at all -- message
        # content alone must not be enough to leak schema/relation detail.
        classified = _classify_rpc_exception(
            "rpc", Exception("Could not find the table public.scenario_attempts in the schema cache")
        )
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn("scenario_attempts", str(classified))
        self.assertNotIn("schema cache", str(classified))

    def test_pgrst202_via_error_attribute_response_sanitized(self):
        self.client.queue_response(
            _START_RPC,
            error={
                "message": "Could not find the function public.start_or_resume_scenario_attempt_v1 in the schema cache",
                "code": "PGRST202",
                "hint": None,
                "details": "Searched for the function public.start_or_resume_scenario_attempt_v1 ...",
            },
        )
        with self.assertRaises(ScenarioSupabasePortV2UnknownError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        # The RPC name itself is intentionally still present (it is a
        # known, public, non-secret identifier -- see the permission/auth/
        # timeout sanitized-message tests, which follow the same
        # convention). What must NOT leak is anything from the raw
        # `.message`/`.details` PostgREST error content.
        self.assertNotIn("schema cache", str(ctx.exception))
        self.assertNotIn("Searched for the function", str(ctx.exception))

    # -- 8: SQL-bearing unrecognized error -----------------------------------

    def test_8_sql_bearing_error_sanitized(self):
        sql = "SELECT * FROM scenario_attempts WHERE user_email = 'victim@example.com'"
        classified = _classify_rpc_exception("rpc", _CodedException(f"unexpected failure while executing: {sql}"))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn(sql, str(classified))
        self.assertNotIn("victim@example.com", str(classified))

    # -- 9: stack-trace-shaped message ---------------------------------------

    def test_9_stack_trace_shaped_message_sanitized(self):
        stack = (
            'Traceback (most recent call last):\n  File "server.py", line 42, in handler\n'
            '    raise RuntimeError("boom")\nRuntimeError: boom'
        )
        classified = _classify_rpc_exception("rpc", Exception(stack))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn("Traceback", str(classified))
        self.assertNotIn("server.py", str(classified))

    # -- 10: database URL (unrecognized-shape carrier) -----------------------

    def test_10_database_url_sanitized(self):
        secret_url = "postgres://svc_role:s3cr3tPassw0rd@internal-db-host.example.net:5432/postgres"
        classified = _classify_rpc_exception("rpc", _CodedException(f"unexpected failure near {secret_url}"))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn(secret_url, str(classified))
        self.assertNotIn("s3cr3tPassw0rd", str(classified))

    # -- 11: JWT-like string (routed to the generic bucket, not the auth
    #        marker path, because it avoids every recognized auth marker) --

    def test_11_jwt_like_string_sanitized(self):
        jwt_like = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.abc123signature"
        classified = _classify_rpc_exception("rpc", _CodedException(f"unexpected failure, payload={jwt_like}"))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn(jwt_like, str(classified))

    # -- 12: service-role token-like string ----------------------------------

    def test_12_service_role_token_like_string_sanitized(self):
        token_like = "sb_service_role_key_1234567890abcdefABCDEF"
        classified = _classify_rpc_exception("rpc", _CodedException(f"unexpected internal failure token={token_like}"))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn(token_like, str(classified))

    # -- 13: host and port ----------------------------------------------------

    def test_13_host_and_port_sanitized(self):
        classified = _classify_rpc_exception("rpc", _CodedException("unexpected internal failure at 10.0.4.17:5432"))
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn("10.0.4.17:5432", str(classified))

    # -- 14: relation/table/function names (distinct from 6/7) --------------

    def test_14_relation_table_function_names_sanitized(self):
        classified = _classify_rpc_exception(
            "rpc",
            _CodedException(
                "unexpected constraint violation on public.scenario_decisions.idempotency_key_fn_v1"
            ),
        )
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertNotIn("scenario_decisions", str(classified))
        self.assertNotIn("idempotency_key_fn_v1", str(classified))

    # -- 15: empty message ----------------------------------------------------

    def test_15_empty_message_sanitized(self):
        class _EmptyException(Exception):
            def __str__(self) -> str:
                return ""

        classified = _classify_rpc_exception("rpc", _EmptyException())
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)
        self.assertEqual(str(classified), "persistence_error: RPC 'rpc' failed unexpectedly.")

    # -- 16: non-string error content -----------------------------------------

    def test_16_non_string_error_attribute_content_sanitized(self):
        self.client.queue_response(_START_RPC, error=12345)
        with self.assertRaises(ScenarioSupabasePortV2UnknownError):
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)

    def test_16_non_string_exception_message_attribute_sanitized(self):
        class _WeirdException(Exception):
            def __init__(self) -> None:
                super().__init__()
                self.message = {"not": "a string"}
                self.code = None

        classified = _classify_rpc_exception("rpc", _WeirdException())
        self.assertIsInstance(classified, ScenarioSupabasePortV2UnknownError)

    # -- 21: classification precedence is deterministic ----------------------

    def test_21_business_prefix_wins_even_if_message_also_contains_auth_marker(self):
        # A synthetic (not real-world) message engineered to also contain an
        # authentication marker string ("jwt") AFTER a genuine approved
        # prefix -- the approved-prefix check must win because it is
        # checked first, proving precedence is exactly what the module's
        # own documented classification order states.
        classified = _classify_rpc_exception(
            "rpc", Exception("attempt_not_found: no scenario_attempts row jwt-labelled-column is owned")
        )
        self.assertIsInstance(classified, ScenarioSupabasePortV2RpcError)
        self.assertEqual(
            str(classified), "attempt_not_found: no scenario_attempts row jwt-labelled-column is owned"
        )

    def test_21_business_prefix_wins_even_if_message_also_contains_permission_marker(self):
        classified = _classify_rpc_exception(
            "rpc", Exception("attempt_not_in_progress: scenario_attempts x has status permission denied-like")
        )
        self.assertIsInstance(classified, ScenarioSupabasePortV2RpcError)

    def test_21_business_prefix_wins_even_if_code_is_a_permission_code(self):
        classified = _classify_rpc_exception(
            "rpc", _CodedException("sequence_mismatch: expected 1 got 2", code="42501")
        )
        self.assertIsInstance(classified, ScenarioSupabasePortV2RpcError)
        self.assertEqual(str(classified), "sequence_mismatch: expected 1 got 2")

    # -- 22: __cause__ preserved through the NEW generic-unknown branch ------

    def test_22_cause_preserved_through_unknown_error_branch(self):
        original = _CodedException(
            "Could not find the function public.foo in the schema cache", code="PGRST202"
        )
        self.client.queue_exception(_START_RPC, original)
        with self.assertRaises(ScenarioSupabasePortV2UnknownError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertIs(ctx.exception.__cause__, original)

    def test_22_cause_preserved_for_error_attribute_carrier_path(self):
        self.client.queue_response(_START_RPC, error={"message": "totally_unrecognized: x", "code": None})
        with self.assertRaises(ScenarioSupabasePortV2UnknownError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertIsNotNone(ctx.exception.__cause__)


class TestReviewClosureLow01Low02(unittest.TestCase):
    """Promotes SCENARIO_ENGINE_V2_SUPABASE_PORT_FOCUSED_REVIEW.md's LOW-01
    and LOW-02 temporary review probes to permanent regression tests."""

    def setUp(self) -> None:
        self.client = _FakeSupabaseClient()
        self.port = SupabaseScenarioOrchestrationV2Port(self.client)

    # -- LOW-01: null `.data` passthrough for the two RPC-call methods -----

    def test_low01_null_data_passthrough_on_start_rpc(self):
        self.client.queue_response(_START_RPC, data=None)
        result = self.port.call_start_or_resume_scenario_attempt_v1(_START_PARAMS)
        self.assertIsNone(result)

    def test_low01_null_data_passthrough_on_submit_rpc(self):
        self.client.queue_response(_SUBMIT_RPC, data=None)
        result = self.port.call_submit_scenario_decision_v1(_SUBMIT_PARAMS)
        self.assertIsNone(result)

    def test_low01_null_data_fails_closed_on_load_attempt_snapshot(self):
        self.client.queue_response(_GET_ATTEMPT_RPC, data=None)
        with self.assertRaises(ScenarioSupabasePortV2MalformedResponseError):
            self.port.load_attempt_snapshot(user_email="learner@example.com", attempt_id="a")

    # -- LOW-02: zero client-side user_email validation ----------------------

    def test_low02_malformed_user_email_forwarded_verbatim_not_rejected_locally(self):
        malformed_email = "not-an-email"
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[_full_attempt_row()])
        # Must NOT raise -- this port performs zero client-side email
        # format/ownership validation of its own; it is a pure pass-through
        # to the RPC's own `p_user_email` parameter.
        self.port.load_attempt_snapshot(user_email=malformed_email, attempt_id="a")
        self.assertEqual(self.client.calls[0][1]["p_user_email"], malformed_email)

    def test_low02_empty_user_email_forwarded_verbatim_not_rejected_locally(self):
        self.client.queue_response(_GET_ATTEMPT_RPC, data=[_full_attempt_row()])
        self.port.load_attempt_snapshot(user_email="", attempt_id="a")
        self.assertEqual(self.client.calls[0][1]["p_user_email"], "")


class TestIntegrationSkipSemantics(unittest.TestCase):
    """Requirement checklist item 28: a real failure once the disposable
    environment starts must never be silently converted into a skip -- the
    only permitted skip path is genuine pinned-image unavailability, and
    only before any container/network is created."""

    def test_image_genuinely_unavailable_raises_skip_test_not_a_failure(self):
        with mock.patch("subprocess.run", side_effect=RuntimeError("no docker hub reachable")):
            with self.assertRaises(unittest.SkipTest):
                TestSupabasePortDisposablePostgrestSmoke._ensure_postgrest_image()

    def test_failure_after_image_check_propagates_and_still_cleans_up(self):
        target = TestSupabasePortDisposablePostgrestSmoke
        with mock.patch.object(target, "_ensure_postgrest_image", return_value=None), mock.patch.object(
            target, "_cleanup_containers"
        ) as mock_cleanup, mock.patch(
            "subprocess.run"
        ) as mock_run, mock.patch.object(
            target, "_start_postgres", side_effect=RuntimeError("simulated postgres startup failure")
        ):
            mock_run.return_value = mock.Mock(returncode=0)
            with self.assertRaises(RuntimeError):
                target.setUpClass()
            self.assertGreaterEqual(mock_cleanup.call_count, 1)


# ---------------------------------------------------------------------------
# End-to-end protocol compatibility (real orchestration public API)
# ---------------------------------------------------------------------------


class _RpcBackedBySupabaseFake:
    """Wraps ``FakeOrchestrationPersistence``'s already-validated CAS /
    idempotency business logic behind a raw ``.rpc(name, params).execute()``
    surface shaped like a real Supabase client, so
    ``SupabaseScenarioOrchestrationV2Port`` can be exercised end-to-end
    through the real, committed orchestration public API."""

    def __init__(self, persistence: FakeOrchestrationPersistence) -> None:
        self._persistence = persistence
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def rpc(self, fn: str, params: Mapping[str, Any]) -> _FakeRpcRequest:
        self.calls.append((fn, dict(params)))
        try:
            if fn == _START_RPC:
                data = self._persistence.call_start_or_resume_scenario_attempt_v1(params)
            elif fn == _SUBMIT_RPC:
                data = self._persistence.call_submit_scenario_decision_v1(params)
            elif fn == _GET_ATTEMPT_RPC:
                row = self._persistence.load_attempt_snapshot(
                    user_email=params["p_user_email"], attempt_id=params["p_attempt_id"]
                )
                data = [row]
            else:
                raise AssertionError(f"unexpected rpc {fn!r}")
        except Exception as exc:  # noqa: BLE001 - re-raised through the same execute() surface a real client uses.
            return _FakeRpcRequest(exception=exc)
        return _FakeRpcRequest(response=_FakeResponse(data=data))


class TestPortSatisfiesOrchestrationProtocolEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _load_document()
        self.content = build_scenario_content_v2(copy.deepcopy(self.document))
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.backend = _RpcBackedBySupabaseFake(self.persistence)
        self.port = SupabaseScenarioOrchestrationV2Port(self.backend)
        self.attempt_id = _new_attempt_id()

    def test_start_submit_resume_idempotency_and_conflict(self):
        start = start_or_resume_scenario_run_v2(
            self.content,
            persistence=self.port,
            user_email=_EMAIL,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=self.attempt_id,
        )
        self.assertEqual(start.attempt_id, self.attempt_id)
        self.assertTrue(start.created)

        option = start.learner_view.scene_view.options[0].id
        submitted = submit_scenario_decision_v2(
            self.content,
            persistence=self.port,
            submission_context=start.submission_context,
            selected_option_id=option,
        )
        self.assertEqual(submitted.sequence_number, 1)

        resumed, _ = resume_and_replay_scenario_run_v2(
            self.content,
            persistence=self.port,
            user_email=_EMAIL,
            attempt_id=self.attempt_id,
        )
        self.assertEqual(resumed.expected_sequence_number, submitted.run.expected_sequence_number)

        retry = submit_scenario_decision_v2(
            self.content,
            persistence=self.port,
            submission_context=start.submission_context,
            selected_option_id=option,
            idempotency_key=submitted.idempotency_key,
        )
        self.assertTrue(retry.idempotent_replay)
        self.assertEqual(len(self.persistence.decisions[self.attempt_id]), 1)

        with self.assertRaises(ScenarioOrchestrationV2SequenceConflictError):
            submit_scenario_decision_v2(
                self.content,
                persistence=self.port,
                submission_context=start.submission_context,
                selected_option_id=option,
            )



# ---------------------------------------------------------------------------
# Disposable REAL PostgREST + real Postgres integration smoke
# ---------------------------------------------------------------------------
#
# Unlike TestScenarioOrchestrationV2DisposableSmoke's psycopg2-backed fake in
# tests/test_scenario_orchestration_v2.py, this class exercises the actual
# postgrest-py SyncPostgrestClient (the same client `supabase-py` wraps
# internally for `.rpc(...)`) talking to a REAL PostgREST server, which in
# turn talks to a REAL disposable Postgres -- proving
# SupabaseScenarioOrchestrationV2Port genuinely satisfies
# ScenarioOrchestrationV2PersistencePort end-to-end against SDK-level
# machinery, not merely a hand-rolled fake shaped to look like one.


# Pinned, immutable-by-convention PostgREST version -- NOT `:latest`. Chosen
# because it is the exact version this test suite was validated against
# (confirmed via `docker run --rm --entrypoint postgrest
# postgrest/postgrest:latest --version` -> "PostgREST 14.16", and via
# `docker image inspect ... --format '{{json .RepoDigests}}'` on both
# `:latest` and `:v14.16` resolving to the IDENTICAL digest
# `sha256:bea1c76a856fa39d1e542d25911cf95d02fe2bf971992d033044ff209f1504b8`
# at pin time). Pinning to a specific version tag (rather than `:latest`)
# means a future PostgREST release can never silently change this test's
# error-response shapes, `.rpc()` semantics, or the exact behavior HIGH-01's
# regression test depends on without a deliberate, reviewed bump of this
# constant. See SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md
# (MEDIUM-01) for the finding this closes. No test-only environment-variable
# override is provided: this repository has no existing precedent for that
# pattern on any other disposable-integration test, so none is introduced
# here either -- bump this constant directly (with a fresh digest
# cross-check) when a deliberate upgrade is warranted.
_POSTGREST_IMAGE = "postgrest/postgrest:v14.16"


def _docker_available() -> bool:
    """True only if the `docker` CLI exists on PATH and the daemon actually
    responds. This is the ONLY condition allowed to cause a silent skip --
    any failure discovered after this point (missing image that fails to
    pull, migration errors, PostgREST/Postgres startup failures, RPC/replay
    assertion failures, ...) must propagate as a real test FAILURE/ERROR,
    never a skip. See SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md
    (MEDIUM-03) for the finding this closes."""
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
        return True
    except Exception:
        return False


_SCENARIO_MIGRATION_FILES = (
    "20260718170000_v66_scenario_definition_persistence_foundation.sql",
    "20260719003000_v67_harden_scenario_definition_security.sql",
    "20260719130000_v68_scenario_attempt_persistence_foundation.sql",
    "20260719140000_v69_scenario_v2_attempt_identity_support.sql",
)


def _scenario_migrations_present() -> bool:
    """Disposable DB smoke needs migration SQL files; this narrow production
    candidate intentionally excludes them because production already has the
    schema and Render does not execute migrations on deploy.
    """
    root = REPO_ROOT / "supabase" / "migrations"
    return all((root / name).is_file() for name in _SCENARIO_MIGRATION_FILES)


@unittest.skipUnless(
    _docker_available() and _scenario_migrations_present(),
    "docker unavailable or scenario migration SQL excluded from narrow candidate",
)
class TestSupabasePortDisposablePostgrestSmoke(unittest.TestCase):
    """Real ``postgrest-py`` client against a real disposable PostgREST
    server backed by a real disposable Postgres. Both containers (plus the
    docker network joining them) are destroyed in ``tearDownClass``, even if
    ``setUpClass`` itself fails partway through. Never touches production."""

    NETWORK = "certbound-v2-port-smoke-net"
    PG_CONTAINER = "certbound-v2-port-smoke-pg"
    POSTGREST_CONTAINER = "certbound-v2-port-smoke-postgrest"
    PG_HOST_PORT = 55434
    POSTGREST_HOST_PORT = 33002
    # Throwaway, local-only, disposable-container credentials -- never used
    # outside this test's own ephemeral Docker network, rotated (regenerated)
    # every test run, and never persisted anywhere.
    AUTHENTICATOR_PASSWORD = "disposable-postgrest-smoke-authenticator-pw"
    JWT_SECRET = "disposable-postgrest-smoke-hs256-jwt-secret-1234567890ABCDEF"
    MIGRATIONS = (
        "20260718170000_v66_scenario_definition_persistence_foundation.sql",
        "20260719003000_v67_harden_scenario_definition_security.sql",
        "20260719130000_v68_scenario_attempt_persistence_foundation.sql",
        "20260719140000_v69_scenario_v2_attempt_identity_support.sql",
    )

    @classmethod
    def setUpClass(cls) -> None:
        # The ONLY step in this method allowed to raise `unittest.SkipTest`.
        # No container or network exists yet, so no cleanup is needed before
        # a skip here. Every subsequent step's failure is a real test
        # failure/error, never a skip -- see MEDIUM-03 in
        # SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md.
        cls._ensure_postgrest_image()
        cls._cleanup_containers()
        subprocess.run(["docker", "network", "rm", cls.NETWORK], check=False, capture_output=True)
        subprocess.run(["docker", "network", "create", cls.NETWORK], check=True, capture_output=True)
        try:
            cls._start_postgres()
            cls._bootstrap_roles()
            for migration in cls.MIGRATIONS:
                cls._psql_file(REPO_ROOT / "supabase" / "migrations" / migration)
            cls.scenario_version_id, cls.content = cls._seed_scenario_fixture()
            cls._start_postgrest()
            cls._wait_for_postgrest_ready()
        except Exception:
            cls._cleanup_containers()
            subprocess.run(["docker", "network", "rm", cls.NETWORK], check=False, capture_output=True)
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._cleanup_containers()
        subprocess.run(["docker", "network", "rm", cls.NETWORK], check=False, capture_output=True)

    @classmethod
    def _ensure_postgrest_image(cls) -> None:
        """Pull the pinned ``_POSTGREST_IMAGE`` if not already cached.

        Raises ``unittest.SkipTest`` with an explicit reason ONLY when the
        pinned image genuinely cannot be obtained (e.g. no outbound network
        access to Docker Hub in this environment) -- this is the single,
        clearly-labeled escape valve this task's own instructions permit
        ("If an image pull is unavailable in a network-isolated environment,
        skipping is acceptable only with a clear reason."). Any other
        failure (a real migration/RPC/replay/assertion failure once the
        disposable environment is actually running) is never converted to a
        skip anywhere in this class.
        """
        try:
            subprocess.run(
                ["docker", "image", "inspect", _POSTGREST_IMAGE],
                check=True,
                capture_output=True,
                timeout=10,
            )
            return
        except Exception:
            pass
        try:
            subprocess.run(
                ["docker", "pull", _POSTGREST_IMAGE],
                check=True,
                capture_output=True,
                timeout=120,
            )
            return
        except Exception as exc:
            raise unittest.SkipTest(
                f"pinned postgrest image {_POSTGREST_IMAGE!r} is not cached locally and could not be "
                f"pulled (likely no outbound network access to Docker Hub in this environment): {exc}"
            ) from exc

    @classmethod
    def _cleanup_containers(cls) -> None:
        subprocess.run(["docker", "rm", "-f", cls.POSTGREST_CONTAINER], check=False, capture_output=True)
        subprocess.run(["docker", "rm", "-f", cls.PG_CONTAINER], check=False, capture_output=True)

    @classmethod
    def _docker_exec_pg(cls, *args: str, input: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "exec", "-i", cls.PG_CONTAINER, *args],
            check=True,
            capture_output=True,
            text=True,
            input=input,
        )

    @classmethod
    def _psql(cls, sql: str) -> str:
        return cls._docker_exec_pg("psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", sql).stdout

    @classmethod
    def _psql_file(cls, path: Path) -> None:
        content = path.read_text(encoding="utf-8")
        cls._docker_exec_pg("psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-", input=content)

    @classmethod
    def _start_postgres(cls) -> None:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cls.PG_CONTAINER, "--network", cls.NETWORK,
                "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                "-p", f"{cls.PG_HOST_PORT}:5432",
                "postgres:16",
            ],
            check=True,
            capture_output=True,
        )
        deadline = time.time() + 30
        last_error: Optional[BaseException] = None
        while time.time() < deadline:
            try:
                cls._docker_exec_pg("pg_isready", "-U", "postgres")
                return
            except subprocess.CalledProcessError as exc:
                last_error = exc
                time.sleep(1)
        raise RuntimeError(f"disposable postgres container never became ready: {last_error}")

    @classmethod
    def _bootstrap_roles(cls) -> None:
        cls._psql(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon NOLOGIN; END IF; "
            "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF; "
            "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'service_role') THEN CREATE ROLE service_role NOLOGIN; END IF; "
            "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticator') THEN "
            f"CREATE ROLE authenticator LOGIN PASSWORD '{cls.AUTHENTICATOR_PASSWORD}' NOINHERIT; END IF; "
            "END $$;"
        )
        # Real hosted Supabase provisions `service_role` with the Postgres
        # BYPASSRLS role attribute out of band -- outside of, and prior to,
        # any migration this repository owns. The committed V66/V67/V68
        # migrations deliberately enable RLS with ZERO policies on every
        # RLS-protected table (see their own "RLS design" sections),
        # relying entirely on that platform-level BYPASSRLS grant -- never a
        # table policy -- to let `service_role` see rows through its RPCs.
        # A disposable bare `postgres:16` container does not provision that
        # automatically, so this test replicates it explicitly here to
        # faithfully exercise the exact trust model documented in
        # utils/scenario_supabase_port_v2.py, rather than accidentally
        # passing only because a from-scratch container's `service_role`
        # happens to equal the table owner.
        cls._psql("ALTER ROLE service_role BYPASSRLS;")
        cls._psql("GRANT service_role TO authenticator; GRANT anon TO authenticator; GRANT authenticated TO authenticator;")

    @classmethod
    def _seed_scenario_fixture(cls) -> Tuple[str, Any]:
        import psycopg2

        document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        content = build_scenario_content_v2(document)
        conn = psycopg2.connect(host="127.0.0.1", port=cls.PG_HOST_PORT, user="postgres", dbname="postgres")
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.scenarios (simulation_id, certification_exam_name, title) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (content.simulation_id, "Business Analyst", "Supabase Port Disposable Smoke"),
                )
                scenario_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO public.scenario_versions "
                    "(scenario_id, version, schema_version, engine_version, source_repository_path) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (
                        scenario_id,
                        content.version,
                        content.schema_version,
                        ENGINE_VERSION,
                        "tests/fixtures/scenario_engine_v2_vslice_1_1_0.json",
                    ),
                )
                version_id = cur.fetchone()[0]
                cur.execute(
                    "SELECT public.publish_scenario_version_v1(%s, %s::jsonb, %s)",
                    (version_id, json.dumps(document), content.canonical_content_sha256),
                )
        finally:
            conn.close()
        return str(version_id), content

    @classmethod
    def _start_postgrest(cls) -> None:
        db_uri = f"postgres://authenticator:{cls.AUTHENTICATOR_PASSWORD}@{cls.PG_CONTAINER}:5432/postgres"
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cls.POSTGREST_CONTAINER, "--network", cls.NETWORK,
                "-p", f"{cls.POSTGREST_HOST_PORT}:3000",
                "-e", f"PGRST_DB_URI={db_uri}",
                "-e", "PGRST_DB_SCHEMAS=public",
                "-e", "PGRST_DB_ANON_ROLE=anon",
                "-e", f"PGRST_JWT_SECRET={cls.JWT_SECRET}",
                _POSTGREST_IMAGE,
            ],
            check=True,
            capture_output=True,
        )

    @classmethod
    def _wait_for_postgrest_ready(cls) -> None:
        import httpx

        deadline = time.time() + 30
        last_error: Optional[BaseException] = None
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{cls.POSTGREST_HOST_PORT}/", timeout=2)
                return
            except Exception as exc:  # noqa: BLE001 - readiness probe, retried until deadline.
                last_error = exc
                time.sleep(1)
        raise RuntimeError(f"disposable postgrest container never became ready: {last_error}")

    @classmethod
    def _mint_service_role_jwt(cls) -> str:
        import jwt as pyjwt

        payload = {
            "role": "service_role",
            "iss": "certbound-v2-port-disposable-smoke",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
        return pyjwt.encode(payload, cls.JWT_SECRET, algorithm="HS256")

    def setUp(self) -> None:
        from postgrest import SyncPostgrestClient

        token = self._mint_service_role_jwt()
        client = SyncPostgrestClient(
            f"http://127.0.0.1:{self.POSTGREST_HOST_PORT}",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.port = SupabaseScenarioOrchestrationV2Port(client)
        self.attempt_id = _new_attempt_id()
        self.email = f"postgrest-smoke-{uuid.uuid4().hex[:8]}@example.com"

    def test_real_postgrest_start_submit_resume_idempotency_and_conflict(self):
        start = start_or_resume_scenario_run_v2(
            self.content,
            persistence=self.port,
            user_email=self.email,
            scenario_version_id=self.scenario_version_id,
            attempt_id=self.attempt_id,
        )
        self.assertEqual(start.attempt_id, self.attempt_id)
        self.assertTrue(start.created)

        option = start.learner_view.scene_view.options[0].id
        submitted = submit_scenario_decision_v2(
            self.content,
            persistence=self.port,
            submission_context=start.submission_context,
            selected_option_id=option,
        )
        self.assertEqual(submitted.sequence_number, 1)

        resumed, _ = resume_and_replay_scenario_run_v2(
            self.content,
            persistence=self.port,
            user_email=self.email,
            attempt_id=self.attempt_id,
        )
        self.assertEqual(resumed.expected_sequence_number, submitted.run.expected_sequence_number)

        retry = submit_scenario_decision_v2(
            self.content,
            persistence=self.port,
            submission_context=start.submission_context,
            selected_option_id=option,
            idempotency_key=submitted.idempotency_key,
        )
        self.assertTrue(retry.idempotent_replay)

        with self.assertRaises(ScenarioOrchestrationV2SequenceConflictError):
            submit_scenario_decision_v2(
                self.content,
                persistence=self.port,
                submission_context=start.submission_context,
                selected_option_id=option,
            )

    def test_real_postgrest_unknown_function_error_is_sanitized(self):
        """HIGH-01 regression, exercised against a REAL disposable PostgREST
        server (not a fake): calling a function that genuinely does not
        exist reproduces the exact real-world PostgREST `PGRST202`
        "function not found in schema cache" error this finding was
        originally reported against, and proves the corrected port
        sanitizes it rather than preserving it verbatim."""
        with self.assertRaises(ScenarioSupabasePortV2UnknownError) as ctx:
            self.port.call_start_or_resume_scenario_attempt_v1(
                {"p_bogus_param": "definitely_does_not_exist_xyz_v1"}
            )
        message = str(ctx.exception)
        self.assertNotIn("schema cache", message)
        self.assertNotIn("p_bogus_param", message)
        self.assertNotIn("does not exist", message)
        self.assertIsNotNone(ctx.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
