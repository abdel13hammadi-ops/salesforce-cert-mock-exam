"""Python persistence/serialization adapter for SCENARIO_ENGINE_V2 attempts.

This module is the translation and verification layer between:

- Engine V2 immutable Python runtime objects (``utils/scenario_engine_v2.py``);
- JSON-compatible persisted envelopes (the ``serialized_engine_state`` /
  ``state_before`` / ``state_after`` jsonb columns on the existing, unmodified
  V68 tables);
- canonical ``scenario_decisions`` rows (the ordered, authoritative
  ``(sequenceNumber, sceneId, optionId)`` triples);
- the existing scenario persistence RPC contract, extended (additively, via
  the validated V69 migration) with one optional ``p_attempt_id`` parameter
  on ``start_or_resume_scenario_attempt_v1``.

This module does **not** implement the learner controller, the full
start/resume workflow, decision-submission orchestration, UI integration, or
any database/network I/O. It never instantiates a Supabase client, never
calls an RPC, never reads an environment variable, and never opens a
PostgreSQL connection -- every function here is a pure, deterministic
transformation of already-in-memory Python values.

Authoritative-data boundary (see
``docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md``
and ``docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md`` section 19):

- Authoritative: trusted database identity columns (``attempt.id``,
  ``attempt.engine_version``, ``attempt.scenario_content_sha256``),
  immutable scenario content, and the ordered ``scenario_decisions`` rows.
- **Never authoritative**: the serialized envelope. Every cached field
  inside the envelope (``currentSceneId``, ``expectedSequenceNumber``,
  ``isComplete``, ``state``, ``counters``, ``flags``, ``decisionHistory``,
  ``routingResolutions``, ``optionDisplayOrderByScene``,
  ``selectedVariantIdByScene``, ``terminalResult``) is re-verified against a
  fresh replay on every call to :func:`replay_serialized_run_v2` and is
  never trusted on disagreement -- a mismatch is a fail-closed error, never
  a silent repair. The envelope never carries attempt identity at all (see
  below) -- there is nothing inside it that could ever override the trusted
  attempt row id.

Snapshot envelope contract (``envelopeVersion`` 1)
---------------------------------------------------
The persisted envelope is a plain JSON object with **exactly 17** top-level
keys (see :data:`_ENVELOPE_V1_KEYS`) -- frozen by the independent focused
review (SIM-PERSIST-V2-04-REVIEW-01) against the validated V68/V69 SQL
contract, ``SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md`` section 9,
and ``SCENARIO_SCHEMA_1_1_0_SPEC.md`` section 19.2:

``envelopeVersion``, ``simulationId``, ``version`` (the scenario content's
own version string -- **not** ``scenarioVersion``; SQL's
``p_initial_serialized_state->>'version'``/``state_before->>'version'``
checks are load-bearing and require exactly this key name),
``schemaVersion``, ``engineVersion``, ``canonicalContentSha256``,
``currentSceneId``, ``expectedSequenceNumber``, ``isComplete`` (a JSON
**boolean** -- **not** a ``status`` string; SQL's
``jsonb_typeof(...->'isComplete') = 'boolean'`` checks are load-bearing),
``state``, ``counters``, ``flags``, ``decisionHistory``,
``optionDisplayOrderByScene``, ``selectedVariantIdByScene``,
``routingResolutions``, ``terminalResult``.

**Deliberately excluded** (SIM-PERSIST-V2-04B correction): ``attemptId``
(attempt identity is never duplicated inside the JSONB envelope -- it is
always the trusted database attempt row's own ``id`` column, supplied
separately to every adapter function that needs it, exactly as
``SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md`` section 7 specifies --
"``attemptId`` is deliberately excluded from the envelope... avoids a
second place it could drift") and ``decisionCount`` (not part of the
authoritative contract; always ``len(decisionHistory)`` where a count is
ever needed, never a separately-trusted field).

``decisionHistory`` entries are restricted to **exactly**
``{"sequenceNumber", "sceneId", "optionId"}`` -- every other field on
Engine V2's own ``DebriefTraceEntry`` (evaluation tier, debrief seed, state
delta, flags cleared/set, corrective/routing internals, dialogue variant
ids, competency tags) is intentionally and permanently excluded from this
envelope, on both serialization (mandatory projection) and deserialization
(hard rejection of any of those keys if a corrupted or adversarial payload
attempts to smuggle one back in). The canonical ``scenario_decisions``
database rows remain the sole authoritative source for anything beyond
those three fields.

``routingResolutions`` and ``selectedVariantIdByScene`` are **mandatory**
keys on every envelope (never omitted), always present as ``[]``/``{}``
when there is nothing yet to report -- frozen by the focused review's
"optional-field decision" (simpler, uniform shape; SQL never inspects
either key, so this does not conflict with the SQL contract).

Strict JSON typing
-------------------
Every value this module persists or accepts is a JSON-native type: ``dict``
with string keys, ``list``, ``str``, an exact ``int`` (``type(value) is
int`` -- ``bool`` is explicitly rejected, since Python's ``bool`` is an
``int`` subclass), a finite ``float`` (``NaN``/``Infinity``/``-Infinity``
rejected), an actual ``bool`` only where semantically boolean, or ``None``.
``MappingProxyType``/``frozenset``/``tuple`` (Engine V2's own runtime
container types) are explicitly thawed into plain ``dict``/``list`` on the
way out. On the way in, ``_require_json_object`` deliberately also thaws a
``MappingProxyType`` (rather than rejecting it) so that a caller re-feeding
one of this module's own already-thawed structures back in never fails
spuriously; only a non-``Mapping``, non-``dict``-convertible value is
rejected.

Cache-comparison / floating-point handling
-------------------------------------------
:func:`replay_serialized_run_v2` compares the freshly recomputed envelope
against the persisted one using exact, canonical-JSON-shape equality
(``==`` on already-normalized plain ``dict``/``list``/``str``/``int``/
``float``/``bool``/``None`` values) -- never Python object identity, never
dictionary insertion order (Python ``dict.__eq__`` is already
order-independent), and never a numeric tolerance. Engine V2's own state
arithmetic is deterministic IEEE-754 double-precision arithmetic given
identical inputs (no floating-point-affecting randomness anywhere in the
engine), so an exact comparison is the correct, safest choice -- a
tolerance could silently hide a genuine score/state divergence. Ordered
fields (``decisionHistory``, ``optionDisplayOrderByScene`` values,
``routingResolutions``) are compared positionally; unordered fields
(``state``, ``counters``, ``flags`` -- flags are always emitted as a sorted
list) are compared as plain dict/set-equivalent values.

Domain errors
-------------
See the exception classes declared below. No ``KeyError``/``TypeError``/
``ValueError``/``AttributeError``/``json.JSONDecodeError``/dataclass
conversion error is ever allowed to escape a public function in this
module uncaught -- every one of those is translated into one of this
module's own typed domain errors before being raised. The four
object-shape-sensitive public serializers (:func:`serialize_run_snapshot_v2`,
:func:`serialize_decision_input_v2`, :func:`serialize_learner_scene_view_v2`,
:func:`serialize_learner_terminal_view_v2`) are wrapped with the
:func:`_wrap_serialization_boundary_errors` decorator (SIM-PERSIST-V2-04B
correction), which catches exactly ``AttributeError``/``TypeError``/
``KeyError``/``ValueError``/``IndexError`` raised while reading a malformed
input object's attributes and re-raises
:class:`ScenarioPersistenceV2SerializationError` -- it never catches
``BaseException`` subclasses like ``SystemExit``/``KeyboardInterrupt``/
``GeneratorExit``, and it never catches this module's own
:class:`ScenarioPersistenceV2Error` subclasses (those already-domain errors
pass through unchanged). Engine V2's own exception types
(``ScenarioReplayV2Error``, ``ScenarioRunStateV2Error``,
``ScenarioContentV2Error``) are re-raised unchanged from
:func:`replay_serialized_run_v2` (mirroring the reviewed Slice A contract,
section 8.7) -- they are already focused, already-hardened domain errors in
their own right, not a raw/ambiguous Python builtin.

Engine V1 isolation
--------------------
This module never imports ``utils/scenario_engine.py`` (Engine V1) and
never modifies ``utils/scenario_persistence.py``. It reuses exactly two
already-public, engine-agnostic primitives from that module by direct
import -- :func:`~utils.scenario_persistence.generate_idempotency_key` and
:func:`~utils.scenario_persistence.compute_request_fingerprint` -- exactly
as the SIM-PERSIST-V2-01 design document's own section 16 explicitly
endorses ("Reuses, unchanged, by direct import ... generate_idempotency_key
(), compute_request_fingerprint(...)"). Nothing in ``utils/scenario_persistence.py``
imports this module, so Engine V1's own behavior is completely unaffected.

No database calls
------------------
This module implements adapter construction and parsing only. It never
instantiates a Supabase client, never calls ``client.rpc(...)``, never
reads an environment variable, and never opens a PostgreSQL connection.
"""

