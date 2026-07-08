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
import sys
import unittest
import uuid
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
# The Supabase-shaped psycopg2 RPC/table shim is real, non-test runtime code
# (V58-QUALITY-04C) so ``workers.quality_benchmark_v48_orchestration`` can
# reuse it too; this test module only imports it, it does not define it.
from workers.v48_psycopg_client import PsycopgV48Client

_DSN_ENV = "V48_TEST_DATABASE_URL"
_DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test"
_EMPTY_EVIDENCE_HASH = hashlib.sha256(b"[]").hexdigest()


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


def _is_correctness_sub_call(kwargs: dict) -> bool:
    return (kwargs.get("metadata") or {}).get("pass_b_sub_call") == "correctness_detector"


def _correctness_agrees_with_stored_key(chunk_id: str) -> dict:
    """V60 specialist response for this fixture's fixed two-option (A/B,
    A correct) question that independently confirms the stored key, so
    ``derive_correctness_finding`` returns no correctness finding.
    """
    return {
        "option_judgments": [
            {
                "option_label": "A",
                "verdict": "SUPPORTED_AS_CORRECT",
                "citation_chunk_ids": [chunk_id],
                "evidence_rationale": "Fixture evidence confirms option A.",
            },
            {
                "option_label": "B",
                "verdict": "NOT_SUPPORTED_AS_CORRECT",
                "citation_chunk_ids": [chunk_id],
                "evidence_rationale": "Fixture evidence does not support option B.",
            },
        ],
        "evidence_sufficient_for_decision": True,
        "abstention_reason": None,
    }


