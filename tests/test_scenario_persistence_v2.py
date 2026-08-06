"""Focused tests for the SCENARIO_ENGINE_V2 persistence/serialization adapter
(``utils/scenario_persistence_v2.py``).

Covers, per SIM-PERSIST-V2-04's lettered test requirements (A-AX): snapshot
serialization/round-trip, envelope-version enforcement, identity
verification, strict integer/UUID/nonfinite-number rejection, thaw
behavior for MappingProxyType/frozenset/tuple, the mandatory three-field
``decisionHistory`` projection and its bidirectional (in and out)
enforcement, replay's use of canonical decision rows (never the envelope's
own cached history), cache-mismatch / terminal-outcome-mismatch fail-closed
behavior, immutability of every input, RPC parameter construction (start
and submit), RPC response parsing/validation, learner-safe serialization,
and JSON-native output.

This module never touches protected paths (.local/, local_only/, etc.) and
never stages/commits anything. It never connects to a database, instantiates
a Supabase client, or calls an RPC -- every RPC-response test here uses a
plain, hand-constructed dict/list standing in for a Supabase response.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_engine_v2 import (
    ENGINE_VERSION,
    ScenarioDecisionInputV2,
    apply_decision_v2,
    build_learner_scene_view,
    build_learner_terminal_view,
    build_scenario_content_v2,
    start_scenario_run_v2,
)
from utils.scenario_persistence_v2 import (
    ScenarioPersistenceV2CacheMismatchError,
    ScenarioPersistenceV2IdentityError,
    ScenarioPersistenceV2RpcResponseError,
    ScenarioPersistenceV2SerializationError,
    ScenarioPersistenceV2TerminalMismatchError,
    ScenarioPersistenceV2ValidationError,
    build_start_or_resume_rpc_params_v2,
    build_submit_decision_rpc_params_v2,
    deserialize_decision_input_v2,
    deserialize_run_snapshot_v2,
    parse_start_or_resume_rpc_response_v2,
    parse_submit_decision_rpc_response_v2,
    replay_serialized_run_v2,
    serialize_decision_input_v2,
    serialize_learner_scene_view_v2,
    serialize_learner_terminal_view_v2,
    serialize_run_snapshot_v2,
    verify_persisted_attempt_identity_v2,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "scenario_engine_v2_vslice_1_1_0.json"

# The fixture's own happy (non-corrective) completion path, reused by
# test_scenario_engine_v2.py::test_43_replay_reproduces_normal_path.
HAPPY_PATH_DECISIONS = (
    (1, "SC001-C01", "opt-sc001-c01-a"),
    (2, "SC001-C02", "opt-sc001-c02-a"),
    (3, "SC001-C03", "opt-sc001-c03-a"),
    (4, "SC001-C04", "opt-sc001-c04-a"),
)


def _load_document() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _new_attempt_id() -> str:
    return str(uuid.uuid4())


class PersistenceV2TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _load_document()
        self.content = build_scenario_content_v2(copy.deepcopy(self.document))
        self.attempt_id = _new_attempt_id()

    def _start_run(self):
        return start_scenario_run_v2(self.content, attempt_id=self.attempt_id)

    def _run_happy_path(self, up_to: int = len(HAPPY_PATH_DECISIONS)):
        """Return the run snapshot after applying the first ``up_to`` steps
        of the fixture's happy completion path."""
        run = self._start_run()
        for sequence_number, scene_id, option_id in HAPPY_PATH_DECISIONS[:up_to]:
            run = apply_decision_v2(run, ScenarioDecisionInputV2(sequence_number, scene_id, option_id))
        return run

    def _canonical_rows(self, up_to: int = len(HAPPY_PATH_DECISIONS)):
        return [
            {"sequenceNumber": seq, "sceneId": scene, "optionId": opt}
            for seq, scene, opt in HAPPY_PATH_DECISIONS[:up_to]
        ]


# The frozen, SIM-PERSIST-V2-04B-corrected envelope shape -- exactly these
# 17 keys, matching the validated V68/V69 SQL contract, the Slice A
# contract, and SCENARIO_SCHEMA_1_1_0_SPEC.md section 19.2. Deliberately
# excludes `attemptId`/`decisionCount`/`scenarioVersion`/`status`.
_FROZEN_ENVELOPE_KEYS = frozenset(
    {
        "envelopeVersion",
        "simulationId",
        "version",
        "schemaVersion",
        "engineVersion",
        "canonicalContentSha256",
        "currentSceneId",
        "expectedSequenceNumber",
        "isComplete",
        "state",
        "counters",
        "flags",
        "decisionHistory",
        "optionDisplayOrderByScene",
        "selectedVariantIdByScene",
        "routingResolutions",
        "terminalResult",
    }
)


def _recursive_keys(value: Any) -> set:
    """All string keys appearing anywhere in a nested JSON-like structure."""
    keys: set = set()
    if isinstance(value, dict):
        for k, v in value.items():
            keys.add(k)
            keys.update(_recursive_keys(v))
    elif isinstance(value, list):
        for item in value:
            keys.update(_recursive_keys(item))
    return keys


# ---------------------------------------------------------------------------
# A-B: snapshot serialization + round trip
# ---------------------------------------------------------------------------


class TestSnapshotSerialization(PersistenceV2TestCase):
    def test_A_snapshot_serialization_succeeds(self):
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        self.assertEqual(envelope["envelopeVersion"], 1)
        self.assertEqual(envelope["simulationId"], self.content.simulation_id)
        self.assertEqual(envelope["version"], self.content.version)
        self.assertEqual(envelope["schemaVersion"], self.content.schema_version)
        self.assertEqual(envelope["engineVersion"], ENGINE_VERSION)
        self.assertIs(envelope["isComplete"], False)
        self.assertIsNone(envelope["terminalResult"])
        # SIM-PERSIST-V2-04B: attempt identity and decision count are never
        # part of the envelope, and the old field names must never appear.
        self.assertNotIn("attemptId", envelope)
        self.assertNotIn("decisionCount", envelope)
        self.assertNotIn("scenarioVersion", envelope)
        self.assertNotIn("status", envelope)
        # routingResolutions/selectedVariantIdByScene are mandatory keys,
        # present (empty) even when there is nothing yet to report.
        self.assertIn("routingResolutions", envelope)
        self.assertEqual(envelope["routingResolutions"], [])
        self.assertIn("selectedVariantIdByScene", envelope)
        self.assertEqual(set(envelope.keys()), _FROZEN_ENVELOPE_KEYS)

    def test_B_round_trip_preserves_approved_cache_fields(self):
        run = self._run_happy_path(up_to=2)
        envelope = serialize_run_snapshot_v2(run)
        parsed = deserialize_run_snapshot_v2(envelope)
        self.assertFalse(hasattr(parsed, "attempt_id"))
        self.assertEqual(parsed.version, self.content.version)
        self.assertEqual(parsed.current_scene_id, run.current_scene_id)
        self.assertEqual(parsed.expected_sequence_number, run.expected_sequence_number)
        self.assertEqual(dict(parsed.state), dict(run.state))
        self.assertEqual(dict(parsed.counters), dict(run.counters))
        self.assertEqual(parsed.flags, run.flags)
        self.assertEqual(len(parsed.decision_history), len(run.decisions))
        for parsed_entry, engine_entry in zip(parsed.decision_history, run.decisions):
            self.assertEqual(parsed_entry.sequence_number, engine_entry.sequence_number)
            self.assertEqual(parsed_entry.scene_id, engine_entry.scene_id)
            self.assertEqual(parsed_entry.option_id, engine_entry.option_id)


