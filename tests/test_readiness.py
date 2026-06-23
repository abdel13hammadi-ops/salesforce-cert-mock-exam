"""
CertBound V42 Readiness — integration tests for calculate_readiness.

Tests that exercise Batch 1/2 helper logic in isolation live in
tests/test_readiness_v5.py.  This file covers the wired-up
calculate_readiness function (payload, unlock rules, scoring,
caps, backward compat) and a small number of end-to-end scenarios.

Run:
    python -m pytest tests/test_readiness.py -v
"""

import sys
import os
import math
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.readiness import (
    REQUIRED_FULL_MOCKS,
    READINESS_VERSION,
    calculate_readiness,
    is_full_mock_attempt,
    _compute_ema,
    _normalize_weights,
    _compute_pacing_diagnostics,
    readiness_methodology_text,
    V5_STALENESS_STALE,
    V5_STALENESS_AGING,
    V5_STALENESS_OLD,
    V5_STALENESS_CURRENT,
    V5_STALENESS_CURRENT_MAX_DAYS,
    V5_STALENESS_AGING_MAX_DAYS,
    V5_STALENESS_OLD_MAX_DAYS,
    V5_CAP_AGING_OFFSET,
    V5_CAP_OLD_OFFSET,
    V5_METADATA_THRESHOLD,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_days_ago(n: int) -> str:
    """Return ISO8601 UTC string for a date n days before today."""
    dt = datetime.now(tz=timezone.utc) - timedelta(days=n)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mock(
    score,
    mode="Paid Mock Exam",
    total_questions=60,
    completed_at=None,
    attempt_id=None,
):
    completed_at = completed_at or _utc_days_ago(30)
    return {
        "id": str(attempt_id) if attempt_id is not None else str(score),
        "mode": mode,
        "score": score,
        "total_questions": total_questions,
        "completed_at": completed_at,
        "domain_breakdown": {},
    }


def _qattempt(
    exam_attempt_id,
    question_id,
    category="Domain A",
    is_correct=True,
    time_spent_seconds=80.0,
):
    return {
        "exam_attempt_id": exam_attempt_id,
        "question_id": question_id,
        "category": category,
        "is_correct": is_correct,
        "time_spent_seconds": time_spent_seconds,
    }


def _verified_child_rows(
    attempt_id,
    total_q=60,
    score=None,
    time_spent_seconds=None,
    category="Domain A",
):
    """Return *total_q* child rows that satisfy v5_grade_attempt VERIFIED criteria.

    Difficulty and cognitive levels are distributed so no analysis cap is
    triggered in the default case:
        - rows 0–4    : easy / recall          (5 rows)
        - rows 5–14   : hard / analysis        (10 rows, higher-order)
        - rows 15–34  : medium / application   (20 rows, higher-order)
        - rows 35+    : medium / understanding (remaining rows)
    """
    correct_count = (
        round(score * total_q / 100.0) if score is not None else total_q // 2
    )
    rows = []
    for j in range(total_q):
        if j < 5:
            diff, cog = "easy", "recall"
        elif j < 15:
            diff, cog = "hard", "analysis"
        elif j < 35:
            diff, cog = "medium", "application"
        else:
            diff, cog = "medium", "understanding"

        row = {
            "id": int(attempt_id) * 10000 + j,
            "exam_attempt_id": str(attempt_id),
            "question_id": f"q_a{attempt_id}_r{j}",
            "is_correct": j < correct_count,
            "category": category,
            "difficulty": diff,
            "cognitive_level": cog,
        }
        if time_spent_seconds is not None:
            row["time_spent_seconds"] = time_spent_seconds
        rows.append(row)
    return rows


def _readiness(
    scores,
    domain_weights=None,
    question_attempts=None,
    question_bank_total=None,
    passing_score=68,
    time_limit_minutes=105,
    expected_question_count=60,
    modes=None,
    completed_ats=None,
    captured_bank_size=None,
):
    """Build attempts list and call calculate_readiness.

    When *question_attempts* is None the helper auto-generates one full set of
    verified child rows per 'Paid Mock Exam' attempt so that each mock grades
    as GRADE_VERIFIED in the V5 engine.
    """
    modes = modes or ["Paid Mock Exam"] * len(scores)
    completed_ats = completed_ats or [
        _utc_days_ago(max(1, 20 - i)) for i in range(len(scores))
    ]
    attempts = [
        _mock(s, mode=m, completed_at=d, attempt_id=i)
        for i, (s, m, d) in enumerate(zip(scores, modes, completed_ats))
    ]
    if question_attempts is None:
        qa = []
        for i, (s, m) in enumerate(zip(scores, modes)):
            if m == "Paid Mock Exam":
                qa.extend(
                    _verified_child_rows(i, total_q=expected_question_count, score=s)
                )
        question_attempts = qa

    return calculate_readiness(
        attempts=attempts,
        passing_score=passing_score,
        domain_weights=domain_weights,
        expected_question_count=expected_question_count,
        question_bank_total=question_bank_total,
        question_attempts=question_attempts,
        time_limit_minutes=time_limit_minutes,
        captured_bank_size=captured_bank_size,
    )


# ---------------------------------------------------------------------------
# 1. Locking / unlock rules
# ---------------------------------------------------------------------------

class TestLocking(unittest.TestCase):

    def test_locked_with_zero_mocks(self):
        r = _readiness([])
        self.assertTrue(r["is_locked"])
        self.assertEqual(r["score"], 0.0)
        self.assertIn("Locked", r["label"])

    def test_locked_with_two_verified_mocks(self):
        r = _readiness([70.0, 75.0])
        self.assertTrue(r["is_locked"])
        self.assertEqual(r["score"], 0.0)

    def test_unlocked_with_three_verified_mocks(self):
        r = _readiness([60.0, 65.0, 70.0])
        self.assertFalse(r["is_locked"])
        self.assertGreater(r["score"], 0.0)

    def test_locked_exposes_eligible_and_remaining(self):
        r = _readiness([50.0])
        self.assertEqual(r["eligible_mock_count"], 1)
        self.assertEqual(r["required_mock_count"], REQUIRED_FULL_MOCKS)
        self.assertEqual(r["mocks_remaining"], REQUIRED_FULL_MOCKS - 1)

    def test_three_legacy_mocks_remain_locked(self):
        """Mocks without matching child rows grade as LEGACY and stay locked."""
        attempts = [
            _mock(70.0, attempt_id=i, completed_at=_utc_days_ago(20 - i))
            for i in range(3)
        ]
        # Only 20 child rows per mock (< total_questions=60) → LEGACY
        qa = [
            {"id": i * 100 + j, "exam_attempt_id": str(i),
             "question_id": f"q{i}_{j}", "is_correct": True}
            for i in range(3) for j in range(20)
        ]
        r = calculate_readiness(attempts=attempts, question_attempts=qa)
        self.assertTrue(r["is_locked"])
        self.assertEqual(r["legacy_mock_count"], 3)
        self.assertEqual(r["verified_mock_count"], 0)

    def test_two_verified_one_legacy_remain_locked(self):
        """Two verified + one legacy = 2 verified < 3 required → locked."""
        attempts = [
            _mock(70.0, attempt_id=i, completed_at=_utc_days_ago(20 - i))
            for i in range(3)
        ]
        qa = []
        # Mocks 0 and 1: 60 verified child rows each
        for i in range(2):
            qa.extend(_verified_child_rows(i, score=70.0))
        # Mock 2: only 10 rows → LEGACY
        qa.extend([
            {"id": 20000 + j, "exam_attempt_id": "2",
             "question_id": f"q2_{j}", "is_correct": True}
            for j in range(10)
        ])
        r = calculate_readiness(attempts=attempts, question_attempts=qa)
        self.assertTrue(r["is_locked"])
        self.assertEqual(r["verified_mock_count"], 2)
        self.assertEqual(r["legacy_mock_count"], 1)

    def test_three_verified_mocks_unlock(self):
        """Exactly 3 verified mocks must unlock readiness."""
        attempts = [
            _mock(75.0, attempt_id=i, completed_at=_utc_days_ago(20 - i))
            for i in range(3)
        ]
        qa = []
        for i in range(3):
            qa.extend(_verified_child_rows(i, score=75.0))
        r = calculate_readiness(attempts=attempts, question_attempts=qa)
        self.assertFalse(r["is_locked"])
        self.assertEqual(r["verified_mock_count"], 3)
        self.assertGreater(r["score"], 0.0)


# ---------------------------------------------------------------------------
# 2. Legacy scores do not contaminate scoring
# ---------------------------------------------------------------------------

class TestLegacyIsolation(unittest.TestCase):

    def _build(self, verified_scores, legacy_score):
        """3 verified mocks + 1 legacy mock (no child rows)."""
        attempts = []
        qa = []
        for i, s in enumerate(verified_scores):
            attempts.append(_mock(s, attempt_id=i, completed_at=_utc_days_ago(25 - i)))
            qa.extend(_verified_child_rows(i, score=s))
        # Legacy mock: no child rows
        legacy_id = len(verified_scores)
        attempts.append(
            _mock(legacy_score, attempt_id=legacy_id,
                  completed_at=_utc_days_ago(5))
        )
        return calculate_readiness(attempts=attempts, question_attempts=qa)

    def test_legacy_score_does_not_affect_ema(self):
        # EMA of verified [60,65,70] should be identical regardless of a high legacy score
        r_with_legacy = self._build([60.0, 65.0, 70.0], legacy_score=99.0)
        r_clean = _readiness([60.0, 65.0, 70.0])
        self.assertAlmostEqual(
            r_with_legacy["recent_accuracy"], r_clean["recent_accuracy"], places=1
        )

    def test_legacy_score_does_not_affect_trend(self):
        r_with_legacy = self._build([60.0, 65.0, 70.0], legacy_score=99.0)
        r_clean = _readiness([60.0, 65.0, 70.0])
        self.assertAlmostEqual(
            r_with_legacy["trend_delta"], r_clean["trend_delta"], places=1
        )

    def test_legacy_counted_in_diagnostics(self):
        r = self._build([60.0, 65.0, 70.0], legacy_score=50.0)
        self.assertEqual(r["legacy_mock_count"], 1)
        self.assertEqual(r["verified_mock_count"], 3)


# ---------------------------------------------------------------------------
# 3. Verified attempt sorting
# ---------------------------------------------------------------------------

class TestVerifiedSorting(unittest.TestCase):

    def test_sorting_uses_parsed_datetime_and_numeric_id(self):
        """Attempts supplied in reverse date order must sort oldest→newest internally
        so that EMA and trend are computed in the correct direction."""
        # Supply 3 verified mocks in REVERSE chronological order
        completed_ats = [
            _utc_days_ago(3),   # newest  → should be last in sorted order
            _utc_days_ago(15),  # middle
            _utc_days_ago(30),  # oldest  → should be first in sorted order
        ]
        scores = [90.0, 60.0, 30.0]   # newest=90, oldest=30
        qa = []
        for i, s in enumerate(scores):
            qa.extend(_verified_child_rows(i, score=s))
        attempts = [
            _mock(s, attempt_id=i, completed_at=d)
            for i, (s, d) in enumerate(zip(scores, completed_ats))
        ]
        r = calculate_readiness(attempts=attempts, question_attempts=qa)
        self.assertFalse(r["is_locked"])
        # EMA (oldest→newest) for sorted [30,60,90]: heavily weighted toward 90
        # If sorted wrong (newest first) EMA would lean toward 30
        self.assertGreater(r["recent_accuracy"], 60.0,
                           "EMA should be weighted toward the most recent (highest) score")


# ---------------------------------------------------------------------------
# 4. High coverage cannot inflate low accuracy
# ---------------------------------------------------------------------------

class TestCoverageDoesNotInflate(unittest.TestCase):

    def test_high_coverage_low_accuracy_stays_near_accuracy(self):
        scores = [22.0, 25.0, 18.0, 16.0, 11.67]
        r = _readiness(scores, question_bank_total=189)
        self.assertLess(r["score"], 25.0,
                        f"Readiness {r['score']} is inflated above accuracy")

    def test_observed_certbound_case(self):
        scores = [26.67, 20.0, 18.0, 16.0, 11.67]
        r = _readiness(scores)
        self.assertLess(r["score"], 22.0,
                        f"Score {r['score']} still too high for 18% performer")
        self.assertGreaterEqual(r["score"], 0.0)


# ---------------------------------------------------------------------------
# 5. Fast guesser — pacing diagnostic only, no score bonus
# ---------------------------------------------------------------------------

class TestFastGuesserPacing(unittest.TestCase):

    def test_fast_wrong_does_not_add_readiness(self):
        scores = [25.0, 22.0, 20.0]
        # 60 rows per mock, unique question_ids, 2 s each, all wrong
        qa = [
            _qattempt(str(i % 3), str(i), is_correct=False, time_spent_seconds=2.0)
            for i in range(180)
        ]
        r = _readiness(scores, question_attempts=qa)
        self.assertEqual(r["pacing_status"], "Too Fast / Likely Guessing")
        self.assertEqual(r["pacing_score"], 0.0)
        ema_approx = _compute_ema(scores)
        self.assertLessEqual(r["score"], ema_approx + 5.1)

    def test_pacing_score_key_always_zero(self):
        r = _readiness([70.0, 72.0, 75.0])
        self.assertEqual(r["pacing_score"], 0.0)


# ---------------------------------------------------------------------------
# 6. Slow accurate candidate receives no readiness penalty
# ---------------------------------------------------------------------------

class TestSlowAccurate(unittest.TestCase):

    def test_slow_but_correct_no_readiness_penalty(self):
        scores = [78.0, 80.0, 79.0]
        target = 105 * 60 / 60
        qa = [
            _qattempt(str(i % 3), str(i), is_correct=True,
                      time_spent_seconds=target * 1.6)
            for i in range(180)
        ]
        r = _readiness(scores, question_attempts=qa)
        ema = _compute_ema(scores)
        self.assertGreaterEqual(r["score"], ema - 6.0,
                                "Slow accurate candidate unfairly penalized")


# ---------------------------------------------------------------------------
# 7. Trend adjustments (V5 clamp: −4.0 … +2.0)
# ---------------------------------------------------------------------------

class TestTrend(unittest.TestCase):

    def test_improving_trend_gets_small_positive(self):
        improving = [45.0, 52.0, 60.0, 68.0, 74.0]
        declining  = [74.0, 68.0, 60.0, 52.0, 45.0]
        r_imp = _readiness(improving)
        r_dec = _readiness(declining)
        self.assertGreater(r_imp["score"], r_dec["score"],
                           "Improving trend should yield higher readiness than declining")
        self.assertEqual(r_imp["trend_label"], "Improving")

    def test_declining_trend_gets_negative(self):
        r = _readiness([78.0, 74.0, 65.0, 58.0, 50.0])
        self.assertLess(r["trend_adjustment"], 0.0)
        self.assertEqual(r["trend_label"], "Declining")

    def test_improving_credit_capped_at_v5_max(self):
        # V5 cap: trend_adjustment <= +2.0
        r = _readiness([40.0, 55.0, 70.0, 85.0, 99.0])
        self.assertLessEqual(r["trend_adjustment"], 2.01)

    def test_declining_penalty_floored_at_v5_min(self):
        # V5 floor: trend_adjustment >= −4.0
        r = _readiness([90.0, 75.0, 60.0, 45.0, 30.0])
        self.assertGreaterEqual(r["trend_adjustment"], -4.01)

    def test_trend_delta_equals_trend_slope(self):
        r = _readiness([60.0, 65.0, 70.0, 75.0, 80.0])
        self.assertAlmostEqual(r["trend_delta"], r["trend_slope"], places=5)


# ---------------------------------------------------------------------------
# 8. Consistency penalty
# ---------------------------------------------------------------------------

class TestConsistencyPenalty(unittest.TestCase):

    def test_inconsistent_gets_penalty(self):
        inconsistent = [40.0, 85.0, 45.0, 82.0, 50.0]
        consistent   = [60.0, 62.0, 61.0, 63.0, 62.0]
        r_inc = _readiness(inconsistent)
        r_con = _readiness(consistent)
        self.assertGreater(r_inc["consistency_penalty"], 0.0)
        self.assertGreater(r_con["score"], r_inc["score"],
                           "Consistent performer should outscore inconsistent one")

    def test_small_variation_no_penalty(self):
        r = _readiness([70.0, 72.0, 71.0, 73.0, 70.0])
        self.assertEqual(r["consistency_penalty"], 0.0)


# ---------------------------------------------------------------------------
# 9. Weak reliable domain reduces domain robustness
# ---------------------------------------------------------------------------

class TestWeakDomain(unittest.TestCase):

    def _build_3_mocks_with_weak_domain_c(self):
        """3 verified mocks × 60 rows each, distributed 20/20/20 across A/B/C.
        Domain C has only 3/20 correct → accuracy ≈ 15 %.
        Returns (attempts, qa, domain_weights).
        """
        dw = {"Domain A": 33.33, "Domain B": 33.33, "Domain C": 33.34}
        attempts = []
        qa = []
        for j in range(3):
            attempts.append(
                _mock(75.0, attempt_id=j, completed_at=_utc_days_ago(20 - j))
            )
            base = j * 10000
            for k in range(20):
                qa.append({
                    "id": base + k,
                    "exam_attempt_id": str(j),
                    "question_id": f"da_{j}_{k}",
                    "is_correct": True,
                    "category": "Domain A",
                    "difficulty": "medium",
                    "cognitive_level": "application",
                })
            for k in range(20):
                qa.append({
                    "id": base + 100 + k,
                    "exam_attempt_id": str(j),
                    "question_id": f"db_{j}_{k}",
                    "is_correct": True,
                    "category": "Domain B",
                    "difficulty": "hard",
                    "cognitive_level": "analysis",
                })
            for k in range(20):
                qa.append({
                    "id": base + 200 + k,
                    "exam_attempt_id": str(j),
                    "question_id": f"dc_{j}_{k}",
                    "is_correct": k < 3,   # only 3/20 correct
                    "category": "Domain C",
                    "difficulty": "easy" if k < 5 else "medium",
                    "cognitive_level": "recall",
                })
        return attempts, qa, dw

    def test_weak_reliable_domain_reduces_dr(self):
        attempts, qa, dw = self._build_3_mocks_with_weak_domain_c()
        r = calculate_readiness(
            attempts=attempts,
            question_attempts=qa,
            domain_weights=dw,
            passing_score=68,
            expected_question_count=60,
        )
        self.assertFalse(r["is_locked"])
        self.assertLess(
            r["domain_robustness"], r["domain_score"],
            "Weak reliable domain should pull DR below D",
        )


# ---------------------------------------------------------------------------
# 10. Sparse domain does not create exaggerated floor
# ---------------------------------------------------------------------------

class TestSparseDomainNoFloor(unittest.TestCase):

    def test_sparse_domain_excluded_from_floor(self):
        """Domain C (1 row per mock) is under-sampled and must not qualify as
        the weakest reliable domain.  Domain A (59 rows per mock, all correct)
        is the only qualified domain; DR must not be pulled below D."""
        dw = {"Domain A": 90, "Domain C": 10}
        attempts = []
        qa = []
        for j in range(3):
            attempts.append(
                _mock(70.0, attempt_id=j, completed_at=_utc_days_ago(20 - j))
            )
            base = j * 10000
            for k in range(59):
                qa.append({
                    "id": base + k,
                    "exam_attempt_id": str(j),
                    "question_id": f"da_{j}_{k}",
                    "is_correct": True,
                    "category": "Domain A",
                    "difficulty": "hard" if k < 10 else ("easy" if k < 15 else "medium"),
                    "cognitive_level": "analysis" if k < 10 else "application" if k < 30 else "understanding",
                })
            # Only 1 Domain C row per mock → sparse
            qa.append({
                "id": base + 200,
                "exam_attempt_id": str(j),
                "question_id": f"dc_{j}_0",
                "is_correct": False,
                "category": "Domain C",
                "difficulty": "medium",
                "cognitive_level": "recall",
            })

        r = calculate_readiness(
            attempts=attempts,
            question_attempts=qa,
            domain_weights=dw,
            expected_question_count=60,
        )
        self.assertNotEqual(
            r["weakest_reliable_domain"], "Domain C",
            "Sparse Domain C must not qualify as a reliable weak domain",
        )
        self.assertGreaterEqual(
            r["domain_robustness"], r["domain_score"] - 0.1,
            "Sparse bad domain must not drag DR below D",
        )


# ---------------------------------------------------------------------------
# 11. Readiness hard cap: never exceeds A + 5
# ---------------------------------------------------------------------------

class TestHardCap(unittest.TestCase):

    def test_score_never_exceeds_ema_plus_5(self):
        for scores in [
            [70.0, 75.0, 80.0, 85.0, 90.0],
            [50.0, 60.0, 70.0, 80.0, 90.0],
            [80.0, 80.0, 80.0, 80.0, 80.0],
        ]:
            r = _readiness(scores)
            ema = _compute_ema(scores)
            self.assertLessEqual(
                r["score"], ema + 5.1,
                f"Score {r['score']} exceeds EMA {ema}+5 for {scores}",
            )


# ---------------------------------------------------------------------------
# 12. Staleness locking and caps
# ---------------------------------------------------------------------------

class TestStaleness(unittest.TestCase):

    def test_stale_evidence_locks_score_zero_and_label(self):
        """3 verified mocks all > 365 days old → stale → score 0, label 'Evidence Stale'."""
        attempts = []
        qa = []
        for i in range(3):
            attempts.append(
                _mock(80.0, attempt_id=i, completed_at=_utc_days_ago(400 + i))
            )
            qa.extend(_verified_child_rows(i, score=80.0))
        r = calculate_readiness(attempts=attempts, question_attempts=qa)
        self.assertTrue(r["is_locked"])
        self.assertEqual(r["score"], 0.0)
        self.assertEqual(r["label"], "Evidence Stale")
        self.assertTrue(r["staleness_locked"])
        self.assertEqual(r["staleness_state"], V5_STALENESS_STALE)

    def test_aging_cap_applied(self):
        """Newest mock 91-180 days old → aging cap = passing_score + V5_CAP_AGING_OFFSET."""
        passing = 65.0
        days_old = V5_STALENESS_CURRENT_MAX_DAYS + 20   # inside aging window
        attempts = []
        qa = []
        for i in range(3):
            attempts.append(
                _mock(95.0, attempt_id=i,
                      completed_at=_utc_days_ago(days_old + i))
            )
            qa.extend(_verified_child_rows(i, score=95.0))
        r = calculate_readiness(
            attempts=attempts, question_attempts=qa, passing_score=passing
        )
        self.assertFalse(r["staleness_locked"])
        expected_cap = passing + V5_CAP_AGING_OFFSET
        self.assertLessEqual(r["score"], expected_cap + 0.01,
                             "Aging cap should limit score to passing + CAP_AGING_OFFSET")

    def test_old_cap_applied(self):
        """Newest mock 181-365 days old → old cap = passing_score + V5_CAP_OLD_OFFSET."""
        passing = 65.0
        days_old = V5_STALENESS_AGING_MAX_DAYS + 20   # inside old window
        attempts = []
        qa = []
        for i in range(3):
            attempts.append(
                _mock(95.0, attempt_id=i,
                      completed_at=_utc_days_ago(days_old + i))
            )
            qa.extend(_verified_child_rows(i, score=95.0))
        r = calculate_readiness(
            attempts=attempts, question_attempts=qa, passing_score=passing
        )
        self.assertFalse(r["staleness_locked"])
        expected_cap = passing + V5_CAP_OLD_OFFSET
        self.assertLessEqual(r["score"], expected_cap + 0.01,
                             "Old cap should limit score to passing + CAP_OLD_OFFSET")


# ---------------------------------------------------------------------------
# 13. Difficulty and cognitive caps
# ---------------------------------------------------------------------------

class TestDifficultyAndCognitiveCaps(unittest.TestCase):

    def _build_no_hard_rows(self, n_mocks=3):
        """Verified mocks whose rows all have difficulty='medium'.
        Metadata coverage = 100%, hard tier = 0 → difficulty cap active."""
        attempts = []
        qa = []
        for i in range(n_mocks):
            attempts.append(
                _mock(90.0, attempt_id=i, completed_at=_utc_days_ago(20 - i))
            )
            for j in range(60):
                qa.append({
                    "id": i * 10000 + j,
                    "exam_attempt_id": str(i),
                    "question_id": f"q_{i}_{j}",
                    "is_correct": True,
                    "category": "Domain A",
                    "difficulty": "medium",     # no hard rows at all
                    "cognitive_level": "application",
                })
        return attempts, qa

    def _build_no_ho_rows(self, n_mocks=3):
        """Verified mocks whose rows have no higher-order cognitive levels.
        HO total = 0 < ho_target → cognitive cap active."""
        attempts = []
        qa = []
        for i in range(n_mocks):
            attempts.append(
                _mock(90.0, attempt_id=i, completed_at=_utc_days_ago(20 - i))
            )
            for j in range(60):
                qa.append({
                    "id": i * 10000 + j,
                    "exam_attempt_id": str(i),
                    "question_id": f"q_{i}_{j}",
                    "is_correct": True,
                    "category": "Domain A",
                    "difficulty": "hard" if j < 10 else "easy" if j < 15 else "medium",
                    "cognitive_level": "recall",   # no higher-order
                })
        return attempts, qa

    def test_difficulty_cap_activates_with_available_metadata(self):
        """Full metadata coverage, no hard rows → difficulty_cap_active=True."""
        attempts, qa = self._build_no_hard_rows()
        r = calculate_readiness(attempts=attempts, question_attempts=qa, passing_score=68)
        self.assertTrue(r["difficulty_data_available"])
        self.assertTrue(r["difficulty_cap_active"])

    def test_difficulty_cap_absent_without_metadata(self):
        """Rows with unknown difficulty → coverage < 90% → cap_active=False."""
        attempts = []
        qa = []
        for i in range(3):
            attempts.append(
                _mock(90.0, attempt_id=i, completed_at=_utc_days_ago(20 - i))
            )
            for j in range(60):
                qa.append({
                    "id": i * 10000 + j,
                    "exam_attempt_id": str(i),
                    "question_id": f"q_{i}_{j}",
                    "is_correct": True,
                    "category": "Domain A",
                    # No difficulty field → coverage = 0 < 0.9
                })
        r = calculate_readiness(attempts=attempts, question_attempts=qa)
        self.assertFalse(r["difficulty_data_available"])
        self.assertFalse(r["difficulty_cap_active"])

    def test_cognitive_cap_activates_with_available_metadata(self):
        """Full metadata coverage, no higher-order rows → cognitive_cap_active=True."""
        attempts, qa = self._build_no_ho_rows()
        r = calculate_readiness(attempts=attempts, question_attempts=qa, passing_score=68)
        self.assertTrue(r["cognitive_data_available"])
        self.assertTrue(r["cognitive_cap_active"])

    def test_cognitive_cap_absent_without_metadata(self):
        """Rows with unknown cognitive_level → cap_active=False."""
        attempts = []
        qa = []
        for i in range(3):
            attempts.append(
                _mock(90.0, attempt_id=i, completed_at=_utc_days_ago(20 - i))
            )
            for j in range(60):
                qa.append({
                    "id": i * 10000 + j,
                    "exam_attempt_id": str(i),
                    "question_id": f"q_{i}_{j}",
                    "is_correct": True,
                    "category": "Domain A",
                    "difficulty": "hard" if j < 10 else "medium",
                    # No cognitive_level field → coverage = 0 < 0.9
                })
        r = calculate_readiness(attempts=attempts, question_attempts=qa)
        self.assertFalse(r["cognitive_data_available"])
        self.assertFalse(r["cognitive_cap_active"])

    def test_metadata_threshold_boundary(self):
        """Below 90% metadata coverage → data_available=False; at 90% → True."""
        passing = 65.0

        def _build_partial(n_recognized, n_total=60, n_mocks=3):
            attempts = []
            qa = []
            for i in range(n_mocks):
                attempts.append(
                    _mock(90.0, attempt_id=i, completed_at=_utc_days_ago(20 - i))
                )
                for j in range(n_total):
                    qa.append({
                        "id": i * 10000 + j,
                        "exam_attempt_id": str(i),
                        "question_id": f"q_{i}_{j}",
                        "is_correct": True,
                        "category": "Domain A",
                        "difficulty": ("medium" if j < n_recognized else None),
                        "cognitive_level": ("application" if j < n_recognized else None),
                    })
            return attempts, qa

        # 53 / 60 = 0.883 < 0.90 → data_available = False
        att_low, qa_low = _build_partial(53)
        r_low = calculate_readiness(
            attempts=att_low, question_attempts=qa_low, passing_score=passing
        )
        self.assertFalse(r_low["difficulty_data_available"])
        self.assertFalse(r_low["cognitive_data_available"])

        # 54 / 60 = 0.90 → data_available = True
        att_high, qa_high = _build_partial(54)
        r_high = calculate_readiness(
            attempts=att_high, question_attempts=qa_high, passing_score=passing
        )
        self.assertTrue(r_high["difficulty_data_available"])
        self.assertTrue(r_high["cognitive_data_available"])

    def test_lowest_score_cap_wins(self):
        """When both aging and difficulty caps apply, the lower cap wins."""
        # Use aging staleness (score 90%) + no hard rows (difficulty cap)
        passing = 65.0
        days_old = V5_STALENESS_CURRENT_MAX_DAYS + 20   # aging window
        attempts = []
        qa = []
        for i in range(3):
            attempts.append(
                _mock(90.0, attempt_id=i,
                      completed_at=_utc_days_ago(days_old + i))
            )
            for j in range(60):
                qa.append({
                    "id": i * 10000 + j,
                    "exam_attempt_id": str(i),
                    "question_id": f"q_{i}_{j}",
                    "is_correct": True,
                    "category": "Domain A",
                    "difficulty": "medium",    # no hard → difficulty cap active
                    "cognitive_level": "application",
                })
        r = calculate_readiness(
            attempts=attempts, question_attempts=qa, passing_score=passing
        )
        aging_cap = passing + V5_CAP_AGING_OFFSET         # e.g. 72
        difficulty_cap = passing + (-1)                   # e.g. 64 (V5_CAP_DIFFICULTY_OFFSET)
        expected_winner = min(aging_cap, difficulty_cap)
        self.assertLessEqual(r["score"], expected_winner + 0.01,
                             "Lowest cap should win")
        self.assertGreater(len(r["applied_score_caps"]), 0)


# ---------------------------------------------------------------------------
# 14. Confidence vs coverage
# ---------------------------------------------------------------------------

class TestConfidenceVsCoverage(unittest.TestCase):

    def test_more_unique_questions_raises_confidence(self):
        """3 verified mocks sharing all 60 question IDs (few breadth) vs
        3 verified mocks each using unique question IDs (many breadth).
        Confidence must be higher for many; readiness stays the same."""
        scores = [70.0, 72.0, 75.0]

        def _build(share_qids):
            attempts = []
            qa = []
            for i, s in enumerate(scores):
                attempts.append(
                    _mock(s, attempt_id=i, completed_at=_utc_days_ago(20 - i))
                )
                for j in range(60):
                    qid = f"q_{j}" if share_qids else f"q_{i}_{j}"
                    qa.append({
                        "id": i * 10000 + j,
                        "exam_attempt_id": str(i),
                        "question_id": qid,
                        "is_correct": j < round(s * 60 / 100),
                        "category": "Domain A",
                        "difficulty": "hard" if j < 10 else "easy" if j < 15 else "medium",
                        "cognitive_level": "analysis" if j < 10 else "application" if j < 30 else "understanding",
                    })
            return calculate_readiness(
                attempts=attempts,
                question_attempts=qa,
                question_bank_total=300,
            )

        r_few  = _build(share_qids=True)
        r_many = _build(share_qids=False)

        self.assertGreater(r_many["confidence_score"], r_few["confidence_score"],
                           "Broader coverage should raise confidence")
        self.assertAlmostEqual(r_few["score"], r_many["score"], places=1,
                               msg="Readiness must not change when coverage differs")


# ---------------------------------------------------------------------------
# 15. Cross-mock repeated question_ids counted once for unique_questions_seen
# ---------------------------------------------------------------------------

class TestDuplicateRowsNoInflation(unittest.TestCase):

    def test_cross_mock_repeats_counted_once(self):
        """3 verified mocks sharing the same 60 question IDs → unique_questions_seen=60."""
        scores = [70.0, 72.0, 75.0]
        attempts = []
        qa = []
        for i, s in enumerate(scores):
            attempts.append(
                _mock(s, attempt_id=i, completed_at=_utc_days_ago(20 - i))
            )
            for j in range(60):
                qa.append({
                    "id": i * 10000 + j,
                    "exam_attempt_id": str(i),
                    "question_id": f"q{j}",   # same 60 IDs across all mocks
                    "is_correct": True,
                    "category": "Domain A",
                    "difficulty": "hard" if j < 10 else "easy" if j < 15 else "medium",
                    "cognitive_level": "analysis" if j < 10 else "application" if j < 30 else "understanding",
                })
        r = calculate_readiness(attempts=attempts, question_attempts=qa,
                                question_bank_total=60)
        self.assertEqual(r["unique_questions_seen"], 60,
                         "Cross-mock repeated question IDs must be counted once")


# ---------------------------------------------------------------------------
# 16. Missing question_attempts lowers confidence, does not fabricate domains
# ---------------------------------------------------------------------------

class TestMissingQuestionAttempts(unittest.TestCase):

    def test_no_question_attempts_low_completeness(self):
        # Explicit empty QAs → all mocks LEGACY → locked → completeness=0
        r = _readiness([70.0, 72.0, 75.0], question_attempts=[])
        self.assertEqual(r["question_attempt_completeness"], 0.0)

    def test_no_question_attempts_domain_fallback(self):
        r = _readiness([70.0, 72.0, 75.0])
        self.assertIsInstance(r["domain_scores"], dict)


# ---------------------------------------------------------------------------
# 17. Invalid domain weights fall back gracefully
# ---------------------------------------------------------------------------

class TestDomainWeightFallback(unittest.TestCase):

    INVALID_WEIGHTS = [
        {"Domain A": True, "Domain B": False},
        {"Domain A": float("nan"), "Domain B": float("inf")},
        {"Domain A": -10, "Domain B": 0},
        {"Domain A": "bad", "Domain B": "worse"},
        {},
        None,
    ]

    def test_invalid_weights_do_not_crash(self):
        for dw in self.INVALID_WEIGHTS:
            with self.subTest(weights=str(dw)):
                r = _readiness([70.0, 72.0, 75.0], domain_weights=dw)
                self.assertIsInstance(r["score"], float)
                self.assertGreaterEqual(r["score"], 0.0)


# ---------------------------------------------------------------------------
# 18. Official weights normalized when sum != 100
# ---------------------------------------------------------------------------

class TestWeightNormalization(unittest.TestCase):

    def test_weights_summing_to_200_still_work(self):
        dw = {"Domain A": 60, "Domain B": 140}
        obs = ["Domain A", "Domain B"]
        normalized = _normalize_weights(dw, obs)
        self.assertAlmostEqual(sum(normalized.values()), 1.0, places=5)

    def test_weights_as_decimals_summing_to_0_5_still_work(self):
        dw = {"Domain A": 0.2, "Domain B": 0.3}
        obs = ["Domain A", "Domain B"]
        normalized = _normalize_weights(dw, obs)
        self.assertAlmostEqual(sum(normalized.values()), 1.0, places=5)

    def test_bool_weights_rejected(self):
        dw = {"Domain A": True, "Domain B": False}
        obs = ["Domain A", "Domain B"]
        normalized = _normalize_weights(dw, obs)
        self.assertAlmostEqual(normalized.get("Domain A", 0), 0.5, places=3)
        self.assertAlmostEqual(normalized.get("Domain B", 0), 0.5, places=3)


# ---------------------------------------------------------------------------
# 19. Import consistency and version string
# ---------------------------------------------------------------------------

class TestImportConsistency(unittest.TestCase):

    def test_both_pages_import_same_function(self):
        import utils.readiness as ur
        self.assertTrue(callable(ur.calculate_readiness))
        self.assertTrue(callable(ur.readiness_methodology_text))
        self.assertEqual(ur.READINESS_VERSION, "READINESS_V5_VERIFIED_EVIDENCE")

    def test_readiness_version_constant(self):
        self.assertEqual(READINESS_VERSION, "READINESS_V5_VERIFIED_EVIDENCE")


# ---------------------------------------------------------------------------
# 20. Observed low-performance case
# ---------------------------------------------------------------------------

class TestObservedCase(unittest.TestCase):

    def test_observed_case_below_old_score(self):
        scores = [26.67, 20.0, 18.0, 16.0, 11.67]
        r = _readiness(scores, passing_score=68)
        self.assertLess(r["score"], 22.0,
                        f"V5 readiness {r['score']} is still inflated; expected <22")

    def test_observed_case_not_negative(self):
        r = _readiness([26.67, 20.0, 18.0, 16.0, 11.67])
        self.assertGreaterEqual(r["score"], 0.0)


# ---------------------------------------------------------------------------
# 21. Exam-ready balanced candidate
# ---------------------------------------------------------------------------

class TestExamReadyCandidate(unittest.TestCase):

    def test_exam_ready_high_score(self):
        scores = [72.0, 75.0, 78.0, 80.0, 82.0]
        r = _readiness(scores, passing_score=68)
        self.assertFalse(r["is_locked"])
        ema = _compute_ema(scores)
        self.assertLessEqual(r["score"], ema + 5.1)
        self.assertGreater(r["score"], 70.0,
                           "Exam-ready candidate should score above 70")

    def test_exam_ready_label(self):
        scores = [72.0, 75.0, 78.0, 80.0, 82.0]
        r = _readiness(scores, passing_score=68)
        self.assertIn(r["label"], {"Exam Ready", "Strongly Ready"})


# ---------------------------------------------------------------------------
# 22. All output components stay in valid bounds
# ---------------------------------------------------------------------------

class TestBounds(unittest.TestCase):

    SCORE_KEYS = [
        "score", "raw_score", "recent_accuracy", "domain_score", "domain_robustness",
        "confidence_score", "coverage_percent", "coverage_score", "pacing_score",
        "domain_balance_score", "accuracy_score",
    ]
    RATE_KEYS = [
        "timing_completeness", "fast_incorrect_rate", "slow_answer_rate",
        "question_attempt_completeness", "domain_sample_sufficiency",
    ]

    def _check(self, r):
        for k in self.SCORE_KEYS:
            v = r.get(k, 0.0)
            self.assertGreaterEqual(v, 0.0, f"{k}={v} below 0")
            self.assertLessEqual(v, 100.0, f"{k}={v} above 100")
        for k in self.RATE_KEYS:
            v = r.get(k, 0.0)
            self.assertGreaterEqual(v, 0.0, f"{k}={v} below 0")
            self.assertLessEqual(v, 1.0, f"{k}={v} above 1.0")

    def test_bounds_locked(self):
        self._check(_readiness([70.0]))

    def test_bounds_unlocked_normal(self):
        self._check(_readiness([70.0, 72.0, 75.0, 78.0, 80.0]))

    def test_bounds_all_zeros(self):
        self._check(_readiness([0.0, 0.0, 0.0]))

    def test_bounds_all_100(self):
        self._check(_readiness([100.0, 100.0, 100.0, 100.0, 100.0]))

    def test_bounds_inconsistent(self):
        self._check(_readiness([0.0, 100.0, 0.0, 100.0, 0.0]))


# ---------------------------------------------------------------------------
# 23. Synthetic scenario spot-checks
# ---------------------------------------------------------------------------

class TestSyntheticScenarios(unittest.TestCase):

    def test_s1_low_scores_high_coverage_stays_low(self):
        r = _readiness([15.0, 20.0, 18.0, 25.0, 12.0])
        self.assertLess(r["score"], 25.0)

    def test_s2_high_scores_low_coverage_shows_good_readiness(self):
        r = _readiness([75.0, 78.0, 80.0])
        self.assertGreater(r["score"], 65.0)

    def test_s3_improving_higher_than_declining(self):
        r_imp = _readiness([45.0, 52.0, 60.0, 68.0, 74.0])
        r_dec = _readiness([74.0, 68.0, 60.0, 52.0, 45.0])
        self.assertGreater(r_imp["score"], r_dec["score"])

    def test_s4_inconsistent_penalized(self):
        r = _readiness([40.0, 85.0, 45.0, 82.0, 50.0])
        self.assertGreater(r["consistency_penalty"], 0.0)

    def test_s9_exam_ready_balanced(self):
        r = _readiness([72.0, 75.0, 78.0, 80.0, 82.0])
        self.assertGreater(r["score"], 70.0)

    def test_methodology_text_not_empty(self):
        text = readiness_methodology_text()
        self.assertTrue(len(text) > 50)
        self.assertIn("confidence", text.lower())
        self.assertIn("3 full paid mock", text.lower())


# ---------------------------------------------------------------------------
# 24. Pacing diagnostics (pure helper tests)
# ---------------------------------------------------------------------------

class TestPacingDiagnostics(unittest.TestCase):

    def test_insufficient_timing_when_few_valid_rows(self):
        result = _compute_pacing_diagnostics([], 105, 60)
        self.assertEqual(result["pacing_status"], "Insufficient Timing Data")

    def test_below_1s_rows_ignored(self):
        qa = [_qattempt("0", str(i), time_spent_seconds=0.5) for i in range(60)]
        result = _compute_pacing_diagnostics(qa, 105, 60)
        self.assertEqual(result["timed_questions"], 0)

    def test_capped_at_300s(self):
        qa = [_qattempt("0", str(i), time_spent_seconds=1000.0) for i in range(60)]
        result = _compute_pacing_diagnostics(qa, 105, 60)
        self.assertEqual(result["median_time_per_question"], 300.0)

    def test_on_pace_when_fast_and_correct(self):
        target = 105 * 60 / 60
        fast_correct = [
            _qattempt("0", str(i), is_correct=True, time_spent_seconds=target * 0.2)
            for i in range(60)
        ]
        result = _compute_pacing_diagnostics(fast_correct, 105, 60)
        self.assertNotEqual(result["pacing_status"], "Too Fast / Likely Guessing")


# ---------------------------------------------------------------------------
# 25. Non-eligible modes are excluded
# ---------------------------------------------------------------------------

class TestEligibility(unittest.TestCase):

    EXCLUDED_MODES = [
        "Free Mock Exam",
        "Timed Practice",
        "Practice by Category",
        "Weak Areas Practice",
        "Daily Sprint",
        "free",
        "practice",
        "",
    ]

    def test_excluded_modes_do_not_count(self):
        for mode in self.EXCLUDED_MODES:
            with self.subTest(mode=mode):
                r = _readiness([80.0, 85.0, 90.0], modes=[mode] * 3)
                self.assertTrue(r["is_locked"],
                                f"Mode '{mode}' should be excluded")

    def test_mixed_modes_only_paid_count(self):
        scores = [70.0, 75.0, 80.0, 85.0, 90.0]
        modes = [
            "Paid Mock Exam", "Paid Mock Exam",
            "Free Mock Exam", "Timed Practice", "Daily Sprint",
        ]
        r = _readiness(scores, modes=modes)
        self.assertTrue(r["is_locked"])

    def test_partial_mock_excluded(self):
        attempts = [_mock(80.0, total_questions=30, attempt_id=0)]
        r = calculate_readiness(attempts=attempts)
        self.assertTrue(r["is_locked"])


# ---------------------------------------------------------------------------
# 26. Backward-compatible and V5 payload key completeness
# ---------------------------------------------------------------------------

class TestPayloadKeys(unittest.TestCase):

    V4_KEYS = [
        "is_locked", "eligible_mock_count", "required_mock_count", "mocks_remaining",
        "score", "raw_score", "label", "color", "recommendation",
        "recent_accuracy", "domain_score", "domain_robustness",
        "weakest_reliable_domain", "weakest_reliable_domain_score",
        "consistency_standard_deviation", "consistency_penalty",
        "trend_slope", "trend_adjustment", "trend_label",
        "pacing_status", "timing_completeness", "fast_incorrect_rate",
        "slow_answer_rate", "median_time_per_question", "target_time_per_question",
        "timed_questions", "confidence_score", "confidence_label", "confidence",
        "unique_questions_seen", "question_attempt_completeness",
        "domain_sample_sufficiency", "coverage_percent",
        "domain_scores", "weak_domains", "strong_domains",
        "accuracy_score", "coverage_score", "domain_balance_score",
        "pacing_score", "recent_mock_score", "weighted_domain_score",
        "consistency_score", "practice_volume_score",
        "total_attempted", "full_mock_count", "mock_scores_used",
        "guardrail_applied", "guardrail_cap",
    ]

    V5_KEYS = [
        "formula_version", "verified_mock_count", "legacy_mock_count",
        "invalid_mock_count", "verified_attempt_ids", "legacy_attempt_ids",
        "trend_delta", "staleness_state", "staleness_days", "staleness_locked",
        "domain_states", "domain_gap_triggered", "domain_floor_triggered",
        "uncovered_domains", "difficulty_metadata_coverage",
        "difficulty_data_available", "difficulty_cap_active",
        "difficulty_effective_totals", "difficulty_accuracies",
        "cognitive_metadata_coverage", "cognitive_data_available",
        "cognitive_cap_active", "cognitive_effective_totals", "cognitive_accuracies",
        "higher_order_effective_total", "higher_order_accuracy", "higher_order_target",
        "cross_mock_repeat_fraction", "family_data_available",
        "effective_target_sample", "captured_bank_size_used",
        "bank_size_fallback_used", "coverage_target", "applied_score_caps",
        "hard_capped_score",
    ]

    def _unlocked(self):
        return _readiness([70.0, 72.0, 75.0])

    def test_all_v4_keys_present_unlocked(self):
        r = self._unlocked()
        for key in self.V4_KEYS:
            self.assertIn(key, r, f"V4 key '{key}' missing from unlocked result")

    def test_all_v5_keys_present_unlocked(self):
        r = self._unlocked()
        for key in self.V5_KEYS:
            self.assertIn(key, r, f"V5 key '{key}' missing from unlocked result")

    def test_all_v4_keys_present_locked(self):
        r = _readiness([70.0])   # locked
        for key in self.V4_KEYS:
            self.assertIn(key, r, f"V4 key '{key}' missing from locked result")

    def test_all_v5_keys_present_locked(self):
        r = _readiness([70.0])   # locked
        for key in self.V5_KEYS:
            self.assertIn(key, r, f"V5 key '{key}' missing from locked result")

    def test_pacing_score_is_zero(self):
        r = self._unlocked()
        self.assertEqual(r["pacing_score"], 0.0)

    def test_eligible_mock_count_equals_verified_mock_count(self):
        r = self._unlocked()
        self.assertEqual(r["eligible_mock_count"], r["verified_mock_count"])

    def test_full_mock_count_equals_verified_mock_count(self):
        r = self._unlocked()
        self.assertEqual(r["full_mock_count"], r["verified_mock_count"])


# ---------------------------------------------------------------------------
# 27. Captured bank size and fallback reporting
# ---------------------------------------------------------------------------

class TestCapturedBankSize(unittest.TestCase):

    def test_captured_bank_size_passed_into_confidence(self):
        r = _readiness([70.0, 72.0, 75.0], captured_bank_size=100)
        self.assertEqual(r["captured_bank_size_used"], 100)
        # captured_bank_size provided → not a fallback
        self.assertFalse(r["bank_size_fallback_used"])

    def test_bank_size_fallback_when_captured_absent(self):
        # No captured_bank_size (even with question_bank_total) → fallback=True
        r = _readiness([70.0, 72.0, 75.0])
        self.assertTrue(r["bank_size_fallback_used"])

    def test_live_bank_total_used_when_captured_absent(self):
        # question_bank_total provides live bank size; captured is still absent → fallback=True
        r_live    = _readiness([70.0, 72.0, 75.0], question_bank_total=189)
        r_no_live = _readiness([70.0, 72.0, 75.0])
        # Both report fallback=True (captured_bank_size absent in both)
        self.assertTrue(r_live["bank_size_fallback_used"])
        self.assertTrue(r_no_live["bank_size_fallback_used"])


if __name__ == "__main__":
    unittest.main()
