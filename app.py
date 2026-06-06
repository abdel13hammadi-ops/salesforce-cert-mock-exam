import json
import time
import random
from collections import defaultdict
import streamlit as st

st.set_page_config(
    page_title="Salesforce Admin Mock Exam",
    layout="wide"
)

PASSING_SCORE = 65
EXAM_MINUTES = 105
QUESTION_FILE = "questions/mock_exam_1.json"

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

def normalize_answer(answer):
    if answer is None:
        return []
    if isinstance(answer, list):
        return answer
    return [answer]

def is_correct(user_answer, correct_answers):
    return set(normalize_answer(user_answer)) == set(correct_answers)

def calculate_breakdown(field):
    stats = defaultdict(lambda: {"correct": 0, "total": 0})

    for i, q in enumerate(questions):
        value = q.get(field, "Uncategorized")
        stats[value]["total"] += 1

        if is_correct(st.session_state.answers.get(i, []), q["answers"]):
            stats[value]["correct"] += 1

    return stats

st.markdown("""
<style>
.exam-header {
    background-color: #f4f6f9;
    padding: 18px;
    border-radius: 10px;
    border: 1px solid #d8dde6;
    margin-bottom: 20px;
}
.exam-title {
    font-size: 28px;
    font-weight: 700;
}
.exam-subtitle {
    color: #5f6368;
    font-size: 15px;
}
.timer-box {
    background-color: #fff3cd;
    padding: 12px;
    border-radius: 8px;
    border: 1px solid #ffeeba;
    text-align: center;
    font-size: 22px;
    font-weight: 700;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="exam-header">
    <div class="exam-title">Salesforce Admin Mock Exam Simulator</div>
    <div class="exam-subtitle">Timed practice exam with review, scoring, explanations, and performance breakdown.</div>
</div>
""", unsafe_allow_html=True)

if not st.session_state.started:
    st.header("Exam Instructions")
    st.write(f"Time limit: {EXAM_MINUTES} minutes")
    st.write(f"Questions: {len(all_questions)}")
    st.write(f"Passing score: {PASSING_SCORE}%")

    st.write("""
    Instructions:
    - Read each question carefully.
    - Some questions may have multiple correct answers.
    - You can mark questions for review.
    - Review all answers before final submission.
    - Explanations will appear only after submission.
    """)

    st.session_state.randomize_questions = st.checkbox(
        "Randomize question order",
        value=st.session_state.randomize_questions
    )

    st.session_state.randomize_choices = st.checkbox(
        "Randomize answer choices",
        value=st.session_state.randomize_choices
    )

    if st.button("Start Exam"):
        st.session_state.started = True
        st.session_state.start_time = time.time()
        st.session_state.question_order = []
        st.session_state.choice_orders = {}
        st.rerun()

