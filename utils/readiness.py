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

from utils.activity_modes import PAID_MOCK_EXAM, is_readiness_eligible_mode

READINESS_VERSION = "READINESS_V5_VERIFIED_EVIDENCE"

EMA_ALPHA = 0.40
REQUIRED_FULL_MOCKS = 3
MAX_RECENT_MOCKS = 5
QUESTION_TIME_CAP_SECONDS = 300.0
QUESTION_TIME_MIN_SECONDS = 1.0  # below this = instrumentation noise, ignored

# ---------------------------------------------------------------------------
# V5 constants  (helpers below are additive; calculate_readiness still runs V4)
# ---------------------------------------------------------------------------

# Attempt grade strings — use these constants everywhere so callers never
# compare against bare string literals.
GRADE_VERIFIED = "verified"
GRADE_LEGACY   = "legacy"
GRADE_INVALID  = "invalid"

# Window sizes
V5_MAX_SCORING_MOCKS = 5        # EMA / consistency / trend window
V5_MAX_REPEAT_HISTORY_MOCKS = 10  # repeat-discount look-back

# Metadata-availability gate: fraction of rows that must carry a recognized
# value before the component is considered available at all.
V5_METADATA_THRESHOLD = 0.90

# Question-level repeat-discount weights (by occurrence rank across history).
V5_QUESTION_DISCOUNT = {1: 1.00, 2: 0.25}   # rank >= 3 → 0.00
V5_QUESTION_DISCOUNT_DEFAULT = 0.00

# Family-level repeat-discount weights (by distinct-mock rank for the family).
# Activated only when family_data_available == True.
V5_FAMILY_DISCOUNT = {1: 1.00, 2: 0.70}     # rank >= 3 → 0.50
V5_FAMILY_DISCOUNT_FLOOR = 0.50

# ---------------------------------------------------------------------------
# V5 Batch 2 constants — scoring-analysis helpers
# ---------------------------------------------------------------------------

# DR formula weights (DR = V5_DR_DOMAIN_WEIGHT * D + V5_DR_FLOOR_WEIGHT * F)
V5_DR_DOMAIN_WEIGHT = 0.70
V5_DR_FLOOR_WEIGHT  = 0.30

# Domain evidence-state strings
V5_DOMAIN_UNCOVERED            = "uncovered"
V5_DOMAIN_UNDER_SAMPLED        = "under_sampled"
V5_DOMAIN_SUFFICIENTLY_SAMPLED = "sufficiently_sampled"
V5_DOMAIN_RELIABLY_SAMPLED     = "reliably_sampled"

# Domain gap/floor thresholds
V5_DOMAIN_GAP_WEIGHT_THRESHOLD  = 0.10   # uncovered domain triggers gap when weight >= this
V5_DOMAIN_FLOOR_SCORE_THRESHOLD = 40.0   # floor triggers when weakest reliable domain < this
V5_DOMAIN_MIN_QUESTIONS         = 5      # floor for expected_domain_questions

# Difficulty tiers and minimum effective-total thresholds
V5_DIFFICULTY_LEVELS         = {"easy", "medium", "hard"}
V5_DIFFICULTY_EASY_MIN       = 5
V5_DIFFICULTY_MEDIUM_MIN     = 10
V5_DIFFICULTY_HARD_MIN       = 10
V5_DIFFICULTY_EASY_CONF_WT   = 0.25   # weight in confidence_fraction
V5_DIFFICULTY_MEDIUM_CONF_WT = 0.35
V5_DIFFICULTY_HARD_CONF_WT   = 0.40

# Cognitive levels and higher-order target
V5_COGNITIVE_LEVELS        = {"recall", "understanding", "application", "analysis", "judgment"}
V5_COGNITIVE_HIGHER_ORDER  = {"application", "analysis", "judgment"}
V5_COGNITIVE_HO_MULTIPLIER = 0.30   # fraction of expected_question_count
V5_COGNITIVE_HO_MIN        = 10     # floor for higher_order_target

# Trend computation
V5_TREND_COEFFICIENT         = 0.25
V5_TREND_CLAMP_MIN           = -4.0
V5_TREND_CLAMP_MAX           = 2.0
V5_TREND_IMPROVING_THRESHOLD = 2.0
V5_TREND_DECLINING_THRESHOLD = -2.0

# Staleness state names
V5_STALENESS_CURRENT = "current"
V5_STALENESS_AGING   = "aging"
V5_STALENESS_OLD     = "old"
V5_STALENESS_STALE   = "stale"
V5_STALENESS_UNKNOWN = "unknown"

# Staleness age-boundary inclusive upper bounds (days)
V5_STALENESS_CURRENT_MAX_DAYS = 90
V5_STALENESS_AGING_MAX_DAYS   = 180
V5_STALENESS_OLD_MAX_DAYS     = 365

# Score-cap offsets from passing_score (or absolute value for stale)
V5_CAP_AGING_OFFSET        = 7      # passing + 7
V5_CAP_OLD_OFFSET          = -3     # passing - 3
V5_CAP_STALE_VALUE         = 0.0    # hard cap at 0
V5_CAP_DOMAIN_GAP_OFFSET   = -3     # passing - 3
V5_CAP_DOMAIN_FLOOR_OFFSET = -5     # passing - 5  (floored at 50)
V5_CAP_DOMAIN_FLOOR_MIN    = 50.0
V5_CAP_DIFFICULTY_OFFSET   = -1     # passing - 1
V5_CAP_COGNITIVE_OFFSET    = 7      # passing + 7

# Confidence component points (must sum to 100)
V5_CONF_MOCK_VOLUME_PTS = 30
V5_CONF_BREADTH_PTS     = 25
V5_CONF_RECENCY_PTS     = 20
V5_CONF_DOMAIN_PTS      = 15
V5_CONF_DIFFICULTY_PTS  = 5
V5_CONF_COGNITIVE_PTS   = 5

# Coverage-target multiplier (bank * this for breadth denominator)
V5_CONF_COVERAGE_MOCK_COUNT = 10   # min(bank_size, expected_q * 10)

# Confidence label thresholds
V5_CONF_HIGH_THRESHOLD   = 70
V5_CONF_MEDIUM_THRESHOLD = 40


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
# V5 pure helpers — datetime, grading, weighting
# (calculate_readiness still uses the V4 path; these are additive)
# ---------------------------------------------------------------------------

def v5_parse_attempt_datetime(attempt: Dict[str, Any]) -> Optional[datetime]:
    """Return a timezone-aware UTC datetime for the attempt's readiness timestamp.

    Priority: completed_at > started_at.
    Supports ISO 8601 with Z suffix or explicit UTC offsets (+00:00, -05:00, …).
    Returns None when both fields are absent, empty, or unparseable.
    Never falls back to the attempt ID.
    """
    for field in ("completed_at", "started_at"):
        raw = attempt.get(field)
        if not raw:
            continue
        ts = str(raw).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _v5_parse_strict_int(value: Any) -> Optional[int]:
    """Strict integer parser used everywhere a numeric ID is required.

    Accepts:
    - int (but NOT bool, which is a subclass of int in Python)
    - digit-only strings after stripping whitespace (optionally with a leading minus)

    Rejects:
    - bool (True / False)
    - float values such as 10.0
    - float strings such as "10.0", "10.9"
    - scientific-notation strings such as "1e2"
    - UUIDs and any other non-digit text
    - None

    Returns None for every rejected value; never raises.
    """
    if value is None:
        return None
    if isinstance(value, bool):          # bool subclasses int; must be checked first
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):         # raw float (e.g. 10.0) is rejected
        return None
    s = str(value).strip()
    # Allow an optional leading minus then only decimal digits — no dots, no 'e'
    check = s.lstrip("-")
    if not check or not check.isdigit():
        return None
    try:
        return int(s)
    except (ValueError, OverflowError):
        return None


def v5_parse_attempt_id(attempt: Dict[str, Any]) -> Optional[int]:
    """Return the attempt's numeric ID as an int, or None when absent/non-numeric.

    Delegates to _v5_parse_strict_int so the same rules apply throughout.
    """
    return _v5_parse_strict_int(attempt.get("id"))


def v5_attempt_sort_key(attempt: Dict[str, Any]) -> Tuple[datetime, int]:
    """Return (parsed_datetime, numeric_id) for deterministic chronological sort.

    Raises ValueError when the attempt lacks either a valid datetime or a
    numeric ID, so callers know it cannot be used as readiness evidence.
    """
    dt = v5_parse_attempt_datetime(attempt)
    if dt is None:
        raise ValueError(
            f"Attempt {attempt.get('id')!r} has no parseable readiness timestamp "
            f"(completed_at={attempt.get('completed_at')!r}, "
            f"started_at={attempt.get('started_at')!r})"
        )
    numeric_id = v5_parse_attempt_id(attempt)
    if numeric_id is None:
        raise ValueError(
            f"Attempt has no numeric ID (id={attempt.get('id')!r})"
        )
    return (dt, numeric_id)


