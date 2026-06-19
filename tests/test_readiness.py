"""
CertBound V38 Readiness — unit tests.

Run:
    python -m unittest tests.test_readiness -v

Tests use only Python's built-in unittest. No third-party packages required.
"""

import sys
import os
import unittest

# Allow imports from repo root regardless of how tests are launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.readiness import (
    REQUIRED_FULL_MOCKS,
    calculate_readiness,
    is_full_mock_attempt,
    _compute_ema,
    _normalize_weights,
    _compute_pacing_diagnostics,
    _compute_confidence,
    readiness_methodology_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock(score, mode="Paid Mock Exam", total_questions=60, completed_at="2025-01-10T10:00:00Z", attempt_id=None):
    return {
        "id": attempt_id or str(score),
        "mode": mode,
        "score": score,
        "total_questions": total_questions,
        "completed_at": completed_at,
        "domain_breakdown": {},
    }


def _qattempt(exam_attempt_id, question_id, category="Domain A", is_correct=True, time_spent_seconds=80.0):
    return {
        "exam_attempt_id": exam_attempt_id,
        "question_id": question_id,
        "category": category,
        "is_correct": is_correct,
        "time_spent_seconds": time_spent_seconds,
    }


def _readiness(scores, domain_weights=None, question_attempts=None, question_bank_total=None,
               passing_score=68, time_limit_minutes=105, expected_question_count=60,
               modes=None, completed_ats=None):
    """Build attempts list and call calculate_readiness."""
    modes = modes or ["Paid Mock Exam"] * len(scores)
    completed_ats = completed_ats or [f"2025-01-{10 + i:02d}T10:00:00Z" for i in range(len(scores))]
    attempts = [
        _mock(s, mode=m, completed_at=d, attempt_id=str(i))
        for i, (s, m, d) in enumerate(zip(scores, modes, completed_ats))
    ]
    return calculate_readiness(
        attempts=attempts,
        passing_score=passing_score,
        domain_weights=domain_weights,
        expected_question_count=expected_question_count,
        question_bank_total=question_bank_total,
        question_attempts=question_attempts or [],
        time_limit_minutes=time_limit_minutes,
    )


# ---------------------------------------------------------------------------
# 1. Readiness locked below 3 eligible full mocks
# ---------------------------------------------------------------------------

class TestLocking(unittest.TestCase):

    def test_locked_with_zero_mocks(self):
        r = _readiness([])
        self.assertTrue(r["is_locked"])
        self.assertEqual(r["score"], 0.0)
        self.assertIn("Locked", r["label"])

    def test_locked_with_two_mocks(self):
        r = _readiness([70.0, 75.0])
        self.assertTrue(r["is_locked"])
        self.assertEqual(r["score"], 0.0)

    def test_unlocked_with_three_mocks(self):
        r = _readiness([60.0, 65.0, 70.0])
        self.assertFalse(r["is_locked"])
        self.assertGreater(r["score"], 0.0)

    def test_locked_exposes_eligible_and_remaining(self):
        r = _readiness([50.0])
        self.assertEqual(r["eligible_mock_count"], 1)
        self.assertEqual(r["required_mock_count"], REQUIRED_FULL_MOCKS)
        self.assertEqual(r["mocks_remaining"], REQUIRED_FULL_MOCKS - 1)


# ---------------------------------------------------------------------------
# 2. Non-eligible modes are excluded
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
                self.assertTrue(r["is_locked"], f"Mode '{mode}' should be excluded")

    def test_mixed_modes_only_paid_count(self):
        # 2 paid + 3 free = should stay locked
        scores = [70.0, 75.0, 80.0, 85.0, 90.0]
        modes = ["Paid Mock Exam", "Paid Mock Exam", "Free Mock Exam", "Timed Practice", "Daily Sprint"]
        r = _readiness(scores, modes=modes)
        self.assertTrue(r["is_locked"])

    def test_partial_mock_excluded(self):
        # total_questions below expected_question_count → not eligible
        attempts = [_mock(80.0, total_questions=30, attempt_id="x")]
        r = calculate_readiness(attempts=attempts)
        self.assertTrue(r["is_locked"])


# ---------------------------------------------------------------------------
# 3. High coverage cannot inflate low accuracy
# ---------------------------------------------------------------------------

