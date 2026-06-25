"""
Migration artifact tests for V45 fail_background_job_v1 ambiguity fix.
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
    / "20260624140000_v45_fix_fail_background_job_ambiguity.sql"
)


class TestFailBackgroundJobAmbiguityFixMigration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_replaces_fail_background_job_v1_with_same_signature(self):
        self.assertIn("CREATE OR REPLACE FUNCTION public.fail_background_job_v1(", self.sql)
        self.assertIn("p_job_id               uuid,", self.sql)
        self.assertIn("p_worker_id            text,", self.sql)
        self.assertIn("p_error_message        text,", self.sql)
        self.assertIn("p_retry_delay_seconds  integer DEFAULT 60,", self.sql)
        self.assertIn("p_checkpoint           jsonb   DEFAULT NULL,", self.sql)
        self.assertIn("p_metadata             jsonb   DEFAULT '{}'::jsonb", self.sql)
        self.assertIn("SECURITY INVOKER", self.sql)
        self.assertIn("SET search_path = public, pg_catalog", self.sql)

    def test_migration_qualifies_ambiguous_available_at_reference(self):
        self.assertIn("UPDATE public.background_jobs AS bj", self.sql)
        self.assertIn("ELSE bj.available_at", self.sql)

    def test_migration_does_not_retain_bare_else_available_at(self):
        self.assertNotIn("ELSE available_at", self.sql)

    def test_migration_preserves_retry_and_dead_letter_branches(self):
        self.assertIn("IF v_attempt_count < v_max_attempts THEN", self.sql)
        self.assertIn("v_final_status := 'pending'", self.sql)
        self.assertIn(
            "v_available_at := now() + (p_retry_delay_seconds || ' seconds')::interval",
            self.sql,
        )
        self.assertIn("v_final_status := 'dead_letter'", self.sql)
        self.assertIn("WHEN v_final_status = 'pending'", self.sql)
        self.assertIn("THEN v_available_at", self.sql)

    def test_migration_privilege_hardening(self):
        signature = "public.fail_background_job_v1(\n    uuid, text, text, integer, jsonb, jsonb\n)"
        self.assertIn(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;", self.sql)
        self.assertIn(f"REVOKE EXECUTE ON FUNCTION {signature} FROM anon;", self.sql)
        self.assertIn(
            f"REVOKE EXECUTE ON FUNCTION {signature} FROM authenticated;",
            self.sql,
        )
        self.assertIn(f"GRANT EXECUTE ON FUNCTION {signature} TO service_role;", self.sql)


if __name__ == "__main__":
    unittest.main()
