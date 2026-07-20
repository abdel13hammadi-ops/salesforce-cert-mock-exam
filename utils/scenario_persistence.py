"""Python persistence adapter for V68 Scenario Simulator learner attempts.

This module is the ONLY supported way application code may durably persist a
Scenario Simulator learner attempt or decision. It talks exclusively to the
four RPCs defined by
supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql:

    start_or_resume_scenario_attempt_v1
    get_scenario_attempt_v1
    submit_scenario_decision_v1
    abandon_scenario_attempt_v1

There is deliberately no `complete_scenario_attempt_v1` — completion happens
atomically inside `submit_decision(...)` when the caller declares the
decision terminal (see the migration header for the full rationale).

What this module is NOT
------------------------
It never implements scene transitions, option validity, scoring, domain
performance, learner-state changes, ending selection, or terminal-outcome
calculation — that is exclusively `utils/scenario_engine.py`'s job. Every
`state_before`/`state_after`/`initial_serialized_state` value accepted here is
expected to already be the output of `utils.scenario_engine.serialize_run_snapshot(...)`
(or an equivalent already-validated engine snapshot) — this module only
validates its *shape*, never recomputes its content.

It also never writes directly to `scenario_attempts` or `scenario_decisions`
— every mutation goes through `client.rpc(...)`, never `client.table(...)`.

SIM-PERSIST-04C corrections
----------------------------
This module was hardened after a line-by-line security/integrity review:
`is_terminal` must be an actual `bool` (never `bool(is_terminal)`),
`expected_sequence_number` must be an actual `int` and not `bool`, a
caller-supplied `idempotency_key` must be UUIDv4 specifically, a
caller-supplied `request_fingerprint` is stripped but never case-folded (an
uppercase-containing value is rejected, not silently lowercased),
`compute_request_fingerprint(...)` now explicitly covers
`terminal_result_snapshot`, and both `start_or_resume_attempt(...)` and
`submit_decision(...)` now validate the same snapshot IDENTITY/LIFECYCLE
consistency rules the RPCs themselves enforce (see
`_validate_initial_state_consistency` / `_validate_decision_snapshot_consistency`),
raising `ScenarioSnapshotConsistencyError` locally before ever calling SQL.

SIM-PERSIST-04E corrections
----------------------------
A further independent review found five more defects, all corrected here:
`terminal_ending_mismatch` (new SQL exception prefix, mirrored locally in
`_validate_decision_snapshot_consistency` -- a terminal decision's
`terminal_result_snapshot.endingId` must be a normalized, non-empty string
EXACTLY equal to the separately-supplied `terminal_ending_id`) now maps to
`ScenarioSnapshotConsistencyError`; the public `compute_request_fingerprint(...)`
helper no longer coerces its inputs with permissive `int(...)`/`bool(...)`/
`str(...)` -- it now uses this module's own strict helpers
(`_require_strict_int`, `_require_strict_bool`, `_require_uuid_str`,
`_require_nonempty_str`), rejecting e.g. `"1"` or `True` as a sequence
number rather than silently accepting them; every RPC response is now
parsed with focused strict helpers (`_require_strict_bool_field`,
`_require_strict_int_field`, `_require_uuid_field`,
`_require_json_object_field`, `_require_nullable_json_object_field`,
`_require_lifecycle_status_field`, `_require_content_hash_field`) that
raise `ScenarioPersistenceBackendError` for a response field that is not
already exactly the required type/shape -- previously, e.g.
`serialized_engine_state=[]` (a falsy but non-object value) would have been
silently coerced into `{}` via `dict(value or {})`, and `created="false"`
would have been silently accepted as truthy via `bool(...)`; and
`validate_serialized_engine_state(...)` no longer normalizes
`simulationId`/`version`/`engineVersion`/`currentSceneId` (they must already
be trimmed) or `canonicalContentSha256` (it must already be lowercase) --
this function validates shape, it never silently rewrites a caller's value
to make it pass.

SIM-PERSIST-04F corrections
----------------------------
A further independent review found four more defects, all corrected here:
`submit_decision(...)` now ALWAYS computes the canonical request fingerprint
from its own already-validated, already-normalized inputs -- a
caller-supplied `request_fingerprint` is used only as an extra consistency
check against that computed value (raising the new, local-only
`request_fingerprint_mismatch:` `ScenarioPersistenceValidationError`, without
ever calling the RPC, when it disagrees) and is never sent to the RPC as an
independently-trusted value, closing the gap where a format-valid but
content-inconsistent supplied fingerprint could previously have been
forwarded as-is; `_require_nonempty_str` (used for scene ids, option ids,
ending ids, and engine-version text) no longer does `str(value or "")` --
it now requires an actual `str` up front, rejecting an integer, `bool`,
`uuid.UUID` object, or other non-string value instead of silently
stringifying it into something that could then pass the non-empty/trim
checks; and this module's docstring/comments now also describe the mirrored
SQL-side corrections (`get_scenario_attempt_v1`'s new `FOR SHARE` lock,
`submit_scenario_decision_v1`'s idempotent-retry check now binding every
stored request field -- not just `request_fingerprint` -- and its terminal
`state_after.currentSceneId` now requiring an EXPLICIT JSON null rather than
merely tolerating a missing key) that this adapter's own validation must stay
consistent with even though this module does not implement them itself.

Ownership
----------
Every function requires an already-verified `user_email`. This module never
trusts a browser-supplied ownership value — callers must obtain `user_email`
from the existing verified-session access-control layer
(`utils.access_control.get_current_user_email()` for Streamlit callers), the
same pattern this application's pre-existing exam_attempts/question_attempts
persistence already follows (see `utils/question_selection.py`). Email
normalization (`lower(btrim(...))`) happens exactly once, in
`normalize_scenario_persistence_email`, and is applied identically here and
in every one of the four RPCs.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# The exact field set `utils.scenario_engine.serialize_run_snapshot(...)`
# always emits. `state_before`/`state_after`/`initial_serialized_state` are
# expected to be that exact shape -- this module validates it structurally
# without importing utils.scenario_engine (keeping this adapter's runtime
# dependency surface limited to the Supabase client), so the two modules'
# contracts must be kept in sync by convention, not by import.
REQUIRED_SERIALIZED_STATE_KEYS = frozenset(
    {
        "simulationId",
        "version",
        "canonicalContentSha256",
        "engineVersion",
        "currentSceneId",
        "state",
        "flags",
        "decisionHistory",
        "isComplete",
        "terminalResult",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScenarioPersistenceError(Exception):
    """Base error for the Scenario Simulator attempt-persistence adapter."""


class ScenarioPersistenceValidationError(ScenarioPersistenceError):
    """Raised for a malformed caller input, whether caught locally before any
    RPC call, or reflected back from one of the four RPCs' own `invalid_*`
    validation (scalar format, JSON-object shape, terminal-field
    consistency)."""


class ScenarioVersionMismatchError(ScenarioPersistenceError):
    """Raised when the target scenario_versions row does not exist, is not
    published, or does not match the caller's expected engine_version /
    scenario_content_sha256."""


class ScenarioAttemptNotFoundError(ScenarioPersistenceError):
    """Raised when an attempt id does not exist, OR exists but is owned by a
    different learner. Deliberately identical for both cases -- see
    `get_scenario_attempt_v1`'s and `submit_scenario_decision_v1`'s own
    documentation for why this must never be distinguishable by a caller."""


class ScenarioAttemptNotInProgressError(ScenarioPersistenceError):
    """Raised when a decision is submitted against (or abandonment is
    requested for) an attempt that is not currently in_progress."""


class ScenarioSequenceConflictError(ScenarioPersistenceError):
    """Raised when `expected_sequence_number` does not equal the attempt's
    actual `next_sequence_number`."""


class ScenarioSceneConflictError(ScenarioPersistenceError):
    """Raised when `expected_scene_id` does not equal the attempt's actual
    `current_scene_id`."""


class ScenarioStateConflictError(ScenarioPersistenceError):
    """Raised when `state_before` does not exactly match the attempt's
    persisted `serialized_engine_state`."""


class ScenarioIdempotencyConflictError(ScenarioPersistenceError):
    """Raised when an idempotency key is reused on the same attempt with a
    different request fingerprint (a genuine conflict, not a safe retry)."""


class ScenarioSnapshotConsistencyError(ScenarioPersistenceError):
    """SIM-PERSIST-04C/04E: raised when a serialized-engine-state snapshot
    fails an identity/lifecycle consistency check -- either locally, before
    any RPC call (e.g. `state_before`/`state_after` immutable-identity
    fields do not match each other), or reflected back from one of the two
    RPCs' own `state_identity_mismatch` / `state_lifecycle_mismatch` /
    `terminal_result_mismatch` / `terminal_ending_mismatch` /
    `invalid_initial_state_identity` / `invalid_initial_state_lifecycle`
    validation. This is always a pure equality/shape check against values
    the caller itself supplied -- never a recomputation of which scene,
    score, or ending is correct."""


class ScenarioInsertGuardViolationError(ScenarioPersistenceError):
    """SIM-PERSIST-04C: raised only if a `scenario_attempts` or
    `scenario_decisions` INSERT reaches the database without this module's
    own transaction-local insert guard already being set for that exact row
    id -- i.e. some code path attempted to insert into one of these tables
    outside `start_or_resume_scenario_attempt_v1` /
    `submit_scenario_decision_v1`. This adapter never does that itself (it
    never calls `client.table(...)` at all -- see the module docstring), so
    this exception should never actually be raised through normal use of
    this module; it exists purely so a future bug that adds a direct
    `client.table(...)` write path fails loudly instead of silently
    succeeding."""


class ScenarioPersistenceBackendError(ScenarioPersistenceError):
    """Raised for any RPC failure this module cannot map to a more specific
    error, for a malformed/incomplete RPC response, or for a returned
    identity (attempt id / scenario_version_id / engine_version /
    scenario_content_sha256) that does not match what the caller supplied or
    requested."""


# Ordered so a more specific prefix is never shadowed by a shorter one that
# happens to also match (none currently collide, but order is preserved
# deliberately for future additions).
_ERROR_PREFIX_MAP: Tuple[Tuple[str, type], ...] = (
    ("invalid_user_email:", ScenarioPersistenceValidationError),
    ("invalid_attempt_id:", ScenarioPersistenceValidationError),
    ("invalid_scenario_version_id:", ScenarioPersistenceValidationError),
    ("invalid_idempotency_key:", ScenarioPersistenceValidationError),
    ("invalid_sequence_number:", ScenarioPersistenceValidationError),
    ("invalid_expected_scene_id:", ScenarioPersistenceValidationError),
    ("invalid_selected_option_id:", ScenarioPersistenceValidationError),
    ("invalid_request_fingerprint:", ScenarioPersistenceValidationError),
    ("invalid_state_before:", ScenarioPersistenceValidationError),
    ("invalid_state_after:", ScenarioPersistenceValidationError),
    ("invalid_is_terminal:", ScenarioPersistenceValidationError),
    ("invalid_resulting_scene_id:", ScenarioPersistenceValidationError),
    ("invalid_terminal_ending_id:", ScenarioPersistenceValidationError),
    ("invalid_terminal_result_snapshot:", ScenarioPersistenceValidationError),
    ("invalid_terminal_fields:", ScenarioPersistenceValidationError),
    ("invalid_initial_scene:", ScenarioPersistenceValidationError),
    ("invalid_initial_state:", ScenarioPersistenceValidationError),
    ("invalid_initial_state_identity:", ScenarioSnapshotConsistencyError),
    ("invalid_initial_state_lifecycle:", ScenarioSnapshotConsistencyError),
    ("scenario_version_not_found:", ScenarioVersionMismatchError),
    ("scenario_version_not_published:", ScenarioVersionMismatchError),
    ("engine_version_mismatch:", ScenarioVersionMismatchError),
    ("content_hash_mismatch:", ScenarioVersionMismatchError),
    ("attempt_not_found:", ScenarioAttemptNotFoundError),
    ("attempt_not_in_progress:", ScenarioAttemptNotInProgressError),
    ("idempotency_key_conflict:", ScenarioIdempotencyConflictError),
    ("sequence_mismatch:", ScenarioSequenceConflictError),
    ("scene_mismatch:", ScenarioSceneConflictError),
    ("state_before_mismatch:", ScenarioStateConflictError),
    ("state_identity_mismatch:", ScenarioSnapshotConsistencyError),
    ("state_lifecycle_mismatch:", ScenarioSnapshotConsistencyError),
    ("terminal_result_mismatch:", ScenarioSnapshotConsistencyError),
    ("terminal_ending_mismatch:", ScenarioSnapshotConsistencyError),
    ("attempt_insert_guard_violation:", ScenarioInsertGuardViolationError),
    ("decision_insert_guard_violation:", ScenarioInsertGuardViolationError),
    ("start_or_resume_failed:", ScenarioPersistenceBackendError),
)


def _extract_error_message(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return str(exc).strip()


def _map_rpc_exception(rpc_name: str, exc: BaseException) -> ScenarioPersistenceError:
    message = _extract_error_message(exc)
    for prefix, exc_cls in _ERROR_PREFIX_MAP:
        if message.startswith(prefix):
            return exc_cls(message)
    return ScenarioPersistenceBackendError(f"RPC {rpc_name!r} failed: {message}")


# ---------------------------------------------------------------------------
# Normalization / validation helpers
# ---------------------------------------------------------------------------


def normalize_scenario_persistence_email(email: Optional[str]) -> str:
    """The single, focused email-normalization helper used everywhere in
    this module. Matches the exact `lower(btrim(user_email))` normalization
    enforced by every one of the four RPCs."""
    normalized = str(email or "").strip().lower()
    if not normalized or "@" not in normalized:
        raise ScenarioPersistenceValidationError(
            "invalid_user_email: a non-empty, non-whitespace email address is required"
        )
    return normalized


def _require_nonempty_str(value: Any, field: str) -> str:
    """SIM-PERSIST-04F: require an actual `str`, never `str(value or "")`.
    Used for scene ids, option ids, ending ids, and engine-version text --
    every one of these is a caller/engine-supplied IDENTIFIER, not free-form
    display text, so an integer, `bool`, `uuid.UUID` object, or any other
    non-string value must be rejected outright rather than silently
    stringified into something that could then pass the non-empty/trim
    checks below (e.g. `str(True)` == `"True"`, `str(uuid.uuid4())` would
    otherwise silently produce *a* seemingly-valid identifier)."""
    if not isinstance(value, str):
        raise ScenarioPersistenceValidationError(
            f"invalid_{field}: must be an actual str, got {type(value).__name__} ({value!r})"
        )
    text = value.strip()
    if not text:
        raise ScenarioPersistenceValidationError(f"invalid_{field}: must be a non-empty, non-whitespace string")
    return text


def _require_uuid_str(value: Any, field: str) -> str:
    text = str(value or "").strip()
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ScenarioPersistenceValidationError(f"invalid_{field}: must be a valid UUID, got {value!r}") from exc


def _require_uuid4_str(value: Any, field: str) -> str:
    """SIM-PERSIST-04C: like `_require_uuid_str`, but additionally requires
    UUID version 4 -- used for caller-supplied `idempotency_key`, which this
    module's own `generate_idempotency_key()` always produces as UUIDv4 and
    which the underlying `idempotency_key uuid` column has no way to enforce
    the version of by itself."""
    text = str(value or "").strip()
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ScenarioPersistenceValidationError(f"invalid_{field}: must be a valid UUID, got {value!r}") from exc
    if parsed.version != 4:
        raise ScenarioPersistenceValidationError(
            f"invalid_{field}: must be a version-4 UUID, got version {parsed.version} ({value!r})"
        )
    return str(parsed)


def _require_strict_bool(value: Any, field: str) -> bool:
    """SIM-PERSIST-04C: require an actual `bool`, never `bool(value)`.
    Rejects strings ("true"/"false"/"1"/...), integers (0/1), and any other
    truthy/falsy value that is not already exactly `True` or `False`."""
    if not isinstance(value, bool):
        raise ScenarioPersistenceValidationError(
            f"invalid_{field}: must be an actual bool, got {type(value).__name__} ({value!r})"
        )
    return value


def _require_strict_int(value: Any, field: str, *, minimum: Optional[int] = None) -> int:
    """SIM-PERSIST-04C: require an actual `int`, explicitly excluding `bool`
    (a `bool` is a subclass of `int` in Python, so `isinstance(True, int)` is
    `True` -- this helper rejects that case specifically)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioPersistenceValidationError(
            f"invalid_{field}: must be an actual int, not bool or {type(value).__name__} ({value!r})"
        )
    if minimum is not None and value < minimum:
        raise ScenarioPersistenceValidationError(f"invalid_{field}: must be >= {minimum}, got {value}")
    return value


