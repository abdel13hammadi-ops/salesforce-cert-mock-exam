import json
import base64
import math
import time
import random
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
import os

import streamlit as st
from streamlit_autorefresh import st_autorefresh
from supabase import create_client
from utils.access_control import render_app_chrome, get_current_user_email as shared_get_current_user_email, get_user_subscription_status as shared_get_user_subscription_status, get_preferred_language_code as shared_get_preferred_language_code, PAID_STATUS_VALUES
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.user_errors import EXAM_BANK_LOAD_ERROR_MESSAGE, log_and_get_user_message
from utils.version import APP_VERSION
import streamlit.components.v1 as components
CONFIG_FILE = "exam_config.json"
DEFAULT_EXAM_NAME = "Salesforce Certified Platform Administrator"
DEFAULT_LANGUAGE_CODE = "en"

FALLBACK_CATEGORY_COUNTS = {
    "Configuration and Setup": 9,
    "Object Manager and Lightning App Builder": 9,
    "Data and Analytics Management": 10,
    "Automation": 9,
    "Sales and Marketing Applications": 6,
    "Service and Support Applications": 6,
    "Agentforce AI": 5,
    "Productivity and Collaboration": 6,
}

FALLBACK_CATEGORY_WEIGHTS = {
    "Configuration and Setup": 15,
    "Object Manager and Lightning App Builder": 15,
    "Data and Analytics Management": 17,
    "Automation": 15,
    "Sales and Marketing Applications": 10,
    "Service and Support Applications": 10,
    "Agentforce AI": 8,
    "Productivity and Collaboration": 10,
}

PASSING_SCORE_DEFAULT = 68
EXAM_MINUTES_DEFAULT = 105
QUESTION_COUNT_DEFAULT = 60


def format_domain_weight(weight) -> str:
    """Render a domain weight for display.

    Whole-number weights (e.g. 23.0) display without a trailing ".0"
    (as "23"); fractional weights (e.g. 23.3) display exactly as given.
    Accepts int/float/Decimal/str and never raises on bad input.
    """
    try:
        value = round(float(weight or 0), 1)
    except (TypeError, ValueError):
        return "0"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


st.set_page_config(
    page_title="Salesforce Certification Mock Exam",
    layout="wide",
    initial_sidebar_state="expanded",
)


def redirect_supabase_recovery_hash_to_reset_page():
    """
    Supabase password reset links may land at the root URL with tokens in the
    browser hash. Streamlit/Python cannot read URL hashes, so this must run in
    the browser and must read the parent window, not the Streamlit component
    iframe URL.
    """
    components.html(
        """
        <script>
        (function () {
            function getRealLocation() {
                try {
                    if (window.parent && window.parent.location) {
                        return window.parent.location;
                    }
                } catch (e) {}
                return window.location;
            }

            const loc = getRealLocation();
            const hash = loc.hash || "";
            const path = (loc.pathname || "").toLowerCase();

            const hasRecoveryToken =
                hash.includes("access_token=") ||
                hash.includes("refresh_token=") ||
                hash.includes("type=recovery");

            const alreadyOnResetPage = path.includes("reset_password");

            if (hasRecoveryToken && !alreadyOnResetPage) {
                loc.href = "/Reset_Password" + hash;
            }
        })();
        </script>
        """,
        height=0,
    )


redirect_supabase_recovery_hash_to_reset_page()

render_app_chrome()

# Exempt only while a legitimate exam is actively running with time remaining.
# All six conditions must hold: started, not submitted, numeric start_time not in
# the future, non-empty questions, a configured duration > 0, and elapsed seconds
# strictly less than that duration.  Expired, abandoned, stale-reload, submitted,
# results-page, and lobby states are never exempt.
# SESSION_TIMEOUT_APPLIED

def _valid_exam_duration_minutes(value) -> float:
    """Return the exam duration as a finite positive float, or 0.0 if invalid.

    Rejects bool (True/False), None, zero, negative, NaN, +/-inf, malformed
    strings, and nonnumeric objects.  bool is checked first because bool is a
    subclass of int and would otherwise pass float() as 1.0/0.0.  math.isfinite
    excludes NaN and infinities; float() never raises OverflowError for huge
    numeric strings (it returns inf, which isfinite then rejects).
    """
    if isinstance(value, bool):
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isfinite(v) and v > 0:
        return v
    return 0.0


def _valid_exam_start_time(value):
    """Return start_time as a finite float, or None if invalid.

    Rejects bool, None, malformed values, NaN, +/-inf, zero/negative timestamps,
    and timestamps more than 5 seconds in the future.
    """
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if v <= 0:
        return None
    if v > time.time() + 5:  # reject meaningfully future timestamps
        return None
    return v


_exam_start_time = _valid_exam_start_time(st.session_state.get("start_time"))
_exam_duration_minutes = _valid_exam_duration_minutes(
    st.session_state.get("exam_time_limit_minutes")
)
_elapsed_seconds = (time.time() - _exam_start_time) if _exam_start_time is not None else None
_has_time_remaining = (
    _exam_start_time is not None
    and _exam_duration_minutes > 0
    and _elapsed_seconds is not None
    and _elapsed_seconds >= 0
    and _elapsed_seconds < (_exam_duration_minutes * 60)
)
_is_active_exam = (
    bool(st.session_state.get("started"))
    and not bool(st.session_state.get("submitted"))
    and bool(st.session_state.get("all_questions"))
    and _has_time_remaining
)
enforce_session_timeout(exempt_active_exam=_is_active_exam)
show_session_expired_notice()


