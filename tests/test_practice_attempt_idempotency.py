"""Focused retry-safety / idempotency tests for the practice save paths.

Covers V55-PRACTICE-IDEMPOTENCY-01: ``Practice_By_Category.save_practice_attempt``
and ``Weak_Areas_Practice.save_weak_attempt`` must reuse the same exam_attempts
parent row on retry (never insert a second parent when one is already known)
and must upsert question_attempts children on (exam_attempt_id, question_id)
instead of blind-inserting duplicates.

Also covers V55-PRACTICE-IDEMPOTENCY-03: a stored attempt id must not be
trusted for reuse without verifying that the underlying exam_attempts row
actually belongs to the current user and the expected workflow (mode,
exam_name, language_code). A mismatched or missing row must fall back to
creating a new parent; a verification-query failure must propagate rather
than silently creating a duplicate parent.

Each page's save_* function is extracted from its source via ``ast`` (the same
technique already used by tests/test_daily_sprint_auto_start.py) so this test
does not import the full Streamlit page module -- which calls
st.set_page_config()/render_app_chrome() at import time -- and does not
require a real Supabase/Streamlit environment. The existing FakeSupabase from
tests/test_exam_attempt_tracking.py is reused, extended locally (not modified
in place) with the one lookup shape it does not yet model:
``.table("exam_attempts").select(...).eq("id", ...)``, which the new ownership
verification requires.
"""

from __future__ import annotations

import ast
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_exam_attempt_tracking import FakeSupabase
from utils.question_selection import count_question_attempts, resolve_or_create_exam_attempt_id

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRACTICE_PAGE = os.path.join(ROOT, "pages", "Practice_By_Category.py")
WEAK_PAGE = os.path.join(ROOT, "pages", "Weak_Areas_Practice.py")


class _OwnershipFakeSupabase(FakeSupabase):
    """Extends the shared FakeSupabase (left untouched) so exam_attempts rows
    can be looked up by id -- the shared fake only filters by exam_attempt_id
    (for question_attempts children), not by id (for the parent row itself).
    Defined locally so tests/test_exam_attempt_tracking.py is not modified.
    """

    def __init__(self):
        super().__init__()
        self.raise_on_verify = False

    def select(self, name, filters):
        if name == "exam_attempts" and "id" in filters:
            if self.raise_on_verify:
                raise RuntimeError("simulated ownership-verification query failure")
            rows = self.tables.get(name, [])
            return [r for r in rows if r.get("id") == filters["id"]]
        return super().select(name, filters)