from __future__ import annotations

import copy
import functools
import math
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, TypeVar

from utils.scenario_engine_v2 import (
    ENGINE_VERSION,
    LearnerSceneView,
    LearnerTerminalView,
    ScenarioContentV2,
    ScenarioDecisionInputV2,
    ScenarioReplayV2Error,
    ScenarioRunV2Snapshot,
    replay_scenario_run_v2,
    verify_replay_identity_v2,
)
from utils.scenario_persistence import compute_request_fingerprint, generate_idempotency_key

__all__ = (
    "ENVELOPE_VERSION",
    # Errors
    "ScenarioPersistenceV2Error",
    "ScenarioPersistenceV2SerializationError",
    "ScenarioPersistenceV2ValidationError",
    "ScenarioPersistenceV2IdentityError",
    "ScenarioPersistenceV2CacheMismatchError",
    "ScenarioPersistenceV2TerminalMismatchError",
    "ScenarioPersistenceV2RpcResponseError",
    # Typed values
    "TerminalSummaryV2",
    "RoutingResolutionRecordV2",
    "PersistedRunEnvelopeV2",
    "StartOrResumeRpcResultV2",
    "SubmitDecisionRpcResultV2",
    # Public adapter API
    "serialize_run_snapshot_v2",
    "deserialize_run_snapshot_v2",
    "serialize_decision_input_v2",
    "deserialize_decision_input_v2",
    "serialize_learner_scene_view_v2",
    "serialize_learner_terminal_view_v2",
    "replay_serialized_run_v2",
    "verify_persisted_attempt_identity_v2",
    "build_start_or_resume_rpc_params_v2",
    "parse_start_or_resume_rpc_response_v2",
    "build_submit_decision_rpc_params_v2",
    "parse_submit_decision_rpc_response_v2",
)

ENVELOPE_VERSION = 1
_NIL_UUID = "00000000-0000-0000-0000-000000000000"

# Full attempt-level lifecycle, used only for RPC response parsing (the
# RPC's own `status`/`attempt_status` columns -- database-row-level
# concepts the envelope itself never carries -- can legitimately be
# "abandoned" too, unlike the envelope's own boolean `isComplete`).
_ATTEMPT_LIFECYCLE_STATUSES = frozenset({"in_progress", "completed", "abandoned"})

_DECISION_INPUT_KEYS = frozenset({"sequenceNumber", "sceneId", "optionId"})
_ROUTING_RESOLUTION_KEYS = frozenset({"sequenceNumber", "nextSceneId", "enteredCorrective", "skippedCorrective"})
_TERMINAL_RESULT_KEYS = frozenset({"endingId", "displayScore", "engineVersion", "canonicalContentSha256"})

# Frozen, SIM-PERSIST-V2-04B-corrected envelope shape: exactly 17 top-level
# keys, matching the validated V68/V69 SQL contract, Slice A contract
# section 9, and SCENARIO_SCHEMA_1_1_0_SPEC.md section 19.2 exactly.
# Deliberately EXCLUDES `attemptId` (attempt identity is never duplicated
# inside the JSONB envelope -- always the trusted database attempt row's
# own `id` column) and `decisionCount` (not part of the authoritative
# contract; always `len(decisionHistory)`). Uses `version` (NOT
# `scenarioVersion`) and boolean `isComplete` (NOT string `status`) --
# both load-bearing SQL identity/lifecycle key names.
_ENVELOPE_V1_KEYS = frozenset(
    {
        "envelopeVersion",
        "simulationId",
        "version",
        "schemaVersion",
        "engineVersion",
        "canonicalContentSha256",
        "currentSceneId",
        "expectedSequenceNumber",
        "isComplete",
        "state",
        "counters",
        "flags",
        "decisionHistory",
        "optionDisplayOrderByScene",
        "selectedVariantIdByScene",
        "routingResolutions",
        "terminalResult",
    }
)

# Cache/derived fields re-verified (never trusted) against a fresh replay,
# per SCENARIO_SCHEMA_1_1_0_SPEC.md section 19 and the reviewed resume
# design's section 3. Identity fields (simulationId/version/schemaVersion/
# engineVersion/canonicalContentSha256) are verified separately, by
# `verify_persisted_attempt_identity_v2`, before replay happens at all --
# they are not re-checked here as "cache". Attempt identity is never part
# of the envelope at all (trusted attempt_row_id is supplied out-of-band),
# so it never appears in this cache-comparison set either.
_NON_TERMINAL_CACHE_KEYS: Tuple[str, ...] = (
    "currentSceneId",
    "expectedSequenceNumber",
    "isComplete",
    "decisionHistory",
    "optionDisplayOrderByScene",
    "selectedVariantIdByScene",
    "state",
    "counters",
    "flags",
    "routingResolutions",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScenarioPersistenceV2Error(Exception):
    """Base error for the SCENARIO_ENGINE_V2 persistence/serialization adapter."""


class ScenarioPersistenceV2SerializationError(ScenarioPersistenceV2Error):
    """Raised when a value that should already be well-formed (an Engine V2
    runtime object, guaranteed by the engine's own invariants) cannot be
    safely converted to a JSON-native value. Should never fire in practice
    against a genuine Engine V2 snapshot -- this is a fail-closed boundary
    assertion, not a recomputation."""


class ScenarioPersistenceV2ValidationError(ScenarioPersistenceV2Error):
    """Raised for a malformed, untrusted input: an unsupported envelope
    version, a missing/extra/wrongly-typed field, a nonfinite number, an
    invalid UUID, a malformed persisted decision, or any other structural
    defect found while deserializing untrusted JSON."""


class ScenarioPersistenceV2IdentityError(ScenarioPersistenceV2Error):
    """Raised when a persisted attempt's identity (attempt id, content hash,
    engine version, or any of the pinned content-identity fields) does not
    match what is currently trusted (database columns, or a freshly loaded
    content document)."""


class ScenarioPersistenceV2CacheMismatchError(ScenarioPersistenceV2Error):
    """Raised when a persisted envelope's cached, server-computed fields
    (state/counters/flags/routing/option order/current scene/sequence/
    status/decision history) disagree with a fresh, authoritative replay.
    Never silently repaired -- the caller must treat this as corrupted
    history."""


class ScenarioPersistenceV2TerminalMismatchError(ScenarioPersistenceV2Error):
    """Raised specifically when a persisted terminal-result summary
    disagrees with the outcome a fresh replay actually produces. Kept
    distinct from :class:`ScenarioPersistenceV2CacheMismatchError` so
    callers can distinguish "the engine's current state disagrees" from
    "the recorded final outcome disagrees"."""


class ScenarioPersistenceV2RpcResponseError(ScenarioPersistenceV2Error):
    """Raised for a malformed RPC response: not exactly one row, a missing
    or wrongly-typed field, an identity mismatch against the request, an
    incompatible engine version, or an unrecognized lifecycle status."""


# ---------------------------------------------------------------------------
# Serialization-boundary error wrapping (SIM-PERSIST-V2-04B correction)
# ---------------------------------------------------------------------------

_F = TypeVar("_F", bound=Callable[..., Any])


def _wrap_serialization_boundary_errors(func: _F) -> _F:
    """Decorator for the object-shape-sensitive public serializers.

    A caller passing a malformed object (``None``, a plain ``dict``, an
    instance of the wrong dataclass, an object missing an expected
    attribute) must never see a raw, ambiguous builtin exception escape a
    public function in this module. This decorator catches exactly
    ``AttributeError``/``TypeError``/``KeyError``/``ValueError``/
    ``IndexError`` -- the concrete exception types Python itself raises
    when code written against one dataclass's attributes/keys is handed an
    object of the wrong shape -- and re-raises
    :class:`ScenarioPersistenceV2SerializationError`. It never catches this
    module's own :class:`ScenarioPersistenceV2Error` subclasses (a
    deliberate, already-domain validation failure raised *inside* the
    wrapped function passes through completely unchanged), and it never
    catches ``BaseException`` subclasses such as ``SystemExit``/
    ``KeyboardInterrupt``/``GeneratorExit`` (those are not listed, so are
    never intercepted)."""

    @functools.wraps(func)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ScenarioPersistenceV2Error:
            raise
        except (AttributeError, TypeError, KeyError, ValueError, IndexError) as exc:
            raise ScenarioPersistenceV2SerializationError(
                f"malformed_input: {func.__name__} received an object with an unexpected shape "
                f"({type(exc).__name__}: {exc})"
            ) from exc

    return _wrapped  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Local strict-type helpers (deliberately not imported from
# utils.scenario_persistence -- those are private to that module; this
# module owns its own copies to keep the two adapters independently
# maintainable, per the task's "prefer isolation" instruction).
# ---------------------------------------------------------------------------


def _require_strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_{field}: must be an actual int, not bool or {type(value).__name__} ({value!r})"
        )
    return value


def _require_strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_{field}: must be an actual bool, got {type(value).__name__} ({value!r})"
        )
    return value


