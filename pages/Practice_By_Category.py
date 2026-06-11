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
APP_VERSION = "PRACTICE_BY_CATEGORY_V4_ENROLLED_CERT_ACCESS"
QUESTION_COUNT_OPTIONS = [10, 20, 30]

st.set_page_config(page_title="Practice by Category", layout="wide", initial_sidebar_state="expanded")
render_sidebar_navigation()


@st.cache_resource
def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error("Supabase secrets are missing. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


def get_current_user_email():
    email = str(st.session_state.get("user_email", "")).strip().lower()
    if email and "@" in email and "." in email.split("@")[-1]:
        return email
    return None


@st.cache_data(ttl=60)
def fetch_user_profile(email):
    if not email:
        return {}
    supabase = get_supabase_client()
    result = (
        supabase.table("app_users")
        .select("email,full_name,subscription_status,preferred_language_code")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    return (result.data or [{}])[0]


@st.cache_data(ttl=60)
def fetch_languages():
    supabase = get_supabase_client()
    try:
        result = (
            supabase.table("languages")
            .select("language_code,language_name,native_name,is_active,display_order")
            .eq("is_active", True)
            .order("display_order")
            .execute()
        )
        return result.data or []
    except Exception:
        return [{"language_code": "en", "language_name": "English", "native_name": "English"}]


def language_label(language_code):
    for lang in fetch_languages():
        if lang.get("language_code") == language_code:
            native = lang.get("native_name") or lang.get("language_name") or language_code
            return f"{native} ({language_code})"
    return language_code


@st.cache_data(ttl=60)
def fetch_user_certifications(user_email):
    user_email = str(user_email or "").strip().lower()
    if not user_email:
        return []

    supabase = get_supabase_client()
    access_result = (
        supabase.table("user_certification_access")
        .select("exam_name, access_status")
        .eq("user_email", user_email)
        .eq("access_status", "active")
        .execute()
    )
    access_rows = access_result.data or []
    allowed_exam_names = [row.get("exam_name") for row in access_rows if row.get("exam_name")]

    if not allowed_exam_names:
        return []

    result = (
        supabase.table("certifications")
        .select("exam_name,display_name,certification_code,is_active")
        .in_("exam_name", allowed_exam_names)
        .eq("is_active", True)
        .order("display_name")
        .execute()
    )
    return result.data or []


@st.cache_data(ttl=60)
def fetch_domains(exam_name):
    supabase = get_supabase_client()
    result = (
        supabase.table("certification_domains")
        .select("domain_name,display_order,is_active")
        .eq("exam_name", exam_name)
        .eq("is_active", True)
        .order("display_order")
        .execute()
    )
    return [row["domain_name"] for row in (result.data or [])]


@st.cache_data(ttl=60)
def fetch_question_bank(exam_name, language_code):
    supabase = get_supabase_client()
    q_response = (
        supabase.table("questions")
        .select("id, exam_name, language_code, category, difficulty, question_text, question_type, select_count, explanation, is_active, is_exam_eligible, quality_status")
        .eq("exam_name", exam_name)
        .eq("language_code", language_code)
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
    for start in range(0, len(question_ids), 100):
        chunk = question_ids[start:start + 100]
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
            "exam_name": q.get("exam_name"),
            "language_code": q.get("language_code"),
            "category": q.get("category") or "Uncategorized",
            "difficulty": (q.get("difficulty") or "unknown").lower(),
            "question": q.get("question_text") or "",
            "type": q.get("question_type") or "single",
            "select_count": q.get("select_count"),
            "explanation": q.get("explanation") or "No explanation available.",
            "options": [{"id": str(o["id"]), "text": o.get("option_text") or "", "is_correct": bool(o.get("is_correct"))} for o in opts],
            "correct_ids": correct_ids,
        })
    return normalized


def reset_practice():
    keys = [
        "practice_started", "practice_submitted", "practice_current_index", "practice_questions",
        "practice_answers", "practice_feedback_shown", "practice_saved", "practice_category",
        "practice_count", "practice_exam_name", "practice_language_code",
    ]
    for key in keys:
        st.session_state.pop(key, None)
    st.rerun()


def is_correct(user_ids, correct_ids):
    return set(user_ids or []) == set(correct_ids or [])


def build_breakdown(questions, answers, field):
    stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for i, q in enumerate(questions):
        value = q.get(field, "Unknown") or "Unknown"
        stats[value]["total"] += 1
        if is_correct(answers.get(i, []), q.get("correct_ids", [])):
            stats[value]["correct"] += 1
    return dict(stats)


def save_practice_attempt(score, correct, total, category, domain_breakdown, difficulty_breakdown, exam_name, language_code):
    user_email = get_current_user_email()
    if not user_email:
        raise ValueError("No account email saved. Open Account first.")
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
        "exam_name": exam_name,
        "language_code": language_code,
    }
    get_supabase_client().table("exam_attempts").insert(payload).execute()