def _require_content_hash(value: Any, field: str = "scenario_content_sha256") -> str:
    text = str(value or "").strip().lower()
    if not CONTENT_HASH_PATTERN.fullmatch(text):
        raise ScenarioPersistenceValidationError(
            f"invalid_{field}: must be exactly 64 lowercase hexadecimal characters"
        )
    return text


def _require_json_object(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ScenarioPersistenceValidationError(f"invalid_{field}: must be a JSON object (mapping)")
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ScenarioPersistenceValidationError(f"invalid_{field}: not JSON-serializable ({exc})") from exc
    return dict(value)


def validate_serialized_engine_state(value: Any, *, field: str = "serialized_engine_state") -> Dict[str, Any]:
    """Validate that `value` has the exact shape
    `utils.scenario_engine.serialize_run_snapshot(...)` always produces.

    This is deliberately structural, not a recomputation -- it never
    re-derives state, score, or ending; it only rejects a payload that could
    not possibly have come from that function (wrong type, missing identity
    field, malformed content hash, non-JSON-serializable content) before it
    is ever sent to SQL as `serialized_engine_state` / `state_before` /
    `state_after`.
    """
    payload = _require_json_object(value, field)

    missing = REQUIRED_SERIALIZED_STATE_KEYS - set(payload.keys())
    if missing:
        raise ScenarioPersistenceValidationError(
            f"invalid_{field}: missing required key(s) {sorted(missing)}"
        )

    # SIM-PERSIST-04E Correction 7: every identity/lifecycle field below must
    # already be normalized -- this function validates SHAPE, it never
    # silently rewrites (trims, lowercases) a caller-supplied value to make
    # it pass. A caller whose upstream engine snapshot is not already
    # normalized has its own bug that must be fixed at the source, not
    # papered over here.
    for identity_field in ("simulationId", "version", "engineVersion"):
        identity_value = payload.get(identity_field)
        if not isinstance(identity_value, str) or not identity_value.strip():
            raise ScenarioPersistenceValidationError(
                f"invalid_{field}: {identity_field} must be a non-empty string"
            )
        if identity_value != identity_value.strip():
            raise ScenarioPersistenceValidationError(
                f"invalid_{field}: {identity_field} must already be trimmed (no leading/trailing "
                "whitespace) -- it is never silently normalized"
            )

    content_hash = payload.get("canonicalContentSha256")
    if not isinstance(content_hash, str) or not CONTENT_HASH_PATTERN.fullmatch(content_hash):
        raise ScenarioPersistenceValidationError(
            f"invalid_{field}: canonicalContentSha256 must already be exactly 64 lowercase hexadecimal "
            "characters -- an uppercase-containing value is rejected, never silently lowercased"
        )

    if not isinstance(payload.get("decisionHistory"), list):
        raise ScenarioPersistenceValidationError(f"invalid_{field}: decisionHistory must be a JSON array")

    if not isinstance(payload.get("isComplete"), bool):
        raise ScenarioPersistenceValidationError(f"invalid_{field}: isComplete must be a boolean")

    current_scene_id = payload.get("currentSceneId")
    if current_scene_id is not None:
        if not isinstance(current_scene_id, str) or not current_scene_id.strip():
            raise ScenarioPersistenceValidationError(
                f"invalid_{field}: currentSceneId must be a non-empty string or null"
            )
        if current_scene_id != current_scene_id.strip():
            raise ScenarioPersistenceValidationError(
                f"invalid_{field}: currentSceneId must already be trimmed (no leading/trailing "
                "whitespace) when not null"
            )

    return payload


def _validate_initial_state_consistency(
    state: Mapping[str, Any],
    *,
    engine_version: str,
    scenario_content_sha256: str,
    initial_current_scene_id: str,
) -> None:
    """SIM-PERSIST-04C snapshot IDENTITY/LIFECYCLE integrity boundary for a
    newly-created attempt's `initial_serialized_state`, mirroring
    `start_or_resume_scenario_attempt_v1`'s own
    `invalid_initial_state_identity` / `invalid_initial_state_lifecycle`
    checks exactly, so a caller-side bug is caught here first (with the same
    exception class and message prefix the RPC would otherwise raise for the
    identical mistake). Pure equality/shape checks -- never a computation of
    which scene, score, or ending is correct."""
    if state.get("engineVersion") != engine_version:
        raise ScenarioSnapshotConsistencyError(
            "invalid_initial_state_identity: initial_serialized_state.engineVersion does not match "
            "the pinned engine_version"
        )
    if state.get("canonicalContentSha256") != scenario_content_sha256:
        raise ScenarioSnapshotConsistencyError(
            "invalid_initial_state_identity: initial_serialized_state.canonicalContentSha256 does not "
            "match the pinned scenario_content_sha256"
        )
    if state.get("currentSceneId") != initial_current_scene_id:
        raise ScenarioSnapshotConsistencyError(
            "invalid_initial_state_lifecycle: initial_serialized_state.currentSceneId does not match "
            "initial_current_scene_id"
        )
    if state.get("isComplete") is not False:
        raise ScenarioSnapshotConsistencyError(
            "invalid_initial_state_lifecycle: initial_serialized_state.isComplete must be false for a "
            "newly created attempt"
        )
    if state.get("terminalResult") is not None:
        raise ScenarioSnapshotConsistencyError(
            "invalid_initial_state_lifecycle: initial_serialized_state.terminalResult must be null for "
            "a newly created attempt"
        )


def _validate_decision_snapshot_consistency(
    state_before: Mapping[str, Any],
    state_after: Mapping[str, Any],
    *,
    expected_scene_id: str,
    is_terminal: bool,
    resulting_scene_id: Optional[str],
    terminal_ending_id: Optional[str] = None,
    terminal_result_snapshot: Optional[Mapping[str, Any]],
) -> None:
    """SIM-PERSIST-04C/04E snapshot IDENTITY/LIFECYCLE integrity boundary for
    a decision submission, mirroring `submit_scenario_decision_v1`'s own
    `state_identity_mismatch` / `state_lifecycle_mismatch` /
    `terminal_result_mismatch` / `terminal_ending_mismatch` checks exactly,
    so a caller-side bug is caught here first (with the same exception class
    and message prefix the RPC would otherwise raise for the identical
    mistake). Pure equality/shape checks -- never a computation of which
    scene, score, or ending is correct. `state_before`'s consistency against
    the attempt's actually persisted `serialized_engine_state` can only ever
    be checked server-side (this module has no local view of that value) --
    that remains `state_before_mismatch` / `ScenarioStateConflictError`,
    raised only by the RPC.

    SIM-PERSIST-04E: for a terminal decision, also requires
    `terminal_result_snapshot.endingId` to be a normalized, non-empty string
    EXACTLY equal to `terminal_ending_id` -- these two caller-supplied
    identities must never be allowed to silently disagree."""
    if is_terminal:
        ending_id = terminal_result_snapshot.get("endingId") if terminal_result_snapshot else None
        if not isinstance(ending_id, str) or not ending_id.strip():
            raise ScenarioSnapshotConsistencyError(
                "terminal_ending_mismatch: terminal_result_snapshot.endingId must be a normalized, "
                "non-empty string for a terminal decision"
            )
        if ending_id != ending_id.strip():
            raise ScenarioSnapshotConsistencyError(
                "terminal_ending_mismatch: terminal_result_snapshot.endingId must already be trimmed "
                "(no leading/trailing whitespace)"
            )
        if ending_id != terminal_ending_id:
            raise ScenarioSnapshotConsistencyError(
                "terminal_ending_mismatch: terminal_result_snapshot.endingId does not equal "
                "terminal_ending_id"
            )

    identity_fields = ("simulationId", "version", "canonicalContentSha256", "engineVersion")
    if any(state_before.get(field) != state_after.get(field) for field in identity_fields):
        raise ScenarioSnapshotConsistencyError(
            "state_identity_mismatch: state_before and state_after immutable identity fields "
            f"({', '.join(identity_fields)}) do not match"
        )

    if state_before.get("currentSceneId") != expected_scene_id:
        raise ScenarioSnapshotConsistencyError(
            "state_lifecycle_mismatch: state_before.currentSceneId does not match expected_scene_id"
        )
    if state_before.get("isComplete") is not False:
        raise ScenarioSnapshotConsistencyError(
            "state_lifecycle_mismatch: state_before.isComplete must be false"
        )

    if is_terminal:
        if state_after.get("currentSceneId") is not None:
            raise ScenarioSnapshotConsistencyError(
                "state_lifecycle_mismatch: state_after.currentSceneId must be null for a terminal decision"
            )
        if state_after.get("isComplete") is not True:
            raise ScenarioSnapshotConsistencyError(
                "state_lifecycle_mismatch: state_after.isComplete must be true for a terminal decision"
            )
        terminal_result = state_after.get("terminalResult")
        if not isinstance(terminal_result, Mapping):
            raise ScenarioSnapshotConsistencyError(
                "state_lifecycle_mismatch: state_after.terminalResult must be a JSON object for a "
                "terminal decision"
            )
        if dict(terminal_result) != dict(terminal_result_snapshot or {}):
            raise ScenarioSnapshotConsistencyError(
                "terminal_result_mismatch: state_after.terminalResult does not equal terminal_result_snapshot"
            )
    else:
        if state_after.get("currentSceneId") != resulting_scene_id:
            raise ScenarioSnapshotConsistencyError(
                "state_lifecycle_mismatch: state_after.currentSceneId does not match resulting_scene_id"
            )
        if state_after.get("isComplete") is not False:
            raise ScenarioSnapshotConsistencyError(
                "state_lifecycle_mismatch: state_after.isComplete must be false for a non-terminal decision"
            )
        if state_after.get("terminalResult") is not None:
            raise ScenarioSnapshotConsistencyError(
                "state_lifecycle_mismatch: state_after.terminalResult must be null for a non-terminal decision"
            )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def generate_idempotency_key() -> str:
    """A fresh, Python-generated UUIDv4 idempotency key.

    Called by `submit_decision(...)` whenever the caller does not supply its
    own key -- callers that need to retry a specific in-flight submission
    (e.g. after a network timeout) should instead generate one key
    themselves up front and pass it explicitly on every retry attempt.
    """
    return str(uuid.uuid4())


def compute_request_fingerprint(
    *,
    attempt_id: str,
    expected_sequence_number: int,
    expected_scene_id: str,
    selected_option_id: str,
    state_before: Mapping[str, Any],
    state_after: Mapping[str, Any],
    resulting_scene_id: Optional[str],
    is_terminal: bool,
    terminal_ending_id: Optional[str],
    terminal_result_snapshot: Optional[Mapping[str, Any]] = None,
) -> str:
    """Deterministic 64-lowercase-hex request fingerprint.

    Formula (chosen once, documented here, and never varied): SHA-256 over
    the `json.dumps(..., sort_keys=True, separators=(",", ":"))` canonical
    encoding of an object containing exactly attempt id, expected sequence
    number, expected current scene id, selected option id, state-before,
    state-after, resulting scene id, terminal flag, terminal ending id, and
    (as of SIM-PERSIST-04C) terminal result snapshot -- the same minimum
    field set
    supabase/migrations/20260719130000_v68_scenario_attempt_persistence_
    foundation.sql's header specifies. `terminal_result_snapshot` is
    included explicitly and independently of `state_after` (even though
    `state_after.terminalResult` is required to equal it for a terminal
    decision -- see `_validate_decision_snapshot_consistency`) so the
    fingerprint never silently depends on that equality holding.
    `sort_keys=True` canonicalizes nested object key order recursively, so
    two logically-identical payloads with differently-ordered dict keys
    always hash identically. Two calls with the same inputs always produce
    the same fingerprint; any change to any covered field -- including only
    `terminal_result_snapshot` -- changes it.

    SIM-PERSIST-04E: this is a *public* helper, so the scalar inputs it
    covers are now validated with this module's own strict helpers rather
    than the permissive `int(...)`/`bool(...)`/`str(...)` coercions used
    previously -- `int("1")` and `int(True)` would otherwise silently accept
    a string or a bool as a valid sequence number, and `bool(0)`/`bool(1)`
    would silently accept an integer as a valid terminal flag. A malformed
    caller input is rejected here, never silently coerced into something
    that happens to still produce *a* fingerprint.
    """
    attempt_id_value = _require_uuid_str(attempt_id, "attempt_id")
    sequence_value = _require_strict_int(expected_sequence_number, "sequence_number", minimum=1)
    scene_value = _require_nonempty_str(expected_scene_id, "expected_scene_id")
    option_value = _require_nonempty_str(selected_option_id, "selected_option_id")
    is_terminal_value = _require_strict_bool(is_terminal, "is_terminal")

    canonical_payload = {
        "attemptId": attempt_id_value,
        "expectedSequenceNumber": sequence_value,
        "expectedSceneId": scene_value,
        "selectedOptionId": option_value,
        "stateBefore": state_before,
        "stateAfter": state_after,
        "resultingSceneId": resulting_scene_id,
        "isTerminal": is_terminal_value,
        "terminalEndingId": terminal_ending_id,
        "terminalResultSnapshot": terminal_result_snapshot,
    }
    canonical_text = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# RPC-response parsing
# ---------------------------------------------------------------------------


def _first_row(data: Any) -> Optional[Dict[str, Any]]:
    if isinstance(data, list):
        return dict(data[0]) if data and isinstance(data[0], Mapping) else None
    if isinstance(data, Mapping):
        return dict(data)
    return None


def _require_row(data: Any, rpc_name: str) -> Dict[str, Any]:
    row = _first_row(data)
    if row is None:
        raise ScenarioPersistenceBackendError(f"malformed_response: RPC {rpc_name!r} returned no row")
    return row


def _require_field(row: Mapping[str, Any], key: str, rpc_name: str) -> Any:
    if key not in row:
        raise ScenarioPersistenceBackendError(f"malformed_response: RPC {rpc_name!r} response missing field {key!r}")
    return row[key]


# SIM-PERSIST-04E Correction 6: RPC responses are external input, exactly
# like caller input -- `bool(...)`/permissive `int(...)` coercions would
# silently accept e.g. `created="false"` (truthy!) or `sequence_number="1"`
# from a malformed or unexpectedly-shaped response. Every helper below
# raises `ScenarioPersistenceBackendError` (never
# `ScenarioPersistenceValidationError`, which is reserved for caller input)
# for a field that does not already have the exact required type/shape.
_LIFECYCLE_STATUSES = frozenset({"in_progress", "completed", "abandoned"})


def _require_strict_bool_field(row: Mapping[str, Any], key: str, rpc_name: str) -> bool:
    value = _require_field(row, key, rpc_name)
    if not isinstance(value, bool):
        raise ScenarioPersistenceBackendError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be an actual bool, "
            f"got {type(value).__name__} ({value!r})"
        )
    return value


