"""SIM-STREAMLIT-V2-01 / CORRECTION-01: Streamlit integration for Engine V2 CB-SC-001.

Frozen Option B session-state contract:

- store only ``attempt_id``, optional non-authoritative scenario_version_id,
  pending submission retry metadata, and cosmetic UI values;
- resume/replay from persistence on every authoritative rerun;
- never store ``LearnerScenarioControllerStateV2``, orchestration results,
  Supabase clients, or credentials in session state.

Production content loads exclusively from ``scenario_content/`` — never from
``tests/``.
"""

from __future__ import annotations

import json
import pickle
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from utils.scenario_controller_v2 import (
    LearnerIdentityContextV2,
    LearnerScenarioControllerResultV2,
    ScenarioControllerV2AttemptNotFoundError,
    ScenarioControllerV2CorruptedAttemptError,
    ScenarioControllerV2DecisionConflictError,
    ScenarioControllerV2Error,
    ScenarioControllerV2InvalidIdentityError,
    ScenarioControllerV2InvalidRequestError,
    ScenarioControllerV2PersistenceUnavailableError,
    ScenarioControllerV2ScenarioUnavailableError,
    ScenarioControllerV2StaleSessionError,
    ScenarioControllerV2TerminalAttemptError,
    ScenarioControllerV2UnauthenticatedError,
    ScenarioControllerV2UnexpectedInternalError,
    resume_learner_scenario_v2,
    serialize_learner_controller_result_v2,
    start_or_resume_learner_scenario_v2,
    submit_learner_scenario_choice_v2,
)
from utils.scenario_engine_v2 import ScenarioContentV2, build_scenario_content_v2
from utils.scenario_orchestration_v2 import ScenarioOrchestrationV2PersistencePort
from utils.scenario_schema import REPO_ROOT, load_json_document

# ---------------------------------------------------------------------------
# CB-SC-001 canonical production identity
# ---------------------------------------------------------------------------

CB_SC001_SCENARIO_IDENTIFIER = "CB-SC-001"
CB_SC001_SIMULATION_ID = "cb-sc-001-onboarding-handoff-vslice"
CB_SC001_CERTIFICATION_EXAM_NAME = "Salesforce Certified Business Analyst"
CB_SC001_SEMANTIC_VERSION = "0.2.1-vslice-engine-v2"
CB_SC001_SCHEMA_VERSION = "1.1.0"
CB_SC001_CANONICAL_CONTENT_SHA256 = (
    "c74d61c42dbb2b0c34e6b84f722815d42b4fa4fe6e3aabebce18d46d6b4db551"
)

# Canonical production asset (packaged with the application; never under tests/).
CB_SC001_CONTENT_PATH = (
    REPO_ROOT
    / "scenario_content"
    / "business_analyst"
    / "cb_sc_001_onboarding_handoff_v1_1_0.json"
)

# ---------------------------------------------------------------------------
# Dedicated V2 session keys (no V1 collisions)
# ---------------------------------------------------------------------------

SESSION_KEY_ATTEMPT_ID = "cb_sc001_v2_attempt_id"
SESSION_KEY_SCENARIO_VERSION_ID = "cb_sc001_v2_scenario_version_id"
SESSION_KEY_PENDING_IDEMPOTENCY_KEY = "cb_sc001_v2_pending_idempotency_key"
SESSION_KEY_PENDING_OPTION_ID = "cb_sc001_v2_pending_option_id"
SESSION_KEY_UI_MESSAGE = "cb_sc001_v2_ui_message"
SESSION_KEY_UI_MESSAGE_KIND = "cb_sc001_v2_ui_message_kind"

ALLOWED_SESSION_KEYS = frozenset(
    {
        SESSION_KEY_ATTEMPT_ID,
        SESSION_KEY_SCENARIO_VERSION_ID,
        SESSION_KEY_PENDING_IDEMPOTENCY_KEY,
        SESSION_KEY_PENDING_OPTION_ID,
        SESSION_KEY_UI_MESSAGE,
        SESSION_KEY_UI_MESSAGE_KIND,
    }
)

FORBIDDEN_SESSION_KEY_SUBSTRINGS = (
    "submission_context",
    "learner_view",
    "supabase",
    "service_role",
    "controller_state",
    "orchestration",
    "content_hash",
    "canonical_content",
)

