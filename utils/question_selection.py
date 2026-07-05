"""
V40 repeat-resistant question selection for paid full mock exams.

Pure, testable ranking and selection logic.  No Streamlit or Supabase imports.
History loading and wiring lives in app.py.
"""

import random
from collections import defaultdict
from typing import Optional


# ── Family identity ───────────────────────────────────────────────────────────

def _family_key(q: dict) -> str:
    """Return a stable family identifier for a question.

    Non-null question_family_id  → namespaced family key shared with related
                                   questions from the same stem.
    Null / empty family_id       → per-question unique key, so two questions
                                   that both lack a family_id are never treated
                                   as related to each other.
    """
    fid = q.get("question_family_id")
    if fid is not None and str(fid).strip():
        return f"fam:{fid}"
    return f"solo:{q['id']}"


# ── History context ───────────────────────────────────────────────────────────

def build_history_context(
    all_paid_attempts: list,
    question_exposure_rows: list,
    recent_attempt_count: int = 2,
) -> dict:
    """Build a history context dict from raw Supabase rows.

    Parameters
    ----------
    all_paid_attempts : list[dict]
        Rows from exam_attempts ordered by completed_at DESC (most-recent first).
        Each row must have at least ``id`` and ``completed_at``.
    question_exposure_rows : list[dict]
        Rows from question_attempts.  Each row must have at least
        ``question_id`` and ``exam_attempt_id``.
        An optional ``completed_at`` field is used for last-seen tracking.
    recent_attempt_count : int
        How many of the most-recent completed attempts constitute the
        "recent window" for family-recency checks.

    Returns
    -------
    dict with keys:
        seen_question_ids   – set[str]         all previously seen question IDs
        exposure_count      – dict[str, int]   exposures per question
        last_seen           – dict[str, str]   most-recent completed_at per question
        recent_attempt_ids  – set[str]         attempt IDs in the recent window
        recent_question_ids – set[str]         question IDs seen in recent attempts
    """
    seen_question_ids: set = set()
    exposure_count: dict = defaultdict(int)
    last_seen: dict = {}

    recent_slice = list(all_paid_attempts or [])[:recent_attempt_count]
    recent_attempt_ids: set = {
        str(a["id"]) for a in recent_slice if a.get("id") is not None
    }
    recent_question_ids: set = set()

    for row in question_exposure_rows or []:
        qid = row.get("question_id")
        if qid is None:
            continue
        qid_str = str(qid)
        seen_question_ids.add(qid_str)
        exposure_count[qid_str] += 1

        ts = row.get("completed_at") or ""
        if ts and (qid_str not in last_seen or ts > last_seen[qid_str]):
            last_seen[qid_str] = ts

        if str(row.get("exam_attempt_id") or "") in recent_attempt_ids:
            recent_question_ids.add(qid_str)

    return {
        "seen_question_ids": seen_question_ids,
        "exposure_count": dict(exposure_count),
        "last_seen": last_seen,
        "recent_attempt_ids": recent_attempt_ids,
        "recent_question_ids": recent_question_ids,
    }


# ── Waterfall ranking ─────────────────────────────────────────────────────────

def _compute_recent_families(pool: list, recent_question_ids: set) -> set:
    """Return the family keys for every question in pool that was recently seen."""
    recent_families: set = set()
    for q in pool:
        if str(q["id"]) in recent_question_ids:
            recent_families.add(_family_key(q))
    return recent_families


