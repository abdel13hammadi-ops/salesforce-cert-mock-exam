#!/usr/bin/env python3
"""
Build a local-only relevance review packet from the frozen 10-question V48 replay.

Default mode is dry-run (no Supabase or provider calls). Real execution reads
existing durable embedding-cache rows only and never calls OpenAI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.v48_real_hybrid_replay import (  # noqa: E402
    FROZEN_QUESTION_COUNT,
    FROZEN_REPLAY_FIXTURE_PATH,
    load_frozen_replay_fixture,
)
from workers.v48_hybrid_replay_authoritative_text import (  # noqa: E402
    DEFAULT_VALIDATED_MODEL_VERSION,
    AuthoritativeEmbeddingTextError,
    AuthoritativeEmbeddingTextResolver,
    assert_execute_resolver_is_authoritative,
    build_supabase_authoritative_embedding_text_resolver,
    _selected_semantic_review_bindings,
)
from workers.ai_quality_audit_hybrid_replay import (  # noqa: E402
    HybridReplayError,
    run_hybrid_replay_from_records,
)
from workers.ai_quality_audit_shadow import (  # noqa: E402
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    classify_question_shadow_from_replay_record,
)
from workers.embedding_cache import (  # noqa: E402
    EmbeddingCacheError,
    EmbeddingCacheRepository,
    EmbeddingProviderResponse,
    SupabaseEmbeddingCacheRepository,
)
from workers.embedding_providers import OPENAI_PROVIDER_NAME  # noqa: E402
from workers.resource_chunking import sha256_hex  # noqa: E402

ENV_SUPABASE_URL = "SUPABASE_URL"
ENV_SUPABASE_SERVICE_ROLE_KEY = "SUPABASE_SERVICE_ROLE_KEY"

DEFAULT_CANDIDATE_LIMIT = 2
DEFAULT_DIMENSIONS = 1536
DEFAULT_MODEL_NAME = "text-embedding-3-small"
REFERENCE_REPLAY_CONTENT_SET_HASH = (
    "b7c05c1c04b2b55e37919990408068c6df244db41a28970d283821ce4f3d61e3"
)

LOCAL_REVIEW_ROOT = os.path.join(_REPO_ROOT, ".local", "v48", "relevance_review")
PACKET_SCHEMA_VERSION = "v48_relevance_review_packet_v1"
ALLOWED_RELEVANCE_LABELS = (
    "relevant",
    "partially_relevant",
    "irrelevant",
    "uncertain",
)


class RelevanceReviewPacketError(RuntimeError):
    """Base error for the relevance review packet builder."""


class RelevanceReviewPacketConfigError(RelevanceReviewPacketError):
    """Raised when CLI configuration is invalid."""


class RelevanceReviewPacketEnvironmentError(RelevanceReviewPacketError):
    """Raised when required environment variables are missing."""


class RelevanceReviewPacketContentSetError(RelevanceReviewPacketError):
    """Raised when the resolved content-set hash does not match expectation."""


class RelevanceReviewPacketOutputError(RelevanceReviewPacketError):
    """Raised when output path rules are violated."""


class RelevanceReviewPacketCacheError(RelevanceReviewPacketError):
    """Raised when a required cache row is missing."""


class CacheOnlyForbiddenProvider:
    """Fail-closed provider that forbids any embedding computation."""

    provider_request_count = 0

    def embed(self, **_kwargs: Any) -> EmbeddingProviderResponse:
        self.provider_request_count += 1
        raise EmbeddingCacheError(
            "cache-only relevance review packet forbids provider embedding requests"
        )


@dataclass(frozen=True)
class RelevanceReviewPacketConfig:
    execute: bool
    expected_content_set_hash: Optional[str]
    model_name: Optional[str]
    model_version: Optional[str]
    dimensions: Optional[int]
    candidate_limit: int
    fixture_path: str
    output_path: str
    overwrite: bool


@dataclass(frozen=True)
class RelevanceReviewPacketPlan:
    question_count: int
    semantic_review_question_count: int
    pair_count: int
    candidate_limit: int
    reference_replay_content_set_hash: str


def parse_args(argv: Optional[Sequence[str]] = None) -> RelevanceReviewPacketConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Build a local relevance review packet for the frozen 10-question "
            "V48 replay using durable cache embeddings only."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Read authoritative text and cache embeddings from Supabase (no OpenAI)",
    )
    parser.add_argument(
        "--expected-content-set-hash",
        default=None,
        help="Required with --execute; must match resolver replay_content_set_hash",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Embedding model name (required with --execute; default {DEFAULT_MODEL_NAME})",
    )
    parser.add_argument(
        "--model-version",
        default=None,
        help=(
            "Embedding model version label (required with --execute; "
            f"validated tag: {DEFAULT_VALIDATED_MODEL_VERSION})"
        ),
    )
    parser.add_argument(
        "--dimensions",
        type=int,
        default=None,
        help=f"Embedding dimensions (required with --execute; default {DEFAULT_DIMENSIONS})",
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=DEFAULT_CANDIDATE_LIMIT,
        help=f"Stage 2 candidate cap per semantic-review question (default {DEFAULT_CANDIDATE_LIMIT})",
    )
    parser.add_argument(
        "--fixture-path",
        default=FROZEN_REPLAY_FIXTURE_PATH,
        help="Path to the frozen replay fixture",
    )
    parser.add_argument(
        "--output-path",
        default=None,
        help=f"Output JSON path (must be under {LOCAL_REVIEW_ROOT})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing packet file",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.candidate_limit <= 0:
        raise SystemExit("--candidate-limit must be a positive integer")

    default_output = default_packet_output_path(
        content_set_hash=REFERENCE_REPLAY_CONTENT_SET_HASH,
    )
    output_path = str(args.output_path or default_output)

    return RelevanceReviewPacketConfig(
        execute=bool(args.execute),
        expected_content_set_hash=(
            str(args.expected_content_set_hash).strip().lower()
            if args.expected_content_set_hash is not None
            else None
        ),
        model_name=str(args.model).strip() if args.model is not None else None,
        model_version=str(args.model_version).strip()
        if args.model_version is not None
        else None,
        dimensions=int(args.dimensions) if args.dimensions is not None else None,
        candidate_limit=int(args.candidate_limit),
        fixture_path=str(args.fixture_path),
        output_path=output_path,
        overwrite=bool(args.overwrite),
    )


def default_packet_output_path(*, content_set_hash: str) -> str:
    prefix = str(content_set_hash).strip().lower()[:16]
    return os.path.join(LOCAL_REVIEW_ROOT, f"v48_relevance_review_{prefix}.json")


def compute_review_packet_plan(
    fixture: Mapping[str, Any],
    *,
    candidate_limit: int,
) -> RelevanceReviewPacketPlan:
    if candidate_limit <= 0:
        raise RelevanceReviewPacketConfigError("candidate_limit must be a positive integer")

    semantic_review_question_count = 0
    pair_count = 0
    for record in fixture["questions"]:
        shadow = classify_question_shadow_from_replay_record(record)
        if shadow["confidence_class"] != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
            continue
        semantic_review_question_count += 1
        pair_count += len(
            _selected_semantic_review_bindings(record, candidate_limit=candidate_limit)
        )

    return RelevanceReviewPacketPlan(
        question_count=FROZEN_QUESTION_COUNT,
        semantic_review_question_count=semantic_review_question_count,
        pair_count=pair_count,
        candidate_limit=candidate_limit,
        reference_replay_content_set_hash=REFERENCE_REPLAY_CONTENT_SET_HASH,
    )


def validate_execute_configuration(
    config: RelevanceReviewPacketConfig,
    *,
    env: Mapping[str, str],
) -> None:
    if not config.execute:
        raise RelevanceReviewPacketConfigError(
            "execution confirmation is required via --execute"
        )
    if not config.expected_content_set_hash:
        raise RelevanceReviewPacketConfigError(
            "--expected-content-set-hash is required when --execute is supplied"
        )
    if len(str(config.expected_content_set_hash)) != 64:
        raise RelevanceReviewPacketConfigError(
            "--expected-content-set-hash must be a 64-character lowercase SHA-256 hex digest"
        )
    if not config.model_name:
        raise RelevanceReviewPacketConfigError(
            "--model is required when --execute is supplied"
        )
    if not config.model_version:
        raise RelevanceReviewPacketConfigError(
            "--model-version is required when --execute is supplied"
        )
    if config.dimensions is None:
        raise RelevanceReviewPacketConfigError(
            "--dimensions is required when --execute is supplied"
        )
    if config.dimensions <= 0:
        raise RelevanceReviewPacketConfigError("--dimensions must be a positive integer")
    if config.candidate_limit <= 0:
        raise RelevanceReviewPacketConfigError("--candidate-limit must be a positive integer")

    missing_env = [
        name
        for name in (ENV_SUPABASE_URL, ENV_SUPABASE_SERVICE_ROLE_KEY)
        if not str(env.get(name) or "").strip()
    ]
    if missing_env:
        raise RelevanceReviewPacketEnvironmentError(
            "missing required environment variables: " + ", ".join(sorted(missing_env))
        )

    validate_output_path(config.output_path, overwrite=config.overwrite)


def validate_output_path(output_path: str, *, overwrite: bool) -> str:
    normalized = os.path.normpath(os.path.abspath(str(output_path)))
    allowed_root = os.path.normpath(os.path.abspath(LOCAL_REVIEW_ROOT))
    try:
        common = os.path.commonpath([normalized, allowed_root])
    except ValueError as exc:
        raise RelevanceReviewPacketOutputError(
            "output path must be located under the local relevance review directory"
        ) from exc
    if common != allowed_root:
        raise RelevanceReviewPacketOutputError(
            "output path must be located under the local relevance review directory"
        )
    if os.path.exists(normalized) and not overwrite:
        raise RelevanceReviewPacketOutputError(
            "output file already exists; pass --overwrite to replace it"
        )
    return normalized


def compute_pair_id(*, question_version_id: str, candidate_identity: str) -> str:
    payload = json.dumps(
        {
            "candidate_identity": str(candidate_identity),
            "question_version_id": str(question_version_id),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256_hex(payload)


def compute_packet_hash(packet_body: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        packet_body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256_hex(canonical)


def format_dry_run_plan(
    config: RelevanceReviewPacketConfig,
    *,
    plan: RelevanceReviewPacketPlan,
) -> str:
    lines = [
        "V48 relevance review packet dry-run",
        "execute: false",
        f"fixture_path: {config.fixture_path}",
        f"question_count: {plan.question_count}",
        f"semantic_review_question_count: {plan.semantic_review_question_count}",
        f"pair_count: {plan.pair_count}",
        f"candidate_limit: {plan.candidate_limit}",
        f"reference_replay_content_set_hash: {plan.reference_replay_content_set_hash}",
        f"default_output_path: {default_packet_output_path(content_set_hash=plan.reference_replay_content_set_hash)}",
        "cache_only: true",
        "provider_requests_allowed: 0",
        f"model_name: {config.model_name or DEFAULT_MODEL_NAME}",
        f"model_version: {config.model_version or DEFAULT_VALIDATED_MODEL_VERSION}",
        f"dimensions: {config.dimensions if config.dimensions is not None else DEFAULT_DIMENSIONS}",
        "allowed_relevance_labels: relevant, partially_relevant, irrelevant, uncertain",
        "No external calls performed (dry-run).",
    ]
    return "\n".join(lines)


def format_redacted_console_summary(summary: Mapping[str, Any]) -> str:
    payload = {
        "final_status": summary["final_status"],
        "output_path": summary.get("output_path", ""),
        "packet_hash": summary.get("packet_hash", ""),
        "replay_content_set_hash": summary.get("replay_content_set_hash", ""),
        "question_count": summary.get("question_count", 0),
        "semantic_review_question_count": summary.get("semantic_review_question_count", 0),
        "pair_count": summary.get("pair_count", 0),
        "cache_hit_count": summary.get("cache_hit_count", 0),
        "cache_miss_count": summary.get("cache_miss_count", 0),
        "provider_request_count": summary.get("provider_request_count", 0),
        "cache_only": summary.get("cache_only", True),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _aggregate_cache_metrics(replay_result: Mapping[str, Any]) -> dict[str, int]:
    metrics = {"cache_hit_count": 0, "cache_miss_count": 0}
    for item in replay_result["questions"]:
        semantic_result = item["semantic_result"]
        if str(semantic_result.get("status")) != "completed":
            continue
        if bool(semantic_result.get("query_embedding_cache_hit")):
            metrics["cache_hit_count"] += 1
        else:
            metrics["cache_miss_count"] += 1
        for candidate in semantic_result.get("candidates") or []:
            if bool(candidate.get("embedding_cache_hit")):
                metrics["cache_hit_count"] += 1
            else:
                metrics["cache_miss_count"] += 1
    return metrics


def build_review_pairs(
    *,
    fixture: Mapping[str, Any],
    replay_result: Mapping[str, Any],
    resolver: AuthoritativeEmbeddingTextResolver,
    candidate_limit: int,
) -> list[dict[str, Any]]:
    fixture_by_id = {
        str(record["question_version_id"]): record for record in fixture["questions"]
    }
    pairs: list[dict[str, Any]] = []

    for item in replay_result["questions"]:
        if str(item["confidence_class"]) != CONFIDENCE_CLASS_SEMANTIC_REVIEW:
            continue
        question_version_id = str(item["question_version_id"])
        question_record = fixture_by_id[question_version_id]
        shadow = classify_question_shadow_from_replay_record(question_record)
        query_text = resolver.resolve_question_embedding_text(question_version_id)
        if query_text is None or not str(query_text).strip():
            raise RelevanceReviewPacketError(
                f"missing authoritative query text for question {question_version_id!r}"
            )

        semantic_by_identity = {
            str(candidate["candidate_identity"]): candidate
            for candidate in item["semantic_result"].get("candidates") or []
        }
        shadow_candidates = list(shadow["candidates"])

        for binding in _selected_semantic_review_bindings(
            question_record,
            candidate_limit=candidate_limit,
        ):
            semantic_candidate = semantic_by_identity.get(binding.candidate_identity)
            if semantic_candidate is None:
                raise RelevanceReviewPacketError(
                    "semantic scoring result missing selected candidate "
                    f"{binding.candidate_identity!r}"
                )
            chunk_text = resolver.resolve_candidate_embedding_text(binding.candidate_identity)
            if chunk_text is None or not str(chunk_text).strip():
                raise RelevanceReviewPacketError(
                    "missing authoritative candidate text for identity "
                    f"{binding.candidate_identity!r}"
                )
            candidate_match = resolver.resolve_candidate_match(binding.candidate_identity)
            if candidate_match is None:
                raise RelevanceReviewPacketError(
                    "missing authoritative candidate match metadata for identity "
                    f"{binding.candidate_identity!r}"
                )
            shadow_candidate = shadow_candidates[binding.candidate_position]
            pairs.append(
                {
                    "pair_id": compute_pair_id(
                        question_version_id=question_version_id,
                        candidate_identity=binding.candidate_identity,
                    ),
                    "question_version_id": question_version_id,
                    "candidate_identity": binding.candidate_identity,
                    "resource_id": candidate_match.resource_id,
                    "resource_chunk_id": candidate_match.resource_chunk_id,
                    "resource_type": candidate_match.resource_type,
                    "resource_title": candidate_match.title,
                    "authoritative_query_text": str(query_text),
                    "authoritative_candidate_chunk_text": str(chunk_text),
                    "relevance_score": float(semantic_candidate["relevance_score"]),
                    "semantic_similarity": float(
                        semantic_candidate["semantic_similarity"]
                    ),
                    "confidence_class": str(item["confidence_class"]),
                    "qualified_count_v1": int(item["qualified_count_v1"]),
                    "structural_candidate_count": int(item["structural_candidate_count"]),
                    "l1_structural_guards_pass": bool(
                        shadow_candidate["l1_structural_guards_pass"]
                    ),
                    "qualified_v1": bool(shadow_candidate["qualified_v1"]),
                    "relevance_label": None,
                    "reviewer_notes": "",
                }
            )

    pairs.sort(key=lambda row: (row["question_version_id"], row["candidate_identity"]))
    return pairs


def build_packet_payload(
    *,
    config: RelevanceReviewPacketConfig,
    plan: RelevanceReviewPacketPlan,
    replay_content_set_hash: str,
    pairs: Sequence[Mapping[str, Any]],
    cache_metrics: Mapping[str, int],
    provider_request_count: int,
) -> dict[str, Any]:
    assert config.model_name is not None
    assert config.model_version is not None
    assert config.dimensions is not None

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    body = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "generated_at": generated_at,
        "replay_content_set_hash": replay_content_set_hash,
        "model_name": config.model_name,
        "model_version": config.model_version,
        "dimensions": int(config.dimensions),
        "question_count": plan.question_count,
        "semantic_review_question_count": plan.semantic_review_question_count,
        "pair_count": len(pairs),
        "candidate_limit": plan.candidate_limit,
        "provider_request_count": int(provider_request_count),
        "cache_only": True,
        "cache_hit_count": int(cache_metrics.get("cache_hit_count", 0)),
        "cache_miss_count": int(cache_metrics.get("cache_miss_count", 0)),
        "allowed_relevance_labels": list(ALLOWED_RELEVANCE_LABELS),
        "pairs": list(pairs),
    }
    hash_input = {
        key: value
        for key, value in body.items()
        if key not in {"generated_at", "packet_hash"}
    }
    body["packet_hash"] = compute_packet_hash(hash_input)
    return body


def run_build_relevance_review_packet(
    config: RelevanceReviewPacketConfig,
    *,
    env: Optional[Mapping[str, str]] = None,
    fixture: Optional[Mapping[str, Any]] = None,
    client_factory: Optional[Callable[[], Any]] = None,
    resolver_factory: Optional[
        Callable[[Any, Mapping[str, Any], int], AuthoritativeEmbeddingTextResolver]
    ] = None,
) -> dict[str, Any]:
    environment = dict(os.environ if env is None else env)
    loaded_fixture = fixture if fixture is not None else load_frozen_replay_fixture(
        fixture_path=config.fixture_path
    )
    plan = compute_review_packet_plan(
        loaded_fixture,
        candidate_limit=config.candidate_limit,
    )

    if not config.execute:
        return {
            "final_status": "planned",
            "question_count": plan.question_count,
            "semantic_review_question_count": plan.semantic_review_question_count,
            "pair_count": plan.pair_count,
            "candidate_limit": plan.candidate_limit,
            "reference_replay_content_set_hash": plan.reference_replay_content_set_hash,
            "cache_only": True,
            "provider_request_count": 0,
        }

    validate_execute_configuration(config, env=environment)

    model_name = str(config.model_name)
    model_version = str(config.model_version)
    dimensions = int(config.dimensions)
    execute_config = config

    if client_factory is None:
        from utils.access_control import create_supabase_admin_client

        client = create_supabase_admin_client()
    else:
        client = client_factory()

    if resolver_factory is None:
        resolver = build_supabase_authoritative_embedding_text_resolver(
            client,
            loaded_fixture,
            candidate_limit=execute_config.candidate_limit,
        )
    else:
        resolver = resolver_factory(
            client,
            loaded_fixture,
            execute_config.candidate_limit,
        )
    assert_execute_resolver_is_authoritative(resolver)
    resolver.prepare()

    replay_content_set_hash = resolver.replay_content_set_hash
    expected_hash = str(execute_config.expected_content_set_hash or "").strip().lower()
    if replay_content_set_hash != expected_hash:
        raise RelevanceReviewPacketContentSetError(
            "resolved replay_content_set_hash does not match "
            f"--expected-content-set-hash ({replay_content_set_hash!r} != {expected_hash!r})"
        )

    provider = CacheOnlyForbiddenProvider()
    repository = SupabaseEmbeddingCacheRepository(client)
    try:
        replay_result = run_hybrid_replay_from_records(
            replay_records=loaded_fixture["questions"],
            candidate_limit=execute_config.candidate_limit,
            embedding_text_resolver=resolver,
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name=model_name,
            embedding_model_version=model_version,
            embedding_dimensions=dimensions,
            repository=repository,
            provider=provider,
        )
    except HybridReplayError as exc:
        if provider.provider_request_count > 0:
            raise RelevanceReviewPacketCacheError(
                "cache-only execution attempted a provider embedding request"
            ) from exc
        raise RelevanceReviewPacketCacheError(
            "cache-only semantic scoring failed; verify durable cache rows exist"
        ) from exc

    if provider.provider_request_count > 0:
        raise RelevanceReviewPacketCacheError(
            "cache-only execution attempted a provider embedding request"
        )

    pairs = build_review_pairs(
        fixture=loaded_fixture,
        replay_result=replay_result,
        resolver=resolver,
        candidate_limit=execute_config.candidate_limit,
    )
    if len(pairs) != plan.pair_count:
        raise RelevanceReviewPacketError(
            f"expected {plan.pair_count} review pairs; built {len(pairs)}"
        )

    cache_metrics = _aggregate_cache_metrics(replay_result)
    if cache_metrics["cache_miss_count"] > 0:
        raise RelevanceReviewPacketCacheError(
            "cache-only execution encountered cache misses; "
            f"cache_miss_count={cache_metrics['cache_miss_count']}"
        )

    output_path = validate_output_path(
        execute_config.output_path,
        overwrite=execute_config.overwrite,
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    packet = build_packet_payload(
        config=execute_config,
        plan=plan,
        replay_content_set_hash=replay_content_set_hash,
        pairs=pairs,
        cache_metrics=cache_metrics,
        provider_request_count=provider.provider_request_count,
    )
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(packet, handle, sort_keys=True, indent=2, ensure_ascii=True)
        handle.write("\n")

    return {
        "final_status": "success",
        "output_path": output_path,
        "packet_hash": packet["packet_hash"],
        "replay_content_set_hash": replay_content_set_hash,
        "question_count": plan.question_count,
        "semantic_review_question_count": plan.semantic_review_question_count,
        "pair_count": len(pairs),
        "cache_hit_count": cache_metrics["cache_hit_count"],
        "cache_miss_count": cache_metrics["cache_miss_count"],
        "provider_request_count": provider.provider_request_count,
        "cache_only": True,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        config = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 1)

    loaded_fixture = load_frozen_replay_fixture(fixture_path=config.fixture_path)
    plan = compute_review_packet_plan(
        loaded_fixture,
        candidate_limit=config.candidate_limit,
    )

    if not config.execute:
        print(format_dry_run_plan(config, plan=plan))
        return 0

    try:
        summary = run_build_relevance_review_packet(config, env=os.environ, fixture=loaded_fixture)
    except RelevanceReviewPacketError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_redacted_console_summary(summary))
    return 0 if summary.get("final_status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