def v5_is_historical_attempt(
    attempt: Dict[str, Any],
    target_dt: datetime,
    target_id: int,
) -> bool:
    """Return True iff the attempt is on or before the target (completed_at, id) cursor.

    Rules (frozen requirements §2):
    - attempt_datetime < target_datetime          → True
    - attempt_datetime == target_datetime
      AND numeric attempt_id <= target_id         → True
    - attempt_datetime > target_datetime          → False
    - unparseable timestamp or non-numeric ID     → False  (never silently included)

    target_dt must be timezone-aware UTC.
    """
    attempt_dt = v5_parse_attempt_datetime(attempt)
    if attempt_dt is None:
        return False
    attempt_num_id = v5_parse_attempt_id(attempt)
    if attempt_num_id is None:
        return False

    if attempt_dt < target_dt:
        return True
    if attempt_dt == target_dt:
        return attempt_num_id <= target_id
    return False


# ---------------------------------------------------------------------------
# V5 attempt grading
# ---------------------------------------------------------------------------

def _v5_normalize_correct_count(attempt: Dict[str, Any]) -> Optional[int]:
    """Return the canonical correct-answers count from parent row, or None.

    Prefers correct_count; falls back to correct_answers when it holds a
    numeric value.  Returns None when neither is available.
    """
    for field in ("correct_count", "correct_answers"):
        raw = attempt.get(field)
        if raw is None:
            continue
        try:
            v = int(float(raw))
            return v
        except (TypeError, ValueError):
            continue
    return None


def v5_grade_attempt(
    attempt: Dict[str, Any],
    child_rows: List[Dict[str, Any]],
    expected_question_count: int,
) -> str:
    """Return GRADE_VERIFIED, GRADE_LEGACY, or GRADE_INVALID for one attempt.

    INVALID conditions (any one → INVALID):
    - mode != "Paid Mock Exam" (case-insensitive)
    - total_questions < expected_question_count
    - score is missing, < 0, or > 100
    - no parseable readiness timestamp (completed_at then started_at)
    - no numeric attempt ID

    VERIFIED requires (all must hold):
    - parent passes INVALID checks (i.e., eligible)
    - len(child_rows) == total_questions exactly
    - distinct non-null question_ids == total_questions exactly
    - when correct_count (or correct_answers fallback) is present,
      sum(is_correct) == correct_count exactly

    LEGACY: eligible parent but any verification check fails.
    """
    # ── INVALID gate ──────────────────────────────────────────────────────────
    mode = str(attempt.get("mode") or "").strip().lower()
    if not is_readiness_eligible_mode(mode):
        return GRADE_INVALID

    total_q = _safe_int(attempt.get("total_questions"), 0)
    if total_q < int(expected_question_count or 60):
        return GRADE_INVALID

    score_raw = attempt.get("score")
    if score_raw is None:
        return GRADE_INVALID
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        return GRADE_INVALID
    if score < 0.0 or score > 100.0:
        return GRADE_INVALID

    if v5_parse_attempt_datetime(attempt) is None:
        return GRADE_INVALID

    if v5_parse_attempt_id(attempt) is None:
        return GRADE_INVALID

    # ── VERIFIED checks ───────────────────────────────────────────────────────
    if len(child_rows) != total_q:
        return GRADE_LEGACY

    distinct_qids = {
        row.get("question_id")
        for row in child_rows
        if row.get("question_id") is not None
    }
    if len(distinct_qids) != total_q:
        return GRADE_LEGACY

    expected_correct = _v5_normalize_correct_count(attempt)
    if expected_correct is not None:
        actual_correct = sum(1 for r in child_rows if bool(r.get("is_correct")))
        if actual_correct != expected_correct:
            return GRADE_LEGACY

    return GRADE_VERIFIED


def v5_grade_all_attempts(
    attempts: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]],
    expected_question_count: int,
) -> Dict[str, Any]:
    """Grade every attempt in the list; return structured result without mutating inputs.

    Groups child rows by numeric exam_attempt_id, then calls v5_grade_attempt
    for each attempt.

    Returns a dict with:
        verified:     list of attempt dicts (copies, grade key added)
        legacy:       list of attempt dicts (copies, grade key added)
        invalid:      list of attempt dicts (copies, grade key added)
        verified_ids: list of numeric attempt IDs (int)
        legacy_ids:   list of numeric attempt IDs (int)
    """
    # Index child rows by strictly-parsed exam_attempt_id.
    # int(float(...)) is intentionally NOT used here: "1.0" must not map to 1.
    children_by_id: Dict[int, List[Dict[str, Any]]] = {}
    for row in (question_attempts or []):
        eid = _v5_parse_strict_int(row.get("exam_attempt_id"))
        if eid is None:
            continue  # invalid ID — do not attach to any parent
        children_by_id.setdefault(eid, []).append(row)

    verified: List[Dict[str, Any]] = []
    legacy:   List[Dict[str, Any]] = []
    invalid:  List[Dict[str, Any]] = []

    for attempt in (attempts or []):
        numeric_id = v5_parse_attempt_id(attempt)
        child_rows = children_by_id.get(numeric_id, []) if numeric_id is not None else []
        grade = v5_grade_attempt(attempt, child_rows, expected_question_count)
        copy = dict(attempt, grade=grade)
        if grade == GRADE_VERIFIED:
            verified.append(copy)
        elif grade == GRADE_LEGACY:
            legacy.append(copy)
        else:
            invalid.append(copy)

    return {
        "verified":     verified,
        "legacy":       legacy,
        "invalid":      invalid,
        "verified_ids": [v5_parse_attempt_id(a) for a in verified],
        "legacy_ids":   [v5_parse_attempt_id(a) for a in legacy],
    }


def count_verified_unique_questions_seen(
    attempts: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]],
    expected_question_count: int = 60,
) -> int:
    """Count distinct question IDs from child rows linked to verified paid mocks."""
    graded = v5_grade_all_attempts(attempts, question_attempts, expected_question_count)
    verified_ids = {attempt_id for attempt_id in (graded.get("verified_ids") or []) if attempt_id is not None}
    if not verified_ids:
        return 0

    seen = set()
    for row in question_attempts or []:
        exam_attempt_id = _v5_parse_strict_int(row.get("exam_attempt_id"))
        question_id = row.get("question_id")
        if exam_attempt_id in verified_ids and question_id is not None:
            seen.add(question_id)
    return len(seen)


def filter_verified_mock_attempts(
    attempts: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]],
    expected_question_count: int = 60,
) -> List[Dict[str, Any]]:
    """Return verified paid mocks only, preserving caller attempt order."""
    graded = v5_grade_all_attempts(attempts, question_attempts, expected_question_count)
    verified_ids = {attempt_id for attempt_id in (graded.get("verified_ids") or []) if attempt_id is not None}
    if not verified_ids:
        return []
    return [
        attempt for attempt in (attempts or [])
        if v5_parse_attempt_id(attempt) in verified_ids
    ]


def filter_verified_question_attempts(
    attempts: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]],
    expected_question_count: int = 60,
) -> List[Dict[str, Any]]:
    """Return child question rows linked to VERIFIED paid mock attempts only."""
    graded = v5_grade_all_attempts(attempts, question_attempts, expected_question_count)
    verified_ids = {attempt_id for attempt_id in (graded.get("verified_ids") or []) if attempt_id is not None}
    if not verified_ids:
        return []
    filtered: List[Dict[str, Any]] = []
    for row in question_attempts or []:
        exam_attempt_id = _v5_parse_strict_int(row.get("exam_attempt_id"))
        if exam_attempt_id in verified_ids:
            filtered.append(row)
    return filtered


def build_verified_domain_table_rows(
    attempts: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]],
    expected_question_count: int = 60,
) -> List[Dict[str, Any]]:
    """Build Weak Areas by Domain rows from verified question attempts only."""
    verified_qa = filter_verified_question_attempts(
        attempts,
        question_attempts,
        expected_question_count,
    )
    if not verified_qa:
        return []

    stats = _build_domain_stats(verified_qa, [])
    rows: List[Dict[str, Any]] = []
    for name, data in stats.items():
        total = _safe_float(data.get("total"), 0.0)
        correct = _safe_float(data.get("correct"), 0.0)
        rows.append(
            {
                "Domain": name,
                "Correct": int(correct),
                "Total": int(total),
                "Accuracy %": round((correct / total) * 100, 2) if total > 0 else 0.0,
            }
        )
    rows.sort(key=lambda row: row["Accuracy %"])
    return rows


