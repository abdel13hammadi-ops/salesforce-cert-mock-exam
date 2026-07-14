"""
Ingest verified official-evidence fixture packages into runtime evidence tables.

Uses the existing ``ingest_resource_version_v1`` persistence path (via
``make_resource_ingestion_handler``) after ensuring ``official_resources``
catalog rows exist. Fixture text is submitted verbatim — no network refetch.

Disposable/local databases only unless explicitly allowed via environment flag.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from workers.certification_registry import PAB_EXAM_NAME
from workers.job_handlers import make_resource_ingestion_handler
from workers.official_evidence_seed import (
    FIXTURE_VERSION,
    OfficialEvidenceSeedError,
    PAB_DEFAULT_OUTPUT_PATH,
    PAB_EVIDENCE_CONFIG_ID,
    PAB_FIXTURE_VERSION,
    filter_fixture_items_by_certification,
    load_official_evidence_fixture,
    validate_fixture_payload,
)
from workers.run_resource_ingestion import validate_ingest_payload
from workers.v48_psycopg_client import PsycopgV48Client

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore

ALLOW_FIXTURE_INGEST_FLAG = "CERTBOUND_ALLOW_FIXTURE_INGEST"
DEFAULT_CREATED_BY = "official-evidence-fixture-ingestion"

PRODUCTION_HOST_MARKERS = ("supabase.co", "supabase.in")

KNOWN_EVIDENCE_PACKAGES: Dict[str, Dict[str, Any]] = {
    PAB_FIXTURE_VERSION: {
        "evidence_config_id": PAB_EVIDENCE_CONFIG_ID,
        "certification_exam_name": PAB_EXAM_NAME,
        "expected_record_count": 7,
        "expected_domain_count": 5,
        "default_fixture_path": PAB_DEFAULT_OUTPUT_PATH,
    },
}


class OfficialEvidenceFixtureIngestionError(RuntimeError):
    """Base error for fixture ingestion failures."""


class OfficialEvidenceFixtureIngestionConflictError(
    OfficialEvidenceFixtureIngestionError
):
    """Raised when an existing runtime row conflicts with fixture identity."""


class OfficialEvidenceFixtureIngestionSafetyError(
    OfficialEvidenceFixtureIngestionError
):
    """Raised when ingestion is attempted against an unsafe database target."""


@dataclass(frozen=True)
class IngestedFixtureItemResult:
    official_resource_id: str
    resource_version_id: str
    version_number: int
    chunk_count: int
    content_hash: str
    domain: str
    idempotent: bool = False


@dataclass
class PackageIngestionResult:
    fixture_path: str
    fixture_version: str
    evidence_config_id: str
    certification_exam_name: str
    dry_run: bool
    item_count: int
    domains_covered: Tuple[str, ...]
    items: List[IngestedFixtureItemResult] = field(default_factory=list)
    resources_created: int = 0
    resources_existing: int = 0
    versions_created: int = 0
    versions_idempotent: int = 0
    chunks_created: int = 0

    def to_summary_dict(self) -> Dict[str, Any]:
        return {
            "fixture_path": self.fixture_path,
            "fixture_version": self.fixture_version,
            "evidence_config_id": self.evidence_config_id,
            "certification_exam_name": self.certification_exam_name,
            "dry_run": self.dry_run,
            "item_count": self.item_count,
            "domains_covered": list(self.domains_covered),
            "resources_created": self.resources_created,
            "resources_existing": self.resources_existing,
            "versions_created": self.versions_created,
            "versions_idempotent": self.versions_idempotent,
            "chunks_created": self.chunks_created,
            "items": [
                {
                    "official_resource_id": item.official_resource_id,
                    "resource_version_id": item.resource_version_id,
                    "version_number": item.version_number,
                    "chunk_count": item.chunk_count,
                    "content_hash": item.content_hash,
                    "domain": item.domain,
                    "idempotent": item.idempotent,
                }
                for item in self.items
            ],
        }


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def assert_fixture_ingest_allowed(
    *,
    database_url: Optional[str],
    dry_run: bool,
) -> None:
    """Refuse production-like targets unless disposable DSN + explicit flag."""
    if dry_run:
        return
    if running_under_pytest():
        return
    if os.environ.get(ALLOW_FIXTURE_INGEST_FLAG) != "1":
        raise OfficialEvidenceFixtureIngestionSafetyError(
            f"Refusing fixture ingestion. Set {ALLOW_FIXTURE_INGEST_FLAG}=1 "
            "and pass an approved disposable --database-url."
        )
    if not database_url or not database_url.strip():
        raise OfficialEvidenceFixtureIngestionSafetyError(
            "Refusing fixture ingestion without an explicit disposable --database-url."
        )
    from workers.quality_benchmark_v48_orchestration import (
        V48DisposableDsnRejectedError,
        validate_disposable_dsn,
    )

    try:
        validate_disposable_dsn(database_url)
    except V48DisposableDsnRejectedError as exc:
        raise OfficialEvidenceFixtureIngestionSafetyError(str(exc)) from exc


def reject_production_like_dsn(database_url: str) -> None:
    """Structural guard against obvious production Supabase hosts."""
    lowered = database_url.strip().lower()
    for marker in PRODUCTION_HOST_MARKERS:
        if marker in lowered:
            raise OfficialEvidenceFixtureIngestionSafetyError(
                f"database URL contains production marker {marker!r}; refusing"
            )


def resolve_package_config(fixture_version: str) -> Dict[str, Any]:
    config = KNOWN_EVIDENCE_PACKAGES.get(fixture_version)
    if config is None:
        raise OfficialEvidenceFixtureIngestionError(
            f"unknown fixture identity {fixture_version!r}; "
            f"supported identities: {sorted(KNOWN_EVIDENCE_PACKAGES)}"
        )
    return config


def validate_fixture_for_ingestion(
    payload: Mapping[str, Any],
    *,
    fixture_path: Optional[Path | str] = None,
    expected_fixture_version: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Validate fixture structure and return (fixture_version, package_config)."""
    validate_fixture_payload(payload)

    fixture_version = str(payload.get("fixture_version") or "").strip()
    if not fixture_version:
        raise OfficialEvidenceFixtureIngestionError(
            "fixture is missing required field 'fixture_version'"
        )
    if expected_fixture_version and fixture_version != expected_fixture_version:
        raise OfficialEvidenceFixtureIngestionError(
            f"fixture_version {fixture_version!r} does not match expected "
            f"{expected_fixture_version!r}"
        )

    package_config = resolve_package_config(fixture_version)
    expected_config_id = str(package_config["evidence_config_id"])
    actual_config_id = str(payload.get("evidence_config_id") or "").strip()
    if actual_config_id != expected_config_id:
        raise OfficialEvidenceFixtureIngestionError(
            f"evidence_config_id {actual_config_id!r} does not match expected "
            f"{expected_config_id!r} for fixture {fixture_version!r}"
        )

    expected_cert = str(package_config["certification_exam_name"])
    certs_covered = payload.get("certifications_covered") or []
    if not isinstance(certs_covered, list):
        raise OfficialEvidenceFixtureIngestionError(
            "certifications_covered must be an array"
        )
    normalized_certs = {str(item).strip() for item in certs_covered}
    if normalized_certs != {expected_cert}:
        raise OfficialEvidenceFixtureIngestionError(
            f"fixture certifications_covered {sorted(normalized_certs)!r} must "
            f"contain only {expected_cert!r}"
        )

    items = filter_fixture_items_by_certification(payload, expected_cert)
    expected_count = int(package_config["expected_record_count"])
    if len(items) != expected_count:
        raise OfficialEvidenceFixtureIngestionError(
            f"fixture must contain exactly {expected_count} records for "
            f"{expected_cert!r}, found {len(items)}"
        )

    domains = {str(item.get("domain") or "").strip() for item in items}
    expected_domain_count = int(package_config["expected_domain_count"])
    if len(domains) != expected_domain_count:
        raise OfficialEvidenceFixtureIngestionError(
            f"fixture must cover exactly {expected_domain_count} domains, "
            f"found {len(domains)}: {sorted(domains)}"
        )

    if fixture_path is not None:
        path = Path(fixture_path)
        if not path.exists():
            raise OfficialEvidenceFixtureIngestionError(
                f"evidence fixture not found: {path}"
            )

    return fixture_version, package_config


