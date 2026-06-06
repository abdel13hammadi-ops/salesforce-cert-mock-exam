import json
import time
import random
from collections import defaultdict
import streamlit as st
from streamlit_autorefresh import st_autorefresh


def load_config():
    with open("exam_config.json", "r") as file:
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
    with open(QUESTION_FILE, "r") as file:
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


def domain_counts():
    counts = defaultdict(int)
    for q in all_questions:
        counts[q.get("topic", "Uncategorized")] += 1
    return counts


def reset_exam():
    for key in list(defaults.keys()):
        if key in st.session_state:
            del st.session_state[key]
    st.rerun()


st.markdown(
    """
    <style>
    .block-container {padding-top: 1.1rem; max-width: 1180px;}
    [data-testid="stSidebar"] {background-color: #f7f8fa;}
    .exam-shell {
        border: 1px solid #c9cdd3;
        border-radius: 6px;
        background: #ffffff;
        box-shadow: 0 1px 2px rgba(0,0,0,.06);
        margin-bottom: 14px;
    }
    .exam-topbar {
        background: #243447;
        color: #ffffff;
        padding: 12px 18px;
        border-radius: 6px 6px 0 0;
        font-size: 20px;
        font-weight: 700;
    }
    .exam-subbar {
        background: #eef2f7;
        color: #23313f;
        padding: 10px 18px;
        border-top: 1px solid #c9cdd3;
        border-bottom: 1px solid #c9cdd3;
        font-size: 14px;
    }
    .exam-body {
        padding: 18px;
    }
    .timer-box {
        background:#fff4d6;
        border:1px solid #d29b00;
        border-radius:6px;
        padding:12px;
        text-align:center;
        font-size:26px;
        font-weight:800;
        color:#1f2933;
    }
    .qcard {
        border:1px solid #d6d9de;
        border-radius:6px;
        padding:20px;
        background:white;
        margin-top:12px;
    }
    .qid {
        color:#5f6670;
        font-size:14px;
        font-weight:600;
        margin-bottom:8px;
    }
    .domain-pill {
        display:inline-block;
        background:#eef2f7;
        border:1px solid #d4dae3;
        border-radius:18px;
        padding:4px 10px;
        font-size:12px;
        color:#3c4653;
        margin-bottom:10px;
    }
    .small-note {font-size:13px; color:#5f6670;}
    .nav-legend {font-size: 13px; color:#4b5563; line-height:1.4;}
    </style>
    """,
    unsafe_allow_html=True
)

st.markdown(
    f"""
    <div class="exam-shell">
      <div class="exam-topbar">{config["exam_title"]}</div>
      <div class="exam-subbar">{config["certification"]} &nbsp;|&nbsp; {len(all_questions)} questions &nbsp;|&nbsp; {EXAM_MINUTES} minutes &nbsp;|&nbsp; Passing score: {PASSING_SCORE}%</div>
    </div>
    """,
    unsafe_allow_html=True
)


