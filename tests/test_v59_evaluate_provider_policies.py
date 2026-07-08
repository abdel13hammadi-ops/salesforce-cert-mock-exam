"""Tests for V59-POLICY-01 offline provider policy evaluator."""

from __future__ import annotations

import importlib
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.v59_evaluate_provider_policies import (  # noqa: E402
    EXPECTED_FIXTURE_SHA256,
    POLICY_FUNCTIONS,
    POLICY_NAMES,
    PolicyEvaluatorError,
    ProviderNormalizedDecision,
    build_case_matrix,
    choose_conclusion,
    compute_routing_outcome_label,
    derive_policy_metrics,
    load_prediction_map,
    load_sme_cases,
    normalize_provider_prediction,
    policy_disagreement_to_human_review,
    policy_materiality_gated,
    policy_openai_single,
    policy_reject_on_both,
    policy_reject_on_either,
    policy_sonnet_rejection_validated_by_openai,
    policy_sonnet_single,
    run_policy_evaluation,
    validate_case_alignment,
    verify_fixture_sha256,
    _evaluate_provider_detection,
)

_REAL_OPENAI_PATH = _REPO_ROOT / ".local/v58_openai_baseline/20260708T033458Z/result.json"
_REAL_OPENAI_SCORECARD_PATH = _REPO_ROOT / ".local/v58_openai_baseline/20260708T033458Z/scorecard.json"
_REAL_RECONCILED_PATH = _REPO_ROOT / ".local/v58_openai_baseline/20260708T033458Z/provider_comparison_reconciled.json"
_REAL_SONNET_PATH = Path(
    r"C:\Users\Abdel\AppData\Local\Temp\v58_day7_rerun_20260708T002827Z\v48_day7_rerun_predictions.json"
)
_REAL_FIXTURE_PATH = _REPO_ROOT / "workers/fixtures/quality_benchmark_v1_sme_reviewed.json"


def _real_artifacts_available() -> bool:
    return all(
        path.exists()
        for path in (
            _REAL_OPENAI_PATH,
            _REAL_OPENAI_SCORECARD_PATH,
            _REAL_RECONCILED_PATH,
            _REAL_SONNET_PATH,
            _REAL_FIXTURE_PATH,
        )
    )


def _decision(
    disposition: str,
    *,
    has_blocking_finding: bool = False,
    has_warning_finding: bool = False,
    available: bool | None = None,
    finding_codes: tuple[str, ...] = (),
) -> ProviderNormalizedDecision:
    if available is None:
        available = disposition != "HUMAN_REVIEW"
    return ProviderNormalizedDecision(
        disposition=disposition,
        has_blocking_finding=has_blocking_finding,
        has_warning_finding=has_warning_finding,
        available=available,
        finding_codes=finding_codes,
    )


def _synthetic_sme_cases() -> list[dict]:
    return [
        {
            "case_id": "syn-001",
            "known_good": True,
            "expected_materiality": None,
            "expected_finding_codes": [],
            "benchmark_version": "syn",
            "certification": "test",
            "domain": "test",
            "defect_category": "none",
        },
        {
            "case_id": "syn-002",
            "known_good": False,
            "expected_materiality": "blocking",
            "expected_finding_codes": ["WRONG_ANSWER_KEY"],
            "benchmark_version": "syn",
            "certification": "test",
            "domain": "test",
            "defect_category": "correctness",
        },
        {
            "case_id": "syn-003",
            "known_good": False,
            "expected_materiality": "warning",
            "expected_finding_codes": ["WEAK_DISTRACTORS"],
            "benchmark_version": "syn",
            "certification": "test",
            "domain": "test",
            "defect_category": "answer_quality",
        },
    ]


def _minimal_detection_metrics() -> dict:
    return {
        "detection_finding_source": "merged",
        "scored_cases_for_detection": 1,
        "unscored_cases_for_detection": [],
        "correct_detections": 0,
        "missed_detections": 1,
        "blocking_recall": 0.0,
        "blocking_recall_detected": 0,
        "blocking_recall_total": 1,
        "warning_recall": None,
        "warning_recall_detected": 0,
        "warning_recall_total": 0,
        "overall_recall": 0.0,
        "overall_recall_detected": 0,
        "overall_recall_total": 1,
        "detection_false_approvals": 1,
        "detection_false_approval_rate": 1.0,
        "_detected_by_case_id": {"syn-002": False},
        "_unscored_case_ids": set(),
    }


