"""Migration-contract tests for widening certification_domains.weight to
numeric(5,1) (SCC-EXP-03A).

These are static, text-based checks against the migration SQL, following the
same convention as tests/test_retrieval_shadow_evaluations_migration.py and
tests/test_repair_question_1067_migration.py: no live Postgres/Supabase
connection is available in this environment, so correctness is verified by
inspecting the migration's exact SQL contract (the guarded type check, the
single ALTER TABLE statement, and the absence of any row-level DML) rather
than by executing it against a database.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260714100000_v63_widen_certification_domain_weight_to_numeric.sql"
)

VERIFICATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "tests"
    / "v63_certification_domain_weight_numeric_verification.sql"
)


class TestWidenCertificationDomainWeightMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        # Executable body only (the DO $$ ... $$; block), excluding the
        # leading comment header where prose necessarily discusses the
        # motivating Sales Cloud Consultant decimal weights and mentions
        # "ALTER TABLE" descriptively without executing it.
        cls.executable_sql = cls.sql.split("DO $$", 1)[1]

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.is_file())

    def test_exact_alter_table_statement_is_present(self):
        self.assertIn(
            "ALTER TABLE public.certification_domains\n"
            "        ALTER COLUMN weight TYPE numeric(5,1)\n"
            "        USING weight::numeric(5,1);",
            self.sql,
        )

    def test_no_row_level_dml_present(self):
        # This migration must only change the column type, never touch row
        # data: no INSERT, UPDATE, or DELETE statement in the executable body.
        upper_sql = self.executable_sql.upper()
        for forbidden in ("INSERT INTO", "UPDATE PUBLIC.", "DELETE FROM"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, upper_sql)

    def test_no_sales_cloud_consultant_catalog_data(self):
        # The comment header may discuss the motivating SCC decimal weights
        # (documentation, not data); the executable body must contain none
        # of it -- no SCC identifiers and no decimal weight literals.
        self.assertNotIn("Sales Cloud Consultant", self.executable_sql)
        self.assertNotIn("23.3", self.executable_sql)
        self.assertNotIn("18.3", self.executable_sql)
        self.assertNotIn("13.3", self.executable_sql)

    def test_no_rls_grants_indexes_or_unrelated_columns_touched(self):
        upper_sql = self.executable_sql.upper()
        for forbidden in (
            "ROW LEVEL SECURITY",
            "GRANT ",
            "REVOKE ",
            "CREATE INDEX",
            "DROP INDEX",
            "ALTER COLUMN QUESTION_COUNT",
            "ALTER COLUMN DISPLAY_ORDER",
            "ALTER COLUMN IS_ACTIVE",
            "ALTER COLUMN DOMAIN_NAME",
            "ALTER COLUMN EXAM_NAME",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, upper_sql)

    def test_guards_against_unexpected_pre_migration_type(self):
        self.assertIn("information_schema.columns", self.sql)
        self.assertIn("v_data_type <> 'integer'", self.sql)
        self.assertIn("RAISE EXCEPTION", self.sql)
        self.assertIn("column not found", self.sql)

    def test_repeated_execution_is_a_safe_no_op(self):
        self.assertIn(
            "v_data_type = 'numeric' AND v_numeric_precision = 5 AND v_numeric_scale = 1",
            self.sql,
        )
        self.assertIn("already numeric(5,1); no changes made", self.sql)
        self.assertIn("RETURN;", self.sql)

    def test_documents_verified_pre_migration_production_baseline(self):
        for fact in (
            "runtime_type = integer",
            "existing row count",
            "19",
            "minimum weight",
            "= 8",
            "maximum weight",
            "= 28",
            "existing fractional rows",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, self.sql)

    def test_single_do_block_only(self):
        self.assertEqual(self.sql.count("DO $$"), 1)
        self.assertEqual(self.executable_sql.count("ALTER TABLE"), 1)


class TestWidenCertificationDomainWeightVerificationSql(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = VERIFICATION_PATH.read_text(encoding="utf-8")

    def test_verification_file_exists(self):
        self.assertTrue(VERIFICATION_PATH.is_file())

    def test_checks_numeric_type_scale_and_precision(self):
        self.assertIn("numeric_scale = 1", self.sql)
        self.assertIn("v_numeric_precision = 5", self.sql)

    def test_checks_all_19_rows_present(self):
        self.assertIn("v_count = 19", self.sql)

    def test_checks_no_existing_value_changed(self):
        self.assertIn("weight <> trunc(weight)", self.sql)
        self.assertIn("v_min_weight = 8", self.sql)
        self.assertIn("v_max_weight = 28", self.sql)

    def test_checks_adm_ba_pab_totals_unchanged(self):
        self.assertIn("Salesforce Certified Platform Administrator", self.sql)
        self.assertIn("Salesforce Certified Business Analyst", self.sql)
        self.assertIn("Salesforce Certified Platform App Builder", self.sql)
        self.assertEqual(self.sql.count("= 100"), 3)

    def test_fractional_probe_uses_rollback_not_permanent_insert(self):
        self.assertIn("BEGIN;", self.sql)
        self.assertIn("ROLLBACK;", self.sql)
        self.assertIn("23.3", self.sql)
        # The probe insert must happen strictly between BEGIN; and ROLLBACK;.
        begin_idx = self.sql.index("BEGIN;")
        rollback_idx = self.sql.index("ROLLBACK;")
        insert_idx = self.sql.index("INSERT INTO public.certification_domains")
        self.assertTrue(begin_idx < insert_idx < rollback_idx)

    def test_confirms_probe_row_not_permanently_persisted(self):
        self.assertIn("V63-VERIFY-FRACTIONAL-PROBE", self.sql)
        self.assertIn("v_probe_count = 0", self.sql)


if __name__ == "__main__":
    unittest.main()
