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
    derive_correctness_finding,
    derive_explanation_finding,
    merge_pass_b_findings,
    validate_pass_a_result,
    validate_pass_b_correctness_result,
    validate_pass_b_result,
    validate_pass_c_result,
)
from workers.quality_benchmark_execution import _summarize_findings

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


class TestCanonicalMaterialityEnforcement(unittest.TestCase):
    """V59-FINDING-01: ``assign_materiality()`` must be the sole materiality
    authority reaching persistence, for both Pass B primary proposals and
    Pass C substitution proposals, regardless of what the provider itself
    reported.
    """

    def _proposed_finding(self, result: dict, finding_ref: str = "F1") -> dict:
        for item in result["proposed_findings"]:
            if item["finding_ref"] == finding_ref:
                return item
        raise AssertionError(f"finding_ref {finding_ref!r} not found in {result!r}")

    def test_explanation_missing_warning_is_canonicalized_to_blocking_in_pass_b(self):
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [
                    _finding(
                        finding_code="EXPLANATION_MISSING",
                        finding_type="explanation_quality",
                        severity="medium",
                        materiality="warning",
                    )
                ],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        finding = self._proposed_finding(result)
        self.assertEqual(finding["materiality"], "blocking")

    def test_explanation_missing_warning_is_canonicalized_in_pass_c_substitution(self):
        """Primary (Pass B) and dispute (Pass C) proposals share the exact
        same canonicalization path (``_validate_proposed_finding``)."""
        result = validate_pass_c_result(
            {
                "resolution_type": "PASS_B_SUBSTITUTION",
                "resolution_status": "RESOLVED",
                "substituted_for_passes": ["B"],
                "confirmed_finding_refs": ["F1"],
                "proposed_findings": [
                    _finding(
                        finding_code="EXPLANATION_MISSING",
                        finding_type="explanation_quality",
                        severity="medium",
                        materiality="warning",
                    )
                ],
            },
            frozen_evidence_chunk_ids=_FROZEN,
        )
        finding = self._proposed_finding(result)
        self.assertEqual(finding["materiality"], "blocking")

    def test_provider_materiality_preserved_in_metadata_when_overridden(self):
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [
                    _finding(
                        finding_code="EXPLANATION_MISSING",
                        finding_type="explanation_quality",
                        severity="medium",
                        materiality="warning",
                        metadata={"note": "kept"},
                    )
                ],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        finding = self._proposed_finding(result)
        self.assertEqual(finding["materiality"], "blocking")
        self.assertEqual(finding["metadata"]["provider_materiality"], "warning")
        # Pre-existing metadata is untouched, not discarded.
        self.assertEqual(finding["metadata"]["note"], "kept")

    def test_legitimate_warning_only_finding_remains_warning(self):
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [
                    _finding(
                        finding_code="WEAK_DISTRACTORS",
                        finding_type="answer_quality",
                        severity="low",
                        materiality="warning",
                    )
                ],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        finding = self._proposed_finding(result)
        self.assertEqual(finding["materiality"], "warning")
        # No mismatch occurred, so no provenance clutter is added.
        self.assertNotIn("provider_materiality", finding["metadata"])

    def test_provider_supplied_blocking_cannot_override_canonical_warning(self):
        """A provider cannot escalate a canonically-warning code to blocking
        either -- canonical policy is the sole authority in both directions.
        """
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [
                    _finding(
                        finding_code="WEAK_DISTRACTORS",
                        finding_type="answer_quality",
                        severity="high",
                        materiality="blocking",
                    )
                ],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        finding = self._proposed_finding(result)
        self.assertEqual(finding["materiality"], "warning")
        self.assertEqual(finding["metadata"]["provider_materiality"], "blocking")

    def test_canonicalization_is_idempotent(self):
        once = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [
                    _finding(
                        finding_code="EXPLANATION_MISSING",
                        finding_type="explanation_quality",
                        severity="medium",
                        materiality="warning",
                    )
                ],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        canonicalized_finding = self._proposed_finding(once)

        twice = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [canonicalized_finding],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        finding = self._proposed_finding(twice)
        self.assertEqual(finding["materiality"], "blocking")
        # Re-running canonicalization on already-canonical input must not
        # keep stacking provenance or otherwise change the result.
        self.assertEqual(finding["metadata"], canonicalized_finding["metadata"])

    def test_unsupported_finding_code_still_fails_closed_not_invented(self):
        """Unknown/unsupported codes are rejected by the existing enum
        policy rather than being handed an invented materiality."""
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "not a supported finding code",
        ):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [
                        _finding(finding_code="NOT_A_REAL_CODE", materiality="warning")
                    ],
                },
                allowed_option_labels=_OPTIONS,
                required_selection_count=1,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_qbv1_010_regression_full_path_without_ai_call(self):
        """Regression for the confirmed qbv1-010 defect: both providers
        emitted ``EXPLANATION_MISSING@warning``; canonical policy requires
        ``EXPLANATION_MISSING@blocking``. Chains the exact two boundaries
        the defect crossed -- pass-result schema validation (persistence)
        and ``_summarize_findings`` (approval/publication-gate) -- with no
        AI call and no database involved.
        """
        provider_materiality = "warning"
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [
                    _finding(
                        finding_ref="qbv1-010-f1",
                        finding_code="EXPLANATION_MISSING",
                        finding_type="explanation_quality",
                        severity="medium",
                        materiality=provider_materiality,
                        title="Explanation missing",
                        description="No explanation was provided for the correct answer.",
                    )
                ],
            },
            allowed_option_labels=_OPTIONS,
            required_selection_count=1,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        persisted_finding = self._proposed_finding(result, finding_ref="qbv1-010-f1")
        persisted_materiality = persisted_finding["materiality"]
        self.assertEqual(persisted_materiality, "blocking")

        codes, overall_materiality, approved = _summarize_findings([persisted_finding])
        self.assertEqual(codes, ["EXPLANATION_MISSING"])
        self.assertEqual(overall_materiality, "blocking")
        publication_blocked = overall_materiality == "blocking"

        self.assertEqual(provider_materiality, "warning")
        self.assertEqual(persisted_materiality, "blocking")
        self.assertFalse(approved)
        self.assertTrue(publication_blocked)


def _option_judgment(
    label: str,
    verdict: str,
    *,
    chunk_ids=None,
    rationale=None,
    answer_completeness=None,
) -> dict:
    item = {
        "option_label": label,
        "verdict": verdict,
        "citation_chunk_ids": list(chunk_ids or []),
        "evidence_rationale": rationale or f"Evidence assessment for option {label}.",
    }
    if answer_completeness is not None:
        item["answer_completeness"] = answer_completeness
    return item