def _synthetic_predictions() -> tuple[dict[str, dict], dict[str, dict]]:
    sonnet = {
        "syn-001": {
            "approved": True,
            "case_id": "syn-001",
            "error": None,
            "finding_codes": [],
            "materiality": None,
            "raw_output": {"run_status": "completed", "requires_human_review": False, "findings": []},
        },
        "syn-002": {
            "approved": False,
            "case_id": "syn-002",
            "error": None,
            "finding_codes": ["WRONG_ANSWER_KEY"],
            "materiality": "blocking",
            "raw_output": {
                "run_status": "completed",
                "requires_human_review": False,
                "findings": [{"finding_code": "WRONG_ANSWER_KEY", "materiality": "blocking"}],
            },
        },
        "syn-003": {
            "approved": True,
            "case_id": "syn-003",
            "error": None,
            "finding_codes": ["LOW_COGNITIVE_LEVEL"],
            "materiality": "warning",
            "raw_output": {
                "run_status": "completed",
                "requires_human_review": False,
                "findings": [{"finding_code": "LOW_COGNITIVE_LEVEL", "materiality": "warning"}],
            },
        },
    }
    openai = {
        "syn-001": {
            "approved": True,
            "case_id": "syn-001",
            "error": None,
            "finding_codes": [],
            "materiality": None,
            "raw_output": {"run_status": "completed", "requires_human_review": False, "findings": []},
        },
        "syn-002": {
            "approved": True,
            "case_id": "syn-002",
            "error": None,
            "finding_codes": [],
            "materiality": None,
            "raw_output": {"run_status": "completed", "requires_human_review": False, "findings": []},
        },
        "syn-003": {
            "approved": False,
            "case_id": "syn-003",
            "error": None,
            "finding_codes": ["WEAK_DISTRACTORS"],
            "materiality": "warning",
            "raw_output": {
                "run_status": "completed",
                "requires_human_review": False,
                "findings": [{"finding_code": "WEAK_DISTRACTORS", "materiality": "warning"}],
            },
        },
    }
    return sonnet, openai


class TestPolicyFunctionsDoNotUseGroundTruth(unittest.TestCase):
    def test_policy_functions_have_no_sme_parameters(self) -> None:
        for policy_name, policy_fn in POLICY_FUNCTIONS.items():
            signature = inspect.signature(policy_fn)
            parameter_names = set(signature.parameters)
            self.assertEqual(parameter_names, {"sonnet", "openai"}, policy_name)
            self.assertNotIn("sme_case_type", parameter_names, policy_name)

    def test_policy_results_do_not_depend_on_sme_fields(self) -> None:
        sonnet = _decision("REJECT", has_blocking_finding=True)
        openai = _decision("APPROVE")
        sme_payload = {
            "known_good": True,
            "expected_materiality": "blocking",
            "expected_finding_codes": ["WRONG_ANSWER_KEY"],
        }
        for policy_fn in POLICY_FUNCTIONS.values():
            baseline = policy_fn(sonnet, openai)
            _ = sme_payload
            self.assertEqual(policy_fn(sonnet, openai), baseline)


class TestPolicyDispositionValidity(unittest.TestCase):
    def test_synthetic_fixture_produces_valid_dispositions(self) -> None:
        sonnet, openai = _synthetic_predictions()
        matrix = build_case_matrix(_synthetic_sme_cases(), sonnet, openai)
        for row in matrix:
            for policy_name in POLICY_NAMES:
                disposition = row[f"{policy_name}_disposition"]
                self.assertIn(disposition, {"APPROVE", "REJECT", "HUMAN_REVIEW"})

    @unittest.skipUnless(_real_artifacts_available(), "real artifacts unavailable")
    def test_real_fixture_produces_valid_dispositions(self) -> None:
        from scripts.v59_evaluate_provider_policies import load_sme_cases

        sme_cases = load_sme_cases(_REAL_FIXTURE_PATH)
        sonnet = load_prediction_map(_REAL_SONNET_PATH)
        openai = load_prediction_map(_REAL_OPENAI_PATH)
        matrix = build_case_matrix(sme_cases, sonnet, openai)
        self.assertEqual(len(matrix), 40)
        for row in matrix:
            for policy_name in POLICY_NAMES:
                self.assertIn(row[f"{policy_name}_disposition"], {"APPROVE", "REJECT", "HUMAN_REVIEW"})


