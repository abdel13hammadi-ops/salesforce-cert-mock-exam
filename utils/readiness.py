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
    if mode != "paid mock exam":
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
# Eligibility  (V4 — unchanged)
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
