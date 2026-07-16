"""Focused tests for secondary-page presentation components."""

from __future__ import annotations

import html
import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.billing_stripe import PORTAL_MANAGE_LABEL, render_portal_session_link_markdown
from utils.legal_policy_pages import TERMS_HEADINGS, render_terms_content
from utils.secondary_components import (
    PREMIUM_BENEFITS,
    format_placeholder,
    inject_secondary_theme,
    render_access_status_pill,
    render_secondary_section,
    render_subscription_plan_summary,
    secondary_css,
)
from utils.ui_theme import COLORS, validate_theme_tokens

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestSecondaryTheme(unittest.TestCase):
    def test_secondary_css_uses_centralized_theme_tokens(self):
        self.assertTrue(validate_theme_tokens())
        css = secondary_css()
        self.assertIn(COLORS["primary_navy"], css)
        self.assertIn(COLORS["border"], css)


class TestAuthenticationPresentation(unittest.TestCase):
    def test_auth_errors_are_escaped_in_section_rendering(self):
        calls: list[str] = []

        def fake_markdown(value, **_kwargs):
            calls.append(value)

        with patch("utils.secondary_components.st.markdown", side_effect=fake_markdown):
            render_secondary_section(kicker="Auth", title='<script>alert(1)</script>', body="safe")

        self.assertNotIn("<script>", calls[0])
        self.assertIn(html.escape('<script>alert(1)</script>'), calls[0])

    def test_password_reset_page_does_not_render_tokens(self):
        source = (REPO_ROOT / "pages" / "Reset_Password.py").read_text(encoding="utf-8")
        self.assertNotIn('st.write("access_token', source)
        self.assertNotIn("st.write(access_token", source)
        self.assertNotIn('st.code(access_token', source)


class TestPlaceholders(unittest.TestCase):
    def test_missing_account_values_use_truthful_placeholders(self):
        self.assertEqual(format_placeholder(None), "—")
        self.assertEqual(format_placeholder(""), "—")
        self.assertEqual(format_placeholder("learner@example.com"), "learner@example.com")


class TestSubscriptionPresentation(unittest.TestCase):
    def test_subscription_states_have_textual_labels(self):
        calls: list[str] = []

        def fake_markdown(value, **_kwargs):
            calls.append(value)

        with patch("utils.secondary_components.st.markdown", side_effect=fake_markdown):
            render_access_status_pill(has_premium=True, subscription_status="active")
            render_access_status_pill(has_premium=False, subscription_status="free")

        self.assertIn("Access: Premium", calls[0])
        self.assertIn("Access: Free", calls[1])

    def test_upgrade_and_portal_actions_use_existing_destinations(self):
        account_source = (REPO_ROOT / "pages" / "Account.py").read_text(encoding="utf-8")
        self.assertIn("create_checkout_session_url", account_source)
        self.assertIn("resolve_portal_session_url", account_source)
        self.assertIn("render_portal_session_link_markdown", account_source)
        self.assertIn(PORTAL_MANAGE_LABEL, render_portal_session_link_markdown("https://billing.stripe.com/p/session/test"))

    def test_no_unfinished_feature_is_advertised(self):
        source = inspect.getsource(render_subscription_plan_summary)
        combined = " ".join(PREMIUM_BENEFITS).lower()
        self.assertNotIn("scenario simulator", combined)
        self.assertNotIn("automatic publishing", combined)


class TestSupportPresentation(unittest.TestCase):
    def test_support_ticket_states_remain_truthful(self):
        support_source = (REPO_ROOT / "pages" / "Support.py").read_text(encoding="utf-8")
        self.assertIn("Support ticket submitted.", support_source)
        self.assertIn("Status: Open", support_source)
        self.assertIn("Could not save the support ticket", support_source)


class TestLegalPresentation(unittest.TestCase):
    def test_legal_text_content_is_not_altered_by_presentation_helpers(self):
        legal_source = (REPO_ROOT / "utils" / "legal_policy_pages.py").read_text(encoding="utf-8")
        self.assertIn("CertBound is provided on an", legal_source)
        self.assertIn("as available", legal_source)
        for heading in TERMS_HEADINGS[1:]:
            self.assertIn(f'st.subheader("{heading}")', legal_source)

    def test_legal_and_reset_pages_remain_public(self):
        for page in (
            "Terms_of_Service.py",
            "Privacy_Policy.py",
            "Refund_and_Cancellation_Policy.py",
            "Reset_Password.py",
        ):
            source = (REPO_ROOT / "pages" / page).read_text(encoding="utf-8")
            self.assertIn("render_public_chrome()", source)
            self.assertNotIn("require_login", source)
            if page != "Reset_Password.py":
                self.assertNotIn("enforce_session_timeout()", source)


class TestAccessRequirements(unittest.TestCase):
    def test_account_and_support_retain_existing_access_requirements(self):
        account_source = (REPO_ROOT / "pages" / "Account.py").read_text(encoding="utf-8")
        support_source = (REPO_ROOT / "pages" / "Support.py").read_text(encoding="utf-8")
        self.assertIn("enforce_session_timeout()", account_source)
        self.assertIn("require_login", support_source)


class TestPresentationPurity(unittest.TestCase):
    def test_presentation_helpers_perform_no_provider_calls(self):
        source = inspect.getsource(sys.modules["utils.secondary_components"])
        forbidden = ("supabase", "create_client", "auth.sign_in", "table(", "import stripe", "stripe.checkout")
        for token in forbidden:
            self.assertNotIn(token, source)

    def test_authentication_logic_remains_in_existing_modules(self):
        account_source = (REPO_ROOT / "pages" / "Account.py").read_text(encoding="utf-8")
        self.assertIn("sign_in_with_password", account_source)
        self.assertIn("auth.sign_up", account_source)
        self.assertNotIn("def sign_in_with_password", account_source)


class TestCompatibility(unittest.TestCase):
    def test_existing_stripe_return_handling_remains_unchanged(self):
        account_source = (REPO_ROOT / "pages" / "Account.py").read_text(encoding="utf-8")
        self.assertIn("st.info(CHECKOUT_PENDING_MESSAGE)", account_source)
        self.assertIn("CHECKOUT_SUCCESS_SIGNIN_MESSAGE", account_source)
        self.assertIn('billing_return == "success"', account_source)

    def test_existing_password_recovery_behavior_remains_unchanged(self):
        reset_source = (REPO_ROOT / "pages" / "Reset_Password.py").read_text(encoding="utf-8")
        self.assertIn("install_parent_hash_redirect", reset_source)
        self.assertIn('st.success("Valid password reset session detected. Enter a new password below.")', reset_source)
        self.assertIn("classify_recovery_session_error", reset_source)

    def test_secondary_pages_import_shared_components(self):
        for module_name in (
            "pages.Account",
            "pages.Support",
            "pages.Reset_Password",
            "pages.Terms_of_Service",
        ):
            module = __import__(module_name, fromlist=["*"])
            source = inspect.getsource(module)
            self.assertIn("utils.secondary_components", source)
            self.assertIn("inject_secondary_theme", source)


class TestLegalWrapper(unittest.TestCase):
    def test_render_terms_content_still_uses_streamlit_markdown(self):
        source = inspect.getsource(render_terms_content)
        self.assertIn("st.subheader", source)
        self.assertIn("st.markdown", source)


if __name__ == "__main__":
    unittest.main()
