"""Reusable CertBound secondary-page presentation components."""

from __future__ import annotations

import html
from typing import Any, Optional, Sequence

import streamlit as st

from utils.ui_theme import COLORS, theme_css

PREMIUM_BENEFITS: Sequence[str] = (
    "Randomized full mock exams",
    "Practice by Category and Weak Areas Practice",
    "Progress tracking and readiness insights",
    "Saved attempt history",
)

SUBSCRIPTION_PRICE_NOTE = (
    "Monthly recurring subscription. The price configured for this deployment "
    "is shown securely on Stripe Checkout."
)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def secondary_css() -> str:
    c = COLORS
    return f"""
    <style>
    .cb-secondary-page .block-container {{
        max-width: 980px;
        padding-top: 2rem !important;
        padding-bottom: 2.5rem !important;
    }}
    .cb-secondary-section {{
        border: 1px solid {c['border']};
        border-radius: 14px;
        background: {c['surface']};
        padding: 1.15rem 1.3rem;
        margin-bottom: 1rem;
        box-shadow: 0 4px 14px rgba(15, 23, 42, 0.05);
    }}
    .cb-secondary-kicker {{
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: {c['accent_muted']};
        margin-bottom: 0.35rem;
    }}
    .cb-secondary-title {{
        font-size: 1.2rem;
        font-weight: 800;
        color: {c['primary_navy']};
        margin: 0 0 0.35rem;
        line-height: 1.2;
    }}
    .cb-secondary-body {{
        color: {c['text_muted']};
        font-size: 0.92rem;
        line-height: 1.5;
        margin: 0;
    }}
    .cb-status-pill {{
        display: inline-block;
        padding: 0.3rem 0.65rem;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 800;
        margin-right: 0.35rem;
    }}
    .cb-status-pill-success {{
        background: {c['success_bg']};
        color: {c['success']};
    }}
    .cb-status-pill-warning {{
        background: {c['warning_bg']};
        color: {c['warning']};
    }}
    .cb-status-pill-danger {{
        background: {c['danger_bg']};
        color: {c['danger']};
    }}
    .cb-status-pill-neutral {{
        background: {c['neutral_bg']};
        color: {c['neutral']};
    }}
    .cb-plan-card {{
        border: 1px solid {c['border']};
        border-radius: 12px;
        background: {c['surface_muted']};
        padding: 0.95rem 1.05rem;
        margin-top: 0.65rem;
    }}
    .cb-plan-benefits {{
        margin: 0.55rem 0 0;
        padding-left: 1.1rem;
        color: {c['text']};
        line-height: 1.55;
        font-size: 0.9rem;
    }}
    .cb-support-ticket {{
        border: 1px solid {c['border']};
        border-radius: 12px;
        background: {c['surface']};
        padding: 0.85rem 1rem;
        margin-bottom: 0.65rem;
    }}
    .cb-support-ticket-title {{
        font-weight: 750;
        color: {c['text']};
        margin-bottom: 0.25rem;
    }}
    .cb-support-ticket-meta {{
        color: {c['text_muted']};
        font-size: 0.84rem;
    }}
    .cb-legal-doc {{
        max-width: 72ch;
        line-height: 1.6;
        color: {c['text']};
    }}
    .cb-legal-doc h1, .cb-legal-doc h2, .cb-legal-doc h3 {{
        color: {c['primary_navy']};
    }}
    .cb-auth-panel {{
        border: 1px solid {c['border']};
        border-radius: 12px;
        background: {c['surface_muted']};
        padding: 0.9rem 1rem;
        margin-top: 0.5rem;
    }}
    a.portal-manage-link {{
        display: inline-block;
        padding: 0.45rem 1rem;
        background-color: {c['primary_navy']};
        color: {c['text_inverse']} !important;
        text-decoration: none;
        border-radius: 0.5rem;
        font-weight: 650;
        line-height: 1.4;
    }}
    a.portal-manage-link:hover {{
        color: {c['text_inverse']} !important;
        opacity: 0.92;
    }}
    @media (max-width: 900px) {{
        .cb-secondary-page .block-container {{
            padding-left: 1rem !important;
            padding-right: 1rem !important;
        }}
    }}
    </style>
    """


def inject_secondary_theme() -> None:
    """Inject secondary-page styling on top of the shared shell theme.

    Shell pages already call inject_shell_theme via render_app_chrome or
    render_public_chrome. Re-applying theme_css here duplicates styles and can
    cause Streamlit to leak trailing HTML closers into code blocks.
    """
    scoped_css = secondary_css().replace(
        ".cb-secondary-page .block-container",
        "section.main div.block-container",
    )
    st.markdown(scoped_css, unsafe_allow_html=True)


def portal_manage_link_css() -> str:
    return secondary_css()


