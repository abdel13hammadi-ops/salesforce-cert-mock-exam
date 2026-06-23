"""
CertBound V5 Readiness — Batch 1 unit tests.

Tests cover only the pure helpers added in Batch 1:
  v5_parse_attempt_datetime
  v5_parse_attempt_id
  v5_attempt_sort_key
  v5_is_historical_attempt
  v5_grade_attempt
  v5_grade_all_attempts
  v5_assign_evidence_weights

Run:
    python -m pytest tests/test_readiness_v5.py -v
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.readiness import (
    # Batch 1 constants
    GRADE_VERIFIED,
    GRADE_LEGACY,
    GRADE_INVALID,
    V5_MAX_SCORING_MOCKS,
    V5_MAX_REPEAT_HISTORY_MOCKS,
    V5_METADATA_THRESHOLD,
    V5_QUESTION_DISCOUNT,
    V5_QUESTION_DISCOUNT_DEFAULT,
    V5_FAMILY_DISCOUNT,
    V5_FAMILY_DISCOUNT_FLOOR,
    # Batch 2 constants
    V5_DR_DOMAIN_WEIGHT,
    V5_DR_FLOOR_WEIGHT,
    V5_DOMAIN_UNCOVERED,
    V5_DOMAIN_UNDER_SAMPLED,
    V5_DOMAIN_SUFFICIENTLY_SAMPLED,
    V5_DOMAIN_RELIABLY_SAMPLED,
    V5_DOMAIN_GAP_WEIGHT_THRESHOLD,
    V5_DOMAIN_FLOOR_SCORE_THRESHOLD,
    V5_DIFFICULTY_EASY_MIN,
    V5_DIFFICULTY_MEDIUM_MIN,
    V5_DIFFICULTY_HARD_MIN,
    V5_COGNITIVE_HO_MULTIPLIER,
    V5_COGNITIVE_HO_MIN,
    V5_TREND_CLAMP_MIN,
    V5_TREND_CLAMP_MAX,
    V5_STALENESS_CURRENT,
    V5_STALENESS_AGING,
    V5_STALENESS_OLD,
    V5_STALENESS_STALE,
    V5_STALENESS_UNKNOWN,
    V5_STALENESS_CURRENT_MAX_DAYS,
    V5_STALENESS_AGING_MAX_DAYS,
    V5_STALENESS_OLD_MAX_DAYS,
    V5_CAP_AGING_OFFSET,
    V5_CAP_OLD_OFFSET,
    V5_CAP_STALE_VALUE,
    V5_CAP_DOMAIN_GAP_OFFSET,
    V5_CAP_DOMAIN_FLOOR_OFFSET,
    V5_CAP_DOMAIN_FLOOR_MIN,
    V5_CAP_DIFFICULTY_OFFSET,
    V5_CAP_COGNITIVE_OFFSET,
    V5_CONF_MOCK_VOLUME_PTS,
    V5_CONF_BREADTH_PTS,
    V5_CONF_RECENCY_PTS,
    V5_CONF_DOMAIN_PTS,
    V5_CONF_DIFFICULTY_PTS,
    V5_CONF_COGNITIVE_PTS,
    V5_CONF_COVERAGE_MOCK_COUNT,
    # Batch 1 helpers
    _v5_parse_strict_int,
    v5_parse_attempt_datetime,
    v5_parse_attempt_id,
    v5_attempt_sort_key,
    v5_is_historical_attempt,
    v5_grade_attempt,
    v5_grade_all_attempts,
    v5_assign_evidence_weights,
    # Batch 2 helpers
    _v5_normalize_domain_weights,
    _v5_build_domain_stats,
    _v5_compute_domain_score,
    _v5_compute_difficulty_analysis,
    _v5_compute_cognitive_analysis,
    _v5_compute_trend,
    _v5_compute_staleness,
    _v5_apply_score_caps,
    _v5_compute_confidence,
)


# ---------------------------------------------------------------------------
# Shared construction helpers
# ---------------------------------------------------------------------------

def _attempt(
    attempt_id=1,
    mode="Paid Mock Exam",
    total_questions=60,
    score=70.0,
    completed_at="2026-01-10T10:00:00+00:00",
    started_at=None,
    correct_count=None,
    correct_answers=None,
):
    a = {
        "id": attempt_id,
        "mode": mode,
        "total_questions": total_questions,
        "score": score,
        "completed_at": completed_at,
        "started_at": started_at,
    }
    if correct_count is not None:
        a["correct_count"] = correct_count
    if correct_answers is not None:
        a["correct_answers"] = correct_answers
    return a


def _child_rows(attempt_id, count, distinct=None, correct_count=None):
    """Build synthetic child rows for one attempt.

    distinct: how many distinct question_ids (default = count).
    correct_count: how many rows have is_correct=True (default = count).
    """
    if distinct is None:
        distinct = count
    if correct_count is None:
        correct_count = count
    rows = []
    for i in range(count):
        qid = i if i < distinct else 0  # repeat qid=0 for duplicates
        rows.append({
            "id": i + 1,
            "exam_attempt_id": attempt_id,
            "question_id": qid,
            "is_correct": i < correct_count,
            "answered_at": f"2026-01-10T10:{i:02d}:00+00:00",
            "difficulty": "medium",
            "cognitive_level": "application",
            "question_family_id": f"fam-{i}",
        })
    return rows


def _utc(year=2026, month=1, day=10, hour=10, minute=0, second=0, microsecond=0):
    return datetime(year, month, day, hour, minute, second, microsecond,
                    tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Strict integer parser
# ---------------------------------------------------------------------------

class TestStrictIntParser(unittest.TestCase):
    """_v5_parse_strict_int must accept only plain integers and digit-only strings."""

    def test_string_10_maps_to_10(self):
        self.assertEqual(_v5_parse_strict_int("10"), 10)

    def test_int_10_maps_to_10(self):
        self.assertEqual(_v5_parse_strict_int(10), 10)

    def test_float_string_10_0_rejected(self):
        self.assertIsNone(_v5_parse_strict_int("10.0"))

    def test_float_string_10_9_rejected(self):
        self.assertIsNone(_v5_parse_strict_int("10.9"))

    def test_scientific_notation_rejected(self):
        self.assertIsNone(_v5_parse_strict_int("1e2"))

    def test_bool_true_rejected(self):
        self.assertIsNone(_v5_parse_strict_int(True))

    def test_bool_false_rejected(self):
        self.assertIsNone(_v5_parse_strict_int(False))

    def test_none_rejected(self):
        self.assertIsNone(_v5_parse_strict_int(None))

    def test_raw_float_rejected(self):
        self.assertIsNone(_v5_parse_strict_int(10.0))

    def test_uuid_rejected(self):
        self.assertIsNone(_v5_parse_strict_int("550e8400-e29b-41d4-a716-446655440000"))

    def test_arbitrary_text_rejected(self):
        self.assertIsNone(_v5_parse_strict_int("abc"))

    def test_zero_accepted(self):
        self.assertEqual(_v5_parse_strict_int(0), 0)

    def test_negative_digit_only_string_accepted(self):
        self.assertEqual(_v5_parse_strict_int("-5"), -5)

    def test_whitespace_padded_string_accepted(self):
        self.assertEqual(_v5_parse_strict_int("  42  "), 42)

    def test_whitespace_float_string_rejected(self):
        self.assertIsNone(_v5_parse_strict_int("  10.0  "))

    def test_string_9_and_10_ordered_numerically(self):
        """Numeric comparison: 9 < 10, not '9' > '10' lexicographically."""
        id9  = _v5_parse_strict_int("9")
        id10 = _v5_parse_strict_int("10")
        self.assertIsNotNone(id9)
        self.assertIsNotNone(id10)
        self.assertLess(id9, id10)


# ---------------------------------------------------------------------------
# A. Datetime parsing
# ---------------------------------------------------------------------------

class TestParseDatetime(unittest.TestCase):

    def test_z_suffix(self):
        a = {"completed_at": "2026-01-10T10:00:00Z"}
        dt = v5_parse_attempt_datetime(a)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt, _utc(2026, 1, 10, 10))

    def test_explicit_offset(self):
        a = {"completed_at": "2026-01-10T10:00:00+00:00"}
        dt = v5_parse_attempt_datetime(a)
        self.assertIsNotNone(dt)
        self.assertEqual(dt, _utc(2026, 1, 10, 10))

    def test_z_and_offset_compare_equal(self):
        a_z   = {"completed_at": "2026-01-10T10:00:00Z"}
        a_off = {"completed_at": "2026-01-10T10:00:00+00:00"}
        self.assertEqual(
            v5_parse_attempt_datetime(a_z),
            v5_parse_attempt_datetime(a_off),
        )

    def test_invalid_timestamp_returns_none(self):
        self.assertIsNone(v5_parse_attempt_datetime({"completed_at": "not-a-date"}))

    def test_missing_both_returns_none(self):
        self.assertIsNone(v5_parse_attempt_datetime({}))

    def test_started_at_fallback(self):
        a = {"started_at": "2026-01-10T09:00:00Z"}
        dt = v5_parse_attempt_datetime(a)
        self.assertIsNotNone(dt)
        self.assertEqual(dt, _utc(2026, 1, 10, 9))

    def test_completed_at_takes_precedence_over_started_at(self):
        a = {
            "completed_at": "2026-01-10T10:00:00Z",
            "started_at":   "2026-01-10T09:00:00Z",
        }
        dt = v5_parse_attempt_datetime(a)
        self.assertEqual(dt, _utc(2026, 1, 10, 10))

    def test_null_completed_at_falls_back_to_started_at(self):
        a = {"completed_at": None, "started_at": "2026-01-10T09:00:00Z"}
        dt = v5_parse_attempt_datetime(a)
        self.assertEqual(dt, _utc(2026, 1, 10, 9))

    def test_empty_string_returns_none(self):
        self.assertIsNone(v5_parse_attempt_datetime({"completed_at": ""}))

    def test_non_utc_offset_normalised_to_utc(self):
        a = {"completed_at": "2026-01-10T06:00:00-04:00"}
        dt = v5_parse_attempt_datetime(a)
        self.assertIsNotNone(dt)
        self.assertEqual(dt, _utc(2026, 1, 10, 10))

    def test_never_falls_back_to_id(self):
        # id is a valid integer but must never be used as a timestamp
        a = {"id": 99999}
        self.assertIsNone(v5_parse_attempt_datetime(a))


# ---------------------------------------------------------------------------
# B. Numeric attempt-ID parsing and ordering
# ---------------------------------------------------------------------------

class TestParseAttemptId(unittest.TestCase):

    def test_integer_id(self):
        self.assertEqual(v5_parse_attempt_id({"id": 42}), 42)

    def test_numeric_string_id(self):
        self.assertEqual(v5_parse_attempt_id({"id": "10"}), 10)

    def test_id_9_less_than_id_10(self):
        self.assertLess(
            v5_parse_attempt_id({"id": 9}),
            v5_parse_attempt_id({"id": 10}),
        )

    def test_string_9_less_than_string_10(self):
        # Ensure numeric comparison, not lexicographic ("10" < "9" lexicographically)
        id9  = v5_parse_attempt_id({"id": "9"})
        id10 = v5_parse_attempt_id({"id": "10"})
        self.assertLess(id9, id10)

    def test_uuid_rejected(self):
        self.assertIsNone(v5_parse_attempt_id({"id": "550e8400-e29b-41d4-a716-446655440000"}))

    def test_none_id_rejected(self):
        self.assertIsNone(v5_parse_attempt_id({"id": None}))

    def test_missing_id_rejected(self):
        self.assertIsNone(v5_parse_attempt_id({}))

    def test_bool_rejected(self):
        self.assertIsNone(v5_parse_attempt_id({"id": True}))

    def test_float_string_rejected(self):
        # "10.5" is not an integer ID
        result = v5_parse_attempt_id({"id": "10.5"})
        self.assertIsNone(result)

    def test_zero_id_accepted(self):
        self.assertEqual(v5_parse_attempt_id({"id": 0}), 0)

    def test_large_integer(self):
        self.assertEqual(v5_parse_attempt_id({"id": 1_000_000}), 1_000_000)


# ---------------------------------------------------------------------------
# C. Historical as-of predicate
# ---------------------------------------------------------------------------

class TestHistoricalCutoff(unittest.TestCase):
    """v5_is_historical_attempt correctness."""

    def _target(self, year=2026, month=1, day=15, hour=10, tid=100):
        return _utc(year, month, day, hour), tid

    def test_earlier_timestamp_included(self):
        attempt = _attempt(attempt_id=50, completed_at="2026-01-10T10:00:00Z")
        tdt, tid = self._target()
        self.assertTrue(v5_is_historical_attempt(attempt, tdt, tid))

    def test_later_timestamp_excluded(self):
        attempt = _attempt(attempt_id=50, completed_at="2026-01-20T10:00:00Z")
        tdt, tid = self._target()
        self.assertFalse(v5_is_historical_attempt(attempt, tdt, tid))

    def test_same_timestamp_lower_id_included(self):
        tdt = _utc(2026, 1, 15, 10)
        attempt = _attempt(attempt_id=9, completed_at="2026-01-15T10:00:00+00:00")
        self.assertTrue(v5_is_historical_attempt(attempt, tdt, 10))

    def test_same_timestamp_equal_id_included(self):
        tdt = _utc(2026, 1, 15, 10)
        attempt = _attempt(attempt_id=10, completed_at="2026-01-15T10:00:00+00:00")
        self.assertTrue(v5_is_historical_attempt(attempt, tdt, 10))

    def test_same_timestamp_higher_id_excluded(self):
        tdt = _utc(2026, 1, 15, 10)
        attempt = _attempt(attempt_id=11, completed_at="2026-01-15T10:00:00+00:00")
        self.assertFalse(v5_is_historical_attempt(attempt, tdt, 10))

    def test_id_9_before_id_10_same_timestamp(self):
        tdt = _utc(2026, 6, 1, 12)
        a9  = _attempt(attempt_id=9,  completed_at="2026-06-01T12:00:00+00:00")
        a10 = _attempt(attempt_id=10, completed_at="2026-06-01T12:00:00+00:00")
        # Snapshot for id=9: id=10 must be excluded
        self.assertFalse(v5_is_historical_attempt(a10, tdt, 9))
        # Snapshot for id=10: id=9 must be included
        self.assertTrue(v5_is_historical_attempt(a9, tdt, 10))

    def test_microsecond_earlier_included(self):
        tdt = _utc(2026, 1, 15, 10, 0, 0, microsecond=1)
        attempt = _attempt(
            attempt_id=5,
            completed_at="2026-01-15T10:00:00.000000+00:00",
        )
        self.assertTrue(v5_is_historical_attempt(attempt, tdt, 99))

    def test_microsecond_later_excluded(self):
        tdt = _utc(2026, 1, 15, 10, 0, 0, microsecond=0)
        attempt = _attempt(
            attempt_id=5,
            completed_at="2026-01-15T10:00:00.000001+00:00",
        )
        self.assertFalse(v5_is_historical_attempt(attempt, tdt, 99))

    def test_mixed_z_and_offset_formats(self):
        """Z and +00:00 represent the same instant; must compare equal."""
        tdt = _utc(2026, 1, 15, 10)
        attempt_z   = _attempt(attempt_id=5, completed_at="2026-01-15T10:00:00Z")
        attempt_off = _attempt(attempt_id=5, completed_at="2026-01-15T10:00:00+00:00")
        self.assertEqual(
            v5_is_historical_attempt(attempt_z,   tdt, 5),
            v5_is_historical_attempt(attempt_off, tdt, 5),
        )

    def test_missing_timestamp_excluded(self):
        """An attempt with no completed_at or started_at must return False."""
        attempt = {
            "id": 1,
            "mode": "Paid Mock Exam",
            "total_questions": 60,
            "score": 70.0,
        }
        tdt = _utc(2026, 1, 15, 10)
        self.assertFalse(v5_is_historical_attempt(attempt, tdt, 100))

    def test_non_numeric_id_excluded(self):
        attempt = _attempt(
            attempt_id="not-a-number",
            completed_at="2026-01-10T10:00:00Z",
        )
        tdt = _utc(2026, 1, 15, 10)
        self.assertFalse(v5_is_historical_attempt(attempt, tdt, 100))


# ---------------------------------------------------------------------------
# D. Attempt grading
# ---------------------------------------------------------------------------

class TestAttemptGrading(unittest.TestCase):

    def test_valid_verified_60_row_attempt(self):
        a = _attempt(attempt_id=1, correct_count=60)
        rows = _child_rows(1, 60, distinct=60, correct_count=60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_VERIFIED)

    def test_59_rows_is_legacy(self):
        a = _attempt(attempt_id=1)
        rows = _child_rows(1, 59)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_LEGACY)

    def test_60_rows_but_59_distinct_question_ids_is_legacy(self):
        a = _attempt(attempt_id=1)
        rows = _child_rows(1, 60, distinct=59)  # one duplicate qid
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_LEGACY)

    def test_correct_count_mismatch_is_legacy(self):
        a = _attempt(attempt_id=1, correct_count=50)
        rows = _child_rows(1, 60, distinct=60, correct_count=55)  # 55 correct, parent says 50
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_LEGACY)

    def test_correct_count_match_is_verified(self):
        a = _attempt(attempt_id=1, correct_count=42)
        rows = _child_rows(1, 60, distinct=60, correct_count=42)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_VERIFIED)

    def test_null_score_invalid(self):
        a = _attempt(attempt_id=1, score=None)
        rows = _child_rows(1, 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_score_101_invalid(self):
        a = _attempt(attempt_id=1, score=101.0)
        rows = _child_rows(1, 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_negative_score_invalid(self):
        a = _attempt(attempt_id=1, score=-1.0)
        rows = _child_rows(1, 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_score_zero_valid_can_be_verified(self):
        a = _attempt(attempt_id=1, score=0.0, correct_count=0)
        rows = _child_rows(1, 60, distinct=60, correct_count=0)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_VERIFIED)

    def test_non_paid_mode_invalid(self):
        a = _attempt(attempt_id=1, mode="Free Mock Exam")
        rows = _child_rows(1, 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_practice_mode_invalid(self):
        a = _attempt(attempt_id=1, mode="Practice by Category")
        rows = _child_rows(1, 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_short_mock_invalid(self):
        a = _attempt(attempt_id=1, total_questions=30)
        rows = _child_rows(1, 30)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_missing_timestamp_invalid(self):
        a = {
            "id": 1,
            "mode": "Paid Mock Exam",
            "total_questions": 60,
            "score": 70.0,
            "completed_at": None,
            "started_at": None,
        }
        rows = _child_rows(1, 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_invalid_timestamp_invalid(self):
        a = _attempt(attempt_id=1, completed_at="not-a-date")
        rows = _child_rows(1, 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_missing_numeric_id_invalid(self):
        a = _attempt(attempt_id="not-a-number")
        rows = _child_rows("not-a-number", 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_uuid_id_invalid(self):
        a = _attempt(attempt_id="550e8400-e29b-41d4-a716-446655440000")
        rows = _child_rows(1, 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_INVALID)

    def test_correct_answers_fallback_when_correct_count_absent(self):
        """correct_answers used when correct_count is not present."""
        a = _attempt(attempt_id=1, correct_answers=42)
        # correct_count not set → falls back to correct_answers=42
        rows = _child_rows(1, 60, distinct=60, correct_count=42)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_VERIFIED)

    def test_correct_answers_fallback_mismatch_is_legacy(self):
        a = _attempt(attempt_id=1, correct_answers=42)
        rows = _child_rows(1, 60, distinct=60, correct_count=40)  # 40 correct, expects 42
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_LEGACY)

    def test_no_correct_count_field_skips_check(self):
        """Without correct_count or correct_answers, the check is skipped."""
        a = _attempt(attempt_id=1)  # no correct_count/correct_answers
        rows = _child_rows(1, 60, distinct=60, correct_count=30)  # any number is fine
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_VERIFIED)

    def test_mode_case_insensitive(self):
        a = _attempt(attempt_id=1, mode="PAID MOCK EXAM")
        rows = _child_rows(1, 60)
        self.assertEqual(v5_grade_attempt(a, rows, 60), GRADE_VERIFIED)


# ---------------------------------------------------------------------------
# D continued — grade_all_attempts
# ---------------------------------------------------------------------------

class TestGradeAllAttempts(unittest.TestCase):

    def _make_attempts_and_rows(self):
        a_verified = _attempt(attempt_id=1, completed_at="2026-01-10T10:00:00Z")
        a_legacy   = _attempt(attempt_id=2, completed_at="2026-01-11T10:00:00Z")
        a_invalid  = _attempt(attempt_id=3, mode="Free Mock Exam",
                              completed_at="2026-01-12T10:00:00Z")
        rows_v = _child_rows(1, 60)
        rows_l = _child_rows(2, 59)   # one short → legacy
        rows_i = _child_rows(3, 60)
        return (
            [a_verified, a_legacy, a_invalid],
            rows_v + rows_l + rows_i,
        )

    def test_verified_count(self):
        attempts, rows = self._make_attempts_and_rows()
        result = v5_grade_all_attempts(attempts, rows, 60)
        self.assertEqual(len(result["verified"]), 1)

    def test_legacy_count(self):
        attempts, rows = self._make_attempts_and_rows()
        result = v5_grade_all_attempts(attempts, rows, 60)
        self.assertEqual(len(result["legacy"]), 1)

    def test_invalid_count(self):
        attempts, rows = self._make_attempts_and_rows()
        result = v5_grade_all_attempts(attempts, rows, 60)
        self.assertEqual(len(result["invalid"]), 1)

    def test_verified_ids_correct(self):
        attempts, rows = self._make_attempts_and_rows()
        result = v5_grade_all_attempts(attempts, rows, 60)
        self.assertEqual(result["verified_ids"], [1])

    def test_legacy_ids_correct(self):
        attempts, rows = self._make_attempts_and_rows()
        result = v5_grade_all_attempts(attempts, rows, 60)
        self.assertEqual(result["legacy_ids"], [2])

    def test_does_not_mutate_input_attempts(self):
        attempts, rows = self._make_attempts_and_rows()
        originals = [dict(a) for a in attempts]
        v5_grade_all_attempts(attempts, rows, 60)
        for original, current in zip(originals, attempts):
            self.assertEqual(original, current)

    def test_grade_key_added_to_copies(self):
        attempts, rows = self._make_attempts_and_rows()
        result = v5_grade_all_attempts(attempts, rows, 60)
        for a in result["verified"] + result["legacy"] + result["invalid"]:
            self.assertIn("grade", a)

    def test_empty_input(self):
        result = v5_grade_all_attempts([], [], 60)
        self.assertEqual(result["verified"], [])
        self.assertEqual(result["legacy"],   [])
        self.assertEqual(result["invalid"],  [])
        self.assertEqual(result["verified_ids"], [])
        self.assertEqual(result["legacy_ids"],   [])

    def test_invalid_child_exam_attempt_id_not_grouped_under_valid_parent(self):
        """Child row with exam_attempt_id='1.0' must NOT be grouped under parent id=1.

        int(float('1.0')) == 1, so old code would coerce it; strict parser must reject it.
        The parent then has only 59 valid child rows → legacy, not verified.
        """
        valid_rows = _child_rows(1, 59)          # 59 rows with exam_attempt_id=1
        invalid_row = {
            "id": 100,
            "exam_attempt_id": "1.0",            # float string → strict parser rejects
            "question_id": 59,
            "is_correct": True,
            "answered_at": "2026-01-10T11:00:00Z",
        }
        attempt = _attempt(attempt_id=1)
        result = v5_grade_all_attempts([attempt], valid_rows + [invalid_row], 60)
        # Parent 1 sees only 59 rows (float-string row excluded) → legacy
        self.assertEqual(len(result["legacy"]),   1, "expect 1 legacy")
        self.assertEqual(len(result["verified"]), 0, "expect 0 verified")

    def test_float_exam_attempt_id_not_grouped(self):
        """Child row with exam_attempt_id='10.9' must not be attached to parent 10."""
        valid_rows = _child_rows(10, 59)         # 59 rows for parent 10
        rogue = {
            "id": 200,
            "exam_attempt_id": "10.9",           # strictly invalid
            "question_id": 99,
            "is_correct": False,
        }
        attempt = _attempt(attempt_id=10, completed_at="2026-02-01T10:00:00Z")
        result = v5_grade_all_attempts([attempt], valid_rows + [rogue], 60)
        self.assertEqual(len(result["legacy"]),   1)
        self.assertEqual(len(result["verified"]), 0)


# ---------------------------------------------------------------------------
# E. Repeat-evidence weights
# ---------------------------------------------------------------------------

def _qa_row(row_id, exam_attempt_id, question_id, answered_at,
            family_id=None, is_correct=True):
    return {
        "id": row_id,
        "exam_attempt_id": exam_attempt_id,
        "question_id": question_id,
        "answered_at": answered_at,
        "question_family_id": family_id,
        "is_correct": is_correct,
    }


def _dt_map(pairs):
    """Build attempt_dt_map from list of (attempt_id, datetime_str)."""
    result = {}
    for aid, ts in pairs:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        result[aid] = dt.astimezone(timezone.utc)
    return result


class TestEvidenceWeights(unittest.TestCase):

    def _single_exposure(self):
        """One question, seen once in one mock. Both target and history are identical."""
        row = _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z")
        dt_map = _dt_map([(10, "2026-01-10T10:00:00Z")])
        return [row], [row], dt_map

    def test_first_exposure_weight_1_00(self):
        target, history, dtm = self._single_exposure()
        result = v5_assign_evidence_weights(target, history, dtm)
        self.assertAlmostEqual(result["weights"]["1"], 1.00)

    def test_second_exposure_weight_0_25(self):
        row1 = _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z")
        row2 = _qa_row(2, 11, "q1", "2026-01-11T10:00:00Z")
        dtm  = _dt_map([(10, "2026-01-10T10:00:00Z"), (11, "2026-01-11T10:00:00Z")])
        # Both in history; only row2 is in target (it's the repeat)
        result = v5_assign_evidence_weights([row2], [row1, row2], dtm)
        self.assertAlmostEqual(result["weights"]["2"], 0.25)

    def test_third_exposure_weight_0_00(self):
        rows = [
            _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z"),
            _qa_row(2, 11, "q1", "2026-01-11T10:00:00Z"),
            _qa_row(3, 12, "q1", "2026-01-12T10:00:00Z"),
        ]
        dtm = _dt_map([
            (10, "2026-01-10T10:00:00Z"),
            (11, "2026-01-11T10:00:00Z"),
            (12, "2026-01-12T10:00:00Z"),
        ])
        result = v5_assign_evidence_weights([rows[2]], rows, dtm)
        self.assertAlmostEqual(result["weights"]["3"], 0.00)

    def test_returned_weights_contain_only_target_rows(self):
        row1 = _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z")
        row2 = _qa_row(2, 11, "q2", "2026-01-11T10:00:00Z")
        dtm  = _dt_map([(10, "2026-01-10T10:00:00Z"), (11, "2026-01-11T10:00:00Z")])
        result = v5_assign_evidence_weights([row1], [row1, row2], dtm)
        self.assertIn("1", result["weights"])
        self.assertNotIn("2", result["weights"])

    def test_history_older_than_10_mocks_not_considered(self):
        """
        Build 11 mocks all seeing q1.  The target is mock 11 (11th exposure).
        But history is capped at the last 10 mocks → q1 in mock 11 sees rank
        depends on how the caller limits history.  Here we simulate the caller
        already having trimmed history to the last 10 rows.
        """
        # History: mocks 2–11 (10 mocks, q1 in each) — mock 1 is excluded by caller
        history = [
            _qa_row(i + 1, i + 2, "q1", f"2026-01-{i + 2:02d}T10:00:00Z")
            for i in range(10)
        ]
        target = [history[-1]]  # mock 11 is the target
        dtm = _dt_map([(i + 2, f"2026-01-{i + 2:02d}T10:00:00Z") for i in range(10)])
        result = v5_assign_evidence_weights(target, history, dtm)
        # q1 appears in all 10 history rows; its rank in the target row is 10 → weight 0.00
        row_id = str(history[-1]["id"])
        self.assertAlmostEqual(result["weights"][row_id], 0.00)

    def test_family_discount_skipped_below_90_pct(self):
        """8 of 10 rows have family_id → 80 % < 90 % threshold → no family discount."""
        rows = []
        for i in range(10):
            fid = f"fam-A" if i < 8 else None  # 8 have family_id, 2 don't
            rows.append(_qa_row(i + 1, 10, f"q{i}", f"2026-01-10T10:{i:02d}:00Z",
                                family_id=fid))
        dtm = _dt_map([(10, "2026-01-10T10:00:00Z")])
        result = v5_assign_evidence_weights(rows, rows, dtm)
        self.assertFalse(result["family_data_available"])
        # All first-exposure weights must be 1.00 (no family discount applied)
        for w in result["weights"].values():
            self.assertAlmostEqual(w, 1.00)

    def test_family_discount_activates_at_exactly_90_pct(self):
        """9 of 10 rows have family_id → exactly 90 % → family discount active."""
        rows = []
        for i in range(10):
            fid = "fam-A" if i < 9 else None  # 9 have family_id
            rows.append(_qa_row(i + 1, 10, f"q{i}", f"2026-01-10T10:{i:02d}:00Z",
                                family_id=fid))
        dtm = _dt_map([(10, "2026-01-10T10:00:00Z")])
        result = v5_assign_evidence_weights(rows, rows, dtm)
        self.assertTrue(result["family_data_available"])

    def test_second_family_mock_weight_0_70(self):
        """A fresh question (q2) from a family seen in a prior mock → family rank 2 → 0.70."""
        row1 = _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z", family_id="fam-A")
        row2 = _qa_row(2, 11, "q2", "2026-01-11T10:00:00Z", family_id="fam-A")
        # Need 90% family coverage: both rows have family_id → 100%
        dtm  = _dt_map([(10, "2026-01-10T10:00:00Z"), (11, "2026-01-11T10:00:00Z")])
        result = v5_assign_evidence_weights([row2], [row1, row2], dtm)
        # row2: question rank=1 (q2 is fresh) → q_discount=1.00
        #       family rank=2 (fam-A seen in mock 10 then mock 11) → f_discount=0.70
        self.assertAlmostEqual(result["weights"]["2"], 1.00 * 0.70)

    def test_third_family_mock_weight_0_50(self):
        """Fresh question from a family seen in two prior mocks → family rank 3 → 0.50."""
        row1 = _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z", family_id="fam-A")
        row2 = _qa_row(2, 11, "q2", "2026-01-11T10:00:00Z", family_id="fam-A")
        row3 = _qa_row(3, 12, "q3", "2026-01-12T10:00:00Z", family_id="fam-A")
        dtm  = _dt_map([
            (10, "2026-01-10T10:00:00Z"),
            (11, "2026-01-11T10:00:00Z"),
            (12, "2026-01-12T10:00:00Z"),
        ])
        result = v5_assign_evidence_weights([row3], [row1, row2, row3], dtm)
        # row3: q_discount=1.00 (q3 is fresh); f_discount=0.50 (3rd mock for fam-A)
        self.assertAlmostEqual(result["weights"]["3"], 1.00 * 0.50)

    def test_question_and_family_discounts_multiply(self):
        """A repeated question (rank 2 → 0.25) from a second-family mock (rank 2 → 0.70)
        should yield 0.25 * 0.70 = 0.175."""
        row1 = _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z", family_id="fam-A")
        row2 = _qa_row(2, 11, "q1", "2026-01-11T10:00:00Z", family_id="fam-A")
        dtm  = _dt_map([(10, "2026-01-10T10:00:00Z"), (11, "2026-01-11T10:00:00Z")])
        result = v5_assign_evidence_weights([row2], [row1, row2], dtm)
        # q_discount=0.25 (q1 repeat) * f_discount=0.70 (2nd mock for fam-A)
        self.assertAlmostEqual(result["weights"]["2"], 0.25 * 0.70, places=6)

    def test_deterministic_tie_handling_with_row_ids(self):
        """Two rows for different questions in the same mock at identical answered_at.
        Both should be first-exposure; ordering must be stable (by row id)."""
        row_a = _qa_row(1, 10, "qA", "2026-01-10T10:00:00Z")
        row_b = _qa_row(2, 10, "qB", "2026-01-10T10:00:00Z")
        dtm   = _dt_map([(10, "2026-01-10T10:00:00Z")])
        result = v5_assign_evidence_weights([row_a, row_b], [row_a, row_b], dtm)
        self.assertAlmostEqual(result["weights"]["1"], 1.00)
        self.assertAlmostEqual(result["weights"]["2"], 1.00)

    def test_cross_mock_repeat_fraction_zero_when_no_repeats(self):
        rows = [
            _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z"),
            _qa_row(2, 11, "q2", "2026-01-11T10:00:00Z"),
        ]
        dtm  = _dt_map([(10, "2026-01-10T10:00:00Z"), (11, "2026-01-11T10:00:00Z")])
        result = v5_assign_evidence_weights(rows, rows, dtm)
        self.assertAlmostEqual(result["cross_mock_repeat_fraction"], 0.0)

    def test_cross_mock_repeat_fraction_nonzero_when_repeats(self):
        row1 = _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z")
        row2 = _qa_row(2, 11, "q1", "2026-01-11T10:00:00Z")
        dtm  = _dt_map([(10, "2026-01-10T10:00:00Z"), (11, "2026-01-11T10:00:00Z")])
        result = v5_assign_evidence_weights([row1, row2], [row1, row2], dtm)
        # q1 seen in 2 mocks → 1 cross-mock repeat out of 1 unique question → 1.0
        self.assertAlmostEqual(result["cross_mock_repeat_fraction"], 1.0)

    def test_effective_target_sample_all_fresh(self):
        rows = [
            _qa_row(i + 1, 10, f"q{i}", f"2026-01-10T10:{i:02d}:00Z")
            for i in range(5)
        ]
        dtm = _dt_map([(10, "2026-01-10T10:00:00Z")])
        result = v5_assign_evidence_weights(rows, rows, dtm)
        self.assertAlmostEqual(result["effective_target_sample"], 5.0)

    def test_effective_target_sample_with_discounts(self):
        """One fresh (1.0) and one second-exposure (0.25) → effective sample = 1.25."""
        row1 = _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z")
        row2 = _qa_row(2, 11, "q1", "2026-01-11T10:00:00Z")
        dtm  = _dt_map([(10, "2026-01-10T10:00:00Z"), (11, "2026-01-11T10:00:00Z")])
        result = v5_assign_evidence_weights([row1, row2], [row1, row2], dtm)
        self.assertAlmostEqual(result["effective_target_sample"], 1.25)

    def test_empty_rows_returns_empty_weights(self):
        result = v5_assign_evidence_weights([], [], {})
        self.assertEqual(result["weights"], {})
        self.assertAlmostEqual(result["effective_target_sample"], 0.0)
        self.assertFalse(result["family_data_available"])


# ---------------------------------------------------------------------------
# E-extra. 10-mock boundary enforcement inside v5_assign_evidence_weights
# ---------------------------------------------------------------------------

class TestEvidenceWeightsBoundary(unittest.TestCase):
    """Verify that v5_assign_evidence_weights enforces the 10-mock window internally."""

    # ── helpers ──────────────────────────────────────────────────────────────

    def _build_mocks(self, n, question_id="q1", start_day=1):
        """Return (history_rows, dtm) for n mocks (attempt_ids = start_day .. start_day+n-1),
        each containing one row for question_id."""
        rows = []
        dtm_pairs = []
        for i in range(n):
            aid = start_day + i
            day = start_day + i
            ts  = f"2026-01-{day:02d}T10:00:00Z"
            rows.append(_qa_row(aid, aid, question_id, ts))
            dtm_pairs.append((aid, ts))
        return rows, _dt_map(dtm_pairs)

    # ── boundary tests ────────────────────────────────────────────────────────

    def test_11_mocks_oldest_discarded_no_rank_leak(self):
        """Pass 11 mocks in history_rows; the 10-mock boundary discards mock 1.

        q1 appears in all 11 mocks.  After discarding mock 1, q1 is seen 10
        times in the retained window.  The target row (mock 11) has rank 10 →
        weight 0.00 (third-exposure floor).
        """
        all_rows, dtm = self._build_mocks(11)   # mocks 1-11, all have q1
        target = [all_rows[-1]]                  # mock 11 row
        result = v5_assign_evidence_weights(target, all_rows, dtm)
        row_id = str(all_rows[-1]["id"])
        self.assertAlmostEqual(result["weights"][row_id], 0.00)

    def test_first_in_retained_window_gets_weight_1_00_despite_discarded_occurrence(self):
        """q1 exists in mock 1 (discarded) and in mock 11 (retained, first in window).

        After the 10-mock boundary trims mock 1, q1's rank inside the window is 1
        for its first retained appearance (mock 2).  The target row for mock 2
        must receive weight 1.00.
        """
        rows = []
        dtm_pairs = []

        # Mock 1 (will be discarded): q1
        rows.append(_qa_row(1, 1, "q1", "2026-01-01T10:00:00Z"))
        dtm_pairs.append((1, "2026-01-01T10:00:00Z"))

        # Mocks 2-10: distinct questions q2..q10 — no q1
        for i in range(9):
            aid = i + 2   # 2..10
            day = i + 2
            rows.append(_qa_row(aid, aid, f"q{aid}", f"2026-01-{day:02d}T10:00:00Z"))
            dtm_pairs.append((aid, f"2026-01-{day:02d}T10:00:00Z"))

        # Mock 11: q1 again (its first retained appearance after discarding mock 1)
        rows.append(_qa_row(11, 11, "q1", "2026-01-11T10:00:00Z"))
        dtm_pairs.append((11, "2026-01-11T10:00:00Z"))

        dtm = _dt_map(dtm_pairs)
        target = [rows[-1]]  # mock 11 row (q1, first appearance in retained window)
        result = v5_assign_evidence_weights(target, rows, dtm)
        row_id = str(rows[-1]["id"])
        # In bounded window (mocks 2-11): q1 appears only in mock 11 → rank 1 → 1.00
        self.assertAlmostEqual(result["weights"][row_id], 1.00)

    def test_mock_selection_uses_datetime_tiebreak_by_numeric_id(self):
        """When two mocks share the same datetime, numeric ID decides order.

        ID 9 sorts before ID 10 → mock 9 is older.
        If both are in a window of exactly 1, mock 9 is discarded and mock 10 is
        retained.  Here we keep both in a window of 2 but verify ordering:
        q1 in mock 9 gets rank 1 (weight 1.00) and q1 in mock 10 gets rank 2
        (weight 0.25).
        """
        common_dt = "2026-06-01T12:00:00Z"
        row9  = _qa_row(1, 9,  "q1", None)   # no answered_at → uses parent attempt dt
        row10 = _qa_row(2, 10, "q1", None)
        dtm   = _dt_map([(9, common_dt), (10, common_dt)])

        result = v5_assign_evidence_weights([row9, row10], [row9, row10], dtm)
        self.assertAlmostEqual(result["weights"]["1"], 1.00, msg="mock 9 → rank 1")
        self.assertAlmostEqual(result["weights"]["2"], 0.25, msg="mock 10 → rank 2")

    def test_ids_9_and_10_ordered_numerically_not_lexicographically(self):
        """String exam_attempt_ids '9' and '10': '9' is numerically smaller.

        Lexicographic order would put '10' before '9'.  Numeric order puts '9'
        before '10'.  The test verifies numeric ordering is used for mock selection.
        """
        common_dt = "2026-06-01T12:00:00Z"
        row_s9  = _qa_row(1, "9",  "q1", None)
        row_s10 = _qa_row(2, "10", "q1", None)
        dtm     = {9: _utc(2026, 6, 1, 12), 10: _utc(2026, 6, 1, 12)}

        result = v5_assign_evidence_weights([row_s9, row_s10], [row_s9, row_s10], dtm)
        # mock "9" (numeric 9) comes before mock "10" (numeric 10)
        # → "q1" in row_s9 → rank 1 → 1.00; in row_s10 → rank 2 → 0.25
        self.assertAlmostEqual(result["weights"]["1"], 1.00)
        self.assertAlmostEqual(result["weights"]["2"], 0.25)

    def test_target_output_contains_target_rows_only(self):
        """Weights dict must have exactly one key per target row, not for history-only rows."""
        row_history = _qa_row(1, 10, "q1", "2026-01-10T10:00:00Z")
        row_target  = _qa_row(2, 11, "q2", "2026-01-11T10:00:00Z")
        dtm = _dt_map([(10, "2026-01-10T10:00:00Z"), (11, "2026-01-11T10:00:00Z")])

        result = v5_assign_evidence_weights(
            [row_target],                    # only row_target is in target
            [row_history, row_target],       # both in history
            dtm,
        )
        self.assertEqual(set(result["weights"].keys()), {"2"})  # only row_target's key

    def test_target_row_outside_bounded_history_gets_zero_weight(self):
        """A target row from a mock older than the 10-mock window gets weight 0.0."""
        rows = []
        dtm_pairs = []

        # Build 10 mocks (1-10) all seeing q1
        for i in range(10):
            aid = i + 1
            rows.append(_qa_row(aid, aid, "q1", f"2026-01-{aid:02d}T10:00:00Z"))
            dtm_pairs.append((aid, f"2026-01-{aid:02d}T10:00:00Z"))

        # Mock 0 (older than the 10-mock window, row_id=100)
        outside_row = _qa_row(100, 0, "q_outside", "2025-12-01T10:00:00Z")
        rows.append(outside_row)
        dtm_pairs.append((0, "2025-12-01T10:00:00Z"))

        dtm = _dt_map(dtm_pairs)

        # Target: mock 0's row (outside bounded window of mocks 1-10)
        result = v5_assign_evidence_weights([outside_row], rows, dtm)
        self.assertAlmostEqual(result["weights"]["100"], 0.00)

    def test_strict_parsing_rejects_float_string_exam_attempt_id_in_history(self):
        """Rows with float-string exam_attempt_id are not counted in any mock window."""
        row_valid   = _qa_row(1, 10,    "q1", "2026-01-10T10:00:00Z")
        row_invalid = _qa_row(2, "10.0", "q1", "2026-01-10T10:05:00Z")
        dtm = _dt_map([(10, "2026-01-10T10:00:00Z")])

        # Only row_valid should be in the bounded history; row_invalid dropped.
        result = v5_assign_evidence_weights([row_valid], [row_valid, row_invalid], dtm)
        # q1 has rank 1 in the window (only row_valid counted) → weight 1.00
        self.assertAlmostEqual(result["weights"]["1"], 1.00)
        self.assertNotIn("2", result["weights"])


# ---------------------------------------------------------------------------
# G. Domain-weight normalization
# ---------------------------------------------------------------------------

class TestDomainWeightNormalization(unittest.TestCase):

    def test_percentages_normalize_correctly(self):
        r = _v5_normalize_domain_weights({"A": 50.0, "B": 25.0, "C": 25.0})
        self.assertAlmostEqual(r["A"], 0.50)
        self.assertAlmostEqual(r["B"], 0.25)
        self.assertAlmostEqual(r["C"], 0.25)

    def test_decimals_normalize_correctly(self):
        r = _v5_normalize_domain_weights({"A": 0.50, "B": 0.25, "C": 0.25})
        self.assertAlmostEqual(r["A"], 0.50)
        self.assertAlmostEqual(r["B"], 0.25)
        self.assertAlmostEqual(r["C"], 0.25)

    def test_null_negative_zero_invalid_ignored(self):
        r = _v5_normalize_domain_weights({"A": 50.0, "B": None, "C": -10.0, "D": 0.0, "E": "bad"})
        self.assertAlmostEqual(r.get("A", 0.0), 1.0)
        for k in ("B", "C", "D", "E"):
            self.assertNotIn(k, r)

    def test_empty_returns_empty(self):
        self.assertEqual(_v5_normalize_domain_weights({}), {})
        self.assertEqual(_v5_normalize_domain_weights(None), {})

    def test_all_invalid_returns_empty(self):
        self.assertEqual(_v5_normalize_domain_weights({"A": None, "B": -1, "C": 0}), {})

    def test_preserves_official_domain_names(self):
        r = _v5_normalize_domain_weights({"Sales Cloud": 30.0, "Service Cloud": 30.0, "Data Model": 40.0})
        self.assertIn("Sales Cloud", r)
        self.assertIn("Service Cloud", r)
        self.assertIn("Data Model", r)

    def test_weights_sum_to_1(self):
        r = _v5_normalize_domain_weights({"A": 10.0, "B": 20.0, "C": 30.0, "D": 40.0})
        self.assertAlmostEqual(sum(r.values()), 1.0)


# ---------------------------------------------------------------------------
# H. Domain statistics
# ---------------------------------------------------------------------------

def _make_domain_rows(attempt_id, domain, count, correct_count, weight=1.0,
                      base_id=0):
    rows = []
    wts  = {}
    for i in range(count):
        rid = base_id + i + 1
        rows.append({
            "id": rid,
            "exam_attempt_id": attempt_id,
            "question_id": f"q-{domain}-{i}",
            "category": domain,
            "difficulty": "medium",
            "cognitive_level": "application",
            "is_correct": i < correct_count,
        })
        wts[str(rid)] = weight
    return rows, wts


class TestDomainStats(unittest.TestCase):
    """_v5_build_domain_stats evidence states and boundary conditions."""

    _NW = {"Domain A": 0.50, "Domain B": 0.50}   # expected_dq = max(5, round(30)) = 30

    def _call(self, rows, wts, norm_w=None):
        return _v5_build_domain_stats(rows, wts, 60, norm_w or self._NW)

    # -- four states --

    def test_uncovered_state(self):
        rows, wts = _make_domain_rows(1, "Domain B", 5, 3)
        r = self._call(rows, wts)
        self.assertEqual(r["Domain A"]["evidence_state"], V5_DOMAIN_UNCOVERED)
        self.assertAlmostEqual(r["Domain A"]["effective_total"], 0.0)

    def test_under_sampled_state(self):
        rows, wts = _make_domain_rows(1, "Domain A", 10, 7)
        r = self._call(rows, wts)
        self.assertEqual(r["Domain A"]["evidence_state"], V5_DOMAIN_UNDER_SAMPLED)

    def test_sufficiently_sampled_state(self):
        rows, wts = _make_domain_rows(1, "Domain A", 30, 20)
        r = self._call(rows, wts)
        self.assertEqual(r["Domain A"]["evidence_state"], V5_DOMAIN_SUFFICIENTLY_SAMPLED)

    def test_reliably_sampled_state(self):
        rows, wts = _make_domain_rows(1, "Domain A", 60, 45)
        r = self._call(rows, wts)
        self.assertEqual(r["Domain A"]["evidence_state"], V5_DOMAIN_RELIABLY_SAMPLED)

    # -- exact threshold boundaries --

    def test_boundary_under_to_sufficient_at_expected_dq(self):
        # expected_dq = 30; 30 rows → sufficiently_sampled
        rows30, wts30 = _make_domain_rows(1, "Domain A", 30, 15)
        self.assertEqual(
            self._call(rows30, wts30)["Domain A"]["evidence_state"],
            V5_DOMAIN_SUFFICIENTLY_SAMPLED,
        )
        # 29 rows → under_sampled
        rows29, wts29 = _make_domain_rows(2, "Domain A", 29, 14, base_id=1000)
        self.assertEqual(
            self._call(rows29, wts29)["Domain A"]["evidence_state"],
            V5_DOMAIN_UNDER_SAMPLED,
        )

    def test_boundary_sufficient_to_reliable_at_2x_expected_dq(self):
        # expected_dq = 30; 60 rows → reliably_sampled
        rows60, wts60 = _make_domain_rows(1, "Domain A", 60, 40)
        self.assertEqual(
            self._call(rows60, wts60)["Domain A"]["evidence_state"],
            V5_DOMAIN_RELIABLY_SAMPLED,
        )
        # 59 rows → sufficiently_sampled
        rows59, wts59 = _make_domain_rows(2, "Domain A", 59, 39, base_id=1000)
        self.assertEqual(
            self._call(rows59, wts59)["Domain A"]["evidence_state"],
            V5_DOMAIN_SUFFICIENTLY_SAMPLED,
        )

    def test_expected_dq_minimum_is_5(self):
        nw = _v5_normalize_domain_weights({"Domain A": 0.01, "Domain B": 99.0})
        rows, wts = _make_domain_rows(1, "Domain A", 1, 1)
        r = _v5_build_domain_stats(rows, wts, 60, nw)
        self.assertEqual(r["Domain A"]["expected_domain_questions"], 5)

    def test_percent_correct_computed(self):
        rows, wts = _make_domain_rows(1, "Domain A", 10, 7)
        r = self._call(rows, wts)
        self.assertAlmostEqual(r["Domain A"]["percent"], 70.0)

    def test_unmapped_rows_do_not_distort_official_domains(self):
        rows_off, wts_off = _make_domain_rows(1, "Domain A", 10, 10)
        rogue = {"id": 9999, "exam_attempt_id": 1, "question_id": "q9999",
                 "category": "Unknown", "is_correct": False}
        all_rows = rows_off + [rogue]
        all_wts  = dict(wts_off); all_wts["9999"] = 1.0
        r = self._call(all_rows, all_wts)
        self.assertAlmostEqual(r["Domain A"]["effective_total"], 10.0)


# ---------------------------------------------------------------------------
# I. Domain score
# ---------------------------------------------------------------------------

def _domain_stat(et, ec, expected_dq, state):
    return {
        "effective_correct": ec, "effective_total": et,
        "percent": 100.0 * ec / et if et > 0 else 0.0,
        "expected_domain_questions": expected_dq,
        "evidence_state": state,
    }


class TestDomainScore(unittest.TestCase):

    def test_DR_exact_arithmetic(self):
        # A: weight 0.6, 80% acc; B: weight 0.4, 60% acc — both reliably_sampled
        # D = 0.6*80 + 0.4*60 = 72.0 (weights sum to 1, no renorm needed)
        # F = weakest reliable = B = 60
        # DR = 0.70*72 + 0.30*60 = 50.4 + 18 = 68.4
        stats = {
            "A": _domain_stat(60, 48, 30, V5_DOMAIN_RELIABLY_SAMPLED),
            "B": _domain_stat(40, 24, 20, V5_DOMAIN_RELIABLY_SAMPLED),
        }
        r = _v5_compute_domain_score(stats, {"A": 0.6, "B": 0.4})
        self.assertAlmostEqual(r["D"],  72.0, places=4)
        self.assertAlmostEqual(r["F"],  60.0, places=4)
        self.assertAlmostEqual(r["DR"], 68.4, places=4)

    def test_domain_gap_triggers_at_weight_exactly_0_10(self):
        stats = {
            "A": _domain_stat(50, 40, 25, V5_DOMAIN_RELIABLY_SAMPLED),
            "B": _domain_stat(0,  0,  5,  V5_DOMAIN_UNCOVERED),
        }
        r = _v5_compute_domain_score(stats, {"A": 0.90, "B": 0.10})
        self.assertTrue(r["domain_gap_triggered"])

    def test_domain_gap_not_triggered_at_weight_0_09(self):
        stats = {
            "A": _domain_stat(50, 40, 25, V5_DOMAIN_RELIABLY_SAMPLED),
            "B": _domain_stat(0,  0,  5,  V5_DOMAIN_UNCOVERED),
        }
        r = _v5_compute_domain_score(stats, {"A": 0.91, "B": 0.09})
        self.assertFalse(r["domain_gap_triggered"])

    def test_floor_triggers_at_39_99(self):
        s = _domain_stat(100, 40, 50, V5_DOMAIN_RELIABLY_SAMPLED)
        s["percent"] = 39.99
        r = _v5_compute_domain_score({"A": s}, {"A": 1.0})
        self.assertTrue(r["domain_floor_triggered"])

    def test_floor_not_triggered_at_40_0(self):
        s = _domain_stat(100, 40, 50, V5_DOMAIN_RELIABLY_SAMPLED)
        s["percent"] = 40.0
        r = _v5_compute_domain_score({"A": s}, {"A": 1.0})
        self.assertFalse(r["domain_floor_triggered"])

    def test_no_qualified_domain_F_equals_D(self):
        stats = {
            "A": _domain_stat(5, 3, 30, V5_DOMAIN_UNDER_SAMPLED),
            "B": _domain_stat(0, 0, 30, V5_DOMAIN_UNCOVERED),
        }
        r = _v5_compute_domain_score(stats, {"A": 0.5, "B": 0.5})
        self.assertAlmostEqual(r["F"], r["D"])
        self.assertIsNone(r["weakest_domain"])

    def test_no_official_weights_skips_gap(self):
        r = _v5_compute_domain_score({}, {})
        self.assertFalse(r["domain_gap_triggered"])

    def test_uncovered_domains_list_populated(self):
        stats = {
            "A": _domain_stat(50, 40, 25, V5_DOMAIN_RELIABLY_SAMPLED),
            "B": _domain_stat(0,  0,  5,  V5_DOMAIN_UNCOVERED),
        }
        r = _v5_compute_domain_score(stats, {"A": 0.8, "B": 0.2})
        self.assertIn("B", r["uncovered_domains"])


# ---------------------------------------------------------------------------
# J. Difficulty evidence analysis
# ---------------------------------------------------------------------------

def _diff_rows_weights(counts, correct_counts=None, base_id=0):
    """Build rows+weights for difficulty analysis. counts={'easy':n,...}"""
    rows, wts = [], {}
    rid = base_id
    for diff, n in counts.items():
        correct = (correct_counts or {}).get(diff, n)
        for i in range(n):
            rid += 1
            rows.append({"id": rid, "question_id": f"qd{rid}",
                         "difficulty": diff, "is_correct": i < correct})
            wts[str(rid)] = 1.0
    return rows, wts


class TestDifficultyAnalysis(unittest.TestCase):

    def test_activates_at_exactly_90_pct(self):
        # 90 recognized + 10 unknown = 100 rows → 90% exactly
        rows, wts = _diff_rows_weights({"easy": 30, "medium": 30, "hard": 30})
        for i in range(10):
            rid = 1000 + i
            rows.append({"id": rid, "question_id": f"qu{rid}",
                         "difficulty": "unknown_level", "is_correct": True})
            wts[str(rid)] = 1.0
        r = _v5_compute_difficulty_analysis(rows, wts)
        self.assertTrue(r["data_available"])
        self.assertAlmostEqual(r["metadata_coverage"], 0.90, places=10)

    def test_not_active_at_89_pct(self):
        # 89 recognized + 11 unknown = 100 → 89%
        rows, wts = _diff_rows_weights({"easy": 30, "medium": 30, "hard": 29})
        for i in range(11):
            rid = 1000 + i
            rows.append({"id": rid, "question_id": f"qu{rid}",
                         "difficulty": "unknown_level", "is_correct": True})
            wts[str(rid)] = 1.0
        r = _v5_compute_difficulty_analysis(rows, wts)
        self.assertFalse(r["data_available"])
        self.assertFalse(r["cap_active"])

    def test_hard_sufficient_at_exactly_10(self):
        rows, wts = _diff_rows_weights({"easy": 5, "medium": 10, "hard": 10})
        r = _v5_compute_difficulty_analysis(rows, wts)
        self.assertTrue(r["data_available"])
        self.assertTrue(r["hard_sufficient"])
        self.assertFalse(r["cap_active"])

    def test_hard_not_sufficient_at_9_99(self):
        # 10 hard rows each with weight 0.999 → effective_total = 9.99 < 10
        rows, wts = [], {}
        for i in range(10):
            rid = i + 1
            rows.append({"id": rid, "question_id": f"qh{rid}",
                         "difficulty": "hard", "is_correct": True})
            wts[str(rid)] = 0.999
        for i in range(10):
            rid = 100 + i
            rows.append({"id": rid, "question_id": f"qe{rid}",
                         "difficulty": "easy", "is_correct": True})
            wts[str(rid)] = 0.6    # 6.0 ≥ 5 → easy sufficient
        for i in range(10):
            rid = 200 + i
            rows.append({"id": rid, "question_id": f"qm{rid}",
                         "difficulty": "medium", "is_correct": True})
            wts[str(rid)] = 1.0    # 10.0 → medium sufficient
        r = _v5_compute_difficulty_analysis(rows, wts)
        self.assertTrue(r["data_available"])
        self.assertFalse(r["hard_sufficient"])
        self.assertTrue(r["cap_active"])

    def test_cap_active_false_when_hard_sufficient(self):
        rows, wts = _diff_rows_weights({"easy": 5, "medium": 10, "hard": 10})
        r = _v5_compute_difficulty_analysis(rows, wts)
        self.assertFalse(r["cap_active"])

    def test_no_metadata_no_cap(self):
        r = _v5_compute_difficulty_analysis([], {})
        self.assertFalse(r["cap_active"])
        self.assertAlmostEqual(r["confidence_fraction"], 0.0)

    def test_confidence_fraction_max_1_when_all_sufficient(self):
        rows, wts = _diff_rows_weights({"easy": 10, "medium": 10, "hard": 10})
        r = _v5_compute_difficulty_analysis(rows, wts)
        self.assertAlmostEqual(r["confidence_fraction"], 1.0)


# ---------------------------------------------------------------------------
# K. Cognitive evidence analysis
# ---------------------------------------------------------------------------

def _cog_rows_weights(level_counts, base_id=0):
    rows, wts = [], {}
    rid = base_id
    for lv, n in level_counts.items():
        for _ in range(n):
            rid += 1
            rows.append({"id": rid, "question_id": f"qc{rid}",
                         "cognitive_level": lv, "is_correct": True})
            wts[str(rid)] = 1.0
    return rows, wts


class TestCognitiveAnalysis(unittest.TestCase):

    def test_activates_at_exactly_90_pct(self):
        # 18 recognized + 2 unknown = 20 rows → 90%
        rows, wts = _cog_rows_weights(
            {"recall": 5, "understanding": 5, "application": 5, "analysis": 3})
        for i in range(2):
            rid = 2000 + i
            rows.append({"id": rid, "question_id": f"qu{rid}",
                         "cognitive_level": "badlevel", "is_correct": True})
            wts[str(rid)] = 1.0
        r = _v5_compute_cognitive_analysis(rows, wts, 60)
        self.assertTrue(r["data_available"])
        self.assertAlmostEqual(r["metadata_coverage"], 0.90, places=10)

    def test_not_active_at_89_pct(self):
        # 89 recognized + 11 unknown = 100 rows → 89%
        rows, wts = [], {}
        levels = ["recall", "application", "analysis", "judgment", "understanding"]
        for i in range(89):
            rid = i + 1
            rows.append({"id": rid, "question_id": f"q{rid}",
                         "cognitive_level": levels[i % 5], "is_correct": True})
            wts[str(rid)] = 1.0
        for i in range(11):
            rid = 1000 + i
            rows.append({"id": rid, "question_id": f"qu{rid}",
                         "cognitive_level": "unknown_level", "is_correct": True})
            wts[str(rid)] = 1.0
        r = _v5_compute_cognitive_analysis(rows, wts, 60)
        self.assertFalse(r["data_available"])

    def test_higher_order_target_for_60_questions(self):
        # max(10, floor(60 * 0.30)) = max(10, 18) = 18
        rows, wts = _cog_rows_weights({"application": 20, "recall": 40})
        r = _v5_compute_cognitive_analysis(rows, wts, 60)
        self.assertEqual(r["higher_order_target"], 18)

    def test_higher_order_target_minimum_10(self):
        # max(10, floor(25 * 0.30)) = max(10, 7) = 10
        rows, wts = _cog_rows_weights({"application": 5, "recall": 20})
        r = _v5_compute_cognitive_analysis(rows, wts, 25)
        self.assertEqual(r["higher_order_target"], 10)

    def test_cap_active_when_ho_below_target(self):
        # target=18 for 60q; 17 ho rows → cap active
        rows, wts = _cog_rows_weights({"application": 17, "recall": 43})
        r = _v5_compute_cognitive_analysis(rows, wts, 60)
        self.assertTrue(r["data_available"])
        self.assertTrue(r["cap_active"])

    def test_cap_not_active_at_exactly_target(self):
        # 18 ho rows → cap not active
        rows, wts = _cog_rows_weights({"application": 18, "recall": 42})
        r = _v5_compute_cognitive_analysis(rows, wts, 60)
        self.assertTrue(r["data_available"])
        self.assertFalse(r["cap_active"])

    def test_no_metadata_no_cap(self):
        r = _v5_compute_cognitive_analysis([], {}, 60)
        self.assertFalse(r["cap_active"])
        self.assertAlmostEqual(r["confidence_fraction"], 0.0)

    def test_confidence_fraction_at_exactly_target(self):
        rows, wts = _cog_rows_weights({"application": 18, "recall": 42})
        r = _v5_compute_cognitive_analysis(rows, wts, 60)
        self.assertAlmostEqual(r["confidence_fraction"], 1.0)


# ---------------------------------------------------------------------------
# L. Trend helper
# ---------------------------------------------------------------------------

class TestTrend(unittest.TestCase):

    def test_yoyo_trend_is_declining(self):
        # [45, 90, 50]: delta = 50 - mean(45, 90) = -17.5 → Declining; clamped adj = -4.0
        r = _v5_compute_trend([45.0, 90.0, 50.0])
        self.assertAlmostEqual(r["trend_delta"],      -17.5)
        self.assertAlmostEqual(r["trend_adjustment"],  -4.0)   # clamped at V5_TREND_CLAMP_MIN
        self.assertEqual(r["trend_label"], "Declining")

    def test_stable_trend(self):
        # [70, 75, 73]: delta = 73 - 72.5 = 0.5 → Stable
        r = _v5_compute_trend([70.0, 75.0, 73.0])
        self.assertAlmostEqual(r["trend_delta"], 0.5)
        self.assertEqual(r["trend_label"], "Stable")

    def test_improving_trend(self):
        # [50, 60, 85]: delta = 85 - 55 = 30 → Improving; clamped adj = +2.0
        r = _v5_compute_trend([50.0, 60.0, 85.0])
        self.assertAlmostEqual(r["trend_delta"], 30.0)
        self.assertAlmostEqual(r["trend_adjustment"], 2.0)
        self.assertEqual(r["trend_label"], "Improving")

    def test_exactly_minus_2_is_declining(self):
        # [80, 70, 73]: delta = 73 - 75 = -2 → Declining
        r = _v5_compute_trend([80.0, 70.0, 73.0])
        self.assertAlmostEqual(r["trend_delta"], -2.0)
        self.assertEqual(r["trend_label"], "Declining")

    def test_exactly_plus_2_is_improving(self):
        # [50, 60, 67]: delta = 67 - 55 = 12 → Improving
        r = _v5_compute_trend([50.0, 60.0, 67.0])
        self.assertEqual(r["trend_label"], "Improving")

    def test_four_scores_formula(self):
        # [60, 70, 80, 85]: mean(last 2)=82.5, mean(earlier 2)=65 → delta=17.5
        r = _v5_compute_trend([60.0, 70.0, 80.0, 85.0])
        self.assertAlmostEqual(r["trend_delta"], 17.5)

    def test_five_scores_formula(self):
        # [60, 65, 70, 75, 80]: mean(last 2)=77.5, mean(earlier 3)=65 → delta=12.5
        r = _v5_compute_trend([60.0, 65.0, 70.0, 75.0, 80.0])
        self.assertAlmostEqual(r["trend_delta"], 12.5)

    def test_adjustment_clamped_positive(self):
        r = _v5_compute_trend([50.0, 60.0, 100.0])
        self.assertAlmostEqual(r["trend_adjustment"], V5_TREND_CLAMP_MAX)

    def test_adjustment_clamped_negative(self):
        r = _v5_compute_trend([90.0, 80.0, 40.0])
        self.assertAlmostEqual(r["trend_adjustment"], V5_TREND_CLAMP_MIN)

    def test_fewer_than_3_scores_zero_delta(self):
        self.assertAlmostEqual(_v5_compute_trend([80.0])["trend_delta"], 0.0)
        self.assertAlmostEqual(_v5_compute_trend([80.0, 85.0])["trend_delta"], 0.0)


# ---------------------------------------------------------------------------
# M. Staleness helper
# ---------------------------------------------------------------------------

class TestStaleness(unittest.TestCase):

    def _now(self):
        return _utc(2026, 6, 1, 12)

    def _ago(self, days):
        return self._now() - timedelta(days=days)

    def test_current_at_0_days(self):
        self.assertEqual(_v5_compute_staleness(self._ago(0), self._now())["state"], V5_STALENESS_CURRENT)

    def test_current_at_90_days(self):
        self.assertEqual(_v5_compute_staleness(self._ago(90), self._now())["state"], V5_STALENESS_CURRENT)

    def test_aging_at_91_days(self):
        self.assertEqual(_v5_compute_staleness(self._ago(91), self._now())["state"], V5_STALENESS_AGING)

    def test_aging_at_180_days(self):
        self.assertEqual(_v5_compute_staleness(self._ago(180), self._now())["state"], V5_STALENESS_AGING)

    def test_old_at_181_days(self):
        self.assertEqual(_v5_compute_staleness(self._ago(181), self._now())["state"], V5_STALENESS_OLD)

    def test_old_at_365_days(self):
        self.assertEqual(_v5_compute_staleness(self._ago(365), self._now())["state"], V5_STALENESS_OLD)

    def test_stale_at_366_days(self):
        self.assertEqual(_v5_compute_staleness(self._ago(366), self._now())["state"], V5_STALENESS_STALE)

    def test_unknown_when_no_datetime(self):
        r = _v5_compute_staleness(None)
        self.assertEqual(r["state"], V5_STALENESS_UNKNOWN)
        self.assertIsNone(r["age_days"])

    def test_not_classified_as_current_when_missing(self):
        self.assertNotEqual(_v5_compute_staleness(None)["state"], V5_STALENESS_CURRENT)

    def test_age_days_returned_correctly(self):
        self.assertEqual(_v5_compute_staleness(self._ago(120), self._now())["age_days"], 120)


# ---------------------------------------------------------------------------
# N. Score caps
# ---------------------------------------------------------------------------

class TestScoreCaps(unittest.TestCase):

    def _cap(self, score=90.0, passing=65.0, staleness="current",
             domain_gap=False, domain_floor=False, diff=False, cog=False):
        return _v5_apply_score_caps(score, passing, staleness,
                                    domain_gap, domain_floor, diff, cog)

    def test_no_caps_no_guardrail(self):
        r = self._cap(score=80.0, staleness=V5_STALENESS_CURRENT)
        self.assertAlmostEqual(r["final_score"], 80.0)
        self.assertFalse(r["guardrail_applied"])
        self.assertIsNone(r["guardrail_cap"])

    def test_aging_cap(self):
        # 65 + 7 = 72
        r = self._cap(score=90.0, passing=65.0, staleness=V5_STALENESS_AGING)
        self.assertAlmostEqual(r["final_score"], 72.0)
        self.assertTrue(r["guardrail_applied"])

    def test_old_cap(self):
        # 65 - 3 = 62
        r = self._cap(score=90.0, passing=65.0, staleness=V5_STALENESS_OLD)
        self.assertAlmostEqual(r["final_score"], 62.0)

    def test_stale_cap_zero(self):
        r = self._cap(score=90.0, staleness=V5_STALENESS_STALE)
        self.assertAlmostEqual(r["final_score"], 0.0)

    def test_domain_gap_cap(self):
        # 65 - 3 = 62
        r = self._cap(score=90.0, passing=65.0, domain_gap=True)
        self.assertAlmostEqual(r["final_score"], 62.0)

    def test_domain_floor_cap(self):
        # max(65-5, 50) = max(60, 50) = 60
        r = self._cap(score=90.0, passing=65.0, domain_floor=True)
        self.assertAlmostEqual(r["final_score"], 60.0)

    def test_domain_floor_cap_min_50(self):
        # max(50-5, 50) = max(45, 50) = 50
        r = self._cap(score=90.0, passing=50.0, domain_floor=True)
        self.assertAlmostEqual(r["final_score"], 50.0)

    def test_difficulty_cap(self):
        # 65 - 1 = 64
        r = self._cap(score=90.0, passing=65.0, diff=True)
        self.assertAlmostEqual(r["final_score"], 64.0)

    def test_cognitive_cap(self):
        # 65 + 7 = 72
        r = self._cap(score=90.0, passing=65.0, cog=True)
        self.assertAlmostEqual(r["final_score"], 72.0)

    def test_multiple_caps_lowest_wins(self):
        # aging=72, domain_gap=62 → guardrail=62
        r = self._cap(score=90.0, passing=65.0,
                      staleness=V5_STALENESS_AGING, domain_gap=True)
        self.assertAlmostEqual(r["final_score"], 62.0)
        self.assertEqual(len(r["applied_caps"]), 2)

    def test_score_below_cap_not_raised(self):
        # score 50 < aging cap 72 → final stays 50, guardrail not applied
        r = self._cap(score=50.0, passing=65.0, staleness=V5_STALENESS_AGING)
        self.assertAlmostEqual(r["final_score"], 50.0)
        self.assertFalse(r["guardrail_applied"])

    def test_applied_caps_lists_all_applicable(self):
        r = self._cap(score=90.0, passing=65.0, domain_gap=True, diff=True)
        reasons = [c["reason"] for c in r["applied_caps"]]
        self.assertIn("domain_gap", reasons)
        self.assertIn("difficulty_insufficient", reasons)


# ---------------------------------------------------------------------------
# O. Confidence helper
# ---------------------------------------------------------------------------

class TestConfidence(unittest.TestCase):

    def _perfect_domain_stats(self, domains):
        return {
            d: {"effective_correct": 48.0, "effective_total": 60.0, "percent": 80.0,
                "expected_domain_questions": 30, "evidence_state": V5_DOMAIN_RELIABLY_SAMPLED}
            for d in domains
        }

    def test_components_total_exactly_100_at_maximum(self):
        """All components at max → score = 100."""
        verified = [_attempt(i + 1, completed_at=f"2026-01-{i+1:02d}T10:00:00Z")
                    for i in range(5)]
        history = [{"id": i, "question_id": f"q{i}", "exam_attempt_id": 1}
                   for i in range(600)]
        nw = {"A": 0.5, "B": 0.5}
        ds = self._perfect_domain_stats(nw)
        r = _v5_compute_confidence(
            verified_attempts=verified,
            history_rows=history,
            domain_stats=ds,
            normalized_weights=nw,
            difficulty_analysis={"confidence_fraction": 1.0},
            cognitive_analysis={"confidence_fraction": 1.0},
            staleness_state=V5_STALENESS_CURRENT,
            age_days=0,
            captured_bank_size=600,
            live_bank_size=None,
            expected_question_count=60,
        )
        self.assertAlmostEqual(r["score"], 100.0, places=1)

    def test_coverage_target_bank_840_gives_600(self):
        r = _v5_compute_confidence(
            verified_attempts=[_attempt(1)], history_rows=[],
            domain_stats={}, normalized_weights={},
            difficulty_analysis={"confidence_fraction": 0.0},
            cognitive_analysis={"confidence_fraction": 0.0},
            staleness_state=V5_STALENESS_CURRENT, age_days=0,
            captured_bank_size=840, live_bank_size=None, expected_question_count=60,
        )
        self.assertEqual(r["coverage_target"], 600)

    def test_coverage_target_bank_1200_gives_600(self):
        r = _v5_compute_confidence(
            verified_attempts=[_attempt(1)], history_rows=[],
            domain_stats={}, normalized_weights={},
            difficulty_analysis={"confidence_fraction": 0.0},
            cognitive_analysis={"confidence_fraction": 0.0},
            staleness_state=V5_STALENESS_CURRENT, age_days=0,
            captured_bank_size=1200, live_bank_size=None, expected_question_count=60,
        )
        self.assertEqual(r["coverage_target"], 600)

    def test_unknown_recency_gives_zero_recency_pts(self):
        r = _v5_compute_confidence(
            verified_attempts=[], history_rows=[],
            domain_stats={}, normalized_weights={},
            difficulty_analysis={"confidence_fraction": 0.0},
            cognitive_analysis={"confidence_fraction": 0.0},
            staleness_state=V5_STALENESS_UNKNOWN, age_days=None,
            captured_bank_size=None, live_bank_size=None, expected_question_count=60,
        )
        self.assertAlmostEqual(r["recency_pts"], 0.0)

    def test_no_metadata_difficulty_cognitive_pts_zero(self):
        r = _v5_compute_confidence(
            verified_attempts=[], history_rows=[],
            domain_stats={}, normalized_weights={},
            difficulty_analysis={"confidence_fraction": 0.0},
            cognitive_analysis={"confidence_fraction": 0.0},
            staleness_state=V5_STALENESS_CURRENT, age_days=0,
            captured_bank_size=None, live_bank_size=None, expected_question_count=60,
        )
        self.assertAlmostEqual(r["difficulty_pts"], 0.0)
        self.assertAlmostEqual(r["cognitive_pts"],  0.0)

    def test_bank_fallback_used_when_captured_none(self):
        r = _v5_compute_confidence(
            verified_attempts=[], history_rows=[],
            domain_stats={}, normalized_weights={},
            difficulty_analysis={"confidence_fraction": 0.0},
            cognitive_analysis={"confidence_fraction": 0.0},
            staleness_state=V5_STALENESS_CURRENT, age_days=0,
            captured_bank_size=None, live_bank_size=500, expected_question_count=60,
        )
        self.assertTrue(r["bank_fallback_used"])

    def test_bank_fallback_not_used_when_captured_present(self):
        r = _v5_compute_confidence(
            verified_attempts=[], history_rows=[],
            domain_stats={}, normalized_weights={},
            difficulty_analysis={"confidence_fraction": 0.0},
            cognitive_analysis={"confidence_fraction": 0.0},
            staleness_state=V5_STALENESS_CURRENT, age_days=0,
            captured_bank_size=600, live_bank_size=500, expected_question_count=60,
        )
        self.assertFalse(r["bank_fallback_used"])

    def test_component_scores_non_negative_and_sum_lte_100(self):
        verified = [_attempt(i + 1, completed_at=f"2026-01-{i+1:02d}T10:00:00Z")
                    for i in range(3)]
        r = _v5_compute_confidence(
            verified_attempts=verified, history_rows=[],
            domain_stats={}, normalized_weights={},
            difficulty_analysis={"confidence_fraction": 0.5},
            cognitive_analysis={"confidence_fraction": 0.5},
            staleness_state=V5_STALENESS_AGING, age_days=120,
            captured_bank_size=600, live_bank_size=None, expected_question_count=60,
        )
        for key in ("mock_volume_pts", "breadth_pts", "recency_pts",
                    "domain_pts", "difficulty_pts", "cognitive_pts"):
            self.assertGreaterEqual(r[key], 0.0, msg=key)
        total = sum(r[k] for k in ("mock_volume_pts", "breadth_pts", "recency_pts",
                                   "domain_pts", "difficulty_pts", "cognitive_pts"))
        self.assertLessEqual(total, 100.01)


# ---------------------------------------------------------------------------
# F. Constants sanity
# ---------------------------------------------------------------------------

class TestConstants(unittest.TestCase):

    def test_grade_strings_distinct(self):
        self.assertNotEqual(GRADE_VERIFIED, GRADE_LEGACY)
        self.assertNotEqual(GRADE_VERIFIED, GRADE_INVALID)
        self.assertNotEqual(GRADE_LEGACY,   GRADE_INVALID)

    def test_max_scoring_mocks(self):
        self.assertEqual(V5_MAX_SCORING_MOCKS, 5)

    def test_max_repeat_history_mocks(self):
        self.assertEqual(V5_MAX_REPEAT_HISTORY_MOCKS, 10)

    def test_metadata_threshold(self):
        self.assertAlmostEqual(V5_METADATA_THRESHOLD, 0.90)

    def test_question_discounts(self):
        self.assertAlmostEqual(V5_QUESTION_DISCOUNT[1], 1.00)
        self.assertAlmostEqual(V5_QUESTION_DISCOUNT[2], 0.25)
        self.assertAlmostEqual(V5_QUESTION_DISCOUNT_DEFAULT, 0.00)

    def test_family_discounts(self):
        self.assertAlmostEqual(V5_FAMILY_DISCOUNT[1], 1.00)
        self.assertAlmostEqual(V5_FAMILY_DISCOUNT[2], 0.70)
        self.assertAlmostEqual(V5_FAMILY_DISCOUNT_FLOOR, 0.50)

    def test_confidence_components_sum_to_100(self):
        total = (V5_CONF_MOCK_VOLUME_PTS + V5_CONF_BREADTH_PTS + V5_CONF_RECENCY_PTS +
                 V5_CONF_DOMAIN_PTS + V5_CONF_DIFFICULTY_PTS + V5_CONF_COGNITIVE_PTS)
        self.assertEqual(total, 100)

    def test_dr_weights_sum_to_1(self):
        self.assertAlmostEqual(V5_DR_DOMAIN_WEIGHT + V5_DR_FLOOR_WEIGHT, 1.0)

    def test_staleness_boundaries_ordered(self):
        self.assertLess(V5_STALENESS_CURRENT_MAX_DAYS, V5_STALENESS_AGING_MAX_DAYS)
        self.assertLess(V5_STALENESS_AGING_MAX_DAYS,   V5_STALENESS_OLD_MAX_DAYS)


if __name__ == "__main__":
    unittest.main()
