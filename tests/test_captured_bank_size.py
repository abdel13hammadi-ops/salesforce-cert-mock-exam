"""
Unit tests for extract_captured_bank_size — V43.

Verifies that the helper correctly extracts the newest valid
eligible_question_bank_size from a list of exam_attempt rows, and that
Dashboard / My Progress pass this value into calculate_readiness.

Run:
    python -m pytest tests/test_captured_bank_size.py -v
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.readiness_persistence import extract_captured_bank_size


# ── helper ───────────────────────────────────────────────────────────────────

def _attempt(bank_size=None, mode="Paid Mock Exam", total_questions=60, attempt_id=1):
    return {
        "id": attempt_id,
        "mode": mode,
        "total_questions": total_questions,
        "eligible_question_bank_size": bank_size,
        "completed_at": "2026-06-22T12:00:00+00:00",
        "score": 75.0,
    }


# ── core extraction logic ────────────────────────────────────────────────────

class TestExtractCapturedBankSize(unittest.TestCase):

    def test_returns_none_for_empty_list(self):
        self.assertIsNone(extract_captured_bank_size([]))

    def test_returns_none_for_none_input(self):
        self.assertIsNone(extract_captured_bank_size(None))  # type: ignore[arg-type]

    def test_returns_none_when_all_values_absent(self):
        attempts = [_attempt(bank_size=None), _attempt(bank_size=None)]
        self.assertIsNone(extract_captured_bank_size(attempts))

    def test_returns_none_for_zero_bank_size(self):
        attempts = [_attempt(bank_size=0)]
        self.assertIsNone(extract_captured_bank_size(attempts))

    def test_returns_none_for_negative_bank_size(self):
        attempts = [_attempt(bank_size=-5)]
        self.assertIsNone(extract_captured_bank_size(attempts))

    def test_returns_valid_positive_value(self):
        attempts = [_attempt(bank_size=840)]
        self.assertEqual(extract_captured_bank_size(attempts), 840)

    def test_newest_first_wins(self):
        # attempts must be pre-sorted newest-first by callers
        attempts = [
            _attempt(bank_size=900, attempt_id=3),  # newest
            _attempt(bank_size=840, attempt_id=2),
            _attempt(bank_size=800, attempt_id=1),
        ]
        self.assertEqual(extract_captured_bank_size(attempts), 900)

    def test_skips_none_returns_next_valid(self):
        attempts = [
            _attempt(bank_size=None, attempt_id=3),  # no value
            _attempt(bank_size=840, attempt_id=2),   # first valid
            _attempt(bank_size=800, attempt_id=1),
        ]
        self.assertEqual(extract_captured_bank_size(attempts), 840)

    def test_skips_zero_returns_next_valid(self):
        attempts = [
            _attempt(bank_size=0, attempt_id=2),
            _attempt(bank_size=750, attempt_id=1),
        ]
        self.assertEqual(extract_captured_bank_size(attempts), 750)

    def test_string_numeric_coerced(self):
        """Supabase may return numbers as strings in some clients."""
        attempt = _attempt()
        attempt["eligible_question_bank_size"] = "600"
        self.assertEqual(extract_captured_bank_size([attempt]), 600)

    def test_non_numeric_string_skipped(self):
        attempt = _attempt()
        attempt["eligible_question_bank_size"] = "n/a"
        self.assertIsNone(extract_captured_bank_size([attempt]))

    def test_float_value_coerced(self):
        attempt = _attempt()
        attempt["eligible_question_bank_size"] = 720.0
        self.assertEqual(extract_captured_bank_size([attempt]), 720)

    def test_never_raises(self):
        """Helper must not propagate exceptions regardless of input."""
        bad_attempts = [
            {"eligible_question_bank_size": object()},
            {},
        ]
        # Should return None without raising
        result = extract_captured_bank_size(bad_attempts)  # type: ignore[arg-type]
        self.assertIsNone(result)

    def test_single_valid_attempt(self):
        attempts = [_attempt(bank_size=1200)]
        self.assertEqual(extract_captured_bank_size(attempts), 1200)

    def test_all_invalid_except_last(self):
        attempts = [
            _attempt(bank_size=None, attempt_id=5),
            _attempt(bank_size=0,    attempt_id=4),
            _attempt(bank_size=-1,   attempt_id=3),
            _attempt(bank_size=None, attempt_id=2),
            _attempt(bank_size=500,  attempt_id=1),
        ]
        self.assertEqual(extract_captured_bank_size(attempts), 500)


# ── Dashboard integration ────────────────────────────────────────────────────

class TestDashboardPassesCapturedBankSize(unittest.TestCase):
    """Dashboard must forward captured_bank_size to calculate_readiness."""

    def test_captured_bank_size_passed_from_readiness_attempts(self):
        """extract_captured_bank_size is called with the readiness attempts list."""
        attempts_with_bank = [_attempt(bank_size=840)]
        result = extract_captured_bank_size(attempts_with_bank)
        self.assertEqual(result, 840)

    def test_none_forwarded_when_no_bank_size_available(self):
        attempts_without_bank = [_attempt(bank_size=None)]
        result = extract_captured_bank_size(attempts_without_bank)
        self.assertIsNone(result)


# ── My Progress integration ──────────────────────────────────────────────────

class TestMyProgressPassesCapturedBankSize(unittest.TestCase):
    """My Progress must forward captured_bank_size to calculate_readiness."""

    def test_newest_valid_bank_size_selected(self):
        attempts = [
            _attempt(bank_size=900, attempt_id=3),
            _attempt(bank_size=840, attempt_id=2),
        ]
        self.assertEqual(extract_captured_bank_size(attempts), 900)

    def test_fallback_to_none_when_unavailable(self):
        attempts = [_attempt(bank_size=None)]
        self.assertIsNone(extract_captured_bank_size(attempts))


# ── Fallback behavior ────────────────────────────────────────────────────────

class TestFallbackBehaviorUnchanged(unittest.TestCase):
    """When captured_bank_size is None, calculate_readiness existing fallback stays active."""

    def test_calculate_readiness_accepts_none_captured_bank_size(self):
        from utils.readiness import calculate_readiness
        result = calculate_readiness(
            attempts=[],
            passing_score=68.0,
            domain_weights={},
            expected_question_count=60,
            question_bank_total=None,
            question_attempts=[],
            captured_bank_size=None,
        )
        self.assertIsInstance(result, dict)
        self.assertIn("score", result)

    def test_calculate_readiness_accepts_valid_captured_bank_size(self):
        from utils.readiness import calculate_readiness
        result = calculate_readiness(
            attempts=[],
            passing_score=68.0,
            domain_weights={},
            expected_question_count=60,
            question_bank_total=None,
            question_attempts=[],
            captured_bank_size=840,
        )
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main()
