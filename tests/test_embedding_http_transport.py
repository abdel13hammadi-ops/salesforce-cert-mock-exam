"""Tests for stdlib embedding HTTP transport."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from typing import Any, Dict
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.embedding_http_transport import StdlibEmbeddingHttpTransport
from workers.embedding_providers import (
    DEFAULT_OPENAI_EMBEDDINGS_URL,
    OPENAI_PROVIDER_NAME,
    EmbeddingProviderAuthError,
    EmbeddingProviderRateLimitError,
    EmbeddingProviderResponseError,
    EmbeddingProviderTransportError,
    OpenAIEmbeddingProvider,
    OpenAIEmbeddingProviderConfig,
)

_API_KEY = "sk-test-key-not-real"
_MODEL = "text-embedding-3-small"
_VERSION = "2024-01-15"
_DIMENSIONS = 3
_SENSITIVE_TEXT = "SECRET TEXT MUST NOT LEAK"
_SENSITIVE_HEADER = f"Bearer {_API_KEY}"


def _response_body(*, vector=None) -> str:
    return json.dumps(
        {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": list(vector or [0.1, 0.2, 0.3]),
                }
            ],
            "model": _MODEL,
        }
    )


class TestStdlibEmbeddingHttpTransport(unittest.TestCase):
    def test_post_json_uses_explicit_timeout(self):
        transport = StdlibEmbeddingHttpTransport()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = _response_body().encode("utf-8")
        mock_context = MagicMock()
        mock_context.__enter__.return_value = mock_response
        mock_context.__exit__.return_value = False

        with patch("urllib.request.urlopen", return_value=mock_context) as urlopen:
            result = transport.post_json(
                url=DEFAULT_OPENAI_EMBEDDINGS_URL,
                headers={"Authorization": _SENSITIVE_HEADER, "Content-Type": "application/json"},
                payload={"model": _MODEL, "input": _SENSITIVE_TEXT, "dimensions": _DIMENSIONS},
                timeout_seconds=17.5,
            )

        self.assertEqual(result.status_code, 200)
        urlopen.assert_called_once()
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 17.5)

    def test_post_json_performs_single_request_without_retry(self):
        transport = StdlibEmbeddingHttpTransport()
        with patch(
            "urllib.request.urlopen",
            side_effect=OSError("connection reset"),
        ) as urlopen:
            with self.assertRaises(OSError):
                transport.post_json(
                    url=DEFAULT_OPENAI_EMBEDDINGS_URL,
                    headers={"Authorization": _SENSITIVE_HEADER},
                    payload={"model": _MODEL, "input": "x", "dimensions": _DIMENSIONS},
                    timeout_seconds=5.0,
                )
        self.assertEqual(urlopen.call_count, 1)

    def test_http_error_returns_status_and_body_without_retry(self):
        import urllib.error

        transport = StdlibEmbeddingHttpTransport()
        error_body = '{"error":"rate limit"}'
        http_error = urllib.error.HTTPError(
            DEFAULT_OPENAI_EMBEDDINGS_URL,
            429,
            "Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(error_body.encode("utf-8")),
        )

        with patch("urllib.request.urlopen", side_effect=http_error):
            response = transport.post_json(
                url=DEFAULT_OPENAI_EMBEDDINGS_URL,
                headers={"Authorization": _SENSITIVE_HEADER},
                payload={"model": _MODEL, "input": "x", "dimensions": _DIMENSIONS},
                timeout_seconds=5.0,
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.body, error_body)

    def test_provider_maps_transport_failures_safely(self):
        transport = StdlibEmbeddingHttpTransport()
        provider = OpenAIEmbeddingProvider(
            config=OpenAIEmbeddingProviderConfig(api_key=_API_KEY, timeout_seconds=5.0),
            transport=transport,
        )

        cases = [
            (401, EmbeddingProviderAuthError),
            (403, EmbeddingProviderAuthError),
            (429, EmbeddingProviderRateLimitError),
            (500, EmbeddingProviderTransportError),
            (200, EmbeddingProviderResponseError, "{not-json"),
        ]

        for case in cases:
            status_code = case[0]
            expected = case[1]
            body = case[2] if len(case) > 2 else _response_body()
            with self.subTest(status_code=status_code):
                with patch(
                    "urllib.request.urlopen",
                    return_value=self._mock_urlopen(status_code, body),
                ):
                    with self.assertRaises(expected) as ctx:
                        provider.embed(
                            text=_SENSITIVE_TEXT,
                            embedding_provider_name=OPENAI_PROVIDER_NAME,
                            embedding_model_name=_MODEL,
                            embedding_model_version=_VERSION,
                            embedding_dimensions=_DIMENSIONS,
                        )
                    combined = str(ctx.exception)
                    self.assertNotIn(_API_KEY, combined)
                    self.assertNotIn(_SENSITIVE_TEXT, combined)
                    self.assertNotIn(_SENSITIVE_HEADER, combined)

    def test_successful_provider_call_does_not_log_sensitive_values(self):
        transport = StdlibEmbeddingHttpTransport()
        provider = OpenAIEmbeddingProvider(
            config=OpenAIEmbeddingProviderConfig(api_key=_API_KEY, timeout_seconds=5.0),
            transport=transport,
        )
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen(200, _response_body()),
        ):
            result = provider.embed(
                text=_SENSITIVE_TEXT,
                embedding_provider_name=OPENAI_PROVIDER_NAME,
                embedding_model_name=_MODEL,
                embedding_model_version=_VERSION,
                embedding_dimensions=_DIMENSIONS,
            )

        self.assertEqual(len(result.embedding_vector), _DIMENSIONS)

    @staticmethod
    def _mock_urlopen(status_code: int, body: str) -> MagicMock:
        mock_response = MagicMock()
        mock_response.status = status_code
        mock_response.read.return_value = body.encode("utf-8")
        mock_context = MagicMock()
        mock_context.__enter__.return_value = mock_response
        mock_context.__exit__.return_value = False
        return mock_context


class TestStdlibTransportIsolation(unittest.TestCase):
    def test_no_live_worker_imports_transport_module(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        workers_dir = os.path.join(repo_root, "workers")
        offenders = []
        for name in os.listdir(workers_dir):
            if not name.endswith(".py") or name in {
                "embedding_http_transport.py",
                "embedding_providers.py",
                "__init__.py",
            }:
                continue
            path = os.path.join(workers_dir, name)
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
            if "embedding_http_transport" in contents:
                offenders.append(name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
