"""Focused tests for shared learner analytics contracts."""

from __future__ import annotations

import inspect
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.activity_modes import (
    DAILY_SPRINT,
    FREE_MOCK_EXAM,
    PAID_MOCK_EXAM,
    PRACTICE_BY_CATEGORY,
    WEAK_AREAS_PRACTICE,
)
from utils.learner_analytics import (
    build_readiness_display_contract,
    build_study_activity_summary,
    build_verified_domain_performance,
    build_verified_mock_performance,
    build_verified_mock_performance_metrics,
    normalize_activity_history_row,
    rank_weak_domains,
)
from utils.readiness import calculate_readiness


def _attempt(
    attempt_id: int,
    score: float,
    *,
    mode: str = PAID_MOCK_EXAM,
    total_questions: int = 60,
    completed_at: str = "2026-06-24T12:00:00+00:00",
    exam_name: str = "Salesforce Certified Platform Administrator",
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
        "exam_name": exam_name,
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
                "category": "Domain A",
                "difficulty": "medium",
                "cognitive_level": "application",
            }
        )
    return rows


class TestVerifiedMockPerformance(unittest.TestCase):
    def test_only_verified_paid_mocks_are_included(self):
        attempts = [
            _attempt(79, 82.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(73, 76.0, completed_at="2026-06-24T12:00:00+00:00"),
        ]
        question_attempts = _verified_child_rows(79, score=82.0) + _verified_child_rows(73, score=76.0)
        contract = build_verified_mock_performance(attempts, question_attempts, 60)

        self.assertTrue(contract.has_verified_mocks)
        self.assertEqual(contract.attempt_count, 2)
        self.assertEqual(contract.latest_score, 82.0)
        self.assertEqual(contract.average_score, 79.0)
        self.assertEqual(contract.best_score, 82.0)
        self.assertEqual(contract.previous_score, 76.0)
        self.assertEqual(contract.score_change, 6.0)

    def test_free_mock_is_excluded(self):
        attempts = [
            _attempt(79, 82.0),
            _attempt(80, 90.0, mode=FREE_MOCK_EXAM),
        ]
        question_attempts = _verified_child_rows(79, score=82.0) + _verified_child_rows(80, score=90.0)
        contract = build_verified_mock_performance(attempts, question_attempts, 60)

        self.assertEqual(contract.attempt_count, 1)
        self.assertEqual(contract.latest_score, 82.0)

    def test_practice_weak_area_and_sprint_are_excluded(self):
        attempts = [
            _attempt(79, 75.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(101, 100.0, mode=DAILY_SPRINT, total_questions=10, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(102, 90.0, mode=PRACTICE_BY_CATEGORY, total_questions=10, completed_at="2026-06-23T12:00:00+00:00"),
            _attempt(103, 85.0, mode=WEAK_AREAS_PRACTICE, total_questions=10, completed_at="2026-06-22T12:00:00+00:00"),
        ]
        question_attempts = (
            _verified_child_rows(79, score=75.0)
            + _verified_child_rows(101, total_q=10, score=100.0)
            + _verified_child_rows(102, total_q=10, score=90.0)
            + _verified_child_rows(103, total_q=10, score=85.0)
        )
        contract = build_verified_mock_performance(attempts, question_attempts, 60)

        self.assertEqual(contract.attempt_count, 1)
        self.assertEqual(contract.latest_score, 75.0)

    def test_trend_points_are_chronological_and_deterministic(self):
        attempts = [
            _attempt(79, 82.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(73, 76.0, completed_at="2026-06-24T12:00:00+00:00"),
        ]
        question_attempts = _verified_child_rows(79, score=82.0) + _verified_child_rows(73, score=76.0)
        contract = build_verified_mock_performance(
            attempts,
            question_attempts,
            60,
            passing_threshold=68.0,
        )

        self.assertEqual(len(contract.score_series), 2)
        self.assertEqual(contract.score_series[0].attempt_id, 73)
        self.assertEqual(contract.score_series[1].attempt_id, 79)
        self.assertEqual(contract.score_series[0].sequence_number, 1)
        self.assertEqual(contract.score_series[1].sequence_number, 2)
        self.assertEqual(contract.score_series[0].passing_threshold, 68.0)

    def test_empty_state_is_explicit(self):
        contract = build_verified_mock_performance([], [], 60)

        self.assertFalse(contract.has_verified_mocks)
        self.assertFalse(contract.has_sufficient_data)
        self.assertIsNone(contract.latest_score)
        self.assertEqual(contract.score_series, ())


class TestReadinessDisplayContract(unittest.TestCase):
    def test_locked_readiness_exposes_progress_counts(self):
        readiness = calculate_readiness(
            attempts=[_attempt(79, 70.0)],
            question_attempts=_verified_child_rows(79, score=70.0),
            expected_question_count=60,
            question_bank_total=60,
        )
        contract = build_readiness_display_contract(readiness)

        self.assertTrue(contract.is_locked)
        self.assertEqual(contract.completed_verified_mock_count, readiness["eligible_mock_count"])
        self.assertEqual(contract.required_mock_count, 3)
        self.assertGreater(contract.remaining_mock_count, 0)
        self.assertEqual(contract.unlock_message_key, "readiness_unlock_after_verified_mocks")
        self.assertEqual(contract.progress_message_key, "readiness_progress_completed_of_required")

    def test_locked_readiness_never_invents_score(self):
        readiness = calculate_readiness(
            attempts=[_attempt(79, 70.0)],
            question_attempts=_verified_child_rows(79, score=70.0),
            expected_question_count=60,
            question_bank_total=60,
        )
        contract = build_readiness_display_contract(readiness)

        self.assertIsNone(contract.readiness_score)

    def test_unlocked_readiness_preserves_calculated_score(self):
        attempts = [
            _attempt(79, 82.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(78, 80.0, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(77, 78.0, completed_at="2026-06-23T12:00:00+00:00"),
        ]
        question_attempts = (
            _verified_child_rows(79, score=82.0)
            + _verified_child_rows(78, score=80.0)
            + _verified_child_rows(77, score=78.0)
        )
        readiness = calculate_readiness(
            attempts=attempts,
            question_attempts=question_attempts,
            expected_question_count=60,
            question_bank_total=60,
        )
        contract = build_readiness_display_contract(readiness)

        self.assertFalse(contract.is_locked)
        self.assertEqual(contract.readiness_score, readiness["score"])


class TestVerifiedDomainPerformance(unittest.TestCase):
    def test_unverified_activity_is_excluded(self):
        attempts = [
            _attempt(79, 75.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(81, 100.0, mode=DAILY_SPRINT, total_questions=10, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(82, 90.0, mode=PRACTICE_BY_CATEGORY, total_questions=10, completed_at="2026-06-23T12:00:00+00:00"),
            _attempt(84, 70.0, mode=FREE_MOCK_EXAM, total_questions=60, completed_at="2026-06-21T12:00:00+00:00"),
        ]
        question_attempts = (
            _verified_child_rows(79, score=75.0)
            + _verified_child_rows(81, total_q=10, score=100.0)
            + _verified_child_rows(82, total_q=10, score=90.0)
            + _verified_child_rows(84, score=70.0)
        )
        rows = build_verified_domain_performance(attempts, question_attempts, 60)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Total"], 60)

    def test_domain_rows_support_weakest_first_ranking_and_exam_order(self):
        attempts = [_attempt(79, 66.67)]
        question_attempts = []
        idx = 0
        for domain, correct, total in [("Weak Domain", 8, 20), ("Strong Domain", 32, 40)]:
            for j in range(total):
                question_attempts.append(
                    {
                        "id": 790000 + idx,
                        "exam_attempt_id": "79",
                        "question_id": f"q_{idx}",
                        "is_correct": j < correct,
                        "category": domain,
                    }
                )
                idx += 1

        rows = build_verified_domain_performance(
            attempts,
            question_attempts,
            60,
            domain_weights={"Strong Domain": 30.0, "Weak Domain": 20.0},
            passing_threshold=68.0,
        )
        ranked = rank_weak_domains(rows)

        self.assertEqual(ranked[0]["Domain"], "Weak Domain")
        self.assertEqual(ranked[1]["Domain"], "Strong Domain")
        self.assertLess(ranked[1]["display_order"], ranked[0]["display_order"])

    def test_empty_domain_state_is_explicit(self):
        rows = build_verified_domain_performance([_attempt(71, 3.33)], [], 60)
        self.assertEqual(rows, [])


class TestStudyActivitySummary(unittest.TestCase):
    def test_counts_each_canonical_mode_correctly(self):
        attempts = [
            _attempt(1, 70.0, mode=PAID_MOCK_EXAM, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(2, 60.0, mode=FREE_MOCK_EXAM, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(3, 80.0, mode=PRACTICE_BY_CATEGORY, total_questions=10, completed_at="2026-06-23T12:00:00+00:00"),
            _attempt(4, 75.0, mode=WEAK_AREAS_PRACTICE, total_questions=10, completed_at="2026-06-22T12:00:00+00:00"),
            _attempt(5, 90.0, mode=DAILY_SPRINT, total_questions=10, completed_at="2026-06-21T12:00:00+00:00"),
        ]
        summary = build_study_activity_summary(
            attempts,
            window_days=30,
            reference_dt=datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(summary.completed_verified_mocks, 1)
        self.assertEqual(summary.completed_free_mocks, 1)
        self.assertEqual(summary.completed_practice_sessions, 1)
        self.assertEqual(summary.completed_weak_area_sessions, 1)
        self.assertEqual(summary.completed_daily_sprints, 1)
        self.assertEqual(summary.total_completed_activities, 5)

    def test_streak_handles_gaps_and_empty_histories(self):
        empty = build_study_activity_summary([], reference_dt=datetime(2026, 6, 25, tzinfo=timezone.utc))
        self.assertEqual(empty.current_streak_days, 0)

        attempts = [
            _attempt(1, 70.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(2, 71.0, completed_at="2026-06-23T12:00:00+00:00"),
        ]
        summary = build_study_activity_summary(
            attempts,
            window_days=30,
            reference_dt=datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(summary.current_streak_days, 1)


class TestActivityHistoryNormalization(unittest.TestCase):
    def test_preserves_canonical_mode_values(self):
        row = normalize_activity_history_row(_attempt(79, 75.0, mode=DAILY_SPRINT, total_questions=10))
        self.assertEqual(row.canonical_mode, DAILY_SPRINT)
        self.assertEqual(row.activity_type, "daily_sprint")
        self.assertFalse(row.readiness_eligible)

    def test_no_scenario_simulation_mode(self):
        import utils.learner_analytics as learner_analytics

        self.assertFalse(hasattr(learner_analytics, "SCENARIO_SIMULATION"))
        self.assertFalse(hasattr(learner_analytics, "SCENARIO_SIMULATOR"))


class TestSharedPageIntegration(unittest.TestCase):
    def test_dashboard_and_my_progress_use_shared_functions(self):
        import pages.Dashboard as dashboard
        import pages.My_Progress as my_progress

        dashboard_source = inspect.getsource(dashboard)
        progress_source = inspect.getsource(my_progress)

        shared_calls = [
            "utils.learner_analytics",
            "build_verified_domain_performance",
            "build_readiness_display_contract",
            "filter_readiness_attempts",
            "filter_question_attempts_for_attempts",
        ]
        for call in shared_calls:
            self.assertIn(call, dashboard_source)
            self.assertIn(call, progress_source)

        self.assertIn("build_verified_mock_performance", dashboard_source)
        self.assertIn("build_verified_mock_performance", progress_source)

    def test_metrics_dict_remains_compatible(self):
        attempts = [
            _attempt(79, 82.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(73, 76.0, completed_at="2026-06-24T12:00:00+00:00"),
        ]
        question_attempts = _verified_child_rows(79, score=82.0) + _verified_child_rows(73, score=76.0)
        metrics = build_verified_mock_performance_metrics(attempts, question_attempts, 60)

        self.assertTrue(metrics["has_verified_mocks"])
        self.assertEqual(metrics["verified_mock_count"], 2)
        self.assertEqual(metrics["latest_score"], 82.0)


if __name__ == "__main__":
    unittest.main()
