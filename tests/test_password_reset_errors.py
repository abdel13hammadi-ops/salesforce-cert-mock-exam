"""Focused tests for Reset Password Supabase error classification."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from supabase_auth.errors import AuthApiError, AuthWeakPasswordError
except ImportError:
    from gotrue.errors import AuthApiError, AuthWeakPasswordError

from utils.password_reset_errors import (
    CATEGORY_RECOVERY_INVALID,
    CATEGORY_SAME_PASSWORD,
    CATEGORY_UNEXPECTED,
    CATEGORY_VALIDATION,
    EXPIRED_RECOVERY_LINK_MESSAGE,
    SAME_PASSWORD_MESSAGE,
    UNEXPECTED_PASSWORD_UPDATE_MESSAGE,
    WEAK_PASSWORD_MESSAGE,
    classify_password_update_error,
    classify_recovery_session_error,
    log_password_reset_failure,
)

RESET_PASSWORD_PATH = Path(__file__).resolve().parents[1] / "pages" / "Reset_Password.py"


class TestPasswordUpdateErrorClassification(unittest.TestCase):
    def test_same_password_error_code_produces_correct_message(self):
        exc = AuthApiError(
            "New password should be different from the old password.",
            422,
            "same_password",
        )
        category, message = classify_password_update_error(exc)
        self.assertEqual(category, CATEGORY_SAME_PASSWORD)
        self.assertEqual(message, SAME_PASSWORD_MESSAGE)

    def test_same_password_message_fallback_produces_correct_message(self):
        exc = RuntimeError("New password should be different from your current password.")
        category, message = classify_password_update_error(exc)
        self.assertEqual(category, CATEGORY_SAME_PASSWORD)
        self.assertEqual(message, SAME_PASSWORD_MESSAGE)

    def test_expired_recovery_session_produces_expired_link_message(self):
        exc = AuthApiError("Invalid Refresh Token: Already Used", 401, "session_not_found")
        category, message = classify_password_update_error(exc)
        self.assertEqual(category, CATEGORY_RECOVERY_INVALID)
        self.assertEqual(message, EXPIRED_RECOVERY_LINK_MESSAGE)

    def test_recovery_session_error_classifier_uses_expired_link_message(self):
        exc = AuthApiError("JWT expired", 401, "invalid_jwt")
        category, message = classify_recovery_session_error(exc)
        self.assertEqual(category, CATEGORY_RECOVERY_INVALID)
        self.assertEqual(message, EXPIRED_RECOVERY_LINK_MESSAGE)

    def test_weak_password_produces_validation_message(self):
        exc = AuthWeakPasswordError("Password is too weak", 422, ["length"])
        category, message = classify_password_update_error(exc)
        self.assertEqual(category, CATEGORY_VALIDATION)
        self.assertEqual(message, WEAK_PASSWORD_MESSAGE)

    def test_validation_failed_produces_validation_message(self):
        exc = AuthApiError("Validation failed", 422, "validation_failed")
        category, message = classify_password_update_error(exc)
        self.assertEqual(category, CATEGORY_VALIDATION)
        self.assertNotEqual(message, EXPIRED_RECOVERY_LINK_MESSAGE)

    def test_unexpected_error_does_not_claim_link_expired(self):
        exc = RuntimeError("upstream database unavailable")
        category, message = classify_password_update_error(exc)
        self.assertEqual(category, CATEGORY_UNEXPECTED)
        self.assertEqual(message, UNEXPECTED_PASSWORD_UPDATE_MESSAGE)
        self.assertNotIn("expired", message.lower())
        self.assertNotIn("already used", message.lower())

    def test_unexpected_recovery_session_error_does_not_claim_link_expired(self):
        exc = RuntimeError("network timeout")
        category, message = classify_recovery_session_error(exc)
        self.assertEqual(category, CATEGORY_UNEXPECTED)
        self.assertEqual(message, UNEXPECTED_PASSWORD_UPDATE_MESSAGE)


class TestResetPasswordPageWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = RESET_PASSWORD_PATH.read_text(encoding="utf-8")

    def test_page_uses_error_classifier_instead_of_blanket_expired_message(self):
        self.assertIn("classify_password_update_error", self.source)
        self.assertNotIn(
            'st.error("Could not update password. The reset link may be expired or already used.")',
            self.source,
        )

    def test_same_password_path_keeps_warning_on_form(self):
        self.assertIn("CATEGORY_SAME_PASSWORD", self.source)
        self.assertIn("st.warning(message)", self.source)
        success_block = self.source.split("st.success(\"Password updated.", 1)[0]
        self.assertNotIn("st.page_link(\"pages/Account.py\", label=\"Go to Login\"", success_block)

    def test_successful_reset_behavior_unchanged(self):
        self.assertIn('st.success("Password updated. You can now log in with your new password.")', self.source)
        self.assertIn('st.page_link("pages/Account.py", label="Go to Login"', self.source)

    def test_update_and_session_errors_are_classified_separately(self):
        self.assertIn("classify_recovery_session_error", self.source)
        update_block = self.source.split("client.auth.update_user", 1)[1].split("else:", 1)[0]
        self.assertIn("classify_password_update_error", update_block)

    def test_unexpected_failures_do_not_render_exception_details_to_user(self):
        self.assertNotIn("st.caption(str(update_exc))", self.source)
        self.assertNotIn("st.caption(str(session_exc))", self.source)
        self.assertNotIn("st.caption(str(exc))", self.source)
        self.assertIn("log_password_reset_failure(", self.source)
        self.assertIn("st.error(message)", self.source)


class TestPasswordResetFailureLogging(unittest.TestCase):
    def test_log_password_reset_failure_omits_exception_message_and_secrets(self):
        exc = RuntimeError("access_token=secret refresh_token=secret password=hunter2")
        with self.assertLogs("utils.password_reset_errors", level="ERROR") as logs:
            log_password_reset_failure("password reset password update failed", exc)
        record = logs.records[0]
        self.assertIn("password reset password update failed", record.getMessage())
        self.assertIn("error_type=RuntimeError", record.getMessage())
        self.assertIn("error_code=unknown", record.getMessage())
        self.assertNotIn("access_token", record.getMessage())
        self.assertNotIn("refresh_token", record.getMessage())
        self.assertNotIn("hunter2", record.getMessage())


class TestSamePasswordKeepsRecoverySession(unittest.TestCase):
    def test_same_password_classification_does_not_imply_recovery_invalid(self):
        exc = AuthApiError(
            "New password should be different from the old password.",
            422,
            "same_password",
        )
        category, _ = classify_password_update_error(exc)
        self.assertNotEqual(category, CATEGORY_RECOVERY_INVALID)

    def test_reset_page_does_not_clear_tokens_on_same_password_warning(self):
        source = RESET_PASSWORD_PATH.read_text(encoding="utf-8")
        warning_section = source.split("if category == CATEGORY_SAME_PASSWORD:", 1)[1].split(
            "elif category == CATEGORY_VALIDATION:",
            1,
        )[0]
        self.assertIn("st.warning(message)", warning_section)
        self.assertNotIn("loc.replace", warning_section)
        self.assertNotIn("clear_login_state", warning_section)


if __name__ == "__main__":
    unittest.main()