# ---------------------------------------------------------------------------
# C-D: envelope version
# ---------------------------------------------------------------------------


class TestEnvelopeVersion(PersistenceV2TestCase):
    def test_C_envelope_version_required(self):
        envelope = serialize_run_snapshot_v2(self._start_run())
        del envelope["envelopeVersion"]
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)

    def test_D_unsupported_envelope_version_rejected(self):
        envelope = serialize_run_snapshot_v2(self._start_run())
        envelope["envelopeVersion"] = 2
        with self.assertRaises(ScenarioPersistenceV2ValidationError) as ctx:
            deserialize_run_snapshot_v2(envelope)
        self.assertIn("unsupported_envelope_version", str(ctx.exception))


# ---------------------------------------------------------------------------
# E-I: identity verification
# ---------------------------------------------------------------------------


class TestIdentityVerification(PersistenceV2TestCase):
    def test_E_missing_identity_field_rejected(self):
        envelope = serialize_run_snapshot_v2(self._start_run())
        del envelope["simulationId"]
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)

    def test_F_identity_mismatch_rejected(self):
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        envelope["version"] = "9.9.9-does-not-match"
        parsed = deserialize_run_snapshot_v2(envelope)
        with self.assertRaises(ScenarioPersistenceV2IdentityError):
            verify_persisted_attempt_identity_v2(
                self.content,
                attempt_row_id=self.attempt_id,
                attempt_row_engine_version=ENGINE_VERSION,
                attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
                envelope=parsed,
            )

    def test_G_trusted_attempt_row_id_not_compared_against_envelope(self):
        # SIM-PERSIST-V2-04B: the envelope carries no attempt identity at
        # all, so verify_persisted_attempt_identity_v2 has nothing to
        # compare attempt_row_id against -- it validates attempt_row_id is
        # a well-formed UUID and otherwise only verifies *content*
        # identity (simulationId/version/schemaVersion/
        # canonicalContentSha256/engineVersion). A trusted attempt_row_id
        # unrelated to how the run was originally started must not, by
        # itself, cause an identity failure.
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        parsed = deserialize_run_snapshot_v2(envelope)
        other_attempt_id = _new_attempt_id()
        self.assertNotEqual(other_attempt_id, self.attempt_id)

        verify_persisted_attempt_identity_v2(
            self.content,
            attempt_row_id=other_attempt_id,
            attempt_row_engine_version=ENGINE_VERSION,
            attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
            envelope=parsed,
        )

    def test_G3_replay_identity_always_matches_trusted_attempt_row_id(self):
        # replay_serialized_run_v2 must assign the reconstructed run's
        # attempt_id strictly from the trusted attempt_row_id parameter
        # (never from the envelope, which carries none).
        run = self._run_happy_path(up_to=2)
        envelope = serialize_run_snapshot_v2(run)
        rows = self._canonical_rows(up_to=2)
        replayed = replay_serialized_run_v2(
            self.content,
            attempt_row_id=self.attempt_id,
            attempt_row_engine_version=ENGINE_VERSION,
            attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
            canonical_decision_rows=rows,
            cached_envelope_payload=envelope,
        )
        self.assertEqual(replayed.attempt_id, self.attempt_id)

    def test_G2_forged_attempt_id_in_raw_envelope_rejected(self):
        # A forged/adversarial payload attempting to smuggle attempt
        # identity back into the envelope must be rejected structurally,
        # at deserialization, before any identity/replay logic even runs.
        envelope = dict(serialize_run_snapshot_v2(self._start_run()))
        envelope["attemptId"] = _new_attempt_id()
        with self.assertRaises(ScenarioPersistenceV2ValidationError) as ctx:
            deserialize_run_snapshot_v2(envelope)
        self.assertIn("unexpected_field", str(ctx.exception))
        self.assertIn("attemptId", str(ctx.exception))

    def test_H_hash_mismatch_rejected(self):
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        parsed = deserialize_run_snapshot_v2(envelope)
        wrong_hash = "0" * 64
        with self.assertRaises(ScenarioPersistenceV2IdentityError):
            verify_persisted_attempt_identity_v2(
                self.content,
                attempt_row_id=self.attempt_id,
                attempt_row_engine_version=ENGINE_VERSION,
                attempt_row_scenario_content_sha256=wrong_hash,
                envelope=parsed,
            )

    def test_I_engine_version_mismatch_rejected(self):
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        parsed = deserialize_run_snapshot_v2(envelope)
        with self.assertRaises(ScenarioPersistenceV2IdentityError):
            verify_persisted_attempt_identity_v2(
                self.content,
                attempt_row_id=self.attempt_id,
                attempt_row_engine_version="SCENARIO_ENGINE_V1",
                attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
                envelope=parsed,
            )


# ---------------------------------------------------------------------------
# J-N: strict integer / nonfinite rejection
# ---------------------------------------------------------------------------


class TestStrictTypesAndNonfinite(PersistenceV2TestCase):
    def test_J_exact_integer_sequence_enforced(self):
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_decision_input_v2({"sequenceNumber": "1", "sceneId": "SC001-C01", "optionId": "opt-a"})

    def test_K_bool_sequence_rejected(self):
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_decision_input_v2({"sequenceNumber": True, "sceneId": "SC001-C01", "optionId": "opt-a"})
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            serialize_decision_input_v2(ScenarioDecisionInputV2(True, "SC001-C01", "opt-a"))

    def test_L_nan_rejected(self):
        envelope = serialize_run_snapshot_v2(self._start_run())
        envelope["state"] = dict(envelope["state"])
        first_key = next(iter(envelope["state"]))
        envelope["state"][first_key] = float("nan")
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)

    def test_M_positive_infinity_rejected(self):
        envelope = serialize_run_snapshot_v2(self._start_run())
        envelope["state"] = dict(envelope["state"])
        first_key = next(iter(envelope["state"]))
        envelope["state"][first_key] = float("inf")
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)

    def test_N_negative_infinity_rejected(self):
        envelope = serialize_run_snapshot_v2(self._start_run())
        envelope["state"] = dict(envelope["state"])
        first_key = next(iter(envelope["state"]))
        envelope["state"][first_key] = float("-inf")
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)


# ---------------------------------------------------------------------------
# O-Q: MappingProxyType / frozenset / tuple thaw behavior
# ---------------------------------------------------------------------------


