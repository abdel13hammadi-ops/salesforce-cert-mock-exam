"""Focused tests for practice browser-refresh recovery."""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.practice_session_persistence import (
    MAX_AGE_SECONDS,
    build_category_practice_state,
    build_weak_practice_state,
    capture_option_orders,
    clear_category_practice_state,
    clear_weak_practice_state,
    restore_category_practice_session,
    restore_questions_from_state,
    restore_weak_practice_session,
    validate_practice_state,
)


def _question(qid: int, *, category: str = "Discovery") -> dict:
    return {
        "id": qid,
        "exam_name": "Salesforce Certified Business Analyst",
        "language_code": "en",
        "category": category,
        "difficulty": "medium",
        "question": f"Question {qid}",
        "type": "single",
        "select_count": 1,
        "explanation": "Because.",
        "options": [
            {"id": f"{qid}-a", "text": "A", "is_correct": True},
            {"id": f"{qid}-b", "text": "B", "is_correct": False},
        ],
        "correct_ids": [f"{qid}-a"],
    }


class _FakeSessionState(dict):
    pass


class TestPracticeSessionPersistence(unittest.TestCase):
    def setUp(self):
        self.bank = [_question(101), _question(102), _question(103)]
        for question in self.bank:
            question["options"] = list(reversed(question["options"]))

    def test_refresh_restores_exact_question_ids_order_and_options(self):
        session = _FakeSessionState(
            {
                "practice_started": True,
                "practice_submitted": False,
                "practice_saved": False,
                "practice_exam_name": "Salesforce Certified Business Analyst",
                "practice_language_code": "en",
                "practice_category": "Discovery",
                "practice_mode_label": "Daily Sprint",
                "practice_count": 3,
                "practice_questions": self.bank,
                "practice_option_orders": capture_option_orders(self.bank),
                "practice_current_index": 1,
                "practice_answers": {0: ["101-a"], 1: ["102-b"]},
                "practice_feedback_shown": True,
                "practice_question_time_spent": {0: 12.5},
                "practice_started_at": time.time(),
            }
        )
        state = build_category_practice_state(session, "user@example.com")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["question_ids"], ["101", "102", "103"])
        self.assertEqual(state["mode_label"], "Daily Sprint")

        restored = _FakeSessionState()
        self.assertTrue(restore_category_practice_session(state, self.bank, "user@example.com", restored))
        self.assertEqual([q["id"] for q in restored["practice_questions"]], [101, 102, 103])
        self.assertEqual(restored["practice_current_index"], 1)
        self.assertEqual(restored["practice_answers"], {0: ["101-a"], 1: ["102-b"]})
        self.assertEqual(restored["practice_mode_label"], "Daily Sprint")
        self.assertEqual(
            [opt["id"] for opt in restored["practice_questions"][0]["options"]],
            self.bank[0]["options"] and [opt["id"] for opt in self.bank[0]["options"]],
        )

    def test_weak_areas_refresh_restores_domains_and_answers(self):
        session = _FakeSessionState(
            {
                "weak_started": True,
                "weak_submitted": False,
                "weak_saved": False,
                "weak_exam_name": "Salesforce Certified Business Analyst",
                "weak_language_code": "en",
                "weak_categories": ["Discovery", "Testing"],
                "weak_questions": self.bank[:2],
                "weak_option_orders": capture_option_orders(self.bank[:2]),
                "weak_current_index": 1,
                "weak_answers": {1: ["102-b"]},
                "weak_started_at": time.time(),
            }
        )
        state = build_weak_practice_state(session, "user@example.com")
        restored = _FakeSessionState()
        self.assertTrue(restore_weak_practice_session(state, self.bank, "user@example.com", restored))
        self.assertEqual(restored["weak_categories"], ["Discovery", "Testing"])
        self.assertEqual(restored["weak_current_index"], 1)
        self.assertEqual(restored["weak_answers"], {1: ["102-b"]})

    def test_another_user_cannot_restore_session(self):
        state = {
            "v": 1,
            "kind": "category",
            "user_email": "owner@example.com",
            "updated_at": time.time(),
            "submitted": False,
            "saved": False,
            "question_ids": ["101"],
            "option_orders": {"0": ["101-a", "101-b"]},
        }
        self.assertFalse(validate_practice_state(state, user_email="other@example.com", kind="category"))

    def test_submitted_or_saved_sessions_are_not_restored(self):
        base = {
            "v": 1,
            "kind": "category",
            "user_email": "user@example.com",
            "updated_at": time.time(),
            "question_ids": ["101"],
        }
        self.assertFalse(validate_practice_state({**base, "submitted": True, "saved": False}, user_email="user@example.com", kind="category"))
        self.assertFalse(validate_practice_state({**base, "submitted": False, "saved": True}, user_email="user@example.com", kind="category"))

    def test_expired_recovery_state_is_rejected(self):
        state = {
            "v": 1,
            "kind": "weak",
            "user_email": "user@example.com",
            "updated_at": time.time() - MAX_AGE_SECONDS - 60,
            "submitted": False,
            "saved": False,
            "question_ids": ["101"],
        }
        self.assertFalse(validate_practice_state(state, user_email="user@example.com", kind="weak"))

    def test_invalid_question_reference_fails_restore(self):
        state = {
            "v": 1,
            "kind": "category",
            "user_email": "user@example.com",
            "updated_at": time.time(),
            "submitted": False,
            "saved": False,
            "question_ids": ["999"],
            "option_orders": {"0": ["999-a", "999-b"]},
            "current_index": 0,
            "answers": {},
        }
        restored = _FakeSessionState()
        self.assertFalse(restore_category_practice_session(state, self.bank, "user@example.com", restored))

    def test_build_state_is_empty_for_submitted_active_session(self):
        session = _FakeSessionState(
            {
                "practice_started": True,
                "practice_submitted": True,
                "practice_questions": self.bank,
            }
        )
        self.assertIsNone(build_category_practice_state(session, "user@example.com"))

    def test_restore_questions_honors_saved_option_order(self):
        bank = [_question(101)]
        shuffled = capture_option_orders([{**bank[0], "options": list(reversed(bank[0]["options"]))}])
        restored = restore_questions_from_state(bank, [101], shuffled)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual([opt["id"] for opt in restored[0]["options"]], shuffled["0"])

    def test_clear_helpers_do_not_raise_without_streamlit_query(self):
        clear_category_practice_state()
        clear_weak_practice_state()


if __name__ == "__main__":
    unittest.main()
