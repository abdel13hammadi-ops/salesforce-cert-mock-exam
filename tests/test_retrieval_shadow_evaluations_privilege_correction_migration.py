"""Migration-contract tests for the retrieval_shadow_evaluations privilege
correction (V48 hybrid Stage 1 persistence).

The original foundation migration
(20260702230000_v48_retrieval_shadow_evaluations_foundation.sql) has already
been applied to the live database. Live inspection showed service_role held
broader direct ACL grants (SELECT, INSERT, UPDATE, DELETE, TRUNCATE,
REFERENCES, TRIGGER, MAINTAIN) than the migration's own
``GRANT SELECT, INSERT, DELETE ... TO service_role`` implied, because GRANT
is additive and never strips privileges a role already holds. This test
suite proves the corrective migration (added here) revokes everything from
service_role first and then re-grants only the intended three privileges,
without touching the already-applied foundation migration, the table owner,
RLS, anon/authenticated access, or any RPC/runtime file.

These are static, text-based checks against the migration SQL (no live
database connection is available in this environment), following the same
convention as tests/test_retrieval_shadow_evaluations_migration.py.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

FOUNDATION_MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260702230000_v48_retrieval_shadow_evaluations_foundation.sql"
)

CORRECTION_MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260703200000_v48_retrieval_shadow_evaluations_privilege_correction.sql"
)

# Byte-for-byte snapshot of the already-applied foundation migration at the
# time this corrective migration was authored, so any future accidental edit
# to the applied file is caught immediately.
_EXPECTED_FOUNDATION_GRANT_LINE = (
    "GRANT SELECT, INSERT, DELETE ON TABLE public.retrieval_shadow_evaluations "
    "TO service_role;"
)

_FORBIDDEN_REGRANT_PRIVILEGES = (
    "UPDATE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
    "ALL",
)


def _grant_statements(sql: str) -> list[str]:
    return [
        line.strip().rstrip(";")
        for line in sql.splitlines()
        if line.strip().startswith("GRANT")
    ]


def _revoke_statements(sql: str) -> list[str]:
    return [
        line.strip().rstrip(";")
        for line in sql.splitlines()
        if line.strip().startswith("REVOKE")
    ]


class TestFoundationMigrationUnmodified(unittest.TestCase):
    """The already-applied foundation migration must not be edited."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = FOUNDATION_MIGRATION_PATH.read_text(encoding="utf-8")

    def test_foundation_migration_exists(self):
        self.assertTrue(FOUNDATION_MIGRATION_PATH.is_file())

    def test_foundation_migration_original_grant_line_unchanged(self):
        self.assertIn(_EXPECTED_FOUNDATION_GRANT_LINE, self.sql)

    def test_foundation_migration_has_no_service_role_revoke(self):
        # The foundation migration itself must remain exactly as applied:
        # no REVOKE ... FROM service_role was ever part of it, and none
        # should have been added while authoring the corrective migration.
        self.assertNotIn("REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM service_role", self.sql)

    def test_foundation_migration_is_not_staged_or_modified_in_git(self):
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", str(FOUNDATION_MIGRATION_PATH)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.stdout.strip(),
            "",
            msg=f"foundation migration must have zero diff from HEAD, got: {result.stdout!r}",
        )
        result_cached = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--", str(FOUNDATION_MIGRATION_PATH)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result_cached.stdout.strip(), "")


class TestPrivilegeCorrectionMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = CORRECTION_MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_file_exists(self):
        self.assertTrue(CORRECTION_MIGRATION_PATH.is_file())

    def test_no_table_create_drop_or_truncate(self):
        self.assertNotIn("CREATE TABLE", self.sql)
        self.assertNotIn("DROP TABLE", self.sql)
        self.assertNotIn("TRUNCATE TABLE", self.sql)

    def test_contains_revoke_all_from_service_role(self):
        self.assertIn(
            "REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM service_role;",
            self.sql,
        )

    def test_revoke_occurs_before_the_limited_grant(self):
        revoke_idx = self.sql.index(
            "REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM service_role;"
        )
        grant_idx = self.sql.index(
            "GRANT SELECT, INSERT, DELETE ON TABLE public.retrieval_shadow_evaluations TO service_role;"
        )
        self.assertLess(
            revoke_idx,
            grant_idx,
            msg="REVOKE ALL from service_role must appear before the re-GRANT",
        )

    def test_only_select_insert_delete_are_regranted(self):
        grant_lines = [
            line
            for line in _grant_statements(self.sql)
            if "public.retrieval_shadow_evaluations" in line
        ]
        self.assertTrue(grant_lines, "expected at least one GRANT on the table")
        for line in grant_lines:
            with self.subTest(line=line):
                self.assertIn("service_role", line)
                privileges = line.split("ON TABLE", 1)[0].replace("GRANT", "", 1)
                granted = {token.strip() for token in privileges.split(",")}
                self.assertEqual(granted, {"SELECT", "INSERT", "DELETE"})

    def test_forbidden_privileges_are_not_regranted(self):
        grant_lines = [
            line
            for line in _grant_statements(self.sql)
            if "public.retrieval_shadow_evaluations" in line
        ]
        for line in grant_lines:
            privileges_clause = line.split("ON TABLE", 1)[0]
            for forbidden in _FORBIDDEN_REGRANT_PRIVILEGES:
                with self.subTest(line=line, forbidden=forbidden):
                    self.assertNotIn(forbidden, privileges_clause)

    def test_revoke_targets_only_service_role_not_postgres_or_owner(self):
        revoke_lines = _revoke_statements(self.sql)
        self.assertTrue(revoke_lines, "expected at least one REVOKE statement")
        for line in revoke_lines:
            with self.subTest(line=line):
                self.assertNotIn("postgres", line)
                self.assertNotIn("FROM PUBLIC", line)
                self.assertNotIn("FROM anon", line)
                self.assertNotIn("FROM authenticated", line)
                self.assertIn("FROM service_role", line)

    def test_no_anon_or_authenticated_grants_or_policies(self):
        self.assertNotIn("TO anon", self.sql)
        self.assertNotIn("TO authenticated", self.sql)
        self.assertNotIn("CREATE POLICY", self.sql)
        self.assertNotIn("ALTER TABLE", self.sql)  # RLS is not re-toggled here

    def test_no_rls_change(self):
        self.assertNotIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertNotIn("DISABLE ROW LEVEL SECURITY", self.sql)

    def test_no_rpc_or_function_changes(self):
        self.assertNotIn("CREATE OR REPLACE FUNCTION", self.sql)
        self.assertNotIn("CREATE FUNCTION", self.sql)
        self.assertNotIn("DROP FUNCTION", self.sql)

    def test_no_column_or_constraint_changes(self):
        self.assertNotIn("ADD COLUMN", self.sql)
        self.assertNotIn("DROP COLUMN", self.sql)
        self.assertNotIn("ADD CONSTRAINT", self.sql)
        self.assertNotIn("DROP CONSTRAINT", self.sql)

    def test_timestamp_is_after_foundation_migration(self):
        foundation_ts = FOUNDATION_MIGRATION_PATH.name.split("_", 1)[0]
        correction_ts = CORRECTION_MIGRATION_PATH.name.split("_", 1)[0]
        self.assertTrue(foundation_ts.isdigit())
        self.assertTrue(correction_ts.isdigit())
        self.assertGreater(int(correction_ts), int(foundation_ts))

    def test_only_intended_files_changed_in_working_tree(self):
        # No runtime worker file may be modified, and this corrective
        # migration itself must never be modified or deleted relative to
        # HEAD. This intentionally uses `git diff` (tracked-file changes),
        # not `git status` (which also lists untracked files): later,
        # unrelated additive migrations may legitimately appear as new
        # untracked files over time and are not a violation of this
        # corrective slice's own scope.
        result = subprocess.run(
            [
                "git",
                "diff",
                "--name-status",
                "HEAD",
                "--",
                str(CORRECTION_MIGRATION_PATH),
                "workers",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.stdout.strip(),
            "",
            msg=(
                "expected no tracked-file diff for the corrective migration or "
                f"workers/, got: {result.stdout!r}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
