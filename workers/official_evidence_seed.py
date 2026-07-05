"""
Read-only export helpers for verified official Salesforce evidence seeds (V58).

Selects bounded excerpts from active official_resources / resource_versions /
resource_chunks rows only. Never mutates production data.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

from workers.resource_chunking import sha256_hex
from workers.structural_audit_launcher import ADM_EXAM_NAME, BA_EXAM_NAME

FIXTURE_VERSION = "official-evidence-seed-v1"
DEFAULT_OUTPUT_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "official_evidence_seed_v1.json"
)
MAX_EXCERPT_CHARS = 1500
DEFAULT_TARGET_MIN_CHUNKS = 24
DEFAULT_TARGET_MAX_CHUNKS = 40
DEFAULT_MAX_CHUNKS_PER_RESOURCE = 3

CERTIFICATION_CODES = {
    ADM_EXAM_NAME: "ADM-201",
    BA_EXAM_NAME: "BA-201",
}

SUPPORTED_CERTIFICATIONS = frozenset(CERTIFICATION_CODES)

ALLOWED_RESOURCE_TYPES = frozenset({
    "exam_guide",
    "official_documentation",
    "release_notes",
    "help_article",
    "trailhead",
    "policy",
    "other",
})

PLACEHOLDER_CHUNK_ID_PATTERNS = (
    re.compile(r"^0{8}-0{4}-0{4}-0{4}-0{12}$", re.I),
    re.compile(r"^1{8}-1{4}-1{4}-1{4}-1{12}$", re.I),
    re.compile(r"^a{8}-0{4}-0{4}-0{4}-", re.I),
    re.compile(r"^b{8}-0{4}-0{4}-0{4}-", re.I),
    re.compile(r"^c{8}-0{4}-0{4}-0{4}-", re.I),
    re.compile(r"^d{8}-0{4}-0{4}-0{4}-", re.I),
    re.compile(r"^e{8}-0{4}-0{4}-0{4}-", re.I),
    re.compile(r"^f{8}-0{4}-0{4}-0{4}-", re.I),
)

SECRET_ENV_NAMES = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
CREDENTIAL_SUBSTRINGS = (
    "service_role",
    "eyJ",
    "supabase.co",
    "SUPABASE_",
)


class OfficialEvidenceSeedError(ValueError):
    """Raised when evidence seed export or validation fails."""


class OfficialEvidenceSeedConfigError(OfficialEvidenceSeedError):
    """Raised when Supabase configuration is missing."""


class OfficialEvidenceSeedOutputError(OfficialEvidenceSeedError):
    """Raised when output path rules are violated."""


@dataclass(frozen=True)
class ResourceInventory:
    resource_count_by_certification: Dict[str, int]
    active_resource_count_by_certification: Dict[str, int]
    resource_version_count: int
    chunk_count_by_certification: Dict[str, int]
    domains_by_certification: Dict[str, List[str]]
    eligible_chunk_count: int
    missing_canonical_url_count: int
    missing_publisher_count: int
    missing_content_hash_count: int
    provenance_complete_resource_count: int
    total_resource_count: int
    total_chunk_count: int


@dataclass(frozen=True)
class ExportSelection:
    items: Tuple[Dict[str, Any], ...]
    inventory: ResourceInventory


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_uuid(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfficialEvidenceSeedError(f"{field_name} must be a non-empty UUID string")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError as exc:
        raise OfficialEvidenceSeedError(f"{field_name} is not a valid UUID: {value!r}") from exc


def _is_placeholder_chunk_id(chunk_id: str) -> bool:
    normalized = chunk_id.strip().lower()
    return any(pattern.search(normalized) for pattern in PLACEHOLDER_CHUNK_ID_PATTERNS)


def extract_domain_tags(metadata: Mapping[str, Any] | None) -> List[str]:
    """Return normalized domain/topic tags from resource or chunk metadata."""
    if not isinstance(metadata, dict):
        return []
    tags: List[str] = []
    seen: Set[str] = set()
    for key in ("domain", "domains", "exam_domain", "category", "feature", "features", "topic", "topics"):
        raw = metadata.get(key)
        values: Iterable[str]
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, list):
            values = (str(item) for item in raw if isinstance(item, str))
        else:
            continue
        for value in values:
            cleaned = " ".join(str(value).strip().split())
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                tags.append(cleaned)
    return sorted(tags)


def bound_excerpt(text: str, *, max_chars: int = MAX_EXCERPT_CHARS) -> str:
    """Return a bounded excerpt: first paragraph block, capped at max_chars."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise OfficialEvidenceSeedError("chunk_text excerpt must not be empty")

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    excerpt = paragraphs[0] if paragraphs else normalized
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip()
        if " " in excerpt:
            excerpt = excerpt.rsplit(" ", 1)[0].rstrip()
    if not excerpt:
        raise OfficialEvidenceSeedError("bounded excerpt collapsed to empty text")
    if len(excerpt) > max_chars:
        raise OfficialEvidenceSeedError(
            f"bounded excerpt exceeds max_chars={max_chars} (got {len(excerpt)})"
        )
    return excerpt


