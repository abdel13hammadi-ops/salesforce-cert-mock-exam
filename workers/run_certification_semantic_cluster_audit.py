#!/usr/bin/env python3
"""
Plan or enqueue one certification_semantic_cluster_audit background job.

Default mode is dry-run (no Supabase writes, no model download). Pass
``--enqueue`` to create a background job after reviewing the printed plan.
Requires ``CERTBOUND_ALLOW_JOB_ENQUEUE=1``.

Usage::

    # Dry-run (default; uses env vars or .streamlit/secrets.toml)
    python -m workers.run_certification_semantic_cluster_audit \\
        --certification-exam-name "Platform Administrator"

    set CERTBOUND_ALLOW_JOB_ENQUEUE=1
    python -m workers.run_certification_semantic_cluster_audit \\
        --certification-exam-name "Platform Administrator" \\
        --enqueue
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

from utils.access_control import SupabaseAdminConfigError, create_supabase_admin_client
from workers.certification_question_loader import (
    load_certification_current_question_versions,
)
from workers.semantic_cluster_detector import (
    DEFAULT_MODEL_NAME,
    DEFAULT_RULESET_VERSION,
    DEFAULT_SEMANTIC_CLUSTER_THRESHOLDS,
    SCAN_TYPE_SEMANTIC_CLUSTER,
    build_semantic_cluster_thresholds,
)

_LIVE_FLAG = "CERTBOUND_ALLOW_JOB_ENQUEUE"
_ENQUEUE_RPC = "enqueue_background_job_v1"
_JOB_TYPE = "certification_semantic_cluster_audit"


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def assert_enqueue_allowed() -> None:
    if running_under_pytest():
        raise RuntimeError(
            "Refusing to enqueue certification semantic cluster audit under pytest."
        )
    if os.environ.get(_LIVE_FLAG) != "1":
        raise RuntimeError(
            f"Refusing live job enqueue. Set {_LIVE_FLAG}=1 to enqueue a job."
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
    model_name: Optional[str] = None,
    stem_edge_threshold: Optional[float] = None,
    full_edge_threshold: Optional[float] = None,
    correct_edge_threshold: Optional[float] = None,
    cohesion_min_similarity: Optional[float] = None,
    cohesion_signal: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    payload: dict = {
        "certification_exam_name": certification_exam_name.strip(),
    }
    if created_by:
        payload["created_by"] = created_by.strip()
    if ruleset_version:
        payload["ruleset_version"] = ruleset_version.strip()
    if model_name:
        payload["model_name"] = model_name.strip()
    if stem_edge_threshold is not None:
        payload["stem_edge_threshold"] = stem_edge_threshold
    if full_edge_threshold is not None:
        payload["full_edge_threshold"] = full_edge_threshold
    if correct_edge_threshold is not None:
        payload["correct_edge_threshold"] = correct_edge_threshold
    if cohesion_min_similarity is not None:
        payload["cohesion_min_similarity"] = cohesion_min_similarity
    if cohesion_signal:
        payload["cohesion_signal"] = cohesion_signal.strip()
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
        "p_model_name": payload.get("model_name"),
        "p_prompt_version": None,
        "p_estimated_cost_usd": None,
        "p_metadata": metadata or {},
    }


def enqueue_certification_semantic_cluster_audit_job(client, params: dict) -> dict:
    result = client.rpc(_ENQUEUE_RPC, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {_ENQUEUE_RPC!r} returned error: {result.error}")
    rows = result.data or []
    if not rows:
        raise RuntimeError(f"RPC {_ENQUEUE_RPC!r} returned no rows")
    return rows[0]


def load_completed_semantic_cluster_audit_keys(
    client,
    *,
    certification_exam_name: str,
    ruleset_version: str,
    model_name: str,
) -> Set[Tuple[str, str, str]]:
    """Return completed scan keys: (certification, ruleset, model_name)."""
    cert = str(certification_exam_name).strip()
    ruleset = str(ruleset_version).strip()
    model = str(model_name).strip()
    result = (
        client.table("audit_runs")
        .select("ruleset_version, metadata")
        .eq("audit_type", "deterministic")
        .eq("run_status", "completed")
        .eq("ruleset_version", ruleset)
        .filter("metadata->>scan_type", "eq", SCAN_TYPE_SEMANTIC_CLUSTER)
        .filter("metadata->>certification_exam_name", "eq", cert)
        .filter("metadata->>model_name", "eq", model)
        .execute()
    )
    keys: Set[Tuple[str, str, str]] = set()
    for row in result.data or []:
        metadata = row.get("metadata") or {}
        row_cert = str(metadata.get("certification_exam_name") or cert).strip()
        row_ruleset = str(row.get("ruleset_version") or ruleset).strip()
        row_model = str(metadata.get("model_name") or model).strip()
        if row_cert and row_ruleset and row_model:
            keys.add((row_cert, row_ruleset, row_model))
    return keys


def load_active_semantic_cluster_job_keys(
    client,
    *,
    certification_exam_name: str,
    ruleset_version: str,
    model_name: str,
) -> Set[Tuple[str, str, str]]:
    cert = str(certification_exam_name).strip()
    ruleset = str(ruleset_version).strip()
    model = str(model_name).strip()
    result = (
        client.table("background_jobs")
        .select("payload")
        .eq("job_type", _JOB_TYPE)
        .in_("job_status", ["pending", "leased", "running"])
        .execute()
    )
    keys: Set[Tuple[str, str, str]] = set()
    for row in result.data or []:
        payload = row.get("payload") or {}
        row_cert = str(payload.get("certification_exam_name") or "").strip()
        row_ruleset = str(payload.get("ruleset_version") or DEFAULT_RULESET_VERSION).strip()
        row_model = str(payload.get("model_name") or DEFAULT_MODEL_NAME).strip()
        if row_cert == cert and row_ruleset == ruleset and row_model == model:
            keys.add((row_cert, row_ruleset, row_model))
    return keys


def build_dry_run_report(
    *,
    certification_exam_name: str,
    question_count: int,
    ruleset_version: str,
    model_name: str,
    thresholds,
    completed_scan: bool,
    active_job: bool,
) -> str:
    lines = [
        "mode: dry-run",
        f"job_type: {_JOB_TYPE}",
        f"certification_exam_name: {certification_exam_name}",
        f"question_count: {question_count}",
        f"ruleset_version: {ruleset_version}",
        f"model_name: {model_name}",
        f"stem_threshold: {thresholds.stem_edge_threshold}",
        f"full_question_threshold: {thresholds.full_edge_threshold}",
        f"cohesion_signal: {thresholds.cohesion_signal}",
        f"cohesion_threshold: {thresholds.cohesion_min_similarity}",
        f"completed_scan_exists: {completed_scan}",
        f"active_job_exists: {active_job}",
        "model_download: skipped",
    ]
    if completed_scan:
        lines.append(
            "note: completed audit run already exists for this certification/ruleset/model"
        )
    if active_job:
        lines.append(
            "note: active background job already exists for this certification/ruleset/model"
        )
    return "\n".join(lines)


def format_enqueue_report(row: dict, *, certification_exam_name: str) -> str:
    return "\n".join([
        f"job_id: {row.get('job_id')}",
        f"job_status: {row.get('job_status')}",
        f"job_type: {_JOB_TYPE}",
        f"certification_exam_name: {certification_exam_name}",
    ])


def run_dry_run(
    client,
    *,
    certification_exam_name: str,
    ruleset_version: str,
    model_name: str,
    thresholds,
) -> str:
    rows = load_certification_current_question_versions(client, certification_exam_name)
    completed = load_completed_semantic_cluster_audit_keys(
        client,
        certification_exam_name=certification_exam_name,
        ruleset_version=ruleset_version,
        model_name=model_name,
    )
    active = load_active_semantic_cluster_job_keys(
        client,
        certification_exam_name=certification_exam_name,
        ruleset_version=ruleset_version,
        model_name=model_name,
    )
    key = (certification_exam_name.strip(), ruleset_version.strip(), model_name.strip())
    return build_dry_run_report(
        certification_exam_name=certification_exam_name,
        question_count=len(rows),
        ruleset_version=ruleset_version,
        model_name=model_name,
        thresholds=thresholds,
        completed_scan=key in completed,
        active_job=key in active,
    )


def run_enqueue(
    client,
    payload: dict,
    *,
    priority: int = 100,
    max_attempts: int = 3,
    available_at: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_by: Optional[str] = None,
    allow_repeat: bool = False,
) -> str:
    certification_exam_name = validate_payload(payload)
    ruleset_version = str(payload.get("ruleset_version") or DEFAULT_RULESET_VERSION).strip()
    model_name = str(payload.get("model_name") or DEFAULT_MODEL_NAME).strip()
    if not allow_repeat:
        completed = load_completed_semantic_cluster_audit_keys(
            client,
            certification_exam_name=certification_exam_name,
            ruleset_version=ruleset_version,
            model_name=model_name,
        )
        active = load_active_semantic_cluster_job_keys(
            client,
            certification_exam_name=certification_exam_name,
            ruleset_version=ruleset_version,
            model_name=model_name,
        )
        key = (certification_exam_name, ruleset_version, model_name)
        if key in completed:
            raise RuntimeError(
                "completed semantic cluster audit already exists for "
                f"{certification_exam_name!r} ruleset={ruleset_version!r} "
                f"model={model_name!r}; pass --allow-repeat to enqueue anyway"
            )
        if key in active:
            raise RuntimeError(
                "active semantic cluster audit job already exists for "
                f"{certification_exam_name!r} ruleset={ruleset_version!r} "
                f"model={model_name!r}"
            )

    params = build_enqueue_params(
        payload,
        priority=priority,
        max_attempts=max_attempts,
        available_at=available_at,
        metadata=metadata,
        created_by=created_by,
    )
    row = enqueue_certification_semantic_cluster_audit_job(client, params)
    return format_enqueue_report(row, certification_exam_name=certification_exam_name)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan or enqueue one certification_semantic_cluster_audit job.",
    )
    parser.add_argument(
        "--certification-exam-name",
        required=True,
        help="Certification exam_name to scan for semantic concept clusters",
    )
    parser.add_argument(
        "--created-by",
        default="certbound-local-enqueue",
        help="Actor recorded on the background job and audit run",
    )
    parser.add_argument("--ruleset-version", default=DEFAULT_RULESET_VERSION)
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument(
        "--stem-threshold",
        type=float,
        default=DEFAULT_SEMANTIC_CLUSTER_THRESHOLDS.stem_edge_threshold,
    )
    parser.add_argument(
        "--full-threshold",
        type=float,
        default=DEFAULT_SEMANTIC_CLUSTER_THRESHOLDS.full_edge_threshold,
    )
    parser.add_argument(
        "--cohesion-threshold",
        type=float,
        default=DEFAULT_SEMANTIC_CLUSTER_THRESHOLDS.cohesion_min_similarity,
    )
    parser.add_argument(
        "--cohesion-signal",
        default=DEFAULT_SEMANTIC_CLUSTER_THRESHOLDS.cohesion_signal,
    )
    parser.add_argument("--priority", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="Create a background job (default is dry-run)",
    )
    parser.add_argument(
        "--allow-repeat",
        action="store_true",
        help="Allow enqueue even when a completed scan exists",
    )
    args = parser.parse_args(argv)

    thresholds = build_semantic_cluster_thresholds(
        stem_edge_threshold=args.stem_threshold,
        full_edge_threshold=args.full_threshold,
        cohesion_min_similarity=args.cohesion_threshold,
        cohesion_signal=args.cohesion_signal,
    )
    payload = build_payload(
        certification_exam_name=args.certification_exam_name,
        created_by=args.created_by,
        ruleset_version=args.ruleset_version,
        model_name=args.model_name,
        stem_edge_threshold=thresholds.stem_edge_threshold,
        full_edge_threshold=thresholds.full_edge_threshold,
        cohesion_min_similarity=thresholds.cohesion_min_similarity,
        cohesion_signal=thresholds.cohesion_signal,
    )

    try:
        client = create_supabase_admin_client()
    except SupabaseAdminConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not args.enqueue:
        print(
            run_dry_run(
                client,
                certification_exam_name=args.certification_exam_name,
                ruleset_version=args.ruleset_version,
                model_name=args.model_name,
                thresholds=thresholds,
            )
        )
        return 0

    assert_enqueue_allowed()
    report = run_enqueue(
        client,
        payload,
        priority=args.priority,
        max_attempts=args.max_attempts,
        created_by=args.created_by,
        allow_repeat=args.allow_repeat,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
