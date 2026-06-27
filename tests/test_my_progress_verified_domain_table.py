"""Focused tests for verified Weak Areas by Domain in My Progress."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pages.My_Progress import build_attempt_history_rows
from utils.readiness import (
    build_verified_domain_table_rows,
    filter_verified_question_attempts,
    select_weakest_verified_domain,
)


def _attempt(
    attempt_id: int,
    score: float,
    *,
    mode: str = "Paid Mock Exam",
    total_questions: int = 60,
    completed_at: str = "2026-06-24T12:00:00+00:00",
    domain_breakdown: dict | None = None,
):
    correct_answers = int(round(score * total_questions / 100.0))
    row = {
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
    if domain_breakdown is not None:
        row["domain_breakdown"] = domain_breakdown
    return row


def _child_rows_for_domains(
    attempt_id: int,
    domain_counts: dict[str, tuple[int, int]],
):
    rows = []
    idx = 0
    for domain, (correct, total) in domain_counts.items():
        for j in range(total):
            rows.append(
                {
                    "id": attempt_id * 10000 + idx,
                    "exam_attempt_id": str(attempt_id),
                    "question_id": f"q_{attempt_id}_{idx}",
                    "is_correct": j < correct,
                    "category": domain,
                    "difficulty": "medium",
                    "cognitive_level": "application",
                }
            )
            idx += 1
    return rows


def _legacy_domain_breakdown(total_questions: int = 60) -> dict:
    return {
        "User Stories": {"correct": 11, "total": 11},
        "Business Process Mapping": {"correct": 7, "total": 7},
        "Collaboration with Stakeholders": {"correct": 14, "total": 14},
        "Customer Discovery": {"correct": 12, "total": 12},
        "Requirements": {"correct": 11, "total": 11},
        "User Acceptance": {"correct": 7, "total": 7},
    }


class TestVerifiedDomainTable(unittest.TestCase):
    def test_only_verified_child_rows_contribute(self):
        attempts = [
            _attempt(79, 50.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(73, 40.0, completed_at="2026-06-24T12:00:00+00:00"),
        ]
        question_attempts = (
            _child_rows_for_domains(79, {"Domain A": (30, 60)})
            + _child_rows_for_domains(73, {"Domain A": (24, 60)})
        )
        rows = build_verified_domain_table_rows(attempts, question_attempts, 60)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Domain"], "Domain A")
        self.assertEqual(rows[0]["Correct"], 54)
        self.assertEqual(rows[0]["Total"], 120)
        self.assertEqual(rows[0]["Accuracy %"], 45.0)

    def test_legacy_parent_domain_breakdown_is_ignored(self):
        attempts = [
            _attempt(
                71,
                3.33,
                completed_at="2026-06-21T12:00:00+00:00",
                domain_breakdown=_legacy_domain_breakdown(),
            ),
            _attempt(
                72,
                3.33,
                completed_at="2026-06-20T12:00:00+00:00",
                domain_breakdown=_legacy_domain_breakdown(),
            ),
            _attempt(73, 0.0, completed_at="2026-06-22T12:00:00+00:00"),
            _attempt(79, 3.33, completed_at="2026-06-25T12:00:00+00:00"),
        ]
        question_attempts = (
            _child_rows_for_domains(73, {"Requirements": (0, 60)})
            + _child_rows_for_domains(79, {"Requirements": (2, 60)})
        )
        rows = build_verified_domain_table_rows(attempts, question_attempts, 60)

        self.assertEqual(sum(row["Total"] for row in rows), 120)
        self.assertNotEqual(sum(row["Total"] for row in rows), 370)

    def test_daily_sprint_and_practice_rows_are_excluded(self):
        attempts = [
            _attempt(79, 75.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(81, 100.0, mode="Daily Sprint", total_questions=10, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(82, 90.0, mode="Practice by Category", total_questions=10, completed_at="2026-06-23T12:00:00+00:00"),
            _attempt(83, 80.0, mode="Weak Areas Practice", total_questions=10, completed_at="2026-06-22T12:00:00+00:00"),
            _attempt(84, 70.0, mode="Free Mock Exam", total_questions=60, completed_at="2026-06-21T12:00:00+00:00"),
        ]
        question_attempts = (
            _child_rows_for_domains(79, {"Customer Discovery": (45, 60)})
            + _child_rows_for_domains(81, {"Customer Discovery": (10, 10)})
            + _child_rows_for_domains(82, {"Customer Discovery": (9, 10)})
            + _child_rows_for_domains(83, {"Customer Discovery": (8, 10)})
            + _child_rows_for_domains(84, {"Customer Discovery": (42, 60)})
        )
        rows = build_verified_domain_table_rows(attempts, question_attempts, 60)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Total"], 60)
        self.assertEqual(rows[0]["Correct"], 45)

    def test_table_chart_and_weakest_use_same_filtered_dataset(self):
        attempts = [_attempt(79, 66.67)]
        question_attempts = _child_rows_for_domains(
            79,
            {
                "Strong Domain": (32, 40),
                "Weak Domain": (8, 20),
            },
        )
        rows = build_verified_domain_table_rows(attempts, question_attempts, 60)
        weakest = select_weakest_verified_domain(rows)

        self.assertEqual([row["Domain"] for row in rows], ["Weak Domain", "Strong Domain"])
        self.assertEqual([row["Accuracy %"] for row in rows], [40.0, 80.0])
        self.assertIsNotNone(weakest)
        assert weakest is not None
        self.assertEqual(weakest["Domain"], rows[0]["Domain"])
        self.assertEqual(weakest["Accuracy %"], rows[0]["Accuracy %"])

    def test_accuracy_calculations_are_exact(self):
        attempts = [_attempt(79, 50.0)]
        question_attempts = _child_rows_for_domains(
            79,
            {
                "Alpha": (10, 30),
                "Beta": (20, 30),
            },
        )
        rows = build_verified_domain_table_rows(attempts, question_attempts, 60)

        by_domain = {row["Domain"]: row for row in rows}
        self.assertEqual(by_domain["Alpha"]["Correct"], 10)
        self.assertEqual(by_domain["Alpha"]["Total"], 30)
        self.assertEqual(by_domain["Alpha"]["Accuracy %"], 33.33)
        self.assertEqual(by_domain["Beta"]["Correct"], 20)
        self.assertEqual(by_domain["Beta"]["Total"], 30)
        self.assertEqual(by_domain["Beta"]["Accuracy %"], 66.67)

    def test_empty_state_when_no_verified_evidence(self):
        attempts = [
            _attempt(71, 3.33, domain_breakdown=_legacy_domain_breakdown()),
            _attempt(81, 100.0, mode="Daily Sprint", total_questions=10),
        ]
        rows = build_verified_domain_table_rows(attempts, [], 60)

        self.assertEqual(rows, [])
        self.assertIsNone(select_weakest_verified_domain(rows))

    def test_filter_verified_question_attempts_matches_table_totals(self):
        attempts = [
            _attempt(79, 50.0),
            _attempt(81, 100.0, mode="Daily Sprint", total_questions=10),
        ]
        question_attempts = (
            _child_rows_for_domains(79, {"Domain A": (30, 60)})
            + _child_rows_for_domains(81, {"Domain A": (10, 10)})
        )
        filtered = filter_verified_question_attempts(attempts, question_attempts, 60)
        rows = build_verified_domain_table_rows(attempts, question_attempts, 60)

        self.assertEqual(len(filtered), 60)
        self.assertEqual(rows[0]["Total"], 60)

    def test_production_like_fixture_totals_are_verified_only(self):
        attempts = [
            _attempt(79, 3.33, completed_at="2026-06-25T12:00:00+00:00", domain_breakdown=_legacy_domain_breakdown()),
            _attempt(75, 3.33, completed_at="2026-06-24T12:00:00+00:00", domain_breakdown=_legacy_domain_breakdown()),
            _attempt(74, 3.33, completed_at="2026-06-23T12:00:00+00:00", domain_breakdown=_legacy_domain_breakdown()),
            _attempt(73, 0.0, completed_at="2026-06-22T12:00:00+00:00", domain_breakdown=_legacy_domain_breakdown()),
            _attempt(72, 3.33, completed_at="2026-06-21T12:00:00+00:00", domain_breakdown=_legacy_domain_breakdown()),
            _attempt(71, 3.33, completed_at="2026-06-20T12:00:00+00:00", domain_breakdown=_legacy_domain_breakdown()),
            _attempt(81, 100.0, mode="Daily Sprint", total_questions=10, completed_at="2026-06-26T12:00:00+00:00"),
        ]
        question_attempts = (
            _child_rows_for_domains(79, {"User Stories": (2, 60)})
            + _child_rows_for_domains(73, {"User Stories": (0, 60)})
            + _child_rows_for_domains(81, {"User Stories": (10, 10)})
        )
        rows = build_verified_domain_table_rows(attempts, question_attempts, 60)

        self.assertEqual(sum(row["Total"] for row in rows), 120)
        self.assertNotEqual(sum(row["Total"] for row in rows), 370)

    def test_attempt_history_remains_unfiltered(self):
        attempts = [
            _attempt(79, 75.0, completed_at="2026-06-25T12:00:00+00:00"),
            _attempt(81, 100.0, mode="Daily Sprint", total_questions=10, completed_at="2026-06-24T12:00:00+00:00"),
            _attempt(71, 3.33, completed_at="2026-06-23T12:00:00+00:00"),
        ]
        history = build_attempt_history_rows(attempts, "UTC")

        self.assertEqual(len(history), 3)
        self.assertEqual([row["Attempt ID"] for row in history], [79, 81, 71])


if __name__ == "__main__":
    unittest.main()
