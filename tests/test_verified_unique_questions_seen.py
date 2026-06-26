"""Focused tests for verified unique-question counting used by My Progress."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.readiness import REQUIRED_FULL_MOCKS, calculate_readiness, count_verified_unique_questions_seen


def _utc_days_ago(n: int) -> str:
    dt = datetime.now(tz=timezone.utc) - timedelta(days=n)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mock(score, *, attempt_id, total_questions=60, mode="Paid Mock Exam"):
    return {
        "id": str(attempt_id),
        "mode": mode,
        "score": score,
        "total_questions": total_questions,
        "completed_at": _utc_days_ago(10),
        "domain_breakdown": {},
    }


def _verified_child_rows(attempt_id, total_q=60, *, question_prefix="q"):
    rows = []
    for j in range(total_q):
        rows.append(
            {
                "id": int(attempt_id) * 10000 + j,
                "exam_attempt_id": str(attempt_id),
                "question_id": f"{question_prefix}{j}",
                "is_correct": True,
                "category": "Domain A",
                "difficulty": "medium",
                "cognitive_level": "application",
            }
        )
    return rows


class TestVerifiedUniqueQuestionsSeen(unittest.TestCase):
    def test_verified_mock_with_sixty_unique_child_rows_reports_sixty(self):
        attempts = [_mock(75.0, attempt_id=79)]
        question_attempts = _verified_child_rows(79, total_q=60)

        self.assertEqual(
            count_verified_unique_questions_seen(attempts, question_attempts, 60),
            60,
        )

    def test_verified_mock_without_child_rows_contributes_zero(self):
        attempts = [
            _mock(70.0, attempt_id=75),
            _mock(80.0, attempt_id=79),
        ]
        question_attempts = _verified_child_rows(79, total_q=60)

        self.assertEqual(
            count_verified_unique_questions_seen(attempts, question_attempts, 60),
            60,
        )

    def test_duplicates_across_verified_mocks_are_deduplicated(self):
        attempts = [
            _mock(70.0, attempt_id=1),
            _mock(72.0, attempt_id=2),
        ]
        question_attempts = _verified_child_rows(1, total_q=60) + _verified_child_rows(2, total_q=60)

        self.assertEqual(
            count_verified_unique_questions_seen(attempts, question_attempts, 60),
            60,
        )

    def test_practice_and_daily_sprint_child_rows_are_excluded(self):
        attempts = [
            _mock(75.0, attempt_id=79),
            _mock(90.0, attempt_id=101, total_questions=10, mode="Daily Sprint"),
            _mock(85.0, attempt_id=102, total_questions=10, mode="Practice by Category"),
        ]
        question_attempts = (
            _verified_child_rows(79, total_q=60)
            + _verified_child_rows(101, total_q=10, question_prefix="sprint")
            + _verified_child_rows(102, total_q=10, question_prefix="practice")
        )

        self.assertEqual(
            count_verified_unique_questions_seen(attempts, question_attempts, 60),
            60,
        )

    def test_non_qualifying_paid_mock_rows_are_excluded(self):
        attempts = [
            _mock(95.0, attempt_id=75, total_questions=30),
            _mock(80.0, attempt_id=79, total_questions=60),
        ]
        question_attempts = _verified_child_rows(75, total_q=30) + _verified_child_rows(79, total_q=60)

        self.assertEqual(
            count_verified_unique_questions_seen(attempts, question_attempts, 60),
            60,
        )

    def test_locked_readiness_reports_unique_questions_from_verified_child_rows(self):
        attempts = [
            _mock(70.0, attempt_id=75),
            _mock(80.0, attempt_id=79),
        ]
        question_attempts = _verified_child_rows(79, total_q=60)

        result = calculate_readiness(
            attempts=attempts,
            question_attempts=question_attempts,
            expected_question_count=60,
            question_bank_total=60,
        )

        self.assertTrue(result["is_locked"])
        self.assertLess(result["eligible_mock_count"], REQUIRED_FULL_MOCKS)
        self.assertEqual(result["unique_questions_seen"], 60)


if __name__ == "__main__":
    unittest.main()
