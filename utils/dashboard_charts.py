"""Chart construction for CertBound learner dashboards.

Pure Plotly figure builders — no UI framework, database access, or analytics
recalculation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from utils.ui_theme import CHART_THEME

try:
    import plotly.graph_objects as go
except ImportError:  # pragma: no cover - dependency guarded in tests
    go = None  # type: ignore[assignment]


def _require_plotly():
    if go is None:
        raise ImportError("plotly is required for dashboard charts")
    return go


def _base_layout(title: str = "") -> Dict[str, Any]:
    theme = CHART_THEME
    layout: Dict[str, Any] = {
        "font": {"family": theme.font_family, "size": theme.font_size, "color": theme.axis_color},
        "paper_bgcolor": theme.paper_background,
        "plot_bgcolor": theme.background,
        "margin": {"l": 24, "r": 16, "t": 40 if title else 16, "b": 36},
        "hovermode": "x unified",
        "autosize": True,
    }
    if title:
        layout["title"] = {"text": title, "font": {"size": theme.title_size, "color": "#0F172A"}, "x": 0}
    return layout


def build_score_trend_figure(
    score_series: Sequence[Any],
    *,
    passing_threshold: Optional[float] = None,
    average_score: Optional[float] = None,
    compact: bool = False,
) -> Optional[Any]:
    """Build verified mock score trend line chart from ScoreTrendPoint series."""
    plotly_go = _require_plotly()
    if not score_series:
        return None

    x_labels: List[str] = []
    scores: List[float] = []
    hover_text: List[str] = []
    for point in score_series:
        completed = getattr(point, "completed_at", None)
        if isinstance(completed, datetime):
            date_label = completed.strftime("%b %d, %Y")
        else:
            date_label = str(completed or "")
        sequence = getattr(point, "sequence_number", len(scores) + 1)
        score = float(getattr(point, "score", 0.0) or 0.0)
        threshold = getattr(point, "passing_threshold", passing_threshold)
        x_labels.append(f"Mock {sequence}")
        scores.append(score)
        hover_text.append(
            f"Attempt {sequence}<br>Completed: {date_label}<br>Score: {score:.1f}%"
            + (f"<br>Passing threshold: {float(threshold):.0f}%" if threshold is not None else "")
        )

    fig = plotly_go.Figure()
    fig.add_trace(
        plotly_go.Scatter(
            x=x_labels,
            y=scores,
            mode="lines+markers",
            name="Verified mock score",
            line={"color": CHART_THEME.line_color, "width": 2.5},
            marker={"color": CHART_THEME.marker_color, "size": 8},
            text=hover_text,
            hoverinfo="text",
        )
    )

    if passing_threshold is not None:
        fig.add_hline(
            y=float(passing_threshold),
            line_dash="dash",
            line_color=CHART_THEME.threshold_color,
            annotation_text=f"Passing {passing_threshold:.0f}%",
            annotation_position="top right",
        )

    if average_score is not None and len(scores) > 1:
        fig.add_hline(
            y=float(average_score),
            line_dash="dot",
            line_color=CHART_THEME.average_color,
            annotation_text=f"Average {average_score:.1f}%",
            annotation_position="bottom right",
        )

    height = 280 if compact else 360
    layout = _base_layout()
    layout.update(
        {
            "height": height,
            "yaxis": {
                "title": "Score %",
                "range": [max(0, min(scores + [passing_threshold or 0]) - 10), min(100, max(scores + [passing_threshold or 100]) + 8)],
                "gridcolor": CHART_THEME.grid_color,
                "zeroline": False,
            },
            "xaxis": {"title": "", "gridcolor": CHART_THEME.grid_color},
            "showlegend": False,
        }
    )
    fig.update_layout(**layout)
    return fig


def _domain_status_color(row: Mapping[str, Any]) -> str:
    if not row.get("has_sufficient_evidence", True):
        return CHART_THEME.bar_insufficient
    status = str(row.get("status") or "")
    if status in {"high_risk", "below_target"}:
        return CHART_THEME.bar_primary
    if status == "on_target":
        return CHART_THEME.bar_muted
    return "#3B5B8C"


def build_domain_mastery_figure(
    domain_rows: Sequence[Mapping[str, Any]],
    *,
    compact: bool = False,
    limit: Optional[int] = None,
) -> Optional[Any]:
    """Build horizontal domain mastery bars from verified domain contract rows."""
    plotly_go = _require_plotly()
    rows = list(domain_rows or [])
    if not rows:
        return None
    if limit is not None:
        rows = rows[: max(int(limit), 0)]

    labels: List[str] = []
    accuracies: List[Optional[float]] = []
    colors: List[str] = []
    hover: List[str] = []
    for row in rows:
        domain = str(row.get("Domain") or "")
        labels.append(domain)
        if row.get("has_sufficient_evidence", True):
            accuracy = float(row.get("Accuracy %", 0.0) or 0.0)
            accuracies.append(accuracy)
        else:
            accuracies.append(None)
        colors.append(_domain_status_color(row))
        hover.append(
            f"{domain}<br>"
            f"Accuracy: {row.get('Accuracy %', '—')}%<br>"
            f"Correct: {row.get('Correct', 0)} / {row.get('Total', 0)}<br>"
            f"Exam weight: {row.get('exam_weight', 0)}%<br>"
            f"Evidence: {'Sufficient' if row.get('has_sufficient_evidence') else 'Insufficient'}"
        )

    display_values = [value if value is not None else 0 for value in accuracies]
    fig = plotly_go.Figure(
        plotly_go.Bar(
            x=display_values,
            y=labels,
            orientation="h",
            marker={"color": colors},
            text=[
                "Insufficient evidence" if value is None else f"{value:.1f}%"
                for value in accuracies
            ],
            textposition="outside",
            hovertext=hover,
            hoverinfo="text",
        )
    )
    height = max(220, min(520, 56 * len(labels) + 80)) if not compact else max(180, 48 * len(labels) + 60)
    layout = _base_layout("Verified Domain Mastery" if not compact else "")
    layout.update(
        {
            "height": height,
            "xaxis": {"title": "Verified accuracy %", "range": [0, 100], "gridcolor": CHART_THEME.grid_color},
            "yaxis": {"autorange": "reversed", "tickfont": {"size": 11}},
            "margin": {"l": 180 if not compact else 140, "r": 24, "t": 40 if not compact else 16, "b": 36},
        }
    )
    fig.update_layout(**layout)
    return fig


def build_study_activity_figure(
    daily_counts: Sequence[Tuple[str, int]],
    *,
    window_days: int = 30,
    compact: bool = False,
) -> Optional[Any]:
    """Build compact study-activity bar chart from daily count tuples."""
    plotly_go = _require_plotly()
    if not daily_counts:
        return None

    labels = [label for label, _ in daily_counts]
    values = [count for _, count in daily_counts]
    fig = plotly_go.Figure(
        plotly_go.Bar(
            x=labels,
            y=values,
            marker={"color": CHART_THEME.activity_bar},
            hovertemplate="Date: %{x}<br>Activities: %{y}<extra></extra>",
        )
    )
    height = 220 if compact else 280
    layout = _base_layout()
    layout.update(
        {
            "height": height,
            "xaxis": {"title": f"Last {window_days} days", "tickangle": -35},
            "yaxis": {"title": "Completed activities", "gridcolor": CHART_THEME.grid_color, "rangemode": "tozero"},
            "showlegend": False,
        }
    )
    fig.update_layout(**layout)
    return fig


def chart_summary_score_trend(score_series: Sequence[Any], passing_threshold: Optional[float]) -> str:
    if not score_series:
        return "No verified mock score trend yet."
    latest = score_series[-1]
    score = float(getattr(latest, "score", 0.0) or 0.0)
    count = len(score_series)
    threshold_text = f" Passing threshold is {passing_threshold:.0f}%." if passing_threshold is not None else ""
    return f"Verified score trend across {count} mock{'s' if count != 1 else ''}. Latest score {score:.1f}%.{threshold_text}"


def chart_summary_domain_mastery(domain_rows: Sequence[Mapping[str, Any]]) -> str:
    if not domain_rows:
        return "No verified domain evidence yet."
    weakest = domain_rows[0]
    return (
        f"Verified domain mastery for {len(domain_rows)} domains. "
        f"Priority focus: {weakest.get('Domain')} at {weakest.get('Accuracy %')}% accuracy."
    )


def chart_summary_study_activity(summary: Any) -> str:
    if summary is None:
        return "No recent study activity recorded."
    return (
        f"{getattr(summary, 'active_study_days', 0)} active study days in the last "
        f"{getattr(summary, 'window_days', 30)} days with "
        f"{getattr(summary, 'total_completed_activities', 0)} completed activities."
    )
