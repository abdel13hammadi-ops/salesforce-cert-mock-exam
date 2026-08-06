"""Learner-facing BA Scenario Simulator presentation helpers (visual Phase 1).

Presentation-only: resolves local scene images, formats progress/mission copy
from the canonical Scenario document / learner-safe views, and injects CSS that
follows CertBound design tokens. Does not own start/submit/persistence logic.
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from utils.scenario_asset_resolver_v2 import (
    SceneImageResolution,
    resolve_cb_sc001_scene_image,
)
from utils.scenario_engine_v2 import ScenarioContentV2
from utils.ui_theme import COLORS


def _st():
    """Lazy Streamlit import so page tests can inject a fake ``streamlit`` module."""
    import streamlit as st

    return st

# Stable widget keys — never include attempt UUID.
WIDGET_KEY_NOTES = "cb_sc001_v2_widget_notes"
WIDGET_KEY_ARTIFACTS = "cb_sc001_v2_widget_artifacts"
WIDGET_KEY_HELP = "cb_sc001_v2_widget_help"
WIDGET_KEY_DECISION_BRIEF = "cb_sc001_v2_widget_decision_brief"
WIDGET_KEY_NOTES_AREA = "cb_sc001_v2_widget_notes_area"

MSG_SIMULATOR_FOOTER_TIP = (
    "Good decisions early create smoother outcomes later. "
    "Think clearly, communicate openly, and align with the right data."
)

# Simulator-scoped palette correction (CERTBOUND-BA-SIMULATOR-RELEASE-CANDIDATE-02).
#
# ``utils.ui_theme.COLORS`` is a shared token dict consumed by Dashboard and
# other unmigrated pages in this isolated release (see dashboard_components.py,
# secondary_components.py, activity_components.py). Overwriting "accent" or
# "primary_navy" globally would silently recolor those unrelated pages, which
# this release must not do. These two keys are overridden locally, for the
# Simulator's own CSS only, with the values already approved and shipped for
# the identical purpose (primary interactive accent / dark navy header
# structure) by the Dashboard redesign — matching the canonical Simulator UI
# reference (assets/scenarios/business_analyst/cb-sc-001/references/
# canonical-simulator-ui-reference.png) and the approved Sky Blue brand
# direction. No other COLORS key is overridden: "border" differs only
# cosmetically from baseline and is not brand-critical; "success"/"success_bg"
# are already green in the baseline palette, so the semantic-success
# requirement is already satisfied without any change.
SIMULATOR_COLOR_OVERRIDES: Dict[str, str] = {
    "accent": "#0369A1",
    "primary_navy": "#0B1F3A",
}


def inject_ba_simulator_css() -> None:
    """Inject Simulator-specific layout CSS using CertBound tokens."""
    c = {**COLORS, **SIMULATOR_COLOR_OVERRIDES}
    _st().markdown(
        f"""
<style>
/* Focused workflow: collapse accidental sidebar sprawl on this page */
section[data-testid="stSidebar"] {{
  min-width: 0 !important;
}}
div[data-testid="stToolbar"] {{
  visibility: hidden;
  height: 0;
}}

.cb-sim-shell {{
  font-family: "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
  color: {c['text']};
  max-width: 1180px;
  width: 100%;
  margin: 0 auto 1.5rem auto;
  box-sizing: border-box;
  overflow-wrap: anywhere;
}}
.cb-sim-panel p,
.cb-sim-panel h3,
.cb-sim-choices-heading,
.cb-sim-bubble__text,
.cb-sim-terminal p,
.cb-sim-terminal h2 {{
  overflow-wrap: anywhere;
  word-wrap: break-word;
  max-width: 100%;
}}