def render_secondary_section(
    *,
    kicker: str,
    title: str,
    body: str = "",
) -> None:
    body_html = f'<p class="cb-secondary-body">{_esc(body)}</p>' if body else ""
    st.markdown(
        f"""
        <div class="cb-secondary-section">
            <div class="cb-secondary-kicker">{_esc(kicker)}</div>
            <div class="cb-secondary-title">{_esc(title)}</div>
            {body_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_access_status_pill(*, has_premium: bool, subscription_status: str = "") -> None:
    if has_premium:
        pill = '<span class="cb-status-pill cb-status-pill-success">Access: Premium</span>'
        detail = _esc(subscription_status or "active")
    else:
        pill = '<span class="cb-status-pill cb-status-pill-neutral">Access: Free</span>'
        detail = _esc(subscription_status or "free")
    st.markdown(
        f'<div>{pill}<span class="cb-secondary-body">Subscription status: <strong>{detail}</strong></span></div>',
        unsafe_allow_html=True,
    )


def render_subscription_plan_summary(
    *,
    has_premium: bool,
    stripe_sub_status: str = "",
    cancel_at_period_end: bool = False,
) -> None:
    plan_label = "CertBound Premium" if has_premium else "CertBound Free"
    status_text = "Active" if has_premium else "Current plan"
    benefits_html = "".join(f"<li>{_esc(item)}</li>" for item in PREMIUM_BENEFITS)
    cancel_html = ""
    if cancel_at_period_end:
        cancel_html = (
            '<p class="cb-secondary-body" style="margin-top:0.55rem;">'
            "Status: Cancellation scheduled — Premium access remains active until the end of the current billing period."
            "</p>"
        )
    stripe_html = ""
    if stripe_sub_status:
        stripe_html = (
            f'<p class="cb-secondary-body" style="margin-top:0.35rem;">'
            f"Billing status: <strong>{_esc(stripe_sub_status)}</strong></p>"
        )
    st.markdown(
        f"""
        <div class="cb-plan-card">
            <div class="cb-secondary-kicker">Subscription</div>
            <div class="cb-secondary-title">{_esc(plan_label)} — {_esc(status_text)}</div>
            <p class="cb-secondary-body">{_esc(SUBSCRIPTION_PRICE_NOTE)}</p>
            {stripe_html}
            {cancel_html}
            <ul class="cb-plan-benefits">{benefits_html}</ul>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_status_message(
    *,
    kind: str,
    title: str,
    body: str = "",
) -> None:
    if kind == "success":
        st.success(f"**{title}**" + (f" — {body}" if body else ""))
    elif kind == "warning":
        st.warning(f"**{title}**" + (f" — {body}" if body else ""))
    elif kind == "error":
        st.error(f"**{title}**" + (f" — {body}" if body else ""))
    else:
        st.info(f"**{title}**" + (f" — {body}" if body else ""))


def render_support_ticket_card(
    *,
    subject: str,
    status: str,
    issue_type: str,
    created_label: str,
    message: str,
    related_question_id: str = "",
) -> None:
    question_html = ""
    if related_question_id:
        question_html = f"<div><strong>Related question ID:</strong> {_esc(related_question_id)}</div>"
    st.markdown(
        f"""
        <div class="cb-support-ticket">
            <div class="cb-support-ticket-title">{_esc(subject)}</div>
            <div class="cb-support-ticket-meta">Status: {_esc(status)} · Issue: {_esc(issue_type)} · Submitted: {_esc(created_label)}</div>
            {question_html}
            <p style="margin-top:0.55rem;color:{COLORS['text']};line-height:1.5;">{_esc(message)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_support_empty_state() -> None:
    st.markdown(
        f"""
        <div class="cb-secondary-section">
            <div class="cb-secondary-kicker">Support history</div>
            <div class="cb-secondary-title">No tickets yet</div>
            <p class="cb-secondary-body">Submit a ticket above when you need help with questions, explanations, access, or technical issues.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_legal_document_start() -> None:
    st.markdown('<div class="cb-legal-doc">', unsafe_allow_html=True)


def render_legal_document_end() -> None:
    st.markdown('<span class="cb-legal-doc-end" aria-hidden="true"></span></div>', unsafe_allow_html=True)


def render_password_reset_header() -> None:
    render_secondary_section(
        kicker="Account security",
        title="Reset your password",
        body="Use this page after clicking the secure password reset link sent by email.",
    )


def render_auth_panel_start() -> None:
    st.markdown('<div class="cb-auth-panel">', unsafe_allow_html=True)


def render_auth_panel_end() -> None:
    # Bare closing tags are interpreted by Streamlit as code blocks; prefix with
    # a hidden marker so the auth-panel wrapper closes without leaking markup.
    st.markdown('<span class="cb-auth-panel-end" aria-hidden="true"></span></div>', unsafe_allow_html=True)


def format_placeholder(value: Optional[Any], fallback: str = "—") -> str:
    text = str(value or "").strip()
    return text if text else fallback
