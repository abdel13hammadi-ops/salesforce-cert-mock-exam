"""Migration-contract tests for the Sales Cloud Consultant catalog foundation
(SCC-EXP-03B).

These are static, text-based checks against the migration SQL and its
verification SQL, following the same convention as
tests/test_repair_question_1067_migration.py and
tests/test_widen_certification_domain_weight_migration.py: no live
Postgres/Supabase connection is available in this environment, so
correctness is verified by inspecting the exact SQL contract (certification
values, all five exact domains, decimal weight literals, conflict/no-op
branches, and isolation from other certifications) rather than by executing
it against a database.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260714110000_v64_add_sales_cloud_consultant_certification_catalog.sql"
)

VERIFICATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "tests"
    / "v64_sales_cloud_consultant_certification_catalog_verification.sql"
)

EXPECTED_DOMAINS = (
    ("Practical Application of Sales Cloud Expertise", "23.3", 1),
    ("Sales Lifecycle", "20.0", 2),
    ("Consulting & Implementation Strategies", "25.0", 3),
    ("Data Management", "18.3", 4),
    ("Predictive and Generative AI", "13.3", 5),
)


class TestAddSalesCloudConsultantCatalogMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.is_file())

    def test_exact_certification_values_present(self):
        self.assertIn(
            "v_exam_name          text := 'Salesforce Certified Sales Cloud Consultant';",
            self.sql,
        )
        self.assertIn("v_cert_code          text := 'Sales-Con-201';", self.sql)
        self.assertIn("v_passing_score      integer := 73;", self.sql)
        self.assertIn("v_time_limit_minutes integer := 105;", self.sql)

    def test_certification_insert_uses_zero_question_count_and_inactive(self):
        self.assertIn(
            "v_exam_name, v_cert_code,\n            v_passing_score, v_time_limit_minutes, 0, false",
            self.sql,
        )

    def test_all_five_exact_domains_present_with_decimal_weights(self):
        for domain_name, weight_literal, display_order in EXPECTED_DOMAINS:
            with self.subTest(domain=domain_name):
                self.assertIn(domain_name, self.sql)
                self.assertIn(weight_literal, self.sql)

    def test_decimal_weights_are_not_integers(self):
        # Guard against a regression where someone "cleans up" 20.0/25.0 into
        # bare integer literals 20/25, which would silently defeat the
        # numeric(5,1) widening's whole purpose for this certification.
        self.assertIn("20.0, 0, 2", self.sql)
        self.assertIn("25.0, 0, 3", self.sql)
        self.assertNotIn("'Sales Lifecycle', 20, 0, 2", self.sql)
        self.assertNotIn("'Consulting & Implementation Strategies', 25, 0, 3", self.sql)

    def test_insert_domain_count_is_exactly_five(self):
        insert_block = self.sql.split(
            "INSERT INTO public.certification_domains", 1
        )[1].split(";", 1)[0]
        self.assertEqual(insert_block.count("(v_exam_name,"), 5)

    def test_does_not_activate_scc(self):
        insert_stmt = self.sql.split("INSERT INTO public.certifications", 1)[1].split(");", 1)[0]
        self.assertIn("false", insert_stmt)
        self.assertNotIn(", true", insert_stmt)

    def test_does_not_add_questions(self):
        self.assertNotIn("INSERT INTO public.questions", self.sql)
        self.assertNotIn("INSERT INTO public.answer_options", self.sql)
        self.assertNotIn("INSERT INTO public.question_versions", self.sql)

    def test_is_additive_no_dml_against_other_certifications(self):
        upper_sql = self.sql.upper()
        self.assertNotIn("UPDATE PUBLIC.CERTIFICATIONS", upper_sql)
        self.assertNotIn("UPDATE PUBLIC.CERTIFICATION_DOMAINS", upper_sql)
        self.assertNotIn("DELETE FROM", upper_sql)
        # The only certification_code literal outside of comments must be
        # SCC's own; Administrator/BA/PAB codes must not appear as INSERT
        # targets (they may only appear in prose comments, if at all).
        self.assertNotIn("'platform_app_builder'", self.sql)
        self.assertNotIn("'ADM-201'", self.sql)
        self.assertNotIn("'BA-201'", self.sql)

    def test_no_rls_grants_or_indexes_touched(self):
        upper_sql = self.sql.upper()
        for forbidden in ("ROW LEVEL SECURITY", "GRANT ", "REVOKE ", "CREATE INDEX", "DROP INDEX"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, upper_sql)

    def test_certification_conflict_checks_present(self):
        for fragment in (
            "certification_code IS DISTINCT FROM v_cert_code",
            "passing_score IS DISTINCT FROM v_passing_score",
            "time_limit_minutes IS DISTINCT FROM v_time_limit_minutes",
            "is_active IS DISTINCT FROM false",
            "question_count IS DISTINCT FROM 0",
            "v_cert_count > 1",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.sql)

    def test_domain_conflict_checks_present(self):
        for fragment in (
            "v_domain_count <> 5",
            "HAVING count(*) > 1",
            "v_exact_match_count <> 5",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.sql)

    def test_idempotent_no_op_branch_present(self):
        self.assertIn("already exist in the expected shape; no changes made", self.sql)

    def test_on_conflict_clause_not_used(self):
        # The comment header prose explicitly discusses why ON CONFLICT is
        # NOT used; only the executable DO $$ body must be free of it.
        executable_sql = self.sql.split("DO $$", 1)[1]
        self.assertNotIn("ON CONFLICT", executable_sql.upper())

    def test_raises_exception_on_every_conflict_branch(self):
        # Every conflict IF must RAISE EXCEPTION, never silently continue.
        self.assertGreaterEqual(self.sql.count("RAISE EXCEPTION"), 7)

    def test_total_published_weight_is_99_9_not_normalized(self):
        weights = [float(w) for _, w, _ in EXPECTED_DOMAINS]
        self.assertAlmostEqual(sum(weights), 99.9, delta=0.01)


class TestSalesCloudConsultantCatalogVerificationSql(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = VERIFICATION_PATH.read_text(encoding="utf-8")

    def test_verification_file_exists(self):
        self.assertTrue(VERIFICATION_PATH.is_file())

    def test_checks_exactly_one_certification_row(self):
        self.assertIn("v_count = 1", self.sql)

    def test_checks_exact_canonical_exam_name(self):
        self.assertIn(
            "v_row.exam_name = 'Salesforce Certified Sales Cloud Consultant'",
            self.sql,
        )

    def test_checks_certification_code(self):
        self.assertIn("v_row.certification_code = 'Sales-Con-201'", self.sql)

    def test_checks_passing_score_73(self):
        self.assertIn("v_row.passing_score = 73", self.sql)

    def test_checks_time_limit_105(self):
        self.assertIn("v_row.time_limit_minutes = 105", self.sql)

    def test_checks_inactive(self):
        self.assertIn("v_row.is_active = false", self.sql)

    def test_checks_question_count_zero(self):
        self.assertIn("v_row.question_count = 0", self.sql)

    def test_checks_exactly_five_domain_rows(self):
        self.assertIn("v_count = 5", self.sql)

    def test_checks_all_five_exact_domain_names(self):
        for domain_name, _, _ in EXPECTED_DOMAINS:
            with self.subTest(domain=domain_name):
                self.assertIn(domain_name, self.sql)

    def test_checks_exact_decimal_weights_preserved(self):
        for _, weight_literal, _ in EXPECTED_DOMAINS:
            with self.subTest(weight=weight_literal):
                self.assertIn(weight_literal, self.sql)
        self.assertIn("weight = 23.3", self.sql)
        self.assertIn("weight = 18.3", self.sql)
        self.assertIn("weight = 13.3", self.sql)

    def test_checks_weight_total_approximately_99_9_with_tolerance(self):
        self.assertIn("99.9", self.sql)
        self.assertIn("v_delta <= 0.15", self.sql)
        self.assertIn("v_total <> 100", self.sql)

    def test_checks_no_duplicate_domains(self):
        self.assertIn("v_dup_count = 0", self.sql)

    def test_checks_administrator_ba_pab_unchanged(self):
        self.assertIn("Salesforce Certified Platform Administrator", self.sql)
        self.assertIn("Salesforce Certified Business Analyst", self.sql)
        self.assertIn("Salesforce Certified Platform App Builder", self.sql)
        self.assertIn("v_admin_total = 100", self.sql)
        self.assertIn("v_ba_total = 100", self.sql)
        self.assertIn("v_pab_total = 100", self.sql)

    def test_checks_weight_column_remains_numeric_5_1(self):
        self.assertIn("v_data_type = 'numeric'", self.sql)
        self.assertIn("v_numeric_precision = 5", self.sql)
        self.assertIn("v_numeric_scale = 1", self.sql)

    def test_uses_fail_closed_assert_statements(self):
        self.assertGreaterEqual(self.sql.count("ASSERT "), 15)


if __name__ == "__main__":
    unittest.main()
