"""
Canonical paid-status unit tests — V43.

Verifies that PAID_STATUS_VALUES in utils.access_control is the single
source of truth for subscription-based access decisions and that every
required status value is present.

Run:
    python -m pytest tests/test_paid_status.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.access_control import PAID_STATUS_VALUES


# ── helpers ─────────────────────────────────────────────────────────────────

def _is_paid(status: str) -> bool:
    """Mirrors the is_paid_subscription check used in app.py."""
    return str(status or "").strip().lower() in PAID_STATUS_VALUES


# ── canonical values ─────────────────────────────────────────────────────────

class TestCanonicalValues:
    """PAID_STATUS_VALUES must contain exactly the agreed set."""

    def test_active_is_paid(self):
        assert "active" in PAID_STATUS_VALUES

    def test_paid_is_paid(self):
        assert "paid" in PAID_STATUS_VALUES

    def test_premium_is_paid(self):
        assert "premium" in PAID_STATUS_VALUES

    def test_subscribed_is_paid(self):
        assert "subscribed" in PAID_STATUS_VALUES

    def test_trialing_is_paid(self):
        assert "trialing" in PAID_STATUS_VALUES

    def test_no_unexpected_values(self):
        expected = {"active", "paid", "premium", "subscribed", "trialing"}
        assert PAID_STATUS_VALUES == expected


# ── access decisions ─────────────────────────────────────────────────────────

class TestPaidAccessGranted:
    """All canonical paid statuses must receive access."""

    def test_active_receives_access(self):
        assert _is_paid("active") is True

    def test_paid_receives_access(self):
        assert _is_paid("paid") is True

    def test_premium_receives_access(self):
        assert _is_paid("premium") is True

    def test_subscribed_receives_access(self):
        assert _is_paid("subscribed") is True

    def test_trialing_receives_access(self):
        assert _is_paid("trialing") is True


class TestPaidAccessDenied:
    """Non-paid statuses must not receive access."""

    def test_free_is_denied(self):
        assert _is_paid("free") is False

    def test_empty_string_is_denied(self):
        assert _is_paid("") is False

    def test_none_is_denied(self):
        assert _is_paid(None) is False  # type: ignore[arg-type]

    def test_cancelled_is_denied(self):
        assert _is_paid("cancelled") is False

    def test_expired_is_denied(self):
        assert _is_paid("expired") is False

    def test_unknown_is_denied(self):
        assert _is_paid("unknown_status_xyz") is False

    def test_inactive_is_denied(self):
        assert _is_paid("inactive") is False


class TestCaseInsensitivity:
    """Status check must be case-insensitive."""

    def test_upper_active(self):
        assert _is_paid("ACTIVE") is True

    def test_mixed_trialing(self):
        assert _is_paid("Trialing") is True

    def test_upper_premium(self):
        assert _is_paid("PREMIUM") is True


class TestWhitespaceHandling:
    """Leading/trailing whitespace must be stripped."""

    def test_padded_paid(self):
        assert _is_paid("  paid  ") is True

    def test_padded_free(self):
        assert _is_paid("  free  ") is False
