"""
Focused tests for V49-AUDIT-WORKER-03 (scripts/v49_run_single_audit_job.py).

Hermetic: no Supabase connection, no network, no real LLM provider calls.
The runner reads one background_jobs row for identifiers only, then calls
process_ai_quality_audit_job directly. It must never call background queue
lifecycle RPCs or mutate the outer background_jobs row.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Optional
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.v49_run_single_audit_job import (
    SingleJobRunnerError,
    TARGET_JOB_TYPE,
    _FORBIDDEN_QUEUE_RPCS,
    build_plan,
    execute_single_job,
    main,
)
from tests.test_ai_quality_audit_worker import (
    MIN_BLIND_CONTEXT,
    MIN_COMPARISON_CONTEXT,
    OrchestrationFakeSupabase,
    _claim,
    _pass_a_result,
    _pass_b_result,
    _wire_normal_no_dispute_tables,
)
from workers.ai_quality_audit_worker import AiQualityAuditProviders
from workers.llm_providers import LlmResponse

_TARGET_JOB_ID = "dddddddd-0000-0000-0000-000000000001"
_LEGACY_JOB_ID = "eeeeeeee-0000-0000-0000-000000000009"
_RUN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_QVID = "cccccccc-0000-0000-0000-000000000001"


class _RpcResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


class _JobsTableQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self._filters: List[tuple] = []
        self._limit: Optional[int] = None
        self._select_fields: Optional[str] = None

    def select(self, fields: str):
        self._select_fields = fields
        return self

    def eq(self, field: str, value: object):
        self._filters.append((field, value))
        return self

    def limit(self, count: int):
        self._limit = count
        return self

    def execute(self):
        rows = [dict(r) for r in self._rows]
        for field, value in self._filters:
            rows = [r for r in rows if r.get(field) == value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return _RpcResult(rows)


class _V49ReadOnlyFake:
    """Read-only background_jobs table + audit RPC delegation only."""

    def __init__(
        self,
        audit_client: OrchestrationFakeSupabase,
        jobs: List[Dict[str, Any]],
    ):
        self._audit = audit_client
        self.background_jobs = jobs
        self.rpc_calls: List[tuple] = []
        self.table_calls: List[tuple] = []

    def table(self, name: str):
        self.table_calls.append((name,))
        if name == "background_jobs":
            return _JobsTableQuery(self.background_jobs)
        return self._audit.table(name)

    def rpc(self, name: str, params: Optional[dict] = None):
        params = dict(params or {})
        self.rpc_calls.append((name, params))
        if name in _FORBIDDEN_QUEUE_RPCS:
            raise AssertionError(f"forbidden queue RPC called: {name}")
        return self._audit.rpc(name, params)


def _job_row(
    job_id: str,
    *,
    job_type: str = TARGET_JOB_TYPE,
    job_status: str = "pending",
    audit_run_id: str = _RUN_ID,
    question_version_id: str = _QVID,
    payload: Optional[dict] = None,
    attempt_count: int = 0,
) -> Dict[str, Any]:
    return {
        "id": job_id,
        "job_type": job_type,
        "job_status": job_status,
        "attempt_count": attempt_count,
        "max_attempts": 3,
        "payload": payload
        if payload is not None
        else {
            "audit_run_id": audit_run_id,
            "question_version_id": question_version_id,
        },
        "metadata": {"secret_marker": "SHOULD-NOT-PRINT"},
    }


def _happy_path_audit_client() -> OrchestrationFakeSupabase:
    client = OrchestrationFakeSupabase()
    client.enqueue_claims(
        _claim("EXECUTE_PASS_A", "A"),
        _claim("EXECUTE_PASS_B", "B"),
        _claim("SKIP_PASS_C", "C"),
        _claim("RUN_READY_TO_COMPLETE"),
    )
    _wire_normal_no_dispute_tables(client)
    return client


def _happy_path_providers() -> AiQualityAuditProviders:
    def primary(**kwargs):
        pass_code = (kwargs.get("metadata") or {}).get("pass_code")
        body = _pass_a_result() if pass_code == "A" else _pass_b_result()
        return LlmResponse(parsed_response=body, input_tokens=1, output_tokens=1)

    def dispute(**_kwargs):
        raise AssertionError("dispute provider must not be called on no-dispute path")

    return AiQualityAuditProviders(primary=primary, dispute=dispute)


def _context_patches():
    return (
        patch(
            "workers.ai_quality_audit_worker.load_blind_audit_context",
            return_value=dict(MIN_BLIND_CONTEXT),
        ),
        patch(
            "workers.ai_quality_audit_worker.load_comparison_audit_context",
            return_value=dict(MIN_COMPARISON_CONTEXT),
        ),
    )


def _assert_no_queue_rpc(calls: List[tuple]) -> None:
    for name, _params in calls:
        self_msg = f"unexpected queue RPC {name!r}"
        assert name not in _FORBIDDEN_QUEUE_RPCS, self_msg


class PreflightTests(unittest.TestCase):
    def test_pending_job_is_ready_to_execute(self):
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [_job_row(_TARGET_JOB_ID)])
        plan = build_plan(client, _TARGET_JOB_ID)
        self.assertTrue(plan.exists)
        self.assertTrue(plan.ready_to_execute)
        self.assertEqual(plan.audit_run_id, _RUN_ID.lower())
        self.assertEqual(plan.question_version_id, _QVID.lower())
        self.assertEqual(len(client.table_calls), 1)

    def test_missing_job_fails_closed(self):
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [])
        plan = build_plan(client, _TARGET_JOB_ID)
        self.assertFalse(plan.exists)
        self.assertFalse(plan.ready_to_execute)

    def test_non_smoke_job_type_fails_closed(self):
        row = _job_row(_TARGET_JOB_ID, job_type="llm_audit")
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [row])
        plan = build_plan(client, _TARGET_JOB_ID)
        self.assertFalse(plan.ready_to_execute)
        self.assertIn("approved audit path", plan.blocking_reason or "")

    def test_non_pending_job_fails_before_audit_layer(self):
        row = _job_row(_TARGET_JOB_ID, job_status="completed")
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [row])
        plan = build_plan(client, _TARGET_JOB_ID)
        self.assertFalse(plan.ready_to_execute)
        self.assertIn("pending", plan.blocking_reason or "")
        with self.assertRaises(SingleJobRunnerError):
            execute_single_job(
                client,
                plan,
                worker_id="w1",
                lease_seconds=300,
                schema_version="v48.1",
                ai_quality_providers=_happy_path_providers(),
            )
        self.assertEqual(client.rpc_calls, [])

    def test_malformed_payload_fails_closed(self):
        row = _job_row(_TARGET_JOB_ID, payload={"audit_run_id": _RUN_ID})
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [row])
        plan = build_plan(client, _TARGET_JOB_ID)
        self.assertFalse(plan.ready_to_execute)
        self.assertIn("audit_run_id and question_version_id", plan.blocking_reason or "")

    def test_invalid_uuid_in_payload_fails_closed(self):
        row = _job_row(
            _TARGET_JOB_ID,
            payload={
                "audit_run_id": "not-a-uuid",
                "question_version_id": _QVID,
            },
        )
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [row])
        plan = build_plan(client, _TARGET_JOB_ID)
        self.assertFalse(plan.ready_to_execute)
        self.assertIn("UUID", plan.blocking_reason or "")

    def test_legacy_job_in_queue_is_never_read(self):
        legacy = _job_row(_LEGACY_JOB_ID)
        target = _job_row(_TARGET_JOB_ID)
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [legacy, target])
        plan = build_plan(client, _TARGET_JOB_ID)
        self.assertTrue(plan.ready_to_execute)
        self.assertEqual(len(client.table_calls), 1)


class ExecuteSingleJobTests(unittest.TestCase):
    def test_execute_calls_process_once_with_extracted_ids(self):
        target = _job_row(_TARGET_JOB_ID)
        audit_client = _happy_path_audit_client()
        client = _V49ReadOnlyFake(audit_client, [target])
        plan = build_plan(client, _TARGET_JOB_ID)
        before = copy.deepcopy(target)

        with _context_patches()[0], _context_patches()[1]:
            result = execute_single_job(
                client,
                plan,
                worker_id="v49-test-worker",
                lease_seconds=300,
                schema_version="v48.1",
                ai_quality_providers=_happy_path_providers(),
            )

        self.assertEqual(result.run_status, "completed")
        self.assertTrue(result.audit_execution_completed)
        self.assertEqual(result.audit_run_id, _RUN_ID.lower())
        self.assertEqual(result.question_version_id, _QVID.lower())
        _assert_no_queue_rpc(client.rpc_calls)
        self.assertGreaterEqual(
            sum(1 for name, _ in client.rpc_calls if name == "claim_ai_quality_audit_pass_v1"),
            1,
        )
        self.assertEqual(len(audit_client.complete_calls), 1)
        self.assertEqual(target, before)

    def test_no_background_queue_rpc_is_called(self):
        target = _job_row(_TARGET_JOB_ID)
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [target])
        plan = build_plan(client, _TARGET_JOB_ID)

        with _context_patches()[0], _context_patches()[1]:
            execute_single_job(
                client,
                plan,
                worker_id="w1",
                lease_seconds=300,
                schema_version="v48.1",
                ai_quality_providers=_happy_path_providers(),
            )

        _assert_no_queue_rpc(client.rpc_calls)

    def test_outer_background_job_remains_unchanged(self):
        target = _job_row(_TARGET_JOB_ID)
        snapshot = copy.deepcopy(target)
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [target])
        plan = build_plan(client, _TARGET_JOB_ID)

        with _context_patches()[0], _context_patches()[1]:
            execute_single_job(
                client,
                plan,
                worker_id="w1",
                lease_seconds=300,
                schema_version="v48.1",
                ai_quality_providers=_happy_path_providers(),
            )

        self.assertEqual(target, snapshot)

    def test_completed_audit_run_does_not_duplicate_on_second_execute(self):
        target = _job_row(_TARGET_JOB_ID)
        audit_client = _happy_path_audit_client()
        client = _V49ReadOnlyFake(audit_client, [target])
        plan = build_plan(client, _TARGET_JOB_ID)

        with _context_patches()[0], _context_patches()[1]:
            first = execute_single_job(
                client,
                plan,
                worker_id="w1",
                lease_seconds=300,
                schema_version="v48.1",
                ai_quality_providers=_happy_path_providers(),
            )
            audit_client.enqueue_claims(_claim("RUN_COMPLETE", run_status="completed"))
            second = execute_single_job(
                client,
                plan,
                worker_id="w1",
                lease_seconds=300,
                schema_version="v48.1",
                ai_quality_providers=_happy_path_providers(),
            )

        self.assertEqual(first.run_status, "completed")
        self.assertEqual(second.run_status, "completed")
        self.assertEqual(len(audit_client.complete_calls), 1)

    def test_execution_failure_does_not_report_completed(self):
        target = _job_row(_TARGET_JOB_ID)
        audit_client = OrchestrationFakeSupabase()
        audit_client.enqueue_claims(_claim("EXECUTE_PASS_A", "A"))
        client = _V49ReadOnlyFake(audit_client, [target])
        plan = build_plan(client, _TARGET_JOB_ID)

        def exploding_primary(**_kwargs):
            raise RuntimeError("simulated provider outage")

        providers = AiQualityAuditProviders(
            primary=exploding_primary,
            dispute=exploding_primary,
        )

        with _context_patches()[0], _context_patches()[1]:
            with self.assertRaises(RuntimeError):
                execute_single_job(
                    client,
                    plan,
                    worker_id="w1",
                    lease_seconds=300,
                    schema_version="v48.1",
                    ai_quality_providers=providers,
                )

        _assert_no_queue_rpc(client.rpc_calls)
        self.assertEqual(len(audit_client.complete_calls), 0)
        self.assertEqual(target["job_status"], "pending")


def _script_source() -> str:
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "v49_run_single_audit_job.py",
    )
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _script_source_excluding_module_docstring() -> str:
    source = _script_source()
    tree = ast.parse(source)
    docstring = ast.get_docstring(tree)
    if docstring:
        return source.replace(docstring, "", 1)
    return source


class NoV48HybridImportTests(unittest.TestCase):
    _FORBIDDEN_SUBSTRINGS = (
        "hybrid_question_match_v2",
        "ai_quality_audit_hybrid_replay",
        "ai_quality_audit_semantic",
        "ai_quality_audit_shadow",
        "qualified_v2",
    )

    def test_script_source_contains_no_forbidden_v48_hybrid_references(self):
        code = _script_source_excluding_module_docstring()
        for forbidden in self._FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(forbidden, code)

    def test_script_imports_no_v48_hybrid_module(self):
        tree = ast.parse(_script_source())
        imported_names: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.append(node.module)
        forbidden_modules = (
            "ai_quality_audit_hybrid_replay",
            "ai_quality_audit_semantic",
            "ai_quality_audit_shadow",
        )
        for name in imported_names:
            for forbidden in forbidden_modules:
                self.assertNotIn(forbidden, name)

    def test_script_never_imports_background_worker_class(self):
        tree = ast.parse(_script_source())
        imported_from_workers = [
            node.names
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "workers.background_worker"
        ]
        for names in imported_from_workers:
            imported = {alias.name for alias in names}
            self.assertNotIn("BackgroundWorker", imported)


class CliDryRunTests(unittest.TestCase):
    def test_dry_run_makes_no_audit_rpc_or_writes(self):
        target = _job_row(_TARGET_JOB_ID)
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [target])

        buf = io.StringIO()
        with patch(
            "scripts.v49_run_single_audit_job.build_supabase_client",
            return_value=client,
        ), redirect_stdout(buf):
            exit_code = main(["--job-id", _TARGET_JOB_ID])

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.rpc_calls, [])
        output = buf.getvalue()
        self.assertIn("DRY RUN", output)
        self.assertIn("outer background job will remain unchanged", output)
        self.assertEqual(target["job_status"], "pending")

    def test_dry_run_output_is_redacted(self):
        target = _job_row(_TARGET_JOB_ID)
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [target])

        buf = io.StringIO()
        with patch(
            "scripts.v49_run_single_audit_job.build_supabase_client",
            return_value=client,
        ), redirect_stdout(buf):
            main(["--job-id", _TARGET_JOB_ID])

        output = buf.getvalue()
        self.assertNotIn("SHOULD-NOT-PRINT", output)
        self.assertIn(_RUN_ID.lower(), output.lower())

    def test_execute_under_pytest_refuses_live_execution(self):
        target = _job_row(_TARGET_JOB_ID)
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [target])

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with patch(
            "scripts.v49_run_single_audit_job.build_supabase_client",
            return_value=client,
        ), patch(
            "scripts.v49_run_single_audit_job.running_under_pytest",
            return_value=True,
        ), redirect_stdout(out_buf), redirect_stderr(err_buf):
            exit_code = main(["--job-id", _TARGET_JOB_ID, "--execute"])

        self.assertEqual(exit_code, 2)
        self.assertEqual(client.rpc_calls, [])
        self.assertIn("refusing live execution under pytest", err_buf.getvalue())
        self.assertEqual(target["job_status"], "pending")

    def test_execute_summary_includes_required_safe_fields(self):
        target = _job_row(_TARGET_JOB_ID)
        client = _V49ReadOnlyFake(_happy_path_audit_client(), [target])
        plan = build_plan(client, _TARGET_JOB_ID)

        with _context_patches()[0], _context_patches()[1], patch(
            "scripts.v49_run_single_audit_job.execute_single_job",
            return_value=type(
                "R",
                (),
                {
                    "audit_run_id": _RUN_ID.lower(),
                    "question_version_id": _QVID.lower(),
                    "run_status": "completed",
                    "audit_execution_started": True,
                    "audit_execution_completed": True,
                    "finding_count": 0,
                    "passes_executed": ["A", "B"],
                },
            )(),
        ), patch(
            "workers.ai_quality_provider_factory.build_ai_quality_providers_from_env",
            return_value=_happy_path_providers(),
        ), patch(
            "scripts.v49_run_single_audit_job.running_under_pytest",
            return_value=False,
        ):
            buf = io.StringIO()
            with patch(
                "scripts.v49_run_single_audit_job.build_supabase_client",
                return_value=client,
            ), redirect_stdout(buf):
                exit_code = main(["--job-id", _TARGET_JOB_ID, "--execute"])

        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        json_start = output.index("{")
        summary = json.loads(output[json_start:])
        self.assertFalse(summary["outer_background_job_mutated"])
        self.assertFalse(summary["queue_claim_rpc_called"])
        self.assertTrue(summary["v1_retrieval_only"])
        self.assertEqual(summary["requested_job_id"], _TARGET_JOB_ID)


if __name__ == "__main__":
    unittest.main()
