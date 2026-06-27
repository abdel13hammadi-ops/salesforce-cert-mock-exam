"""Focused tests for Weak Areas Practice domain evidence selection."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pages.Weak_Areas_Practice import (
    aggregate_domains_from_evidence,
    choose_questions,
    recommend_practice_categories,
)


def _attempt(attempt_id: int, *, mode: str = "Paid Mock Exam", domain_breakdown: dict | None = None):
    row = {
        "id": attempt_id,
        "mode": mode,
        "total_questions": 60,
        "score": 50.0,
        "correct_answers": 30,
    }
    if domain_breakdown is not None:
        row["domain_breakdown"] = domain_breakdown
    return row


def _legacy_domain_breakdown() -> dict:
    return {
        "User Stories": {"correct": 11, "total": 11},
        "Business Process Mapping": {"correct": 7, "total": 7},
        "Collaboration with Stakeholders": {"correct": 14, "total": 14},
        "Customer Discovery": {"correct": 12, "total": 12},
        "Requirements": {"correct": 11, "total": 11},
        "User Acceptance": {"correct": 7, "total": 7},
    }


def _child_rows(attempt_id: int, domain_counts: dict[str, tuple[int, int]], *, mode: str = "Paid Mock Exam"):
    rows = []
    idx = 0
    for domain, (correct, total) in domain_counts.items():
        for j in range(total):
            rows.append(
                {
                    "exam_attempt_id": str(attempt_id),
                    "question_id": f"q_{attempt_id}_{idx}",
                    "category": domain,
                    "is_correct": j < correct,
                }
            )
            idx += 1
    return rows


class TestWeakAreasPracticeEvidence(unittest.TestCase):
    def test_legacy_parent_summaries_are_ignored_without_child_rows(self):
        attempts = [
            _attempt(71, domain_breakdown=_legacy_domain_breakdown()),
            _attempt(72, domain_breakdown=_legacy_domain_breakdown()),
        ]
        rows = aggregate_domains_from_evidence(attempts, [])

        self.assertEqual(rows, [])

    def test_paid_mock_child_rows_contribute(self):
        attempts = [_attempt(73)]
        question_attempts = _child_rows(73, {"Requirements": (12, 60)})
        rows = aggregate_domains_from_evidence(attempts, question_attempts)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Requirements")
        self.assertEqual(rows[0]["correct"], 12)
        self.assertEqual(rows[0]["total"], 60)
        self.assertEqual(rows[0]["accuracy"], 20.0)

    def test_daily_sprint_and_practice_child_rows_contribute(self):
        attempts = [
            _attempt(81, mode="Daily Sprint"),
            _attempt(82, mode="Practice by Category"),
            _attempt(83, mode="Weak Areas Practice"),
            _attempt(84, mode="Free Mock Exam"),
        ]
        question_attempts = (
            _child_rows(81, {"Customer Discovery": (8, 10)})
            + _child_rows(82, {"Customer Discovery": (6, 10)})
            + _child_rows(83, {"Customer Discovery": (4, 10)})
            + _child_rows(84, {"Customer Discovery": (2, 10)})
        )
        rows = aggregate_domains_from_evidence(attempts, question_attempts)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["correct"], 20)
        self.assertEqual(rows[0]["total"], 40)
        self.assertEqual(rows[0]["accuracy"], 50.0)

    def test_accuracy_values_are_exact(self):
        attempts = [_attempt(79)]
        question_attempts = _child_rows(
            79,
            {
                "Alpha": (10, 30),
                "Beta": (20, 30),
            },
        )
        rows = aggregate_domains_from_evidence(attempts, question_attempts)
        by_name = {row["name"]: row for row in rows}

        self.assertEqual(by_name["Alpha"]["correct"], 10)
        self.assertEqual(by_name["Alpha"]["total"], 30)
        self.assertEqual(by_name["Alpha"]["accuracy"], 33.33)
        self.assertEqual(by_name["Beta"]["correct"], 20)
        self.assertEqual(by_name["Beta"]["total"], 30)
        self.assertEqual(by_name["Beta"]["accuracy"], 66.67)

    def test_domains_are_ranked_weakest_first(self):
        attempts = [
            _attempt(79),
            _attempt(81, mode="Daily Sprint"),
        ]
        question_attempts = (
            _child_rows(79, {"Strong Domain": (32, 40), "Weak Domain": (8, 20)})
            + _child_rows(81, {"Strong Domain": (0, 10), "Weak Domain": (1, 10)})
        )
        rows = aggregate_domains_from_evidence(attempts, question_attempts)

        self.assertEqual([row["name"] for row in rows], ["Weak Domain", "Strong Domain"])
        self.assertEqual(rows[0]["accuracy"], 30.0)
        self.assertEqual(rows[1]["accuracy"], 64.0)

    def test_no_evidence_fallback_uses_first_available_category(self):
        attempts = [_attempt(71, domain_breakdown=_legacy_domain_breakdown())]
        available = ["Requirements", "User Stories"]

        rows = aggregate_domains_from_evidence(attempts, [])
        recommended = recommend_practice_categories(rows, available)

        self.assertEqual(rows, [])
        self.assertEqual(recommended, ["Requirements"])

    def test_recommendations_prefer_weakest_available_domains(self):
        rows = aggregate_domains_from_evidence(
            [_attempt(79)],
            _child_rows(79, {"Weak Domain": (2, 10), "Medium Domain": (5, 10), "Strong Domain": (8, 10)}),
        )
        available = ["Weak Domain", "Medium Domain", "Strong Domain", "Unused Domain"]
        recommended = recommend_practice_categories(rows, available)

        self.assertEqual(recommended, ["Weak Domain", "Medium Domain"])

    def test_production_like_totals_ignore_parent_only_legacy(self):
        attempts = [
            _attempt(79, domain_breakdown=_legacy_domain_breakdown()),
            _attempt(75, domain_breakdown=_legacy_domain_breakdown()),
            _attempt(74, domain_breakdown=_legacy_domain_breakdown()),
            _attempt(73, domain_breakdown=_legacy_domain_breakdown()),
            _attempt(72, domain_breakdown=_legacy_domain_breakdown()),
            _attempt(71, domain_breakdown=_legacy_domain_breakdown()),
            _attempt(81, mode="Daily Sprint"),
        ]
        question_attempts = (
            _child_rows(79, {"User Stories": (2, 60)})
            + _child_rows(73, {"User Stories": (0, 60)})
            + _child_rows(81, {"User Stories": (10, 10)})
        )
        rows = aggregate_domains_from_evidence(attempts, question_attempts)

        self.assertEqual(sum(row["total"] for row in rows), 130)
        self.assertNotEqual(sum(row["total"] for row in rows), 370)

    def test_choose_questions_still_prioritizes_selected_categories(self):
        bank = [
            {"id": 1, "category": "Weak Domain", "options": [], "correct_ids": []},
            {"id": 2, "category": "Weak Domain", "options": [], "correct_ids": []},
            {"id": 3, "category": "Other Domain", "options": [], "correct_ids": []},
        ]
        selected = choose_questions(bank, ["Weak Domain"], 2)

        self.assertEqual(len(selected), 2)
        self.assertTrue(all(q["category"] == "Weak Domain" for q in selected))


if __name__ == "__main__":
    unittest.main()
