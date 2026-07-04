"""Provider-agnostic durable embedding cache for hybrid_question_match_v2.

Supports deterministic content hashing, cache lookup by full identity, provider
calls on miss, strict response validation, insert-only persistence, and
race-safe concurrent misses. Not wired into live retrieval or audit workers.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8 fallback
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

from workers.resource_chunking import sha256_hex

CONTENT_SCOPE_QUERY = "query"
CONTENT_SCOPE_CHUNK = "chunk"
VALID_CONTENT_SCOPES = frozenset({CONTENT_SCOPE_QUERY, CONTENT_SCOPE_CHUNK})

TABLE_NAME = "retrieval_embedding_cache"
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_LOGGER = logging.getLogger(__name__)


class EmbeddingCacheError(RuntimeError):
    """Base error for embedding-cache operations."""


class InvalidProviderResponseError(EmbeddingCacheError):
    """Raised when an embedding provider returns an invalid vector or hash."""


class EmbeddingCacheReadError(EmbeddingCacheError):
    """Raised when a cache lookup fails unexpectedly."""


class EmbeddingCacheInsertError(EmbeddingCacheError):
    """Raised when a cache insert fails and no winning row can be recovered."""


class EmbeddingCacheUniqueViolationError(EmbeddingCacheInsertError):
    """Raised when insert loses a uniqueness race; service re-reads the winner."""


class EmbeddingCacheConflictError(EmbeddingCacheError):
    """Raised when a cached row contradicts the requested cache identity."""


@dataclass(frozen=True)
class EmbeddingCacheIdentity:
    content_scope: str
    content_hash: str
    embedding_provider_name: str
    embedding_model_name: str
    embedding_model_version: str
    embedding_dimensions: int

    def as_tuple(self) -> Tuple[str, str, str, str, str, int]:
        return (
            self.content_scope,
            self.content_hash,
            self.embedding_provider_name,
            self.embedding_model_name,
            self.embedding_model_version,
            self.embedding_dimensions,
        )


@dataclass(frozen=True)
class EmbeddingCacheRecord:
    content_scope: str
    content_hash: str
    embedding_provider_name: str
    embedding_model_name: str
    embedding_model_version: str
    embedding_dimensions: int
    embedding_vector: Tuple[float, ...]
    provider_response_hash: str


@dataclass(frozen=True)
class EmbeddingCacheLookupResult:
    record: EmbeddingCacheRecord
    cache_hit: bool


@dataclass(frozen=True)
class EmbeddingProviderResponse:
    embedding_vector: Tuple[float, ...]
    provider_response_hash: str


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Structural interface for embedding providers."""

    def embed(
        self,
        *,
        text: str,
        embedding_provider_name: str,
        embedding_model_name: str,
        embedding_model_version: str,
        embedding_dimensions: int,
    ) -> EmbeddingProviderResponse:
        """Return one validated embedding vector for the exact input text."""


@runtime_checkable
class EmbeddingCacheRepository(Protocol):
    """Structural interface for retrieval_embedding_cache persistence."""

    def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
        """Return the cached row for identity, or None on miss."""

    def insert(self, record: EmbeddingCacheRecord) -> None:
        """Insert one immutable cache row. Never updates existing rows."""


def hash_embedding_input(text: str) -> str:
    """Return lowercase SHA-256 hex digest of exact embedding input text."""
    return sha256_hex(text)


def build_cache_identity(
    *,
    text: str,
    content_scope: str,
    embedding_provider_name: str,
    embedding_model_name: str,
    embedding_model_version: str,
    embedding_dimensions: int,
) -> EmbeddingCacheIdentity:
    """Build the full cache identity for one embedding input."""
    _validate_content_scope(content_scope)
    if embedding_dimensions <= 0:
        raise ValueError("embedding_dimensions must be positive")
    for field_name, value in (
        ("embedding_provider_name", embedding_provider_name),
        ("embedding_model_name", embedding_model_name),
        ("embedding_model_version", embedding_model_version),
    ):
        if not str(value).strip():
            raise ValueError(f"{field_name} must be nonempty")

    content_hash = hash_embedding_input(text)
    if not SHA256_HEX_RE.fullmatch(content_hash):
        raise ValueError("content_hash must be lowercase SHA-256 hex")

    return EmbeddingCacheIdentity(
        content_scope=content_scope,
        content_hash=content_hash,
        embedding_provider_name=str(embedding_provider_name).strip(),
        embedding_model_name=str(embedding_model_name).strip(),
        embedding_model_version=str(embedding_model_version).strip(),
        embedding_dimensions=int(embedding_dimensions),
    )