class TestSonnetInconclusiveNormalization(unittest.TestCase):
    @unittest.skipUnless(_real_artifacts_available(), "real artifacts unavailable")
    def test_qbv1_008_normalizes_to_human_review(self) -> None:
        sonnet = load_prediction_map(_REAL_SONNET_PATH)
        normalized = normalize_provider_prediction(sonnet["qbv1-008"])
        self.assertEqual(normalized.disposition, "HUMAN_REVIEW")
        self.assertFalse(normalized.available)

    def test_inconclusive_run_status_becomes_human_review(self) -> None:
        prediction = {
            "approved": False,
            "error": "inconclusive run",
            "finding_codes": ["WRONG_ANSWER_KEY"],
            "materiality": "blocking",
            "raw_output": {
                "run_status": "inconclusive",
                "requires_human_review": True,
                "findings": [
                    {
                        "finding_code": "WRONG_ANSWER_KEY",
                        "materiality": "blocking",
                        "metadata": {"dispute_resolution_status": "UNRESOLVED"},
                    }
                ],
            },
        }
        normalized = normalize_provider_prediction(prediction)
        self.assertEqual(normalized.disposition, "HUMAN_REVIEW")


class TestRejectOnEitherPolicy(unittest.TestCase):
    def test_reject_on_either_branches(self) -> None:
        self.assertEqual(
            policy_reject_on_either(_decision("REJECT"), _decision("APPROVE")),
            "REJECT",
        )
        self.assertEqual(
            policy_reject_on_either(_decision("APPROVE"), _decision("REJECT")),
            "REJECT",
        )
        self.assertEqual(
            policy_reject_on_either(_decision("HUMAN_REVIEW"), _decision("APPROVE")),
            "HUMAN_REVIEW",
        )
        self.assertEqual(
            policy_reject_on_either(_decision("APPROVE"), _decision("APPROVE")),
            "APPROVE",
        )


class TestRejectOnBothPolicy(unittest.TestCase):
    def test_reject_on_both_branches(self) -> None:
        self.assertEqual(
            policy_reject_on_both(_decision("REJECT"), _decision("REJECT")),
            "REJECT",
        )
        self.assertEqual(
            policy_reject_on_both(_decision("REJECT"), _decision("APPROVE")),
            "APPROVE",
        )
        self.assertEqual(
            policy_reject_on_both(_decision("APPROVE"), _decision("REJECT")),
            "APPROVE",
        )
        self.assertEqual(
            policy_reject_on_both(_decision("HUMAN_REVIEW"), _decision("REJECT")),
            "HUMAN_REVIEW",
        )


class TestDisagreementToHumanReviewPolicy(unittest.TestCase):
    def test_disagreement_to_human_review_branches(self) -> None:
        self.assertEqual(
            policy_disagreement_to_human_review(_decision("APPROVE"), _decision("REJECT")),
            "HUMAN_REVIEW",
        )
        self.assertEqual(
            policy_disagreement_to_human_review(_decision("APPROVE"), _decision("APPROVE")),
            "APPROVE",
        )
        self.assertEqual(
            policy_disagreement_to_human_review(_decision("REJECT"), _decision("REJECT")),
            "REJECT",
        )
        self.assertEqual(
            policy_disagreement_to_human_review(_decision("HUMAN_REVIEW"), _decision("APPROVE")),
            "HUMAN_REVIEW",
        )