def rank_questions_waterfall(
    pool: list,
    history: dict,
    rng: Optional[random.Random] = None,
) -> list:
    """Rank pool questions by the V40 five-tier waterfall.

    Tier 1  unseen · mock-only (practice_eligible=False) · family NOT recent
    Tier 2  unseen · mock-only                           · family IS  recent
    Tier 3  unseen · practice_eligible=True              · family NOT recent
    Tier 4  previously seen                              · family NOT recent
               secondary: exposure ASC → last_seen ASC
    Tier 5  all remaining (seen + recent family, or unseen + recent family)
               secondary: unseen first → exposure ASC → last_seen ASC

    Within otherwise identical sort keys a per-question random jitter ensures
    exams are not deterministically identical across runs.

    Parameters
    ----------
    pool : list[dict]
        Candidate questions.  Each dict must contain at minimum ``id``,
        ``practice_eligible``, and ``question_family_id``.
    history : dict
        As returned by ``build_history_context()``, or an equivalent
        empty-history dict (first-time user).
    rng : random.Random or None
        Supply a seeded instance for deterministic tests.

    Returns
    -------
    list[dict]  sorted from highest priority (Tier 1) to lowest (Tier 5).
    """
    if rng is None:
        rng = random.Random()

    seen_ids: set = history.get("seen_question_ids") or set()
    exposure_count: dict = history.get("exposure_count") or {}
    last_seen_map: dict = history.get("last_seen") or {}
    recent_question_ids: set = history.get("recent_question_ids") or set()

    recent_families = _compute_recent_families(pool, recent_question_ids)

    ranked = []
    for q in pool:
        qid = str(q["id"])
        fkey = _family_key(q)
        is_unseen = qid not in seen_ids
        # practice_eligible defaults to True (not mock-only) when absent
        is_mock_only = not bool(q.get("practice_eligible", True))
        is_recent_family = fkey in recent_families
        exp = exposure_count.get(qid, 0)
        ts = last_seen_map.get(qid, "")
        # unseen_bit: 0 = unseen (preferred), 1 = seen
        unseen_bit = 0 if is_unseen else 1

        if is_unseen and is_mock_only and not is_recent_family:
            tier = 1
        elif is_unseen and is_mock_only and is_recent_family:
            tier = 2
        elif is_unseen and not is_mock_only and not is_recent_family:
            tier = 3
        elif not is_unseen and not is_recent_family:
            tier = 4
        else:
            tier = 5

        ranked.append((tier, unseen_bit, exp, ts, rng.random(), q))

    ranked.sort(key=lambda x: x[:5])
    return [item[5] for item in ranked]


# ── Difficulty targets ────────────────────────────────────────────────────────

def difficulty_targets(count: int) -> dict:
    """Return per-difficulty target counts for a domain quota.

    This mirrors the existing paid-mock difficulty distribution exactly
    (≈20% easy / 50% medium / 30% hard, with easy suppressed for tiny
    quotas).  Targets are advisory: the caller breaks as soon as the domain
    quota is met, so the small over-allocation that the original formula can
    produce for very small counts is harmless.
    """
    if count <= 0:
        return {"easy": 0, "medium": 0, "hard": 0}
    easy = max(1, round(count * 0.20)) if count >= 5 else 0
    medium = max(1, round(count * 0.50))
    hard = max(1, count - easy - medium)
    return {"easy": easy, "medium": medium, "hard": hard}


# ── Domain selection ──────────────────────────────────────────────────────────

