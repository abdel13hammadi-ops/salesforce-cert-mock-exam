"""
Tests for V58-QUALITY-04E precommitted benchmark acceptance policy
(``workers.quality_benchmark_policy.classify_scorecard``), including the
V58-QUALITY-04E-R1 fail-closed corrections (exact pilot size, additive-only
safety categories, complete configuration identity, and an end-to-end proof
that the actual repository fixture cannot PASS/CONDITIONAL PASS) and the
V58-QUALITY-04E-R2 correction (configuration identity is read exclusively
from scorecard["configuration_identity"] - fixture_metadata carries none at
all, so there is nothing for a caller to override at classification time).

Uses only in-memory fixtures/fakes and hand-constructed scorecards/fixture
metadata — no live provider, database, or worker calls occur anywhere in
this file, and no real launch-scale (100+ case) benchmark is generated
(that would require actual engine execution, which this task explicitly
forbids); launch-tier scenarios are exercised against small, synthetic,
schema-valid scorecard/fixture_metadata dicts instead.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.benchmark_sme_review import (
    build_export_rows,
    build_reviewed_fixture,
    compute_source_fixture_sha256,
    load_source_fixture,
    validate_sme_review_rows,
)
from workers.quality_benchmark import _build_case_result, load_benchmark_fixture
from workers.quality_benchmark_execution import (
    GroundTruthNotFinalizedError,
    PredictionArtifactError,
    load_finalized_sme_ground_truth_fixture,
    score_predictions,
)
from workers.quality_benchmark_policy import (
    CLASSIFICATION_CONDITIONAL_PASS,
    CLASSIFICATION_FAIL,
    CLASSIFICATION_INVALID_RUN,
    CLASSIFICATION_PASS,
    LAUNCH_PASS_LANGUAGE,
    PILOT_REQUIRED_CASE_COUNT,
    POLICY_VERSION,
    REQUIRED_LAUNCH_SAFETY_CATEGORIES,
    PolicyInputError,
    _PROHIBITED_LAUNCH_CLAIM_PHRASES,
    classify_scorecard,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AI_DRAFTED_FIXTURE_PATH = _REPO_ROOT / "workers" / "fixtures" / "quality_benchmark_v1.json"

_CLEAN_ENGINE_CONFIGURATION = {
    "provider_id": "anthropic",
    "model_id": "claude-sonnet-4-6",
    "prompt_version": "legacy-system-prompt-v1",
    "ruleset_version": "legacy-ruleset-v1",
    "evidence_config_id": "evidence-set-v1",
}


def _clean_configuration_identity(
    *, engine_id: str = "legacy", engine_version: str = "legacy-v1", source_fixture_sha256: str = "abc123"
) -> dict:
    """A complete configuration_identity dict (V58-QUALITY-04E-R2): this is
    what ``score_predictions`` copies unchanged from a prediction artifact's
    own identity into the scorecard, and what ``classify_scorecard`` now
    reads exclusively - never from fixture_metadata."""
    identity = dict(_CLEAN_ENGINE_CONFIGURATION)
    identity["engine_id"] = engine_id
    identity["engine_version"] = engine_version
    identity["source_fixture_sha256"] = source_fixture_sha256
    return identity


# ---------------------------------------------------------------------------
# Shared pilot-scale fixtures (real 40-case benchmark, all-approve review)
# ---------------------------------------------------------------------------


def _approve_row(row: dict) -> dict:
    filled = dict(row)
    filled["sme_decision"] = "approve"
    filled["confidence"] = "high"
    return filled


def _build_all_approve_reviewed_fixture() -> dict:
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


def _perfect_predictions_artifact(fixture: dict, *, engine_id: str = "legacy") -> dict:
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
    engine_version = "legacy-v1"
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


def _mutate_prediction(artifact: dict, case_id: str, **updates) -> None:
    entry = next(p for p in artifact["predictions"] if p["case_id"] == case_id)
    entry.update(updates)


def _mark_false_approved(artifact: dict, case_id: str) -> None:
    _mutate_prediction(artifact, case_id, finding_codes=[], raw_output={"findings": []})


def _mark_unscored(artifact: dict, case_id: str, *, reason: str = "simulated execution failure") -> None:
    _mutate_prediction(artifact, case_id, error=reason)


def _mark_false_rejected(artifact: dict, case_id: str) -> None:
    _mutate_prediction(
        artifact,
        case_id,
        finding_codes=["WRONG_ANSWER_KEY"],
        raw_output={"findings": [{"finding_code": "WRONG_ANSWER_KEY", "materiality": "blocking"}]},
    )


def _find_case(fixture: dict, *, known_good: bool | None = None, materiality: str | None = None) -> dict:
    for case in fixture["cases"]:
        if known_good is not None and bool(case.get("known_good")) != known_good:
            continue
        if materiality is not None and case.get("expected_materiality") != materiality:
            continue
        return case
    raise AssertionError(f"no matching case found (known_good={known_good}, materiality={materiality})")


def _find_cases(fixture: dict, *, known_good: bool | None = None, materiality: str | None = None) -> list:
    results = []
    for case in fixture["cases"]:
        if known_good is not None and bool(case.get("known_good")) != known_good:
            continue
        if materiality is not None and case.get("expected_materiality") != materiality:
            continue
        results.append(case)
    return results


def _fixture_metadata_from_reviewed(
    reviewed: dict,
    *,
    is_ai_drafted: bool = False,
    review_process: dict | None = None,
    overrides: dict | None = None,
) -> dict:
    summary = reviewed.get("sme_review_summary") or {}
    known_good_count = sum(1 for c in reviewed["cases"] if c.get("known_good"))
    blocking_count = sum(
        1 for c in reviewed["cases"] if not c.get("known_good") and c.get("expected_materiality") == "blocking"
    )
    warning_count = sum(
        1 for c in reviewed["cases"] if not c.get("known_good") and c.get("expected_materiality") == "warning"
    )
    category_counts: dict = {}
    for case in reviewed["cases"]:
        if not case.get("known_good"):
            category_counts[case["defect_category"]] = category_counts.get(case["defect_category"], 0) + 1

    meta = {
        "ground_truth_finalized": reviewed.get("sme_reviewed") is True,
        "is_ai_drafted": is_ai_drafted,
        "sme_review_status": reviewed.get("sme_review_status"),
        "rejected_case_ids": list(summary.get("rejected_case_ids") or []),
        "unresolved_second_review_case_ids": list(summary.get("unresolved_second_review_case_ids") or []),
        "source_fixture_sha256": reviewed.get("source_fixture_sha256"),
        "configuration_mixed": False,
        "total_case_count": len(reviewed["cases"]),
        "known_good_case_count": known_good_count,
        "defective_case_count": len(reviewed["cases"]) - known_good_count,
        "blocking_case_count": blocking_count,
        "warning_case_count": warning_count,
        "category_case_counts": category_counts,
        "review_process": review_process,
    }
    if overrides:
        meta.update(overrides)
    return meta


class PolicyPilotTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.reviewed = _build_all_approve_reviewed_fixture()

    def _score(self, artifact: dict) -> dict:
        return score_predictions(self.reviewed, artifact)

    def test_clean_pilot_is_pass(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_PASS)
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["policy_version"], POLICY_VERSION)
        self.assertEqual(result["benchmark_tier"], "pilot")

    def test_ai_drafted_fixture_is_invalid_run(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(self.reviewed, is_ai_drafted=True)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        self.assertTrue(any("AI-drafted" in reason for reason in result["reasons"]))

    def test_one_blocking_miss_is_fail(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        blocking_case = _find_case(self.reviewed, known_good=False, materiality="blocking")
        _mark_false_approved(artifact, blocking_case["case_id"])
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_FAIL)
        self.assertIn(blocking_case["case_id"], scorecard["blocking_false_approval_case_ids"])
        self.assertTrue(any("falsely approved" in reason for reason in result["reasons"]))

    def test_one_unscored_blocking_case_is_fail(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        blocking_case = _find_case(self.reviewed, known_good=False, materiality="blocking")
        _mark_unscored(artifact, blocking_case["case_id"])
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_FAIL)
        self.assertIn(blocking_case["case_id"], scorecard["unscored_blocking_case_ids"])
        self.assertTrue(any("blocking case is unscored" in reason for reason in result["reasons"]))

    def test_three_unscored_nonblocking_cases_is_fail(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        known_good_cases = _find_cases(self.reviewed, known_good=True)[:3]
        for case in known_good_cases:
            _mark_unscored(artifact, case["case_id"])
        scorecard = self._score(artifact)
        self.assertEqual(scorecard["unscored_case_count"], 3)
        self.assertEqual(scorecard["unscored_blocking_case_ids"], [])
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_FAIL)
        self.assertTrue(any("more than 2 cases are unscored" in reason for reason in result["reasons"]))

    def test_one_or_two_nonblocking_unscored_is_conditional(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        known_good_cases = _find_cases(self.reviewed, known_good=True)[:2]
        for case in known_good_cases:
            _mark_unscored(artifact, case["case_id"])
        scorecard = self._score(artifact)
        self.assertEqual(scorecard["unscored_case_count"], 2)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_CONDITIONAL_PASS)

    def test_one_false_rejection_is_conditional_pass(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        known_good_case = _find_case(self.reviewed, known_good=True)
        _mark_false_rejected(artifact, known_good_case["case_id"])
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_CONDITIONAL_PASS)
        self.assertEqual(scorecard["false_rejection_case_ids"], [known_good_case["case_id"]])
        self.assertTrue(any("exactly 1 known-good case" in reason for reason in result["reasons"]))

    def test_two_false_rejections_is_fail(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        known_good_cases = _find_cases(self.reviewed, known_good=True)[:2]
        for case in known_good_cases:
            _mark_false_rejected(artifact, case["case_id"])
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_FAIL)
        self.assertEqual(len(scorecard["false_rejection_case_ids"]), 2)
        self.assertTrue(any("known-good cases received a false rejection" in reason for reason in result["reasons"]))

    def test_warning_recall_below_50_percent_is_conditional_pass(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        warning_cases = _find_cases(self.reviewed, known_good=False, materiality="warning")
        self.assertGreaterEqual(len(warning_cases), 2, "fixture must have >=2 warning cases for this test")
        # Miss more than half of the warning cases -> recall strictly below 0.50.
        misses = warning_cases[: (len(warning_cases) // 2) + 1]
        for case in misses:
            _mark_false_approved(artifact, case["case_id"])
        scorecard = self._score(artifact)
        self.assertLess(scorecard["warning_recall"], 0.50)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_CONDITIONAL_PASS)
        self.assertTrue(any("warning-level defective-case recall" in reason for reason in result["reasons"]))

    def test_pass_language_does_not_claim_launch_readiness(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_PASS)
        language = result["classification_language"]
        self.assertIn("NOT a launch-readiness claim", language)
        self.assertIn("NOT a production-accuracy claim", language)
        self.assertNotIn("launch-ready", language.lower())
        for phrase in _PROHIBITED_LAUNCH_CLAIM_PHRASES:
            self.assertNotIn(phrase, language.lower())

    def test_pilot_required_case_count_is_exactly_40(self):
        self.assertEqual(PILOT_REQUIRED_CASE_COUNT, 40)

    def test_pilot_case_count_other_than_40_is_invalid_run(self):
        # V58-QUALITY-04E-R1 correction 2: pilot policy v1 accepts exactly
        # 40 finalized cases only - a [20, 100) band is no longer allowed.
        # Exercises 20, 39, 41, and 99 (the acceptance-criteria examples).
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        for bad_count in (20, 39, 41, 99):
            with self.subTest(total_case_count=bad_count):
                # Keep the scorecard/fixture_metadata case counts mutually
                # consistent (case_count == total_case_count) so only the
                # pilot-required-case-count gate is exercised, not the
                # separate "all expected cases represented" gate.
                tampered = copy.deepcopy(scorecard)
                tampered["case_count"] = bad_count
                tampered["scored_case_count"] = bad_count
                meta = _fixture_metadata_from_reviewed(
                    self.reviewed, overrides={"total_case_count": bad_count}
                )
                result = classify_scorecard(tampered, "pilot", meta)
                self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
                self.assertNotIn(CLASSIFICATION_PASS, result["classification_language"])
                self.assertNotIn(CLASSIFICATION_CONDITIONAL_PASS, result["classification_language"])
                self.assertTrue(
                    any("required case count" in reason for reason in result["reasons"]),
                    result["reasons"],
                )

    def test_complete_finalized_40_case_pilot_with_complete_identity_can_pass(self):
        # Acceptance criterion 8: a complete finalized 40-case SME fixture
        # with complete configuration identity can still PASS after all
        # four corrections are applied.
        self.assertEqual(len(self.reviewed["cases"]), 40)
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_PASS)

    def test_provenance_mismatch_is_invalid_run(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        tampered = copy.deepcopy(scorecard)
        tampered["prediction_source_fixture_sha256"] = "tampered-hash"
        tampered["prediction_source_fixture_matches_ground_truth_source"] = False
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(tampered, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        self.assertTrue(any("provenance does not match" in reason for reason in result["reasons"]))

    def test_rejected_cases_present_is_invalid_run(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(
            self.reviewed, overrides={"rejected_case_ids": [self.reviewed["cases"][0]["case_id"]]}
        )
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)

    def test_configuration_identity_missing_is_invalid_run(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        tampered = copy.deepcopy(scorecard)
        tampered["configuration_identity"]["engine_version"] = ""
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(tampered, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)

    def test_configuration_identity_absent_entirely_is_invalid_run(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        tampered = copy.deepcopy(scorecard)
        del tampered["configuration_identity"]
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(tampered, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)

    def test_sample_counts_and_category_diagnostics_present(self):
        artifact = _perfect_predictions_artifact(self.reviewed)
        scorecard = self._score(artifact)
        meta = _fixture_metadata_from_reviewed(self.reviewed)
        result = classify_scorecard(scorecard, "pilot", meta)
        sample_counts = result["sample_counts"]
        self.assertEqual(sample_counts["total_case_count"], 40)
        self.assertEqual(sample_counts["known_good_case_count"], 10)
        self.assertEqual(sample_counts["blocking_case_count"], 16)
        self.assertEqual(sample_counts["warning_case_count"], 14)
        for category, diag in sample_counts["category_diagnostics"].items():
            self.assertTrue(diag["diagnostic_only"], f"pilot category {category!r} must be diagnostic-only")


# ---------------------------------------------------------------------------
# V58-QUALITY-04E-R1 correction 1: the actual repository fixture must always
# be INVALID RUN. Uses the real on-disk workers/fixtures/quality_benchmark_v1
# .json (still AI-drafted / not SME-reviewed as of this task) - never a
# synthetic all-approved copy - to prove this end to end. No live engine
# predictions are generated: a temporary, hand-constructed "hypothetically
# perfect" synthetic prediction artifact is used only to reach the
# classification path, exactly as directed.
# ---------------------------------------------------------------------------


class RealFixtureContradictionTestCase(unittest.TestCase):
    """Reproduces and closes out the reported "real fixture -> clean PASS"
    contradiction.

    Root cause (see completion report): the prior smoke test classified an
    in-memory, fully-SME-approved COPY of the real fixture's content
    (``_build_all_approve_reviewed_fixture()``, used throughout this file),
    not the actual persisted ``workers/fixtures/quality_benchmark_v1.json``.
    That on-disk fixture has never been SME-reviewed and was, and remains,
    correctly rejected before a classification could ever be produced. This
    class pins that behavior down at both the CLI layer (refuses before
    classification) and the classify_scorecard layer (defense in depth for
    any caller that skips the CLI's own pre-check).
    """

    @classmethod
    def setUpClass(cls):
        cls.raw_fixture = load_benchmark_fixture(_AI_DRAFTED_FIXTURE_PATH)

    def test_actual_fixture_is_not_sme_reviewed_today(self):
        # Pins down the premise: if this ever flips to True (real SME
        # review lands), this whole test class's scenario becomes moot and
        # must be revisited - it is deliberately not silently skipped.
        self.assertIsNot(self.raw_fixture.get("sme_reviewed"), True)

    def test_cli_ground_truth_gate_refuses_the_actual_fixture(self):
        with self.assertRaises(GroundTruthNotFinalizedError) as ctx:
            load_finalized_sme_ground_truth_fixture(str(_AI_DRAFTED_FIXTURE_PATH))
        self.assertIn("sme_reviewed must be true", str(ctx.exception))

    def _honest_fixture_metadata_for_raw_fixture(self) -> dict:
        fixture = self.raw_fixture
        known_good = sum(1 for c in fixture["cases"] if c.get("known_good"))
        blocking = sum(
            1 for c in fixture["cases"] if not c.get("known_good") and c.get("expected_materiality") == "blocking"
        )
        warning = sum(
            1 for c in fixture["cases"] if not c.get("known_good") and c.get("expected_materiality") == "warning"
        )
        return {
            "ground_truth_finalized": fixture.get("sme_reviewed") is True,
            "is_ai_drafted": fixture.get("sme_reviewed") is not True,
            "sme_review_status": fixture.get("sme_review_status"),
            "rejected_case_ids": [],
            "unresolved_second_review_case_ids": [],
            "source_fixture_sha256": fixture.get("source_fixture_sha256"),
            "configuration_mixed": False,
            "total_case_count": len(fixture["cases"]),
            "known_good_case_count": known_good,
            "defective_case_count": len(fixture["cases"]) - known_good,
            "blocking_case_count": blocking,
            "warning_case_count": warning,
            "category_case_counts": {},
        }

    def _hypothetically_perfect_scorecard_for_raw_fixture(self) -> dict:
        fixture = self.raw_fixture
        known_good = sum(1 for c in fixture["cases"] if c.get("known_good"))
        warning = sum(
            1 for c in fixture["cases"] if not c.get("known_good") and c.get("expected_materiality") == "warning"
        )
        # Deliberately numerically flawless (zero errors, zero false
        # approvals/rejections, perfect recall/precision) - this proves
        # classification is INVALID RUN purely because ground truth is not
        # finalized, regardless of how clean the underlying numbers are.
        return {
            "engine_id": "legacy",
            "engine_version": "legacy-v1",
            "configuration_identity": _clean_configuration_identity(
                source_fixture_sha256=fixture.get("source_fixture_sha256") or "deadbeef"
            ),
            "case_count": len(fixture["cases"]),
            "scored_case_count": len(fixture["cases"]),
            "unscored_case_count": 0,
            "unscored_case_ids": [],
            "unscored_blocking_case_ids": [],
            "ground_truth_source_fixture_sha256": fixture.get("source_fixture_sha256"),
            "prediction_source_fixture_sha256": fixture.get("source_fixture_sha256"),
            "prediction_source_fixture_matches_ground_truth_source": True,
            "sme_reviewer_id": None,
            "blocking_false_approval_case_ids": [],
            "false_approval_case_ids": [],
            "false_rejection_case_ids": [],
            "warning_recall_detected": warning,
            "warning_recall_total": warning,
            "warning_recall": 1.0,
            "overall_precision_numerator": 30,
            "overall_precision_denominator": 30,
            "metrics": {
                "known_good_cases": known_good,
                "defective_cases": len(fixture["cases"]) - known_good,
                "false_rejections": 0,
                "overall_recall": 1.0,
                "finding_precision": 1.0,
                "recall_by_defect_category": {},
            },
        }

    def test_actual_fixture_with_honest_metadata_is_invalid_run_even_with_perfect_scorecard(self):
        meta = self._honest_fixture_metadata_for_raw_fixture()
        scorecard = self._hypothetically_perfect_scorecard_for_raw_fixture()
        result = classify_scorecard(scorecard, "pilot", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        self.assertTrue(
            any("ground truth is not finalized" in reason for reason in result["reasons"]),
            result["reasons"],
        )
        self.assertTrue(
            any("AI-drafted" in reason for reason in result["reasons"]),
            result["reasons"],
        )
        language = result["classification_language"]
        self.assertNotIn("PASS (pilot)", language)
        self.assertNotIn("CONDITIONAL PASS (pilot)", language)
        self.assertNotIn("development continuation signal", language)
        for phrase in _PROHIBITED_LAUNCH_CLAIM_PHRASES:
            self.assertNotIn(phrase, language.lower())

    def test_end_to_end_cli_score_on_actual_fixture_never_reaches_pass(self):
        # End-to-end CLI reproduction: invoke the real CLI's `score`
        # subcommand, unmodified, against the actual on-disk fixture with a
        # temporary synthetic prediction artifact. No engine is run - the
        # artifact is written directly, never generated live.
        import tempfile

        fixture = self.raw_fixture
        artifact = _perfect_predictions_artifact(fixture)
        with tempfile.TemporaryDirectory() as tmp:
            predictions_path = os.path.join(tmp, "predictions.json")
            output_path = os.path.join(tmp, "scorecard.json")
            with open(predictions_path, "w", encoding="utf-8") as handle:
                json.dump(artifact, handle)

            # subprocess.run inherits the parent environment by default,
            # which would otherwise carry pytest's PYTEST_CURRENT_TEST into
            # the child and trip the CLI's own "refuse to run under pytest"
            # guard; this is a real, separate `python -m ...` process, not
            # an in-process call, so that env var is stripped deliberately.
            child_env = dict(os.environ)
            child_env.pop("PYTEST_CURRENT_TEST", None)

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.v58_run_quality_benchmark_engines",
                    "score",
                    "--fixture",
                    str(_AI_DRAFTED_FIXTURE_PATH),
                    "--predictions",
                    predictions_path,
                    "--output",
                    output_path,
                    "--benchmark-tier",
                    "pilot",
                ],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
                env=child_env,
            )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Refusing to score", proc.stdout)
        self.assertIn("not finalized SME ground truth", proc.stdout)
        self.assertNotIn("PASS", proc.stdout)
        self.assertNotIn("CONDITIONAL PASS", proc.stdout)
        self.assertFalse(os.path.exists(output_path))


# ---------------------------------------------------------------------------
# V58-QUALITY-04E-R1 correction 3: launch safety categories are additive-only.
# ---------------------------------------------------------------------------


class SafetyCategoryEnforcementTestCase(unittest.TestCase):

    def test_baseline_is_fixed_and_matches_approved_set(self):
        self.assertEqual(REQUIRED_LAUNCH_SAFETY_CATEGORIES, frozenset({"correctness", "ambiguity", "source_support"}))

    def _run_with_override(self, override) -> dict:
        meta = _clean_launch_fixture_metadata()
        if override is not None:
            meta["additional_safety_relevant_categories"] = override
        # Force all baseline categories below the sample floor so the
        # coverage gate fires whenever - and only whenever - the baseline
        # category is actually still enforced.
        for category in REQUIRED_LAUNCH_SAFETY_CATEGORIES:
            meta["category_case_counts"][category] = 1
        return classify_scorecard(_clean_launch_scorecard(), "launch", meta)

    def test_omitting_override_still_enforces_all_baseline_categories(self):
        result = self._run_with_override(None)
        undercovered = result["gate_results"]["safety_relevant_category_coverage"]["undercovered_categories"]
        undercovered_names = {entry["category"] for entry in undercovered}
        self.assertEqual(undercovered_names, set(REQUIRED_LAUNCH_SAFETY_CATEGORIES))

    def test_empty_override_does_not_disable_baseline_categories(self):
        result = self._run_with_override([])
        undercovered = result["gate_results"]["safety_relevant_category_coverage"]["undercovered_categories"]
        undercovered_names = {entry["category"] for entry in undercovered}
        self.assertEqual(undercovered_names, set(REQUIRED_LAUNCH_SAFETY_CATEGORIES))

    def test_fewer_categories_does_not_disable_baseline_categories(self):
        # Caller supplies only one baseline category - the other two must
        # still be enforced (proves this cannot be used to shrink coverage).
        result = self._run_with_override(["correctness"])
        undercovered = result["gate_results"]["safety_relevant_category_coverage"]["undercovered_categories"]
        undercovered_names = {entry["category"] for entry in undercovered}
        self.assertEqual(undercovered_names, set(REQUIRED_LAUNCH_SAFETY_CATEGORIES))

    def test_additional_categories_are_additive(self):
        meta = _clean_launch_fixture_metadata()
        meta["additional_safety_relevant_categories"] = ["answer_quality"]
        # Push both the addition and the baseline below the sample floor so
        # both must appear as undercovered - proving the addition is layered
        # on top of (not instead of) the enforced baseline.
        meta["category_case_counts"]["answer_quality"] = 1
        for category in REQUIRED_LAUNCH_SAFETY_CATEGORIES:
            meta["category_case_counts"][category] = 1
        result = classify_scorecard(_clean_launch_scorecard(), "launch", meta)
        undercovered = result["gate_results"]["safety_relevant_category_coverage"]["undercovered_categories"]
        undercovered_names = {entry["category"] for entry in undercovered}
        self.assertIn("answer_quality", undercovered_names)
        # Baseline categories remain enforced alongside the addition.
        self.assertTrue(set(REQUIRED_LAUNCH_SAFETY_CATEGORIES).issubset(undercovered_names))

    def test_baseline_set_is_a_module_constant_not_a_parameter(self):
        # Altering the required baseline set requires editing this module
        # constant (and bumping POLICY_VERSION per the module docstring) -
        # there is no fixture_metadata key that can replace it.
        import inspect

        import workers.quality_benchmark_policy as policy_module

        source = inspect.getsource(policy_module)
        self.assertIn("REQUIRED_LAUNCH_SAFETY_CATEGORIES = frozenset(", source)


# ---------------------------------------------------------------------------
# V58-QUALITY-04E-R1 correction 4: complete configuration identity.
# ---------------------------------------------------------------------------


class ConfigurationIdentityEnforcementTestCase(unittest.TestCase):
    """V58-QUALITY-04E-R2: identity is read exclusively from
    ``scorecard["configuration_identity"]`` - fixture_metadata carries none
    at all, so every scenario here mutates the scorecard, never
    fixture_metadata."""

    def test_missing_configuration_identity_key_entirely_is_invalid_run(self):
        scorecard = _clean_launch_scorecard()
        del scorecard["configuration_identity"]
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        missing = result["gate_results"]["engine_configuration_identity"]["missing_or_blank_fields"]
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
            self.assertIn(field, missing)

    def test_each_missing_dimension_individually_is_invalid_run(self):
        for field in (
            "provider_id",
            "model_id",
            "prompt_version",
            "ruleset_version",
            "evidence_config_id",
            "source_fixture_sha256",
        ):
            with self.subTest(field=field):
                scorecard = _clean_launch_scorecard()
                del scorecard["configuration_identity"][field]
                result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
                self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
                self.assertIn(
                    field, result["gate_results"]["engine_configuration_identity"]["missing_or_blank_fields"]
                )

    def test_each_blank_dimension_individually_is_invalid_run(self):
        for field in ("provider_id", "model_id", "prompt_version", "ruleset_version", "evidence_config_id"):
            with self.subTest(field=field):
                scorecard = _clean_launch_scorecard()
                scorecard["configuration_identity"][field] = "   "
                result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
                self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
                self.assertIn(
                    field, result["gate_results"]["engine_configuration_identity"]["missing_or_blank_fields"]
                )

    def test_null_dimension_is_invalid_run_not_pass(self):
        scorecard = _clean_launch_scorecard()
        scorecard["configuration_identity"]["model_id"] = None
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)

    def test_missing_prediction_source_fixture_hash_is_invalid_run(self):
        scorecard = _clean_launch_scorecard()
        scorecard["prediction_source_fixture_sha256"] = None
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        self.assertTrue(any("provenance does not match" in reason for reason in result["reasons"]))

    def test_configuration_identity_hash_inconsistent_with_prediction_hash_is_invalid_run(self):
        # V58-QUALITY-04E-R2 tamper check: configuration_identity's own
        # source_fixture_sha256 must agree with the scorecard's top-level
        # prediction_source_fixture_sha256 - a scorecard hand-edited so the
        # two disagree must never reach PASS.
        scorecard = _clean_launch_scorecard()
        scorecard["configuration_identity"]["source_fixture_sha256"] = "tampered-hash"
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        self.assertTrue(any("provenance does not match" in reason for reason in result["reasons"]))

    def test_explicit_not_applicable_sentinel_is_accepted_as_complete(self):
        # A genuinely not-applicable dimension must be an explicit,
        # non-blank sentinel string recorded at prediction-generation time -
        # this is accepted (module never rejects a specific spelling), but
        # it is never invented automatically (see the "missing" tests
        # above), and it is read from the scorecard, never fixture_metadata.
        scorecard = _clean_launch_scorecard()
        scorecard["configuration_identity"].update(
            {
                "provider_id": "deterministic-only",
                "model_id": "not-applicable",
                "prompt_version": "not-applicable",
                "ruleset_version": "legacy-ruleset-v1",
                "evidence_config_id": "evidence-set-v1",
            }
        )
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_PASS)

    def test_complete_configuration_identity_reproduced_in_result(self):
        scorecard = _clean_launch_scorecard()
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        identity = result["configuration_identity"]
        for field, value in scorecard["configuration_identity"].items():
            self.assertEqual(identity[field], value)

    def test_fixture_metadata_engine_configuration_is_ignored_if_present(self):
        # V58-QUALITY-04E-R2 correction 3/4: even if a caller still passes a
        # legacy-shaped fixture_metadata["engine_configuration"], it must
        # have zero effect - identity comes from the scorecard only. Uses a
        # deliberately WRONG engine_configuration to prove it cannot
        # override or repair anything.
        meta = _clean_launch_fixture_metadata()
        meta["engine_configuration"] = {"provider_id": "should-be-ignored"}
        scorecard = _clean_launch_scorecard()
        result = classify_scorecard(scorecard, "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_PASS)
        self.assertEqual(result["configuration_identity"]["provider_id"], scorecard["configuration_identity"]["provider_id"])

    def test_pilot_tier_also_enforces_configuration_identity(self):
        reviewed = _build_all_approve_reviewed_fixture()
        artifact = _perfect_predictions_artifact(reviewed)
        artifact["configuration_identity"]["ruleset_version"] = ""
        artifact["provider_config"] = {}
        with self.assertRaises(PredictionArtifactError):
            # score_predictions itself refuses an incomplete identity
            # before a scorecard is ever produced (V58-QUALITY-04E-R2
            # correction 2) - this is stricter than the prior behavior
            # where an incomplete identity could reach classify_scorecard.
            score_predictions(reviewed, artifact)


# ---------------------------------------------------------------------------
# Launch-tier: synthetic (non-executed) scorecard/fixture_metadata pairs.
#
# A real 100+ case reviewed benchmark does not exist yet and generating one
# would require running real engines (forbidden by this task). Since
# classify_scorecard() only ever reads plain dict fields (never a fixture
# file or a live engine), a hand-built, internally-consistent scorecard is
# exactly the "no benchmark result generation" way to exercise every launch
# gate deterministically.
# ---------------------------------------------------------------------------


def _clean_launch_scorecard() -> dict:
    return {
        "engine_id": "legacy",
        "engine_version": "legacy-v1",
        "configuration_identity": _clean_configuration_identity(),
        "case_count": 120,
        "scored_case_count": 120,
        "unscored_case_count": 0,
        "unscored_case_ids": [],
        "unscored_blocking_case_ids": [],
        "ground_truth_source_fixture_sha256": "abc123",
        "prediction_source_fixture_sha256": "abc123",
        "prediction_source_fixture_matches_ground_truth_source": True,
        "sme_reviewer_id": "test-sme",
        "blocking_false_approval_case_ids": [],
        "false_approval_case_ids": [],
        "false_rejection_case_ids": [],
        "warning_recall_detected": 48,
        "warning_recall_total": 50,
        "warning_recall": 0.96,
        "overall_precision_numerator": 85,
        "overall_precision_denominator": 100,
        "metrics": {
            "known_good_cases": 30,
            "defective_cases": 90,
            "false_rejections": 0,
            "overall_recall": 0.95,
            "finding_precision": 0.85,
            "recall_by_defect_category": {
                "correctness": {"detected": 19, "total": 20, "recall": 0.95, "n": 20, "false_negatives": 1},
                "ambiguity": {"detected": 19, "total": 20, "recall": 0.95, "n": 20, "false_negatives": 1},
                "source_support": {"detected": 19, "total": 20, "recall": 0.95, "n": 20, "false_negatives": 1},
                "answer_quality": {"detected": 14, "total": 15, "recall": 0.933333, "n": 15, "false_negatives": 1},
                "explanation_quality": {
                    "detected": 14,
                    "total": 15,
                    "recall": 0.933333,
                    "n": 15,
                    "false_negatives": 1,
                },
            },
        },
    }


def _clean_launch_fixture_metadata() -> dict:
    return {
        "ground_truth_finalized": True,
        "is_ai_drafted": False,
        "sme_review_status": "complete",
        "rejected_case_ids": [],
        "unresolved_second_review_case_ids": [],
        "source_fixture_sha256": "abc123",
        "configuration_mixed": False,
        "total_case_count": 120,
        "known_good_case_count": 30,
        "defective_case_count": 90,
        "blocking_case_count": 40,
        "warning_case_count": 50,
        "category_case_counts": {
            "correctness": 20,
            "ambiguity": 20,
            "source_support": 20,
            "answer_quality": 15,
            "explanation_quality": 15,
        },
        "review_process": {
            "blocking_cases_double_reviewed_count": 40,
            "blocking_cases_total_count": 40,
            "non_blocking_cases_double_reviewed_count": 20,
            "non_blocking_cases_total_count": 80,
            "disagreements_adjudicated_or_excluded": True,
        },
    }


class PolicyLaunchTestCase(unittest.TestCase):

    def test_clean_launch_is_pass(self):
        result = classify_scorecard(_clean_launch_scorecard(), "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_PASS)
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["classification_language"], LAUNCH_PASS_LANGUAGE)

    def test_fewer_than_100_cases_is_invalid_run(self):
        meta = _clean_launch_fixture_metadata()
        meta["total_case_count"] = 40
        # Keep scorecard case_count consistent with the (too-small)
        # fixture_metadata total so only the launch minimum-count gate is
        # exercised, not the separate "all expected cases represented" gate.
        scorecard = _clean_launch_scorecard()
        scorecard["case_count"] = 40
        scorecard["scored_case_count"] = 40
        result = classify_scorecard(scorecard, "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        self.assertTrue(any("fewer than 100" in reason for reason in result["reasons"]))

    def test_missing_double_review_attestation_is_invalid_run(self):
        meta = _clean_launch_fixture_metadata()
        meta["review_process"] = None
        result = classify_scorecard(_clean_launch_scorecard(), "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        self.assertTrue(any("attestation is absent" in reason for reason in result["reasons"]))

    def test_incomplete_blocking_double_review_is_invalid_run(self):
        meta = _clean_launch_fixture_metadata()
        meta["review_process"]["blocking_cases_double_reviewed_count"] = 39
        result = classify_scorecard(_clean_launch_scorecard(), "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        self.assertTrue(any("100% of blocking cases" in reason for reason in result["reasons"]))

    def test_insufficient_nonblocking_double_review_is_invalid_run(self):
        meta = _clean_launch_fixture_metadata()
        meta["review_process"]["non_blocking_cases_double_reviewed_count"] = 5  # 5/80 = 6.25% < 20%
        result = classify_scorecard(_clean_launch_scorecard(), "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)

    def test_unadjudicated_disagreements_is_invalid_run(self):
        meta = _clean_launch_fixture_metadata()
        meta["review_process"]["disagreements_adjudicated_or_excluded"] = False
        result = classify_scorecard(_clean_launch_scorecard(), "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)

    def test_any_unscored_case_is_invalid_run(self):
        scorecard = _clean_launch_scorecard()
        scorecard["unscored_case_count"] = 1
        scorecard["unscored_case_ids"] = ["launch-case-1"]
        scorecard["scored_case_count"] = 119
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_INVALID_RUN)
        self.assertTrue(any("unscored/execution-errored" in reason for reason in result["reasons"]))

    def test_one_blocking_miss_is_fail(self):
        scorecard = _clean_launch_scorecard()
        scorecard["blocking_false_approval_case_ids"] = ["launch-blocking-1"]
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_FAIL)

    def test_known_good_false_rejection_rate_bands(self):
        meta = _clean_launch_fixture_metadata()

        # > 5% -> FAIL (2/30 = 6.67%)
        scorecard = _clean_launch_scorecard()
        scorecard["metrics"]["false_rejections"] = 2
        scorecard["false_rejection_case_ids"] = ["a", "b"]
        result = classify_scorecard(scorecard, "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_FAIL)

        # >2% and <=5% -> CONDITIONAL PASS (1/30 = 3.33%)
        scorecard = _clean_launch_scorecard()
        scorecard["metrics"]["false_rejections"] = 1
        scorecard["false_rejection_case_ids"] = ["a"]
        result = classify_scorecard(scorecard, "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_CONDITIONAL_PASS)

        # 0% -> PASS-eligible (baseline)
        result = classify_scorecard(_clean_launch_scorecard(), "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_PASS)

    def test_defective_case_recall_bands(self):
        meta = _clean_launch_fixture_metadata()

        scorecard = _clean_launch_scorecard()
        scorecard["metrics"]["overall_recall"] = 0.75
        result = classify_scorecard(scorecard, "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_FAIL)

        scorecard = _clean_launch_scorecard()
        scorecard["metrics"]["overall_recall"] = 0.85
        result = classify_scorecard(scorecard, "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_CONDITIONAL_PASS)

        scorecard = _clean_launch_scorecard()
        scorecard["metrics"]["overall_recall"] = 0.95
        result = classify_scorecard(scorecard, "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_PASS)

    def test_eligible_category_recall_below_85_percent_is_conditional(self):
        scorecard = _clean_launch_scorecard()
        scorecard["metrics"]["recall_by_defect_category"]["correctness"] = {
            "detected": 16,
            "total": 20,
            "recall": 0.80,
            "n": 20,
            "false_negatives": 4,
        }
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_CONDITIONAL_PASS)
        self.assertTrue(any("eligible categor" in reason for reason in result["reasons"]))

    def test_missing_safety_category_coverage_is_conditional(self):
        meta = _clean_launch_fixture_metadata()
        meta["category_case_counts"]["source_support"] = 10  # below the 15-case floor
        result = classify_scorecard(_clean_launch_scorecard(), "launch", meta)
        self.assertEqual(result["classification"], CLASSIFICATION_CONDITIONAL_PASS)
        self.assertTrue(any("safety-relevant categor" in reason for reason in result["reasons"]))

    def test_precision_below_70_percent_is_conditional_never_fail(self):
        scorecard = _clean_launch_scorecard()
        scorecard["metrics"]["finding_precision"] = 0.65
        result = classify_scorecard(scorecard, "launch", _clean_launch_fixture_metadata())
        self.assertEqual(result["classification"], CLASSIFICATION_CONDITIONAL_PASS)
        self.assertTrue(any("finding precision" in reason for reason in result["reasons"]))

    def test_exact_configuration_identity_preserved(self):
        result = classify_scorecard(_clean_launch_scorecard(), "launch", _clean_launch_fixture_metadata())
        identity = result["configuration_identity"]
        self.assertEqual(identity["engine_id"], "legacy")
        self.assertEqual(identity["engine_version"], "legacy-v1")
        self.assertEqual(identity["ground_truth_source_fixture_sha256"], "abc123")
        self.assertEqual(identity["prediction_source_fixture_sha256"], "abc123")

    def test_launch_pass_language_is_exact_and_scoped(self):
        result = classify_scorecard(_clean_launch_scorecard(), "launch", _clean_launch_fixture_metadata())
        self.assertEqual(
            result["classification_language"],
            "Passed CertBound's launch benchmark for the exact engine, model, prompt, "
            "ruleset, evidence configuration, and version tested.",
        )

    def test_prohibited_accuracy_language_never_appears(self):
        scenarios = [
            (_clean_launch_scorecard(), _clean_launch_fixture_metadata()),  # PASS
        ]
        fail_scorecard = _clean_launch_scorecard()
        fail_scorecard["blocking_false_approval_case_ids"] = ["x"]
        scenarios.append((fail_scorecard, _clean_launch_fixture_metadata()))

        conditional_scorecard = _clean_launch_scorecard()
        conditional_scorecard["metrics"]["finding_precision"] = 0.5
        scenarios.append((conditional_scorecard, _clean_launch_fixture_metadata()))

        invalid_meta = _clean_launch_fixture_metadata()
        invalid_meta["review_process"] = None
        scenarios.append((_clean_launch_scorecard(), invalid_meta))

        for scorecard, meta in scenarios:
            result = classify_scorecard(scorecard, "launch", meta)
            haystack = " ".join(
                [result["classification_language"], " ".join(result["reasons"]), " ".join(result["limitations"])]
            ).lower()
            for phrase in _PROHIBITED_LAUNCH_CLAIM_PHRASES:
                self.assertNotIn(phrase, haystack, f"classification={result['classification']!r}")


class PolicyInputValidationTestCase(unittest.TestCase):

    def test_unsupported_tier_raises(self):
        with self.assertRaises(PolicyInputError):
            classify_scorecard(_clean_launch_scorecard(), "beta", _clean_launch_fixture_metadata())

    def test_non_mapping_scorecard_raises(self):
        with self.assertRaises(PolicyInputError):
            classify_scorecard(["not", "a", "mapping"], "pilot", _clean_launch_fixture_metadata())  # type: ignore[arg-type]

    def test_malformed_fixture_metadata_type_raises(self):
        meta = _clean_launch_fixture_metadata()
        meta["total_case_count"] = "one-hundred-twenty"  # wrong type
        with self.assertRaises(PolicyInputError):
            classify_scorecard(_clean_launch_scorecard(), "launch", meta)


class RegressionTestCase(unittest.TestCase):
    """Confirms this task did not change pre-existing behavior it wasn't
    asked to change."""

    def test_false_rejection_definition_unchanged_for_defective_case(self):
        # A defective case that gets an extra/wrong blocking finding must
        # NOT be scored as a "false rejection" — that label is reserved for
        # known-good cases only (V58-QUALITY-04E explicitly preserves this).
        case = {
            "case_id": "regression-1",
            "known_good": False,
            "expected_finding_codes": ["UNSUPPORTED_ANSWER"],
            "expected_materiality": "blocking",
            "benchmark_version": "v1",
            "certification": "cert",
            "domain": "domain",
            "defect_category": "correctness",
        }
        findings = [
            {"finding_code": "UNSUPPORTED_ANSWER", "materiality": "blocking"},
            {"finding_code": "WRONG_ANSWER_KEY", "materiality": "blocking"},  # extra/wrong finding
        ]
        result = _build_case_result(case, engine="legacy", findings=findings)
        self.assertFalse(result.false_rejection)
        self.assertTrue(result.detection_success)

    def test_score_predictions_retains_original_scorecard_fields(self):
        reviewed = _build_all_approve_reviewed_fixture()
        artifact = _perfect_predictions_artifact(reviewed)
        scorecard = score_predictions(reviewed, artifact)
        for key in (
            "schema_version",
            "case_count",
            "scored_case_count",
            "unscored_case_count",
            "unscored_case_ids",
            "metrics",
            "finding_category_metrics",
            "known_good_approval_rate",
            "defective_case_rejection_rate",
        ):
            self.assertIn(key, scorecard)

    def test_policy_module_has_no_forbidden_dependencies(self):
        import workers.quality_benchmark_policy as policy_module

        source = Path(policy_module.__file__).read_text(encoding="utf-8")
        forbidden_tokens = (
            "psycopg2",
            "supabase",
            "boto3",
            "requests",
            "httpx",
            "import workers.ai_quality_audit_worker",
            "import workers.job_handlers",
        )
        for token in forbidden_tokens:
            self.assertNotIn(token, source, f"quality_benchmark_policy.py must not depend on {token!r}")


if __name__ == "__main__":
    unittest.main()
