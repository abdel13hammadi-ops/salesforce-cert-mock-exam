import json
import streamlit as st

st.set_page_config(
    page_title="Salesforce Certification Mock Exam",
    layout="wide"
)

PASSING_SCORE = 65

def load_questions():
    with open("questions/mock_exam_1.json", "r") as file:
        return json.load(file)

questions = load_questions()

if "current_question" not in st.session_state:
    st.session_state.current_question = 0

if "answers" not in st.session_state:
    st.session_state.answers = {}

if "submitted" not in st.session_state:
    st.session_state.submitted = False

st.title("Salesforce Certification Mock Exam")

if not st.session_state.submitted:
    q_index = st.session_state.current_question
    q = questions[q_index]

    st.write(f"Question {q_index + 1} of {len(questions)}")
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

    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("Previous") and q_index > 0:
            st.session_state.current_question -= 1
            st.rerun()

    with col2:
        if st.button("Next") and q_index < len(questions) - 1:
            st.session_state.current_question += 1
            st.rerun()

    with col3:
        if st.button("Submit Exam"):
            st.session_state.submitted = True
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
        st.session_state.current_question = 0
        st.session_state.answers = {}
        st.session_state.submitted = False
        st.rerun()
