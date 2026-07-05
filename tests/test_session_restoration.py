"""Focused tests for signed-session restoration after external navigation."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.access_control as access_control
from utils.access_control import (
    BROWSER_SESSION_STORAGE_KEY,
    SESSION_PARAM,
    bootstrap_signed_session,
    clear_login_state,
    is_session_restoration_pending,
    make_signed_session,
    restore_login_from_signed_url,
)
from utils.billing_mapping import map_stripe_subscription_status_to_certbound, stripe_status_grants_premium
from utils.billing_stripe import _portal_return_url_with_marker

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_PATH = REPO_ROOT / "pages" / "Account.py"
ACCESS_CONTROL_PATH = REPO_ROOT / "utils" / "access_control.py"


class _FakeSessionState(dict):
    def pop(self, key, default=None):
        return super().pop(key, default)


class TestSessionBootstrapSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.account_source = ACCOUNT_PATH.read_text(encoding="utf-8")
        cls.access_source = ACCESS_CONTROL_PATH.read_text(encoding="utf-8")

    def test_account_waits_for_restoration_before_login_ui(self):
        bootstrap_idx = self.account_source.index("render_app_chrome()")
        pending_idx = self.account_source.index("is_session_restoration_pending()")
        current_email_idx = self.account_source.index("current_email = get_current_user_email()")
        self.assertLess(bootstrap_idx, pending_idx)
        self.assertLess(pending_idx, current_email_idx)

    def test_render_app_chrome_uses_bootstrap(self):
        start = self.access_source.index("def render_app_chrome")
        block = self.access_source[start:start + 300]
        self.assertIn("bootstrap_signed_session()", block)

    def test_bootstrap_reads_local_storage_before_login(self):
        start = self.access_source.index("def bootstrap_signed_session")
        block = self.access_source[start:start + 1200]
        self.assertIn("_read_browser_session_token_via_js_eval", block)
        self.assertIn("st.rerun()", block)


class TestBootstrapBehavior(unittest.TestCase):
    def setUp(self):
        self.session_state = _FakeSessionState()
        self.query_params = {}
        self.rerun_calls = 0
        access_control.st.session_state = self.session_state
        access_control.st.query_params = self.query_params
        access_control.st.rerun = self._rerun

    def _rerun(self):
        self.rerun_calls += 1

    def _token_for(self, email: str = "learner@example.com") -> str:
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            return make_signed_session({"user_email": email, "subscription_status": "active"})

    def test_url_restore_triggers_rerun(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._get_query_param",
            side_effect=lambda name: self.query_params.get(name, ""),
        ):
            result = bootstrap_signed_session()
        self.assertTrue(result)
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertEqual(self.rerun_calls, 1)

    def test_local_storage_restore_triggers_rerun(self):
        token = self._token_for()
        with patch(
            "utils.access_control._read_browser_session_token_via_js_eval",
            return_value=token,
        ), patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            bootstrap_signed_session()
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertEqual(self.rerun_calls, 1)

    def test_pending_flag_set_while_browser_token_is_loading(self):
        with patch(
            "utils.access_control._read_browser_session_token_via_js_eval",
            return_value=None,
        ):
            result = bootstrap_signed_session()
        self.assertFalse(result)
        self.assertTrue(is_session_restoration_pending())

    def test_invalid_session_still_shows_login(self):
        with patch(
            "utils.access_control._read_browser_session_token_via_js_eval",
            return_value="not-a-valid-token",
        ):
            result = bootstrap_signed_session()
        self.assertFalse(result)
        self.assertFalse(is_session_restoration_pending())
        self.assertNotIn("user_email", self.session_state)

    def test_logout_clears_restoration_pending_flag(self):
        self.session_state["user_email"] = "learner@example.com"
        self.session_state["_session_restoration_pending"] = True
        clear_login_state()
        self.assertNotIn("user_email", self.session_state)
        self.assertNotIn("_session_restoration_pending", self.session_state)

    def test_logout_clears_practice_exam_attempt_id(self):
        """A stale practice attempt id must not survive a logout/timeout: a
        different user logging into the same browser tab afterward must not
        find a leftover id in session state (V55-PRACTICE-IDEMPOTENCY-03)."""
        self.session_state["user_email"] = "learner@example.com"
        self.session_state["practice_exam_attempt_id"] = 501
        clear_login_state()
        self.assertNotIn("practice_exam_attempt_id", self.session_state)

    def test_logout_clears_weak_exam_attempt_id(self):
        self.session_state["user_email"] = "learner@example.com"
        self.session_state["weak_exam_attempt_id"] = 502
        clear_login_state()
        self.assertNotIn("weak_exam_attempt_id", self.session_state)

    def test_logout_clears_current_exam_attempt_id(self):
        """A stale paid-mock attempt id must not survive a logout/timeout: a
        different user logging into the same browser tab afterward must not
        find a leftover id in session state (V56-PAID-MOCK-IDEMPOTENCY-02)."""
        self.session_state["user_email"] = "learner@example.com"
        self.session_state["current_exam_attempt_id"] = 503
        clear_login_state()
        self.assertNotIn("current_exam_attempt_id", self.session_state)

    def test_expired_session_flag_blocks_restore(self):
        token = self._token_for()
        self.session_state["user_session_expired"] = True
        self.query_params[SESSION_PARAM] = token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._get_query_param",
            side_effect=lambda name: self.query_params.get(name, ""),
        ):
            self.assertFalse(restore_login_from_signed_url())
            self.assertFalse(bootstrap_signed_session())


class TestPortalReturnUrls(unittest.TestCase):
    def test_portal_return_url_adds_harmless_marker_only(self):
        url = _portal_return_url_with_marker(
            secrets_getter=lambda name, default="": "https://app.example/Account" if name == "STRIPE_PORTAL_RETURN_URL" else default,
        )
        self.assertEqual(url, "https://app.example/Account?billing=portal")
        self.assertNotIn(SESSION_PARAM, url)
        self.assertNotIn("cus_", url)
        self.assertNotIn("sub_", url)

    def test_portal_return_url_does_not_duplicate_marker(self):
        url = _portal_return_url_with_marker(
            secrets_getter=lambda name, default="": "https://app.example/Account?billing=portal" if name == "STRIPE_PORTAL_RETURN_URL" else default,
        )
        self.assertEqual(url, "https://app.example/Account?billing=portal")


class TestCancelAtPeriodEndAccess(unittest.TestCase):
    def test_active_cancel_scheduled_still_maps_to_premium_until_end(self):
        self.assertTrue(stripe_status_grants_premium("active"))
        self.assertEqual(map_stripe_subscription_status_to_certbound("active"), "active")

    def test_account_shows_scheduled_cancellation_message(self):
        text = ACCOUNT_PATH.read_text(encoding="utf-8")
        self.assertIn("stripe_cancel_at_period_end", text)
        self.assertIn("scheduled to cancel at the end of the current billing period", text)


class TestBrowserStorageContract(unittest.TestCase):
    def test_local_storage_key_is_stable(self):
        self.assertEqual(BROWSER_SESSION_STORAGE_KEY, "salesforce_cert_mock_fr_session")
