"""Focused security/data-integrity tests for V56-PAID-MOCK-IDEMPOTENCY-02.

Covers the two trust points identified by V56-PAID-MOCK-IDEMPOTENCY-01:

1. ``app.py::save_exam_attempt`` must not blindly reuse
   ``st.session_state["current_exam_attempt_id"]``; it must verify the
   underlying ``exam_attempts`` row belongs to the current user/mode/
   exam_name/language_code before attaching child rows to it. A missing or
   mismatched id must fall through to the existing (unchanged) 45-second
   recovery heuristic and, failing that, the existing parent-insert path. A
   verification-query failure must propagate rather than silently creating a
   second parent.

2. ``app.py::apply_pending_exam_state_if_valid`` restores
   ``current_exam_attempt_id`` from the *unsigned* ``exam_state`` URL query
   parameter. That value must be re-verified the same way before it is ever
   written into session state, so URL tampering cannot attach a learner's
   question_attempts rows to another user's exam_attempts parent. A
   verification-query failure here must fail closed (discard the id) rather
   than raising into the middle of page-render restoration or accepting the
   value unverified.

Both functions are extracted from app.py source via ``ast`` (the same
technique already used by tests/test_daily_sprint_auto_start.py and
tests/test_practice_attempt_idempotency.py) so this test does not import the
full Streamlit app module -- which calls st.set_page_config() and touches a
real Supabase/auth environment at import time.

The shared FakeSupabase from tests/test_exam_attempt_tracking.py is reused,
extended locally (not modified in place) with the two exam_attempts lookup
shapes it does not yet model: ``.eq("id", ...)`` (ownership verification) and
``.gte("completed_at", ...)`` plus the full equality filter set (the
unchanged 45-second duplicate-guard heuristic).
"""

from __future__ import annotations

import ast
import math
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_exam_attempt_tracking import FakeSupabase, _FakeTable, make_60_questions, make_answers_for
from utils.activity_modes import FREE_MOCK_EXAM, PAID_MOCK_EXAM

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PAGE = os.path.join(ROOT, "app.py")

DEFAULT_USER_EMAIL = "learner@example.test"
DEFAULT_EXAM_NAME = "Exam X"
DEFAULT_LANGUAGE_CODE = "en"
PAID_MODE = PAID_MOCK_EXAM


# ── Shared fakes ───────────────────────────────────────────────────────────────

class _PaidMockFakeTable(_FakeTable):
    """Adds .gte(), which app.py's unchanged 45-second heuristic uses and the
    shared _FakeTable does not implement."""

    def gte(self, col, val):
        self._filters[f"{col}__gte"] = val
        return self


class _PaidMockFakeSupabase(FakeSupabase):
    """Extends the shared FakeSupabase (left untouched) so exam_attempts can
    be looked up either by id (ownership verification) or by the duplicate
    guard's full equality-filter set (the unchanged 45-second heuristic).
    Defined locally so tests/test_exam_attempt_tracking.py is not modified.
    """

    def __init__(self):
        super().__init__()
        self.raise_on_verify = False
        self.verify_calls = []
        self.heuristic_calls = 0

    def table(self, name):
        return _PaidMockFakeTable(self, name)

    def select(self, name, filters):
        if name != "exam_attempts":
            return super().select(name, filters)
        rows = self.tables.get(name, [])
        if "id" in filters:
            self.verify_calls.append(filters["id"])
            if self.raise_on_verify:
                raise RuntimeError("simulated ownership-verification query failure")
            return [r for r in rows if r.get("id") == filters["id"]]
        self.heuristic_calls += 1
        matched = []
        for row in rows:
            ok = True
            for key, val in filters.items():
                if key.endswith("__gte"):
                    continue
                if row.get(key) != val:
                    ok = False
                    break
            if ok:
                matched.append(row)
        return matched


