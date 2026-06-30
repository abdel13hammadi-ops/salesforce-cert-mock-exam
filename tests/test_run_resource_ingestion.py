"""
Hermetic tests for official-resource ingestion CLI.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.job_handlers import make_resource_ingestion_handler
from workers.run_resource_ingestion import (
    _ENQUEUE_RPC,
    _JOB_TYPE,
    assert_enqueue_allowed,
    build_enqueue_params,
    build_ingest_payload,
    fetch_official_resource,
    main,
    validate_ingest_payload,
    validate_resource_id,
)
from workers.resource_chunking import chunk_resource_content, sha256_hex

_RESOURCE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_CREATED_BY = "ingest-operator@certbound.io"
_SAMPLE_TEXT = (
    "Configuration and Setup covers org settings.\n\n"
    "Object Manager covers custom objects and fields."
)
_OFFICIAL_RESOURCE = {
    "id": _RESOURCE_ID,
    "title": "Platform Administrator Exam Guide",
    "certification_exam_name": "Salesforce Certified Platform Administrator",
    "resource_type": "exam_guide",
}


class FakeRpcResult:
    def __init__(self, data=None, error=None):
        self.data = data if data is not None else []
        self.error = error


class FakeTableQuery:
    def __init__(self, parent: "FakeSupabase", table_name: str):
        self.parent = parent
        self.table_name = table_name
        self.filters = []

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def execute(self):
        self.parent.table_calls.append({
            "table": self.table_name,
            "filters": list(self.filters),
        })
        rows = self.parent.table_responses.get(self.table_name, [])
        return FakeRpcResult(data=rows)


class FakeSupabase:
    def __init__(self):
        self.rpc_calls: list[tuple[str, dict]] = []
        self.table_calls: list[dict] = []
        self.rpc_responses: dict = {}
        self.table_responses: dict = {}

    def set_rpc_response(self, name: str, data: list):
        self.rpc_responses[name] = FakeRpcResult(data=data)

    def set_table_response(self, name: str, rows: list):
        self.table_responses[name] = rows

    def rpc(self, name: str, params: dict):
        self.rpc_calls.append((name, params))
        response = self.rpc_responses.get(name, FakeRpcResult(data=[]))
        return _ImmediateExecutor(response)

    def table(self, name: str):
        return FakeTableQuery(self, name)


class _ImmediateExecutor:
    def __init__(self, response: FakeRpcResult):
        self._response = response

    def execute(self):
        return self._response


class TestRunResourceIngestionValidation(unittest.TestCase):
    def test_malformed_uuid_rejected(self):
        with self.assertRaises(ValueError):
            validate_resource_id("not-a-uuid")

    def test_valid_payload_matches_handler_contract(self):
        chunked = chunk_resource_content(_SAMPLE_TEXT, target_words_per_chunk=50)
        payload = build_ingest_payload(
            resource_id=_RESOURCE_ID,
            created_by=_CREATED_BY,
            chunked=chunked,
            input_file=Path("exam-guide.txt"),
            target_words_per_chunk=50,
        )
        validate_ingest_payload(payload)

        fake = FakeSupabase()
        fake.set_rpc_response("ingest_resource_version_v1", [{
            "resource_version_id": "11111111-1111-1111-1111-111111111111",
            "resource_id": _RESOURCE_ID,
            "version_number": 1,
            "chunk_count": len(chunked.chunks),
        }])
        handler = make_resource_ingestion_handler(fake)
        handler(
            job_id="job-1",
            payload=payload,
            checkpoint={},
            attempt=1,
            heartbeat_fn=lambda: None,
        )
        rpc_name, rpc_params = fake.rpc_calls[0]
        self.assertEqual(rpc_name, "ingest_resource_version_v1")
        self.assertEqual(rpc_params["p_resource_id"], _RESOURCE_ID)
        self.assertEqual(rpc_params["p_content_text"], chunked.content_text)
        self.assertEqual(rpc_params["p_content_hash"], chunked.content_hash)
        self.assertEqual(rpc_params["p_created_by"], _CREATED_BY)
        self.assertEqual(rpc_params["p_chunks"], chunked.chunks)

    def test_content_and_chunk_hashes_are_generated_not_supplied_by_user(self):
        chunked = chunk_resource_content(_SAMPLE_TEXT, target_words_per_chunk=50)
        self.assertEqual(chunked.content_hash, sha256_hex(chunked.content_text))
        for chunk in chunked.chunks:
            self.assertEqual(chunk["content_hash"], sha256_hex(chunk["chunk_text"]))


class TestRunResourceIngestionCli(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.input_path = Path(self.temp_dir.name) / "exam-guide.txt"
        self.input_path.write_text(_SAMPLE_TEXT, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("workers.run_resource_ingestion.create_supabase_admin_client")
    def test_dry_run_performs_no_enqueue(self, client_mock):
        fake = FakeSupabase()
        fake.set_table_response("official_resources", [_OFFICIAL_RESOURCE])
        client_mock.return_value = fake

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = main([
                "--resource-id", _RESOURCE_ID,
                "--input-file", str(self.input_path),
                "--created-by", _CREATED_BY,
            ])
        self.assertEqual(rc, 0)
        self.assertNotIn(_ENQUEUE_RPC, [name for name, _ in fake.rpc_calls])
        output = buffer.getvalue()
        self.assertIn("mode: dry-run", output)
        self.assertIn("Platform Administrator Exam Guide", output)
        self.assertIn("content_hash:", output)
        self.assertIn("chunk_count:", output)

    @patch("workers.run_resource_ingestion.create_supabase_admin_client")
    def test_missing_resource_row_rejected(self, client_mock):
        fake = FakeSupabase()
        fake.set_table_response("official_resources", [])
        client_mock.return_value = fake

        buffer = io.StringIO()
        with redirect_stderr(buffer):
            rc = main([
                "--resource-id", _RESOURCE_ID,
                "--input-file", str(self.input_path),
                "--created-by", _CREATED_BY,
            ])
        self.assertEqual(rc, 1)
        self.assertIn("official_resource not found", buffer.getvalue())

    def test_missing_file_rejected(self):
        buffer = io.StringIO()
        with patch(
            "workers.run_resource_ingestion.create_supabase_admin_client",
        ) as client_mock:
            client_mock.return_value = FakeSupabase()
            with redirect_stderr(buffer):
                rc = main([
                    "--resource-id", _RESOURCE_ID,
                    "--input-file", str(Path(self.temp_dir.name) / "missing.txt"),
                    "--created-by", _CREATED_BY,
                ])
        self.assertEqual(rc, 1)
        self.assertIn("input file not found", buffer.getvalue())

    def test_malformed_uuid_rejected_by_cli(self):
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            rc = main([
                "--resource-id", "bad-uuid",
                "--input-file", str(self.input_path),
                "--created-by", _CREATED_BY,
            ])
        self.assertEqual(rc, 1)
        self.assertIn("invalid resource_id UUID", buffer.getvalue())

    def test_enqueue_requires_safety_env_var(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                assert_enqueue_allowed()

    @patch("workers.run_resource_ingestion.create_supabase_admin_client")
    def test_enqueue_calls_enqueue_rpc_with_resource_ingestion_payload(self, client_mock):
        fake = FakeSupabase()
        fake.set_table_response("official_resources", [_OFFICIAL_RESOURCE])
        fake.set_rpc_response(_ENQUEUE_RPC, [{
            "job_id": "job-ingest-001",
            "job_status": "pending",
        }])
        client_mock.return_value = fake

        with patch.dict(os.environ, {"CERTBOUND_ALLOW_JOB_ENQUEUE": "1"}, clear=False):
            with patch(
                "workers.run_resource_ingestion.running_under_pytest",
                return_value=False,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    rc = main([
                        "--resource-id", _RESOURCE_ID,
                        "--input-file", str(self.input_path),
                        "--created-by", _CREATED_BY,
                        "--enqueue",
                    ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.rpc_calls), 1)
        rpc_name, rpc_params = fake.rpc_calls[0]
        self.assertEqual(rpc_name, _ENQUEUE_RPC)
        self.assertEqual(rpc_params["p_job_type"], _JOB_TYPE)
        payload = rpc_params["p_payload"]
        self.assertEqual(payload["resource_id"], _RESOURCE_ID)
        self.assertEqual(payload["created_by"], _CREATED_BY)
        self.assertTrue(payload["content_hash"])
        self.assertTrue(payload["chunks"])
        output = buffer.getvalue()
        self.assertIn("job_id: job-ingest-001", output)
        self.assertIn("job_status: pending", output)

    def test_fetch_official_resource_raises_when_missing(self):
        fake = FakeSupabase()
        fake.set_table_response("official_resources", [])
        with self.assertRaises(ValueError) as ctx:
            fetch_official_resource(fake, _RESOURCE_ID)
        self.assertIn("official_resource not found", str(ctx.exception))

    def test_build_enqueue_params_uses_handler_payload(self):
        chunked = chunk_resource_content(_SAMPLE_TEXT, target_words_per_chunk=50)
        payload = build_ingest_payload(
            resource_id=_RESOURCE_ID,
            created_by=_CREATED_BY,
            chunked=chunked,
            input_file=Path("exam-guide.txt"),
            target_words_per_chunk=50,
        )
        params = build_enqueue_params(payload, created_by=_CREATED_BY)
        self.assertEqual(params["p_job_type"], _JOB_TYPE)
        self.assertEqual(params["p_payload"], payload)
        self.assertEqual(params["p_created_by"], _CREATED_BY)


if __name__ == "__main__":
    unittest.main()
