#!/usr/bin/env python3
"""
V49-AUDIT-WORKER-03 — bounded local runner for one audit-run vertical slice.

This script validates exactly one ``ai_quality_audit_smoke`` audit run by
reading a single ``background_jobs`` row for identifiers only, then calling
``process_ai_quality_audit_job`` directly. Evidence uses the approved V1
path (``bm25_question_match_v1`` frozen at enqueue time). No V48 hybrid
modules are imported or called.

What this runner does
---------------------
* Read-only preflight on one explicit ``--job-id`` (existence, job type,
  pending status, payload fields).
* Dry-run by default: plan only, zero writes, zero provider calls.
* With ``--execute``: one direct call to ``process_ai_quality_audit_job``,
  which internally uses the atomic ``claim_ai_quality_audit_pass_v1``
  RPC scoped to the explicit ``audit_run_id``.

What this runner intentionally does NOT do
------------------------------------------
* It does NOT claim, lease, complete, fail, or heartbeat any
  ``background_jobs`` row. The outer queue row remains unchanged.
* It does NOT sweep the queue, loop, or act as a queue completion
  mechanism. Normal queue dispatch remains owned by ``BackgroundWorker``.
* It does NOT inspect or order other pending jobs (legacy V48 jobs are
  never read or touched).

Usage
-----
    python scripts/v49_run_single_audit_job.py --job-id <background_jobs.id>
    python scripts/v49_run_single_audit_job.py --job-id <uuid> --execute

Environment (only consulted with --execute)
--------------------------------------------
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
    CERTBOUND_AI_QUALITY_PRIMARY_LLM_PROVIDER / CERTBOUND_LLM_PROVIDER
    CERTBOUND_AI_QUALITY_DISPUTE_LLM_PROVIDER (optional)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workers.background_worker import build_supabase_client  # noqa: E402

logger = logging.getLogger(__name__)

TARGET_JOB_TYPE = "ai_quality_audit_smoke"
DEFAULT_WORKER_ID = "v49-local-single-job"
DEFAULT_LEASE_SECONDS = 300
DEFAULT_SCHEMA_VERSION = "v48.1"

_TABLE_NAME = "background_jobs"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_REDACTED_ROW_FIELDS = (
    "job_type",
    "job_status",
    "attempt_count",
    "max_attempts",
)

_FORBIDDEN_QUEUE_RPCS = frozenset({
    "claim_background_job_v1",
    "fail_background_job_v1",
    "complete_background_job_v1",
    "heartbeat_background_job_v1",
})


class SingleJobRunnerError(RuntimeError):
    """Raised when preflight or execution cannot proceed safely."""


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _validate_uuid(value: str, field_name: str) -> str:
    text = str(value).strip()
    if not _UUID_RE.match(text):
        raise SingleJobRunnerError(
            f"payload {field_name} must be a UUID string, got {value!r}"
        )
    return text.lower()


def _table_rows(client, *, build_query) -> List[Dict[str, Any]]:
    result = build_query(client.table(_TABLE_NAME)).execute()
    if getattr(result, "error", None):
        raise SingleJobRunnerError(
            f"table read {_TABLE_NAME!r} failed: {result.error}"
        )
    return result.data or []


def fetch_job_row(client, job_id: str) -> Optional[Dict[str, Any]]:
    """Read-only fetch of one background_jobs row by id. Never mutates."""
    rows = _table_rows(
        client,
        build_query=lambda q: q.select("*").eq("id", job_id).limit(1),
    )
    return rows[0] if rows else None


@dataclass
class SingleJobPlan:
    job_id: str
    exists: bool
    row: Optional[Dict[str, Any]] = None
    ready_to_execute: bool = False
    blocking_reason: Optional[str] = None
    audit_run_id: Optional[str] = None
    question_version_id: Optional[str] = None


def build_plan(client, job_id: str) -> SingleJobPlan:
    """Read-only preflight for one explicit background job. Never mutates."""
    row = fetch_job_row(client, job_id)
    if row is None:
        return SingleJobPlan(
            job_id=job_id,
            exists=False,
            blocking_reason="job_id not found",
        )

    plan = SingleJobPlan(job_id=job_id, exists=True, row=row)

    row_job_type = str(row.get("job_type"))
    if row_job_type != TARGET_JOB_TYPE:
        plan.blocking_reason = (
            f"job_type {row_job_type!r} is not the approved audit path "
            f"({TARGET_JOB_TYPE!r}); refusing to proceed"
        )
        return plan

    row_status = str(row.get("job_status"))
    if row_status != "pending":
        plan.blocking_reason = (
            f"job_status is {row_status!r}, not 'pending' — refusing before "
            "the audit layer is invoked"
        )
        return plan

    payload = row.get("payload")
    if not isinstance(payload, dict):
        plan.blocking_reason = "payload must be a JSON object"
        return plan

    audit_run_id = payload.get("audit_run_id")
    question_version_id = payload.get("question_version_id")
    if audit_run_id is None or question_version_id is None:
        plan.blocking_reason = (
            "payload must contain audit_run_id and question_version_id"
        )
        return plan

    try:
        plan.audit_run_id = _validate_uuid(audit_run_id, "audit_run_id")
        plan.question_version_id = _validate_uuid(
            question_version_id,
            "question_version_id",
        )
    except SingleJobRunnerError as exc:
        plan.blocking_reason = str(exc)
        return plan

    plan.ready_to_execute = True
    return plan


def format_plan_summary(plan: SingleJobPlan) -> str:
    """Redacted, human-readable summary. Never prints raw payload/metadata."""
    lines = [
        f"requested_job_id: {plan.job_id}",
        f"exists: {plan.exists}",
    ]
    if plan.row:
        for field_name in _REDACTED_ROW_FIELDS:
            lines.append(f"{field_name}: {plan.row.get(field_name)}")
        lines.append(f"audit_run_id: {plan.audit_run_id}")
        lines.append(f"question_version_id: {plan.question_version_id}")
    lines.append(f"ready_to_execute: {plan.ready_to_execute}")
    lines.append("outer_background_job_mutated: false")
    lines.append("queue_claim_rpc_called: false")
    lines.append("v1_retrieval_only: true")
    if plan.blocking_reason:
        lines.append(f"blocking_reason: {plan.blocking_reason}")
    return "\n".join(lines)


@dataclass
class SingleJobRunResult:
    audit_run_id: str
    question_version_id: str
    run_status: str
    audit_execution_started: bool = True
    audit_execution_completed: bool = False
    finding_count: int = 0
    passes_executed: Optional[List[str]] = None


def execute_single_job(
    client,
    plan: SingleJobPlan,
    *,
    worker_id: str,
    lease_seconds: int,
    schema_version: str,
    ai_quality_providers,
) -> SingleJobRunResult:
    """Run one audit vertical slice via process_ai_quality_audit_job.

    Never calls background queue lifecycle RPCs. The outer background_jobs
    row is not read again for mutation and is not updated by this function.
    """
    if not plan.ready_to_execute:
        raise SingleJobRunnerError(
            plan.blocking_reason or "job is not ready to execute"
        )
    if plan.audit_run_id is None or plan.question_version_id is None:
        raise SingleJobRunnerError("preflight did not produce audit identifiers")

    from workers.ai_quality_audit_worker import (  # noqa: PLC0415
        process_ai_quality_audit_job,
    )

    summary = process_ai_quality_audit_job(
        client,
        {
            "audit_run_id": plan.audit_run_id,
            "question_version_id": plan.question_version_id,
        },
        ai_quality_providers,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        schema_version=schema_version,
    )

    run_status = str(summary.get("run_status") or "")
    completed = run_status == "completed"

    return SingleJobRunResult(
        audit_run_id=plan.audit_run_id,
        question_version_id=plan.question_version_id,
        run_status=run_status,
        audit_execution_started=True,
        audit_execution_completed=completed,
        finding_count=int(summary.get("finding_count") or 0),
        passes_executed=list(summary.get("passes_executed") or []),
    )


def format_execute_summary(
    plan: SingleJobPlan,
    result: SingleJobRunResult,
) -> str:
    """Redacted JSON summary after execution."""
    payload = {
        "final_status": result.run_status,
        "requested_job_id": plan.job_id,
        "audit_run_id": result.audit_run_id,
        "question_version_id": result.question_version_id,
        "audit_execution_started": result.audit_execution_started,
        "audit_execution_completed": result.audit_execution_completed,
        "outer_background_job_mutated": False,
        "queue_claim_rpc_called": False,
        "v1_retrieval_only": True,
        "finding_count": result.finding_count,
        "passes_executed": result.passes_executed or [],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded local runner: validates one ai_quality_audit_smoke "
            "audit run via direct process_ai_quality_audit_job execution. "
            "Does not mutate the outer background_jobs row."
        ),
    )
    parser.add_argument(
        "--job-id",
        required=True,
        help="background_jobs.id (uuid) to read for audit identifiers only",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the audit vertical slice (default is dry-run/plan-only)",
    )
    parser.add_argument("--worker-id", default=DEFAULT_WORKER_ID)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--schema-version", default=DEFAULT_SCHEMA_VERSION)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        client = build_supabase_client()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        plan = build_plan(client, args.job_id)
    except SingleJobRunnerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_plan_summary(plan))

    if not plan.exists:
        print("ERROR: job_id not found", file=sys.stderr)
        return 1
    if not plan.ready_to_execute:
        print(f"ERROR: {plan.blocking_reason}", file=sys.stderr)
        return 1

    if not args.execute:
        print(
            "DRY RUN: read-only plan only; no audit RPC, no provider calls, "
            "no database writes; outer background job will remain unchanged."
        )
        return 0

    if running_under_pytest():
        print("ERROR: refusing live execution under pytest.", file=sys.stderr)
        return 2

    try:
        from workers.ai_quality_provider_factory import (  # noqa: PLC0415
            AiQualityProviderConfigError,
            build_ai_quality_providers_from_env,
        )

        providers = build_ai_quality_providers_from_env(required=True)
    except AiQualityProviderConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        result = execute_single_job(
            client,
            plan,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            schema_version=args.schema_version,
            ai_quality_providers=providers,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(format_execute_summary(plan, result))
    return 0 if result.audit_execution_completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
