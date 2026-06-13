import json
import time
import random
from collections import defaultdict, Counter
from datetime import datetime, timezone

import streamlit as st
from streamlit_autorefresh import st_autorefresh


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
    render_locked_premium_previews,
    get_available_certifications,
    has_premium_access,
    is_admin_unlocked,
    get_current_user_email,
    get_supabase_client,
    get_supabase_public_client,
    get_subscription_status,
    is_paid_user,
    PAID_STATUS_VALUES,
    ADMIN_EXAM_NAME,
    FALLBACK_CERTIFICATIONS,
)

FALLBACK_CERT_BY_EXAM = {cert["exam_name"]: cert for cert in FALLBACK_CERTIFICATIONS}
APP_VERSION = "SUPABASE_DB_V13_CERT_SWITCH_FIX"
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

PASSING_SCORE_DEFAULT = 65
EXAM_MINUTES_DEFAULT = 105
QUESTION_COUNT_DEFAULT = 60


st.set_page_config(
    page_title="Salesforce Certification Mock Exam",
    layout="wide",
    initial_sidebar_state="expanded",
)
try:
    st.set_option("client.showSidebarNavigation", False)
except Exception:
    pass
render_app_chrome()


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


def get_selected_exam_name():
    return st.session_state.get("selected_exam_name") or config.get("certification") or DEFAULT_EXAM_NAME


def get_selected_language_code():
    return st.session_state.get("selected_language_code") or DEFAULT_LANGUAGE_CODE


