"""Tests for provider-agnostic retrieval embedding cache service."""

from __future__ import annotations

import logging
import os
import sys
import unittest
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.embedding_cache import (
    CONTENT_SCOPE_CHUNK,
    CONTENT_SCOPE_QUERY,
    TABLE_NAME,
    EmbeddingCacheConflictError,
    EmbeddingCacheIdentity,
    EmbeddingCacheInsertError,
    EmbeddingCacheReadError,
    EmbeddingCacheRecord,
    EmbeddingCacheUniqueViolationError,
    EmbeddingProviderResponse,
    InvalidProviderResponseError,
    SupabaseEmbeddingCacheRepository,
    build_cache_identity,
    get_or_compute_embedding,
    hash_embedding_input,
    record_from_row,
    record_to_payload,
    validate_provider_response,
)

_PROVIDER = "fake_provider"
_MODEL = "fake-model"
_VERSION = "v1"
_DIMENSIONS = 3
_RESPONSE_HASH = "a" * 64
_OTHER_RESPONSE_HASH = "b" * 64


def _vector(*values: float) -> Tuple[float, ...]:
    return tuple(float(v) for v in values)


def _identity(
    *,
    text: str,
    content_scope: str = CONTENT_SCOPE_QUERY,
    provider: str = _PROVIDER,
    model: str = _MODEL,
    version: str = _VERSION,
    dimensions: int = _DIMENSIONS,
) -> EmbeddingCacheIdentity:
    return build_cache_identity(
        text=text,
        content_scope=content_scope,
        embedding_provider_name=provider,
        embedding_model_name=model,
        embedding_model_version=version,
        embedding_dimensions=dimensions,
    )


def _record_for_text(
    text: str,
    *,
    content_scope: str = CONTENT_SCOPE_QUERY,
    vector: Tuple[float, ...] = _vector(0.1, 0.2, 0.3),
    provider_response_hash: str = _RESPONSE_HASH,
) -> EmbeddingCacheRecord:
    identity = _identity(text=text, content_scope=content_scope)
    return EmbeddingCacheRecord(
        content_scope=identity.content_scope,
        content_hash=identity.content_hash,
        embedding_provider_name=identity.embedding_provider_name,
        embedding_model_name=identity.embedding_model_name,
        embedding_model_version=identity.embedding_model_version,
        embedding_dimensions=identity.embedding_dimensions,
        embedding_vector=vector,
        provider_response_hash=provider_response_hash,
    )


class FakeEmbeddingProvider:
    def __init__(
        self,
        *,
        vector: Tuple[float, ...] = _vector(0.1, 0.2, 0.3),
        provider_response_hash: str = _RESPONSE_HASH,
    ) -> None:
        self.vector = vector
        self.provider_response_hash = provider_response_hash
        self.calls: List[dict[str, Any]] = []

    def embed(self, **kwargs: Any) -> EmbeddingProviderResponse:
        self.calls.append(dict(kwargs))
        return EmbeddingProviderResponse(
            embedding_vector=self.vector,
            provider_response_hash=self.provider_response_hash,
        )


def _record_key(record: EmbeddingCacheRecord) -> Tuple[str, str, str, str, str, int]:
    return (
        record.content_scope,
        record.content_hash,
        record.embedding_provider_name,
        record.embedding_model_name,
        record.embedding_model_version,
        record.embedding_dimensions,
    )


class FakeEmbeddingCacheRepository:
    def __init__(
        self,
        rows: Optional[List[EmbeddingCacheRecord]] = None,
    ) -> None:
        self._rows: Dict[Tuple[str, str, str, str, str, int], EmbeddingCacheRecord] = {}
        self.lookup_calls = 0
        self.insert_calls = 0
        self.update_calls = 0
        for row in rows or []:
            self._rows[_record_key(row)] = row

    def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
        self.lookup_calls += 1
        return self._rows.get(identity.as_tuple())

    def insert(self, record: EmbeddingCacheRecord) -> None:
        self.insert_calls += 1
        key = _record_key(record)
        if key in self._rows:
            raise EmbeddingCacheUniqueViolationError("duplicate key value violates unique constraint")
        self._rows[key] = record

    def update(self, record: EmbeddingCacheRecord) -> None:
        self.update_calls += 1
        raise AssertionError("embedding cache rows must never be updated")