def _require_finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float):
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_{field}: must be a finite number (bool/non-numeric rejected), got {value!r}"
        )
    if not math.isfinite(value):
        raise ScenarioPersistenceV2ValidationError(f"invalid_{field}: must be finite (NaN/Infinity rejected)")
    return float(value)

def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_{field}: must be an actual str, got {type(value).__name__} ({value!r})"
        )
    if not value or value != value.strip():
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_{field}: must be a non-empty, already-trimmed string, got {value!r}"
        )
    return value


def _require_uuid_str(value: Any, field: str, *, allow_nil: bool = True) -> str:
    text = value if isinstance(value, str) else str(value)
    try:
        parsed = uuid.UUID(text.strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise ScenarioPersistenceV2ValidationError(f"invalid_{field}: must be a valid UUID, got {value!r}") from exc
    canonical = str(parsed)
    if not allow_nil and canonical == _NIL_UUID:
        raise ScenarioPersistenceV2ValidationError(f"invalid_{field}: nil UUID is not permitted")
    return canonical


def _require_json_object(value: Any, field: str) -> Dict[str, Any]:
    if isinstance(value, MappingProxyType) or (isinstance(value, Mapping) and not isinstance(value, dict)):
        value = dict(value)
    if not isinstance(value, dict):
        raise ScenarioPersistenceV2ValidationError(f"invalid_{field}: must be a JSON object, got {type(value).__name__}")
    for key in value.keys():
        if not isinstance(key, str):
            raise ScenarioPersistenceV2ValidationError(f"invalid_{field}: object keys must all be strings")
    return value


def _require_list(value: Any, field: str) -> list:
    if not isinstance(value, list):
        raise ScenarioPersistenceV2ValidationError(f"invalid_{field}: must be a JSON array, got {type(value).__name__}")
    return value


def _deep_thaw(value: Any) -> Any:
    """Recursively convert MappingProxyType/frozenset/tuple/set into plain
    dict/list, leaving already-JSON-native values untouched. Never mutates
    ``value`` -- always returns a new structure."""
    if isinstance(value, Mapping):
        return {str(k): _deep_thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        return [_deep_thaw(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Typed values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalSummaryV2:
    ending_id: str
    display_score: int
    engine_version: str
    canonical_content_sha256: str


@dataclass(frozen=True)
class RoutingResolutionRecordV2:
    sequence_number: int
    next_scene_id: str
    entered_corrective: bool
    skipped_corrective: bool


@dataclass(frozen=True)
class PersistedRunEnvelopeV2:
    """A strictly-typed, validated, but explicitly NON-authoritative view of
    exactly what was persisted. This is never a reconstructed
    ``ScenarioRunV2Snapshot`` (that dataclass requires the loaded
    ``ScenarioContentV2``, which the envelope never carries) -- reconstructing
    an authoritative run is always :func:`replay_serialized_run_v2`'s job.

    Deliberately carries **no attempt identity** (SIM-PERSIST-V2-04B
    correction) -- the envelope itself never contains an ``attemptId``
    field, so there is no ``attempt_id`` attribute here to compare, forge,
    or accidentally trust; every function that needs attempt identity
    (:func:`verify_persisted_attempt_identity_v2`,
    :func:`replay_serialized_run_v2`) takes the trusted database attempt
    row id as an explicit, separate parameter instead. Likewise carries no
    ``decision_count`` -- callers needing a count use
    ``len(decision_history)``."""

    envelope_version: int
    simulation_id: str
    version: str
    schema_version: str
    engine_version: str
    canonical_content_sha256: str
    expected_sequence_number: int
    current_scene_id: Optional[str]
    is_complete: bool
    decision_history: Tuple[ScenarioDecisionInputV2, ...]
    option_display_order_by_scene: Mapping[str, Tuple[str, ...]]
    selected_variant_id_by_scene: Mapping[str, Optional[str]]
    routing_resolutions: Tuple[RoutingResolutionRecordV2, ...]
    state: Mapping[str, float]
    counters: Mapping[str, int]
    flags: frozenset
    terminal_result: Optional[TerminalSummaryV2]


@dataclass(frozen=True)
class StartOrResumeRpcResultV2:
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
class SubmitDecisionRpcResultV2:
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


# ---------------------------------------------------------------------------
# Decision-input serialization (public adapter API items 3-4)
# ---------------------------------------------------------------------------


@_wrap_serialization_boundary_errors
def serialize_decision_input_v2(decision: ScenarioDecisionInputV2) -> Dict[str, Any]:
    """``ScenarioDecisionInputV2`` -> exactly ``{sequenceNumber, sceneId,
    optionId}``. Re-validates strictly on the way out; never trusts that an
    in-memory dataclass instance was constructed correctly. Pure; does not
    mutate ``decision`` (a frozen dataclass). Wrapped by
    :func:`_wrap_serialization_boundary_errors` -- a malformed ``decision``
    object (``None``, missing attributes, wrong type) raises
    :class:`ScenarioPersistenceV2SerializationError`, never a raw
    ``AttributeError``."""
    sequence_number = _require_strict_int(decision.sequence_number, "sequenceNumber")
    if sequence_number < 1:
        raise ScenarioPersistenceV2ValidationError(f"invalid_sequenceNumber: must be >= 1, got {sequence_number}")
    scene_id = _require_nonempty_str(decision.scene_id, "sceneId")
    option_id = _require_nonempty_str(decision.option_id, "optionId")
    return {"sequenceNumber": sequence_number, "sceneId": scene_id, "optionId": option_id}


def deserialize_decision_input_v2(payload: Mapping[str, Any]) -> ScenarioDecisionInputV2:
    """Untrusted ``Mapping`` -> ``ScenarioDecisionInputV2``. Rejects any key
    beyond ``sequenceNumber``/``sceneId``/``optionId`` -- this is the
    structural enforcement point that a client (or a corrupted persisted
    ``decisionHistory`` element) can never smuggle a hidden/derived field
    (``evaluationTier``, ``debriefSeed``, ``stateDelta``, etc.) back in.
    Never mutates ``payload``."""
    if not isinstance(payload, Mapping):
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_decision_input: must be a JSON object, got {type(payload).__name__}"
        )
    extra_keys = set(payload.keys()) - _DECISION_INPUT_KEYS
    if extra_keys:
        raise ScenarioPersistenceV2ValidationError(
            f"unexpected_field: decision input contains disallowed field(s) {sorted(extra_keys)}"
        )
    missing_keys = _DECISION_INPUT_KEYS - set(payload.keys())
    if missing_keys:
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_decision_input: missing required field(s) {sorted(missing_keys)}"
        )
    sequence_number = _require_strict_int(payload["sequenceNumber"], "sequenceNumber")
    if sequence_number < 1:
        raise ScenarioPersistenceV2ValidationError(f"invalid_sequenceNumber: must be >= 1, got {sequence_number}")
    scene_id = _require_nonempty_str(payload["sceneId"], "sceneId")
    option_id = _require_nonempty_str(payload["optionId"], "optionId")
    return ScenarioDecisionInputV2(sequence_number=sequence_number, scene_id=scene_id, option_id=option_id)


# ---------------------------------------------------------------------------
# Snapshot envelope serialization (public adapter API items 1-2)
# ---------------------------------------------------------------------------


def _selected_variant_id_by_scene(run: ScenarioRunV2Snapshot) -> Dict[str, Optional[str]]:
    """Latest (by sequence number) selected dialogue variant id per scene,
    derived from ``run.variant_selections`` -- an optional audit map, never
    used to influence replay."""
    latest: Dict[str, Tuple[int, Optional[str]]] = {}
    for event in run.variant_selections:
        current = latest.get(event.scene_id)
        if current is None or event.sequence_number >= current[0]:
            latest[event.scene_id] = (event.sequence_number, event.selected_variant_id)
    return {scene_id: value for scene_id, (_, value) in latest.items()}


@_wrap_serialization_boundary_errors
def serialize_run_snapshot_v2(run: ScenarioRunV2Snapshot) -> Dict[str, Any]:
    """``ScenarioRunV2Snapshot`` -> the exact, frozen 17-key envelope shape
    (SIM-PERSIST-V2-04B correction; see the module docstring's "Snapshot
    envelope contract" section for the full key list and rationale).

    None of the engine's own invariants are re-validated here (the engine
    already guarantees them) -- this function's job is type conversion
    (``frozenset``/``MappingProxyType``/``tuple`` -> plain ``dict``/``list``)
    plus a defensive, fail-closed finite-number assertion. ``decisionHistory``
    is a MANDATORY field-subset projection of each ``DebriefTraceEntry`` down
    to exactly ``{sequenceNumber, sceneId, optionId}`` -- every one of the
    other twelve ``DebriefTraceEntry`` fields (``evaluationTier``,
    ``debriefSeed``, ``stateDelta``, ``stateAfter``, ``flagsCleared``,
    ``flagsSet``, ``nextSceneId``, ``enteredCorrective``,
    ``skippedCorrective``, ``presentedDialogueVariantId``,
    ``nextDialogueVariantId``, ``competencyTags``) MUST NEVER appear in the
    output, under any key name. The output never contains ``attemptId`` or
    ``decisionCount`` (both removed by SIM-PERSIST-V2-04B; attempt identity
    is carried out-of-band, never inside the envelope). Wrapped by
    :func:`_wrap_serialization_boundary_errors`. Pure; does not mutate
    ``run``.
    """
    content = run.content

    decision_history = [
        {"sequenceNumber": entry.sequence_number, "sceneId": entry.scene_id, "optionId": entry.option_id}
        for entry in run.decisions
    ]

    option_display_order_by_scene: Dict[str, list] = {}
    for scene_id, order in run.option_display_order_by_scene.items():
        option_display_order_by_scene[str(scene_id)] = [str(option_id) for option_id in order]

    routing_resolutions = [
        {
            "sequenceNumber": event.sequence_number,
            "nextSceneId": event.next_scene_id,
            "enteredCorrective": event.entered_corrective,
            "skippedCorrective": event.skipped_corrective,
        }
        for event in run.routing_resolutions
    ]

    state: Dict[str, float] = {}
    for key, value in run.state.items():
        if isinstance(value, bool) or type(value) not in (int, float) or not math.isfinite(value):
            raise ScenarioPersistenceV2SerializationError(
                f"invalid_state.{key}: engine produced a non-finite value ({value!r})"
            )
        state[str(key)] = float(value)

    counters: Dict[str, int] = {}
    for key, value in run.counters.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ScenarioPersistenceV2SerializationError(
                f"invalid_counters.{key}: engine produced a non-int counter value ({value!r})"
            )
        counters[str(key)] = value

    flags = sorted(str(flag) for flag in run.flags)

    if not isinstance(run.is_complete, bool):
        raise ScenarioPersistenceV2SerializationError(
            f"invalid_isComplete: engine produced a non-bool is_complete value ({run.is_complete!r})"
        )

    if run.is_complete:
        if run.terminal_result is None:
            raise ScenarioPersistenceV2SerializationError(
                "invalid_terminal_result: run.is_complete is True but run.terminal_result is None"
            )
        terminal_result: Optional[Dict[str, Any]] = {
            "endingId": run.terminal_result.outcome_id,
            "displayScore": run.terminal_result.display_score,
            "engineVersion": run.terminal_result.engine_version,
            "canonicalContentSha256": run.terminal_result.canonical_content_sha256,
        }
    else:
        terminal_result = None

    envelope: Dict[str, Any] = {
        "envelopeVersion": ENVELOPE_VERSION,
        "simulationId": content.simulation_id,
        "version": content.version,
        "schemaVersion": content.schema_version,
        "engineVersion": ENGINE_VERSION,
        "canonicalContentSha256": content.canonical_content_sha256,
        "currentSceneId": run.current_scene_id,
        "expectedSequenceNumber": run.expected_sequence_number,
        "isComplete": run.is_complete,
        "state": state,
        "counters": counters,
        "flags": flags,
        "decisionHistory": decision_history,
        "optionDisplayOrderByScene": option_display_order_by_scene,
        "selectedVariantIdByScene": _selected_variant_id_by_scene(run),
        "routingResolutions": routing_resolutions,
        "terminalResult": terminal_result,
    }

    # Defensive, fail-closed boundary assertion (should be structurally
    # impossible given the checks above, but never trusted silently): the
    # whole envelope must already be exactly JSON-native.
    _assert_json_native(envelope, "envelope")
    return envelope


def _assert_json_native(value: Any, path: str) -> None:
    if value is None or isinstance(value, str):
        return
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ScenarioPersistenceV2SerializationError(f"invalid_{path}: nonfinite float in serialized output")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ScenarioPersistenceV2SerializationError(f"invalid_{path}: object key must be a string")
            _assert_json_native(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_native(item, f"{path}[{index}]")
        return
    raise ScenarioPersistenceV2SerializationError(
        f"invalid_{path}: not a JSON-native value (got {type(value).__name__})"
    )


def _validate_option_display_order_by_scene(payload: Any) -> Dict[str, Tuple[str, ...]]:
    obj = _require_json_object(payload, "optionDisplayOrderByScene")
    result: Dict[str, Tuple[str, ...]] = {}
    for scene_id, order in obj.items():
        order_list = _require_list(order, f"optionDisplayOrderByScene.{scene_id}")
        option_ids = []
        seen = set()
        for item in order_list:
            option_id = _require_nonempty_str(item, f"optionDisplayOrderByScene.{scene_id}[]")
            if option_id in seen:
                raise ScenarioPersistenceV2ValidationError(
                    f"invalid_optionDisplayOrderByScene: duplicate optionId {option_id!r} in scene {scene_id!r}"
                )
            seen.add(option_id)
            option_ids.append(option_id)
        result[str(scene_id)] = tuple(option_ids)
    return result


def _validate_selected_variant_id_by_scene(payload: Any) -> Dict[str, Optional[str]]:
    obj = _require_json_object(payload, "selectedVariantIdByScene")
    result: Dict[str, Optional[str]] = {}
    for scene_id, value in obj.items():
        if value is not None:
            value = _require_nonempty_str(value, f"selectedVariantIdByScene.{scene_id}")
        result[str(scene_id)] = value
    return result


def _validate_routing_resolutions(payload: Any) -> Tuple[RoutingResolutionRecordV2, ...]:
    items = _require_list(payload, "routingResolutions")
    records = []
    for index, item in enumerate(items):
        obj = _require_json_object(item, f"routingResolutions[{index}]")
        extra = set(obj.keys()) - _ROUTING_RESOLUTION_KEYS
        if extra:
            raise ScenarioPersistenceV2ValidationError(
                f"unexpected_field: routingResolutions[{index}] contains disallowed field(s) {sorted(extra)}"
            )
        missing = _ROUTING_RESOLUTION_KEYS - set(obj.keys())
        if missing:
            raise ScenarioPersistenceV2ValidationError(
                f"invalid_routingResolutions[{index}]: missing field(s) {sorted(missing)}"
            )
        records.append(
            RoutingResolutionRecordV2(
                sequence_number=_require_strict_int(obj["sequenceNumber"], f"routingResolutions[{index}].sequenceNumber"),
                next_scene_id=_require_nonempty_str(obj["nextSceneId"], f"routingResolutions[{index}].nextSceneId"),
                entered_corrective=_require_strict_bool(
                    obj["enteredCorrective"], f"routingResolutions[{index}].enteredCorrective"
                ),
                skipped_corrective=_require_strict_bool(
                    obj["skippedCorrective"], f"routingResolutions[{index}].skippedCorrective"
                ),
            )
        )
    return tuple(records)


def _validate_state(payload: Any) -> Dict[str, float]:
    obj = _require_json_object(payload, "state")
    return {str(key): _require_finite_number(value, f"state.{key}") for key, value in obj.items()}


def _validate_counters(payload: Any) -> Dict[str, int]:
    obj = _require_json_object(payload, "counters")
    return {str(key): _require_strict_int(value, f"counters.{key}") for key, value in obj.items()}


def _validate_flags(payload: Any) -> frozenset:
    items = _require_list(payload, "flags")
    flags = []
    seen = set()
    for item in items:
        flag_id = _require_nonempty_str(item, "flags[]")
        if flag_id in seen:
            raise ScenarioPersistenceV2ValidationError(f"invalid_flags: duplicate flag {flag_id!r}")
        seen.add(flag_id)
        flags.append(flag_id)
    return frozenset(flags)


def _validate_terminal_result(payload: Any) -> TerminalSummaryV2:
    obj = _require_json_object(payload, "terminalResult")
    extra = set(obj.keys()) - _TERMINAL_RESULT_KEYS
    if extra:
        raise ScenarioPersistenceV2ValidationError(
            f"unexpected_field: terminalResult contains disallowed field(s) {sorted(extra)}"
        )
    missing = _TERMINAL_RESULT_KEYS - set(obj.keys())
    if missing:
        raise ScenarioPersistenceV2ValidationError(f"invalid_terminalResult: missing field(s) {sorted(missing)}")
    engine_version = _require_nonempty_str(obj["engineVersion"], "terminalResult.engineVersion")
    if engine_version != ENGINE_VERSION:
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_terminalResult.engineVersion: expected {ENGINE_VERSION!r}, got {engine_version!r}"
        )
    content_hash = _require_nonempty_str(obj["canonicalContentSha256"], "terminalResult.canonicalContentSha256")
    if len(content_hash) != 64 or content_hash != content_hash.lower():
        raise ScenarioPersistenceV2ValidationError(
            "invalid_terminalResult.canonicalContentSha256: must be exactly 64 lowercase hexadecimal characters"
        )
    try:
        int(content_hash, 16)
    except ValueError as exc:
        raise ScenarioPersistenceV2ValidationError(
            "invalid_terminalResult.canonicalContentSha256: must be exactly 64 lowercase hexadecimal characters"
        ) from exc
    return TerminalSummaryV2(
        ending_id=_require_nonempty_str(obj["endingId"], "terminalResult.endingId"),
        display_score=_require_strict_int(obj["displayScore"], "terminalResult.displayScore"),
        engine_version=engine_version,
        canonical_content_sha256=content_hash,
    )


def deserialize_run_snapshot_v2(payload: Mapping[str, Any]) -> PersistedRunEnvelopeV2:
    """Untrusted ``Mapping`` (a JSONB value read back from
    ``serialized_engine_state``/``state_before``/``state_after``) ->
    ``PersistedRunEnvelopeV2``.

    Structural + strict-type validation only -- never a semantic/content
    check (it never loads or compares against actual scenario content; that
    is :func:`verify_persisted_attempt_identity_v2`'s job). Rejects unknown
    top-level keys (including the now-removed ``attemptId``,
    ``decisionCount``, ``scenarioVersion``, and ``status`` -- SIM-PERSIST-
    V2-04B correction: this adapter has never been published or committed,
    so no dual-key backward compatibility is introduced for the pre-
    correction shape), missing required keys, wrong JSON types, nonfinite
    values, extra fields inside ``decisionHistory``/``routingResolutions``
    elements, malformed ``optionDisplayOrderByScene``, duplicate option ids
    in a display order, terminal fields present on an active
    (``isComplete: false``) attempt, and missing terminal fields on a
    completed (``isComplete: true``) attempt. Never mutates ``payload``.
    """
    obj = _require_json_object(payload, "envelope")

    extra_keys = set(obj.keys()) - _ENVELOPE_V1_KEYS
    if extra_keys:
        raise ScenarioPersistenceV2ValidationError(
            f"unexpected_field: envelope contains disallowed field(s) {sorted(extra_keys)}"
        )
    missing_keys = _ENVELOPE_V1_KEYS - set(obj.keys())
    if missing_keys:
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_envelope: missing required field(s) {sorted(missing_keys)}"
        )

    envelope_version = _require_strict_int(obj["envelopeVersion"], "envelopeVersion")
    if envelope_version != ENVELOPE_VERSION:
        raise ScenarioPersistenceV2ValidationError(
            f"unsupported_envelope_version: this adapter only supports envelopeVersion "
            f"{ENVELOPE_VERSION}, got {envelope_version}"
        )

    simulation_id = _require_nonempty_str(obj["simulationId"], "simulationId")
    version = _require_nonempty_str(obj["version"], "version")
    schema_version = _require_nonempty_str(obj["schemaVersion"], "schemaVersion")
    engine_version = _require_nonempty_str(obj["engineVersion"], "engineVersion")
    if engine_version != ENGINE_VERSION:
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_engineVersion: expected {ENGINE_VERSION!r}, got {engine_version!r}"
        )
    content_hash = _require_nonempty_str(obj["canonicalContentSha256"], "canonicalContentSha256").lower()
    if len(content_hash) != 64 or obj["canonicalContentSha256"] != content_hash:
        raise ScenarioPersistenceV2ValidationError(
            "invalid_canonicalContentSha256: must be exactly 64 lowercase hexadecimal characters"
        )
    try:
        int(content_hash, 16)
    except ValueError as exc:
        raise ScenarioPersistenceV2ValidationError(
            "invalid_canonicalContentSha256: must be exactly 64 lowercase hexadecimal characters"
        ) from exc

    expected_sequence_number = _require_strict_int(obj["expectedSequenceNumber"], "expectedSequenceNumber")
    if expected_sequence_number < 1:
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_expectedSequenceNumber: must be >= 1, got {expected_sequence_number}"
        )

    is_complete = _require_strict_bool(obj["isComplete"], "isComplete")

    current_scene_id_raw = obj["currentSceneId"]
    if is_complete:
        if current_scene_id_raw is not None:
            raise ScenarioPersistenceV2ValidationError(
                "invalid_currentSceneId: must be null for a completed (isComplete: true) attempt"
            )
        current_scene_id: Optional[str] = None
    else:
        current_scene_id = _require_nonempty_str(current_scene_id_raw, "currentSceneId")

    decision_history_raw = _require_list(obj["decisionHistory"], "decisionHistory")
    decision_history = tuple(deserialize_decision_input_v2(item) for item in decision_history_raw)

    option_display_order_by_scene = _validate_option_display_order_by_scene(obj["optionDisplayOrderByScene"])
    selected_variant_id_by_scene = _validate_selected_variant_id_by_scene(obj["selectedVariantIdByScene"])
    routing_resolutions = _validate_routing_resolutions(obj["routingResolutions"])
    state = _validate_state(obj["state"])
    counters = _validate_counters(obj["counters"])
    flags = _validate_flags(obj["flags"])

    terminal_result_raw = obj["terminalResult"]
    if is_complete:
        if terminal_result_raw is None:
            raise ScenarioPersistenceV2ValidationError(
                "invalid_terminalResult: must be present for a completed (isComplete: true) attempt"
            )
        terminal_result: Optional[TerminalSummaryV2] = _validate_terminal_result(terminal_result_raw)
        if terminal_result.canonical_content_sha256 != content_hash:
            raise ScenarioPersistenceV2ValidationError(
                "invalid_terminalResult.canonicalContentSha256: does not match the envelope's own "
                "canonicalContentSha256"
            )
    else:
        if terminal_result_raw is not None:
            raise ScenarioPersistenceV2ValidationError(
                "invalid_terminalResult: must be null for an active (isComplete: false) attempt"
            )
        terminal_result = None

    return PersistedRunEnvelopeV2(
        envelope_version=envelope_version,
        simulation_id=simulation_id,
        version=version,
        schema_version=schema_version,
        engine_version=engine_version,
        canonical_content_sha256=content_hash,
        expected_sequence_number=expected_sequence_number,
        current_scene_id=current_scene_id,
        is_complete=is_complete,
        decision_history=decision_history,
        option_display_order_by_scene=MappingProxyType(option_display_order_by_scene),
        selected_variant_id_by_scene=MappingProxyType(selected_variant_id_by_scene),
        routing_resolutions=routing_resolutions,
        state=MappingProxyType(state),
        counters=MappingProxyType(counters),
        flags=flags,
        terminal_result=terminal_result,
    )