class _SessionState(dict):
    """Minimal stand-in for st.session_state: dict storage + attribute access."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


def _extract_function(path, name):
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module_source = ast.unparse(ast.Module(body=[node], type_ignores=[]))
            namespace = {"datetime": datetime, "timezone": timezone}
            exec(compile(module_source, path, "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in {path}")


def _make_questions(count):
    return [
        {"id": i, "category": "Cat", "difficulty": "medium", "correct_ids": ["a"]}
        for i in range(count)
    ]


def _fake_build_question_attempt_rows(*, exam_attempt_id, user_email, questions, answers):
    """Stand-in row builder: this test verifies parent/child retry-safety
    wiring, not row-content correctness (already covered elsewhere), so a
    minimal deterministic row shape is enough."""
    return [
        {
            "exam_attempt_id": exam_attempt_id,
            "question_id": q["id"],
            "user_email": user_email,
            "is_correct": True,
        }
        for q in (questions or [])
    ]


# ── Practice by Category ──────────────────────────────────────────────────────

def _load_save_practice_attempt(session_state, supabase, user_email="learner@example.test"):
    fn = _extract_function(PRACTICE_PAGE, "save_practice_attempt")
    fn.__globals__["st"] = types.SimpleNamespace(session_state=session_state)
    fn.__globals__["get_current_user_email"] = lambda: user_email
    fn.__globals__["get_supabase_client"] = lambda: supabase
    fn.__globals__["build_question_attempt_rows"] = _fake_build_question_attempt_rows
    return fn


def test_practice_normal_save_creates_exactly_one_parent():
    session_state = _SessionState(practice_questions=_make_questions(5), practice_answers={})
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_practice_attempt(session_state, sb)

    save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")

    assert sb.insert_counts["exam_attempts"] == 1
    assert session_state.get("practice_exam_attempt_id") is not None


def test_practice_normal_save_persists_expected_child_rows():
    session_state = _SessionState(practice_questions=_make_questions(5), practice_answers={})
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_practice_attempt(session_state, sb)

    save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")

    pid = session_state.get("practice_exam_attempt_id")
    assert count_question_attempts(sb, pid) == 5


def test_practice_retry_after_child_failure_reuses_same_parent():
    session_state = _SessionState(practice_questions=_make_questions(5), practice_answers={})
    sb = _OwnershipFakeSupabase()
    sb.raise_on_upsert = True
    save_fn = _load_save_practice_attempt(session_state, sb)

    raised = False
    try:
        save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")
    except Exception:
        raised = True
    assert raised is True

    # Parent created exactly once; its id is retained for the caller's retry.
    assert sb.insert_counts["exam_attempts"] == 1
    first_id = session_state.get("practice_exam_attempt_id")
    assert first_id is not None

    # Retry: child persistence now succeeds.
    sb.raise_on_upsert = False
    save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")

    assert sb.insert_counts["exam_attempts"] == 1  # still just one parent
    assert session_state.get("practice_exam_attempt_id") == first_id
    assert count_question_attempts(sb, first_id) == 5


def test_practice_saved_flag_not_set_by_save_fn_after_failure():
    """save_practice_attempt itself never touches practice_saved; only the
    page's calling block sets it, and only after a successful call. This
    confirms a failed attempt leaves that guard exactly as the caller left it."""
    session_state = _SessionState(
        practice_questions=_make_questions(5), practice_answers={}, practice_saved=False,
    )
    sb = _OwnershipFakeSupabase()
    sb.raise_on_upsert = True
    save_fn = _load_save_practice_attempt(session_state, sb)

    try:
        save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")
    except Exception:
        pass

    assert session_state.get("practice_saved") is False


def test_practice_attempt_id_available_for_retry_after_failure():
    session_state = _SessionState(practice_questions=_make_questions(5), practice_answers={})
    sb = _OwnershipFakeSupabase()
    sb.raise_on_upsert = True
    save_fn = _load_save_practice_attempt(session_state, sb)

    try:
        save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")
    except Exception:
        pass

    assert session_state.get("practice_exam_attempt_id") is not None


def test_practice_no_duplicate_child_rows_across_redundant_retries():
    session_state = _SessionState(practice_questions=_make_questions(5), practice_answers={})
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_practice_attempt(session_state, sb)

    save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")
    pid = session_state.get("practice_exam_attempt_id")

    # A redundant extra call for the same submitted session (attempt id still
    # stored) must not create a second parent or duplicate child rows.
    save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")

    assert sb.insert_counts["exam_attempts"] == 1
    assert count_question_attempts(sb, pid) == 5


def test_practice_new_session_after_reset_can_create_a_new_parent():
    """A brand-new practice session (attempt id cleared by
    initialize_practice_session/reset_practice) must be free to create its own
    new parent row, independent of a prior session's id."""
    session_state = _SessionState(practice_questions=_make_questions(5), practice_answers={})
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_practice_attempt(session_state, sb)
    save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")
    first_id = session_state.get("practice_exam_attempt_id")

    session_state["practice_exam_attempt_id"] = None
    session_state["practice_questions"] = _make_questions(3)

    save_fn(60.0, 2, 3, "Cat", {}, {}, "Exam X", "en")
    second_id = session_state.get("practice_exam_attempt_id")

    assert second_id is not None
    assert second_id != first_id
    assert sb.insert_counts["exam_attempts"] == 2


def test_practice_cross_user_sessions_do_not_share_a_parent():
    sb = _OwnershipFakeSupabase()

    session_a = _SessionState(practice_questions=_make_questions(4), practice_answers={})
    save_a = _load_save_practice_attempt(session_a, sb, user_email="alice@example.test")
    save_a(75.0, 3, 4, "Cat", {}, {}, "Exam X", "en")
    id_a = session_a.get("practice_exam_attempt_id")

    session_b = _SessionState(practice_questions=_make_questions(4), practice_answers={})
    save_b = _load_save_practice_attempt(session_b, sb, user_email="bob@example.test")
    save_b(50.0, 2, 4, "Cat", {}, {}, "Exam X", "en")
    id_b = session_b.get("practice_exam_attempt_id")

    assert id_a != id_b
    assert sb.insert_counts["exam_attempts"] == 2


