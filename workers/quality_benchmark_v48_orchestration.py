"""
V58-QUALITY-04C — V48 disposable-database benchmark execution adapter.

Runs the real, completely unmodified V48 audit pipeline
(``workers.ai_quality_audit_worker.process_ai_quality_audit_job`` and every
RPC it calls) against one benchmark case at a time, using an explicitly
identified *disposable* PostgreSQL database instead of a live Supabase
project. Nothing in this module reimplements pass sequencing, lease
handling, retry logic, dispute handling, completion-shape logic, or evidence
confirmation logic — all of that continues to execute exclusively inside the
real ``claim_ai_quality_audit_pass_v1`` / ``record_audit_pass_result_v1`` /
``persist_audit_run_dispute_trigger_v1`` / ``complete_ai_quality_audit_run_v1``
RPCs (``supabase/migrations/20260630130000_v48_ai_quality_audit_rpcs.sql``),
reached through ``workers.v48_psycopg_client.PsycopgV48Client`` — the same
Supabase-shaped psycopg2 shim already proven in
``tests/test_ai_quality_audit_integration.py``.

What this module *does* own (and only this):
  1. Disposable-database DSN safety validation (``validate_disposable_dsn``)
     — rejects anything that is not an explicitly approved, non-production,
     loopback-only, name-pattern-matched test database, cross-checked
     against the database's own live-reported identity.
  2. Schema/RPC compatibility verification (``verify_v48_schema_compatibility``)
     — fails BLOCKED, not silently, if required V48 tables/RPCs are missing.
  3. Seeding the minimum real rows for one benchmark case's question and its
     own frozen evidence snapshot (``seed_benchmark_case``) — using the
     case's own resource/version/chunk ids and content hashes verbatim, and
     the existing pure hashing helpers from
     ``workers.ai_quality_audit_evidence`` (never inventing evidence).
  4. Transaction lifecycle (``v48_disposable_transaction``) — BEGIN once per
     case, always ROLLBACK in a ``finally``, regardless of success, failure,
     or exception. No code path in this module ever commits.
  5. Converting the real worker's terminal summary + persisted findings into
     the shared ``workers.quality_benchmark_execution.CasePrediction`` shape.

Safety posture
---------------
* Every case runs in its own independent transaction that is always rolled
  back — no seeded or generated row is ever left behind, in the disposable
  database or anywhere else.
* The disposable DSN is never logged, printed, or embedded in any error
  message, prediction, or artifact — only its parsed, non-secret host/dbname
  components are ever referenced.
* Connecting requires an explicit ``allow_disposable_v48_db=True`` flag *and*
  a DSN that independently passes ``validate_disposable_dsn`` *and* passes a
  live ``current_database()`` cross-check — three independent factors, none
  of which can be spoofed by a username, password, query parameter, or
  hostname substring.
* Production Supabase hosts and any non-loopback host are always rejected.
* This module never constructs an ``AiQualityAuditProviders`` from
  environment variables or performs a live AI call itself — callers
  (currently only Docker-gated tests) must inject providers explicitly.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from workers.ai_quality_audit_evidence import (
    build_create_run_evidence_payload,
    compute_evidence_set_hash,
)
from workers.ai_quality_audit_worker import (
    AiQualityAuditProviders,
    process_ai_quality_audit_job,
)
from workers.quality_benchmark_execution import (
    CasePrediction,
    EngineAdapterUnavailableError,
)
# Reuses the canonical finding-summarization/prediction-shape helper rather
# than reimplementing it (mirrors the existing precedent of importing
# ``workers.quality_benchmark._build_case_result`` the same way).
from workers.quality_benchmark_execution import _prediction_from_findings
from workers.v48_psycopg_client import PsycopgV48Client, is_psycopg2_available

try:
    import psycopg2
except ImportError:  # pragma: no cover - exercised only when psycopg2 absent
    psycopg2 = None  # type: ignore

DEFAULT_WORKER_ID = "v58-quality-04c-benchmark"
DEFAULT_PROMPT_VERSION = "v58-quality-04c-benchmark-prompt"
DEFAULT_RULESET_VERSION = "v58-quality-04c-benchmark-rules"
DEFAULT_PRIMARY_MODEL_NAME = "benchmark-primary"
DEFAULT_DISPUTE_MODEL_NAME = "benchmark-dispute"
DEFAULT_PILOT_BATCH_ID = "v58-quality-04c-benchmark"

# ---------------------------------------------------------------------------
# Disposable-database safety
# ---------------------------------------------------------------------------

# Loopback only. Production Supabase (or any shared/staging environment) is
# never reachable on these hosts, so this is a structural guard, not a
# string-matching heuristic.
ALLOWED_DISPOSABLE_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# The parsed *database name* (never the raw DSN string) must match this
# pattern exactly. Anchored full-string match — a DSN cannot smuggle an
# unapproved dbname past this by hiding it in a username, password, or query
# parameter, because we only ever look at ``urlsplit(dsn).path``.
ALLOWED_DISPOSABLE_DB_NAME_RE = re.compile(r"^certbound_v48_test(_[a-z0-9_]+)?$")

# Explicit deny-list of hostname substrings that are always production-like,
# even if somehow present alongside a loopback-looking host string. Checked
# in addition to (not instead of) the allow-list above.
_PRODUCTION_HOST_MARKERS = (
    "supabase.co",
    "supabase.in",
    "supabase.com",
    "amazonaws.com",
    "rds.amazonaws.com",
    "azure.com",
    "gcp.com",
)

REQUIRED_V48_TABLES = (
    "certifications",
    "languages",
    "questions",
    "question_versions",
    "question_option_versions",
    "official_resources",
    "resource_versions",
    "resource_chunks",
    "audit_runs",
    "audit_run_dedup_keys",
    "audit_run_pass_results",
    "audit_run_dispute_triggers",
    "audit_run_evidence_set",
    "audit_findings",
    "audit_finding_evidence",
)

REQUIRED_V48_RPCS = (
    "get_question_version_blind_context_v1",
    "get_question_version_comparison_context_v1",
    "list_audit_candidate_resource_chunks_v1",
    "create_or_get_ai_quality_audit_run_v1",
    "claim_ai_quality_audit_pass_v1",
    "record_audit_pass_result_v1",
    "persist_audit_run_dispute_trigger_v1",
    "complete_ai_quality_audit_run_v1",
)

# Best-effort only (see verify_v48_schema_compatibility): the leading
# timestamp of the two migrations that introduce the V48 audit pipeline.
# This repository's disposable V48 test container is not guaranteed to be
# provisioned through the Supabase CLI's migration-history table, so the
# authoritative gate is the live table/RPC existence check above; this is
# an additional signal only when that history table happens to be present.
REQUIRED_V48_MIGRATION_VERSIONS = ("20260630120000", "20260630130000")


class V48DisposableDatabaseError(RuntimeError):
    """Base error for the V48 disposable-database execution seam."""


class V48DisposableDsnRejectedError(V48DisposableDatabaseError):
    """The supplied DSN failed disposable-database safety checks."""


class V48MigrationCompatibilityError(V48DisposableDatabaseError):
    """The database is missing required V48 tables, RPCs, or migrations."""


class V48DatabaseUnavailableError(V48DisposableDatabaseError):
    """The disposable database could not be reached."""


class V48EvidenceUnavailableError(V48DisposableDatabaseError):
    """The benchmark case has no valid, groundable evidence to seed."""


@dataclass(frozen=True)
class DisposableDsnInfo:
    """Parsed, non-secret identity of a validated disposable DSN.

    Deliberately excludes the raw DSN (which may carry credentials) and any
    query parameters; only the structural components needed for safety
    checks and for redacted logging/error messages are kept.
    """

    host: str
    dbname: str


def validate_disposable_dsn(dsn: Optional[str]) -> DisposableDsnInfo:
    """Raise ``V48DisposableDsnRejectedError`` unless *dsn* is structurally
    an approved disposable V48 test database.

    Checks the *parsed* scheme, hostname, and database-name components only
    — never a substring search over the raw DSN — so a spoofed username,
    password, or query parameter containing the word "test" cannot pass
    this check.
    """
    if not dsn or not dsn.strip():
        raise V48DisposableDsnRejectedError(
            "V48 disposable database DSN must not be empty"
        )

    parsed = urlsplit(dsn.strip())

    if parsed.scheme not in ("postgres", "postgresql"):
        raise V48DisposableDsnRejectedError(
            f"unsupported DSN scheme {parsed.scheme!r}; expected postgres/postgresql"
        )

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise V48DisposableDsnRejectedError("DSN is missing a hostname")

    for marker in _PRODUCTION_HOST_MARKERS:
        if marker in hostname:
            raise V48DisposableDsnRejectedError(
                f"DSN host {hostname!r} matches a production-host marker "
                f"({marker!r}); refusing"
            )

    if hostname not in ALLOWED_DISPOSABLE_HOSTS:
        raise V48DisposableDsnRejectedError(
            f"DSN host {hostname!r} is not an approved disposable-database "
            f"host (expected one of {sorted(ALLOWED_DISPOSABLE_HOSTS)}); "
            "refusing to guard against accidental production or "
            "shared-environment connections"
        )

    dbname = (parsed.path or "").lstrip("/").strip().lower()
    if not dbname:
        raise V48DisposableDsnRejectedError("DSN is missing a database name")

    if not ALLOWED_DISPOSABLE_DB_NAME_RE.match(dbname):
        raise V48DisposableDsnRejectedError(
            f"database name {dbname!r} does not match the approved "
            f"disposable-database naming pattern "
            f"({ALLOWED_DISPOSABLE_DB_NAME_RE.pattern!r}); refusing to guard "
            "against ambiguous or production-like targets"
        )

    return DisposableDsnInfo(host=hostname, dbname=dbname)


def _verify_live_database_identity(conn, expected_dbname: str) -> None:
    """Cross-check the connection's own reported identity against the
    DSN-declared database name, defending against DNS/proxy redirection
    making a DSN merely *look* disposable.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        row = cur.fetchone()
    actual = str(row[0]).strip().lower() if row else ""
    if actual != expected_dbname:
        raise V48DisposableDsnRejectedError(
            f"connected database identity {actual!r} does not match the "
            f"DSN-declared disposable database name {expected_dbname!r}; "
            "refusing (possible proxy or redirect)"
        )


