import random
from collections import defaultdict
from datetime import datetime, timezone
import time

import streamlit as st
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.access_control import (
    get_supabase_admin_client,
    render_app_chrome,
    render_session_page_link,
    get_current_user_email as shared_get_current_user_email,
    require_login,
    has_premium_access,
)

from utils.question_answer_key import (
    apply_multi_select_answer_ui,
    effective_explanation_feedback_shown,
    EXPLANATION_GATE_HINT,
    is_answer_correct,
    is_answer_key_valid,
    is_answer_selection_complete,
    is_multiple_select,
)
from utils.practice_session_persistence import (
    capture_option_orders,
    clear_category_practice_state,
    decode_pending_category_practice_state,
    persist_category_practice_state,
    restore_category_practice_session,
)
from utils.user_errors import PRACTICE_SAVE_ERROR_MESSAGE, log_and_get_user_message
from utils.activity_modes import DAILY_SPRINT, PRACTICE_BY_CATEGORY
from utils.version import APP_VERSION
QUESTION_COUNT_OPTIONS = [10, 20, 30]
DAILY_SPRINT_QUESTION_COUNT = 10
DAILY_SPRINT_AUTO_START_GUARD = "daily_sprint_auto_start_attempted"
DAILY_SPRINT_DASHBOARD_PAGE = "pages/Dashboard.py"