class RaceEmbeddingCacheRepository:
    """Simulates a concurrent miss where insert loses and lookup returns winner."""

    def __init__(self, winner: EmbeddingCacheRecord) -> None:
        self.winner = winner
        self.lookup_calls = 0
        self.insert_calls = 0
        self.update_calls = 0

    def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
        self.lookup_calls += 1
        if self.lookup_calls == 1:
            return None
        if identity.as_tuple() != _record_key(self.winner):
            raise EmbeddingCacheConflictError("unexpected identity on re-read")
        return self.winner

    def insert(self, record: EmbeddingCacheRecord) -> None:
        self.insert_calls += 1
        raise EmbeddingCacheUniqueViolationError("duplicate key value violates unique constraint")

    def update(self, record: EmbeddingCacheRecord) -> None:
        self.update_calls += 1
        raise AssertionError("embedding cache rows must never be updated")


class MismatchedLookupRepository:
    def __init__(self, record: EmbeddingCacheRecord) -> None:
        self.record = record
        self.insert_calls = 0
        self.update_calls = 0

    def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
        return self.record

    def insert(self, record: EmbeddingCacheRecord) -> None:
        self.insert_calls += 1
        raise AssertionError("should not insert on conflicting cache hit")


class BrokenLookupRepository(FakeEmbeddingCacheRepository):
    def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
        raise RuntimeError("lookup failed")


class BrokenInsertRepository(FakeEmbeddingCacheRepository):
    def insert(self, record: EmbeddingCacheRecord) -> None:
        raise RuntimeError("insert failed")


class ConflictOnReReadRepository(FakeEmbeddingCacheRepository):
    def __init__(self) -> None:
        super().__init__()
        self._insert_attempted = False

    def lookup(self, identity: EmbeddingCacheIdentity) -> Optional[EmbeddingCacheRecord]:
        self.lookup_calls += 1
        if not self._insert_attempted:
            return None
        return None

    def insert(self, record: EmbeddingCacheRecord) -> None:
        self._insert_attempted = True
        raise EmbeddingCacheUniqueViolationError("duplicate key value violates unique constraint")


class TestHashEmbeddingInput(unittest.TestCase):
    def test_content_hash_is_deterministic(self):
        text = "Which feature enables profile-based defaults?"
        first = hash_embedding_input(text)
        second = hash_embedding_input(text)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_content_hash_changes_with_input(self):
        self.assertNotEqual(
            hash_embedding_input("alpha"),
            hash_embedding_input("beta"),
        )


class TestCacheIdentity(unittest.TestCase):
    def test_query_and_chunk_scopes_remain_distinct(self):
        text = "shared exact text"
        query_identity = _identity(text=text, content_scope=CONTENT_SCOPE_QUERY)
        chunk_identity = _identity(text=text, content_scope=CONTENT_SCOPE_CHUNK)
        self.assertNotEqual(query_identity.as_tuple(), chunk_identity.as_tuple())
        self.assertEqual(query_identity.content_hash, chunk_identity.content_hash)

    def test_model_version_and_dimension_changes_produce_separate_identities(self):
        text = "shared exact text"
        base = _identity(text=text)
        other_model = _identity(text=text, model="other-model")
        other_version = _identity(text=text, version="v2")
        other_dimensions = _identity(text=text, dimensions=4)
        identities = {base, other_model, other_version, other_dimensions}
        self.assertEqual(len(identities), 4)