def select_questions_for_domain(
    pool: list,
    required_count: int,
    history: dict,
    rng: Optional[random.Random] = None,
    preserve_difficulty: bool = True,
) -> list:
    """Select required_count questions from pool using the V40 waterfall.

    When ``preserve_difficulty`` is True the existing per-domain difficulty
    targets remain the authority: within each difficulty bucket candidates are
    ranked by the five-tier waterfall, then any shortage is filled from the
    full domain pool (still waterfall-ranked) so the exact domain quota is met.

    Uniqueness rules in priority order:
      1. never the same question id twice;
      2. never two questions from the same family while a family-unique
         alternative still exists;
      3. only when inventory is insufficient is the family constraint relaxed
         to satisfy the domain quota.

    Returns at most min(required_count, len(pool)) questions.
    """
    if rng is None:
        rng = random.Random()

    if not pool or required_count <= 0:
        return []

    selected: list = []
    selected_ids: set = set()
    selected_families: set = set()

    def _try_add(q, enforce_family: bool) -> bool:
        qid = str(q["id"])
        if qid in selected_ids:
            return False
        fkey = _family_key(q)
        if enforce_family and fkey in selected_families:
            return False
        selected.append(q)
        selected_ids.add(qid)
        selected_families.add(fkey)
        return True

    # Step 1 – fill difficulty buckets, waterfall-ranked within each bucket.
    if preserve_difficulty:
        targets = difficulty_targets(required_count)
        by_diff: dict = defaultdict(list)
        for q in pool:
            by_diff[(q.get("difficulty") or "medium")].append(q)
        for diff in ("easy", "medium", "hard"):
            if len(selected) >= required_count:
                break
            target = targets.get(diff, 0)
            if target <= 0:
                continue
            ranked_bucket = rank_questions_waterfall(by_diff.get(diff, []), history, rng=rng)
            added = 0
            for q in ranked_bucket:
                if added >= target or len(selected) >= required_count:
                    break
                if _try_add(q, enforce_family=True):
                    added += 1

    # Step 2 – fill any shortage from the full pool, family-unique first.
    if len(selected) < required_count:
        ranked_all = rank_questions_waterfall(pool, history, rng=rng)
        for q in ranked_all:
            if len(selected) >= required_count:
                break
            _try_add(q, enforce_family=True)

    # Step 3 – relax family uniqueness only when inventory is insufficient.
    if len(selected) < required_count:
        ranked_all = rank_questions_waterfall(pool, history, rng=rng)
        for q in ranked_all:
            if len(selected) >= required_count:
                break
            _try_add(q, enforce_family=False)

    return selected[:required_count]


# ── Top-level entry point ─────────────────────────────────────────────────────

def select_paid_mock_questions(
    bank: list,
    category_counts: dict,
    history: dict,
    rng: Optional[random.Random] = None,
) -> dict:
    """Select questions for every domain using the V40 waterfall.

    Domain quotas from category_counts are applied independently; the waterfall
    runs separately inside each domain pool.

    Parameters
    ----------
    bank : list[dict]
        Full eligible question bank (all domains combined).
    category_counts : dict[str, int]
        Target question count per domain / category name.
    history : dict
        As returned by ``build_history_context()``.  Pass an empty-history
        dict (all fields set to empty collections) for first-time users.
    rng : random.Random or None
        Supply a seeded instance for deterministic tests.

    Returns
    -------
    dict with:
        ``"selected"`` – list[dict]  chosen questions (domain order)
        ``"missing"``  – list[str]   categories with insufficient inventory
    """
    if rng is None:
        rng = random.Random()

    by_category: dict = defaultdict(list)
    for q in bank:
        by_category[q["category"]].append(q)

    selected: list = []
    missing: list = []

    for category, required_count in category_counts.items():
        pool = by_category.get(category, [])
        if len(pool) < required_count:
            missing.append(f"{category}: need {required_count}, found {len(pool)}")
        chosen = select_questions_for_domain(pool, required_count, history, rng=rng)
        selected.extend(chosen)

    return {"selected": selected, "missing": missing}


# ── Multi-select balancing (V40-aware) ────────────────────────────────────────

