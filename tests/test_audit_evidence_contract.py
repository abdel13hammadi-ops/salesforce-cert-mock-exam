"""Tests for V45 Phase 4C audit evidence contract."""

from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.audit_evidence_contract import (
    EVIDENCE_CONTRACT_VERSION,
    AuditEvidenceContext,
    EvidenceContractError,
    attach_evidence_contracts,
    build_deterministic_evidence,
    build_hybrid_evidence,
    build_llm_evidence,
    evidence_fingerprint,
    normalize_legacy_evidence_contract,
    validate_evidence_contract,
)
from workers.audit_orchestration import orchestrate_audit
from workers.deterministic_audit import run_deterministic_checks
from workers.finding_merge import merge_findings
from workers.finding_policy import normalize_deterministic_finding, normalize_llm_finding

_QUESTION_VSID = "bbbbbbbb-0000-0000-0000-000000000001"
_QUESTION_ID = "cccccccc-0000-0000-0000-000000000001"
_CANDIDATE_ID = "dddddddd-0000-0000-0000-000000000001"
_CHUNK_ID = "eeeeeeee-0000-0000-0000-000000000001"

_BASE_CONTEXT = AuditEvidenceContext.from_orchestration(
    audit_type="deterministic",
    target_question_version_id=_QUESTION_VSID,
    target_candidate_id=None,
    ruleset_version="1.0.0",
    question_snapshot={
        "question_id": _QUESTION_ID,
        "version_number": 3,
        "certification_code": "ADM-201",
    },
    generated_at="2026-06-24T12:00:00+00:00",
)


def _deterministic_finding(**overrides) -> dict:
    base = normalize_deterministic_finding({
        "finding_code": "EMPTY_QUESTION_TEXT",
        "finding_type": "formatting",
        "severity": "critical",
        "title": "Question text is empty",
        "description": "The question_text field must not be empty.",
        "field_path": "question.question_text",
        "detector_name": "certbound-deterministic-audit",
        "detector_version": "1.0.0",
        "metadata": {"ruleset_version": "1.0.0"},
        "evidence": [],
    })
    base.update(overrides)
    return base


def _llm_finding(**overrides) -> dict:
    base = normalize_llm_finding({
        "finding_code": "AMB-001",
        "finding_type": "ambiguity",
        "severity": "medium",
        "title": "Ambiguous wording",
        "description": "Multiple approaches appear defensible.",
        "field_path": "question.question_text",
        "confidence": 0.82,
        "detector_name": "certbound-llm-audit",
        "detector_version": "1.0.0",
        "metadata": {},
        "evidence": [{
            "resource_chunk_id": _CHUNK_ID,
            "evidence_role": "supporting",
            "quote_text": "Use one clear business requirement.",
            "relevance_score": 0.9,
        }],
    })
    base.update(overrides)
    return base