class TestProviderResponseValidation(unittest.TestCase):
    def test_invalid_dimensions_rejected(self):
        with self.assertRaises(InvalidProviderResponseError):
            validate_provider_response(
                EmbeddingProviderResponse(
                    embedding_vector=_vector(0.1, 0.2),
                    provider_response_hash=_RESPONSE_HASH,
                ),
                embedding_dimensions=_DIMENSIONS,
            )

    def test_nan_values_rejected(self):
        with self.assertRaises(InvalidProviderResponseError):
            validate_provider_response(
                EmbeddingProviderResponse(
                    embedding_vector=_vector(0.1, float("nan"), 0.3),
                    provider_response_hash=_RESPONSE_HASH,
                ),
                embedding_dimensions=_DIMENSIONS,
            )

    def test_infinite_values_rejected(self):
        with self.assertRaises(InvalidProviderResponseError):
            validate_provider_response(
                EmbeddingProviderResponse(
                    embedding_vector=_vector(0.1, float("inf"), 0.3),
                    provider_response_hash=_RESPONSE_HASH,
                ),
                embedding_dimensions=_DIMENSIONS,
            )

    def test_null_values_rejected(self):
        with self.assertRaises(InvalidProviderResponseError):
            validate_provider_response(
                EmbeddingProviderResponse(
                    embedding_vector=(0.1, None, 0.3),  # type: ignore[arg-type]
                    provider_response_hash=_RESPONSE_HASH,
                ),
                embedding_dimensions=_DIMENSIONS,
            )

    def test_non_numeric_values_rejected(self):
        with self.assertRaises(InvalidProviderResponseError):
            validate_provider_response(
                EmbeddingProviderResponse(
                    embedding_vector=("a", "b", "c"),  # type: ignore[arg-type]
                    provider_response_hash=_RESPONSE_HASH,
                ),
                embedding_dimensions=_DIMENSIONS,
            )

    def test_nested_vectors_rejected(self):
        with self.assertRaises(InvalidProviderResponseError):
            validate_provider_response(
                EmbeddingProviderResponse(
                    embedding_vector=((0.1, 0.2), (0.3, 0.4)),  # type: ignore[arg-type]
                    provider_response_hash=_RESPONSE_HASH,
                ),
                embedding_dimensions=_DIMENSIONS,
            )

    def test_empty_provider_response_hash_rejected(self):
        with self.assertRaises(InvalidProviderResponseError):
            validate_provider_response(
                EmbeddingProviderResponse(
                    embedding_vector=_vector(0.1, 0.2, 0.3),
                    provider_response_hash="",
                ),
                embedding_dimensions=_DIMENSIONS,
            )


