import random
from collections import defaultdict
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

from utils.access_control import render_sidebar_navigation
APP_VERSION = "PRACTICE_BY_CATEGORY_V2_ACCOUNT"

st.set_page_config(
    page_title="Practice by Category",
    layout="wide",
    initial_sidebar_state="expanded",
)
render_sidebar_navigation()

CATEGORY_ORDER = [
    "Configuration and Setup",
    "Object Manager and Lightning App Builder",
    "Data and Analytics Management",
    "Automation",
    "Sales and Marketing Applications",
    "Service and Support Applications",
    "Agentforce AI",
    "Productivity and Collaboration",
]

QUESTION_COUNT_OPTIONS = [10, 20, 30]


@st.cache_resource
def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        st.error("Supabase secrets are missing. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()

    return create_client(url, key)


@st.cache_data(ttl=60)
def fetch_question_bank():
    supabase = get_supabase_client()

    q_response = (
        supabase.table("questions")
        .select("id, category, difficulty, question_text, question_type, select_count, explanation, is_active, is_exam_eligible, quality_status")
        .eq("is_active", True)
        .eq("is_exam_eligible", True)
        .eq("quality_status", "approved")
        .execute()
    )
    questions = q_response.data or []

    if not questions:
        return []

    question_ids = [q["id"] for q in questions]
    options_by_question = defaultdict(list)

    # Supabase REST has practical limits for very long IN lists, so fetch in chunks.
    chunk_size = 100
    for start in range(0, len(question_ids), chunk_size):
        chunk = question_ids[start:start + chunk_size]
        opt_response = (
            supabase.table("answer_options")
            .select("id, question_id, option_text, is_correct, display_order")
            .in_("question_id", chunk)
            .order("display_order")
            .execute()
        )

        for opt in opt_response.data or []:
            options_by_question[opt["question_id"]].append(opt)

    normalized = []
    for q in questions:
        opts = options_by_question.get(q["id"], [])
        if len(opts) < 2:
            continue

        correct_ids = [str(o["id"]) for o in opts if o.get("is_correct")]
        if not correct_ids:
            continue

        normalized.append({
            "id": q["id"],
            "category": q.get("category") or "Uncategorized",
            "difficulty": (q.get("difficulty") or "unknown").lower(),
            "question": q.get("question_text") or "",
            "type": q.get("question_type") or "single",
            "select_count": q.get("select_count"),
            "explanation": q.get("explanation") or "No explanation available.",
            "options": [
                {
                    "id": str(o["id"]),
                    "text": o.get("option_text") or "",
                    "is_correct": bool(o.get("is_correct")),
                }
                for o in opts
            ],
            "correct_ids": correct_ids,
        })

    return normalized


def reset_practice():
    for key in [
        "practice_started",
        "practice_submitted",
        "practice_current_index",
        "practice_questions",
        "practice_answers",
        "practice_feedback_shown",
        "practice_saved",
        "practice_category",
        "practice_count",
    ]:
        if key in st.session_state:
            del st.session_state[key]
    st.rerun()


def is_correct(user_ids, correct_ids):
    return set(user_ids or []) == set(correct_ids or [])


def get_current_user_email():
    email = str(st.session_state.get("user_email", "")).strip().lower()
    if email and "@" in email and "." in email.split("@")[-1]:
        return email
    return None


def build_breakdown(questions, answers, field):
    stats = defaultdict(lambda: {"correct": 0, "total": 0})

    for i, q in enumerate(questions):
        value = q.get(field, "Unknown") or "Unknown"
        stats[value]["total"] += 1
        if is_correct(answers.get(i, []), q.get("correct_ids", [])):
            stats[value]["correct"] += 1

    return dict(stats)


def save_practice_attempt(score, correct, total, category, domain_breakdown, difficulty_breakdown):
    supabase = get_supabase_client()
    user_email = get_current_user_email()
    if not user_email:
        raise ValueError("No account email saved. Open the Account page and save your email first.")

    payload = {
        "user_email": user_email,
        "mode": "Practice by Category",
        "category": category,
        "score": float(score),
        "correct_answers": int(correct),
        "total_questions": int(total),
        "domain_breakdown": domain_breakdown,
        "difficulty_breakdown": difficulty_breakdown,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    supabase.table("exam_attempts").insert(payload).execute()


st.markdown(
    """
    <style>
    .block-container { max-width: 1120px; padding-top: 2rem !important; }
    .practice-banner {
        background: #16325c;
        color: white;
        padding: 18px 22px;
        border-radius: 8px;
        font-size: 27px;
        font-weight: 700;
        margin-bottom: 18px;
    }
    .practice-card {
        border: 1px solid #d8dde6;
        border-radius: 8px;
        padding: 20px;
        background: white;
        margin-bottom: 18px;
    }
    .small-muted { color: #5f6368; font-size: 13px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="practice-banner">Practice by Category</div>', unsafe_allow_html=True)
st.caption(f"App version: {APP_VERSION}")

user_email = get_current_user_email()
if user_email:
    st.success(f"Practice progress will save for: {user_email} ✅")
else:
    st.warning("No account email saved. Open the Account page and save your email before starting practice.")
    st.stop()

question_bank = fetch_question_bank()

if not question_bank:
    st.error("No approved exam-eligible questions found in Supabase.")
    st.stop()

available_categories = [c for c in CATEGORY_ORDER if any(q["category"] == c for q in question_bank)]
extra_categories = sorted({q["category"] for q in question_bank if q["category"] not in CATEGORY_ORDER})
available_categories.extend(extra_categories)

if "practice_started" not in st.session_state:
    st.session_state.practice_started = False
if "practice_submitted" not in st.session_state:
    st.session_state.practice_submitted = False
if "practice_current_index" not in st.session_state:
    st.session_state.practice_current_index = 0
if "practice_answers" not in st.session_state:
    st.session_state.practice_answers = {}
if "practice_feedback_shown" not in st.session_state:
    st.session_state.practice_feedback_shown = False
if "practice_saved" not in st.session_state:
    st.session_state.practice_saved = False


if not st.session_state.practice_started:
    st.header("Choose Practice Settings")

    st.info(
        "Use this page to practice one Salesforce Admin domain at a time. "
        "This is untimed and shows explanations after each submitted answer."
    )

    selected_category = st.selectbox("Select category", available_categories)
    available_count = sum(1 for q in question_bank if q["category"] == selected_category)
    valid_counts = [n for n in QUESTION_COUNT_OPTIONS if n <= available_count]
    if not valid_counts:
        valid_counts = [available_count]

    selected_count = st.selectbox("Number of questions", valid_counts)

    c1, c2, c3 = st.columns(3)
    c1.metric("Available Questions", available_count)
    c2.metric("Practice Questions", selected_count)
    c3.metric("Mode", "Untimed")

    with st.expander("Category bank check"):
        category_counts = defaultdict(int)
        difficulty_counts = defaultdict(int)
        for q in question_bank:
            if q["category"] == selected_category:
                category_counts[q["category"]] += 1
                difficulty_counts[q["difficulty"]] += 1
        st.write("Category:", dict(category_counts))
        st.write("Difficulty mix:", dict(difficulty_counts))

    if st.button("Start Practice", type="primary"):
        category_questions = [q for q in question_bank if q["category"] == selected_category]

        # Try to include a difficulty mix by taking roughly equal samples when possible.
        grouped = defaultdict(list)
        for q in category_questions:
            grouped[q["difficulty"]].append(q)
        for difficulty in grouped:
            random.shuffle(grouped[difficulty])

        selected = []
        difficulty_order = ["easy", "medium", "hard"]
        while len(selected) < selected_count and sum(len(grouped[d]) for d in grouped) > 0:
            for d in difficulty_order:
                if len(selected) >= selected_count:
                    break
                if grouped.get(d):
                    selected.append(grouped[d].pop())

            # Include any non-standard difficulty values if needed.
            for d in list(grouped.keys()):
                if d in difficulty_order:
                    continue
                if len(selected) >= selected_count:
                    break
                if grouped[d]:
                    selected.append(grouped[d].pop())

        random.shuffle(selected)

        for q in selected:
            random.shuffle(q["options"])

        st.session_state.practice_questions = selected
        st.session_state.practice_category = selected_category
        st.session_state.practice_count = selected_count
        st.session_state.practice_started = True
        st.session_state.practice_submitted = False
        st.session_state.practice_current_index = 0
        st.session_state.practice_answers = {}
        st.session_state.practice_feedback_shown = False
        st.session_state.practice_saved = False
        st.rerun()

elif not st.session_state.practice_submitted:
    questions = st.session_state.practice_questions
    index = st.session_state.practice_current_index
    q = questions[index]

    st.markdown(
        f"""
        <div class="practice-card">
            <strong>Question {index + 1} of {len(questions)}</strong><br>
            <span class="small-muted">Domain: {q['category']} | Difficulty: {q['difficulty'].title()}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.progress((index + 1) / len(questions))
    st.subheader(q["question"])

    previous_answer = st.session_state.practice_answers.get(index, [])

    if q["type"] == "multiple":
        select_count = q.get("select_count") or len(q["correct_ids"])
        st.warning(f"Choose {select_count} answers.")
        selected_ids = []
        for opt in q["options"]:
            checked = opt["id"] in previous_answer
            if st.checkbox(opt["text"], value=checked, key=f"practice_{index}_{opt['id']}"):
                selected_ids.append(opt["id"])
        st.session_state.practice_answers[index] = selected_ids
    else:
        option_texts = [opt["text"] for opt in q["options"]]
        id_by_text = {opt["text"]: opt["id"] for opt in q["options"]}
        previous_text = None
        if previous_answer:
            previous_id = previous_answer[0]
            previous_text = next((opt["text"] for opt in q["options"] if opt["id"] == previous_id), None)

        selected_text = st.radio(
            "Choose one answer.",
            option_texts,
            index=option_texts.index(previous_text) if previous_text in option_texts else None,
            key=f"practice_radio_{index}",
        )
        if selected_text:
            st.session_state.practice_answers[index] = [id_by_text[selected_text]]

    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("Submit Answer", type="primary"):
            st.session_state.practice_feedback_shown = True
            st.rerun()

    with col2:
        if st.button("Previous") and index > 0:
            st.session_state.practice_current_index -= 1
            st.session_state.practice_feedback_shown = False
            st.rerun()

    with col3:
        if index < len(questions) - 1:
            if st.button("Next"):
                st.session_state.practice_current_index += 1
                st.session_state.practice_feedback_shown = False
                st.rerun()
        else:
            if st.button("Finish Practice"):
                st.session_state.practice_submitted = True
                st.rerun()

    if st.session_state.practice_feedback_shown:
        user_answer = st.session_state.practice_answers.get(index, [])
        correct_now = is_correct(user_answer, q["correct_ids"])

        if correct_now:
            st.success("Correct ✅")
        else:
            st.error("Incorrect")

        correct_texts = [opt["text"] for opt in q["options"] if opt["id"] in q["correct_ids"]]
        selected_texts = [opt["text"] for opt in q["options"] if opt["id"] in user_answer]

        st.write("Your answer: " + (", ".join(selected_texts) if selected_texts else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_texts))
        st.info(q["explanation"])

    st.divider()
    if st.button("Start New Practice"):
        reset_practice()

else:
    questions = st.session_state.practice_questions
    answers = st.session_state.practice_answers

    correct = sum(1 for i, q in enumerate(questions) if is_correct(answers.get(i, []), q["correct_ids"]))
    total = len(questions)
    score = round((correct / total) * 100, 2) if total else 0

    domain_breakdown = build_breakdown(questions, answers, "category")
    difficulty_breakdown = build_breakdown(questions, answers, "difficulty")

    if not st.session_state.practice_saved:
        try:
            save_practice_attempt(
                score=score,
                correct=correct,
                total=total,
                category=st.session_state.practice_category,
                domain_breakdown=domain_breakdown,
                difficulty_breakdown=difficulty_breakdown,
            )
            st.session_state.practice_saved = True
            st.success("Practice attempt saved to progress tracking ✅")
        except Exception as exc:
            st.warning(f"Practice completed, but saving to progress tracking failed: {exc}")

    st.header("Practice Results")
    c1, c2, c3 = st.columns(3)
    c1.metric("Score", f"{score}%")
    c2.metric("Correct", f"{correct} / {total}")
    c3.metric("Category", st.session_state.practice_category)

    st.subheader("By Difficulty")
    for difficulty, data in difficulty_breakdown.items():
        pct = round((data["correct"] / data["total"]) * 100, 2) if data["total"] else 0
        st.write(f"**{difficulty.title()}:** {data['correct']} / {data['total']} correct ({pct}%)")

    st.subheader("Answer Review")
    for i, q in enumerate(questions):
        user_answer = answers.get(i, [])
        result_correct = is_correct(user_answer, q["correct_ids"])

        if result_correct:
            st.success(f"Question {i + 1} — Correct")
        else:
            st.error(f"Question {i + 1} — Incorrect")

        selected_texts = [opt["text"] for opt in q["options"] if opt["id"] in user_answer]
        correct_texts = [opt["text"] for opt in q["options"] if opt["id"] in q["correct_ids"]]

        st.caption(f"Domain: {q['category']} | Difficulty: {q['difficulty'].title()}")
        st.write(q["question"])
        st.write("Your answer: " + (", ".join(selected_texts) if selected_texts else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_texts))
        st.info(q["explanation"])
        st.divider()

    if st.button("Start New Practice", type="primary"):
        reset_practice()
