"""Tests for deterministic scenario execution, replay, and immutability."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Mapping, Sequence
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_engine import (
    ENGINE_VERSION,
    ScenarioDecisionInput,
    ScenarioEngineError,
    ScenarioReplayIdentityError,
    ScenarioRunStateError,
    apply_decision,
    apply_state_changes,
    build_terminal_result,
    decision_history,
    deserialize_decision_history,
    ending_matches_state,
    evaluate_ending,
    get_current_scene,
    replay_matches_run,
    replay_scenario_run,
    replay_serialized_run,
    resume_scenario_run,
    serialize_run_snapshot,
    serialize_terminal_result,
    start_scenario_run,
)
from utils.scenario_schema import (
    ScenarioContent,
    ScenarioDecision,
    ScenarioDomain,
    ScenarioEnding,
    ScenarioGraphMetadata,
    ScenarioOption,
    ScenarioScene,
    ScenarioStateVariable,
    ScenarioStructureCounts,
    TERMINAL_SENTINEL,
    load_scenario_content,
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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _pick_option(scene: ScenarioScene, *, correct: bool, first_incorrect: bool = True) -> ScenarioOption:
    if correct:
        return next(option for option in scene.decision.options if option.is_correct)
    incorrect_options = [option for option in scene.decision.options if not option.is_correct]
    index = 0 if first_incorrect else 1
    return incorrect_options[index]


def _playthrough(content: ScenarioContent, *, correct: bool, first_incorrect: bool = True):
    run = start_scenario_run(content)
    while not run.is_complete:
        scene = get_current_scene(run)
        option = _pick_option(scene, correct=correct, first_incorrect=first_incorrect)
        run = apply_decision(run, option.id)
    assert run.terminal_result is not None
    return run


def _make_scenario_content(
    *,
    simulation_id: str,
    scenes: Sequence[ScenarioScene],
    start_scene: str,
    endings: Sequence[ScenarioEnding],
    state_variables: Sequence[ScenarioStateVariable],
    initial_state: Mapping[str, float],
    domains: Sequence[ScenarioDomain] | None = None,
    version: str = "1.0.0",
) -> ScenarioContent:
    """Build a minimal, directly-constructed ScenarioContent for engine-only
    unit tests. Bypasses JSON schema validation entirely (the engine never
    performs schema validation itself — that is scenario_schema.py's job,
    exercised by tests/test_scenario_schema.py), so this is only suitable
    for engine-level tests, not content-authoring regression tests.
    """
    scenes_tuple = tuple(scenes)
    endings_tuple = tuple(endings)
    domains_tuple = (
        tuple(domains) if domains is not None else (ScenarioDomain(id="d1", label="Domain", weight="100%"),)
    )
    choice_count = sum(len(scene.decision.options) for scene in scenes_tuple)
    detour_count = sum(1 for scene in scenes_tuple if scene.is_detour)
    return ScenarioContent(
        simulation_id=simulation_id,
        version=version,
        schema_version="1.0.0",
        certification_exam_name="Synthetic",
        exam_code="SYN-000",
        title="Synthetic",
        description=None,
        estimated_minutes=None,
        domains=domains_tuple,
        state_variables=tuple(state_variables),
        initial_state=dict(initial_state),
        scenes=scenes_tuple,
        start_scene=start_scene,
        endings=endings_tuple,
        graph_metadata=ScenarioGraphMetadata(len(scenes_tuple), len(scenes_tuple), (), 1, len(scenes_tuple)),
        structure_counts=ScenarioStructureCounts(choice_count, detour_count, len(domains_tuple), len(endings_tuple)),
        canonical_content_sha256=f"synthetic-{simulation_id}",
        source_path=None,
    )


def _linear_synthetic_content(
    scene_count: int,
    *,
    state_key: str = "score",
    minimum: float = 0.0,
    maximum: float = 100.0,
    initial_value: float = 50.0,
    shuffle: bool = False,
    simulation_id: str = "synthetic-linear",
) -> ScenarioContent:
    """A trivial N-scene chain: each scene has a correct option (+2) and an
    incorrect option (-3), both converging on the same next scene. Used to
    exercise partial/complete replay and clamping bounds generically,
    without hardcoding any particular state-variable name or scene count.
    """
    scenes: list[ScenarioScene] = []
    for index in range(scene_count):
        scene_id = f"s{index + 1}"
        next_scene = f"s{index + 2}" if index + 1 < scene_count else TERMINAL_SENTINEL
        scenes.append(
            ScenarioScene(
                id=scene_id,
                domain_id="d1",
                narrative="n",
                decision=ScenarioDecision(
                    prompt="p",
                    decision_type="single_select",
                    options=(
                        ScenarioOption(
                            id="A",
                            text="correct",
                            is_correct=True,
                            feedback="f",
                            next_scene=next_scene,
                            state_changes={state_key: 2.0},
                            set_flags=(),
                        ),
                        ScenarioOption(
                            id="B",
                            text="incorrect",
                            is_correct=False,
                            feedback="f2",
                            next_scene=next_scene,
                            state_changes={state_key: -3.0},
                            set_flags=(f"wrong_{scene_id}",),
                        ),
                    ),
                ),
            )
        )
    ordered_scenes = tuple(reversed(scenes)) if shuffle else tuple(scenes)
    endings = (
        ScenarioEnding(
            id="ending_pass",
            condition={f"{state_key}Min": -1000},
            narrative="pass",
            score_band="Pass",
            recommended_review=(),
        ),
    )
    return _make_scenario_content(
        simulation_id=f"{simulation_id}-{scene_count}",
        scenes=ordered_scenes,
        start_scene="s1",
        endings=endings,
        state_variables=(ScenarioStateVariable(key=state_key, minimum=minimum, maximum=maximum),),
        initial_state={state_key: initial_value},
    )


def _reconverging_synthetic_content() -> ScenarioContent:
    """s1 branches to s2a (via A) or s2b (via B); both reconverge at s3."""
    scenes = (
        ScenarioScene(
            id="s1",
            domain_id="d1",
            narrative="n",
            decision=ScenarioDecision(
                prompt="p",
                decision_type="single_select",
                options=(
                    ScenarioOption(
                        id="A", text="a", is_correct=True, feedback="f",
                        next_scene="s2a", state_changes={"score": 5.0}, set_flags=(),
                    ),
                    ScenarioOption(
                        id="B", text="b", is_correct=False, feedback="f",
                        next_scene="s2b", state_changes={"score": -5.0}, set_flags=(),
                    ),
                ),
            ),
        ),
        ScenarioScene(
            id="s2a",
            domain_id="d1",
            narrative="n",
            decision=ScenarioDecision(
                prompt="p",
                decision_type="single_select",
                options=(
                    ScenarioOption(
                        id="X", text="x", is_correct=True, feedback="f",
                        next_scene="s3", state_changes={"score": 1.0}, set_flags=(),
                    ),
                ),
            ),
        ),
        ScenarioScene(
            id="s2b",
            domain_id="d1",
            narrative="n",
            decision=ScenarioDecision(
                prompt="p",
                decision_type="single_select",
                options=(
                    ScenarioOption(
                        id="Y", text="y", is_correct=True, feedback="f",
                        next_scene="s3", state_changes={"score": 1.0}, set_flags=(),
                    ),
                ),
            ),
        ),
        ScenarioScene(
            id="s3",
            domain_id="d1",
            narrative="n",
            decision=ScenarioDecision(
                prompt="p",
                decision_type="single_select",
                options=(
                    ScenarioOption(
                        id="Z", text="z", is_correct=True, feedback="f",
                        next_scene=TERMINAL_SENTINEL, state_changes={"score": 1.0}, set_flags=(),
                    ),
                ),
            ),
        ),
    )
    endings = (
        ScenarioEnding(id="ending_pass", condition={"scoreMin": -1000}, narrative="p", score_band="Pass", recommended_review=()),
    )
    return _make_scenario_content(
        simulation_id="synthetic-reconverge",
        scenes=scenes,
        start_scene="s1",
        endings=endings,
        state_variables=(ScenarioStateVariable(key="score", minimum=-1000.0, maximum=1000.0),),
        initial_state={"score": 0.0},
    )


def _content_with_endings(endings: Sequence[ScenarioEnding]) -> ScenarioContent:
    base = _linear_synthetic_content(1, state_key="score")
    return _make_scenario_content(
        simulation_id="synthetic-endings-override",
        scenes=base.scenes,
        start_scene=base.start_scene,
        endings=endings,
        state_variables=base.state_variables,
        initial_state=base.initial_state,
    )


class TestScenarioEngineFoundation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = load_scenario_content(BA_SCENARIO_PATH)

    # -- Core start/step behavior -----------------------------------------

    def test_start_run_initializes_at_start_scene(self) -> None:
        run = start_scenario_run(self.content)
        self.assertEqual(run.current_scene_id, "s01_kickoff")
        self.assertFalse(run.is_complete)
        self.assertEqual(dict(run.state), dict(self.content.initial_state))
        self.assertEqual(run.flags, ())
        self.assertEqual(run.decisions, ())

    def test_get_current_scene_resolves_by_explicit_scene_id(self) -> None:
        run = start_scenario_run(self.content)
        scene = get_current_scene(run)
        self.assertEqual(scene.id, "s01_kickoff")

    def test_apply_decision_rejects_invalid_option(self) -> None:
        run = start_scenario_run(self.content)
        with self.assertRaises(ScenarioRunStateError):
            apply_decision(run, "ZZ")

    def test_apply_decision_rejects_after_completion(self) -> None:
        run = _playthrough(self.content, correct=True)
        with self.assertRaises(ScenarioRunStateError):
            apply_decision(run, "A")

    def test_per_update_clamping_is_applied_immediately(self) -> None:
        variables = {
            "score": ScenarioStateVariable(key="score", minimum=0.0, maximum=100.0),
        }
        state = apply_state_changes({"score": 95.0}, {"score": 10.0}, variables=variables)
        self.assertEqual(state["score"], 100.0)
        state = apply_state_changes(state, {"score": -30.0}, variables=variables)
        self.assertEqual(state["score"], 70.0)

        undershoot = apply_state_changes({"score": 5.0}, {"score": -20.0}, variables=variables)
        self.assertEqual(undershoot["score"], 0.0)

    def test_domain_performance_is_tracked(self) -> None:
        run = _playthrough(self.content, correct=True)
        performance = {entry.domain_id: entry for entry in run.terminal_result.domain_performance}
        self.assertGreater(len(performance), 0)
        self.assertEqual(performance["d1"].correct_count, performance["d1"].total_count)

    def test_flags_are_accumulated(self) -> None:
        run = _playthrough(self.content, correct=False, first_incorrect=True)
        self.assertIn("skipped_validation", run.terminal_result.flags)

    # -- BA-201 calibration (must remain unchanged) ------------------------

    def test_all_correct_ba201_calibration(self) -> None:
        run = _playthrough(self.content, correct=True)
        self.assertEqual(len(run.decisions), 24)
        self.assertEqual(run.state["projectHealth"], 100)
        self.assertEqual(run.terminal_result.ending_id, "ending_distinction")
        self.assertEqual(run.terminal_result.engine_version, ENGINE_VERSION)
        self.assertEqual(
            run.terminal_result.canonical_content_sha256,
            self.content.canonical_content_sha256,
        )

    def test_canonical_all_incorrect_ba201_calibration(self) -> None:
        run = _playthrough(self.content, correct=False, first_incorrect=True)
        self.assertEqual(len(run.decisions), 25)
        self.assertEqual(run.state["projectHealth"], 19)
        self.assertEqual(run.terminal_result.ending_id, "ending_fail")

    # -- Ending precedence / operator hardening ---------------------------

    def test_ending_array_order_precedence(self) -> None:
        endings = (
            ScenarioEnding(
                id="ending_distinction",
                condition={"projectHealthMin": 85},
                narrative="distinction",
                score_band="Pass with Distinction",
                recommended_review=(),
            ),
            ScenarioEnding(
                id="ending_pass",
                condition={"projectHealthMin": 60},
                narrative="pass",
                score_band="Pass",
                recommended_review=(),
            ),
            ScenarioEnding(
                id="ending_fail",
                condition={"projectHealthMin": -1000},
                narrative="fail",
                score_band="Fail",
                recommended_review=(),
            ),
        )
        base = _linear_synthetic_content(1, state_key="projectHealth", minimum=0, maximum=100, initial_value=70)
        content = _make_scenario_content(
            simulation_id="synthetic-order",
            scenes=base.scenes,
            start_scene=base.start_scene,
            endings=endings,
            state_variables=base.state_variables,
            initial_state={"projectHealth": 70},
        )
        state = {"projectHealth": 70}
        ending = evaluate_ending(content, state)
        self.assertEqual(ending.id, "ending_pass")
        self.assertFalse(ending_matches_state(endings[0], state))
        self.assertTrue(ending_matches_state(endings[1], state))
        self.assertTrue(ending_matches_state(endings[2], state))

    def test_ending_condition_rejects_unsupported_operator(self) -> None:
        content = _content_with_endings((
            ScenarioEnding(id="e1", condition={"scoreWeird": 1}, narrative="n", score_band="b", recommended_review=()),
        ))
        with self.assertRaises(ScenarioEngineError):
            evaluate_ending(content, {"score": 0.0})

    def test_ending_condition_rejects_missing_referenced_variable(self) -> None:
        content = _content_with_endings((
            ScenarioEnding(id="e1", condition={"otherMin": 1}, narrative="n", score_band="b", recommended_review=()),
        ))
        with self.assertRaises(ScenarioEngineError):
            evaluate_ending(content, {"score": 0.0})

    # ======================================================================
    # 1. Replay contract — replay_scenario_run is the general primitive
    # ======================================================================

    def test_replay_scenario_run_with_empty_history_equals_start(self) -> None:
        replayed = replay_scenario_run(self.content, ())
        fresh = start_scenario_run(self.content)
        self.assertEqual(replayed, fresh)

    def test_replay_scenario_run_partial_history_is_incomplete_and_resumable(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        partial_history = decision_history(full_run)[:5]
        replayed = replay_scenario_run(self.content, partial_history)

        self.assertFalse(replayed.is_complete)
        self.assertIsNone(replayed.terminal_result)
        self.assertEqual(len(replayed.decisions), 5)

        continued = replayed
        for decision in full_run.decisions[5:]:
            continued = apply_decision(continued, decision.option_id)
        self.assertTrue(continued.is_complete)
        self.assertEqual(continued.terminal_result, full_run.terminal_result)

    def test_replay_scenario_run_complete_history_equals_interactive_execution(self) -> None:
        full_run = _playthrough(self.content, correct=False, first_incorrect=True)
        replayed = replay_scenario_run(self.content, decision_history(full_run))
        self.assertEqual(replayed, full_run)

    def test_resume_scenario_run_is_a_thin_wrapper_around_replay(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        history = decision_history(full_run)[:5]
        with mock.patch(
            "utils.scenario_engine.replay_scenario_run", wraps=replay_scenario_run
        ) as spy:
            result = resume_scenario_run(self.content, history)
        spy.assert_called_once_with(self.content, history)
        self.assertEqual(result, replay_scenario_run(self.content, history))

    def test_build_terminal_result_rejects_partial_run(self) -> None:
        run = start_scenario_run(self.content)
        with self.assertRaises(ScenarioEngineError):
            build_terminal_result(run)

        partial = replay_scenario_run(self.content, decision_history(_playthrough(self.content, correct=True))[:5])
        with self.assertRaises(ScenarioEngineError):
            build_terminal_result(partial)

    def test_build_terminal_result_accepts_complete_run(self) -> None:
        run = _playthrough(self.content, correct=True)
        result = build_terminal_result(run)
        self.assertEqual(result, run.terminal_result)

    def test_replay_scenario_run_rejects_extra_decision_after_terminal(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        history = list(decision_history(full_run))
        extra = ScenarioDecisionInput(
            sequence_number=len(history) + 1,
            scene_id=history[-1].scene_id,
            option_id=history[-1].option_id,
        )
        history.append(extra)
        with self.assertRaises(ScenarioRunStateError):
            replay_scenario_run(self.content, history)

    def test_replay_matches_run_uses_build_terminal_result(self) -> None:
        run = _playthrough(self.content, correct=True)
        replayed = replay_matches_run(self.content, run)
        self.assertEqual(replayed, run.terminal_result)

    def test_replay_matches_run_rejects_incomplete_run(self) -> None:
        run = start_scenario_run(self.content)
        with self.assertRaises(ScenarioRunStateError):
            replay_matches_run(self.content, run)

    def test_replay_scenario_run_rejects_stale_scene_id(self) -> None:
        run = start_scenario_run(self.content)
        scene = get_current_scene(run)
        option = _pick_option(scene, correct=True)
        with self.assertRaises(ScenarioRunStateError):
            replay_scenario_run(
                self.content,
                [ScenarioDecisionInput(sequence_number=1, scene_id="s99_wrong_scene", option_id=option.id)],
            )

    def test_resume_scenario_run_rejects_stale_scene_id(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            resume_scenario_run(
                self.content,
                [ScenarioDecisionInput(sequence_number=1, scene_id="s99_wrong_scene", option_id="A")],
            )

    def test_replay_scenario_run_rejects_duplicate_sequence_number(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        history = list(decision_history(full_run)[:3])
        history[2] = ScenarioDecisionInput(
            sequence_number=history[1].sequence_number,
            scene_id=history[2].scene_id,
            option_id=history[2].option_id,
        )
        with self.assertRaises(ScenarioRunStateError) as ctx:
            replay_scenario_run(self.content, history)
        self.assertIn("sequenceNumber", ctx.exception.path)

    def test_replay_scenario_run_rejects_sequence_gap(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        history = list(decision_history(full_run)[:3])
        del history[1]
        with self.assertRaises(ScenarioRunStateError) as ctx:
            replay_scenario_run(self.content, history)
        self.assertIn("sequenceNumber", ctx.exception.path)

    def test_replay_scenario_run_rejects_out_of_order_sequence(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        history = list(decision_history(full_run)[:3])
        history[0], history[1] = history[1], history[0]
        with self.assertRaises(ScenarioRunStateError) as ctx:
            replay_scenario_run(self.content, history)
        self.assertIn("sequenceNumber", ctx.exception.path)

    def test_replay_scenario_run_rejects_sequence_not_starting_at_one(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        history = list(decision_history(full_run)[1:4])
        with self.assertRaises(ScenarioRunStateError) as ctx:
            replay_scenario_run(self.content, history)
        self.assertIn("sequenceNumber", ctx.exception.path)

    def test_decision_history_matches_replay_input(self) -> None:
        run = _playthrough(self.content, correct=False, first_incorrect=True)
        history = decision_history(run)
        self.assertEqual(len(history), len(run.decisions))
        replayed = build_terminal_result(replay_scenario_run(self.content, history))
        self.assertEqual(replayed, run.terminal_result)

    # ======================================================================
    # 2. Strict decision-history deserialization
    # ======================================================================

    def test_deserialize_decision_history_accepts_valid_payload(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = serialize_run_snapshot(run)
        parsed = deserialize_decision_history(payload["decisionHistory"])
        self.assertEqual(parsed, decision_history(run))

    def test_deserialize_decision_history_rejects_non_list(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history({"sequenceNumber": 1, "sceneId": "s1", "optionId": "A"})

    def test_deserialize_decision_history_rejects_non_object_entry(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history(["not-an-object"])

    def test_deserialize_decision_history_rejects_missing_field(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history([{"sequenceNumber": 1, "sceneId": "s1"}])

    def test_deserialize_decision_history_rejects_extra_field(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history(
                [{"sequenceNumber": 1, "sceneId": "s1", "optionId": "A", "extra": "nope"}]
            )

    def test_deserialize_decision_history_rejects_boolean_sequence_number(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history([{"sequenceNumber": True, "sceneId": "s1", "optionId": "A"}])

    def test_deserialize_decision_history_rejects_non_integer_sequence_number(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history([{"sequenceNumber": 1.5, "sceneId": "s1", "optionId": "A"}])
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history([{"sequenceNumber": "1", "sceneId": "s1", "optionId": "A"}])

    def test_deserialize_decision_history_rejects_sequence_below_one(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history([{"sequenceNumber": 0, "sceneId": "s1", "optionId": "A"}])

    def test_deserialize_decision_history_rejects_empty_scene_id(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history([{"sequenceNumber": 1, "sceneId": "", "optionId": "A"}])

    def test_deserialize_decision_history_rejects_whitespace_scene_id(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history([{"sequenceNumber": 1, "sceneId": "   ", "optionId": "A"}])

    def test_deserialize_decision_history_rejects_empty_option_id(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history([{"sequenceNumber": 1, "sceneId": "s1", "optionId": ""}])

    def test_deserialize_decision_history_rejects_whitespace_option_id(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history([{"sequenceNumber": 1, "sceneId": "s1", "optionId": "  "}])

    def test_deserialize_decision_history_rejects_duplicate_sequence(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history(
                [
                    {"sequenceNumber": 1, "sceneId": "s1", "optionId": "A"},
                    {"sequenceNumber": 1, "sceneId": "s2", "optionId": "A"},
                ]
            )

    def test_deserialize_decision_history_rejects_skipped_sequence(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history(
                [
                    {"sequenceNumber": 1, "sceneId": "s1", "optionId": "A"},
                    {"sequenceNumber": 3, "sceneId": "s2", "optionId": "A"},
                ]
            )

    def test_deserialize_decision_history_rejects_reordered_sequence(self) -> None:
        with self.assertRaises(ScenarioRunStateError):
            deserialize_decision_history(
                [
                    {"sequenceNumber": 2, "sceneId": "s1", "optionId": "A"},
                    {"sequenceNumber": 1, "sceneId": "s2", "optionId": "A"},
                ]
            )

    # ======================================================================
    # 2. Serialized run replay + identity verification
    # ======================================================================

    def test_replay_serialized_run_reconstructs_completed_run(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = serialize_run_snapshot(run)
        reconstructed = replay_serialized_run(self.content, payload)
        self.assertEqual(reconstructed, run)

    def test_replay_serialized_run_reconstructs_partial_run(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        partial_run = replay_scenario_run(self.content, decision_history(full_run)[:5])
        payload = serialize_run_snapshot(partial_run)
        reconstructed = replay_serialized_run(self.content, payload)
        self.assertEqual(reconstructed, partial_run)

    def test_replay_serialized_run_rejects_simulation_id_mismatch(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = copy.deepcopy(serialize_run_snapshot(run))
        payload["simulationId"] = "not-the-real-simulation"
        with self.assertRaises(ScenarioReplayIdentityError):
            replay_serialized_run(self.content, payload)

    def test_replay_serialized_run_rejects_version_mismatch(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = copy.deepcopy(serialize_run_snapshot(run))
        payload["version"] = "9.9.9-does-not-exist"
        with self.assertRaises(ScenarioReplayIdentityError):
            replay_serialized_run(self.content, payload)

    def test_replay_serialized_run_rejects_canonical_hash_mismatch(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = copy.deepcopy(serialize_run_snapshot(run))
        payload["canonicalContentSha256"] = "0" * 64
        with self.assertRaises(ScenarioReplayIdentityError):
            replay_serialized_run(self.content, payload)

    def test_replay_serialized_run_rejects_engine_version_mismatch(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = copy.deepcopy(serialize_run_snapshot(run))
        payload["engineVersion"] = "SCENARIO_ENGINE_V0_DOES_NOT_EXIST"
        with self.assertRaises(ScenarioReplayIdentityError):
            replay_serialized_run(self.content, payload)

    # -- Derived-field tampering is always ignored by replay ---------------

    def test_replay_serialized_run_ignores_tampered_state(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = copy.deepcopy(serialize_run_snapshot(run))
        payload["state"] = {"projectHealth": -999, "stakeholderTrust": -999, "scheduleRisk": -999}
        reconstructed = replay_serialized_run(self.content, payload)
        self.assertEqual(dict(reconstructed.state), dict(run.state))

    def test_replay_serialized_run_ignores_tampered_flags(self) -> None:
        run = _playthrough(self.content, correct=False, first_incorrect=True)
        payload = copy.deepcopy(serialize_run_snapshot(run))
        payload["flags"] = ["totally_fake_flag"]
        reconstructed = replay_serialized_run(self.content, payload)
        self.assertEqual(reconstructed.flags, run.flags)

    def test_replay_serialized_run_ignores_tampered_domain_performance(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = copy.deepcopy(serialize_run_snapshot(run))
        payload["terminalResult"]["domainPerformance"] = []
        reconstructed = replay_serialized_run(self.content, payload)
        result = build_terminal_result(reconstructed)
        self.assertEqual(result.domain_performance, run.terminal_result.domain_performance)

    def test_replay_serialized_run_ignores_tampered_ending(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = copy.deepcopy(serialize_run_snapshot(run))
        payload["terminalResult"]["endingId"] = "ending_fail"
        reconstructed = replay_serialized_run(self.content, payload)
        result = build_terminal_result(reconstructed)
        self.assertEqual(result.ending_id, "ending_distinction")

    def test_replay_serialized_run_ignores_tampered_current_scene(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        partial_run = replay_scenario_run(self.content, decision_history(full_run)[:5])
        payload = copy.deepcopy(serialize_run_snapshot(partial_run))
        payload["currentSceneId"] = "s99_does_not_exist"
        reconstructed = replay_serialized_run(self.content, payload)
        self.assertEqual(reconstructed.current_scene_id, partial_run.current_scene_id)

    def test_replay_serialized_run_ignores_tampered_completion_status(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        partial_run = replay_scenario_run(self.content, decision_history(full_run)[:5])
        payload = copy.deepcopy(serialize_run_snapshot(partial_run))
        payload["isComplete"] = True
        reconstructed = replay_serialized_run(self.content, payload)
        self.assertFalse(reconstructed.is_complete)

    def test_replay_serialized_run_ignores_tampered_terminal_result(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = copy.deepcopy(serialize_run_snapshot(run))
        payload["terminalResult"] = {"endingId": "totally_fake", "scoreBand": "??"}
        reconstructed = replay_serialized_run(self.content, payload)
        result = build_terminal_result(reconstructed)
        self.assertEqual(result, run.terminal_result)

    def test_serialize_run_snapshot_round_trips_decision_history(self) -> None:
        full_run = _playthrough(self.content, correct=True)
        partial_pairs = decision_history(full_run)[:5]
        resumed = replay_scenario_run(self.content, partial_pairs)

        payload = serialize_run_snapshot(resumed)
        decoded = json.loads(json.dumps(payload))

        self.assertEqual(decoded["simulationId"], self.content.simulation_id)
        self.assertEqual(decoded["engineVersion"], ENGINE_VERSION)
        self.assertEqual(decoded["currentSceneId"], resumed.current_scene_id)
        self.assertFalse(decoded["isComplete"])
        self.assertIsNone(decoded["terminalResult"])

        decoded_inputs = deserialize_decision_history(decoded["decisionHistory"])
        self.assertEqual(decoded_inputs, partial_pairs)

        rebuilt = replay_scenario_run(self.content, decoded_inputs)
        self.assertEqual(rebuilt, resumed)

    def test_serialize_run_snapshot_includes_terminal_result_when_complete(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = serialize_run_snapshot(run)
        self.assertTrue(payload["isComplete"])
        self.assertIsNotNone(payload["terminalResult"])
        self.assertEqual(payload["terminalResult"]["endingId"], "ending_distinction")

    def test_serialize_terminal_result_is_json_safe_plain_dict(self) -> None:
        run = _playthrough(self.content, correct=True)
        payload = serialize_terminal_result(run.terminal_result)
        encoded = json.dumps(payload)
        decoded = json.loads(encoded)

        self.assertEqual(decoded["endingId"], "ending_distinction")
        self.assertEqual(decoded["finalState"]["projectHealth"], 100)
        self.assertEqual(decoded["engineVersion"], ENGINE_VERSION)
        self.assertEqual(
            decoded["canonicalContentSha256"], self.content.canonical_content_sha256
        )
        self.assertEqual(len(decoded["decisions"]), len(run.decisions))
        self.assertIn("domainPerformance", decoded)

    # ======================================================================
    # 3. Deep immutability
    # ======================================================================

    def test_runtime_state_mapping_cannot_be_mutated(self) -> None:
        run = start_scenario_run(self.content)
        with self.assertRaises(TypeError):
            run.state["projectHealth"] = 0  # type: ignore[index]

        scene = get_current_scene(run)
        option = _pick_option(scene, correct=True)
        stepped = apply_decision(run, option.id)
        with self.assertRaises(TypeError):
            stepped.state["projectHealth"] = 0  # type: ignore[index]

    def test_terminal_result_final_state_cannot_be_mutated(self) -> None:
        run = _playthrough(self.content, correct=True)
        with self.assertRaises(TypeError):
            run.terminal_result.final_state["projectHealth"] = 0  # type: ignore[index]

    def test_decision_history_cannot_be_mutated(self) -> None:
        run = _playthrough(self.content, correct=True)
        with self.assertRaises(TypeError):
            run.decisions[0] = run.decisions[0]  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            run.decisions[0].option_id = "TAMPERED"  # type: ignore[misc]

    def test_domain_performance_cannot_be_mutated(self) -> None:
        run = _playthrough(self.content, correct=True)
        with self.assertRaises(TypeError):
            run.terminal_result.domain_performance[0] = run.terminal_result.domain_performance[0]  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            run.terminal_result.domain_performance[0].correct_count = 999  # type: ignore[misc]

    def test_flags_cannot_be_mutated(self) -> None:
        run = _playthrough(self.content, correct=False, first_incorrect=True)
        with self.assertRaises(TypeError):
            run.flags[0] = "TAMPERED"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            run.flags.append("TAMPERED")  # type: ignore[attr-defined]

    def test_serialization_output_mutation_does_not_affect_runtime_state(self) -> None:
        run = _playthrough(self.content, correct=True)
        original_final_state = dict(run.terminal_result.final_state)
        original_option_id = run.decisions[0].option_id

        payload = serialize_run_snapshot(run)
        payload["state"]["projectHealth"] = -999
        payload["decisionHistory"][0]["optionId"] = "TAMPERED"
        payload["terminalResult"]["finalState"]["projectHealth"] = -999

        self.assertEqual(dict(run.state), original_final_state)
        self.assertEqual(dict(run.terminal_result.final_state), original_final_state)
        self.assertEqual(run.decisions[0].option_id, original_option_id)

    def test_scenario_content_is_unchanged_after_execution_and_replay(self) -> None:
        original_hash = self.content.canonical_content_sha256
        original_scenes = self.content.scenes
        original_initial_state = dict(self.content.initial_state)

        run = _playthrough(self.content, correct=True)
        replay_scenario_run(self.content, decision_history(run))
        replay_serialized_run(self.content, serialize_run_snapshot(run))

        self.assertEqual(self.content.canonical_content_sha256, original_hash)
        self.assertEqual(self.content.scenes, original_scenes)
        self.assertEqual(dict(self.content.initial_state), original_initial_state)

    # ======================================================================
    # 4. Generic behavior on synthetic content
    # ======================================================================

    def test_synthetic_three_scene_scenario_supports_partial_and_complete_replay(self) -> None:
        content = _linear_synthetic_content(3)
        full_history = (
            ScenarioDecisionInput(1, "s1", "A"),
            ScenarioDecisionInput(2, "s2", "A"),
            ScenarioDecisionInput(3, "s3", "A"),
        )

        partial = replay_scenario_run(content, full_history[:2])
        self.assertFalse(partial.is_complete)
        self.assertEqual(partial.current_scene_id, "s3")

        complete = replay_scenario_run(content, full_history)
        self.assertTrue(complete.is_complete)
        result = build_terminal_result(complete)
        self.assertEqual(result.ending_id, "ending_pass")
        self.assertEqual(dict(complete.state), {"score": 56.0})

    def test_synthetic_forty_scene_scenario_supports_partial_replay(self) -> None:
        content = _linear_synthetic_content(40)
        history = tuple(ScenarioDecisionInput(i + 1, f"s{i + 1}", "A") for i in range(10))
        partial = replay_scenario_run(content, history)
        self.assertFalse(partial.is_complete)
        self.assertEqual(partial.current_scene_id, "s11")
        self.assertEqual(len(partial.decisions), 10)

    def test_synthetic_scenario_supports_arbitrary_state_variable_names(self) -> None:
        content = _linear_synthetic_content(6, state_key="focus", minimum=0.0, maximum=50.0, initial_value=25.0)
        run = start_scenario_run(content)
        self.assertIn("focus", run.state)
        while not run.is_complete:
            scene = get_current_scene(run)
            option = _pick_option(scene, correct=True)
            run = apply_decision(run, option.id)
        self.assertEqual(run.state["focus"], 37.0)

    def test_explicit_scene_ids_control_navigation_not_array_position(self) -> None:
        content = _linear_synthetic_content(5, shuffle=True)
        array_order = [scene.id for scene in content.scenes]
        self.assertNotEqual(array_order, [f"s{i + 1}" for i in range(5)])

        run = start_scenario_run(content)
        visited_scene_ids = []
        while not run.is_complete:
            visited_scene_ids.append(run.current_scene_id)
            scene = get_current_scene(run)
            option = _pick_option(scene, correct=True)
            run = apply_decision(run, option.id)

        self.assertEqual(visited_scene_ids, ["s1", "s2", "s3", "s4", "s5"])

    def test_reconverging_branches_remain_deterministic(self) -> None:
        content = _reconverging_synthetic_content()

        run_a = replay_scenario_run(
            content,
            (
                ScenarioDecisionInput(1, "s1", "A"),
                ScenarioDecisionInput(2, "s2a", "X"),
                ScenarioDecisionInput(3, "s3", "Z"),
            ),
        )
        run_b = replay_scenario_run(
            content,
            (
                ScenarioDecisionInput(1, "s1", "B"),
                ScenarioDecisionInput(2, "s2b", "Y"),
                ScenarioDecisionInput(3, "s3", "Z"),
            ),
        )

        self.assertTrue(run_a.is_complete)
        self.assertTrue(run_b.is_complete)
        self.assertNotEqual(dict(run_a.state), dict(run_b.state))
        self.assertEqual(run_a.decisions[2].scene_id, "s3")
        self.assertEqual(run_b.decisions[2].scene_id, "s3")

        replay_a_again = replay_scenario_run(content, decision_history(run_a))
        self.assertEqual(replay_a_again, run_a)


if __name__ == "__main__":
    unittest.main()
