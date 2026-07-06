"""
Dual-engine benchmark execution readiness (V58-QUALITY-04A).

Separates the quality-benchmark workflow into two independent stages:

1. Prediction generation — run an engine adapter against each case in a
   benchmark fixture's ``question``/``resource_snapshot`` content and save
   raw, fully-provenanced predictions keyed by ``case_id``. No scoring
   happens in this stage, and no ground-truth label is required or
   consumed (a fixture only needs to pass the base structural schema in
   ``workers.quality_benchmark.load_benchmark_fixture``).

2. Scoring — load a *finalized* SME-reviewed fixture (see
   ``assert_finalized_sme_ground_truth`` below) and a compatible prediction
   artifact, verify exact 1:1 case coverage, and compute a deterministic
   scorecard using the existing benchmark metrics
   (``workers.quality_benchmark.compute_benchmark_metrics``) plus the
   additional metrics required for engine comparison.

Ground-truth safety gate
------------------------
Scoring fails closed: ``workers/fixtures/quality_benchmark_v1.json`` (the
AI-drafted fixture) and any fixture missing a genuine, complete SME
adjudication are rejected outright by ``assert_finalized_sme_ground_truth``.
Only a fixture produced by
``workers.benchmark_sme_review.build_reviewed_fixture`` — i.e. one with
``sme_reviewed=true``, ``sme_review_status="complete"``, a non-blank
``sme_reviewer_id``, a recorded ``source_fixture_sha256`` and
``review_imported_at_utc``, no rejected cases, no unresolved
needs-second-review cases, and a resolved SME label on every case — can be
scored as ground truth.

Engine adapters
----------------
``LegacyEngineAdapter`` reuses the exact pure engine entry points already
used by ``workers.audit_calibration`` and ``workers.job_handlers`` for the
legacy hybrid path — ``run_deterministic_checks``, an injected LLM
provider callable, ``validate_llm_response``, and ``merge_findings`` — none
of which require a database-backed target. When no provider is injected
(the default, non-live posture) predictions are deterministic-only and
clearly flagged as such; this is not an error. When a provider *is*
injected but a case has no ``user_prompt``, that case's LLM step reports a
per-case execution error rather than silently skipping.

``V48EngineAdapter`` is architecturally BLOCKED for real prediction
generation in this task. The only complete V48 engine entry point,
``workers.ai_quality_audit_worker.process_ai_quality_audit_job``, requires
a live Supabase client and a pre-existing ``question_versions`` row (its
pass-sequencing, dispute-trigger, and substitution logic is driven entirely
by ``claim_ai_quality_audit_pass_v1``/``get_question_version_blind_context_v1``/
``get_question_version_comparison_context_v1`` and related RPCs keyed by
``question_version_id``). Benchmark cases are in-memory JSON fixtures with
no corresponding database row, so running them through the real V48 engine
would require either inserting benchmark content into production tables
(forbidden) or reimplementing the pass-orchestration/dispute state machine
inside the benchmark harness (duplicated audit logic, also disallowed).
See ``V48EngineAdapter.UNAVAILABLE_REASON`` / ``FOLLOW_UP`` for the precise
incompatibility and the smallest safe follow-up.

No database writes, migrations, candidate publishing, or live AI calls
occur anywhere in this module.

Configuration identity is generated, not asserted (V58-QUALITY-04E-R2)
------------------------------------------------------------------------
Every prediction artifact produced by ``generate_predictions`` carries a
``configuration_identity`` dict recording the *actual* engine/provider/
model/prompt/ruleset/evidence configuration used to generate it, plus the
source fixture's hash - all derived from the adapter's own
``describe_config()`` at the moment predictions were generated, never
supplied separately at scoring time. ``score_predictions`` re-validates
this identity is complete and internally consistent (cross-checked against
the artifact's independently-recorded top-level fields and raw
``provider_config``) before copying it, unchanged, into the scorecard.
There is no scoring-time mechanism (CLI flag or otherwise) to supply,
override, or relabel this identity - the only way to change what a
scorecard says was tested is to generate new predictions with a different
engine/adapter configuration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from workers.deterministic_audit import run_deterministic_checks
from workers.finding_merge import merge_findings
from workers.finding_policy import ALLOWED_MATERIALITY, MATERIALITY_RANK
from workers.llm_audit import AUDIT_RESPONSE_SCHEMA, LlmAuditValidationError, validate_llm_response
from workers.quality_benchmark import (
    BenchmarkMetrics,
    compute_benchmark_metrics,
    load_benchmark_fixture,
)
# Reuses the canonical per-case detection/false-approval/false-rejection
# algorithm rather than reimplementing it; kept as an explicit import (not
# re-exported) since it is a private helper of workers.quality_benchmark.
from workers.quality_benchmark import _build_case_result

PREDICTION_ARTIFACT_SCHEMA_VERSION = "quality-benchmark-prediction-v1"
SCORECARD_SCHEMA_VERSION = "quality-benchmark-scorecard-v1"

ENGINE_LEGACY = "legacy"
ENGINE_V48 = "v48"

DEFAULT_LEGACY_RULESET_VERSION = "1.0.0"
DEFAULT_LEGACY_MODEL_ID = "claude-sonnet-4-6"
DEFAULT_LEGACY_SYSTEM_PROMPT = "You are a CertBound certification question auditor."
DEFAULT_LEGACY_PROMPT_VERSION = "legacy-system-prompt-v1"
DEFAULT_LEGACY_EVIDENCE_CONFIG_ID = "legacy-evidence-v1"

# Explicit, non-inferred markers for a deterministic-only run (V58-QUALITY-
# 04E-R2 correction 6): when no LLM provider is injected, these are facts
# about what actually happened (no provider/model/prompt was used), not
# guesses - the deterministic-only adapter code path always sets exactly
# these values itself; nothing downstream ever invents them.
DETERMINISTIC_ONLY_PROVIDER_ID = "deterministic-only"
NOT_APPLICABLE_IDENTITY_VALUE = "not-applicable"

# The complete set of dimensions that must be explicit, non-blank strings in
# a prediction artifact's ``configuration_identity`` (and, downstream, a
# scorecard's copy of it) before scoring/classification may proceed. A
# genuinely not-applicable dimension must still be an explicit sentinel
# string (e.g. NOT_APPLICABLE_IDENTITY_VALUE) - never null, never omitted.
REQUIRED_CONFIGURATION_IDENTITY_FIELDS = (
    "engine_id",
    "engine_version",
    "provider_id",
    "model_id",
    "prompt_version",
    "ruleset_version",
    "evidence_config_id",
    "source_fixture_sha256",
)


# ===========================================================================
# Errors
# ===========================================================================


class QualityBenchmarkExecutionError(ValueError):
    """Base error for the benchmark execution layer."""


class GroundTruthNotFinalizedError(QualityBenchmarkExecutionError):
    """Raised when a fixture is not usable as finalized SME ground truth."""


class PredictionArtifactError(QualityBenchmarkExecutionError):
    """Raised when a prediction artifact is malformed or fails coverage checks."""


class EngineAdapterUnavailableError(RuntimeError):
    """Raised when an engine adapter cannot safely generate real predictions.

    Carries a precise ``reason`` (the exact incompatibility) and a
    ``follow_up`` (the smallest safe change that would unblock it), so
    callers can report BLOCKED status without fabricating predictions.
    """

    def __init__(self, reason: str, follow_up: str):
        super().__init__(reason)
        self.reason = reason
        self.follow_up = follow_up


# ===========================================================================
# Ground-truth safety gate
# ===========================================================================


def assert_finalized_sme_ground_truth(fixture: Mapping[str, Any]) -> None:
    """Raise ``GroundTruthNotFinalizedError`` unless *fixture* is genuine,
    finalized SME ground truth produced by
    ``workers.benchmark_sme_review.build_reviewed_fixture``.

    This function performs no I/O; callers should first load the fixture
    with ``workers.quality_benchmark.load_benchmark_fixture`` (structural
    schema validation) before calling this gate.
    """
    errors: List[str] = []

    if fixture.get("sme_reviewed") is not True:
        errors.append("sme_reviewed must be true")
    if fixture.get("sme_review_status") != "complete":
        errors.append(
            f"sme_review_status must be 'complete', got {fixture.get('sme_review_status')!r}"
        )

    reviewer_id = fixture.get("sme_reviewer_id")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        errors.append("sme_reviewer_id must be a non-blank string")

    source_hash = fixture.get("source_fixture_sha256")
    if not isinstance(source_hash, str) or not source_hash.strip():
        errors.append("source_fixture_sha256 must be present")

    imported_at = fixture.get("review_imported_at_utc")
    if not isinstance(imported_at, str) or not imported_at.strip():
        errors.append("review_imported_at_utc must be present")

    summary = fixture.get("sme_review_summary")
    if not isinstance(summary, dict):
        errors.append("sme_review_summary must be present")
        summary = {}
    rejected = summary.get("rejected_case_ids") or []
    if rejected:
        errors.append(
            f"fixture has {len(rejected)} rejected case(s) still present: {sorted(rejected)}"
        )
    unresolved = summary.get("unresolved_second_review_case_ids") or []
    if unresolved:
        errors.append(
            f"fixture has {len(unresolved)} unresolved needs_second_review case(s): "
            f"{sorted(unresolved)}"
        )

    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("fixture has no cases")
    else:
        for case in cases:
            case_id = case.get("case_id", "<unknown>")
            review = case.get("sme_review")
            if not isinstance(review, dict):
                errors.append(f"case {case_id!r} has no sme_review record")
                continue
            decision = review.get("decision")
            if decision not in ("approve", "correct_label"):
                errors.append(
                    f"case {case_id!r} sme_review.decision={decision!r} is not a "
                    "finalized decision (expected 'approve' or 'correct_label')"
                )
            if "ai_drafted_reviewer_label" not in case:
                errors.append(
                    f"case {case_id!r} is missing ai_drafted_reviewer_label provenance; "
                    "the effective SME label may not have been resolved"
                )
            for required_field in (
                "expected_correct_option_labels",
                "expected_finding_codes",
                "known_good",
                "reviewer_label",
            ):
                if required_field not in case:
                    errors.append(
                        f"case {case_id!r} is missing effective label field {required_field!r}"
                    )

    if errors:
        raise GroundTruthNotFinalizedError(
            "fixture is not usable as finalized SME ground truth: " + "; ".join(errors)
        )


def load_benchmark_case_fixture(path: Path | str) -> dict:
    """Load a benchmark fixture for prediction generation.

    Only the base structural schema is enforced (via
    ``workers.quality_benchmark.load_benchmark_fixture``); no SME-review
    finalization is required. Predictions may legitimately be generated
    against an AI-drafted fixture (e.g. ahead of SME review) or a finalized
    SME-reviewed fixture — both expose the same ``question`` /
    ``resource_snapshot`` case content.
    """
    return load_benchmark_fixture(path)


def load_finalized_sme_ground_truth_fixture(path: Path | str) -> dict:
    """Load a fixture and assert it is finalized SME ground truth.

    This is the *only* sanctioned way to obtain a fixture for scoring.
    """
    fixture = load_benchmark_fixture(path)
    assert_finalized_sme_ground_truth(fixture)
    return fixture


# ===========================================================================
# Shared helpers
# ===========================================================================


def _utc_now_iso8601() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path | str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        raise QualityBenchmarkExecutionError(f"file not found for hashing: {file_path}")
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def _nonblank_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _assert_complete_configuration_identity(identity: Mapping[str, Any], *, context: str) -> None:
    """Raise ``PredictionArtifactError`` unless every dimension in
    ``REQUIRED_CONFIGURATION_IDENTITY_FIELDS`` is an explicit, non-blank
    string. Never fills in a missing/blank dimension - a genuinely
    not-applicable dimension must already be an explicit caller-supplied
    sentinel (see ``NOT_APPLICABLE_IDENTITY_VALUE`` /
    ``DETERMINISTIC_ONLY_PROVIDER_ID``).
    """
    missing = [field for field in REQUIRED_CONFIGURATION_IDENTITY_FIELDS if not _nonblank_str(identity.get(field))]
    if missing:
        raise PredictionArtifactError(
            f"{context}: configuration identity is incomplete; missing or blank field(s): {missing} "
            "(a not-applicable dimension must be an explicit sentinel such as "
            f"{NOT_APPLICABLE_IDENTITY_VALUE!r} or {DETERMINISTIC_ONLY_PROVIDER_ID!r} - it is never inferred)"
        )


def _summarize_findings(findings: Sequence[Mapping[str, Any]]) -> tuple[List[str], Optional[str], bool]:
    """Return (finding_codes, overall_materiality, approved) for *findings*."""
    codes = [str(f.get("finding_code")) for f in findings if f.get("finding_code")]
    materialities = [
        f.get("materiality") for f in findings if f.get("materiality") in ALLOWED_MATERIALITY
    ]
    overall = max(materialities, key=lambda m: MATERIALITY_RANK[m]) if materialities else None
    approved = overall != "blocking"
    return codes, overall, approved


# ===========================================================================
# Prediction data model
# ===========================================================================


@dataclass
class CasePrediction:
    """One engine's raw prediction for one benchmark case."""

    case_id: str
    finding_codes: List[str] = field(default_factory=list)
    materiality: Optional[str] = None
    approved: bool = True
    raw_output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _prediction_from_findings(
    case_id: str,
    findings: Sequence[Mapping[str, Any]],
    *,
    raw_output_extra: Optional[Mapping[str, Any]] = None,
    error: Optional[str] = None,
) -> CasePrediction:
    codes, materiality, approved = _summarize_findings(findings)
    raw_output: Dict[str, Any] = {"findings": list(findings)}
    if raw_output_extra:
        raw_output.update(raw_output_extra)
    return CasePrediction(
        case_id=case_id,
        finding_codes=codes,
        materiality=materiality,
        approved=approved,
        raw_output=raw_output,
        error=error,
    )


