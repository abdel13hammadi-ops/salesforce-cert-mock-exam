"""
Live diagnostic for paid-mock question_attempts persistence.

Reproduces the EXACT production child-row build + persist path against the real
Supabase database using the installed supabase-py client and the production
helpers in ``utils.question_selection`` â€” WITHOUT importing app.py (no Streamlit
side effects). It creates a temporary diagnostic exam_attempts parent, persists
child rows through the real helper, verifies the real row count and the distinct
question-id count, prints the full raw PostgREST/APIError on failure, and then
deletes every diagnostic row it created in a ``finally`` block.

WHY THIS EXISTS
    Attempts 74/75 saved a parent row but zero question_attempts children. The
    only reliable way to find the real database rejection is to run the real
    persistence helper against the real schema with the real client. Mocked
    tests cannot prove the live PostgREST behaviour of
    ``.upsert(..., on_conflict="exam_attempt_id,question_id")``.

USAGE (PowerShell)
    $env:SUPABASE_URL = "https://<project>.supabase.co"
    $env:SUPABASE_SERVICE_ROLE_KEY = "<service-role-key>"
    python scripts/diagnose_question_attempts.py --count 3
    python scripts/diagnose_question_attempts.py --count 60
    python scripts/diagnose_question_attempts.py --count 60 --exam "Salesforce Certified Business Analyst" --language en

Credentials are read from environment variables first, then from
``.streamlit/secrets.toml`` if present. No credentials, answer payloads, or
question text are printed. Only ids, counts, and the raw exception (locally) are
shown so the real database rejection is visible during a manual run.

EXIT CODE
    0 on full success (rows saved, counts verified, cleanup confirmed).
    1 on any failure (the raw error is printed above the summary).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone

# Make the repo root importable so ``utils`` resolves when run from anywhere.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# EXACT production helpers â€” no duplicated persistence logic.
from utils.question_selection import (  # noqa: E402
    build_question_attempt_rows,
    persist_question_attempts,
    resolve_exam_attempt_id,
)


# â”€â”€ Credentials â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _load_credentials():
    """Return (url, service_role_key) from env, then .streamlit/secrets.toml.

    Never prints the key. Raises SystemExit with a safe message if missing.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        secrets_path = os.path.join(_REPO_ROOT, ".streamlit", "secrets.toml")
        if os.path.exists(secrets_path):
            try:
                import tomllib  # Python 3.11+
                with open(secrets_path, "rb") as fh:
                    data = tomllib.load(fh)
                url = url or data.get("SUPABASE_URL")
                key = key or data.get("SUPABASE_SERVICE_ROLE_KEY")
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[warn] could not parse .streamlit/secrets.toml: {type(exc).__name__}")

    if not url or not key:
        raise SystemExit(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY. "
            "Set them as environment variables or in .streamlit/secrets.toml."
        )
    return url, key


# â”€â”€ Raw error printing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _print_raw_error(prefix, exc):
    """Print the complete raw client/PostgREST error locally (no PII involved).

    supabase-py raises postgrest.exceptions.APIError whose payload carries
    message/details/hint/code â€” exactly what we need to see the real rejection.
    """
    print(f"\n========== RAW ERROR: {prefix} ==========")
    print(f"type   : {type(exc).__module__}.{type(exc).__name__}")
    print(f"repr   : {exc!r}")
    for attr in ("message", "details", "hint", "code"):
        if hasattr(exc, attr):
            print(f"{attr:7}: {getattr(exc, attr)}")
    # APIError sometimes stores the parsed body in .args[0] as a dict.
    if getattr(exc, "args", None):
        print(f"args   : {exc.args}")
    print("=" * (28 + len(prefix)))


