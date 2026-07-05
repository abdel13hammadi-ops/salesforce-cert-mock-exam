#!/usr/bin/env python3
"""
Validate a completed (or partially completed) SME review CSV against the
quality benchmark pilot fixture, and — only when the review is fully
complete and free of validation errors — produce a separate proposed
reviewed-fixture JSON file (V58-QUALITY-03D).

This script never modifies the source AI-drafted fixture
(workers/fixtures/quality_benchmark_v1.json). It performs no live AI calls
and no database writes. It does not invent, guess, or auto-fill SME
decisions; it only reads whatever a human reviewer actually entered.

Usage (validate only, always safe to run)::

    python scripts/v58_import_benchmark_sme_review.py --review review_packet.csv

Usage (validate and, if fully adjudicated with no rejected cases, write the
proposed reviewed fixture)::

    python scripts/v58_import_benchmark_sme_review.py \\
        --review review_packet.csv \\
        --reviewer-id sme-jdoe \\
        --output workers/fixtures/quality_benchmark_v1_sme_reviewed.json

A reviewed fixture is only ever produced when the review is valid, every
case has a non-blank decision, no case is stuck needing a second review, no
case is marked reject_case, and an explicit --reviewer-id is supplied. The
finalized fixture records sme_reviewer_id, source_fixture_sha256, and a UTC
review_imported_at_utc timestamp generated at write time.
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
    DEFAULT_REVIEWED_OUTPUT_PATH,
    DEFAULT_SOURCE_FIXTURE_PATH,
    BenchmarkSmeReviewError,
    build_reviewed_fixture,
    load_source_fixture,
    read_sme_review_csv,
    validate_sme_review_rows,
    validation_report_dict,
    write_reviewed_fixture,
)


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an SME review CSV and optionally produce a proposed reviewed fixture",
    )
    parser.add_argument(
        "--fixture",
        default=str(DEFAULT_SOURCE_FIXTURE_PATH),
        help="Path to the AI-drafted benchmark fixture (read-only)",
    )
    parser.add_argument(
        "--review",
        required=True,
        help="Path to the completed (or partially completed) SME review CSV",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "If the review is complete and valid, write the proposed reviewed "
            f"fixture here (default when omitted but review is complete: "
            f"{DEFAULT_REVIEWED_OUTPUT_PATH})"
        ),
    )
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="Allow overwriting an existing reviewed-fixture file at --output",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Only print the validation report; never write a reviewed fixture",
    )
    parser.add_argument(
        "--reviewer-id",
        default=None,
        help=(
            "Internal identifier for the qualified SME reviewer (not a personal name or "
            "email required). Required to produce a finalized reviewed fixture."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if _running_under_pytest():
        print("Refusing to run SME review importer under pytest.")
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        fixture = load_source_fixture(args.fixture)
        rows = read_sme_review_csv(args.review)
        report = validate_sme_review_rows(rows, fixture, source_fixture_path=args.fixture)
    except BenchmarkSmeReviewError as exc:
        print(f"Validation failed: {exc}")
        return 1

    print(json.dumps(validation_report_dict(report), indent=2, sort_keys=True))

    if not report.is_valid:
        print(f"REJECTED: {len(report.errors)} validation error(s) found. No fixture written.")
        return 1

    if args.report_only:
        return 0

    pending = len(report.missing_case_ids) + len(report.missing_decision_case_ids)
    unresolved = len(report.unresolved_second_review_case_ids)
    rejected = len(report.rejected_case_ids)
    blocking_reasons = []
    if pending:
        blocking_reasons.append(f"{pending} case(s) still need an SME decision")
    if unresolved:
        blocking_reasons.append(f"{unresolved} case(s) still need a second review")
    if rejected:
        blocking_reasons.append(
            f"{rejected} case(s) marked reject_case (must be corrected or replaced — "
            "not silently dropped — before the benchmark can be finalized as trusted "
            "ground truth)"
        )
    if blocking_reasons:
        print(
            "NOT FINALIZABLE: " + "; ".join(blocking_reasons) + ". "
            "The review may still be in progress or partially adjudicated; no reviewed "
            "fixture written."
        )
        return 0

    if not args.reviewer_id or not args.reviewer_id.strip():
        print(
            "Refusing to finalize: --reviewer-id is required to produce a reviewed fixture "
            "(an internal identifier is sufficient; no personal name or email required)."
        )
        return 1

    output_path = args.output or str(DEFAULT_REVIEWED_OUTPUT_PATH)
    try:
        reviewed = build_reviewed_fixture(
            fixture, rows, report, reviewer_id=args.reviewer_id
        )
        write_reviewed_fixture(
            output_path,
            reviewed,
            source_fixture_path=args.fixture,
            allow_overwrite=args.allow_overwrite,
        )
    except BenchmarkSmeReviewError as exc:
        print(f"Could not write reviewed fixture: {exc}")
        return 1

    print(f"COMPLETE: reviewed fixture written to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
