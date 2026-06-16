import json
import base64
import time
import random
from collections import defaultdict, Counter
from datetime import datetime, timezone
import os

import streamlit as st
from streamlit_autorefresh import st_autorefresh
from supabase import create_client
from utils.access_control import render_app_chrome, get_current_user_email as shared_get_current_user_email, get_user_subscription_status as shared_get_user_subscription_status, get_preferred_language_code as shared_get_preferred_language_code
import streamlit.components.v1 as components


APP_VERSION = "SUPABASE_DB_V14_EXAM_REFRESH_PERSISTENCE"
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


PAID_STATUS_VALUES = {"active", "paid", "trialing"}


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
                d["domain_name"]: int(d.get("weight") or 0)
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
def fetch_question_bank(exam_name, language_code):
    supabase = get_supabase_client()
    exam_name = exam_name or DEFAULT_EXAM_NAME
    language_code = language_code or DEFAULT_LANGUAGE_CODE

    questions_query = (
        supabase.table("questions")
        .select("id, exam_name, language_code, category, difficulty, question_text, question_type, select_count, explanation, is_active, is_exam_eligible, quality_status, free_mock_exam, free_sample_order")
        .eq("exam_name", exam_name)
        .eq("language_code", language_code)
        .eq("is_active", True)
        .eq("is_exam_eligible", True)
        .eq("quality_status", "approved")
    )

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
        })

    meta = {
        "total_bank_questions": len(normalized),
        "skipped_no_options_or_answers": skipped_no_options,
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


def generate_paid_exam_questions(bank, category_counts):
    selected = []
    by_category = defaultdict(list)
    for q in bank:
        by_category[q["category"]].append(q)

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

    min_multi = 8
    max_multi = 10
    multi_count = sum(1 for q in selected if q.get("type") == "multiple")

    if multi_count < min_multi:
        selected_ids = {q["id"] for q in selected}
        for idx, q in enumerate(list(selected)):
            if multi_count >= min_multi:
                break
            if q.get("type") == "multiple":
                continue
            same_category_multi = [
                candidate for candidate in by_category[q["category"]]
                if candidate.get("type") == "multiple" and candidate["id"] not in selected_ids
            ]
            if same_category_multi:
                replacement = random.choice(same_category_multi)
                selected_ids.remove(q["id"])
                selected_ids.add(replacement["id"])
                selected[idx] = replacement
                multi_count += 1

    if multi_count > max_multi:
        selected_ids = {q["id"] for q in selected}
        for idx, q in enumerate(list(selected)):
            if multi_count <= max_multi:
                break
            if q.get("type") != "multiple":
                continue
            same_category_single = [
                candidate for candidate in by_category[q["category"]]
                if candidate.get("type") == "single" and candidate["id"] not in selected_ids
            ]
            if same_category_single:
                replacement = random.choice(same_category_single)
                selected_ids.remove(q["id"])
                selected_ids.add(replacement["id"])
                selected[idx] = replacement
                multi_count -= 1

    random.shuffle(selected)
    return selected


def generate_free_mock_questions(bank, category_counts=None):
    """Generate the logged-in Free Preview.

    Free Preview is intentionally NOT tied to paid enrollment and NOT tied to
    the 60-question paid exam distribution. It must use exactly 10 fixed
    approved sample questions flagged in the database.
    """
    selected = [q for q in bank if q.get("free_mock_exam") is True]

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
    bank, meta = fetch_question_bank(exam_name, language_code)
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
        st.session_state.attempt_saved = False
        st.session_state.exam_access_type = exam_access_type
        st.session_state.exam_key = exam_key

    restored_questions = apply_pending_exam_state_if_valid(bank, exam_key)
    if restored_questions:
        return restored_questions

    if "all_questions" not in st.session_state or not st.session_state.all_questions:
        if exam_access_type == "paid":
            st.session_state.all_questions = generate_paid_exam_questions(bank, category_counts)
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
    "attempt_saved": False,
    "attempt_save_checked": False,
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


def apply_pending_exam_state_if_valid(bank, exam_key):
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
        st.session_state.attempt_saved = False
        st.session_state.exam_key = None
        st.rerun()

SELECTED_EXAM_NAME = st.session_state.get("selected_exam_name") or current_exam
exam_setup = fetch_exam_setup(SELECTED_EXAM_NAME)

PASSING_SCORE = exam_setup["passing_score"]
EXAM_MINUTES = exam_setup["time_limit_minutes"]
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


def is_correct(user_answer, correct_answers):
    return set(user_answer) == set(correct_answers)


def calculate_breakdown(field):
    stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for i, q in enumerate(questions):
        value = q.get(field, "Uncategorized")
        stats[value]["total"] += 1
        if is_correct(st.session_state.answers.get(i, []), q["answers"]):
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


def save_exam_attempt(score, correct, total_questions, domain_breakdown, difficulty_breakdown):
    user_email = get_current_user_email()
    if not user_email:
        return False, "No account email saved. Open the Account page and save your email first."

    payload = {
        "user_email": user_email,
        "mode": "Paid Mock Exam" if st.session_state.get("exam_access_type") == "paid" else "Free Mock Exam",
        "category": "All Domains",
        "score": float(score),
        "total_questions": int(total_questions),
        "correct_answers": int(correct),
        "domain_breakdown": domain_breakdown,
        "difficulty_breakdown": difficulty_breakdown,
        "exam_name": SELECTED_EXAM_NAME,
        "language_code": SELECTED_LANGUAGE_CODE,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        get_supabase_client().table("exam_attempts").insert(payload).execute()
        return True, None
    except Exception as exc:
        return False, str(exc)


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
        weight = int(row.get("weight") or CATEGORY_WEIGHTS.get(domain, 0))
        st.write(f"- **{domain}** — {weight}% / {count} questions")

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
            st.session_state.attempt_saved = False
            st.session_state.attempt_save_checked = False
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

        question_type = q.get("type", "single")
        if question_type == "multiple":
            select_count = q.get("select_count") or len(q.get("answers", []))
            st.warning(f"Choose {select_count} answers.")
            selected_answers = []
            for option in options:
                checked = option in st.session_state.answers.get(q_index, [])
                if st.checkbox(option, value=checked, key=f"q_{q_index}_{option}"):
                    selected_answers.append(option)
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
    correct = 0
    for i, q in enumerate(questions):
        if is_correct(st.session_state.answers.get(i, []), q["answers"]):
            correct += 1

    score = round((correct / len(questions)) * 100, 2)

    domain_stats = calculate_breakdown("category")
    difficulty_stats = calculate_breakdown("difficulty")
    domain_breakdown_json = plain_breakdown(domain_stats)
    difficulty_breakdown_json = plain_breakdown(difficulty_stats)

    if not st.session_state.get("attempt_save_checked", False):
        saved, save_error = save_exam_attempt(
            score=score,
            correct=correct,
            total_questions=len(questions),
            domain_breakdown=domain_breakdown_json,
            difficulty_breakdown=difficulty_breakdown_json,
        )
        st.session_state.attempt_saved = saved
        st.session_state.attempt_save_error = save_error
        st.session_state.attempt_save_checked = True

    st.header("Exam Results")

    c1, c2, c3 = st.columns(3)
    c1.metric("Score", f"{score}%")
    c2.metric("Correct", f"{correct} / {len(questions)}")
    c3.metric("Passing Score", f"{PASSING_SCORE}%")

    if score >= PASSING_SCORE:
        st.success("PASS")
    else:
        st.error("FAIL")

    if st.session_state.get("attempt_saved"):
        st.success("Attempt saved to progress tracking ✅")
    elif st.session_state.get("attempt_save_error"):
        st.warning("Attempt was scored, but it was not saved to Supabase. Check exam_attempts columns if this continues.")

    st.divider()
    st.header("Performance Breakdown")

    st.subheader("By Domain")
    for domain in CATEGORY_COUNTS.keys():
        data = domain_stats.get(domain, {"correct": 0, "total": 0})
        if data["total"] == 0:
            continue
        percent = round((data["correct"] / data["total"]) * 100, 2)
        st.write(f"**{domain}:** {data['correct']} / {data['total']} correct ({percent}%)")

    if st.session_state.get("exam_access_type") == "paid":
        st.subheader("By Difficulty")
        for difficulty in ["easy", "medium", "hard"]:
            data = difficulty_stats.get(difficulty, {"correct": 0, "total": 0})
            if data["total"] == 0:
                continue
            percent = round((data["correct"] / data["total"]) * 100, 2)
            st.write(f"**{format_diff(difficulty)}:** {data['correct']} / {data['total']} correct ({percent}%)")

    st.divider()
    st.header("Answer Review")

    review_filter = st.radio("Review filter:", ["All Questions", "Incorrect Only", "Correct Only"], horizontal=True)

    for i, q in enumerate(questions):
        user_answer = st.session_state.answers.get(i, [])
        correct_answers = q["answers"]
        result_correct = is_correct(user_answer, correct_answers)

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
