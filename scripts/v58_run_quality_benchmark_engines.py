#!/usr/bin/env python3
"""
V58-QUALITY-04A — dual-engine benchmark execution CLI.

Three explicit, non-overlapping modes:

  generate  — run an engine adapter against a benchmark fixture's case
              content and write a prediction artifact. Does not score.
  validate  — check that a prediction artifact has exactly one prediction
              per case in a fixture (no missing/duplicate/unknown ids).
              Does not score.
  score     — load a *finalized* SME-reviewed fixture and a compatible
              prediction artifact, verify coverage, and write a
              deterministic scorecard.

Safety posture
--------------
* Default mode is always safe and non-live: no network calls, no database
  writes, no candidate publishing.
* ``generate --engine v48`` always reports BLOCKED — see
  ``workers.quality_benchmark_execution.V48EngineAdapter`` for the exact
  architectural reason and the smallest safe follow-up.
* ``generate --engine legacy --live`` requires
  ``CERTBOUND_ALLOW_LIVE_AI_TEST=1`` to even be considered, and even then
  this task does not wire a real provider — it prints a clear refusal,
  mirroring the existing ``workers/run_quality_benchmark.py`` precedent.
* ``score`` refuses any fixture that is not genuine, finalized SME ground
  truth (see ``assert_finalized_sme_ground_truth``); AI-drafted fixtures
  such as ``workers/fixtures/quality_benchmark_v1.json`` are always
  rejected for scoring.

Usage
-----
    python -m scripts.v58_run_quality_benchmark_engines generate \\
        --engine legacy --fixture workers/fixtures/quality_benchmark_v1.json \\
        --output /tmp/legacy_predictions.json

    python -m scripts.v58_run_quality_benchmark_engines generate \\
        --engine v48 --fixture workers/fixtures/quality_benchmark_v1.json \\
        --output /tmp/v48_predictions.json
    # -> exits with BLOCKED status; no artifact written

    python -m scripts.v58_run_quality_benchmark_engines validate \\
        --fixture workers/fixtures/quality_benchmark_v1.json \\
        --predictions /tmp/legacy_predictions.json

    python -m scripts.v58_run_quality_benchmark_engines score \\
        --fixture workers/fixtures/quality_benchmark_v1_sme_reviewed.json \\
        --predictions /tmp/legacy_predictions.json \\
        --output /tmp/legacy_scorecard.json
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.quality_benchmark_execution import (  # noqa: E402
    ENGINE_LEGACY,
    ENGINE_V48,
    EngineAdapterUnavailableError,
    GroundTruthNotFinalizedError,
    LegacyEngineAdapter,
    PredictionArtifactError,
    QualityBenchmarkExecutionError,
    V48EngineAdapter,
    dumps_scorecard,
    generate_predictions,
    load_benchmark_case_fixture,
    load_finalized_sme_ground_truth_fixture,
    load_prediction_artifact,
    score_predictions,
    validate_prediction_coverage,
    write_prediction_artifact,
    write_scorecard,
)
from workers.quality_benchmark import BenchmarkFixtureError  # noqa: E402

_LIVE_FLAG = "CERTBOUND_ALLOW_LIVE_AI_TEST"


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _build_adapter(engine: str, *, live: bool):
    if engine == ENGINE_LEGACY:
        return LegacyEngineAdapter()
    if engine == ENGINE_V48:
        return V48EngineAdapter()
    raise ValueError(f"unsupported engine {engine!r}")


def _cmd_generate(args: argparse.Namespace) -> int:
    if args.live:
        if os.environ.get(_LIVE_FLAG) != "1":
            print(f"Refusing live prediction generation. Set {_LIVE_FLAG}=1 to authorize live mode.")
            return 1
        print(
            "Live provider execution is not implemented in V58-QUALITY-04A. "
            "Use mock/non-live mode until a later task wires a real provider."
        )
        return 1

    try:
        fixture = load_benchmark_case_fixture(args.fixture)
    except (OSError, BenchmarkFixtureError, ValueError, TypeError) as exc:
        print(f"Invalid benchmark fixture: {exc}")
        return 1

    adapter = _build_adapter(args.engine, live=args.live)

    try:
        artifact = generate_predictions(fixture, adapter, source_fixture_path=args.fixture)
    except EngineAdapterUnavailableError as exc:
        print(f"BLOCKED: engine {args.engine!r} cannot generate real predictions.")
        print(f"Reason: {exc.reason}")
        print(f"Follow-up: {exc.follow_up}")
        return 3

    write_prediction_artifact(args.output, artifact, allow_overwrite=args.allow_overwrite)
    print(f"engine: {args.engine}")
    print(f"case_count: {artifact['case_count']}")
    print(f"error_case_count: {artifact['error_case_count']}")
    print(f"predictions written to: {args.output}")
    return 1 if artifact["error_case_count"] else 0


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        fixture = load_benchmark_case_fixture(args.fixture)
        artifact = load_prediction_artifact(args.predictions)
        coverage = validate_prediction_coverage(fixture, artifact)
    except (OSError, BenchmarkFixtureError, PredictionArtifactError, ValueError, TypeError) as exc:
        print(f"Invalid prediction artifact: {exc}")
        return 1

    print(f"expected_case_count: {coverage['expected_case_count']}")
    print(f"predicted_case_count: {coverage['predicted_case_count']}")
    print("coverage: OK (every case has exactly one prediction; no unknown ids)")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    try:
        fixture = load_finalized_sme_ground_truth_fixture(args.fixture)
    except (OSError, BenchmarkFixtureError, GroundTruthNotFinalizedError, ValueError, TypeError) as exc:
        print("Refusing to score: fixture is not finalized SME ground truth.")
        print(f"Reason: {exc}")
        return 1

    try:
        artifact = load_prediction_artifact(args.predictions)
        scorecard = score_predictions(fixture, artifact)
    except (OSError, PredictionArtifactError, QualityBenchmarkExecutionError, ValueError, TypeError) as exc:
        print(f"Invalid prediction artifact: {exc}")
        return 1

    write_scorecard(args.output, scorecard, allow_overwrite=args.allow_overwrite)
    print(dumps_scorecard(scorecard))
    print(f"scorecard written to: {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if _running_under_pytest():
        print("Refusing to run quality benchmark execution CLI under pytest.")
        return 2

    parser = argparse.ArgumentParser(
        description="CertBound dual-engine benchmark execution (prediction generation + scoring)",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    generate_parser = subparsers.add_parser("generate", help="Generate raw engine predictions")
    generate_parser.add_argument("--engine", choices=[ENGINE_LEGACY, ENGINE_V48], required=True)
    generate_parser.add_argument("--fixture", required=True, help="Path to a benchmark JSON fixture")
    generate_parser.add_argument("--output", required=True, help="Path to write the prediction artifact")
    generate_parser.add_argument(
        "--live", action="store_true", help="Request live provider execution (requires explicit authorization)"
    )
    generate_parser.add_argument("--allow-overwrite", action="store_true")
    generate_parser.set_defaults(func=_cmd_generate)

    validate_parser = subparsers.add_parser("validate", help="Validate prediction artifact case coverage")
    validate_parser.add_argument("--fixture", required=True)
    validate_parser.add_argument("--predictions", required=True)
    validate_parser.set_defaults(func=_cmd_validate)

    score_parser = subparsers.add_parser("score", help="Score predictions against finalized SME ground truth")
    score_parser.add_argument("--fixture", required=True, help="Path to a finalized SME-reviewed fixture")
    score_parser.add_argument("--predictions", required=True)
    score_parser.add_argument("--output", required=True, help="Path to write the scorecard artifact")
    score_parser.add_argument("--allow-overwrite", action="store_true")
    score_parser.set_defaults(func=_cmd_score)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