def _require_strict_int_field(row: Mapping[str, Any], key: str, rpc_name: str) -> int:
    value = _require_field(row, key, rpc_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioPersistenceBackendError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be an actual int, not bool or "
            f"{type(value).__name__} ({value!r})"
        )
    return value


def _require_uuid_field(row: Mapping[str, Any], key: str, rpc_name: str) -> str:
    value = _require_field(row, key, rpc_name)
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ScenarioPersistenceBackendError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be a valid UUID, got {value!r}"
        ) from exc


def _require_json_object_field(row: Mapping[str, Any], key: str, rpc_name: str) -> Dict[str, Any]:
    value = _require_field(row, key, rpc_name)
    if not isinstance(value, Mapping):
        raise ScenarioPersistenceBackendError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be a JSON object, "
            f"got {type(value).__name__} ({value!r})"
        )
    return dict(value)


def _require_nullable_json_object_field(
    row: Mapping[str, Any], key: str, rpc_name: str
) -> Optional[Dict[str, Any]]:
    value = _require_field(row, key, rpc_name)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ScenarioPersistenceBackendError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be null or a JSON object, "
            f"got {type(value).__name__} ({value!r})"
        )
    return dict(value)


def _require_lifecycle_status_field(row: Mapping[str, Any], key: str, rpc_name: str) -> str:
    value = _require_field(row, key, rpc_name)
    if not isinstance(value, str) or value not in _LIFECYCLE_STATUSES:
        raise ScenarioPersistenceBackendError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be one of "
            f"{sorted(_LIFECYCLE_STATUSES)}, got {value!r}"
        )
    return value