.cb-sim-header {{
  background: {c['primary_navy']};
  color: #FFFFFF;
  border-radius: 12px;
  padding: 0.85rem 1.1rem;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem 1.25rem;
  box-shadow: 0 8px 24px rgba(11, 31, 58, 0.14);
}}
.cb-sim-brand {{
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  min-width: 10rem;
}}
.cb-sim-brand__wordmark {{
  font-size: 1.35rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.1;
}}
.cb-sim-brand__cert {{ color: #FFFFFF; }}
.cb-sim-brand__bound {{ color: {c['bound_wordmark']}; }}
.cb-sim-brand__product {{
  font-size: 0.78rem;
  color: rgba(255,255,255,0.78);
  font-weight: 500;
}}
.cb-sim-title-block {{
  flex: 1 1 14rem;
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.55rem;
}}
.cb-sim-title {{
  font-size: 1.05rem;
  font-weight: 650;
  margin: 0;
}}
.cb-sim-status {{
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  background: {c['success_bg']};
  color: {c['success']};
  border-radius: 999px;
  padding: 0.18rem 0.65rem;
  font-size: 0.72rem;
  font-weight: 650;
}}
.cb-sim-status--complete {{
  background: {c['accent_surface']};
  color: {c['accent_pressed']};
}}
.cb-sim-meta {{
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.65rem 0.9rem;
  margin-left: auto;
}}
.cb-sim-progress {{
  font-size: 0.82rem;
  color: rgba(255,255,255,0.88);
  font-weight: 560;
}}
.cb-sim-progress-dots {{
  display: inline-flex;
  align-items: center;
  gap: 0.28rem;
  margin-left: 0.35rem;
}}
.cb-sim-dot {{
  width: 0.55rem;
  height: 0.55rem;
  border-radius: 999px;
  background: rgba(255,255,255,0.28);
}}
.cb-sim-dot.is-done {{ background: {c['bound_wordmark']}; }}
.cb-sim-dot.is-current {{
  background: #FFFFFF;
  box-shadow: 0 0 0 2px {c['bound_wordmark']};
}}
.cb-sim-avatar {{
  width: 2rem;
  height: 2rem;
  border-radius: 999px;
  background: {c['accent_pressed']};
  border: 2px solid {c['bound_wordmark']};
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 0.7rem;
  font-weight: 700;
}}

.cb-sim-context-row {{
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
  gap: 0.85rem;
  margin: 0.9rem 0 0.75rem 0;
}}
@media (max-width: 900px) {{
  .cb-sim-context-row {{ grid-template-columns: 1fr; }}
}}
.cb-sim-panel {{
  background: #FFFFFF;
  border: 1px solid {c['border']};
  border-radius: 12px;
  padding: 0.9rem 1rem;
  box-shadow: 0 2px 8px rgba(11, 31, 58, 0.05);
}}
.cb-sim-panel--mission {{
  background: {c['accent_surface']};
  border-color: #BAE6FD;
}}
.cb-sim-kicker {{
  display: inline-block;
  background: {c['accent']};
  color: #FFFFFF;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  border-radius: 6px;
  padding: 0.18rem 0.45rem;
  margin-bottom: 0.45rem;
}}
.cb-sim-panel h3 {{
  margin: 0 0 0.35rem 0;
  font-size: 1.05rem;
  color: {c['text']};
}}
.cb-sim-panel p {{
  margin: 0;
  color: {c['text_secondary']};
  font-size: 0.92rem;
  line-height: 1.45;
}}
.cb-sim-mission-title {{
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-weight: 700;
  color: {c['accent_pressed']};
  margin-bottom: 0.35rem;
}}

.cb-sim-main {{
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr);
  gap: 0.9rem;
  margin-bottom: 0.85rem;
  align-items: stretch;
}}
@media (max-width: 900px) {{
  .cb-sim-main {{ grid-template-columns: 1fr; }}
}}
.cb-sim-image-wrap {{
  background: {c['surface_muted']};
  border: 1px solid {c['border']};
  border-radius: 12px;
  overflow: hidden;
  min-height: 220px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0.35rem;
}}
div[data-testid="stImage"] {{
  width: 100%;
}}
div[data-testid="stImage"] img {{
  width: 100% !important;
  height: auto !important;
  max-height: 420px !important;
  object-fit: contain !important;
  display: block;
}}
.sr-only {{
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}}
.cb-sim-image-wrap img {{
  width: 100%;
  max-width: 100%;
  height: auto;
  display: block;
  object-fit: contain;
}}
  width: 100%;
  min-height: 220px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.35rem;
  padding: 1.25rem;
  text-align: center;
  background: linear-gradient(160deg, {c['accent_surface']} 0%, #FFFFFF 70%);
  color: {c['text_secondary']};
}}
.cb-sim-image-fallback strong {{
  color: {c['accent_pressed']};
  font-size: 1rem;
}}

