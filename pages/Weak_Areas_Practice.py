import json
import random
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from supabase import create_client

APP_VERSION = "WEAK_AREAS_PRACTICE_V6_CERT_SELECTOR"
QUESTION_COUNT_OPTIONS = [10, 20, 30]
PAID_STATUS_VALUES = {"active", "paid", "premium", "subscribed", "trialing"}

st.set_page_config(page_title="Weak Areas Practice", layout="wide", initial_sidebar_state="expanded")


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
    result = (
        get_supabase_client().table("app_users")
        .select("email,full_name,subscription_status,preferred_language_code")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    return (result.data or [{}])[0]


@st.cache_data(ttl=60)
def fetch_languages():
    try:
        result = (
            get_supabase_client().table("languages")
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
def fetch_certifications():
    result = (
        get_supabase_client().table("certifications")
        .select("exam_name,display_name,certification_code,is_active")
        .eq("is_active", True)
        .order("display_name")
        .execute()
    )
    return result.data or []


@st.cache_data(ttl=60)
def fetch_domains(exam_name):
    result = (
        get_supabase_client().table("certification_domains")
        .select("domain_name,display_order,is_active")
        .eq("exam_name", exam_name)
        .eq("is_active", True)
        .order("display_order")
        .execute()
    )
    return [row["domain_name"] for row in (result.data or [])]


@st.cache_data(ttl=60)
def fetch_attempts(user_email, exam_name, language_code):
    if not user_email or not exam_name or not language_code:
        return []
    result = (
        get_supabase_client().table("exam_attempts")
        .select("id,user_email,mode,category,score,correct_answers,total_questions,domain_breakdown,completed_at,exam_name,language_code")
        .eq("user_email", user_email)
        .eq("exam_name", exam_name)
        .eq("language_code", language_code)
        .order("id", desc=True)
        .execute()
    )
    return result.data or []


@st.cache_data(ttl=60)
def fetch_question_bank(exam_name, language_code):
    q_response = (
        get_supabase_client().table("questions")
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

    ids = [q["id"] for q in questions]
    options_by_question = defaultdict(list)
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        opt_response = (
            get_supabase_client().table("answer_options")
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


def normalize_breakdown(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def aggregate_domains(attempts):
    totals = defaultdict(lambda: {"correct": 0, "total": 0})
    for attempt in attempts:
        breakdown = normalize_breakdown(attempt.get("domain_breakdown"))
        for name, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            correct = int(data.get("correct", 0) or 0)
            total = int(data.get("total", 0) or 0)
            if total <= 0:
                continue
            totals[name]["correct"] += correct
            totals[name]["total"] += total

    rows = []
    for name, data in totals.items():
        accuracy = round((data["correct"] / data["total"]) * 100, 2) if data["total"] else 0
        rows.append({"name": name, "correct": data["correct"], "total": data["total"], "accuracy": accuracy})
    rows.sort(key=lambda r: r["accuracy"])
    return rows


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


def choose_questions(question_bank, selected_categories, count):
    priority = [q for q in question_bank if q["category"] in selected_categories]
    fallback = [q for q in question_bank if q["category"] not in selected_categories]
    random.shuffle(priority)
    random.shuffle(fallback)
    selected = priority[:count]
    if len(selected) < count:
        selected.extend(fallback[:count - len(selected)])
    random.shuffle(selected)
    return selected[:count]


def save_weak_attempt(score, correct, total, category_label, domain_breakdown, difficulty_breakdown, exam_name, language_code):
    user_email = get_current_user_email()
    if not user_email:
        raise ValueError("No account email saved.")
    payload = {
        "user_email": user_email,
        "mode": "Weak Areas Practice",
        "category": category_label,
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


def reset_weak():
    for key in [
        "weak_started", "weak_submitted", "weak_current_index", "weak_answers", "weak_feedback_shown",
        "weak_saved", "weak_questions", "weak_categories", "weak_exam_name", "weak_language_code",
    ]:
        st.session_state.pop(key, None)
    st.rerun()


st.markdown(
    """
    <style>
    .block-container { max-width:1120px; padding-top:2rem !important; }
    .weak-banner { background:#16325c;color:white;padding:18px 22px;border-radius:8px;font-size:27px;font-weight:700;margin-bottom:18px; }
    .weak-card { border:1px solid #d8dde6;border-radius:8px;padding:20px;background:white;margin-bottom:18px; }
    .small-muted { color:#5f6368;font-size:13px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="weak-banner">Weak Areas Practice</div>', unsafe_allow_html=True)
st.caption(f"App version: {APP_VERSION}")

user_email = get_current_user_email()
if not user_email:
    st.warning("Please log in from the Account page before using Weak Areas Practice.")
    st.stop()

profile = fetch_user_profile(user_email)
status = str(profile.get("subscription_status") or "free").strip().lower()
if status not in PAID_STATUS_VALUES:
    st.warning("Weak Areas Practice is a premium feature.")
    st.info("Upgrade to unlock weak-area practice by certification.")
    st.stop()

language_code = str(profile.get("preferred_language_code") or "en").strip().lower()
st.success(f"Account: {user_email} ✅ | Access: {status} | Preferred language: {language_label(language_code)}")

certifications = fetch_certifications()
if not certifications:
    st.error("No active certifications found. Add a certification in Supabase first.")
    st.stop()
exam_names = [c["exam_name"] for c in certifications]
display_by_exam = {c["exam_name"]: c.get("display_name") or c["exam_name"] for c in certifications}

if not st.session_state.get("weak_started", False):
    selected_exam = st.selectbox(
        "Choose certification",
        exam_names,
        format_func=lambda x: display_by_exam.get(x, x),
        key="weak_selected_exam_name",
    )
    domains = fetch_domains(selected_exam)
    question_bank = fetch_question_bank(selected_exam, language_code)
    attempts = fetch_attempts(user_email, selected_exam, language_code)

    if not question_bank:
        st.error(f"No approved questions found for {display_by_exam.get(selected_exam, selected_exam)} in {language_label(language_code)}.")
        st.stop()

    available_categories = [d for d in domains if any(q["category"] == d for q in question_bank)]
    extra_categories = sorted({q["category"] for q in question_bank if q["category"] not in available_categories})
    available_categories.extend(extra_categories)

    weak_domains = aggregate_domains(attempts)
    st.header("Build Practice from Your Weak Areas")

    if not attempts or not weak_domains:
        st.warning("No weak-area data found yet for this certification/language. Complete a mock exam or practice set first, or manually choose a domain.")
        recommended_categories = available_categories[:1]
    else:
        recommended_categories = [r["name"] for r in weak_domains[:2] if r["name"] in available_categories] or available_categories[:1]
        st.success("Your weakest domains were detected from saved attempts for this certification.")
        st.subheader("Weakest Domains")
        st.dataframe(pd.DataFrame(weak_domains[:5]).rename(columns={"name": "Domain", "accuracy": "Accuracy %", "correct": "Correct", "total": "Total"}), use_container_width=True, hide_index=True)

    selected_categories = st.multiselect("Practice these domain(s):", available_categories, default=recommended_categories)
    question_count = st.selectbox("Number of questions:", QUESTION_COUNT_OPTIONS, index=0)

    if st.button("Start Weak Areas Practice", type="primary"):
        if not selected_categories:
            st.error("Choose at least one category.")
            st.stop()
        selected_questions = choose_questions(question_bank, selected_categories, int(question_count))
        if not selected_questions:
            st.error("No questions found for these settings.")
            st.stop()
        for q in selected_questions:
            random.shuffle(q["options"])
        st.session_state.weak_questions = selected_questions
        st.session_state.weak_categories = selected_categories
        st.session_state.weak_exam_name = selected_exam
        st.session_state.weak_language_code = language_code
        st.session_state.weak_started = True
        st.session_state.weak_submitted = False
        st.session_state.weak_current_index = 0
        st.session_state.weak_answers = {}
        st.session_state.weak_feedback_shown = False
        st.session_state.weak_saved = False
        st.rerun()

elif not st.session_state.get("weak_submitted", False):
    questions = st.session_state.weak_questions
    q_index = st.session_state.get("weak_current_index", 0)
    q = questions[q_index]
    st.markdown(f"""
    <div class="weak-card">
        <strong>Question:</strong> {q_index + 1} of {len(questions)}<br>
        <span class="small-muted">Certification: {display_by_exam.get(st.session_state.weak_exam_name, st.session_state.weak_exam_name)} | Domain: {q['category']} | Difficulty: {q['difficulty'].title()}</span>
    </div>
    """, unsafe_allow_html=True)
    st.progress((q_index + 1) / len(questions))
    st.subheader(q["question"])

    current_answer = st.session_state.get("weak_answers", {}).get(q_index, [])
    selected_ids = []
    if q["type"] == "multiple":
        count_text = q.get("select_count") or len(q["correct_ids"])
        st.warning(f"Choose {count_text} answers.")
        for opt in q["options"]:
            if st.checkbox(opt["text"], value=opt["id"] in current_answer, key=f"weak_{q_index}_{opt['id']}"):
                selected_ids.append(opt["id"])
    else:
        option_labels = [opt["text"] for opt in q["options"]]
        id_by_text = {opt["text"]: opt["id"] for opt in q["options"]}
        current_text = next((opt["text"] for opt in q["options"] if current_answer and opt["id"] == current_answer[0]), None)
        selected_text = st.radio("Choose one answer.", option_labels, index=option_labels.index(current_text) if current_text in option_labels else None, key=f"weak_radio_{q_index}")
        if selected_text:
            selected_ids = [id_by_text[selected_text]]

    if selected_ids:
        st.session_state.weak_answers[q_index] = selected_ids
    elif q_index in st.session_state.get("weak_answers", {}):
        del st.session_state.weak_answers[q_index]

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Previous") and q_index > 0:
            st.session_state.weak_current_index -= 1
            st.session_state.weak_feedback_shown = False
            st.rerun()
    with col2:
        if st.button("Show Explanation"):
            st.session_state.weak_feedback_shown = True
    with col3:
        if q_index < len(questions) - 1:
            if st.button("Next", type="primary"):
                st.session_state.weak_current_index += 1
                st.session_state.weak_feedback_shown = False
                st.rerun()
        else:
            if st.button("Submit Practice", type="primary"):
                st.session_state.weak_submitted = True
                st.rerun()

    if st.session_state.get("weak_feedback_shown", False):
        user_ids = st.session_state.weak_answers.get(q_index, [])
        st.success("Correct") if is_correct(user_ids, q["correct_ids"]) else st.error("Incorrect")
        correct_texts = [opt["text"] for opt in q["options"] if opt["id"] in q["correct_ids"]]
        selected_texts = [opt["text"] for opt in q["options"] if opt["id"] in user_ids]
        st.write("Your answer: " + (", ".join(selected_texts) if selected_texts else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_texts))
        st.info(q["explanation"])

else:
    questions = st.session_state.weak_questions
    answers = st.session_state.weak_answers
    correct = sum(1 for i, q in enumerate(questions) if is_correct(answers.get(i, []), q["correct_ids"]))
    total = len(questions)
    score = round((correct / total) * 100, 2) if total else 0
    domain_breakdown = build_breakdown(questions, answers, "category")
    difficulty_breakdown = build_breakdown(questions, answers, "difficulty")
    category_label = ", ".join(st.session_state.get("weak_categories", [])) or "Weak Areas"

    st.header("Weak Areas Practice Results")
    c1, c2, c3 = st.columns(3)
    c1.metric("Score", f"{score}%")
    c2.metric("Correct", f"{correct} / {total}")
    c3.metric("Focus Domains", len(st.session_state.get("weak_categories", [])))

    if not st.session_state.get("weak_saved", False):
        try:
            save_weak_attempt(score, correct, total, category_label, domain_breakdown, difficulty_breakdown, st.session_state.weak_exam_name, st.session_state.weak_language_code)
            st.session_state.weak_saved = True
            st.success("Weak areas practice attempt saved to progress tracking ✅")
        except Exception as exc:
            st.error(f"Practice result was calculated, but saving to Supabase failed: {exc}")

    st.divider()
    st.subheader("Breakdown by Domain")
    for name, data in domain_breakdown.items():
        pct = round((data["correct"] / data["total"]) * 100, 2) if data["total"] else 0
        st.write(f"**{name}:** {data['correct']} / {data['total']} correct ({pct}%)")

    st.divider()
    st.header("Answer Review")
    for i, q in enumerate(questions):
        user_ids = answers.get(i, [])
        result_correct = is_correct(user_ids, q["correct_ids"])
        st.success(f"Question {i + 1} — Correct") if result_correct else st.error(f"Question {i + 1} — Incorrect")
        selected_texts = [opt["text"] for opt in q["options"] if opt["id"] in user_ids]
        correct_texts = [opt["text"] for opt in q["options"] if opt["id"] in q["correct_ids"]]
        st.caption(f"Domain: {q['category']} | Difficulty: {q['difficulty'].title()}")
        st.write(q["question"])
        st.write("Your answer: " + (", ".join(selected_texts) if selected_texts else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_texts))
        st.info(q["explanation"])
        st.divider()

    if st.button("Start New Weak Areas Practice", type="primary"):
        reset_weak()