def balance_multi_select(
    selected: list,
    by_category: dict,
    history: dict,
    rng: Optional[random.Random] = None,
    min_multi: int = 8,
    max_multi: int = 10,
) -> list:
    """Adjust the selected set toward min_multi..max_multi multiple-answer
    questions without weakening V40 guarantees.

    Replacements are drawn from the same category, chosen by the same five-tier
    waterfall (ranking + history), and:
      * never duplicate a question id already selected;
      * prefer a family-unique replacement, only reusing a family when no
        family-unique alternative exists;
      * are always a 1-for-1 swap, so total count and per-category counts are
        preserved exactly.

    Returns a new list (does not mutate the input list object).
    """
    if rng is None:
        rng = random.Random()

    selected = list(selected)
    selected_ids = {str(q["id"]) for q in selected}

    def _families() -> set:
        return {_family_key(q) for q in selected}

    def _ranked_candidates(category: str, want_type: str) -> list:
        candidates = [
            c for c in by_category.get(category, [])
            if c.get("type") == want_type and str(c["id"]) not in selected_ids
        ]
        return rank_questions_waterfall(candidates, history, rng=rng)

    def _pick(ranked: list, outgoing_family: str) -> Optional[dict]:
        # Families occupied by other already-selected questions (the outgoing
        # question's slot is about to be freed, so its own family is allowed).
        occupied = _families()
        for c in ranked:
            fkey = _family_key(c)
            if fkey not in occupied or fkey == outgoing_family:
                return c
        # No family-unique alternative exists: relax to still hit 8–10.
        return ranked[0] if ranked else None

    def _swap(idx: int, outgoing: dict, incoming: dict) -> None:
        selected[idx] = incoming
        selected_ids.discard(str(outgoing["id"]))
        selected_ids.add(str(incoming["id"]))

    multi_count = sum(1 for q in selected if q.get("type") == "multiple")

    if multi_count < min_multi:
        for idx in range(len(selected)):
            if multi_count >= min_multi:
                break
            q = selected[idx]
            if q.get("type") == "multiple":
                continue
            ranked = _ranked_candidates(q.get("category"), "multiple")
            replacement = _pick(ranked, _family_key(q))
            if replacement is not None:
                _swap(idx, q, replacement)
                multi_count += 1

    elif multi_count > max_multi:
        for idx in range(len(selected)):
            if multi_count <= max_multi:
                break
            q = selected[idx]
            if q.get("type") != "multiple":
                continue
            ranked = _ranked_candidates(q.get("category"), "single")
            replacement = _pick(ranked, _family_key(q))
            if replacement is not None:
                _swap(idx, q, replacement)
                multi_count -= 1

    return selected


# ── Question-attempt persistence (V40) ────────────────────────────────────────
#
# These helpers are import-safe (no Streamlit / Supabase imports).  The
# persistence functions accept a duck-typed ``supabase`` client so they can be
# unit-tested with a fake client.  Row building is deliberately separated from
# persistence so the JSON-safe shape can be asserted independently.

