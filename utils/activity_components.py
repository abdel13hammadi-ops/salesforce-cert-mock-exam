"""Reusable CertBound learner activity presentation components."""

from __future__ import annotations

import html
from typing import Any, Dict, List, Mapping, Optional, Sequence

import streamlit as st

from utils.ui_theme import COLORS, theme_css


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def activity_css() -> str:
    """Activity-specific CSS layered on top of shared CertBound theme tokens."""
    c = COLORS
    return f"""
    <style>
    .cb-activity-page .block-container {{
        max-width: 1180px;
        padding-top: 2rem !important;
        padding-bottom: 2.5rem !important;
    }}
    .cb-activity-banner {{
        background: {c['primary_navy']};
        color: {c['text_inverse']};
        padding: 1.1rem 1.35rem;
        border-radius: 14px 14px 0 0;
        font-size: 1.55rem;
        font-weight: 800;
        line-height: 1.2;
        margin-top: 0.5rem;
    }}
    .cb-activity-subbanner {{
        background: {c['surface_muted']};
        border: 1px solid {c['border']};
        border-top: none;
        padding: 0.75rem 1.35rem;
        border-radius: 0 0 14px 14px;
        margin-bottom: 1.25rem;
        color: {c['primary_navy']};
        font-size: 0.92rem;
    }}
    .cb-activity-launch {{
        border: 1px solid {c['border']};
        border-radius: 14px;
        background: {c['surface']};
        padding: 1.2rem 1.35rem;
        margin-bottom: 1rem;
        box-shadow: 0 4px 14px rgba(15, 23, 42, 0.05);
    }}
    .cb-activity-launch-kicker {{
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: {c['accent_muted']};
        margin-bottom: 0.35rem;
    }}
    .cb-activity-launch-title {{
        font-size: 1.45rem;
        font-weight: 800;
        color: {c['primary_navy']};
        margin: 0 0 0.35rem;
        line-height: 1.15;
    }}
    .cb-activity-launch-body {{
        color: {c['text_muted']};
        font-size: 0.92rem;
        margin: 0;
        line-height: 1.5;
        max-width: 70ch;
    }}
    .cb-activity-status {{
        background: {c['surface_muted']};
        border: 1px solid {c['border']};
        border-radius: 12px;
        padding: 0.75rem 1rem;
        margin-bottom: 0.85rem;
        font-size: 0.9rem;
        color: {c['text']};
    }}
    .cb-question-card {{
        border: 1px solid {c['border']};
        border-radius: 14px;
        padding: 1.35rem 1.4rem;
        background: {c['surface']};
        margin: 0.65rem 0 1rem;
        box-shadow: 0 4px 14px rgba(15, 23, 42, 0.05);
        max-width: 100%;
        overflow-wrap: anywhere;
    }}
    .cb-question-meta {{
        color: {c['accent_muted']};
        font-size: 0.82rem;
        font-weight: 700;
        margin-bottom: 0.65rem;
    }}
    .cb-question-stem {{
        font-size: 1.08rem;
        line-height: 1.55;
        color: {c['text']};
        font-weight: 650;
        margin: 0 0 0.75rem;
        max-width: 72ch;
    }}
    .cb-activity-timer {{
        position: fixed;
        top: 68px;
        right: 30px;
        z-index: 1001;
        min-width: 170px;
        background: {c['warning_bg']};
        border: 1px solid #e0b84f;
        border-radius: 12px;
        padding: 0.65rem 0.9rem;
        box-shadow: 0 6px 20px rgba(15, 23, 42, 0.16);
        text-align: center;
    }}
    .cb-activity-timer-label {{
        font-size: 0.72rem;
        font-weight: 800;
        color: #5f4b00;
        text-transform: uppercase;
        letter-spacing: 0.06em;
    }}
    .cb-activity-timer-value {{
        font-size: 1.65rem;
        font-weight: 900;
        color: {c['text']};
        line-height: 1.1;
    }}
    .cb-navigator-title {{
        font-weight: 800;
        font-size: 1rem;
        color: {c['text']};
        margin: 0.35rem 0 0.45rem;
    }}
    .cb-navigator-help {{
        color: {c['text_muted']};
        font-size: 0.82rem;
        margin-bottom: 0.5rem;
    }}
    .cb-review-warning {{
        border: 1px solid {c['warning']};
        background: {c['warning_bg']};
        color: {c['text']};
        border-radius: 12px;
        padding: 0.85rem 1rem;
        margin: 0.75rem 0;
        font-size: 0.9rem;
    }}
    .cb-result-hero {{
        border: 1px solid {c['border']};
        border-radius: 14px;
        padding: 1.2rem 1.35rem;
        background: {c['surface']};
        margin-bottom: 1rem;
    }}
    .cb-result-status-pass {{
        display: inline-block;
        padding: 0.35rem 0.7rem;
        border-radius: 999px;
        background: {c['success_bg']};
        color: {c['success']};
        font-weight: 800;
        font-size: 0.88rem;
    }}
    .cb-result-status-fail {{
        display: inline-block;
        padding: 0.35rem 0.7rem;
        border-radius: 999px;
        background: {c['danger_bg']};
        color: {c['danger']};
        font-weight: 800;
        font-size: 0.88rem;
    }}
    .cb-feedback-panel {{
        border: 1px solid {c['border']};
        border-radius: 12px;
        padding: 0.95rem 1.05rem;
        margin-top: 0.85rem;
        background: {c['surface_muted']};
    }}
    .cb-feedback-correct {{
        color: {c['success']};
        font-weight: 800;
        margin-bottom: 0.35rem;
    }}
    .cb-feedback-incorrect {{
        color: {c['danger']};
        font-weight: 800;
        margin-bottom: 0.35rem;
    }}
    .cb-locked-preview {{
        border: 1px solid {c['border_strong']};
        border-radius: 14px;
        padding: 1.2rem 1.35rem;
        background: {c['locked_bg']};
        margin-bottom: 1rem;
    }}
    .cb-locked-eyebrow {{
        color: {c['accent_muted']};
        font-size: 0.72rem;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 0.45rem;
    }}
    .cb-locked-pill {{
        display: inline-block;
        margin-top: 0.75rem;
        padding: 0.35rem 0.7rem;
        border-radius: 999px;
        background: {c['neutral_bg']};
        color: {c['locked']};
        font-size: 0.75rem;
        font-weight: 700;
    }}
    .cb-save-status-saved {{
        color: {c['success']};
        font-weight: 700;
    }}
    .cb-save-status-failed {{
        color: {c['danger']};
        font-weight: 700;
    }}
    .cb-activity-empty {{
        border: 1px dashed {c['border_strong']};
        border-radius: 12px;
        padding: 1rem 1.1rem;
        color: {c['text_muted']};
        background: {c['surface_muted']};
    }}
    div[data-testid="stSidebar"] div.stButton > button {{
        width: 100%;
        border-radius: 8px;
        font-weight: 650;
        font-size: 0.86rem;
    }}
    div.stButton > button {{
        border-radius: 8px;
        font-weight: 650;
    }}
    @media (max-width: 900px) {{
        .cb-activity-page .block-container {{
            padding-left: 1rem !important;
            padding-right: 1rem !important;
        }}
        .cb-activity-timer {{
            position: sticky;
            top: 0;
            right: auto;
            width: 100%;
            margin-bottom: 0.75rem;
            min-width: 0;
        }}
    }}
    </style>
    """


