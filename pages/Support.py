import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st
from supabase import create_client

from utils.access_control import (
    get_current_user_email as shared_get_current_user_email,
    get_preferred_timezone,
    render_app_chrome,
)

APP_VERSION = "SUPPORT_V2_PRODUCT_POLISH"

st.set_page_config(page_title="Support", layout="wide")
render_app_chrome()


def get_secret(name: str) -> str:
    value = str(os.environ.get(name, "") or "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, "") or "").strip()
    except Exception:
        return ""


@st.cache_resource(show_spinner=False)
def get_supabase_client():
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        st.error("Missing Supabase environment variables: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY.")
        st.stop()

    return create_client(url, key)


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def get_saved_email() -> str:
    return normalize_email(shared_get_current_user_email() or st.session_state.get("support_lookup_email") or "")


def is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def safe_text(value, fallback="") -> str:
    if value is None:
        return fallback
    return str(value)


def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_for_user_timezone(value, user_tz: str) -> str:
    dt = parse_datetime(value)
    if not dt:
        return "N/A"
    tz_name = str(user_tz or "UTC").strip() or "UTC"
    try:
        local_dt = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        tz_name = "UTC"
        local_dt = dt.astimezone(timezone.utc)
    return local_dt.strftime("%b %-d, %Y, %-I:%M %p %Z") if os.name != "nt" else local_dt.strftime("%b %#d, %Y, %#I:%M %p %Z")


def status_label(status: str) -> str:
    status = str(status or "open").strip().lower()
    labels = {
        "open": "Open",
        "in_progress": "In progress",
        "resolved": "Resolved",
        "closed": "Closed",
    }
    return labels.get(status, status.replace("_", " ").title())


st.title("Support")
st.caption(f"App version: {APP_VERSION}")

current_email = normalize_email(shared_get_current_user_email() or "")
lookup_email = get_saved_email()
user_timezone = get_preferred_timezone(current_email) if current_email else str(st.session_state.get("preferred_timezone") or "UTC")

st.markdown(
    """
Get help with question issues, confusing explanations, typos, technical problems, or account access.
Keep reports specific. Vague tickets waste everyone’s time and slow down fixes.
"""
)

summary_left, summary_mid, summary_right = st.columns(3)
summary_left.metric("Signed in", "Yes" if current_email else "No")
summary_mid.metric("Ticket status", "Open / In progress / Resolved")
summary_right.metric("Timezone", user_timezone or "UTC")

if current_email:
    st.info(f"Tickets will be submitted under your signed-in email: {current_email}")
else:
    st.warning("You can submit a ticket while logged out, but logging in keeps your ticket history attached to your account.")

supabase = get_supabase_client()

with st.form("support_ticket_form", clear_on_submit=False):
    st.subheader("Submit a Support Ticket")

    if current_email:
        user_email = st.text_input(
            "Email",
            value=current_email,
            disabled=True,
            help="Signed-in users submit tickets under their account email.",
        )
    else:
        user_email = st.text_input(
            "Email",
            value=lookup_email,
            placeholder="your@email.com",
            help="Use the same email you use for your account if you have one.",
        )

    issue_type = st.selectbox(
        "Issue type",
        [
            "Question issue",
            "Wrong answer",
            "Confusing explanation",
            "Typo / wording issue",
            "Technical issue",
            "Account issue",
            "Access issue",
            "Other",
        ],
    )

    related_question_id = st.text_input(
        "Related question ID (optional)",
        placeholder="Paste the question ID if this is about a specific question",
    )

    subject = st.text_input(
        "Subject",
        placeholder="Short summary of the issue",
    )

    message = st.text_area(
        "Message",
        height=180,
        placeholder="Describe the problem clearly. If this is about a question, include the exam, question text, and what looks wrong.",
    )

    submitted = st.form_submit_button("Submit Ticket", type="primary")

if submitted:
    user_email = normalize_email(current_email or user_email)
    subject = subject.strip()
    message = message.strip()
    related_question_id = related_question_id.strip() or None

    if not is_valid_email(user_email):
        st.error("Please enter a valid email address.")
    elif not subject:
        st.error("Please enter a subject.")
    elif len(subject) < 6:
        st.error("Subject is too short. Give a useful summary.")
    elif not message:
        st.error("Please enter a message.")
    elif len(message) < 20:
        st.error("Message is too short. Describe the problem clearly enough to reproduce or review it.")
    else:
        ticket_data = {
            "user_email": user_email,
            "issue_type": issue_type,
            "related_question_id": related_question_id,
            "subject": subject,
            "message": message,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            supabase.table("support_tickets").insert(ticket_data).execute()
            st.session_state["support_lookup_email"] = user_email
            st.success("Support ticket submitted.")
            st.info("Status: Open. Check recent tickets below for updates.")
        except Exception as e:
            st.error("Could not save the support ticket.")
            st.write("This usually means the support_tickets table is missing one of these columns:")
            st.code(
                "user_email, issue_type, related_question_id, subject, message, status, created_at",
                language="text",
            )
            with st.expander("Show technical error"):
                st.exception(e)

st.divider()

st.subheader("My Recent Tickets")
email_for_lookup = get_saved_email()

if not email_for_lookup:
    st.info("Enter your email in the support form or log in to see recent tickets here.")
else:
    try:
        result = (
            supabase.table("support_tickets")
            .select("id,user_email,issue_type,subject,status,created_at,related_question_id")
            .eq("user_email", email_for_lookup)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        rows = result.data or []

        if not rows:
            st.info("No support tickets found for this email yet.")
        else:
            st.caption(f"Showing tickets for {email_for_lookup}. Times shown in {user_timezone or 'UTC'}.")
            for row in rows:
                with st.container(border=True):
                    top_left, top_right = st.columns([4, 1])
                    top_left.markdown(f"**#{row.get('id')} — {safe_text(row.get('subject'), 'No subject')}**")
                    top_right.markdown(f"**{status_label(row.get('status'))}**")
                    st.write(
                        f"Type: {safe_text(row.get('issue_type'), 'N/A')} | "
                        f"Created: {format_for_user_timezone(row.get('created_at'), user_timezone)}"
                    )
                    if row.get("related_question_id"):
                        st.caption(f"Question ID: {row.get('related_question_id')}")
    except Exception as e:
        st.warning("Recent tickets could not be loaded yet. Ticket submission may still work.")
        with st.expander("Show technical error"):
            st.exception(e)