def validate_provider_response(
    response: EmbeddingProviderResponse,
    *,
    embedding_dimensions: int,
) -> Tuple[float, ...]:
    """Validate provider output and return an immutable vector tuple."""
    vector = _coerce_vector_tuple(response.embedding_vector)
    _validate_vector(vector, embedding_dimensions=embedding_dimensions)

    provider_response_hash = str(response.provider_response_hash or "").strip()
    if not provider_response_hash:
        raise InvalidProviderResponseError("provider_response_hash must be nonempty")
    if not SHA256_HEX_RE.fullmatch(provider_response_hash):
        raise InvalidProviderResponseError(
            "provider_response_hash must be lowercase SHA-256 hex"
        )

    return vector


def get_or_compute_embedding(
    *,
    text: str,
    content_scope: str,
    embedding_provider_name: str,
    embedding_model_name: str,
    embedding_model_version: str,
    embedding_dimensions: int,
    repository: EmbeddingCacheRepository,
    provider: EmbeddingProvider,
    logger: Optional[logging.Logger] = None,
) -> EmbeddingCacheLookupResult:
    """Look up a cached embedding or compute, validate, and insert on miss."""
    log = logger or _LOGGER
    identity = build_cache_identity(
        text=text,
        content_scope=content_scope,
        embedding_provider_name=embedding_provider_name,
        embedding_model_name=embedding_model_name,
        embedding_model_version=embedding_model_version,
        embedding_dimensions=embedding_dimensions,
    )

    _log_cache_event(
        log,
        event="lookup",
        identity=identity,
    )

    cached = _lookup_record(repository, identity)
    if cached is not None:
        _log_cache_event(
            log,
            event="hit",
            identity=identity,
            cache_hit=True,
        )
        return EmbeddingCacheLookupResult(record=cached, cache_hit=True)

    provider_response = provider.embed(
        text=text,
        embedding_provider_name=identity.embedding_provider_name,
        embedding_model_name=identity.embedding_model_name,
        embedding_model_version=identity.embedding_model_version,
        embedding_dimensions=identity.embedding_dimensions,
    )
    vector = validate_provider_response(
        provider_response,
        embedding_dimensions=identity.embedding_dimensions,
    )
    candidate = EmbeddingCacheRecord(
        content_scope=identity.content_scope,
        content_hash=identity.content_hash,
        embedding_provider_name=identity.embedding_provider_name,
        embedding_model_name=identity.embedding_model_name,
        embedding_model_version=identity.embedding_model_version,
        embedding_dimensions=identity.embedding_dimensions,
        embedding_vector=vector,
        provider_response_hash=str(provider_response.provider_response_hash).strip(),
    )

    stored = _insert_or_fetch_winner(repository, candidate)
    _log_cache_event(
        log,
        event="stored",
        identity=identity,
        cache_hit=False,
    )
    return EmbeddingCacheLookupResult(record=stored, cache_hit=False)


