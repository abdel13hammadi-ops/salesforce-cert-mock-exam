"""
Tests for V48 AI quality audit prompt builders.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_prompts import (
    build_pass_a_prompt,
    build_pass_b_prompt,
    build_pass_c_prompt,
)

_CHUNK_1 = "11111111-1111-1111-1111-111111111111"
_CHUNK_2 = "22222222-2222-2222-2222-222222222222"
_QVID = "cccccccc-0000-0000-0000-000000000001"
_RUN_ID = "aaaaaaaa-0000-0000-0000-000000000001"


def _blind_context(**overrides) -> dict:
    base = {
        "question_version_id": _QVID,
        "certification_exam_name": "ADM-201",
        "domain_name": "Configuration",
        "question_text": "Which feature enables this?",
        "question_type": "single",
        "required_selection_count": 1,
        "options": [
            {"option_label": "A", "option_text": "Profiles", "display_order": 1},
            {"option_label": "B", "option_text": "Roles", "display_order": 2},
        ],
    }
    base.update(overrides)
    return base


def _comparison_context(**overrides) -> dict:
    base = {
        "question_version_id": _QVID,
        "audit_run_id": _RUN_ID,
        "certification_exam_name": "ADM-201",
        "domain_name": "Configuration",
        "question_text": "Which feature enables this?",
        "question_type": "single",
        "required_selection_count": 1,
        "options": [
            {
                "option_label": "A",
                "option_text": "Profiles",
                "display_order": 1,
                "is_correct": True,
            },
            {
                "option_label": "B",
                "option_text": "Roles",
                "display_order": 2,
                "is_correct": False,
            },
        ],
        "stored_correct_option_labels": ["A"],
        "pass_a_selected_option_labels": ["A"],
        "explanation": "Profiles control object permissions.",
        "frozen_evidence": [
            {
                "rank": 2,
                "chunk_id": _CHUNK_2,
                "chunk_text": "Permission sets extend access.",
                "authoritative_hash": "hash-2",
            },
            {
                "rank": 1,
                "chunk_id": _CHUNK_1,
                "chunk_text": "Profiles define default settings.",
                "authoritative_hash": "hash-1",
            },
        ],
    }
    base.update(overrides)
    return base


def _dispute_context(**overrides) -> dict:
    base = {
        "reason_code": "BLOCKING_DEFECT_PROPOSED",
        "finding_refs": ["F1"],
        "trigger_reason": "Pass B proposed one or more blocking findings",
        "resolution_hints": {
            "expected_resolution_type": "NORMAL_DISPUTE",
            "expected_substituted_for_passes": [],
            "allowed_confirmed_finding_refs": ["F1"],
            "trigger_reason": "Pass B proposed one or more blocking findings",
        },
    }
    base.update(overrides)
    return base


class TestPassANoLeakage(unittest.TestCase):

    def test_blind_prompt_excludes_answer_key_explanation_and_evidence(self):
        system_prompt, user_prompt = build_pass_a_prompt(_blind_context())
        combined = f"{system_prompt}\n{user_prompt}"

        self.assertIn("blind answer selection", user_prompt)
        self.assertNotIn("stored_correct", combined)
        self.assertNotIn("Profiles control object permissions.", combined)
        self.assertNotIn("Frozen evidence", combined)
        self.assertNotIn(_CHUNK_1, combined)
        self.assertNotIn("is_correct", combined)
        self.assertNotIn("Pass A selected labels", combined)
        self.assertNotIn("(stored_correct=", combined)


class TestPassBEvidenceRankOrder(unittest.TestCase):

    def test_frozen_evidence_emitted_in_rank_order(self):
        _, user_prompt = build_pass_b_prompt(_comparison_context())

        rank1_pos = user_prompt.index(f"rank=1 chunk_id={_CHUNK_1}")
        rank2_pos = user_prompt.index(f"rank=2 chunk_id={_CHUNK_2}")
        self.assertLess(rank1_pos, rank2_pos)
        self.assertIn("Profiles define default settings.", user_prompt)
        self.assertIn("Permission sets extend access.", user_prompt)


class TestPassCDiscriminator(unittest.TestCase):

    def test_normal_dispute_includes_resolution_discriminator(self):
        pass_b_findings = [
            {
                "finding_ref": "F1",
                "finding_code": "WRONG_ANSWER_KEY",
                "finding_type": "correctness",
                "severity": "high",
                "materiality": "blocking",
                "title": "Wrong answer key",
                "description": "Marked correct option is wrong.",
                "evidence_chunk_ids": [_CHUNK_1],
                "metadata": {},
            }
        ]
        _, user_prompt = build_pass_c_prompt(
            _comparison_context(),
            pass_b_findings,
            _dispute_context(),
        )

        self.assertIn("Resolution discriminator (required):", user_prompt)
        self.assertIn("- resolution_type must be 'NORMAL_DISPUTE'", user_prompt)
        self.assertIn('- substituted_for_passes must be exactly []', user_prompt)
        self.assertIn('"F1"', user_prompt)
        self.assertIn("Pass B proposed_findings:", user_prompt)

    def test_substitution_discriminator(self):
        _, user_prompt = build_pass_c_prompt(
            _comparison_context(),
            [],
            _dispute_context(
                reason_code="PASS_A_SCHEMA_INVALID",
                finding_refs=[],
                trigger_reason="Pass A response failed schema validation after two attempts",
                resolution_hints={
                    "expected_resolution_type": "PASS_A_SUBSTITUTION",
                    "expected_substituted_for_passes": ["A", "B"],
                    "allowed_confirmed_finding_refs": [],
                    "trigger_reason": (
                        "Pass A response failed schema validation after two attempts"
                    ),
                },
            ),
        )

        self.assertIn("- resolution_type must be 'PASS_A_SUBSTITUTION'", user_prompt)
        self.assertIn('- substituted_for_passes must be exactly ["A","B"]', user_prompt)
        self.assertIn("Comparison context for your substituted review:", user_prompt)


    def test_pass_b_prompt_contains_zero_evidence_source_support_contract(self):
        _, user_prompt = build_pass_b_prompt(_comparison_context())

        self.assertIn("metadata.source_support_context", user_prompt)
        self.assertIn('"attempted_retrieval"', user_prompt)
        self.assertIn('"evidence_limitation"', user_prompt)
        self.assertIn('"proposed_technical_claim"', user_prompt)
        self.assertIn('"insufficiency_reason"', user_prompt)
        self.assertIn("metadata: {} is invalid", user_prompt)
        self.assertIn("SOURCE_SUPPORT_WEAK cannot use materiality=blocking", user_prompt)
        self.assertIn('"finding_code": "SOURCE_SUPPORT_WEAK"', user_prompt)
        self.assertIn('"evidence_chunk_ids": []', user_prompt)

    def test_pass_b_retry_prompt_includes_prior_validation_error(self):
        _, user_prompt = build_pass_b_prompt(
            _comparison_context(),
            retry_schema_errors=[
                "proposed_findings[0].metadata.source_support_context must be a JSON object"
            ],
        )

        self.assertIn("Prior Pass B response failed deterministic schema validation:", user_prompt)
        self.assertIn("source_support_context must be a JSON object", user_prompt)
        self.assertIn("Correct only the invalid JSON shape", user_prompt)


class TestPromptStability(unittest.TestCase):

    def test_identical_inputs_produce_identical_prompts(self):
        blind = _blind_context()
        comparison = _comparison_context()
        dispute = _dispute_context()
        pass_b_findings = [{"finding_ref": "F1", "title": "x"}]

        first_a = build_pass_a_prompt(blind)
        second_a = build_pass_a_prompt(blind)
        self.assertEqual(first_a, second_a)

        first_b = build_pass_b_prompt(comparison)
        second_b = build_pass_b_prompt(comparison)
        self.assertEqual(first_b, second_b)

        first_c = build_pass_c_prompt(comparison, pass_b_findings, dispute)
        second_c = build_pass_c_prompt(comparison, pass_b_findings, dispute)
        self.assertEqual(first_c, second_c)


if __name__ == "__main__":
    unittest.main()
