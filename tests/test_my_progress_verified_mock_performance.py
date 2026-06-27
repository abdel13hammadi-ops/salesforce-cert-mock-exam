"""Focused tests for verified mock performance metrics in My Progress."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.readiness import build_verified_mock_performance_metrics, calculate_readiness


def _attempt(
    attempt_id: int,
    score: float,
    *,
    mode: str = "Paid Mock Exam",
    total_questions: int = 60,
    completed_at: str = "2026-06-24T12:00:00+00:00",
):
    correct_answers = int(round(score * total_questions / 100.0))
    return {
        "id": attempt_id,
        "mode": mode,
        "score": score,
        "total_questions": total_questions,
        "correct_answers": correct_answers,
        "completed_at": completed_at,
        "started_at": completed_at,
        "category": "Needs Analysis",
        "language_code": "en",
    }


def _verified_child_rows(attempt_id: int, total_q: int = 60, *, score: float | None = None):
    correct_count = (
        int(round((score if score is not None else 0.0) * total_q / 100.0))
        if score is not None
        else total_q // 2
    )
    rows = []
    for j in range(total_q):
        rows.append(
            {
                "id": attempt_id * 10000 + j,
                "exam_attempt_id": str(attempt_id),
                "question_id": f"q_{attempt_id}_{j}",
                "is_correct": j < correct_count,
                "category": "Needs Analysis",
                "difficulty": "medium",
                "cognitive_level": "application",
            }
        )
    return rows


class TestVerifiedMockPerformanceMetrics(unittest.TestCase):
    def test_complete_verified_attempts_populate_metrics(self):
        attempts = [
            _attempt(79, 82.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(73, 76.0, completed_at="2026-06-24T12:00:00+00:00"),
        ]
        question_attempts = _verified_child_rows(79, score=82.0) + _verified_child_rows(73, score=76.0)
        metrics = build_verified_mock_performance_metrics(attempts, question_attempts, 60)

        self.assertTrue(metrics["has_verified_mocks"])
        self.assertEqual(metrics["verified_mock_count"], 2)
        self.assertEqual(metrics["latest_score"], 82.0)
        self.assertEqual(metrics["average_score"], 79.0)
        self.assertEqual(metrics["best_score"], 82.0)
        self.assertEqual(len(metrics["trend_attempts"]), 2)

    def test_parent_only_paid_mocks_are_excluded(self):
        attempts = [
            _attempt(71, 3.33, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(72, 3.33, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(73, 0.0, completed_at="2026-06-23T12:00:00+00:00"),
        ]
        metrics = build_verified_mock_performance_metrics(attempts, [], 60)

        self.assertFalse(metrics["has_verified_mocks"])
        self.assertEqual(metrics["verified_mock_count"], 0)
        self.assertIsNone(metrics["latest_score"])

    def test_partial_child_paid_mocks_are_excluded(self):
        attempts = [_attempt(79, 80.0)]
        question_attempts = _verified_child_rows(79, total_q=30, score=80.0)
        metrics = build_verified_mock_performance_metrics(attempts, question_attempts, 60)

        self.assertFalse(metrics["has_verified_mocks"])
        self.assertEqual(metrics["verified_mock_count"], 0)

    def test_verified_mock_count_matches_readiness_verified_count(self):
        attempts = [
            _attempt(79, 3.33, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(75, 3.33, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(73, 0.0, completed_at="2026-06-23T12:00:00+00:00"),
        ]
        question_attempts = _verified_child_rows(79, score=3.33) + _verified_child_rows(73, score=0.0)
        metrics = build_verified_mock_performance_metrics(attempts, question_attempts, 60)
        readiness = calculate_readiness(
            attempts=attempts,
            question_attempts=question_attempts,
            expected_question_count=60,
            question_bank_total=60,
        )

        self.assertEqual(metrics["verified_mock_count"], readiness["eligible_mock_count"])
        self.assertEqual(metrics["verified_mock_count"], 2)

    def test_production_like_fixture_values(self):
        attempts = [
            _attempt(79, 3.33, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(75, 3.33, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(74, 3.33, completed_at="2026-06-23T12:00:00+00:00"),
            _attempt(73, 0.0, completed_at="2026-06-22T12:00:00+00:00"),
            _attempt(72, 3.33, completed_at="2026-06-21T12:00:00+00:00"),
            _attempt(71, 3.33, completed_at="2026-06-20T12:00:00+00:00"),
        ]
        question_attempts = _verified_child_rows(79, score=3.33) + _verified_child_rows(73, score=0.0)
        metrics = build_verified_mock_performance_metrics(attempts, question_attempts, 60)

        self.assertEqual(metrics["verified_mock_count"], 2)
        self.assertEqual(metrics["latest_score"], 3.33)
        self.assertEqual(metrics["average_score"], 1.67)
        self.assertEqual(metrics["best_score"], 3.33)
        self.assertEqual(
            [attempt["id"] for attempt in metrics["trend_attempts"]],
            [73, 79],
        )

    def test_practice_and_daily_sprint_do_not_affect_metrics(self):
        attempts = [
            _attempt(79, 75.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(101, 100.0, mode="Daily Sprint", total_questions=10, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(102, 90.0, mode="Practice by Category", total_questions=10, completed_at="2026-06-23T12:00:00+00:00"),
        ]
        question_attempts = (
            _verified_child_rows(79, score=75.0)
            + _verified_child_rows(101, total_q=10, score=100.0)
            + _verified_child_rows(102, total_q=10, score=90.0)
        )
        metrics = build_verified_mock_performance_metrics(attempts, question_attempts, 60)

        self.assertEqual(metrics["verified_mock_count"], 1)
        self.assertEqual(metrics["latest_score"], 75.0)

    def test_empty_state_when_no_verified_evidence(self):
        attempts = [
            _attempt(71, 3.33),
            _attempt(72, 3.33),
        ]
        metrics = build_verified_mock_performance_metrics(attempts, [], 60)

        self.assertFalse(metrics["has_verified_mocks"])
        self.assertEqual(metrics["verified_mock_count"], 0)
        self.assertIsNone(metrics["latest_score"])
        self.assertEqual(metrics["trend_attempts"], [])


if __name__ == "__main__":
    unittest.main()
