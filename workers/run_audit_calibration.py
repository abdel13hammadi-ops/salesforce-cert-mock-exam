#!/usr/bin/env python3
"""
Manual five-question audit calibration pilot.

Dry-run only: deterministic checks + Anthropic LLM + merge. No audit RPCs,
publishing, promotion, or question mutation.

Usage::

    set CERTBOUND_ALLOW_LIVE_AI_TEST=1
    set CERTBOUND_LLM_PROVIDER=anthropic
    set CERTBOUND_ANTHROPIC_API_KEY=your-key-here
    python -m workers.run_audit_calibration

Optional::

    set CERTBOUND_CALIBRATION_FIXTURE=path/to/fixture.json
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from workers.audit_calibration import (
    DEFAULT_FIXTURE_PATH,
    format_pilot_summary,
    load_calibration_fixture,
    run_calibration_pilot,
)
from workers.llm_provider_factory import build_llm_provider_from_env

_LIVE_FLAG = "CERTBOUND_ALLOW_LIVE_AI_TEST"
_FIXTURE_ENV = "CERTBOUND_CALIBRATION_FIXTURE"


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def main(argv: list[str] | None = None) -> int:
    if _running_under_pytest():
        print("Refusing to run calibration pilot under pytest.")
        return 2

    if os.environ.get(_LIVE_FLAG) != "1":
        print(
            f"Refusing live calibration. Set {_LIVE_FLAG}=1 to run this pilot."
        )
        return 1

    parser = argparse.ArgumentParser(
        description="CertBound five-question audit calibration pilot (dry-run)",
    )
    parser.add_argument(
        "--fixture",
        default=os.environ.get(_FIXTURE_ENV, str(DEFAULT_FIXTURE_PATH)),
        help="Path to calibration JSON fixture",
    )
    args = parser.parse_args(argv)

    try:
        provider = build_llm_provider_from_env()
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to configure LLM provider: {exc}")
        return 1

    if provider is None:
        print(
            "No LLM provider configured. Set CERTBOUND_LLM_PROVIDER=anthropic "
            "and CERTBOUND_ANTHROPIC_API_KEY before running calibration."
        )
        return 1

    try:
        fixture = load_calibration_fixture(Path(args.fixture))
    except (OSError, ValueError, TypeError) as exc:
        print(f"Invalid calibration fixture: {exc}")
        return 1

    summary = run_calibration_pilot(fixture, provider)
    print(format_pilot_summary(summary))
    return 0 if summary.cases_passed == len(summary.case_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