elif not st.session_state.submitted:
    elapsed = time.time() - st.session_state.start_time
    remaining = (EXAM_MINUTES * 60) - elapsed

    if remaining <= 0:
        st.session_state.submitted = True
        st.rerun()

    mins = int(remaining // 60)
    secs = int(remaining % 60)

    st.sidebar.markdown("## Exam Timer")
    st.sidebar.markdown(
        f"<div class='timer-box'>{mins:02d}:{secs:02d}</div>",
        unsafe_allow_html=True
    )

    st.sidebar.markdown("## Question Navigator")

    for i in range(len(questions)):
        label = f"Q{i + 1}"

        if i in st.session_state.answers:
            label += " ✓"

        if i in st.session_state.marked:
            label += " 🚩"

        if st.sidebar.button(label, key=f"nav_{i}"):
            st.session_state.current_question = i
            st.session_state.review_mode = False
            st.rerun()

    if st.session_state.review_mode:
        st.header("Review Before Submit")

        answered = len(st.session_state.answers)
        unanswered = len(questions) - answered

        st.metric("Answered", answered)
        st.metric("Unanswered", unanswered)
        st.metric("Marked for Review", len(st.session_state.marked))

        st.divider()

        for i in range(len(questions)):
            status = "Answered" if i in st.session_state.answers else "Unanswered"

            if i in st.session_state.marked:
                status += " | 🚩 Marked for Review"

            st.write(f"Question {i + 1}: {status}")

        col1, col2 = st.columns(2)

        with col1:
            if st.button("Return to Exam"):
                st.session_state.review_mode = False
                st.rerun()

        with col2:
            if st.button("Final Submit"):
                st.session_state.submitted = True
                st.rerun()

    else:
        q_index = st.session_state.current_question
        q = questions[q_index]
        options = get_options(q_index, q)

        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Question", f"{q_index + 1} / {len(questions)}")
        col_b.metric("Answered", len(st.session_state.answers))
        col_c.metric("Marked", len(st.session_state.marked))

        st.progress((q_index + 1) / len(questions))

        st.caption(
            f"Topic: {q.get('topic', 'Uncategorized')} | "
            f"Difficulty: {q.get('difficulty', 'N/A')}"
        )

        st.subheader(q["question"])

        question_type = q.get("type", "single")

        if question_type == "multiple":
            st.write(f"Select {q.get('select_count', 'all that apply')} answers.")

            selected = st.multiselect(
                "Choose answers:",
                options,
                default=st.session_state.answers.get(q_index, []),
                key=f"question_{q_index}"
            )

            if selected:
                st.session_state.answers[q_index] = selected
            elif q_index in st.session_state.answers:
                del st.session_state.answers[q_index]

        else:
            previous_answer = st.session_state.answers.get(q_index, [])
            previous_answer = previous_answer[0] if previous_answer else None

            selected = st.radio(
                "Choose one answer:",
                options,
                index=options.index(previous_answer)
                if previous_answer in options
                else None,
                key=f"question_{q_index}"
            )

            if selected:
                st.session_state.answers[q_index] = [selected]

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
                if st.button("Unmark Review"):
                    st.session_state.marked.remove(q_index)
                    st.rerun()
            else:
                if st.button("Mark for Review"):
                    st.session_state.marked.add(q_index)
                    st.rerun()

        with col4:
            if st.button("Review / Submit"):
                st.session_state.review_mode = True
                st.rerun()

else:
    correct = 0

    for i, q in enumerate(questions):
        if is_correct(st.session_state.answers.get(i, []), q["answers"]):
            correct += 1

    score = round((correct / len(questions)) * 100, 2)

    st.header("Exam Results")

    col1, col2, col3 = st.columns(3)
    col1.metric("Score", f"{score}%")
    col2.metric("Correct", f"{correct} / {len(questions)}")
    col3.metric("Passing Score", f"{PASSING_SCORE}%")

    if score >= PASSING_SCORE:
        st.success("PASS")
    else:
        st.error("FAIL")

    st.divider()

    st.header("Performance Breakdown")

    st.subheader("By Topic")
    topic_stats = calculate_breakdown("topic")

    for topic, data in topic_stats.items():
        percent = round((data["correct"] / data["total"]) * 100, 2)
        st.write(f"**{topic}:** {data['correct']} / {data['total']} correct ({percent}%)")

    st.subheader("By Difficulty")
    difficulty_stats = calculate_breakdown("difficulty")

    for difficulty, data in difficulty_stats.items():
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
            f"Topic: {q.get('topic', 'Uncategorized')} | "
            f"Difficulty: {q.get('difficulty', 'N/A')}"
        )

        st.write(q["question"])
        st.write("Your answer: " + (", ".join(user_answer) if user_answer else "No answer selected"))
        st.write("Correct answer: " + ", ".join(correct_answers))
        st.info(q["explanation"])
        st.divider()

    if st.button("Restart Exam"):
        for key in list(defaults.keys()):
            if key in st.session_state:
                del st.session_state[key]
        st.rerun()
