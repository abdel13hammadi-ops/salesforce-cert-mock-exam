"""Engine V2 start/resume and decision-submission orchestration.

Pure orchestration over:

- ``utils.scenario_engine_v2`` (authoritative compute)
- ``utils.scenario_persistence_v2`` (serialization, replay verification, RPC
  param/response parsing)

This module never instantiates a Supabase client, never reads environment
variables, and never calls RPCs directly -- every database interaction is
injected through :class:`ScenarioOrchestrationV2PersistencePort`.

Authoritative data: immutable scenario content, trusted database attempt
identity columns, and ordered ``scenario_decisions`` rows. Persisted envelope
JSON is verify-only cache, never trusted for reconstruction.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from utils.scenario_engine_v2 import (
    ENGINE_VERSION,
    LearnerSceneView,
    LearnerTerminalView,
    ScenarioContentV2,
    ScenarioDecisionInputV2,
    ScenarioRunV2Snapshot,
    apply_decision_v2,
    build_learner_scene_view,
    build_learner_terminal_view,
    start_scenario_run_v2,
)
from utils.scenario_persistence import (
    ScenarioPersistenceValidationError,
    normalize_scenario_persistence_email,
)
from utils.scenario_persistence_v2 import (
    ScenarioPersistenceV2CacheMismatchError,
    ScenarioPersistenceV2Error,
    ScenarioPersistenceV2IdentityError,
    ScenarioPersistenceV2RpcResponseError,
    ScenarioPersistenceV2TerminalMismatchError,
    build_start_or_resume_rpc_params_v2,
    build_submit_decision_rpc_params_v2,
    parse_start_or_resume_rpc_response_v2,
    parse_submit_decision_rpc_response_v2,
    replay_serialized_run_v2,
)

__all__ = (
    # Errors
    "ScenarioOrchestrationV2Error",
    "ScenarioOrchestrationV2InvalidRequestError",
    "ScenarioOrchestrationV2MalformedPersistenceResponseError",
    "ScenarioOrchestrationV2IdentityMismatchError",
    "ScenarioOrchestrationV2CanonicalDecisionSequenceError",
    "ScenarioOrchestrationV2StaleRunError",
    "ScenarioOrchestrationV2SequenceConflictError",
    "ScenarioOrchestrationV2SceneConflictError",
    "ScenarioOrchestrationV2IdempotencyConflictError",
    "ScenarioOrchestrationV2ReplayMismatchError",
    "ScenarioOrchestrationV2TerminalMismatchError",
    "ScenarioOrchestrationV2PersistenceDependencyError",
    # Port + typed values
    "ScenarioOrchestrationV2PersistencePort",
    "LearnerAttemptSummaryV2",
    "AuthoritativeAttemptRefV2",
    "TrustedAttemptSnapshotV2",
    "ScenarioOrchestrationSubmissionContextV2",
    "ScenarioOrchestrationLearnerViewV2",
    "StartOrResumeScenarioRunResultV2",
    "SubmitScenarioDecisionResultV2",
    # Public API
    "load_canonical_scenario_decisions_v2",
    "resolve_authoritative_attempt_ref_v2",
    "resume_and_replay_scenario_run_v2",
    "start_or_resume_scenario_run_v2",
    "submit_scenario_decision_v2",
)

# RPC error prefix map (mirrors utils.scenario_persistence._ERROR_PREFIX_MAP for
# the two RPCs this orchestration layer calls).


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScenarioOrchestrationV2Error(Exception):
    """Base error for Engine V2 orchestration."""


class ScenarioOrchestrationV2InvalidRequestError(ScenarioOrchestrationV2Error):
    """Malformed caller input or RPC-rejected invalid parameter."""


class ScenarioOrchestrationV2MalformedPersistenceResponseError(ScenarioOrchestrationV2Error):
    """Malformed RPC response or trusted-row shape."""


class ScenarioOrchestrationV2IdentityMismatchError(ScenarioOrchestrationV2Error):
    """Trusted attempt identity no longer matches immutable content or request."""


class ScenarioOrchestrationV2CanonicalDecisionSequenceError(ScenarioOrchestrationV2Error):
    """Canonical decision rows are missing, duplicated, gapped, or malformed."""


class ScenarioOrchestrationV2StaleRunError(ScenarioOrchestrationV2Error):
    """Attempt is no longer in the expected in-progress CAS state."""


class ScenarioOrchestrationV2SequenceConflictError(ScenarioOrchestrationV2Error):
    """Expected sequence number disagrees with the persisted attempt."""


class ScenarioOrchestrationV2SceneConflictError(ScenarioOrchestrationV2Error):
    """Expected scene disagrees with the persisted attempt."""


class ScenarioOrchestrationV2IdempotencyConflictError(ScenarioOrchestrationV2Error):
    """Same idempotency key reused with a different request fingerprint."""


class ScenarioOrchestrationV2ReplayMismatchError(ScenarioOrchestrationV2Error):
    """Post-RPC reload/replay disagrees with the locally computed run."""


class ScenarioOrchestrationV2TerminalMismatchError(ScenarioOrchestrationV2Error):
    """Terminal outcome disagreement after reload/replay."""


class ScenarioOrchestrationV2PersistenceDependencyError(ScenarioOrchestrationV2Error):
    """Underlying persistence dependency failed unexpectedly."""


# RPC error prefix map (mirrors utils.scenario_persistence._ERROR_PREFIX_MAP for
# the two RPCs this orchestration layer calls). Defined after the error classes
# above so each entry can reference its target exception type directly.
_RPC_ERROR_PREFIX_MAP: Tuple[Tuple[str, type], ...] = (
    ("invalid_user_email:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_attempt_id:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_scenario_version_id:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_idempotency_key:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_sequence_number:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_expected_scene_id:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_selected_option_id:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_request_fingerprint:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_state_before:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_state_after:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_is_terminal:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_resulting_scene_id:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_terminal_ending_id:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_terminal_result_snapshot:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_terminal_fields:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_initial_scene:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_initial_state:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_initial_state_identity:", ScenarioOrchestrationV2InvalidRequestError),
    ("invalid_initial_state_lifecycle:", ScenarioOrchestrationV2InvalidRequestError),
    ("scenario_version_not_found:", ScenarioOrchestrationV2InvalidRequestError),
    ("scenario_version_not_published:", ScenarioOrchestrationV2InvalidRequestError),
    ("engine_version_mismatch:", ScenarioOrchestrationV2IdentityMismatchError),
    ("content_hash_mismatch:", ScenarioOrchestrationV2IdentityMismatchError),
    ("attempt_not_found:", ScenarioOrchestrationV2InvalidRequestError),
    ("attempt_not_in_progress:", ScenarioOrchestrationV2StaleRunError),
    ("idempotency_key_conflict:", ScenarioOrchestrationV2IdempotencyConflictError),
    ("sequence_mismatch:", ScenarioOrchestrationV2SequenceConflictError),
    ("scene_mismatch:", ScenarioOrchestrationV2SceneConflictError),
    ("state_before_mismatch:", ScenarioOrchestrationV2StaleRunError),
    ("state_identity_mismatch:", ScenarioOrchestrationV2StaleRunError),
    ("state_lifecycle_mismatch:", ScenarioOrchestrationV2StaleRunError),
    ("terminal_result_mismatch:", ScenarioOrchestrationV2TerminalMismatchError),
    ("terminal_ending_mismatch:", ScenarioOrchestrationV2TerminalMismatchError),
    ("attempt_id_conflict:", ScenarioOrchestrationV2IdentityMismatchError),
    ("attempt_id_collision:", ScenarioOrchestrationV2InvalidRequestError),
    ("start_or_resume_failed:", ScenarioOrchestrationV2PersistenceDependencyError),
)


# ---------------------------------------------------------------------------
# Dependency injection port
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearnerAttemptSummaryV2:
    """Minimum fields for authoritative attempt selection (never learner-facing)."""

    attempt_id: str
    status: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass(frozen=True)
class AuthoritativeAttemptRefV2:
    """Selected attempt identity for Option B resume without session attempt_id."""

    attempt_id: str
    status: str


class ScenarioOrchestrationV2PersistencePort(Protocol):
    """Minimal persistence/data-access surface for Engine V2 orchestration."""

    def call_start_or_resume_scenario_attempt_v1(self, params: Mapping[str, Any]) -> Any:
        """Invoke ``start_or_resume_scenario_attempt_v1`` with named params."""

    def call_submit_scenario_decision_v1(self, params: Mapping[str, Any]) -> Any:
        """Invoke ``submit_scenario_decision_v1`` with named params."""

    def load_attempt_snapshot(self, *, user_email: str, attempt_id: str) -> Mapping[str, Any]:
        """Load one trusted attempt row plus ordered decision history.

        Must return the ``get_scenario_attempt_v1`` row shape (attempt columns
        plus a ``decisions`` JSON array). Never returns a raw Supabase client
        response wrapper to callers of this port's consumers.
        """

    def list_learner_attempt_summaries_v2(
        self,
        *,
        user_email: str,
        scenario_version_id: str,
    ) -> Tuple[LearnerAttemptSummaryV2, ...]:
        """Return ownership-scoped attempt summaries for one scenario version.

        Returns only ``attempt_id``, ``status``, ``started_at``, and
        ``completed_at``. Callers must supply a trusted server-side email.
        """


# ---------------------------------------------------------------------------
# Typed values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedAttemptSnapshotV2:
    attempt_id: str
    scenario_id: str
    scenario_version_id: str
    status: str
    current_scene_id: Optional[str]
    next_sequence_number: int
    serialized_engine_state: Dict[str, Any]
    engine_version: str
    scenario_content_sha256: str
    decisions: Tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class ScenarioOrchestrationSubmissionContextV2:
    """Trusted server-side state required for the next CAS submission.

    Deliberately separate from learner-safe views -- never serialized to the
    client as-is.
    """

    user_email: str
    attempt_id: str
    scenario_version_id: str
    expected_sequence_number: int
    expected_scene_id: str
    cached_envelope: Dict[str, Any]
    visible_option_ids: Tuple[str, ...]
    run: ScenarioRunV2Snapshot


@dataclass(frozen=True)
class ScenarioOrchestrationLearnerViewV2:
    scene_view: Optional[LearnerSceneView]
    terminal_view: Optional[LearnerTerminalView]


@dataclass(frozen=True)
class StartOrResumeScenarioRunResultV2:
    attempt_id: str
    created: bool
    run: ScenarioRunV2Snapshot
    submission_context: ScenarioOrchestrationSubmissionContextV2
    learner_view: ScenarioOrchestrationLearnerViewV2


@dataclass(frozen=True)
class SubmitScenarioDecisionResultV2:
    attempt_id: str
    sequence_number: int
    decision_id: str
    idempotent_replay: bool
    idempotency_key: str
    run: ScenarioRunV2Snapshot
    submission_context: ScenarioOrchestrationSubmissionContextV2
    learner_view: ScenarioOrchestrationLearnerViewV2


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_error_message(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return str(exc).strip()


def _map_persistence_exception(rpc_name: str, exc: BaseException) -> ScenarioOrchestrationV2Error:
    if isinstance(exc, ScenarioOrchestrationV2Error):
        return exc
    if isinstance(exc, ScenarioPersistenceV2Error):
        mapped: ScenarioOrchestrationV2Error = ScenarioOrchestrationV2MalformedPersistenceResponseError(str(exc))
        mapped.__cause__ = exc
        return mapped
    message = _extract_error_message(exc)
    for prefix, exc_cls in _RPC_ERROR_PREFIX_MAP:
        if message.startswith(prefix):
            mapped = exc_cls(message)
            mapped.__cause__ = exc
            return mapped
    mapped = ScenarioOrchestrationV2PersistenceDependencyError(f"RPC {rpc_name!r} failed: {message}")
    mapped.__cause__ = exc
    return mapped


def _wrap_persistence_call(rpc_name: str, func: Any) -> Any:
    try:
        return func()
    except ScenarioOrchestrationV2Error:
        raise
    except Exception as exc:
        raise _map_persistence_exception(rpc_name, exc) from exc


def _normalize_email_or_raise(user_email: str) -> str:
    """Normalize a caller-supplied email through the reused V1 validator,
    translating its ``ScenarioPersistenceValidationError`` (a V1-specific,
    cross-module exception type) into this module's own
    ``ScenarioOrchestrationV2InvalidRequestError``, with the original
    exception preserved as the cause. No V2 orchestration entry point may let
    a raw V1 exception type escape."""
    try:
        return normalize_scenario_persistence_email(user_email)
    except ScenarioPersistenceValidationError as exc:
        raise ScenarioOrchestrationV2InvalidRequestError(str(exc)) from exc


def _require_uuid(value: Any, field: str, *, allow_nil: bool = False) -> str:
    text = value if isinstance(value, str) else str(value)
    try:
        parsed = uuid.UUID(text.strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise ScenarioOrchestrationV2InvalidRequestError(
            f"invalid_{field}: must be a valid UUID, got {value!r}"
        ) from exc
    canonical = str(parsed)
    if not allow_nil and canonical == "00000000-0000-0000-0000-000000000000":
        raise ScenarioOrchestrationV2InvalidRequestError(f"invalid_{field}: nil UUID is not permitted")
    return canonical


def _require_strict_int(value: Any, field: str, *, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            f"invalid_{field}: must be an actual int, not bool or {type(value).__name__} ({value!r})"
        )
    if minimum is not None and value < minimum:
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            f"invalid_{field}: must be >= {minimum}, got {value}"
        )
    return value


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            f"invalid_{field}: must be a non-empty string, got {value!r}"
        )
    return value


def _deep_copy_json(value: Mapping[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(dict(value))


def _validate_engine_content(content: ScenarioContentV2) -> None:
    if content.schema_version != "1.1.0":
        raise ScenarioOrchestrationV2InvalidRequestError(
            f"invalid_content: schemaVersion must be '1.1.0', got {content.schema_version!r}"
        )
    if content.required_engine_version != ENGINE_VERSION:
        raise ScenarioOrchestrationV2InvalidRequestError(
            f"invalid_content: requiredEngineVersion must be {ENGINE_VERSION!r}, got {content.required_engine_version!r}"
        )
    if not content.canonical_content_sha256 or len(content.canonical_content_sha256) != 64:
        raise ScenarioOrchestrationV2InvalidRequestError(
            "invalid_content: canonicalContentSha256 must be exactly 64 lowercase hex characters"
        )


def _parse_attempt_snapshot_row(row: Mapping[str, Any], *, expected_attempt_id: str) -> TrustedAttemptSnapshotV2:
    if not isinstance(row, Mapping):
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            f"invalid_attempt_snapshot: must be a mapping, got {type(row).__name__}"
        )
    attempt_id = _require_uuid(row.get("attempt_id"), "attempt_id")
    if attempt_id != _require_uuid(expected_attempt_id, "expected_attempt_id"):
        raise ScenarioOrchestrationV2IdentityMismatchError(
            "identity_mismatch: loaded attempt_id does not match the requested attempt_id"
        )
    scenario_id = _require_uuid(row.get("scenario_id"), "scenario_id")
    scenario_version_id = _require_uuid(row.get("scenario_version_id"), "scenario_version_id")
    status = _require_nonempty_str(row.get("status"), "status")
    current_scene_id = row.get("current_scene_id")
    if current_scene_id is not None and (not isinstance(current_scene_id, str) or not current_scene_id.strip()):
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            "invalid_current_scene_id: must be null or a non-empty string"
        )
    next_sequence_number = _require_strict_int(row.get("next_sequence_number"), "next_sequence_number", minimum=1)
    engine_version = _require_nonempty_str(row.get("engine_version"), "engine_version")
    content_hash = _require_nonempty_str(row.get("scenario_content_sha256"), "scenario_content_sha256").lower()
    if len(content_hash) != 64:
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            "invalid_scenario_content_sha256: must be exactly 64 lowercase hex characters"
        )
    serialized = row.get("serialized_engine_state")
    if not isinstance(serialized, Mapping):
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            "invalid_serialized_engine_state: must be a JSON object"
        )
    decisions_raw = row.get("decisions")
    if not isinstance(decisions_raw, list):
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            "invalid_decisions: must be a JSON array"
        )
    decisions: list[Dict[str, Any]] = []
    for index, item in enumerate(decisions_raw):
        # Only a Mapping (JSON object) is an acceptable decision row shape.
        # Rejecting anything else here -- rather than passing it to dict(...)
        # -- prevents a raw TypeError (e.g. from an int, string, list, bool,
        # or None element) from escaping this parser. Malformed elements are
        # never silently skipped, and decisions_raw/its elements are never
        # mutated: each accepted element is deep-copied only after the type
        # check passes.
        if not isinstance(item, Mapping):
            raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
                f"invalid_decisions[{index}]: must be a JSON object, got {type(item).__name__} ({item!r})"
            )
        decisions.append(_deep_copy_json(item))
    return TrustedAttemptSnapshotV2(
        attempt_id=attempt_id,
        scenario_id=scenario_id,
        scenario_version_id=scenario_version_id,
        status=status,
        current_scene_id=current_scene_id,
        next_sequence_number=next_sequence_number,
        serialized_engine_state=_deep_copy_json(serialized),
        engine_version=engine_version,
        scenario_content_sha256=content_hash,
        decisions=tuple(decisions),
    )


def _build_submission_context(
    *,
    user_email: str,
    content: ScenarioContentV2,
    snapshot: TrustedAttemptSnapshotV2,
    run: ScenarioRunV2Snapshot,
) -> ScenarioOrchestrationSubmissionContextV2:
    if run.is_complete or run.current_scene_id is None:
        raise ScenarioOrchestrationV2InvalidRequestError(
            "invalid_run: cannot build submission context for a completed run"
        )
    scene_view = build_learner_scene_view(run)
    return ScenarioOrchestrationSubmissionContextV2(
        user_email=user_email,
        attempt_id=snapshot.attempt_id,
        scenario_version_id=snapshot.scenario_version_id,
        expected_sequence_number=run.expected_sequence_number,
        expected_scene_id=run.current_scene_id,
        cached_envelope=_deep_copy_json(snapshot.serialized_engine_state),
        visible_option_ids=tuple(option.id for option in scene_view.options),
        run=run,
    )


def _build_learner_view(run: ScenarioRunV2Snapshot) -> ScenarioOrchestrationLearnerViewV2:
    if run.is_complete:
        return ScenarioOrchestrationLearnerViewV2(scene_view=None, terminal_view=build_learner_terminal_view(run))
    return ScenarioOrchestrationLearnerViewV2(scene_view=build_learner_scene_view(run), terminal_view=None)


def _assert_runs_equivalent(expected: ScenarioRunV2Snapshot, actual: ScenarioRunV2Snapshot) -> None:
    if (
        expected.attempt_id != actual.attempt_id
        or expected.current_scene_id != actual.current_scene_id
        or expected.expected_sequence_number != actual.expected_sequence_number
        or expected.is_complete != actual.is_complete
        or dict(expected.state) != dict(actual.state)
        or dict(expected.counters) != dict(actual.counters)
        or expected.flags != actual.flags
        or len(expected.decisions) != len(actual.decisions)
    ):
        raise ScenarioOrchestrationV2ReplayMismatchError(
            "replay_mismatch: reloaded canonical replay disagrees with the locally computed run"
        )
    for left, right in zip(expected.decisions, actual.decisions):
        if left.sequence_number != right.sequence_number or left.scene_id != right.scene_id or left.option_id != right.option_id:
            raise ScenarioOrchestrationV2ReplayMismatchError(
                "replay_mismatch: reloaded canonical replay disagrees with the locally computed decision history"
            )
    if expected.is_complete:
        if expected.terminal_result is None or actual.terminal_result is None:
            raise ScenarioOrchestrationV2TerminalMismatchError(
                "terminal_mismatch: completed run missing terminal_result after reload/replay"
            )
        if (
            expected.terminal_result.outcome_id != actual.terminal_result.outcome_id
            or expected.terminal_result.display_score != actual.terminal_result.display_score
        ):
            raise ScenarioOrchestrationV2TerminalMismatchError(
                "terminal_mismatch: reloaded terminal outcome disagrees with the locally computed run"
            )


# ---------------------------------------------------------------------------
# Public API — canonical decision loading
# ---------------------------------------------------------------------------


def load_canonical_scenario_decisions_v2(
    decision_rows: Sequence[Mapping[str, Any]],
    *,
    attempt_id: str,
) -> Tuple[ScenarioDecisionInputV2, ...]:
    """Validate and normalize trusted ``get_scenario_attempt_v1`` decision rows.

    Maps database replay triple ``(sequenceNumber, expectedSceneId,
    selectedOptionId)`` to Engine V2's ``(sequenceNumber, sceneId, optionId)``
    shape. Never mutates ``decision_rows``.
    """
    trusted_attempt_id = _require_uuid(attempt_id, "attempt_id")
    if not isinstance(decision_rows, Sequence):
        raise ScenarioOrchestrationV2CanonicalDecisionSequenceError(
            f"invalid_decisions: must be a sequence, got {type(decision_rows).__name__}"
        )

    normalized: list[ScenarioDecisionInputV2] = []
    seen_sequences: set[int] = set()
    expected_next = 1

    for index, row in enumerate(decision_rows):
        if not isinstance(row, Mapping):
            raise ScenarioOrchestrationV2CanonicalDecisionSequenceError(
                f"invalid_decisions[{index}]: must be a JSON object"
            )
        row_attempt = row.get("attemptId") or row.get("attempt_id")
        if row_attempt is not None and _require_uuid(row_attempt, f"decisions[{index}].attemptId") != trusted_attempt_id:
            raise ScenarioOrchestrationV2CanonicalDecisionSequenceError(
                f"invalid_decisions[{index}]: attemptId does not match the trusted attempt_id"
            )
        sequence_number = row.get("sequenceNumber", row.get("sequence_number"))
        if isinstance(sequence_number, bool) or not isinstance(sequence_number, int):
            raise ScenarioOrchestrationV2CanonicalDecisionSequenceError(
                f"invalid_decisions[{index}].sequenceNumber: must be an actual int"
            )
        scene_id = row.get("sceneId") or row.get("expectedSceneId") or row.get("expected_scene_id")
        option_id = row.get("optionId") or row.get("selectedOptionId") or row.get("selected_option_id")
        if not isinstance(scene_id, str) or not scene_id.strip():
            raise ScenarioOrchestrationV2CanonicalDecisionSequenceError(
                f"invalid_decisions[{index}].sceneId: must be a non-empty string"
            )
        if not isinstance(option_id, str) or not option_id.strip():
            raise ScenarioOrchestrationV2CanonicalDecisionSequenceError(
                f"invalid_decisions[{index}].optionId: must be a non-empty string"
            )
        if sequence_number in seen_sequences:
            raise ScenarioOrchestrationV2CanonicalDecisionSequenceError(
                f"invalid_decisions: duplicate sequenceNumber {sequence_number}"
            )
        if sequence_number != expected_next:
            raise ScenarioOrchestrationV2CanonicalDecisionSequenceError(
                f"invalid_decisions: expected sequenceNumber {expected_next}, got {sequence_number} (gap or out-of-order row)"
            )
        seen_sequences.add(sequence_number)
        expected_next += 1
        normalized.append(
            ScenarioDecisionInputV2(
                sequence_number=sequence_number,
                scene_id=scene_id,
                option_id=option_id,
            )
        )

    return tuple(normalized)


# ---------------------------------------------------------------------------
# Public API — authoritative attempt selection (no session attempt_id)
# ---------------------------------------------------------------------------


def resolve_authoritative_attempt_ref_v2(
    persistence: ScenarioOrchestrationV2PersistencePort,
    *,
    user_email: str,
    scenario_version_id: str,
) -> Optional[AuthoritativeAttemptRefV2]:
    """Select the learner's authoritative attempt for CB-SC-001 session loss.

    Rules (deterministic):

    1. Exactly one ``in_progress`` row for ``(user_email, scenario_version_id)``
       → resume that attempt.
    2. More than one ``in_progress`` row (unique index violated) → fail closed.
    3. Else the most recent ``completed`` row by
       ``(completed_at DESC, started_at DESC, attempt_id DESC)`` → resume terminal.
    4. Else ``None`` (first-ever attempt may be created by the caller).

    Abandoned attempts are never selected.
    """
    normalized_email = _normalize_email_or_raise(user_email)
    version_id = _require_uuid(scenario_version_id, "scenario_version_id")
    summaries = _wrap_persistence_call(
        "list_learner_attempt_summaries_v2",
        lambda: persistence.list_learner_attempt_summaries_v2(
            user_email=normalized_email,
            scenario_version_id=version_id,
        ),
    )
    if not isinstance(summaries, (tuple, list)):
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            "malformed_response: list_learner_attempt_summaries_v2 must return a sequence"
        )

    in_progress = [row for row in summaries if getattr(row, "status", None) == "in_progress"]
    if len(in_progress) > 1:
        raise ScenarioOrchestrationV2CanonicalDecisionSequenceError(
            "multiple_in_progress: more than one in_progress attempt exists for this learner/version"
        )
    if len(in_progress) == 1:
        chosen = in_progress[0]
        return AuthoritativeAttemptRefV2(attempt_id=str(chosen.attempt_id), status="in_progress")

    completed = [row for row in summaries if getattr(row, "status", None) == "completed"]
    if not completed:
        return None

    def _completed_sort_key(row: LearnerAttemptSummaryV2) -> Tuple[str, str, str]:
        return (
            str(row.completed_at or ""),
            str(row.started_at or ""),
            str(row.attempt_id),
        )

    completed_sorted = sorted(completed, key=_completed_sort_key, reverse=True)
    chosen = completed_sorted[0]
    return AuthoritativeAttemptRefV2(attempt_id=str(chosen.attempt_id), status="completed")


# ---------------------------------------------------------------------------
# Public API — resume / replay
# ---------------------------------------------------------------------------


def resume_and_replay_scenario_run_v2(
    content: ScenarioContentV2,
    *,
    persistence: ScenarioOrchestrationV2PersistencePort,
    user_email: str,
    attempt_id: str,
) -> Tuple[ScenarioRunV2Snapshot, TrustedAttemptSnapshotV2]:
    """Load trusted attempt identity + canonical decisions and replay authoritatively."""
    _validate_engine_content(content)
    normalized_email = _normalize_email_or_raise(user_email)
    trusted_attempt_id = _require_uuid(attempt_id, "attempt_id", allow_nil=False)

    row = _wrap_persistence_call(
        "get_scenario_attempt_v1",
        lambda: persistence.load_attempt_snapshot(user_email=normalized_email, attempt_id=trusted_attempt_id),
    )
    snapshot = _parse_attempt_snapshot_row(row, expected_attempt_id=trusted_attempt_id)

    if snapshot.engine_version != ENGINE_VERSION:
        raise ScenarioOrchestrationV2IdentityMismatchError(
            f"identity_mismatch: attempt engine_version {snapshot.engine_version!r} != {ENGINE_VERSION!r}"
        )
    if snapshot.scenario_content_sha256 != content.canonical_content_sha256:
        raise ScenarioOrchestrationV2IdentityMismatchError(
            "identity_mismatch: attempt scenario_content_sha256 does not match immutable content"
        )

    canonical_rows = [
        {
            "sequenceNumber": decision.sequence_number,
            "sceneId": decision.scene_id,
            "optionId": decision.option_id,
        }
        for decision in load_canonical_scenario_decisions_v2(snapshot.decisions, attempt_id=trusted_attempt_id)
    ]

    try:
        replayed = replay_serialized_run_v2(
            content,
            attempt_row_id=trusted_attempt_id,
            attempt_row_engine_version=snapshot.engine_version,
            attempt_row_scenario_content_sha256=snapshot.scenario_content_sha256,
            canonical_decision_rows=canonical_rows,
            cached_envelope_payload=snapshot.serialized_engine_state,
        )
    except ScenarioPersistenceV2IdentityError as exc:
        raise ScenarioOrchestrationV2IdentityMismatchError(str(exc)) from exc
    except ScenarioPersistenceV2CacheMismatchError as exc:
        raise ScenarioOrchestrationV2ReplayMismatchError(str(exc)) from exc
    except ScenarioPersistenceV2TerminalMismatchError as exc:
        raise ScenarioOrchestrationV2TerminalMismatchError(str(exc)) from exc
    except ScenarioPersistenceV2Error as exc:
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(str(exc)) from exc

    return replayed, snapshot


# ---------------------------------------------------------------------------
# Public API — start / resume
# ---------------------------------------------------------------------------


def start_or_resume_scenario_run_v2(
    content: ScenarioContentV2,
    *,
    persistence: ScenarioOrchestrationV2PersistencePort,
    user_email: str,
    scenario_version_id: str,
    attempt_id: Optional[str] = None,
) -> StartOrResumeScenarioRunResultV2:
    """Start a new Engine V2 attempt or resume an existing one.

    ``attempt_id`` selects which of these V69
    ``start_or_resume_scenario_attempt_v1`` RPC behaviors applies; the
    caller-supplied envelope never carries attempt identity of its own --
    identity is always driven by ``p_attempt_id`` plus the caller's trusted
    user/scenario-version identity.

    - If ``attempt_id`` is omitted, a fresh non-nil UUIDv4 is minted and used
      for both Engine V2 initialization and ``p_attempt_id`` so a brand-new
      attempt's envelope stays attempt-bound. If that mint conflicts with an
      existing in-progress attempt (typical after browser session loss), this
      function performs **one** recovery RPC with ``p_attempt_id=NULL``, which
      V69 treats as resume-existing, then reloads/replays that row.
    - If ``attempt_id`` is supplied and the caller has **no** existing
      in-progress attempt, the RPC creates a **new** attempt using that id,
      provided it is not already in use by any other row.
    - If the caller already has an existing in-progress attempt and a
      non-null ``p_attempt_id`` does not match it, V69 fails closed with
      ``attempt_id_conflict`` (recovered automatically only when the caller
      omitted ``attempt_id``).
    - If the supplied ``p_attempt_id`` already exists as some *other* row's
      id (any owner, any scenario version), V69 fails closed with
      ``attempt_id_collision``.

    In every case, this function never trusts the RPC's returned identity by
    itself: after the RPC call it reloads the trusted persisted attempt row
    and replays canonical decisions from the database before returning, so
    the final ``attempt_id`` and ``created`` flag reflect verified server
    state, not raw RPC response data.
    """
    _validate_engine_content(content)
    normalized_email = _normalize_email_or_raise(user_email)
    version_id = _require_uuid(scenario_version_id, "scenario_version_id")

    caller_supplied_attempt_id = attempt_id is not None
    trusted_attempt_id = (
        _require_uuid(attempt_id, "attempt_id", allow_nil=False)
        if caller_supplied_attempt_id
        else str(uuid.uuid4())
    )
    run = start_scenario_run_v2(content, attempt_id=trusted_attempt_id)
    params = build_start_or_resume_rpc_params_v2(
        run,
        user_email=normalized_email,
        scenario_version_id=version_id,
    )

    def _invoke_start(rpc_params: Mapping[str, Any], *, expected: Optional[str]) -> Any:
        rpc_data = _wrap_persistence_call(
            "start_or_resume_scenario_attempt_v1",
            lambda: persistence.call_start_or_resume_scenario_attempt_v1(rpc_params),
        )
        try:
            return parse_start_or_resume_rpc_response_v2(rpc_data, expected_attempt_id=expected)
        except ScenarioPersistenceV2RpcResponseError as exc:
            raise ScenarioOrchestrationV2MalformedPersistenceResponseError(str(exc)) from exc

    try:
        rpc_result = _invoke_start(params, expected=trusted_attempt_id)
    except ScenarioOrchestrationV2IdentityMismatchError as exc:
        # attempt_id_conflict maps here. Recover only when the caller omitted
        # attempt identity (browser session loss / first authoritative load).
        message = str(exc)
        if caller_supplied_attempt_id or "attempt_id_conflict:" not in message:
            raise
        recovery_params = dict(params)
        recovery_params["p_attempt_id"] = None
        rpc_result = _invoke_start(recovery_params, expected=None)

    # Never trust the RPC envelope alone -- reload trusted identity + replay.
    replayed, snapshot = resume_and_replay_scenario_run_v2(
        content,
        persistence=persistence,
        user_email=normalized_email,
        attempt_id=rpc_result.attempt_id,
    )

    if rpc_result.scenario_version_id != version_id:
        raise ScenarioOrchestrationV2IdentityMismatchError(
            "identity_mismatch: start RPC returned a different scenario_version_id than requested"
        )
    if rpc_result.engine_version != ENGINE_VERSION:
        raise ScenarioOrchestrationV2IdentityMismatchError(
            "identity_mismatch: start RPC returned an unexpected engine_version"
        )
    if rpc_result.scenario_content_sha256 != content.canonical_content_sha256:
        raise ScenarioOrchestrationV2IdentityMismatchError(
            "identity_mismatch: start RPC returned a different scenario_content_sha256 than content"
        )

    if replayed.is_complete:
        submission_context = ScenarioOrchestrationSubmissionContextV2(
            user_email=normalized_email,
            attempt_id=snapshot.attempt_id,
            scenario_version_id=snapshot.scenario_version_id,
            expected_sequence_number=replayed.expected_sequence_number,
            expected_scene_id=replayed.current_scene_id or "",
            cached_envelope=_deep_copy_json(snapshot.serialized_engine_state),
            visible_option_ids=(),
            run=replayed,
        )
    else:
        submission_context = _build_submission_context(
            user_email=normalized_email,
            content=content,
            snapshot=snapshot,
            run=replayed,
        )
    return StartOrResumeScenarioRunResultV2(
        attempt_id=snapshot.attempt_id,
        created=rpc_result.created,
        run=replayed,
        submission_context=submission_context,
        learner_view=_build_learner_view(replayed),
    )


# ---------------------------------------------------------------------------
# Public API — submit
# ---------------------------------------------------------------------------


def submit_scenario_decision_v2(
    content: ScenarioContentV2,
    *,
    persistence: ScenarioOrchestrationV2PersistencePort,
    submission_context: ScenarioOrchestrationSubmissionContextV2,
    selected_option_id: str,
    idempotency_key: Optional[str] = None,
) -> SubmitScenarioDecisionResultV2:
    """Submit one learner-visible decision with CAS + idempotency.

    ``idempotency_key`` must be supplied explicitly for a retry -- this
    function generates a fresh UUIDv4 only when ``idempotency_key`` is
    ``None`` (first submission). The returned result always includes the key
    used so callers can retry safely with the same value.
    """
    _validate_engine_content(content)
    normalized_email = _normalize_email_or_raise(submission_context.user_email)
    if not isinstance(selected_option_id, str) or not selected_option_id.strip():
        raise ScenarioOrchestrationV2InvalidRequestError(
            "invalid_selected_option_id: must be a non-empty string"
        )
    if selected_option_id not in submission_context.visible_option_ids:
        raise ScenarioOrchestrationV2InvalidRequestError(
            f"invalid_selected_option_id: option {selected_option_id!r} is not visible on the current scene"
        )

    run_before = submission_context.run
    if run_before.is_complete or run_before.current_scene_id is None:
        raise ScenarioOrchestrationV2StaleRunError(
            "stale_run: cannot submit a decision against a completed attempt"
        )
    if run_before.expected_sequence_number != submission_context.expected_sequence_number:
        raise ScenarioOrchestrationV2StaleRunError(
            "stale_run: submission_context.expected_sequence_number disagrees with the embedded run"
        )
    if run_before.current_scene_id != submission_context.expected_scene_id:
        raise ScenarioOrchestrationV2StaleRunError(
            "stale_run: submission_context.expected_scene_id disagrees with the embedded run"
        )

    decision = ScenarioDecisionInputV2(
        sequence_number=submission_context.expected_sequence_number,
        scene_id=submission_context.expected_scene_id,
        option_id=selected_option_id,
    )
    run_after = apply_decision_v2(run_before, decision)

    params = build_submit_decision_rpc_params_v2(
        run_before,
        run_after,
        decision,
        user_email=normalized_email,
        idempotency_key=idempotency_key,
    )
    used_idempotency_key = params["p_idempotency_key"]

    rpc_data = _wrap_persistence_call(
        "submit_scenario_decision_v1",
        lambda: persistence.call_submit_scenario_decision_v1(params),
    )
    try:
        rpc_result = parse_submit_decision_rpc_response_v2(
            rpc_data,
            expected_attempt_id=submission_context.attempt_id,
        )
    except ScenarioPersistenceV2RpcResponseError as exc:
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(str(exc)) from exc

    replayed, snapshot = resume_and_replay_scenario_run_v2(
        content,
        persistence=persistence,
        user_email=normalized_email,
        attempt_id=submission_context.attempt_id,
    )
    _assert_runs_equivalent(run_after, replayed)

    if rpc_result.sequence_number != decision.sequence_number:
        raise ScenarioOrchestrationV2MalformedPersistenceResponseError(
            "malformed_response: submit RPC sequence_number does not match the submitted decision"
        )

    next_context = _build_submission_context(
        user_email=normalized_email,
        content=content,
        snapshot=snapshot,
        run=replayed,
    ) if not replayed.is_complete else ScenarioOrchestrationSubmissionContextV2(
        user_email=normalized_email,
        attempt_id=snapshot.attempt_id,
        scenario_version_id=snapshot.scenario_version_id,
        expected_sequence_number=replayed.expected_sequence_number,
        expected_scene_id=replayed.current_scene_id or "",
        cached_envelope=_deep_copy_json(snapshot.serialized_engine_state),
        visible_option_ids=(),
        run=replayed,
    )

    return SubmitScenarioDecisionResultV2(
        attempt_id=snapshot.attempt_id,
        sequence_number=rpc_result.sequence_number,
        decision_id=rpc_result.decision_id,
        idempotent_replay=rpc_result.idempotent_replay,
        idempotency_key=used_idempotency_key,
        run=replayed,
        submission_context=next_context,
        learner_view=_build_learner_view(replayed),
    )
