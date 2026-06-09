import json
import os
import string

import streamlit as st
from supabase import create_client


st.set_page_config(page_title="Admin Import Questions", layout="wide")

st.title("Admin Import Questions")
st.warning("Admin-only page. Use this page to import reviewed JSON question files into Supabase.")


def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url:
        st.error("SUPABASE_URL is missing in Streamlit Secrets.")
        st.stop()

    if not key:
        st.error("SUPABASE_SERVICE_ROLE_KEY is missing in Streamlit Secrets.")
        st.stop()

    return create_client(url, key)


def normalize_question_type(q):
    qtype = q.get("question_type") or q.get("type") or "single"

    if qtype in ["multi", "multiple_select", "checkbox"]:
        qtype = "multiple"

    if qtype not in ["single", "multiple"]:
        qtype = "single"

    return qtype


def get_category(q):
    return q.get("category") or q.get("topic") or "Uncategorized"


def get_exam_name(q):
    return q.get("exam_name") or q.get("exam") or "Salesforce Certified Administrator"


def get_question_text(q):
    return q.get("question_text") or q.get("question") or ""


def get_answers(q):
    answers = q.get("answers") or q.get("correct_answers") or q.get("answer") or []

    if isinstance(answers, str):
        answers = [answers]

    return answers


def get_options(q):
    options = q.get("options") or []
    cleaned_options = []

    for opt in options:
        if isinstance(opt, dict):
            cleaned_options.append(
                opt.get("text") or opt.get("option_text") or opt.get("label") or ""
            )
        else:
            cleaned_options.append(str(opt))

    return [x for x in cleaned_options if x.strip()]


def import_questions_to_supabase(supabase, questions, source_batch, source_file, replace_existing):
    if replace_existing:
        existing = (
            supabase.table("questions")
            .select("id")
            .eq("source_batch", source_batch)
            .execute()
        )

        existing_ids = [row["id"] for row in existing.data]

        for question_id in existing_ids:
            supabase.table("questions").delete().eq("id", question_id).execute()

    imported_questions = 0
    imported_options = 0
    skipped_questions = 0

    for q in questions:
        question_text = get_question_text(q).strip()
        options = get_options(q)
        answers = get_answers(q)

        if not question_text or not options or not answers:
            skipped_questions += 1
            continue

        question_type = normalize_question_type(q)

        select_count = q.get("select_count")
        if question_type == "single":
            select_count = None
        elif not select_count:
            select_count = len(answers)

        question_row = {
            "exam_name": get_exam_name(q),
            "category": get_category(q),
            "difficulty": q.get("difficulty", "medium"),
            "question_text": question_text,
            "question_type": question_type,
            "select_count": select_count,
            "explanation": q.get("explanation", ""),
            "is_active": q.get("is_active", True),
            "is_exam_eligible": q.get("is_exam_eligible", True),
            "quality_status": q.get("quality_status", "approved"),
            "review_notes": q.get("review_notes", ""),
            "source_batch": source_batch,
            "source_file": source_file
        }

        inserted_question = (
            supabase.table("questions")
            .insert(question_row)
            .execute()
        )

        if not inserted_question.data:
            skipped_questions += 1
            continue

        question_id = inserted_question.data[0]["id"]
        imported_questions += 1

        for index, option_text in enumerate(options):
            option_label = string.ascii_uppercase[index]

            option_row = {
                "question_id": question_id,
                "option_label": option_label,
                "option_text": option_text,
                "is_correct": option_text in answers,
                "display_order": index + 1
            }

            supabase.table("answer_options").insert(option_row).execute()
            imported_options += 1

    return imported_questions, imported_options, skipped_questions


uploaded_file = st.file_uploader("Upload one JSON question file", type=["json"])

if uploaded_file:
    source_file = uploaded_file.name
    source_batch = os.path.splitext(source_file)[0]

    st.info(f"Detected source batch: {source_batch}")

    try:
        questions = json.load(uploaded_file)

        if not isinstance(questions, list):
            st.error("This JSON file must contain a list of questions.")
            st.stop()

        st.success(f"File loaded successfully: {len(questions)} questions found.")

        categories = {}
        question_types = {}

        for q in questions:
            category = get_category(q)
            qtype = normalize_question_type(q)

            categories[category] = categories.get(category, 0) + 1
            question_types[qtype] = question_types.get(qtype, 0) + 1

        st.subheader("Preview Summary")

        col1, col2 = st.columns(2)

        with col1:
            st.write("Categories")
            st.json(categories)

        with col2:
            st.write("Question Types")
            st.json(question_types)

        replace_existing = st.checkbox(
            "Replace existing questions from this same source batch if already imported",
            value=False
        )

        st.divider()

        if st.button("Import this JSON file into Supabase", type="primary"):
            supabase = get_supabase_client()

            with st.spinner("Importing questions and answer options..."):
                imported_questions, imported_options, skipped_questions = import_questions_to_supabase(
                    supabase=supabase,
                    questions=questions,
                    source_batch=source_batch,
                    source_file=source_file,
                    replace_existing=replace_existing
                )

            st.success("Import complete.")
            st.write(f"Questions imported: {imported_questions}")
            st.write(f"Answer options imported: {imported_options}")
            st.write(f"Questions skipped: {skipped_questions}")

    except Exception as e:
        st.error("Import failed.")
        st.exception(e)

st.divider()
st.subheader("Database Read Test")

if st.button("Check Question Bank Counts"):
    supabase = get_supabase_client()

    questions_result = (
        supabase.table("questions")
        .select("id", count="exact")
        .execute()
    )

    options_result = (
        supabase.table("answer_options")
        .select("id", count="exact")
        .execute()
    )

    st.success("Connected to Supabase successfully.")
    st.write(f"Questions in database: {questions_result.count}")
    st.write(f"Answer options in database: {options_result.count}")

    category_result = (
        supabase.table("questions")
        .select("category")
        .execute()
    )

    category_counts = {}
    for row in category_result.data:
        category = row.get("category", "Uncategorized")
        category_counts[category] = category_counts.get(category, 0) + 1

    st.write("Category counts:")
    st.json(category_counts)