def load_fixture_for_ingestion(
    fixture_path: Path | str,
    *,
    expected_fixture_version: Optional[str] = None,
) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    path = Path(fixture_path)
    payload = load_official_evidence_fixture(path)
    fixture_version, package_config = validate_fixture_for_ingestion(
        payload,
        fixture_path=path,
        expected_fixture_version=expected_fixture_version,
    )
    return payload, fixture_version, package_config


def fixture_item_to_catalog_row(
    item: Mapping[str, Any],
    *,
    created_by: str,
    package_identity: str,
    evidence_config_id: str,
) -> Dict[str, Any]:
    domain_tags = list(item.get("domain_tags") or [item.get("domain")])
    canonical = str(item.get("canonical_url") or "").strip()
    publisher = str(item.get("publisher") or "").strip()
    return {
        "id": str(item["official_resource_id"]).strip().lower(),
        "certification_exam_name": str(item["certification"]).strip(),
        "resource_type": str(item.get("resource_type") or "").strip(),
        "title": str(item.get("resource_title") or "").strip(),
        "canonical_url": canonical or None,
        "publisher": publisher or None,
        "is_active": True,
        "created_by": created_by,
        "metadata": {
            "domain": item.get("domain"),
            "domains": domain_tags,
            "certification_code": item.get("certification_code"),
            "evidence_package_identity": package_identity,
            "evidence_config_id": evidence_config_id,
            "provenance_status": item.get("provenance_status"),
            "fixture_source_url": item.get("source_url"),
        },
    }


