"""
Regression tests for claim_background_job_v1 certification_duplicate_audit allowlist.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    / "20260624170000_v45_fix_claim_certification_duplicate_audit_job_type.sql"
)

ALL_JOB_TYPES = (
    "resource_ingestion",
    "deterministic_audit",
    "llm_audit",
    "hybrid_audit",
    "certification_duplicate_audit",
    "question_generation",
    "candidate_promotion",
    "embedding_generation",
    "other",
)


def _claim_validation_section(sql: str) -> str:
    start = sql.index("IF p_job_types IS NOT NULL THEN")
    end = sql.index("END IF;", start) + len("END IF;")
    return sql[start:end]


class TestClaimCertificationDuplicateAuditJobTypeFix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.foundation_sql = FOUNDATION_MIGRATION.read_text(encoding="utf-8")
        cls.corrective_sql = CORRECTIVE_MIGRATION.read_text(encoding="utf-8")

    def test_foundation_migration_updates_claim_allowlist(self):
        section = _claim_validation_section(self.foundation_sql)
        self.assertIn("'certification_duplicate_audit'", section)
        for job_type in ALL_JOB_TYPES:
            self.assertIn(f"'{job_type}'", section)

    def test_corrective_migration_replaces_claim_background_job_v1_only(self):
        self.assertIn(
            "CREATE OR REPLACE FUNCTION public.claim_background_job_v1(",
            self.corrective_sql,
        )
        self.assertNotIn("CREATE OR REPLACE FUNCTION public.enqueue_background_job_v1(", self.corrective_sql)
        self.assertNotIn("ALTER TABLE public.background_jobs", self.corrective_sql)

    def test_corrective_migration_accepts_certification_duplicate_audit(self):
        section = _claim_validation_section(self.corrective_sql)
        self.assertIn("'certification_duplicate_audit'", section)
        for job_type in ALL_JOB_TYPES:
            self.assertIn(f"'{job_type}'", section)

    def test_corrective_migration_preserves_claim_ambiguity_fix(self):
        self.assertIn("attempt_count     = bj.attempt_count + 1", self.corrective_sql)
        self.assertNotIn("attempt_count     = attempt_count + 1", self.corrective_sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", self.corrective_sql)


if __name__ == "__main__":
    unittest.main()
