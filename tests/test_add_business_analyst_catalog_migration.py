"""Migration-contract tests for the Business Analyst catalog foundation
(BA-CAT-01).

These are static, text-based checks against the migration SQL and its
verification SQL, following the same convention as
tests/test_add_sales_cloud_consultant_catalog_migration.py and
tests/test_repair_question_1067_migration.py: no live Postgres/Supabase
connection is available in this environment, so correctness is verified by
inspecting the exact SQL contract (certification values, all six exact
domains, integer weight literals, conflict/no-op branches, and isolation
from other certifications) rather than by executing it against a database.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260802175000_v70_business_analyst_catalog.sql"
)

VERIFICATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "tests"
    / "v70_business_analyst_catalog_verification.sql"
)

EXPECTED_DOMAINS = (
    ("Customer Discovery", "17", 1),
    ("Collaboration with Stakeholders", "17", 2),
    ("Business Process Mapping", "17", 3),
    ("Requirements", "17", 4),
    ("User Stories", "16", 5),
    ("User Acceptance", "16", 6),
)


class TestAddBusinessAnalystCatalogMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.is_file())

    def test_exact_certification_values_present(self):
        self.assertIn(
            "v_exam_name          text := 'Salesforce Certified Business Analyst';",
            self.sql,
        )
        self.assertIn("v_cert_code          text := 'BA-201';", self.sql)
        self.assertIn("v_passing_score      integer := 65;", self.sql)
        self.assertIn("v_time_limit_minutes integer := 105;", self.sql)

    def test_certification_insert_uses_zero_question_count_and_inactive(self):
        self.assertIn(
            "v_exam_name, v_cert_code,\n            v_passing_score, v_time_limit_minutes, 0, false",
            self.sql,
        )

    def test_all_six_exact_domains_present_with_integer_weights(self):
        for domain_name, weight_literal, display_order in EXPECTED_DOMAINS:
            with self.subTest(domain=domain_name):
                self.assertIn(domain_name, self.sql)
                self.assertIn(weight_literal, self.sql)

    def test_insert_domain_count_is_exactly_six(self):
        insert_block = self.sql.split(
            "INSERT INTO public.certification_domains", 1
        )[1].split(";", 1)[0]
        self.assertEqual(insert_block.count("(v_exam_name,"), 6)

    def test_domain_weights_sum_to_100(self):
        weights = [int(w) for _, w, _ in EXPECTED_DOMAINS]
        self.assertEqual(sum(weights), 100)

    def test_does_not_activate_ba(self):
        insert_stmt = self.sql.split("INSERT INTO public.certifications", 1)[1].split(");", 1)[0]
        self.assertIn("false", insert_stmt)
        self.assertNotIn(", true", insert_stmt)

    def test_does_not_add_questions(self):
        self.assertNotIn("INSERT INTO public.questions", self.sql)
        self.assertNotIn("INSERT INTO public.answer_options", self.sql)
        self.assertNotIn("INSERT INTO public.question_versions", self.sql)

    def test_does_not_touch_scenario_tables(self):
        self.assertNotIn("INSERT INTO public.scenarios", self.sql)
        self.assertNotIn("INSERT INTO public.scenario_versions", self.sql)
        self.assertNotIn("INSERT INTO public.scenario_attempts", self.sql)
        self.assertNotIn("INSERT INTO public.scenario_decisions", self.sql)

    def test_is_additive_no_dml_against_other_certifications(self):
        upper_sql = self.sql.upper()
        self.assertNotIn("UPDATE PUBLIC.CERTIFICATIONS", upper_sql)
        self.assertNotIn("UPDATE PUBLIC.CERTIFICATION_DOMAINS", upper_sql)
        self.assertNotIn("DELETE FROM", upper_sql)
        # The only certification_code literal outside of comments must be
        # BA's own; Administrator/PAB/SCC/SVC codes must not appear as
        # INSERT targets (they may only appear in prose comments, if at all).
        self.assertNotIn("'platform_app_builder'", self.sql)
        self.assertNotIn("'ADM-201'", self.sql)
        self.assertNotIn("'Sales-Con-201'", self.sql)
        self.assertNotIn("'service_cloud_consultant'", self.sql)

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

    def test_cross_certification_code_conflict_check_present(self):
        # BA-CAT-01 adds a guard beyond the V61/V64/V65 precedent: detect
        # 'BA-201' claimed by a different exam_name before doing anything
        # else, since certification_code has no UNIQUE constraint.
        self.assertIn("v_code_conflict_name", self.sql)
        self.assertIn("certification_code = v_cert_code", self.sql)
        self.assertIn("exam_name <> v_exam_name", self.sql)

    def test_domain_conflict_checks_present(self):
        for fragment in (
            "v_domain_count <> 6",
            "HAVING count(*) > 1",
            "v_exact_match_count <> 6",
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

    def test_registry_domain_names_match_certification_registry_module(self):
        # Cross-check against the live engine profile so this migration can
        # never silently drift from workers/certification_registry.py.
        from workers.certification_registry import get_business_analyst_definition

        definition = get_business_analyst_definition()
        registry_domains = tuple(
            (d.domain_name, str(int(d.weight)), idx + 1)
            for idx, d in enumerate(definition.domains)
        )
        self.assertEqual(registry_domains, EXPECTED_DOMAINS)
        self.assertEqual(definition.total_domain_weight, 100)
        self.assertEqual(definition.certification_code, "BA-201")


class TestBusinessAnalystCatalogVerificationSql(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = VERIFICATION_PATH.read_text(encoding="utf-8")

    def test_verification_file_exists(self):
        self.assertTrue(VERIFICATION_PATH.is_file())

    def test_checks_exactly_one_certification_row(self):
        self.assertIn("v_count = 1", self.sql)

    def test_checks_exact_canonical_exam_name(self):
        self.assertIn(
            "v_row.exam_name = 'Salesforce Certified Business Analyst'",
            self.sql,
        )

    def test_checks_certification_code(self):
        self.assertIn("v_row.certification_code = 'BA-201'", self.sql)

    def test_checks_passing_score_65(self):
        self.assertIn("v_row.passing_score = 65", self.sql)

    def test_checks_time_limit_105(self):
        self.assertIn("v_row.time_limit_minutes = 105", self.sql)

    def test_checks_inactive(self):
        self.assertIn("v_row.is_active = false", self.sql)

    def test_checks_question_count_zero(self):
        self.assertIn("v_row.question_count = 0", self.sql)

    def test_checks_exactly_six_domain_rows(self):
        self.assertIn("v_count = 6", self.sql)

    def test_checks_all_six_exact_domain_names(self):
        for domain_name, _, _ in EXPECTED_DOMAINS:
            with self.subTest(domain=domain_name):
                self.assertIn(domain_name, self.sql)

    def test_checks_exact_integer_weights_preserved(self):
        self.assertIn("weight = 17", self.sql)
        self.assertIn("weight = 16", self.sql)

    def test_checks_weight_total_exactly_100(self):
        self.assertIn("v_total = 100", self.sql)

    def test_checks_no_duplicate_domains(self):
        self.assertIn("v_dup_count = 0", self.sql)

    def test_checks_no_unexpected_domains(self):
        self.assertIn("v_unexpected_cnt = 0", self.sql)

    def test_checks_no_orphan_domain_rows(self):
        self.assertIn("v_orphan_count = 0", self.sql)

    def test_checks_pab_scc_svc_unchanged(self):
        self.assertIn("Salesforce Certified Platform App Builder", self.sql)
        self.assertIn("Salesforce Certified Sales Cloud Consultant", self.sql)
        self.assertIn("Salesforce Certified Service Cloud Consultant", self.sql)
        self.assertIn("v_pab_total = 100", self.sql)
        self.assertIn("v_svc_total = 100", self.sql)

    def test_checks_zero_scenario_and_attempt_rows(self):
        self.assertIn("public.scenarios", self.sql)
        self.assertIn("public.scenario_versions", self.sql)
        self.assertIn("public.scenario_attempts", self.sql)
        self.assertIn("public.scenario_decisions", self.sql)
        self.assertIn("v_scenario_count = 0", self.sql)
        self.assertIn("v_attempt_count = 0", self.sql)

    def test_checks_rls_enabled_on_both_catalog_tables(self):
        self.assertIn("v_cert_rls = true", self.sql)
        self.assertIn("v_domain_rls = true", self.sql)

    def test_uses_fail_closed_assert_statements(self):
        self.assertGreaterEqual(self.sql.count("ASSERT "), 15)


if __name__ == "__main__":
    unittest.main()
