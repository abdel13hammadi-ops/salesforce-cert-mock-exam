"""
Local Docker integration verification for the V48 AI quality audit worker.

Requires the disposable database:
  container: certbound-v48-test
  database:  certbound_v48_test

Set V48_TEST_DATABASE_URL (default postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test).
When V48_TEST_DATABASE_URL is explicitly set, tests run and fail if the database is unreachable.
The test wraps all work in BEGIN ... ROLLBACK.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import unittest
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore

from workers.ai_quality_audit_worker import (
    AiQualityAuditProviders,
    process_ai_quality_audit_job,
)
from workers.ai_quality_audit_evidence import prepare_smoke_evidence_set
from workers.llm_providers import LlmResponse

_DSN_ENV = "V48_TEST_DATABASE_URL"
_DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test"
_EMPTY_EVIDENCE_HASH = hashlib.sha256(b"[]").hexdigest()


_UUID_ARG_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _adapt_rpc_arg(value: object):
    if isinstance(value, list):
        if value and all(
            isinstance(item, str) and _UUID_ARG_RE.match(item) for item in value
        ):
            elements = ",".join(f"'{item}'::uuid" for item in value)
            return psycopg2.extensions.AsIs(f"ARRAY[{elements}]")
        return psycopg2.extras.Json(value)
    if isinstance(value, dict):
        return psycopg2.extras.Json(value)
    return value


class _FakeRpcResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


class _PsycopgRpcBuilder:
    def __init__(self, rows: List[dict], error=None):
        self._rows = rows
        self._error = error

    def execute(self):
        return _FakeRpcResult(self._rows, self._error)


class PsycopgV48Client:
    """Minimal Supabase-shaped client over psycopg2 for V48 RPC + table reads."""

    def __init__(self, conn):
        self.conn = conn

    def rpc(self, name: str, params: dict):
        args = [_adapt_rpc_arg(value) for value in params.values()]
        placeholders = ", ".join(f"{key} => %s" for key in params)
        sql = f"SELECT * FROM public.{name}({placeholders})"
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(row) for row in cur.fetchall()]
        return _PsycopgRpcBuilder(rows)

    def table(self, name: str):
        return _PsycopgTableQuery(self.conn, name)


class _PsycopgTableQuery:
    def __init__(self, conn, table_name: str):
        self.conn = conn
        self.table_name = table_name
        self.select_fields = "*"
        self.filters: List[tuple] = []
        self.order_field: Optional[str] = None
        self.limit_count: Optional[int] = None

    def select(self, fields: str):
        self.select_fields = fields
        return self

    def eq(self, field: str, value: object):
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field: str, values: list):
        self.filters.append(("in", field, tuple(values)))
        return self

    def order(self, field: str):
        self.order_field = field
        return self

    def limit(self, count: int):
        self.limit_count = count
        return self

    def execute(self):
        if (
            self.table_name == "resource_chunks"
            and "resource_versions(" in self.select_fields
        ):
            return self._execute_resource_chunks_nested_query()
        sql = f"SELECT {self.select_fields} FROM public.{self.table_name}"
        args: List[Any] = []
        if self.filters:
            clauses = []
            for op, field, value in self.filters:
                if op == "eq":
                    clauses.append(f"{field} = %s")
                    args.append(value)
                elif op == "in":
                    placeholders = ", ".join(["%s"] * len(value))
                    clauses.append(f"{field} IN ({placeholders})")
                    args.extend(value)
            sql += " WHERE " + " AND ".join(clauses)
        if self.order_field:
            sql += f" ORDER BY {self.order_field}"
        if self.limit_count is not None:
            sql += " LIMIT %s"
            args.append(self.limit_count)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(row) for row in cur.fetchall()]
        return _FakeRpcResult(rows)

    def _execute_resource_chunks_nested_query(self) -> _FakeRpcResult:
        chunk_ids = None
        clauses: List[str] = []
        args: List[Any] = []
        for op, field, value in self.filters:
            if op == "eq":
                clauses.append(f"rc.{field} = %s")
                args.append(value)
            elif op == "in":
                chunk_ids = list(value)
                placeholders = ", ".join(["%s"] * len(value))
                clauses.append(f"rc.id IN ({placeholders})")
                args.extend(value)
        sql = """
            SELECT
                rc.id,
                rc.chunk_text,
                rc.resource_version_id,
                rv.resource_id,
                rv.version_number,
                ors.id AS official_resource_id,
                ors.title,
                ors.certification_exam_name
            FROM public.resource_chunks rc
            JOIN public.resource_versions rv ON rv.id = rc.resource_version_id
            JOIN public.official_resources ors ON ors.id = rv.resource_id
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            raw_rows = [dict(row) for row in cur.fetchall()]
        rows = [
            {
                "id": row["id"],
                "chunk_text": row["chunk_text"],
                "resource_version_id": row["resource_version_id"],
                "resource_versions": {
                    "resource_id": row["resource_id"],
                    "version_number": row["version_number"],
                    "official_resources": {
                        "id": row["official_resource_id"],
                        "title": row["title"],
                        "certification_exam_name": row["certification_exam_name"],
                    },
                },
            }
            for row in raw_rows
        ]
        return _FakeRpcResult(rows)