def fixture_item_to_ingest_payload(
    item: Mapping[str, Any],
    *,
    created_by: str,
    package_identity: str,
    evidence_config_id: str,
) -> Dict[str, Any]:
    excerpt = str(item["chunk_text_excerpt"])
    content_hash = str(item["content_hash"])
    domain_tags = list(item.get("domain_tags") or [item.get("domain")])
    payload: Dict[str, Any] = {
        "resource_id": str(item["official_resource_id"]).strip().lower(),
        "content_text": excerpt,
        "content_hash": content_hash,
        "created_by": created_by,
        "chunks": [
            {
                "chunk_index": int(item.get("chunk_index") or 0),
                "chunk_text": excerpt,
                "content_hash": content_hash,
                "metadata": {
                    "domain": item.get("domain"),
                    "domains": domain_tags,
                    "fixture_chunk_id": str(item["resource_chunk_id"]).strip().lower(),
                    "fixture_version_id": str(item["resource_version_id"]).strip().lower(),
                    "evidence_package_identity": package_identity,
                },
            }
        ],
        "metadata": {
            "ingestion_source": "official_evidence_fixture",
            "evidence_package_identity": package_identity,
            "evidence_config_id": evidence_config_id,
            "provenance_status": item.get("provenance_status"),
            "fixture_version_number": item.get("version_number"),
        },
    }
    source_url = item.get("source_url")
    if source_url:
        payload["source_url"] = str(source_url).strip()
    source_external_version = item.get("source_external_version")
    if source_external_version:
        payload["source_external_version"] = str(source_external_version).strip()
    effective_at = item.get("effective_at")
    if effective_at:
        payload["effective_at"] = str(effective_at).strip()
    return payload


