"""SIM-CONTROLLER-V2-01: isolated Engine V2 learner controller.

This module is the ONE application-facing boundary between a trusted
server-side caller (e.g. a future Streamlit V2 page -- not implemented by
this task) and:

- ``utils.scenario_orchestration_v2`` (``start_or_resume_scenario_run_v2``,
  ``submit_scenario_decision_v2``, ``resume_and_replay_scenario_run_v2``);
- ``utils.scenario_supabase_port_v2.SupabaseScenarioOrchestrationV2Port``
  (the concrete persistence port, injected via a trusted server-side
  Supabase client).

It exists so a future V2 page never has to import the orchestration or
persistence-port modules directly, never has to know the shape of a
``ScenarioRunV2Snapshot``/``ScenarioOrchestrationSubmissionContextV2``, and
never has to duplicate any replay, CAS, or idempotency logic those modules
already implement. This module performs NO engine execution, RPC
parameter construction, replay, or persistence logic of its own -- it only
sequences the existing orchestration API and translates its results (and
its failures) into one small, learner-safe result model
(:class:`LearnerScenarioControllerResultV2`) and one small, focused
exception hierarchy (``ScenarioControllerV2Error`` and subclasses).

Engine V1 isolation
--------------------
This module is fully independent of ``utils/scenario_learner_controller.py``
(the existing BA-201 Engine V1 controller). It does not import that module,
does not modify it, and nothing in that module imports this one. There is
no shared execution or session-state path between the two controllers.

Trusted identity boundary
--------------------------
Every public entry point requires a :class:`LearnerIdentityContextV2`
instance -- never a raw email string, dict, or any other client-controlled
shape. ``LearnerIdentityContextV2`` itself fails closed at construction
time:

- a missing/empty/non-string ``user_email`` raises
  :class:`ScenarioControllerV2UnauthenticatedError` (no learner session at
  all);
- a present-but-malformed ``user_email`` (fails the same
  ``lower(btrim(...))`` + ``"@"`` normalization every V1/V2 RPC already
  enforces, via the existing, unmodified
  ``utils.scenario_persistence.normalize_scenario_persistence_email``)
  raises :class:`ScenarioControllerV2InvalidIdentityError`;
- a missing ``supabase_client`` raises
  :class:`ScenarioControllerV2InvalidIdentityError`.

``user_email`` is stored ONLY in its already-normalized form after
construction (a frozen dataclass cannot be mutated afterward). This module
never reads a query parameter, form field, cookie, or any other
client-controlled value for identity -- the caller (a future page/session
layer) is entirely responsible for deriving ``user_email`` from its own
trusted, authenticated server-side session before constructing a
``LearnerIdentityContextV2``, exactly like
``utils.scenario_learner_controller``'s own ``user_email`` parameters
already document for Engine V1. This module also never reads an
environment variable and never constructs or caches a global Supabase
client -- ``supabase_client`` (or an explicit ``persistence`` override, see
below) must be supplied by the caller on every call.

Supabase port injection
-------------------------
Every public entry point accepts an optional ``persistence`` keyword
argument implementing ``ScenarioOrchestrationV2PersistencePort``. When
omitted, this module builds exactly one
``SupabaseScenarioOrchestrationV2Port(identity.supabase_client)`` for that
single call -- it never caches, pools, or reuses a port instance across
calls. Passing an explicit ``persistence`` (used throughout this module's
own unit tests via deterministic fakes) bypasses ``identity.supabase_client``
entirely for that call; ``identity.supabase_client`` is still required at
construction time regardless, so a caller can never accidentally end up
with an identity context that silently has no way to reach the database in
production use.

Controller state and attempt-id exposure
-------------------------------------------
:func:`start_or_resume_learner_scenario_v2`,
:func:`resume_learner_scenario_v2`, and
:func:`submit_learner_scenario_choice_v2` all return a
:class:`LearnerScenarioControllerResultV2`, whose ``state`` field
(:class:`LearnerScenarioControllerStateV2`) is the ONLY thing a caller needs
to retain (e.g. in trusted server-side session state) to make the next
call. It carries the trusted attempt id, the internal orchestration
submission context required for the next CAS-protected submission (``None``
once the attempt is complete -- a completed attempt can never be submitted
against again, enforced here even before calling the orchestration layer),
and the learner-safe view.

**Decision: ``attemptId`` is never included in
``serialize_learner_controller_result_v2(...)``'s output.** The attempt id
lives only in ``LearnerScenarioControllerStateV2.attempt_id``, which a
caller keeps in trusted server-side session state (mirroring
``utils.scenario_learner_controller.ScenarioAttemptView``'s own
``attempt_id`` field, which that module's docstring already flags as
"must never [be rendered] to the learner, or any other backend identifier,
directly"). This task's current application architecture has no
requirement for a client-visible opaque attempt identifier (there is no V2
Streamlit page yet), so the safer default -- keep it entirely server-side
-- is used rather than inventing a new client-facing identifier contract.
A future page that legitimately needs one can add it deliberately, with a
real threat-model discussion, rather than this controller exposing it by
default today.

Error contract
---------------
Every ``ScenarioControllerV2*Error`` message is a small, fixed, generic
string -- never derived from the underlying orchestration/port exception's
own text. RPC error prefixes (``sequence_mismatch:``, ...), raw database/
PostgREST text, UUID values, content hashes, stack traces, and hostnames
never reach a raised ``ScenarioControllerV2Error``'s message. The original
exception is always preserved as ``__cause__`` for server-side logging.
``KeyboardInterrupt``/``SystemExit`` are never caught (this module only
ever catches ``Exception``, which does not include either)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, TypeVar

from utils.scenario_engine_v2 import LearnerSceneView, ScenarioContentV2
from utils.scenario_orchestration_v2 import (
    ScenarioOrchestrationLearnerViewV2,
    ScenarioOrchestrationSubmissionContextV2,
    ScenarioOrchestrationV2CanonicalDecisionSequenceError,
    ScenarioOrchestrationV2Error,
    ScenarioOrchestrationV2IdempotencyConflictError,
    ScenarioOrchestrationV2IdentityMismatchError,
    ScenarioOrchestrationV2InvalidRequestError,
    ScenarioOrchestrationV2MalformedPersistenceResponseError,
    ScenarioOrchestrationV2PersistenceDependencyError,
    ScenarioOrchestrationV2PersistencePort,
    ScenarioOrchestrationV2ReplayMismatchError,
    ScenarioOrchestrationV2SceneConflictError,
    ScenarioOrchestrationV2SequenceConflictError,
    ScenarioOrchestrationV2StaleRunError,
    ScenarioOrchestrationV2TerminalMismatchError,
    _build_learner_view,
    _build_submission_context,
    resume_and_replay_scenario_run_v2,
    start_or_resume_scenario_run_v2,
    submit_scenario_decision_v2,
)
from utils.scenario_persistence import (
    ScenarioPersistenceValidationError,
    normalize_scenario_persistence_email,
)
from utils.scenario_supabase_port_v2 import SupabaseScenarioOrchestrationV2Port

__all__ = (
    # Errors
    "ScenarioControllerV2Error",
    "ScenarioControllerV2UnauthenticatedError",
    "ScenarioControllerV2InvalidIdentityError",
    "ScenarioControllerV2InvalidRequestError",
    "ScenarioControllerV2AttemptNotFoundError",
    "ScenarioControllerV2StaleSessionError",
    "ScenarioControllerV2DecisionConflictError",
    "ScenarioControllerV2ScenarioUnavailableError",
    "ScenarioControllerV2PersistenceUnavailableError",
    "ScenarioControllerV2CorruptedAttemptError",
    "ScenarioControllerV2TerminalAttemptError",
    "ScenarioControllerV2UnexpectedInternalError",
    # Identity + state + result
    "LearnerIdentityContextV2",
    "LearnerScenarioControllerStateV2",
    "LearnerScenarioControllerResultV2",
    # Public API
    "start_or_resume_learner_scenario_v2",
    "resume_learner_scenario_v2",
    "submit_learner_scenario_choice_v2",
    "serialize_learner_controller_result_v2",
)


# ---------------------------------------------------------------------------
# Learner-safe, stable public error messages
# ---------------------------------------------------------------------------
#
# Every message below is the ONLY text that may ever appear in the
# corresponding ScenarioControllerV2*Error's str(...) -- never string-
# interpolated with any part of the underlying orchestration/port
# exception. See the module docstring's "Error contract" section.

_UNAUTHENTICATED_MESSAGE = "You must be signed in to continue this scenario."
_INVALID_IDENTITY_MESSAGE = "Your session could not be verified. Please sign in again."
_INVALID_REQUEST_MESSAGE = "Your request could not be processed."
_ATTEMPT_NOT_FOUND_MESSAGE = "This scenario attempt could not be found."
_STALE_SESSION_MESSAGE = "Your scenario session is out of date. Reload and try again."
_DECISION_CONFLICT_MESSAGE = "Your selection could not be saved. Try again."
_SCENARIO_UNAVAILABLE_MESSAGE = "The scenario could not be loaded."
_PERSISTENCE_UNAVAILABLE_MESSAGE = "The scenario service is temporarily unavailable."
_CORRUPTED_ATTEMPT_MESSAGE = "This scenario attempt could not be restored."
_TERMINAL_ATTEMPT_MESSAGE = "This scenario attempt is already complete."
_UNEXPECTED_MESSAGE = "Something went wrong. Please try again."


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScenarioControllerV2Error(Exception):
    """Base error for the isolated Engine V2 learner controller."""


class ScenarioControllerV2UnauthenticatedError(ScenarioControllerV2Error):
    """No authenticated learner identity was supplied at all (missing/empty
    ``user_email``, or ``identity`` was not a :class:`LearnerIdentityContextV2`
    instance)."""


class ScenarioControllerV2InvalidIdentityError(ScenarioControllerV2Error):
    """An identity/session value was supplied but is not trustworthy: a
    malformed email, a missing required Supabase client, or an identity
    that no longer matches the one bound into a prior
    :class:`LearnerScenarioControllerStateV2`."""


class ScenarioControllerV2InvalidRequestError(ScenarioControllerV2Error):
    """Malformed caller input: missing/invalid controller state, a missing/
    empty/malformed selected option id, a malformed idempotency key, or any
    other orchestration-rejected invalid parameter that is not one of the
    more specific categories below."""


class ScenarioControllerV2AttemptNotFoundError(ScenarioControllerV2Error):
    """The requested attempt id does not exist, or does not belong to the
    authenticated learner -- deliberately indistinguishable, matching the
    underlying persistence layer's own "never distinguish not-found from
    not-owned" contract."""


