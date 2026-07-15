"""Focused tests for dashboard chart builders."""

from __future__ import annotations

import inspect
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.dashboard_charts import (
    build_domain_mastery_figure,
    build_score_trend_figure,
    build_study_activity_figure,
    chart_summary_score_trend,
)
from utils.learner_analytics import ScoreTrendPoint, StudyActivitySummary


class TestScoreTrendChart(unittest.TestCase):
    def test_score_chart_uses_verified_series_exactly(self):
        series = (
            ScoreTrendPoint(1, datetime(2026, 6, 20, tzinfo=timezone.utc), 70.0, 1, 68.0),
            ScoreTrendPoint(2, datetime(2026, 6, 22, tzinfo=timezone.utc), 76.0, 2, 68.0),
        )
        fig = build_score_trend_figure(series, passing_threshold=68.0, average_score=73.0)
        self.assertIsNotNone(fig)
        y = list(fig.data[0].y)
        self.assertEqual(y, [70.0, 76.0])
        self.assertTrue(any("Passing" in str(shape) or shape.y0 == 68.0 or getattr(shape, "y", None) == 68.0 for shape in fig.layout.shapes))

    def test_passing_threshold_summary_appears_when_supplied(self):
        series = (
            ScoreTrendPoint(1, datetime(2026, 6, 20, tzinfo=timezone.utc), 70.0, 1, 68.0),
        )
        summary = chart_summary_score_trend(series, 68.0)
        self.assertIn("Passing threshold is 68%", summary)


class TestDomainMasteryChart(unittest.TestCase):
    def test_domain_chart_distinguishes_insufficient_evidence(self):
        rows = [
            {
                "Domain": "Weak Domain",
                "Accuracy %": 54.0,
                "Correct": 27,
                "Total": 50,
                "exam_weight": 22.0,
                "has_sufficient_evidence": True,
                "status": "below_target",
            },
            {
                "Domain": "Sparse Domain",
                "Accuracy %": 0.0,
                "Correct": 1,
                "Total": 2,
                "exam_weight": 10.0,
                "has_sufficient_evidence": False,
                "status": "high_risk",
            },
        ]
        fig = build_domain_mastery_figure(rows)
        self.assertIsNotNone(fig)
        self.assertIn("Insufficient evidence", fig.data[0].text[1])

    def test_domain_ordering_follows_priority_rows(self):
        rows = [
            {"Domain": "Weak", "Accuracy %": 40.0, "Correct": 4, "Total": 10, "exam_weight": 20, "has_sufficient_evidence": True, "status": "high_risk"},
            {"Domain": "Strong", "Accuracy %": 85.0, "Correct": 17, "Total": 20, "exam_weight": 30, "has_sufficient_evidence": True, "status": "strong"},
        ]
        fig = build_domain_mastery_figure(rows)
        self.assertEqual(list(fig.data[0].y), ["Weak", "Strong"])


class TestStudyActivityChart(unittest.TestCase):
    def test_study_activity_chart_uses_daily_counts(self):
        fig = build_study_activity_figure((("2026-06-20", 1), ("2026-06-21", 3)), window_days=7)
        self.assertIsNotNone(fig)
        self.assertEqual(list(fig.data[0].x), ["2026-06-20", "2026-06-21"])
        self.assertEqual(list(fig.data[0].y), [1, 3])


class TestChartModuleBoundaries(unittest.TestCase):
    def test_chart_helpers_perform_no_database_access(self):
        import utils.dashboard_charts as charts

        source = inspect.getsource(charts)
        self.assertNotIn("supabase", source.lower())
        self.assertNotIn("get_supabase_client", source)
        self.assertNotIn("streamlit", source.lower())


if __name__ == "__main__":
    unittest.main()
