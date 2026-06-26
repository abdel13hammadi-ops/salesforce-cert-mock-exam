"""
Regression tests for certification loader latest-version selection (no published event).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.certification_question_loader import (
    _dedupe_latest_version_per_question,
    load_certification_current_question_versions,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_MIGRATION = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260624160000_v45_certification_duplicate_audit_job.sql"
)
CORRECTIVE_MIGRATION = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260624180000_v45_fix_certification_loader_latest_version.sql"
)

_CERT = "Platform Administrator"


def _loader_sql_section(sql: str) -> str:
    start = sql.index("CREATE OR REPLACE FUNCTION public.list_certification_current_question_versions_v1(")
    end = sql.index("$$;", start) + 3
    return sql[start:end]


class FakeSupabase:
    def __init__(self, rows=None):
        self.calls = []
        self._rows = rows or []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return self

    def execute(self):
        return _FakeResult(self._rows)


class _FakeResult:
    def __init__(self, data):
        self.data = data
        self.error = None


class TestCertificationLoaderLatestVersionMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.foundation_sql = FOUNDATION_MIGRATION.read_text(encoding="utf-8")
        cls.corrective_sql = CORRECTIVE_MIGRATION.read_text(encoding="utf-8")

    def _assert_latest_version_loader_sql(self, sql: str):
        section = _loader_sql_section(sql)
        self.assertNotIn("question_version_events", section)
        self.assertNotIn("published", section.lower())
        self.assertIn("ORDER  BY qv.version_number DESC", section)
        self.assertIn("qv.created_at DESC", section)
        self.assertIn("qv.id DESC", section)
        self.assertIn("LIMIT  1", section)
        self.assertIn("q.is_active = TRUE", section)
        self.assertIn("TRIM(q.exam_name) = TRIM(p_certification_exam_name)", section)

    def test_foundation_migration_uses_latest_version_without_published_event(self):
        self._assert_latest_version_loader_sql(self.foundation_sql)

    def test_corrective_migration_replaces_loader_only(self):
        self.assertIn(
            "CREATE OR REPLACE FUNCTION public.list_certification_current_question_versions_v1(",
            self.corrective_sql,
        )
        self.assertNotIn("claim_background_job_v1", self.corrective_sql)
        self.assertNotIn("enqueue_background_job_v1", self.corrective_sql)
        self._assert_latest_version_loader_sql(self.corrective_sql)


class TestCertificationLoaderLatestVersionBehavior(unittest.TestCase):
    def test_created_only_versions_are_returned(self):
        fake = FakeSupabase([
            {
                "question_version_id": "11111111-0000-0000-0000-000000000001",
                "question_id": 1,
                "certification_exam_name": _CERT,
                "question_text": "Created-only stem",
                "category": "Domain A",
                "version_number": 1,
            },
        ])
        rows = load_certification_current_question_versions(fake, _CERT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question_text"], "Created-only stem")
        self.assertEqual(fake.calls[0][1]["p_certification_exam_name"], _CERT)

    def test_multiple_versions_keep_highest_only(self):
        deduped = _dedupe_latest_version_per_question([
            {
                "question_id": 42,
                "question_version_id": "aaaaaaaa-0000-0000-0000-000000000001",
                "version_number": 1,
                "question_text": "Historical stem",
            },
            {
                "question_id": 42,
                "question_version_id": "bbbbbbbb-0000-0000-0000-000000000002",
                "version_number": 3,
                "question_text": "Latest stem",
            },
            {
                "question_id": 42,
                "question_version_id": "cccccccc-0000-0000-0000-000000000003",
                "version_number": 2,
                "question_text": "Middle stem",
            },
        ])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["question_version_id"], "bbbbbbbb-0000-0000-0000-000000000002")
        self.assertEqual(deduped[0]["version_number"], 3)

    def test_historical_lower_version_excluded_from_loader_output(self):
        fake = FakeSupabase([
            {
                "question_id": 7,
                "question_version_id": "old-version-id",
                "version_number": 1,
                "certification_exam_name": _CERT,
                "question_text": "Old",
            },
            {
                "question_id": 7,
                "question_version_id": "new-version-id",
                "version_number": 2,
                "certification_exam_name": _CERT,
                "question_text": "New",
            },
        ])
        rows = load_certification_current_question_versions(fake, _CERT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question_version_id"], "new-version-id")
        self.assertEqual(rows[0]["question_text"], "New")


if __name__ == "__main__":
    unittest.main()
