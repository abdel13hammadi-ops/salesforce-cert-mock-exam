"""
Question-version publication gate helpers (V45 Phase 4E).

Mirrors PostgreSQL eligibility rules for tests and admin UI.  Publication is
enforced in publish_question_version_v1; these helpers are read-only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

BLOCKING_FINDING_STATUSES = frozenset({"open", "accepted"})
NON_BLOCKING_FINDING_STATUSES = frozenset({"rejected", "resolved", "overridden"})


class PublicationGateError(ValueError):
    """Raised when publication status cannot be loaded or publish fails."""


def _clean_uuid(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def finding_tied_to_question_version(
    *,
    question_version_id: str,
    run_target_question_version_id: Optional[str],
    metadata: Optional[Mapping[str, Any]],
) -> bool:
    """Return True when a finding is explicitly tied to the exact version."""
    if not question_version_id:
        return False
    meta = metadata or {}
    contract = meta.get("evidence_contract") or {}
    candidates = [
        run_target_question_version_id,
        meta.get("question_version_id_a"),
        meta.get("question_version_id_b"),
        contract.get("question_version_id"),
    ]
    for candidate in candidates:
        cleaned = _clean_uuid(candidate)
        if cleaned and cleaned.lower() == question_version_id.lower():
            return True
    return False


def finding_blocks_publication(
    finding: Mapping[str, Any],
    *,
    question_version_id: str,
    run_target_question_version_id: Optional[str] = None,
) -> bool:
    """Return True when a finding should block publication for the version."""
    if finding.get("materiality") != "blocking":
        return False
    status = str(finding.get("finding_status") or "").strip().lower()
    if status not in BLOCKING_FINDING_STATUSES:
        return False
    metadata = finding.get("metadata") or {}
    run_target = run_target_question_version_id
    if run_target is None:
        run_target = finding.get("run_target_question_version_id")
    return finding_tied_to_question_version(
        question_version_id=question_version_id,
        run_target_question_version_id=_clean_uuid(run_target),
        metadata=metadata,
    )


def summarize_blocking_findings(
    findings: List[Mapping[str, Any]],
    *,
    question_version_id: str,
) -> Dict[str, Any]:
    """Compute publishable state from in-memory finding rows."""
    blocking = [
        {
            "finding_id": f.get("finding_id") or f.get("id"),
            "finding_code": f.get("finding_code"),
            "finding_status": f.get("finding_status"),
            "materiality": f.get("materiality"),
            "title": f.get("title"),
        }
        for f in findings
        if finding_blocks_publication(f, question_version_id=question_version_id)
    ]
    count = len(blocking)
    return {
        "question_version_id": question_version_id,
        "publishable": count == 0,
        "blocking_finding_count": count,
        "blocking_findings": blocking,
    }


def _call_rpc(client, name: str, params: dict) -> List[dict]:
    result = client.rpc(name, params).execute()
    if getattr(result, "error", None):
        raise PublicationGateError(f"RPC {name!r} failed: {result.error}")
    return list(result.data or [])


def get_publication_status(client, *, question_version_id: str) -> dict:
    """Load publication eligibility from get_question_version_publication_status_v1."""
    qvid = _clean_uuid(question_version_id)
    if not qvid or not UUID_RE.match(qvid):
        raise PublicationGateError("question_version_id must be a UUID")
    rows = _call_rpc(
        client,
        "get_question_version_publication_status_v1",
        {"p_question_version_id": qvid},
    )
    if not rows:
        raise PublicationGateError("get_question_version_publication_status_v1 returned no rows")
    row = rows[0]
    return {
        "question_version_id": row.get("question_version_id"),
        "publishable": bool(row.get("publishable")),
        "blocking_finding_count": int(row.get("blocking_finding_count") or 0),
        "blocking_findings": row.get("blocking_findings") or [],
    }


def approve_question_version(
    client,
    *,
    question_version_id: str,
    actor_email: str,
    reason: str,
    event_data: Optional[dict] = None,
) -> dict:
    """Call approve_question_version_v1 and normalize errors for UI display.

    This only appends an ``approved`` event through the existing RPC; it does
    not duplicate any database-side eligibility checks in Python and does not
    publish. Re-approval of an already-approved version is idempotent at the
    RPC layer and is treated here as a normal successful result.
    """
    qvid = _clean_uuid(question_version_id)
    email = str(actor_email or "").strip().lower()
    note = str(reason or "").strip()
    if not qvid:
        raise PublicationGateError("question_version_id is required")
    if not email:
        raise PublicationGateError("actor email is required")
    if not note:
        raise PublicationGateError("approval reason is required")
    rows = _call_rpc(
        client,
        "approve_question_version_v1",
        {
            "p_question_version_id": qvid,
            "p_actor_email": email,
            "p_reason": note,
            "p_event_data": event_data or {},
        },
    )
    if not rows:
        raise PublicationGateError("approve_question_version_v1 returned no rows")
    return rows[0]


def publish_question_version(
    client,
    *,
    question_version_id: str,
    actor_email: str,
    reason: str,
    event_data: Optional[dict] = None,
) -> dict:
    """Call publish_question_version_v1 and normalize errors for UI display."""
    qvid = _clean_uuid(question_version_id)
    email = str(actor_email or "").strip().lower()
    note = str(reason or "").strip()
    if not qvid:
        raise PublicationGateError("question_version_id is required")
    if not email:
        raise PublicationGateError("actor email is required")
    if not note:
        raise PublicationGateError("publish reason is required")
    try:
        rows = _call_rpc(
            client,
            "publish_question_version_v1",
            {
                "p_question_version_id": qvid,
                "p_actor_email": email,
                "p_reason": note,
                "p_event_data": event_data or {},
            },
        )
    except PublicationGateError as exc:
        message = str(exc)
        if "publication blocked" in message.lower():
            raise PublicationGateError(
                "Publication is blocked by unresolved blocking audit findings."
            ) from exc
        raise
    if not rows:
        raise PublicationGateError("publish_question_version_v1 returned no rows")
    return rows[0]


def format_publication_status_message(status: Mapping[str, Any]) -> str:
    """Human-readable publication status for admin UI."""
    if status.get("publishable"):
        return "Publishable — no unresolved blocking audit findings for this version."
    count = int(status.get("blocking_finding_count") or 0)
    return (
        f"Blocking publication: {count} unresolved blocking finding(s). "
        "Accepted and open findings still block publication."
    )


def format_blocking_findings_summary(status: Mapping[str, Any]) -> str:
    findings = status.get("blocking_findings") or []
    if not findings:
        return ""
    parts = []
    for item in findings:
        fid = str(item.get("finding_id") or "")[:8]
        code = item.get("finding_code") or "?"
        fstatus = item.get("finding_status") or "?"
        parts.append(f"{fid}…/{code}/{fstatus}")
    return "; ".join(parts)
