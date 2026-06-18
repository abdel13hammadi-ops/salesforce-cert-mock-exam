import json
import math
import statistics
from typing import Any, Dict, List, Tuple

APP_VERSION = "READINESS_V3_FULL_MOCK_ONLY"

EMA_ALPHA = 0.30
M_TARGET_FULL_MOCKS = 3
READINESS_GUARDRAIL_CAP = 65.0
QUESTION_TIME_CAP_SECONDS = 300.0


def normalize_breakdown(value: Any) -> Dict[str, Dict[str, float]]:
    """Normalize domain_breakdown/difficulty_breakdown saved in Supabase into a dict."""
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


def readiness_label(score: float, passing_score: float) -> str:
    score = _safe_float(score)
    passing_score = _safe_float(passing_score, 65)
    if score <= 0:
        return "Not Enough Data"
    if score < 50:
        return "Not Ready"
    if score < max(60, passing_score - 5):
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


def _parse_sort_value(attempt: Dict[str, Any]) -> str:
    return str(attempt.get("completed_at") or attempt.get("started_at") or attempt.get("id") or "")


def is_full_mock_attempt(attempt: Dict[str, Any], expected_question_count: int = 60) -> bool:
    """Return True only for readiness-eligible full paid mock exams.

    CertBound readiness must be based only on full Paid Mock Exams.
    Timed exams, practice mode, free previews, and partial drills are learning activity;
    they should not drive readiness scoring or unlock readiness.
    """
    mode = str(attempt.get("mode") or "").strip().lower()
    total = _safe_int(attempt.get("total_questions"), 0)
    full_enough = total >= int(expected_question_count or 60)
    return mode == "paid mock exam" and full_enough


def full_mock_count(attempts: List[Dict[str, Any]], expected_question_count: int = 60) -> int:
    return sum(1 for attempt in attempts or [] if is_full_mock_attempt(attempt, expected_question_count))


def calculate_recency_weighted_accuracy(attempts: List[Dict[str, Any]], alpha: float = EMA_ALPHA) -> Tuple[float, List[float]]:
    """EMA over attempt scores. Oldest score starts the EMA; newest attempts carry alpha weight."""
    usable = [a for a in attempts or [] if a.get("score") is not None]
    if not usable:
        return 0.0, []
    chronological = sorted(usable, key=_parse_sort_value)
    scores = [_clamp(_safe_float(a.get("score"), 0.0)) for a in chronological]
    ema = scores[0]
    for score in scores[1:]:
        ema = (alpha * score) + ((1 - alpha) * ema)
    return round(_clamp(ema), 2), list(reversed(scores[-5:]))


def aggregate_domain_scores_from_attempts(attempts: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    totals: Dict[str, Dict[str, float]] = {}
    for attempt in attempts or []:
        breakdown = normalize_breakdown(attempt.get("domain_breakdown"))
        for domain, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            correct = _safe_float(data.get("correct"), 0.0)
            total = _safe_float(data.get("total"), 0.0)
            if total <= 0:
                continue
            totals.setdefault(str(domain), {"correct": 0.0, "total": 0.0, "percent": 0.0})
            totals[str(domain)]["correct"] += correct
            totals[str(domain)]["total"] += total
    for domain, data in totals.items():
        data["percent"] = round((data["correct"] / data["total"]) * 100, 2) if data["total"] else 0.0
    return totals


def aggregate_domain_scores_from_question_attempts(question_attempts: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    totals: Dict[str, Dict[str, float]] = {}
    for row in question_attempts or []:
        domain = str(row.get("category") or "Uncategorized")
        totals.setdefault(domain, {"correct": 0.0, "total": 0.0, "percent": 0.0})
        totals[domain]["total"] += 1
        if bool(row.get("is_correct")):
            totals[domain]["correct"] += 1
    for domain, data in totals.items():
        data["percent"] = round((data["correct"] / data["total"]) * 100, 2) if data["total"] else 0.0
    return totals


def calculate_domain_balance_score(
    attempts: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]] | None = None,
) -> Tuple[float, Dict[str, Dict[str, float]], float, float]:
    """B_dom = mean(domain scores) - 2 * stddev(domain scores), clamped 0..100."""
    domain_scores = aggregate_domain_scores_from_question_attempts(question_attempts or [])
    if not domain_scores:
        domain_scores = aggregate_domain_scores_from_attempts(attempts)
    if not domain_scores:
        return 0.0, {}, 0.0, 0.0

    percents = [_clamp(data.get("percent", 0.0)) for data in domain_scores.values() if _safe_float(data.get("total"), 0.0) > 0]
    if not percents:
        return 0.0, domain_scores, 0.0, 0.0

    mu = sum(percents) / len(percents)
    sigma = statistics.pstdev(percents) if len(percents) > 1 else 0.0
    balance = _clamp(mu - (2 * sigma))
    return round(balance, 2), domain_scores, round(mu, 2), round(sigma, 2)