.cb-sim-conversation {{
  background: #FFFFFF;
  border: 1px solid {c['border']};
  border-radius: 12px;
  padding: 0.85rem 0.95rem;
  display: flex;
  flex-direction: column;
  min-height: 220px;
  max-height: 420px;
  overflow: auto;
}}
.cb-sim-conversation__title {{
  font-weight: 700;
  font-size: 0.95rem;
  margin-bottom: 0.65rem;
  color: {c['text']};
}}
.cb-sim-bubble {{
  margin-bottom: 0.65rem;
  padding-bottom: 0.55rem;
  border-bottom: 1px solid {c['surface_subtle']};
}}
.cb-sim-bubble:last-child {{ border-bottom: none; margin-bottom: 0; }}
.cb-sim-bubble__speaker {{
  font-size: 0.78rem;
  font-weight: 700;
  color: {c['accent']};
  margin-bottom: 0.15rem;
}}
.cb-sim-bubble__text {{
  font-size: 0.9rem;
  line-height: 1.4;
  color: {c['text']};
}}
.cb-sim-bubble--you {{
  background: {c['accent_surface']};
  border: 1px solid #BAE6FD;
  border-radius: 10px;
  padding: 0.55rem 0.7rem;
  border-bottom: none;
}}
.cb-sim-bubble--you .cb-sim-bubble__speaker {{
  color: {c['accent_pressed']};
}}

.cb-sim-choices-heading {{
  font-size: 1.05rem;
  font-weight: 700;
  margin: 0.35rem 0 0.55rem 0;
  color: {c['text']};
}}
div[data-testid="stRadio"] > label {{
  display: none !important;
}}
div[data-testid="stRadio"] div[role="radiogroup"] label {{
  background: #FFFFFF !important;
  border: 1px solid {c['border']} !important;
  border-radius: 12px !important;
  padding: 0.85rem 1rem !important;
  margin-bottom: 0.55rem !important;
  box-shadow: 0 2px 8px rgba(11, 31, 58, 0.05);
  transition: border-color 120ms ease, box-shadow 120ms ease;
  height: auto !important;
  min-height: 0 !important;
  align-items: flex-start !important;
  white-space: normal !important;
  overflow: visible !important;
}}
div[data-testid="stRadio"] div[role="radiogroup"] label > div,
div[data-testid="stRadio"] div[role="radiogroup"] label [data-testid="stMarkdownContainer"],
div[data-testid="stRadio"] div[role="radiogroup"] label p,
div[data-testid="stRadio"] div[role="radiogroup"] label span {{
  white-space: normal !important;
  overflow: visible !important;
  word-wrap: break-word !important;
  overflow-wrap: anywhere !important;
  text-overflow: unset !important;
  max-width: 100% !important;
}}
div[data-testid="stRadio"] div[role="radiogroup"] label:hover {{
  border-color: {c['accent_bright']} !important;
  box-shadow: 0 0 0 2px {c['focus_ring']};
}}
div[data-testid="stRadio"] div[role="radiogroup"] label[data-checked="true"],
div[data-testid="stRadio"] div[role="radiogroup"] label:has(input:checked) {{
  border-color: {c['accent']} !important;
  background: {c['accent_surface']} !important;
}}

.cb-sim-footer {{
  margin-top: 0.85rem;
  background: {c['primary_navy']};
  color: rgba(255,255,255,0.9);
  border-radius: 12px;
  padding: 0.75rem 1rem;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.65rem 1rem;
  font-size: 0.85rem;
}}
.cb-sim-footer__tip {{ flex: 1 1 16rem; min-width: 0; }}