def select_weakest_verified_domain(
    domain_rows: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return the lowest-accuracy domain row from build_verified_domain_table_rows output."""
    if not domain_rows:
        return None
    return domain_rows[0]


def build_verified_mock_performance_metrics(
    attempts: List[Dict[str, Any]],
    question_attempts: List[Dict[str, Any]],
    expected_question_count: int = 60,
) -> Dict[str, Any]:
    """Summarize score metrics from VERIFIED paid mock attempts only."""
    verified_attempts = filter_verified_mock_attempts(
        attempts,
        question_attempts,
        expected_question_count,
    )
    if not verified_attempts:
        return {
            "has_verified_mocks": False,
            "latest_score": None,
            "average_score": None,
            "best_score": None,
            "verified_mock_count": 0,
            "trend_attempts": [],
        }

    scores = [_safe_float(attempt.get("score"), 0.0) for attempt in verified_attempts]
    return {
        "has_verified_mocks": True,
        "latest_score": scores[0],
        "average_score": round(sum(scores) / len(scores), 2),
        "best_score": round(max(scores), 2),
        "verified_mock_count": len(verified_attempts),
        "trend_attempts": list(reversed(verified_attempts)),
    }


# ---------------------------------------------------------------------------
# V5 repeat-evidence weights
# ---------------------------------------------------------------------------

def _v5_parse_row_sort_key(
    row: Dict[str, Any],
    attempt_dt_map: Dict[int, datetime],
) -> Tuple[datetime, int, int]:
    """Return (row_datetime, numeric_exam_attempt_id, numeric_row_id) for ordering.

    Row ordering priority (frozen requirements §7):
    1. parsed answered_at when valid
    2. parent attempt datetime (from attempt_dt_map)
    3. numeric exam_attempt_id
    4. numeric row ID
    """
    # answered_at
    answered_raw = row.get("answered_at")
    row_dt: Optional[datetime] = None
    if answered_raw:
        ts = str(answered_raw).strip().replace("Z", "+00:00")
        try:
            row_dt = datetime.fromisoformat(ts)
            if row_dt.tzinfo is None:
                row_dt = row_dt.replace(tzinfo=timezone.utc)
            row_dt = row_dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            row_dt = None

    # Strict exam_attempt_id for attempt_dt_map lookup and sort position.
    # Fallback 0 is used only as a sort sentinel — not for grouping.
    eid_parsed = _v5_parse_strict_int(row.get("exam_attempt_id"))
    eid = eid_parsed if eid_parsed is not None else 0
    if row_dt is None:
        row_dt = attempt_dt_map.get(eid, datetime.min.replace(tzinfo=timezone.utc))

    # Strict row ID, fallback 0 as sort sentinel.
    rid_parsed = _v5_parse_strict_int(row.get("id"))
    rid = rid_parsed if rid_parsed is not None else 0

    return (row_dt, eid, rid)


def v5_assign_evidence_weights(
    target_rows: List[Dict[str, Any]],
    history_rows: List[Dict[str, Any]],
    attempt_dt_map: Dict[int, datetime],
) -> Dict[str, Any]:
    """Compute per-row evidence weights for the target scoring rows.

    Parameters
    ----------
    target_rows:
        Question-attempt rows for the latest V5_MAX_SCORING_MOCKS (5) verified
        mocks.  Weights are returned only for these rows.
    history_rows:
        Question-attempt rows for the latest V5_MAX_REPEAT_HISTORY_MOCKS (10)
        verified mocks.  Used to establish prior-exposure counts.
    attempt_dt_map:
        {numeric_attempt_id → UTC datetime} for all relevant verified attempts,
        used as the answered_at fallback in sort ordering.

    Returns
    -------
    dict with:
        weights:                    {row_id_str → float}  — target rows only
        family_data_available:      bool
        cross_mock_repeat_fraction: float
        effective_target_sample:    float  (sum of weights for target rows)

    Discount rules
    ──────────────
    Question-level (by chronological occurrence rank across history):
        1st  →  1.00
        2nd  →  0.25
        3rd+ →  0.00

    Family-level (active only when ≥90 % of history rows carry a non-null
    question_family_id; by distinct mock rank for the family):
        1st mock containing family  →  1.00
        2nd mock                    →  0.70
        3rd+ mock                   →  0.50

    Final weight = question_discount × family_discount.

    A target row not found in the bounded history receives weight 0.0
    (no fabricated exposure rank).
    """
    _EPOCH = datetime.min.replace(tzinfo=timezone.utc)

    # ── Step 1: Enforce V5_MAX_REPEAT_HISTORY_MOCKS boundary ─────────────────
    # Collect all distinct valid mock IDs present in history_rows.
    all_history_mock_ids: set = set()
    for row in history_rows:
        eid = _v5_parse_strict_int(row.get("exam_attempt_id"))
        if eid is not None:
            all_history_mock_ids.add(eid)

    # Sort distinct mocks oldest-to-newest:
    #   primary key  = UTC datetime from attempt_dt_map (EPOCH when absent)
    #   secondary key = numeric ID so that ID 9 sorts before ID 10 when tied
    sorted_mock_ids = sorted(
        all_history_mock_ids,
        key=lambda m: (attempt_dt_map.get(m, _EPOCH), m),
    )

    # Retain only the latest V5_MAX_REPEAT_HISTORY_MOCKS distinct mocks.
    bounded_mock_ids: set = set(sorted_mock_ids[-V5_MAX_REPEAT_HISTORY_MOCKS:])

    # Filter history rows to the bounded window; rows with invalid IDs are dropped.
    bounded_history = [
        row for row in history_rows
        if _v5_parse_strict_int(row.get("exam_attempt_id")) in bounded_mock_ids
    ]

    # ── Step 2: Family-data availability (over bounded history only) ──────────
    total_history = len(bounded_history)
    family_populated = sum(
        1 for r in bounded_history if r.get("question_family_id") is not None
    )
    family_data_available = (
        total_history > 0
        and (family_populated / total_history) >= V5_METADATA_THRESHOLD
    )

    # ── Step 3: Sort bounded history chronologically ───────────────────────────
    sorted_history = sorted(
        bounded_history,
        key=lambda r: _v5_parse_row_sort_key(r, attempt_dt_map),
    )

    # ── Step 4: Per-row question rank (rank at the time each row appears) ──────
    qid_seen_count: Dict[Any, int] = {}
    row_question_rank: Dict[str, int] = {}  # str(row["id"]) → rank within bounded window
    for row in sorted_history:
        qid = row.get("question_id")
        row_id = str(row.get("id", id(row)))
        if qid is None:
            continue
        qid_seen_count[qid] = qid_seen_count.get(qid, 0) + 1
        row_question_rank[row_id] = qid_seen_count[qid]

    # ── Step 5: Family-mock rank (chronological order of distinct mocks per family) ──
    family_mock_rank: Dict[Tuple[Any, int], int] = {}
    if family_data_available:
        family_seen_mocks: Dict[Any, List[int]] = {}
        for row in sorted_history:
            fid = row.get("question_family_id")
            if fid is None:
                continue
            eid = _v5_parse_strict_int(row.get("exam_attempt_id"))
            if eid is None:
                continue
            seen = family_seen_mocks.setdefault(fid, [])
            if eid not in seen:
                seen.append(eid)
            family_mock_rank[(fid, eid)] = seen.index(eid) + 1

    # ── Step 6: Cross-mock repeat fraction (from bounded history) ─────────────
    qid_mock_sets: Dict[Any, set] = {}
    for row in bounded_history:
        qid = row.get("question_id")
        if qid is None:
            continue
        eid = _v5_parse_strict_int(row.get("exam_attempt_id"))
        if eid is None:
            continue
        qid_mock_sets.setdefault(qid, set()).add(eid)
    all_unique_qids = len(qid_mock_sets)
    cross_mock_repeated = sum(1 for s in qid_mock_sets.values() if len(s) > 1)
    cross_mock_repeat_fraction = (
        cross_mock_repeated / all_unique_qids if all_unique_qids > 0 else 0.0
    )

    # ── Step 7: Assign weights for target rows ────────────────────────────────
    weights: Dict[str, float] = {}
    for row in target_rows:
        row_id = str(row.get("id", id(row)))

        if row_id not in row_question_rank:
            # Target row is outside the bounded history window.
            # Assign weight 0.0 — no fabricated exposure rank.
            weights[row_id] = 0.0
            continue

        q_rank = row_question_rank[row_id]
        q_discount = V5_QUESTION_DISCOUNT.get(q_rank, V5_QUESTION_DISCOUNT_DEFAULT)

        f_discount = 1.0
        if family_data_available:
            fid = row.get("question_family_id")
            if fid is not None:
                eid = _v5_parse_strict_int(row.get("exam_attempt_id"))
                if eid is None:
                    eid = -1  # sentinel — will not match any family_mock_rank entry
                f_rank = family_mock_rank.get((fid, eid), 1)
                f_discount = V5_FAMILY_DISCOUNT.get(f_rank, V5_FAMILY_DISCOUNT_FLOOR)

        weights[row_id] = q_discount * f_discount

    effective_target_sample = sum(weights.values())

    return {
        "weights":                    weights,
        "family_data_available":      family_data_available,
        "cross_mock_repeat_fraction": cross_mock_repeat_fraction,
        "effective_target_sample":    effective_target_sample,
    }


# ---------------------------------------------------------------------------
# V5 Batch 2 — scoring-analysis helpers
# ---------------------------------------------------------------------------

def _v5_normalize_domain_weights(
    domain_weights: Optional[Dict[str, Any]]
) -> Dict[str, float]:
    """Normalize domain weights to sum to 1.0.

    Accepts percentages (e.g. 25.0) or decimals (e.g. 0.25).
    Ignores invalid, null, negative, and zero values.
    Preserves official domain names exactly as supplied.
    Returns an empty dict when no valid weights exist.
    """
    if not domain_weights or not isinstance(domain_weights, dict):
        return {}
    raw: Dict[str, float] = {}
    for name, value in domain_weights.items():
        if value is None:
            continue
        try:
            w = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(w) or w <= 0:
            continue
        raw[str(name)] = w
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


def _v5_build_domain_stats(
    target_rows: List[Dict[str, Any]],
    row_weights: Dict[str, float],
    expected_question_count: int,
    normalized_weights: Dict[str, float],
) -> Dict[str, dict]:
    """Build weighted domain statistics for each official domain.

    Rows whose category/domain does not match any official domain are ignored
    for scoring purposes (unmapped).  They do not distort official stats.

    Returns dict mapping official domain name → stats dict with keys:
        effective_correct, effective_total, percent,
        expected_domain_questions, evidence_state.
    """
    norm_to_official: Dict[str, str] = {
        str(d).strip().lower(): d for d in normalized_weights
    }

    eff_correct: Dict[str, float] = {d: 0.0 for d in normalized_weights}
    eff_total:   Dict[str, float] = {d: 0.0 for d in normalized_weights}

    for row in target_rows:
        row_id = str(row.get("id", id(row)))
        weight = row_weights.get(row_id, 0.0)
        if weight <= 0:
            continue
        cat = str(row.get("domain") or row.get("category") or "").strip().lower()
        official = norm_to_official.get(cat)
        if official is None:
            continue
        eff_total[official] += weight
        if bool(row.get("is_correct")):
            eff_correct[official] += weight

    result: Dict[str, dict] = {}
    for domain, nw in normalized_weights.items():
        et = eff_total[domain]
        ec = eff_correct[domain]
        expected_dq = max(V5_DOMAIN_MIN_QUESTIONS, round(expected_question_count * nw))
        percent = 100.0 * ec / et if et > 0 else 0.0

        if et == 0:
            state = V5_DOMAIN_UNCOVERED
        elif et < expected_dq:
            state = V5_DOMAIN_UNDER_SAMPLED
        elif et < 2 * expected_dq:
            state = V5_DOMAIN_SUFFICIENTLY_SAMPLED
        else:
            state = V5_DOMAIN_RELIABLY_SAMPLED

        result[domain] = {
            "effective_correct":         ec,
            "effective_total":           et,
            "percent":                   percent,
            "expected_domain_questions": expected_dq,
            "evidence_state":            state,
        }
    return result


def _v5_compute_domain_score(
    domain_stats: Dict[str, dict],
    normalized_weights: Dict[str, float],
) -> Dict[str, Any]:
    """Compute V5 domain score DR = 0.70 * D + 0.30 * F.

    D = official-weighted accuracy, renormalized over domains with evidence.
    F = weakest sufficiently/reliably sampled domain score (or D when none qualifies).
    domain_gap triggers when an uncovered official domain has weight >= 10 %.
    domain_floor triggers when the weakest reliable domain score < 40 %.
    """
    have_official = bool(normalized_weights)

    domains_with_evidence = {
        d: s for d, s in domain_stats.items() if s["effective_total"] > 0
    }

    if not domains_with_evidence:
        D = 0.0
    elif have_official:
        wsum = sum(normalized_weights.get(d, 0.0) for d in domains_with_evidence)
        D = 0.0 if wsum <= 0 else sum(
            normalized_weights.get(d, 0.0) / wsum * s["percent"]
            for d, s in domains_with_evidence.items()
        )
    else:
        n = len(domains_with_evidence)
        D = sum(s["percent"] for s in domains_with_evidence.values()) / n

    uncovered = [
        d for d, s in domain_stats.items()
        if s["evidence_state"] == V5_DOMAIN_UNCOVERED
    ]

    domain_gap_triggered = have_official and any(
        normalized_weights.get(d, 0.0) >= V5_DOMAIN_GAP_WEIGHT_THRESHOLD
        for d in uncovered
    )

    qualified = {
        d: s for d, s in domain_stats.items()
        if s["evidence_state"] in (V5_DOMAIN_SUFFICIENTLY_SAMPLED, V5_DOMAIN_RELIABLY_SAMPLED)
    }

    if not qualified:
        F = D
        weakest_domain = None
        weakest_score = None
        domain_floor_triggered = False
    else:
        weakest_domain = min(qualified, key=lambda d: qualified[d]["percent"])
        weakest_score = qualified[weakest_domain]["percent"]
        F = weakest_score
        domain_floor_triggered = weakest_score < V5_DOMAIN_FLOOR_SCORE_THRESHOLD

    DR = V5_DR_DOMAIN_WEIGHT * D + V5_DR_FLOOR_WEIGHT * F

    return {
        "D":                      D,
        "F":                      F,
        "DR":                     DR,
        "weakest_domain":         weakest_domain,
        "weakest_score":          weakest_score,
        "uncovered_domains":      uncovered,
        "domain_gap_triggered":   domain_gap_triggered,
        "domain_floor_triggered": domain_floor_triggered,
    }


def _v5_compute_difficulty_analysis(
    target_rows: List[Dict[str, Any]],
    row_weights: Dict[str, float],
) -> Dict[str, Any]:
    """Analyse difficulty-tier evidence in target_rows.

    Data activates when >= 90 % of rows carry a recognized difficulty value
    (easy / medium / hard).  cap_active = data_available AND hard not sufficient.
    confidence_fraction is the weighted completeness of all three tiers.
    """
    _zero: Dict[str, Any] = {
        "metadata_coverage":      0.0,
        "data_available":         False,
        "easy_effective_total":   0.0,
        "medium_effective_total": 0.0,
        "hard_effective_total":   0.0,
        "easy_accuracy":          0.0,
        "medium_accuracy":        0.0,
        "hard_accuracy":          0.0,
        "easy_sufficient":        False,
        "medium_sufficient":      False,
        "hard_sufficient":        False,
        "cap_active":             False,
        "confidence_fraction":    0.0,
    }
    total = len(target_rows)
    if total == 0:
        return dict(_zero)

    recognized = sum(
        1 for r in target_rows
        if str(r.get("difficulty") or "").strip().lower() in V5_DIFFICULTY_LEVELS
    )
    coverage = recognized / total

    if coverage < V5_METADATA_THRESHOLD:
        out = dict(_zero)
        out["metadata_coverage"] = coverage
        return out

    tc: Dict[str, float] = {"easy": 0.0, "medium": 0.0, "hard": 0.0}
    tt: Dict[str, float] = {"easy": 0.0, "medium": 0.0, "hard": 0.0}
    for row in target_rows:
        row_id = str(row.get("id", id(row)))
        w = row_weights.get(row_id, 0.0)
        d = str(row.get("difficulty") or "").strip().lower()
        if d not in V5_DIFFICULTY_LEVELS:
            continue
        tt[d] += w
        if bool(row.get("is_correct")):
            tc[d] += w

    et = tt["easy"]; mt = tt["medium"]; ht = tt["hard"]
    ea = 100.0 * tc["easy"]   / et if et > 0 else 0.0
    ma = 100.0 * tc["medium"] / mt if mt > 0 else 0.0
    ha = 100.0 * tc["hard"]   / ht if ht > 0 else 0.0

    e_ok = et >= V5_DIFFICULTY_EASY_MIN
    m_ok = mt >= V5_DIFFICULTY_MEDIUM_MIN
    h_ok = ht >= V5_DIFFICULTY_HARD_MIN

    conf_frac = (
        V5_DIFFICULTY_EASY_CONF_WT   * min(et / V5_DIFFICULTY_EASY_MIN,   1.0) +
        V5_DIFFICULTY_MEDIUM_CONF_WT * min(mt / V5_DIFFICULTY_MEDIUM_MIN, 1.0) +
        V5_DIFFICULTY_HARD_CONF_WT   * min(ht / V5_DIFFICULTY_HARD_MIN,   1.0)
    )

    return {
        "metadata_coverage":      coverage,
        "data_available":         True,
        "easy_effective_total":   et,
        "medium_effective_total": mt,
        "hard_effective_total":   ht,
        "easy_accuracy":          ea,
        "medium_accuracy":        ma,
        "hard_accuracy":          ha,
        "easy_sufficient":        e_ok,
        "medium_sufficient":      m_ok,
        "hard_sufficient":        h_ok,
        "cap_active":             not h_ok,
        "confidence_fraction":    conf_frac,
    }


def _v5_compute_cognitive_analysis(
    target_rows: List[Dict[str, Any]],
    row_weights: Dict[str, float],
    expected_question_count: int,
) -> Dict[str, Any]:
    """Analyse cognitive-level evidence in target_rows.

    Recognized levels: recall, understanding, application, analysis, judgment.
    Higher-order:      application, analysis, judgment.
    Data activates when >= 90 % of rows carry a recognized cognitive value.
    cap_active = data_available AND higher_order_effective_total < higher_order_target.
    higher_order_target = max(10, floor(expected_question_count * 0.30)).
    """
    ho_target = max(V5_COGNITIVE_HO_MIN,
                    math.floor(expected_question_count * V5_COGNITIVE_HO_MULTIPLIER))

    _zero: Dict[str, Any] = {
        "metadata_coverage":            0.0,
        "data_available":               False,
        "level_effective_totals":       {lv: 0.0 for lv in V5_COGNITIVE_LEVELS},
        "level_accuracies":             {lv: 0.0 for lv in V5_COGNITIVE_LEVELS},
        "higher_order_effective_total": 0.0,
        "higher_order_accuracy":        0.0,
        "higher_order_target":          ho_target,
        "cap_active":                   False,
        "confidence_fraction":          0.0,
    }

    total = len(target_rows)
    if total == 0:
        return dict(_zero)

    recognized = sum(
        1 for r in target_rows
        if str(r.get("cognitive_level") or "").strip().lower() in V5_COGNITIVE_LEVELS
    )
    coverage = recognized / total

    if coverage < V5_METADATA_THRESHOLD:
        out = dict(_zero)
        out["metadata_coverage"] = coverage
        out["level_effective_totals"] = {lv: 0.0 for lv in V5_COGNITIVE_LEVELS}
        out["level_accuracies"]       = {lv: 0.0 for lv in V5_COGNITIVE_LEVELS}
        return out

    lc: Dict[str, float] = {lv: 0.0 for lv in V5_COGNITIVE_LEVELS}
    lt: Dict[str, float] = {lv: 0.0 for lv in V5_COGNITIVE_LEVELS}
    for row in target_rows:
        row_id = str(row.get("id", id(row)))
        w = row_weights.get(row_id, 0.0)
        lv = str(row.get("cognitive_level") or "").strip().lower()
        if lv not in V5_COGNITIVE_LEVELS:
            continue
        lt[lv] += w
        if bool(row.get("is_correct")):
            lc[lv] += w

    level_acc = {
        lv: (100.0 * lc[lv] / lt[lv] if lt[lv] > 0 else 0.0)
        for lv in V5_COGNITIVE_LEVELS
    }
    ho_total   = sum(lt[lv] for lv in V5_COGNITIVE_HIGHER_ORDER)
    ho_correct = sum(lc[lv] for lv in V5_COGNITIVE_HIGHER_ORDER)
    ho_acc     = 100.0 * ho_correct / ho_total if ho_total > 0 else 0.0
    conf_frac  = min(ho_total / ho_target, 1.0)

    return {
        "metadata_coverage":            coverage,
        "data_available":               True,
        "level_effective_totals":       lt,
        "level_accuracies":             level_acc,
        "higher_order_effective_total": ho_total,
        "higher_order_accuracy":        ho_acc,
        "higher_order_target":          ho_target,
        "cap_active":                   ho_total < ho_target,
        "confidence_fraction":          conf_frac,
    }


def _v5_compute_trend(scores: List[float]) -> Dict[str, Any]:
    """Compute trend delta, adjustment, and label from recent mock scores.

    3 scores:   delta = newest - mean(first two)
    4-5 scores: delta = mean(last two) - mean(all earlier scores)
    <3 scores:  delta = 0.0 (insufficient evidence)

    Adjustment: clamp(0.25 * delta, -4.0, +2.0)
    Labels: Improving >= 2, Declining <= -2, Stable otherwise.
    """
    n = len(scores)
    if n < 3:
        delta = 0.0
    elif n == 3:
        delta = scores[2] - statistics.mean(scores[:2])
    else:
        delta = statistics.mean(scores[-2:]) - statistics.mean(scores[:-2])

    adjustment = max(V5_TREND_CLAMP_MIN,
                     min(V5_TREND_CLAMP_MAX, V5_TREND_COEFFICIENT * delta))

    if delta >= V5_TREND_IMPROVING_THRESHOLD:
        label = "Improving"
    elif delta <= V5_TREND_DECLINING_THRESHOLD:
        label = "Declining"
    else:
        label = "Stable"

    return {
        "trend_delta":      delta,
        "trend_adjustment": adjustment,
        "trend_label":      label,
    }


def _v5_compute_staleness(
    newest_verified_dt: Optional[datetime],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Classify the age of the latest verified mock.

    States (inclusive boundaries in days):
        current: 0–90
        aging:   91–180
        old:     181–365
        stale:   > 365
        unknown: datetime is None

    Missing data is never classified as current.
    """
    if newest_verified_dt is None:
        return {"state": V5_STALENESS_UNKNOWN, "age_days": None}

    reference = now if now is not None else datetime.now(timezone.utc)
    age_days  = (reference - newest_verified_dt).days

    if age_days <= V5_STALENESS_CURRENT_MAX_DAYS:
        state = V5_STALENESS_CURRENT
    elif age_days <= V5_STALENESS_AGING_MAX_DAYS:
        state = V5_STALENESS_AGING
    elif age_days <= V5_STALENESS_OLD_MAX_DAYS:
        state = V5_STALENESS_OLD
    else:
        state = V5_STALENESS_STALE

    return {"state": state, "age_days": age_days}


def _v5_apply_score_caps(
    score: float,
    passing_score: float,
    staleness_state: str,
    domain_gap: bool,
    domain_floor: bool,
    difficulty_cap: bool,
    cognitive_cap: bool,
) -> Dict[str, Any]:
    """Apply score caps; the lowest (most restrictive) cap wins.

    Staleness caps: aging → passing+7, old → passing-3, stale → 0.
    Domain gap:     passing-3.
    Domain floor:   max(passing-5, 50).
    Difficulty:     passing-1.
    Cognitive:      passing+7.

    Returns final_score, applied_caps (all applicable), guardrail_applied, guardrail_cap.
    """
    caps: List[Tuple[str, float]] = []

    if staleness_state == V5_STALENESS_AGING:
        caps.append(("aging_staleness",       passing_score + V5_CAP_AGING_OFFSET))
    elif staleness_state == V5_STALENESS_OLD:
        caps.append(("old_staleness",         passing_score + V5_CAP_OLD_OFFSET))
    elif staleness_state == V5_STALENESS_STALE:
        caps.append(("stale",                 V5_CAP_STALE_VALUE))

    if domain_gap:
        caps.append(("domain_gap",            passing_score + V5_CAP_DOMAIN_GAP_OFFSET))
    if domain_floor:
        caps.append(("domain_floor",          max(passing_score + V5_CAP_DOMAIN_FLOOR_OFFSET,
                                                  V5_CAP_DOMAIN_FLOOR_MIN)))
    if difficulty_cap:
        caps.append(("difficulty_insufficient", passing_score + V5_CAP_DIFFICULTY_OFFSET))
    if cognitive_cap:
        caps.append(("cognitive_insufficient",  passing_score + V5_CAP_COGNITIVE_OFFSET))

    applied_caps = [{"reason": r, "cap": c} for r, c in caps]

    if not caps:
        return {
            "final_score":     round(score, 2),
            "applied_caps":    [],
            "guardrail_applied": False,
            "guardrail_cap":   None,
        }

    _, guardrail_cap = min(caps, key=lambda x: x[1])
    final_score      = min(score, guardrail_cap)

    return {
        "final_score":     round(final_score, 2),
        "applied_caps":    applied_caps,
        "guardrail_applied": score > guardrail_cap,
        "guardrail_cap":   guardrail_cap,
    }


def _v5_recency_fraction(age_days: Optional[int], staleness_state: str) -> float:
    """Return 0.0–1.0 recency fraction for the confidence recency component.

    age <= 30:   1.0
    31–90:       linear from 1.0 down to 0.70
    91–180:      linear from 0.70 down to 0.30
    181–365:     linear from 0.30 down to 0.0
    > 365:       0.0
    unknown:     0.0
    """
    if staleness_state == V5_STALENESS_UNKNOWN or age_days is None:
        return 0.0
    if age_days <= 30:
        return 1.0
    if age_days <= 90:
        return 1.0 - (age_days - 30) / 60.0 * 0.30
    if age_days <= 180:
        return 0.70 - (age_days - 90) / 90.0 * 0.40
    if age_days <= 365:
        return 0.30 - (age_days - 180) / 185.0 * 0.30
    return 0.0


def _v5_compute_confidence(
    verified_attempts: List[Dict[str, Any]],
    history_rows: List[Dict[str, Any]],
    domain_stats: Dict[str, dict],
    normalized_weights: Dict[str, float],
    difficulty_analysis: Dict[str, Any],
    cognitive_analysis: Dict[str, Any],
    staleness_state: str,
    age_days: Optional[int],
    captured_bank_size: Optional[int],
    live_bank_size: Optional[int],
    expected_question_count: int,
) -> Dict[str, Any]:
    """Compute V5 confidence score (0–100 points) from six components.

    Components and maximum points:
        verified mock volume:    30  (scales to V5_MAX_SCORING_MOCKS)
        unique-question breadth: 25  (distinct qids vs coverage_target)
        recency:                 20  (age-based linear decay)
        domain sufficiency:      15  (official-weighted per-domain fraction)
        difficulty evidence:      5  (difficulty confidence_fraction)
        cognitive evidence:       5  (cognitive confidence_fraction)

    coverage_target = min(bank_size, expected_q * 10).
    If captured_bank_size is absent, falls back to live_bank_size.
    """
    # 1. Verified mock volume (30 pts)
    vol_frac = min(len(verified_attempts) / V5_MAX_SCORING_MOCKS, 1.0)
    mock_volume_pts = vol_frac * V5_CONF_MOCK_VOLUME_PTS

    # 2. Unique-question breadth (25 pts)
    bank_size = captured_bank_size
    bank_fallback_used = (bank_size is None)   # True when captured_bank_size absent
    if bank_size is None:
        bank_size = live_bank_size

    coverage_target: Optional[int] = None
    breadth_pts = 0.0
    if bank_size is not None and bank_size > 0:
        coverage_target = min(bank_size, expected_question_count * V5_CONF_COVERAGE_MOCK_COUNT)
        distinct_qids = len({
            r.get("question_id") for r in history_rows if r.get("question_id") is not None
        })
        breadth_frac = min(distinct_qids / coverage_target, 1.0) if coverage_target > 0 else 0.0
        breadth_pts  = breadth_frac * V5_CONF_BREADTH_PTS

    # 3. Recency (20 pts)
    recency_pts = _v5_recency_fraction(age_days, staleness_state) * V5_CONF_RECENCY_PTS

    # 4. Domain sufficiency (15 pts) — official-weighted average of per-domain fraction
    domain_pts = 0.0
    if domain_stats and normalized_weights:
        wsum = sum(normalized_weights.values())
        if wsum > 0:
            df = 0.0
            for domain, stats in domain_stats.items():
                w    = normalized_weights.get(domain, 0.0) / wsum
                et   = stats["effective_total"]
                edq  = stats["expected_domain_questions"]
                st   = stats["evidence_state"]
                if st == V5_DOMAIN_UNCOVERED:
                    f = 0.0
                elif st == V5_DOMAIN_UNDER_SAMPLED:
                    f = 0.5 * et / edq if edq > 0 else 0.0
                elif st == V5_DOMAIN_SUFFICIENTLY_SAMPLED:
                    f = 0.5 + 0.5 * (et - edq) / edq if edq > 0 else 0.5
                else:  # reliably_sampled
                    f = 1.0
                df += w * f
            domain_pts = df * V5_CONF_DOMAIN_PTS

    # 5. Difficulty evidence (5 pts)
    diff_pts = difficulty_analysis.get("confidence_fraction", 0.0) * V5_CONF_DIFFICULTY_PTS

    # 6. Cognitive evidence (5 pts)
    cog_pts = cognitive_analysis.get("confidence_fraction", 0.0) * V5_CONF_COGNITIVE_PTS

    total = min(mock_volume_pts + breadth_pts + recency_pts + domain_pts + diff_pts + cog_pts,
                100.0)

    label = (
        "High"   if total >= V5_CONF_HIGH_THRESHOLD else
        "Medium" if total >= V5_CONF_MEDIUM_THRESHOLD else
        "Low"
    )

    return {
        "score":              round(total, 2),
        "label":              label,
        "mock_volume_pts":    round(mock_volume_pts, 4),
        "breadth_pts":        round(breadth_pts, 4),
        "recency_pts":        round(recency_pts, 4),
        "domain_pts":         round(domain_pts, 4),
        "difficulty_pts":     round(diff_pts, 4),
        "cognitive_pts":      round(cog_pts, 4),
        "coverage_target":    coverage_target,
        "bank_fallback_used": bank_fallback_used,
    }


# ---------------------------------------------------------------------------
# Eligibility  (V4 — unchanged)
# ---------------------------------------------------------------------------

def _parse_sort_value(attempt: Dict[str, Any]) -> str:
    return str(attempt.get("completed_at") or attempt.get("started_at") or attempt.get("id") or "")


def is_full_mock_attempt(attempt: Dict[str, Any], expected_question_count: int = 60) -> bool:
    """Return True only for full-length Paid Mock Exam attempts eligible for readiness."""
    total = _safe_int(attempt.get("total_questions"), 0)
    return is_readiness_eligible_mode(attempt.get("mode")) and total >= int(expected_question_count or 60)


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

def _empty_result(
    reason: str,
    eligible_mock_count: int = 0,
    expected_question_count: int = 60,
) -> Dict[str, Any]:
    ho_target = max(
        V5_COGNITIVE_HO_MIN,
        math.floor(expected_question_count * V5_COGNITIVE_HO_MULTIPLIER),
    )
    return {
        # Eligibility
        "is_locked": True,
        "eligible_mock_count": eligible_mock_count,
        "required_mock_count": REQUIRED_FULL_MOCKS,
        "mocks_remaining": max(REQUIRED_FULL_MOCKS - eligible_mock_count, 0),
        # Score
        "score": 0.0,
        "raw_score": 0.0,
        "hard_capped_score": 0.0,
        "label": "Readiness Locked",
        "color": "gray",
        "recommendation": reason,
        # V4 diagnostic keys
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
        # Pacing
        "pacing_status": "Insufficient Timing Data",
        "timing_completeness": 0.0,
        "fast_incorrect_rate": 0.0,
        "slow_answer_rate": 0.0,
        "median_time_per_question": 0.0,
        "target_time_per_question": 0.0,
        "timed_questions": 0,
        # Confidence
        "confidence_score": 0.0,
        "confidence_label": "Low",
        "confidence": "Low",
        "unique_questions_seen": 0,
        "question_attempt_completeness": 0.0,
        "domain_sample_sufficiency": 0.0,
        "coverage_percent": 0.0,
        # Backward-compatible keys
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
        "guardrail_cap": None,
        "domain_scores": {},
        "strong_domains": [],
        "weak_domains": [],
        # V5 fields
        "formula_version": READINESS_VERSION,
        "verified_mock_count": 0,
        "legacy_mock_count": 0,
        "invalid_mock_count": 0,
        "verified_attempt_ids": [],
        "legacy_attempt_ids": [],
        "trend_delta": 0.0,
        "staleness_state": V5_STALENESS_UNKNOWN,
        "staleness_days": None,
        "staleness_locked": False,
        "domain_states": {},
        "domain_gap_triggered": False,
        "domain_floor_triggered": False,
        "uncovered_domains": [],
        "difficulty_metadata_coverage": 0.0,
        "difficulty_data_available": False,
        "difficulty_cap_active": False,
        "difficulty_effective_totals": {},
        "difficulty_accuracies": {},
        "cognitive_metadata_coverage": 0.0,
        "cognitive_data_available": False,
        "cognitive_cap_active": False,
        "cognitive_effective_totals": {},
        "cognitive_accuracies": {},
        "higher_order_effective_total": 0.0,
        "higher_order_accuracy": 0.0,
        "higher_order_target": ho_target,
        "cross_mock_repeat_fraction": 0.0,
        "family_data_available": False,
        "effective_target_sample": 0.0,
        "captured_bank_size_used": 0,
        "bank_size_fallback_used": False,
        "coverage_target": 0,
        "applied_score_caps": [],
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
    captured_bank_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Calculate V5 READINESS_V5_VERIFIED_EVIDENCE readiness.

    Returns a dict with readiness score, diagnostics, confidence, and backward-
    compatible keys so existing Dashboard/My_Progress display code still works.
    Only verified paid mocks (child rows present and consistent) unlock and
    enter the score.  Legacy and invalid attempts appear in diagnostics only.
    """
    # ── 0. Normalize inputs ────────────────────────────────────────────────
    attempts = attempts or []
    question_attempts = question_attempts or []
    passing_score = _safe_float(passing_score, 65.0)
    expected_question_count = _safe_int(expected_question_count, 60) or 60
    time_limit_minutes = _safe_int(time_limit_minutes, 105) or 105
    bank_total = _safe_int(question_bank_total, 0) if question_bank_total is not None else 0
    bank_total_or_none: Optional[int] = bank_total if bank_total > 0 else None

    # ── 1. Grade every attempt ─────────────────────────────────────────────
    graded = v5_grade_all_attempts(attempts, question_attempts, expected_question_count)
    verified_list    = graded["verified"]
    legacy_list      = graded["legacy"]
    invalid_list     = graded["invalid"]
    verified_mock_count = len(verified_list)
    legacy_mock_count   = len(legacy_list)
    invalid_mock_count  = len(invalid_list)
    verified_attempt_ids = graded["verified_ids"] or []
    legacy_attempt_ids   = graded["legacy_ids"]   or []

    # ── 2. Pacing (all QAs regardless of grade) ────────────────────────────
    pacing = _compute_pacing_diagnostics(
        question_attempts, time_limit_minutes, expected_question_count
    )

    # ── 3. V4 domain stats for backward-compat domain_scores / weak_domains ─
    v4_domain_stats = _build_domain_stats(question_attempts, attempts)
    sorted_v4 = sorted(v4_domain_stats.items(), key=lambda x: x[1].get("percent", 0.0))
    weak_domains   = [n for n, d in sorted_v4[:3]  if d.get("total", 0) > 0]
    strong_domains = [n for n, d in sorted_v4[-3:]][::-1] if sorted_v4 else []
    total_attempted = sum(_safe_int(a.get("total_questions"), 0) for a in attempts)

    # ── 4. Lock check ──────────────────────────────────────────────────────
    if verified_mock_count < REQUIRED_FULL_MOCKS:
        result = _empty_result(
            f"Complete {REQUIRED_FULL_MOCKS} verified full paid mock exams to unlock "
            f"readiness.  You have {verified_mock_count} of {REQUIRED_FULL_MOCKS} required.",
            verified_mock_count,
            expected_question_count=expected_question_count,
        )
        result.update(pacing)
        result["domain_scores"]          = v4_domain_stats
        result["weak_domains"]           = weak_domains
        result["strong_domains"]         = strong_domains
        result["total_attempted"]        = total_attempted
        result["verified_mock_count"]    = verified_mock_count
        result["legacy_mock_count"]      = legacy_mock_count
        result["invalid_mock_count"]     = invalid_mock_count
        result["verified_attempt_ids"]   = verified_attempt_ids
        result["legacy_attempt_ids"]     = legacy_attempt_ids
        result["full_mock_count"]        = verified_mock_count
        result["eligible_mock_count"]    = verified_mock_count
        result["formula_version"]        = READINESS_VERSION
        result["unique_questions_seen"]  = count_verified_unique_questions_seen(
            attempts,
            question_attempts,
            expected_question_count,
        )
        return result

    # ── 5. Sort verified oldest → newest (V5 deterministic order) ──────────
    sorted_verified = sorted(verified_list, key=v5_attempt_sort_key)

    # ── 6. Scoring and history windows ─────────────────────────────────────
    scoring_window: List[Dict[str, Any]] = sorted_verified[-V5_MAX_SCORING_MOCKS:]
    history_window: List[Dict[str, Any]] = sorted_verified[-V5_MAX_REPEAT_HISTORY_MOCKS:]

    # ── 7. Attempt datetime map (for repeat-weight helper) ──────────────────
    attempt_dt_map: Dict[int, datetime] = {}
    for a in history_window:
        aid = v5_parse_attempt_id(a)
        adt = v5_parse_attempt_datetime(a)
        if aid is not None and adt is not None:
            attempt_dt_map[aid] = adt

    # ── 8. Filter child rows to scoring / history windows ───────────────────
    scoring_ids: set = {v5_parse_attempt_id(a) for a in scoring_window} - {None}
    history_ids: set = {v5_parse_attempt_id(a) for a in history_window} - {None}

    target_rows  = [
        r for r in question_attempts
        if _v5_parse_strict_int(r.get("exam_attempt_id")) in scoring_ids
    ]
    history_rows = [
        r for r in question_attempts
        if _v5_parse_strict_int(r.get("exam_attempt_id")) in history_ids
    ]

    # ── 9. Repeat-evidence weights ─────────────────────────────────────────
    evidence = v5_assign_evidence_weights(target_rows, history_rows, attempt_dt_map)
    row_weights                = evidence["weights"]
    cross_mock_repeat_fraction = evidence["cross_mock_repeat_fraction"]
    family_data_available      = evidence["family_data_available"]
    effective_target_sample    = evidence["effective_target_sample"]

    # ── 10. EMA on scoring-window scores (oldest → newest) ─────────────────
    scores = [_clamp(_safe_float(a.get("score"), 0.0)) for a in scoring_window]
    A = _compute_ema(scores, EMA_ALPHA)

    # ── 11. V5 domain analysis ─────────────────────────────────────────────
    normalized_weights = _v5_normalize_domain_weights(domain_weights)
    domain_stats_v5 = _v5_build_domain_stats(
        target_rows, row_weights, expected_question_count, normalized_weights
    )
    domain_result = _v5_compute_domain_score(domain_stats_v5, normalized_weights)

    has_domain_evidence = any(
        s["effective_total"] > 0 for s in domain_stats_v5.values()
    )
    if has_domain_evidence:
        D                  = domain_result["D"]
        F                  = domain_result["F"]
        DR                 = domain_result["DR"]
        weakest_domain_v5  = domain_result["weakest_domain"]
        weakest_score_v5   = domain_result["weakest_score"]
        domain_gap_triggered   = domain_result["domain_gap_triggered"]
        domain_floor_triggered = domain_result["domain_floor_triggered"]
        uncovered_domains      = domain_result["uncovered_domains"]
    else:
        D = F = DR = A
        weakest_domain_v5      = None
        weakest_score_v5       = None
        domain_gap_triggered   = False
        domain_floor_triggered = False
        uncovered_domains      = []

    # ── 12. Performance base ───────────────────────────────────────────────
    Base = 0.80 * A + 0.20 * DR

    # ── 13. Consistency penalty ────────────────────────────────────────────
    SD = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    consistency_penalty = min(8.0, max(0.0, (SD - 5.0) * 0.25))

    # ── 14. V5 trend ──────────────────────────────────────────────────────
    trend_result    = _v5_compute_trend(scores)
    trend_delta     = trend_result["trend_delta"]
    trend_adjustment = trend_result["trend_adjustment"]
    trend_label     = trend_result["trend_label"]
    trend_slope     = trend_delta   # backward compat alias

    # ── 15. Raw and hard-capped score ─────────────────────────────────────
    raw_score        = Base - consistency_penalty + trend_adjustment
    hard_capped_score = min(raw_score, A + 5.0)

    # ── 16. Staleness ─────────────────────────────────────────────────────
    newest_dt      = v5_parse_attempt_datetime(scoring_window[-1])
    stale_result   = _v5_compute_staleness(newest_dt)
    staleness_state = stale_result["state"]
    staleness_days  = stale_result["age_days"]
    staleness_locked = staleness_state == V5_STALENESS_STALE

    # ── 17. Difficulty and cognitive evidence analysis ─────────────────────
    difficulty_analysis = _v5_compute_difficulty_analysis(target_rows, row_weights)
    cognitive_analysis  = _v5_compute_cognitive_analysis(
        target_rows, row_weights, expected_question_count
    )
    difficulty_cap_active = difficulty_analysis["cap_active"]
    cognitive_cap_active  = cognitive_analysis["cap_active"]

    # ── 18. Score caps ─────────────────────────────────────────────────────
    caps_result = _v5_apply_score_caps(
        hard_capped_score,
        passing_score,
        staleness_state,
        domain_gap_triggered,
        domain_floor_triggered,
        difficulty_cap_active,
        cognitive_cap_active,
    )

    # ── 19. Final score and label ──────────────────────────────────────────
    if staleness_locked:
        final_score      = 0.0
        label            = "Evidence Stale"
        is_locked        = True
        staleness_locked_flag = True
    else:
        final_score      = round(_clamp(caps_result["final_score"]), 2)
        is_locked        = False
        staleness_locked_flag = False
        label            = readiness_label(final_score, passing_score)

    # ── 20. V5 confidence ─────────────────────────────────────────────────
    conf = _v5_compute_confidence(
        verified_attempts   = history_window,
        history_rows        = history_rows,
        domain_stats        = domain_stats_v5,
        normalized_weights  = normalized_weights,
        difficulty_analysis = difficulty_analysis,
        cognitive_analysis  = cognitive_analysis,
        staleness_state     = staleness_state,
        age_days            = staleness_days,
        captured_bank_size  = captured_bank_size,
        live_bank_size      = bank_total_or_none,
        expected_question_count = expected_question_count,
    )

    # ── 21. Backward-compat counters ───────────────────────────────────────
    unique_questions_seen = len({
        r.get("question_id") for r in history_rows
        if r.get("question_id") is not None
    })

    expected_linked = sum(
        _safe_int(a.get("total_questions"), 0) for a in history_window
    )
    unique_pairs = len({
        (str(r.get("exam_attempt_id")), str(r.get("question_id")))
        for r in history_rows
        if r.get("exam_attempt_id") is not None and r.get("question_id") is not None
    })
    completeness = min(unique_pairs / expected_linked, 1.0) if expected_linked > 0 else 0.0

    domain_sample_sufficiency = 0.0
    if domain_stats_v5 and normalized_weights:
        wsum = sum(normalized_weights.values())
        if wsum > 0:
            for _d, _s in domain_stats_v5.items():
                _w   = normalized_weights.get(_d, 0.0) / wsum
                _edq = _s["expected_domain_questions"]
                domain_sample_sufficiency += _w * min(
                    _s["effective_total"] / _edq, 1.0
                ) if _edq > 0 else 0.0

    # ── 22. Recommendation ────────────────────────────────────────────────
    if staleness_locked_flag:
        recommendation = (
            "Your most recent verified mock exam is over a year old. "
            "Take a new full paid mock exam to restore readiness."
        )
    elif final_score < passing_score:
        focus = weak_domains[0] if weak_domains else "your weakest domains"
        recommendation = (
            f"Your readiness is below the passing benchmark. "
            f"Focus next on {focus}, then retake a full mock exam."
        )
    elif weakest_domain_v5:
        recommendation = (
            f"You are trending exam-ready, but {weakest_domain_v5} remains your "
            f"highest-risk area. Strengthen it before scheduling."
        )
    else:
        recommendation = (
            "You are trending exam-ready. "
            "Take another full mock exam to confirm consistency."
        )

    # ── 23. Assemble return payload ────────────────────────────────────────
    return {
        # Eligibility
        "is_locked":           is_locked,
        "eligible_mock_count": verified_mock_count,
        "required_mock_count": REQUIRED_FULL_MOCKS,
        "mocks_remaining":     0,
        "full_mock_count":     verified_mock_count,
        # Score
        "score":            final_score,
        "raw_score":        round(_clamp(raw_score), 2),
        "hard_capped_score": round(hard_capped_score, 2),
        "label":            label,
        "color":            "gray" if staleness_locked_flag else readiness_color(final_score, passing_score),
        "recommendation":   recommendation,
        # V4-compat diagnostic keys
        "recent_accuracy":                  round(A,  2),
        "domain_score":                     round(D,  2),
        "domain_robustness":                round(DR, 2),
        "weakest_reliable_domain":          weakest_domain_v5,
        "weakest_reliable_domain_score":    round(weakest_score_v5, 2) if weakest_score_v5 is not None else 0.0,
        "consistency_standard_deviation":   round(SD, 2),
        "consistency_penalty":              round(consistency_penalty, 2),
        "trend_slope":                      round(trend_slope, 2),
        "trend_adjustment":                 round(trend_adjustment, 2),
        "trend_label":                      trend_label,
        # Pacing (diagnostics only)
        **pacing,
        # Confidence keys
        "confidence_score":  conf["score"],
        "confidence_label":  conf["label"],
        "confidence":        conf["label"],
        # Backward-compat coverage / completeness
        "unique_questions_seen":        unique_questions_seen,
        "question_attempt_completeness": round(completeness, 3),
        "domain_sample_sufficiency":    round(domain_sample_sufficiency, 3),
        "coverage_percent":             round((unique_questions_seen / bank_total) * 100, 2) if bank_total > 0 else 0.0,
        # Domain
        "domain_scores": v4_domain_stats,
        "domain_states": domain_stats_v5,
        "weak_domains":  weak_domains,
        "strong_domains": strong_domains,
        # Backward-compatible score keys
        "accuracy_score":        round(A,  2),
        "recent_mock_score":     round(A,  2),
        "domain_balance_score":  round(DR, 2),
        "weighted_domain_score": round(DR, 2),
        "coverage_score":        0.0,
        "pacing_score":          0.0,
        "consistency_score":     0.0,
        "practice_volume_score": 0.0,
        "total_attempted":   total_attempted,
        "mock_scores_used":  list(reversed(scores)),
        # Guardrail
        "guardrail_applied": caps_result["guardrail_applied"],
        "guardrail_cap":     caps_result["guardrail_cap"],
        "applied_score_caps": caps_result["applied_caps"],
        # V5 diagnostics
        "formula_version":        READINESS_VERSION,
        "verified_mock_count":    verified_mock_count,
        "legacy_mock_count":      legacy_mock_count,
        "invalid_mock_count":     invalid_mock_count,
        "verified_attempt_ids":   verified_attempt_ids,
        "legacy_attempt_ids":     legacy_attempt_ids,
        "trend_delta":            trend_delta,
        "staleness_state":        staleness_state,
        "staleness_days":         staleness_days,
        "staleness_locked":       staleness_locked_flag,
        "domain_gap_triggered":   domain_gap_triggered,
        "domain_floor_triggered": domain_floor_triggered,
        "uncovered_domains":      uncovered_domains,
        "difficulty_metadata_coverage": difficulty_analysis["metadata_coverage"],
        "difficulty_data_available":    difficulty_analysis["data_available"],
        "difficulty_cap_active":        difficulty_cap_active,
        "difficulty_effective_totals":  {
            "easy":   difficulty_analysis["easy_effective_total"],
            "medium": difficulty_analysis["medium_effective_total"],
            "hard":   difficulty_analysis["hard_effective_total"],
        },
        "difficulty_accuracies": {
            "easy":   difficulty_analysis["easy_accuracy"],
            "medium": difficulty_analysis["medium_accuracy"],
            "hard":   difficulty_analysis["hard_accuracy"],
        },
        "cognitive_metadata_coverage":      cognitive_analysis["metadata_coverage"],
        "cognitive_data_available":         cognitive_analysis["data_available"],
        "cognitive_cap_active":             cognitive_cap_active,
        "cognitive_effective_totals":       cognitive_analysis["level_effective_totals"],
        "cognitive_accuracies":             cognitive_analysis["level_accuracies"],
        "higher_order_effective_total":     cognitive_analysis["higher_order_effective_total"],
        "higher_order_accuracy":            cognitive_analysis["higher_order_accuracy"],
        "higher_order_target":              cognitive_analysis["higher_order_target"],
        "cross_mock_repeat_fraction":       cross_mock_repeat_fraction,
        "family_data_available":            family_data_available,
        "effective_target_sample":          effective_target_sample,
        "captured_bank_size_used":          captured_bank_size or 0,
        "bank_size_fallback_used":          conf["bank_fallback_used"],
        "coverage_target":                  conf["coverage_target"] or 0,
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