def test_practice_unrelated_attempt_rows_are_not_touched():
    sb = _OwnershipFakeSupabase()
    unrelated_rows = [{"exam_attempt_id": 555, "question_id": 1, "user_email": "other@example.test"}]
    sb.upsert("question_attempts", unrelated_rows)

    session_state = _SessionState(practice_questions=_make_questions(3), practice_answers={})
    save_fn = _load_save_practice_attempt(session_state, sb)
    save_fn(60.0, 2, 3, "Cat", {}, {}, "Exam X", "en")

    assert count_question_attempts(sb, 555) == 1  # untouched


# ── Weak Areas Practice ────────────────────────────────────────────────────────

def _load_save_weak_attempt(session_state, supabase, user_email="learner@example.test"):
    fn = _extract_function(WEAK_PAGE, "save_weak_attempt")
    fn.__globals__["st"] = types.SimpleNamespace(session_state=session_state)
    fn.__globals__["get_current_user_email"] = lambda: user_email
    fn.__globals__["get_supabase_client"] = lambda: supabase
    fn.__globals__["build_question_attempt_rows"] = _fake_build_question_attempt_rows
    return fn


def test_weak_normal_save_creates_exactly_one_parent():
    session_state = _SessionState(weak_questions=_make_questions(5), weak_answers={})
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_weak_attempt(session_state, sb)

    save_fn(80.0, 4, 5, "Weak Areas", {}, {}, "Exam X", "en")

    assert sb.insert_counts["exam_attempts"] == 1
    assert session_state.get("weak_exam_attempt_id") is not None


def test_weak_normal_save_persists_expected_child_rows():
    session_state = _SessionState(weak_questions=_make_questions(5), weak_answers={})
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_weak_attempt(session_state, sb)

    save_fn(80.0, 4, 5, "Weak Areas", {}, {}, "Exam X", "en")

    pid = session_state.get("weak_exam_attempt_id")
    assert count_question_attempts(sb, pid) == 5


def test_weak_retry_after_child_failure_reuses_same_parent():
    session_state = _SessionState(weak_questions=_make_questions(5), weak_answers={})
    sb = _OwnershipFakeSupabase()
    sb.raise_on_upsert = True
    save_fn = _load_save_weak_attempt(session_state, sb)

    raised = False
    try:
        save_fn(80.0, 4, 5, "Weak Areas", {}, {}, "Exam X", "en")
    except Exception:
        raised = True
    assert raised is True

    assert sb.insert_counts["exam_attempts"] == 1
    first_id = session_state.get("weak_exam_attempt_id")
    assert first_id is not None

    sb.raise_on_upsert = False
    save_fn(80.0, 4, 5, "Weak Areas", {}, {}, "Exam X", "en")

    assert sb.insert_counts["exam_attempts"] == 1
    assert session_state.get("weak_exam_attempt_id") == first_id
    assert count_question_attempts(sb, first_id) == 5


def test_weak_saved_flag_not_set_by_save_fn_after_failure():
    session_state = _SessionState(
        weak_questions=_make_questions(5), weak_answers={}, weak_saved=False,
    )
    sb = _OwnershipFakeSupabase()
    sb.raise_on_upsert = True
    save_fn = _load_save_weak_attempt(session_state, sb)

    try:
        save_fn(80.0, 4, 5, "Weak Areas", {}, {}, "Exam X", "en")
    except Exception:
        pass

    assert session_state.get("weak_saved") is False


def test_weak_attempt_id_available_for_retry_after_failure():
    session_state = _SessionState(weak_questions=_make_questions(5), weak_answers={})
    sb = _OwnershipFakeSupabase()
    sb.raise_on_upsert = True
    save_fn = _load_save_weak_attempt(session_state, sb)

    try:
        save_fn(80.0, 4, 5, "Weak Areas", {}, {}, "Exam X", "en")
    except Exception:
        pass

    assert session_state.get("weak_exam_attempt_id") is not None


