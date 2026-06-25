#!/usr/bin/env python3
"""
V45 Phase 4A: enqueue exactly one hybrid_audit background job.

Enqueues via ``enqueue_background_job_v1`` only. Does not claim or process
the job. The background worker must be run separately.

Usage::

    set CERTBOUND_ALLOW_LIVE_AI_TEST=1
    set CERTBOUND_LLM_PROVIDER=anthropic
    set CERTBOUND_ANTHROPIC_API_KEY=your-key-here
    set SUPABASE_URL=https://your-project.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
    python -m workers.run_hybrid_audit_pilot --payload-file path/to/payload.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from workers.background_worker import build_supabase_client
from workers.llm_provider_factory import build_llm_provider_from_env

_LIVE_FLAG = "CERTBOUND_ALLOW_LIVE_AI_TEST"
_ENQUEUE_RPC = "enqueue_background_job_v1"

_ENQUEUE_PARAM_NAMES = (
    "p_job_type",
    "p_payload",
    "p_priority",
    "p_max_attempts",
    "p_available_at",
    "p_created_by",
    "p_model_name",
    "p_prompt_version",
    "p_estimated_cost_usd",
    "p_metadata",
)

_REQUIRED_PAYLOAD_FIELDS = (
    "created_by",
    "model_name",
    "prompt_version",
    "ruleset_version",
    "system_prompt",
    "user_prompt",
    "question",
)


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def assert_pilot_allowed() -> None:
    if running_under_pytest():
        raise RuntimeError("Refusing to run hybrid audit pilot under pytest.")
    if os.environ.get(_LIVE_FLAG) != "1":
        raise RuntimeError(
            f"Refusing live hybrid audit pilot. Set {_LIVE_FLAG}=1 to enqueue a job."
        )


def assert_supabase_configured() -> None:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required to enqueue a job."
        )


def assert_anthropic_configured() -> None:
    provider = build_llm_provider_from_env()
    if provider is None:
        raise RuntimeError(
            "Anthropic is not configured. Set CERTBOUND_LLM_PROVIDER=anthropic "
            "and CERTBOUND_ANTHROPIC_API_KEY before enqueueing a hybrid audit job."
        )


def load_payload_file(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("payload file must contain a JSON object")
    return payload


def validate_hybrid_audit_payload(payload: dict) -> Tuple[str, str]:
    """Validate the hybrid handler payload and return (target_type, target_id)."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    tqv_id = str(payload.get("target_question_version_id") or "").strip() or None
    tc_id = str(payload.get("target_candidate_id") or "").strip() or None
    target_count = sum(1 for value in (tqv_id, tc_id) if value)
    if target_count != 1:
        raise ValueError(
            "exactly one of target_question_version_id or "
            "target_candidate_id must be provided"
        )

    for field in _REQUIRED_PAYLOAD_FIELDS:
        if field == "question":
            if not isinstance(payload.get("question"), dict):
                raise ValueError("payload field 'question' must be a non-null object")
            continue
        value = payload.get(field)
        if value is None or str(value).strip() == "":
            raise ValueError(f"payload field {field!r} must not be empty")

    if tqv_id:
        return "question_version", tqv_id
    return "candidate", tc_id  # type: ignore[return-value]


