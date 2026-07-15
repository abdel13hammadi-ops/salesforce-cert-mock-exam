"""Centralized CertBound learner-dashboard design tokens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class ChartTheme:
    font_family: str = "Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif"
    font_size: int = 13
    title_size: int = 15
    background: str = "rgba(0,0,0,0)"
    paper_background: str = "rgba(0,0,0,0)"
    grid_color: str = "#E2E8F0"
    axis_color: str = "#64748B"
    line_color: str = "#1E3A5F"
    marker_color: str = "#2563EB"
    threshold_color: str = "#D97706"
    average_color: str = "#64748B"
    bar_primary: str = "#1E3A5F"
    bar_muted: str = "#94A3B8"
    bar_insufficient: str = "#CBD5E1"
    activity_bar: str = "#3B5B8C"


COLORS: Dict[str, str] = {
    "primary": "#0F172A",
    "primary_navy": "#1E3A5F",
    "secondary": "#334155",
    "surface": "#FFFFFF",
    "surface_muted": "#F8FAFC",
    "surface_subtle": "#F1F5F9",
    "text": "#0F172A",
    "text_muted": "#64748B",
    "text_inverse": "#FFFFFF",
    "border": "#E2E8F0",
    "border_strong": "#CBD5E1",
    "accent": "#2563EB",
    "accent_muted": "#3B5B8C",
    "success": "#0F766E",
    "success_bg": "#ECFDF5",
    "warning": "#D97706",
    "warning_bg": "#FFFBEB",
    "danger": "#B91C1C",
    "danger_bg": "#FEF2F2",
    "neutral": "#64748B",
    "neutral_bg": "#F8FAFC",
    "locked": "#475569",
    "locked_bg": "#F1F5F9",
}

SPACING: Dict[str, str] = {
    "xs": "0.35rem",
    "sm": "0.65rem",
    "md": "1rem",
    "lg": "1.5rem",
    "xl": "2rem",
}

RADIUS = "14px"
SHADOW = "0 10px 28px rgba(15, 23, 42, 0.08)"
SHADOW_SOFT = "0 4px 14px rgba(15, 23, 42, 0.05)"

CHART_THEME = ChartTheme()

REQUIRED_TOKEN_KEYS = (
    "primary",
    "secondary",
    "surface",
    "text",
    "text_muted",
    "border",
    "success",
    "warning",
    "danger",
    "neutral",
)


def validate_theme_tokens() -> bool:
    """Return True when all required color tokens are defined."""
    return all(key in COLORS for key in REQUIRED_TOKEN_KEYS)


def theme_css() -> str:
    """Return shared CertBound dashboard CSS."""
    c = COLORS
    s = SPACING
    return f"""
    <style>
    :root {{
        --cb-primary: {c['primary']};
        --cb-primary-navy: {c['primary_navy']};
        --cb-secondary: {c['secondary']};
        --cb-surface: {c['surface']};
        --cb-surface-muted: {c['surface_muted']};
        --cb-surface-subtle: {c['surface_subtle']};
        --cb-text: {c['text']};
        --cb-text-muted: {c['text_muted']};
        --cb-border: {c['border']};
        --cb-accent: {c['accent']};
        --cb-success: {c['success']};
        --cb-warning: {c['warning']};
        --cb-danger: {c['danger']};
        --cb-neutral: {c['neutral']};
        --cb-radius: {RADIUS};
        --cb-shadow: {SHADOW};
        --cb-space-sm: {s['sm']};
        --cb-space-md: {s['md']};
        --cb-space-lg: {s['lg']};
    }}
    .cb-shell {{
        font-family: Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
        color: var(--cb-text);
    }}
    .cb-card {{
        background: var(--cb-surface);
        border: 1px solid var(--cb-border);
        border-radius: var(--cb-radius);
        box-shadow: var(--cb-shadow);
        padding: var(--cb-space-lg);
        margin-bottom: var(--cb-space-md);
        width: 100%;
        box-sizing: border-box;
    }}
    .cb-card-muted {{
        background: var(--cb-surface-muted);
    }}
    .cb-card-title {{
        font-size: 0.78rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--cb-text-muted);
        margin: 0 0 0.35rem 0;
    }}
    .cb-card-heading {{
        font-size: 1.35rem;
        font-weight: 800;
        color: var(--cb-primary-navy);
        margin: 0 0 0.5rem 0;
        line-height: 1.2;
    }}
    .cb-body {{
        font-size: 0.95rem;
        line-height: 1.55;
        color: var(--cb-secondary);
        margin: 0;
    }}
    .cb-caption {{
        font-size: 0.82rem;
        color: var(--cb-text-muted);
        line-height: 1.45;
        margin-top: 0.35rem;
    }}
    .cb-kpi-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: var(--cb-space-md);
        width: 100%;
    }}
    .cb-kpi-card {{
        background: var(--cb-surface);
        border: 1px solid var(--cb-border);
        border-radius: 12px;
        padding: 1rem 1.05rem;
        min-width: 0;
        box-sizing: border-box;
    }}
    .cb-kpi-label {{
        font-size: 0.78rem;
        font-weight: 600;
        color: var(--cb-text-muted);
        margin-bottom: 0.35rem;
    }}
    .cb-kpi-value {{
        font-size: 1.55rem;
        font-weight: 800;
        color: var(--cb-primary-navy);
        line-height: 1.1;
    }}
    .cb-kpi-sub {{
        font-size: 0.8rem;
        color: var(--cb-text-muted);
        margin-top: 0.3rem;
    }}
    .cb-trend-up {{ color: var(--cb-success); font-weight: 600; }}
    .cb-trend-down {{ color: var(--cb-danger); font-weight: 600; }}
    .cb-trend-flat {{ color: var(--cb-neutral); font-weight: 600; }}
    .cb-badge {{
        display: inline-block;
        padding: 0.18rem 0.55rem;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.03em;
        border: 1px solid var(--cb-border);
        background: var(--cb-surface-subtle);
        color: var(--cb-secondary);
        white-space: nowrap;
    }}
    .cb-badge-success {{ background: {c['success_bg']}; color: {c['success']}; border-color: #A7F3D0; }}
    .cb-badge-warning {{ background: {c['warning_bg']}; color: {c['warning']}; border-color: #FDE68A; }}
    .cb-badge-danger {{ background: {c['danger_bg']}; color: {c['danger']}; border-color: #FECACA; }}
    .cb-badge-neutral {{ background: {c['neutral_bg']}; color: {c['neutral']}; }}
    .cb-badge-locked {{ background: {c['locked_bg']}; color: {c['locked']}; }}
    .cb-empty-state {{
        background: var(--cb-surface-muted);
        border: 1px dashed var(--cb-border);
        border-radius: var(--cb-radius);
        padding: 1.25rem;
    }}
    .cb-progress-track {{
        width: 100%;
        height: 10px;
        background: var(--cb-surface-subtle);
        border-radius: 999px;
        overflow: hidden;
        margin: 0.75rem 0;
    }}
    .cb-progress-fill {{
        height: 100%;
        background: linear-gradient(90deg, {c['primary_navy']} 0%, {c['accent']} 100%);
        border-radius: 999px;
    }}
    .cb-readiness-hero {{
        display: grid;
        grid-template-columns: minmax(140px, 180px) 1fr;
        gap: 1.25rem;
        align-items: center;
        width: 100%;
    }}
    @media (max-width: 720px) {{
        .cb-readiness-hero {{
            grid-template-columns: 1fr;
        }}
    }}
    .cb-gauge-wrap {{
        position: relative;
        width: 160px;
        max-width: 100%;
        margin: 0 auto;
    }}
    .cb-gauge-score {{
        position: absolute;
        inset: 0;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        font-weight: 800;
        color: var(--cb-primary-navy);
    }}
    .cb-gauge-score .value {{ font-size: 2rem; line-height: 1; }}
    .cb-gauge-score .label {{ font-size: 0.75rem; color: var(--cb-text-muted); margin-top: 0.2rem; }}
    .cb-action-link {{
        display: inline-block;
        margin-top: 0.75rem;
        padding: 0.55rem 0.95rem;
        border-radius: 999px;
        background: var(--cb-primary-navy);
        color: #FFFFFF !important;
        text-decoration: none !important;
        font-weight: 700;
        font-size: 0.88rem;
    }}
    div[data-testid="stMarkdownContainer"] a.cb-action-link,
    div[data-testid="stMarkdownContainer"] a.cb-action-link:visited,
    div[data-testid="stMarkdownContainer"] a.cb-action-link:hover,
    div[data-testid="stMarkdownContainer"] a.cb-action-link:active {{
        color: #FFFFFF !important;
        text-decoration: none !important;
    }}
    .cb-activity-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 0.75rem;
        width: 100%;
    }}
    .cb-activity-row {{
        border: 1px solid var(--cb-border);
        border-radius: 12px;
        padding: 0.85rem 1rem;
        background: var(--cb-surface);
        display: grid;
        gap: 0.35rem;
    }}
    .cb-activity-top {{
        display: flex;
        justify-content: space-between;
        gap: 0.5rem;
        align-items: flex-start;
        flex-wrap: wrap;
    }}
    .cb-activity-meta {{
        font-size: 0.82rem;
        color: var(--cb-text-muted);
    }}
    </style>
    """
