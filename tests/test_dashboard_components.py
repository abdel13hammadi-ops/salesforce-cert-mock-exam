"""Focused tests for dashboard presentation components."""

from __future__ import annotations

import inspect
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.activity_modes import DAILY_SPRINT, PAID_MOCK_EXAM
from utils.dashboard_components import (
    build_mock_exam_href,
    build_practice_href,
    format_score_value,
    format_trend_change,
    render_empty_state,
    status_badge_class,
    status_label_text,
)
from utils.ui_theme import COLORS, REQUIRED_TOKEN_KEYS, SPACING, validate_theme_tokens


class TestThemeTokens(unittest.TestCase):
    def test_theme_tokens_are_centralized_and_complete(self):
        self.assertTrue(validate_theme_tokens())
        for key in REQUIRED_TOKEN_KEYS:
            self.assertIn(key, COLORS)
        self.assertIn("md", SPACING)
        self.assertIn("primary_navy", COLORS)


class TestReadinessPresentation(unittest.TestCase):
    def test_locked_contract_never_displays_fabricated_score(self):
        from utils.dashboard_components import render_readiness_hero
        from utils.learner_analytics import build_readiness_display_contract

        readiness = {
            "is_locked": True,
            "eligible_mock_count": 2,
            "required_mock_count": 3,
            "mocks_remaining": 1,
            "confidence_score": 12,
            "confidence_label": "Low",
            "label": "Readiness Locked",
            "trend_label": "Stable",
            "recommendation": "Complete one more verified mock.",
        }
        contract = build_readiness_display_contract(readiness)
        self.assertIsNone(contract.readiness_score)
        source = inspect.getsource(render_readiness_hero)
        self.assertIn("Readiness locked", source)
        self.assertNotIn("0.0%", source)


class TestKpiFormatting(unittest.TestCase):
    def test_format_score_handles_missing_data(self):
        self.assertEqual(format_score_value(None), "—")
        self.assertEqual(format_score_value(82.0), "82.0%")

    def test_trend_direction_formatting(self):
        self.assertEqual(format_trend_change(4.5)["arrow"], "↑")
        self.assertEqual(format_trend_change(-2.0)["arrow"], "↓")
        self.assertEqual(format_trend_change(None)["text"], "No prior verified mock")


class TestEmptyStates(unittest.TestCase):
    def test_empty_state_does_not_display_fake_data(self):
        source = inspect.getsource(render_empty_state)
        self.assertNotIn("Sample", source)
        self.assertNotIn("74%", source)


class TestActivityHistoryPresentation(unittest.TestCase):
    def test_activity_badges_preserve_canonical_modes(self):
        from utils.dashboard_components import _activity_badge

        label, css = _activity_badge("paid_mock_exam")
        self.assertEqual(label, "Verified mock")
        label, css = _activity_badge("daily_sprint")
        self.assertEqual(label, "Daily sprint")


class TestSharedImports(unittest.TestCase):
    def test_dashboard_and_progress_import_shared_components(self):
        import pages.Dashboard as dashboard
        import pages.My_Progress as my_progress

        dashboard_source = inspect.getsource(dashboard)
        progress_source = inspect.getsource(my_progress)
        for token in (
            "utils.dashboard_components",
            "inject_certbound_theme",
            "render_readiness_hero",
            "render_verified_kpi_row",
            "render_score_trend_section",
            "render_domain_mastery_section",
            "render_study_activity_section",
            "render_weak_area_action_panel",
            "render_activity_history",
        ):
            self.assertIn(token, dashboard_source)
            self.assertIn(token, progress_source)

    def test_pages_do_not_define_duplicate_chart_builders(self):
        import pages.Dashboard as dashboard
        import pages.My_Progress as my_progress

        dashboard_source = inspect.getsource(dashboard)
        progress_source = inspect.getsource(my_progress)
        self.assertNotIn("def build_score_trend_figure", dashboard_source)
        self.assertNotIn("def build_score_trend_figure", progress_source)
        self.assertNotIn("st.line_chart", progress_source)
        self.assertNotIn("st.bar_chart", progress_source)


class TestNavigationHelpers(unittest.TestCase):
    def test_practice_href_includes_category_and_exam(self):
        href = build_practice_href("pages/Practice_By_Category.py", "ADM", "Configuration", "token")
        self.assertIn("category=Configuration", href)
        self.assertIn("exam_name=ADM", href)
        self.assertIn("fr_session=token", href)

    def test_mock_exam_href_preserves_session(self):
        self.assertEqual(build_mock_exam_href("abc"), "/?fr_session=abc")


class TestWeakAreaPanel(unittest.TestCase):
    def test_insufficient_evidence_uses_evidence_building_action(self):
        from utils.dashboard_components import render_weak_area_action_panel

        source = inspect.getsource(render_weak_area_action_panel)
        self.assertIn("Build verified mock evidence", source)
        self.assertIn("before targeting this domain", source)


class TestStatusLabels(unittest.TestCase):
    def test_status_labels_exist_for_color_and_text(self):
        self.assertEqual(status_badge_class("high_risk"), "cb-badge-danger")
        self.assertEqual(status_label_text("insufficient"), "Insufficient evidence")


if __name__ == "__main__":
    unittest.main()