# â”€â”€ Question selection (mirrors app.fetch_question_bank filters) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _discover_exam_name(supabase, language_code):
    """Pick an exam_name with approved/eligible questions, preferring BA."""
    result = (
        supabase.table("questions")
        .select("exam_name")
        .eq("language_code", language_code)
        .eq("is_active", True)
        .eq("is_exam_eligible", True)
        .eq("quality_status", "approved")
        .eq("mock_eligible", True)
        .limit(2000)
        .execute()
    )
    names = {}
    for row in (getattr(result, "data", None) or []):
        name = row.get("exam_name")
        if name:
            names[name] = names.get(name, 0) + 1
    if not names:
        raise SystemExit(
            f"No approved/eligible questions found for language '{language_code}'. "
            "Pass --exam and --language explicitly."
        )
    # Prefer a Business Analyst exam; otherwise the one with the most questions.
    ba = [n for n in names if "business analyst" in n.lower()]
    if ba:
        return max(ba, key=lambda n: names[n])
    return max(names, key=lambda n: names[n])


def _fetch_questions(supabase, exam_name, language_code, count):
    """Return ``count`` normalized question dicts (id + answers texts only)."""
    q_result = (
        supabase.table("questions")
        .select("id, exam_name, language_code, category, difficulty")
        .eq("exam_name", exam_name)
        .eq("language_code", language_code)
        .eq("is_active", True)
        .eq("is_exam_eligible", True)
        .eq("quality_status", "approved")
        .eq("mock_eligible", True)
        .limit(max(count * 3, count))
        .execute()
    )
    raw = getattr(q_result, "data", None) or []
    if len(raw) < count:
        raise SystemExit(
            f"Only {len(raw)} eligible questions for '{exam_name}' / '{language_code}', "
            f"need {count}."
        )
    raw = raw[:count]

    ids = [r["id"] for r in raw]
    opts_by_q = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        opt_result = (
            supabase.table("answer_options")
            .select("question_id, option_text, is_correct")
            .in_("question_id", chunk)
            .execute()
        )
        for opt in (getattr(opt_result, "data", None) or []):
            opts_by_q.setdefault(opt["question_id"], []).append(opt)

    questions = []
    for r in raw:
        opts = opts_by_q.get(r["id"], [])
        answers = [o["option_text"] for o in opts if o.get("is_correct")]
        questions.append({
            "id": r["id"],
            "exam_name": r.get("exam_name") or exam_name,
            "language_code": r.get("language_code") or language_code,
            "category": r.get("category") or "Uncategorized",
            "difficulty": (r.get("difficulty") or "medium"),
            "answers": answers or ["__no_correct_option__"],
        })
    return questions


