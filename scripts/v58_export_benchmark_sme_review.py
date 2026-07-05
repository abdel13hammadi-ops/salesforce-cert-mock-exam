#!/usr/bin/env python3
"""
Export a reviewer-friendly CSV packet for qualified Salesforce SME review of
the 40-case AI-drafted quality benchmark pilot (V58-QUALITY-03D).

Read-only with respect to the source fixture: this script never modifies
workers/fixtures/quality_benchmark_v1.json. It only reads it and writes a new
CSV file. All SME-editable columns are left blank in the export.

Usage::

    python scripts/v58_export_benchmark_sme_review.py --output review_packet.csv
    python scripts/v58_export_benchmark_sme_review.py \\
        --fixture workers/fixtures/quality_benchmark_v1.json \\
        --output review_packet.csv --allow-overwrite
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

from workers.benchmark_sme_review import (  # noqa: E402
    DEFAULT_SOURCE_FIXTURE_PATH,
    BenchmarkSmeReviewError,
    export_sme_review_csv,
)


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a reviewer-friendly SME review CSV from the quality benchmark fixture",
    )
    parser.add_argument(
        "--fixture",
        default=str(DEFAULT_SOURCE_FIXTURE_PATH),
        help="Path to the AI-drafted benchmark fixture (read-only)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the reviewer CSV",
    )
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="Allow overwriting an existing CSV at --output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if _running_under_pytest():
        print("Refusing to run SME review exporter under pytest.")
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        summary = export_sme_review_csv(
            args.fixture,
            args.output,
            allow_overwrite=args.allow_overwrite,
        )
    except BenchmarkSmeReviewError as exc:
        print(f"Export failed: {exc}")
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