def test_weak_no_duplicate_child_rows_across_redundant_retries():
    session_state = _SessionState(weak_questions=_make_questions(5), weak_answers={})
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_weak_attempt(session_state, sb)

    save_fn(80.0, 4, 5, "Weak Areas", {}, {}, "Exam X", "en")
    pid = session_state.get("weak_exam_attempt_id")

    save_fn(80.0, 4, 5, "Weak Areas", {}, {}, "Exam X", "en")

    assert sb.insert_counts["exam_attempts"] == 1
    assert count_question_attempts(sb, pid) == 5


def test_weak_new_session_after_reset_can_create_a_new_parent():
    session_state = _SessionState(weak_questions=_make_questions(5), weak_answers={})
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_weak_attempt(session_state, sb)
    save_fn(80.0, 4, 5, "Weak Areas", {}, {}, "Exam X", "en")
    first_id = session_state.get("weak_exam_attempt_id")

    session_state["weak_exam_attempt_id"] = None
    session_state["weak_questions"] = _make_questions(3)

    save_fn(60.0, 2, 3, "Weak Areas", {}, {}, "Exam X", "en")
    second_id = session_state.get("weak_exam_attempt_id")

    assert second_id is not None
    assert second_id != first_id
    assert sb.insert_counts["exam_attempts"] == 2


def test_weak_unrelated_attempt_rows_are_not_touched():
    sb = _OwnershipFakeSupabase()
    unrelated_rows = [{"exam_attempt_id": 777, "question_id": 1, "user_email": "other@example.test"}]
    sb.upsert("question_attempts", unrelated_rows)

    session_state = _SessionState(weak_questions=_make_questions(3), weak_answers={})
    save_fn = _load_save_weak_attempt(session_state, sb)
    save_fn(60.0, 2, 3, "Weak Areas", {}, {}, "Exam X", "en")

    assert count_question_attempts(sb, 777) == 1  # untouched


# ── Cross-mode isolation ───────────────────────────────────────────────────────

def test_practice_and_weak_and_paid_mock_use_independent_session_keys():
    """Practice, Weak Areas, and the paid mock exam path each store their
    resolved attempt id under a distinct session_state key, so none of them
    can accidentally reuse another mode's stored parent id."""
    session_state = _SessionState(
        current_exam_attempt_id=9001,  # paid mock's key, pre-populated
        practice_exam_attempt_id=None,
        weak_exam_attempt_id=None,
        practice_questions=_make_questions(2),
        practice_answers={},
    )
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_practice_attempt(session_state, sb)
    save_fn(50.0, 1, 2, "Cat", {}, {}, "Exam X", "en")

    # Practice must have created (and stored) its own new parent id, not
    # reused the paid mock's pre-existing 9001.
    assert session_state.get("practice_exam_attempt_id") != 9001
    assert session_state.get("current_exam_attempt_id") == 9001  # untouched


def test_weak_does_not_reuse_practice_attempt_id():
    session_state = _SessionState(
        practice_exam_attempt_id=4242,  # a different mode's key, pre-populated
        weak_exam_attempt_id=None,
        weak_questions=_make_questions(2),
        weak_answers={},
    )
    sb = _OwnershipFakeSupabase()
    save_fn = _load_save_weak_attempt(session_state, sb)
    save_fn(50.0, 1, 2, "Weak Areas", {}, {}, "Exam X", "en")

    assert session_state.get("weak_exam_attempt_id") != 4242
    assert session_state.get("practice_exam_attempt_id") == 4242  # untouched


# ── resolve_or_create_exam_attempt_id: ownership/mode verification ────────────
# V55-PRACTICE-IDEMPOTENCY-03: a stored attempt id must not be trusted for
# reuse without verifying it actually belongs to the current user/workflow.

_EXPECTED = dict(
    expected_user_email="alice@example.test",
    expected_mode="Practice by Category",
    expected_exam_name="Exam X",
    expected_language_code="en",
)


def _insert_attempt_row(sb, **overrides):
    row = {
        "user_email": "alice@example.test",
        "mode": "Practice by Category",
        "exam_name": "Exam X",
        "language_code": "en",
    }
    row.update(overrides)
    result = sb.table("exam_attempts").insert(row).execute()
    return result.data[0]["id"]


def test_resolve_reuses_id_when_row_matches_every_expected_field():
    sb = _OwnershipFakeSupabase()
    existing_id = _insert_attempt_row(sb)

    resolved = resolve_or_create_exam_attempt_id(
        sb, {"user_email": "alice@example.test"},
        existing_attempt_id=existing_id, **_EXPECTED,
    )

    assert resolved == existing_id
    assert sb.insert_counts["exam_attempts"] == 1  # only the setup insert


