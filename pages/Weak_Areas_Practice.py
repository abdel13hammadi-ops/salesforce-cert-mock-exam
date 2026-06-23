import json
import random
from collections import defaultdict
from datetime import datetime, timezone
import time

import pandas as pd
import streamlit as st
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.access_control import (
    get_supabase_admin_client,
    get_user_access_level,
    render_app_chrome,
    get_current_user_email as shared_get_current_user_email,
    require_login,
    has_premium_access,
)

from utils.version import APP_VERSION
QUESTION_COUNT_OPTIONS = [10, 20, 30]

st.set_page_config(page_title="Weak Areas Practice", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()


# SESSION_TIMEOUT_APPLIED
enforce_session_timeout()
show_session_expired_notice()


@st.cache_resource
def get_supabase_client():
    """Use the centralized admin client so Render env vars and Streamlit secrets both work."""
    return get_supabase_admin_client()


def get_current_user_email():
    return shared_get_current_user_email()


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
def fetch_active_certifications():
    """Paid/admin users can access every active certification when no per-cert rows exist."""
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
        .select(
            "id, exam_name, language_code, category, difficulty, question_text, "
            "question_type, select_count, explanation, is_active, is_exam_eligible, "
            "quality_status, question_family_id, cognitive_level, concept_key, "
            "content_version, external_key"
        )
        .eq("exam_name", exam_name)
        .eq("language_code", language_code)
        .eq("is_active", True)
        .eq("is_exam_eligible", True)
        .eq("quality_status", "approved")
        .eq("practice_eligible", True)
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
            # Prospective metadata — captured at attempt time
            "question_family_id": q.get("question_family_id"),
            "cognitive_level": q.get("cognitive_level"),
            "concept_key": q.get("concept_key"),
            "content_version": q.get("content_version"),
            "external_key": q.get("external_key"),
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


def _clamped_seconds(value, max_seconds=7200):
    try:
        seconds = float(value or 0)
    except Exception:
        return 0.0
    if seconds < 0:
        return 0.0
    return round(min(seconds, max_seconds), 3)


def reset_weak_timing():
    st.session_state.weak_question_time_spent = {}
    st.session_state.weak_question_entered_at = time.time()
    st.session_state.weak_timing_index = int(st.session_state.get("weak_current_index") or 0)


def record_current_weak_time():
    questions = st.session_state.get("weak_questions") or []
    if not questions:
        return

    try:
        idx = int(st.session_state.get("weak_timing_index", st.session_state.get("weak_current_index", 0)) or 0)
    except Exception:
        idx = 0

    now = time.time()
    entered_at = st.session_state.get("weak_question_entered_at")
    if entered_at is not None and 0 <= idx < len(questions):
        elapsed = _clamped_seconds(now - float(entered_at))
        existing = float((st.session_state.get("weak_question_time_spent") or {}).get(idx, 0) or 0)
        st.session_state.weak_question_time_spent[idx] = round(existing + elapsed, 3)

    st.session_state.weak_question_entered_at = now
    st.session_state.weak_timing_index = int(st.session_state.get("weak_current_index") or 0)


def move_to_weak_question(new_index):
    record_current_weak_time()
    st.session_state.weak_current_index = int(new_index)
    st.session_state.weak_question_entered_at = time.time()
    st.session_state.weak_timing_index = int(new_index)


def option_texts_by_id(question, ids):
    ids = {str(v) for v in (ids or [])}
    return [opt.get("text", "") for opt in question.get("options", []) if str(opt.get("id")) in ids]


def build_question_attempt_rows(exam_attempt_id, user_email, questions, answers):
    from utils.readiness_persistence import build_attempt_metadata  # noqa: PLC0415
    question_times = st.session_state.get("weak_question_time_spent") or {}
    rows = []
    for idx, q in enumerate(questions or []):
        selected_ids = [str(v) for v in (answers.get(idx, []) if answers else [])]
        correct_ids = [str(v) for v in q.get("correct_ids", [])]
        row = {
            "exam_attempt_id": exam_attempt_id,
            "question_id": int(q.get("id")),
            "user_email": user_email,
            "exam_name": q.get("exam_name") or st.session_state.get("weak_exam_name"),
            "language_code": q.get("language_code") or st.session_state.get("weak_language_code") or "en",
            "category": q.get("category") or "Uncategorized",
            "difficulty": str(q.get("difficulty") or "medium").strip().lower(),
            "selected_options": option_texts_by_id(q, selected_ids),
            "correct_options": option_texts_by_id(q, correct_ids),
            "is_correct": is_correct(selected_ids, correct_ids),
            "time_spent_seconds": _clamped_seconds(question_times.get(idx, 0)),
            "answered_at": datetime.now(timezone.utc).isoformat(),
        }
        row.update(build_attempt_metadata(q))
        rows.append(row)
    return rows


def save_question_attempt_rows(supabase, rows):
    if not rows:
        return
    for start in range(0, len(rows), 100):
        supabase.table("question_attempts").insert(rows[start:start + 100]).execute()


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
    supabase = get_supabase_client()
    result = supabase.table("exam_attempts").insert(payload).execute()
    inserted_rows = getattr(result, "data", None) or []
    exam_attempt_id = inserted_rows[0].get("id") if inserted_rows else None
    if exam_attempt_id:
        question_rows = build_question_attempt_rows(
            exam_attempt_id=exam_attempt_id,
            user_email=user_email,
            questions=st.session_state.get("weak_questions", []),
            answers=st.session_state.get("weak_answers", {}),
        )
        save_question_attempt_rows(supabase, question_rows)


def reset_weak():
    for key in [
        "weak_started", "weak_submitted", "weak_current_index", "weak_answers", "weak_feedback_shown",
        "weak_saved", "weak_questions", "weak_categories", "weak_exam_name", "weak_language_code",
        "weak_question_time_spent", "weak_question_entered_at", "weak_timing_index",
    ]:
        st.session_state.pop(key, None)
    st.rerun()


def render_locked_weak_areas_preview(user_email, language_code, access_level):
    """Show a premium preview for free users without exposing real weak-area practice questions."""
    st.markdown(
        """
        <div class="weak-card locked-preview-card">
            <div class="locked-eyebrow">Premium weak-area preview</div>
            <h2 style="margin:0 0 8px 0;">Practice where your scores are actually leaking points.</h2>
            <p class="small-muted" style="font-size:15px;line-height:1.5;margin-bottom:0;">
                Weak Areas Practice builds targeted practice sets from your saved mock exams and practice history.
                This preview uses sample data only and does not expose the paid question bank.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.info(f"Signed in as {user_email} | Access: {access_level} | Preferred language: {language_label(language_code)}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Sample Weak Domains", "3")
    c2.metric("Sample Practice Set", "10 questions")
    c3.metric("Mode", "Targeted")

    left, right = st.columns([1.15, 1])
    with left:
        st.markdown(
            """
            <div class="weak-card">
                <h3 style="margin-top:0;">What premium users can do</h3>
                <ul style="line-height:1.8;margin-bottom:0;">
                    <li>Automatically detect weak domains from saved attempts.</li>
                    <li>Build practice sets focused on the lowest-scoring domains.</li>
                    <li>Review explanations after each question.</li>
                    <li>Save weak-area practice results into My Progress.</li>
                    <li>Use results to improve readiness scoring over time.</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with right:
        sample_domains = pd.DataFrame(
            [
                {"Sample Weak Domain": "Security and Access", "Accuracy %": 58, "Priority": "High"},
                {"Sample Weak Domain": "Automation and Process", "Accuracy %": 63, "Priority": "High"},
                {"Sample Weak Domain": "Data Management", "Accuracy %": 69, "Priority": "Medium"},
            ]
        )
        st.markdown('<div class="weak-card sample-panel"><h3 style="margin-top:0;">Sample weak-area signal</h3>', unsafe_allow_html=True)
        st.dataframe(sample_domains, use_container_width=True, hide_index=True)
        st.markdown('<span class="locked-pill">Locked preview</span></div>', unsafe_allow_html=True)

    st.warning("Weak Areas Practice is locked on free accounts. Complete a free mock exam now, or unlock premium access to generate real weak-area drills.")


st.markdown(
    """
    <style>
    .block-container { max-width:1120px; padding-top:2rem !important; }
    .weak-banner { background:#16325c;color:white;padding:18px 22px;border-radius:8px;font-size:27px;font-weight:700;margin-bottom:18px; }
    .weak-card { border:1px solid #d8dde6;border-radius:8px;padding:20px;background:white;margin-bottom:18px; }
    .locked-preview-card { border:1px solid #c9d7f5;background:linear-gradient(135deg,#ffffff 0%,#f4f8ff 100%); }
    .locked-eyebrow { color:#1b4d89;font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px; }
    .sample-panel { background:#f8fafc; }
    .locked-pill { display:inline-block;margin-top:10px;padding:6px 10px;border-radius:999px;background:#e8f0fe;color:#1b4d89;font-size:12px;font-weight:700; }
    .small-muted { color:#5f6368;font-size:13px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="weak-banner">Weak Areas Practice</div>', unsafe_allow_html=True)
st.caption(f"App Version: {APP_VERSION}")

user_email = require_login()

profile = fetch_user_profile(user_email)
access_level = get_user_access_level(user_email)
language_code = str(profile.get("preferred_language_code") or "en").strip().lower()

if not has_premium_access(user_email):
    render_locked_weak_areas_preview(user_email, language_code, access_level)
    st.stop()

st.success(f"Account: {user_email} ✅ | Access: {access_level} | Preferred language: {language_label(language_code)}")

certifications = fetch_user_certifications(user_email)
if not certifications:
    certifications = fetch_active_certifications()

if not certifications:
    st.error("No active certifications are configured.")
    st.info("Admin setup required: add active rows in the certifications table.")
    st.stop()

exam_names = [c["exam_name"] for c in certifications if c.get("exam_name")]
display_by_exam = {c["exam_name"]: c.get("display_name") or c["exam_name"] for c in certifications if c.get("exam_name")}

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
        reset_weak_timing()
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
            move_to_weak_question(q_index - 1)
            st.session_state.weak_feedback_shown = False
            st.rerun()
    with col2:
        if st.button("Show Explanation"):
            st.session_state.weak_feedback_shown = True
    with col3:
        if q_index < len(questions) - 1:
            if st.button("Next", type="primary"):
                move_to_weak_question(q_index + 1)
                st.session_state.weak_feedback_shown = False
                st.rerun()
        else:
            if st.button("Submit Practice", type="primary"):
                record_current_weak_time()
                st.session_state.weak_submitted = True
                st.rerun()

    if st.session_state.get("weak_feedback_shown", False):
        user_ids = st.session_state.weak_answers.get(q_index, [])
        if is_correct(user_ids, q["correct_ids"]):
            st.success("Correct")
        else:
            st.error("Incorrect")
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
        record_current_weak_time()
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
        if result_correct:
            st.success(f"Question {i + 1} — Correct")
        else:
            st.error(f"Question {i + 1} — Incorrect")
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
