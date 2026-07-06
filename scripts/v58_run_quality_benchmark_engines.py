#!/usr/bin/env python3
"""
V58-QUALITY-04A/04C — dual-engine benchmark execution CLI.

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
* ``generate --engine v48`` reports BLOCKED by default — see
  ``workers.quality_benchmark_execution.V48EngineAdapter`` for the exact
  architectural reason and the smallest safe follow-up.
* ``generate --engine v48 --allow-disposable-v48-db --v48-db-url ...``
  (V58-QUALITY-04C) plumbs the explicit opt-in and disposable-database DSN
  through to ``V48EngineAdapter``, but this CLI never constructs a live AI
  provider itself — "no provider injected" still reports a clean BLOCKED
  message (exit 3), never a stack trace. Real V48 disposable-database
  execution with injected fake/test providers is only exercised directly
  through the Python API in
  ``tests/test_quality_benchmark_v48_orchestration.py`` (Docker-gated), by
  design: "no live AI calls unless using explicitly injected test
  providers" and "no provider constructed implicitly from environment
  variables" both forbid this CLI from silently wiring one up.
* ``generate --engine legacy --live`` requires
  ``CERTBOUND_ALLOW_LIVE_AI_TEST=1`` to even be considered, and even then
  this task does not wire a real provider — it prints a clear refusal,
  mirroring the existing ``workers/run_quality_benchmark.py`` precedent.
* ``score`` refuses any fixture that is not genuine, finalized SME ground
  truth (see ``assert_finalized_sme_ground_truth``); AI-drafted fixtures
  such as ``workers/fixtures/quality_benchmark_v1.json`` are always
  rejected for scoring.
* The disposable V48 DSN is never printed or embedded in any output,
  error, or artifact — only whether it was supplied.
* ``score --benchmark-tier {pilot,launch}`` (V58-QUALITY-04E, hardened in
  V58-QUALITY-04E-R1, bound to prediction-artifact identity in
  V58-QUALITY-04E-R2) additionally runs the scorecard through the
  precommitted acceptance policy
  (``workers.quality_benchmark_policy.classify_scorecard``) and prints the
  resulting PASS / CONDITIONAL PASS / FAIL / INVALID RUN classification.
  ``--benchmark-tier launch`` additionally requires
  ``--review-attestation-json`` (a small JSON file attesting double-review
  coverage — see module help); this CLI refuses to attempt launch
  classification without it rather than silently treating missing
  attestation as "no double review happened".
* Configuration identity (provider/model/prompt/ruleset/evidence, plus
  engine id/version and the source-fixture hash) is recorded exclusively at
  **prediction-generation** time by the engine adapter
  (``workers.quality_benchmark_execution.generate_predictions``) and flows
  unchanged through ``score`` into the scorecard. There is no
  ``score``-time flag to supply, override, or relabel it (V58-QUALITY-04E-R2
  removed ``--engine-configuration-json`` for exactly this reason — it was
  a scoring-time relabeling loophole). ``score`` refuses to proceed (exit 1)
  if the prediction artifact's identity is incomplete or internally
  inconsistent.

Classification exit codes (``score --benchmark-tier ...`` only)
-----------------------------------------------------------------
    0  PASS
    4  CONDITIONAL PASS
    5  FAIL
    6  INVALID RUN
(``score`` without ``--benchmark-tier`` keeps its original exit codes: 0
success, 1 invalid input.)

Usage
-----
    python -m scripts.v58_run_quality_benchmark_engines generate \\
        --engine legacy --fixture workers/fixtures/quality_benchmark_v1.json \\
        --output /tmp/legacy_predictions.json

    python -m scripts.v58_run_quality_benchmark_engines generate \\
        --engine v48 --fixture workers/fixtures/quality_benchmark_v1.json \\
        --output /tmp/v48_predictions.json
    # -> exits with BLOCKED status; no artifact written

    python -m scripts.v58_run_quality_benchmark_engines generate \\
        --engine v48 --allow-disposable-v48-db \\
        --v48-db-url postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test \\
        --fixture workers/fixtures/quality_benchmark_v1.json \\
        --output /tmp/v48_predictions.json
    # -> still exits with BLOCKED status; this CLI does not inject a
    #    provider, so it never performs a live AI call or a real
    #    disposable-database write (see safety posture above)

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
import json
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
from workers.quality_benchmark_policy import (  # noqa: E402
    PolicyInputError,
    TIER_LAUNCH,
    TIER_PILOT,
    classify_scorecard,
)

_LIVE_FLAG = "CERTBOUND_ALLOW_LIVE_AI_TEST"
_V48_DB_URL_ENV = "CERTBOUND_V48_DISPOSABLE_DB_URL"

_CLASSIFICATION_EXIT_CODES = {
    "PASS": 0,
    "CONDITIONAL PASS": 4,
    "FAIL": 5,
    "INVALID RUN": 6,
}


def _build_fixture_metadata(fixture: dict, *, review_process: dict | None) -> dict:
    """Assemble ``fixture_metadata`` for ``classify_scorecard`` from an
    already finalized SME-reviewed fixture (see
    ``load_finalized_sme_ground_truth_fixture`` — by the time this is
    called, ``assert_finalized_sme_ground_truth`` has already accepted the
    fixture). Recomputes the ground-truth/provenance fields directly from
    the fixture anyway, rather than hardcoding them to "always true", so
    ``classify_scorecard``'s independent fail-closed checks stay meaningful
    if this function is ever called from a different, less-trusted path.

    Carries no engine-configuration identity at all (V58-QUALITY-04E-R2):
    ``classify_scorecard`` reads that exclusively from the scorecard's own
    ``configuration_identity`` (copied from the prediction artifact by
    ``score_predictions``), so there is nothing for this CLI to pass
    through or default here.
    """
    summary = fixture.get("sme_review_summary") or {}
    known_good_count = 0
    blocking_count = 0
    warning_count = 0
    defective_count = 0
    category_counts: dict[str, int] = {}
    for case in fixture["cases"]:
        if case.get("known_good"):
            known_good_count += 1
            continue
        defective_count += 1
        materiality = case.get("expected_materiality")
        if materiality == "blocking":
            blocking_count += 1
        elif materiality == "warning":
            warning_count += 1
        category = str(case.get("defect_category") or "")
        if category:
            category_counts[category] = category_counts.get(category, 0) + 1

    return {
        "ground_truth_finalized": fixture.get("sme_reviewed") is True,
        "is_ai_drafted": fixture.get("sme_reviewed") is not True,
        "sme_review_status": fixture.get("sme_review_status"),
        "rejected_case_ids": list(summary.get("rejected_case_ids") or []),
        "unresolved_second_review_case_ids": list(summary.get("unresolved_second_review_case_ids") or []),
        "source_fixture_sha256": fixture.get("source_fixture_sha256"),
        "configuration_mixed": False,
        "total_case_count": len(fixture["cases"]),
        "known_good_case_count": known_good_count,
        "defective_case_count": defective_count,
        "blocking_case_count": blocking_count,
        "warning_case_count": warning_count,
        "category_case_counts": category_counts,
        "review_process": review_process,
    }


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _build_adapter(args: argparse.Namespace):
    if args.engine == ENGINE_LEGACY:
        return LegacyEngineAdapter()
    if args.engine == ENGINE_V48:
        db_url = args.v48_db_url or os.environ.get(_V48_DB_URL_ENV)
        # Deliberately never passes a ``providers`` value here: this CLI
        # does not construct a live AI provider from any source (flags or
        # environment variables), so V48EngineAdapter._is_opted_in() is
        # always False and generate_prediction() always reports a clean
        # BLOCKED message rather than attempting a live/disposable-database
        # run. Real opt-in execution with an injected fake/test provider is
        # only exercised via the Python API in Docker-gated tests.
        return V48EngineAdapter(
            allow_disposable_db=args.allow_disposable_v48_db,
            disposable_db_url=db_url,
        )
    raise ValueError(f"unsupported engine {args.engine!r}")


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

    if args.engine == ENGINE_V48 and args.allow_disposable_v48_db:
        # Pure, no-I/O structural pre-check so a bad/missing DSN is reported
        # distinctly from "no opt-in" or "no provider injected" — never
        # connects, never touches the DSN string itself in output.
        from workers.quality_benchmark_v48_orchestration import (  # noqa: E402, PLC0415
            V48DisposableDsnRejectedError,
            validate_disposable_dsn,
        )

        db_url = args.v48_db_url or os.environ.get(_V48_DB_URL_ENV)
        try:
            validate_disposable_dsn(db_url)
        except V48DisposableDsnRejectedError as exc:
            print(f"BLOCKED: engine {args.engine!r} disposable-database DSN rejected.")
            print(f"Reason: {exc}")
            return 3
        print(
            "BLOCKED: --allow-disposable-v48-db and a valid disposable DSN were "
            "provided, but this CLI never constructs a live AI provider (by "
            "design — see module docstring), so no V48 disposable-database "
            "execution can proceed here."
        )
        print(
            "Follow-up: use the Python API "
            "(workers.quality_benchmark_v48_orchestration.generate_v48_prediction) "
            "with an explicitly injected test/fake provider, as "
            "tests/test_quality_benchmark_v48_orchestration.py does."
        )
        return 3

    try:
        fixture = load_benchmark_case_fixture(args.fixture)
    except (OSError, BenchmarkFixtureError, ValueError, TypeError) as exc:
        print(f"Invalid benchmark fixture: {exc}")
        return 1

    adapter = _build_adapter(args)

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

    if not args.benchmark_tier:
        return 0

    review_process = None
    if args.benchmark_tier == TIER_LAUNCH:
        if not args.review_attestation_json:
            print(
                "Refusing launch classification: --review-attestation-json is required for "
                "--benchmark-tier launch (double-review coverage cannot be inferred from case "
                "count alone)."
            )
            return 6
        try:
            with open(args.review_attestation_json, "r", encoding="utf-8-sig") as handle:
                review_process = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"Refusing launch classification: could not read --review-attestation-json: {exc}")
            return 6
        if not isinstance(review_process, dict):
            print("Refusing launch classification: --review-attestation-json must contain a JSON object.")
            return 6

    fixture_metadata = _build_fixture_metadata(fixture, review_process=review_process)

    try:
        result = classify_scorecard(scorecard, args.benchmark_tier, fixture_metadata)
    except PolicyInputError as exc:
        print(f"Invalid classification input: {exc}")
        return 1

    print("")
    print(f"benchmark_tier: {result['benchmark_tier']}")
    print(f"policy_version: {result['policy_version']}")
    print(f"classification: {result['classification']}")
    print(f"classification_language: {result['classification_language']}")
    print(f"reasons: {result['reasons']}")
    print(f"limitations: {result['limitations']}")
    print(f"configuration_identity: {result['configuration_identity']}")
    if args.classification_output:
        with open(args.classification_output, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"classification written to: {args.classification_output}")

    return _CLASSIFICATION_EXIT_CODES.get(result["classification"], 1)


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
    generate_parser.add_argument(
        "--allow-disposable-v48-db",
        action="store_true",
        help=(
            "Explicit opt-in for V48 disposable-database execution "
            "(V58-QUALITY-04C). Still requires --v48-db-url (or "
            f"{_V48_DB_URL_ENV}) and, since this CLI injects no provider, "
            "always reports BLOCKED — see module docstring."
        ),
    )
    generate_parser.add_argument(
        "--v48-db-url",
        default=None,
        help=(
            "Disposable V48 test-database DSN, e.g. "
            "postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test. "
            f"Falls back to {_V48_DB_URL_ENV} if unset. Never printed or "
            "stored in any output/artifact."
        ),
    )
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
    score_parser.add_argument(
        "--benchmark-tier",
        choices=[TIER_PILOT, TIER_LAUNCH],
        default=None,
        help=(
            "Optional (V58-QUALITY-04E): additionally classify the resulting scorecard under the "
            "precommitted acceptance policy (PASS/CONDITIONAL PASS/FAIL/INVALID RUN). Omit to keep "
            "the original score-only behavior."
        ),
    )
    score_parser.add_argument(
        "--review-attestation-json",
        default=None,
        help=(
            "Required with --benchmark-tier launch: path to a JSON object with "
            "blocking_cases_double_reviewed_count, blocking_cases_total_count, "
            "non_blocking_cases_double_reviewed_count, non_blocking_cases_total_count, and "
            "disagreements_adjudicated_or_excluded. Never inferred from case counts alone."
        ),
    )
    score_parser.add_argument(
        "--classification-output",
        default=None,
        help="Optional path to also write the full classify_scorecard() result as JSON.",
    )
    score_parser.set_defaults(func=_cmd_score)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