class ScenarioControllerV2StaleSessionError(ScenarioControllerV2Error):
    """The caller's retained controller state (expected sequence number,
    expected scene, or cached identity) no longer matches the persisted
    attempt. The caller must discard the stale state and call
    :func:`resume_learner_scenario_v2` again rather than retry the same
    submission."""


class ScenarioControllerV2DecisionConflictError(ScenarioControllerV2Error):
    """A concurrent or conflicting decision was detected for this exact
    submission (a scene mismatch, or the same idempotency key reused for a
    genuinely different request). A conclusive rejection for this specific
    submission; the caller must reload fresh state before retrying."""


class ScenarioControllerV2ScenarioUnavailableError(ScenarioControllerV2Error):
    """The target scenario version does not exist, or is not currently
    published/available."""


class ScenarioControllerV2PersistenceUnavailableError(ScenarioControllerV2Error):
    """The underlying persistence dependency (RPC transport, timeout,
    permission, authentication, or any other backend failure) could not
    complete this request. Safe to retry once the backend recovers."""


class ScenarioControllerV2CorruptedAttemptError(ScenarioControllerV2Error):
    """The persisted attempt's canonical decision history, cached envelope,
    or terminal result could not be independently verified by replay. This
    module never attempts to repair or silently overwrite the persisted
    row."""


