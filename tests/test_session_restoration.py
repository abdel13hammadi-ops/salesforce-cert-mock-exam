"""Focused tests for signed-session restoration after external navigation."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import sys
import time
import types
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.access_control as access_control
import utils.session_timeout as session_timeout
from utils.access_control import (
    BROWSER_SESSION_STORAGE_KEY,
    SESSION_PARAM,
    SESSION_TTL_SECONDS,
    bootstrap_signed_session,
    clear_login_state,
    get_user_access_level,
    has_premium_access,
    is_session_restoration_pending,
    make_signed_session,
    render_app_chrome,
    require_paid_access,
    restore_login_from_signed_url,
    verify_signed_session,
)
from utils.billing_mapping import map_stripe_subscription_status_to_certbound, stripe_status_grants_premium
from utils.billing_stripe import _portal_return_url_with_marker
from utils.secondary_components import render_subscription_plan_summary

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_PATH = REPO_ROOT / "pages" / "Account.py"
ACCESS_CONTROL_PATH = REPO_ROOT / "utils" / "access_control.py"

_ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
_COMPLETED_QUERY_PARAM = "completed_attempt"

# SIM-SMOKE-02D: `access_control.st` is a direct `import streamlit as st`
# binding -- i.e. `access_control.st` and the real, installed `streamlit`
# module (`sys.modules["streamlit"]`) are literally the same object. An
# earlier version of this file's test isolation fix (SIM-SMOKE-02C) tried to
# clean up after direct attribute assignment onto that object
# (`access_control.st.markdown = MagicMock()`, etc.) by snapshotting
# `vars(access_control.st)` once and clearing+repopulating the real module's
# `__dict__` in each test's cleanup. That still touched the real, installed
# Streamlit module's own namespace, which corrupted its internal state
# (observed as `ImportError: cannot import name 'config' from '<unknown
# module name>'` once `utils.dashboard_components` -- a different module
# that also does `import streamlit as st` -- was imported against it).
#
# The correct fix touches the real `streamlit` module -- and `sys.modules`
# -- not at all. Every test class below now installs a disposable,
# per-test fake object as the *value of the `st` name inside
# `utils.access_control`'s own namespace* via `unittest.mock.patch.object`,
# which:
#   - never reads or writes a single attribute on the real `streamlit`
#     module or any of its real submodules (`streamlit.components`,
#     `streamlit.components.v1`, etc.);
#   - never touches `sys.modules` at all;
#   - automatically restores `access_control.st` to whatever object it
#     pointed to before the patch (the real module) once the patcher is
#     stopped, which is registered via `addCleanup` so it always runs, even
#     if the test body raises.
#
# Because the real `streamlit` module is never mutated, any other module
# (such as `utils.dashboard_components`) that resolves its own
# `import streamlit as st` against `sys.modules["streamlit"]` -- whether
# before, during, or after these tests run -- always observes the single,
# healthy, untouched real module, never a corrupted or leaked test double.


def _install_fake_access_control_streamlit(test_case: unittest.TestCase, **overrides):
    """Patch `access_control.st` (the module-level name inside
    `utils.access_control`, NOT the real `streamlit` module) to a disposable
    fake for the duration of `test_case`, restoring the original binding via
    `addCleanup` regardless of test outcome. Returns the fake so callers can
    keep mutating/inspecting it directly."""
    defaults = dict(
        session_state={},
        query_params={},
        markdown=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        caption=lambda *args, **kwargs: None,
        write=lambda *args, **kwargs: None,
        page_link=lambda *args, **kwargs: None,
        sidebar=lambda *args, **kwargs: None,
        divider=lambda *args, **kwargs: None,
        stop=lambda: (_ for _ in ()).throw(SystemExit()),
        rerun=lambda: (_ for _ in ()).throw(SystemExit()),
    )
    defaults.update(overrides)
    fake_st = types.SimpleNamespace(**defaults)
    patcher = patch.object(access_control, "st", fake_st)
    patcher.start()
    test_case.addCleanup(patcher.stop)
    return fake_st


def _install_fake_access_control_components(test_case: unittest.TestCase, **overrides):
    """Patch `access_control.components` (the module-level name, NOT the
    real `streamlit.components.v1` module) to a disposable fake for the
    duration of `test_case`, restoring the original binding via
    `addCleanup`."""
    defaults = dict(html=lambda *args, **kwargs: None)
    defaults.update(overrides)
    fake_components = types.SimpleNamespace(**defaults)
    patcher = patch.object(access_control, "components", fake_components)
    patcher.start()
    test_case.addCleanup(patcher.stop)
    return fake_components


def _install_fake_session_timeout_streamlit(test_case: unittest.TestCase, fake_st) -> None:
    """`utils/session_timeout.py` has its own separate `import streamlit as
    st` binding (just like `utils.dashboard_components`) -- patching only
    `access_control.st` does not affect it. To exercise the REAL
    `enforce_session_timeout()` against the same fake `session_state` a test
    already installed on `access_control`, its own `st` binding must be
    patched to the identical object, restored via `addCleanup`."""
    patcher = patch.object(session_timeout, "st", fake_st)
    patcher.start()
    test_case.addCleanup(patcher.stop)


def _decode_signature_bytes(token: str) -> bytes:
    _body, encoded_signature = token.split(".", 1)
    padding = "=" * (-len(encoded_signature) % 4)
    return base64.urlsafe_b64decode((encoded_signature + padding).encode("ascii"))


def _tamper_signature_bytes(token: str) -> str:
    """Return a copy of `token` whose decoded HMAC signature differs by at
    least one bit. Unlike final-character substitution, this always produces
    a genuinely invalid signature regardless of the token's Base64URL suffix."""
    body, encoded_signature = token.split(".", 1)
    padding = "=" * (-len(encoded_signature) % 4)
    signature = bytearray(
        base64.urlsafe_b64decode((encoded_signature + padding).encode("ascii"))
    )
    if len(signature) != hashlib.sha256().digest_size:
        raise AssertionError("Unexpected signed-session signature length")
    signature[0] ^= 0x01
    corrupted = (
        base64.urlsafe_b64encode(bytes(signature))
        .decode("ascii")
        .rstrip("=")
    )
    return f"{body}.{corrupted}"


def _sign_body(body: str, secret: str = "test-signing-secret") -> str:
    """Sign an arbitrary already-encoded body string with the same HMAC
    scheme `access_control.make_signed_session` uses, without going through
    it -- lets tests construct payloads `make_signed_session` would never
    produce itself (e.g. an already-expired `exp`, or an unparseable body),
    while still passing signature verification so `verify_signed_session`
    reaches the specific downstream check under test."""
    sig = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{access_control._b64url_encode(sig)}"


def _custom_signed_token(payload: Dict[str, Any], secret: str = "test-signing-secret") -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = access_control._b64url_encode(raw)
    return _sign_body(body, secret=secret)


class _FakeSessionState(dict):
    def pop(self, key, default=None):
        return super().pop(key, default)


class _FakeQueryParams(dict):
    def get(self, key, default=""):  # noqa: ANN001
        if key not in self:
            return default
        value = super().get(key, default)
        if isinstance(value, list):
            return str(value[-1] if value else default)
        return str(value or default)

    def get_all(self, key):  # noqa: ANN001
        if key not in self:
            return []
        value = super().get(key)
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]


class _BrokenQueryParams:
    def __contains__(self, _key) -> bool:
        raise RuntimeError("query params unavailable")

    def get_all(self, _key):  # noqa: ANN001
        raise RuntimeError("query params unavailable")

    def get(self, _key, default=""):  # noqa: ANN001
        raise RuntimeError("query params unavailable")


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
        block = self.access_source[start : start + 300]
        self.assertIn("bootstrap_signed_session()", block)

    def test_bootstrap_reads_local_storage_before_login(self):
        start = self.access_source.index("def bootstrap_signed_session")
        end = self.access_source.index("\ndef ", start + 1)
        block = self.access_source[start:end]
        self.assertIn("_read_browser_session_token_via_js_eval", block)
        self.assertNotIn("st.rerun()", block)

    def test_fr_session_never_written_by_activity_stamping_or_login_persist(self):
        for fn_name in ("stamp_activity_to_token", "persist_login_to_signed_url"):
            with self.subTest(fn=fn_name):
                start = self.access_source.index(f"def {fn_name}")
                end = self.access_source.index("\ndef ", start + 1)
                block = self.access_source[start:end]
                self.assertNotIn("_set_query_param", block)
                self.assertNotIn("_persist_token_to_url", block)

    def test_url_bootstrap_handoff_removes_token_only_after_ack(self):
        start = self.access_source.index("def _finalize_url_bootstrap_handoff")
        end = self.access_source.index("\ndef ", start + 1)
        block = self.access_source[start:end]
        write_idx = block.index("_write_browser_session_token_via_js_eval")
        clear_idx = block.index("_clear_signed_session_query_token")
        self.assertLess(write_idx, clear_idx)


