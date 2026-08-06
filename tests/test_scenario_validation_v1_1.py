"""Comprehensive tests for Scenario Simulator schema 1.1.0 layered validation."""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_catalog import assert_catalog_scenario_valid, validate_catalog_scenario_document
from utils.scenario_schema import (
    DEFAULT_SCHEMA_VERSION,
    SCHEMA_VERSION_1_1,
    TERMINAL_SENTINEL,
    ScenarioContentError,
    ScenarioValidationError,
    build_scenario_content,
    collect_scenario_validation_findings,
    first_blocking_finding,
    load_scenario_content,
    validate_scenario_document,
    validate_scenario_for_publication,
)
from utils.scenario_validation_findings import findings_contain_blocking
from utils.scenario_validation_v1_1 import (
    _MAX_REACHABILITY_STATES,
    compute_canonical_content_sha256_v1_1,
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
VSLICE_SPEC_PATH = REPO_ROOT / "docs" / "scenario_simulator" / "SCENARIO_SCHEMA_1_1_0_SPEC.md"


def findings_with_rule(findings, rule_id: str):
    return [finding for finding in findings if finding.rule_id == rule_id]


def mutate(doc: dict, path_list: list, value):
    doc = copy.deepcopy(doc)
    target = doc
    for key in path_list[:-1]:
        target = target[key]
    target[path_list[-1]] = value
    return doc


def _minimal_classifier(*, outcome_id: str = "only_outcome") -> dict:
    return {
        "evaluationOrder": "v1_seven_step",
        "tierPoints": {"optimal": 4, "acceptable": 3, "suboptimal": 1, "high-risk": 0},
        "positiveHealthFormula": {
            "type": "weighted_dimension_health",
            "dimensions": [{"variableId": "score", "polarity": "higher_is_better"}],
        },
        "decisionQualityFormula": {"type": "tier_average", "divisor": "scoredDecisionCount"},
        "compositeFormula": {
            "type": "linear_blend",
            "terms": [
                {"metric": "positiveHealth", "weight": 0.5},
                {"metric": "decisionQuality", "weight": 0.5},
            ],
        },
        "severeCaps": [],
        "moderateCaps": [],
        "strongGuards": [],
        "scoreBands": [{"outcomeId": outcome_id, "minInclusive": None, "maxExclusive": None}],
        "tieBreakRules": "v1_default",
    }


def _scene_option(
    option_id: str,
    *,
    next_scene: str = TERMINAL_SENTINEL,
    terminal: bool = True,
    tier: str = "acceptable",
    corrective_route: dict | None = None,
    set_flags: list[str] | None = None,
    clear_flags: list[str] | None = None,
    state_changes: dict | None = None,
) -> dict:
    routing: dict = {"terminal": terminal, "primaryNextSceneId": next_scene}
    if corrective_route is not None:
        routing["correctiveRoute"] = corrective_route
    option: dict = {
        "id": option_id,
        "text": f"Option {option_id}",
        "evaluationTier": tier,
        "feedback": "Feedback.",
        "routing": routing,
        "debriefSeed": {
            "strongestOptionId": option_id,
            "whyStronger": "Strongest.",
            "immediateConsequence": "Done.",
        },
    }
    if set_flags:
        option["setFlags"] = set_flags
    if clear_flags:
        option["clearFlags"] = clear_flags
    if state_changes:
        option["stateChanges"] = state_changes
    return option


def _core_scene(
    scene_id: str,
    *,
    options: list[dict],
    characters: list[str] | None = None,
    dialogue_exchanges: list[dict] | None = None,
    variants: list[dict] | None = None,
) -> dict:
    exchanges = dialogue_exchanges or [
        {"exchangeId": "ex-001", "speakerId": "char-a", "text": "Hello.", "tone": "neutral"}
    ]
    dialogue: dict = {"exchanges": exchanges}
    if variants:
        dialogue["variants"] = variants
    return {
        "id": scene_id,
        "sceneType": "core",
        "title": scene_id,
        "setting": "Room.",
        "charactersPresent": characters or ["char-a"],
        "learnerPresent": True,
        "dialogue": dialogue,
        "decision": {"prompt": "Choose.", "options": options},
    }


def _corrective_scene(
    scene_id: str,
    *,
    trigger_scene_id: str,
    reconvergence_scene_id: str,
    next_scene: str,
) -> dict:
    return {
        "id": scene_id,
        "sceneType": "corrective",
        "title": scene_id,
        "setting": "Hallway.",
        "charactersPresent": ["char-a"],
        "learnerPresent": True,
        "correctiveMetadata": {
            "triggerSceneId": trigger_scene_id,
            "reconvergenceSceneId": reconvergence_scene_id,
            "mayRebranch": False,
        },
        "dialogue": {
            "exchanges": [
                {"exchangeId": "ex-001", "speakerId": "char-a", "text": "Wait.", "tone": "firm"}
            ]
        },
        "decision": {
            "prompt": "Recover.",
            "options": [
                _scene_option(f"{scene_id}-a", next_scene=next_scene, terminal=False, tier="acceptable"),
                _scene_option(f"{scene_id}-b", next_scene=next_scene, terminal=False, tier="suboptimal"),
            ],
        },
    }


def minimal_v1_1_document() -> dict:
    """Smallest valid 1.1.0 document: one terminal scene, one outcome, full classifier."""
    return {
        "simulationId": "test-minimal-v1-1",
        "version": "0.0.1-minimal",
        "schemaVersion": SCHEMA_VERSION_1_1,
        "requiredEngineVersion": "SCENARIO_ENGINE_V2",
        "certificationExamName": "Salesforce Certified Business Analyst",
        "examCode": "BA-201",
        "title": "Minimal 1.1.0 Fixture",
        "learnerRole": {"title": "Analyst", "summary": "Advises during the scenario."},
        "introduction": {
            "companyIntroduction": "Acme Corp.",
            "projectBriefing": {"title": "Project", "summary": "Brief."},
            "startGate": {
                "headline": "Ready?",
                "body": "Begin when ready.",
                "confirmButtonLabel": "Start",
                "cancelBehavior": "return_to_catalog",
            },
        },
        "characters": [
            {
                "characterId": "char-a",
                "displayName": "Alex",
                "roleTitle": "Lead",
                "accessibilityDescription": "Lead at desk",
            }
        ],
        "flags": [],
        "stateVariables": [
            {
                "key": "score",
                "displayName": "Score",
                "polarity": "higher_is_better",
                "minimum": 0,
                "maximum": 100,
                "learnerVisibleDuringRun": False,
            }
        ],
        "initialState": {"score": 50},
        "runtimeCounters": [
            {
                "counterId": "decisionCount",
                "initialValue": 0,
                "incrementOn": [{"event": "decision_applied", "whenTier": "optimal"}],
            }
        ],
        "startScene": "s-terminal",
        "scenes": [
            _core_scene(
                "s-terminal",
                options=[
                    _scene_option("opt-a", terminal=True),
                    _scene_option("opt-b", terminal=True, tier="suboptimal"),
                ],
            )
        ],
        "outcomeClassifier": _minimal_classifier(),
        "outcomes": [
            {
                "outcomeId": "only_outcome",
                "title": "Outcome",
                "classificationRank": 1,
                "narrative": "The run ends.",
            }
        ],
    }


def minimal_v1_1_graph_document() -> dict:
    """Extend minimal doc with SC001-style core + corrective routing graph."""
    doc = minimal_v1_1_document()
    doc["simulationId"] = "test-graph-v1-1"
    doc["flags"] = [
        {
            "flagId": "flag-test",
            "valueType": "boolean",
            "initialValue": False,
            "sticky": True,
            "allowedSetters": [{"sceneId": "SC001-C01", "optionId": "opt-trigger"}],
            "allowedClearers": [{"sceneId": "SC001-C01", "optionId": "opt-clear"}],
            "debriefRelevant": True,
        }
    ]
    doc["runtimeCounters"] = [
        {
            "counterId": "correctiveScenesExperienced",
            "initialValue": 0,
            "minimum": 0,
            "maximum": 3,
            "incrementOn": [{"event": "corrective_scene_entered"}],
        }
    ]
    doc["correctiveBudgetPolicy"] = {
        "maxAvailableCorrectiveScenes": 1,
        "maxExperiencedCorrectiveScenes": 1,
        "maxScoredDecisions": 4,
        "minScoredDecisions": 2,
        "experiencedCounterId": "correctiveScenesExperienced",
    }
    doc["startScene"] = "SC001-C01"
    corrective_route = {
        "triggerOnTiers": ["suboptimal", "high-risk"],
        "budgetCondition": {
            "counterCompare": {"counterId": "correctiveScenesExperienced", "op": "lt", "value": 1}
        },
        "correctiveSceneId": "SC001-R2A",
        "whenCorrectiveSkippedNextSceneId": "SC001-C02",
        "reconvergenceSceneId": "SC001-C02",
    }
    doc["scenes"] = [
        _core_scene(
            "SC001-C01",
            options=[
                _scene_option("opt-good", next_scene="SC001-C02", terminal=False, tier="optimal"),
                _scene_option(
                    "opt-trigger",
                    next_scene="SC001-C02",
                    terminal=False,
                    tier="suboptimal",
                    corrective_route=corrective_route,
                    set_flags=["flag-test"],
                ),
                _scene_option(
                    "opt-clear",
                    next_scene="SC001-C02",
                    terminal=False,
                    tier="acceptable",
                    clear_flags=["flag-test"],
                ),
            ],
        ),
        _corrective_scene(
            "SC001-R2A",
            trigger_scene_id="SC001-C01",
            reconvergence_scene_id="SC001-C02",
            next_scene="SC001-C02",
        ),
        _core_scene(
            "SC001-C02",
            options=[
                _scene_option("opt-end-a", terminal=True, tier="optimal"),
                _scene_option("opt-end-b", terminal=True, tier="acceptable"),
            ],
        ),
    ]
    return doc


def load_vslice_fixture() -> dict:
    text = VSLICE_SPEC_PATH.read_text(encoding="utf-8")
    marker = "## 23. Illustrative vertical-slice JSON"
    section = text.split(marker, 1)[1]
    json_start = section.index("```json") + len("```json\n")
    json_end = section.index("```", json_start)
    return json.loads(section[json_start:json_end])


def _nested_condition(depth: int) -> dict:
    if depth <= 1:
        return {"flagNotSet": "flag-verbal-handoff-only"}
    return {"all": [_nested_condition(depth - 1)]}


def _wide_condition(node_count: int) -> dict:
    leaves = [{"flagNotSet": "flag-verbal-handoff-only"} for _ in range(node_count)]
    return {"any": leaves}


class TestV1_1ValidationFoundation(unittest.TestCase):
    def test_minimal_v1_1_passes_without_blocking_findings(self) -> None:
        doc = minimal_v1_1_document()
        findings = collect_scenario_validation_findings(doc)
        self.assertFalse(findings_contain_blocking(findings))
        validate_scenario_document(doc)

    def test_vslice_fixture_passes_custom_validation(self) -> None:
        doc = load_vslice_fixture()
        findings = validate_catalog_scenario_document(doc, publication=False)
        self.assertFalse(findings_contain_blocking(findings))
        assert_catalog_scenario_valid(doc, publication=False)

    def test_vslice_publication_reports_unreachable_high_outcomes(self) -> None:
        # Honest CV-089: §23 slice composites stay in the failed band; higher
        # declared outcomes remain referenced (CV-089R) but are not path-reachable.
        doc = copy.deepcopy(load_vslice_fixture())
        doc["canonicalContentSha256"] = compute_canonical_content_sha256_v1_1(doc)
        findings = collect_scenario_validation_findings(doc, publication=True)
        cv089 = findings_with_rule(findings, "CV-089")
        self.assertTrue(cv089)
        unreachable_ids = {finding.identifier for finding in cv089}
        self.assertIn("strong_resolution", unreachable_ids)
        self.assertIn("acceptable_resolution", unreachable_ids)
        self.assertIn("partial_resolution", unreachable_ids)
        self.assertNotIn("failed_resolution", unreachable_ids)
        self.assertFalse(any("exceeded safe limits" in finding.message for finding in cv089))

    def test_band_referenced_but_unreachable_outcome_fails_publication(self) -> None:
        doc = minimal_v1_1_document()
        doc["outcomes"] = [
            {
                "outcomeId": "low_outcome",
                "title": "Low",
                "classificationRank": 2,
                "narrative": "Low band.",
            },
            {
                "outcomeId": "high_outcome",
                "title": "High",
                "classificationRank": 1,
                "narrative": "Unreachable high band.",
            },
        ]
        doc["outcomeClassifier"]["scoreBands"] = [
            {"outcomeId": "high_outcome", "minInclusive": 90, "maxExclusive": None},
            {"outcomeId": "low_outcome", "minInclusive": None, "maxExclusive": 90},
        ]
        doc["canonicalContentSha256"] = compute_canonical_content_sha256_v1_1(doc)
        findings = collect_scenario_validation_findings(doc, publication=True)
        self.assertTrue(findings_with_rule(findings, "CV-089"))
        self.assertTrue(
            any(finding.identifier == "high_outcome" for finding in findings_with_rule(findings, "CV-089"))
        )

    def test_cap_forced_outcome_is_recognized_as_reachable(self) -> None:
        doc = minimal_v1_1_document()
        doc["flags"] = [
            {
                "flagId": "flag-force",
                "valueType": "boolean",
                "initialValue": False,
                "allowedSetters": [{"sceneId": "s-terminal", "optionId": "opt-b"}],
                "allowedClearers": [],
            }
        ]
        doc["scenes"][0]["decision"]["options"][1]["setFlags"] = ["flag-force"]
        doc["scenes"][0]["decision"]["options"][1]["evaluationTier"] = "high-risk"
        doc["outcomes"].append(
            {
                "outcomeId": "forced_outcome",
                "title": "Forced",
                "classificationRank": 2,
                "narrative": "Cap forced.",
            }
        )
        doc["outcomeClassifier"]["severeCaps"] = [
            {
                "capId": "CAP-FORCE",
                "when": {"flagSet": "flag-force"},
                "effect": {"forceOutcomeId": "forced_outcome"},
            }
        ]
        # Keep covering bands; forced outcome is also listed for CV-089R.
        doc["outcomeClassifier"]["scoreBands"] = [
            {"outcomeId": "only_outcome", "minInclusive": None, "maxExclusive": None},
        ]
        # Reference forced outcome via maxOutcomeId-style presence: add to band gap-free by
        # using force-only reachability while keeping band for only_outcome; CV-089R needs
        # forced_outcome referenced — severeCaps forceOutcomeId counts in CV-089R.
        doc["canonicalContentSha256"] = compute_canonical_content_sha256_v1_1(doc)
        findings = collect_scenario_validation_findings(doc, publication=True)
        self.assertFalse(
            any(finding.identifier == "forced_outcome" for finding in findings_with_rule(findings, "CV-089")),
            msg=[(f.identifier, f.message) for f in findings_with_rule(findings, "CV-089")],
        )
        self.assertFalse(findings_contain_blocking(findings), msg=list(findings))

    def test_ba_1_0_0_still_passes_validate_scenario_document(self) -> None:
        document = json.loads(BA_SCENARIO_PATH.read_text(encoding="utf-8"))
        validate_scenario_document(document)
        findings = collect_scenario_validation_findings(document)
        self.assertFalse(findings_contain_blocking(findings))

    def test_unsupported_schema_version_fails_closed(self) -> None:
        doc = mutate(minimal_v1_1_document(), ["schemaVersion"], "9.9.9")
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_contain_blocking(findings))
        self.assertTrue(findings_with_rule(findings, "CV-001"))
        with self.assertRaises(ScenarioValidationError):
            validate_scenario_document(doc)

    def test_build_scenario_content_rejects_v1_1_engine_message(self) -> None:
        doc = minimal_v1_1_document()
        with self.assertRaises(ScenarioContentError) as ctx:
            build_scenario_content(doc)
        message = str(ctx.exception)
        self.assertIn("1.1.0", message)
        self.assertIn("SCENARIO_ENGINE_V2", message)

    def test_build_scenario_content_hint_mismatch_before_engine_message(self) -> None:
        doc = minimal_v1_1_document()
        with self.assertRaises(ScenarioValidationError) as ctx:
            build_scenario_content(doc, schema_version=DEFAULT_SCHEMA_VERSION)
        self.assertIn("does not match", str(ctx.exception))

    def test_schema_version_hint_mismatch_for_1_1_document(self) -> None:
        doc = minimal_v1_1_document()
        findings = collect_scenario_validation_findings(doc, schema_version=DEFAULT_SCHEMA_VERSION)
        self.assertTrue(findings_with_rule(findings, "CV-001"))
        first = first_blocking_finding(findings)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.layer, "structural")

    def test_schema_version_hint_mismatch_for_1_0_document(self) -> None:
        document = json.loads(BA_SCENARIO_PATH.read_text(encoding="utf-8"))
        findings = collect_scenario_validation_findings(document, schema_version=SCHEMA_VERSION_1_1)
        self.assertTrue(findings_with_rule(findings, "CV-001"))

    def test_matching_schema_version_hint_ok(self) -> None:
        doc = minimal_v1_1_document()
        findings = collect_scenario_validation_findings(doc, schema_version=SCHEMA_VERSION_1_1)
        self.assertFalse(findings_contain_blocking(findings))

    def test_json_schema_validation_does_not_mutate_input(self) -> None:
        doc = minimal_v1_1_document()
        before = copy.deepcopy(doc)
        collect_scenario_validation_findings(doc)
        validate_scenario_document(doc)
        self.assertEqual(doc, before)

    def test_v1_0_0_behavior_unchanged_inline(self) -> None:
        document = json.loads(BA_SCENARIO_PATH.read_text(encoding="utf-8"))
        content = load_scenario_content(BA_SCENARIO_PATH)
        self.assertEqual(content.schema_version, DEFAULT_SCHEMA_VERSION)
        self.assertEqual(content.simulation_id, "ba201-sim-meridian-health-01")
        terminal_scene = next(scene for scene in document["scenes"] if scene["id"] == "s24_golive_readiness")
        terminal_scene["decision"]["options"][0]["nextScene"] = "s01_kickoff"
        with self.assertRaises(ScenarioValidationError) as ctx:
            validate_scenario_document(document)
        self.assertIn("cycle", str(ctx.exception).lower())


