"""Presentation-only charts for learner activity results."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from utils.dashboard_charts import CHART_THEME
from utils.ui_theme import COLORS


def _import_plotly():
    try:
        import plotly.graph_objects as go  # noqa: PLC0415
    except Exception:
        return None
    return go


def build_breakdown_figure(
    rows: Sequence[Mapping[str, Any]],
    *,
    title: str = "",
    passing_threshold: Optional[float] = None,
    horizontal: bool = True,
) -> Optional[Any]:
    """Build a restrained horizontal/vertical breakdown chart from precomputed rows."""
    if not rows:
        return None
    go = _import_plotly()
    if go is None:
        return None

    labels = [str(row.get("label") or "") for row in rows]
    percents = [float(row.get("percent") or 0) for row in rows]
    hover = [
        f"{row.get('correct', 0)} / {row.get('total', 0)} correct ({row.get('percent', 0)}%)"
        for row in rows
    ]

    if horizontal:
        fig = go.Figure(
            go.Bar(
                x=percents,
                y=labels,
                orientation="h",
                marker_color=CHART_THEME.activity_bar,
                text=[f"{p:.0f}%" for p in percents],
                textposition="outside",
                hovertext=hover,
                hoverinfo="text",
            )
        )
        fig.update_layout(
            title=title or None,
            height=max(220, 42 * len(labels) + 80),
            margin=dict(l=20, r=20, t=40 if title else 20, b=20),
            paper_bgcolor=CHART_THEME.paper_background,
            plot_bgcolor=CHART_THEME.background,
            font=dict(family=CHART_THEME.font_family, size=CHART_THEME.font_size, color=COLORS["text"]),
            xaxis=dict(range=[0, 100], ticksuffix="%", gridcolor=CHART_THEME.grid_color),
            yaxis=dict(automargin=True),
        )
    else:
        fig = go.Figure(
            go.Bar(
                x=labels,
                y=percents,
                marker_color=CHART_THEME.activity_bar,
                text=[f"{p:.0f}%" for p in percents],
                textposition="outside",
                hovertext=hover,
                hoverinfo="text",
            )
        )
        fig.update_layout(
            title=title or None,
            height=300,
            margin=dict(l=20, r=20, t=40 if title else 20, b=20),
            paper_bgcolor=CHART_THEME.paper_background,
            plot_bgcolor=CHART_THEME.background,
            font=dict(family=CHART_THEME.font_family, size=CHART_THEME.font_size, color=COLORS["text"]),
            yaxis=dict(range=[0, 100], ticksuffix="%", gridcolor=CHART_THEME.grid_color),
        )

    if passing_threshold is not None:
        if horizontal:
            fig.add_vline(
                x=float(passing_threshold),
                line_dash="dash",
                line_color=CHART_THEME.threshold_color,
                annotation_text=f"Passing {passing_threshold:.0f}%",
                annotation_position="top",
            )
        else:
            fig.add_hline(
                y=float(passing_threshold),
                line_dash="dash",
                line_color=CHART_THEME.threshold_color,
                annotation_text=f"Passing {passing_threshold:.0f}%",
                annotation_position="right",
            )
    return fig


def breakdown_chart_caption(row_count: int, *, kind: str) -> str:
    if row_count <= 0:
        return f"No {kind} breakdown data available."
    return f"{kind.title()} performance across {row_count} categories."