def build_admin_domain_fallback():
    return {
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


def enrich_setup_from_bank(setup, bank, question_count):
    """Align domain breakdown with categories that exist in the selected certification bank."""
    if not bank:
        return setup

    resolved_counts = resolve_category_counts(setup.get("category_counts") or {}, bank, question_count)
    if not resolved_counts:
        return setup

    total = sum(resolved_counts.values()) or 1
    domains = []
    weights = {}
    for idx, (name, count) in enumerate(sorted(resolved_counts.items())):
        weight = max(1, round(100 * count / total))
        domains.append({
            "domain_name": name,
            "weight": weight,
            "question_count": count,
            "display_order": idx + 1,
        })
        weights[name] = weight

    weight_sum = sum(weights.values()) or 1
    for name in weights:
        weights[name] = round(100 * weights[name] / weight_sum)

    enriched = setup.copy()
    enriched["domains"] = domains
    enriched["category_counts"] = resolved_counts
    enriched["category_weights"] = weights
    return enriched


@st.cache_data(ttl=300, show_spinner=False)
def fetch_exam_setup(exam_name):
    """Load exam metadata and domain structure from Supabase.
    Falls back to per-cert defaults; Admin domains only apply to the Admin exam.
    """
    supabase = get_supabase_public_client()
    exam_name = exam_name or DEFAULT_EXAM_NAME
    fallback_cert = FALLBACK_CERT_BY_EXAM.get(exam_name, {})
    admin_domains = build_admin_domain_fallback() if exam_name == ADMIN_EXAM_NAME else {
        "category_counts": {},
        "category_weights": {},
        "domains": [],
    }

    setup = {
        "exam_name": exam_name,
        "display_name": fallback_cert.get("display_name") or exam_name,
        "certification_code": None,
        "passing_score": int(fallback_cert.get("passing_score") or PASSING_SCORE_DEFAULT),
        "time_limit_minutes": int(fallback_cert.get("time_limit_minutes") or EXAM_MINUTES_DEFAULT),
        "question_count": int(fallback_cert.get("question_count") or QUESTION_COUNT_DEFAULT),
        **admin_domains,
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
            get_supabase_public_client()
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
    """Return certifications visible to this user (Admin + BA at minimum)."""
    return get_available_certifications()


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
        .select("id, exam_name, language_code, category, difficulty, question_text, question_type, select_count, explanation, is_active, is_exam_eligible, quality_status, free_mock_exam")
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


def resolve_category_counts(category_counts, bank, question_count):
    """Use DB domain targets when they match the bank; otherwise derive from actual categories."""
    by_category = defaultdict(int)
    for q in bank:
        by_category[q["category"]] += 1

    if category_counts:
        sufficient = True
        for category, required_count in category_counts.items():
            if required_count > 0 and by_category.get(category, 0) < required_count:
                sufficient = False
                break
        if sufficient:
            return category_counts

    return derive_category_counts_from_bank(bank, question_count)


def derive_category_counts_from_bank(bank, target_count):
    """Build per-category question targets from categories that exist in the bank."""
    pools = defaultdict(list)
    for q in bank:
        pools[q["category"]].append(q)
    if not pools:
        return {}

    categories = sorted(pools.keys())
    total_available = sum(len(pools[c]) for c in categories)
    target = min(int(target_count or 60), total_available)
    if target <= 0:
        return {}

    counts = {c: 1 for c in categories}
    remaining = target - len(categories)
    if remaining < 0:
        counts = {}
        for cat in sorted(categories, key=lambda name: -len(pools[name]))[:target]:
            counts[cat] = 1
        return counts

    weights = {c: len(pools[c]) for c in categories}
    weight_total = sum(weights.values()) or 1
    for cat in categories:
        counts[cat] += int(round(remaining * weights[cat] / weight_total))

    while sum(counts.values()) > target:
        cat = max(counts, key=lambda name: counts[name])
        if counts[cat] > 1:
            counts[cat] -= 1
        else:
            break
    while sum(counts.values()) < target:
        cat = max(categories, key=lambda name: len(pools[name]) - counts.get(name, 0))
        if counts[cat] < len(pools[cat]):
            counts[cat] += 1
        else:
            break

    return counts


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
        available = ", ".join(sorted({q["category"] for q in bank}))
        st.session_state.exam_setup_error = (
            "Not enough questions in one or more categories for this certification/language: "
            + "; ".join(missing)
            + (f". Available categories in bank: {available}" if available else "")
        )
        return []

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


def generate_free_mock_questions(bank, category_counts):
    """Free Preview: exactly 10 fixed sample questions.

    Preferred source is questions.free_mock_exam = true. If fewer than 10 are tagged,
    fall back to the first approved questions so the preview does not break during setup.
    """
    sample = [q for q in bank if q.get("free_mock_exam") is True]
    if len(sample) < 10:
        sample_ids = {q["id"] for q in sample}
        fallback = [q for q in bank if q["id"] not in sample_ids]
        fallback.sort(key=lambda q: (q.get("category", ""), q.get("id", 0)))
        sample.extend(fallback[: 10 - len(sample)])

    if len(sample) < 10:
        st.session_state.exam_setup_error = (
            f"Free Preview setup error: expected 10 sample questions, found {len(sample)}."
        )
        return []

    category_order = list(category_counts.keys())
    sample.sort(key=lambda q: (category_order.index(q["category"]) if q["category"] in category_order else 999, q["id"]))
    return sample[:10]


def ensure_exam_generated(exam_access_type, exam_name, language_code, category_counts, question_count=60):
    bank, meta = fetch_question_bank(exam_name, language_code)
    st.session_state.bank_meta = meta

    if meta.get("error"):
        st.session_state.exam_setup_error = meta["error"]
        return []

    st.session_state.pop("exam_setup_error", None)

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

    if "all_questions" not in st.session_state or not st.session_state.all_questions:
        if exam_access_type == "paid":
            resolved_counts = resolve_category_counts(category_counts, bank, question_count)
            st.session_state.all_questions = generate_paid_exam_questions(bank, resolved_counts)
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


# Language comes from the user profile when signed in; otherwise default to English.
user_email_for_language = get_current_user_email()
SELECTED_LANGUAGE_CODE = get_user_preferred_language_code(user_email_for_language)
LANGUAGE_LABEL = fetch_language_label(SELECTED_LANGUAGE_CODE)

AVAILABLE_CERTIFICATIONS = fetch_user_certifications(user_email_for_language)

CERT_DISPLAY_BY_NAME = {
    row.get("exam_name"): row.get("display_name") or row.get("exam_name")
    for row in AVAILABLE_CERTIFICATIONS
    if row.get("exam_name")
}
CERT_NAMES = list(CERT_DISPLAY_BY_NAME.keys()) or [DEFAULT_EXAM_NAME]

if st.session_state.get("selected_exam_name") not in CERT_NAMES:
    st.session_state.selected_exam_name = CERT_NAMES[0]

if not st.session_state.started:
    st.subheader("Choose Certification")
    st.selectbox(
        "Certification",
        options=CERT_NAMES,
        format_func=lambda name: CERT_DISPLAY_BY_NAME.get(name, name),
        key="selected_exam_name",
        label_visibility="collapsed",
    )

SELECTED_EXAM_NAME = st.session_state.get("selected_exam_name") or CERT_NAMES[0]
exam_setup = fetch_exam_setup(SELECTED_EXAM_NAME)
exam_setup["display_name"] = CERT_DISPLAY_BY_NAME.get(SELECTED_EXAM_NAME, exam_setup["display_name"])

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
        subscription_status = get_subscription_status(user_email)
        has_paid_access = is_paid_user(user_email)

    exam_access_type = "paid" if has_paid_access else "free"
    return user_email, subscription_status, has_paid_access, exam_access_type


user_email, subscription_status, has_paid_access, exam_access_type = get_access_context()
if is_admin_unlocked():
    has_paid_access = True
    exam_access_type = "paid"

# Free Preview is intentionally short. Premium keeps official exam timing.
if exam_access_type == "free":
    EXAM_MINUTES = 20
    EXAM_TITLE = f"{exam_setup['display_name']} Free Preview"
else:
    EXAM_TITLE = f"{exam_setup['display_name']} Mock Exam"

all_questions = ensure_exam_generated(
    exam_access_type,
    SELECTED_EXAM_NAME,
    SELECTED_LANGUAGE_CODE,
    CATEGORY_COUNTS,
    QUESTION_COUNT,
)
questions = all_questions

question_bank, _bank_meta = fetch_question_bank(SELECTED_EXAM_NAME, SELECTED_LANGUAGE_CODE)
exam_setup = enrich_setup_from_bank(exam_setup, question_bank, QUESTION_COUNT)
PASSING_SCORE = exam_setup["passing_score"]
EXAM_MINUTES = exam_setup["time_limit_minutes"] if exam_access_type == "paid" else 20
CERTIFICATION = exam_setup["display_name"]
CATEGORY_COUNTS = exam_setup["category_counts"]
CATEGORY_WEIGHTS = exam_setup["category_weights"]
DOMAIN_ROWS = exam_setup["domains"]
if exam_access_type == "free":
    EXAM_TITLE = f"{exam_setup['display_name']} Free Preview"
else:
    EXAM_TITLE = f"{exam_setup['display_name']} Mock Exam"


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
        "mode": "Paid Mock Exam" if st.session_state.get("exam_access_type") == "paid" else "Free Preview",
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
    for key in list(defaults.keys()) + ["all_questions", "bank_meta", "exam_key"]:
        if key in st.session_state:
            del st.session_state[key]
    fetch_question_bank.clear()
    st.rerun()


st.markdown(
    """
    <style>
    .block-container { max-width: 1120px; padding-top: 2rem !important; padding-bottom: 2rem !important; }
    header[data-testid="stHeader"] { height: 0px; }
    .exam-banner { background: #16325c; color: white; padding: 18px 22px; border-radius: 8px 8px 0 0; font-size: 27px; font-weight: 700; line-height: 1.25; margin-top: 10px; }
    .exam-sub-banner { background: #f4f6f9; border: 1px solid #d8dde6; border-top: none; padding: 12px 20px; border-radius: 0 0 8px 8px; margin-bottom: 30px; color: #16325c; font-size: 15px; }
    .exam-card { border: 1px solid #d8dde6; border-radius: 8px; padding: 18px 20px; background: #ffffff; margin-bottom: 18px; }
    .question-card { border: 1px solid #d8dde6; border-radius: 8px; padding: 22px; background: #ffffff; margin-top: 12px; margin-bottom: 18px; }
    .status-strip { background: #f8f9fb; border: 1px solid #d8dde6; border-radius: 8px; padding: 12px 16px; margin-bottom: 15px; }
    section[data-testid="stSidebar"] > div:first-child { padding-top: 0.75rem; }
    .timer-sticky { position: sticky; top: 0; z-index: 999; background: #ffffff; padding-top: 4px; padding-bottom: 14px; border-bottom: 1px solid #d8dde6; margin-bottom: 14px; }
    .timer-label { font-weight: 700; font-size: 16px; margin-bottom: 7px; color: #1f2937; }
    .timer-box { background: #fff4d6; border: 1px solid #e0b84f; border-radius: 8px; padding: 12px; text-align: center; font-size: 27px; font-weight: 800; color: #1f2937; letter-spacing: 1px; }
    .question-nav-title { font-weight: 700; font-size: 16px; margin-top: 10px; margin-bottom: 8px; color: #1f2937; }
    .small-help { color: #5f6368; font-size: 13px; margin-bottom: 8px; }
    section[data-testid="stSidebar"] div.stButton > button { width: 100%; padding: 0.35rem 0.5rem; font-size: 14px; }
    div.stButton > button { border-radius: 6px; font-weight: 600; }
    </style>
    """,
    unsafe_allow_html=True,
)

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
    setup_error = st.session_state.get("exam_setup_error")
    if setup_error:
        st.error(setup_error)
        st.info(
            "Choose a certification above. Language comes from your Account profile. "
            "Make sure that certification has questions imported for your preferred language."
        )

    st.header("Exam Instructions")
    if all_questions:
        st.success(f"Question bank ready ✅ | {'Premium randomized full mock exam' if has_paid_access else 'Free Preview'} | {len(all_questions)} questions")
    else:
        st.warning("Question bank is not ready yet for this certification and language.")
    st.caption(f"Preferred language: {LANGUAGE_LABEL}")

    if user_email:
        st.success(f"Account email: {user_email} ✅")
        if has_paid_access:
            st.success(f"Subscription status: {subscription_status} ✅ Premium unlocked")
        else:
            st.info("Free Preview: 10 fixed sample questions, basic score, and full explanations for all 10 sample questions.")
            st.caption("Premium unlocks full 60-question exams, the full question bank, category practice, weak-area practice, visual progress, and readiness scoring.")
    else:
        st.warning("Please log in from the Account page before starting an exam.")
        st.page_link("pages/Account.py", label="Go to Account to sign in", icon="👤")

    st.markdown(
        """
        <div class="exam-card">
            <p><strong>Free Preview:</strong> 10 fixed sample questions with full explanations after submission.</p>
            <p><strong>Premium Launch Plan:</strong> $29.99 for 3 months for a limited time. Regular price $49.99.</p>
            <p><strong>Premium includes:</strong> Salesforce Administrator + Business Analyst, full mock exams, full question bank, Practice by Category, Weak Areas Practice, Visual Progress, and Visual Readiness Score.</p>
            <p>Answers and explanations are hidden until after final submission.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Questions", len(all_questions))
    c2.metric("Time Limit", f"{EXAM_MINUTES} min")
    c3.metric("Passing Score", f"{PASSING_SCORE}%")

    if has_paid_access:
        st.subheader("Exam Domain Breakdown")
        for row in DOMAIN_ROWS:
            domain = row.get("domain_name")
            count = int(row.get("question_count") or CATEGORY_COUNTS.get(domain, 0))
            weight = int(row.get("weight") or CATEGORY_WEIGHTS.get(domain, 0))
            st.write(f"- **{domain}** — {weight}% / {count} questions")
    else:
        st.subheader("Free Preview")
        st.write("- 10 fixed sample questions")
        st.write("- Basic score after submission")
        st.write("- Full explanations for all 10 sample questions")
        st.write("- Premium previews remain locked until upgrade")

    st.info(
        """
        - Single-answer questions use radio buttons.
        - Multiple-answer questions use checkboxes.
        - Answer choices are randomized.
        - You may mark questions for review and return before submitting.
        - Unanswered questions count as incorrect.
        - Explanations appear only after final submission.
        - Free Preview results are a sample only and do not represent full exam readiness.
        """
    )

    st.session_state.randomize_choices = st.checkbox("Randomize answer choices", value=st.session_state.randomize_choices)

    col_start, col_regen = st.columns(2)
    with col_start:
        begin_disabled = (user_email is None) or (not all_questions)
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

    st.sidebar.markdown(
        f"""
        <div class="timer-sticky">
            <div class="timer-label">Time Remaining</div>
            <div class="timer-box">{mins:02d}:{secs:02d}</div>
        </div>
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
                st.rerun()
        with col2:
            if st.button("Final Submit", type="primary"):
                st.session_state.submitted = True
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
        st.caption(f"Domain: {q.get('category', 'Uncategorized')} | Difficulty: {format_diff(q.get('difficulty', 'medium'))}")
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

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            if st.button("Previous") and q_index > 0:
                st.session_state.current_question -= 1
                st.rerun()
        with col2:
            if st.button("Next") and q_index < len(questions) - 1:
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

    st.subheader("By Difficulty")
    for difficulty in ["easy", "medium", "hard"]:
        data = difficulty_stats.get(difficulty, {"correct": 0, "total": 0})
        if data["total"] == 0:
            continue
        percent = round((data["correct"] / data["total"]) * 100, 2)
        st.write(f"**{format_diff(difficulty)}:** {data['correct']} / {data['total']} correct ({percent}%)")

    if st.session_state.get("exam_access_type") != "paid":
        st.divider()
        render_locked_premium_previews()

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
