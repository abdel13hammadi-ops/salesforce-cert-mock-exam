"""Layered custom validation for CertBound Scenario Simulator schema 1.1.0."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
except ImportError:  # pragma: no cover - exercised when dependency is missing
    Draft202012Validator = None  # type: ignore[assignment,misc]
    JsonSchemaValidationError = Exception  # type: ignore[assignment,misc]

from utils.scenario_validation_findings import (
    ValidationFinding,
    findings_contain_blocking,
    sort_validation_findings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TERMINAL_SENTINEL = "EVALUATE_ENDING"
SCHEMA_VERSION_1_1 = "1.1.0"
SUPPORTED_ENGINE_VERSIONS_V1_1 = frozenset({"SCENARIO_ENGINE_V2"})

_WEIGHT_SUM_TOLERANCE = 1e-9
_MAX_CONDITION_DEPTH = 8
_MAX_CONDITION_NODES = 64
_MAX_REACHABILITY_STATES = 5000
_DEFAULT_MAX_SCORED_DECISIONS = 15
_TIER_ORDER = ("optimal", "acceptable", "suboptimal", "high-risk")
_REQUIRED_PROPERTY_MESSAGE_RE = re.compile(r"^'([^']+)' is a required property$")
_EXECUTABLE_CODE_RE = re.compile(
    r"(?i)(eval\s*\(|Function\s*\(|new\s+Function|exec\s*\(|__import__|subprocess)"
)

_SCHEMA_CACHE: dict[str, Mapping[str, Any]] = {}


def load_json_document(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Unable to read scenario content at {path}") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON in {path}: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Scenario content root must be a JSON object: {path}")
    return parsed


def _schema_path_v1_1() -> Path:
    return REPO_ROOT / "scenario_content" / "schemas" / SCHEMA_VERSION_1_1 / "simulation.schema.json"


def _load_v1_1_schema() -> Mapping[str, Any]:
    if SCHEMA_VERSION_1_1 not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[SCHEMA_VERSION_1_1] = load_json_document(_schema_path_v1_1())
    return _SCHEMA_CACHE[SCHEMA_VERSION_1_1]


def _finding(
    rule_id: str,
    layer: str,
    severity: str,
    path: str,
    message: str,
    *,
    identifier: str | None = None,
) -> ValidationFinding:
    return ValidationFinding(
        rule_id=rule_id,
        layer=layer,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        path=path,
        message=message,
        identifier=identifier,
    )


def _json_pointer(*segments: str | int) -> str:
    encoded: list[str] = []
    for segment in segments:
        text = str(segment)
        encoded.append(text.replace("~", "~0").replace("/", "~1"))
    return "/" + "/".join(encoded) if encoded else "/"


def _json_pointer_from_parts(parts: Iterable[Any]) -> str:
    return _json_pointer(*parts)


def _jsonschema_error_pointer(error: JsonSchemaValidationError) -> str:
    if error.absolute_path:
        return _json_pointer_from_parts(error.absolute_path)

    if error.validator == "required":
        match = _REQUIRED_PROPERTY_MESSAGE_RE.match(error.message)
        if match:
            parent = _json_pointer_from_parts(error.absolute_path)
            missing = match.group(1)
            return f"{parent}/{missing}" if parent != "/" else _json_pointer(missing)
        instance = error.instance
        required_keys = error.validator_value
        if isinstance(instance, Mapping) and isinstance(required_keys, (list, tuple)):
            for key in required_keys:
                if key not in instance:
                    parent = _json_pointer_from_parts(error.absolute_path)
                    return f"{parent}/{key}" if parent != "/" else _json_pointer(key)

    return "/"


def _map_jsonschema_error(error: JsonSchemaValidationError) -> ValidationFinding:
    path = _jsonschema_error_pointer(error)
    message = error.message
    validator = error.validator or ""
    instance_path = list(error.absolute_path)

    if path == "/schemaVersion" or path.endswith("/schemaVersion"):
        return _finding("CV-001", "json_schema", "blocker", path, message, identifier="schemaVersion")
    if path == "/endings" or instance_path == ["endings"] or "/endings" in path:
        return _finding("CV-002", "json_schema", "blocker", path, message)
    if path.endswith("/nextScene") or "nextScene" in path.split("/"):
        return _finding("CV-003", "json_schema", "blocker", path, message)
    if path.endswith("/isCorrect") or "isCorrect" in path.split("/"):
        return _finding("CV-004", "json_schema", "blocker", path, message)
    if "/narrative" in path and "/scenes/" in path and "/outcomes/" not in path:
        return _finding("CV-005", "json_schema", "blocker", path, message)
    if "optionTierInCurrentDecision" in path.split("/"):
        return _finding("CV-006", "json_schema", "blocker", path, message)
    if path.endswith("/all") or path.endswith("/any"):
        return _finding("CV-030", "json_schema", "blocker", path, message)
    if path.endswith("/not") or "/when/not" in path:
        return _finding("CV-031", "json_schema", "blocker", path, message)
    if "/charactersPresent/" in path and validator in {"not", "enum", "const"}:
        return _finding("CV-041", "json_schema", "blocker", path, message)
    if path.endswith("/learnerPresent"):
        return _finding("CV-042", "json_schema", "blocker", path, message)
    if "/routing/" in path:
        if "EVALUATE_ENDING" in message or validator == "const":
            if "terminal" in message.lower() or "correctiveRoute" in message:
                return _finding("CV-061", "json_schema", "blocker", path, message)
            return _finding("CV-062", "json_schema", "blocker", path, message)
        if "correctiveRoute" in path or "correctiveRoute" in message:
            return _finding("CV-061", "json_schema", "blocker", path, message)

    return _finding("JS-SCHEMA", "json_schema", "blocker", path, message)


def collect_v1_1_json_schema_findings(document: Mapping[str, Any]) -> list[ValidationFinding]:
    if Draft202012Validator is None:
        return [
            _finding(
                "JS-SCHEMA",
                "json_schema",
                "blocker",
                "/",
                "jsonschema is required for scenario validation but is not installed",
            )
        ]
    if not isinstance(document, Mapping):
        return [
            _finding(
                "JS-SCHEMA",
                "json_schema",
                "blocker",
                "/",
                "scenario document root must be a JSON object",
            )
        ]

    schema = _load_v1_1_schema()
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda err: _jsonschema_error_pointer(err))
    return [_map_jsonschema_error(error) for error in errors]


def compute_canonical_content_sha256_v1_1(document: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(document))
    for excluded in ("canonicalContentSha256", "contentProvenance", "publicationMetadata"):
        payload.pop(excluded, None)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _as_sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _non_empty(value: Any) -> str:
    return str(value or "").strip()


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _finite_number_finding(path: str, message: str, *, identifier: str | None = None) -> ValidationFinding:
    return _finding("CV-FIN", "semantic", "blocker", path, message, identifier=identifier)


def _walk_document_paths(
    value: Any,
    *,
    path: str = "/",
) -> Iterable[tuple[str, Any]]:
    yield path, value
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}/{key}" if path != "/" else _json_pointer(key)
            yield from _walk_document_paths(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}/{index}"
            yield from _walk_document_paths(child, path=child_path)


def _collect_structural_findings(document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []

    for path, value in _walk_document_paths(document):
        if path.split("/")[-1] == "optionTierInCurrentDecision":
            findings.append(
                _finding(
                    "CV-006",
                    "structural",
                    "blocker",
                    path,
                    "optionTierInCurrentDecision is prohibited in schema 1.1.0",
                )
            )

    scenes = _as_sequence(document.get("scenes"))
    scene_ids: dict[str, str] = {}
    for index, scene in enumerate(scenes):
        scene_map = _as_mapping(scene)
        if scene_map is None:
            continue
        scene_id = _non_empty(scene_map.get("id"))
        path = _json_pointer("scenes", index, "id")
        if scene_id:
            if scene_id in scene_ids:
                findings.append(
                    _finding(
                        "CV-010",
                        "structural",
                        "blocker",
                        path,
                        f"duplicate scene id {scene_id!r}",
                        identifier=scene_id,
                    )
                )
            scene_ids[scene_id] = path

        options = _as_sequence((_as_mapping(scene_map.get("decision")) or {}).get("options"))
        option_ids: set[str] = set()
        for option_index, option in enumerate(options):
            option_map = _as_mapping(option)
            if option_map is None:
                continue
            option_id = _non_empty(option_map.get("id"))
            option_path = _json_pointer("scenes", index, "decision", "options", option_index, "id")
            if option_id:
                if option_id in option_ids:
                    findings.append(
                        _finding(
                            "CV-011",
                            "structural",
                            "blocker",
                            option_path,
                            f"duplicate option id {option_id!r} within scene",
                            identifier=option_id,
                        )
                    )
                option_ids.add(option_id)

        dialogue = _as_mapping(scene_map.get("dialogue")) or {}
        exchange_ids: set[str] = set()
        for exchange_index, exchange in enumerate(_as_sequence(dialogue.get("exchanges"))):
            exchange_map = _as_mapping(exchange)
            if exchange_map is None:
                continue
            exchange_id = _non_empty(exchange_map.get("exchangeId"))
            exchange_path = _json_pointer(
                "scenes",
                index,
                "dialogue",
                "exchanges",
                exchange_index,
                "exchangeId",
            )
            if exchange_id:
                if exchange_id in exchange_ids:
                    findings.append(
                        _finding(
                            "CV-018",
                            "structural",
                            "blocker",
                            exchange_path,
                            f"duplicate exchangeId {exchange_id!r} within scene",
                            identifier=exchange_id,
                        )
                    )
                exchange_ids.add(exchange_id)

        variant_ids: set[str] = set()
        for variant_index, variant in enumerate(_as_sequence(dialogue.get("variants"))):
            variant_map = _as_mapping(variant)
            if variant_map is None:
                continue
            variant_id = _non_empty(variant_map.get("variantId"))
            variant_path = _json_pointer(
                "scenes",
                index,
                "dialogue",
                "variants",
                variant_index,
                "variantId",
            )
            if variant_id:
                if variant_id in variant_ids:
                    findings.append(
                        _finding(
                            "CV-019",
                            "structural",
                            "blocker",
                            variant_path,
                            f"duplicate variantId {variant_id!r} within scene",
                            identifier=variant_id,
                        )
                    )
                variant_ids.add(variant_id)

    flag_ids_seen: set[str] = set()
    for index, flag in enumerate(_as_sequence(document.get("flags"))):
        flag_map = _as_mapping(flag)
        if flag_map is None:
            continue
        flag_id = _non_empty(flag_map.get("flagId"))
        path = _json_pointer("flags", index, "flagId")
        if flag_id:
            if flag_id in flag_ids_seen:
                findings.append(
                    _finding(
                        "CV-012",
                        "structural",
                        "blocker",
                        path,
                        f"duplicate flagId {flag_id!r}",
                        identifier=flag_id,
                    )
                )
            flag_ids_seen.add(flag_id)

    state_keys_seen: set[str] = set()
    for index, variable in enumerate(_as_sequence(document.get("stateVariables"))):
        variable_map = _as_mapping(variable)
        if variable_map is None:
            continue
        key = _non_empty(variable_map.get("key"))
        path = _json_pointer("stateVariables", index, "key")
        if key:
            if key in state_keys_seen:
                findings.append(
                    _finding(
                        "CV-013",
                        "structural",
                        "blocker",
                        path,
                        f"duplicate stateVariables.key {key!r}",
                        identifier=key,
                    )
                )
            state_keys_seen.add(key)

    counter_ids_seen: set[str] = set()
    for index, counter in enumerate(_as_sequence(document.get("runtimeCounters"))):
        counter_map = _as_mapping(counter)
        if counter_map is None:
            continue
        counter_id = _non_empty(counter_map.get("counterId"))
        path = _json_pointer("runtimeCounters", index, "counterId")
        if counter_id:
            if counter_id in counter_ids_seen:
                findings.append(
                    _finding(
                        "CV-014",
                        "structural",
                        "blocker",
                        path,
                        f"duplicate counterId {counter_id!r}",
                        identifier=counter_id,
                    )
                )
            counter_ids_seen.add(counter_id)

    character_ids_seen: set[str] = set()
    for index, character in enumerate(_as_sequence(document.get("characters"))):
        character_map = _as_mapping(character)
        if character_map is None:
            continue
        character_id = _non_empty(character_map.get("characterId"))
        path = _json_pointer("characters", index, "characterId")
        if character_id:
            if character_id in character_ids_seen:
                findings.append(
                    _finding(
                        "CV-015",
                        "structural",
                        "blocker",
                        path,
                        f"duplicate characterId {character_id!r}",
                        identifier=character_id,
                    )
                )
            character_ids_seen.add(character_id)

    outcome_ids_seen: set[str] = set()
    for index, outcome in enumerate(_as_sequence(document.get("outcomes"))):
        outcome_map = _as_mapping(outcome)
        if outcome_map is None:
            continue
        outcome_id = _non_empty(outcome_map.get("outcomeId"))
        path = _json_pointer("outcomes", index, "outcomeId")
        if outcome_id:
            if outcome_id in outcome_ids_seen:
                findings.append(
                    _finding(
                        "CV-016",
                        "structural",
                        "blocker",
                        path,
                        f"duplicate outcomeId {outcome_id!r}",
                        identifier=outcome_id,
                    )
                )
            outcome_ids_seen.add(outcome_id)

    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    cap_ids: set[str] = set()
    for cap_kind in ("severeCaps", "moderateCaps"):
        for cap_index, cap in enumerate(_as_sequence(classifier.get(cap_kind))):
            cap_map = _as_mapping(cap)
            if cap_map is None:
                continue
            cap_id = _non_empty(cap_map.get("capId"))
            cap_path = _json_pointer("outcomeClassifier", cap_kind, cap_index, "capId")
            if cap_id:
                if cap_id in cap_ids:
                    findings.append(
                        _finding(
                            "CV-020",
                            "structural",
                            "blocker",
                            cap_path,
                            f"duplicate capId {cap_id!r} within outcomeClassifier",
                            identifier=cap_id,
                        )
                    )
                cap_ids.add(cap_id)

    guard_ids: set[str] = set()
    for guard_index, guard in enumerate(_as_sequence(classifier.get("strongGuards"))):
        guard_map = _as_mapping(guard)
        if guard_map is None:
            continue
        guard_id = _non_empty(guard_map.get("guardId"))
        guard_path = _json_pointer("outcomeClassifier", "strongGuards", guard_index, "guardId")
        if guard_id:
            if guard_id in guard_ids:
                findings.append(
                    _finding(
                        "CV-020",
                        "structural",
                        "blocker",
                        guard_path,
                        f"duplicate guardId {guard_id!r} within outcomeClassifier",
                        identifier=guard_id,
                    )
                )
            guard_ids.add(guard_id)

    findings.extend(_collect_condition_shape_findings(document))
    findings.extend(_collect_executable_code_findings(document))
    return findings


def _flag_ids(document: Mapping[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for index, flag in enumerate(_as_sequence(document.get("flags"))):
        flag_map = _as_mapping(flag)
        if flag_map is None:
            continue
        flag_id = _non_empty(flag_map.get("flagId"))
        if flag_id:
            ids.setdefault(flag_id, _json_pointer("flags", index, "flagId"))
    return ids


def _state_variable_keys(document: Mapping[str, Any]) -> dict[str, str]:
    keys: dict[str, str] = {}
    for index, variable in enumerate(_as_sequence(document.get("stateVariables"))):
        variable_map = _as_mapping(variable)
        if variable_map is None:
            continue
        key = _non_empty(variable_map.get("key"))
        if key:
            keys.setdefault(key, _json_pointer("stateVariables", index, "key"))
    return keys


def _counter_ids(document: Mapping[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for index, counter in enumerate(_as_sequence(document.get("runtimeCounters"))):
        counter_map = _as_mapping(counter)
        if counter_map is None:
            continue
        counter_id = _non_empty(counter_map.get("counterId"))
        if counter_id:
            ids.setdefault(counter_id, _json_pointer("runtimeCounters", index, "counterId"))
    return ids


def _character_ids(document: Mapping[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for index, character in enumerate(_as_sequence(document.get("characters"))):
        character_map = _as_mapping(character)
        if character_map is None:
            continue
        character_id = _non_empty(character_map.get("characterId"))
        if character_id:
            ids.setdefault(character_id, _json_pointer("characters", index, "characterId"))
    return ids


def _outcome_ids(document: Mapping[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for index, outcome in enumerate(_as_sequence(document.get("outcomes"))):
        outcome_map = _as_mapping(outcome)
        if outcome_map is None:
            continue
        outcome_id = _non_empty(outcome_map.get("outcomeId"))
        if outcome_id:
            ids.setdefault(outcome_id, _json_pointer("outcomes", index, "outcomeId"))
    return ids


def _scene_maps(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    scenes: dict[str, Mapping[str, Any]] = {}
    for index, scene in enumerate(_as_sequence(document.get("scenes"))):
        scene_map = _as_mapping(scene)
        if scene_map is None:
            continue
        scene_id = _non_empty(scene_map.get("id"))
        if scene_id and scene_id not in scenes:
            scenes[scene_id] = scene_map
    return scenes


def _iter_condition_roots(document: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    for cap_kind in ("severeCaps", "moderateCaps"):
        for cap_index, cap in enumerate(_as_sequence(classifier.get(cap_kind))):
            cap_map = _as_mapping(cap)
            if cap_map is None:
                continue
            condition = _as_mapping(cap_map.get("when"))
            if condition is not None:
                yield _json_pointer("outcomeClassifier", cap_kind, cap_index, "when"), condition

    for guard_index, guard in enumerate(_as_sequence(classifier.get("strongGuards"))):
        guard_map = _as_mapping(guard)
        if guard_map is None:
            continue
        condition = _as_mapping(guard_map.get("when"))
        if condition is not None:
            yield _json_pointer("outcomeClassifier", "strongGuards", guard_index, "when"), condition

    for scene_index, scene in enumerate(_as_sequence(document.get("scenes"))):
        scene_map = _as_mapping(scene)
        if scene_map is None:
            continue
        dialogue = _as_mapping(scene_map.get("dialogue")) or {}
        for variant_index, variant in enumerate(_as_sequence(dialogue.get("variants"))):
            variant_map = _as_mapping(variant)
            if variant_map is None:
                continue
            condition = _as_mapping(variant_map.get("when"))
            if condition is not None:
                yield (
                    _json_pointer("scenes", scene_index, "dialogue", "variants", variant_index, "when"),
                    condition,
                )

        options = _as_sequence((_as_mapping(scene_map.get("decision")) or {}).get("options"))
        for option_index, option in enumerate(options):
            option_map = _as_mapping(option)
            if option_map is None:
                continue
            routing = _as_mapping(option_map.get("routing")) or {}
            corrective_route = _as_mapping(routing.get("correctiveRoute"))
            if corrective_route is None:
                continue
            condition = _as_mapping(corrective_route.get("budgetCondition"))
            if condition is not None:
                yield (
                    _json_pointer(
                        "scenes",
                        scene_index,
                        "decision",
                        "options",
                        option_index,
                        "routing",
                        "correctiveRoute",
                        "budgetCondition",
                    ),
                    condition,
                )


def _condition_metrics(condition: Mapping[str, Any], *, depth: int = 1) -> tuple[int, int]:
    max_depth = depth
    node_count = 1
    if "all" in condition or "any" in condition:
        children = _as_sequence(condition.get("all") or condition.get("any"))
        for child in children:
            child_map = _as_mapping(child)
            if child_map is None:
                continue
            child_depth, child_nodes = _condition_metrics(child_map, depth=depth + 1)
            max_depth = max(max_depth, child_depth)
            node_count += child_nodes
    elif "not" in condition:
        child_map = _as_mapping(condition.get("not"))
        if child_map is not None:
            child_depth, child_nodes = _condition_metrics(child_map, depth=depth + 1)
            max_depth = max(max_depth, child_depth)
            node_count += child_nodes
    return max_depth, node_count


def _collect_condition_shape_findings(document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for path, condition in _iter_condition_roots(document):
        max_depth, node_count = _condition_metrics(condition)
        if max_depth > _MAX_CONDITION_DEPTH:
            findings.append(
                _finding(
                    "CV-032",
                    "structural",
                    "blocker",
                    path,
                    f"condition nesting depth {max_depth} exceeds maximum {_MAX_CONDITION_DEPTH}",
                )
            )
        if node_count > _MAX_CONDITION_NODES:
            findings.append(
                _finding(
                    "CV-033",
                    "structural",
                    "blocker",
                    path,
                    f"condition node count {node_count} exceeds maximum {_MAX_CONDITION_NODES}",
                )
            )
    return findings


def _collect_executable_code_findings(document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for path, value in _walk_document_paths(document):
        if isinstance(value, str) and _EXECUTABLE_CODE_RE.search(value):
            findings.append(
                _finding(
                    "CV-037",
                    "structural",
                    "blocker",
                    path,
                    "executable code or arbitrary expression strings are forbidden",
                )
            )
    return findings


def _iter_condition_leaves(
    condition: Mapping[str, Any],
    *,
    path: str,
) -> Iterable[tuple[str, Mapping[str, Any]]]:
    if "all" in condition or "any" in condition:
        children = _as_sequence(condition.get("all") or condition.get("any"))
        for index, child in enumerate(children):
            child_map = _as_mapping(child)
            if child_map is None:
                continue
            child_path = f"{path}/{'all' if 'all' in condition else 'any'}/{index}"
            yield from _iter_condition_leaves(child_map, path=child_path)
        return
    if "not" in condition:
        child_map = _as_mapping(condition.get("not"))
        if child_map is not None:
            yield from _iter_condition_leaves(child_map, path=f"{path}/not")
        return
    yield path, condition


def _collect_finite_numeric_findings(document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []

    for index, variable in enumerate(_as_sequence(document.get("stateVariables"))):
        variable_map = _as_mapping(variable)
        if variable_map is None:
            continue
        key = _non_empty(variable_map.get("key"))
        base = _json_pointer("stateVariables", index)
        minimum = variable_map.get("minimum")
        maximum = variable_map.get("maximum")
        for bound_name, bound_value in (("minimum", minimum), ("maximum", maximum)):
            if bound_value is None:
                continue
            bound_path = f"{base}/{bound_name}"
            if _is_bool(bound_value) or not _is_finite_number(bound_value):
                findings.append(
                    _finite_number_finding(
                        bound_path,
                        f"stateVariables.{bound_name} must be a finite number, not bool or non-finite",
                        identifier=key or None,
                    )
                )
        if (
            minimum is not None
            and maximum is not None
            and _is_finite_number(minimum)
            and _is_finite_number(maximum)
            and float(minimum) > float(maximum)
        ):
            findings.append(
                _finding(
                    "CV-054",
                    "semantic",
                    "blocker",
                    base,
                    f"stateVariables minimum {minimum} exceeds maximum {maximum}",
                    identifier=key or None,
                )
            )

    initial_state = _as_mapping(document.get("initialState")) or {}
    for key, value in initial_state.items():
        path = _json_pointer("initialState", key)
        if not _is_finite_number(value):
            findings.append(
                _finite_number_finding(
                    path,
                    f"initialState value for {key!r} must be a finite number, not bool or non-finite",
                    identifier=str(key),
                )
            )

    for scene_index, scene in enumerate(_as_sequence(document.get("scenes"))):
        scene_map = _as_mapping(scene)
        if scene_map is None:
            continue
        options = _as_sequence((_as_mapping(scene_map.get("decision")) or {}).get("options"))
        for option_index, option in enumerate(options):
            option_map = _as_mapping(option)
            if option_map is None:
                continue
            option_base = _json_pointer("scenes", scene_index, "decision", "options", option_index)
            for key, delta in (_as_mapping(option_map.get("stateChanges")) or {}).items():
                delta_path = f"{option_base}/stateChanges/{key}"
                if not _is_finite_number(delta):
                    findings.append(
                        _finite_number_finding(
                            delta_path,
                            f"stateChanges delta for {key!r} must be a finite number, not bool or non-finite",
                            identifier=str(key),
                        )
                    )

            set_seen: set[str] = set()
            for flag_index, flag_id in enumerate(_as_sequence(option_map.get("setFlags"))):
                flag_id_text = _non_empty(flag_id)
                flag_path = f"{option_base}/setFlags/{flag_index}"
                if flag_id_text and flag_id_text in set_seen:
                    findings.append(
                        _finding(
                            "CV-051",
                            "semantic",
                            "blocker",
                            flag_path,
                            f"duplicate setFlags entry {flag_id_text!r}",
                            identifier=flag_id_text,
                        )
                    )
                if flag_id_text:
                    set_seen.add(flag_id_text)

            clear_seen: set[str] = set()
            for flag_index, flag_id in enumerate(_as_sequence(option_map.get("clearFlags"))):
                flag_id_text = _non_empty(flag_id)
                flag_path = f"{option_base}/clearFlags/{flag_index}"
                if flag_id_text and flag_id_text in clear_seen:
                    findings.append(
                        _finding(
                            "CV-051",
                            "semantic",
                            "blocker",
                            flag_path,
                            f"duplicate clearFlags entry {flag_id_text!r}",
                            identifier=flag_id_text,
                        )
                    )
                if flag_id_text:
                    clear_seen.add(flag_id_text)

    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    composite = _as_mapping(classifier.get("compositeFormula")) or {}
    for index, term in enumerate(_as_sequence(composite.get("terms"))):
        term_map = _as_mapping(term)
        if term_map is None:
            continue
        weight = term_map.get("weight")
        if weight is not None and not _is_finite_number(weight):
            findings.append(
                _finite_number_finding(
                    _json_pointer("outcomeClassifier", "compositeFormula", "terms", index, "weight"),
                    "formula weight must be a finite number, not bool or non-finite",
                )
            )

    for index, band in enumerate(_as_sequence(classifier.get("scoreBands"))):
        band_map = _as_mapping(band)
        if band_map is None:
            continue
        path = _json_pointer("outcomeClassifier", "scoreBands", index)
        for bound_name in ("minInclusive", "maxExclusive"):
            bound_value = band_map.get(bound_name)
            if bound_value is None:
                continue
            if not _is_finite_number(bound_value):
                findings.append(
                    _finite_number_finding(
                        f"{path}/{bound_name}",
                        f"scoreBands.{bound_name} must be a finite number when present, not bool or non-finite",
                    )
                )

    for path, condition in _iter_condition_roots(document):
        for leaf_path, leaf in _iter_condition_leaves(condition, path=path):
            state_compare = _as_mapping(leaf.get("stateCompare"))
            if state_compare is not None:
                compare_value = state_compare.get("value")
                if not _is_finite_number(compare_value):
                    findings.append(
                        _finite_number_finding(
                            f"{leaf_path}/stateCompare/value",
                            "stateCompare.value must be a finite number, not bool or non-finite",
                        )
                    )
            counter_compare = _as_mapping(leaf.get("counterCompare"))
            if counter_compare is not None:
                compare_value = counter_compare.get("value")
                if not _is_finite_number(compare_value):
                    findings.append(
                        _finite_number_finding(
                            f"{leaf_path}/counterCompare/value",
                            "counterCompare.value must be a finite number, not bool or non-finite",
                        )
                    )

    for index, counter in enumerate(_as_sequence(document.get("runtimeCounters"))):
        counter_map = _as_mapping(counter)
        if counter_map is None:
            continue
        counter_id = _non_empty(counter_map.get("counterId"))
        base = _json_pointer("runtimeCounters", index)
        minimum = counter_map.get("minimum")
        maximum = counter_map.get("maximum")
        initial_value = counter_map.get("initialValue")
        if initial_value is not None and not _is_finite_number(initial_value):
            findings.append(
                _finite_number_finding(
                    f"{base}/initialValue",
                    f"runtimeCounters.initialValue must be a finite number, not bool or non-finite",
                    identifier=counter_id or None,
                )
            )
        for bound_name, bound_value in (("minimum", minimum), ("maximum", maximum)):
            if bound_value is None:
                continue
            if not _is_finite_number(bound_value):
                findings.append(
                    _finite_number_finding(
                        f"{base}/{bound_name}",
                        f"runtimeCounters.{bound_name} must be a finite number, not bool or non-finite",
                        identifier=counter_id or None,
                    )
                )
        if (
            minimum is not None
            and maximum is not None
            and _is_finite_number(minimum)
            and _is_finite_number(maximum)
            and float(minimum) > float(maximum)
        ):
            findings.append(
                _finding(
                    "CV-054",
                    "semantic",
                    "blocker",
                    base,
                    f"runtimeCounters minimum {minimum} exceeds maximum {maximum}",
                    identifier=counter_id or None,
                )
            )

    for index, outcome in enumerate(_as_sequence(document.get("outcomes"))):
        outcome_map = _as_mapping(outcome)
        if outcome_map is None:
            continue
        rank = outcome_map.get("classificationRank")
        if _is_bool(rank) or (rank is not None and not isinstance(rank, int)):
            if _is_bool(rank):
                findings.append(
                    _finite_number_finding(
                        _json_pointer("outcomes", index, "classificationRank"),
                        "classificationRank must be an integer, not bool",
                    )
                )

    for scene_index, scene in enumerate(_as_sequence(document.get("scenes"))):
        scene_map = _as_mapping(scene)
        if scene_map is None:
            continue
        dialogue = _as_mapping(scene_map.get("dialogue")) or {}
        for variant_index, variant in enumerate(_as_sequence(dialogue.get("variants"))):
            variant_map = _as_mapping(variant)
            if variant_map is None:
                continue
            priority = variant_map.get("priority")
            if _is_bool(priority):
                findings.append(
                    _finite_number_finding(
                        _json_pointer(
                            "scenes",
                            scene_index,
                            "dialogue",
                            "variants",
                            variant_index,
                            "priority",
                        ),
                        "variant priority must be an integer, not bool",
                    )
                )

    return findings


def _collect_semantic_findings(document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    flag_ids = set(_flag_ids(document))
    state_keys = set(_state_variable_keys(document))
    counter_ids = set(_counter_ids(document))
    character_ids = set(_character_ids(document))
    outcome_ids = set(_outcome_ids(document))
    scene_by_id = _scene_maps(document)
    domain_ids: set[str] = set()
    for domain in _as_sequence(document.get("domains")):
        domain_map = _as_mapping(domain)
        if domain_map is None:
            continue
        domain_id = _non_empty(domain_map.get("id"))
        if domain_id:
            domain_ids.add(domain_id)

    scenes_have_domain = False
    for scene in _as_sequence(document.get("scenes")):
        scene_map = _as_mapping(scene)
        if scene_map is not None and _non_empty(scene_map.get("domainId")):
            scenes_have_domain = True
            break

    if scenes_have_domain:
        if not domain_ids:
            findings.append(
                _finding(
                    "CV-100",
                    "semantic",
                    "high",
                    "/domains",
                    "domains[] is required when any scene declares domainId",
                )
            )

    classification_ranks: dict[int, str] = {}
    for index, outcome in enumerate(_as_sequence(document.get("outcomes"))):
        outcome_map = _as_mapping(outcome)
        if outcome_map is None:
            continue
        rank = outcome_map.get("classificationRank")
        path = _json_pointer("outcomes", index, "classificationRank")
        if isinstance(rank, int) and not isinstance(rank, bool):
            if rank in classification_ranks:
                findings.append(
                    _finding(
                        "CV-017",
                        "semantic",
                        "blocker",
                        path,
                        f"duplicate classificationRank {rank}",
                        identifier=str(rank),
                    )
                )
            classification_ranks[rank] = path

    for path, condition in _iter_condition_roots(document):
        for leaf_path, leaf in _iter_condition_leaves(condition, path=path):
            if "flagSet" in leaf:
                flag_id = _non_empty(leaf.get("flagSet"))
                if flag_id and flag_id not in flag_ids:
                    findings.append(
                        _finding(
                            "CV-034",
                            "semantic",
                            "blocker",
                            f"{leaf_path}/flagSet",
                            f"flagSet references unknown flagId {flag_id!r}",
                            identifier=flag_id,
                        )
                    )
            if "flagNotSet" in leaf:
                flag_id = _non_empty(leaf.get("flagNotSet"))
                if flag_id and flag_id not in flag_ids:
                    findings.append(
                        _finding(
                            "CV-034",
                            "semantic",
                            "blocker",
                            f"{leaf_path}/flagNotSet",
                            f"flagNotSet references unknown flagId {flag_id!r}",
                            identifier=flag_id,
                        )
                    )
            state_compare = _as_mapping(leaf.get("stateCompare"))
            if state_compare is not None:
                variable_id = _non_empty(state_compare.get("variableId"))
                if variable_id and variable_id not in state_keys:
                    findings.append(
                        _finding(
                            "CV-035",
                            "semantic",
                            "blocker",
                            f"{leaf_path}/stateCompare/variableId",
                            f"stateCompare.variableId references unknown state variable {variable_id!r}",
                            identifier=variable_id,
                        )
                    )
            counter_compare = _as_mapping(leaf.get("counterCompare"))
            if counter_compare is not None:
                counter_id = _non_empty(counter_compare.get("counterId"))
                if counter_id and counter_id not in counter_ids:
                    findings.append(
                        _finding(
                            "CV-036",
                            "semantic",
                            "blocker",
                            f"{leaf_path}/counterCompare/counterId",
                            f"counterCompare.counterId references unknown counter {counter_id!r}",
                            identifier=counter_id,
                        )
                    )

    initial_state = _as_mapping(document.get("initialState")) or {}
    for key, value in initial_state.items():
        if key in counter_ids:
            findings.append(
                _finding(
                    "CV-055",
                    "semantic",
                    "blocker",
                    _json_pointer("initialState", key),
                    f"initialState must not declare counter {key!r}",
                    identifier=key,
                )
            )
        if key not in state_keys:
            findings.append(
                _finding(
                    "CV-053",
                    "semantic",
                    "blocker",
                    _json_pointer("initialState", key),
                    f"initialState key {key!r} is not declared in stateVariables",
                    identifier=key,
                )
            )
        elif _is_finite_number(value):
            findings.extend(_validate_state_value_bounds(document, key, float(value), _json_pointer("initialState", key)))

    for index, variable in enumerate(_as_sequence(document.get("stateVariables"))):
        variable_map = _as_mapping(variable)
        if variable_map is None:
            continue
        key = _non_empty(variable_map.get("key"))
        if key in counter_ids:
            findings.append(
                _finding(
                    "CV-055",
                    "semantic",
                    "blocker",
                    _json_pointer("stateVariables", index, "key"),
                    f"stateVariables must not reuse counter id {key!r}",
                    identifier=key,
                )
            )

    for scene_index, scene in enumerate(_as_sequence(document.get("scenes"))):
        scene_map = _as_mapping(scene)
        if scene_map is None:
            continue
        scene_id = _non_empty(scene_map.get("id"))
        scene_type = _non_empty(scene_map.get("sceneType"))

        for char_index, character_id_raw in enumerate(_as_sequence(scene_map.get("charactersPresent"))):
            character_id = _non_empty(character_id_raw)
            char_path = _json_pointer("scenes", scene_index, "charactersPresent", char_index)
            if character_id == "learner":
                findings.append(
                    _finding(
                        "CV-041",
                        "semantic",
                        "blocker",
                        char_path,
                        'charactersPresent must not contain the literal "learner"',
                    )
                )
            elif character_id and character_id not in character_ids:
                findings.append(
                    _finding(
                        "CV-040",
                        "semantic",
                        "blocker",
                        char_path,
                        f"charactersPresent references unknown characterId {character_id!r}",
                        identifier=character_id,
                    )
                )

        domain_id = _non_empty(scene_map.get("domainId"))
        if domain_id and domain_ids and domain_id not in domain_ids:
            findings.append(
                _finding(
                    "CV-100",
                    "semantic",
                    "high",
                    _json_pointer("scenes", scene_index, "domainId"),
                    f"domainId {domain_id!r} is not declared in domains[]",
                    identifier=domain_id,
                )
            )

        if scene_type == "corrective":
            metadata = _as_mapping(scene_map.get("correctiveMetadata"))
            if metadata is None:
                findings.append(
                    _finding(
                        "CV-068",
                        "semantic",
                        "blocker",
                        _json_pointer("scenes", scene_index, "correctiveMetadata"),
                        "correctiveMetadata is required on corrective scenes",
                    )
                )
            elif metadata.get("mayRebranch") is not False:
                findings.append(
                    _finding(
                        "CV-068",
                        "semantic",
                        "blocker",
                        _json_pointer("scenes", scene_index, "correctiveMetadata", "mayRebranch"),
                        "correctiveMetadata.mayRebranch must be false",
                    )
                )

        is_detour = scene_map.get("isDetour")
        if is_detour is not None and bool(is_detour) != (scene_type == "corrective"):
            findings.append(
                _finding(
                    "CV-090",
                    "semantic",
                    "high",
                    _json_pointer("scenes", scene_index, "isDetour"),
                    "isDetour must match sceneType == corrective when present",
                )
            )

        dialogue = _as_mapping(scene_map.get("dialogue")) or {}
        exchange_ids = {
            _non_empty((_as_mapping(exchange) or {}).get("exchangeId"))
            for exchange in _as_sequence(dialogue.get("exchanges"))
            if _as_mapping(exchange) is not None
        }
        exchange_ids.discard("")

        variant_priorities: dict[int, str] = {}
        for variant_index, variant in enumerate(_as_sequence(dialogue.get("variants"))):
            variant_map = _as_mapping(variant)
            if variant_map is None:
                continue
            priority = variant_map.get("priority")
            priority_path = _json_pointer("scenes", scene_index, "dialogue", "variants", variant_index, "priority")
            if isinstance(priority, int) and not isinstance(priority, bool):
                if priority in variant_priorities:
                    findings.append(
                        _finding(
                            "CV-044",
                            "semantic",
                            "blocker",
                            priority_path,
                            f"duplicate variant priority {priority} within scene",
                            identifier=str(priority),
                        )
                    )
                variant_priorities[priority] = priority_path

            override_ids: list[str] = []
            for override_index, override in enumerate(_as_sequence(variant_map.get("overrides"))):
                override_map = _as_mapping(override)
                if override_map is None:
                    continue
                exchange_id = _non_empty(override_map.get("exchangeId"))
                override_path = _json_pointer(
                    "scenes",
                    scene_index,
                    "dialogue",
                    "variants",
                    variant_index,
                    "overrides",
                    override_index,
                    "exchangeId",
                )
                if exchange_id and exchange_id not in exchange_ids:
                    findings.append(
                        _finding(
                            "CV-045",
                            "semantic",
                            "blocker",
                            override_path,
                            f"override exchangeId {exchange_id!r} does not exist in base exchanges",
                            identifier=exchange_id,
                        )
                    )
                if exchange_id:
                    override_ids.append(exchange_id)
            if len(set(override_ids)) != len(override_ids):
                findings.append(
                    _finding(
                        "CV-046",
                        "semantic",
                        "blocker",
                        _json_pointer("scenes", scene_index, "dialogue", "variants", variant_index, "overrides"),
                        "variant overrides must not duplicate or reorder exchanges",
                    )
                )

        for exchange_index, exchange in enumerate(_as_sequence(dialogue.get("exchanges"))):
            exchange_map = _as_mapping(exchange)
            if exchange_map is None:
                continue
            speaker_id = _non_empty(exchange_map.get("speakerId"))
            if speaker_id and speaker_id != "learner" and speaker_id not in character_ids:
                findings.append(
                    _finding(
                        "CV-043",
                        "semantic",
                        "blocker",
                        _json_pointer("scenes", scene_index, "dialogue", "exchanges", exchange_index, "speakerId"),
                        f"speakerId {speaker_id!r} does not resolve to character registry",
                        identifier=speaker_id,
                    )
                )

        options = _as_sequence((_as_mapping(scene_map.get("decision")) or {}).get("options"))
        option_ids_in_scene = {
            _non_empty((_as_mapping(option) or {}).get("id"))
            for option in options
            if _as_mapping(option) is not None
        }
        option_ids_in_scene.discard("")

        for option_index, option in enumerate(options):
            option_map = _as_mapping(option)
            if option_map is None:
                continue
            option_id = _non_empty(option_map.get("id"))
            option_base = _json_pointer("scenes", scene_index, "decision", "options", option_index)

            for key in (_as_mapping(option_map.get("stateChanges")) or {}):
                if key not in state_keys:
                    findings.append(
                        _finding(
                            "CV-050",
                            "semantic",
                            "blocker",
                            f"{option_base}/stateChanges/{key}",
                            f"stateChanges key {key!r} is not declared in stateVariables",
                            identifier=key,
                        )
                    )
                if key in counter_ids:
                    findings.append(
                        _finding(
                            "CV-055",
                            "semantic",
                            "blocker",
                            f"{option_base}/stateChanges/{key}",
                            f"stateChanges must not declare counter {key!r}",
                            identifier=key,
                        )
                    )

            for flag_index, flag_id in enumerate(_as_sequence(option_map.get("setFlags"))):
                flag_id_text = _non_empty(flag_id)
                flag_path = f"{option_base}/setFlags/{flag_index}"
                if flag_id_text and flag_id_text not in flag_ids:
                    findings.append(
                        _finding(
                            "CV-051",
                            "semantic",
                            "blocker",
                            flag_path,
                            f"setFlags references unknown flagId {flag_id_text!r}",
                            identifier=flag_id_text,
                        )
                    )
                elif flag_id_text and scene_id and option_id:
                    findings.extend(
                        _validate_flag_writer_authorization(
                            document,
                            flag_id=flag_id_text,
                            scene_id=scene_id,
                            option_id=option_id,
                            path=flag_path,
                            kind="set",
                        )
                    )

            for flag_index, flag_id in enumerate(_as_sequence(option_map.get("clearFlags"))):
                flag_id_text = _non_empty(flag_id)
                flag_path = f"{option_base}/clearFlags/{flag_index}"
                if flag_id_text and flag_id_text not in flag_ids:
                    findings.append(
                        _finding(
                            "CV-051",
                            "semantic",
                            "blocker",
                            flag_path,
                            f"clearFlags references unknown flagId {flag_id_text!r}",
                            identifier=flag_id_text,
                        )
                    )
                elif flag_id_text and scene_id and option_id:
                    findings.extend(
                        _validate_flag_writer_authorization(
                            document,
                            flag_id=flag_id_text,
                            scene_id=scene_id,
                            option_id=option_id,
                            path=flag_path,
                            kind="clear",
                        )
                    )

            debrief_seed = _as_mapping(option_map.get("debriefSeed"))
            if debrief_seed is not None:
                strongest_option_id = _non_empty(debrief_seed.get("strongestOptionId"))
                if strongest_option_id and strongest_option_id not in option_ids_in_scene:
                    findings.append(
                        _finding(
                            "CV-056",
                            "semantic",
                            "high",
                            f"{option_base}/debriefSeed/strongestOptionId",
                            f"strongestOptionId {strongest_option_id!r} does not resolve in the same scene",
                            identifier=strongest_option_id,
                        )
                    )

            routing = _as_mapping(option_map.get("routing")) or {}
            if scene_type == "corrective" and routing.get("correctiveRoute") is not None:
                findings.append(
                    _finding(
                        "CV-063",
                        "semantic",
                        "blocker",
                        f"{option_base}/routing/correctiveRoute",
                        "correctiveRoute is forbidden on corrective-scene options",
                    )
                )

            primary_next = _non_empty(routing.get("primaryNextSceneId"))
            if routing.get("terminal") is True:
                if primary_next != TERMINAL_SENTINEL:
                    findings.append(
                        _finding(
                            "CV-061",
                            "semantic",
                            "blocker",
                            f"{option_base}/routing/primaryNextSceneId",
                            f"terminal options must route to {TERMINAL_SENTINEL}",
                        )
                    )
                if routing.get("correctiveRoute") is not None:
                    findings.append(
                        _finding(
                            "CV-061",
                            "semantic",
                            "blocker",
                            f"{option_base}/routing/correctiveRoute",
                            "terminal options must not declare correctiveRoute",
                        )
                    )
            elif primary_next == TERMINAL_SENTINEL:
                findings.append(
                    _finding(
                        "CV-062",
                        "semantic",
                        "blocker",
                        f"{option_base}/routing/primaryNextSceneId",
                        f"non-terminal options must not route to {TERMINAL_SENTINEL}",
                    )
                )

            corrective_route = _as_mapping(routing.get("correctiveRoute"))
            if corrective_route is not None:
                corrective_scene_id = _non_empty(corrective_route.get("correctiveSceneId"))
                if corrective_scene_id:
                    corrective_scene = scene_by_id.get(corrective_scene_id)
                    if corrective_scene is None:
                        findings.append(
                            _finding(
                                "CV-067",
                                "semantic",
                                "blocker",
                                f"{option_base}/routing/correctiveRoute/correctiveSceneId",
                                f"correctiveSceneId {corrective_scene_id!r} does not resolve to a scene",
                                identifier=corrective_scene_id,
                            )
                        )
                    elif _non_empty(corrective_scene.get("sceneType")) != "corrective":
                        findings.append(
                            _finding(
                                "CV-067",
                                "semantic",
                                "blocker",
                                f"{option_base}/routing/correctiveRoute/correctiveSceneId",
                                f"correctiveSceneId {corrective_scene_id!r} must reference sceneType corrective",
                                identifier=corrective_scene_id,
                            )
                        )

                skip_target = _non_empty(corrective_route.get("whenCorrectiveSkippedNextSceneId"))
                reconvergence = _non_empty(corrective_route.get("reconvergenceSceneId"))
                if skip_target and reconvergence and primary_next:
                    if not (skip_target == reconvergence == primary_next):
                        findings.append(
                            _finding(
                                "CV-066",
                                "semantic",
                                "blocker",
                                f"{option_base}/routing/correctiveRoute",
                                "whenCorrectiveSkippedNextSceneId, reconvergenceSceneId, and primaryNextSceneId must match",
                            )
                        )

    corrective_scenes = [
        scene_id
        for scene_id, scene in scene_by_id.items()
        if _non_empty(scene.get("sceneType")) == "corrective"
    ]
    if corrective_scenes and _as_mapping(document.get("correctiveBudgetPolicy")) is None:
        findings.append(
            _finding(
                "CV-069",
                "semantic",
                "blocker",
                "/correctiveBudgetPolicy",
                "correctiveBudgetPolicy is required when corrective scenes exist",
            )
        )

    budget_policy = _as_mapping(document.get("correctiveBudgetPolicy")) or {}
    experienced_counter_id = _non_empty(budget_policy.get("experiencedCounterId"))
    if experienced_counter_id and experienced_counter_id not in counter_ids:
        findings.append(
            _finding(
                "CV-070",
                "semantic",
                "blocker",
                "/correctiveBudgetPolicy/experiencedCounterId",
                f"experiencedCounterId {experienced_counter_id!r} references unknown counter",
                identifier=experienced_counter_id,
            )
        )

    max_available = budget_policy.get("maxAvailableCorrectiveScenes")
    max_experienced = budget_policy.get("maxExperiencedCorrectiveScenes")
    if isinstance(max_available, int) and isinstance(max_experienced, int) and max_experienced > max_available:
        findings.append(
            _finding(
                "CV-071",
                "semantic",
                "high",
                "/correctiveBudgetPolicy/maxExperiencedCorrectiveScenes",
                "maxExperiencedCorrectiveScenes must be <= maxAvailableCorrectiveScenes",
            )
        )

    if _as_mapping(document.get("introduction")) is None:
        findings.append(
            _finding(
                "CV-107",
                "semantic",
                "high",
                "/introduction",
                "introduction is required for schema 1.1.0 scenarios",
            )
        )

    start_scene = _non_empty(document.get("startScene"))
    if start_scene:
        start = scene_by_id.get(start_scene)
        if start is None:
            findings.append(
                _finding(
                    "CV-107",
                    "semantic",
                    "high",
                    "/startScene",
                    f"startScene {start_scene!r} must resolve to an authored scene",
                    identifier=start_scene,
                )
            )
        elif _non_empty(start.get("sceneType")) != "core":
            findings.append(
                _finding(
                    "CV-107",
                    "semantic",
                    "high",
                    "/startScene",
                    "startScene must reference a scored core scene",
                    identifier=start_scene,
                )
            )

    findings.extend(_collect_formula_findings(document, state_keys=state_keys))
    findings.extend(_collect_classifier_reference_findings(document, outcome_ids=outcome_ids))
    findings.extend(_collect_score_band_findings(document, outcome_ids=outcome_ids))
    findings.extend(_collect_outcome_reference_findings(document, outcome_ids=outcome_ids))
    findings.extend(_collect_finite_numeric_findings(document))
    return findings


def _validate_state_value_bounds(
    document: Mapping[str, Any],
    key: str,
    value: float,
    path: str,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for variable in _as_sequence(document.get("stateVariables")):
        variable_map = _as_mapping(variable)
        if variable_map is None:
            continue
        if _non_empty(variable_map.get("key")) != key:
            continue
        minimum = variable_map.get("minimum")
        maximum = variable_map.get("maximum")
        if isinstance(minimum, (int, float)) and _is_finite_number(minimum) and value < float(minimum):
            findings.append(
                _finding(
                    "CV-054",
                    "semantic",
                    "blocker",
                    path,
                    f"initialState value {value} is below declared minimum {minimum}",
                    identifier=key,
                )
            )
        if isinstance(maximum, (int, float)) and _is_finite_number(maximum) and value > float(maximum):
            findings.append(
                _finding(
                    "CV-054",
                    "semantic",
                    "blocker",
                    path,
                    f"initialState value {value} is above declared maximum {maximum}",
                    identifier=key,
                )
            )
        break
    return findings


def _validate_flag_writer_authorization(
    document: Mapping[str, Any],
    *,
    flag_id: str,
    scene_id: str,
    option_id: str,
    path: str,
    kind: str,
) -> list[ValidationFinding]:
    for flag in _as_sequence(document.get("flags")):
        flag_map = _as_mapping(flag)
        if flag_map is None or _non_empty(flag_map.get("flagId")) != flag_id:
            continue
        allowed_key = "allowedSetters" if kind == "set" else "allowedClearers"
        allowed = _as_sequence(flag_map.get(allowed_key))
        if not allowed:
            return [
                _finding(
                    "CV-052",
                    "semantic",
                    "high",
                    path,
                    f"{kind} on flag {flag_id!r} is not authorized by {allowed_key}",
                    identifier=flag_id,
                )
            ]
        for writer in allowed:
            writer_map = _as_mapping(writer)
            if writer_map is None:
                continue
            if (
                _non_empty(writer_map.get("sceneId")) == scene_id
                and _non_empty(writer_map.get("optionId")) == option_id
            ):
                return []
        return [
            _finding(
                "CV-052",
                "semantic",
                "high",
                path,
                f"{kind} on flag {flag_id!r} is not listed in {allowed_key}",
                identifier=flag_id,
            )
        ]
    return []


def _collect_formula_findings(
    document: Mapping[str, Any],
    *,
    state_keys: set[str],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    formula_fields = {
        "positiveHealth": "/outcomeClassifier/positiveHealthFormula",
        "decisionQuality": "/outcomeClassifier/decisionQualityFormula",
        "composite": "/outcomeClassifier/compositeFormula",
    }
    dependency_graph: dict[str, set[str]] = {name: set() for name in formula_fields}

    for formula_name, base_path in formula_fields.items():
        field_key = f"{formula_name}Formula"
        formula = _as_mapping(classifier.get(field_key))
        if formula is None:
            continue
        findings.extend(
            _validate_formula_node(
                formula,
                path=base_path,
                state_keys=state_keys,
                formula_name=formula_name,
                dependency_graph=dependency_graph,
            )
        )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, *, path: str) -> None:
        if node in visiting:
            findings.append(
                _finding(
                    "CV-082",
                    "semantic",
                    "blocker",
                    path,
                    f"formula dependency cycle detected involving {node!r}",
                    identifier=node,
                )
            )
            return
        if node in visited:
            return
        visiting.add(node)
        for dependency in dependency_graph.get(node, set()):
            visit(dependency, path=path)
        visiting.remove(node)
        visited.add(node)

    for node in dependency_graph:
        visit(node, path="/outcomeClassifier")

    return findings


def _validate_formula_node(
    formula: Mapping[str, Any],
    *,
    path: str,
    state_keys: set[str],
    formula_name: str,
    dependency_graph: dict[str, set[str]],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    formula_type = _non_empty(formula.get("type"))

    if formula_type == "weighted_dimension_health":
        for index, dimension in enumerate(_as_sequence(formula.get("dimensions"))):
            dimension_map = _as_mapping(dimension)
            if dimension_map is None:
                continue
            variable_id = _non_empty(dimension_map.get("variableId"))
            if variable_id and variable_id not in state_keys:
                findings.append(
                    _finding(
                        "CV-080",
                        "semantic",
                        "blocker",
                        f"{path}/dimensions/{index}/variableId",
                        f"formula references unknown state variable {variable_id!r}",
                        identifier=variable_id,
                    )
                )

    elif formula_type == "linear_blend":
        terms = _as_sequence(formula.get("terms"))
        weight_sum = 0.0
        for index, term in enumerate(terms):
            term_map = _as_mapping(term)
            if term_map is None:
                continue
            metric = _non_empty(term_map.get("metric"))
            if metric:
                dependency_graph.setdefault(formula_name, set()).add(metric)
            weight = term_map.get("weight")
            if _is_finite_number(weight):
                weight_sum += float(weight)
            elif weight is not None:
                findings.append(
                    _finite_number_finding(
                        f"{path}/terms/{index}/weight",
                        "formula weight must be a finite number, not bool or non-finite",
                    )
                )
        if terms and not math.isclose(weight_sum, 1.0, abs_tol=_WEIGHT_SUM_TOLERANCE):
            findings.append(
                _finding(
                    "CV-081",
                    "semantic",
                    "blocker",
                    f"{path}/terms",
                    f"linear_blend weights must sum to 1.0 ± {_WEIGHT_SUM_TOLERANCE}; got {weight_sum}",
                )
            )

    elif formula_type == "identity":
        source = _non_empty(formula.get("source"))
        if source:
            dependency_graph.setdefault(formula_name, set()).add(source)

    return findings


def _collect_classifier_reference_findings(
    document: Mapping[str, Any],
    *,
    outcome_ids: set[str],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}

    for cap_kind in ("severeCaps", "moderateCaps"):
        for cap_index, cap in enumerate(_as_sequence(classifier.get(cap_kind))):
            cap_map = _as_mapping(cap)
            if cap_map is None:
                continue
            effect = _as_mapping(cap_map.get("effect")) or {}
            for key in ("forceOutcomeId", "maxOutcomeId"):
                outcome_id = _non_empty(effect.get(key))
                if outcome_id and outcome_id not in outcome_ids:
                    findings.append(
                        _finding(
                            "CV-086",
                            "semantic",
                            "blocker",
                            _json_pointer("outcomeClassifier", cap_kind, cap_index, "effect", key),
                            f"{key} references unknown outcomeId {outcome_id!r}",
                            identifier=outcome_id,
                        )
                    )

    for guard_index, guard in enumerate(_as_sequence(classifier.get("strongGuards"))):
        guard_map = _as_mapping(guard)
        if guard_map is None:
            continue
        effect = _as_mapping(guard_map.get("effect")) or {}
        for outcome_index, outcome_id_raw in enumerate(_as_sequence(effect.get("disqualifyOutcomeIds"))):
            outcome_id = _non_empty(outcome_id_raw)
            if outcome_id and outcome_id not in outcome_ids:
                findings.append(
                    _finding(
                        "CV-086",
                        "semantic",
                        "blocker",
                        _json_pointer(
                            "outcomeClassifier",
                            "strongGuards",
                            guard_index,
                            "effect",
                            "disqualifyOutcomeIds",
                            outcome_index,
                        ),
                        f"disqualifyOutcomeIds references unknown outcomeId {outcome_id!r}",
                        identifier=outcome_id,
                    )
                )

    return findings


def _collect_score_band_findings(
    document: Mapping[str, Any],
    *,
    outcome_ids: set[str],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    bands = list(_as_sequence(classifier.get("scoreBands")))
    if not bands:
        return findings

    normalized: list[tuple[str, float | None, float | None, str]] = []
    seen_outcomes: set[str] = set()

    for index, band in enumerate(bands):
        band_map = _as_mapping(band)
        if band_map is None:
            continue
        path = _json_pointer("outcomeClassifier", "scoreBands", index)
        outcome_id = _non_empty(band_map.get("outcomeId"))
        if outcome_id:
            if outcome_id in seen_outcomes:
                findings.append(
                    _finding(
                        "CV-087",
                        "semantic",
                        "blocker",
                        f"{path}/outcomeId",
                        f"duplicate scoreBands outcomeId {outcome_id!r}",
                        identifier=outcome_id,
                    )
                )
            seen_outcomes.add(outcome_id)
            if outcome_id not in outcome_ids:
                findings.append(
                    _finding(
                        "CV-086",
                        "semantic",
                        "blocker",
                        f"{path}/outcomeId",
                        f"scoreBands outcomeId {outcome_id!r} does not resolve",
                        identifier=outcome_id,
                    )
                )

        min_inclusive = band_map.get("minInclusive")
        max_exclusive = band_map.get("maxExclusive")
        min_value = float(min_inclusive) if isinstance(min_inclusive, (int, float)) else None
        max_value = float(max_exclusive) if isinstance(max_exclusive, (int, float)) else None
        if min_value is not None and max_value is not None and min_value >= max_value:
            findings.append(
                _finding(
                    "CV-087",
                    "semantic",
                    "blocker",
                    path,
                    "score band minInclusive must be less than maxExclusive when both are present",
                )
            )
        normalized.append((path, min_value, max_value, outcome_id))

    normalized.sort(key=lambda item: (-math.inf if item[1] is None else item[1], item[0]))

    if normalized and normalized[0][1] is not None:
        findings.append(
            _finding(
                "CV-087",
                "semantic",
                "blocker",
                normalized[0][0],
                "score bands must cover -∞; lowest band minInclusive must be null",
            )
        )
    if normalized and normalized[-1][2] is not None:
        findings.append(
            _finding(
                "CV-087",
                "semantic",
                "blocker",
                normalized[-1][0],
                "score bands must cover +∞; highest band maxExclusive must be null",
            )
        )

    for index in range(len(normalized) - 1):
        left_path, _, left_max, _ = normalized[index]
        right_path, right_min, _, _ = normalized[index + 1]
        if left_max is None or right_min is None:
            findings.append(
                _finding(
                    "CV-087",
                    "semantic",
                    "blocker",
                    right_path,
                    "adjacent score bands must define contiguous finite boundaries",
                )
            )
            continue
        if not math.isclose(left_max, right_min, rel_tol=0.0, abs_tol=0.0):
            findings.append(
                _finding(
                    "CV-087",
                    "semantic",
                    "blocker",
                    right_path,
                    f"score band gap or overlap between {left_path} and {right_path}",
                )
            )

    return findings


def _collect_outcome_reference_findings(
    document: Mapping[str, Any],
    *,
    outcome_ids: set[str],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    declared = _outcome_ids(document)
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    referenced: set[str] = {
        _non_empty((_as_mapping(band) or {}).get("outcomeId"))
        for band in _as_sequence(classifier.get("scoreBands"))
        if _as_mapping(band) is not None
    }
    referenced.discard("")

    for cap_kind in ("severeCaps", "moderateCaps"):
        for cap in _as_sequence(classifier.get(cap_kind)):
            cap_map = _as_mapping(cap)
            if cap_map is None:
                continue
            effect = _as_mapping(cap_map.get("effect")) or {}
            for key in ("forceOutcomeId", "maxOutcomeId"):
                outcome_id = _non_empty(effect.get(key))
                if outcome_id:
                    referenced.add(outcome_id)

    for outcome_id, path in declared.items():
        if outcome_id not in referenced:
            findings.append(
                _finding(
                    "CV-089R",
                    "semantic",
                    "blocker",
                    path,
                    (
                        f"outcome {outcome_id!r} must appear in scoreBands or cap "
                        "forceOutcomeId/maxOutcomeId"
                    ),
                    identifier=outcome_id,
                )
            )
    return findings


def _build_union_adjacency(scene_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {scene_id: set() for scene_id in scene_by_id}
    for scene_id, scene in scene_by_id.items():
        options = _as_sequence((_as_mapping(scene.get("decision")) or {}).get("options"))
        for option in options:
            option_map = _as_mapping(option)
            if option_map is None:
                continue
            routing = _as_mapping(option_map.get("routing")) or {}
            primary = _non_empty(routing.get("primaryNextSceneId"))
            if primary and primary != TERMINAL_SENTINEL:
                adjacency[scene_id].add(primary)
            corrective_route = _as_mapping(routing.get("correctiveRoute"))
            if corrective_route is not None:
                corrective_scene_id = _non_empty(corrective_route.get("correctiveSceneId"))
                skip_target = _non_empty(corrective_route.get("whenCorrectiveSkippedNextSceneId"))
                if corrective_scene_id:
                    adjacency[scene_id].add(corrective_scene_id)
                if skip_target:
                    adjacency[scene_id].add(skip_target)
    return adjacency


def _detect_graph_cycles(adjacency: Mapping[str, set[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle_nodes: list[str] = []

    def visit(node: str) -> None:
        if node in visiting:
            cycle_nodes.append(node)
            return
        if node in visited:
            return
        visiting.add(node)
        for neighbor in adjacency.get(node, set()):
            visit(neighbor)
        visiting.remove(node)
        visited.add(node)

    for node in adjacency:
        if node not in visited:
            visit(node)
    return cycle_nodes


def _reachable_nodes(start_scene: str, adjacency: Mapping[str, set[str]]) -> set[str]:
    reachable = {start_scene}
    queue = deque([start_scene])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency.get(current, set()):
            if neighbor not in reachable:
                reachable.add(neighbor)
                queue.append(neighbor)
    return reachable


def _corrective_reconvergence(scene: Mapping[str, Any]) -> str | None:
    metadata = _as_mapping(scene.get("correctiveMetadata"))
    if metadata is not None:
        reconvergence = _non_empty(metadata.get("reconvergenceSceneId"))
        if reconvergence:
            return reconvergence
    options = _as_sequence((_as_mapping(scene.get("decision")) or {}).get("options"))
    targets = {
        _non_empty((_as_mapping(option) or {}).get("routing", {}).get("primaryNextSceneId"))  # type: ignore[union-attr]
        for option in options
        if _as_mapping(option) is not None
    }
    targets.discard("")
    if len(targets) == 1:
        return next(iter(targets))
    return None


def _collect_graph_findings(document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    scene_by_id = _scene_maps(document)
    if not scene_by_id:
        return findings

    scene_ids = set(scene_by_id)
    start_scene = _non_empty(document.get("startScene"))

    for scene_index, scene in enumerate(_as_sequence(document.get("scenes"))):
        scene_map = _as_mapping(scene)
        if scene_map is None:
            continue
        scene_id = _non_empty(scene_map.get("id"))
        scene_type = _non_empty(scene_map.get("sceneType"))
        options = _as_sequence((_as_mapping(scene_map.get("decision")) or {}).get("options"))

        if scene_type == "corrective":
            reconv_targets = {
                _non_empty((_as_mapping(option) or {}).get("routing", {}).get("primaryNextSceneId"))  # type: ignore[union-attr]
                for option in options
                if _as_mapping(option) is not None
            }
            reconv_targets.discard("")
            if len(reconv_targets) > 1:
                findings.append(
                    _finding(
                        "CV-064",
                        "graph",
                        "blocker",
                        _json_pointer("scenes", scene_index),
                        "corrective scene options must share the same reconvergence target",
                        identifier=scene_id,
                    )
                )
            for option_index, option in enumerate(options):
                option_map = _as_mapping(option)
                if option_map is None:
                    continue
                target = _non_empty((_as_mapping(option_map.get("routing")) or {}).get("primaryNextSceneId"))
                if target and target in scene_by_id and _non_empty(scene_by_id[target].get("sceneType")) == "corrective":
                    findings.append(
                        _finding(
                            "CV-065",
                            "graph",
                            "blocker",
                            _json_pointer(
                                "scenes",
                                scene_index,
                                "decision",
                                "options",
                                option_index,
                                "routing",
                                "primaryNextSceneId",
                            ),
                            "corrective→corrective routing is forbidden",
                            identifier=target,
                        )
                    )

        for option_index, option in enumerate(options):
            option_map = _as_mapping(option)
            if option_map is None:
                continue
            option_base = _json_pointer("scenes", scene_index, "decision", "options", option_index, "routing")
            routing = _as_mapping(option_map.get("routing")) or {}
            primary = _non_empty(routing.get("primaryNextSceneId"))
            if primary and primary != TERMINAL_SENTINEL and primary not in scene_ids:
                findings.append(
                    _finding(
                        "CV-060",
                        "graph",
                        "blocker",
                        f"{option_base}/primaryNextSceneId",
                        f"primaryNextSceneId {primary!r} does not resolve",
                        identifier=primary,
                    )
                )

            corrective_route = _as_mapping(routing.get("correctiveRoute"))
            if corrective_route is None:
                continue

            for edge_key in ("correctiveSceneId", "whenCorrectiveSkippedNextSceneId", "reconvergenceSceneId"):
                target = _non_empty(corrective_route.get(edge_key))
                if target and target != TERMINAL_SENTINEL and target not in scene_ids:
                    findings.append(
                        _finding(
                            "CV-060",
                            "graph",
                            "blocker",
                            f"{option_base}/correctiveRoute/{edge_key}",
                            f"{edge_key} target {target!r} does not resolve",
                            identifier=target,
                        )
                    )

            corrective_scene_id = _non_empty(corrective_route.get("correctiveSceneId"))
            if corrective_scene_id and corrective_scene_id in scene_by_id:
                corrective_scene = scene_by_id[corrective_scene_id]
                if _non_empty(corrective_scene.get("sceneType")) == "corrective":
                    reconv = _corrective_reconvergence(corrective_scene)
                    if reconv and reconv in scene_by_id and _non_empty(scene_by_id[reconv].get("sceneType")) == "corrective":
                        findings.append(
                            _finding(
                                "CV-065",
                                "graph",
                                "blocker",
                                f"{option_base}/correctiveRoute/correctiveSceneId",
                                "corrective→corrective routing is forbidden via reconvergence target",
                                identifier=corrective_scene_id,
                            )
                        )

    adjacency = _build_union_adjacency(scene_by_id)
    cycle_nodes = _detect_graph_cycles(adjacency)
    if cycle_nodes:
        findings.append(
            _finding(
                "CV-072",
                "graph",
                "blocker",
                "/scenes",
                f"union routing graph contains a cycle involving {cycle_nodes[0]!r}",
                identifier=cycle_nodes[0],
            )
        )

    if start_scene and start_scene in scene_ids:
        reachable = _reachable_nodes(start_scene, adjacency)
        for scene_id, scene in scene_by_id.items():
            if _non_empty(scene.get("sceneType")) != "core":
                continue
            if scene_id not in reachable:
                findings.append(
                    _finding(
                        "CV-073",
                        "graph",
                        "blocker",
                        "/startScene",
                        f"core scene {scene_id!r} is unreachable from startScene",
                        identifier=scene_id,
                    )
                )

    budget_policy = _as_mapping(document.get("correctiveBudgetPolicy")) or {}
    max_scored = budget_policy.get("maxScoredDecisions")
    min_scored = budget_policy.get("minScoredDecisions")
    max_corrective = budget_policy.get("maxExperiencedCorrectiveScenes")
    if (
        not cycle_nodes
        and start_scene
        and start_scene in scene_ids
        and isinstance(max_scored, int)
        and not isinstance(max_scored, bool)
        and isinstance(min_scored, int)
        and not isinstance(min_scored, bool)
    ):
        min_path, max_path = _compute_scored_path_bounds(
            scene_by_id,
            start_scene=start_scene,
            max_experienced=int(max_corrective) if isinstance(max_corrective, int) and not isinstance(max_corrective, bool) else 0,
        )
        if max_path > max_scored:
            findings.append(
                _finding(
                    "CV-074",
                    "graph",
                    "blocker",
                    "/correctiveBudgetPolicy/maxScoredDecisions",
                    f"maximum scored decision path length {max_path} exceeds maxScoredDecisions {max_scored}",
                )
            )
        if min_path < min_scored:
            findings.append(
                _finding(
                    "CV-075",
                    "graph",
                    "blocker",
                    "/correctiveBudgetPolicy/minScoredDecisions",
                    f"minimum scored decision path length {min_path} is below minScoredDecisions {min_scored}",
                )
            )

    return findings


def _compute_scored_path_bounds(
    scene_by_id: Mapping[str, Mapping[str, Any]],
    *,
    start_scene: str,
    max_experienced: int,
) -> tuple[int, int]:
    memo_min: dict[tuple[str, int], int] = {}
    memo_max: dict[tuple[str, int], int] = {}

    def min_steps(scene_id: str, corrective_used: int) -> int:
        key = (scene_id, corrective_used)
        cached = memo_min.get(key)
        if cached is not None:
            return cached
        memo_min[key] = math.inf

        scene = scene_by_id.get(scene_id)
        if scene is None:
            return math.inf

        options = _as_sequence((_as_mapping(scene.get("decision")) or {}).get("options"))
        if not options:
            memo_min[key] = 1
            return 1

        best = math.inf
        for option in options:
            option_map = _as_mapping(option)
            if option_map is None:
                continue
            routing = _as_mapping(option_map.get("routing")) or {}
            primary = _non_empty(routing.get("primaryNextSceneId"))
            if primary == TERMINAL_SENTINEL:
                best = min(best, 1)
                continue
            if primary in scene_by_id:
                best = min(best, 1 + min_steps(primary, corrective_used))

        result = 1 if best is math.inf else int(best)
        memo_min[key] = result
        return result

    def max_steps(scene_id: str, corrective_used: int) -> int:
        key = (scene_id, corrective_used)
        cached = memo_max.get(key)
        if cached is not None:
            return cached
        memo_max[key] = 0

        scene = scene_by_id.get(scene_id)
        if scene is None:
            return 0

        options = _as_sequence((_as_mapping(scene.get("decision")) or {}).get("options"))
        if not options:
            memo_max[key] = 1
            return 1

        best = 0
        for option in options:
            option_map = _as_mapping(option)
            if option_map is None:
                continue
            routing = _as_mapping(option_map.get("routing")) or {}
            primary = _non_empty(routing.get("primaryNextSceneId"))
            candidates: list[int] = []

            if primary == TERMINAL_SENTINEL:
                candidates.append(1)
            elif primary in scene_by_id:
                child = max_steps(primary, corrective_used)
                if child > 0:
                    candidates.append(1 + child)

            corrective_route = _as_mapping(routing.get("correctiveRoute"))
            if corrective_route is not None and corrective_used < max_experienced:
                corrective_scene_id = _non_empty(corrective_route.get("correctiveSceneId"))
                corrective_scene = scene_by_id.get(corrective_scene_id or "")
                if corrective_scene is not None:
                    reconvergence = _corrective_reconvergence(corrective_scene)
                    if reconvergence == TERMINAL_SENTINEL:
                        candidates.append(2)
                    elif reconvergence in scene_by_id:
                        child = max_steps(reconvergence, corrective_used + 1)
                        if child > 0:
                            candidates.append(2 + child)

            if candidates:
                best = max(best, max(candidates))

        result = best if best > 0 else 1
        memo_max[key] = result
        return result

    return min_steps(start_scene, 0), max_steps(start_scene, 0)


def _compare_values(actual: float, op: str, expected: float) -> bool:
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
    return False


def _evaluate_reachability_condition(
    condition: Mapping[str, Any],
    *,
    flags: frozenset[str],
    state: Mapping[str, float],
    counters: Mapping[str, int],
) -> bool:
    if "all" in condition:
        children = _as_sequence(condition.get("all"))
        return all(
            _evaluate_reachability_condition(
                child_map,
                flags=flags,
                state=state,
                counters=counters,
            )
            for child in children
            if (child_map := _as_mapping(child)) is not None
        )
    if "any" in condition:
        children = _as_sequence(condition.get("any"))
        return any(
            _evaluate_reachability_condition(
                child_map,
                flags=flags,
                state=state,
                counters=counters,
            )
            for child in children
            if (child_map := _as_mapping(child)) is not None
        )
    if "not" in condition:
        child_map = _as_mapping(condition.get("not"))
        if child_map is None:
            return True
        return not _evaluate_reachability_condition(
            child_map,
            flags=flags,
            state=state,
            counters=counters,
        )
    if "flagSet" in condition:
        return _non_empty(condition.get("flagSet")) in flags
    if "flagNotSet" in condition:
        return _non_empty(condition.get("flagNotSet")) not in flags
    state_compare = _as_mapping(condition.get("stateCompare"))
    if state_compare is not None:
        variable_id = _non_empty(state_compare.get("variableId"))
        op = _non_empty(state_compare.get("op"))
        value = state_compare.get("value")
        if not variable_id or not op or not _is_finite_number(value):
            return False
        actual = float(state.get(variable_id, 0.0))
        return _compare_values(actual, op, float(value))
    counter_compare = _as_mapping(condition.get("counterCompare"))
    if counter_compare is not None:
        counter_id = _non_empty(counter_compare.get("counterId"))
        op = _non_empty(counter_compare.get("op"))
        value = counter_compare.get("value")
        if not counter_id or not op or not _is_finite_number(value):
            return False
        actual = float(counters.get(counter_id, 0))
        return _compare_values(actual, op, float(value))
    return False


def _state_variable_bounds(document: Mapping[str, Any]) -> dict[str, tuple[float | None, float | None]]:
    bounds: dict[str, tuple[float | None, float | None]] = {}
    for variable in _as_sequence(document.get("stateVariables")):
        variable_map = _as_mapping(variable)
        if variable_map is None:
            continue
        key = _non_empty(variable_map.get("key"))
        if not key:
            continue
        minimum = variable_map.get("minimum")
        maximum = variable_map.get("maximum")
        min_value = float(minimum) if _is_finite_number(minimum) else None
        max_value = float(maximum) if _is_finite_number(maximum) else None
        bounds[key] = (min_value, max_value)
    return bounds


def _clamp_state_value(key: str, value: float, bounds: Mapping[str, tuple[float | None, float | None]]) -> float:
    minimum, maximum = bounds.get(key, (None, None))
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _initial_flags(document: Mapping[str, Any]) -> frozenset[str]:
    active: set[str] = set()
    for flag in _as_sequence(document.get("flags")):
        flag_map = _as_mapping(flag)
        if flag_map is None:
            continue
        flag_id = _non_empty(flag_map.get("flagId"))
        if flag_id and bool(flag_map.get("initialValue")):
            active.add(flag_id)
    return frozenset(active)


def _initial_counters(document: Mapping[str, Any]) -> dict[str, int]:
    counters: dict[str, int] = {}
    for counter in _as_sequence(document.get("runtimeCounters")):
        counter_map = _as_mapping(counter)
        if counter_map is None:
            continue
        counter_id = _non_empty(counter_map.get("counterId"))
        if not counter_id:
            continue
        initial_value = counter_map.get("initialValue", 0)
        counters[counter_id] = int(initial_value) if _is_finite_number(initial_value) else 0
    return counters


def _counter_specs(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    specs: dict[str, Mapping[str, Any]] = {}
    for counter in _as_sequence(document.get("runtimeCounters")):
        counter_map = _as_mapping(counter)
        if counter_map is None:
            continue
        counter_id = _non_empty(counter_map.get("counterId"))
        if counter_id:
            specs[counter_id] = counter_map
    return specs


def _clamp_counter_value(counter_id: str, value: int, specs: Mapping[str, Mapping[str, Any]]) -> int:
    spec = specs.get(counter_id)
    if spec is None:
        return value
    minimum = spec.get("minimum")
    maximum = spec.get("maximum")
    if _is_finite_number(minimum):
        value = max(int(minimum), value)
    if _is_finite_number(maximum):
        value = min(int(maximum), value)
    return value


def _increment_counters_for_decision(
    counters: dict[str, int],
    *,
    tier: str,
    entered_corrective: bool,
    specs: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    updated = dict(counters)
    for counter_id, spec in specs.items():
        for rule in _as_sequence(spec.get("incrementOn")):
            rule_map = _as_mapping(rule)
            if rule_map is None:
                continue
            event = _non_empty(rule_map.get("event"))
            if event == "decision_applied":
                when_tier = _non_empty(rule_map.get("whenTier"))
                if when_tier and when_tier != tier:
                    continue
                updated[counter_id] = _clamp_counter_value(counter_id, updated.get(counter_id, 0) + 1, specs)
            elif event == "corrective_scene_entered" and entered_corrective:
                updated[counter_id] = _clamp_counter_value(counter_id, updated.get(counter_id, 0) + 1, specs)
    return updated


def _resolve_routing(
    option_map: Mapping[str, Any],
    *,
    flags: frozenset[str],
    state: Mapping[str, float],
    counters: Mapping[str, int],
) -> tuple[str, bool]:
    routing = _as_mapping(option_map.get("routing")) or {}
    if routing.get("terminal") is True:
        return TERMINAL_SENTINEL, False

    primary = _non_empty(routing.get("primaryNextSceneId"))
    corrective_route = _as_mapping(routing.get("correctiveRoute"))
    if corrective_route is None:
        return primary, False

    tier = _non_empty(option_map.get("evaluationTier"))
    trigger_tiers = {
        _non_empty(value)
        for value in _as_sequence(corrective_route.get("triggerOnTiers"))
    }
    trigger_tiers.discard("")
    if tier not in trigger_tiers:
        return primary, False

    budget_condition = _as_mapping(corrective_route.get("budgetCondition"))
    if budget_condition is not None and _evaluate_reachability_condition(
        budget_condition,
        flags=flags,
        state=state,
        counters=counters,
    ):
        corrective_scene_id = _non_empty(corrective_route.get("correctiveSceneId"))
        return corrective_scene_id or primary, True

    skip_target = _non_empty(corrective_route.get("whenCorrectiveSkippedNextSceneId"))
    return skip_target or primary, False


def _dimension_health(
    raw: float,
    *,
    minimum: float | None,
    maximum: float | None,
    polarity: str,
) -> float:
    lo = minimum if minimum is not None else 0.0
    hi = maximum if maximum is not None else 100.0
    span = hi - lo
    if span <= 0:
        return raw
    if polarity == "higher_is_worse":
        return (hi - raw) / span * 100.0
    return (raw - lo) / span * 100.0


def _compute_positive_health(
    document: Mapping[str, Any],
    *,
    state: Mapping[str, float],
) -> float:
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    formula = _as_mapping(classifier.get("positiveHealthFormula")) or {}
    if _non_empty(formula.get("type")) != "weighted_dimension_health":
        return 0.0
    bounds = _state_variable_bounds(document)
    polarities = {
        _non_empty(variable_map.get("key")): _non_empty(variable_map.get("polarity"))
        for variable in _as_sequence(document.get("stateVariables"))
        if (variable_map := _as_mapping(variable)) is not None and _non_empty(variable_map.get("key"))
    }
    values: list[float] = []
    for dimension in _as_sequence(formula.get("dimensions")):
        dimension_map = _as_mapping(dimension)
        if dimension_map is None:
            continue
        variable_id = _non_empty(dimension_map.get("variableId"))
        if not variable_id:
            continue
        polarity = _non_empty(dimension_map.get("polarity")) or polarities.get(variable_id, "higher_is_better")
        minimum, maximum = bounds.get(variable_id, (None, None))
        values.append(
            _dimension_health(
                float(state.get(variable_id, 0.0)),
                minimum=minimum,
                maximum=maximum,
                polarity=polarity,
            )
        )
    if not values:
        return 0.0
    return sum(values) / len(values)


def _compute_decision_quality(document: Mapping[str, Any], *, tier_history: Sequence[str]) -> float:
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    formula = _as_mapping(classifier.get("decisionQualityFormula")) or {}
    if _non_empty(formula.get("type")) != "tier_average":
        return 0.0
    tier_points = _as_mapping(classifier.get("tierPoints")) or {}
    scored_count = len(tier_history)
    if scored_count == 0:
        return 0.0
    total = sum(float(tier_points.get(tier, 0)) for tier in tier_history)
    return total / scored_count


def _compute_composite(
    document: Mapping[str, Any],
    *,
    state: Mapping[str, float],
    tier_history: Sequence[str],
) -> float:
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    formula = _as_mapping(classifier.get("compositeFormula")) or {}
    formula_type = _non_empty(formula.get("type"))
    metrics = {
        "positiveHealth": _compute_positive_health(document, state=state),
        "decisionQuality": _compute_decision_quality(document, tier_history=tier_history),
    }
    if formula_type == "identity":
        source = _non_empty(formula.get("source"))
        return metrics.get(source, 0.0)
    if formula_type != "linear_blend":
        return metrics["positiveHealth"]
    total = 0.0
    for term in _as_sequence(formula.get("terms")):
        term_map = _as_mapping(term)
        if term_map is None:
            continue
        metric = _non_empty(term_map.get("metric"))
        weight = term_map.get("weight")
        if metric and _is_finite_number(weight):
            total += metrics.get(metric, 0.0) * float(weight)
    return total


def _outcome_rank_by_id(document: Mapping[str, Any]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for outcome in _as_sequence(document.get("outcomes")):
        outcome_map = _as_mapping(outcome)
        if outcome_map is None:
            continue
        outcome_id = _non_empty(outcome_map.get("outcomeId"))
        rank = outcome_map.get("classificationRank")
        if outcome_id and isinstance(rank, int) and not isinstance(rank, bool):
            ranks[outcome_id] = rank
    return ranks


def _select_score_band_outcome(document: Mapping[str, Any], composite: float) -> str | None:
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    for band in _as_sequence(classifier.get("scoreBands")):
        band_map = _as_mapping(band)
        if band_map is None:
            continue
        outcome_id = _non_empty(band_map.get("outcomeId"))
        min_inclusive = band_map.get("minInclusive")
        max_exclusive = band_map.get("maxExclusive")
        min_value = float(min_inclusive) if _is_finite_number(min_inclusive) else None
        max_value = float(max_exclusive) if _is_finite_number(max_exclusive) else None
        if min_value is not None and composite < min_value:
            continue
        if max_value is not None and composite >= max_value:
            continue
        return outcome_id or None
    return None


def _classify_outcome_v1_seven_step(
    document: Mapping[str, Any],
    *,
    flags: frozenset[str],
    state: Mapping[str, float],
    counters: Mapping[str, int],
    tier_history: Sequence[str],
) -> str | None:
    classifier = _as_mapping(document.get("outcomeClassifier")) or {}
    outcome_ranks = _outcome_rank_by_id(document)

    for cap in _as_sequence(classifier.get("severeCaps")):
        cap_map = _as_mapping(cap)
        if cap_map is None:
            continue
        condition = _as_mapping(cap_map.get("when"))
        effect = _as_mapping(cap_map.get("effect")) or {}
        if condition is not None and _evaluate_reachability_condition(
            condition,
            flags=flags,
            state=state,
            counters=counters,
        ):
            forced = _non_empty(effect.get("forceOutcomeId"))
            if forced:
                return forced

    max_cap_rank: int | None = None
    max_cap_outcome: str | None = None
    for cap in _as_sequence(classifier.get("moderateCaps")):
        cap_map = _as_mapping(cap)
        if cap_map is None:
            continue
        condition = _as_mapping(cap_map.get("when"))
        effect = _as_mapping(cap_map.get("effect")) or {}
        if condition is None or not _evaluate_reachability_condition(
            condition,
            flags=flags,
            state=state,
            counters=counters,
        ):
            continue
        capped = _non_empty(effect.get("maxOutcomeId"))
        if not capped:
            continue
        rank = outcome_ranks.get(capped)
        if rank is None:
            continue
        if max_cap_rank is None or rank < max_cap_rank:
            max_cap_rank = rank
            max_cap_outcome = capped

    composite = _compute_composite(document, state=state, tier_history=tier_history)
    disqualified: set[str] = set()
    for guard in _as_sequence(classifier.get("strongGuards")):
        guard_map = _as_mapping(guard)
        if guard_map is None:
            continue
        condition = _as_mapping(guard_map.get("when"))
        effect = _as_mapping(guard_map.get("effect")) or {}
        if condition is not None and _evaluate_reachability_condition(
            condition,
            flags=flags,
            state=state,
            counters=counters,
        ):
            for outcome_id_raw in _as_sequence(effect.get("disqualifyOutcomeIds")):
                outcome_id = _non_empty(outcome_id_raw)
                if outcome_id:
                    disqualified.add(outcome_id)

    selected = _select_score_band_outcome(document, composite)
    if selected is None:
        return None
    if selected in disqualified:
        selected_rank = outcome_ranks.get(selected)
        if selected_rank is not None:
            for candidate_id, candidate_rank in sorted(outcome_ranks.items(), key=lambda item: item[1]):
                if candidate_rank > selected_rank and candidate_id not in disqualified:
                    selected = candidate_id
                    break
        if selected in disqualified:
            return None

    if max_cap_rank is not None:
        selected_rank = outcome_ranks.get(selected)
        if selected_rank is not None and selected_rank < max_cap_rank and max_cap_outcome is not None:
            return max_cap_outcome
    return selected


def _collect_bounded_outcome_reachability_findings(document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    declared_outcomes = _outcome_ids(document)
    if not declared_outcomes:
        return findings

    scene_by_id = _scene_maps(document)
    start_scene = _non_empty(document.get("startScene"))
    if not start_scene or start_scene not in scene_by_id:
        return findings

    bounds = _state_variable_bounds(document)
    counter_specs = _counter_specs(document)
    budget_policy = _as_mapping(document.get("correctiveBudgetPolicy")) or {}
    max_scored_raw = budget_policy.get("maxScoredDecisions", _DEFAULT_MAX_SCORED_DECISIONS)
    max_scored = (
        int(max_scored_raw)
        if isinstance(max_scored_raw, int) and not isinstance(max_scored_raw, bool)
        else _DEFAULT_MAX_SCORED_DECISIONS
    )

    initial_state_raw = _as_mapping(document.get("initialState")) or {}
    initial_state = {
        key: float(value)
        for key, value in initial_state_raw.items()
        if _is_finite_number(value)
    }
    initial_flags = _initial_flags(document)
    initial_counters = _initial_counters(document)

    reachable_outcomes: set[str] = set()
    visited: set[tuple[Any, ...]] = set()
    stack: list[tuple[str, frozenset[str], dict[str, float], dict[str, int], tuple[str, ...], int]] = [
        (start_scene, initial_flags, initial_state, initial_counters, (), 0),
    ]
    explored = 0
    exhausted = False

    while stack:
        scene_id, flags, state, counters, tier_history, corrective_used = stack.pop()
        state_key = (
            scene_id,
            flags,
            tuple(sorted(state.items())),
            tuple(sorted((key, int(value)) for key, value in counters.items())),
            tier_history,
            corrective_used,
        )
        if state_key in visited:
            continue
        visited.add(state_key)
        explored += 1
        if explored > _MAX_REACHABILITY_STATES:
            exhausted = True
            break

        scene = scene_by_id.get(scene_id)
        if scene is None:
            continue

        options = _as_sequence((_as_mapping(scene.get("decision")) or {}).get("options"))
        for option in options:
            option_map = _as_mapping(option)
            if option_map is None:
                continue

            next_state = dict(state)
            for key, delta in (_as_mapping(option_map.get("stateChanges")) or {}).items():
                if not _is_finite_number(delta):
                    continue
                current = float(next_state.get(key, 0.0))
                next_state[key] = _clamp_state_value(key, current + float(delta), bounds)

            next_flags = set(flags)
            for flag_id in _as_sequence(option_map.get("clearFlags")):
                flag_text = _non_empty(flag_id)
                if flag_text:
                    next_flags.discard(flag_text)
            for flag_id in _as_sequence(option_map.get("setFlags")):
                flag_text = _non_empty(flag_id)
                if flag_text:
                    next_flags.add(flag_text)
            frozen_flags = frozenset(next_flags)

            tier = _non_empty(option_map.get("evaluationTier"))
            next_tier_history = tier_history + ((tier,) if tier else ())
            if len(next_tier_history) > max_scored:
                continue

            next_counters = dict(counters)
            next_counters = _increment_counters_for_decision(
                next_counters,
                tier=tier,
                entered_corrective=False,
                specs=counter_specs,
            )

            next_scene, entered_corrective = _resolve_routing(
                option_map,
                flags=frozen_flags,
                state=next_state,
                counters=next_counters,
            )
            if entered_corrective:
                next_counters = _increment_counters_for_decision(
                    next_counters,
                    tier=tier,
                    entered_corrective=True,
                    specs=counter_specs,
                )
                next_corrective_used = corrective_used + 1
            else:
                next_corrective_used = corrective_used

            if next_scene == TERMINAL_SENTINEL:
                outcome_id = _classify_outcome_v1_seven_step(
                    document,
                    flags=frozen_flags,
                    state=next_state,
                    counters=next_counters,
                    tier_history=next_tier_history,
                )
                if outcome_id:
                    reachable_outcomes.add(outcome_id)
                continue

            if next_scene in scene_by_id:
                stack.append(
                    (
                        next_scene,
                        frozen_flags,
                        next_state,
                        next_counters,
                        next_tier_history,
                        next_corrective_used,
                    )
                )

    if exhausted:
        findings.append(
            _finding(
                "CV-089",
                "publication",
                "blocker",
                "/outcomes",
                "bounded outcome reachability analysis exceeded safe limits",
            )
        )
        return findings

    for outcome_id, path in declared_outcomes.items():
        if outcome_id not in reachable_outcomes:
            findings.append(
                _finding(
                    "CV-089",
                    "publication",
                    "blocker",
                    path,
                    f"outcome {outcome_id!r} is not reachable via bounded path analysis",
                    identifier=outcome_id,
                )
            )
    return findings


def _collect_publication_findings_without_reachability(document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []

    schema_version = _non_empty(document.get("schemaVersion"))
    if schema_version and schema_version != SCHEMA_VERSION_1_1:
        findings.append(
            _finding(
                "CV-001",
                "publication",
                "blocker",
                "/schemaVersion",
                f"schemaVersion must be {SCHEMA_VERSION_1_1!r}",
                identifier=schema_version,
            )
        )

    engine_version = _non_empty(document.get("requiredEngineVersion"))
    if not engine_version:
        findings.append(
            _finding(
                "CV-101",
                "publication",
                "blocker",
                "/requiredEngineVersion",
                "requiredEngineVersion is required for publication",
            )
        )
    elif engine_version not in SUPPORTED_ENGINE_VERSIONS_V1_1:
        findings.append(
            _finding(
                "CV-102",
                "publication",
                "blocker",
                "/requiredEngineVersion",
                f"unsupported requiredEngineVersion {engine_version!r}",
                identifier=engine_version,
            )
        )

    stored_hash = document.get("canonicalContentSha256")
    if not isinstance(stored_hash, str) or not stored_hash.strip():
        findings.append(
            _finding(
                "CV-HASH",
                "publication",
                "blocker",
                "/canonicalContentSha256",
                "canonicalContentSha256 is required for publication",
            )
        )
    else:
        computed = compute_canonical_content_sha256_v1_1(document)
        if stored_hash.strip().lower() != computed:
            findings.append(
                _finding(
                    "PB-HASH",
                    "publication",
                    "blocker",
                    "/canonicalContentSha256",
                    "canonicalContentSha256 does not match computed canonical digest",
                    identifier=stored_hash,
                )
            )

    # CV-007/008 catalog-context checks are intentionally skipped here when no
    # catalog metadata is available at validation time.
    return findings


def _collect_publication_findings(document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings = _collect_publication_findings_without_reachability(document)
    findings.extend(_collect_bounded_outcome_reachability_findings(document))
    return findings


def validate_v1_1_scenario_document(
    document: Mapping[str, Any],
    *,
    publication: bool = False,
) -> tuple[ValidationFinding, ...]:
    findings: list[ValidationFinding] = []
    if not isinstance(document, Mapping):
        findings.append(
            _finding(
                "JS-SCHEMA",
                "json_schema",
                "blocker",
                "/",
                "scenario document root must be a JSON object",
            )
        )
        return sort_validation_findings(findings)

    findings.extend(collect_v1_1_json_schema_findings(document))
    structural = _collect_structural_findings(document)
    semantic = _collect_semantic_findings(document)
    graph = _collect_graph_findings(document)
    findings.extend(structural)
    findings.extend(semantic)
    findings.extend(graph)
    if publication:
        findings.extend(_collect_publication_findings_without_reachability(document))
        earlier_layers = [*structural, *semantic, *graph]
        if not findings_contain_blocking(earlier_layers):
            findings.extend(_collect_bounded_outcome_reachability_findings(document))

    return sort_validation_findings(findings)


def validate_v1_1_scenario_for_publication(document: Mapping[str, Any]) -> tuple[ValidationFinding, ...]:
    return validate_v1_1_scenario_document(document, publication=True)