def calculate_coverage_score(
    attempts: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]] | None,
    question_bank_total: int | None,
    expected_question_count: int = 60,
) -> Tuple[float, int, float, int, float]:
    """C_comp blends unique question coverage and progress toward 3 full mocks."""
    question_attempts = question_attempts or []
    unique_seen = len({row.get("question_id") for row in question_attempts if row.get("question_id") is not None})

    bank_total = _safe_int(question_bank_total, 0)
    if bank_total <= 0:
        bank_total = max(_safe_int(sum(_safe_int(a.get("total_questions"), 0) for a in attempts), 0), expected_question_count)

    question_coverage_pct = _clamp((unique_seen / bank_total) * 100) if bank_total > 0 else 0.0
    mocks_done = full_mock_count(attempts, expected_question_count)
    mock_progress_pct = _clamp((mocks_done / M_TARGET_FULL_MOCKS) * 100)

    # Unique coverage matters slightly more than raw full-mock count.
    coverage = (question_coverage_pct * 0.60) + (mock_progress_pct * 0.40)
    return round(_clamp(coverage), 2), unique_seen, round(question_coverage_pct, 2), mocks_done, round(mock_progress_pct, 2)


def calculate_pacing_stability(
    question_attempts: List[Dict[str, Any]] | None,
    time_limit_minutes: int = 105,
    expected_question_count: int = 60,
) -> Tuple[float, float, float, int]:
    """P_time uses median capped per-question time so abandoned tabs do not destroy the score."""
    target = (_safe_float(time_limit_minutes, 105.0) * 60.0) / max(_safe_int(expected_question_count, 60), 1)
    times: List[float] = []
    for row in question_attempts or []:
        seconds = _safe_float(row.get("time_spent_seconds"), 0.0)
        if seconds <= 0:
            continue
        times.append(min(seconds, QUESTION_TIME_CAP_SECONDS))

    if not times:
        return 0.0, 0.0, round(target, 2), 0

    median_time = statistics.median(times)
    if median_time <= target:
        score = 100.0
    else:
        score = _clamp((target / median_time) * 100)
    return round(score, 2), round(median_time, 2), round(target, 2), len(times)


def confidence_level(
    attempts: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]] | None,
    expected_question_count: int = 60,
) -> str:
    mocks = full_mock_count(attempts, expected_question_count)
    unique_seen = len({row.get("question_id") for row in question_attempts or [] if row.get("question_id") is not None})
    if mocks >= 3 and unique_seen >= expected_question_count * 2:
        return "High"
    if mocks >= 2 and unique_seen >= expected_question_count:
        return "Medium"
    if mocks >= 1 or unique_seen >= 30:
        return "Low"
    return "Very Low"


