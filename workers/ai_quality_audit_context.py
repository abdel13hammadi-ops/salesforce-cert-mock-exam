"""
Read-only V48 AI quality audit context loaders.

Loads and strictly normalizes blind Pass A context and post-Pass-A comparison
context using the V48 RPC migration as the source of truth for RPC names,
parameters, and return shapes.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from workers.certification_registry import (
    CertificationRegistryError,
    validate_frozen_audit_context_certification,
)

BLIND_CONTEXT_RPC = "get_question_version_blind_context_v1"
COMPARISON_CONTEXT_RPC = "get_question_version_comparison_context_v1"

_BLIND_RPC_PARAM = "p_question_version_id"
_COMPARISON_RPC_PARAMS = ("p_question_version_id", "p_audit_run_id")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_BLIND_LEAKAGE_KEYS = frozenset({
    "explanation",
    "stored_correct_option_labels",
    "pass_a_selected_option_labels",
    "frozen_evidence",
    "proposed_findings",
    "is_correct",
    "correct_option_labels",
    "stored_correct_answers",
    "answer_key",
    "result_json",
    "evidence_set_hash",
})

_BLIND_OPTION_KEYS = frozenset({"option_label", "option_text", "display_order"})
_COMPARISON_OPTION_KEYS = frozenset({"option_label", "option_text", "display_order", "is_correct"})


class AiQualityAuditContextError(ValueError):
    """Raised when AI quality audit context loading or normalization fails."""


def load_blind_audit_context(
    client,
    question_version_id: str,
    *,
    audit_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Load and normalize redacted Pass A context for one question version."""
    requested_qvid = _validate_uuid(question_version_id, "question_version_id")
    requested_run_id: Optional[str] = None
    if audit_run_id is not None:
        requested_run_id = _validate_uuid(audit_run_id, "audit_run_id")
        _assert_audit_run_targets_question_version(
            client,
            audit_run_id=requested_run_id,
            question_version_id=requested_qvid,
        )

    row = _call_rpc_single(
        client,
        BLIND_CONTEXT_RPC,
        {_BLIND_RPC_PARAM: requested_qvid},
    )
    _reject_blind_leakage(row, prefix=BLIND_CONTEXT_RPC)
    normalized = _normalize_blind_context_row(
        row,
        requested_question_version_id=requested_qvid,
    )
    try:
        validate_frozen_audit_context_certification(
            certification_exam_name=normalized["certification_exam_name"],
            domain_name=normalized["domain_name"],
        )
    except CertificationRegistryError as exc:
        raise AiQualityAuditContextError(str(exc)) from exc
    if requested_run_id is not None:
        normalized["audit_run_id"] = requested_run_id
    return normalized


def load_comparison_audit_context(
    client,
    question_version_id: str,
    audit_run_id: str,
) -> Dict[str, Any]:
    """Load and normalize Pass B/C comparison context for one audit run."""
    requested_qvid = _validate_uuid(question_version_id, "question_version_id")
    requested_run_id = _validate_uuid(audit_run_id, "audit_run_id")
    _assert_audit_run_targets_question_version(
        client,
        audit_run_id=requested_run_id,
        question_version_id=requested_qvid,
    )

    row = _call_rpc_single(
        client,
        COMPARISON_CONTEXT_RPC,
        {
            _COMPARISON_RPC_PARAMS[0]: requested_qvid,
            _COMPARISON_RPC_PARAMS[1]: requested_run_id,
        },
    )
    normalized_question = _normalize_comparison_question_row(
        row,
        requested_question_version_id=requested_qvid,
    )
    option_labels = {
        option["option_label"] for option in normalized_question["options"]
    }
    stored_correct = _normalize_stored_correct_option_labels(
        row.get("stored_correct_option_labels"),
        option_labels=option_labels,
        prefix=f"{COMPARISON_CONTEXT_RPC}.stored_correct_option_labels",
    )
    pass_a_labels = _load_pass_a_selected_option_labels(client, requested_run_id)
    _validate_pass_a_labels_against_options(
        pass_a_labels,
        option_labels=option_labels,
    )
    frozen_evidence = _load_frozen_evidence_rows(
        client,
        audit_run_id=requested_run_id,
    )

    return {
        "audit_run_id": requested_run_id,
        "question_version_id": normalized_question["question_version_id"],
        "question_id": normalized_question["question_id"],
        "certification_exam_name": normalized_question["certification_exam_name"],
        "domain_name": normalized_question["domain_name"],
        "question_text": normalized_question["question_text"],
        "question_type": normalized_question["question_type"],
        "required_selection_count": normalized_question["required_selection_count"],
        "explanation": normalized_question["explanation"],
        "options": normalized_question["options"],
        "stored_correct_option_labels": stored_correct,
        "pass_a_selected_option_labels": pass_a_labels,
        "frozen_evidence": frozen_evidence,
    }


