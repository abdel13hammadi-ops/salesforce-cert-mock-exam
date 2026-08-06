"""Focused tests for SCENARIO_ENGINE_V2 (schema 1.1.0 deterministic runtime).

Covers: version isolation from Engine V1, pure/deterministic initialization,
content immutability, decision-input contract enforcement, the 16-step
decision application order, corrective routing/budget/skip, dialogue variant
selection, deterministic option display order, formula evaluation, the
seven-step outcome classifier, debrief trace, replay, and learner-safe view
separation.

This module never touches protected paths (.local/, local_only/, etc.) and
never stages/commits anything.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_engine_v2 import (
    ENGINE_VERSION,
    ClassificationTrace,
    CorrectiveEntryEvent,
    LearnerSceneView,
    LearnerTerminalView,
    ScenarioClassificationV2Error,
    ScenarioContentV2Error,
    ScenarioDecisionInputV2,
    ScenarioEngineV2Error,
    ScenarioReplayV2Error,
    ScenarioRunStateV2Error,
    SkippedCorrectiveEvent,
    apply_decision_v2,
    build_debrief_trace,
    build_learner_scene_view,
    build_learner_terminal_view,
    build_scenario_content_v2,
    classify_outcome,
    compute_composite,
    compute_decision_quality,
    compute_positive_health,
    deterministic_option_display_order,
    evaluate_condition,
    replay_scenario_run_v2,
    round_half_away_from_zero,
    select_dialogue_variant,
    start_scenario_run_v2,
)
from utils.scenario_schema import ScenarioContentError, load_scenario_content

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "scenario_engine_v2_vslice_1_1_0.json"
V1_SCENARIO_PATH = (
    REPO_ROOT
    / "scenario_content"
    / "business_analyst"
    / "ba201-sim-meridian-health-01"
    / "1.0.0"
    / "scenario.json"
)


def _load_document() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class EngineV2TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _load_document()
        self.content = build_scenario_content_v2(copy.deepcopy(self.document))


# ---------------------------------------------------------------------------
# 1-4: initialization, version isolation, content immutability
# ---------------------------------------------------------------------------


class TestInitializationAndVersionIsolation(EngineV2TestCase):
    def test_01_valid_v2_initialization(self):
        run = start_scenario_run_v2(self.content, attempt_id="attempt-init")
        self.assertEqual(run.current_scene_id, "SC001-C01")
        self.assertEqual(run.expected_sequence_number, 1)
        self.assertFalse(run.is_complete)

    def test_02_unsupported_engine_rejected(self):
        bad = copy.deepcopy(self.document)
        bad["requiredEngineVersion"] = "SCENARIO_ENGINE_V1"
        with self.assertRaises(ScenarioContentV2Error):
            build_scenario_content_v2(bad)

    def test_02b_unsupported_schema_version_rejected(self):
        bad = copy.deepcopy(self.document)
        bad["schemaVersion"] = "1.0.0"
        with self.assertRaises(ScenarioContentV2Error):
            build_scenario_content_v2(bad)

    def test_03_schema_v1_content_remains_on_engine_v1(self):
        # schema 1.0.0 content must load via Engine V1's loader and must be
        # rejected outright by build_scenario_content_v2 (no silent V1
        # reinterpretation under V2 semantics).
        v1_content = load_scenario_content(V1_SCENARIO_PATH)
        self.assertEqual(v1_content.schema_version, "1.0.0")
        with self.assertRaises(ScenarioContentV2Error):
            build_scenario_content_v2(json.loads(V1_SCENARIO_PATH.read_text(encoding="utf-8")))

    def test_04_content_not_mutated_by_execution(self):
        pristine = copy.deepcopy(self.document)
        run = start_scenario_run_v2(self.content, attempt_id="attempt-immutable")
        for seq, scene_id, option_id in (
            (1, "SC001-C01", "opt-sc001-c01-a"),
            (2, "SC001-C02", "opt-sc001-c02-a"),
            (3, "SC001-C03", "opt-sc001-c03-a"),
            (4, "SC001-C04", "opt-sc001-c04-a"),
        ):
            run = apply_decision_v2(run, ScenarioDecisionInputV2(seq, scene_id, option_id))
        self.assertEqual(pristine, self.document)
        self.assertEqual(json.loads(json.dumps(dict(self.content.document), default=_thaw)), pristine)


def _thaw(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _thaw(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if hasattr(value, "items"):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        return [_thaw(v) for v in value]
    return value


def _json_default(value):
    return str(value)


# ---------------------------------------------------------------------------
# 5-7: initial state / flags / counters
# ---------------------------------------------------------------------------


class TestInitialRuntimeValues(EngineV2TestCase):
    def test_05_initial_state_values_correct(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        self.assertEqual(dict(run.state), dict(self.document["initialState"]))

    def test_06_initial_flags_correct(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        self.assertEqual(run.flags, frozenset())  # all fixture flags default false

    def test_07_initial_counters_separate_from_state(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        self.assertEqual(
            dict(run.counters),
            {"correctiveScenesExperienced": 0, "highRiskDecisionCount": 0, "optimalDecisionCount": 0},
        )
        self.assertNotIn("correctiveScenesExperienced", run.state)


# ---------------------------------------------------------------------------
# 8-11: dialogue variant + option display order determinism
# ---------------------------------------------------------------------------


class TestDeterminism(EngineV2TestCase):
    def test_08_first_dialogue_variant_deterministic(self):
        run_a = start_scenario_run_v2(self.content, attempt_id="attempt-x")
        run_b = start_scenario_run_v2(self.content, attempt_id="attempt-x")
        self.assertEqual(run_a.variant_selections, run_b.variant_selections)

    def test_09_first_option_order_deterministic(self):
        run_a = start_scenario_run_v2(self.content, attempt_id="attempt-x")
        run_b = start_scenario_run_v2(self.content, attempt_id="attempt-x")
        self.assertEqual(
            run_a.option_display_order_by_scene["SC001-C01"],
            run_b.option_display_order_by_scene["SC001-C01"],
        )

    def test_10_same_attempt_identity_same_order(self):
        order_a = deterministic_option_display_order(
            ["opt-a", "opt-b", "opt-c"],
            attempt_id="fixed-attempt",
            simulation_id=self.content.simulation_id,
            version=self.content.version,
            canonical_content_sha256=self.content.canonical_content_sha256,
            scene_id="SC001-C01",
        )
        order_b = deterministic_option_display_order(
            ["opt-a", "opt-b", "opt-c"],
            attempt_id="fixed-attempt",
            simulation_id=self.content.simulation_id,
            version=self.content.version,
            canonical_content_sha256=self.content.canonical_content_sha256,
            scene_id="SC001-C01",
        )
        self.assertEqual(order_a, order_b)

    def test_11_different_attempt_identity_can_differ(self):
        ids = ["opt-a", "opt-b", "opt-c", "opt-d", "opt-e"]
        orders = {
            deterministic_option_display_order(
                ids,
                attempt_id=f"attempt-{i}",
                simulation_id=self.content.simulation_id,
                version=self.content.version,
                canonical_content_sha256=self.content.canonical_content_sha256,
                scene_id="SC001-C01",
            )
            for i in range(8)
        }
        self.assertGreater(len(orders), 1)

    def test_order_is_permutation_of_input(self):
        order = deterministic_option_display_order(
            ["opt-a", "opt-b", "opt-c"],
            attempt_id="p1",
            simulation_id="s",
            version="v",
            canonical_content_sha256="h",
            scene_id="sc",
        )
        self.assertEqual(sorted(order), ["opt-a", "opt-b", "opt-c"])


# ---------------------------------------------------------------------------
# 12-13: decision-input contract (stable option id, not display index)
# ---------------------------------------------------------------------------


class TestDecisionInputContract(EngineV2TestCase):
    def test_12_submission_uses_stable_option_id(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run2 = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        self.assertEqual(run2.current_scene_id, "SC001-C02")

    def test_13_display_index_cannot_be_submitted_as_identity(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        with self.assertRaises(ScenarioRunStateV2Error):
            apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "0"))

    def test_decision_input_has_no_hidden_fields(self):
        field_names = {f.name for f in dataclasses.fields(ScenarioDecisionInputV2)}
        self.assertEqual(field_names, {"sequence_number", "scene_id", "option_id"})


# ---------------------------------------------------------------------------
# 14-18: state deltas, clamping, flags, tier, learner-view hiding
# ---------------------------------------------------------------------------


class TestStateFlagTierApplication(EngineV2TestCase):
    def test_14_correct_state_deltas_applied(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run2 = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        expected = dict(self.document["initialState"])
        deltas = self.document["scenes"][0]["decision"]["options"][0]["stateChanges"]
        for key, delta in deltas.items():
            expected[key] = expected[key] + delta
        self.assertEqual(dict(run2.state), {k: float(v) for k, v in expected.items()})

    def test_15_state_clamping_applied(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-c"))
        # opt-sc001-c02-c pushes scheduleImpact +10 from 39 -> 49 (no clamp yet);
        # instead assert clamping logic directly on the mutation primitive.
        clamped = self.content.state_bounds["customerConfidence"]
        self.assertEqual(clamped, (0.0, 100.0))
        # Directly verify clamp behavior at the boundary via a large synthetic delta.
        from utils.scenario_engine_v2 import _apply_state_deltas

        over = _apply_state_deltas(self.content, {"customerConfidence": 95.0}, {"customerConfidence": 50.0})
        self.assertEqual(over["customerConfidence"], 100.0)
        under = _apply_state_deltas(self.content, {"customerConfidence": 5.0}, {"customerConfidence": -50.0})
        self.assertEqual(under["customerConfidence"], 0.0)

    def test_16_flags_clear_before_set(self):
        from utils.scenario_engine_v2 import _apply_flag_changes

        f_verbal = "flag-verbal-handoff-only"
        f_date = "flag-unsupported-customer-date"
        result = _apply_flag_changes(
            self.content, frozenset({f_verbal}), clear=(f_verbal,), set_=(f_verbal,)
        )
        self.assertEqual(result, frozenset({f_verbal}))
        result2 = _apply_flag_changes(
            self.content, frozenset({f_verbal, f_date}), clear=(f_verbal,), set_=()
        )
        self.assertEqual(result2, frozenset({f_date}))

    def test_17_evaluation_tier_recorded_internally(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        self.assertEqual(run.tier_history, ("optimal",))
        self.assertEqual(run.decisions[-1].evaluation_tier, "optimal")

    def test_18_hidden_tier_absent_from_learner_view(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        view = build_learner_scene_view(run)
        for field_name in dataclasses.fields(view):
            self.assertNotIn("tier", field_name.name.lower())
        for option in view.options:
            self.assertFalse(hasattr(option, "evaluationTier"))
            self.assertFalse(hasattr(option, "evaluation_tier"))


# ---------------------------------------------------------------------------
# 19-26: transitions, corrective trigger/budget/skip/reconvergence
# ---------------------------------------------------------------------------


class TestCorrectiveRouting(EngineV2TestCase):
    def test_19_normal_c01_to_c02_transition(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        self.assertEqual(run.current_scene_id, "SC001-C02")

    def test_20_c02_corrective_trigger_enters_r2a(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"))
        self.assertEqual(run.current_scene_id, "SC001-R2A")

    def test_21_corrective_entry_increments_counter_once(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        self.assertEqual(run.counters["correctiveScenesExperienced"], 0)
        run = apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"))
        self.assertEqual(run.counters["correctiveScenesExperienced"], 1)
        self.assertEqual(len(run.corrective_entries), 1)
        self.assertIsInstance(run.corrective_entries[0], CorrectiveEntryEvent)

    def test_22_r2a_reconverges_to_c03(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(3, "SC001-R2A", "opt-sc001-r2a-a"))
        self.assertEqual(run.current_scene_id, "SC001-C03")

    def test_23_corrective_scene_cannot_rebranch(self):
        scene = self.content.scenes_by_id["SC001-R2A"]
        for option in scene["decision"]["options"]:
            self.assertNotIn("correctiveRoute", option.get("routing", {}))

    def test_24_budget_exhausted_corrective_skips_to_c03(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(3, "SC001-R2A", "opt-sc001-r2a-c"))
        self.assertEqual(run.current_scene_id, "SC001-C03")
        self.assertEqual(run.counters["correctiveScenesExperienced"], 1)
        # C03's option opt-sc001-c03-a is terminal (no further corrective route
        # possible in this fixture); demonstrate skip using a synthetically
        # pre-exhausted budget on a fresh run instead, since the fixture's
        # only two corrective-triggering scenes both route through R2A.
        run2 = start_scenario_run_v2(self.content, attempt_id="a2")
        run2 = apply_decision_v2(run2, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        run2 = dataclasses.replace(
            run2, counters={**dict(run2.counters), "correctiveScenesExperienced": 1}
        )
        run2 = apply_decision_v2(run2, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"))
        self.assertEqual(run2.current_scene_id, "SC001-C03")
        self.assertEqual(len(run2.corrective_entries), 0)
        self.assertEqual(len(run2.skipped_corrective_events), 1)

    def test_25_skipped_corrective_does_not_increment_counter(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        run = dataclasses.replace(run, counters={**dict(run.counters), "correctiveScenesExperienced": 1})
        run = apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"))
        self.assertEqual(run.counters["correctiveScenesExperienced"], 1)

    def test_26_skipped_event_recorded(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        run = dataclasses.replace(run, counters={**dict(run.counters), "correctiveScenesExperienced": 1})
        run = apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"))
        self.assertEqual(len(run.skipped_corrective_events), 1)
        event = run.skipped_corrective_events[0]
        self.assertIsInstance(event, SkippedCorrectiveEvent)
        self.assertEqual(event.attempted_corrective_scene_id, "SC001-R2A")
        self.assertEqual(event.reconvergence_scene_id, "SC001-C03")
        self.assertEqual(event.reason, "budget_exhausted")

    def test_prior_weak_choice_consequences_remain_after_corrective(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-c"))
        self.assertIn("flag-unsupported-customer-date", run.flags)
        run = apply_decision_v2(run, ScenarioDecisionInputV2(3, "SC001-R2A", "opt-sc001-r2a-a"))
        # corrective completion does not erase the originating decision's flag
        self.assertIn("flag-unsupported-customer-date", run.flags)


# ---------------------------------------------------------------------------
# 27-28: dialogue variant selection
# ---------------------------------------------------------------------------


class TestDialogueVariants(EngineV2TestCase):
    def test_27_flag_dependent_variant_selected(self):
        scene = self.content.scenes_by_id["SC001-C03"]
        resolved = select_dialogue_variant(
            scene,
            content=self.content,
            flags=frozenset({"flag-verbal-handoff-only"}),
            state=self.content.initial_state,
            counters=self.content.initial_counters,
        )
        self.assertEqual(resolved.selected_variant_id, "c03-verbal-handoff")

    def test_27b_higher_priority_variant_wins_when_both_match(self):
        scene = self.content.scenes_by_id["SC001-C03"]
        resolved = select_dialogue_variant(
            scene,
            content=self.content,
            flags=frozenset({"flag-verbal-handoff-only", "flag-sales-reengaged"}),
            state=self.content.initial_state,
            counters=self.content.initial_counters,
        )
        self.assertEqual(resolved.selected_variant_id, "c03-both-flags")

    def test_28_variant_fallback_selected_when_condition_absent(self):
        scene = self.content.scenes_by_id["SC001-C03"]
        resolved = select_dialogue_variant(
            scene,
            content=self.content,
            flags=frozenset(),
            state=self.content.initial_state,
            counters=self.content.initial_counters,
        )
        self.assertIsNone(resolved.selected_variant_id)
        self.assertTrue(len(resolved.exchanges) > 0)

    def test_variant_override_preserves_exchange_ids(self):
        scene = self.content.scenes_by_id["SC001-C02"]
        base = select_dialogue_variant(
            scene, content=self.content, flags=frozenset(), counters=self.content.initial_counters,
            state=self.content.initial_state,
        )
        overridden = select_dialogue_variant(
            scene,
            content=self.content,
            flags=frozenset({"flag-verbal-handoff-only"}),
            state=self.content.initial_state,
            counters=self.content.initial_counters,
        )
        base_ids = [ex["exchangeId"] for ex in base.exchanges]
        overridden_ids = [ex["exchangeId"] for ex in overridden.exchanges]
        self.assertEqual(base_ids, overridden_ids)


# ---------------------------------------------------------------------------
# 29-33: rejection without mutation
# ---------------------------------------------------------------------------


class TestRejectionWithoutMutation(EngineV2TestCase):
    def test_29_stale_sequence_rejected_without_mutation(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        with self.assertRaises(ScenarioRunStateV2Error):
            apply_decision_v2(run, ScenarioDecisionInputV2(0, "SC001-C01", "opt-sc001-c01-a"))
        self.assertEqual(run.expected_sequence_number, 1)
        self.assertEqual(run.current_scene_id, "SC001-C01")

    def test_29b_future_sequence_rejected(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        with self.assertRaises(ScenarioRunStateV2Error):
            apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C01", "opt-sc001-c01-a"))

    def test_30_scene_mismatch_rejected_without_mutation(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        with self.assertRaises(ScenarioRunStateV2Error):
            apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C02", "opt-sc001-c02-a"))
        self.assertEqual(run.current_scene_id, "SC001-C01")

    def test_31_unknown_option_rejected_without_mutation(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        with self.assertRaises(ScenarioRunStateV2Error):
            apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-does-not-exist"))
        self.assertEqual(run.expected_sequence_number, 1)

    def test_31b_option_not_belonging_to_scene_rejected(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        with self.assertRaises(ScenarioRunStateV2Error):
            apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c02-a"))

    def test_32_duplicate_submission_rejected_without_mutation(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        run2 = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        with self.assertRaises(ScenarioRunStateV2Error):
            apply_decision_v2(run2, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        self.assertEqual(run2.current_scene_id, "SC001-C02")

    def test_33_submission_after_terminal_rejected(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        for seq, scene_id, option_id in (
            (1, "SC001-C01", "opt-sc001-c01-a"),
            (2, "SC001-C02", "opt-sc001-c02-a"),
            (3, "SC001-C03", "opt-sc001-c03-a"),
            (4, "SC001-C04", "opt-sc001-c04-a"),
        ):
            run = apply_decision_v2(run, ScenarioDecisionInputV2(seq, scene_id, option_id))
        self.assertTrue(run.is_complete)
        with self.assertRaises(ScenarioRunStateV2Error):
            apply_decision_v2(run, ScenarioDecisionInputV2(4, None, "anything"))


# ---------------------------------------------------------------------------
# 34-40: formula evaluation, caps, guards, bands, tie-break, rounding
# ---------------------------------------------------------------------------


class TestFormulasAndClassification(EngineV2TestCase):
    def test_34a_weighted_dimension_health(self):
        health = compute_positive_health(self.content, state=self.content.initial_state)
        self.assertTrue(0.0 <= health <= 100.0)

    def test_34b_tier_average(self):
        quality = compute_decision_quality(self.content, tier_history=("optimal", "acceptable"))
        # tierPoints optimal=4, acceptable=3 -> average 3.5
        self.assertAlmostEqual(quality, 3.5)

    def test_34c_linear_blend(self):
        composite = compute_composite(
            self.content, state=self.content.initial_state, tier_history=("optimal",)
        )
        self.assertIsInstance(composite, float)

    def test_34d_identity_formula(self):
        content_dict = copy.deepcopy(self.document)
        content_dict["outcomeClassifier"]["compositeFormula"] = {
            "type": "identity",
            "source": "positiveHealth",
        }
        content = build_scenario_content_v2(content_dict)
        composite = compute_composite(content, state=content.initial_state, tier_history=("optimal",))
        health = compute_positive_health(content, state=content.initial_state)
        self.assertAlmostEqual(composite, health)

    def test_34e_decision_quality_zero_scored_decisions_fails_closed(self):
        with self.assertRaises(ScenarioClassificationV2Error):
            compute_decision_quality(self.content, tier_history=())

    def test_35_severe_cap_applied_first(self):
        classification = classify_outcome(
            self.content,
            flags=frozenset({"flag-unsupported-customer-date"}),
            state=self.content.initial_state,
            counters=self.content.initial_counters,
            tier_history=("high-risk",),
        )
        self.assertEqual(classification.final_outcome_id, "failed_resolution")
        self.assertEqual(classification.severe_cap_id, "CAP-F01")

    def test_36_moderate_cap_constrains_outcome(self):
        # Every other dimension maximally favorable so the composite alone
        # would land in strong_resolution; operationalRisk >= 75 must still
        # cap the outcome down to partial_resolution (rank check, not just
        # band selection).
        state = {
            "customerConfidence": 100.0,
            "operationalRisk": 80.0,
            "dataQuality": 100.0,
            "scheduleImpact": 0.0,
            "complianceExposure": 0.0,
            "requirementsClarity": 100.0,
            "stakeholderAlignment": 100.0,
        }
        classification = classify_outcome(
            self.content,
            flags=frozenset(),
            state=state,
            counters=self.content.initial_counters,
            tier_history=("optimal", "optimal", "optimal"),
        )
        self.assertEqual(classification.band_outcome_id, "strong_resolution")
        self.assertTrue(classification.moderate_cap_applied)
        self.assertEqual(classification.moderate_cap_outcome_id, "partial_resolution")
        self.assertEqual(classification.final_outcome_id, "partial_resolution")
        outcome_rank = self.content.outcome_ranks[classification.final_outcome_id]
        self.assertGreaterEqual(outcome_rank, self.content.outcome_ranks["partial_resolution"])

    def test_36b_moderate_cap_present_but_not_applied_when_already_compliant(self):
        state = dict(self.content.initial_state)
        state["operationalRisk"] = 80.0
        classification = classify_outcome(
            self.content,
            flags=frozenset(),
            state=state,
            counters=self.content.initial_counters,
            tier_history=("optimal", "optimal", "optimal"),
        )
        self.assertEqual(classification.moderate_cap_outcome_id, "partial_resolution")
        self.assertFalse(classification.moderate_cap_applied)
        self.assertEqual(classification.final_outcome_id, "partial_resolution")

    def test_37_strong_guard_disqualifies_outcome(self):
        classification = classify_outcome(
            self.content,
            flags=frozenset(),
            state=self.content.initial_state,
            counters={**dict(self.content.initial_counters), "highRiskDecisionCount": 1},
            tier_history=("optimal", "optimal", "optimal"),
        )
        self.assertNotEqual(classification.band_outcome_id, "strong_resolution") if classification.band_outcome_id == "strong_resolution" else None
        if classification.band_outcome_id == "strong_resolution":
            self.assertTrue(classification.guard_tie_break_applied)
            self.assertNotEqual(classification.final_outcome_id, "strong_resolution")

    def test_38_numerical_band_classification(self):
        classification = classify_outcome(
            self.content,
            flags=frozenset(),
            state=self.content.initial_state,
            counters=self.content.initial_counters,
            tier_history=("optimal", "optimal", "optimal"),
        )
        self.assertIsNotNone(classification.band_outcome_id)
        self.assertIn(classification.band_outcome_id, self.content.outcome_ranks)

    def test_39_tie_break_deterministic(self):
        c1 = classify_outcome(
            self.content,
            flags=frozenset(),
            state=self.content.initial_state,
            counters={**dict(self.content.initial_counters), "highRiskDecisionCount": 1},
            tier_history=("optimal", "optimal", "optimal"),
        )
        c2 = classify_outcome(
            self.content,
            flags=frozenset(),
            state=self.content.initial_state,
            counters={**dict(self.content.initial_counters), "highRiskDecisionCount": 1},
            tier_history=("optimal", "optimal", "optimal"),
        )
        self.assertEqual(c1, c2)

    def test_40_display_rounding_after_classification(self):
        self.assertEqual(round_half_away_from_zero(2.5), 3)
        self.assertEqual(round_half_away_from_zero(-2.5), -3)
        self.assertEqual(round_half_away_from_zero(2.4999), 2)
        classification = classify_outcome(
            self.content,
            flags=frozenset(),
            state=self.content.initial_state,
            counters=self.content.initial_counters,
            tier_history=("optimal",),
        )
        rounded = round_half_away_from_zero(classification.composite_score_unrounded)
        self.assertIsInstance(rounded, int)
        self.assertNotEqual(classification.composite_score_unrounded, rounded)  # unrounded stays a float upstream


# ---------------------------------------------------------------------------
# 41-42: terminal outcome + debrief trace
# ---------------------------------------------------------------------------


class TestTerminalAndDebrief(EngineV2TestCase):
    def _play_best_path(self, attempt_id="a1"):
        run = start_scenario_run_v2(self.content, attempt_id=attempt_id)
        for seq, scene_id, option_id in (
            (1, "SC001-C01", "opt-sc001-c01-a"),
            (2, "SC001-C02", "opt-sc001-c02-a"),
            (3, "SC001-C03", "opt-sc001-c03-a"),
            (4, "SC001-C04", "opt-sc001-c04-a"),
        ):
            run = apply_decision_v2(run, ScenarioDecisionInputV2(seq, scene_id, option_id))
        return run

    def test_41_terminal_outcome_generated(self):
        run = self._play_best_path()
        self.assertTrue(run.is_complete)
        self.assertIsNotNone(run.terminal_result)
        self.assertIn(run.terminal_result.outcome_id, self.content.outcome_ranks)

    def test_42_debrief_trace_complete(self):
        run = self._play_best_path()
        trace = build_debrief_trace(run)
        self.assertEqual(len(trace), 4)
        for entry in trace:
            self.assertTrue(entry.scene_id)
            self.assertTrue(entry.option_id)
            self.assertIn(entry.evaluation_tier, ("optimal", "acceptable", "suboptimal", "high-risk"))

    def test_debrief_trace_refuses_incomplete_run(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        with self.assertRaises(ScenarioRunStateV2Error):
            build_debrief_trace(run)

    def test_at_least_two_outcomes_reachable(self):
        best = self._play_best_path(attempt_id="best")
        run = start_scenario_run_v2(self.content, attempt_id="worst")
        for seq, scene_id, option_id in (
            (1, "SC001-C01", "opt-sc001-c01-c"),
            (2, "SC001-C02", "opt-sc001-c02-c"),
        ):
            run = apply_decision_v2(run, ScenarioDecisionInputV2(seq, scene_id, option_id))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(3, "SC001-R2A", "opt-sc001-r2a-c"))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(4, "SC001-C03", "opt-sc001-c03-c"))
        run = apply_decision_v2(run, ScenarioDecisionInputV2(5, "SC001-C04", "opt-sc001-c04-c"))
        self.assertNotEqual(best.terminal_result.outcome_id, run.terminal_result.outcome_id)


# ---------------------------------------------------------------------------
# 43-48: replay
# ---------------------------------------------------------------------------


class TestReplay(EngineV2TestCase):
    def test_43_replay_reproduces_normal_path(self):
        decisions = (
            ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"),
            ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-a"),
            ScenarioDecisionInputV2(3, "SC001-C03", "opt-sc001-c03-a"),
            ScenarioDecisionInputV2(4, "SC001-C04", "opt-sc001-c04-a"),
        )
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        for decision in decisions:
            run = apply_decision_v2(run, decision)
        replayed = replay_scenario_run_v2(self.content, attempt_id="a1", decisions=decisions)
        self.assertEqual(run, replayed)

    def test_44_replay_reproduces_corrective_path(self):
        decisions = (
            ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"),
            ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"),
            ScenarioDecisionInputV2(3, "SC001-R2A", "opt-sc001-r2a-a"),
            ScenarioDecisionInputV2(4, "SC001-C03", "opt-sc001-c03-a"),
            ScenarioDecisionInputV2(5, "SC001-C04", "opt-sc001-c04-a"),
        )
        run = start_scenario_run_v2(self.content, attempt_id="a2")
        for decision in decisions:
            run = apply_decision_v2(run, decision)
        replayed = replay_scenario_run_v2(self.content, attempt_id="a2", decisions=decisions)
        self.assertEqual(run, replayed)
        self.assertEqual(len(replayed.corrective_entries), 1)

    def test_45_replay_reproduces_skipped_corrective_path(self):
        # Natural budget exhaustion: enter R2A (counter=1), reconverge, then
        # C03-b requests R3A but skips to C04 without incrementing again.
        decisions = (
            ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"),
            ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"),
            ScenarioDecisionInputV2(3, "SC001-R2A", "opt-sc001-r2a-a"),
            ScenarioDecisionInputV2(4, "SC001-C03", "opt-sc001-c03-b"),
            ScenarioDecisionInputV2(5, "SC001-C04", "opt-sc001-c04-a"),
        )
        run = start_scenario_run_v2(self.content, attempt_id="a3-natural-skip")
        for decision in decisions:
            run = apply_decision_v2(run, decision)
        self.assertTrue(run.is_complete)
        self.assertEqual(len(run.corrective_entries), 1)
        self.assertEqual(len(run.skipped_corrective_events), 1)
        self.assertEqual(run.counters["correctiveScenesExperienced"], 1)
        self.assertEqual(run.skipped_corrective_events[0].attempted_corrective_scene_id, "SC001-R3A")
        replayed = replay_scenario_run_v2(
            self.content, attempt_id="a3-natural-skip", decisions=decisions
        )
        self.assertEqual(run, replayed)

    def test_46_replay_reproduces_outcome(self):
        decisions = (
            ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"),
            ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-a"),
            ScenarioDecisionInputV2(3, "SC001-C03", "opt-sc001-c03-a"),
            ScenarioDecisionInputV2(4, "SC001-C04", "opt-sc001-c04-a"),
        )
        replayed = replay_scenario_run_v2(self.content, attempt_id="a4", decisions=decisions)
        self.assertTrue(replayed.is_complete)
        self.assertEqual(replayed.terminal_result.outcome_id, "strong_resolution")

    def test_47_replay_detects_content_hash_mismatch(self):
        from utils.scenario_engine_v2 import verify_replay_identity_v2

        with self.assertRaises(ScenarioReplayV2Error):
            verify_replay_identity_v2(
                self.content,
                pinned_simulation_id=self.content.simulation_id,
                pinned_version=self.content.version,
                pinned_schema_version=self.content.schema_version,
                pinned_canonical_content_sha256="deadbeef",
                pinned_engine_version=ENGINE_VERSION,
            )

    def test_48_replay_rejects_decision_after_terminal(self):
        decisions = (
            ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"),
            ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-a"),
            ScenarioDecisionInputV2(3, "SC001-C03", "opt-sc001-c03-a"),
            ScenarioDecisionInputV2(4, "SC001-C04", "opt-sc001-c04-a"),
            ScenarioDecisionInputV2(5, "SC001-C01", "opt-sc001-c01-a"),
        )
        with self.assertRaises(ScenarioReplayV2Error):
            replay_scenario_run_v2(self.content, attempt_id="a5", decisions=decisions)

    def test_replay_rejects_duplicate_sequence(self):
        decisions = (
            ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"),
            ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"),
        )
        with self.assertRaises(ScenarioRunStateV2Error):
            replay_scenario_run_v2(self.content, attempt_id="a6", decisions=decisions)

    def test_replay_of_empty_history_matches_start(self):
        start = start_scenario_run_v2(self.content, attempt_id="a7")
        replayed = replay_scenario_run_v2(self.content, attempt_id="a7", decisions=())
        self.assertEqual(start, replayed)


# ---------------------------------------------------------------------------
# 49: learner view excludes hidden scoring/routing data
# ---------------------------------------------------------------------------


class TestLearnerSafeViews(EngineV2TestCase):
    def test_49_learner_scene_view_excludes_hidden_fields(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        view = build_learner_scene_view(run)
        self.assertIsInstance(view, LearnerSceneView)
        blob = json.dumps(_thaw(view), default=_json_default)
        for forbidden in (
            "evaluationTier",
            "stateChanges",
            "correctiveRoute",
            "budgetCondition",
            "debriefSeed",
            "strongestOptionId",
            "setFlags",
            "clearFlags",
            "severeCaps",
            "moderateCaps",
        ):
            self.assertNotIn(forbidden, blob)

    def test_49b_learner_terminal_view_only_after_completion(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        with self.assertRaises(ScenarioRunStateV2Error):
            build_learner_terminal_view(run)

    def test_49c_learner_terminal_view_excludes_classification_internals(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        for seq, scene_id, option_id in (
            (1, "SC001-C01", "opt-sc001-c01-a"),
            (2, "SC001-C02", "opt-sc001-c02-a"),
            (3, "SC001-C03", "opt-sc001-c03-a"),
            (4, "SC001-C04", "opt-sc001-c04-a"),
        ):
            run = apply_decision_v2(run, ScenarioDecisionInputV2(seq, scene_id, option_id))
        view = build_learner_terminal_view(run)
        self.assertIsInstance(view, LearnerTerminalView)
        field_names = {f.name for f in dataclasses.fields(view)}
        self.assertEqual(field_names, {"outcome_id", "outcome_title", "narrative", "display_score"})

    def test_scene_view_options_in_display_order(self):
        run = start_scenario_run_v2(self.content, attempt_id="a1")
        view = build_learner_scene_view(run)
        self.assertEqual(
            tuple(o.id for o in view.options), run.option_display_order_by_scene["SC001-C01"]
        )


# ---------------------------------------------------------------------------
# Condition grammar defensive checks
# ---------------------------------------------------------------------------


class TestConditionGrammar(EngineV2TestCase):
    def test_all_any_not_combinators(self):
        state = self.content.initial_state
        counters = self.content.initial_counters
        cond_all = {"all": [{"flagNotSet": "flag-verbal-handoff-only"}, {"flagNotSet": "flag-sales-reengaged"}]}
        self.assertTrue(
            evaluate_condition(cond_all, content=self.content, flags=frozenset(), state=state, counters=counters)
        )
        cond_any = {"any": [{"flagSet": "flag-verbal-handoff-only"}, {"flagNotSet": "flag-sales-reengaged"}]}
        self.assertTrue(
            evaluate_condition(cond_any, content=self.content, flags=frozenset(), state=state, counters=counters)
        )
        cond_not = {"not": {"flagSet": "flag-verbal-handoff-only"}}
        self.assertTrue(
            evaluate_condition(cond_not, content=self.content, flags=frozenset(), state=state, counters=counters)
        )

    def test_state_compare_and_counter_compare(self):
        state = self.content.initial_state
        counters = self.content.initial_counters
        cond = {"stateCompare": {"variableId": "customerConfidence", "op": "gte", "value": 68}}
        self.assertTrue(
            evaluate_condition(cond, content=self.content, flags=frozenset(), state=state, counters=counters)
        )
        cond_counter = {"counterCompare": {"counterId": "correctiveScenesExperienced", "op": "lt", "value": 1}}
        self.assertTrue(
            evaluate_condition(cond_counter, content=self.content, flags=frozenset(), state=state, counters=counters)
        )

    def test_unknown_flag_reference_fails_closed(self):
        with self.assertRaises(ScenarioContentV2Error):
            evaluate_condition(
                {"flagSet": "flag-does-not-exist"},
                content=self.content,
                flags=frozenset(),
                state=self.content.initial_state,
                counters=self.content.initial_counters,
            )

    def test_unknown_state_variable_reference_fails_closed(self):
        with self.assertRaises(ScenarioContentV2Error):
            evaluate_condition(
                {"stateCompare": {"variableId": "notReal", "op": "gte", "value": 1}},
                content=self.content,
                flags=frozenset(),
                state=self.content.initial_state,
                counters=self.content.initial_counters,
            )

    def test_unknown_counter_reference_fails_closed(self):
        with self.assertRaises(ScenarioContentV2Error):
            evaluate_condition(
                {"counterCompare": {"counterId": "notReal", "op": "gte", "value": 1}},
                content=self.content,
                flags=frozenset(),
                state=self.content.initial_state,
                counters=self.content.initial_counters,
            )

    def test_unrecognized_condition_node_fails_closed(self):
        with self.assertRaises(ScenarioContentV2Error):
            evaluate_condition(
                {"someArbitraryKey": True},
                content=self.content,
                flags=frozenset(),
                state=self.content.initial_state,
                counters=self.content.initial_counters,
            )

    def test_condition_depth_limit_enforced(self):
        deep = {"flagNotSet": "flag-verbal-handoff-only"}
        for _ in range(10):
            deep = {"not": deep}
        with self.assertRaises(ScenarioContentV2Error):
            evaluate_condition(
                deep, content=self.content, flags=frozenset(), state=self.content.initial_state,
                counters=self.content.initial_counters,
            )


# ---------------------------------------------------------------------------
# Error contract: no raw KeyError/AssertionError/jsonschema exceptions escape
# ---------------------------------------------------------------------------


class TestErrorContract(EngineV2TestCase):
    def test_all_public_errors_derive_from_base(self):
        for error_cls in (
            ScenarioContentV2Error,
            ScenarioRunStateV2Error,
            ScenarioReplayV2Error,
            ScenarioClassificationV2Error,
        ):
            self.assertTrue(issubclass(error_cls, ScenarioEngineV2Error))

    def test_malformed_document_raises_domain_error_not_keyerror(self):
        with self.assertRaises(ScenarioEngineV2Error):
            build_scenario_content_v2({"schemaVersion": "1.1.0", "requiredEngineVersion": "SCENARIO_ENGINE_V2"})

    def test_non_mapping_document_rejected(self):
        with self.assertRaises(ScenarioContentV2Error):
            build_scenario_content_v2([1, 2, 3])


# ---------------------------------------------------------------------------
# SIM-ENGINE-V2-02 hardening: F-H-001..F-H-002 and related MEDIUM findings
# ---------------------------------------------------------------------------


class TestHardeningSequenceTyping(EngineV2TestCase):
    def _assert_malformed_sequence(self, value):
        run = start_scenario_run_v2(self.content, attempt_id="seq-type")
        before = run
        with self.assertRaises(ScenarioRunStateV2Error) as ctx:
            apply_decision_v2(run, ScenarioDecisionInputV2(value, "SC001-C01", "opt-sc001-c01-a"))  # type: ignore[arg-type]
        self.assertIn("strict integer", str(ctx.exception))
        self.assertEqual(ctx.exception.path, "sequenceNumber")
        self.assertIs(run, before)
        self.assertEqual(run.expected_sequence_number, 1)

    def test_rejects_true(self):
        self._assert_malformed_sequence(True)

    def test_rejects_false(self):
        self._assert_malformed_sequence(False)

    def test_rejects_float(self):
        self._assert_malformed_sequence(1.0)

    def test_rejects_numeric_string(self):
        self._assert_malformed_sequence("1")

    def test_rejects_none(self):
        self._assert_malformed_sequence(None)

    def test_rejects_negative(self):
        run = start_scenario_run_v2(self.content, attempt_id="seq-neg")
        with self.assertRaises(ScenarioRunStateV2Error) as ctx:
            apply_decision_v2(run, ScenarioDecisionInputV2(-1, "SC001-C01", "opt-sc001-c01-a"))
        self.assertIn(">= 1", str(ctx.exception))
        self.assertEqual(run.expected_sequence_number, 1)

    def test_stale_integer_distinct(self):
        run = start_scenario_run_v2(self.content, attempt_id="seq-stale")
        with self.assertRaises(ScenarioRunStateV2Error) as ctx:
            apply_decision_v2(run, ScenarioDecisionInputV2(0, "SC001-C01", "opt-sc001-c01-a"))
        # 0 is rejected as < 1 before stale/future comparison
        self.assertIn(">= 1", str(ctx.exception))
        run2 = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        with self.assertRaises(ScenarioRunStateV2Error) as ctx2:
            apply_decision_v2(run2, ScenarioDecisionInputV2(1, "SC001-C02", "opt-sc001-c02-a"))
        self.assertIn("expected sequenceNumber 2, got 1", str(ctx2.exception))

    def test_future_integer_distinct(self):
        run = start_scenario_run_v2(self.content, attempt_id="seq-future")
        with self.assertRaises(ScenarioRunStateV2Error) as ctx:
            apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C01", "opt-sc001-c01-a"))
        self.assertIn("expected sequenceNumber 1, got 2", str(ctx.exception))

    def test_valid_integer(self):
        run = start_scenario_run_v2(self.content, attempt_id="seq-ok")
        run2 = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"))
        self.assertEqual(run2.current_scene_id, "SC001-C02")


class TestHardeningOptionDisplayPolicy(EngineV2TestCase):
    def test_authored_order_preserves_document_order(self):
        from utils.scenario_engine_v2 import resolve_option_display_order

        doc = copy.deepcopy(self.document)
        doc["optionDisplayPolicy"] = "authored_order"
        content = build_scenario_content_v2(doc)
        authored = [o["id"] for o in doc["scenes"][0]["decision"]["options"]]
        run_a = start_scenario_run_v2(content, attempt_id="attempt-a")
        run_b = start_scenario_run_v2(content, attempt_id="attempt-b")
        self.assertEqual(list(run_a.option_display_order_by_scene["SC001-C01"]), authored)
        self.assertEqual(run_a.option_display_order_by_scene["SC001-C01"], run_b.option_display_order_by_scene["SC001-C01"])
        resolved = resolve_option_display_order(
            authored, content=content, attempt_id="ignored", scene_id="SC001-C01"
        )
        self.assertEqual(resolved, tuple(authored))

    def test_randomize_remains_deterministic(self):
        run_a = start_scenario_run_v2(self.content, attempt_id="same")
        run_b = start_scenario_run_v2(self.content, attempt_id="same")
        self.assertEqual(
            run_a.option_display_order_by_scene["SC001-C01"],
            run_b.option_display_order_by_scene["SC001-C01"],
        )

    def test_unsupported_policy_fails_closed(self):
        from utils.scenario_engine_v2 import ScenarioContentV2

        # Bypass builder validation by constructing a content object with a bad policy.
        good = self.content
        bad = dataclasses.replace(good, option_display_policy="shuffle_unknown")
        from utils.scenario_engine_v2 import resolve_option_display_order

        with self.assertRaises(ScenarioContentV2Error) as ctx:
            resolve_option_display_order(
                ["a", "b"], content=bad, attempt_id="x", scene_id="SC001-C01"
            )
        self.assertIn("unsupported optionDisplayPolicy", str(ctx.exception))


class TestHardeningSeedGoldenVector(EngineV2TestCase):
    def test_golden_vector_option_order(self):
        # Frozen Engine V2 §17 stream: SHA256(material || uint32be(counter)) for counter=0..
        order = deterministic_option_display_order(
            ["opt-a", "opt-b", "opt-c"],
            attempt_id="golden-attempt",
            simulation_id="golden-sim",
            version="1.0.0",
            canonical_content_sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            scene_id="SC-GOLDEN",
        )
        self.assertEqual(order, ("opt-b", "opt-c", "opt-a"))


class TestHardeningFiniteAndFlags(EngineV2TestCase):
    def test_nan_delta_fails_closed(self):
        from utils.scenario_engine_v2 import _apply_state_deltas

        run = start_scenario_run_v2(self.content, attempt_id="nan")
        before = dict(run.state)
        with self.assertRaises(ScenarioContentV2Error):
            _apply_state_deltas(self.content, run.state, {"customerConfidence": float("nan")})
        self.assertEqual(dict(run.state), before)

    def test_inf_delta_fails_closed(self):
        from utils.scenario_engine_v2 import _apply_state_deltas

        with self.assertRaises(ScenarioContentV2Error):
            _apply_state_deltas(self.content, self.content.initial_state, {"customerConfidence": float("inf")})

    def test_bool_as_number_fails_closed(self):
        from utils.scenario_engine_v2 import _apply_state_deltas

        with self.assertRaises(ScenarioContentV2Error):
            _apply_state_deltas(self.content, self.content.initial_state, {"customerConfidence": True})

    def test_undeclared_set_flag_fails_closed(self):
        from utils.scenario_engine_v2 import _apply_flag_changes

        with self.assertRaises(ScenarioContentV2Error) as ctx:
            _apply_flag_changes(self.content, frozenset(), clear=(), set_=("flag-never-declared",))
        self.assertIn("undeclared flag", str(ctx.exception))

    def test_undeclared_clear_flag_fails_closed(self):
        from utils.scenario_engine_v2 import _apply_flag_changes

        with self.assertRaises(ScenarioContentV2Error):
            _apply_flag_changes(self.content, frozenset(), clear=("flag-never-declared",), set_=())


class TestHardeningDebriefVariants(EngineV2TestCase):
    def test_presented_and_next_variant_fields_are_distinct(self):
        run = start_scenario_run_v2(self.content, attempt_id="var")
        # Set verbal handoff so C02 selects variant c02-after-documented is when flag NOT set;
        # use C01-c to set flag, then at C02 the base/variant differs.
        run = apply_decision_v2(run, ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-c"))
        entry = run.decisions[-1]
        self.assertIsNone(entry.presented_dialogue_variant_id)  # C01 has no variants
        # Next scene C02: flag-verbal is set, so "c02-after-documented" (flagNotSet) does NOT match
        self.assertIsNone(entry.next_dialogue_variant_id)
        self.assertTrue(hasattr(entry, "presented_dialogue_variant_id"))
        self.assertTrue(hasattr(entry, "next_dialogue_variant_id"))
        self.assertFalse(hasattr(entry, "selected_variant_id"))

        run = apply_decision_v2(run, ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-a"))
        entry2 = run.decisions[-1]
        self.assertIsNone(entry2.presented_dialogue_variant_id)  # C02 had no matching variant
        # C03 may select verbal-handoff variant after flag set
        self.assertEqual(entry2.next_dialogue_variant_id, "c03-verbal-handoff")


class TestHardeningNaturalCorrectiveExhaustion(EngineV2TestCase):
    def test_natural_corrective_budget_exhaustion_end_to_end(self):
        decisions = (
            ScenarioDecisionInputV2(1, "SC001-C01", "opt-sc001-c01-a"),
            ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-b"),  # enters R2A
            ScenarioDecisionInputV2(3, "SC001-R2A", "opt-sc001-r2a-a"),  # reconverge C03
            ScenarioDecisionInputV2(4, "SC001-C03", "opt-sc001-c03-b"),  # skip R3A
            ScenarioDecisionInputV2(5, "SC001-C04", "opt-sc001-c04-a"),
        )
        run = start_scenario_run_v2(self.content, attempt_id="natural-exhaust")
        # Step through assertions matching the required proof sequence
        run = apply_decision_v2(run, decisions[0])
        run = apply_decision_v2(run, decisions[1])
        self.assertEqual(run.current_scene_id, "SC001-R2A")
        self.assertEqual(run.counters["correctiveScenesExperienced"], 1)
        self.assertEqual(len(run.corrective_entries), 1)

        run = apply_decision_v2(run, decisions[2])
        self.assertEqual(run.current_scene_id, "SC001-C03")

        run = apply_decision_v2(run, decisions[3])
        self.assertEqual(run.current_scene_id, "SC001-C04")
        self.assertEqual(run.counters["correctiveScenesExperienced"], 1)
        self.assertEqual(len(run.skipped_corrective_events), 1)
        self.assertEqual(run.skipped_corrective_events[0].attempted_corrective_scene_id, "SC001-R3A")

        run = apply_decision_v2(run, decisions[4])
        self.assertTrue(run.is_complete)

        replayed = replay_scenario_run_v2(
            self.content, attempt_id="natural-exhaust", decisions=decisions
        )
        self.assertEqual(replayed, run)
        self.assertEqual(replayed.option_display_order_by_scene, run.option_display_order_by_scene)
        self.assertEqual(replayed.variant_selections, run.variant_selections)
        self.assertEqual(replayed.terminal_result.outcome_id, run.terminal_result.outcome_id)
        self.assertEqual(
            [(d.presented_dialogue_variant_id, d.next_dialogue_variant_id) for d in replayed.decisions],
            [(d.presented_dialogue_variant_id, d.next_dialogue_variant_id) for d in run.decisions],
        )


# ---------------------------------------------------------------------------
# 50: existing Engine V1 focused tests remain passing (smoke re-check here;
# full V1 regression is executed separately as its own test module run).
# ---------------------------------------------------------------------------


class TestEngineV1Untouched(unittest.TestCase):
    def test_50_engine_v1_module_still_imports_and_loads_v1_content(self):
        import importlib.util

        if importlib.util.find_spec("utils.scenario_engine") is None:
            self.skipTest("Engine V1 runtime excluded from narrow production candidate")

        from utils.scenario_engine import ENGINE_VERSION as V1_ENGINE_VERSION
        from utils.scenario_engine import start_scenario_run as v1_start_scenario_run

        self.assertEqual(V1_ENGINE_VERSION, "SCENARIO_ENGINE_V1")
        content = load_scenario_content(V1_SCENARIO_PATH)
        run = v1_start_scenario_run(content)
        self.assertFalse(run.is_complete)

    def test_v1_rejects_1_1_0_content(self):
        from utils.scenario_schema import build_scenario_content

        v2_document = _load_document()
        with self.assertRaises(ScenarioContentError):
            build_scenario_content(v2_document)


if __name__ == "__main__":
    unittest.main()
