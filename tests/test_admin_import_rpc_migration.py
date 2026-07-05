"""Repository artifact tests for the V54 admin_import_questions_batch_v1 restoration.

These tests only read the migration file's text. They do not connect to any
database and do not execute any SQL. Their purpose is to confirm that the
migration restoring ``admin_import_questions_batch_v1`` to version control
faithfully reproduces the live production function's key safety properties,
and that this restoration step did not silently introduce behavior changes,
new routing, or new hardening beyond what was authorized.
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
    / "20260704000000_v54_restore_admin_import_questions_batch_v1.sql"
)


class TestMigrationArtifacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        start = cls.sql.index("AS $function$")
        end = cls.sql.index("$function$", start + len("AS $function$"))
        cls.function_body = cls.sql[start:end]

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.exists())

    def test_defines_expected_function_signature(self):
        self.assertIn(
            "CREATE OR REPLACE FUNCTION public.admin_import_questions_batch_v1(p_questions jsonb)",
            self.sql,
        )
        self.assertIn("RETURNS jsonb", self.sql)
        self.assertIn("LANGUAGE plpgsql", self.sql)

    def test_security_definer_preserved(self):
        self.assertIn("SECURITY DEFINER", self.sql)

    def test_search_path_pinned(self):
        self.assertIn("SET search_path TO 'public', 'pg_temp'", self.sql)

    def test_certification_language_domain_validation_present(self):
        self.assertIn("from public.certifications c", self.sql)
        self.assertIn("references unknown certification", self.sql)
        self.assertIn("from public.languages l", self.sql)
        self.assertIn("references unknown language", self.sql)
        self.assertIn("from public.certification_domains cd", self.sql)
        self.assertIn("references unconfigured domain", self.sql)

    def test_student_attempt_protection_present(self):
        self.assertIn("from public.question_attempts qa", self.sql)
        self.assertIn(
            "has student attempts and its tested content cannot be overwritten",
            self.sql,
        )

    def test_answer_option_protection_present(self):
        self.assertIn(
            "has student attempts and its answer options cannot be overwritten",
            self.sql,
        )
        self.assertIn("from public.answer_options ao", self.sql)

    def test_revoke_statements_present_for_public_anon_authenticated(self):
        self.assertIn(
            "REVOKE ALL ON FUNCTION public.admin_import_questions_batch_v1(jsonb) FROM PUBLIC;",
            self.sql,
        )
        self.assertIn(
            "REVOKE ALL ON FUNCTION public.admin_import_questions_batch_v1(jsonb) FROM anon;",
            self.sql,
        )
        self.assertIn(
            "REVOKE ALL ON FUNCTION public.admin_import_questions_batch_v1(jsonb) FROM authenticated;",
            self.sql,
        )

    def test_service_role_grant_present(self):
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION public.admin_import_questions_batch_v1(jsonb) TO service_role;",
            self.sql,
        )

    def test_comment_on_function_present(self):
        self.assertIn(
            "COMMENT ON FUNCTION public.admin_import_questions_batch_v1(jsonb) IS",
            self.sql,
        )

    def test_anon_not_granted_execute(self):
        self.assertNotIn(
            "GRANT EXECUTE ON FUNCTION public.admin_import_questions_batch_v1(jsonb) TO anon",
            self.sql,
        )

    def test_authenticated_not_granted_execute(self):
        self.assertNotIn(
            "GRANT EXECUTE ON FUNCTION public.admin_import_questions_batch_v1(jsonb) TO authenticated",
            self.sql,
        )

    def test_no_candidate_or_version_routing_introduced(self):
        self.assertNotIn("question_candidates", self.sql)
        self.assertNotIn("question_versions", self.sql)
        self.assertNotIn("promote_question_candidate", self.sql)
        self.assertNotIn("create_question_version", self.sql)

    def test_no_behavioral_hardening_added(self):
        # Scoped to the executable function body only, since the migration's
        # surrounding header comments legitimately discuss (without adding)
        # FOR UPDATE locking as a deferred, out-of-scope hardening idea.
        self.assertNotIn("FOR UPDATE", self.function_body)
        self.assertNotIn("for update", self.function_body)

    def test_migration_not_referenced_from_python_execution_paths(self):
        # This is a source-control restoration only: nothing in the
        # repository should invoke this migration file as SQL.
        admin_import = (REPO_ROOT / "pages" / "Admin_Import.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "20260704000000_v54_restore_admin_import_questions_batch_v1",
            admin_import,
        )


if __name__ == "__main__":
    unittest.main()