def _call_rpc_single(client, name: str, params: dict) -> Dict[str, Any]:
    try:
        result = client.rpc(name, params).execute()
    except Exception as exc:
        raise AiQualityAuditContextError(
            f"RPC {name!r} call failed: {exc}"
        ) from exc

    if getattr(result, "error", None):
        raise AiQualityAuditContextError(
            f"RPC {name!r} failed: {result.error}"
        )

    rows = result.data or []
    if not rows:
        raise AiQualityAuditContextError(f"RPC {name!r} returned no rows")
    if len(rows) != 1:
        raise AiQualityAuditContextError(
            f"RPC {name!r} returned {len(rows)} rows; expected exactly 1"
        )

    row = rows[0]
    if not isinstance(row, dict):
        raise AiQualityAuditContextError(
            f"RPC {name!r} returned a malformed row of type {type(row).__name__}"
        )
    return row


def _call_table(client, *, table_name: str, build_query):
    try:
        result = build_query(client.table(table_name)).execute()
    except Exception as exc:
        raise AiQualityAuditContextError(
            f"table read {table_name!r} failed: {exc}"
        ) from exc

    if getattr(result, "error", None):
        raise AiQualityAuditContextError(
            f"table read {table_name!r} failed: {result.error}"
        )
    return result.data or []


def _assert_audit_run_targets_question_version(
    client,
    *,
    audit_run_id: str,
    question_version_id: str,
) -> None:
    rows = _call_table(
        client,
        table_name="audit_runs",
        build_query=lambda table: (
            table.select("id, target_question_version_id")
            .eq("id", audit_run_id)
            .limit(1)
        ),
    )
    if not rows:
        raise AiQualityAuditContextError(
            f"audit run {audit_run_id!r} was not found"
        )
    row = rows[0]
    target_qvid = _normalize_uuid(row.get("target_question_version_id"), "audit_runs.target_question_version_id")
    if target_qvid != question_version_id:
        raise AiQualityAuditContextError(
            f"audit run {audit_run_id!r} targets question_version "
            f"{target_qvid!r}, expected {question_version_id!r}"
        )


def _reject_blind_leakage(row: Mapping[str, Any], *, prefix: str) -> None:
    for key in row.keys():
        if key in _BLIND_LEAKAGE_KEYS:
            raise AiQualityAuditContextError(
                f"{prefix} leaked forbidden field {key!r} into blind context"
            )
        lowered = str(key).lower()
        if "evidence" in lowered or lowered.endswith("_findings"):
            raise AiQualityAuditContextError(
                f"{prefix} leaked forbidden field {key!r} into blind context"
            )