class TestThawBehavior(PersistenceV2TestCase):
    def test_O_mapping_proxy_type_thawed(self):
        run = self._start_run()
        scene_view = build_learner_scene_view(run)
        # The content is deep-frozen, so progressMetadata/accessibility (when
        # present) originate as MappingProxyType. Whether present or absent
        # in this fixture, the serialized output must never itself be a
        # MappingProxyType.
        serialized = serialize_learner_scene_view_v2(scene_view)
        for field in ("progressMetadata", "accessibility", "mobilePresentation"):
            value = serialized[field]
            self.assertNotIsInstance(value, MappingProxyType)
            if value is not None:
                self.assertIsInstance(value, dict)
        json.dumps(serialized, allow_nan=False)  # would raise on a MappingProxyType leak

    def test_O2_deserialize_thaws_mapping_proxy_type_input(self):
        envelope = dict(serialize_run_snapshot_v2(self._start_run()))
        envelope["selectedVariantIdByScene"] = MappingProxyType(dict(envelope["selectedVariantIdByScene"]))
        parsed = deserialize_run_snapshot_v2(envelope)
        self.assertIsInstance(dict(parsed.selected_variant_id_by_scene), dict)

    def test_P_frozenset_thawed_deterministically(self):
        run = self._run_happy_path(up_to=2)
        envelope_a = serialize_run_snapshot_v2(run)
        envelope_b = serialize_run_snapshot_v2(run)
        self.assertIsInstance(envelope_a["flags"], list)
        self.assertEqual(envelope_a["flags"], sorted(envelope_a["flags"]))
        self.assertEqual(envelope_a["flags"], envelope_b["flags"])

    def test_Q_tuples_converted_explicitly(self):
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        for scene_id, order in envelope["optionDisplayOrderByScene"].items():
            self.assertIsInstance(order, list)
            self.assertNotIsInstance(order, tuple)


# ---------------------------------------------------------------------------
# R-T: UUID contract
# ---------------------------------------------------------------------------


class TestUuidContract(PersistenceV2TestCase):
    def test_R_uuid_canonicalized_at_serialize_boundaries(self):
        # The envelope itself carries no UUID field anymore (attemptId was
        # removed), so UUID canonicalization is exercised at the two
        # remaining trusted-attempt-identity boundaries: RPC param
        # construction and replay/verification.
        mixed_case_attempt_id = self.attempt_id.upper()
        mixed_case_run = start_scenario_run_v2(self.content, attempt_id=mixed_case_attempt_id)
        params = build_start_or_resume_rpc_params_v2(
            mixed_case_run, user_email="learner@example.com", scenario_version_id=_new_attempt_id()
        )
        self.assertEqual(params["p_attempt_id"], mixed_case_attempt_id.lower())

        # Replay canonicalizes `attempt_row_id` before using it to drive
        # reconstruction. Uses `self.attempt_id` (already lowercase) for
        # the underlying run so option-display randomization -- which is
        # seeded per-attempt -- is unaffected by the case-canonicalization
        # itself; only `attempt_row_id`'s case varies here.
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        replayed = replay_serialized_run_v2(
            self.content,
            attempt_row_id=self.attempt_id.upper(),
            attempt_row_engine_version=ENGINE_VERSION,
            attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
            canonical_decision_rows=[],
            cached_envelope_payload=envelope,
        )
        self.assertEqual(replayed.attempt_id, self.attempt_id.lower())
        self.assertEqual(replayed.attempt_id, replayed.attempt_id.lower())

    def test_S_nil_attempt_uuid_rejected(self):
        nil_uuid = "00000000-0000-0000-0000-000000000000"
        run = start_scenario_run_v2(self.content, attempt_id=nil_uuid)
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            build_start_or_resume_rpc_params_v2(
                run, user_email="learner@example.com", scenario_version_id=_new_attempt_id()
            )

    def test_T_non_v4_idempotency_uuid_rejected(self):
        run_before = self._run_happy_path(up_to=1)
        decision = ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-a")
        run_after = apply_decision_v2(run_before, decision)
        v1_uuid = str(uuid.uuid1())
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            build_submit_decision_rpc_params_v2(
                run_before,
                run_after,
                decision,
                user_email="learner@example.com",
                idempotency_key=v1_uuid,
            )


# ---------------------------------------------------------------------------
# U-Y: decisionHistory hidden-field exclusion (SA-16-1)
# ---------------------------------------------------------------------------


class TestDecisionHistoryExclusion(PersistenceV2TestCase):
    def test_U_decision_history_contains_exactly_three_fields(self):
        run = self._run_happy_path(up_to=2)
        envelope = serialize_run_snapshot_v2(run)
        for entry in envelope["decisionHistory"]:
            self.assertEqual(set(entry.keys()), {"sequenceNumber", "sceneId", "optionId"})

    def test_U2_decision_history_excludes_every_debrief_trace_field(self):
        # Independently confirm against the real DebriefTraceEntry-derived
        # decisions that none of the twelve excluded fields ever leak.
        run = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS))
        self.assertTrue(run.is_complete)
        envelope = serialize_run_snapshot_v2(run)
        serialized_text = json.dumps(envelope["decisionHistory"])
        for forbidden in (
            "evaluationTier",
            "debriefSeed",
            "stateDelta",
            "stateAfter",
            "flagsCleared",
            "flagsSet",
            "nextSceneId",
            "enteredCorrective",
            "skippedCorrective",
            "presentedDialogueVariantId",
            "nextDialogueVariantId",
            "competencyTags",
        ):
            self.assertNotIn(forbidden, serialized_text)

    def test_V_decision_history_rejects_extra_evaluation_tier(self):
        with self.assertRaises(ScenarioPersistenceV2ValidationError) as ctx:
            deserialize_decision_input_v2(
                {"sequenceNumber": 1, "sceneId": "SC001-C01", "optionId": "opt-a", "evaluationTier": "optimal"}
            )
        self.assertIn("unexpected_field", str(ctx.exception))

    def test_W_decision_history_rejects_debrief_seed(self):
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_decision_input_v2(
                {"sequenceNumber": 1, "sceneId": "SC001-C01", "optionId": "opt-a", "debriefSeed": {}}
            )

    def test_X_decision_history_rejects_state_delta(self):
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_decision_input_v2(
                {"sequenceNumber": 1, "sceneId": "SC001-C01", "optionId": "opt-a", "stateDelta": {"x": 1.0}}
            )

    def test_Y_decision_history_rejects_flags(self):
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_decision_input_v2(
                {"sequenceNumber": 1, "sceneId": "SC001-C01", "optionId": "opt-a", "flagsSet": ["x"]}
            )
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_decision_input_v2(
                {"sequenceNumber": 1, "sceneId": "SC001-C01", "optionId": "opt-a", "flagsCleared": ["x"]}
            )


# ---------------------------------------------------------------------------
# Z-AF: replay authority, cache comparison, terminal comparison
# ---------------------------------------------------------------------------