class _SessionState(dict):
    """Minimal stand-in for st.session_state: dict storage + attribute access."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


def _extract_functions(path, names):
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    wanted = set(names)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    found = {n.name for n in nodes}
    missing = wanted - found
    if missing:
        raise AssertionError(f"{missing} not found in {path}")
    module_source = ast.unparse(ast.Module(body=nodes, type_ignores=[]))
    namespace = {
        "datetime": datetime,
        "timezone": timezone,
        "timedelta": timedelta,
        "time": time,
        "math": math,
        "PAID_MOCK_EXAM": PAID_MOCK_EXAM,
        "FREE_MOCK_EXAM": FREE_MOCK_EXAM,
    }
    exec(compile(module_source, path, "exec"), namespace)
    return namespace


# ── Trust point 1: save_exam_attempt session-state reuse ──────────────────────

def _load_save_exam_attempt(
    session_state,
    supabase,
    *,
    user_email=DEFAULT_USER_EMAIL,
    exam_name=DEFAULT_EXAM_NAME,
    language_code=DEFAULT_LANGUAGE_CODE,
):
    ns = _extract_functions(
        APP_PAGE,
        ["save_exam_attempt", "_persist_children_and_report", "_save_question_attempts_batch"],
    )
    ns.update(
        {
            "st": types.SimpleNamespace(session_state=session_state),
            "get_current_user_email": lambda: user_email,
            "get_supabase_client": lambda: supabase,
            "SELECTED_EXAM_NAME": exam_name,
            "SELECTED_LANGUAGE_CODE": language_code,
            "_capture_exception_safe": lambda exc: None,
        }
    )
    return ns["save_exam_attempt"]


def _paid_session(**overrides):
    base = {"exam_access_type": "paid", "question_time_spent": {}}
    base.update(overrides)
    return _SessionState(**base)


def _insert_row(sb, **fields):
    row = {
        "user_email": DEFAULT_USER_EMAIL,
        "mode": PAID_MODE,
        "exam_name": DEFAULT_EXAM_NAME,
        "language_code": DEFAULT_LANGUAGE_CODE,
    }
    row.update(fields)
    sb.tables["exam_attempts"].append(row)
    return row


def _call_save(save_fn, *, questions=None, answers=None, total=60, correct=48):
    questions = questions if questions is not None else make_60_questions()
    answers = answers if answers is not None else make_answers_for(questions)
    return save_fn(80.0, correct, total, {}, {}, questions=questions, answers=answers)


def test_matching_session_state_attempt_id_is_reused():
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500)
    session = _paid_session(current_exam_attempt_id=500)
    save_fn = _load_save_exam_attempt(session, sb)

    ok, err = _call_save(save_fn)

    assert (ok, err) == (True, None)
    assert sb.insert_counts["exam_attempts"] == 0
    assert all(r["exam_attempt_id"] == 500 for r in sb.tables["question_attempts"].values())
    assert session.get("current_exam_attempt_id") == 500


def test_attempt_id_owned_by_another_user_is_rejected():
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500, user_email="attacker@example.test")
    session = _paid_session(current_exam_attempt_id=500)
    save_fn = _load_save_exam_attempt(session, sb)

    ok, err = _call_save(save_fn)

    assert ok is True
    # No children were attached to the other user's row.
    assert not any(r["exam_attempt_id"] == 500 for r in sb.tables["question_attempts"].values())
    # A fresh, correctly-owned parent was created instead.
    assert sb.insert_counts["exam_attempts"] == 1
    new_id = session.get("current_exam_attempt_id")
    assert new_id != 500
    # The mismatched row itself was never touched.
    assert next(r for r in sb.tables["exam_attempts"] if r.get("id") == 500)["user_email"] == "attacker@example.test"


def test_attempt_id_with_another_mode_is_rejected():
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500, mode="Free Mock Exam")
    session = _paid_session(current_exam_attempt_id=500)
    save_fn = _load_save_exam_attempt(session, sb)

    _call_save(save_fn)

    assert not any(r["exam_attempt_id"] == 500 for r in sb.tables["question_attempts"].values())
    assert sb.insert_counts["exam_attempts"] == 1
    assert session.get("current_exam_attempt_id") != 500


def test_attempt_id_with_another_exam_name_is_rejected():
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500, exam_name="Other Exam")
    session = _paid_session(current_exam_attempt_id=500)
    save_fn = _load_save_exam_attempt(session, sb)

    _call_save(save_fn)

    assert not any(r["exam_attempt_id"] == 500 for r in sb.tables["question_attempts"].values())
    assert sb.insert_counts["exam_attempts"] == 1
    assert session.get("current_exam_attempt_id") != 500


def test_attempt_id_with_another_language_code_is_rejected():
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500, language_code="fr")
    session = _paid_session(current_exam_attempt_id=500)
    save_fn = _load_save_exam_attempt(session, sb)

    _call_save(save_fn)

    assert not any(r["exam_attempt_id"] == 500 for r in sb.tables["question_attempts"].values())
    assert sb.insert_counts["exam_attempts"] == 1
    assert session.get("current_exam_attempt_id") != 500


def test_missing_id_falls_through_to_heuristic_and_then_insert():
    sb = _PaidMockFakeSupabase()
    session = _paid_session()  # no current_exam_attempt_id at all
    save_fn = _load_save_exam_attempt(session, sb)

    ok, err = _call_save(save_fn, total=60, correct=48)

    assert ok is True
    assert sb.heuristic_calls == 1  # the existing heuristic ran
    assert sb.insert_counts["exam_attempts"] == 1  # heuristic missed -> new parent
    assert session.get("current_exam_attempt_id") is not None


def test_mismatch_falls_through_to_heuristic_which_hits():
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500, user_email="attacker@example.test")
    recent_completed_at = datetime.now(timezone.utc).isoformat()
    _insert_row(
        sb,
        id=777,
        total_questions=60,
        correct_answers=48,
        completed_at=recent_completed_at,
    )
    session = _paid_session(current_exam_attempt_id=500)
    save_fn = _load_save_exam_attempt(session, sb)

    ok, err = _call_save(save_fn, total=60, correct=48)

    assert ok is True
    assert sb.heuristic_calls == 1
    assert sb.insert_counts["exam_attempts"] == 0  # heuristic hit -> no new parent
    assert session.get("current_exam_attempt_id") == 777
    assert not any(r["exam_attempt_id"] == 500 for r in sb.tables["question_attempts"].values())
    # Mismatched row untouched.
    assert next(r for r in sb.tables["exam_attempts"] if r.get("id") == 500)["user_email"] == "attacker@example.test"


def test_verification_query_failure_propagates_and_skips_heuristic_and_insert():
    sb = _PaidMockFakeSupabase()
    sb.raise_on_verify = True
    _insert_row(sb, id=500)
    session = _paid_session(current_exam_attempt_id=500)
    save_fn = _load_save_exam_attempt(session, sb)

    try:
        _call_save(save_fn)
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True
    assert sb.heuristic_calls == 0
    assert sb.insert_counts["exam_attempts"] == 0


# ── Trust point 3: apply_pending_exam_state_if_valid URL restoration ──────────

def _load_apply_pending_exam_state_if_valid(supabase, *, user_email=DEFAULT_USER_EMAIL):
    ns = _extract_functions(
        APP_PAGE,
        [
            "apply_pending_exam_state_if_valid",
            "_restore_indexed_answers",
            "_restore_choice_orders",
            "_valid_exam_duration_minutes",
        ],
    )
    ns.update(
        {
            "get_current_user_email": lambda: user_email,
            "get_supabase_client": lambda: supabase,
        }
    )
    return ns


def _make_bank():
    return [
        {"id": 1, "options": ["A", "B", "C"]},
        {"id": 2, "options": ["A", "B", "C"]},
    ]


def _make_pending_state(**overrides):
    state = {
        "exam_key": "paid|Exam X|en",
        "question_ids": [1, 2],
        "started": True,
        "submitted": False,
        "review_mode": False,
        "current_question": 0,
        "start_time": time.time(),
        "randomize_choices": True,
        "exam_access_type": "paid",
        "answers": {},
        "marked": [],
        "choice_orders": {},
        "attempt_id": 500,
        "save_state": "saved",
        "time_limit_minutes": 105,
    }
    state.update(overrides)
    return state


def _restore(sb, state, *, user_email=DEFAULT_USER_EMAIL, exam_name=DEFAULT_EXAM_NAME, language_code=DEFAULT_LANGUAGE_CODE):
    ns = _load_apply_pending_exam_state_if_valid(sb, user_email=user_email)
    session = _SessionState(_pending_exam_state=state)
    ns["apply_pending_exam_state_if_valid"].__globals__["st"] = types.SimpleNamespace(session_state=session)
    restored = ns["apply_pending_exam_state_if_valid"](
        _make_bank(), state["exam_key"], exam_name=exam_name, language_code=language_code,
    )
    return session, restored


def test_matching_url_restored_attempt_id_is_accepted():
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500)
    session, restored = _restore(sb, _make_pending_state())

    assert restored is not None
    assert session.get("current_exam_attempt_id") == 500


def test_tampered_url_attempt_id_owned_by_another_user_is_discarded():
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500, user_email="attacker@example.test")
    session, restored = _restore(sb, _make_pending_state())

    assert restored is not None  # rest of restoration still proceeds
    assert session.get("current_exam_attempt_id") is None
    assert session.get("started") is True


def test_url_attempt_id_with_mismatched_mode_exam_or_language_is_discarded():
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500, mode="Free Mock Exam")
    session, restored = _restore(sb, _make_pending_state())
    assert session.get("current_exam_attempt_id") is None

    sb2 = _PaidMockFakeSupabase()
    _insert_row(sb2, id=500, exam_name="Other Exam")
    session2, _ = _restore(sb2, _make_pending_state())
    assert session2.get("current_exam_attempt_id") is None

    sb3 = _PaidMockFakeSupabase()
    _insert_row(sb3, id=500, language_code="fr")
    session3, _ = _restore(sb3, _make_pending_state())
    assert session3.get("current_exam_attempt_id") is None


def test_discarded_url_attempt_id_never_written_on_query_failure_fail_closed():
    sb = _PaidMockFakeSupabase()
    sb.raise_on_verify = True
    _insert_row(sb, id=500)
    session, restored = _restore(sb, _make_pending_state())

    # Fails closed: no exception escapes, id is simply never accepted, and
    # the rest of restoration still proceeds normally.
    assert restored is not None
    assert session.get("current_exam_attempt_id") is None
    assert session.get("started") is True


def test_discarded_url_attempt_id_never_reaches_child_persistence():
    """End-to-end: a tampered URL attempt_id is discarded on restoration, and
    the subsequent save never attaches any child row to that attacker-owned
    parent -- confirming the discard is not merely cosmetic."""
    sb = _PaidMockFakeSupabase()
    _insert_row(sb, id=500, user_email="attacker@example.test")
    session, restored = _restore(sb, _make_pending_state())
    assert session.get("current_exam_attempt_id") is None

    save_fn = _load_save_exam_attempt(session, sb)
    ok, err = _call_save(save_fn)

    assert ok is True
    assert not any(r["exam_attempt_id"] == 500 for r in sb.tables["question_attempts"].values())
    assert session.get("current_exam_attempt_id") != 500
    assert next(r for r in sb.tables["exam_attempts"] if r.get("id") == 500)["user_email"] == "attacker@example.test"
