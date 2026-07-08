#!/usr/bin/env python3
"""
V58 Day 8 — credential-safe OpenAI 40-case quality-benchmark baseline runner.

Generates an independent OpenAI *prediction* baseline against the exact
existing 40-case V58 quality benchmark
(``workers/fixtures/quality_benchmark_v1.json``), using the committed
``OpenAIAuditProvider`` and the real, completely unmodified V48 Pass A/B/C
worker/orchestration path
(``workers.quality_benchmark_v48_orchestration.run_v48_benchmark_case`` via
``workers.quality_benchmark_execution.V48EngineAdapter``).

This module does not implement its own audit engine, prompts, schemas,
scoring, or database-safety logic. It:

  * reuses ``V48EngineAdapter``/``generate_v48_prediction`` for real,
    per-case, guaranteed-rollback V48 execution (no changes to that module);
  * reuses ``validate_disposable_dsn`` for database host/name safety
    (no changes, no re-implementation);
  * reuses ``build_ai_quality_providers_from_env`` /
    ``resolve_ai_quality_model_provenance_from_env`` for provider/model
    resolution (no changes);
  * reuses ``load_benchmark_case_fixture`` for fixture structural
    validation (no changes);
  * adds only the smallest new logic genuinely absent from the existing
    pipeline: (1) a thin, in-process provider-call recorder that captures
    token/cost/request-id per Pass A/B/C call for artifact reporting
    (never touches prompts, schemas, or provider internals), and (2) a
    minimal per-case checkpoint/resume mechanism (the existing
    ``generate_predictions`` helper processes an entire fixture in one
    call with no intermediate persistence).

Ground-truth isolation
-----------------------
This module never imports, opens, or reads
``workers/fixtures/quality_benchmark_v1_sme_reviewed.json``. Only the
AI-drafted source fixture (``quality_benchmark_v1.json``) is read, and only
its ``question``/``resource_snapshot``/``certification``/``domain`` fields
are ever consulted by the (unmodified) seeding code in
``workers.quality_benchmark_v48_orchestration.seed_benchmark_case`` — the
per-case ``expected_finding_codes``/``known_good``/``reviewer_label``/
``expected_correct_option_labels``/``expected_materiality``/
``reviewer_rationale`` fields present in that fixture (drafted ground truth,
pending SME review) are never read, copied, or forwarded anywhere by this
script or by the seeding code it calls.

Safety gates (all enforced before any provider is constructed)
-----------------------------------------------------------------
  1. ``CERTBOUND_ALLOW_LIVE_AI_TEST=1`` and a non-blank
     ``CERTBOUND_OPENAI_API_KEY`` must both be present (see
     ``_check_live_authorization_gate``). The key value is never printed,
     logged, hashed, serialized, or included in any exception/artifact.
  2. The source fixture must load, have exactly 40 cases, and its
     SHA-256 must exactly match ``APPROVED_FIXTURE_SHA256`` (a
     repository-approved constant in this file) — see
     ``load_and_validate_fixture``.
  3. The resolved V48 disposable-database DSN must (a) contain no query
     string and no URL fragment -- see ``_reject_dsn_query_or_fragment``,
     which refuses before any further parsing; a DSN's authority/path can
     look like an approved loopback target under ``urlsplit()`` while a
     query-string parameter (e.g. ``?host=``/``?hostaddr=``/``?dbname=``)
     is given *higher* precedence by libpq/psycopg2 at actual connection
     time, silently redirecting the connection elsewhere -- and then (b)
     pass ``workers.quality_benchmark_v48_orchestration.validate_disposable_dsn``
     unchanged (loopback host, ``certbound_v48_test`` naming pattern). Both
     checks are performed by the single ``resolve_disposable_db_url``
     helper, used identically by live mode and ``--dry-run``.
  4. The resolved AI-quality provider configuration must resolve to
     ``openai`` for both the primary and dispute roles — see
     ``resolve_and_validate_provider_selection``. This baseline is a pure
     OpenAI run; it refuses rather than silently mixing providers.

Only after all four gates pass does ``build_ai_quality_providers_from_env``
run (constructing the real ``OpenAIAuditProvider`` — a local object; the
OpenAI SDK client itself does not make a network call at construction time).

Cost/checkpoint/resume
------------------------
Cases are processed strictly sequentially, exactly once each, identified by
``case_id``. After each case completes (success or per-case error), the
accumulated prediction list is written to ``checkpoint.json`` in the run
directory, so an interruption (crash, Ctrl+C, or an unrecoverable
``EngineAdapterUnavailableError``) never loses more than the one in-flight
case's paid work. ``--resume <run_dir>`` re-attaches to an existing
``checkpoint.json`` only when its recorded configuration fingerprint
(fixture hash, provider, model, reasoning effort, prompt/ruleset/evidence
identity, and the effective OpenAI timeout/max-retries/max-output-tokens
settings) exactly matches the current configuration; on any mismatch this
refuses outright rather than mixing results. A case already present in the
checkpoint is never re-run.

Artifacts
---------
Written under ``.local/v58_openai_baseline/<UTC_TIMESTAMP>/``:
  * ``checkpoint.json`` — updated after every case (resume input).
  * ``result.json`` — final artifact, written once the run stops (whether
    all 40 cases completed, the run was BLOCKED, or an interruption was
    observed). ``result.json`` is a strict superset of the existing
    ``quality-benchmark-prediction-v1`` schema produced by
    ``workers.quality_benchmark_execution.generate_predictions`` (same
    ``schema_version``/``engine_id``/``engine_version``/
    ``configuration_identity``/``provider_config``/``source_fixture_*``/
    ``case_count``/``predictions``/``error_case_count`` fields, byte-for-byte
    consistent with what that function would have produced), plus
    baseline-runner-specific provenance (run id, timestamps, effective
    model/reasoning-effort/timeout/retry configuration excluding secrets,
    aggregated token/cost/request-id totals, and post-run database
    cleanup-count verification). This means the existing, unmodified
    ``score`` subcommand of ``scripts/v58_run_quality_benchmark_engines.py``
    can consume ``result.json`` directly.

This script never scores predictions and never reads SME-reviewed ground
truth. Scoring remains a fully separate, offline step.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENV_ALLOW_LIVE = "CERTBOUND_ALLOW_LIVE_AI_TEST"
ENV_OPENAI_API_KEY = "CERTBOUND_OPENAI_API_KEY"
ENV_PRIMARY_PROVIDER = "CERTBOUND_AI_QUALITY_PRIMARY_LLM_PROVIDER"
ENV_DISPUTE_PROVIDER = "CERTBOUND_AI_QUALITY_DISPUTE_LLM_PROVIDER"
ENV_V48_DB_URL = "CERTBOUND_V48_DISPOSABLE_DB_URL"
ENV_V48_DB_URL_FALLBACK = "V48_TEST_DATABASE_URL"
ENV_OPENAI_TIMEOUT = "CERTBOUND_OPENAI_TIMEOUT_SECONDS"
ENV_OPENAI_MAX_RETRIES = "CERTBOUND_OPENAI_MAX_RETRIES"
ENV_OPENAI_MAX_OUTPUT_TOKENS = "CERTBOUND_OPENAI_MAX_OUTPUT_TOKENS"

# Documented disposable-test-environment default (matches
# tests/test_quality_benchmark_v48_orchestration.py and
# tests/test_ai_quality_audit_integration.py). Always re-validated by
# validate_disposable_dsn() regardless of source.
DEFAULT_DISPOSABLE_DSN = "postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test"

FIXTURE_PATH = _REPO_ROOT / "workers" / "fixtures" / "quality_benchmark_v1.json"
# Never referenced for reading anywhere in this module — documented here
# only so the ground-truth isolation guarantee above is grep-able.
_FORBIDDEN_SME_REVIEWED_FIXTURE_PATH = (
    _REPO_ROOT / "workers" / "fixtures" / "quality_benchmark_v1_sme_reviewed.json"
)

EXPECTED_CASE_COUNT = 40
# Repository-approved hash of workers/fixtures/quality_benchmark_v1.json at
# the time this runner was prepared (V58-DAY8-OPENAI-07). Any mismatch
# refuses outright; there is no override flag.
APPROVED_FIXTURE_SHA256 = "8dad069126d84e826f7edcc180773f2278583e41ec964cdb86c4e6d503cb9fa6"

ARTIFACT_ROOT = _REPO_ROOT / ".local" / "v58_openai_baseline"

WORKER_ID = "v58-day8-openai-baseline"
CHECKPOINT_SCHEMA_VERSION = "v58-openai-baseline-checkpoint-v1"
RESULT_ARTIFACT_SCHEMA_VERSION = "quality-benchmark-prediction-v1"

# Tables whose zero-row cleanup contract this task calls out explicitly.
CLEANUP_VERIFICATION_TABLES = (
    "questions",
    "audit_runs",
    "audit_findings",
    "audit_finding_evidence",
)


class BaselineRunnerRefusal(Exception):
    """Raised for any classified pre-flight refusal. Message must already be
    sanitized (no secrets); exit_code is returned by main()."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _running_under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


