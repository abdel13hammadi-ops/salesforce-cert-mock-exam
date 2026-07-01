"""
Strict JSON schema validation for CertBound AI quality audit pass results (V48).

Validates model responses before ``record_audit_pass_result_v1`` may persist a
pass as ``completed``.  Rules mirror the V48 RPC migration shape checks plus the
worker-side finding constraints required for downstream completion.
"""

from __future__ import annotations

import re
from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Sequence, Set, Union

from workers.finding_policy import ALLOWED_MATERIALITY, CANONICAL_FINDING_CODES
from workers.llm_audit import ALLOWED_FINDING_TYPES, ALLOWED_SEVERITIES

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

SUPPORTED_FINDING_CODES = CANONICAL_FINDING_CODES | frozenset({"DOMAIN_MISALIGNMENT"})

ALLOWED_RESOLUTION_TYPES = frozenset({
    "NORMAL_DISPUTE",
    "PASS_A_SUBSTITUTION",
    "PASS_B_SUBSTITUTION",
})

ALLOWED_RESOLUTION_STATUS = frozenset({"RESOLVED", "UNRESOLVED"})

_SUBSTITUTED_FOR_PASSES_BY_TYPE: Dict[str, List[str]] = {
    "NORMAL_DISPUTE": [],
    "PASS_A_SUBSTITUTION": ["A", "B"],
    "PASS_B_SUBSTITUTION": ["B"],
}

LabelSet = Union[AbstractSet[str], Sequence[str], Set[str]]


class AiQualityAuditValidationError(ValueError):
    """Raised when an AI quality audit pass result fails schema validation."""


def validate_pass_a_result(
    raw: object,
    *,
    allowed_option_labels: LabelSet,
    required_selection_count: int,
) -> Dict[str, Any]:
    """Validate and normalize a Pass A completed result payload."""
    obj = _require_object(raw, "pass A result")
    labels = _validate_selected_option_labels(
        obj.get("selected_option_labels"),
        allowed_option_labels=allowed_option_labels,
        required_selection_count=required_selection_count,
        prefix="pass A result",
    )
    return {"selected_option_labels": labels}


def validate_pass_b_result(
    raw: object,
    *,
    allowed_option_labels: LabelSet,
    required_selection_count: int,
    frozen_evidence_chunk_ids: LabelSet,
) -> Dict[str, Any]:
    """Validate and normalize a Pass B completed result payload."""
    obj = _require_object(raw, "pass B result")
    labels = _validate_selected_option_labels(
        obj.get("selected_option_labels"),
        allowed_option_labels=allowed_option_labels,
        required_selection_count=required_selection_count,
        prefix="pass B result",
    )
    if "proposed_findings" not in obj:
        raise AiQualityAuditValidationError(
            "pass B result is missing required field 'proposed_findings'"
        )
    proposed = _validate_proposed_findings(
        obj.get("proposed_findings"),
        frozen_evidence_chunk_ids=frozen_evidence_chunk_ids,
        prefix="pass B result.proposed_findings",
    )
    return {
        "selected_option_labels": labels,
        "proposed_findings": proposed,
    }


