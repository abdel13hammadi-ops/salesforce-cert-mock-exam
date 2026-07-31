"""Lightweight local catalog discovery and immutable scenario-version resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from utils.scenario_schema import (
    ScenarioContent,
    ScenarioContentError,
    ScenarioValidationError,
    collect_scenario_validation_findings,
    load_json_document,
    load_scenario_content,
    scenario_content_root,
)
from utils.scenario_validation_findings import ValidationFinding, findings_contain_blocking, first_blocking_finding

CATALOG_FILENAME = "catalog.json"


class ScenarioCatalogError(ScenarioContentError):
    """Raised when a scenario catalog cannot be loaded or resolved."""


@dataclass(frozen=True)
class ScenarioVersionCatalogEntry:
    version: str
    schema_version: str
    relative_path: str
    canonical_content_sha256: str | None = None
    estimated_minutes: int | None = None
    is_default: bool = False


@dataclass(frozen=True)
class ScenarioCatalogEntry:
    simulation_id: str
    title: str
    exam_code: str
    versions: tuple[ScenarioVersionCatalogEntry, ...]


@dataclass(frozen=True)
class CertificationScenarioCatalog:
    catalog_version: str
    certification_slug: str
    certification_exam_name: str
    scenarios: tuple[ScenarioCatalogEntry, ...]
    catalog_path: Path


def _load_catalog_document(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioCatalogError(f"Unable to read catalog at {path}") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScenarioCatalogError(f"Malformed JSON in {path}: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ScenarioCatalogError(f"Catalog root must be a JSON object: {path}")
    return parsed


def _parse_version_entry(raw: Mapping[str, Any]) -> ScenarioVersionCatalogEntry:
    estimated_minutes = raw.get("estimatedMinutes")
    return ScenarioVersionCatalogEntry(
        version=str(raw.get("version") or "").strip(),
        schema_version=str(raw.get("schemaVersion") or "").strip(),
        relative_path=str(raw.get("relativePath") or "").strip(),
        canonical_content_sha256=str(raw.get("canonicalContentSha256") or "").strip() or None,
        estimated_minutes=int(estimated_minutes) if isinstance(estimated_minutes, int) else None,
        is_default=bool(raw.get("isDefault")),
    )


def _parse_scenario_entry(raw: Mapping[str, Any]) -> ScenarioCatalogEntry:
    versions_raw = raw.get("versions") or []
    versions = tuple(_parse_version_entry(entry) for entry in versions_raw if isinstance(entry, Mapping))
    return ScenarioCatalogEntry(
        simulation_id=str(raw.get("simulationId") or "").strip(),
        title=str(raw.get("title") or "").strip(),
        exam_code=str(raw.get("examCode") or "").strip(),
        versions=versions,
    )


def parse_certification_catalog(
    document: Mapping[str, Any],
    *,
    catalog_path: Path,
    certification_slug: str | None = None,
) -> CertificationScenarioCatalog:
    slug = certification_slug or catalog_path.parent.name
    scenarios_raw = document.get("scenarios") or []
    scenarios = tuple(
        _parse_scenario_entry(entry) for entry in scenarios_raw if isinstance(entry, Mapping)
    )
    return CertificationScenarioCatalog(
        catalog_version=str(document.get("catalogVersion") or "1.0.0"),
        certification_slug=str(document.get("certificationSlug") or slug),
        certification_exam_name=str(document.get("certificationExamName") or "").strip(),
        scenarios=scenarios,
        catalog_path=catalog_path.resolve(),
    )


def discover_certification_catalog_paths(content_root: Path | None = None) -> tuple[Path, ...]:
    root = scenario_content_root(content_root)
    paths = sorted(root.glob(f"*/{CATALOG_FILENAME}"))
    return tuple(path.resolve() for path in paths if path.is_file())


def load_certification_catalog(
    certification_slug: str,
    *,
    content_root: Path | None = None,
) -> CertificationScenarioCatalog:
    root = scenario_content_root(content_root)
    catalog_path = (root / certification_slug / CATALOG_FILENAME).resolve()
    if not catalog_path.is_file():
        raise ScenarioCatalogError(
            f"No scenario catalog found for certification slug {certification_slug!r}: {catalog_path}"
        )
    document = _load_catalog_document(catalog_path)
    return parse_certification_catalog(document, catalog_path=catalog_path, certification_slug=certification_slug)


def load_all_certification_catalogs(
    *,
    content_root: Path | None = None,
) -> tuple[CertificationScenarioCatalog, ...]:
    catalogs: list[CertificationScenarioCatalog] = []
    for catalog_path in discover_certification_catalog_paths(content_root):
        document = _load_catalog_document(catalog_path)
        catalogs.append(
            parse_certification_catalog(
                document,
                catalog_path=catalog_path,
                certification_slug=catalog_path.parent.name,
            )
        )
    return tuple(catalogs)


def list_scenarios_grouped_by_certification(
    *,
    content_root: Path | None = None,
) -> dict[str, tuple[ScenarioCatalogEntry, ...]]:
    grouped: dict[str, tuple[ScenarioCatalogEntry, ...]] = {}
    for catalog in load_all_certification_catalogs(content_root=content_root):
        grouped[catalog.certification_exam_name] = catalog.scenarios
    return grouped


def find_scenario_catalog_entry(
    *,
    certification_exam_name: str,
    simulation_id: str,
    content_root: Path | None = None,
) -> tuple[CertificationScenarioCatalog, ScenarioCatalogEntry]:
    normalized_name = certification_exam_name.strip()
    normalized_simulation_id = simulation_id.strip()
    for catalog in load_all_certification_catalogs(content_root=content_root):
        if catalog.certification_exam_name != normalized_name:
            continue
        for scenario in catalog.scenarios:
            if scenario.simulation_id == normalized_simulation_id:
                return catalog, scenario
    raise ScenarioCatalogError(
        "Scenario not found for "
        f"certificationExamName={normalized_name!r}, simulationId={normalized_simulation_id!r}"
    )


def resolve_scenario_version_path(
    *,
    certification_exam_name: str,
    simulation_id: str,
    version: str,
    content_root: Path | None = None,
) -> Path:
    catalog, scenario = find_scenario_catalog_entry(
        certification_exam_name=certification_exam_name,
        simulation_id=simulation_id,
        content_root=content_root,
    )
    normalized_version = version.strip()
    for entry in scenario.versions:
        if entry.version == normalized_version:
            if not entry.relative_path:
                raise ScenarioCatalogError(
                    f"Catalog entry for {simulation_id!r} version {normalized_version!r} "
                    "is missing relativePath"
                )
            resolved = (catalog.catalog_path.parent / entry.relative_path).resolve()
            if not resolved.is_file():
                raise ScenarioCatalogError(
                    f"Scenario content file not found for {simulation_id!r} "
                    f"version {normalized_version!r}: {resolved}"
                )
            return resolved
    available = ", ".join(entry.version for entry in scenario.versions)
    raise ScenarioCatalogError(
        f"Scenario version {normalized_version!r} not found for simulationId={simulation_id!r}. "
        f"Available versions: {available or '(none)'}"
    )


def resolve_default_scenario_version_path(
    *,
    certification_exam_name: str,
    simulation_id: str,
    content_root: Path | None = None,
) -> Path:
    catalog, scenario = find_scenario_catalog_entry(
        certification_exam_name=certification_exam_name,
        simulation_id=simulation_id,
        content_root=content_root,
    )
    for entry in scenario.versions:
        if entry.is_default:
            return resolve_scenario_version_path(
                certification_exam_name=certification_exam_name,
                simulation_id=simulation_id,
                version=entry.version,
                content_root=content_root,
            )
    if len(scenario.versions) == 1:
        return resolve_scenario_version_path(
            certification_exam_name=certification_exam_name,
            simulation_id=simulation_id,
            version=scenario.versions[0].version,
            content_root=content_root,
        )
    raise ScenarioCatalogError(
        f"No default scenario version declared for simulationId={simulation_id!r} "
        f"under certificationExamName={certification_exam_name!r}"
    )


def validate_catalog_scenario_document(
    document: Mapping[str, Any],
    *,
    publication: bool = False,
) -> tuple[ValidationFinding, ...]:
    return collect_scenario_validation_findings(document, publication=publication)


def validate_catalog_scenario_file(
    path: Path,
    *,
    publication: bool = False,
) -> tuple[ValidationFinding, ...]:
    document = load_json_document(path)
    return validate_catalog_scenario_document(document, publication=publication)


def assert_catalog_scenario_valid(
    document: Mapping[str, Any],
    *,
    publication: bool = False,
) -> None:
    findings = validate_catalog_scenario_document(document, publication=publication)
    first = first_blocking_finding(findings)
    if first is not None:
        raise ScenarioValidationError(
            f"[{first.rule_id}] {first.message}",
            path=first.path,
        )


def load_resolved_scenario_content(
    *,
    certification_exam_name: str,
    simulation_id: str,
    version: str,
    content_root: Path | None = None,
    expected_canonical_content_sha256: str | None = None,
) -> ScenarioContent:
    path = resolve_scenario_version_path(
        certification_exam_name=certification_exam_name,
        simulation_id=simulation_id,
        version=version,
        content_root=content_root,
    )
    document = load_json_document(path)
    assert_catalog_scenario_valid(document, publication=False)
    content = load_scenario_content(path)
    if content.certification_exam_name != certification_exam_name.strip():
        raise ScenarioValidationError(
            "Loaded scenario certificationExamName does not match catalog lookup identity",
            path="certificationExamName",
        )
    if content.simulation_id != simulation_id.strip():
        raise ScenarioValidationError(
            "Loaded scenario simulationId does not match catalog entry",
            path="simulationId",
        )
    if content.version != version.strip():
        raise ScenarioValidationError(
            "Loaded scenario version does not match requested version",
            path="version",
        )
    if expected_canonical_content_sha256:
        expected = expected_canonical_content_sha256.strip().lower()
        actual = content.canonical_content_sha256.lower()
        if actual != expected:
            raise ScenarioValidationError(
                f"canonical content SHA-256 mismatch: expected {expected}, got {actual}",
                path="canonical_content_sha256",
            )
    return content