# ---------------------------------------------------------------------------
# Gate 1: credential-safe live authorization
# ---------------------------------------------------------------------------


def _check_live_authorization_gate() -> None:
    """Refuse before any provider or database code is touched. Never reads
    the API key's value beyond checking it is non-blank."""
    if os.environ.get(ENV_ALLOW_LIVE, "").strip() != "1":
        raise BaselineRunnerRefusal(
            f"{ENV_ALLOW_LIVE} must be set to exactly '1' to authorize a live "
            "OpenAI benchmark baseline run that makes real, billed API calls. "
            "No provider was constructed and no network call was made."
        )
    if not os.environ.get(ENV_OPENAI_API_KEY, "").strip():
        raise BaselineRunnerRefusal(
            f"{ENV_OPENAI_API_KEY} is not set. This script never prints, logs, "
            "hashes, or infers the key; set it in this shell session before "
            "running. No provider was constructed and no network call was made."
        )


# ---------------------------------------------------------------------------
# Gate 2: fixture validation (no SME-reviewed fixture is ever referenced)
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def load_and_validate_fixture(path: Path) -> Tuple[dict, str]:
    """Load and validate the benchmark source fixture for live generation.

    Reuses ``workers.quality_benchmark_execution.load_benchmark_case_fixture``
    for structural schema validation (no re-implementation). Additionally
    refuses (``BaselineRunnerRefusal``) unless the fixture has exactly
    ``EXPECTED_CASE_COUNT`` cases and its SHA-256 exactly matches
    ``APPROVED_FIXTURE_SHA256``. Never opens any other fixture file.
    """
    from workers.quality_benchmark import BenchmarkFixtureError
    from workers.quality_benchmark_execution import load_benchmark_case_fixture

    if not path.exists():
        raise BaselineRunnerRefusal(f"benchmark fixture not found: {path}")

    fixture_sha256 = _sha256_file(path)

    try:
        fixture = load_benchmark_case_fixture(path)
    except (OSError, BenchmarkFixtureError, ValueError, TypeError) as exc:
        raise BaselineRunnerRefusal(f"invalid benchmark fixture {path}: {exc}") from exc

    case_count = len(fixture.get("cases") or [])
    if case_count != EXPECTED_CASE_COUNT:
        raise BaselineRunnerRefusal(
            f"benchmark fixture {path} has {case_count} case(s); this runner "
            f"requires exactly {EXPECTED_CASE_COUNT}"
        )

    if fixture_sha256 != APPROVED_FIXTURE_SHA256:
        raise BaselineRunnerRefusal(
            f"benchmark fixture {path} SHA-256 {fixture_sha256!r} does not match "
            f"the repository-approved hash {APPROVED_FIXTURE_SHA256!r} recorded in "
            "this script; refusing (there is no override flag - update "
            "APPROVED_FIXTURE_SHA256 only via a deliberate, reviewed code change "
            "if the fixture is intentionally updated)"
        )

    return fixture, fixture_sha256