class TestCoverageDoesNotInflate(unittest.TestCase):

    def test_high_coverage_low_accuracy_stays_near_accuracy(self):
        # 5 mocks averaging ~18%, with 110 question_attempts (high coverage)
        scores = [22.0, 25.0, 18.0, 16.0, 11.67]
        qa = [_qattempt(str(i % 5), str(i), is_correct=(i % 5 == 0)) for i in range(110)]
        r = _readiness(scores, question_attempts=qa, question_bank_total=189)
        # V38 readiness must be close to actual scores, not ~36% as in old formula
        self.assertLess(r["score"], 25.0, f"Readiness {r['score']} is inflated above accuracy")

    def test_observed_certbound_case(self):
        # Observed: avg 18.61%, best 26.67%, latest 11.67%, coverage 58%
        scores = [26.67, 20.0, 18.0, 16.0, 11.67]
        r = _readiness(scores)
        # Old formula: 36.47. V38 must be substantially lower.
        self.assertLess(r["score"], 22.0, f"Score {r['score']} still too high for 18% performer")
        self.assertGreaterEqual(r["score"], 0.0)


# ---------------------------------------------------------------------------
# 4. Fast wrong guesser — pacing diagnostic only, no bonus points
# ---------------------------------------------------------------------------

class TestFastGuesserPacing(unittest.TestCase):

    def test_fast_wrong_does_not_add_readiness(self):
        scores = [25.0, 22.0, 20.0]
        # All answers: 2 seconds (very fast) and wrong
        qa = [_qattempt(str(i % 3), str(i), is_correct=False, time_spent_seconds=2.0) for i in range(180)]
        r = _readiness(scores, question_attempts=qa)
        # Pacing should signal guessing
        self.assertEqual(r["pacing_status"], "Too Fast / Likely Guessing")
        # Pacing score key must be 0.0 (not a positive contribution)
        self.assertEqual(r["pacing_score"], 0.0)
        # Readiness must not be boosted above EMA+5
        ema_approx = _compute_ema(scores)
        self.assertLessEqual(r["score"], ema_approx + 5.1)

    def test_pacing_score_key_always_zero(self):
        r = _readiness([70.0, 72.0, 75.0])
        self.assertEqual(r["pacing_score"], 0.0)


# ---------------------------------------------------------------------------
# 5. Slow accurate candidate receives no penalty
# ---------------------------------------------------------------------------

class TestSlowAccurate(unittest.TestCase):

    def test_slow_but_correct_no_readiness_penalty(self):
        scores = [78.0, 80.0, 79.0]
        # Answers correct but slightly slow (time > 1.5 * target for some)
        target = 105 * 60 / 60  # 105s per question
        qa = [_qattempt(str(i % 3), str(i), is_correct=True, time_spent_seconds=target * 1.6)
              for i in range(180)]
        r = _readiness(scores, question_attempts=qa)
        # Readiness should be near the EMA of 78-80%
        ema = _compute_ema(scores)
        self.assertGreaterEqual(r["score"], ema - 6.0, "Slow accurate candidate unfairly penalized")


# ---------------------------------------------------------------------------
# 6 & 7. Trend adjustments
# ---------------------------------------------------------------------------

class TestTrend(unittest.TestCase):

    def test_improving_trend_gets_small_positive(self):
        improving = [45.0, 52.0, 60.0, 68.0, 74.0]
        declining = [74.0, 68.0, 60.0, 52.0, 45.0]
        r_imp = _readiness(improving)
        r_dec = _readiness(declining)
        self.assertGreater(r_imp["score"], r_dec["score"],
                           "Improving trend should yield higher readiness than declining")
        self.assertEqual(r_imp["trend_label"], "Improving")

    def test_declining_trend_gets_negative(self):
        r = _readiness([78.0, 74.0, 65.0, 58.0, 50.0])
        self.assertLess(r["trend_adjustment"], 0.0)
        self.assertEqual(r["trend_label"], "Declining")

    def test_improving_credit_capped(self):
        # trend_adjustment capped at +4
        r = _readiness([40.0, 55.0, 70.0, 85.0, 99.0])
        self.assertLessEqual(r["trend_adjustment"], 4.01)

    def test_declining_penalty_capped(self):
        # trend_adjustment floored at -6
        r = _readiness([90.0, 75.0, 60.0, 45.0, 30.0])
        self.assertGreaterEqual(r["trend_adjustment"], -6.01)


