#!/usr/bin/env python3
"""
Enqueue one certification_duplicate_audit background job.

Enqueues via ``enqueue_background_job_v1`` only. Does not claim or process the
job. Run the background worker separately (locally or on Render).

Usage::

    set CERTBOUND_ALLOW_JOB_ENQUEUE=1
    set SUPABASE_URL=https://your-project.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
    python -m workers.run_certification_duplicate_audit \\
        --certification-exam-name "Platform Administrator" \\
        --created-by you@example.com
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from workers.background_worker import build_supabase_client

_LIVE_FLAG = "CERTBOUND_ALLOW_JOB_ENQUEUE"
_ENQUEUE_RPC = "enqueue_background_job_v1"
_JOB_TYPE = "certification_duplicate_audit"


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def assert_enqueue_allowed() -> None:
    if running_under_pytest():
        raise RuntimeError(
            "Refusing to enqueue certification duplicate audit under pytest."
        )
    if os.environ.get(_LIVE_FLAG) != "1":
        raise RuntimeError(
            f"Refusing live job enqueue. Set {_LIVE_FLAG}=1 to enqueue a job."
        )


def assert_supabase_configured() -> None:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required to enqueue a job."
        )


def validate_payload(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    cert = str(payload.get("certification_exam_name") or "").strip()
    if not cert:
        raise ValueError("payload field 'certification_exam_name' must not be empty")
    return cert


def build_payload(
    *,
    certification_exam_name: str,
    created_by: Optional[str] = None,
    ruleset_version: Optional[str] = None,
    near_exact_threshold: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> dict:
    payload: dict = {
        "certification_exam_name": certification_exam_name.strip(),
    }
    if created_by:
        payload["created_by"] = created_by.strip()
    if ruleset_version:
        payload["ruleset_version"] = ruleset_version.strip()
    if near_exact_threshold is not None:
        payload["near_exact_threshold"] = near_exact_threshold
    if metadata:
        payload["metadata"] = metadata
    return payload


def build_enqueue_params(
    payload: dict,
    *,
    priority: int = 100,
    max_attempts: int = 3,
    available_at: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    actor = str(created_by or payload.get("created_by") or "certbound-local-enqueue").strip()
    if available_at is None:
        available_at = datetime.now(timezone.utc).isoformat()
    return {
        "p_job_type": _JOB_TYPE,
        "p_payload": payload,
        "p_priority": priority,
        "p_max_attempts": max_attempts,
        "p_available_at": available_at,
        "p_created_by": actor,
        "p_model_name": None,
        "p_prompt_version": None,
        "p_estimated_cost_usd": None,
        "p_metadata": metadata or {},
    }


def enqueue_certification_duplicate_audit_job(client, params: dict) -> dict:
    result = client.rpc(_ENQUEUE_RPC, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {_ENQUEUE_RPC!r} returned error: {result.error}")
    rows = result.data or []
    if not rows:
        raise RuntimeError(f"RPC {_ENQUEUE_RPC!r} returned no rows")
    return rows[0]


def format_enqueue_report(row: dict, *, certification_exam_name: str) -> str:
    return "\n".join([
        f"job_id: {row.get('job_id')}",
        f"job_status: {row.get('job_status')}",
        f"job_type: {_JOB_TYPE}",
        f"certification_exam_name: {certification_exam_name}",
    ])


def run_enqueue(
    client,
    payload: dict,
    *,
    priority: int = 100,
    max_attempts: int = 3,
    available_at: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_by: Optional[str] = None,
) -> str:
    certification_exam_name = validate_payload(payload)
    params = build_enqueue_params(
        payload,
        priority=priority,
        max_attempts=max_attempts,
        available_at=available_at,
        metadata=metadata,
        created_by=created_by,
    )
    row = enqueue_certification_duplicate_audit_job(client, params)
    return format_enqueue_report(row, certification_exam_name=certification_exam_name)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enqueue one certification_duplicate_audit background job.",
    )
    parser.add_argument(
        "--certification-exam-name",
        required=True,
        help="Certification exam_name to scan for duplicate stems",
    )
    parser.add_argument(
        "--created-by",
        default="certbound-local-enqueue",
        help="Actor recorded on the background job and audit run",
    )
    parser.add_argument("--ruleset-version", default="1.0.0")
    parser.add_argument("--priority", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)

    assert_enqueue_allowed()
    assert_supabase_configured()
    client = build_supabase_client()
    payload = build_payload(
        certification_exam_name=args.certification_exam_name,
        created_by=args.created_by,
        ruleset_version=args.ruleset_version,
    )
    report = run_enqueue(
        client,
        payload,
        priority=args.priority,
        max_attempts=args.max_attempts,
        created_by=args.created_by,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
