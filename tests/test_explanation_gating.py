"""Focused tests for answer-gated explanation display on practice surfaces."""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.practice_session_persistence import restore_category_practice_session, restore_weak_practice_session
from utils.question_answer_key import (
    EXPLANATION_GATE_HINT,
    effective_explanation_feedback_shown,
    is_answer_selection_complete,
    resolve_required_select_count,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PRACTICE_PAGE = REPO_ROOT / "pages" / "Practice_By_Category.py"
WEAK_PAGE = REPO_ROOT / "pages" / "Weak_Areas_Practice.py"


def _option(opt_id: str, *, correct: bool = False) -> dict:
    return {"id": opt_id, "text": f"Option {opt_id}", "is_correct": correct}


def _single_question() -> dict:
    return {
        "type": "single",
        "select_count": 1,
        "options": [_option("1", correct=True), _option("2")],
        "correct_ids": ["1"],
    }


def _multi_question(select_count: int) -> dict:
    correct_ids = [str(i) for i in range(1, select_count + 1)]
    options = [_option(str(i), correct=i <= select_count) for i in range(1, 5)]
    return {
        "type": "multiple",
        "select_count": select_count,
        "options": options,
        "correct_ids": correct_ids,
    }


def _bank_question(qid: int, *, qtype: str = "single", select_count: int = 1) -> dict:
    if qtype == "multiple":
        correct_ids = [f"{qid}-a", f"{qid}-b"][:select_count]
        options = [
            {"id": f"{qid}-a", "text": "A", "is_correct": f"{qid}-a" in correct_ids},
            {"id": f"{qid}-b", "text": "B", "is_correct": f"{qid}-b" in correct_ids},
            {"id": f"{qid}-c", "text": "C", "is_correct": False},
            {"id": f"{qid}-d", "text": "D", "is_correct": False},
        ]
    else:
        correct_ids = [f"{qid}-a"]
        options = [
            {"id": f"{qid}-a", "text": "A", "is_correct": True},
            {"id": f"{qid}-b", "text": "B", "is_correct": False},
        ]
    return {
        "id": qid,
        "exam_name": "Salesforce Certified Business Analyst",
        "language_code": "en",
        "category": "Discovery",
        "difficulty": "medium",
        "question": f"Question {qid}",
        "type": qtype,
        "select_count": select_count,
        "explanation": "Because.",
        "options": options,
        "correct_ids": correct_ids,
    }


class _FakeSessionState(dict):
    pass


class TestExplanationCompletenessHelper(unittest.TestCase):
    def test_unanswered_single_select_is_incomplete(self):
        question = _single_question()
        self.assertFalse(is_answer_selection_complete([], question))
        self.assertFalse(is_answer_selection_complete(None, question))

    def test_answered_single_select_is_complete(self):
        question = _single_question()
        self.assertTrue(is_answer_selection_complete(["1"], question))

    def test_partial_select_two_is_incomplete(self):
        question = _multi_question(2)
        self.assertFalse(is_answer_selection_complete(["1"], question))

    def test_exactly_two_selections_is_complete(self):
        question = _multi_question(2)
        self.assertTrue(is_answer_selection_complete(["1", "2"], question))

    def test_partial_select_three_is_incomplete(self):
        question = _multi_question(3)
        self.assertFalse(is_answer_selection_complete(["1", "2"], question))

    def test_exactly_three_selections_is_complete(self):
        question = _multi_question(3)
        self.assertTrue(is_answer_selection_complete(["1", "2", "3"], question))

    def test_deselecting_after_opening_hides_explanation(self):
        question = _multi_question(2)
        self.assertTrue(
            effective_explanation_feedback_shown(True, ["1", "2"], question)
        )
        self.assertFalse(
            effective_explanation_feedback_shown(True, ["1"], question)
        )

    def test_feedback_flag_without_complete_answer_is_hidden(self):
        question = _single_question()
        self.assertFalse(effective_explanation_feedback_shown(True, [], question))
        self.assertFalse(effective_explanation_feedback_shown(False, ["1"], question))


class TestPracticeSurfaceSourceGating(unittest.TestCase):
    def test_practice_by_category_uses_shared_rule(self):
        source = PRACTICE_PAGE.read_text(encoding="utf-8")
        self.assertIn("is_answer_selection_complete", source)
        self.assertIn("effective_explanation_feedback_shown", source)
        self.assertIn("EXPLANATION_GATE_HINT", source)
        self.assertIn('disabled=not answer_complete', source)
        self.assertIn("practice_feedback_shown = False", source)

    def test_weak_areas_uses_shared_rule(self):
        source = WEAK_PAGE.read_text(encoding="utf-8")
        self.assertIn("is_answer_selection_complete", source)
        self.assertIn("effective_explanation_feedback_shown", source)
        self.assertIn("EXPLANATION_GATE_HINT", source)
        self.assertIn('"Show Explanation", disabled=not answer_complete', source)
        self.assertIn("weak_feedback_shown = False", source)

    def test_daily_sprint_routes_through_practice_page(self):
        source = PRACTICE_PAGE.read_text(encoding="utf-8")
        self.assertIn("Daily Sprint", source)
        self.assertIn("is_answer_selection_complete", source)

    def test_completion_review_does_not_use_explanation_gate(self):
        practice_source = PRACTICE_PAGE.read_text(encoding="utf-8")
        weak_source = WEAK_PAGE.read_text(encoding="utf-8")
        practice_review = practice_source.split('st.subheader(completion_view["review_heading"])', 1)[1]
        weak_review = weak_source.split('st.header("Answer Review")', 1)[1]
        self.assertNotIn("effective_explanation_feedback_shown", practice_review)
        self.assertNotIn("effective_explanation_feedback_shown", weak_review)
        self.assertIn('st.info(q["explanation"])', practice_review)
        self.assertIn('st.info(q["explanation"])', weak_review)

    def test_navigation_clears_feedback_in_move_helpers(self):
        practice_source = PRACTICE_PAGE.read_text(encoding="utf-8")
        weak_source = WEAK_PAGE.read_text(encoding="utf-8")
        self.assertIn("def move_to_practice_question", practice_source)
        self.assertRegex(
            practice_source,
            r"def move_to_practice_question[\s\S]*?practice_feedback_shown = False",
        )
        self.assertRegex(
            weak_source,
            r"def move_to_weak_question[\s\S]*?weak_feedback_shown = False",
        )


class TestExplanationRestoreGating(unittest.TestCase):
    def test_restored_unanswered_session_does_not_reveal_explanation(self):
        bank = [_bank_question(101), _bank_question(102)]
        state = {
            "v": 1,
            "kind": "category",
            "user_email": "user@example.com",
            "updated_at": time.time(),
            "submitted": False,
            "saved": False,
            "question_ids": ["101", "102"],
            "option_orders": {
                "0": ["101-a", "101-b"],
                "1": ["102-a", "102-b"],
            },
            "current_index": 1,
            "answers": {0: ["101-a"]},
            "feedback_shown": True,
            "mode_label": "Daily Sprint",
        }
        restored = _FakeSessionState()
        self.assertTrue(restore_category_practice_session(state, bank, "user@example.com", restored))
        self.assertFalse(restored["practice_feedback_shown"])

    def test_restored_partial_multi_select_does_not_reveal_explanation(self):
        bank = [_bank_question(101), _bank_question(102, qtype="multiple", select_count=2)]
        state = {
            "v": 1,
            "kind": "weak",
            "user_email": "user@example.com",
            "updated_at": time.time(),
            "submitted": False,
            "saved": False,
            "question_ids": ["101", "102"],
            "option_orders": {
                "0": ["101-a", "101-b"],
                "1": ["102-a", "102-b", "102-c", "102-d"],
            },
            "current_index": 1,
            "answers": {1: ["102-a"]},
            "feedback_shown": True,
            "categories": ["Discovery"],
        }
        restored = _FakeSessionState()
        self.assertTrue(restore_weak_practice_session(state, bank, "user@example.com", restored))
        self.assertFalse(restored["weak_feedback_shown"])
        self.assertEqual(resolve_required_select_count(bank[1]), 2)

    def test_restored_complete_answer_may_keep_explanation_visible(self):
        bank = [_bank_question(101), _bank_question(102)]
        state = {
            "v": 1,
            "kind": "category",
            "user_email": "user@example.com",
            "updated_at": time.time(),
            "submitted": False,
            "saved": False,
            "question_ids": ["101", "102"],
            "option_orders": {
                "0": ["101-a", "101-b"],
                "1": ["102-a", "102-b"],
            },
            "current_index": 1,
            "answers": {1: ["102-b"]},
            "feedback_shown": True,
        }
        restored = _FakeSessionState()
        self.assertTrue(restore_category_practice_session(state, bank, "user@example.com", restored))
        self.assertTrue(restored["practice_feedback_shown"])


class TestExplanationGateHint(unittest.TestCase):
    def test_hint_text_is_stable(self):
        self.assertEqual(
            EXPLANATION_GATE_HINT,
            "Select your answer before viewing the explanation.",
        )


if __name__ == "__main__":
    unittest.main()
