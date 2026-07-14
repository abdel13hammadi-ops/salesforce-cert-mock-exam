"""
Focused tests for official-evidence fixture ingestion (PAB-EXP-04C).

Uses ephemeral pgserver PostgreSQL with repository migration SQL adapted for
environments without pgcrypto. No network access or production credentials.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore

try:
    import pgserver
except ImportError:  # pragma: no cover
    pgserver = None  # type: ignore

from workers.ai_quality_audit_evidence import prepare_smoke_evidence_set
from workers.certification_registry import PAB_EXAM_NAME
from workers.official_evidence_fixture_ingestion import (
    ALLOW_APPROVED_SUPABASE_INGEST_FLAG,
    ALLOW_FIXTURE_INGEST_FLAG,
    APPROVED_HOSTED_TARGET_CERTIFICATION,
    APPROVED_HOSTED_TARGET_FIXTURE_VERSION,
    APPROVED_HOSTED_TARGET_RECORD_COUNT,
    OfficialEvidenceFixtureIngestionConflictError,
    OfficialEvidenceFixtureIngestionError,
    OfficialEvidenceFixtureIngestionSafetyError,
    assert_approved_hosted_supabase_target,
    assert_fixture_ingest_allowed,
    enforce_dsn_target_safety,
    ingest_official_evidence_fixture_package,
    is_hosted_supabase_dsn,
    load_fixture_for_ingestion,
    reject_production_like_dsn,
    resolve_package_config,
    validate_fixture_for_ingestion,
)
from workers.official_evidence_seed import (
    ADM_EXAM_NAME,
    BA_EXAM_NAME,
    DEFAULT_OUTPUT_PATH,
    PAB_DEFAULT_OUTPUT_PATH,
    PAB_FIXTURE_VERSION,
)
from workers.v48_psycopg_client import PsycopgV48Client

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NEW_UUID_SQL = "md5(random()::text || clock_timestamp()::text)::uuid"
_PAB_DOMAINS = (
    "App Deployment",
    "Business Logic and Process Automation",
    "Data Modeling and Management",
    "Salesforce Fundamentals",
    "User Interface",
)


def _substitute_uuid_sql(sql: str) -> str:
    return (
        sql.replace("DEFAULT gen_random_uuid()", f"DEFAULT ({_NEW_UUID_SQL})")
        .replace("gen_random_uuid()", _NEW_UUID_SQL)
    )


def _read_migration(name: str) -> str:
    return (_REPO_ROOT / "supabase" / "migrations" / name).read_text(encoding="utf-8")


def _extract_sql_function(source: str, function_name: str) -> str:
    marker = f"CREATE OR REPLACE FUNCTION public.{function_name}"
    start = source.index(marker)
    dollar = source.index("$$", start)
    end = source.index("$$", dollar + 2) + 2
    return source[start : end + 1]


_MINIMAL_QUESTION_DDL = """
CREATE TABLE IF NOT EXISTS public.questions (
    id integer PRIMARY KEY,
    exam_name text NOT NULL,
    category text,
    difficulty text,
    question_text text NOT NULL,
    question_type text NOT NULL,
    select_count integer NOT NULL,
    explanation text,
    is_active boolean NOT NULL DEFAULT true,
    is_exam_eligible boolean NOT NULL DEFAULT true,
    language_code text NOT NULL DEFAULT 'en'
);

CREATE TABLE IF NOT EXISTS public.question_versions (
    id uuid PRIMARY KEY,
    question_id integer NOT NULL REFERENCES public.questions(id),
    version_number integer NOT NULL,
    question_text text NOT NULL,
    explanation text,
    category text,
    difficulty text,
    question_type text NOT NULL,
    select_count integer NOT NULL,
    language_code text NOT NULL DEFAULT 'en',
    content_hash text NOT NULL,
    source_type text NOT NULL DEFAULT 'manual',
    created_by text NOT NULL,
    UNIQUE (question_id, version_number)
);

