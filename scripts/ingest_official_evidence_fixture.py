#!/usr/bin/env python3
"""
Ingest a verified official-evidence fixture package into runtime tables.

Requires an explicit PostgreSQL DSN and never uses production Supabase
credentials automatically. Live writes additionally require
``CERTBOUND_ALLOW_FIXTURE_INGEST=1``.

Hosted Supabase targets (``supabase.co`` / ``supabase.in``) are rejected by
default. Two approved hosted-target overrides exist (PAB-EXP-04E, BA-EXP-03),
and each stays fail-closed unless *every* condition below holds:

    1. ``CERTBOUND_ALLOW_FIXTURE_INGEST=1``
    2. ``CERTBOUND_ALLOW_APPROVED_SUPABASE_INGEST=1``
    3. ``--allow-approved-supabase-target`` passed explicitly
    4. an explicit ``--database-url``
    5. fixture identity exactly ``official-evidence-pab-v1``,
       ``official-evidence-ba-v1``, or ``official-evidence-scc-v1``
    6. certification scope matching the fixture (PAB, Business Analyst, or
       Sales Cloud Consultant)
    7. exact fixture record count for that package (7 for PAB, 6 for BA, 5 for SCC)

This script never prints or logs the database URL, credentials, or tokens.

Usage::

    python scripts/ingest_official_evidence_fixture.py \\
        --fixture workers/fixtures/official_evidence_pab_v1.json \\
        --database-url postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test \\
        --dry-run

    set CERTBOUND_ALLOW_FIXTURE_INGEST=1
    python scripts/ingest_official_evidence_fixture.py \\
        --fixture workers/fixtures/official_evidence_pab_v1.json \\
        --database-url postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test

    REM Approved hosted Supabase target only (all flags required together):
    set CERTBOUND_ALLOW_FIXTURE_INGEST=1
    set CERTBOUND_ALLOW_APPROVED_SUPABASE_INGEST=1
    python scripts/ingest_official_evidence_fixture.py \\
        --fixture workers/fixtures/official_evidence_pab_v1.json \\
        --database-url <operator-supplied-approved-dsn> \\
        --allow-approved-supabase-target
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workers.official_evidence_fixture_ingestion import (  # noqa: E402
    DEFAULT_CREATED_BY,
    OfficialEvidenceFixtureIngestionConflictError,
    OfficialEvidenceFixtureIngestionError,
    OfficialEvidenceFixtureIngestionSafetyError,
    format_package_summary,
    ingest_fixture_file,
    load_fixture_for_ingestion,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a verified official-evidence fixture package into "
            "disposable runtime evidence tables."
        ),
    )
    parser.add_argument(
        "--fixture",
        required=True,
        help="Path to an official evidence fixture JSON package",
    )
    parser.add_argument(
        "--database-url",
        help=(
            "Disposable PostgreSQL DSN (required for non-dry-run ingestion). "
            "Must pass disposable-host validation."
        ),
    )
    parser.add_argument(
        "--created-by",
        default=DEFAULT_CREATED_BY,
        help="Actor recorded on catalog rows and resource versions",
    )
    parser.add_argument(
        "--expected-fixture-version",
        help="Optional fixture_version guard (e.g. official-evidence-pab-v1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate fixture and plan ingestion without writing",
    )
    parser.add_argument(
        "--allow-approved-supabase-target",
        action="store_true",
        help=(
            "Required (together with CERTBOUND_ALLOW_FIXTURE_INGEST=1 and "
            "CERTBOUND_ALLOW_APPROVED_SUPABASE_INGEST=1) to target a hosted "
            "Supabase database. Ignored for non-hosted (disposable/local) "
            "targets. Fails closed if any other required condition is missing."
        ),
    )
    parser.add_argument(
        "--json-summary",
        action="store_true",
        help="Print structured JSON summary instead of plain text",
    )
    args = parser.parse_args(argv)

    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(f"ERROR: fixture not found: {fixture_path}", file=sys.stderr)
        return 1

    try:
        if args.dry_run and not args.database_url:
            # Fixture-only dry run: no connection attempted, existing-row
            # disposition cannot be reported (unchanged pre-04E behavior).
            payload, fixture_version, package_config = load_fixture_for_ingestion(
                fixture_path,
                expected_fixture_version=args.expected_fixture_version,
            )
            summary = {
                "mode": "dry-run (fixture-only, no --database-url)",
                "fixture_path": str(fixture_path),
                "fixture_version": fixture_version,
                "evidence_config_id": package_config["evidence_config_id"],
                "certification_exam_name": package_config["certification_exam_name"],
                "item_count": package_config["expected_record_count"],
                "domains_expected": package_config["expected_domain_count"],
            }
            if args.json_summary:
                print(json.dumps(summary, indent=2, sort_keys=True))
            else:
                print("\n".join(f"{key}: {value}" for key, value in summary.items()))
            return 0

        if not args.database_url:
            print(
                "ERROR: --database-url is required for non-dry-run ingestion",
                file=sys.stderr,
            )
            return 1

        # Connected dry run (--dry-run + --database-url) or live ingestion.
        # ingest_fixture_file() enforces all safety gating -- including the
        # approved-hosted-target checks -- before opening any connection.
        result = ingest_fixture_file(
            fixture_path,
            database_url=args.database_url,
            created_by=args.created_by,
            dry_run=args.dry_run,
            expected_fixture_version=args.expected_fixture_version,
            allow_hosted_cli_flag=args.allow_approved_supabase_target,
        )
        if args.json_summary:
            print(json.dumps(result.to_summary_dict(), indent=2, sort_keys=True))
        else:
            print(format_package_summary(result))
        return 0
    except OfficialEvidenceFixtureIngestionConflictError as exc:
        print(f"CONFLICT: {exc}", file=sys.stderr)
        return 2
    except OfficialEvidenceFixtureIngestionSafetyError as exc:
        print(f"SAFETY: {exc}", file=sys.stderr)
        return 3
    except OfficialEvidenceFixtureIngestionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
