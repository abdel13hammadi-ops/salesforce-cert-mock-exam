"""Deterministic audit coverage for BA multi-select repair manifest."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures.ba_multiselect_repair_manifest import REPAIR_MANIFEST
from workers.deterministic_audit import check_stem_select_count, run_deterministic_checks


def _audit_snapshot(entry: dict, *, select_count: int, explanation: str | None = None) -> dict:
    options = []
    labels = entry["after_correct_labels"] if select_count == entry["after_select_count"] else None
    for idx, label in enumerate(entry["option_labels"], start=1):
        if labels is not None:
            is_correct = label in labels
        else:
            is_correct = entry["before_correct"][idx - 1]
        options.append(
            {
                "option_label": label,
                "option_text": entry["option_texts"][idx - 1],
                "is_correct": is_correct,
                "display_order": entry["option_orders"][idx - 1],
            }
        )
    return {
        "question_text": entry["stem"],
        "explanation": explanation if explanation is not None else entry["explanation"],
        "question_type": "multiple",
        "select_count": select_count,
        "options": options,
    }


class TestBaMultiselectStemCountAudit(unittest.TestCase):
    def test_corrupted_manifest_questions_emit_stem_count_mismatch(self):
        for entry in REPAIR_MANIFEST:
            with self.subTest(question_id=entry["question_id"]):
                snapshot = _audit_snapshot(entry, select_count=entry["before_select_count"])
                codes = [finding["finding_code"] for finding in check_stem_select_count(snapshot)]
                self.assertEqual(codes, ["STEM_COUNT_MISMATCH"])

    def test_repaired_manifest_questions_do_not_emit_stem_count_mismatch(self):
        for entry in REPAIR_MANIFEST:
            with self.subTest(question_id=entry["question_id"]):
                explanation = entry["repaired_explanation"] or entry["explanation"]
                snapshot = _audit_snapshot(
                    entry,
                    select_count=entry["after_select_count"],
                    explanation=explanation,
                )
                codes = [finding["finding_code"] for finding in check_stem_select_count(snapshot)]
                self.assertEqual(codes, [])

    def test_repaired_manifest_questions_pass_deterministic_stem_and_count_checks(self):
        for entry in REPAIR_MANIFEST:
            with self.subTest(question_id=entry["question_id"]):
                explanation = entry["repaired_explanation"] or entry["explanation"]
                snapshot = _audit_snapshot(
                    entry,
                    select_count=entry["after_select_count"],
                    explanation=explanation,
                )
                codes = {
                    finding["finding_code"]
                    for finding in run_deterministic_checks(snapshot)
                    if finding["finding_code"] in {"STEM_COUNT_MISMATCH", "CORRECT_COUNT_MISMATCH", "INVALID_SELECT_COUNT"}
                }
                self.assertEqual(codes, set())


if __name__ == "__main__":
    unittest.main()