class TestBootstrapBehavior(unittest.TestCase):
    def setUp(self):
        self.session_state = _FakeSessionState()
        self.query_params = _FakeQueryParams()
        self.rerun_calls = 0
        self.fake_st = _install_fake_access_control_streamlit(
            self,
            session_state=self.session_state,
            query_params=self.query_params,
            rerun=self._rerun,
        )

    def _rerun(self):
        self.rerun_calls += 1

    def _token_for(
        self,
        email: str = "learner@example.com",
        *,
        subscription_status: str = "active",
        preferred_language_code: str = "en",
        preferred_timezone: str = "UTC",
        exp_offset_seconds: int = 3600,
    ) -> str:
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            return make_signed_session(
                {
                    "user_email": email,
                    "subscription_status": subscription_status,
                    "preferred_language_code": preferred_language_code,
                    "preferred_timezone": preferred_timezone,
                    "exp": int(time.time()) + exp_offset_seconds,
                }
            )

    def _complete_render_app_chrome_session_restore(self, *, browser_ack: Optional[bool] = True) -> None:
        """Simulate the second half of `render_app_chrome()`: the explicit,
        acknowledged browser-storage handoff that runs after
        `bootstrap_signed_session()` for a URL-bootstrapped token.

        `browser_ack` controls the simulated outcome of the browser's
        localStorage write: True = confirmed, None = still pending,
        False = the browser reported failure.
        """
        with patch(
            "utils.access_control._write_browser_session_token_via_js_eval",
            return_value=browser_ack,
        ):
            access_control._render_pending_browser_storage_clear_if_needed()
            if self.session_state.get("auth_restored_from_url"):
                access_control._finalize_url_bootstrap_handoff()

    def test_url_restore_populates_session_without_rerun(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            result = bootstrap_signed_session()
            self._complete_render_app_chrome_session_restore()
        self.assertTrue(result)
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertEqual(self.session_state.get("subscription_status"), "active")
        self.assertEqual(self.session_state.get("preferred_language_code"), "en")
        self.assertEqual(self.session_state.get("preferred_timezone"), "UTC")
        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertEqual(self.rerun_calls, 0)

    def test_local_storage_restore_populates_session_without_rerun(self):
        token = self._token_for()
        with patch(
            "utils.access_control._read_browser_session_token_via_js_eval",
            return_value=token,
        ), patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            bootstrap_signed_session()
            self._complete_render_app_chrome_session_restore()
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertEqual(self.rerun_calls, 0)

    def test_hard_refresh_local_storage_restore_leaves_url_completely_clean(self):
        """SIM-SMOKE-02B: a hard refresh restored purely from localStorage
        (no `fr_session` ever present in the URL) must not add it."""
        token = self._token_for()
        self.assertNotIn(SESSION_PARAM, self.query_params)
        with patch(
            "utils.access_control._read_browser_session_token_via_js_eval",
            return_value=token,
        ), patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            result = bootstrap_signed_session()
            self._complete_render_app_chrome_session_restore()
        self.assertTrue(result)
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertFalse(self.session_state.get("auth_restored_from_url"))

    def test_browser_persistence_pending_keeps_url_token_and_stays_authenticated(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertTrue(bootstrap_signed_session())
            self._complete_render_app_chrome_session_restore(browser_ack=None)
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertEqual(self.query_params.get(SESSION_PARAM), token)
        self.assertTrue(self.session_state.get("auth_restored_from_url"))
        self.assertFalse(is_session_restoration_pending())
        self.assertEqual(self.rerun_calls, 0)

    def test_browser_persistence_failure_keeps_url_token_without_leaking(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        access_control.st.error = MagicMock()
        access_control.st.warning = MagicMock()
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertTrue(bootstrap_signed_session())
            self._complete_render_app_chrome_session_restore(browser_ack=False)
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertEqual(self.query_params.get(SESSION_PARAM), token)
        access_control.st.error.assert_not_called()
        access_control.st.warning.assert_not_called()

    def test_pending_handoff_then_confirmed_ack_removes_token(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            bootstrap_signed_session()
            self._complete_render_app_chrome_session_restore(browser_ack=None)
            self.assertIn(SESSION_PARAM, self.query_params)
            # Next rerun: browser now confirms the write succeeded.
            bootstrap_signed_session()
            self._complete_render_app_chrome_session_restore(browser_ack=True)
        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertFalse(self.session_state.get("auth_restored_from_url"))
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")

    def test_confirmed_persistence_then_second_rerun_does_not_readd_token(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            bootstrap_signed_session()
            self._complete_render_app_chrome_session_restore(browser_ack=True)
            self.assertNotIn(SESSION_PARAM, self.query_params)

            with patch("utils.access_control._write_browser_session_token_via_js_eval") as write_mock:
                result = bootstrap_signed_session()
                if self.session_state.get("auth_restored_from_url"):
                    access_control._finalize_url_bootstrap_handoff()
                write_mock.assert_not_called()
        self.assertTrue(result)
        self.assertNotIn(SESSION_PARAM, self.query_params)

    def test_activity_stamp_refreshes_browser_storage_without_url_token(self):
        from utils.access_control import stamp_activity_to_token

        token = self._token_for()
        self.session_state["signed_session_token"] = token
        self.session_state["last_activity_at"] = time.time()
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._write_browser_session_token_via_js_eval",
            return_value=True,
        ) as write_mock:
            stamp_activity_to_token()
        write_mock.assert_called_once()
        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertNotEqual(self.session_state["signed_session_token"], token)

    def test_activity_stamp_with_no_token_does_not_touch_browser_or_url(self):
        from utils.access_control import stamp_activity_to_token

        with patch("utils.access_control._write_browser_session_token_via_js_eval") as write_mock:
            stamp_activity_to_token()
        write_mock.assert_not_called()
        self.assertNotIn(SESSION_PARAM, self.query_params)

    def test_fresh_login_persist_does_not_write_url_token(self):
        from utils.access_control import persist_login_to_signed_url

        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._write_browser_session_token_via_js_eval",
            return_value=True,
        ) as write_mock:
            persist_login_to_signed_url(
                {"email": "learner@example.com", "subscription_status": "active"}
            )
        write_mock.assert_called_once()
        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertTrue(self.session_state.get("signed_session_token"))

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
        self.session_state["user_email"] = "learner@example.com"
        self.session_state["current_exam_attempt_id"] = 503
        clear_login_state()
        self.assertNotIn("current_exam_attempt_id", self.session_state)

    def test_expired_session_flag_blocks_restore(self):
        token = self._token_for()
        self.session_state["user_session_expired"] = True
        self.query_params[SESSION_PARAM] = token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertFalse(restore_login_from_signed_url())
            self.assertFalse(bootstrap_signed_session())

    def test_fake_query_params_get_returns_last_repeated_value(self):
        params = _FakeQueryParams({SESSION_PARAM: ["first-token", "second-token"]})
        self.assertEqual(params.get(SESSION_PARAM), "second-token")

    def test_fake_query_params_get_all_returns_all_values_in_order(self):
        params = _FakeQueryParams({SESSION_PARAM: ["token-a", "token-b"]})
        self.assertEqual(params.get_all(SESSION_PARAM), ["token-a", "token-b"])

    def test_two_different_valid_session_tokens_are_rejected(self):
        token_a = self._token_for(email="a@example.com")
        token_b = self._token_for(email="b@example.com")
        self.query_params[SESSION_PARAM] = [token_a, token_b]
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertFalse(restore_login_from_signed_url())
        self.assertNotIn("user_email", self.session_state)
        self.assertNotIn(SESSION_PARAM, self.query_params)

    def test_two_identical_valid_session_tokens_are_rejected(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = [token, token]
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertFalse(restore_login_from_signed_url())
        self.assertNotIn("user_email", self.session_state)

    def test_valid_token_followed_by_malformed_value_is_rejected(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = [token, "not-a-valid-token"]
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertFalse(restore_login_from_signed_url())
        self.assertNotIn("user_email", self.session_state)

    def test_malformed_value_followed_by_valid_token_is_rejected(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = ["not-a-valid-token", token]
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertFalse(restore_login_from_signed_url())
        self.assertNotIn("user_email", self.session_state)

    def test_empty_repeated_session_value_is_rejected(self):
        self.query_params[SESSION_PARAM] = [""]
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertFalse(restore_login_from_signed_url())
        self.assertNotIn("user_email", self.session_state)

    def test_query_params_api_failure_does_not_authenticate(self):
        access_control.st.query_params = _BrokenQueryParams()
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertFalse(bootstrap_signed_session())
        self.assertNotIn("user_email", self.session_state)

    def test_invalid_signature_does_not_authenticate(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = _tamper_signature_bytes(token)
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertFalse(restore_login_from_signed_url())
        self.assertNotIn("user_email", self.session_state)

    def test_expired_token_does_not_authenticate(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control.time.time",
            return_value=int(time.time()) + SESSION_TTL_SECONDS + 60,
        ):
            self.assertFalse(restore_login_from_signed_url())
        self.assertNotIn("user_email", self.session_state)

    def test_completed_attempt_survives_successful_session_token_removal(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        self.query_params[_COMPLETED_QUERY_PARAM] = _ATTEMPT_ID
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertTrue(bootstrap_signed_session())
            self._complete_render_app_chrome_session_restore()
        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertEqual(self.query_params.get(_COMPLETED_QUERY_PARAM), _ATTEMPT_ID)

        # A later rerun (e.g. activity stamping, another bootstrap pass) must
        # not disturb completed_attempt either.
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._write_browser_session_token_via_js_eval"
        ):
            access_control.stamp_activity_to_token()
            bootstrap_signed_session()
        self.assertEqual(self.query_params.get(_COMPLETED_QUERY_PARAM), _ATTEMPT_ID)
        self.assertNotIn(SESSION_PARAM, self.query_params)

    def test_invalid_tokens_never_reach_logs(self):
        token = self._token_for()
        bad_token = _tamper_signature_bytes(token)
        self.query_params[SESSION_PARAM] = bad_token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch.object(
            logging.getLogger("utils.user_errors"),
            "error",
        ) as error_mock:
            self.assertFalse(restore_login_from_signed_url())
        joined = " ".join(str(call) for call in error_mock.call_args_list)
        self.assertNotIn(bad_token, joined)


class TestSignatureTamperingDeterminism(unittest.TestCase):
    """SIM-SMOKE-02F: proves byte-level signature corruption is always
    deterministic and always rejected, unlike final-character substitution."""

    def setUp(self):
        self.session_state = _FakeSessionState()
        self.query_params = _FakeQueryParams()
        self.fake_st = _install_fake_access_control_streamlit(
            self,
            session_state=self.session_state,
            query_params=self.query_params,
        )

    def _token_for(
        self,
        email: str = "learner@example.com",
        *,
        exp_offset_seconds: int = 3600,
    ) -> str:
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            return make_signed_session(
                {
                    "user_email": email,
                    "subscription_status": "active",
                    "preferred_language_code": "en",
                    "preferred_timezone": "UTC",
                    "exp": int(time.time()) + exp_offset_seconds,
                }
            )

    def test_tampered_token_differs_from_valid_token(self):
        token = self._token_for()
        bad_token = _tamper_signature_bytes(token)
        self.assertNotEqual(bad_token, token)

    def test_tampered_token_preserves_body(self):
        token = self._token_for()
        bad_token = _tamper_signature_bytes(token)
        self.assertEqual(token.split(".", 1)[0], bad_token.split(".", 1)[0])

    def test_decoded_signature_bytes_differ(self):
        token = self._token_for()
        bad_token = _tamper_signature_bytes(token)
        original = _decode_signature_bytes(token)
        corrupted = _decode_signature_bytes(bad_token)
        self.assertNotEqual(original, corrupted)

    def test_both_signatures_are_exactly_32_bytes(self):
        token = self._token_for()
        bad_token = _tamper_signature_bytes(token)
        self.assertEqual(len(_decode_signature_bytes(token)), hashlib.sha256().digest_size)
        self.assertEqual(len(_decode_signature_bytes(bad_token)), hashlib.sha256().digest_size)

    def test_verify_signed_session_rejects_tampered_token(self):
        token = self._token_for()
        bad_token = _tamper_signature_bytes(token)
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertIsNone(verify_signed_session(bad_token))
            self.assertIsNotNone(verify_signed_session(token))

    def test_restore_login_rejects_tampered_token(self):
        token = self._token_for()
        bad_token = _tamper_signature_bytes(token)
        self.query_params[SESSION_PARAM] = bad_token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            self.assertFalse(restore_login_from_signed_url())

    def test_learner_email_absent_after_tampered_token_rejection(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = _tamper_signature_bytes(token)
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            restore_login_from_signed_url()
        self.assertNotIn("user_email", self.session_state)

    def test_tampered_token_never_appears_in_logs_or_diagnostics(self):
        token = self._token_for()
        bad_token = _tamper_signature_bytes(token)
        self.query_params[SESSION_PARAM] = bad_token
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch.object(
            logging.getLogger("utils.user_errors"),
            "error",
        ) as error_mock, patch.object(
            access_control.st,
            "error",
        ) as ui_error_mock, patch.object(
            access_control.st,
            "warning",
        ) as ui_warning_mock:
            self.assertFalse(restore_login_from_signed_url())
        joined = " ".join(
            str(call)
            for mock in (error_mock, ui_error_mock, ui_warning_mock)
            for call in mock.call_args_list
        )
        self.assertNotIn(bad_token, joined)

    def test_rejection_deterministic_across_multiple_generated_tokens(self):
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            for email, exp_offset in (
                ("a@example.com", 3600),
                ("b@example.com", 7200),
                ("learner@example.com", 1800),
            ):
                with self.subTest(email=email, exp_offset=exp_offset):
                    token = make_signed_session(
                        {
                            "user_email": email,
                            "subscription_status": "active",
                            "exp": int(time.time()) + exp_offset,
                        }
                    )
                    bad_token = _tamper_signature_bytes(token)
                    self.assertNotEqual(bad_token, token)
                    self.assertNotEqual(
                        _decode_signature_bytes(token),
                        _decode_signature_bytes(bad_token),
                    )
                    self.assertIsNone(verify_signed_session(bad_token))


class TestDirectLinkScenarioSimulatorBootstrap(unittest.TestCase):
    def setUp(self):
        self.session_state = _FakeSessionState()
        self.query_params = _FakeQueryParams()
        self.rerun_calls = 0
        self.stop_calls = 0
        self.fake_st = _install_fake_access_control_streamlit(
            self,
            session_state=self.session_state,
            query_params=self.query_params,
            rerun=self._rerun,
            stop=self._stop,
            markdown=MagicMock(),
            info=MagicMock(),
            warning=MagicMock(),
            page_link=MagicMock(),
            sidebar=MagicMock(),
            divider=MagicMock(),
            caption=MagicMock(),
            error=MagicMock(),
        )
        self.fake_components = _install_fake_access_control_components(
            self, html=lambda *args, **kwargs: None
        )

    def _rerun(self):
        self.rerun_calls += 1

    def _stop(self):
        self.stop_calls += 1
        raise SystemExit()

    def _token_for(self) -> str:
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            return make_signed_session(
                {
                    "user_email": "learner@example.com",
                    "subscription_status": "active",
                    "preferred_language_code": "en",
                    "preferred_timezone": "UTC",
                }
            )

    def test_valid_direct_link_reaches_premium_access_without_app_users_lookup(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        controller_mock = MagicMock()

        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._write_browser_session_token_via_js_eval", return_value=True
        ), patch("utils.access_control.render_sidebar_navigation"), patch(
            "utils.dashboard_components.inject_shell_theme"
        ), patch("utils.navigation.is_feature_flag_enabled", return_value=True), patch(
            "utils.session_timeout.enforce_session_timeout", return_value=True
        ), patch("utils.session_timeout.show_session_expired_notice"), patch(
            "utils.dashboard_components.inject_certbound_theme"
        ), patch(
            "utils.scenario_learner_controller.start_or_resume_ba201_attempt", controller_mock
        ), patch("utils.access_control.get_user_profile", return_value=None):
            render_app_chrome()
            self.assertTrue(access_control.require_paid_access("Scenario Simulator"))

        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertTrue(has_premium_access("learner@example.com"))
        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertEqual(self.rerun_calls, 0)
        controller_mock.assert_not_called()

    def test_valid_direct_link_restores_before_require_paid_access(self):
        token = self._token_for()
        self.query_params[SESSION_PARAM] = token
        events: list[str] = []

        def _paid_access(feature_name):
            events.append("paid_access")
            self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
            return True

        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._write_browser_session_token_via_js_eval", return_value=True
        ), patch("utils.access_control.render_sidebar_navigation"), patch(
            "utils.dashboard_components.inject_shell_theme"
        ), patch("utils.access_control.require_paid_access", side_effect=_paid_access), patch(
            "utils.access_control.get_user_profile", return_value=None
        ):
            render_app_chrome()
            access_control.require_paid_access("Scenario Simulator")

        self.assertEqual(events, ["paid_access"])
        self.assertNotIn(SESSION_PARAM, self.query_params)


class TestActivityTimestampTimeoutPolicy(unittest.TestCase):
    """SIM-SMOKE-02H.

    SIM-SMOKE-02G incorrectly claimed the live smoke failure was caused by a
    stale `last_activity_at` embedded in the smoke launcher's token, and
    "fixed" it by resetting the idle clock to `time.time()` for every
    URL-bootstrap token. A review of the actual launcher and
    `make_signed_session()` disproved that: the launcher's token only ever
    contained `user_email`, `subscription_status`,
    `preferred_language_code`, and `preferred_timezone` -- never
    `last_activity_at` -- and `_hydrate_session_from_payload()` already
    defaulted a missing value to `time.time()` even before SIM-SMOKE-02G.
    The stale-timestamp explanation therefore could not explain the observed
    failure, and the "fix" introduced a real security regression: the signed
    token has a 30-day TTL with no one-time nonce or server-side consumption
    record, so unconditionally resetting the idle clock on every valid
    URL-bootstrap token would let an old bearer token bypass the intended
    30-minute inactivity timeout simply by being replayed via the URL.

    This class proves the SIM-SMOKE-02G override has been fully removed and
    that both restoration paths (`fr_session` URL bootstrap and browser
    localStorage) now correctly, identically honor whatever
    `last_activity_at` a validly-signed token actually carries -- a missing
    value still defaults safely to "now", but a stale *signed* value is
    still subject to the real, unmocked `enforce_session_timeout()` policy
    exactly as before SIM-SMOKE-02G.
    """

    def setUp(self):
        self.session_state = _FakeSessionState()
        self.query_params = _FakeQueryParams()
        self.rerun_mock = MagicMock()
        self.stop_calls = 0

        def _stop():
            self.stop_calls += 1
            raise SystemExit()

        self.fake_st = _install_fake_access_control_streamlit(
            self,
            session_state=self.session_state,
            query_params=self.query_params,
            rerun=self.rerun_mock,
            stop=_stop,
            markdown=MagicMock(),
            info=MagicMock(),
            warning=MagicMock(),
            page_link=MagicMock(),
        )
        _install_fake_session_timeout_streamlit(self, self.fake_st)

    def _token_with_activity(
        self,
        *,
        stale_seconds: Optional[float] = None,
        email: str = "learner@example.com",
    ) -> str:
        payload: Dict[str, Any] = {
            "user_email": email,
            "subscription_status": "active",
        }
        if stale_seconds is not None:
            payload["last_activity_at"] = time.time() - stale_seconds
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            return make_signed_session(payload)

    def _run_render_app_chrome_then_real_timeout(
        self, *, browser_ack: Optional[bool] = True
    ) -> bool:
        """Mirrors the real, unmocked production order:
        `render_app_chrome()` (bootstrap + acknowledged handoff attempt)
        immediately followed by the REAL `enforce_session_timeout()`."""
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._write_browser_session_token_via_js_eval",
            return_value=browser_ack,
        ), patch("utils.access_control.render_sidebar_navigation"), patch(
            "utils.dashboard_components.inject_shell_theme"
        ), patch("utils.access_control.get_user_profile", return_value=None):
            render_app_chrome()
            return session_timeout.enforce_session_timeout()

    def test_url_bootstrap_token_with_no_activity_field_gets_fresh_timestamp(self):
        """Legitimate, narrow behavior (unchanged by the revert): a token
        that never set `last_activity_at` at all -- exactly like the real
        launcher's token shape -- still safely defaults to "now" and must
        never be treated as pre-expired."""
        token = self._token_with_activity(stale_seconds=None)
        self.query_params[SESSION_PARAM] = token

        still_active = self._run_render_app_chrome_then_real_timeout()

        self.assertTrue(still_active)
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertAlmostEqual(
            self.session_state.get("last_activity_at"), time.time(), delta=5
        )

    def test_url_bootstrap_token_with_fresh_activity_survives(self):
        """No-regression baseline: a token whose signed `last_activity_at`
        is already fresh must obviously also survive."""
        token = self._token_with_activity(stale_seconds=5)
        self.query_params[SESSION_PARAM] = token

        still_active = self._run_render_app_chrome_then_real_timeout()

        self.assertTrue(still_active)
        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")

    def test_url_bootstrap_token_with_stale_signed_activity_is_still_timed_out(self):
        """SECURITY: the SIM-SMOKE-02G override must be gone. A URL-bootstrap
        token whose SIGNED `last_activity_at` is genuinely 45 minutes old
        (past the 30-minute idle window) must still be subject to the real
        idle-timeout policy -- exactly as it was before SIM-SMOKE-02G. If the
        override were still present, this old bearer token would be able to
        "refresh" its own idle clock merely by being replayed via the URL."""
        stale_token = self._token_with_activity(stale_seconds=45 * 60)
        self.query_params[SESSION_PARAM] = stale_token

        still_active = self._run_render_app_chrome_then_real_timeout()

        self.assertFalse(still_active)
        self.rerun_mock.assert_called_once()
        self.assertTrue(self.session_state.get("user_session_expired"))
        self.assertNotIn("user_email", self.session_state)

    def test_localstorage_restore_with_stale_signed_activity_is_still_timed_out(self):
        """Same security property via the OTHER restoration path (browser
        localStorage, not a fresh URL bootstrap): a stale signed
        `last_activity_at` must still trigger the real timeout policy.
        `stamp_activity_to_token()` keeps this value fresh on every rerun for
        an ongoing session, so a stale value here represents a real idle gap
        (e.g. a laptop left closed) and must still expire the session."""
        stale_token = self._token_with_activity(stale_seconds=45 * 60)

        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._read_browser_session_token_via_js_eval",
            return_value=stale_token,
        ):
            bootstrap_signed_session()

        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        still_active = session_timeout.enforce_session_timeout()

        self.assertFalse(still_active)
        self.rerun_mock.assert_called_once()
        self.assertTrue(self.session_state.get("user_session_expired"))
        self.assertNotIn("user_email", self.session_state)

    def test_localstorage_restore_with_no_activity_field_gets_fresh_timestamp(self):
        """Same missing-field default, via the localStorage restore path."""
        token = self._token_with_activity(stale_seconds=None)

        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._read_browser_session_token_via_js_eval",
            return_value=token,
        ):
            bootstrap_signed_session()

        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertAlmostEqual(
            self.session_state.get("last_activity_at"), time.time(), delta=5
        )

    def test_invalid_token_never_authenticates_with_real_timeout_in_path(self):
        """Invalid-signature rejection must be unaffected by any of this:
        running the real `enforce_session_timeout()` afterward changes
        nothing about an invalid signature being rejected."""
        token = self._token_with_activity(stale_seconds=None)
        bad_token = _tamper_signature_bytes(token)
        self.query_params[SESSION_PARAM] = bad_token

        still_active = self._run_render_app_chrome_then_real_timeout()

        self.assertTrue(still_active)  # no session to expire -- never authenticated
        self.assertNotIn("user_email", self.session_state)
        self.assertNotIn(SESSION_PARAM, self.query_params)

    def test_completed_attempt_preserved_even_when_stale_activity_triggers_timeout(self):
        """Unrelated query parameters must survive regardless of which path
        removed `fr_session` -- including the timeout-triggered
        `clear_login_state()` path exercised by a stale signed token."""
        stale_token = self._token_with_activity(stale_seconds=45 * 60)
        self.query_params[SESSION_PARAM] = stale_token
        self.query_params[_COMPLETED_QUERY_PARAM] = _ATTEMPT_ID

        self._run_render_app_chrome_then_real_timeout()

        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertEqual(self.query_params.get(_COMPLETED_QUERY_PARAM), _ATTEMPT_ID)


class TestUrlBootstrapComponentTimingSequencing(unittest.TestCase):
    """Component-return timing coverage for the acknowledged
    browser-storage handoff (`_finalize_url_bootstrap_handoff()`), using
    tokens with no stale-activity ambiguity so this class stays entirely
    orthogonal to the idle-timeout policy covered by
    `TestActivityTimestampTimeoutPolicy`."""

    def setUp(self):
        self.session_state = _FakeSessionState()
        self.query_params = _FakeQueryParams()
        self.rerun_mock = MagicMock()
        self.fake_st = _install_fake_access_control_streamlit(
            self,
            session_state=self.session_state,
            query_params=self.query_params,
            rerun=self.rerun_mock,
            markdown=MagicMock(),
            info=MagicMock(),
            warning=MagicMock(),
            page_link=MagicMock(),
        )

    def _token(self, email: str = "learner@example.com") -> str:
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"):
            return make_signed_session({"user_email": email, "subscription_status": "active"})

    def _run_render_app_chrome(self, *, browser_ack: Optional[bool]) -> None:
        with patch("utils.access_control._signing_secret", return_value="test-signing-secret"), patch(
            "utils.access_control._write_browser_session_token_via_js_eval",
            return_value=browser_ack,
        ), patch("utils.access_control.render_sidebar_navigation"), patch(
            "utils.dashboard_components.inject_shell_theme"
        ), patch("utils.access_control.get_user_profile", return_value=None):
            render_app_chrome()

    def test_pending_ack_keeps_learner_authenticated_and_retains_url_token(self):
        """Requirement 1/3: a still-pending (`None`) browser-storage
        acknowledgment leaves the learner authenticated and retains the URL
        token rather than losing it prematurely."""
        self.query_params[SESSION_PARAM] = self._token()

        self._run_render_app_chrome(browser_ack=None)

        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertIn(SESSION_PARAM, self.query_params)
        self.assertTrue(self.session_state.get("auth_restored_from_url"))

    def test_confirmed_ack_cleans_url_and_stays_authenticated(self):
        """Requirement 2: once the browser confirms persistence, the URL is
        cleaned and the learner remains authenticated."""
        self.query_params[SESSION_PARAM] = self._token()

        self._run_render_app_chrome(browser_ack=True)

        self.assertEqual(self.session_state.get("user_email"), "learner@example.com")
        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertFalse(self.session_state.get("auth_restored_from_url"))

    def test_completed_attempt_preserved_through_confirmed_handoff(self):
        """Unrelated query parameters survive the confirmed handoff."""
        self.query_params[SESSION_PARAM] = self._token()
        self.query_params[_COMPLETED_QUERY_PARAM] = _ATTEMPT_ID

        self._run_render_app_chrome(browser_ack=True)

        self.assertNotIn(SESSION_PARAM, self.query_params)
        self.assertEqual(self.query_params.get(_COMPLETED_QUERY_PARAM), _ATTEMPT_ID)


class TestAuthSmokeDiagnostics(unittest.TestCase):
    """SIM-SMOKE-02H: the opt-in, environment-gated `_auth_smoke_trace()`
    helper. Proves it is a strict no-op by default, that enabling it only
    ever emits fixed, allowlisted event/field/value combinations, that any
    unrecognized event/field/value is silently dropped rather than printed,
    and that no token/email/URL/secret-shaped value can ever reach the
    captured output -- even when a caller (mis)attempts to pass one in."""

    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {}, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        os.environ.pop(access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR, None)

    def _captured(self, fn) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fn()
        return buf.getvalue()

    def test_disabled_by_default_emits_nothing(self):
        self.assertNotIn(access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR, os.environ)
        output = self._captured(
            lambda: access_control._auth_smoke_trace(
                "render_app_chrome_started"
            )
        )
        self.assertEqual(output, "")

    def test_explicitly_disabled_values_emit_nothing(self):
        for disabled_value in ["0", "false", "False", "no", "", "true", "TRUE", "1 "]:
            os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = disabled_value
            output = self._captured(
                lambda: access_control._auth_smoke_trace(
                    "bootstrap_started", user_email_present=True
                )
            )
            self.assertEqual(output, "", f"unexpected output for env value {disabled_value!r}")

    def test_enabled_emits_only_the_allowlisted_event_and_fields(self):
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"
        output = self._captured(
            lambda: access_control._auth_smoke_trace(
                "session_hydrated", completed=True, user_email_present=False
            )
        )
        self.assertIn("event=session_hydrated", output)
        self.assertIn("completed=True", output)
        self.assertIn("user_email_present=False", output)
        self.assertTrue(output.startswith("[certbound_auth_smoke]"))

    def test_unknown_event_name_is_dropped_entirely(self):
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"
        output = self._captured(
            lambda: access_control._auth_smoke_trace(
                "not_a_real_event", user_email="learner@example.com"
            )
        )
        self.assertEqual(output, "")

    def test_disallowed_field_name_is_silently_omitted(self):
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"
        output = self._captured(
            lambda: access_control._auth_smoke_trace(
                "bootstrap_started",
                user_email_present=True,
                token="eyJhbGciOiJIUzI1NiJ9.super-secret-value",
            )
        )
        self.assertIn("event=bootstrap_started", output)
        self.assertIn("user_email_present=True", output)
        self.assertNotIn("token", output)
        self.assertNotIn("super-secret-value", output)

    def test_disallowed_value_type_is_silently_omitted(self):
        """A field declared `bool` for its event must reject a non-bool
        value (e.g. an arbitrary string) rather than printing it raw."""
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"
        output = self._captured(
            lambda: access_control._auth_smoke_trace(
                "bootstrap_started", user_email_present="learner@example.com"
            )
        )
        self.assertIn("event=bootstrap_started", output)
        self.assertNotIn("learner@example.com", output)
        self.assertNotIn("user_email_present=learner", output)

    def test_disallowed_enum_value_is_silently_omitted(self):
        """A field declared as a fixed enum tuple must reject any value not
        in that exact tuple."""
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"
        output = self._captured(
            lambda: access_control._auth_smoke_trace(
                "token_verified", result="mostly_valid_but_not_really"
            )
        )
        self.assertIn("event=token_verified", output)
        self.assertNotIn("mostly_valid_but_not_really", output)

    def test_bool_cannot_sneak_into_int_enum_field(self):
        """`True`/`False` are `int` subtypes in Python (`True == 1`); a
        field declared with an int/enum tuple spec must still reject an
        actual bool value rather than silently accepting it as `1`/`0`."""
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"
        output = self._captured(
            lambda: access_control._auth_smoke_trace(
                "fr_session_query_state", present=True, count=True
            )
        )
        self.assertIn("present=True", output)
        self.assertNotIn("count=True", output)
        self.assertNotIn("count=1", output)

    def test_fixed_int_count_values_are_accepted(self):
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"
        output = self._captured(
            lambda: access_control._auth_smoke_trace(
                "fr_session_query_state", present=True, count=1
            )
        )
        self.assertIn("count=1", output)

    def test_more_than_one_enum_value_is_accepted(self):
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"
        output = self._captured(
            lambda: access_control._auth_smoke_trace(
                "fr_session_query_state", present=True, count="more_than_one"
            )
        )
        self.assertIn("count=more_than_one", output)

    def test_trace_call_never_raises_even_with_malformed_arguments(self):
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"
        try:
            access_control._auth_smoke_trace("token_verified", result=object())
            access_control._auth_smoke_trace(123)  # not even a string event name
        except Exception as exc:  # pragma: no cover - must never happen
            self.fail(f"_auth_smoke_trace raised unexpectedly: {exc!r}")

    def test_every_allowlisted_event_name_is_a_string_with_declared_fields_dict(self):
        for event, fields in access_control._AUTH_SMOKE_EVENT_FIELDS.items():
            self.assertIsInstance(event, str)
            self.assertIsInstance(fields, dict)
            for spec in fields.values():
                self.assertTrue(spec is bool or isinstance(spec, tuple))


class TestAuthSmokeDiagnosticsEndToEndNoLeakage(unittest.TestCase):
    """Runs the REAL, full bootstrap + timeout + access-gate sequence with
    diagnostics enabled and a genuinely sensitive token/email/secret in
    play, then asserts none of those sensitive values -- nor the token's
    signature bytes, nor the raw URL -- ever appear anywhere in the
    captured terminal output."""

    _SENSITIVE_EMAIL = "very-sensitive-learner@example.com"
    _SENSITIVE_SIGNING_SECRET = "top-secret-cookie-password-do-not-log"

    def setUp(self):
        self.session_state = _FakeSessionState()
        self.query_params = _FakeQueryParams()
        self.fake_st = _install_fake_access_control_streamlit(
            self,
            session_state=self.session_state,
            query_params=self.query_params,
            rerun=MagicMock(),
            markdown=MagicMock(),
            info=MagicMock(),
            warning=MagicMock(),
            page_link=MagicMock(),
        )
        _install_fake_session_timeout_streamlit(self, self.fake_st)
        self.env_patcher = patch.dict(
            os.environ, {access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR: "1"}
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def test_no_sensitive_value_reaches_diagnostics_output(self):
        with patch(
            "utils.access_control._signing_secret",
            return_value=self._SENSITIVE_SIGNING_SECRET,
        ):
            token = make_signed_session(
                {"user_email": self._SENSITIVE_EMAIL, "subscription_status": "active"}
            )
        self.query_params[SESSION_PARAM] = token
        token_body, token_signature = token.split(".", 1)

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with patch(
                "utils.access_control._signing_secret",
                return_value=self._SENSITIVE_SIGNING_SECRET,
            ), patch(
                "utils.access_control._write_browser_session_token_via_js_eval",
                return_value=True,
            ), patch("utils.access_control.render_sidebar_navigation"), patch(
                "utils.dashboard_components.inject_shell_theme"
            ), patch("utils.access_control.get_user_profile", return_value=None):
                render_app_chrome()
                session_timeout.enforce_session_timeout()
                access_control.require_paid_access("Scenario Simulator")

        output = buf.getvalue()
        self.assertGreater(len(output), 0)  # diagnostics really did fire
        self.assertNotIn(self._SENSITIVE_EMAIL, output)
        self.assertNotIn(self._SENSITIVE_SIGNING_SECRET, output)
        self.assertNotIn(token, output)
        self.assertNotIn(token_body, output)
        self.assertNotIn(token_signature, output)
        self.assertNotIn(SESSION_PARAM + "=", output)
        self.assertNotIn("fr_session=" + token, output)

    def test_no_sensitive_value_reaches_diagnostics_output_on_invalid_token(self):
        with patch(
            "utils.access_control._signing_secret",
            return_value=self._SENSITIVE_SIGNING_SECRET,
        ):
            token = make_signed_session(
                {"user_email": self._SENSITIVE_EMAIL, "subscription_status": "active"}
            )
        bad_token = _tamper_signature_bytes(token)
        self.query_params[SESSION_PARAM] = bad_token

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with patch(
                "utils.access_control._signing_secret",
                return_value=self._SENSITIVE_SIGNING_SECRET,
            ), patch("utils.access_control.render_sidebar_navigation"), patch(
                "utils.dashboard_components.inject_shell_theme"
            ), patch("utils.access_control.get_user_profile", return_value=None):
                render_app_chrome()

        output = buf.getvalue()
        self.assertIn("event=token_verified", output)
        self.assertIn("result=invalid", output)
        self.assertNotIn(self._SENSITIVE_EMAIL, output)
        self.assertNotIn(bad_token, output)
        self.assertNotIn(token, output)


class TestTokenVerificationDetailDiagnostics(unittest.TestCase):
    """SIM-SMOKE-02I: the `token_verification_detail` marker emitted from
    inside `verify_signed_session()` itself, one fixed rejection-category
    `reason` per branch. Every test calls the REAL, unmodified
    `verify_signed_session()` directly and asserts both (a) its public
    return value is exactly what it always was, and (b) the captured
    diagnostic output contains only the expected fixed enum -- never the
    token, body, signature, payload, email, timestamp, or any derived
    value."""

    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {}, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        os.environ.pop(access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR, None)
        self.secret_patcher = patch(
            "utils.access_control._signing_secret", return_value="test-signing-secret"
        )
        self.secret_patcher.start()
        self.addCleanup(self.secret_patcher.stop)

    def _enable(self) -> None:
        os.environ[access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR] = "1"

    def _verify_capturing(self, token: str) -> tuple[Optional[Dict[str, Any]], str]:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            result = verify_signed_session(token)
        return result, buf.getvalue()

    def _valid_token(self, email: str = "learner@example.com") -> str:
        return make_signed_session({"user_email": email, "subscription_status": "active"})

    def test_disabled_by_default_emits_no_detail_event_for_valid_token(self):
        token = self._valid_token()
        result, output = self._verify_capturing(token)
        self.assertIsNotNone(result)
        self.assertEqual(output, "")

    def test_disabled_by_default_emits_no_detail_event_for_invalid_token(self):
        token = _tamper_signature_bytes(self._valid_token())
        result, output = self._verify_capturing(token)
        self.assertIsNone(result)
        self.assertEqual(output, "")

    def test_missing_separator_reason(self):
        self._enable()
        result, output = self._verify_capturing("no-dot-in-this-token")
        self.assertIsNone(result)
        self.assertIn("event=token_verification_detail", output)
        self.assertIn("reason=missing_separator", output)

    def test_signature_decode_failed_reason(self):
        self._enable()
        valid_token = self._valid_token()
        body, _sig = valid_token.split(".", 1)
        # A single-character Base64URL "signature" always raises during
        # decode (`binascii.Error`: an invalid number of data characters
        # after this module's padding logic), regardless of Python version.
        bad_token = f"{body}.a"
        result, output = self._verify_capturing(bad_token)
        self.assertIsNone(result)
        self.assertIn("event=token_verification_detail", output)
        self.assertIn("reason=signature_decode_failed", output)

    def test_signature_mismatch_reason(self):
        self._enable()
        tampered = _tamper_signature_bytes(self._valid_token())
        result, output = self._verify_capturing(tampered)
        self.assertIsNone(result)
        self.assertIn("event=token_verification_detail", output)
        self.assertIn("reason=signature_mismatch", output)

    def test_payload_decode_failed_reason(self):
        self._enable()
        body = access_control._b64url_encode(b"not valid json{")
        token = _sign_body(body)
        result, output = self._verify_capturing(token)
        self.assertIsNone(result)
        self.assertIn("event=token_verification_detail", output)
        self.assertIn("reason=payload_decode_failed", output)

    def test_expired_reason_for_past_exp(self):
        self._enable()
        token = _custom_signed_token(
            {"user_email": "learner@example.com", "exp": int(time.time()) - 100}
        )
        result, output = self._verify_capturing(token)
        self.assertIsNone(result)
        self.assertIn("event=token_verification_detail", output)
        self.assertIn("reason=expired", output)

    def test_expired_reason_for_missing_exp(self):
        self._enable()
        token = _custom_signed_token({"user_email": "learner@example.com"})
        result, output = self._verify_capturing(token)
        self.assertIsNone(result)
        self.assertIn("event=token_verification_detail", output)
        self.assertIn("reason=expired", output)

    def test_invalid_email_reason_for_missing_email(self):
        self._enable()
        token = _custom_signed_token({"exp": int(time.time()) + 3600})
        result, output = self._verify_capturing(token)
        self.assertIsNone(result)
        self.assertIn("event=token_verification_detail", output)
        self.assertIn("reason=invalid_email", output)

    def test_invalid_email_reason_for_malformed_email(self):
        self._enable()
        token = _custom_signed_token(
            {"user_email": "not-an-email", "exp": int(time.time()) + 3600}
        )
        result, output = self._verify_capturing(token)
        self.assertIsNone(result)
        self.assertIn("event=token_verification_detail", output)
        self.assertIn("reason=invalid_email", output)

    def test_valid_reason_for_genuinely_valid_token(self):
        self._enable()
        token = self._valid_token()
        result, output = self._verify_capturing(token)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("user_email"), "learner@example.com")
        self.assertIn("event=token_verification_detail", output)
        self.assertIn("reason=valid", output)

    def test_exactly_one_detail_event_per_verification_attempt(self):
        self._enable()
        for token in [
            "no-dot-in-this-token",
            _tamper_signature_bytes(self._valid_token()),
            self._valid_token(),
        ]:
            _result, output = self._verify_capturing(token)
            self.assertEqual(
                output.count("event=token_verification_detail"),
                1,
                f"expected exactly one detail event, got output: {output!r}",
            )

    def test_no_sensitive_or_derived_data_in_detail_event(self):
        self._enable()
        sensitive_email = "very-sensitive-learner@example.com"
        with patch(
            "utils.access_control._signing_secret",
            return_value="top-secret-cookie-password",
        ):
            token = make_signed_session(
                {"user_email": sensitive_email, "subscription_status": "active"}
            )
        cases = [
            token,
            _tamper_signature_bytes(token),
            "no-dot-in-this-token",
            _custom_signed_token({"user_email": sensitive_email}),
        ]
        for case_token in cases:
            with patch(
                "utils.access_control._signing_secret",
                return_value="top-secret-cookie-password",
            ):
                _result, output = self._verify_capturing(case_token)
            self.assertNotIn(case_token, output)
            self.assertNotIn(sensitive_email, output)
            self.assertNotIn("top-secret-cookie-password", output)
            body_part = case_token.split(".", 1)[0]
            self.assertNotIn(body_part, output)
            # No timestamp, length, or fingerprint-shaped leakage either.
            self.assertNotIn(str(len(case_token)), output)

    def test_public_return_behavior_unchanged_whether_diagnostics_enabled_or_not(self):
        """The exact same inputs must return the exact same values (None or
        the same payload) regardless of the diagnostics environment
        variable -- diagnostics must never influence verification."""
        valid_token = self._valid_token()
        cases = [
            "no-dot-in-this-token",
            valid_token.split(".", 1)[0] + ".%%%not-valid-base64%%%",
            _tamper_signature_bytes(valid_token),
            _sign_body(access_control._b64url_encode(b"not valid json{")),
            _custom_signed_token({"user_email": "learner@example.com", "exp": int(time.time()) - 100}),
            _custom_signed_token({"exp": int(time.time()) + 3600}),
            valid_token,
        ]
        for case_token in cases:
            os.environ.pop(access_control._AUTH_SMOKE_DIAGNOSTICS_ENV_VAR, None)
            result_disabled = verify_signed_session(case_token)
            self._enable()
            result_enabled = verify_signed_session(case_token)
            self.assertEqual(result_disabled, result_enabled)


class TestCleanAuthenticatedNavigation(unittest.TestCase):
    """SIM-SMOKE-02B: ordinary authenticated navigation must use clean routes."""

    def setUp(self):
        self.session_state = _FakeSessionState()
        self.query_params = _FakeQueryParams()
        self.markdown_calls: list[str] = []
        self.fake_st = _install_fake_access_control_streamlit(
            self,
            session_state=self.session_state,
            query_params=self.query_params,
            markdown=lambda value, **_kwargs: self.markdown_calls.append(value),
        )

    def test_sidebar_link_href_excludes_fr_session(self):
        self.session_state["signed_session_token"] = "super-secret-token-value"
        access_control._sidebar_nav_link("pages/Dashboard.py", "Home", "🏠")
        self.assertTrue(self.markdown_calls)
        rendered = self.markdown_calls[-1]
        self.assertNotIn(SESSION_PARAM, rendered)
        self.assertNotIn("super-secret-token-value", rendered)

    def test_page_link_href_excludes_fr_session_but_keeps_extra_params(self):
        self.session_state["signed_session_token"] = "super-secret-token-value"
        access_control.render_session_page_link(
            "pages/Practice.py",
            "Practice",
            extra_params={"completed_attempt": _ATTEMPT_ID},
        )
        rendered = self.markdown_calls[-1]
        self.assertNotIn(SESSION_PARAM, rendered)
        self.assertNotIn("super-secret-token-value", rendered)
        self.assertIn("completed_attempt", rendered)
        self.assertIn(_ATTEMPT_ID, rendered)

    def test_clean_nav_href_helper_never_includes_session_param(self):
        self.session_state["signed_session_token"] = "super-secret-token-value"
        href = access_control._clean_nav_href("pages/Dashboard.py")
        self.assertNotIn(SESSION_PARAM, href)
        self.assertNotIn("super-secret-token-value", href)


class TestPendingHandoffLoginBehavior(unittest.TestCase):
    """SIM-SMOKE-02B: a pending/in-flight browser handoff must never be
    mistaken for "not signed in", and a genuinely pending cold-load restore
    must show a restoring message rather than a hard denial."""

    def setUp(self):
        self.session_state = _FakeSessionState()
        self.query_params = _FakeQueryParams()
        self.stop_calls = 0

        def _stop():
            self.stop_calls += 1
            raise SystemExit()

        self.fake_st = _install_fake_access_control_streamlit(
            self,
            session_state=self.session_state,
            query_params=self.query_params,
            info=MagicMock(),
            warning=MagicMock(),
            page_link=MagicMock(),
            stop=_stop,
        )

    def test_pending_browser_ack_does_not_trigger_login_denial(self):
        self.session_state["user_email"] = "learner@example.com"
        self.session_state["auth_restored_from_url"] = True
        email = access_control.require_login()
        self.assertEqual(email, "learner@example.com")
        access_control.st.warning.assert_not_called()
        access_control.st.info.assert_not_called()
        self.assertEqual(self.stop_calls, 0)

    def test_cold_load_pending_local_storage_read_shows_restoring_message_not_denial(self):
        self.session_state["_session_restoration_pending"] = True
        with self.assertRaises(SystemExit):
            access_control.require_login()
        access_control.st.info.assert_called_once()
        access_control.st.warning.assert_not_called()


class TestBrowserWriteAcknowledgment(unittest.TestCase):
    """Direct tests of the acknowledged browser-storage write primitive."""

    def test_confirmed_write_returns_true(self):
        with patch("streamlit_js_eval.streamlit_js_eval", return_value="ok"):
            self.assertTrue(access_control._write_browser_session_token_via_js_eval("token-value"))

    def test_pending_write_returns_none(self):
        with patch("streamlit_js_eval.streamlit_js_eval", return_value=None):
            self.assertIsNone(access_control._write_browser_session_token_via_js_eval("token-value"))

    def test_browser_reported_error_returns_false(self):
        with patch("streamlit_js_eval.streamlit_js_eval", return_value="error"):
            self.assertFalse(access_control._write_browser_session_token_via_js_eval("token-value"))

    def test_js_eval_exception_returns_false(self):
        with patch("streamlit_js_eval.streamlit_js_eval", side_effect=RuntimeError("boom")):
            self.assertFalse(access_control._write_browser_session_token_via_js_eval("token-value"))

    def test_empty_token_returns_false_without_calling_js_eval(self):
        with patch("streamlit_js_eval.streamlit_js_eval") as js_mock:
            self.assertFalse(access_control._write_browser_session_token_via_js_eval(""))
        js_mock.assert_not_called()

    def test_token_flows_only_through_js_expression_not_component_key(self):
        captured = {}

        def _fake_js_eval(js_expressions, key):
            captured["js"] = js_expressions
            captured["key"] = key
            return "ok"

        with patch("streamlit_js_eval.streamlit_js_eval", side_effect=_fake_js_eval):
            access_control._write_browser_session_token_via_js_eval("super-secret-token")
        self.assertIn("super-secret-token", captured["js"])
        self.assertNotIn("super-secret-token", captured["key"])


class TestPortalReturnUrls(unittest.TestCase):
    def test_portal_return_url_adds_harmless_marker_only(self):
        with patch("utils.billing_config._read_env", return_value=""):
            url = _portal_return_url_with_marker(
                secrets_getter=lambda name, default="": "https://app.example/Account" if name == "STRIPE_PORTAL_RETURN_URL" else default,
            )
        self.assertEqual(url, "https://app.example/Account?billing=portal")
        self.assertNotIn(SESSION_PARAM, url)
        self.assertNotIn("cus_", url)
        self.assertNotIn("sub_", url)

    def test_portal_return_url_does_not_duplicate_marker(self):
        with patch("utils.billing_config._read_env", return_value=""):
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
        self.assertIn("render_subscription_plan_summary", text)

        calls: list[str] = []

        def fake_markdown(value, **_kwargs):
            calls.append(value)

        with patch("utils.secondary_components.st.markdown", side_effect=fake_markdown):
            render_subscription_plan_summary(
                has_premium=True,
                stripe_sub_status="active",
                cancel_at_period_end=True,
            )

        self.assertTrue(calls)
        rendered = calls[0]
        self.assertIn("Cancellation scheduled", rendered)
        self.assertIn("remains active until the end of the current billing period", rendered)


class TestBrowserStorageContract(unittest.TestCase):
    def test_local_storage_key_is_stable(self):
        self.assertEqual(BROWSER_SESSION_STORAGE_KEY, "salesforce_cert_mock_fr_session")


class TestModuleStateIsolation(unittest.TestCase):
    """SIM-SMOKE-02D: proves that no test in this file ever mutates the
    real, installed `streamlit` module (or its real submodules) or leaks a
    stale/fake binding into `sys.modules`, and that `access_control.st` /
    `access_control.components` are restored to their *exact prior object*
    via scoped `unittest.mock.patch.object` cleanup -- never via clearing or
    repopulating the real module's own `__dict__` (the SIM-SMOKE-02C
    approach, which corrupted the real Streamlit module's internal state and
    produced `ImportError: cannot import name 'config' from '<unknown module
    name>'` once `utils.dashboard_components` resolved its own
    `import streamlit as st` against it).
    """

    def test_sys_modules_streamlit_identity_unchanged_by_bootstrap_test(self):
        before = sys.modules.get("streamlit")
        result = unittest.TestResult()
        TestBootstrapBehavior("test_url_restore_populates_session_without_rerun").run(result)
        self.assertFalse(result.errors, result.errors)
        self.assertFalse(result.failures, result.failures)
        self.assertIs(sys.modules.get("streamlit"), before)

    def test_sys_modules_submodule_and_dashboard_entries_unchanged_by_direct_link_test(self):
        before_streamlit = sys.modules.get("streamlit")
        before_components = sys.modules.get("streamlit.components")
        before_components_v1 = sys.modules.get("streamlit.components.v1")
        before_dashboard = sys.modules.get("utils.dashboard_components")

        result = unittest.TestResult()
        TestDirectLinkScenarioSimulatorBootstrap(
            "test_valid_direct_link_reaches_premium_access_without_app_users_lookup"
        ).run(result)
        self.assertFalse(result.errors, result.errors)
        self.assertFalse(result.failures, result.failures)

        self.assertIs(sys.modules.get("streamlit"), before_streamlit)
        self.assertIs(sys.modules.get("streamlit.components"), before_components)
        self.assertIs(sys.modules.get("streamlit.components.v1"), before_components_v1)
        self.assertIs(sys.modules.get("utils.dashboard_components"), before_dashboard)

    def test_access_control_st_restored_to_exact_prior_object_after_mutating_tests(self):
        prior_st = access_control.st
        cases = (
            TestBootstrapBehavior("test_url_restore_populates_session_without_rerun"),
            TestDirectLinkScenarioSimulatorBootstrap(
                "test_valid_direct_link_restores_before_require_paid_access"
            ),
            TestCleanAuthenticatedNavigation("test_sidebar_link_href_excludes_fr_session"),
            TestPendingHandoffLoginBehavior(
                "test_pending_browser_ack_does_not_trigger_login_denial"
            ),
        )
        for case in cases:
            result = unittest.TestResult()
            case.run(result)
            self.assertFalse(result.errors, result.errors)
            self.assertFalse(result.failures, result.failures)
            self.assertIs(access_control.st, prior_st, f"{case} left access_control.st mutated")

    def test_access_control_components_restored_to_exact_prior_object(self):
        prior_components = access_control.components
        result = unittest.TestResult()
        TestDirectLinkScenarioSimulatorBootstrap(
            "test_valid_direct_link_reaches_premium_access_without_app_users_lookup"
        ).run(result)
        self.assertFalse(result.errors, result.errors)
        self.assertFalse(result.failures, result.failures)
        self.assertIs(access_control.components, prior_components)

    def test_no_fake_attributes_remain_on_real_streamlit_module(self):
        real_streamlit = sys.modules.get("streamlit")
        self.assertIsNotNone(real_streamlit)
        original_markdown = real_streamlit.markdown

        result = unittest.TestResult()
        TestDirectLinkScenarioSimulatorBootstrap(
            "test_valid_direct_link_reaches_premium_access_without_app_users_lookup"
        ).run(result)
        self.assertFalse(result.errors, result.errors)
        self.assertFalse(result.failures, result.failures)

        self.assertIs(real_streamlit.markdown, original_markdown)
        self.assertNotIsInstance(real_streamlit.markdown, MagicMock)
        self.assertFalse(hasattr(real_streamlit, "session_state") and isinstance(
            real_streamlit.session_state, _FakeSessionState
        ))

    def test_dashboard_components_sees_healthy_streamlit_after_scoped_fake_install(self):
        """Direct isolation check: installing and removing the same scoped
        `access_control.st` / `access_control.components` fakes every
        mutating test class uses must leave `utils.dashboard_components.st`
        bound to the real, healthy Streamlit module -- without recursively
        executing other test classes."""
        prior_access_control_st = access_control.st
        prior_access_control_components = access_control.components
        before_streamlit = sys.modules.get("streamlit")
        before_components = sys.modules.get("streamlit.components")
        before_components_v1 = sys.modules.get("streamlit.components.v1")
        before_dashboard = sys.modules.get("utils.dashboard_components")

        fake_st = types.SimpleNamespace(
            session_state=_FakeSessionState(),
            query_params=_FakeQueryParams(),
            markdown=MagicMock(),
            info=MagicMock(),
            warning=MagicMock(),
            error=MagicMock(),
            page_link=MagicMock(),
            sidebar=MagicMock(),
            divider=MagicMock(),
            caption=MagicMock(),
            stop=lambda: (_ for _ in ()).throw(SystemExit()),
            rerun=lambda: (_ for _ in ()).throw(SystemExit()),
        )
        fake_components = types.SimpleNamespace(html=lambda *args, **kwargs: None)

        with patch.object(access_control, "st", fake_st), \
             patch.object(access_control, "components", fake_components):
            self.assertIs(access_control.st, fake_st)
            self.assertIs(access_control.components, fake_components)
            fake_st.session_state["user_email"] = "learner@example.com"

        self.assertIs(access_control.st, prior_access_control_st)
        self.assertIs(access_control.components, prior_access_control_components)
        self.assertIs(sys.modules.get("streamlit"), before_streamlit)
        self.assertIs(sys.modules.get("streamlit.components"), before_components)
        self.assertIs(sys.modules.get("streamlit.components.v1"), before_components_v1)
        self.assertIs(sys.modules.get("utils.dashboard_components"), before_dashboard)

        import utils.dashboard_components as dashboard_components

        real_streamlit = sys.modules["streamlit"]
        self.assertIs(dashboard_components.st, real_streamlit)
        self.assertTrue(
            hasattr(dashboard_components.st, "config"),
            "utils.dashboard_components lost access to a healthy streamlit.config",
        )
        self.assertFalse(isinstance(dashboard_components.st.markdown, MagicMock))
        self.assertIs(access_control.st, real_streamlit)

    def test_scenario_page_style_fake_streamlit_swap_is_unaffected_by_session_tests(self):
        """SIM-SMOKE-02D requirement 7: a simulated Scenario-page-style
        `patch.dict(sys.modules, {"streamlit": fake})` swap, exercised right
        after representative session tests, must still resolve `streamlit`
        to its own fake while active, and must cleanly revert to the real
        module afterward -- proving session tests leave no residual
        `sys.modules["streamlit"]` override or corrupted state behind that
        could defeat a page test's own per-test swap."""
        for case in (
            TestBootstrapBehavior("test_url_restore_populates_session_without_rerun"),
            TestDirectLinkScenarioSimulatorBootstrap(
                "test_valid_direct_link_reaches_premium_access_without_app_users_lookup"
            ),
        ):
            result = unittest.TestResult()
            case.run(result)
            self.assertFalse(result.errors, result.errors)
            self.assertFalse(result.failures, result.failures)

        real_streamlit = sys.modules["streamlit"]
        fake_page_st = types.SimpleNamespace(sentinel="page-fake")
        with patch.dict(sys.modules, {"streamlit": fake_page_st}):
            self.assertIs(__import__("streamlit"), fake_page_st)
        self.assertIs(sys.modules["streamlit"], real_streamlit)
        self.assertIsNot(sys.modules["streamlit"], fake_page_st)

    def test_cleanup_runs_even_when_test_body_raises(self):
        """SIM-SMOKE-02D requirement 8: the `addCleanup`-registered
        `patch.object` restoration must run even when the test itself
        fails, exactly like `unittest`/`addCleanup` guarantees -- proving
        the isolation mechanism does not depend on the test passing."""

        class _DeliberatelyFailingCase(unittest.TestCase):
            def setUp(self):
                self.fake_st = _install_fake_access_control_streamlit(self)

            def test_body_raises(self):
                assert access_control.st is self.fake_st
                raise AssertionError("deliberate failure to prove cleanup still runs")

        prior_st = access_control.st
        result = unittest.TestResult()
        _DeliberatelyFailingCase("test_body_raises").run(result)
        self.assertTrue(result.failures, "expected the deliberate failure to be recorded")
        self.assertIs(access_control.st, prior_st)