class TestGetOrComputeEmbedding(unittest.TestCase):
    def test_cache_hit_does_not_call_provider(self):
        text = "cache hit text"
        repository = FakeEmbeddingCacheRepository([_record_for_text(text)])
        provider = FakeEmbeddingProvider()

        result = get_or_compute_embedding(
            text=text,
            content_scope=CONTENT_SCOPE_QUERY,
            embedding_provider_name=_PROVIDER,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            repository=repository,
            provider=provider,
        )

        self.assertTrue(result.cache_hit)
        self.assertEqual(provider.calls, [])
        self.assertEqual(repository.insert_calls, 0)
        self.assertEqual(repository.update_calls, 0)
        self.assertEqual(result.record.embedding_vector, _vector(0.1, 0.2, 0.3))

    def test_cache_miss_calls_provider_once_and_inserts_once(self):
        text = "cache miss text"
        repository = FakeEmbeddingCacheRepository()
        provider = FakeEmbeddingProvider()

        result = get_or_compute_embedding(
            text=text,
            content_scope=CONTENT_SCOPE_QUERY,
            embedding_provider_name=_PROVIDER,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            repository=repository,
            provider=provider,
        )

        self.assertFalse(result.cache_hit)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(repository.insert_calls, 1)
        self.assertEqual(repository.update_calls, 0)
        self.assertEqual(result.record.provider_response_hash, _RESPONSE_HASH)

    def test_concurrent_uniqueness_conflict_rereads_matching_winning_row(self):
        text = "race text"
        winner = _record_for_text(
            text,
            vector=_vector(0.1, 0.2, 0.3),
            provider_response_hash=_RESPONSE_HASH,
        )
        repository = RaceEmbeddingCacheRepository(winner)
        provider = FakeEmbeddingProvider()

        result = get_or_compute_embedding(
            text=text,
            content_scope=CONTENT_SCOPE_QUERY,
            embedding_provider_name=_PROVIDER,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            repository=repository,
            provider=provider,
        )

        self.assertFalse(result.cache_hit)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(repository.insert_calls, 1)
        self.assertEqual(repository.lookup_calls, 2)
        self.assertEqual(result.record, winner)
        self.assertEqual(repository.update_calls, 0)

    def test_concurrent_uniqueness_conflict_rejects_mismatched_vector(self):
        text = "race vector mismatch"
        winner = _record_for_text(
            text,
            vector=_vector(0.9, 0.8, 0.7),
            provider_response_hash=_RESPONSE_HASH,
        )
        repository = RaceEmbeddingCacheRepository(winner)
        provider = FakeEmbeddingProvider()

        with self.assertRaises(EmbeddingCacheConflictError):
            get_or_compute_embedding(
                text=text,
                content_scope=CONTENT_SCOPE_QUERY,
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=repository,
                provider=provider,
            )

    def test_concurrent_uniqueness_conflict_rejects_mismatched_provider_hash(self):
        text = "race hash mismatch"
        winner = _record_for_text(
            text,
            vector=_vector(0.1, 0.2, 0.3),
            provider_response_hash=_OTHER_RESPONSE_HASH,
        )
        repository = RaceEmbeddingCacheRepository(winner)
        provider = FakeEmbeddingProvider()

        with self.assertRaises(EmbeddingCacheConflictError):
            get_or_compute_embedding(
                text=text,
                content_scope=CONTENT_SCOPE_QUERY,
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=repository,
                provider=provider,
            )

    def test_existing_rows_are_never_updated(self):
        text = "immutable row text"
        repository = FakeEmbeddingCacheRepository()
        provider = FakeEmbeddingProvider()

        get_or_compute_embedding(
            text=text,
            content_scope=CONTENT_SCOPE_QUERY,
            embedding_provider_name=_PROVIDER,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            repository=repository,
            provider=provider,
        )

        self.assertEqual(repository.update_calls, 0)

    def test_cache_read_failure_raises_typed_error(self):
        repository = BrokenLookupRepository()
        provider = FakeEmbeddingProvider()

        with self.assertRaises(EmbeddingCacheReadError):
            get_or_compute_embedding(
                text="lookup failure",
                content_scope=CONTENT_SCOPE_QUERY,
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=repository,
                provider=provider,
            )

    def test_cache_insert_failure_raises_typed_error(self):
        repository = BrokenInsertRepository()
        provider = FakeEmbeddingProvider()

        with self.assertRaises(EmbeddingCacheInsertError):
            get_or_compute_embedding(
                text="insert failure",
                content_scope=CONTENT_SCOPE_QUERY,
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=repository,
                provider=provider,
            )

    def test_unresolved_unique_conflict_raises_insert_error(self):
        repository = ConflictOnReReadRepository()
        provider = FakeEmbeddingProvider()

        with self.assertRaises(EmbeddingCacheInsertError):
            get_or_compute_embedding(
                text="unresolved conflict",
                content_scope=CONTENT_SCOPE_QUERY,
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=repository,
                provider=provider,
            )

    def test_conflicting_cached_identity_raises_conflict_error(self):
        text = "conflict text"
        identity = _identity(text=text)
        mismatched = EmbeddingCacheRecord(
            content_scope=identity.content_scope,
            content_hash=identity.content_hash,
            embedding_provider_name=identity.embedding_provider_name,
            embedding_model_name=identity.embedding_model_name,
            embedding_model_version=identity.embedding_model_version,
            embedding_dimensions=identity.embedding_dimensions + 1,
            embedding_vector=_vector(0.1, 0.2, 0.3, 0.4),
            provider_response_hash=_RESPONSE_HASH,
        )
        repository = MismatchedLookupRepository(mismatched)
        provider = FakeEmbeddingProvider()

        with self.assertRaises(EmbeddingCacheConflictError):
            get_or_compute_embedding(
                text=text,
                content_scope=CONTENT_SCOPE_QUERY,
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=repository,
                provider=provider,
            )

    def test_deterministic_return_for_hit_and_miss(self):
        text = "deterministic text"
        repository = FakeEmbeddingCacheRepository()
        provider = FakeEmbeddingProvider(vector=_vector(0.4, 0.5, 0.6))

        first = get_or_compute_embedding(
            text=text,
            content_scope=CONTENT_SCOPE_QUERY,
            embedding_provider_name=_PROVIDER,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            repository=repository,
            provider=provider,
        )
        second = get_or_compute_embedding(
            text=text,
            content_scope=CONTENT_SCOPE_QUERY,
            embedding_provider_name=_PROVIDER,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            repository=repository,
            provider=provider,
        )

        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.record, second.record)

    def test_logs_contain_no_source_text_or_vector_contents(self):
        sensitive_text = "SECRET QUESTION TEXT WITH UNIQUE PHRASE 12345"
        sensitive_vector = _vector(9.87654321, 8.7654321, 7.654321)
        repository = FakeEmbeddingCacheRepository()
        provider = FakeEmbeddingProvider(vector=sensitive_vector)
        logger = logging.getLogger("test.embedding_cache.logging")
        logger.propagate = True

        with self.assertLogs("test.embedding_cache.logging", level="INFO") as captured:
            get_or_compute_embedding(
                text=sensitive_text,
                content_scope=CONTENT_SCOPE_QUERY,
                embedding_provider_name=_PROVIDER,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
                repository=repository,
                provider=provider,
                logger=logger,
            )

        combined_logs = "\n".join(captured.output)
        self.assertIn("content_hash=", combined_logs)
        self.assertNotIn(sensitive_text, combined_logs)
        self.assertNotIn("9.87654321", combined_logs)
        self.assertNotIn("8.7654321", combined_logs)
        self.assertNotIn("7.654321", combined_logs)
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", combined_logs)