def _dsn() -> str:
    return os.environ.get(_DSN_ENV, _DEFAULT_DSN).strip()


def _dsn_explicitly_configured() -> bool:
    return _DSN_ENV in os.environ and os.environ[_DSN_ENV].strip() != ""


def _can_connect() -> bool:
    if psycopg2 is None:
        return False
    try:
        conn = psycopg2.connect(_dsn())
        conn.close()
        return True
    except Exception:
        return False


class TestAiQualityAuditDockerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if psycopg2 is None:
            raise unittest.SkipTest("psycopg2 is not installed; skipping integration test")
        if _dsn_explicitly_configured():
            return
        if not _can_connect():
            raise unittest.SkipTest(
                f"V48 Docker database unavailable at {_dsn()!r}; skipping integration test"
            )

    def setUp(self):
        self.conn = psycopg2.connect(_dsn())
        self.conn.autocommit = False
        with self.conn.cursor() as cur:
            cur.execute("BEGIN")
        self.client = PsycopgV48Client(self.conn)
        self.fixture = self._seed_fixture()

    def tearDown(self):
        try:
            with self.conn.cursor() as cur:
                cur.execute("ROLLBACK")
        finally:
            self.conn.close()

    def _seed_fixture(self) -> dict:
        qvid = str(uuid.uuid4())
        chunk1 = str(uuid.uuid4())
        chunk2 = str(uuid.uuid4())
        resource1 = str(uuid.uuid4())
        resource2 = str(uuid.uuid4())
        rv1 = str(uuid.uuid4())
        rv2 = str(uuid.uuid4())
        question_id = 990001
        exam = "V48-INTEGRATION-ADM"
        hash1 = "a" * 64
        hash2 = "b" * 64

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.certifications (
                    exam_name, display_name, certification_code,
                    passing_score, time_limit_minutes, question_count, is_active
                ) VALUES (%s, %s, %s, 68, 105, 60, true)
                ON CONFLICT (exam_name) DO NOTHING
                """,
                (exam, "V48 Integration Exam", "V48INT"),
            )
            cur.execute(
                """
                INSERT INTO public.languages (
                    language_code, language_name, native_name, is_active, display_order
                ) VALUES ('en', 'English', 'English', true, 1)
                ON CONFLICT (language_code) DO NOTHING
                """
            )
            cur.execute(
                """
                INSERT INTO public.questions (
                    id, exam_name, category, difficulty, question_text, question_type,
                    select_count, explanation, is_active, is_exam_eligible, language_code
                ) VALUES (
                    %s, %s, %s, 'medium', %s, 'single', 1, %s, true, true, 'en'
                )
                ON CONFLICT (id) DO UPDATE SET exam_name = EXCLUDED.exam_name
                """,
                (
                    question_id,
                    exam,
                    "Configuration",
                    "Which Salesforce feature enables profile-based defaults?",
                    "Profiles define default settings.",
                ),
            )
            cur.execute(
                """
                INSERT INTO public.question_versions (
                    id, question_id, version_number, question_text, explanation,
                    category, difficulty, question_type, select_count, language_code,
                    content_hash, source_type, created_by
                ) VALUES (
                    %s, %s, 1, %s, %s, %s, 'medium', 'single', 1, 'en',
                    %s, 'manual', 'v48-integration'
                )
                """,
                (
                    qvid,
                    question_id,
                    "Which Salesforce feature enables profile-based defaults?",
                    "Profiles define default settings.",
                    "Configuration",
                    "c" * 64,
                ),
            )
            for label, text, order, correct in (
                ("A", "Profiles", 1, True),
                ("B", "Roles", 2, False),
            ):
                cur.execute(
                    """
                    INSERT INTO public.question_option_versions (
                        question_version_id, option_label, option_text,
                        display_order, is_correct
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (qvid, label, text, order, correct),
                )
            for resource_id, rv_id, chunk_id, chunk_hash, title, chunk_text, metadata in (
                (
                    resource1,
                    rv1,
                    chunk1,
                    hash1,
                    "Profiles Help",
                    "Profiles define default settings and object permissions for users.",
                    '{"domain": "Configuration"}',
                ),
                (
                    resource2,
                    rv2,
                    chunk2,
                    hash2,
                    "Roles Overview",
                    "Roles define record-level access and hierarchy visibility.",
                    '{"domain": "Configuration"}',
                ),
            ):
                cur.execute(
                    """
                    INSERT INTO public.official_resources (
                        id, certification_exam_name, resource_type, title,
                        metadata, is_active, created_by
                    ) VALUES (%s, %s, 'official_documentation', %s, %s::jsonb, true, 'v48-integration')
                    """,
                    (resource_id, exam, title, metadata),
                )
                cur.execute(
                    """
                    INSERT INTO public.resource_versions (
                        id, resource_id, version_number, content_text, content_hash, created_by
                    ) VALUES (%s, %s, 1, %s, %s, 'v48-integration')
                    """,
                    (rv_id, resource_id, chunk_text, chunk_hash),
                )
                cur.execute(
                    """
                    INSERT INTO public.resource_chunks (
                        id, resource_version_id, chunk_index, chunk_text, content_hash
                    ) VALUES (%s, %s, 0, %s, %s)
                    """,
                    (chunk_id, rv_id, chunk_text, chunk_hash),
                )

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT encode(extensions.digest(
                    COALESCE(
                        jsonb_agg(
                            jsonb_build_array(entry.r, entry.cid::text, entry.h)
                            ORDER BY entry.r
                        ),
                        '[]'::jsonb
                    )::text,
                    'sha256'::text
                ), 'hex')
                FROM (
                    VALUES
                        (1, %s::uuid, %s),
                        (2, %s::uuid, %s)
                ) AS entry(r, cid, h)
                """,
                (chunk1, hash1, chunk2, hash2),
            )
            evidence_hash = cur.fetchone()[0]

        return {
            "question_version_id": qvid,
            "chunk1": chunk1,
            "chunk2": chunk2,
            "evidence_hash": evidence_hash,
        }

    def _create_run(self) -> str:
        row = self.client.rpc(
            "create_or_get_ai_quality_audit_run_v1",
            {
                "p_target_question_version_id": self.fixture["question_version_id"],
                "p_prompt_version": "v48-integration-prompt",
                "p_ruleset_version": "v48-integration-rules",
                "p_primary_model_name": "integration-primary",
                "p_dispute_model_name": "integration-dispute",
                "p_pilot_batch_id": "v48-integration-batch",
                "p_evidence_set_hash": self.fixture["evidence_hash"],
                "p_evidence_chunks": [
                    {"resource_chunk_id": self.fixture["chunk1"], "retrieval_rank": 1},
                    {"resource_chunk_id": self.fixture["chunk2"], "retrieval_rank": 2},
                ],
                "p_created_by": "v48-integration-test",
                "p_metadata": {},
            },
        ).execute()
        self.assertFalse(row.error, row.error)
        return str(row.data[0]["audit_run_id"])

    def test_no_dispute_run_completes_under_rollback(self):
        audit_run_id = self._create_run()

        def primary(**kwargs):
            pass_code = (kwargs.get("metadata") or {}).get("pass_code")
            if pass_code == "A":
                body = {"selected_option_labels": ["A"]}
            else:
                body = {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [],
                }
            return LlmResponse(
                parsed_response=body,
                input_tokens=1,
                output_tokens=1,
                model_name=kwargs.get("model_name"),
            )

        providers = AiQualityAuditProviders(primary=primary, dispute=primary)
        summary = process_ai_quality_audit_job(
            self.client,
            {
                "audit_run_id": audit_run_id,
                "question_version_id": self.fixture["question_version_id"],
            },
            providers,
            worker_id="v48-integration-worker",
        )
        self.assertEqual(summary["run_status"], "completed")
        self.assertEqual(summary["finding_count"], 0)
        self.assertIn("A", summary["passes_executed"])
        self.assertIn("B", summary["passes_executed"])

    def test_resolved_dispute_completes_under_rollback(self):
        audit_run_id = self._create_run()
        calls = {"b": 0}

        def primary(**kwargs):
            pass_code = (kwargs.get("metadata") or {}).get("pass_code")
            if pass_code == "A":
                return LlmResponse(
                    parsed_response={"selected_option_labels": ["A"]},
                    input_tokens=1,
                    output_tokens=1,
                )
            calls["b"] += 1
            return LlmResponse(
                parsed_response={
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        {
                            "finding_ref": "F1",
                            "finding_code": "WRONG_ANSWER_KEY",
                            "finding_type": "correctness",
                            "severity": "high",
                            "materiality": "blocking",
                            "title": "Wrong key",
                            "description": "Stored answer appears wrong.",
                            "evidence_chunk_ids": [self.fixture["chunk1"]],
                            "metadata": {},
                        }
                    ],
                },
                input_tokens=1,
                output_tokens=1,
            )

        def dispute(**kwargs):
            return LlmResponse(
                parsed_response={
                    "resolution_type": "NORMAL_DISPUTE",
                    "resolution_status": "RESOLVED",
                    "substituted_for_passes": [],
                    "confirmed_finding_refs": ["F1"],
                },
                input_tokens=1,
                output_tokens=1,
            )

        providers = AiQualityAuditProviders(primary=primary, dispute=dispute)
        summary = process_ai_quality_audit_job(
            self.client,
            {
                "audit_run_id": audit_run_id,
                "question_version_id": self.fixture["question_version_id"],
            },
            providers,
            worker_id="v48-integration-worker",
        )
        self.assertEqual(summary["run_status"], "completed")
        self.assertGreaterEqual(summary["finding_count"], 1)
        self.assertIn("C", summary["passes_executed"])

    def test_malformed_pass_a_persisted_as_schema_invalid_not_running(self):
        audit_run_id = self._create_run()
        pass_a_calls = {"count": 0}
        recorded_statuses: list[str] = []
        original_record = __import__(
            "workers.ai_quality_audit_worker", fromlist=["_record_pass_result"]
        )._record_pass_result

        def tracking_record(client, **kwargs):
            recorded_statuses.append(str(kwargs.get("status")))
            return original_record(client, **kwargs)

        def primary(**kwargs):
            pass_code = (kwargs.get("metadata") or {}).get("pass_code")
            if pass_code == "A":
                pass_a_calls["count"] += 1
                if pass_a_calls["count"] == 1:
                    return LlmResponse(
                        parsed_response={"selected_option_labels": ["Z"]},
                        input_tokens=1,
                        output_tokens=1,
                    )
                return LlmResponse(
                    parsed_response={"selected_option_labels": ["A"]},
                    input_tokens=1,
                    output_tokens=1,
                )
            return LlmResponse(
                parsed_response={
                    "selected_option_labels": ["A"],
                    "proposed_findings": [],
                },
                input_tokens=1,
                output_tokens=1,
            )

        with patch(
            "workers.ai_quality_audit_worker._record_pass_result",
            side_effect=tracking_record,
        ):
            summary = process_ai_quality_audit_job(
                self.client,
                {
                    "audit_run_id": audit_run_id,
                    "question_version_id": self.fixture["question_version_id"],
                },
                AiQualityAuditProviders(primary=primary, dispute=primary),
                worker_id="v48-integration-worker",
            )

        self.assertEqual(summary["run_status"], "completed")
        self.assertIn("schema_invalid", recorded_statuses)
        self.assertIn("completed", recorded_statuses)
        self.assertLess(
            recorded_statuses.index("schema_invalid"),
            recorded_statuses.index("completed"),
        )

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT status, result_json, attempt_count
                FROM public.audit_run_pass_results
                WHERE audit_run_id = %s AND pass_code = 'A'
                """,
                (audit_run_id,),
            )
            row = cur.fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["attempt_count"], 2)
        self.assertIsNotNone(row["result_json"])

    def test_smoke_evidence_is_frozen_before_job_enqueue(self):
        prepared = prepare_smoke_evidence_set(
            self.client,
            self.fixture["question_version_id"],
        )

        self.assertGreaterEqual(len(prepared.evidence_chunks), 1)
        self.assertIn(
            self.fixture["chunk1"].lower(),
            {item["resource_chunk_id"] for item in prepared.evidence_chunks},
        )

        create_row = self.client.rpc(
            "create_or_get_ai_quality_audit_run_v1",
            {
                "p_target_question_version_id": self.fixture["question_version_id"],
                "p_prompt_version": "v48-integration-prompt",
                "p_ruleset_version": "v48-integration-rules",
                "p_primary_model_name": "integration-primary",
                "p_dispute_model_name": "integration-dispute",
                "p_pilot_batch_id": "v48-integration-batch",
                "p_evidence_set_hash": prepared.evidence_set_hash,
                "p_evidence_chunks": prepared.evidence_chunks,
                "p_created_by": "v48-integration-test",
                "p_metadata": {},
            },
        ).execute()
        self.assertFalse(create_row.error, create_row.error)
        audit_run_id = str(create_row.data[0]["audit_run_id"])

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT retrieval_rank, resource_chunk_id, content_hash_at_execution
                FROM public.audit_run_evidence_set
                WHERE audit_run_id = %s
                ORDER BY retrieval_rank
                """,
                (audit_run_id,),
            )
            evidence_rows = cur.fetchall()

        self.assertEqual(len(evidence_rows), len(prepared.evidence_chunks))
        for index, chunk in enumerate(prepared.evidence_chunks):
            self.assertEqual(evidence_rows[index]["retrieval_rank"], chunk["retrieval_rank"])
            self.assertEqual(
                str(evidence_rows[index]["resource_chunk_id"]).lower(),
                chunk["resource_chunk_id"],
            )

        enqueue_row = self.client.rpc(
            "enqueue_background_job_v1",
            {
                "p_job_type": "ai_quality_audit_smoke",
                "p_payload": {
                    "audit_run_id": audit_run_id,
                    "question_version_id": self.fixture["question_version_id"],
                },
                "p_priority": 100,
                "p_max_attempts": 3,
                "p_created_by": "v48-integration-test",
                "p_model_name": "integration-primary",
                "p_prompt_version": "v48-integration-prompt",
                "p_metadata": {"pilot_batch_id": "v48-integration-batch"},
            },
        ).execute()
        self.assertFalse(enqueue_row.error, enqueue_row.error)
        self.assertEqual(enqueue_row.data[0]["job_status"], "pending")

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.audit_run_evidence_set
                WHERE audit_run_id = %s
                """,
                (audit_run_id,),
            )
            frozen_count = cur.fetchone()[0]

        self.assertEqual(frozen_count, len(prepared.evidence_chunks))

    def test_pass_b_source_support_retry_completes_under_rollback(self):
        audit_run_id = self._create_run()
        pass_b_calls = {"count": 0}

        invalid_source_support = {
            "selected_option_labels": ["A"],
            "proposed_findings": [
                {
                    "finding_ref": "F2",
                    "finding_code": "SOURCE_SUPPORT_WEAK",
                    "finding_type": "source_support",
                    "severity": "medium",
                    "materiality": "warning",
                    "title": "Weak source support",
                    "description": "No supporting chunk.",
                    "evidence_chunk_ids": [],
                    "metadata": {},
                }
            ],
        }
        valid_source_support = {
            "selected_option_labels": ["A"],
            "proposed_findings": [
                {
                    "finding_ref": "F2",
                    "finding_code": "SOURCE_SUPPORT_WEAK",
                    "finding_type": "source_support",
                    "severity": "medium",
                    "materiality": "warning",
                    "title": "Weak source support",
                    "description": "No supporting chunk.",
                    "evidence_chunk_ids": [],
                    "metadata": {
                        "source_support_context": {
                            "attempted_retrieval": 2,
                            "evidence_limitation": "Frozen evidence did not substantiate the claim.",
                            "proposed_technical_claim": "The explanation overstates source support.",
                            "insufficiency_reason": "No frozen chunk directly supports the explanation.",
                        }
                    },
                }
            ],
        }

        def primary(**kwargs):
            pass_code = (kwargs.get("metadata") or {}).get("pass_code")
            if pass_code == "A":
                return LlmResponse(
                    parsed_response={"selected_option_labels": ["A"]},
                    input_tokens=1,
                    output_tokens=1,
                )
            pass_b_calls["count"] += 1
            if pass_b_calls["count"] == 1:
                return LlmResponse(
                    parsed_response=invalid_source_support,
                    input_tokens=1,
                    output_tokens=1,
                )
            return LlmResponse(
                parsed_response=valid_source_support,
                input_tokens=1,
                output_tokens=1,
            )

        summary = process_ai_quality_audit_job(
            self.client,
            {
                "audit_run_id": audit_run_id,
                "question_version_id": self.fixture["question_version_id"],
            },
            AiQualityAuditProviders(primary=primary, dispute=primary),
            worker_id="v48-integration-worker",
        )

        self.assertEqual(summary["run_status"], "completed")
        self.assertEqual(pass_b_calls["count"], 2)
        self.assertIn("B", summary["passes_executed"])

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT status, attempt_count, result_json
                FROM public.audit_run_pass_results
                WHERE audit_run_id = %s AND pass_code = 'B'
                """,
                (audit_run_id,),
            )
            row = cur.fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["attempt_count"], 2)
        context = row["result_json"]["proposed_findings"][0]["metadata"][
            "source_support_context"
        ]
        self.assertEqual(context["attempted_retrieval"], 2)
        self.assertTrue(context["evidence_limitation"])

    def test_wait_coordination_does_not_raise_under_rollback(self):
        audit_run_id = self._create_run()
        claim = self.client.rpc(
            "claim_ai_quality_audit_pass_v1",
            {
                "p_audit_run_id": audit_run_id,
                "p_worker_id": "v48-integration-blocker",
                "p_lease_seconds": 120,
            },
        ).execute()
        self.assertFalse(claim.error, claim.error)
        self.assertEqual(claim.data[0]["action"], "EXECUTE_PASS_A")
        blocker_token = claim.data[0]["lease_token"]

        wait_polls = {"count": 0}

        def expire_blocker_lease_on_first_wait(_seconds):
            wait_polls["count"] += 1
            if wait_polls["count"] == 1:
                with self.conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.audit_run_pass_results
                        SET lease_expires_at = now() - interval '1 second'
                        WHERE audit_run_id = %s
                          AND pass_code = 'A'
                          AND lease_token = %s
                        """,
                        (audit_run_id, blocker_token),
                    )

        def primary(**kwargs):
            pass_code = (kwargs.get("metadata") or {}).get("pass_code")
            if pass_code == "A":
                return LlmResponse(
                    parsed_response={"selected_option_labels": ["A"]},
                    input_tokens=1,
                    output_tokens=1,
                )
            return LlmResponse(
                parsed_response={
                    "selected_option_labels": ["A"],
                    "proposed_findings": [],
                },
                input_tokens=1,
                output_tokens=1,
            )

        providers = AiQualityAuditProviders(primary=primary, dispute=primary)
        with patch("workers.ai_quality_audit_worker.time.sleep", expire_blocker_lease_on_first_wait):
            summary = process_ai_quality_audit_job(
                self.client,
                {
                    "audit_run_id": audit_run_id,
                    "question_version_id": self.fixture["question_version_id"],
                },
                providers,
                worker_id="v48-integration-worker",
                wait_poll_seconds=0,
                max_wait_polls=5,
            )

        self.assertEqual(summary["run_status"], "completed")
        self.assertGreaterEqual(wait_polls["count"], 1)
        self.assertIn("A", summary["passes_executed"])


if __name__ == "__main__":
    unittest.main()
