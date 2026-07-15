"""Reusable CertBound dashboard presentation components."""

from __future__ import annotations

import html
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

import streamlit as st

from utils.dashboard_charts import (
    build_domain_mastery_figure,
    build_score_trend_figure,
    build_study_activity_figure,
    chart_summary_domain_mastery,
    chart_summary_score_trend,
    chart_summary_study_activity,
)
from utils.learner_analytics import VerifiedMockPerformance
from utils.ui_theme import COLORS, theme_css


def inject_certbound_theme() -> None:
    """Inject shared CertBound dashboard CSS once per page render."""
    st.markdown(theme_css(), unsafe_allow_html=True)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def format_score_value(value: Optional[float], *, suffix: str = "%") -> str:
    if value is None:
        return "—"
    return f"{value:.1f}{suffix}" if isinstance(value, float) else f"{value}{suffix}"


def format_trend_change(change: Optional[float]) -> Dict[str, str]:
    if change is None:
        return {"class": "cb-trend-flat", "text": "No prior verified mock", "arrow": "→"}
    if change > 0:
        return {"class": "cb-trend-up", "text": f"Up {change:.1f} pts vs previous", "arrow": "↑"}
    if change < 0:
        return {"class": "cb-trend-down", "text": f"Down {abs(change):.1f} pts vs previous", "arrow": "↓"}
    return {"class": "cb-trend-flat", "text": "Unchanged vs previous", "arrow": "→"}


def status_badge_class(status: str) -> str:
    mapping = {
        "high_risk": "cb-badge-danger",
        "below_target": "cb-badge-warning",
        "on_target": "cb-badge-neutral",
        "strong": "cb-badge-success",
        "locked": "cb-badge-locked",
        "insufficient": "cb-badge-neutral",
    }
    return mapping.get(status, "cb-badge-neutral")


def status_label_text(status: str) -> str:
    mapping = {
        "high_risk": "High risk",
        "below_target": "Below target",
        "on_target": "On target",
        "strong": "Strong",
        "insufficient": "Insufficient evidence",
    }
    return mapping.get(status, status.replace("_", " ").title())


def build_practice_href(page_path: str, exam_name: str, category: str, session_token: str = "") -> str:
    route = "/Practice_By_Category" if "Practice_By_Category" in page_path else page_path
    params = {"exam_name": exam_name, "category": category}
    if session_token:
        params["fr_session"] = session_token
    query = "&".join(f"{quote(str(k))}={quote(str(v))}" for k, v in params.items())
    return f"{route}?{query}"


def build_mock_exam_href(session_token: str = "") -> str:
    return f"/?{quote('fr_session')}={quote(session_token)}" if session_token else "/"


