"""Deterministic Scenario Simulator execution and replay engine."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from utils.scenario_schema import (
    TERMINAL_SENTINEL,
    ScenarioContent,
    ScenarioEnding,
    ScenarioScene,
    ScenarioStateVariable,
)

ENGINE_VERSION = "SCENARIO_ENGINE_V1"


class ScenarioEngineError(Exception):
    """Base error for scenario execution and replay."""


class ScenarioRunStateError(ScenarioEngineError):
    """Raised when a decision cannot be applied to the current run state."""

    def __init__(self, message: str, *, path: str = "") -> None:
        self.path = path
        super().__init__(message if not path else f"{path}: {message}")


class ScenarioReplayIdentityError(ScenarioEngineError):
    """Raised when a serialized run payload's identity does not match the
    scenario content it is being replayed against.

    Identity is checked before a single decision is replayed — a history
    that was serialized against a different simulation, version, content
    hash, or engine version must never be silently replayed as if it
    belonged to the supplied content.
    """


@dataclass(frozen=True)
class ScenarioDecisionRecord:
    sequence_number: int
    scene_id: str
    option_id: str
    domain_id: str
    is_correct: bool
    next_scene: str
    state_after: Mapping[str, float]
    flags_after: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioDecisionInput:
    """The authoritative, persistable unit of replay input.

    This is the engine's canonical contract for "one decision a candidate
    made," independent of any storage backend. `sequence_number` is
    explicit and mandatory (1-based, matching `ScenarioDecisionRecord`)
    specifically so replay can strictly validate that a supplied decision
    history has no gaps, duplicates, or reordering *before* trusting it to
    drive scene traversal — the engine must never infer ordering purely
    from list position, since a future persistence layer may return rows
    in any order. Replay trusts only these three fields; every other
    aspect of a run (state, flags, correctness, domain performance,
    current scene, completion, ending) is always recomputed from the
    scenario content, never taken from a serialized/persisted source.
    """

    sequence_number: int
    scene_id: str
    option_id: str


@dataclass(frozen=True)
class DomainPerformanceSnapshot:
    domain_id: str
    correct_count: int
    total_count: int

    @property
    def accuracy(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.correct_count / self.total_count


@dataclass(frozen=True)
class ScenarioTerminalResult:
    ending_id: str
    score_band: str
    narrative: str
    recommended_review: tuple[str, ...]
    final_state: Mapping[str, float]
    flags: tuple[str, ...]
    decisions: tuple[ScenarioDecisionRecord, ...]
    domain_performance: tuple[DomainPerformanceSnapshot, ...]
    engine_version: str
    canonical_content_sha256: str


@dataclass(frozen=True)
class ScenarioRunSnapshot:
    content: ScenarioContent
    current_scene_id: str | None
    state: Mapping[str, float]
    flags: tuple[str, ...]
    decisions: tuple[ScenarioDecisionRecord, ...]
    is_complete: bool
    terminal_result: ScenarioTerminalResult | None = None


def _scene_map(content: ScenarioContent) -> dict[str, ScenarioScene]:
    return {scene.id: scene for scene in content.scenes}


def _clamp_value(value: float, variable: ScenarioStateVariable | None) -> float:
    if variable is None:
        return value
    if variable.minimum is not None:
        value = max(variable.minimum, value)
    if variable.maximum is not None:
        value = min(variable.maximum, value)
    return value


def _variable_lookup(content: ScenarioContent) -> dict[str, ScenarioStateVariable]:
    return {variable.key: variable for variable in content.state_variables}


def _freeze_state(state: Mapping[str, float]) -> Mapping[str, float]:
    """Return an immutable, independently-owned view of a state mapping.

    A defensive copy is taken first (so later external mutation of
    whatever mapping was passed in can never leak into the frozen view),
    then wrapped in `MappingProxyType` so the view itself rejects item
    assignment. A frozen dataclass field holding a plain `dict` is not
    sufficient on its own — the dict itself remains mutable through the
    attribute — so every state mapping stored on a `ScenarioRunSnapshot`,
    `ScenarioDecisionRecord`, or `ScenarioTerminalResult` is produced
    through this helper.
    """
    return MappingProxyType(dict(state))


def apply_state_changes(
    state: Mapping[str, float],
    changes: Mapping[str, float],
    *,
    variables: Mapping[str, ScenarioStateVariable],
) -> Mapping[str, float]:
    updated = dict(state)
    for key, delta in changes.items():
        if key not in variables:
            raise ScenarioRunStateError(
                f"stateChanges key {key!r} is not declared in stateVariables",
                path=f"stateChanges.{key}",
            )
        current = float(updated.get(key, 0.0))
        updated[key] = _clamp_value(current + float(delta), variables.get(key))
    return _freeze_state(updated)


def ending_condition_base_key(condition_key: str) -> str:
    for suffix in ("Min", "Max", "Equals"):
        if condition_key.endswith(suffix):
            return condition_key[: -len(suffix)]
    return condition_key


_SUPPORTED_ENDING_CONDITION_SUFFIXES = ("Min", "Max", "Equals")


def ending_matches_state(ending: ScenarioEnding, state: Mapping[str, float]) -> bool:
    for condition_key, threshold in ending.condition.items():
        if not condition_key.endswith(_SUPPORTED_ENDING_CONDITION_SUFFIXES):
            raise ScenarioEngineError(
                f"Unsupported ending condition operator in {condition_key!r}; "
                f"only Min/Max/Equals suffixes are defined by the adopted schema"
            )
        base_key = ending_condition_base_key(condition_key)
        if base_key not in state:
            raise ScenarioEngineError(
                f"Ending condition {condition_key!r} references state variable "
                f"{base_key!r}, which is missing from the final state"
            )
        actual = float(state[base_key])
        if condition_key.endswith("Min"):
            if actual < float(threshold):
                return False
        elif condition_key.endswith("Max"):
            if actual > float(threshold):
                return False
        elif condition_key.endswith("Equals"):
            if actual != float(threshold):
                return False
    return True


def evaluate_ending(content: ScenarioContent, state: Mapping[str, float]) -> ScenarioEnding:
    for ending in content.endings:
        if ending_matches_state(ending, state):
            return ending
    raise ScenarioEngineError("No ending condition matched the final state")


def compute_domain_performance(
    decisions: Sequence[ScenarioDecisionRecord],
) -> tuple[DomainPerformanceSnapshot, ...]:
    totals: dict[str, list[int]] = {}
    for decision in decisions:
        bucket = totals.setdefault(decision.domain_id, [0, 0])
        bucket[1] += 1
        if decision.is_correct:
            bucket[0] += 1
    return tuple(
        DomainPerformanceSnapshot(
            domain_id=domain_id,
            correct_count=counts[0],
            total_count=counts[1],
        )
        for domain_id, counts in sorted(totals.items())
    )


def start_scenario_run(content: ScenarioContent) -> ScenarioRunSnapshot:
    initial_state = {key: float(value) for key, value in content.initial_state.items()}
    return ScenarioRunSnapshot(
        content=content,
        current_scene_id=content.start_scene,
        state=_freeze_state(initial_state),
        flags=(),
        decisions=(),
        is_complete=False,
        terminal_result=None,
    )


def get_current_scene(run: ScenarioRunSnapshot) -> ScenarioScene:
    if run.is_complete or run.current_scene_id is None:
        raise ScenarioRunStateError("Scenario run is already complete", path="current_scene_id")
    scenes = _scene_map(run.content)
    scene = scenes.get(run.current_scene_id)
    if scene is None:
        raise ScenarioRunStateError(
            f"Current scene {run.current_scene_id!r} does not exist in scenario content",
            path="current_scene_id",
        )
    return scene


def _build_terminal_result(
    content: ScenarioContent,
    *,
    state: Mapping[str, float],
    flags: tuple[str, ...],
    decisions: tuple[ScenarioDecisionRecord, ...],
    ending: ScenarioEnding,
) -> ScenarioTerminalResult:
    return ScenarioTerminalResult(
        ending_id=ending.id,
        score_band=ending.score_band,
        narrative=ending.narrative,
        recommended_review=ending.recommended_review,
        final_state=_freeze_state(state),
        flags=flags,
        decisions=decisions,
        domain_performance=compute_domain_performance(decisions),
        engine_version=ENGINE_VERSION,
        canonical_content_sha256=content.canonical_content_sha256,
    )


def apply_decision(run: ScenarioRunSnapshot, option_id: str) -> ScenarioRunSnapshot:
    if run.is_complete:
        raise ScenarioRunStateError("Scenario run is already complete", path="is_complete")

    scene = get_current_scene(run)
    selected = next((option for option in scene.decision.options if option.id == option_id), None)
    if selected is None:
        raise ScenarioRunStateError(
            f"Option {option_id!r} is not valid for scene {scene.id!r}",
            path="option_id",
        )

    variables = _variable_lookup(run.content)
    updated_state = apply_state_changes(run.state, selected.state_changes, variables=variables)
    updated_flags = run.flags + tuple(selected.set_flags)
    record = ScenarioDecisionRecord(
        sequence_number=len(run.decisions) + 1,
        scene_id=scene.id,
        option_id=selected.id,
        domain_id=scene.domain_id,
        is_correct=selected.is_correct,
        next_scene=selected.next_scene,
        state_after=updated_state,
        flags_after=updated_flags,
    )
    decisions = run.decisions + (record,)

    if selected.next_scene == TERMINAL_SENTINEL:
        ending = evaluate_ending(run.content, updated_state)
        terminal_result = _build_terminal_result(
            run.content,
            state=updated_state,
            flags=updated_flags,
            decisions=decisions,
            ending=ending,
        )
        return ScenarioRunSnapshot(
            content=run.content,
            current_scene_id=None,
            state=updated_state,
            flags=updated_flags,
            decisions=decisions,
            is_complete=True,
            terminal_result=terminal_result,
        )

    scenes = _scene_map(run.content)
    if selected.next_scene not in scenes:
        raise ScenarioRunStateError(
            f"nextScene {selected.next_scene!r} does not resolve to an authored scene",
            path="nextScene",
        )

    return ScenarioRunSnapshot(
        content=run.content,
        current_scene_id=selected.next_scene,
        state=updated_state,
        flags=updated_flags,
        decisions=decisions,
        is_complete=False,
        terminal_result=None,
    )


def build_terminal_result(run: ScenarioRunSnapshot) -> ScenarioTerminalResult:
    """Enforce terminal completion and return the run's terminal result.

    This is the ONLY function in the engine that requires a run to be
    complete. `replay_scenario_run`/`resume_scenario_run` never enforce
    completion themselves — they are general reconstruction primitives
    that happily return a live, still-in-progress `ScenarioRunSnapshot`.
    The terminal result itself is actually computed eagerly inside
    `apply_decision` the moment a run reaches `EVALUATE_ENDING`, so this
    function's job is purely to enforce+extract that invariant, not to
    recompute anything.
    """
    if not run.is_complete or run.terminal_result is None:
        raise ScenarioEngineError("Cannot build a terminal result from an incomplete scenario run")
    return run.terminal_result


def decision_history(run: ScenarioRunSnapshot) -> tuple[ScenarioDecisionInput, ...]:
    """Return the ordered, sequence-numbered decision history for `run`.

    This is the canonical, engine-owned representation of "what a candidate
    chose, in order" — the only input a caller needs to retain (e.g. for a
    future persistence layer) to reconstruct or continue a run. Everything
    else about a run (state, flags, terminal result) is derived from this
    history plus the scenario content and is therefore never itself the
    source of truth. The result is directly accepted by
    `replay_scenario_run`/`resume_scenario_run`.
    """
    return tuple(
        ScenarioDecisionInput(
            sequence_number=decision.sequence_number,
            scene_id=decision.scene_id,
            option_id=decision.option_id,
        )
        for decision in run.decisions
    )


def _validate_decision_sequence(
    decisions: Sequence[ScenarioDecisionInput],
    *,
    path_prefix: str = "decisions",
) -> None:
    """Strictly validate that a decision history has no gaps, duplicates,
    or reordering, independent of scene-graph shape.

    Sequence numbers must be exactly 1, 2, 3, ... in list position — this
    is checked before any scene is resolved or any decision is applied, so
    a malformed history (e.g. a dropped row, a duplicated row, or rows
    returned out of order by a future persistence layer) is rejected
    deterministically rather than relying on it happening to also produce
    a scene-id mismatch downstream.
    """
    expected = 1
    seen: set[int] = set()
    for index, decision in enumerate(decisions):
        if decision.sequence_number in seen:
            raise ScenarioRunStateError(
                f"Duplicate sequenceNumber {decision.sequence_number} in decision history",
                path=f"{path_prefix}[{index}].sequenceNumber",
            )
        seen.add(decision.sequence_number)
        if decision.sequence_number != expected:
            raise ScenarioRunStateError(
                f"Expected sequenceNumber {expected} at position {index}, "
                f"got {decision.sequence_number} — decision history must be "
                "gap-free, duplicate-free, and in ascending order",
                path=f"{path_prefix}[{index}].sequenceNumber",
            )
        expected += 1


def replay_scenario_run(
    content: ScenarioContent,
    decisions: Sequence[ScenarioDecisionInput],
) -> ScenarioRunSnapshot:
    """The general, authoritative run-reconstruction primitive.

    Deterministically reconstructs a `ScenarioRunSnapshot` from an ordered,
    sequence-numbered decision history. Supports an empty history (returns
    the same result as `start_scenario_run`), a partial history (returns a
    live, still-in-progress, resumable snapshot), and a complete history
    (returns a snapshot with `is_complete=True` and a populated
    `terminal_result`) — it never requires or enforces terminal completion
    itself. Use `build_terminal_result(...)` when completion must be
    enforced.

    Replay trusts only each entry's `sequence_number`, `scene_id`, and
    `option_id`. Every derived value — state, flags, correctness, domain
    performance, current scene, completion status, and ending — is always
    recomputed from the scenario content via `apply_decision`, never taken
    from any external/serialized source.

    Validation, in order:
    1. `sequence_number`s must be gap-free, duplicate-free, and ascending
       (checked up front, before any scene is resolved).
    2. Each entry's `scene_id` must match the scene the run is actually
       sitting at when that entry is reached (rejects reordered/rewritten
       histories that happen to have valid sequence numbers).
    3. Each entry's `option_id` must be a valid option for that scene
       (enforced by `apply_decision`).
    4. No decision may be supplied once a prior decision already reached
       `EVALUATE_ENDING` (enforced by `apply_decision`).
    """
    _validate_decision_sequence(decisions)
    run = start_scenario_run(content)
    for decision in decisions:
        if run.current_scene_id != decision.scene_id:
            raise ScenarioRunStateError(
                f"Expected current scene {run.current_scene_id!r}, got replay "
                f"step for {decision.scene_id!r} at sequenceNumber {decision.sequence_number}",
                path=f"decisions[{decision.sequence_number - 1}].sceneId",
            )
        run = apply_decision(run, decision.option_id)
    return run


def resume_scenario_run(
    content: ScenarioContent,
    decisions: Sequence[ScenarioDecisionInput],
) -> ScenarioRunSnapshot:
    """Documented alias for `replay_scenario_run(...)`.

    Kept as a distinct name for callers whose intent is specifically
    "resume an in-progress run from a persisted decision history" — but it
    performs no logic of its own and must never diverge from
    `replay_scenario_run`; the two names share exactly one implementation.
    """
    return replay_scenario_run(content, decisions)


def replay_matches_run(
    content: ScenarioContent,
    run: ScenarioRunSnapshot,
) -> ScenarioTerminalResult:
    if not run.is_complete or run.terminal_result is None:
        raise ScenarioRunStateError("Cannot replay a run that is not complete", path="is_complete")
    replayed_run = replay_scenario_run(content, decision_history(run))
    replayed_result = build_terminal_result(replayed_run)
    if replayed_result != run.terminal_result:
        raise ScenarioEngineError("Replayed terminal result does not match the original run")
    return replayed_result


_DECISION_HISTORY_ENTRY_FIELDS = frozenset({"sequenceNumber", "sceneId", "optionId"})


def _parse_decision_history_entry(entry: object, index: int, *, path_prefix: str) -> ScenarioDecisionInput:
    """Strictly parse a single decoded `decisionHistory` entry.

    Internal single-record parser used by `deserialize_decision_history`.
    Not part of the public API — callers that need to deserialize a full
    history must go through `deserialize_decision_history`, which also
    enforces cross-entry sequence-ordering invariants that a single entry
    cannot validate on its own.
    """
    entry_path = f"{path_prefix}[{index}]"
    if not isinstance(entry, Mapping):
        raise ScenarioRunStateError(
            f"{entry_path} must be a JSON object",
            path=entry_path,
        )

    entry_keys = set(entry.keys())
    missing = _DECISION_HISTORY_ENTRY_FIELDS - entry_keys
    if missing:
        raise ScenarioRunStateError(
            f"{entry_path} is missing required field(s): {sorted(missing)}",
            path=entry_path,
        )
    extra = entry_keys - _DECISION_HISTORY_ENTRY_FIELDS
    if extra:
        raise ScenarioRunStateError(
            f"{entry_path} has unexpected field(s): {sorted(extra)}",
            path=entry_path,
        )

    sequence_number = entry["sequenceNumber"]
    if isinstance(sequence_number, bool) or not isinstance(sequence_number, int):
        raise ScenarioRunStateError(
            f"{entry_path}.sequenceNumber must be an integer",
            path=f"{entry_path}.sequenceNumber",
        )
    if sequence_number < 1:
        raise ScenarioRunStateError(
            f"{entry_path}.sequenceNumber must be >= 1",
            path=f"{entry_path}.sequenceNumber",
        )

    scene_id = entry["sceneId"]
    if not isinstance(scene_id, str) or not scene_id.strip():
        raise ScenarioRunStateError(
            f"{entry_path}.sceneId must be a non-empty, non-whitespace string",
            path=f"{entry_path}.sceneId",
        )

    option_id = entry["optionId"]
    if not isinstance(option_id, str) or not option_id.strip():
        raise ScenarioRunStateError(
            f"{entry_path}.optionId must be a non-empty, non-whitespace string",
            path=f"{entry_path}.optionId",
        )

    return ScenarioDecisionInput(sequence_number=sequence_number, scene_id=scene_id, option_id=option_id)


def deserialize_decision_history(value: object) -> tuple[ScenarioDecisionInput, ...]:
    """Strictly parse a JSON-decoded `decisionHistory` payload.

    Accepts only a list of objects, each containing exactly
    `sequenceNumber` (a non-boolean integer >= 1), `sceneId` (a non-empty,
    non-whitespace string), and `optionId` (a non-empty, non-whitespace
    string) — no missing or extra fields. The list is never sorted or
    otherwise normalized: sequence numbers must already be gap-free,
    duplicate-free, and strictly ascending in list order, or the entire
    history is rejected. This is the strict deserialization half of the
    replay contract; the serialization half is `serialize_run_snapshot`'s
    `decisionHistory` field.
    """
    if not isinstance(value, list):
        raise ScenarioRunStateError(
            "decisionHistory must be a JSON array",
            path="decisionHistory",
        )

    parsed = tuple(
        _parse_decision_history_entry(entry, index, path_prefix="decisionHistory")
        for index, entry in enumerate(value)
    )
    _validate_decision_sequence(parsed, path_prefix="decisionHistory")
    return parsed


def _verify_serialized_identity(content: ScenarioContent, payload: Mapping[str, object]) -> None:
    """Verify a serialized run payload's identity fields against `content`.

    Runtime snapshots and terminal results must remain bound to the exact
    `simulationId`, scenario `version`, `canonicalContentSha256`, and
    `engineVersion` they were produced under — a history serialized
    against a different content version, a different content hash (even
    under the same simulationId/version), or a different engine version
    must never be silently replayed as if it belonged to `content`.
    """
    expected = {
        "simulationId": content.simulation_id,
        "version": content.version,
        "canonicalContentSha256": content.canonical_content_sha256,
        "engineVersion": ENGINE_VERSION,
    }
    for field, expected_value in expected.items():
        if field not in payload:
            raise ScenarioReplayIdentityError(
                f"Serialized run payload is missing required identity field {field!r}"
            )
        actual_value = payload[field]
        if actual_value != expected_value:
            raise ScenarioReplayIdentityError(
                f"Serialized run payload {field}={actual_value!r} does not match "
                f"content {field}={expected_value!r}"
            )


def replay_serialized_run(
    content: ScenarioContent,
    payload: Mapping[str, object],
) -> ScenarioRunSnapshot:
    """Reconstruct a run strictly from a serialized payload's decision history.

    Uses the exact field names emitted by `serialize_run_snapshot`. Only
    `decisionHistory` is trusted for reconstruction. Every other field in
    `payload` — `state`, `flags`, `currentSceneId`, `isComplete`,
    `terminalResult` (including its nested `domainPerformance`/`endingId`)
    — is ignored for reconstruction purposes: they may be present in
    `payload` for display or debugging, but the engine always recomputes
    them from `content` and the ordered decisions, never from what was
    serialized. This makes tampering with any derived field in a
    persisted payload a no-op from replay's perspective.

    Identity fields (`simulationId`, `version`, `canonicalContentSha256`,
    `engineVersion`) are verified first via `_verify_serialized_identity`,
    and a mismatch is rejected with `ScenarioReplayIdentityError` before
    any decision is replayed.
    """
    _verify_serialized_identity(content, payload)
    history = deserialize_decision_history(payload.get("decisionHistory"))
    return replay_scenario_run(content, history)


def _serialize_decision_record(record: ScenarioDecisionRecord) -> dict:
    return {
        "sequenceNumber": record.sequence_number,
        "sceneId": record.scene_id,
        "optionId": record.option_id,
        "domainId": record.domain_id,
        "isCorrect": record.is_correct,
        "nextScene": record.next_scene,
        "stateAfter": dict(record.state_after),
        "flagsAfter": list(record.flags_after),
    }


def _serialize_domain_performance(snapshot: DomainPerformanceSnapshot) -> dict:
    return {
        "domainId": snapshot.domain_id,
        "correctCount": snapshot.correct_count,
        "totalCount": snapshot.total_count,
        "accuracy": snapshot.accuracy,
    }


def serialize_terminal_result(result: ScenarioTerminalResult) -> dict:
    """Convert a terminal result into a plain, JSON-safe, fully-detached dict.

    This is a serialization contract only — it performs no I/O and knows
    nothing about any storage backend. Every value is a fresh `dict`/`list`
    built via `dict(...)`/`list(...)` (never the runtime's own immutable
    mappings/tuples), so mutating the returned payload can never mutate the
    `ScenarioTerminalResult` it was built from.
    """
    return {
        "endingId": result.ending_id,
        "scoreBand": result.score_band,
        "narrative": result.narrative,
        "recommendedReview": list(result.recommended_review),
        "finalState": dict(result.final_state),
        "flags": list(result.flags),
        "decisions": [_serialize_decision_record(decision) for decision in result.decisions],
        "domainPerformance": [
            _serialize_domain_performance(snapshot) for snapshot in result.domain_performance
        ],
        "engineVersion": result.engine_version,
        "canonicalContentSha256": result.canonical_content_sha256,
    }


def _serialize_decision_input(decision: ScenarioDecisionInput) -> dict:
    return {
        "sequenceNumber": decision.sequence_number,
        "sceneId": decision.scene_id,
        "optionId": decision.option_id,
    }


def serialize_run_snapshot(run: ScenarioRunSnapshot) -> dict:
    """Convert a run snapshot into a plain, JSON-safe, fully-detached dict.

    Deliberately omits the `content` field (the full scenario document) —
    callers are expected to identify the content out-of-band via the
    `simulationId`/`version`/`canonicalContentSha256` identity fields and
    reload it through `utils/scenario_catalog.py` rather than persisting
    the document itself alongside every run.

    `decisionHistory` entries carry an explicit `sequenceNumber` (not just
    position in the list) so a future persistence layer can round-trip
    through `deserialize_decision_history` and still have
    `replay_scenario_run`/`replay_serialized_run` strictly validate
    ordering. `simulationId`, `version`, `canonicalContentSha256`, and
    `engineVersion` together form the identity contract enforced by
    `replay_serialized_run` — every other field (`state`, `flags`,
    `currentSceneId`, `isComplete`, `terminalResult`) is derived/display
    data only and is never trusted by replay.

    Every value here is a fresh `dict`/`list`, so mutating the returned
    payload can never mutate the `ScenarioRunSnapshot` it was built from.
    """
    return {
        "simulationId": run.content.simulation_id,
        "version": run.content.version,
        "canonicalContentSha256": run.content.canonical_content_sha256,
        "engineVersion": ENGINE_VERSION,
        "currentSceneId": run.current_scene_id,
        "state": dict(run.state),
        "flags": list(run.flags),
        "decisionHistory": [_serialize_decision_input(decision) for decision in decision_history(run)],
        "isComplete": run.is_complete,
        "terminalResult": (
            serialize_terminal_result(run.terminal_result) if run.terminal_result is not None else None
        ),
    }