class SupabaseEmbeddingCacheRepository:
    """Service-role Supabase persistence for retrieval_embedding_cache."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
        try:
            response = (
                self._client.table(TABLE_NAME)
                .select(
                    "content_scope,content_hash,embedding_provider_name,"
                    "embedding_model_name,embedding_model_version,"
                    "embedding_dimensions,embedding_vector,provider_response_hash"
                )
                .eq("content_scope", identity.content_scope)
                .eq("content_hash", identity.content_hash)
                .eq("embedding_provider_name", identity.embedding_provider_name)
                .eq("embedding_model_name", identity.embedding_model_name)
                .eq("embedding_model_version", identity.embedding_model_version)
                .eq("embedding_dimensions", identity.embedding_dimensions)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise EmbeddingCacheReadError(
                "embedding cache lookup failed for content_hash="
                f"{identity.content_hash}"
            ) from exc

        rows = getattr(response, "data", None) or []
        if not rows:
            return None
        return record_from_row(rows[0], expected_identity=identity)

    def insert(self, record: EmbeddingCacheRecord) -> None:
        payload = record_to_payload(record)
        try:
            self._client.table(TABLE_NAME).insert(payload).execute()
        except Exception as exc:
            if _is_unique_violation(exc):
                raise EmbeddingCacheUniqueViolationError(
                    "embedding cache insert lost uniqueness race for content_hash="
                    f"{record.content_hash}"
                ) from exc
            raise EmbeddingCacheInsertError(
                "embedding cache insert failed for content_hash="
                f"{record.content_hash}"
            ) from exc


def record_to_payload(record: EmbeddingCacheRecord) -> dict[str, Any]:
    return {
        "content_scope": record.content_scope,
        "content_hash": record.content_hash,
        "embedding_provider_name": record.embedding_provider_name,
        "embedding_model_name": record.embedding_model_name,
        "embedding_model_version": record.embedding_model_version,
        "embedding_dimensions": record.embedding_dimensions,
        "embedding_vector": list(record.embedding_vector),
        "provider_response_hash": record.provider_response_hash,
    }


def record_from_row(
    row: Mapping[str, Any],
    *,
    expected_identity: EmbeddingCacheIdentity,
) -> EmbeddingCacheRecord:
    record = EmbeddingCacheRecord(
        content_scope=str(row["content_scope"]),
        content_hash=str(row["content_hash"]),
        embedding_provider_name=str(row["embedding_provider_name"]),
        embedding_model_name=str(row["embedding_model_name"]),
        embedding_model_version=str(row["embedding_model_version"]),
        embedding_dimensions=int(row["embedding_dimensions"]),
        embedding_vector=_coerce_vector_tuple(row["embedding_vector"]),
        provider_response_hash=str(row["provider_response_hash"]),
    )
    _assert_record_matches_identity(record, expected_identity)
    _validate_vector(record.embedding_vector, embedding_dimensions=record.embedding_dimensions)
    return record


def _lookup_record(
    repository: EmbeddingCacheRepository,
    identity: EmbeddingCacheIdentity,
) -> Optional[EmbeddingCacheRecord]:
    try:
        record = repository.lookup(identity)
    except EmbeddingCacheConflictError:
        raise
    except Exception as exc:
        raise EmbeddingCacheReadError(
            "embedding cache lookup failed for content_hash="
            f"{identity.content_hash}"
        ) from exc

    if record is None:
        return None
    _assert_record_matches_identity(record, identity)
    return record


def _insert_or_fetch_winner(
    repository: EmbeddingCacheRepository,
    candidate: EmbeddingCacheRecord,
) -> EmbeddingCacheRecord:
    identity = EmbeddingCacheIdentity(
        content_scope=candidate.content_scope,
        content_hash=candidate.content_hash,
        embedding_provider_name=candidate.embedding_provider_name,
        embedding_model_name=candidate.embedding_model_name,
        embedding_model_version=candidate.embedding_model_version,
        embedding_dimensions=candidate.embedding_dimensions,
    )
    try:
        repository.insert(candidate)
        return candidate
    except EmbeddingCacheUniqueViolationError:
        winner = _lookup_record(repository, identity)
        if winner is None:
            raise EmbeddingCacheInsertError(
                "embedding cache insert conflict could not be resolved for "
                f"content_hash={identity.content_hash}"
            )
        _assert_record_matches_candidate(winner, candidate)
        return winner
    except EmbeddingCacheInsertError:
        raise
    except Exception as exc:
        raise EmbeddingCacheInsertError(
            "embedding cache insert failed for content_hash="
            f"{identity.content_hash}"
        ) from exc


def _assert_record_matches_identity(
    record: EmbeddingCacheRecord,
    identity: EmbeddingCacheIdentity,
) -> None:
    mismatches = []
    for field_name in (
        "content_scope",
        "content_hash",
        "embedding_provider_name",
        "embedding_model_name",
        "embedding_model_version",
    ):
        if getattr(record, field_name) != getattr(identity, field_name):
            mismatches.append(field_name)
    if record.embedding_dimensions != identity.embedding_dimensions:
        mismatches.append("embedding_dimensions")
    if mismatches:
        raise EmbeddingCacheConflictError(
            "cached embedding identity mismatch for content_hash="
            f"{identity.content_hash}: {', '.join(mismatches)}"
        )


def _assert_record_matches_candidate(
    winner: EmbeddingCacheRecord,
    candidate: EmbeddingCacheRecord,
) -> None:
    """Ensure a race-winning row matches the losing caller's computed result."""
    identity = EmbeddingCacheIdentity(
        content_scope=candidate.content_scope,
        content_hash=candidate.content_hash,
        embedding_provider_name=candidate.embedding_provider_name,
        embedding_model_name=candidate.embedding_model_name,
        embedding_model_version=candidate.embedding_model_version,
        embedding_dimensions=candidate.embedding_dimensions,
    )
    _assert_record_matches_identity(winner, identity)
    if winner.provider_response_hash != candidate.provider_response_hash:
        raise EmbeddingCacheConflictError(
            "cached provider_response_hash mismatch for content_hash="
            f"{candidate.content_hash}"
        )
    if winner.embedding_vector != candidate.embedding_vector:
        raise EmbeddingCacheConflictError(
            "cached embedding_vector mismatch for content_hash="
            f"{candidate.content_hash}"
        )