class TestV1_1StructuralDuplicates(unittest.TestCase):
    def test_duplicate_character_id(self) -> None:
        doc = minimal_v1_1_document()
        doc = mutate(doc, ["characters"], list(doc["characters"]) + [copy.deepcopy(doc["characters"][0])])
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-015"))

    def test_duplicate_scene_id(self) -> None:
        doc = minimal_v1_1_document()
        doc = mutate(doc, ["scenes"], list(doc["scenes"]) + [copy.deepcopy(doc["scenes"][0])])
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-010"))

    def test_duplicate_option_id_within_scene(self) -> None:
        doc = minimal_v1_1_document()
        doc["scenes"][0]["decision"]["options"][1]["id"] = doc["scenes"][0]["decision"]["options"][0]["id"]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-011"))


class TestV1_1SpeakerAndLearner(unittest.TestCase):
    def test_unknown_speaker_in_dialogue(self) -> None:
        doc = minimal_v1_1_document()
        doc["scenes"][0]["dialogue"]["exchanges"][0]["speakerId"] = "missing-char"
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-043"))

    def test_learner_speaker_allowed_in_dialogue(self) -> None:
        doc = minimal_v1_1_document()
        doc["scenes"][0]["dialogue"]["exchanges"].append(
            {"exchangeId": "ex-learner", "speakerId": "learner", "text": "I agree.", "tone": "neutral"}
        )
        findings = collect_scenario_validation_findings(doc)
        self.assertFalse(findings_with_rule(findings, "CV-043"))

    def test_learner_in_characters_registry_fails(self) -> None:
        doc = minimal_v1_1_document()
        doc["characters"].append(
            {"characterId": "learner", "displayName": "You", "roleTitle": "Learner"}
        )
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_contain_blocking(findings))

    def test_learner_in_characters_present_fails(self) -> None:
        doc = minimal_v1_1_document()
        doc["scenes"][0]["charactersPresent"] = ["learner"]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-041"))