class TestReplayAndCacheComparison(PersistenceV2TestCase):
    def test_Z_canonical_decision_rows_are_used_for_replay(self):
        # Persist an envelope after only 2 decisions, but corrupt its
        # decisionHistory so it disagrees with the canonical rows (a full
        # 4-decision, completed history). Replay must reconstruct from the
        # canonical rows (producing a *completed* run), not from the
        # envelope's own (2-decision, in_progress) decisionHistory -- the
        # mismatch is caught as a cache disagreement, proving the envelope's
        # decisionHistory was never used to drive reconstruction.
        partial_run = self._run_happy_path(up_to=2)
        stale_envelope = serialize_run_snapshot_v2(partial_run)
        full_rows = self._canonical_rows(up_to=len(HAPPY_PATH_DECISIONS))

        with self.assertRaises(ScenarioPersistenceV2CacheMismatchError) as ctx:
            replay_serialized_run_v2(
                self.content,
                attempt_row_id=self.attempt_id,
                attempt_row_engine_version=ENGINE_VERSION,
                attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
                canonical_decision_rows=full_rows,
                cached_envelope_payload=stale_envelope,
            )
        self.assertIn("decisionHistory", str(ctx.exception))

    def test_AA_corrupted_cached_state_cannot_influence_replay(self):
        run = self._run_happy_path(up_to=2)
        envelope = serialize_run_snapshot_v2(run)
        envelope["state"] = dict(envelope["state"])
        first_key = next(iter(envelope["state"]))
        envelope["state"][first_key] = 999999.0  # a value the real engine would never produce here
        rows = self._canonical_rows(up_to=2)
        # Reconstruction itself must not choke on / adopt the corrupted
        # cache -- the only observable effect is a focused cache-mismatch
        # domain error, never an engine-internal crash or a silently
        # accepted bad state.
        with self.assertRaises(ScenarioPersistenceV2CacheMismatchError):
            replay_serialized_run_v2(
                self.content,
                attempt_row_id=self.attempt_id,
                attempt_row_engine_version=ENGINE_VERSION,
                attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
                canonical_decision_rows=rows,
                cached_envelope_payload=envelope,
            )

    def test_AB_corrupted_cached_state_produces_cache_mismatch_error(self):
        run = self._run_happy_path(up_to=2)
        envelope = serialize_run_snapshot_v2(run)
        envelope["state"] = dict(envelope["state"])
        first_key = next(iter(envelope["state"]))
        envelope["state"][first_key] = envelope["state"][first_key] + 1.0
        rows = self._canonical_rows(up_to=2)
        with self.assertRaises(ScenarioPersistenceV2CacheMismatchError) as ctx:
            replay_serialized_run_v2(
                self.content,
                attempt_row_id=self.attempt_id,
                attempt_row_engine_version=ENGINE_VERSION,
                attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
                canonical_decision_rows=rows,
                cached_envelope_payload=envelope,
            )
        self.assertIn("state", str(ctx.exception))

    def test_AC_corrupted_option_order_produces_cache_mismatch_error(self):
        run = self._run_happy_path(up_to=1)
        envelope = serialize_run_snapshot_v2(run)
        scene_id = run.current_scene_id
        order = list(envelope["optionDisplayOrderByScene"][scene_id])
        if len(order) < 2:
            self.skipTest("fixture scene does not have enough options to reorder")
        order[0], order[1] = order[1], order[0]
        envelope["optionDisplayOrderByScene"] = dict(envelope["optionDisplayOrderByScene"])
        envelope["optionDisplayOrderByScene"][scene_id] = order
        rows = self._canonical_rows(up_to=1)
        with self.assertRaises(ScenarioPersistenceV2CacheMismatchError) as ctx:
            replay_serialized_run_v2(
                self.content,
                attempt_row_id=self.attempt_id,
                attempt_row_engine_version=ENGINE_VERSION,
                attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
                canonical_decision_rows=rows,
                cached_envelope_payload=envelope,
            )
        self.assertIn("optionDisplayOrderByScene", str(ctx.exception))

    def test_AD_corrupted_counter_produces_cache_mismatch_error(self):
        run = self._run_happy_path(up_to=2)
        envelope = serialize_run_snapshot_v2(run)
        envelope["counters"] = dict(envelope["counters"])
        first_key = next(iter(envelope["counters"]))
        envelope["counters"][first_key] = envelope["counters"][first_key] + 5
        rows = self._canonical_rows(up_to=2)
        with self.assertRaises(ScenarioPersistenceV2CacheMismatchError) as ctx:
            replay_serialized_run_v2(
                self.content,
                attempt_row_id=self.attempt_id,
                attempt_row_engine_version=ENGINE_VERSION,
                attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
                canonical_decision_rows=rows,
                cached_envelope_payload=envelope,
            )
        self.assertIn("counters", str(ctx.exception))

    def test_AE_corrupted_final_outcome_fails_closed(self):
        run = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS))
        self.assertTrue(run.is_complete)
        envelope = serialize_run_snapshot_v2(run)
        envelope["terminalResult"] = dict(envelope["terminalResult"])
        envelope["terminalResult"]["displayScore"] = envelope["terminalResult"]["displayScore"] + 1
        rows = self._canonical_rows(up_to=len(HAPPY_PATH_DECISIONS))
        with self.assertRaises(ScenarioPersistenceV2TerminalMismatchError):
            replay_serialized_run_v2(
                self.content,
                attempt_row_id=self.attempt_id,
                attempt_row_engine_version=ENGINE_VERSION,
                attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
                canonical_decision_rows=rows,
                cached_envelope_payload=envelope,
            )

    def test_AF_valid_completed_outcome_passes(self):
        run = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS))
        envelope = serialize_run_snapshot_v2(run)
        rows = self._canonical_rows(up_to=len(HAPPY_PATH_DECISIONS))
        replayed = replay_serialized_run_v2(
            self.content,
            attempt_row_id=self.attempt_id,
            attempt_row_engine_version=ENGINE_VERSION,
            attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
            canonical_decision_rows=rows,
            cached_envelope_payload=envelope,
        )
        self.assertTrue(replayed.is_complete)
        self.assertEqual(replayed.terminal_result.outcome_id, run.terminal_result.outcome_id)
        self.assertEqual(replayed.terminal_result.display_score, run.terminal_result.display_score)

    def test_AF2_valid_in_progress_replay_passes(self):
        run = self._run_happy_path(up_to=2)
        envelope = serialize_run_snapshot_v2(run)
        rows = self._canonical_rows(up_to=2)
        replayed = replay_serialized_run_v2(
            self.content,
            attempt_row_id=self.attempt_id,
            attempt_row_engine_version=ENGINE_VERSION,
            attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
            canonical_decision_rows=rows,
            cached_envelope_payload=envelope,
        )
        self.assertFalse(replayed.is_complete)
        self.assertEqual(replayed.current_scene_id, run.current_scene_id)


# ---------------------------------------------------------------------------
# AG-AJ: envelope internal consistency
# ---------------------------------------------------------------------------