CREATE TABLE IF NOT EXISTS public.question_option_versions (
    question_version_id uuid NOT NULL REFERENCES public.question_versions(id),
    option_label text NOT NULL,
    option_text text NOT NULL,
    display_order integer NOT NULL,
    is_correct boolean NOT NULL
);
"""


def _sanitize_pgserver_sql(sql: str) -> str:
    sql = re.sub(r"COMMENT ON .*?;\s*", "", sql, flags=re.DOTALL | re.IGNORECASE)
    sql = re.sub(r"REVOKE .*?;\s*", "", sql, flags=re.DOTALL | re.IGNORECASE)
    sql = re.sub(r"GRANT .*?;\s*", "", sql, flags=re.DOTALL | re.IGNORECASE)
    sql = re.sub(
        r"CREATE EXTENSION IF NOT EXISTS pgcrypto;\s*",
        "",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def _patch_ingest_rpc_for_pgserver(sql: str) -> str:
    return sql.replace(
        "FROM   public.resource_chunks\n        WHERE  resource_version_id = v_version_id",
        "FROM   public.resource_chunks rc\n        WHERE  rc.resource_version_id = v_version_id",
    )


def bootstrap_fixture_ingestion_schema(conn) -> None:
    ingest_sql = _patch_ingest_rpc_for_pgserver(
        _read_migration("20260623234600_v44_ingest_resource_version_rpc.sql")
    )
    parts = [
        _sanitize_pgserver_sql(
            _substitute_uuid_sql(
                _read_migration("20260623233800_v44_resource_library_foundation.sql")
            )
        ),
        _sanitize_pgserver_sql(_substitute_uuid_sql(ingest_sql)),
        _MINIMAL_QUESTION_DDL,
    ]
    v48 = _read_migration("20260630130000_v48_ai_quality_audit_rpcs.sql")
    parts.append(
        _sanitize_pgserver_sql(
            _extract_sql_function(v48, "get_question_version_blind_context_v1")
        )
    )
    parts.append(
        _sanitize_pgserver_sql(
            _extract_sql_function(v48, "list_audit_candidate_resource_chunks_v1")
        )
    )
    sql = "\n".join(parts)
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _pgserver_available() -> bool:
    return psycopg2 is not None and pgserver is not None


class PgserverFixtureIngestionTestCase(unittest.TestCase):
    _pg = None
    _dsn = None

    @classmethod
    def setUpClass(cls):
        if not _pgserver_available():
            raise unittest.SkipTest("pgserver/psycopg2 unavailable")
        cls._tmpdir = tempfile.mkdtemp(prefix="pab_fixture_ingest_")
        cls._pg = pgserver.get_server(cls._tmpdir, cleanup_mode="delete")
        cls._dsn = cls._pg.get_uri()
        conn = psycopg2.connect(cls._dsn)
        try:
            bootstrap_fixture_ingestion_schema(conn)
        finally:
            conn.close()

    def setUp(self):
        self.conn = psycopg2.connect(self._dsn)
        self.conn.autocommit = True
        self._truncate_runtime_tables()
        self.client = PsycopgV48Client(self.conn)
        self.pab_payload, _, _ = load_fixture_for_ingestion(PAB_DEFAULT_OUTPUT_PATH)

    def tearDown(self):
        try:
            self._truncate_runtime_tables()
        finally:
            self.conn.close()

    def _truncate_runtime_tables(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE TABLE
                    public.question_option_versions,
                    public.question_versions,
                    public.questions,
                    public.resource_chunks,
                    public.resource_versions,
                    public.official_resources
                RESTART IDENTITY CASCADE
                """
            )

    def _ingest_pab(self, *, dry_run: bool = False):
        return ingest_official_evidence_fixture_package(
            self.conn,
            self.pab_payload,
            fixture_path=PAB_DEFAULT_OUTPUT_PATH,
            dry_run=dry_run,
        )

    def _count_table(self, table: str, *, certification: str | None = None) -> int:
        with self.conn.cursor() as cur:
            if certification is None:
                cur.execute(f"SELECT COUNT(*) FROM public.{table}")
            elif table == "official_resources":
                cur.execute(
                    "SELECT COUNT(*) FROM public.official_resources WHERE certification_exam_name = %s",
                    (certification,),
                )
            elif table == "resource_versions":
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.resource_versions rv
                    JOIN public.official_resources r ON r.id = rv.resource_id
                    WHERE r.certification_exam_name = %s
                    """,
                    (certification,),
                )
            else:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.resource_chunks rc
                    JOIN public.resource_versions rv ON rv.id = rc.resource_version_id
                    JOIN public.official_resources r ON r.id = rv.resource_id
                    WHERE r.certification_exam_name = %s
                    """,
                    (certification,),
                )
            return int(cur.fetchone()[0])

    def _seed_other_cert_resource(
        self,
        *,
        certification: str,
        resource_id: str | None = None,
    ) -> str:
        rid = resource_id or str(uuid.uuid4())
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.official_resources (
                    id, certification_exam_name, resource_type, title,
                    metadata, is_active, created_by
                ) VALUES (%s, %s, 'official_documentation', %s, %s::jsonb, true, 'seed')
                """,
                (
                    rid,
                    certification,
                    f"{certification} Seed Resource",
                    json.dumps({"domain": "Configuration"}),
                ),
            )
        return rid

    def _seed_question(self, *, exam_name: str, domain: str, question_text: str) -> str:
        qvid = str(uuid.uuid4())
        question_id = 880000 + abs(hash(domain)) % 1000
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.questions (
                    id, exam_name, category, difficulty, question_text,
                    question_type, select_count, explanation, is_active,
                    is_exam_eligible, language_code
                ) VALUES (%s, %s, %s, 'medium', %s, 'single', 1, '', true, true, 'en')
                ON CONFLICT (id) DO UPDATE SET exam_name = EXCLUDED.exam_name
                """,
                (question_id, exam_name, domain, question_text),
            )
            cur.execute(
                """
                INSERT INTO public.question_versions (
                    id, question_id, version_number, question_text, explanation,
                    category, difficulty, question_type, select_count, language_code,
                    content_hash, source_type, created_by
                ) VALUES (
                    %s, %s, 1, %s, '', %s, 'medium', 'single', 1, 'en',
                    %s, 'manual', 'pab-ingest-test'
                )
                """,
                (qvid, question_id, question_text, domain, "aa" * 32),
            )
            cur.execute(
                """
                INSERT INTO public.question_option_versions (
                    question_version_id, option_label, option_text, display_order, is_correct
                ) VALUES (%s, 'A', 'Option A', 1, true),
                       (%s, 'B', 'Option B', 2, false)
                """,
                (qvid, qvid),
            )
        return qvid


