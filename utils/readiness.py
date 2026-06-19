"""
CertBound Readiness Scoring — V4 Performance-Anchored Formula.

Readiness estimates how ready a user is for the real exam based exclusively
on their recent full paid mock exam performance.  Coverage and pacing are
diagnostic signals only — they do not add readiness points.

Design principles:
- Anchored to recent full paid mock scores (EMA alpha=0.40, last 5 mocks)
- Official domain weights applied when valid; equal weights otherwise
- Weak reliable domain pulls down a domain-robustness term
- Consistency penalty for volatile scores; trend gives limited upside / larger downside
- Readiness can never exceed recent EMA accuracy + 5 points
- Coverage and mock volume feed Confidence only
- Pacing is a diagnostic status string, not a score contribution
- Locked until 3 full paid mock exams are completed
"""

import json
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

READINESS_VERSION = "READINESS_V4_PERFORMANCE_ANCHORED"

EMA_ALPHA = 0.40
REQUIRED_FULL_MOCKS = 3
MAX_RECENT_MOCKS = 5
QUESTION_TIME_CAP_SECONDS = 300.0
QUESTION_TIME_MIN_SECONDS = 1.0  # below this = instrumentation noise, ignored


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------

def normalize_breakdown(value: Any) -> Dict[str, Dict[str, float]]:
    """Normalize domain_breakdown stored in Supabase into a plain dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, _safe_float(value, 0.0)))


def _norm_key(s: Any) -> str:
    return str(s or "").strip().lower()


def _valid_weight(value: Any) -> Optional[float]:
    """Return weight as fraction (0–1) or None when invalid.

    Explicitly rejects booleans, NaN, infinity, zero, and negative values.
    Accepts both percentage form (15 → 0.15) and decimal form (0.15).
    """
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except Exception:
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f / 100.0 if f > 1.0 else f


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def _parse_sort_value(attempt: Dict[str, Any]) -> str:
    return str(attempt.get("completed_at") or attempt.get("started_at") or attempt.get("id") or "")


def is_full_mock_attempt(attempt: Dict[str, Any], expected_question_count: int = 60) -> bool:
    """Return True only for full-length Paid Mock Exam attempts eligible for readiness."""
    mode = str(attempt.get("mode") or "").strip().lower()
    total = _safe_int(attempt.get("total_questions"), 0)
    return mode == "paid mock exam" and total >= int(expected_question_count or 60)


def full_mock_count(attempts: List[Dict[str, Any]], expected_question_count: int = 60) -> int:
    return sum(1 for a in (attempts or []) if is_full_mock_attempt(a, expected_question_count))


def _select_recent_mocks(
    attempts: List[Dict[str, Any]], n: int = MAX_RECENT_MOCKS
) -> List[Dict[str, Any]]:
    """Return the n most recent eligible mocks with valid non-negative scores, oldest first."""
    usable = [
        a for a in (attempts or [])
        if a.get("score") is not None and _safe_float(a.get("score"), -1) >= 0
    ]
    return sorted(usable, key=_parse_sort_value)[-n:]


# ---------------------------------------------------------------------------
# Formula components
# ---------------------------------------------------------------------------

def _compute_ema(scores: List[float], alpha: float = EMA_ALPHA) -> float:
    """EMA seeded by the oldest score; each newer score applies the given alpha."""
    if not scores:
        return 0.0
    ema = scores[0]
    for s in scores[1:]:
        ema = alpha * s + (1.0 - alpha) * ema
    return _clamp(ema)


def _normalize_weights(
    domain_weights: Optional[Dict[str, Any]],
    observed_domains: List[str],
) -> Dict[str, float]:
    """Return per-domain weights normalized to sum=1.0 for observed domains.

    1. Try to match observed domain names against supplied official weights.
    2. Fall back to equal weights across all observed domains.
    """
    raw: Dict[str, float] = {}
    if domain_weights and isinstance(domain_weights, dict):
        for k, v in domain_weights.items():
            w = _valid_weight(v)
            if w is not None:
                raw[_norm_key(k)] = w

    if raw and observed_domains:
        matched: Dict[str, float] = {}
        for domain in observed_domains:
            nk = _norm_key(domain)
            if nk in raw:
                matched[domain] = raw[nk]
        if matched:
            total_w = sum(matched.values())
            if total_w > 0:
                return {d: w / total_w for d, w in matched.items()}

    if not observed_domains:
        return {}
    eq = 1.0 / len(observed_domains)
    return {d: eq for d in observed_domains}


def _build_domain_stats(
    question_attempts: List[Dict[str, Any]],
    attempts: List[Dict[str, Any]],
) -> Dict[str, Dict[str, float]]:
    """Aggregate domain accuracy.  question_attempts rows preferred; falls back to domain_breakdown."""
    totals: Dict[str, Dict[str, float]] = {}

    if question_attempts:
        for row in question_attempts:
            domain = str(row.get("category") or "Uncategorized")
            totals.setdefault(domain, {"correct": 0.0, "total": 0.0})
            totals[domain]["total"] += 1.0
            if bool(row.get("is_correct")):
                totals[domain]["correct"] += 1.0
    else:
        for attempt in (attempts or []):
            breakdown = normalize_breakdown(attempt.get("domain_breakdown"))
            for domain, data in breakdown.items():
                if not isinstance(data, dict):
                    continue
                correct = _safe_float(data.get("correct"), 0.0)
                total = _safe_float(data.get("total"), 0.0)
                if total <= 0:
                    continue
                totals.setdefault(str(domain), {"correct": 0.0, "total": 0.0})
                totals[str(domain)]["correct"] += correct
                totals[str(domain)]["total"] += total

    for domain, data in totals.items():
        t = data["total"]
        data["percent"] = round((data["correct"] / t) * 100, 2) if t > 0 else 0.0

    return totals


def _compute_domain_score(
    domain_stats: Dict[str, Dict[str, float]],
    weights: Dict[str, float],
    expected_question_count: int,
) -> Tuple[float, float, Optional[str], float]:
    """Return (D, DR, weakest_reliable_domain, weakest_score).

    D  = weighted average domain accuracy
    F  = accuracy of the weakest domain meeting its minimum sample threshold (or D)
    DR = 0.75*D + 0.25*F  — domain robustness
    """
    if not domain_stats or not weights:
        return 0.0, 0.0, None, 0.0

    d_num = 0.0
    w_total = 0.0
    for domain, w in weights.items():
        stats = domain_stats.get(domain)
        if stats is None or _safe_float(stats.get("total"), 0.0) <= 0:
            continue
        d_num += w * _safe_float(stats.get("percent"), 0.0)
        w_total += w

    if w_total <= 0:
        return 0.0, 0.0, None, 0.0

    D = _clamp(d_num / w_total)

    weakest_domain: Optional[str] = None
    weakest_score: float = D  # default F = D (no qualifying domain found)
    worst_pct = 101.0

    for domain, w in weights.items():
        stats = domain_stats.get(domain)
        if stats is None:
            continue
        attempted = _safe_float(stats.get("total"), 0.0)
        required = max(5, math.ceil(expected_question_count * w * 1.5))
        if attempted < required:
            continue
        pct = _safe_float(stats.get("percent"), 0.0)
        if pct < worst_pct:
            worst_pct = pct
            weakest_domain = domain
            weakest_score = pct

    F = weakest_score if weakest_domain is not None else D
    DR = _clamp(0.75 * D + 0.25 * F)

    return round(D, 2), round(DR, 2), weakest_domain, round(weakest_score if weakest_domain is not None else 0.0, 2)


def _compute_pacing_diagnostics(
    question_attempts: List[Dict[str, Any]],
    time_limit_minutes: int,
    expected_question_count: int,
) -> Dict[str, Any]:
    """Return pacing diagnostics.  Does not affect readiness score."""
    target = (_safe_float(time_limit_minutes, 105.0) * 60.0) / max(_safe_int(expected_question_count, 60), 1)
    fast_threshold = max(3.0, 0.25 * target)
    slow_threshold = 1.5 * target

    valid_times: List[float] = []
    fast_incorrect = 0
    slow_count = 0
    total_rows = len(question_attempts or [])

    for row in (question_attempts or []):
        seconds = _safe_float(row.get("time_spent_seconds"), 0.0)
        if not math.isfinite(seconds) or seconds < QUESTION_TIME_MIN_SECONDS:
            continue
        capped = min(seconds, QUESTION_TIME_CAP_SECONDS)
        valid_times.append(capped)
        if capped < fast_threshold and not bool(row.get("is_correct")):
            fast_incorrect += 1
        if capped > slow_threshold:
            slow_count += 1

    n_valid = len(valid_times)
    completeness = (n_valid / total_rows) if total_rows > 0 else 0.0
    median_time = statistics.median(valid_times) if valid_times else 0.0
    fast_incorrect_rate = (fast_incorrect / n_valid) if n_valid > 0 else 0.0
    slow_rate = (slow_count / n_valid) if n_valid > 0 else 0.0

    if completeness < 0.60:
        status = "Insufficient Timing Data"
    elif fast_incorrect_rate >= 0.20:
        status = "Too Fast / Likely Guessing"
    elif slow_rate >= 0.30:
        status = "Too Slow"
    else:
        status = "On Pace"

    return {
        "pacing_status": status,
        "timing_completeness": round(completeness, 3),
        "median_time_per_question": round(median_time, 2),
        "target_time_per_question": round(target, 2),
        "fast_incorrect_rate": round(fast_incorrect_rate, 3),
        "slow_answer_rate": round(slow_rate, 3),
        "timed_questions": n_valid,
    }


def _compute_confidence(
    eligible_mocks: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]],
    question_bank_total: int,
    expected_question_count: int,
    domain_stats: Dict[str, Dict[str, float]],
    weights: Dict[str, float],
) -> Dict[str, Any]:
    """Return confidence score (0–100) and label.  Confidence is separate from readiness."""
    mocks_done = len(eligible_mocks)

    # A. Mock volume — 30 pts
    mock_component = 30.0 * min(mocks_done / 5.0, 1.0)

    # B. Unique-question breadth — 30 pts
    bank = _safe_int(question_bank_total, 0)
    coverage_target = min(bank, expected_question_count * 3) if bank > 0 else expected_question_count * 3
    unique_seen = len({
        row.get("question_id")
        for row in (question_attempts or [])
        if row.get("question_id") is not None
    })
    breadth_component = 30.0 * min(unique_seen / coverage_target, 1.0) if coverage_target > 0 else 0.0

    # C. Question-attempt completeness — 20 pts (unique exam_attempt_id+question_id pairs)
    expected_linked = sum(_safe_int(a.get("total_questions"), 0) for a in eligible_mocks)
    unique_pairs = len({
        (str(row.get("exam_attempt_id")), str(row.get("question_id")))
        for row in (question_attempts or [])
        if row.get("exam_attempt_id") is not None and row.get("question_id") is not None
    })
    completeness = min(unique_pairs / expected_linked, 1.0) if expected_linked > 0 else 0.0
    completeness_component = 20.0 * completeness

    # D. Recency — 10 pts
    recency_component = 0.0
    newest_date: Optional[datetime] = None
    for a in eligible_mocks:
        dt_str = str(a.get("completed_at") or a.get("started_at") or "").strip()
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if newest_date is None or dt > newest_date:
                newest_date = dt
        except Exception:
            pass
    if newest_date is not None:
        age_days = (datetime.now(timezone.utc) - newest_date).total_seconds() / 86400.0
        if age_days <= 14:
            recency_component = 10.0
        elif age_days < 90:
            recency_component = 10.0 * (1.0 - (age_days - 14.0) / 76.0)

    # E. Domain sample sufficiency — 10 pts
    domain_suff = 0.0
    if domain_stats and weights:
        suff_pairs: List[Tuple[float, float]] = []
        for domain, w in weights.items():
            stats = domain_stats.get(domain)
            required = max(5, math.ceil(expected_question_count * w * 1.5))
            if stats is None:
                suff_pairs.append((w, 0.0))
                continue
            attempted = _safe_float(stats.get("total"), 0.0)
            suff_pairs.append((w, min(attempted / required, 1.0)))
        w_total = sum(w for w, _ in suff_pairs)
        if w_total > 0:
            domain_suff = sum(w * s for w, s in suff_pairs) / w_total
    domain_suff_component = 10.0 * domain_suff

    conf_score = _clamp(
        mock_component + breadth_component + completeness_component
        + recency_component + domain_suff_component
    )
    conf_label = "High" if conf_score >= 70 else ("Medium" if conf_score >= 40 else "Low")

    return {
        "confidence_score": round(conf_score, 2),
        "confidence_label": conf_label,
        "confidence": conf_label,  # backward-compatible key
        "unique_questions_seen": unique_seen,
        "question_attempt_completeness": round(completeness, 3),
        "domain_sample_sufficiency": round(domain_suff, 3),
        "coverage_percent": round((unique_seen / bank) * 100, 2) if bank > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Labels / colors
# ---------------------------------------------------------------------------

def readiness_label(score: float, passing_score: float, is_locked: bool = False) -> str:
    if is_locked:
        return "Readiness Locked"
    score = _safe_float(score)
    passing_score = _safe_float(passing_score, 65)
    if score < passing_score - 20:
        return "Not Ready"
    if score < passing_score - 10:
        return "Building Foundation"
    if score < passing_score:
        return "Close, But Risky"
    if score < passing_score + 8:
        return "Exam Ready"
    return "Strongly Ready"


def readiness_color(score: float, passing_score: float) -> str:
    score = _safe_float(score)
    passing_score = _safe_float(passing_score, 65)
    if score < passing_score - 10:
        return "red"
    if score < passing_score:
        return "orange"
    if score < passing_score + 8:
        return "blue"
    return "green"


# ---------------------------------------------------------------------------
# Empty / locked result template
# ---------------------------------------------------------------------------

def _empty_result(reason: str, eligible_mock_count: int = 0) -> Dict[str, Any]:
    return {
        "is_locked": True,
        "eligible_mock_count": eligible_mock_count,
        "required_mock_count": REQUIRED_FULL_MOCKS,
        "mocks_remaining": max(REQUIRED_FULL_MOCKS - eligible_mock_count, 0),
        "score": 0.0,
        "raw_score": 0.0,
        "label": "Readiness Locked",
        "color": "gray",
        "recommendation": reason,
        "recent_accuracy": 0.0,
        "domain_score": 0.0,
        "domain_robustness": 0.0,
        "weakest_reliable_domain": None,
        "weakest_reliable_domain_score": 0.0,
        "consistency_standard_deviation": 0.0,
        "consistency_penalty": 0.0,
        "trend_slope": 0.0,
        "trend_adjustment": 0.0,
        "trend_label": "Stable",
        "pacing_status": "Insufficient Timing Data",
        "timing_completeness": 0.0,
        "fast_incorrect_rate": 0.0,
        "slow_answer_rate": 0.0,
        "median_time_per_question": 0.0,
        "target_time_per_question": 0.0,
        "timed_questions": 0,
        "confidence_score": 0.0,
        "confidence_label": "Low",
        "confidence": "Low",
        "unique_questions_seen": 0,
        "question_attempt_completeness": 0.0,
        "domain_sample_sufficiency": 0.0,
        "coverage_percent": 0.0,
        # backward-compatible keys
        "accuracy_score": 0.0,
        "coverage_score": 0.0,
        "domain_balance_score": 0.0,
        "pacing_score": 0.0,
        "recent_mock_score": 0.0,
        "weighted_domain_score": 0.0,
        "consistency_score": 0.0,
        "practice_volume_score": 0.0,
        "total_attempted": 0,
        "full_mock_count": eligible_mock_count,
        "mock_scores_used": [],
        "guardrail_applied": False,
        "guardrail_cap": 0.0,
        "domain_scores": {},
        "strong_domains": [],
        "weak_domains": [],
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def calculate_readiness(
    attempts: List[Dict[str, Any]],
    passing_score: float = 65,
    domain_weights: Optional[Dict[str, Any]] = None,
    expected_question_count: int = 60,
    question_bank_total: Optional[int] = None,
    question_attempts: Optional[List[Dict[str, Any]]] = None,
    time_limit_minutes: int = 105,
) -> Dict[str, Any]:
    """Calculate V38 performance-anchored readiness.

    Returns a dict with readiness score, diagnostics, confidence, and backward-
    compatible keys so existing Dashboard/My_Progress display code still works.
    """
    attempts = attempts or []
    question_attempts = question_attempts or []
    passing_score = _safe_float(passing_score, 65.0)
    expected_question_count = _safe_int(expected_question_count, 60) or 60
    time_limit_minutes = _safe_int(time_limit_minutes, 105) or 105
    bank_total = _safe_int(question_bank_total, 0) if question_bank_total is not None else 0

    eligible_count = full_mock_count(attempts, expected_question_count)
    is_locked = eligible_count < REQUIRED_FULL_MOCKS

    # Always build domain stats (needed for locked diagnostics and Daily Sprint)
    domain_stats = _build_domain_stats(question_attempts, attempts)
    observed_domains = list(domain_stats.keys())
    weights = _normalize_weights(domain_weights, observed_domains)
    total_attempted = sum(_safe_int(a.get("total_questions"), 0) for a in attempts)

    sorted_domains = sorted(domain_stats.items(), key=lambda x: x[1].get("percent", 0.0))
    weak_domains = [n for n, d in sorted_domains[:3] if d.get("total", 0) > 0]
    strong_domains = [n for n, d in sorted_domains[-3:]][::-1] if sorted_domains else []

    pacing = _compute_pacing_diagnostics(question_attempts, time_limit_minutes, expected_question_count)

    if is_locked:
        conf = _compute_confidence(attempts, question_attempts, bank_total, expected_question_count, domain_stats, weights)
        result = _empty_result(
            f"Complete {REQUIRED_FULL_MOCKS} full paid mock exams to unlock readiness. "
            f"You have {eligible_count} of {REQUIRED_FULL_MOCKS} required.",
            eligible_count,
        )
        result.update(conf)
        result.update(pacing)
        result["domain_scores"] = domain_stats
        result["weak_domains"] = weak_domains
        result["strong_domains"] = strong_domains
        result["total_attempted"] = total_attempted
        return result

    # ------------------------------------------------------------------
    # V38 readiness calculation — unlocked path
    # ------------------------------------------------------------------

    recent = _select_recent_mocks(attempts, MAX_RECENT_MOCKS)
    scores = [_clamp(_safe_float(a.get("score"), 0.0)) for a in recent]

    # A. Recent mock accuracy (EMA, alpha=0.40)
    A = _compute_ema(scores, EMA_ALPHA)

    # B+C. Weighted domain accuracy D and domain robustness DR
    D, DR, weakest_reliable_domain, weakest_score = _compute_domain_score(
        domain_stats, weights, expected_question_count
    )
    if not domain_stats:
        D = A
        DR = A

    # D. Performance base
    Base = 0.80 * A + 0.20 * DR

    # E. Consistency penalty
    SD = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    consistency_penalty = min(8.0, max(0.0, (SD - 5.0) * 0.25))

    # F. Trend adjustment
    if len(scores) >= 2:
        deltas = [scores[i + 1] - scores[i] for i in range(len(scores) - 1)]
        slope = sum(deltas) / len(deltas)
    else:
        slope = 0.0
    trend_adjustment = _clamp(0.30 * slope, -6.0, 4.0)
    trend_label = "Improving" if slope >= 2.0 else ("Declining" if slope <= -2.0 else "Stable")

    # G. Final score — hard cap: readiness cannot exceed A+5
    uncapped = Base - consistency_penalty + trend_adjustment
    final_score = round(_clamp(min(uncapped, A + 5.0)), 2)

    # Confidence (separate axis, computed over recent eligible mocks)
    conf = _compute_confidence(
        recent, question_attempts, bank_total, expected_question_count, domain_stats, weights
    )

    label = readiness_label(final_score, passing_score, is_locked=False)

    if final_score < passing_score:
        focus = weak_domains[0] if weak_domains else "your weakest domains"
        recommendation = (
            f"Your readiness is below the passing benchmark. "
            f"Focus next on {focus}, then retake a full mock exam."
        )
    elif weakest_reliable_domain:
        recommendation = (
            f"You are trending exam-ready, but {weakest_reliable_domain} remains your "
            f"highest-risk area. Strengthen it before scheduling."
        )
    else:
        recommendation = (
            "You are trending exam-ready. Take another full mock exam to confirm consistency."
        )

    return {
        # Eligibility
        "is_locked": False,
        "eligible_mock_count": eligible_count,
        "required_mock_count": REQUIRED_FULL_MOCKS,
        "mocks_remaining": 0,
        # Score
        "score": final_score,
        "raw_score": round(_clamp(uncapped), 2),
        "label": label,
        "color": readiness_color(final_score, passing_score),
        "recommendation": recommendation,
        # V38 diagnostic keys
        "recent_accuracy": round(A, 2),
        "domain_score": round(D, 2),
        "domain_robustness": round(DR, 2),
        "weakest_reliable_domain": weakest_reliable_domain,
        "weakest_reliable_domain_score": weakest_score,
        "consistency_standard_deviation": round(SD, 2),
        "consistency_penalty": round(consistency_penalty, 2),
        "trend_slope": round(slope, 2),
        "trend_adjustment": round(trend_adjustment, 2),
        "trend_label": trend_label,
        # Pacing (diagnostics only)
        **pacing,
        # Confidence (separate axis)
        **conf,
        # Domain lists (Daily Sprint uses weak_domains[0])
        "domain_scores": domain_stats,
        "weak_domains": weak_domains,
        "strong_domains": strong_domains,
        # Backward-compatible keys — values are kept for compat but no longer score contributors
        "accuracy_score": round(A, 2),
        "recent_mock_score": round(A, 2),
        "domain_balance_score": round(DR, 2),
        "weighted_domain_score": round(DR, 2),
        "coverage_score": 0.0,       # removed from score formula; kept for compat
        "pacing_score": 0.0,         # removed from score formula; kept for compat
        "consistency_score": 0.0,
        "practice_volume_score": 0.0,
        "total_attempted": total_attempted,
        "full_mock_count": eligible_count,
        "mock_scores_used": list(reversed(scores[-5:])),
        "guardrail_applied": False,
        "guardrail_cap": 0.0,
    }


# ---------------------------------------------------------------------------
# User-facing methodology text
# ---------------------------------------------------------------------------

def readiness_methodology_text() -> str:
    return (
        "Readiness estimates your likely exam performance based on your recent full paid mock scores. "
        "Domain performance, consistency, and trend refine the estimate. "
        "Coverage and mock volume affect Confidence only — they do not add readiness points. "
        "Pacing is diagnostic only. "
        "Confidence measures how well-supported the estimate is, not your probability of passing. "
        "At least 3 full paid mock exams are required before readiness is shown."
    )
