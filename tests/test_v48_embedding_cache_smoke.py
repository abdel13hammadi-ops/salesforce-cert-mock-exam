"""Tests for V48 embedding-cache smoke runner (mocked HTTP and Supabase only)."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.v48_embedding_cache_smoke import (
    ENV_OPENAI_API_KEY,
    ENV_SUPABASE_SERVICE_ROLE_KEY,
    ENV_SUPABASE_URL,
    FLOAT8_ARRAY_STORAGE_ABS_TOL,
    EmbeddingCacheSmokeCleanupError,
    EmbeddingCacheSmokeConfig,
    EmbeddingCacheSmokeConfigError,
    EmbeddingCacheSmokeConsistencyError,
    EmbeddingCacheSmokeEnvironmentError,
    EmbeddingCacheSmokeProviderError,
    assert_vectors_equivalent_within_float8_round_trip,
    build_initial_summary,
    build_synthetic_smoke_texts,
    cleanup_smoke_cache_rows,
    delete_cache_row_by_identity,
    format_redacted_summary,
    main,
    max_absolute_vector_difference,
    parse_args,
    run_embedding_cache_smoke,
    validate_execute_configuration,
)
from workers.embedding_cache import (
    CONTENT_SCOPE_CHUNK,
    CONTENT_SCOPE_QUERY,
    TABLE_NAME,
    EmbeddingCacheIdentity,
    EmbeddingCacheRecord,
    SupabaseEmbeddingCacheRepository,
    build_cache_identity,
    record_to_payload,
)
from workers.embedding_providers import (
    OPENAI_PROVIDER_NAME,
    EmbeddingProviderResponse,
    HttpResponse,
)

_SMOKE_RUN_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_MODEL = "text-embedding-3-small"
_VERSION = "2024-01-15"
_DIMENSIONS = 3
_VECTOR = (0.11, 0.22, 0.33)
_RESPONSE_HASH = "b" * 64
_API_KEY = "sk-test-key-not-real"
_SERVICE_ROLE = "service-role-key-not-real"
_SUPABASE_URL = "https://example.supabase.co"
_SENSITIVE_QUERY = build_synthetic_smoke_texts(_SMOKE_RUN_ID)[0]
_SENSITIVE_CHUNK = build_synthetic_smoke_texts(_SMOKE_RUN_ID)[1]

_REQUIRED_ENV = {
    ENV_OPENAI_API_KEY: _API_KEY,
    ENV_SUPABASE_URL: _SUPABASE_URL,
    ENV_SUPABASE_SERVICE_ROLE_KEY: _SERVICE_ROLE,
}


class RecordingTransport:
    def __init__(self, *, fail_on_call: Optional[int] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.fail_on_call = fail_on_call

    def post_json(self, **kwargs: Any) -> HttpResponse:
        self.calls.append(kwargs)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("transport exploded")
        return HttpResponse(
            status_code=200,
            body=json.dumps(
                {
                    "object": "list",
                    "data": [{"embedding": list(_VECTOR)}],
                    "model": _MODEL,
                }
            ),
        )


class _MockSupabaseResult:
    def __init__(self, data: List[dict[str, Any]]) -> None:
        self.data = data


class _MockSupabaseQuery:
    def __init__(self, client: "_MockSupabaseClient", table_name: str) -> None:
        self._client = client
        self._table_name = table_name
        self._operation: Optional[str] = None
        self._filters: List[Tuple[str, str, Any]] = []
        self._limit: Optional[int] = None
        self._insert_payload: Optional[dict[str, Any]] = None

    def select(self, fields: str) -> "_MockSupabaseQuery":
        self._operation = "select"
        return self

    def delete(self) -> "_MockSupabaseQuery":
        self._operation = "delete"
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
        raise AssertionError("broad or partial cache update is not allowed")

    def upsert(self, _payload: dict[str, Any]) -> "_MockSupabaseQuery":
        raise AssertionError("cache upsert is not allowed")

    def execute(self) -> _MockSupabaseResult:
        self._client.queries.append(self)
        return self._client._execute_query(self)


class _MockSupabaseClient:
    def __init__(self) -> None:
        self.rows: Dict[Tuple[Any, ...], dict[str, Any]] = {}
        self.queries: List[_MockSupabaseQuery] = []
        self.delete_calls = 0
        self.fail_delete_on_call: Optional[int] = None

    def table(self, name: str) -> _MockSupabaseQuery:
        if name != TABLE_NAME:
            raise AssertionError(f"unexpected table {name!r}")
        return _MockSupabaseQuery(self, name)

    def _row_key(self, row: dict[str, Any]) -> Tuple[Any, ...]:
        return (
            row["content_scope"],
            row["content_hash"],
            row["embedding_provider_name"],
            row["embedding_model_name"],
            row["embedding_model_version"],
            row["embedding_dimensions"],
        )

    def _execute_query(self, query: _MockSupabaseQuery) -> _MockSupabaseResult:
        if query._operation == "select":
            matches = [
                row
                for row in self.rows.values()
                if all(row.get(field) == value for _, field, value in query._filters)
            ]
            if query._limit is not None:
                matches = matches[: query._limit]
            return _MockSupabaseResult(matches)
        if query._operation == "insert":
            assert query._insert_payload is not None
            key = self._row_key(query._insert_payload)
            self.rows[key] = dict(query._insert_payload)
            return _MockSupabaseResult([])
        if query._operation == "delete":
            self.delete_calls += 1
            if self.fail_delete_on_call == self.delete_calls:
                raise RuntimeError("delete exploded")
            keys_to_delete = [
                key
                for key, row in self.rows.items()
                if all(row.get(field) == value for _, field, value in query._filters)
            ]
            if len(query._filters) != 6:
                raise AssertionError("delete must filter on all cache identity fields")
            deleted_rows = [dict(self.rows[key]) for key in keys_to_delete]
            for key in keys_to_delete:
                del self.rows[key]
            return _MockSupabaseResult(deleted_rows)
        raise AssertionError(f"unexpected operation {query._operation!r}")


class _Float8RoundTripMockClient(_MockSupabaseClient):
    """Simulate harmless float8[] reload precision drift on cache hits."""

    def _execute_query(self, query: _MockSupabaseQuery) -> _MockSupabaseResult:
        if query._operation != "select":
            return super()._execute_query(query)
        result = super()._execute_query(query)
        if not result.data:
            return result
        perturbed_rows = []
        for row in result.data:
            adjusted = dict(row)
            vector = [float(value) for value in adjusted["embedding_vector"]]
            vector[0] = vector[0] + 1e-13
            adjusted["embedding_vector"] = vector
            perturbed_rows.append(adjusted)
        return _MockSupabaseResult(perturbed_rows)


class _MaterialVectorDriftMockClient(_MockSupabaseClient):
    """Simulate a materially different vector after cache reload."""

    def _execute_query(self, query: _MockSupabaseQuery) -> _MockSupabaseResult:
        if query._operation != "select":
            return super()._execute_query(query)
        result = super()._execute_query(query)
        if not result.data:
            return result
        row = dict(result.data[0])
        row["embedding_vector"] = [
            float(value) + 1.0 for value in row["embedding_vector"]
        ]
        return _MockSupabaseResult([row])


def _execute_config(**overrides: Any) -> EmbeddingCacheSmokeConfig:
    base = {
        "execute": True,
        "model_name": _MODEL,
        "model_version": _VERSION,
        "dimensions": _DIMENSIONS,
        "timeout_seconds": 12.0,
        "smoke_run_id": _SMOKE_RUN_ID,
    }
    base.update(overrides)
    return EmbeddingCacheSmokeConfig(**base)


def _identities() -> tuple[EmbeddingCacheIdentity, EmbeddingCacheIdentity]:
    shared = {
        "embedding_provider_name": OPENAI_PROVIDER_NAME,
        "embedding_model_name": _MODEL,
        "embedding_model_version": _VERSION,
        "embedding_dimensions": _DIMENSIONS,
    }
    query_text, chunk_text = build_synthetic_smoke_texts(_SMOKE_RUN_ID)
    return (
        build_cache_identity(text=query_text, content_scope=CONTENT_SCOPE_QUERY, **shared),
        build_cache_identity(text=chunk_text, content_scope=CONTENT_SCOPE_CHUNK, **shared),
    )


class TestEmbeddingCacheSmokeSafetyGates(unittest.TestCase):
    def test_dry_run_performs_zero_provider_and_database_calls(self):
        config = _execute_config(execute=False)
        transport = RecordingTransport()
        client = _MockSupabaseClient()

        summary = run_embedding_cache_smoke(
            config,
            env=_REQUIRED_ENV,
            client_factory=lambda: client,
            transport_factory=lambda: transport,
        )

        self.assertEqual(summary["final_status"], "planned")
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.queries, [])

    def test_dry_run_prints_redacted_plan_and_exits_successfully(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "--smoke-run-id",
                    _SMOKE_RUN_ID,
                    "--model",
                    _MODEL,
                    "--model-version",
                    _VERSION,
                    "--dimensions",
                    str(_DIMENSIONS),
                ]
            )
        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("dry-run", output)
        self.assertIn(_SMOKE_RUN_ID, output)
        self.assertNotIn(_API_KEY, output)
        self.assertNotIn(_SERVICE_ROLE, output)
        self.assertNotIn(_SENSITIVE_QUERY, output)

    def test_execute_flag_is_required(self):
        config = _execute_config()
        with self.assertRaises(EmbeddingCacheSmokeConfigError):
            validate_execute_configuration(
                EmbeddingCacheSmokeConfig(
                    execute=False,
                    model_name=_MODEL,
                    model_version=_VERSION,
                    dimensions=_DIMENSIONS,
                    timeout_seconds=10.0,
                    smoke_run_id=_SMOKE_RUN_ID,
                ),
                env=_REQUIRED_ENV,
            )

    def test_missing_environment_variables_fail_before_external_calls(self):
        config = _execute_config()
        transport = RecordingTransport()
        client = _MockSupabaseClient()
        env = dict(_REQUIRED_ENV)
        env.pop(ENV_OPENAI_API_KEY)

        with self.assertRaises(EmbeddingCacheSmokeEnvironmentError) as ctx:
            run_embedding_cache_smoke(
                config,
                env=env,
                client_factory=lambda: client,
                transport_factory=lambda: transport,
            )

        self.assertIn(ENV_OPENAI_API_KEY, str(ctx.exception))
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.queries, [])

    def test_model_model_version_and_dimensions_are_mandatory(self):
        for kwargs, message_part in (
            ({"model_name": None}, "--model"),
            ({"model_version": None}, "--model-version"),
            ({"dimensions": None}, "--dimensions"),
            ({"dimensions": 0}, "--dimensions must be a positive integer"),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(EmbeddingCacheSmokeConfigError) as ctx:
                    validate_execute_configuration(
                        _execute_config(**kwargs),
                        env=_REQUIRED_ENV,
                    )
                self.assertIn(message_part, str(ctx.exception))


class TestEmbeddingCacheSmokeExecution(unittest.TestCase):
    def test_successful_cold_cache_run_uses_exactly_two_provider_calls(self):
        config = _execute_config()
        transport = RecordingTransport()
        client = _MockSupabaseClient()

        summary = run_embedding_cache_smoke(
            config,
            env=_REQUIRED_ENV,
            client_factory=lambda: client,
            transport_factory=lambda: transport,
        )

        self.assertEqual(len(transport.calls), 2)
        self.assertFalse(summary["first_query_cache_hit"])
        self.assertFalse(summary["first_chunk_cache_hit"])
        self.assertTrue(summary["repeated_query_cache_hit"])
        self.assertTrue(summary["repeated_chunk_cache_hit"])
        self.assertTrue(summary["cleanup_succeeded"])
        self.assertEqual(summary["final_status"], "success")
        self.assertEqual(client.rows, {})

    def test_float8_round_trip_precision_difference_passes(self):
        config = _execute_config()
        transport = RecordingTransport()
        client = _Float8RoundTripMockClient()

        summary = run_embedding_cache_smoke(
            config,
            env=_REQUIRED_ENV,
            client_factory=lambda: client,
            transport_factory=lambda: transport,
        )

        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(summary["final_status"], "success")
        self.assertTrue(summary["cleanup_succeeded"])
        self.assertEqual(client.rows, {})

    def test_cleanup_succeeds_after_material_vector_validation_failure(self):
        config = _execute_config()
        client = _MaterialVectorDriftMockClient()
        summary = build_initial_summary(config)

        with self.assertRaises(EmbeddingCacheSmokeConsistencyError):
            run_embedding_cache_smoke(
                config,
                env=_REQUIRED_ENV,
                client_factory=lambda: client,
                transport_factory=lambda: RecordingTransport(),
                summary=summary,
            )

        self.assertTrue(summary["cleanup_succeeded"])
        self.assertEqual(client.rows, {})

    def test_repeated_lookups_are_cache_hits(self):
        config = _execute_config()
        transport = RecordingTransport()
        client = _MockSupabaseClient()

        run_embedding_cache_smoke(
            config,
            env=_REQUIRED_ENV,
            client_factory=lambda: client,
            transport_factory=lambda: transport,
        )

        self.assertEqual(len(transport.calls), 2)

    def test_only_two_exact_smoke_identities_are_deleted(self):
        config = _execute_config()
        client = _MockSupabaseClient()
        query_identity, chunk_identity = _identities()

        run_embedding_cache_smoke(
            config,
            env=_REQUIRED_ENV,
            client_factory=lambda: client,
            transport_factory=lambda: RecordingTransport(),
        )

        delete_queries = [query for query in client.queries if query._operation == "delete"]
        self.assertEqual(len(delete_queries), 2)
        self.assertEqual(client.delete_calls, 2)
        deleted_filter_sets = [
            {field: value for _, field, value in query._filters}
            for query in delete_queries
        ]
        self.assertEqual(
            deleted_filter_sets,
            [
                {
                    "content_scope": query_identity.content_scope,
                    "content_hash": query_identity.content_hash,
                    "embedding_provider_name": query_identity.embedding_provider_name,
                    "embedding_model_name": query_identity.embedding_model_name,
                    "embedding_model_version": query_identity.embedding_model_version,
                    "embedding_dimensions": query_identity.embedding_dimensions,
                },
                {
                    "content_scope": chunk_identity.content_scope,
                    "content_hash": chunk_identity.content_hash,
                    "embedding_provider_name": chunk_identity.embedding_provider_name,
                    "embedding_model_name": chunk_identity.embedding_model_name,
                    "embedding_model_version": chunk_identity.embedding_model_version,
                    "embedding_dimensions": chunk_identity.embedding_dimensions,
                },
            ],
        )

    def test_cleanup_runs_after_partial_provider_failure(self):
        config = _execute_config()
        client = _MockSupabaseClient()
        summary = build_initial_summary(config)

        with self.assertRaises(EmbeddingCacheSmokeProviderError):
            run_embedding_cache_smoke(
                config,
                env=_REQUIRED_ENV,
                client_factory=lambda: client,
                transport_factory=lambda: RecordingTransport(fail_on_call=1),
                summary=summary,
            )

        self.assertTrue(summary["cleanup_succeeded"])
        delete_queries = [query for query in client.queries if query._operation == "delete"]
        self.assertEqual(len(delete_queries), 2)

    def test_cleanup_failure_is_reported_after_successful_execution(self):
        config = _execute_config()
        client = _MockSupabaseClient()
        client.fail_delete_on_call = 1
        summary = build_initial_summary(config)

        with self.assertRaises(EmbeddingCacheSmokeCleanupError):
            run_embedding_cache_smoke(
                config,
                env=_REQUIRED_ENV,
                client_factory=lambda: client,
                transport_factory=lambda: RecordingTransport(),
                summary=summary,
            )

        self.assertFalse(summary["cleanup_succeeded"])
        delete_queries = [query for query in client.queries if query._operation == "delete"]
        self.assertEqual(len(delete_queries), 2)

    def test_passed_summary_dict_is_updated_in_place(self):
        config = _execute_config()
        summary = build_initial_summary(config)

        returned = run_embedding_cache_smoke(
            config,
            env=_REQUIRED_ENV,
            client_factory=_MockSupabaseClient,
            transport_factory=lambda: RecordingTransport(),
            summary=summary,
        )

        self.assertIs(returned, summary)
        self.assertTrue(summary["cleanup_succeeded"])
        self.assertEqual(summary["final_status"], "success")

    def test_broad_delete_is_not_possible(self):
        client = _MockSupabaseClient()
        query = client.table(TABLE_NAME).delete().eq("content_scope", CONTENT_SCOPE_QUERY)
        with self.assertRaises(AssertionError):
            query.execute()


class TestEmbeddingCacheSmokeOutputPrivacy(unittest.TestCase):
    def test_redacted_summary_excludes_sensitive_values(self):
        config = _execute_config()
        summary = run_embedding_cache_smoke(
            config,
            env=_REQUIRED_ENV,
            client_factory=_MockSupabaseClient,
            transport_factory=lambda: RecordingTransport(),
        )
        serialized = format_redacted_summary(summary)

        self.assertNotIn(_SENSITIVE_QUERY, serialized)
        self.assertNotIn(_SENSITIVE_CHUNK, serialized)
        self.assertNotIn(_API_KEY, serialized)
        self.assertNotIn(_SERVICE_ROLE, serialized)
        self.assertNotIn("embedding_vector", serialized)
        self.assertIn(_SMOKE_RUN_ID, serialized)

    def test_errors_exclude_sensitive_values(self):
        config = _execute_config()
        summary = build_initial_summary(config)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(EmbeddingCacheSmokeEnvironmentError):
                run_embedding_cache_smoke(
                    config,
                    env={},
                    client_factory=_MockSupabaseClient,
                    transport_factory=lambda: RecordingTransport(),
                    summary=summary,
                )
        combined = stderr.getvalue() + format_redacted_summary(summary)
        self.assertNotIn(_API_KEY, combined)
        self.assertNotIn(_SERVICE_ROLE, combined)
        self.assertNotIn(_SENSITIVE_QUERY, combined)


class TestEmbeddingCacheSmokeVectorEquivalence(unittest.TestCase):
    def test_identical_vectors_pass(self):
        assert_vectors_equivalent_within_float8_round_trip(
            left=_VECTOR,
            right=_VECTOR,
            label="query",
            dimensions=_DIMENSIONS,
        )

    def test_harmless_float8_round_trip_difference_passes(self):
        perturbed = (_VECTOR[0] + 1e-13, _VECTOR[1], _VECTOR[2])
        assert_vectors_equivalent_within_float8_round_trip(
            left=_VECTOR,
            right=perturbed,
            label="query",
            dimensions=_DIMENSIONS,
        )

    def test_material_vector_difference_fails(self):
        different = (_VECTOR[0] + 1.0, _VECTOR[1], _VECTOR[2])
        with self.assertRaises(EmbeddingCacheSmokeConsistencyError) as ctx:
            assert_vectors_equivalent_within_float8_round_trip(
                left=_VECTOR,
                right=different,
                label="query",
                dimensions=_DIMENSIONS,
            )
        self.assertIn("max_abs_diff", str(ctx.exception))
        self.assertNotIn(str(_VECTOR[0]), str(ctx.exception))

    def test_dimension_mismatch_fails(self):
        with self.assertRaises(EmbeddingCacheSmokeConsistencyError):
            assert_vectors_equivalent_within_float8_round_trip(
                left=_VECTOR,
                right=_VECTOR[:2],
                label="query",
                dimensions=_DIMENSIONS,
            )

    def test_nan_and_infinity_are_rejected(self):
        with self.assertRaises(EmbeddingCacheSmokeConsistencyError):
            assert_vectors_equivalent_within_float8_round_trip(
                left=(float("nan"), 0.2, 0.3),
                right=_VECTOR,
                label="query",
                dimensions=_DIMENSIONS,
            )
        with self.assertRaises(EmbeddingCacheSmokeConsistencyError):
            assert_vectors_equivalent_within_float8_round_trip(
                left=_VECTOR,
                right=(float("inf"), 0.2, 0.3),
                label="query",
                dimensions=_DIMENSIONS,
            )

    def test_max_absolute_difference_helper(self):
        self.assertAlmostEqual(
            max_absolute_vector_difference(_VECTOR, (_VECTOR[0] + 1e-13, *_VECTOR[1:])),
            1e-13,
        )
        self.assertEqual(FLOAT8_ARRAY_STORAGE_ABS_TOL, 1e-12)


class TestEmbeddingCacheSmokeHelpers(unittest.TestCase):
    def test_parse_args_requires_explicit_dimensions_for_execute(self):
        config = parse_args(
            [
                "--execute",
                "--model",
                _MODEL,
                "--model-version",
                _VERSION,
                "--dimensions",
                str(_DIMENSIONS),
                "--smoke-run-id",
                _SMOKE_RUN_ID,
            ]
        )
        self.assertTrue(config.execute)
        self.assertEqual(config.dimensions, _DIMENSIONS)

    def test_delete_helper_requires_all_identity_filters(self):
        client = _MockSupabaseClient()
        identity = _identities()[0]
        row = record_to_payload(
            EmbeddingCacheRecord(
                content_scope=identity.content_scope,
                content_hash=identity.content_hash,
                embedding_provider_name=identity.embedding_provider_name,
                embedding_model_name=identity.embedding_model_name,
                embedding_model_version=identity.embedding_model_version,
                embedding_dimensions=identity.embedding_dimensions,
                embedding_vector=_VECTOR,
                provider_response_hash=_RESPONSE_HASH,
            )
        )
        client.rows[client._row_key(row)] = row

        deleted_count = delete_cache_row_by_identity(client, identity)

        self.assertEqual(deleted_count, 1)
        self.assertEqual(client.rows, {})
        self.assertEqual(len(client.queries[-1]._filters), 6)

    def test_cleanup_smoke_cache_rows_reports_success_when_rows_absent(self):
        client = _MockSupabaseClient()
        repository = SupabaseEmbeddingCacheRepository(client)
        query_identity, chunk_identity = _identities()

        cleanup_smoke_cache_rows(
            client=client,
            repository=repository,
            identities=[query_identity, chunk_identity],
        )

        self.assertEqual(client.delete_calls, 2)
        self.assertEqual(client.rows, {})


class TestEmbeddingCacheSmokeIsolation(unittest.TestCase):
    def test_no_live_worker_imports_smoke_runner_or_transport(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        workers_dir = os.path.join(repo_root, "workers")
        offenders = []
        for name in os.listdir(workers_dir):
            if not name.endswith(".py") or name in {
                "embedding_http_transport.py",
                "__init__.py",
            }:
                continue
            path = os.path.join(workers_dir, name)
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
            if "v48_embedding_cache_smoke" in contents or "embedding_http_transport" in contents:
                offenders.append(name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
