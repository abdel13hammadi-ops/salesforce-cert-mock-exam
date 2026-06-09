import json
import random
from collections import defaultdict
from datetime import datetime, timezone

import streamlit as st
from supabase import create_client

APP_VERSION = "WEAK_AREAS_PRACTICE_V1"

st.set_page_config(
    page_title="Weak Areas Practice",
    layout="wide",
    initial_sidebar_state="expanded",
)

CATEGORY_ORDER = [
    "Configuration and Setup",
    "Object Manager and Lightning App Builder",
    "Data and Analytics Management",
    "Automation",
    "Sales and Marketing Applications",
    "Service and Support Applications",
    "Agentforce AI",
    "Productivity and Collaboration",
]

QUESTION_COUNT_OPTIONS = [10, 20, 30]


@st.cache_resource
def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        st.error("Supabase secrets are missing. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()

    return create_client(url, key)


@st.cache_data(ttl=60)
def fetch_attempts():
    supabase = get_supabase_client()
    result = (
        supabase.table("exam_attempts")
        .select("id,mode,category,score,correct_answers,total_questions,domain_breakdown,difficulty_breakdown,completed_at")
        .order("id", desc=True)
        .execute()
    )
    return result.data or []


@st.cache_data(ttl=60)
def fetch_question_bank():
    supabase = get_supabase_client()

    q_response = (
        supabase.table("questions")
        .select("id, category, difficulty, question_text, question_type, select_count, explanation, is_active, is_exam_eligible, quality_status")
        .eq("is_active", True)
        .eq("is_exam_eligible", True)
        .eq("quality_status", "approved")
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
            "category": q.get("category") or "Uncategorized",
            "difficulty": (q.get("difficulty") or "unknown").lower(),
            "question": q.get("question_text") or "",
            "type": q.get("question_type") or "single",
            "select_count": q.get("select_count"),
            "explanation": q.get("explanation") or "No explanation available.",
            "options": [
                {
                    "id": str(o["id"]),
                    "text": o.get("option_text") or "",
                    "is_correct": bool(o.get("is_correct")),
                }
                for o in opts
            ],
            "correct_ids": correct_ids,
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


def aggregate_breakdown(attempts, field_name):
    totals = defaultdict(lambda: {"correct": 0, "total": 0})

    for attempt in attempts:
        breakdown = normalize_breakdown(attempt.get(field_name))
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
        total = data["total"]
        correct = data["correct"]
        accuracy = round((correct / total) * 100, 2) if total else 0
        rows.append({
            "name": name,
            "correct": correct,
            "total": total,
            "accuracy": accuracy,
        })

    return sorted(rows, key=lambda r: (r["accuracy"], -r["total"], r["name"]))


def is_correct(user_ids, correct_ids):
    return set(user_ids or []) == set(correct_ids or [])


def build_breakdown(questions, answers, field):
    stats = defaultdict(lambda: {"correct": 0, "total": 0})

    for i, q in enumerate(questions):
        value = q.get(field, "Unknown") or "Unknown"
        stats[value]["total"] += 1
        if is_correct(answers.get(i, []), q.get("correct_ids", [])):
            stats[value]["correct"] += 1

    return dict(stats)


def save_weak_attempt(score, correct, total, category_label, domain_breakdown, difficulty_breakdown):
    supabase = get_supabase_client()
    payload = {
        "user_email": "guest",
        "mode": "Weak Areas Practice",
        "category": category_label,
        "score": float(score),
        "correct_answers": int(correct),
        "total_questions": int(total),
        "domain_breakdown": domain_breakdown,
        "difficulty_breakdown": difficulty_breakdown,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("exam_attempts").insert(payload).execute()


def reset_weak_practice():
    for key in [
        "weak_started",
        "weak_submitted",
        "weak_current_index",
        "weak_questions",
        "weak_answers",
        "weak_feedback_shown",
        "weak_saved",
        "weak_categories",
        "weak_difficulty",
        "weak_count",
    ]:
        if key in st.session_state:
            del st.session_state[key]
    st.rerun()


def choose_questions(question_bank, selected_categories, target_difficulty, count):
    selected = []

    # First preference: selected weak categories + weakest difficulty.
    primary = [
        q for q in question_bank
        if q["category"] in selected_categories and q["difficulty"] == target_difficulty
    ]
    random.shuffle(primary)
    selected.extend(primary[:count])

    # Second preference: selected weak categories, any difficulty.
    if len(selected) < count:
        used_ids = {q["id"] for q in selected}
        category_pool = [
            q for q in question_bank
            if q["category"] in selected_categories and q["id"] not in used_ids
        ]
        random.shuffle(category_pool)
        selected.extend(category_pool[:count - len(selected)])

    # Fallback: weakest difficulty anywhere.
    if len(selected) < count:
        used_ids = {q["id"] for q in selected}
        diff_pool = [
            q for q in question_bank
            if q["difficulty"] == target_difficulty and q["id"] not in used_ids
        ]
        random.shuffle(diff_pool)
        selected.extend(diff_pool[:count - len(selected)])

    # Final fallback: any approved question.
    if len(selected) < count:
        used_ids = {q["id"] for q in selected}
        fallback = [q for q in question_bank if q["id"] not in used_ids]
        random.shuffle(fallback)
        selected.extend(fallback[:count - len(selected)])

    random.shuffle(selected)
    return selected[:count]


st.markdown(
    """
    <style>
    .block-container { max-width: 1120px; padding-top: 2rem !important; }
    .weak-banner {
        background: #16325c;
        color: white;
        padding: 18px 22px;
        border-radius: 8px;
        font-size: 27px;
        font-weight: 700;
        margin-bottom: 18px;
    }
    .weak-card {
        border: 1px solid #d8dde6;
        border-radius: 8px;
        padding: 20px;
        background: white;
        margin-bottom: 18px;
    }
    .small-muted { color: #5f6368; font-size: 13px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="weak-banner">Weak Areas Practice</div>', unsafe_allow_html=True)
st.caption(f"App version: {APP_VERSION}")

question_bank = fetch_question_bank()
attempts = fetch_attempts()

if not question_bank:
    st.error("No approved exam-eligible questions found in Supabase.")
    st.stop()

available_categories = [c for c in CATEGORY_ORDER if any(q["category"] == c for q in question_bank)]
extra_categories = sorted({q["category"] for q in question_bank if q["category"] not in CATEGORY_ORDER})
available_categories.extend(extra_categories)

available_difficulties = [d for d in ["easy", "medium", "hard"] if any(q["difficulty"] == d for q in question_bank)]
if not available_difficulties:
    available_difficulties = ["medium"]

for key, default in {
    "weak_started": False,
    "weak_submitted": False,
    "weak_current_index": 0,
    "weak_answers": {},
    "weak_feedback_shown": False,
    "weak_saved": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

weak_domains = aggregate_breakdown(attempts, "domain_breakdown")
weak_difficulties = aggregate_breakdown(attempts, "difficulty_breakdown")

if not st.session_state.weak_started:
    st.header("Build Practice from Your Weak Areas")

    if not attempts or not weak_domains:
        st.warning(
            "No detailed weak-area data found yet. Complete at least one timed mock exam or category practice first. "
            "For now, you can manually choose a category."
        )
        recommended_categories = available_categories[:1]
        recommended_difficulty = "medium" if "medium" in available_difficulties else available_difficulties[0]
    else:
        recommended_categories = [r["name"] for r in weak_domains[:2] if r["name"] in available_categories]
        if not recommended_categories:
            recommended_categories = available_categories[:1]
        recommended_difficulty = weak_difficulties[0]["name"] if weak_difficulties else "medium"
        if recommended_difficulty not in available_difficulties:
            recommended_difficulty = "medium" if "medium" in available_difficulties else available_difficulties[0]

        st.success("Your weakest areas were detected from saved attempts.")
        left, right = st.columns(2)
        with left:
            st.subheader("Weakest Domains")
            for row in weak_domains[:3]:
                st.write(f"- **{row['name']}** — {row['accuracy']}% ({row['correct']} / {row['total']})")
        with right:
            st.subheader("Weakest Difficulty")
            for row in weak_difficulties[:3]:
                st.write(f"- **{row['name'].title()}** — {row['accuracy']}% ({row['correct']} / {row['total']})")

    st.divider()

    default_indices = [available_categories.index(c) for c in recommended_categories if c in available_categories]
    selected_categories = st.multiselect(
        "Practice these weak domain(s):",
        available_categories,
        default=[available_categories[i] for i in default_indices] if default_indices else available_categories[:1],
    )

    difficulty_choice = st.selectbox(
        "Focus difficulty:",
        available_difficulties,
        index=available_difficulties.index(recommended_difficulty) if recommended_difficulty in available_difficulties else 0,
        format_func=lambda x: x.title(),
    )

    question_count = st.selectbox("Number of questions:", QUESTION_COUNT_OPTIONS, index=0)

    st.info(
        "The app first pulls questions from your selected weak domain(s) and weakest difficulty. "
        "If there are not enough, it fills the practice set with more questions from those domains."
    )

    if st.button("Start Weak Areas Practice", type="primary"):
        if not selected_categories:
            st.error("Choose at least one category.")
            st.stop()

        selected_questions = choose_questions(question_bank, selected_categories, difficulty_choice, int(question_count))
        if not selected_questions:
            st.error("No questions found for these settings.")
            st.stop()

        # Randomize answer choices for each question.
        for q in selected_questions:
            random.shuffle(q["options"])

        st.session_state.weak_questions = selected_questions
        st.session_state.weak_categories = selected_categories
        st.session_state.weak_difficulty = difficulty_choice
        st.session_state.weak_count = len(selected_questions)
        st.session_state.weak_started = True
        st.session_state.weak_submitted = False
        st.session_state.weak_current_index = 0
        st.session_state.weak_answers = {}
        st.session_state.weak_feedback_shown = False
        st.session_state.weak_saved = False
        st.rerun()

elif not st.session_state.weak_submitted:
    questions = st.session_state.weak_questions
    q_index = st.session_state.weak_current_index
    q = questions[q_index]

    st.markdown(
        f"""
        <div class="weak-card">
            <strong>Question:</strong> {q_index + 1} of {len(questions)}
            &nbsp;&nbsp; | &nbsp;&nbsp;
            <strong>Domain:</strong> {q['category']}
            &nbsp;&nbsp; | &nbsp;&nbsp;
            <strong>Difficulty:</strong> {q['difficulty'].title()}
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.progress((q_index + 1) / len(questions))
    st.subheader(q["question"])

    current_answer = st.session_state.weak_answers.get(q_index, [])
    selected_ids = []

    if q["type"] == "multiple":
        count_text = q.get("select_count") or len(q["correct_ids"])
        st.warning(f"Choose {count_text} answers.")
        for opt in q["options"]:
            checked = opt["id"] in current_answer
            if st.checkbox(opt["text"], value=checked, key=f"weak_{q_index}_{opt['id']}"):
                selected_ids.append(opt["id"])
    else:
        option_labels = [opt["text"] for opt in q["options"]]
        id_by_text = {opt["text"]: opt["id"] for opt in q["options"]}
        current_text = None
        if current_answer:
            for opt in q["options"]:
                if opt["id"] == current_answer[0]:
                    current_text = opt["text"]
                    break
        selected_text = st.radio(
            "Choose one answer.",
            option_labels,
            index=option_labels.index(current_text) if current_text in option_labels else None,
            key=f"weak_radio_{q_index}",
        )
        if selected_text:
            selected_ids = [id_by_text[selected_text]]

    if selected_ids:
        st.session_state.weak_answers[q_index] = selected_ids
    elif q_index in st.session_state.weak_answers:
        del st.session_state.weak_answers[q_index]

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Previous") and q_index > 0:
            st.session_state.weak_current_index -= 1
            st.session_state.weak_feedback_shown = False
            st.rerun()
    with col2:
        if st.button("Show Explanation"):
            st.session_state.weak_feedback_shown = True
    with col3:
        if q_index < len(questions) - 1:
            if st.button("Next", type="primary"):
                st.session_state.weak_current_index += 1
                st.session_state.weak_feedback_shown = False
                st.rerun()
        else:
            if st.button("Submit Practice", type="primary"):
                st.session_state.weak_submitted = True
                st.rerun()

    if st.session_state.weak_feedback_shown:
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
    c3.metric("Focus", st.session_state.get("weak_difficulty", "mixed").title())

    if not st.session_state.weak_saved:
        try:
            save_weak_attempt(score, correct, total, category_label, domain_breakdown, difficulty_breakdown)
            st.session_state.weak_saved = True
            st.success("Weak areas practice attempt saved to progress tracking ✅")
        except Exception as exc:
            st.error(f"Practice result was calculated, but saving to Supabase failed: {exc}")
    else:
        st.success("Weak areas practice attempt saved to progress tracking ✅")

    st.divider()
    st.subheader("Breakdown by Domain")
    for name, data in domain_breakdown.items():
        pct = round((data["correct"] / data["total"]) * 100, 2) if data["total"] else 0
        st.write(f"**{name}:** {data['correct']} / {data['total']} correct ({pct}%)")

    st.subheader("Breakdown by Difficulty")
    for name, data in difficulty_breakdown.items():
        pct = round((data["correct"] / data["total"]) * 100, 2) if data["total"] else 0
        st.write(f"**{name.title()}:** {data['correct']} / {data['total']} correct ({pct}%)")

    st.divider()
    st.header("Answer Review")
    for i, q in enumerate(questions):
        user_ids = answers.get(i, [])
        result_correct = is_correct(user_ids, q["correct_ids"])
        if result_correct:
            st.success(f"Question {i + 1} — Correct")
        else:
            st.error(f"Question {i + 1} — Incorrect")

        st.caption(f"Domain: {q['category']} | Difficulty: {q['difficulty'].title()}")
        st.write(q["question"])
        selected_texts = [opt["text"] for opt in q["options"] if opt["id"] in user_ids]
        correct_texts = [opt["text"] for opt in q["options"] if opt["id"] in q["correct_ids"]]
        st.write("Your answer: " + (", ".join(selected_texts) if selected_texts else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_texts))
        st.info(q["explanation"])
        st.divider()

    if st.button("Start New Weak Areas Practice"):
        reset_weak_practice()
