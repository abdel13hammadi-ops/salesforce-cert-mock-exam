"""
Production Anthropic provider for CertBound LLM audit workers (V45 Phase 1).

Implements the ``LlmProvider`` Protocol using the official Anthropic Python SDK
and Messages API with native structured JSON output when supported by the
installed SDK version.

Environment variables
---------------------
CERTBOUND_ANTHROPIC_API_KEY
    Required. API key for Anthropic. Never logged.
CERTBOUND_ANTHROPIC_MODEL
    Default model ID. Default: ``claude-sonnet-4-6``.
CERTBOUND_ANTHROPIC_TIMEOUT_SECONDS
    Per-request timeout in seconds. Default: 120.
CERTBOUND_ANTHROPIC_MAX_OUTPUT_TOKENS
    Maximum output tokens per request. Default: 4096.
CERTBOUND_ANTHROPIC_MAX_RETRIES
    Maximum retry attempts for transient failures. Default: 3.
CERTBOUND_ANTHROPIC_INPUT_COST_PER_MTOK
    Optional input token price (USD per 1M tokens) for cost estimation.
CERTBOUND_ANTHROPIC_OUTPUT_COST_PER_MTOK
    Optional output token price (USD per 1M tokens) for cost estimation.

Retry policy
------------
Retries (with exponential backoff) only for transient failures:
  * rate limits
  * timeouts
  * connection errors
  * 5xx server errors

Does **not** retry:
  * authentication failures
  * bad requests / permanent client errors
  * malformed or schema-invalid model output
  * empty responses
"""

from __future__ import annotations

import copy
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from workers.llm_audit import LlmAuditValidationError, validate_llm_response
from workers.llm_providers import LlmProviderError, LlmResponse, MissingProviderError

logger = logging.getLogger(__name__)

PROVIDER_NAME = "anthropic"

ENV_API_KEY = "CERTBOUND_ANTHROPIC_API_KEY"
ENV_MODEL = "CERTBOUND_ANTHROPIC_MODEL"
ENV_TIMEOUT = "CERTBOUND_ANTHROPIC_TIMEOUT_SECONDS"
ENV_MAX_OUTPUT_TOKENS = "CERTBOUND_ANTHROPIC_MAX_OUTPUT_TOKENS"
ENV_MAX_RETRIES = "CERTBOUND_ANTHROPIC_MAX_RETRIES"
ENV_INPUT_COST = "CERTBOUND_ANTHROPIC_INPUT_COST_PER_MTOK"
ENV_OUTPUT_COST = "CERTBOUND_ANTHROPIC_OUTPUT_COST_PER_MTOK"

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _read_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise LlmProviderError(
            f"{name} must be a number, got {raw!r}"
        ) from exc


def _read_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise LlmProviderError(
            f"{name} must be an integer, got {raw!r}"
        ) from exc
    if value < 0:
        raise LlmProviderError(f"{name} must be non-negative, got {value}")
    return value


def _read_optional_float(name: str) -> Optional[float]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise LlmProviderError(
            f"{name} must be a number, got {raw!r}"
        ) from exc


@dataclass(frozen=True)
class AnthropicProviderConfig:
    api_key: str
    model: str
    timeout: float
    max_output_tokens: int
    max_retries: int
    input_cost_per_mtok: Optional[float] = None
    output_cost_per_mtok: Optional[float] = None


def load_anthropic_config_from_env() -> AnthropicProviderConfig:
    """Load provider configuration from environment variables."""
    api_key = os.environ.get(ENV_API_KEY, "").strip()
    if not api_key:
        raise MissingProviderError(
            f"{ENV_API_KEY} is not set; cannot create Anthropic provider"
        )

    model = os.environ.get(ENV_MODEL, DEFAULT_MODEL).strip() or DEFAULT_MODEL
    timeout = _read_float(ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS)
    max_output_tokens = _read_int(ENV_MAX_OUTPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS)
    max_retries = _read_int(ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES)

    if max_output_tokens <= 0:
        raise LlmProviderError(
            f"{ENV_MAX_OUTPUT_TOKENS} must be positive, got {max_output_tokens}"
        )

    return AnthropicProviderConfig(
        api_key=api_key,
        model=model,
        timeout=timeout,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
        input_cost_per_mtok=_read_optional_float(ENV_INPUT_COST),
        output_cost_per_mtok=_read_optional_float(ENV_OUTPUT_COST),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_user_content(user_prompt: str, metadata: Optional[dict]) -> str:
    """Combine user prompt with question snapshot and resource evidence."""
    sections = [user_prompt.strip()]

    meta = metadata or {}
    question = meta.get("question")
    if isinstance(question, dict) and question:
        sections.append(
            "## Question snapshot\n"
            + json.dumps(question, indent=2, sort_keys=True)
        )

    resource_snapshot = meta.get("resource_snapshot")
    if isinstance(resource_snapshot, dict) and resource_snapshot:
        sections.append(
            "## Official evidence / source chunks\n"
            + json.dumps(resource_snapshot, indent=2, sort_keys=True)
        )

    return "\n\n".join(sections)


def _estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    config: AnthropicProviderConfig,
) -> Optional[float]:
    if (
        config.input_cost_per_mtok is None
        or config.output_cost_per_mtok is None
    ):
        return None
    return (
        (input_tokens * config.input_cost_per_mtok) / 1_000_000.0
        + (output_tokens * config.output_cost_per_mtok) / 1_000_000.0
    )


