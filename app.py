import json
import time
import random
from collections import defaultdict

import streamlit as st
from streamlit_autorefresh import st_autorefresh


CONFIG_FILE = "exam_config.json"


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


config = load_config()

st.set_page_config(
    page_title=config["exam_title"],
    layout="wide",
    initial_sidebar_state="expanded"
)

PASSING_SCORE = config["passing_score"]
EXAM_MINUTES = config["time_limit_minutes"]
QUESTION_FILE = config["question_file"]


def load_questions():
    with open(QUESTION_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


all_questions = load_questions()

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
    "question_order": [],
    "choice_orders": {}
}

for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


def get_questions():
    if not st.session_state.question_order:
        st.session_state.question_order = list(range(len(all_questions)))
        if st.session_state.randomize_questions:
            random.shuffle(st.session_state.question_order)
    return [all_questions[i] for i in st.session_state.question_order]


questions = get_questions()


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


def reset_exam():
    for key in list(defaults.keys()):
        if key in st.session_state:
            del st.session_state[key]
    st.rerun()


st.markdown(
    """
    <style>
    .block-container {
        max-width: 1120px;
        padding-top: 2rem !important;
        padding-bottom: 2rem !important;
    }

    header[data-testid="stHeader"] {
        height: 0px;
    }

    .exam-banner {
        background: #16325c;
        color: white;
        padding: 18px 22px;
        border-radius: 8px 8px 0 0;
        font-size: 27px;
        font-weight: 700;
        line-height: 1.25;
        margin-top: 10px;
    }

    .exam-sub-banner {
        background: #f4f6f9;
        border: 1px solid #d8dde6;
        border-top: none;
        padding: 12px 20px;
        border-radius: 0 0 8px 8px;
        margin-bottom: 30px;
        color: #16325c;
        font-size: 15px;
    }

    .exam-card {
        border: 1px solid #d8dde6;
        border-radius: 8px;
        padding: 18px 20px;
        background: #ffffff;
        margin-bottom: 18px;
    }

    .question-card {
        border: 1px solid #d8dde6;
        border-radius: 8px;
        padding: 22px;
        background: #ffffff;
        margin-top: 12px;
        margin-bottom: 18px;
    }

    .status-strip {
        background: #f8f9fb;
        border: 1px solid #d8dde6;
        border-radius: 8px;
        padding: 12px 16px;
        margin-bottom: 15px;
    }

    section[data-testid="stSidebar"] > div:first-child {
        padding-top: 0.5rem;
    }

    .timer-fixed {
        position: fixed;
        top: 0.65rem;
        left: 0.75rem;
        width: 18.25rem;
        z-index: 999999;
        background: #ffffff;
        padding: 8px 10px 12px 10px;
        border-bottom: 1px solid #d8dde6;
        box-shadow: 0 2px 4px rgba(0,0,0,0.06);
    }

    .timer-label {
        font-weight: 700;
        font-size: 15px;
        margin-bottom: 7px;
        color: #1f2937;
    }

    .timer-box {
        background: #fff4d6;
        border: 1px solid #e0b84f;
        border-radius: 8px;
        padding: 10px;
        text-align: center;
        font-size: 26px;
        font-weight: 800;
        color: #1f2937;
        letter-spacing: 1px;
    }

    .navigator-spacer {
        height: 118px;
    }

    .question-nav-title {
        font-weight: 700;
        font-size: 16px;
        margin-top: 6px;
        margin-bottom: 8px;
        color: #1f2937;
    }

    .small-help {
        color: #5f6368;
        font-size: 13px;
        margin-bottom: 10px;
    }

    section[data-testid="stSidebar"] div.stButton > button {
        width: 100%;
        padding: 0.28rem 0.2rem;
        font-size: 12px;
        border-radius: 6px;
        min-height: 2.1rem;
    }

    div.stButton > button {
        border-radius: 6px;
        font-weight: 600;
    }

    section[data-testid="stSidebar"] div[data-testid="column"] {
        padding-left: 0.12rem;
        padding-right: 0.12rem;
    }

    @media (max-width: 900px) {
        .timer-fixed {
            position: sticky;
            top: 0;
            width: auto;
            left: auto;
        }

        .navigator-spacer {
            height: 0px;
        }
    }
    </style>
    """,
    unsafe_allow_html=True
)


st.markdown(
    f"""
    <div class="exam-banner">
        {config["exam_title"]}
    </div>

    <div class="exam-sub-banner">
        {config["certification"]} |
        {len(all_questions)} questions |
        {EXAM_MINUTES} minutes |
        Passing score: {PASSING_SCORE}%
    </div>
    """,
    unsafe_allow_html=True
)