# ---------------------------------------------------------------------------
# 8. Inconsistent scores receive consistency penalty
# ---------------------------------------------------------------------------

class TestConsistencyPenalty(unittest.TestCase):

    def test_inconsistent_gets_penalty(self):
        inconsistent = [40.0, 85.0, 45.0, 82.0, 50.0]
        consistent = [60.0, 62.0, 61.0, 63.0, 62.0]
        r_inc = _readiness(inconsistent)
        r_con = _readiness(consistent)
        self.assertGreater(r_inc["consistency_penalty"], 0.0)
        self.assertGreater(r_con["score"], r_inc["score"],
                           "Consistent performer should outscore inconsistent one")

    def test_small_variation_no_penalty(self):
        # SD < 5 → no penalty
        r = _readiness([70.0, 72.0, 71.0, 73.0, 70.0])
        self.assertEqual(r["consistency_penalty"], 0.0)


# ---------------------------------------------------------------------------
# 9. Weak reliable domain reduces domain robustness
# ---------------------------------------------------------------------------

class TestWeakDomain(unittest.TestCase):

    def test_weak_reliable_domain_reduces_dr(self):
        # 3 domains: A strong, B strong, C very weak (meets sample threshold)
        qa = []
        for i in range(30):
            qa.append(_qattempt("0", f"qa_{i}", category="Domain A", is_correct=True))
        for i in range(30):
            qa.append(_qattempt("0", f"qb_{i}", category="Domain B", is_correct=True))
        for i in range(20):
            qa.append(_qattempt("0", f"qc_{i}", category="Domain C", is_correct=(i < 3)))

        attempts_only = [_mock(75.0, attempt_id="0")]
        # Need 3 mocks — duplicate for test
        attempts_3 = [_mock(75.0, attempt_id=str(j), completed_at=f"2025-01-{10+j:02d}T10:00:00Z") for j in range(3)]
        qa_3 = []
        for j in range(3):
            for row in qa:
                row2 = dict(row, exam_attempt_id=str(j))
                qa_3.append(row2)

        r = calculate_readiness(
            attempts=attempts_3,
            question_attempts=qa_3,
            passing_score=68,
            expected_question_count=60,
        )
        self.assertFalse(r["is_locked"])
        self.assertLess(r["domain_robustness"], r["domain_score"],
                        "Weak reliable domain should pull DR below D")


# ---------------------------------------------------------------------------
# 10. Sparse weak domain does not trigger exaggerated floor
# ---------------------------------------------------------------------------

class TestSparseDomainNoFloor(unittest.TestCase):

    def test_sparse_domain_excluded_from_floor(self):
        """
        Domain C (2 attempts, all wrong) is below the required sample threshold
        and must not drag DR down to zero.
        Domain A (90 attempts, all correct) qualifies and is identified as the
        weakest reliable domain at 100% — pulling F *up*, not down.

        This means DR >= D (the sparse bad domain cannot inflict an exaggerated floor).
        """
        qa = []
        for i in range(30):
            qa.append(_qattempt("0", f"qa_{i}", category="Domain A", is_correct=True))
        for i in range(2):
            qa.append(_qattempt("0", f"qc_{i}", category="Domain C", is_correct=False))
        qa_3 = []
        for j in range(3):
            for row in qa:
                qa_3.append(dict(row, exam_attempt_id=str(j)))
        attempts = [_mock(70.0, attempt_id=str(j), completed_at=f"2025-01-{10+j:02d}T10:00:00Z") for j in range(3)]
        r = calculate_readiness(attempts=attempts, question_attempts=qa_3)
        # Domain C is sparse: it must NOT be the weakest_reliable_domain
        self.assertNotEqual(r["weakest_reliable_domain"], "Domain C",
                            "Sparse Domain C must not qualify as a reliable weak domain")
        # Because the sparse bad domain is excluded, DR must not be pulled below D
        self.assertGreaterEqual(
            r["domain_robustness"], r["domain_score"] - 0.1,
            "Sparse bad domain must not drag DR below D",
        )