def _require_https_url(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfficialEvidenceSeedError(f"{field_name} must be a non-empty URL")
    cleaned = value.strip()
    if not cleaned.lower().startswith("https://"):
        raise OfficialEvidenceSeedError(f"{field_name} must use https:// scheme")
    return cleaned


def _require_non_empty(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfficialEvidenceSeedError(f"{field_name} must be a non-empty string")
    return value.strip()


def _publisher_is_official(publisher: str) -> bool:
    lowered = publisher.lower()
    return "salesforce" in lowered or publisher.strip() == "Salesforce, Inc."


def validate_chunk_provenance(
    *,
    resource: Mapping[str, Any],
    version: Mapping[str, Any],
    chunk: Mapping[str, Any],
) -> None:
    """Raise OfficialEvidenceSeedError when provenance is incomplete or synthetic."""
    chunk_id = _normalize_uuid(chunk.get("id"), field_name="resource_chunks.id")
    if _is_placeholder_chunk_id(chunk_id):
        raise OfficialEvidenceSeedError(
            f"resource_chunks.id={chunk_id!r} matches a synthetic placeholder pattern"
        )

    resource_id = _normalize_uuid(resource.get("id"), field_name="official_resources.id")
    version_id = _normalize_uuid(version.get("id"), field_name="resource_versions.id")
    _normalize_uuid(
        chunk.get("resource_version_id"),
        field_name="resource_chunks.resource_version_id",
    )

    if not resource.get("is_active", False):
        raise OfficialEvidenceSeedError(
            f"official_resources.id={resource_id!r} is not active"
        )

    certification = _require_non_empty(
        resource.get("certification_exam_name"),
        field_name="official_resources.certification_exam_name",
    )
    if certification not in SUPPORTED_CERTIFICATIONS:
        raise OfficialEvidenceSeedError(
            f"unsupported certification_exam_name {certification!r}"
        )

    resource_type = _require_non_empty(resource.get("resource_type"), field_name="resource_type")
    if resource_type not in ALLOWED_RESOURCE_TYPES:
        raise OfficialEvidenceSeedError(f"unsupported resource_type {resource_type!r}")

    publisher = _require_non_empty(resource.get("publisher"), field_name="publisher")
    if not _publisher_is_official(publisher):
        raise OfficialEvidenceSeedError(
            f"publisher {publisher!r} is not recognized as official Salesforce provenance"
        )

    canonical_url = _require_https_url(
        resource.get("canonical_url"),
        field_name="canonical_url",
    )
    title = _require_non_empty(resource.get("title"), field_name="title")

    chunk_index = chunk.get("chunk_index")
    if not isinstance(chunk_index, int) or chunk_index < 0:
        raise OfficialEvidenceSeedError("chunk_index must be a non-negative integer")

    chunk_text = _require_non_empty(chunk.get("chunk_text"), field_name="chunk_text")
    stored_hash = _require_non_empty(chunk.get("content_hash"), field_name="content_hash")
    computed_hash = sha256_hex(chunk_text)
    if stored_hash.lower() != computed_hash.lower():
        raise OfficialEvidenceSeedError(
            f"resource_chunks.id={chunk_id!r} content_hash does not match chunk_text"
        )

    version_hash = _require_non_empty(version.get("content_hash"), field_name="version.content_hash")
    if not version_hash:
        raise OfficialEvidenceSeedError("resource_versions.content_hash must be present")

    # Silence unused-variable lint for validated fields used in export builder.
    _ = (canonical_url, title, version_hash)


def build_export_item(
    *,
    resource: Mapping[str, Any],
    version: Mapping[str, Any],
    chunk: Mapping[str, Any],
    exported_at: str,
) -> Dict[str, Any]:
    validate_chunk_provenance(resource=resource, version=version, chunk=chunk)

    resource_id = _normalize_uuid(resource["id"], field_name="official_resources.id")
    version_id = _normalize_uuid(version["id"], field_name="resource_versions.id")
    chunk_id = _normalize_uuid(chunk["id"], field_name="resource_chunks.id")
    certification = _require_non_empty(
        resource["certification_exam_name"],
        field_name="official_resources.certification_exam_name",
    )
    domain_tags = extract_domain_tags(resource.get("metadata"))
    domain_tags.extend(tag for tag in extract_domain_tags(chunk.get("metadata")) if tag not in domain_tags)
    domain_tags = sorted(set(domain_tags))
    primary_domain = domain_tags[0] if domain_tags else "General"

    excerpt = bound_excerpt(str(chunk["chunk_text"]))
    if sha256_hex(excerpt) == sha256_hex(str(chunk["chunk_text"])):
        excerpt_note = "full_chunk"
    else:
        excerpt_note = "bounded_excerpt"

    return {
        "resource_chunk_id": chunk_id,
        "resource_version_id": version_id,
        "official_resource_id": resource_id,
        "certification": certification,
        "certification_code": CERTIFICATION_CODES[certification],
        "domain": primary_domain,
        "domain_tags": domain_tags,
        "resource_title": _require_non_empty(resource["title"], field_name="title"),
        "resource_type": _require_non_empty(resource["resource_type"], field_name="resource_type"),
        "publisher": _require_non_empty(resource["publisher"], field_name="publisher"),
        "canonical_url": _require_https_url(resource["canonical_url"], field_name="canonical_url"),
        "source_external_version": version.get("source_external_version"),
        "effective_at": version.get("effective_at"),
        "retrieved_at": version.get("retrieved_at"),
        "source_url": version.get("source_url"),
        "version_number": version.get("version_number"),
        "chunk_index": chunk["chunk_index"],
        "chunk_text_excerpt": excerpt,
        "content_hash": _require_non_empty(chunk["content_hash"], field_name="content_hash"),
        "excerpt_mode": excerpt_note,
        "exported_at": exported_at,
        "provenance_status": "verified_official_resource_library",
    }


def _load_secrets_toml(path: Path) -> Dict[str, str]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py311+
        import tomli as tomllib  # type: ignore

    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {str(key): str(value).strip() for key, value in data.items() if isinstance(value, str)}


def load_supabase_client(*, repo_root: Path | None = None):
    """Create a Supabase client from env vars or .streamlit/secrets.toml."""
    import os

    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        root = repo_root or Path(__file__).resolve().parents[1]
        secrets = _load_secrets_toml(root / ".streamlit" / "secrets.toml")
        url = url or secrets.get("SUPABASE_URL", "").strip()
        key = key or secrets.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise OfficialEvidenceSeedConfigError(
            "Missing Supabase configuration. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY or provide .streamlit/secrets.toml."
        )
    return create_client(url, key)


def _table(client, name: str):
    return client.table(name)


def _fetch_all_rows(client, table_name: str, select: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    page_size = 1000
    while True:
        result = _table(client, table_name).select(select).range(offset, offset + page_size - 1).execute()
        if getattr(result, "error", None):
            raise OfficialEvidenceSeedError(
                f"{table_name} lookup failed: {result.error}"
            )
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def _latest_versions_by_resource(versions: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for version in versions:
        resource_id = str(version.get("resource_id"))
        current = latest.get(resource_id)
        if current is None or int(version.get("version_number") or 0) > int(current.get("version_number") or 0):
            latest[resource_id] = dict(version)
    return latest


def _build_inventory(
    resources: Sequence[Mapping[str, Any]],
    versions: Sequence[Mapping[str, Any]],
    chunks: Sequence[Mapping[str, Any]],
    eligible_items: Sequence[Mapping[str, Any]],
) -> ResourceInventory:
    resource_count_by_cert: Dict[str, int] = defaultdict(int)
    active_resource_count_by_cert: Dict[str, int] = defaultdict(int)
    chunk_count_by_cert: Dict[str, int] = defaultdict(int)
    domains_by_cert: Dict[str, Set[str]] = defaultdict(set)

    missing_canonical_url = 0
    missing_publisher = 0
    missing_content_hash = 0
    provenance_complete = 0

    latest = _latest_versions_by_resource(versions)
    chunks_by_version: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_version[str(chunk.get("resource_version_id"))].append(chunk)

    for resource in resources:
        cert = str(resource.get("certification_exam_name") or "")
        resource_count_by_cert[cert] += 1
        if resource.get("is_active"):
            active_resource_count_by_cert[cert] += 1

        has_url = bool(str(resource.get("canonical_url") or "").strip())
        has_publisher = bool(str(resource.get("publisher") or "").strip())
        if not has_url:
            missing_canonical_url += 1
        if not has_publisher:
            missing_publisher += 1

        latest_version = latest.get(str(resource.get("id")))
        if latest_version and has_url and has_publisher and str(latest_version.get("content_hash") or "").strip():
            provenance_complete += 1

        for tag in extract_domain_tags(resource.get("metadata")):
            domains_by_cert[cert].add(tag)

        if latest_version:
            for chunk in chunks_by_version.get(str(latest_version.get("id")), []):
                chunk_count_by_cert[cert] += 1
                if not str(chunk.get("content_hash") or "").strip():
                    missing_content_hash += 1
                for tag in extract_domain_tags(chunk.get("metadata")):
                    domains_by_cert[cert].add(tag)

    for item in eligible_items:
        domains_by_cert[str(item["certification"])].add(str(item["domain"]))

    return ResourceInventory(
        resource_count_by_certification=dict(sorted(resource_count_by_cert.items())),
        active_resource_count_by_certification=dict(sorted(active_resource_count_by_cert.items())),
        resource_version_count=len(versions),
        chunk_count_by_certification=dict(sorted(chunk_count_by_cert.items())),
        domains_by_certification={
            cert: sorted(domains) for cert, domains in sorted(domains_by_cert.items())
        },
        eligible_chunk_count=len(eligible_items),
        missing_canonical_url_count=missing_canonical_url,
        missing_publisher_count=missing_publisher,
        missing_content_hash_count=missing_content_hash,
        provenance_complete_resource_count=provenance_complete,
        total_resource_count=len(resources),
        total_chunk_count=len(chunks),
    )


def collect_eligible_export_items(
    client,
    *,
    exported_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load and validate all eligible official evidence rows (read-only)."""
    timestamp = exported_at or _utc_now_iso()
    resources = _fetch_all_rows(
        client,
        "official_resources",
        "id,certification_exam_name,title,publisher,canonical_url,resource_type,is_active,metadata",
    )
    versions = _fetch_all_rows(
        client,
        "resource_versions",
        "id,resource_id,version_number,content_hash,effective_at,source_external_version,retrieved_at,source_url",
    )
    chunks = _fetch_all_rows(
        client,
        "resource_chunks",
        "id,resource_version_id,chunk_index,chunk_text,content_hash,metadata",
    )

    resources_by_id = {str(row["id"]): row for row in resources}
    latest = _latest_versions_by_resource(versions)
    versions_by_id = {str(row["id"]): row for row in versions}

    eligible: List[Dict[str, Any]] = []
    for chunk in chunks:
        version = versions_by_id.get(str(chunk.get("resource_version_id")))
        if version is None:
            continue
        resource = resources_by_id.get(str(version.get("resource_id")))
        if resource is None:
            continue
        if latest.get(str(resource["id"]), {}).get("id") != version.get("id"):
            continue
        try:
            item = build_export_item(
                resource=resource,
                version=version,
                chunk=chunk,
                exported_at=timestamp,
            )
        except OfficialEvidenceSeedError:
            continue
        eligible.append(item)

    eligible.sort(key=lambda item: (
        item["certification"],
        item["domain"],
        item["resource_title"],
        item["chunk_index"],
        item["resource_chunk_id"],
    ))
    return _dedupe_items(eligible)


def _dedupe_items(items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for item in items:
        chunk_id = str(item["resource_chunk_id"]).lower()
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        deduped.append(dict(item))
    deduped.sort(key=lambda item: (
        item["certification"],
        item["domain"],
        item["resource_title"],
        item["chunk_index"],
        item["resource_chunk_id"],
    ))
    return deduped


def select_export_items(
    eligible_items: Sequence[Mapping[str, Any]],
    *,
    target_min: int = DEFAULT_TARGET_MIN_CHUNKS,
    target_max: int = DEFAULT_TARGET_MAX_CHUNKS,
    max_per_resource: int = DEFAULT_MAX_CHUNKS_PER_RESOURCE,
) -> List[Dict[str, Any]]:
    """Deterministically select a diverse cross-domain evidence set."""
    if len(eligible_items) < target_min:
        raise OfficialEvidenceSeedError(
            f"eligible chunk count {len(eligible_items)} is below target minimum {target_min}"
        )

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for item in eligible_items:
        key = (str(item["certification"]), str(item["domain"]))
        grouped[key].append(dict(item))

    for key in grouped:
        grouped[key].sort(key=lambda item: (
            item["resource_title"],
            item["chunk_index"],
            item["resource_chunk_id"],
        ))

    group_keys = sorted(grouped.keys())
    selected: List[Dict[str, Any]] = []
    per_resource: Dict[str, int] = defaultdict(int)
    seen_chunk_ids: Set[str] = set()

    while len(selected) < target_max:
        progressed = False
        for key in group_keys:
            bucket = grouped[key]
            while bucket:
                candidate = bucket.pop(0)
                chunk_id = str(candidate["resource_chunk_id"]).lower()
                resource_id = str(candidate["official_resource_id"]).lower()
                if chunk_id in seen_chunk_ids:
                    continue
                if per_resource[resource_id] >= max_per_resource:
                    continue
                selected.append(candidate)
                seen_chunk_ids.add(chunk_id)
                per_resource[resource_id] += 1
                progressed = True
                break
            if len(selected) >= target_max:
                break
        if not progressed:
            break

    selected.sort(key=lambda item: (
        item["certification"],
        item["domain"],
        item["resource_title"],
        item["chunk_index"],
        item["resource_chunk_id"],
    ))

    if len(selected) < target_min:
        raise OfficialEvidenceSeedError(
            f"selected chunk count {len(selected)} is below target minimum {target_min}"
        )
    return selected


def build_fixture_payload(
    selected_items: Sequence[Mapping[str, Any]],
    *,
    inventory: ResourceInventory,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    timestamp = generated_at or _utc_now_iso()
    certifications = sorted({str(item["certification"]) for item in selected_items})
    domains = sorted({str(item["domain"]) for item in selected_items})
    sources = sorted({str(item["resource_title"]) for item in selected_items})
    max_excerpt = max(len(str(item["chunk_text_excerpt"])) for item in selected_items)

    return {
        "fixture_version": FIXTURE_VERSION,
        "status": "verified_evidence_seed",
        "intended_use": "quality_benchmark_v1",
        "generated_at": timestamp,
        "no_synthetic_evidence": True,
        "source_verification_method": (
            "Read-only SELECT from official_resources, resource_versions, and "
            "resource_chunks with active-resource latest-version filtering, "
            "https canonical_url, official publisher, and content_hash validation."
        ),
        "certifications_covered": certifications,
        "domains_covered": domains,
        "sources_represented": sources,
        "inventory_summary": {
            "total_resource_count": inventory.total_resource_count,
            "total_chunk_count": inventory.total_chunk_count,
            "eligible_chunk_count": inventory.eligible_chunk_count,
            "resource_count_by_certification": inventory.resource_count_by_certification,
            "chunk_count_by_certification": inventory.chunk_count_by_certification,
            "domains_by_certification": inventory.domains_by_certification,
            "provenance_complete_resource_count": inventory.provenance_complete_resource_count,
        },
        "export_summary": {
            "exported_chunk_count": len(selected_items),
            "exported_by_certification": {
                cert: sum(1 for item in selected_items if item["certification"] == cert)
                for cert in certifications
            },
            "exported_by_domain": {
                domain: sum(1 for item in selected_items if item["domain"] == domain)
                for domain in domains
            },
            "maximum_excerpt_chars": max_excerpt,
        },
        "evidence_items": list(selected_items),
    }


def inventory_report_dict(inventory: ResourceInventory) -> Dict[str, Any]:
    return {
        "resource_count_by_certification": inventory.resource_count_by_certification,
        "active_resource_count_by_certification": inventory.active_resource_count_by_certification,
        "resource_version_count": inventory.resource_version_count,
        "chunk_count_by_certification": inventory.chunk_count_by_certification,
        "domains_by_certification": inventory.domains_by_certification,
        "eligible_chunk_count": inventory.eligible_chunk_count,
        "missing_canonical_url_count": inventory.missing_canonical_url_count,
        "missing_publisher_count": inventory.missing_publisher_count,
        "missing_content_hash_count": inventory.missing_content_hash_count,
        "provenance_complete_resource_count": inventory.provenance_complete_resource_count,
        "total_resource_count": inventory.total_resource_count,
        "total_chunk_count": inventory.total_chunk_count,
    }


def build_inventory(client) -> ResourceInventory:
    resources = _fetch_all_rows(
        client,
        "official_resources",
        "id,certification_exam_name,title,publisher,canonical_url,resource_type,is_active,metadata",
    )
    versions = _fetch_all_rows(
        client,
        "resource_versions",
        "id,resource_id,version_number,content_hash,effective_at,source_external_version,retrieved_at,source_url",
    )
    chunks = _fetch_all_rows(
        client,
        "resource_chunks",
        "id,resource_version_id,chunk_index,chunk_text,content_hash,metadata",
    )
    eligible = collect_eligible_export_items(client)
    return _build_inventory(resources, versions, chunks, eligible)


def export_official_evidence_seed(
    client,
    *,
    target_min: int = DEFAULT_TARGET_MIN_CHUNKS,
    target_max: int = DEFAULT_TARGET_MAX_CHUNKS,
    generated_at: Optional[str] = None,
) -> ExportSelection:
    timestamp = generated_at or _utc_now_iso()
    eligible = collect_eligible_export_items(client, exported_at=timestamp)
    resources = _fetch_all_rows(
        client,
        "official_resources",
        "id,certification_exam_name,title,publisher,canonical_url,resource_type,is_active,metadata",
    )
    versions = _fetch_all_rows(
        client,
        "resource_versions",
        "id,resource_id,version_number,content_hash,effective_at,source_external_version,retrieved_at,source_url",
    )
    chunks = _fetch_all_rows(
        client,
        "resource_chunks",
        "id,resource_version_id,chunk_index,chunk_text,content_hash,metadata",
    )
    inventory = _build_inventory(resources, versions, chunks, eligible)
    selected = select_export_items(
        eligible,
        target_min=target_min,
        target_max=target_max,
    )
    return ExportSelection(items=tuple(selected), inventory=inventory)


def assert_output_safe_for_write(text: str) -> None:
    lowered = text.lower()
    for marker in CREDENTIAL_SUBSTRINGS:
        if marker.lower() in lowered:
            raise OfficialEvidenceSeedOutputError(
                f"refusing to write output containing sensitive marker {marker!r}"
            )


def write_fixture_file(
    path: Path | str,
    payload: Mapping[str, Any],
    *,
    allow_overwrite: bool = False,
) -> None:
    output_path = Path(path)
    if output_path.exists() and not allow_overwrite:
        raise OfficialEvidenceSeedOutputError(
            f"refusing to overwrite existing fixture: {output_path}"
        )
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    assert_output_safe_for_write(serialized)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized + "\n", encoding="utf-8")


def validate_fixture_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("no_synthetic_evidence") is not True:
        raise OfficialEvidenceSeedError("fixture must set no_synthetic_evidence=true")
    items = payload.get("evidence_items")
    if not isinstance(items, list) or not items:
        raise OfficialEvidenceSeedError("fixture evidence_items must be a non-empty array")

    seen_chunk_ids: Set[str] = set()
    certs: Set[str] = set()
    domains: Set[str] = set()
    max_len = 0

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise OfficialEvidenceSeedError(f"evidence_items[{index}] must be an object")
        for field in (
            "resource_chunk_id",
            "resource_version_id",
            "official_resource_id",
            "certification",
            "domain",
            "resource_title",
            "publisher",
            "canonical_url",
            "chunk_index",
            "chunk_text_excerpt",
            "content_hash",
            "exported_at",
            "provenance_status",
        ):
            if field not in item:
                raise OfficialEvidenceSeedError(
                    f"evidence_items[{index}] missing required field {field!r}"
                )
        chunk_id = _normalize_uuid(item["resource_chunk_id"], field_name="resource_chunk_id")
        if _is_placeholder_chunk_id(chunk_id):
            raise OfficialEvidenceSeedError(
                f"evidence_items[{index}] uses synthetic placeholder chunk id"
            )
        if chunk_id.lower() in seen_chunk_ids:
            raise OfficialEvidenceSeedError(f"duplicate resource_chunk_id {chunk_id!r}")
        seen_chunk_ids.add(chunk_id.lower())

        excerpt = bound_excerpt(str(item["chunk_text_excerpt"]))
        if excerpt != item["chunk_text_excerpt"]:
            raise OfficialEvidenceSeedError(
                f"evidence_items[{index}] chunk_text_excerpt is not a valid bounded excerpt"
            )
        max_len = max(max_len, len(excerpt))
        if item["provenance_status"] != "verified_official_resource_library":
            raise OfficialEvidenceSeedError(
                f"evidence_items[{index}] has invalid provenance_status"
            )
        certs.add(str(item["certification"]))
        domains.add(str(item["domain"]))

    if not certs.intersection(SUPPORTED_CERTIFICATIONS):
        raise OfficialEvidenceSeedError("fixture must include supported certifications")
    if len(domains) < 2:
        raise OfficialEvidenceSeedError("fixture must cover multiple domains")
