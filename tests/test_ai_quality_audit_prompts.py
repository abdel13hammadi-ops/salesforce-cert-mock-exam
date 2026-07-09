"""
Tests for V48 AI quality audit prompt builders.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_prompts import (
    _FINDING_CODE_DEFINITIONS,
    _option_judgment_schema,
    build_pass_a_prompt,
    build_pass_b_correctness_prompt,
    build_pass_b_prompt,
    build_pass_c_prompt,
    PASS_B_CORRECTNESS_RESPONSE_SCHEMA,
)
from workers.ai_quality_audit_schemas import (
    ANSWER_COMPLETENESS_VALUES,
    ANSWER_CORRECTNESS_CODES,
    SUPPORTED_FINDING_CODES,
)
from workers.openai_provider import normalize_schema_for_openai

_HELD_OUT_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "pass_b_taxonomy_held_out_scenarios.json"
)
_BENCHMARK_CASE_ID_PATTERN = re.compile(r"qbv1-\d{3}")

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


class TestPassBTaxonomyGuidance(unittest.TestCase):

    def test_every_supported_code_has_non_empty_definition(self):
        for code in sorted(SUPPORTED_FINDING_CODES):
            with self.subTest(code=code):
                self.assertIn(code, _FINDING_CODE_DEFINITIONS)
                definition = _FINDING_CODE_DEFINITIONS[code].strip()
                self.assertTrue(definition, f"{code} definition must not be empty")

    def test_pass_b_prompt_contains_all_supported_codes_with_definitions(self):
        _, user_prompt = build_pass_b_prompt(_comparison_context())

        for code in sorted(SUPPORTED_FINDING_CODES):
            with self.subTest(code=code):
                self.assertIn(code, user_prompt)
                self.assertIn(_FINDING_CODE_DEFINITIONS[code], user_prompt)

    def test_pass_b_prompt_contains_decision_procedure(self):
        _, user_prompt = build_pass_b_prompt(_comparison_context())

        self.assertIn("Finding code selection procedure:", user_prompt)
        self.assertIn("Solve the question independently", user_prompt)
        self.assertIn("Compare your independent answer with the stored answer key", user_prompt)
        self.assertIn("most specific finding code", user_prompt)
        self.assertIn(
            "Do not emit WRONG_ANSWER_KEY merely because evidence is weak",
            user_prompt,
        )
        self.assertIn("UNSUPPORTED_ANSWER when the stored answer is plausible", user_prompt)
        self.assertIn(
            "Do not emit MULTIPLE_DEFENSIBLE_ANSWERS unless multiple options",
            user_prompt,
        )
        self.assertIn("Do not add multiple codes when one code fully explains", user_prompt)

    def test_pass_b_prompt_contains_materiality_guidance(self):
        _, user_prompt = build_pass_b_prompt(_comparison_context())

        self.assertIn("Materiality assignment:", user_prompt)
        self.assertIn("materiality=blocking only when the defect can invalidate correctness", user_prompt)
        self.assertIn("materiality=warning for quality defects that do not invalidate", user_prompt)
        self.assertIn("SOURCE_SUPPORT_WEAK and DOMAIN_MISALIGNMENT are warning-only", user_prompt)

    def test_pass_b_prompt_contains_no_benchmark_case_ids(self):
        _, user_prompt = build_pass_b_prompt(_comparison_context())

        self.assertIsNone(_BENCHMARK_CASE_ID_PATTERN.search(user_prompt))

    def test_held_out_scenarios_have_distinguishing_prompt_guidance(self):
        fixture = json.loads(_HELD_OUT_FIXTURE.read_text(encoding="utf-8"))
        _, user_prompt = build_pass_b_prompt(_comparison_context())

        for scenario in fixture["scenarios"]:
            with self.subTest(scenario_id=scenario["id"]):
                for code in scenario["codes"]:
                    self.assertIn(code, user_prompt)
                for phrase in scenario["required_prompt_phrases"]:
                    self.assertIn(phrase, user_prompt)


class TestPassBCorrectnessPrompt(unittest.TestCase):
    """V60-IMPL-01: specialized answer-correctness detector prompt."""

    def test_excludes_pass_a_selected_labels(self):
        context = _comparison_context(pass_a_selected_option_labels=["B"])
        system_prompt, user_prompt = build_pass_b_correctness_prompt(context)
        combined = f"{system_prompt}\n{user_prompt}"

        self.assertNotIn("Pass A selected labels", combined)
        self.assertNotIn("Pass A", combined)

    def test_includes_stored_labels_and_frozen_evidence_but_not_pass_a(self):
        _, user_prompt = build_pass_b_correctness_prompt(_comparison_context())

        self.assertIn("Stored correct labels", user_prompt)
        self.assertIn(_CHUNK_1, user_prompt)
        self.assertIn(_CHUNK_2, user_prompt)
        self.assertIn("Profiles define default settings.", user_prompt)

    def test_forbids_choosing_finding_code_or_materiality(self):
        _, user_prompt = build_pass_b_correctness_prompt(_comparison_context())

        self.assertIn("not selecting a finding code, materiality, severity", user_prompt)
        # "materiality" appears exactly once, in the explicit prohibition
        # sentence above -- the specialist schema itself never asks for it.
        self.assertEqual(user_prompt.count("materiality"), 1)
        self.assertNotIn("finding_code", user_prompt)

    def test_requires_three_state_verdict_and_citations(self):
        _, user_prompt = build_pass_b_correctness_prompt(_comparison_context())

        self.assertIn("SUPPORTED_AS_CORRECT", user_prompt)
        self.assertIn("NOT_SUPPORTED_AS_CORRECT", user_prompt)
        self.assertIn("INSUFFICIENT_EVIDENCE", user_prompt)
        self.assertIn("must cite at least one frozen", user_prompt)
        self.assertIn("evidence_sufficient_for_decision", user_prompt)
        self.assertIn("abstention_reason", user_prompt)

    def test_forbids_general_knowledge_guessing(self):
        _, user_prompt = build_pass_b_correctness_prompt(_comparison_context())

        self.assertIn("Do not use general Salesforce knowledge", user_prompt)
        self.assertIn("do not guess", user_prompt)

    def test_zero_frozen_evidence_forces_insufficient_evidence_guidance(self):
        context = _comparison_context(frozen_evidence=[])
        _, user_prompt = build_pass_b_correctness_prompt(context)

        self.assertIn("zero frozen evidence chunks", user_prompt)
        self.assertIn("every option must be judged INSUFFICIENT_EVIDENCE", user_prompt)

    def test_retry_prompt_includes_prior_validation_error(self):
        _, user_prompt = build_pass_b_correctness_prompt(
            _comparison_context(),
            retry_schema_errors=["option_judgments is missing judgments for options: ['B']"],
        )

        self.assertIn(
            "Prior answer-correctness response failed deterministic schema validation:",
            user_prompt,
        )
        self.assertIn("option_judgments is missing judgments", user_prompt)

    def test_identical_inputs_produce_identical_prompts(self):
        context = _comparison_context()
        first = build_pass_b_correctness_prompt(context)
        second = build_pass_b_correctness_prompt(context)
        self.assertEqual(first, second)

    def test_includes_answer_completeness_instruction(self):
        _, user_prompt = build_pass_b_correctness_prompt(_comparison_context())

        self.assertIn("FULLY_RESPONSIVE", user_prompt)
        self.assertIn("PARTIAL_COMPONENT", user_prompt)
        self.assertIn("NOT_APPLICABLE", user_prompt)
        self.assertIn("Do not infer completeness merely from words", user_prompt)
        self.assertIn("both, either, all, or neither", user_prompt)

    def test_option_judgment_schema_includes_answer_completeness(self):
        schema = _option_judgment_schema()
        self.assertIn("answer_completeness", schema["required"])
        self.assertEqual(
            set(schema["properties"]["answer_completeness"]["enum"]),
            set(ANSWER_COMPLETENESS_VALUES),
        )

    def test_openai_normalization_requires_answer_completeness_field(self):
        normalized = normalize_schema_for_openai(PASS_B_CORRECTNESS_RESPONSE_SCHEMA)
        judgment_schema = normalized["properties"]["option_judgments"]["items"]
        self.assertIn("answer_completeness", judgment_schema["required"])
        self.assertEqual(
            set(judgment_schema["properties"]["answer_completeness"]["enum"]),
            set(ANSWER_COMPLETENESS_VALUES),
        )


class TestPassBPromptExcludeFindingCodes(unittest.TestCase):
    """V60-IMPL-01: general Pass B judge narrowed to exclude correctness codes."""

    def test_excluded_codes_are_not_offered_in_codes_block(self):
        _, user_prompt = build_pass_b_prompt(
            _comparison_context(),
            exclude_finding_codes=ANSWER_CORRECTNESS_CODES,
        )
        for code in ANSWER_CORRECTNESS_CODES:
            self.assertNotIn(f"- {code}", user_prompt)

    def test_scope_restriction_notice_present_when_excluding(self):
        _, user_prompt = build_pass_b_prompt(
            _comparison_context(),
            exclude_finding_codes=ANSWER_CORRECTNESS_CODES,
        )
        self.assertIn("Scope restriction for this run:", user_prompt)
        self.assertIn("Do NOT propose these finding codes:", user_prompt)
        for code in ANSWER_CORRECTNESS_CODES:
            self.assertIn(code, user_prompt)

    def test_default_call_without_exclusion_still_offers_all_codes(self):
        _, user_prompt = build_pass_b_prompt(_comparison_context())
        for code in ANSWER_CORRECTNESS_CODES:
            self.assertIn(f"- {code}", user_prompt)
        self.assertNotIn("Scope restriction for this run:", user_prompt)

    def test_non_correctness_codes_remain_available_when_excluding(self):
        _, user_prompt = build_pass_b_prompt(
            _comparison_context(),
            exclude_finding_codes=ANSWER_CORRECTNESS_CODES,
        )
        self.assertIn("WEAK_DISTRACTORS", user_prompt)
        self.assertIn("EXPLANATION_MISSING", user_prompt)


if __name__ == "__main__":
    unittest.main()