class ScenarioControllerV2TerminalAttemptError(ScenarioControllerV2Error):
    """The attempt has already reached a terminal (complete) state and can
    never accept another decision."""


class ScenarioControllerV2UnexpectedInternalError(ScenarioControllerV2Error):
    """Any other unexpected internal failure. Never carries the original
    exception's message -- only ``__cause__`` (for server-side logging)
    retains it."""


# ---------------------------------------------------------------------------
# Trusted identity context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearnerIdentityContextV2:
    """A narrow, frozen, server-side-only identity/session value object.

    Construction itself is the fail-closed boundary described in the
    module docstring: an invalid ``user_email`` or a missing
    ``supabase_client`` raises immediately, before this object can ever be
    passed to a controller entry point. ``user_email`` is replaced with its
    normalized form at construction time; the caller's original raw string
    is never retained.

    ``supabase_client`` must already be an authenticated, trusted
    server-side Supabase client (or any object satisfying
    ``utils.scenario_supabase_port_v2.SupabaseRpcClientProtocol``) obtained
    through the caller's own trusted credential source -- this module never
    reads an environment variable, never constructs a client, and never
    falls back to one. No service-role key or bearer token is ever read out
    of, or stored directly on, this dataclass beyond the opaque client
    object itself.
    """

    user_email: str
    supabase_client: Any

    def __post_init__(self) -> None:
        if self.supabase_client is None:
            raise ScenarioControllerV2InvalidIdentityError(_INVALID_IDENTITY_MESSAGE)
        raw_email = self.user_email
        if not isinstance(raw_email, str) or not raw_email.strip():
            raise ScenarioControllerV2UnauthenticatedError(_UNAUTHENTICATED_MESSAGE)
        try:
            normalized = normalize_scenario_persistence_email(raw_email)
        except ScenarioPersistenceValidationError as exc:
            raise ScenarioControllerV2InvalidIdentityError(_INVALID_IDENTITY_MESSAGE) from exc
        object.__setattr__(self, "user_email", normalized)


