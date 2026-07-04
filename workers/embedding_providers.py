"""OpenAI-compatible embedding providers for V48 hybrid retrieval.

Offline adapter layer only: no live worker wiring, no automatic retries, and
no logging of input text, vectors, credentials, or raw provider payloads.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8 fallback
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

from workers.embedding_cache import EmbeddingProviderResponse
from workers.resource_chunking import sha256_hex

OPENAI_PROVIDER_NAME = "openai"
DEFAULT_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


class EmbeddingProviderError(Exception):
    """Base error for embedding-provider adapters."""


class EmbeddingProviderConfigError(EmbeddingProviderError):
    """Raised when provider configuration or request preconditions are invalid."""


class EmbeddingProviderTransportError(EmbeddingProviderError):
    """Raised when the provider HTTP/API transport fails."""


class EmbeddingProviderAuthError(EmbeddingProviderTransportError):
    """Raised when the provider rejects credentials."""


class EmbeddingProviderRateLimitError(EmbeddingProviderTransportError):
    """Raised when the provider reports rate limiting."""


class EmbeddingProviderResponseError(EmbeddingProviderError):
    """Raised when a provider response is malformed or fails validation."""


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: str


@runtime_checkable
class EmbeddingHttpTransport(Protocol):
    """Injectable HTTP transport for embedding-provider adapters."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> HttpResponse:
        """Perform exactly one HTTP POST with a JSON body."""


@dataclass(frozen=True)
class OpenAIEmbeddingProviderConfig:
    api_key: str
    timeout_seconds: float = 30.0
    embeddings_url: str = DEFAULT_OPENAI_EMBEDDINGS_URL


class OpenAIEmbeddingProvider:
    """OpenAI-compatible embedding adapter implementing ``EmbeddingProvider``."""

    def __init__(
        self,
        *,
        config: OpenAIEmbeddingProviderConfig,
        transport: EmbeddingHttpTransport,
    ) -> None:
        if config.timeout_seconds <= 0:
            raise EmbeddingProviderConfigError("timeout_seconds must be positive")
        if not str(config.embeddings_url or "").strip():
            raise EmbeddingProviderConfigError("embeddings_url must be nonempty")
        self._config = config
        self._transport = transport

    def embed(
        self,
        *,
        text: str,
        embedding_provider_name: str,
        embedding_model_name: str,
        embedding_model_version: str,
        embedding_dimensions: int,
    ) -> EmbeddingProviderResponse:
        _validate_embed_request(
            text=text,
            embedding_provider_name=embedding_provider_name,
            embedding_model_name=embedding_model_name,
            embedding_model_version=embedding_model_version,
            embedding_dimensions=embedding_dimensions,
            api_key=self._config.api_key,
        )

        payload = {
            "model": embedding_model_name.strip(),
            "input": text,
            "dimensions": embedding_dimensions,
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key.strip()}",
            "Content-Type": "application/json",
        }

        try:
            response = self._transport.post_json(
                url=self._config.embeddings_url,
                headers=headers,
                payload=payload,
                timeout_seconds=self._config.timeout_seconds,
            )
        except Exception as exc:
            raise EmbeddingProviderTransportError(
                "embedding provider transport request failed"
            ) from exc

        _raise_for_http_status(response.status_code)
        parsed = _parse_openai_response_body(response.body)

        vector = _extract_validated_embedding_vector(
            parsed,
            expected_model_name=embedding_model_name.strip(),
            embedding_dimensions=embedding_dimensions,
        )
        provider_response_hash = compute_stable_provider_response_hash(
            embedding_provider_name=embedding_provider_name.strip(),
            embedding_model_name=embedding_model_name.strip(),
            embedding_model_version=embedding_model_version.strip(),
            embedding_dimensions=embedding_dimensions,
            embedding_vector=vector,
        )
        return EmbeddingProviderResponse(
            embedding_vector=vector,
            provider_response_hash=provider_response_hash,
        )


def compute_stable_provider_response_hash(
    *,
    embedding_provider_name: str,
    embedding_model_name: str,
    embedding_model_version: str,
    embedding_dimensions: int,
    embedding_vector: Sequence[float],
) -> str:
    """Return lowercase SHA-256 over canonical stable embedding-result JSON."""
    canonical_payload = {
        "embedding_provider_name": embedding_provider_name,
        "embedding_model_name": embedding_model_name,
        "embedding_model_version": embedding_model_version,
        "embedding_dimensions": embedding_dimensions,
        "embedding_vector": [float(value) for value in embedding_vector],
    }
    canonical_json = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256_hex(canonical_json)