def render_empty_state(
    title: str,
    body: str,
    *,
    action_label: Optional[str] = None,
    action_href: Optional[str] = None,
) -> None:
    action_html = ""
    if action_label and action_href:
        action_html = f'<a class="cb-action-link" href="{_esc(action_href)}" target="_self">{_esc(action_label)}</a>'
    st.markdown(
        f"""
        <div class="cb-empty-state">
            <div class="cb-card-heading" style="font-size:1.05rem;">{_esc(title)}</div>
            <p class="cb-body">{_esc(body)}</p>
            {action_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_cert_context_header(
    *,
    title: str,
    subtitle: str,
    passing_score: Optional[float] = None,
    access_label: str = "",
) -> None:
    passing_html = (
        f'<span class="cb-badge cb-badge-neutral">Passing score {passing_score:.0f}%</span>'
        if passing_score is not None
        else ""
    )
    access_html = f'<span class="cb-badge cb-badge-neutral">{_esc(access_label)}</span>' if access_label else ""
    st.markdown(
        f"""
        <div class="cb-card cb-card-muted">
            <div class="cb-card-title">Certification focus</div>
            <div class="cb-card-heading">{_esc(title)}</div>
            <p class="cb-body">{_esc(subtitle)}</p>
            <div style="display:flex;gap:0.5rem;flex-wrap:wrap;margin-top:0.65rem;">{passing_html}{access_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _readiness_gauge_svg(score: float) -> str:
    clamped = max(0.0, min(100.0, float(score)))
    angle = 180 * (clamped / 100.0)
    return f"""
    <svg viewBox="0 0 180 110" width="100%" aria-hidden="true">
      <path d="M20 95 A70 70 0 0 1 160 95" fill="none" stroke="#E2E8F0" stroke-width="12" stroke-linecap="round"/>
      <path d="M20 95 A70 70 0 0 1 160 95" fill="none" stroke="{COLORS['accent']}" stroke-width="12"
            stroke-linecap="round" pathLength="100" stroke-dasharray="{angle} 100"/>
    </svg>
    """


def render_readiness_hero(
    readiness_display: Any,
    readiness_raw: Mapping[str, Any],
    *,
    variant: str = "compact",
    mock_exam_href: str = "/",
) -> None:
    """Render unlocked or locked readiness hero from ReadinessDisplayContract."""
    if readiness_display.is_locked:
        completed = readiness_display.completed_verified_mock_count
        required = readiness_display.required_mock_count
        remaining = readiness_display.remaining_mock_count
        pct = int(round((completed / required) * 100)) if required else 0
        segments = "".join(
            f'<div style="flex:1;height:8px;border-radius:999px;background:{"#1E3A5F" if i < completed else "#E2E8F0"};"></div>'
            for i in range(required)
        )
        st.markdown(
            f"""
            <div class="cb-card">
                <div class="cb-card-title">Readiness</div>
                <div class="cb-card-heading">Readiness locked</div>
                <p class="cb-body">
                    Readiness calculation unlocks after {required} verified mock exams.
                    Progress: {completed} of {required} completed.
                </p>
                <div style="display:flex;gap:0.35rem;">{segments}</div>
                <p class="cb-caption">{remaining} verified mock{'s' if remaining != 1 else ''} remaining before unlock.</p>
                <a class="cb-action-link" href="{_esc(mock_exam_href)}" target="_self">Start a verified mock exam</a>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    score = readiness_display.readiness_score
    score_text = format_score_value(score)
    trend = _esc(readiness_display.score_trend_indicator)
    confidence = _esc(readiness_display.confidence_label)
    label = _esc(readiness_display.readiness_label)
    evidence = _safe_int(readiness_raw.get("unique_questions_seen"), 0)
    mocks = readiness_display.completed_verified_mock_count
    recommendation = _esc(readiness_display.recommended_next_action)
    gauge = _readiness_gauge_svg(score or 0.0)

    if variant == "detailed":
        st.markdown(
            f"""
            <div class="cb-card">
                <div class="cb-readiness-hero">
                    <div class="cb-gauge-wrap">{gauge}
                        <div class="cb-gauge-score"><div class="value">{score_text}</div><div class="label">Readiness</div></div>
                    </div>
                    <div>
                        <div class="cb-card-title">Overall readiness</div>
                        <div class="cb-card-heading">{label}</div>
                        <p class="cb-body">Confidence: <strong>{confidence}</strong> · Trend: <strong>{trend}</strong></p>
                        <p class="cb-caption">Verified mocks completed: {mocks} · Unique questions seen: {evidence}</p>
                        <p class="cb-body" style="margin-top:0.75rem;">{recommendation}</p>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""
            <div class="cb-card">
                <div class="cb-readiness-hero">
                    <div class="cb-gauge-wrap">{gauge}
                        <div class="cb-gauge-score"><div class="value">{score_text}</div><div class="label">Readiness</div></div>
                    </div>
                    <div>
                        <div class="cb-card-title">Readiness</div>
                        <div class="cb-card-heading">{label}</div>
                        <p class="cb-body">{confidence} confidence · {trend} trend · {mocks} verified mocks</p>
                        <p class="cb-caption">{recommendation}</p>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def render_verified_kpi_row(
    performance: VerifiedMockPerformance,
    *,
    all_activity_average: Optional[float] = None,
) -> None:
    trend = format_trend_change(performance.score_change)
    cards = [
        ("Latest verified score", format_score_value(performance.latest_score), "Most recent verified full paid mock"),
        ("Verified average", format_score_value(performance.average_score), "Average across verified mocks only"),
        ("Best verified score", format_score_value(performance.best_score), "Highest verified mock result"),
        ("Verified mocks completed", str(performance.attempt_count) if performance.has_verified_mocks else "—", "Full paid mocks with question-level evidence"),
        ("Change vs previous", trend["text"], "Compared to the prior verified mock"),
    ]
    card_html = []
    for label, value, caption in cards:
        extra_class = trend["class"] if label == "Change vs previous" else ""
        card_html.append(
            f"""
            <div class="cb-kpi-card">
                <div class="cb-kpi-label">{_esc(label)}</div>
                <div class="cb-kpi-value {extra_class}">{_esc(value)}</div>
                <div class="cb-kpi-sub">{_esc(caption)}</div>
            </div>
            """
        )
    st.markdown(f'<div class="cb-kpi-grid">{"".join(card_html)}</div>', unsafe_allow_html=True)
    if all_activity_average is not None:
        st.markdown(
            f'<p class="cb-caption">All-activity average score (separate metric): <strong>{all_activity_average:.1f}%</strong></p>',
            unsafe_allow_html=True,
        )


def render_plotly_chart(fig: Any, *, caption: str = "", key: Optional[str] = None) -> None:
    if fig is None:
        return
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=key)
    if caption:
        st.caption(caption)


def render_score_trend_section(
    performance: VerifiedMockPerformance,
    *,
    compact: bool = False,
    chart_key: Optional[str] = None,
) -> None:
    if not performance.score_series:
        render_empty_state(
            "No verified score trend yet",
            "Complete verified full paid mock exams to chart your score progression over time.",
        )
        return
    fig = build_score_trend_figure(
        performance.score_series,
        passing_threshold=performance.passing_threshold,
        average_score=performance.average_score if performance.attempt_count > 1 else None,
        compact=compact,
    )
    summary = chart_summary_score_trend(performance.score_series, performance.passing_threshold)
    render_plotly_chart(fig, caption=summary, key=chart_key)


def render_domain_mastery_section(
    domain_rows: Sequence[Mapping[str, Any]],
    *,
    compact: bool = False,
    limit: Optional[int] = None,
    chart_key: Optional[str] = None,
) -> None:
    if not domain_rows:
        render_empty_state(
            "No verified domain evidence",
            "Complete a verified full paid mock with saved question-level results to see domain mastery.",
        )
        return
    fig = build_domain_mastery_figure(domain_rows, compact=compact, limit=limit)
    summary = chart_summary_domain_mastery(domain_rows)
    render_plotly_chart(fig, caption=summary, key=chart_key)


def render_study_activity_section(
    summary: Any,
    *,
    compact: bool = False,
    chart_key: Optional[str] = None,
) -> None:
    if summary is None or getattr(summary, "total_completed_activities", 0) == 0:
        render_empty_state(
            "No recent study activity",
            "Your completed mocks and practice sessions will appear here once activity is recorded.",
        )
        return

    stats = [
        ("Active study days", getattr(summary, "active_study_days", 0)),
        ("Total activities", getattr(summary, "total_completed_activities", 0)),
        ("Verified mocks", getattr(summary, "completed_verified_mocks", 0)),
        ("Practice sessions", getattr(summary, "completed_practice_sessions", 0)),
        ("Weak-area sessions", getattr(summary, "completed_weak_area_sessions", 0)),
        ("Daily sprints", getattr(summary, "completed_daily_sprints", 0)),
        ("Free mocks", getattr(summary, "completed_free_mocks", 0)),
        ("Current streak", getattr(summary, "current_streak_days", 0)),
    ]
    stat_html = "".join(
        f'<div class="cb-kpi-card"><div class="cb-kpi-label">{_esc(label)}</div><div class="cb-kpi-value">{_esc(value)}</div></div>'
        for label, value in stats
    )
    st.markdown(f'<div class="cb-kpi-grid">{stat_html}</div>', unsafe_allow_html=True)
    fig = build_study_activity_figure(
        getattr(summary, "daily_counts", ()),
        window_days=getattr(summary, "window_days", 30),
        compact=compact,
    )
    render_plotly_chart(fig, caption=chart_summary_study_activity(summary), key=chart_key)


def render_weak_area_action_panel(
    domain_row: Optional[Mapping[str, Any]],
    *,
    exam_name: str,
    session_token: str = "",
    compact: bool = False,
) -> None:
    if not domain_row:
        render_empty_state(
            "Build verified domain evidence",
            "Complete a verified full paid mock exam to identify your highest-priority weak domain.",
            action_label="Start mock exam",
            action_href=build_mock_exam_href(session_token),
        )
        return

    domain = str(domain_row.get("Domain") or "Unknown domain")
    accuracy = domain_row.get("Accuracy %")
    weight = domain_row.get("exam_weight", 0)
    attempts_counted = domain_row.get("attempts_counted", domain_row.get("Total", 0))
    sufficient = bool(domain_row.get("has_sufficient_evidence", True))
    status = "insufficient" if not sufficient else str(domain_row.get("status") or "below_target")
    badge_class = status_badge_class(status)
    status_text = status_label_text(status)
    action_href = build_practice_href("pages/Practice_By_Category.py", exam_name, domain, session_token)
    accuracy_text = "Insufficient evidence" if not sufficient else f"{accuracy}% verified accuracy"

    st.markdown(
        f"""
        <div class="cb-card">
            <div class="cb-card-title">Priority weak area</div>
            <div class="cb-card-heading">{_esc(domain)}</div>
            <div style="margin-bottom:0.55rem;"><span class="cb-badge {badge_class}">{_esc(status_text)}</span></div>
            <p class="cb-body">{_esc(accuracy_text)} · Exam weight {weight}% · {attempts_counted} attempts counted</p>
            <p class="cb-caption">Recommended action: complete a targeted practice session in this domain.</p>
            <a class="cb-action-link" href="{_esc(action_href)}" target="_self">Practice this domain</a>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _activity_badge(activity_type: str) -> str:
    mapping = {
        "paid_mock_exam": ("Verified mock", "cb-badge-success"),
        "free_mock_exam": ("Free mock", "cb-badge-neutral"),
        "practice_by_category": ("Practice", "cb-badge-neutral"),
        "weak_areas_practice": ("Weak areas", "cb-badge-warning"),
        "daily_sprint": ("Daily sprint", "cb-badge-neutral"),
    }
    return mapping.get(activity_type, ("Activity", "cb-badge-neutral"))


def render_activity_history(
    history_rows: Sequence[Mapping[str, Any]],
    *,
    compact: bool = False,
    limit: Optional[int] = None,
) -> None:
    rows = list(history_rows or [])
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        render_empty_state(
            "No activity history yet",
            "Your completed mocks and practice sessions will appear here after your first attempt.",
        )
        return

    cards = []
    for row in rows:
        activity_type = str(row.get("activity_type") or "unknown_activity")
        badge_label, badge_class = _activity_badge(activity_type)
        readiness_flag = "Readiness-eligible" if row.get("readiness_eligible") else "Not readiness-eligible"
        score = row.get("Score %")
        score_text = format_score_value(float(score)) if score is not None else "—"
        cards.append(
            f"""
            <div class="cb-activity-row">
                <div class="cb-activity-top">
                    <strong>{_esc(row.get('Mode') or row.get('activity_type'))}</strong>
                    <span class="cb-badge {badge_class}">{_esc(badge_label)}</span>
                </div>
                <div class="cb-activity-meta">
                    Completed {_esc(row.get('Completed At', ''))} · Score {score_text} ·
                    {readiness_flag} · Questions { _esc(row.get('Total', '—')) }
                </div>
            </div>
            """
        )
    st.markdown(f'<div class="cb-activity-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def render_data_failure_state() -> None:
    render_empty_state(
        "Progress data unavailable",
        "We could not load your latest progress right now. Please refresh the page or try again shortly.",
    )
