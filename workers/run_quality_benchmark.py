#!/usr/bin/env python3
"""
Dual-engine quality benchmark runner (mock mode by default).

Evaluates versioned benchmark fixtures against the legacy or V48 audit path
using mock engine outputs. Live provider execution remains disabled unless
explicitly authorized.

Usage (mock mode, no live flag required)::

    python -m workers.run_quality_benchmark --engine legacy
    python -m workers.run_quality_benchmark --engine v48
    python -m workers.run_quality_benchmark --engine both

Live mode (not implemented in V58-QUALITY-03A; gate only)::

    set CERTBOUND_ALLOW_LIVE_AI_TEST=1
    python -m workers.run_quality_benchmark --engine legacy --live
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from workers.quality_benchmark import (
    DEFAULT_FIXTURE_PATH,
    ENGINE_LEGACY,
    ENGINE_V48,
    SUPPORTED_ENGINES,
    BenchmarkFixtureError,
    dumps_run_report,
    load_benchmark_fixture,
    run_quality_benchmark,
)

_LIVE_FLAG = "CERTBOUND_ALLOW_LIVE_AI_TEST"
_FIXTURE_ENV = "CERTBOUND_QUALITY_BENCHMARK_FIXTURE"


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _parse_engines(raw: str) -> list[str]:
    if raw == "both":
        return [ENGINE_LEGACY, ENGINE_V48]
    if raw not in SUPPORTED_ENGINES:
        raise ValueError(
            f"Unsupported engine {raw!r}; expected one of {sorted(SUPPORTED_ENGINES)} or 'both'"
        )
    return [raw]


def main(argv: list[str] | None = None) -> int:
    if _running_under_pytest():
        print("Refusing to run quality benchmark CLI under pytest.")
        return 2

    parser = argparse.ArgumentParser(
        description="CertBound dual-engine quality benchmark harness",
    )
    parser.add_argument(
        "--engine",
        choices=[ENGINE_LEGACY, ENGINE_V48, "both"],
        default=ENGINE_LEGACY,
        help="Audit engine to evaluate (legacy, v48, or both)",
    )
    parser.add_argument(
        "--fixture",
        default=os.environ.get(_FIXTURE_ENV, str(DEFAULT_FIXTURE_PATH)),
        help="Path to benchmark JSON fixture",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Request live provider execution (requires explicit authorization)",
    )
    args = parser.parse_args(argv)

    if args.live:
        if os.environ.get(_LIVE_FLAG) != "1":
            print(
                f"Refusing live benchmark execution. Set {_LIVE_FLAG}=1 to authorize live mode."
            )
            return 1
        print(
            "Live benchmark execution is not implemented in V58-QUALITY-03A. "
            "Use mock mode (default) until a later task wires live adapters."
        )
        return 1

    try:
        fixture = load_benchmark_fixture(Path(args.fixture))
    except (OSError, BenchmarkFixtureError, ValueError, TypeError) as exc:
        print(f"Invalid benchmark fixture: {exc}")
        return 1

    exit_code = 0
    for engine in _parse_engines(args.engine):
        report = run_quality_benchmark(fixture, engine)
        print(f"engine: {engine}")
        print(dumps_run_report(report))
        if report.metrics.false_approvals or report.metrics.false_rejections:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