.cb-sim-terminal {{
  background: #FFFFFF;
  border: 1px solid {c['border']};
  border-radius: 12px;
  padding: 1.25rem 1.35rem;
  box-shadow: 0 8px 24px rgba(11, 31, 58, 0.08);
}}
.cb-sim-terminal__score {{
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  background: {c['accent_surface']};
  color: {c['accent_pressed']};
  border-radius: 999px;
  padding: 0.3rem 0.75rem;
  font-weight: 700;
  margin: 0.65rem 0;
}}

/* Prevent horizontal overflow on narrow viewports */
.cb-sim-shell, .cb-sim-shell * {{
  max-width: 100%;
  box-sizing: border-box;
}}

  @media (max-width: 480px) {{
  html, body {{
    max-width: 100%;
    overflow-x: clip;
  }}
  section.main > div.block-container {{
    max-width: 100% !important;
    padding-left: 0.5rem !important;
    padding-right: 0.5rem !important;
    overflow-x: clip !important;
  }}
  .cb-sim-header {{
    flex-direction: column;
    align-items: stretch;
    gap: 0.65rem;
  }}
  .cb-sim-brand {{
    min-width: 0;
    width: 100%;
  }}
  .cb-sim-title-block {{
    flex: none;
    width: 100%;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.35rem;
  }}
  .cb-sim-title {{
    font-size: 1rem;
    line-height: 1.3;
  }}
  .cb-sim-status {{
    flex-shrink: 0;
    white-space: nowrap;
  }}
  .cb-sim-meta {{
    margin-left: 0;
    width: 100%;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.5rem;
  }}
  .cb-sim-progress {{
    flex: 1 1 auto;
    min-width: 0;
    white-space: normal;
    line-height: 1.35;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.35rem;
  }}
  .cb-sim-progress-dots {{
    margin-left: 0;
    flex: 0 0 auto;
  }}
  .cb-sim-terminal {{
    padding: 1rem 0.95rem;
  }}
  .cb-sim-terminal h2 {{
    font-size: 1.15rem;
    line-height: 1.3;
    word-wrap: break-word;
    overflow-wrap: anywhere;
  }}
  .cb-sim-terminal p {{
    word-wrap: break-word;
    overflow-wrap: anywhere;
  }}
  div[data-testid="stRadio"] div[role="radiogroup"] {{
    width: 100% !important;
  }}
  div[data-testid="stRadio"] div[role="radiogroup"] label {{
    width: 100% !important;
    max-width: 100% !important;
  }}
  .cb-sim-shell {{
    padding-left: 0.35rem;
    padding-right: 0.35rem;
    overflow-x: clip;
  }}
}}
</style>
        """,
        unsafe_allow_html=True,
    )


def scene_progress_from_content(
    content: ScenarioContentV2,
    scene_id: str,
) -> Tuple[int, int]:
    """Return 1-based (index, total) from authored scene order in the document."""
    scenes = content.document.get("scenes") or ()
    if not isinstance(scenes, (list, tuple)) or not scenes:
        return 0, 0
    ordered = sorted(
        [s for s in scenes if isinstance(s, Mapping)],
        key=lambda s: int(s.get("authoredOrder") or 0),
    )
    total = len(ordered)
    sid = str(scene_id or "").strip()
    for index, scene in enumerate(ordered, start=1):
        if str(scene.get("id") or "").strip() == sid:
            return index, total
    return 0, total


def format_scene_progress_caption(index: int, total: int) -> str:
    if index <= 0 or total <= 0:
        return "In progress"
    return f"Scene {index:02d} of {total:02d}"


def mission_text_from_content(content: ScenarioContentV2, scene_id: str) -> str:
    """Mission copy from learnerRole / scene entering description — never hard-coded plot."""
    scenes_by_id = content.scenes_by_id
    scene = scenes_by_id.get(str(scene_id or "").strip()) if scenes_by_id else None
    if isinstance(scene, Mapping):
        entering = str(scene.get("enteringStateDescription") or "").strip()
        if entering:
            return entering
    role = content.document.get("learnerRole") or {}
    if isinstance(role, Mapping):
        summary = str(role.get("summary") or "").strip()
        if summary:
            return summary
    intro = content.document.get("introduction") or {}
    if isinstance(intro, Mapping):
        briefing = intro.get("projectBriefing") or {}
        if isinstance(briefing, Mapping):
            summary = str(briefing.get("summary") or "").strip()
            if summary:
                return summary
    return ""


def scene_context_text(scene: Mapping[str, Any], content: ScenarioContentV2) -> str:
    setting = str(scene.get("setting") or "").strip()
    if setting:
        return setting
    return mission_text_from_content(content, str(scene.get("sceneId") or ""))


def note_prompts_from_scene(scene: Mapping[str, Any]) -> List[str]:
    progress = scene.get("progressMetadata")
    if not isinstance(progress, Mapping):
        return []
    prompts = progress.get("notePrompts")
    if not isinstance(prompts, (list, tuple)):
        return []
    return [str(p).strip() for p in prompts if str(p).strip()]


def artifacts_for_scene(content: ScenarioContentV2, scene_id: str) -> List[Dict[str, str]]:
    scene = content.scenes_by_id.get(str(scene_id or "").strip())
    refs: Sequence[Any] = ()
    if isinstance(scene, Mapping):
        raw_refs = scene.get("artifactReferences") or []
        if isinstance(raw_refs, (list, tuple)):
            refs = raw_refs
    wanted = {str(r).strip() for r in refs if str(r).strip()}
    intro = content.document.get("introduction") or {}
    previews = intro.get("artifactPreviews") if isinstance(intro, Mapping) else None
    if not isinstance(previews, (list, tuple)):
        return []
    results: List[Dict[str, str]] = []
    for preview in previews:
        if not isinstance(preview, Mapping):
            continue
        artifact_id = str(preview.get("artifactId") or "").strip()
        if wanted and artifact_id not in wanted:
            continue
        title = str(preview.get("title") or artifact_id).strip()
        summary = str(preview.get("summary") or "").strip()
        if title:
            results.append({"id": artifact_id, "title": title, "summary": summary})
    if results:
        return results
    # If the scene has no refs, still surface all previews under Artifacts.
    for preview in previews:
        if not isinstance(preview, Mapping):
            continue
        title = str(preview.get("title") or "").strip()
        if not title:
            continue
        results.append(
            {
                "id": str(preview.get("artifactId") or ""),
                "title": title,
                "summary": str(preview.get("summary") or "").strip(),
            }
        )
    return results


def help_copy_from_content(content: ScenarioContentV2) -> Tuple[str, List[str]]:
    intro = content.document.get("introduction") or {}
    if not isinstance(intro, Mapping):
        return "", []
    gate = intro.get("startGate") or {}
    body = ""
    if isinstance(gate, Mapping):
        body = str(gate.get("body") or "").strip()
    risks = intro.get("knownRisks") or []
    risk_list = (
        [str(r).strip() for r in risks if str(r).strip()]
        if isinstance(risks, (list, tuple))
        else []
    )
    return body, risk_list


def decision_brief_sections(content: ScenarioContentV2) -> List[Tuple[str, str]]:
    """Sections for the Decision Brief control — sourced only from the document."""
    sections: List[Tuple[str, str]] = []
    intro = content.document.get("introduction") or {}
    if not isinstance(intro, Mapping):
        return sections
    briefing = intro.get("projectBriefing") or {}
    if isinstance(briefing, Mapping):
        title = str(briefing.get("title") or "Project briefing").strip()
        summary = str(briefing.get("summary") or "").strip()
        timeline = str(briefing.get("timelineContext") or "").strip()
        body = summary
        if timeline:
            body = f"{body}\n\n{timeline}".strip()
        if body:
            sections.append((title, body))
    criteria = intro.get("successCriteria") or ()
    if isinstance(criteria, (list, tuple)) and criteria:
        bullets = "\n".join(f"• {str(item).strip()}" for item in criteria if str(item).strip())
        if bullets:
            sections.append(("Success criteria", bullets))
    risks = intro.get("knownRisks") or ()
    if isinstance(risks, (list, tuple)) and risks:
        bullets = "\n".join(f"• {str(item).strip()}" for item in risks if str(item).strip())
        if bullets:
            sections.append(("Known risks", bullets))
    return sections


def resolve_scene_image_for_view(
    scene: Mapping[str, Any],
) -> SceneImageResolution:
    scene_id = str(scene.get("sceneId") or "").strip()
    title = str(scene.get("title") or "").strip()
    return resolve_cb_sc001_scene_image(scene_id, scene_title=title)


def _progress_dots_html(index: int, total: int, *, max_dots: int = 12) -> str:
    if total <= 0:
        return ""
    shown = min(total, max_dots)
    # Scale current index into shown dots when total exceeds max_dots.
    current = index
    if total > max_dots and index > 0:
        current = max(1, round(index * shown / total))
    parts = []
    for i in range(1, shown + 1):
        cls = "cb-sim-dot"
        if i < current:
            cls += " is-done"
        elif i == current:
            cls += " is-current"
        parts.append(f'<span class="{cls}"></span>')
    return '<span class="cb-sim-progress-dots">' + "".join(parts) + "</span>"


def render_simulator_header(
    *,
    scenario_title: str,
    status_label: str,
    progress_caption: str,
    progress_index: int,
    progress_total: int,
    complete: bool = False,
) -> None:
    status_class = "cb-sim-status cb-sim-status--complete" if complete else "cb-sim-status"
    dots = _progress_dots_html(progress_index, progress_total) if not complete else ""
    _st().markdown(
        f"""
