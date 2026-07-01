"""
Tests for V48 AI quality audit pass-result schema validation.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_schemas import (
    AiQualityAuditValidationError,
    validate_pass_a_result,
    validate_pass_b_result,
    validate_pass_c_result,
)

_OPTIONS = frozenset({"A", "B", "C", "D"})
_CHUNK_1 = "11111111-1111-1111-1111-111111111111"
_CHUNK_2 = "22222222-2222-2222-2222-222222222222"
_FROZEN = frozenset({_CHUNK_1, _CHUNK_2})


def _source_support_context(**overrides) -> dict:
    base = {
        "attempted_retrieval": 2,
        "evidence_limitation": "no official source addressed this claim",
        "proposed_technical_claim": "claim text",
        "insufficiency_reason": "no matching chunk retrieved",
    }
    base.update(overrides)
    return base


def _finding(**overrides) -> dict:
    base = {
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
    base.update(overrides)
    return base


class TestPassAValidation(unittest.TestCase):

    def test_valid_single_select(self):
        result = validate_pass_a_result(
            {"selected_option_labels": ["A"]},
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
        )
        self.assertEqual(result, {"selected_option_labels": ["A"]})

    def test_valid_multi_select(self):
        result = validate_pass_a_result(
            {"selected_option_labels": ["A", "C"]},
            allowed_option_labels=_OPTIONS,
            required_selection_count=2,
        )
        self.assertEqual(result, {"selected_option_labels": ["A", "C"]})

    def test_wrong_selected_count(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "must contain exactly 2 label",
        ):
            validate_pass_a_result(
                {"selected_option_labels": ["A"]},
                allowed_option_labels=_OPTIONS,
                required_selection_count=2,
            )

    def test_unknown_option(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "not an allowed option label",
        ):
            validate_pass_a_result(
                {"selected_option_labels": ["Z"]},
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
            )

    def test_duplicate_option(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "duplicate label",
        ):
            validate_pass_a_result(
                {"selected_option_labels": ["A", "A"]},
                allowed_option_labels=_OPTIONS,
                required_selection_count=2,
            )

    def test_empty_selection(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "must not be empty",
        ):
            validate_pass_a_result(
                {"selected_option_labels": []},
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
            )

    def test_malformed_json_type(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "must be a JSON object",
        ):
            validate_pass_a_result(
                ["A"],
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
            )


class TestPassBValidation(unittest.TestCase):

    def test_valid_zero_proposed_findings(self):
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        self.assertEqual(result["proposed_findings"], [])

    def test_valid_blocking_proposed_finding(self):
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [_finding()],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        self.assertEqual(result["proposed_findings"][0]["finding_ref"], "F1")

    def test_duplicate_finding_ref(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "duplicate finding_ref",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        _finding(finding_ref="F1"),
                        _finding(finding_ref="F1", title="other"),
                    ],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_malformed_finding_object(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "must be a JSON object",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": ["not-an-object"],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_missing_metadata_rejected(self):
        finding = _finding()
        del finding["metadata"]
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "missing required field 'metadata'",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [finding],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_null_metadata_rejected(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            r"metadata must be a JSON object, got null",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [_finding(metadata=None)],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_non_object_metadata_rejected(self):
        invalid_values = {
            "array": [],
            "string": "meta",
            "number": 1,
            "boolean": True,
        }
        for label, value in invalid_values.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    AiQualityAuditValidationError,
                    r"metadata must be a JSON object",
                ):
                    validate_pass_b_result(
                        {
                            "selected_option_labels": ["A"],
                            "proposed_findings": [_finding(metadata=value)],
                        },
                        allowed_option_labels=_OPTIONS,
                        required_selection_count=1,
                        frozen_evidence_chunk_ids=_FROZEN,
                    )

    def test_empty_object_metadata_accepted(self):
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [_finding(metadata={})],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        self.assertEqual(result["proposed_findings"][0]["metadata"], {})

    def test_unsupported_finding_code(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "not a supported finding code",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [_finding(finding_code="NOT_A_REAL_CODE")],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_invalid_finding_type(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "not an allowed finding type",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [_finding(finding_type="domain_alignment")],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_evidence_chunk_outside_frozen_set(self):
        foreign = "33333333-3333-3333-3333-333333333333"
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "outside the frozen run evidence set",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [_finding(evidence_chunk_ids=[foreign])],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_duplicate_evidence_chunk(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "duplicate chunk id",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        _finding(evidence_chunk_ids=[_CHUNK_1, _CHUNK_1])
                    ],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_source_support_weak_rejects_blocking(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "cannot be 'blocking' when finding_code is SOURCE_SUPPORT_WEAK",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        _finding(
                            finding_ref="F2",
                            finding_code="SOURCE_SUPPORT_WEAK",
                            finding_type="source_support",
                            severity="medium",
                            materiality="blocking",
                            evidence_chunk_ids=[],
                            metadata={
                                "source_support_context": _source_support_context()
                            },
                        )
                    ],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_zero_evidence_source_support_missing_context_fields(self):
        text_fields = (
            "evidence_limitation",
            "proposed_technical_claim",
            "insufficiency_reason",
        )
        for field in text_fields:
            ctx = _source_support_context(**{field: "   "})
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    AiQualityAuditValidationError,
                    f"source_support_context.{field} must be non-empty",
                ):
                    validate_pass_b_result(
                        {
                            "selected_option_labels": ["A"],
                            "proposed_findings": [
                                _finding(
                                    finding_ref="F2",
                                    finding_code="SOURCE_SUPPORT_WEAK",
                                    finding_type="source_support",
                                    severity="medium",
                                    materiality="warning",
                                    evidence_chunk_ids=[],
                                    metadata={"source_support_context": ctx},
                                )
                            ],
                        },
                        allowed_option_labels=_OPTIONS,
                        required_selection_count=1,
                        frozen_evidence_chunk_ids=_FROZEN,
                    )

        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "attempted_retrieval must be a nonnegative integer",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        _finding(
                            finding_ref="F2",
                            finding_code="SOURCE_SUPPORT_WEAK",
                            finding_type="source_support",
                            severity="medium",
                            materiality="warning",
                            evidence_chunk_ids=[],
                            metadata={
                                "source_support_context": _source_support_context(
                                    attempted_retrieval=-1
                                )
                            },
                        )
                    ],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_domain_misalignment_wrong_type(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "must be 'coverage' when finding_code is DOMAIN_MISALIGNMENT",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        _finding(
                            finding_ref="F4",
                            finding_code="DOMAIN_MISALIGNMENT",
                            finding_type="source_support",
                            severity="medium",
                            materiality="warning",
                            evidence_chunk_ids=[],
                        )
                    ],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_domain_misalignment_submitted_as_blocking(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "cannot be 'blocking' when finding_code is DOMAIN_MISALIGNMENT",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        _finding(
                            finding_ref="F4",
                            finding_code="DOMAIN_MISALIGNMENT",
                            finding_type="coverage",
                            severity="medium",
                            materiality="blocking",
                            evidence_chunk_ids=[],
                        )
                    ],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )


class TestPassCValidation(unittest.TestCase):

    def test_valid_normal_resolved_dispute(self):
        result = validate_pass_c_result(
            {
                "resolution_type": "NORMAL_DISPUTE",
                "resolution_status": "RESOLVED",
                "substituted_for_passes": [],
                "confirmed_finding_refs": ["F1"],
            },
            pass_b_proposed_finding_refs={"F1", "F2"},
        )
        self.assertEqual(result["confirmed_finding_refs"], ["F1"])
        self.assertNotIn("proposed_findings", result)

    def test_valid_unresolved_dispute(self):
        result = validate_pass_c_result(
            {
                "resolution_type": "NORMAL_DISPUTE",
                "resolution_status": "UNRESOLVED",
                "substituted_for_passes": [],
                "confirmed_finding_refs": [],
            },
        )
        self.assertEqual(result["confirmed_finding_refs"], [])
        self.assertNotIn("proposed_findings", result)

    def test_valid_pass_a_substitution(self):
        result = validate_pass_c_result(
            {
                "resolution_type": "PASS_A_SUBSTITUTION",
                "resolution_status": "RESOLVED",
                "substituted_for_passes": ["A", "B"],
                "confirmed_finding_refs": ["F1"],
                "proposed_findings": [_finding()],
            },
            frozen_evidence_chunk_ids=_FROZEN,
        )
        self.assertEqual(result["proposed_findings"][0]["finding_ref"], "F1")

    def test_valid_pass_b_substitution(self):
        result = validate_pass_c_result(
            {
                "resolution_type": "PASS_B_SUBSTITUTION",
                "resolution_status": "RESOLVED",
                "substituted_for_passes": ["B"],
                "confirmed_finding_refs": ["F1"],
                "proposed_findings": [_finding()],
            },
            frozen_evidence_chunk_ids=_FROZEN,
        )
        self.assertEqual(result["substituted_for_passes"], ["B"])

    def test_discriminator_mismatch(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "requires substituted_for_passes=\\['A', 'B'\\]",
        ):
            validate_pass_c_result(
                {
                    "resolution_type": "PASS_A_SUBSTITUTION",
                    "resolution_status": "RESOLVED",
                    "substituted_for_passes": [],
                    "confirmed_finding_refs": [],
                    "proposed_findings": [],
                },
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_confirmed_ref_absent_from_upstream_proposals(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "is not present in Pass B proposed_findings",
        ):
            validate_pass_c_result(
                {
                    "resolution_type": "NORMAL_DISPUTE",
                    "resolution_status": "RESOLVED",
                    "substituted_for_passes": [],
                    "confirmed_finding_refs": ["F9"],
                },
                pass_b_proposed_finding_refs={"F1"},
            )

    def test_duplicate_confirmed_refs(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "duplicate value",
        ):
            validate_pass_c_result(
                {
                    "resolution_type": "NORMAL_DISPUTE",
                    "resolution_status": "RESOLVED",
                    "substituted_for_passes": [],
                    "confirmed_finding_refs": ["F1", "F1"],
                },
                pass_b_proposed_finding_refs={"F1"},
            )

    def test_unresolved_result_containing_confirmed_refs(self):
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "must be empty when resolution_status is UNRESOLVED",
        ):
            validate_pass_c_result(
                {
                    "resolution_type": "NORMAL_DISPUTE",
                    "resolution_status": "UNRESOLVED",
                    "substituted_for_passes": [],
                    "confirmed_finding_refs": ["F1"],
                },
            )

    def test_substitution_finding_with_evidence_outside_frozen_set(self):
        foreign = "33333333-3333-3333-3333-333333333333"
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "outside the frozen run evidence set",
        ):
            validate_pass_c_result(
                {
                    "resolution_type": "PASS_B_SUBSTITUTION",
                    "resolution_status": "RESOLVED",
                    "substituted_for_passes": ["B"],
                    "confirmed_finding_refs": ["F1"],
                    "proposed_findings": [
                        _finding(evidence_chunk_ids=[foreign])
                    ],
                },
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_substitution_missing_metadata_rejected(self):
        finding = _finding()
        del finding["metadata"]
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "missing required field 'metadata'",
        ):
            validate_pass_c_result(
                {
                    "resolution_type": "PASS_B_SUBSTITUTION",
                    "resolution_status": "RESOLVED",
                    "substituted_for_passes": ["B"],
                    "confirmed_finding_refs": ["F1"],
                    "proposed_findings": [finding],
                },
                frozen_evidence_chunk_ids=_FROZEN,
            )


if __name__ == "__main__":
    unittest.main()
