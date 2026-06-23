"""
Temporary safe diagnostic logger for paid-mock submission tracing.

Enabled ONLY when the environment variable::

    CERTBOUND_PAID_MOCK_DIAGNOSTICS=1

is set.  When absent or set to any other value this module is a complete no-op;
no imports, no IO, no overhead in production.

Safe fields logged (per the spec):
    event name, attempt ID, mode, total question count, passed question count,
    built row count, distinct question-ID count, null question-ID count,
    chunk number, chunk size, saved database row count, exception class,
    PostgREST error code, success/failure boolean.

Never logged:
    user email, selected answers, correct answers, question text,
    answer-option text, Supabase keys, tokens, session values,
    full database payloads.

Output is written to sys.stderr (visible in the Streamlit terminal) as compact
single-line key=value records prefixed with ``[diag]``.
"""

from __future__ import annotations

import os
import sys
from typing import Any


def _enabled() -> bool:
    # Evaluated dynamically on every call (never cached at import) so tests and
    # runtime environment changes take effect immediately. Enabled only when the
    # trimmed value is exactly "1"; absent/""/"0"/"false"/"False" are disabled.
    return (
        str(os.environ.get("CERTBOUND_PAID_MOCK_DIAGNOSTICS", ""))
        .strip()
        == "1"
    )


def _postgrest_code(exc: BaseException) -> str | None:
    """Extract the PostgREST error code from an APIError without touching the
    message, details, hint, or any payload that may contain credentials."""
    return getattr(exc, "code", None)


def _emit(event: str, **fields: Any) -> None:
    """Write one log line to stderr.  Only called when diagnostics are enabled.

    Format: ``[diag] event=<name> key=val key=val ...``
    """
    parts = [f"event={event}"]
    for k, v in fields.items():
        parts.append(f"{k}={v!r}")
    print("[diag] " + " ".join(parts), file=sys.stderr, flush=True)


# ── Public API ─────────────────────────────────────────────────────────────────
# Every function is a no-op when diagnostics are disabled.

def log_results_persistence_branch_enter() -> None:
    if not _enabled():
        return
    _emit("results_persistence_branch_enter")


def log_submission_snapshot_ready(
    *,
    question_count: int,
    answer_count: int,
    distinct_question_count: int,
) -> None:
    if not _enabled():
        return
    _emit(
        "submission_snapshot_ready",
        question_count=question_count,
        answer_count=answer_count,
        distinct_question_count=distinct_question_count,
    )


def log_save_call_before() -> None:
    if not _enabled():
        return
    _emit("save_call_before")


def log_save_call_after(*, success: bool) -> None:
    if not _enabled():
        return
    _emit("save_call_after", success=success)


def log_save_call_exception(*, exc: BaseException) -> None:
    if not _enabled():
        return
    _emit(
        "save_call_exception",
        exc_class=type(exc).__name__,
        postgrest_code=_postgrest_code(exc),
    )


def log_save_retry_requested() -> None:
    if not _enabled():
        return
    _emit("save_retry_requested")


def log_save_state_transition(*, from_state: str, to_state: str) -> None:
    if not _enabled():
        return
    _emit("save_state_transition", from_state=from_state, to_state=to_state)


def log_parent_id_reused(*, attempt_id: Any) -> None:
    if not _enabled():
        return
    _emit("parent_id_reused", attempt_id=attempt_id)


def log_distinct_count_verification(*, expected_count: Any, distinct_count: int) -> None:
    if not _enabled():
        return
    _emit("distinct_count_verification", expected_count=expected_count, distinct_count=distinct_count)


def log_save_exam_attempt_enter(*, mode: str) -> None:
    if not _enabled():
        return
    _emit("save_exam_attempt_enter", mode=mode)


def log_duplicate_guard_result(*, existing_attempt_id: Any) -> None:
    if not _enabled():
        return
    _emit("duplicate_guard_result", existing_attempt_id=existing_attempt_id)


def log_parent_insert_start() -> None:
    if not _enabled():
        return
    _emit("parent_insert_start")


def log_parent_insert_complete(*, returned_data_count: int) -> None:
    if not _enabled():
        return
    _emit("parent_insert_complete", returned_data_count=returned_data_count)


def log_parent_id_resolved(*, attempt_id: Any) -> None:
    if not _enabled():
        return
    _emit("parent_id_resolved", attempt_id=attempt_id)


def log_child_persistence_call(*, attempt_id: Any, passed_question_count: int) -> None:
    if not _enabled():
        return
    _emit("child_persistence_call", attempt_id=attempt_id, passed_question_count=passed_question_count)


def log_save_exam_attempt_result(*, success: bool, error_category: str | None) -> None:
    if not _enabled():
        return
    _emit("save_exam_attempt_result", success=success, error_category=error_category)


def log_batch_enter(
    *,
    passed_question_count: int,
    answer_count: int,
    expected_count: int | None,
) -> None:
    if not _enabled():
        return
    _emit(
        "batch_enter",
        passed_question_count=passed_question_count,
        answer_count=answer_count,
        expected_count=expected_count,
    )


def log_batch_question_ids(*, distinct_count: int, null_count: int) -> None:
    if not _enabled():
        return
    _emit("batch_question_ids", distinct_count=distinct_count, null_count=null_count)


def log_batch_rows_built(*, built_count: int) -> None:
    if not _enabled():
        return
    _emit("batch_rows_built", built_count=built_count)


def log_chunk_start(*, chunk_num: int, chunk_size: int) -> None:
    if not _enabled():
        return
    _emit("chunk_start", chunk_num=chunk_num, chunk_size=chunk_size)


def log_chunk_complete(*, chunk_num: int, chunk_size: int) -> None:
    if not _enabled():
        return
    _emit("chunk_complete", chunk_num=chunk_num, chunk_size=chunk_size)


def log_count_verification(*, expected_count: int | None, saved_count: int) -> None:
    if not _enabled():
        return
    _emit("count_verification", expected_count=expected_count, saved_count=saved_count)


def log_persistence_complete(*, success: bool) -> None:
    if not _enabled():
        return
    _emit("persistence_complete", success=success)


def log_persistence_exception(*, exc: BaseException) -> None:
    if not _enabled():
        return
    _emit(
        "persistence_exception",
        exc_class=type(exc).__name__,
        postgrest_code=_postgrest_code(exc),
    )
