"""
Admin audit review helpers (V45 Phase 4D).

Loads audit runs/findings through service-role RPCs and records human decisions.
Authorization is enforced in the Streamlit layer via require_admin(); RPCs remain
service_role-only at the database boundary.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Mapping, Optional

from workers.audit_evidence_contract import (
    AuditEvidenceContext,
    normalize_legacy_evidence_contract,
)

ALLOWED_DECISIONS = frozenset({"accepted", "rejected", "resolved"})

ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    "open": frozenset({"accepted", "rejected", "resolved"}),
    "accepted": frozenset({"rejected", "resolved"}),
    "rejected": frozenset({"accepted", "resolved"}),
    "resolved": frozenset(),
    "overridden": frozenset(),
}

_RUN_STATUSES = frozenset({"pending", "running", "completed", "failed", "cancelled"})
_AUDIT_TYPES = frozenset({"deterministic", "llm", "hybrid", "human"})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuditReviewError(ValueError):
    """Raised when review input or RPC calls fail validation."""


class AuditReviewAccessError(PermissionError):
    """Raised when a non-admin attempts a review write."""


def assert_admin_reviewer(
    *,
    is_admin_user: bool,
    is_admin_unlocked: bool,
    reviewer_email: Optional[str],
) -> str:
    """Server-side admin gate for review writes."""
    if not is_admin_user:
        raise AuditReviewAccessError("Admin access required.")
    if not is_admin_unlocked:
        raise AuditReviewAccessError("Admin unlock required.")
    email = str(reviewer_email or "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        raise AuditReviewAccessError("Authenticated reviewer email required.")
    return email


def validate_decision_value(decision: str) -> str:
    normalized = str(decision or "").strip().lower()
    if normalized not in ALLOWED_DECISIONS:
        raise AuditReviewError(
            f"invalid decision {decision!r}; allowed: {sorted(ALLOWED_DECISIONS)}"
        )
    return normalized


def validate_reviewer_note(note: str) -> str:
    cleaned = str(note or "").strip()
    if not cleaned:
        raise AuditReviewError("reviewer note is required")
    return cleaned


def validate_status_transition(previous_status: str, new_status: str) -> None:
    previous = str(previous_status or "").strip().lower()
    new = validate_decision_value(new_status)
    allowed = ALLOWED_TRANSITIONS.get(previous)
    if allowed is None:
        raise AuditReviewError(f"unknown previous status: {previous_status!r}")
    if previous == new:
        return
    if new not in allowed:
        raise AuditReviewError(
            f"invalid transition from {previous!r} to {new!r}"
        )


def escape_review_text(value: Any) -> str:
    """Escape dynamic text before rendering in Streamlit markdown."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _call_rpc(client, name: str, params: dict) -> List[dict]:
    result = client.rpc(name, params).execute()
    if getattr(result, "error", None):
        raise AuditReviewError(f"RPC {name!r} failed: {result.error}")
    return list(result.data or [])


def list_audit_runs(
    client,
    *,
    limit: int = 50,
    run_status: Optional[str] = None,
    audit_type: Optional[str] = None,
    certification_code: Optional[str] = None,
    blocking_only: bool = False,
) -> List[dict]:
    if run_status is not None and run_status not in _RUN_STATUSES:
        raise AuditReviewError(f"invalid run_status: {run_status!r}")
    if audit_type is not None and audit_type not in _AUDIT_TYPES:
        raise AuditReviewError(f"invalid audit_type: {audit_type!r}")
    return _call_rpc(
        client,
        "list_audit_runs_for_review_v1",
        {
            "p_limit": limit,
            "p_run_status": run_status,
            "p_audit_type": audit_type,
            "p_certification_code": certification_code or None,
            "p_blocking_only": bool(blocking_only),
        },
    )


def list_audit_findings(client, *, audit_run_id: str) -> List[dict]:
    if not str(audit_run_id or "").strip():
        raise AuditReviewError("audit_run_id is required")
    return _call_rpc(
        client,
        "list_audit_findings_for_review_v1",
        {"p_audit_run_id": audit_run_id},
    )


