"""
Tests for V58-QUALITY-04A dual-engine benchmark execution readiness.

Covers the ground-truth safety gate, the real (non-mock) legacy engine
adapter, the architecturally-blocked V48 adapter, prediction-artifact
generation/coverage-validation, scoring, and the CLI's safety posture.
Uses only fakes/stubs and temporary directories — no live provider or
database calls occur anywhere in this file.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.benchmark_sme_review import (
    build_export_rows,
    build_reviewed_fixture,
    compute_source_fixture_sha256,
    load_source_fixture,
    validate_sme_review_rows,
)
from workers.llm_providers import LlmProviderError, LlmResponse
from workers.quality_benchmark import DEFAULT_FIXTURE_PATH, load_benchmark_fixture
from workers.quality_benchmark_execution import (
    ENGINE_LEGACY,
    ENGINE_V48,
    CasePrediction,
    EngineAdapterUnavailableError,
    GroundTruthNotFinalizedError,
    LegacyEngineAdapter,
    PredictionArtifactError,
    QualityBenchmarkExecutionError,
    V48EngineAdapter,
    assert_finalized_sme_ground_truth,
    generate_predictions,
    load_benchmark_case_fixture,
    load_finalized_sme_ground_truth_fixture,
    load_prediction_artifact,
    score_predictions,
    validate_prediction_coverage,
    write_prediction_artifact,
    write_scorecard,
)
import scripts.v58_run_quality_benchmark_engines as cli

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AI_DRAFTED_FIXTURE_PATH = _REPO_ROOT / "workers" / "fixtures" / "quality_benchmark_v1.json"


def _approve_row(row: dict) -> dict:
    filled = dict(row)
    filled["sme_decision"] = "approve"
    filled["confidence"] = "high"
    return filled


def _build_all_approve_reviewed_fixture() -> dict:
    """Build a genuine, fully-finalized SME-reviewed fixture from the real
    40-case AI-drafted pilot benchmark, approving every case as-is (the
    simplest possible finalized review: the effective label equals the
    original AI-drafted label for every case)."""
    fixture = load_source_fixture(_AI_DRAFTED_FIXTURE_PATH)
    source_hash = compute_source_fixture_sha256(_AI_DRAFTED_FIXTURE_PATH)
    rows = [_approve_row(row) for row in build_export_rows(fixture, source_fixture_sha256=source_hash)]
    report = validate_sme_review_rows(rows, fixture, source_fixture_path=_AI_DRAFTED_FIXTURE_PATH)
    assert report.is_finalizable, report.errors
    return build_reviewed_fixture(
        fixture,
        rows,
        report,
        reviewer_id="test-sme-reviewer",
        review_imported_at_utc="2026-07-05T20:00:00Z",
    )


class _FakeAdapter:
    """Minimal adapter double conforming to the engine-adapter surface."""

    engine_id = "fake"

    def __init__(self, *, fail_case_id: str | None = None):
        self._fail_case_id = fail_case_id

    def describe_config(self):
        return {
            "engine_id": self.engine_id,
            "engine_version": "fake-v1",
            "provider_id": "fake-provider",
            "model_id": "fake-model",
            "prompt_version": "fake-prompt-v1",
            "ruleset_version": "fake-ruleset-v1",
            "evidence_config_id": "fake-evidence-v1",
        }

    def generate_prediction(self, case) -> CasePrediction:
        if case["case_id"] == self._fail_case_id:
            raise RuntimeError("synthetic per-case failure")
        codes = list(case.get("expected_finding_codes") or [])
        materiality = case.get("expected_materiality")
        findings = [{"finding_code": c, "materiality": materiality or "blocking"} for c in codes]
        return CasePrediction(
            case_id=case["case_id"],
            finding_codes=codes,
            materiality=materiality,
            approved=bool(case.get("known_good")),
            raw_output={"findings": findings},
        )


def _clean_configuration_identity(*, engine_id: str, engine_version: str, source_fixture_sha256: str) -> dict:
    """A complete, internally-consistent configuration identity (V58-QUALITY-04E-R2)."""
    return {
        "engine_id": engine_id,
        "engine_version": engine_version,
        "provider_id": "fake-provider",
        "model_id": "fake-model",
        "prompt_version": "fake-prompt-v1",
        "ruleset_version": "fake-ruleset-v1",
        "evidence_config_id": "fake-evidence-v1",
        "source_fixture_sha256": source_fixture_sha256,
    }


def _perfect_predictions_artifact(fixture: dict, *, engine_id: str = "fake") -> dict:
    """Build a prediction artifact that exactly reproduces each case's
    effective ground-truth label, for precise scoring-math assertions."""
    predictions = []
    for case in fixture["cases"]:
        codes = list(case.get("expected_finding_codes") or [])
        materiality = case.get("expected_materiality")
        findings = [{"finding_code": c, "materiality": materiality or "blocking"} for c in codes]
        predictions.append(
            {
                "case_id": case["case_id"],
                "finding_codes": codes,
                "materiality": materiality,
                "approved": bool(case.get("known_good")),
                "raw_output": {"findings": findings},
                "error": None,
            }
        )
    engine_version = "fake-v1"
    source_fixture_sha256 = fixture.get("source_fixture_sha256", "deadbeef")
    return {
        "schema_version": "quality-benchmark-prediction-v1",
        "engine_id": engine_id,
        "engine_version": engine_version,
        "configuration_identity": _clean_configuration_identity(
            engine_id=engine_id, engine_version=engine_version, source_fixture_sha256=source_fixture_sha256
        ),
        "provider_config": {},
        "generated_at_utc": "2026-07-05T20:00:00Z",
        "source_fixture_path": str(_AI_DRAFTED_FIXTURE_PATH),
        "source_fixture_sha256": source_fixture_sha256,
        "case_count": len(fixture["cases"]),
        "predictions": predictions,
        "error_case_count": 0,
    }


class TestGroundTruthGate(unittest.TestCase):
    """Critical integrity rule: scoring must fail closed."""

    @classmethod
    def setUpClass(cls):
        cls.reviewed = _build_all_approve_reviewed_fixture()

    def test_ai_drafted_fixture_is_rejected(self):
        fixture = load_benchmark_fixture(_AI_DRAFTED_FIXTURE_PATH)
        with self.assertRaises(GroundTruthNotFinalizedError):
            assert_finalized_sme_ground_truth(fixture)

    def test_ai_drafted_fixture_rejected_via_loader(self):
        with self.assertRaises(GroundTruthNotFinalizedError):
            load_finalized_sme_ground_truth_fixture(_AI_DRAFTED_FIXTURE_PATH)

    def test_default_harness_fixture_is_also_rejected(self):
        # workers/fixtures/quality_benchmark_harness_v0.json is a self-test
        # fixture, never SME-reviewed; it must never score as ground truth.
        with self.assertRaises(GroundTruthNotFinalizedError):
            load_finalized_sme_ground_truth_fixture(DEFAULT_FIXTURE_PATH)

    def test_genuine_finalized_fixture_is_accepted(self):
        assert_finalized_sme_ground_truth(self.reviewed)  # must not raise

    def test_rejects_when_review_status_not_complete(self):
        broken = copy.deepcopy(self.reviewed)
        broken["sme_review_status"] = "in_progress"
        with self.assertRaises(GroundTruthNotFinalizedError):
            assert_finalized_sme_ground_truth(broken)

    def test_rejects_when_reviewer_id_blank(self):
        broken = copy.deepcopy(self.reviewed)
        broken["sme_reviewer_id"] = "  "
        with self.assertRaises(GroundTruthNotFinalizedError):
            assert_finalized_sme_ground_truth(broken)

    def test_rejects_when_source_hash_missing(self):
        broken = copy.deepcopy(self.reviewed)
        del broken["source_fixture_sha256"]
        with self.assertRaises(GroundTruthNotFinalizedError):
            assert_finalized_sme_ground_truth(broken)

    def test_rejects_when_review_imported_at_missing(self):
        broken = copy.deepcopy(self.reviewed)
        broken["review_imported_at_utc"] = ""
        with self.assertRaises(GroundTruthNotFinalizedError):
            assert_finalized_sme_ground_truth(broken)

    def test_rejects_when_rejected_cases_present(self):
        broken = copy.deepcopy(self.reviewed)
        broken["sme_review_summary"]["rejected_case_ids"] = ["qbv1-001"]
        with self.assertRaises(GroundTruthNotFinalizedError):
            assert_finalized_sme_ground_truth(broken)

    def test_rejects_when_unresolved_second_review_cases_present(self):
        broken = copy.deepcopy(self.reviewed)
        broken["sme_review_summary"]["unresolved_second_review_case_ids"] = ["qbv1-002"]
        with self.assertRaises(GroundTruthNotFinalizedError):
            assert_finalized_sme_ground_truth(broken)

    def test_rejects_when_case_missing_sme_review_record(self):
        broken = copy.deepcopy(self.reviewed)
        del broken["cases"][0]["sme_review"]
        with self.assertRaises(GroundTruthNotFinalizedError):
            assert_finalized_sme_ground_truth(broken)

    def test_rejects_when_case_missing_ai_drafted_provenance(self):
        broken = copy.deepcopy(self.reviewed)
        del broken["cases"][0]["ai_drafted_reviewer_label"]
        with self.assertRaises(GroundTruthNotFinalizedError):
            assert_finalized_sme_ground_truth(broken)


class TestLegacyEngineAdapterDefaultMode(unittest.TestCase):
    """No provider injected: deterministic-only, always safe, never live."""

    def setUp(self):
        self.fixture = load_benchmark_fixture(DEFAULT_FIXTURE_PATH)
        self.adapter = LegacyEngineAdapter()

    def test_known_good_case_has_no_findings_and_is_approved(self):
        case = next(c for c in self.fixture["cases"] if c["case_id"] == "harness-001-known-good")
        prediction = self.adapter.generate_prediction(case)
        self.assertEqual(prediction.finding_codes, [])
        self.assertTrue(prediction.approved)
        self.assertIsNone(prediction.error)
        self.assertTrue(prediction.raw_output["llm_skipped"])

    def test_explanation_defect_detected_deterministically(self):
        case = next(
            c for c in self.fixture["cases"] if c["case_id"] == "harness-006-explanation-defect"
        )
        prediction = self.adapter.generate_prediction(case)
        self.assertIn("EXPLANATION_MISSING", prediction.finding_codes)
        self.assertEqual(prediction.materiality, "blocking")
        self.assertFalse(prediction.approved)
        self.assertIsNone(prediction.error)

    def test_describe_config_reports_non_live(self):
        config = self.adapter.describe_config()
        self.assertFalse(config["live"])
        self.assertEqual(config["engine_id"], ENGINE_LEGACY)

    def test_describe_config_reports_explicit_deterministic_only_markers(self):
        # V58-QUALITY-04E-R2 correction 6: these must be explicit, never
        # inferred/omitted, and must be true statements about what actually
        # ran (no provider/model/prompt was used).
        config = self.adapter.describe_config()
        self.assertEqual(config["provider_id"], "deterministic-only")
        self.assertEqual(config["model_id"], "not-applicable")
        self.assertEqual(config["prompt_version"], "not-applicable")
        self.assertTrue(config["ruleset_version"])
        self.assertTrue(config["evidence_config_id"])


class TestLegacyEngineAdapterWithFakeProvider(unittest.TestCase):
    """A provider is injected (simulating live mode) but is a pure fake —
    no network access ever occurs in this test."""

    def setUp(self):
        self.fixture = load_benchmark_fixture(DEFAULT_FIXTURE_PATH)

    def _fake_provider_returning(self, findings: list[dict]):
        def provider(**kwargs):
            return LlmResponse(
                parsed_response={"findings": findings},
                input_tokens=10,
                output_tokens=5,
                actual_cost_usd=0.001,
                provider_request_id="fake-req-1",
            )

        return provider

    def test_live_mode_merges_llm_findings_when_prompt_present(self):
        case = dict(
            next(c for c in self.fixture["cases"] if c["case_id"] == "harness-001-known-good")
        )
        case["user_prompt"] = "Audit this question."
        llm_finding = {
            "finding_code": "SOURCE_SUPPORT_WEAK",
            "finding_type": "source_support",
            "severity": "medium",
            "title": "Weak source support",
            "description": "The explanation is not well supported by evidence.",
        }
        adapter = LegacyEngineAdapter(llm_provider=self._fake_provider_returning([llm_finding]))
        prediction = adapter.generate_prediction(case)
        self.assertIn("SOURCE_SUPPORT_WEAK", prediction.finding_codes)
        self.assertIsNone(prediction.error)
        self.assertFalse(prediction.raw_output["llm_skipped"])

    def test_live_mode_without_user_prompt_reports_visible_error(self):
        case = next(c for c in self.fixture["cases"] if c["case_id"] == "harness-001-known-good")
        self.assertNotIn("user_prompt", case)
        adapter = LegacyEngineAdapter(llm_provider=self._fake_provider_returning([]))
        prediction = adapter.generate_prediction(case)
        self.assertIsNotNone(prediction.error)
        self.assertIn("user_prompt", prediction.error)

    def test_provider_failure_is_captured_not_dropped(self):
        case = dict(
            next(c for c in self.fixture["cases"] if c["case_id"] == "harness-001-known-good")
        )
        case["user_prompt"] = "Audit this question."

        def failing_provider(**kwargs):
            raise LlmProviderError("simulated network failure")

        adapter = LegacyEngineAdapter(llm_provider=failing_provider)
        prediction = adapter.generate_prediction(case)
        self.assertIsNotNone(prediction.error)
        self.assertIn("simulated network failure", prediction.error)
        self.assertEqual(prediction.case_id, case["case_id"])

    def test_describe_config_reports_live_when_provider_injected(self):
        adapter = LegacyEngineAdapter(
            llm_provider=self._fake_provider_returning([]), provider_id="fake-live-provider"
        )
        self.assertTrue(adapter.describe_config()["live"])

    def test_describe_config_live_requires_explicit_provider_id(self):
        adapter = LegacyEngineAdapter(llm_provider=self._fake_provider_returning([]))
        with self.assertRaises(QualityBenchmarkExecutionError):
            adapter.describe_config()


class TestV48EngineAdapterBlocked(unittest.TestCase):

    def setUp(self):
        self.fixture = load_benchmark_fixture(DEFAULT_FIXTURE_PATH)
        self.adapter = V48EngineAdapter()

    def test_generate_prediction_raises_unavailable_with_reason_and_follow_up(self):
        case = self.fixture["cases"][0]
        with self.assertRaises(EngineAdapterUnavailableError) as ctx:
            self.adapter.generate_prediction(case)
        self.assertIn("question_version_id", ctx.exception.reason)
        self.assertTrue(ctx.exception.follow_up)

    def test_generate_predictions_propagates_block_without_partial_artifact(self):
        with self.assertRaises(EngineAdapterUnavailableError):
            generate_predictions(self.fixture, self.adapter, source_fixture_path=DEFAULT_FIXTURE_PATH)

    def test_describe_config_reports_blocked_status(self):
        self.assertEqual(self.adapter.describe_config()["status"], "blocked")


class TestGeneratePredictionsRoundTrip(unittest.TestCase):

    def setUp(self):
        self.fixture = load_benchmark_fixture(DEFAULT_FIXTURE_PATH)

    def test_artifact_schema_and_hash(self):
        adapter = _FakeAdapter()
        artifact = generate_predictions(self.fixture, adapter, source_fixture_path=DEFAULT_FIXTURE_PATH)
        self.assertEqual(artifact["schema_version"], "quality-benchmark-prediction-v1")
        self.assertEqual(artifact["engine_id"], "fake")
        self.assertEqual(artifact["case_count"], len(self.fixture["cases"]))
        self.assertEqual(len(artifact["predictions"]), len(self.fixture["cases"]))
        self.assertEqual(artifact["error_case_count"], 0)
        from workers.quality_benchmark_execution import _sha256_file  # local helper

        self.assertEqual(artifact["source_fixture_sha256"], _sha256_file(DEFAULT_FIXTURE_PATH))

    def test_artifact_carries_complete_configuration_identity(self):
        # V58-QUALITY-04E-R2: prediction artifact is authoritative — every
        # required identity dimension must be present and non-blank.
        adapter = _FakeAdapter()
        artifact = generate_predictions(self.fixture, adapter, source_fixture_path=DEFAULT_FIXTURE_PATH)
        identity = artifact["configuration_identity"]
        for field in (
            "engine_id",
            "engine_version",
            "provider_id",
            "model_id",
            "prompt_version",
            "ruleset_version",
            "evidence_config_id",
            "source_fixture_sha256",
        ):
            self.assertTrue(identity.get(field), f"{field} must be non-blank")
        self.assertEqual(identity["engine_id"], artifact["engine_id"])
        self.assertEqual(identity["source_fixture_sha256"], artifact["source_fixture_sha256"])

    def test_legacy_deterministic_artifact_carries_explicit_not_applicable_markers(self):
        fixture = load_benchmark_fixture(DEFAULT_FIXTURE_PATH)
        artifact = generate_predictions(fixture, LegacyEngineAdapter(), source_fixture_path=DEFAULT_FIXTURE_PATH)
        identity = artifact["configuration_identity"]
        self.assertEqual(identity["provider_id"], "deterministic-only")
        self.assertEqual(identity["model_id"], "not-applicable")
        self.assertEqual(identity["prompt_version"], "not-applicable")

    def test_generation_refuses_incomplete_configuration_identity(self):
        class _IncompleteAdapter:
            engine_id = "incomplete"

            def describe_config(self):
                return {"engine_id": self.engine_id, "engine_version": "v1", "provider_id": ""}

            def generate_prediction(self, case):
                return CasePrediction(case_id=case["case_id"])

        with self.assertRaises(PredictionArtifactError):
            generate_predictions(self.fixture, _IncompleteAdapter(), source_fixture_path=DEFAULT_FIXTURE_PATH)

    def test_per_case_exception_is_isolated_not_fatal(self):
        failing_case_id = self.fixture["cases"][0]["case_id"]
        adapter = _FakeAdapter(fail_case_id=failing_case_id)
        artifact = generate_predictions(self.fixture, adapter, source_fixture_path=DEFAULT_FIXTURE_PATH)
        self.assertEqual(artifact["error_case_count"], 1)
        self.assertEqual(len(artifact["predictions"]), len(self.fixture["cases"]))
        failing_entry = next(p for p in artifact["predictions"] if p["case_id"] == failing_case_id)
        self.assertIn("synthetic per-case failure", failing_entry["error"])

    def test_write_then_load_round_trip(self):
        adapter = _FakeAdapter()
        artifact = generate_predictions(self.fixture, adapter, source_fixture_path=DEFAULT_FIXTURE_PATH)
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "predictions.json"
            write_prediction_artifact(out_path, artifact)
            with self.assertRaises(PredictionArtifactError):
                write_prediction_artifact(out_path, artifact)  # refuses overwrite
            write_prediction_artifact(out_path, artifact, allow_overwrite=True)
            loaded = load_prediction_artifact(out_path)
            self.assertEqual(loaded["case_count"], artifact["case_count"])


class TestValidatePredictionCoverage(unittest.TestCase):

    def setUp(self):
        self.fixture = load_benchmark_fixture(DEFAULT_FIXTURE_PATH)
        adapter = _FakeAdapter()
        self.artifact = generate_predictions(self.fixture, adapter, source_fixture_path=DEFAULT_FIXTURE_PATH)

    def test_valid_coverage_passes(self):
        coverage = validate_prediction_coverage(self.fixture, self.artifact)
        self.assertEqual(coverage["expected_case_count"], len(self.fixture["cases"]))
        self.assertEqual(coverage["predicted_case_count"], len(self.fixture["cases"]))

    def test_missing_case_rejected(self):
        broken = copy.deepcopy(self.artifact)
        broken["predictions"].pop()
        with self.assertRaises(PredictionArtifactError):
            validate_prediction_coverage(self.fixture, broken)

    def test_unknown_case_rejected(self):
        broken = copy.deepcopy(self.artifact)
        broken["predictions"].append({"case_id": "not-a-real-case", "raw_output": {"findings": []}})
        with self.assertRaises(PredictionArtifactError):
            validate_prediction_coverage(self.fixture, broken)

    def test_duplicate_case_rejected(self):
        broken = copy.deepcopy(self.artifact)
        broken["predictions"].append(dict(broken["predictions"][0]))
        with self.assertRaises(PredictionArtifactError):
            validate_prediction_coverage(self.fixture, broken)


class TestScorePredictions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.reviewed = _build_all_approve_reviewed_fixture()

    def test_rejects_scoring_against_ai_drafted_fixture(self):
        ai_drafted = load_benchmark_fixture(_AI_DRAFTED_FIXTURE_PATH)
        artifact = _perfect_predictions_artifact(ai_drafted)
        with self.assertRaises(GroundTruthNotFinalizedError):
            score_predictions(ai_drafted, artifact)

    def test_perfect_predictions_yield_perfect_scores(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = score_predictions(self.reviewed, artifact)

        self.assertEqual(scorecard["schema_version"], "quality-benchmark-scorecard-v1")
        self.assertEqual(scorecard["case_count"], len(self.reviewed["cases"]))
        self.assertEqual(scorecard["scored_case_count"], len(self.reviewed["cases"]))
        self.assertEqual(scorecard["unscored_case_count"], 0)
        self.assertEqual(scorecard["metrics"]["false_approvals"], 0)
        self.assertEqual(scorecard["metrics"]["false_rejections"], 0)
        self.assertEqual(scorecard["known_good_approval_rate"], 1.0)
        self.assertEqual(scorecard["defective_case_rejection_rate"], 1.0)
        if scorecard["metrics"]["blocking_category_total"] > 0:
            self.assertEqual(scorecard["metrics"]["blocking_category_recall"], 1.0)

    def test_scorecard_identity_matches_prediction_artifact(self):
        # V58-QUALITY-04E-R2 correction 2: identity is copied unchanged.
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = score_predictions(self.reviewed, artifact)
        self.assertEqual(scorecard["configuration_identity"], artifact["configuration_identity"])

    def test_scoring_rejects_missing_configuration_identity(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        del artifact["configuration_identity"]
        with self.assertRaises(PredictionArtifactError):
            score_predictions(self.reviewed, artifact)

    def test_scoring_rejects_incomplete_configuration_identity(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        artifact["configuration_identity"]["model_id"] = ""
        with self.assertRaises(PredictionArtifactError):
            score_predictions(self.reviewed, artifact)

    def test_scoring_rejects_engine_id_mismatch_between_identity_and_top_level(self):
        # Simulates a hand-edited artifact where only one copy of engine_id
        # was updated after generation.
        artifact = _perfect_predictions_artifact(self.reviewed)
        artifact["engine_id"] = "tampered-engine"
        with self.assertRaises(PredictionArtifactError):
            score_predictions(self.reviewed, artifact)

    def test_scoring_rejects_altered_provider_or_model_inconsistent_with_provider_config(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        # provider_config independently records the same facts; a caller
        # relabeling only configuration_identity is a detectable tamper.
        artifact["provider_config"] = dict(artifact["configuration_identity"])
        artifact["configuration_identity"]["model_id"] = "relabeled-model"
        with self.assertRaises(PredictionArtifactError):
            score_predictions(self.reviewed, artifact)

    def test_scoring_rejects_altered_prompt_ruleset_or_evidence_identity(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        artifact["provider_config"] = dict(artifact["configuration_identity"])
        for field in ("prompt_version", "ruleset_version", "evidence_config_id"):
            tampered = copy.deepcopy(artifact)
            tampered["configuration_identity"][field] = "relabeled-value"
            with self.assertRaises(PredictionArtifactError):
                score_predictions(self.reviewed, tampered)

    def test_scoring_rejects_source_fixture_hash_mismatch_between_identity_and_top_level(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        artifact["source_fixture_sha256"] = "0" * 64
        with self.assertRaises(PredictionArtifactError):
            score_predictions(self.reviewed, artifact)

    def test_error_case_is_excluded_and_counted_as_unscored(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        artifact["predictions"][0]["error"] = "simulated execution failure"
        scorecard = score_predictions(self.reviewed, artifact)
        self.assertEqual(scorecard["unscored_case_count"], 1)
        self.assertEqual(
            scorecard["unscored_case_ids"], [artifact["predictions"][0]["case_id"]]
        )
        self.assertEqual(scorecard["scored_case_count"], len(self.reviewed["cases"]) - 1)

    def test_false_approval_on_defective_case_reduces_rejection_rate(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        defective_case = next(c for c in self.reviewed["cases"] if not c["known_good"])
        entry = next(p for p in artifact["predictions"] if p["case_id"] == defective_case["case_id"])
        entry["finding_codes"] = []
        entry["raw_output"]["findings"] = []
        scorecard = score_predictions(self.reviewed, artifact)
        self.assertGreaterEqual(scorecard["metrics"]["false_approvals"], 1)
        self.assertLess(scorecard["defective_case_rejection_rate"], 1.0)

    def test_false_rejection_on_known_good_case_reduces_approval_rate(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        known_good_case = next(c for c in self.reviewed["cases"] if c["known_good"])
        entry = next(p for p in artifact["predictions"] if p["case_id"] == known_good_case["case_id"])
        entry["finding_codes"] = ["WRONG_ANSWER_KEY"]
        entry["raw_output"]["findings"] = [{"finding_code": "WRONG_ANSWER_KEY", "materiality": "blocking"}]
        scorecard = score_predictions(self.reviewed, artifact)
        self.assertGreaterEqual(scorecard["metrics"]["false_rejections"], 1)
        self.assertLess(scorecard["known_good_approval_rate"], 1.0)
        self.assertIn("WRONG_ANSWER_KEY", scorecard["finding_category_metrics"])
        self.assertGreaterEqual(
            scorecard["finding_category_metrics"]["WRONG_ANSWER_KEY"]["false_positives"], 1
        )

    def test_rejects_case_coverage_mismatch_even_for_finalized_fixture(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        artifact["predictions"].pop()
        with self.assertRaises(PredictionArtifactError):
            score_predictions(self.reviewed, artifact)

    def test_scorecard_write_and_dump_round_trip(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = score_predictions(self.reviewed, artifact)
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "scorecard.json"
            write_scorecard(out_path, scorecard)
            reloaded = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["case_count"], scorecard["case_count"])


class TestCli(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.reviewed = _build_all_approve_reviewed_fixture()

    def _run(self, argv, *, patch_pytest_guard: bool = True):
        buf = io.StringIO()
        if patch_pytest_guard:
            with patch("scripts.v58_run_quality_benchmark_engines._running_under_pytest", return_value=False):
                with redirect_stdout(buf):
                    code = cli.main(argv)
        else:
            with redirect_stdout(buf):
                code = cli.main(argv)
        return code, buf.getvalue()

    def test_refuses_under_pytest_by_default(self):
        code, _ = self._run(["generate", "--engine", "legacy", "--fixture", "x", "--output", "y"], patch_pytest_guard=False)
        self.assertEqual(code, 2)

    def test_generate_v48_reports_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = str(Path(tmpdir) / "predictions.json")
            code, output = self._run([
                "generate", "--engine", ENGINE_V48,
                "--fixture", str(DEFAULT_FIXTURE_PATH),
                "--output", out_path,
            ])
            self.assertEqual(code, 3)
            self.assertIn("BLOCKED", output)
            self.assertFalse(Path(out_path).exists())

    def test_generate_legacy_default_mode_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = str(Path(tmpdir) / "predictions.json")
            code, output = self._run([
                "generate", "--engine", ENGINE_LEGACY,
                "--fixture", str(DEFAULT_FIXTURE_PATH),
                "--output", out_path,
            ])
            self.assertEqual(code, 0)
            self.assertTrue(Path(out_path).exists())

    def test_generate_live_without_authorization_refused(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CERTBOUND_ALLOW_LIVE_AI_TEST", None)
            code, output = self._run([
                "generate", "--engine", ENGINE_LEGACY,
                "--fixture", str(DEFAULT_FIXTURE_PATH),
                "--output", "unused.json",
                "--live",
            ])
        self.assertEqual(code, 1)
        self.assertIn("Refusing", output)

    def test_generate_live_even_with_authorization_unimplemented_in_this_task(self):
        with patch.dict(os.environ, {"CERTBOUND_ALLOW_LIVE_AI_TEST": "1"}, clear=False):
            code, output = self._run([
                "generate", "--engine", ENGINE_LEGACY,
                "--fixture", str(DEFAULT_FIXTURE_PATH),
                "--output", "unused.json",
                "--live",
            ])
        self.assertEqual(code, 1)
        self.assertIn("not implemented", output)

    def test_validate_and_score_pipeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reviewed_path = Path(tmpdir) / "reviewed.json"
            reviewed_path.write_text(json.dumps(self.reviewed), encoding="utf-8")
            predictions_path = Path(tmpdir) / "predictions.json"
            artifact = _perfect_predictions_artifact(self.reviewed)
            predictions_path.write_text(json.dumps(artifact), encoding="utf-8")

            code, output = self._run([
                "validate",
                "--fixture", str(reviewed_path),
                "--predictions", str(predictions_path),
            ])
            self.assertEqual(code, 0)
            self.assertIn("coverage: OK", output)

            scorecard_path = Path(tmpdir) / "scorecard.json"
            code, output = self._run([
                "score",
                "--fixture", str(reviewed_path),
                "--predictions", str(predictions_path),
                "--output", str(scorecard_path),
            ])
            self.assertEqual(code, 0)
            self.assertTrue(scorecard_path.exists())

    def test_score_cli_rejects_unknown_engine_configuration_json_flag(self):
        # V58-QUALITY-04E-R2 correction 4: --engine-configuration-json was
        # removed entirely (not merely undocumented) - a scoring-time
        # operator can no longer supply/override configuration identity.
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("scripts.v58_run_quality_benchmark_engines._running_under_pytest", return_value=False):
                with self.assertRaises(SystemExit):
                    cli.main([
                        "score",
                        "--fixture", "unused.json",
                        "--predictions", "unused.json",
                        "--output", str(Path(tmpdir) / "scorecard.json"),
                        "--engine-configuration-json", "unused.json",
                    ])

    def test_score_cli_classification_uses_artifact_identity_with_no_identity_flag(self):
        # score --benchmark-tier classifies purely from the artifact's own
        # configuration_identity; there is no flag to supply one.
        with tempfile.TemporaryDirectory() as tmpdir:
            reviewed_path = Path(tmpdir) / "reviewed.json"
            reviewed_path.write_text(json.dumps(self.reviewed), encoding="utf-8")
            predictions_path = Path(tmpdir) / "predictions.json"
            artifact = _perfect_predictions_artifact(self.reviewed)
            predictions_path.write_text(json.dumps(artifact), encoding="utf-8")
            scorecard_path = Path(tmpdir) / "scorecard.json"

            code, output = self._run([
                "score",
                "--fixture", str(reviewed_path),
                "--predictions", str(predictions_path),
                "--output", str(scorecard_path),
                "--benchmark-tier", "pilot",
            ])
            # This fixture has 40 finalized cases, so it is pilot-eligible;
            # the run should reach an actual classification (not a
            # missing-flag refusal), proving identity came from the
            # artifact alone.
            self.assertIn("configuration_identity", output)
            self.assertIn(artifact["configuration_identity"]["provider_id"], output)
            self.assertNotIn("engine-configuration-json", output)

    def test_score_rejects_ai_drafted_fixture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_drafted = load_benchmark_fixture(_AI_DRAFTED_FIXTURE_PATH)
            predictions_path = Path(tmpdir) / "predictions.json"
            artifact = _perfect_predictions_artifact(ai_drafted)
            predictions_path.write_text(json.dumps(artifact), encoding="utf-8")
            scorecard_path = Path(tmpdir) / "scorecard.json"

            code, output = self._run([
                "score",
                "--fixture", str(_AI_DRAFTED_FIXTURE_PATH),
                "--predictions", str(predictions_path),
                "--output", str(scorecard_path),
            ])
            self.assertEqual(code, 1)
            self.assertIn("not finalized SME ground truth", output)
            self.assertFalse(scorecard_path.exists())


if __name__ == "__main__":
    unittest.main()