def build_enqueue_params(
    payload: dict,
    *,
    priority: int = 100,
    max_attempts: int = 3,
    available_at: Optional[str] = None,
    estimated_cost_usd: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> Dict[str, Any]:
    """Build the enqueue_background_job_v1 RPC parameter dict."""
    if available_at is None:
        available_at = datetime.now(timezone.utc).isoformat()

    return {
        "p_job_type": "hybrid_audit",
        "p_payload": payload,
        "p_priority": priority,
        "p_max_attempts": max_attempts,
        "p_available_at": available_at,
        "p_created_by": str(payload["created_by"]).strip(),
        "p_model_name": str(payload["model_name"]).strip(),
        "p_prompt_version": str(payload["prompt_version"]).strip(),
        "p_estimated_cost_usd": estimated_cost_usd,
        "p_metadata": metadata or {},
    }


def enqueue_hybrid_audit_job(client, params: dict) -> dict:
    """Call enqueue_background_job_v1 and return the first result row."""
    result = client.rpc(_ENQUEUE_RPC, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(
            f"RPC {_ENQUEUE_RPC!r} returned error: {result.error}"
        )
    rows = result.data or []
    if not rows:
        raise RuntimeError(f"RPC {_ENQUEUE_RPC!r} returned no rows")
    return rows[0]


def format_enqueue_report(
    row: dict,
    *,
    target_type: str,
    target_id: str,
    model_name: str,
    prompt_version: str,
) -> str:
    """Format the pilot enqueue summary for stdout."""
    lines = [
        f"job_id: {row.get('job_id')}",
        f"job_status: {row.get('job_status')}",
        f"target_type: {target_type}",
        f"target_id: {target_id}",
        f"model_name: {model_name}",
        f"prompt_version: {prompt_version}",
    ]
    return "\n".join(lines)


def run_enqueue_pilot(
    client,
    payload: dict,
    *,
    priority: int = 100,
    max_attempts: int = 3,
    available_at: Optional[str] = None,
    estimated_cost_usd: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> str:
    """Validate payload, enqueue one hybrid_audit job, return report text."""
    target_type, target_id = validate_hybrid_audit_payload(payload)
    params = build_enqueue_params(
        payload,
        priority=priority,
        max_attempts=max_attempts,
        available_at=available_at,
        estimated_cost_usd=estimated_cost_usd,
        metadata=metadata,
    )
    row = enqueue_hybrid_audit_job(client, params)
    return format_enqueue_report(
        row,
        target_type=target_type,
        target_id=target_id,
        model_name=str(payload["model_name"]).strip(),
        prompt_version=str(payload["prompt_version"]).strip(),
    )


def main(argv: Optional[list[str]] = None) -> int:
    if running_under_pytest():
        print("Refusing to run hybrid audit pilot under pytest.")
        return 2

    if os.environ.get(_LIVE_FLAG) != "1":
        print(
            f"Refusing live hybrid audit pilot. Set {_LIVE_FLAG}=1 to enqueue a job."
        )
        return 1

    try:
        assert_supabase_configured()
        assert_anthropic_configured()
    except RuntimeError as exc:
        print(str(exc))
        return 1

    parser = argparse.ArgumentParser(
        description="Enqueue one hybrid_audit background job (V45 Phase 4A)",
    )
    parser.add_argument(
        "--payload-file",
        required=True,
        help="Path to JSON file containing the hybrid_audit handler payload",
    )
    parser.add_argument("--priority", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--available-at",
        default=None,
        help="ISO-8601 timestamp for p_available_at (default: now UTC)",
    )
    parser.add_argument("--estimated-cost-usd", type=float, default=None)
    parser.add_argument(
        "--metadata-file",
        default=None,
        help="Optional JSON object for job-level p_metadata",
    )
    args = parser.parse_args(argv)

    try:
        payload = load_payload_file(Path(args.payload_file))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid payload file: {exc}")
        return 1

    job_metadata: Optional[dict] = None
    if args.metadata_file:
        try:
            raw = Path(args.metadata_file).read_text(encoding="utf-8")
            job_metadata = json.loads(raw)
            if not isinstance(job_metadata, dict):
                raise ValueError("metadata file must contain a JSON object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Invalid metadata file: {exc}")
            return 1

    try:
        client = build_supabase_client()
        report = run_enqueue_pilot(
            client,
            payload,
            priority=args.priority,
            max_attempts=args.max_attempts,
            available_at=args.available_at,
            estimated_cost_usd=args.estimated_cost_usd,
            metadata=job_metadata,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc))
        return 1

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
