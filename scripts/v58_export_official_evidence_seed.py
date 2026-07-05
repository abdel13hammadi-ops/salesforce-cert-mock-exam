#!/usr/bin/env python3
"""
Read-only exporter for verified official Salesforce evidence seeds (V58).

Default mode prints an inventory summary. Pass ``--output`` to write a bounded
fixture for the quality benchmark pilot.

Usage::

    python scripts/v58_export_official_evidence_seed.py --inventory
    python scripts/v58_export_official_evidence_seed.py \\
        --output workers/fixtures/official_evidence_seed_v1.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workers.official_evidence_seed import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_TARGET_MAX_CHUNKS,
    DEFAULT_TARGET_MIN_CHUNKS,
    OfficialEvidenceSeedConfigError,
    OfficialEvidenceSeedError,
    OfficialEvidenceSeedOutputError,
    build_fixture_payload,
    export_official_evidence_seed,
    inventory_report_dict,
    load_supabase_client,
    validate_fixture_payload,
    write_fixture_file,
)


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export verified official Salesforce evidence seed (read-only)",
    )
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="Print read-only resource-library inventory and exit",
    )
    parser.add_argument(
        "--output",
        help="Write fixture JSON to this path (required for export mode)",
    )
    parser.add_argument(
        "--target-min",
        type=int,
        default=DEFAULT_TARGET_MIN_CHUNKS,
        help=f"Minimum selected chunks (default: {DEFAULT_TARGET_MIN_CHUNKS})",
    )
    parser.add_argument(
        "--target-max",
        type=int,
        default=DEFAULT_TARGET_MAX_CHUNKS,
        help=f"Maximum selected chunks (default: {DEFAULT_TARGET_MAX_CHUNKS})",
    )
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="Development-only override to replace an existing fixture file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if _running_under_pytest():
        print("Refusing to run evidence seed exporter under pytest.")
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.inventory and not args.output:
        parser.error("Specify --inventory or --output")

    try:
        client = load_supabase_client(repo_root=_REPO_ROOT)
    except OfficialEvidenceSeedConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    try:
        if args.inventory:
            from workers.official_evidence_seed import build_inventory

            inventory = build_inventory(client)
            print(json.dumps(inventory_report_dict(inventory), indent=2, sort_keys=True))
            return 0

        selection = export_official_evidence_seed(
            client,
            target_min=args.target_min,
            target_max=args.target_max,
        )
        payload = build_fixture_payload(
            selection.items,
            inventory=selection.inventory,
        )
        validate_fixture_payload(payload)
        write_fixture_file(
            args.output,
            payload,
            allow_overwrite=args.allow_overwrite,
        )
        print(
            json.dumps(
                {
                    "output_path": str(Path(args.output)),
                    "exported_chunk_count": len(selection.items),
                    "exported_by_certification": payload["export_summary"]["exported_by_certification"],
                    "exported_by_domain": payload["export_summary"]["exported_by_domain"],
                    "maximum_excerpt_chars": payload["export_summary"]["maximum_excerpt_chars"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (OfficialEvidenceSeedError, OfficialEvidenceSeedOutputError) as exc:
        print(f"Export failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