def _normalize_blind_context_row(
    row: Mapping[str, Any],
    *,
    requested_question_version_id: str,
) -> Dict[str, Any]:
    question_version_id = _normalize_uuid(
        row.get("question_version_id"),
        f"{BLIND_CONTEXT_RPC}.question_version_id",
    )
    if question_version_id != requested_question_version_id:
        raise AiQualityAuditContextError(
            f"{BLIND_CONTEXT_RPC}.question_version_id={question_version_id!r} does not "
            f"match requested {requested_question_version_id!r}"
        )

    question_id = _require_positive_int(
        row.get("question_id"),
        f"{BLIND_CONTEXT_RPC}.question_id",
    )
    certification_exam_name = _require_non_empty_string(
        row.get("certification_exam_name"),
        f"{BLIND_CONTEXT_RPC}.certification_exam_name",
    )
    domain_name = _require_non_empty_string(
        row.get("domain_name"),
        f"{BLIND_CONTEXT_RPC}.domain_name",
    )
    question_text = _require_non_empty_string(
        row.get("question_text"),
        f"{BLIND_CONTEXT_RPC}.question_text",
    )
    question_type = _require_non_empty_string(
        row.get("question_type"),
        f"{BLIND_CONTEXT_RPC}.question_type",
    )
    required_selection_count = _require_positive_int(
        row.get("select_count"),
        f"{BLIND_CONTEXT_RPC}.select_count",
    )
    options = _normalize_blind_options(
        row.get("options"),
        prefix=f"{BLIND_CONTEXT_RPC}.options",
    )

    return {
        "question_version_id": question_version_id,
        "question_id": question_id,
        "certification_exam_name": certification_exam_name,
        "domain_name": domain_name,
        "question_text": question_text,
        "question_type": question_type,
        "required_selection_count": required_selection_count,
        "options": options,
    }


def _normalize_comparison_question_row(
    row: Mapping[str, Any],
    *,
    requested_question_version_id: str,
) -> Dict[str, Any]:
    question_version_id = _normalize_uuid(
        row.get("question_version_id"),
        f"{COMPARISON_CONTEXT_RPC}.question_version_id",
    )
    if question_version_id != requested_question_version_id:
        raise AiQualityAuditContextError(
            f"{COMPARISON_CONTEXT_RPC}.question_version_id={question_version_id!r} does not "
            f"match requested {requested_question_version_id!r}"
        )

    question_id = _require_positive_int(
        row.get("question_id"),
        f"{COMPARISON_CONTEXT_RPC}.question_id",
    )
    certification_exam_name = _require_non_empty_string(
        row.get("certification_exam_name"),
        f"{COMPARISON_CONTEXT_RPC}.certification_exam_name",
    )
    domain_name = _require_non_empty_string(
        row.get("domain_name"),
        f"{COMPARISON_CONTEXT_RPC}.domain_name",
    )
    question_text = _require_non_empty_string(
        row.get("question_text"),
        f"{COMPARISON_CONTEXT_RPC}.question_text",
    )
    question_type = _require_non_empty_string(
        row.get("question_type"),
        f"{COMPARISON_CONTEXT_RPC}.question_type",
    )
    required_selection_count = _require_positive_int(
        row.get("select_count"),
        f"{COMPARISON_CONTEXT_RPC}.select_count",
    )
    explanation = _require_string(
        row.get("explanation"),
        f"{COMPARISON_CONTEXT_RPC}.explanation",
    )
    options = _normalize_comparison_options(
        row.get("options"),
        prefix=f"{COMPARISON_CONTEXT_RPC}.options",
    )

    return {
        "question_version_id": question_version_id,
        "question_id": question_id,
        "certification_exam_name": certification_exam_name,
        "domain_name": domain_name,
        "question_text": question_text,
        "question_type": question_type,
        "required_selection_count": required_selection_count,
        "explanation": explanation,
        "options": options,
    }


def _normalize_blind_options(raw: object, *, prefix: str) -> List[Dict[str, Any]]:
    options = _require_option_array(raw, prefix=prefix)
    normalized: List[Dict[str, Any]] = []
    seen_labels: Set[str] = set()
    for index, item in enumerate(options):
        option_prefix = f"{prefix}[{index}]"
        if not isinstance(item, dict):
            raise AiQualityAuditContextError(
                f"{option_prefix} must be a JSON object, got {type(item).__name__}"
            )
        _reject_unexpected_keys(item, allowed=_BLIND_OPTION_KEYS, prefix=option_prefix)
        if "is_correct" in item:
            raise AiQualityAuditContextError(
                f"{option_prefix} leaked forbidden field 'is_correct'"
            )

        label = _require_non_empty_string(item.get("option_label"), f"{option_prefix}.option_label")
        text = _require_non_empty_string(item.get("option_text"), f"{option_prefix}.option_text")
        display_order = _require_positive_int(
            item.get("display_order"),
            f"{option_prefix}.display_order",
        )
        if label in seen_labels:
            raise AiQualityAuditContextError(
                f"{prefix} contains duplicate option_label: {label!r}"
            )
        seen_labels.add(label)
        normalized.append(
            {
                "option_label": label,
                "option_text": text,
                "display_order": display_order,
            }
        )

    if not normalized:
        raise AiQualityAuditContextError(f"{prefix} must not be empty")
    return normalized


