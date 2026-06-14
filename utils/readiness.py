import json
from typing import Any, Dict, List, Tuple


def normalize_breakdown(value: Any) -> Dict[str, Dict[str, float]]:
    """Normalize domain_breakdown saved in Supabase into a dict."""
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
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def readiness_label(score: float, passing_score: float) -> str:
    """Return a user-friendly readiness label based on score and exam pass mark."""
    score = _safe_float(score)
    passing_score = _safe_float(passing_score, 65)
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


def is_full_mock_attempt(attempt: Dict[str, Any], expected_question_count: int = 60) -> bool:
    mode = str(attempt.get("mode") or "").lower()
    total = _safe_int(attempt.get("total_questions"), 0)
    return "mock" in mode or total >= max(50, expected_question_count - 5)


def calculate_recent_mock_score(attempts: List[Dict[str, Any]], expected_question_count: int = 60) -> Tuple[float, List[float]]:
    full_mocks = [a for a in attempts if is_full_mock_attempt(a, expected_question_count)]
    recent = full_mocks[:3]
    if not recent:
        recent = attempts[:3]
    scores = [_safe_float(a.get("score"), 0.0) for a in recent]
    if not scores:
        return 0.0, []
    weights = [0.50, 0.30, 0.20]
    if len(scores) == 1:
        return scores[0], scores
    if len(scores) == 2:
        total = (scores[0] * 0.60) + (scores[1] * 0.40)
        return round(total, 2), scores
    total = sum(score * weights[idx] for idx, score in enumerate(scores[:3]))
    return round(total, 2), scores