def inject_activity_theme() -> None:
    """Inject shared dashboard + activity styling."""
    st.markdown(theme_css() + activity_css(), unsafe_allow_html=True)
    st.markdown('<div class="cb-activity-page cb-shell">', unsafe_allow_html=True)


def render_activity_header(
    activity_label: str,
    *,
    certification: str = "",
    subtitle: str = "",
) -> None:
    cert_html = f" | {_esc(certification)}" if certification else ""
    sub_html = f'<div class="cb-activity-subbanner">{_esc(subtitle)}</div>' if subtitle else ""
    st.markdown(
        f"""
        <div class="cb-activity-banner">{_esc(activity_label)}{cert_html}</div>
        {sub_html}
        """,
        unsafe_allow_html=True,
    )


def render_activity_launch_card(
    *,
    kicker: str,
    title: str,
    body: str,
    access_label: str = "",
    metrics: Optional[Sequence[tuple[str, str]]] = None,
) -> None:
    access_html = (
        f'<span class="cb-badge cb-badge-neutral" style="margin-left:0.5rem;">{_esc(access_label)}</span>'
        if access_label
        else ""
    )
    st.markdown(
        f"""
        <div class="cb-activity-launch">
            <div class="cb-activity-launch-kicker">{_esc(kicker)}</div>
            <div class="cb-activity-launch-title">{_esc(title)}{access_html}</div>
            <p class="cb-activity-launch-body">{_esc(body)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if metrics:
        cols = st.columns(len(metrics))
        for col, (label, value) in zip(cols, metrics):
            col.metric(label, value)


def render_activity_progress(
    current: int,
    total: int,
    *,
    answered: Optional[int] = None,
    marked: Optional[int] = None,
    timer_label: str = "",
) -> None:
    current = max(0, int(current))
    total = max(1, int(total))
    parts = [f"<strong>Question:</strong> {current} of {total}"]
    if answered is not None:
        parts.append(f"<strong>Answered:</strong> {answered}")
    if marked is not None:
        parts.append(f"<strong>Marked:</strong> {marked}")
    if timer_label:
        parts.append(f"<strong>Time:</strong> {_esc(timer_label)}")
    st.markdown(
        f'<div class="cb-activity-status">{" &nbsp;|&nbsp; ".join(parts)}</div>',
        unsafe_allow_html=True,
    )
    st.progress(min(1.0, current / total))


def render_exam_timer(minutes: int, seconds: int) -> None:
    st.markdown(
        f"""
        <div class="cb-activity-timer">
            <div class="cb-activity-timer-label">Time Remaining</div>
            <div class="cb-activity-timer-value">{int(minutes):02d}:{int(seconds):02d}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_navigator_help() -> None:
    st.markdown(
        """
        <div class="cb-navigator-title">Question Navigator</div>
        <div class="cb-navigator-help">✓ answered &nbsp;&nbsp; 🚩 marked for review</div>
        """,
        unsafe_allow_html=True,
    )


def render_question_card_start(
    *,
    domain: str = "",
    difficulty: str = "",
    question_number: Optional[int] = None,
    question_total: Optional[int] = None,
    certification: str = "",
) -> None:
    meta_parts: List[str] = []
    if question_number is not None and question_total is not None:
        meta_parts.append(f"Question {question_number} of {question_total}")
    if certification:
        meta_parts.append(f"Certification: {certification}")
    if domain:
        meta_parts.append(f"Domain: {domain}")
    if difficulty:
        meta_parts.append(f"Difficulty: {difficulty}")
    meta_html = " &nbsp;|&nbsp; ".join(_esc(part) for part in meta_parts)
    meta_block = f'<div class="cb-question-meta">{meta_html}</div>' if meta_html else ""
    st.markdown(f'<div class="cb-question-card">{meta_block}', unsafe_allow_html=True)


def render_question_stem(question_text: str) -> None:
    st.markdown(f'<div class="cb-question-stem">{_esc(question_text)}</div>', unsafe_allow_html=True)


def render_question_card_end() -> None:
    st.markdown("</div>", unsafe_allow_html=True)


def render_answer_guidance(*, multiple_select: bool, required_count: Optional[int] = None) -> None:
    if multiple_select and required_count:
        st.caption(f"Select {required_count} answers.")
    elif multiple_select:
        st.caption("Select all answers that apply.")
    else:
        st.caption("Choose one answer.")


def render_review_summary(
    *,
    answered: int,
    unanswered: int,
    marked: int,
    remaining_time: str = "",
    warning_message: str = "",
) -> None:
    cols = st.columns(4 if remaining_time else 3)
    cols[0].metric("Answered", answered)
    cols[1].metric("Unanswered", unanswered)
    cols[2].metric("Marked for review", marked)
    if remaining_time:
        cols[3].metric("Time remaining", remaining_time)
    if warning_message:
        st.markdown(
            f'<div class="cb-review-warning">{_esc(warning_message)}</div>',
            unsafe_allow_html=True,
        )


def render_result_hero(
    *,
    title: str,
    score: Optional[float],
    correct: Optional[int],
    total: Optional[int],
    passing_score: Optional[float] = None,
    passed: Optional[bool] = None,
) -> None:
    score_text = "—" if score is None else f"{score:.1f}%"
    correct_text = "—" if correct is None or total is None else f"{correct} / {total}"
    passing_text = "—" if passing_score is None else f"{passing_score:.0f}%"
    status_html = ""
    if passed is True:
        status_html = '<span class="cb-result-status-pass">Status: Pass</span>'
    elif passed is False:
        status_html = '<span class="cb-result-status-fail">Status: Fail</span>'
    st.markdown(
        f"""
        <div class="cb-result-hero">
            <div class="cb-card-heading" style="margin-bottom:0.5rem;">{_esc(title)}</div>
            <div style="display:flex;gap:1rem;flex-wrap:wrap;align-items:center;">
                <div><strong>Score:</strong> {_esc(score_text)}</div>
                <div><strong>Correct:</strong> {_esc(correct_text)}</div>
                <div><strong>Passing threshold:</strong> {_esc(passing_text)}</div>
                {status_html}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_save_status(*, state: str, message: str = "") -> None:
    if state == "saved":
        st.markdown(
            f'<p class="cb-save-status-saved">Saved to progress tracking.</p>',
            unsafe_allow_html=True,
        )
        st.success(message or "Attempt saved to progress tracking ✅")
    elif state == "failed":
        st.markdown(
            f'<p class="cb-save-status-failed">Save failed — retry required.</p>',
            unsafe_allow_html=True,
        )
        st.warning(message or "Your result could not be saved. Use Retry Saving Result below.")
    elif state == "saving":
        st.info("Saving your result…")


def render_feedback_panel(
    *,
    is_correct_answer: bool,
    learner_answer: str,
    correct_answer: str,
    explanation: str = "",
) -> None:
    status_class = "cb-feedback-correct" if is_correct_answer else "cb-feedback-incorrect"
    status_text = "Correct" if is_correct_answer else "Incorrect"
    explanation_html = (
        f'<p style="margin-top:0.65rem;color:var(--cb-text-muted);">{_esc(explanation)}</p>'
        if explanation
        else ""
    )
    st.markdown(
        f"""
        <div class="cb-feedback-panel">
            <div class="{status_class}">Result: {status_text}</div>
            <div><strong>Your answer:</strong> {_esc(learner_answer or "No answer selected")}</div>
            <div><strong>Correct answer:</strong> {_esc(correct_answer)}</div>
            {explanation_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_locked_preview_panel(
    *,
    eyebrow: str,
    title: str,
    body: str,
    sample_label: str = "Locked preview",
) -> None:
    st.markdown(
        f"""
        <div class="cb-locked-preview">
            <div class="cb-locked-eyebrow">{_esc(eyebrow)}</div>
            <div class="cb-card-heading" style="margin-bottom:0.45rem;">{_esc(title)}</div>
            <p class="cb-body">{_esc(body)}</p>
            <span class="cb-locked-pill">{_esc(sample_label)} — sample data only</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_activity_empty_state(title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="cb-activity-empty">
            <div style="font-weight:700;margin-bottom:0.35rem;">{_esc(title)}</div>
            <div>{_esc(body)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def format_breakdown_rows(
    breakdown: Mapping[str, Mapping[str, Any]],
    *,
    order: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    keys = list(order) if order else list(breakdown.keys())
    for key in keys:
        data = breakdown.get(key) or {}
        total = int(data.get("total") or 0)
        if total <= 0:
            continue
        correct = int(data.get("correct") or 0)
        percent = float(data.get("percent")) if data.get("percent") is not None else round((correct / total) * 100, 2)
        rows.append({"label": str(key), "correct": correct, "total": total, "percent": percent})
    return rows
