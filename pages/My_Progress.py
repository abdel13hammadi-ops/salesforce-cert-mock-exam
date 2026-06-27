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
from utils.readiness import (
    build_verified_domain_table_rows,
    build_verified_mock_performance_metrics,
    calculate_readiness,
    readiness_methodology_text,
    select_weakest_verified_domain,
)
from utils.readiness_persistence import extract_captured_bank_size
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.version import APP_VERSION

st.set_page_config(page_title="My Progress", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()

# SESSION_TIMEOUT_APPLIED
enforce_session_timeout()
show_session_expired_notice()


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
                "started_at,completed_at,domain_breakdown,difficulty_breakdown,exam_name,language_code,"
                "eligible_question_bank_size"
            )
            .ilike("user_email", user_email)
        )
        if exam_name:
            query = query.eq("exam_name", exam_name)
        result = query.execute()
        return {"rows": sort_attempts(result.data or []), "error": None}
    except Exception as exc:
        return {"rows": [], "error": str(exc)}


@st.cache_data(ttl=60)
def load_question_attempts(user_email: str, exam_name: str | None = None) -> Dict[str, Any]:
    """Load question-level attempts for the new readiness formula."""
    if not user_email:
        return {"rows": [], "error": "Missing user email."}
    try:
        query = (
            get_supabase_client()
            .table("question_attempts")
            .select(
                "id,exam_attempt_id,question_id,user_email,exam_name,language_code,category,difficulty,"
                "is_correct,time_spent_seconds,answered_at"
            )
            .ilike("user_email", user_email)
        )
        if exam_name:
            query = query.eq("exam_name", exam_name)
        result = query.order("answered_at", desc=True).execute()
        return {"rows": result.data or [], "error": None}
    except Exception as exc:
        return {"rows": [], "error": str(exc)}


@st.cache_data(ttl=60)
def fetch_question_bank_total(exam_name: str, language_code: str) -> int:
    if not exam_name:
        return 0
    try:
        result = (
            get_supabase_client()
            .table("questions")
            .select("id")
            .eq("exam_name", exam_name)
            .eq("language_code", language_code or "en")
            .eq("is_active", True)
            .eq("is_exam_eligible", True)
            .eq("quality_status", "approved")
            .execute()
        )
        return len(result.data or [])
    except Exception:
        return 0


def get_correct_count(attempt: Dict[str, Any]) -> int:
    # Your table has correct_count NULL on old rows; correct_answers is the reliable value.
    if attempt.get("correct_answers") is not None:
        return _safe_int(attempt.get("correct_answers"), 0)
    return _safe_int(attempt.get("correct_count"), 0)


def paid_full_mock_count(attempts: List[Dict[str, Any]], expected_question_count: int = 60) -> int:
    count = 0
    for attempt in attempts or []:
        if str(attempt.get("mode") or "").strip() == "Paid Mock Exam" and _safe_int(attempt.get("total_questions"), 0) >= int(expected_question_count or 60):
            count += 1
    return count



def filter_readiness_attempts(attempts: List[Dict[str, Any]], expected_question_count: int = 60) -> List[Dict[str, Any]]:
    """Keep only full-length Paid Mock Exam attempts for readiness."""
    filtered: List[Dict[str, Any]] = []
    for attempt in attempts or []:
        if str(attempt.get("mode") or "").strip() == "Paid Mock Exam" and _safe_int(attempt.get("total_questions"), 0) >= int(expected_question_count or 60):
            filtered.append(attempt)
    return filtered


