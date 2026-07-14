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
from workers.certification_registry import PAB_EXAM_NAME, SCC_EXAM_NAME
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
    is_legitimate_hosted_supabase_hostname,
    load_fixture_for_ingestion,
    parse_dsn_hostname,
    reject_production_like_dsn,
    resolve_package_config,
    validate_fixture_for_ingestion,
)
from workers.official_evidence_seed import (
    ADM_EXAM_NAME,
    BA_DEFAULT_OUTPUT_PATH,
    BA_EVIDENCE_CONFIG_ID,
    BA_EXAM_NAME,
    BA_FIXTURE_VERSION,
    DEFAULT_OUTPUT_PATH,
    PAB_DEFAULT_OUTPUT_PATH,
    PAB_FIXTURE_VERSION,
    SCC_DEFAULT_OUTPUT_PATH,
    SCC_EVIDENCE_CONFIG_ID,
    SCC_FIXTURE_VERSION,
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
_BA_DOMAINS = (
    "Business Process Mapping",
    "Collaboration with Stakeholders",
    "Customer Discovery",
    "Requirements",
    "User Acceptance",
    "User Stories",
)
_SCC_DOMAINS = (
    "Practical Application of Sales Cloud Expertise",
    "Sales Lifecycle",
    "Consulting & Implementation Strategies",
    "Data Management",
    "Predictive and Generative AI",
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


def bootstrap_fixture_ingestion_schema(conn) -> None:
    """Apply the real migration sequence: the original (still-ambiguous) V44
    ingest_resource_version_v1 migration, followed by the PAB-EXP-04H
    CREATE OR REPLACE corrective migration. This proves the *actual*
    committed migration file fixes SQLSTATE 42702 -- it is no longer
    patched around in Python string substitution here.
    """
    original_ingest_sql = _substitute_uuid_sql(
        _read_migration("20260623234600_v44_ingest_resource_version_rpc.sql")
    )
    fixed_ingest_sql = _substitute_uuid_sql(
        _read_migration(
            "20260713230000_v62_fix_ingest_resource_version_idempotency_ambiguity.sql"
        )
    )
    parts = [
        _sanitize_pgserver_sql(
            _substitute_uuid_sql(
                _read_migration("20260623233800_v44_resource_library_foundation.sql")
            )
        ),
        _sanitize_pgserver_sql(original_ingest_sql),
        _sanitize_pgserver_sql(fixed_ingest_sql),
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
        self.ba_payload, _, _ = load_fixture_for_ingestion(BA_DEFAULT_OUTPUT_PATH)
        self.scc_payload, _, _ = load_fixture_for_ingestion(SCC_DEFAULT_OUTPUT_PATH)

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

    def _ingest_ba(self, *, dry_run: bool = False):
        return ingest_official_evidence_fixture_package(
            self.conn,
            self.ba_payload,
            fixture_path=BA_DEFAULT_OUTPUT_PATH,
            dry_run=dry_run,
        )

    def _ingest_scc(self, *, dry_run: bool = False):
        return ingest_official_evidence_fixture_package(
            self.conn,
            self.scc_payload,
            fixture_path=SCC_DEFAULT_OUTPUT_PATH,
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


@unittest.skipUnless(_pgserver_available(), "pgserver/psycopg2 unavailable")
class TestIngestResourceVersionRpcIdempotency(PgserverFixtureIngestionTestCase):
    """PAB-EXP-04H: direct RPC-level regression tests for the
    ingest_resource_version_v1 idempotency-branch fix, run against the real
    migration sequence (original V44 migration + the new corrective
    migration) applied by bootstrap_fixture_ingestion_schema(). These are
    independent of the PAB fixture package -- they exercise the RPC itself
    with a synthetic resource.
    """

    def _seed_standalone_resource(self, *, certification: str = PAB_EXAM_NAME) -> str:
        resource_id = str(uuid.uuid4())
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.official_resources (
                    id, certification_exam_name, resource_type, title,
                    metadata, is_active, created_by
                ) VALUES (%s, %s, 'official_documentation', %s, %s::jsonb, true, 'rpc-idempotency-test')
                """,
                (
                    resource_id,
                    certification,
                    "RPC Idempotency Test Resource",
                    json.dumps({"domain": "Salesforce Fundamentals"}),
                ),
            )
        return resource_id

    def _call_ingest_rpc(
        self,
        *,
        resource_id: str,
        content_text: str,
        content_hash: str,
        chunks: list,
        created_by: str = "rpc-idempotency-test",
    ) -> dict:
        params = {
            "p_resource_id": resource_id,
            "p_source_url": None,
            "p_source_external_version": None,
            "p_content_text": content_text,
            "p_content_hash": content_hash,
            "p_effective_at": None,
            "p_created_by": created_by,
            "p_metadata": {},
            "p_chunks": chunks,
        }
        result = self.client.rpc("ingest_resource_version_v1", params).execute()
        self.assertIsNone(result.error)
        self.assertEqual(len(result.data), 1)
        return result.data[0]

    def _one_chunk(self, *, text: str, content_hash: str) -> list:
        return [
            {
                "chunk_index": 0,
                "chunk_text": text,
                "content_hash": content_hash,
            }
        ]

    def test_first_ingestion_creates_one_version_and_expected_chunks(self):
        resource_id = self._seed_standalone_resource()
        row = self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="First ingestion content.",
            content_hash="aa" * 32,
            chunks=self._one_chunk(text="First ingestion content.", content_hash="aa" * 32),
        )
        self.assertEqual(row["version_number"], 1)
        self.assertEqual(row["chunk_count"], 1)
        self.assertEqual(
            self._count_table("resource_versions", certification=PAB_EXAM_NAME), 1
        )
        self.assertEqual(
            self._count_table("resource_chunks", certification=PAB_EXAM_NAME), 1
        )

    def test_second_identical_call_succeeds(self):
        resource_id = self._seed_standalone_resource()
        content_hash = "bb" * 32
        chunks = self._one_chunk(text="Idempotent content.", content_hash=content_hash)
        self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Idempotent content.",
            content_hash=content_hash,
            chunks=chunks,
        )
        # Must not raise psycopg2.errors.AmbiguousColumn (SQLSTATE 42702).
        second = self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Idempotent content.",
            content_hash=content_hash,
            chunks=chunks,
        )
        self.assertIsNotNone(second)

    def test_second_call_returns_original_version_id(self):
        resource_id = self._seed_standalone_resource()
        content_hash = "cc" * 32
        chunks = self._one_chunk(text="Same content twice.", content_hash=content_hash)
        first = self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Same content twice.",
            content_hash=content_hash,
            chunks=chunks,
        )
        second = self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Same content twice.",
            content_hash=content_hash,
            chunks=chunks,
        )
        self.assertEqual(str(second["resource_version_id"]), str(first["resource_version_id"]))
        self.assertEqual(second["version_number"], first["version_number"])

    def test_second_call_returns_existing_chunk_count(self):
        resource_id = self._seed_standalone_resource()
        content_hash = "dd" * 32
        chunks = [
            {"chunk_index": 0, "chunk_text": "Chunk zero.", "content_hash": "d0" * 32},
            {"chunk_index": 1, "chunk_text": "Chunk one.", "content_hash": "d1" * 32},
        ]
        first = self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Two-chunk content.",
            content_hash=content_hash,
            chunks=chunks,
        )
        second = self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Two-chunk content.",
            content_hash=content_hash,
            chunks=chunks,
        )
        self.assertEqual(first["chunk_count"], 2)
        self.assertEqual(second["chunk_count"], 2)

    def test_no_second_version_row_created_on_idempotent_rerun(self):
        resource_id = self._seed_standalone_resource()
        content_hash = "ee" * 32
        chunks = self._one_chunk(text="Version row check.", content_hash=content_hash)
        self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Version row check.",
            content_hash=content_hash,
            chunks=chunks,
        )
        self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Version row check.",
            content_hash=content_hash,
            chunks=chunks,
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.resource_versions WHERE resource_id = %s",
                (resource_id,),
            )
            self.assertEqual(cur.fetchone()[0], 1)

    def test_no_additional_chunk_rows_created_on_idempotent_rerun(self):
        resource_id = self._seed_standalone_resource()
        content_hash = "ff" * 32
        chunks = [
            {"chunk_index": 0, "chunk_text": "Chunk A.", "content_hash": "f0" * 32},
            {"chunk_index": 1, "chunk_text": "Chunk B.", "content_hash": "f1" * 32},
            {"chunk_index": 2, "chunk_text": "Chunk C.", "content_hash": "f2" * 32},
        ]
        row1 = self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Three-chunk content.",
            content_hash=content_hash,
            chunks=chunks,
        )
        self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Three-chunk content.",
            content_hash=content_hash,
            chunks=chunks,
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.resource_chunks WHERE resource_version_id = %s",
                (row1["resource_version_id"],),
            )
            self.assertEqual(cur.fetchone()[0], 3)

    def test_new_content_hash_creates_next_version_normally(self):
        resource_id = self._seed_standalone_resource()
        first = self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Version one content.",
            content_hash="11" * 32,
            chunks=self._one_chunk(text="Version one content.", content_hash="11" * 32),
        )
        second = self._call_ingest_rpc(
            resource_id=resource_id,
            content_text="Version two content.",
            content_hash="22" * 32,
            chunks=self._one_chunk(text="Version two content.", content_hash="22" * 32),
        )
        self.assertEqual(first["version_number"], 1)
        self.assertEqual(second["version_number"], 2)
        self.assertNotEqual(
            str(first["resource_version_id"]), str(second["resource_version_id"])
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.resource_versions WHERE resource_id = %s",
                (resource_id,),
            )
            self.assertEqual(cur.fetchone()[0], 2)

    def test_existing_invalid_payload_validation_unchanged(self):
        resource_id = self._seed_standalone_resource()
        params = {
            "p_resource_id": resource_id,
            "p_source_url": None,
            "p_source_external_version": None,
            "p_content_text": "",  # invalid: empty content_text
            "p_content_hash": "33" * 32,
            "p_effective_at": None,
            "p_created_by": "rpc-idempotency-test",
            "p_metadata": {},
            "p_chunks": [],
        }
        with self.assertRaises(Exception) as ctx:
            self.client.conn.cursor().execute(
                "SELECT * FROM public.ingest_resource_version_v1("
                "p_resource_id => %s, p_source_url => %s, "
                "p_source_external_version => %s, p_content_text => %s, "
                "p_content_hash => %s, p_effective_at => %s, "
                "p_created_by => %s, p_metadata => %s::jsonb, p_chunks => %s::jsonb)",
                (
                    params["p_resource_id"],
                    params["p_source_url"],
                    params["p_source_external_version"],
                    params["p_content_text"],
                    params["p_content_hash"],
                    params["p_effective_at"],
                    params["p_created_by"],
                    json.dumps(params["p_metadata"]),
                    json.dumps(params["p_chunks"]),
                ),
            )
        self.assertIn("p_content_text", str(ctx.exception))
        self.conn.rollback()


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


class TestBaFixtureValidation(unittest.TestCase):
    def test_ba_fixture_validation_passes(self):
        payload, fixture_version, package_config = load_fixture_for_ingestion(
            BA_DEFAULT_OUTPUT_PATH
        )
        self.assertEqual(fixture_version, BA_FIXTURE_VERSION)
        self.assertEqual(package_config["evidence_config_id"], BA_EVIDENCE_CONFIG_ID)
        self.assertEqual(package_config["certification_exam_name"], BA_EXAM_NAME)
        self.assertEqual(package_config["expected_record_count"], 6)
        self.assertEqual(package_config["expected_domain_count"], 6)
        self.assertEqual(len(payload["evidence_items"]), 6)

    def test_ba_unknown_fixture_identity_rejected(self):
        payload, _, _ = load_fixture_for_ingestion(BA_DEFAULT_OUTPUT_PATH)
        tampered = deepcopy(payload)
        tampered["fixture_version"] = "official-evidence-unknown-v9"
        with self.assertRaises(OfficialEvidenceFixtureIngestionError):
            validate_fixture_for_ingestion(tampered)

    def test_ba_wrong_certification_rejected(self):
        payload, _, _ = load_fixture_for_ingestion(BA_DEFAULT_OUTPUT_PATH)
        tampered = deepcopy(payload)
        tampered["certifications_covered"] = [ADM_EXAM_NAME]
        with self.assertRaises(OfficialEvidenceFixtureIngestionError):
            validate_fixture_for_ingestion(tampered)

    def test_ba_wrong_record_count_rejected(self):
        payload, _, _ = load_fixture_for_ingestion(BA_DEFAULT_OUTPUT_PATH)
        tampered = deepcopy(payload)
        tampered["evidence_items"] = tampered["evidence_items"][:-1]
        with self.assertRaises(OfficialEvidenceFixtureIngestionError):
            validate_fixture_for_ingestion(tampered)


class TestSccFixtureValidation(unittest.TestCase):
    def test_scc_fixture_validation_passes(self):
        payload, fixture_version, package_config = load_fixture_for_ingestion(
            SCC_DEFAULT_OUTPUT_PATH
        )
        self.assertEqual(fixture_version, SCC_FIXTURE_VERSION)
        self.assertEqual(package_config["evidence_config_id"], SCC_EVIDENCE_CONFIG_ID)
        self.assertEqual(package_config["certification_exam_name"], SCC_EXAM_NAME)
        self.assertEqual(package_config["expected_record_count"], 5)
        self.assertEqual(package_config["expected_domain_count"], 5)
        self.assertEqual(len(payload["evidence_items"]), 5)

    def test_scc_unknown_fixture_identity_rejected(self):
        payload, _, _ = load_fixture_for_ingestion(SCC_DEFAULT_OUTPUT_PATH)
        tampered = deepcopy(payload)
        tampered["fixture_version"] = "official-evidence-unknown-v9"
        with self.assertRaises(OfficialEvidenceFixtureIngestionError):
            validate_fixture_for_ingestion(tampered)

    def test_scc_wrong_certification_rejected(self):
        payload, _, _ = load_fixture_for_ingestion(SCC_DEFAULT_OUTPUT_PATH)
        tampered = deepcopy(payload)
        tampered["certifications_covered"] = [ADM_EXAM_NAME]
        with self.assertRaises(OfficialEvidenceFixtureIngestionError):
            validate_fixture_for_ingestion(tampered)

    def test_scc_wrong_record_count_rejected(self):
        payload, _, _ = load_fixture_for_ingestion(SCC_DEFAULT_OUTPUT_PATH)
        tampered = deepcopy(payload)
        tampered["evidence_items"] = tampered["evidence_items"][:-1]
        with self.assertRaises(OfficialEvidenceFixtureIngestionError):
            validate_fixture_for_ingestion(tampered)


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
        self.assertEqual(first.resources_created, 7)
        self.assertEqual(first.versions_created, 7)
        self.assertEqual(second.item_count, 7)
        # PAB-EXP-04H acceptance criterion: exact rerun disposition.
        self.assertEqual(second.resources_created, 0)
        self.assertEqual(second.resources_existing, 7)
        self.assertEqual(second.versions_created, 0)
        self.assertEqual(second.versions_idempotent, 7)
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


@unittest.skipUnless(_pgserver_available(), "pgserver/psycopg2 unavailable")
class TestBaFixtureIngestion(PgserverFixtureIngestionTestCase):
    def test_six_record_ingestion(self):
        result = self._ingest_ba()
        self.assertEqual(result.item_count, 6)
        self.assertEqual(self._count_table("official_resources", certification=BA_EXAM_NAME), 6)
        self.assertEqual(self._count_table("resource_versions", certification=BA_EXAM_NAME), 6)
        self.assertEqual(self._count_table("resource_chunks", certification=BA_EXAM_NAME), 6)

    def test_all_six_domain_coverage(self):
        self._ingest_ba()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT metadata->>'domain'
                FROM public.official_resources
                WHERE certification_exam_name = %s
                ORDER BY 1
                """,
                (BA_EXAM_NAME,),
            )
            domains = {row[0] for row in cur.fetchall()}
        self.assertEqual(domains, set(_BA_DOMAINS))

    def test_exact_rerun_idempotency_no_duplicates(self):
        first = self._ingest_ba()
        second = self._ingest_ba()
        self.assertEqual(first.item_count, 6)
        self.assertEqual(first.resources_created, 6)
        self.assertEqual(first.versions_created, 6)
        self.assertEqual(second.item_count, 6)
        self.assertEqual(second.resources_created, 0)
        self.assertEqual(second.resources_existing, 6)
        self.assertEqual(second.versions_created, 0)
        self.assertEqual(second.versions_idempotent, 6)
        self.assertEqual(second.chunks_created, 0)
        self.assertEqual(self._count_table("official_resources", certification=BA_EXAM_NAME), 6)
        self.assertEqual(self._count_table("resource_versions", certification=BA_EXAM_NAME), 6)
        self.assertEqual(self._count_table("resource_chunks", certification=BA_EXAM_NAME), 6)

    def test_conflicting_content_hash_rejected(self):
        item = self.ba_payload["evidence_items"][0]
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
                            "evidence_package_identity": BA_FIXTURE_VERSION,
                            "evidence_config_id": BA_EVIDENCE_CONFIG_ID,
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
            self._ingest_ba()

    def test_conflicting_certification_rejected(self):
        item = self.ba_payload["evidence_items"][0]
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
                    PAB_EXAM_NAME,
                    item["resource_type"],
                    item["resource_title"],
                    item["canonical_url"],
                    item["publisher"],
                    json.dumps({"domain": item["domain"], "domains": item["domain_tags"]}),
                ),
            )
        with self.assertRaises(OfficialEvidenceFixtureIngestionConflictError):
            self._ingest_ba()

    def test_pab_ingestion_unchanged_after_ba_registration(self):
        result = self._ingest_pab()
        self.assertEqual(result.item_count, 7)
        self.assertEqual(self._count_table("official_resources", certification=PAB_EXAM_NAME), 7)


@unittest.skipUnless(_pgserver_available(), "pgserver/psycopg2 unavailable")
class TestSccFixtureIngestion(PgserverFixtureIngestionTestCase):
    def test_five_record_ingestion(self):
        result = self._ingest_scc()
        self.assertEqual(result.item_count, 5)
        self.assertEqual(self._count_table("official_resources", certification=SCC_EXAM_NAME), 5)
        self.assertEqual(self._count_table("resource_versions", certification=SCC_EXAM_NAME), 5)
        self.assertEqual(self._count_table("resource_chunks", certification=SCC_EXAM_NAME), 5)

    def test_all_five_domain_coverage(self):
        self._ingest_scc()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT metadata->>'domain'
                FROM public.official_resources
                WHERE certification_exam_name = %s
                ORDER BY 1
                """,
                (SCC_EXAM_NAME,),
            )
            domains = {row[0] for row in cur.fetchall()}
        self.assertEqual(domains, set(_SCC_DOMAINS))

    def test_exact_rerun_idempotency_no_duplicates(self):
        first = self._ingest_scc()
        second = self._ingest_scc()
        self.assertEqual(first.item_count, 5)
        self.assertEqual(first.resources_created, 5)
        self.assertEqual(first.versions_created, 5)
        self.assertEqual(second.item_count, 5)
        self.assertEqual(second.resources_created, 0)
        self.assertEqual(second.resources_existing, 5)
        self.assertEqual(second.versions_created, 0)
        self.assertEqual(second.versions_idempotent, 5)
        self.assertEqual(second.chunks_created, 0)
        self.assertEqual(self._count_table("official_resources", certification=SCC_EXAM_NAME), 5)
        self.assertEqual(self._count_table("resource_versions", certification=SCC_EXAM_NAME), 5)
        self.assertEqual(self._count_table("resource_chunks", certification=SCC_EXAM_NAME), 5)

    def test_conflicting_content_hash_rejected(self):
        item = self.scc_payload["evidence_items"][0]
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
                            "evidence_package_identity": SCC_FIXTURE_VERSION,
                            "evidence_config_id": SCC_EVIDENCE_CONFIG_ID,
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
            self._ingest_scc()

    def test_conflicting_certification_rejected(self):
        item = self.scc_payload["evidence_items"][0]
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
                    PAB_EXAM_NAME,
                    item["resource_type"],
                    item["resource_title"],
                    item["canonical_url"],
                    item["publisher"],
                    json.dumps({"domain": item["domain"], "domains": item["domain_tags"]}),
                ),
            )
        with self.assertRaises(OfficialEvidenceFixtureIngestionConflictError):
            self._ingest_scc()

    def test_ba_ingestion_unchanged_after_scc_registration(self):
        result = self._ingest_ba()
        self.assertEqual(result.item_count, 6)
        self.assertEqual(self._count_table("official_resources", certification=BA_EXAM_NAME), 6)

    def test_pab_ingestion_unchanged_after_scc_registration(self):
        result = self._ingest_pab()
        self.assertEqual(result.item_count, 7)
        self.assertEqual(self._count_table("official_resources", certification=PAB_EXAM_NAME), 7)


class TestFixtureIngestionSafety(unittest.TestCase):
    def test_production_dsn_rejected_for_spoofed_hostname(self):
        with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
            reject_production_like_dsn(_SPOOF_EVIL_SUPABASE_COM)

    def test_legitimate_supabase_co_not_rejected_by_generic_path(self):
        # Legitimate hosted hosts route through the seven-condition gate, not
        # the generic production rejection path.
        reject_production_like_dsn(_HOSTED_DSN)

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
_POOLER_HOSTED_DSN = (
    "postgresql://postgres:REDACTED@aws-1-us-east-1.pooler.supabase.com:5432/postgres"
)
_SPOOF_SUPABASE_COM_EXAMPLE_ORG = (
    "postgresql://postgres:REDACTED@supabase.com.example.org:5432/postgres"
)
_SPOOF_EVIL_SUPABASE_COM = "postgresql://postgres:REDACTED@evil-supabase.com:5432/postgres"
_LOCAL_DSN = "postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test"

_APPROVED_KWARGS = dict(
    fixture_version=APPROVED_HOSTED_TARGET_FIXTURE_VERSION,
    certification_exam_name=APPROVED_HOSTED_TARGET_CERTIFICATION,
    record_count=APPROVED_HOSTED_TARGET_RECORD_COUNT,
)
_BA_APPROVED_KWARGS = dict(
    fixture_version=BA_FIXTURE_VERSION,
    certification_exam_name=BA_EXAM_NAME,
    record_count=6,
)
_SCC_APPROVED_KWARGS = dict(
    fixture_version=SCC_FIXTURE_VERSION,
    certification_exam_name=SCC_EXAM_NAME,
    record_count=5,
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
        self.assertTrue(is_hosted_supabase_dsn(_POOLER_HOSTED_DSN))
        self.assertFalse(is_hosted_supabase_dsn(_LOCAL_DSN))
        self.assertFalse(is_hosted_supabase_dsn(_SPOOF_SUPABASE_COM_EXAMPLE_ORG))
        self.assertFalse(is_hosted_supabase_dsn(_SPOOF_EVIL_SUPABASE_COM))
        self.assertFalse(is_hosted_supabase_dsn(None))
        self.assertFalse(is_hosted_supabase_dsn(""))

    def test_pooler_supabase_com_hostname_parsed_correctly(self):
        self.assertEqual(
            parse_dsn_hostname(_POOLER_HOSTED_DSN),
            "aws-1-us-east-1.pooler.supabase.com",
        )
        self.assertTrue(
            is_legitimate_hosted_supabase_hostname(
                "aws-1-us-east-1.pooler.supabase.com"
            )
        )

    def test_pooler_host_rejected_by_default(self):
        with self._clear_hosted_flags():
            with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                enforce_dsn_target_safety(_POOLER_HOSTED_DSN)

    def test_pooler_host_permitted_only_with_all_seven_conditions(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                enforce_dsn_target_safety(
                    _POOLER_HOSTED_DSN,
                    allow_hosted_cli_flag=True,
                    **_APPROVED_KWARGS,
                )

    def test_spoof_supabase_com_example_org_not_hosted(self):
        self.assertFalse(is_hosted_supabase_dsn(_SPOOF_SUPABASE_COM_EXAMPLE_ORG))
        with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
            reject_production_like_dsn(_SPOOF_SUPABASE_COM_EXAMPLE_ORG)

    def test_spoof_evil_supabase_com_not_hosted(self):
        self.assertFalse(is_hosted_supabase_dsn(_SPOOF_EVIL_SUPABASE_COM))
        with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
            reject_production_like_dsn(_SPOOF_EVIL_SUPABASE_COM)

    def test_pooler_hosted_dry_run_without_flags_never_connects(self):
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
                        database_url=_POOLER_HOSTED_DSN,
                        dry_run=True,
                        allow_hosted_cli_flag=False,
                    )

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

    def test_ba_all_required_conditions_together_permit_hosted_target(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                assert_approved_hosted_supabase_target(
                    database_url=_HOSTED_DSN,
                    allow_hosted_cli_flag=True,
                    **_BA_APPROVED_KWARGS,
                )

    def test_ba_non_ba_certification_rejected_even_with_all_flags(self):
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
                        fixture_version=BA_FIXTURE_VERSION,
                        certification_exam_name=PAB_EXAM_NAME,
                        record_count=6,
                    )

    def test_ba_record_count_other_than_six_rejected_even_with_all_flags(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                for bad_count in (0, 1, 5, 7, 100):
                    with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                        assert_approved_hosted_supabase_target(
                            database_url=_HOSTED_DSN,
                            allow_hosted_cli_flag=True,
                            fixture_version=BA_FIXTURE_VERSION,
                            certification_exam_name=BA_EXAM_NAME,
                            record_count=bad_count,
                        )

    def test_pab_record_count_six_rejected_even_with_all_flags(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                bad_kwargs = dict(_APPROVED_KWARGS)
                bad_kwargs["record_count"] = 6
                with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                    assert_approved_hosted_supabase_target(
                        database_url=_HOSTED_DSN,
                        allow_hosted_cli_flag=True,
                        **bad_kwargs,
                    )

    def test_scc_all_required_conditions_together_permit_hosted_target(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                assert_approved_hosted_supabase_target(
                    database_url=_HOSTED_DSN,
                    allow_hosted_cli_flag=True,
                    **_SCC_APPROVED_KWARGS,
                )

    def test_scc_non_scc_certification_rejected_even_with_all_flags(self):
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
                        fixture_version=SCC_FIXTURE_VERSION,
                        certification_exam_name=PAB_EXAM_NAME,
                        record_count=5,
                    )

    def test_scc_record_count_other_than_five_rejected_even_with_all_flags(self):
        with self._clear_hosted_flags():
            with patch.dict(
                os.environ,
                {
                    ALLOW_FIXTURE_INGEST_FLAG: "1",
                    ALLOW_APPROVED_SUPABASE_INGEST_FLAG: "1",
                },
            ):
                for bad_count in (0, 1, 4, 6, 7, 100):
                    with self.assertRaises(OfficialEvidenceFixtureIngestionSafetyError):
                        assert_approved_hosted_supabase_target(
                            database_url=_HOSTED_DSN,
                            allow_hosted_cli_flag=True,
                            fixture_version=SCC_FIXTURE_VERSION,
                            certification_exam_name=SCC_EXAM_NAME,
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