# ---------------------------------------------------------------------------
# Gate 3: disposable-database DSN safety (fully reused, not re-implemented)
# ---------------------------------------------------------------------------


def _reject_dsn_query_or_fragment(dsn: str) -> None:
    """Refuse a disposable-database DSN carrying a query string or a URL
    fragment, before any further parsing, validation, or connection.

    libpq/psycopg2 DSN parsing gives query-string connection parameters
    (``host``, ``hostaddr``, ``dbname``, ``port``, ``options``, ...)
    *higher* precedence than the values embedded in the URI's own
    authority/path components. This means a DSN can pass a
    ``urlsplit()``-based host/database-name check (e.g.
    ``validate_disposable_dsn``) while ``psycopg2.connect()`` actually
    connects somewhere else entirely -- for example
    ``...@127.0.0.1:54329/certbound_v48_test?host=evil.example.com``
    structurally looks like an approved loopback DSN but libpq resolves
    the real connection host to ``evil.example.com``. This runner's
    approved disposable DSN never legitimately needs a query string or a
    fragment, so either one is refused outright with no override.

    The DSN itself is never included in the refusal message because it
    may contain a password.
    """
    parsed = urlsplit(dsn.strip())
    if parsed.query:
        raise BaselineRunnerRefusal(
            "disposable-database DSN must not contain a query string "
            "(e.g. ?host=/?hostaddr=/?dbname=/?port=/?options=...); "
            "libpq/psycopg2 gives query-string connection parameters "
            "precedence over the DSN's own host/database, which could "
            "silently redirect this runner to an unapproved database. "
            "Remove the query string and retry. (DSN value withheld: it "
            "may contain a password.)"
        )
    if parsed.fragment:
        raise BaselineRunnerRefusal(
            "disposable-database DSN must not contain a URL fragment. "
            "Remove it and retry. (DSN value withheld: it may contain a "
            "password.)"
        )


def resolve_disposable_db_url(explicit: Optional[str] = None) -> str:
    """Resolve the V48 disposable-database DSN from (in order) an explicit
    argument, ``CERTBOUND_V48_DISPOSABLE_DB_URL``,
    ``V48_TEST_DATABASE_URL``, or the documented local default -- reject
    outright if it carries a query string or fragment (see
    ``_reject_dsn_query_or_fragment``) -- then validate it with the
    existing, unmodified
    ``workers.quality_benchmark_v48_orchestration.validate_disposable_dsn``.
    Raises that function's ``V48DisposableDsnRejectedError`` unchanged on
    any host/name mismatch.

    This is the single runner-local DSN-resolution helper used by both
    live mode (``_run_live``) and ``--dry-run`` (``_run_dry_run``), so
    both modes enforce exactly the same validation.
    """
    from workers.quality_benchmark_v48_orchestration import validate_disposable_dsn

    dsn = (
        explicit
        or os.environ.get(ENV_V48_DB_URL, "").strip()
        or os.environ.get(ENV_V48_DB_URL_FALLBACK, "").strip()
        or DEFAULT_DISPOSABLE_DSN
    )
    _reject_dsn_query_or_fragment(dsn)
    validate_disposable_dsn(dsn)  # raises V48DisposableDsnRejectedError on rejection
    return dsn


# ---------------------------------------------------------------------------
# Gate 4: provider selection must resolve to openai (primary and dispute)
# ---------------------------------------------------------------------------


def resolve_and_validate_provider_selection():
    """Force the AI-quality primary provider to ``openai`` (only if the
    caller has not already configured one explicitly) and refuse unless the
    resolved primary *and* dispute providers are both ``openai`` -- this is
    a pure OpenAI baseline, never a mixed-provider run.
    """
    from workers.ai_quality_provider_factory import (
        AiQualityProviderConfigError,
        resolve_ai_quality_model_provenance_from_env,
    )

    os.environ.setdefault(ENV_PRIMARY_PROVIDER, "openai")

    try:
        provenance = resolve_ai_quality_model_provenance_from_env()
    except AiQualityProviderConfigError as exc:
        raise BaselineRunnerRefusal(f"AI-quality provider configuration error: {exc}") from exc

    if provenance.primary_provider != "openai" or provenance.dispute_provider != "openai":
        raise BaselineRunnerRefusal(
            "this OpenAI baseline runner requires provider=openai for both primary "
            f"and dispute roles; resolved primary={provenance.primary_provider!r} "
            f"dispute={provenance.dispute_provider!r}. Unset "
            f"{ENV_PRIMARY_PROVIDER}/{ENV_DISPUTE_PROVIDER}/CERTBOUND_LLM_PROVIDER "
            "or set them to 'openai' and re-run."
        )
    return provenance


