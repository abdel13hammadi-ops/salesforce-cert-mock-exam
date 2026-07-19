"""Scenario Simulator content loading, schema validation, and graph analysis."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
except ImportError:  # pragma: no cover - exercised when dependency is missing
    Draft202012Validator = None  # type: ignore[assignment,misc]
    JsonSchemaValidationError = Exception  # type: ignore[assignment,misc]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_VERSION = "1.0.0"
TERMINAL_SENTINEL = "EVALUATE_ENDING"

_SCHEMA_CACHE: dict[str, Mapping[str, Any]] = {}


class ScenarioContentError(Exception):
    """Base error for scenario content loading and validation."""


class ScenarioValidationError(ScenarioContentError):
    """Raised when scenario content fails schema or custom validation."""

    def __init__(self, message: str, *, path: str = "") -> None:
        self.path = path
        super().__init__(message if not path else f"{path}: {message}")


@dataclass(frozen=True)
class ScenarioGraphMetadata:
    authored_scene_count: int
    reachable_scene_count: int
    unreachable_scene_ids: tuple[str, ...]
    minimum_path_length: int
    maximum_path_length: int


@dataclass(frozen=True)
class ScenarioStructureCounts:
    choice_count: int
    detour_count: int
    domain_count: int
    ending_count: int


@dataclass(frozen=True)
class ScenarioOption:
    id: str
    text: str
    is_correct: bool
    feedback: str
    next_scene: str
    state_changes: Mapping[str, float]
    set_flags: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioDecision:
    prompt: str
    decision_type: str
    options: tuple[ScenarioOption, ...]


@dataclass(frozen=True)
class ScenarioScene:
    id: str
    domain_id: str
    narrative: str
    decision: ScenarioDecision
    is_detour: bool = False
    explanation: str | None = None


@dataclass(frozen=True)
class ScenarioDomain:
    id: str
    label: str
    weight: str


@dataclass(frozen=True)
class ScenarioStateVariable:
    key: str
    minimum: float | None = None
    maximum: float | None = None
    description: str | None = None


@dataclass(frozen=True)
class ScenarioEnding:
    id: str
    condition: Mapping[str, float]
    narrative: str
    score_band: str
    recommended_review: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioContent:
    simulation_id: str
    version: str
    schema_version: str
    certification_exam_name: str
    exam_code: str
    title: str
    description: str | None
    estimated_minutes: int | None
    domains: tuple[ScenarioDomain, ...]
    state_variables: tuple[ScenarioStateVariable, ...]
    initial_state: Mapping[str, float]
    scenes: tuple[ScenarioScene, ...]
    start_scene: str
    endings: tuple[ScenarioEnding, ...]
    graph_metadata: ScenarioGraphMetadata
    structure_counts: ScenarioStructureCounts
    canonical_content_sha256: str
    source_path: Path | None = None


def scenario_content_root(content_root: Path | None = None) -> Path:
    root = content_root or (REPO_ROOT / "scenario_content")
    return root.resolve()


def schema_path_for_version(schema_version: str, *, content_root: Path | None = None) -> Path:
    return scenario_content_root(content_root) / "schemas" / schema_version / "simulation.schema.json"


def load_json_document(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioContentError(f"Unable to read scenario content at {path}") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScenarioContentError(f"Malformed JSON in {path}: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ScenarioContentError(f"Scenario content root must be a JSON object: {path}")
    return parsed


def load_schema(schema_version: str = DEFAULT_SCHEMA_VERSION, *, content_root: Path | None = None) -> Mapping[str, Any]:
    if schema_version in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[schema_version]
    path = schema_path_for_version(schema_version, content_root=content_root)
    if not path.is_file():
        raise ScenarioContentError(f"Scenario schema not found for version {schema_version!r}: {path}")
    document = load_json_document(path)
    _SCHEMA_CACHE[schema_version] = document
    return document


def compute_canonical_content_sha256(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        document,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


_REQUIRED_PROPERTY_MESSAGE_RE = re.compile(r"^'([^']+)' is a required property$")


def _jsonschema_error_path(error: JsonSchemaValidationError) -> str:
    if error.absolute_path:
        return ".".join(str(part) for part in error.absolute_path)

    if error.validator == "required":
        match = _REQUIRED_PROPERTY_MESSAGE_RE.match(error.message)
        if match:
            return match.group(1)
        instance = error.instance
        if isinstance(instance, Mapping):
            required_keys = error.validator_value
            if isinstance(required_keys, (list, tuple)):
                for key in required_keys:
                    if key not in instance:
                        return str(key)

    return "$"


def _format_jsonschema_error(error: JsonSchemaValidationError) -> str:
    path = _jsonschema_error_path(error)
    return error.message if path == "$" else f"{path}: {error.message}"


def validate_json_schema(document: Mapping[str, Any], *, schema_version: str) -> None:
    if Draft202012Validator is None:
        raise ScenarioContentError(
            "jsonschema is required for scenario validation but is not installed"
        )
    schema = load_schema(schema_version)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda err: list(err.absolute_path))
    if errors:
        first = errors[0]
        raise ScenarioValidationError(
            _format_jsonschema_error(first),
            path=_jsonschema_error_path(first),
        )


def _state_variable_keys(document: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for entry in document.get("stateVariables") or []:
        if isinstance(entry, Mapping):
            key = str(entry.get("key") or "").strip()
            if key:
                keys.add(key)
    return keys


def _scene_by_id(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    scenes = document.get("scenes") or []
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, scene in enumerate(scenes):
        if not isinstance(scene, Mapping):
            raise ScenarioValidationError("scene must be an object", path=f"scenes[{index}]")
        scene_id = str(scene.get("id") or "").strip()
        if not scene_id:
            raise ScenarioValidationError("scene id is required", path=f"scenes[{index}].id")
        if scene_id in by_id:
            raise ScenarioValidationError(f"duplicate scene id {scene_id!r}", path=f"scenes[{index}].id")
        by_id[scene_id] = scene
    return by_id


def _validate_state_variable_references(document: Mapping[str, Any]) -> None:
    declared = _state_variable_keys(document)
    if not declared:
        raise ScenarioValidationError("stateVariables must declare at least one key", path="stateVariables")

    initial_state = document.get("initialState") or {}
    if not isinstance(initial_state, Mapping):
        raise ScenarioValidationError("initialState must be an object", path="initialState")
    for key in initial_state:
        if key not in declared:
            raise ScenarioValidationError(
                f"initialState key {key!r} is not declared in stateVariables",
                path=f"initialState.{key}",
            )

    scenes = document.get("scenes") or []
    for scene_index, scene in enumerate(scenes):
        options = ((scene.get("decision") or {}).get("options") or [])
        for option_index, option in enumerate(options):
            state_changes = option.get("stateChanges") or {}
            if not isinstance(state_changes, Mapping):
                continue
            for key in state_changes:
                if key not in declared:
                    raise ScenarioValidationError(
                        f"stateChanges key {key!r} is not declared in stateVariables",
                        path=(
                            f"scenes[{scene_index}].decision.options[{option_index}]"
                            f".stateChanges.{key}"
                        ),
                    )


def _validate_choice_ids(document: Mapping[str, Any]) -> None:
    scenes = document.get("scenes") or []
    for scene_index, scene in enumerate(scenes):
        options = ((scene.get("decision") or {}).get("options") or [])
        seen: set[str] = set()
        for option_index, option in enumerate(options):
            option_id = str(option.get("id") or "").strip()
            if not option_id:
                raise ScenarioValidationError(
                    "option id is required",
                    path=f"scenes[{scene_index}].decision.options[{option_index}].id",
                )
            if option_id in seen:
                raise ScenarioValidationError(
                    f"duplicate option id {option_id!r} within scene",
                    path=f"scenes[{scene_index}].decision.options[{option_index}].id",
                )
            seen.add(option_id)


def _validate_transitions(document: Mapping[str, Any], scene_ids: set[str]) -> None:
    start_scene = str(document.get("startScene") or "").strip()
    if start_scene not in scene_ids:
        raise ScenarioValidationError(
            f"startScene {start_scene!r} does not resolve to an authored scene",
            path="startScene",
        )

    scenes = document.get("scenes") or []
    for scene_index, scene in enumerate(scenes):
        options = ((scene.get("decision") or {}).get("options") or [])
        for option_index, option in enumerate(options):
            next_scene = str(option.get("nextScene") or "").strip()
            path = f"scenes[{scene_index}].decision.options[{option_index}].nextScene"
            if not next_scene:
                raise ScenarioValidationError("nextScene is required", path=path)
            if next_scene == TERMINAL_SENTINEL:
                continue
            if next_scene not in scene_ids:
                raise ScenarioValidationError(
                    f"nextScene {next_scene!r} does not resolve to an authored scene or {TERMINAL_SENTINEL}",
                    path=path,
                )


def _validate_domain_references(document: Mapping[str, Any], scene_by_id: Mapping[str, Mapping[str, Any]]) -> None:
    domains = document.get("domains") or []
    domain_ids = {
        str(domain.get("id") or "").strip()
        for domain in domains
        if isinstance(domain, Mapping) and str(domain.get("id") or "").strip()
    }
    if not domain_ids:
        return
    for scene_id, scene in scene_by_id.items():
        domain_id = str(scene.get("domainId") or "").strip()
        if domain_id not in domain_ids:
            raise ScenarioValidationError(
                f"domainId {domain_id!r} is not declared in domains",
                path=f"scenes[{scene_id}].domainId",
            )
    for ending_index, ending in enumerate(document.get("endings") or []):
        for domain_id in ending.get("recommendedReview") or []:
            domain_id_text = str(domain_id or "").strip()
            if domain_id_text and domain_id_text not in domain_ids:
                raise ScenarioValidationError(
                    f"recommendedReview domain id {domain_id_text!r} is not declared in domains",
                    path=f"endings[{ending_index}].recommendedReview",
                )


def _validate_ending_conditions(document: Mapping[str, Any]) -> None:
    declared = _state_variable_keys(document)
    for ending_index, ending in enumerate(document.get("endings") or []):
        condition = ending.get("condition") or {}
        if not isinstance(condition, Mapping):
            raise ScenarioValidationError("ending condition must be an object", path=f"endings[{ending_index}].condition")
        for key in condition:
            base_key = _ending_condition_base_key(key)
            if base_key not in declared:
                raise ScenarioValidationError(
                    f"ending condition key {key!r} references undeclared state variable {base_key!r}",
                    path=f"endings[{ending_index}].condition.{key}",
                )


def _ending_condition_base_key(condition_key: str) -> str:
    for suffix in ("Min", "Max", "Equals"):
        if condition_key.endswith(suffix):
            return condition_key[: -len(suffix)]
    return condition_key


def _build_adjacency(scene_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {scene_id: set() for scene_id in scene_by_id}
    for scene_id, scene in scene_by_id.items():
        options = ((scene.get("decision") or {}).get("options") or [])
        for option in options:
            next_scene = str(option.get("nextScene") or "").strip()
            if next_scene == TERMINAL_SENTINEL:
                continue
            adjacency[scene_id].add(next_scene)
    return adjacency


def _detect_cycle(scene_ids: set[str], adjacency: Mapping[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ScenarioValidationError(f"cycle detected involving scene {node!r}", path=f"scenes.{node}")
        if node in visited:
            return
        visiting.add(node)
        for neighbor in adjacency.get(node, set()):
            visit(neighbor)
        visiting.remove(node)
        visited.add(node)

    for scene_id in scene_ids:
        if scene_id not in visited:
            visit(scene_id)


def _reachable_scene_ids(start_scene: str, adjacency: Mapping[str, set[str]]) -> set[str]:
    reachable = {start_scene}
    queue = deque([start_scene])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency.get(current, set()):
            if neighbor not in reachable:
                reachable.add(neighbor)
                queue.append(neighbor)
    return reachable


def _path_length_bounds(
    scene_by_id: Mapping[str, Mapping[str, Any]],
    start_scene: str,
) -> tuple[int, int]:
    """Return min/max attempt path lengths using memoized DAG analysis.

    Path length counts scenes visited before reaching ``EVALUATE_ENDING``.
    Reconvergent branches share subproblem results instead of re-enumerating
    every distinct root-to-leaf path (which is exponential on branching graphs).
    """
    memo: dict[str, tuple[int, int]] = {}

    def bounds(scene_id: str) -> tuple[int, int]:
        cached = memo.get(scene_id)
        if cached is not None:
            return cached

        scene = scene_by_id[scene_id]
        options = ((scene.get("decision") or {}).get("options") or [])
        if not options:
            raise ScenarioValidationError(
                f"scene {scene_id!r} has no options and cannot terminate",
                path=f"scenes.{scene_id}.decision.options",
            )

        mins: list[int] = []
        maxs: list[int] = []
        for option in options:
            next_scene = str(option.get("nextScene") or "").strip()
            if next_scene == TERMINAL_SENTINEL:
                mins.append(1)
                maxs.append(1)
            else:
                child_min, child_max = bounds(next_scene)
                mins.append(1 + child_min)
                maxs.append(1 + child_max)

        result = (min(mins), max(maxs))
        memo[scene_id] = result
        return result

    return bounds(start_scene)


def _compute_graph_metadata_from_context(
    scene_by_id: Mapping[str, Mapping[str, Any]],
    *,
    start_scene: str,
    adjacency: Mapping[str, set[str]],
) -> ScenarioGraphMetadata:
    scene_ids = set(scene_by_id)
    reachable = _reachable_scene_ids(start_scene, adjacency)
    unreachable = tuple(sorted(scene_ids - reachable))
    minimum_path_length, maximum_path_length = _path_length_bounds(scene_by_id, start_scene)
    return ScenarioGraphMetadata(
        authored_scene_count=len(scene_ids),
        reachable_scene_count=len(reachable),
        unreachable_scene_ids=unreachable,
        minimum_path_length=minimum_path_length,
        maximum_path_length=maximum_path_length,
    )


def _validate_and_compute_graph_metadata(
    document: Mapping[str, Any],
    *,
    schema_version: str | None = None,
) -> ScenarioGraphMetadata:
    resolved_schema_version = schema_version or str(document.get("schemaVersion") or DEFAULT_SCHEMA_VERSION)
    validate_json_schema(document, schema_version=resolved_schema_version)

    scene_by_id = _scene_by_id(document)
    scene_ids = set(scene_by_id)
    start_scene = str(document.get("startScene") or "").strip()
    _validate_state_variable_references(document)
    _validate_choice_ids(document)
    _validate_transitions(document, scene_ids)
    _validate_domain_references(document, scene_by_id)
    _validate_ending_conditions(document)

    adjacency = _build_adjacency(scene_by_id)
    _detect_cycle(scene_ids, adjacency)

    graph_metadata = _compute_graph_metadata_from_context(
        scene_by_id,
        start_scene=start_scene,
        adjacency=adjacency,
    )
    if graph_metadata.unreachable_scene_ids:
        raise ScenarioValidationError(
            "all authored scenes must be reachable in V1: "
            + ", ".join(graph_metadata.unreachable_scene_ids),
            path="scenes",
        )
    return graph_metadata


def compute_graph_metadata(document: Mapping[str, Any]) -> ScenarioGraphMetadata:
    scene_by_id = _scene_by_id(document)
    start_scene = str(document.get("startScene") or "").strip()
    adjacency = _build_adjacency(scene_by_id)
    return _compute_graph_metadata_from_context(
        scene_by_id,
        start_scene=start_scene,
        adjacency=adjacency,
    )


def compute_structure_counts(document: Mapping[str, Any]) -> ScenarioStructureCounts:
    scenes = document.get("scenes") or []
    choice_count = 0
    detour_count = 0
    for scene in scenes:
        if bool(scene.get("isDetour")):
            detour_count += 1
        choice_count += len(((scene.get("decision") or {}).get("options") or []))
    domains = document.get("domains") or []
    endings = document.get("endings") or []
    return ScenarioStructureCounts(
        choice_count=choice_count,
        detour_count=detour_count,
        domain_count=len(domains),
        ending_count=len(endings),
    )


def validate_scenario_document(
    document: Mapping[str, Any],
    *,
    schema_version: str | None = None,
) -> None:
    _validate_and_compute_graph_metadata(document, schema_version=schema_version)


def _parse_domains(document: Mapping[str, Any]) -> tuple[ScenarioDomain, ...]:
    domains: list[ScenarioDomain] = []
    for domain in document.get("domains") or []:
        if not isinstance(domain, Mapping):
            continue
        domains.append(
            ScenarioDomain(
                id=str(domain.get("id") or ""),
                label=str(domain.get("label") or ""),
                weight=str(domain.get("weight") or ""),
            )
        )
    return tuple(domains)


def _parse_state_variables(document: Mapping[str, Any]) -> tuple[ScenarioStateVariable, ...]:
    variables: list[ScenarioStateVariable] = []
    for entry in document.get("stateVariables") or []:
        if not isinstance(entry, Mapping):
            continue
        minimum = entry.get("minimum")
        maximum = entry.get("maximum")
        variables.append(
            ScenarioStateVariable(
                key=str(entry.get("key") or ""),
                minimum=float(minimum) if minimum is not None else None,
                maximum=float(maximum) if maximum is not None else None,
                description=str(entry.get("description") or "") or None,
            )
        )
    return tuple(variables)


def _parse_scenes(document: Mapping[str, Any]) -> tuple[ScenarioScene, ...]:
    scenes: list[ScenarioScene] = []
    for scene in document.get("scenes") or []:
        if not isinstance(scene, Mapping):
            continue
        options: list[ScenarioOption] = []
        decision = scene.get("decision") or {}
        for option in decision.get("options") or []:
            state_changes_raw = option.get("stateChanges") or {}
            state_changes = {
                str(key): float(value)
                for key, value in state_changes_raw.items()
                if isinstance(value, (int, float))
            }
            options.append(
                ScenarioOption(
                    id=str(option.get("id") or ""),
                    text=str(option.get("text") or ""),
                    is_correct=bool(option.get("isCorrect")),
                    feedback=str(option.get("feedback") or ""),
                    next_scene=str(option.get("nextScene") or ""),
                    state_changes=state_changes,
                    set_flags=tuple(str(flag) for flag in (option.get("setFlags") or [])),
                )
            )
        scenes.append(
            ScenarioScene(
                id=str(scene.get("id") or ""),
                domain_id=str(scene.get("domainId") or ""),
                narrative=str(scene.get("narrative") or ""),
                decision=ScenarioDecision(
                    prompt=str(decision.get("prompt") or ""),
                    decision_type=str(decision.get("type") or "single_select"),
                    options=tuple(options),
                ),
                is_detour=bool(scene.get("isDetour")),
                explanation=str(scene.get("explanation") or "") or None,
            )
        )
    return tuple(scenes)


def _parse_endings(document: Mapping[str, Any]) -> tuple[ScenarioEnding, ...]:
    endings: list[ScenarioEnding] = []
    for ending in document.get("endings") or []:
        if not isinstance(ending, Mapping):
            continue
        condition_raw = ending.get("condition") or {}
        condition = {
            str(key): float(value)
            for key, value in condition_raw.items()
            if isinstance(value, (int, float))
        }
        endings.append(
            ScenarioEnding(
                id=str(ending.get("id") or ""),
                condition=condition,
                narrative=str(ending.get("narrative") or ""),
                score_band=str(ending.get("scoreBand") or ""),
                recommended_review=tuple(str(domain_id) for domain_id in (ending.get("recommendedReview") or [])),
            )
        )
    return tuple(endings)


def build_scenario_content(
    document: Mapping[str, Any],
    *,
    source_path: Path | None = None,
    schema_version: str | None = None,
) -> ScenarioContent:
    graph_metadata = _validate_and_compute_graph_metadata(document, schema_version=schema_version)
    initial_state_raw = document.get("initialState") or {}
    initial_state = {
        str(key): float(value)
        for key, value in initial_state_raw.items()
        if isinstance(value, (int, float))
    }
    estimated_minutes = document.get("estimatedMinutes")
    return ScenarioContent(
        simulation_id=str(document.get("simulationId") or ""),
        version=str(document.get("version") or ""),
        schema_version=str(document.get("schemaVersion") or schema_version or DEFAULT_SCHEMA_VERSION),
        certification_exam_name=str(document.get("certificationExamName") or ""),
        exam_code=str(document.get("examCode") or ""),
        title=str(document.get("title") or ""),
        description=str(document.get("description") or "") or None,
        estimated_minutes=int(estimated_minutes) if isinstance(estimated_minutes, int) else None,
        domains=_parse_domains(document),
        state_variables=_parse_state_variables(document),
        initial_state=initial_state,
        scenes=_parse_scenes(document),
        start_scene=str(document.get("startScene") or ""),
        endings=_parse_endings(document),
        graph_metadata=graph_metadata,
        structure_counts=compute_structure_counts(document),
        canonical_content_sha256=compute_canonical_content_sha256(document),
        source_path=source_path.resolve() if source_path else None,
    )


def load_scenario_content(
    path: Path,
    *,
    schema_version: str | None = None,
) -> ScenarioContent:
    resolved = path.resolve()
    document = load_json_document(resolved)
    return build_scenario_content(document, source_path=resolved, schema_version=schema_version)