# ===========================================================================
# Engine adapters
# ===========================================================================


class LegacyEngineAdapter:
    """Real (non-mock) legacy engine adapter: deterministic + LLM + merge.

    Reuses ``run_deterministic_checks``, ``validate_llm_response``, and
    ``merge_findings`` exactly as ``workers.audit_calibration`` and
    ``workers.job_handlers.make_hybrid_audit_handler`` do — no audit logic
    is reimplemented, and no database access is required.

    The deterministic stage always runs. The LLM stage only runs when an
    ``llm_provider`` callable is injected (matching the structural
    interface in ``workers.llm_providers``); this keeps the default,
    non-live posture safe (no network access) while still letting callers
    (and tests) exercise the full path with a fake/stub provider.
    """

    engine_id = ENGINE_LEGACY

    def __init__(
        self,
        *,
        ruleset_version: str = DEFAULT_LEGACY_RULESET_VERSION,
        llm_provider: Optional[Callable[..., Any]] = None,
        model_id: str = DEFAULT_LEGACY_MODEL_ID,
        system_prompt: str = DEFAULT_LEGACY_SYSTEM_PROMPT,
        prompt_version: str = DEFAULT_LEGACY_PROMPT_VERSION,
        provider_id: Optional[str] = None,
        evidence_config_id: str = DEFAULT_LEGACY_EVIDENCE_CONFIG_ID,
    ) -> None:
        self._ruleset_version = ruleset_version
        self._provider = llm_provider
        self._model_id = model_id
        self._system_prompt = system_prompt
        self._prompt_version = prompt_version
        # Deliberately not defaulted when a provider IS injected (live mode)
        # - see describe_config(): a live run's provider identity must be
        # supplied explicitly by whoever wires the real provider, never
        # guessed by this adapter.
        self._provider_id = provider_id
        self._evidence_config_id = evidence_config_id

    def describe_config(self) -> Dict[str, Any]:
        live = self._provider is not None
        if live:
            if not _nonblank_str(self._provider_id):
                raise QualityBenchmarkExecutionError(
                    "LegacyEngineAdapter(llm_provider=...) requires an explicit non-blank provider_id "
                    "for live execution - it is never inferred from the injected callable"
                )
            provider_id = self._provider_id
            model_id = self._model_id
            prompt_version = self._prompt_version
        else:
            # Deterministic-only posture: these are facts about what
            # actually ran (no provider/model/prompt was used), not
            # inferred placeholders.
            provider_id = DETERMINISTIC_ONLY_PROVIDER_ID
            model_id = NOT_APPLICABLE_IDENTITY_VALUE
            prompt_version = NOT_APPLICABLE_IDENTITY_VALUE
        return {
            "engine_id": self.engine_id,
            "engine_version": "legacy-deterministic+llm-v1",
            "provider_id": provider_id,
            "model_id": model_id,
            "prompt_version": prompt_version,
            "ruleset_version": self._ruleset_version,
            "evidence_config_id": self._evidence_config_id,
            "live": live,
        }

    def generate_prediction(self, case: Mapping[str, Any]) -> CasePrediction:
        case_id = str(case["case_id"])
        question = case["question"]
        ruleset = case.get("ruleset_version") or self._ruleset_version

        det_findings = run_deterministic_checks(question, ruleset)

        if self._provider is None:
            # Default, non-live posture: deterministic-only prediction.
            # Not an error — no live AI call was requested.
            return _prediction_from_findings(
                case_id,
                merge_findings(det_findings, []),
                raw_output_extra={"llm_skipped": True, "llm_skipped_reason": "no provider configured"},
            )

        user_prompt = case.get("user_prompt")
        if not user_prompt:
            return _prediction_from_findings(
                case_id,
                merge_findings(det_findings, []),
                raw_output_extra={"llm_skipped": True},
                error=(
                    "live LLM prediction requested but case has no 'user_prompt' field; "
                    "the current benchmark fixture schema does not yet include prompt "
                    "content required for live legacy-engine execution"
                ),
            )

        try:
            response = self._provider(
                model_name=case.get("model_name") or self._model_id,
                system_prompt=case.get("system_prompt") or self._system_prompt,
                user_prompt=user_prompt,
                response_schema=AUDIT_RESPONSE_SCHEMA,
                metadata={"case_id": case_id, "benchmark_execution": True},
            )
            llm_findings = validate_llm_response(response.parsed_response)
        except LlmAuditValidationError as exc:
            return _prediction_from_findings(
                case_id,
                merge_findings(det_findings, []),
                raw_output_extra={"llm_skipped": False},
                error=f"LlmAuditValidationError: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - provider failures must not be silently dropped
            return _prediction_from_findings(
                case_id,
                merge_findings(det_findings, []),
                raw_output_extra={"llm_skipped": False},
                error=f"{type(exc).__name__}: {exc}",
            )

        merged = merge_findings(det_findings, llm_findings)
        return _prediction_from_findings(
            case_id,
            merged,
            raw_output_extra={
                "llm_skipped": False,
                "input_tokens": getattr(response, "input_tokens", None),
                "output_tokens": getattr(response, "output_tokens", None),
                "actual_cost_usd": getattr(response, "actual_cost_usd", None),
                "provider_request_id": getattr(response, "provider_request_id", None),
            },
        )


class V48EngineAdapter:
    """V48 grounded engine adapter.

    Safe by default: with no arguments, this behaves exactly as it did
    before V58-QUALITY-04C — ``generate_prediction`` always raises
    ``EngineAdapterUnavailableError`` (see ``UNAVAILABLE_REASON``/
    ``FOLLOW_UP``) rather than fabricating a prediction or silently falling
    back to mock data.

    V58-QUALITY-04C adds one narrow, explicit-opt-in exception: when *all*
    of ``allow_disposable_db=True``, a non-empty ``disposable_db_url``, and
    an explicitly injected ``providers`` are supplied, ``generate_prediction``
    instead runs the real, unmodified V48 worker/RPC pipeline against that
    disposable database via
    ``workers.quality_benchmark_v48_orchestration.generate_v48_prediction``
    (imported lazily to avoid a module import cycle — mirrors the existing
    lazy-import pattern already used for
    ``workers.ai_quality_audit_worker`` in ``workers/job_handlers.py``).
    Every disposable-database safety check (DSN validation, schema/RPC
    compatibility, guaranteed-rollback transaction) lives entirely in that
    module; nothing here duplicates it. No provider is ever constructed
    implicitly from environment variables — callers (currently only
    Docker-gated tests) must inject ``providers`` explicitly.
    """

    engine_id = ENGINE_V48

    def __init__(
        self,
        *,
        allow_disposable_db: bool = False,
        disposable_db_url: Optional[str] = None,
        providers: Optional[Any] = None,
        worker_id: str = "v58-quality-04c-benchmark",
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        ruleset_version: Optional[str] = None,
        evidence_config_id: Optional[str] = None,
    ) -> None:
        self._allow_disposable_db = allow_disposable_db
        self._disposable_db_url = disposable_db_url
        self._providers = providers
        self._worker_id = worker_id
        # Required (non-blank) before an opted-in artifact can be built by
        # generate_predictions() - see describe_config(). Never defaulted:
        # the disposable-db path is real, injected-provider execution, so
        # there is no "deterministic-only" fallback identity available.
        self._provider_id = provider_id
        self._model_id = model_id
        self._prompt_version = prompt_version
        self._ruleset_version = ruleset_version
        self._evidence_config_id = evidence_config_id

    def _is_opted_in(self) -> bool:
        return bool(
            self._allow_disposable_db
            and self._disposable_db_url
            and self._providers is not None
        )

    UNAVAILABLE_REASON = (
        "The V48 grounded engine's only complete entry point, "
        "workers.ai_quality_audit_worker.process_ai_quality_audit_job, requires a live "
        "Supabase client and a pre-existing question_versions row: pass sequencing is "
        "driven by claim_ai_quality_audit_pass_v1, blind/comparison context is fetched via "
        "get_question_version_blind_context_v1 / get_question_version_comparison_context_v1 "
        "(workers/ai_quality_audit_context.py), and dispute/substitution/completion-shape "
        "decisions (workers/ai_quality_audit_worker.py: _execute_pass_a/_execute_pass_b/"
        "_execute_pass_c/_detect_completion_shape) are all keyed by audit_run_id/"
        "question_version_id rows persisted in the database. Benchmark cases are in-memory "
        "JSON fixtures with no corresponding question_version_id, so they cannot be run "
        "through the real V48 engine without either inserting benchmark content into "
        "production tables (forbidden by this task's safety constraints) or reimplementing "
        "the pass-orchestration/dispute state machine inside the benchmark harness "
        "(duplicated audit logic, also disallowed)."
    )

    FOLLOW_UP = (
        "Smallest safe follow-up: have the V48 engine owners add a sanctioned, "
        "storage-agnostic context seam to workers/ai_quality_audit_context.py — e.g. an "
        "in-memory context provider that returns the exact same normalized shape as "
        "load_blind_audit_context / load_comparison_audit_context, backed by a frozen "
        "benchmark-case snapshot instead of Supabase RPCs — so "
        "process_ai_quality_audit_job's pass-sequencing, dispute-trigger, and "
        "substitution logic can run completely unchanged against benchmark content. "
        "This decouples only the storage layer and touches zero audit-decision logic."
    )

    def describe_config(self) -> Dict[str, Any]:
        if not self._is_opted_in():
            return {
                "engine_id": self.engine_id,
                "status": "blocked",
                "reason": self.UNAVAILABLE_REASON,
                "follow_up": self.FOLLOW_UP,
            }
        missing = [
            name
            for name, value in (
                ("provider_id", self._provider_id),
                ("model_id", self._model_id),
                ("prompt_version", self._prompt_version),
                ("ruleset_version", self._ruleset_version),
                ("evidence_config_id", self._evidence_config_id),
            )
            if not _nonblank_str(value)
        ]
        if missing:
            raise QualityBenchmarkExecutionError(
                "V48EngineAdapter disposable-database execution requires explicit non-blank "
                f"configuration identity for prediction-artifact generation; missing or blank: {missing} "
                "(never inferred - pass these explicitly to the constructor)"
            )
        # Deliberately never includes the DSN (may carry credentials) —
        # only a boolean opt-in flag and the worker id are ever recorded in
        # provenance/artifacts.
        return {
            "engine_id": self.engine_id,
            "engine_version": "v48-disposable-db-v1",
            "status": "disposable_db_live",
            "live": True,
            "worker_id": self._worker_id,
            "provider_id": self._provider_id,
            "model_id": self._model_id,
            "prompt_version": self._prompt_version,
            "ruleset_version": self._ruleset_version,
            "evidence_config_id": self._evidence_config_id,
        }

    def generate_prediction(self, case: Mapping[str, Any]) -> CasePrediction:
        if not self._is_opted_in():
            raise EngineAdapterUnavailableError(self.UNAVAILABLE_REASON, self.FOLLOW_UP)
        # Lazy import: workers.quality_benchmark_v48_orchestration imports
        # CasePrediction/EngineAdapterUnavailableError from this module, so
        # importing it eagerly at module scope here would be circular.
        from workers.quality_benchmark_v48_orchestration import (  # noqa: PLC0415
            generate_v48_prediction,
        )

        return generate_v48_prediction(
            case,
            dsn=self._disposable_db_url,
            allow_disposable_v48_db=self._allow_disposable_db,
            providers=self._providers,
            worker_id=self._worker_id,
        )


ENGINE_ADAPTERS = {
    ENGINE_LEGACY: LegacyEngineAdapter,
    ENGINE_V48: V48EngineAdapter,
}


# ===========================================================================
# Stage 1: prediction generation
# ===========================================================================


def generate_predictions(
    fixture: Mapping[str, Any],
    adapter: Any,
    *,
    source_fixture_path: Path | str,
) -> Dict[str, Any]:
    """Run *adapter* against every case in *fixture* and build a prediction
    artifact. Does not score. Propagates ``EngineAdapterUnavailableError``
    immediately (the whole engine is unavailable, not just one case); any
    other per-case exception is caught and recorded as that case's
    ``error`` so no case is silently dropped.

    Builds and validates ``configuration_identity`` (V58-QUALITY-04E-R2)
    from ``adapter.describe_config()`` - the single, authoritative source
    of what engine/provider/model/prompt/ruleset/evidence configuration was
    actually used. Raises ``PredictionArtifactError`` if any required
    identity dimension is missing or blank, so an incomplete-identity
    artifact is never written to disk in the first place.
    """
    predictions: List[Dict[str, Any]] = []
    error_case_count = 0

    for case in fixture["cases"]:
        case_id = str(case["case_id"])
        try:
            prediction = adapter.generate_prediction(case)
        except EngineAdapterUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - never silently drop a case
            prediction = CasePrediction(
                case_id=case_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        if prediction.case_id != case_id:
            prediction.case_id = case_id
        predictions.append(prediction.to_dict())
        if prediction.error:
            error_case_count += 1

    config = adapter.describe_config()
    source_fixture_sha256 = _sha256_file(source_fixture_path)
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
    _assert_complete_configuration_identity(configuration_identity, context="prediction generation")

    return {
        "schema_version": PREDICTION_ARTIFACT_SCHEMA_VERSION,
        "engine_id": config.get("engine_id"),
        "engine_version": config.get("engine_version"),
        "configuration_identity": configuration_identity,
        "provider_config": config,
        "generated_at_utc": _utc_now_iso8601(),
        "source_fixture_path": str(source_fixture_path),
        "source_fixture_sha256": source_fixture_sha256,
        "case_count": len(fixture["cases"]),
        "predictions": predictions,
        "error_case_count": error_case_count,
    }


def write_prediction_artifact(
    path: Path | str,
    artifact: Mapping[str, Any],
    *,
    allow_overwrite: bool = False,
) -> None:
    output_path = Path(path)
    if output_path.exists() and not allow_overwrite:
        raise PredictionArtifactError(f"refusing to overwrite existing prediction artifact: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_prediction_artifact(path: Path | str) -> Dict[str, Any]:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise PredictionArtifactError(f"prediction artifact not found: {artifact_path}")
    with artifact_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PredictionArtifactError("prediction artifact root must be a JSON object")
    if data.get("schema_version") != PREDICTION_ARTIFACT_SCHEMA_VERSION:
        raise PredictionArtifactError(
            f"unsupported prediction artifact schema_version={data.get('schema_version')!r}; "
            f"expected {PREDICTION_ARTIFACT_SCHEMA_VERSION!r}"
        )
    if not isinstance(data.get("predictions"), list):
        raise PredictionArtifactError("prediction artifact 'predictions' must be a JSON array")
    return data


# ===========================================================================
# Stage 2: scoring
# ===========================================================================


def validate_prediction_coverage(
    fixture: Mapping[str, Any], artifact: Mapping[str, Any]
) -> Dict[str, Any]:
    """Verify every case in *fixture* has exactly one prediction, and every
    prediction corresponds to a known case. Raises ``PredictionArtifactError``
    on any missing, duplicate, or unknown case_id.

    Predictions are represented as a JSON *array* (not an object keyed by
    case_id) specifically so a duplicate case_id cannot be silently
    collapsed by JSON parsing before this check ever runs.
    """
    predictions = artifact.get("predictions")
    if not isinstance(predictions, list):
        raise PredictionArtifactError("prediction artifact 'predictions' must be a JSON array")

    expected_ids = [str(case["case_id"]) for case in fixture["cases"]]
    expected_id_set = set(expected_ids)

    seen: List[str] = []
    duplicates: set = set()
    unknown: set = set()
    for index, entry in enumerate(predictions):
        if not isinstance(entry, dict) or not entry.get("case_id"):
            raise PredictionArtifactError(
                f"predictions[{index}] must be a JSON object with a non-empty 'case_id'"
            )
        case_id = str(entry["case_id"])
        if case_id in seen:
            duplicates.add(case_id)
        seen.append(case_id)
        if case_id not in expected_id_set:
            unknown.add(case_id)

    missing = expected_id_set - set(seen)
    if missing or duplicates or unknown:
        raise PredictionArtifactError(
            "prediction artifact case coverage mismatch: "
            f"missing={sorted(missing)}, duplicate={sorted(duplicates)}, unknown={sorted(unknown)}"
        )

    return {"expected_case_count": len(expected_ids), "predicted_case_count": len(seen)}


def _extract_and_verify_configuration_identity(artifact: Mapping[str, Any]) -> Dict[str, str]:
    """Return the prediction artifact's authoritative ``configuration_identity``,
    after verifying it is complete and internally consistent.

    This is the sole boundary where scoring accepts configuration identity
    (V58-QUALITY-04E-R2): it is never accepted as a separate scoring-time
    argument. Raises ``PredictionArtifactError`` if the identity is
    incomplete, or if it disagrees with any of the other places the same
    facts are independently recorded in the artifact (the legacy top-level
    ``engine_id``/``engine_version``/``source_fixture_sha256`` fields, and
    the raw ``provider_config`` the identity was derived from) - such a
    disagreement means the artifact was hand-edited inconsistently after
    generation and must not be trusted.
    """
    identity = artifact.get("configuration_identity")
    if not isinstance(identity, Mapping):
        raise PredictionArtifactError(
            "prediction artifact is missing 'configuration_identity'; scoring refuses to proceed "
            "without complete, explicit engine-configuration identity recorded at prediction-generation "
            "time (see workers.quality_benchmark_execution.generate_predictions)"
        )
    normalized: Dict[str, str] = {
        field: identity.get(field) for field in REQUIRED_CONFIGURATION_IDENTITY_FIELDS
    }
    _assert_complete_configuration_identity(normalized, context="scoring")

    mismatches: List[str] = []
    top_level_fields = {
        "engine_id": artifact.get("engine_id"),
        "engine_version": artifact.get("engine_version"),
        "source_fixture_sha256": artifact.get("source_fixture_sha256"),
    }
    for field, top_level_value in top_level_fields.items():
        if top_level_value != normalized[field]:
            mismatches.append(
                f"{field}: top-level={top_level_value!r} vs configuration_identity={normalized[field]!r}"
            )

    provider_config = artifact.get("provider_config")
    if isinstance(provider_config, Mapping):
        for field in REQUIRED_CONFIGURATION_IDENTITY_FIELDS:
            if field in provider_config and provider_config[field] != normalized[field]:
                mismatches.append(
                    f"{field}: provider_config={provider_config[field]!r} vs "
                    f"configuration_identity={normalized[field]!r}"
                )

    if mismatches:
        raise PredictionArtifactError(
            "prediction artifact configuration identity is internally inconsistent (possible "
            f"post-generation tampering); mismatches: {mismatches}"
        )

    return normalized


def _finding_category_metrics(
    cases: Sequence[Mapping[str, Any]],
    findings_by_case_id: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """Precision and recall broken out per canonical finding code.

    A "true positive" for code C is a defective case whose
    expected_finding_codes includes C and whose predicted findings also
    include C. A "false positive" is any case (known-good or defective)
    where C was predicted but not expected. "false_negatives" is the
    complement of true_positives within expected_total (cases that expected
    C but did not get it); "n" is expected_total, i.e. the ground-truth
    sample size this code's recall is computed over (V58-QUALITY-04E —
    exposed so tier-aware acceptance policies can flag small-n codes as
    diagnostic-only without recomputing anything).
    """
    all_codes: set = set()
    for case in cases:
        all_codes.update(case.get("expected_finding_codes") or [])
    for findings in findings_by_case_id.values():
        all_codes.update(str(f.get("finding_code")) for f in findings if f.get("finding_code"))

    per_code: Dict[str, Dict[str, Any]] = {}
    for code in sorted(all_codes):
        true_positives = 0
        false_positives = 0
        expected_total = 0
        for case in cases:
            case_id = str(case["case_id"])
            predicted_codes = {
                str(f.get("finding_code"))
                for f in findings_by_case_id.get(case_id, [])
                if f.get("finding_code")
            }
            expected_codes = set(case.get("expected_finding_codes") or [])
            expects_code = code in expected_codes
            predicts_code = code in predicted_codes
            if expects_code:
                expected_total += 1
                if predicts_code:
                    true_positives += 1
            elif predicts_code:
                false_positives += 1

        predicted_total = true_positives + false_positives
        precision = round(true_positives / predicted_total, 6) if predicted_total else None
        recall = round(true_positives / expected_total, 6) if expected_total else None
        per_code[code] = {
            "true_positives": true_positives,
            "false_positives": false_positives,
            "false_negatives": expected_total - true_positives,
            "expected_total": expected_total,
            "predicted_total": predicted_total,
            "n": expected_total,
            "precision": precision,
            "recall": recall,
        }
    return per_code


def score_predictions(
    fixture: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    """Score a prediction artifact against a finalized SME-reviewed fixture.

    Fails closed: re-asserts ``assert_finalized_sme_ground_truth`` even if
    the caller already used ``load_finalized_sme_ground_truth_fixture``, so
    this function is safe to call directly. Also fails closed on
    configuration identity (V58-QUALITY-04E-R2): the artifact's
    ``configuration_identity`` must be complete and internally consistent
    (see ``_extract_and_verify_configuration_identity``); it is copied,
    unchanged, into the returned scorecard. There is no parameter on this
    function through which a caller can supply or override identity.
    """
    assert_finalized_sme_ground_truth(fixture)
    coverage = validate_prediction_coverage(fixture, artifact)
    configuration_identity = _extract_and_verify_configuration_identity(artifact)

    predictions_by_id = {str(p["case_id"]): p for p in artifact["predictions"]}

    scored_cases: List[Mapping[str, Any]] = []
    case_results = []
    unscored_case_ids: List[str] = []
    unscored_blocking_case_ids: List[str] = []
    findings_by_case_id: Dict[str, Sequence[Mapping[str, Any]]] = {}

    for case in fixture["cases"]:
        case_id = str(case["case_id"])
        prediction = predictions_by_id[case_id]
        if prediction.get("error"):
            unscored_case_ids.append(case_id)
            # A blocking-labeled case that could not be scored is tracked
            # separately (V58-QUALITY-04E): it must never silently vanish
            # from the acceptance-policy denominator the way it vanishes
            # from the rate-based metrics below (see
            # docs/V58_BENCHMARK_ACCEPTANCE_POLICY.md).
            if case.get("expected_materiality") == "blocking":
                unscored_blocking_case_ids.append(case_id)
            continue
        findings = (prediction.get("raw_output") or {}).get("findings") or []
        findings_by_case_id[case_id] = findings
        scored_cases.append(case)
        case_results.append(_build_case_result(case, engine=str(artifact.get("engine_id", "unknown")), findings=findings))

    metrics: BenchmarkMetrics = (
        compute_benchmark_metrics(scored_cases, case_results) if scored_cases else BenchmarkMetrics()
    )
    metrics_dict = asdict(metrics)
    for _category, _bucket in metrics_dict.get("recall_by_defect_category", {}).items():
        _bucket["n"] = _bucket.get("total", 0)
        _bucket["false_negatives"] = _bucket.get("total", 0) - _bucket.get("detected", 0)

    known_good_cases = metrics.known_good_cases
    defective_cases = metrics.defective_cases
    known_good_approval_rate = (
        round((known_good_cases - metrics.false_rejections) / known_good_cases, 6)
        if known_good_cases
        else None
    )
    defective_case_rejection_rate = (
        round((defective_cases - metrics.false_approvals) / defective_cases, 6)
        if defective_cases
        else None
    )

    # Per-case identifiers required for mechanical acceptance-policy
    # classification (V58-QUALITY-04E) — derived straight from the same
    # case_results the aggregate metrics above are computed from, so these
    # can never disagree with the counts in ``metrics``.
    false_approval_case_ids = sorted(result.case_id for result in case_results if result.false_approval)
    blocking_false_approval_case_ids = sorted(
        result.case_id
        for result in case_results
        if result.false_approval and result.expected_materiality == "blocking"
    )
    false_rejection_case_ids = sorted(result.case_id for result in case_results if result.false_rejection)

    warning_results = [
        result
        for result in case_results
        if not result.known_good and result.expected_materiality == "warning"
    ]
    warning_recall_detected = sum(1 for result in warning_results if result.detection_success)
    warning_recall_total = len(warning_results)
    warning_recall = (
        round(warning_recall_detected / warning_recall_total, 6) if warning_recall_total else None
    )

    source_fixture_sha256_ground_truth = fixture.get("source_fixture_sha256")
    prediction_source_fixture_sha256 = artifact.get("source_fixture_sha256")

    return {
        "schema_version": SCORECARD_SCHEMA_VERSION,
        "generated_at_utc": _utc_now_iso8601(),
        "engine_id": artifact.get("engine_id"),
        "engine_version": artifact.get("engine_version"),
        "configuration_identity": configuration_identity,
        "sme_reviewer_id": fixture.get("sme_reviewer_id"),
        "ground_truth_source_fixture_sha256": source_fixture_sha256_ground_truth,
        "prediction_source_fixture_sha256": prediction_source_fixture_sha256,
        "prediction_source_fixture_matches_ground_truth_source": (
            prediction_source_fixture_sha256 == source_fixture_sha256_ground_truth
            if prediction_source_fixture_sha256 and source_fixture_sha256_ground_truth
            else None
        ),
        "case_count": coverage["expected_case_count"],
        "scored_case_count": len(scored_cases),
        "unscored_case_count": len(unscored_case_ids),
        "unscored_case_ids": sorted(unscored_case_ids),
        "unscored_blocking_case_ids": sorted(unscored_blocking_case_ids),
        "false_approval_case_ids": false_approval_case_ids,
        "blocking_false_approval_case_ids": blocking_false_approval_case_ids,
        "false_rejection_case_ids": false_rejection_case_ids,
        "warning_recall_detected": warning_recall_detected,
        "warning_recall_total": warning_recall_total,
        "warning_recall": warning_recall,
        "overall_precision_numerator": metrics.finding_precision_true_positives,
        "overall_precision_denominator": metrics.finding_precision_total_findings,
        "metrics": metrics_dict,
        "finding_category_metrics": _finding_category_metrics(scored_cases, findings_by_case_id),
        "known_good_approval_rate": known_good_approval_rate,
        "defective_case_rejection_rate": defective_case_rejection_rate,
    }


def write_scorecard(
    path: Path | str,
    scorecard: Mapping[str, Any],
    *,
    allow_overwrite: bool = False,
) -> None:
    output_path = Path(path)
    if output_path.exists() and not allow_overwrite:
        raise QualityBenchmarkExecutionError(f"refusing to overwrite existing scorecard: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(scorecard, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def dumps_scorecard(scorecard: Mapping[str, Any]) -> str:
    return json.dumps(scorecard, indent=2, sort_keys=True)
