
import streamlit as st

st.set_page_config(page_title="Admin Import", layout="wide")

st.title("Admin Import Page")
st.success("This page is working.")

st.subheader("Step 1: Check Streamlit Secrets")

supabase_url = st.secrets.get("SUPABASE_URL")
supabase_key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")

if supabase_url:
    st.success("SUPABASE_URL found.")
else:
    st.error("SUPABASE_URL is missing.")

if supabase_key:
    st.success("SUPABASE_SERVICE_ROLE_KEY found.")
else:
    st.error("SUPABASE_SERVICE_ROLE_KEY is missing.")

st.info("This page is only testing whether the secrets are available. It is not importing questions yet.")
