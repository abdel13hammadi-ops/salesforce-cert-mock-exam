"""
Unit tests for workers/background_worker.py and workers/job_handlers.py.

Tests are fully hermetic: no Supabase connection, no network, no Streamlit.
The Supabase client is replaced with a FakeSupabase that captures all RPC calls
and returns configurable responses.

Coverage
--------
  T1  No job available — run_once returns False, no RPC besides claim
  T2  Claim and dispatch — correct job fields forwarded to handler
  T3  Successful handler — complete RPC called with handler result
  T4  Handler exception — fail RPC called with error message
  T5  Unsupported job type — fail RPC called without invoking any handler
  T6  Worker shutdown — request_shutdown stops the polling loop
  T7  No direct table writes — worker never calls client.table()

Run:
    python -m pytest tests/test_background_worker.py -v
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Make project root importable regardless of cwd.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.background_worker import (
    BackgroundWorker,
    DEFAULT_RECOVERY_INTERVAL_SECONDS,
    DEFAULT_RECOVERY_LIMIT,
    DEFAULT_RECOVERY_RETRY_DELAY_SECONDS,
    ENV_RECOVERY_INTERVAL,
    ENV_RECOVERY_LIMIT,
    ENV_RECOVERY_RETRY_DELAY,
    MAX_RECOVERY_LIMIT,
    MAX_RECOVERY_RETRY_DELAY_SECONDS,
    load_recovery_settings_from_env,
)
from workers.job_handlers import (
    HANDLER_REGISTRY,
    NotImplementedHandler,
    HandlerPayloadError,
    make_resource_ingestion_handler,
    make_candidate_promotion_handler,
    build_handler_registry,
)


# ===========================================================================
# Fake Supabase infrastructure
# ===========================================================================

class FakeRpcResult:
    """Mimics the PostgREST response object returned by .execute()."""

    def __init__(self, data: Optional[List[dict]] = None, error=None) -> None:
        self.data = data if data is not None else []
        self.error = error


class FakeRpcBuilder:
    """Mimics the builder returned by client.rpc(...)."""

    def __init__(self, result: FakeRpcResult) -> None:
        self._result = result

    def execute(self) -> FakeRpcResult:
        return self._result


class FakeSupabase:
    """Captures all .rpc(...) calls and returns configurable responses.

    Does NOT expose a .table() method by design — any attempt by the worker
    to call client.table() will raise AttributeError, making that assertion
    trivially testable.
    """

    def __init__(self) -> None:
        self.rpc_calls: List[Dict[str, Any]] = []
        self._defaults: Dict[str, FakeRpcResult] = {}
        # Sequence overrides: consumed in order before falling back to defaults.
        self._sequences: Dict[str, List[List[dict]]] = {}

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def set_response(self, rpc_name: str, data: List[dict]) -> None:
        """Set a static response for an RPC name."""
        self._defaults[rpc_name] = FakeRpcResult(data=data)

    def set_error_response(self, rpc_name: str, error: str) -> None:
        """Set an error response for an RPC name (simulates Supabase error)."""
        self._defaults[rpc_name] = FakeRpcResult(data=None, error=error)

    def set_sequence(self, rpc_name: str, data_list: List[List[dict]]) -> None:
        """Set multiple responses consumed in order; falls back to default."""
        self._sequences[rpc_name] = list(data_list)

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    def rpc(self, name: str, params: Optional[dict] = None) -> FakeRpcBuilder:
        self.rpc_calls.append({"name": name, "params": params or {}})
        if name in self._sequences and self._sequences[name]:
            data = self._sequences[name].pop(0)
            return FakeRpcBuilder(FakeRpcResult(data=data))
        return FakeRpcBuilder(self._defaults.get(name, FakeRpcResult(data=[])))

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def calls_for(self, rpc_name: str) -> List[Dict[str, Any]]:
        return [c for c in self.rpc_calls if c["name"] == rpc_name]

    @property
    def called_rpc_names(self) -> set:
        return {c["name"] for c in self.rpc_calls}


# ===========================================================================
# Shared factories
# ===========================================================================

_SAMPLE_JOB = {
    "job_id":        "aaaaaaaa-0000-0000-0000-000000000001",
    "job_type":      "other",
    "payload":       {"key": "value"},
    "checkpoint":    {},
    "attempt_count": 1,
    "max_attempts":  3,
    "lease_expires_at": "2099-01-01T00:00:00+00:00",
    "model_name":    None,
    "prompt_version": None,
    "metadata":      {},
}


def _make_worker(
    fake: FakeSupabase,
    handlers: Optional[Dict] = None,
    job_types: Optional[List[str]] = None,
    sleep_interval: float = 0.0,
) -> BackgroundWorker:
    return BackgroundWorker(
        worker_id="test-worker-1",
        client=fake,
        handlers=handlers if handlers is not None else {},
        job_types=job_types,
        lease_seconds=60,
        sleep_interval=sleep_interval,
    )


# ===========================================================================
# Tests
# ===========================================================================

class TestNoJobAvailable(unittest.TestCase):
    """T1: When no job is available, run_once returns False."""

    def test_returns_false_when_queue_empty(self):
        fake = FakeSupabase()
        # claim RPC returns empty list (no job)
        fake.set_response("claim_background_job_v1", [])

        worker = _make_worker(fake)
        result = worker.run_once()

        self.assertFalse(result, "run_once must return False when no job is available")

    def test_only_claim_rpc_called(self):
        fake = FakeSupabase()
        fake.set_response("recover_expired_background_jobs_v1",
                          [{"recovered_count": 0, "dead_letter_count": 0}])
        fake.set_response("claim_background_job_v1", [])
        worker = _make_worker(fake)
        worker.run_once()

        self.assertIn("recover_expired_background_jobs_v1", fake.called_rpc_names)
        self.assertIn("claim_background_job_v1", fake.called_rpc_names)
        names = [c["name"] for c in fake.rpc_calls]
        self.assertLess(
            names.index("recover_expired_background_jobs_v1"),
            names.index("claim_background_job_v1"),
        )
        # heartbeat, complete, and fail must NOT be called
        for forbidden in (
            "heartbeat_background_job_v1",
            "complete_background_job_v1",
            "fail_background_job_v1",
        ):
            self.assertNotIn(forbidden, fake.called_rpc_names,
                             f"{forbidden} must not be called on empty queue")


class TestClaimAndDispatch(unittest.TestCase):
    """T2: Correct job fields are forwarded to the handler."""

    def test_handler_receives_correct_arguments(self):
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [_SAMPLE_JOB])
        fake.set_response("heartbeat_background_job_v1",
                          [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "running",
                            "lease_expires_at": "2099-01-01T00:00:00+00:00",
                            "heartbeat_at": "2099-01-01T00:00:00+00:00"}])
        fake.set_response("complete_background_job_v1",
                          [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "completed",
                            "completed_at": "2099-01-01T00:00:00+00:00"}])

        received: Dict[str, Any] = {}

        def capturing_handler(job_id, payload, checkpoint, attempt, heartbeat_fn):
            received.update({
                "job_id": job_id,
                "payload": payload,
                "checkpoint": checkpoint,
                "attempt": attempt,
            })
            return {"done": True}

        worker = _make_worker(fake, handlers={"other": capturing_handler})
        result = worker.run_once()

        self.assertTrue(result, "run_once must return True when a job was processed")
        self.assertEqual(received["job_id"],    _SAMPLE_JOB["job_id"])
        self.assertEqual(received["payload"],   _SAMPLE_JOB["payload"])
        self.assertEqual(received["checkpoint"], _SAMPLE_JOB["checkpoint"])
        self.assertEqual(received["attempt"],   _SAMPLE_JOB["attempt_count"])


class TestJobRecovery(unittest.TestCase):
    """Expired-lease recovery via recover_expired_background_jobs_v1."""

    @staticmethod
    def _recover_response(recovered: int = 0, dead_letter: int = 0) -> List[dict]:
        return [{"recovered_count": recovered, "dead_letter_count": dead_letter}]

    def test_startup_recovery_occurs_before_claim(self):
        fake = FakeSupabase()
        fake.set_response("recover_expired_background_jobs_v1", self._recover_response())
        fake.set_response("claim_background_job_v1", [])
        worker = _make_worker(fake)

        worker.run_once()

        names = [c["name"] for c in fake.rpc_calls]
        self.assertEqual(names[0], "recover_expired_background_jobs_v1")
        self.assertEqual(names[1], "claim_background_job_v1")

    def test_once_mode_performs_recovery_before_claim(self):
        fake = FakeSupabase()
        fake.set_response("recover_expired_background_jobs_v1", self._recover_response())
        fake.set_response("claim_background_job_v1", [])
        worker = _make_worker(fake)

        worker.run_once()

        recover_idx = next(
            i for i, call in enumerate(fake.rpc_calls)
            if call["name"] == "recover_expired_background_jobs_v1"
        )
        claim_idx = next(
            i for i, call in enumerate(fake.rpc_calls)
            if call["name"] == "claim_background_job_v1"
        )
        self.assertLess(recover_idx, claim_idx)

    def test_periodic_recovery_respects_interval(self):
        fake = FakeSupabase()
        fake.set_response("recover_expired_background_jobs_v1", self._recover_response())
        worker = BackgroundWorker(
            worker_id="test-worker-1",
            client=fake,
            handlers={},
            recovery_interval_seconds=60,
        )
        worker._startup_recovery_done = True
        worker._last_recovery_at = 1000.0

        with patch("workers.background_worker.time.monotonic", return_value=1030.0):
            worker._maybe_periodic_recovery()
        self.assertEqual(len(fake.calls_for("recover_expired_background_jobs_v1")), 0)

        with patch("workers.background_worker.time.monotonic", return_value=1061.0):
            worker._maybe_periodic_recovery()
        self.assertEqual(len(fake.calls_for("recover_expired_background_jobs_v1")), 1)

    def test_zero_interval_disables_periodic_recovery(self):
        fake = FakeSupabase()
        fake.set_response("recover_expired_background_jobs_v1", self._recover_response())
        fake.set_response("claim_background_job_v1", [])
        worker = BackgroundWorker(
            worker_id="shutdown-test",
            client=fake,
            handlers={},
            sleep_interval=0.0,
            recovery_interval_seconds=0,
        )

        iterations = [0]
        original_run_once = worker.run_once

        def counting_run_once():
            iterations[0] += 1
            result = original_run_once()
            if iterations[0] >= 3:
                worker.request_shutdown()
            return result

        worker.run_once = counting_run_once

        with patch("time.sleep"):
            worker.run()

        self.assertEqual(len(fake.calls_for("recover_expired_background_jobs_v1")), 1)

    def test_recovery_rpc_failure_does_not_crash_worker(self):
        fake = FakeSupabase()
        fake.set_error_response("recover_expired_background_jobs_v1", "recovery unavailable")
        fake.set_response("claim_background_job_v1", [])
        worker = _make_worker(fake)

        worker.run_once()

        self.assertIn("claim_background_job_v1", fake.called_rpc_names)


class TestRecoverySettingsFromEnv(unittest.TestCase):
    """Validated parsing for CERTBOUND_JOB_RECOVERY_* environment variables."""

    def test_missing_values_use_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = load_recovery_settings_from_env()

        self.assertEqual(
            settings,
            {
                "recovery_interval_seconds": DEFAULT_RECOVERY_INTERVAL_SECONDS,
                "recovery_limit": DEFAULT_RECOVERY_LIMIT,
                "recovery_retry_delay_seconds": DEFAULT_RECOVERY_RETRY_DELAY_SECONDS,
            },
        )

    def test_malformed_non_integer_uses_default_with_warning(self):
        env = {ENV_RECOVERY_INTERVAL: "not-a-number"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("workers.background_worker", level="WARNING") as logs:
                settings = load_recovery_settings_from_env()

        self.assertEqual(settings["recovery_interval_seconds"], 60)
        self.assertTrue(
            any(ENV_RECOVERY_INTERVAL in message for message in logs.output)
        )

    def test_negative_interval_uses_default_with_warning(self):
        env = {ENV_RECOVERY_INTERVAL: "-5"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("workers.background_worker", level="WARNING") as logs:
                settings = load_recovery_settings_from_env()

        self.assertEqual(settings["recovery_interval_seconds"], 60)
        self.assertTrue(
            any(ENV_RECOVERY_INTERVAL in message for message in logs.output)
        )

    def test_zero_or_negative_recovery_limit_uses_default_with_warning(self):
        for invalid_value in ("0", "-1"):
            with self.subTest(invalid_value=invalid_value):
                env = {ENV_RECOVERY_LIMIT: invalid_value}
                with patch.dict(os.environ, env, clear=True):
                    with self.assertLogs("workers.background_worker", level="WARNING") as logs:
                        settings = load_recovery_settings_from_env()

                self.assertEqual(settings["recovery_limit"], 100)
                self.assertTrue(
                    any(ENV_RECOVERY_LIMIT in message for message in logs.output)
                )

    def test_negative_retry_delay_uses_default_with_warning(self):
        env = {ENV_RECOVERY_RETRY_DELAY: "-10"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("workers.background_worker", level="WARNING") as logs:
                settings = load_recovery_settings_from_env()

        self.assertEqual(settings["recovery_retry_delay_seconds"], 60)
        self.assertTrue(
            any(ENV_RECOVERY_RETRY_DELAY in message for message in logs.output)
        )

    def test_valid_zero_interval_is_accepted(self):
        env = {ENV_RECOVERY_INTERVAL: "0"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_recovery_settings_from_env()

        self.assertEqual(settings["recovery_interval_seconds"], 0)

    def test_valid_custom_values_are_accepted(self):
        env = {
            ENV_RECOVERY_INTERVAL: "120",
            ENV_RECOVERY_LIMIT: "25",
            ENV_RECOVERY_RETRY_DELAY: "15",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_recovery_settings_from_env()

        self.assertEqual(
            settings,
            {
                "recovery_interval_seconds": 120,
                "recovery_limit": 25,
                "recovery_retry_delay_seconds": 15,
            },
        )

    def test_maximum_recovery_limit_is_preserved(self):
        env = {ENV_RECOVERY_LIMIT: str(MAX_RECOVERY_LIMIT)}
        with patch.dict(os.environ, env, clear=True):
            settings = load_recovery_settings_from_env()

        self.assertEqual(settings["recovery_limit"], MAX_RECOVERY_LIMIT)

    def test_recovery_limit_one_above_maximum_falls_back_to_default(self):
        env = {ENV_RECOVERY_LIMIT: str(MAX_RECOVERY_LIMIT + 1)}
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("workers.background_worker", level="WARNING") as logs:
                settings = load_recovery_settings_from_env()

        self.assertEqual(settings["recovery_limit"], DEFAULT_RECOVERY_LIMIT)
        self.assertTrue(
            any(ENV_RECOVERY_LIMIT in message for message in logs.output)
        )
        self.assertTrue(
            any(str(MAX_RECOVERY_LIMIT) in message for message in logs.output)
        )
        self.assertTrue(
            any(str(DEFAULT_RECOVERY_LIMIT) in message for message in logs.output)
        )

    def test_maximum_recovery_retry_delay_is_preserved(self):
        env = {ENV_RECOVERY_RETRY_DELAY: str(MAX_RECOVERY_RETRY_DELAY_SECONDS)}
        with patch.dict(os.environ, env, clear=True):
            settings = load_recovery_settings_from_env()

        self.assertEqual(
            settings["recovery_retry_delay_seconds"],
            MAX_RECOVERY_RETRY_DELAY_SECONDS,
        )

    def test_valid_zero_retry_delay_is_accepted(self):
        env = {ENV_RECOVERY_RETRY_DELAY: "0"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_recovery_settings_from_env()

        self.assertEqual(settings["recovery_retry_delay_seconds"], 0)

    def test_recovery_retry_delay_one_above_maximum_falls_back_to_default(self):
        env = {ENV_RECOVERY_RETRY_DELAY: str(MAX_RECOVERY_RETRY_DELAY_SECONDS + 1)}
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("workers.background_worker", level="WARNING") as logs:
                settings = load_recovery_settings_from_env()

        self.assertEqual(
            settings["recovery_retry_delay_seconds"],
            DEFAULT_RECOVERY_RETRY_DELAY_SECONDS,
        )
        self.assertTrue(
            any(ENV_RECOVERY_RETRY_DELAY in message for message in logs.output)
        )
        self.assertTrue(
            any(str(MAX_RECOVERY_RETRY_DELAY_SECONDS) in message for message in logs.output)
        )
        self.assertTrue(
            any(str(DEFAULT_RECOVERY_RETRY_DELAY_SECONDS) in message for message in logs.output)
        )


class TestSuccessfulHandler(unittest.TestCase):
    """T3: A handler that returns successfully causes complete RPC to be called."""

    def setUp(self):
        self.fake = FakeSupabase()
        self.fake.set_response("claim_background_job_v1", [_SAMPLE_JOB])
        self.fake.set_response("heartbeat_background_job_v1",
                               [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "running",
                                 "lease_expires_at": "2099-01-01T00:00:00+00:00",
                                 "heartbeat_at": "2099-01-01T00:00:00+00:00"}])
        self.fake.set_response("complete_background_job_v1",
                               [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "completed",
                                 "completed_at": "2099-01-01T00:00:00+00:00"}])

    def _make_success_handler(self, return_value=None):
        def handler(job_id, payload, checkpoint, attempt, heartbeat_fn):
            return return_value
        return handler

    def test_complete_rpc_is_called(self):
        worker = _make_worker(
            self.fake,
            handlers={"other": self._make_success_handler({"output": "ok"})},
        )
        worker.run_once()
        self.assertIn("complete_background_job_v1", self.fake.called_rpc_names,
                      "complete RPC must be called on handler success")

    def test_fail_rpc_is_not_called(self):
        worker = _make_worker(
            self.fake,
            handlers={"other": self._make_success_handler()},
        )
        worker.run_once()
        self.assertNotIn("fail_background_job_v1", self.fake.called_rpc_names,
                         "fail RPC must NOT be called on handler success")

    def test_complete_rpc_receives_handler_result(self):
        handler_result = {"lines_processed": 42}
        worker = _make_worker(
            self.fake,
            handlers={"other": self._make_success_handler(handler_result)},
        )
        worker.run_once()

        complete_calls = self.fake.calls_for("complete_background_job_v1")
        self.assertEqual(len(complete_calls), 1)
        self.assertEqual(
            complete_calls[0]["params"]["p_result"],
            handler_result,
        )


class TestHandlerFailure(unittest.TestCase):
    """T4: A handler that raises causes the fail RPC to be called."""

    def setUp(self):
        self.fake = FakeSupabase()
        self.fake.set_response("claim_background_job_v1", [_SAMPLE_JOB])
        self.fake.set_response("heartbeat_background_job_v1",
                               [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "running",
                                 "lease_expires_at": "2099-01-01T00:00:00+00:00",
                                 "heartbeat_at": "2099-01-01T00:00:00+00:00"}])
        self.fake.set_response("fail_background_job_v1",
                               [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "pending",
                                 "available_at": "2099-01-01T00:00:00+00:00",
                                 "completed_at": None}])

    def _raising_handler(self, exc: Exception):
        def handler(job_id, payload, checkpoint, attempt, heartbeat_fn):
            raise exc
        return handler

    def test_fail_rpc_is_called(self):
        worker = _make_worker(
            self.fake,
            handlers={"other": self._raising_handler(RuntimeError("network error"))},
        )
        worker.run_once()
        self.assertIn("fail_background_job_v1", self.fake.called_rpc_names,
                      "fail RPC must be called when handler raises")

    def test_complete_rpc_is_not_called(self):
        worker = _make_worker(
            self.fake,
            handlers={"other": self._raising_handler(ValueError("bad input"))},
        )
        worker.run_once()
        self.assertNotIn("complete_background_job_v1", self.fake.called_rpc_names,
                         "complete RPC must NOT be called when handler raises")

    def test_fail_rpc_receives_error_message(self):
        worker = _make_worker(
            self.fake,
            handlers={"other": self._raising_handler(RuntimeError("exploded"))},
        )
        worker.run_once()
        fail_calls = self.fake.calls_for("fail_background_job_v1")
        self.assertEqual(len(fail_calls), 1)
        error_msg = fail_calls[0]["params"]["p_error_message"]
        self.assertIn("exploded", error_msg, "error message must include the exception text")
        self.assertIn("RuntimeError", error_msg, "error message must include the exception type")

    def test_stub_handler_causes_fail(self):
        """NotImplementedHandler from a stub also triggers fail RPC."""
        worker = _make_worker(
            self.fake,
            handlers={"other": HANDLER_REGISTRY["other"]},
        )
        worker.run_once()
        self.assertIn("fail_background_job_v1", self.fake.called_rpc_names)
        fail_msg = self.fake.calls_for("fail_background_job_v1")[0]["params"]["p_error_message"]
        self.assertIn("not yet implemented", fail_msg)


class TestUnsupportedJobType(unittest.TestCase):
    """T5: An unsupported job type fails the job without invoking any handler."""

    def test_fail_called_for_unknown_type(self):
        fake = FakeSupabase()
        job = {**_SAMPLE_JOB, "job_type": "totally_unknown_type"}
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response("fail_background_job_v1",
                          [{"job_id": job["job_id"], "job_status": "pending",
                            "available_at": None, "completed_at": None}])

        worker = _make_worker(fake, handlers={})  # empty registry
        worker.run_once()

        self.assertIn("fail_background_job_v1", fake.called_rpc_names)

    def test_heartbeat_not_called_for_unknown_type(self):
        fake = FakeSupabase()
        job = {**_SAMPLE_JOB, "job_type": "totally_unknown_type"}
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response("fail_background_job_v1", [{}])

        worker = _make_worker(fake, handlers={})
        worker.run_once()

        self.assertNotIn("heartbeat_background_job_v1", fake.called_rpc_names,
                         "heartbeat must not be sent for an unsupported job type")

    def test_fail_message_contains_job_type(self):
        fake = FakeSupabase()
        job = {**_SAMPLE_JOB, "job_type": "totally_unknown_type"}
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response("fail_background_job_v1", [{}])

        worker = _make_worker(fake, handlers={})
        worker.run_once()

        fail_msg = fake.calls_for("fail_background_job_v1")[0]["params"]["p_error_message"]
        self.assertIn("totally_unknown_type", fail_msg)


class TestWorkerShutdown(unittest.TestCase):
    """T6: request_shutdown stops the polling loop cleanly."""

    def test_shutdown_stops_loop(self):
        fake = FakeSupabase()
        # Queue is always empty; the shutdown flag is what terminates the loop.
        fake.set_response("claim_background_job_v1", [])

        iterations = [0]
        original_run_once = None

        worker = BackgroundWorker(
            worker_id="shutdown-test",
            client=fake,
            handlers={},
            sleep_interval=0.0,
        )

        # Patch run_once to count calls and request shutdown after the first.
        original_run_once = worker.run_once

        def counting_run_once():
            iterations[0] += 1
            result = original_run_once()
            if iterations[0] >= 1:
                worker.request_shutdown()
            return result

        worker.run_once = counting_run_once

        start = time.monotonic()
        with patch("time.sleep"):
            worker.run()
        elapsed = time.monotonic() - start

        self.assertTrue(worker._shutdown_requested, "shutdown flag must be set")
        self.assertGreaterEqual(iterations[0], 1, "loop must have iterated at least once")
        self.assertLess(elapsed, 5.0, "shutdown must complete quickly")

    def test_request_shutdown_sets_flag(self):
        fake = FakeSupabase()
        worker = _make_worker(fake)
        self.assertFalse(worker._shutdown_requested)
        worker.request_shutdown()
        self.assertTrue(worker._shutdown_requested)


class TestNoDirectTableWrites(unittest.TestCase):
    """T7: The worker never calls client.table() for any operation."""

    def test_no_table_method_called_on_success(self):
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [_SAMPLE_JOB])
        fake.set_response("heartbeat_background_job_v1",
                          [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "running",
                            "lease_expires_at": "2099-01-01T00:00:00+00:00",
                            "heartbeat_at": "2099-01-01T00:00:00+00:00"}])
        fake.set_response("complete_background_job_v1",
                          [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "completed",
                            "completed_at": "2099-01-01T00:00:00+00:00"}])

        worker = _make_worker(
            fake,
            handlers={"other": lambda **kw: {"done": True}},
        )
        # FakeSupabase has no .table attribute; calling it would raise AttributeError.
        # If the worker does not call .table(), no error occurs.
        try:
            worker.run_once()
        except AttributeError as exc:
            self.fail(f"worker called client.table() directly: {exc}")

    def test_no_table_method_called_on_failure(self):
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [_SAMPLE_JOB])
        fake.set_response("heartbeat_background_job_v1",
                          [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "running",
                            "lease_expires_at": "2099-01-01T00:00:00+00:00",
                            "heartbeat_at": "2099-01-01T00:00:00+00:00"}])
        fake.set_response("fail_background_job_v1",
                          [{"job_id": _SAMPLE_JOB["job_id"], "job_status": "pending",
                            "available_at": None, "completed_at": None}])

        def raising_handler(**kw):
            raise RuntimeError("handler failure")

        worker = _make_worker(fake, handlers={"other": raising_handler})
        try:
            worker.run_once()
        except AttributeError as exc:
            self.fail(f"worker called client.table() directly: {exc}")

    def test_only_rpc_methods_used(self):
        """All calls to the client must go through .rpc()."""
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [])
        worker = _make_worker(fake)
        worker.run_once()
        # All calls are captured in rpc_calls; none should be direct table ops.
        for call in fake.rpc_calls:
            self.assertIn("name", call,
                          "every client interaction must be an RPC call with a name")


class TestHandlerRegistry(unittest.TestCase):
    """Verify the HANDLER_REGISTRY structure and stub behavior."""

    EXPECTED_TYPES = {
        "resource_ingestion",
        "deterministic_audit",
        "llm_audit",
        "hybrid_audit",
        "certification_duplicate_audit",
        "certification_semantic_cluster_audit",
        "ai_quality_audit_smoke",
        "question_generation",
        "candidate_promotion",
        "embedding_generation",
        "other",
    }

    def test_all_expected_types_registered(self):
        self.assertEqual(set(HANDLER_REGISTRY.keys()), self.EXPECTED_TYPES)

    def test_all_handlers_are_callable(self):
        for job_type, handler in HANDLER_REGISTRY.items():
            self.assertTrue(callable(handler),
                            f"handler for {job_type!r} must be callable")

    def test_all_stubs_raise_not_implemented(self):
        for job_type, handler in HANDLER_REGISTRY.items():
            with self.assertRaises(NotImplementedHandler,
                                   msg=f"stub for {job_type!r} must raise NotImplementedHandler"):
                handler(
                    job_id="test-id",
                    payload={},
                    checkpoint={},
                    attempt=1,
                    heartbeat_fn=lambda: None,
                )

    def test_no_stub_returns_success(self):
        """No stub may silently return a success result."""
        for job_type, handler in HANDLER_REGISTRY.items():
            try:
                result = handler(
                    job_id="test-id",
                    payload={},
                    checkpoint={},
                    attempt=1,
                    heartbeat_fn=lambda: None,
                )
                self.fail(
                    f"stub for {job_type!r} returned {result!r} instead of raising"
                )
            except NotImplementedHandler:
                pass  # expected

    def test_error_message_contains_job_type(self):
        for job_type, handler in HANDLER_REGISTRY.items():
            try:
                handler(
                    job_id="test-id",
                    payload={},
                    checkpoint={},
                    attempt=1,
                    heartbeat_fn=lambda: None,
                )
            except NotImplementedHandler as exc:
                self.assertIn(
                    job_type, str(exc),
                    f"stub error for {job_type!r} must mention the job type"
                )


class TestWorkerConstruction(unittest.TestCase):
    """Construction-time validation on BackgroundWorker."""

    def test_empty_worker_id_raises(self):
        with self.assertRaises(ValueError):
            BackgroundWorker("", FakeSupabase(), {})

    def test_whitespace_worker_id_raises(self):
        with self.assertRaises(ValueError):
            BackgroundWorker("   ", FakeSupabase(), {})

    def test_valid_worker_id_accepted(self):
        worker = BackgroundWorker("w1", FakeSupabase(), {})
        self.assertEqual(worker.worker_id, "w1")

    def test_worker_id_is_stripped(self):
        worker = BackgroundWorker("  w2  ", FakeSupabase(), {})
        self.assertEqual(worker.worker_id, "w2")


# ===========================================================================
# Phase 8B — Resource ingestion handler
# ===========================================================================

_INGEST_RESPONSE = {
    "resource_version_id": "bbbbbbbb-1111-1111-1111-000000000001",
    "resource_id":         "cccccccc-2222-2222-2222-000000000002",
    "version_number":      1,
    "chunk_count":         3,
}

_VALID_INGEST_PAYLOAD = {
    "resource_id":  "cccccccc-2222-2222-2222-000000000002",
    "content_text": "Salesforce chapter 1 content",
    "content_hash": "abc123def456",
    "created_by":   "ingest-worker@certbound.io",
    "source_url":   "https://help.salesforce.com/article/1",
    "chunks":       [],
}


class TestResourceIngestionHandler(unittest.TestCase):
    """Phase 8B: resource_ingestion handler unit tests (direct, no worker)."""

    def _make_client(self, data=None, error=None):
        fake = FakeSupabase()
        if error:
            fake.set_error_response("ingest_resource_version_v1", error)
        else:
            fake.set_response("ingest_resource_version_v1", [data or _INGEST_RESPONSE])
        return fake

    def test_successful_resource_ingestion(self):
        fake = self._make_client()
        handler = make_resource_ingestion_handler(fake)

        result = handler(
            job_id="j1",
            payload=_VALID_INGEST_PAYLOAD,
            checkpoint={},
            attempt=1,
            heartbeat_fn=lambda: None,
        )

        self.assertEqual(result["resource_version_id"],
                         str(_INGEST_RESPONSE["resource_version_id"]))
        self.assertEqual(result["resource_id"],
                         str(_INGEST_RESPONSE["resource_id"]))
        self.assertEqual(result["version_number"], _INGEST_RESPONSE["version_number"])
        self.assertEqual(result["chunk_count"],    _INGEST_RESPONSE["chunk_count"])

    def test_ingest_rpc_called_with_correct_params(self):
        fake = self._make_client()
        handler = make_resource_ingestion_handler(fake)
        handler(job_id="j1", payload=_VALID_INGEST_PAYLOAD,
                checkpoint={}, attempt=1, heartbeat_fn=lambda: None)

        calls = fake.calls_for("ingest_resource_version_v1")
        self.assertEqual(len(calls), 1)
        params = calls[0]["params"]
        self.assertEqual(params["p_resource_id"],  _VALID_INGEST_PAYLOAD["resource_id"])
        self.assertEqual(params["p_content_text"], _VALID_INGEST_PAYLOAD["content_text"])
        self.assertEqual(params["p_content_hash"], _VALID_INGEST_PAYLOAD["content_hash"])
        self.assertEqual(params["p_created_by"],   _VALID_INGEST_PAYLOAD["created_by"])
        self.assertEqual(params["p_source_url"],   _VALID_INGEST_PAYLOAD["source_url"])

    def test_malformed_payload_missing_resource_id(self):
        fake = self._make_client()
        handler = make_resource_ingestion_handler(fake)
        payload = {k: v for k, v in _VALID_INGEST_PAYLOAD.items() if k != "resource_id"}
        with self.assertRaises(HandlerPayloadError):
            handler(job_id="j1", payload=payload, checkpoint={},
                    attempt=1, heartbeat_fn=lambda: None)
        self.assertNotIn("ingest_resource_version_v1", fake.called_rpc_names)

    def test_malformed_payload_missing_content_text(self):
        fake = self._make_client()
        handler = make_resource_ingestion_handler(fake)
        payload = {**_VALID_INGEST_PAYLOAD, "content_text": ""}
        with self.assertRaises(HandlerPayloadError):
            handler(job_id="j1", payload=payload, checkpoint={},
                    attempt=1, heartbeat_fn=lambda: None)

    def test_malformed_payload_missing_content_hash(self):
        fake = self._make_client()
        handler = make_resource_ingestion_handler(fake)
        payload = {k: v for k, v in _VALID_INGEST_PAYLOAD.items() if k != "content_hash"}
        with self.assertRaises(HandlerPayloadError):
            handler(job_id="j1", payload=payload, checkpoint={},
                    attempt=1, heartbeat_fn=lambda: None)

    def test_malformed_payload_missing_created_by(self):
        fake = self._make_client()
        handler = make_resource_ingestion_handler(fake)
        payload = {**_VALID_INGEST_PAYLOAD, "created_by": None}
        with self.assertRaises(HandlerPayloadError):
            handler(job_id="j1", payload=payload, checkpoint={},
                    attempt=1, heartbeat_fn=lambda: None)

    def test_rpc_error_propagates_as_runtime_error(self):
        fake = self._make_client(error="resource not found")
        handler = make_resource_ingestion_handler(fake)
        with self.assertRaises(RuntimeError) as ctx:
            handler(job_id="j1", payload=_VALID_INGEST_PAYLOAD,
                    checkpoint={}, attempt=1, heartbeat_fn=lambda: None)
        self.assertIn("resource not found", str(ctx.exception))

    def test_no_direct_table_calls(self):
        """Handler must never call client.table()."""
        fake = self._make_client()
        handler = make_resource_ingestion_handler(fake)
        try:
            handler(job_id="j1", payload=_VALID_INGEST_PAYLOAD,
                    checkpoint={}, attempt=1, heartbeat_fn=lambda: None)
        except AttributeError as exc:
            self.fail(f"handler called client.table() directly: {exc}")

    def test_optional_fields_default_when_absent(self):
        """Optional payload fields fall back to None / empty without error."""
        fake = self._make_client()
        handler = make_resource_ingestion_handler(fake)
        minimal = {
            "resource_id":  "cccccccc-2222-2222-2222-000000000002",
            "content_text": "text",
            "content_hash": "hash",
            "created_by":   "worker",
        }
        handler(job_id="j1", payload=minimal, checkpoint={},
                attempt=1, heartbeat_fn=lambda: None)

        params = fake.calls_for("ingest_resource_version_v1")[0]["params"]
        self.assertIsNone(params["p_source_url"])
        self.assertIsNone(params["p_source_external_version"])
        self.assertIsNone(params["p_effective_at"])
        self.assertEqual(params["p_metadata"], {})
        self.assertEqual(params["p_chunks"], [])


# ===========================================================================
# Phase 8C — Candidate promotion handler
# ===========================================================================

_PROMOTE_RESPONSE = {
    "candidate_id":        "dddddddd-3333-3333-3333-000000000003",
    "question_version_id": "eeeeeeee-4444-4444-4444-000000000004",
    "question_id":         42,
    "version_number":      2,
}

_VALID_PROMOTE_PAYLOAD = {
    "candidate_id": "dddddddd-3333-3333-3333-000000000003",
    "actor_email":  "reviewer@certbound.io",
    "reason":       "passed automated audit",
    "event_data":   {"audit_run_id": "ffff"},
}


class TestCandidatePromotionHandler(unittest.TestCase):
    """Phase 8C: candidate_promotion handler unit tests (direct, no worker)."""

    def _make_client(self, data=None, error=None):
        fake = FakeSupabase()
        if error:
            fake.set_error_response("promote_question_candidate_v1", error)
        else:
            fake.set_response("promote_question_candidate_v1", [data or _PROMOTE_RESPONSE])
        return fake

    def test_successful_candidate_promotion(self):
        fake = self._make_client()
        handler = make_candidate_promotion_handler(fake)

        result = handler(
            job_id="j2",
            payload=_VALID_PROMOTE_PAYLOAD,
            checkpoint={},
            attempt=1,
            heartbeat_fn=lambda: None,
        )

        self.assertEqual(result["candidate_id"],
                         str(_PROMOTE_RESPONSE["candidate_id"]))
        self.assertEqual(result["question_version_id"],
                         str(_PROMOTE_RESPONSE["question_version_id"]))
        self.assertEqual(result["question_id"],    _PROMOTE_RESPONSE["question_id"])
        self.assertEqual(result["version_number"], _PROMOTE_RESPONSE["version_number"])

    def test_promotion_rpc_called_with_correct_params(self):
        fake = self._make_client()
        handler = make_candidate_promotion_handler(fake)
        handler(job_id="j2", payload=_VALID_PROMOTE_PAYLOAD,
                checkpoint={}, attempt=1, heartbeat_fn=lambda: None)

        calls = fake.calls_for("promote_question_candidate_v1")
        self.assertEqual(len(calls), 1)
        params = calls[0]["params"]
        self.assertEqual(params["p_candidate_id"], _VALID_PROMOTE_PAYLOAD["candidate_id"])
        self.assertEqual(params["p_actor_email"],  _VALID_PROMOTE_PAYLOAD["actor_email"])
        self.assertEqual(params["p_reason"],       _VALID_PROMOTE_PAYLOAD["reason"])
        self.assertEqual(params["p_event_data"],   _VALID_PROMOTE_PAYLOAD["event_data"])

    def test_malformed_payload_missing_candidate_id(self):
        fake = self._make_client()
        handler = make_candidate_promotion_handler(fake)
        payload = {k: v for k, v in _VALID_PROMOTE_PAYLOAD.items() if k != "candidate_id"}
        with self.assertRaises(HandlerPayloadError):
            handler(job_id="j2", payload=payload, checkpoint={},
                    attempt=1, heartbeat_fn=lambda: None)
        self.assertNotIn("promote_question_candidate_v1", fake.called_rpc_names)

    def test_malformed_payload_missing_actor_email(self):
        fake = self._make_client()
        handler = make_candidate_promotion_handler(fake)
        payload = {**_VALID_PROMOTE_PAYLOAD, "actor_email": ""}
        with self.assertRaises(HandlerPayloadError):
            handler(job_id="j2", payload=payload, checkpoint={},
                    attempt=1, heartbeat_fn=lambda: None)

    def test_malformed_payload_missing_reason(self):
        fake = self._make_client()
        handler = make_candidate_promotion_handler(fake)
        payload = {k: v for k, v in _VALID_PROMOTE_PAYLOAD.items() if k != "reason"}
        with self.assertRaises(HandlerPayloadError):
            handler(job_id="j2", payload=payload, checkpoint={},
                    attempt=1, heartbeat_fn=lambda: None)

    def test_rpc_error_propagates_as_runtime_error(self):
        fake = self._make_client(error="candidate not found")
        handler = make_candidate_promotion_handler(fake)
        with self.assertRaises(RuntimeError) as ctx:
            handler(job_id="j2", payload=_VALID_PROMOTE_PAYLOAD,
                    checkpoint={}, attempt=1, heartbeat_fn=lambda: None)
        self.assertIn("candidate not found", str(ctx.exception))

    def test_no_direct_table_calls(self):
        """Handler must never call client.table()."""
        fake = self._make_client()
        handler = make_candidate_promotion_handler(fake)
        try:
            handler(job_id="j2", payload=_VALID_PROMOTE_PAYLOAD,
                    checkpoint={}, attempt=1, heartbeat_fn=lambda: None)
        except AttributeError as exc:
            self.fail(f"handler called client.table() directly: {exc}")

    def test_optional_event_data_defaults_to_empty(self):
        fake = self._make_client()
        handler = make_candidate_promotion_handler(fake)
        payload = {k: v for k, v in _VALID_PROMOTE_PAYLOAD.items() if k != "event_data"}
        handler(job_id="j2", payload=payload, checkpoint={},
                attempt=1, heartbeat_fn=lambda: None)
        params = fake.calls_for("promote_question_candidate_v1")[0]["params"]
        self.assertEqual(params["p_event_data"], {})


# ===========================================================================
# Integration: worker dispatches to real handlers, result forwarded
# ===========================================================================

class TestWorkerHandlerIntegration(unittest.TestCase):
    """End-to-end: worker + real handler + fake Supabase client."""

    def _make_ingest_job(self):
        return {
            **_SAMPLE_JOB,
            "job_type": "resource_ingestion",
            "payload":  _VALID_INGEST_PAYLOAD,
        }

    def _make_promote_job(self):
        return {
            **_SAMPLE_JOB,
            "job_id":   "ffffffff-9999-9999-9999-000000000009",
            "job_type": "candidate_promotion",
            "payload":  _VALID_PROMOTE_PAYLOAD,
        }

    def _setup_fake(self, job: dict, domain_rpc: str, domain_response: list) -> FakeSupabase:
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response("heartbeat_background_job_v1", [
            {"job_id": job["job_id"], "job_status": "running",
             "lease_expires_at": "2099-01-01T00:00:00+00:00",
             "heartbeat_at": "2099-01-01T00:00:00+00:00"},
        ])
        fake.set_response(domain_rpc, domain_response)
        fake.set_response("complete_background_job_v1", [
            {"job_id": job["job_id"], "job_status": "completed",
             "completed_at": "2099-01-01T00:00:00+00:00"},
        ])
        return fake

    # --- resource_ingestion integration ---

    def test_ingest_result_forwarded_to_complete_rpc(self):
        """The result returned by the handler is passed to complete_background_job_v1."""
        job = self._make_ingest_job()
        fake = self._setup_fake(job, "ingest_resource_version_v1",
                                [_INGEST_RESPONSE])
        worker = BackgroundWorker(
            worker_id="integration-worker",
            client=fake,
            handlers=build_handler_registry(fake),
            sleep_interval=0.0,
        )
        worker.run_once()

        complete_calls = fake.calls_for("complete_background_job_v1")
        self.assertEqual(len(complete_calls), 1, "complete must be called once")

        p_result = complete_calls[0]["params"]["p_result"]
        self.assertEqual(p_result["version_number"], _INGEST_RESPONSE["version_number"])
        self.assertEqual(p_result["chunk_count"],    _INGEST_RESPONSE["chunk_count"])

    def test_ingest_no_table_writes_via_worker(self):
        job = self._make_ingest_job()
        fake = self._setup_fake(job, "ingest_resource_version_v1", [_INGEST_RESPONSE])
        worker = BackgroundWorker(
            worker_id="integration-worker",
            client=fake,
            handlers=build_handler_registry(fake),
            sleep_interval=0.0,
        )
        try:
            worker.run_once()
        except AttributeError as exc:
            self.fail(f"worker or handler called client.table() directly: {exc}")

    def test_ingest_rpc_error_causes_fail_rpc(self):
        """When the domain RPC errors, the worker calls fail_background_job_v1."""
        job = self._make_ingest_job()
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response("heartbeat_background_job_v1", [
            {"job_id": job["job_id"], "job_status": "running",
             "lease_expires_at": "2099-01-01T00:00:00+00:00",
             "heartbeat_at": "2099-01-01T00:00:00+00:00"},
        ])
        fake.set_error_response("ingest_resource_version_v1", "connection timeout")
        fake.set_response("fail_background_job_v1", [
            {"job_id": job["job_id"], "job_status": "pending",
             "available_at": None, "completed_at": None},
        ])

        worker = BackgroundWorker(
            worker_id="integration-worker",
            client=fake,
            handlers=build_handler_registry(fake),
            sleep_interval=0.0,
        )
        worker.run_once()

        self.assertIn("fail_background_job_v1", fake.called_rpc_names)
        self.assertNotIn("complete_background_job_v1", fake.called_rpc_names)

    # --- candidate_promotion integration ---

    def test_promote_result_forwarded_to_complete_rpc(self):
        job = self._make_promote_job()
        fake = self._setup_fake(job, "promote_question_candidate_v1",
                                [_PROMOTE_RESPONSE])
        worker = BackgroundWorker(
            worker_id="integration-worker",
            client=fake,
            handlers=build_handler_registry(fake),
            sleep_interval=0.0,
        )
        worker.run_once()

        complete_calls = fake.calls_for("complete_background_job_v1")
        self.assertEqual(len(complete_calls), 1)

        p_result = complete_calls[0]["params"]["p_result"]
        self.assertEqual(p_result["question_id"],    _PROMOTE_RESPONSE["question_id"])
        self.assertEqual(p_result["version_number"], _PROMOTE_RESPONSE["version_number"])

    def test_promote_rpc_error_causes_fail_rpc(self):
        job = self._make_promote_job()
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response("heartbeat_background_job_v1", [
            {"job_id": job["job_id"], "job_status": "running",
             "lease_expires_at": "2099-01-01T00:00:00+00:00",
             "heartbeat_at": "2099-01-01T00:00:00+00:00"},
        ])
        fake.set_error_response("promote_question_candidate_v1", "candidate not approved")
        fake.set_response("fail_background_job_v1", [
            {"job_id": job["job_id"], "job_status": "pending",
             "available_at": None, "completed_at": None},
        ])
        worker = BackgroundWorker(
            worker_id="integration-worker",
            client=fake,
            handlers=build_handler_registry(fake),
            sleep_interval=0.0,
        )
        worker.run_once()

        self.assertIn("fail_background_job_v1", fake.called_rpc_names)
        self.assertNotIn("complete_background_job_v1", fake.called_rpc_names)

    # --- remaining stubs still not implemented ---

    def test_remaining_handlers_still_not_implemented(self):
        """build_handler_registry must leave non-implemented types as stubs."""
        fake = FakeSupabase()
        registry = build_handler_registry(fake)

        # deterministic_audit, llm_audit, and hybrid_audit are now implemented.
        # Only these remain as NotImplementedHandler stubs.
        still_stubbed = [
            "question_generation",
            "embedding_generation",
            "other",
        ]
        for job_type in still_stubbed:
            handler = registry[job_type]
            with self.assertRaises(NotImplementedHandler,
                                   msg=f"{job_type} must still raise NotImplementedHandler"):
                handler(job_id="x", payload={}, checkpoint={},
                        attempt=1, heartbeat_fn=lambda: None)

    def test_implemented_types_are_not_stubs(self):
        """resource_ingestion, candidate_promotion, deterministic_audit,
        llm_audit, and hybrid_audit must not raise NotImplementedHandler."""
        _AUDIT_RUN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
        fake = FakeSupabase()
        fake.set_response("ingest_resource_version_v1", [_INGEST_RESPONSE])
        fake.set_response("promote_question_candidate_v1", [_PROMOTE_RESPONSE])
        fake.set_response("create_audit_run_v1", [
            {"audit_run_id": _AUDIT_RUN_ID, "run_status": "pending"},
        ])
        fake.set_response("complete_audit_run_v1", [
            {"audit_run_id": _AUDIT_RUN_ID, "run_status": "completed",
             "finding_count": 0, "evidence_count": 0},
        ])
        registry = build_handler_registry(fake)

        _VALID_AUDIT_PAYLOAD = {
            "target_question_version_id": "bbbbbbbb-0000-0000-0000-000000000001",
            "created_by": "audit-worker@certbound.io",
            "question": {
                "question_text": "What is 2+2?",
                "explanation":   "Elementary arithmetic.",
                "question_type": "single",
                "select_count":  1,
                "options": [
                    {"option_label": "A", "option_text": "4",
                     "is_correct": True,  "display_order": 1},
                    {"option_label": "B", "option_text": "5",
                     "is_correct": False, "display_order": 2},
                ],
            },
        }

        _VALID_LLM_PAYLOAD = {
            "target_question_version_id": "bbbbbbbb-0000-0000-0000-000000000001",
            "created_by":     "audit-worker@certbound.io",
            "model_name":     "gpt-4o",
            "prompt_version": "v1.0.0",
            "system_prompt":  "You are an expert Salesforce question auditor.",
            "user_prompt":    "Audit this question: What is 2+2?",
        }

        _VALID_HYBRID_PAYLOAD = {
            "target_question_version_id": "bbbbbbbb-0000-0000-0000-000000000001",
            "created_by":      "audit-worker@certbound.io",
            "model_name":      "gpt-4o",
            "prompt_version":  "v1.0.0",
            "ruleset_version": "1.0.0",
            "system_prompt":   "You are an expert Salesforce question auditor.",
            "user_prompt":     "Audit this question: What is 2+2?",
            "question": {
                "question_text": "What is 2+2?",
                "explanation":   "Elementary arithmetic.",
                "question_type": "single",
                "select_count":  1,
                "options": [
                    {"option_label": "A", "option_text": "4",
                     "is_correct": True,  "display_order": 1},
                    {"option_label": "B", "option_text": "5",
                     "is_correct": False, "display_order": 2},
                ],
            },
        }

        for job_type, payload in [
            ("resource_ingestion",  _VALID_INGEST_PAYLOAD),
            ("candidate_promotion", _VALID_PROMOTE_PAYLOAD),
            ("deterministic_audit", _VALID_AUDIT_PAYLOAD),
            ("llm_audit",           _VALID_LLM_PAYLOAD),
            ("hybrid_audit",        _VALID_HYBRID_PAYLOAD),
        ]:
            try:
                registry[job_type](
                    job_id="x", payload=payload, checkpoint={},
                    attempt=1, heartbeat_fn=lambda: None,
                )
            except NotImplementedHandler:
                self.fail(f"{job_type} raised NotImplementedHandler — must be a real handler")
            except Exception:
                # Any other exception (MissingProviderError, RPC error, etc.)
                # is acceptable; only NotImplementedHandler signals "not yet
                # implemented".
                pass


# ===========================================================================
# T8  Automatic heartbeat during long-running handlers
# ===========================================================================

_HEARTBEAT_RESPONSE = [
    {
        "job_id": _SAMPLE_JOB["job_id"],
        "job_status": "running",
        "lease_expires_at": "2099-01-01T00:00:00+00:00",
        "heartbeat_at": "2099-01-01T00:00:00+00:00",
    }
]


def _fake_for_heartbeat_tests(handler: Callable) -> "FakeSupabase":
    """Build a FakeSupabase wired for a single job dispatch."""
    fake = FakeSupabase()
    fake.set_response("recover_expired_background_jobs_v1",
                      [{"recovered_count": 0, "dead_letter_count": 0}])
    fake.set_response("claim_background_job_v1", [_SAMPLE_JOB])
    fake.set_response("heartbeat_background_job_v1", _HEARTBEAT_RESPONSE)
    fake.set_response("complete_background_job_v1",
                      [{"job_id": _SAMPLE_JOB["job_id"],
                        "job_status": "completed",
                        "completed_at": "2099-01-01T00:00:00+00:00"}])
    fake.set_response("fail_background_job_v1",
                      [{"job_id": _SAMPLE_JOB["job_id"],
                        "job_status": "pending",
                        "available_at": "2099-01-01T00:05:00+00:00",
                        "completed_at": None}])
    return fake


class TestAutoHeartbeat(unittest.TestCase):
    """T8: Automatic periodic heartbeat while a handler is executing."""

    # Use a very short lease so the auto-heartbeat fires quickly in tests.
    _LEASE = 3  # seconds
    _JOB_ID = _SAMPLE_JOB["job_id"]

    def _make_worker(self, fake: "FakeSupabase", handler: Callable) -> BackgroundWorker:
        return BackgroundWorker(
            worker_id="heartbeat-test-worker",
            client=fake,
            handlers={_SAMPLE_JOB["job_type"]: handler},
            lease_seconds=self._LEASE,
            sleep_interval=0.0,
        )

    def _heartbeat_count(self, fake: "FakeSupabase") -> int:
        return sum(
            1 for c in fake.rpc_calls
            if c["name"] == "heartbeat_background_job_v1"
        )

    # ------------------------------------------------------------------

    def test_heartbeats_continue_during_long_running_handler(self):
        """Auto-heartbeat fires at least once while the handler sleeps."""
        fake = _fake_for_heartbeat_tests(lambda **_kw: {"ok": True})

        def slow_handler(job_id, payload, checkpoint, attempt, heartbeat_fn):
            # Sleep for ~2 intervals so at least one auto-heartbeat fires.
            time.sleep(self._LEASE / 3.0 * 2.2)
            return {"ok": True}

        worker = self._make_worker(fake, slow_handler)
        worker.run_once()

        heartbeat_calls = self._heartbeat_count(fake)
        # Initial pre-handler heartbeat (1) + at least one auto-heartbeat (1).
        self.assertGreaterEqual(
            heartbeat_calls, 2,
            f"expected ≥2 heartbeat RPCs for a long handler, got {heartbeat_calls}",
        )

    def test_heartbeat_stops_after_success(self):
        """No extra heartbeat is sent after the job completes."""
        fake = _fake_for_heartbeat_tests(lambda **_kw: {"ok": True})

        def fast_handler(job_id, payload, checkpoint, attempt, heartbeat_fn):
            return {"ok": True}

        worker = self._make_worker(fake, fast_handler)
        worker.run_once()

        # Record heartbeat count immediately after completion.
        count_after = self._heartbeat_count(fake)

        # Sleep longer than two auto-heartbeat intervals to give the thread
        # time to fire spuriously if it hasn't stopped.
        time.sleep(self._LEASE / 3.0 * 2.5)

        count_later = self._heartbeat_count(fake)
        self.assertEqual(
            count_after,
            count_later,
            "heartbeat thread must stop after handler success; "
            f"count grew from {count_after} to {count_later}",
        )

    def test_heartbeat_stops_after_handler_failure(self):
        """Heartbeat thread stops even when the handler raises."""
        fake = _fake_for_heartbeat_tests(lambda **_kw: {})

        def failing_handler(job_id, payload, checkpoint, attempt, heartbeat_fn):
            raise ValueError("deliberate test failure")

        worker = self._make_worker(fake, failing_handler)
        worker.run_once()

        count_after = self._heartbeat_count(fake)
        time.sleep(self._LEASE / 3.0 * 2.5)
        count_later = self._heartbeat_count(fake)

        self.assertEqual(
            count_after,
            count_later,
            "heartbeat thread must stop after handler failure; "
            f"count grew from {count_after} to {count_later}",
        )

    def test_handler_exception_still_reaches_fail_path(self):
        """A handler exception must result in fail_background_job_v1 being called."""
        fake = _fake_for_heartbeat_tests(lambda **_kw: {})

        def failing_handler(job_id, payload, checkpoint, attempt, heartbeat_fn):
            raise RuntimeError("something went wrong")

        worker = self._make_worker(fake, failing_handler)
        worker.run_once()

        self.assertIn(
            "fail_background_job_v1",
            fake.called_rpc_names,
            "fail_background_job_v1 must be called when the handler raises",
        )
        self.assertNotIn(
            "complete_background_job_v1",
            fake.called_rpc_names,
            "complete_background_job_v1 must NOT be called when the handler raises",
        )
        fail_call = next(
            c for c in fake.rpc_calls if c["name"] == "fail_background_job_v1"
        )
        self.assertIn("something went wrong", fail_call["params"]["p_error_message"])

    def test_no_extra_heartbeat_after_completion(self):
        """complete_background_job_v1 is called exactly once, not again after."""
        fake = _fake_for_heartbeat_tests(lambda **_kw: {})

        def fast_handler(job_id, payload, checkpoint, attempt, heartbeat_fn):
            return {"result": "done"}

        worker = self._make_worker(fake, fast_handler)
        worker.run_once()

        # Give any rogue background thread time to fire.
        time.sleep(self._LEASE / 3.0 * 2.5)

        complete_calls = sum(
            1 for c in fake.rpc_calls
            if c["name"] == "complete_background_job_v1"
        )
        self.assertEqual(
            complete_calls, 1,
            f"complete_background_job_v1 must be called exactly once, called {complete_calls} times",
        )


class TestAiQualityAuditBackgroundWorkerWait(unittest.TestCase):
    """WAIT coordination inside the ai_quality handler must not fail the background job."""

    _AUDIT_RUN_ID = "aaaaaaaa-0000-0000-0000-000000000002"
    _QUESTION_VERSION_ID = "cccccccc-0000-0000-0000-000000000001"

    class _CombinedFake(FakeSupabase):
        def __init__(self, audit_client):
            super().__init__()
            self._audit = audit_client

        def table(self, name: str):
            return self._audit.table(name)

        def rpc(self, name: str, params: Optional[dict] = None) -> FakeRpcBuilder:
            if name in (
                "claim_ai_quality_audit_pass_v1",
                "record_audit_pass_result_v1",
                "persist_audit_run_dispute_trigger_v1",
                "complete_ai_quality_audit_run_v1",
            ):
                return self._audit.rpc(name, params or {})
            return super().rpc(name, params)

    def _make_ai_quality_job(self) -> dict:
        return {
            **_SAMPLE_JOB,
            "job_id": "bbbbbbbb-0000-0000-0000-000000000002",
            "job_type": "ai_quality_audit_smoke",
            "payload": {
                "audit_run_id": self._AUDIT_RUN_ID,
                "question_version_id": self._QUESTION_VERSION_ID,
            },
            "attempt_count": 1,
            "max_attempts": 3,
        }

    def test_wait_coordination_completes_job_without_fail_rpc(self):
        from tests.test_ai_quality_audit_worker import (
            MIN_BLIND_CONTEXT,
            OrchestrationFakeSupabase,
            _claim,
        )
        from workers.ai_quality_audit_worker import AiQualityAuditProviders
        from workers.llm_providers import LlmResponse

        audit_client = OrchestrationFakeSupabase()
        audit_client.enqueue_claims(
            _claim("WAIT"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        fake = self._CombinedFake(audit_client)
        job = self._make_ai_quality_job()
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response(
            "heartbeat_background_job_v1",
            [
                {
                    "job_id": job["job_id"],
                    "job_status": "running",
                    "lease_expires_at": "2099-01-01T00:00:00+00:00",
                    "heartbeat_at": "2099-01-01T00:00:00+00:00",
                }
            ],
        )
        fake.set_response(
            "complete_background_job_v1",
            [
                {
                    "job_id": job["job_id"],
                    "job_status": "completed",
                    "completed_at": "2099-01-01T00:00:00+00:00",
                }
            ],
        )

        def _llm_response(parsed):
            return LlmResponse(
                parsed_response=parsed,
                input_tokens=1,
                output_tokens=1,
            )

        providers = AiQualityAuditProviders(
            primary=lambda **_: _llm_response({"selected_option_labels": ["A"]}),
            dispute=lambda **_: _llm_response({}),
        )

        handlers = build_handler_registry(fake, ai_quality_providers=providers)
        worker = BackgroundWorker(
            worker_id="test-worker-1",
            client=fake,
            handlers=handlers,
            lease_seconds=60,
            sleep_interval=0.0,
        )

        with patch(
            "workers.ai_quality_audit_worker.load_blind_audit_context",
            return_value=dict(MIN_BLIND_CONTEXT),
        ), patch(
            "workers.ai_quality_audit_worker.load_comparison_audit_context",
            return_value={},
        ), patch("workers.ai_quality_audit_worker.time.sleep"):
            worker.run_once()

        self.assertIn("complete_background_job_v1", fake.called_rpc_names)
        self.assertNotIn("fail_background_job_v1", fake.called_rpc_names)
        complete_calls = fake.calls_for("complete_background_job_v1")
        self.assertEqual(len(complete_calls), 1)
        self.assertEqual(
            complete_calls[0]["params"]["p_result"]["run_status"],
            "inconclusive",
        )


if __name__ == "__main__":
    unittest.main()
