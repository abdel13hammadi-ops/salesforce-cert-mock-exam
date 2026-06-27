"""
Paid-mock submission orchestration: immutable snapshot + persistence state machine.

Pure, import-safe logic (no Streamlit, no Supabase).  app.py owns session_state
and the actual database call; this module owns:

  * building an immutable submission snapshot at submit time, and
  * deciding what the results page should do on each rerun (the state machine).

State machine
-------------
    idle    -> a snapshot exists, persistence has not been attempted yet.
    saving  -> a persistence attempt is in progress this run (set immediately
               before calling the DB; idempotent if a rerun re-enters it).
    saved   -> parent + all expected distinct children verified saved.
    failed  -> a persistence attempt failed; show a safe error + Retry button.

Transitions (see ``plan_persistence``):
    idle              -> run  (auto first attempt)            -> saving
    failed + retry    -> run  (explicit Retry button)         -> saving
    saving            -> run  (rerun re-entry; upsert is idempotent)
    saved             -> skip (never persist again)
    failed (no retry) -> skip (wait for the Retry button)
"""

from __future__ import annotations

import copy
from typing import Any, Optional

STATE_IDLE = "idle"
STATE_SAVING = "saving"
STATE_SAVED = "saved"
STATE_FAILED = "failed"

VALID_STATES = {STATE_IDLE, STATE_SAVING, STATE_SAVED, STATE_FAILED}


# ── Scoring (mirrors app.is_correct / calculate_breakdown / plain_breakdown) ────

def _is_correct(user_answer, correct_answers, question=None) -> bool:
    from utils.question_answer_key import is_answer_correct

    if question is not None:
        return is_answer_correct(user_answer, question)
    return set(user_answer or []) == set(correct_answers or [])


def _plain_breakdown(stats: dict) -> dict:
    out = {}
    for key, value in stats.items():
        total = int(value.get("total", 0))
        correct = int(value.get("correct", 0))
        out[str(key)] = {
            "correct": correct,
            "total": total,
            "percent": round((correct / total) * 100, 2) if total else 0,
        }
    return out


def _breakdown(questions: list, answers: dict, field: str) -> dict:
    stats: dict = {}
    for i, q in enumerate(questions):
        value = q.get(field, "Uncategorized") or "Uncategorized"
        bucket = stats.setdefault(value, {"correct": 0, "total": 0})
        bucket["total"] += 1
        if _is_correct(answers.get(i, []), q.get("answers", []), question=q):
            bucket["correct"] += 1
    return _plain_breakdown(stats)


# ── Immutable snapshot ──────────────────────────────────────────────────────────

def build_submission_snapshot(
    questions: list,
    answers: dict,
    *,
    submitted_at_iso: str,
    exam_name: str,
    language_code: str,
    mode: str,
) -> dict:
    """Return a deep-copied, self-contained snapshot of one exam submission.

    The snapshot is the single source of truth for scoring, persistence, and the
    results UI.  It is deep-copied so later mutation of session_state (answers,
    all_questions) can never change what was scored or what gets saved.
    """
    snap_questions = copy.deepcopy(list(questions or []))
    # answers keys are positional indices; normalize to int-keyed dict of lists.
    snap_answers: dict = {}
    for k, v in dict(answers or {}).items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        snap_answers[idx] = list(v or [])

    total = len(snap_questions)
    correct = sum(
        1 for i, q in enumerate(snap_questions)
        if _is_correct(snap_answers.get(i, []), q.get("answers", []))
    )
    score = round((correct / total) * 100, 2) if total else 0.0

    return {
        "questions": snap_questions,
        "answers": snap_answers,
        "score": score,
        "correct": correct,
        "total": total,
        "domain_breakdown": _breakdown(snap_questions, snap_answers, "category"),
        "difficulty_breakdown": _breakdown(snap_questions, snap_answers, "difficulty"),
        "submitted_at": submitted_at_iso,
        "exam_name": exam_name,
        "language_code": language_code,
        "mode": mode,
    }


def snapshot_question_count(snapshot: Optional[dict]) -> int:
    if not snapshot:
        return 0
    return len(snapshot.get("questions") or [])


def snapshot_distinct_question_ids(snapshot: Optional[dict]) -> int:
    if not snapshot:
        return 0
    ids = {
        q.get("id")
        for q in (snapshot.get("questions") or [])
        if q.get("id") is not None
    }
    return len(ids)


def snapshot_is_persistable(snapshot: Optional[dict]) -> bool:
    """A snapshot can only be persisted if it carries at least one question."""
    return snapshot_question_count(snapshot) > 0


# ── State machine ────────────────────────────────────────────────────────────────

def initial_state() -> str:
    return STATE_IDLE


def plan_persistence(state: Optional[str], retry_requested: bool) -> tuple:
    """Decide whether to run persistence this rerun.

    Returns ``(action, next_state)`` where action is ``"run"`` or ``"skip"``.
    ``next_state`` is the state to set when action == "run" (always SAVING).
    """
    if state == STATE_SAVED:
        return "skip", STATE_SAVED
    if state == STATE_FAILED:
        if retry_requested:
            return "run", STATE_SAVING
        return "skip", STATE_FAILED
    if state in (STATE_IDLE, STATE_SAVING, None):
        return "run", STATE_SAVING
    return "skip", state if state in VALID_STATES else STATE_IDLE


def resolve_final_state(success: bool) -> str:
    return STATE_SAVED if success else STATE_FAILED


def distinct_question_id_count(rows: list) -> int:
    """Distinct question_id count for a list of question_attempts rows."""
    return len({
        r.get("question_id")
        for r in (rows or [])
        if r.get("question_id") is not None
    })
