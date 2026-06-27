"""Focused tests for canonical multi-select answer-key handling."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.question_answer_key import (
    cap_multi_select_selection,
    get_correct_selection,
    is_answer_correct,
    is_answer_key_valid,
    is_multiple_select,
    reconcile_multi_select_selection,
    resolve_required_select_count,
    validate_question_answer_key,
)


def _option(opt_id: str, text: str, *, correct: bool = False) -> dict:
    return {"id": opt_id, "text": text, "is_correct": correct}


def _multiple_question(*, select_count: int, correct_ids: list[str]) -> dict:
    options = [
        _option("1", "Alpha", correct="1" in correct_ids),
        _option("2", "Beta", correct="2" in correct_ids),
        _option("3", "Gamma", correct="3" in correct_ids),
        _option("4", "Delta", correct="4" in correct_ids),
    ]
    return {
        "id": 101,
        "type": "multiple",
        "question_type": "multiple",
        "select_count": select_count,
        "options": options,
        "correct_ids": correct_ids,
    }


def _question_1067_corrupted_state() -> dict:
    return {
        "id": 1067,
        "type": "multiple",
        "question_type": "multiple",
        "select_count": 4,
        "options": [
            _option("4354", "The external third-party marketing agency's graphic design intern.", correct=True),
            _option("4355", "The Data Privacy and Compliance Officer responsible for health information regulations.", correct=True),
            _option("4356", "A senior representative from the clinical intake nursing team who executes the daily workflow.", correct=True),
            _option("4357", "The junior database developer who manages legacy archived backups.", correct=True),
            _option("4358", "The hardware technician who manages the corporate laptop inventory.", correct=False),
        ],
        "correct_ids": ["4354", "4355", "4356", "4357"],
    }


def _question_1067_repaired_state() -> dict:
    return {
        "id": 1067,
        "type": "multiple",
        "question_type": "multiple",
        "select_count": 2,
        "options": [
            _option("4354", "The external third-party marketing agency's graphic design intern.", correct=False),
            _option("4355", "The Data Privacy and Compliance Officer responsible for health information regulations.", correct=True),
            _option("4356", "A senior representative from the clinical intake nursing team who executes the daily workflow.", correct=True),
            _option("4357", "The junior database developer who manages legacy archived backups.", correct=False),
            _option("4358", "The hardware technician who manages the corporate laptop inventory.", correct=False),
        ],
        "correct_ids": ["4355", "4356"],
    }


class TestQuestionAnswerKey(unittest.TestCase):
    def test_valid_select_two_displays_and_enforces_two(self):
        question = _multiple_question(select_count=2, correct_ids=["1", "3"])

        self.assertTrue(is_multiple_select(question))
        self.assertEqual(resolve_required_select_count(question), 2)
        self.assertEqual(get_correct_selection(question), ["1", "3"])
        self.assertEqual(reconcile_multi_select_selection(["1", "3", "4"], ["1", "3"], 2), ["1", "3"])
        self.assertEqual(cap_multi_select_selection(["1", "3", "4"], 2), ["1", "3"])

    def test_valid_select_three(self):
        question = _multiple_question(select_count=3, correct_ids=["1", "2", "4"])

        self.assertEqual(resolve_required_select_count(question), 3)
        self.assertTrue(is_answer_key_valid(question))
        self.assertTrue(is_answer_correct(["1", "2", "4"], question))
        self.assertFalse(is_answer_correct(["1", "2"], question))

    def test_single_select_remains_unchanged(self):
        question = {
            "type": "single",
            "select_count": 1,
            "options": [_option("1", "Only", correct=True), _option("2", "Other")],
            "correct_ids": ["1"],
        }

        self.assertFalse(is_multiple_select(question))
        self.assertEqual(resolve_required_select_count(question), 1)
        self.assertTrue(is_answer_correct(["1"], question))
        self.assertFalse(is_answer_correct(["1", "2"], question))

    def test_contradictory_answer_key_is_detected(self):
        question = _multiple_question(select_count=2, correct_ids=["1", "2", "3", "4"])

        valid, codes = validate_question_answer_key(question)

        self.assertFalse(valid)
        self.assertIn("CORRECT_COUNT_MISMATCH", codes)
        self.assertFalse(is_answer_key_valid(question))

    def test_missing_select_count_on_multiple_is_detected(self):
        question = _multiple_question(select_count=2, correct_ids=["1", "2"])
        question["select_count"] = None

        valid, codes = validate_question_answer_key(question)

        self.assertFalse(valid)
        self.assertIn("INVALID_SELECT_COUNT", codes)

    def test_scoring_uses_same_canonical_count(self):
        question = _multiple_question(select_count=2, correct_ids=["1", "3"])

        self.assertTrue(is_answer_correct(["1", "3"], question))
        self.assertFalse(is_answer_correct(["1"], question))
        self.assertFalse(is_answer_correct(["1", "3", "4"], question))
        self.assertFalse(is_answer_correct(["1", "2"], question))

    def test_old_fallback_to_correct_option_count_is_not_used_for_display(self):
        """Four marked-correct options with select_count=2 must fail validation, not show Choose 4."""
        question = _multiple_question(select_count=2, correct_ids=["1", "2", "3", "4"])

        self.assertFalse(is_answer_key_valid(question))
        self.assertEqual(resolve_required_select_count(question), 2)

    def test_exam_text_shape_supports_scoring(self):
        question = {
            "type": "multiple",
            "select_count": 2,
            "options": ["A", "B", "C", "D"],
            "answers": ["A", "C"],
        }

        self.assertTrue(is_answer_key_valid(question))
        self.assertEqual(resolve_required_select_count(question), 2)
        self.assertTrue(is_answer_correct(["A", "C"], question))
        self.assertFalse(is_answer_correct(["A", "B", "C"], question))

    def test_question_1067_corrupted_state_requires_four_selections(self):
        question = _question_1067_corrupted_state()

        self.assertTrue(is_answer_key_valid(question))
        self.assertEqual(resolve_required_select_count(question), 4)
        self.assertFalse(is_answer_correct(["4355", "4356"], question))
        self.assertTrue(is_answer_correct(["4354", "4355", "4356", "4357"], question))

    def test_question_1067_repaired_state_is_valid_select_two(self):
        question = _question_1067_repaired_state()

        self.assertTrue(is_answer_key_valid(question))
        self.assertEqual(resolve_required_select_count(question), 2)
        self.assertEqual(get_correct_selection(question), ["4355", "4356"])
        self.assertTrue(is_answer_correct(["4355", "4356"], question))
        self.assertFalse(is_answer_correct(["4354", "4355", "4356", "4357"], question))


if __name__ == "__main__":
    unittest.main()
