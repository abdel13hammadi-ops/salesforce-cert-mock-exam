"""
Migration artifact tests for V45 claim_background_job_v1 ambiguity fix.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260624130000_v45_fix_claim_background_job_ambiguity.sql"
)


class TestClaimBackgroundJobAmbiguityFixMigration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_replaces_claim_background_job_v1(self):
        self.assertIn("CREATE OR REPLACE FUNCTION public.claim_background_job_v1(", self.sql)
        self.assertIn("SECURITY INVOKER", self.sql)
        self.assertIn("SET search_path = public, pg_catalog", self.sql)

    def test_migration_uses_qualified_attempt_count_increment(self):
        self.assertIn("attempt_count     = bj.attempt_count + 1", self.sql)

    def test_migration_does_not_retain_bare_attempt_count_increment(self):
        self.assertNotIn("attempt_count     = attempt_count + 1", self.sql)
        self.assertNotIn("attempt_count = attempt_count + 1", self.sql)

    def test_migration_qualifies_returning_columns(self):
        returning_section = self.sql.split("RETURNING", 1)[1]
        for column in (
            "bj.job_type",
            "bj.payload",
            "bj.checkpoint",
            "bj.attempt_count",
            "bj.max_attempts",
            "bj.lease_expires_at",
            "bj.model_name",
            "bj.prompt_version",
            "bj.metadata",
        ):
            self.assertIn(column, returning_section)

    def test_migration_preserves_skip_locked_claim_pattern(self):
        self.assertIn("FOR UPDATE SKIP LOCKED", self.sql)
        self.assertIn("UPDATE public.background_jobs AS bj", self.sql)

    def test_migration_privilege_hardening(self):
        signature = "public.claim_background_job_v1(\n    text, integer, text[]\n)"
        self.assertIn(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;", self.sql)
        self.assertIn(f"REVOKE EXECUTE ON FUNCTION {signature} FROM anon;", self.sql)
        self.assertIn(
            f"REVOKE EXECUTE ON FUNCTION {signature} FROM authenticated;",
            self.sql,
        )
        self.assertIn(f"GRANT EXECUTE ON FUNCTION {signature} TO service_role;", self.sql)


if __name__ == "__main__":
    unittest.main()
