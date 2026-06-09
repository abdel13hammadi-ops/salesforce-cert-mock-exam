import json
from datetime import datetime

import pandas as pd
import streamlit as st
from supabase import create_client

APP_VERSION = "MY_PROGRESS_V1"

st.set_page_config(
    page_title="My Progress",
    layout="wide",
    initial_sidebar_state="expanded",
)


def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        st.error("Supabase secrets are missing. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()

    return create_client(url, key)


def load_attempts():
    supabase = get_supabase_client()
    result = (
        supabase.table("exam_attempts")
        .select("id,user_email,mode,category,score,correct_answers,total_questions,domain_breakdown,difficulty_breakdown,completed_at")
        .order("id", desc=True)
        .execute()
    )
    return result.data or []


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


def make_breakdown_table(attempts, field_name):
    totals = {}

    for attempt in attempts:
        breakdown = normalize_breakdown(attempt.get(field_name))
        for name, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            correct = int(data.get("correct", 0) or 0)
            total = int(data.get("total", 0) or 0)
            if name not in totals:
                totals[name] = {"correct": 0, "total": 0}
            totals[name]["correct"] += correct
            totals[name]["total"] += total

    rows = []
    for name, data in totals.items():
        total = data["total"]
        correct = data["correct"]
        percent = round((correct / total) * 100, 2) if total else 0
        rows.append({
            "Area": name,
            "Correct": correct,
            "Total": total,
            "Accuracy %": percent,
        })

    return pd.DataFrame(rows).sort_values("Accuracy %") if rows else pd.DataFrame()


st.title("My Progress")
st.caption(f"App version: {APP_VERSION}")

attempts = load_attempts()

if not attempts:
    st.info("No exam attempts saved yet. Complete a timed mock exam first, then come back here.")
    st.stop()

# Summary metrics
scores = [float(a.get("score") or 0) for a in attempts]
latest = attempts[0]
latest_score = float(latest.get("score") or 0)
average_score = round(sum(scores) / len(scores), 2) if scores else 0
best_score = round(max(scores), 2) if scores else 0
attempt_count = len(attempts)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Latest Score", f"{latest_score}%")
c2.metric("Average Score", f"{average_score}%")
c3.metric("Best Score", f"{best_score}%")
c4.metric("Attempts", attempt_count)

st.divider()

# Weak areas
st.header("Weak Areas")

domain_df = make_breakdown_table(attempts, "domain_breakdown")
difficulty_df = make_breakdown_table(attempts, "difficulty_breakdown")

left, right = st.columns(2)

with left:
    st.subheader("By Domain")
    if domain_df.empty:
        st.warning("No domain breakdown data saved yet.")
    else:
        st.dataframe(domain_df, use_container_width=True, hide_index=True)
        weakest_domain = domain_df.iloc[0]
        st.info(f"Weakest domain: {weakest_domain['Area']} ({weakest_domain['Accuracy %']}%)")

with right:
    st.subheader("By Difficulty")
    if difficulty_df.empty:
        st.warning("No difficulty breakdown data saved yet.")
    else:
        st.dataframe(difficulty_df, use_container_width=True, hide_index=True)
        weakest_difficulty = difficulty_df.iloc[0]
        st.info(f"Weakest difficulty: {weakest_difficulty['Area']} ({weakest_difficulty['Accuracy %']}%)")

st.divider()

# Attempt history
st.header("Attempt History")

history_rows = []
for attempt in attempts:
    completed_at = attempt.get("completed_at")
    history_rows.append({
        "Attempt ID": attempt.get("id"),
        "Completed At": completed_at if completed_at else "Not recorded",
        "Mode": attempt.get("mode"),
        "Category": attempt.get("category"),
        "Score %": attempt.get("score"),
        "Correct": attempt.get("correct_answers"),
        "Total": attempt.get("total_questions"),
    })

history_df = pd.DataFrame(history_rows)
st.dataframe(history_df, use_container_width=True, hide_index=True)

st.divider()

st.header("Recommendation")
if not domain_df.empty:
    weakest = domain_df.iloc[0]
    st.write(
        f"Focus next on **{weakest['Area']}**. Your current accuracy there is **{weakest['Accuracy %']}%**."
    )
else:
    st.write("Complete more exams to generate recommendations.")