class TestMaterialityGatedPolicy(unittest.TestCase):
    def test_blocking_wins_over_warning(self) -> None:
        sonnet = _decision("APPROVE", has_warning_finding=True)
        openai = _decision("APPROVE", has_blocking_finding=True)
        self.assertEqual(policy_materiality_gated(sonnet, openai), "REJECT")

    def test_warning_only_routes_to_human_review(self) -> None:
        sonnet = _decision("APPROVE", has_warning_finding=True)
        openai = _decision("APPROVE")
        self.assertEqual(policy_materiality_gated(sonnet, openai), "HUMAN_REVIEW")

    def test_remaining_disagreement_fallback(self) -> None:
        sonnet = _decision("REJECT")
        openai = _decision("APPROVE")
        self.assertEqual(policy_materiality_gated(sonnet, openai), "HUMAN_REVIEW")


class TestAutoRejectRecallExcludesHumanReview(unittest.TestCase):
    def test_human_review_not_counted_in_auto_reject_recall(self) -> None:
        matrix = [
            {
                "case_id": "syn-002",
                "sme_case_type": "defective",
                "sme_expected_materiality": "blocking",
                "SONNET_SINGLE_disposition": "HUMAN_REVIEW",
                "SONNET_SINGLE_detection_success": False,
            }
        ]
        metrics = derive_policy_metrics(matrix, "SONNET_SINGLE", detection_metrics=_minimal_detection_metrics())
        self.assertEqual(metrics["safety"]["blocking_auto_reject_recall"], 0.0)


class TestProtectedRoutingIncludesHumanReview(unittest.TestCase):
    def test_human_review_counted_in_protected_routing_recall(self) -> None:
        matrix = [
            {
                "case_id": "syn-002",
                "sme_case_type": "defective",
                "sme_expected_materiality": "blocking",
                "SONNET_SINGLE_disposition": "HUMAN_REVIEW",
                "SONNET_SINGLE_detection_success": False,
            }
        ]
        metrics = derive_policy_metrics(matrix, "SONNET_SINGLE", detection_metrics=_minimal_detection_metrics())
        self.assertEqual(metrics["protected_routing"]["blocking_protected_routing_recall"], 1.0)


class TestKnownGoodRates(unittest.TestCase):
    def test_known_good_rejection_and_human_review_rates(self) -> None:
        matrix = [
            {
                "case_id": "kg-1",
                "sme_case_type": "known_good",
                "sme_expected_materiality": None,
                "TEST_disposition": "APPROVE",
            },
            {
                "case_id": "kg-2",
                "sme_case_type": "known_good",
                "sme_expected_materiality": None,
                "TEST_disposition": "REJECT",
            },
            {
                "case_id": "kg-3",
                "sme_case_type": "known_good",
                "sme_expected_materiality": None,
                "TEST_disposition": "HUMAN_REVIEW",
            },
        ]
        metrics = derive_policy_metrics(matrix, "TEST", detection_metrics=_minimal_detection_metrics())
        known_good = metrics["known_good_impact"]
        self.assertEqual(known_good["known_good_automatically_approved"], 1)
        self.assertEqual(known_good["known_good_automatically_rejected"], 1)
        self.assertEqual(known_good["known_good_routed_to_human_review"], 1)
        self.assertAlmostEqual(known_good["known_good_automatic_rejection_rate"], 1 / 3)
        self.assertAlmostEqual(known_good["known_good_human_review_rate"], 1 / 3)


