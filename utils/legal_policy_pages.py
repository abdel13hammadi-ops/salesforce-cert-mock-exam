"""Public legal policy page content and navigation helpers."""

from __future__ import annotations

import streamlit as st

from utils.version import APP_VERSION

TERMS_PAGE = "pages/Terms_of_Service.py"
PRIVACY_PAGE = "pages/Privacy_Policy.py"
REFUND_PAGE = "pages/Refund_and_Cancellation_Policy.py"

LEGAL_BUSINESS_NAME = "CertBound LLC"
SUPPORT_EMAIL = "support@certbound.com"
EFFECTIVE_DATE = "July 1, 2026"
GOVERNING_JURISDICTION = "New Jersey, United States"
TERMS_HEADINGS = (
    "Terms of Service",
    "Acceptance of Terms",
    "Use of the Service",
    "Accounts and Access",
    "Premium Subscription",
    "Disclaimers",
    "Contact",
    "Governing Law",
)

PRIVACY_HEADINGS = (
    "Privacy Policy",
    "Information We Collect",
    "How We Use Information",
    "Sharing and Processors",
    "Data Retention",
    "Your Choices",
    "Contact",
)

REFUND_HEADINGS = (
    "Refund and Cancellation Policy",
    "Premium Subscription Billing",
    "Cancellation Through Stripe",
    "Access After Cancellation",
    "Refunds",
    "Contact",
)


def render_public_policy_page_header(title: str, page_icon: str) -> None:
    st.caption(f"App Version: {APP_VERSION}")
    st.title(f"{page_icon} {title}")
    st.caption(
        f"Operator: {LEGAL_BUSINESS_NAME} · Effective date: {EFFECTIVE_DATE} · "
        f"Contact: {SUPPORT_EMAIL}"
    )


def render_legal_policy_links() -> None:
    """Render links to all public legal policy pages."""
    st.markdown("**Legal policies**")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.page_link(TERMS_PAGE, label="Terms of Service")
    with col2:
        st.page_link(PRIVACY_PAGE, label="Privacy Policy")
    with col3:
        st.page_link(REFUND_PAGE, label="Refund and Cancellation Policy")


def render_terms_content() -> None:
    st.markdown(
        "These Terms of Service govern your use of CertBound, an independent "
        "Salesforce certification exam-preparation platform."
    )
    st.subheader("Acceptance of Terms")
    st.markdown(
        "By creating an account, logging in, or using CertBound, you agree to these "
        "Terms and to our Privacy Policy."
    )
    st.subheader("Use of the Service")
    st.markdown(
        "CertBound provides practice exams, progress tracking, and related study tools. "
        "The platform is not affiliated with, endorsed by, or sponsored by Salesforce."
    )
    st.subheader("Accounts and Access")
    st.markdown(
        "You are responsible for maintaining the confidentiality of your login credentials "
        "and for activity under your account."
    )
    st.subheader("Premium Subscription")
    st.markdown(
        "Premium features may require an active paid subscription. Billing terms, "
        "cancellation, and access after cancellation are described in the "
        "Refund and Cancellation Policy."
    )
    st.subheader("Disclaimers")
    st.markdown(
        "CertBound is provided on an \"as is\" and \"as available\" basis. "
        "Exam outcomes are not guaranteed."
    )
    st.subheader("Contact")
    st.markdown(f"Questions about these Terms may be sent to {SUPPORT_EMAIL}.")
    st.subheader("Governing Law")
    st.markdown(
        f"These Terms are governed by the laws of {GOVERNING_JURISDICTION}, "
        "except where prohibited by applicable law."
    )


def render_privacy_content() -> None:
    st.markdown(
        "This Privacy Policy describes how CertBound handles information when you use "
        "the platform."
    )
    st.subheader("Information We Collect")
    st.markdown(
        "Depending on how you use CertBound, we may process account information such as "
        "your email address, profile preferences, exam and practice activity, support "
        "requests, and subscription billing status processed through Stripe."
    )
    st.subheader("How We Use Information")
    st.markdown(
        "We use this information to operate the service, save your progress, manage "
        "Premium access, provide support, and maintain platform security."
    )
    st.subheader("Sharing and Processors")
    st.markdown(
        "CertBound uses service providers such as Supabase for authentication and data "
        "storage and Stripe for subscription billing. Those providers process data on "
        "our behalf according to their own terms and privacy policies."
    )
    st.subheader("Data Retention")
    st.markdown(
        "We retain account and activity data for as long as needed to provide the service "
        "and meet legal or operational requirements."
    )
    st.subheader("Your Choices")
    st.markdown(
        "You may request account-related assistance by contacting support. Additional "
        "privacy rights may apply based on your location."
    )
    st.subheader("Contact")
    st.markdown(f"Privacy questions may be sent to {SUPPORT_EMAIL}.")


def render_refund_content() -> None:
    st.markdown(
        "This policy describes how CertBound Premium subscriptions, cancellations, "
        "and billing access work today."
    )
    st.subheader("Premium Subscription Billing")
    st.markdown(
        "CertBound Premium is offered as a monthly recurring subscription. "
        "Checkout and renewals are processed by Stripe using the price configured "
        "for this deployment."
    )
    st.subheader("Cancellation Through Stripe")
    st.markdown(
        "Paid subscribers with a mapped Stripe customer can cancel through the Stripe "
        "Customer Portal using **Manage subscription** on the Account page."
    )
    st.subheader("Access After Cancellation")
    st.markdown(
        "If you schedule cancellation at period end, Premium access remains active until "
        "the end of the current paid billing period. After that period ends, Premium "
        "features are no longer available unless you subscribe again."
    )
    st.subheader("Refunds")
    st.markdown(
        "CertBound does not automatically promise refunds for partial billing periods, "
        "unused time, or previously processed subscription charges. Refund requests, "
        "if any, are reviewed case by case at CertBound LLC's discretion."    )
    st.subheader("Contact")
    st.markdown(
        f"Billing questions may be sent to {SUPPORT_EMAIL}. "
        "Please include the account email used for CertBound."
    )
