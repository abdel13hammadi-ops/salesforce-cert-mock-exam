"""
Canonical audit-evidence contract for CertBound (V45 Phase 4C).

Provides a stable, validated evidence payload stored on each finding at
``metadata["evidence_contract"]``.  Uses the existing ``audit_findings.metadata``
JSONB column — no separate evidence-document table is required.

Legacy findings without ``evidence_contract`` remain readable via
``normalize_legacy_evidence_contract()``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from workers.finding_policy import ALLOWED_MATERIALITY
from workers.llm_audit import ALLOWED_EVIDENCE_ROLES, ALLOWED_FINDING_TYPES, ALLOWED_SEVERITIES

EVIDENCE_CONTRACT_VERSION = "1.0.0"

ALLOWED_AUDIT_SOURCES = frozenset({"deterministic", "llm", "hybrid", "human"})

_DETERMINISTIC_DETECTOR = "certbound-deterministic-audit"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class EvidenceContractError(ValueError):
    """Raised when an evidence contract fails validation."""


@dataclass(frozen=True)
class AuditEvidenceContext:
    """Run-level context used to anchor evidence to an immutable target."""

    audit_type: str
    target_question_version_id: Optional[str] = None
    target_candidate_id: Optional[str] = None
    ruleset_version: Optional[str] = None
    question_snapshot: Optional[dict] = None
    model_name: Optional[str] = None
    prompt_version: Optional[str] = None
    provider_request_id: Optional[str] = None
    run_metadata: Optional[dict] = None
    generated_at: Optional[str] = None

    @classmethod
    def from_orchestration(
        cls,
        *,
        audit_type: str,
        target_question_version_id: Optional[str],
        target_candidate_id: Optional[str],
        ruleset_version: Optional[str] = None,
        question_snapshot: Optional[dict] = None,
        model_name: Optional[str] = None,
        prompt_version: Optional[str] = None,
        provider_request_id: Optional[str] = None,
        run_metadata: Optional[dict] = None,
        generated_at: Optional[str] = None,
    ) -> "AuditEvidenceContext":
        return cls(
            audit_type=audit_type,
            target_question_version_id=_clean_id(target_question_version_id),
            target_candidate_id=_clean_id(target_candidate_id),
            ruleset_version=_clean_optional_text(ruleset_version),
            question_snapshot=question_snapshot,
            model_name=_clean_optional_text(model_name),
            prompt_version=_clean_optional_text(prompt_version),
            provider_request_id=_clean_optional_text(provider_request_id),
            run_metadata=run_metadata or {},
            generated_at=generated_at or _utc_now_iso(),
        )


@dataclass
class EvidenceReference:
    resource_chunk_id: str
    evidence_role: str
    quote_text: Optional[str] = None
    relevance_score: Optional[float] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class AuditEvidenceContract:
    contract_version: str
    finding_code: str
    finding_category: str
    severity: str
    materiality: str
    audit_source: str
    summary: str
    detailed_rationale: str
    field_path: Optional[str] = None
    question_id: Optional[str] = None
    question_version_id: Optional[str] = None
    question_version_number: Optional[int] = None
    target_candidate_id: Optional[str] = None
    certification_code: Optional[str] = None
    observed_evidence: Optional[str] = None
    expected_rule_or_value: Optional[str] = None
    suggested_correction: Optional[str] = None
    confidence: Optional[float] = None
    deterministic_rule: Optional[dict] = None
    model_metadata: Optional[dict] = None
    prompt_version: Optional[str] = None
    ruleset_version: Optional[str] = None
    supporting_references: List[EvidenceReference] = field(default_factory=list)
    related_question_version_id: Optional[str] = None
    generated_at: Optional[str] = None
    fingerprint: Optional[str] = None
    deduplication_inputs: Optional[dict] = None
    legacy: bool = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_key(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _require_non_empty(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EvidenceContractError(f"{field_name} must not be empty")
    return text


def _validate_uuid(value: Optional[str], field_name: str, *, required: bool = False) -> None:
    if value is None or str(value).strip() == "":
        if required:
            raise EvidenceContractError(f"{field_name} is required")
        return
    if not _UUID_RE.match(str(value).strip()):
        raise EvidenceContractError(f"{field_name} must be a UUID, got: {value!r}")


def _validate_confidence(value: Any, field_name: str = "confidence") -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise EvidenceContractError(f"{field_name} must be numeric, not bool")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceContractError(f"{field_name} must be numeric") from exc
    if number < 0 or number > 1:
        raise EvidenceContractError(f"{field_name} must be in [0, 1], got: {number}")
    return number


def _validate_version_number(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise EvidenceContractError("question_version_number must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceContractError("question_version_number must be an integer") from exc
    if number <= 0:
        raise EvidenceContractError("question_version_number must be positive")
    return number


def _references_from_finding_evidence(items: Any) -> List[EvidenceReference]:
    if not items:
        return []
    if not isinstance(items, list):
        raise EvidenceContractError("evidence must be a list")
    refs: List[EvidenceReference] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise EvidenceContractError(f"evidence[{index}] must be an object")
        chunk_id = _require_non_empty(item.get("resource_chunk_id"), f"evidence[{index}].resource_chunk_id")
        role = _require_non_empty(item.get("evidence_role"), f"evidence[{index}].evidence_role")
        if role not in ALLOWED_EVIDENCE_ROLES:
            raise EvidenceContractError(f"evidence[{index}] has invalid evidence_role: {role!r}")
        _validate_uuid(chunk_id, f"evidence[{index}].resource_chunk_id", required=True)
        refs.append(
            EvidenceReference(
                resource_chunk_id=chunk_id,
                evidence_role=role,
                quote_text=_clean_optional_text(item.get("quote_text")),
                relevance_score=_validate_confidence(item.get("relevance_score"), "relevance_score"),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return refs


def _serialize_reference(ref: EvidenceReference) -> dict:
    payload = {
        "resource_chunk_id": ref.resource_chunk_id,
        "evidence_role": ref.evidence_role,
    }
    if ref.quote_text:
        payload["quote_text"] = ref.quote_text
    if ref.relevance_score is not None:
        payload["relevance_score"] = ref.relevance_score
    if ref.metadata:
        payload["metadata"] = ref.metadata
    return payload


def serialize_evidence_contract(contract: AuditEvidenceContract) -> dict:
    """Return a deterministic JSON-compatible evidence-contract dict."""
    payload: Dict[str, Any] = {
        "contract_version": contract.contract_version,
        "finding_code": contract.finding_code,
        "finding_category": contract.finding_category,
        "severity": contract.severity,
        "materiality": contract.materiality,
        "audit_source": contract.audit_source,
        "summary": contract.summary,
        "detailed_rationale": contract.detailed_rationale,
    }
    optional_fields = {
        "field_path": contract.field_path,
        "question_id": contract.question_id,
        "question_version_id": contract.question_version_id,
        "question_version_number": contract.question_version_number,
        "target_candidate_id": contract.target_candidate_id,
        "certification_code": contract.certification_code,
        "observed_evidence": contract.observed_evidence,
        "expected_rule_or_value": contract.expected_rule_or_value,
        "suggested_correction": contract.suggested_correction,
        "confidence": contract.confidence,
        "deterministic_rule": contract.deterministic_rule,
        "model_metadata": contract.model_metadata,
        "prompt_version": contract.prompt_version,
        "ruleset_version": contract.ruleset_version,
        "related_question_version_id": contract.related_question_version_id,
        "generated_at": contract.generated_at,
        "legacy": contract.legacy,
    }
    for key, value in optional_fields.items():
        if value is not None:
            payload[key] = value
    if contract.supporting_references:
        payload["supporting_references"] = [
            _serialize_reference(ref) for ref in contract.supporting_references
        ]
    dedup = contract.deduplication_inputs or build_deduplication_inputs(payload)
    payload["deduplication_inputs"] = dedup
    payload["fingerprint"] = contract.fingerprint or evidence_fingerprint(payload)
    return payload


def build_deduplication_inputs(contract: Mapping[str, Any]) -> dict:
    """Build stable dedup inputs aligned with hybrid merge keys."""
    return {
        "finding_code": _normalize_key(contract.get("finding_code")),
        "field_path": _normalize_key(contract.get("field_path")),
        "description": _normalize_key(contract.get("detailed_rationale")),
        "question_version_id": _normalize_key(contract.get("question_version_id")),
        "target_candidate_id": _normalize_key(contract.get("target_candidate_id")),
    }


def evidence_fingerprint(contract: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint for deduplication comparisons."""
    dedup = contract.get("deduplication_inputs")
    if not isinstance(dedup, dict):
        dedup = build_deduplication_inputs(contract)
    canonical = json.dumps(
        {
            "contract_version": contract.get("contract_version"),
            **dedup,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_evidence_contract(data: Mapping[str, Any], *, strict_identity: bool = True) -> None:
    """Validate a serialized evidence contract."""
    if not isinstance(data, Mapping):
        raise EvidenceContractError("evidence contract must be an object")

    _require_non_empty(data.get("contract_version"), "contract_version")
    _require_non_empty(data.get("finding_code"), "finding_code")
    finding_category = _require_non_empty(data.get("finding_category"), "finding_category")
    severity = _require_non_empty(data.get("severity"), "severity")
    materiality = _require_non_empty(data.get("materiality"), "materiality")
    audit_source = _require_non_empty(data.get("audit_source"), "audit_source")
    _require_non_empty(data.get("summary"), "summary")
    _require_non_empty(data.get("detailed_rationale"), "detailed_rationale")

    if finding_category not in ALLOWED_FINDING_TYPES:
        raise EvidenceContractError(f"invalid finding_category: {finding_category!r}")
    if severity not in ALLOWED_SEVERITIES:
        raise EvidenceContractError(f"invalid severity: {severity!r}")
    if materiality not in ALLOWED_MATERIALITY:
        raise EvidenceContractError(f"invalid materiality: {materiality!r}")
    if audit_source not in ALLOWED_AUDIT_SOURCES:
        raise EvidenceContractError(f"invalid audit_source: {audit_source!r}")

    _validate_confidence(data.get("confidence"))
    _validate_version_number(data.get("question_version_number"))

    qvid = _clean_id(data.get("question_version_id"))
    candidate_id = _clean_id(data.get("target_candidate_id"))
    related_qvid = _clean_id(data.get("related_question_version_id"))
    _validate_uuid(qvid, "question_version_id")
    _validate_uuid(candidate_id, "target_candidate_id")
    _validate_uuid(related_qvid, "related_question_version_id")

    if strict_identity and not data.get("legacy"):
        has_version = bool(qvid or related_qvid)
        has_candidate = bool(candidate_id)
        if not has_version and not has_candidate:
            raise EvidenceContractError(
                "question_version_id or target_candidate_id is required for non-legacy evidence"
            )

    refs = data.get("supporting_references")
    if refs is not None:
        if not isinstance(refs, list):
            raise EvidenceContractError("supporting_references must be a list")
        for index, item in enumerate(refs):
            if not isinstance(item, dict):
                raise EvidenceContractError(f"supporting_references[{index}] must be an object")
            role = _require_non_empty(item.get("evidence_role"), f"supporting_references[{index}].evidence_role")
            if role not in ALLOWED_EVIDENCE_ROLES:
                raise EvidenceContractError(
                    f"supporting_references[{index}] has invalid evidence_role: {role!r}"
                )
            _validate_uuid(
                _require_non_empty(item.get("resource_chunk_id"), f"supporting_references[{index}].resource_chunk_id"),
                f"supporting_references[{index}].resource_chunk_id",
                required=True,
            )
            _validate_confidence(item.get("relevance_score"), "relevance_score")


def _infer_audit_source(finding: dict, context: AuditEvidenceContext) -> str:
    if context.audit_type in ALLOWED_AUDIT_SOURCES:
        if context.audit_type != "hybrid":
            return context.audit_type
    meta = finding.get("metadata") or {}
    if meta.get("llm_detector_name"):
        return "hybrid"
    if finding.get("detector_name") == _DETERMINISTIC_DETECTOR:
        return "deterministic"
    if context.audit_type == "hybrid":
        return "llm"
    return context.audit_type


def _resolve_question_identity(finding: dict, context: AuditEvidenceContext) -> dict:
    meta = finding.get("metadata") or {}
    snapshot = context.question_snapshot or {}

    question_id = _clean_id(
        snapshot.get("question_id")
        or snapshot.get("id")
        or meta.get("question_id")
    )
    question_version_id = _clean_id(context.target_question_version_id)
    related_question_version_id = None
    target_candidate_id = _clean_id(context.target_candidate_id)

    if meta.get("question_version_id_a"):
        question_version_id = _clean_id(meta.get("question_version_id_a"))
        related_question_version_id = _clean_id(meta.get("question_version_id_b"))

    version_number = snapshot.get("version_number")
    if version_number is None:
        version_number = snapshot.get("version")
    if version_number is None:
        version_number = meta.get("question_version_number")

    certification_code = _clean_optional_text(
        meta.get("certification_code")
        or meta.get("certification_id")
        or meta.get("certification_exam_name")
        or snapshot.get("certification_code")
        or snapshot.get("certification_id")
        or snapshot.get("exam_name")
        or (context.run_metadata or {}).get("certification_exam_name")
    )

    return {
        "question_id": question_id,
        "question_version_id": question_version_id,
        "question_version_number": version_number,
        "target_candidate_id": target_candidate_id,
        "certification_code": certification_code,
        "related_question_version_id": related_question_version_id,
    }


def _deterministic_rule_metadata(finding: dict, context: AuditEvidenceContext) -> dict:
    meta = dict(finding.get("metadata") or {})
    return {
        "detector_name": finding.get("detector_name"),
        "detector_version": finding.get("detector_version"),
        "finding_code": finding.get("finding_code"),
        "ruleset_version": meta.get("ruleset_version") or context.ruleset_version,
        "original_finding_code": meta.get("original_finding_code"),
    }


def _model_metadata(finding: dict, context: AuditEvidenceContext) -> Optional[dict]:
    meta = finding.get("metadata") or {}
    payload = {}
    if context.model_name:
        payload["model_name"] = context.model_name
    if context.provider_request_id:
        payload["provider_request_id"] = context.provider_request_id
    llm_name = meta.get("llm_detector_name") or finding.get("detector_name")
    llm_version = meta.get("llm_detector_version") or finding.get("detector_version")
    if llm_name:
        payload["detector_name"] = llm_name
    if llm_version:
        payload["detector_version"] = llm_version
    return payload or None


def build_deterministic_evidence(finding: dict, context: AuditEvidenceContext) -> dict:
    """Construct a validated deterministic evidence contract."""
    identity = _resolve_question_identity(finding, context)
    contract = AuditEvidenceContract(
        contract_version=EVIDENCE_CONTRACT_VERSION,
        finding_code=_require_non_empty(finding.get("finding_code"), "finding_code"),
        finding_category=_require_non_empty(finding.get("finding_type"), "finding_type"),
        severity=_require_non_empty(finding.get("severity"), "severity"),
        materiality=finding.get("materiality") or "warning",
        audit_source="deterministic",
        summary=_require_non_empty(finding.get("title"), "title"),
        detailed_rationale=_require_non_empty(finding.get("description"), "description"),
        field_path=_clean_optional_text(finding.get("field_path")),
        question_id=identity["question_id"],
        question_version_id=identity["question_version_id"],
        question_version_number=_validate_version_number(identity["question_version_number"]),
        target_candidate_id=identity["target_candidate_id"],
        certification_code=identity["certification_code"],
        related_question_version_id=identity["related_question_version_id"],
        observed_evidence=_clean_optional_text(finding.get("description")),
        expected_rule_or_value=_clean_optional_text(finding.get("finding_code")),
        suggested_correction=_clean_optional_text((finding.get("metadata") or {}).get("suggested_correction")),
        confidence=_validate_confidence(finding.get("confidence")),
        deterministic_rule=_deterministic_rule_metadata(finding, context),
        ruleset_version=_clean_optional_text(
            (finding.get("metadata") or {}).get("ruleset_version") or context.ruleset_version
        ),
        supporting_references=_references_from_finding_evidence(finding.get("evidence")),
        generated_at=context.generated_at or _utc_now_iso(),
    )
    serialized = serialize_evidence_contract(contract)
    validate_evidence_contract(serialized, strict_identity=True)
    return serialized


def build_llm_evidence(finding: dict, context: AuditEvidenceContext) -> dict:
    """Construct a validated LLM evidence contract."""
    identity = _resolve_question_identity(finding, context)
    meta = finding.get("metadata") or {}
    contract = AuditEvidenceContract(
        contract_version=EVIDENCE_CONTRACT_VERSION,
        finding_code=_require_non_empty(finding.get("finding_code"), "finding_code"),
        finding_category=_require_non_empty(finding.get("finding_type"), "finding_type"),
        severity=_require_non_empty(finding.get("severity"), "severity"),
        materiality=finding.get("materiality") or "warning",
        audit_source="llm",
        summary=_require_non_empty(finding.get("title"), "title"),
        detailed_rationale=_require_non_empty(finding.get("description"), "description"),
        field_path=_clean_optional_text(finding.get("field_path")),
        question_id=identity["question_id"],
        question_version_id=identity["question_version_id"],
        question_version_number=_validate_version_number(identity["question_version_number"]),
        target_candidate_id=identity["target_candidate_id"],
        certification_code=identity["certification_code"],
        related_question_version_id=identity["related_question_version_id"],
        observed_evidence=_clean_optional_text(meta.get("observed_evidence") or finding.get("description")),
        expected_rule_or_value=_clean_optional_text(meta.get("expected_rule_or_value")),
        suggested_correction=_clean_optional_text(meta.get("suggested_correction")),
        confidence=_validate_confidence(finding.get("confidence")),
        model_metadata=_model_metadata(finding, context),
        prompt_version=context.prompt_version,
        ruleset_version=context.ruleset_version,
        supporting_references=_references_from_finding_evidence(finding.get("evidence")),
        generated_at=context.generated_at or _utc_now_iso(),
    )
    serialized = serialize_evidence_contract(contract)
    validate_evidence_contract(serialized, strict_identity=True)
    return serialized


def build_hybrid_evidence(finding: dict, context: AuditEvidenceContext) -> dict:
    """Construct a validated hybrid evidence contract."""
    source = _infer_audit_source(finding, context)
    if source == "deterministic":
        payload = build_deterministic_evidence(finding, context)
    elif source == "llm":
        payload = build_llm_evidence(finding, context)
    else:
        identity = _resolve_question_identity(finding, context)
        contract = AuditEvidenceContract(
            contract_version=EVIDENCE_CONTRACT_VERSION,
            finding_code=_require_non_empty(finding.get("finding_code"), "finding_code"),
            finding_category=_require_non_empty(finding.get("finding_type"), "finding_type"),
            severity=_require_non_empty(finding.get("severity"), "severity"),
            materiality=finding.get("materiality") or "warning",
            audit_source="hybrid",
            summary=_require_non_empty(finding.get("title"), "title"),
            detailed_rationale=_require_non_empty(finding.get("description"), "description"),
            field_path=_clean_optional_text(finding.get("field_path")),
            question_id=identity["question_id"],
            question_version_id=identity["question_version_id"],
            question_version_number=_validate_version_number(identity["question_version_number"]),
            target_candidate_id=identity["target_candidate_id"],
            certification_code=identity["certification_code"],
            related_question_version_id=identity["related_question_version_id"],
            observed_evidence=_clean_optional_text(finding.get("description")),
            expected_rule_or_value=_clean_optional_text(finding.get("finding_code")),
            confidence=_validate_confidence(finding.get("confidence")),
            deterministic_rule=_deterministic_rule_metadata(finding, context),
            model_metadata=_model_metadata(finding, context),
            prompt_version=context.prompt_version,
            ruleset_version=_clean_optional_text(
                (finding.get("metadata") or {}).get("ruleset_version") or context.ruleset_version
            ),
            supporting_references=_references_from_finding_evidence(finding.get("evidence")),
            generated_at=context.generated_at or _utc_now_iso(),
        )
        payload = serialize_evidence_contract(contract)
    payload["audit_source"] = source if source != "hybrid" else "hybrid"
    validate_evidence_contract(payload, strict_identity=True)
    return payload


def normalize_legacy_evidence_contract(
    finding: dict,
    *,
    context: Optional[AuditEvidenceContext] = None,
) -> dict:
    """Build a best-effort evidence contract from a legacy finding payload."""
    ctx = context or AuditEvidenceContext(audit_type="deterministic")
    existing = (finding.get("metadata") or {}).get("evidence_contract")
    if isinstance(existing, dict) and existing.get("contract_version"):
        merged = dict(existing)
        merged.setdefault("legacy", True)
        validate_evidence_contract(merged, strict_identity=False)
        return merged

    identity = _resolve_question_identity(finding, ctx)
    contract = AuditEvidenceContract(
        contract_version=EVIDENCE_CONTRACT_VERSION,
        finding_code=_require_non_empty(finding.get("finding_code"), "finding_code"),
        finding_category=_require_non_empty(finding.get("finding_type"), "finding_type"),
        severity=_require_non_empty(finding.get("severity"), "severity"),
        materiality=finding.get("materiality") or "warning",
        audit_source=_infer_audit_source(finding, ctx),
        summary=_require_non_empty(finding.get("title"), "title"),
        detailed_rationale=_require_non_empty(finding.get("description"), "description"),
        field_path=_clean_optional_text(finding.get("field_path")),
        question_id=identity["question_id"],
        question_version_id=identity["question_version_id"],
        question_version_number=_validate_version_number(identity["question_version_number"])
        if identity["question_version_number"] is not None
        else None,
        target_candidate_id=identity["target_candidate_id"],
        certification_code=identity["certification_code"],
        related_question_version_id=identity["related_question_version_id"],
        observed_evidence=_clean_optional_text(finding.get("description")),
        expected_rule_or_value=_clean_optional_text(finding.get("finding_code")),
        confidence=_validate_confidence(finding.get("confidence")),
        deterministic_rule=_deterministic_rule_metadata(finding, ctx),
        model_metadata=_model_metadata(finding, ctx),
        prompt_version=ctx.prompt_version,
        ruleset_version=_clean_optional_text(
            (finding.get("metadata") or {}).get("ruleset_version") or ctx.ruleset_version
        ),
        supporting_references=_references_from_finding_evidence(finding.get("evidence") or []),
        generated_at=ctx.generated_at or _utc_now_iso(),
        legacy=True,
    )
    serialized = serialize_evidence_contract(contract)
    validate_evidence_contract(serialized, strict_identity=False)
    return serialized


def attach_evidence_contracts(
    findings: Sequence[dict],
    context: AuditEvidenceContext,
) -> List[dict]:
    """Attach validated evidence contracts to findings before persistence."""
    enriched: List[dict] = []
    for finding in findings:
        item = dict(finding)
        meta = dict(item.get("metadata") or {})
        if context.audit_type == "deterministic":
            contract = build_deterministic_evidence(item, context)
        elif context.audit_type == "llm":
            contract = build_llm_evidence(item, context)
        elif context.audit_type == "hybrid":
            contract = build_hybrid_evidence(item, context)
        else:
            contract = normalize_legacy_evidence_contract(item, context=context)
        meta["evidence_contract"] = contract
        item["metadata"] = meta
        enriched.append(item)
    return enriched