def _normalize_comparison_options(raw: object, *, prefix: str) -> List[Dict[str, Any]]:
    options = _require_option_array(raw, prefix=prefix)
    normalized: List[Dict[str, Any]] = []
    seen_labels: Set[str] = set()
    for index, item in enumerate(options):
        option_prefix = f"{prefix}[{index}]"
        if not isinstance(item, dict):
            raise AiQualityAuditContextError(
                f"{option_prefix} must be a JSON object, got {type(item).__name__}"
            )
        _reject_unexpected_keys(item, allowed=_COMPARISON_OPTION_KEYS, prefix=option_prefix)

        label = _require_non_empty_string(item.get("option_label"), f"{option_prefix}.option_label")
        text = _require_non_empty_string(item.get("option_text"), f"{option_prefix}.option_text")
        display_order = _require_positive_int(
            item.get("display_order"),
            f"{option_prefix}.display_order",
        )
        is_correct = item.get("is_correct")
        if not isinstance(is_correct, bool):
            raise AiQualityAuditContextError(
                f"{option_prefix}.is_correct must be a boolean, got {type(is_correct).__name__}"
            )
        if label in seen_labels:
            raise AiQualityAuditContextError(
                f"{prefix} contains duplicate option_label: {label!r}"
            )
        seen_labels.add(label)
        normalized.append(
            {
                "option_label": label,
                "option_text": text,
                "display_order": display_order,
                "is_correct": is_correct,
            }
        )

    if not normalized:
        raise AiQualityAuditContextError(f"{prefix} must not be empty")
    return normalized


def _normalize_stored_correct_option_labels(
    raw: object,
    *,
    option_labels: Set[str],
    prefix: str,
) -> List[str]:
    if not isinstance(raw, list):
        raise AiQualityAuditContextError(
            f"{prefix} must be a JSON array, got {type(raw).__name__ if raw is not None else 'null'}"
        )

    normalized: List[str] = []
    seen: Set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise AiQualityAuditContextError(
                f"{prefix}[{index}] must be a string, got {type(item).__name__}"
            )
        label = item.strip()
        if not label:
            raise AiQualityAuditContextError(f"{prefix}[{index}] must not be empty")
        if label in seen:
            raise AiQualityAuditContextError(
                f"{prefix} contains duplicate option label: {label!r}"
            )
        if label not in option_labels:
            raise AiQualityAuditContextError(
                f"{prefix}[{index}]={label!r} is not present in comparison options"
            )
        seen.add(label)
        normalized.append(label)
    return normalized