def _validate_embed_request(
    *,
    text: str,
    embedding_provider_name: str,
    embedding_model_name: str,
    embedding_model_version: str,
    embedding_dimensions: int,
    api_key: str,
) -> None:
    if not str(text).strip():
        raise EmbeddingProviderConfigError("embedding input text must be nonempty")
    if not str(api_key or "").strip():
        raise EmbeddingProviderConfigError("OpenAI API key configuration is missing")
    if embedding_provider_name.strip() != OPENAI_PROVIDER_NAME:
        raise EmbeddingProviderConfigError(
            f"unsupported embedding provider identity: {embedding_provider_name.strip()!r}"
        )
    for field_name, value in (
        ("embedding_model_name", embedding_model_name),
        ("embedding_model_version", embedding_model_version),
    ):
        if not str(value or "").strip():
            raise EmbeddingProviderConfigError(f"{field_name} must be nonempty")
    if embedding_dimensions <= 0:
        raise EmbeddingProviderConfigError("embedding_dimensions must be positive")


def _parse_openai_response_body(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise EmbeddingProviderResponseError(
            "embedding provider returned malformed JSON"
        ) from exc


def _raise_for_http_status(status_code: int) -> None:
    if 200 <= status_code < 300:
        return
    if status_code in (401, 403):
        raise EmbeddingProviderAuthError(
            "embedding provider authentication failed"
        )
    if status_code == 429:
        raise EmbeddingProviderRateLimitError(
            "embedding provider rate limit exceeded"
        )
    if status_code >= 500:
        raise EmbeddingProviderTransportError(
            "embedding provider transport failed with server error"
        )
    raise EmbeddingProviderTransportError(
        "embedding provider transport failed with HTTP error"
    )


def _extract_validated_embedding_vector(
    parsed: Any,
    *,
    expected_model_name: str,
    embedding_dimensions: int,
) -> Tuple[float, ...]:
    if not isinstance(parsed, Mapping):
        raise EmbeddingProviderResponseError(
            "embedding provider response must be a JSON object"
        )

    response_model = parsed.get("model")
    if response_model is not None and str(response_model) != expected_model_name:
        raise EmbeddingProviderResponseError(
            "embedding provider response model does not match request model"
        )

    data = parsed.get("data")
    if not isinstance(data, list) or not data:
        raise EmbeddingProviderResponseError(
            "embedding provider response data must be a nonempty array"
        )
    if len(data) != 1:
        raise EmbeddingProviderResponseError(
            "embedding provider response must contain exactly one embedding"
        )

    first = data[0]
    if not isinstance(first, Mapping):
        raise EmbeddingProviderResponseError(
            "embedding provider response item must be a JSON object"
        )
    if "embedding" not in first:
        raise EmbeddingProviderResponseError(
            "embedding provider response is missing embedding vector"
        )

    return _coerce_embedding_vector(
        first["embedding"],
        embedding_dimensions=embedding_dimensions,
    )


def _coerce_embedding_vector(
    values: Any,
    *,
    embedding_dimensions: int,
) -> Tuple[float, ...]:
    if values is None:
        raise EmbeddingProviderResponseError("embedding vector must not be null")
    if isinstance(values, (str, bytes, dict)):
        raise EmbeddingProviderResponseError("embedding vector must be one-dimensional")

    if isinstance(values, Sequence):
        if values and isinstance(values[0], Sequence) and not isinstance(values[0], (str, bytes)):
            raise EmbeddingProviderResponseError("embedding vector must be one-dimensional")
        coerced: list[float] = []
        for index, item in enumerate(values):
            if item is None:
                raise EmbeddingProviderResponseError(
                    "embedding vector must not contain null values"
                )
            try:
                number = float(item)
            except (TypeError, ValueError) as exc:
                raise EmbeddingProviderResponseError(
                    "embedding vector must contain only numeric values"
                ) from exc
            if math.isnan(number) or math.isinf(number):
                raise EmbeddingProviderResponseError(
                    f"embedding vector contains non-finite value at index {index}"
                )
            coerced.append(number)
        if len(coerced) != embedding_dimensions:
            raise EmbeddingProviderResponseError(
                "embedding vector length does not match requested dimensions"
            )
        return tuple(coerced)

    raise EmbeddingProviderResponseError("embedding vector must be one-dimensional")
