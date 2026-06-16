from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

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

try:
    from utils.readiness import calculate_readiness
except Exception:
    calculate_readiness = None

APP_VERSION = "DASHBOARD_ONBOARDING_V1"
DEFAULT_ADMIN_EXAM = "Salesforce Certified Platform Administrator"
DEFAULT_BA_EXAM = "Salesforce Certified Business Analyst"

st.set_page_config(page_title="Dashboard", page_icon="🏠", layout="wide", initial_sidebar_state="expanded")
render_app_chrome()


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
        return datetime.min
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace("+00:00", ""))
    except Exception:
        return datetime.min


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
                "started_at,completed_at,domain_breakdown,difficulty_breakdown,exam_name,language_code"
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


def certification_display(cert: Dict[str, Any]) -> str:
    return safe_str(cert.get("display_name") or cert.get("exam_name"), "Certification")


def attempt_correct_count(attempt: Dict[str, Any]) -> int:
    if attempt.get("correct_answers") is not None:
        return safe_int(attempt.get("correct_answers"), 0)
    return safe_int(attempt.get("correct_count"), 0)


def build_domain_summary(attempts: List[Dict[str, Any]]) -> pd.DataFrame:
    totals: Dict[str, Dict[str, float]] = {}
    for attempt in attempts:
        breakdown = normalize_breakdown(attempt.get("domain_breakdown"))
        for domain, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            correct = safe_float(data.get("correct"), 0.0)
            total = safe_float(data.get("total"), 0.0)
            if total <= 0:
                continue
            totals.setdefault(str(domain), {"correct": 0.0, "total": 0.0})
            totals[str(domain)]["correct"] += correct
            totals[str(domain)]["total"] += total

    rows = []
    for domain, data in totals.items():
        total = data["total"]
        correct = data["correct"]
        rows.append(
            {
                "Domain": domain,
                "Accuracy %": round((correct / total) * 100, 2) if total else 0.0,
                "Correct": int(correct),
                "Total": int(total),
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Accuracy %", ascending=True)
    return df


def render_public_onboarding() -> None:
    st.title("CertBound Dashboard")
    st.caption(f"App version: {APP_VERSION}")

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
        render_session_page_link("pages/Account.py", label="Create / Log In", icon="👤")
    with step2:
        st.markdown("**2. Pick certification**")
        st.write("Choose Salesforce Admin or Business Analyst when starting the mock exam.")
        render_session_page_link("app.py", label="Open Mock Exam", icon="📝")
    with step3:
        st.markdown("**3. Review result**")
        st.write("Use score, answer review, and explanations to decide what to study next.")
        render_session_page_link("pages/My_Progress.py", label="View Progress", icon="📈")

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
    st.subheader("Premium features")
    cols = st.columns(3)
    cards = [
        ("Practice By Category", "Drill specific exam domains after you finish the free preview.", "pages/Practice_By_Category.py", "📚"),
        ("Weak Areas Practice", "Use your result history to generate targeted practice.", "pages/Weak_Areas_Practice.py", "🎯"),
        ("My Progress", "Track attempt history, readiness, and domain performance.", "pages/My_Progress.py", "📈"),
    ]
    for col, (title, body, path, icon) in zip(cols, cards):
        with col:
            st.markdown(f"**{icon} {title}**")
            st.write(body)
            st.caption("Premium access required")
            render_session_page_link(path, label="Preview / Open", icon=icon)


def render_logged_in_dashboard(email: str) -> None:
    profile = get_user_profile(email) or {}
    access_level = get_user_access_level(email)
    subscription_status = safe_lower(profile.get("subscription_status") or st.session_state.get("subscription_status"), "free")
    preferred_language = safe_lower(profile.get("preferred_language_code") or st.session_state.get("preferred_language_code"), "en") or "en"
    full_name = safe_str(profile.get("full_name") or st.session_state.get("full_name"), "")

    st.title("Dashboard")
    st.caption(f"App version: {APP_VERSION}")
    st.info(f"Signed in: {email} | Access: {access_level} | Subscription: {subscription_status} | Language: {preferred_language}")

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
        st.write("Use category or weak-area practice when premium is active.")
        render_session_page_link("pages/Practice_By_Category.py", label="Practice By Category", icon="📚")
    with n3:
        st.markdown("**Review progress**")
        st.write("See score trend, weak domains, and attempt history.")
        render_session_page_link("pages/My_Progress.py", label="My Progress", icon="📈")

    st.divider()
    st.subheader("Current status")
    latest_score = safe_float(attempts[0].get("score"), 0.0) if attempts else 0.0
    best_score = max([safe_float(a.get("score"), 0.0) for a in attempts], default=0.0)
    avg_score = round(sum([safe_float(a.get("score"), 0.0) for a in attempts]) / len(attempts), 2) if attempts else 0.0

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Latest Score", f"{latest_score}%" if attempts else "No attempt")
    s2.metric("Average Score", f"{avg_score}%" if attempts else "No attempt")
    s3.metric("Best Score", f"{round(best_score, 2)}%" if attempts else "No attempt")
    s4.metric("Attempts", len(attempts))

    h1, h2, h3 = st.columns(3)
    h1.metric("Approved Questions", question_health.get("approved_questions", 0))
    h2.metric("Free Preview Questions", question_health.get("free_questions", 0))
    h3.metric("Domains Covered", question_health.get("domains", 0))

    if not attempts:
        st.warning("No attempt data for this certification yet. Start with the mock exam. Dashboard intelligence stays weak until attempts exist.")
        if not premium:
            render_locked_premium_cards()
        return

    domain_df = build_domain_summary(attempts)
    st.divider()
    if calculate_readiness is not None:
        domain_weights = fetch_domain_weights(selected_exam)
        passing_score = safe_float(cert.get("passing_score"), 72 if "Business Analyst" in selected_exam else 68)
        expected_question_count = safe_int(cert.get("question_count"), 60) or 60
        readiness = calculate_readiness(
            attempts=attempts,
            passing_score=passing_score,
            domain_weights=domain_weights,
            expected_question_count=expected_question_count,
            question_bank_total=None,
        )
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Readiness", f"{round(safe_float(readiness.get('score')), 2)}%")
        r2.metric("Status", safe_str(readiness.get("label"), "Not Enough Data"))
        r3.metric("Confidence", safe_str(readiness.get("confidence"), "No Data"))
        r4.metric("Questions Practiced", safe_int(readiness.get("total_attempted"), 0))
        recommendation = safe_str(readiness.get("recommendation"), "Complete more attempts to improve the readiness signal.")
        st.info(recommendation)

    st.subheader("Weakest domains")
    if domain_df.empty:
        st.warning("No domain breakdown saved yet. Future attempts should save domain_breakdown for better recommendations.")
    else:
        st.dataframe(domain_df.head(5), use_container_width=True, hide_index=True)
        weakest = domain_df.iloc[0]
        st.warning(f"Highest-risk domain: {weakest['Domain']} ({weakest['Accuracy %']}%)")

    st.subheader("Recent attempts")
    rows = []
    for attempt in attempts[:5]:
        rows.append(
            {
                "Completed": attempt.get("completed_at") or attempt.get("started_at") or "Not recorded",
                "Mode": attempt.get("mode"),
                "Score %": attempt.get("score"),
                "Correct": attempt_correct_count(attempt),
                "Total": attempt.get("total_questions"),
                "Language": attempt.get("language_code"),
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if not premium:
        render_locked_premium_cards()


email = get_current_user_email()
if not email:
    render_public_onboarding()
else:
    render_logged_in_dashboard(email)