def _correctness_disagrees_with_stored_key(chunk_id: str) -> dict:
    """V60 specialist response that independently supports option B instead
    of the stored-correct option A, so ``derive_correctness_finding``
    deterministically derives a real ``WRONG_ANSWER_KEY`` finding
    (finding_ref defaults to ``FC1``).
    """
    return {
        "option_judgments": [
            {
                "option_label": "A",
                "verdict": "NOT_SUPPORTED_AS_CORRECT",
                "citation_chunk_ids": [chunk_id],
                "evidence_rationale": "Fixture evidence does not support option A.",
            },
            {
                "option_label": "B",
                "verdict": "SUPPORTED_AS_CORRECT",
                "citation_chunk_ids": [chunk_id],
                "evidence_rationale": "Fixture evidence supports option B instead.",
            },
        ],
        "evidence_sufficient_for_decision": True,
        "abstention_reason": None,
    }


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
                    "Profiles are a Salesforce feature that enable profile-based defaults and configuration settings for users.",
                    '{"domain": "Configuration"}',
                ),
                (
                    resource2,
                    rv2,
                    chunk2,
                    hash2,
                    "Roles Overview",
                    "Roles define record-level access and hierarchy visibility.",
                    '{"topic": "roles hierarchy"}',
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
            elif _is_correctness_sub_call(kwargs):
                body = _correctness_agrees_with_stored_key(self.fixture["chunk1"])
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
            if _is_correctness_sub_call(kwargs):
                # The specialist independently disagrees with the stored
                # key (V60: WRONG_ANSWER_KEY is now exclusively
                # specialist-derived, not general-judge-proposed).
                calls["b"] += 1
                return LlmResponse(
                    parsed_response=_correctness_disagrees_with_stored_key(
                        self.fixture["chunk1"]
                    ),
                    input_tokens=1,
                    output_tokens=1,
                )
            return LlmResponse(
                parsed_response={"selected_option_labels": ["A"], "proposed_findings": []},
                input_tokens=1,
                output_tokens=1,
            )

        def dispute(**kwargs):
            return LlmResponse(
                parsed_response={
                    "resolution_type": "NORMAL_DISPUTE",
                    "resolution_status": "RESOLVED",
                    "substituted_for_passes": [],
                    "confirmed_finding_refs": ["FC1"],
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
            if _is_correctness_sub_call(kwargs):
                return LlmResponse(
                    parsed_response=_correctness_agrees_with_stored_key(
                        self.fixture["chunk1"]
                    ),
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
            if _is_correctness_sub_call(kwargs):
                # The specialist succeeds and agrees with the stored key on
                # every attempt; only the general judge's response is
                # schema-invalid on the first attempt.
                return LlmResponse(
                    parsed_response=_correctness_agrees_with_stored_key(
                        self.fixture["chunk1"]
                    ),
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
            if _is_correctness_sub_call(kwargs):
                return LlmResponse(
                    parsed_response=_correctness_agrees_with_stored_key(
                        self.fixture["chunk1"]
                    ),
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

    # -------------------------------------------------------------------
    # V58-DAY7-IMPROVE-06: Pass C UNRESOLVED must preserve the disputed
    # Pass B blocking proposal instead of discarding it.
    # -------------------------------------------------------------------

    def _unresolved_single_blocking_providers(self):
        def primary(**kwargs):
            pass_code = (kwargs.get("metadata") or {}).get("pass_code")
            if pass_code == "A":
                return LlmResponse(
                    parsed_response={"selected_option_labels": ["A"]},
                    input_tokens=1,
                    output_tokens=1,
                )
            if _is_correctness_sub_call(kwargs):
                # V60: WRONG_ANSWER_KEY is now exclusively specialist-derived
                # (finding_ref defaults to "FC1"), not general-judge-proposed.
                return LlmResponse(
                    parsed_response=_correctness_disagrees_with_stored_key(
                        self.fixture["chunk1"]
                    ),
                    input_tokens=1,
                    output_tokens=1,
                )
            return LlmResponse(
                parsed_response={"selected_option_labels": ["A"], "proposed_findings": []},
                input_tokens=1,
                output_tokens=1,
            )

        def dispute(**kwargs):
            return LlmResponse(
                parsed_response={
                    "resolution_type": "NORMAL_DISPUTE",
                    "resolution_status": "UNRESOLVED",
                    "substituted_for_passes": [],
                    "confirmed_finding_refs": [],
                },
                input_tokens=1,
                output_tokens=1,
            )

        return AiQualityAuditProviders(primary=primary, dispute=dispute)

    def test_unresolved_dispute_preserves_disputed_blocking_finding(self):
        audit_run_id = self._create_run()
        summary = process_ai_quality_audit_job(
            self.client,
            {
                "audit_run_id": audit_run_id,
                "question_version_id": self.fixture["question_version_id"],
            },
            self._unresolved_single_blocking_providers(),
            worker_id="v48-integration-worker",
        )

        self.assertEqual(summary["run_status"], "inconclusive")
        self.assertIn("C", summary["passes_executed"])

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT run_status, completed_at FROM public.audit_runs WHERE id = %s",
                (audit_run_id,),
            )
            run_row = cur.fetchone()
            cur.execute(
                """
                SELECT finding_code, finding_type, severity, materiality,
                       finding_status, title, description, metadata
                FROM public.audit_findings
                WHERE audit_run_id = %s
                """,
                (audit_run_id,),
            )
            finding_rows = cur.fetchall()

        self.assertEqual(run_row["run_status"], "inconclusive")
        self.assertIsNotNone(run_row["completed_at"])

        self.assertEqual(len(finding_rows), 1)
        finding = finding_rows[0]
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")
        self.assertEqual(finding["finding_type"], "correctness")
        self.assertEqual(finding["materiality"], "blocking")
        self.assertEqual(finding["finding_status"], "open")
        self.assertNotIn(finding["finding_status"], ("accepted", "resolved", "overridden"))

        metadata = finding["metadata"]
        self.assertEqual(metadata["dispute_resolution_status"], "UNRESOLVED")
        self.assertIs(metadata["pass_c_confirmed"], False)
        self.assertIs(metadata["requires_human_review"], True)
        self.assertEqual(metadata["source_pass_code"], "B")
        self.assertEqual(metadata["finding_ref"], "FC1")
        self.assertEqual(metadata["completion_shape"], "NORMAL_DISPUTE")

    def test_unresolved_dispute_preserves_evidence_and_provenance(self):
        audit_run_id = self._create_run()
        summary = process_ai_quality_audit_job(
            self.client,
            {
                "audit_run_id": audit_run_id,
                "question_version_id": self.fixture["question_version_id"],
            },
            self._unresolved_single_blocking_providers(),
            worker_id="v48-integration-worker",
        )
        self.assertEqual(summary["run_status"], "inconclusive")

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT af.detector_name, af.detector_version,
                       afe.resource_chunk_id, afe.evidence_role
                FROM public.audit_findings af
                JOIN public.audit_finding_evidence afe ON afe.finding_id = af.id
                WHERE af.audit_run_id = %s
                """,
                (audit_run_id,),
            )
            evidence_rows = cur.fetchall()

        self.assertEqual(len(evidence_rows), 1)
        self.assertEqual(evidence_rows[0]["detector_name"], "ai_quality_audit")
        self.assertEqual(evidence_rows[0]["detector_version"], "NORMAL_DISPUTE")
        self.assertEqual(
            str(evidence_rows[0]["resource_chunk_id"]).lower(),
            self.fixture["chunk1"].lower(),
        )
        self.assertEqual(evidence_rows[0]["evidence_role"], "supporting")

    def test_unresolved_dispute_does_not_persist_unrelated_pass_b_findings(self):
        audit_run_id = self._create_run()

        def primary(**kwargs):
            pass_code = (kwargs.get("metadata") or {}).get("pass_code")
            if pass_code == "A":
                return LlmResponse(
                    parsed_response={"selected_option_labels": ["A"]},
                    input_tokens=1,
                    output_tokens=1,
                )
            if _is_correctness_sub_call(kwargs):
                # V60: WRONG_ANSWER_KEY (finding_ref="FC1") is now
                # exclusively specialist-derived.
                return LlmResponse(
                    parsed_response=_correctness_disagrees_with_stored_key(
                        self.fixture["chunk1"]
                    ),
                    input_tokens=1,
                    output_tokens=1,
                )
            return LlmResponse(
                parsed_response={
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        {
                            "finding_ref": "F2",
                            "finding_code": "WEAK_DISTRACTORS",
                            "finding_type": "answer_quality",
                            "severity": "low",
                            "materiality": "warning",
                            "title": "Weak distractor",
                            "description": "Option B is not competitive.",
                            "evidence_chunk_ids": [],
                            "metadata": {},
                        },
                    ],
                },
                input_tokens=1,
                output_tokens=1,
            )

        def dispute(**kwargs):
            return LlmResponse(
                parsed_response={
                    "resolution_type": "NORMAL_DISPUTE",
                    "resolution_status": "UNRESOLVED",
                    "substituted_for_passes": [],
                    "confirmed_finding_refs": [],
                },
                input_tokens=1,
                output_tokens=1,
            )

        summary = process_ai_quality_audit_job(
            self.client,
            {
                "audit_run_id": audit_run_id,
                "question_version_id": self.fixture["question_version_id"],
            },
            AiQualityAuditProviders(primary=primary, dispute=dispute),
            worker_id="v48-integration-worker",
        )
        self.assertEqual(summary["run_status"], "inconclusive")

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT finding_code, metadata FROM public.audit_findings WHERE audit_run_id = %s",
                (audit_run_id,),
            )
            rows = cur.fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["finding_code"], "WRONG_ANSWER_KEY")
        self.assertEqual(rows[0]["metadata"]["finding_ref"], "FC1")

    def test_unresolved_dispute_blocks_publication(self):
        audit_run_id = self._create_run()
        summary = process_ai_quality_audit_job(
            self.client,
            {
                "audit_run_id": audit_run_id,
                "question_version_id": self.fixture["question_version_id"],
            },
            self._unresolved_single_blocking_providers(),
            worker_id="v48-integration-worker",
        )
        self.assertEqual(summary["run_status"], "inconclusive")

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT public.is_question_version_publishable_v1(%s)",
                (self.fixture["question_version_id"],),
            )
            publishable = cur.fetchone()[0]
            cur.execute(
                "SELECT public.count_blocking_findings_for_question_version_v1(%s)",
                (self.fixture["question_version_id"],),
            )
            blocking_count = cur.fetchone()[0]

        self.assertFalse(publishable)
        self.assertEqual(blocking_count, 1)

    def test_repeated_completion_after_unresolved_does_not_duplicate_findings(self):
        audit_run_id = self._create_run()
        summary = process_ai_quality_audit_job(
            self.client,
            {
                "audit_run_id": audit_run_id,
                "question_version_id": self.fixture["question_version_id"],
            },
            self._unresolved_single_blocking_providers(),
            worker_id="v48-integration-worker",
        )
        self.assertEqual(summary["run_status"], "inconclusive")

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.audit_findings WHERE audit_run_id = %s",
                (audit_run_id,),
            )
            count_before = cur.fetchone()[0]
        self.assertEqual(count_before, 1)

        with self.conn.cursor() as cur:
            cur.execute("SAVEPOINT repeat_completion_attempt")

        raised = False
        try:
            self.client.rpc(
                "complete_ai_quality_audit_run_v1",
                {
                    "p_audit_run_id": audit_run_id,
                    "p_confirmed_findings": [],
                    "p_metadata": {},
                },
            ).execute()
        except psycopg2.Error as exc:
            raised = True
            self.assertIn("inconclusive", str(exc).lower())
        finally:
            with self.conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT repeat_completion_attempt")

        self.assertTrue(raised, "expected re-completing an inconclusive run to raise")

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.audit_findings WHERE audit_run_id = %s",
                (audit_run_id,),
            )
            count_after = cur.fetchone()[0]
        self.assertEqual(count_after, 1)

    def test_normal_dispute_resolved_behavior_unchanged(self):
        audit_run_id = self._create_run()

        def primary(**kwargs):
            pass_code = (kwargs.get("metadata") or {}).get("pass_code")
            if pass_code == "A":
                return LlmResponse(
                    parsed_response={"selected_option_labels": ["A"]},
                    input_tokens=1,
                    output_tokens=1,
                )
            if _is_correctness_sub_call(kwargs):
                # V60: WRONG_ANSWER_KEY (finding_ref="FC1") is now
                # exclusively specialist-derived.
                return LlmResponse(
                    parsed_response=_correctness_disagrees_with_stored_key(
                        self.fixture["chunk1"]
                    ),
                    input_tokens=1,
                    output_tokens=1,
                )
            return LlmResponse(
                parsed_response={"selected_option_labels": ["A"], "proposed_findings": []},
                input_tokens=1,
                output_tokens=1,
            )

        def dispute(**kwargs):
            return LlmResponse(
                parsed_response={
                    "resolution_type": "NORMAL_DISPUTE",
                    "resolution_status": "RESOLVED",
                    "substituted_for_passes": [],
                    "confirmed_finding_refs": ["FC1"],
                },
                input_tokens=1,
                output_tokens=1,
            )

        summary = process_ai_quality_audit_job(
            self.client,
            {
                "audit_run_id": audit_run_id,
                "question_version_id": self.fixture["question_version_id"],
            },
            AiQualityAuditProviders(primary=primary, dispute=dispute),
            worker_id="v48-integration-worker",
        )

        self.assertEqual(summary["run_status"], "completed")
        self.assertEqual(summary["finding_count"], 1)

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT finding_status, materiality, metadata FROM public.audit_findings WHERE audit_run_id = %s",
                (audit_run_id,),
            )
            rows = cur.fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["materiality"], "blocking")
        # RESOLVED-path findings must never carry the UNRESOLVED-only dispute
        # markers this migration introduces.
        self.assertNotIn("dispute_resolution_status", rows[0]["metadata"])
        self.assertNotIn("pass_c_confirmed", rows[0]["metadata"])
        self.assertNotIn("requires_human_review", rows[0]["metadata"])

    def test_normal_no_dispute_warning_persistence_unchanged(self):
        audit_run_id = self._create_run()

        def primary(**kwargs):
            pass_code = (kwargs.get("metadata") or {}).get("pass_code")
            if pass_code == "A":
                return LlmResponse(
                    parsed_response={"selected_option_labels": ["A"]},
                    input_tokens=1,
                    output_tokens=1,
                )
            if _is_correctness_sub_call(kwargs):
                return LlmResponse(
                    parsed_response=_correctness_agrees_with_stored_key(
                        self.fixture["chunk1"]
                    ),
                    input_tokens=1,
                    output_tokens=1,
                )
            return LlmResponse(
                parsed_response={
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        {
                            "finding_ref": "F1",
                            "finding_code": "WEAK_DISTRACTORS",
                            "finding_type": "answer_quality",
                            "severity": "low",
                            "materiality": "warning",
                            "title": "Weak distractor",
                            "description": "Option B is not competitive.",
                            "evidence_chunk_ids": [],
                            "metadata": {},
                        }
                    ],
                },
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
        self.assertEqual(summary["finding_count"], 1)
        self.assertNotIn("C", summary["passes_executed"])

        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT finding_code, materiality, finding_status FROM public.audit_findings WHERE audit_run_id = %s",
                (audit_run_id,),
            )
            rows = cur.fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["finding_code"], "WEAK_DISTRACTORS")
        self.assertEqual(rows[0]["materiality"], "warning")
        self.assertEqual(rows[0]["finding_status"], "open")


if __name__ == "__main__":
    unittest.main()