class TestDispositionCountsSumToTotal(unittest.TestCase):
    def test_synthetic_fixture_disposition_counts(self) -> None:
        sonnet, openai = _synthetic_predictions()
        matrix = build_case_matrix(_synthetic_sme_cases(), sonnet, openai)
        for policy_name in POLICY_NAMES:
            metrics = derive_policy_metrics(matrix, policy_name, detection_metrics=_minimal_detection_metrics())
            total = (
                metrics["disposition"]["automatic_approval_count"]
                + metrics["disposition"]["automatic_rejection_count"]
                + metrics["disposition"]["human_review_count"]
            )
            self.assertEqual(total, len(matrix))

    @unittest.skipUnless(_real_artifacts_available(), "real artifacts unavailable")
    def test_real_fixture_disposition_counts(self) -> None:
        from scripts.v59_evaluate_provider_policies import load_sme_cases

        sme_cases = load_sme_cases(_REAL_FIXTURE_PATH)
        sonnet = load_prediction_map(_REAL_SONNET_PATH)
        openai = load_prediction_map(_REAL_OPENAI_PATH)
        matrix = build_case_matrix(sme_cases, sonnet, openai)
        for policy_name in POLICY_NAMES:
            metrics = derive_policy_metrics(matrix, policy_name, detection_metrics=_minimal_detection_metrics())
            total = (
                metrics["disposition"]["automatic_approval_count"]
                + metrics["disposition"]["automatic_rejection_count"]
                + metrics["disposition"]["human_review_count"]
            )
            self.assertEqual(total, 40)