def open_disposable_v48_connection(dsn: Optional[str], *, allow_disposable_v48_db: bool):
    """Validate, connect to, and identity-verify a disposable V48 database.

    Requires *both* an explicit ``allow_disposable_v48_db=True`` opt-in and a
    DSN that independently passes ``validate_disposable_dsn`` — two
    independent factors, on top of the live identity cross-check performed
    once connected. Returns a plain ``psycopg2`` connection with
    ``autocommit`` disabled and no open transaction yet.
    """
    if not allow_disposable_v48_db:
        raise V48DisposableDsnRejectedError(
            "V48 disposable-database execution requires explicit opt-in "
            "(allow_disposable_v48_db=True / --allow-disposable-v48-db)"
        )

    info = validate_disposable_dsn(dsn)

    if not is_psycopg2_available():
        raise V48DatabaseUnavailableError(
            "psycopg2 is not installed; cannot connect to the V48 disposable database"
        )

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:  # noqa: BLE001 - report cleanly, never a raw traceback
        raise V48DatabaseUnavailableError(
            f"could not connect to V48 disposable database at host {info.host!r}: "
            f"{type(exc).__name__}"
        ) from exc

    try:
        conn.autocommit = False
        _verify_live_database_identity(conn, info.dbname)
    except Exception:
        conn.close()
        raise

    return conn


