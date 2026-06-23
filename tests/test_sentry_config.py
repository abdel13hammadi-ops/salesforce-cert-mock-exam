"""
Tests for utils/sentry_config.py — privacy-safe Sentry integration.

Covers:
  1. No DSN → no initialization
  2. Initialization is idempotent
  3. Sensitive fields are removed / redacted by before_send
  4. Exception metadata (type, stack trace, filename, lineno) is preserved
  5. Initialization failure does not crash the app

Run:
    pytest -q tests/test_sentry_config.py
    python -m unittest tests.test_sentry_config -v
"""

from __future__ import annotations

import builtins
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import sentry_sdk as _sentry_sdk_module
    HAS_SENTRY_SDK = True
except ImportError:
    HAS_SENTRY_SDK = False


def _reset_module() -> None:
    """Reset the _SENTRY_INITIALIZED flag to False between tests."""
    import utils.sentry_config as sc
    sc._SENTRY_INITIALIZED = False


def _clear_dsn_env() -> None:
    os.environ.pop("SENTRY_DSN", None)
    os.environ.pop("SENTRY_ENVIRONMENT", None)


# ---------------------------------------------------------------------------
# 1. No DSN → no initialization
# ---------------------------------------------------------------------------

class TestNoDSN(unittest.TestCase):

    def setUp(self) -> None:
        _clear_dsn_env()
        _reset_module()
        self._get_dsn_patcher = patch("utils.sentry_config._get_dsn", return_value=None)
        self._get_dsn_patcher.start()

    def tearDown(self) -> None:
        self._get_dsn_patcher.stop()
        _clear_dsn_env()
        _reset_module()

    def test_no_dsn_flag_stays_false(self) -> None:
        import utils.sentry_config as sc
        sc.init_sentry()
        self.assertFalse(sc._SENTRY_INITIALIZED)

    def test_no_dsn_does_not_raise(self) -> None:
        import utils.sentry_config as sc
        try:
            sc.init_sentry()
        except Exception as exc:
            self.fail(f"init_sentry() raised unexpectedly with no DSN: {exc}")

    def test_empty_dsn_env_not_used(self) -> None:
        self._get_dsn_patcher.stop()

        def _get_dsn_env_only():
            dsn = os.environ.get("SENTRY_DSN", "")
            if dsn and dsn.strip():
                return dsn.strip()
            return None

        self._get_dsn_patcher = patch(
            "utils.sentry_config._get_dsn",
            side_effect=_get_dsn_env_only,
        )
        self._get_dsn_patcher.start()
        os.environ["SENTRY_DSN"] = "   "
        import utils.sentry_config as sc
        sc.init_sentry()
        self.assertFalse(sc._SENTRY_INITIALIZED)


# ---------------------------------------------------------------------------
# 2. Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency(unittest.TestCase):

    def setUp(self) -> None:
        _clear_dsn_env()
        _reset_module()
        self._get_dsn_patcher = patch("utils.sentry_config._get_dsn", return_value=None)
        self._get_dsn_patcher.start()

    def tearDown(self) -> None:
        self._get_dsn_patcher.stop()
        _clear_dsn_env()
        _reset_module()

    def test_multiple_calls_no_dsn_no_crash(self) -> None:
        import utils.sentry_config as sc
        for _ in range(5):
            sc.init_sentry()
        self.assertFalse(sc._SENTRY_INITIALIZED)

    def test_flag_true_prevents_re_entry(self) -> None:
        import utils.sentry_config as sc
        sc._SENTRY_INITIALIZED = True
        # Should return immediately without touching DSN or sentry_sdk
        sc.init_sentry()
        self.assertTrue(sc._SENTRY_INITIALIZED)

    def test_flag_true_no_dsn_lookup(self) -> None:
        """When already initialized, _get_dsn must never be called."""
        import utils.sentry_config as sc
        sc._SENTRY_INITIALIZED = True
        original = sc._get_dsn
        called = []

        def spy_get_dsn():
            called.append(True)
            return original()

        sc._get_dsn = spy_get_dsn
        try:
            sc.init_sentry()
        finally:
            sc._get_dsn = original

        self.assertEqual(called, [], "_get_dsn was called despite _SENTRY_INITIALIZED=True")


