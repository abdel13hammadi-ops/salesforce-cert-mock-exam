"""
Tests for V45 Phase 3 finding materiality and canonical-code policy.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.audit_calibration import run_calibration_case
from workers.finding_merge import merge_findings
from workers.finding_policy import (
    CANONICAL_FINDING_CODES,
    canonicalize_llm_finding_code,
    normalize_deterministic_finding,
    normalize_llm_finding,
    original_llm_codes,
)
from workers.llm_providers import LlmResponse


def _llm_finding(**overrides) -> dict:
    base = {
        "finding_code": "AMB-001",
        "finding_type": "ambiguity",
        "severity": "medium",
        "title": "Ambiguous wording",
        "description": "Missing business context makes multiple approaches defensible.",
        "evidence": [],
    }
    base.update(overrides)
    return base


class TestMaterialityMapping(unittest.TestCase):

    def test_blocking_correctness(self):
        finding = normalize_llm_finding(_llm_finding(
            finding_code="INCORRECT_KEY",
            finding_type="correctness",
            title="Wrong answer key",
            description="The marked correct option is wrong.",
        ))
        self.assertEqual(finding["materiality"], "blocking")
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")

    def test_blocking_missing_explanation(self):
        finding = normalize_deterministic_finding({
            "finding_code": "MISSING_EXPLANATION",
            "finding_type": "explanation_quality",
            "severity": "medium",
            "title": "Missing explanation",
            "description": "Explanation is empty.",
            "evidence": [],
        })
        self.assertEqual(finding["finding_code"], "EXPLANATION_MISSING")
        self.assertEqual(finding["materiality"], "blocking")

    def test_warning_weak_distractors(self):
        finding = normalize_llm_finding(_llm_finding(
            finding_code="WEAK_DISTRACTORS_001",
            finding_type="answer_quality",
            title="Weak distractors",
            description="Several distractors are unrealistic or trivially dismissible.",
        ))
        self.assertEqual(finding["finding_code"], "WEAK_DISTRACTORS")
        self.assertEqual(finding["materiality"], "warning")

    def test_informational_stylistic(self):
        finding = normalize_llm_finding(_llm_finding(
            finding_code="STYLE-001",
            finding_type="other",
            title="Stylistic improvement",
            description="Consider making the question more scenario-based.",
        ))
        self.assertEqual(finding["materiality"], "informational")

    def test_unknown_defaults_to_warning(self):
        finding = normalize_llm_finding(_llm_finding(
            finding_code="MYSTERY-999",
            finding_type="policy",
            title="Needs review",
            description="Something unclear that does not match a known pattern.",
        ))
        self.assertEqual(finding["materiality"], "warning")


class TestCanonicalCodeNormalization(unittest.TestCase):

    def test_ambiguous_code_maps_to_canonical(self):
        code = canonicalize_llm_finding_code(_llm_finding(finding_code="AMB-001"))
        self.assertIn(code, {"AMBIGUOUS_QUESTION", "MULTIPLE_DEFENSIBLE_ANSWERS"})

    def test_wrong_answer_key_maps_to_canonical(self):
        finding = normalize_llm_finding(_llm_finding(
            finding_code="EXP_001",
            finding_type="correctness",
            title="Incorrect answer key",
            description="The marked correct option contradicts the evidence.",
        ))
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")
        self.assertIn(finding["finding_code"], CANONICAL_FINDING_CODES)

    def test_preserves_original_llm_code_in_metadata(self):
        finding = normalize_llm_finding(_llm_finding(finding_code="AMB-001"))
        self.assertEqual(finding["metadata"]["original_finding_code"], "AMB-001")
        self.assertNotEqual(finding["finding_code"], "AMB-001")

    def test_no_arbitrary_code_survives_as_canonical(self):
        finding = normalize_llm_finding(_llm_finding(
            finding_code="WEAK_DISTRACTORS_001",
            finding_type="answer_quality",
            title="Weak distractors",
            description="Several distractors are unrealistic or trivially dismissible.",
        ))
        self.assertEqual(finding["finding_code"], "WEAK_DISTRACTORS")
        self.assertIn(finding["finding_code"], CANONICAL_FINDING_CODES)


class TestOriginalLlmCodes(unittest.TestCase):

    def test_flattens_mixed_string_and_list_metadata_with_dedup(self):
        findings = [
            {"metadata": {"original_finding_code": "EXP_001"}},
            {"metadata": {"original_finding_code": [
                "EXP_MISSING",
                "  EXP_MISSING  ",
                "",
                None,
                "EXPL_NO_DISTRACTOR_RATIONALE",
            ]}},
            {"metadata": {"original_finding_code": "MISSING_EXPLANATION"}},
            {"metadata": {"original_finding_code": "  AMB-001  "}},
            {"metadata": {"original_finding_code": "EXP_001"}},
            {"metadata": {}},
            {"metadata": {"original_finding_code": None}},
        ]
        self.assertEqual(
            original_llm_codes(findings),
            [
                "EXP_001",
                "EXP_MISSING",
                "EXPL_NO_DISTRACTOR_RATIONALE",
                "MISSING_EXPLANATION",
                "AMB-001",
            ],
        )


class TestMergeMaterialityEscalation(unittest.TestCase):

    def test_merge_picks_highest_materiality(self):
        det = {
            "finding_code": "EXPLANATION_MISSING",
            "finding_type": "explanation_quality",
            "severity": "medium",
            "materiality": "warning",
            "title": "Thin explanation",
            "description": "Explanation is incomplete.",
            "field_path": "explanation",
            "evidence": [],
            "metadata": {},
            "detector_name": "certbound-det",
            "detector_version": "1.0.0",
        }
        llm = {
            "finding_code": "EXPLANATION_MISSING",
            "finding_type": "explanation_quality",
            "severity": "high",
            "materiality": "blocking",
            "title": "Missing explanation",
            "description": "Explanation is incomplete.",
            "field_path": "explanation",
            "evidence": [],
            "metadata": {},
            "detector_name": "gpt-auditor",
            "detector_version": "v1",
        }
        merged = merge_findings([det], [llm])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["materiality"], "blocking")


class TestCalibrationMateriality(unittest.TestCase):

    def test_known_good_passes_with_warnings_only(self):
        class WarningOnlyProvider:
            def __call__(self, **kwargs):
                return LlmResponse(
                    parsed_response={"findings": [{
                        "finding_code": "STYLE-001",
                        "finding_type": "other",
                        "severity": "low",
                        "title": "Stylistic suggestion",
                        "description": "Consider a scenario-based rewrite.",
                        "evidence": [],
                    }]},
                    input_tokens=10,
                    output_tokens=5,
                )

        fixture = {
            "ruleset_version": "1.0.0",
            "model_name": "fake",
            "system_prompt": "audit",
            "cases": [{
                "label": "known-good",
                "expected_defect_category": "none",
                "expect_detection": False,
                "user_prompt": "audit",
                "question": {
                    "question_text": "What is Salesforce?",
                    "explanation": "Salesforce is a CRM platform.",
                    "question_type": "single",
                    "select_count": 1,
                    "options": [
                        {"option_label": "A", "option_text": "CRM", "is_correct": True, "display_order": 1},
                        {"option_label": "B", "option_text": "ERP", "is_correct": False, "display_order": 2},
                        {"option_label": "C", "option_text": "CMS", "is_correct": False, "display_order": 3},
                        {"option_label": "D", "option_text": "BI", "is_correct": False, "display_order": 4},
                    ],
                },
                "resource_snapshot": {"chunks": []},
            }],
        }
        # Pilot requires 5 cases; test single case via run_calibration_case instead.
        result = run_calibration_case(
            fixture["cases"][0],
            WarningOnlyProvider(),
            ruleset_version="1.0.0",
            model_name="fake",
            system_prompt="audit",
        )
        self.assertTrue(result.passed)
        self.assertFalse(result.false_positive)
        self.assertEqual(result.blocking_count, 0)
        self.assertGreater(result.warning_count + result.informational_count, 0)

    def test_known_good_fails_with_blocking_finding(self):
        class BlockingProvider:
            def __call__(self, **kwargs):
                return LlmResponse(
                    parsed_response={"findings": [{
                        "finding_code": "WRONG-001",
                        "finding_type": "correctness",
                        "severity": "high",
                        "title": "Wrong answer key",
                        "description": "The marked correct option is incorrect.",
                        "evidence": [],
                    }]},
                    input_tokens=10,
                    output_tokens=5,
                )

        case = {
            "label": "known-good",
            "expected_defect_category": "none",
            "expect_detection": False,
            "user_prompt": "audit",
            "question": {
                "question_text": "What is Salesforce?",
                "explanation": "Salesforce is a CRM platform.",
                "question_type": "single",
                "select_count": 1,
                "options": [
                    {"option_label": "A", "option_text": "CRM", "is_correct": True, "display_order": 1},
                    {"option_label": "B", "option_text": "ERP", "is_correct": False, "display_order": 2},
                    {"option_label": "C", "option_text": "CMS", "is_correct": False, "display_order": 3},
                    {"option_label": "D", "option_text": "BI", "is_correct": False, "display_order": 4},
                ],
            },
            "resource_snapshot": {"chunks": []},
        }
        result = run_calibration_case(
            case,
            BlockingProvider(),
            ruleset_version="1.0.0",
            model_name="fake",
            system_prompt="audit",
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.false_positive)
        self.assertEqual(result.blocking_count, 1)
        self.assertEqual(result.finding_codes, ["WRONG_ANSWER_KEY"])


if __name__ == "__main__":
    unittest.main()
