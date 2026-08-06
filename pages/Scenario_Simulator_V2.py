"""SIM-STREAMLIT-V2-01: isolated Engine V2 CB-SC-001 Streamlit vertical slice.

This page is intentionally separate from ``pages/Scenario_Simulator.py`` (Engine
V1 / BA-201). It uses the frozen Option B session contract implemented in
``utils.scenario_streamlit_v2`` and the Engine V2 controller in
``utils.scenario_controller_v2``.

Visual Phase 1 (CERTBOUND-BA-SIMULATOR-VISUAL-01) restyles the learner-facing
layout to match the approved CertBound Simulator reference while preserving
discovery / start / resume / submit / idempotency / completion behavior.
"""

from __future__ import annotations

import html
from typing import Optional

import streamlit as st

from utils.access_control import (
    get_current_user_email,
    get_supabase_admin_client,
    render_app_chrome,
    require_paid_access,
)
from utils.dashboard_components import inject_certbound_theme, render_empty_state
from utils.navigation import CERTBOUND_ENABLE_SCENARIO_SIMULATOR, is_feature_flag_enabled
from utils.scenario_controller_v2 import (
    ScenarioControllerV2Error,
    ScenarioControllerV2StaleSessionError,
)
from utils.scenario_simulator_ui_v2 import (
    format_scene_progress_caption,
    inject_ba_simulator_css,
    mission_text_from_content,
    option_card_label,
    render_conversation_html,
    render_decision_brief_control,
    render_notes_artifacts_help,
    render_scene_context_and_mission,
    render_scene_image_panel,
    render_simulator_footer_tip,
    render_simulator_header,
    resolve_scene_image_for_view,
    scene_progress_from_content,
)
from utils.scenario_streamlit_v2 import (
    CB_SC001_SCENARIO_IDENTIFIER,
    MSG_PENDING_RETRY,
    MSG_SCENARIO_UNAVAILABLE,
    MSG_SELECT_OPTION,
    ScenarioStreamlitV2Error,
    UiMessageKind,
    WIDGET_KEY_CHOICE,
    WIDGET_KEY_FORM,
    WIDGET_KEY_RETRY,
    WIDGET_KEY_RETURN,
    WIDGET_KEY_START_NEW,
    build_trusted_identity_v2,
    fetch_authoritative_cb_sc001_view,
    has_pending_submission,
    load_cb_sc001_v2_content,
    map_controller_error_to_ui_message,
    prepare_return_to_practice_navigation,
    read_ui_message,
    resolve_cb_sc001_scenario_version_id,
    start_new_cb_sc001_attempt_v2,
    submit_cb_sc001_v2_choice,
)
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.user_errors import log_and_get_user_message

st.set_page_config(
    page_title="BA Scenario Simulator",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="collapsed",
)

if not is_feature_flag_enabled(CERTBOUND_ENABLE_SCENARIO_SIMULATOR):
    render_app_chrome()
    st.info("The Scenario Simulator is not available yet.")
    st.stop()

render_app_chrome()
enforce_session_timeout()
show_session_expired_notice()
inject_certbound_theme()
inject_ba_simulator_css()


def _require_premium_learner_email() -> str:
    require_paid_access("Scenario Simulator V2")
    email = get_current_user_email()
    if not email:
        st.warning("Please log in again to continue.")
        st.stop()
    return email


def _navigate_return_to_practice() -> None:
    """Clear only V2 scenario keys and switch to the registered Practice page."""
    destination = prepare_return_to_practice_navigation(st.session_state)
    st.switch_page(destination)


def _render_unavailable(message: str) -> None:
    # Do not use HTML href links here: with multipage sidebar navigation
    # disabled, raw ``pages/Practice.py`` hrefs open a blank page.
    render_empty_state("Scenario unavailable", message)
    if st.button("Return to Practice", key=f"{WIDGET_KEY_RETURN}_unavailable"):
        _navigate_return_to_practice()


def _render_ui_message() -> None:
    message = read_ui_message(st.session_state)
    if message is None:
        return
    if message.kind is UiMessageKind.WARNING:
        st.warning(message.text)
    elif message.kind is UiMessageKind.ERROR:
        st.error(message.text)
    else:
        st.info(message.text)


