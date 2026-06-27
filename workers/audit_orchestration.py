"""
Reusable audit orchestration layer (Phase 8E).

Provides ``orchestrate_audit()``, which manages the full lifecycle of one
audit run through three Supabase RPCs:

  1. create_audit_run_v1   — initialise the run (pending)
  2. check_fn()            — execute the audit engine (caller-supplied)
  3. complete_audit_run_v1 — persist findings and transition to completed

Failure contract
----------------
* If ``create_audit_run_v1`` raises, no ``end_audit_run_v1`` is attempted
  (the run row was never created).
* If ``check_fn`` raises after the run is created, ``end_audit_run_v1`` is
  called with ``p_final_status='failed'`` (best-effort, swallows its own
  errors) and the original exception is re-raised.

This module has no dependency on ``workers.job_handlers`` to avoid circular
imports.  The ``_call_rpc`` helper is defined locally.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local RPC helper (mirrors job_handlers._call_rpc — no shared dep needed)
# ---------------------------------------------------------------------------

def _call_rpc(client, name: str, params: dict) -> dict:
    """Invoke a Supabase RPC and return the first result row.

    Raises RuntimeError when the response carries an error or returns no rows.
    """
    result = client.rpc(name, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {name!r} failed: {result.error}")
    rows = result.data or []
    if not rows:
        raise RuntimeError(f"RPC {name!r} returned no rows")
    return rows[0]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def orchestrate_audit(
    client,
    *,
    audit_type: str,
    target_question_version_id: Optional[str],
    target_candidate_id: Optional[str],
    created_by: str,
    ruleset_version: Optional[str],
    resource_snapshot: Optional[dict],
    metadata: Optional[dict],
    check_fn: Callable[[], List[dict]],
    question_snapshot: Optional[dict] = None,
    model_name: Optional[str] = None,
    prompt_version: Optional[str] = None,
    provider_info: Optional[dict] = None,
) -> Dict[str, object]:
    """Manage one audit run lifecycle.

    Parameters
    ----------
    client:
        Supabase client (real or mock). Must expose ``.rpc(name, params).execute()``.
    audit_type:
        One of 'deterministic', 'llm', 'hybrid', 'human'.
    target_question_version_id / target_candidate_id:
        Exactly one must be non-None; validated by ``create_audit_run_v1``.
    created_by:
        Identifier of the caller (email or service name).
    ruleset_version:
        Optional ruleset version string passed to the audit run.
    resource_snapshot:
        Optional resource snapshot dict attached to the run.
    metadata:
        Optional extra metadata merged into the run.
    check_fn:
        Zero-argument callable that executes the audit engine and returns
        a list of finding dicts compatible with ``complete_audit_run_v1``.
    question_snapshot:
        Optional immutable question snapshot used to anchor evidence contracts.
    model_name / prompt_version / provider_info:
        Optional LLM provenance attached to evidence contracts.  Handlers may
        populate ``provider_info`` from inside ``check_fn`` (e.g. provider
        request id) before evidence attachment runs.

    Returns
    -------
    dict with keys: audit_run_id, run_status, finding_count, evidence_count

    Raises
    ------
    RuntimeError
        Propagated from any failing RPC or re-raised after ``check_fn``
        failure (after best-effort ``end_audit_run_v1``).
    """
    # ---- Step 1: create audit run ----
    # Any error here propagates immediately; no run row exists to clean up.
    create_row = _call_rpc(
        client,
        "create_audit_run_v1",
        {
            "p_audit_type":                 audit_type,
            "p_target_question_version_id": target_question_version_id,
            "p_target_candidate_id":        target_candidate_id,
            "p_ruleset_version":            ruleset_version,
            "p_resource_snapshot":          resource_snapshot or {},
            "p_created_by":                 created_by,
            "p_metadata":                   metadata or {},
        },
    )
    audit_run_id = str(create_row["audit_run_id"])
    logger.info(
        "audit run created: audit_run_id=%s type=%s", audit_run_id, audit_type
    )

    # ---- Step 2: execute the audit engine ----
    from workers.audit_evidence_contract import (  # noqa: PLC0415
        AuditEvidenceContext,
        attach_evidence_contracts,
    )

    try:
        findings = check_fn()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "audit engine raised after run creation: "
            "audit_run_id=%s reason=%r",
            audit_run_id,
            reason,
        )
        try:
            _call_rpc(
                client,
                "end_audit_run_v1",
                {
                    "p_audit_run_id": audit_run_id,
                    "p_final_status": "failed",
                    "p_reason":       reason,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "end_audit_run_v1 also failed: audit_run_id=%s", audit_run_id
            )
        raise  # re-raise the original check_fn exception

    evidence_context = AuditEvidenceContext.from_orchestration(
        audit_type=audit_type,
        target_question_version_id=target_question_version_id,
        target_candidate_id=target_candidate_id,
        ruleset_version=ruleset_version,
        question_snapshot=question_snapshot,
        model_name=model_name,
        prompt_version=prompt_version,
        provider_request_id=(provider_info or {}).get("provider_request_id"),
        run_metadata=metadata,
    )
    findings = attach_evidence_contracts(findings, evidence_context)

    # ---- Step 3: complete the run ----
    complete_row = _call_rpc(
        client,
        "complete_audit_run_v1",
        {
            "p_audit_run_id": audit_run_id,
            "p_findings":     findings,
            "p_metadata":     metadata or {},
        },
    )
    logger.info(
        "audit run completed: audit_run_id=%s findings=%s evidence=%s",
        audit_run_id,
        complete_row.get("finding_count"),
        complete_row.get("evidence_count"),
    )

    return {
        "audit_run_id":   audit_run_id,
        "run_status":     str(complete_row.get("run_status", "completed")),
        "finding_count":  complete_row.get("finding_count", len(findings)),
        "evidence_count": complete_row.get("evidence_count", 0),
    }