def _load_pass_a_selected_option_labels(client, audit_run_id: str) -> List[str]:
    rows = _call_table(
        client,
        table_name="audit_run_pass_results",
        build_query=lambda table: (
            table.select("status, result_json")
            .eq("audit_run_id", audit_run_id)
            .eq("pass_code", "A")
            .limit(1)
        ),
    )
    if not rows:
        raise AiQualityAuditContextError(
            f"Pass A result for audit run {audit_run_id!r} was not found"
        )

    row = rows[0]
    status = _require_non_empty_string(row.get("status"), "audit_run_pass_results.status")
    if status != "completed":
        raise AiQualityAuditContextError(
            f"Pass A result for audit run {audit_run_id!r} is not completed"
        )

    result_json = row.get("result_json")
    if not isinstance(result_json, dict):
        raise AiQualityAuditContextError(
            "audit_run_pass_results.result_json must be a JSON object"
        )
    raw_labels = result_json.get("selected_option_labels")
    if not isinstance(raw_labels, list):
        raise AiQualityAuditContextError(
            "audit_run_pass_results.result_json.selected_option_labels must be a JSON array"
        )

    normalized: List[str] = []
    seen: Set[str] = set()
    for index, item in enumerate(raw_labels):
        if not isinstance(item, str):
            raise AiQualityAuditContextError(
                "audit_run_pass_results.result_json.selected_option_labels["
                f"{index}] must be a string"
            )
        label = item.strip()
        if not label:
            raise AiQualityAuditContextError(
                "audit_run_pass_results.result_json.selected_option_labels["
                f"{index}] must not be empty"
            )
        if label in seen:
            raise AiQualityAuditContextError(
                "audit_run_pass_results.result_json.selected_option_labels "
                f"contains duplicate label: {label!r}"
            )
        seen.add(label)
        normalized.append(label)
    if not normalized:
        raise AiQualityAuditContextError(
            "audit_run_pass_results.result_json.selected_option_labels must not be empty"
        )
    return normalized


def _validate_pass_a_labels_against_options(
    labels: Sequence[str],
    *,
    option_labels: Set[str],
) -> None:
    for label in labels:
        if label not in option_labels:
            raise AiQualityAuditContextError(
                f"pass_a_selected_option_labels[{label!r}] is not present in comparison options"
            )


def _load_frozen_evidence_rows(
    client,
    *,
    audit_run_id: str,
) -> List[Dict[str, Any]]:
    evidence_rows = _call_table(
        client,
        table_name="audit_run_evidence_set",
        build_query=lambda table: (
            table.select(
                "audit_run_id, resource_chunk_id, retrieval_rank, "
                "content_hash_at_execution, metadata"
            )
            .eq("audit_run_id", audit_run_id)
            .order("retrieval_rank")
        ),
    )

    if not evidence_rows:
        return []

    chunk_ids = []
    for index, row in enumerate(evidence_rows):
        row_run_id = _normalize_uuid(
            row.get("audit_run_id"),
            f"audit_run_evidence_set[{index}].audit_run_id",
        )
        if row_run_id != audit_run_id:
            raise AiQualityAuditContextError(
                f"audit_run_evidence_set[{index}] belongs to audit run "
                f"{row_run_id!r}, expected {audit_run_id!r}"
            )
        chunk_id = _normalize_uuid(
            row.get("resource_chunk_id"),
            f"audit_run_evidence_set[{index}].resource_chunk_id",
        )
        chunk_ids.append(chunk_id)

    chunk_rows = _call_table(
        client,
        table_name="resource_chunks",
        build_query=lambda table: (
            table.select(
                "id, chunk_text, resource_version_id, "
                "resource_versions(resource_id, version_number, "
                "official_resources(id, title, certification_exam_name))"
            )
            .in_("id", chunk_ids)
        ),
    )
    chunk_by_id = {
        _normalize_uuid(item.get("id"), "resource_chunks.id"): item
        for item in chunk_rows
    }

    normalized: List[Dict[str, Any]] = []
    seen_chunk_ids: Set[str] = set()
    seen_ranks: Set[int] = set()
    for index, row in enumerate(evidence_rows):
        prefix = f"audit_run_evidence_set[{index}]"
        chunk_id = _normalize_uuid(row.get("resource_chunk_id"), f"{prefix}.resource_chunk_id")
        rank = _require_positive_int(row.get("retrieval_rank"), f"{prefix}.retrieval_rank")
        authoritative_hash = _require_non_empty_string(
            row.get("content_hash_at_execution"),
            f"{prefix}.content_hash_at_execution",
        )
        metadata = row.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise AiQualityAuditContextError(
                f"{prefix}.metadata must be a JSON object, got {type(metadata).__name__}"
            )

        if chunk_id in seen_chunk_ids:
            raise AiQualityAuditContextError(
                f"frozen evidence contains duplicate chunk_id: {chunk_id!r}"
            )
        if rank in seen_ranks:
            raise AiQualityAuditContextError(
                f"frozen evidence contains duplicate rank: {rank}"
            )
        seen_chunk_ids.add(chunk_id)
        seen_ranks.add(rank)

        chunk_row = chunk_by_id.get(chunk_id)
        if chunk_row is None:
            raise AiQualityAuditContextError(
                f"{prefix}.resource_chunk_id={chunk_id!r} was not found in resource_chunks"
            )

        chunk_text = _require_non_empty_string(
            chunk_row.get("chunk_text"),
            f"resource_chunks[{chunk_id}].chunk_text",
        )
        resource_version_id = _normalize_uuid(
            chunk_row.get("resource_version_id"),
            f"resource_chunks[{chunk_id}].resource_version_id",
        )
        resource_versions = chunk_row.get("resource_versions") or {}
        if not isinstance(resource_versions, dict):
            raise AiQualityAuditContextError(
                f"resource_chunks[{chunk_id}].resource_versions must be a JSON object"
            )
        resource_id = _normalize_uuid(
            resource_versions.get("resource_id"),
            f"resource_chunks[{chunk_id}].resource_versions.resource_id",
        )
        resource_version_number = _require_positive_int(
            resource_versions.get("version_number"),
            f"resource_chunks[{chunk_id}].resource_versions.version_number",
        )
        official_resources = resource_versions.get("official_resources") or {}
        if not isinstance(official_resources, dict):
            raise AiQualityAuditContextError(
                f"resource_chunks[{chunk_id}].official_resources must be a JSON object"
            )
        title = _optional_non_empty_string(official_resources.get("title"))
        source_label = _optional_non_empty_string(
            official_resources.get("certification_exam_name")
        )

        item: Dict[str, Any] = {
            "chunk_id": chunk_id,
            "rank": rank,
            "resource_id": resource_id,
            "resource_version_id": resource_version_id,
            "resource_version_number": resource_version_number,
            "chunk_text": chunk_text,
            "authoritative_hash": authoritative_hash,
            "metadata": dict(metadata),
        }
        if title is not None:
            item["title"] = title
        if source_label is not None:
            item["source_label"] = source_label
        normalized.append(item)

    normalized.sort(key=lambda item: item["rank"])
    return normalized