def _envelope_to_json_dict(envelope: PersistedRunEnvelopeV2) -> Dict[str, Any]:
    """Reconstruct the exact JSON-shape dict a validated envelope
    represents, in the same shape :func:`serialize_run_snapshot_v2` emits --
    used only for cache-comparison against a freshly recomputed envelope."""
    return {
        "envelopeVersion": envelope.envelope_version,
        "simulationId": envelope.simulation_id,
        "version": envelope.version,
        "schemaVersion": envelope.schema_version,
        "engineVersion": envelope.engine_version,
        "canonicalContentSha256": envelope.canonical_content_sha256,
        "currentSceneId": envelope.current_scene_id,
        "expectedSequenceNumber": envelope.expected_sequence_number,
        "isComplete": envelope.is_complete,
        "state": dict(envelope.state),
        "counters": dict(envelope.counters),
        "flags": sorted(envelope.flags),
        "decisionHistory": [serialize_decision_input_v2(d) for d in envelope.decision_history],
        "optionDisplayOrderByScene": {k: list(v) for k, v in envelope.option_display_order_by_scene.items()},
        "selectedVariantIdByScene": dict(envelope.selected_variant_id_by_scene),
        "routingResolutions": [
            {
                "sequenceNumber": r.sequence_number,
                "nextSceneId": r.next_scene_id,
                "enteredCorrective": r.entered_corrective,
                "skippedCorrective": r.skipped_corrective,
            }
            for r in envelope.routing_resolutions
        ],
        "terminalResult": (
            {
                "endingId": envelope.terminal_result.ending_id,
                "displayScore": envelope.terminal_result.display_score,
                "engineVersion": envelope.terminal_result.engine_version,
                "canonicalContentSha256": envelope.terminal_result.canonical_content_sha256,
            }
            if envelope.terminal_result is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Learner-safe serialization (public adapter API items 5-6)
# ---------------------------------------------------------------------------


@_wrap_serialization_boundary_errors
def serialize_learner_scene_view_v2(view: LearnerSceneView) -> Dict[str, Any]:
    """``LearnerSceneView`` -> plain JSON dict, field-for-field.

    Reads only from ``view`` (never from ``run``/``scene``/``option``
    directly) -- ``LearnerSceneView`` is already the learner-safe exclusion
    boundary (``utils.scenario_engine_v2.build_learner_scene_view``), so
    this function is structurally incapable of leaking a field that
    boundary already excludes (evaluation tier, state, flags, routing,
    formulas, caps, guards, debrief seeds). Wrapped by
    :func:`_wrap_serialization_boundary_errors`."""
    return {
        "sceneId": view.scene_id,
        "title": view.title,
        "setting": view.setting,
        "dialogueExchanges": [_deep_thaw(exchange) for exchange in view.dialogue_exchanges],
        "charactersPresent": list(view.characters_present),
        "learnerPresent": view.learner_present,
        "decisionPrompt": view.decision_prompt,
        "options": [{"id": option.id, "title": option.title, "text": option.text} for option in view.options],
        "progressMetadata": _deep_thaw(view.progress_metadata) if view.progress_metadata is not None else None,
        "accessibility": _deep_thaw(view.accessibility) if view.accessibility is not None else None,
        "mobilePresentation": _deep_thaw(view.mobile_presentation) if view.mobile_presentation is not None else None,
        "expectedSequenceNumber": view.expected_sequence_number,
        "isComplete": view.is_complete,
    }


@_wrap_serialization_boundary_errors
def serialize_learner_terminal_view_v2(view: LearnerTerminalView) -> Dict[str, Any]:
    """``LearnerTerminalView`` -> exactly ``{outcomeId, outcomeTitle,
    narrative, displayScore}`` -- four keys, no more. No evaluation tier, no
    debrief seed, no classification trace, no internal state. Wrapped by
    :func:`_wrap_serialization_boundary_errors`."""
    return {
        "outcomeId": view.outcome_id,
        "outcomeTitle": view.outcome_title,
        "narrative": view.narrative,
        "displayScore": view.display_score,
    }


# ---------------------------------------------------------------------------
# Identity verification (public adapter API item 8)
# ---------------------------------------------------------------------------


def verify_persisted_attempt_identity_v2(
    content: ScenarioContentV2,
    *,
    attempt_row_id: str,
    attempt_row_engine_version: str,
    attempt_row_scenario_content_sha256: str,
    envelope: PersistedRunEnvelopeV2,
) -> None:
    """Fail closed if the persisted attempt's identity no longer matches
    trusted data. Compares against **database columns** for hash/engine
    version (never the envelope's own copies, which are untrusted cache) --
    a stale/corrupted envelope copy can never mask a real drift, because it
    is cross-checked against the trusted columns as an independent step
    below. Returns ``None`` on success; raises on any mismatch. Pure
    assertion function -- no I/O, no mutation.

    ``attempt_row_id`` is the trusted database attempt row's own ``id``
    column, canonicalized and validated here (SIM-PERSIST-V2-04B
    correction). The envelope itself carries **no** attempt identity at
    all -- there is no ``envelope.attempt_id`` to compare against, so a
    forged/tampered envelope is structurally incapable of influencing
    attempt identity in any way. ``attempt_row_id`` is accepted and
    validated explicitly so every attempt-identity-bearing call site in
    this module has a single, uniform, trusted-parameter shape."""
    _require_uuid_str(attempt_row_id, "attempt_row_id")
    trusted_engine_version = _require_nonempty_str(attempt_row_engine_version, "attempt_row_engine_version")
    trusted_content_hash = _require_nonempty_str(
        attempt_row_scenario_content_sha256, "attempt_row_scenario_content_sha256"
    ).lower()

    try:
        verify_replay_identity_v2(
            content,
            pinned_simulation_id=envelope.simulation_id,
            pinned_version=envelope.version,
            pinned_schema_version=envelope.schema_version,
            pinned_canonical_content_sha256=trusted_content_hash,
            pinned_engine_version=trusted_engine_version,
        )
    except ScenarioReplayV2Error as exc:
        raise ScenarioPersistenceV2IdentityError(str(exc)) from exc

    mismatches = []
    if envelope.canonical_content_sha256 != trusted_content_hash:
        mismatches.append("canonicalContentSha256 (envelope copy disagrees with the trusted database column)")
    if envelope.engine_version != trusted_engine_version:
        mismatches.append("engineVersion (envelope copy disagrees with the trusted database column)")
    if mismatches:
        raise ScenarioPersistenceV2IdentityError(
            f"persisted_identity_mismatch: field(s) disagree between the envelope and trusted data: {mismatches}"
        )


# ---------------------------------------------------------------------------
# Replay (public adapter API item 7)
# ---------------------------------------------------------------------------


def replay_serialized_run_v2(
    content: ScenarioContentV2,
    *,
    attempt_row_id: str,
    attempt_row_engine_version: str,
    attempt_row_scenario_content_sha256: str,
    canonical_decision_rows: Sequence[Mapping[str, Any]],
    cached_envelope_payload: Mapping[str, Any],
) -> ScenarioRunV2Snapshot:
    """Deterministically reconstruct the authoritative run and verify it
    against a persisted cache, in the exact required order:

    1. Deserialize + validate the cached envelope's shape.
    2. Validate trusted persisted identity (never the envelope's own
       identity copies) via :func:`verify_persisted_attempt_identity_v2`.
    3. Deserialize the CANONICAL ``scenario_decisions`` rows (never the
       envelope's own ``decisionHistory`` -- that field is a cache, not an
       input to reconstruction).
    4. Replay from immutable content via ``replay_scenario_run_v2`` --
       cached state/flags/counters/routing/option-order/outcome are never
       consulted for reconstruction.
    5. Recompute the full envelope from the freshly-replayed run.
    6. Compare every cache field against the persisted envelope; fail
       closed (:class:`ScenarioPersistenceV2CacheMismatchError`) on any
       disagreement.
    7. Compare the completed outcome against the persisted terminal
       summary separately; fail closed
       (:class:`ScenarioPersistenceV2TerminalMismatchError`) on disagreement.
    8. Return the recomputed, authoritative ``ScenarioRunV2Snapshot``.

    Engine V2's own exceptions (``ScenarioReplayV2Error``,
    ``ScenarioRunStateV2Error``) are re-raised unchanged from the replay
    step -- they are already focused domain errors. Does not mutate
    ``canonical_decision_rows`` or ``cached_envelope_payload``.

    ``attempt_row_id`` is the sole source of attempt identity for the
    replayed run (SIM-PERSIST-V2-04B correction) -- the envelope carries no
    ``attemptId`` field at all, so a forged/tampered
    ``cached_envelope_payload`` is structurally incapable of influencing
    which attempt the reconstructed ``ScenarioRunV2Snapshot`` identifies as.
    """
    envelope = deserialize_run_snapshot_v2(cached_envelope_payload)
    trusted_attempt_id = _require_uuid_str(attempt_row_id, "attempt_row_id")
    verify_persisted_attempt_identity_v2(
        content,
        attempt_row_id=trusted_attempt_id,
        attempt_row_engine_version=attempt_row_engine_version,
        attempt_row_scenario_content_sha256=attempt_row_scenario_content_sha256,
        envelope=envelope,
    )

    canonical_decisions = tuple(deserialize_decision_input_v2(row) for row in canonical_decision_rows)

    recomputed_run = replay_scenario_run_v2(
        content,
        attempt_id=trusted_attempt_id,
        decisions=canonical_decisions,
    )

    recomputed_envelope = serialize_run_snapshot_v2(recomputed_run)
    cached_envelope_dict = _envelope_to_json_dict(envelope)

    mismatched_fields = [
        key for key in _NON_TERMINAL_CACHE_KEYS if cached_envelope_dict[key] != recomputed_envelope[key]
    ]
    if mismatched_fields:
        raise ScenarioPersistenceV2CacheMismatchError(
            f"cache_mismatch: field(s) disagree between the persisted envelope and a fresh replay: "
            f"{mismatched_fields}"
        )

    if cached_envelope_dict["terminalResult"] != recomputed_envelope["terminalResult"]:
        raise ScenarioPersistenceV2TerminalMismatchError(
            "terminal_outcome_mismatch: the persisted terminal result summary disagrees with the "
            "outcome a fresh replay actually produces"
        )

    return recomputed_run


# ---------------------------------------------------------------------------
# RPC response parsing helpers
# ---------------------------------------------------------------------------


def _require_single_row(data: Any, rpc_name: str) -> Dict[str, Any]:
    if isinstance(data, list):
        if len(data) == 0:
            raise ScenarioPersistenceV2RpcResponseError(f"empty_response: RPC {rpc_name!r} returned no row")
        if len(data) > 1:
            raise ScenarioPersistenceV2RpcResponseError(
                f"multi_row_response: RPC {rpc_name!r} returned {len(data)} rows, expected exactly 1"
            )
        row = data[0]
    elif isinstance(data, Mapping):
        row = data
    else:
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} returned an unrecognized response shape "
            f"({type(data).__name__})"
        )
    if not isinstance(row, Mapping):
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} row must be a JSON object, got {type(row).__name__}"
        )
    return dict(row)


