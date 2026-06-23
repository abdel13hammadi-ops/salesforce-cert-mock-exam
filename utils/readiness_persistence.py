"""
CertBound readiness-persistence helpers.

No Streamlit dependency.  All heavy computation (readiness formula, DB calls)
is confined to function bodies so this module stays import-safe for tests.

Public API
----------
ATTEMPT_METADATA_V1
    Named constant written as ``metadata_capture_version`` in every
    question_attempts row created by this feature.

build_attempt_metadata(question)
    Pure extractor: returns the seven new metadata fields from the question
    dict as shown to the student.  Never re-queries the database.

fetch_eligible_mock_bank_size(supabase, exam_name, language_code)
    Count eligible mock questions for a cert+language combination.

build_readiness_snapshot_payload(...)
    Pure mapper: calculate_readiness result dict → readiness_snapshots row.

insert_or_fetch_readiness_snapshot(supabase, payload)
    Insert once; on unique conflict, return the existing snapshot as success.
    Historical snapshots are immutable: the existing row is never overwritten.

compute_and_persist_readiness_snapshot(supabase, *, ...)
    End-to-end orchestration: fetch → temporal filter → calculate_readiness
    → insert_or_fetch.  Always call AFTER parent + child rows are verified saved.

Design: historical immutability
    Each readiness snapshot is keyed on (exam_attempt_id, formula_version).
    The snapshot is computed using only exam_attempts completed on or before
    the target attempt's completed_at, with a deterministic id tie-breaker
    when timestamps are equal.  Once written the row is never updated; a
    conflict means the historical data already exists and is the canonical truth.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Tuple

# ── Named constants ─────────────────────────────────────────────────────────────

ATTEMPT_METADATA_V1 = "ATTEMPT_METADATA_V1"
_METADATA_SOURCE = "captured_at_attempt"

# ── Question metadata extraction ────────────────────────────────────────────────


def build_attempt_metadata(question: dict) -> dict:
    """Extract immutable metadata fields from a question dict.

    Uses the question exactly as shown to the student (from the submission
    snapshot or session state).  Never re-queries the database.  All fields
    are nullable; missing or unrecognised values become None so existing
    rows without these columns are not broken.
    """
    def _text_or_none(raw) -> Optional[str]:
        """Strip whitespace; return None when absent or blank."""
        if raw is None:
            return None
        stripped = str(raw).strip()
        return stripped if stripped else None

    # content_version must be int or None; blank strings and non-numerics → None
    cv_raw = question.get("content_version")
    cv: Optional[int] = None
    if cv_raw is not None:
        stripped_cv = str(cv_raw).strip()
        if stripped_cv:
            try:
                cv = int(stripped_cv)
            except (TypeError, ValueError):
                cv = None

    # question_family_id: non-empty string (UUID) or None
    qfid: Optional[str] = _text_or_none(question.get("question_family_id"))

    return {
        "cognitive_level": _text_or_none(question.get("cognitive_level")),
        "concept_key": _text_or_none(question.get("concept_key")),
        "question_family_id": qfid,
        "question_content_version": cv,
        "question_external_key": _text_or_none(question.get("external_key")),
        "metadata_source": _METADATA_SOURCE,
        "metadata_capture_version": ATTEMPT_METADATA_V1,
    }


# ── Eligible bank size ──────────────────────────────────────────────────────────


def extract_captured_bank_size(attempts: list) -> Optional[int]:
    """Return the eligible_question_bank_size from the newest attempt that carries a positive value.

    Expects attempts sorted newest-first (as returned by both pages' attempt fetchers).
    Returns None when no attempt carries a valid positive bank size, which preserves
    the existing question_bank_total fallback behavior inside calculate_readiness.

    Never raises.
    """
    for attempt in (attempts or []):
        raw = attempt.get("eligible_question_bank_size")
        if raw is None:
            continue
        try:
            v = int(raw)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def fetch_eligible_mock_bank_size(
    supabase, exam_name: str, language_code: str
) -> int:
    """Count questions eligible for paid-mock selection for a cert+language.

    Filters applied (same as app.py fetch_question_bank for paid path):
        is_active = True
        is_exam_eligible = True
        mock_eligible = True
        quality_status = 'approved'
        exam_name = <exam_name>
        language_code = <language_code>

    Returns 0 on any error; never raises.
    """
    try:
        result = (
            supabase.table("questions")
            .select("id", count="exact")
            .eq("exam_name", exam_name)
            .eq("language_code", language_code)
            .eq("is_active", True)
            .eq("is_exam_eligible", True)
            .eq("mock_eligible", True)
            .eq("quality_status", "approved")
            .execute()
        )
        count = getattr(result, "count", None)
        if count is not None:
            return int(count)
        return len(getattr(result, "data", None) or [])
    except Exception:
        return 0


# ── Snapshot payload ────────────────────────────────────────────────────────────


def _extract_component_scores(readiness: dict) -> dict:
    """Extract the named scoring components for the component_scores jsonb column.

    Maps keys that are present in every calculate_readiness return dict.
    """
    def _f(key: str, default: float = 0.0) -> float:
        try:
            return float(readiness.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "recent_accuracy": _f("recent_accuracy"),
        "domain_score": _f("domain_score"),
        "domain_robustness": _f("domain_robustness"),
        "consistency_penalty": _f("consistency_penalty"),
        "trend_adjustment": _f("trend_adjustment"),
        "trend_slope": _f("trend_slope"),
        "consistency_standard_deviation": _f("consistency_standard_deviation"),
    }


def build_readiness_snapshot_payload(
    user_email: str,
    exam_name: str,
    exam_attempt_id: Any,
    formula_version: str,
    readiness: dict,
    eligible_bank_size: int,
) -> dict:
    """Map a calculate_readiness result dict to a readiness_snapshots DB row.

    Pure function: no network calls, no Streamlit.
    The returned dict is ready for insert_or_fetch_readiness_snapshot().
    """
    def _f(key: str, default: float = 0.0) -> float:
        try:
            return float(readiness.get(key, default))
        except (TypeError, ValueError):
            return default

    def _i(key: str, default: int = 0) -> int:
        try:
            return int(readiness.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "user_email": str(user_email),
        "exam_name": str(exam_name),
        "exam_attempt_id": exam_attempt_id,
        "formula_version": str(formula_version),
        "score": _f("score"),
        "label": str(readiness.get("label") or ""),
        "confidence_score": _f("confidence_score"),
        "eligible_mock_count": _i("eligible_mock_count"),
        "eligible_question_bank_size": int(eligible_bank_size),
        "component_scores": _extract_component_scores(readiness),
        "snapshot_data": dict(readiness),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Immutable insert ────────────────────────────────────────────────────────────


def insert_or_fetch_readiness_snapshot(
    supabase, payload: dict
) -> Tuple[bool, Optional[str]]:
    """Insert a snapshot once; on conflict return the existing row as success.

    Historical snapshots are immutable.  The unique constraint
    (exam_attempt_id, formula_version) ensures each attempt-formula pair has
    exactly one canonical snapshot.  When a conflict fires it means a prior
    successful insert already exists; we return success without mutating
    ``computed_at``, ``snapshot_data``, ``score``, or ``component_scores``.

    Sequence:
      1. Attempt INSERT.
      2. INSERT succeeds  →  return (True, None).
      3. INSERT fails     →  SELECT the existing row by unique key.
      4. Existing found   →  return (True, None): historical data preserved.
      5. No existing row  →  return (False, error): genuine save failure.

    Returns (ok, error_message).  Never raises.
    """
    insert_exc: Optional[Exception] = None
    try:
        (
            supabase.table("readiness_snapshots")
            .insert(payload)
            .execute()
        )
        return True, None
    except Exception as exc:
        insert_exc = exc

    # INSERT failed — check whether a snapshot already exists for this key.
    # This handles the unique-violation case without requiring specific error codes.
    try:
        existing = (
            supabase.table("readiness_snapshots")
            .select("exam_attempt_id,formula_version")
            .eq("exam_attempt_id", payload.get("exam_attempt_id"))
            .eq("formula_version", payload.get("formula_version"))
            .limit(1)
            .execute()
        )
        if getattr(existing, "data", None):
            # A canonical snapshot already exists — historical data is intact.
            return True, None
    except Exception:
        pass

    # No existing snapshot and INSERT failed — genuine failure.
    try:
        error_msg = str(insert_exc)[:300]
    except Exception:
        error_msg = "unknown"
    return False, f"Readiness snapshot could not be saved: {error_msg}"


# ── End-to-end orchestration ────────────────────────────────────────────────────


def _is_historical_attempt(
    attempt: dict,
    target_completed_at: str,
    exam_attempt_id: Any,
) -> bool:
    """Return True if ``attempt`` was completed on or before the target attempt.

    Uses (completed_at, id) as a compound sort key with a deterministic
    integer tie-breaker when timestamps are equal.  ISO 8601 strings compare
    correctly under lexicographic ordering.
    """
    ts = str(attempt.get("completed_at") or "")
    if ts < target_completed_at:
        return True
    if ts == target_completed_at:
        try:
            return int(attempt.get("id") or 0) <= int(exam_attempt_id or 0)
        except (TypeError, ValueError):
            # UUID or other non-integer id: fall back to string comparison
            return str(attempt.get("id") or "") <= str(exam_attempt_id or "")
    return False


def compute_and_persist_readiness_snapshot(
    supabase,
    *,
    user_email: str,
    exam_name: str,
    exam_attempt_id: Any,
    eligible_bank_size: int,
    on_error=None,
) -> Tuple[bool, Optional[str]]:
    """Compute readiness as of the target attempt and insert the snapshot.

    IMPORTANT: Call this ONLY after the parent exam_attempts row and all
    expected child question_attempts rows have been saved and verified.
    This is a secondary persister: its failure does NOT invalidate the exam
    attempt or the child rows.

    Historical immutability
    -----------------------
    Readiness is computed using only exam_attempts whose (completed_at, id)
    sort key is <= the target attempt's sort key.  This ensures the snapshot
    reflects the exact state of the student's history at the time the target
    attempt was completed, regardless of how many later attempts exist when
    the results page is revisited.

    Steps:
      1. Fetch cert config (passing_score, question_count, time_limit_minutes)
      2. Fetch the target attempt's completed_at for temporal anchoring
      3. Fetch all exam_attempts for user+exam
      4. Filter to attempts <= target attempt's (completed_at, id)
      5. Filter paid-mock-eligible from that historical set
      6. Fetch question_attempts linked to the paid-mock historical set
      7. Fetch certification domain weights
      8. Call calculate_readiness (formula unchanged)
      9. Build snapshot payload
     10. insert_or_fetch — insert once, historical row is never overwritten

    Returns (ok, error_message).  Never raises.
    """
    try:
        from utils.readiness import calculate_readiness, READINESS_VERSION  # noqa: PLC0415

        # Step 1: cert config
        cert: dict = {}
        try:
            cert_result = (
                supabase.table("certifications")
                .select("passing_score,question_count,time_limit_minutes")
                .eq("exam_name", exam_name)
                .limit(1)
                .execute()
            )
            rows = getattr(cert_result, "data", None) or []
            if rows:
                cert = rows[0]
        except Exception:
            pass

        try:
            passing_score = float(cert.get("passing_score") or 68)
        except (TypeError, ValueError):
            passing_score = 68.0
        try:
            expected_q_count = int(cert.get("question_count") or 60) or 60
        except (TypeError, ValueError):
            expected_q_count = 60
        try:
            time_limit = int(cert.get("time_limit_minutes") or 105) or 105
        except (TypeError, ValueError):
            time_limit = 105

        # Step 2: fetch target attempt's completed_at for temporal anchoring
        target_completed_at: Optional[str] = None
        try:
            target_result = (
                supabase.table("exam_attempts")
                .select("id,completed_at")
                .eq("id", exam_attempt_id)
                .limit(1)
                .execute()
            )
            target_rows = getattr(target_result, "data", None) or []
            if target_rows:
                target_completed_at = str(target_rows[0].get("completed_at") or "")
        except Exception:
            pass

        # Step 3: all attempts for user + exam
        attempts_result = (
            supabase.table("exam_attempts")
            .select(
                "id,user_email,mode,score,total_questions,correct_answers,"
                "started_at,completed_at,domain_breakdown,difficulty_breakdown,"
                "exam_name,language_code"
            )
            .ilike("user_email", user_email)
            .eq("exam_name", exam_name)
            .execute()
        )
        all_attempts = getattr(attempts_result, "data", None) or []

        # Step 4: filter to attempts on or before the target attempt.
        # If target_completed_at is unknown, include all attempts as a safe fallback.
        if target_completed_at is not None:
            historical_attempts = [
                a for a in all_attempts
                if _is_historical_attempt(a, target_completed_at, exam_attempt_id)
            ]
        else:
            historical_attempts = list(all_attempts)

        # Step 5: paid-mock-eligible subset of the historical set
        paid_mock_attempts = [
            a for a in historical_attempts
            if str(a.get("mode") or "").strip().lower() == "paid mock exam"
            and int(a.get("total_questions") or 0) >= expected_q_count
        ]

        # Step 6: question_attempts linked to the paid-mock historical set
        eligible_ids: set = {
            str(a.get("id"))
            for a in paid_mock_attempts
            if a.get("id") is not None
        }
        question_attempts: list = []
        if eligible_ids:
            qa_result = (
                supabase.table("question_attempts")
                .select(
                    "exam_attempt_id,question_id,user_email,exam_name,"
                    "language_code,category,difficulty,is_correct,"
                    "time_spent_seconds,answered_at"
                )
                .ilike("user_email", user_email)
                .eq("exam_name", exam_name)
                .execute()
            )
            all_qa = getattr(qa_result, "data", None) or []
            question_attempts = [
                row for row in all_qa
                if str(row.get("exam_attempt_id")) in eligible_ids
            ]

        # Step 7: domain weights
        domain_weights: dict = {}
        try:
            dw_result = (
                supabase.table("certification_domains")
                .select("domain_name,weight")
                .eq("exam_name", exam_name)
                .eq("is_active", True)
                .execute()
            )
            for row in (getattr(dw_result, "data", None) or []):
                name = row.get("domain_name")
                if name:
                    try:
                        domain_weights[str(name)] = float(row.get("weight") or 0)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

        # Step 8: compute readiness (formula unchanged)
        readiness = calculate_readiness(
            attempts=paid_mock_attempts,
            passing_score=passing_score,
            domain_weights=domain_weights,
            expected_question_count=expected_q_count,
            question_bank_total=eligible_bank_size,
            question_attempts=question_attempts,
            time_limit_minutes=time_limit,
        )

        # Step 9: build payload
        payload = build_readiness_snapshot_payload(
            user_email=user_email,
            exam_name=exam_name,
            exam_attempt_id=exam_attempt_id,
            formula_version=READINESS_VERSION,
            readiness=readiness,
            eligible_bank_size=eligible_bank_size,
        )

        # Step 10: insert once; never overwrite historical data on retry
        return insert_or_fetch_readiness_snapshot(supabase, payload)

    except Exception as exc:
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                pass
        return False, "Readiness snapshot could not be computed or saved."