class _MockSupabaseResult:
    def __init__(self, data: List[dict[str, Any]]) -> None:
        self.data = data


class _MockSupabaseQuery:
    def __init__(self, client: "_MockSupabaseClient", table_name: str) -> None:
        self._client = client
        self._table_name = table_name
        self._operation: Optional[str] = None
        self._select_fields: Optional[str] = None
        self._filters: List[Tuple[str, str, Any]] = []
        self._limit: Optional[int] = None
        self._insert_payload: Optional[dict[str, Any]] = None

    def select(self, fields: str) -> "_MockSupabaseQuery":
        self._operation = "select"
        self._select_fields = fields
        return self

    def eq(self, field: str, value: Any) -> "_MockSupabaseQuery":
        self._filters.append(("eq", field, value))
        return self

    def limit(self, count: int) -> "_MockSupabaseQuery":
        self._limit = count
        return self

    def insert(self, payload: dict[str, Any]) -> "_MockSupabaseQuery":
        self._operation = "insert"
        self._insert_payload = payload
        return self

    def update(self, _payload: dict[str, Any]) -> "_MockSupabaseQuery":
        raise AssertionError("embedding cache must never use UPDATE")

    def upsert(self, _payload: dict[str, Any]) -> "_MockSupabaseQuery":
        raise AssertionError("embedding cache must never use UPSERT")

    def execute(self) -> _MockSupabaseResult:
        self._client.queries.append(self)
        return self._client._execute_query(self)


class _MockSupabaseClient:
    def __init__(
        self,
        *,
        lookup_rows: Optional[List[dict[str, Any]]] = None,
        lookup_error: Optional[Exception] = None,
        insert_error: Optional[Exception] = None,
    ) -> None:
        self.lookup_rows = list(lookup_rows or [])
        self.lookup_error = lookup_error
        self.insert_error = insert_error
        self.queries: List[_MockSupabaseQuery] = []

    def table(self, name: str) -> _MockSupabaseQuery:
        if name != TABLE_NAME:
            raise AssertionError(f"unexpected table {name!r}")
        return _MockSupabaseQuery(self, name)

    def _execute_query(self, query: _MockSupabaseQuery) -> _MockSupabaseResult:
        if query._operation == "select":
            if self.lookup_error is not None:
                raise self.lookup_error
            rows = list(self.lookup_rows)
            if query._limit is not None:
                rows = rows[: query._limit]
            return _MockSupabaseResult(rows)
        if query._operation == "insert":
            if self.insert_error is not None:
                raise self.insert_error
            return _MockSupabaseResult([])
        raise AssertionError(f"unexpected operation {query._operation!r}")


