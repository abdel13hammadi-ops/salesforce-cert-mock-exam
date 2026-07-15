"""Shared learner-facing analytics contracts for Dashboard and My Progress.

Pure functions only — no Streamlit, chart libraries, or rendering code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from utils.activity_modes import (
    ALL_ACTIVITY_MODES,
    DAILY_SPRINT,
    FREE_MOCK_EXAM,
    PAID_MOCK_EXAM,
    PRACTICE_BY_CATEGORY,
    WEAK_AREAS_PRACTICE,
    is_readiness_eligible_mode,
)

DEFAULT_ACTIVITY_WINDOW_DAYS = 30
DOMAIN_EVIDENCE_MIN_QUESTIONS = 5


def _readiness_helpers():
    from utils.readiness import (
        _build_domain_stats,
        filter_verified_mock_attempts,
        filter_verified_question_attempts,
        v5_parse_attempt_id,
    )

    return (
        _build_domain_stats,
        filter_verified_mock_attempts,
        filter_verified_question_attempts,
        v5_parse_attempt_id,
    )


# ---------------------------------------------------------------------------
# Shared attempt filtering
# ---------------------------------------------------------------------------


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


def parse_attempt_datetime(value: Any) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def sort_attempts_newest_first(attempts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        list(attempts or []),
        key=lambda attempt: (
            parse_attempt_datetime(attempt.get("completed_at")),
            parse_attempt_datetime(attempt.get("started_at")),
            _safe_int(attempt.get("id"), 0),
        ),
        reverse=True,
    )


def sort_attempts_oldest_first(attempts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return list(reversed(sort_attempts_newest_first(attempts)))


def filter_readiness_attempts(
    attempts: Sequence[Dict[str, Any]],
    expected_question_count: int = 60,
) -> List[Dict[str, Any]]:
    """Keep only full-length paid mock exam attempts used for readiness."""
    filtered: List[Dict[str, Any]] = []
    minimum = int(expected_question_count or 60)
    for attempt in attempts or []:
        mode = str(attempt.get("mode") or "").strip()
        if mode == PAID_MOCK_EXAM and _safe_int(attempt.get("total_questions"), 0) >= minimum:
            filtered.append(dict(attempt))
    return filtered


def filter_question_attempts_for_attempts(
    question_attempts: Sequence[Dict[str, Any]],
    attempts: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep question rows linked to the supplied parent attempts."""
    eligible_ids = {
        str(attempt.get("id"))
        for attempt in attempts or []
        if attempt.get("id") is not None
    }
    if not eligible_ids:
        return []
    return [
        dict(row)
        for row in question_attempts or []
        if str(row.get("exam_attempt_id")) in eligible_ids
    ]


def is_readiness_eligible_attempt(
    attempt: Mapping[str, Any],
    expected_question_count: int = 60,
) -> bool:
    mode = str(attempt.get("mode") or "").strip()
    return (
        is_readiness_eligible_mode(mode)
        and _safe_int(attempt.get("total_questions"), 0) >= int(expected_question_count or 60)
    )


# ---------------------------------------------------------------------------
# Verified mock performance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreTrendPoint:
    attempt_id: Optional[int]
    completed_at: datetime
    score: float
    sequence_number: int
    passing_threshold: Optional[float] = None


@dataclass(frozen=True)
class VerifiedMockPerformance:
    attempt_count: int
    latest_score: Optional[float]
    average_score: Optional[float]
    best_score: Optional[float]
    previous_score: Optional[float]
    score_change: Optional[float]
    score_series: Tuple[ScoreTrendPoint, ...]
    passing_threshold: Optional[float]
    has_sufficient_data: bool
    readiness_eligible_attempt_ids: Tuple[int, ...]

    @property
    def has_verified_mocks(self) -> bool:
        return self.attempt_count > 0 and self.has_sufficient_data