def _render_active_scene(*, serialized: dict, scenario_title: str, content) -> None:
    scene = serialized.get("currentScene")
    if not isinstance(scene, dict):
        _render_unavailable("The scenario could not be loaded. Please try again.")
        st.stop()

    scene_id = str(scene.get("sceneId") or "")
    progress_index, progress_total = scene_progress_from_content(content, scene_id)
    progress_caption = format_scene_progress_caption(progress_index, progress_total)

    st.markdown('<div class="cb-sim-shell">', unsafe_allow_html=True)
    render_simulator_header(
        scenario_title=scenario_title,
        status_label="In Progress",
        progress_caption=progress_caption,
        progress_index=progress_index,
        progress_total=progress_total,
        complete=False,
    )
    render_notes_artifacts_help(content=content, scene=scene)
    render_scene_context_and_mission(
        content=content,
        scene=scene,
        progress_index=progress_index,
    )

    image_col, conversation_col = st.columns([1.55, 1], gap="medium")
    with image_col:
        st.markdown('<div class="cb-sim-image-wrap">', unsafe_allow_html=True)
        render_scene_image_panel(resolve_scene_image_for_view(scene))
        st.markdown("</div>", unsafe_allow_html=True)
    with conversation_col:
        st.markdown(render_conversation_html(scene), unsafe_allow_html=True)

    prompt = scene.get("decisionPrompt")
    prompt_text = prompt.strip() if isinstance(prompt, str) else ""
    if prompt_text:
        st.markdown(
            f'<div class="cb-sim-choices-heading">{html.escape(prompt_text)}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<div class="cb-sim-choices-heading">How would you respond?</div>', unsafe_allow_html=True)

    options = scene.get("options") or []
    option_ids = []
    option_labels: dict[str, str] = {}
    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id") or "").strip()
        if not option_id:
            continue
        option_ids.append(option_id)
        option_labels[option_id] = option_card_label(option)

    if has_pending_submission(st.session_state):
        st.warning(MSG_PENDING_RETRY)
        if st.button("Retry submission", key=WIDGET_KEY_RETRY):
            _handle_submit(retry_pending=True)
        st.markdown("</div>", unsafe_allow_html=True)
        return

    with st.form(key=WIDGET_KEY_FORM):
        selected_option_id: Optional[str] = None
        if option_ids:
            selected_option_id = st.radio(
                "Choose one option:",
                option_ids,
                format_func=lambda option_id: option_labels.get(option_id, option_id),
                key=WIDGET_KEY_CHOICE,
            )
        submitted = st.form_submit_button("Submit Decision", type="primary", disabled=not option_ids)
    if submitted:
        if not selected_option_id:
            st.warning(MSG_SELECT_OPTION)
        else:
            _handle_submit(selected_option_id=selected_option_id, retry_pending=False)

    footer_left, footer_right = st.columns([2.2, 1])
    with footer_left:
        render_simulator_footer_tip()
    with footer_right:
        render_decision_brief_control(content)
    st.markdown("</div>", unsafe_allow_html=True)


def _handle_start_new_attempt() -> None:
    try:
        content = load_cb_sc001_v2_content()
        client = get_supabase_admin_client()
        identity = build_trusted_identity_v2(user_email=_learner_email, supabase_client=client)
        scenario_version_id = resolve_cb_sc001_scenario_version_id(client, content=content)
        start_new_cb_sc001_attempt_v2(
            content=content,
            identity=identity,
            session_state=st.session_state,
            scenario_version_id=scenario_version_id,
        )
    except ScenarioControllerV2Error as exc:
        message = map_controller_error_to_ui_message(exc)
        log_and_get_user_message(
            "Scenario Simulator V2: controller failure during Start New Attempt",
            message.text,
            exc=exc,
        )
        st.warning(message.text)
        return
    except ScenarioStreamlitV2Error as exc:
        safe_text = str(exc) if str(exc).strip() else MSG_SCENARIO_UNAVAILABLE
        log_and_get_user_message(
            "Scenario Simulator V2: streamlit failure during Start New Attempt",
            safe_text,
            exc=exc,
        )
        st.warning(safe_text)
        return
    except Exception as exc:  # noqa: BLE001 - sanitized at page boundary
        message = log_and_get_user_message(
            "Scenario Simulator V2: unexpected Start New Attempt failure",
            "The scenario could not be started. Please try again.",
            exc=exc,
        )
        st.warning(message)
        return
    st.rerun()


