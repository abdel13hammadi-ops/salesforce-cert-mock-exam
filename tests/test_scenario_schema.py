"""Tests for scenario content schema loading and custom validation."""

from __future__ import annotations

import copy
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_schema import (
    DEFAULT_SCHEMA_VERSION,
    TERMINAL_SENTINEL,
    ScenarioContentError,
    ScenarioValidationError,
    build_scenario_content,
    compute_canonical_content_sha256,
    compute_graph_metadata,
    load_json_document,
    load_schema,
    load_scenario_content,
    schema_path_for_version,
    scenario_content_root,
    validate_json_schema,
    validate_scenario_document,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BA_SCENARIO_PATH = (
    REPO_ROOT
    / "scenario_content"
    / "business_analyst"
    / "ba201-sim-meridian-health-01"
    / "1.0.0"
    / "scenario.json"
)
EXPECTED_CANONICAL_SHA256 = "f29c39b64c4f0786a54c7ab4bf6b2f07f5a8e683eb2d19d834a1440d347f4f36"


class TestScenarioSchemaFoundation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(BA_SCENARIO_PATH.read_text(encoding="utf-8"))

    def test_scenario_content_root_and_schema_path(self) -> None:
        root = scenario_content_root()
        self.assertEqual(root, (REPO_ROOT / "scenario_content").resolve())
        schema_path = schema_path_for_version(DEFAULT_SCHEMA_VERSION)
        self.assertTrue(schema_path.is_file())
        self.assertEqual(schema_path.name, "simulation.schema.json")

    def test_load_schema_default_version(self) -> None:
        schema = load_schema(DEFAULT_SCHEMA_VERSION)
        self.assertIn("certificationExamName", schema["required"])
        self.assertIn("examCode", schema["required"])
        self.assertFalse(schema.get("additionalProperties", True))

    def test_load_ba201_scenario_content(self) -> None:
        content = load_scenario_content(BA_SCENARIO_PATH)
        self.assertEqual(content.simulation_id, "ba201-sim-meridian-health-01")
        self.assertEqual(content.version, "1.0.0")
        self.assertEqual(content.schema_version, "1.0.0")
        self.assertEqual(content.certification_exam_name, "Salesforce Certified Business Analyst")
        self.assertEqual(content.exam_code, "BA-201")
        self.assertEqual(content.start_scene, "s01_kickoff")
        self.assertEqual(content.source_path, BA_SCENARIO_PATH.resolve())
        self.assertEqual(len(content.scenes), 28)
        self.assertEqual(len(content.endings), 4)
        self.assertEqual(len(content.domains), 6)
        self.assertEqual(len(content.state_variables), 3)

    def test_ba201_canonical_content_sha256(self) -> None:
        content = load_scenario_content(BA_SCENARIO_PATH)
        self.assertEqual(content.canonical_content_sha256, EXPECTED_CANONICAL_SHA256)
        self.assertEqual(
            compute_canonical_content_sha256(self.document),
            EXPECTED_CANONICAL_SHA256,
        )

    def test_ba201_graph_metadata(self) -> None:
        content = load_scenario_content(BA_SCENARIO_PATH)
        metadata = content.graph_metadata
        self.assertEqual(metadata.authored_scene_count, 28)
        self.assertEqual(metadata.reachable_scene_count, 28)
        self.assertEqual(metadata.unreachable_scene_ids, ())
        self.assertEqual(metadata.minimum_path_length, 24)
        self.assertEqual(metadata.maximum_path_length, 25)

    def test_compute_graph_metadata_matches_loaded_content(self) -> None:
        metadata = compute_graph_metadata(self.document)
        content = load_scenario_content(BA_SCENARIO_PATH)
        self.assertEqual(metadata, content.graph_metadata)

    def test_ba201_structure_counts(self) -> None:
        content = load_scenario_content(BA_SCENARIO_PATH)
        counts = content.structure_counts
        self.assertEqual(counts.choice_count, 57)
        self.assertEqual(counts.detour_count, 4)
        self.assertEqual(counts.domain_count, 6)
        self.assertEqual(counts.ending_count, 4)

    def test_terminal_sentinel_only_on_ba201_terminal_options(self) -> None:
        content = load_scenario_content(BA_SCENARIO_PATH)
        terminal_targets = {
            option.next_scene
            for scene in content.scenes
            for option in scene.decision.options
            if option.next_scene == TERMINAL_SENTINEL
        }
        self.assertEqual(terminal_targets, {TERMINAL_SENTINEL})

    def test_rejects_legacy_top_level_certification_code(self) -> None:
        document = copy.deepcopy(self.document)
        document["certificationCode"] = "Salesforce Certified Business Analyst"
        with self.assertRaises(ScenarioValidationError):
            validate_scenario_document(document)

    def test_rejects_legacy_ending_sentinel(self) -> None:
        document = copy.deepcopy(self.document)
        terminal_scene = next(scene for scene in document["scenes"] if scene["id"] == "s24_golive_readiness")
        terminal_scene["decision"]["options"][0]["nextScene"] = "ENDING"
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("ENDING", str(ctx.exception))

    def test_rejects_unknown_state_variable(self) -> None:
        document = copy.deepcopy(self.document)
        first_scene = document["scenes"][0]
        first_scene["decision"]["options"][0]["stateChanges"]["moraleFactor"] = -5
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("moraleFactor", str(ctx.exception))

    def test_rejects_dangling_scene_reference(self) -> None:
        document = copy.deepcopy(self.document)
        first_scene = document["scenes"][0]
        first_scene["decision"]["options"][0]["nextScene"] = "s99_missing"
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("s99_missing", str(ctx.exception))

    def test_rejects_cycle(self) -> None:
        document = copy.deepcopy(self.document)
        terminal_scene = next(scene for scene in document["scenes"] if scene["id"] == "s24_golive_readiness")
        terminal_scene["decision"]["options"][0]["nextScene"] = "s01_kickoff"
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_rejects_unreachable_scene(self) -> None:
        document = copy.deepcopy(self.document)
        document["scenes"].append(
            {
                "id": "s99_orphan",
                "domainId": "d1",
                "narrative": "Unreachable scene.",
                "decision": {
                    "prompt": "Nowhere to go.",
                    "options": [
                        {
                            "id": "A",
                            "text": "Terminal",
                            "isCorrect": True,
                            "feedback": "Done.",
                            "nextScene": TERMINAL_SENTINEL,
                        },
                        {
                            "id": "B",
                            "text": "Also terminal",
                            "isCorrect": False,
                            "feedback": "Done.",
                            "nextScene": TERMINAL_SENTINEL,
                        },
                    ],
                },
            }
        )
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("s99_orphan", str(ctx.exception))

    def test_rejects_duplicate_scene_ids(self) -> None:
        document = copy.deepcopy(self.document)
        duplicate = copy.deepcopy(document["scenes"][0])
        document["scenes"].append(duplicate)
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("duplicate scene id", str(ctx.exception))

    def test_rejects_duplicate_option_ids_within_scene(self) -> None:
        document = copy.deepcopy(self.document)
        first_scene = document["scenes"][0]
        first_scene["decision"]["options"][1]["id"] = first_scene["decision"]["options"][0]["id"]
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("duplicate option id", str(ctx.exception))

    def test_rejects_invalid_domain_reference(self) -> None:
        document = copy.deepcopy(self.document)
        document["scenes"][0]["domainId"] = "d99_missing"
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("d99_missing", str(ctx.exception))

    def test_rejects_invalid_ending_condition_variable(self) -> None:
        document = copy.deepcopy(self.document)
        document["endings"][0]["condition"]["unknownMetricMin"] = 10
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("unknownMetric", str(ctx.exception))

    def test_rejects_undeclared_initial_state_key(self) -> None:
        document = copy.deepcopy(self.document)
        document["initialState"]["moraleFactor"] = 50
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("moraleFactor", str(ctx.exception))

    def test_validate_json_schema_includes_path_on_error(self) -> None:
        document = copy.deepcopy(self.document)
        document.pop("simulationId")
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_json_schema(document, schema_version=DEFAULT_SCHEMA_VERSION)
        self.assertTrue(ctx.exception.path)

    def test_malformed_json_raises_scenario_content_error(self) -> None:
        with patch.object(Path, "read_text", return_value="{not-json"):
            with self.assertRaises(ScenarioContentError) as ctx:
                load_json_document(Path("ignored.json"))
        self.assertIn("Malformed JSON", str(ctx.exception))

    def test_build_scenario_content_returns_immutable_models(self) -> None:
        content = build_scenario_content(self.document, source_path=BA_SCENARIO_PATH)
        with self.assertRaises(Exception):
            content.simulation_id = "mutated"  # type: ignore[misc]

    def test_ba201_validation_completes_quickly(self) -> None:
        start = time.perf_counter()
        for _ in range(20):
            validate_scenario_document(self.document)
            build_scenario_content(self.document, source_path=BA_SCENARIO_PATH)
        elapsed = time.perf_counter() - start
        self.assertLess(
            elapsed,
            5.0,
            msg=f"20 validation/build cycles took {elapsed:.2f}s; expected sub-second per cycle",
        )


if __name__ == "__main__":
    unittest.main()