class TestEnvelopeConsistency(PersistenceV2TestCase):
    def test_AG_active_attempt_with_terminal_result_rejected(self):
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        self.assertIs(envelope["isComplete"], False)
        envelope["terminalResult"] = {
            "endingId": "fake-ending",
            "displayScore": 50,
            "engineVersion": ENGINE_VERSION,
            "canonicalContentSha256": self.content.canonical_content_sha256,
        }
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)

    def test_AH_completed_attempt_without_terminal_result_rejected(self):
        run = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS))
        envelope = serialize_run_snapshot_v2(run)
        self.assertIs(envelope["isComplete"], True)
        envelope["terminalResult"] = None
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)

    def test_AG2_isComplete_true_on_active_run_rejected(self):
        # An active run's envelope has a non-null currentSceneId and no
        # terminalResult -- forcing isComplete to True must fail closed
        # (both the "currentSceneId must be null" and "terminalResult must
        # be present" invariants are violated).
        envelope = serialize_run_snapshot_v2(self._start_run())
        self.assertIs(envelope["isComplete"], False)
        envelope["isComplete"] = True
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)

    def test_AH2_isComplete_false_on_completed_run_rejected(self):
        run = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS))
        envelope = serialize_run_snapshot_v2(run)
        self.assertIs(envelope["isComplete"], True)
        envelope["isComplete"] = False
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)

    def test_AH3_string_isComplete_rejected(self):
        for bad_value in ("in_progress", "completed", "active", "true", ""):
            with self.subTest(bad_value=bad_value):
                envelope = serialize_run_snapshot_v2(self._start_run())
                envelope["isComplete"] = bad_value
                with self.assertRaises(ScenarioPersistenceV2ValidationError):
                    deserialize_run_snapshot_v2(envelope)

    def test_AH4_int_isComplete_rejected(self):
        # bool/int confusion must not occur: 0/1 are not accepted in place
        # of an actual JSON boolean, even though Python's bool is an int
        # subclass at runtime.
        for bad_value in (0, 1, 2, -1):
            with self.subTest(bad_value=bad_value):
                envelope = serialize_run_snapshot_v2(self._start_run())
                envelope["isComplete"] = bad_value
                with self.assertRaises(ScenarioPersistenceV2ValidationError):
                    deserialize_run_snapshot_v2(envelope)

    def test_AH5_scenarioVersion_key_rejected(self):
        # Both an unexpected `scenarioVersion` key and a missing `version`
        # key are present; unexpected-key rejection fires first, but either
        # way the old field name must never be accepted.
        envelope = dict(serialize_run_snapshot_v2(self._start_run()))
        envelope["scenarioVersion"] = envelope.pop("version")
        with self.assertRaises(ScenarioPersistenceV2ValidationError) as ctx:
            deserialize_run_snapshot_v2(envelope)
        message = str(ctx.exception)
        self.assertIn("unexpected_field", message)
        self.assertIn("scenarioVersion", message)

    def test_AH6_status_key_rejected(self):
        envelope = dict(serialize_run_snapshot_v2(self._start_run()))
        envelope["status"] = "in_progress" if not envelope["isComplete"] else "completed"
        del envelope["isComplete"]
        with self.assertRaises(ScenarioPersistenceV2ValidationError) as ctx:
            deserialize_run_snapshot_v2(envelope)
        message = str(ctx.exception)
        self.assertIn("unexpected_field", message)
        self.assertIn("status", message)

    def test_AH7_decisionCount_key_rejected(self):
        envelope = dict(serialize_run_snapshot_v2(self._run_happy_path(up_to=2)))
        envelope["decisionCount"] = len(envelope["decisionHistory"])
        with self.assertRaises(ScenarioPersistenceV2ValidationError) as ctx:
            deserialize_run_snapshot_v2(envelope)
        self.assertIn("unexpected_field", str(ctx.exception))
        self.assertIn("decisionCount", str(ctx.exception))

    def test_AH8_routing_resolutions_always_present(self):
        # Mandatory key -- present as [] on a freshly started run (no
        # routing has happened yet), never omitted.
        envelope = serialize_run_snapshot_v2(self._start_run())
        self.assertIn("routingResolutions", envelope)
        self.assertEqual(envelope["routingResolutions"], [])
        parsed = deserialize_run_snapshot_v2(envelope)
        self.assertEqual(parsed.routing_resolutions, ())

        del_envelope = dict(envelope)
        del del_envelope["routingResolutions"]
        with self.assertRaises(ScenarioPersistenceV2ValidationError) as ctx:
            deserialize_run_snapshot_v2(del_envelope)
        self.assertIn("routingResolutions", str(ctx.exception))

    def test_AH9_selected_variant_id_by_scene_always_present(self):
        envelope = serialize_run_snapshot_v2(self._start_run())
        self.assertIn("selectedVariantIdByScene", envelope)
        self.assertIsInstance(envelope["selectedVariantIdByScene"], dict)
        parsed = deserialize_run_snapshot_v2(envelope)
        self.assertIsInstance(dict(parsed.selected_variant_id_by_scene), dict)

        del_envelope = dict(envelope)
        del del_envelope["selectedVariantIdByScene"]
        with self.assertRaises(ScenarioPersistenceV2ValidationError) as ctx:
            deserialize_run_snapshot_v2(del_envelope)
        self.assertIn("selectedVariantIdByScene", str(ctx.exception))

    def test_AI_option_order_duplicates_rejected(self):
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        scene_id = run.current_scene_id
        order = list(envelope["optionDisplayOrderByScene"][scene_id])
        if not order:
            self.skipTest("fixture start scene has no options")
        order.append(order[0])
        envelope["optionDisplayOrderByScene"] = dict(envelope["optionDisplayOrderByScene"])
        envelope["optionDisplayOrderByScene"][scene_id] = order
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            deserialize_run_snapshot_v2(envelope)

    def test_AJ_unknown_envelope_fields_rejected(self):
        envelope = serialize_run_snapshot_v2(self._start_run())
        envelope["unexpectedTopLevelField"] = "should not be here"
        with self.assertRaises(ScenarioPersistenceV2ValidationError) as ctx:
            deserialize_run_snapshot_v2(envelope)
        self.assertIn("unexpected_field", str(ctx.exception))


# ---------------------------------------------------------------------------
# AK-AM: immutability
# ---------------------------------------------------------------------------


