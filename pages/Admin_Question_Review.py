import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

import pandas as pd
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
APP_VERSION = "ADMIN_QUESTION_REVIEW_V3_ID_LABELS"

st.set_page_config(page_title="Admin Question Review", layout="wide")
require_admin()

CATEGORIES = [
    "Configuration and Setup",
    "Object Manager and Lightning App Builder",
    "Data and Analytics Management",
    "Automation",
    "Sales and Marketing Applications",
    "Service and Support Applications",
    "Agentforce AI",
    "Productivity and Collaboration",
]

DIFFICULTIES = ["easy", "medium", "hard"]
QUALITY_STATUSES = ["approved", "needs_edit", "practice_only", "reject"]
QUESTION_TYPES = ["single", "multiple"]


def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        st.error("Missing Supabase secrets. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()

    return create_client(url, key)


@st.cache_data(ttl=60)
def load_questions():
    supabase = get_supabase_client()
    result = (
        supabase.table("questions")
        .select(
            "id, exam_name, category, difficulty, question_text, question_type, "
            "select_count, explanation, is_active, is_exam_eligible, quality_status, "
            "review_notes, source_batch, source_file, created_at, updated_at"
        )
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@st.cache_data(ttl=60)
def load_answer_option_counts():
    supabase = get_supabase_client()
    result = supabase.table("answer_options").select("question_id, is_correct").execute()
    rows = result.data or []

    counts = defaultdict(lambda: {"options": 0, "correct": 0})
    for row in rows:
        qid = row.get("question_id")
        if qid:
            counts[qid]["options"] += 1
            if row.get("is_correct"):
                counts[qid]["correct"] += 1
    return dict(counts)


@st.cache_data(ttl=30)
def load_answer_options(question_id):
    supabase = get_supabase_client()
    result = (
        supabase.table("answer_options")
        .select("id, question_id, option_label, option_text, is_correct, display_order")
        .eq("question_id", question_id)
        .order("display_order")
        .execute()
    )
    return result.data or []


def clear_cache_and_rerun():
    st.cache_data.clear()
    st.rerun()


def update_question(question_id, updates):
    supabase = get_supabase_client()
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    return supabase.table("questions").update(updates).eq("id", question_id).execute()


def update_answer_option(option_id, updates):
    supabase = get_supabase_client()
    return supabase.table("answer_options").update(updates).eq("id", option_id).execute()


def normalize_bool(value):
    return bool(value) if value is not None else False


def valid_email_or_none(value):
    value = str(value or "").strip().lower()
    return value if "@" in value and "." in value.split("@")[-1] else None


st.title("Admin Question Review")
st.caption(f"App version: {APP_VERSION}")
st.info("Admin-only page for reviewing and editing questions, answers, explanations, categories, difficulty, and eligibility.")

questions = load_questions()
option_counts = load_answer_option_counts()

if not questions:
    st.warning("No questions found in Supabase.")
    st.stop()

for q in questions:
    qid = q.get("id")
    q["option_count"] = option_counts.get(qid, {}).get("options", 0)
    q["correct_option_count"] = option_counts.get(qid, {}).get("correct", 0)
    q["question_preview"] = (q.get("question_text") or "")[:140]

total_questions = len(questions)
active_questions = sum(1 for q in questions if q.get("is_active"))
exam_eligible = sum(1 for q in questions if q.get("is_exam_eligible"))
approved = sum(1 for q in questions if q.get("quality_status") == "approved")
multi_select = sum(1 for q in questions if q.get("question_type") == "multiple")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Questions", total_questions)
c2.metric("Active", active_questions)
c3.metric("Exam Eligible", exam_eligible)
c4.metric("Approved", approved)
c5.metric("Multi-Select", multi_select)

st.divider()

with st.expander("Question bank health check", expanded=True):
    category_counts = Counter(q.get("category", "Uncategorized") for q in questions)
    difficulty_counts = Counter(q.get("difficulty", "Uncategorized") for q in questions)
    quality_counts = Counter(q.get("quality_status", "Uncategorized") for q in questions)
    type_counts = Counter(q.get("question_type", "Uncategorized") for q in questions)

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("By Category")
        st.dataframe(pd.DataFrame([{"Category": k, "Questions": v} for k, v in sorted(category_counts.items())]), use_container_width=True, hide_index=True)
        st.subheader("By Question Type")
        st.dataframe(pd.DataFrame([{"Question Type": k, "Questions": v} for k, v in sorted(type_counts.items())]), use_container_width=True, hide_index=True)
    with col_b:
        st.subheader("By Difficulty")
        st.dataframe(pd.DataFrame([{"Difficulty": k, "Questions": v} for k, v in sorted(difficulty_counts.items())]), use_container_width=True, hide_index=True)
        st.subheader("By Quality Status")
        st.dataframe(pd.DataFrame([{"Quality Status": k, "Questions": v} for k, v in sorted(quality_counts.items())]), use_container_width=True, hide_index=True)

st.divider()
st.header("Search and Filter")

filter_col1, filter_col2, filter_col3, filter_col4 = st.columns(4)
with filter_col1:
    selected_category = st.selectbox("Category", ["All"] + CATEGORIES)
with filter_col2:
    selected_difficulty = st.selectbox("Difficulty", ["All"] + DIFFICULTIES)
with filter_col3:
    selected_quality = st.selectbox("Quality Status", ["All"] + QUALITY_STATUSES)
with filter_col4:
    selected_type = st.selectbox("Question Type", ["All"] + QUESTION_TYPES)

filter_col5, filter_col6, filter_col7 = st.columns(3)
with filter_col5:
    active_filter = st.selectbox("Active", ["All", "Active only", "Inactive only"])
with filter_col6:
    eligible_filter = st.selectbox("Exam Eligible", ["All", "Eligible only", "Not eligible only"])
with filter_col7:
    search_text = st.text_input("Search question text")

filtered = []
for q in questions:
    if selected_category != "All" and q.get("category") != selected_category:
        continue
    if selected_difficulty != "All" and q.get("difficulty") != selected_difficulty:
        continue
    if selected_quality != "All" and q.get("quality_status") != selected_quality:
        continue
    if selected_type != "All" and q.get("question_type") != selected_type:
        continue
    if active_filter == "Active only" and not q.get("is_active"):
        continue
    if active_filter == "Inactive only" and q.get("is_active"):
        continue
    if eligible_filter == "Eligible only" and not q.get("is_exam_eligible"):
        continue
    if eligible_filter == "Not eligible only" and q.get("is_exam_eligible"):
        continue
    if search_text and search_text.lower() not in (q.get("question_text") or "").lower():
        continue
    filtered.append(q)

st.write(f"Showing **{len(filtered)}** of **{len(questions)}** questions.")

table_rows = []
for q in filtered:
    table_rows.append(
        {
            "id": q.get("id"),
            "category": q.get("category"),
            "difficulty": q.get("difficulty"),
            "type": q.get("question_type"),
            "select_count": q.get("select_count"),
            "active": q.get("is_active"),
            "exam_eligible": q.get("is_exam_eligible"),
            "quality_status": q.get("quality_status"),
            "options": q.get("option_count"),
            "correct": q.get("correct_option_count"),
            "preview": q.get("question_preview"),
            "source_batch": q.get("source_batch"),
        }
    )
st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

st.divider()
st.header("Review / Edit One Question")

if not filtered:
    st.warning("No question matches the current filters.")
    st.stop()

question_labels = [
    f"ID: {str(q.get('id'))[:8]} | {q.get('category', 'Uncategorized')} | {q.get('difficulty', 'N/A')} | {q.get('question_preview', '')}"
    for q in filtered
]

selected_index = st.selectbox(
    "Choose a question to review",
    range(len(filtered)),
    format_func=lambda i: question_labels[i],
)

q = filtered[selected_index]
qid = q.get("id")
answer_options = load_answer_options(qid)

st.subheader("Current Question Preview")
st.caption(f"Question ID: {qid}")
st.code(str(qid), language="text")
st.write(q.get("question_text", ""))

meta1, meta2, meta3, meta4, meta5 = st.columns(5)
meta1.metric("ID", str(qid)[:8])
meta2.metric("Options", q.get("option_count", 0))
meta3.metric("Correct Options", q.get("correct_option_count", 0))
meta4.metric("Type", q.get("question_type", "N/A"))
meta5.metric("Select Count", q.get("select_count") if q.get("select_count") is not None else "—")

with st.expander("Current Answer Options", expanded=True):
    if not answer_options:
        st.warning("No answer options found for this question.")
    else:
        option_rows = [
            {
                "Label": option.get("option_label"),
                "Answer Text": option.get("option_text"),
                "Correct": option.get("is_correct"),
                "Display Order": option.get("display_order"),
            }
            for option in answer_options
        ]
        st.dataframe(pd.DataFrame(option_rows), use_container_width=True, hide_index=True)

with st.form(f"edit_question_text_metadata_{qid}"):
    st.subheader("Edit Question, Explanation, and Metadata")

    new_question_text = st.text_area("Question Text", value=q.get("question_text") or "", height=160)
    new_explanation = st.text_area("Explanation", value=q.get("explanation") or "", height=220)

    edit_col1, edit_col2 = st.columns(2)
    with edit_col1:
        new_category = st.selectbox(
            "Category",
            CATEGORIES,
            index=CATEGORIES.index(q.get("category")) if q.get("category") in CATEGORIES else 0,
        )
        new_difficulty = st.selectbox(
            "Difficulty",
            DIFFICULTIES,
            index=DIFFICULTIES.index(q.get("difficulty")) if q.get("difficulty") in DIFFICULTIES else 1,
        )
        new_quality = st.selectbox(
            "Quality Status",
            QUALITY_STATUSES,
            index=QUALITY_STATUSES.index(q.get("quality_status")) if q.get("quality_status") in QUALITY_STATUSES else 0,
        )
        new_question_type = st.selectbox(
            "Question Type",
            QUESTION_TYPES,
            index=QUESTION_TYPES.index(q.get("question_type")) if q.get("question_type") in QUESTION_TYPES else 0,
        )

    with edit_col2:
        new_select_count_raw = st.number_input(
            "Select Count for multi-select questions. Use 0 for single-answer questions.",
            min_value=0,
            max_value=5,
            value=int(q.get("select_count") or 0),
            step=1,
        )
        new_active = st.checkbox("Active", value=normalize_bool(q.get("is_active")))
        new_exam_eligible = st.checkbox("Exam Eligible", value=normalize_bool(q.get("is_exam_eligible")))
        new_review_notes = st.text_area("Review Notes", value=q.get("review_notes") or "", height=120)

    save_question_button = st.form_submit_button("Save Question Text / Explanation / Metadata", type="primary")

    if save_question_button:
        if not new_question_text.strip():
            st.error("Question text cannot be blank.")
        elif not new_explanation.strip():
            st.error("Explanation cannot be blank.")
        elif new_question_type == "single" and new_select_count_raw != 0:
            st.error("Single-answer questions must use select_count = 0.")
        elif new_question_type == "multiple" and new_select_count_raw < 2:
            st.error("Multi-select questions should have select_count of at least 2.")
        else:
            updates = {
                "question_text": new_question_text.strip(),
                "explanation": new_explanation.strip(),
                "category": new_category,
                "difficulty": new_difficulty,
                "quality_status": new_quality,
                "question_type": new_question_type,
                "select_count": None if new_question_type == "single" else int(new_select_count_raw),
                "is_active": new_active,
                "is_exam_eligible": new_exam_eligible,
                "review_notes": new_review_notes,
            }
            try:
                update_question(qid, updates)
                st.success("Question text, explanation, and metadata saved ✅")
                clear_cache_and_rerun()
            except Exception as exc:
                st.error(f"Could not save question updates: {exc}")

st.divider()

with st.form(f"edit_answer_options_{qid}"):
    st.subheader("Edit Answer Options")
    st.caption("Change answer text and mark the correct answer(s). For single-answer questions, exactly one option must be correct.")

    option_updates = []
    if not answer_options:
        st.warning("No answer options available to edit.")
    else:
        for idx, option in enumerate(answer_options):
            st.markdown(f"**Option {idx + 1}**")
            oc1, oc2, oc3 = st.columns([1, 6, 1])
            with oc1:
                option_label = st.text_input(
                    "Label",
                    value=option.get("option_label") or chr(65 + idx),
                    key=f"label_{qid}_{option.get('id')}",
                )
            with oc2:
                option_text = st.text_area(
                    "Answer Text",
                    value=option.get("option_text") or "",
                    height=90,
                    key=f"text_{qid}_{option.get('id')}",
                )
            with oc3:
                is_correct = st.checkbox(
                    "Correct",
                    value=normalize_bool(option.get("is_correct")),
                    key=f"correct_{qid}_{option.get('id')}",
                )
            display_order = idx + 1
            option_updates.append(
                {
                    "id": option.get("id"),
                    "option_label": option_label.strip(),
                    "option_text": option_text.strip(),
                    "is_correct": bool(is_correct),
                    "display_order": display_order,
                }
            )

    save_options_button = st.form_submit_button("Save Answer Options", type="primary")

    if save_options_button:
        if not option_updates:
            st.error("No answer options to save.")
        elif any(not item["option_text"] for item in option_updates):
            st.error("Answer option text cannot be blank.")
        else:
            correct_count = sum(1 for item in option_updates if item["is_correct"])
            current_type = q.get("question_type", "single")
            current_select_count = q.get("select_count")

            if current_type == "single" and correct_count != 1:
                st.error("Single-answer questions must have exactly one correct answer.")
            elif current_type == "multiple" and correct_count < 2:
                st.error("Multi-select questions must have at least two correct answers.")
            elif current_type == "multiple" and current_select_count and int(current_select_count) != correct_count:
                st.error(f"This question has select_count={current_select_count}, but {correct_count} options are marked correct. Update select_count or correct answers first.")
            else:
                try:
                    for item in option_updates:
                        option_id = item.pop("id")
                        update_answer_option(option_id, item)
                    st.success("Answer options saved ✅")
                    clear_cache_and_rerun()
                except Exception as exc:
                    st.error(f"Could not save answer options: {exc}")

st.divider()
st.header("Fast Quality Actions")
fast_col1, fast_col2, fast_col3 = st.columns(3)

with fast_col1:
    if st.button("Approve + Make Exam Eligible"):
        update_question(
            qid,
            {
                "quality_status": "approved",
                "is_active": True,
                "is_exam_eligible": True,
                "review_notes": q.get("review_notes") or "Approved from admin review page.",
            },
        )
        st.success("Question approved and made exam eligible ✅")
        clear_cache_and_rerun()

with fast_col2:
    if st.button("Needs Edit + Remove From Exam"):
        update_question(
            qid,
            {
                "quality_status": "needs_edit",
                "is_exam_eligible": False,
                "review_notes": q.get("review_notes") or "Needs edit from admin review page.",
            },
        )
        st.warning("Question marked needs_edit and removed from exam eligibility.")
        clear_cache_and_rerun()

with fast_col3:
    if st.button("Reject + Deactivate"):
        update_question(
            qid,
            {
                "quality_status": "reject",
                "is_active": False,
                "is_exam_eligible": False,
                "review_notes": q.get("review_notes") or "Rejected from admin review page.",
            },
        )
        st.error("Question rejected and deactivated.")
        clear_cache_and_rerun()
