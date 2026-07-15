from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import pandas as pd
import streamlit as st

from utils.access_control import (
    get_current_user_email,
    get_supabase_client,
    get_user_access_level,
    get_user_profile,
    has_premium_access,
    render_app_chrome,
    render_session_page_link,
)
from utils.datetime_display import DEFAULT_DISPLAY_TIMEZONE, format_user_datetime
from utils.learner_analytics import (
    build_activity_history_display_rows,
    build_all_activity_score_summary,
    build_readiness_display_contract,
    build_study_activity_summary,
    build_verified_domain_performance,
    build_verified_mock_performance,
    filter_question_attempts_for_attempts,
    filter_readiness_attempts,
    rank_weak_domains,
)
from utils.dashboard_components import (
    build_mock_exam_href,
    inject_certbound_theme,
    render_activity_history,
    render_cert_context_header,
    render_domain_mastery_section,
    render_empty_state,
    render_readiness_hero,
    render_score_trend_section,
    render_study_activity_section,
    render_verified_kpi_row,
    render_page_header,
    render_weak_area_action_panel,
)
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.version import APP_VERSION

try:
    from utils.readiness import calculate_readiness, readiness_methodology_text
    from utils.readiness_persistence import extract_captured_bank_size
except Exception:
    calculate_readiness = None
    extract_captured_bank_size = None  # type: ignore[assignment]
    def readiness_methodology_text() -> str:  # type: ignore[misc]
        return ""
DEFAULT_ADMIN_EXAM = "Salesforce Certified Platform Administrator"
DEFAULT_BA_EXAM = "Salesforce Certified Business Analyst"
DAILY_SPRINT_QUESTION_COUNT = 10