class TestImmutability(PersistenceV2TestCase):
    def test_AK_serialization_does_not_mutate_snapshot(self):
        run = self._run_happy_path(up_to=2)
        state_before = dict(run.state)
        counters_before = dict(run.counters)
        flags_before = set(run.flags)
        decisions_before = tuple(run.decisions)
        order_before = {k: tuple(v) for k, v in run.option_display_order_by_scene.items()}

        serialize_run_snapshot_v2(run)

        self.assertEqual(dict(run.state), state_before)
        self.assertEqual(dict(run.counters), counters_before)
        self.assertEqual(set(run.flags), flags_before)
        self.assertEqual(tuple(run.decisions), decisions_before)
        self.assertEqual({k: tuple(v) for k, v in run.option_display_order_by_scene.items()}, order_before)

    def test_AL_deserialization_does_not_mutate_input(self):
        envelope = serialize_run_snapshot_v2(self._start_run())
        reference_copy = copy.deepcopy(envelope)
        deserialize_run_snapshot_v2(envelope)
        self.assertEqual(envelope, reference_copy)

    def test_AM_replay_does_not_mutate_decisions(self):
        run = self._run_happy_path(up_to=2)
        envelope = serialize_run_snapshot_v2(run)
        rows = self._canonical_rows(up_to=2)
        rows_copy = copy.deepcopy(rows)
        envelope_copy = copy.deepcopy(envelope)

        replay_serialized_run_v2(
            self.content,
            attempt_row_id=self.attempt_id,
            attempt_row_engine_version=ENGINE_VERSION,
            attempt_row_scenario_content_sha256=self.content.canonical_content_sha256,
            canonical_decision_rows=rows,
            cached_envelope_payload=envelope,
        )

        self.assertEqual(rows, rows_copy)
        self.assertEqual(envelope, envelope_copy)


# ---------------------------------------------------------------------------
# AN-AT: RPC parameter construction and response parsing
# ---------------------------------------------------------------------------


class TestStartRpcParams(PersistenceV2TestCase):
    def test_AN_start_rpc_params_contain_exactly_seven_keys(self):
        run = self._start_run()
        params = build_start_or_resume_rpc_params_v2(
            run, user_email="learner@example.com", scenario_version_id=_new_attempt_id()
        )
        self.assertEqual(
            set(params.keys()),
            {
                "p_user_email",
                "p_scenario_version_id",
                "p_initial_current_scene_id",
                "p_initial_serialized_state",
                "p_engine_version",
                "p_scenario_content_sha256",
                "p_attempt_id",
            },
        )
        self.assertEqual(len(params), 7)

    def test_AO_start_rpc_attempt_id_matches_runtime_attempt_id(self):
        run = self._start_run()
        params = build_start_or_resume_rpc_params_v2(
            run, user_email="learner@example.com", scenario_version_id=_new_attempt_id()
        )
        self.assertEqual(params["p_attempt_id"], run.attempt_id.lower())
        self.assertEqual(params["p_engine_version"], ENGINE_VERSION)
        self.assertEqual(params["p_scenario_content_sha256"], self.content.canonical_content_sha256)
        self.assertEqual(params["p_initial_current_scene_id"], run.current_scene_id)
        json.dumps(params, allow_nan=False)

    def test_AO2_start_rpc_uses_corrected_envelope_shape(self):
        # Item 15: the start RPC's serialized-state param must use the
        # corrected frozen envelope shape -- `version`/`isComplete` present,
        # `scenarioVersion`/`status`/`attemptId`/`decisionCount` absent --
        # and `p_attempt_id` must remain a separate, top-level RPC param
        # (never duplicated inside the envelope).
        run = self._start_run()
        params = build_start_or_resume_rpc_params_v2(
            run, user_email="learner@example.com", scenario_version_id=_new_attempt_id()
        )
        envelope = params["p_initial_serialized_state"]
        self.assertEqual(set(envelope.keys()), _FROZEN_ENVELOPE_KEYS)
        self.assertIn("version", envelope)
        self.assertIn("isComplete", envelope)
        self.assertIs(envelope["isComplete"], False)
        for forbidden in ("scenarioVersion", "status", "attemptId", "decisionCount"):
            self.assertNotIn(forbidden, envelope)
        self.assertIn("p_attempt_id", params)
        self.assertEqual(params["p_attempt_id"], run.attempt_id.lower())


class TestSubmitRpcParams(PersistenceV2TestCase):
    def test_AP_submit_rpc_params_contain_no_client_derived_hidden_values(self):
        run_before = self._run_happy_path(up_to=1)
        decision = ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-a")
        run_after = apply_decision_v2(run_before, decision)
        params = build_submit_decision_rpc_params_v2(
            run_before, run_after, decision, user_email="learner@example.com"
        )
        self.assertEqual(
            set(params.keys()),
            {
                "p_user_email",
                "p_attempt_id",
                "p_idempotency_key",
                "p_expected_sequence_number",
                "p_expected_scene_id",
                "p_selected_option_id",
                "p_request_fingerprint",
                "p_state_before",
                "p_state_after",
                "p_is_terminal",
                "p_resulting_scene_id",
                "p_terminal_ending_id",
                "p_terminal_result_snapshot",
            },
        )
        for forbidden in ("p_tier", "p_state", "p_flags", "p_score", "p_outcome", "p_routing"):
            self.assertNotIn(forbidden, params)
        json.dumps(params, allow_nan=False)

    def test_AP2_submit_rpc_params_for_terminal_decision(self):
        run_before = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS) - 1)
        last_seq, last_scene, last_option = HAPPY_PATH_DECISIONS[-1]
        decision = ScenarioDecisionInputV2(last_seq, last_scene, last_option)
        run_after = apply_decision_v2(run_before, decision)
        self.assertTrue(run_after.is_complete)
        params = build_submit_decision_rpc_params_v2(
            run_before, run_after, decision, user_email="learner@example.com"
        )
        self.assertTrue(params["p_is_terminal"])
        self.assertIsNone(params["p_resulting_scene_id"])
        self.assertIsNotNone(params["p_terminal_ending_id"])
        self.assertIsNotNone(params["p_terminal_result_snapshot"])

    def test_AP3_submit_rpc_uses_corrected_envelope_shape(self):
        # Item 16: both before/after envelopes embedded in the submit RPC
        # params must use the corrected frozen shape.
        run_before = self._run_happy_path(up_to=1)
        decision = ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-a")
        run_after = apply_decision_v2(run_before, decision)
        params = build_submit_decision_rpc_params_v2(
            run_before, run_after, decision, user_email="learner@example.com"
        )
        self.assertEqual(len(params), 13)
        for key in ("p_state_before", "p_state_after"):
            envelope = params[key]
            self.assertEqual(set(envelope.keys()), _FROZEN_ENVELOPE_KEYS)
            self.assertIn("version", envelope)
            self.assertIn("isComplete", envelope)
            for forbidden in ("scenarioVersion", "status", "attemptId", "decisionCount"):
                self.assertNotIn(forbidden, envelope)
        self.assertIs(params["p_state_before"]["isComplete"], False)
        self.assertIs(params["p_state_after"]["isComplete"], False)


