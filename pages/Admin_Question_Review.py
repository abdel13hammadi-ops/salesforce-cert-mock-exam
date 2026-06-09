import json
from collections import Counter, defaultdict

import pandas as pd
import streamlit as st
from supabase import create_client

APP_VERSION = "ADMIN_QUESTION_REVIEW_V1"

st.set_page_config(page_title="Admin Question Review", layout="wide")

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

    # Pull question metadata only first. Keep this page fast and admin-focused.
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

    rows = result.data or []
    return rows


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


def clear_cache_and_rerun():
    st.cache_data.clear()
    st.rerun()


def update_question(question_id, updates):
    supabase = get_supabase_client()
    return supabase.table("questions").update(updates).eq("id", question_id).execute()


def normalize_bool(value):
    return bool(value) if value is not None else False


st.title("Admin Question Review")
st.caption(f"App version: {APP_VERSION}")
st.info("Admin-only page for reviewing question quality, status, categories, difficulty, and eligibility.")

questions = load_questions()
option_counts = load_answer_option_counts()

if not questions:
    st.warning("No questions found in Supabase.")
    st.stop()

# Add helper fields for display/filtering.
for q in questions:
    qid = q.get("id")
    q["option_count"] = option_counts.get(qid, {}).get("options", 0)
    q["correct_option_count"] = option_counts.get(qid, {}).get("correct", 0)
    q["question_preview"] = (q.get("question_text") or "")[:140]

# Summary metrics.
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
        st.dataframe(
            pd.DataFrame(
                [{"Category": k, "Questions": v} for k, v in sorted(category_counts.items())]
            ),
            use_container_width=True,
            hide_index=True,
        )

        st.subheader("By Question Type")
        st.dataframe(
            pd.DataFrame(
                [{"Question Type": k, "Questions": v} for k, v in sorted(type_counts.items())]
            ),
            use_container_width=True,
            hide_index=True,
        )

    with col_b:
        st.subheader("By Difficulty")
        st.dataframe(
            pd.DataFrame(
                [{"Difficulty": k, "Questions": v} for k, v in sorted(difficulty_counts.items())]
            ),
            use_container_width=True,
            hide_index=True,
        )

        st.subheader("By Quality Status")
        st.dataframe(
            pd.DataFrame(
                [{"Quality Status": k, "Questions": v} for k, v in sorted(quality_counts.items())]
            ),
            use_container_width=True,
            hide_index=True,
        )

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

# Table view.
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
    f"{idx + 1}. {q.get('category', 'Uncategorized')} | {q.get('difficulty', 'N/A')} | {q.get('question_preview', '')}"
    for idx, q in enumerate(filtered)
]

selected_index = st.selectbox(
    "Choose a question to review",
    range(len(filtered)),
    format_func=lambda i: question_labels[i],
)

q = filtered[selected_index]
qid = q.get("id")

st.subheader("Question")
st.write(q.get("question_text", ""))

meta1, meta2, meta3, meta4 = st.columns(4)
meta1.metric("Options", q.get("option_count", 0))
meta2.metric("Correct Options", q.get("correct_option_count", 0))
meta3.metric("Type", q.get("question_type", "N/A"))
meta4.metric("Select Count", q.get("select_count") if q.get("select_count") is not None else "—")

with st.expander("Explanation", expanded=False):
    st.write(q.get("explanation") or "No explanation stored.")

with st.form(f"edit_question_{qid}"):
    st.subheader("Edit Metadata")

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

    with edit_col2:
        new_active = st.checkbox("Active", value=normalize_bool(q.get("is_active")))
        new_exam_eligible = st.checkbox("Exam Eligible", value=normalize_bool(q.get("is_exam_eligible")))
        new_review_notes = st.text_area("Review Notes", value=q.get("review_notes") or "", height=120)

    save_button = st.form_submit_button("Save Question Metadata", type="primary")

    if save_button:
        updates = {
            "category": new_category,
            "difficulty": new_difficulty,
            "quality_status": new_quality,
            "is_active": new_active,
            "is_exam_eligible": new_exam_eligible,
            "review_notes": new_review_notes,
        }

        try:
            update_question(qid, updates)
            st.success("Question metadata saved ✅")
            clear_cache_and_rerun()
        except Exception as exc:
            st.error(f"Could not save question metadata: {exc}")

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
