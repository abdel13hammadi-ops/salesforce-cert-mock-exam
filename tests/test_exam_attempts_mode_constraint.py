"""
Regression tests for exam_attempts.mode check constraint migration.

Verifies the corrective migration matches verified production validation and
adds Daily Sprint without weakening the whitelist.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260624190000_v45_allow_daily_sprint_exam_attempt_mode.sql"
)

LEGACY_ALLOWED_MODES = (
    "Free Mock Exam",
    "Paid Mock Exam",
    "Timed Mock Exam",
    "Practice by Category",
    "Weak Areas Practice",
)

NEW_ALLOWED_MODE = "Daily Sprint"
DISALLOWED_INFERRED_MODE = "Timed Practice"


def _extract_allowed_modes(sql: str) -> list[str]:
    match = re.search(
        r"ARRAY\[(.*?)\]::text\[\]",
        sql,
        re.DOTALL,
    )
    if not match:
        raise AssertionError("Could not parse chk_exam_attempts_mode ARRAY clause")
    return re.findall(r"'((?:''|[^'])*)'", match.group(1))


class TestExamAttemptsModeConstraintMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
        cls.allowed_modes = _extract_allowed_modes(cls.migration_sql)

    def test_migration_replaces_existing_constraint_safely(self):
        self.assertIn("DROP CONSTRAINT IF EXISTS chk_exam_attempts_mode", self.migration_sql)
        self.assertIn("ADD CONSTRAINT chk_exam_attempts_mode", self.migration_sql)
        self.assertIn("mode IS NULL", self.migration_sql)
        self.assertIn("mode::text = ANY (", self.migration_sql)

    def test_migration_permits_daily_sprint(self):
        self.assertIn(NEW_ALLOWED_MODE, self.allowed_modes)

    def test_migration_retains_null_mode(self):
        self.assertIn("mode IS NULL", self.migration_sql)

    def test_migration_retains_verified_production_modes(self):
        for mode in LEGACY_ALLOWED_MODES:
            with self.subTest(mode=mode):
                self.assertIn(mode, self.allowed_modes)

    def test_migration_does_not_introduce_timed_practice(self):
        self.assertNotIn(DISALLOWED_INFERRED_MODE, self.allowed_modes)

    def test_migration_does_not_weaken_validation(self):
        self.assertEqual(
            set(self.allowed_modes),
            set(LEGACY_ALLOWED_MODES + (NEW_ALLOWED_MODE,)),
        )
        self.assertNotIn("CHECK (true)", self.migration_sql.lower())
        self.assertNotIn("CHECK (1=1)", self.migration_sql)


if __name__ == "__main__":
    unittest.main()
