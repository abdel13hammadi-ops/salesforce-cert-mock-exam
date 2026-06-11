import json
from datetime import datetime, timezone

import streamlit as st
from supabase import create_client

# Ensure Streamlit Cloud can import project-level utilities from pages/.
import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parent
if ROOT_DIR.name == "pages":
    ROOT_DIR = ROOT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.access_control import require_admin
APP_VERSION = "ADMIN_IMPORT_V2_BA_COMPATIBLE"

st.set_page_config(page_title="Admin Import", layout="wide")
require_admin()
st.title("Admin Import")
st.caption(f"App version: {APP_VERSION}")


def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


def normalize_difficulty(value):
    value = str(value or "medium").strip().lower()
    if value in ["easy", "beginner", "foundational", "foundation"]:
        return "easy"
    if value in ["hard", "advanced", "challenging", "challenge", "high", "high difficulty"]:
        return "hard"
    return "medium"


def normalize_question_type(q):
    q_type = str(q.get("type") or q.get("question_type") or "single").strip().lower()
    answers = q.get("answers") or q.get("correct_answers") or q.get("answer") or []
    if isinstance(answers, str):
        answers = [answers]
    if q_type in ["multiple", "multi", "checkbox", "multi-select", "multiselect"] or len(answers) > 1:
        return "multiple"
    return "single"