st.set_page_config(page_title="Practice by Category", layout="wide", initial_sidebar_state="expanded")
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
    supabase = get_supabase_client()
    result = (
        supabase.table("app_users")
        .select("email,full_name,subscription_status,preferred_language_code")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    return (result.data or [{}])[0]


@st.cache_data(ttl=60)
def fetch_languages():
    supabase = get_supabase_client()
    try:
        result = (
            supabase.table("languages")
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


def get_daily_sprint_params():
    """Read Daily Sprint query params sent by Dashboard."""
    try:
        params = st.query_params
        is_daily = str(params.get("daily_sprint", "")).strip() == "1"
        exam_name = str(params.get("exam_name", "")).strip()
        category = str(params.get("category", "")).strip()
        try:
            count = int(params.get("count", 10))
        except Exception:
            count = 10
        count = max(1, min(count, 30))
        return is_daily, exam_name, category, count
    except Exception:
        return False, "", "", 10


def build_available_categories(domains, question_bank):
    available_categories = [d for d in domains if any(q["category"] == d for q in question_bank)]
    extra_categories = sorted({q["category"] for q in question_bank if q["category"] not in available_categories})
    available_categories.extend(extra_categories)
    return available_categories


def select_practice_questions(question_bank, selected_category, selected_count):
    """Select and shuffle questions using the Start Practice button logic."""
    category_questions = [q for q in question_bank if q["category"] == selected_category]
    grouped = defaultdict(list)
    for q in category_questions:
        grouped[q["difficulty"]].append(q)
    for difficulty in grouped:
        random.shuffle(grouped[difficulty])

    selected = []
    while len(selected) < selected_count and sum(len(v) for v in grouped.values()) > 0:
        for d in ["easy", "medium", "hard"] + [x for x in grouped.keys() if x not in {"easy", "medium", "hard"}]:
            if len(selected) >= selected_count:
                break
            if grouped.get(d):
                selected.append(grouped[d].pop())
    random.shuffle(selected)
    for q in selected:
        random.shuffle(q["options"])
    return selected


def initialize_practice_session(
    selected,
    selected_category,
    selected_count,
    selected_exam,
    language_code,
    mode_label,
    session_state,
):
    from utils.practice_session_persistence import capture_option_orders, clear_category_practice_state

    clear_category_practice_state()
    session_state["practice_questions"] = selected
    session_state["practice_option_orders"] = capture_option_orders(selected)
    session_state["practice_category"] = selected_category
    session_state["practice_count"] = selected_count
    session_state["practice_exam_name"] = selected_exam
    session_state["practice_language_code"] = language_code
    session_state["practice_mode_label"] = mode_label
    session_state["practice_started"] = True
    session_state["practice_started_at"] = time.time()
    session_state["practice_submitted"] = False
    session_state["practice_current_index"] = 0
    session_state["practice_answers"] = {}
    session_state["practice_feedback_shown"] = False
    session_state["practice_saved"] = False
    session_state["practice_exam_attempt_id"] = None
    session_state["practice_question_time_spent"] = {}
    session_state["practice_question_entered_at"] = time.time()
    session_state["practice_timing_index"] = int(session_state.get("practice_current_index") or 0)


def maybe_auto_start_daily_sprint(
    *,
    is_daily_sprint,
    daily_sprint_exam_name,
    daily_sprint_category,
    premium,
    exam_names,
    question_bank,
    domains,
    language_code,
    session_state,
    rerun_fn,
):
    """Auto-start Daily Sprint from deep-link params. Returns True after triggering rerun."""
    if not is_daily_sprint or not premium:
        return False
    if session_state.get("practice_started"):
        return False
    if session_state.get(DAILY_SPRINT_AUTO_START_GUARD):
        return False
    if not daily_sprint_exam_name or not daily_sprint_category:
        return False
    if daily_sprint_exam_name not in exam_names:
        return False

    available_categories = build_available_categories(domains, question_bank)
    if daily_sprint_category not in available_categories:
        return False

    selected_count = DAILY_SPRINT_QUESTION_COUNT
    available_count = sum(1 for q in question_bank if q["category"] == daily_sprint_category)
    if available_count < selected_count:
        return False

    session_state[DAILY_SPRINT_AUTO_START_GUARD] = True
    selected = select_practice_questions(question_bank, daily_sprint_category, selected_count)
    if len(selected) < selected_count:
        session_state.pop(DAILY_SPRINT_AUTO_START_GUARD, None)
        return False

    initialize_practice_session(
        selected,
        daily_sprint_category,
        selected_count,
        daily_sprint_exam_name,
        language_code,
        DAILY_SPRINT,
        session_state,
    )
    rerun_fn()
    return True


def is_daily_sprint_session(session_state) -> bool:
    return str(session_state.get("practice_mode_label") or "").strip() == DAILY_SPRINT


def practice_results_heading(session_state) -> str:
    return "Daily Sprint Complete" if is_daily_sprint_session(session_state) else "Practice Results"


def format_practice_score_metric(score) -> str:
    return f"{score}%"


def format_practice_correct_metric(correct, total) -> str:
    return f"{correct} / {total}"


def build_practice_completion_view(score, correct, total, session_state) -> dict:
    sprint = is_daily_sprint_session(session_state)
    return {
        "heading": practice_results_heading(session_state),
        "score_metric": format_practice_score_metric(score),
        "correct_metric": format_practice_correct_metric(correct, total),
        "review_heading": "Answer Review",
        "show_dashboard_return": sprint,
        "dashboard_path": DAILY_SPRINT_DASHBOARD_PAGE,
        "dashboard_label": "Back to Dashboard",
        "show_primary_start_new_practice": not sprint,
    }


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
    supabase = get_supabase_client()
    result = (
        supabase.table("certification_domains")
        .select("domain_name,display_order,is_active")
        .eq("exam_name", exam_name)
        .eq("is_active", True)
        .order("display_order")
        .execute()
    )
    return [row["domain_name"] for row in (result.data or [])]


@st.cache_data(ttl=60)
def fetch_question_bank(exam_name, language_code):
    supabase = get_supabase_client()
    q_response = (
        supabase.table("questions")
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

    question_ids = [q["id"] for q in questions]
    options_by_question = defaultdict(list)
    for start in range(0, len(question_ids), 100):
        chunk = question_ids[start:start + 100]
        opt_response = (
            supabase.table("answer_options")
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
        if not is_answer_key_valid(normalized[-1]):
            normalized.pop()
            continue
    return normalized


def reset_practice():
    from utils.practice_session_persistence import clear_category_practice_state

    clear_category_practice_state()
    keys = [
        "practice_started", "practice_submitted", "practice_current_index", "practice_questions",
        "practice_answers", "practice_feedback_shown", "practice_saved", "practice_category",
        "practice_exam_attempt_id",
        "practice_count", "practice_exam_name", "practice_language_code", "practice_mode_label",
        "practice_option_orders", "practice_started_at",
        "practice_question_time_spent", "practice_question_entered_at", "practice_timing_index",
        "_category_practice_restored_once",
        DAILY_SPRINT_AUTO_START_GUARD,
    ]
    for key in keys:
        st.session_state.pop(key, None)
    st.rerun()


def is_correct(user_ids, correct_ids, question=None):
    if question is not None:
        return is_answer_correct(user_ids, question)
    return set(user_ids or []) == set(correct_ids or [])


def _clamped_seconds(value, max_seconds=7200):
    try:
        seconds = float(value or 0)
    except Exception:
        return 0.0
    if seconds < 0:
        return 0.0
    return round(min(seconds, max_seconds), 3)


def reset_practice_timing():
    st.session_state.practice_question_time_spent = {}
    st.session_state.practice_question_entered_at = time.time()
    st.session_state.practice_timing_index = int(st.session_state.get("practice_current_index") or 0)


def record_current_practice_time():
    questions = st.session_state.get("practice_questions") or []
    if not questions:
        return

    try:
        idx = int(st.session_state.get("practice_timing_index", st.session_state.get("practice_current_index", 0)) or 0)
    except Exception:
        idx = 0

    now = time.time()
    entered_at = st.session_state.get("practice_question_entered_at")
    if entered_at is not None and 0 <= idx < len(questions):
        elapsed = _clamped_seconds(now - float(entered_at))
        existing = float((st.session_state.get("practice_question_time_spent") or {}).get(idx, 0) or 0)
        st.session_state.practice_question_time_spent[idx] = round(existing + elapsed, 3)

    st.session_state.practice_question_entered_at = now
    st.session_state.practice_timing_index = int(st.session_state.get("practice_current_index") or 0)


def move_to_practice_question(new_index):
    record_current_practice_time()
    st.session_state.practice_current_index = int(new_index)
    st.session_state.practice_question_entered_at = time.time()
    st.session_state.practice_timing_index = int(new_index)
    st.session_state.practice_feedback_shown = False


def option_texts_by_id(question, ids):
    ids = {str(v) for v in (ids or [])}
    return [opt.get("text", "") for opt in question.get("options", []) if str(opt.get("id")) in ids]


def build_question_attempt_rows(exam_attempt_id, user_email, questions, answers):
    from utils.readiness_persistence import build_attempt_metadata  # noqa: PLC0415
    question_times = st.session_state.get("practice_question_time_spent") or {}
    rows = []
    for idx, q in enumerate(questions or []):
        selected_ids = [str(v) for v in (answers.get(idx, []) if answers else [])]
        correct_ids = [str(v) for v in q.get("correct_ids", [])]
        row = {
            "exam_attempt_id": exam_attempt_id,
            "question_id": int(q.get("id")),
            "user_email": user_email,
            "exam_name": q.get("exam_name") or st.session_state.get("practice_exam_name"),
            "language_code": q.get("language_code") or st.session_state.get("practice_language_code") or "en",
            "category": q.get("category") or "Uncategorized",
            "difficulty": str(q.get("difficulty") or "medium").strip().lower(),
            "selected_options": option_texts_by_id(q, selected_ids),
            "correct_options": option_texts_by_id(q, correct_ids),
            "is_correct": is_correct(selected_ids, correct_ids, question=q),
            "time_spent_seconds": _clamped_seconds(question_times.get(idx, 0)),
            "answered_at": datetime.now(timezone.utc).isoformat(),
        }
        row.update(build_attempt_metadata(q))
        rows.append(row)
    return rows


def build_breakdown(questions, answers, field):
    stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for i, q in enumerate(questions):
        value = q.get(field, "Unknown") or "Unknown"
        stats[value]["total"] += 1
        if is_correct(answers.get(i, []), q.get("correct_ids", []), question=q):
            stats[value]["correct"] += 1
    return dict(stats)


def save_practice_attempt(score, correct, total, category, domain_breakdown, difficulty_breakdown, exam_name, language_code):
    """Persist one practice attempt (parent row + child question rows).

    Retry-safe: reuses the exam_attempt_id already stored in session state
    (set immediately after a successful parent insert, before any child
    persistence) instead of inserting a new parent on every call, and upserts
    child rows on the (exam_attempt_id, question_id) unique key. Mirrors the
    paid-mock exam pattern in app.py / utils.question_selection. Raises on
    failure so the existing caller's try/except + warning behavior is
    unchanged.
    """
    from utils.question_selection import persist_question_attempts, resolve_or_create_exam_attempt_id

    user_email = get_current_user_email()
    if not user_email:
        raise ValueError("No account email saved. Open Account first.")
    supabase = get_supabase_client()

    payload = {
        "user_email": user_email,
        "mode": st.session_state.get("practice_mode_label", PRACTICE_BY_CATEGORY),
        "category": category,
        "score": float(score),
        "correct_answers": int(correct),
        "total_questions": int(total),
        "domain_breakdown": domain_breakdown,
        "difficulty_breakdown": difficulty_breakdown,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "exam_name": exam_name,
        "language_code": language_code,
    }
    exam_attempt_id = resolve_or_create_exam_attempt_id(
        supabase,
        payload,
        existing_attempt_id=st.session_state.get("practice_exam_attempt_id"),
        expected_user_email=user_email,
        expected_mode=payload["mode"],
        expected_exam_name=exam_name,
        expected_language_code=language_code,
    )
    if exam_attempt_id is None:
        raise RuntimeError("Practice attempt could not be saved: no attempt id returned.")

    # Store immediately, before child persistence, so a retry after a
    # child-write failure reuses this exact parent instead of inserting
    # another one.
    st.session_state.practice_exam_attempt_id = exam_attempt_id

    question_rows = build_question_attempt_rows(
        exam_attempt_id=exam_attempt_id,
        user_email=user_email,
        questions=st.session_state.get("practice_questions", []),
        answers=st.session_state.get("practice_answers", {}),
    )
    ok, error = persist_question_attempts(
        supabase,
        question_rows,
        exam_attempt_id=exam_attempt_id,
        expected_count=int(total),
    )
    if not ok:
        raise RuntimeError(error or "Could not save detailed practice results.")


def render_locked_practice_preview(user_email, language_code):
    """Show a premium preview for free users without exposing real paid practice questions."""
    st.markdown(
        """
        <div class="practice-card locked-preview-card">
            <div class="locked-eyebrow">Premium practice preview</div>
            <h2 style="margin:0 0 8px 0;">Target weak domains before exam day.</h2>
            <p class="small-muted" style="font-size:15px;line-height:1.5;margin-bottom:0;">
                Practice by Category unlocks focused question sets by certification domain, instant explanations,
                and saved progress tracking. This preview uses sample data only.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.info(f"Signed in as {user_email} | Preferred language: {language_label(language_code)}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Sample Domains", "6")
    c2.metric("Sample Practice Set", "20 questions")
    c3.metric("Mode", "Untimed")

    left, right = st.columns([1.2, 1])
    with left:
        st.markdown(
            """
            <div class="practice-card">
                <h3 style="margin-top:0;">What premium users can do</h3>
                <ul style="line-height:1.8;margin-bottom:0;">
                    <li>Choose a certification and drill one domain at a time.</li>
                    <li>Pick focused sets of 10, 20, or 30 approved questions.</li>
                    <li>See explanations immediately after each answer.</li>
                    <li>Save every practice session into My Progress.</li>
                    <li>Feed weak-area data into readiness tracking.</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            """
            <div class="practice-card sample-panel">
                <h3 style="margin-top:0;">Sample domain drill</h3>
                <div class="sample-row"><span>Configuration and Setup</span><strong>20 questions</strong></div>
                <div class="sample-row"><span>Object Manager</span><strong>10 questions</strong></div>
                <div class="sample-row"><span>Security and Access</span><strong>30 questions</strong></div>
                <div class="locked-pill">Locked preview</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.warning("Practice by Category is locked on free accounts. Use the free mock exam now, or unlock premium access to practice by domain.")


st.markdown(
    """
    <style>
    .block-container { max-width: 1120px; padding-top: 2rem !important; }
    .practice-banner { background:#16325c;color:white;padding:18px 22px;border-radius:8px;font-size:27px;font-weight:700;margin-bottom:18px; }
    .practice-card { border:1px solid #d8dde6;border-radius:8px;padding:20px;background:white;margin-bottom:18px; }
    .locked-preview-card { border:1px solid #c9d7f5;background:linear-gradient(135deg,#ffffff 0%,#f4f8ff 100%); }
    .locked-eyebrow { color:#1b4d89;font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px; }
    .sample-panel { background:#f8fafc; }
    .sample-row { display:flex;justify-content:space-between;border-bottom:1px solid #e5e7eb;padding:10px 0;font-size:14px;gap:12px; }
    .locked-pill { display:inline-block;margin-top:14px;padding:6px 10px;border-radius:999px;background:#e8f0fe;color:#1b4d89;font-size:12px;font-weight:700; }
    .small-muted { color:#5f6368;font-size:13px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="practice-banner">Practice by Category</div>', unsafe_allow_html=True)
st.caption(f"App Version: {APP_VERSION}")

user_email = require_login()

profile = fetch_user_profile(user_email)
language_code = str(profile.get("preferred_language_code") or "en").strip().lower()
is_daily_sprint, daily_sprint_exam_name, daily_sprint_category, daily_sprint_count = get_daily_sprint_params()

if not has_premium_access(user_email):
    render_locked_practice_preview(user_email, language_code)
    st.stop()

st.success(f"Account: {user_email} ✅ | Preferred language: {language_label(language_code)}")

certifications = fetch_user_certifications(user_email)
if not certifications:
    certifications = fetch_active_certifications()

if not certifications:
    st.error("No active certifications are configured.")
    st.info("Admin setup required: add active rows in the certifications table.")
    st.stop()

def maybe_restore_category_practice(user_email, language_code):
    if st.session_state.get("practice_started") or st.session_state.get("_category_practice_restored_once"):
        return False

    pending = st.session_state.get("_pending_category_practice_state")
    if pending is None:
        pending = decode_pending_category_practice_state()
    if not pending:
        return False

    exam_name = pending.get("exam_name")
    restore_language = pending.get("language_code") or language_code
    if not exam_name:
        clear_category_practice_state()
        return False

    question_bank = fetch_question_bank(exam_name, restore_language)
    if restore_category_practice_session(pending, question_bank, user_email, st.session_state):
        st.session_state.pop("_pending_category_practice_state", None)
        return True

    clear_category_practice_state()
    st.session_state.pop("_pending_category_practice_state", None)
    return False


pending_category_practice_state = decode_pending_category_practice_state()
if pending_category_practice_state and "_pending_category_practice_state" not in st.session_state:
    st.session_state["_pending_category_practice_state"] = pending_category_practice_state

exam_names = [c["exam_name"] for c in certifications if c.get("exam_name")]
display_by_exam = {c["exam_name"]: c.get("display_name") or c["exam_name"] for c in certifications if c.get("exam_name")}

if maybe_restore_category_practice(user_email, language_code):
    st.rerun()

if not st.session_state.get("practice_started", False):
    default_exam_index = 0
    if is_daily_sprint and daily_sprint_exam_name in exam_names:
        default_exam_index = exam_names.index(daily_sprint_exam_name)

    if is_daily_sprint and daily_sprint_exam_name in exam_names:
        sprint_domains = fetch_domains(daily_sprint_exam_name)
        sprint_question_bank = fetch_question_bank(daily_sprint_exam_name, language_code)
        if sprint_question_bank and maybe_auto_start_daily_sprint(
            is_daily_sprint=is_daily_sprint,
            daily_sprint_exam_name=daily_sprint_exam_name,
            daily_sprint_category=daily_sprint_category,
            premium=True,
            exam_names=exam_names,
            question_bank=sprint_question_bank,
            domains=sprint_domains,
            language_code=language_code,
            session_state=st.session_state,
            rerun_fn=st.rerun,
        ):
            st.stop()

    selected_exam = st.selectbox(
        "Choose certification",
        exam_names,
        index=default_exam_index,
        format_func=lambda x: display_by_exam.get(x, x),
        key="practice_selected_exam_name",
    )
    domains = fetch_domains(selected_exam)
    question_bank = fetch_question_bank(selected_exam, language_code)

    st.header("Choose Practice Settings")
    if is_daily_sprint and daily_sprint_category:
        st.success(f"Daily Sprint loaded: 10 questions focused on {daily_sprint_category}.")
    else:
        st.info("Practice one domain at a time. Explanations are shown during practice and again in the final review.")

    if not question_bank:
        st.error(f"No approved questions found for {display_by_exam.get(selected_exam, selected_exam)} in {language_label(language_code)}.")
        st.stop()

    available_categories = build_available_categories(domains, question_bank)

    default_category_index = 0
    if is_daily_sprint and daily_sprint_category in available_categories:
        default_category_index = available_categories.index(daily_sprint_category)

    selected_category = st.selectbox("Select category", available_categories, index=default_category_index)
    available_count = sum(1 for q in question_bank if q["category"] == selected_category)
    valid_counts = [n for n in QUESTION_COUNT_OPTIONS if n <= available_count] or [available_count]

    default_count_index = 0
    desired_count = 10 if is_daily_sprint else None
    if desired_count in valid_counts:
        default_count_index = valid_counts.index(desired_count)

    selected_count = st.selectbox("Number of questions", valid_counts, index=default_count_index)

    c1, c2, c3 = st.columns(3)
    c1.metric("Available Questions", available_count)
    c2.metric("Practice Questions", selected_count)
    c3.metric("Mode", "Untimed")

    start_label = "Start Daily Sprint" if is_daily_sprint else "Start Practice"
    if st.button(start_label, type="primary"):
        clear_category_practice_state()
        selected = select_practice_questions(question_bank, selected_category, selected_count)
        initialize_practice_session(
            selected,
            selected_category,
            selected_count,
            selected_exam,
            language_code,
            DAILY_SPRINT if is_daily_sprint else PRACTICE_BY_CATEGORY,
            st.session_state,
        )
        reset_practice_timing()
        st.rerun()

elif not st.session_state.get("practice_submitted", False):
    questions = st.session_state.practice_questions
    index = st.session_state.get("practice_current_index", 0)
    q = questions[index]
    st.markdown(f"""
    <div class="practice-card">
        <strong>Question {index + 1} of {len(questions)}</strong><br>
        <span class="small-muted">Certification: {display_by_exam.get(st.session_state.practice_exam_name, st.session_state.practice_exam_name)} | Domain: {q['category']} | Difficulty: {q['difficulty'].title()}</span>
    </div>
    """, unsafe_allow_html=True)
    st.progress((index + 1) / len(questions))
    st.subheader(q["question"])

    previous_answer = st.session_state.get("practice_answers", {}).get(index, [])
    if is_multiple_select(q):
        selected_ids = apply_multi_select_answer_ui(
            q,
            previous_selection=previous_answer,
            key_prefix=f"practice_{index}",
            session_state=st.session_state,
            checkbox_fn=st.checkbox,
            warning_fn=st.warning,
            limit_message_fn=lambda count: st.info(
                f"You can only select {count} answers. Deselect an option to choose a different one."
            ),
        )
        if selected_ids:
            st.session_state.practice_answers[index] = selected_ids
        elif index in st.session_state.get("practice_answers", {}):
            del st.session_state.practice_answers[index]
    else:
        option_texts = [opt["text"] for opt in q["options"]]
        id_by_text = {opt["text"]: opt["id"] for opt in q["options"]}
        previous_text = next((opt["text"] for opt in q["options"] if previous_answer and opt["id"] == previous_answer[0]), None)
        selected_text = st.radio("Choose one answer.", option_texts, index=option_texts.index(previous_text) if previous_text in option_texts else None, key=f"practice_radio_{index}")
        if selected_text:
            st.session_state.practice_answers[index] = [id_by_text[selected_text]]

    user_answer = st.session_state.practice_answers.get(index, [])
    answer_complete = is_answer_selection_complete(user_answer, q)
    if st.session_state.get("practice_feedback_shown") and not answer_complete:
        st.session_state.practice_feedback_shown = False

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Submit Answer", type="primary", disabled=not answer_complete):
            st.session_state.practice_feedback_shown = True
            st.rerun()
    with col2:
        if st.button("Previous") and index > 0:
            move_to_practice_question(index - 1)
            st.rerun()
    with col3:
        if index < len(questions) - 1:
            if st.button("Next"):
                move_to_practice_question(index + 1)
                st.rerun()
        else:
            if st.button("Finish Practice"):
                record_current_practice_time()
                st.session_state.practice_submitted = True
                clear_category_practice_state()
                st.rerun()

    if not answer_complete:
        st.caption(EXPLANATION_GATE_HINT)

    if effective_explanation_feedback_shown(
        st.session_state.get("practice_feedback_shown", False),
        user_answer,
        q,
    ):
        correct_now = is_correct(user_answer, q["correct_ids"], question=q)
        if correct_now:
            st.success("Correct ✅")
        else:
            st.error("Incorrect")
        correct_texts = [opt["text"] for opt in q["options"] if opt["id"] in q["correct_ids"]]
        selected_texts = [opt["text"] for opt in q["options"] if opt["id"] in user_answer]
        st.write("Your answer: " + (", ".join(selected_texts) if selected_texts else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_texts))
        st.info(q["explanation"])

    persist_category_practice_state(st.session_state, user_email)

    st.divider()
    if st.button("Start New Practice"):
        reset_practice()

else:
    questions = st.session_state.practice_questions
    answers = st.session_state.practice_answers
    correct = sum(1 for i, q in enumerate(questions) if is_correct(answers.get(i, []), q["correct_ids"], question=q))
    total = len(questions)
    score = round((correct / total) * 100, 2) if total else 0
    domain_breakdown = build_breakdown(questions, answers, "category")
    difficulty_breakdown = build_breakdown(questions, answers, "difficulty")

    if not st.session_state.get("practice_saved", False):
        record_current_practice_time()
        try:
            save_practice_attempt(score, correct, total, st.session_state.practice_category, domain_breakdown, difficulty_breakdown, st.session_state.practice_exam_name, st.session_state.practice_language_code)
            st.session_state.practice_saved = True
            clear_category_practice_state()
            st.success("Practice attempt saved to progress tracking ✅")
        except Exception as exc:
            log_and_get_user_message(
                "category practice save failed",
                PRACTICE_SAVE_ERROR_MESSAGE,
                exc=exc,
            )
            st.warning(PRACTICE_SAVE_ERROR_MESSAGE)

    completion_view = build_practice_completion_view(score, correct, total, st.session_state)
    st.header(completion_view["heading"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Score", completion_view["score_metric"])
    c2.metric("Correct", completion_view["correct_metric"])
    c3.metric("Category", st.session_state.practice_category)

    if completion_view["show_dashboard_return"]:
        render_session_page_link(
            completion_view["dashboard_path"],
            label=completion_view["dashboard_label"],
            icon="🏠",
        )

    st.subheader(completion_view["review_heading"])
    for i, q in enumerate(questions):
        user_answer = answers.get(i, [])
        result_correct = is_correct(user_answer, q["correct_ids"], question=q)
        if result_correct:
            st.success(f"Question {i + 1} — Correct")
        else:
            st.error(f"Question {i + 1} — Incorrect")
        selected_texts = [opt["text"] for opt in q["options"] if opt["id"] in user_answer]
        correct_texts = [opt["text"] for opt in q["options"] if opt["id"] in q["correct_ids"]]
        st.caption(f"Domain: {q['category']} | Difficulty: {q['difficulty'].title()}")
        st.write(q["question"])
        st.write("Your answer: " + (", ".join(selected_texts) if selected_texts else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_texts))
        st.info(q["explanation"])
        st.divider()

    if completion_view["show_primary_start_new_practice"]:
        if st.button("Start New Practice", type="primary"):
            reset_practice()