# ---------------------------------------------------------------------------
# Provider-call instrumentation (thin, in-process, no orchestration changes)
# ---------------------------------------------------------------------------


class PassCallRecorder:
    """Wraps provider callables to capture per-Pass-A/B/C token/cost/
    request-id/duration metadata for the current case, without altering the
    call, its arguments, or its return value in any way.

    Attribution to a case relies entirely on ``set_case`` being called by
    the sequential case loop immediately before invoking
    ``adapter.generate_prediction(case)`` -- safe because execution is
    strictly sequential (no concurrent calls are ever made).
    """

    def __init__(self) -> None:
        self._current_case_id: Optional[str] = None
        self._calls_by_case: Dict[str, List[Dict[str, Any]]] = {}

    def set_case(self, case_id: str) -> None:
        self._current_case_id = case_id
        self._calls_by_case.setdefault(case_id, [])

    def pop_case_calls(self, case_id: str) -> List[Dict[str, Any]]:
        return self._calls_by_case.pop(case_id, [])

    def _record(self, record: Dict[str, Any]) -> None:
        case_id = self._current_case_id or "<unattributed>"
        self._calls_by_case.setdefault(case_id, []).append(record)

    def wrap(self, provider: Callable[..., Any], *, role: str) -> Callable[..., Any]:
        def _wrapped(
            *,
            model_name: str,
            system_prompt: str,
            user_prompt: str,
            response_schema: dict,
            metadata: Optional[dict] = None,
        ):
            pass_code = (metadata or {}).get("pass_code")
            started = time.monotonic()
            try:
                response = provider(
                    model_name=model_name,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_schema=response_schema,
                    metadata=metadata,
                )
            except Exception as exc:  # noqa: BLE001 - observe, never swallow
                # The provider's own LlmProviderError message is already
                # sanitized (no API key, headers, prompts, or raw SDK
                # objects) -- see workers.openai_provider.describe_openai_error.
                # This is a defensive bound only, not a re-sanitization.
                message = f"{type(exc).__name__}: {exc}"
                if len(message) > 2000:
                    message = message[:2000] + "...[truncated]"
                self._record(
                    {
                        "role": role,
                        "pass_code": pass_code,
                        "status": "error",
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "error": message,
                    }
                )
                raise
            self._record(
                {
                    "role": role,
                    "pass_code": pass_code,
                    "status": "success",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "provider_name": getattr(response, "provider_name", None),
                    "model_name": getattr(response, "model_name", None),
                    "provider_request_id": getattr(response, "provider_request_id", None),
                    "input_tokens": getattr(response, "input_tokens", None),
                    "output_tokens": getattr(response, "output_tokens", None),
                    "actual_cost_usd": getattr(response, "actual_cost_usd", None),
                }
            )
            return response

        return _wrapped