class TestSupabaseEmbeddingCacheRepository(unittest.TestCase):
    def test_lookup_filters_on_every_identity_field(self):
        text = "supabase lookup filters"
        identity = _identity(text=text)
        row = {
            "content_scope": identity.content_scope,
            "content_hash": identity.content_hash,
            "embedding_provider_name": identity.embedding_provider_name,
            "embedding_model_name": identity.embedding_model_name,
            "embedding_model_version": identity.embedding_model_version,
            "embedding_dimensions": identity.embedding_dimensions,
            "embedding_vector": [0.1, 0.2, 0.3],
            "provider_response_hash": _RESPONSE_HASH,
        }
        client = _MockSupabaseClient(lookup_rows=[row])
        repository = SupabaseEmbeddingCacheRepository(client)

        record = repository.lookup(identity)

        self.assertIsNotNone(record)
        self.assertEqual(len(client.queries), 1)
        query = client.queries[0]
        self.assertEqual(query._operation, "select")
        self.assertEqual(query._limit, 1)
        filtered_fields = {field: value for _op, field, value in query._filters}
        self.assertEqual(
            filtered_fields,
            {
                "content_scope": identity.content_scope,
                "content_hash": identity.content_hash,
                "embedding_provider_name": identity.embedding_provider_name,
                "embedding_model_name": identity.embedding_model_name,
                "embedding_model_version": identity.embedding_model_version,
                "embedding_dimensions": identity.embedding_dimensions,
            },
        )
        assert record is not None
        self.assertEqual(record.embedding_vector, _vector(0.1, 0.2, 0.3))

    def test_lookup_returns_none_for_zero_rows(self):
        identity = _identity(text="missing row")
        client = _MockSupabaseClient(lookup_rows=[])
        repository = SupabaseEmbeddingCacheRepository(client)

        self.assertIsNone(repository.lookup(identity))

    def test_lookup_reads_only_first_row_when_multiple_rows_returned(self):
        identity = _identity(text="multiple rows")
        rows = [
            {
                "content_scope": identity.content_scope,
                "content_hash": identity.content_hash,
                "embedding_provider_name": identity.embedding_provider_name,
                "embedding_model_name": identity.embedding_model_name,
                "embedding_model_version": identity.embedding_model_version,
                "embedding_dimensions": identity.embedding_dimensions,
                "embedding_vector": [0.1, 0.2, 0.3],
                "provider_response_hash": _RESPONSE_HASH,
            },
            {
                "content_scope": identity.content_scope,
                "content_hash": identity.content_hash,
                "embedding_provider_name": identity.embedding_provider_name,
                "embedding_model_name": identity.embedding_model_name,
                "embedding_model_version": identity.embedding_model_version,
                "embedding_dimensions": identity.embedding_dimensions,
                "embedding_vector": [0.9, 0.8, 0.7],
                "provider_response_hash": _OTHER_RESPONSE_HASH,
            },
        ]
        client = _MockSupabaseClient(lookup_rows=rows)
        repository = SupabaseEmbeddingCacheRepository(client)

        record = repository.lookup(identity)

        assert record is not None
        self.assertEqual(record.embedding_vector, _vector(0.1, 0.2, 0.3))
        self.assertEqual(client.queries[0]._limit, 1)

    def test_insert_payload_contains_exact_cache_fields(self):
        record = _record_for_text("insert payload text")
        client = _MockSupabaseClient()
        repository = SupabaseEmbeddingCacheRepository(client)

        repository.insert(record)

        self.assertEqual(len(client.queries), 1)
        query = client.queries[0]
        self.assertEqual(query._operation, "insert")
        self.assertEqual(query._insert_payload, record_to_payload(record))
        self.assertEqual(
            set(query._insert_payload or {}),
            {
                "content_scope",
                "content_hash",
                "embedding_provider_name",
                "embedding_model_name",
                "embedding_model_version",
                "embedding_dimensions",
                "embedding_vector",
                "provider_response_hash",
            },
        )

    def test_duplicate_key_maps_to_unique_violation_error(self):
        client = _MockSupabaseClient(
            insert_error=RuntimeError(
                'duplicate key value violates unique constraint "retrieval_embedding_cache_unique_identity" (SQLSTATE 23505)'
            )
        )
        repository = SupabaseEmbeddingCacheRepository(client)
        record = _record_for_text("duplicate insert text")

        with self.assertRaises(EmbeddingCacheUniqueViolationError):
            repository.insert(record)

    def test_non_duplicate_insert_failure_maps_to_insert_error(self):
        client = _MockSupabaseClient(insert_error=RuntimeError("permission denied"))
        repository = SupabaseEmbeddingCacheRepository(client)
        record = _record_for_text("insert failure text")

        with self.assertRaises(EmbeddingCacheInsertError):
            repository.insert(record)

    def test_non_duplicate_lookup_failure_maps_to_read_error(self):
        identity = _identity(text="lookup failure text")
        client = _MockSupabaseClient(lookup_error=RuntimeError("connection reset"))
        repository = SupabaseEmbeddingCacheRepository(client)

        with self.assertRaises(EmbeddingCacheReadError):
            repository.lookup(identity)

    def test_repository_source_has_no_update_or_upsert_operations(self):
        import inspect

        source = inspect.getsource(SupabaseEmbeddingCacheRepository)
        self.assertNotIn(".update(", source)
        self.assertNotIn(".upsert(", source)

    def test_record_from_row_validates_identity(self):
        text = "row validation text"
        identity = _identity(text=text)
        row = {
            "content_scope": identity.content_scope,
            "content_hash": identity.content_hash,
            "embedding_provider_name": identity.embedding_provider_name,
            "embedding_model_name": identity.embedding_model_name,
            "embedding_model_version": identity.embedding_model_version,
            "embedding_dimensions": identity.embedding_dimensions,
            "embedding_vector": [0.1, 0.2, 0.3],
            "provider_response_hash": _RESPONSE_HASH,
        }
        record = record_from_row(row, expected_identity=identity)
        self.assertEqual(record.embedding_vector, _vector(0.1, 0.2, 0.3))


if __name__ == "__main__":
    unittest.main()
