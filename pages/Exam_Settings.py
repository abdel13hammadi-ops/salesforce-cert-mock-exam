import streamlit as st
from supabase import create_client

APP_VERSION = "EXAM_SETTINGS_V1_CERT_LANGUAGE_SELECTOR"

st.set_page_config(page_title="Exam Settings", layout="wide")


def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        st.error("Missing Supabase secrets. Please check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


def fetch_certifications():
    supabase = get_supabase_client()
    result = (
        supabase.table("certifications")
        .select("exam_name, display_name, certification_code, passing_score, time_limit_minutes, question_count, is_active")
        .eq("is_active", True)
        .order("display_name")
        .execute()
    )
    return result.data or []


def fetch_languages():
    supabase = get_supabase_client()
    result = (
        supabase.table("languages")
        .select("language_code, language_name, native_name, is_active, display_order")
        .eq("is_active", True)
        .order("display_order")
        .execute()
    )
    return result.data or []


def fetch_domains(exam_name):
    supabase = get_supabase_client()
    result = (
        supabase.table("certification_domains")
        .select("domain_name, weight, question_count, display_order")
        .eq("exam_name", exam_name)
        .eq("is_active", True)
        .order("display_order")
        .execute()
    )
    return result.data or []


def get_current_user_email():
    email = st.session_state.get("user_email", "")
    return str(email).strip().lower()


def save_user_language(email, language_code):
    if not email:
        return
    supabase = get_supabase_client()
    supabase.table("app_users").update({
        "preferred_language_code": language_code
    }).eq("email", email).execute()


st.title("Exam Settings")
st.caption(f"App version: {APP_VERSION}")

certifications = fetch_certifications()
languages = fetch_languages()

if not certifications:
    st.error("No active certifications found. Please add a certification in Supabase first.")
    st.stop()

if not languages:
    st.error("No active languages found. Please add languages in Supabase first.")
    st.stop()

# Defaults
if "selected_exam_name" not in st.session_state:
    st.session_state["selected_exam_name"] = certifications[0]["exam_name"]

if "selected_language_code" not in st.session_state:
    st.session_state["selected_language_code"] = "en"

cert_options = [c["exam_name"] for c in certifications]
cert_labels = {
    c["exam_name"]: f"{c.get('display_name') or c['exam_name']}" + (f" ({c.get('certification_code')})" if c.get("certification_code") else "")
    for c in certifications
}

if st.session_state["selected_exam_name"] not in cert_options:
    st.session_state["selected_exam_name"] = cert_options[0]

lang_options = [l["language_code"] for l in languages]
lang_labels = {
    l["language_code"]: f"{l.get('language_name') or l['language_code']}" + (f" — {l.get('native_name')}" if l.get("native_name") and l.get("native_name") != l.get("language_name") else "")
    for l in languages
}

if st.session_state["selected_language_code"] not in lang_options:
    st.session_state["selected_language_code"] = "en" if "en" in lang_options else lang_options[0]

st.markdown("Choose the certification and language you want to use across the platform.")

selected_exam = st.selectbox(
    "Certification",
    cert_options,
    index=cert_options.index(st.session_state["selected_exam_name"]),
    format_func=lambda x: cert_labels.get(x, x),
)

selected_language = st.selectbox(
    "Language",
    lang_options,
    index=lang_options.index(st.session_state["selected_language_code"]),
    format_func=lambda x: lang_labels.get(x, x),
)

if st.button("Save Settings", type="primary"):
    st.session_state["selected_exam_name"] = selected_exam
    st.session_state["selected_language_code"] = selected_language
    save_user_language(get_current_user_email(), selected_language)
    st.success("Exam settings saved ✅")
    st.rerun()

st.divider()

active_cert = next((c for c in certifications if c["exam_name"] == selected_exam), certifications[0])

c1, c2, c3 = st.columns(3)
c1.metric("Passing Score", f"{active_cert.get('passing_score', 65)}%")
c2.metric("Time Limit", f"{active_cert.get('time_limit_minutes', 105)} min")
c3.metric("Questions", active_cert.get("question_count", 60))

st.subheader("Domain Breakdown")
domains = fetch_domains(selected_exam)

if not domains:
    st.warning("No domains found for this certification yet.")
else:
    for d in domains:
        st.write(f"- **{d['domain_name']}** — {d['weight']}% / {d['question_count']} questions")

st.divider()
st.info(
    "For now, only the Salesforce Certified Administrator English question bank is available. "
    "This settings page prepares the platform for additional certifications and languages later."
)