# Stable page-local widget keys — never include attempt UUID (or a hash of it).
WIDGET_KEY_FORM = "cb_sc001_v2_widget_form"
WIDGET_KEY_CHOICE = "cb_sc001_v2_widget_choice"
WIDGET_KEY_RETRY = "cb_sc001_v2_widget_retry"
WIDGET_KEY_RETURN = "cb_sc001_v2_widget_return"

WIDGET_KEYS = (
    WIDGET_KEY_FORM,
    WIDGET_KEY_CHOICE,
    WIDGET_KEY_RETRY,
    WIDGET_KEY_RETURN,
)

# ---------------------------------------------------------------------------
# Learner-safe UI messages
# ---------------------------------------------------------------------------

MSG_UNAUTHENTICATED = "Please log in from the Account page before continuing."
MSG_SCENARIO_UNAVAILABLE = "The scenario could not be loaded. Please try again."
MSG_SELECTION_NOT_SAVED = "Your selection could not be saved. Please try again."
MSG_STALE_SESSION = "Your scenario session changed. Reloading the latest progress."
MSG_ALREADY_COMPLETE = "This scenario attempt is already complete."
MSG_GENERIC_FAILURE = "The Scenario Simulator is temporarily unavailable. Please try again shortly."
MSG_SELECT_OPTION = "Please select an option before submitting."
MSG_PENDING_RETRY = "Your last submission has not been confirmed yet. Select Retry submission to try again."
MSG_PROGRESS_IN_PROGRESS = "Scenario in progress"
MSG_PROGRESS_COMPLETE = "Scenario complete"