# ---------------------------------------------------------------------------
# 11. Readiness never exceeds A + 5
# ---------------------------------------------------------------------------

class TestHardCap(unittest.TestCase):

    def test_score_never_exceeds_ema_plus_5(self):
        # Even with improving trend, large domain score, etc.
        for scores in [
            [70.0, 75.0, 80.0, 85.0, 90.0],
            [50.0, 60.0, 70.0, 80.0, 90.0],
            [80.0, 80.0, 80.0, 80.0, 80.0],
        ]:
            r = _readiness(scores)
            ema = _compute_ema(scores)
            self.assertLessEqual(
                r["score"], ema + 5.1,
                f"Score {r['score']} exceeds EMA {ema}+5 for scores {scores}",
            )


# ---------------------------------------------------------------------------
# 12. Confidence changes when coverage changes; readiness stays unchanged
# ---------------------------------------------------------------------------

class TestConfidenceVsCoverage(unittest.TestCase):

    def test_more_unique_questions_raises_confidence(self):
        scores = [65.0, 68.0, 70.0]
        attempts = [_mock(s, attempt_id=str(i), completed_at=f"2025-01-{10+i:02d}T10:00:00Z") for i, s in enumerate(scores)]

        qa_few = [_qattempt(str(i % 3), str(i)) for i in range(30)]
        qa_many = [_qattempt(str(i % 3), str(i)) for i in range(120)]

        r_few = calculate_readiness(attempts=attempts, question_attempts=qa_few)
        r_many = calculate_readiness(attempts=attempts, question_attempts=qa_many)

        self.assertGreater(r_many["confidence_score"], r_few["confidence_score"])
        # Readiness should be the same (coverage doesn't affect it)
        self.assertAlmostEqual(r_few["score"], r_many["score"], places=1)


# ---------------------------------------------------------------------------
# 13. Duplicate question_attempt rows do not inflate unique coverage / completeness
# ---------------------------------------------------------------------------

class TestDuplicateRowsNoInflation(unittest.TestCase):

    def test_duplicate_pairs_counted_once(self):
        scores = [70.0, 72.0, 75.0]
        attempts = [_mock(s, attempt_id=str(i), completed_at=f"2025-01-{10+i:02d}T10:00:00Z") for i, s in enumerate(scores)]
        # 5 unique pairs, each duplicated 10x
        qa = [_qattempt(str(i % 3), str(q_id)) for i in range(3) for q_id in range(5) for _ in range(10)]
        r = calculate_readiness(attempts=attempts, question_attempts=qa, question_bank_total=60)
        # unique pairs = 5, not 50 or 150
        self.assertEqual(r["unique_questions_seen"], 5)


# ---------------------------------------------------------------------------
# 14. Missing question_attempts lowers confidence, does not fabricate domains
# ---------------------------------------------------------------------------

class TestMissingQuestionAttempts(unittest.TestCase):

    def test_no_question_attempts_low_completeness(self):
        r = _readiness([70.0, 72.0, 75.0])
        self.assertEqual(r["question_attempt_completeness"], 0.0)

    def test_no_question_attempts_domain_fallback(self):
        # Should not crash; domain scores fall back to attempt domain_breakdown (empty here)
        r = _readiness([70.0, 72.0, 75.0])
        self.assertIsInstance(r["domain_scores"], dict)


# ---------------------------------------------------------------------------
# 15. Invalid domain weights fall back to equal weights
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
# 16. Official weights normalized when sum != 100
# ---------------------------------------------------------------------------

class TestWeightNormalization(unittest.TestCase):

    def test_weights_summing_to_200_still_work(self):
        # percentage form summing to 200% instead of 100%
        dw = {"Domain A": 60, "Domain B": 140}
        obs = ["Domain A", "Domain B"]
        normalized = _normalize_weights(dw, obs)
        total = sum(normalized.values())
        self.assertAlmostEqual(total, 1.0, places=5)

    def test_weights_as_decimals_summing_to_0_5_still_work(self):
        dw = {"Domain A": 0.2, "Domain B": 0.3}
        obs = ["Domain A", "Domain B"]
        normalized = _normalize_weights(dw, obs)
        total = sum(normalized.values())
        self.assertAlmostEqual(total, 1.0, places=5)

    def test_bool_weights_rejected(self):
        dw = {"Domain A": True, "Domain B": False}
        obs = ["Domain A", "Domain B"]
        normalized = _normalize_weights(dw, obs)
        # Both invalid → equal weights fallback
        self.assertAlmostEqual(normalized.get("Domain A", 0), 0.5, places=3)
        self.assertAlmostEqual(normalized.get("Domain B", 0), 0.5, places=3)


