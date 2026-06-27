"""Tests for V45 Phase 4E publication gate."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.audit_review import assert_admin_reviewer, record_finding_decision
from utils.publication_gate import (
    BLOCKING_FINDING_STATUSES,
    NON_BLOCKING_FINDING_STATUSES,
    PublicationGateError,
    finding_blocks_publication,
    finding_tied_to_question_version,
    format_publication_status_message,
    get_publication_status,
    publish_question_version,
    summarize_blocking_findings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260624240000_v45_question_version_publication_gate.sql"
)
VERIFICATION_PATH = (
    REPO_ROOT / "supabase" / "tests" / "v45_publication_gate_verification.sql"
)

_VERSION_A = "aaaaaaaa-0000-0000-0000-000000000001"
_VERSION_B = "bbbbbbbb-0000-0000-0000-000000000002"
_FINDING_ID = "cccccccc-0000-0000-0000-000000000003"
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
        self._errors: dict[str, str] = {}

    def set_response(self, name: str, data: list):
        self._responses[name] = data

    def set_error(self, name: str, message: str):
        self._errors[name] = message

    def rpc(self, name, params):
        self.calls.append((name, params))
        if name in self._errors:
            return _FakeRpcBuilder(data=[], error=self._errors[name])
        return _FakeRpcBuilder(self._responses.get(name, []))


def _finding(**overrides):
    base = {
        "finding_id": _FINDING_ID,
        "finding_code": "EXPLANATION_MISSING",
        "finding_status": "open",
        "materiality": "blocking",
        "title": "Missing explanation",
        "metadata": {},
        "run_target_question_version_id": _VERSION_A,
    }
    base.update(overrides)
    return base


class TestPublicationEligibilityRules(unittest.TestCase):
    def test_open_blocking_finding_prevents_publication(self):
        self.assertTrue(
            finding_blocks_publication(
                _finding(finding_status="open"),
                question_version_id=_VERSION_A,
            )
        )

    def test_accepted_blocking_finding_still_prevents_publication(self):
        self.assertTrue(
            finding_blocks_publication(
                _finding(finding_status="accepted"),
                question_version_id=_VERSION_A,
            )
        )

    def test_rejected_blocking_finding_permits_publication(self):
        self.assertFalse(
            finding_blocks_publication(
                _finding(finding_status="rejected"),
                question_version_id=_VERSION_A,
            )
        )

    def test_resolved_blocking_finding_permits_publication(self):
        self.assertFalse(
            finding_blocks_publication(
                _finding(finding_status="resolved"),
                question_version_id=_VERSION_A,
            )
        )

    def test_overridden_does_not_block(self):
        self.assertFalse(
            finding_blocks_publication(
                _finding(finding_status="overridden"),
                question_version_id=_VERSION_A,
            )
        )

    def test_non_blocking_materiality_permits_publication(self):
        self.assertFalse(
            finding_blocks_publication(
                _finding(materiality="warning", severity="high"),
                question_version_id=_VERSION_A,
            )
        )

    def test_other_version_finding_does_not_block(self):
        self.assertFalse(
            finding_blocks_publication(
                _finding(run_target_question_version_id=_VERSION_B),
                question_version_id=_VERSION_A,
            )
        )

    def test_legacy_metadata_version_anchor_blocks_exact_version(self):
        self.assertTrue(
            finding_tied_to_question_version(
                question_version_id=_VERSION_A,
                run_target_question_version_id=None,
                metadata={"question_version_id_a": _VERSION_A},
            )
        )

    def test_finding_without_exact_version_identity_does_not_contaminate(self):
        self.assertFalse(
            finding_tied_to_question_version(
                question_version_id=_VERSION_A,
                run_target_question_version_id=_VERSION_B,
                metadata={"question_id": 1067},
            )
        )

    def test_summarize_blocking_findings(self):
        status = summarize_blocking_findings(
            [_finding(), _finding(finding_status="rejected", finding_id="x")],
            question_version_id=_VERSION_A,
        )
        self.assertFalse(status["publishable"])
        self.assertEqual(status["blocking_finding_count"], 1)


class TestPublicationRpcIntegration(unittest.TestCase):
    def test_get_publication_status_from_rpc(self):
        fake = FakeSupabase()
        fake.set_response(
            "get_question_version_publication_status_v1",
            [{
                "question_version_id": _VERSION_A,
                "publishable": False,
                "blocking_finding_count": 2,
                "blocking_findings": [
                    {"finding_id": _FINDING_ID, "finding_code": "X", "finding_status": "open"}
                ],
            }],
        )
        status = get_publication_status(fake, question_version_id=_VERSION_A)
        self.assertFalse(status["publishable"])
        self.assertEqual(status["blocking_finding_count"], 2)
        self.assertIn("Blocking", format_publication_status_message(status))

    def test_publish_blocked_error_is_readable(self):
        fake = FakeSupabase()
        fake.set_error(
            "publish_question_version_v1",
            "publication blocked: 1 unresolved blocking audit finding(s)",
        )
        with self.assertRaises(PublicationGateError) as ctx:
            publish_question_version(
                fake,
                question_version_id=_VERSION_A,
                actor_email=_REVIEWER,
                reason="Attempt publish",
            )
        self.assertIn("blocked", str(ctx.exception).lower())

    def test_successful_publish_rpc(self):
        fake = FakeSupabase()
        fake.set_response(
            "publish_question_version_v1",
            [{"question_version_id": _VERSION_A, "question_id": 1067, "version_number": 2}],
        )
        row = publish_question_version(
            fake,
            question_version_id=_VERSION_A,
            actor_email=_REVIEWER,
            reason="Ready to publish",
        )
        self.assertEqual(row["question_id"], 1067)


class TestAdminReviewCompatibility(unittest.TestCase):
    def test_admin_review_decision_still_works(self):
        fake = FakeSupabase()
        fake.set_response(
            "record_audit_finding_decision_v1",
            [{
                "finding_id": _FINDING_ID,
                "previous_status": "open",
                "new_status": "rejected",
                "reviewer_email": _REVIEWER,
                "reviewer_note": "False positive",
                "decision_id": "dddddddd-0000-0000-0000-000000000004",
                "created_at": "2026-06-24T12:00:00+00:00",
                "idempotent": False,
            }],
        )
        row = record_finding_decision(
            fake,
            finding_id=_FINDING_ID,
            decision="rejected",
            reviewer_email=_REVIEWER,
            reviewer_note="False positive",
            is_admin_user=True,
            is_admin_unlocked=True,
        )
        self.assertEqual(row["new_status"], "rejected")

    def test_non_admin_cannot_bypass_gate_via_decision_write(self):
        with self.assertRaises(Exception):
            assert_admin_reviewer(
                is_admin_user=False,
                is_admin_unlocked=True,
                reviewer_email=_REVIEWER,
            )


class TestMigrationArtifacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        cls.verification = VERIFICATION_PATH.read_text(encoding="utf-8")

    def test_migration_defines_gate_helpers_and_publish_check(self):
        self.assertIn("is_question_version_publishable_v1", self.sql)
        self.assertIn("get_question_version_publication_status_v1", self.sql)
        self.assertIn("publication blocked:", self.sql)
        self.assertIn("pg_advisory_xact_lock(hashtext(p_question_version_id::text))", self.sql)

    def test_migration_preserves_duplicate_pair_complete_logic(self):
        self.assertIn("ON CONFLICT", self.sql)
        self.assertIn("DUPLICATE_QUESTION_STEM_EXACT", self.sql)

    def test_blocking_statuses_in_sql(self):
        self.assertIn("('open', 'accepted')", self.sql)

    def test_verification_script_covers_workflow(self):
        self.assertIn("record_audit_finding_decision_v1", self.verification)
        self.assertIn("publish_question_version_v1", self.verification)
        self.assertIn("publication blocked", self.verification.lower())

    def test_no_service_role_credentials_exposed(self):
        page = (REPO_ROOT / "pages" / "Admin_Audit_Review.py").read_text(encoding="utf-8")
        utils = (REPO_ROOT / "utils" / "publication_gate.py").read_text(encoding="utf-8")
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", page)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", utils)


class TestStatusSets(unittest.TestCase):
    def test_blocking_status_set(self):
        self.assertEqual(BLOCKING_FINDING_STATUSES, {"open", "accepted"})

    def test_non_blocking_includes_overridden(self):
        self.assertIn("overridden", NON_BLOCKING_FINDING_STATUSES)


if __name__ == "__main__":
    unittest.main()
