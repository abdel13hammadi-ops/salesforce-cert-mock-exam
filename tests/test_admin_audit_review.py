"""Tests for V45 Phase 4D admin audit review workflow."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.audit_review import (
    ALLOWED_DECISIONS,
    AuditReviewAccessError,
    AuditReviewError,
    assert_admin_reviewer,
    build_evidence_contract_view,
    escape_review_text,
    get_finding_review_detail,
    list_audit_findings,
    list_audit_runs,
    load_immutable_question_version,
    record_finding_decision,
    validate_decision_value,
    validate_reviewer_note,
    validate_status_transition,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260624230000_v45_audit_finding_review_workflow.sql"
)

_RUN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_FINDING_ID = "bbbbbbbb-0000-0000-0000-000000000001"
_VERSION_ID = "cccccccc-0000-0000-0000-000000000001"
_REVIEWER = "admin@certbound.test"


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

    def set_response(self, name: str, data: list):
        self._responses[name] = data

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _FakeRpcBuilder(self._responses.get(name, []))


def _sample_run(**overrides):
    row = {
        "audit_run_id": _RUN_ID,
        "audit_type": "hybrid",
        "run_status": "completed",
        "certification_code": "ADM-201",
        "target_question_version_id": _VERSION_ID,
        "question_id": 1067,
        "version_number": 3,
        "finding_count": 2,
        "blocking_finding_count": 1,
        "high_severity_count": 1,
        "started_at": "2026-06-24T10:00:00+00:00",
        "completed_at": "2026-06-24T10:05:00+00:00",
        "created_at": "2026-06-24T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def _sample_finding(**overrides):
    row = {
        "finding_id": _FINDING_ID,
        "finding_code": "EXPLANATION_MISSING",
        "finding_type": "explanation_quality",
        "severity": "medium",
        "materiality": "blocking",
        "finding_status": "open",
        "title": "Missing explanation",
        "field_path": "question.explanation",
        "confidence": None,
        "question_id": 1067,
        "question_version_id": _VERSION_ID,
        "question_version_number": 3,
        "audit_source": "hybrid",
        "created_at": "2026-06-24T10:01:00+00:00",
    }
    row.update(overrides)
    return row


def _sample_detail(**overrides):
    row = {
        "finding_id": _FINDING_ID,
        "audit_run_id": _RUN_ID,
        "finding_code": "EXPLANATION_MISSING",
        "finding_type": "explanation_quality",
        "severity": "medium",
        "materiality": "blocking",
        "finding_status": "open",
        "title": "Missing explanation",
        "description": "Explanation is empty.",
        "field_path": "question.explanation",
        "confidence": None,
        "detector_name": "certbound-deterministic-audit",
        "detector_version": "1.0.0",
        "metadata": {
            "ruleset_version": "1.0.0",
            "evidence_contract": {
                "contract_version": "1.0.0",
                "finding_code": "EXPLANATION_MISSING",
                "finding_category": "explanation_quality",
                "severity": "medium",
                "materiality": "blocking",
                "audit_source": "hybrid",
                "summary": "Missing explanation",
                "detailed_rationale": "Explanation is empty.",
                "question_version_id": _VERSION_ID,
                "question_id": "1067",
                "question_version_number": 3,
                "fingerprint": "abc123",
                "legacy": False,
            },
        },
        "target_question_version_id": _VERSION_ID,
        "question_id": 1067,
        "question_version_number": 3,
        "question_text": "What is the immutable stem?",
        "explanation": "",
        "question_type": "single",
        "select_count": 1,
        "options": [
            {"option_label": "A", "option_text": "One", "is_correct": True, "display_order": 1},
            {"option_label": "B", "option_text": "Two", "is_correct": False, "display_order": 2},
        ],
        "evidence": [],
        "decision_history": [],
    }
    row.update(overrides)
    return row


class TestAdminAuthorization(unittest.TestCase):
    def test_non_admin_access_denied(self):
        with self.assertRaises(AuditReviewAccessError):
            assert_admin_reviewer(
                is_admin_user=False,
                is_admin_unlocked=True,
                reviewer_email=_REVIEWER,
            )

    def test_admin_access_succeeds(self):
        email = assert_admin_reviewer(
            is_admin_user=True,
            is_admin_unlocked=True,
            reviewer_email=_REVIEWER,
        )
        self.assertEqual(email, _REVIEWER)


class TestAuditReviewQueries(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSupabase()
        self.fake.set_response("list_audit_runs_for_review_v1", [_sample_run()])
        self.fake.set_response("list_audit_findings_for_review_v1", [_sample_finding()])
        self.fake.set_response("get_audit_finding_review_detail_v1", [_sample_detail()])

    def test_recent_runs_loaded_and_filtered(self):
        runs = list_audit_runs(
            self.fake,
            run_status="completed",
            audit_type="hybrid",
            certification_code="ADM-201",
            blocking_only=True,
        )
        self.assertEqual(len(runs), 1)
        params = self.fake.calls[0][1]
        self.assertEqual(params["p_run_status"], "completed")
        self.assertEqual(params["p_audit_type"], "hybrid")
        self.assertTrue(params["p_blocking_only"])

    def test_findings_tied_to_selected_run(self):
        findings = list_audit_findings(self.fake, audit_run_id=_RUN_ID)
        self.assertEqual(findings[0]["finding_id"], _FINDING_ID)
        self.assertEqual(self.fake.calls[0][1]["p_audit_run_id"], _RUN_ID)

    def test_exact_immutable_question_version_loaded(self):
        detail = get_finding_review_detail(self.fake, finding_id=_FINDING_ID)
        snapshot = load_immutable_question_version(detail)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["question_version_id"], _VERSION_ID)
        self.assertEqual(snapshot["question_text"], "What is the immutable stem?")

    def test_missing_version_is_not_substituted(self):
        detail = _sample_detail(
            target_question_version_id=None,
            question_text=None,
            question_id=None,
            question_version_number=None,
            options=[],
        )
        self.assertIsNone(load_immutable_question_version(detail))


class TestEvidenceContractDisplay(unittest.TestCase):
    def test_new_evidence_contract_renders(self):
        contract = build_evidence_contract_view(_sample_detail())
        self.assertEqual(contract["contract_version"], "1.0.0")
        self.assertEqual(contract["audit_source"], "hybrid")
        self.assertIn("detailed_rationale", contract)

    def test_legacy_findings_normalize_safely(self):
        detail = _sample_detail(metadata={"ruleset_version": "1.0.0"})
        contract = build_evidence_contract_view(detail)
        self.assertEqual(contract["finding_code"], "EXPLANATION_MISSING")
        self.assertTrue(contract.get("legacy"))

    def test_supporting_references_render_safely(self):
        detail = _sample_detail(
            evidence=[
                {
                    "resource_chunk_id": "dddddddd-0000-0000-0000-000000000001",
                    "evidence_role": "supporting",
                    "quote_text": "<script>alert(1)</script>",
                }
            ],
            metadata={"ruleset_version": "1.0.0"},
        )
        contract = build_evidence_contract_view(detail)
        refs = contract.get("supporting_references") or detail["evidence"]
        escaped = escape_review_text(refs[0]["quote_text"])
        self.assertIn("&lt;script&gt;", escaped)


class TestDecisionPersistence(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSupabase()

    def test_accepted_decision_persists(self):
        self.fake.set_response(
            "record_audit_finding_decision_v1",
            [{
                "finding_id": _FINDING_ID,
                "previous_status": "open",
                "new_status": "accepted",
                "reviewer_email": _REVIEWER,
                "reviewer_note": "Valid concern.",
                "decision_id": "eeeeeeee-0000-0000-0000-000000000001",
                "created_at": "2026-06-24T11:00:00+00:00",
                "idempotent": False,
            }],
        )
        row = record_finding_decision(
            self.fake,
            finding_id=_FINDING_ID,
            decision="accepted",
            reviewer_email=_REVIEWER,
            reviewer_note="Valid concern.",
            is_admin_user=True,
            is_admin_unlocked=True,
        )
        self.assertEqual(row["new_status"], "accepted")
        self.assertEqual(
            self.fake.calls[0][1]["p_reviewer_email"],
            _REVIEWER,
        )

    def test_reviewer_note_required(self):
        with self.assertRaises(AuditReviewError):
            validate_reviewer_note("   ")

    def test_invalid_decision_values_fail(self):
        with self.assertRaises(AuditReviewError):
            validate_decision_value("published")

    def test_invalid_status_transitions_fail(self):
        with self.assertRaises(AuditReviewError):
            validate_status_transition("resolved", "accepted")

    def test_repeated_submission_is_idempotent(self):
        self.fake.set_response(
            "record_audit_finding_decision_v1",
            [{
                "finding_id": _FINDING_ID,
                "previous_status": "accepted",
                "new_status": "accepted",
                "reviewer_email": _REVIEWER,
                "reviewer_note": "Already accepted.",
                "decision_id": "eeeeeeee-0000-0000-0000-000000000001",
                "created_at": "2026-06-24T11:00:00+00:00",
                "idempotent": True,
            }],
        )
        row = record_finding_decision(
            self.fake,
            finding_id=_FINDING_ID,
            decision="accepted",
            reviewer_email=_REVIEWER,
            reviewer_note="Already accepted.",
            is_admin_user=True,
            is_admin_unlocked=True,
        )
        self.assertTrue(row["idempotent"])

    def test_non_admin_decision_writes_fail(self):
        with self.assertRaises(AuditReviewAccessError):
            record_finding_decision(
                self.fake,
                finding_id=_FINDING_ID,
                decision="rejected",
                reviewer_email=_REVIEWER,
                reviewer_note="Should fail.",
                is_admin_user=False,
                is_admin_unlocked=True,
            )


class TestMigrationArtifacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_creates_decision_table_and_rpcs(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS public.audit_finding_decisions", self.sql)
        self.assertIn("list_audit_runs_for_review_v1", self.sql)
        self.assertIn("record_audit_finding_decision_v1", self.sql)

    def test_migration_service_role_only(self):
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION public.record_audit_finding_decision_v1",
            self.sql,
        )
        self.assertIn("REVOKE EXECUTE ON FUNCTION public.record_audit_finding_decision_v1", self.sql)

    def test_no_service_role_credentials_exposed(self):
        page_source = (REPO_ROOT / "pages" / "Admin_Audit_Review.py").read_text(encoding="utf-8")
        utils_source = (REPO_ROOT / "utils" / "audit_review.py").read_text(encoding="utf-8")
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", page_source)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", utils_source)
        self.assertNotIn("create_client(", page_source)


class TestPageImport(unittest.TestCase):
    def test_page_imports_without_streamlit_runtime_failure(self):
        import utils.access_control  # noqa: F401
        import utils.session_timeout  # noqa: F401
        import utils.version  # noqa: F401

        fake_st = types.SimpleNamespace(
            set_page_config=lambda *args, **kwargs: None,
            title=lambda *args, **kwargs: None,
            caption=lambda *args, **kwargs: None,
            subheader=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
            success=lambda *args, **kwargs: None,
            markdown=lambda *args, **kwargs: None,
            text=lambda *args, **kwargs: None,
            text_input=lambda *args, **kwargs: types.SimpleNamespace(strip=lambda: ""),
            text_area=lambda *args, **kwargs: "note",
            selectbox=lambda *args, **kwargs: args[1][0] if len(args) > 1 and args[1] else "",
            checkbox=lambda *args, **kwargs: False,
            columns=lambda n: [MagicMock() for _ in range(n)],
            metric=lambda *args, **kwargs: None,
            button=lambda *args, **kwargs: False,
            dataframe=lambda *args, **kwargs: None,
            json=lambda *args, **kwargs: None,
            cache_data=lambda **kwargs: (lambda fn: fn),
            rerun=lambda: None,
            session_state={},
            stop=lambda: (_ for _ in ()).throw(SystemExit()),
        )
        fake_client = FakeSupabase()
        fake_client.set_response("list_audit_runs_for_review_v1", [])

        with patch.dict(sys.modules, {"streamlit": fake_st}):
            with patch("utils.access_control.get_current_user_email", return_value=_REVIEWER), \
                 patch("utils.access_control.get_supabase_admin_client", return_value=fake_client), \
                 patch("utils.access_control.is_admin_unlocked", return_value=True), \
                 patch("utils.access_control.is_admin_user", return_value=True), \
                 patch("utils.access_control.render_app_chrome"), \
                 patch("utils.access_control.require_admin", return_value=_REVIEWER), \
                 patch("utils.session_timeout.enforce_session_timeout"), \
                 patch("utils.session_timeout.show_session_expired_notice"), \
                 patch("utils.version.APP_VERSION", "test"):
                spec = importlib.util.spec_from_file_location(
                    "admin_audit_review_page",
                    REPO_ROOT / "pages" / "Admin_Audit_Review.py",
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(hasattr(module, "_render_run_filters"))


if __name__ == "__main__":
    unittest.main()