def normalize_option_list(value) -> list:
    """Coerce an answer/option value into a JSON-safe list of strings.

    Handles list, tuple, set, scalar, and None safely:
      * None              → []
      * list/tuple/set    → [str(v) for v in value]
      * scalar (str/int…) → [str(value)]
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def build_question_attempt_rows(
    questions: list,
    answers: dict,
    *,
    exam_attempt_id,
    user_email: str,
    default_exam_name: str,
    default_language_code: str,
    answered_at_iso: str,
    time_spent_by_index: Optional[dict] = None,
) -> list:
    """Build exactly one JSON-safe question_attempts row per question.

    Pure function: no network, no Streamlit.  ``answers`` is keyed by the
    question's positional index (matching the app's session_state.answers).
    No question text or secrets are ever included.

    Each row includes immutable metadata captured from the question exactly as
    shown to the student (cognitive_level, concept_key, question_family_id,
    question_content_version, question_external_key, metadata_source,
    metadata_capture_version).  Missing fields become NULL in the database.
    """
    from utils.readiness_persistence import build_attempt_metadata  # noqa: PLC0415

    time_spent_by_index = time_spent_by_index or {}
    rows: list = []
    for idx, q in enumerate(questions):
        selected = normalize_option_list(answers.get(idx))
        correct = normalize_option_list(q.get("answers"))
        raw_ts = time_spent_by_index.get(idx)
        try:
            time_spent = int(raw_ts) if raw_ts is not None else None
        except (TypeError, ValueError):
            time_spent = None
        row = {
            "exam_attempt_id": exam_attempt_id,
            "question_id": q.get("id"),
            "user_email": user_email,
            "exam_name": q.get("exam_name") or default_exam_name,
            "language_code": q.get("language_code") or default_language_code,
            "category": q.get("category") or "Uncategorized",
            "difficulty": q.get("difficulty") or "medium",
            "selected_options": selected,
            "correct_options": correct,
            "is_correct": set(selected) == set(correct),
            "time_spent_seconds": time_spent,
            "answered_at": answered_at_iso,
        }
        row.update(build_attempt_metadata(q))
        rows.append(row)
    return rows


def chunk_rows(rows: list, size: int = 50) -> list:
    """Split rows into chunks of at most ``size`` (default 50)."""
    if size <= 0:
        size = 50
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def resolve_exam_attempt_id(insert_result, recover_fn=None):
    """Return the inserted exam_attempts.id, or fall back to a recent-match lookup.

    ``insert_result`` is the object returned by the parent insert .execute().
    When its data contains no usable id, ``recover_fn`` (a zero-arg recent-match
    lookup) is invoked. Never raises.
    """
    rows = getattr(insert_result, "data", None) or []
    if rows:
        rid = rows[0].get("id")
        if rid is not None:
            return rid
    if recover_fn is not None:
        try:
            return recover_fn()
        except Exception:
            return None
    return None


def _normalized(value) -> str:
    return str(value or "").strip().lower()


def _exam_attempt_row_matches_expected(
    row,
    *,
    expected_user_email=None,
    expected_mode=None,
    expected_exam_name=None,
    expected_language_code=None,
) -> bool:
    """True only if every identity field on ``row`` matches the expected value.

    Comparisons are case-insensitive/whitespace-trimmed (matching how
    user_email is already normalized elsewhere) but otherwise exact -- no
    partial or heuristic matching.
    """
    return (
        _normalized(row.get("user_email")) == _normalized(expected_user_email)
        and _normalized(row.get("mode")) == _normalized(expected_mode)
        and _normalized(row.get("exam_name")) == _normalized(expected_exam_name)
        and _normalized(row.get("language_code")) == _normalized(expected_language_code)
    )


def resolve_or_create_exam_attempt_id(
    supabase,
    payload,
    *,
    existing_attempt_id=None,
    expected_user_email=None,
    expected_mode=None,
    expected_exam_name=None,
    expected_language_code=None,
):
    """Reuse an already-known parent exam_attempts id, or insert one and resolve it.

    Pure with respect to session/UI state: the caller decides what
    ``existing_attempt_id`` is (typically a value already stored in session
    state) and is responsible for storing the returned id immediately, before
    any child persistence, so a later retry reuses it instead of inserting a
    second parent row.

    A stored id is never trusted blindly. When ``existing_attempt_id`` is
    given, exactly that row is looked up and its ``user_email``, ``mode``,
    ``exam_name``, and ``language_code`` must match the ``expected_*``
    arguments. This guards against a stale id left in session state (for
    example after a same-tab account switch, or one that belonged to a
    different workflow) being reused to attach child rows to the wrong
    parent. If the row is missing or any expected field mismatches, the id is
    treated as absent -- a new parent is inserted instead, and the
    mismatched row is never updated or deleted.

    If the verification query itself raises, the exception propagates
    unchanged: an unknown database failure must never be silently treated as
    "no existing row" and turned into a second parent insert.

    Intentionally has no heuristic recent-match fallback: callers that need
    that (the paid mock exam path) call ``resolve_exam_attempt_id`` directly
    with an explicit ``recover_fn``. Reusing a heuristic here would risk
    matching the wrong practice session by score/timestamp/category alone.
    """
    if existing_attempt_id is not None:
        result = (
            supabase.table("exam_attempts")
            .select("id,user_email,mode,exam_name,language_code")
            .eq("id", existing_attempt_id)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        row = rows[0] if rows else None
        if row is not None and _exam_attempt_row_matches_expected(
            row,
            expected_user_email=expected_user_email,
            expected_mode=expected_mode,
            expected_exam_name=expected_exam_name,
            expected_language_code=expected_language_code,
        ):
            return existing_attempt_id
        # Missing or mismatched: do not reuse, do not touch the row. Fall
        # through to the normal insert-and-resolve path below.

    insert_result = supabase.table("exam_attempts").insert(payload).execute()
    return resolve_exam_attempt_id(insert_result)


def count_question_attempts(supabase, exam_attempt_id) -> int:
    """Return the number of question_attempts rows for an attempt.

    Prefers PostgREST's exact count; falls back to len(data) if the client
    does not populate ``count``.
    """
    result = (
        supabase.table("question_attempts")
        .select("question_id", count="exact")
        .eq("exam_attempt_id", exam_attempt_id)
        .execute()
    )
    count = getattr(result, "count", None)
    if count is not None:
        return int(count)
    return len(getattr(result, "data", None) or [])


def count_distinct_question_attempts(supabase, exam_attempt_id) -> int:
    """Return the number of DISTINCT question_id values saved for an attempt.

    The unique constraint (exam_attempt_id, question_id) means this equals the
    row count in a healthy table, but verifying it explicitly guards against any
    future schema drift and satisfies the distinct-count success requirement.
    """
    result = (
        supabase.table("question_attempts")
        .select("question_id")
        .eq("exam_attempt_id", exam_attempt_id)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    return len({r.get("question_id") for r in rows if r.get("question_id") is not None})


def persist_question_attempts(
    supabase,
    rows: list,
    *,
    exam_attempt_id,
    expected_count: Optional[int],
    chunk_size: int = 50,
    on_error=None,
) -> tuple:
    """Upsert question_attempts rows in chunks and verify the saved count.

    Idempotency: relies on the unique constraint (exam_attempt_id, question_id)
    via upsert, so a Streamlit rerun repairs missing/partial rows instead of
    duplicating them or skipping permanently.

    Failure handling: exceptions are NOT silently swallowed.  Any failure is
    forwarded to ``on_error`` (used by the app for Sentry capture) and reported
    as a SAFE message that contains no credentials, payloads, tokens, answers,
    or question text.

    Returns (ok: bool, error: Optional[str]).
    """
    from utils.paid_mock_diagnostics import (
        log_chunk_complete,
        log_chunk_start,
        log_count_verification,
        log_distinct_count_verification,
        log_persistence_complete,
        log_persistence_exception,
    )

    def _report(exc, message: str) -> tuple:
        log_persistence_exception(exc=exc)
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                pass
        return False, message

    if not rows:
        return False, "No question results to save."

    try:
        for chunk_num, chunk in enumerate(chunk_rows(rows, chunk_size), start=1):
            log_chunk_start(chunk_num=chunk_num, chunk_size=len(chunk))
            (
                supabase.table("question_attempts")
                .upsert(chunk, on_conflict="exam_attempt_id,question_id")
                .execute()
            )
            log_chunk_complete(chunk_num=chunk_num, chunk_size=len(chunk))
    except Exception as exc:
        return _report(exc, "Could not save detailed question results. Your score was recorded.")

    try:
        saved = count_question_attempts(supabase, exam_attempt_id)
    except Exception as exc:
        return _report(exc, "Could not verify saved question results. Your score was recorded.")

    log_count_verification(expected_count=expected_count, saved_count=saved)

    if expected_count is not None and saved != expected_count:
        log_persistence_complete(success=False)
        return False, "Detailed question results were incomplete and will be retried."

    # Distinct-count verification: success requires the expected number of
    # DISTINCT question ids, not merely the expected row count.
    try:
        distinct = count_distinct_question_attempts(supabase, exam_attempt_id)
    except Exception as exc:
        return _report(exc, "Could not verify saved question results. Your score was recorded.")

    log_distinct_count_verification(expected_count=expected_count, distinct_count=distinct)

    if expected_count is not None and distinct != expected_count:
        log_persistence_complete(success=False)
        return False, "Detailed question results were incomplete and will be retried."

    log_persistence_complete(success=True)
    return True, None