class TestPabFixtureValidation(unittest.TestCase):
    def test_pab_fixture_validation_passes(self):
        payload, fixture_version, package_config = load_fixture_for_ingestion(
            PAB_DEFAULT_OUTPUT_PATH
        )
        self.assertEqual(fixture_version, PAB_FIXTURE_VERSION)
        self.assertEqual(package_config["certification_exam_name"], PAB_EXAM_NAME)
        self.assertEqual(len(payload["evidence_items"]), 7)

    def test_unknown_fixture_identity_rejected(self):
        payload, _, _ = load_fixture_for_ingestion(PAB_DEFAULT_OUTPUT_PATH)
        tampered = deepcopy(payload)
        tampered["fixture_version"] = "official-evidence-unknown-v9"
        with self.assertRaises(OfficialEvidenceFixtureIngestionError):
            validate_fixture_for_ingestion(tampered)

    def test_v1_fixture_identity_not_ingestible_without_package_config(self):
        with self.assertRaises(OfficialEvidenceFixtureIngestionError):
            resolve_package_config("official-evidence-seed-v1")


@unittest.skipUnless(_pgserver_available(), "pgserver/psycopg2 unavailable")
class TestPabFixtureIngestion(PgserverFixtureIngestionTestCase):
    def test_seven_record_ingestion(self):
        result = self._ingest_pab()
        self.assertEqual(result.item_count, 7)
        self.assertEqual(self._count_table("official_resources", certification=PAB_EXAM_NAME), 7)
        self.assertEqual(self._count_table("resource_versions", certification=PAB_EXAM_NAME), 7)
        self.assertEqual(self._count_table("resource_chunks", certification=PAB_EXAM_NAME), 7)

    def test_all_five_domain_coverage(self):
        self._ingest_pab()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT metadata->>'domain'
                FROM public.official_resources
                WHERE certification_exam_name = %s
                ORDER BY 1
                """,
                (PAB_EXAM_NAME,),
            )
            domains = {row[0] for row in cur.fetchall()}
        self.assertEqual(domains, set(_PAB_DOMAINS))

    def test_exact_rerun_idempotency_no_duplicates(self):
        first = self._ingest_pab()
        second = self._ingest_pab()
        self.assertEqual(first.item_count, 7)
        self.assertEqual(second.item_count, 7)
        self.assertEqual(second.versions_idempotent, 7)
        self.assertEqual(second.versions_created, 0)
        self.assertEqual(second.chunks_created, 0)
        self.assertEqual(self._count_table("official_resources", certification=PAB_EXAM_NAME), 7)
        self.assertEqual(self._count_table("resource_versions", certification=PAB_EXAM_NAME), 7)
        self.assertEqual(self._count_table("resource_chunks", certification=PAB_EXAM_NAME), 7)

    def test_conflicting_content_hash_rejected(self):
        item = self.pab_payload["evidence_items"][0]
        resource_id = item["official_resource_id"]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.official_resources (
                    id, certification_exam_name, resource_type, title,
                    canonical_url, publisher, metadata, is_active, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, true, 'seed')
                """,
                (
                    resource_id,
                    item["certification"],
                    item["resource_type"],
                    item["resource_title"],
                    item["canonical_url"],
                    item["publisher"],
                    json.dumps(
                        {
                            "domain": item["domain"],
                            "domains": item["domain_tags"],
                            "certification_code": item["certification_code"],
                            "evidence_package_identity": PAB_FIXTURE_VERSION,
                            "evidence_config_id": "official_evidence_pab_v1",
                        }
                    ),
                ),
            )
            version_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO public.resource_versions (
                    id, resource_id, version_number, source_url, content_text,
                    content_hash, created_by, metadata
                ) VALUES (%s, %s, 1, %s, %s, %s, 'seed', '{}'::jsonb)
                """,
                (
                    version_id,
                    resource_id,
                    item["source_url"],
                    "different text",
                    "deadbeef" * 8,
                ),
            )
        with self.assertRaises(OfficialEvidenceFixtureIngestionConflictError):
            self._ingest_pab()

    def test_conflicting_certification_rejected(self):
        item = self.pab_payload["evidence_items"][0]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.official_resources (
                    id, certification_exam_name, resource_type, title,
                    canonical_url, publisher, metadata, is_active, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, true, 'seed')
                """,
                (
                    item["official_resource_id"],
                    ADM_EXAM_NAME,
                    item["resource_type"],
                    item["resource_title"],
                    item["canonical_url"],
                    item["publisher"],
                    json.dumps({"domain": item["domain"], "domains": item["domain_tags"]}),
                ),
            )
        with self.assertRaises(OfficialEvidenceFixtureIngestionConflictError):
            self._ingest_pab()

    def test_conflicting_domain_rejected(self):
        item = self.pab_payload["evidence_items"][0]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.official_resources (
                    id, certification_exam_name, resource_type, title,
                    canonical_url, publisher, metadata, is_active, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, true, 'seed')
                """,
                (
                    item["official_resource_id"],
                    item["certification"],
                    item["resource_type"],
                    item["resource_title"],
                    item["canonical_url"],
                    item["publisher"],
                    json.dumps(
                        {
                            "domain": "Wrong Domain",
                            "domains": ["Wrong Domain"],
                            "certification_code": item["certification_code"],
                            "evidence_package_identity": PAB_FIXTURE_VERSION,
                            "evidence_config_id": "official_evidence_pab_v1",
                        }
                    ),
                ),
            )
        with self.assertRaises(OfficialEvidenceFixtureIngestionConflictError):
            self._ingest_pab()

    def test_partial_package_not_committed_on_conflict(self):
        untouched = self.pab_payload["evidence_items"][1]
        conflict = self.pab_payload["evidence_items"][0]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.official_resources (
                    id, certification_exam_name, resource_type, title,
                    canonical_url, publisher, metadata, is_active, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, true, 'seed')
                """,
                (
                    conflict["official_resource_id"],
                    ADM_EXAM_NAME,
                    conflict["resource_type"],
                    conflict["resource_title"],
                    conflict["canonical_url"],
                    conflict["publisher"],
                    json.dumps({"domain": conflict["domain"], "domains": conflict["domain_tags"]}),
                ),
            )
        with self.assertRaises(OfficialEvidenceFixtureIngestionConflictError):
            self._ingest_pab()
        self.assertEqual(self._count_table("official_resources", certification=PAB_EXAM_NAME), 0)
        self.assertFalse(
            self._catalog_row_exists(untouched["official_resource_id"])
        )

    def _catalog_row_exists(self, resource_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM public.official_resources WHERE id = %s",
                (resource_id,),
            )
            return cur.fetchone() is not None

    def test_administrator_and_business_analyst_rows_unchanged(self):
        adm_id = self._seed_other_cert_resource(certification=ADM_EXAM_NAME)
        ba_id = self._seed_other_cert_resource(certification=BA_EXAM_NAME)
        before = {
            ADM_EXAM_NAME: self._fetch_resource_snapshot(adm_id),
            BA_EXAM_NAME: self._fetch_resource_snapshot(ba_id),
        }
        self._ingest_pab()
        after = {
            ADM_EXAM_NAME: self._fetch_resource_snapshot(adm_id),
            BA_EXAM_NAME: self._fetch_resource_snapshot(ba_id),
        }
        self.assertEqual(before, after)
        self.assertEqual(self._count_table("official_resources", certification=ADM_EXAM_NAME), 1)
        self.assertEqual(self._count_table("official_resources", certification=BA_EXAM_NAME), 1)

    def _fetch_resource_snapshot(self, resource_id: str) -> dict:
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT certification_exam_name, resource_type, title,
                       canonical_url, publisher, metadata
                FROM public.official_resources
                WHERE id = %s
                """,
                (resource_id,),
            )
            row = cur.fetchone()
            return dict(row)

    def test_runtime_retrieval_per_domain_uses_database_rows(self):
        self._ingest_pab()
        ingested_chunk_hashes = self._ingested_chunk_hashes()
        for domain in _PAB_DOMAINS:
            qvid = self._seed_question(
                exam_name=PAB_EXAM_NAME,
                domain=domain,
                question_text=f"Platform App Builder question about {domain}",
            )
            prepared = prepare_smoke_evidence_set(self.client, qvid)
            self.assertGreater(prepared.selected_count, 0)
            self.assertEqual(prepared.certification_exam_name, PAB_EXAM_NAME)
            for ranked in prepared.ranked_candidates:
                self.assertIn(ranked["content_hash"], ingested_chunk_hashes)

    def test_cross_certification_retrieval_isolation(self):
        self._ingest_pab()
        adm_id = self._seed_other_cert_resource(certification=ADM_EXAM_NAME)
        rv_id = str(uuid.uuid4())
        chunk_id = str(uuid.uuid4())
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.resource_versions (
                    id, resource_id, version_number, content_text, content_hash,
                    created_by, metadata
                ) VALUES (%s, %s, 1, %s, %s, 'seed', '{}'::jsonb)
                """,
                (
                    rv_id,
                    adm_id,
                    "Administrator-only evidence about profiles and roles.",
                    "bb" * 32,
                ),
            )
            cur.execute(
                """
                INSERT INTO public.resource_chunks (
                    id, resource_version_id, chunk_index, chunk_text, content_hash, metadata
                ) VALUES (%s, %s, 0, %s, %s, '{}'::jsonb)
                """,
                (
                    chunk_id,
                    rv_id,
                    "Administrator-only evidence about profiles and roles.",
                    "bb" * 32,
                ),
            )
        qvid = self._seed_question(
            exam_name=PAB_EXAM_NAME,
            domain="App Deployment",
            question_text="Which deployment approach should a Platform App Builder use?",
        )
        prepared = prepare_smoke_evidence_set(self.client, qvid)
        self.assertEqual(prepared.certification_exam_name, PAB_EXAM_NAME)
        for ranked in prepared.ranked_candidates:
            self.assertNotEqual(ranked["content_hash"], "bb" * 32)

    def _ingested_chunk_hashes(self) -> set[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT rc.content_hash
                FROM public.resource_chunks rc
                JOIN public.resource_versions rv ON rv.id = rc.resource_version_id
                JOIN public.official_resources r ON r.id = rv.resource_id
                WHERE r.certification_exam_name = %s
                """,
                (PAB_EXAM_NAME,),
            )
            return {row[0] for row in cur.fetchall()}

    def test_dry_run_package_validation_rolls_back(self):
        result = self._ingest_pab(dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.item_count, 7)
        self.assertEqual(self._count_table("official_resources", certification=PAB_EXAM_NAME), 0)

    def test_no_network_dependency_during_ingestion(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with patch("urllib.request.urlretrieve", side_effect=AssertionError("network")):
                self._ingest_pab()

    def test_stable_resource_identity_preserved(self):
        self._ingest_pab()
        expected_ids = {
            str(item["official_resource_id"]).strip().lower()
            for item in self.pab_payload["evidence_items"]
        }
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text
                FROM public.official_resources
                WHERE certification_exam_name = %s
                """,
                (PAB_EXAM_NAME,),
            )
            actual_ids = {row[0] for row in cur.fetchall()}
        self.assertEqual(actual_ids, expected_ids)


