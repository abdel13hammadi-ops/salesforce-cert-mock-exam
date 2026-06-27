"""Focused tests for MVP launch-hardening slice."""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import types
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.datetime_display import DEFAULT_DISPLAY_TIMEZONE, format_user_datetime, parse_utc_datetime
from utils.user_errors import (
    EXAM_BANK_LOAD_ERROR_MESSAGE,
    PRACTICE_SAVE_ERROR_MESSAGE,
    PROGRESS_LOAD_ERROR_MESSAGE,
    log_and_get_user_message,
)
from utils.version import APP_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]

VERSION_PAGES = [
    REPO_ROOT / "app.py",
    REPO_ROOT / "pages" / "Dashboard.py",
    REPO_ROOT / "pages" / "My_Progress.py",
    REPO_ROOT / "pages" / "Practice_By_Category.py",
    REPO_ROOT / "pages" / "Weak_Areas_Practice.py",
    REPO_ROOT / "pages" / "Admin_Audit_Review.py",
    REPO_ROOT / "pages" / "Admin.py",
    REPO_ROOT / "pages" / "Account.py",
]

SOURCE_ROOTS = [REPO_ROOT / "app.py", REPO_ROOT / "pages", REPO_ROOT / "utils"]


class TestVersionLabel(unittest.TestCase):
    def test_shared_constant_value(self):
        self.assertEqual(APP_VERSION, "V45_MVP_LAUNCH_HARDENING")

    def test_stale_v43_label_absent_from_source(self):
        for root in SOURCE_ROOTS:
            paths = [root] if root.is_file() else list(root.rglob("*.py"))
            for path in paths:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(
                    "V43_ACCESS_AND_READINESS_INTEGRATION",
                    text,
                    msg=f"stale label found in {path}",
                )

    def test_rendered_pages_import_shared_constant(self):
        for path in VERSION_PAGES:
            text = path.read_text(encoding="utf-8")
            self.assertIn("from utils.version import APP_VERSION", text, msg=str(path))
            self.assertIn("APP_VERSION", text, msg=str(path))
            self.assertNotIn('"V45_MVP_LAUNCH_HARDENING"', text, msg=str(path))
            self.assertNotIn("'V45_MVP_LAUNCH_HARDENING'", text, msg=str(path))


