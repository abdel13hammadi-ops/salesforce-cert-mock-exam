#!/usr/bin/env python3
"""
Plan or enqueue one resource_ingestion background job from a local text file.

Default mode is dry-run (read-only validation and report). Pass ``--enqueue`` to
create a background job after reviewing the printed plan. Requires
``CERTBOUND_ALLOW_JOB_ENQUEUE=1``. Run the background worker separately.

Usage::

    python -m workers.run_resource_ingestion \\
        --resource-id <official-resources-uuid> \\
        --input-file path/to/exam-guide.txt \\
        --created-by you@example.com

    set CERTBOUND_ALLOW_JOB_ENQUEUE=1
    python -m workers.run_resource_ingestion \\
        --resource-id <official-resources-uuid> \\
        --input-file path/to/exam-guide.txt \\
        --created-by you@example.com \\
        --enqueue
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from utils.access_control import SupabaseAdminConfigError, create_supabase_admin_client
from workers.resource_chunking import (
    DEFAULT_TARGET_WORDS_PER_CHUNK,
    chunk_resource_content,
)

_LIVE_FLAG = "CERTBOUND_ALLOW_JOB_ENQUEUE"
_ENQUEUE_RPC = "enqueue_background_job_v1"
_JOB_TYPE = "resource_ingestion"
_PREVIEW_CHARS = 120


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def assert_enqueue_allowed() -> None:
    if running_under_pytest():
        raise RuntimeError("Refusing to enqueue resource ingestion under pytest.")
    if os.environ.get(_LIVE_FLAG) != "1":
        raise RuntimeError(
            f"Refusing live job enqueue. Set {_LIVE_FLAG}=1 to enqueue a job."
        )


def validate_resource_id(value: str) -> str:
    """Return canonical UUID string or raise ValueError."""
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError("resource_id must not be empty")
    try:
        return str(uuid.UUID(cleaned))
    except ValueError as exc:
        raise ValueError(f"invalid resource_id UUID: {cleaned!r}") from exc


def load_input_file(path: Path) -> str:
    """Load UTF-8 text from a local file."""
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")
    if not path.is_file():
        raise ValueError(f"input path is not a file: {path}")
    return path.read_text(encoding="utf-8")


def fetch_official_resource(client, resource_id: str) -> dict:
    """Load catalog metadata for an official_resources row."""
    result = (
        client.table("official_resources")
        .select("id,title,certification_exam_name,resource_type")
        .eq("id", resource_id)
        .execute()
    )
    if getattr(result, "error", None):
        raise RuntimeError(
            f"official_resources lookup failed: {result.error}"
        )
    rows = result.data or []
    if not rows:
        raise ValueError(f"official_resource not found: {resource_id}")
    return rows[0]


def build_ingest_payload(
    *,
    resource_id: str,
    created_by: str,
    chunked,
    input_file: Path,
    target_words_per_chunk: int,
    source_url: Optional[str] = None,
    source_external_version: Optional[str] = None,
    effective_at: Optional[str] = None,
) -> dict:
    """Build the resource_ingestion handler payload."""
    actor = created_by.strip()
    if not actor:
        raise ValueError("created_by must not be empty")

    payload: dict = {
        "resource_id": resource_id,
        "content_text": chunked.content_text,
        "content_hash": chunked.content_hash,
        "created_by": actor,
        "chunks": chunked.chunks,
        "metadata": {
            "input_file": input_file.name,
            "target_words_per_chunk": target_words_per_chunk,
            "chunking_version": "1",
        },
    }
    if source_url:
        payload["source_url"] = source_url.strip()
    if source_external_version:
        payload["source_external_version"] = source_external_version.strip()
    if effective_at:
        payload["effective_at"] = effective_at.strip()
    return payload


def validate_ingest_payload(payload: dict) -> None:
    """Validate payload matches make_resource_ingestion_handler contract."""
    required = ("resource_id", "content_text", "content_hash", "created_by")
    for field in required:
        value = payload.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"payload field {field!r} must not be empty")

    chunks = payload.get("chunks")
    if chunks is None:
        raise ValueError("payload field 'chunks' must be present")
    if not isinstance(chunks, list):
        raise ValueError("payload field 'chunks' must be a list")

    for idx, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise ValueError(f"chunk {idx} must be an object")
        for field in ("chunk_index", "chunk_text", "content_hash"):
            value = chunk.get(field)
            if value is None or (isinstance(value, str) and not str(value).strip()):
                raise ValueError(f"chunk {idx} field {field!r} must not be empty")


def _chunk_preview(chunk: dict) -> str:
    text = str(chunk.get("chunk_text") or "")
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[: _PREVIEW_CHARS - 3] + "..."


def format_dry_run_report(
    *,
    resource: dict,
    chunked,
    input_file: Path,
    target_words_per_chunk: int,
) -> str:
    """Format the dry-run operator report."""
    first_preview = _chunk_preview(chunked.chunks[0]) if chunked.chunks else ""
    last_preview = _chunk_preview(chunked.chunks[-1]) if chunked.chunks else ""
    lines = [
        "mode: dry-run",
        f"resource_id: {resource['id']}",
        f"title: {resource.get('title')}",
        f"certification_exam_name: {resource.get('certification_exam_name')}",
        f"resource_type: {resource.get('resource_type')}",
        f"input_file: {input_file}",
        f"target_words_per_chunk: {target_words_per_chunk}",
        f"content_hash: {chunked.content_hash}",
        f"content_length: {len(chunked.content_text)}",
        f"chunk_count: {len(chunked.chunks)}",
        f"first_chunk_preview: {first_preview}",
        f"last_chunk_preview: {last_preview}",
    ]
    return "\n".join(lines)


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


def enqueue_resource_ingestion_job(client, params: dict) -> dict:
    result = client.rpc(_ENQUEUE_RPC, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {_ENQUEUE_RPC!r} returned error: {result.error}")
    rows = result.data or []
    if not rows:
        raise RuntimeError(f"RPC {_ENQUEUE_RPC!r} returned no rows")
    return rows[0]


def format_enqueue_report(
    row: dict,
    *,
    resource: dict,
    chunked,
    input_file: Path,
    target_words_per_chunk: int,
) -> str:
    dry_run = format_dry_run_report(
        resource=resource,
        chunked=chunked,
        input_file=input_file,
        target_words_per_chunk=target_words_per_chunk,
    )
    return "\n".join([
        dry_run.replace("mode: dry-run", "mode: enqueue"),
        f"job_id: {row.get('job_id')}",
        f"job_status: {row.get('job_status')}",
        f"job_type: {_JOB_TYPE}",
    ])


def run_dry_run(
    client,
    *,
    resource_id: str,
    input_file: Path,
    created_by: str,
    target_words_per_chunk: int,
    source_url: Optional[str] = None,
    source_external_version: Optional[str] = None,
    effective_at: Optional[str] = None,
) -> str:
    resource = fetch_official_resource(client, resource_id)
    raw_text = load_input_file(input_file)
    chunked = chunk_resource_content(
        raw_text,
        target_words_per_chunk=target_words_per_chunk,
    )
    payload = build_ingest_payload(
        resource_id=resource_id,
        created_by=created_by,
        chunked=chunked,
        input_file=input_file,
        target_words_per_chunk=target_words_per_chunk,
        source_url=source_url,
        source_external_version=source_external_version,
        effective_at=effective_at,
    )
    validate_ingest_payload(payload)
    return format_dry_run_report(
        resource=resource,
        chunked=chunked,
        input_file=input_file,
        target_words_per_chunk=target_words_per_chunk,
    )


def run_enqueue(
    client,
    *,
    resource_id: str,
    input_file: Path,
    created_by: str,
    target_words_per_chunk: int,
    source_url: Optional[str] = None,
    source_external_version: Optional[str] = None,
    effective_at: Optional[str] = None,
    priority: int = 100,
    max_attempts: int = 3,
) -> str:
    resource = fetch_official_resource(client, resource_id)
    raw_text = load_input_file(input_file)
    chunked = chunk_resource_content(
        raw_text,
        target_words_per_chunk=target_words_per_chunk,
    )
    payload = build_ingest_payload(
        resource_id=resource_id,
        created_by=created_by,
        chunked=chunked,
        input_file=input_file,
        target_words_per_chunk=target_words_per_chunk,
        source_url=source_url,
        source_external_version=source_external_version,
        effective_at=effective_at,
    )
    validate_ingest_payload(payload)
    params = build_enqueue_params(
        payload,
        priority=priority,
        max_attempts=max_attempts,
        created_by=created_by,
    )
    row = enqueue_resource_ingestion_job(client, params)
    return format_enqueue_report(
        row,
        resource=resource,
        chunked=chunked,
        input_file=input_file,
        target_words_per_chunk=target_words_per_chunk,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan or enqueue one resource_ingestion background job.",
    )
    parser.add_argument(
        "--resource-id",
        required=True,
        help="UUID of an existing official_resources catalog row",
    )
    parser.add_argument(
        "--input-file",
        required=True,
        help="Local UTF-8 plain-text or Markdown file to ingest",
    )
    parser.add_argument(
        "--created-by",
        required=True,
        help="Actor recorded on the resource version and background job",
    )
    parser.add_argument("--source-url")
    parser.add_argument("--source-external-version")
    parser.add_argument("--effective-at")
    parser.add_argument("--priority", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--target-words-per-chunk",
        type=int,
        default=DEFAULT_TARGET_WORDS_PER_CHUNK,
        help=f"Target chunk size in words (default: {DEFAULT_TARGET_WORDS_PER_CHUNK})",
    )
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="Enqueue a background job (default is dry-run)",
    )
    args = parser.parse_args(argv)

    try:
        resource_id = validate_resource_id(args.resource_id)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    input_path = Path(args.input_file)
    try:
        load_input_file(input_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.target_words_per_chunk <= 0:
        print("ERROR: --target-words-per-chunk must be > 0", file=sys.stderr)
        return 1

    try:
        client = create_supabase_admin_client()
    except SupabaseAdminConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    common_kwargs = {
        "resource_id": resource_id,
        "input_file": input_path,
        "created_by": args.created_by,
        "target_words_per_chunk": args.target_words_per_chunk,
        "source_url": args.source_url,
        "source_external_version": args.source_external_version,
        "effective_at": args.effective_at,
    }

    if not args.enqueue:
        try:
            print(run_dry_run(client, **common_kwargs))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        assert_enqueue_allowed()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        print(
            run_enqueue(
                client,
                **common_kwargs,
                priority=args.priority,
                max_attempts=args.max_attempts,
            )
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