def get_finding_review_detail(client, *, finding_id: str) -> dict:
    if not str(finding_id or "").strip():
        raise AuditReviewError("finding_id is required")
    rows = _call_rpc(
        client,
        "get_audit_finding_review_detail_v1",
        {"p_finding_id": finding_id},
    )
    if not rows:
        raise AuditReviewError(f"finding not found: {finding_id}")
    return rows[0]


def build_evidence_contract_view(detail: Mapping[str, Any]) -> dict:
    """Return normalized evidence contract for display."""
    metadata = dict(detail.get("metadata") or {})
    finding = {
        "finding_code": detail.get("finding_code"),
        "finding_type": detail.get("finding_type"),
        "severity": detail.get("severity"),
        "materiality": detail.get("materiality"),
        "title": detail.get("title"),
        "description": detail.get("description"),
        "field_path": detail.get("field_path"),
        "confidence": detail.get("confidence"),
        "detector_name": detail.get("detector_name"),
        "detector_version": detail.get("detector_version"),
        "metadata": metadata,
        "evidence": detail.get("evidence") or [],
    }
    context = AuditEvidenceContext.from_orchestration(
        audit_type=str(
            (metadata.get("evidence_contract") or {}).get("audit_source")
            or metadata.get("audit_source")
            or "deterministic"
        ),
        target_question_version_id=detail.get("target_question_version_id"),
        target_candidate_id=None,
        ruleset_version=metadata.get("ruleset_version"),
        question_snapshot={
            "question_id": detail.get("question_id"),
            "version_number": detail.get("question_version_number"),
        },
    )
    return normalize_legacy_evidence_contract(finding, context=context)


def load_immutable_question_version(detail: Mapping[str, Any]) -> Optional[dict]:
    """Return immutable version snapshot from review detail; never substitute live question."""
    version_id = detail.get("target_question_version_id")
    if not version_id:
        return None
    if not detail.get("question_text") and not detail.get("question_id"):
        return None
    return {
        "question_version_id": version_id,
        "question_id": detail.get("question_id"),
        "version_number": detail.get("question_version_number"),
        "question_text": detail.get("question_text"),
        "explanation": detail.get("explanation"),
        "question_type": detail.get("question_type"),
        "select_count": detail.get("select_count"),
        "options": detail.get("options") or [],
    }


def record_finding_decision(
    client,
    *,
    finding_id: str,
    decision: str,
    reviewer_email: str,
    reviewer_note: str,
    is_admin_user: bool,
    is_admin_unlocked: bool,
) -> dict:
    """Persist a human review decision through the database RPC."""
    email = assert_admin_reviewer(
        is_admin_user=is_admin_user,
        is_admin_unlocked=is_admin_unlocked,
        reviewer_email=reviewer_email,
    )
    normalized_decision = validate_decision_value(decision)
    note = validate_reviewer_note(reviewer_note)

    rows = _call_rpc(
        client,
        "record_audit_finding_decision_v1",
        {
            "p_finding_id": finding_id,
            "p_decision": normalized_decision,
            "p_reviewer_email": email,
            "p_reviewer_note": note,
        },
    )
    if not rows:
        raise AuditReviewError("record_audit_finding_decision_v1 returned no rows")
    row = rows[0]
    validate_status_transition(row.get("previous_status", ""), row.get("new_status", ""))
    return row


def format_run_label(run: Mapping[str, Any]) -> str:
    run_id = str(run.get("audit_run_id") or "")[:8]
    audit_type = run.get("audit_type") or "?"
    status = run.get("run_status") or "?"
    cert = run.get("certification_code") or "unknown cert"
    findings = run.get("finding_count", 0)
    blocking = run.get("blocking_finding_count", 0)
    return (
        f"{run_id}… | {audit_type} | {status} | {cert} | "
        f"{findings} findings ({blocking} blocking)"
    )


def format_finding_label(finding: Mapping[str, Any]) -> str:
    code = finding.get("finding_code") or "?"
    severity = finding.get("severity") or "?"
    materiality = finding.get("materiality") or "?"
    status = finding.get("finding_status") or "?"
    title = finding.get("title") or ""
    if len(title) > 72:
        title = title[:69] + "..."
    return f"[{severity}/{materiality}/{status}] {code} — {title}"
