"""SIM-STREAMLIT-V2-01: isolated Engine V2 CB-SC-001 Streamlit vertical slice.

This page is intentionally separate from ``pages/Scenario_Simulator.py`` (Engine
V1 / BA-201). It uses the frozen Option B session contract implemented in
``utils.scenario_streamlit_v2`` and the Engine V2 controller in
``utils.scenario_controller_v2``.
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

from utils.access_control import (
    get_current_user_email,
    get_supabase_admin_client,
    render_app_chrome,
    require_paid_access,
)
from utils.dashboard_components import inject_certbound_theme, render_empty_state, render_page_header
from utils.navigation import CERTBOUND_ENABLE_SCENARIO_SIMULATOR, is_feature_flag_enabled
from utils.scenario_controller_v2 import ScenarioControllerV2Error
from utils.scenario_streamlit_v2 import (
    CB_SC001_CERTIFICATION_EXAM_NAME,
    CB_SC001_SCENARIO_IDENTIFIER,
    MSG_PENDING_RETRY,
    MSG_SELECT_OPTION,
    UiMessageKind,
    WIDGET_KEY_CHOICE,
    WIDGET_KEY_FORM,
    WIDGET_KEY_RETRY,
    WIDGET_KEY_RETURN,
    build_trusted_identity_v2,
    clear_cosmetic_ui_state,
    clear_v2_session_keys,
    extract_progress_label,
    extract_scene_heading,
    fetch_authoritative_cb_sc001_view,
    has_pending_submission,
    load_cb_sc001_v2_content,
    map_controller_error_to_ui_message,
    read_ui_message,
    resolve_cb_sc001_scenario_version_id,
    submit_cb_sc001_v2_choice,
)
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.user_errors import log_and_get_user_message

st.set_page_config(
    page_title="Scenario Simulator V2",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)

if not is_feature_flag_enabled(CERTBOUND_ENABLE_SCENARIO_SIMULATOR):
    render_app_chrome()
    st.info("The Scenario Simulator is not available yet.")
    st.stop()

render_app_chrome()
enforce_session_timeout()
show_session_expired_notice()
inject_certbound_theme()


def _require_premium_learner_email() -> str:
    require_paid_access("Scenario Simulator V2")
    email = get_current_user_email()
    if not email:
        st.warning("Please log in again to continue.")
        st.stop()
    return email


def _render_unavailable(message: str) -> None:
    render_empty_state(
        "Scenario unavailable",
        message,
        action_label="Return to Practice",
        action_href="pages/Practice.py",
    )


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


def _render_dialogue(scene: dict) -> None:
    exchanges = scene.get("dialogueExchanges") or []
    if isinstance(exchanges, list):
        for exchange in exchanges:
            if not isinstance(exchange, dict):
                continue
            speaker = exchange.get("speakerDisplayName") or exchange.get("speakerId") or "Speaker"
            text = exchange.get("text") or exchange.get("dialogue") or ""
            if text:
                st.markdown(f"**{speaker}:** {text}")
    setting = scene.get("setting")
    if isinstance(setting, str) and setting.strip():
        st.caption(setting)


def _render_active_scene(*, serialized: dict, scenario_title: str) -> None:
    scene = serialized.get("currentScene")
    if not isinstance(scene, dict):
        _render_unavailable("The scenario could not be loaded. Please try again.")
        st.stop()

    st.markdown(f"### {scenario_title}")
    st.caption(extract_progress_label(serialized))
    _render_dialogue(scene)

    prompt = scene.get("decisionPrompt")
    if isinstance(prompt, str) and prompt.strip():
        st.markdown(f"**{prompt.strip()}**")

    options = scene.get("options") or []
    option_ids = []
    option_labels: dict[str, str] = {}
    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("id") or "").strip()
        if not option_id:
            continue
        label = str(option.get("title") or option.get("text") or option_id).strip()
        option_ids.append(option_id)
        option_labels[option_id] = label

    if has_pending_submission(st.session_state):
        st.warning(MSG_PENDING_RETRY)
        if st.button("Retry submission", key=WIDGET_KEY_RETRY):
            _handle_submit(retry_pending=True)
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
        submitted = st.form_submit_button("Submit Decision", disabled=not option_ids)
    if submitted:
        if not selected_option_id:
            st.warning(MSG_SELECT_OPTION)
        else:
            _handle_submit(selected_option_id=selected_option_id, retry_pending=False)


def _render_terminal_result(*, serialized: dict, scenario_title: str) -> None:
    terminal = serialized.get("terminalResult")
    if not isinstance(terminal, dict):
        _render_unavailable("The scenario could not be loaded. Please try again.")
        st.stop()
    st.markdown(f"### {scenario_title}")
    st.caption("Scenario complete")
    st.markdown(f"**{terminal.get('outcomeTitle') or 'Scenario complete'}**")
    narrative = terminal.get("narrative")
    if isinstance(narrative, str) and narrative.strip():
        st.write(narrative)
    display_score = terminal.get("displayScore")
    if isinstance(display_score, str) and display_score.strip():
        st.write(display_score)
    if st.button("Return to Practice", key=WIDGET_KEY_RETURN):
        clear_cosmetic_ui_state(st.session_state)
        clear_v2_session_keys(st.session_state)
        st.switch_page("pages/Practice.py")


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

render_page_header(
    "Scenario Simulator V2",
    description=(
        f"Engine V2 learner preview for {CB_SC001_SCENARIO_IDENTIFIER}. "
        "Select one visible option and submit your decision."
    ),
    badge="Engine V2 preview",
    certification_name=CB_SC001_CERTIFICATION_EXAM_NAME,
)

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
    message = map_controller_error_to_ui_message(exc)
    log_and_get_user_message(
        "Scenario Simulator V2: controller failure during authoritative load",
        message.text,
        exc=exc,
    )
    _render_unavailable(message.text)
    st.stop()
except Exception as exc:  # noqa: BLE001 - sanitized at page boundary
    message = log_and_get_user_message(
        "Scenario Simulator V2: unexpected load failure",
        "The scenario could not be loaded. Please try again.",
        exc=exc,
    )
    _render_unavailable(message)
    st.stop()

_heading = extract_scene_heading(_view.serialized, fallback_title=_view.scenario_title)
if _view.serialized.get("isComplete"):
    _render_terminal_result(serialized=_view.serialized, scenario_title=_view.scenario_title)
else:
    _render_active_scene(
        serialized=_view.serialized,
        scenario_title=_view.scenario_title,
    )

st.caption("Independent exam-prep platform. Not affiliated with Salesforce.")