if not st.session_state.started:
    st.header("Exam Instructions")

    st.markdown(
        """
        <div class="exam-card">
            <p>This simulator uses the current Platform Administrator-style structure, including Agentforce AI.</p>
            <p>Answers and explanations are hidden until after final submission.</p>
        </div>
        """,
        unsafe_allow_html=True
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Questions", len(all_questions))
    c2.metric("Time Limit", f"{EXAM_MINUTES} min")
    c3.metric("Passing Score", f"{PASSING_SCORE}%")

    st.subheader("Exam Domain Breakdown")

    for section in config["sections"]:
        domain = section.get("name", section) if isinstance(section, dict) else section
        weight = section.get("weight", None) if isinstance(section, dict) else None
        count = section.get("questions", None) if isinstance(section, dict) else None

        if weight is not None and count is not None:
            st.write(f"- **{domain}** — {weight}% / {count} questions")
        else:
            st.write(f"- **{domain}**")

    st.info(
        """
        - Single-answer questions use radio buttons.
        - Multiple-answer questions use checkboxes.
        - You may mark questions for review and return before submitting.
        - Unanswered questions are allowed, but they count as incorrect.
        - Explanations appear only after final submission.
        """
    )

    st.session_state.randomize_questions = st.checkbox(
        "Randomize question order",
        value=st.session_state.randomize_questions
    )

    st.session_state.randomize_choices = st.checkbox(
        "Randomize answer choices",
        value=st.session_state.randomize_choices
    )

    if st.button("Begin Exam", type="primary"):
        st.session_state.started = True
        st.session_state.start_time = time.time()
        st.session_state.question_order = []
        st.session_state.choice_orders = {}
        st.session_state.answers = {}
        st.session_state.marked = set()
        st.session_state.current_question = 0
        st.session_state.review_mode = False
        st.session_state.submitted = False
        st.rerun()


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
        <div class="timer-fixed">
            <div class="timer-label">Time Remaining</div>
            <div class="timer-box">{mins:02d}:{secs:02d}</div>
        </div>
        <div class="navigator-spacer"></div>
        <div class="question-nav-title">Question Navigator</div>
        <div class="small-help">✓ answered &nbsp;&nbsp; 🚩 marked</div>
        """,
        unsafe_allow_html=True
    )

    # Three-column question navigator
    nav_cols = st.sidebar.columns(3)

    for i in range(len(questions)):

        if i in st.session_state.answers and i in st.session_state.marked:
            label = f"{i + 1} ✅ 🚩"

        elif i in st.session_state.answers:
            label = f"{i + 1} ✅"

        elif i in st.session_state.marked:
            label = f"{i + 1} 🚩"

        else:
            label = f"{i + 1}"

        with nav_cols[i % 3]:
            if st.button(label, key=f"nav_{i}"):
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
            st.warning(
                f"You still have {unanswered} unanswered question(s). "
                "You can submit, but unanswered questions count as incorrect."
            )

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
            unsafe_allow_html=True
        )

        st.progress((q_index + 1) / len(questions))

        st.markdown('<div class="question-card">', unsafe_allow_html=True)

        st.caption(
            f"Domain: {q.get('topic', 'Uncategorized')} | "
            f"Difficulty: {q.get('difficulty', 'N/A')}"
        )

        st.subheader(q["question"])

        question_type = q.get("type", "single")

        if question_type == "multiple":
            select_count = q.get("select_count", "all correct")
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
                key=f"question_{q_index}"
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
        user_answer = st.session_state.answers.get(i, [])

        if is_correct(user_answer, q["answers"]):
            correct += 1

    score = round((correct / len(questions)) * 100, 2)

    st.header("Exam Results")

    c1, c2, c3 = st.columns(3)
    c1.metric("Score", f"{score}%")
    c2.metric("Correct", f"{correct} / {len(questions)}")
    c3.metric("Passing Score", f"{PASSING_SCORE}%")

    if score >= PASSING_SCORE:
        st.success("PASS")
    else:
        st.error("FAIL")

    st.divider()

    st.header("Performance Breakdown")

    st.subheader("By Domain")
    for topic, data in calculate_breakdown("topic").items():
        percent = round((data["correct"] / data["total"]) * 100, 2)
        st.write(
            f"**{topic}:** {data['correct']} / {data['total']} correct ({percent}%)"
        )

    st.subheader("By Difficulty")
    for difficulty, data in calculate_breakdown("difficulty").items():
        percent = round((data["correct"] / data["total"]) * 100, 2)
        st.write(
            f"**{difficulty}:** {data['correct']} / {data['total']} correct ({percent}%)"
        )

    st.divider()

    st.header("Answer Review")

    review_filter = st.radio(
        "Review filter:",
        ["All Questions", "Incorrect Only", "Correct Only"],
        horizontal=True
    )

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

        st.caption(
            f"Domain: {q.get('topic', 'Uncategorized')} | "
            f"Difficulty: {q.get('difficulty', 'N/A')}"
        )

        st.write(q["question"])
        st.write(
            "Your answer: "
            + (", ".join(user_answer) if user_answer else "No answer selected")
        )
        st.write("Correct answer: " + ", ".join(correct_answers))
        st.info(q["explanation"])
        st.divider()

    if st.button("Restart Exam"):
        reset_exam()