def test_resolve_rejects_reuse_when_owned_by_another_user():
    sb = _OwnershipFakeSupabase()
    existing_id = _insert_attempt_row(sb, user_email="mallory@example.test")

    resolved = resolve_or_create_exam_attempt_id(
        sb, {"user_email": "alice@example.test", "mode": "Practice by Category"},
        existing_attempt_id=existing_id, **_EXPECTED,
    )

    assert resolved != existing_id
    assert sb.insert_counts["exam_attempts"] == 2  # setup insert + new parent
    # The mismatched row itself must never be modified.
    original = [r for r in sb.tables["exam_attempts"] if r["id"] == existing_id][0]
    assert original["user_email"] == "mallory@example.test"


def test_resolve_rejects_reuse_when_mode_differs():
    sb = _OwnershipFakeSupabase()
    existing_id = _insert_attempt_row(sb, mode="Weak Areas Practice")

    resolved = resolve_or_create_exam_attempt_id(
        sb, {"user_email": "alice@example.test", "mode": "Practice by Category"},
        existing_attempt_id=existing_id, **_EXPECTED,
    )

    assert resolved != existing_id
    assert sb.insert_counts["exam_attempts"] == 2


def test_resolve_rejects_reuse_when_exam_name_differs():
    sb = _OwnershipFakeSupabase()
    existing_id = _insert_attempt_row(sb, exam_name="Exam Y")

    resolved = resolve_or_create_exam_attempt_id(
        sb, {"user_email": "alice@example.test", "mode": "Practice by Category"},
        existing_attempt_id=existing_id, **_EXPECTED,
    )

    assert resolved != existing_id
    assert sb.insert_counts["exam_attempts"] == 2


def test_resolve_rejects_reuse_when_language_code_differs():
    sb = _OwnershipFakeSupabase()
    existing_id = _insert_attempt_row(sb, language_code="fr")

    resolved = resolve_or_create_exam_attempt_id(
        sb, {"user_email": "alice@example.test", "mode": "Practice by Category"},
        existing_attempt_id=existing_id, **_EXPECTED,
    )

    assert resolved != existing_id
    assert sb.insert_counts["exam_attempts"] == 2


def test_resolve_creates_new_parent_when_existing_row_is_missing():
    sb = _OwnershipFakeSupabase()
    # No row inserted at all; the stored id refers to nothing (e.g. it was
    # never actually persisted, or the table was somehow emptied).
    resolved = resolve_or_create_exam_attempt_id(
        sb, {"user_email": "alice@example.test", "mode": "Practice by Category"},
        existing_attempt_id=999999, **_EXPECTED,
    )

    assert resolved is not None
    assert resolved != 999999
    assert sb.insert_counts["exam_attempts"] == 1


def test_resolve_propagates_verification_query_failure_without_inserting():
    sb = _OwnershipFakeSupabase()
    existing_id = _insert_attempt_row(sb)
    sb.raise_on_verify = True

    raised = False
    try:
        resolve_or_create_exam_attempt_id(
            sb, {"user_email": "alice@example.test", "mode": "Practice by Category"},
            existing_attempt_id=existing_id, **_EXPECTED,
        )
    except RuntimeError:
        raised = True

    assert raised is True
    # The failure must not be silently treated as "no row" -- no new parent.
    assert sb.insert_counts["exam_attempts"] == 1  # only the setup insert


def test_resolve_inserts_when_no_existing_id_given():
    sb = _OwnershipFakeSupabase()
    resolved = resolve_or_create_exam_attempt_id(
        sb, {"user_email": "alice@example.test", "mode": "Practice by Category"},
        existing_attempt_id=None, **_EXPECTED,
    )
    assert resolved is not None
    assert sb.insert_counts["exam_attempts"] == 1


# ── Integration: callers pass correct expected identity fields ───────────────

