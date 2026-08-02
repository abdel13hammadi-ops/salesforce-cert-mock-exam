"""Tests for local scenario catalog discovery and version resolution."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_catalog import (
    ScenarioCatalogError,
    discover_certification_catalog_paths,
    find_scenario_catalog_entry,
    list_scenarios_grouped_by_certification,
    load_all_certification_catalogs,
    load_certification_catalog,
    load_resolved_scenario_content,
    parse_certification_catalog,
    resolve_default_scenario_version_path,
    resolve_scenario_version_path,
)
from utils.scenario_schema import REPO_ROOT, ScenarioValidationError

BA_CERTIFICATION_EXAM_NAME = "Salesforce Certified Business Analyst"
BA_SIMULATION_ID = "ba201-sim-meridian-health-01"
BA_VERSION = "1.0.0"
EXPECTED_CANONICAL_SHA256 = "f29c39b64c4f0786a54c7ab4bf6b2f07f5a8e683eb2d19d834a1440d347f4f36"
BA_SCENARIO_RELATIVE = Path("ba201-sim-meridian-health-01") / "1.0.0" / "scenario.json"


class TestScenarioCatalogFoundation(unittest.TestCase):
    def test_discover_certification_catalog_paths(self) -> None:
        paths = discover_certification_catalog_paths()
        self.assertGreaterEqual(len(paths), 1)
        self.assertTrue(any(path.name == "catalog.json" for path in paths))
        self.assertTrue(
            any("business_analyst" in str(path) for path in paths),
            msg="Expected business_analyst catalog to be discoverable",
        )

    def test_load_business_analyst_catalog(self) -> None:
        catalog = load_certification_catalog("business_analyst")
        self.assertEqual(catalog.certification_slug, "business_analyst")
        self.assertEqual(catalog.certification_exam_name, BA_CERTIFICATION_EXAM_NAME)
        simulation_ids = {entry.simulation_id for entry in catalog.scenarios}
        self.assertIn(BA_SIMULATION_ID, simulation_ids)
        self.assertIn("cb-sc-001-onboarding-handoff-vslice", simulation_ids)
        ba_entry = next(entry for entry in catalog.scenarios if entry.simulation_id == BA_SIMULATION_ID)
        self.assertEqual(ba_entry.exam_code, "BA-201")
        self.assertNotEqual(ba_entry.exam_code, catalog.certification_exam_name)

    def test_parse_certification_catalog(self) -> None:
        catalog_path = REPO_ROOT / "scenario_content" / "business_analyst" / "catalog.json"
        document = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog = parse_certification_catalog(document, catalog_path=catalog_path)
        self.assertEqual(catalog.certification_exam_name, BA_CERTIFICATION_EXAM_NAME)
        self.assertEqual(catalog.scenarios[0].versions[0].canonical_content_sha256, EXPECTED_CANONICAL_SHA256)

    def test_list_scenarios_grouped_by_certification(self) -> None:
        grouped = list_scenarios_grouped_by_certification()
        self.assertIn(BA_CERTIFICATION_EXAM_NAME, grouped)
        simulation_ids = {entry.simulation_id for entry in grouped[BA_CERTIFICATION_EXAM_NAME]}
        self.assertIn(BA_SIMULATION_ID, simulation_ids)

    def test_resolve_scenario_version_path_without_hardcoded_module_lookup(self) -> None:
        path = resolve_scenario_version_path(
            certification_exam_name=BA_CERTIFICATION_EXAM_NAME,
            simulation_id=BA_SIMULATION_ID,
            version=BA_VERSION,
        )
        expected = (
            REPO_ROOT
            / "scenario_content"
            / "business_analyst"
            / BA_SCENARIO_RELATIVE
        ).resolve()
        self.assertEqual(path, expected)

    def test_resolve_default_scenario_version_path(self) -> None:
        path = resolve_default_scenario_version_path(
            certification_exam_name=BA_CERTIFICATION_EXAM_NAME,
            simulation_id=BA_SIMULATION_ID,
        )
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "scenario.json")

    def test_load_resolved_scenario_content(self) -> None:
        content = load_resolved_scenario_content(
            certification_exam_name=BA_CERTIFICATION_EXAM_NAME,
            simulation_id=BA_SIMULATION_ID,
            version=BA_VERSION,
            expected_canonical_content_sha256=EXPECTED_CANONICAL_SHA256,
        )
        self.assertEqual(content.simulation_id, BA_SIMULATION_ID)
        self.assertEqual(content.version, BA_VERSION)
        self.assertEqual(content.canonical_content_sha256, EXPECTED_CANONICAL_SHA256)
        self.assertEqual(content.graph_metadata.minimum_path_length, 24)
        self.assertEqual(content.graph_metadata.maximum_path_length, 25)

    def test_find_scenario_catalog_entry(self) -> None:
        catalog, scenario = find_scenario_catalog_entry(
            certification_exam_name=BA_CERTIFICATION_EXAM_NAME,
            simulation_id=BA_SIMULATION_ID,
        )
        self.assertEqual(catalog.certification_slug, "business_analyst")
        self.assertEqual(scenario.simulation_id, BA_SIMULATION_ID)
        self.assertEqual(scenario.versions[0].version, BA_VERSION)

    def test_missing_scenario_raises_catalog_error(self) -> None:
        with self.assertRaises(ScenarioCatalogError):
            resolve_scenario_version_path(
                certification_exam_name=BA_CERTIFICATION_EXAM_NAME,
                simulation_id="missing-simulation",
                version=BA_VERSION,
            )

    def test_missing_version_raises_catalog_error(self) -> None:
        with self.assertRaises(ScenarioCatalogError):
            resolve_scenario_version_path(
                certification_exam_name=BA_CERTIFICATION_EXAM_NAME,
                simulation_id=BA_SIMULATION_ID,
                version="9.9.9",
            )

    def test_missing_certification_slug_raises_catalog_error(self) -> None:
        with self.assertRaises(ScenarioCatalogError):
            load_certification_catalog("missing_certification_slug")

    def test_canonical_hash_mismatch_raises_validation_error(self) -> None:
        with self.assertRaises(ScenarioValidationError) as ctx:
            load_resolved_scenario_content(
                certification_exam_name=BA_CERTIFICATION_EXAM_NAME,
                simulation_id=BA_SIMULATION_ID,
                version=BA_VERSION,
                expected_canonical_content_sha256="0" * 64,
            )
        self.assertIn("canonical content SHA-256 mismatch", str(ctx.exception))

    def test_load_all_certification_catalogs(self) -> None:
        catalogs = load_all_certification_catalogs()
        exam_names = {catalog.certification_exam_name for catalog in catalogs}
        self.assertIn(BA_CERTIFICATION_EXAM_NAME, exam_names)

    def test_malformed_catalog_json_raises_catalog_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cert_dir = temp_root / "business_analyst"
            cert_dir.mkdir()
            (cert_dir / "catalog.json").write_text("{not-json", encoding="utf-8")
            with self.assertRaises(ScenarioCatalogError) as ctx:
                load_certification_catalog("business_analyst", content_root=temp_root)
            self.assertIn("Malformed JSON", str(ctx.exception))

    def test_missing_catalog_relative_path_target_raises_catalog_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cert_dir = temp_root / "business_analyst"
            cert_dir.mkdir()
            catalog_document = {
                "catalogVersion": "1.0.0",
                "certificationSlug": "business_analyst",
                "certificationExamName": BA_CERTIFICATION_EXAM_NAME,
                "scenarios": [
                    {
                        "simulationId": "demo-sim",
                        "title": "Demo",
                        "examCode": "DEMO-001",
                        "versions": [
                            {
                                "version": "1.0.0",
                                "schemaVersion": "1.0.0",
                                "relativePath": "missing/scenario.json",
                            }
                        ],
                    }
                ],
            }
            (cert_dir / "catalog.json").write_text(json.dumps(catalog_document), encoding="utf-8")
            with self.assertRaises(ScenarioCatalogError) as ctx:
                resolve_scenario_version_path(
                    certification_exam_name=BA_CERTIFICATION_EXAM_NAME,
                    simulation_id="demo-sim",
                    version="1.0.0",
                    content_root=temp_root,
                )
            self.assertIn("Scenario content file not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