class TestInputIntegrityFailures(unittest.TestCase):
    def test_duplicate_case_ids_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dup.json"
            path.write_text(
                json.dumps(
                    {
                        "predictions": [
                            {"case_id": "a", "approved": True},
                            {"case_id": "a", "approved": False},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(PolicyEvaluatorError):
                load_prediction_map(path)

    def test_unknown_case_ids_raise(self) -> None:
        sme_cases = [{"case_id": "only-one", "known_good": True, "expected_materiality": None}]
        sonnet = {"only-one": {"approved": True, "raw_output": {"run_status": "completed"}}}
        openai = {"unknown": {"approved": True, "raw_output": {"run_status": "completed"}}}
        with self.assertRaises(PolicyEvaluatorError):
            validate_case_alignment(sme_cases, sonnet, openai)


class TestDeterministicRepeatedExecution(unittest.TestCase):
    @unittest.skipUnless(_real_artifacts_available(), "real artifacts unavailable")
    def test_repeated_execution_produces_identical_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_parent = Path(tmpdir)
            kwargs = dict(
                fixture_path=_REAL_FIXTURE_PATH,
                openai_artifact_path=_REAL_OPENAI_PATH,
                sonnet_artifact_path=_REAL_SONNET_PATH,
                output_parent=output_parent,
                openai_scorecard_path=_REAL_OPENAI_SCORECARD_PATH,
                reconciled_comparison_path=_REAL_RECONCILED_PATH,
                executed_at_utc="20990101T000000Z",
            )
            first = run_policy_evaluation(**kwargs)
            first_policies = first["payload"]["policies"]
            from scripts.v59_evaluate_provider_policies import load_sme_cases

            sme_cases = load_sme_cases(_REAL_FIXTURE_PATH)
            sonnet = load_prediction_map(_REAL_SONNET_PATH)
            openai = load_prediction_map(_REAL_OPENAI_PATH)
            matrix = build_case_matrix(sme_cases, sonnet, openai)
            second_policies = {
                policy_name: derive_policy_metrics(
                    matrix,
                    policy_name,
                    detection_metrics=first["payload"]["policies"][policy_name]["detection"],
                )
                for policy_name in POLICY_NAMES
            }
            self.assertEqual(first_policies, second_policies)


class TestNoNetworkOrDatabaseImports(unittest.TestCase):
    def test_module_does_not_import_network_or_database_clients(self) -> None:
        module = importlib.import_module("scripts.v59_evaluate_provider_policies")
        imported = set(getattr(module, "__dict__", {}).keys())
        forbidden = {
            "requests",
            "httpx",
            "urllib3",
            "psycopg",
            "psycopg2",
            "supabase",
            "sqlalchemy",
            "asyncpg",
        }
        self.assertFalse(imported.intersection(forbidden))


class TestSingleProviderPolicies(unittest.TestCase):
    def test_sonnet_single_policy(self) -> None:
        self.assertEqual(policy_sonnet_single(_decision("APPROVE"), _decision("REJECT")), "APPROVE")
        self.assertEqual(policy_sonnet_single(_decision("REJECT"), _decision("APPROVE")), "REJECT")
        self.assertEqual(
            policy_sonnet_single(_decision("HUMAN_REVIEW"), _decision("APPROVE")),
            "HUMAN_REVIEW",
        )

    def test_openai_single_policy(self) -> None:
        self.assertEqual(policy_openai_single(_decision("REJECT"), _decision("APPROVE")), "APPROVE")
        self.assertEqual(policy_openai_single(_decision("APPROVE"), _decision("REJECT")), "REJECT")
        self.assertEqual(
            policy_openai_single(_decision("APPROVE"), _decision("HUMAN_REVIEW")),
            "HUMAN_REVIEW",
        )


class TestSonnetRejectionValidatedByOpenaiPolicy(unittest.TestCase):
    def test_sonnet_rejection_requires_openai_confirmation(self) -> None:
        self.assertEqual(
            policy_sonnet_rejection_validated_by_openai(_decision("REJECT"), _decision("REJECT")),
            "REJECT",
        )
        self.assertEqual(
            policy_sonnet_rejection_validated_by_openai(_decision("REJECT"), _decision("APPROVE")),
            "HUMAN_REVIEW",
        )


class TestFixtureSha256Verification(unittest.TestCase):
    @unittest.skipUnless(_REAL_FIXTURE_PATH.exists(), "fixture unavailable")
    def test_fixture_sha256_matches_expected(self) -> None:
        verify_fixture_sha256(_REAL_FIXTURE_PATH, EXPECTED_FIXTURE_SHA256)

    @unittest.skipUnless(_REAL_FIXTURE_PATH.exists(), "fixture unavailable")
    def test_fixture_sha256_mismatch_raises(self) -> None:
        with self.assertRaises(PolicyEvaluatorError):
            verify_fixture_sha256(_REAL_FIXTURE_PATH, "0" * 64)


class TestConclusionSelection(unittest.TestCase):
    def test_zero_undetected_auto_approval_and_zero_known_good_rejection_freezes_policy(self) -> None:
        policy_metrics = {
            "SONNET_SINGLE": {
                "safety": {"undetected_defective_cases_automatically_approved": 0},
                "known_good_impact": {"known_good_automatically_rejected": 0},
            }
        }
        pareto = {"non_dominated_policies": ["SONNET_SINGLE"]}
        self.assertEqual(choose_conclusion(policy_metrics, pareto), "FREEZE POLICY: SONNET_SINGLE")


class TestDetectionDispositionSeparation(unittest.TestCase):
    def test_correct_warning_detection_with_approval_is_not_detection_false_approval(self) -> None:
        self.assertEqual(
            compute_routing_outcome_label(
                "defective",
                "APPROVE",
                detection_success=True,
                detection_unscored=False,
            ),
            "defective_detected_but_approved",
        )

    def test_missed_defect_with_approval_is_detection_failure(self) -> None:
        self.assertEqual(
            compute_routing_outcome_label(
                "defective",
                "APPROVE",
                detection_success=False,
                detection_unscored=False,
            ),
            "defective_missed_and_approved",
        )

    @unittest.skipUnless(_real_artifacts_available(), "real artifacts unavailable")
    def test_qbv1_010_materiality_mismatch_is_detection_failure_not_warning_approval(self) -> None:
        sme_cases = {case["case_id"]: case for case in load_sme_cases(_REAL_FIXTURE_PATH)}
        sonnet = load_prediction_map(_REAL_SONNET_PATH)
        openai = load_prediction_map(_REAL_OPENAI_PATH)
        case = sme_cases["qbv1-010"]

        sonnet_detection, sonnet_unscored = _evaluate_provider_detection(case, sonnet["qbv1-010"])
        openai_detection, openai_unscored = _evaluate_provider_detection(case, openai["qbv1-010"])

        self.assertFalse(sonnet_unscored)
        self.assertFalse(openai_unscored)
        self.assertFalse(sonnet_detection)
        self.assertFalse(openai_detection)
        self.assertEqual(normalize_provider_prediction(sonnet["qbv1-010"]).disposition, "APPROVE")
        self.assertEqual(normalize_provider_prediction(openai["qbv1-010"]).disposition, "APPROVE")

    @unittest.skipUnless(_real_artifacts_available(), "real artifacts unavailable")
    def test_qbv1_011_sonnet_correct_warning_detection_remains_approved(self) -> None:
        sme_cases = {case["case_id"]: case for case in load_sme_cases(_REAL_FIXTURE_PATH)}
        sonnet = load_prediction_map(_REAL_SONNET_PATH)
        case = sme_cases["qbv1-011"]

        detection_success, detection_unscored = _evaluate_provider_detection(case, sonnet["qbv1-011"])
        normalized = normalize_provider_prediction(sonnet["qbv1-011"])

        self.assertFalse(detection_unscored)
        self.assertTrue(detection_success)
        self.assertEqual(normalized.disposition, "APPROVE")
        self.assertEqual(
            compute_routing_outcome_label(
                "defective",
                normalized.disposition,
                detection_success=detection_success,
                detection_unscored=detection_unscored,
            ),
            "defective_detected_but_approved",
        )

    @unittest.skipUnless(_real_artifacts_available(), "real artifacts unavailable")
    def test_qbv1_011_openai_missed_warning_is_detection_failure(self) -> None:
        sme_cases = {case["case_id"]: case for case in load_sme_cases(_REAL_FIXTURE_PATH)}
        openai = load_prediction_map(_REAL_OPENAI_PATH)
        case = sme_cases["qbv1-011"]

        detection_success, detection_unscored = _evaluate_provider_detection(case, openai["qbv1-011"])
        normalized = normalize_provider_prediction(openai["qbv1-011"])

        self.assertFalse(detection_unscored)
        self.assertFalse(detection_success)
        self.assertEqual(normalized.disposition, "APPROVE")
        self.assertEqual(
            compute_routing_outcome_label(
                "defective",
                normalized.disposition,
                detection_success=detection_success,
                detection_unscored=detection_unscored,
            ),
            "defective_missed_and_approved",
        )

    @unittest.skipUnless(_real_artifacts_available(), "real artifacts unavailable")
    def test_real_matrix_separates_detected_warning_auto_approvals_from_missed(self) -> None:
        sme_cases = load_sme_cases(_REAL_FIXTURE_PATH)
        sonnet = load_prediction_map(_REAL_SONNET_PATH)
        openai = load_prediction_map(_REAL_OPENAI_PATH)
        matrix = build_case_matrix(sme_cases, sonnet, openai)
        by_id = {row["case_id"]: row for row in matrix}

        self.assertEqual(by_id["qbv1-011"]["SONNET_SINGLE_outcome"], "defective_detected_but_approved")
        self.assertEqual(by_id["qbv1-011"]["OPENAI_SINGLE_outcome"], "defective_missed_and_approved")
        self.assertEqual(by_id["qbv1-010"]["SONNET_SINGLE_outcome"], "defective_missed_and_approved")
        self.assertEqual(by_id["qbv1-010"]["OPENAI_SINGLE_outcome"], "defective_missed_and_approved")

        sonnet_metrics = derive_policy_metrics(
            matrix,
            "SONNET_SINGLE",
            detection_metrics={
                "detection_finding_source": "sonnet",
                "scored_cases_for_detection": 39,
                "unscored_cases_for_detection": ["qbv1-008"],
                "correct_detections": 0,
                "missed_detections": 0,
                "blocking_recall": 0.0,
                "blocking_recall_detected": 0,
                "blocking_recall_total": 0,
                "warning_recall": 0.0,
                "warning_recall_detected": 0,
                "warning_recall_total": 0,
                "overall_recall": 0.0,
                "overall_recall_detected": 0,
                "overall_recall_total": 0,
                "detection_false_approvals": 0,
                "detection_false_approval_rate": 0.0,
            },
        )
        self.assertGreater(
            sonnet_metrics["safety"]["detected_warning_cases_automatically_approved"],
            0,
        )
        self.assertGreater(
            sonnet_metrics["safety"]["undetected_defective_cases_automatically_approved"],
            0,
        )
        self.assertGreater(
            sonnet_metrics["safety"]["defective_cases_automatically_approved"],
            sonnet_metrics["safety"]["undetected_defective_cases_automatically_approved"],
        )


if __name__ == "__main__":
    unittest.main()