def _row_field(row: Mapping[str, Any], key: str, rpc_name: str) -> Any:
    if key not in row:
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} response is missing field {key!r}"
        )
    return row[key]


def _row_uuid_field(row: Mapping[str, Any], key: str, rpc_name: str) -> str:
    value = _row_field(row, key, rpc_name)
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be a valid UUID, got {value!r}"
        ) from exc


def _row_bool_field(row: Mapping[str, Any], key: str, rpc_name: str) -> bool:
    value = _row_field(row, key, rpc_name)
    if not isinstance(value, bool):
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be an actual bool, "
            f"got {type(value).__name__} ({value!r})"
        )
    return value


def _row_int_field(row: Mapping[str, Any], key: str, rpc_name: str) -> int:
    value = _row_field(row, key, rpc_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be an actual int, not bool or "
            f"{type(value).__name__} ({value!r})"
        )
    return value


def _row_nonempty_str_field(row: Mapping[str, Any], key: str, rpc_name: str) -> str:
    value = _row_field(row, key, rpc_name)
    if not isinstance(value, str) or not value.strip():
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be a non-empty string, got {value!r}"
        )
    return value


def _row_json_object_field(row: Mapping[str, Any], key: str, rpc_name: str) -> Dict[str, Any]:
    """Return an independent, fully-owned copy of a JSON-object response
    field. Uses ``copy.deepcopy`` (SIM-PERSIST-V2-04B correction) -- a
    shallow ``dict(value)`` only copies the top-level mapping and leaves
    every nested dict/list aliased to the raw RPC response, so mutating the
    raw response after parsing (or mutating a nested structure the caller
    got back) could silently corrupt or be corrupted by the other side.
    A deep copy severs every such alias."""
    value = _row_field(row, key, rpc_name)
    if not isinstance(value, Mapping):
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be a JSON object, "
            f"got {type(value).__name__} ({value!r})"
        )
    return copy.deepcopy(dict(value))


