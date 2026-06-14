import streamlit as st
from utils.access_control import require_admin, get_supabase_admin_client
from datetime import datetime

require_admin()

st.title("👤 Admin Users Management")

supabase = get_supabase_admin_client()


# -----------------------------
# SEARCH USER
# -----------------------------
st.header("🔎 Search User")

email = st.text_input("Enter user email")

user_data = None
attempts_data = []

if st.button("Search User") and email:
    user_res = supabase.table("app_users") \
        .select("*") \
        .eq("email", email.lower()) \
        .execute()

    if user_res.data:
        user_data = user_res.data[0]
    else:
        st.warning("No app user found")

    attempts_res = supabase.table("exam_attempts") \
        .select("*") \
        .eq("user_email", email.lower()) \
        .order("completed_at", desc=True) \
        .limit(20) \
        .execute()

    attempts_data = attempts_res.data or []


# -----------------------------
# USER INFO PANEL
# -----------------------------
if user_data:
    st.subheader("👤 User Profile")

    st.write(f"**Email:** {user_data['email']}")
    st.write(f"**Status:** {user_data['subscription_status']}")
    st.write(f"**Created:** {user_data.get('created_at')}")
    st.write(f"**Updated:** {user_data.get('updated_at')}")

    st.divider()

    col1, col2, col3 = st.columns(3)

    # -------------------------
    # GRANT PREMIUM
    # -------------------------
    with col1:
        if st.button("💎 Grant Premium"):
            supabase.table("app_users") \
                .update({
                    "subscription_status": "active",
                    "updated_at": datetime.utcnow().isoformat()
                }) \
                .eq("email", email.lower()) \
                .execute()

            st.success("Premium granted")
            st.rerun()

    # -------------------------
    # REVOKE PREMIUM
    # -------------------------
    with col2:
        if st.button("🆓 Set Free"):
            supabase.table("app_users") \
                .update({
                    "subscription_status": "free",
                    "updated_at": datetime.utcnow().isoformat()
                }) \
                .eq("email", email.lower()) \
                .execute()

            st.warning("User set to free")
            st.rerun()

    # -------------------------
    # DELETE APP PROFILE
    # -------------------------
    with col3:
        confirm = st.text_input("Type email to confirm delete")

        if st.button("🗑️ Delete Profile"):
            if confirm.lower() == email.lower():
                supabase.table("user_certification_access") \
                    .delete() \
                    .eq("user_email", email.lower()) \
                    .execute()

                supabase.table("app_users") \
                    .delete() \
                    .eq("email", email.lower()) \
                    .execute()

                st.error("User deleted")
                st.rerun()
            else:
                st.error("Email confirmation does not match")


# -----------------------------
# ATTEMPTS
# -----------------------------
st.divider()
st.header("📊 Exam Attempts")

if attempts_data:
    for a in attempts_data:
        st.write(
            f"**{a['exam_name']}** | "
            f"{a['score']}% | "
            f"{a['correct_answers']}/{a['total_questions']} | "
            f"{a['completed_at']}"
        )
else:
    st.info("No attempts found")