def validate_pass_c_result(
    raw: object,
    *,
    pass_b_proposed_finding_refs: Optional[LabelSet] = None,
    frozen_evidence_chunk_ids: Optional[LabelSet] = None,
) -> Dict[str, Any]:
    """Validate and normalize a Pass C completed result payload."""
    obj = _require_object(raw, "pass C result")

    resolution_type = _require_non_empty_string(
        obj.get("resolution_type"),
        "pass C result.resolution_type",
    )
    if resolution_type not in ALLOWED_RESOLUTION_TYPES:
        raise AiQualityAuditValidationError(
            f"pass C result.resolution_type must be one of "
            f"{sorted(ALLOWED_RESOLUTION_TYPES)}, got {resolution_type!r}"
        )

    resolution_status = _require_non_empty_string(
        obj.get("resolution_status"),
        "pass C result.resolution_status",
    )
    if resolution_status not in ALLOWED_RESOLUTION_STATUS:
        raise AiQualityAuditValidationError(
            f"pass C result.resolution_status must be one of "
            f"{sorted(ALLOWED_RESOLUTION_STATUS)}, got {resolution_status!r}"
        )

    substituted = _validate_string_list(
        obj.get("substituted_for_passes"),
        "pass C result.substituted_for_passes",
        element_type=str,
        allow_empty=True,
    )
    expected_substituted = _SUBSTITUTED_FOR_PASSES_BY_TYPE[resolution_type]
    if substituted != expected_substituted:
        raise AiQualityAuditValidationError(
            f"pass C result.resolution_type={resolution_type!r} requires "
            f"substituted_for_passes={expected_substituted!r}, got {substituted!r}"
        )

    confirmed = _validate_confirmed_finding_refs(
        obj.get("confirmed_finding_refs"),
        prefix="pass C result.confirmed_finding_refs",
    )

    if resolution_status == "UNRESOLVED":
        if confirmed:
            raise AiQualityAuditValidationError(
                "pass C result.confirmed_finding_refs must be empty when "
                "resolution_status is UNRESOLVED"
            )
        normalized: Dict[str, Any] = {
            "resolution_type": resolution_type,
            "resolution_status": resolution_status,
            "substituted_for_passes": substituted,
            "confirmed_finding_refs": [],
        }
        return normalized

    if resolution_type == "NORMAL_DISPUTE":
        upstream = _label_set(pass_b_proposed_finding_refs)
        if upstream is None:
            raise AiQualityAuditValidationError(
                "pass_b_proposed_finding_refs is required for NORMAL_DISPUTE validation"
            )
        for ref in confirmed:
            if ref not in upstream:
                raise AiQualityAuditValidationError(
                    f"pass C result.confirmed_finding_refs[{ref!r}] is not present "
                    f"in Pass B proposed_findings"
                )
        normalized = {
            "resolution_type": resolution_type,
            "resolution_status": resolution_status,
            "substituted_for_passes": substituted,
            "confirmed_finding_refs": confirmed,
        }
        return normalized

    # Substitution modes: validate Pass C proposed_findings when present.
    frozen = _label_set(frozen_evidence_chunk_ids)
    if frozen is None:
        raise AiQualityAuditValidationError(
            "frozen_evidence_chunk_ids is required for substitution Pass C validation"
        )
    if "proposed_findings" not in obj:
        raise AiQualityAuditValidationError(
            "pass C result is missing required field 'proposed_findings' "
            f"for resolution_type={resolution_type!r}"
        )
    proposed = _validate_proposed_findings(
        obj.get("proposed_findings"),
        frozen_evidence_chunk_ids=frozen,
        prefix="pass C result.proposed_findings",
    )
    proposed_refs = {item["finding_ref"] for item in proposed}
    for ref in confirmed:
        if ref not in proposed_refs:
            raise AiQualityAuditValidationError(
                f"pass C result.confirmed_finding_refs[{ref!r}] is not present "
                f"in Pass C proposed_findings"
            )

    return {
        "resolution_type": resolution_type,
        "resolution_status": resolution_status,
        "substituted_for_passes": substituted,
        "confirmed_finding_refs": confirmed,
        "proposed_findings": proposed,
    }


