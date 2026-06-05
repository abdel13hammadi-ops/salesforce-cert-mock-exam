import streamlit as st

st.set_page_config(
    page_title="Salesforce Certification Mock Exam",
    layout="wide"
)

st.title("Salesforce Certification Mock Exam")

st.subheader("Welcome!")

st.write("""
This platform will simulate Salesforce certification exams.

Features coming soon:
- Timed exams
- 60-question mock tests
- Admin certification
- BA certification
- Score reports
- Answer explanations
""")

if st.button("Start Mock Exam"):
    st.success("Exam engine coming next!")