# ---------------------------------------------------------------------------
# Controller state + result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearnerScenarioControllerStateV2:
    """The minimum trusted server-side state required for the next call.

    Never serialize this object directly to a learner-facing surface --
    ``submission_context`` carries the full internal
    ``ScenarioOrchestrationSubmissionContextV2`` (engine state, counters,
    flags, cached envelope, ...) needed for the NEXT CAS-protected
    submission. Use :func:`serialize_learner_controller_result_v2` to
    obtain the approved learner-safe projection instead.

    ``submission_context`` is ``None`` exactly when ``is_complete`` is
    ``True`` -- a completed attempt can never be submitted against again,
    and this module enforces that locally (before ever calling the
    orchestration layer) by refusing to build a submission whenever this
    field is ``None``.
    """

    user_email: str
    attempt_id: str
    is_complete: bool
    submission_context: Optional[ScenarioOrchestrationSubmissionContextV2]
    learner_view: ScenarioOrchestrationLearnerViewV2


@dataclass(frozen=True)
class LearnerScenarioControllerResultV2:
    """Returned by every start/resume/submit entry point.

    ``last_idempotency_key`` is populated only by
    :func:`submit_learner_scenario_choice_v2` (``None`` for start/resume) --
    a caller that needs to safely retry an uncertain submission stores this
    value and passes it back explicitly as ``idempotency_key`` on retry
    (see that function's own docstring). It is never included in
    :func:`serialize_learner_controller_result_v2`'s learner-safe output.
    """

    state: LearnerScenarioControllerStateV2
    last_idempotency_key: Optional[str] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