class TestRpcResponseParsing(PersistenceV2TestCase):
    def _valid_start_row(self) -> dict:
        run = self._start_run()
        envelope = serialize_run_snapshot_v2(run)
        return {
            "attempt_id": self.attempt_id,
            "created": True,
            "scenario_id": _new_attempt_id(),
            "scenario_version_id": _new_attempt_id(),
            "status": "in_progress",
            "current_scene_id": run.current_scene_id,
            "next_sequence_number": run.expected_sequence_number,
            "serialized_engine_state": envelope,
            "engine_version": ENGINE_VERSION,
            "scenario_content_sha256": self.content.canonical_content_sha256,
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": None,
            "abandoned_at": None,
            "terminal_ending_id": None,
            "terminal_result_snapshot": None,
        }

    def _valid_submit_row(self) -> dict:
        run_before = self._run_happy_path(up_to=1)
        decision = ScenarioDecisionInputV2(2, "SC001-C02", "opt-sc001-c02-a")
        run_after = apply_decision_v2(run_before, decision)
        envelope = serialize_run_snapshot_v2(run_after)
        return {
            "decision_id": _new_attempt_id(),
            "attempt_id": self.attempt_id,
            "sequence_number": 2,
            "idempotent_replay": False,
            "attempt_status": "in_progress",
            "current_scene_id": run_after.current_scene_id,
            "next_sequence_number": run_after.expected_sequence_number,
            "serialized_engine_state": envelope,
            "completed_at": None,
            "terminal_ending_id": None,
            "terminal_result_snapshot": None,
        }

    def test_start_response_happy_path(self):
        result = parse_start_or_resume_rpc_response_v2(self._valid_start_row(), expected_attempt_id=self.attempt_id)
        self.assertEqual(result.attempt_id, self.attempt_id)
        self.assertTrue(result.created)

    def test_submit_response_happy_path(self):
        result = parse_submit_decision_rpc_response_v2(
            self._valid_submit_row(), expected_attempt_id=self.attempt_id
        )
        self.assertEqual(result.attempt_id, self.attempt_id)
        self.assertEqual(result.sequence_number, 2)

    def test_AQ_empty_rpc_response_rejected(self):
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_start_or_resume_rpc_response_v2([])
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_submit_decision_rpc_response_v2([])

    def test_AR_multi_row_rpc_response_rejected(self):
        row = self._valid_start_row()
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_start_or_resume_rpc_response_v2([row, row])
        submit_row = self._valid_submit_row()
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_submit_decision_rpc_response_v2([submit_row, submit_row])

    def test_AS_missing_rpc_response_field_rejected(self):
        row = self._valid_start_row()
        del row["attempt_id"]
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_start_or_resume_rpc_response_v2(row)

        submit_row = self._valid_submit_row()
        del submit_row["decision_id"]
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_submit_decision_rpc_response_v2(submit_row)

    def test_AT_wrong_rpc_response_type_rejected(self):
        row = self._valid_start_row()
        row["created"] = "false"  # truthy string, not an actual bool
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_start_or_resume_rpc_response_v2(row)

        row2 = self._valid_start_row()
        row2["next_sequence_number"] = "1"
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_start_or_resume_rpc_response_v2(row2)

    def test_identity_mismatch_rejected(self):
        row = self._valid_start_row()
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_start_or_resume_rpc_response_v2(row, expected_attempt_id=_new_attempt_id())

    def test_incompatible_engine_version_rejected(self):
        row = self._valid_start_row()
        row["engine_version"] = "SCENARIO_ENGINE_V1"
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_start_or_resume_rpc_response_v2(row)

        submit_row = self._valid_submit_row()
        submit_row["serialized_engine_state"] = dict(submit_row["serialized_engine_state"])
        submit_row["serialized_engine_state"]["engineVersion"] = "SCENARIO_ENGINE_V1"
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_submit_decision_rpc_response_v2(submit_row)

    def test_unexpected_status_rejected(self):
        row = self._valid_start_row()
        row["status"] = "not_a_real_status"
        with self.assertRaises(ScenarioPersistenceV2RpcResponseError):
            parse_start_or_resume_rpc_response_v2(row)

    def test_AT2_start_parser_deep_copy_isolation(self):
        # Item 17/19: mutating the raw RPC response (including nested
        # structures) after parsing must never change the parsed result,
        # and a shallow dict(...) copy would NOT be sufficient protection
        # here because `serialized_engine_state` contains nested dicts
        # (`state`, `counters`, `optionDisplayOrderByScene`, ...).
        row = self._valid_start_row()
        result = parse_start_or_resume_rpc_response_v2(row, expected_attempt_id=self.attempt_id)
        before = copy.deepcopy(result.serialized_engine_state)

        # Mutate the raw response's top-level field and a nested structure.
        row["serialized_engine_state"]["state"]["__mutated__"] = 999.0
        row["serialized_engine_state"]["flags"].append("__mutated_flag__")
        row["serialized_engine_state"] = {"replaced": True}

        self.assertEqual(result.serialized_engine_state, before)
        self.assertNotIn("__mutated__", result.serialized_engine_state["state"])
        self.assertNotIn("__mutated_flag__", result.serialized_engine_state["flags"])

    def test_AT3_submit_parser_deep_copy_isolation(self):
        # Item 18/19, and covers a nullable-nested-field case: mutating the
        # raw response's terminal_result_snapshot after parsing a completed
        # decision must not alter the parsed result.
        run_before = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS) - 1)
        last_seq, last_scene, last_option = HAPPY_PATH_DECISIONS[-1]
        decision = ScenarioDecisionInputV2(last_seq, last_scene, last_option)
        run_after = apply_decision_v2(run_before, decision)
        self.assertTrue(run_after.is_complete)
        envelope = serialize_run_snapshot_v2(run_after)
        row = {
            "decision_id": _new_attempt_id(),
            "attempt_id": self.attempt_id,
            "sequence_number": last_seq,
            "idempotent_replay": False,
            "attempt_status": "completed",
            "current_scene_id": None,
            "next_sequence_number": last_seq + 1,
            "serialized_engine_state": envelope,
            "completed_at": "2026-01-01T00:00:00Z",
            "terminal_ending_id": run_after.terminal_result.outcome_id,
            "terminal_result_snapshot": envelope["terminalResult"],
        }
        result = parse_submit_decision_rpc_response_v2(row, expected_attempt_id=self.attempt_id)
        state_before = copy.deepcopy(result.serialized_engine_state)
        terminal_before = copy.deepcopy(result.terminal_result_snapshot)

        row["serialized_engine_state"]["counters"]["__mutated__"] = 12345
        row["terminal_result_snapshot"]["displayScore"] = -999999

        self.assertEqual(result.serialized_engine_state, state_before)
        self.assertEqual(result.terminal_result_snapshot, terminal_before)
        self.assertNotIn("__mutated__", result.serialized_engine_state["counters"])
        self.assertNotEqual(result.terminal_result_snapshot["displayScore"], -999999)

    def test_AT4_parsed_result_mutation_does_not_affect_raw_response(self):
        # The other direction of item 19: mutating a mutable structure the
        # caller received back out must not corrupt the original raw
        # response either (both sides are fully independent copies).
        row = self._valid_start_row()
        raw_reference = copy.deepcopy(row)
        result = parse_start_or_resume_rpc_response_v2(row, expected_attempt_id=self.attempt_id)

        result.serialized_engine_state["state"]["__caller_mutated__"] = 1.0
        result.serialized_engine_state["flags"].append("__caller_mutated_flag__")

        self.assertEqual(row, raw_reference)


# ---------------------------------------------------------------------------
# AT5-AT12: serializer error wrapping (SIM-PERSIST-V2-04B correction)
# ---------------------------------------------------------------------------