def verify_v48_schema_compatibility(conn) -> None:
    """Raise ``V48MigrationCompatibilityError`` unless every required V48
    table and RPC exists in *conn*'s database.

    The live table/RPC existence check is the authoritative, mechanism-
    agnostic gate (this repository's disposable V48 test container is not
    guaranteed to be provisioned through the Supabase CLI's migration
    tracking). When ``supabase_migrations.schema_migrations`` happens to be
    present, required V48 migration versions are also cross-checked as an
    additional, best-effort signal.
    """
    missing_tables: List[str] = []
    missing_rpcs: List[str] = []

    with conn.cursor() as cur:
        for table in REQUIRED_V48_TABLES:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            row = cur.fetchone()
            if not row or row[0] is None:
                missing_tables.append(table)

        for rpc_name in REQUIRED_V48_RPCS:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND p.proname = %s
                """,
                (rpc_name,),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                missing_rpcs.append(rpc_name)

        missing_migration_versions: List[str] = []
        cur.execute("SELECT to_regclass('supabase_migrations.schema_migrations')")
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute("SELECT version FROM supabase_migrations.schema_migrations")
            applied = {str(r[0]) for r in cur.fetchall()}
            missing_migration_versions = sorted(
                v for v in REQUIRED_V48_MIGRATION_VERSIONS if v not in applied
            )

    if missing_tables or missing_rpcs or missing_migration_versions:
        raise V48MigrationCompatibilityError(
            "V48 disposable database is missing required schema objects: "
            f"tables={missing_tables}, rpcs={missing_rpcs}, "
            f"missing_migration_versions={missing_migration_versions}"
        )


@contextmanager
def v48_disposable_transaction(dsn: Optional[str], *, allow_disposable_v48_db: bool):
    """Open a disposable V48 connection, verify schema compatibility, BEGIN
    a transaction, yield ``(conn, client)``, and unconditionally ROLLBACK +
    close on exit — on success, on any exception raised inside the ``with``
    block, or on an exception raised by schema verification itself. Never
    commits under any code path.
    """
    conn = open_disposable_v48_connection(dsn, allow_disposable_v48_db=allow_disposable_v48_db)
    try:
        verify_v48_schema_compatibility(conn)
        with conn.cursor() as cur:
            cur.execute("BEGIN")
        client = PsycopgV48Client(conn)
        yield conn, client
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Seeding — real rows for one benchmark case's question + frozen evidence
# ---------------------------------------------------------------------------

_SEED_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "certbound.v58-quality-04c.benchmark-v48-seed"
)


@dataclass(frozen=True)
class SeededV48Case:
    question_version_id: str
    question_id: int
    evidence_set_hash: str
    evidence_chunk_count: int


def _stable_question_id(case_id: str) -> int:
    """Deterministic per-case integer id, kept well outside any plausible
    production question-id range.
    """
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    return 900_000_000 + (int(digest[:8], 16) % 90_000_000)


def _stable_uuid(*parts: str) -> str:
    return str(uuid.uuid5(_SEED_NAMESPACE, "|".join(parts)))


def _stable_certification_code(certification: str) -> str:
    digest = hashlib.sha256(certification.encode("utf-8")).hexdigest()
    return f"BM{digest[:8].upper()}"


@dataclass(frozen=True)
class _NormalizedChunk:
    rank: int
    resource_chunk_id: str
    official_resource_id: str
    resource_version_id: str
    content_hash: str
    chunk_text: str
    title: str
    chunk_index: int


def _validate_and_normalize_chunks(case_id: str, chunks: List[Any]) -> List[_NormalizedChunk]:
    """Validate every evidence chunk *before any SQL is issued* so a
    malformed/empty case fails closed without touching the connection at
    all (never inventing missing evidence, never partially seeding).
    """
    normalized: List[_NormalizedChunk] = []
    for rank, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, Mapping):
            raise V48EvidenceUnavailableError(
                f"case {case_id!r} resource_snapshot.chunks[{rank - 1}] must be a JSON object"
            )
        resource_chunk_id = str(chunk.get("resource_chunk_id") or "").strip().lower()
        if not resource_chunk_id:
            raise V48EvidenceUnavailableError(
                f"case {case_id!r} chunk at rank {rank} has no resource_chunk_id"
            )
        content_hash_chunk = str(chunk.get("content_hash") or "").strip()
        if not content_hash_chunk:
            raise V48EvidenceUnavailableError(
                f"case {case_id!r} chunk {resource_chunk_id!r} has no "
                "content_hash; refusing to fabricate evidence integrity data"
            )
        chunk_text = str(chunk.get("chunk_text") or "").strip()
        if not chunk_text:
            raise V48EvidenceUnavailableError(
                f"case {case_id!r} chunk {resource_chunk_id!r} has no "
                "chunk_text; refusing to seed empty evidence"
            )
        official_resource_id = str(
            chunk.get("official_resource_id") or _stable_uuid("res", resource_chunk_id)
        ).strip().lower()
        resource_version_id = str(
            chunk.get("resource_version_id") or _stable_uuid("rv", resource_chunk_id)
        ).strip().lower()
        title = str(chunk.get("resource_title") or "").strip() or "Untitled resource"
        chunk_index = int(chunk.get("chunk_index") or 0)
        normalized.append(
            _NormalizedChunk(
                rank=rank,
                resource_chunk_id=resource_chunk_id,
                official_resource_id=official_resource_id,
                resource_version_id=resource_version_id,
                content_hash=content_hash_chunk,
                chunk_text=chunk_text,
                title=title,
                chunk_index=chunk_index,
            )
        )
    return normalized


def seed_benchmark_case(conn, case: Mapping[str, Any]) -> Tuple[SeededV48Case, List[Dict[str, Any]]]:
    """Seed the minimum real rows for one benchmark case's question and its
    own frozen evidence snapshot, inside *conn*'s already-open transaction.

    Uses the case's own ``resource_snapshot`` chunk/resource/version ids and
    content hashes verbatim — this is the case's real, previously-frozen
    evidence (see ``workers/fixtures/quality_benchmark_v1.json``), never
    invented or fabricated. Raises ``V48EvidenceUnavailableError`` if the
    case has no usable evidence chunk, rather than silently running the
    audit ungrounded.

    Returns the seeded identifiers plus the ``p_evidence_chunks`` payload
    ready for ``create_or_get_ai_quality_audit_run_v1``.
    """
    case_id = str(case["case_id"])
    question = case["question"]
    resource_snapshot = case.get("resource_snapshot") or {}
    chunks = resource_snapshot.get("chunks") or []
    if not chunks:
        raise V48EvidenceUnavailableError(
            f"case {case_id!r} has no resource_snapshot.chunks; refusing to "
            "run the V48 audit ungrounded rather than inventing evidence"
        )

    certification = str(case["certification"]).strip()
    if not certification:
        raise V48EvidenceUnavailableError(f"case {case_id!r} has no certification")
    domain = str(case.get("domain") or "General").strip() or "General"
    question_text = str(question["question_text"]).strip()
    explanation = str(question.get("explanation") or "").strip()
    question_type = str(question.get("question_type") or "single").strip()
    select_count = int(question.get("select_count") or 1)
    options = question.get("options") or []
    if not options:
        raise V48EvidenceUnavailableError(f"case {case_id!r} question has no options")

    # Validate every chunk *before* issuing any SQL — a malformed/empty
    # case must fail closed without touching the connection at all.
    normalized_chunks = _validate_and_normalize_chunks(case_id, chunks)

    question_version_id = _stable_uuid("qv", case_id)
    question_id = _stable_question_id(case_id)
    content_hash = hashlib.sha256(
        f"{case_id}|{question_text}|{explanation}".encode("utf-8")
    ).hexdigest()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.certifications (
                exam_name, display_name, certification_code,
                passing_score, time_limit_minutes, question_count, is_active
            ) VALUES (%s, %s, %s, 65, 105, 60, true)
            ON CONFLICT (exam_name) DO NOTHING
            """,
            (certification, certification, _stable_certification_code(certification)),
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
            ) VALUES (%s, %s, %s, 'medium', %s, %s, %s, %s, true, true, 'en')
            ON CONFLICT (id) DO UPDATE SET exam_name = EXCLUDED.exam_name
            """,
            (
                question_id,
                certification,
                domain,
                question_text,
                question_type,
                select_count,
                explanation,
            ),
        )
        cur.execute(
            """
            INSERT INTO public.question_versions (
                id, question_id, version_number, question_text, explanation,
                category, difficulty, question_type, select_count, language_code,
                content_hash, source_type, created_by
            ) VALUES (%s, %s, 1, %s, %s, %s, 'medium', %s, %s, 'en', %s, 'manual', %s)
            """,
            (
                question_version_id,
                question_id,
                question_text,
                explanation,
                domain,
                question_type,
                select_count,
                content_hash,
                f"benchmark-v58-04c:{case_id}",
            ),
        )
        for option in options:
            cur.execute(
                """
                INSERT INTO public.question_option_versions (
                    question_version_id, option_label, option_text,
                    display_order, is_correct
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    question_version_id,
                    str(option["option_label"]),
                    str(option["option_text"]),
                    int(option.get("display_order") or 0),
                    bool(option.get("is_correct", False)),
                ),
            )

        evidence_rank_rows: List[Dict[str, Any]] = []
        for chunk in normalized_chunks:
            cur.execute(
                """
                INSERT INTO public.official_resources (
                    id, certification_exam_name, resource_type, title,
                    metadata, is_active, created_by
                ) VALUES (%s, %s, 'official_documentation', %s, '{}'::jsonb, true, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (chunk.official_resource_id, certification, chunk.title, f"benchmark-v58-04c:{case_id}"),
            )
            cur.execute(
                """
                INSERT INTO public.resource_versions (
                    id, resource_id, version_number, content_text, content_hash, created_by
                ) VALUES (%s, %s, 1, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    chunk.resource_version_id,
                    chunk.official_resource_id,
                    chunk.chunk_text,
                    chunk.content_hash,
                    f"benchmark-v58-04c:{case_id}",
                ),
            )
            cur.execute(
                """
                INSERT INTO public.resource_chunks (
                    id, resource_version_id, chunk_index, chunk_text, content_hash
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    chunk.resource_chunk_id,
                    chunk.resource_version_id,
                    chunk.chunk_index,
                    chunk.chunk_text,
                    chunk.content_hash,
                ),
            )
            evidence_rank_rows.append(
                {
                    "retrieval_rank": chunk.rank,
                    "resource_chunk_id": chunk.resource_chunk_id,
                    "content_hash": chunk.content_hash,
                }
            )

    evidence_set_hash = compute_evidence_set_hash(evidence_rank_rows)
    evidence_chunks_payload = build_create_run_evidence_payload(evidence_rank_rows)

    seeded = SeededV48Case(
        question_version_id=question_version_id,
        question_id=question_id,
        evidence_set_hash=evidence_set_hash,
        evidence_chunk_count=len(evidence_rank_rows),
    )
    return seeded, evidence_chunks_payload


def create_v48_audit_run(
    client: PsycopgV48Client,
    seeded: SeededV48Case,
    evidence_chunks_payload: List[Dict[str, Any]],
    *,
    created_by: str,
    prompt_version: str,
    ruleset_version: str,
    primary_model_name: str,
    dispute_model_name: str,
    pilot_batch_id: str,
) -> str:
    """Call the real ``create_or_get_ai_quality_audit_run_v1`` RPC unchanged."""
    row = client.rpc(
        "create_or_get_ai_quality_audit_run_v1",
        {
            "p_target_question_version_id": seeded.question_version_id,
            "p_prompt_version": prompt_version,
            "p_ruleset_version": ruleset_version,
            "p_primary_model_name": primary_model_name,
            "p_dispute_model_name": dispute_model_name,
            "p_pilot_batch_id": pilot_batch_id,
            "p_evidence_set_hash": seeded.evidence_set_hash,
            "p_evidence_chunks": evidence_chunks_payload,
            "p_created_by": created_by,
            "p_metadata": {},
        },
    ).execute()
    if row.error:
        raise V48DisposableDatabaseError(
            f"create_or_get_ai_quality_audit_run_v1 failed: {row.error}"
        )
    return str(row.data[0]["audit_run_id"])


def read_v48_findings(client: PsycopgV48Client, audit_run_id: str) -> List[Dict[str, Any]]:
    """Read back the real, RPC-persisted findings for a completed audit run."""
    result = (
        client.table("audit_findings")
        .select("finding_code, finding_type, severity, materiality, title, description, metadata")
        .eq("audit_run_id", audit_run_id)
        .execute()
    )
    if result.error:
        raise V48DisposableDatabaseError(f"failed to read audit_findings: {result.error}")
    return list(result.data or [])


# ---------------------------------------------------------------------------
# Top-level: one benchmark case, real V48 pipeline, guaranteed rollback
# ---------------------------------------------------------------------------


def run_v48_benchmark_case(
    case: Mapping[str, Any],
    *,
    dsn: Optional[str],
    allow_disposable_v48_db: bool,
    providers: AiQualityAuditProviders,
    worker_id: str = DEFAULT_WORKER_ID,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
    primary_model_name: str = DEFAULT_PRIMARY_MODEL_NAME,
    dispute_model_name: str = DEFAULT_DISPUTE_MODEL_NAME,
    pilot_batch_id: str = DEFAULT_PILOT_BATCH_ID,
) -> CasePrediction:
    """Seed one benchmark case, run the real V48 worker against it inside a
    disposable-database transaction, and return a ``CasePrediction``.

    Whole-run blockers (DSN rejected, schema/migration incompatible,
    database unreachable) propagate as their specific
    ``V48DisposableDatabaseError`` subclasses so callers can distinguish
    "this whole engine is unavailable" from "this one case failed" — see
    ``generate_v48_prediction`` for the case-vs-engine split used by the
    benchmark CLI/adapter. Every other failure (missing per-case evidence,
    a provider exception, a worker error) is converted into this case's
    ``CasePrediction.error`` rather than propagating, and the transaction is
    always rolled back by ``v48_disposable_transaction`` regardless of which
    branch is taken.
    """
    case_id = str(case["case_id"])
    with v48_disposable_transaction(dsn, allow_disposable_v48_db=allow_disposable_v48_db) as (
        conn,
        client,
    ):
        try:
            seeded, evidence_payload = seed_benchmark_case(conn, case)
            audit_run_id = create_v48_audit_run(
                client,
                seeded,
                evidence_payload,
                created_by=worker_id,
                prompt_version=prompt_version,
                ruleset_version=ruleset_version,
                primary_model_name=primary_model_name,
                dispute_model_name=dispute_model_name,
                pilot_batch_id=pilot_batch_id,
            )
            summary = process_ai_quality_audit_job(
                client,
                {
                    "audit_run_id": audit_run_id,
                    "question_version_id": seeded.question_version_id,
                },
                providers,
                worker_id=worker_id,
            )
            findings: List[Dict[str, Any]] = []
            if summary.get("run_status") == "completed" and summary.get("finding_count"):
                findings = read_v48_findings(client, audit_run_id)
        except Exception as exc:  # noqa: BLE001 - never silently drop a case
            return CasePrediction(case_id=case_id, error=f"{type(exc).__name__}: {exc}")

        return _prediction_from_findings(
            case_id,
            findings,
            raw_output_extra={
                "run_status": summary.get("run_status"),
                "passes_executed": summary.get("passes_executed"),
                "completion_shape": summary.get("completion_shape"),
                "audit_run_id": audit_run_id,
                "evidence_chunk_count": seeded.evidence_chunk_count,
            },
            error=(
                None
                if summary.get("run_status") == "completed"
                else f"V48 run did not complete (run_status={summary.get('run_status')!r})"
            ),
        )


def generate_v48_prediction(
    case: Mapping[str, Any],
    *,
    dsn: Optional[str],
    allow_disposable_v48_db: bool,
    providers: AiQualityAuditProviders,
    worker_id: str = DEFAULT_WORKER_ID,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
    primary_model_name: str = DEFAULT_PRIMARY_MODEL_NAME,
    dispute_model_name: str = DEFAULT_DISPUTE_MODEL_NAME,
    pilot_batch_id: str = DEFAULT_PILOT_BATCH_ID,
) -> CasePrediction:
    """Entry point used by ``V48EngineAdapter``: run one benchmark case
    through the real, disposable-database-backed V48 pipeline.

    Whole-engine blockers (DSN rejected, schema incompatible, database
    unreachable) are re-raised as ``EngineAdapterUnavailableError`` — the
    same signal ``LegacyEngineAdapter``/the CLI already treat as "the whole
    engine is unavailable, stop generating predictions" rather than a
    per-case error, so a down/misconfigured disposable database produces one
    clear BLOCKED message instead of N identical per-case errors.
    """
    try:
        return run_v48_benchmark_case(
            case,
            dsn=dsn,
            allow_disposable_v48_db=allow_disposable_v48_db,
            providers=providers,
            worker_id=worker_id,
            prompt_version=prompt_version,
            ruleset_version=ruleset_version,
            primary_model_name=primary_model_name,
            dispute_model_name=dispute_model_name,
            pilot_batch_id=pilot_batch_id,
        )
    except (
        V48DisposableDsnRejectedError,
        V48MigrationCompatibilityError,
        V48DatabaseUnavailableError,
    ) as exc:
        raise EngineAdapterUnavailableError(
            reason=(
                "V48 disposable-database execution is unavailable: "
                f"{type(exc).__name__}: {exc}"
            ),
            follow_up=(
                "Provide a reachable, schema-compatible disposable V48 test "
                "database via --v48-db-url (see "
                "tests/test_ai_quality_audit_integration.py for the expected "
                "container/database setup), or omit --engine v48 entirely."
            ),
        ) from exc


__all__ = [
    "V48DisposableDatabaseError",
    "V48DisposableDsnRejectedError",
    "V48MigrationCompatibilityError",
    "V48DatabaseUnavailableError",
    "V48EvidenceUnavailableError",
    "DisposableDsnInfo",
    "validate_disposable_dsn",
    "open_disposable_v48_connection",
    "verify_v48_schema_compatibility",
    "v48_disposable_transaction",
    "SeededV48Case",
    "seed_benchmark_case",
    "create_v48_audit_run",
    "read_v48_findings",
    "run_v48_benchmark_case",
    "generate_v48_prediction",
    "REQUIRED_V48_TABLES",
    "REQUIRED_V48_RPCS",
    "ALLOWED_DISPOSABLE_HOSTS",
    "ALLOWED_DISPOSABLE_DB_NAME_RE",
]
