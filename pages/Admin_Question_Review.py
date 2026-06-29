import pandas as pd
import streamlit as st
from collections import Counter
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.access_control import get_supabase_admin_client, render_app_chrome, require_admin

from utils.version import APP_VERSION

READ_ONLY_CONTAINMENT_NOTICE = (
    "Live question editing is disabled on this page. "
    "All future content changes must use immutable question versions "
    "and the governed audit/publication workflow. "
    "Use Admin Audit Review to inspect existing audit findings."
)

st.set_page_config(page_title="Admin Question Review", layout="wide")
render_app_chrome()
require_admin()


# SESSION_TIMEOUT_APPLIED
enforce_session_timeout()
show_session_expired_notice()

FALLBACK_CATEGORIES = [
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
    """Use centralized service-role client so Render env vars and Streamlit secrets both work."""
    return get_supabase_admin_client()


@st.cache_data(ttl=60)
def load_active_certifications():
    try:
        result = (
            get_supabase_client().table("certifications")
            .select("exam_name,display_name,certification_code,is_active")
            .eq("is_active", True)
            .order("display_name")
            .execute()
        )
        return result.data or []
    except Exception:
        return []


@st.cache_data(ttl=60)
def load_exam_names_from_questions():
    result = (
        get_supabase_client().table("questions")
        .select("exam_name")
        .order("exam_name")
        .execute()
    )
    names = []
    seen = set()
    for row in result.data or []:
        name = row.get("exam_name")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def build_exam_options():
    certifications = load_active_certifications()
    display_by_exam = {
        row.get("exam_name"): row.get("display_name") or row.get("exam_name")
        for row in certifications
        if row.get("exam_name")
    }
    ordered_names = [row.get("exam_name") for row in certifications if row.get("exam_name")]

    for exam_name in load_exam_names_from_questions():
        if exam_name not in display_by_exam:
            display_by_exam[exam_name] = exam_name
            ordered_names.append(exam_name)

    return ordered_names, display_by_exam


@st.cache_data(ttl=60)
def load_domains_for_exam(exam_name):
    if not exam_name:
        return []
    try:
        result = (
            get_supabase_client().table("certification_domains")
            .select("domain_name,display_order,is_active")
            .eq("exam_name", exam_name)
            .eq("is_active", True)
            .order("display_order")
            .execute()
        )
        return [row.get("domain_name") for row in (result.data or []) if row.get("domain_name")]
    except Exception:
        return []


@st.cache_data(ttl=60)
def load_questions(exam_name):
    supabase = get_supabase_client()
    result = (
        supabase.table("questions")
        .select(
            "id, exam_name, language_code, category, difficulty, question_text, question_type, "
            "select_count, explanation, is_active, is_exam_eligible, quality_status, "
            "review_notes, source_batch, source_file, created_at, updated_at"
        )
        .eq("exam_name", exam_name)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@st.cache_data(ttl=60)
def load_answer_option_counts():
    supabase = get_supabase_client()
    result = supabase.table("answer_options").select("question_id, is_correct").execute()
    rows = result.data or []

    counts = {}
    for row in rows:
        qid = row.get("question_id")
        if not qid:
            continue
        if qid not in counts:
            counts[qid] = {"options": 0, "correct": 0}
        counts[qid]["options"] += 1
        if row.get("is_correct"):
            counts[qid]["correct"] += 1
    return counts


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


def format_bool(value):
    return "Yes" if value else "No"


st.title("Admin Question Review")
st.caption(f"App Version: {APP_VERSION} | Read-only bank browser")
st.warning(READ_ONLY_CONTAINMENT_NOTICE)

exam_names, display_by_exam = build_exam_options()
if not exam_names:
    st.warning("No certifications or question exam names found in Supabase.")
    st.stop()

selected_exam_name = st.selectbox(
    "Choose certification/question bank to review",
    exam_names,
    format_func=lambda name: display_by_exam.get(name, name),
    key="admin_question_review_exam_name",
)

questions = load_questions(selected_exam_name)
option_counts = load_answer_option_counts()

if not questions:
    st.warning(f"No questions found for {display_by_exam.get(selected_exam_name, selected_exam_name)}.")
    st.stop()

domain_categories = load_domains_for_exam(selected_exam_name)
question_categories = sorted({q.get("category") for q in questions if q.get("category")})
CATEGORIES = domain_categories + [c for c in question_categories if c not in domain_categories]
if not CATEGORIES:
    CATEGORIES = FALLBACK_CATEGORIES.copy()

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

filter_col5, filter_col6, filter_col7, filter_col8 = st.columns(4)
with filter_col5:
    active_filter = st.selectbox("Active", ["All", "Active only", "Inactive only"])
with filter_col6:
    eligible_filter = st.selectbox("Exam Eligible", ["All", "Eligible only", "Not eligible only"])
with filter_col7:
    search_text = st.text_input("Search question text")
with filter_col8:
    id_search = st.text_input("Search question ID")

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
    if id_search and id_search.strip() not in str(q.get("id") or ""):
        continue
    filtered.append(q)

st.write(f"Showing **{len(filtered)}** of **{len(questions)}** questions for **{display_by_exam.get(selected_exam_name, selected_exam_name)}**.")

table_rows = []
for q in filtered:
    table_rows.append(
        {
            "id": q.get("id"),
            "exam_name": q.get("exam_name"),
            "language": q.get("language_code"),
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
st.header("Browse One Question")

if not filtered:
    st.warning("No question matches the current filters.")
    st.stop()

question_labels = [
    f"ID: {str(q.get('id'))[:8]} | {q.get('exam_name', selected_exam_name)} | {q.get('category', 'Uncategorized')} | {q.get('difficulty', 'N/A')} | {q.get('question_preview', '')}"
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
st.caption(f"Question ID: {qid} | Exam: {q.get('exam_name', selected_exam_name)} | Language: {q.get('language_code') or 'not set'}")
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

with st.expander("Explanation and Metadata", expanded=True):
    st.markdown("**Explanation**")
    st.write(q.get("explanation") or "_No explanation provided._")

    detail_col1, detail_col2 = st.columns(2)
    with detail_col1:
        st.markdown("**Category**")
        st.write(q.get("category") or "—")
        st.markdown("**Difficulty**")
        st.write(q.get("difficulty") or "—")
        st.markdown("**Quality Status**")
        st.write(q.get("quality_status") or "—")
        st.markdown("**Question Type**")
        st.write(q.get("question_type") or "—")
    with detail_col2:
        st.markdown("**Select Count**")
        st.write(q.get("select_count") if q.get("select_count") is not None else "—")
        st.markdown("**Active**")
        st.write(format_bool(q.get("is_active")))
        st.markdown("**Exam Eligible**")
        st.write(format_bool(q.get("is_exam_eligible")))
        st.markdown("**Review Notes**")
        st.write(q.get("review_notes") or "—")

st.info(
    "This page is read-only. To review audit findings or progress versioned remediation, "
    "open Admin Audit Review from the sidebar."
)
