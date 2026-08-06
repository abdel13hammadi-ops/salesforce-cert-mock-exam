"""Minimum CertBound color tokens for the BA Scenario Simulator integration.

This is a narrow port of the COLORS dictionary required by
``utils/scenario_simulator_ui_v2.py``. It intentionally omits the full
Dashboard ``theme_css`` redesign and chart/analytics styling surface.
"""

from __future__ import annotations

from typing import Dict

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
    # Additive tokens required by scenario_simulator_ui_v2.py (release e89a7d6).
    "bound_wordmark": "#38BDF8",
    "accent_pressed": "#0C4A6E",
    "accent_bright": "#0284C7",
    "accent_surface": "#E0F2FE",
    "focus_ring": "#7DD3FC",
    "text_secondary": "#3D4F66",
}


def theme_css() -> str:
    """Minimal shared CSS so ``inject_certbound_theme`` does not pull Dashboard CSS.

    Simulator-specific styling is injected separately by
    ``inject_ba_simulator_css`` in ``utils/scenario_simulator_ui_v2.py``.
    """
    c = COLORS
    return f"""
<style>
  .cb-empty-state {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 12px;
    padding: 1rem 1.1rem;
  }}
  .cb-empty-state .cb-card-heading {{
    color: {c['text']};
    font-weight: 700;
    margin-bottom: 0.35rem;
  }}
  .cb-empty-state .cb-body {{
    color: {c['text_secondary']};
    margin: 0;
  }}
  .cb-action-link {{
    display: inline-block;
    margin-top: 0.65rem;
    color: {c['accent']} !important;
    text-decoration: none !important;
    font-weight: 600;
  }}
</style>
"""
