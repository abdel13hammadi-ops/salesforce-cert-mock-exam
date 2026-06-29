#!/usr/bin/env python3
"""
Plan or enqueue structural full-bank audits for CertBound.

Default mode is dry-run. Pass ``--enqueue`` to create background jobs after
reviewing the printed plan. Requires ``CERTBOUND_ALLOW_JOB_ENQUEUE=1``.

Examples::

    # Dry-run ADM only (default; uses env vars or .streamlit/secrets.toml)
    python -m workers.run_structural_full_bank_audit --certification adm

    # Dry-run both certifications with JSON summary
    python -m workers.run_structural_full_bank_audit --certification both --json-summary

    # Pilot: first 25 questions only
    python -m workers.run_structural_full_bank_audit --certification adm --max-questions 25

    # Production enqueue (do not run without review)
    set CERTBOUND_ALLOW_JOB_ENQUEUE=1
    python -m workers.run_structural_full_bank_audit --certification both --enqueue --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from utils.access_control import SupabaseAdminConfigError, create_supabase_admin_client
from workers.structural_audit_launcher import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CREATED_BY,
    DEFAULT_RULESET_VERSION,
    MalformedSelectionError,
    StructuralAuditEnqueueError,
    StructuralAuditLauncherError,
    UnknownCertificationError,
    build_structural_audit_plan,
    execute_structural_audit_plan,
    format_human_report,
    load_resume_state,
    summarize_plan,
)

_LIVE_FLAG = "CERTBOUND_ALLOW_JOB_ENQUEUE"


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def assert_enqueue_allowed() -> None:
    if running_under_pytest():
        raise RuntimeError("Refusing to enqueue structural audit jobs under pytest.")
    if os.environ.get(_LIVE_FLAG) != "1":
        raise RuntimeError(
            f"Refusing live job enqueue. Set {_LIVE_FLAG}=1 to enqueue jobs."
        )


def load_background_jobs_for_planning(client) -> list:
    result = (
        client.table("background_jobs")
        .select("job_type, job_status, payload")
        .in_("job_type", ["deterministic_audit", "certification_duplicate_audit"])
        .execute()
    )
    return result.data or []


def confirm_enqueue(plan_report: str) -> None:
    print(plan_report)
    print()
    answer = input("Type ENQUEUE to create background jobs: ").strip()
    if answer != "ENQUEUE":
        raise SystemExit("Enqueue cancelled.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or enqueue structural full-bank deterministic audits.",
    )
    parser.add_argument(
        "--certification",
        required=True,
        help="adm, ba, both, or a supported exam_name",
    )
    parser.add_argument("--created-by", default=DEFAULT_CREATED_BY)
    parser.add_argument("--ruleset-version", default=DEFAULT_RULESET_VERSION)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="Optional pilot cap on deterministic jobs per run",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Optional JSON file for resume bookkeeping",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip version IDs already recorded in --state-file",
    )
    parser.add_argument(
        "--json-summary",
        action="store_true",
        help="Print machine-readable JSON summary to stdout",
    )
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="Create background jobs (default is dry-run)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation when using --enqueue",
    )
    return parser


def run_launcher(
    client,
    *,
    certification: str,
    created_by: str,
    ruleset_version: str,
    batch_size: int,
    max_questions: Optional[int],
    state_file: Optional[str],
    resume: bool,
    enqueue: bool,
    confirm: bool = True,
) -> dict:
    resume_state = load_resume_state(state_file, resume=resume)
    if resume and state_file and not os.path.exists(state_file):
        print(
            f"WARNING: resume state file not found at {state_file!r}; "
            "treating as empty resume state.",
            file=sys.stderr,
        )

    active_jobs = load_background_jobs_for_planning(client)
    plan = build_structural_audit_plan(
        client,
        certification_scope=certification,
        ruleset_version=ruleset_version,
        created_by=created_by,
        batch_size=batch_size,
        max_questions=max_questions,
        background_jobs=active_jobs,
        resume_state=resume_state,
    )
    report = format_human_report(
        plan,
        summarize_plan(plan, dry_run=not enqueue),
    )

    if enqueue:
        if confirm:
            confirm_enqueue(report)
        else:
            print(report)
        summary = execute_structural_audit_plan(
            client,
            plan,
            dry_run=False,
            state_file=state_file,
        )
    else:
        print(report)
        summary = execute_structural_audit_plan(client, plan, dry_run=True)

    return summary.to_dict()


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.enqueue:
        assert_enqueue_allowed()

    try:
        client = create_supabase_admin_client()
    except SupabaseAdminConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        summary = run_launcher(
            client,
            certification=args.certification,
            created_by=args.created_by,
            ruleset_version=args.ruleset_version,
            batch_size=args.batch_size,
            max_questions=args.max_questions,
            state_file=args.state_file,
            resume=args.resume,
            enqueue=args.enqueue,
            confirm=not args.yes,
        )
    except UnknownCertificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except MalformedSelectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except StructuralAuditLauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    except StructuralAuditEnqueueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Failed target: {exc.failed_target}", file=sys.stderr)
        partial = exc.summary.to_dict()
        print(
            "Partial enqueue summary:\n"
            f"  enqueued deterministic jobs this run: "
            f"{partial['enqueued_deterministic_jobs']}\n"
            f"  enqueued duplicate scans this run: "
            f"{partial['enqueued_duplicate_scans']}\n"
            f"  already queued/running skipped: "
            f"{partial['already_queued_running_skipped']}\n"
            f"  resume skipped: {partial['resume_skipped']}\n"
            f"  completed audit skipped: {partial['completed_audit_skipped']}",
            file=sys.stderr,
        )
        if args.json_summary:
            print(json.dumps(partial, indent=2, sort_keys=True))
        return 5

    if args.json_summary:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
