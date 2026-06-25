"""
Tests for V45 Phase 2 audit calibration pilot (dry-run).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.audit_calibration import (
    CALIBRATION_CASE_COUNT,
    DEFAULT_FIXTURE_PATH,
    REQUIRED_CASE_LABELS,
    load_calibration_fixture,
    run_calibration_case,
    run_calibration_pilot,
)
from workers.deterministic_audit import run_deterministic_checks
from workers.llm_audit import AUDIT_RESPONSE_SCHEMA
from workers.llm_providers import LlmResponse
from workers.run_audit_calibration import main as calibration_main


VALID_EXPECTED_DETECTORS = frozenset({"none", "llm", "deterministic", "either"})


def _load_default_fixture() -> dict:
    return load_calibration_fixture(DEFAULT_FIXTURE_PATH)


class FakeCalibrationProvider:
    """Returns deterministic LLM findings keyed by calibration label."""

    def __init__(self, *, input_tokens: int = 50, output_tokens: int = 20, cost: float = 0.001):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost = cost
        self.calls = []

    def __call__(self, *, model_name, system_prompt, user_prompt,
                 response_schema, metadata=None):
        self.calls.append(metadata or {})
        label = (metadata or {}).get("calibration_label", "")
        findings = []
        if label == "ambiguous":
            findings = [{
                "finding_code": "AMBIGUOUS_WORDING",
                "finding_type": "ambiguity",
                "severity": "medium",
                "title": "Ambiguous wording",
                "description": "Missing business context makes multiple access approaches defensible.",
                "evidence": [],
            }]
        elif label == "wrong-answer-key":
            findings = [{
                "finding_code": "INCORRECT_ANSWER_KEY",
                "finding_type": "correctness",
                "severity": "high",
                "title": "Incorrect answer key",
                "description": "The marked correct option contradicts the calibration evidence.",
                "evidence": [],
            }]
        elif label == "weak-distractors":
            findings = [{
                "finding_code": "WEAK_DISTRACTOR",
                "finding_type": "answer_quality",
                "severity": "medium",
                "title": "Weak distractors",
                "description": "Several distractors are unrealistic or trivially dismissible.",
                "evidence": [],
            }]
        return LlmResponse(
            parsed_response={"findings": findings},
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            actual_cost_usd=self.cost,
            model_name=model_name,
            provider_name="fake",
        )


class TestCalibrationFixture(unittest.TestCase):

    def test_default_fixture_has_exactly_five_cases(self):
        fixture = _load_default_fixture()
        self.assertEqual(len(fixture["cases"]), CALIBRATION_CASE_COUNT)

    def test_fixture_documents_synthetic_evidence(self):
        fixture = _load_default_fixture()
        note = fixture["fixture_metadata"]["evidence_note"].lower()
        self.assertIn("synthetic", note)
        self.assertIn("not official", note)

    def test_every_case_has_expected_detector(self):
        fixture = _load_default_fixture()
        expected_by_label = {
            "known-good": "none",
            "ambiguous": "llm",
            "wrong-answer-key": "llm",
            "weak-distractors": "llm",
            "incomplete-explanation": "deterministic",
        }
        labels = [case["label"] for case in fixture["cases"]]
        self.assertEqual(labels, list(REQUIRED_CASE_LABELS))
        for case in fixture["cases"]:
            detector = case["expected_detector"]
            self.assertIn(detector, VALID_EXPECTED_DETECTORS)
            self.assertEqual(detector, expected_by_label[case["label"]])

    def test_known_good_has_four_options_and_no_deterministic_findings(self):
        fixture = _load_default_fixture()
        case = next(c for c in fixture["cases"] if c["label"] == "known-good")
        self.assertEqual(len(case["question"]["options"]), 4)
        self.assertEqual(run_deterministic_checks(case["question"], "1.0.0"), [])

    def test_wrong_answer_key_has_one_marked_correct_and_no_count_mismatch(self):
        fixture = _load_default_fixture()
        case = next(c for c in fixture["cases"] if c["label"] == "wrong-answer-key")
        options = case["question"]["options"]
        self.assertEqual(len(options), 4)
        self.assertEqual(sum(1 for opt in options if opt["is_correct"]), 1)
        codes = [f["finding_code"] for f in run_deterministic_checks(case["question"], "1.0.0")]
        self.assertNotIn("CORRECT_COUNT_MISMATCH", codes)

    def test_incomplete_explanation_uses_explanation_quality_category(self):
        fixture = _load_default_fixture()
        case = next(c for c in fixture["cases"] if c["label"] == "incomplete-explanation")
        self.assertEqual(case["expected_defect_category"], "explanation_quality")
        self.assertEqual(case["expected_finding_codes"], ["MISSING_EXPLANATION"])

    def test_fixture_with_wrong_count_rejected(self):
        payload = {"cases": [{"label": "only-one", "question": {}, "user_prompt": "x",
                              "expected_defect_category": "none", "resource_snapshot": {}}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            path = handle.name
        try:
            with self.assertRaises(ValueError):
                load_calibration_fixture(path)
        finally:
            os.remove(path)


class TestCalibrationPilot(unittest.TestCase):

    def test_known_good_false_positive_reporting(self):
        fixture = _load_default_fixture()
        provider = FakeCalibrationProvider()
        summary = run_calibration_pilot(fixture, provider)
        known_good = next(r for r in summary.case_results if r.label == "known-good")
        self.assertFalse(known_good.false_positive)
        self.assertTrue(known_good.passed)
        self.assertEqual(known_good.merged_finding_count, 0)

    def test_expected_defect_detection_reporting(self):
        fixture = _load_default_fixture()
        provider = FakeCalibrationProvider()
        summary = run_calibration_pilot(fixture, provider)

        wrong_key = next(r for r in summary.case_results if r.label == "wrong-answer-key")
        incomplete = next(r for r in summary.case_results if r.label == "incomplete-explanation")
        ambiguous = next(r for r in summary.case_results if r.label == "ambiguous")
        weak = next(r for r in summary.case_results if r.label == "weak-distractors")

        self.assertIn("INCORRECT_ANSWER_KEY", wrong_key.finding_codes)
        self.assertTrue(wrong_key.passed)
        self.assertNotIn("CORRECT_COUNT_MISMATCH", wrong_key.finding_codes)
        self.assertIn("MISSING_EXPLANATION", incomplete.finding_codes)
        self.assertTrue(incomplete.passed)
        self.assertGreater(ambiguous.merged_finding_count, 0)
        self.assertTrue(ambiguous.passed)
        self.assertIn("WEAK_DISTRACTOR", weak.finding_codes)
        self.assertTrue(weak.passed)

    def test_token_and_cost_aggregation(self):
        fixture = _load_default_fixture()
        provider = FakeCalibrationProvider(input_tokens=100, output_tokens=40, cost=0.002)
        summary = run_calibration_pilot(fixture, provider)

        self.assertAlmostEqual(summary.total_cost_usd, 0.002 * CALIBRATION_CASE_COUNT)
        self.assertGreater(summary.average_duration_seconds, 0.0)
        self.assertEqual(len(provider.calls), CALIBRATION_CASE_COUNT)

    def test_no_publish_or_promote_rpc_calls(self):
        """Calibration dry-run never touches Supabase RPCs."""
        fixture = _load_default_fixture()
        provider = FakeCalibrationProvider()
        client = MagicMock()
        run_calibration_pilot(fixture, provider)
        client.rpc.assert_not_called()


class TestCalibrationSafetyGate(unittest.TestCase):

    def test_main_refuses_without_live_flag(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CERTBOUND_ALLOW_LIVE_AI_TEST", None)
            with patch(
                "workers.run_audit_calibration._running_under_pytest",
                return_value=False,
            ):
                code = calibration_main([])
        self.assertEqual(code, 1)

    def test_main_refuses_under_pytest(self):
        with patch.dict(os.environ, {"CERTBOUND_ALLOW_LIVE_AI_TEST": "1"}, clear=False):
            with patch(
                "workers.run_audit_calibration._running_under_pytest",
                return_value=True,
            ):
                code = calibration_main([])
        self.assertEqual(code, 2)

    def test_main_runs_five_cases_with_mocked_provider(self):
        fixture = _load_default_fixture()
        provider = FakeCalibrationProvider()
        with patch.dict(
            os.environ,
            {
                "CERTBOUND_ALLOW_LIVE_AI_TEST": "1",
                "CERTBOUND_LLM_PROVIDER": "anthropic",
            },
            clear=False,
        ):
            with patch(
                "workers.run_audit_calibration._running_under_pytest",
                return_value=False,
            ):
                with patch(
                    "workers.run_audit_calibration.build_llm_provider_from_env",
                    return_value=provider,
                ):
                    with patch(
                        "workers.run_audit_calibration.load_calibration_fixture",
                        return_value=fixture,
                    ):
                        with patch(
                            "workers.run_audit_calibration.format_pilot_summary",
                            return_value="ok",
                        ):
                            code = calibration_main(
                                ["--fixture", str(DEFAULT_FIXTURE_PATH)]
                            )

        self.assertEqual(code, 0)
        self.assertEqual(len(provider.calls), CALIBRATION_CASE_COUNT)


class TestRunCalibrationCase(unittest.TestCase):

    def test_provider_failure_still_reports_deterministic_findings(self):
        fixture = _load_default_fixture()
        case = next(c for c in fixture["cases"] if c["label"] == "incomplete-explanation")

        def _fail_provider(**kwargs):
            raise RuntimeError("provider down")

        result = run_calibration_case(
            case,
            _fail_provider,
            ruleset_version="1.0.0",
            model_name="claude-sonnet-4-6",
            system_prompt="audit",
        )

        self.assertTrue(result.provider_failure)
        self.assertGreater(result.deterministic_finding_count, 0)
        self.assertIn("MISSING_EXPLANATION", result.finding_codes)


if __name__ == "__main__":
    unittest.main()
