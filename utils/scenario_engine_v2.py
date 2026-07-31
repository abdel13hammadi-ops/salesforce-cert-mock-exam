"""Deterministic SCENARIO_ENGINE_V2 runtime for validated schema 1.1.0 content.

This module is fully additive and independent of ``utils/scenario_engine.py``
(SCENARIO_ENGINE_V1). There is no shared execution code path between the two
engines: :func:`build_scenario_content_v2` refuses any document that does not
declare ``schemaVersion: "1.1.0"`` and ``requiredEngineVersion:
"SCENARIO_ENGINE_V2"``, and schema 1.0.0 content is never accepted here. This
refusal is itself the version-isolation boundary — callers select an engine
module based on the document's declared ``schemaVersion`` and never route a
1.1.0 document through Engine V1 (``utils/scenario_engine.py``) or vice
versa. ``utils/scenario_engine.py`` is not imported or modified by this
module.

Design summary
---------------
* Pure deterministic core: every function here is a pure function of its
  arguments. No file I/O, no network calls, no wall-clock time, no
  environment variables, no ``random`` module. The only "randomness" is the
  fully deterministic SHA-256-derived option display order (see
  :func:`deterministic_option_display_order`).
* Immutable content: content is deep-frozen once at
  :func:`build_scenario_content_v2` time (``MappingProxyType``/``tuple``
  recursively) and never mutated again. All runtime state is carried in
  frozen dataclasses; every transition returns a *new* snapshot.
* Runtime state contract: :class:`ScenarioRunV2Snapshot` carries exactly the
  fields enumerated in the SIM-ENGINE-V2-01 task (simulation/content/schema
  identity, canonical hash, current scene, expected sequence, state, flags,
  counters, corrective/skip/routing/variant/order history, terminal state).
* Learner-safe output: :func:`build_learner_scene_view` /
  :func:`build_learner_terminal_view` expose only presentation-safe fields.
  Hidden scoring/routing data (evaluation tiers, state deltas, flag
  conditions, caps, formula weights, debrief seeds) never appear in a
  learner view and are only reachable through the internal
  :class:`ScenarioRunV2Snapshot` / :func:`build_debrief_trace` (which itself
  refuses to run on an incomplete attempt).

This module reuses validated primitives from ``utils.scenario_schema`` /
``utils.scenario_validation_v1_1`` (schema + custom + publication
validation, and the noncircular canonical-hash function) rather than
duplicating that validation logic. Runtime execution primitives (condition
evaluation, routing resolution, counter increments, dialogue variant
selection, option display order, formula evaluation, and the seven-step
outcome classifier) are implemented fresh in this module against the
project's own frozen runtime dataclasses, because the validator's equivalent
internal helpers are private, are shaped for the validator's bounded
*reachability search* rather than single-attempt execution, and (as
discovered during this task — see
``docs/scenario_simulator/SCENARIO_ENGINE_V2_SLICE_01_IMPLEMENTATION_REPORT.md``)
conflate two independent counter-increment events in a way that is safe for
reachability search but would be incorrect for exact runtime counter
semantics.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from utils.scenario_schema import SCHEMA_VERSION_1_1, TERMINAL_SENTINEL
from utils.scenario_validation_findings import first_blocking_finding
from utils.scenario_validation_v1_1 import (
    SUPPORTED_ENGINE_VERSIONS_V1_1,
    compute_canonical_content_sha256_v1_1,
    validate_v1_1_scenario_document,
)

__all__ = (
    "ENGINE_VERSION",
    "SCHEMA_VERSION",
    "TERMINAL_SENTINEL",
    # Errors
    "ScenarioEngineV2Error",
    "ScenarioContentV2Error",
    "ScenarioRunStateV2Error",
    "ScenarioReplayV2Error",
    "ScenarioClassificationV2Error",
    # Content
    "ScenarioContentV2",
    "build_scenario_content_v2",
    "load_scenario_content_v2",
    # Condition grammar
    "evaluate_condition",
    # Routing
    "RoutingOutcome",
    "resolve_routing",
    # Dialogue variants
    "ResolvedDialogue",
    "select_dialogue_variant",
    # Option display order
    "deterministic_option_display_order",
    "resolve_option_display_order",
    "SUPPORTED_OPTION_DISPLAY_POLICIES",
    # Formula evaluation
    "compute_positive_health",
    "compute_decision_quality",
    "compute_composite",
    # Outcome classification
    "ClassificationTrace",
    "classify_outcome",
    "round_half_away_from_zero",
    # Runtime state
    "ScenarioDecisionInputV2",
    "DebriefTraceEntry",
    "RoutingResolutionEvent",
    "CorrectiveEntryEvent",
    "SkippedCorrectiveEvent",
    "VariantSelectionEvent",
    "ScenarioTerminalResultV2",
    "ScenarioRunV2Snapshot",
    # Pipeline
    "start_scenario_run_v2",
    "apply_decision_v2",
    "replay_scenario_run_v2",
    "verify_replay_identity_v2",
    "build_debrief_trace",
    # Learner-safe views
    "LearnerOptionView",
    "LearnerSceneView",
    "LearnerTerminalView",
    "build_learner_scene_view",
    "build_learner_terminal_view",
)

ENGINE_VERSION = "SCENARIO_ENGINE_V2"
SCHEMA_VERSION = SCHEMA_VERSION_1_1

_TIER_ORDER = ("optimal", "acceptable", "suboptimal", "high-risk")
_MAX_CONDITION_DEPTH = 8
SUPPORTED_OPTION_DISPLAY_POLICIES = frozenset({"randomize_per_attempt_scene", "authored_order"})


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------


class ScenarioEngineV2Error(Exception):
    """Base error for SCENARIO_ENGINE_V2 content loading and execution."""

    def __init__(self, message: str, *, path: str = "") -> None:
        self.path = path
        super().__init__(message if not path else f"{path}: {message}")


class ScenarioContentV2Error(ScenarioEngineV2Error):
    """Raised for unsupported schema/engine, or invalid/impossible runtime content.

    Covers: wrong schemaVersion/requiredEngineVersion, content that fails
    layered validation, and any runtime reference to an undeclared flag,
    state variable, counter, scene, or outcome (defensive fail-closed checks
    that assume validation already rejected these, but never trust that
    assumption at runtime).
    """


class ScenarioRunStateV2Error(ScenarioEngineV2Error):
    """Raised when a decision cannot be applied to the current run state.

    Covers: stale/future sequence numbers, scene mismatches, unknown or
    foreign option ids, and submissions against an already-terminal run.
    Raising this error never mutates the supplied snapshot — every runtime
    dataclass in this module is frozen, and this module never assigns
    through them.
    """


class ScenarioReplayV2Error(ScenarioEngineV2Error):
    """Raised when a replay's pinned identity or decision history is invalid.

    Covers: simulationId/version/schemaVersion/canonicalContentSha256/
    engineVersion mismatches, and decisions supplied after a replayed run
    already reached terminal completion.
    """


class ScenarioClassificationV2Error(ScenarioEngineV2Error):
    """Raised when the outcome classifier cannot deterministically resolve.

    Covers: zero scored decisions, no matching score band, and a
    guard-disqualified outcome with no lower-ranked fallback available.
    """


# ---------------------------------------------------------------------------
# Small internal helpers (content access is always defensive / fail-closed)
# ---------------------------------------------------------------------------


def _deep_freeze(value: Any) -> Any:
    """Recursively convert dict/list into MappingProxyType/tuple.

    Used exactly once, at :func:`build_scenario_content_v2` time, to produce
    an independently-owned, structurally immutable copy of the validated
    document. The caller's original ``document`` argument is never mutated
    and is not aliased by the frozen copy (nested containers are rebuilt,
    not wrapped).
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _as_seq(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _non_empty(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _is_finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _require_finite_number(value: Any, *, path: str) -> float:
    """Reject bool/NaN/Inf and non-numeric values with a domain error."""
    if not _is_finite_number(value):
        raise ScenarioContentV2Error(
            f"value must be a finite number (bool/NaN/Infinity/non-numeric rejected); got {value!r}",
            path=path,
        )
    return float(value)


def _require_strict_int(value: Any, *, path: str) -> int:
    """Accept only ``type(value) is int`` (rejects bool, float, str, None)."""
    if type(value) is not int:
        raise ScenarioRunStateV2Error(
            f"must be a strict integer (bool/float/str/null rejected); got {type(value).__name__}",
            path=path,
        )
    return value


def _require_non_empty_str(value: Any, *, path: str) -> str:
    if type(value) is not str or not value.strip():
        raise ScenarioRunStateV2Error(
            f"must be a non-empty string; got {value!r}",
            path=path,
        )
    return value


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioContentV2:
    """Immutable, validated schema 1.1.0 content plus precomputed lookups.

    ``document`` is the deep-frozen, validated source document. All other
    fields are lookup indices computed once at construction time so that
    hot-path engine functions never need to re-scan the full document.
    """

    document: Mapping[str, Any]
    simulation_id: str
    version: str
    schema_version: str
    required_engine_version: str
    canonical_content_sha256: str
    start_scene: str
    scenes_by_id: Mapping[str, Mapping[str, Any]]
    flags_spec: Mapping[str, Mapping[str, Any]]
    counters_spec: Mapping[str, Mapping[str, Any]]
    state_bounds: Mapping[str, tuple[float | None, float | None]]
    outcome_ranks: Mapping[str, int]
    initial_state: Mapping[str, float]
    initial_flags: frozenset[str]
    initial_counters: Mapping[str, int]
    corrective_budget_policy: Mapping[str, Any]
    option_display_policy: str
    source_path: Path | None = None


def _scenes_by_id(document: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType({str(scene["id"]): scene for scene in _as_seq(document.get("scenes"))})


def _flags_spec(document: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType(
        {str(flag["flagId"]): flag for flag in _as_seq(document.get("flags")) if _non_empty(flag.get("flagId"))}
    )


def _counters_spec(document: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType(
        {
            str(counter["counterId"]): counter
            for counter in _as_seq(document.get("runtimeCounters"))
            if _non_empty(counter.get("counterId"))
        }
    )


def _state_bounds(document: Mapping[str, Any]) -> Mapping[str, tuple[float | None, float | None]]:
    bounds: dict[str, tuple[float | None, float | None]] = {}
    for variable in _as_seq(document.get("stateVariables")):
        key = _non_empty(variable.get("key"))
        if not key:
            continue
        minimum = variable.get("minimum")
        maximum = variable.get("maximum")
        bounds[key] = (
            float(minimum) if _is_finite_number(minimum) else None,
            float(maximum) if _is_finite_number(maximum) else None,
        )
    return MappingProxyType(bounds)


def _outcome_ranks(document: Mapping[str, Any]) -> Mapping[str, int]:
    ranks: dict[str, int] = {}
    for outcome in _as_seq(document.get("outcomes")):
        outcome_id = _non_empty(outcome.get("outcomeId"))
        rank = outcome.get("classificationRank")
        if outcome_id and isinstance(rank, int) and not isinstance(rank, bool):
            ranks[outcome_id] = rank
    return MappingProxyType(ranks)


def _initial_state(document: Mapping[str, Any]) -> Mapping[str, float]:
    result: dict[str, float] = {}
    for key, value in (document.get("initialState") or {}).items():
        result[str(key)] = _require_finite_number(value, path=f"initialState.{key}")
    return MappingProxyType(result)


def _initial_flags(document: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        str(flag["flagId"])
        for flag in _as_seq(document.get("flags"))
        if _non_empty(flag.get("flagId")) and bool(flag.get("initialValue"))
    )


def _initial_counters(document: Mapping[str, Any]) -> Mapping[str, int]:
    counters: dict[str, int] = {}
    for counter in _as_seq(document.get("runtimeCounters")):
        counter_id = _non_empty(counter.get("counterId"))
        if not counter_id:
            continue
        initial_value = counter.get("initialValue", 0)
        finite = _require_finite_number(
            initial_value if initial_value is not None else 0,
            path=f"runtimeCounters.{counter_id}.initialValue",
        )
        if finite != int(finite):
            raise ScenarioContentV2Error(
                f"counter initialValue must be an integer-valued finite number; got {initial_value!r}",
                path=f"runtimeCounters.{counter_id}.initialValue",
            )
        counters[counter_id] = int(finite)
    return MappingProxyType(counters)


def build_scenario_content_v2(
    document: Mapping[str, Any],
    *,
    source_path: Path | None = None,
) -> ScenarioContentV2:
    """Validate and freeze a schema 1.1.0 document for SCENARIO_ENGINE_V2.

    This is the version-isolation boundary for Engine V2: any document that
    does not declare ``schemaVersion: "1.1.0"`` and an exactly-matching
    ``requiredEngineVersion`` is rejected here, before any engine state is
    constructed. Full layered validation (JSON Schema + structural +
    semantic + graph) is performed via
    ``utils.scenario_validation_v1_1.validate_v1_1_scenario_document`` — this
    module never re-implements or weakens that validation.
    """
    if not isinstance(document, Mapping):
        raise ScenarioContentV2Error("scenario document root must be a JSON object")

    declared_schema = _non_empty(document.get("schemaVersion"))
    if declared_schema != SCHEMA_VERSION:
        raise ScenarioContentV2Error(
            f"SCENARIO_ENGINE_V2 requires schemaVersion {SCHEMA_VERSION!r}; "
            f"got {declared_schema!r}",
            path="schemaVersion",
        )

    declared_engine = _non_empty(document.get("requiredEngineVersion"))
    if declared_engine not in SUPPORTED_ENGINE_VERSIONS_V1_1 or declared_engine != ENGINE_VERSION:
        raise ScenarioContentV2Error(
            f"SCENARIO_ENGINE_V2 requires requiredEngineVersion {ENGINE_VERSION!r}; "
            f"content declares {declared_engine!r}",
            path="requiredEngineVersion",
        )

    findings = validate_v1_1_scenario_document(document)
    blocking = first_blocking_finding(findings)
    if blocking is not None:
        raise ScenarioContentV2Error(
            f"[{blocking.rule_id}] {blocking.message}", path=blocking.path
        )

    start_scene = _non_empty(document.get("startScene"))
    frozen_document = _deep_freeze(document)

    option_display_policy = _non_empty(document.get("optionDisplayPolicy")) or "randomize_per_attempt_scene"
    if option_display_policy not in SUPPORTED_OPTION_DISPLAY_POLICIES:
        raise ScenarioContentV2Error(
            f"unsupported optionDisplayPolicy {option_display_policy!r}; "
            f"supported: {sorted(SUPPORTED_OPTION_DISPLAY_POLICIES)}",
            path="optionDisplayPolicy",
        )

    return ScenarioContentV2(
        document=frozen_document,
        simulation_id=_non_empty(document.get("simulationId")),
        version=_non_empty(document.get("version")),
        schema_version=declared_schema,
        required_engine_version=declared_engine,
        canonical_content_sha256=compute_canonical_content_sha256_v1_1(document),
        start_scene=start_scene,
        scenes_by_id=_scenes_by_id(frozen_document),
        flags_spec=_flags_spec(frozen_document),
        counters_spec=_counters_spec(frozen_document),
        state_bounds=_state_bounds(frozen_document),
        outcome_ranks=_outcome_ranks(frozen_document),
        initial_state=_initial_state(frozen_document),
        initial_flags=_initial_flags(frozen_document),
        initial_counters=_initial_counters(frozen_document),
        corrective_budget_policy=frozen_document.get("correctiveBudgetPolicy") or MappingProxyType({}),
        option_display_policy=option_display_policy,
        source_path=source_path.resolve() if source_path else None,
    )


def load_scenario_content_v2(path: Path) -> ScenarioContentV2:
    import json

    resolved = path.resolve()
    document = json.loads(resolved.read_text(encoding="utf-8"))
    return build_scenario_content_v2(document, source_path=resolved)


# ---------------------------------------------------------------------------
# Condition grammar: all / any / not / flagSet / flagNotSet / stateCompare /
# counterCompare
# ---------------------------------------------------------------------------


def _compare(actual: float, op: str, expected: float) -> bool:
    if op == "gte":
        return actual >= expected
    if op == "lte":
        return actual <= expected
    if op == "gt":
        return actual > expected
    if op == "lt":
        return actual < expected
    if op == "eq":
        return actual == expected
    raise ScenarioContentV2Error(f"unsupported condition operator {op!r}")


def evaluate_condition(
    condition: Mapping[str, Any],
    *,
    content: ScenarioContentV2,
    flags: frozenset[str],
    state: Mapping[str, float],
    counters: Mapping[str, int],
    depth: int = 1,
) -> bool:
    """Evaluate the bounded condition grammar (spec section 9.3).

    Only ``all`` / ``any`` / ``not`` / ``flagSet`` / ``flagNotSet`` /
    ``stateCompare`` / ``counterCompare`` are recognized. No arbitrary
    expressions or executable strings are possible: every branch below
    matches a fixed key and rejects anything else. References to
    undeclared flags/variables/counters, and nesting beyond the validated
    depth bound, fail closed with :class:`ScenarioContentV2Error` rather
    than silently defaulting — content is expected to already be blocked
    from reaching runtime in this shape, so this is a defensive backstop,
    not the primary enforcement point.
    """
    if depth > _MAX_CONDITION_DEPTH:
        raise ScenarioContentV2Error("condition nesting exceeds the validated maximum depth (8)")
    if not isinstance(condition, Mapping):
        raise ScenarioContentV2Error("condition node must be a JSON object")

    if "all" in condition:
        children = _as_seq(condition["all"])
        if not children:
            raise ScenarioContentV2Error("'all' condition must be a non-empty array")
        return all(
            evaluate_condition(child, content=content, flags=flags, state=state, counters=counters, depth=depth + 1)
            for child in children
        )
    if "any" in condition:
        children = _as_seq(condition["any"])
        if not children:
            raise ScenarioContentV2Error("'any' condition must be a non-empty array")
        return any(
            evaluate_condition(child, content=content, flags=flags, state=state, counters=counters, depth=depth + 1)
            for child in children
        )
    if "not" in condition:
        return not evaluate_condition(
            condition["not"], content=content, flags=flags, state=state, counters=counters, depth=depth + 1
        )
    if "flagSet" in condition:
        flag_id = _non_empty(condition["flagSet"])
        if flag_id not in content.flags_spec:
            raise ScenarioContentV2Error(f"flagSet references undeclared flag {flag_id!r}")
        return flag_id in flags
    if "flagNotSet" in condition:
        flag_id = _non_empty(condition["flagNotSet"])
        if flag_id not in content.flags_spec:
            raise ScenarioContentV2Error(f"flagNotSet references undeclared flag {flag_id!r}")
        return flag_id not in flags
    if "stateCompare" in condition:
        spec = condition["stateCompare"] or {}
        variable_id = _non_empty(spec.get("variableId"))
        if variable_id not in content.state_bounds:
            raise ScenarioContentV2Error(f"stateCompare references undeclared state variable {variable_id!r}")
        op = _non_empty(spec.get("op"))
        value = spec.get("value")
        if not _is_finite_number(value):
            raise ScenarioContentV2Error("stateCompare.value must be a finite number")
        return _compare(float(state.get(variable_id, 0.0)), op, float(value))
    if "counterCompare" in condition:
        spec = condition["counterCompare"] or {}
        counter_id = _non_empty(spec.get("counterId"))
        if counter_id not in content.counters_spec:
            raise ScenarioContentV2Error(f"counterCompare references undeclared counter {counter_id!r}")
        op = _non_empty(spec.get("op"))
        value = spec.get("value")
        if not _is_finite_number(value):
            raise ScenarioContentV2Error("counterCompare.value must be a finite number")
        return _compare(float(counters.get(counter_id, 0)), op, float(value))

    raise ScenarioContentV2Error(f"unrecognized condition node keys: {sorted(condition.keys())}")


# ---------------------------------------------------------------------------
# State / flag / counter mutation
# ---------------------------------------------------------------------------


def _clamp_state_value(content: ScenarioContentV2, key: str, value: float) -> float:
    value = _require_finite_number(value, path=f"state.{key}")
    minimum, maximum = content.state_bounds.get(key, (None, None))
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _apply_state_deltas(
    content: ScenarioContentV2,
    state: Mapping[str, float],
    deltas: Mapping[str, float],
) -> dict[str, float]:
    updated = dict(state)
    for key, delta in deltas.items():
        if key not in content.state_bounds:
            raise ScenarioContentV2Error(f"stateChanges key {key!r} is not declared in stateVariables")
        current = _require_finite_number(updated.get(key, 0.0), path=f"state.{key}")
        delta_value = _require_finite_number(delta, path=f"stateChanges.{key}")
        updated[key] = _clamp_state_value(content, key, current + delta_value)
    return updated


def _apply_flag_changes(
    content: ScenarioContentV2,
    flags: frozenset[str],
    *,
    clear: Sequence[str],
    set_: Sequence[str],
) -> frozenset[str]:
    """Clear-before-set flag mutation (spec section 12.2 / 22)."""
    for flag_id in clear:
        if flag_id not in content.flags_spec:
            raise ScenarioContentV2Error(f"clearFlags references undeclared flag {flag_id!r}")
    for flag_id in set_:
        if flag_id not in content.flags_spec:
            raise ScenarioContentV2Error(f"setFlags references undeclared flag {flag_id!r}")
    updated = set(flags)
    for flag_id in clear:
        updated.discard(flag_id)
    for flag_id in set_:
        updated.add(flag_id)
    return frozenset(updated)


def _apply_environmental_entry_flags(
    content: ScenarioContentV2, scene: Mapping[str, Any], flags: frozenset[str]
) -> frozenset[str]:
    entry_flags = tuple(str(f) for f in _as_seq(scene.get("environmentalFlagsOnEntry")))
    if not entry_flags:
        return flags
    return _apply_flag_changes(content, flags, clear=(), set_=entry_flags)


def _clamp_counter_value(content: ScenarioContentV2, counter_id: str, value: int) -> int:
    spec = content.counters_spec.get(counter_id)
    if spec is None:
        return value
    minimum = spec.get("minimum")
    maximum = spec.get("maximum")
    if _is_finite_number(minimum):
        value = max(int(minimum), value)
    if _is_finite_number(maximum):
        value = min(int(maximum), value)
    return value


def _increment_decision_tier_counters(
    content: ScenarioContentV2,
    counters: Mapping[str, int],
    *,
    tier: str,
) -> dict[str, int]:
    """Spec section 11.3 step 4: increment ``decision_applied`` counters only."""
    updated = dict(counters)
    for counter_id, spec in content.counters_spec.items():
        for rule in _as_seq(spec.get("incrementOn")):
            if _non_empty(rule.get("event")) != "decision_applied":
                continue
            when_tier = _non_empty(rule.get("whenTier"))
            if when_tier and when_tier != tier:
                continue
            updated[counter_id] = _clamp_counter_value(content, counter_id, updated.get(counter_id, 0) + 1)
    return updated


def _increment_corrective_entry_counters(
    content: ScenarioContentV2,
    counters: Mapping[str, int],
) -> dict[str, int]:
    """Spec section 11.3 step 6: increment ``corrective_scene_entered`` counters only.

    Kept as a separate pass from :func:`_increment_decision_tier_counters` so
    that a decision which both matches a ``decision_applied`` tier rule and
    also enters a corrective scene increments each declared counter exactly
    once for its own event, never twice for the same physical event.
    """
    updated = dict(counters)
    for counter_id, spec in content.counters_spec.items():
        for rule in _as_seq(spec.get("incrementOn")):
            if _non_empty(rule.get("event")) == "corrective_scene_entered":
                updated[counter_id] = _clamp_counter_value(content, counter_id, updated.get(counter_id, 0) + 1)
    return updated


# ---------------------------------------------------------------------------
# Routing resolution (spec section 11.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingOutcome:
    next_scene_id: str
    entered_corrective: bool
    skipped_corrective: bool
    corrective_scene_id: str | None = None
    reconvergence_scene_id: str | None = None


def resolve_routing(
    option: Mapping[str, Any],
    *,
    content: ScenarioContentV2,
    flags: frozenset[str],
    state: Mapping[str, float],
    counters: Mapping[str, int],
) -> RoutingOutcome:
    """The single authoritative routing algorithm from spec section 11.2."""
    routing = option.get("routing") or {}
    if routing.get("terminal") is True:
        return RoutingOutcome(next_scene_id=TERMINAL_SENTINEL, entered_corrective=False, skipped_corrective=False)

    primary = _non_empty(routing.get("primaryNextSceneId"))
    corrective_route = routing.get("correctiveRoute")
    if not corrective_route:
        return RoutingOutcome(next_scene_id=primary, entered_corrective=False, skipped_corrective=False)

    tier = _non_empty(option.get("evaluationTier"))
    trigger_tiers = {_non_empty(t) for t in _as_seq(corrective_route.get("triggerOnTiers"))}
    if tier not in trigger_tiers:
        return RoutingOutcome(next_scene_id=primary, entered_corrective=False, skipped_corrective=False)

    corrective_scene_id = _non_empty(corrective_route.get("correctiveSceneId"))
    reconvergence_scene_id = _non_empty(corrective_route.get("reconvergenceSceneId"))
    budget_condition = corrective_route.get("budgetCondition")
    budget_ok = (
        evaluate_condition(budget_condition, content=content, flags=flags, state=state, counters=counters)
        if budget_condition
        else True
    )
    if budget_ok:
        return RoutingOutcome(
            next_scene_id=corrective_scene_id or primary,
            entered_corrective=True,
            skipped_corrective=False,
            corrective_scene_id=corrective_scene_id,
            reconvergence_scene_id=reconvergence_scene_id,
        )

    skip_target = _non_empty(corrective_route.get("whenCorrectiveSkippedNextSceneId"))
    return RoutingOutcome(
        next_scene_id=skip_target or primary,
        entered_corrective=False,
        skipped_corrective=True,
        corrective_scene_id=corrective_scene_id,
        reconvergence_scene_id=reconvergence_scene_id,
    )


# ---------------------------------------------------------------------------
# Dialogue variant selection (spec section 9.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedDialogue:
    exchanges: tuple[Mapping[str, Any], ...]
    selected_variant_id: str | None


def select_dialogue_variant(
    scene: Mapping[str, Any],
    *,
    content: ScenarioContentV2,
    flags: frozenset[str],
    state: Mapping[str, float],
    counters: Mapping[str, int],
) -> ResolvedDialogue:
    """Deterministic variant selection: ascending priority, first match wins.

    Base ``dialogue.exchanges`` is the structural fallback used when no
    conditional variant matches (or none are declared). Overrides may only
    replace fields on an existing ``exchangeId`` — they never add, remove,
    or reorder exchanges.
    """
    dialogue = scene.get("dialogue") or {}
    base_exchanges = _as_seq(dialogue.get("exchanges"))
    base_order = tuple(str(ex["exchangeId"]) for ex in base_exchanges)
    resolved_by_id = {str(ex["exchangeId"]): dict(ex) for ex in base_exchanges}

    variants = sorted(_as_seq(dialogue.get("variants")), key=lambda v: v.get("priority"))
    seen_priorities: set[Any] = set()
    for variant in variants:
        priority = variant.get("priority")
        if priority in seen_priorities:
            raise ScenarioContentV2Error(
                f"duplicate dialogue variant priority {priority!r} in scene {scene.get('id')!r}"
            )
        seen_priorities.add(priority)

    for variant in variants:
        condition = variant.get("when")
        if not condition:
            raise ScenarioContentV2Error("dialogue variant is missing a 'when' condition")
        if not evaluate_condition(condition, content=content, flags=flags, state=state, counters=counters):
            continue

        merged = dict(resolved_by_id)
        for override in _as_seq(variant.get("overrides")):
            exchange_id = _non_empty(override.get("exchangeId"))
            if exchange_id not in merged:
                raise ScenarioContentV2Error(
                    f"dialogue variant override references unknown exchangeId {exchange_id!r}"
                )
            replaced = dict(merged[exchange_id])
            replaced.update({key: value for key, value in override.items() if key != "exchangeId"})
            merged[exchange_id] = replaced

        exchanges = tuple(MappingProxyType(merged[eid]) for eid in base_order)
        return ResolvedDialogue(exchanges=exchanges, selected_variant_id=_non_empty(variant.get("variantId")) or None)

    exchanges = tuple(MappingProxyType(resolved_by_id[eid]) for eid in base_order)
    return ResolvedDialogue(exchanges=exchanges, selected_variant_id=None)


# ---------------------------------------------------------------------------
# Deterministic option display order (spec section 17)
# ---------------------------------------------------------------------------


def _sha256_byte_stream(material: bytes):
    """Yield an unbounded deterministic byte stream derived from ``material``.

    **Engine V2 frozen §17 stream contract (SIM-ENGINE-V2-02):**

    Spec §17 text reads ``SHA256(material) … extended by SHA256(material ||
    counter)``. That wording is ambiguous about whether the *first* digest
    omits the counter. Engine V2 freezes a single unambiguous reading that
    matches the Slice-01 implementation and golden vectors:

    - ``material`` = UTF-8 encoding of
      ``attemptId + "\\n" + simulationId + "\\n" + version + "\\n" +
      canonicalContentSha256 + "\\n" + sceneId``
    - For ``counter = 0, 1, 2, …`` (unsigned big-endian uint32), yield the
      32 bytes of ``SHA256(material || uint32be(counter))`` in order.

    This does **not** use Python ``hash()``; ``PYTHONHASHSEED`` cannot affect
    results. Process-local ``random`` is never consulted.
    """
    counter = 0
    while True:
        block = hashlib.sha256(material + counter.to_bytes(4, "big")).digest()
        yield from block
        counter += 1


def _uniform_index(stream, upper_inclusive: int) -> int:
    """Return a uniform index in ``[0, upper_inclusive]`` via rejection sampling.

    Single-byte draws are rejected above the largest multiple of ``n`` that
    fits in a byte, which removes modulo bias while keeping the algorithm
    simple and fully deterministic given ``stream``. Scenario option counts
    are always small (well under 256), so a byte-at-a-time draw is
    sufficient and rejections are rare.
    """
    if upper_inclusive <= 0:
        return 0
    n = upper_inclusive + 1
    if n > 256:
        raise ScenarioContentV2Error("option display order does not support scenes with more than 256 options")
    limit = (256 // n) * n
    while True:
        draw = next(stream)
        if draw < limit:
            return draw % n


def deterministic_option_display_order(
    option_ids: Sequence[str],
    *,
    attempt_id: str,
    simulation_id: str,
    version: str,
    canonical_content_sha256: str,
    scene_id: str,
) -> tuple[str, ...]:
    """Deterministic Fisher-Yates shuffle for ``randomize_per_attempt_scene``.

    Seed material and byte stream: see :func:`_sha256_byte_stream`.
    """
    material = "\n".join((attempt_id, simulation_id, version, canonical_content_sha256, scene_id)).encode("utf-8")
    order = list(option_ids)
    stream = _sha256_byte_stream(material)
    for index in range(len(order) - 1, 0, -1):
        swap_index = _uniform_index(stream, index)
        order[index], order[swap_index] = order[swap_index], order[index]
    return tuple(order)


def resolve_option_display_order(
    option_ids: Sequence[str],
    *,
    content: ScenarioContentV2,
    attempt_id: str,
    scene_id: str,
) -> tuple[str, ...]:
    """Resolve display order according to validated ``optionDisplayPolicy``.

    - ``authored_order``: return authored ids unchanged (no seed, no shuffle).
    - ``randomize_per_attempt_scene``: SHA-256 Fisher–Yates (§17 / frozen stream).
    - any other policy: fail closed.
    """
    policy = content.option_display_policy
    if policy not in SUPPORTED_OPTION_DISPLAY_POLICIES:
        raise ScenarioContentV2Error(
            f"unsupported optionDisplayPolicy {policy!r}",
            path="optionDisplayPolicy",
        )
    if policy == "authored_order":
        return tuple(option_ids)
    return deterministic_option_display_order(
        option_ids,
        attempt_id=attempt_id,
        simulation_id=content.simulation_id,
        version=content.version,
        canonical_content_sha256=content.canonical_content_sha256,
        scene_id=scene_id,
    )


# ---------------------------------------------------------------------------
# Formula evaluation (spec section 14.2)
# ---------------------------------------------------------------------------


def _dimension_health(raw: float, *, minimum: float | None, maximum: float | None, polarity: str) -> float:
    lo = minimum if minimum is not None else 0.0
    hi = maximum if maximum is not None else 100.0
    span = hi - lo
    if span <= 0:
        raise ScenarioContentV2Error("state variable bounds produce a non-positive span for a health formula")
    if polarity == "higher_is_worse":
        return (hi - raw) / span * 100.0
    return (raw - lo) / span * 100.0


def compute_positive_health(content: ScenarioContentV2, *, state: Mapping[str, float]) -> float:
    classifier = content.document.get("outcomeClassifier") or {}
    formula = classifier.get("positiveHealthFormula") or {}
    if _non_empty(formula.get("type")) != "weighted_dimension_health":
        raise ScenarioContentV2Error("unsupported positiveHealthFormula.type")
    dimensions = _as_seq(formula.get("dimensions"))
    if not dimensions:
        raise ScenarioContentV2Error("weighted_dimension_health formula requires at least one dimension")
    values: list[float] = []
    for dimension in dimensions:
        variable_id = _non_empty(dimension.get("variableId"))
        if variable_id not in content.state_bounds:
            raise ScenarioContentV2Error(f"positiveHealthFormula references undeclared variable {variable_id!r}")
        if variable_id not in state:
            raise ScenarioContentV2Error(f"runtime state is missing required variable {variable_id!r}")
        polarity = _non_empty(dimension.get("polarity")) or "higher_is_better"
        minimum, maximum = content.state_bounds[variable_id]
        values.append(
            _require_finite_number(
                _dimension_health(
                    _require_finite_number(state[variable_id], path=f"state.{variable_id}"),
                    minimum=minimum,
                    maximum=maximum,
                    polarity=polarity,
                ),
                path=f"positiveHealth.{variable_id}",
            )
        )
    return _require_finite_number(sum(values) / len(values), path="positiveHealth")


def compute_decision_quality(content: ScenarioContentV2, *, tier_history: Sequence[str]) -> float:
    classifier = content.document.get("outcomeClassifier") or {}
    formula = classifier.get("decisionQualityFormula") or {}
    if _non_empty(formula.get("type")) != "tier_average":
        raise ScenarioContentV2Error("unsupported decisionQualityFormula.type")
    if _non_empty(formula.get("divisor")) != "scoredDecisionCount":
        raise ScenarioContentV2Error("unsupported decisionQualityFormula.divisor")
    if not tier_history:
        raise ScenarioClassificationV2Error("cannot classify an outcome with zero scored decisions")
    tier_points = classifier.get("tierPoints") or {}
    total = 0.0
    for tier in tier_history:
        points = tier_points.get(tier, 0)
        total += _require_finite_number(points, path=f"tierPoints.{tier}")
    return _require_finite_number(total / len(tier_history), path="decisionQuality")


def compute_composite(
    content: ScenarioContentV2,
    *,
    state: Mapping[str, float],
    tier_history: Sequence[str],
) -> float:
    classifier = content.document.get("outcomeClassifier") or {}
    formula = classifier.get("compositeFormula") or {}
    formula_type = _non_empty(formula.get("type"))
    metrics = {
        "positiveHealth": compute_positive_health(content, state=state),
        "decisionQuality": compute_decision_quality(content, tier_history=tier_history),
    }
    if formula_type == "identity":
        source = _non_empty(formula.get("source"))
        if source not in metrics:
            raise ScenarioContentV2Error(f"identity formula references unknown source {source!r}")
        return metrics[source]
    if formula_type == "linear_blend":
        terms = _as_seq(formula.get("terms"))
        if not terms:
            raise ScenarioContentV2Error("linear_blend formula requires at least one term")
        total = 0.0
        weight_sum = 0.0
        for term in terms:
            metric = _non_empty(term.get("metric"))
            if metric not in metrics:
                raise ScenarioContentV2Error(f"linear_blend term references unknown metric {metric!r}")
            weight = term.get("weight")
            weight_value = _require_finite_number(weight, path="compositeFormula.terms.weight")
            weight_sum += weight_value
            total += metrics[metric] * weight_value
        if abs(weight_sum - 1.0) > 1e-9:
            raise ScenarioContentV2Error("linear_blend weights must sum to 1.0 +/- 1e-9")
        return _require_finite_number(total, path="compositeScore")
    raise ScenarioContentV2Error(f"unsupported compositeFormula.type {formula_type!r}")


# ---------------------------------------------------------------------------
# Outcome classification: seven-step order (spec section 14.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassificationTrace:
    """Deterministic, replay-verifiable classification trace for debrief."""

    severe_cap_id: str | None
    moderate_cap_id: str | None
    moderate_cap_outcome_id: str | None
    moderate_cap_applied: bool
    disqualified_outcome_ids: tuple[str, ...]
    composite_score_unrounded: float
    band_outcome_id: str | None
    guard_tie_break_applied: bool
    final_outcome_id: str


def _select_score_band(content: ScenarioContentV2, composite: float) -> str | None:
    classifier = content.document.get("outcomeClassifier") or {}
    for band in _as_seq(classifier.get("scoreBands")):
        outcome_id = _non_empty(band.get("outcomeId"))
        min_inclusive = band.get("minInclusive")
        max_exclusive = band.get("maxExclusive")
        if _is_finite_number(min_inclusive) and composite < float(min_inclusive):
            continue
        if _is_finite_number(max_exclusive) and composite >= float(max_exclusive):
            continue
        return outcome_id or None
    return None


def classify_outcome(
    content: ScenarioContentV2,
    *,
    flags: frozenset[str],
    state: Mapping[str, float],
    counters: Mapping[str, int],
    tier_history: Sequence[str],
) -> ClassificationTrace:
    """The seven-step outcome classifier (spec section 14.4).

    1. Severe caps (first match forces the outcome; cannot be overridden by
       any later step).
    2. Moderate caps (collect the tightest ``maxOutcomeId`` by rank).
    3. Compute positiveHealth / decisionQuality / compositeScoreUnrounded.
       Computed unconditionally (even when a severe cap already forces the
       outcome) purely so ``composite_score_unrounded`` is always a real,
       displayable number in the trace/debrief rather than ``NaN`` — this
       never changes which outcome wins once a severe cap has fired.
    4. Strong guards (collect disqualified outcome ids).
    5. Numerical band selection on the unrounded composite.
    6. Deterministic tie-break: if the band-selected outcome is
       guard-disqualified, downgrade to the next-worse-ranked outcome that
       is not disqualified; then, if a moderate cap applies and the
       (possibly downgraded) outcome is still numerically better than the
       cap allows, downgrade again to the capped outcome.
    7. Display rounding happens separately, in :func:`round_half_away_from_zero`,
       so this function's ``composite_score_unrounded`` is never rounded.
    """
    classifier = content.document.get("outcomeClassifier") or {}
    ranks = content.outcome_ranks

    forced_outcome: str | None = None
    forced_cap_id: str | None = None
    for cap in _as_seq(classifier.get("severeCaps")):
        condition = cap.get("when")
        if condition and evaluate_condition(condition, content=content, flags=flags, state=state, counters=counters):
            candidate = _non_empty((cap.get("effect") or {}).get("forceOutcomeId"))
            if candidate:
                if candidate not in ranks:
                    raise ScenarioContentV2Error(f"severe cap forces unknown outcome {candidate!r}")
                forced_outcome = candidate
                forced_cap_id = _non_empty(cap.get("capId"))
                break

    max_cap_rank: int | None = None
    max_cap_outcome: str | None = None
    max_cap_id: str | None = None
    for cap in _as_seq(classifier.get("moderateCaps")):
        condition = cap.get("when")
        if not condition or not evaluate_condition(
            condition, content=content, flags=flags, state=state, counters=counters
        ):
            continue
        capped = _non_empty((cap.get("effect") or {}).get("maxOutcomeId"))
        if not capped:
            continue
        rank = ranks.get(capped)
        if rank is None:
            raise ScenarioContentV2Error(f"moderate cap references unknown outcome {capped!r}")
        if max_cap_rank is None or rank < max_cap_rank:
            max_cap_rank = rank
            max_cap_outcome = capped
            max_cap_id = _non_empty(cap.get("capId"))

    composite = compute_composite(content, state=state, tier_history=tier_history)

    disqualified: set[str] = set()
    for guard in _as_seq(classifier.get("strongGuards")):
        condition = guard.get("when")
        if condition and evaluate_condition(condition, content=content, flags=flags, state=state, counters=counters):
            for outcome_id in _as_seq((guard.get("effect") or {}).get("disqualifyOutcomeIds")):
                disqualified.add(_non_empty(outcome_id))

    band_outcome = _select_score_band(content, composite)
    if band_outcome is None:
        raise ScenarioClassificationV2Error("no score band matched the computed composite score")

    if forced_outcome is not None:
        return ClassificationTrace(
            severe_cap_id=forced_cap_id,
            moderate_cap_id=max_cap_id,
            moderate_cap_outcome_id=max_cap_outcome,
            moderate_cap_applied=False,
            disqualified_outcome_ids=tuple(sorted(disqualified)),
            composite_score_unrounded=composite,
            band_outcome_id=band_outcome,
            guard_tie_break_applied=False,
            final_outcome_id=forced_outcome,
        )

    selected = band_outcome
    guard_tie_break_applied = False
    if selected in disqualified:
        guard_tie_break_applied = True
        selected_rank = ranks.get(selected)
        fallback: str | None = None
        if selected_rank is not None:
            for candidate_id, candidate_rank in sorted(ranks.items(), key=lambda item: item[1]):
                if candidate_rank > selected_rank and candidate_id not in disqualified:
                    fallback = candidate_id
                    break
        if fallback is None:
            raise ScenarioClassificationV2Error(
                f"outcome {selected!r} is disqualified by a strong guard with no lower-ranked fallback available"
            )
        selected = fallback

    moderate_cap_applied = False
    if max_cap_rank is not None and max_cap_outcome is not None:
        selected_rank = ranks.get(selected)
        if selected_rank is not None and selected_rank < max_cap_rank:
            selected = max_cap_outcome
            moderate_cap_applied = True

    return ClassificationTrace(
        severe_cap_id=None,
        moderate_cap_id=max_cap_id,
        moderate_cap_outcome_id=max_cap_outcome,
        moderate_cap_applied=moderate_cap_applied,
        disqualified_outcome_ids=tuple(sorted(disqualified)),
        composite_score_unrounded=composite,
        band_outcome_id=band_outcome,
        guard_tie_break_applied=guard_tie_break_applied,
        final_outcome_id=selected,
    )


def round_half_away_from_zero(value: float) -> int:
    """Display rounding only — never used for band selection (spec 14.2)."""
    if isinstance(value, float) and math.isnan(value):
        raise ScenarioClassificationV2Error("cannot round a NaN composite score")
    if value >= 0:
        return math.floor(value + 0.5)
    return math.ceil(value - 0.5)


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioDecisionInputV2:
    """The only learner-submittable decision shape: sequence + scene + option.

    There is no field here for evaluation tier, state, flags, counters,
    routing, or outcome — the decision-input contract in the task is
    enforced structurally by this dataclass having no such fields, not by a
    runtime rejection list.
    """

    sequence_number: int
    scene_id: str
    option_id: str


@dataclass(frozen=True)
class RoutingResolutionEvent:
    sequence_number: int
    scene_id: str
    option_id: str
    next_scene_id: str
    entered_corrective: bool
    skipped_corrective: bool


@dataclass(frozen=True)
class CorrectiveEntryEvent:
    sequence_number: int
    scene_id: str
    option_id: str
    corrective_scene_id: str
    reconvergence_scene_id: str


@dataclass(frozen=True)
class SkippedCorrectiveEvent:
    sequence_number: int
    scene_id: str
    option_id: str
    attempted_corrective_scene_id: str
    reconvergence_scene_id: str
    reason: str = "budget_exhausted"


@dataclass(frozen=True)
class VariantSelectionEvent:
    sequence_number: int
    """0 for the initial scene entered at initialization (no decision yet)."""
    scene_id: str
    selected_variant_id: str | None


@dataclass(frozen=True)
class DebriefTraceEntry:
    """One decision's full server-side trace (spec section 16.2 inputs).

    Never surfaced through a learner-facing view; only reachable via
    :func:`build_debrief_trace`, which itself refuses to run on an
    incomplete attempt.

    Dialogue variant fields (SIM-ENGINE-V2-02 / F-M-005):

    - ``presented_dialogue_variant_id``: variant visible in the scene where
      the learner submitted this decision (may be ``None`` for base dialogue).
    - ``next_dialogue_variant_id``: variant selected when entering the next
      scene after this decision; ``None`` when the decision is terminal or
      the next scene uses base dialogue.
    """

    sequence_number: int
    scene_id: str
    option_id: str
    evaluation_tier: str
    debrief_seed: Mapping[str, Any]
    state_delta: Mapping[str, float]
    state_after: Mapping[str, float]
    flags_cleared: tuple[str, ...]
    flags_set: tuple[str, ...]
    next_scene_id: str
    entered_corrective: bool
    skipped_corrective: bool
    presented_dialogue_variant_id: str | None
    next_dialogue_variant_id: str | None
    competency_tags: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioTerminalResultV2:
    outcome_id: str
    display_score: int
    classification: ClassificationTrace
    decisions: tuple[DebriefTraceEntry, ...]
    final_state: Mapping[str, float]
    final_counters: Mapping[str, int]
    flags: frozenset[str]
    engine_version: str
    canonical_content_sha256: str


@dataclass(frozen=True)
class ScenarioRunV2Snapshot:
    """The full Engine V2 runtime-state contract.

    Every field required by the SIM-ENGINE-V2-01 runtime-state contract is
    present: identity (via ``content``), current scene, expected sequence,
    state, flags, counters, corrective/skip/routing/variant/order history,
    terminal state, and final outcome. This dataclass is frozen and every
    engine function returns a *new* instance rather than mutating one.
    """

    content: ScenarioContentV2
    attempt_id: str
    current_scene_id: str | None
    expected_sequence_number: int
    state: Mapping[str, float]
    flags: frozenset[str]
    counters: Mapping[str, int]
    tier_history: tuple[str, ...]
    decisions: tuple[DebriefTraceEntry, ...]
    routing_resolutions: tuple[RoutingResolutionEvent, ...]
    corrective_entries: tuple[CorrectiveEntryEvent, ...]
    skipped_corrective_events: tuple[SkippedCorrectiveEvent, ...]
    variant_selections: tuple[VariantSelectionEvent, ...]
    option_display_order_by_scene: Mapping[str, tuple[str, ...]]
    is_complete: bool
    terminal_result: ScenarioTerminalResultV2 | None = None

    @property
    def corrective_scenes_experienced(self) -> int:
        experienced_counter_id = (
            _non_empty(self.content.corrective_budget_policy.get("experiencedCounterId"))
            or "correctiveScenesExperienced"
        )
        return int(self.counters.get(experienced_counter_id, 0))


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def _scene_option_ids(scene: Mapping[str, Any]) -> list[str]:
    return [str(option["id"]) for option in _as_seq((scene.get("decision") or {}).get("options"))]


def _enter_scene(
    content: ScenarioContentV2,
    scene: Mapping[str, Any],
    *,
    attempt_id: str,
    flags: frozenset[str],
    state: Mapping[str, float],
    counters: Mapping[str, int],
) -> tuple[frozenset[str], ResolvedDialogue, tuple[str, ...]]:
    """Shared "enter a scene" logic used by both initialization and
    decision application: apply environmental entry flags, then select the
    dialogue variant, then compute the deterministic option display order.
    Order matches spec section 9.2 ("apply environmentalFlagsOnEntry before
    variant selection") and the task's step-12 ordering.
    """
    entered_flags = _apply_environmental_entry_flags(content, scene, flags)
    dialogue = select_dialogue_variant(scene, content=content, flags=entered_flags, state=state, counters=counters)
    order = resolve_option_display_order(
        _scene_option_ids(scene),
        content=content,
        attempt_id=attempt_id,
        scene_id=str(scene["id"]),
    )
    return entered_flags, dialogue, order


def start_scenario_run_v2(content: ScenarioContentV2, *, attempt_id: str) -> ScenarioRunV2Snapshot:
    """Initialize a new Engine V2 run from validated content (spec section 5)."""
    if not _non_empty(attempt_id):
        raise ScenarioRunStateV2Error("attemptId must be a non-empty string", path="attemptId")

    scene = content.scenes_by_id.get(content.start_scene)
    if scene is None:
        raise ScenarioContentV2Error(f"startScene {content.start_scene!r} does not resolve to an authored scene")

    state = dict(content.initial_state)
    counters = dict(content.initial_counters)
    flags, dialogue, order = _enter_scene(
        content, scene, attempt_id=attempt_id, flags=content.initial_flags, state=state, counters=counters
    )

    return ScenarioRunV2Snapshot(
        content=content,
        attempt_id=str(attempt_id),
        current_scene_id=content.start_scene,
        expected_sequence_number=1,
        state=MappingProxyType(state),
        flags=flags,
        counters=MappingProxyType(counters),
        tier_history=(),
        decisions=(),
        routing_resolutions=(),
        corrective_entries=(),
        skipped_corrective_events=(),
        variant_selections=(VariantSelectionEvent(0, str(scene["id"]), dialogue.selected_variant_id),),
        option_display_order_by_scene=MappingProxyType({str(scene["id"]): order}),
        is_complete=False,
        terminal_result=None,
    )


# ---------------------------------------------------------------------------
# Decision application: the exact 16-step order
# ---------------------------------------------------------------------------


def apply_decision_v2(run: ScenarioRunV2Snapshot, decision_input: ScenarioDecisionInputV2) -> ScenarioRunV2Snapshot:
    """Apply one learner decision, in the exact 16-step order from the task.

    Never mutates ``run`` (every dataclass involved is frozen); on any
    rejection this raises before constructing a new snapshot, so the
    original ``run`` remains the caller's only valid state.
    """
    if run.is_complete:
        raise ScenarioRunStateV2Error("scenario run is already complete", path="isComplete")

    # Step 1: validate sequence, scene, and option identity (strict typing first).
    sequence_number = _require_strict_int(decision_input.sequence_number, path="sequenceNumber")
    if sequence_number < 1:
        raise ScenarioRunStateV2Error(
            f"sequenceNumber must be >= 1; got {sequence_number}",
            path="sequenceNumber",
        )
    if sequence_number != run.expected_sequence_number:
        raise ScenarioRunStateV2Error(
            f"expected sequenceNumber {run.expected_sequence_number}, got {sequence_number}",
            path="sequenceNumber",
        )
    scene_id = _require_non_empty_str(decision_input.scene_id, path="sceneId")
    option_id = _require_non_empty_str(decision_input.option_id, path="optionId")
    if scene_id != run.current_scene_id:
        raise ScenarioRunStateV2Error(
            f"expected sceneId {run.current_scene_id!r}, got {scene_id!r}",
            path="sceneId",
        )
    scene = run.content.scenes_by_id.get(run.current_scene_id)
    if scene is None:
        raise ScenarioContentV2Error(f"current scene {run.current_scene_id!r} does not exist in content")

    # Step 2: resolve the option from immutable content.
    option = next(
        (o for o in _as_seq((scene.get("decision") or {}).get("options")) if str(o.get("id")) == option_id),
        None,
    )
    if option is None:
        raise ScenarioRunStateV2Error(
            f"option {option_id!r} is not valid for scene {scene['id']!r}",
            path="optionId",
        )
    # Step 3: record the selected stable option id (carried via option_id below).

    presented_variant_id = None
    for event in reversed(run.variant_selections):
        if event.scene_id == scene["id"]:
            presented_variant_id = event.selected_variant_id
            break

    # Steps 4-5: apply state deltas, clamped per registry definition.
    raw_deltas = option.get("stateChanges") or {}
    deltas = {
        str(key): _require_finite_number(value, path=f"stateChanges.{key}")
        for key, value in raw_deltas.items()
    }
    new_state = _apply_state_deltas(run.content, run.state, deltas)

    # Steps 6-7: clear flags, then set flags.
    clear_flags = tuple(str(f) for f in _as_seq(option.get("clearFlags")))
    set_flags = tuple(str(f) for f in _as_seq(option.get("setFlags")))
    new_flags = _apply_flag_changes(run.content, run.flags, clear=clear_flags, set_=set_flags)

    # Step 8: record the server-resolved evaluation tier.
    tier = _non_empty(option.get("evaluationTier"))
    if tier not in _TIER_ORDER:
        raise ScenarioContentV2Error(f"option {option['id']!r} has an invalid evaluationTier {tier!r}")
    new_counters = _increment_decision_tier_counters(run.content, run.counters, tier=tier)

    if scene.get("sceneType") == "corrective" and (option.get("routing") or {}).get("correctiveRoute"):
        raise ScenarioContentV2Error(
            f"corrective scene {scene['id']!r} option {option['id']!r} must not own a correctiveRoute"
        )

    # Step 9: resolve the selected option's routing.
    routing_outcome = resolve_routing(option, content=run.content, flags=new_flags, state=new_state, counters=new_counters)

    # Step 10: corrective entry / budget / skip.
    corrective_entries = run.corrective_entries
    skipped_events = run.skipped_corrective_events
    if routing_outcome.entered_corrective:
        new_counters = _increment_corrective_entry_counters(run.content, new_counters)
        corrective_entries = corrective_entries + (
            CorrectiveEntryEvent(
                sequence_number=sequence_number,
                scene_id=scene["id"],
                option_id=option["id"],
                corrective_scene_id=routing_outcome.corrective_scene_id or "",
                reconvergence_scene_id=routing_outcome.reconvergence_scene_id or "",
            ),
        )
    elif routing_outcome.skipped_corrective:
        skipped_events = skipped_events + (
            SkippedCorrectiveEvent(
                sequence_number=sequence_number,
                scene_id=scene["id"],
                option_id=option["id"],
                attempted_corrective_scene_id=routing_outcome.corrective_scene_id or "",
                reconvergence_scene_id=routing_outcome.reconvergence_scene_id or "",
            ),
        )
    # Step 11 (no corrective route taken) is implicit: routing_outcome.next_scene_id
    # already carries primaryNextSceneId / EVALUATE_ENDING in that case.

    routing_resolution_event = RoutingResolutionEvent(
        sequence_number=sequence_number,
        scene_id=scene["id"],
        option_id=option["id"],
        next_scene_id=routing_outcome.next_scene_id,
        entered_corrective=routing_outcome.entered_corrective,
        skipped_corrective=routing_outcome.skipped_corrective,
    )
    new_tier_history = run.tier_history + (tier,)
    new_expected_sequence = run.expected_sequence_number + 1

    debrief_entry = DebriefTraceEntry(
        sequence_number=sequence_number,
        scene_id=scene["id"],
        option_id=option["id"],
        evaluation_tier=tier,
        debrief_seed=MappingProxyType(dict(option.get("debriefSeed") or {})),
        state_delta=MappingProxyType(dict(deltas)),
        state_after=MappingProxyType(dict(new_state)),
        flags_cleared=clear_flags,
        flags_set=set_flags,
        next_scene_id=routing_outcome.next_scene_id,
        entered_corrective=routing_outcome.entered_corrective,
        skipped_corrective=routing_outcome.skipped_corrective,
        presented_dialogue_variant_id=presented_variant_id,
        next_dialogue_variant_id=None,
        competency_tags=tuple(str(t) for t in _as_seq(option.get("competencyTags"))),
    )

    # Step 15/16 (terminal branch): classify outcome and return.
    if routing_outcome.next_scene_id == TERMINAL_SENTINEL:
        classification = classify_outcome(
            run.content, flags=new_flags, state=new_state, counters=new_counters, tier_history=new_tier_history
        )
        display_score = round_half_away_from_zero(
            _require_finite_number(
                classification.composite_score_unrounded, path="classification.composite_score_unrounded"
            )
        )
        decisions = run.decisions + (debrief_entry,)
        terminal_result = ScenarioTerminalResultV2(
            outcome_id=classification.final_outcome_id,
            display_score=display_score,
            classification=classification,
            decisions=decisions,
            final_state=MappingProxyType(dict(new_state)),
            final_counters=MappingProxyType(dict(new_counters)),
            flags=new_flags,
            engine_version=ENGINE_VERSION,
            canonical_content_sha256=run.content.canonical_content_sha256,
        )
        return ScenarioRunV2Snapshot(
            content=run.content,
            attempt_id=run.attempt_id,
            current_scene_id=None,
            expected_sequence_number=new_expected_sequence,
            state=MappingProxyType(dict(new_state)),
            flags=new_flags,
            counters=MappingProxyType(dict(new_counters)),
            tier_history=new_tier_history,
            decisions=decisions,
            routing_resolutions=run.routing_resolutions + (routing_resolution_event,),
            corrective_entries=corrective_entries,
            skipped_corrective_events=skipped_events,
            variant_selections=run.variant_selections,
            option_display_order_by_scene=run.option_display_order_by_scene,
            is_complete=True,
            terminal_result=terminal_result,
        )

    # Steps 12-13 (non-terminal branch): enter next scene, select variant,
    # compute/reuse deterministic option display order.
    next_scene = run.content.scenes_by_id.get(routing_outcome.next_scene_id)
    if next_scene is None:
        raise ScenarioContentV2Error(f"routing resolved to unknown scene {routing_outcome.next_scene_id!r}")

    entered_flags, dialogue, computed_order = _enter_scene(
        run.content, next_scene, attempt_id=run.attempt_id, flags=new_flags, state=new_state, counters=new_counters
    )
    existing_order = run.option_display_order_by_scene.get(routing_outcome.next_scene_id)
    next_order = existing_order if existing_order is not None else computed_order

    debrief_entry = dataclasses.replace(debrief_entry, next_dialogue_variant_id=dialogue.selected_variant_id)

    return ScenarioRunV2Snapshot(
        content=run.content,
        attempt_id=run.attempt_id,
        current_scene_id=routing_outcome.next_scene_id,
        expected_sequence_number=new_expected_sequence,
        state=MappingProxyType(dict(new_state)),
        flags=entered_flags,
        counters=MappingProxyType(dict(new_counters)),
        tier_history=new_tier_history,
        decisions=run.decisions + (debrief_entry,),
        routing_resolutions=run.routing_resolutions + (routing_resolution_event,),
        corrective_entries=corrective_entries,
        skipped_corrective_events=skipped_events,
        variant_selections=run.variant_selections
        + (VariantSelectionEvent(sequence_number, routing_outcome.next_scene_id, dialogue.selected_variant_id),),
        option_display_order_by_scene=MappingProxyType(
            {**run.option_display_order_by_scene, routing_outcome.next_scene_id: next_order}
        ),
        is_complete=False,
        terminal_result=None,
    )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _validate_decision_sequence_v2(decisions: Sequence[ScenarioDecisionInputV2]) -> None:
    expected = 1
    seen: set[int] = set()
    for index, decision in enumerate(decisions):
        sequence_number = _require_strict_int(
            decision.sequence_number, path=f"decisions[{index}].sequenceNumber"
        )
        if sequence_number < 1:
            raise ScenarioRunStateV2Error(
                f"sequenceNumber must be >= 1; got {sequence_number}",
                path=f"decisions[{index}].sequenceNumber",
            )
        if sequence_number in seen:
            raise ScenarioRunStateV2Error(
                f"duplicate sequenceNumber {sequence_number} in decision history",
                path=f"decisions[{index}].sequenceNumber",
            )
        seen.add(sequence_number)
        if sequence_number != expected:
            raise ScenarioRunStateV2Error(
                f"expected sequenceNumber {expected} at position {index}, got {sequence_number}",
                path=f"decisions[{index}].sequenceNumber",
            )
        _require_non_empty_str(decision.scene_id, path=f"decisions[{index}].sceneId")
        _require_non_empty_str(decision.option_id, path=f"decisions[{index}].optionId")
        expected += 1


def replay_scenario_run_v2(
    content: ScenarioContentV2,
    *,
    attempt_id: str,
    decisions: Sequence[ScenarioDecisionInputV2],
) -> ScenarioRunV2Snapshot:
    """Deterministically reconstruct a run from validated content + decision history.

    Supports empty (returns the same result as :func:`start_scenario_run_v2`),
    partial (returns a live, resumable snapshot), and complete histories.
    Every derived value (state, flags, counters, routing, corrective
    entries/skips, dialogue variants, option order, terminal outcome) is
    always recomputed via :func:`apply_decision_v2`, never trusted from any
    external source.
    """
    _validate_decision_sequence_v2(decisions)
    run = start_scenario_run_v2(content, attempt_id=attempt_id)
    for decision in decisions:
        if run.is_complete:
            raise ScenarioReplayV2Error(
                "decision supplied after the run already reached terminal completion",
                path=f"decisions[{decision.sequence_number - 1}]",
            )
        if run.current_scene_id != decision.scene_id:
            raise ScenarioRunStateV2Error(
                f"expected current scene {run.current_scene_id!r}, got replay step for {decision.scene_id!r}",
                path=f"decisions[{decision.sequence_number - 1}].sceneId",
            )
        run = apply_decision_v2(run, decision)
    return run


def verify_replay_identity_v2(
    content: ScenarioContentV2,
    *,
    pinned_simulation_id: str,
    pinned_version: str,
    pinned_schema_version: str,
    pinned_canonical_content_sha256: str,
    pinned_engine_version: str,
) -> None:
    """Fail closed if pinned attempt identity no longer matches ``content``."""
    mismatches = []
    if content.simulation_id != pinned_simulation_id:
        mismatches.append("simulationId")
    if content.version != pinned_version:
        mismatches.append("version")
    if content.schema_version != pinned_schema_version:
        mismatches.append("schemaVersion")
    if content.canonical_content_sha256 != pinned_canonical_content_sha256:
        mismatches.append("canonicalContentSha256")
    if ENGINE_VERSION != pinned_engine_version:
        mismatches.append("engineVersion")
    if mismatches:
        raise ScenarioReplayV2Error(f"replay identity mismatch on field(s): {mismatches}")


def build_debrief_trace(run: ScenarioRunV2Snapshot) -> tuple[DebriefTraceEntry, ...]:
    """Return the full per-decision debrief trace. Requires a complete run."""
    if not run.is_complete:
        raise ScenarioRunStateV2Error("debrief trace is only available for a completed run", path="isComplete")
    return run.decisions


# ---------------------------------------------------------------------------
# Learner-safe views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearnerOptionView:
    id: str
    title: str | None
    text: str


@dataclass(frozen=True)
class LearnerSceneView:
    scene_id: str
    title: str
    setting: str
    dialogue_exchanges: tuple[Mapping[str, Any], ...]
    characters_present: tuple[str, ...]
    learner_present: bool
    decision_prompt: str
    options: tuple[LearnerOptionView, ...]
    progress_metadata: Mapping[str, Any] | None
    accessibility: Mapping[str, Any] | None
    mobile_presentation: Mapping[str, Any] | None
    expected_sequence_number: int
    is_complete: bool


@dataclass(frozen=True)
class LearnerTerminalView:
    outcome_id: str
    outcome_title: str
    narrative: str
    display_score: int


def build_learner_scene_view(run: ScenarioRunV2Snapshot) -> LearnerSceneView:
    """Build the learner-facing view of the current scene.

    Excludes evaluationTier, state deltas, hidden flags, route conditions,
    outcome caps, formula weights, strongest-option hints, and debrief
    seeds — none of those fields are read from ``scene``/``option`` here.
    """
    if run.is_complete or run.current_scene_id is None:
        raise ScenarioRunStateV2Error("cannot build a scene view for a completed run", path="currentSceneId")
    scene = run.content.scenes_by_id[run.current_scene_id]
    dialogue = select_dialogue_variant(
        scene, content=run.content, flags=run.flags, state=run.state, counters=run.counters
    )
    order = run.option_display_order_by_scene.get(run.current_scene_id, ())
    options_by_id = {str(o["id"]): o for o in _as_seq((scene.get("decision") or {}).get("options"))}
    options = tuple(
        LearnerOptionView(
            id=option_id,
            title=options_by_id[option_id].get("title"),
            text=str(options_by_id[option_id].get("text") or ""),
        )
        for option_id in order
        if option_id in options_by_id
    )
    return LearnerSceneView(
        scene_id=str(scene["id"]),
        title=str(scene.get("title") or ""),
        setting=str(scene.get("setting") or ""),
        dialogue_exchanges=dialogue.exchanges,
        characters_present=tuple(str(c) for c in _as_seq(scene.get("charactersPresent"))),
        learner_present=bool(scene.get("learnerPresent")),
        decision_prompt=str((scene.get("decision") or {}).get("prompt") or ""),
        options=options,
        progress_metadata=scene.get("progressMetadata"),
        accessibility=scene.get("accessibility"),
        mobile_presentation=scene.get("mobilePresentation"),
        expected_sequence_number=run.expected_sequence_number,
        is_complete=run.is_complete,
    )


def build_learner_terminal_view(run: ScenarioRunV2Snapshot) -> LearnerTerminalView:
    if not run.is_complete or run.terminal_result is None:
        raise ScenarioRunStateV2Error("cannot build a terminal view for an incomplete run", path="isComplete")
    outcome = next(
        o for o in _as_seq(run.content.document.get("outcomes")) if str(o.get("outcomeId")) == run.terminal_result.outcome_id
    )
    return LearnerTerminalView(
        outcome_id=run.terminal_result.outcome_id,
        outcome_title=str(outcome.get("title") or ""),
        narrative=str(outcome.get("narrative") or ""),
        display_score=run.terminal_result.display_score,
    )