def _validate_content_scope(content_scope: str) -> None:
    if content_scope not in VALID_CONTENT_SCOPES:
        raise ValueError(
            f"content_scope must be one of {sorted(VALID_CONTENT_SCOPES)}"
        )


def _coerce_vector_tuple(values: Any) -> Tuple[float, ...]:
    if values is None:
        raise InvalidProviderResponseError("embedding_vector must not be null")
    if isinstance(values, (str, bytes, dict)):
        raise InvalidProviderResponseError("embedding_vector must be one-dimensional")

    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        if values and isinstance(values[0], Sequence) and not isinstance(values[0], (str, bytes)):
            raise InvalidProviderResponseError("embedding_vector must be one-dimensional")
        coerced: list[float] = []
        for item in values:
            if item is None:
                raise InvalidProviderResponseError("embedding_vector must not contain null values")
            try:
                number = float(item)
            except (TypeError, ValueError) as exc:
                raise InvalidProviderResponseError(
                    "embedding_vector must contain only numeric values"
                ) from exc
            coerced.append(number)
        return tuple(coerced)

    raise InvalidProviderResponseError("embedding_vector must be one-dimensional")


def _validate_vector(vector: Tuple[float, ...], *, embedding_dimensions: int) -> None:
    if embedding_dimensions <= 0:
        raise InvalidProviderResponseError("embedding_dimensions must be positive")
    if len(vector) != embedding_dimensions:
        raise InvalidProviderResponseError(
            f"embedding_vector length {len(vector)} does not match "
            f"expected dimensions {embedding_dimensions}"
        )
    for index, value in enumerate(vector):
        if math.isnan(value) or math.isinf(value):
            raise InvalidProviderResponseError(
                f"embedding_vector contains non-finite value at index {index}"
            )


def _is_unique_violation(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code == "23505":
        return True
    message = str(exc).lower()
    return (
        "23505" in message
        or "duplicate key" in message
        or "unique constraint" in message
    )


def _log_cache_event(
    logger: logging.Logger,
    *,
    event: str,
    identity: EmbeddingCacheIdentity,
    cache_hit: Optional[bool] = None,
) -> None:
    extra = f" cache_hit={cache_hit}" if cache_hit is not None else ""
    logger.info(
        "embedding_cache.%s content_scope=%s content_hash=%s provider=%s "
        "model=%s version=%s dimensions=%s%s",
        event,
        identity.content_scope,
        identity.content_hash,
        identity.embedding_provider_name,
        identity.embedding_model_name,
        identity.embedding_model_version,
        identity.embedding_dimensions,
        extra,
    )