if not st.session_state.started:
    st.header("Exam Instructions")

    st.write("This simulator uses the current Platform Administrator-style structure, including Agentforce AI.")
    st.write("Answers and explanations are hidden until after final submission.")

    c1, c2, c3 = st.columns(3)
    c1.metric("Questions", len(all_questions))
    c2.metric("Time Limit", f"{EXAM_MINUTES} min")
    c3.metric("Passing Score", f"{PASSING_SCORE}%")

    st.subheader("Domain Breakdown")
    counts = domain_counts()

    for section in config["sections"]:
        name = section["name"]
        st.write(
            f"**{name}:** {section['weight']}% target, "
            f"{section['target_questions']} questions planned, "
            f"{counts.get(name, 0)} in this mock exam"
        )

    st.info(
        """
        Exam behavior:
        - Single-answer questions use radio buttons.
        - Multiple-answer questions use checkboxes and show how many answers to choose.
        - Use Mark for Review to flag questions.
        - Use Review / Submit before final submission.
        - The timer auto-submits when time expires.
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

    st.sidebar.markdown("## Time Remaining")
    st.sidebar.markdown(
        f"<div class='timer-box'>{mins:02d}:{secs:02d}</div>",
        unsafe_allow_html=True
    )

    st.sidebar.markdown("## Question Navigator")
    st.sidebar.markdown(
        "<div class='nav-legend'>✓ Answered<br>🚩 Marked for review</div>",
        unsafe_allow_html=True
    )

    nav_cols = st.sidebar.columns(4)
    for i in range(len(questions)):
        label = f"{i + 1}"
        if i in st.session_state.answers:
            label += "✓"
        if i in st.session_state.marked:
            label += "🚩"

        with nav_cols[i % 4]:
            if st.button(label, key=f"nav_{i}", use_container_width=True):
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
            st.warning("You still have unanswered questions. You can submit, but unanswered questions will be scored incorrect.")

        st.divider()

        for i in range(len(questions)):
            status = "Answered" if i in st.session_state.answers else "Unanswered"
            if i in st.session_state.marked:
                status += " | 🚩 Marked"
            st.write(f"Question {i + 1}: {status}")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Return to Exam", use_container_width=True):
                st.session_state.review_mode = False
                st.rerun()

        with col2:
            if st.button("Final Submit", type="primary", use_container_width=True):
                st.session_state.submitted = True
                st.rerun()

    else:
        q_index = st.session_state.current_question
        q = questions[q_index]
        options = get_options(q_index, q)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Question", f"{q_index + 1} of {len(questions)}")
        c2.metric("Answered", len(st.session_state.answers))
        c3.metric("Marked", len(st.session_state.marked))
        c4.metric("Unanswered", len(questions) - len(st.session_state.answers))

        st.progress((q_index + 1) / len(questions))

        st.markdown("<div class='qcard'>", unsafe_allow_html=True)
        st.markdown(f"<div class='qid'>Question {q_index + 1}</div>", unsafe_allow_html=True)
        st.markdown(
            f"<span class='domain-pill'>{q.get('topic', 'Uncategorized')} • {q.get('difficulty', 'N/A')}</span>",
            unsafe_allow_html=True
        )

        st.subheader(q["question"])

        question_type = q.get("type", "single")

        if question_type == "multiple":
            select_count = q.get("select_count", len(q["answers"]))
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

            if len(selected_answers) > select_count:
                st.error(f"You selected {len(selected_answers)} answers. This question asks for {select_count}.")
            elif 0 < len(selected_answers) < select_count:
                st.warning(f"You selected {len(selected_answers)} answer(s). This question asks for {select_count}.")

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
            if st.button("Previous", use_container_width=True) and q_index > 0:
                st.session_state.current_question -= 1
                st.rerun()

        with col2:
            if st.button("Next", use_container_width=True) and q_index < len(questions) - 1:
                st.session_state.current_question += 1
                st.rerun()

        with col3:
            if q_index in st.session_state.marked:
                if st.button("Unmark", use_container_width=True):
                    st.session_state.marked.remove(q_index)
                    st.rerun()
            else:
                if st.button("Mark for Review", use_container_width=True):
                    st.session_state.marked.add(q_index)
                    st.rerun()

        with col4:
            if st.button("Review / Submit", type="primary", use_container_width=True):
                st.session_state.review_mode = True
                st.rerun()


else:
    correct = 0

    for i, q in enumerate(questions):
        if is_correct(st.session_state.answers.get(i, []), q["answers"]):
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

    st.subheader("By Exam Domain")
    for section in config["sections"]:
        topic = section["name"]
        stats = calculate_breakdown("topic").get(topic, {"correct": 0, "total": 0})
        if stats["total"] == 0:
            percent = 0
        else:
            percent = round((stats["correct"] / stats["total"]) * 100, 2)

        st.write(
            f"**{topic}:** {stats['correct']} / {stats['total']} correct "
            f"({percent}%) — target weight {section['weight']}%"
        )

    st.subheader("By Difficulty")
    for difficulty, data in calculate_breakdown("difficulty").items():
        percent = round((data["correct"] / data["total"]) * 100, 2)
        st.write(f"**{difficulty}:** {data['correct']} / {data['total']} correct ({percent}%)")

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
        st.write("Your answer: " + (", ".join(user_answer) if user_answer else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_answers))
        st.info(q["explanation"])
        st.divider()

    if st.button("Restart Exam", type="primary"):
        reset_exam()
