"""
Tests for V45 Phase 3 audit-finding materiality persistence.

Covers migration artifacts, RPC contract expectations, and orchestration
payloads sent to complete_audit_run_v1 (mocked — no live DB or AI calls).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.audit_orchestration import orchestrate_audit
from workers.deterministic_audit import DETECTOR_NAME, DETECTOR_VERSION, run_deterministic_checks
from workers.finding_merge import merge_findings
from workers.finding_policy import normalize_llm_finding
from workers.llm_audit import validate_llm_response

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT / "supabase" / "migrations" / "20260624120000_v45_audit_finding_materiality.sql"
)
VERIFICATION_PATH = (
    REPO_ROOT / "supabase" / "tests" / "v45_audit_finding_materiality_verification.sql"
)

_AUDIT_RUN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_QUESTION_VSID = "bbbbbbbb-0000-0000-0000-000000000001"

_CREATE_RESPONSE = [{"audit_run_id": _AUDIT_RUN_ID, "run_status": "pending"}]
_COMPLETE_RESPONSE = [
    {"audit_run_id": _AUDIT_RUN_ID, "run_status": "completed",
     "finding_count": 1, "evidence_count": 0},
]


class _FakeRpcResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


class _FakeRpcBuilder:
    def __init__(self, data, error=None):
        self._data = data
        self._error = error

    def execute(self):
        return _FakeRpcResult(self._data, self._error)


class FakeSupabase:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._responses: dict[str, list] = {}
        self._errors: dict[str, str] = {}

    def set_response(self, name: str, data: list):
        self._responses[name] = data

    def set_error(self, name: str, message: str):
        self._errors[name] = message

    def rpc(self, name, params):
        self.calls.append((name, params))
        if name in self._errors:
            return _FakeRpcBuilder(data=[], error=self._errors[name])
        return _FakeRpcBuilder(data=self._responses.get(name, []))

    def calls_for(self, name: str) -> list[dict]:
        return [params for rpc_name, params in self.calls if rpc_name == name]


def _rpc_resolve_materiality(raw) -> str:
    """Mirror complete_audit_run_v1 materiality handling for contract tests."""
    if raw is None or str(raw).strip() == "":
        return "warning"
    value = str(raw).strip()
    if value not in {"blocking", "warning", "informational"}:
        raise ValueError(f"invalid materiality: {value!r}")
    return value


def _base_orchestration_kwargs(check_fn):
    return dict(
        audit_type="deterministic",
        target_question_version_id=_QUESTION_VSID,
        target_candidate_id=None,
        created_by="test@certbound.io",
        ruleset_version="1.0.0",
        resource_snapshot=None,
        metadata=None,
        check_fn=check_fn,
    )


class TestMaterialityMigrationArtifacts(unittest.TestCase):

    def test_migration_adds_column_constraint_and_index(self):
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS materiality text NOT NULL DEFAULT 'warning'", sql)
        self.assertIn("audit_findings_materiality_valid", sql)
        self.assertIn("materiality IN ('blocking', 'warning', 'informational')", sql)
        self.assertIn("idx_af_run_materiality_status", sql)
        self.assertIn("(audit_run_id, materiality, finding_status)", sql)

    def test_migration_backfill_is_conservative(self):
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("SET    materiality = 'blocking'", sql)
        self.assertIn("'MISSING_EXPLANATION'", sql)
        self.assertIn("'EXPLANATION_MISSING'", sql)
        self.assertIn("DEFAULT 'warning'", sql)

    def test_migration_updates_complete_audit_run_v1(self):
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("has invalid materiality", sql)
        self.assertIn("COALESCE(NULLIF(TRIM(v_finding ->> 'materiality'), ''), 'warning')", sql)
        self.assertIn("materiality,", sql)

    def test_migration_privilege_hardening_for_complete_audit_run_v1(self):
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("REVOKE ALL ON FUNCTION public.complete_audit_run_v1(", sql)
        self.assertIn(") FROM PUBLIC;", sql)
        self.assertIn(") FROM anon;", sql)
        self.assertIn(") FROM authenticated;", sql)
        self.assertIn(") TO service_role;", sql)

    def test_verification_script_covers_schema_and_rpc(self):
        sql = VERIFICATION_PATH.read_text(encoding="utf-8")
        self.assertIn("audit_findings.materiality column must exist", sql)
        self.assertIn("audit_findings_materiality_valid", sql)
        self.assertIn("idx_af_run_materiality_status", sql)
        self.assertIn("complete_audit_run_v1 must reject invalid materiality", sql)
        self.assertIn("has_function_privilege(", sql)
        self.assertIn("'public.complete_audit_run_v1(uuid,jsonb,jsonb)'", sql)
        self.assertIn("'public',", sql)
        self.assertIn("service_role must have EXECUTE on complete_audit_run_v1", sql)
        self.assertIn("anon must not have EXECUTE on complete_audit_run_v1", sql)
        self.assertIn("authenticated must not have EXECUTE on complete_audit_run_v1", sql)
        self.assertIn("PUBLIC must not have EXECUTE on complete_audit_run_v1", sql)


class TestRpcMaterialityContract(unittest.TestCase):

    def test_blocking_warning_informational_allowed(self):
        self.assertEqual(_rpc_resolve_materiality("blocking"), "blocking")
        self.assertEqual(_rpc_resolve_materiality("warning"), "warning")
        self.assertEqual(_rpc_resolve_materiality("informational"), "informational")

    def test_missing_defaults_to_warning(self):
        self.assertEqual(_rpc_resolve_materiality(None), "warning")
        self.assertEqual(_rpc_resolve_materiality(""), "warning")
        self.assertEqual(_rpc_resolve_materiality("   "), "warning")

    def test_invalid_materiality_rejected(self):
        with self.assertRaises(ValueError):
            _rpc_resolve_materiality("critical")
        with self.assertRaises(ValueError):
            _rpc_resolve_materiality("BLOCKING")


class TestOrchestrationMaterialityPayload(unittest.TestCase):

    def _run_with_findings(self, findings: list[dict]) -> list[dict]:
        fake = FakeSupabase()
        fake.set_response("create_audit_run_v1", _CREATE_RESPONSE)
        fake.set_response("complete_audit_run_v1", _COMPLETE_RESPONSE)
        orchestrate_audit(fake, **_base_orchestration_kwargs(lambda: findings))
        return fake.calls_for("complete_audit_run_v1")[0]["p_findings"]

    def _finding(self, *, materiality: str, code: str = "WEAK_DISTRACTORS") -> dict:
        return {
            "finding_code": code,
            "finding_type": "answer_quality",
            "severity": "medium",
            "materiality": materiality,
            "title": "Test finding",
            "description": "Payload materiality test.",
            "evidence": [],
            "metadata": {},
        }

    def test_blocking_materiality_reaches_p_findings(self):
        sent = self._run_with_findings([self._finding(materiality="blocking", code="WRONG_ANSWER_KEY")])
        self.assertEqual(sent[0]["materiality"], "blocking")

    def test_warning_materiality_reaches_p_findings(self):
        sent = self._run_with_findings([self._finding(materiality="warning")])
        self.assertEqual(sent[0]["materiality"], "warning")

    def test_informational_materiality_reaches_p_findings(self):
        sent = self._run_with_findings([self._finding(materiality="informational", code="OTHER_REVIEW_NEEDED")])
        self.assertEqual(sent[0]["materiality"], "informational")

    def test_policy_assigns_materiality_when_missing_on_deterministic_path(self):
        question = {
            **{
                "question_text": "What is Salesforce?",
                "explanation": "",
                "question_type": "single",
                "select_count": 1,
                "options": [
                    {"option_label": "A", "option_text": "CRM", "is_correct": True, "display_order": 1},
                    {"option_label": "B", "option_text": "ERP", "is_correct": False, "display_order": 2},
                    {"option_label": "C", "option_text": "CMS", "is_correct": False, "display_order": 3},
                    {"option_label": "D", "option_text": "BI", "is_correct": False, "display_order": 4},
                ],
            },
        }
        findings = run_deterministic_checks(question, "1.0.0")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["finding_code"], "EXPLANATION_MISSING")
        self.assertEqual(findings[0]["materiality"], "blocking")
        self.assertNotIn("materiality", findings[0].get("metadata", {}))

    def test_merge_escalation_persists_through_orchestration_payload(self):
        det = {
            "finding_code": "EXPLANATION_MISSING",
            "finding_type": "explanation_quality",
            "severity": "medium",
            "materiality": "warning",
            "title": "Thin explanation",
            "description": "Explanation is incomplete.",
            "field_path": "explanation",
            "evidence": [],
            "metadata": {"ruleset_version": "1.0.0"},
            "detector_name": DETECTOR_NAME,
            "detector_version": DETECTOR_VERSION,
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
        sent = self._run_with_findings(merged)
        self.assertEqual(sent[0]["materiality"], "blocking")
        self.assertEqual(sent[0]["finding_code"], "EXPLANATION_MISSING")

    def test_canonical_code_and_original_llm_code_preserved_in_payload(self):
        finding = normalize_llm_finding({
            "finding_code": "EXP_001",
            "finding_type": "correctness",
            "severity": "high",
            "title": "Incorrect answer key",
            "description": "The marked correct option contradicts the evidence.",
            "evidence": [],
        })
        sent = self._run_with_findings([finding])
        self.assertEqual(sent[0]["finding_code"], "WRONG_ANSWER_KEY")
        self.assertEqual(sent[0]["metadata"]["original_finding_code"], "EXP_001")
        self.assertEqual(sent[0]["materiality"], "blocking")

    def test_llm_validation_path_includes_materiality(self):
        findings = validate_llm_response({"findings": [{
            "finding_code": "STYLE-001",
            "finding_type": "other",
            "severity": "low",
            "title": "Stylistic suggestion",
            "description": "Consider making this question more scenario-based.",
            "evidence": [],
        }]})
        self.assertEqual(findings[0]["materiality"], "informational")


if __name__ == "__main__":
    unittest.main()