def build_score_trend_points(
    verified_attempts: Sequence[Dict[str, Any]],
    *,
    passing_threshold: Optional[float] = None,
) -> Tuple[ScoreTrendPoint, ...]:
    """Return chronological chart-ready score points with deterministic ordering."""
    _, _, _, v5_parse_attempt_id = _readiness_helpers()
    chronological = sort_attempts_oldest_first(verified_attempts)
    points: List[ScoreTrendPoint] = []
    for index, attempt in enumerate(chronological, start=1):
        completed = parse_attempt_datetime(attempt.get("completed_at") or attempt.get("started_at"))
        points.append(
            ScoreTrendPoint(
                attempt_id=v5_parse_attempt_id(attempt),
                completed_at=completed,
                score=_safe_float(attempt.get("score"), 0.0),
                sequence_number=index,
                passing_threshold=passing_threshold,
            )
        )
    return tuple(points)


def build_verified_mock_performance(
    attempts: Sequence[Dict[str, Any]],
    question_attempts: Sequence[Dict[str, Any]],
    expected_question_count: int = 60,
    *,
    passing_threshold: Optional[float] = None,
) -> VerifiedMockPerformance:
    """Canonical verified full paid-mock performance contract."""
    _, filter_verified_mock_attempts, _, v5_parse_attempt_id = _readiness_helpers()
    verified_attempts = filter_verified_mock_attempts(
        list(attempts),
        list(question_attempts),
        expected_question_count,
    )
    eligible_ids = tuple(
        attempt_id
        for attempt_id in (v5_parse_attempt_id(attempt) for attempt in verified_attempts)
        if attempt_id is not None
    )

    if not verified_attempts:
        return VerifiedMockPerformance(
            attempt_count=0,
            latest_score=None,
            average_score=None,
            best_score=None,
            previous_score=None,
            score_change=None,
            score_series=(),
            passing_threshold=passing_threshold,
            has_sufficient_data=False,
            readiness_eligible_attempt_ids=eligible_ids,
        )

    scores = [_safe_float(attempt.get("score"), 0.0) for attempt in verified_attempts]
    latest = scores[0]
    previous = scores[1] if len(scores) > 1 else None
    score_change = round(latest - previous, 2) if previous is not None else None
    score_series = build_score_trend_points(
        verified_attempts,
        passing_threshold=passing_threshold,
    )

    return VerifiedMockPerformance(
        attempt_count=len(verified_attempts),
        latest_score=latest,
        average_score=round(sum(scores) / len(scores), 2),
        best_score=round(max(scores), 2),
        previous_score=previous,
        score_change=score_change,
        score_series=score_series,
        passing_threshold=passing_threshold,
        has_sufficient_data=True,
        readiness_eligible_attempt_ids=eligible_ids,
    )