def normalize_questions(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ["questions", "items", "data"]:
            if isinstance(payload.get(key), list):
                return payload[key]
    raise ValueError("JSON must be a list of questions or an object containing a questions list.")


def normalize_options_and_answers(q):
    raw_options = q.get("options") or q.get("answer_options") or []
    raw_answers = q.get("answers") or q.get("correct_answers") or q.get("answer") or []

    if isinstance(raw_answers, str):
        raw_answers = [raw_answers]

    answer_texts = {str(a).strip() for a in raw_answers if str(a).strip()}
    options = []

    for idx, opt in enumerate(raw_options):
        label = chr(65 + idx)
        text = ""
        is_correct = False

        if isinstance(opt, dict):
            label = str(opt.get("option_label") or opt.get("label") or label).strip()
            text = str(opt.get("option_text") or opt.get("text") or opt.get("value") or "").strip()
            is_correct = bool(opt.get("is_correct", False))
        else:
            text = str(opt).strip()
            is_correct = text in answer_texts

        if text:
            options.append({
                "option_label": label,
                "option_text": text,
                "is_correct": is_correct,
                "display_order": idx + 1,
            })

    # If options were dicts with is_correct, use those as answers too.
    if not answer_texts:
        answer_texts = {o["option_text"] for o in options if o["is_correct"]}

    # If answer list contains letters, map letters to option text.
    letters = {str(a).strip().upper() for a in raw_answers if len(str(a).strip()) == 1}
    if letters:
        for option in options:
            if option["option_label"].upper() in letters:
                option["is_correct"] = True

    correct_count = sum(1 for o in options if o["is_correct"])
    return options, correct_count


def validate_questions(questions):
    errors = []
    warnings = []

    for i, q in enumerate(questions, start=1):
        question_text = str(q.get("question") or q.get("question_text") or "").strip()
        explanation = str(q.get("explanation") or "").strip()
        options, correct_count = normalize_options_and_answers(q)
        q_type = normalize_question_type(q)

        if not question_text:
            errors.append(f"Question {i}: missing question text")
        if len(options) < 2:
            errors.append(f"Question {i}: fewer than 2 answer options")
        if not explanation:
            errors.append(f"Question {i}: missing explanation")
        if q_type == "single" and correct_count != 1:
            errors.append(f"Question {i}: single-answer question has {correct_count} correct options")
        if q_type == "multiple" and correct_count < 2:
            errors.append(f"Question {i}: multi-select question has fewer than 2 correct options")

        select_count = q.get("select_count")
        if q_type == "multiple" and select_count and int(select_count) != correct_count:
            warnings.append(f"Question {i}: select_count {select_count} adjusted to {correct_count}")

    return errors, warnings


def delete_existing_batch(supabase, source_batch):
    existing = supabase.table("questions").select("id").eq("source_batch", source_batch).execute().data or []
    ids = [row["id"] for row in existing]
    if ids:
        supabase.table("answer_options").delete().in_("question_id", ids).execute()
        supabase.table("questions").delete().in_("id", ids).execute()
    return len(ids)


def import_questions(supabase, questions, source_batch, source_file):
    imported_questions = 0
    imported_options = 0
    skipped = []

    for i, q in enumerate(questions, start=1):
        question_text = str(q.get("question") or q.get("question_text") or "").strip()
        explanation = str(q.get("explanation") or "").strip()
        options, correct_count = normalize_options_and_answers(q)
        q_type = normalize_question_type(q)

        if not question_text or not explanation or not options or correct_count == 0:
            skipped.append(i)
            continue

        select_count = q.get("select_count")
        if q_type == "multiple":
            select_count = correct_count
        else:
            select_count = None

        question_payload = {
            "exam_name": str(q.get("exam_name") or q.get("exam") or "Salesforce Certified Business Analyst").strip(),
            "language_code": str(q.get("language_code") or "en").strip().lower(),
            "category": str(q.get("category") or q.get("topic") or "Uncategorized").strip(),
            "difficulty": normalize_difficulty(q.get("difficulty")),
            "question_text": question_text,
            "question_type": q_type,
            "select_count": select_count,
            "explanation": explanation,
            "is_active": bool(q.get("is_active", True)),
            "is_exam_eligible": bool(q.get("is_exam_eligible", True)),
            "quality_status": str(q.get("quality_status") or "approved").strip().lower(),
            "review_notes": str(q.get("review_notes") or "Imported through Admin Import V2.").strip(),
            "source_batch": source_batch,
            "source_file": source_file,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        inserted = supabase.table("questions").insert(question_payload).execute().data
        if not inserted:
            skipped.append(i)
            continue
        question_id = inserted[0]["id"]
        imported_questions += 1

        option_rows = []
        for option in options:
            option_rows.append({
                "question_id": question_id,
                "option_label": option["option_label"],
                "option_text": option["option_text"],
                "is_correct": bool(option["is_correct"]),
                "display_order": int(option["display_order"]),
                "language_code": question_payload["language_code"],
            })

        if option_rows:
            supabase.table("answer_options").insert(option_rows).execute()
            imported_options += len(option_rows)

    return imported_questions, imported_options, skipped


supabase = get_supabase_client()

st.info("Upload one JSON file. This importer supports the Admin JSON structure and the new Business Analyst JSON structure.")

uploaded_file = st.file_uploader("Upload JSON file", type=["json"])
source_batch = st.text_input("Source batch", value="business_analyst_certification_1")
replace_existing = st.checkbox("Replace existing questions from this same source batch", value=True)

if uploaded_file:
    try:
        payload = json.load(uploaded_file)
        questions = normalize_questions(payload)
        errors, warnings = validate_questions(questions)

        st.success(f"Loaded JSON successfully: {len(questions)} questions found")

        exam_counts = {}
        for q in questions:
            exam = q.get("exam_name") or q.get("exam") or "Unknown"
            lang = q.get("language_code") or "en"
            key = f"{exam} | {lang}"
            exam_counts[key] = exam_counts.get(key, 0) + 1
        st.write("Exam/language preview:")
        st.json(exam_counts)

        if warnings:
            with st.expander("Warnings", expanded=False):
                for warning in warnings:
                    st.warning(warning)

        if errors:
            st.error("Fix validation errors before import.")
            with st.expander("Validation errors", expanded=True):
                for error in errors:
                    st.write(f"- {error}")
        else:
            if st.button("Import Questions", type="primary"):
                deleted = 0
                if replace_existing:
                    deleted = delete_existing_batch(supabase, source_batch)

                imported_q, imported_o, skipped = import_questions(
                    supabase=supabase,
                    questions=questions,
                    source_batch=source_batch,
                    source_file=uploaded_file.name,
                )

                st.success("Import completed")
                st.write(f"Deleted existing questions from batch: {deleted}")
                st.write(f"Imported questions: {imported_q}")
                st.write(f"Imported answer options: {imported_o}")
                if skipped:
                    st.warning(f"Skipped question numbers: {skipped}")

                st.info("Now verify in Supabase: total questions should increase, and Business Analyst should appear in grouped counts.")

    except Exception as exc:
        st.error("Import page error")
        st.exception(exc)