def calculate_readiness(
    attempts: List[Dict[str, Any]],
    passing_score: float = 65,
    domain_weights: Dict[str, float] | None = None,  # kept for backward-compatible calls
    expected_question_count: int = 60,
    question_bank_total: int | None = None,
    question_attempts: List[Dict[str, Any]] | None = None,
    time_limit_minutes: int = 105,
) -> Dict[str, Any]:
    attempts = attempts or []
    question_attempts = question_attempts or []
    passing_score = _safe_float(passing_score, 65.0)
    expected_question_count = _safe_int(expected_question_count, 60) or 60
    time_limit_minutes = _safe_int(time_limit_minutes, 105) or 105

    if not attempts and not question_attempts:
        return {
            "score": 0.0,
            "raw_score": 0.0,
            "label": "Not Enough Data",
            "color": "gray",
            "accuracy_score": 0.0,
            "coverage_score": 0.0,
            "domain_balance_score": 0.0,
            "pacing_score": 0.0,
            "recent_mock_score": 0.0,  # backward-compatible key
            "weighted_domain_score": 0.0,  # backward-compatible key
            "consistency_score": 0.0,  # no longer used
            "practice_volume_score": 0.0,  # backward-compatible display key
            "total_attempted": 0,
            "unique_questions_seen": 0,
            "full_mock_count": 0,
            "guardrail_applied": False,
            "confidence": "No Data",
            "domain_scores": {},
            "strong_domains": [],
            "weak_domains": [],
            "mock_scores_used": [],
            "recommendation": "Complete at least one mock exam before trusting a readiness estimate.",
        }

    accuracy_score, scores_used = calculate_recency_weighted_accuracy(attempts, EMA_ALPHA)
    coverage_score, unique_seen, question_coverage_pct, mocks_done, mock_progress_pct = calculate_coverage_score(
        attempts, question_attempts, question_bank_total, expected_question_count
    )
    domain_balance_score, domain_scores, domain_mean, domain_sigma = calculate_domain_balance_score(attempts, question_attempts)
    pacing_score, median_time, target_time, timed_questions = calculate_pacing_stability(
        question_attempts, time_limit_minutes, expected_question_count
    )

    raw_score = (
        accuracy_score * 0.40
        + coverage_score * 0.25
        + domain_balance_score * 0.20
        + pacing_score * 0.15
    )
    raw_score = round(_clamp(raw_score), 2)

    guardrail_applied = mocks_done < 2 and raw_score > READINESS_GUARDRAIL_CAP
    final_score = round(min(raw_score, READINESS_GUARDRAIL_CAP), 2) if guardrail_applied else raw_score

    sorted_domains = sorted(domain_scores.items(), key=lambda item: item[1].get("percent", 0.0))
    weak_domains = [name for name, data in sorted_domains[:3] if data.get("total", 0) > 0]
    strong_domains = [name for name, data in sorted_domains[-3:]][::-1] if sorted_domains else []

    confidence = confidence_level(attempts, question_attempts, expected_question_count)
    label = readiness_label(final_score, passing_score)

    if mocks_done < 2:
        recommendation = "Readiness is capped at 65 until you complete at least 2 full-length mock exams. This prevents false confidence from short practice sessions."
    elif final_score < passing_score:
        focus = weak_domains[0] if weak_domains else "your weakest domains"
        recommendation = f"You are below the passing benchmark. Focus next on {focus}, then retake a full mock exam."
    elif weak_domains:
        recommendation = f"You are trending exam-ready, but {weak_domains[0]} is the highest-risk area. Strengthen it before scheduling the real exam."
    else:
        recommendation = "You are trending exam-ready. Maintain consistency with another full mock exam before scheduling."

    return {
        "score": final_score,
        "raw_score": raw_score,
        "label": label,
        "color": readiness_color(final_score, passing_score),
        "accuracy_score": round(accuracy_score, 2),
        "coverage_score": round(coverage_score, 2),
        "domain_balance_score": round(domain_balance_score, 2),
        "pacing_score": round(pacing_score, 2),
        # Backward-compatible keys used by older dashboard/progress code.
        "recent_mock_score": round(accuracy_score, 2),
        "weighted_domain_score": round(domain_balance_score, 2),
        "consistency_score": 0.0,
        "practice_volume_score": round(coverage_score, 2),
        "total_attempted": sum(_safe_int(a.get("total_questions"), 0) for a in attempts),
        "unique_questions_seen": unique_seen,
        "question_coverage_pct": question_coverage_pct,
        "full_mock_count": mocks_done,
        "mock_progress_pct": mock_progress_pct,
        "guardrail_applied": guardrail_applied,
        "guardrail_cap": READINESS_GUARDRAIL_CAP,
        "confidence": confidence,
        "domain_scores": domain_scores,
        "domain_mean": domain_mean,
        "domain_sigma": domain_sigma,
        "median_time_per_question": median_time,
        "target_time_per_question": target_time,
        "timed_questions": timed_questions,
        "strong_domains": strong_domains,
        "weak_domains": weak_domains,
        "mock_scores_used": scores_used,
        "recommendation": recommendation,
    }


def readiness_methodology_text() -> str:
    return (
        "Readiness uses the CertBound multidimensional formula: 40% recency-weighted accuracy "
        "(EMA alpha 0.30), 25% coverage and comprehensiveness, 20% domain balance, and 15% pacing stability. "
        "A guardrail caps visible readiness at 65 until at least two full-length mock exams are completed."
    )
