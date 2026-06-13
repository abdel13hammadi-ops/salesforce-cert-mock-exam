import json
from datetime import datetime

import pandas as pd
import streamlit as st

import sys
from pathlib import Path

_file = Path(__file__).resolve()
_root = _file.parent.parent if _file.parent.name == "pages" else _file.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import path_setup

path_setup.ensure_project_root(__file__)

from utils.access_control import (
    render_app_chrome,
    require_premium_access,
    get_available_certifications,
    get_supabase_client,
    get_supabase_public_client,
    get_user_profile,
    ADMIN_EXAM_NAME,
    BA_EXAM_NAME,
    FALLBACK_CERTIFICATIONS,
)
from utils.readiness import calculate_readiness, readiness_methodology_text

APP_VERSION = "MY_PROGRESS_V11_TWO_CERT_DROPDOWN"
ALL_CERTIFICATIONS = "All certifications"

st.set_page_config(page_title="My Progress", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()
user_email = require_premium_access("My Progress and Overall Readiness")


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def parse_timestamp(value) -> datetime:
    if value is None or str(value).strip() == "":
        return datetime.min
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def sort_attempts(attempts: list) -> list:
    """completed_at desc nulls last, started_at desc nulls last, id desc."""
    return sorted(
        attempts,
        key=lambda row: (
            parse_timestamp(row.get("completed_at")),
            parse_timestamp(row.get("started_at")),
            int(row.get("id") or 0),
        ),
        reverse=True,
    )


def attempt_correct_count(attempt: dict):
    """Prefer correct_answers; fallback to correct_count."""
    if attempt.get("correct_answers") is not None:
        return attempt.get("correct_answers")
    return attempt.get("correct_count")


def load_attempts(user_email: str, exam_name: str | None = None):
    """Load exam attempts for the logged-in user. Returns (rows, error_message)."""
    normalized_email = normalize_email(user_email)
    if not normalized_email:
        return [], "No logged-in email available."

    try:
        supabase = get_supabase_client()
        query = (
            supabase.table("exam_attempts")
            .select(
                "id,user_email,mode,category,score,correct_answers,correct_count,total_questions,"
                "domain_breakdown,difficulty_breakdown,completed_at,started_at,exam_name,language_code"
            )
            .ilike("user_email", normalized_email)
        )

        if exam_name and exam_name != ALL_CERTIFICATIONS:
            query = query.eq("exam_name", exam_name)

        result = query.execute()
        rows = result.data or []

        rows = [
            row
            for row in rows
            if normalize_email(row.get("user_email")) == normalized_email
        ]
        rows = sort_attempts(rows)
        return rows, None
    except Exception as exc:
        return [], str(exc)


@st.cache_data(ttl=60)
def fetch_user_profile(email):
    return get_user_profile(email) or {}


@st.cache_data(ttl=60)
def fetch_languages():
    try:
        result = (
            get_supabase_public_client()
            .table("languages")
            .select("language_code,language_name,native_name,is_active,display_order")
            .eq("is_active", True)
            .order("display_order")
            .execute()
        )
        return result.data or []
    except Exception:
        return [{"language_code": "en", "language_name": "English", "native_name": "English"}]


@st.cache_data(ttl=60)
def fetch_user_certifications(user_email):
    return get_available_certifications()


def build_certification_catalog(user_email: str, all_attempts: list):
    """Use certifications table when available; otherwise fall back to known exams and attempt data."""
    db_certifications = fetch_user_certifications(user_email)
    catalog = {c["exam_name"]: dict(c) for c in db_certifications if c.get("exam_name")}

    for fallback in FALLBACK_CERTIFICATIONS:
        catalog.setdefault(fallback["exam_name"], dict(fallback))

    for attempt in all_attempts:
        exam_name = attempt.get("exam_name")
        if not exam_name:
            continue
        catalog.setdefault(
            exam_name,
            {
                "exam_name": exam_name,
                "display_name": exam_name,
                "passing_score": 65 if exam_name == ADMIN_EXAM_NAME else 72 if exam_name == BA_EXAM_NAME else 65,
                "question_count": 60,
            },
        )

    certifications = sorted(catalog.values(), key=lambda row: row.get("display_name") or row.get("exam_name") or "")
    return certifications, bool(db_certifications)


def filter_attempts_by_exam(all_attempts: list, exam_name: str | None):
    if not exam_name or exam_name == ALL_CERTIFICATIONS:
        return all_attempts
    return [row for row in all_attempts if row.get("exam_name") == exam_name]


def language_label(language_code):
    languages = fetch_languages()
    for lang in languages:
        if lang.get("language_code") == language_code:
            native = lang.get("native_name") or lang.get("language_name") or language_code
            return f"{native} ({language_code})"
    return language_code or "Not set"


@st.cache_data(ttl=60)
def fetch_domain_weights(exam_name):
    result = (
        get_supabase_public_client()
        .table("certification_domains")
        .select("domain_name, weight")
        .eq("exam_name", exam_name)
        .eq("is_active", True)
        .execute()
    )
    return {row.get("domain_name"): float(row.get("weight") or 0) for row in (result.data or []) if row.get("domain_name")}


@st.cache_data(ttl=60)
def fetch_question_bank_total(exam_name, language_code):
    result = (
        get_supabase_client()
        .table("questions")
        .select("id", count="exact")
        .eq("exam_name", exam_name)
        .eq("language_code", language_code)
        .eq("is_active", True)
        .eq("is_exam_eligible", True)
        .eq("quality_status", "approved")
        .execute()
    )
    return int(result.count or 0)


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


def make_domain_table(attempts):
    totals = {}
    for attempt in attempts:
        breakdown = normalize_breakdown(attempt.get("domain_breakdown"))
        for name, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            correct = int(data.get("correct", 0) or 0)
            total = int(data.get("total", 0) or 0)
            if name not in totals:
                totals[name] = {"correct": 0, "total": 0}
            totals[name]["correct"] += correct
            totals[name]["total"] += total

    rows = []
    for name, data in totals.items():
        total = data["total"]
        correct = data["correct"]
        percent = round((correct / total) * 100, 2) if total else 0
        rows.append({"Domain": name, "Correct": correct, "Total": total, "Accuracy %": percent})
    return pd.DataFrame(rows).sort_values("Accuracy %") if rows else pd.DataFrame()


st.title("My Progress")
st.caption(f"App version: {APP_VERSION}")

profile = fetch_user_profile(user_email)
preferred_language = str(profile.get("preferred_language_code") or "en").strip().lower()
st.info(f"Account: {user_email} | Preferred language: {language_label(preferred_language)}")

all_attempts, query_error = load_attempts(user_email, None)

if query_error:
    st.error(f"Could not load exam attempts: {query_error}")
    st.stop()

certifications = get_available_certifications()
exam_names = [c["exam_name"] for c in certifications if c.get("exam_name")]
display_by_exam = {
    c["exam_name"]: c.get("display_name") or c["exam_name"] for c in certifications if c.get("exam_name")
}

if st.session_state.get("my_progress_exam_name") not in exam_names:
    recent_exam = all_attempts[0].get("exam_name") if all_attempts else None
    st.session_state.my_progress_exam_name = recent_exam if recent_exam in exam_names else exam_names[0]

selected_exam = st.selectbox(
    "Choose certification for progress",
    exam_names,
    format_func=lambda x: display_by_exam.get(x, x),
    key="my_progress_exam_name",
)

attempts = [row for row in all_attempts if row.get("exam_name") == selected_exam]

with st.expander("Debug (temporary)", expanded=False):
    st.write(f"current_user_email: {normalize_email(user_email)}")
    st.write(f"selected_exam_name: {selected_exam}")
    st.write(f"number_of_attempts_returned: {len(attempts)}")
    st.write(f"total_attempts_all_certs: {len(all_attempts)}")
    st.write(f"query_error: {query_error or 'none'}")

if not attempts:
    st.info("No exam attempts found.")
    st.stop()

readiness_exam = selected_exam
domain_weights = fetch_domain_weights(readiness_exam)
readiness_language = str(
    (attempts[0].get("language_code") if attempts else None) or preferred_language
).strip().lower()
question_bank_total = fetch_question_bank_total(readiness_exam, readiness_language)
passing_score = float(
    next(
        (c.get("passing_score") or (72 if readiness_exam == BA_EXAM_NAME else 65) for c in certifications if c.get("exam_name") == readiness_exam),
        72 if readiness_exam == BA_EXAM_NAME else 65,
    )
)
expected_question_count = int(
    next((c.get("question_count") or 60 for c in certifications if c.get("exam_name") == readiness_exam), 60)
)
readiness = calculate_readiness(
    attempts=attempts,
    passing_score=passing_score,
    domain_weights=domain_weights,
    expected_question_count=expected_question_count,
    question_bank_total=question_bank_total,
)

st.header("Overall Readiness")
st.caption("This is a readiness estimate, not a guarantee of passing. It is a study-planning signal.")
r1, r2, r3, r4 = st.columns(4)
r1.metric("Readiness Score", f"{readiness['score']}%")
r2.metric("Status", readiness["label"])
r3.metric("Confidence", readiness["confidence"])
r4.metric("Passing Score", f"{passing_score}%")

progress_value = max(0, min(float(readiness["score"]) / 100, 1))
st.progress(progress_value)
st.info(readiness["recommendation"])

with st.expander("How readiness is calculated", expanded=False):
    st.write(readiness_methodology_text())
    st.write("- 50% Recent Mock Performance")
    st.write("- 30% Weighted Domain Readiness")
    st.write("- 10% Consistency")
    st.write("- 10% Practice Volume")

if readiness.get("domain_scores"):
    st.subheader("Domain Readiness")
    rows = []
    for domain, data in readiness["domain_scores"].items():
        pct = float(data.get("percent") or 0)
        risk = "High Risk" if pct < passing_score else "On Track" if pct < passing_score + 8 else "Strong"
        rows.append(
            {
                "Domain": domain,
                "Accuracy %": pct,
                "Correct": data.get("correct"),
                "Total": data.get("total"),
                "Status": risk,
            }
        )
    st.dataframe(pd.DataFrame(rows).sort_values("Accuracy %"), use_container_width=True, hide_index=True)

st.divider()
scores = [float(a.get("score") or 0) for a in attempts]
latest_score = float(attempts[0].get("score") or 0)
average_score = round(sum(scores) / len(scores), 2) if scores else 0
best_score = round(max(scores), 2) if scores else 0
attempt_count = len(attempts)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Latest Score", f"{latest_score}%")
c2.metric("Average Score", f"{average_score}%")
c3.metric("Best Score", f"{best_score}%")
c4.metric("Attempts", attempt_count)

st.divider()
st.header("Weak Areas by Domain")
domain_df = make_domain_table(attempts)
if domain_df.empty:
    st.warning("No domain breakdown data saved yet.")
else:
    st.dataframe(domain_df, use_container_width=True, hide_index=True)
    weakest = domain_df.iloc[0]
    st.info(f"Weakest domain: {weakest['Domain']} ({weakest['Accuracy %']}%)")

st.divider()
st.header("Attempt History")
history_rows = []
for attempt in attempts:
    completed = attempt.get("completed_at") or attempt.get("started_at") or "Not recorded"
    history_rows.append(
        {
            "Attempt ID": attempt.get("id"),
            "Exam": attempt.get("exam_name"),
            "Completed At": completed,
            "Started At": attempt.get("started_at") or "Not recorded",
            "Mode": attempt.get("mode"),
            "Category": attempt.get("category"),
            "Score %": attempt.get("score"),
            "Correct": attempt_correct_count(attempt),
            "Total": attempt.get("total_questions"),
            "Language": attempt.get("language_code") or "Not set",
        }
    )
st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)

st.divider()
st.header("Recommendation")
if not domain_df.empty:
    weakest = domain_df.iloc[0]
    label = display_by_exam.get(selected_exam, selected_exam)
    st.write(f"Focus next on **{weakest['Domain']}** for **{label}**.")
else:
    st.write("Complete more attempts to generate recommendations.")
