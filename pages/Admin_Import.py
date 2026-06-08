import json
import streamlit as st
from supabase import create_client

st.set_page_config(page_title="Admin Import", layout="wide")

st.title("Admin Import Questions")
st.success("Secrets confirmed!")

supabase_url = st.secrets["SUPABASE_URL"]
supabase_key = st.secrets["SUPABASE_SERVICE_ROLE_KEY"]
supabase = create_client(supabase_url, supabase_key)

uploaded_file = st.file_uploader("Upload one JSON question file", type=["json"])

if uploaded_file:
    source_file = uploaded_file.name
    source_batch = source_file.replace(".json","")
    try:
        questions = json.load(uploaded_file)
        st.success(f"File loaded: {len(questions)} questions")

        # Add import button
        if st.button("Import this JSON file into Supabase"):
            imported_count = 0
            for q in questions:
                # Minimal insert for test
                data = {
                    "exam_name": q.get("exam","Salesforce Certified Administrator"),
                    "category": q.get("topic","Uncategorized"),
                    "question_text": q.get("question",""),
                    "question_type": q.get("type","single"),
                    "select_count": q.get("select_count"),
                    "difficulty": q.get("difficulty","medium"),
                    "is_exam_eligible": q.get("is_exam_eligible", True),
                    "quality_status": q.get("quality_status","approved"),
                    "review_notes": q.get("review_notes",""),
                    "explanation": q.get("explanation",""),
                    "source_batch": source_batch,
                    "source_file": source_file,
                    "is_active": True
                }
                supabase.table("questions").insert(data).execute()
                imported_count += 1

            st.success(f"Imported {imported_count} questions to Supabase!")

    except Exception as e:
        st.error("Failed to load JSON file")
        st.exception(e)
