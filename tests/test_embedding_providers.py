"""Tests for OpenAI-compatible embedding provider adapters."""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.embedding_cache import EmbeddingProviderResponse
from workers.embedding_providers import (
    DEFAULT_OPENAI_EMBEDDINGS_URL,
    OPENAI_PROVIDER_NAME,
    EmbeddingProviderAuthError,
    EmbeddingProviderConfigError,
    EmbeddingProviderRateLimitError,
    EmbeddingProviderResponseError,
    EmbeddingProviderTransportError,
    HttpResponse,
    OpenAIEmbeddingProvider,
    OpenAIEmbeddingProviderConfig,
    compute_stable_provider_response_hash,
)

_API_KEY = "sk-test-key-not-real"
_MODEL = "text-embedding-3-small"
_VERSION = "2024-01-15"
_DIMENSIONS = 3
_VECTOR = (0.1, 0.2, 0.3)
_SENSITIVE_TEXT = "SECRET QUESTION TEXT MUST NOT APPEAR IN LOGS"


def _openai_response_body(
    *,
    vector: Optional[List[float]] = None,
    model: str = _MODEL,
    request_id: str = "req-123",
    usage: Optional[Dict[str, int]] = None,
    extra_top_level: Optional[Dict[str, Any]] = None,
) -> str:
    payload: Dict[str, Any] = {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "index": 0,
                "embedding": list(vector if vector is not None else _VECTOR),
            }
        ],
        "model": model,
        "usage": usage or {"prompt_tokens": 5, "total_tokens": 5},
        "id": request_id,
    }
    if extra_top_level:
        payload.update(extra_top_level)
    return json.dumps(payload)