def _require_object(raw: object, prefix: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise AiQualityAuditValidationError(
            f"{prefix} must be a JSON object, got {type(raw).__name__}"
        )
    return raw


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AiQualityAuditValidationError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise AiQualityAuditValidationError(f"{field_name} must not be empty")
    return text


def _label_set(values: Optional[LabelSet]) -> Optional[Set[str]]:
    if values is None:
        return None
    return {str(item).strip() for item in values if str(item).strip()}


def _validate_selected_option_labels(
    raw: object,
    *,
    allowed_option_labels: LabelSet,
    required_selection_count: int,
    prefix: str,
) -> List[str]:
    if required_selection_count < 1:
        raise AiQualityAuditValidationError(
            "required_selection_count must be a positive integer"
        )
    allowed = _label_set(allowed_option_labels) or set()
    if not allowed:
        raise AiQualityAuditValidationError(
            "allowed_option_labels must contain at least one label"
        )

    if not isinstance(raw, list):
        raise AiQualityAuditValidationError(
            f"{prefix}.selected_option_labels must be a JSON array, got "
            f"{type(raw).__name__ if raw is not None else 'null'}"
        )
    if not raw:
        raise AiQualityAuditValidationError(
            f"{prefix}.selected_option_labels must not be empty"
        )

    normalized: List[str] = []
    seen: Set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise AiQualityAuditValidationError(
                f"{prefix}.selected_option_labels[{index}] must be a string, "
                f"got {type(item).__name__}"
            )
        label = item.strip()
        if not label:
            raise AiQualityAuditValidationError(
                f"{prefix}.selected_option_labels[{index}] must not be empty"
            )
        if label in seen:
            raise AiQualityAuditValidationError(
                f"{prefix}.selected_option_labels contains duplicate label: {label!r}"
            )
        if label not in allowed:
            raise AiQualityAuditValidationError(
                f"{prefix}.selected_option_labels[{index}]={label!r} is not an allowed option label"
            )
        seen.add(label)
        normalized.append(label)

    if len(normalized) != required_selection_count:
        raise AiQualityAuditValidationError(
            f"{prefix}.selected_option_labels must contain exactly "
            f"{required_selection_count} label(s), got {len(normalized)}"
        )
    return normalized


def _validate_string_list(
    raw: object,
    field_name: str,
    *,
    element_type: type = str,
    allow_empty: bool = True,
    unique: bool = False,
    non_empty_elements: bool = False,
) -> List[Any]:
    if not isinstance(raw, list):
        raise AiQualityAuditValidationError(
            f"{field_name} must be a JSON array, got "
            f"{type(raw).__name__ if raw is not None else 'null'}"
        )
    if not allow_empty and not raw:
        raise AiQualityAuditValidationError(f"{field_name} must not be empty")

    normalized: List[Any] = []
    seen: Set[Any] = set()
    for index, item in enumerate(raw):
        if element_type is str:
            if not isinstance(item, str):
                raise AiQualityAuditValidationError(
                    f"{field_name}[{index}] must be a string, got {type(item).__name__}"
                )
            value: Any = item.strip()
            if non_empty_elements and not value:
                raise AiQualityAuditValidationError(
                    f"{field_name}[{index}] must not be empty"
                )
        else:
            value = item
        if unique:
            if value in seen:
                raise AiQualityAuditValidationError(
                    f"{field_name} contains duplicate value: {value!r}"
                )
            seen.add(value)
        normalized.append(value)
    return normalized


def _validate_confirmed_finding_refs(raw: object, *, prefix: str) -> List[str]:
    return _validate_string_list(
        raw,
        prefix,
        element_type=str,
        allow_empty=True,
        unique=True,
        non_empty_elements=True,
    )


def _validate_uuid_string(value: object, field_name: str) -> str:
    text = _require_non_empty_string(value, field_name)
    if not _UUID_RE.match(text):
        raise AiQualityAuditValidationError(
            f"{field_name} must be a UUID string, got {text!r}"
        )
    return text.lower()


def _validate_metadata(raw: object, field_name: str) -> Dict[str, Any]:
    if raw is None:
        raise AiQualityAuditValidationError(
            f"{field_name} must be a JSON object, got null"
        )
    if not isinstance(raw, dict):
        raise AiQualityAuditValidationError(
            f"{field_name} must be a JSON object, got {type(raw).__name__}"
        )
    return dict(raw)


def _validate_source_support_context(
    metadata: Mapping[str, Any],
    *,
    prefix: str,
) -> None:
    ctx = metadata.get("source_support_context")
    if not isinstance(ctx, dict):
        raise AiQualityAuditValidationError(
            f"{prefix}.metadata.source_support_context must be a JSON object"
        )

    attempted = ctx.get("attempted_retrieval")
    if isinstance(attempted, bool) or not isinstance(attempted, (int, float)):
        raise AiQualityAuditValidationError(
            f"{prefix}.metadata.source_support_context.attempted_retrieval "
            "must be a nonnegative integer"
        )
    if attempted < 0 or attempted != int(attempted):
        raise AiQualityAuditValidationError(
            f"{prefix}.metadata.source_support_context.attempted_retrieval "
            "must be a nonnegative integer"
        )

    for field in (
        "evidence_limitation",
        "proposed_technical_claim",
        "insufficiency_reason",
    ):
        value = ctx.get(field)
        if not isinstance(value, str) or not value.strip():
            raise AiQualityAuditValidationError(
                f"{prefix}.metadata.source_support_context.{field} must be non-empty"
            )


def _validate_proposed_finding(
    index: int,
    raw: object,
    *,
    frozen_evidence_chunk_ids: Set[str],
    list_prefix: str,
) -> Dict[str, Any]:
    prefix = f"{list_prefix}[{index}]"
    if not isinstance(raw, dict):
        raise AiQualityAuditValidationError(
            f"{prefix} must be a JSON object, got {type(raw).__name__}"
        )

    finding_ref = _require_non_empty_string(raw.get("finding_ref"), f"{prefix}.finding_ref")
    finding_code = _require_non_empty_string(raw.get("finding_code"), f"{prefix}.finding_code")
    if finding_code not in SUPPORTED_FINDING_CODES:
        raise AiQualityAuditValidationError(
            f"{prefix}.finding_code={finding_code!r} is not a supported finding code"
        )

    finding_type = _require_non_empty_string(raw.get("finding_type"), f"{prefix}.finding_type")
    if finding_type not in ALLOWED_FINDING_TYPES:
        raise AiQualityAuditValidationError(
            f"{prefix}.finding_type={finding_type!r} is not an allowed finding type"
        )

    severity = _require_non_empty_string(raw.get("severity"), f"{prefix}.severity")
    if severity not in ALLOWED_SEVERITIES:
        raise AiQualityAuditValidationError(
            f"{prefix}.severity={severity!r} is not an allowed severity"
        )

    materiality = _require_non_empty_string(raw.get("materiality"), f"{prefix}.materiality")
    if materiality not in ALLOWED_MATERIALITY:
        raise AiQualityAuditValidationError(
            f"{prefix}.materiality={materiality!r} is not an allowed materiality"
        )

    title = _require_non_empty_string(raw.get("title"), f"{prefix}.title")
    description = _require_non_empty_string(raw.get("description"), f"{prefix}.description")
    if "metadata" not in raw:
        raise AiQualityAuditValidationError(
            f"{prefix} is missing required field 'metadata'"
        )
    metadata = _validate_metadata(raw.get("metadata"), f"{prefix}.metadata")

    if finding_code == "SOURCE_SUPPORT_WEAK":
        if finding_type != "source_support":
            raise AiQualityAuditValidationError(
                f"{prefix}.finding_type must be 'source_support' when "
                f"finding_code is SOURCE_SUPPORT_WEAK"
            )
        if materiality == "blocking":
            raise AiQualityAuditValidationError(
                f"{prefix}.materiality cannot be 'blocking' when "
                f"finding_code is SOURCE_SUPPORT_WEAK"
            )

    if finding_code == "DOMAIN_MISALIGNMENT":
        if finding_type != "coverage":
            raise AiQualityAuditValidationError(
                f"{prefix}.finding_type must be 'coverage' when "
                f"finding_code is DOMAIN_MISALIGNMENT"
            )
        if materiality == "blocking":
            raise AiQualityAuditValidationError(
                f"{prefix}.materiality cannot be 'blocking' when "
                f"finding_code is DOMAIN_MISALIGNMENT"
            )

    if "evidence_chunk_ids" not in raw:
        raise AiQualityAuditValidationError(
            f"{prefix} is missing required field 'evidence_chunk_ids'"
        )
    chunk_ids_raw = raw.get("evidence_chunk_ids")
    if not isinstance(chunk_ids_raw, list):
        raise AiQualityAuditValidationError(
            f"{prefix}.evidence_chunk_ids must be a JSON array"
        )

    normalized_chunk_ids: List[str] = []
    seen_chunks: Set[str] = set()
    frozen = {chunk_id.lower() for chunk_id in frozen_evidence_chunk_ids}
    for chunk_index, chunk_value in enumerate(chunk_ids_raw):
        chunk_id = _validate_uuid_string(
            chunk_value,
            f"{prefix}.evidence_chunk_ids[{chunk_index}]",
        )
        if chunk_id in seen_chunks:
            raise AiQualityAuditValidationError(
                f"{prefix}.evidence_chunk_ids contains duplicate chunk id: {chunk_id!r}"
            )
        if chunk_id not in frozen:
            raise AiQualityAuditValidationError(
                f"{prefix}.evidence_chunk_ids[{chunk_index}]={chunk_id!r} is outside "
                f"the frozen run evidence set"
            )
        seen_chunks.add(chunk_id)
        normalized_chunk_ids.append(chunk_id)

    if finding_code == "SOURCE_SUPPORT_WEAK" and not normalized_chunk_ids:
        _validate_source_support_context(metadata, prefix=prefix)

    return {
        "finding_ref": finding_ref,
        "finding_code": finding_code,
        "finding_type": finding_type,
        "severity": severity,
        "materiality": materiality,
        "title": title,
        "description": description,
        "evidence_chunk_ids": normalized_chunk_ids,
        "metadata": metadata,
    }


def _validate_proposed_findings(
    raw: object,
    *,
    frozen_evidence_chunk_ids: LabelSet,
    prefix: str,
) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        raise AiQualityAuditValidationError(
            f"{prefix} must be a JSON array, got "
            f"{type(raw).__name__ if raw is not None else 'null'}"
        )

    frozen = _label_set(frozen_evidence_chunk_ids) or set()
    normalized: List[Dict[str, Any]] = []
    seen_refs: Set[str] = set()
    for index, item in enumerate(raw):
        finding = _validate_proposed_finding(
            index,
            item,
            frozen_evidence_chunk_ids=frozen,
            list_prefix=prefix,
        )
        ref = finding["finding_ref"]
        if ref in seen_refs:
            raise AiQualityAuditValidationError(
                f"{prefix} contains duplicate finding_ref: {ref!r}"
            )
        seen_refs.add(ref)
        normalized.append(finding)
    return normalized
