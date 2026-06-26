"""
Tests for certification-wide duplicate question audit background job handler.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.background_worker import BackgroundWorker
from workers.certification_question_loader import (
    _dedupe_latest_version_per_question,
    load_certification_current_question_versions,
)
from workers.job_handlers import (
    HandlerPayloadError,
    build_handler_registry,
    make_certification_duplicate_audit_handler,
)
from workers.run_certification_duplicate_audit import (
    _ENQUEUE_RPC,
    _JOB_TYPE,
    build_enqueue_params,
    build_payload,
    validate_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260624160000_v45_certification_duplicate_audit_job.sql"
)

_CERT = "Platform Admin"
_QV_A = "aaaaaaaa-0000-0000-0000-000000000001"
_QV_B = "bbbbbbbb-0000-0000-0000-000000000002"
_QV_C = "cccccccc-0000-0000-0000-000000000003"

_VALID_PAYLOAD = {
    "certification_exam_name": _CERT,
    "created_by": "audit-worker@certbound.io",
}

_LOADER_ROWS = [
    {
        "question_version_id": _QV_A,
        "question_id": 1,
        "certification_exam_name": _CERT,
        "question_text": "What is Flow?",
        "category": "Automation",
        "version_number": 2,
    },
    {
        "question_version_id": _QV_B,
        "question_id": 2,
        "certification_exam_name": _CERT,
        "question_text": "What is Flow?",
        "category": "Platform",
        "version_number": 1,
    },
]

_CREATE_RESPONSE = [{"audit_run_id": "audit-run-cert-dup-001"}]
_COMPLETE_RESPONSE = [{
    "run_status": "completed",
    "finding_count": 1,
    "evidence_count": 0,
}]


class FakeSupabase:
    def __init__(self):
        self.calls = []
        self.responses: dict[str, list] = {}
        self.errors: dict[str, str] = {}

    def set_response(self, name: str, rows: list):
        self.responses[name] = rows

    def set_error(self, name: str, message: str):
        self.errors[name] = message

    def rpc(self, name, params):
        self.calls.append({"name": name, "params": params})
        return self

    def execute(self):
        call = self.calls[-1]
        name = call["name"]
        if name in self.errors:
            return _FakeResult([], error=self.errors[name])
        return _FakeResult(self.responses.get(name, []))


class _FakeResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


def _make_handler_client(rows=None, *, use_default_rows: bool = True):
    fake = FakeSupabase()
    if rows is None and use_default_rows:
        rows = _LOADER_ROWS
    elif rows is None:
        rows = []
    fake.set_response("list_certification_current_question_versions_v1", rows)
    fake.set_response("list_duplicate_question_pair_keys_v1", [])
    fake.set_response("create_audit_run_v1", _CREATE_RESPONSE)
    fake.set_response("complete_audit_run_v1", _COMPLETE_RESPONSE)
    handler = make_certification_duplicate_audit_handler(fake)
    return fake, handler


class TestMigrationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def _loader_sql(self) -> str:
        start = self.sql.index(
            "CREATE OR REPLACE FUNCTION public.list_certification_current_question_versions_v1("
        )
        end = self.sql.index("$$;", start) + 3
        return self.sql[start:end]

    def test_migration_registers_job_type_and_latest_version_loader(self):
        self.assertIn("'certification_duplicate_audit'", self.sql)
        loader_sql = self._loader_sql()
        self.assertNotIn("event_type = 'published'", loader_sql)
        self.assertNotIn("question_version_events", loader_sql)
        self.assertIn("ORDER  BY qv.version_number DESC", loader_sql)
        self.assertIn("qv.created_at DESC", loader_sql)
        self.assertIn("qv.id DESC", loader_sql)
        self.assertIn("LIMIT  1", loader_sql)
        self.assertIn("q.is_active = TRUE", loader_sql)


class TestEnqueuePayload(unittest.TestCase):
    def test_validate_payload_requires_certification_exam_name(self):
        with self.assertRaises(ValueError):
            validate_payload({})
        cert = validate_payload({"certification_exam_name": _CERT})
        self.assertEqual(cert, _CERT)

    def test_build_enqueue_params_use_certification_duplicate_audit_job_type(self):
        payload = build_payload(certification_exam_name=_CERT, created_by="ops@certbound.io")
        params = build_enqueue_params(payload, created_by="ops@certbound.io")
        self.assertEqual(params["p_job_type"], _JOB_TYPE)
        self.assertEqual(params["p_payload"]["certification_exam_name"], _CERT)
        self.assertEqual(params["p_created_by"], "ops@certbound.io")


class TestCertificationQuestionLoader(unittest.TestCase):
    def test_loader_calls_list_rpc_and_maps_rows(self):
        fake = FakeSupabase()
        fake.set_response("list_certification_current_question_versions_v1", _LOADER_ROWS)
        rows = load_certification_current_question_versions(fake, _CERT)
        self.assertEqual(len(rows), 2)
        self.assertEqual(fake.calls[0]["name"], "list_certification_current_question_versions_v1")
        self.assertEqual(
            fake.calls[0]["params"]["p_certification_exam_name"],
            _CERT,
        )
        self.assertEqual(rows[0]["question_version_id"], _QV_A)

    def test_loader_keeps_latest_version_per_question_only(self):
        rows = _dedupe_latest_version_per_question([
            {
                "question_id": 10,
                "question_version_id": "old-version",
                "version_number": 1,
                "certification_exam_name": _CERT,
                "question_text": "Old stem",
            },
            {
                "question_id": 10,
                "question_version_id": "new-version",
                "version_number": 3,
                "certification_exam_name": _CERT,
                "question_text": "New stem",
            },
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question_version_id"], "new-version")

    def test_loader_filters_rows_to_requested_certification_in_handler(self):
        fake = FakeSupabase()
        fake.set_response("list_certification_current_question_versions_v1", [
            {
                "question_version_id": _QV_A,
                "question_id": 1,
                "certification_exam_name": _CERT,
                "question_text": "Cert A stem",
                "category": "A",
                "version_number": 1,
            },
        ])
        with patch(
            "workers.duplicate_question_detector.orchestrate_certification_duplicate_audit",
            return_value={
                "audit_run_id": "audit-1",
                "run_status": "completed",
                "finding_count": 0,
                "evidence_count": 0,
            },
        ) as orchestrate_mock:
            handler = make_certification_duplicate_audit_handler(fake)
            handler(
                job_id="job-1",
                payload=_VALID_PAYLOAD,
                checkpoint={},
                attempt=1,
                heartbeat_fn=lambda: None,
            )
        passed_rows = orchestrate_mock.call_args.kwargs["rows"]
        self.assertEqual(len(passed_rows), 1)
        self.assertTrue(all(row["certification_exam_name"] == _CERT for row in passed_rows))
        self.assertEqual(
            fake.calls[0]["params"]["p_certification_exam_name"],
            _CERT,
        )


class TestCertificationDuplicateAuditHandler(unittest.TestCase):
    def test_missing_certification_payload_rejected(self):
        _, handler = _make_handler_client()
        with self.assertRaises(HandlerPayloadError):
            handler(
                job_id="job-1",
                payload={"created_by": "ops@certbound.io"},
                checkpoint={},
                attempt=1,
                heartbeat_fn=lambda: None,
            )

    def test_empty_bank_rejected(self):
        fake, handler = _make_handler_client(None, use_default_rows=False)
        with self.assertRaises(HandlerPayloadError):
            handler(
                job_id="job-1",
                payload=_VALID_PAYLOAD,
                checkpoint={},
                attempt=1,
                heartbeat_fn=lambda: None,
            )
        self.assertEqual(
            fake.calls[0]["name"],
            "list_certification_current_question_versions_v1",
        )

    def test_successful_handler_runs_orchestration(self):
        fake, handler = _make_handler_client()
        result = handler(
            job_id="job-1",
            payload=_VALID_PAYLOAD,
            checkpoint={},
            attempt=1,
            heartbeat_fn=lambda: None,
        )
        rpc_names = [call["name"] for call in fake.calls]
        self.assertEqual(rpc_names[0], "list_certification_current_question_versions_v1")
        self.assertIn("list_duplicate_question_pair_keys_v1", rpc_names)
        self.assertIn("create_audit_run_v1", rpc_names)
        self.assertIn("complete_audit_run_v1", rpc_names)
        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["question_count"], 2)
        self.assertEqual(result["certification_exam_name"], _CERT)

    def test_loader_rpc_failure_propagates(self):
        fake = FakeSupabase()
        fake.set_error(
            "list_certification_current_question_versions_v1",
            "rpc unavailable",
        )
        handler = make_certification_duplicate_audit_handler(fake)
        with self.assertRaises(RuntimeError):
            handler(
                job_id="job-1",
                payload=_VALID_PAYLOAD,
                checkpoint={},
                attempt=1,
                heartbeat_fn=lambda: None,
            )


class TestWorkerIntegration(unittest.TestCase):
    def _heartbeat_response(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "job_status": "running",
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
            "heartbeat_at": "2099-01-01T00:00:00+00:00",
        }

    def _make_job(self, payload: dict) -> dict:
        return {
            "job_id": "job-cert-dup-01",
            "job_type": _JOB_TYPE,
            "payload": payload,
            "checkpoint": {},
            "attempt": 1,
            "job_status": "running",
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
        }

    def test_worker_dispatches_certification_duplicate_audit_and_completes(self):
        job = self._make_job(_VALID_PAYLOAD)
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response("heartbeat_background_job_v1", [self._heartbeat_response(job["job_id"])])
        fake.set_response("list_certification_current_question_versions_v1", _LOADER_ROWS)
        fake.set_response("list_duplicate_question_pair_keys_v1", [])
        fake.set_response("create_audit_run_v1", _CREATE_RESPONSE)
        fake.set_response("complete_audit_run_v1", _COMPLETE_RESPONSE)
        fake.set_response("complete_background_job_v1", [{
            "job_id": job["job_id"],
            "job_status": "completed",
            "completed_at": "2099-01-01T00:00:00+00:00",
        }])

        worker = BackgroundWorker(
            worker_id="integration-cert-dup-worker",
            client=fake,
            handlers=build_handler_registry(fake),
            sleep_interval=0.0,
        )
        worker.run_once()

        self.assertIn("complete_background_job_v1", [c["name"] for c in fake.calls])
        self.assertNotIn("fail_background_job_v1", [c["name"] for c in fake.calls])
        complete_calls = [
            call for call in fake.calls if call["name"] == "complete_background_job_v1"
        ]
        result = complete_calls[0]["params"]["p_result"]
        self.assertEqual(result["certification_exam_name"], _CERT)
        self.assertIn("audit_run_id", result)

    def test_worker_failure_uses_existing_retry_path(self):
        job = self._make_job(_VALID_PAYLOAD)
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response("heartbeat_background_job_v1", [self._heartbeat_response(job["job_id"])])
        fake.set_error(
            "list_certification_current_question_versions_v1",
            "database unavailable",
        )
        fake.set_response("fail_background_job_v1", [{
            "job_id": job["job_id"],
            "job_status": "pending",
            "available_at": "2099-01-01T00:05:00+00:00",
            "completed_at": None,
        }])

        worker = BackgroundWorker(
            worker_id="integration-cert-dup-worker",
            client=fake,
            handlers=build_handler_registry(fake),
            sleep_interval=0.0,
        )
        worker.run_once()

        self.assertIn("fail_background_job_v1", [c["name"] for c in fake.calls])
        self.assertNotIn("complete_background_job_v1", [c["name"] for c in fake.calls])
        fail_calls = [call for call in fake.calls if call["name"] == "fail_background_job_v1"]
        self.assertIn("database unavailable", fail_calls[0]["params"]["p_error_message"])


class TestEnqueueUtility(unittest.TestCase):
    def test_enqueue_rpc_name(self):
        self.assertEqual(_ENQUEUE_RPC, "enqueue_background_job_v1")


if __name__ == "__main__":
    unittest.main()
