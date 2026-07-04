#!/usr/bin/env python3
"""
Manual smoke runner for V48 OpenAI embedding provider + Supabase cache integration.

Default mode is dry-run (no HTTP or Supabase calls). Real execution requires an
explicit --execute flag plus model parameters and configured environment secrets.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from workers.embedding_cache import (  # noqa: E402
    CONTENT_SCOPE_CHUNK,
    CONTENT_SCOPE_QUERY,
    EmbeddingCacheIdentity,
    EmbeddingCacheReadError,
    EmbeddingCacheRepository,
    SupabaseEmbeddingCacheRepository,
    TABLE_NAME,
    build_cache_identity,
    get_or_compute_embedding,
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

DEFAULT_TIMEOUT_SECONDS = 30.0

# retrieval_embedding_cache.embedding_vector is PostgreSQL double precision[] (float8[]).
# On cache miss the service returns the in-memory provider tuple; on cache hit it
# reloads the persisted float8[] via Supabase JSON. Finite float8 round-trip may
# differ slightly from the first in-memory tuple without indicating cache corruption.
FLOAT8_ARRAY_STORAGE_ABS_TOL = 1e-12


class EmbeddingCacheSmokeError(RuntimeError):
    """Base error for the embedding-cache smoke runner."""


class EmbeddingCacheSmokeConfigError(EmbeddingCacheSmokeError):
    """Raised when CLI configuration or execution gates are invalid."""


class EmbeddingCacheSmokeEnvironmentError(EmbeddingCacheSmokeError):
    """Raised when required execution environment variables are missing."""


class EmbeddingCacheSmokeProviderError(EmbeddingCacheSmokeError):
    """Raised when the embedding provider fails during smoke execution."""


class EmbeddingCacheSmokeCacheError(EmbeddingCacheSmokeError):
    """Raised when cache read, insert, or verification fails."""


class EmbeddingCacheSmokeUnexpectedCacheHitError(EmbeddingCacheSmokeCacheError):
    """Raised when the first lookup unexpectedly hits the cache."""


class EmbeddingCacheSmokeUnexpectedCacheMissError(EmbeddingCacheSmokeCacheError):
    """Raised when a repeated lookup unexpectedly misses the cache."""


class EmbeddingCacheSmokeConsistencyError(EmbeddingCacheSmokeCacheError):
    """Raised when repeated cache reads disagree on vector or response hash."""


class EmbeddingCacheSmokeCleanupError(EmbeddingCacheSmokeError):
    """Raised when smoke cache-row cleanup or post-delete verification fails."""


@dataclass(frozen=True)
class EmbeddingCacheSmokeConfig:
    execute: bool
    model_name: Optional[str]
    model_version: Optional[str]
    dimensions: Optional[int]
    timeout_seconds: float
    smoke_run_id: str


def parse_args(argv: Optional[Sequence[str]] = None) -> EmbeddingCacheSmokeConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute a two-request V48 embedding-cache smoke run using "
            "synthetic text only."
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
        help="Embedding model version label stored in cache identity (required with --execute)",
    )
    parser.add_argument(
        "--dimensions",
        type=int,
        default=None,
        help="Requested embedding dimensions (required with --execute)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout for provider requests (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--smoke-run-id",
        default=None,
        help="Optional smoke-run UUID; defaults to a newly generated UUID",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    smoke_run_id = str(args.smoke_run_id or uuid.uuid4())
    return EmbeddingCacheSmokeConfig(
        execute=bool(args.execute),
        model_name=str(args.model).strip() if args.model is not None else None,
        model_version=str(args.model_version).strip()
        if args.model_version is not None
        else None,
        dimensions=int(args.dimensions) if args.dimensions is not None else None,
        timeout_seconds=float(args.timeout_seconds),
        smoke_run_id=smoke_run_id,
    )


def build_synthetic_smoke_texts(smoke_run_id: str) -> tuple[str, str]:
    """Return synthetic query and chunk texts keyed by smoke_run_id."""
    normalized = str(smoke_run_id).strip().lower()
    if not normalized:
        raise EmbeddingCacheSmokeConfigError("smoke_run_id must be nonempty")
    return (
        f"v48-embedding-cache-smoke-query:{normalized}",
        f"v48-embedding-cache-smoke-chunk:{normalized}",
    )


def validate_execute_configuration(
    config: EmbeddingCacheSmokeConfig,
    *,
    env: Mapping[str, str],
) -> None:
    if not config.execute:
        raise EmbeddingCacheSmokeConfigError(
            "execution confirmation is required via --execute"
        )
    if not config.model_name:
        raise EmbeddingCacheSmokeConfigError("--model is required when --execute is supplied")
    if not config.model_version:
        raise EmbeddingCacheSmokeConfigError(
            "--model-version is required when --execute is supplied"
        )
    if config.dimensions is None:
        raise EmbeddingCacheSmokeConfigError(
            "--dimensions is required when --execute is supplied"
        )
    if config.dimensions <= 0:
        raise EmbeddingCacheSmokeConfigError("--dimensions must be a positive integer")
    if config.timeout_seconds <= 0:
        raise EmbeddingCacheSmokeConfigError("--timeout-seconds must be positive")

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
        raise EmbeddingCacheSmokeEnvironmentError(
            "missing required environment variables: " + ", ".join(sorted(missing_env))
        )


def format_dry_run_plan(config: EmbeddingCacheSmokeConfig) -> str:
    lines = [
        "V48 embedding cache smoke dry-run",
        f"smoke_run_id: {config.smoke_run_id}",
        "execute: false",
        f"provider_name: {OPENAI_PROVIDER_NAME}",
        f"model_name: {config.model_name or '(not supplied)'}",
        f"model_version: {config.model_version or '(not supplied)'}",
        f"dimensions: {config.dimensions if config.dimensions is not None else '(not supplied)'}",
        f"timeout_seconds: {config.timeout_seconds}",
        "required_for_execute:",
        "  - --execute",
        "  - --model",
        "  - --model-version",
        "  - --dimensions",
        f"  - {ENV_OPENAI_API_KEY}",
        f"  - {ENV_SUPABASE_URL}",
        f"  - {ENV_SUPABASE_SERVICE_ROLE_KEY}",
        "external_calls_planned: 0",
        "provider_requests_planned: 0",
        "cache_rows_created_planned: 0",
        "cache_rows_deleted_planned: 0",
        "No external calls performed (dry-run).",
    ]
    return "\n".join(lines)


def format_redacted_summary(summary: Mapping[str, Any]) -> str:
    payload = {
        "smoke_run_id": summary["smoke_run_id"],
        "provider_name": summary["provider_name"],
        "model_name": summary["model_name"],
        "model_version": summary["model_version"],
        "dimensions": summary["dimensions"],
        "first_query_cache_hit": summary["first_query_cache_hit"],
        "first_chunk_cache_hit": summary["first_chunk_cache_hit"],
        "repeated_query_cache_hit": summary["repeated_query_cache_hit"],
        "repeated_chunk_cache_hit": summary["repeated_chunk_cache_hit"],
        "cleanup_succeeded": summary["cleanup_succeeded"],
        "final_status": summary["final_status"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_initial_summary(config: EmbeddingCacheSmokeConfig) -> dict[str, Any]:
    return {
        "smoke_run_id": config.smoke_run_id,
        "provider_name": OPENAI_PROVIDER_NAME,
        "model_name": config.model_name or "",
        "model_version": config.model_version or "",
        "dimensions": config.dimensions if config.dimensions is not None else 0,
        "first_query_cache_hit": None,
        "first_chunk_cache_hit": None,
        "repeated_query_cache_hit": None,
        "repeated_chunk_cache_hit": None,
        "cleanup_succeeded": False,
        "final_status": "planned" if not config.execute else "failed",
    }


def delete_cache_row_by_identity(client: Any, identity: EmbeddingCacheIdentity) -> int:
    """Delete exactly one cache row matching the full cache identity."""
    query = client.table(TABLE_NAME).delete()
    for field_name, value in (
        ("content_scope", identity.content_scope),
        ("content_hash", identity.content_hash),
        ("embedding_provider_name", identity.embedding_provider_name),
        ("embedding_model_name", identity.embedding_model_name),
        ("embedding_model_version", identity.embedding_model_version),
        ("embedding_dimensions", identity.embedding_dimensions),
    ):
        query = query.eq(field_name, value)
    response = query.execute()
    deleted_rows = getattr(response, "data", None) or []
    return len(deleted_rows)


def cleanup_smoke_cache_rows(
    *,
    client: Any,
    repository: EmbeddingCacheRepository,
    identities: Sequence[EmbeddingCacheIdentity],
) -> None:
    if len(identities) != 2:
        raise EmbeddingCacheSmokeCleanupError(
            "smoke cleanup requires exactly two cache identities"
        )

    cleanup_errors: list[str] = []
    for identity in identities:
        try:
            deleted_count = delete_cache_row_by_identity(client, identity)
            if deleted_count == 0:
                remaining = repository.lookup(identity)
                if remaining is not None:
                    cleanup_errors.append(
                        "delete matched zero rows for "
                        f"content_hash={identity.content_hash}"
                    )
            elif deleted_count > 1:
                cleanup_errors.append(
                    "delete matched more than one row for "
                    f"content_hash={identity.content_hash}"
                )
        except Exception as exc:
            cleanup_errors.append(
                f"delete failed for content_hash={identity.content_hash}: {type(exc).__name__}"
            )

    for identity in identities:
        try:
            remaining = repository.lookup(identity)
        except Exception as exc:
            cleanup_errors.append(
                "post-delete lookup failed for "
                f"content_hash={identity.content_hash}: {type(exc).__name__}"
            )
            continue
        if remaining is not None:
            cleanup_errors.append(
                "cache row still present after delete for "
                f"content_hash={identity.content_hash}"
            )

    if cleanup_errors:
        raise EmbeddingCacheSmokeCleanupError("; ".join(cleanup_errors))


def run_embedding_cache_smoke(
    config: EmbeddingCacheSmokeConfig,
    *,
    env: Optional[Mapping[str, str]] = None,
    client_factory: Optional[Callable[[], Any]] = None,
    transport_factory: Callable[[], StdlibEmbeddingHttpTransport] = StdlibEmbeddingHttpTransport,
    summary: Optional[MutableMapping[str, Any]] = None,
) -> dict[str, Any]:
    """Run the smoke flow or return a dry-run summary without external calls."""
    environment = dict(os.environ if env is None else env)
    if summary is not None:
        result_summary = summary
    else:
        result_summary = build_initial_summary(config)

    if not config.execute:
        return result_summary

    validate_execute_configuration(config, env=environment)

    assert config.model_name is not None
    assert config.model_version is not None
    assert config.dimensions is not None

    query_text, chunk_text = build_synthetic_smoke_texts(config.smoke_run_id)
    identities_for_cleanup: list[EmbeddingCacheIdentity] = []
    client: Any = None
    repository: Optional[EmbeddingCacheRepository] = None
    execution_error: Optional[EmbeddingCacheSmokeError] = None

    try:
        if client_factory is None:
            from utils.access_control import create_supabase_admin_client

            client = create_supabase_admin_client()
        else:
            client = client_factory()

        transport = transport_factory()
        provider = OpenAIEmbeddingProvider(
            config=OpenAIEmbeddingProviderConfig(
                api_key=str(environment[ENV_OPENAI_API_KEY]).strip(),
                timeout_seconds=config.timeout_seconds,
            ),
            transport=transport,
        )
        repository = SupabaseEmbeddingCacheRepository(client)

        shared_identity_kwargs = {
            "embedding_provider_name": OPENAI_PROVIDER_NAME,
            "embedding_model_name": config.model_name,
            "embedding_model_version": config.model_version,
            "embedding_dimensions": config.dimensions,
        }
        compute_kwargs = {
            **shared_identity_kwargs,
            "repository": repository,
            "provider": provider,
        }

        query_identity = build_cache_identity(
            text=query_text,
            content_scope=CONTENT_SCOPE_QUERY,
            **shared_identity_kwargs,
        )
        chunk_identity = build_cache_identity(
            text=chunk_text,
            content_scope=CONTENT_SCOPE_CHUNK,
            **shared_identity_kwargs,
        )
        identities_for_cleanup = [query_identity, chunk_identity]

        first_query = get_or_compute_embedding(
            text=query_text,
            content_scope=CONTENT_SCOPE_QUERY,
            **compute_kwargs,
        )
        if first_query.cache_hit:
            raise EmbeddingCacheSmokeUnexpectedCacheHitError(
                "first query embedding lookup unexpectedly hit the cache"
            )
        result_summary["first_query_cache_hit"] = False

        first_chunk = get_or_compute_embedding(
            text=chunk_text,
            content_scope=CONTENT_SCOPE_CHUNK,
            **compute_kwargs,
        )
        if first_chunk.cache_hit:
            raise EmbeddingCacheSmokeUnexpectedCacheHitError(
                "first chunk embedding lookup unexpectedly hit the cache"
            )
        result_summary["first_chunk_cache_hit"] = False

        repeated_query = get_or_compute_embedding(
            text=query_text,
            content_scope=CONTENT_SCOPE_QUERY,
            **compute_kwargs,
        )
        if not repeated_query.cache_hit:
            raise EmbeddingCacheSmokeUnexpectedCacheMissError(
                "repeated query embedding lookup unexpectedly missed the cache"
            )
        result_summary["repeated_query_cache_hit"] = True

        repeated_chunk = get_or_compute_embedding(
            text=chunk_text,
            content_scope=CONTENT_SCOPE_CHUNK,
            **compute_kwargs,
        )
        if not repeated_chunk.cache_hit:
            raise EmbeddingCacheSmokeUnexpectedCacheMissError(
                "repeated chunk embedding lookup unexpectedly missed the cache"
            )
        result_summary["repeated_chunk_cache_hit"] = True

        _assert_repeated_results_consistent(
            first=first_query.record,
            repeated=repeated_query.record,
            label="query",
            dimensions=config.dimensions,
        )
        _assert_repeated_results_consistent(
            first=first_chunk.record,
            repeated=repeated_chunk.record,
            label="chunk",
            dimensions=config.dimensions,
        )

        result_summary["final_status"] = "success"
    except EmbeddingCacheSmokeError as exc:
        execution_error = exc
    except EmbeddingProviderError as exc:
        execution_error = EmbeddingCacheSmokeProviderError(
            "embedding provider failed during smoke execution"
        )
        execution_error.__cause__ = exc
    except EmbeddingCacheReadError as exc:
        execution_error = EmbeddingCacheSmokeCacheError(
            "embedding cache read failed during smoke execution"
        )
        execution_error.__cause__ = exc
    except Exception as exc:
        execution_error = EmbeddingCacheSmokeCacheError(
            "embedding cache smoke execution failed"
        )
        execution_error.__cause__ = exc
    finally:
        if client is not None and repository is not None and identities_for_cleanup:
            try:
                cleanup_smoke_cache_rows(
                    client=client,
                    repository=repository,
                    identities=identities_for_cleanup,
                )
                result_summary["cleanup_succeeded"] = True
            except EmbeddingCacheSmokeCleanupError as exc:
                result_summary["cleanup_succeeded"] = False
                if execution_error is None:
                    execution_error = exc

    if execution_error is not None:
        raise execution_error
    return result_summary


def max_absolute_vector_difference(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    """Return the maximum absolute component difference between two vectors."""
    if len(left) != len(right):
        raise EmbeddingCacheSmokeConsistencyError(
            "vector length mismatch during float8 round-trip comparison"
        )
    return max(abs(float(left_value) - float(right_value)) for left_value, right_value in zip(left, right))


def assert_vectors_equivalent_within_float8_round_trip(
    *,
    left: Sequence[float],
    right: Sequence[float],
    label: str,
    dimensions: int,
) -> None:
    """Assert two vectors match within float8[] storage round-trip tolerance."""
    if len(left) != dimensions:
        raise EmbeddingCacheSmokeConsistencyError(
            f"{label} embedding vector length does not match requested dimensions"
        )
    if len(right) != dimensions:
        raise EmbeddingCacheSmokeConsistencyError(
            f"repeated {label} embedding vector length does not match requested dimensions"
        )

    for index, (left_value, right_value) in enumerate(zip(left, right)):
        left_component = float(left_value)
        right_component = float(right_value)
        if math.isnan(left_component) or math.isinf(left_component):
            raise EmbeddingCacheSmokeConsistencyError(
                f"{label} embedding vector contains non-finite value at index {index}"
            )
        if math.isnan(right_component) or math.isinf(right_component):
            raise EmbeddingCacheSmokeConsistencyError(
                f"repeated {label} embedding vector contains non-finite value at index {index}"
            )

    max_abs_diff = max_absolute_vector_difference(left, right)
    if max_abs_diff > FLOAT8_ARRAY_STORAGE_ABS_TOL:
        raise EmbeddingCacheSmokeConsistencyError(
            f"repeated {label} embedding vector differs from first result "
            f"beyond float8[] storage tolerance "
            f"(max_abs_diff={max_abs_diff:.3e}, "
            f"abs_tol={FLOAT8_ARRAY_STORAGE_ABS_TOL:.3e})"
        )


def _assert_repeated_results_consistent(
    *,
    first: Any,
    repeated: Any,
    label: str,
    dimensions: int,
) -> None:
    assert_vectors_equivalent_within_float8_round_trip(
        left=first.embedding_vector,
        right=repeated.embedding_vector,
        label=label,
        dimensions=dimensions,
    )
    if first.provider_response_hash != repeated.provider_response_hash:
        raise EmbeddingCacheSmokeConsistencyError(
            f"repeated {label} provider_response_hash differs from first result"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        config = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 1)

    if not config.execute:
        print(format_dry_run_plan(config))
        return 0

    summary = build_initial_summary(config)
    try:
        run_embedding_cache_smoke(config, summary=summary)
    except EmbeddingCacheSmokeError as exc:
        print(format_redacted_summary(summary), file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_redacted_summary(summary))
    return 0 if summary.get("final_status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