class TestDatetimeDisplay(unittest.TestCase):
    def test_utc_converts_to_new_york(self):
        formatted = format_user_datetime("2026-01-15T18:30:00+00:00")
        self.assertIn("Jan 15, 2026", formatted)
        self.assertIn("1:30 PM", formatted)
        self.assertTrue(formatted.endswith("EST") or formatted.endswith("EDT"))

    def test_daylight_saving_conversion(self):
        winter = format_user_datetime("2026-01-15T18:30:00+00:00")
        summer = format_user_datetime("2026-07-15T18:30:00+00:00")
        self.assertIn("EST", winter)
        self.assertIn("EDT", summer)

    def test_naive_timestamp_treated_as_utc(self):
        parsed = parse_utc_datetime("2026-06-15T12:00:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_null_and_malformed_values_do_not_crash(self):
        self.assertEqual(format_user_datetime(None), "Not recorded")
        self.assertEqual(format_user_datetime(""), "Not recorded")
        self.assertEqual(format_user_datetime("not-a-date"), "Not recorded")
        self.assertIsNone(parse_utc_datetime("bad"))

    def test_default_timezone_constant(self):
        self.assertEqual(DEFAULT_DISPLAY_TIMEZONE, "America/New_York")


class TestSanitizedErrors(unittest.TestCase):
    def test_log_and_get_user_message_returns_safe_text(self):
        msg = log_and_get_user_message("test context", PROGRESS_LOAD_ERROR_MESSAGE)
        self.assertEqual(msg, PROGRESS_LOAD_ERROR_MESSAGE)

    def test_safe_messages_exclude_technical_tokens(self):
        for message in (
            PROGRESS_LOAD_ERROR_MESSAGE,
            PRACTICE_SAVE_ERROR_MESSAGE,
            EXAM_BANK_LOAD_ERROR_MESSAGE,
        ):
            lowered = message.lower()
            self.assertNotIn("traceback", lowered)
            self.assertNotIn("sqlstate", lowered)
            self.assertNotIn("supabase_service_role_key", lowered)
            self.assertNotIn("\\", message)

    def test_practice_page_does_not_embed_exception_in_source(self):
        text = (REPO_ROOT / "pages" / "Practice_By_Category.py").read_text(encoding="utf-8")
        self.assertIn("PRACTICE_SAVE_ERROR_MESSAGE", text)
        self.assertNotIn("saving to progress tracking failed: {exc}", text)

    def test_weak_areas_page_does_not_embed_exception_in_source(self):
        text = (REPO_ROOT / "pages" / "Weak_Areas_Practice.py").read_text(encoding="utf-8")
        self.assertIn("PRACTICE_SAVE_ERROR_MESSAGE", text)
        self.assertNotIn("saving to Supabase failed: {exc}", text)

    def test_my_progress_does_not_render_raw_query_error(self):
        text = (REPO_ROOT / "pages" / "My_Progress.py").read_text(encoding="utf-8")
        self.assertIn("PROGRESS_LOAD_ERROR_MESSAGE", text)
        self.assertNotIn("st.error(query_error)", text)


class TestDuplicateSubmissionGuards(unittest.TestCase):
    def test_app_submission_state_machine_present(self):
        text = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("submission_save_state", text)
        self.assertIn("current_exam_attempt_id", text)

    def test_practice_saved_guard_present(self):
        text = (REPO_ROOT / "pages" / "Practice_By_Category.py").read_text(encoding="utf-8")
        self.assertIn("practice_saved", text)
        self.assertIn('if not st.session_state.get("practice_saved", False)', text)

    def test_weak_saved_guard_present(self):
        text = (REPO_ROOT / "pages" / "Weak_Areas_Practice.py").read_text(encoding="utf-8")
        self.assertIn("weak_saved", text)
        self.assertIn('if not st.session_state.get("weak_saved", False)', text)

    def test_existing_duplicate_parent_tests_still_present(self):
        path = REPO_ROOT / "tests" / "test_exam_attempt_tracking.py"
        text = path.read_text(encoding="utf-8")
        self.assertIn("test_duplicate_parent_backfills_without_duplicates", text)
        self.assertIn("test_no_duplicate_pairs_after_retry", text)

    def test_practice_refresh_recovery_tests_still_present(self):
        path = REPO_ROOT / "tests" / "test_practice_session_persistence.py"
        text = path.read_text(encoding="utf-8")
        self.assertIn("test_refresh_restores_exact_question_ids_order_and_options", text)


class TestDocumentationSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.regression = (REPO_ROOT / "docs" / "MVP_PRODUCTION_REGRESSION.md").read_text(encoding="utf-8")
        cls.runbook = (REPO_ROOT / "docs" / "BACKGROUND_WORKER_RUNBOOK.md").read_text(encoding="utf-8")

    def test_regression_doc_has_twenty_checks(self):
        checks = re.findall(r"^## \d+\.", self.regression, flags=re.MULTILINE)
        self.assertEqual(len(checks), 20)
        for field in ("Setup", "Action", "Expected", "Evidence", "Pass/Fail"):
            self.assertIn(f"**{field}**", self.regression)

    def test_docs_contain_no_secret_placeholders(self):
        combined = self.regression + self.runbook
        self.assertNotIn('SUPABASE_SERVICE_ROLE_KEY = "', combined)
        self.assertNotIn("sk-", combined)

    def test_runbook_covers_safe_secret_handling_and_cleanup(self):
        self.assertIn("Read-Host", self.runbook)
        self.assertIn("Remove-Item Env:SUPABASE_SERVICE_ROLE_KEY", self.runbook)
        self.assertIn("--once", self.runbook)
        self.assertIn("deterministic_audit", self.runbook)
        self.assertIn("do **not** need LLM", self.runbook)


class TestDashboardUsesSharedDatetimeHelper(unittest.TestCase):
    def test_dashboard_imports_shared_formatter(self):
        text = (REPO_ROOT / "pages" / "Dashboard.py").read_text(encoding="utf-8")
        self.assertIn("from utils.datetime_display import", text)
        self.assertNotIn("def format_user_datetime", text)


if __name__ == "__main__":
    unittest.main()