<div class="cb-sim-header">
  <div class="cb-sim-brand">
    <div class="cb-sim-brand__wordmark">
      <span class="cb-sim-brand__cert">Cert</span><span class="cb-sim-brand__bound">Bound</span>
    </div>
    <div class="cb-sim-brand__product">BA Scenario Simulator</div>
  </div>
  <div class="cb-sim-title-block">
    <p class="cb-sim-title">{html.escape(scenario_title)}</p>
    <span class="{status_class}">{html.escape(status_label)}</span>
  </div>
  <div class="cb-sim-meta">
    <span class="cb-sim-progress">{html.escape(progress_caption)}{dots}</span>
    <span class="cb-sim-avatar" title="Business Analyst">BA</span>
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )


def render_notes_artifacts_help(
    *,
    content: ScenarioContentV2,
    scene: Mapping[str, Any],
) -> None:
    scene_id = str(scene.get("sceneId") or "")
    note_prompts = note_prompts_from_scene(scene)
    artifacts = artifacts_for_scene(content, scene_id)
    help_body, help_risks = help_copy_from_content(content)

    n_col, a_col, h_col = _st().columns(3)
    with n_col:
        with _st().popover("Notes", use_container_width=True):
            if note_prompts:
                _st().caption("Suggested notes for this scene")
                for prompt in note_prompts:
                    _st().markdown(f"- {prompt}")
            else:
                _st().caption("Capture observations as you progress.")
            _st().text_area(
                "Your notes",
                key=WIDGET_KEY_NOTES_AREA,
                height=120,
                placeholder="Jot down open questions, assumptions, and follow-ups…",
                label_visibility="collapsed",
            )
    with a_col:
        with _st().popover("Artifacts", use_container_width=True):
            if not artifacts:
                _st().caption("No artifacts are linked to this scene.")
            for artifact in artifacts:
                _st().markdown(f"**{artifact['title']}**")
                if artifact.get("summary"):
                    _st().write(artifact["summary"])
    with h_col:
        with _st().popover("Help", use_container_width=True):
            if help_body:
                _st().write(help_body)
            if help_risks:
                _st().markdown("**Watch for**")
                for risk in help_risks:
                    _st().markdown(f"- {risk}")
            if not help_body and not help_risks:
                _st().caption("Use Notes and Artifacts to support each decision.")