class TestEvidenceContractValidation(unittest.TestCase):
    def test_valid_deterministic_evidence_passes(self):
        contract = build_deterministic_evidence(_deterministic_finding(), _BASE_CONTEXT)
        validate_evidence_contract(contract, strict_identity=True)
        self.assertEqual(contract["audit_source"], "deterministic")
        self.assertEqual(contract["question_version_id"], _QUESTION_VSID)
        self.assertEqual(contract["question_id"], _QUESTION_ID)
        self.assertEqual(contract["contract_version"], EVIDENCE_CONTRACT_VERSION)

    def test_valid_llm_evidence_passes(self):
        ctx = AuditEvidenceContext.from_orchestration(
            audit_type="llm",
            target_question_version_id=_QUESTION_VSID,
            target_candidate_id=None,
            ruleset_version="prompt-v2",
            question_snapshot={"question_id": _QUESTION_ID, "version_number": 3},
            model_name="claude-sonnet-4-20250514",
            prompt_version="prompt-v2",
            generated_at="2026-06-24T12:00:00+00:00",
        )
        contract = build_llm_evidence(_llm_finding(), ctx)
        validate_evidence_contract(contract, strict_identity=True)
        self.assertEqual(contract["audit_source"], "llm")
        self.assertEqual(len(contract["supporting_references"]), 1)
        self.assertEqual(contract["model_metadata"]["model_name"], "claude-sonnet-4-20250514")

    def test_valid_hybrid_evidence_passes(self):
        ctx = AuditEvidenceContext.from_orchestration(
            audit_type="hybrid",
            target_question_version_id=_QUESTION_VSID,
            target_candidate_id=None,
            ruleset_version="1.0.0",
            question_snapshot={"question_id": _QUESTION_ID, "version_number": 3},
            model_name="claude-sonnet-4-20250514",
            prompt_version="prompt-v2",
            generated_at="2026-06-24T12:00:00+00:00",
        )
        merged = merge_findings(
            [_deterministic_finding(finding_code="EXPLANATION_MISSING", finding_type="explanation_quality")],
            [_llm_finding(finding_code="EXPLANATION_MISSING", finding_type="explanation_quality")],
        )[0]
        contract = build_hybrid_evidence(merged, ctx)
        validate_evidence_contract(contract, strict_identity=True)
        self.assertEqual(contract["audit_source"], "hybrid")

    def test_malformed_source_fails(self):
        contract = build_deterministic_evidence(_deterministic_finding(), _BASE_CONTEXT)
        contract["audit_source"] = "magic"
        with self.assertRaises(EvidenceContractError):
            validate_evidence_contract(contract)

    def test_malformed_severity_fails(self):
        contract = build_deterministic_evidence(_deterministic_finding(), _BASE_CONTEXT)
        contract["severity"] = "urgent"
        with self.assertRaises(EvidenceContractError):
            validate_evidence_contract(contract)

    def test_malformed_confidence_fails(self):
        with self.assertRaises(EvidenceContractError):
            build_llm_evidence(
                _llm_finding(confidence=True),
                AuditEvidenceContext.from_orchestration(
                    audit_type="llm",
                    target_question_version_id=_QUESTION_VSID,
                    target_candidate_id=None,
                    generated_at="2026-06-24T12:00:00+00:00",
                ),
            )

    def test_missing_question_version_identity_fails_for_new_evidence(self):
        ctx = AuditEvidenceContext.from_orchestration(
            audit_type="deterministic",
            target_question_version_id=None,
            target_candidate_id=None,
            generated_at="2026-06-24T12:00:00+00:00",
        )
        with self.assertRaises(EvidenceContractError):
            build_deterministic_evidence(_deterministic_finding(), ctx)

    def test_candidate_target_is_acceptable_without_question_version(self):
        ctx = AuditEvidenceContext.from_orchestration(
            audit_type="llm",
            target_question_version_id=None,
            target_candidate_id=_CANDIDATE_ID,
            generated_at="2026-06-24T12:00:00+00:00",
        )
        contract = build_llm_evidence(_llm_finding(), ctx)
        validate_evidence_contract(contract, strict_identity=True)
        self.assertEqual(contract["target_candidate_id"], _CANDIDATE_ID)


class TestLegacyAndFingerprint(unittest.TestCase):
    def test_legacy_payload_normalizes_safely(self):
        legacy = {
            "finding_code": "EMPTY_QUESTION_TEXT",
            "finding_type": "formatting",
            "severity": "critical",
            "materiality": "blocking",
            "title": "Question text is empty",
            "description": "The question_text field must not be empty.",
            "field_path": "question.question_text",
            "detector_name": "certbound-deterministic-audit",
            "detector_version": "1.0.0",
            "metadata": {"ruleset_version": "1.0.0"},
            "evidence": [],
        }
        contract = normalize_legacy_evidence_contract(legacy, context=_BASE_CONTEXT)
        validate_evidence_contract(contract, strict_identity=False)
        self.assertTrue(contract["legacy"])
        self.assertEqual(contract["question_version_id"], _QUESTION_VSID)

    def test_serialization_is_deterministic(self):
        contract = build_deterministic_evidence(_deterministic_finding(), _BASE_CONTEXT)
        reserialized = copy.deepcopy(contract)
        reserialized["fingerprint"] = evidence_fingerprint(reserialized)
        contract["fingerprint"] = evidence_fingerprint(contract)
        self.assertEqual(contract, reserialized)

    def test_identical_evidence_same_fingerprint(self):
        a = build_deterministic_evidence(_deterministic_finding(), _BASE_CONTEXT)
        b = build_deterministic_evidence(_deterministic_finding(), _BASE_CONTEXT)
        self.assertEqual(evidence_fingerprint(a), evidence_fingerprint(b))

    def test_materially_different_evidence_different_fingerprint(self):
        a = build_deterministic_evidence(_deterministic_finding(), _BASE_CONTEXT)
        b = build_deterministic_evidence(
            _deterministic_finding(description="Different rationale."),
            _BASE_CONTEXT,
        )
        self.assertNotEqual(evidence_fingerprint(a), evidence_fingerprint(b))