def _require_option_array(raw: object, *, prefix: str) -> List[Any]:
    if not isinstance(raw, list):
        raise AiQualityAuditContextError(
            f"{prefix} must be a JSON array, got "
            f"{type(raw).__name__ if raw is not None else 'null'}"
        )
    return raw


def _reject_unexpected_keys(
    item: Mapping[str, Any],
    *,
    allowed: Set[str],
    prefix: str,
) -> None:
    unexpected = sorted(set(item.keys()) - allowed)
    if unexpected:
        raise AiQualityAuditContextError(
            f"{prefix} contains unexpected field(s): {', '.join(unexpected)}"
        )


def _validate_uuid(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not _UUID_RE.match(text):
        raise AiQualityAuditContextError(
            f"{field_name} must be a UUID string, got {value!r}"
        )
    return text.lower()


def _normalize_uuid(value: object, field_name: str) -> str:
    if value is None:
        raise AiQualityAuditContextError(f"{field_name} is required")
    if not isinstance(value, str):
        raise AiQualityAuditContextError(
            f"{field_name} must be a UUID string, got {type(value).__name__}"
        )
    text = value.strip()
    if not _UUID_RE.match(text):
        raise AiQualityAuditContextError(
            f"{field_name} must be a UUID string, got {value!r}"
        )
    return text.lower()


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AiQualityAuditContextError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise AiQualityAuditContextError(f"{field_name} must not be empty")
    return text


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AiQualityAuditContextError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    return value


def _optional_non_empty_string(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AiQualityAuditContextError(
            f"optional string field must be a string, got {type(value).__name__}"
        )
    text = value.strip()
    return text or None


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AiQualityAuditContextError(
            f"{field_name} must be a positive integer, got {value!r}"
        )
    if value <= 0:
        raise AiQualityAuditContextError(
            f"{field_name} must be a positive integer, got {value!r}"
        )
    return value
