"""
Tests for workers.quality_benchmark_v48_orchestration (V58-QUALITY-04C).

Two tiers:

1. Pure/offline tests (always run, no Docker/database required): DSN safety
   validation, opt-in gating, evidence-availability guards, and
   ``V48EngineAdapter`` safe-default behavior. These never open a socket.

2. Docker-gated integration tests (``TestV48DisposableDbOrchestration``),
   following the exact skip convention already used by
   ``tests/test_ai_quality_audit_integration.py``: skipped unless
   ``V48_TEST_DATABASE_URL`` is explicitly set or a local disposable
   Postgres is reachable at the documented default. These exercise the real,
   unmodified V48 worker/RPC pipeline end-to-end and prove zero rows survive
   after execution, in any outcome (success, provider failure, worker
   failure, seeding/validation failure, missing evidence).

Existing V48 worker-behavior scenarios (no-dispute completion, resolved
dispute, malformed Pass A, Pass B retry, WAIT coordination, smoke-evidence
freezing) are intentionally NOT duplicated here — they remain covered by
``tests/test_ai_quality_audit_integration.py``, which now imports the shared
``PsycopgV48Client`` from ``workers.v48_psycopg_client`` instead of defining
its own copy. This file covers only the new benchmark-seeding/DSN-safety/
rollback-proof surface introduced by this task.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore

from workers.ai_quality_audit_worker import AiQualityAuditProviders
from workers.llm_providers import LlmResponse
from workers.quality_benchmark import load_benchmark_fixture
from workers.quality_benchmark_execution import (
    ENGINE_V48,
    CasePrediction,
    EngineAdapterUnavailableError,
    V48EngineAdapter,
)
from workers.quality_benchmark_v48_orchestration import (
    ALLOWED_DISPOSABLE_DB_NAME_RE,
    ALLOWED_DISPOSABLE_HOSTS,
    V48DatabaseUnavailableError,
    V48DisposableDsnRejectedError,
    V48EvidenceUnavailableError,
    V48MigrationCompatibilityError,
    generate_v48_prediction,
    open_disposable_v48_connection,
    run_v48_benchmark_case,
    seed_benchmark_case,
    v48_disposable_transaction,
    validate_disposable_dsn,
    verify_v48_schema_compatibility,
)

_DSN_ENV = "V48_TEST_DATABASE_URL"
_DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test"

_DEFAULT_FIXTURE_PATH = "workers/fixtures/quality_benchmark_v1.json"


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


def _one_real_case() -> dict:
    fixture = load_benchmark_fixture(_DEFAULT_FIXTURE_PATH)
    return fixture["cases"][0]


def _always_a_provider(**kwargs):
    pass_code = (kwargs.get("metadata") or {}).get("pass_code")
    if pass_code == "A":
        body = {"selected_option_labels": ["A"]}
    else:
        body = {"selected_option_labels": ["A"], "proposed_findings": []}
    return LlmResponse(parsed_response=body, input_tokens=1, output_tokens=1)


def _no_dispute_providers() -> AiQualityAuditProviders:
    return AiQualityAuditProviders(primary=_always_a_provider, dispute=_always_a_provider)


# ===========================================================================
# Tier 1: pure/offline tests — no Docker, no network, no database
# ===========================================================================


class TestDisposableDsnValidation(unittest.TestCase):
    """Mandatory coverage items 1-3: production/ambiguous rejected, valid accepted."""

    def test_production_supabase_host_rejected(self):
        with self.assertRaises(V48DisposableDsnRejectedError) as ctx:
            validate_disposable_dsn(
                "postgresql://postgres:secret@db.abcxyz.supabase.co:5432/postgres"
            )
        self.assertIn("production-host marker", str(ctx.exception))

    def test_non_loopback_host_rejected(self):
        with self.assertRaises(V48DisposableDsnRejectedError):
            validate_disposable_dsn("postgresql://postgres:postgres@db.internal.example/certbound_v48_test")

    def test_ambiguous_database_name_rejected(self):
        with self.assertRaises(V48DisposableDsnRejectedError) as ctx:
            validate_disposable_dsn("postgresql://postgres:postgres@127.0.0.1:5432/production")
        self.assertIn("naming pattern", str(ctx.exception))

    def test_spoofed_test_marker_in_credentials_does_not_bypass_dbname_check(self):
        # "test" appears in the username/host substring but the *parsed*
        # database name is what's checked, and it is not on the allow-list.
        with self.assertRaises(V48DisposableDsnRejectedError):
            validate_disposable_dsn("postgresql://test_user:test_pw@127.0.0.1:5432/mydb_test_thing")

    def test_spoofed_test_marker_in_query_param_does_not_bypass_host_check(self):
        with self.assertRaises(V48DisposableDsnRejectedError):
            validate_disposable_dsn(
                "postgresql://postgres:postgres@not-localhost.example/certbound_v48_test?env=test"
            )

    def test_empty_dsn_rejected(self):
        with self.assertRaises(V48DisposableDsnRejectedError):
            validate_disposable_dsn("")
        with self.assertRaises(V48DisposableDsnRejectedError):
            validate_disposable_dsn(None)

    def test_non_postgres_scheme_rejected(self):
        with self.assertRaises(V48DisposableDsnRejectedError):
            validate_disposable_dsn("mysql://postgres:postgres@127.0.0.1:5432/certbound_v48_test")

    def test_explicit_disposable_test_database_accepted(self):
        info = validate_disposable_dsn(_DEFAULT_DSN)
        self.assertIn(info.host, ALLOWED_DISPOSABLE_HOSTS)
        self.assertTrue(ALLOWED_DISPOSABLE_DB_NAME_RE.match(info.dbname))
        self.assertEqual(info.dbname, "certbound_v48_test")

    def test_disposable_db_name_with_approved_suffix_accepted(self):
        info = validate_disposable_dsn(
            "postgresql://postgres:postgres@localhost:5432/certbound_v48_test_ci"
        )
        self.assertEqual(info.dbname, "certbound_v48_test_ci")


class TestOptInGating(unittest.TestCase):
    """Mandatory coverage item 15: safe default remains non-live without opt-in."""

    def test_open_connection_without_opt_in_refuses_before_any_network_call(self):
        with self.assertRaises(V48DisposableDsnRejectedError) as ctx:
            open_disposable_v48_connection(_DEFAULT_DSN, allow_disposable_v48_db=False)
        self.assertIn("explicit opt-in", str(ctx.exception))

    def test_v48_engine_adapter_default_constructor_stays_blocked(self):
        adapter = V48EngineAdapter()
        self.assertFalse(adapter._is_opted_in())
        with self.assertRaises(EngineAdapterUnavailableError):
            adapter.generate_prediction(_one_real_case())

    def test_v48_engine_adapter_opt_in_without_dsn_stays_blocked(self):
        adapter = V48EngineAdapter(allow_disposable_db=True, providers=_no_dispute_providers())
        self.assertFalse(adapter._is_opted_in())
        with self.assertRaises(EngineAdapterUnavailableError):
            adapter.generate_prediction(_one_real_case())

    def test_v48_engine_adapter_opt_in_without_providers_stays_blocked(self):
        adapter = V48EngineAdapter(allow_disposable_db=True, disposable_db_url=_DEFAULT_DSN)
        self.assertFalse(adapter._is_opted_in())
        with self.assertRaises(EngineAdapterUnavailableError):
            adapter.generate_prediction(_one_real_case())

    def test_v48_engine_adapter_describe_config_blocked_by_default(self):
        self.assertEqual(V48EngineAdapter().describe_config()["status"], "blocked")


class TestEvidenceAvailabilityGuard(unittest.TestCase):
    """Mandatory coverage item 9: empty/invalid evidence fails closed.

    These never touch a real connection: the empty/invalid-evidence guard
    in ``seed_benchmark_case`` runs before any SQL is issued, so passing
    ``conn=None`` is sufficient to prove it fails closed without ever
    reaching the database.
    """

    def _case(self, **overrides) -> dict:
        base = dict(_one_real_case())
        base.update(overrides)
        return base

    def test_case_with_no_chunks_raises_before_touching_connection(self):
        case = self._case(resource_snapshot={"chunks": []})
        with self.assertRaises(V48EvidenceUnavailableError) as ctx:
            seed_benchmark_case(None, case)
        self.assertIn("resource_snapshot.chunks", str(ctx.exception))

    def test_case_with_missing_resource_snapshot_raises(self):
        case = dict(_one_real_case())
        del case["resource_snapshot"]
        with self.assertRaises(V48EvidenceUnavailableError):
            seed_benchmark_case(None, case)

    def test_case_with_chunk_missing_content_hash_raises(self):
        case = _one_real_case()
        chunk = dict(case["resource_snapshot"]["chunks"][0])
        chunk.pop("content_hash", None)
        case = self._case(resource_snapshot={"chunks": [chunk]})
        with self.assertRaises(V48EvidenceUnavailableError) as ctx:
            seed_benchmark_case(None, case)
        self.assertIn("content_hash", str(ctx.exception))

    def test_case_with_chunk_missing_chunk_text_raises(self):
        case = _one_real_case()
        chunk = dict(case["resource_snapshot"]["chunks"][0])
        chunk["chunk_text"] = ""
        case = self._case(resource_snapshot={"chunks": [chunk]})
        with self.assertRaises(V48EvidenceUnavailableError):
            seed_benchmark_case(None, case)

    def test_case_with_no_options_raises(self):
        case = _one_real_case()
        question = dict(case["question"])
        question["options"] = []
        case = self._case(question=question)
        with self.assertRaises(V48EvidenceUnavailableError):
            seed_benchmark_case(None, case)


class TestDatabaseUnavailableIsClean(unittest.TestCase):
    """Mandatory coverage item 14: disposable DB unavailable -> clean blocked
    result, never a raw stack trace, regardless of whether Docker/the real
    disposable database is running in this environment.
    """

    _UNREACHABLE_DSN = "postgresql://postgres:postgres@127.0.0.1:1/certbound_v48_test"

    def test_connect_to_unreachable_disposable_db_raises_clean_error(self):
        if psycopg2 is None:
            self.skipTest("psycopg2 is not installed")
        with self.assertRaises(V48DatabaseUnavailableError) as ctx:
            open_disposable_v48_connection(self._UNREACHABLE_DSN, allow_disposable_v48_db=True)
        # Never leaks the DSN (credentials) — only redacted host/type info.
        self.assertNotIn("postgres:postgres", str(ctx.exception))

    def test_generate_v48_prediction_reports_unavailable_db_as_engine_blocked(self):
        if psycopg2 is None:
            self.skipTest("psycopg2 is not installed")
        with self.assertRaises(EngineAdapterUnavailableError) as ctx:
            generate_v48_prediction(
                _one_real_case(),
                dsn=self._UNREACHABLE_DSN,
                allow_disposable_v48_db=True,
                providers=_no_dispute_providers(),
            )
        self.assertIn("unavailable", ctx.exception.reason.lower())
        self.assertTrue(ctx.exception.follow_up)


# ===========================================================================
# Tier 2: Docker-gated integration tests (real disposable database)
# ===========================================================================


@unittest.skipUnless(psycopg2 is not None, "psycopg2 is not installed")
class TestV48DisposableDbOrchestration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _dsn_explicitly_configured():
            return
        if not _can_connect():
            raise unittest.SkipTest(
                f"V48 Docker database unavailable at {_dsn()!r}; skipping integration test"
            )

    def _count_matching_questions(self) -> int:
        conn = psycopg2.connect(_dsn())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM public.questions WHERE exam_name LIKE 'Salesforce Certified%'"
                )
                return cur.fetchone()[0]
        finally:
            conn.close()

    def _count_all_relevant_rows(self) -> dict:
        conn = psycopg2.connect(_dsn())
        counts = {}
        try:
            with conn.cursor() as cur:
                for table in (
                    "questions",
                    "question_versions",
                    "question_option_versions",
                    "official_resources",
                    "resource_versions",
                    "resource_chunks",
                    "audit_runs",
                    "audit_run_pass_results",
                    "audit_run_evidence_set",
                    "audit_findings",
                ):
                    cur.execute(f"SELECT COUNT(*) FROM public.{table}")
                    counts[table] = cur.fetchone()[0]
        finally:
            conn.close()
        return counts

    # -- Schema/migration compatibility (mandatory coverage items 4-5) -----

    def test_missing_required_table_blocks_execution(self):
        conn = psycopg2.connect(_dsn())
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
            import workers.quality_benchmark_v48_orchestration as orch

            original = orch.REQUIRED_V48_TABLES
            orch.REQUIRED_V48_TABLES = original + ("definitely_not_a_real_table_xyz",)
            try:
                with self.assertRaises(V48MigrationCompatibilityError) as ctx:
                    verify_v48_schema_compatibility(conn)
                self.assertIn("definitely_not_a_real_table_xyz", str(ctx.exception))
            finally:
                orch.REQUIRED_V48_TABLES = original
        finally:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK")
            conn.close()

    def test_missing_required_rpc_blocks_execution(self):
        conn = psycopg2.connect(_dsn())
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
            import workers.quality_benchmark_v48_orchestration as orch

            original = orch.REQUIRED_V48_RPCS
            orch.REQUIRED_V48_RPCS = original + ("definitely_not_a_real_rpc_xyz",)
            try:
                with self.assertRaises(V48MigrationCompatibilityError) as ctx:
                    verify_v48_schema_compatibility(conn)
                self.assertIn("definitely_not_a_real_rpc_xyz", str(ctx.exception))
            finally:
                orch.REQUIRED_V48_RPCS = original
        finally:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK")
            conn.close()

    def test_real_disposable_db_passes_schema_compatibility(self):
        conn = psycopg2.connect(_dsn())
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
            verify_v48_schema_compatibility(conn)  # must not raise
        finally:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK")
            conn.close()

    # -- End-to-end execution (mandatory coverage items 6-8) ----------------

    def test_one_benchmark_case_completes_through_real_v48_worker(self):
        case = _one_real_case()
        prediction = run_v48_benchmark_case(
            case,
            dsn=_dsn(),
            allow_disposable_v48_db=True,
            providers=_no_dispute_providers(),
        )
        self.assertIsInstance(prediction, CasePrediction)
        self.assertEqual(prediction.case_id, case["case_id"])
        self.assertIsNone(prediction.error)
        self.assertEqual(prediction.raw_output.get("run_status"), "completed")
        self.assertIn("A", prediction.raw_output.get("passes_executed") or [])
        self.assertIn("B", prediction.raw_output.get("passes_executed") or [])

    def test_v48_engine_adapter_end_to_end_opt_in(self):
        case = _one_real_case()
        adapter = V48EngineAdapter(
            allow_disposable_db=True,
            disposable_db_url=_dsn(),
            providers=_no_dispute_providers(),
        )
        self.assertTrue(adapter._is_opted_in())
        prediction = adapter.generate_prediction(case)
        self.assertEqual(prediction.case_id, case["case_id"])
        self.assertIsNone(prediction.error)

    # -- Evidence integrity (mandatory coverage item 8) ---------------------

    def test_evidence_hashes_and_provenance_remain_valid(self):
        case = _one_real_case()
        expected_chunk_ids = {
            str(c["resource_chunk_id"]).lower() for c in case["resource_snapshot"]["chunks"]
        }

        with v48_disposable_transaction(_dsn(), allow_disposable_v48_db=True) as (conn, client):
            seeded, evidence_payload = seed_benchmark_case(conn, case)
            self.assertEqual(seeded.evidence_chunk_count, len(expected_chunk_ids))
            payload_chunk_ids = {entry["resource_chunk_id"] for entry in evidence_payload}
            self.assertEqual(payload_chunk_ids, expected_chunk_ids)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content_hash FROM public.resource_chunks WHERE id = ANY(%s::uuid[])",
                    (list(expected_chunk_ids),),
                )
                stored_hashes = {row[0] for row in cur.fetchall()}
            expected_hashes = {
                str(c["content_hash"]) for c in case["resource_snapshot"]["chunks"]
            }
            self.assertEqual(stored_hashes, expected_hashes)
        # Transaction always rolled back on exit — no explicit assertion
        # needed here (see rollback-proof tests below), but exiting cleanly
        # without raising already proves the context manager's own
        # ROLLBACK/close path did not error.

    # -- Empty/invalid evidence fails closed (mandatory coverage item 9) ---

    def test_case_with_empty_evidence_fails_closed_via_full_orchestration(self):
        case = dict(_one_real_case())
        case["resource_snapshot"] = {"chunks": []}
        prediction = run_v48_benchmark_case(
            case,
            dsn=_dsn(),
            allow_disposable_v48_db=True,
            providers=_no_dispute_providers(),
        )
        self.assertIsNotNone(prediction.error)
        self.assertIn("resource_snapshot.chunks", prediction.error)

    # -- Rollback proof (mandatory coverage items 10-13) --------------------

    def test_successful_execution_leaves_zero_persistent_rows(self):
        before = self._count_all_relevant_rows()
        prediction = run_v48_benchmark_case(
            _one_real_case(),
            dsn=_dsn(),
            allow_disposable_v48_db=True,
            providers=_no_dispute_providers(),
        )
        self.assertIsNone(prediction.error)
        after = self._count_all_relevant_rows()
        self.assertEqual(before, after)

    def test_provider_failure_leaves_zero_persistent_rows(self):
        def failing_provider(**kwargs):
            raise RuntimeError("simulated provider failure")

        providers = AiQualityAuditProviders(primary=failing_provider, dispute=failing_provider)
        before = self._count_all_relevant_rows()
        prediction = run_v48_benchmark_case(
            _one_real_case(),
            dsn=_dsn(),
            allow_disposable_v48_db=True,
            providers=providers,
        )
        self.assertIsNotNone(prediction.error)
        after = self._count_all_relevant_rows()
        self.assertEqual(before, after)

    def test_worker_failure_leaves_zero_persistent_rows(self):
        # A provider that returns a structurally invalid response for every
        # pass drives the real worker into repeated schema_invalid/failed
        # states; whatever the outcome, the transaction must still roll back
        # completely.
        def malformed_provider(**kwargs):
            return LlmResponse(parsed_response={"not_a_valid_field": True}, input_tokens=1, output_tokens=1)

        providers = AiQualityAuditProviders(primary=malformed_provider, dispute=malformed_provider)
        before = self._count_all_relevant_rows()
        run_v48_benchmark_case(
            _one_real_case(),
            dsn=_dsn(),
            allow_disposable_v48_db=True,
            providers=providers,
        )
        after = self._count_all_relevant_rows()
        self.assertEqual(before, after)

    def test_seeding_validation_failure_leaves_zero_persistent_rows(self):
        case = dict(_one_real_case())
        case["resource_snapshot"] = {"chunks": []}
        before = self._count_all_relevant_rows()
        prediction = run_v48_benchmark_case(
            case,
            dsn=_dsn(),
            allow_disposable_v48_db=True,
            providers=_no_dispute_providers(),
        )
        self.assertIsNotNone(prediction.error)
        after = self._count_all_relevant_rows()
        self.assertEqual(before, after)

    def test_independent_cases_do_not_collide_and_both_roll_back(self):
        fixture = load_benchmark_fixture(_DEFAULT_FIXTURE_PATH)
        case_a, case_b = fixture["cases"][0], fixture["cases"][1]
        before = self._count_all_relevant_rows()
        pred_a = run_v48_benchmark_case(
            case_a, dsn=_dsn(), allow_disposable_v48_db=True, providers=_no_dispute_providers()
        )
        pred_b = run_v48_benchmark_case(
            case_b, dsn=_dsn(), allow_disposable_v48_db=True, providers=_no_dispute_providers()
        )
        after = self._count_all_relevant_rows()
        self.assertIsNone(pred_a.error)
        self.assertIsNone(pred_b.error)
        self.assertNotEqual(pred_a.case_id, pred_b.case_id)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