class _NotADecision:
    """Deliberately shapeless stand-in for a malformed input object --
    missing every attribute a real dataclass instance would have."""


class TestSerializerErrorWrapping(PersistenceV2TestCase):
    def test_AT5_serialize_run_snapshot_wraps_attribute_error(self):
        with self.assertRaises(ScenarioPersistenceV2SerializationError) as ctx:
            serialize_run_snapshot_v2(_NotADecision())
        self.assertIn("malformed_input", str(ctx.exception))
        # A valid Engine V2 object must still serialize correctly --
        # wrapping malformed input must not have broken the happy path.
        serialize_run_snapshot_v2(self._start_run())

    def test_AT6_serialize_run_snapshot_wraps_none_input(self):
        with self.assertRaises(ScenarioPersistenceV2SerializationError):
            serialize_run_snapshot_v2(None)

    def test_AT7_serialize_decision_input_wraps_attribute_error(self):
        with self.assertRaises(ScenarioPersistenceV2SerializationError):
            serialize_decision_input_v2(_NotADecision())
        with self.assertRaises(ScenarioPersistenceV2SerializationError):
            serialize_decision_input_v2(None)
        # Valid input still works.
        serialize_decision_input_v2(ScenarioDecisionInputV2(1, "SC001-C01", "opt-a"))

    def test_AT8_serialize_learner_scene_view_wraps_attribute_error(self):
        with self.assertRaises(ScenarioPersistenceV2SerializationError) as ctx:
            serialize_learner_scene_view_v2(_NotADecision())
        self.assertIn("malformed_input", str(ctx.exception))
        with self.assertRaises(ScenarioPersistenceV2SerializationError):
            serialize_learner_scene_view_v2(None)
        # Valid input still works.
        serialize_learner_scene_view_v2(build_learner_scene_view(self._start_run()))

    def test_AT9_serialize_learner_terminal_view_wraps_attribute_error(self):
        with self.assertRaises(ScenarioPersistenceV2SerializationError) as ctx:
            serialize_learner_terminal_view_v2(_NotADecision())
        self.assertIn("malformed_input", str(ctx.exception))
        with self.assertRaises(ScenarioPersistenceV2SerializationError):
            serialize_learner_terminal_view_v2(None)
        # Valid input still works.
        completed_run = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS))
        serialize_learner_terminal_view_v2(build_learner_terminal_view(completed_run))

    def test_AT10_wrapper_does_not_swallow_domain_errors(self):
        # A deliberate, already-domain ScenarioPersistenceV2ValidationError
        # raised from *inside* a wrapped function (e.g. a bool sequence
        # number) must pass through unchanged, not be re-wrapped/masked as
        # a generic SerializationError.
        with self.assertRaises(ScenarioPersistenceV2ValidationError):
            serialize_decision_input_v2(ScenarioDecisionInputV2(True, "SC001-C01", "opt-a"))

    def test_AT11_wrapper_does_not_catch_keyboard_interrupt(self):
        class _RaisesKeyboardInterrupt:
            @property
            def content(self):
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            serialize_run_snapshot_v2(_RaisesKeyboardInterrupt())

    def test_AT12_wrapper_does_not_catch_system_exit(self):
        class _RaisesSystemExit:
            @property
            def content(self):
                raise SystemExit(1)

        with self.assertRaises(SystemExit):
            serialize_run_snapshot_v2(_RaisesSystemExit())


# ---------------------------------------------------------------------------
# AU: Engine V1 isolation
# ---------------------------------------------------------------------------


class TestEngineV1Isolation(unittest.TestCase):
    def test_AU_v1_persistence_module_does_not_import_v2(self):
        v1_source = (REPO_ROOT / "utils" / "scenario_persistence.py").read_text(encoding="utf-8")
        self.assertNotIn("scenario_persistence_v2", v1_source)

    def test_AU2_v1_persistence_tests_module_importable_standalone(self):
        # Structural smoke check only -- the actual regression run is the
        # combined pytest invocation this task's own TEST REQUIREMENTS
        # section specifies; this just proves this test module's own import
        # of utils.scenario_persistence_v2 does not corrupt utils.scenario_persistence's
        # own public surface.
        import utils.scenario_persistence as v1_module

        self.assertTrue(hasattr(v1_module, "start_or_resume_attempt"))
        self.assertTrue(hasattr(v1_module, "submit_decision"))
        self.assertIn("simulationId", v1_module.REQUIRED_SERIALIZED_STATE_KEYS)
        self.assertNotIn("schemaVersion", v1_module.REQUIRED_SERIALIZED_STATE_KEYS)


# ---------------------------------------------------------------------------
# AV-AX: learner-safe serialization + JSON-native output
# ---------------------------------------------------------------------------


_FORBIDDEN_HIDDEN_KEYS = (
    "evaluationTier",
    "debriefSeed",
    "stateDelta",
    "stateChanges",
    "flagsSet",
    "flagsCleared",
    "setFlags",
    "clearFlags",
    "presentedDialogueVariantId",
    "nextDialogueVariantId",
    "competencyTags",
    "classification",
    "classificationTrace",
    "severeCapId",
    "moderateCapId",
    "disqualifiedOutcomeIds",
    "guardTieBreakApplied",
    "correctiveRoute",
    "budgetCondition",
)


class TestLearnerSafeSerialization(PersistenceV2TestCase):
    def test_AV_learner_scene_serialization_excludes_hidden_fields(self):
        run = self._run_happy_path(up_to=1)
        scene_view = build_learner_scene_view(run)
        serialized = serialize_learner_scene_view_v2(scene_view)
        found_keys = _recursive_keys(serialized)
        for forbidden in _FORBIDDEN_HIDDEN_KEYS:
            self.assertNotIn(forbidden, found_keys)

    def test_AW_learner_terminal_serialization_excludes_internal_trace(self):
        run = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS))
        terminal_view = build_learner_terminal_view(run)
        serialized = serialize_learner_terminal_view_v2(terminal_view)
        self.assertEqual(set(serialized.keys()), {"outcomeId", "outcomeTitle", "narrative", "displayScore"})
        found_keys = _recursive_keys(serialized)
        for forbidden in _FORBIDDEN_HIDDEN_KEYS:
            self.assertNotIn(forbidden, found_keys)

    def test_AX_json_output_passes_allow_nan_false(self):
        run = self._run_happy_path(up_to=len(HAPPY_PATH_DECISIONS))
        envelope = serialize_run_snapshot_v2(run)
        json.dumps(envelope, allow_nan=False)

        in_progress_run = self._run_happy_path(up_to=1)
        in_progress_envelope = serialize_run_snapshot_v2(in_progress_run)
        json.dumps(in_progress_envelope, allow_nan=False)

        scene_view = serialize_learner_scene_view_v2(build_learner_scene_view(in_progress_run))
        json.dumps(scene_view, allow_nan=False)

        terminal_view = serialize_learner_terminal_view_v2(build_learner_terminal_view(run))
        json.dumps(terminal_view, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