def inject_exam_layout_css():
    """Centralized exam-page layout CSS.

    Keep this early in the file so top widgets like the certification selector
    do not render before spacing rules load on Streamlit Cloud.
    """
    st.markdown(
        """
        <style>
        /* Global page spacing */
        .block-container {
            max-width: 1180px;
            padding-top: 2.75rem !important;
            padding-left: 2rem !important;
            padding-right: 2rem !important;
            padding-bottom: 3rem !important;
        }
        header[data-testid="stHeader"] { height: 0px; }

        /* Hide Streamlit's native multipage nav; app renders its own sidebar */
        [data-testid="stSidebarNav"] { display: none !important; }
        section[data-testid="stSidebar"] > div:first-child { padding-top: 0.75rem; }
        section[data-testid="stSidebar"] div.stButton > button {
            width: 100%;
            padding: 0.35rem 0.5rem;
            font-size: 14px;
        }
        div.stButton > button { border-radius: 8px; font-weight: 650; }

        /* Top certification selector */
        .exam-shell-top {
            border: 1px solid #d8dde6;
            border-radius: 14px;
            background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
            padding: 18px 20px 16px 20px;
            margin: 10px 0 18px 0;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        }
        .exam-kicker {
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #54698d;
            margin-bottom: 4px;
        }
        .exam-page-title {
            font-size: 28px;
            line-height: 1.15;
            font-weight: 800;
            color: #16325c;
            margin-bottom: 4px;
        }
        .exam-page-subtitle {
            color: #5f6368;
            font-size: 14px;
            margin-bottom: 0;
        }

        /* Exam banner/status */
        .exam-banner {
            background: #16325c;
            color: white;
            padding: 18px 22px;
            border-radius: 12px 12px 0 0;
            font-size: 27px;
            font-weight: 800;
            line-height: 1.25;
            margin-top: 10px;
        }
        .exam-sub-banner {
            background: #f4f6f9;
            border: 1px solid #d8dde6;
            border-top: none;
            padding: 12px 20px;
            border-radius: 0 0 12px 12px;
            margin-bottom: 26px;
            color: #16325c;
            font-size: 15px;
        }
        .exam-card {
            border: 1px solid #d8dde6;
            border-radius: 12px;
            padding: 18px 20px;
            background: #ffffff;
            margin-bottom: 18px;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        }
        .question-card {
            border: 1px solid #d8dde6;
            border-radius: 14px;
            padding: 24px;
            background: #ffffff;
            margin-top: 12px;
            margin-bottom: 18px;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
        }
        .status-strip {
            background: #f8f9fb;
            border: 1px solid #d8dde6;
            border-radius: 12px;
            padding: 12px 16px;
            margin-bottom: 15px;
        }

        /* V26 timer persistence */
        .exam-floating-timer{
            will-change: transform;
            backface-visibility:hidden;
            transform:translateZ(0);
        }

        /* Production-style floating exam timer */
        .exam-floating-timer {
            position: fixed;
            top: 68px;
            right: 30px;
            z-index: 1001;
            min-width: 170px;
            background: #fff4d6;
            border: 1px solid #e0b84f;
            border-radius: 12px;
            padding: 10px 14px;
            box-shadow: 0 6px 20px rgba(15, 23, 42, 0.16);
            text-align: center;
        }
        .exam-floating-timer-label {
            font-size: 12px;
            font-weight: 800;
            color: #5f4b00;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 3px;
        }
        .exam-floating-timer-value {
            font-size: 28px;
            font-weight: 900;
            color: #1f2937;
            letter-spacing: 0.04em;
            line-height: 1;
        }
        .question-nav-title {
            font-weight: 800;
            font-size: 16px;
            margin-top: 10px;
            margin-bottom: 8px;
            color: #1f2937;
        }
        .small-help {
            color: #5f6368;
            font-size: 13px;
            margin-bottom: 8px;
        }
        .exam-question-meta {
            color: #54698d;
            font-size: 13px;
            font-weight: 700;
            margin-bottom: 10px;
        }
        @media (max-width: 900px) {
            .block-container { padding-left: 1rem !important; padding-right: 1rem !important; }
            .exam-floating-timer {
                position: sticky;
                top: 0;
                right: auto;
                width: 100%;
                margin-bottom: 12px;
                min-width: 0;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


inject_exam_layout_css()


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {
            "exam_title": "Salesforce Certification Mock Exam",
            "certification": DEFAULT_EXAM_NAME,
            "passing_score": PASSING_SCORE_DEFAULT,
            "time_limit_minutes": EXAM_MINUTES_DEFAULT,
        }


config = load_config()


def get_secret(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, "") or "").strip()
    except Exception:
        return ""


def get_supabase_client():
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        st.error("Missing Supabase environment variables: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY.")
        st.stop()

    return create_client(url, key)


def get_current_user_email():
    return shared_get_current_user_email()


def get_selected_exam_name():
    return st.session_state.get("selected_exam_name") or config.get("certification") or DEFAULT_EXAM_NAME


def get_selected_language_code():
    return st.session_state.get("selected_language_code") or DEFAULT_LANGUAGE_CODE


def get_user_subscription_status(email):
    return shared_get_user_subscription_status(email)


def is_paid_subscription(status):
    return str(status or "").strip().lower() in PAID_STATUS_VALUES


@st.cache_data(ttl=300, show_spinner=False)
def fetch_exam_setup(exam_name):
    """Load exam metadata and domain structure from Supabase.
    Falls back to Admin hard-coded values if tables are not ready.
    """
    supabase = get_supabase_client()
    exam_name = exam_name or DEFAULT_EXAM_NAME

    setup = {
        "exam_name": exam_name,
        "display_name": exam_name,
        "certification_code": None,
        "passing_score": PASSING_SCORE_DEFAULT,
        "time_limit_minutes": EXAM_MINUTES_DEFAULT,
        "question_count": QUESTION_COUNT_DEFAULT,
        "category_counts": FALLBACK_CATEGORY_COUNTS.copy(),
        "category_weights": FALLBACK_CATEGORY_WEIGHTS.copy(),
        "domains": [
            {
                "domain_name": domain,
                "weight": FALLBACK_CATEGORY_WEIGHTS.get(domain, 0),
                "question_count": count,
                "display_order": idx + 1,
            }
            for idx, (domain, count) in enumerate(FALLBACK_CATEGORY_COUNTS.items())
        ],
    }

    try:
        cert_result = (
            supabase.table("certifications")
            .select("exam_name, display_name, certification_code, passing_score, time_limit_minutes, question_count, is_active")
            .eq("exam_name", exam_name)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        cert_rows = cert_result.data or []
        if cert_rows:
            cert = cert_rows[0]
            setup.update({
                "exam_name": cert.get("exam_name") or exam_name,
                "display_name": cert.get("display_name") or exam_name,
                "certification_code": cert.get("certification_code"),
                "passing_score": int(cert.get("passing_score") or PASSING_SCORE_DEFAULT),
                "time_limit_minutes": int(cert.get("time_limit_minutes") or EXAM_MINUTES_DEFAULT),
                "question_count": int(cert.get("question_count") or QUESTION_COUNT_DEFAULT),
            })

        domain_result = (
            supabase.table("certification_domains")
            .select("domain_name, weight, question_count, display_order, is_active")
            .eq("exam_name", exam_name)
            .eq("is_active", True)
            .order("display_order")
            .execute()
        )
        domain_rows = domain_result.data or []
        if domain_rows:
            setup["domains"] = domain_rows
            setup["category_counts"] = {
                d["domain_name"]: int(d.get("question_count") or 0)
                for d in domain_rows
            }
            setup["category_weights"] = {
                d["domain_name"]: float(d.get("weight") or 0)
                for d in domain_rows
            }
    except Exception:
        # Keep fallback setup so the app stays usable during migration.
        pass

    return setup


@st.cache_data(ttl=300, show_spinner=False)
def fetch_language_label(language_code):
    language_code = language_code or DEFAULT_LANGUAGE_CODE
    try:
        result = (
            get_supabase_client()
            .table("languages")
            .select("language_code, language_name, native_name")
            .eq("language_code", language_code)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if rows:
            row = rows[0]
            return row.get("language_name") or language_code
    except Exception:
        pass
    return language_code


@st.cache_data(ttl=120, show_spinner=False)
def fetch_user_certifications(user_email):
    """Return only certifications this logged-in user is enrolled in."""
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

    cert_result = (
        supabase.table("certifications")
        .select("exam_name, display_name, certification_code, is_active")
        .in_("exam_name", allowed_exam_names)
        .eq("is_active", True)
        .order("display_name")
        .execute()
    )
    return cert_result.data or []



@st.cache_data(ttl=300, show_spinner=False)
def fetch_active_certifications():
    """Return all active certifications.

    Free Preview should not require user_certification_access enrollment.
    Premium launch access currently includes all active certifications.
    """
    supabase = get_supabase_client()
    try:
        result = (
            supabase.table("certifications")
            .select("exam_name, display_name, certification_code, is_active")
            .eq("is_active", True)
            .order("display_name")
            .execute()
        )
        rows = result.data or []
        if rows:
            return rows
    except Exception:
        pass
    return [{
        "exam_name": DEFAULT_EXAM_NAME,
        "display_name": DEFAULT_EXAM_NAME,
        "certification_code": None,
        "is_active": True,
    }]


def get_user_preferred_language_code(email):
    """Use the user profile language everywhere. Do not let exam pages override it."""
    session_lang = str(st.session_state.get("preferred_language_code", "") or "").strip().lower()
    if session_lang:
        return session_lang

    email = str(email or "").strip().lower()
    if not email:
        return DEFAULT_LANGUAGE_CODE

    try:
        result = (
            get_supabase_client()
            .table("app_users")
            .select("preferred_language_code")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if rows and rows[0].get("preferred_language_code"):
            lang = str(rows[0]["preferred_language_code"]).strip().lower()
            st.session_state.preferred_language_code = lang
            return lang
    except Exception:
        pass

    return DEFAULT_LANGUAGE_CODE


@st.cache_data(ttl=300, show_spinner=False)
def fetch_question_bank(exam_name, language_code, free_mock_only=False):
    supabase = get_supabase_client()
    exam_name = exam_name or DEFAULT_EXAM_NAME
    language_code = language_code or DEFAULT_LANGUAGE_CODE

    questions_query = (
        supabase.table("questions")
        .select(
            "id, exam_name, language_code, category, difficulty, question_text, "
            "question_type, select_count, explanation, is_active, is_exam_eligible, "
            "quality_status, free_mock_exam, free_sample_order, practice_eligible, "
            "question_family_id, cognitive_level, concept_key, content_version, external_key"
        )
        .eq("exam_name", exam_name)
        .eq("language_code", language_code)
        .eq("is_active", True)
        .eq("is_exam_eligible", True)
        .eq("quality_status", "approved")
        .eq("mock_eligible", True)
    )

    if free_mock_only:
        questions_query = questions_query.eq("free_mock_exam", True)

    questions_result = questions_query.execute()
    raw_questions = questions_result.data or []
    if not raw_questions:
        return [], {
            "error": f"No approved active exam-eligible questions found for {exam_name} / language {language_code}.",
            "exam_name": exam_name,
            "language_code": language_code,
        }

    question_ids = [q["id"] for q in raw_questions]
    options_by_question = defaultdict(list)

    chunk_size = 100
    for i in range(0, len(question_ids), chunk_size):
        chunk = question_ids[i:i + chunk_size]
        options_result = (
            supabase.table("answer_options")
            .select("id, question_id, option_label, option_text, is_correct, display_order")
            .in_("question_id", chunk)
            .order("display_order")
            .execute()
        )
        for opt in options_result.data or []:
            options_by_question[opt["question_id"]].append(opt)

    normalized = []
    skipped_no_options = 0
    skipped_invalid_answer_key = 0

    from utils.question_answer_key import is_answer_key_valid

    for q in raw_questions:
        opts = options_by_question.get(q["id"], [])
        if not opts:
            skipped_no_options += 1
            continue

        category = (q.get("category") or "Uncategorized").strip()
        if category == "Sales and Marketing / Service Applications":
            continue

        question_type = (q.get("question_type") or "single").strip().lower()
        if question_type not in ["single", "multiple"]:
            question_type = "single"

        options = [o["option_text"] for o in opts]
        answers = [o["option_text"] for o in opts if o.get("is_correct")]
        if not answers:
            skipped_no_options += 1
            continue

        normalized.append({
            "id": q["id"],
            "exam_name": q.get("exam_name") or exam_name,
            "language_code": q.get("language_code") or language_code,
            "category": category,
            "topic": category,
            "difficulty": (q.get("difficulty") or "medium").strip().lower(),
            "question": q.get("question_text") or "",
            "question_text": q.get("question_text") or "",
            "type": question_type,
            "question_type": question_type,
            "select_count": q.get("select_count"),
            "options": options,
            "answers": answers,
            "explanation": q.get("explanation") or "",
            "free_mock_exam": bool(q.get("free_mock_exam")),
            "free_sample_order": q.get("free_sample_order"),
            # V40: used by repeat-resistant selection waterfall
            "practice_eligible": bool(q.get("practice_eligible", True)),
            "question_family_id": q.get("question_family_id"),
            # Prospective metadata — captured at attempt time in question_attempts rows
            "cognitive_level": q.get("cognitive_level"),
            "concept_key": q.get("concept_key"),
            "content_version": q.get("content_version"),
            "external_key": q.get("external_key"),
        })
        candidate = normalized[-1]
        if not is_answer_key_valid(candidate):
            normalized.pop()
            skipped_invalid_answer_key += 1
            continue

    meta = {
        "total_bank_questions": len(normalized),
        "skipped_no_options_or_answers": skipped_no_options,
        "skipped_invalid_answer_key": skipped_invalid_answer_key,
        "exam_name": exam_name,
        "language_code": language_code,
        "bank_category_counts": dict(Counter(q["category"] for q in normalized)),
        "bank_difficulty_counts": dict(Counter(q["difficulty"] for q in normalized)),
    }
    return normalized, meta


def select_by_difficulty(pool, count):
    if count <= 0:
        return []

    by_diff = defaultdict(list)
    for q in pool:
        by_diff[q.get("difficulty", "medium")].append(q)

    for items in by_diff.values():
        random.shuffle(items)

    target = {
        "easy": max(1, round(count * 0.20)) if count >= 5 else 0,
        "medium": max(1, round(count * 0.50)),
        "hard": max(1, count - (max(1, round(count * 0.20)) if count >= 5 else 0) - max(1, round(count * 0.50))),
    }

    selected = []
    selected_ids = set()

    for diff in ["easy", "medium", "hard"]:
        take = min(target.get(diff, 0), len(by_diff.get(diff, [])))
        for q in by_diff.get(diff, [])[:take]:
            if q["id"] not in selected_ids:
                selected.append(q)
                selected_ids.add(q["id"])

    if len(selected) < count:
        leftovers = [q for q in pool if q["id"] not in selected_ids]
        random.shuffle(leftovers)
        selected.extend(leftovers[: count - len(selected)])

    return selected[:count]


def load_paid_mock_history(supabase, user_email: str, exam_name: str) -> dict:
    """Load paid full-mock history for V40 repeat-resistant selection.

    Executes two queries:
      1. exam_attempts  – all completed Paid Mock Exam rows for this user/exam,
                          ordered by completed_at DESC.
      2. question_attempts – question-level exposure rows linked to those attempt IDs.

    Returns the history context dict produced by build_history_context().
    Raises on Supabase errors so the caller (_load_paid_mock_history_safe) can
    catch and fall back gracefully.
    """
    from utils.question_selection import build_history_context

    _empty: dict = {
        "seen_question_ids": set(),
        "exposure_count": {},
        "last_seen": {},
        "recent_attempt_ids": set(),
        "recent_question_ids": set(),
    }

    # Query 1 – all completed paid mock attempts, most-recent first
    attempts_result = (
        supabase.table("exam_attempts")
        .select("id, completed_at")
        .eq("user_email", user_email)
        .eq("exam_name", exam_name)
        .eq("mode", "Paid Mock Exam")
        .not_("completed_at", "is", "null")
        .order("completed_at", desc=True)
        .execute()
    )
    all_attempts = attempts_result.data or []

    if not all_attempts:
        return _empty

    attempt_ids = [a["id"] for a in all_attempts if a.get("id") is not None]
    if not attempt_ids:
        return _empty

    # Lookup: attempt_id → completed_at used as fallback when answered_at is null
    attempt_ts: dict = {
        str(a["id"]): a.get("completed_at") or ""
        for a in all_attempts
        if a.get("id") is not None
    }

    # Query 2 – question exposure rows; the per-question timestamp column is answered_at
    raw_qa_rows: list = []
    chunk_size = 100
    for i in range(0, len(attempt_ids), chunk_size):
        chunk = attempt_ids[i : i + chunk_size]
        qa_result = (
            supabase.table("question_attempts")
            .select("question_id, exam_attempt_id, answered_at")
            .in_("exam_attempt_id", chunk)
            .execute()
        )
        raw_qa_rows.extend(qa_result.data or [])

    # Remap to the field name expected by build_history_context ("completed_at").
    # answered_at is preferred; fall back to the parent attempt's completed_at
    # when answered_at is null so last-seen tracking still works.
    exposure_rows = [
        {
            "question_id": row.get("question_id"),
            "exam_attempt_id": row.get("exam_attempt_id"),
            "completed_at": (
                row.get("answered_at")
                or attempt_ts.get(str(row.get("exam_attempt_id") or ""), "")
            ),
        }
        for row in raw_qa_rows
    ]

    return build_history_context(all_attempts, exposure_rows, recent_attempt_count=2)


def _load_paid_mock_history_safe(exam_name: str):
    """Wrap load_paid_mock_history with full error isolation.

    Returns:
        dict  – valid history context (may be all-empty for a first-time user)
        None  – on any failure; callers must fall back to legacy selection
    """
    user_email = get_current_user_email()
    if not user_email:
        return {
            "seen_question_ids": set(),
            "exposure_count": {},
            "last_seen": {},
            "recent_attempt_ids": set(),
            "recent_question_ids": set(),
        }
    try:
        supabase = get_supabase_client()
        return load_paid_mock_history(supabase, user_email, exam_name)
    except Exception:
        # History is non-critical. Return None to signal failure; the caller
        # will fall back to the legacy difficulty-based selection without
        # surfacing any error or sensitive detail to the user.
        return None


def generate_paid_exam_questions(bank, category_counts, history=None):
    """Select questions for a paid full mock exam.

    When ``history`` is a valid dict (including an empty-history dict for a
    first-time user), the V40 repeat-resistant waterfall is applied within each
    domain quota.  When ``history`` is None (history-load failure), selection
    falls back to the legacy difficulty-based algorithm so exam generation is
    never blocked by a history failure.

    Difficulty (V40): per-domain difficulty targets remain the authority.  In
    the V40 path the waterfall ranking is applied *within* each difficulty
    bucket (see select_questions_for_domain); the legacy fallback path keeps
    select_by_difficulty().  In both paths the 8–10 multiple-answer balancing
    step now reuses the same V40 ranking/history rules when choosing
    replacements, so it can no longer reintroduce duplicate ids or families.
    """
    from utils.question_selection import select_paid_mock_questions, balance_multi_select

    by_category = defaultdict(list)
    for q in bank:
        by_category[q["category"]].append(q)

    # A valid (possibly empty) history dict so balancing can rank replacements
    # even when history loading failed.
    history_for_ranking = history if history is not None else {
        "seen_question_ids": set(),
        "exposure_count": {},
        "last_seen": {},
        "recent_attempt_ids": set(),
        "recent_question_ids": set(),
    }

    if history is not None:
        # V40 waterfall path (difficulty preserved inside the selector).
        result = select_paid_mock_questions(bank, category_counts, history)
        selected = result["selected"]
        missing = result["missing"]
    else:
        # Legacy difficulty-based path (fallback when history load failed).
        selected = []
        missing = []
        for category, required_count in category_counts.items():
            pool = by_category.get(category, [])
            if len(pool) < required_count:
                missing.append(f"{category}: need {required_count}, found {len(pool)}")
            selected.extend(select_by_difficulty(pool, required_count))

    if missing:
        st.error("Not enough questions in one or more categories for this certification/language:")
        for item in missing:
            st.write(f"- {item}")
        st.stop()

    # 8–10 multiple-answer balancing, V40-aware (same-category, waterfall-ranked,
    # id-unique, family-unique where inventory allows; 1-for-1 swaps preserve the
    # exact per-domain and total counts).
    selected = balance_multi_select(selected, by_category, history_for_ranking)

    random.shuffle(selected)
    return selected


def generate_free_mock_questions(bank, category_counts=None):
    """Generate the logged-in Free Preview.

    Free Preview is intentionally NOT tied to paid enrollment and NOT tied to
    the 60-question paid exam distribution. It must use exactly 10 fixed
    approved sample questions flagged in the database.
    """
    selected = list(bank)

    if len(selected) != 10:
        st.error("Free Preview setup error: expected exactly 10 approved sample questions for this certification/language.")
        st.info("In Supabase, verify exactly 10 rows have free_mock_exam = true for the selected certification/language. Use free_sample_order = 1 through 10 to control display order.")
        with st.expander("Setup details"):
            st.write(f"Found {len(selected)} free sample questions for this certification/language.")
        st.stop()

    def sample_sort_key(q):
        order = q.get("free_sample_order")
        try:
            order = int(order) if order is not None else 9999
        except Exception:
            order = 9999
        return (order, int(q.get("id") or 0))

    selected.sort(key=sample_sort_key)
    return selected


def ensure_exam_generated(exam_access_type, exam_name, language_code, category_counts):
    free_mock_only = (exam_access_type != "paid")
    try:
        bank, meta = fetch_question_bank(exam_name, language_code, free_mock_only=free_mock_only)
    except Exception as exc:
        log_and_get_user_message("fetch_question_bank failed", EXAM_BANK_LOAD_ERROR_MESSAGE, exc=exc)
        st.error(EXAM_BANK_LOAD_ERROR_MESSAGE)
        st.stop()

    st.session_state.bank_meta = meta

    if meta.get("error"):
        st.error(meta["error"])
        st.info("Choose a certification on this page. Language comes from your Account profile. Make sure that certification has questions imported for your preferred language.")
        st.stop()

    exam_key = f"{exam_access_type}|{exam_name}|{language_code}"
    existing_key = st.session_state.get("exam_key")
    if existing_key != exam_key:
        st.session_state.all_questions = []
        st.session_state.choice_orders = {}
        st.session_state.answers = {}
        st.session_state.marked = set()
        st.session_state.current_question = 0
        st.session_state.submitted = False
        st.session_state.started = False
        st.session_state.review_mode = False
        st.session_state.exam_access_type = exam_access_type
        st.session_state.exam_key = exam_key
        # A different exam set invalidates any in-flight submission snapshot/id.
        st.session_state.submission_save_state = "idle"
        st.session_state.submission_snapshot = None
        st.session_state.current_exam_attempt_id = None
        st.session_state.attempt_save_error = None
        st.session_state.save_retry_requested = False

    restored_questions = apply_pending_exam_state_if_valid(
        bank, exam_key, exam_name=exam_name, language_code=language_code,
    )
    if restored_questions:
        return restored_questions

    if "all_questions" not in st.session_state or not st.session_state.all_questions:
        if exam_access_type == "paid":
            # V40: load history; None signals failure → legacy fallback inside generator
            history = _load_paid_mock_history_safe(exam_name)
            st.session_state.all_questions = generate_paid_exam_questions(
                bank, category_counts, history=history
            )
        else:
            st.session_state.all_questions = generate_free_mock_questions(bank, category_counts)

    return st.session_state.all_questions


def format_diff(value):
    value = value or "medium"
    return str(value).strip().capitalize()


# Defaults are initialized before exam generation so access type/certification/language can control the exam set.
defaults = {
    "started": False,
    "submitted": False,
    "review_mode": False,
    "current_question": 0,
    "answers": {},
    "marked": set(),
    "start_time": None,
    "randomize_questions": True,
    "randomize_choices": True,
    "choice_orders": {},
    # Submission-persistence state machine (replaces the old attempt_save_checked
    # boolean). See utils/exam_submission.py.
    "submission_save_state": "idle",
    "submission_snapshot": None,
    "current_exam_attempt_id": None,
    "attempt_save_error": None,
    "save_retry_requested": False,
}

for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value



EXAM_STATE_QUERY_KEY = "exam_state"


def _query_param_value(name):
    try:
        value = st.query_params.get(name, "")
        if isinstance(value, list):
            return value[0] if value else ""
        return value or ""
    except Exception:
        return ""


def _decode_exam_state_from_query():
    raw = _query_param_value(EXAM_STATE_QUERY_KEY)
    if not raw:
        return None
    try:
        padding = "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode((raw + padding).encode("utf-8")).decode("utf-8")
        state = json.loads(decoded)
        if not isinstance(state, dict) or state.get("v") != 1:
            return None
        return state
    except Exception:
        return None


def _encode_exam_state_for_query(state):
    payload = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")


def _set_exam_state_query_value(value):
    try:
        current = _query_param_value(EXAM_STATE_QUERY_KEY)
        if current != value:
            st.query_params[EXAM_STATE_QUERY_KEY] = value
    except Exception:
        pass


def clear_exam_state_query():
    try:
        if EXAM_STATE_QUERY_KEY in st.query_params:
            del st.query_params[EXAM_STATE_QUERY_KEY]
    except Exception:
        pass


def _option_index(question, option_text):
    try:
        return question.get("options", []).index(option_text)
    except ValueError:
        return None


def persist_exam_state_to_query(questions=None):
    """Persist active exam state into the URL so browser refresh does not reset the exam.

    This is intentionally scoped to in-progress/submitted exams only. It preserves auth
    query params such as fr_session because it only writes/removes exam_state.
    """
    questions = questions or st.session_state.get("all_questions") or []
    if not questions:
        return

    if not st.session_state.get("started") and not st.session_state.get("submitted"):
        clear_exam_state_query()
        return

    answers_as_indexes = {}
    for idx, selected_options in (st.session_state.get("answers") or {}).items():
        try:
            q_index = int(idx)
        except Exception:
            q_index = idx
        if not isinstance(q_index, int) or q_index < 0 or q_index >= len(questions):
            continue
        indexes = []
        for option_text in selected_options or []:
            option_idx = _option_index(questions[q_index], option_text)
            if option_idx is not None:
                indexes.append(option_idx)
        if indexes:
            answers_as_indexes[str(q_index)] = indexes

    choice_orders_as_indexes = {}
    for idx, ordered_options in (st.session_state.get("choice_orders") or {}).items():
        try:
            q_index = int(idx)
        except Exception:
            q_index = idx
        if not isinstance(q_index, int) or q_index < 0 or q_index >= len(questions):
            continue
        indexes = []
        for option_text in ordered_options or []:
            option_idx = _option_index(questions[q_index], option_text)
            if option_idx is not None:
                indexes.append(option_idx)
        if indexes:
            choice_orders_as_indexes[str(q_index)] = indexes

    state = {
        "v": 1,
        "started": bool(st.session_state.get("started")),
        "submitted": bool(st.session_state.get("submitted")),
        "review_mode": bool(st.session_state.get("review_mode")),
        "current_question": int(st.session_state.get("current_question") or 0),
        "start_time": float(st.session_state.get("start_time") or time.time()),
        "randomize_choices": bool(st.session_state.get("randomize_choices", True)),
        "exam_access_type": st.session_state.get("exam_access_type"),
        "exam_name": st.session_state.get("selected_exam_name"),
        "language_code": SELECTED_LANGUAGE_CODE if "SELECTED_LANGUAGE_CODE" in globals() else st.session_state.get("selected_language_code"),
        "exam_key": st.session_state.get("exam_key"),
        "question_ids": [str(q.get("id")) for q in questions if q.get("id") is not None],
        "answers": answers_as_indexes,
        "marked": sorted([int(i) for i in (st.session_state.get("marked") or set()) if isinstance(i, int)]),
        "choice_orders": choice_orders_as_indexes,
        # Carry the certified exam duration so it survives browser reload and the
        # exemption block can validate elapsed time even before EXAM_MINUTES resolves.
        "time_limit_minutes": int(st.session_state.get("exam_time_limit_minutes") or EXAM_MINUTES_DEFAULT),
        # Carry the persistence state machine + parent id so a full browser
        # refresh after submission reuses the same parent (no duplicate parent
        # even beyond the 45-second recent-match window) and does not re-save an
        # already-saved attempt.
        "save_state": st.session_state.get("submission_save_state") or "idle",
        "attempt_id": st.session_state.get("current_exam_attempt_id"),
    }
    _set_exam_state_query_value(_encode_exam_state_for_query(state))


def _restore_indexed_answers(state, questions):
    answers = {}
    for raw_idx, selected_indexes in (state.get("answers") or {}).items():
        try:
            q_index = int(raw_idx)
        except Exception:
            continue
        if q_index < 0 or q_index >= len(questions):
            continue
        q_options = questions[q_index].get("options", [])
        restored = []
        for option_idx in selected_indexes or []:
            if isinstance(option_idx, int) and 0 <= option_idx < len(q_options):
                restored.append(q_options[option_idx])
        if restored:
            answers[q_index] = restored
    return answers


def _restore_choice_orders(state, questions):
    choice_orders = {}
    for raw_idx, option_indexes in (state.get("choice_orders") or {}).items():
        try:
            q_index = int(raw_idx)
        except Exception:
            continue
        if q_index < 0 or q_index >= len(questions):
            continue
        q_options = questions[q_index].get("options", [])
        restored = []
        for option_idx in option_indexes or []:
            if isinstance(option_idx, int) and 0 <= option_idx < len(q_options):
                restored.append(q_options[option_idx])
        if restored:
            choice_orders[q_index] = restored
    return choice_orders


def apply_pending_exam_state_if_valid(bank, exam_key, *, exam_name, language_code):
    state = st.session_state.get("_pending_exam_state")
    if not state or state.get("exam_key") != exam_key:
        return None

    question_ids = [str(qid) for qid in (state.get("question_ids") or []) if qid is not None]
    if not question_ids:
        return None

    bank_by_id = {str(q.get("id")): q for q in bank}
    restored_questions = [bank_by_id.get(qid) for qid in question_ids]
    if not restored_questions or any(q is None for q in restored_questions):
        return None

    st.session_state.started = bool(state.get("started"))
    st.session_state.submitted = bool(state.get("submitted"))
    st.session_state.review_mode = bool(state.get("review_mode"))
    st.session_state.current_question = max(0, min(int(state.get("current_question") or 0), len(restored_questions) - 1))
    st.session_state.start_time = float(state.get("start_time") or time.time())
    st.session_state.randomize_choices = bool(state.get("randomize_choices", True))
    st.session_state.exam_access_type = state.get("exam_access_type") or st.session_state.get("exam_access_type")
    st.session_state.answers = _restore_indexed_answers(state, restored_questions)
    st.session_state.marked = set(i for i in (state.get("marked") or []) if isinstance(i, int) and 0 <= i < len(restored_questions))
    st.session_state.choice_orders = _restore_choice_orders(state, restored_questions)
    st.session_state.all_questions = restored_questions
    # Restore the persistence state machine + parent id so a refresh after
    # submission reuses the same parent and never re-inserts a duplicate.
    #
    # The attempt id travels through the unsigned exam_state URL query
    # parameter, so it is never trusted as-is here: it must be re-verified
    # against this exam_attempts row's user_email/mode/exam_name/language_code
    # before it is written into current_exam_attempt_id. A tampered,
    # mismatched, or unverifiable (query failure) id is discarded -- fails
    # closed -- rather than accepted; the rest of the restoration (questions,
    # answers, timing, save_state) still proceeds normally either way.
    restored_attempt_id = state.get("attempt_id")
    if restored_attempt_id is not None:
        from utils.question_selection import verify_exam_attempt_ownership
        verified_attempt_id = None
        restoring_user_email = get_current_user_email()
        if restoring_user_email:
            try:
                verified_attempt_id = verify_exam_attempt_ownership(
                    get_supabase_client(), restored_attempt_id,
                    expected_user_email=restoring_user_email,
                    expected_mode="Paid Mock Exam",
                    expected_exam_name=exam_name,
                    expected_language_code=language_code,
                )
            except Exception:
                verified_attempt_id = None  # fail closed: discard, never raise mid-restore
        if verified_attempt_id is not None:
            st.session_state.current_exam_attempt_id = verified_attempt_id
        # else: discarded silently. A fresh parent is created on next save
        # if one is still needed; it never reaches child persistence.
    restored_save_state = state.get("save_state")
    if restored_save_state in {"idle", "saving", "saved", "failed"}:
        st.session_state.submission_save_state = restored_save_state
    # Restore exam duration so the exemption block can validate remaining time on
    # reruns that follow this restoration (e.g. the 1-second autorefresh).
    # Route the untrusted URL value through the hardened validator: it rejects
    # bool/NaN/inf/malformed/huge values without ever raising (no int() on raw URL).
    restored_minutes = _valid_exam_duration_minutes(state.get("time_limit_minutes"))
    if restored_minutes > 0:
        st.session_state["exam_time_limit_minutes"] = int(restored_minutes)
    st.session_state["_exam_state_restored_once"] = True
    st.session_state.pop("_pending_exam_state", None)
    return restored_questions


pending_exam_state = _decode_exam_state_from_query()
if pending_exam_state and not st.session_state.get("_exam_state_restored_once"):
    st.session_state["_pending_exam_state"] = pending_exam_state
    if pending_exam_state.get("exam_name"):
        st.session_state.selected_exam_name = pending_exam_state.get("exam_name")
    if pending_exam_state.get("started") or pending_exam_state.get("submitted"):
        st.session_state.started = bool(pending_exam_state.get("started"))
        st.session_state.submitted = bool(pending_exam_state.get("submitted"))


# Language comes from the user profile. Certification is selected directly on this page.
user_email_for_language = get_current_user_email()
if not user_email_for_language:
    st.warning("Please log in from the Account page before starting an exam.")
    st.stop()

SELECTED_LANGUAGE_CODE = get_user_preferred_language_code(user_email_for_language)
LANGUAGE_LABEL = fetch_language_label(SELECTED_LANGUAGE_CODE)

# Free Preview must not require an active certification enrollment.
# Paid/admin users also get all active certifications for the launch bundle unless
# explicit enrollment rows exist and you later choose to enforce per-cert access.
subscription_status_for_cert_picker = get_user_subscription_status(user_email_for_language)
has_paid_access_for_cert_picker = is_paid_subscription(subscription_status_for_cert_picker)
user_enrolled_certs = fetch_user_certifications(user_email_for_language)
all_active_certs = fetch_active_certifications()

if has_paid_access_for_cert_picker and user_enrolled_certs:
    AVAILABLE_CERTIFICATIONS = user_enrolled_certs
else:
    AVAILABLE_CERTIFICATIONS = all_active_certs

if not AVAILABLE_CERTIFICATIONS:
    st.error("No active certifications are configured.")
    st.info("Admin setup required: add active rows in the certifications table.")
    st.stop()

CERT_DISPLAY_BY_NAME = {
    row.get("exam_name"): row.get("display_name") or row.get("exam_name")
    for row in AVAILABLE_CERTIFICATIONS
    if row.get("exam_name")
}
CERT_NAMES = list(CERT_DISPLAY_BY_NAME.keys()) or [DEFAULT_EXAM_NAME]

current_exam = st.session_state.get("selected_exam_name")
if current_exam not in CERT_NAMES:
    current_exam = CERT_NAMES[0]
    st.session_state.selected_exam_name = current_exam

if not st.session_state.get("started", False):
    st.markdown(
        """
        <div class="exam-shell-top">
            <div class="exam-kicker">Certification Practice Exam</div>
            <div class="exam-page-title">Choose your mock exam</div>
            <div class="exam-page-subtitle">Pick a certification, then start the free preview or full mock exam based on your access.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    selector_col, access_col = st.columns([2.2, 1])
    with selector_col:
        selected_exam = st.selectbox(
            "Choose Certification",
            options=CERT_NAMES,
            index=CERT_NAMES.index(current_exam),
            format_func=lambda name: CERT_DISPLAY_BY_NAME.get(name, name),
            key="mock_exam_certification_selector",
        )
    with access_col:
        st.caption("Current access")
        st.write("Premium" if is_paid_subscription(get_user_subscription_status(user_email_for_language)) else "Free Preview")

    if selected_exam != st.session_state.get("selected_exam_name"):
        st.session_state.selected_exam_name = selected_exam
        st.session_state.all_questions = []
        st.session_state.choice_orders = {}
        st.session_state.answers = {}
        st.session_state.marked = set()
        st.session_state.current_question = 0
        st.session_state.submitted = False
        st.session_state.review_mode = False
        st.session_state.submission_save_state = "idle"
        st.session_state.submission_snapshot = None
        st.session_state.current_exam_attempt_id = None
        st.session_state.attempt_save_error = None
        st.session_state.save_retry_requested = False
        st.session_state.exam_key = None
        st.rerun()

SELECTED_EXAM_NAME = st.session_state.get("selected_exam_name") or current_exam
exam_setup = fetch_exam_setup(SELECTED_EXAM_NAME)

PASSING_SCORE = exam_setup["passing_score"]
EXAM_MINUTES = exam_setup["time_limit_minutes"]
# Persist so the exemption block (which runs before this point on every rerun) can
# read the correct certification-specific duration on the *next* run onwards.
st.session_state["exam_time_limit_minutes"] = int(EXAM_MINUTES)
QUESTION_COUNT = exam_setup["question_count"]
EXAM_TITLE = f"{exam_setup['display_name']} Mock Exam"
CERTIFICATION = exam_setup["display_name"]
CATEGORY_COUNTS = exam_setup["category_counts"]
CATEGORY_WEIGHTS = exam_setup["category_weights"]
DOMAIN_ROWS = exam_setup["domains"]


def get_access_context():
    user_email = get_current_user_email()
    subscription_status = "free"
    has_paid_access = False

    if user_email:
        subscription_status = get_user_subscription_status(user_email)
        has_paid_access = is_paid_subscription(subscription_status)

    exam_access_type = "paid" if has_paid_access else "free"
    return user_email, subscription_status, has_paid_access, exam_access_type


user_email, subscription_status, has_paid_access, exam_access_type = get_access_context()
all_questions = ensure_exam_generated(exam_access_type, SELECTED_EXAM_NAME, SELECTED_LANGUAGE_CODE, CATEGORY_COUNTS)
questions = all_questions
if st.session_state.get("started") or st.session_state.get("submitted"):
    persist_exam_state_to_query(questions)


def get_options(q_index, q):
    if q_index not in st.session_state.choice_orders:
        options = q["options"].copy()
        if st.session_state.randomize_choices:
            random.shuffle(options)
        st.session_state.choice_orders[q_index] = options
    return st.session_state.choice_orders[q_index]


def is_correct(user_answer, correct_answers, question=None):
    from utils.question_answer_key import is_answer_correct

    if question is not None:
        return is_answer_correct(user_answer, question)
    return set(user_answer or []) == set(correct_answers or [])


def calculate_breakdown(field):
    stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for i, q in enumerate(questions):
        value = q.get(field, "Uncategorized")
        stats[value]["total"] += 1
        if is_correct(st.session_state.answers.get(i, []), q["answers"], question=q):
            stats[value]["correct"] += 1
    return stats


def plain_breakdown(stats):
    return {
        str(key): {
            "correct": int(value.get("correct", 0)),
            "total": int(value.get("total", 0)),
            "percent": round((value.get("correct", 0) / value.get("total", 1)) * 100, 2) if value.get("total", 0) else 0,
        }
        for key, value in stats.items()
    }


def _capture_exception_safe(exc):
    """Forward an exception to Sentry without ever raising. No payloads/PII."""
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def _reset_submission_state():
    """Clear all submission-persistence state for a genuinely new exam."""
    st.session_state.submission_save_state = "idle"
    st.session_state.submission_snapshot = None
    st.session_state.current_exam_attempt_id = None
    st.session_state.attempt_save_error = None
    st.session_state.save_retry_requested = False


def _capture_submission_snapshot():
    """Freeze the exact scored questions/answers at submission time.

    The snapshot is the single source of truth for scoring, persistence, and the
    results UI, so later session mutation or question-bank regeneration can never
    change what was scored or what gets saved.
    """
    from utils.exam_submission import build_submission_snapshot
    snapshot = build_submission_snapshot(
        st.session_state.get("all_questions") or [],
        st.session_state.get("answers") or {},
        submitted_at_iso=datetime.now(timezone.utc).isoformat(),
        exam_name=SELECTED_EXAM_NAME,
        language_code=SELECTED_LANGUAGE_CODE,
        mode=st.session_state.get("exam_access_type", "unknown"),
    )
    st.session_state.submission_snapshot = snapshot
    st.session_state.submission_save_state = "idle"
    st.session_state.attempt_save_error = None
    st.session_state.save_retry_requested = False
    return snapshot


def _save_question_attempts_batch(
    supabase,
    exam_attempt_id,
    user_email: str,
    completed_at,
    questions,
    answers,
    total_questions=None,
):
    """Persist one question_attempts row per question for a completed paid mock.

    ``questions`` and ``answers`` must be the immutable scored snapshot passed
    by the caller — this function never reads from st.session_state so that a
    stale or cleared session cannot silently produce zero child rows.

    Idempotent: rows are upserted on the (exam_attempt_id, question_id) unique
    constraint in chunks of 50, so a Streamlit rerun repairs missing/partial
    rows instead of duplicating or skipping them.  The final saved row count is
    verified against the expected total; a wrong count returns a safe error.

    Returns (ok: bool, error: Optional[str]).  Never raises.
    """
    from utils.question_selection import build_question_attempt_rows, persist_question_attempts
    from utils.paid_mock_diagnostics import (
        log_batch_enter,
        log_batch_question_ids,
        log_batch_rows_built,
    )

    questions = list(questions or [])
    answers = dict(answers or {})

    log_batch_enter(
        passed_question_count=len(questions),
        answer_count=len(answers),
        expected_count=total_questions,
    )

    if not questions:
        _capture_exception_safe(
            RuntimeError("_save_question_attempts_batch called with empty questions list")
        )
        return False, "Detailed question results could not be saved. Please try again."

    qids = [q.get("id") for q in questions]
    log_batch_question_ids(
        distinct_count=len({qid for qid in qids if qid is not None}),
        null_count=sum(1 for qid in qids if qid is None),
    )

    rows = build_question_attempt_rows(
        questions,
        answers,
        exam_attempt_id=exam_attempt_id,
        user_email=user_email,
        default_exam_name=SELECTED_EXAM_NAME,
        default_language_code=SELECTED_LANGUAGE_CODE,
        answered_at_iso=completed_at.isoformat(),
        time_spent_by_index=st.session_state.get("question_time_spent") or {},
    )

    log_batch_rows_built(built_count=len(rows))

    expected = int(total_questions) if total_questions is not None else len(rows)

    return persist_question_attempts(
        supabase,
        rows,
        exam_attempt_id=exam_attempt_id,
        expected_count=expected,
        chunk_size=50,
        on_error=_capture_exception_safe,
    )


def _persist_children_and_report(supabase, attempt_id, user_email, completed_at, questions, answers, total_questions):
    """Persist child rows for a known parent id and emit the result event."""
    from utils.paid_mock_diagnostics import log_child_persistence_call, log_save_exam_attempt_result
    log_child_persistence_call(attempt_id=attempt_id, passed_question_count=len(list(questions or [])))
    result = _save_question_attempts_batch(
        supabase, attempt_id, user_email, completed_at,
        questions, answers, total_questions
    )
    log_save_exam_attempt_result(success=result[0], error_category=type(result[1]).__name__ if result[1] else None)
    return result


def save_exam_attempt(score, correct, total_questions, domain_breakdown, difficulty_breakdown, *, questions, answers):
    # TRUE call boundary: this is the very first statement so a "save_exam_attempt
    # was entered" signal is emitted before any early return can hide it.
    from utils.paid_mock_diagnostics import (
        log_duplicate_guard_result,
        log_parent_id_resolved,
        log_parent_id_reused,
        log_parent_insert_complete,
        log_parent_insert_start,
        log_save_exam_attempt_enter,
        log_save_exam_attempt_result,
    )

    mode = "Paid Mock Exam" if st.session_state.get("exam_access_type") == "paid" else "Free Mock Exam"
    log_save_exam_attempt_enter(mode=mode)

    user_email = get_current_user_email()
    if not user_email:
        return False, "No account email saved. Open the Account page and save your email first."

    supabase = get_supabase_client()
    completed_at = datetime.now(timezone.utc)

    try:
        started_at = datetime.fromtimestamp(float(st.session_state.get("start_time") or time.time()), tz=timezone.utc)
    except Exception:
        started_at = completed_at

    is_paid = (mode == "Paid Mock Exam")

    # 1) PARENT ID REUSE — the authoritative guard against duplicate parents.
    # If this submission already created a parent (any prior rerun/retry), reuse
    # that exact id and retry ONLY child persistence. This never depends on the
    # 45-second window, so a delayed retry still repairs the same parent.
    #
    # The stored id is not trusted blindly: it can arrive either from
    # same-session Streamlit session_state or from the unsigned exam_state URL
    # query parameter (see apply_pending_exam_state_if_valid), which a user can
    # edit directly. It is re-verified here against this exam_attempts row's
    # user_email/mode/exam_name/language_code every time. A verification-query
    # failure propagates unchanged (fails closed) rather than falling through
    # to the heuristic or a new insert below.
    if is_paid:
        reused_id = st.session_state.get("current_exam_attempt_id")
        if reused_id is not None:
            from utils.question_selection import verify_exam_attempt_ownership
            verified_id = verify_exam_attempt_ownership(
                supabase, reused_id,
                expected_user_email=user_email,
                expected_mode=mode,
                expected_exam_name=SELECTED_EXAM_NAME,
                expected_language_code=SELECTED_LANGUAGE_CODE,
            )
            if verified_id is not None:
                log_parent_id_reused(attempt_id=verified_id)
                return _persist_children_and_report(
                    supabase, verified_id, user_email, completed_at, questions, answers, total_questions
                )
            # Stale or mismatched id (e.g. tampered URL state, or a same-tab
            # account switch): discard it and fall through to the existing
            # 45-second heuristic / insert path below, unchanged.
            st.session_state.current_exam_attempt_id = None

    # 2) DUPLICATE GUARD (fallback) — recent-match lookup for the case where the
    # stored id was lost. Still avoids a second parent within the window.
    recent_cutoff = (completed_at - timedelta(seconds=45)).isoformat()
    existing_attempt_id = None
    try:
        existing = (
            supabase.table("exam_attempts")
            .select("id")
            .eq("user_email", user_email)
            .eq("exam_name", SELECTED_EXAM_NAME)
            .eq("language_code", SELECTED_LANGUAGE_CODE)
            .eq("mode", mode)
            .eq("total_questions", int(total_questions))
            .eq("correct_answers", int(correct))
            .gte("completed_at", recent_cutoff)
            .order("completed_at", desc=True)
            .limit(1)
            .execute()
        )
        existing_rows = getattr(existing, "data", None) or []
        if existing_rows:
            existing_attempt_id = existing_rows[0].get("id")
    except Exception:
        # Do not block saving if the duplicate check fails. The insert error handler below
        # will still catch real write failures.
        pass

    log_duplicate_guard_result(existing_attempt_id=existing_attempt_id)

    if existing_attempt_id is not None:
        if is_paid:
            # Store the recovered id so every later retry reuses it (Step 1).
            st.session_state.current_exam_attempt_id = existing_attempt_id
            return _persist_children_and_report(
                supabase, existing_attempt_id, user_email, completed_at, questions, answers, total_questions
            )
        return True, None

    # 3) PARENT INSERT — only reached when no parent exists for this submission.
    # Eligible bank size: use the value loaded when the question bank was fetched
    # (cached in bank_meta). Falls back to 0 if metadata is absent.
    bank_meta = st.session_state.get("bank_meta") or {}
    eligible_bank_size = int(bank_meta.get("total_bank_questions") or 0)

    payload = {
        "user_email": user_email,
        "mode": mode,
        "category": "All Domains",
        "score": float(score),
        "total_questions": int(total_questions),
        "correct_answers": int(correct),
        "domain_breakdown": domain_breakdown,
        "difficulty_breakdown": difficulty_breakdown,
        "exam_name": SELECTED_EXAM_NAME,
        "language_code": SELECTED_LANGUAGE_CODE,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "eligible_question_bank_size": eligible_bank_size,
    }

    log_parent_insert_start()
    try:
        insert_result = supabase.table("exam_attempts").insert(payload).execute()
    except Exception as exc:
        _capture_exception_safe(exc)
        log_save_exam_attempt_result(success=False, error_category="parent_insert_exception")
        return False, "Your exam score could not be saved. Please try again."

    log_parent_insert_complete(returned_data_count=len(getattr(insert_result, "data", None) or []))

    # Free mocks do not track per-question rows; parent insert is enough.
    if not is_paid:
        return True, None

    # Use the returned id; fall back to a recent-match lookup if the insert
    # response did not include one, so child rows can still be attached.
    from utils.question_selection import resolve_exam_attempt_id
    attempt_id = resolve_exam_attempt_id(
        insert_result,
        recover_fn=lambda: _recover_recent_attempt_id(
            supabase, user_email, mode, total_questions, correct, recent_cutoff
        ),
    )

    log_parent_id_resolved(attempt_id=attempt_id)

    if attempt_id is None:
        # Parent is saved, but we cannot locate its id to attach child rows.
        # Preserve the parent; a later rerun can backfill via the guard above.
        log_save_exam_attempt_result(success=False, error_category="attempt_id_unresolved")
        return False, "Your attempt was saved, but detailed question results could not be linked yet."

    # STORE THE PARENT ID IMMEDIATELY so a child-write failure (or any rerun)
    # reuses this exact parent and never inserts a second one.
    st.session_state.current_exam_attempt_id = attempt_id

    return _persist_children_and_report(
        supabase, attempt_id, user_email, completed_at, questions, answers, total_questions
    )


def _recover_recent_attempt_id(supabase, user_email, mode, total_questions, correct, recent_cutoff):
    """Best-effort lookup of a just-saved exam_attempts.id via recent match.

    Used only when an insert response did not return the id. Never raises.
    """
    try:
        result = (
            supabase.table("exam_attempts")
            .select("id")
            .eq("user_email", user_email)
            .eq("exam_name", SELECTED_EXAM_NAME)
            .eq("language_code", SELECTED_LANGUAGE_CODE)
            .eq("mode", mode)
            .eq("total_questions", int(total_questions))
            .eq("correct_answers", int(correct))
            .gte("completed_at", recent_cutoff)
            .order("completed_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        return rows[0].get("id") if rows else None
    except Exception:
        return None


def reset_exam():
    clear_exam_state_query()
    for key in list(defaults.keys()) + ["all_questions", "bank_meta", "exam_key", "_pending_exam_state", "_exam_state_restored_once"]:
        if key in st.session_state:
            del st.session_state[key]
    fetch_question_bank.clear()
    st.rerun()


# Layout CSS is injected near the top of the file before widgets render.

st.markdown(
    f"""
    <div class="exam-banner">{EXAM_TITLE}</div>
    <div class="exam-sub-banner">
        {CERTIFICATION} | {len(all_questions)} questions | {EXAM_MINUTES} minutes | Passing score: {PASSING_SCORE}%
    </div>
    """,
    unsafe_allow_html=True,
)

st.caption(f"App Version: {APP_VERSION}")

if not st.session_state.started:
    st.header("Exam Instructions")
    st.success(f"Question bank ready ✅ | {'Paid randomized mock exam' if has_paid_access else 'Free fixed sample mock exam'} | {len(all_questions)} questions")
    st.caption(f"Preferred language: {LANGUAGE_LABEL}")

    if user_email:
        st.success(f"Account email: {user_email} ✅")
        if has_paid_access:
            st.success(f"Subscription status: {subscription_status} ✅ Paid access: randomized full mock exam unlocked")
        else:
            st.info("Free access: fixed sample mock exam unlocked. Results and explanations are included at the end.")
            st.caption("Upgrade later to unlock unlimited randomized mock exams, My Progress, Weak Areas Practice, and the larger question bank.")
    else:
        st.warning("Please open the Account page and save/sign in with your email before starting the exam. This is required so your result can be associated with your account.")
        st.info("After saving your email in Account, return to this page to start the free sample mock exam.")

    st.markdown(
        """
        <div class="exam-card">
            <p>Choose the certification above. Your exam language is pulled automatically from your Account profile.</p>
            <p>Free users get a fixed sample mock exam. Paid users get randomized full mock exams.</p>
            <p>Answers and explanations are hidden until after final submission.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Questions", len(all_questions))
    c2.metric("Time Limit", f"{EXAM_MINUTES} min")
    c3.metric("Passing Score", f"{PASSING_SCORE}%")

    st.subheader("Exam Domain Breakdown")
    for row in DOMAIN_ROWS:
        domain = row.get("domain_name")
        count = int(row.get("question_count") or CATEGORY_COUNTS.get(domain, 0))
        weight = row.get("weight") or CATEGORY_WEIGHTS.get(domain, 0)
        st.write(f"- **{domain}** — {format_domain_weight(weight)}% / {count} questions")

    st.info(
        """
        - Single-answer questions use radio buttons.
        - Multiple-answer questions use checkboxes.
        - Answer choices are randomized.
        - You may mark questions for review and return before submitting.
        - Unanswered questions count as incorrect.
        - Explanations appear only after final submission.
        """
    )

    st.session_state.randomize_choices = st.checkbox("Randomize answer choices", value=st.session_state.randomize_choices)

    col_start, col_regen = st.columns(2)
    with col_start:
        begin_disabled = (user_email is None)
        if st.button("Begin Exam", type="primary", disabled=begin_disabled):
            st.session_state.started = True
            st.session_state.start_time = time.time()
            st.session_state.choice_orders = {}
            st.session_state.answers = {}
            st.session_state.marked = set()
            st.session_state.current_question = 0
            st.session_state.review_mode = False
            st.session_state.submitted = False
            _reset_submission_state()
            persist_exam_state_to_query(questions)
            st.rerun()
    with col_regen:
        if st.button("Start New Exam"):
            reset_exam()

elif not st.session_state.submitted:
    st_autorefresh(interval=1000, key="exam_timer_refresh")

    elapsed = time.time() - st.session_state.start_time
    remaining = (EXAM_MINUTES * 60) - elapsed

    if remaining <= 0:
        st.session_state.submitted = True
        _capture_submission_snapshot()
        st.rerun()

    mins = int(remaining // 60)
    secs = int(remaining % 60)

    st.markdown(
        f"""
        <div class="exam-floating-timer">
            <div class="exam-floating-timer-label">Time Remaining</div>
            <div class="exam-floating-timer-value">{mins:02d}:{secs:02d}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.sidebar.markdown(
        """
        <div class="question-nav-title">Question Navigator</div>
        <div class="small-help">✓ answered &nbsp;&nbsp; 🚩 marked</div>
        """,
        unsafe_allow_html=True,
    )

    for i in range(len(questions)):
        label = f"Question {i + 1}"
        if i in st.session_state.answers:
            label += " ✓"
        if i in st.session_state.marked:
            label += " 🚩"
        if st.sidebar.button(label, key=f"nav_{i}"):
            st.session_state.current_question = i
            st.session_state.review_mode = False
            persist_exam_state_to_query(questions)
            st.rerun()

    if st.session_state.review_mode:
        st.header("Review Before Final Submission")
        answered = len(st.session_state.answers)
        unanswered = len(questions) - answered
        marked = len(st.session_state.marked)

        c1, c2, c3 = st.columns(3)
        c1.metric("Answered", answered)
        c2.metric("Unanswered", unanswered)
        c3.metric("Marked", marked)

        if unanswered > 0:
            st.warning(f"You still have {unanswered} unanswered question(s). You can submit, but unanswered questions count as incorrect.")

        st.divider()
        for i in range(len(questions)):
            status = "Answered" if i in st.session_state.answers else "Unanswered"
            if i in st.session_state.marked:
                status += " | 🚩 Marked"
            st.write(f"Question {i + 1}: {status}")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Return to Exam"):
                st.session_state.review_mode = False
                persist_exam_state_to_query(questions)
                st.rerun()
        with col2:
            if st.button("Final Submit", type="primary"):
                st.session_state.submitted = True
                _capture_submission_snapshot()
                persist_exam_state_to_query(questions)
                st.rerun()

    else:
        q_index = st.session_state.current_question
        q = questions[q_index]
        options = get_options(q_index, q)

        answered = len(st.session_state.answers)
        marked = len(st.session_state.marked)

        st.markdown(
            f"""
            <div class="status-strip">
                <strong>Question:</strong> {q_index + 1} of {len(questions)}
                &nbsp;&nbsp; | &nbsp;&nbsp;
                <strong>Answered:</strong> {answered}
                &nbsp;&nbsp; | &nbsp;&nbsp;
                <strong>Marked:</strong> {marked}
                &nbsp;&nbsp; | &nbsp;&nbsp;
                <strong>Time:</strong> {mins:02d}:{secs:02d}
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.progress((q_index + 1) / len(questions))
        st.markdown('<div class="question-card">', unsafe_allow_html=True)
        st.markdown(
            f"<div class='exam-question-meta'>Domain: {q.get('category', 'Uncategorized')} &nbsp; | &nbsp; Difficulty: {format_diff(q.get('difficulty', 'medium'))}</div>",
            unsafe_allow_html=True,
        )
        st.subheader(q["question"])

        from utils.question_answer_key import (
            apply_multi_select_answer_ui,
            is_multiple_select,
        )

        if is_multiple_select(q):
            selected_answers = apply_multi_select_answer_ui(
                q,
                previous_selection=st.session_state.answers.get(q_index, []),
                key_prefix=f"q_{q_index}",
                session_state=st.session_state,
                checkbox_fn=st.checkbox,
                warning_fn=st.warning,
                limit_message_fn=lambda count: st.info(
                    f"You can only select {count} answers. Deselect an option to choose a different one."
                ),
            )
            if selected_answers:
                st.session_state.answers[q_index] = selected_answers
            elif q_index in st.session_state.answers:
                del st.session_state.answers[q_index]
        else:
            previous_answer = st.session_state.answers.get(q_index, [])
            previous_answer = previous_answer[0] if previous_answer else None
            selected = st.radio(
                "Choose one answer.",
                options,
                index=options.index(previous_answer) if previous_answer in options else None,
                key=f"question_{q_index}",
            )
            if selected:
                st.session_state.answers[q_index] = [selected]

        st.markdown("</div>", unsafe_allow_html=True)
        persist_exam_state_to_query(questions)

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            if st.button("← Previous") and q_index > 0:
                st.session_state.current_question -= 1
                st.rerun()
        with col2:
            if st.button("Next →") and q_index < len(questions) - 1:
                st.session_state.current_question += 1
                st.rerun()
        with col3:
            if q_index in st.session_state.marked:
                if st.button("Unmark"):
                    st.session_state.marked.remove(q_index)
                    st.rerun()
            else:
                if st.button("Mark for Review"):
                    st.session_state.marked.add(q_index)
                    st.rerun()
        with col4:
            if st.button("Review / Submit", type="primary"):
                st.session_state.review_mode = True
                st.rerun()

else:
    from utils.exam_submission import (
        STATE_FAILED,
        STATE_SAVED,
        plan_persistence,
        resolve_final_state,
        snapshot_distinct_question_ids,
        snapshot_question_count,
    )
    from utils.paid_mock_diagnostics import (
        log_results_persistence_branch_enter,
        log_save_call_after,
        log_save_call_before,
        log_save_call_exception,
        log_save_state_transition,
        log_submission_snapshot_ready,
    )

    log_results_persistence_branch_enter()

    # ── IMMUTABLE SNAPSHOT ────────────────────────────────────────────────────
    # The snapshot is captured at submit time. If we somehow reached results
    # without one (e.g. restored mid-flight), capture it now from current state.
    if not st.session_state.get("submission_snapshot"):
        _capture_submission_snapshot()
    snapshot = st.session_state.get("submission_snapshot") or {}

    snap_questions = snapshot.get("questions") or []
    snap_answers = snapshot.get("answers") or {}
    score = snapshot.get("score", 0.0)
    correct = snapshot.get("correct", 0)
    total = snapshot.get("total", len(snap_questions))
    domain_breakdown_json = snapshot.get("domain_breakdown") or {}
    difficulty_breakdown_json = snapshot.get("difficulty_breakdown") or {}

    log_submission_snapshot_ready(
        question_count=snapshot_question_count(snapshot),
        answer_count=len(snap_answers),
        distinct_question_count=snapshot_distinct_question_ids(snapshot),
    )

    # ── PERSISTENCE (runs BEFORE any results UI) ──────────────────────────────
    # No component, metric, query-param write, or other UI call may sit between
    # this state check and the actual save call.
    current_state = st.session_state.get("submission_save_state", "idle")
    retry_requested = bool(st.session_state.pop("save_retry_requested", False))
    action, saving_state = plan_persistence(current_state, retry_requested)

    if action == "run":
        st.session_state.submission_save_state = saving_state
        log_save_state_transition(from_state=current_state, to_state=saving_state)
        log_save_call_before()
        try:
            saved, save_error = save_exam_attempt(
                score=score,
                correct=correct,
                total_questions=total,
                domain_breakdown=domain_breakdown_json,
                difficulty_breakdown=difficulty_breakdown_json,
                questions=snap_questions,
                answers=snap_answers,
            )
        except Exception as exc:  # never let a raw DB error reach the user
            _capture_exception_safe(exc)
            log_save_call_exception(exc=exc)
            saved, save_error = False, "Your result could not be saved. Use Retry Saving Result below."
        log_save_call_after(success=saved)
        final_state = resolve_final_state(saved)
        st.session_state.attempt_save_error = None if saved else save_error
        log_save_state_transition(from_state=saving_state, to_state=final_state)
        st.session_state.submission_save_state = final_state

        # ── READINESS SNAPSHOT (secondary; only after paid mock save succeeds) ──
        # Persisted AFTER parent + child verification passes.  Failure is logged
        # and reported but does NOT change the primary save state; the retry button
        # re-enters this block and the upsert is idempotent.
        if saved and st.session_state.get("exam_access_type") == "paid":
            attempt_id_for_snapshot = st.session_state.get("current_exam_attempt_id")
            if attempt_id_for_snapshot is not None:
                from utils.readiness_persistence import compute_and_persist_readiness_snapshot  # noqa: PLC0415
                bank_meta = st.session_state.get("bank_meta") or {}
                snap_bank_size = int(bank_meta.get("total_bank_questions") or 0)
                snap_ok, snap_err = compute_and_persist_readiness_snapshot(
                    get_supabase_client(),
                    user_email=get_current_user_email() or "",
                    exam_name=SELECTED_EXAM_NAME,
                    exam_attempt_id=attempt_id_for_snapshot,
                    eligible_bank_size=snap_bank_size,
                    on_error=_capture_exception_safe,
                )
                if not snap_ok:
                    _capture_exception_safe(
                        RuntimeError(f"Readiness snapshot failed: {snap_err}")
                    )

    save_state = st.session_state.get("submission_save_state", "idle")

    # Sync the resolved parent id + save state into the URL (a query-param write,
    # intentionally AFTER persistence) so a later browser refresh reuses the same
    # parent and skips re-saving an already-saved attempt.
    persist_exam_state_to_query(snap_questions)

    # ── RESULTS UI (only after persistence resolves) ──────────────────────────
    st.header("Exam Results")

    c1, c2, c3 = st.columns(3)
    c1.metric("Score", f"{score}%")
    c2.metric("Correct", f"{correct} / {total}")
    c3.metric("Passing Score", f"{PASSING_SCORE}%")

    if score >= PASSING_SCORE:
        st.success("PASS")
    else:
        st.error("FAIL")

    if save_state == STATE_SAVED:
        st.success("Attempt saved to progress tracking ✅")
    elif save_state == STATE_FAILED:
        # Visible, safe error (no raw DB message) + an explicit repair action.
        st.warning(
            st.session_state.get("attempt_save_error")
            or "Attempt was scored, but saving to Supabase did not fully complete."
        )
        if st.button("Retry Saving Result", type="primary"):
            from utils.paid_mock_diagnostics import log_save_retry_requested
            log_save_retry_requested()
            st.session_state.save_retry_requested = True
            st.rerun()

    st.divider()
    st.header("Performance Breakdown")

    st.subheader("By Domain")
    for domain in CATEGORY_COUNTS.keys():
        data = domain_breakdown_json.get(domain, {"correct": 0, "total": 0})
        if data.get("total", 0) == 0:
            continue
        percent = round((data["correct"] / data["total"]) * 100, 2)
        st.write(f"**{domain}:** {data['correct']} / {data['total']} correct ({percent}%)")

    if st.session_state.get("exam_access_type") == "paid":
        st.subheader("By Difficulty")
        for difficulty in ["easy", "medium", "hard"]:
            data = difficulty_breakdown_json.get(difficulty, {"correct": 0, "total": 0})
            if data.get("total", 0) == 0:
                continue
            percent = round((data["correct"] / data["total"]) * 100, 2)
            st.write(f"**{format_diff(difficulty)}:** {data['correct']} / {data['total']} correct ({percent}%)")

    st.divider()
    st.header("Answer Review")

    review_filter = st.radio("Review filter:", ["All Questions", "Incorrect Only", "Correct Only"], horizontal=True)

    for i, q in enumerate(snap_questions):
        user_answer = snap_answers.get(i, [])
        correct_answers = q["answers"]
        result_correct = is_correct(user_answer, correct_answers, question=q)

        if review_filter == "Incorrect Only" and result_correct:
            continue
        if review_filter == "Correct Only" and not result_correct:
            continue

        if result_correct:
            st.success(f"Question {i + 1} — Correct")
        else:
            st.error(f"Question {i + 1} — Incorrect")

        st.caption(f"Domain: {q.get('category', 'Uncategorized')} | Difficulty: {format_diff(q.get('difficulty', 'medium'))}")
        st.write(q["question"])
        st.write("Your answer: " + (", ".join(user_answer) if user_answer else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_answers))
        st.info(q["explanation"])
        st.divider()

    if st.button("Start New Exam", type="primary"):
        reset_exam()
