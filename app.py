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

st.title("Salesforce Certification Mock Exam")

if "submitted" not in st.session_state:
    st.session_state.submitted = False

st.write(f"Total Questions: {len(questions)}")
st.write(f"Passing Score: {PASSING_SCORE}%")

user_answers = {}

with st.form("exam_form"):
    for i, q in enumerate(questions):
        st.subheader(f"Question {i + 1}")
        st.write(q["question"])

        user_answers[i] = st.radio(
            "Choose one answer:",
            q["options"],
            key=f"q_{i}"
        )

    submitted = st.form_submit_button("Submit Exam")

if submitted:
    st.session_state.submitted = True

    correct = 0

    st.header("Exam Results")

    for i, q in enumerate(questions):
        user_answer = st.session_state[f"q_{i}"]

        if user_answer == q["answer"]:
            correct += 1

    score = round((correct / len(questions)) * 100, 2)

    st.subheader(f"Score: {score}%")
    st.subheader(f"Correct Answers: {correct} / {len(questions)}")

    if score >= PASSING_SCORE:
        st.success("PASS")
    else:
        st.error("FAIL")

    st.header("Answer Review")

    for i, q in enumerate(questions):
        user_answer = st.session_state[f"q_{i}"]

        st.subheader(f"Question {i + 1}")
        st.write(q["question"])
        st.write(f"Your answer: {user_answer}")
        st.write(f"Correct answer: {q['answer']}")
        st.info(q["explanation"])