st.set_page_config(page_title="Home", page_icon="🏠", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()

# SESSION_TIMEOUT_APPLIED
enforce_session_timeout()
show_session_expired_notice()


def safe_str(value: Any, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def safe_lower(value: Any, default: str = "") -> str:
    return safe_str(value, default).lower()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_dt(value: Any) -> datetime:
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


def paid_full_mock_count(attempts: List[Dict[str, Any]], expected_question_count: int = 60) -> int:
    return len(filter_readiness_attempts(attempts, expected_question_count))


def verified_domain_rows_to_dataframe(domain_rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not domain_rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        [
            {
                "Domain": row.get("Domain"),
                "Accuracy %": row.get("Accuracy %"),
                "Correct": row.get("Correct"),
                "Total": row.get("Total"),
            }
            for row in domain_rows
        ]
    )
    if not df.empty:
        df = df.sort_values("Accuracy %", ascending=True)
    return df


def get_daily_sprint_domain(readiness: Dict[str, Any], domain_df: pd.DataFrame) -> str:
    """Return the single weakest domain for Daily Sprint V1."""
    weak_domains = readiness.get("weak_domains") if isinstance(readiness, dict) else None
    if isinstance(weak_domains, list) and weak_domains:
        return safe_str(weak_domains[0])

    if domain_df is not None and not domain_df.empty and "Domain" in domain_df.columns:
        return safe_str(domain_df.iloc[0].get("Domain"))

    return ""


def domain_has_sprint_capacity(domain: str, domain_counts: Dict[str, int], min_count: int = DAILY_SPRINT_QUESTION_COUNT) -> bool:
    return safe_int(domain_counts.get(safe_str(domain)), 0) >= int(min_count or DAILY_SPRINT_QUESTION_COUNT)


def select_daily_sprint_fallback_domain(
    domain_counts: Dict[str, int],
    min_count: int = DAILY_SPRINT_QUESTION_COUNT,
) -> str:
    """Pick a bank domain with enough practice-eligible questions for auto-start."""
    eligible = sorted(
        name
        for name, count in (domain_counts or {}).items()
        if safe_str(name) and safe_int(count, 0) >= int(min_count or DAILY_SPRINT_QUESTION_COUNT)
    )
    return eligible[0] if eligible else ""


def resolve_daily_sprint_domain(
    readiness: Optional[Dict[str, Any]],
    domain_df: pd.DataFrame,
    domain_counts: Dict[str, int],
    min_count: int = DAILY_SPRINT_QUESTION_COUNT,
) -> str:
    """Resolve sprint domain: readiness weak > historical weak > bank fallback."""
    weak_domains = readiness.get("weak_domains") if isinstance(readiness, dict) else None
    if isinstance(weak_domains, list):
        for domain in weak_domains:
            candidate = safe_str(domain)
            if candidate and domain_has_sprint_capacity(candidate, domain_counts, min_count):
                return candidate

    if domain_df is not None and not domain_df.empty and "Domain" in domain_df.columns:
        for _, row in domain_df.iterrows():
            candidate = safe_str(row.get("Domain"))
            if candidate and domain_has_sprint_capacity(candidate, domain_counts, min_count):
                return candidate

    return select_daily_sprint_fallback_domain(domain_counts, min_count)


def build_daily_sprint_href(page_path: str, exam_name: str, category: str, count: int = 10) -> str:
    """Build a session-preserving Daily Sprint link to Practice By Category."""
    token = safe_str(st.query_params.get("fr_session", ""))
    base = "Practice_By_Category" if page_path.endswith("Practice_By_Category.py") else page_path
    params = {
        "daily_sprint": "1",
        "exam_name": exam_name,
        "category": category,
        "count": str(int(count or 10)),
    }
    if token:
        params["fr_session"] = token
    query = "&".join(f"{quote(str(k))}={quote(str(v))}" for k, v in params.items())
    return f"{base}?{query}"


def render_daily_sprint_card(exam_name: str, weakest_domain: str, premium: bool) -> None:
    """Render the premium Daily Sprint card."""
    if not weakest_domain:
        return

    href = build_daily_sprint_href("pages/Practice_By_Category.py", exam_name, weakest_domain, 10)
    locked_note = "" if premium else "<p style='margin:10px 0 0 0;color:#cbd5e1;'>Premium required to start the sprint.</p>"
    button_html = (
        f'<a href="{href}" target="_self" style="display:inline-block;margin-top:14px;'
        'background:#ffffff;color:#0f172a;padding:10px 16px;border-radius:999px;'
        'font-weight:800;text-decoration:none;">Start Daily Sprint →</a>'
        if premium
        else '<span style="display:inline-block;margin-top:14px;background:#334155;color:#cbd5e1;'
             'padding:10px 16px;border-radius:999px;font-weight:800;">Premium Locked</span>'
    )

    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg,#0b1220 0%,#111827 48%,#172554 100%);
            color: white;
            border-radius: 18px;
            padding: 24px 26px;
            margin: 8px 0 20px 0;
            box-shadow: 0 16px 38px rgba(15,23,42,0.22);
            border: 1px solid rgba(255,255,255,0.10);
        ">
            <div style="font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.12em;color:#93c5fd;margin-bottom:8px;">
                Daily Sprint
            </div>
            <h2 style="margin:0 0 8px 0;color:white;font-size:28px;line-height:1.15;">
                Your Daily Sprint is Ready.
            </h2>
            <p style="margin:0;color:#e5e7eb;font-size:16px;line-height:1.55;">
                10 questions tailored to your current gaps in <strong style="color:white;">{weakest_domain}</strong>.
                Takes ~10 minutes.
            </p>
            {button_html}
            {locked_note}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_readiness_locked(full_mocks: int, required_mocks: int = 3) -> None:
    remaining = max(required_mocks - int(full_mocks or 0), 0)
    st.subheader("Readiness Analysis")
    st.warning("Readiness Locked")
    st.info(
        f"Complete {required_mocks} full paid mock exams to unlock your readiness score. "
        f"Progress: {full_mocks} / {required_mocks}. "
        f"You need {remaining} more full mock exam{'s' if remaining != 1 else ''}."
    )
    st.caption("We do not show readiness from too little data. This protects users from false confidence after one lucky or rushed exam.")


def normalize_breakdown(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_active_certifications() -> List[Dict[str, Any]]:
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
        if rows:
            return rows
    except Exception:
        pass

    return [
        {
            "exam_name": DEFAULT_ADMIN_EXAM,
            "display_name": DEFAULT_ADMIN_EXAM,
            "certification_code": "ADM",
            "passing_score": 68,
            "time_limit_minutes": 105,
            "question_count": 60,
            "is_active": True,
        },
        {
            "exam_name": DEFAULT_BA_EXAM,
            "display_name": DEFAULT_BA_EXAM,
            "certification_code": "BA",
            "passing_score": 72,
            "time_limit_minutes": 105,
            "question_count": 60,
            "is_active": True,
        },
    ]


@st.cache_data(ttl=60, show_spinner=False)
def fetch_user_attempts(user_email: str, exam_name: Optional[str] = None) -> List[Dict[str, Any]]:
    user_email = safe_lower(user_email)
    if not user_email:
        return []

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
        rows = result.data or []
    except Exception:
        rows = []

    return sorted(
        rows,
        key=lambda a: (
            parse_dt(a.get("completed_at")),
            parse_dt(a.get("started_at")),
            safe_int(a.get("id"), 0),
        ),
        reverse=True,
    )


@st.cache_data(ttl=60, show_spinner=False)
def fetch_user_question_attempts(user_email: str, exam_name: Optional[str] = None) -> List[Dict[str, Any]]:
    user_email = safe_lower(user_email)
    if not user_email:
        return []
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
        return result.data or []
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner=False)
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
        return {
            str(row.get("domain_name")): safe_float(row.get("weight"), 0.0)
            for row in result.data or []
            if row.get("domain_name")
        }
    except Exception:
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_question_health_for_exam(exam_name: str, language_code: str) -> Dict[str, Any]:
    if not exam_name:
        return {"approved_questions": 0, "free_questions": 0, "domains": 0}

    try:
        questions_result = (
            get_supabase_client()
            .table("questions")
            .select("id,category,free_mock_exam")
            .eq("exam_name", exam_name)
            .eq("language_code", language_code or "en")
            .eq("is_active", True)
            .eq("is_exam_eligible", True)
            .eq("quality_status", "approved")
            .execute()
        )
        rows = questions_result.data or []
        return {
            "approved_questions": len(rows),
            "free_questions": sum(1 for row in rows if row.get("free_mock_exam") is True),
            "domains": len({row.get("category") for row in rows if row.get("category")}),
        }
    except Exception:
        return {"approved_questions": 0, "free_questions": 0, "domains": 0}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_practice_domain_counts(exam_name: str, language_code: str) -> Dict[str, int]:
    """Count practice-eligible approved questions per domain/category."""
    if not exam_name:
        return {}

    try:
        questions_result = (
            get_supabase_client()
            .table("questions")
            .select("category")
            .eq("exam_name", exam_name)
            .eq("language_code", language_code or "en")
            .eq("is_active", True)
            .eq("is_exam_eligible", True)
            .eq("quality_status", "approved")
            .eq("practice_eligible", True)
            .execute()
        )
        counts: Dict[str, int] = {}
        for row in questions_result.data or []:
            category = safe_str(row.get("category"))
            if not category:
                continue
            counts[category] = counts.get(category, 0) + 1
        return counts
    except Exception:
        return {}


def certification_display(cert: Dict[str, Any]) -> str:
    return safe_str(cert.get("display_name") or cert.get("exam_name"), "Certification")


def attempt_correct_count(attempt: Dict[str, Any]) -> int:
    if attempt.get("correct_answers") is not None:
        return safe_int(attempt.get("correct_answers"), 0)
    return safe_int(attempt.get("correct_count"), 0)


def render_public_onboarding() -> None:
    render_page_header(
        "Home",
        description="Start with a free mock exam. Upgrade only when the data is useful.",
        badge="Free Preview",
    )
    st.caption(f"App Version: {APP_VERSION}")

    st.markdown(
        """
        <div style="border:1px solid #d8dde6;border-radius:14px;padding:22px;background:#ffffff;margin:8px 0 18px 0;">
            <div style="font-size:13px;font-weight:800;letter-spacing:.08em;color:#54698d;text-transform:uppercase;">Certification prep platform</div>
            <div style="font-size:34px;font-weight:900;color:#16325c;line-height:1.15;margin-top:4px;">Start with a free mock exam. Upgrade only when the data is useful.</div>
            <div style="font-size:15px;color:#5f6368;margin-top:8px;">CertBound tracks mock exam results, weak domains, readiness, and targeted practice for Salesforce Admin and Business Analyst preparation.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Free Preview", "10 questions")
    c2.metric("Full Mock", "60 questions")
    c3.metric("Progress Tracking", "Premium")

    st.subheader("Get started")
    step1, step2, step3 = st.columns(3)
    with step1:
        st.markdown("**1. Create account**")
        st.write("Account is required so results can be saved and reset links work correctly.")
        st.page_link("pages/Account.py", label="Create / Log In", icon="👤")
    with step2:
        st.markdown("**2. Pick certification**")
        st.write("Choose Salesforce Admin or Business Analyst when starting the mock exam.")
        st.page_link("app.py", label="Open Mock Exam", icon="📝")
    with step3:
        st.markdown("**3. Review result**")
        st.write("Use score, answer review, and explanations to decide what to study next.")
        st.page_link("pages/My_Progress.py", label="View Progress", icon="📈")

    st.divider()
    st.subheader("Premium unlocks")
    p1, p2, p3 = st.columns(3)
    with p1:
        st.markdown("**Practice By Category**")
        st.write("Target one domain instead of wasting time on random questions.")
    with p2:
        st.markdown("**Weak Areas Practice**")
        st.write("Practice based on your actual attempt history, not guesswork.")
    with p3:
        st.markdown("**Readiness Tracking**")
        st.write("Track score trend, weak domains, and readiness over time.")

    st.warning("Independent exam-prep platform. Not affiliated with Salesforce.")


def render_locked_premium_cards() -> None:
    st.subheader("Premium previews")
    st.caption("Free users can preview these workflows. Real practice data unlocks with Premium access.")
    cols = st.columns(3)
    cards = [
        ("Practice By Category", "Preview domain-based drilling without exposing paid questions.", "pages/Practice_By_Category.py", "📚"),
        ("Weak Areas Practice", "Preview targeted practice based on sample weak-domain data.", "pages/Weak_Areas_Practice.py", "🎯"),
        ("Progress", "Preview readiness, weak areas, and attempt-history tracking.", "pages/My_Progress.py", "📈"),
    ]
    for col, (title, body, path, icon) in zip(cols, cards):
        with col:
            st.markdown(f"**{icon} {title}**")
            st.write(body)
            st.caption("Locked preview — no paid questions exposed")
            render_session_page_link(path, label="Open preview", icon=icon)


def render_logged_in_dashboard(email: str) -> None:
    inject_certbound_theme()
    profile = get_user_profile(email) or {}
    access_level = get_user_access_level(email)
    subscription_status = safe_lower(profile.get("subscription_status") or st.session_state.get("subscription_status"), "free")
    preferred_language = safe_lower(profile.get("preferred_language_code") or st.session_state.get("preferred_language_code"), "en") or "en"
    preferred_timezone = safe_str(
        profile.get("preferred_timezone") or st.session_state.get("preferred_timezone"),
        DEFAULT_DISPLAY_TIMEZONE,
    ) or DEFAULT_DISPLAY_TIMEZONE
    full_name = safe_str(profile.get("full_name") or st.session_state.get("full_name"), "")
    display_name = full_name or email.split("@", 1)[0]

    render_page_header(
        "Home",
        description=f"Welcome back, {display_name}. Track readiness and decide what to study next.",
        badge=access_level.title(),
        certification_name=safe_str(st.session_state.get("selected_exam_name"), ""),
    )
    st.caption(f"App Version: {APP_VERSION}")

    profile_col, status_col, language_col, account_col = st.columns([2, 1, 1, 1])
    profile_col.metric("Signed in", email)
    status_col.metric("Access", access_level.title())
    language_col.metric("Language", preferred_language.upper())
    with account_col:
        st.markdown("**Account**")
        render_session_page_link("pages/Account.py", label="Manage profile", icon="👤")

    if access_level == "free":
        st.info("You are on Free Preview access. Use the free mock exam first; premium pages now show locked previews instead of dead-end blocks.")
    elif access_level == "paid":
        st.success("Premium access is active. Full mock exams, targeted practice, weak-area practice, and progress tracking are available.")
    elif access_level == "admin":
        st.success("Admin session active. User/admin tools are available from the sidebar.")

    certifications = fetch_active_certifications()
    if not certifications:
        st.error("No active certifications are configured. Admin setup is incomplete.")
        st.stop()

    exam_names = [cert.get("exam_name") for cert in certifications if cert.get("exam_name")]
    cert_by_exam = {cert.get("exam_name"): cert for cert in certifications if cert.get("exam_name")}
    display_by_exam = {cert.get("exam_name"): certification_display(cert) for cert in certifications if cert.get("exam_name")}

    existing_selection = st.session_state.get("selected_exam_name")
    default_index = exam_names.index(existing_selection) if existing_selection in exam_names else 0

    left, right = st.columns([2, 1])
    with left:
        selected_exam = st.selectbox(
            "Current certification focus",
            exam_names,
            index=default_index,
            format_func=lambda exam: display_by_exam.get(exam, exam),
            key="dashboard_selected_exam",
        )
        st.session_state.selected_exam_name = selected_exam
    with right:
        cert = cert_by_exam.get(selected_exam, {})
        st.metric("Passing Score", f"{safe_int(cert.get('passing_score'), 68)}%")

    attempts = fetch_user_attempts(email, selected_exam)
    question_attempts = fetch_user_question_attempts(email, selected_exam)
    question_health = fetch_question_health_for_exam(selected_exam, preferred_language)
    premium = has_premium_access(email)

    st.divider()
    st.subheader("Next action")
    n1, n2, n3 = st.columns(3)
    with n1:
        st.markdown("**Start mock exam**")
        if premium:
            st.write("Run a full randomized mock exam for the selected certification.")
        else:
            st.write("Run the free fixed preview and review explanations.")
        render_session_page_link("app.py", label="Open Mock Exam", icon="📝")
    with n2:
        st.markdown("**Practice targeted questions**")
        if premium:
            st.write("Drill categories and weak areas using real question data.")
            render_session_page_link("pages/Practice_By_Category.py", label="Practice By Category", icon="📚")
        else:
            st.write("Preview premium practice workflows without exposing paid questions.")
            render_session_page_link("pages/Practice_By_Category.py", label="Open Practice Preview", icon="📚")
    with n3:
        st.markdown("**Review progress**")
        if premium:
            st.write("See score trend, weak domains, readiness, and recent attempts.")
        else:
            st.write("Preview readiness tracking and attempt-history analytics.")
        render_session_page_link("pages/My_Progress.py", label="My Progress", icon="📈")

    st.divider()
    expected_question_count = safe_int(cert.get("question_count"), 60) or 60
    passing_score = safe_float(cert.get("passing_score"), 72 if "Business Analyst" in selected_exam else 68)
    domain_weights = fetch_domain_weights(selected_exam)
    session_token = safe_str(st.query_params.get("fr_session", ""))
    mock_exam_href = build_mock_exam_href(session_token)

    verified_performance = build_verified_mock_performance(
        attempts,
        question_attempts,
        expected_question_count,
        passing_threshold=passing_score,
    )
    all_activity_scores = build_all_activity_score_summary(attempts)
    verified_domain_rows = build_verified_domain_performance(
        attempts,
        question_attempts,
        expected_question_count,
        domain_weights=domain_weights,
        passing_threshold=passing_score,
    )
    domain_df = verified_domain_rows_to_dataframe(verified_domain_rows)
    study_activity = build_study_activity_summary(attempts, window_days=30)
    weak_domain_rows = rank_weak_domains(verified_domain_rows, limit=5)
    priority_weak_domain = weak_domain_rows[0] if weak_domain_rows else None

    render_cert_context_header(
        title=display_by_exam.get(selected_exam, selected_exam),
        subtitle="Executive learner overview for your current certification focus.",
        passing_score=passing_score,
        access_label=access_level.title(),
    )

    readiness = None
    readiness_display = None
    if calculate_readiness is not None:
        readiness_attempts = filter_readiness_attempts(attempts, expected_question_count)
        readiness_question_attempts = filter_question_attempts_for_attempts(question_attempts, readiness_attempts)
        readiness = calculate_readiness(
            attempts=readiness_attempts,
            passing_score=passing_score,
            domain_weights=domain_weights,
            expected_question_count=expected_question_count,
            question_bank_total=safe_int(question_health.get("approved_questions"), 0),
            question_attempts=readiness_question_attempts,
            time_limit_minutes=safe_int(cert.get("time_limit_minutes"), 105),
            captured_bank_size=extract_captured_bank_size(readiness_attempts) if extract_captured_bank_size else None,
        )
        readiness_display = build_readiness_display_contract(readiness)
        render_readiness_hero(
            readiness_display,
            readiness,
            variant="compact",
            mock_exam_href=mock_exam_href,
        )
    else:
        render_empty_state(
            "Readiness unavailable",
            "Readiness analytics are temporarily unavailable. You can still review verified mock performance below.",
        )

    st.subheader("Verified mock performance")
    if verified_performance.has_verified_mocks:
        render_verified_kpi_row(
            verified_performance,
            all_activity_average=all_activity_scores.average_score if all_activity_scores.has_attempts else None,
        )
    else:
        render_empty_state(
            "No verified mocks yet",
            "Complete a full paid mock exam with saved question-level results to unlock verified KPIs.",
            action_label="Start mock exam",
            action_href=mock_exam_href,
        )

    st.subheader("Verified score trend")
    render_score_trend_section(verified_performance, compact=True, chart_key="dashboard_score_trend")

    st.subheader("Priority weak area")
    render_weak_area_action_panel(
        priority_weak_domain,
        exam_name=selected_exam,
        session_token=session_token,
        compact=True,
    )

    practice_domain_counts = fetch_practice_domain_counts(selected_exam, preferred_language)
    if calculate_readiness is not None and readiness is not None:
        daily_sprint_domain = resolve_daily_sprint_domain(readiness, domain_df, practice_domain_counts)
        render_daily_sprint_card(selected_exam, daily_sprint_domain, premium)

    st.subheader("Domain mastery summary")
    render_domain_mastery_section(verified_domain_rows, compact=True, limit=5, chart_key="dashboard_domain_mastery")

    st.subheader("Study activity")
    render_study_activity_section(study_activity, compact=True, chart_key="dashboard_study_activity")

    if not attempts:
        if not premium:
            render_locked_premium_cards()
        return

    st.subheader("Recent activity")
    recent_history = build_activity_history_display_rows(
        attempts[:5],
        format_datetime=lambda value: format_user_datetime(value, preferred_timezone),
        get_correct_count=attempt_correct_count,
    )
    render_activity_history(recent_history, compact=True)

    if readiness_display and readiness_display.is_locked and calculate_readiness is not None:
        st.caption(readiness_methodology_text())

    if not premium:
        render_locked_premium_cards()


email = get_current_user_email()
if not email:
    render_public_onboarding()
else:
    render_logged_in_dashboard(email)
