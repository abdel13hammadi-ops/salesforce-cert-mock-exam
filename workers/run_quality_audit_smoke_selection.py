#!/usr/bin/env python3
"""
Read-only smoke-question selector for the CertBound AI quality-audit pilot.

Loads current immutable question versions for ADM and BA, applies deterministic
domain-weighted selection, and prints a redacted preview. No writes, enqueues,
model calls, or external network access beyond Supabase reads.

Usage::

    python -m workers.run_quality_audit_smoke_selection --seed 42
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from utils.access_control import SupabaseAdminConfigError, create_supabase_admin_client
from workers.quality_audit_pilot import (
    DEFAULT_SMOKE_SEED,
    QualityAuditPilotError,
    format_quality_audit_smoke_selection,
    select_quality_audit_smoke_questions,
)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic read-only smoke-question selection for the quality-audit pilot.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SMOKE_SEED,
        help=f"Deterministic selection seed (default: {DEFAULT_SMOKE_SEED})",
    )
    args = parser.parse_args(argv)

    try:
        client = create_supabase_admin_client()
    except SupabaseAdminConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        selection = select_quality_audit_smoke_questions(client, seed=args.seed)
    except QualityAuditPilotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_quality_audit_smoke_selection(selection))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
