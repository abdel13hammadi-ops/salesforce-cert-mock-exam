import streamlit as st
from supabase import create_client
from datetime import datetime
from utils.access_control import require_admin_access

APP_VERSION = "ADMIN_SUPPORT_TICKETS_V1"

st.set_page_config(
    page_title="Admin Support Tickets",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Hard security gate: only admins can access this page, even by direct URL.
admin_email = require_admin_access("Admin Support Tickets")


st.title("Admin Support Tickets")
st.caption(f"App version: {APP_VERSION}")

def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        st.error("Missing Supabase secrets. Please check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
        st.stop()

    return create_client(url, key)

supabase = get_supabase_client()

def fetch_tickets(status_filter="All", issue_filter="All", search_text=""):
    query = supabase.table("support_tickets").select("*").order("created_at", desc=True)

    if status_filter != "All":
        query = query.eq("status", status_filter)

    if issue_filter != "All":
        query = query.eq("issue_type", issue_filter)

    result = query.execute()
    tickets = result.data or []

    if search_text:
        s = search_text.lower().strip()
        tickets = [
            t for t in tickets
            if s in str(t.get("id", "")).lower()
            or s in str(t.get("user_email", "")).lower()
            or s in str(t.get("issue_type", "")).lower()
            or s in str(t.get("related_question_id", "")).lower()
            or s in str(t.get("question_id", "")).lower()
            or s in str(t.get("subject", "")).lower()
            or s in str(t.get("message", "")).lower()
        ]

    return tickets

def update_ticket(ticket_id, status, admin_notes=None, has_admin_notes_column=False):
    payload = {
        "status": status,
        "updated_at": datetime.utcnow().isoformat()
    }

    if has_admin_notes_column:
        payload["admin_notes"] = admin_notes or ""

    result = (
        supabase.table("support_tickets")
        .update(payload)
        .eq("id", ticket_id)
        .execute()
    )
    return result.data

def safe_text(value, fallback=""):
    if value is None:
        return fallback
    return str(value)

with st.expander("Database setup note", expanded=False):
    st.write("This page works with the existing support_tickets table.")
    st.write("For admin notes, add this optional column in Supabase:")
    st.code(
        "ALTER TABLE support_tickets\n"
        "ADD COLUMN IF NOT EXISTS admin_notes text;\n",
        language="sql"
    )

try:
    all_tickets = fetch_tickets()
except Exception as e:
    st.error("Could not load support tickets.")
    st.exception(e)
    st.stop()

total = len(all_tickets)
open_count = sum(1 for t in all_tickets if t.get("status") == "open")
in_progress_count = sum(1 for t in all_tickets if t.get("status") == "in_progress")
resolved_count = sum(1 for t in all_tickets if t.get("status") == "resolved")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Tickets", total)
c2.metric("Open", open_count)
c3.metric("In Progress", in_progress_count)
c4.metric("Resolved", resolved_count)

st.divider()

left, mid, right = st.columns([1, 1, 2])

with left:
    status_filter = st.selectbox(
        "Filter by status",
        ["All", "open", "in_progress", "resolved", "closed"]
    )

with mid:
    issue_types = sorted({t.get("issue_type") for t in all_tickets if t.get("issue_type")})
    issue_filter = st.selectbox("Filter by issue type", ["All"] + issue_types)

with right:
    search_text = st.text_input(
        "Search tickets",
        placeholder="Search email, subject, question ID, message..."
    )

tickets = fetch_tickets(status_filter, issue_filter, search_text)

st.subheader("Tickets")

if not tickets:
    st.info("No support tickets found for the current filters.")
    st.stop()

has_admin_notes_column = any("admin_notes" in t for t in tickets)

for ticket in tickets:
    ticket_id = ticket.get("id")
    status = ticket.get("status", "open")
    issue_type = ticket.get("issue_type", "N/A")
    subject = ticket.get("subject", "(No subject)")
    email = ticket.get("user_email", "N/A")
    created_at = ticket.get("created_at", "N/A")
    related_question_id = ticket.get("related_question_id") or ticket.get("question_id") or ""

    label = f"#{ticket_id} | {status} | {issue_type} | {subject}"

    with st.expander(label):
        m1, m2, m3 = st.columns(3)
        m1.write(f"**Ticket ID:** {ticket_id}")
        m2.write(f"**Status:** {status}")
        m3.write(f"**Created:** {created_at}")

        st.write(f"**User Email:** {email}")
        st.write(f"**Issue Type:** {issue_type}")

        if related_question_id:
            st.write(f"**Related Question ID:** `{related_question_id}`")

        st.write("**Subject:**")
        st.write(subject)

        st.write("**Message:**")
        st.info(safe_text(ticket.get("message"), "(No message)"))

        st.divider()

        new_status = st.selectbox(
            "Update status",
            ["open", "in_progress", "resolved", "closed"],
            index=["open", "in_progress", "resolved", "closed"].index(status) if status in ["open", "in_progress", "resolved", "closed"] else 0,
            key=f"status_{ticket_id}"
        )

        if has_admin_notes_column:
            admin_notes = st.text_area(
                "Admin notes",
                value=safe_text(ticket.get("admin_notes")),
                key=f"notes_{ticket_id}",
                height=120
            )
        else:
            admin_notes = None
            st.warning("Admin notes column is not added yet. Status updates still work. Add admin_notes column using the SQL note at the top if you want notes.")

        if st.button("Save Ticket Update", key=f"save_{ticket_id}", type="primary"):
            try:
                update_ticket(
                    ticket_id=ticket_id,
                    status=new_status,
                    admin_notes=admin_notes,
                    has_admin_notes_column=has_admin_notes_column
                )
                st.success("Ticket updated ✅")
                st.rerun()
            except Exception as e:
                st.error("Could not update ticket.")
                st.exception(e)