class TestV1_1GraphRouting(unittest.TestCase):
    def test_unknown_scene_route(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][0]["decision"]["options"][0]["routing"]["primaryNextSceneId"] = "missing-scene"
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-060"))

    def test_self_loop(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][0]["decision"]["options"][0]["routing"]["primaryNextSceneId"] = "SC001-C01"
        doc["scenes"][0]["decision"]["options"][0]["routing"]["terminal"] = False
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-072"))

    def test_illegal_three_node_cycle(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"] = [
            _core_scene(
                "s-a",
                options=[
                    _scene_option("a1", next_scene="s-b", terminal=False),
                    _scene_option("a2", next_scene="s-b", terminal=False, tier="suboptimal"),
                ],
            ),
            _core_scene(
                "s-b",
                options=[
                    _scene_option("b1", next_scene="s-c", terminal=False),
                    _scene_option("b2", next_scene="s-c", terminal=False, tier="suboptimal"),
                ],
            ),
            _core_scene(
                "s-c",
                options=[
                    _scene_option("c1", next_scene="s-a", terminal=False),
                    _scene_option("c2", next_scene="s-a", terminal=False, tier="suboptimal"),
                ],
            ),
        ]
        doc["startScene"] = "s-a"
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-072"))

    def test_unreachable_core_scene(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"].append(
            _core_scene(
                "orphan-core",
                options=[
                    _scene_option("orphan-a", terminal=True),
                    _scene_option("orphan-b", terminal=True, tier="suboptimal"),
                ],
            )
        )
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-073"))

    def test_corrective_to_corrective_route(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"].append(
            _corrective_scene(
                "SC001-R2B",
                trigger_scene_id="SC001-C01",
                reconvergence_scene_id="SC001-C02",
                next_scene="SC001-R2A",
            )
        )
        doc["scenes"][1]["decision"]["options"][0]["routing"]["primaryNextSceneId"] = "SC001-R2B"
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-065"))

    def test_missing_reconvergence_target_alignment(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][0]["decision"]["options"][1]["routing"]["correctiveRoute"][
            "reconvergenceSceneId"
        ] = "SC001-C01"
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-066"))

    def test_corrective_path_over_budget(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["correctiveBudgetPolicy"]["maxExperiencedCorrectiveScenes"] = 5
        doc["correctiveBudgetPolicy"]["maxAvailableCorrectiveScenes"] = 1
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-071"))

    def test_excessive_max_path_vs_max_scored_decisions(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["correctiveBudgetPolicy"]["maxScoredDecisions"] = 1
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-074"))

    def test_cycle_skips_scored_path_bounds_checks(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["correctiveBudgetPolicy"]["maxScoredDecisions"] = 1
        doc["scenes"][0]["decision"]["options"][0]["routing"]["primaryNextSceneId"] = "SC001-C01"
        doc["scenes"][0]["decision"]["options"][0]["routing"]["terminal"] = False
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-072"))
        self.assertFalse(findings_with_rule(findings, "CV-074"))
        self.assertFalse(findings_with_rule(findings, "CV-075"))

    def test_valid_corrective_trigger_skip_reconvergence_graph(self) -> None:
        doc = minimal_v1_1_graph_document()
        findings = collect_scenario_validation_findings(doc)
        self.assertFalse(findings_contain_blocking(findings))
        validate_scenario_document(doc)


class TestV1_1FlagsAndState(unittest.TestCase):
    def test_duplicate_set_flags_in_option(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][0]["decision"]["options"][1]["setFlags"] = ["flag-test", "flag-test"]
        findings = collect_scenario_validation_findings(doc)
        dupes = [
            finding
            for finding in findings_with_rule(findings, "CV-051")
            if "duplicate setFlags" in finding.message
        ]
        self.assertTrue(dupes)

    def test_set_and_clear_same_flag_allowed(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][0]["decision"]["options"][1]["setFlags"] = ["flag-test"]
        doc["scenes"][0]["decision"]["options"][1]["clearFlags"] = ["flag-test"]
        findings = collect_scenario_validation_findings(doc)
        dupes = [
            finding
            for finding in findings_with_rule(findings, "CV-051")
            if "duplicate" in finding.message
        ]
        self.assertFalse(dupes)

    def test_nan_initial_state_fails_cv_fin(self) -> None:
        doc = minimal_v1_1_document()
        doc = mutate(doc, ["initialState", "score"], float("nan"))
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-FIN"))

    def test_positive_infinity_initial_state_fails_cv_fin(self) -> None:
        doc = minimal_v1_1_document()
        doc = mutate(doc, ["initialState", "score"], float("inf"))
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-FIN"))

    def test_negative_infinity_initial_state_fails_cv_fin(self) -> None:
        doc = minimal_v1_1_document()
        doc = mutate(doc, ["initialState", "score"], float("-inf"))
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-FIN"))

    def test_bool_initial_state_fails_cv_fin(self) -> None:
        doc = minimal_v1_1_document()
        doc = mutate(doc, ["initialState", "score"], True)
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-FIN"))

    def test_bool_counter_initial_value_fails_cv_fin(self) -> None:
        doc = minimal_v1_1_document()
        doc["runtimeCounters"][0]["initialValue"] = False
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-FIN"))

    def test_unknown_flag_reference_on_set(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][0]["decision"]["options"][0]["setFlags"] = ["flag-missing"]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-051"))

    def test_unauthorized_flag_setter(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][2]["decision"]["options"][0]["setFlags"] = ["flag-test"]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-052"))

    def test_unauthorized_flag_clearer(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["flags"][0]["allowedClearers"] = []
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-052"))

    def test_unknown_state_variable_in_delta(self) -> None:
        doc = minimal_v1_1_document()
        doc["scenes"][0]["decision"]["options"][0]["stateChanges"] = {"missing": 1}
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-050"))

    def test_state_initial_outside_bounds(self) -> None:
        doc = minimal_v1_1_document()
        doc = mutate(doc, ["initialState", "score"], 150)
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-054"))

    def test_counter_mixed_into_state_and_unknown_counter_ref(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["initialState"]["correctiveScenesExperienced"] = 0
        doc["stateVariables"].append(
            {
                "key": "correctiveScenesExperienced",
                "displayName": "Wrong",
                "polarity": "higher_is_worse",
                "minimum": 0,
                "maximum": 3,
                "learnerVisibleDuringRun": False,
            }
        )
        doc["scenes"][0]["dialogue"]["variants"] = [
            {
                "variantId": "bad-counter",
                "priority": 1,
                "when": {"counterCompare": {"counterId": "missing-counter", "op": "gte", "value": 1}},
                "overrides": [{"exchangeId": "ex-001", "text": "Override.", "tone": "firm"}],
            }
        ]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-055"))
        self.assertTrue(findings_with_rule(findings, "CV-036"))


class TestV1_1ClassifierAndOutcomes(unittest.TestCase):
    def test_formula_cycle(self) -> None:
        doc = minimal_v1_1_document()
        doc["outcomeClassifier"]["compositeFormula"] = {
            "type": "linear_blend",
            "terms": [
                {"metric": "decisionQuality", "weight": 1.0},
            ],
        }
        doc["outcomeClassifier"]["decisionQualityFormula"] = {
            "type": "identity",
            "source": "composite",
        }
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-082"))

    def test_formula_weights_not_summing_to_one(self) -> None:
        doc = minimal_v1_1_document()
        doc["outcomeClassifier"]["compositeFormula"]["terms"] = [
            {"metric": "positiveHealth", "weight": 0.4},
            {"metric": "decisionQuality", "weight": 0.4},
        ]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-081"))

    def test_outcome_band_overlap(self) -> None:
        doc = minimal_v1_1_document()
        doc["outcomes"].append(
            {
                "outcomeId": "second_outcome",
                "title": "Second",
                "classificationRank": 2,
                "narrative": "Another ending.",
            }
        )
        doc["outcomeClassifier"]["scoreBands"] = [
            {"outcomeId": "only_outcome", "minInclusive": None, "maxExclusive": 60},
            {"outcomeId": "second_outcome", "minInclusive": 50, "maxExclusive": None},
        ]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-087"))

    def test_outcome_band_gap(self) -> None:
        doc = minimal_v1_1_document()
        doc["outcomes"].append(
            {
                "outcomeId": "second_outcome",
                "title": "Second",
                "classificationRank": 2,
                "narrative": "Another ending.",
            }
        )
        doc["outcomeClassifier"]["scoreBands"] = [
            {"outcomeId": "only_outcome", "minInclusive": None, "maxExclusive": 40},
            {"outcomeId": "second_outcome", "minInclusive": 50, "maxExclusive": None},
        ]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-087"))

    def test_unknown_outcome_reference_in_score_band(self) -> None:
        doc = minimal_v1_1_document()
        doc["outcomeClassifier"]["scoreBands"][0]["outcomeId"] = "ghost-outcome"
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-086"))

    def test_unreachable_outcome_reference_fails_cv_089r(self) -> None:
        doc = minimal_v1_1_document()
        doc["outcomes"].append(
            {
                "outcomeId": "orphan_outcome",
                "title": "Orphan",
                "classificationRank": 2,
                "narrative": "Never referenced.",
            }
        )
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-089R"))


class TestV1_1DialogueAndConditions(unittest.TestCase):
    def test_duplicate_dialogue_variant_priority(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][0]["dialogue"]["variants"] = [
            {
                "variantId": "v1",
                "priority": 10,
                "when": {"flagNotSet": "flag-test"},
                "overrides": [{"exchangeId": "ex-001", "text": "One.", "tone": "firm"}],
            },
            {
                "variantId": "v2",
                "priority": 10,
                "when": {"flagSet": "flag-test"},
                "overrides": [{"exchangeId": "ex-001", "text": "Two.", "tone": "firm"}],
            },
        ]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-044"))

    def test_missing_dialogue_override_target(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][0]["dialogue"]["variants"] = [
            {
                "variantId": "v1",
                "priority": 10,
                "when": {"flagNotSet": "flag-test"},
                "overrides": [{"exchangeId": "missing-exchange", "text": "Nope.", "tone": "firm"}],
            }
        ]
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-045"))

    def test_condition_depth_limit(self) -> None:
        doc = load_vslice_fixture()
        doc["scenes"][1]["dialogue"]["variants"][0]["when"] = _nested_condition(9)
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-032"))

    def test_condition_node_count_limit(self) -> None:
        doc = load_vslice_fixture()
        doc["scenes"][1]["dialogue"]["variants"][0]["when"] = _wide_condition(65)
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-033"))

    def test_unknown_condition_flag_state_counter(self) -> None:
        doc = load_vslice_fixture()
        doc["scenes"][1]["dialogue"]["variants"][0]["when"] = {"flagSet": "flag-does-not-exist"}
        doc["outcomeClassifier"]["moderateCaps"][0]["when"] = {
            "stateCompare": {"variableId": "missing-state", "op": "gte", "value": 1}
        }
        doc["outcomeClassifier"]["strongGuards"][0]["when"] = {
            "counterCompare": {"counterId": "missing-counter", "op": "gte", "value": 1}
        }
        findings = collect_scenario_validation_findings(doc)
        self.assertTrue(findings_with_rule(findings, "CV-034"))
        self.assertTrue(findings_with_rule(findings, "CV-035"))
        self.assertTrue(findings_with_rule(findings, "CV-036"))


class TestV1_1PublicationAndOrdering(unittest.TestCase):
    def test_publication_without_hash_fails(self) -> None:
        doc = minimal_v1_1_document()
        doc["requiredEngineVersion"] = "SCENARIO_ENGINE_V2"
        findings = collect_scenario_validation_findings(doc, publication=True)
        self.assertTrue(findings_with_rule(findings, "CV-HASH"))

    def test_minimal_publication_passes_with_hash(self) -> None:
        doc = minimal_v1_1_document()
        doc["requiredEngineVersion"] = "SCENARIO_ENGINE_V2"
        doc["canonicalContentSha256"] = compute_canonical_content_sha256_v1_1(doc)
        findings = collect_scenario_validation_findings(doc, publication=True)
        self.assertFalse(findings_contain_blocking(findings))

    def test_reachability_bound_exhaustion_fails_closed(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["requiredEngineVersion"] = "SCENARIO_ENGINE_V2"
        doc["canonicalContentSha256"] = compute_canonical_content_sha256_v1_1(doc)
        original_limit = _MAX_REACHABILITY_STATES
        try:
            import utils.scenario_validation_v1_1 as validation_module

            validation_module._MAX_REACHABILITY_STATES = 1
            findings = collect_scenario_validation_findings(doc, publication=True)
            self.assertTrue(findings_with_rule(findings, "CV-089"))
            exhausted = [
                finding
                for finding in findings_with_rule(findings, "CV-089")
                if "exceeded safe limits" in finding.message
            ]
            self.assertTrue(exhausted)
        finally:
            validation_module._MAX_REACHABILITY_STATES = original_limit

    def test_canonical_hash_mismatch_publication(self) -> None:
        doc = minimal_v1_1_document()
        doc["requiredEngineVersion"] = "SCENARIO_ENGINE_V2"
        doc["canonicalContentSha256"] = "0" * 64
        findings = collect_scenario_validation_findings(doc, publication=True)
        self.assertTrue(findings_with_rule(findings, "PB-HASH"))
        with self.assertRaises(ScenarioValidationError):
            validate_scenario_for_publication(doc)

    def test_unsupported_engine_identifier_publication(self) -> None:
        doc = minimal_v1_1_document()
        doc["requiredEngineVersion"] = "SCENARIO_ENGINE_V99"
        doc["canonicalContentSha256"] = compute_canonical_content_sha256_v1_1(doc)
        findings = collect_scenario_validation_findings(doc, publication=True)
        self.assertTrue(findings_with_rule(findings, "CV-102"))

    def test_deterministic_finding_ordering(self) -> None:
        doc = minimal_v1_1_graph_document()
        doc["scenes"][0]["decision"]["options"][0]["routing"]["primaryNextSceneId"] = "missing-a"
        doc["scenes"][0]["decision"]["options"][0]["setFlags"] = ["flag-missing"]
        doc["scenes"][0]["dialogue"]["exchanges"][0]["speakerId"] = "ghost"
        first = collect_scenario_validation_findings(doc)
        second = collect_scenario_validation_findings(doc)
        self.assertEqual(first, second)
        layer_rank = {
            "json_schema": 0,
            "structural": 1,
            "semantic": 2,
            "graph": 3,
            "publication": 4,
            "runtime": 5,
        }
        ranks = [layer_rank.get(finding.layer, 99) for finding in first]
        self.assertEqual(ranks, sorted(ranks))
        semantic_indexes = [index for index, finding in enumerate(first) if finding.layer == "semantic"]
        graph_indexes = [index for index, finding in enumerate(first) if finding.layer == "graph"]
        if semantic_indexes and graph_indexes:
            self.assertLess(max(semantic_indexes), min(graph_indexes))


if __name__ == "__main__":
    unittest.main()