class UiMessageKind(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class UiMessage:
    text: str
    kind: UiMessageKind = UiMessageKind.INFO


@dataclass(frozen=True)
class CbSc001AuthoritativeView:
    """Ephemeral, rerun-safe learner projection for one script pass."""

    serialized: Dict[str, Any]
    attempt_id: str
    is_new_attempt: bool
    scenario_title: str


@dataclass(frozen=True)
class CbSc001SubmitOutcome:
    serialized: Dict[str, Any]
    attempt_id: str
    idempotency_key: str
    conclusive: bool
    ui_message: Optional[UiMessage] = None
    stale_session: bool = False


@dataclass(frozen=True)
class CbSc001OwnerReadinessResult:
    """Server-side publication readiness — never start an attempt.

    ``findings`` are owner/developer codes only; learner UI must not render them.
    """

    ready: bool
    simulation_id: str
    expected_version: str
    expected_canonical_content_sha256: str
    findings: Tuple[str, ...]
    target_appears_non_production: Optional[bool] = None
    published_scenario_version_id: Optional[str] = None


class ScenarioStreamlitV2Error(Exception):
    """Base error for the Streamlit V2 integration layer."""


class ScenarioStreamlitV2UnauthenticatedError(ScenarioStreamlitV2Error):
    """Trusted server-side identity is missing."""


class ScenarioStreamlitV2ScenarioUnavailableError(ScenarioStreamlitV2Error):
    """Approved CB-SC-001 content or published version could not be resolved."""


def production_content_path_is_non_test(path: Any) -> bool:
    """Return True when ``path`` does not resolve under the repository tests/ tree."""
    try:
        resolved = path if isinstance(path, type(CB_SC001_CONTENT_PATH)) else type(CB_SC001_CONTENT_PATH)(path)
        resolved = resolved.resolve()
        tests_root = (REPO_ROOT / "tests").resolve()
        return tests_root not in resolved.parents and resolved != tests_root
    except Exception:  # noqa: BLE001
        return False


def load_cb_sc001_v2_content() -> ScenarioContentV2:
    """Load and validate the canonical production CB-SC-001 asset."""
    if not production_content_path_is_non_test(CB_SC001_CONTENT_PATH):
        raise ScenarioStreamlitV2ScenarioUnavailableError(MSG_SCENARIO_UNAVAILABLE)
    if not CB_SC001_CONTENT_PATH.is_file():
        raise ScenarioStreamlitV2ScenarioUnavailableError(MSG_SCENARIO_UNAVAILABLE)
    document = load_json_document(CB_SC001_CONTENT_PATH)
    content = build_scenario_content_v2(document, source_path=CB_SC001_CONTENT_PATH)
    if content.simulation_id != CB_SC001_SIMULATION_ID:
        raise ScenarioStreamlitV2ScenarioUnavailableError(MSG_SCENARIO_UNAVAILABLE)
    if content.version != CB_SC001_SEMANTIC_VERSION:
        raise ScenarioStreamlitV2ScenarioUnavailableError(MSG_SCENARIO_UNAVAILABLE)
    if content.schema_version != CB_SC001_SCHEMA_VERSION:
        raise ScenarioStreamlitV2ScenarioUnavailableError(MSG_SCENARIO_UNAVAILABLE)
    if content.canonical_content_sha256 != CB_SC001_CANONICAL_CONTENT_SHA256:
        raise ScenarioStreamlitV2ScenarioUnavailableError(MSG_SCENARIO_UNAVAILABLE)
    cert_name = str(content.document.get("certificationExamName") or "").strip()
    if cert_name != CB_SC001_CERTIFICATION_EXAM_NAME:
        raise ScenarioStreamlitV2ScenarioUnavailableError(MSG_SCENARIO_UNAVAILABLE)
    return content


def build_trusted_identity_v2(*, user_email: str, supabase_client: Any) -> LearnerIdentityContextV2:
    """Construct server-side identity — never accept browser-controlled email."""
    if not isinstance(user_email, str) or not user_email.strip():
        raise ScenarioStreamlitV2UnauthenticatedError(MSG_UNAUTHENTICATED)
    return LearnerIdentityContextV2(user_email=user_email, supabase_client=supabase_client)


def _target_appears_non_production(supabase_url: Optional[str]) -> Optional[bool]:
    if not isinstance(supabase_url, str) or not supabase_url.strip():
        return None
    raw = supabase_url.strip().lower()
    host = urlparse(raw).hostname or raw
    non_prod_markers = (
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "staging",
        "stg.",
        "local",
        "supabase.internal",
    )
    prod_markers = ("prod.", "production", "api.certbound")
    if any(marker in host for marker in prod_markers):
        return False
    if any(marker in host for marker in non_prod_markers):
        return True
    return None


def diagnose_cb_sc001_publication_readiness(
    client: Any,
    *,
    content: Optional[ScenarioContentV2] = None,
    supabase_url: Optional[str] = None,
) -> CbSc001OwnerReadinessResult:
    """Verify published DB identity matches canonical local content without starting an attempt."""
    findings: list[str] = []
    published_version_id: Optional[str] = None
    try:
        loaded = content if content is not None else load_cb_sc001_v2_content()
    except ScenarioStreamlitV2Error:
        findings.append("canonical_content_unavailable")
        return CbSc001OwnerReadinessResult(
            ready=False,
            simulation_id=CB_SC001_SIMULATION_ID,
            expected_version=CB_SC001_SEMANTIC_VERSION,
            expected_canonical_content_sha256=CB_SC001_CANONICAL_CONTENT_SHA256,
            findings=tuple(findings),
            target_appears_non_production=_target_appears_non_production(supabase_url),
        )

    try:
        scenario_rows = (
            client.table("scenarios")
            .select("id,is_active,current_published_version_id,simulation_id")
            .eq("simulation_id", loaded.simulation_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001 - owner diagnostic must not leak raw errors
        findings.append("scenarios_lookup_failed")
        return CbSc001OwnerReadinessResult(
            ready=False,
            simulation_id=loaded.simulation_id,
            expected_version=loaded.version,
            expected_canonical_content_sha256=loaded.canonical_content_sha256,
            findings=tuple(findings),
            target_appears_non_production=_target_appears_non_production(supabase_url),
        )

    if not scenario_rows:
        findings.append("scenario_catalog_entry_missing")
    else:
        scenario_row = scenario_rows[0]
        if not scenario_row.get("is_active"):
            findings.append("scenario_inactive")
        published_version_id = scenario_row.get("current_published_version_id")
        if not published_version_id:
            findings.append("published_version_missing")
        else:
            try:
                version_rows = (
                    client.table("scenario_versions")
                    .select("id,version,canonical_content_sha256,lifecycle_status,scenario_id")
                    .eq("id", published_version_id)
                    .eq("scenario_id", scenario_row["id"])
                    .limit(1)
                    .execute()
                ).data or []
            except Exception:  # noqa: BLE001
                findings.append("scenario_versions_lookup_failed")
                version_rows = []
            if not version_rows:
                findings.append("published_version_row_missing")
            else:
                version_row = version_rows[0]
                lifecycle = str(version_row.get("lifecycle_status") or "").strip().lower()
                if lifecycle and lifecycle != "published":
                    findings.append("published_version_not_published")
                if version_row.get("version") != loaded.version:
                    findings.append("semantic_version_mismatch")
                persisted_hash = str(version_row.get("canonical_content_sha256") or "").strip().lower()
                if persisted_hash != loaded.canonical_content_sha256:
                    findings.append("canonical_content_hash_mismatch")

    target_flag = _target_appears_non_production(supabase_url)
    if target_flag is False:
        findings.append("target_appears_production")

    return CbSc001OwnerReadinessResult(
        ready=not findings,
        simulation_id=loaded.simulation_id,
        expected_version=loaded.version,
        expected_canonical_content_sha256=loaded.canonical_content_sha256,
        findings=tuple(findings),
        target_appears_non_production=target_flag,
        published_scenario_version_id=str(published_version_id) if published_version_id else None,
    )


def resolve_cb_sc001_scenario_version_id(client: Any, *, content: ScenarioContentV2) -> str:
    """Resolve published ``scenario_versions.id`` after version + hash verification."""
    readiness = diagnose_cb_sc001_publication_readiness(client, content=content)
    # Learner path ignores production-target heuristic (owner-only advisory).
    learner_blocking = tuple(f for f in readiness.findings if f != "target_appears_production")
    if learner_blocking or not readiness.published_scenario_version_id:
        raise ScenarioStreamlitV2ScenarioUnavailableError(MSG_SCENARIO_UNAVAILABLE)
    return str(readiness.published_scenario_version_id)


def read_session_attempt_id(session_state: Mapping[str, Any]) -> Optional[str]:
    raw = session_state.get(SESSION_KEY_ATTEMPT_ID)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def write_session_attempt_id(session_state: Any, attempt_id: str) -> None:
    session_state[SESSION_KEY_ATTEMPT_ID] = str(attempt_id)


def write_session_scenario_version_id(session_state: Any, scenario_version_id: str) -> None:
    """Store non-authoritative validation metadata only (never selects content)."""
    session_state[SESSION_KEY_SCENARIO_VERSION_ID] = str(scenario_version_id)


def read_session_scenario_version_id(session_state: Mapping[str, Any]) -> Optional[str]:
    raw = session_state.get(SESSION_KEY_SCENARIO_VERSION_ID)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def clear_pending_submission_state(session_state: Any) -> None:
    session_state.pop(SESSION_KEY_PENDING_IDEMPOTENCY_KEY, None)
    session_state.pop(SESSION_KEY_PENDING_OPTION_ID, None)


def clear_cosmetic_ui_state(session_state: Any) -> None:
    clear_pending_submission_state(session_state)
    session_state.pop(SESSION_KEY_UI_MESSAGE, None)
    session_state.pop(SESSION_KEY_UI_MESSAGE_KIND, None)


def clear_v2_session_keys(session_state: Any) -> None:
    """Clear all CB-SC-001 V2 session keys (identity-change / quarantine)."""
    for key in ALLOWED_SESSION_KEYS:
        session_state.pop(key, None)


def set_ui_message(session_state: Any, message: UiMessage) -> None:
    session_state[SESSION_KEY_UI_MESSAGE] = message.text
    session_state[SESSION_KEY_UI_MESSAGE_KIND] = message.kind.value


def read_ui_message(session_state: Mapping[str, Any]) -> Optional[UiMessage]:
    text = session_state.get(SESSION_KEY_UI_MESSAGE)
    if not isinstance(text, str) or not text.strip():
        return None
    kind_raw = str(session_state.get(SESSION_KEY_UI_MESSAGE_KIND) or UiMessageKind.INFO.value)
    try:
        kind = UiMessageKind(kind_raw)
    except ValueError:
        kind = UiMessageKind.INFO
    return UiMessage(text=text, kind=kind)


def has_pending_submission(session_state: Mapping[str, Any]) -> bool:
    key = session_state.get(SESSION_KEY_PENDING_IDEMPOTENCY_KEY)
    option = session_state.get(SESSION_KEY_PENDING_OPTION_ID)
    return isinstance(key, str) and bool(key.strip()) and isinstance(option, str) and bool(option.strip())


def read_pending_submission(session_state: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    if not has_pending_submission(session_state):
        return None
    return (
        str(session_state[SESSION_KEY_PENDING_IDEMPOTENCY_KEY]).strip(),
        str(session_state[SESSION_KEY_PENDING_OPTION_ID]).strip(),
    )


def store_pending_submission(session_state: Any, *, idempotency_key: str, option_id: str) -> None:
    session_state[SESSION_KEY_PENDING_IDEMPOTENCY_KEY] = str(idempotency_key)
    session_state[SESSION_KEY_PENDING_OPTION_ID] = str(option_id)


def assert_option_b_session_state_compliant(session_state: Mapping[str, Any]) -> None:
    """Fail closed if forbidden authoritative objects appear in session state."""
    for key, value in session_state.items():
        if not str(key).startswith("cb_sc001_v2_"):
            continue
        lowered_key = str(key).lower()
        for forbidden in FORBIDDEN_SESSION_KEY_SUBSTRINGS:
            if forbidden in lowered_key:
                raise AssertionError(f"Forbidden session key present: {key!r}")
        if isinstance(value, LearnerScenarioControllerResultV2):
            raise AssertionError("LearnerScenarioControllerResultV2 must not be stored in session state")
        type_name = type(value).__name__
        if type_name in {"LearnerScenarioControllerStateV2", "LearnerIdentityContextV2"}:
            raise AssertionError(f"{type_name} must not be stored in session state")


def collect_cb_sc001_v2_session_keys(session_state: Mapping[str, Any]) -> Tuple[str, ...]:
    return tuple(sorted(key for key in session_state.keys() if str(key).startswith("cb_sc001_v2_")))


def streamlit_widget_keys() -> Tuple[str, ...]:
    return WIDGET_KEYS


def assert_widget_keys_exclude_attempt_id(attempt_id: str, keys: Sequence[str] = WIDGET_KEYS) -> None:
    if not attempt_id:
        return
    for key in keys:
        if attempt_id in str(key):
            raise AssertionError(f"Widget key must not contain attempt id: {key!r}")


def map_controller_error_to_ui_message(exc: ScenarioControllerV2Error) -> UiMessage:
    if isinstance(exc, ScenarioControllerV2UnauthenticatedError):
        return UiMessage(MSG_UNAUTHENTICATED, UiMessageKind.WARNING)
    if isinstance(exc, ScenarioControllerV2InvalidIdentityError):
        return UiMessage(MSG_UNAUTHENTICATED, UiMessageKind.WARNING)
    if isinstance(exc, ScenarioControllerV2ScenarioUnavailableError):
        return UiMessage(MSG_SCENARIO_UNAVAILABLE, UiMessageKind.ERROR)
    if isinstance(exc, ScenarioControllerV2AttemptNotFoundError):
        return UiMessage(MSG_SCENARIO_UNAVAILABLE, UiMessageKind.ERROR)
    if isinstance(exc, ScenarioControllerV2CorruptedAttemptError):
        return UiMessage(MSG_SCENARIO_UNAVAILABLE, UiMessageKind.ERROR)
    if isinstance(exc, ScenarioControllerV2StaleSessionError):
        return UiMessage(MSG_STALE_SESSION, UiMessageKind.INFO)
    if isinstance(exc, ScenarioControllerV2DecisionConflictError):
        return UiMessage(MSG_SELECTION_NOT_SAVED, UiMessageKind.WARNING)
    if isinstance(exc, ScenarioControllerV2PersistenceUnavailableError):
        return UiMessage(MSG_SELECTION_NOT_SAVED, UiMessageKind.WARNING)
    if isinstance(exc, ScenarioControllerV2TerminalAttemptError):
        return UiMessage(MSG_ALREADY_COMPLETE, UiMessageKind.INFO)
    if isinstance(exc, ScenarioControllerV2InvalidRequestError):
        return UiMessage(MSG_SELECTION_NOT_SAVED, UiMessageKind.WARNING)
    if isinstance(exc, ScenarioControllerV2UnexpectedInternalError):
        return UiMessage(MSG_GENERIC_FAILURE, UiMessageKind.ERROR)
    return UiMessage(MSG_GENERIC_FAILURE, UiMessageKind.ERROR)


def _validate_visible_option(serialized: Mapping[str, Any], selected_option_id: str) -> None:
    if not isinstance(selected_option_id, str) or not selected_option_id.strip():
        raise ScenarioControllerV2InvalidRequestError("invalid option")
    if serialized.get("isComplete"):
        raise ScenarioControllerV2TerminalAttemptError("terminal")
    scene = serialized.get("currentScene")
    if not isinstance(scene, Mapping):
        raise ScenarioControllerV2InvalidRequestError("invalid scene")
    options = scene.get("options")
    if not isinstance(options, Sequence):
        raise ScenarioControllerV2InvalidRequestError("invalid options")
    visible_ids = {str(option.get("id")) for option in options if isinstance(option, Mapping)}
    if selected_option_id not in visible_ids:
        raise ScenarioControllerV2InvalidRequestError("unknown option")


def _sync_non_authoritative_scenario_version_id(
    session_state: Any,
    *,
    authoritative_scenario_version_id: str,
) -> None:
    """Overwrite session metadata so a tampered value never sticks."""
    write_session_scenario_version_id(session_state, authoritative_scenario_version_id)


def fetch_authoritative_cb_sc001_view(
    *,
    content: ScenarioContentV2,
    identity: LearnerIdentityContextV2,
    session_state: Any,
    scenario_version_id: str,
    persistence: Optional[ScenarioOrchestrationV2PersistencePort] = None,
) -> CbSc001AuthoritativeView:
    """Option B authoritative load: resume when attempt_id exists, else start.

    ``scenario_version_id`` must already be the freshly resolved, hash-verified
    published version id. Session ``scenario_version_id`` is never trusted for
    authorization or content selection.
    """
    _sync_non_authoritative_scenario_version_id(
        session_state,
        authoritative_scenario_version_id=scenario_version_id,
    )
    attempt_id = read_session_attempt_id(session_state)
    if attempt_id:
        try:
            result = resume_learner_scenario_v2(
                content,
                identity=identity,
                attempt_id=attempt_id,
                persistence=persistence,
            )
        except ScenarioControllerV2AttemptNotFoundError:
            clear_v2_session_keys(session_state)
            raise
        is_new_attempt = False
    else:
        attempt_id = str(uuid.uuid4())
        result = start_or_resume_learner_scenario_v2(
            content,
            identity=identity,
            scenario_version_id=scenario_version_id,
            attempt_id=attempt_id,
            persistence=persistence,
        )
        write_session_attempt_id(session_state, result.state.attempt_id)
        is_new_attempt = True

    serialized = serialize_learner_controller_result_v2(result)
    scenario_title = str(content.document.get("title") or CB_SC001_SCENARIO_IDENTIFIER)
    return CbSc001AuthoritativeView(
        serialized=serialized,
        attempt_id=result.state.attempt_id,
        is_new_attempt=is_new_attempt,
        scenario_title=scenario_title,
    )


def submit_cb_sc001_v2_choice(
    *,
    content: ScenarioContentV2,
    identity: LearnerIdentityContextV2,
    session_state: Any,
    scenario_version_id: str,
    selected_option_id: Optional[str] = None,
    retry_pending: bool = False,
    persistence: Optional[ScenarioOrchestrationV2PersistencePort] = None,
) -> CbSc001SubmitOutcome:
    """Submit one visible option using resume-first authoritative state."""
    _sync_non_authoritative_scenario_version_id(
        session_state,
        authoritative_scenario_version_id=scenario_version_id,
    )
    attempt_id = read_session_attempt_id(session_state)
    if not attempt_id:
        raise ScenarioControllerV2InvalidRequestError("missing attempt")

    pending = read_pending_submission(session_state)
    if retry_pending:
        if pending is None:
            raise ScenarioControllerV2InvalidRequestError("missing pending submission")
        idempotency_key, option_id = pending
    else:
        if not isinstance(selected_option_id, str) or not selected_option_id.strip():
            raise ScenarioControllerV2InvalidRequestError("missing option")
        option_id = selected_option_id.strip()
        idempotency_key = str(uuid.uuid4())
        store_pending_submission(session_state, idempotency_key=idempotency_key, option_id=option_id)

    try:
        resumed = resume_learner_scenario_v2(
            content,
            identity=identity,
            attempt_id=attempt_id,
            persistence=persistence,
        )
    except ScenarioControllerV2AttemptNotFoundError:
        clear_v2_session_keys(session_state)
        raise

    serialized_before = serialize_learner_controller_result_v2(resumed)
    _validate_visible_option(serialized_before, option_id)

    try:
        submitted = submit_learner_scenario_choice_v2(
            content,
            identity=identity,
            state=resumed.state,
            selected_option_id=option_id,
            idempotency_key=idempotency_key,
            persistence=persistence,
        )
    except ScenarioControllerV2StaleSessionError as exc:
        clear_cosmetic_ui_state(session_state)
        message = map_controller_error_to_ui_message(exc)
        set_ui_message(session_state, message)
        refreshed = fetch_authoritative_cb_sc001_view(
            content=content,
            identity=identity,
            session_state=session_state,
            scenario_version_id=scenario_version_id,
            persistence=persistence,
        )
        return CbSc001SubmitOutcome(
            serialized=refreshed.serialized,
            attempt_id=refreshed.attempt_id,
            idempotency_key=idempotency_key,
            conclusive=False,
            ui_message=message,
            stale_session=True,
        )
    except ScenarioControllerV2AttemptNotFoundError:
        clear_v2_session_keys(session_state)
        raise
    except ScenarioControllerV2Error as exc:
        message = map_controller_error_to_ui_message(exc)
        if isinstance(exc, ScenarioControllerV2PersistenceUnavailableError):
            return CbSc001SubmitOutcome(
                serialized=serialized_before,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                conclusive=False,
                ui_message=message,
            )
        clear_pending_submission_state(session_state)
        set_ui_message(session_state, message)
        refreshed = fetch_authoritative_cb_sc001_view(
            content=content,
            identity=identity,
            session_state=session_state,
            scenario_version_id=scenario_version_id,
            persistence=persistence,
        )
        return CbSc001SubmitOutcome(
            serialized=refreshed.serialized,
            attempt_id=refreshed.attempt_id,
            idempotency_key=idempotency_key,
            conclusive=True,
            ui_message=message,
        )

    clear_pending_submission_state(session_state)
    session_state.pop(SESSION_KEY_UI_MESSAGE, None)
    session_state.pop(SESSION_KEY_UI_MESSAGE_KIND, None)
    serialized_after = serialize_learner_controller_result_v2(submitted)
    return CbSc001SubmitOutcome(
        serialized=serialized_after,
        attempt_id=submitted.state.attempt_id,
        idempotency_key=submitted.last_idempotency_key or idempotency_key,
        conclusive=True,
    )


def extract_scene_heading(serialized: Mapping[str, Any], *, fallback_title: str) -> str:
    if serialized.get("isComplete"):
        terminal = serialized.get("terminalResult")
        if isinstance(terminal, Mapping) and terminal.get("outcomeTitle"):
            return str(terminal["outcomeTitle"])
        return fallback_title
    scene = serialized.get("currentScene")
    if isinstance(scene, Mapping) and scene.get("title"):
        return str(scene["title"])
    return fallback_title


def extract_progress_label(serialized: Mapping[str, Any]) -> str:
    """Learner-safe progress text — never expose expectedSequenceNumber."""
    if serialized.get("isComplete"):
        return MSG_PROGRESS_COMPLETE
    scene = serialized.get("currentScene")
    if isinstance(scene, Mapping):
        progress = scene.get("progressMetadata")
        if isinstance(progress, Mapping):
            label = progress.get("progressLabel") or progress.get("label")
            if isinstance(label, str) and label.strip():
                return label.strip()
    return MSG_PROGRESS_IN_PROGRESS


def learner_safe_json_blob(serialized: Mapping[str, Any]) -> str:
    """Return a stable JSON blob for leakage checks in tests."""
    return json.dumps(serialized, sort_keys=True)


def controller_state_is_intentionally_not_serializable(state: Any) -> Tuple[bool, Optional[str]]:
    """Regression helper proving controller state remains in-process only."""
    try:
        pickle.dumps(state)
        pickle_failed = False
        pickle_error = None
    except Exception as exc:  # noqa: BLE001 - intentional probe
        pickle_failed = True
        pickle_error = f"{type(exc).__name__}: {exc}"
    try:
        json.dumps(state)
        json_failed = False
        json_error = None
    except Exception as exc:  # noqa: BLE001 - intentional probe
        json_failed = True
        json_error = f"{type(exc).__name__}: {exc}"
    if pickle_failed and json_failed:
        return True, pickle_error or json_error
    return False, None
