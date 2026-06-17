import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from utils.access_control import (
    render_app_chrome,
    get_current_user_email,
    has_premium_access,
    get_supabase_client,
)
from utils.readiness import calculate_readiness, readiness_methodology_text

APP_VERSION = "MY_PROGRESS_V9_TZ_DISPLAY"

st.set_page_config(page_title="My Progress", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()


def _safe_lower(value: Any, default: str = "") -> str:
    return str(value or default).strip().lower()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def normalize_breakdown(value: Any) -> Dict[str, Any]:
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


def _parse_dt(value: Any) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def format_user_datetime(value: Any, preferred_timezone: str = "UTC") -> str:
    if not value:
        return "Not recorded"

    parsed = _parse_dt(value)
    if parsed == datetime.min.replace(tzinfo=timezone.utc):
        return str(value)

    tz_name = str(preferred_timezone or "UTC").strip() or "UTC"
    try:
        user_tz = ZoneInfo(tz_name)
    except Exception:
        user_tz = timezone.utc
        tz_name = "UTC"

    local_dt = parsed.astimezone(user_tz)
    # Example: Jun 17, 2026, 12:06 AM EDT
    return local_dt.strftime("%b %d, %Y, %I:%M %p %Z").replace(", 0", ", ", 1)


def sort_attempts(attempts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Your exam_attempts table has completed_at/started_at, not created_at.
    return sorted(
        attempts or [],
        key=lambda a: (
            _parse_dt(a.get("completed_at")),
            _parse_dt(a.get("started_at")),
            _safe_int(a.get("id"), 0),
        ),
        reverse=True,
    )


@st.cache_data(ttl=60)
def fetch_user_profile(email: str) -> Dict[str, Any]:
    if not email:
        return {}
    try:
        result = (
            get_supabase_client()
            .table("app_users")
            .select("email,full_name,subscription_status,preferred_language_code,preferred_timezone")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        return (result.data or [{}])[0]
    except Exception:
        return {}


@st.cache_data(ttl=60)
def fetch_certifications() -> List[Dict[str, Any]]:
    try:
        result = (
            get_supabase_client()
            .table("certifications")
            .select("exam_name,display_name,certification_code,passing_score,time_limit_minutes,question_count,is_active")
            .eq("is_active", True)
            .order("display_name")
            .execute()
        )
        rows = result.data or []
    except Exception:
        rows = []

    if rows:
        return rows

    # Safe fallback so progress/readiness still renders if certification metadata is temporarily unavailable.
    return [
        {
            "exam_name": "Salesforce Certified Platform Administrator",
            "display_name": "Salesforce Certified Platform Administrator",
            "passing_score": 68,
            "question_count": 60,
            "is_active": True,
        },
        {
            "exam_name": "Salesforce Certified Business Analyst",
            "display_name": "Salesforce Certified Business Analyst",
            "passing_score": 72,
            "question_count": 60,
            "is_active": True,
        },
    ]


@st.cache_data(ttl=60)
def fetch_domain_weights(exam_name: str) -> Dict[str, float]:
    if not exam_name:
        return {}
    try:
        result = (
            get_supabase_client()
            .table("certification_domains")
            .select("domain_name,weight")
            .eq("exam_name", exam_name)
            .eq("is_active", True)
            .execute()
        )
        weights = {}
        for row in result.data or []:
            name = row.get("domain_name")
            if name:
                weights[str(name)] = _safe_float(row.get("weight"), 0.0)
        return weights
    except Exception:
        return {}


@st.cache_data(ttl=60)
def load_attempts(user_email: str, exam_name: str | None = None) -> Dict[str, Any]:
    """Load attempts for the logged-in email.

    Important schema reality:
    - exam_attempts has completed_at and started_at
    - exam_attempts does NOT have created_at
    - historical attempts are keyed by user_email, not auth_user_id
    """
    if not user_email:
        return {"rows": [], "error": "Missing user email."}

    try:
        query = (
            get_supabase_client()
            .table("exam_attempts")
            .select(
                "id,user_email,mode,category,score,total_questions,correct_count,correct_answers,"
                "started_at,completed_at,domain_breakdown,difficulty_breakdown,exam_name,language_code"
            )
            .ilike("user_email", user_email)
        )
        if exam_name:
            query = query.eq("exam_name", exam_name)
        result = query.execute()
        return {"rows": sort_attempts(result.data or []), "error": None}
    except Exception as exc:
        return {"rows": [], "error": str(exc)}


def get_correct_count(attempt: Dict[str, Any]) -> int:
    # Your table has correct_count NULL on old rows; correct_answers is the reliable value.
    if attempt.get("correct_answers") is not None:
        return _safe_int(attempt.get("correct_answers"), 0)
    return _safe_int(attempt.get("correct_count"), 0)


def build_domain_table(attempts: List[Dict[str, Any]]) -> pd.DataFrame:
    totals: Dict[str, Dict[str, float]] = {}
    for attempt in attempts:
        breakdown = normalize_breakdown(attempt.get("domain_breakdown"))
        for name, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            correct = _safe_float(data.get("correct"), 0.0)
            total = _safe_float(data.get("total"), 0.0)
            if total <= 0:
                continue
            if name not in totals:
                totals[name] = {"correct": 0.0, "total": 0.0}
            totals[name]["correct"] += correct
            totals[name]["total"] += total

    rows = []
    for name, data in totals.items():
        total = data["total"]
        correct = data["correct"]
        accuracy = round((correct / total) * 100, 2) if total else 0.0
        rows.append({"Domain": name, "Correct": int(correct), "Total": int(total), "Accuracy %": accuracy})

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Accuracy %", ascending=True)
    return df


def render_readiness_card(readiness: Dict[str, Any], passing_score: float, selected_exam: str) -> None:
    st.header("Overall Readiness")
    st.caption("This is a study-planning estimate, not a pass guarantee.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Readiness Score", f"{round(_safe_float(readiness.get('score')), 2)}%")
    c2.metric("Status", readiness.get("label", "Not Enough Data"))
    c3.metric("Confidence", readiness.get("confidence", "No Data"))
    c4.metric("Questions Practiced", _safe_int(readiness.get("total_attempted"), 0))

    st.info(readiness_methodology_text())
    st.warning("Readiness is not a guarantee of passing. It combines recent mock performance, weighted domain performance, consistency, and practice volume.")

    st.subheader("Readiness Components")
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Recent Mock", f"{readiness.get('recent_mock_score', 0)}%")
    r2.metric("Domain Readiness", f"{readiness.get('weighted_domain_score', 0)}%")
    r3.metric("Consistency", f"{readiness.get('consistency_score', 0)}%")
    r4.metric("Practice Volume", f"{readiness.get('practice_volume_score', 0)}%")

    st.write(readiness.get("recommendation", "Complete more attempts to improve the readiness signal."))

    weak = readiness.get("weak_domains") or []
    strong = readiness.get("strong_domains") or []
    cols = st.columns(2)
    with cols[0]:
        st.subheader("Highest-Risk Areas")
        if weak:
            for item in weak:
                st.write(f"- {item}")
        else:
            st.write("No domain-risk signal yet.")
    with cols[1]:
        st.subheader("Strongest Areas")
        if strong:
            for item in strong:
                st.write(f"- {item}")
        else:
            st.write("No strength signal yet.")


def render_locked_progress_preview(user_email: str, subscription_status: str) -> None:
    st.info(f"Account: {user_email} | Access: {subscription_status}")

    st.warning("My Progress is a premium feature. This preview shows the kind of tracking unlocked with full access.")
    st.markdown(
        """
        Full progress tracking gives you a readiness score, weak-domain breakdown, score trend,
        and attempt history across your mock exams and practice sessions. The preview below uses sample data only.
        """
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sample Readiness", "74%")
    c2.metric("Sample Status", "Borderline Ready")
    c3.metric("Sample Confidence", "Medium")
    c4.metric("Sample Questions", "180")

    st.subheader("Sample Readiness Components")
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Recent Mock", "76%")
    r2.metric("Domain Readiness", "71%")
    r3.metric("Consistency", "68%")
    r4.metric("Practice Volume", "82%")

    st.subheader("Sample Weak Areas")
    sample_domains = pd.DataFrame(
        [
            {"Domain": "Security and Access", "Accuracy %": 58, "Priority": "High"},
            {"Domain": "Automation and Process", "Accuracy %": 63, "Priority": "High"},
            {"Domain": "Data Management", "Accuracy %": 69, "Priority": "Medium"},
        ]
    )
    st.dataframe(sample_domains, use_container_width=True, hide_index=True)

    st.subheader("Sample Recent Attempts")
    sample_attempts = pd.DataFrame(
        [
            {"Completed": "Sample Attempt 3", "Mode": "Timed Mock Exam", "Score %": 76, "Result": "Pass-range"},
            {"Completed": "Sample Attempt 2", "Mode": "Practice by Category", "Score %": 70, "Result": "Needs review"},
            {"Completed": "Sample Attempt 1", "Mode": "Free Mock Exam", "Score %": 64, "Result": "Below target"},
        ]
    )
    st.dataframe(sample_attempts, use_container_width=True, hide_index=True)

    st.info("Complete the Free Preview to sample the exam flow. Full progress tracking unlocks real attempt history, readiness scoring, and weak-area analysis.")


st.title("My Progress")
st.caption(f"App version: {APP_VERSION}")

user_email = get_current_user_email()
if not user_email:
    st.warning("Please log in from the Account page before viewing progress.")
    st.stop()

profile = fetch_user_profile(user_email)
preferred_language = _safe_lower(profile.get("preferred_language_code"), "en") or "en"
preferred_timezone = str(profile.get("preferred_timezone") or st.session_state.get("preferred_timezone") or "UTC").strip() or "UTC"
subscription_status = _safe_lower(profile.get("subscription_status") or st.session_state.get("subscription_status"), "free")

if not has_premium_access(user_email):
    render_locked_progress_preview(user_email, subscription_status)
    st.stop()

st.info(f"Account: {user_email} | Access: {subscription_status} | Preferred language: {preferred_language} | Timezone: {preferred_timezone}")

certifications = fetch_certifications()
exam_names = [c.get("exam_name") for c in certifications if c.get("exam_name")]
display_by_exam = {c.get("exam_name"): c.get("display_name") or c.get("exam_name") for c in certifications}
cert_by_exam = {c.get("exam_name"): c for c in certifications if c.get("exam_name")}

if not exam_names:
    st.error("No active certifications are configured.")
    st.stop()

selected_exam = st.selectbox(
    "Choose certification for progress",
    exam_names,
    format_func=lambda x: display_by_exam.get(x, x),
    key="my_progress_exam_name",
)

attempt_result = load_attempts(user_email, selected_exam)
attempts = attempt_result.get("rows", [])
query_error = attempt_result.get("error")

with st.expander("Progress query diagnostics", expanded=False):
    st.write(f"Current user email: `{user_email}`")
    st.write(f"Selected exam: `{selected_exam}`")
    st.write(f"Attempts returned: `{len(attempts)}`")
    if query_error:
        st.error(query_error)

if query_error:
    st.error("Progress could not be loaded because the database query failed. This is a setup/code issue, not 'no attempts'.")
    st.stop()

if not attempts:
    st.info("No exam attempts found for this certification yet. Complete a mock exam first.")
    st.header("Overall Readiness")
    st.warning("No readiness score yet. Complete at least one full mock exam to generate a readiness estimate.")
    st.info(readiness_methodology_text())
    st.stop()

cert = cert_by_exam.get(selected_exam, {})
passing_score = _safe_float(cert.get("passing_score"), 72 if "Business Analyst" in selected_exam else 68)
expected_question_count = _safe_int(cert.get("question_count"), 60) or 60
domain_weights = fetch_domain_weights(selected_exam)

readiness = calculate_readiness(
    attempts=attempts,
    passing_score=passing_score,
    domain_weights=domain_weights,
    expected_question_count=expected_question_count,
    question_bank_total=None,
)

render_readiness_card(readiness, passing_score, selected_exam)

st.divider()
st.header("Score Summary")
scores = [_safe_float(a.get("score"), 0.0) for a in attempts]
latest_score = scores[0] if scores else 0.0
average_score = round(sum(scores) / len(scores), 2) if scores else 0.0
best_score = round(max(scores), 2) if scores else 0.0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Latest Score", f"{latest_score}%")
c2.metric("Average Score", f"{average_score}%")
c3.metric("Best Score", f"{best_score}%")
c4.metric("Attempts", len(attempts))

trend_rows = []
for attempt in reversed(attempts):
    trend_rows.append(
        {
            "Completed": format_user_datetime(attempt.get("completed_at") or attempt.get("started_at"), preferred_timezone),
            "Score": _safe_float(attempt.get("score"), 0.0),
        }
    )
if trend_rows:
    st.subheader("Score Trend")
    trend_df = pd.DataFrame(trend_rows)
    st.line_chart(trend_df.set_index("Completed"))

st.divider()
st.header("Weak Areas by Domain")
domain_df = build_domain_table(attempts)
if domain_df.empty:
    st.warning("No domain breakdown data saved yet. Future exam attempts should save domain_breakdown for stronger readiness scoring.")
else:
    st.dataframe(domain_df, use_container_width=True, hide_index=True)
    st.bar_chart(domain_df.set_index("Domain")["Accuracy %"])
    weakest = domain_df.iloc[0]
    st.info(f"Weakest domain: {weakest['Domain']} ({weakest['Accuracy %']}%)")

st.divider()
st.header("Attempt History")
history_rows = []
for attempt in attempts:
    history_rows.append(
        {
            "Attempt ID": attempt.get("id"),
            "Completed At": format_user_datetime(attempt.get("completed_at"), preferred_timezone),
            "Started At": format_user_datetime(attempt.get("started_at"), preferred_timezone),
            "Mode": attempt.get("mode"),
            "Category": attempt.get("category"),
            "Score %": attempt.get("score"),
            "Correct": get_correct_count(attempt),
            "Total": attempt.get("total_questions"),
            "Language": attempt.get("language_code"),
        }
    )
st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)
