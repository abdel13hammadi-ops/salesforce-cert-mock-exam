"""Focused tests for BA multi-select batch repair migration."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.fixtures.ba_multiselect_repair_manifest import (
    ACTOR,
    MIGRATION_NAME,
    QUESTION_IDS,
    QUESTION_1126_CORRUPTED_EXPLANATION_PREFIX,
    QUESTION_1126_REPAIRED_EXPLANATION,
    REPAIR_MANIFEST,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO_ROOT / "supabase" / "migrations" / f"{MIGRATION_NAME}.sql"


class TestRepairBaMultiselectBatchMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        cls.function_body = cls.sql.split("AS $$", 1)[1].rsplit("$$;", 1)[0]

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.is_file())

    def test_all_ten_question_ids_are_in_manifest(self):
        for question_id in QUESTION_IDS:
            with self.subTest(question_id=question_id):
                self.assertIn(f"\n            {question_id},\n", self.sql)

    def test_exact_correction_mappings_are_present(self):
        expectations = {
            1046: (3, 2, ["C", "D"], [4269, 4270]),
            1055: (2, 3, ["A", "B", "D"], [4304, 4305, 4307]),
            1081: (4, 2, ["B", "C"], [4412, 4413]),
            1091: (3, 2, ["A", "C"], [4452, 4454]),
            1094: (2, 3, ["A", "B", "C"], [4465, 4466, 4467]),
            1102: (4, 2, ["B", "C"], [4500, 4501]),
            1107: (3, 2, ["A", "C"], [4520, 4522]),
            1116: (2, 3, ["A", "B", "D"], [4557, 4558, 4560]),
            1125: (4, 2, ["B", "E"], [4596, 4599]),
            1126: (2, 3, ["A", "C", "D"], [4600, 4602, 4603]),
        }
        for entry in REPAIR_MANIFEST:
            before, after, labels, ids = expectations[entry["question_id"]]
            with self.subTest(question_id=entry["question_id"]):
                self.assertEqual(entry["before_select_count"], before)
                self.assertEqual(entry["after_select_count"], after)
                self.assertEqual(entry["after_correct_labels"], labels)
                self.assertEqual(entry["after_correct_ids"], ids)

    def test_option_texts_and_ids_are_guarded(self):
        for entry in REPAIR_MANIFEST:
            with self.subTest(question_id=entry["question_id"]):
                for option_text in entry["option_texts"]:
                    sql_text = option_text.replace("'", "''")
                    self.assertTrue(
                        option_text in self.sql or sql_text in self.sql,
                        msg=f"missing option text for question {entry['question_id']}",
                    )
                self.assertIn(
                    "ARRAY[" + ", ".join(str(option_id) for option_id in entry["option_ids"]) + "]::integer[]",
                    self.sql,
                )

    def test_question_1126_explanation_is_corrected_exactly(self):
        self.assertIn(QUESTION_1126_CORRUPTED_EXPLANATION_PREFIX, self.sql)
        self.assertIn(QUESTION_1126_REPAIRED_EXPLANATION, self.sql)
        self.assertIn("WHEN v_entry.repaired_explanation IS NOT NULL THEN v_entry.repaired_explanation", self.sql)
        self.assertNotIn("UPDATE public.question_versions", self.sql)

    def test_version_1_is_not_mutated(self):
        self.assertNotRegex(
            self.sql,
            r"UPDATE\s+public\.question_versions[\s\S]*version_number\s*=\s*1",
        )
        self.assertNotRegex(
            self.sql,
            r"UPDATE\s+public\.question_option_versions",
        )

    def test_each_question_gets_exactly_one_version_2_append(self):
        self.assertIn("version_number = 2", self.sql)
        self.assertIn("supersedes_version_id", self.sql)
        self.assertEqual(self.sql.count("INSERT INTO public.question_versions ("), 1)

    def test_version_2_uses_corrected_select_count_and_answer_key(self):
        self.assertIn("WHEN ao.id = ANY (v_entry.after_correct_ids) THEN TRUE", self.sql)
        self.assertIn("select_count    = v_entry.after_select_count", self.sql)
        self.assertIn("version 2 correct option count mismatch", self.sql)

    def test_transaction_is_all_or_nothing(self):
        self.assertIn("batch requires all % questions, found %", self.sql)
        self.assertIn("_repair_manifest", self.sql)
        self.assertIn("Phase 1: validate every question before any write.", self.sql)
        self.assertIn("Phase 2: apply repairs for every question in one transaction.", self.sql)

    def test_partial_state_fails(self):
        self.assertIn(
            "repair blocked: batch contains partial repaired state (% of % already corrected)",
            self.sql,
        )
        self.assertIn(
            "already corrected but immutable version 2 is missing",
            self.sql,
        )
        self.assertIn(
            "already has version 2 but live rows remain corrupted",
            self.sql,
        )

    def test_complete_rerun_is_idempotent(self):
        self.assertIn("'already_repaired'::text", self.sql)
        self.assertIn("v_already_repaired = array_length(c_question_ids, 1)", self.sql)
        self.assertIn("WHERE NOT EXISTS", self.sql)

    def test_aliases_and_array_casts_prevent_prior_production_failures(self):
        self.assertIn("UPDATE public.answer_options AS ao", self.sql)
        self.assertIn("FROM   public.questions AS q", self.sql)
        self.assertIn("ao.option_label::text", self.sql)
        self.assertIn("IS DISTINCT FROM v_entry.after_correct_labels", self.sql)
        self.assertNotRegex(
            self.function_body,
            r"UPDATE\s+public\.answer_options\s*\n\s*SET[\s\S]*?WHEN\s+id\s+IN",
        )
        self.assertNotIn("ARRAY_AGG(ao.option_label ORDER BY ao.display_order, ao.option_label)", self.sql)

    def test_one_time_function_is_dropped_after_execution(self):
        self.assertIn("CREATE OR REPLACE FUNCTION public.repair_ba_multiselect_batch_v1()", self.sql)
        self.assertIn("PERFORM public.repair_ba_multiselect_batch_v1();", self.sql)
        self.assertIn("DROP FUNCTION IF EXISTS public.repair_ba_multiselect_batch_v1();", self.sql)
        self.assertNotIn("#variable_conflict", self.sql)

    def test_content_version_precondition_and_increment(self):
        self.assertIn("content_version must be 1 before repair", self.sql)
        self.assertIn("content_version = 2", self.sql)

    def test_immutable_events_are_appended(self):
        for event_type in ("created", "approved", "published", "override_applied", "superseded"):
            with self.subTest(event_type=event_type):
                self.assertIn(f"'{event_type}'", self.sql)

    def test_actor_and_migration_metadata(self):
        self.assertIn(ACTOR, self.sql)
        self.assertIn(MIGRATION_NAME, self.sql)


if __name__ == "__main__":
    unittest.main()