def _extract_text_content(response: Any) -> str:
    content = getattr(response, "content", None) or []
    texts: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", "")
            if text:
                texts.append(text)
    combined = "".join(texts).strip()
    if not combined:
        raise LlmProviderError("Anthropic response contained no text content")
    return combined


def _parse_json_response(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmProviderError(
            f"Anthropic response was not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise LlmProviderError(
            f"Anthropic response must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _usage_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def _retry_delay_seconds(attempt: int) -> float:
    """Exponential backoff with jitter, capped at 30 seconds."""
    return min((2 ** attempt) + random.uniform(0.0, 1.0), 30.0)


def _is_transient_error(exc: Exception) -> bool:
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        return False

    transient_types = (
        anthropic.RateLimitError,
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
        anthropic.InternalServerError,
    )
    return isinstance(exc, transient_types)


def _is_auth_error(exc: Exception) -> bool:
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        return False
    return isinstance(exc, anthropic.AuthenticationError)


# Validation keywords rejected by Anthropic structured-output JSON Schema.
ANTHROPIC_UNSUPPORTED_SCHEMA_KEYWORDS: frozenset[str] = frozenset({
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
})


def normalize_schema_for_anthropic(schema: dict) -> dict:
    """Return a deep copy of *schema* normalized for Anthropic structured output.

    Anthropic requires ``additionalProperties: false`` on every object node and
    rejects several standard JSON Schema validation keywords. The internal
    ``AUDIT_RESPONSE_SCHEMA`` is left unchanged; this helper produces a
    provider-specific copy only for the API request.
    """
    normalized = copy.deepcopy(schema)
    _normalize_schema_node(normalized)
    return normalized


def _normalize_schema_node(node: object) -> None:
    """Recursively strip unsupported keywords and close object schemas."""
    if isinstance(node, list):
        for item in node:
            _normalize_schema_node(item)
        return

    if not isinstance(node, dict):
        return

    for key in ANTHROPIC_UNSUPPORTED_SCHEMA_KEYWORDS:
        node.pop(key, None)

    if node.get("type") == "object":
        node["additionalProperties"] = False

    properties = node.get("properties")
    if isinstance(properties, dict):
        for subschema in properties.values():
            _normalize_schema_node(subschema)

    pattern_properties = node.get("patternProperties")
    if isinstance(pattern_properties, dict):
        for subschema in pattern_properties.values():
            _normalize_schema_node(subschema)

    for defs_key in ("$defs", "definitions"):
        defs = node.get(defs_key)
        if isinstance(defs, dict):
            for subschema in defs.values():
                _normalize_schema_node(subschema)

    items = node.get("items")
    if isinstance(items, dict):
        _normalize_schema_node(items)
    elif isinstance(items, list):
        for subschema in items:
            _normalize_schema_node(subschema)

    additional = node.get("additionalProperties")
    if isinstance(additional, dict):
        _normalize_schema_node(additional)

    for combiner in ("allOf", "anyOf", "oneOf"):
        group = node.get(combiner)
        if isinstance(group, list):
            for subschema in group:
                _normalize_schema_node(subschema)


def _build_create_kwargs(
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    response_schema: dict,
    config: AnthropicProviderConfig,
    timeout_override: Optional[float] = None,
) -> dict:
    kwargs: dict = {
        "model": model,
        "max_tokens": config.max_output_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
        "timeout": timeout_override if timeout_override is not None else config.timeout,
    }

    # Prefer GA structured output when the installed SDK supports it.
    kwargs["output_config"] = {
        "format": {
            "type": "json_schema",
            "schema": normalize_schema_for_anthropic(response_schema),
        }
    }
    return kwargs


def _create_message(client: Any, kwargs: dict) -> Any:
    """Call Messages API, falling back when structured output is unsupported."""
    try:
        return client.messages.create(**kwargs)
    except TypeError:
        # Older SDK without output_config — retry without structured output.
        fallback = dict(kwargs)
        fallback.pop("output_config", None)
        logger.warning(
            "Anthropic SDK does not support output_config; "
            "falling back to plain JSON response parsing"
        )
        return client.messages.create(**fallback)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class AnthropicAuditProvider:
    """Production Anthropic provider for CertBound audit jobs."""

    def __init__(
        self,
        config: AnthropicProviderConfig,
        *,
        client: Any = None,
        client_factory: Optional[Callable[[AnthropicProviderConfig], Any]] = None,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._client = client

    @classmethod
    def from_env(
        cls,
        *,
        client: Any = None,
        client_factory: Optional[Callable[[AnthropicProviderConfig], Any]] = None,
    ) -> "AnthropicAuditProvider":
        return cls(
            load_anthropic_config_from_env(),
            client=client,
            client_factory=client_factory,
        )

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(self._config)
        return self._client

    def __call__(
        self,
        *,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict,
        metadata: Optional[dict] = None,
    ) -> LlmResponse:
        model = (model_name or "").strip() or self._config.model
        user_content = _build_user_content(user_prompt, metadata)
        timeout_override: Optional[float] = None
        if metadata and metadata.get("timeout_seconds") is not None:
            timeout_override = float(metadata["timeout_seconds"])
        kwargs = _build_create_kwargs(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            response_schema=response_schema,
            config=self._config,
            timeout_override=timeout_override,
        )

        client = self._get_client()
        last_error: Optional[Exception] = None

        for attempt in range(self._config.max_retries + 1):
            try:
                response = _create_message(client, kwargs)
                text = _extract_text_content(response)
                parsed = _parse_json_response(text)
                # Enforce CertBound audit contract before returning.
                validate_llm_response(parsed)
                input_tokens, output_tokens = _usage_tokens(response)
                request_id = getattr(response, "id", None)

                return LlmResponse(
                    parsed_response=parsed,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    actual_cost_usd=_estimate_cost_usd(
                        input_tokens, output_tokens, self._config
                    ),
                    provider_request_id=str(request_id) if request_id else None,
                    model_name=model,
                    provider_name=PROVIDER_NAME,
                )
            except LlmAuditValidationError:
                # Schema-invalid output is permanent; never retry.
                raise
            except LlmProviderError:
                raise
            except Exception as exc:
                last_error = exc
                if _is_auth_error(exc):
                    raise LlmProviderError(
                        "Anthropic authentication failed; check "
                        f"{ENV_API_KEY}"
                    ) from exc

                if _is_transient_error(exc) and attempt < self._config.max_retries:
                    delay = _retry_delay_seconds(attempt)
                    logger.warning(
                        "Anthropic transient error on attempt %s/%s; "
                        "retrying in %.1fs: %s",
                        attempt + 1,
                        self._config.max_retries + 1,
                        delay,
                        type(exc).__name__,
                    )
                    time.sleep(delay)
                    continue

                if _is_transient_error(exc):
                    raise LlmProviderError(
                        f"Anthropic request failed after "
                        f"{self._config.max_retries + 1} attempts: {exc}"
                    ) from exc

                raise LlmProviderError(
                    f"Anthropic request failed: {exc}"
                ) from exc

        raise LlmProviderError(
            f"Anthropic request failed after retries: {last_error}"
        )


def _default_client_factory(config: AnthropicProviderConfig) -> Any:
    try:
        import anthropic  # noqa: PLC0415
    except ImportError as exc:
        raise LlmProviderError(
            "anthropic package is not installed; run: pip install anthropic"
        ) from exc

    return anthropic.Anthropic(
        api_key=config.api_key,
        timeout=config.timeout,
        max_retries=0,  # bounded retries handled in AnthropicAuditProvider
    )


def build_anthropic_provider_from_env() -> AnthropicAuditProvider:
    """Convenience factory for worker wiring."""
    return AnthropicAuditProvider.from_env()