class TestIntegration(unittest.TestCase):
    def test_merge_deduplication_still_works(self):
        det = _deterministic_finding(
            finding_code="EXPLANATION_MISSING",
            finding_type="explanation_quality",
            title="Missing explanation",
            description="Explanation is empty.",
            field_path="question.explanation",
        )
        llm = _llm_finding(
            finding_code="EXPLANATION_MISSING",
            finding_type="explanation_quality",
            title="Missing explanation",
            description="Explanation is empty.",
            field_path="explanation",
        )
        merged = merge_findings([det], [llm])
        self.assertEqual(len(merged), 1)

    def test_attach_evidence_contracts_preserves_merge_behavior(self):
        det = _deterministic_finding(
            finding_code="EXPLANATION_MISSING",
            finding_type="explanation_quality",
            title="Missing explanation",
            description="Explanation is empty.",
            field_path="question.explanation",
        )
        llm = _llm_finding(
            finding_code="EXPLANATION_MISSING",
            finding_type="explanation_quality",
            title="Missing explanation",
            description="Explanation is empty.",
            field_path="explanation",
        )
        merged = merge_findings([det], [llm])
        ctx = AuditEvidenceContext.from_orchestration(
            audit_type="hybrid",
            target_question_version_id=_QUESTION_VSID,
            target_candidate_id=None,
            ruleset_version="1.0.0",
            question_snapshot={"question_id": _QUESTION_ID},
            generated_at="2026-06-24T12:00:00+00:00",
        )
        enriched = attach_evidence_contracts(merged, ctx)
        self.assertEqual(len(enriched), 1)
        self.assertIn("evidence_contract", enriched[0]["metadata"])

    def test_worker_persistence_still_works(self):
        class _FakeRpcResult:
            def __init__(self, data):
                self.data = data
                self.error = None

        class _FakeRpcBuilder:
            def __init__(self, data):
                self._data = data

            def execute(self):
                return _FakeRpcResult(self._data)

        class FakeSupabase:
            def __init__(self):
                self.calls = []

            def rpc(self, name, params):
                self.calls.append((name, params))
                if name == "create_audit_run_v1":
                    return _FakeRpcBuilder([{"audit_run_id": "run-1", "run_status": "pending"}])
                return _FakeRpcBuilder([
                    {"audit_run_id": "run-1", "run_status": "completed",
                     "finding_count": 1, "evidence_count": 0},
                ])

        fake = FakeSupabase()
        question = {"question_text": "", "options": [{"option_text": "A"}, {"option_text": "B"}]}
        orchestrate_audit(
            fake,
            audit_type="deterministic",
            target_question_version_id=_QUESTION_VSID,
            target_candidate_id=None,
            created_by="test@example.com",
            ruleset_version="1.0.0",
            resource_snapshot={},
            metadata={},
            check_fn=lambda: run_deterministic_checks(question, "1.0.0"),
            question_snapshot={"question_id": _QUESTION_ID, "version_number": 3},
        )
        complete_call = [params for name, params in fake.calls if name == "complete_audit_run_v1"][0]
        findings = complete_call["p_findings"]
        self.assertTrue(findings)
        self.assertIn("evidence_contract", findings[0]["metadata"])

    def test_no_service_role_credentials_exposed(self):
        repo_root = Path(__file__).resolve().parents[1]
        contract_source = (repo_root / "workers" / "audit_evidence_contract.py").read_text(encoding="utf-8")
        self.assertNotIn("service_role", contract_source.lower())
        self.assertNotIn("SUPABASE_SERVICE", contract_source)


if __name__ == "__main__":
    unittest.main()
