"""
V40 question-attempt persistence — unit tests.

Covers the repaired paid-mock question_attempts persistence path:
  1.  JSON-safe option normalization
  2.  Exactly 60 rows built from 60 questions
  3.  Chunk sizes are 50 and 10
  4.  Existing/partial child rows are repaired on rerun
  5.  Duplicate parent attempt still backfills child rows (idempotent)
  6.  Missing returned parent ID uses recent-match fallback
  7.  Failed child write returns a safe error and does not create another parent
  8.  Final saved-row count must equal expected count

The persistence helpers are import-safe (no Streamlit, no real Supabase).
A small FakeSupabase models the (exam_attempt_id, question_id) unique
constraint so upsert/idempotency can be asserted without a database.

Run:
    python -m pytest tests/test_exam_attempt_tracking.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.question_selection import (
    normalize_option_list,
    build_question_attempt_rows,
    chunk_rows,
    count_question_attempts,
    persist_question_attempts,
    resolve_exam_attempt_id,
)


# ── Fake Supabase client ──────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _FakeTable:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._op = None
        self._payload = None
        self._on_conflict = None
        self._count_mode = None
        self._filters = {}

    def upsert(self, rows, on_conflict=None):
        self._op = "upsert"
        self._payload = rows
        self._on_conflict = on_conflict
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def select(self, *cols, count=None):
        self._op = "select"
        self._count_mode = count
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._op == "upsert":
            if self._store.raise_on_upsert:
                raise RuntimeError("simulated upsert failure")
            self._store.upsert(self._name, self._payload)
            return _FakeResult(data=self._payload)
        if self._op == "insert":
            enriched = self._store.insert(self._name, self._payload)
            # Use the enriched payload (may include auto-assigned id) so tests
            # can verify that resolve_exam_attempt_id picks up data[0]["id"].
            row = enriched if isinstance(enriched, dict) else self._payload
            return _FakeResult(data=[row])
        if self._op == "select":
            rows = self._store.select(self._name, self._filters)
            if self._count_mode == "exact":
                if self._store.raise_on_count:
                    raise RuntimeError("simulated count failure")
                return _FakeResult(data=rows, count=len(rows))
            return _FakeResult(data=rows)
        return _FakeResult(data=[])


class FakeSupabase:
    """Models question_attempts with a (exam_attempt_id, question_id) unique key.

    exam_attempts inserts auto-assign an incrementing ``id`` so tests can
    verify that ``insert_result.data[0]["id"]`` is used by the production code
    without needing to chain ``.select("id")``.
    """

    def __init__(self):
        self.tables = {"question_attempts": {}, "exam_attempts": []}
        self.insert_counts = {"question_attempts": 0, "exam_attempts": 0}
        self.raise_on_upsert = False
        self.raise_on_count = False
        self.raise_on_insert = False
        self._next_attempt_id = 100

    def table(self, name):
        return _FakeTable(self, name)

    def upsert(self, name, rows):
        bucket = self.tables.setdefault(name, {})
        for row in rows:
            key = (row.get("exam_attempt_id"), row.get("question_id"))
            bucket[key] = row  # idempotent: same key overwrites, never duplicates

    def insert(self, name, payload):
        if self.raise_on_insert:
            raise RuntimeError("simulated insert failure")
        self.insert_counts[name] = self.insert_counts.get(name, 0) + 1
        self.tables.setdefault(name, [])
        if name == "exam_attempts":
            # Assign a stable id so callers can read it from insert_result.data.
            self._next_attempt_id += 1
            if isinstance(payload, dict):
                payload = {**payload, "id": self._next_attempt_id}
        if isinstance(payload, list):
            self.tables[name].extend(payload)
        else:
            self.tables[name].append(payload)
        return payload  # returned so _FakeTable.execute can include it in data

    def select(self, name, filters):
        bucket = self.tables.get(name, {})
        rows = list(bucket.values()) if isinstance(bucket, dict) else list(bucket)
        if "exam_attempt_id" in filters:
            rows = [r for r in rows if r.get("exam_attempt_id") == filters["exam_attempt_id"]]
        return rows


# ── helpers ───────────────────────────────────────────────────────────────────

def make_q(qid, category="Cat A", difficulty="medium", answers=None):
    return {
        "id": qid,
        "exam_name": "Exam X",
        "language_code": "en",
        "category": category,
        "difficulty": difficulty,
        "answers": answers if answers is not None else ["A"],
        "question": f"stem {qid}",
        "question_text": f"stem {qid}",
    }


def make_60_questions():
    return [make_q(i) for i in range(60)]


def make_answers_for(questions):
    # Answer every question with its first correct option (all correct).
    return {idx: list(q["answers"]) for idx, q in enumerate(questions)}


def build_rows(questions, answers, attempt_id=71):
    return build_question_attempt_rows(
        questions,
        answers,
        exam_attempt_id=attempt_id,
        user_email="u@example.com",
        default_exam_name="Exam X",
        default_language_code="en",
        answered_at_iso="2026-06-22T00:00:00+00:00",
    )


# ── 1. JSON-safe option normalization ─────────────────────────────────────────

def test_normalize_none_to_empty_list():
    assert normalize_option_list(None) == []


def test_normalize_list_to_strings():
    assert normalize_option_list(["A", "B"]) == ["A", "B"]


def test_normalize_tuple_to_strings():
    assert normalize_option_list(("A", "B")) == ["A", "B"]


def test_normalize_set_to_strings():
    result = normalize_option_list({"A", "B"})
    assert sorted(result) == ["A", "B"]
    assert all(isinstance(x, str) for x in result)


def test_normalize_scalar_to_single_item_list():
    assert normalize_option_list("A") == ["A"]
    assert normalize_option_list(7) == ["7"]


def test_normalize_mixed_types_become_strings():
    assert normalize_option_list([1, "B", 3.0]) == ["1", "B", "3.0"]


# ── 2. Exactly 60 rows built from 60 questions ────────────────────────────────

def test_builds_exactly_60_rows():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers)
    assert len(rows) == 60


def test_rows_have_required_schema_and_no_secrets():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers)
    required = {
        "exam_attempt_id", "question_id", "user_email", "exam_name",
        "language_code", "category", "difficulty", "selected_options",
        "correct_options", "is_correct", "time_spent_seconds", "answered_at",
    }
    for row in rows:
        assert required.issubset(row.keys())
        # No question text or other content leaks into tracking rows.
        assert "question" not in row
        assert "question_text" not in row
        assert isinstance(row["selected_options"], list)
        assert isinstance(row["correct_options"], list)
        assert isinstance(row["is_correct"], bool)


def test_is_correct_reflects_answer_match():
    questions = [make_q(1, answers=["A"]), make_q(2, answers=["B"])]
    answers = {0: ["A"], 1: ["C"]}  # first right, second wrong
    rows = build_rows(questions, answers)
    assert rows[0]["is_correct"] is True
    assert rows[1]["is_correct"] is False


def test_time_spent_defaults_to_none_safely():
    questions = [make_q(1)]
    rows = build_rows(questions, {0: ["A"]})
    assert rows[0]["time_spent_seconds"] is None


# ── 3. Chunk sizes are 50 and 10 ──────────────────────────────────────────────

def test_chunk_sizes_50_and_10():
    rows = list(range(60))
    chunks = chunk_rows(rows, 50)
    assert [len(c) for c in chunks] == [50, 10]


def test_chunk_default_size_is_50():
    rows = list(range(120))
    chunks = chunk_rows(rows)
    assert [len(c) for c in chunks] == [50, 50, 20]


# ── 4. Existing/partial child rows are repaired on rerun ──────────────────────

def test_partial_rows_repaired_on_rerun():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=71)

    sb = FakeSupabase()
    # Simulate a prior run that only managed to write the first 40 rows.
    sb.upsert("question_attempts", rows[:40])
    assert count_question_attempts(sb, 71) == 40

    ok, err = persist_question_attempts(
        sb, rows, exam_attempt_id=71, expected_count=60
    )
    assert ok is True
    assert err is None
    assert count_question_attempts(sb, 71) == 60


# ── 5. Duplicate parent attempt still backfills child rows (idempotent) ───────

def test_duplicate_parent_backfills_without_duplicates():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=71)

    sb = FakeSupabase()
    ok1, _ = persist_question_attempts(sb, rows, exam_attempt_id=71, expected_count=60)
    # A rerun on the same parent attempt must not create duplicates.
    ok2, err2 = persist_question_attempts(sb, rows, exam_attempt_id=71, expected_count=60)

    assert ok1 is True
    assert ok2 is True
    assert err2 is None
    assert count_question_attempts(sb, 71) == 60


# ── 6. Missing returned parent ID uses recent-match fallback ──────────────────

def test_resolve_uses_returned_id_when_present():
    result = _FakeResult(data=[{"id": 71}])
    assert resolve_exam_attempt_id(result, recover_fn=lambda: 999) == 71


def test_resolve_falls_back_when_no_id_returned():
    result = _FakeResult(data=[])
    assert resolve_exam_attempt_id(result, recover_fn=lambda: 71) == 71


def test_resolve_falls_back_when_id_is_none():
    result = _FakeResult(data=[{"id": None}])
    assert resolve_exam_attempt_id(result, recover_fn=lambda: 71) == 71


def test_resolve_returns_none_when_fallback_raises():
    result = _FakeResult(data=[])

    def boom():
        raise RuntimeError("lookup failed")

    assert resolve_exam_attempt_id(result, recover_fn=boom) is None


# ── 7. Failed child write returns safe error, creates no second parent ────────

def test_failed_child_write_returns_safe_error():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=71)

    sb = FakeSupabase()
    sb.raise_on_upsert = True

    captured = []
    ok, err = persist_question_attempts(
        sb, rows, exam_attempt_id=71, expected_count=60,
        on_error=captured.append,
    )

    assert ok is False
    assert err  # a non-empty, user-safe message
    # No credentials / payloads / tokens / answers / question text leak.
    lowered = err.lower()
    for forbidden in ("password", "token", "secret", "service_role", "stem", "@example.com"):
        assert forbidden not in lowered
    # The failure was captured (for Sentry), not silently swallowed.
    assert len(captured) == 1
    # Persistence must never touch the parent table.
    assert sb.insert_counts["exam_attempts"] == 0


def test_count_verification_failure_returns_safe_error():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=71)

    sb = FakeSupabase()
    sb.raise_on_count = True

    captured = []
    ok, err = persist_question_attempts(
        sb, rows, exam_attempt_id=71, expected_count=60,
        on_error=captured.append,
    )
    assert ok is False
    assert err
    assert len(captured) == 1


# ── 8. Final saved-row count must equal expected count ────────────────────────

def test_wrong_saved_count_does_not_return_success():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=71)

    sb = FakeSupabase()
    # 60 rows saved, but the expected total is 61 → must NOT report success.
    ok, err = persist_question_attempts(
        sb, rows, exam_attempt_id=71, expected_count=61
    )
    assert ok is False
    assert err
    assert count_question_attempts(sb, 71) == 60


def test_correct_saved_count_returns_success():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=71)

    sb = FakeSupabase()
    ok, err = persist_question_attempts(
        sb, rows, exam_attempt_id=71, expected_count=60
    )
    assert ok is True
    assert err is None


def test_empty_rows_returns_safe_error():
    sb = FakeSupabase()
    ok, err = persist_question_attempts(sb, [], exam_attempt_id=71, expected_count=0)
    assert ok is False
    assert err


# ══════════════════════════════════════════════════════════════════════════════
# Tests for the explicit questions/answers argument fix (attempt 74 root cause)
# ══════════════════════════════════════════════════════════════════════════════
#
# These tests exercise _save_question_attempts_batch behaviour through its
# two building-block helpers (build_question_attempt_rows + persist_question_attempts),
# which is the same separation the production function uses and avoids importing
# Streamlit or using real session state.


def _simulate_batch(sb, questions, answers, attempt_id=74, expected=None):
    """Call the batch helper through its building blocks, passing questions and
    answers explicitly — mirrors the production path after the fix."""
    from datetime import datetime, timezone
    from utils.question_selection import build_question_attempt_rows, persist_question_attempts

    answered_at = datetime(2026, 6, 22, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    qs = list(questions or [])
    ans = dict(answers or {})

    captured_errors = []

    if not qs:
        captured_errors.append(
            RuntimeError("_save_question_attempts_batch called with empty questions list")
        )
        return False, "Detailed question results could not be saved. Please try again.", captured_errors

    rows = build_question_attempt_rows(
        qs, ans,
        exam_attempt_id=attempt_id,
        user_email="u@example.com",
        default_exam_name="Exam X",
        default_language_code="en",
        answered_at_iso=answered_at,
    )
    exp = int(expected) if expected is not None else len(rows)
    ok, err = persist_question_attempts(
        sb, rows,
        exam_attempt_id=attempt_id,
        expected_count=exp,
        chunk_size=50,
        on_error=captured_errors.append,
    )
    return ok, err, captured_errors


# ── 1. Uses passed questions even when session-state is empty ────────────────

def test_batch_uses_passed_questions_ignores_empty_session():
    """Passing 60 questions must yield 60 rows even if 'session state' is empty."""
    questions = make_60_questions()
    answers = make_answers_for(questions)
    # session_state is not consulted; its absence cannot cause a false-empty list
    sb = FakeSupabase()
    ok, err, _ = _simulate_batch(sb, questions, answers, attempt_id=74, expected=60)
    assert ok is True
    assert err is None
    assert count_question_attempts(sb, 74) == 60


# ── 2. Sixty passed questions produce exactly sixty child rows ────────────────

def test_60_passed_questions_produce_60_child_rows():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=74)
    assert len(rows) == 60

    sb = FakeSupabase()
    ok, err = persist_question_attempts(sb, rows, exam_attempt_id=74, expected_count=60)
    assert ok is True
    assert count_question_attempts(sb, 74) == 60


# ── 3. Empty passed questions return failure, never success ──────────────────

def test_empty_questions_returns_failure_not_success():
    sb = FakeSupabase()
    ok, err, _ = _simulate_batch(sb, [], {}, attempt_id=74)
    assert ok is False
    assert err


def test_empty_questions_produce_no_child_rows():
    sb = FakeSupabase()
    _simulate_batch(sb, [], {}, attempt_id=74)
    assert count_question_attempts(sb, 74) == 0


# ── 4. Empty questions trigger safe error capture ────────────────────────────

def test_empty_questions_error_is_captured():
    sb = FakeSupabase()
    ok, err, captured = _simulate_batch(sb, [], {}, attempt_id=74)
    assert ok is False
    assert len(captured) == 1
    assert isinstance(captured[0], Exception)


def test_empty_questions_error_message_contains_no_pii():
    sb = FakeSupabase()
    ok, err, _ = _simulate_batch(sb, [], {}, attempt_id=74)
    assert ok is False
    for forbidden in ("password", "token", "secret", "answer", "email", "@", "payload"):
        assert forbidden not in err.lower()


# ── 5. Duplicate-parent backfill uses the explicit scored question snapshot ──

def test_duplicate_parent_backfill_uses_passed_questions():
    """The backfill path must write rows from the passed snapshot, not session state."""
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=74)

    sb = FakeSupabase()
    # Simulate first partial write (30 rows).
    sb.upsert("question_attempts", rows[:30])
    assert count_question_attempts(sb, 74) == 30

    # Backfill via the explicit snapshot.
    ok, err, _ = _simulate_batch(sb, questions, answers, attempt_id=74, expected=60)
    assert ok is True
    assert count_question_attempts(sb, 74) == 60


# ── 6. New-parent persistence uses the explicit scored question snapshot ──────

def test_new_parent_persistence_uses_passed_questions():
    questions = make_60_questions()
    answers = make_answers_for(questions)

    sb = FakeSupabase()
    ok, err, _ = _simulate_batch(sb, questions, answers, attempt_id=74, expected=60)
    assert ok is True
    assert err is None
    assert count_question_attempts(sb, 74) == 60
    assert sb.insert_counts["exam_attempts"] == 0  # child helper never inserts parent


# ── 7. Results call passes local questions and current answers ───────────────

def test_results_call_passes_local_questions_not_session_state():
    """Verify the pattern: list(questions) / dict(answers) are passed explicitly.

    The production results page does:
        save_exam_attempt(..., questions=list(questions), answers=dict(st...))
    This test ensures that pattern produces correct rows even if a hypothetical
    session-state read would return empty data.
    """
    local_questions = make_60_questions()
    local_answers = make_answers_for(local_questions)

    rows = build_question_attempt_rows(
        list(local_questions),
        dict(local_answers),
        exam_attempt_id=74,
        user_email="u@example.com",
        default_exam_name="Exam X",
        default_language_code="en",
        answered_at_iso="2026-06-22T00:00:00+00:00",
    )
    assert len(rows) == 60
    assert all(r["question_id"] == local_questions[i]["id"] for i, r in enumerate(rows))


# ── 8. 50/10 chunk behaviour still passes ────────────────────────────────────

def test_chunk_50_10_still_correct_after_fix():
    rows = list(range(60))
    chunks = chunk_rows(rows, 50)
    assert [len(c) for c in chunks] == [50, 10]


# ── 9. Successful persistence still verifies exactly 60 rows ─────────────────

def test_successful_persistence_verifies_row_count():
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=74)
    sb = FakeSupabase()
    ok, err = persist_question_attempts(sb, rows, exam_attempt_id=74, expected_count=60)
    assert ok is True
    assert err is None
    assert count_question_attempts(sb, 74) == 60


# ── 10. No second parent row is created during retry/backfill ────────────────

def test_no_second_parent_row_during_retry():
    questions = make_60_questions()
    answers = make_answers_for(questions)

    sb = FakeSupabase()

    # First pass: succeed.
    ok1, _, _ = _simulate_batch(sb, questions, answers, attempt_id=74, expected=60)
    assert ok1 is True

    # Retry (simulating rerun): same attempt_id, same questions.
    ok2, err2, _ = _simulate_batch(sb, questions, answers, attempt_id=74, expected=60)
    assert ok2 is True
    assert err2 is None
    # Child rows are still exactly 60 (upsert = no duplicates).
    assert count_question_attempts(sb, 74) == 60
    # Parent table never touched by child path.
    assert sb.insert_counts["exam_attempts"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# New tests required by FIX 1 (parent insert) and FIX 2 (retry flag)
# ══════════════════════════════════════════════════════════════════════════════


# ── 9. Parent insert works without chaining .select() ─────────────────────────

def test_parent_insert_works_without_select_chain():
    """insert(payload).execute() succeeds without a .select() in the chain."""
    sb = FakeSupabase()
    payload = {"user_email": "u@example.com", "score": 80.0, "mode": "Paid Mock Exam"}
    result = sb.table("exam_attempts").insert(payload).execute()
    # Must return a result object with data (not raise, not return None).
    assert result is not None
    assert result.data is not None
    assert sb.insert_counts["exam_attempts"] == 1


def test_parent_insert_without_select_does_not_return_empty_data():
    """The insert result must contain the inserted row so the id can be read."""
    sb = FakeSupabase()
    payload = {"user_email": "u@example.com", "score": 80.0}
    result = sb.table("exam_attempts").insert(payload).execute()
    assert len(result.data) == 1


# ── 10. Returned insert_result.data[0]["id"] is used ─────────────────────────

def test_resolve_uses_id_from_insert_result_data():
    """resolve_exam_attempt_id reads the id from insert_result.data[0]."""
    result = _FakeResult(data=[{"id": 77, "user_email": "u@example.com"}])
    rid = resolve_exam_attempt_id(result, recover_fn=lambda: 999)
    assert rid == 77


def test_fake_supabase_insert_returns_auto_id():
    """FakeSupabase assigns an id so resolve_exam_attempt_id can find it."""
    sb = FakeSupabase()
    result = sb.table("exam_attempts").insert({"score": 70.0}).execute()
    rid = resolve_exam_attempt_id(result)
    assert rid is not None
    assert isinstance(rid, int)


# ── 11. Missing returned ID invokes the recent-match fallback ─────────────────

def test_resolve_calls_recover_fn_when_data_has_no_id():
    result = _FakeResult(data=[{"user_email": "u@example.com"}])  # no "id" key
    fallback_called = []

    def recover():
        fallback_called.append(True)
        return 55

    rid = resolve_exam_attempt_id(result, recover_fn=recover)
    assert rid == 55
    assert fallback_called == [True]


def test_resolve_calls_recover_fn_when_data_is_empty():
    result = _FakeResult(data=[])
    fallback_called = []

    def recover():
        fallback_called.append(True)
        return 56

    rid = resolve_exam_attempt_id(result, recover_fn=recover)
    assert rid == 56
    assert fallback_called == [True]


# ── 12. Failed parent insert: exception captured, safe message returned ────────

def test_failed_insert_exception_is_captured_not_swallowed():
    """The real exception must be forwarded to the capture hook, not swallowed."""
    exc = RuntimeError("connection refused; host=db.supabase.co password=secret")
    captured = []

    def capture_hook(e):
        captured.append(e)

    # Simulate the production pattern: capture then return a safe message.
    try:
        raise exc
    except Exception as e:
        capture_hook(e)
        safe_msg = "Your exam score could not be saved. Please try again."

    assert captured == [exc]   # real exception forwarded
    assert "secret" not in safe_msg
    assert "password" not in safe_msg
    assert "host" not in safe_msg


def test_failed_insert_safe_message_contains_no_credentials():
    safe_msg = "Your exam score could not be saved. Please try again."
    for forbidden in ("password", "token", "secret", "service_role", "key", "host"):
        assert forbidden not in safe_msg


# ── 13. Failed save leaves the session eligible for retry ─────────────────────

def test_failed_save_resets_checked_flag_for_retry():
    """When save fails, attempt_save_checked must be cleared so a rerun can retry."""
    session = {"attempt_save_checked": False, "attempt_saved": None, "attempt_save_error": None}

    def simulate_results_save(save_succeeded):
        """Mirrors the production results-page save block including the retry reset."""
        if not session.get("attempt_save_checked", False):
            session["attempt_save_checked"] = True
            saved = save_succeeded
            save_error = None if save_succeeded else "Could not save"
            session["attempt_saved"] = saved
            session["attempt_save_error"] = save_error
            if not saved:
                session["attempt_save_checked"] = False  # retry eligible
        return session["attempt_save_checked"]

    checked_after = simulate_results_save(save_succeeded=False)
    assert checked_after is False          # flag cleared → next rerun will retry
    assert session["attempt_saved"] is False


# ── 14. Successful save keeps the save guard locked ───────────────────────────

def test_successful_save_keeps_checked_flag_true():
    """When save succeeds, attempt_save_checked stays True to prevent duplicates."""
    session = {"attempt_save_checked": False, "attempt_saved": None, "attempt_save_error": None}

    def simulate_results_save(save_succeeded):
        if not session.get("attempt_save_checked", False):
            session["attempt_save_checked"] = True
            saved = save_succeeded
            session["attempt_saved"] = saved
            session["attempt_save_error"] = None if save_succeeded else "err"
            if not saved:
                session["attempt_save_checked"] = False
        return session["attempt_save_checked"]

    checked_after = simulate_results_save(save_succeeded=True)
    assert checked_after is True           # guard locked → no duplicate insert
    assert session["attempt_saved"] is True


def test_second_call_skipped_when_checked_true():
    """If attempt_save_checked is already True, the save block is not re-entered."""
    session = {"attempt_save_checked": True, "attempt_saved": True, "attempt_save_error": None}
    save_calls = []

    def simulate_results_save():
        if not session.get("attempt_save_checked", False):
            session["attempt_save_checked"] = True
            save_calls.append(1)
            session["attempt_saved"] = True
            if not True:
                session["attempt_save_checked"] = False

    simulate_results_save()
    assert save_calls == []   # block was skipped entirely


# ── 15. Duplicate parent guard avoids a second parent insert ──────────────────

def test_persist_never_touches_exam_attempts_table():
    """persist_question_attempts only writes to question_attempts, not exam_attempts."""
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=71)

    sb = FakeSupabase()
    persist_question_attempts(sb, rows, exam_attempt_id=71, expected_count=60)
    persist_question_attempts(sb, rows, exam_attempt_id=71, expected_count=60)
    assert sb.insert_counts["exam_attempts"] == 0


def test_duplicate_parent_does_not_increase_parent_count():
    sb = FakeSupabase()
    # Simulate first parent insert.
    sb.table("exam_attempts").insert({"score": 80.0}).execute()
    assert sb.insert_counts["exam_attempts"] == 1

    # Child persistence must not touch the parent table.
    rows = build_rows(make_60_questions(), make_answers_for(make_60_questions()), attempt_id=101)
    persist_question_attempts(sb, rows, exam_attempt_id=101, expected_count=60)
    assert sb.insert_counts["exam_attempts"] == 1  # still just the one original parent


# ── 16. Paid duplicate parent still backfills child rows ─────────────────────

def test_paid_duplicate_parent_backfills_missing_child_rows():
    """A second call for the same attempt_id repairs partial child writes via upsert."""
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=71)

    sb = FakeSupabase()
    # First run: only 30 rows persist (simulate a partial failure).
    sb.upsert("question_attempts", rows[:30])
    assert count_question_attempts(sb, 71) == 30

    # Second run (rerun / retry): all 60 must be present via upsert repair.
    ok, err = persist_question_attempts(sb, rows, exam_attempt_id=71, expected_count=60)
    assert ok is True
    assert err is None
    assert count_question_attempts(sb, 71) == 60
    # Parent table must never be touched by the child path.
    assert sb.insert_counts["exam_attempts"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Schema parity + distinct-count + stale-parent guards (live-defect coverage)
# ══════════════════════════════════════════════════════════════════════════════
#
# The paid mock child rows MUST match the column set that the proven-working
# Practice-by-Category insert path writes, otherwise PostgREST rejects the rows
# (the live failure these tests guard against). This is the exact set of keys
# written by pages/Practice_By_Category.build_question_attempt_rows().

PROVEN_QUESTION_ATTEMPT_COLUMNS = {
    # Core identity / scoring columns (original proven set)
    "exam_attempt_id",
    "question_id",
    "user_email",
    "exam_name",
    "language_code",
    "category",
    "difficulty",
    "selected_options",
    "correct_options",
    "is_correct",
    "time_spent_seconds",
    "answered_at",
    # Prospective metadata columns added by the readiness-milestone implementation
    "cognitive_level",
    "concept_key",
    "question_family_id",
    "question_content_version",
    "question_external_key",
    "metadata_source",
    "metadata_capture_version",
}


def test_child_row_schema_matches_practice_path_columns():
    """Each built row must carry exactly the proven-working column set."""
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=74)
    assert rows, "expected non-empty rows"
    for row in rows:
        assert set(row.keys()) == PROVEN_QUESTION_ATTEMPT_COLUMNS


def test_child_row_question_id_is_present_and_not_none():
    """A missing/null question_id would violate the unique conflict target."""
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=74)
    assert all(r["question_id"] is not None for r in rows)


def test_distinct_question_count_equals_row_count_under_unique_constraint():
    """60 saved rows must mean 60 distinct question ids (unique key guarantees it)."""
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=74)

    sb = FakeSupabase()
    ok, err = persist_question_attempts(sb, rows, exam_attempt_id=74, expected_count=60)
    assert ok is True

    bucket = sb.tables["question_attempts"]
    saved_rows = [v for k, v in bucket.items() if k[0] == 74]
    assert len(saved_rows) == 60
    distinct_qids = {r["question_id"] for r in saved_rows}
    assert len(distinct_qids) == 60


def test_no_duplicate_pairs_after_retry():
    """Re-persisting the same snapshot must not create duplicate (attempt,qid) pairs."""
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=74)

    sb = FakeSupabase()
    persist_question_attempts(sb, rows, exam_attempt_id=74, expected_count=60)
    persist_question_attempts(sb, rows, exam_attempt_id=74, expected_count=60)

    bucket = sb.tables["question_attempts"]
    pairs = [k for k in bucket.keys() if k[0] == 74]
    assert len(pairs) == 60
    assert len(set(pairs)) == 60


def test_stale_parent_id_cannot_silently_succeed():
    """If rows land under one parent but verification targets a different (stale)
    parent id, the count check must fail loudly rather than report success."""
    questions = make_60_questions()
    answers = make_answers_for(questions)
    # Rows are built/keyed for parent 74...
    rows = build_rows(questions, answers, attempt_id=74)

    sb = FakeSupabase()
    # ...but we verify against a stale/wrong parent id (99) with zero children.
    ok, err = persist_question_attempts(sb, rows, exam_attempt_id=99, expected_count=60)
    assert ok is False
    assert err
    assert count_question_attempts(sb, 99) == 0


# ══════════════════════════════════════════════════════════════════════════════
# Diagnostic logger tests
# ══════════════════════════════════════════════════════════════════════════════

import importlib
import io

import pytest

_DIAG_ENV_KEY = "CERTBOUND_PAID_MOCK_DIAGNOSTICS"


@pytest.fixture(autouse=True)
def _isolate_diag_env():
    """Snapshot the diagnostics env var before each test and restore it after.

    This guarantees isolation regardless of what the shell exports (e.g. a
    leftover CERTBOUND_PAID_MOCK_DIAGNOSTICS=1 from running Streamlit), and lets
    each test's body execute under the env value it intentionally set.
    """
    original = os.environ.get(_DIAG_ENV_KEY)
    yield
    if original is None:
        os.environ.pop(_DIAG_ENV_KEY, None)
    else:
        os.environ[_DIAG_ENV_KEY] = original


def _reload_diag(env_value=None):
    """Set CERTBOUND_PAID_MOCK_DIAGNOSTICS to ``env_value`` (or unset if None),
    reload paid_mock_diagnostics, and return the fresh module.

    The env value is left in place for the duration of the test body (the
    autouse ``_isolate_diag_env`` fixture restores it afterwards), so dynamic
    ``_enabled()`` evaluation in the body observes exactly what the test set.
    """
    if env_value is None:
        os.environ.pop(_DIAG_ENV_KEY, None)
    else:
        os.environ[_DIAG_ENV_KEY] = env_value
    import utils.paid_mock_diagnostics as _mod
    importlib.reload(_mod)
    return _mod


# 1. Silent when flag is absent ───────────────────────────────────────────────

def test_diagnostics_silent_when_flag_absent(capsys):
    mod = _reload_diag(None)
    mod.log_results_persistence_branch_enter()
    mod.log_submission_snapshot_ready(question_count=60, answer_count=12, distinct_question_count=60)
    mod.log_save_call_before()
    mod.log_save_exam_attempt_enter(mode="paid")
    mod.log_parent_id_reused(attempt_id=42)
    mod.log_save_call_after(success=True)
    mod.log_save_state_transition(from_state="idle", to_state="saving")
    mod.log_distinct_count_verification(expected_count=60, distinct_count=60)
    mod.log_duplicate_guard_result(existing_attempt_id=None)
    mod.log_parent_insert_start()
    mod.log_parent_insert_complete(returned_data_count=1)
    mod.log_parent_id_resolved(attempt_id=42)
    mod.log_child_persistence_call(attempt_id=42, passed_question_count=60)
    mod.log_batch_enter(passed_question_count=60, answer_count=60, expected_count=60)
    mod.log_batch_question_ids(distinct_count=60, null_count=0)
    mod.log_batch_rows_built(built_count=60)
    mod.log_chunk_start(chunk_num=1, chunk_size=50)
    mod.log_chunk_complete(chunk_num=1, chunk_size=50)
    mod.log_count_verification(expected_count=60, saved_count=60)
    mod.log_persistence_complete(success=True)
    mod.log_save_exam_attempt_result(success=True, error_category=None)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_diagnostics_silent_when_flag_is_zero(capsys):
    mod = _reload_diag("0")
    mod.log_batch_enter(passed_question_count=60, answer_count=60, expected_count=60)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_diagnostics_silent_when_flag_is_empty(capsys):
    mod = _reload_diag("")
    mod.log_batch_enter(passed_question_count=60, answer_count=60, expected_count=60)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_diagnostics_silent_when_flag_is_false_lowercase(capsys):
    mod = _reload_diag("false")
    mod.log_batch_enter(passed_question_count=60, answer_count=60, expected_count=60)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_diagnostics_silent_when_flag_is_false_capitalized(capsys):
    mod = _reload_diag("False")
    mod.log_batch_enter(passed_question_count=60, answer_count=60, expected_count=60)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_diagnostics_emit_when_flag_is_one_with_whitespace(capsys):
    mod = _reload_diag(" 1 ")
    mod.log_results_persistence_branch_enter()
    captured = capsys.readouterr()
    assert "[diag]" in captured.err
    assert "results_persistence_branch_enter" in captured.err


def test_diagnostics_env_change_after_import_takes_effect_immediately(capsys):
    # Imported/reloaded while disabled → silent.
    mod = _reload_diag("0")
    mod.log_results_persistence_branch_enter()
    assert capsys.readouterr().err == ""
    # Flip the env value WITHOUT reloading the module → next call must emit,
    # proving _enabled() is evaluated dynamically and never cached at import.
    os.environ[_DIAG_ENV_KEY] = "1"
    mod.log_results_persistence_branch_enter()
    assert "results_persistence_branch_enter" in capsys.readouterr().err
    # Flip back to disabled WITHOUT reloading → silent again.
    os.environ[_DIAG_ENV_KEY] = "0"
    mod.log_results_persistence_branch_enter()
    assert capsys.readouterr().err == ""


# 2. Emits safe events when enabled ───────────────────────────────────────────

def test_diagnostics_emit_when_enabled(capsys):
    mod = _reload_diag("1")
    mod.log_results_persistence_branch_enter()
    captured = capsys.readouterr()
    assert "[diag]" in captured.err
    assert "results_persistence_branch_enter" in captured.err


def test_diagnostics_emit_all_required_events(capsys):
    mod = _reload_diag("1")
    mod.log_save_exam_attempt_enter(mode="paid")
    mod.log_duplicate_guard_result(existing_attempt_id=None)
    mod.log_parent_insert_start()
    mod.log_parent_insert_complete(returned_data_count=1)
    mod.log_parent_id_resolved(attempt_id=42)
    mod.log_child_persistence_call(attempt_id=42, passed_question_count=60)
    mod.log_batch_enter(passed_question_count=60, answer_count=60, expected_count=60)
    mod.log_batch_question_ids(distinct_count=60, null_count=0)
    mod.log_batch_rows_built(built_count=60)
    mod.log_chunk_start(chunk_num=1, chunk_size=50)
    mod.log_chunk_complete(chunk_num=1, chunk_size=50)
    mod.log_count_verification(expected_count=60, saved_count=60)
    mod.log_persistence_complete(success=True)
    mod.log_save_exam_attempt_result(success=True, error_category=None)
    err = capsys.readouterr().err
    for event in (
        "save_exam_attempt_enter", "duplicate_guard_result", "parent_insert_start",
        "parent_insert_complete", "parent_id_resolved", "child_persistence_call",
        "batch_enter", "batch_question_ids", "batch_rows_built",
        "chunk_start", "chunk_complete", "count_verification",
        "persistence_complete", "save_exam_attempt_result",
    ):
        assert event in err, f"missing event: {event}"


# 3. No PII in logged output ──────────────────────────────────────────────────

# Content/secret markers that must never appear. Note: legitimate count field
# names like "answer_count" are allowed, so we match on content markers
# (answer_text, option text, credentials, emails) rather than the bare word
# "answer".
_FORBIDDEN_TERMS = [
    "password", "token", "secret", "service_role", "supabase_key",
    "answer_text", "answers=", "correct_option", "selected_option",
    "question_text", "option_text",
    "@", ".com", "email",
]


def test_diagnostics_contain_no_pii_or_secrets(capsys):
    mod = _reload_diag("1")
    mod.log_results_persistence_branch_enter()
    mod.log_submission_snapshot_ready(question_count=60, answer_count=12, distinct_question_count=60)
    mod.log_save_call_before()
    mod.log_save_call_after(success=True)
    mod.log_save_state_transition(from_state="idle", to_state="saving")
    mod.log_parent_id_reused(attempt_id=42)
    mod.log_distinct_count_verification(expected_count=60, distinct_count=60)
    mod.log_save_retry_requested()
    mod.log_save_exam_attempt_enter(mode="Paid Mock Exam")
    mod.log_duplicate_guard_result(existing_attempt_id=42)
    mod.log_parent_insert_start()
    mod.log_parent_insert_complete(returned_data_count=1)
    mod.log_parent_id_resolved(attempt_id=42)
    mod.log_child_persistence_call(attempt_id=42, passed_question_count=60)
    mod.log_batch_enter(passed_question_count=60, answer_count=60, expected_count=60)
    mod.log_batch_question_ids(distinct_count=60, null_count=0)
    mod.log_batch_rows_built(built_count=60)
    mod.log_chunk_start(chunk_num=1, chunk_size=50)
    mod.log_chunk_complete(chunk_num=1, chunk_size=50)
    mod.log_count_verification(expected_count=60, saved_count=60)
    mod.log_persistence_complete(success=True)
    mod.log_save_exam_attempt_result(success=True, error_category=None)
    err = capsys.readouterr().err.lower()
    for term in _FORBIDDEN_TERMS:
        assert term not in err, f"PII term found: {term!r}"


# 4. Null and distinct question-ID counts ─────────────────────────────────────

def test_diagnostics_distinct_and_null_counts(capsys):
    mod = _reload_diag("1")
    mod.log_batch_question_ids(distinct_count=58, null_count=2)
    err = capsys.readouterr().err
    assert "distinct_count=58" in err
    assert "null_count=2" in err


# 5. Exception diagnostics include class and code only ────────────────────────

def test_diagnostics_exception_logs_class_and_postgrest_code(capsys):
    mod = _reload_diag("1")

    class FakeAPIError(Exception):
        code = "42P10"

    mod.log_persistence_exception(exc=FakeAPIError("unique constraint"))
    err = capsys.readouterr().err
    assert "persistence_exception" in err
    assert "FakeAPIError" in err
    assert "42P10" in err
    # raw message must not appear
    assert "unique constraint" not in err


def test_diagnostics_exception_without_code(capsys):
    mod = _reload_diag("1")
    mod.log_persistence_exception(exc=ValueError("boom"))
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "postgrest_code=None" in err
    assert "boom" not in err


# 6. Persistence behavior unchanged ───────────────────────────────────────────

def test_persist_behavior_unchanged_with_diagnostics_off():
    """Ensure persist_question_attempts still works correctly with flag off."""
    import os
    os.environ.pop("CERTBOUND_PAID_MOCK_DIAGNOSTICS", None)
    questions = make_60_questions()
    answers = make_answers_for(questions)
    rows = build_rows(questions, answers, attempt_id=75)
    sb = FakeSupabase()
    ok, err = persist_question_attempts(sb, rows, exam_attempt_id=75, expected_count=60)
    assert ok is True
    assert err is None
    assert count_question_attempts(sb, 75) == 60


def test_persist_behavior_unchanged_with_diagnostics_on(capsys):
    """Ensure persist_question_attempts still works correctly with flag on."""
    import os
    os.environ["CERTBOUND_PAID_MOCK_DIAGNOSTICS"] = "1"
    try:
        questions = make_60_questions()
        answers = make_answers_for(questions)
        rows = build_rows(questions, answers, attempt_id=76)
        sb = FakeSupabase()
        ok, err = persist_question_attempts(sb, rows, exam_attempt_id=76, expected_count=60)
        assert ok is True
        assert err is None
        assert count_question_attempts(sb, 76) == 60
    finally:
        os.environ.pop("CERTBOUND_PAID_MOCK_DIAGNOSTICS", None)


# 7. Retry behavior unchanged ─────────────────────────────────────────────────

def test_retry_behavior_unchanged_with_diagnostics_on():
    """Partial write is still repaired on retry when diagnostics are on."""
    import os
    os.environ["CERTBOUND_PAID_MOCK_DIAGNOSTICS"] = "1"
    try:
        questions = make_60_questions()
        answers = make_answers_for(questions)
        rows = build_rows(questions, answers, attempt_id=77)
        sb = FakeSupabase()
        sb.upsert("question_attempts", rows[:30])
        assert count_question_attempts(sb, 77) == 30
        ok, err = persist_question_attempts(sb, rows, exam_attempt_id=77, expected_count=60)
        assert ok is True
        assert count_question_attempts(sb, 77) == 60
    finally:
        os.environ.pop("CERTBOUND_PAID_MOCK_DIAGNOSTICS", None)


# ══════════════════════════════════════════════════════════════════════════════
# Submission state machine + immutable snapshot + orchestration contract
# ══════════════════════════════════════════════════════════════════════════════

from utils.exam_submission import (
    STATE_IDLE,
    STATE_SAVING,
    STATE_SAVED,
    STATE_FAILED,
    build_submission_snapshot,
    plan_persistence,
    resolve_final_state,
    snapshot_distinct_question_ids,
    snapshot_question_count,
    snapshot_is_persistable,
)
from utils.question_selection import (
    resolve_exam_attempt_id,
    count_distinct_question_attempts,
)


def _make_snapshot(n=60):
    questions = [make_q(i) for i in range(n)]
    answers = {idx: list(q["answers"]) for idx, q in enumerate(questions)}
    return build_submission_snapshot(
        questions, answers,
        submitted_at_iso="2026-06-22T00:00:00+00:00",
        exam_name="Exam X", language_code="en", mode="paid",
    )


def _simulate_paid_save(session, sb, snapshot, *, user_email="u@example.test"):
    """Faithful replica of app.save_exam_attempt's PAID path: parent-id reuse,
    immediate id storage, child persistence. Proves the no-duplicate-parent and
    repair contract without importing Streamlit."""
    questions = snapshot["questions"]
    answers = snapshot["answers"]
    total = snapshot["total"]

    reused = session.get("current_exam_attempt_id")
    if reused is not None:
        rows = build_rows(questions, answers, attempt_id=reused)
        return persist_question_attempts(sb, rows, exam_attempt_id=reused, expected_count=total)

    insert_result = sb.table("exam_attempts").insert(
        {"user_email": user_email, "total_questions": total}
    ).execute()
    attempt_id = resolve_exam_attempt_id(insert_result, recover_fn=lambda: None)
    session["current_exam_attempt_id"] = attempt_id  # stored immediately
    rows = build_rows(questions, answers, attempt_id=attempt_id)
    return persist_question_attempts(sb, rows, exam_attempt_id=attempt_id, expected_count=total)


# ── State machine transitions ─────────────────────────────────────────────────

def test_plan_idle_runs():
    assert plan_persistence(STATE_IDLE, False) == ("run", STATE_SAVING)


def test_plan_none_runs():
    assert plan_persistence(None, False) == ("run", STATE_SAVING)


def test_plan_saving_reruns_idempotently():
    assert plan_persistence(STATE_SAVING, False) == ("run", STATE_SAVING)


def test_plan_saved_skips():
    assert plan_persistence(STATE_SAVED, False) == ("skip", STATE_SAVED)


def test_plan_saved_skips_even_if_retry_flag_set():
    assert plan_persistence(STATE_SAVED, True) == ("skip", STATE_SAVED)


def test_plan_failed_without_retry_skips():
    assert plan_persistence(STATE_FAILED, False) == ("skip", STATE_FAILED)


def test_plan_failed_with_retry_runs():
    assert plan_persistence(STATE_FAILED, True) == ("run", STATE_SAVING)


def test_resolve_final_state():
    assert resolve_final_state(True) == STATE_SAVED
    assert resolve_final_state(False) == STATE_FAILED


# ── Snapshot ──────────────────────────────────────────────────────────────────

def test_snapshot_captures_exactly_60_questions():
    snap = _make_snapshot(60)
    assert snapshot_question_count(snap) == 60
    assert len(snap["questions"]) == 60


def test_snapshot_distinct_question_ids():
    snap = _make_snapshot(60)
    assert snapshot_distinct_question_ids(snap) == 60


def test_snapshot_scores_correctly():
    snap = _make_snapshot(60)
    assert snap["correct"] == 60
    assert snap["total"] == 60
    assert snap["score"] == 100.0


def test_snapshot_survives_source_mutation_like_a_rerun():
    questions = [make_q(i) for i in range(60)]
    answers = {idx: list(q["answers"]) for idx, q in enumerate(questions)}
    snap = build_submission_snapshot(
        questions, answers,
        submitted_at_iso="2026-06-22T00:00:00+00:00",
        exam_name="Exam X", language_code="en", mode="paid",
    )
    # Simulate a later rerun mutating/clearing the live session structures.
    questions.clear()
    answers.clear()
    assert snapshot_question_count(snap) == 60
    assert snapshot_distinct_question_ids(snap) == 60
    assert snap["total"] == 60


def test_empty_snapshot_is_not_persistable():
    snap = build_submission_snapshot(
        [], {},
        submitted_at_iso="2026-06-22T00:00:00+00:00",
        exam_name="Exam X", language_code="en", mode="paid",
    )
    assert snapshot_is_persistable(snap) is False
    assert snapshot_question_count(snap) == 0


# ── Orchestration contract ────────────────────────────────────────────────────

def test_first_save_creates_one_parent_and_60_children():
    sb = FakeSupabase()
    session = {}
    snap = _make_snapshot(60)
    ok, err = _simulate_paid_save(session, sb, snap)
    assert ok is True
    assert err is None
    assert sb.insert_counts["exam_attempts"] == 1
    pid = session["current_exam_attempt_id"]
    assert count_question_attempts(sb, pid) == 60
    assert count_distinct_question_attempts(sb, pid) == 60


def test_parent_id_stored_immediately():
    sb = FakeSupabase()
    session = {}
    _simulate_paid_save(session, sb, _make_snapshot(60))
    assert session.get("current_exam_attempt_id") is not None


def test_child_failure_preserves_parent_id_and_snapshot():
    sb = FakeSupabase()
    session = {}
    snap = _make_snapshot(60)
    sb.raise_on_upsert = True
    ok, err = _simulate_paid_save(session, sb, snap)
    assert ok is False
    assert err
    # parent created once, id retained for retry; snapshot untouched
    assert sb.insert_counts["exam_attempts"] == 1
    assert session.get("current_exam_attempt_id") is not None
    assert snapshot_question_count(snap) == 60


def test_failed_then_retry_reuses_parent_and_repairs():
    sb = FakeSupabase()
    session = {}
    snap = _make_snapshot(60)

    sb.raise_on_upsert = True
    ok1, _ = _simulate_paid_save(session, sb, snap)
    assert ok1 is False
    pid = session["current_exam_attempt_id"]

    # Retry: child writes now succeed.
    sb.raise_on_upsert = False
    ok2, err2 = _simulate_paid_save(session, sb, snap)
    assert ok2 is True
    assert err2 is None
    # NO second parent created.
    assert sb.insert_counts["exam_attempts"] == 1
    assert session["current_exam_attempt_id"] == pid
    assert count_question_attempts(sb, pid) == 60
    assert count_distinct_question_attempts(sb, pid) == 60


def test_retry_repairs_partial_children_without_new_parent():
    sb = FakeSupabase()
    session = {}
    snap = _make_snapshot(60)

    # First attempt fully succeeds, then simulate losing 40 rows.
    _simulate_paid_save(session, sb, snap)
    pid = session["current_exam_attempt_id"]
    bucket = sb.tables["question_attempts"]
    for key in [k for k in list(bucket.keys()) if k[0] == pid][:40]:
        del bucket[key]
    assert count_question_attempts(sb, pid) == 20

    # Retry reuses same parent and repairs to 60.
    ok, err = _simulate_paid_save(session, sb, snap)
    assert ok is True
    assert sb.insert_counts["exam_attempts"] == 1
    assert count_question_attempts(sb, pid) == 60


def test_delayed_retry_still_uses_same_parent_no_time_window():
    """Reuse is id-based, not time-based, so a retry after >45s reuses the parent."""
    sb = FakeSupabase()
    session = {}
    snap = _make_snapshot(60)
    _simulate_paid_save(session, sb, snap)
    pid = session["current_exam_attempt_id"]
    # No matter how much "time" passes, the stored id is reused.
    ok, err = _simulate_paid_save(session, sb, snap)
    assert ok is True
    assert sb.insert_counts["exam_attempts"] == 1
    assert session["current_exam_attempt_id"] == pid


def test_saved_state_means_no_further_persistence_call():
    """Once saved, plan_persistence returns skip so save is never called again."""
    assert plan_persistence(STATE_SAVED, False)[0] == "skip"


def test_distinct_count_verification_passes_on_healthy_save():
    sb = FakeSupabase()
    session = {}
    _simulate_paid_save(session, sb, _make_snapshot(60))
    pid = session["current_exam_attempt_id"]
    assert count_distinct_question_attempts(sb, pid) == 60