def test_practice_rejects_stale_id_owned_by_another_user_and_creates_new_parent():
    """Proves Practice_By_Category.save_practice_attempt actually wires the
    expected_* fields through: a stored id belonging to a different user must
    be rejected, not reused."""
    sb = _OwnershipFakeSupabase()
    stale_id = _insert_attempt_row(
        sb, user_email="mallory@example.test", mode="Practice by Category",
        exam_name="Exam X", language_code="en",
    )

    session_state = _SessionState(
        practice_questions=_make_questions(3), practice_answers={},
        practice_exam_attempt_id=stale_id,
    )
    save_fn = _load_save_practice_attempt(session_state, sb, user_email="alice@example.test")
    save_fn(60.0, 2, 3, "Cat", {}, {}, "Exam X", "en")

    new_id = session_state.get("practice_exam_attempt_id")
    assert new_id != stale_id
    # Children must be attached to the new parent, not to mallory's row.
    assert count_question_attempts(sb, stale_id) == 0
    assert count_question_attempts(sb, new_id) == 3


def test_weak_rejects_stale_id_owned_by_another_user_and_creates_new_parent():
    sb = _OwnershipFakeSupabase()
    stale_id = _insert_attempt_row(
        sb, user_email="mallory@example.test", mode="Weak Areas Practice",
        exam_name="Exam X", language_code="en",
    )

    session_state = _SessionState(
        weak_questions=_make_questions(3), weak_answers={},
        weak_exam_attempt_id=stale_id,
    )
    save_fn = _load_save_weak_attempt(session_state, sb, user_email="alice@example.test")
    save_fn(60.0, 2, 3, "Weak Areas", {}, {}, "Exam X", "en")

    new_id = session_state.get("weak_exam_attempt_id")
    assert new_id != stale_id
    assert count_question_attempts(sb, stale_id) == 0
    assert count_question_attempts(sb, new_id) == 3


def test_practice_retry_after_child_failure_still_reuses_verified_parent():
    """Same-session retry safety must survive the addition of ownership
    verification: a legitimately-owned id is still reused, not replaced."""
    session_state = _SessionState(practice_questions=_make_questions(5), practice_answers={})
    sb = _OwnershipFakeSupabase()
    sb.raise_on_upsert = True
    save_fn = _load_save_practice_attempt(session_state, sb, user_email="alice@example.test")

    try:
        save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")
    except Exception:
        pass
    first_id = session_state.get("practice_exam_attempt_id")
    assert first_id is not None

    sb.raise_on_upsert = False
    save_fn(80.0, 4, 5, "Cat", {}, {}, "Exam X", "en")

    assert session_state.get("practice_exam_attempt_id") == first_id
    assert sb.insert_counts["exam_attempts"] == 1
    assert count_question_attempts(sb, first_id) == 5


# ── Logout / account-switch simulation ────────────────────────────────────────

def test_account_switch_on_same_tab_cannot_write_children_under_previous_user():
    """Simulates the exact V55-02 risk window: user A's save leaves a stored
    attempt id behind (e.g. because clear_login_state was somehow bypassed or
    predates this fix), a different user B logs into the same tab, and B's
    save must not attach children to A's parent row."""
    sb = _OwnershipFakeSupabase()
    session_state = _SessionState(practice_questions=_make_questions(4), practice_answers={})

    save_a = _load_save_practice_attempt(session_state, sb, user_email="alice@example.test")
    save_a(75.0, 3, 4, "Cat", {}, {}, "Exam X", "en")
    alice_attempt_id = session_state.get("practice_exam_attempt_id")
    assert count_question_attempts(sb, alice_attempt_id) == 4

    # Simulate an account switch on the same browser tab: only the auth
    # identity changes; practice_exam_attempt_id is deliberately left in
    # session_state to prove the verification guard -- not the logout
    # cleanup -- is what stops the cross-user write.
    session_state["practice_questions"] = _make_questions(2)
    session_state["practice_answers"] = {}
    save_b = _load_save_practice_attempt(session_state, sb, user_email="bob@example.test")
    save_b(50.0, 1, 2, "Cat", {}, {}, "Exam X", "en")
    bob_attempt_id = session_state.get("practice_exam_attempt_id")

    assert bob_attempt_id != alice_attempt_id
    # Alice's parent and children are untouched by Bob's save.
    assert count_question_attempts(sb, alice_attempt_id) == 4
    alice_row = [r for r in sb.tables["exam_attempts"] if r["id"] == alice_attempt_id][0]
    assert alice_row["user_email"] == "alice@example.test"
    # Bob's own children went to his own new parent.
    assert count_question_attempts(sb, bob_attempt_id) == 2


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