def _normalize_metadata_for_compare(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    domain = str(metadata.get("domain") or "").strip()
    domains = sorted(str(item).strip() for item in (metadata.get("domains") or []))
    return {
        "domain": domain,
        "domains": domains,
        "certification_code": str(metadata.get("certification_code") or "").strip(),
        "evidence_package_identity": str(
            metadata.get("evidence_package_identity") or ""
        ).strip(),
        "evidence_config_id": str(metadata.get("evidence_config_id") or "").strip(),
    }


def _assert_catalog_row_matches(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    resource_id: str,
) -> None:
    scalar_fields = (
        "certification_exam_name",
        "resource_type",
        "title",
        "canonical_url",
        "publisher",
    )
    for field_name in scalar_fields:
        existing_value = existing.get(field_name)
        expected_value = expected.get(field_name)
        if str(existing_value or "").strip() != str(expected_value or "").strip():
            raise OfficialEvidenceFixtureIngestionConflictError(
                f"official_resources {resource_id!r} conflicts on {field_name}: "
                f"existing={existing_value!r}, fixture={expected_value!r}"
            )

    existing_meta = existing.get("metadata") or {}
    expected_meta = expected.get("metadata") or {}
    if not isinstance(existing_meta, dict) or not isinstance(expected_meta, dict):
        raise OfficialEvidenceFixtureIngestionConflictError(
            f"official_resources {resource_id!r} has invalid metadata shape"
        )
    if _normalize_metadata_for_compare(existing_meta) != _normalize_metadata_for_compare(
        expected_meta
    ):
        raise OfficialEvidenceFixtureIngestionConflictError(
            f"official_resources {resource_id!r} conflicts on metadata domain/"
            f"certification fields: existing={existing_meta!r}, fixture={expected_meta!r}"
        )


def _fetch_catalog_row(conn, resource_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, certification_exam_name, resource_type, title,
                   canonical_url, publisher, is_active, created_by, metadata
            FROM public.official_resources
            WHERE id = %s
            """,
            (resource_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _count_rows(conn, table: str, *, certification: Optional[str] = None) -> int:
    with conn.cursor() as cur:
        if certification is None:
            cur.execute(f"SELECT COUNT(*) FROM public.{table}")
        else:
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM public.{table} t
                JOIN public.official_resources r ON r.id = t.resource_id
                WHERE r.certification_exam_name = %s
                """,
                (certification,),
            )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _count_chunks_for_cert(conn, certification: str) -> int:
    with conn.cursor() as cur:
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
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _fetch_version_hash(
    conn,
    resource_id: str,
    *,
    version_number: int,
) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT content_hash
            FROM public.resource_versions
            WHERE resource_id = %s AND version_number = %s
            """,
            (resource_id, version_number),
        )
        row = cur.fetchone()
        return str(row[0]).strip() if row else None


def _assert_version_hash_compatible(
    conn,
    resource_id: str,
    *,
    expected_hash: str,
    version_number: int,
) -> None:
    existing_hash = _fetch_version_hash(
        conn, resource_id, version_number=version_number
    )
    if existing_hash is None:
        return
    if existing_hash != expected_hash:
        raise OfficialEvidenceFixtureIngestionConflictError(
            f"resource_versions for {resource_id!r} version {version_number} "
            f"conflicts on content_hash: existing={existing_hash!r}, "
            f"fixture={expected_hash!r}"
        )


def ensure_official_resource_catalog_row(
    conn,
    item: Mapping[str, Any],
    *,
    created_by: str,
    package_identity: str,
    evidence_config_id: str,
    dry_run: bool,
) -> str:
    expected = fixture_item_to_catalog_row(
        item,
        created_by=created_by,
        package_identity=package_identity,
        evidence_config_id=evidence_config_id,
    )
    resource_id = expected["id"]
    existing = _fetch_catalog_row(conn, resource_id)
    if existing is None:
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.official_resources (
                        id, certification_exam_name, resource_type, title,
                        canonical_url, publisher, is_active, created_by, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                    )
                    """,
                    (
                        resource_id,
                        expected["certification_exam_name"],
                        expected["resource_type"],
                        expected["title"],
                        expected["canonical_url"],
                        expected["publisher"],
                        expected["is_active"],
                        expected["created_by"],
                        json.dumps(expected["metadata"]),
                    ),
                )
        return "created"
    _assert_catalog_row_matches(existing, expected, resource_id=resource_id)
    return "existing"


def ingest_fixture_item(
    conn,
    item: Mapping[str, Any],
    *,
    created_by: str,
    package_identity: str,
    evidence_config_id: str,
    dry_run: bool,
    handler,
) -> Tuple[IngestedFixtureItemResult, str, bool]:
    resource_id = str(item["official_resource_id"]).strip().lower()
    expected_hash = str(item["content_hash"])
    version_number = int(item.get("version_number") or 1)
    _assert_version_hash_compatible(
        conn,
        resource_id,
        expected_hash=expected_hash,
        version_number=version_number,
    )

    payload = fixture_item_to_ingest_payload(
        item,
        created_by=created_by,
        package_identity=package_identity,
        evidence_config_id=evidence_config_id,
    )
    validate_ingest_payload(payload)

    if dry_run:
        return (
            IngestedFixtureItemResult(
                official_resource_id=resource_id,
                resource_version_id=str(item["resource_version_id"]).strip().lower(),
                version_number=version_number,
                chunk_count=1,
                content_hash=expected_hash,
                domain=str(item.get("domain") or ""),
                idempotent=False,
            ),
            "validated",
            False,
        )

    before_version_count = _count_rows(conn, "resource_versions")
    before_chunk_count = _count_chunks_for_cert(
        conn, str(item["certification"])
    )
    handler_result = handler("fixture-ingest", payload, {}, 1, lambda: None)
    after_version_count = _count_rows(conn, "resource_versions")
    after_chunk_count = _count_chunks_for_cert(conn, str(item["certification"]))
    idempotent = (
        after_version_count == before_version_count
        and after_chunk_count == before_chunk_count
    )
    return (
        IngestedFixtureItemResult(
            official_resource_id=resource_id,
            resource_version_id=str(handler_result.get("resource_version_id") or ""),
            version_number=int(handler_result.get("version_number") or version_number),
            chunk_count=int(handler_result.get("chunk_count") or 1),
            content_hash=expected_hash,
            domain=str(item.get("domain") or ""),
            idempotent=idempotent,
        ),
        "ingested",
        idempotent,
    )


