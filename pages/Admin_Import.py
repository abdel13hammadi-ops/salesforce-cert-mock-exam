"""
Admin Import — V3 Atomic RPC

All writes go through the Supabase RPC
  public.admin_import_questions_batch_v1(p_questions jsonb)

This page never calls:
  supabase.table("questions").delete(...)
  supabase.table("answer_options").delete(...)
  supabase.table("questions").insert(...)
  supabase.table("answer_options").insert(...)

The RPC is transactional: a failure rolls back the entire batch.
Re-importing the same external_key updates existing records without
deleting question IDs. Material changes to questions with student
attempts are rejected by the database.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from typing import Any

import streamlit as st
from supabase import create_client

from utils.access_control import render_app_chrome, require_admin
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.version import APP_VERSION

ADMIN_IMPORT_VERSION = "ADMIN_IMPORT_V3_ATOMIC_RPC"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_VALID_DIFFICULTIES = {"easy", "medium", "hard"}
_VALID_QUESTION_TYPES = {"single", "multiple"}
_VALID_COGNITIVE_LEVELS = {"recall", "understanding", "application", "analysis", "judgment"}
_VALID_TRANSLATION_STATUSES = {
    "source", "machine_translated", "reviewed", "approved", "rejected", "outdated"
}

st.set_page_config(page_title="Admin Import", layout="wide")
render_app_chrome()
require_admin()

enforce_session_timeout()
show_session_expired_notice()

st.title("Admin Import")
st.caption(f"App Version: {APP_VERSION} | Import Engine: {ADMIN_IMPORT_VERSION}")


# ---------------------------------------------------------------------------
# Service-role Supabase client (server-side only, never exposed to browser)
# ---------------------------------------------------------------------------

def _get_secret(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, "") or "").strip()
    except Exception:
        return ""


def _get_supabase_client():
    url = _get_secret("SUPABASE_URL")
    key = _get_secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error(
            "Missing Supabase credentials: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY "
            "must be set in environment variables or Streamlit secrets."
        )
        st.stop()
    return create_client(url, key)


# ---------------------------------------------------------------------------
# Payload extraction
# ---------------------------------------------------------------------------

def extract_questions_array(payload: Any, filename: str) -> list[dict]:
    """Return the raw question list from the uploaded JSON.

    Supports:
      - a direct JSON array
      - an object containing a "questions" key (or "items" / "data" fallbacks)
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("questions", "items", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise ValueError(
        "JSON must be a list of questions, or an object with a 'questions' key "
        f"containing a list. File: {filename}"
    )


# ---------------------------------------------------------------------------
# Option normalisation
# ---------------------------------------------------------------------------

def _normalise_options(q: dict) -> list[dict]:
    """Convert whatever option shape the JSON contains into canonical dicts."""
    raw_options: list = q.get("options") or q.get("answer_options") or []
    raw_answers = q.get("answers") or q.get("correct_answers") or q.get("answer") or []
    if isinstance(raw_answers, str):
        raw_answers = [raw_answers]
    answer_texts = {str(a).strip() for a in raw_answers if str(a).strip()}

    options: list[dict] = []
    for idx, opt in enumerate(raw_options):
        label = chr(65 + idx)  # A, B, C …
        text = ""
        is_correct = False

        if isinstance(opt, dict):
            label = str(opt.get("option_label") or opt.get("label") or label).strip()
            text = str(
                opt.get("option_text") or opt.get("text") or opt.get("value") or ""
            ).strip()
            is_correct = bool(opt.get("is_correct", False))
        else:
            text = str(opt).strip()
            is_correct = text in answer_texts

        if not text:
            continue

        options.append(
            {
                "option_label": label,
                "option_text": text,
                "is_correct": is_correct,
                "display_order": idx + 1,
            }
        )

    # If answer list contained single letters, map letters → is_correct.
    letters = {str(a).strip().upper() for a in raw_answers if len(str(a).strip()) == 1}
    if letters:
        for opt in options:
            if opt["option_label"].upper() in letters:
                opt["is_correct"] = True

    # If no explicit answer list, fall back to is_correct flags already set.
    if not answer_texts and not letters:
        pass  # is_correct flags were set from dict form; nothing to do

    return options


# ---------------------------------------------------------------------------
# Per-question preparation (canonical field extraction)
# ---------------------------------------------------------------------------

def _prepare_question(q: dict, fallback_source_file: str) -> dict:
    """Extract and canonicalise all fields for one question.

    Canonical fields take precedence over aliases.
    source_file falls back to the uploaded filename when blank.
    """
    # Text fields — canonical first, then alias
    question_text = str(
        q.get("question_text") or q.get("question") or ""
    ).strip()
    q_type_raw = str(
        q.get("question_type") or q.get("type") or ""
    ).strip().lower()

    options = _normalise_options(q)
    correct_count = sum(1 for o in options if o["is_correct"])

    if q_type_raw in ("multiple", "multi", "checkbox", "multi-select", "multiselect"):
        q_type = "multiple"
    elif q_type_raw in ("single", ""):
        q_type = "single"
    else:
        q_type = q_type_raw  # will fail validation — preserve for error reporting

    select_count_raw = q.get("select_count")
    if q_type == "multiple":
        # Authoritative: number of correct options
        select_count: int | None = correct_count if correct_count >= 2 else None
        # Override from JSON only when JSON value matches correct count
        if select_count_raw is not None:
            try:
                sc = int(select_count_raw)
                if sc == correct_count:
                    select_count = sc
            except (TypeError, ValueError):
                pass
    else:
        select_count = None

    source_file = str(q.get("source_file") or "").strip() or fallback_source_file

    # content_version: must be an integer ≥ 1; preserve raw value for validation
    cv_raw = q.get("content_version")
    try:
        content_version = int(cv_raw) if cv_raw is not None else None
    except (TypeError, ValueError):
        content_version = cv_raw  # keep as-is so validation can report it

    return {
        "external_key": str(q.get("external_key") or "").strip(),
        "exam_name": str(q.get("exam_name") or "").strip(),
        "language_code": str(q.get("language_code") or "").strip(),
        "category": str(q.get("category") or "").strip(),
        "difficulty": str(q.get("difficulty") or "").strip().lower(),
        "question_text": question_text,
        "question_type": q_type,
        "select_count": select_count,
        "explanation": str(q.get("explanation") or "").strip(),
        "is_active": bool(q.get("is_active", True)),
        "is_exam_eligible": bool(q.get("is_exam_eligible", True)),
        "quality_status": str(q.get("quality_status") or "approved").strip().lower(),
        "review_notes": str(q.get("review_notes") or "").strip(),
        "source_batch": str(q.get("source_batch") or "").strip(),
        "source_file": source_file,
        "free_mock_exam": bool(q.get("free_mock_exam", False)),
        "free_sample_order": q.get("free_sample_order"),
        "concept_key": str(q.get("concept_key") or "").strip(),
        "question_family_id": str(q.get("question_family_id") or "").strip(),
        "translation_group_id": str(q.get("translation_group_id") or "").strip(),
        "practice_eligible": bool(q.get("practice_eligible", True)),
        "mock_eligible": bool(q.get("mock_eligible", True)),
        "cognitive_level": str(q.get("cognitive_level") or "").strip().lower(),
        "content_version": content_version,
        "source_question_id": str(q.get("source_question_id") or "").strip() or None,
        "source_content_version": q.get("source_content_version"),
        "translation_status": str(q.get("translation_status") or "").strip().lower(),
        "options": options,
        # internal helpers — not sent to RPC
        "_correct_count": correct_count,
    }


def prepare_for_rpc(raw_questions: list[dict], fallback_source_file: str) -> list[dict]:
    """Prepare all questions. Returns a list ready for the RPC payload."""
    prepared = []
    for q in raw_questions:
        p = _prepare_question(q, fallback_source_file)
        prepared.append(p)
    return prepared


# ---------------------------------------------------------------------------
# Strict client-side validation
# ---------------------------------------------------------------------------

def _is_valid_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


def validate_prepared(prepared: list[dict]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for the prepared question list.

    Validation is strict: invalid values are errors, not silently downgraded.
    """
    errors: list[str] = []
    warnings: list[str] = []

    seen_external_keys: set[str] = set()

    for i, q in enumerate(prepared, start=1):
        prefix = f"Q{i}"
        if q.get("external_key"):
            prefix = f"Q{i} [{q['external_key']}]"

        # --- Required identity ---
        ext_key = q["external_key"]
        if not ext_key:
            errors.append(f"{prefix}: external_key is missing or blank")
        elif ext_key in seen_external_keys:
            errors.append(f"{prefix}: duplicate external_key '{ext_key}' within this file")
        else:
            seen_external_keys.add(ext_key)

        if not q["exam_name"]:
            errors.append(f"{prefix}: exam_name is missing or blank")
        if not q["language_code"]:
            errors.append(f"{prefix}: language_code is missing or blank")
        if not q["category"]:
            errors.append(f"{prefix}: category is missing or blank")
        if not q["source_batch"]:
            errors.append(f"{prefix}: source_batch is missing or blank")

        # --- Controlled vocabularies ---
        if q["difficulty"] not in _VALID_DIFFICULTIES:
            errors.append(
                f"{prefix}: difficulty '{q['difficulty']}' is not in {sorted(_VALID_DIFFICULTIES)}"
            )
        if q["question_type"] not in _VALID_QUESTION_TYPES:
            errors.append(
                f"{prefix}: question_type '{q['question_type']}' is not in {sorted(_VALID_QUESTION_TYPES)}"
            )
        if q["cognitive_level"] not in _VALID_COGNITIVE_LEVELS:
            errors.append(
                f"{prefix}: cognitive_level '{q['cognitive_level']}' is not in "
                f"{sorted(_VALID_COGNITIVE_LEVELS)}"
            )
        if q["translation_status"] not in _VALID_TRANSLATION_STATUSES:
            errors.append(
                f"{prefix}: translation_status '{q['translation_status']}' is not in "
                f"{sorted(_VALID_TRANSLATION_STATUSES)}"
            )

        # --- Text fields ---
        if not q["question_text"]:
            errors.append(f"{prefix}: question_text is missing or blank")
        if not q["explanation"]:
            errors.append(f"{prefix}: explanation is missing or blank")

        # --- content_version ---
        cv = q["content_version"]
        if cv is None:
            errors.append(f"{prefix}: content_version is missing")
        else:
            try:
                cv_int = int(cv)
                if cv_int < 1:
                    errors.append(f"{prefix}: content_version must be an integer ≥ 1, got {cv_int}")
            except (TypeError, ValueError):
                errors.append(f"{prefix}: content_version '{cv}' is not a valid integer")

        # --- Identity UUIDs ---
        if not q["concept_key"]:
            errors.append(f"{prefix}: concept_key is missing or blank")

        qfid = q["question_family_id"]
        if not qfid:
            errors.append(f"{prefix}: question_family_id is missing or blank")
        elif not _is_valid_uuid(qfid):
            errors.append(f"{prefix}: question_family_id '{qfid}' is not a valid UUID")

        tgid = q["translation_group_id"]
        if not tgid:
            errors.append(f"{prefix}: translation_group_id is missing or blank")
        elif not _is_valid_uuid(tgid):
            errors.append(f"{prefix}: translation_group_id '{tgid}' is not a valid UUID")

        # --- Options ---
        opts = q["options"]
        if len(opts) < 2:
            errors.append(f"{prefix}: fewer than 2 answer options ({len(opts)} found)")
        elif len(opts) > 6:
            errors.append(f"{prefix}: more than 6 answer options ({len(opts)} found)")
        else:
            labels = [o["option_label"] for o in opts]
            if len(labels) != len(set(labels)):
                errors.append(f"{prefix}: duplicate option labels: {labels}")
            orders = [o["display_order"] for o in opts]
            if len(orders) != len(set(orders)):
                errors.append(f"{prefix}: duplicate display_order values: {orders}")
            blank_texts = [o["option_label"] for o in opts if not str(o["option_text"]).strip()]
            if blank_texts:
                errors.append(f"{prefix}: blank option text for labels: {blank_texts}")

        # --- Correct answer counts ---
        correct_count = q["_correct_count"]
        q_type = q["question_type"]
        if q_type == "single":
            if correct_count != 1:
                errors.append(
                    f"{prefix}: single-select question must have exactly 1 correct option, "
                    f"found {correct_count}"
                )
        elif q_type == "multiple":
            if correct_count < 2:
                errors.append(
                    f"{prefix}: multiple-select question must have at least 2 correct options, "
                    f"found {correct_count}"
                )
            sc = q["select_count"]
            if sc is not None and sc != correct_count:
                warnings.append(
                    f"{prefix}: select_count {sc} does not match correct option count "
                    f"{correct_count}; RPC will use {correct_count}"
                )

    return errors, warnings


# ---------------------------------------------------------------------------
# Preview generation
# ---------------------------------------------------------------------------

def build_preview(prepared: list[dict]) -> dict[str, Any]:
    exam_lang: dict[str, int] = {}
    category: dict[str, int] = {}
    difficulty: dict[str, int] = {}
    q_types: dict[str, int] = {}
    source_batches: set[str] = set()
    external_keys: set[str] = set()
    concept_keys: set[str] = set()
    family_ids: set[str] = set()
    practice_count = 0
    mock_count = 0
    free_mock_count = 0

    for q in prepared:
        key = f"{q['exam_name']} | {q['language_code']}"
        exam_lang[key] = exam_lang.get(key, 0) + 1
        cat = q["category"] or "Unknown"
        category[cat] = category.get(cat, 0) + 1
        diff = q["difficulty"] or "unknown"
        difficulty[diff] = difficulty.get(diff, 0) + 1
        qt = q["question_type"] or "unknown"
        q_types[qt] = q_types.get(qt, 0) + 1
        if q["source_batch"]:
            source_batches.add(q["source_batch"])
        if q["external_key"]:
            external_keys.add(q["external_key"])
        if q["concept_key"]:
            concept_keys.add(q["concept_key"])
        if q["question_family_id"] and _is_valid_uuid(q["question_family_id"]):
            family_ids.add(q["question_family_id"])
        if q["practice_eligible"]:
            practice_count += 1
        if q["mock_eligible"]:
            mock_count += 1
        if q["free_mock_exam"]:
            free_mock_count += 1

    return {
        "total": len(prepared),
        "exam_language": exam_lang,
        "category": category,
        "difficulty": difficulty,
        "question_type": q_types,
        "practice_eligible": practice_count,
        "mock_eligible": mock_count,
        "free_mock": free_mock_count,
        "source_batches": sorted(source_batches),
        "unique_external_keys": len(external_keys),
        "unique_concept_keys": len(concept_keys),
        "unique_family_ids": len(family_ids),
    }


def render_preview(preview: dict) -> None:
    st.subheader("Import Preview")
    st.metric("Total Questions", preview["total"])

    col1, col2, col3 = st.columns(3)
    col1.metric("Practice-Eligible", preview["practice_eligible"])
    col2.metric("Mock-Eligible", preview["mock_eligible"])
    col3.metric("Free-Mock", preview["free_mock"])

    col4, col5, col6 = st.columns(3)
    col4.metric("Unique external_keys", preview["unique_external_keys"])
    col5.metric("Unique concept_keys", preview["unique_concept_keys"])
    col6.metric("Unique question_family_ids", preview["unique_family_ids"])

    with st.expander("By exam / language", expanded=True):
        for k, v in sorted(preview["exam_language"].items()):
            st.write(f"- **{k}**: {v}")

    with st.expander("By category"):
        for k, v in sorted(preview["category"].items()):
            st.write(f"- {k}: {v}")

    col_d, col_t = st.columns(2)
    with col_d:
        with st.expander("By difficulty"):
            for k, v in sorted(preview["difficulty"].items()):
                st.write(f"- {k}: {v}")
    with col_t:
        with st.expander("By question type"):
            for k, v in sorted(preview["question_type"].items()):
                st.write(f"- {k}: {v}")

    st.write(f"**Source batch(es):** {', '.join(preview['source_batches']) or '—'}")


# ---------------------------------------------------------------------------
# RPC payload construction (strips internal helpers before sending)
# ---------------------------------------------------------------------------

def build_rpc_payload(prepared: list[dict]) -> list[dict]:
    """Strip internal-only keys and return the array sent to the RPC."""
    _internal = {"_correct_count"}
    return [{k: v for k, v in q.items() if k not in _internal} for q in prepared]


# ---------------------------------------------------------------------------
# RPC execution
# ---------------------------------------------------------------------------

def run_import_rpc(supabase, rpc_payload: list[dict]) -> dict:
    """Call the atomic RPC exactly once and return the result dict."""
    response = supabase.rpc(
        "admin_import_questions_batch_v1",
        {"p_questions": rpc_payload},
    ).execute()
    return response.data if response.data else {}


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

supabase = _get_supabase_client()

st.info(
    "Upload one JSON file containing questions for import. "
    "All writes are handled by the atomic Supabase RPC "
    "`admin_import_questions_batch_v1`. "
    "Re-importing the same `external_key` updates existing records without "
    "deleting question IDs. Material changes to questions with student attempts "
    "are rejected by the database."
)

uploaded_file = st.file_uploader("Upload JSON file", type=["json"])

if not uploaded_file:
    st.stop()

# --- JSON parsing ---
try:
    raw_payload = json.load(uploaded_file)
except json.JSONDecodeError as exc:
    st.error(f"JSON parsing failed: {exc}")
    st.stop()

# --- Extract question array ---
try:
    raw_questions = extract_questions_array(raw_payload, uploaded_file.name)
except ValueError as exc:
    st.error(str(exc))
    st.stop()

if not raw_questions:
    st.error("The uploaded file contains no questions.")
    st.stop()

st.success(f"JSON loaded successfully — {len(raw_questions)} questions found.")

# --- Prepare ---
prepared = prepare_for_rpc(raw_questions, uploaded_file.name)

# --- Validate ---
errors, warnings = validate_prepared(prepared)

# --- Warnings ---
if warnings:
    with st.expander(f"Warnings ({len(warnings)})", expanded=False):
        for w in warnings:
            st.warning(w)

# --- Errors: show and block ---
if errors:
    st.error(f"{len(errors)} validation error(s) found. Fix the source file before importing.")
    with st.expander("Validation errors", expanded=True):
        for e in errors:
            st.write(f"- {e}")
    st.stop()

# --- Preview ---
preview = build_preview(prepared)
render_preview(preview)

st.divider()

# --- Confirmation checkbox ---
confirmed = st.checkbox(
    "I confirm that this file should be inserted or updated using stable external keys."
)

import_enabled = confirmed  # validation already passed; just need confirmation

if not import_enabled:
    st.info("Select the confirmation checkbox above to enable the Import button.")
    st.stop()

# --- Import button ---
if st.button("Import Questions", type="primary"):
    rpc_payload = build_rpc_payload(prepared)

    with st.spinner("Sending to database via atomic RPC…"):
        try:
            result = run_import_rpc(supabase, rpc_payload)
        except Exception as exc:
            st.error(
                "RPC call failed. No records were imported. "
                "The RPC is transactional: a failure rolls back the entire batch."
            )
            st.exception(exc)
            st.stop()

    if not result:
        st.error(
            "The RPC returned an empty response. "
            "Verify that `admin_import_questions_batch_v1` exists and is accessible "
            "to the service-role key."
        )
        st.stop()

    st.success("Import completed successfully.")

    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("Questions Processed", result.get("imported_question_count", "—"))
    col_b.metric("Inserted (new)", result.get("inserted_questions", "—"))
    col_c.metric("Updated (existing)", result.get("updated_questions", "—"))
    col_d.metric("Answer Options Written", result.get("answer_options_written", "—"))

    st.info(
        "Re-importing the same external keys updates existing records without deleting "
        "question IDs. Material changes to questions with student attempts are rejected "
        "by the database."
    )

    with st.expander("Full RPC response"):
        st.json(result)