def _correctness_payload(
    *,
    supported=("A",),
    not_supported=("B", "C", "D"),
    insufficient=(),
    evidence_sufficient=True,
    abstention_reason=None,
    citation_chunk_id=_CHUNK_1,
    unresolved_risk=None,
) -> dict:
    citation_ids = [citation_chunk_id] if citation_chunk_id else []
    judgments = []
    for label in supported:
        judgments.append(
            _option_judgment(label, "SUPPORTED_AS_CORRECT", chunk_ids=citation_ids)
        )
    for label in not_supported:
        judgments.append(
            _option_judgment(label, "NOT_SUPPORTED_AS_CORRECT", chunk_ids=citation_ids)
        )
    for label in insufficient:
        judgments.append(_option_judgment(label, "INSUFFICIENT_EVIDENCE"))
    payload = {
        "option_judgments": judgments,
        "evidence_sufficient_for_decision": evidence_sufficient,
        "abstention_reason": abstention_reason,
    }
    if unresolved_risk is not None:
        payload["unresolved_options_could_change_answer_set"] = unresolved_risk
    return payload


class TestPassBCorrectnessValidation(unittest.TestCase):
    """V60-IMPL-01: specialized answer-correctness detector schema validation."""

    def test_valid_decisive_result_one_judgment_per_option(self):
        result = validate_pass_b_correctness_result(
            _correctness_payload(),
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        self.assertEqual(len(result["option_judgments"]), 4)
        labels = {item["option_label"] for item in result["option_judgments"]}
        self.assertEqual(labels, set(_OPTIONS))
        self.assertTrue(result["evidence_sufficient_for_decision"])
        self.assertIsNone(result["abstention_reason"])

    def test_missing_option_judgment_rejected(self):
        payload = _correctness_payload(supported=("A",), not_supported=("B", "C"))
        with self.assertRaisesRegex(AiQualityAuditValidationError, "missing judgments"):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_duplicate_option_judgment_rejected(self):
        payload = _correctness_payload()
        payload["option_judgments"].append(
            _option_judgment("A", "NOT_SUPPORTED_AS_CORRECT", chunk_ids=[_CHUNK_1])
        )
        with self.assertRaisesRegex(AiQualityAuditValidationError, "duplicate label"):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_unknown_option_label_rejected(self):
        payload = _correctness_payload(supported=("A",), not_supported=("B", "C", "D", "Z"))
        with self.assertRaisesRegex(AiQualityAuditValidationError, "not an allowed option label"):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_invalid_citation_id_outside_frozen_set_rejected(self):
        payload = _correctness_payload(citation_chunk_id="99999999-9999-9999-9999-999999999999")
        with self.assertRaisesRegex(AiQualityAuditValidationError, "outside"):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_supported_verdict_without_citation_rejected(self):
        payload = _correctness_payload()
        payload["option_judgments"][0]["citation_chunk_ids"] = []
        with self.assertRaisesRegex(AiQualityAuditValidationError, "no citation_chunk_ids"):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_evidence_sufficient_true_with_insufficient_judgment_rejected(self):
        payload = _correctness_payload(
            supported=("A",),
            not_supported=("B", "C"),
            insufficient=("D",),
            evidence_sufficient=True,
            abstention_reason=None,
        )
        with self.assertRaisesRegex(
            AiQualityAuditValidationError, "cannot be\\s+true while"
        ):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_abstention_reason_required_when_evidence_insufficient(self):
        payload = _correctness_payload(
            supported=(),
            not_supported=(),
            insufficient=("A", "B", "C", "D"),
            evidence_sufficient=False,
            abstention_reason=None,
        )
        with self.assertRaisesRegex(AiQualityAuditValidationError, "abstention_reason"):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_abstention_reason_must_be_null_when_evidence_sufficient(self):
        payload = _correctness_payload(evidence_sufficient=True, abstention_reason="some reason")
        with self.assertRaisesRegex(AiQualityAuditValidationError, "must be null"):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_empty_frozen_evidence_forces_abstention(self):
        payload = _correctness_payload(
            supported=(),
            not_supported=(),
            insufficient=("A", "B", "C", "D"),
            evidence_sufficient=False,
            abstention_reason="No frozen evidence is available for this run.",
        )
        result = validate_pass_b_correctness_result(
            payload,
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=frozenset(),
        )
        self.assertFalse(result["evidence_sufficient_for_decision"])

    def test_empty_frozen_evidence_rejects_claimed_sufficiency(self):
        payload = _correctness_payload(
            supported=(),
            not_supported=("A", "B", "C", "D"),
            evidence_sufficient=True,
            abstention_reason=None,
            citation_chunk_id=None,
        )
        with self.assertRaisesRegex(
            AiQualityAuditValidationError, "zero frozen evidence chunks"
        ):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=frozenset(),
            )