def _require_identity(identity: Any) -> LearnerIdentityContextV2:
    """Fail closed unless ``identity`` is genuinely a
    :class:`LearnerIdentityContextV2` -- this is what makes a raw
    browser-supplied email/dict/string structurally impossible to use as
    identity anywhere in this module (see module docstring)."""
    if not isinstance(identity, LearnerIdentityContextV2):
        raise ScenarioControllerV2UnauthenticatedError(_UNAUTHENTICATED_MESSAGE)
    return identity


def _require_nonempty_str(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioControllerV2InvalidRequestError(_INVALID_REQUEST_MESSAGE)
    return value


def _validate_idempotency_key(value: Optional[str]) -> None:
    """Reject a malformed idempotency key BEFORE calling orchestration, so
    a malformed key can never reach
    ``utils.scenario_persistence_v2.build_submit_decision_rpc_params_v2``
    (which raises a raw, non-``ScenarioOrchestrationV2Error``
    ``ScenarioPersistenceV2ValidationError`` for this exact case -- see
    this module's own implementation report for why that gap is closed
    defensively here rather than by modifying the orchestration/
    persistence-v2 modules, which are out of scope for this task)."""
    if value is None:
        return
    if not isinstance(value, str):
        raise ScenarioControllerV2InvalidRequestError(_INVALID_REQUEST_MESSAGE)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ScenarioControllerV2InvalidRequestError(_INVALID_REQUEST_MESSAGE) from exc
    if parsed.version != 4:
        raise ScenarioControllerV2InvalidRequestError(_INVALID_REQUEST_MESSAGE)


def _build_port(
    identity: LearnerIdentityContextV2,
    persistence: Optional[ScenarioOrchestrationV2PersistencePort],
) -> ScenarioOrchestrationV2PersistencePort:
    """Create-or-receive the Supabase V2 port for exactly one call.

    Never caches or reuses a port instance across calls; never reads an
    environment variable; never falls back to a global client."""
    if persistence is not None:
        return persistence
    return SupabaseScenarioOrchestrationV2Port(identity.supabase_client)


def _map_orchestration_error(exc: ScenarioOrchestrationV2Error) -> ScenarioControllerV2Error:
    """Deliberately explicit, closed mapping from every
    ``ScenarioOrchestrationV2Error`` subtype (plus select RPC business
    prefixes still visible on ``ScenarioOrchestrationV2InvalidRequestError``/
    ``ScenarioOrchestrationV2StaleRunError``) to one stable, generic
    ``ScenarioControllerV2Error``. Never returns the input exception's
    message -- only a fixed string; ``exc`` itself is attached by every
    caller as ``__cause__``, never surfaced here."""
    message = str(exc)

    if isinstance(exc, ScenarioOrchestrationV2InvalidRequestError):
        if message.startswith("attempt_not_found:"):
            return ScenarioControllerV2AttemptNotFoundError(_ATTEMPT_NOT_FOUND_MESSAGE)
        if message.startswith("scenario_version_not_found:") or message.startswith(
            "scenario_version_not_published:"
        ):
            return ScenarioControllerV2ScenarioUnavailableError(_SCENARIO_UNAVAILABLE_MESSAGE)
        return ScenarioControllerV2InvalidRequestError(_INVALID_REQUEST_MESSAGE)

    if isinstance(exc, ScenarioOrchestrationV2StaleRunError):
        if message.startswith("attempt_not_in_progress:") or "already complete" in message:
            return ScenarioControllerV2TerminalAttemptError(_TERMINAL_ATTEMPT_MESSAGE)
        return ScenarioControllerV2StaleSessionError(_STALE_SESSION_MESSAGE)

    if isinstance(exc, ScenarioOrchestrationV2SequenceConflictError):
        return ScenarioControllerV2StaleSessionError(_STALE_SESSION_MESSAGE)

    if isinstance(exc, (ScenarioOrchestrationV2SceneConflictError, ScenarioOrchestrationV2IdempotencyConflictError)):
        return ScenarioControllerV2DecisionConflictError(_DECISION_CONFLICT_MESSAGE)

    if isinstance(exc, ScenarioOrchestrationV2IdentityMismatchError):
        return ScenarioControllerV2StaleSessionError(_STALE_SESSION_MESSAGE)

    if isinstance(exc, ScenarioOrchestrationV2CanonicalDecisionSequenceError):
        return ScenarioControllerV2CorruptedAttemptError(_CORRUPTED_ATTEMPT_MESSAGE)

    if isinstance(
        exc,
        (ScenarioOrchestrationV2ReplayMismatchError, ScenarioOrchestrationV2TerminalMismatchError),
    ):
        return ScenarioControllerV2CorruptedAttemptError(_CORRUPTED_ATTEMPT_MESSAGE)

    if isinstance(exc, ScenarioOrchestrationV2MalformedPersistenceResponseError):
        return ScenarioControllerV2PersistenceUnavailableError(_PERSISTENCE_UNAVAILABLE_MESSAGE)

    if isinstance(exc, ScenarioOrchestrationV2PersistenceDependencyError):
        return ScenarioControllerV2PersistenceUnavailableError(_PERSISTENCE_UNAVAILABLE_MESSAGE)

    return ScenarioControllerV2UnexpectedInternalError(_UNEXPECTED_MESSAGE)


def _run_controller_step(func: Callable[[], _T]) -> _T:
    """The one error boundary every public entry point routes through.

    Only ``Exception`` is ever caught here -- ``BaseException`` subclasses
    that are not ``Exception`` (``KeyboardInterrupt``, ``SystemExit``, and
    any other control-flow signal) are never intercepted, matching the
    module docstring's error contract."""
    try:
        return func()
    except ScenarioControllerV2Error:
        raise
    except ScenarioOrchestrationV2Error as exc:
        raise _map_orchestration_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed, sanitized controller error below.
        raise ScenarioControllerV2UnexpectedInternalError(_UNEXPECTED_MESSAGE) from exc


def _plain_json_value(value: Any) -> Any:
    """Recursively rebuild ``value`` using only plain ``dict``/``list``/
    scalar types, independent of any ``MappingProxyType``/``tuple`` the
    engine's frozen runtime dataclasses use internally. The result shares
    no mutable structure with its input -- mutating the returned value can
    never affect the source, and vice versa."""
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    return value


def _serialize_scene_view(scene: LearnerSceneView) -> Dict[str, Any]:
    return {
        "sceneId": scene.scene_id,
        "title": scene.title,
        "setting": scene.setting,
        "dialogueExchanges": _plain_json_value(scene.dialogue_exchanges),
        "charactersPresent": list(scene.characters_present),
        "learnerPresent": scene.learner_present,
        "decisionPrompt": scene.decision_prompt,
        "options": [
            {"id": option.id, "title": option.title, "text": option.text} for option in scene.options
        ],
        "progressMetadata": _plain_json_value(scene.progress_metadata)
        if scene.progress_metadata is not None
        else None,
        "accessibility": _plain_json_value(scene.accessibility) if scene.accessibility is not None else None,
        "mobilePresentation": _plain_json_value(scene.mobile_presentation)
        if scene.mobile_presentation is not None
        else None,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_or_resume_learner_scenario_v2(
    content: ScenarioContentV2,
    *,
    identity: Any,
    scenario_version_id: str,
    attempt_id: Optional[str] = None,
    persistence: Optional[ScenarioOrchestrationV2PersistencePort] = None,
) -> LearnerScenarioControllerResultV2:
    """Start a new Engine V2 attempt, or resume the caller's existing one.

    ``identity`` must already be a validated :class:`LearnerIdentityContextV2`
    (see module docstring); a browser-supplied email/dict/string is
    structurally rejected here with :class:`ScenarioControllerV2UnauthenticatedError`.

    Delegates entirely to
    ``utils.scenario_orchestration_v2.start_or_resume_scenario_run_v2`` --
    this function performs no replay, RPC-parameter construction, or CAS
    logic of its own; it only translates that call's typed result into a
    :class:`LearnerScenarioControllerResultV2` (and its typed failures into
    a stable, sanitized :class:`ScenarioControllerV2Error`).
    """
    verified_identity = _require_identity(identity)
    version_id = _require_nonempty_str(scenario_version_id)
    trusted_attempt_id = _require_nonempty_str(attempt_id) if attempt_id is not None else None
    port = _build_port(verified_identity, persistence)

    def _do() -> LearnerScenarioControllerResultV2:
        result = start_or_resume_scenario_run_v2(
            content,
            persistence=port,
            user_email=verified_identity.user_email,
            scenario_version_id=version_id,
            attempt_id=trusted_attempt_id,
        )
        submission_context = None if result.run.is_complete else result.submission_context
        state = LearnerScenarioControllerStateV2(
            user_email=verified_identity.user_email,
            attempt_id=result.attempt_id,
            is_complete=result.run.is_complete,
            submission_context=submission_context,
            learner_view=result.learner_view,
        )
        return LearnerScenarioControllerResultV2(state=state)

    return _run_controller_step(_do)


def resume_learner_scenario_v2(
    content: ScenarioContentV2,
    *,
    identity: Any,
    attempt_id: str,
    persistence: Optional[ScenarioOrchestrationV2PersistencePort] = None,
) -> LearnerScenarioControllerResultV2:
    """Resume an existing Engine V2 attempt from trusted persisted state.

    Requires a trusted ``attempt_id`` (never derived from an untrusted
    caller belief about "the current attempt"). Delegates entirely to
    ``utils.scenario_orchestration_v2.resume_and_replay_scenario_run_v2``,
    which itself: loads the trusted persisted attempt row, loads and
    strictly validates canonical decisions, and replays authoritatively --
    the persisted envelope is verify-only cache, never trusted for
    reconstruction, and is never repaired or overwritten automatically on
    a mismatch (a mismatch instead fails closed as
    :class:`ScenarioControllerV2CorruptedAttemptError` /
    :class:`ScenarioControllerV2StaleSessionError`, see
    :func:`_map_orchestration_error`).
    """
    verified_identity = _require_identity(identity)
    trusted_attempt_id = _require_nonempty_str(attempt_id)
    port = _build_port(verified_identity, persistence)

    def _do() -> LearnerScenarioControllerResultV2:
        run, snapshot = resume_and_replay_scenario_run_v2(
            content,
            persistence=port,
            user_email=verified_identity.user_email,
            attempt_id=trusted_attempt_id,
        )
        if run.is_complete:
            submission_context = None
        else:
            submission_context = _build_submission_context(
                user_email=verified_identity.user_email,
                content=content,
                snapshot=snapshot,
                run=run,
            )
        learner_view = _build_learner_view(run)
        state = LearnerScenarioControllerStateV2(
            user_email=verified_identity.user_email,
            attempt_id=snapshot.attempt_id,
            is_complete=run.is_complete,
            submission_context=submission_context,
            learner_view=learner_view,
        )
        return LearnerScenarioControllerResultV2(state=state)

    return _run_controller_step(_do)


def submit_learner_scenario_choice_v2(
    content: ScenarioContentV2,
    *,
    identity: Any,
    state: LearnerScenarioControllerStateV2,
    selected_option_id: str,
    idempotency_key: Optional[str] = None,
    persistence: Optional[ScenarioOrchestrationV2PersistencePort] = None,
) -> LearnerScenarioControllerResultV2:
    """Submit exactly one learner-visible decision against ``state``.

    ``state`` must be the exact :class:`LearnerScenarioControllerStateV2`
    previously returned by :func:`start_or_resume_learner_scenario_v2` or
    :func:`resume_learner_scenario_v2` -- never reconstructed by the
    caller. ``identity.user_email`` must match ``state.user_email`` exactly
    (fails closed with :class:`ScenarioControllerV2InvalidIdentityError`
    otherwise) -- a prepared/retained controller state can never be
    submitted under a different learner identity.

    ``idempotency_key`` is optional: omit it for a first submission (this
    module generates a fresh UUIDv4 via the orchestration layer), or supply
    the exact value from a prior call's
    ``LearnerScenarioControllerResultV2.last_idempotency_key`` to safely
    retry an uncertain result -- this function never mints a new key
    automatically during an explicit retry, and never automatically retries
    a stale/CAS failure itself (the caller must call
    :func:`resume_learner_scenario_v2` again and resubmit).
    """
    verified_identity = _require_identity(identity)
    if not isinstance(state, LearnerScenarioControllerStateV2):
        raise ScenarioControllerV2InvalidRequestError(_INVALID_REQUEST_MESSAGE)
    if verified_identity.user_email != state.user_email:
        raise ScenarioControllerV2InvalidIdentityError(_INVALID_IDENTITY_MESSAGE)
    if state.is_complete or state.submission_context is None:
        raise ScenarioControllerV2TerminalAttemptError(_TERMINAL_ATTEMPT_MESSAGE)
    option_id = _require_nonempty_str(selected_option_id)
    _validate_idempotency_key(idempotency_key)
    port = _build_port(verified_identity, persistence)
    submission_context = state.submission_context

    def _do() -> LearnerScenarioControllerResultV2:
        result = submit_scenario_decision_v2(
            content,
            persistence=port,
            submission_context=submission_context,
            selected_option_id=option_id,
            idempotency_key=idempotency_key,
        )
        next_submission_context = None if result.run.is_complete else result.submission_context
        new_state = LearnerScenarioControllerStateV2(
            user_email=verified_identity.user_email,
            attempt_id=result.attempt_id,
            is_complete=result.run.is_complete,
            submission_context=next_submission_context,
            learner_view=result.learner_view,
        )
        return LearnerScenarioControllerResultV2(state=new_state, last_idempotency_key=result.idempotency_key)

    return _run_controller_step(_do)


def serialize_learner_controller_result_v2(result: LearnerScenarioControllerResultV2) -> Dict[str, Any]:
    """The ONLY function in this module permitted to return a raw ``dict``.

    Produces exactly the approved learner-safe shape (see module docstring
    for the ``attemptId``-exposure decision):

    - active scene: ``{"isComplete": False, "currentScene": {...},
      "expectedSequenceNumber": <int>}``;
    - terminal: ``{"isComplete": True, "terminalResult": {...}}`` (no
      ``currentScene``, no ``expectedSequenceNumber``).

    Every nested value is freshly rebuilt via :func:`_plain_json_value` --
    the returned mapping shares no mutable structure with
    ``result.state.learner_view`` (or anything else inside ``result``), so
    mutating the returned dict can never affect ``result`` or any future
    call to this function with the same ``result``.
    """
    if not isinstance(result, LearnerScenarioControllerResultV2):
        raise ScenarioControllerV2InvalidRequestError(_INVALID_REQUEST_MESSAGE)
    state = result.state
    if state.is_complete:
        terminal = state.learner_view.terminal_view
        if terminal is None:
            raise ScenarioControllerV2CorruptedAttemptError(_CORRUPTED_ATTEMPT_MESSAGE)
        return {
            "isComplete": True,
            "terminalResult": {
                "outcomeId": terminal.outcome_id,
                "outcomeTitle": terminal.outcome_title,
                "narrative": terminal.narrative,
                "displayScore": terminal.display_score,
            },
        }
    scene = state.learner_view.scene_view
    if scene is None:
        raise ScenarioControllerV2CorruptedAttemptError(_CORRUPTED_ATTEMPT_MESSAGE)
    return {
        "isComplete": False,
        "currentScene": _serialize_scene_view(scene),
        "expectedSequenceNumber": scene.expected_sequence_number,
    }