class TestFixtureIngestionSafety(unittest.TestCase):
    def test_production_dsn_rejected(self):
        with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
            reject_production_like_dsn(
                "postgresql://postgres:secret@db.abcdef.supabase.co:5432/postgres"
            )

    def test_live_ingest_requires_explicit_flag(self):
        with patch(
            "workers.official_evidence_fixture_ingestion.running_under_pytest",
            return_value=False,
        ):
            env = {
                key: value
                for key, value in os.environ.items()
                if key != ALLOW_FIXTURE_INGEST_FLAG
            }
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                    assert_fixture_ingest_allowed(
                        database_url=(
                            "postgresql://postgres:postgres@127.0.0.1:54329/"
                            "certbound_v48_test"
                        ),
                        dry_run=False,
                    )

    def test_v1_fixture_path_not_confused_with_pab_package(self):
        payload = json.loads(DEFAULT_OUTPUT_PATH.read_text(encoding="utf-8"))
        with self.assertRaises(OfficialEvidenceFixtureIngestionError):
            validate_fixture_for_ingestion(payload)


_HOSTED_DSN = "postgresql://postgres:REDACTED@db.abcdefghijk.supabase.co:5432/postgres"
_LOCAL_DSN = "postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test"

_APPROVED_KWARGS = dict(
    fixture_version=APPROVED_HOSTED_TARGET_FIXTURE_VERSION,
    certification_exam_name=APPROVED_HOSTED_TARGET_CERTIFICATION,
    record_count=APPROVED_HOSTED_TARGET_RECORD_COUNT,
)


