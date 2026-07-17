"""Regression tests for read-only containment on Admin Question Review."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = REPO_ROOT / "pages" / "Admin_Question_Review.py"
ACCESS_CONTROL_PATH = REPO_ROOT / "utils" / "access_control.py"
NAVIGATION_PATH = REPO_ROOT / "utils" / "navigation.py"


class TestAdminQuestionReviewReadOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PAGE_PATH.read_text(encoding="utf-8")
        cls.access_control_source = ACCESS_CONTROL_PATH.read_text(encoding="utf-8")

    def test_page_requires_admin(self):
        self.assertIn("require_admin()", self.source)

    def test_read_only_notice_present(self):
        self.assertIn("READ_ONLY_CONTAINMENT_NOTICE", self.source)
        self.assertIn("Live question editing is disabled on this page.", self.source)
        self.assertIn("Admin Audit Review", self.source)

    def test_page_remains_readable(self):
        for token in (
            "load_questions",
            ".select(",
            "Search and Filter",
            "Search question ID",
            "Search question text",
            "Choose certification/question bank to review",
            "Current Question Preview",
            "Current Answer Options",
            "Explanation and Metadata",
            "quality_status",
            "is_exam_eligible",
        ):
            self.assertIn(token, self.source, msg=f"missing readable surface: {token}")

    def test_no_direct_write_methods(self):
        banned_patterns = (
            ".update(",
            ".insert(",
            ".delete(",
            ".upsert(",
            "update_question",
            "update_answer_option",
        )
        for pattern in banned_patterns:
            self.assertNotIn(pattern, self.source, msg=f"write path still present: {pattern}")

    def test_no_save_or_delete_controls(self):
        banned_ui = (
            "form_submit_button",
            "st.form(",
            "Save Question Text",
            "Save Answer Options",
            "Fast Quality Actions",
            "Approve + Make Exam Eligible",
            "Needs Edit + Remove From Exam",
            "Reject + Deactivate",
            "Edit Question, Explanation, and Metadata",
            "Edit Answer Options",
        )
        for token in banned_ui:
            self.assertNotIn(token, self.source, msg=f"editable control still present: {token}")

    def test_no_action_buttons_for_writes(self):
        self.assertNotIn("st.button(", self.source)

    def test_audit_review_remains_in_navigation(self):
        # Navigation definitions were centralized into utils/navigation.py; the
        # route must still be registered there with a discoverable label.
        navigation_source = NAVIGATION_PATH.read_text(encoding="utf-8")
        self.assertIn("pages/Admin_Audit_Review.py", navigation_source)
        self.assertIn("Audit Review", navigation_source)

    def test_page_imports_under_mock_streamlit(self):
        import utils.access_control  # noqa: F401 — ensure patch target is resolvable

        fake_st = types.SimpleNamespace(
            set_page_config=lambda *args, **kwargs: None,
            title=lambda *args, **kwargs: None,
            caption=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            selectbox=lambda *args, **kwargs: args[1][0] if len(args) > 1 and args[1] else 0,
            text_input=lambda *args, **kwargs: "",
            write=lambda *args, **kwargs: None,
            markdown=lambda *args, **kwargs: None,
            subheader=lambda *args, **kwargs: None,
            header=lambda *args, **kwargs: None,
            divider=lambda: None,
            metric=lambda *args, **kwargs: None,
            columns=lambda n: [MagicMock() for _ in range(n)],
            expander=lambda *args, **kwargs: MagicMock(
                __enter__=lambda self: self,
                __exit__=lambda self, exc_type, exc, tb: False,
            ),
            dataframe=lambda *args, **kwargs: None,
            code=lambda *args, **kwargs: None,
            stop=lambda: (_ for _ in ()).throw(SystemExit()),
            cache_data=lambda **kwargs: (lambda fn: fn),
        )

        fake_client = MagicMock()
        fake_client.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value.data = []
        fake_client.table.return_value.select.return_value.order.return_value.execute.return_value.data = []

        with patch.dict(sys.modules, {"streamlit": fake_st}):
            with patch("utils.access_control.get_supabase_admin_client", return_value=fake_client), \
                 patch("utils.access_control.render_app_chrome"), \
                 patch("utils.access_control.require_admin", return_value="admin@test.com"), \
                 patch("utils.session_timeout.enforce_session_timeout"), \
                 patch("utils.session_timeout.show_session_expired_notice"), \
                 patch("utils.version.APP_VERSION", "test"):
                spec = importlib.util.spec_from_file_location(
                    "admin_question_review_page",
                    PAGE_PATH,
                )
                module = importlib.util.module_from_spec(spec)
                with self.assertRaises(SystemExit):
                    spec.loader.exec_module(module)
                self.assertEqual(module.READ_ONLY_CONTAINMENT_NOTICE, (
                    "Live question editing is disabled on this page. "
                    "All future content changes must use immutable question versions "
                    "and the governed audit/publication workflow. "
                    "Use Admin Audit Review to inspect existing audit findings."
                ))


if __name__ == "__main__":
    unittest.main()