def build_instrumented_providers(base_providers: Any, recorder: PassCallRecorder) -> Any:
    from workers.ai_quality_audit_worker import AiQualityAuditProviders

    return AiQualityAuditProviders(
        primary=recorder.wrap(base_providers.primary, role="primary"),
        dispute=recorder.wrap(base_providers.dispute, role="dispute"),
        timeout_seconds=base_providers.timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Adapter construction
# ---------------------------------------------------------------------------


def build_v48_adapter(*, dsn: str, providers: Any, provenance: Any, evidence_config_id: str):
    from workers.quality_benchmark_execution import V48EngineAdapter
    from workers.quality_benchmark_v48_orchestration import (
        DEFAULT_PROMPT_VERSION,
        DEFAULT_RULESET_VERSION,
    )

    return V48EngineAdapter(
        allow_disposable_db=True,
        disposable_db_url=dsn,
        providers=providers,
        worker_id=WORKER_ID,
        provider_id=provenance.primary_provider,
        model_id=provenance.primary_model_name,
        prompt_version=DEFAULT_PROMPT_VERSION,
        ruleset_version=DEFAULT_RULESET_VERSION,
        evidence_config_id=evidence_config_id,
    )


# ---------------------------------------------------------------------------
# Checkpoint / resume
# ---------------------------------------------------------------------------


def compute_config_fingerprint(
    *,
    fixture_sha256: str,
    adapter_config: Dict[str, Any],
    reasoning_effort: str,
    openai_timeout_seconds: float,
    openai_max_retries: int,
    openai_max_output_tokens: int,
) -> Dict[str, Any]:
    """Configuration fingerprint used to gate ``--resume`` compatibility.

    ``openai_timeout_seconds``/``openai_max_retries``/``openai_max_output_tokens``
    must be the already-parsed, canonical numeric values from
    ``describe_effective_openai_settings()`` (never re-read from the
    environment here), so that an env-var override and the equal default
    value normalize to the exact same fingerprint entry (no false
    mismatch), while a genuinely different value correctly refuses resume.
    All values are plain JSON-serializable types (str/int/float), so the
    fingerprint is deterministic and safe to embed in ``checkpoint.json``.
    """
    return {
        "fixture_sha256": fixture_sha256,
        "engine_id": adapter_config.get("engine_id"),
        "engine_version": adapter_config.get("engine_version"),
        "provider_id": adapter_config.get("provider_id"),
        "model_id": adapter_config.get("model_id"),
        "prompt_version": adapter_config.get("prompt_version"),
        "ruleset_version": adapter_config.get("ruleset_version"),
        "evidence_config_id": adapter_config.get("evidence_config_id"),
        "reasoning_effort": reasoning_effort,
        "openai_timeout_seconds": openai_timeout_seconds,
        "openai_max_retries": openai_max_retries,
        "openai_max_output_tokens": openai_max_output_tokens,
    }


def write_checkpoint(run_dir: Path, checkpoint: Dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "checkpoint.json"
    tmp_path = run_dir / "checkpoint.json.tmp"
    tmp_path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def load_checkpoint(run_dir: Path) -> Dict[str, Any]:
    path = run_dir / "checkpoint.json"
    if not path.exists():
        raise BaselineRunnerRefusal(f"--resume directory has no checkpoint.json: {run_dir}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or "predictions" not in data or "config_fingerprint" not in data:
        raise BaselineRunnerRefusal(f"checkpoint.json at {run_dir} is malformed")
    return data


def resume_or_start_run(
    *, resume_dir: Optional[Path], config_fingerprint: Dict[str, Any]
) -> Tuple[Path, List[Dict[str, Any]], set]:
    """Return (run_dir, existing_predictions, completed_case_ids).

    Refuses (``BaselineRunnerRefusal``) if ``resume_dir`` is given but its
    checkpoint's configuration fingerprint does not exactly match
    ``config_fingerprint`` -- this is the only compatibility check resume
    performs, and it is intentionally exact-match (never a partial or
    best-effort comparison), so incompatible configurations can never be
    silently mixed.
    """
    if resume_dir is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = ARTIFACT_ROOT / timestamp
        return run_dir, [], set()

    checkpoint = load_checkpoint(resume_dir)
    if checkpoint.get("config_fingerprint") != config_fingerprint:
        raise BaselineRunnerRefusal(
            f"--resume directory {resume_dir} has an incompatible configuration "
            "fingerprint (fixture hash, provider, model, reasoning effort, or "
            "prompt/ruleset/evidence identity differs from the current "
            "configuration); refusing to resume rather than mixing results. "
            f"checkpoint fingerprint={checkpoint.get('config_fingerprint')!r} "
            f"current fingerprint={config_fingerprint!r}"
        )
    predictions = list(checkpoint.get("predictions") or [])
    completed_case_ids = {str(p["case_id"]) for p in predictions if p.get("case_id")}
    return resume_dir, predictions, completed_case_ids


# ---------------------------------------------------------------------------
# Core sequential case loop (adapter-Protocol-only; fully unit-testable with
# a fake adapter -- no real V48/DB/network dependency)
# ---------------------------------------------------------------------------


def run_case_loop(
    fixture: Dict[str, Any],
    adapter: Any,
    *,
    recorder: PassCallRecorder,
    run_dir: Path,
    config_fingerprint: Dict[str, Any],
    existing_predictions: List[Dict[str, Any]],
    completed_case_ids: set,
    print_progress: bool = True,
) -> List[Dict[str, Any]]:
    """Run every not-yet-completed case in *fixture* through *adapter*
    exactly once, in fixture order, writing checkpoint.json after each
    completed case. Propagates ``EngineAdapterUnavailableError`` immediately
    (whole-engine blocker, matching ``generate_predictions``'s own
    contract) -- the checkpoint already reflects every case completed
    before the blocker, so no completed paid work is lost. Any other
    per-case exception (defensive; the real V48 adapter already converts
    provider/worker failures into ``CasePrediction.error`` itself) is
    likewise converted to a per-case error rather than aborting the run.
    """
    from workers.quality_benchmark_execution import CasePrediction, EngineAdapterUnavailableError

    predictions = list(existing_predictions)
    total_cases = len(fixture["cases"])

    for case in fixture["cases"]:
        case_id = str(case["case_id"])
        if case_id in completed_case_ids:
            if print_progress:
                print(f"  skip (already completed): {case_id}")
            continue

        recorder.set_case(case_id)
        case_started = time.monotonic()
        try:
            prediction = adapter.generate_prediction(case)
        except EngineAdapterUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - never silently drop a case
            prediction = CasePrediction(case_id=case_id, error=f"{type(exc).__name__}: {exc}")

        if prediction.case_id != case_id:
            prediction.case_id = case_id

        case_duration = round(time.monotonic() - case_started, 3)
        calls = recorder.pop_case_calls(case_id)
        prediction.raw_output = dict(prediction.raw_output or {})
        prediction.raw_output["provider_calls"] = calls
        prediction.raw_output["case_duration_seconds"] = case_duration

        predictions.append(prediction.to_dict())
        completed_case_ids.add(case_id)

        write_checkpoint(
            run_dir,
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "config_fingerprint": config_fingerprint,
                "predictions": predictions,
            },
        )
        if print_progress:
            status = "error" if prediction.error else "ok"
            print(f"  completed case {case_id} ({len(predictions)}/{total_cases}) status={status}")

    return predictions


# ---------------------------------------------------------------------------
# Totals aggregation and final artifact construction
# ---------------------------------------------------------------------------


def aggregate_totals(predictions: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_calls = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd: Optional[float] = 0.0
    cost_available = True
    request_ids: List[str] = []

    for prediction in predictions:
        calls = ((prediction.get("raw_output") or {}).get("provider_calls")) or []
        for call in calls:
            if call.get("status") != "success":
                continue
            total_calls += 1
            if call.get("input_tokens") is not None:
                total_input_tokens += int(call["input_tokens"])
            if call.get("output_tokens") is not None:
                total_output_tokens += int(call["output_tokens"])
            if call.get("actual_cost_usd") is not None:
                total_cost_usd = (total_cost_usd or 0.0) + float(call["actual_cost_usd"])
            else:
                cost_available = False
            if call.get("provider_request_id"):
                request_ids.append(call["provider_request_id"])

    return {
        "total_call_count": total_calls,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": round(total_cost_usd, 6) if (cost_available and total_calls) else None,
        "provider_request_ids": request_ids,
    }


def build_final_artifact(
    *,
    adapter: Any,
    source_fixture_path: Path,
    source_fixture_sha256: str,
    case_count: int,
    predictions: List[Dict[str, Any]],
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    requested_model: str,
    reasoning_effort: str,
    provider_timing_config: Dict[str, Any],
    database_info: Dict[str, Any],
    run_status: str,
    blocked_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the final prediction artifact. Mirrors the exact tail
    construction of ``workers.quality_benchmark_execution.generate_predictions``
    (same required fields/shape) so the existing, unmodified ``score``
    subcommand can consume this artifact unchanged, plus baseline-runner
    provenance fields that scorer ignores as harmless extras.
    """
    config = adapter.describe_config()
    configuration_identity = {
        "engine_id": config.get("engine_id"),
        "engine_version": config.get("engine_version"),
        "provider_id": config.get("provider_id"),
        "model_id": config.get("model_id"),
        "prompt_version": config.get("prompt_version"),
        "ruleset_version": config.get("ruleset_version"),
        "evidence_config_id": config.get("evidence_config_id"),
        "source_fixture_sha256": source_fixture_sha256,
    }
    error_case_count = sum(1 for p in predictions if p.get("error"))

    return {
        "schema_version": RESULT_ARTIFACT_SCHEMA_VERSION,
        "engine_id": config.get("engine_id"),
        "engine_version": config.get("engine_version"),
        "configuration_identity": configuration_identity,
        "provider_config": config,
        "generated_at_utc": completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_fixture_path": str(source_fixture_path),
        "source_fixture_sha256": source_fixture_sha256,
        "case_count": case_count,
        "predictions": predictions,
        "error_case_count": error_case_count,
        # Baseline-runner provenance (superset fields; ignored by the
        # existing scorer, never consumed as scoring input).
        "run_id": run_id,
        "started_at_utc": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at_utc": completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
        "requested_model": requested_model,
        "resolved_model": config.get("model_id"),
        "reasoning_effort": reasoning_effort,
        "provider_configuration": provider_timing_config,
        "totals": aggregate_totals(predictions),
        "database": database_info,
        "run_status": run_status,
        "blocked_reason": blocked_reason,
    }


def write_result_artifact(run_dir: Path, artifact: Dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "result.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Post-run database cleanup verification (read-only, best-effort)
# ---------------------------------------------------------------------------


def count_cleanup_tables(dsn: str, tables=CLEANUP_VERIFICATION_TABLES) -> Dict[str, Any]:
    """Best-effort, read-only row-count verification for the named tables.

    Every real V48 case transaction is already guaranteed to ROLLBACK
    (``workers.quality_benchmark_v48_orchestration.v48_disposable_transaction``)
    regardless of outcome, so this is a verification/reporting step, not the
    mechanism that achieves zero-row cleanup. Never raises; reports
    ``verified: false`` with a sanitized reason on any failure so a
    verification-step problem never crashes artifact generation.
    """
    try:
        import psycopg2  # noqa: PLC0415
    except ImportError:
        return {"verified": False, "reason": "psycopg2 not installed", "counts": {}}

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:  # noqa: BLE001 - sanitized, never a raw traceback
        return {
            "verified": False,
            "reason": f"could not connect for verification: {type(exc).__name__}",
            "counts": {},
        }

    counts: Dict[str, int] = {}
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"SELECT COUNT(*) FROM public.{table}")  # noqa: S608 - fixed allow-listed table names
                row = cur.fetchone()
                counts[table] = int(row[0]) if row else -1
        return {"verified": True, "reason": None, "counts": counts}
    except Exception as exc:  # noqa: BLE001
        return {"verified": False, "reason": f"{type(exc).__name__}", "counts": counts}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Effective-configuration reporting (printed before any live call; also
# used by --dry-run, which makes zero network calls and constructs no
# provider or database connection)
# ---------------------------------------------------------------------------


def describe_effective_openai_settings() -> Dict[str, Any]:
    """Read-only, no-API-key-required OpenAI configuration summary.

    ``timeout_seconds`` (float), ``max_retries`` (int), and
    ``max_output_tokens`` (int) are the exact canonical values -- reusing
    ``workers.openai_provider``'s own env-parsing helpers and defaults,
    never re-implemented here -- that
    ``workers.openai_provider.load_openai_config_from_env`` itself would
    resolve. Reusing them (rather than re-deriving display strings) means
    an explicit env-var override equal to the default, and the default
    itself, always normalize to the identical effective value, both for
    display and for ``compute_config_fingerprint`` (no false resume
    mismatch from a numeric-equivalent but textually-different setting).
    """
    from workers.openai_provider import (
        DEFAULT_MAX_OUTPUT_TOKENS,
        DEFAULT_MAX_RETRIES,
        DEFAULT_REASONING_EFFORT,
        DEFAULT_TIMEOUT_SECONDS,
        ENV_MAX_OUTPUT_TOKENS,
        ENV_MAX_RETRIES,
        ENV_MODEL,
        ENV_REASONING_EFFORT,
        ENV_TIMEOUT,
        _read_float,
        _read_int,
        resolve_openai_model_from_env,
    )

    return {
        "requested_model": resolve_openai_model_from_env(),
        "model_env_override": os.environ.get(ENV_MODEL, ""),
        "reasoning_effort": os.environ.get(ENV_REASONING_EFFORT, "").strip() or DEFAULT_REASONING_EFFORT,
        "timeout_seconds": _read_float(ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS),
        "max_retries": _read_int(ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES),
        "max_output_tokens": _read_int(ENV_MAX_OUTPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS),
    }


def _print_effective_configuration(
    *, fixture_path: Path, fixture_sha256: str, case_count: int, openai_settings: Dict[str, Any], dsn_info=None
) -> None:
    print("V58 Day 8 OpenAI 40-case benchmark baseline runner")
    print(f"  fixture_path:          {fixture_path}")
    print(f"  fixture_sha256:        {fixture_sha256}")
    print(f"  case_count:            {case_count}")
    print("  provider:              openai")
    print(f"  requested_model:       {openai_settings['requested_model']}")
    print(f"  reasoning_effort:      {openai_settings['reasoning_effort']}")
    print(f"  timeout_seconds:       {openai_settings['timeout_seconds']}")
    print(f"  max_retries:           {openai_settings['max_retries']}")
    print(f"  max_output_tokens:     {openai_settings['max_output_tokens']}")
    if dsn_info is not None:
        print(f"  db_host:               {dsn_info.host}")
        print(f"  db_name:               {dsn_info.dbname}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate fixture, DSN, and provider/model configuration and print "
            "the effective settings without authorizing live calls, "
            "constructing any provider, or connecting to any database. Does "
            "not require CERTBOUND_ALLOW_LIVE_AI_TEST or an API key."
        ),
    )
    parser.add_argument(
        "--resume",
        metavar="RUN_DIR",
        default=None,
        help=(
            "Resume an interrupted run from an existing run directory "
            "containing checkpoint.json. Refuses if the checkpoint's "
            "configuration fingerprint does not exactly match the current "
            "configuration."
        ),
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help=(
            "Override the V48 disposable database DSN (defaults to "
            f"{ENV_V48_DB_URL} / {ENV_V48_DB_URL_FALLBACK} / the documented "
            "local default). Always re-validated as a disposable test database."
        ),
    )
    return parser


def _run_dry_run(args: argparse.Namespace) -> int:
    fixture, fixture_sha256 = load_and_validate_fixture(FIXTURE_PATH)
    case_count = len(fixture["cases"])

    from workers.quality_benchmark_v48_orchestration import validate_disposable_dsn

    # Shared with live mode: resolves the DSN and refuses a query
    # string/fragment or a disallowed host/name before any connection
    # attempt (see resolve_disposable_db_url/_reject_dsn_query_or_fragment).
    dsn = resolve_disposable_db_url(args.db_url)
    dsn_info = validate_disposable_dsn(dsn)  # structural validation only; no connection attempt

    os.environ.setdefault(ENV_PRIMARY_PROVIDER, "openai")
    from workers.ai_quality_provider_factory import (
        AiQualityProviderConfigError,
        resolve_ai_quality_model_provenance_from_env,
    )

    try:
        provenance = resolve_ai_quality_model_provenance_from_env()
    except AiQualityProviderConfigError as exc:
        raise BaselineRunnerRefusal(f"AI-quality provider configuration error: {exc}") from exc

    openai_settings = describe_effective_openai_settings()
    _print_effective_configuration(
        fixture_path=FIXTURE_PATH,
        fixture_sha256=fixture_sha256,
        case_count=case_count,
        openai_settings=openai_settings,
        dsn_info=dsn_info,
    )
    print(f"  resolved primary provider: {provenance.primary_provider}")
    print(f"  resolved dispute provider: {provenance.dispute_provider}")
    print()

    if provenance.primary_provider != "openai" or provenance.dispute_provider != "openai":
        print(
            "DRY RUN result: WOULD REFUSE at live-run time -- provider selection "
            "does not resolve to openai for both primary and dispute roles.",
        )
        return 1

    print(
        "DRY RUN result: fixture valid (exact case count and approved hash), "
        "DSN structurally valid, provider selection valid (openai/openai). "
        "Zero network calls made. Zero database connections attempted. "
        "Zero providers constructed."
    )
    return 0


def _run_live(args: argparse.Namespace) -> int:
    _check_live_authorization_gate()

    fixture, fixture_sha256 = load_and_validate_fixture(FIXTURE_PATH)
    case_count = len(fixture["cases"])

    dsn = resolve_disposable_db_url(args.db_url)
    from workers.quality_benchmark_v48_orchestration import validate_disposable_dsn

    dsn_info = validate_disposable_dsn(dsn)

    provenance = resolve_and_validate_provider_selection()

    # Provider construction happens only now, after every safety gate above
    # has passed. Constructing OpenAIAuditProvider loads/validates local
    # configuration only; it makes no network call.
    from workers.ai_quality_provider_factory import build_ai_quality_providers_from_env

    base_providers = build_ai_quality_providers_from_env(required=True)
    recorder = PassCallRecorder()
    instrumented_providers = build_instrumented_providers(base_providers, recorder)

    evidence_config_id = str(fixture.get("evidence_fixture") or "quality_benchmark_v1-frozen-evidence")
    adapter = build_v48_adapter(
        dsn=dsn,
        providers=instrumented_providers,
        provenance=provenance,
        evidence_config_id=evidence_config_id,
    )
    adapter_config = adapter.describe_config()

    openai_settings = describe_effective_openai_settings()
    reasoning_effort = openai_settings["reasoning_effort"]

    _print_effective_configuration(
        fixture_path=FIXTURE_PATH,
        fixture_sha256=fixture_sha256,
        case_count=case_count,
        openai_settings=openai_settings,
        dsn_info=dsn_info,
    )
    print(f"  worker_id:             {WORKER_ID}")
    print(f"  prompt_version:        {adapter_config.get('prompt_version')}")
    print(f"  ruleset_version:       {adapter_config.get('ruleset_version')}")
    print(f"  evidence_config_id:    {adapter_config.get('evidence_config_id')}")
    print("  intended live calls:   up to 3 per case (Pass A, Pass B; Pass C only "
          "when a dispute trigger fires) x 40 cases")
    print()

    config_fingerprint = compute_config_fingerprint(
        fixture_sha256=fixture_sha256,
        adapter_config=adapter_config,
        reasoning_effort=reasoning_effort,
        openai_timeout_seconds=openai_settings["timeout_seconds"],
        openai_max_retries=openai_settings["max_retries"],
        openai_max_output_tokens=openai_settings["max_output_tokens"],
    )

    resume_dir = Path(args.resume).resolve() if args.resume else None
    run_dir, existing_predictions, completed_case_ids = resume_or_start_run(
        resume_dir=resume_dir, config_fingerprint=config_fingerprint
    )
    print(f"  run_dir:               {run_dir}")
    if existing_predictions:
        print(f"  resuming with {len(existing_predictions)} already-completed case(s)")
    print()

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    provider_timing_config = {
        "openai_timeout_seconds": openai_settings["timeout_seconds"],
        "openai_max_retries": openai_settings["max_retries"],
        "openai_max_output_tokens": openai_settings["max_output_tokens"],
        "ai_quality_worker_timeout_seconds": base_providers.timeout_seconds,
    }

    run_status = "completed"
    blocked_reason: Optional[str] = None
    predictions = existing_predictions

    from workers.quality_benchmark_execution import EngineAdapterUnavailableError

    try:
        predictions = run_case_loop(
            fixture,
            adapter,
            recorder=recorder,
            run_dir=run_dir,
            config_fingerprint=config_fingerprint,
            existing_predictions=existing_predictions,
            completed_case_ids=completed_case_ids,
        )
    except EngineAdapterUnavailableError as exc:
        run_status = "blocked"
        blocked_reason = f"{exc.reason} | follow_up: {exc.follow_up}"
        print(f"BLOCKED: {blocked_reason}", file=sys.stderr)
        print(f"{len(predictions)}/{case_count} case(s) were completed before the block.", file=sys.stderr)
    except BaseException:
        # Includes KeyboardInterrupt: checkpoint.json already reflects every
        # case completed before the interruption (written synchronously
        # after each case in run_case_loop), so no completed paid work is
        # lost. Re-raise after best-effort final-artifact reporting.
        run_status = "interrupted"
        blocked_reason = "run was interrupted before completion"
        completed_at = datetime.now(timezone.utc)
        cleanup_counts = count_cleanup_tables(dsn)
        artifact = build_final_artifact(
            adapter=adapter,
            source_fixture_path=FIXTURE_PATH,
            source_fixture_sha256=fixture_sha256,
            case_count=case_count,
            predictions=predictions,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            requested_model=openai_settings["requested_model"],
            reasoning_effort=reasoning_effort,
            provider_timing_config=provider_timing_config,
            database_info={"host": dsn_info.host, "dbname": dsn_info.dbname, "cleanup": cleanup_counts},
            run_status=run_status,
            blocked_reason=blocked_reason,
        )
        out_path = write_result_artifact(run_dir, artifact)
        print(f"Interrupted. Partial artifact written: {out_path}", file=sys.stderr)
        raise

    completed_at = datetime.now(timezone.utc)
    cleanup_counts = count_cleanup_tables(dsn)
    artifact = build_final_artifact(
        adapter=adapter,
        source_fixture_path=FIXTURE_PATH,
        source_fixture_sha256=fixture_sha256,
        case_count=case_count,
        predictions=predictions,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        requested_model=openai_settings["requested_model"],
        reasoning_effort=reasoning_effort,
        provider_timing_config=provider_timing_config,
        database_info={"host": dsn_info.host, "dbname": dsn_info.dbname, "cleanup": cleanup_counts},
        run_status=run_status,
        blocked_reason=blocked_reason,
    )
    out_path = write_result_artifact(run_dir, artifact)

    print(f"run_status:            {run_status}")
    print(f"case_count:            {case_count}")
    print(f"completed_case_count:  {len(predictions)}")
    print(f"error_case_count:      {artifact['error_case_count']}")
    print(f"total_call_count:      {artifact['totals']['total_call_count']}")
    print(f"total_cost_usd:        {artifact['totals']['total_cost_usd']}")
    print(f"database_cleanup:      {cleanup_counts}")
    print(f"result artifact:       {out_path}")

    if run_status == "blocked":
        return 3
    return 1 if artifact["error_case_count"] else 0


def main(argv: Optional[List[str]] = None) -> int:
    if _running_under_pytest():
        print(
            "Refusing to run under pytest (this script makes real network/database "
            "calls when not in --dry-run mode); import and call individual "
            "functions directly in tests instead.",
            file=sys.stderr,
        )
        return 2

    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.dry_run:
            return _run_dry_run(args)
        return _run_live(args)
    except BaselineRunnerRefusal as exc:
        print(f"Refusing to run: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - includes V48DisposableDsnRejectedError etc.
        print(f"Refusing to run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