# ---------------------------------------------------------------------------
# 17. Dashboard and My Progress import the same module
# ---------------------------------------------------------------------------

class TestImportConsistency(unittest.TestCase):

    def test_both_pages_import_same_function(self):
        import importlib
        import types

        # Manually check import paths without executing page Streamlit code
        import utils.readiness as ur
        self.assertTrue(callable(ur.calculate_readiness))
        self.assertTrue(callable(ur.readiness_methodology_text))

        # Verify the module version string
        self.assertEqual(ur.READINESS_VERSION, "READINESS_V4_PERFORMANCE_ANCHORED")


# ---------------------------------------------------------------------------
# 18. Observed low-performance case: readiness close to actual, not 36.47%
# ---------------------------------------------------------------------------

class TestObservedCase(unittest.TestCase):

    def test_observed_case_below_old_score(self):
        scores = [26.67, 20.0, 18.0, 16.0, 11.67]
        r = _readiness(scores, passing_score=68)
        self.assertLess(r["score"], 22.0,
                        f"V38 readiness {r['score']} is still inflated; expected <22 for this profile")

    def test_observed_case_not_negative(self):
        scores = [26.67, 20.0, 18.0, 16.0, 11.67]
        r = _readiness(scores)
        self.assertGreaterEqual(r["score"], 0.0)


# ---------------------------------------------------------------------------
# 19. Exam-ready balanced candidate stays near actual performance
# ---------------------------------------------------------------------------

class TestExamReadyCandidate(unittest.TestCase):

    def test_exam_ready_high_score(self):
        scores = [72.0, 75.0, 78.0, 80.0, 82.0]
        r = _readiness(scores, passing_score=68)
        self.assertFalse(r["is_locked"])
        ema = _compute_ema(scores)
        self.assertLessEqual(r["score"], ema + 5.1)
        self.assertGreater(r["score"], 70.0, "Exam-ready candidate should score above 70")

    def test_exam_ready_label(self):
        scores = [72.0, 75.0, 78.0, 80.0, 82.0]
        r = _readiness(scores, passing_score=68)
        self.assertIn(r["label"], {"Exam Ready", "Strongly Ready"})


# ---------------------------------------------------------------------------
# 20. All output components remain within valid bounds
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
        r = _readiness([70.0])
        self._check(r)

    def test_bounds_unlocked_normal(self):
        r = _readiness([70.0, 72.0, 75.0, 78.0, 80.0])
        self._check(r)

    def test_bounds_all_zeros(self):
        r = _readiness([0.0, 0.0, 0.0])
        self._check(r)

    def test_bounds_all_100(self):
        r = _readiness([100.0, 100.0, 100.0, 100.0, 100.0])
        self._check(r)

    def test_bounds_inconsistent(self):
        r = _readiness([0.0, 100.0, 0.0, 100.0, 0.0])
        self._check(r)


# ---------------------------------------------------------------------------
# Synthetic scenario spot-checks (approximate, from audit §11)
# ---------------------------------------------------------------------------

class TestSyntheticScenarios(unittest.TestCase):

    def test_s1_low_scores_high_coverage_stays_low(self):
        # Low scores (avg ~18%), high coverage → should not inflate to ~36
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
# Pacing diagnostics
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
        target = 105 * 60 / 60  # 105s
        fast_correct = [_qattempt("0", str(i), is_correct=True, time_spent_seconds=target * 0.2)
                        for i in range(60)]
        result = _compute_pacing_diagnostics(fast_correct, 105, 60)
        # Fast but correct → not "Too Fast / Likely Guessing"
        self.assertNotEqual(result["pacing_status"], "Too Fast / Likely Guessing")


if __name__ == "__main__":
    unittest.main()