def filter_question_attempts_for_attempts(question_attempts: List[Dict[str, Any]], attempts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only question attempts linked to readiness-eligible full mock attempts."""
    eligible_ids = {str(attempt.get("id")) for attempt in attempts or [] if attempt.get("id") is not None}
    if not eligible_ids:
        return []
    return [
        row for row in question_attempts or []
        if str(row.get("exam_attempt_id")) in eligible_ids
    ]


VERIFIED_MOCK_PERFORMANCE_HEADER = "Verified Mock Performance"
VERIFIED_MOCK_PERFORMANCE_EMPTY_MESSAGE = (
    "No verified full paid mock exams yet. Complete a full paid mock exam to see Latest, "
    "Average, and Best scores here. Daily Sprint and other practice sessions remain "
    "visible in Attempt History below."
)
VERIFIED_DOMAIN_EMPTY_MESSAGE = (
    "No verified mock domain evidence yet. Complete a full paid mock exam with saved "
    "question-level results to see Weak Areas by Domain. Daily Sprint, practice sessions, "
    "and legacy mock summaries are excluded from this section."
)


def build_attempt_history_rows(
    attempts: List[Dict[str, Any]],
    preferred_timezone: str,
) -> List[Dict[str, Any]]:
    """Build Attempt History rows from all saved attempts."""
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
    return history_rows


def render_readiness_locked(full_mocks: int, required_mocks: int = 3) -> None:
    remaining = max(required_mocks - int(full_mocks or 0), 0)
    st.header("Overall Readiness")
    st.warning("Readiness Locked")
    st.info(
        f"Complete {required_mocks} full paid mock exams to unlock readiness analysis. "
        f"Progress: {full_mocks} / {required_mocks}. "
        f"You need {remaining} more full mock exam{'s' if remaining != 1 else ''}."
    )
    st.caption("We do not show readiness from too little data. This protects users from false confidence after one lucky or rushed exam.")


def render_readiness_card(readiness: Dict[str, Any], passing_score: float, selected_exam: str) -> None:
    st.header("Overall Readiness")
    st.caption("This is a study-planning estimate, not a pass guarantee.")

    # Primary metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Readiness Score", f"{round(_safe_float(readiness.get('score')), 2)}%")
    c2.metric("Status", readiness.get("label", "Not Enough Data"))
    c3.metric(
        "Estimate Confidence",
        f"{_safe_float(readiness.get('confidence_score'), 0):.0f}% — {readiness.get('confidence_label', 'Low')}",
    )
    c4.metric("Recent Mock Accuracy", f"{_safe_float(readiness.get('recent_accuracy', readiness.get('accuracy_score')), 0):.2f}%")

    st.caption(
        "Confidence measures how well-supported the estimate is. "
        "It is not your probability of passing."
    )
    st.info(readiness_methodology_text())

    # Diagnostics
    st.subheader("Readiness Diagnostics")
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Domain Robustness", f"{_safe_float(readiness.get('domain_robustness'), 0):.2f}%")
    r2.metric("Trend", readiness.get("trend_label", "Stable"))
    r3.metric("Consistency (SD)", f"±{_safe_float(readiness.get('consistency_standard_deviation'), 0):.1f}pts")
    r4.metric("Pacing", readiness.get("pacing_status", "Insufficient Timing Data"))

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Unique Questions Seen", _safe_int(readiness.get("unique_questions_seen"), 0))
    d2.metric("Full Mocks", _safe_int(readiness.get("eligible_mock_count", readiness.get("full_mock_count")), 0))
    d3.metric("Question Data Completeness", f"{_safe_float(readiness.get('question_attempt_completeness'), 0) * 100:.0f}%")
    d4.metric("Coverage", f"{_safe_float(readiness.get('coverage_percent'), 0):.1f}%")

    st.info(readiness.get("recommendation", "Complete more attempts to improve the readiness signal."))

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
    r1.metric("Recency Accuracy", "76%")
    r2.metric("Coverage", "71%")
    r3.metric("Domain Balance", "68%")
    r4.metric("Pacing Stability", "82%")

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
st.caption(f"App Version: {APP_VERSION}")

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

question_attempt_result = load_question_attempts(user_email, selected_exam)
question_attempts = question_attempt_result.get("rows", [])
question_attempt_error = question_attempt_result.get("error")
question_bank_total = fetch_question_bank_total(selected_exam, preferred_language)

with st.expander("Progress query diagnostics", expanded=False):
    st.write(f"Current user email: `{user_email}`")
    st.write(f"Selected exam: `{selected_exam}`")
    st.write(f"Attempts returned: `{len(attempts)}`")
    st.write(f"Question attempts returned: `{len(question_attempts)}`")
    st.write(f"Approved question bank size: `{question_bank_total}`")
    if query_error:
        st.error(query_error)
    if question_attempt_error:
        st.error(question_attempt_error)

if query_error or question_attempt_error:
    st.error("Progress could not be loaded because a database query failed. This is a setup/code issue, not 'no attempts'.")
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

readiness_attempts = filter_readiness_attempts(attempts, expected_question_count)
readiness_question_attempts = filter_question_attempts_for_attempts(question_attempts, readiness_attempts)

readiness = calculate_readiness(
    attempts=readiness_attempts,
    passing_score=passing_score,
    domain_weights=domain_weights,
    expected_question_count=expected_question_count,
    question_bank_total=question_bank_total,
    question_attempts=readiness_question_attempts,
    time_limit_minutes=_safe_int(cert.get("time_limit_minutes"), 105),
    captured_bank_size=extract_captured_bank_size(readiness_attempts),
)

full_mocks_completed = _safe_int(readiness.get("eligible_mock_count"), len(readiness_attempts))
required_mocks = _safe_int(readiness.get("required_mock_count"), 3)
if readiness.get("is_locked", full_mocks_completed < required_mocks):
    render_readiness_locked(full_mocks_completed, required_mocks)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Full Mocks Completed", f"{full_mocks_completed} / {required_mocks}")
    m2.metric("Mocks Remaining", _safe_int(readiness.get("mocks_remaining"), max(required_mocks - full_mocks_completed, 0)))
    m3.metric("Unique Questions Seen", _safe_int(readiness.get("unique_questions_seen"), 0))
    m4.metric(
        "Estimate Confidence",
        f"{_safe_float(readiness.get('confidence_score'), 0):.0f}% — {readiness.get('confidence_label', 'Low')}",
    )
    st.caption(
        "Confidence measures how well-supported the estimate is. "
        "It is not your probability of passing."
    )
    st.info(readiness_methodology_text())
else:
    render_readiness_card(readiness, passing_score, selected_exam)

st.divider()
st.header(VERIFIED_MOCK_PERFORMANCE_HEADER)
mock_performance = build_verified_mock_performance_metrics(
    attempts,
    question_attempts,
    expected_question_count,
)
if not mock_performance["has_verified_mocks"]:
    st.info(VERIFIED_MOCK_PERFORMANCE_EMPTY_MESSAGE)
else:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Latest Score", f"{mock_performance['latest_score']}%")
    c2.metric("Average Score", f"{mock_performance['average_score']}%")
    c3.metric("Best Score", f"{mock_performance['best_score']}%")
    c4.metric("Verified Mocks", mock_performance["verified_mock_count"])

    trend_rows = []
    for attempt in mock_performance["trend_attempts"]:
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
domain_rows = build_verified_domain_table_rows(
    attempts,
    question_attempts,
    expected_question_count,
)
if not domain_rows:
    st.info(VERIFIED_DOMAIN_EMPTY_MESSAGE)
else:
    domain_df = pd.DataFrame(domain_rows)
    st.dataframe(domain_df, use_container_width=True, hide_index=True)
    st.bar_chart(domain_df.set_index("Domain")["Accuracy %"])
    weakest = select_weakest_verified_domain(domain_rows)
    if weakest is not None:
        st.info(f"Weakest domain: {weakest['Domain']} ({weakest['Accuracy %']}%)")

st.divider()
st.header("Attempt History")
history_rows = build_attempt_history_rows(attempts, preferred_timezone)
st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)
