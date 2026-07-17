"""Focused tests for learner activity presentation components."""

from __future__ import annotations

import html
import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.activity_charts import breakdown_chart_caption, build_breakdown_figure
from utils.activity_components import (
    activity_css,
    format_breakdown_rows,
    inject_activity_theme,
    render_activity_empty_state,
    render_activity_progress,
    render_feedback_panel,
    render_locked_preview_panel,
    render_question_stem,
    render_result_hero,
    render_save_status,
)
from utils.ui_theme import COLORS, validate_theme_tokens

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestActivityTheme(unittest.TestCase):
    def test_activity_css_uses_centralized_theme_tokens(self):
        self.assertTrue(validate_theme_tokens())
        css = activity_css()
        self.assertIn(COLORS["primary_navy"], css)
        self.assertIn(COLORS["border"], css)
        self.assertIn(COLORS["surface"], css)

    def test_inject_activity_theme_layers_shared_css(self):
        source = inspect.getsource(inject_activity_theme)
        self.assertIn("theme_css", source)
        self.assertIn("activity_css", source)


class TestLockedPreviewSecurity(unittest.TestCase):
    def test_locked_preview_does_not_embed_premium_question_bank(self):
        source = inspect.getsource(render_locked_preview_panel)
        self.assertIn("sample data only", source.lower())
        self.assertNotIn("fetch_question_bank", source)
        self.assertNotIn("correct_ids", source)


class TestEscapingAndLayout(unittest.TestCase):
    def test_question_stem_escapes_html(self):
        calls: list[str] = []

        def fake_markdown(value, **_kwargs):
            calls.append(value)

        with patch("utils.activity_components.st.markdown", side_effect=fake_markdown):
            render_question_stem('<script>alert("x")</script>')

        self.assertEqual(len(calls), 1)
        self.assertNotIn("<script>", calls[0])
        self.assertIn(html.escape('<script>alert("x")</script>'), calls[0])

    def test_long_content_uses_responsive_wrapping(self):
        css = activity_css()
        self.assertIn("overflow-wrap", css)
        self.assertIn("max-width: 100%", css)
        self.assertIn("max-width: 72ch", css)


class TestProgressSafety(unittest.TestCase):
    def test_progress_values_are_bounded(self):
        calls: list[float] = []

        def fake_progress(value):
            calls.append(value)

        with patch("utils.activity_components.st.progress", side_effect=fake_progress):
            with patch("utils.activity_components.st.markdown"):
                render_activity_progress(-5, 0)
                render_activity_progress(3, 2)

        self.assertEqual(calls[0], 0.0)
        self.assertEqual(calls[1], 1.0)


class TestResultPlaceholders(unittest.TestCase):
    def test_missing_result_values_render_truthful_placeholders(self):
        calls: list[str] = []

        def fake_markdown(value, **_kwargs):
            calls.append(value)

        with patch("utils.activity_components.st.markdown", side_effect=fake_markdown):
            render_result_hero(
                title="Practice Results",
                score=None,
                correct=None,
                total=None,
                passing_score=None,
                passed=None,
            )

        rendered = calls[0]
        self.assertIn("—", rendered)
        self.assertNotIn("0.0%", rendered)


class TestStatusLabels(unittest.TestCase):
    def test_pass_fail_status_includes_text_labels(self):
        calls: list[str] = []

        def fake_markdown(value, **_kwargs):
            calls.append(value)

        with patch("utils.activity_components.st.markdown", side_effect=fake_markdown):
            render_result_hero(title="Exam Results", score=80, correct=48, total=60, passing_score=68, passed=True)
            render_result_hero(title="Exam Results", score=50, correct=30, total=60, passing_score=68, passed=False)

        self.assertIn("Status: Pass", calls[0])
        self.assertIn("Status: Fail", calls[1])

    def test_feedback_status_includes_text_labels(self):
        calls: list[str] = []

        def fake_markdown(value, **_kwargs):
            calls.append(value)

        with patch("utils.activity_components.st.markdown", side_effect=fake_markdown):
            render_feedback_panel(
                is_correct_answer=True,
                learner_answer="A",
                correct_answer="A",
                explanation="Because.",
            )
            render_feedback_panel(
                is_correct_answer=False,
                learner_answer="B",
                correct_answer="A",
                explanation="Because.",
            )

        self.assertIn("Result: Correct", calls[0])
        self.assertIn("Result: Incorrect", calls[1])