# ---------------------------------------------------------------------------
# 3 & 4. before_send: sensitive fields removed, exception metadata preserved
# ---------------------------------------------------------------------------

class TestBeforeSend(unittest.TestCase):

    def setUp(self) -> None:
        from utils.sentry_config import _before_send
        self._before_send = _before_send

    def _make_event(self) -> dict:
        return {
            "user": {"email": "user@example.com", "id": "u-123"},
            "request": {
                "url": "https://certbound.app/exam?fr_session=secret_token&page=2&lang=en",
                "headers": {"Authorization": "Bearer eyJ...", "Cookie": "fr_session=x"},
                "cookies": {"fr_session": "secret_token", "session": "sess_abc"},
                "data": {"password": "hunter2", "answers": ["A", "B"]},
                "body": '{"selected_answers": ["A"]}',
                "query_string": "fr_session=secret_token&page=2",
            },
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": "exam question not found",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "app.py",
                                    "lineno": 99,
                                    "function": "fetch_question_bank",
                                }
                            ]
                        },
                    }
                ]
            },
            "release": "V38_READINESS_PERFORMANCE_ANCHORED",
            "environment": "production",
        }

    # --- PII removal ---

    def test_user_context_removed(self) -> None:
        result = self._before_send(self._make_event(), None)
        self.assertNotIn("user", result)

    def test_request_headers_removed(self) -> None:
        result = self._before_send(self._make_event(), None)
        self.assertNotIn("headers", result.get("request", {}))

    def test_request_cookies_removed(self) -> None:
        result = self._before_send(self._make_event(), None)
        self.assertNotIn("cookies", result.get("request", {}))

    def test_request_data_removed(self) -> None:
        result = self._before_send(self._make_event(), None)
        self.assertNotIn("data", result.get("request", {}))

    def test_request_body_removed(self) -> None:
        result = self._before_send(self._make_event(), None)
        self.assertNotIn("body", result.get("request", {}))

    def test_request_query_string_removed(self) -> None:
        result = self._before_send(self._make_event(), None)
        self.assertNotIn("query_string", result.get("request", {}))

    def test_fr_session_stripped_from_url(self) -> None:
        result = self._before_send(self._make_event(), None)
        url = result.get("request", {}).get("url", "")
        self.assertNotIn("fr_session", url, f"fr_session still present in URL: {url!r}")

    def test_innocent_query_params_kept_in_url(self) -> None:
        result = self._before_send(self._make_event(), None)
        url = result.get("request", {}).get("url", "")
        self.assertIn("page=2", url, f"Non-sensitive param 'page' missing from URL: {url!r}")
        self.assertIn("lang=en", url, f"Non-sensitive param 'lang' missing from URL: {url!r}")

    # --- Exception metadata preserved ---

    def test_exception_type_preserved(self) -> None:
        result = self._before_send(self._make_event(), None)
        exc_type = result["exception"]["values"][0]["type"]
        self.assertEqual(exc_type, "ValueError")

    def test_exception_value_preserved(self) -> None:
        result = self._before_send(self._make_event(), None)
        exc_value = result["exception"]["values"][0]["value"]
        self.assertEqual(exc_value, "exam question not found")

    def test_stack_trace_filename_preserved(self) -> None:
        result = self._before_send(self._make_event(), None)
        frame = result["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertEqual(frame["filename"], "app.py")

    def test_stack_trace_lineno_preserved(self) -> None:
        result = self._before_send(self._make_event(), None)
        frame = result["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertEqual(frame["lineno"], 99)

    def test_release_preserved(self) -> None:
        result = self._before_send(self._make_event(), None)
        self.assertEqual(result.get("release"), "V38_READINESS_PERFORMANCE_ANCHORED")

    def test_environment_preserved(self) -> None:
        result = self._before_send(self._make_event(), None)
        self.assertEqual(result.get("environment"), "production")

    # --- Edge cases ---

    def test_event_without_user_no_crash(self) -> None:
        event = {"exception": {"values": [{"type": "RuntimeError"}]}}
        result = self._before_send(event, None)
        self.assertIsNotNone(result)

    def test_event_without_request_no_crash(self) -> None:
        event = {"user": {"email": "x@example.com"}}
        result = self._before_send(event, None)
        self.assertIsNotNone(result)
        self.assertNotIn("user", result)

    def test_event_minimal_no_crash(self) -> None:
        result = self._before_send({}, None)
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# URL scrubbing helper
# ---------------------------------------------------------------------------

class TestStripFrSession(unittest.TestCase):

    def setUp(self) -> None:
        from utils.sentry_config import _strip_fr_session_from_url
        self._strip = _strip_fr_session_from_url

    def test_strips_fr_session_only(self) -> None:
        url = "https://app.com/page?fr_session=abc&exam=salesforce"
        result = self._strip(url)
        self.assertNotIn("fr_session", result)
        self.assertIn("exam=salesforce", result)

    def test_url_without_fr_session_unchanged_params(self) -> None:
        url = "https://app.com/page?exam=1&lang=en"
        result = self._strip(url)
        self.assertIn("exam=1", result)
        self.assertIn("lang=en", result)

    def test_none_returns_none(self) -> None:
        self.assertIsNone(self._strip(None))

    def test_empty_string_returned_as_is(self) -> None:
        self.assertEqual(self._strip(""), "")

    def test_url_with_no_query_string_unchanged(self) -> None:
        url = "https://app.com/exam"
        self.assertEqual(self._strip(url), url)

    def test_url_with_only_fr_session_has_empty_query(self) -> None:
        url = "https://app.com/page?fr_session=abc"
        result = self._strip(url)
        self.assertNotIn("fr_session", result)


# ---------------------------------------------------------------------------
# 5. Initialization failure must never crash the app
# ---------------------------------------------------------------------------

class TestInitFailureSafe(unittest.TestCase):

    def setUp(self) -> None:
        _clear_dsn_env()
        _reset_module()

    def tearDown(self) -> None:
        _clear_dsn_env()
        _reset_module()

    def test_import_error_on_sentry_sdk_does_not_crash(self) -> None:
        """Simulate sentry-sdk not installed."""
        real_import = builtins.__import__
        os.environ["SENTRY_DSN"] = "https://fake@sentry.io/0"

        def mock_import(name: str, *args, **kwargs):
            if name == "sentry_sdk":
                raise ImportError("No module named 'sentry_sdk'")
            return real_import(name, *args, **kwargs)

        import utils.sentry_config as sc
        builtins.__import__ = mock_import
        try:
            sc.init_sentry()
        except Exception as exc:
            self.fail(f"init_sentry() raised when SDK unavailable: {exc}")
        finally:
            builtins.__import__ = real_import

        self.assertFalse(sc._SENTRY_INITIALIZED)

    def test_generic_exception_in_dsn_lookup_does_not_crash(self) -> None:
        """Wrap a broken _get_dsn to verify the outer guard catches it."""
        import utils.sentry_config as sc
        original = sc._get_dsn

        def broken_get_dsn():
            raise RuntimeError("Unexpected DSN lookup failure")

        sc._get_dsn = broken_get_dsn
        try:
            # init_sentry calls _get_dsn inside try/except; must not propagate
            sc.init_sentry()
        except Exception as exc:
            self.fail(f"init_sentry() raised on broken _get_dsn: {exc}")
        finally:
            sc._get_dsn = original

    @unittest.skipUnless(HAS_SENTRY_SDK, "sentry-sdk not installed")
    def test_sentry_init_exception_does_not_crash(self) -> None:
        """Verify that a RuntimeError from sentry_sdk.init() is swallowed."""
        import utils.sentry_config as sc
        os.environ["SENTRY_DSN"] = "https://fake@sentry.io/0"

        original_init = _sentry_sdk_module.init

        def bad_init(*args, **kwargs):
            raise RuntimeError("Simulated sentry init failure")

        _sentry_sdk_module.init = bad_init
        try:
            sc.init_sentry()
        except Exception as exc:
            self.fail(f"init_sentry() propagated sentry_sdk.init() exception: {exc}")
        finally:
            _sentry_sdk_module.init = original_init

        self.assertFalse(sc._SENTRY_INITIALIZED)


if __name__ == "__main__":
    unittest.main()