def _row_nullable_json_object_field(row: Mapping[str, Any], key: str, rpc_name: str) -> Optional[Dict[str, Any]]:
    """Nullable counterpart of :func:`_row_json_object_field`; same
    deep-copy isolation guarantee when non-null."""
    value = _row_field(row, key, rpc_name)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be null or a JSON object, "
            f"got {type(value).__name__} ({value!r})"
        )
    return copy.deepcopy(dict(value))


def _row_lifecycle_status_field(row: Mapping[str, Any], key: str, rpc_name: str) -> str:
    value = _row_field(row, key, rpc_name)
    if not isinstance(value, str) or value not in _ATTEMPT_LIFECYCLE_STATUSES:
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be one of "
            f"{sorted(_ATTEMPT_LIFECYCLE_STATUSES)}, got {value!r}"
        )
    return value


def _row_content_hash_field(row: Mapping[str, Any], key: str, rpc_name: str) -> str:
    value = _row_field(row, key, rpc_name)
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be exactly 64 lowercase "
            f"hexadecimal characters, got {value!r}"
        )
    try:
        int(value, 16)
    except ValueError as exc:
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be exactly 64 lowercase "
            f"hexadecimal characters, got {value!r}"
        ) from exc
    return value


def _optional_str_field(row: Mapping[str, Any], key: str, rpc_name: str) -> Optional[str]:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise ScenarioPersistenceV2RpcResponseError(
            f"malformed_response: RPC {rpc_name!r} field {key!r} must be null or a string, got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Start/resume RPC params + response (public adapter API items 9-10)
# ---------------------------------------------------------------------------


def build_start_or_resume_rpc_params_v2(
    run: ScenarioRunV2Snapshot,
    *,
    user_email: str,
    scenario_version_id: str,
) -> Dict[str, Any]:
    """Build exactly the seven named JSON arguments the validated V69
    ``start_or_resume_scenario_attempt_v1`` RPC expects, for a freshly
    initialized (not-yet-persisted) Engine V2 run.

    ``p_attempt_id`` is the same UUID string already supplied to Engine V2
    initialization (``run.attempt_id``) -- never re-minted here. No hidden
    state is sent outside the serialized envelope; no extra RPC parameters.
    Does not mutate ``run``.
    """
    if run.current_scene_id is None:
        raise ScenarioPersistenceV2ValidationError(
            "invalid_run: cannot build start-or-resume params for a run with no current scene "
            "(the run has already reached terminal completion)"
        )
    normalized_email = str(user_email or "").strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise ScenarioPersistenceV2ValidationError(
            "invalid_user_email: a non-empty, non-whitespace email address is required"
        )
    version_id = _require_uuid_str(scenario_version_id, "scenario_version_id")
    attempt_id = _require_uuid_str(run.attempt_id, "attempt_id", allow_nil=False)
    scene_id = _require_nonempty_str(run.current_scene_id, "initial_current_scene_id")
    envelope = serialize_run_snapshot_v2(run)

    return {
        "p_user_email": normalized_email,
        "p_scenario_version_id": version_id,
        "p_initial_current_scene_id": scene_id,
        "p_initial_serialized_state": envelope,
        "p_engine_version": ENGINE_VERSION,
        "p_scenario_content_sha256": run.content.canonical_content_sha256,
        "p_attempt_id": attempt_id,
    }


def parse_start_or_resume_rpc_response_v2(
    data: Any,
    *,
    expected_attempt_id: Optional[str] = None,
) -> StartOrResumeRpcResultV2:
    """Parse and strictly validate the response of
    ``start_or_resume_scenario_attempt_v1`` (unchanged 15-column return
    shape). Validates exactly one row, every required field/type, that the
    returned ``engine_version`` is ``SCENARIO_ENGINE_V2``, and (if supplied)
    that the returned ``attempt_id`` matches the caller's expectation.
    Never returns a raw Supabase dict to callers."""
    name = "start_or_resume_scenario_attempt_v1"
    row = _require_single_row(data, name)

    attempt_id = _row_uuid_field(row, "attempt_id", name)
    created = _row_bool_field(row, "created", name)
    scenario_id = _row_uuid_field(row, "scenario_id", name)
    scenario_version_id = _row_uuid_field(row, "scenario_version_id", name)
    status = _row_lifecycle_status_field(row, "status", name)
    current_scene_id = _optional_str_field(row, "current_scene_id", name)
    next_sequence_number = _row_int_field(row, "next_sequence_number", name)
    serialized_engine_state = _row_json_object_field(row, "serialized_engine_state", name)
    engine_version = _row_nonempty_str_field(row, "engine_version", name)
    scenario_content_sha256 = _row_content_hash_field(row, "scenario_content_sha256", name)
    started_at = _optional_str_field(row, "started_at", name)
    completed_at = _optional_str_field(row, "completed_at", name)
    abandoned_at = _optional_str_field(row, "abandoned_at", name)
    terminal_ending_id = _optional_str_field(row, "terminal_ending_id", name)
    terminal_result_snapshot = _row_nullable_json_object_field(row, "terminal_result_snapshot", name)

    if engine_version != ENGINE_VERSION:
        raise ScenarioPersistenceV2RpcResponseError(
            f"incompatible_engine_version: RPC {name!r} returned engine_version {engine_version!r}, "
            f"expected {ENGINE_VERSION!r}"
        )
    embedded_engine_version = serialized_engine_state.get("engineVersion")
    if embedded_engine_version is not None and embedded_engine_version != ENGINE_VERSION:
        raise ScenarioPersistenceV2RpcResponseError(
            f"incompatible_engine_version: RPC {name!r} serialized_engine_state.engineVersion "
            f"{embedded_engine_version!r} disagrees with engine_version {engine_version!r}"
        )
    if expected_attempt_id is not None:
        expected = _require_uuid_str(expected_attempt_id, "expected_attempt_id")
        if attempt_id != expected:
            raise ScenarioPersistenceV2RpcResponseError(
                f"identity_mismatch: RPC {name!r} returned attempt_id {attempt_id!r}, expected {expected!r}"
            )

    return StartOrResumeRpcResultV2(
        attempt_id=attempt_id,
        created=created,
        scenario_id=scenario_id,
        scenario_version_id=scenario_version_id,
        status=status,
        current_scene_id=current_scene_id,
        next_sequence_number=next_sequence_number,
        serialized_engine_state=serialized_engine_state,
        engine_version=engine_version,
        scenario_content_sha256=scenario_content_sha256,
        started_at=started_at,
        completed_at=completed_at,
        abandoned_at=abandoned_at,
        terminal_ending_id=terminal_ending_id,
        terminal_result_snapshot=terminal_result_snapshot,
    )


# ---------------------------------------------------------------------------
# Submit-decision RPC params + response (public adapter API items 11-12)
# ---------------------------------------------------------------------------


def build_submit_decision_rpc_params_v2(
    run_before: ScenarioRunV2Snapshot,
    run_after: ScenarioRunV2Snapshot,
    decision: ScenarioDecisionInputV2,
    *,
    user_email: str,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the thirteen named JSON arguments the existing, unmodified
    ``submit_scenario_decision_v1`` RPC expects, reusing that RPC's
    validated contract exactly (SIM-PERSIST-V2-01 design section 16).

    Every field is derived from the two server-computed run snapshots and
    the already-applied decision -- the caller can never inject a
    client-supplied tier, routing, state, flags, score, or outcome; there is
    simply no parameter for any of those. Does not mutate ``run_before``/
    ``run_after``/``decision``.
    """
    if run_before.attempt_id != run_after.attempt_id:
        raise ScenarioPersistenceV2ValidationError(
            "invalid_run: run_before and run_after must share the same attempt_id"
        )
    normalized_email = str(user_email or "").strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise ScenarioPersistenceV2ValidationError(
            "invalid_user_email: a non-empty, non-whitespace email address is required"
        )
    attempt_id = _require_uuid_str(run_before.attempt_id, "attempt_id", allow_nil=False)

    decision_payload = serialize_decision_input_v2(decision)
    expected_sequence_number = decision_payload["sequenceNumber"]
    expected_scene_id = decision_payload["sceneId"]
    selected_option_id = decision_payload["optionId"]

    state_before = serialize_run_snapshot_v2(run_before)
    state_after = serialize_run_snapshot_v2(run_after)

    is_terminal = run_after.is_complete
    if is_terminal:
        if run_after.terminal_result is None:
            raise ScenarioPersistenceV2ValidationError(
                "invalid_run_after: is_complete is True but terminal_result is None"
            )
        resulting_scene_id: Optional[str] = None
        terminal_ending_id: Optional[str] = run_after.terminal_result.outcome_id
        terminal_result_snapshot: Optional[Dict[str, Any]] = state_after["terminalResult"]
    else:
        resulting_scene_id = run_after.current_scene_id
        terminal_ending_id = None
        terminal_result_snapshot = None

    idempotency_key_value = (
        _require_uuid_str(idempotency_key, "idempotency_key")
        if idempotency_key is not None
        else generate_idempotency_key()
    )
    try:
        parsed_idempotency = uuid.UUID(idempotency_key_value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_idempotency_key: must be a valid UUID, got {idempotency_key_value!r}"
        ) from exc
    if parsed_idempotency.version != 4:
        raise ScenarioPersistenceV2ValidationError(
            f"invalid_idempotency_key: must be a version-4 UUID, got version {parsed_idempotency.version}"
        )

    request_fingerprint = compute_request_fingerprint(
        attempt_id=attempt_id,
        expected_sequence_number=expected_sequence_number,
        expected_scene_id=expected_scene_id,
        selected_option_id=selected_option_id,
        state_before=state_before,
        state_after=state_after,
        resulting_scene_id=resulting_scene_id,
        is_terminal=is_terminal,
        terminal_ending_id=terminal_ending_id,
        terminal_result_snapshot=terminal_result_snapshot,
    )

    return {
        "p_user_email": normalized_email,
        "p_attempt_id": attempt_id,
        "p_idempotency_key": idempotency_key_value,
        "p_expected_sequence_number": expected_sequence_number,
        "p_expected_scene_id": expected_scene_id,
        "p_selected_option_id": selected_option_id,
        "p_request_fingerprint": request_fingerprint,
        "p_state_before": state_before,
        "p_state_after": state_after,
        "p_is_terminal": is_terminal,
        "p_resulting_scene_id": resulting_scene_id,
        "p_terminal_ending_id": terminal_ending_id,
        "p_terminal_result_snapshot": terminal_result_snapshot,
    }


def parse_submit_decision_rpc_response_v2(
    data: Any,
    *,
    expected_attempt_id: Optional[str] = None,
) -> SubmitDecisionRpcResultV2:
    """Parse and strictly validate the response of
    ``submit_scenario_decision_v1`` (unchanged, existing RPC). Validates
    exactly one row, every required field/type, that the response's
    embedded ``serialized_engine_state.engineVersion`` is
    ``SCENARIO_ENGINE_V2``, and (if supplied) that the returned
    ``attempt_id`` matches the caller's expectation. Never returns a raw
    Supabase dict to callers."""
    name = "submit_scenario_decision_v1"
    row = _require_single_row(data, name)

    decision_id = _row_uuid_field(row, "decision_id", name)
    attempt_id = _row_uuid_field(row, "attempt_id", name)
    sequence_number = _row_int_field(row, "sequence_number", name)
    idempotent_replay = _row_bool_field(row, "idempotent_replay", name)
    attempt_status = _row_lifecycle_status_field(row, "attempt_status", name)
    current_scene_id = _optional_str_field(row, "current_scene_id", name)
    next_sequence_number = _row_int_field(row, "next_sequence_number", name)
    serialized_engine_state = _row_json_object_field(row, "serialized_engine_state", name)
    completed_at = _optional_str_field(row, "completed_at", name)
    terminal_ending_id = _optional_str_field(row, "terminal_ending_id", name)
    terminal_result_snapshot = _row_nullable_json_object_field(row, "terminal_result_snapshot", name)

    embedded_engine_version = serialized_engine_state.get("engineVersion")
    if embedded_engine_version != ENGINE_VERSION:
        raise ScenarioPersistenceV2RpcResponseError(
            f"incompatible_engine_version: RPC {name!r} serialized_engine_state.engineVersion "
            f"{embedded_engine_version!r}, expected {ENGINE_VERSION!r}"
        )
    if expected_attempt_id is not None:
        expected = _require_uuid_str(expected_attempt_id, "expected_attempt_id")
        if attempt_id != expected:
            raise ScenarioPersistenceV2RpcResponseError(
                f"identity_mismatch: RPC {name!r} returned attempt_id {attempt_id!r}, expected {expected!r}"
            )

    return SubmitDecisionRpcResultV2(
        decision_id=decision_id,
        attempt_id=attempt_id,
        sequence_number=sequence_number,
        idempotent_replay=idempotent_replay,
        attempt_status=attempt_status,
        current_scene_id=current_scene_id,
        next_sequence_number=next_sequence_number,
        serialized_engine_state=serialized_engine_state,
        completed_at=completed_at,
        terminal_ending_id=terminal_ending_id,
        terminal_result_snapshot=terminal_result_snapshot,
    )