class TestDeriveCorrectnessFinding(unittest.TestCase):
    """V60-IMPL-01: deterministic finding derivation from specialist output."""

    def test_exact_stored_set_produces_no_finding(self):
        result = _correctness_payload(supported=("A",), not_supported=("B", "C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNone(finding)

    def test_alternative_set_of_required_size_yields_wrong_answer_key(self):
        result = _correctness_payload(supported=("B",), not_supported=("A", "C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")
        self.assertEqual(finding["finding_type"], "correctness")

    def test_more_supported_than_required_yields_multiple_defensible_answers(self):
        result = _correctness_payload(supported=("A", "B"), not_supported=("C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")

    def test_fewer_supported_than_required_yields_unsupported_answer(self):
        result = _correctness_payload(supported=(), not_supported=("A", "B", "C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "UNSUPPORTED_ANSWER")

    def test_insufficient_evidence_never_becomes_unsupported_answer(self):
        result = _correctness_payload(
            supported=(),
            not_supported=(),
            insufficient=("A", "B", "C", "D"),
            evidence_sufficient=False,
            abstention_reason="Evidence does not address any option.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertNotEqual(finding["finding_code"], "UNSUPPORTED_ANSWER")
        self.assertEqual(finding["finding_code"], "OTHER_REVIEW_NEEDED")
        self.assertEqual(finding["finding_type"], "correctness")
        # Materiality assigned here is later re-derived canonically through
        # merge_pass_b_findings -> assign_materiality; this asserts intent only.
        self.assertEqual(finding["materiality"], "blocking")

    def test_stored_answer_confirmed_with_insufficient_distractor_produces_no_finding(self):
        """V60-DERIVE-01 (test 1): a non-stored distractor the specialist
        could not decide is never, by itself, a reason to abstain once the
        stored answer is itself decisively confirmed and nothing else is
        supported."""
        result = _correctness_payload(
            supported=("A",),
            not_supported=("C", "D"),
            insufficient=("B",),
            evidence_sufficient=False,
            abstention_reason="Option B could not be judged from evidence.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNone(finding)

    def test_multi_select_exact_match_produces_no_finding(self):
        result = _correctness_payload(supported=("A", "B"), not_supported=("C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A", "B"],
            required_selection_count=2,
        )
        self.assertIsNone(finding)

    def test_multi_select_wrong_answer_key(self):
        result = _correctness_payload(supported=("A", "C"), not_supported=("B", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A", "B"],
            required_selection_count=2,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")

    def test_multi_select_unsupported_answer(self):
        result = _correctness_payload(supported=("A",), not_supported=("B", "C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A", "B"],
            required_selection_count=2,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "UNSUPPORTED_ANSWER")

    def test_multi_select_supported_alternative_of_required_size_with_insufficient_stored(self):
        """V60-DERIVE-01 (test 7): multi-select analog of qbv1-006/007 --
        neither stored label is contradicted (both INSUFFICIENT_EVIDENCE),
        but a full alternative set of required size is decisively
        supported elsewhere -> UNSUPPORTED_ANSWER, not WRONG_ANSWER_KEY."""
        result = _correctness_payload(
            supported=("C", "D"),
            not_supported=(),
            insufficient=("A", "B"),
            evidence_sufficient=False,
            abstention_reason="Options A and B could not be judged from evidence.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A", "B"],
            required_selection_count=2,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "UNSUPPORTED_ANSWER")

    # -- V60-DERIVE-01 required tests 1-6 -----------------------------------

    def test_required_1_stored_supported_unrelated_distractor_insufficient_no_finding(self):
        result = _correctness_payload(
            supported=("A",),
            not_supported=("C",),
            insufficient=("B", "D"),
            evidence_sufficient=False,
            abstention_reason="Options B and D could not be judged from evidence.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNone(finding)

    def test_required_2_supported_alternative_and_stored_contradicted_yields_wrong_answer_key(self):
        result = _correctness_payload(
            supported=("A",),
            not_supported=("B", "C"),
            insufficient=("D",),
            evidence_sufficient=False,
            abstention_reason="Option D could not be judged from evidence.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["B"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")

    def test_required_3_supported_alternative_and_stored_insufficient_yields_unsupported_answer(self):
        """Matches the captured qbv1-006/qbv1-007 telemetry pattern."""
        result = _correctness_payload(
            supported=("A",),
            not_supported=(),
            insufficient=("B", "C", "D"),
            evidence_sufficient=False,
            abstention_reason="Options B, C, and D could not be judged from evidence.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["B"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "UNSUPPORTED_ANSWER")

    def test_required_4_more_than_required_supported_stored_not_supported_yields_multiple_defensible(self):
        result = _correctness_payload(supported=("B", "C"), not_supported=("A", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")

    def test_required_5_more_than_required_supported_stored_included_yields_other_review_needed(self):
        """Matches the captured qbv1-037 telemetry pattern (trap/meta-option
        protection): stored is among the supported set, but an unresolved
        option remains -- must not auto-resolve to MULTIPLE_DEFENSIBLE_ANSWERS."""
        result = _correctness_payload(
            supported=("A", "B"),
            not_supported=("D",),
            insufficient=("C",),
            evidence_sufficient=False,
            abstention_reason="Option C could not be judged from evidence.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "OTHER_REVIEW_NEEDED")

    def test_multiple_supported_including_stored_but_fully_resolved_yields_multiple_defensible(self):
        """Matches the captured qbv1-036 telemetry pattern: stored is among
        the supported set (same shape as qbv1-037 above), but here every
        option is decisively resolved (no INSUFFICIENT_EVIDENCE anywhere) --
        this is a confirmed tie, not an unresolved trap/meta-option, so it
        must resolve to MULTIPLE_DEFENSIBLE_ANSWERS rather than abstaining."""
        result = _correctness_payload(supported=("A", "B"), not_supported=("C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")

    def test_required_6_all_options_insufficient_yields_other_review_needed(self):
        """Matches the captured qbv1-020/qbv1-030 telemetry pattern: even
        the stored answer itself is unresolved -- must remain human review."""
        result = _correctness_payload(
            supported=(),
            not_supported=(),
            insufficient=("A", "B", "C", "D"),
            evidence_sufficient=False,
            abstention_reason="No option could be judged from evidence.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "OTHER_REVIEW_NEEDED")

    def test_required_9_canonical_materiality_unchanged_for_correctness_findings(self):
        """V60-DERIVE-01 (test 9): correctness findings still self-report
        materiality=blocking (later re-derived canonically through
        merge_pass_b_findings -> assign_materiality); this rule change does
        not alter that contract for any decisive branch."""
        for supported, not_supported, stored, code in (
            (("B",), ("A", "C", "D"), ["A"], "WRONG_ANSWER_KEY"),
            (("A",), (), ["B"], "UNSUPPORTED_ANSWER"),
            (("A", "B"), ("C", "D"), ["A"], "MULTIPLE_DEFENSIBLE_ANSWERS"),
        ):
            insufficient = () if code != "UNSUPPORTED_ANSWER" else ("B", "C", "D")
            result = _correctness_payload(
                supported=supported,
                not_supported=not_supported,
                insufficient=insufficient,
                evidence_sufficient=not insufficient,
                abstention_reason=None if not insufficient else "insufficient",
            )
            finding = derive_correctness_finding(
                correctness_result=result,
                stored_correct_option_labels=stored,
                required_selection_count=1,
            )
            self.assertIsNotNone(finding)
            self.assertEqual(finding["finding_code"], code)
            self.assertEqual(finding["materiality"], "blocking")
            self.assertEqual(finding["finding_type"], "correctness")

    def test_required_10_eleven_captured_telemetry_cases_exact_outcomes(self):
        """V60-DERIVE-01 (test 10): exact expected outcomes for the 11 cases
        captured in .local/v58_openai_baseline/20260708T192911Z/result.json
        (specialist option_judgments) cross-referenced against the stored
        answer key in workers/fixtures/quality_benchmark_v1_sme_reviewed.json
        (``question.options[*].is_correct``), transcribed here as literal
        data so this test does not depend on any file outside the repo.

        Of the 11, 8 resolve deterministically and 3 remain
        OTHER_REVIEW_NEEDED (qbv1-020, qbv1-030, qbv1-037) -- matching the
        validated 3/11 remaining human-review expectation.
        """
        cases = [
            (
                "qbv1-006",
                {"A": "SUPPORTED_AS_CORRECT", "B": "INSUFFICIENT_EVIDENCE",
                 "C": "INSUFFICIENT_EVIDENCE", "D": "INSUFFICIENT_EVIDENCE"},
                ["B"],
                "UNSUPPORTED_ANSWER",
            ),
            (
                "qbv1-007",
                {"A": "SUPPORTED_AS_CORRECT", "B": "INSUFFICIENT_EVIDENCE",
                 "C": "INSUFFICIENT_EVIDENCE", "D": "INSUFFICIENT_EVIDENCE"},
                ["B"],
                "UNSUPPORTED_ANSWER",
            ),
            (
                "qbv1-019",
                {"A": "SUPPORTED_AS_CORRECT", "B": "INSUFFICIENT_EVIDENCE",
                 "C": "INSUFFICIENT_EVIDENCE", "D": "INSUFFICIENT_EVIDENCE"},
                ["A"],
                None,
            ),
            (
                "qbv1-020",
                {"A": "INSUFFICIENT_EVIDENCE", "B": "INSUFFICIENT_EVIDENCE",
                 "C": "INSUFFICIENT_EVIDENCE", "D": "INSUFFICIENT_EVIDENCE"},
                ["A"],
                "OTHER_REVIEW_NEEDED",
            ),
            (
                "qbv1-029",
                {"A": "SUPPORTED_AS_CORRECT", "B": "INSUFFICIENT_EVIDENCE",
                 "C": "INSUFFICIENT_EVIDENCE", "D": "INSUFFICIENT_EVIDENCE"},
                ["A"],
                None,
            ),
            (
                "qbv1-030",
                {"A": "INSUFFICIENT_EVIDENCE", "B": "INSUFFICIENT_EVIDENCE",
                 "C": "INSUFFICIENT_EVIDENCE", "D": "INSUFFICIENT_EVIDENCE"},
                ["A"],
                "OTHER_REVIEW_NEEDED",
            ),
            (
                "qbv1-034",
                {"A": "SUPPORTED_AS_CORRECT", "B": "NOT_SUPPORTED_AS_CORRECT",
                 "C": "NOT_SUPPORTED_AS_CORRECT", "D": "INSUFFICIENT_EVIDENCE"},
                ["B"],
                "WRONG_ANSWER_KEY",
            ),
            (
                "qbv1-036",
                {"A": "SUPPORTED_AS_CORRECT", "B": "SUPPORTED_AS_CORRECT",
                 "C": "NOT_SUPPORTED_AS_CORRECT", "D": "NOT_SUPPORTED_AS_CORRECT"},
                ["A"],
                "MULTIPLE_DEFENSIBLE_ANSWERS",
            ),
            (
                "qbv1-037",
                {"A": "SUPPORTED_AS_CORRECT", "B": "SUPPORTED_AS_CORRECT",
                 "C": "INSUFFICIENT_EVIDENCE", "D": "NOT_SUPPORTED_AS_CORRECT"},
                ["A"],
                "OTHER_REVIEW_NEEDED",
            ),
            (
                "qbv1-039",
                {"A": "SUPPORTED_AS_CORRECT", "B": "INSUFFICIENT_EVIDENCE",
                 "C": "INSUFFICIENT_EVIDENCE", "D": "INSUFFICIENT_EVIDENCE"},
                ["A"],
                None,
            ),
            (
                "qbv1-040",
                {"A": "SUPPORTED_AS_CORRECT", "B": "INSUFFICIENT_EVIDENCE",
                 "C": "INSUFFICIENT_EVIDENCE", "D": "INSUFFICIENT_EVIDENCE"},
                ["A"],
                None,
            ),
        ]

        review_needed_count = 0
        for case_id, verdicts, stored, expected_code in cases:
            with self.subTest(case_id=case_id):
                supported = tuple(l for l, v in verdicts.items() if v == "SUPPORTED_AS_CORRECT")
                not_supported = tuple(l for l, v in verdicts.items() if v == "NOT_SUPPORTED_AS_CORRECT")
                insufficient = tuple(l for l, v in verdicts.items() if v == "INSUFFICIENT_EVIDENCE")
                result = _correctness_payload(
                    supported=supported,
                    not_supported=not_supported,
                    insufficient=insufficient,
                    evidence_sufficient=not insufficient,
                    abstention_reason=None if not insufficient else "captured telemetry abstention",
                )
                finding = derive_correctness_finding(
                    correctness_result=result,
                    stored_correct_option_labels=stored,
                    required_selection_count=1,
                )
                if expected_code is None:
                    self.assertIsNone(finding, f"{case_id}: expected no finding")
                else:
                    self.assertIsNotNone(finding, f"{case_id}: expected {expected_code}")
                    self.assertEqual(finding["finding_code"], expected_code, case_id)
                    if expected_code == "OTHER_REVIEW_NEEDED":
                        review_needed_count += 1

        self.assertEqual(
            review_needed_count, 3,
            "expected exactly 3/11 captured cases to remain OTHER_REVIEW_NEEDED",
        )

    def test_derivation_is_deterministic_across_repeated_calls(self):
        result = _correctness_payload(supported=("B",), not_supported=("A", "C", "D"))
        first = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        second = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(first, second)

    # -- V60-DERIVE-03: derived_correctness_finding provenance marker -------

    def test_wrong_answer_key_includes_derived_correctness_finding_marker(self):
        result = _correctness_payload(supported=("B",), not_supported=("A", "C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")
        self.assertIs(finding["metadata"]["derived_correctness_finding"], True)
        # Pre-existing provenance keys must remain present and unrenamed.
        self.assertIn("correctness_detector_supported_labels", finding["metadata"])
        self.assertIn("correctness_detector_stored_labels", finding["metadata"])

    def test_multiple_defensible_answers_includes_derived_correctness_finding_marker(self):
        result = _correctness_payload(supported=("A", "B"), not_supported=("C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")
        self.assertIs(finding["metadata"]["derived_correctness_finding"], True)
        self.assertIn("correctness_detector_supported_labels", finding["metadata"])
        self.assertIn("correctness_detector_stored_labels", finding["metadata"])

    def test_unsupported_answer_includes_derived_correctness_finding_marker(self):
        result = _correctness_payload(supported=(), not_supported=("A", "B", "C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "UNSUPPORTED_ANSWER")
        self.assertIs(finding["metadata"]["derived_correctness_finding"], True)
        self.assertIn("correctness_detector_supported_labels", finding["metadata"])
        self.assertIn("correctness_detector_stored_labels", finding["metadata"])

    def test_unsupported_answer_rule_4a_includes_derived_correctness_finding_marker(self):
        """Rule 4a (too-few-supported, all stored labels decisive) is a
        distinct return site from rule 3b -- covered separately so every
        UNSUPPORTED_ANSWER-producing branch is verified independently."""
        result = _correctness_payload(
            supported=(), not_supported=("A",), insufficient=("B", "C", "D"),
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "UNSUPPORTED_ANSWER")
        self.assertIs(finding["metadata"]["derived_correctness_finding"], True)

    def test_other_review_needed_includes_marker_and_retains_abstention_flag(self):
        result = _correctness_payload(
            supported=(),
            not_supported=(),
            insufficient=("A", "B", "C", "D"),
            evidence_sufficient=False,
            abstention_reason="No option could be judged from evidence.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "OTHER_REVIEW_NEEDED")
        self.assertIs(finding["metadata"]["derived_correctness_finding"], True)
        # The new marker must never displace or rename the pre-existing
        # abstention provenance keys.
        self.assertIs(finding["metadata"]["correctness_detector_abstained"], True)
        self.assertIn("abstention_reason", finding["metadata"])

    def test_other_review_needed_rule_2a_includes_marker(self):
        """The rule 2a (trap/meta-option) abstention path also calls
        ``_abstain()`` -- covered separately from the rule 5 catch-all
        above to verify both call sites of the shared helper."""
        result = _correctness_payload(
            supported=("A", "B"),
            not_supported=("D",),
            insufficient=("C",),
            evidence_sufficient=False,
            abstention_reason="Option C could not be judged from evidence.",
        )
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "OTHER_REVIEW_NEEDED")
        self.assertIs(finding["metadata"]["derived_correctness_finding"], True)
        self.assertIs(finding["metadata"]["correctness_detector_abstained"], True)

    def test_no_finding_case_is_unaffected_by_marker_change(self):
        """Rule 1 (exact stored set confirmed) still returns None -- the
        marker change touches only the metadata of returned findings, never
        whether a finding is returned at all."""
        result = _correctness_payload(supported=("A",), not_supported=("B", "C", "D"))
        finding = derive_correctness_finding(
            correctness_result=result,
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNone(finding)

    def test_marker_addition_does_not_alter_finding_codes_or_other_fields(self):
        """V60-DERIVE-03 acceptance: existing derivation outputs and finding
        codes are otherwise unchanged -- every field except metadata is
        byte-for-byte identical to the pre-V60-DERIVE-03 shape, and the only
        metadata change is the additive derived_correctness_finding key."""
        cases = (
            (("B",), ("A", "C", "D"), (), ["A"], "WRONG_ANSWER_KEY"),
            (("A", "B"), ("C", "D"), (), ["A"], "MULTIPLE_DEFENSIBLE_ANSWERS"),
            ((), ("A", "B", "C", "D"), (), ["A"], "UNSUPPORTED_ANSWER"),
        )
        for supported, not_supported, insufficient, stored, expected_code in cases:
            result = _correctness_payload(
                supported=supported, not_supported=not_supported, insufficient=insufficient,
            )
            finding = derive_correctness_finding(
                correctness_result=result,
                stored_correct_option_labels=stored,
                required_selection_count=1,
            )
            self.assertEqual(finding["finding_code"], expected_code)
            self.assertEqual(finding["finding_type"], "correctness")
            self.assertEqual(finding["severity"], "high")
            self.assertEqual(finding["materiality"], "blocking")
            self.assertEqual(finding["finding_ref"], "FC1")
            metadata_without_marker = dict(finding["metadata"])
            self.assertEqual(metadata_without_marker.pop("derived_correctness_finding"), True)
            self.assertEqual(
                metadata_without_marker,
                {
                    "correctness_detector_supported_labels": sorted(supported),
                    "correctness_detector_stored_labels": sorted(stored),
                },
            )


class TestAnswerCompletenessValidation(unittest.TestCase):
    """V60-DERIVE-06: per-option answer_completeness validation."""

    def test_valid_fully_responsive_classification(self):
        payload = _correctness_payload(
            supported=("C",),
            not_supported=("A", "B", "D"),
        )
        payload["option_judgments"][0]["answer_completeness"] = "FULLY_RESPONSIVE"
        for item in payload["option_judgments"][1:]:
            item["answer_completeness"] = "NOT_APPLICABLE"
        result = validate_pass_b_correctness_result(
            payload,
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        by_label = {item["option_label"]: item for item in result["option_judgments"]}
        self.assertEqual(by_label["C"]["answer_completeness"], "FULLY_RESPONSIVE")

    def test_valid_partial_component_classification(self):
        payload = _correctness_payload(
            supported=("A", "C"),
            not_supported=("B", "D"),
        )
        for item in payload["option_judgments"]:
            if item["option_label"] == "A":
                item["answer_completeness"] = "PARTIAL_COMPONENT"
            elif item["option_label"] == "C":
                item["answer_completeness"] = "FULLY_RESPONSIVE"
            else:
                item["answer_completeness"] = "NOT_APPLICABLE"
        result = validate_pass_b_correctness_result(
            payload,
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        by_label = {item["option_label"]: item for item in result["option_judgments"]}
        self.assertEqual(by_label["A"]["answer_completeness"], "PARTIAL_COMPONENT")
        self.assertEqual(by_label["C"]["answer_completeness"], "FULLY_RESPONSIVE")

    def test_non_supported_verdict_requires_not_applicable(self):
        payload = _correctness_payload(supported=("A",), not_supported=("B", "C", "D"))
        payload["option_judgments"][1]["answer_completeness"] = "FULLY_RESPONSIVE"
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "answer_completeness must be NOT_APPLICABLE",
        ):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_unknown_completeness_enum_rejected(self):
        payload = _correctness_payload(supported=("A",), not_supported=("B", "C", "D"))
        payload["option_judgments"][0]["answer_completeness"] = "MAYBE_COMPLETE"
        with self.assertRaisesRegex(AiQualityAuditValidationError, "answer_completeness"):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_missing_field_normalized_to_not_applicable(self):
        result = validate_pass_b_correctness_result(
            _correctness_payload(),
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        for item in result["option_judgments"]:
            self.assertEqual(item["answer_completeness"], "NOT_APPLICABLE")


class TestUnresolvedAnswerSetRiskValidation(unittest.TestCase):
    """V60-DERIVE-12: unresolved_options_could_change_answer_set validation."""

    def test_true_accepted_when_insufficient_and_not_sufficient(self):
        payload = _correctness_payload(
            supported=("A",),
            not_supported=("D",),
            insufficient=("B", "C"),
            evidence_sufficient=False,
            abstention_reason="Cannot rule out B as an alternative correct answer.",
            unresolved_risk=True,
        )
        result = validate_pass_b_correctness_result(
            payload,
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        self.assertIs(result["unresolved_options_could_change_answer_set"], True)

    def test_false_accepted(self):
        payload = _correctness_payload(
            supported=("A",),
            not_supported=("B", "C"),
            insufficient=("D",),
            evidence_sufficient=False,
            abstention_reason="Option D could not be fully judged.",
            unresolved_risk=False,
        )
        result = validate_pass_b_correctness_result(
            payload,
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        self.assertIs(result["unresolved_options_could_change_answer_set"], False)

    def test_missing_field_normalized_to_false(self):
        result = validate_pass_b_correctness_result(
            _correctness_payload(),
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=_FROZEN,
        )
        self.assertIs(result["unresolved_options_could_change_answer_set"], False)

    def test_non_boolean_rejected(self):
        payload = _correctness_payload()
        payload["unresolved_options_could_change_answer_set"] = "yes"
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "unresolved_options_could_change_answer_set",
        ):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_true_with_evidence_sufficient_rejected(self):
        payload = _correctness_payload(
            supported=("A",),
            not_supported=("B", "C", "D"),
            unresolved_risk=True,
        )
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "must be false when evidence_sufficient_for_decision is true",
        ):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_true_with_no_insufficient_option_rejected(self):
        payload = _correctness_payload(
            supported=("A",),
            not_supported=("B", "C", "D"),
            evidence_sufficient=False,
            abstention_reason="Global insufficiency despite decisive per-option verdicts.",
            unresolved_risk=True,
        )
        with self.assertRaisesRegex(
            AiQualityAuditValidationError,
            "must be false when no option_judgments entry has "
            "verdict=INSUFFICIENT_EVIDENCE",
        ):
            validate_pass_b_correctness_result(
                payload,
                allowed_option_labels=_OPTIONS,
                frozen_evidence_chunk_ids=_FROZEN,
            )


class TestDeriveCorrectnessFindingUnresolvedAnswerSetRisk(unittest.TestCase):
    """V60-DERIVE-12: Rule 1 abstain when unresolved options could change answer set."""

    def _validated(self, payload: dict) -> dict:
        return validate_pass_b_correctness_result(
            payload,
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=_FROZEN,
        )

    def _qbv1_037_payload(self) -> dict:
        return {
            "option_judgments": [
                _option_judgment(
                    "A", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="FULLY_RESPONSIVE",
                ),
                _option_judgment("B", "INSUFFICIENT_EVIDENCE"),
                _option_judgment("C", "INSUFFICIENT_EVIDENCE"),
                _option_judgment(
                    "D", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
            ],
            "evidence_sufficient_for_decision": False,
            "abstention_reason": (
                "The evidence does not establish why option A is uniquely correct "
                "over option B or whether both are equally appropriate."
            ),
            "unresolved_options_could_change_answer_set": True,
        }

    def test_supported_equals_stored_with_risk_abstains(self):
        finding = derive_correctness_finding(
            correctness_result=self._validated(self._qbv1_037_payload()),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "OTHER_REVIEW_NEEDED")
        self.assertEqual(finding["materiality"], "blocking")
        self.assertIs(finding["metadata"]["correctness_detector_abstained"], True)
        self.assertIs(finding["metadata"]["derived_correctness_finding"], True)

    def test_harmless_unresolved_distractor_returns_none(self):
        payload = _correctness_payload(
            supported=("A",),
            not_supported=("B", "C"),
            insufficient=("D",),
            evidence_sufficient=False,
            abstention_reason="Option D could not be fully judged from evidence.",
            unresolved_risk=False,
        )
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNone(finding)

    def test_supported_equals_stored_no_insufficient_returns_none(self):
        payload = _correctness_payload(
            supported=("A",),
            not_supported=("B", "C", "D"),
        )
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNone(finding)

    def test_historical_missing_field_preserves_none_behavior(self):
        payload = self._qbv1_037_payload()
        del payload["unresolved_options_could_change_answer_set"]
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertIsNone(finding)

    def test_qbv1_037_equivalent_shape_abstains(self):
        finding = derive_correctness_finding(
            correctness_result=self._validated(self._qbv1_037_payload()),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "OTHER_REVIEW_NEEDED")
        self.assertIn("uniquely correct", finding["description"].lower())


class TestDeriveCorrectnessFindingAnswerCompleteness(unittest.TestCase):
    """V60-DERIVE-06: Rule 2b meta-option completeness via answer_completeness."""

    def _validated(self, payload: dict) -> dict:
        return validate_pass_b_correctness_result(
            payload,
            allowed_option_labels=_OPTIONS,
            frozen_evidence_chunk_ids=_FROZEN,
        )

    def _qbv1_026_payload(self) -> dict:
        return {
            "option_judgments": [
                _option_judgment(
                    "A", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="PARTIAL_COMPONENT",
                ),
                _option_judgment(
                    "B", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="PARTIAL_COMPONENT",
                ),
                _option_judgment(
                    "C", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="FULLY_RESPONSIVE",
                ),
                _option_judgment(
                    "D", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
            ],
            "evidence_sufficient_for_decision": True,
            "abstention_reason": None,
        }

    def test_qbv1_026_shape_returns_wrong_answer_key(self):
        finding = derive_correctness_finding(
            correctness_result=self._validated(self._qbv1_026_payload()),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")
        self.assertIs(finding["metadata"]["derived_correctness_finding"], True)
        self.assertIs(finding["metadata"]["answer_completeness_applied"], True)
        self.assertEqual(finding["metadata"]["fully_responsive_labels"], ["C"])

    def test_qbv1_036_genuine_multi_defensible_unchanged(self):
        payload = _correctness_payload(supported=("A", "B"), not_supported=("C", "D"))
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")

    def test_both_either_wording_without_completeness_signal_no_forced_wrong_key(self):
        """Without explicit completeness signals, supported>required still falls
        through to MULTIPLE_DEFENSIBLE_ANSWERS -- no lexical heuristic."""
        payload = _correctness_payload(
            supported=("A", "B", "C"), not_supported=("D",),
        )
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")

    def test_insufficient_evidence_rule_2a_still_takes_precedence(self):
        """qbv1-037 control: unresolved option -> OTHER_REVIEW_NEEDED even when
        a supported label is marked FULLY_RESPONSIVE."""
        payload = {
            "option_judgments": [
                _option_judgment(
                    "A", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="FULLY_RESPONSIVE",
                ),
                _option_judgment(
                    "B", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="PARTIAL_COMPONENT",
                ),
                _option_judgment("C", "INSUFFICIENT_EVIDENCE"),
                _option_judgment(
                    "D", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
            ],
            "evidence_sufficient_for_decision": False,
            "abstention_reason": "Option C could not be judged from evidence.",
        }
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "OTHER_REVIEW_NEEDED")

    def test_more_fully_responsive_than_required_falls_through_safely(self):
        payload = {
            "option_judgments": [
                _option_judgment(
                    "A", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="FULLY_RESPONSIVE",
                ),
                _option_judgment(
                    "B", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="FULLY_RESPONSIVE",
                ),
                _option_judgment(
                    "C", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
                _option_judgment(
                    "D", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
            ],
            "evidence_sufficient_for_decision": True,
            "abstention_reason": None,
        }
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")

    def test_stored_overlaps_fully_responsive_set_does_not_trigger_conversion(self):
        payload = {
            "option_judgments": [
                _option_judgment(
                    "A", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="FULLY_RESPONSIVE",
                ),
                _option_judgment(
                    "B", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="PARTIAL_COMPONENT",
                ),
                _option_judgment(
                    "C", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
                _option_judgment(
                    "D", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
            ],
            "evidence_sufficient_for_decision": True,
            "abstention_reason": None,
        }
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")

    def test_qbv1_016_rule_3_control_unchanged(self):
        payload = _correctness_payload(
            supported=("C",), not_supported=("A", "B", "D"),
        )
        for item in payload["option_judgments"]:
            if item["option_label"] == "C":
                item["answer_completeness"] = "FULLY_RESPONSIVE"
            else:
                item["answer_completeness"] = "NOT_APPLICABLE"
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")
        self.assertNotIn("answer_completeness_applied", finding["metadata"])

    def test_qbv1_008_rule_3_control_unchanged(self):
        payload = _correctness_payload(
            supported=("C",), not_supported=("A", "B", "D"),
        )
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "WRONG_ANSWER_KEY")
        self.assertNotIn("answer_completeness_applied", finding["metadata"])

    def test_stored_supported_not_applicable_does_not_convert(self):
        """Counterexample 1: stored SUPPORTED+NOT_APPLICABLE must not convert."""
        payload = {
            "option_judgments": [
                _option_judgment(
                    "A", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
                _option_judgment(
                    "B", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="FULLY_RESPONSIVE",
                ),
                _option_judgment(
                    "C", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
                _option_judgment(
                    "D", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
            ],
            "evidence_sufficient_for_decision": True,
            "abstention_reason": None,
        }
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")
        self.assertNotIn("answer_completeness_applied", finding["metadata"])

    def test_extra_supported_not_applicable_prevents_conversion(self):
        """Counterexample 2: unclassified supported option blocks conversion."""
        payload = {
            "option_judgments": [
                _option_judgment(
                    "A", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="PARTIAL_COMPONENT",
                ),
                _option_judgment(
                    "B", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="FULLY_RESPONSIVE",
                ),
                _option_judgment(
                    "C", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
                _option_judgment(
                    "D", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
            ],
            "evidence_sufficient_for_decision": True,
            "abstention_reason": None,
        }
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")
        self.assertNotIn("answer_completeness_applied", finding["metadata"])

    def test_stored_must_be_subset_of_explicit_partial_components(self):
        """Stored answer not explicitly PARTIAL_COMPONENT must not convert."""
        payload = {
            "option_judgments": [
                _option_judgment(
                    "A", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
                _option_judgment(
                    "B", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="FULLY_RESPONSIVE",
                ),
                _option_judgment(
                    "C", "SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="PARTIAL_COMPONENT",
                ),
                _option_judgment(
                    "D", "NOT_SUPPORTED_AS_CORRECT",
                    chunk_ids=[_CHUNK_1], answer_completeness="NOT_APPLICABLE",
                ),
            ],
            "evidence_sufficient_for_decision": True,
            "abstention_reason": None,
        }
        finding = derive_correctness_finding(
            correctness_result=self._validated(payload),
            stored_correct_option_labels=["A"],
            required_selection_count=1,
        )
        self.assertEqual(finding["finding_code"], "MULTIPLE_DEFENSIBLE_ANSWERS")
        self.assertNotIn("answer_completeness_applied", finding["metadata"])


class TestMergePassBFindings(unittest.TestCase):
    """V60-IMPL-01: deterministic Pass B specialist/general merge boundary."""

    def test_specialist_correctness_finding_is_authoritative(self):
        correctness_finding = {
            "finding_ref": "FC1",
            "finding_code": "WRONG_ANSWER_KEY",
            "finding_type": "correctness",
            "severity": "high",
            "materiality": "blocking",
            "title": "Wrong key",
            "description": "Specialist disagrees with stored key.",
            "evidence_chunk_ids": [_CHUNK_1],
            "metadata": {},
        }
        result = merge_pass_b_findings(
            correctness_finding=correctness_finding,
            general_proposed_findings=[],
            frozen_evidence_chunk_ids=_FROZEN,
        )
        codes = [item["finding_code"] for item in result["proposed_findings"]]
        self.assertEqual(codes, ["WRONG_ANSWER_KEY"])

    def test_general_non_correctness_findings_remain(self):
        general_finding = _finding(
            finding_ref="F1",
            finding_code="WEAK_DISTRACTORS",
            finding_type="answer_quality",
            severity="low",
            materiality="warning",
        )
        result = merge_pass_b_findings(
            correctness_finding=None,
            general_proposed_findings=[general_finding],
            frozen_evidence_chunk_ids=_FROZEN,
        )
        codes = [item["finding_code"] for item in result["proposed_findings"]]
        self.assertEqual(codes, ["WEAK_DISTRACTORS"])
        self.assertEqual(result["dropped_general_findings"], [])

    def test_general_correctness_drift_cannot_override_specialist(self):
        correctness_finding = {
            "finding_ref": "FC1",
            "finding_code": "WRONG_ANSWER_KEY",
            "finding_type": "correctness",
            "severity": "high",
            "materiality": "blocking",
            "title": "Wrong key",
            "description": "Specialist disagrees with stored key.",
            "evidence_chunk_ids": [_CHUNK_1],
            "metadata": {},
        }
        drifting_general_finding = _finding(
            finding_ref="F1",
            finding_code="UNSUPPORTED_ANSWER",
            finding_type="correctness",
            materiality="blocking",
        )
        result = merge_pass_b_findings(
            correctness_finding=correctness_finding,
            general_proposed_findings=[drifting_general_finding],
            frozen_evidence_chunk_ids=_FROZEN,
        )
        codes = [item["finding_code"] for item in result["proposed_findings"]]
        self.assertEqual(codes, ["WRONG_ANSWER_KEY"])
        self.assertEqual(len(result["dropped_general_findings"]), 1)
        self.assertEqual(
            result["dropped_general_findings"][0]["finding_code"], "UNSUPPORTED_ANSWER"
        )

    def test_finding_refs_remain_unique(self):
        correctness_finding = {
            "finding_ref": "F1",
            "finding_code": "WRONG_ANSWER_KEY",
            "finding_type": "correctness",
            "severity": "high",
            "materiality": "blocking",
            "title": "Wrong key",
            "description": "Specialist disagrees with stored key.",
            "evidence_chunk_ids": [_CHUNK_1],
            "metadata": {},
        }
        colliding_general_finding = _finding(finding_ref="F1", finding_code="WEAK_DISTRACTORS")
        with self.assertRaisesRegex(AiQualityAuditValidationError, "duplicate finding_ref"):
            merge_pass_b_findings(
                correctness_finding=correctness_finding,
                general_proposed_findings=[colliding_general_finding],
                frozen_evidence_chunk_ids=_FROZEN,
            )

    def test_canonical_materiality_remains_blocking_for_all_three_correctness_codes(self):
        for code in ("WRONG_ANSWER_KEY", "MULTIPLE_DEFENSIBLE_ANSWERS", "UNSUPPORTED_ANSWER"):
            with self.subTest(code=code):
                correctness_finding = {
                    "finding_ref": "FC1",
                    "finding_code": code,
                    "finding_type": "correctness",
                    "severity": "high",
                    # Deliberately wrong materiality; canonical policy must
                    # override this exactly as it does for any other finding.
                    "materiality": "warning",
                    "title": "Correctness defect",
                    "description": "Specialist-derived correctness defect.",
                    "evidence_chunk_ids": [_CHUNK_1],
                    "metadata": {},
                }
                result = merge_pass_b_findings(
                    correctness_finding=correctness_finding,
                    general_proposed_findings=[],
                    frozen_evidence_chunk_ids=_FROZEN,
                )
                self.assertEqual(result["proposed_findings"][0]["materiality"], "blocking")


class TestDeriveExplanationFinding(unittest.TestCase):
    """V60-EXPL-03: deterministic explanation-presence detection."""

    def test_empty_explanation_produces_explanation_missing(self):
        finding = derive_explanation_finding(explanation="")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "EXPLANATION_MISSING")
        self.assertEqual(finding["finding_type"], "explanation_quality")

    def test_whitespace_only_explanation_produces_explanation_missing(self):
        finding = derive_explanation_finding(explanation="   \n\t  ")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "EXPLANATION_MISSING")

    def test_none_explanation_produces_explanation_missing(self):
        finding = derive_explanation_finding(explanation=None)
        self.assertIsNotNone(finding)
        self.assertEqual(finding["finding_code"], "EXPLANATION_MISSING")

    def test_non_empty_explanation_produces_no_finding(self):
        finding = derive_explanation_finding(explanation="Profiles control object permissions.")
        self.assertIsNone(finding)

    def test_canonical_materiality_is_blocking_after_validation(self):
        finding = derive_explanation_finding(explanation="")
        result = merge_pass_b_findings(
            correctness_finding=None,
            general_proposed_findings=[],
            frozen_evidence_chunk_ids=_FROZEN,
            explanation_finding=finding,
        )
        self.assertEqual(len(result["proposed_findings"]), 1)
        self.assertEqual(result["proposed_findings"][0]["materiality"], "blocking")

    def test_stable_finding_ref_and_provenance_metadata(self):
        finding = derive_explanation_finding(explanation="")
        self.assertEqual(finding["finding_ref"], "FE1")
        self.assertEqual(
            finding["metadata"],
            {
                "deterministic_explanation_check": True,
                "deterministic_detector": "explanation_presence",
                "deterministic_detector_version": "1.0.0",
            },
        )

    def test_custom_finding_ref_is_honored(self):
        finding = derive_explanation_finding(explanation="", finding_ref="FE9")
        self.assertEqual(finding["finding_ref"], "FE9")

    def test_no_evidence_chunk_required(self):
        finding = derive_explanation_finding(explanation="")
        self.assertEqual(finding["evidence_chunk_ids"], [])


class TestMergePassBFindingsExplanationDedup(unittest.TestCase):
    """V60-EXPL-03: deterministic explanation finding merge/dedup behavior."""

    def test_deterministic_explanation_finding_is_authoritative(self):
        explanation_finding = derive_explanation_finding(explanation="")
        result = merge_pass_b_findings(
            correctness_finding=None,
            general_proposed_findings=[],
            frozen_evidence_chunk_ids=_FROZEN,
            explanation_finding=explanation_finding,
        )
        codes = [item["finding_code"] for item in result["proposed_findings"]]
        self.assertEqual(codes, ["EXPLANATION_MISSING"])

    def test_general_judge_duplicate_is_dropped(self):
        explanation_finding = derive_explanation_finding(explanation="")
        general_duplicate = _finding(
            finding_ref="F1",
            finding_code="EXPLANATION_MISSING",
            finding_type="explanation_quality",
            severity="medium",
            materiality="warning",
        )
        result = merge_pass_b_findings(
            correctness_finding=None,
            general_proposed_findings=[general_duplicate],
            frozen_evidence_chunk_ids=_FROZEN,
            explanation_finding=explanation_finding,
        )
        codes = [item["finding_code"] for item in result["proposed_findings"]]
        self.assertEqual(codes, ["EXPLANATION_MISSING"])
        self.assertEqual(len(result["proposed_findings"]), 1)
        self.assertEqual(len(result["dropped_general_findings"]), 1)
        self.assertEqual(
            result["dropped_general_findings"][0]["finding_code"], "EXPLANATION_MISSING"
        )
        self.assertEqual(
            result["dropped_general_findings"][0]["reason"],
            "general_judge_emitted_deterministic_owned_code",
        )

    def test_unrelated_explanation_finding_is_not_suppressed(self):
        explanation_finding = derive_explanation_finding(explanation="")
        unrelated = _finding(
            finding_ref="F1",
            finding_code="EXPLANATION_INCOMPLETE",
            finding_type="explanation_quality",
            severity="low",
            materiality="warning",
        )
        result = merge_pass_b_findings(
            correctness_finding=None,
            general_proposed_findings=[unrelated],
            frozen_evidence_chunk_ids=_FROZEN,
            explanation_finding=explanation_finding,
        )
        codes = sorted(item["finding_code"] for item in result["proposed_findings"])
        self.assertEqual(codes, ["EXPLANATION_INCOMPLETE", "EXPLANATION_MISSING"])
        self.assertEqual(result["dropped_general_findings"], [])

    def test_coexists_with_correctness_finding(self):
        correctness_finding = {
            "finding_ref": "FC1",
            "finding_code": "WRONG_ANSWER_KEY",
            "finding_type": "correctness",
            "severity": "high",
            "materiality": "blocking",
            "title": "Wrong key",
            "description": "Specialist disagrees with stored key.",
            "evidence_chunk_ids": [_CHUNK_1],
            "metadata": {},
        }
        explanation_finding = derive_explanation_finding(explanation="")
        result = merge_pass_b_findings(
            correctness_finding=correctness_finding,
            general_proposed_findings=[],
            frozen_evidence_chunk_ids=_FROZEN,
            explanation_finding=explanation_finding,
        )
        codes = sorted(item["finding_code"] for item in result["proposed_findings"])
        self.assertEqual(codes, ["EXPLANATION_MISSING", "WRONG_ANSWER_KEY"])

    def test_no_explanation_finding_leaves_general_findings_unchanged(self):
        general_finding = _finding(
            finding_ref="F1",
            finding_code="WEAK_DISTRACTORS",
            finding_type="answer_quality",
            severity="low",
            materiality="warning",
        )
        result = merge_pass_b_findings(
            correctness_finding=None,
            general_proposed_findings=[general_finding],
            frozen_evidence_chunk_ids=_FROZEN,
            explanation_finding=None,
        )
        codes = [item["finding_code"] for item in result["proposed_findings"]]
        self.assertEqual(codes, ["WEAK_DISTRACTORS"])
        self.assertEqual(result["dropped_general_findings"], [])

    def test_existing_correctness_merge_behavior_remains_unchanged(self):
        correctness_finding = {
            "finding_ref": "FC1",
            "finding_code": "WRONG_ANSWER_KEY",
            "finding_type": "correctness",
            "severity": "high",
            "materiality": "blocking",
            "title": "Wrong key",
            "description": "Specialist disagrees with stored key.",
            "evidence_chunk_ids": [_CHUNK_1],
            "metadata": {},
        }
        drifting_general_finding = _finding(
            finding_ref="F1",
            finding_code="UNSUPPORTED_ANSWER",
            finding_type="correctness",
            materiality="blocking",
        )
        result = merge_pass_b_findings(
            correctness_finding=correctness_finding,
            general_proposed_findings=[drifting_general_finding],
            frozen_evidence_chunk_ids=_FROZEN,
            explanation_finding=None,
        )
        codes = [item["finding_code"] for item in result["proposed_findings"]]
        self.assertEqual(codes, ["WRONG_ANSWER_KEY"])
        self.assertEqual(len(result["dropped_general_findings"]), 1)
        self.assertEqual(
            result["dropped_general_findings"][0]["finding_code"], "UNSUPPORTED_ANSWER"
        )


if __name__ == "__main__":
    unittest.main()
