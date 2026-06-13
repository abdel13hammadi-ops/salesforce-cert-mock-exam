import re

from datetime import datetime, timezone



import streamlit as st



import sys

from pathlib import Path



_file = Path(__file__).resolve()

_root = _file.parent.parent if _file.parent.name == "pages" else _file.parent

if str(_root) not in sys.path:

    sys.path.insert(0, str(_root))



import path_setup



path_setup.ensure_project_root(__file__)



from utils.access_control import render_app_chrome, get_current_user_email, get_supabase_client

APP_VERSION = "SUPPORT_V1_TICKET_SUBMISSION"



st.set_page_config(page_title="Support", layout="wide")

render_app_chrome()





def is_valid_email(email: str) -> bool:

    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))





st.title("Support")

st.caption(f"App version: {APP_VERSION}")



st.markdown(

    """

Use this page to report a question issue, confusing explanation, typo, technical problem, or account/support request.

"""

)



with st.form("support_ticket_form", clear_on_submit=False):

    st.subheader("Submit a Support Ticket")



    user_email = st.text_input(

        "Email",

        value=get_current_user_email() or "",

        placeholder="your@email.com",

        help="Use the same email saved on the Account page."

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

            "Billing question",

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

        placeholder="Describe the problem clearly. If this is about a question, include what you think should be changed.",

    )



    submitted = st.form_submit_button("Submit Ticket", type="primary")



if submitted:

    user_email = user_email.strip().lower()

    subject = subject.strip()

    message = message.strip()

    related_question_id = related_question_id.strip() or None



    if not is_valid_email(user_email):

        st.error("Please enter a valid email address.")

    elif not subject:

        st.error("Please enter a subject.")

    elif not message:

        st.error("Please enter a message.")

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

            get_supabase_client().table("support_tickets").insert(ticket_data).execute()

            st.session_state["user_email"] = user_email

            st.success("Support ticket submitted ✅")

            st.info("We saved this ticket with status: open")

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

email_for_lookup = get_current_user_email()



if not email_for_lookup:

    st.info("Save your email on the Account page first to see your recent tickets here.")

else:

    try:

        result = (

            get_supabase_client().table("support_tickets")

            .select("id,user_email,issue_type,subject,status,created_at,related_question_id")

            .eq("user_email", email_for_lookup)

            .order("created_at", desc=True)

            .limit(10)

            .execute()

        )

        rows = result.data or []



        if not rows:

            st.info("No support tickets found for your saved email yet.")

        else:

            for row in rows:

                st.markdown(f"**{row.get('subject', 'No subject')}**")

                st.write(

                    f"Type: {row.get('issue_type', 'N/A')} | "

                    f"Status: {row.get('status', 'N/A')} | "

                    f"Created: {row.get('created_at', 'N/A')}"

                )

                if row.get("related_question_id"):

                    st.caption(f"Question ID: {row.get('related_question_id')}")

                st.divider()

    except Exception as e:

        st.warning("Recent tickets could not be loaded yet. Ticket submission may still work.")

        with st.expander("Show technical error"):

            st.exception(e)