def _render_terminal_result(*, serialized: dict, scenario_title: str, content) -> None:
    terminal = serialized.get("terminalResult")
    if not isinstance(terminal, dict):
        _render_unavailable("The scenario could not be loaded. Please try again.")
        st.stop()

    total_scenes = 0
    if content is not None:
        scenes = content.document.get("scenes") or ()
        if isinstance(scenes, (list, tuple)):
            total_scenes = len(scenes)
    st.markdown('<div class="cb-sim-shell">', unsafe_allow_html=True)
    render_simulator_header(
        scenario_title=scenario_title,
        status_label="Complete",
        progress_caption="Scenario complete",
        progress_index=total_scenes,
        progress_total=total_scenes,
        complete=True,
    )

    outcome_title = str(terminal.get("outcomeTitle") or "Scenario complete").strip()
    narrative = terminal.get("narrative")
    display_score = terminal.get("displayScore")

    score_html = ""
    if display_score is not None and str(display_score).strip() != "":
        score_html = (
            f'<div class="cb-sim-terminal__score">Score: '
            f"{html.escape(str(display_score))}</div>"
        )

    narrative_html = ""
    if isinstance(narrative, str) and narrative.strip():
        narrative_html = f"<p>{html.escape(narrative.strip())}</p>"

    # Mission summary for results context — still from document, not hard-coded plot.
    mission = mission_text_from_content(content, "") if content is not None else ""
    mission_html = f"<p><em>{html.escape(mission)}</em></p>" if mission else ""

    st.markdown(
        f"""
<div class="cb-sim-terminal">
  <span class="cb-sim-kicker">RESULTS</span>
  <h2 style="margin:0.35rem 0 0.5rem 0;">{html.escape(outcome_title)}</h2>
  {score_html}
  {narrative_html}
  {mission_html}
</div>
        """,
        unsafe_allow_html=True,
    )

    start_col, return_col = st.columns(2)
    with start_col:
        if st.button("Start New Attempt", key=WIDGET_KEY_START_NEW, type="primary"):
            _handle_start_new_attempt()
    with return_col:
        if st.button("Return to Practice", key=WIDGET_KEY_RETURN):
            _navigate_return_to_practice()
    st.markdown("</div>", unsafe_allow_html=True)


def _handle_submit(*, selected_option_id: Optional[str] = None, retry_pending: bool) -> None:
    try:
        content = load_cb_sc001_v2_content()
        client = get_supabase_admin_client()
        identity = build_trusted_identity_v2(user_email=_learner_email, supabase_client=client)
        scenario_version_id = resolve_cb_sc001_scenario_version_id(client, content=content)
        outcome = submit_cb_sc001_v2_choice(
            content=content,
            identity=identity,
            session_state=st.session_state,
            scenario_version_id=scenario_version_id,
            selected_option_id=selected_option_id,
            retry_pending=retry_pending,
        )
    except ScenarioControllerV2Error as exc:
        message = map_controller_error_to_ui_message(exc)
        log_and_get_user_message(
            "Scenario Simulator V2: controller failure during submission",
            message.text,
            exc=exc,
        )
        st.warning(message.text)
        return
    except Exception as exc:  # noqa: BLE001 - sanitized at page boundary
        message = log_and_get_user_message(
            "Scenario Simulator V2: unexpected submission failure",
            "Your selection could not be saved. Please try again.",
            exc=exc,
        )
        st.warning(message)
        return

    if outcome.ui_message is not None:
        if outcome.ui_message.kind is UiMessageKind.WARNING:
            st.warning(outcome.ui_message.text)
        elif outcome.ui_message.kind is UiMessageKind.ERROR:
            st.error(outcome.ui_message.text)
        else:
            st.info(outcome.ui_message.text)
    if outcome.conclusive and not outcome.stale_session:
        st.rerun()


_learner_email = _require_premium_learner_email()

_render_ui_message()

try:
    _content = load_cb_sc001_v2_content()
    _client = get_supabase_admin_client()
    _identity = build_trusted_identity_v2(user_email=_learner_email, supabase_client=_client)
    _scenario_version_id = resolve_cb_sc001_scenario_version_id(_client, content=_content)
    _view = fetch_authoritative_cb_sc001_view(
        content=_content,
        identity=_identity,
        session_state=st.session_state,
        scenario_version_id=_scenario_version_id,
    )
except ScenarioControllerV2Error as exc:
    # Ordinary refresh/resume must not surface the submission-only stale banner
    # as a hard unavailable page. Stale-session text is reserved for submit CAS.
    if isinstance(exc, ScenarioControllerV2StaleSessionError):
        safe_text = MSG_SCENARIO_UNAVAILABLE
    else:
        safe_text = map_controller_error_to_ui_message(exc).text
    log_and_get_user_message(
        "Scenario Simulator V2: controller failure during authoritative load",
        safe_text,
        exc=exc,
    )
    _render_unavailable(safe_text)
    st.stop()
except Exception as exc:  # noqa: BLE001 - sanitized at page boundary
    message = log_and_get_user_message(
        "Scenario Simulator V2: unexpected load failure",
        "The scenario could not be loaded. Please try again.",
        exc=exc,
    )
    _render_unavailable(message)
    st.stop()

if _view.serialized.get("isComplete"):
    _render_terminal_result(
        serialized=_view.serialized,
        scenario_title=_view.scenario_title,
        content=_content,
    )
else:
    _render_active_scene(
        serialized=_view.serialized,
        scenario_title=_view.scenario_title,
        content=_content,
    )

st.caption(
    f"Independent exam-prep platform. Not affiliated with Salesforce. "
    f"({CB_SC001_SCENARIO_IDENTIFIER})"
)
