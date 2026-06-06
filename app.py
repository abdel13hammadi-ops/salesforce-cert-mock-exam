import json
import time
import streamlit as st

st.set_page_config(
    page_title="Salesforce Certification Mock Exam",
    layout="wide"
)

PASSING_SCORE = 65
EXAM_MINUTES = 105

def load_questions():
    with open("questions/mock_exam_1.json", "r") as file:
        return json.load(file)

questions = load_questions()

defaults = {
    "started": False,
    "submitted": False,
    "review_mode": False,
    "current_question": 0,
    "answers": {},
    "marked": set(),
    "start_time": None
}

for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value

st.title("Salesforce Certification Mock Exam")

if not st.session_state.started:
    st.header("Exam Instructions")
    st.write(f"Time limit: {EXAM_MINUTES} minutes")
    st.write(f"Questions: {len(questions)}")
    st.write(f"Passing score: {PASSING_SCORE}%")
    st.write("You may mark questions for review and return to them before submitting.")

    if st.button("Start Exam"):
        st.session_state.started = True
        st.session_state.start_time = time.time()
        st.rerun()

elif not st.session_state.submitted:
    elapsed = time.time() - st.session_state.start_time
    remaining = (EXAM_MINUTES * 60) - elapsed

    if remaining <= 0:
        st.session_state.submitted = True
        st.rerun()

    mins = int(remaining // 60)
    secs = int(remaining % 60)

    st.sidebar.header("Exam Timer")
    st.sidebar.subheader(f"{mins:02d}:{secs:02d}")

    st.sidebar.header("Question Navigator")
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

        st.write(f"Answered: {answered}")
        st.write(f"Unanswered: {unanswered}")
        st.write(f"Marked for Review: {len(st.session_state.marked)}")

        for i in range(len(questions)):
            status = "Answered" if i in st.session_state.answers else "Unanswered"
            flag = " | Marked for Review" if i in st.session_state.marked else ""
            st.write(f"Question {i + 1}: {status}{flag}")

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

        st.write(f"Time Remaining: **{mins:02d}:{secs:02d}**")
        st.write(f"Question {q_index + 1} of {len(questions)}")
        st.write(f"Answered: {len(st.session_state.answers)} | Marked: {len(st.session_state.marked)}")
        st.progress((q_index + 1) / len(questions))

        st.subheader(q["question"])

        selected = st.radio(
            "Choose one answer:",
            q["options"],
            index=q["options"].index(st.session_state.answers[q_index])
            if q_index in st.session_state.answers else None,
            key=f"question_{q_index}"
        )

        if selected:
            st.session_state.answers[q_index] = selected

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
        if st.session_state.answers.get(i) == q["answer"]:
            correct += 1

    score = round((correct / len(questions)) * 100, 2)

    st.header("Exam Results")
    st.subheader(f"Score: {score}%")
    st.subheader(f"Correct Answers: {correct} / {len(questions)}")

    if score >= PASSING_SCORE:
        st.success("PASS")
    else:
        st.error("FAIL")

    st.header("Answer Review")

    for i, q in enumerate(questions):
        user_answer = st.session_state.answers.get(i, "No answer selected")

        st.subheader(f"Question {i + 1}")
        st.write(q["question"])
        st.write(f"Your answer: {user_answer}")
        st.write(f"Correct answer: {q['answer']}")
        st.info(q["explanation"])

    if st.button("Restart Exam"):
        for key in defaults:
            del st.session_state[key]
        st.rerun()