class TestSaveFailurePresentation(unittest.TestCase):
    def test_save_failure_remains_visible_and_actionable(self):
        source = inspect.getsource(render_save_status)
        self.assertIn("Save failed", source)
        self.assertIn("st.warning", source)


class TestPresentationPurity(unittest.TestCase):
    def test_shared_components_perform_no_database_queries(self):
        modules = (
            "utils.activity_components",
            "utils.activity_charts",
        )
        forbidden = ("supabase", "create_client", "fetch_", "select(", "insert(", "update(")
        for module_name in modules:
            module = __import__(module_name, fromlist=["*"])
            source = inspect.getsource(module)
            for token in forbidden:
                self.assertNotIn(token, source, msg=f"{token} found in {module_name}")

    def test_presentation_helpers_do_not_calculate_exam_scores(self):
        forbidden = ("is_correct(", "compute_score", "passing_score =", "score = round")
        for module_name in ("utils.activity_components", "utils.activity_charts"):
            module = __import__(module_name, fromlist=["*"])
            source = inspect.getsource(module)
            for token in forbidden:
                self.assertNotIn(token, source, msg=f"{token} found in {module_name}")
        rows = format_breakdown_rows({"Security": {"correct": 2, "total": 4, "percent": 50.0}})
        self.assertEqual(rows[0]["percent"], 50.0)

    def test_presentation_helpers_do_not_mutate_session_state(self):
        for fn_name in (
            "render_activity_header",
            "render_activity_launch_card",
            "render_activity_progress",
            "render_question_stem",
            "render_feedback_panel",
            "render_result_hero",
            "render_save_status",
            "render_locked_preview_panel",
            "render_activity_empty_state",
        ):
            from utils import activity_components as mod

            source = inspect.getsource(getattr(mod, fn_name))
            self.assertNotIn("session_state", source, msg=fn_name)


class TestSharedImports(unittest.TestCase):
    def test_activity_pages_import_shared_components(self):
        # Static source inspection only. Do not import these page modules for
        # real: each is a Streamlit script whose top-level body calls
        # Supabase-backed helpers on import, and the first access to
        # st.secrets anywhere in the process copies every key in
        # .streamlit/secrets.toml into os.environ for the rest of the pytest
        # run, silently poisoning unrelated tests that rely on injected
        # secrets_getter values (e.g. Stripe portal-return-URL tests).
        for page_path in ("app.py", "pages/Practice_By_Category.py", "pages/Weak_Areas_Practice.py"):
            source = (REPO_ROOT / page_path).read_text(encoding="utf-8")
            self.assertIn("utils.activity_components", source)
            self.assertIn("inject_activity_theme", source)


class TestBreakdownCharts(unittest.TestCase):
    def test_missing_breakdown_data_returns_truthful_empty_caption(self):
        self.assertIn("breakdown data available", breakdown_chart_caption(0, kind="domain").lower())
        self.assertIsNone(build_breakdown_figure([]))

    def test_breakdown_chart_uses_precomputed_rows_only(self):
        rows = [{"label": "Security", "correct": 3, "total": 5, "percent": 60.0}]
        fig = build_breakdown_figure(rows, title="By Domain")
        if fig is not None:
            self.assertEqual(fig.data[0].x[0], 60.0)


class TestEmptyState(unittest.TestCase):
    def test_empty_state_renders_without_fake_metrics(self):
        calls: list[str] = []

        def fake_markdown(value, **_kwargs):
            calls.append(value)

        with patch("utils.activity_components.st.markdown", side_effect=fake_markdown):
            render_activity_empty_state("No domain breakdown", "Domain performance data is not available.")

        self.assertIn("No domain breakdown", calls[0])
        self.assertNotIn("0%", calls[0])


if __name__ == "__main__":
    unittest.main()
