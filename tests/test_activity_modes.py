"""Focused tests for canonical learner activity-mode identities."""

from __future__ import annotations

import unittest

from utils.activity_modes import (
    ALL_ACTIVITY_MODES,
    DAILY_SPRINT,
    FREE_MOCK_EXAM,
    PAID_MOCK_EXAM,
    PRACTICE_BY_CATEGORY,
    READINESS_ELIGIBLE_MODES,
    WEAK_AREA_EVIDENCE_MODES,
    WEAK_AREAS_PRACTICE,
    is_readiness_eligible_mode,
)


class TestActivityModeConstants(unittest.TestCase):
    def test_canonical_string_values_match_historical_persistence(self):
        self.assertEqual(PAID_MOCK_EXAM, "Paid Mock Exam")
        self.assertEqual(FREE_MOCK_EXAM, "Free Mock Exam")
        self.assertEqual(PRACTICE_BY_CATEGORY, "Practice by Category")
        self.assertEqual(WEAK_AREAS_PRACTICE, "Weak Areas Practice")
        self.assertEqual(DAILY_SPRINT, "Daily Sprint")

    def test_all_canonical_values_are_unique(self):
        self.assertEqual(len(set(ALL_ACTIVITY_MODES)), len(ALL_ACTIVITY_MODES))

    def test_all_modes_collection_contains_exactly_five_modes(self):
        self.assertEqual(len(ALL_ACTIVITY_MODES), 5)
        self.assertEqual(
            ALL_ACTIVITY_MODES,
            (
                PAID_MOCK_EXAM,
                FREE_MOCK_EXAM,
                PRACTICE_BY_CATEGORY,
                WEAK_AREAS_PRACTICE,
                DAILY_SPRINT,
            ),
        )

    def test_readiness_eligible_modes_unchanged(self):
        self.assertEqual(READINESS_ELIGIBLE_MODES, frozenset({PAID_MOCK_EXAM}))
        self.assertTrue(is_readiness_eligible_mode(PAID_MOCK_EXAM))
        self.assertTrue(is_readiness_eligible_mode("paid mock exam"))

    def test_weak_area_evidence_modes_unchanged(self):
        self.assertEqual(
            WEAK_AREA_EVIDENCE_MODES,
            frozenset({
                PAID_MOCK_EXAM,
                DAILY_SPRINT,
                PRACTICE_BY_CATEGORY,
                WEAK_AREAS_PRACTICE,
                FREE_MOCK_EXAM,
            }),
        )

    def test_free_mock_exam_excluded_from_readiness_eligible_modes(self):
        self.assertNotIn(FREE_MOCK_EXAM, READINESS_ELIGIBLE_MODES)
        self.assertFalse(is_readiness_eligible_mode(FREE_MOCK_EXAM))

    def test_daily_sprint_classified_as_weak_area_evidence_only(self):
        self.assertIn(DAILY_SPRINT, WEAK_AREA_EVIDENCE_MODES)
        self.assertNotIn(DAILY_SPRINT, READINESS_ELIGIBLE_MODES)
        self.assertFalse(is_readiness_eligible_mode(DAILY_SPRINT))

    def test_persisted_values_require_no_transformation(self):
        for mode in ALL_ACTIVITY_MODES:
            self.assertEqual(mode, str(mode).strip())

    def test_no_scenario_simulation_constant_yet(self):
        module_names = {
            "SCENARIO_SIMULATION",
            "SCENARIO_SIMULATOR",
        }
        import utils.activity_modes as activity_modes

        for name in module_names:
            self.assertFalse(hasattr(activity_modes, name))


if __name__ == "__main__":
    unittest.main()
