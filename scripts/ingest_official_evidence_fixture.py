#!/usr/bin/env python3
"""
Ingest a verified official-evidence fixture package into disposable runtime tables.

Requires an explicit disposable PostgreSQL DSN and never uses production
Supabase credentials. Live writes additionally require
``CERTBOUND_ALLOW_FIXTURE_INGEST=1``.

Usage::

    python scripts/ingest_official_evidence_fixture.py \\
        --fixture workers/fixtures/official_evidence_pab_v1.json \\
        --database-url postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test \\
        --dry-run

    set CERTBOUND_ALLOW_FIXTURE_INGEST=1
    python scripts/ingest_official_evidence_fixture.py \\
        --fixture workers/fixtures/official_evidence_pab_v1.json \\
        --database-url postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test
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
    assert_fixture_ingest_allowed,
    format_package_summary,
    ingest_fixture_file,
    load_fixture_for_ingestion,
    reject_production_like_dsn,
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
        if args.dry_run:
            payload, fixture_version, package_config = load_fixture_for_ingestion(
                fixture_path,
                expected_fixture_version=args.expected_fixture_version,
            )
            summary = {
                "mode": "dry-run",
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

        assert_fixture_ingest_allowed(
            database_url=args.database_url, dry_run=False
        )
        reject_production_like_dsn(args.database_url)

        result = ingest_fixture_file(
            fixture_path,
            database_url=args.database_url,
            created_by=args.created_by,
            dry_run=False,
            expected_fixture_version=args.expected_fixture_version,
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