# â”€â”€ Diagnostic run â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def run(count, exam_arg, language_code):
    from supabase import create_client

    url, key = _load_credentials()
    supabase = create_client(url, key)

    exam_name = exam_arg or _discover_exam_name(supabase, language_code)
    print(f"[info] exam_name     : {exam_name}")
    print(f"[info] language_code : {language_code}")
    print(f"[info] question count: {count}")

    questions = _fetch_questions(supabase, exam_name, language_code, count)
    # Answer every question correctly so is_correct is deterministic.
    answers = {idx: list(q["answers"]) for idx, q in enumerate(questions)}

    diag_email = f"diag-{uuid.uuid4().hex[:12]}@certbound.invalid"
    completed_at = datetime.now(timezone.utc)
    parent_id = None
    ok = False

    try:
        # 1) Create the temporary diagnostic parent. Mode is explicitly marked
        #    DIAGNOSTIC so it can never be mistaken for a real readiness mock.
        payload = {
            "user_email": diag_email,
            "mode": "Paid Mock Exam",
            "category": "All Domains",
            "score": 100.0,
            "total_questions": int(count),
            "correct_answers": int(count),
            "domain_breakdown": {},
            "difficulty_breakdown": {},
            "exam_name": exam_name,
            "language_code": language_code,
            "started_at": completed_at.isoformat(),
            "completed_at": completed_at.isoformat(),
        }
        try:
            insert_result = supabase.table("exam_attempts").insert(payload).execute()
        except Exception as exc:
            _print_raw_error("parent exam_attempts insert", exc)
            raise

        parent_id = resolve_exam_attempt_id(insert_result, recover_fn=lambda: None)
        if parent_id is None:
            print("[fail] could not resolve parent exam_attempts id from insert result")
            return 1
        print(f"[ok]   parent id      : {parent_id}")

        # 2) Build child rows with the EXACT production helper.
        rows = build_question_attempt_rows(
            questions,
            answers,
            exam_attempt_id=parent_id,
            user_email=diag_email,
            default_exam_name=exam_name,
            default_language_code=language_code,
            answered_at_iso=completed_at.isoformat(),
            time_spent_by_index={idx: 30 for idx in range(len(questions))},
        )
        print(f"[ok]   built rows     : {len(rows)}")

        # 3) Persist with the EXACT production helper. Capture the raw error.
        captured = []

        def _on_error(exc):
            captured.append(exc)
            _print_raw_error("persist_question_attempts", exc)

        persist_ok, persist_err = persist_question_attempts(
            supabase,
            rows,
            exam_attempt_id=parent_id,
            expected_count=count,
            chunk_size=50,
            on_error=_on_error,
        )
        print(f"[..]   persist ok     : {persist_ok}  msg: {persist_err}")

        # 4) Verify the REAL database state independently of the helper.
        verify = (
            supabase.table("question_attempts")
            .select("question_id", count="exact")
            .eq("exam_attempt_id", parent_id)
            .execute()
        )
        saved_count = getattr(verify, "count", None)
        rows_back = getattr(verify, "data", None) or []
        if saved_count is None:
            saved_count = len(rows_back)
        distinct_ids = len({r.get("question_id") for r in rows_back if r.get("question_id") is not None})

        print(f"[..]   db row count   : {saved_count}")
        print(f"[..]   distinct qids  : {distinct_ids if rows_back else '(count-only; re-query below)'}")

        # Distinct count needs the rows; the count='exact' select returns them too,
        # but be defensive if the client returned only the count.
        if not rows_back:
            full = (
                supabase.table("question_attempts")
                .select("question_id")
                .eq("exam_attempt_id", parent_id)
                .execute()
            )
            rows_back = getattr(full, "data", None) or []
            distinct_ids = len({r.get("question_id") for r in rows_back if r.get("question_id") is not None})
            print(f"[..]   distinct qids  : {distinct_ids}")

        ok = bool(persist_ok) and saved_count == count and distinct_ids == count
        print(f"[{'ok' if ok else 'fail'}]   verification   : "
              f"persist={persist_ok} count={saved_count}=={count} distinct={distinct_ids}=={count}")
        return 0 if ok else 1

    except Exception as exc:
        _print_raw_error("unexpected", exc)
        return 1

    finally:
        # 5) Always delete diagnostic children then the parent, and confirm.
        if parent_id is not None:
            try:
                supabase.table("question_attempts").delete().eq("exam_attempt_id", parent_id).execute()
            except Exception as exc:
                _print_raw_error("cleanup children delete", exc)
            try:
                supabase.table("exam_attempts").delete().eq("id", parent_id).execute()
            except Exception as exc:
                _print_raw_error("cleanup parent delete", exc)

            # Confirm cleanup.
            try:
                child_left = (
                    supabase.table("question_attempts")
                    .select("question_id", count="exact")
                    .eq("exam_attempt_id", parent_id)
                    .execute()
                )
                parent_left = (
                    supabase.table("exam_attempts")
                    .select("id", count="exact")
                    .eq("id", parent_id)
                    .execute()
                )
                c_left = getattr(child_left, "count", None)
                p_left = getattr(parent_left, "count", None)
                if c_left is None:
                    c_left = len(getattr(child_left, "data", None) or [])
                if p_left is None:
                    p_left = len(getattr(parent_left, "data", None) or [])
                print(f"[cleanup] children remaining: {c_left}  parent remaining: {p_left}")
                if c_left == 0 and p_left == 0:
                    print("[cleanup] OK â€” all diagnostic rows removed")
                else:
                    print("[cleanup] WARNING â€” manual cleanup required for parent id "
                          f"{parent_id}")
            except Exception as exc:
                _print_raw_error("cleanup verification", exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live question_attempts persistence diagnostic.")
    parser.add_argument("--count", type=int, default=3, help="number of questions/child rows (use 3 then 60).")
    parser.add_argument("--exam", type=str, default=None, help="exam_name; auto-discovered (prefers BA) if omitted.")
    parser.add_argument("--language", type=str, default="en", help="language_code (default: en).")
    args = parser.parse_args(argv)
    return run(args.count, args.exam, args.language)


if __name__ == "__main__":
    raise SystemExit(main())