def ingest_official_evidence_fixture_package(
    conn,
    payload: Mapping[str, Any],
    *,
    fixture_path: Path | str,
    created_by: str = DEFAULT_CREATED_BY,
    dry_run: bool = False,
) -> PackageIngestionResult:
    """Ingest one validated fixture package inside a single DB transaction."""
    if psycopg2 is None:
        raise OfficialEvidenceFixtureIngestionError("psycopg2 is required for ingestion")

    path = Path(fixture_path)
    fixture_version, package_config = validate_fixture_for_ingestion(
        payload, fixture_path=path
    )
    certification = str(package_config["certification_exam_name"])
    items = filter_fixture_items_by_certification(payload, certification)
    domains = tuple(sorted({str(item.get("domain") or "").strip() for item in items}))

    result = PackageIngestionResult(
        fixture_path=str(path),
        fixture_version=fixture_version,
        evidence_config_id=str(package_config["evidence_config_id"]),
        certification_exam_name=certification,
        dry_run=dry_run,
        item_count=len(items),
        domains_covered=domains,
    )

    client = PsycopgV48Client(conn)
    handler = make_resource_ingestion_handler(client)
    actor = created_by.strip()
    if not actor:
        raise OfficialEvidenceFixtureIngestionError("created_by must not be empty")

    previous_autocommit = conn.autocommit
    conn.autocommit = False
    try:
        for item in items:
            catalog_status = ensure_official_resource_catalog_row(
                conn,
                item,
                created_by=actor,
                package_identity=fixture_version,
                evidence_config_id=str(package_config["evidence_config_id"]),
                dry_run=dry_run,
            )
            if catalog_status == "created":
                result.resources_created += 1
            else:
                result.resources_existing += 1

            item_result, _, idempotent = ingest_fixture_item(
                conn,
                item,
                created_by=actor,
                package_identity=fixture_version,
                evidence_config_id=str(package_config["evidence_config_id"]),
                dry_run=dry_run,
                handler=handler,
            )
            result.items.append(item_result)
            if dry_run:
                continue
            if idempotent:
                result.versions_idempotent += 1
            else:
                result.versions_created += 1
                result.chunks_created += item_result.chunk_count

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = previous_autocommit

    return result


def format_package_summary(result: PackageIngestionResult) -> str:
    lines = [
        f"mode: {'dry-run' if result.dry_run else 'ingest'}",
        f"fixture_path: {result.fixture_path}",
        f"fixture_version: {result.fixture_version}",
        f"evidence_config_id: {result.evidence_config_id}",
        f"certification_exam_name: {result.certification_exam_name}",
        f"item_count: {result.item_count}",
        f"domains_covered: {', '.join(result.domains_covered)}",
        f"resources_created: {result.resources_created}",
        f"resources_existing: {result.resources_existing}",
        f"versions_created: {result.versions_created}",
        f"versions_idempotent: {result.versions_idempotent}",
        f"chunks_created: {result.chunks_created}",
    ]
    return "\n".join(lines)


def ingest_fixture_file(
    fixture_path: Path | str,
    *,
    database_url: str,
    created_by: str = DEFAULT_CREATED_BY,
    dry_run: bool = False,
    expected_fixture_version: Optional[str] = None,
) -> PackageIngestionResult:
    assert_fixture_ingest_allowed(database_url=database_url, dry_run=dry_run)
    reject_production_like_dsn(database_url)
    payload, _, _ = load_fixture_for_ingestion(
        fixture_path, expected_fixture_version=expected_fixture_version
    )
    conn = psycopg2.connect(database_url)
    try:
        return ingest_official_evidence_fixture_package(
            conn,
            payload,
            fixture_path=fixture_path,
            created_by=created_by,
            dry_run=dry_run,
        )
    finally:
        conn.close()


__all__ = [
    "ALLOW_FIXTURE_INGEST_FLAG",
    "DEFAULT_CREATED_BY",
    "KNOWN_EVIDENCE_PACKAGES",
    "OfficialEvidenceFixtureIngestionConflictError",
    "OfficialEvidenceFixtureIngestionError",
    "OfficialEvidenceFixtureIngestionSafetyError",
    "PackageIngestionResult",
    "assert_fixture_ingest_allowed",
    "fixture_item_to_catalog_row",
    "fixture_item_to_ingest_payload",
    "format_package_summary",
    "ingest_fixture_file",
    "ingest_official_evidence_fixture_package",
    "load_fixture_for_ingestion",
    "reject_production_like_dsn",
    "resolve_package_config",
    "validate_fixture_for_ingestion",
]