def build_verified_mock_performance_metrics(
    attempts: Sequence[Dict[str, Any]],
    question_attempts: Sequence[Dict[str, Any]],
    expected_question_count: int = 60,
    *,
    passing_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    """Backward-compatible dict view of verified mock performance."""
    _, filter_verified_mock_attempts, _, _ = _readiness_helpers()
    contract = build_verified_mock_performance(
        attempts,
        question_attempts,
        expected_question_count,
        passing_threshold=passing_threshold,
    )
    trend_attempts = sort_attempts_oldest_first(
        filter_verified_mock_attempts(
            list(attempts),
            list(question_attempts),
            expected_question_count,
        )
    )
    return {
        "has_verified_mocks": contract.has_verified_mocks,
        "latest_score": contract.latest_score,
        "average_score": contract.average_score,
        "best_score": contract.best_score,
        "previous_score": contract.previous_score,
        "score_change": contract.score_change,
        "verified_mock_count": contract.attempt_count,
        "trend_attempts": trend_attempts,
        "score_series": contract.score_series,
        "passing_threshold": contract.passing_threshold,
        "has_sufficient_data": contract.has_sufficient_data,
        "readiness_eligible_attempt_ids": list(contract.readiness_eligible_attempt_ids),
    }


# ---------------------------------------------------------------------------
# Readiness display adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadinessDisplayContract:
    is_locked: bool
    readiness_score: Optional[float]
    completed_verified_mock_count: int
    required_mock_count: int
    remaining_mock_count: int
    confidence_score: float
    confidence_label: str
    readiness_label: str
    score_trend_indicator: str
    recommended_next_action: str
    unlock_message_key: str
    progress_message_key: str


def build_readiness_display_contract(readiness: Mapping[str, Any]) -> ReadinessDisplayContract:
    """Adapt calculate_readiness() output into a stable display contract."""
    is_locked = bool(readiness.get("is_locked"))
    completed = _safe_int(readiness.get("eligible_mock_count"), 0)
    required = _safe_int(readiness.get("required_mock_count"), 3)
    remaining = _safe_int(readiness.get("mocks_remaining"), max(required - completed, 0))
    readiness_score = None if is_locked else _safe_float(readiness.get("score"))

    return ReadinessDisplayContract(
        is_locked=is_locked,
        readiness_score=readiness_score,
        completed_verified_mock_count=completed,
        required_mock_count=required,
        remaining_mock_count=remaining,
        confidence_score=_safe_float(readiness.get("confidence_score"), 0.0),
        confidence_label=str(readiness.get("confidence_label") or "Low"),
        readiness_label=str(readiness.get("label") or ("Readiness Locked" if is_locked else "Not Enough Data")),
        score_trend_indicator=str(readiness.get("trend_label") or "Stable"),
        recommended_next_action=str(
            readiness.get("recommendation")
            or "Complete more verified mock exams to improve the readiness signal."
        ),
        unlock_message_key="readiness_unlock_after_verified_mocks",
        progress_message_key="readiness_progress_completed_of_required",
    )


# ---------------------------------------------------------------------------
# Verified domain performance
# ---------------------------------------------------------------------------


def _domain_status_label(accuracy: float, passing_threshold: Optional[float]) -> str:
    threshold = _safe_float(passing_threshold, 68.0)
    if accuracy < threshold - 15:
        return "high_risk"
    if accuracy < threshold:
        return "below_target"
    if accuracy < threshold + 10:
        return "on_target"
    return "strong"


def build_verified_domain_performance(
    attempts: Sequence[Dict[str, Any]],
    question_attempts: Sequence[Dict[str, Any]],
    expected_question_count: int = 60,
    *,
    domain_weights: Optional[Mapping[str, float]] = None,
    passing_threshold: Optional[float] = None,
    evidence_min_questions: int = DOMAIN_EVIDENCE_MIN_QUESTIONS,
) -> List[Dict[str, Any]]:
    """Canonical verified domain rows using My Progress evidence rules."""
    _build_domain_stats, _, filter_verified_question_attempts, _ = _readiness_helpers()
    verified_qa = filter_verified_question_attempts(
        list(attempts),
        list(question_attempts),
        expected_question_count,
    )
    if not verified_qa:
        return []

    stats = _build_domain_stats(verified_qa, [])
    weights = dict(domain_weights or {})
    display_order = {
        domain: index
        for index, domain in enumerate(
            sorted(weights.keys(), key=lambda name: (-weights.get(name, 0.0), name))
        )
    }

    rows: List[Dict[str, Any]] = []
    for name, data in stats.items():
        total = _safe_float(data.get("total"), 0.0)
        correct = _safe_float(data.get("correct"), 0.0)
        accuracy = round((correct / total) * 100, 2) if total > 0 else 0.0
        has_sufficient_evidence = int(total) >= int(evidence_min_questions or DOMAIN_EVIDENCE_MIN_QUESTIONS)
        rows.append(
            {
                "Domain": name,
                "display_order": display_order.get(name, 10_000 + len(rows)),
                "exam_weight": round(_safe_float(weights.get(name), 0.0), 2),
                "attempts_counted": int(total),
                "Correct": int(correct),
                "Total": int(total),
                "Accuracy %": accuracy,
                "status": _domain_status_label(accuracy, passing_threshold),
                "has_sufficient_evidence": has_sufficient_evidence,
                "ranking_score": accuracy,
            }
        )

    rows.sort(key=lambda row: (row["ranking_score"], row["Domain"]))
    return rows


def build_verified_domain_table_rows(
    attempts: Sequence[Dict[str, Any]],
    question_attempts: Sequence[Dict[str, Any]],
    expected_question_count: int = 60,
    *,
    domain_weights: Optional[Mapping[str, float]] = None,
    passing_threshold: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Backward-compatible verified domain table rows."""
    rows = build_verified_domain_performance(
        attempts,
        question_attempts,
        expected_question_count,
        domain_weights=domain_weights,
        passing_threshold=passing_threshold,
    )
    return [
        {
            "Domain": row["Domain"],
            "Correct": row["Correct"],
            "Total": row["Total"],
            "Accuracy %": row["Accuracy %"],
        }
        for row in rows
    ]


def select_weakest_verified_domain(
    domain_rows: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not domain_rows:
        return None
    return dict(domain_rows[0])


def rank_weak_domains(
    domain_rows: Sequence[Dict[str, Any]],
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return weakest-to-strongest domain rows."""
    ordered = sorted(
        [dict(row) for row in domain_rows],
        key=lambda row: (_safe_float(row.get("ranking_score", row.get("Accuracy %")), 0.0), str(row.get("Domain"))),
    )
    if limit is not None:
        return ordered[: max(int(limit), 0)]
    return ordered


# ---------------------------------------------------------------------------
# All-activity score summary (explicitly separate from verified mocks)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllActivityScoreSummary:
    attempt_count: int
    latest_score: Optional[float]
    average_score: Optional[float]
    best_score: Optional[float]
    has_attempts: bool


def build_all_activity_score_summary(
    attempts: Sequence[Dict[str, Any]],
) -> AllActivityScoreSummary:
    """Summarize scores across every saved attempt regardless of mode."""
    ordered = sort_attempts_newest_first(attempts)
    if not ordered:
        return AllActivityScoreSummary(
            attempt_count=0,
            latest_score=None,
            average_score=None,
            best_score=None,
            has_attempts=False,
        )
    scores = [_safe_float(attempt.get("score"), 0.0) for attempt in ordered]
    return AllActivityScoreSummary(
        attempt_count=len(ordered),
        latest_score=scores[0],
        average_score=round(sum(scores) / len(scores), 2),
        best_score=round(max(scores), 2),
        has_attempts=True,
    )


# ---------------------------------------------------------------------------
# Study activity summary
# ---------------------------------------------------------------------------


_MODE_COUNT_KEYS: Dict[str, str] = {
    PAID_MOCK_EXAM: "completed_verified_mocks",
    FREE_MOCK_EXAM: "completed_free_mocks",
    PRACTICE_BY_CATEGORY: "completed_practice_sessions",
    WEAK_AREAS_PRACTICE: "completed_weak_area_sessions",
    DAILY_SPRINT: "completed_daily_sprints",
}


@dataclass(frozen=True)
class StudyActivitySummary:
    window_days: int
    active_study_days: int
    total_completed_activities: int
    completed_verified_mocks: int
    completed_practice_sessions: int
    completed_weak_area_sessions: int
    completed_daily_sprints: int
    completed_free_mocks: int
    current_streak_days: int
    daily_counts: Tuple[Tuple[str, int], ...]


def _attempt_activity_date(attempt: Mapping[str, Any]) -> Optional[date]:
    completed = parse_attempt_datetime(attempt.get("completed_at") or attempt.get("started_at"))
    if completed == datetime.min.replace(tzinfo=timezone.utc):
        return None
    return completed.date()


def build_study_activity_summary(
    attempts: Sequence[Dict[str, Any]],
    *,
    window_days: int = DEFAULT_ACTIVITY_WINDOW_DAYS,
    reference_dt: Optional[datetime] = None,
) -> StudyActivitySummary:
    """Lightweight activity summary for a rolling day window."""
    reference = (reference_dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_start = (reference - timedelta(days=max(int(window_days or 1), 1) - 1)).date()
    reference_date = reference.date()

    counts_by_mode = {key: 0 for key in _MODE_COUNT_KEYS.values()}
    daily_counts: Dict[str, int] = {}
    active_days: set[date] = set()

    for attempt in attempts or []:
        activity_date = _attempt_activity_date(attempt)
        if activity_date is None or activity_date < window_start or activity_date > reference_date:
            continue

        mode = str(attempt.get("mode") or "").strip()
        count_key = _MODE_COUNT_KEYS.get(mode)
        if count_key:
            counts_by_mode[count_key] += 1

        active_days.add(activity_date)
        day_key = activity_date.isoformat()
        daily_counts[day_key] = daily_counts.get(day_key, 0) + 1

    streak = 0
    cursor = reference_date
    while cursor >= window_start:
        if cursor in active_days:
            streak += 1
            cursor -= timedelta(days=1)
        else:
            break

    ordered_daily_counts = tuple(
        sorted(daily_counts.items(), key=lambda item: item[0])
    )
    total_completed = sum(counts_by_mode.values())

    return StudyActivitySummary(
        window_days=int(window_days),
        active_study_days=len(active_days),
        total_completed_activities=total_completed,
        completed_verified_mocks=counts_by_mode["completed_verified_mocks"],
        completed_practice_sessions=counts_by_mode["completed_practice_sessions"],
        completed_weak_area_sessions=counts_by_mode["completed_weak_area_sessions"],
        completed_daily_sprints=counts_by_mode["completed_daily_sprints"],
        completed_free_mocks=counts_by_mode["completed_free_mocks"],
        current_streak_days=streak,
        daily_counts=ordered_daily_counts,
    )


# ---------------------------------------------------------------------------
# Activity history normalization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityHistoryRow:
    activity_type: str
    canonical_mode: str
    certification: str
    completed_at: datetime
    score: Optional[float]
    question_count: Optional[int]
    display_label: str
    readiness_eligible: bool
    attempt_id: Optional[int]


def _activity_type_for_mode(mode: str) -> str:
    mapping = {
        PAID_MOCK_EXAM: "paid_mock_exam",
        FREE_MOCK_EXAM: "free_mock_exam",
        PRACTICE_BY_CATEGORY: "practice_by_category",
        WEAK_AREAS_PRACTICE: "weak_areas_practice",
        DAILY_SPRINT: "daily_sprint",
    }
    return mapping.get(mode, "unknown_activity")


def normalize_activity_history_row(
    attempt: Mapping[str, Any],
    *,
    expected_question_count: int = 60,
) -> ActivityHistoryRow:
    _, _, _, v5_parse_attempt_id = _readiness_helpers()
    mode = str(attempt.get("mode") or "").strip()
    canonical_mode = mode if mode in ALL_ACTIVITY_MODES else mode
    completed = parse_attempt_datetime(attempt.get("completed_at") or attempt.get("started_at"))
    score_raw = attempt.get("score")
    score = _safe_float(score_raw) if score_raw is not None else None
    question_count_raw = attempt.get("total_questions")
    question_count = _safe_int(question_count_raw) if question_count_raw is not None else None

    return ActivityHistoryRow(
        activity_type=_activity_type_for_mode(mode),
        canonical_mode=canonical_mode,
        certification=str(attempt.get("exam_name") or "").strip(),
        completed_at=completed,
        score=score,
        question_count=question_count,
        display_label=mode or "Unknown Activity",
        readiness_eligible=is_readiness_eligible_attempt(attempt, expected_question_count),
        attempt_id=v5_parse_attempt_id(dict(attempt)),
    )


def normalize_activity_history(
    attempts: Sequence[Dict[str, Any]],
    *,
    expected_question_count: int = 60,
) -> Tuple[ActivityHistoryRow, ...]:
    ordered = sort_attempts_newest_first(attempts)
    return tuple(
        normalize_activity_history_row(attempt, expected_question_count=expected_question_count)
        for attempt in ordered
    )


def build_activity_history_display_rows(
    attempts: Sequence[Dict[str, Any]],
    *,
    format_datetime,
    expected_question_count: int = 60,
    get_correct_count=None,
) -> List[Dict[str, Any]]:
    """Build display rows for attempt-history tables."""
    rows: List[Dict[str, Any]] = []
    for attempt in sort_attempts_newest_first(attempts):
        normalized = normalize_activity_history_row(
            attempt,
            expected_question_count=expected_question_count,
        )
        correct = None
        if get_correct_count is not None:
            correct = get_correct_count(attempt)
        rows.append(
            {
                "Attempt ID": attempt.get("id"),
                "Completed At": format_datetime(attempt.get("completed_at")),
                "Started At": format_datetime(attempt.get("started_at")),
                "Mode": normalized.canonical_mode,
                "Category": attempt.get("category"),
                "Score %": normalized.score,
                "Correct": correct,
                "Total": normalized.question_count,
                "Language": attempt.get("language_code"),
                "readiness_eligible": normalized.readiness_eligible,
                "activity_type": normalized.activity_type,
            }
        )
    return rows