def render_scene_context_and_mission(
    *,
    content: ScenarioContentV2,
    scene: Mapping[str, Any],
    progress_index: int,
) -> None:
    title = str(scene.get("title") or "Scene").strip()
    context = scene_context_text(scene, content)
    mission = mission_text_from_content(content, str(scene.get("sceneId") or ""))
    kicker = f"SCENE {progress_index:02d}" if progress_index > 0 else "SCENE"
    _st().markdown(
        f"""
<div class="cb-sim-context-row">
  <div class="cb-sim-panel">
    <span class="cb-sim-kicker">{html.escape(kicker)}</span>
    <h3>{html.escape(title)}</h3>
    <p>{html.escape(context)}</p>
  </div>
  <div class="cb-sim-panel cb-sim-panel--mission">
    <div class="cb-sim-mission-title">◎ Your Mission</div>
    <p>{html.escape(mission)}</p>
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )


def render_conversation_html(scene: Mapping[str, Any]) -> str:
    exchanges = scene.get("dialogueExchanges") or []
    parts = ['<div class="cb-sim-conversation">', '<div class="cb-sim-conversation__title">Conversation</div>']
    if isinstance(exchanges, list):
        for exchange in exchanges:
            if not isinstance(exchange, Mapping):
                continue
            speaker = exchange.get("speakerDisplayName") or exchange.get("speakerId") or "Speaker"
            text = exchange.get("text") or exchange.get("dialogue") or ""
            if not text:
                continue
            parts.append(
                '<div class="cb-sim-bubble">'
                f'<div class="cb-sim-bubble__speaker">{html.escape(str(speaker))}</div>'
                f'<div class="cb-sim-bubble__text">{html.escape(str(text))}</div>'
                "</div>"
            )
    prompt = scene.get("decisionPrompt")
    if isinstance(prompt, str) and prompt.strip():
        parts.append(
            '<div class="cb-sim-bubble cb-sim-bubble--you">'
            '<div class="cb-sim-bubble__speaker">You (Business Analyst)</div>'
            f'<div class="cb-sim-bubble__text">{html.escape(prompt.strip())}</div>'
            "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def render_scene_image_panel(resolution: SceneImageResolution) -> None:
    if resolution.available and resolution.path is not None:
        _st().image(str(resolution.path), use_container_width=True)
        _st().markdown(
            f'<span class="sr-only">{html.escape(resolution.alt_text)}</span>',
            unsafe_allow_html=True,
        )
    else:
        title = resolution.title or resolution.scene_id or "Scene"
        _st().markdown(
            f"""
<div class="cb-sim-image-fallback" role="img" aria-label="{html.escape(resolution.alt_text)}">
  <strong>{html.escape(title)}</strong>
  <span>Scene illustration unavailable</span>
</div>
            """,
            unsafe_allow_html=True,
        )


def render_decision_brief_control(content: ScenarioContentV2) -> None:
    sections = decision_brief_sections(content)
    with _st().expander("Review Decision Brief", expanded=False):
        if not sections:
            _st().caption("Decision brief details will appear when the scenario provides them.")
            return
        for title, body in sections:
            _st().markdown(f"**{title}**")
            _st().write(body)


def render_simulator_footer_tip() -> None:
    _st().markdown(
        f"""
<div class="cb-sim-footer">
  <div class="cb-sim-footer__tip">ℹ {html.escape(MSG_SIMULATOR_FOOTER_TIP)}</div>
</div>
        """,
        unsafe_allow_html=True,
    )


def option_card_label(option: Mapping[str, Any]) -> str:
    title = str(option.get("title") or "").strip()
    text = str(option.get("text") or "").strip()
    if title and text and title != text:
        return f"{title}\n{text}"
    return title or text or str(option.get("id") or "Option")
