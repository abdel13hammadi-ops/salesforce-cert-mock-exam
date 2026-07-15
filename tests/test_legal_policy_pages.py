"""Focused tests for public legal policy pages and Account links."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.legal_policy_pages import (
    EFFECTIVE_DATE,
    GOVERNING_JURISDICTION,
    LEGAL_BUSINESS_NAME,
    PRIVACY_HEADINGS,
    PRIVACY_PAGE,
    REFUND_HEADINGS,
    REFUND_PAGE,
    SUPPORT_EMAIL,
    TERMS_HEADINGS,
    TERMS_PAGE,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_PATH = REPO_ROOT / "pages" / "Account.py"
ACCESS_CONTROL_PATH = REPO_ROOT / "utils" / "access_control.py"
POLICY_PAGES = {
    "Terms of Service": REPO_ROOT / "pages" / "Terms_of_Service.py",
    "Privacy Policy": REPO_ROOT / "pages" / "Privacy_Policy.py",
    "Refund and Cancellation Policy": REPO_ROOT / "pages" / "Refund_and_Cancellation_Policy.py",
}
LEGAL_UTILS_PATH = REPO_ROOT / "utils" / "legal_policy_pages.py"


class TestPublicAccessibility(unittest.TestCase):
    def test_policy_pages_use_public_navigation_without_login_gate(self):
        for label, path in POLICY_PAGES.items():
            with self.subTest(page=label):
                source = path.read_text(encoding="utf-8")
                self.assertTrue(
                    "render_public_chrome()" in source or "render_sidebar_navigation()" in source,
                    msg=f"{label} must use public chrome",
                )
                self.assertNotIn("require_login", source)
                self.assertNotIn("render_app_chrome()", source)
                self.assertNotIn("enforce_session_timeout()", source)

    def test_policy_page_paths_are_declared(self):
        source = LEGAL_UTILS_PATH.read_text(encoding="utf-8")
        self.assertIn(TERMS_PAGE, source)
        self.assertIn(PRIVACY_PAGE, source)
        self.assertIn(REFUND_PAGE, source)


class TestSidebarLegalNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sidebar_source = ACCESS_CONTROL_PATH.read_text(encoding="utf-8")
        cls.account_source = ACCOUNT_PATH.read_text(encoding="utf-8")

    def test_sidebar_uses_centralized_legal_routes(self):
        self.assertIn("legal_routes", self.sidebar_source)

    def test_sidebar_renders_legal_group(self):
        self.assertIn("Legal", self.sidebar_source)

    def test_account_no_longer_renders_horizontal_policy_row(self):
        self.assertNotIn("render_legal_policy_links", self.account_source)
        self.assertNotIn("Legal policies", self.account_source)


class TestRequiredPolicyHeadings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legal_source = LEGAL_UTILS_PATH.read_text(encoding="utf-8")

    def test_terms_headings_present(self):
        for heading in TERMS_HEADINGS[1:]:
            with self.subTest(heading=heading):
                self.assertIn(f'st.subheader("{heading}")', self.legal_source)
        self.assertIn('st.title(f"{page_icon} {title}")', self.legal_source)

    def test_privacy_headings_present(self):
        for heading in PRIVACY_HEADINGS[1:]:
            with self.subTest(heading=heading):
                self.assertIn(f'st.subheader("{heading}")', self.legal_source)

    def test_refund_headings_present(self):
        for heading in REFUND_HEADINGS[1:]:
            with self.subTest(heading=heading):
                self.assertIn(f'st.subheader("{heading}")', self.legal_source)

    def test_refund_policy_reflects_current_product_behavior(self):
        source = self.legal_source
        self.assertIn("monthly recurring subscription", source)
        self.assertIn("Stripe", source)
        self.assertIn("Customer Portal", source)
        self.assertIn("Manage subscription", source)
        self.assertIn("remains active until", source)
        self.assertIn("does not automatically promise refunds", source)


class TestConfirmedOwnerInformation(unittest.TestCase):
    PLACEHOLDER_MARKERS = (
        "[LEGAL_BUSINESS_NAME",
        "[SUPPORT_EMAIL",
        "[EFFECTIVE_DATE",
        "[GOVERNING_JURISDICTION",
        "owner to confirm",
    )

    @classmethod
    def setUpClass(cls):
        cls.sources = {
            "utils": LEGAL_UTILS_PATH.read_text(encoding="utf-8"),
            **{
                label: path.read_text(encoding="utf-8")
                for label, path in POLICY_PAGES.items()
            },
        }

    def test_confirmed_values_are_present(self):
        self.assertEqual(LEGAL_BUSINESS_NAME, "CertBound LLC")
        self.assertEqual(SUPPORT_EMAIL, "support@certbound.com")
        self.assertEqual(EFFECTIVE_DATE, "July 1, 2026")
        self.assertEqual(GOVERNING_JURISDICTION, "New Jersey, United States")

        legal_source = self.sources["utils"]
        self.assertIn("CertBound LLC", legal_source)
        self.assertIn("support@certbound.com", legal_source)
        self.assertIn("July 1, 2026", legal_source)
        self.assertIn("New Jersey, United States", legal_source)

    def test_no_placeholder_markers_remain(self):
        for label, source in self.sources.items():
            with self.subTest(source=label):
                for marker in self.PLACEHOLDER_MARKERS:
                    self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