st.markdown(
    """
    <style>
    .block-container { max-width: 1120px; padding-top: 2rem !important; }
    .practice-banner { background:#16325c;color:white;padding:18px 22px;border-radius:8px;font-size:27px;font-weight:700;margin-bottom:18px; }
    .practice-card { border:1px solid #d8dde6;border-radius:8px;padding:20px;background:white;margin-bottom:18px; }
    .small-muted { color:#5f6368;font-size:13px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="practice-banner">Practice by Category</div>', unsafe_allow_html=True)
st.caption(f"App version: {APP_VERSION}")

user_email = get_current_user_email()
if not user_email:
    st.warning("Please log in from the Account page before starting practice.")
    st.stop()

profile = fetch_user_profile(user_email)
language_code = str(profile.get("preferred_language_code") or "en").strip().lower()
st.success(f"Account: {user_email} ✅ | Preferred language: {language_label(language_code)}")

certifications = fetch_user_certifications(user_email)
if not certifications:
    st.error("No certification enrollment found for this account.")
    st.info("Ask an admin to enroll this email in a certification, or purchase access when payments are enabled.")
    st.stop()
exam_names = [c["exam_name"] for c in certifications]
display_by_exam = {c["exam_name"]: c.get("display_name") or c["exam_name"] for c in certifications}

if not st.session_state.get("practice_started", False):
    selected_exam = st.selectbox(
        "Choose certification",
        exam_names,
        format_func=lambda x: display_by_exam.get(x, x),
        key="practice_selected_exam_name",
    )
    domains = fetch_domains(selected_exam)
    question_bank = fetch_question_bank(selected_exam, language_code)

    st.header("Choose Practice Settings")
    st.info("Practice one domain at a time. Explanations are shown during practice and again in the final review.")

    if not question_bank:
        st.error(f"No approved questions found for {display_by_exam.get(selected_exam, selected_exam)} in {language_label(language_code)}.")
        st.stop()

    available_categories = [d for d in domains if any(q["category"] == d for q in question_bank)]
    extra_categories = sorted({q["category"] for q in question_bank if q["category"] not in available_categories})
    available_categories.extend(extra_categories)

    selected_category = st.selectbox("Select category", available_categories)
    available_count = sum(1 for q in question_bank if q["category"] == selected_category)
    valid_counts = [n for n in QUESTION_COUNT_OPTIONS if n <= available_count] or [available_count]
    selected_count = st.selectbox("Number of questions", valid_counts)

    c1, c2, c3 = st.columns(3)
    c1.metric("Available Questions", available_count)
    c2.metric("Practice Questions", selected_count)
    c3.metric("Mode", "Untimed")

    if st.button("Start Practice", type="primary"):
        category_questions = [q for q in question_bank if q["category"] == selected_category]
        grouped = defaultdict(list)
        for q in category_questions:
            grouped[q["difficulty"]].append(q)
        for difficulty in grouped:
            random.shuffle(grouped[difficulty])

        selected = []
        while len(selected) < selected_count and sum(len(v) for v in grouped.values()) > 0:
            for d in ["easy", "medium", "hard"] + [x for x in grouped.keys() if x not in {"easy", "medium", "hard"}]:
                if len(selected) >= selected_count:
                    break
                if grouped.get(d):
                    selected.append(grouped[d].pop())
        random.shuffle(selected)
        for q in selected:
            random.shuffle(q["options"])

        st.session_state.practice_questions = selected
        st.session_state.practice_category = selected_category
        st.session_state.practice_count = selected_count
        st.session_state.practice_exam_name = selected_exam
        st.session_state.practice_language_code = language_code
        st.session_state.practice_started = True
        st.session_state.practice_submitted = False
        st.session_state.practice_current_index = 0
        st.session_state.practice_answers = {}
        st.session_state.practice_feedback_shown = False
        st.session_state.practice_saved = False
        st.rerun()

elif not st.session_state.get("practice_submitted", False):
    questions = st.session_state.practice_questions
    index = st.session_state.get("practice_current_index", 0)
    q = questions[index]
    st.markdown(f"""
    <div class="practice-card">
        <strong>Question {index + 1} of {len(questions)}</strong><br>
        <span class="small-muted">Certification: {display_by_exam.get(st.session_state.practice_exam_name, st.session_state.practice_exam_name)} | Domain: {q['category']} | Difficulty: {q['difficulty'].title()}</span>
    </div>
    """, unsafe_allow_html=True)
    st.progress((index + 1) / len(questions))
    st.subheader(q["question"])

    previous_answer = st.session_state.get("practice_answers", {}).get(index, [])
    if q["type"] == "multiple":
        select_count = q.get("select_count") or len(q["correct_ids"])
        st.warning(f"Choose {select_count} answers.")
        selected_ids = []
        for opt in q["options"]:
            if st.checkbox(opt["text"], value=opt["id"] in previous_answer, key=f"practice_{index}_{opt['id']}"):
                selected_ids.append(opt["id"])
        st.session_state.practice_answers[index] = selected_ids
    else:
        option_texts = [opt["text"] for opt in q["options"]]
        id_by_text = {opt["text"]: opt["id"] for opt in q["options"]}
        previous_text = next((opt["text"] for opt in q["options"] if previous_answer and opt["id"] == previous_answer[0]), None)
        selected_text = st.radio("Choose one answer.", option_texts, index=option_texts.index(previous_text) if previous_text in option_texts else None, key=f"practice_radio_{index}")
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

    if st.session_state.get("practice_feedback_shown", False):
        user_answer = st.session_state.practice_answers.get(index, [])
        correct_now = is_correct(user_answer, q["correct_ids"])
        st.success("Correct ✅" if correct_now else "Incorrect") if correct_now else st.error("Incorrect")
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

    if not st.session_state.get("practice_saved", False):
        try:
            save_practice_attempt(score, correct, total, st.session_state.practice_category, domain_breakdown, difficulty_breakdown, st.session_state.practice_exam_name, st.session_state.practice_language_code)
            st.session_state.practice_saved = True
            st.success("Practice attempt saved to progress tracking ✅")
        except Exception as exc:
            st.warning(f"Practice completed, but saving to progress tracking failed: {exc}")

    st.header("Practice Results")
    c1, c2, c3 = st.columns(3)
    c1.metric("Score", f"{score}%")
    c2.metric("Correct", f"{correct} / {total}")
    c3.metric("Category", st.session_state.practice_category)

    st.subheader("Answer Review")
    for i, q in enumerate(questions):
        user_answer = answers.get(i, [])
        result_correct = is_correct(user_answer, q["correct_ids"])
        st.success(f"Question {i + 1} — Correct") if result_correct else st.error(f"Question {i + 1} — Incorrect")
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