class TestApprovedHostedSupabaseTargetOverride(unittest.TestCase):
    """PAB-EXP-04E: the narrow, fail-closed hosted-Supabase override."""

    def _clear_hosted_flags(self):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in (ALLOW_FIXTURE_INGEST_FLAG, ALLOW_APPROVED_SUPABASE_INGEST_FLAG)
        }
        return patch.dict(os.environ, env, clear=True)

    def test_is_hosted_supabase_dsn_classification(self):
        self.assertTrue(is_hosted_supabase_dsn(_HOSTED_DSN))
        self.assertFalse(is_hosted_supabase_dsn(_LOCAL_DSN))
        self.assertFalse(is_hosted_supabase_dsn(None))
        self.assertFalse(is_hosted_supabase_dsn(""))

    def test_hosted_target_rejected_with_zero_flags(self):
        with self._clear_hosted_flags():
            with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                assert_approved_hosted_supabase_target(
                    database_url=_HOSTED_DSN,
                    allow_hosted_cli_flag=False,
                    **_APPROVED_KWARGS,
                )

    def test_one_environment_flag_alone_is_insufficient(self):
        with self._clear_hosted_flags():
            with patch.dict(os.environ, {ALLOW_FIXTURE_INGEST_FLAG: "1"}):
                with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                    assert_approved_hosted_supabase_target(
                        database_url=_HOSTED_DSN,
                        allow_hosted_cli_flag=False,
                        **_APPROVED_KWARGS,
                    )

    def test_other_environment_flag_alone_is_insufficient(self):
        with self._clear_hosted_flags():
            with patch.dict(os.environ, {ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1"}):
                with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                    assert_approved_hosted_supabase_target(
                        database_url=_HOSTED_DSN,
                        allow_hosted_cli_flag=False,
                        **_APPROVED_KWARGS,
                    )

    def test_both_environment_flags_without_cli_flag_is_insufficient(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                    assert_approved_hosted_supabase_target(
                        database_url=_HOSTED_DSN,
                        allow_hosted_cli_flag=False,
                        **_APPROVED_KWARGS,
                    )

    def test_cli_flag_alone_without_env_flags_is_insufficient(self):
        with self._clear_hosted_flags():
            with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                assert_approved_hosted_supabase_target(
                    database_url=_HOSTED_DSN,
                    allow_hosted_cli_flag=True,
                    **_APPROVED_KWARGS,
                )

    def test_missing_database_url_is_insufficient_even_with_all_flags(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                    assert_approved_hosted_supabase_target(
                        database_url="",
                        allow_hosted_cli_flag=True,
                        **_APPROVED_KWARGS,
                    )

    def test_all_required_conditions_together_permit_hosted_target(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                # Must not raise: every required condition is satisfied.
                assert_approved_hosted_supabase_target(
                    database_url=_HOSTED_DSN,
                    allow_hosted_cli_flag=True,
                    **_APPROVED_KWARGS,
                )

    def test_unknown_fixture_identity_rejected_even_with_all_flags(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                    assert_approved_hosted_supabase_target(
                        database_url=_HOSTED_DSN,
                        allow_hosted_cli_flag=True,
                        fixture_version="official-evidence-unknown-v9",
                        certification_exam_name=APPROVED_HOSTED_TARGET_CERTIFICATION,
                        record_count=APPROVED_HOSTED_TARGET_RECORD_COUNT,
                    )

    def test_non_pab_certification_rejected_even_with_all_flags(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                    assert_approved_hosted_supabase_target(
                        database_url=_HOSTED_DSN,
                        allow_hosted_cli_flag=True,
                        fixture_version=APPROVED_HOSTED_TARGET_FIXTURE_VERSION,
                        certification_exam_name="Salesforce Certified Administrator",
                        record_count=APPROVED_HOSTED_TARGET_RECORD_COUNT,
                    )

    def test_record_count_other_than_seven_rejected_even_with_all_flags(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                for bad_count in (0, 1, 6, 8, 100):
                    with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                        assert_approved_hosted_supabase_target(
                            database_url=_HOSTED_DSN,
                            allow_hosted_cli_flag=True,
                            fixture_version=APPROVED_HOSTED_TARGET_FIXTURE_VERSION,
                            certification_exam_name=APPROVED_HOSTED_TARGET_CERTIFICATION,
                            record_count=bad_count,
                        )

    def test_enforce_dsn_target_safety_routes_local_dsn_unchanged(self):
        """Non-hosted DSNs never require any of the new hosted-target flags."""
        with self._clear_hosted_flags():
            enforce_dsn_target_safety(_LOCAL_DSN)  # must not raise

    def test_enforce_dsn_target_safety_still_rejects_hosted_by_default(self):
        """No generic safety protection was removed: with zero configuration,
        a hosted DSN is rejected exactly as it was before this override existed.
        """
        with self._clear_hosted_flags():
            with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                enforce_dsn_target_safety(_HOSTED_DSN)

    def test_enforce_dsn_target_safety_permits_hosted_when_fully_approved(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                enforce_dsn_target_safety(  # must not raise
                    _HOSTED_DSN,
                    allow_hosted_cli_flag=True,
                    **_APPROVED_KWARGS,
                )

    def test_credentials_never_appear_in_safety_error_messages(self):
        """Error messages must never echo back the DSN (which may embed a
        password), even when reporting a rejection for that exact DSN.
        """
        secret_marker = "REDACTED"
        with self._clear_hosted_flags():
            try:
                assert_approved_hosted_supabase_target(
                    database_url=_HOSTED_DSN,
                    allow_hosted_cli_flag=False,
                    **_APPROVED_KWARGS,
                )
                self.fail("expected OfficialEvidenceFixtureIngestionSafetyError")
            except OfficialEvidenceFixtureIngestionSafetyError as exc:
                self.assertNotIn(secret_marker, str(exc))
                self.assertNotIn(_HOSTED_DSN, str(exc))


class TestIngestFixtureFileHostedGateWiring(unittest.TestCase):
    """``ingest_fixture_file`` must invoke the hosted-target gate before
    ever calling ``psycopg2.connect`` -- proven by patching ``connect`` to
    fail the test if reached while the gate should have already raised.
    """

    def _clear_hosted_flags(self):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in (ALLOW_FIXTURE_INGEST_FLAG, ALLOW_APPROVED_SUPABASE_INGEST_FLAG)
        }
        return patch.dict(os.environ, env, clear=True)

    def test_hosted_dry_run_without_flags_never_connects(self):
        from workers import official_evidence_fixture_ingestion as mod

        with self._clear_hosted_flags():
            with patch.object(
                mod.psycopg2,
                "connect",
                side_effect=AssertionError("must not connect"),
            ):
                with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                    mod.ingest_fixture_file(
                        PAB_DEFAULT_OUTPUT_PATH,
                        database_url=_HOSTED_DSN,
                        dry_run=True,
                        allow_hosted_cli_flag=False,
                    )

    def test_hosted_live_run_without_cli_flag_never_connects(self):
        from workers import official_evidence_fixture_ingestion as mod

        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                with patch.object(
                    mod.psycopg2,
                    "connect",
                    side_effect=AssertionError("must not connect"),
                ):
                    with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                        mod.ingest_fixture_file(
                            PAB_DEFAULT_OUTPUT_PATH,
                            database_url=_HOSTED_DSN,
                            dry_run=False,
                            allow_hosted_cli_flag=False,
                        )


class TestCliHostedTargetFlag(unittest.TestCase):
    """CLI-level coverage for ``--allow-approved-supabase-target``."""

    def _run_cli(self, argv):
        import importlib

        script_path = _REPO_ROOT / "scripts" / "ingest_official_evidence_fixture.py"
        spec = importlib.util.spec_from_file_location(
            "ingest_official_evidence_fixture_cli", script_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.main(argv)

    def test_cli_defines_hosted_target_flag(self):
        source = (_REPO_ROOT / "scripts" / "ingest_official_evidence_fixture.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("--allow-approved-supabase-target", source)
        self.assertIn(ALLOW_APPROVED_SUPABASE_INGEST_FLAG, source)

    def test_cli_fixture_only_dry_run_never_prints_database_url(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = self._run_cli(
                ["--fixture", str(PAB_DEFAULT_OUTPUT_PATH), "--dry-run"]
            )
        self.assertEqual(exit_code, 0)
        output = buf.getvalue()
        self.assertNotIn("supabase.co", output)
        self.assertNotIn("postgresql://", output)


if __name__ == "__main__":
    unittest.main()