def aggregate_domain_scores(attempts: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    totals: Dict[str, Dict[str, float]] = {}
    for attempt in attempts:
        breakdown = normalize_breakdown(attempt.get("domain_breakdown"))
        for domain, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            correct = _safe_float(data.get("correct"), 0.0)
            total = _safe_float(data.get("total"), 0.0)
            if total <= 0:
                continue
            if domain not in totals:
                totals[domain] = {"correct": 0.0, "total": 0.0, "percent": 0.0}
            totals[domain]["correct"] += correct
            totals[domain]["total"] += total
    for domain, data in totals.items():
        data["percent"] = round((data["correct"] / data["total"]) * 100, 2) if data["total"] else 0.0
    return totals


def calculate_weighted_domain_score(
    attempts: List[Dict[str, Any]],
    domain_weights: Dict[str, float] | None = None,
) -> Tuple[float, Dict[str, Dict[str, float]]]:
    domain_scores = aggregate_domain_scores(attempts)
    if not domain_scores:
        return 0.0, {}

    domain_weights = domain_weights or {}
    weight_total = sum(_safe_float(w) for w in domain_weights.values())

    # If official weights are missing, use a simple average of attempted domains.
    if not domain_weights or weight_total <= 0:
        avg = sum(d["percent"] for d in domain_scores.values()) / len(domain_scores)
        return round(avg, 2), domain_scores

    weighted_sum = 0.0
    applied_weight_total = 0.0
    for domain, weight in domain_weights.items():
        weight = _safe_float(weight)
        if domain in domain_scores:
            weighted_sum += domain_scores[domain]["percent"] * weight
            applied_weight_total += weight

    if applied_weight_total <= 0:
        avg = sum(d["percent"] for d in domain_scores.values()) / len(domain_scores)
        return round(avg, 2), domain_scores

    return round(weighted_sum / applied_weight_total, 2), domain_scores


def calculate_consistency_score(attempts: List[Dict[str, Any]], passing_score: float, expected_question_count: int = 60) -> float:
    full_mocks = [a for a in attempts if is_full_mock_attempt(a, expected_question_count)]
    recent = full_mocks[:3]
    if not recent:
        return 0.0
    passed = sum(1 for a in recent if _safe_float(a.get("score"), 0.0) >= passing_score)
    if len(recent) == 1:
        return 60.0 if passed == 1 else 20.0
    if len(recent) == 2:
        return {0: 15.0, 1: 55.0, 2: 85.0}.get(passed, 15.0)
    return {0: 15.0, 1: 45.0, 2: 75.0, 3: 100.0}.get(passed, 15.0)


def calculate_practice_volume_score(attempts: List[Dict[str, Any]], question_bank_total: int | None = None) -> Tuple[float, int]:
    total_attempted = sum(_safe_int(a.get("total_questions"), 0) for a in attempts)
    target = question_bank_total or 120
    target = max(60, min(target, 150))

    if total_attempted <= 0:
        return 0.0, 0
    if total_attempted >= target:
        return 100.0, total_attempted
    if total_attempted >= target * 0.75:
        return 80.0, total_attempted
    if total_attempted >= target * 0.50:
        return 60.0, total_attempted
    if total_attempted >= target * 0.25:
        return 35.0, total_attempted
    return 15.0, total_attempted


def confidence_level(attempts: List[Dict[str, Any]], total_attempted: int, expected_question_count: int = 60) -> str:
    full_mock_count = sum(1 for a in attempts if is_full_mock_attempt(a, expected_question_count))
    if full_mock_count >= 3 and total_attempted >= 120:
        return "High"
    if full_mock_count >= 2 and total_attempted >= 90:
        return "Medium"
    if full_mock_count >= 1 and total_attempted >= 60:
        return "Low"
    return "Very Low"


def calculate_readiness(
    attempts: List[Dict[str, Any]],
    passing_score: float = 65,
    domain_weights: Dict[str, float] | None = None,
    expected_question_count: int = 60,
    question_bank_total: int | None = None,
) -> Dict[str, Any]:
    attempts = attempts or []
    passing_score = _safe_float(passing_score, 65.0)
    expected_question_count = _safe_int(expected_question_count, 60)

    if not attempts:
        return {
            "score": 0.0,
            "label": "Not Enough Data",
            "color": "gray",
            "recent_mock_score": 0.0,
            "weighted_domain_score": 0.0,
            "consistency_score": 0.0,
            "practice_volume_score": 0.0,
            "total_attempted": 0,
            "confidence": "No Data",
            "domain_scores": {},
            "strong_domains": [],
            "weak_domains": [],
            "mock_scores_used": [],
            "recommendation": "Complete at least one full mock exam before trusting a readiness estimate.",
        }

    recent_mock_score, mock_scores = calculate_recent_mock_score(attempts, expected_question_count)
    weighted_domain_score, domain_scores = calculate_weighted_domain_score(attempts, domain_weights)
    consistency_score = calculate_consistency_score(attempts, passing_score, expected_question_count)
    practice_volume_score, total_attempted = calculate_practice_volume_score(attempts, question_bank_total)

    # If no domain data exists, avoid pretending the domain score is meaningful.
    effective_domain_score = weighted_domain_score if domain_scores else recent_mock_score

    score = (
        recent_mock_score * 0.50
        + effective_domain_score * 0.30
        + consistency_score * 0.10
        + practice_volume_score * 0.10
    )
    score = round(score, 2)

    sorted_domains = sorted(domain_scores.items(), key=lambda item: item[1].get("percent", 0.0))
    weak_domains = [name for name, data in sorted_domains[:3] if data.get("total", 0) > 0]
    strong_domains = [name for name, data in sorted_domains[-3:]][::-1] if sorted_domains else []

    label = readiness_label(score, passing_score)
    confidence = confidence_level(attempts, total_attempted, expected_question_count)

    if confidence in {"No Data", "Very Low", "Low"}:
        recommendation = "Do not rely on this score yet. Complete more full mock exams and category practice to build a stronger signal."
    elif score < passing_score:
        focus = weak_domains[0] if weak_domains else "your weakest domains"
        recommendation = f"You are below the passing benchmark. Focus next on {focus}, then retake a full mock exam."
    elif weak_domains:
        recommendation = f"You are trending exam-ready, but {weak_domains[0]} is the highest-risk area. Strengthen it before scheduling the real exam."
    else:
        recommendation = "You are trending exam-ready. Maintain consistency with another full mock exam before scheduling."

    return {
        "score": score,
        "label": label,
        "color": readiness_color(score, passing_score),
        "recent_mock_score": recent_mock_score,
        "weighted_domain_score": weighted_domain_score,
        "consistency_score": round(consistency_score, 2),
        "practice_volume_score": round(practice_volume_score, 2),
        "total_attempted": total_attempted,
        "confidence": confidence,
        "domain_scores": domain_scores,
        "strong_domains": strong_domains,
        "weak_domains": weak_domains,
        "mock_scores_used": mock_scores,
        "recommendation": recommendation,
    }


def readiness_methodology_text() -> str:
    return (
        "Readiness is an estimate, not a guarantee. It combines recent mock exam performance, "
        "officially weighted domain performance, consistency across recent mock exams, and practice volume. "
        "The score becomes more reliable after multiple full mock exams and enough practice across all domains."
    )
