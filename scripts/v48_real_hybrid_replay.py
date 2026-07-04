#!/usr/bin/env python3
"""
Manual runner for the frozen 10-question V48 hybrid replay with real embeddings.

Default mode is dry-run (no HTTP, Supabase, or database writes). Real execution
requires explicit --execute plus model parameters, a provider-request ceiling,
and configured environment secrets. Collects shadow semantic-score evidence only;
does not apply qualified_v2 rules or semantic thresholds.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workers.v48_hybrid_replay_authoritative_text import (  # noqa: E402
    DEFAULT_VALIDATED_MODEL_VERSION,
    FAILURE_STAGE_EMBEDDING_EXECUTION,
    FAILURE_STAGE_EXECUTE_PREFLIGHT,
    FAILURE_STAGE_RESULT_AGGREGATION,
    STALE_MODEL_VERSION_TAGS,
    AuthoritativeEmbeddingTextError,
    AuthoritativeEmbeddingTextResolver,
    assert_execute_resolver_is_authoritative,
    build_supabase_authoritative_embedding_text_resolver,
    sanitize_error_detail,
    _selected_semantic_review_bindings,
)
from workers.ai_quality_audit_shadow import (  # noqa: E402
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    CONFIDENCE_CLASS_V1_SUFFICIENT,
    classify_question_shadow_from_replay_record,
)
from workers.embedding_cache import (  # noqa: E402
    EmbeddingCacheReadError,
    EmbeddingCacheRepository,
    EmbeddingProviderResponse,
    SupabaseEmbeddingCacheRepository,
    TABLE_NAME,
)
from workers.ai_quality_audit_hybrid_replay import (  # noqa: E402
    HybridReplayError,
    HybridReplayStage1Error,
    HybridReplayStage2Error,
    run_hybrid_replay_from_records,
)
from workers.embedding_http_transport import StdlibEmbeddingHttpTransport  # noqa: E402
from workers.embedding_providers import (  # noqa: E402
    OPENAI_PROVIDER_NAME,
    EmbeddingProviderError,
    OpenAIEmbeddingProvider,
    OpenAIEmbeddingProviderConfig,
)

ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_SUPABASE_URL = "SUPABASE_URL"
ENV_SUPABASE_SERVICE_ROLE_KEY = "SUPABASE_SERVICE_ROLE_KEY"

FROZEN_REPLAY_FIXTURE_PATH = os.path.join(
    _REPO_ROOT,
    "tests",
    "fixtures",
    "v48_retrieval_replay_v1.json",
)
FROZEN_QUESTION_COUNT = 10
DEFAULT_CANDIDATE_LIMIT = 2
DEFAULT_TIMEOUT_SECONDS = 30.0

RUNNER_SCHEMA_VERSION = "v48_real_hybrid_replay_v1"
SHADOW_EVIDENCE_ONLY_NOTICE = (
    "Shadow/offline semantic evidence only; no v2 qualification decision was made; "
    "no semantic cutoff threshold was applied."
)
PRODUCTION_STATE_NOTICE = (
    "No production audit runs, evidence sets, job queue, or worker state were "
    "modified. Future explicit execution may populate durable retrieval_embedding_cache "
    "rows for legitimate embedding inputs."
)


class RealHybridReplayError(RuntimeError):
    """Base error for the real hybrid replay runner."""


class RealHybridReplayConfigError(RealHybridReplayError):
    """Raised when CLI configuration or execution gates are invalid."""


class RealHybridReplayEnvironmentError(RealHybridReplayError):
    """Raised when required execution environment variables are missing."""


class RealHybridReplayProviderError(RealHybridReplayError):
    """Raised when the embedding provider fails during replay execution."""


class RealHybridReplayCacheError(RealHybridReplayError):
    """Raised when embedding cache operations fail during replay execution."""


class RealHybridReplayBudgetError(RealHybridReplayError):
    """Raised when the provider-request ceiling would be exceeded."""


class RealHybridReplayTextResolutionError(RealHybridReplayError):
    """Raised when authoritative embedding text cannot be resolved before execution."""


@dataclass(frozen=True)
class RealHybridReplayConfig:
    execute: bool
    model_name: Optional[str]
    model_version: Optional[str]
    dimensions: Optional[int]
    max_provider_requests: Optional[int]
    candidate_limit: int
    timeout_seconds: float
    run_id: str
    fixture_path: str


@dataclass(frozen=True)
class ReplayExecutionPlan:
    question_count: int
    distinct_query_identity_count: int
    distinct_chunk_identity_count: int
    cold_cache_max_provider_requests: int
    semantic_review_question_count: int
    candidate_limit: int


class ProviderRequestBudget:
    """Track provider requests and enforce a hard ceiling before each embed call."""

    def __init__(self, *, max_provider_requests: int) -> None:
        if max_provider_requests <= 0:
            raise RealHybridReplayConfigError(
                "--max-provider-requests must be a positive integer"
            )
        self.max_provider_requests = int(max_provider_requests)
        self.provider_request_count = 0

    def reserve_provider_request(self) -> None:
        if self.provider_request_count >= self.max_provider_requests:
            raise RealHybridReplayBudgetError(
                "provider-request ceiling reached before next embedding request "
                f"(provider_request_count={self.provider_request_count}, "
                f"max_provider_requests={self.max_provider_requests})"
            )
        self.provider_request_count += 1


class BudgetEnforcingEmbeddingProvider:
    """Wrap an embedding provider with a hard request ceiling."""

    def __init__(
        self,
        *,
        inner: Any,
        budget: ProviderRequestBudget,
    ) -> None:
        self._inner = inner
        self._budget = budget

    def embed(self, **kwargs: Any) -> EmbeddingProviderResponse:
        self._budget.reserve_provider_request()
        return self._inner.embed(**kwargs)


def parse_args(argv: Optional[Sequence[str]] = None) -> RealHybridReplayConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute the frozen 10-question V48 hybrid replay using "
            "OpenAI embeddings and the durable Supabase cache."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform real provider and Supabase cache calls (requires env secrets)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Embedding model name (required with --execute)",
    )
    parser.add_argument(
        "--model-version",
        default=None,
        help=(
            "Embedding model version label stored in cache identity "
            f"(required with --execute; validated tag: {DEFAULT_VALIDATED_MODEL_VERSION})"
        ),
    )
    parser.add_argument(
        "--dimensions",
        type=int,
        default=None,
        help="Requested embedding dimensions (required with --execute)",
    )
    parser.add_argument(
        "--max-provider-requests",
        type=int,
        default=None,
        help="Hard ceiling on OpenAI embedding requests (required with --execute)",
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=DEFAULT_CANDIDATE_LIMIT,
        help=f"Stage 2 candidate cap per semantic-review question (default {DEFAULT_CANDIDATE_LIMIT})",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout for provider requests (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional replay run UUID; defaults to a newly generated UUID",
    )
    parser.add_argument(
        "--fixture-path",
        default=FROZEN_REPLAY_FIXTURE_PATH,
        help="Path to the frozen replay fixture (defaults to the committed V48 fixture)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.candidate_limit <= 0:
        raise SystemExit("--candidate-limit must be a positive integer")

    return RealHybridReplayConfig(
        execute=bool(args.execute),
        model_name=str(args.model).strip() if args.model is not None else None,
        model_version=str(args.model_version).strip()
        if args.model_version is not None
        else None,
        dimensions=int(args.dimensions) if args.dimensions is not None else None,
        max_provider_requests=int(args.max_provider_requests)
        if args.max_provider_requests is not None
        else None,
        candidate_limit=int(args.candidate_limit),
        timeout_seconds=float(args.timeout_seconds),
        run_id=str(args.run_id or uuid.uuid4()),
        fixture_path=str(args.fixture_path),
    )


def load_frozen_replay_fixture(*, fixture_path: str = FROZEN_REPLAY_FIXTURE_PATH) -> dict[str, Any]:
    """Load and validate the committed frozen 10-question replay fixture."""
    if not os.path.isfile(fixture_path):
        raise RealHybridReplayConfigError(
            f"frozen replay fixture not found at {fixture_path!r}"
        )
    with open(fixture_path, encoding="utf-8") as handle:
        fixture = json.load(handle)
    questions = fixture.get("questions")
    if not isinstance(questions, list):
        raise RealHybridReplayConfigError("fixture questions must be a list")
    if len(questions) != FROZEN_QUESTION_COUNT:
        raise RealHybridReplayConfigError(
            f"fixture must contain exactly {FROZEN_QUESTION_COUNT} questions; "
            f"found {len(questions)}"
        )
    return fixture


def compute_replay_execution_plan(
    fixture: Mapping[str, Any],
    *,
    candidate_limit: int,
) -> ReplayExecutionPlan:
    """Compute structural replay counts without resolving authoritative text."""
    if candidate_limit <= 0:
        raise RealHybridReplayConfigError("candidate_limit must be a positive integer")

    cold_cache_max_provider_requests = 0
    semantic_review_question_count = 0
    distinct_query_identity_count = 0
    distinct_chunk_identity_count = 0

    for record in fixture["questions"]:
        shadow = classify_question_shadow_from_replay_record(record)
        if shadow["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
            continue

        semantic_review_question_count += 1
        bindings = _selected_semantic_review_bindings(
            record,
            candidate_limit=candidate_limit,
        )
        distinct_query_identity_count += 1
        distinct_chunk_identity_count += len(bindings)
        cold_cache_max_provider_requests += 1 + len(bindings)

    return ReplayExecutionPlan(
        question_count=FROZEN_QUESTION_COUNT,
        distinct_query_identity_count=distinct_query_identity_count,
        distinct_chunk_identity_count=distinct_chunk_identity_count,
        cold_cache_max_provider_requests=cold_cache_max_provider_requests,
        semantic_review_question_count=semantic_review_question_count,
        candidate_limit=candidate_limit,
    )


def validate_execute_configuration(
    config: RealHybridReplayConfig,
    *,
    env: Mapping[str, str],
    plan: ReplayExecutionPlan,
) -> None:
    if not config.execute:
        raise RealHybridReplayConfigError(
            "execution confirmation is required via --execute"
        )
    if not config.model_name:
        raise RealHybridReplayConfigError("--model is required when --execute is supplied")
    if not config.model_version:
        raise RealHybridReplayConfigError(
            "--model-version is required when --execute is supplied"
        )
    if config.dimensions is None:
        raise RealHybridReplayConfigError(
            "--dimensions is required when --execute is supplied"
        )
    if config.dimensions <= 0:
        raise RealHybridReplayConfigError("--dimensions must be a positive integer")
    if config.max_provider_requests is None:
        raise RealHybridReplayConfigError(
            "--max-provider-requests is required when --execute is supplied"
        )
    if config.max_provider_requests <= 0:
        raise RealHybridReplayConfigError(
            "--max-provider-requests must be a positive integer"
        )
    if config.timeout_seconds <= 0:
        raise RealHybridReplayConfigError("--timeout-seconds must be positive")
    if config.candidate_limit <= 0:
        raise RealHybridReplayConfigError("--candidate-limit must be a positive integer")
    if config.max_provider_requests > plan.cold_cache_max_provider_requests:
        raise RealHybridReplayConfigError(
            "--max-provider-requests exceeds the calculated cold-cache bound "
            f"({config.max_provider_requests} > {plan.cold_cache_max_provider_requests})"
        )
    if str(config.model_version).strip() in STALE_MODEL_VERSION_TAGS:
        raise RealHybridReplayConfigError(
            f"--model-version {config.model_version!r} is stale; use "
            f"{DEFAULT_VALIDATED_MODEL_VERSION!r}"
        )

    missing_env = [
        name
        for name in (
            ENV_OPENAI_API_KEY,
            ENV_SUPABASE_URL,
            ENV_SUPABASE_SERVICE_ROLE_KEY,
        )
        if not str(env.get(name) or "").strip()
    ]
    if missing_env:
        raise RealHybridReplayEnvironmentError(
            "missing required environment variables: " + ", ".join(sorted(missing_env))
        )

    _ = plan


def build_initial_summary(
    config: RealHybridReplayConfig,
    *,
    plan: ReplayExecutionPlan,
) -> dict[str, Any]:
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "run_id": config.run_id,
        "final_status": "planned" if not config.execute else "failed",
        "question_count": plan.question_count,
        "semantic_review_question_count": plan.semantic_review_question_count,
        "candidate_limit": plan.candidate_limit,
        "distinct_query_identity_count": plan.distinct_query_identity_count,
        "distinct_chunk_identity_count": plan.distinct_chunk_identity_count,
        "cold_cache_max_provider_requests": plan.cold_cache_max_provider_requests,
        "max_provider_requests": config.max_provider_requests or 0,
        "provider_request_count": 0,
        "cache_hit_count": 0,
        "cache_miss_count": 0,
        "query_cache_hit_count": 0,
        "query_cache_miss_count": 0,
        "chunk_cache_hit_count": 0,
        "chunk_cache_miss_count": 0,
        "total_v1_qualified_candidates": 0,
        "total_hybrid_evaluated_candidates": 0,
        "stage1_classification_counts": {},
        "semantic_status_counts": {},
        "classification_transition_counts": {
            "v1_sufficient_skipped_stage2": 0,
            "semantic_review_completed_stage2": 0,
        },
        "questions": [],
        "qualified_v2_applied": False,
        "semantic_threshold_applied": False,
        "retrieval_shadow_evaluations_written": False,
        "authoritative_text_used": False,
        "authoritative_text_resolution": (
            "planned_not_executed" if not config.execute else "pending"
        ),
        "replay_content_set_hash": "",
        "semantic_evidence_collected": False,
        "failure_stage": "",
        "error_code": "",
        "error_type": "",
        "error_detail": "",
        "notice": SHADOW_EVIDENCE_ONLY_NOTICE,
        "production_state_notice": PRODUCTION_STATE_NOTICE,
    }


def format_dry_run_plan(
    config: RealHybridReplayConfig,
    *,
    plan: ReplayExecutionPlan,
) -> str:
    lines = [
        "V48 real hybrid replay dry-run",
        f"run_id: {config.run_id}",
        "execute: false",
        f"fixture_path: {config.fixture_path}",
        f"provider_name: {OPENAI_PROVIDER_NAME}",
        f"model_name: {config.model_name or '(not supplied)'}",
        f"model_version: {config.model_version or '(not supplied)'}",
        f"dimensions: {config.dimensions if config.dimensions is not None else '(not supplied)'}",
        f"candidate_limit: {config.candidate_limit}",
        f"max_provider_requests: {config.max_provider_requests if config.max_provider_requests is not None else '(not supplied)'}",
        f"timeout_seconds: {config.timeout_seconds}",
        f"question_count: {plan.question_count}",
        f"semantic_review_question_count: {plan.semantic_review_question_count}",
        f"distinct_query_identity_count: {plan.distinct_query_identity_count}",
        f"distinct_chunk_identity_count: {plan.distinct_chunk_identity_count}",
        f"cold_cache_max_provider_requests: {plan.cold_cache_max_provider_requests}",
        f"validated_model_version_tag: {DEFAULT_VALIDATED_MODEL_VERSION}",
        "authoritative_text_resolution: planned_not_executed",
        "authoritative_text_used: false",
        "semantic_evidence_collected: false",
        "qualified_v2_applied: false",
        "semantic_threshold_applied: false",
        SHADOW_EVIDENCE_ONLY_NOTICE,
        PRODUCTION_STATE_NOTICE,
        "required_for_execute:",
        "  - --execute",
        "  - --model",
        "  - --model-version",
        "  - --dimensions",
        "  - --max-provider-requests",
        f"  - {ENV_OPENAI_API_KEY}",
        f"  - {ENV_SUPABASE_URL}",
        f"  - {ENV_SUPABASE_SERVICE_ROLE_KEY}",
        "external_calls_planned: 0",
        "provider_requests_planned: 0",
        "database_writes_planned: 0",
        "No external calls performed (dry-run).",
    ]
    return "\n".join(lines)


def format_redacted_summary(summary: Mapping[str, Any]) -> str:
    payload = {
        "schema_version": summary["schema_version"],
        "run_id": summary["run_id"],
        "final_status": summary["final_status"],
        "question_count": summary["question_count"],
        "semantic_review_question_count": summary["semantic_review_question_count"],
        "candidate_limit": summary["candidate_limit"],
        "distinct_query_identity_count": summary["distinct_query_identity_count"],
        "distinct_chunk_identity_count": summary["distinct_chunk_identity_count"],
        "cold_cache_max_provider_requests": summary["cold_cache_max_provider_requests"],
        "max_provider_requests": summary["max_provider_requests"],
        "provider_request_count": summary["provider_request_count"],
        "cache_hit_count": summary["cache_hit_count"],
        "cache_miss_count": summary["cache_miss_count"],
        "query_cache_hit_count": summary["query_cache_hit_count"],
        "query_cache_miss_count": summary["query_cache_miss_count"],
        "chunk_cache_hit_count": summary["chunk_cache_hit_count"],
        "chunk_cache_miss_count": summary["chunk_cache_miss_count"],
        "total_v1_qualified_candidates": summary["total_v1_qualified_candidates"],
        "total_hybrid_evaluated_candidates": summary["total_hybrid_evaluated_candidates"],
        "stage1_classification_counts": summary["stage1_classification_counts"],
        "semantic_status_counts": summary["semantic_status_counts"],
        "classification_transition_counts": summary["classification_transition_counts"],
        "questions": summary["questions"],
        "qualified_v2_applied": summary["qualified_v2_applied"],
        "semantic_threshold_applied": summary["semantic_threshold_applied"],
        "retrieval_shadow_evaluations_written": summary[
            "retrieval_shadow_evaluations_written"
        ],
        "authoritative_text_used": summary["authoritative_text_used"],
        "authoritative_text_resolution": summary.get("authoritative_text_resolution"),
        "replay_content_set_hash": summary.get("replay_content_set_hash", ""),
        "semantic_evidence_collected": summary.get("semantic_evidence_collected", False),
        "failure_stage": summary.get("failure_stage", ""),
        "error_code": summary.get("error_code", ""),
        "error_type": summary.get("error_type", ""),
        "error_detail": summary.get("error_detail", ""),
        "notice": summary["notice"],
        "production_state_notice": summary["production_state_notice"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _aggregate_cache_metrics(replay_result: Mapping[str, Any]) -> dict[str, int]:
    metrics = {
        "cache_hit_count": 0,
        "cache_miss_count": 0,
        "query_cache_hit_count": 0,
        "query_cache_miss_count": 0,
        "chunk_cache_hit_count": 0,
        "chunk_cache_miss_count": 0,
    }
    for item in replay_result["questions"]:
        semantic_result = item["semantic_result"]
        if str(semantic_result.get("status")) != "completed":
            continue
        if bool(semantic_result.get("query_embedding_cache_hit")):
            metrics["query_cache_hit_count"] += 1
            metrics["cache_hit_count"] += 1
        else:
            metrics["query_cache_miss_count"] += 1
            metrics["cache_miss_count"] += 1

        for candidate in semantic_result.get("candidates") or []:
            if bool(candidate.get("embedding_cache_hit")):
                metrics["chunk_cache_hit_count"] += 1
                metrics["cache_hit_count"] += 1
            else:
                metrics["chunk_cache_miss_count"] += 1
                metrics["cache_miss_count"] += 1
    return metrics


def _build_redacted_question_summaries(
    replay_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in replay_result["questions"]:
        semantic_result = item["semantic_result"]
        similarities = [
            float(candidate["semantic_similarity"])
            for candidate in semantic_result.get("candidates") or []
        ]
        question_summary: dict[str, Any] = {
            "question_version_id": str(item["question_version_id"]),
            "confidence_class": str(item["confidence_class"]),
            "qualified_count_v1": int(item["qualified_count_v1"]),
            "structural_candidate_count": int(item["structural_candidate_count"]),
            "semantic_status": str(semantic_result["status"]),
            "evaluated_candidate_count": int(
                semantic_result.get("evaluated_candidate_count") or 0
            ),
            "query_embedding_cache_hit": bool(
                semantic_result.get("query_embedding_cache_hit") or False
            ),
        }
        if similarities:
            question_summary["semantic_similarity_min"] = min(similarities)
            question_summary["semantic_similarity_max"] = max(similarities)
            question_summary["semantic_similarity_mean"] = round(
                sum(similarities) / len(similarities),
                9,
            )
        summaries.append(question_summary)
    return summaries


def _apply_replay_result_to_summary(
    summary: MutableMapping[str, Any],
    *,
    replay_result: Mapping[str, Any],
    provider_request_count: int,
) -> None:
    cache_metrics = _aggregate_cache_metrics(replay_result)
    summary.update(cache_metrics)
    summary["provider_request_count"] = int(provider_request_count)
    summary["stage1_classification_counts"] = dict(
        replay_result["stage1_classification_counts"]
    )
    summary["semantic_status_counts"] = dict(replay_result["semantic_status_counts"])
    summary["total_v1_qualified_candidates"] = sum(
        int(item["qualified_count_v1"]) for item in replay_result["questions"]
    )
    summary["total_hybrid_evaluated_candidates"] = sum(
        int(item["semantic_result"].get("evaluated_candidate_count") or 0)
        for item in replay_result["questions"]
    )
    summary["classification_transition_counts"] = {
        "v1_sufficient_skipped_stage2": int(
            replay_result["stage1_classification_counts"].get(
                CONFIDENCE_CLASS_V1_SUFFICIENT,
                0,
            )
        ),
        "semantic_review_completed_stage2": int(
            replay_result["semantic_status_counts"].get("completed", 0)
        ),
    }
    summary["questions"] = _build_redacted_question_summaries(replay_result)
    summary["final_status"] = "success"


def _find_runner_error(exc: BaseException) -> Optional[RealHybridReplayError]:
    """Return the nearest runner error preserved in an exception cause chain."""
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, RealHybridReplayError):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


def _find_error_in_chain(
    exc: BaseException,
    error_type: type[BaseException],
) -> Optional[BaseException]:
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, error_type):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


def _apply_execution_failure(
    summary: MutableMapping[str, Any],
    exc: BaseException,
    *,
    budget: ProviderRequestBudget,
    prepare_completed: bool,
    current_stage: str,
) -> None:
    """Populate redacted failure fields without leaking raw text or secrets."""
    summary["final_status"] = "failed"
    summary["provider_request_count"] = int(budget.provider_request_count)
    summary["semantic_evidence_collected"] = False

    auth_error = _find_error_in_chain(exc, AuthoritativeEmbeddingTextError)
    if auth_error is not None:
        summary["authoritative_text_resolution"] = "failed"
        summary["authoritative_text_used"] = False
        summary["replay_content_set_hash"] = ""
        summary["failure_stage"] = auth_error.failure_stage
        summary["error_code"] = auth_error.error_code
        summary["error_type"] = auth_error.error_type
        summary["error_detail"] = auth_error.error_detail
        return

    if prepare_completed:
        summary["authoritative_text_resolution"] = "completed"
        summary["authoritative_text_used"] = True
    else:
        summary["authoritative_text_resolution"] = "failed"
        summary["authoritative_text_used"] = False
        summary["replay_content_set_hash"] = ""

    if isinstance(exc, RealHybridReplayEnvironmentError):
        summary["failure_stage"] = FAILURE_STAGE_EXECUTE_PREFLIGHT
        summary["error_code"] = "execute_environment_invalid"
        summary["error_type"] = "configuration"
        summary["error_detail"] = sanitize_error_detail(str(exc))
        return

    if isinstance(exc, RealHybridReplayConfigError):
        summary["failure_stage"] = FAILURE_STAGE_EXECUTE_PREFLIGHT
        summary["error_code"] = "execute_configuration_invalid"
        summary["error_type"] = "configuration"
        summary["error_detail"] = sanitize_error_detail(str(exc))
        return

    if isinstance(exc, RealHybridReplayTextResolutionError):
        summary["failure_stage"] = current_stage
        summary["error_code"] = "authoritative_text_resolution_failed"
        summary["error_type"] = "authoritative_resolution"
        summary["error_detail"] = sanitize_error_detail(str(exc))
        return

    if isinstance(exc, RealHybridReplayBudgetError):
        summary["failure_stage"] = FAILURE_STAGE_EMBEDDING_EXECUTION
        summary["error_code"] = "provider_request_ceiling_reached"
        summary["error_type"] = "budget"
        summary["error_detail"] = sanitize_error_detail(str(exc))
        return

    if isinstance(exc, RealHybridReplayProviderError):
        summary["failure_stage"] = FAILURE_STAGE_EMBEDDING_EXECUTION
        summary["error_code"] = "embedding_provider_failed"
        summary["error_type"] = "embedding_provider"
        summary["error_detail"] = "embedding provider failed during hybrid replay execution"
        return

    if isinstance(exc, RealHybridReplayCacheError):
        summary["failure_stage"] = FAILURE_STAGE_EMBEDDING_EXECUTION
        summary["error_code"] = "embedding_cache_failed"
        summary["error_type"] = "embedding_cache"
        summary["error_detail"] = "embedding cache operation failed during hybrid replay execution"
        return

    summary["failure_stage"] = current_stage or FAILURE_STAGE_EXECUTE_PREFLIGHT
    summary["error_code"] = "unexpected_execution_failure"
    summary["error_type"] = "unexpected"
    cause = exc.__cause__ or exc
    summary["error_detail"] = sanitize_error_detail(str(cause))


def run_real_hybrid_replay(
    config: RealHybridReplayConfig,
    *,
    env: Optional[Mapping[str, str]] = None,
    fixture: Optional[Mapping[str, Any]] = None,
    client_factory: Optional[Callable[[], Any]] = None,
    transport_factory: Callable[[], StdlibEmbeddingHttpTransport] = StdlibEmbeddingHttpTransport,
    resolver_factory: Optional[
        Callable[[Any, Mapping[str, Any], int], AuthoritativeEmbeddingTextResolver]
    ] = None,
    summary: Optional[MutableMapping[str, Any]] = None,
) -> dict[str, Any]:
    """Run the frozen replay or return a dry-run summary without external calls."""
    environment = dict(os.environ if env is None else env)
    loaded_fixture = fixture if fixture is not None else load_frozen_replay_fixture(
        fixture_path=config.fixture_path
    )

    plan = compute_replay_execution_plan(
        loaded_fixture,
        candidate_limit=config.candidate_limit,
    )

    if summary is not None:
        result_summary: dict[str, Any] = summary
        result_summary.setdefault("schema_version", RUNNER_SCHEMA_VERSION)
        result_summary.setdefault("run_id", config.run_id)
        result_summary.setdefault("final_status", "planned" if not config.execute else "failed")
        result_summary.setdefault("question_count", plan.question_count)
        result_summary.setdefault(
            "semantic_review_question_count",
            plan.semantic_review_question_count,
        )
        result_summary.setdefault("candidate_limit", plan.candidate_limit)
        result_summary.setdefault(
            "distinct_query_identity_count",
            plan.distinct_query_identity_count,
        )
        result_summary.setdefault(
            "distinct_chunk_identity_count",
            plan.distinct_chunk_identity_count,
        )
        result_summary.setdefault(
            "cold_cache_max_provider_requests",
            plan.cold_cache_max_provider_requests,
        )
        result_summary.setdefault(
            "max_provider_requests",
            config.max_provider_requests or 0,
        )
        result_summary.setdefault("provider_request_count", 0)
        result_summary.setdefault("cache_hit_count", 0)
        result_summary.setdefault("cache_miss_count", 0)
        result_summary.setdefault("query_cache_hit_count", 0)
        result_summary.setdefault("query_cache_miss_count", 0)
        result_summary.setdefault("chunk_cache_hit_count", 0)
        result_summary.setdefault("chunk_cache_miss_count", 0)
        result_summary.setdefault("total_v1_qualified_candidates", 0)
        result_summary.setdefault("total_hybrid_evaluated_candidates", 0)
        result_summary.setdefault("stage1_classification_counts", {})
        result_summary.setdefault("semantic_status_counts", {})
        result_summary.setdefault(
            "classification_transition_counts",
            {
                "v1_sufficient_skipped_stage2": 0,
                "semantic_review_completed_stage2": 0,
            },
        )
        result_summary.setdefault("questions", [])
        result_summary.setdefault("qualified_v2_applied", False)
        result_summary.setdefault("semantic_threshold_applied", False)
        result_summary.setdefault("retrieval_shadow_evaluations_written", False)
        result_summary.setdefault("authoritative_text_used", False)
        result_summary.setdefault(
            "authoritative_text_resolution",
            "planned_not_executed" if not config.execute else "pending",
        )
        result_summary.setdefault("replay_content_set_hash", "")
        result_summary.setdefault("semantic_evidence_collected", False)
        result_summary.setdefault("failure_stage", "")
        result_summary.setdefault("error_code", "")
        result_summary.setdefault("error_type", "")
        result_summary.setdefault("error_detail", "")
        result_summary.setdefault("notice", SHADOW_EVIDENCE_ONLY_NOTICE)
        result_summary.setdefault("production_state_notice", PRODUCTION_STATE_NOTICE)
    else:
        result_summary = build_initial_summary(config, plan=plan)

    if not config.execute:
        return result_summary

    validate_execute_configuration(config, env=environment, plan=plan)

    assert config.model_name is not None
    assert config.model_version is not None
    assert config.dimensions is not None
    assert config.max_provider_requests is not None

    budget = ProviderRequestBudget(max_provider_requests=config.max_provider_requests)
    execution_error: Optional[RealHybridReplayError] = None
    replay_result: Optional[dict[str, Any]] = None
    prepare_completed = False
    current_stage = FAILURE_STAGE_EXECUTE_PREFLIGHT

    try:
        result_summary["authoritative_text_resolution"] = "started"
        result_summary["failure_stage"] = current_stage

        if client_factory is None:
            from utils.access_control import (
                SupabaseAdminConfigError,
                create_supabase_admin_client,
            )

            try:
                client = create_supabase_admin_client()
            except SupabaseAdminConfigError as exc:
                raise RealHybridReplayEnvironmentError(
                    "Supabase admin client configuration is invalid"
                ) from exc
        else:
            client = client_factory()

        transport = transport_factory()
        base_provider = OpenAIEmbeddingProvider(
            config=OpenAIEmbeddingProviderConfig(
                api_key=str(environment[ENV_OPENAI_API_KEY]).strip(),
                timeout_seconds=config.timeout_seconds,
            ),
            transport=transport,
        )
        provider = BudgetEnforcingEmbeddingProvider(inner=base_provider, budget=budget)
        repository = SupabaseEmbeddingCacheRepository(client)
        if resolver_factory is None:
            resolver = build_supabase_authoritative_embedding_text_resolver(
                client,
                loaded_fixture,
                candidate_limit=config.candidate_limit,
            )
        else:
            resolver = resolver_factory(
                client,
                loaded_fixture,
                config.candidate_limit,
            )
        assert_execute_resolver_is_authoritative(resolver)
        resolver.prepare()
        prepare_completed = True
        result_summary["authoritative_text_used"] = True
        result_summary["authoritative_text_resolution"] = "completed"
        result_summary["replay_content_set_hash"] = resolver.replay_content_set_hash
        result_summary["failure_stage"] = ""
        result_summary["error_code"] = ""
        result_summary["error_type"] = ""
        result_summary["error_detail"] = ""

        current_stage = FAILURE_STAGE_EMBEDDING_EXECUTION
        replay_result = run_hybrid_replay_from_records(
            replay_records=loaded_fixture["questions"],
            candidate_limit=config.candidate_limit,
            embedding_text_resolver=resolver,
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name=config.model_name,
            embedding_model_version=config.model_version,
            embedding_dimensions=config.dimensions,
            repository=repository,
            provider=provider,
        )
        current_stage = FAILURE_STAGE_RESULT_AGGREGATION
        _apply_replay_result_to_summary(
            result_summary,
            replay_result=replay_result,
            provider_request_count=budget.provider_request_count,
        )
        result_summary["semantic_evidence_collected"] = True
    except AuthoritativeEmbeddingTextError as exc:
        execution_error = RealHybridReplayTextResolutionError(str(exc))
        execution_error.__cause__ = exc
    except RealHybridReplayError as exc:
        execution_error = exc
    except HybridReplayError as exc:
        preserved = _find_runner_error(exc)
        if preserved is not None:
            execution_error = preserved
        elif _find_error_in_chain(exc, EmbeddingProviderError) is not None:
            execution_error = RealHybridReplayProviderError(
                "embedding provider failed during hybrid replay execution"
            )
            execution_error.__cause__ = exc
        elif _find_error_in_chain(exc, EmbeddingCacheReadError) is not None:
            execution_error = RealHybridReplayCacheError(
                "embedding cache read failed during hybrid replay execution"
            )
            execution_error.__cause__ = exc
        else:
            execution_error = RealHybridReplayError("hybrid replay execution failed")
            execution_error.__cause__ = exc
    except EmbeddingProviderError as exc:
        execution_error = RealHybridReplayProviderError(
            "embedding provider failed during hybrid replay execution"
        )
        execution_error.__cause__ = exc
    except EmbeddingCacheReadError as exc:
        execution_error = RealHybridReplayCacheError(
            "embedding cache read failed during hybrid replay execution"
        )
        execution_error.__cause__ = exc
    except Exception as exc:
        from utils.access_control import SupabaseAdminConfigError

        if isinstance(exc, SupabaseAdminConfigError):
            execution_error = RealHybridReplayEnvironmentError(
                "Supabase admin client configuration is invalid"
            )
            execution_error.__cause__ = exc
        else:
            execution_error = RealHybridReplayError(
                "hybrid replay execution failed unexpectedly"
            )
            execution_error.__cause__ = exc
    finally:
        result_summary["provider_request_count"] = budget.provider_request_count

    if execution_error is not None:
        _apply_execution_failure(
            result_summary,
            execution_error,
            budget=budget,
            prepare_completed=prepare_completed,
            current_stage=current_stage,
        )
        raise execution_error
    return result_summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        config = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 1)

    loaded_fixture = load_frozen_replay_fixture(fixture_path=config.fixture_path)
    plan = compute_replay_execution_plan(
        loaded_fixture,
        candidate_limit=config.candidate_limit,
    )

    if not config.execute:
        print(format_dry_run_plan(config, plan=plan))
        return 0

    summary = build_initial_summary(config, plan=plan)
    try:
        run_real_hybrid_replay(
            config,
            env=os.environ,
            fixture=loaded_fixture,
            summary=summary,
        )
    except RealHybridReplayError as exc:
        print(format_redacted_summary(summary), file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_redacted_summary(summary))
    return 0 if summary.get("final_status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
