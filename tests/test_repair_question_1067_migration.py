"""Focused tests for question 1067 production-data repair migration."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260624210000_v45_repair_question_1067_select_count.sql"
)

EXPECTED_OPTION_TEXTS = [
    "The external third-party marketing agency's graphic design intern.",
    "The Data Privacy and Compliance Officer responsible for health information regulations.",
    "A senior representative from the clinical intake nursing team who executes the daily workflow.",
    "The junior database developer who manages legacy archived backups.",
    "The hardware technician who manages the corporate laptop inventory.",
]

EXPECTED_STEM = (
    "A Salesforce BA is planning a requirements elicitation workshop for a healthcare client. "
    "The project involves highly sensitive patient intake workflows. Which two stakeholders must "
    "the BA ensure are included as core active contributors in these sessions? (Select TWO)"
)

EXPECTED_EXPLANATION = (
    "For a sensitive healthcare project, the BA must include the operational experts who execute "
    "the daily workflow (C) to ensure functional accuracy, and the compliance officer (B) to ensure "
    "the proposed process adheres to data privacy laws. Options A, D, and E represent ancillary "
    "or technical roles that do not own or execute the patient intake business process."
)


class TestRepairQuestion1067Migration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.is_file())

    def test_version_1_is_not_mutated(self):
        self.assertNotRegex(
            self.sql,
            r"UPDATE\s+public\.question_versions[\s\S]*version_number\s*=\s*1",
        )
        self.assertNotRegex(
            self.sql,
            r"UPDATE\s+public\.question_option_versions[\s\S]*question_version_id\s*=\s*v_v1_id",
        )

    def test_exact_option_text_assertions_are_present(self):
        for text in EXPECTED_OPTION_TEXTS:
            with self.subTest(option_text=text):
                self.assertIn(text, self.sql)
        self.assertIn("ao.option_text = c_option_texts[v_idx]", self.sql)
        self.assertIn("qov.option_text = c_option_texts[v_idx]", self.sql)

    def test_exact_verified_stem_and_explanation_literals_are_present(self):
        self.assertIn(EXPECTED_STEM, self.sql)
        self.assertIn(EXPECTED_EXPLANATION, self.sql)
        self.assertIn("c_expected_question_text constant text := $txt$", self.sql)
        self.assertIn("c_expected_explanation constant text := $txt$", self.sql)

    def test_exact_verified_stem_and_explanation_equality_checks_are_present(self):
        self.assertIn("live stem must match verified production text", self.sql)
        self.assertIn("live explanation must match verified production text", self.sql)
        self.assertIn("version 1 stem must match verified production text", self.sql)
        self.assertIn("version 1 explanation must match verified production text", self.sql)
        self.assertIn("live stem must match immutable version 1 snapshot", self.sql)
        self.assertIn("live explanation must match immutable version 1 snapshot", self.sql)
        self.assertIn("v_q.question_text IS DISTINCT FROM c_expected_question_text", self.sql)
        self.assertIn("v_q.explanation IS DISTINCT FROM c_expected_explanation", self.sql)
        self.assertIn("v_v1_question_text IS DISTINCT FROM c_expected_question_text", self.sql)
        self.assertIn("v_v1_explanation IS DISTINCT FROM c_expected_explanation", self.sql)

    def test_brittle_keyword_checks_are_removed(self):
        self.assertNotIn("must identify options B and C", self.sql)
        self.assertNotIn("version 1 stem must reference Select TWO", self.sql)
        self.assertNotIn("!~* '(select two|which two)'", self.sql)
        self.assertNotIn("!~* 'Data Privacy and Compliance Officer'", self.sql)
        self.assertNotIn("!~* 'clinical intake nursing team'", self.sql)

    def test_answer_options_update_uses_qualified_aliases(self):
        self.assertIn("UPDATE public.answer_options AS ao", self.sql)
        self.assertIn("WHEN ao.id IN (4355, 4356) THEN TRUE", self.sql)
        self.assertIn("WHERE  ao.question_id = c_question_id", self.sql)
        self.assertIn("AND  ao.id = ANY (c_option_ids)", self.sql)
        self.assertNotIn(
            "WHERE  question_id = c_question_id\n      AND  id = ANY (c_option_ids)",
            self.sql,
        )

    def test_questions_statements_use_qualified_aliases(self):
        self.assertIn("FROM   public.questions AS q\n    WHERE  q.id = c_question_id\n    FOR UPDATE", self.sql)
        self.assertIn("UPDATE public.questions AS q", self.sql)
        self.assertIn("WHERE  q.id = c_question_id", self.sql)
        self.assertIn(
            "IF (SELECT q.select_count FROM public.questions AS q WHERE q.id = c_question_id) <> 2 THEN",
            self.sql,
        )

    def test_no_unqualified_returns_table_column_collisions_in_function_body(self):
        function_body = self.sql.split("AS $$", 1)[1].rsplit("$$;", 1)[0]
        unsafe_patterns = [
            r"WHERE\s+question_id\s*=",
            r"WHEN\s+id\s+IN\s*\(",
            r"FROM\s+public\.questions\s*\n\s*WHERE\s+id\s*=",
            r"UPDATE\s+public\.answer_options\s*\n\s*SET[\s\S]*?WHEN\s+id\s+IN",
        ]
        for pattern in unsafe_patterns:
            with self.subTest(pattern=pattern):
                self.assertNotRegex(function_body, pattern)

    def test_changed_wording_or_reordered_references_cannot_bypass_exact_equality(self):
        self.assertNotIn("compliance officer (C)", self.sql)
        self.assertNotIn("operational experts who execute the daily workflow (B)", self.sql)
        self.assertIn("IS DISTINCT FROM c_expected_explanation", self.sql)
        self.assertIn("IS DISTINCT FROM c_expected_question_text", self.sql)

    def test_display_order_assertions_are_present(self):
        self.assertIn("c_option_orders constant integer[] := ARRAY[1, 2, 3, 4, 5]", self.sql)
        self.assertIn("ao.display_order = c_option_orders[v_idx]", self.sql)
        self.assertIn("qov.display_order = c_option_orders[v_idx]", self.sql)

    def test_unexpected_text_drift_aborts_repair(self):
        drift_messages = [
            "must match verified text, order, and corrupted correctness",
            "must match verified corrupted snapshot",
            "live answer_options must be exactly ids",
            "must have exactly 5 live answer_options",
            "must match verified production text",
        ]
        for message in drift_messages:
            with self.subTest(message=message):
                self.assertIn(message, self.sql)

    def test_one_time_function_is_dropped_after_execution(self):
        self.assertIn("CREATE OR REPLACE FUNCTION public.repair_question_1067_answer_key_v1()", self.sql)
        self.assertIn("PERFORM public.repair_question_1067_answer_key_v1();", self.sql)
        self.assertIn("DROP FUNCTION IF EXISTS public.repair_question_1067_answer_key_v1();", self.sql)
        self.assertNotIn("GRANT EXECUTE ON FUNCTION public.repair_question_1067_answer_key_v1", self.sql)
        self.assertNotIn("#variable_conflict", self.sql)

    def test_version_2_uses_select_count_two(self):
        self.assertIn("select_count    = 2", self.sql)
        self.assertIn("version_number = 2", self.sql)

    def test_label_array_comparisons_use_explicit_text_array_casts(self):
        self.assertIn(
            "SELECT ARRAY_AGG(\n"
            "               ao.option_label::text\n"
            "               ORDER BY ao.display_order, ao.option_label\n"
            "           )",
            self.sql,
        )
        self.assertIn("v_live_correct_labels = ARRAY['B', 'C']::text[]", self.sql)
        self.assertIn(") IS DISTINCT FROM ARRAY['B', 'C']::text[] THEN", self.sql)
        self.assertNotIn(
            "v_live_correct_labels = ARRAY['B', 'C'] THEN",
            self.sql,
        )
        self.assertNotIn(
            ") IS DISTINCT FROM ARRAY['B', 'C'] THEN",
            self.sql,
        )
        self.assertNotIn(
            "ARRAY_AGG(ao.option_label ORDER BY ao.display_order, ao.option_label)",
            self.sql,
        )

    def test_option_id_arrays_use_explicit_integer_array_types(self):
        self.assertIn("c_option_ids    constant integer[] := ARRAY[4354, 4355, 4356, 4357, 4358]", self.sql)
        self.assertIn("ao.id <> ALL (c_option_ids)", self.sql)
        self.assertIn("ao.id = ANY (c_option_ids)", self.sql)
        self.assertIn("'correct_option_ids', ARRAY[4355, 4356]", self.sql)

    def test_version_2_options_keep_only_b_and_c_correct(self):
        self.assertIn("WHEN ao.id IN (4355, 4356) THEN TRUE", self.sql)
        self.assertIn("correct_option_labels', ARRAY['B', 'C']", self.sql)
        self.assertIn("version 2 must contain exactly 2 correct options", self.sql)

    def test_live_repair_is_exact(self):
        self.assertIn("c_question_id   constant integer := 1067", self.sql)
        self.assertIn("c_option_ids    constant integer[] := ARRAY[4354, 4355, 4356, 4357, 4358]", self.sql)
        self.assertIn("select_count <> 4", self.sql)
        self.assertIn("live correct options are not B and C", self.sql)

    def test_defensive_assertions_present(self):
        required_fragments = [
            "question % not found",
            "missing version 1",
            "version 1 select_count must be 4",
            "question_type must be multiple",
            "quality_status must be approved",
            "already has version 2 but live rows remain corrupted",
        ]
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.sql)

    def test_idempotency_strategy_present(self):
        self.assertIn("'already_repaired'::text", self.sql)
        self.assertIn("v_v2_exists", self.sql)
        self.assertIn("WHERE NOT EXISTS", self.sql)
        self.assertIn("question 1067 not present in this database", self.sql)

    def test_content_hash_uses_canonical_separator_scheme(self):
        self.assertIn("E'\\x01'", self.sql)
        self.assertIn("E'\\x02'", self.sql)
        self.assertIn("E'\\x03'", self.sql)
        self.assertIn("md5(", self.sql)

    def test_immutable_events_are_appended(self):
        for event_type in ("created", "approved", "published", "override_applied", "superseded"):
            with self.subTest(event_type=event_type):
                self.assertIn(f"'{event_type}'", self.sql)


if __name__ == "__main__":
    unittest.main()