class RecordingTransport:
    def __init__(
        self,
        *,
        status_code: int = 200,
        body: str = "",
        exc: Optional[Exception] = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.exc = exc
        self.calls: List[Dict[str, Any]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.exc is not None:
            raise self.exc
        return HttpResponse(status_code=self.status_code, body=self.body)


def _provider(
    transport: RecordingTransport,
    *,
    api_key: str = _API_KEY,
    timeout_seconds: float = 12.5,
    embeddings_url: str = DEFAULT_OPENAI_EMBEDDINGS_URL,
) -> OpenAIEmbeddingProvider:
    return OpenAIEmbeddingProvider(
        config=OpenAIEmbeddingProviderConfig(
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            embeddings_url=embeddings_url,
        ),
        transport=transport,
    )


def _embed(
    provider: OpenAIEmbeddingProvider,
    *,
    text: str = _SENSITIVE_TEXT,
) -> EmbeddingProviderResponse:
    return provider.embed(
        text=text,
        embedding_provider_name=OPENAI_PROVIDER_NAME,
        embedding_model_name=_MODEL,
        embedding_model_version=_VERSION,
        embedding_dimensions=_DIMENSIONS,
    )


class TestOpenAIEmbeddingProviderRequest(unittest.TestCase):
    def test_valid_response_returns_embedding_provider_response(self):
        transport = RecordingTransport(status_code=200, body=_openai_response_body())
        provider = _provider(transport)

        result = _embed(provider)

        self.assertIsInstance(result, EmbeddingProviderResponse)
        self.assertEqual(result.embedding_vector, _VECTOR)
        self.assertRegex(result.provider_response_hash, r"^[0-9a-f]{64}$")

    def test_request_uses_configured_model_input_and_dimensions(self):
        transport = RecordingTransport(status_code=200, body=_openai_response_body())
        provider = _provider(transport)

        _embed(provider, text="exact input text")

        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["url"], DEFAULT_OPENAI_EMBEDDINGS_URL)
        self.assertEqual(call["payload"]["model"], _MODEL)
        self.assertEqual(call["payload"]["input"], "exact input text")
        self.assertEqual(call["payload"]["dimensions"], _DIMENSIONS)

    def test_api_key_is_passed_in_authorization_header(self):
        transport = RecordingTransport(status_code=200, body=_openai_response_body())
        provider = _provider(transport, api_key="sk-secret-test-key")

        _embed(provider)

        auth_header = transport.calls[0]["headers"]["Authorization"]
        self.assertEqual(auth_header, "Bearer sk-secret-test-key")

    def test_configured_timeout_is_applied(self):
        transport = RecordingTransport(status_code=200, body=_openai_response_body())
        provider = _provider(transport, timeout_seconds=17.25)

        _embed(provider)

        self.assertEqual(transport.calls[0]["timeout_seconds"], 17.25)

    def test_no_automatic_retries_occur(self):
        transport = RecordingTransport(
            status_code=500,
            body='{"error":{"message":"server exploded"}}',
        )
        provider = _provider(transport)

        with self.assertRaises(EmbeddingProviderTransportError):
            _embed(provider)

        self.assertEqual(len(transport.calls), 1)

    def test_empty_input_text_rejected(self):
        provider = _provider(RecordingTransport(status_code=200, body=_openai_response_body()))

        with self.assertRaises(EmbeddingProviderConfigError):
            _embed(provider, text="   ")

    def test_missing_api_key_rejected(self):
        provider = _provider(
            RecordingTransport(status_code=200, body=_openai_response_body()),
            api_key="",
        )

        with self.assertRaises(EmbeddingProviderConfigError):
            _embed(provider)

    def test_unsupported_provider_identity_rejected(self):
        transport = RecordingTransport(status_code=200, body=_openai_response_body())
        provider = _provider(transport)

        with self.assertRaises(EmbeddingProviderConfigError):
            provider.embed(
                text="text",
                embedding_provider_name="anthropic",
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
            )

        self.assertEqual(transport.calls, [])

    def test_empty_model_or_version_rejected(self):
        transport = RecordingTransport(status_code=200, body=_openai_response_body())
        provider = _provider(transport)

        with self.assertRaises(EmbeddingProviderConfigError):
            provider.embed(
                text="text",
                embedding_provider_name=OPENAI_PROVIDER_NAME,
                embedding_model_name="",
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
            )

    def test_nonpositive_dimensions_rejected(self):
        provider = _provider(RecordingTransport(status_code=200, body=_openai_response_body()))

        with self.assertRaises(EmbeddingProviderConfigError):
            provider.embed(
                text="text",
                embedding_provider_name=OPENAI_PROVIDER_NAME,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=0,
            )


class TestStableProviderResponseHash(unittest.TestCase):
    def test_hash_is_deterministic_for_same_embedding_result(self):
        first = compute_stable_provider_response_hash(
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            embedding_vector=_VECTOR,
        )
        second = compute_stable_provider_response_hash(
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            embedding_vector=_VECTOR,
        )
        self.assertEqual(first, second)

    def test_changing_request_id_does_not_change_hash(self):
        transport_a = RecordingTransport(
            status_code=200,
            body=_openai_response_body(request_id="req-a"),
        )
        transport_b = RecordingTransport(
            status_code=200,
            body=_openai_response_body(request_id="req-b"),
        )

        hash_a = _embed(_provider(transport_a)).provider_response_hash
        hash_b = _embed(_provider(transport_b)).provider_response_hash

        self.assertEqual(hash_a, hash_b)

    def test_changing_usage_metadata_does_not_change_hash(self):
        transport_a = RecordingTransport(
            status_code=200,
            body=_openai_response_body(usage={"prompt_tokens": 1, "total_tokens": 1}),
        )
        transport_b = RecordingTransport(
            status_code=200,
            body=_openai_response_body(usage={"prompt_tokens": 999, "total_tokens": 999}),
        )

        hash_a = _embed(_provider(transport_a)).provider_response_hash
        hash_b = _embed(_provider(transport_b)).provider_response_hash

        self.assertEqual(hash_a, hash_b)

    def test_changing_json_formatting_or_key_order_does_not_change_hash(self):
        pretty = json.dumps(
            {
                "model": _MODEL,
                "data": [{"embedding": list(_VECTOR), "index": 0, "object": "embedding"}],
                "object": "list",
                "usage": {"prompt_tokens": 5, "total_tokens": 5},
            },
            indent=2,
        )
        reordered = json.dumps(
            {
                "usage": {"total_tokens": 5, "prompt_tokens": 5},
                "data": [{"object": "embedding", "index": 0, "embedding": list(_VECTOR)}],
                "object": "list",
                "model": _MODEL,
            },
            separators=(",", ":"),
        )
        transport_a = RecordingTransport(status_code=200, body=pretty)
        transport_b = RecordingTransport(status_code=200, body=reordered)

        hash_a = _embed(_provider(transport_a)).provider_response_hash
        hash_b = _embed(_provider(transport_b)).provider_response_hash

        self.assertEqual(hash_a, hash_b)

    def test_changing_vector_model_or_version_changes_hash(self):
        base = compute_stable_provider_response_hash(
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            embedding_vector=_VECTOR,
        )
        other_vector = compute_stable_provider_response_hash(
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name=_MODEL,
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            embedding_vector=(0.9, 0.8, 0.7),
        )
        other_model = compute_stable_provider_response_hash(
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name="text-embedding-3-large",
            embedding_model_version=_VERSION,
            embedding_dimensions=_DIMENSIONS,
            embedding_vector=_VECTOR,
        )
        other_version = compute_stable_provider_response_hash(
            embedding_provider_name=OPENAI_PROVIDER_NAME,
            embedding_model_name=_MODEL,
            embedding_model_version="2024-02-01",
            embedding_dimensions=_DIMENSIONS,
            embedding_vector=_VECTOR,
        )

        self.assertNotEqual(base, other_vector)
        self.assertNotEqual(base, other_model)
        self.assertNotEqual(base, other_version)


class TestOpenAIEmbeddingProviderErrors(unittest.TestCase):
    def test_malformed_json_fails_closed(self):
        provider = _provider(RecordingTransport(status_code=200, body="{not-json"))

        with self.assertRaises(EmbeddingProviderResponseError):
            _embed(provider)

    def test_missing_data_array_fails_closed(self):
        provider = _provider(
            RecordingTransport(status_code=200, body=json.dumps({"model": _MODEL}))
        )

        with self.assertRaises(EmbeddingProviderResponseError):
            _embed(provider)

    def test_multiple_embeddings_rejected(self):
        body = json.dumps(
            {
                "model": _MODEL,
                "data": [
                    {"embedding": list(_VECTOR)},
                    {"embedding": [0.4, 0.5, 0.6]},
                ],
            }
        )
        provider = _provider(RecordingTransport(status_code=200, body=body))

        with self.assertRaises(EmbeddingProviderResponseError):
            _embed(provider)

    def test_unexpected_response_model_rejected(self):
        provider = _provider(
            RecordingTransport(
                status_code=200,
                body=_openai_response_body(model="unexpected-model"),
            )
        )

        with self.assertRaises(EmbeddingProviderResponseError):
            _embed(provider)

    def test_invalid_vector_values_rejected(self):
        for vector, label in (
            ([0.1, None, 0.3], "null"),
            ([0.1, 0.2], "wrong length"),
            ([0.1, float("nan"), 0.3], "nan"),
            ([0.1, float("inf"), 0.3], "inf"),
            (["a", "b", "c"], "nonnumeric"),
        ):
            with self.subTest(label=label):
                provider = _provider(
                    RecordingTransport(
                        status_code=200,
                        body=_openai_response_body(vector=vector),  # type: ignore[arg-type]
                    )
                )
                with self.assertRaises(EmbeddingProviderResponseError):
                    _embed(provider)

    def test_401_and_403_map_to_auth_error(self):
        for status_code in (401, 403):
            with self.subTest(status_code=status_code):
                provider = _provider(
                    RecordingTransport(
                        status_code=status_code,
                        body='{"error":{"message":"invalid api key"}}',
                    )
                )
                with self.assertRaises(EmbeddingProviderAuthError):
                    _embed(provider)

    def test_429_maps_to_rate_limit_error(self):
        provider = _provider(
            RecordingTransport(
                status_code=429,
                body='{"error":{"message":"rate limit"}}',
            )
        )

        with self.assertRaises(EmbeddingProviderRateLimitError):
            _embed(provider)

    def test_5xx_maps_to_transport_error(self):
        provider = _provider(
            RecordingTransport(
                status_code=503,
                body='{"error":{"message":"upstream unavailable"}}',
            )
        )

        with self.assertRaises(EmbeddingProviderTransportError):
            _embed(provider)

    def test_transport_exception_maps_to_transport_error(self):
        provider = _provider(
            RecordingTransport(
                status_code=200,
                body=_openai_response_body(),
                exc=RuntimeError("connection reset"),
            )
        )

        with self.assertRaises(EmbeddingProviderTransportError):
            _embed(provider)

    def test_exceptions_do_not_expose_api_key_or_raw_payload(self):
        provider = _provider(
            RecordingTransport(
                status_code=401,
                body=json.dumps(
                    {
                        "error": {
                            "message": f"invalid key {_API_KEY} for {_SENSITIVE_TEXT}",
                        }
                    }
                ),
            ),
            api_key=_API_KEY,
        )

        with self.assertRaises(EmbeddingProviderAuthError) as ctx:
            _embed(provider, text=_SENSITIVE_TEXT)

        message = str(ctx.exception)
        self.assertNotIn(_API_KEY, message)
        self.assertNotIn(_SENSITIVE_TEXT, message)
        self.assertNotIn("invalid key", message.lower())


class TestOpenAIEmbeddingProviderPrivacy(unittest.TestCase):
    def test_provider_does_not_log_sensitive_data(self):
        transport = RecordingTransport(status_code=200, body=_openai_response_body())
        provider = _provider(transport)
        logger = logging.getLogger("test.embedding_providers.privacy")
        logger.propagate = True

        with self.assertLogs("test.embedding_providers.privacy", level="INFO") as captured:
            logger.info("embedding_provider.test.begin")
            _embed(provider, text=_SENSITIVE_TEXT)
            logger.info("embedding_provider.test.end")

        combined = "\n".join(captured.output)
        self.assertNotIn(_SENSITIVE_TEXT, combined)
        self.assertNotIn(_API_KEY, combined)
        self.assertNotIn("0.1", combined)
        self.assertNotIn(_openai_response_body(), combined)

    def test_no_live_worker_imports_embedding_providers(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        workers_dir = os.path.join(repo_root, "workers")
        offenders: List[str] = []
        for name in os.listdir(workers_dir):
            if not name.endswith(".py") or name in {"embedding_providers.py", "__init__.py"}:
                continue
            path = os.path.join(workers_dir, name)
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
            if "embedding_providers" in contents:
                offenders.append(name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