def _require_content_hash_field(row: Mapping[str, Any], key: str, rpc_name: str) -> str:
    value = _require_field(row, key, rpc_name)
    if not isinstance(value, str) or not CONTENT_HASH_PATTERN.fullmatch(value):
        raise ScenarioPersistenceBackendError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be exactly 64 lowercase "
            f"hexadecimal characters, got {value!r}"
        )
    return value


def _resolve_client(client: Any) -> Any:
    if client is not None:
        return client
    from utils.access_control import get_supabase_admin_client  # noqa: PLC0415

    return get_supabase_admin_client()


def _call_rpc(client: Any, rpc_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = client.rpc(rpc_name, params).execute()
    except Exception as exc:  # noqa: BLE001 - RPC failures are re-raised as typed errors below.
        raise _map_rpc_exception(rpc_name, exc) from exc

    error = getattr(result, "error", None)
    if error:
        raise _map_rpc_exception(rpc_name, Exception(str(error)))

    return _require_row(getattr(result, "data", None), rpc_name)


# ---------------------------------------------------------------------------
# Typed results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioAttemptStartResult:
    attempt_id: str
    created: bool
    scenario_id: str
    scenario_version_id: str
    status: str
    current_scene_id: Optional[str]
    next_sequence_number: int
    serialized_engine_state: Dict[str, Any]
    engine_version: str
    scenario_content_sha256: str
    started_at: Optional[str]
    completed_at: Optional[str]
    abandoned_at: Optional[str]
    terminal_ending_id: Optional[str]
    terminal_result_snapshot: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class ScenarioAttemptSnapshot:
    attempt_id: str
    scenario_id: str
    scenario_version_id: str
    status: str
    current_scene_id: Optional[str]
    next_sequence_number: int
    serialized_engine_state: Dict[str, Any]
    engine_version: str
    scenario_content_sha256: str
    started_at: Optional[str]
    updated_at: Optional[str]
    completed_at: Optional[str]
    abandoned_at: Optional[str]
    terminal_ending_id: Optional[str]
    terminal_result_snapshot: Optional[Dict[str, Any]]
    decisions: Tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class ScenarioDecisionSubmissionResult:
    decision_id: str
    attempt_id: str
    sequence_number: int
    idempotent_replay: bool
    attempt_status: str
    current_scene_id: Optional[str]
    next_sequence_number: int
    serialized_engine_state: Dict[str, Any]
    completed_at: Optional[str]
    terminal_ending_id: Optional[str]
    terminal_result_snapshot: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class ScenarioAttemptAbandonResult:
    attempt_id: str
    status: str
    abandoned_at: Optional[str]


def _parse_start_or_resume_row(row: Mapping[str, Any]) -> ScenarioAttemptStartResult:
    name = "start_or_resume_scenario_attempt_v1"
    return ScenarioAttemptStartResult(
        attempt_id=_require_uuid_field(row, "attempt_id", name),
        created=_require_strict_bool_field(row, "created", name),
        scenario_id=_require_uuid_field(row, "scenario_id", name),
        scenario_version_id=_require_uuid_field(row, "scenario_version_id", name),
        status=_require_lifecycle_status_field(row, "status", name),
        current_scene_id=row.get("current_scene_id"),
        next_sequence_number=_require_strict_int_field(row, "next_sequence_number", name),
        serialized_engine_state=_require_json_object_field(row, "serialized_engine_state", name),
        engine_version=str(_require_field(row, "engine_version", name)),
        scenario_content_sha256=_require_content_hash_field(row, "scenario_content_sha256", name),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        abandoned_at=row.get("abandoned_at"),
        terminal_ending_id=row.get("terminal_ending_id"),
        terminal_result_snapshot=_require_nullable_json_object_field(row, "terminal_result_snapshot", name),
    )


def _parse_attempt_snapshot_row(row: Mapping[str, Any]) -> ScenarioAttemptSnapshot:
    name = "get_scenario_attempt_v1"
    decisions_raw = _require_field(row, "decisions", name)
    if not isinstance(decisions_raw, list):
        raise ScenarioPersistenceBackendError(
            f"malformed_response: RPC {name!r} response field 'decisions' must be a list"
        )
    return ScenarioAttemptSnapshot(
        attempt_id=_require_uuid_field(row, "attempt_id", name),
        scenario_id=_require_uuid_field(row, "scenario_id", name),
        scenario_version_id=_require_uuid_field(row, "scenario_version_id", name),
        status=_require_lifecycle_status_field(row, "status", name),
        current_scene_id=row.get("current_scene_id"),
        next_sequence_number=_require_strict_int_field(row, "next_sequence_number", name),
        serialized_engine_state=_require_json_object_field(row, "serialized_engine_state", name),
        engine_version=str(_require_field(row, "engine_version", name)),
        scenario_content_sha256=_require_content_hash_field(row, "scenario_content_sha256", name),
        started_at=row.get("started_at"),
        updated_at=row.get("updated_at"),
        completed_at=row.get("completed_at"),
        abandoned_at=row.get("abandoned_at"),
        terminal_ending_id=row.get("terminal_ending_id"),
        terminal_result_snapshot=_require_nullable_json_object_field(row, "terminal_result_snapshot", name),
        decisions=tuple(dict(entry) for entry in decisions_raw),
    )


def _parse_submit_decision_row(row: Mapping[str, Any]) -> ScenarioDecisionSubmissionResult:
    name = "submit_scenario_decision_v1"
    return ScenarioDecisionSubmissionResult(
        decision_id=_require_uuid_field(row, "decision_id", name),
        attempt_id=_require_uuid_field(row, "attempt_id", name),
        sequence_number=_require_strict_int_field(row, "sequence_number", name),
        idempotent_replay=_require_strict_bool_field(row, "idempotent_replay", name),
        attempt_status=_require_lifecycle_status_field(row, "attempt_status", name),
        current_scene_id=row.get("current_scene_id"),
        next_sequence_number=_require_strict_int_field(row, "next_sequence_number", name),
        serialized_engine_state=_require_json_object_field(row, "serialized_engine_state", name),
        completed_at=row.get("completed_at"),
        terminal_ending_id=row.get("terminal_ending_id"),
        terminal_result_snapshot=_require_nullable_json_object_field(row, "terminal_result_snapshot", name),
    )


def _parse_abandon_row(row: Mapping[str, Any]) -> ScenarioAttemptAbandonResult:
    name = "abandon_scenario_attempt_v1"
    return ScenarioAttemptAbandonResult(
        attempt_id=_require_uuid_field(row, "attempt_id", name),
        status=_require_lifecycle_status_field(row, "status", name),
        abandoned_at=row.get("abandoned_at"),
    )


# ---------------------------------------------------------------------------
# Public adapter API
# ---------------------------------------------------------------------------


def start_or_resume_attempt(
    client: Any = None,
    *,
    user_email: str,
    scenario_version_id: str,
    initial_current_scene_id: str,
    initial_serialized_state: Mapping[str, Any],
    engine_version: str,
    scenario_content_sha256: str,
) -> ScenarioAttemptStartResult:
    """Start a new attempt, or resume the caller's existing in_progress
    attempt, for one exact (user_email, scenario_version_id) pair.

    `initial_current_scene_id` and `initial_serialized_state` are used ONLY
    when a new attempt is actually created; they are ignored when an
    existing in_progress attempt is resumed (the RPC returns that attempt's
    own persisted values instead). Calls
    `public.start_or_resume_scenario_attempt_v1`.
    """
    normalized_email = normalize_scenario_persistence_email(user_email)
    version_id = _require_uuid_str(scenario_version_id, "scenario_version_id")
    scene_id = _require_nonempty_str(initial_current_scene_id, "initial_current_scene_id")
    validated_state = validate_serialized_engine_state(
        initial_serialized_state, field="initial_serialized_state"
    )
    engine_version_value = _require_nonempty_str(engine_version, "engine_version")
    content_hash_value = _require_content_hash(scenario_content_sha256)
    _validate_initial_state_consistency(
        validated_state,
        engine_version=engine_version_value,
        scenario_content_sha256=content_hash_value,
        initial_current_scene_id=scene_id,
    )

    resolved_client = _resolve_client(client)
    row = _call_rpc(
        resolved_client,
        "start_or_resume_scenario_attempt_v1",
        {
            "p_user_email": normalized_email,
            "p_scenario_version_id": version_id,
            "p_initial_current_scene_id": scene_id,
            "p_initial_serialized_state": validated_state,
            "p_engine_version": engine_version_value,
            "p_scenario_content_sha256": content_hash_value,
        },
    )
    result = _parse_start_or_resume_row(row)

    if result.scenario_version_id != version_id:
        raise ScenarioPersistenceBackendError(
            "identity_mismatch: start_or_resume_scenario_attempt_v1 returned a different "
            "scenario_version_id than requested"
        )
    if result.engine_version != engine_version_value:
        raise ScenarioPersistenceBackendError(
            "identity_mismatch: start_or_resume_scenario_attempt_v1 returned a different "
            "engine_version than requested"
        )
    if result.scenario_content_sha256 != content_hash_value:
        raise ScenarioPersistenceBackendError(
            "identity_mismatch: start_or_resume_scenario_attempt_v1 returned a different "
            "scenario_content_sha256 than requested"
        )
    return result


def get_attempt(
    client: Any = None,
    *,
    user_email: str,
    attempt_id: str,
) -> ScenarioAttemptSnapshot:
    """Read-only lookup of one attempt owned by `user_email`, including its
    full ordered decision history. Calls `public.get_scenario_attempt_v1`.

    Raises `ScenarioAttemptNotFoundError` both when `attempt_id` does not
    exist at all and when it exists but is owned by a different learner --
    this module never distinguishes the two to a caller, matching the RPC's
    own documented behavior.
    """
    normalized_email = normalize_scenario_persistence_email(user_email)
    attempt_id_value = _require_uuid_str(attempt_id, "attempt_id")

    resolved_client = _resolve_client(client)
    row = _call_rpc(
        resolved_client,
        "get_scenario_attempt_v1",
        {"p_user_email": normalized_email, "p_attempt_id": attempt_id_value},
    )
    result = _parse_attempt_snapshot_row(row)

    if result.attempt_id != attempt_id_value:
        raise ScenarioPersistenceBackendError(
            "identity_mismatch: get_scenario_attempt_v1 returned a different attempt_id than requested"
        )
    return result


def submit_decision(
    client: Any = None,
    *,
    user_email: str,
    attempt_id: str,
    expected_sequence_number: int,
    expected_scene_id: str,
    selected_option_id: str,
    state_before: Mapping[str, Any],
    state_after: Mapping[str, Any],
    is_terminal: bool,
    resulting_scene_id: Optional[str] = None,
    terminal_ending_id: Optional[str] = None,
    terminal_result_snapshot: Optional[Mapping[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    request_fingerprint: Optional[str] = None,
) -> ScenarioDecisionSubmissionResult:
    """Record one decision and, when terminal, atomically complete the
    attempt in the same call. Calls `public.submit_scenario_decision_v1`.

    `state_before`/`state_after` must both already be the exact
    `utils.scenario_engine.serialize_run_snapshot(...)` shape (validated via
    `validate_serialized_engine_state`) -- this function never recomputes
    scoring, resulting scenes, or endings; it only persists what the caller
    (backed by `utils.scenario_engine`) already computed.

    When `idempotency_key` is omitted, a fresh UUIDv4 is generated via
    `generate_idempotency_key()`. `request_fingerprint` is ALWAYS computed
    deterministically via `compute_request_fingerprint(...)` from this call's
    own validated, normalized inputs (SIM-PERSIST-04F) -- when the caller
    also supplies `request_fingerprint` explicitly, it is used only as an
    extra consistency check against that computed value (format-validated,
    then required to match exactly) and is never sent to the RPC as an
    independently-trusted value; a mismatch raises
    `ScenarioPersistenceValidationError` (`request_fingerprint_mismatch:`)
    locally, without calling the RPC at all. A caller that needs to safely
    retry a specific submission (e.g. after a network timeout) should
    generate its own `idempotency_key` up front and pass the identical value
    (and identical other arguments, so the computed fingerprint matches) on
    every retry.
    """
    normalized_email = normalize_scenario_persistence_email(user_email)
    attempt_id_value = _require_uuid_str(attempt_id, "attempt_id")

    sequence_value = _require_strict_int(expected_sequence_number, "sequence_number", minimum=1)

    scene_value = _require_nonempty_str(expected_scene_id, "expected_scene_id")
    option_value = _require_nonempty_str(selected_option_id, "selected_option_id")
    state_before_value = validate_serialized_engine_state(state_before, field="state_before")
    state_after_value = validate_serialized_engine_state(state_after, field="state_after")
    # SIM-PERSIST-04C: require an actual bool -- never bool(is_terminal),
    # which would silently accept a truthy string or integer.
    is_terminal_value = _require_strict_bool(is_terminal, "is_terminal")

    if is_terminal_value:
        if resulting_scene_id is not None:
            raise ScenarioPersistenceValidationError(
                "invalid_resulting_scene_id: resulting_scene_id must be None for a terminal decision"
            )
        resulting_scene_value: Optional[str] = None
        ending_value = _require_nonempty_str(terminal_ending_id, "terminal_ending_id")
        snapshot_value: Optional[Dict[str, Any]] = _require_json_object(
            terminal_result_snapshot, "terminal_result_snapshot"
        )
    else:
        resulting_scene_value = _require_nonempty_str(resulting_scene_id, "resulting_scene_id")
        if terminal_ending_id is not None or terminal_result_snapshot is not None:
            raise ScenarioPersistenceValidationError(
                "invalid_terminal_fields: terminal_ending_id and terminal_result_snapshot must be "
                "None for a non-terminal decision"
            )
        ending_value = None
        snapshot_value = None

    # SIM-PERSIST-04C snapshot IDENTITY/LIFECYCLE integrity boundary --
    # checked locally, before any RPC call, mirroring the RPC's own checks
    # exactly (see `_validate_decision_snapshot_consistency`'s docstring).
    _validate_decision_snapshot_consistency(
        state_before_value,
        state_after_value,
        expected_scene_id=scene_value,
        is_terminal=is_terminal_value,
        resulting_scene_id=resulting_scene_value,
        terminal_ending_id=ending_value,
        terminal_result_snapshot=snapshot_value,
    )

    idempotency_key_value = (
        _require_uuid4_str(idempotency_key, "idempotency_key")
        if idempotency_key is not None
        else generate_idempotency_key()
    )

    # SIM-PERSIST-04F Correction 2: ALWAYS compute the canonical fingerprint
    # from the already-validated, already-normalized request inputs -- never
    # trust a caller-supplied value merely because it happens to already be
    # 64 lowercase hex characters. Previously, an explicitly-supplied
    # request_fingerprint that matched FORMAT but did not actually match
    # THIS request's own content would be sent to the RPC as-is, and (since
    # the RPC's own idempotent-retry check, before this same SIM-PERSIST-04F,
    # compared only request_fingerprint) could have silently masked a
    # genuinely different request as a safe retry of an unrelated one. Now a
    # caller-supplied value is only ever used as an EXTRA consistency check
    # against the value this module itself computes -- it is never sent to
    # the RPC as an independently-trusted input.
    computed_fingerprint = compute_request_fingerprint(
        attempt_id=attempt_id_value,
        expected_sequence_number=sequence_value,
        expected_scene_id=scene_value,
        selected_option_id=option_value,
        state_before=state_before_value,
        state_after=state_after_value,
        resulting_scene_id=resulting_scene_value,
        is_terminal=is_terminal_value,
        terminal_ending_id=ending_value,
        terminal_result_snapshot=snapshot_value,
    )

    if request_fingerprint is not None:
        # SIM-PERSIST-04C: strip only -- never lower() -- so an
        # uppercase-containing supplied fingerprint is rejected rather than
        # silently case-folded into something that would then pass the
        # lowercase-hex format check.
        supplied_fingerprint = str(request_fingerprint).strip()
        if not CONTENT_HASH_PATTERN.fullmatch(supplied_fingerprint):
            raise ScenarioPersistenceValidationError(
                "invalid_request_fingerprint: request_fingerprint must already be exactly 64 lowercase "
                "hexadecimal characters (uppercase is not accepted)"
            )
        if supplied_fingerprint != computed_fingerprint:
            raise ScenarioPersistenceValidationError(
                "request_fingerprint_mismatch: the supplied request_fingerprint does not match the "
                "fingerprint computed from this request's own validated inputs"
            )

    fingerprint_value = computed_fingerprint

    resolved_client = _resolve_client(client)
    row = _call_rpc(
        resolved_client,
        "submit_scenario_decision_v1",
        {
            "p_user_email": normalized_email,
            "p_attempt_id": attempt_id_value,
            "p_idempotency_key": idempotency_key_value,
            "p_expected_sequence_number": sequence_value,
            "p_expected_scene_id": scene_value,
            "p_selected_option_id": option_value,
            "p_request_fingerprint": fingerprint_value,
            "p_state_before": state_before_value,
            "p_state_after": state_after_value,
            "p_is_terminal": is_terminal_value,
            "p_resulting_scene_id": resulting_scene_value,
            "p_terminal_ending_id": ending_value,
            "p_terminal_result_snapshot": snapshot_value,
        },
    )
    result = _parse_submit_decision_row(row)

    if result.attempt_id != attempt_id_value:
        raise ScenarioPersistenceBackendError(
            "identity_mismatch: submit_scenario_decision_v1 returned a different attempt_id than requested"
        )
    return result


def abandon_attempt(
    client: Any = None,
    *,
    user_email: str,
    attempt_id: str,
) -> ScenarioAttemptAbandonResult:
    """Transition one owned, in_progress attempt to abandoned. Idempotent:
    calling this again on an already-abandoned attempt returns its existing
    final state rather than raising. Calls
    `public.abandon_scenario_attempt_v1`.
    """
    normalized_email = normalize_scenario_persistence_email(user_email)
    attempt_id_value = _require_uuid_str(attempt_id, "attempt_id")

    resolved_client = _resolve_client(client)
    row = _call_rpc(
        resolved_client,
        "abandon_scenario_attempt_v1",
        {"p_user_email": normalized_email, "p_attempt_id": attempt_id_value},
    )
    result = _parse_abandon_row(row)

    if result.attempt_id != attempt_id_value:
        raise ScenarioPersistenceBackendError(
            "identity_mismatch: abandon_scenario_attempt_v1 returned a different attempt_id than requested"
        )
    return result
