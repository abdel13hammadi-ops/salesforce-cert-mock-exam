"""
Production OpenAI provider for CertBound LLM audit workers (V58 Day 8).

Implements the ``LlmProvider`` Protocol using the official OpenAI Python SDK
and the **Responses API** (``client.responses.create``) with native
structured JSON output via ``text={"format": {"type": "json_schema", ...}}``.

Environment variables
---------------------
CERTBOUND_OPENAI_API_KEY
    Required. API key for OpenAI. Never logged.
CERTBOUND_OPENAI_MODEL
    Default model ID. Default: ``gpt-5.5``.
CERTBOUND_OPENAI_REASONING_EFFORT
    Reasoning effort sent on every request. One of: none, low, medium, high,
    xhigh. Default: ``medium``. Invalid values are rejected at configuration
    load time.
CERTBOUND_OPENAI_TIMEOUT_SECONDS
    Per-request timeout in seconds. Must be positive. Default: 120.
CERTBOUND_OPENAI_MAX_OUTPUT_TOKENS
    Maximum output tokens per request. Must be positive. Default: 4096.
CERTBOUND_OPENAI_MAX_RETRIES
    Maximum retry attempts (after the first attempt) for transient
    failures. Must be non-negative. Default: 3.
CERTBOUND_OPENAI_INPUT_COST_PER_MTOK
    Optional input token price (USD per 1M tokens) for cost estimation.
CERTBOUND_OPENAI_OUTPUT_COST_PER_MTOK
    Optional output token price (USD per 1M tokens) for cost estimation.

Privacy and statelessness
--------------------------
Every request explicitly sets ``store=False``. This provider never uses
background mode, conversations, ``previous_response_id``, or any other
server-side response chaining -- each Pass A, B, or C invocation is a fully
independent request containing only the supplied system and user prompts.

Retry policy
------------
Retries (with exponential backoff), owned entirely by this provider --
the underlying SDK client is constructed with ``max_retries=0`` so retries
are never duplicated. Only transient failures are retried:
  * rate limits
  * timeouts
  * connection errors
  * 5xx server errors

Does **not** retry:
  * authentication / permission / bad-request failures
  * refusals
  * incomplete responses (including max-output-token truncation)
  * malformed or schema-invalid model output
  * empty responses
  * local configuration / schema-normalization errors

Error diagnostics
------------------
Every ``LlmProviderError`` raised for an OpenAI SDK exception includes a
compact, sanitized diagnostic string (see ``describe_openai_error``) with
only safe structured fields -- HTTP status, error type, error code, error
param, and request id, plus a clean error message when one is safely
available. The API key, authorization headers, full request/response
bodies, prompt content, and raw SDK objects are never included, and the
combined string is length-bounded.
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
from workers.llm_providers import (
    LlmProviderError,
    LlmResponse,
    MissingProviderError,
    SKIP_LEGACY_LLM_AUDIT_VALIDATION_METADATA_KEY,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "openai"

ENV_API_KEY = "CERTBOUND_OPENAI_API_KEY"
ENV_MODEL = "CERTBOUND_OPENAI_MODEL"
ENV_REASONING_EFFORT = "CERTBOUND_OPENAI_REASONING_EFFORT"
ENV_TIMEOUT = "CERTBOUND_OPENAI_TIMEOUT_SECONDS"
ENV_MAX_OUTPUT_TOKENS = "CERTBOUND_OPENAI_MAX_OUTPUT_TOKENS"
ENV_MAX_RETRIES = "CERTBOUND_OPENAI_MAX_RETRIES"
ENV_INPUT_COST = "CERTBOUND_OPENAI_INPUT_COST_PER_MTOK"
ENV_OUTPUT_COST = "CERTBOUND_OPENAI_OUTPUT_COST_PER_MTOK"

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_MAX_RETRIES = 3

ALLOWED_REASONING_EFFORTS: frozenset[str] = frozenset(
    {"none", "low", "medium", "high", "xhigh"}
)


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
class OpenAIProviderConfig:
    api_key: str
    model: str
    reasoning_effort: str
    timeout: float
    max_output_tokens: int
    max_retries: int
    input_cost_per_mtok: Optional[float] = None
    output_cost_per_mtok: Optional[float] = None


def resolve_openai_model_from_env() -> str:
    """Return the configured OpenAI model ID without requiring an API key."""
    model = os.environ.get(ENV_MODEL, DEFAULT_MODEL).strip()
    return model or DEFAULT_MODEL


def _resolve_reasoning_effort_from_env() -> str:
    raw = os.environ.get(ENV_REASONING_EFFORT, "").strip().lower()
    if not raw:
        return DEFAULT_REASONING_EFFORT
    if raw not in ALLOWED_REASONING_EFFORTS:
        raise LlmProviderError(
            f"{ENV_REASONING_EFFORT} must be one of "
            f"{sorted(ALLOWED_REASONING_EFFORTS)}, got {raw!r}"
        )
    return raw


def load_openai_config_from_env() -> OpenAIProviderConfig:
    """Load provider configuration from environment variables."""
    api_key = os.environ.get(ENV_API_KEY, "").strip()
    if not api_key:
        raise MissingProviderError(
            f"{ENV_API_KEY} is not set; cannot create OpenAI provider"
        )

    model = resolve_openai_model_from_env()
    reasoning_effort = _resolve_reasoning_effort_from_env()
    timeout = _read_float(ENV_TIMEOUT, DEFAULT_TIMEOUT_SECONDS)
    if timeout <= 0:
        raise LlmProviderError(f"{ENV_TIMEOUT} must be positive, got {timeout}")

    max_output_tokens = _read_int(ENV_MAX_OUTPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS)
    if max_output_tokens <= 0:
        raise LlmProviderError(
            f"{ENV_MAX_OUTPUT_TOKENS} must be positive, got {max_output_tokens}"
        )

    max_retries = _read_int(ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES)

    return OpenAIProviderConfig(
        api_key=api_key,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
        max_output_tokens=max_output_tokens,
        max_retries=max_retries,
        input_cost_per_mtok=_read_optional_float(ENV_INPUT_COST),
        output_cost_per_mtok=_read_optional_float(ENV_OUTPUT_COST),
    )


# ---------------------------------------------------------------------------
# Prompt / usage / cost helpers
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
    config: OpenAIProviderConfig,
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


def _usage_tokens(response: Any) -> "tuple[int, int]":
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
        import openai  # noqa: PLC0415
    except ImportError:
        return False

    transient_types = (
        openai.RateLimitError,
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.InternalServerError,
    )
    return isinstance(exc, transient_types)


def _is_auth_error(exc: Exception) -> bool:
    try:
        import openai  # noqa: PLC0415
    except ImportError:
        return False
    return isinstance(exc, openai.AuthenticationError)


# ---------------------------------------------------------------------------
# Sanitized OpenAI SDK error diagnostics
# ---------------------------------------------------------------------------
#
# openai.APIStatusError (base of BadRequestError, AuthenticationError,
# PermissionDeniedError, RateLimitError, InternalServerError, etc.; verified
# against the installed openai==2.44.0 sources) exposes:
#   * status_code        -- HTTP status code (int)
#   * type / code / param -- parsed from the response body's ``error`` object
#     by the SDK's own ``_make_status_error`` (only populated when the SDK
#     could parse a JSON body)
#   * request_id         -- ``response.headers.get("x-request-id")``
#   * body               -- the (already ``error``-unwrapped) parsed body
#     dict, the raw response text if it wasn't JSON, or ``None`` if the
#     response was closed before it could be read
#   * message            -- **not** a clean human message: the SDK sets this
#     to ``f"Error code: {status} - {body}"`` (the *entire* body dict
#     rendered inline) whenever a body exists, and to the short
#     ``f"Error code: {status}"`` only when there is no body at all.
#
# Because ``exc.message`` / ``str(exc)`` can embed the full response body,
# it is only trusted here when no body was captured. Otherwise, only
# ``body["message"]`` (a single already-parsed string field) is used, and
# only when it is actually a non-empty string. This is the fix for the
# original bug: the provider previously did ``f"OpenAI request failed: {exc}"``,
# which is safe-looking for the "closed response" case (short) but discards
# type/code/param/request_id entirely, and would have been unsafe if applied
# uncritically whenever a body preview happened to be short.

_MAX_DIAGNOSTIC_FIELD_LENGTH = 200
_MAX_DIAGNOSTIC_MESSAGE_FIELD_LENGTH = 300
_MAX_DIAGNOSTIC_TOTAL_LENGTH = 500


def _sanitize_diagnostic_text(
    value: Optional[Any], *, max_length: int, api_key: Optional[str] = None
) -> Optional[str]:
    """Collapse control characters/newlines to single spaces, redact the
    literal API key if present, and bound a single diagnostic field.

    Never raises; returns ``None`` for empty/whitespace-only input.
    """
    if value is None:
        return None
    text = str(value)
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text


def _extract_openai_error_message(exc: Exception) -> Optional[str]:
    """Return a clean human error message only when it is safe to do so.

    Deliberately does not fall back to ``exc.message`` / ``str(exc)`` when a
    response body was captured but was not a dict or lacked a ``message``
    key, because the SDK embeds the *entire* body inline in that string
    whenever any body exists. ``exc.message`` / ``str(exc)`` is only trusted
    when the SDK captured no body at all (guaranteed short by the SDK).
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        candidate = body.get("message")
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        return None
    if body is not None:
        # Raw / non-JSON body text -- never surfaced, even truncated.
        return None
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message
    text = str(exc).strip()
    return text or None


def _extract_openai_request_id(exc: Exception) -> Optional[str]:
    """Return the provider request id from ``exc.request_id`` or, failing
    that, directly from response headers (``x-request-id``)."""
    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str) and request_id.strip():
        return request_id
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            header_value = headers.get("x-request-id")
        except Exception:  # defensive: headers may not be a real mapping
            header_value = None
        if isinstance(header_value, str) and header_value.strip():
            return header_value
    return None


def describe_openai_error(exc: Exception, *, api_key: Optional[str] = None) -> str:
    """Build a compact, sanitized, length-bounded diagnostic string for an
    OpenAI SDK exception.

    Extracts only: HTTP status code, error type, error code, error param,
    request id, and (when safely available) a clean error message -- see
    the module comment above for exactly what is and is not trusted from
    the SDK exception. Never includes the API key, authorization headers,
    full request/response bodies, prompt content, or SDK/HTTP object
    representations. Fields that are absent are simply omitted.
    """
    status_code = getattr(exc, "status_code", None)
    field_order = (
        ("status", str(status_code) if status_code is not None else None, _MAX_DIAGNOSTIC_FIELD_LENGTH),
        ("type", getattr(exc, "type", None), _MAX_DIAGNOSTIC_FIELD_LENGTH),
        ("code", getattr(exc, "code", None), _MAX_DIAGNOSTIC_FIELD_LENGTH),
        ("param", getattr(exc, "param", None), _MAX_DIAGNOSTIC_FIELD_LENGTH),
        ("request_id", _extract_openai_request_id(exc), _MAX_DIAGNOSTIC_FIELD_LENGTH),
        ("message", _extract_openai_error_message(exc), _MAX_DIAGNOSTIC_MESSAGE_FIELD_LENGTH),
    )
    parts = []
    for name, raw_value, max_length in field_order:
        sanitized = _sanitize_diagnostic_text(raw_value, max_length=max_length, api_key=api_key)
        if sanitized:
            parts.append(f"{name}={sanitized}")
    if not parts:
        parts.append(f"error_class={type(exc).__name__}")
    diagnostics = ", ".join(parts)
    return _sanitize_diagnostic_text(
        diagnostics, max_length=_MAX_DIAGNOSTIC_TOTAL_LENGTH, api_key=api_key
    ) or f"error_class={type(exc).__name__}"


# ---------------------------------------------------------------------------
# Structured-output schema normalization
# ---------------------------------------------------------------------------
#
# OpenAI's strict json_schema structured output requires:
#   * every object node is closed (``additionalProperties: false``);
#   * every declared property appears in that object's ``required`` array
#     (optionality is expressed with nullable unions, never omission).
#
# Unlike Anthropic, OpenAI's strict mode *does* support the full standard
# JSON Schema validation-keyword vocabulary (enums, minItems/maxItems,
# numeric bounds, string length/pattern/format, etc.), so none of those are
# stripped here.
#
# The one genuinely free-form node in the canonical Pass A/B/C schemas is
# each proposed finding's ``metadata: {"type": "object"}``
# (see ``_proposed_finding_schema`` in ``ai_quality_audit_prompts.py``).
# OpenAI strict mode cannot represent an open-ended object at all. Inspection
# of ``workers/ai_quality_audit_schemas.py`` shows this field is *not* purely
# decorative: ``_validate_source_support_context`` requires a real, populated
# ``metadata.source_support_context`` object (with ``attempted_retrieval``,
# ``evidence_limitation``, ``proposed_technical_claim``, and
# ``insufficiency_reason``) whenever a proposed finding uses
# ``finding_code == "SOURCE_SUPPORT_WEAK"`` with no supporting evidence
# chunks. Collapsing ``metadata`` to an always-empty closed object would
# silently make that legitimate finding shape unreachable for OpenAI-backed
# runs. Instead, every schema-less object node is replaced with a closed
# schema that always requires a (nullable) ``source_support_context``
# property: ``null`` when not applicable, or the fully populated object
# when it is. This fully preserves the one known semantic use of the field
# while still satisfying OpenAI's strict-mode requirements. No other
# metadata sub-keys are read by any validator, worker, or persistence path,
# so nothing else is added. The canonical ``PASS_A/B/C_RESPONSE_SCHEMA``
# constants (and ``_proposed_finding_schema``) are never mutated; this
# produces a provider-local copy only, used solely for the outbound OpenAI
# request.

_SOURCE_SUPPORT_CONTEXT_SCHEMA: Dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "attempted_retrieval": {"type": "integer"},
        "evidence_limitation": {"type": "string"},
        "proposed_technical_claim": {"type": "string"},
        "insufficiency_reason": {"type": "string"},
    },
    "required": [
        "attempted_retrieval",
        "evidence_limitation",
        "proposed_technical_claim",
        "insufficiency_reason",
    ],
}


def _free_form_object_replacement() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_support_context": copy.deepcopy(_SOURCE_SUPPORT_CONTEXT_SCHEMA),
        },
        "required": ["source_support_context"],
    }


def _is_object_type(type_value: object) -> bool:
    if type_value == "object":
        return True
    if isinstance(type_value, list):
        return "object" in type_value
    return False


def normalize_schema_for_openai(schema: dict) -> dict:
    """Return a deep copy of *schema* normalized for OpenAI strict structured
    output.

    See the module-level comment above for the rationale behind each
    transformation. The supplied *schema* (and any of the shared
    ``PASS_A/B/C_RESPONSE_SCHEMA`` constants it may reference) is never
    mutated.
    """
    normalized = copy.deepcopy(schema)
    _normalize_schema_node(normalized)
    return normalized


def _normalize_schema_node(node: object) -> None:
    """Recursively close object schemas and require every declared property.

    Mutates *node* in place; callers must only invoke this on a deep copy.
    """
    if isinstance(node, list):
        for item in node:
            _normalize_schema_node(item)
        return

    if not isinstance(node, dict):
        return

    if node.get("type") == "object" and "properties" not in node:
        # Genuinely free-form object (e.g. a proposed finding's ``metadata``).
        # OpenAI strict mode cannot represent this; replace it in place with
        # the closed, source-support-context-preserving shape described in
        # the module-level comment above.
        replacement = _free_form_object_replacement()
        node.clear()
        node.update(replacement)

    if _is_object_type(node.get("type")):
        node["additionalProperties"] = False
        properties = node.get("properties")
        if isinstance(properties, dict):
            required = set(node.get("required") or [])
            required.update(properties.keys())
            node["required"] = sorted(required)
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


# ---------------------------------------------------------------------------
# Request construction / response extraction
# ---------------------------------------------------------------------------

def _build_create_kwargs(
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    response_schema: dict,
    config: OpenAIProviderConfig,
    timeout_override: Optional[float] = None,
) -> dict:
    return {
        "model": model,
        "instructions": system_prompt,
        "input": user_content,
        "max_output_tokens": config.max_output_tokens,
        "reasoning": {"effort": config.reasoning_effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "certbound_audit_response",
                "schema": normalize_schema_for_openai(response_schema),
                "strict": True,
            }
        },
        "store": False,
        "timeout": timeout_override if timeout_override is not None else config.timeout,
    }


def _find_refusal(response: Any) -> Optional[str]:
    """Return the refusal explanation if the response contains one."""
    output = getattr(response, "output", None) or []
    for item in output:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                return getattr(content, "refusal", None) or "model refused to respond"
    return None


def _extract_text_or_raise(response: Any) -> str:
    """Validate response completion state and return the raw output text.

    Explicitly handles refusals, incomplete responses, and empty output
    before any JSON parsing is attempted, so truncated or non-JSON output
    is never fed into ``json.loads``.
    """
    refusal = _find_refusal(response)
    if refusal is not None:
        raise LlmProviderError(
            f"OpenAI response was refused by the model: {refusal}"
        )

    status = getattr(response, "status", None)
    if status == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None) if details is not None else None
        raise LlmProviderError(
            f"OpenAI response was incomplete (reason={reason!r})"
        )

    if status is not None and status != "completed":
        raise LlmProviderError(
            f"OpenAI response did not complete successfully (status={status!r})"
        )

    text = (getattr(response, "output_text", None) or "").strip()
    if not text:
        raise LlmProviderError("OpenAI response contained no output text")
    return text


def _parse_json_response(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmProviderError(
            f"OpenAI response was not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise LlmProviderError(
            f"OpenAI response must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class OpenAIAuditProvider:
    """Production OpenAI provider for CertBound audit jobs.

    Uses the Responses API (``client.responses.create``) with native
    structured JSON output. Every request explicitly sets ``store=False``
    and never uses background mode, conversations, or
    ``previous_response_id`` -- each Pass A/B/C invocation is a fully
    independent, stateless request.
    """

    def __init__(
        self,
        config: OpenAIProviderConfig,
        *,
        client: Any = None,
        client_factory: Optional[Callable[[OpenAIProviderConfig], Any]] = None,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._client = client

    @classmethod
    def from_env(
        cls,
        *,
        client: Any = None,
        client_factory: Optional[Callable[[OpenAIProviderConfig], Any]] = None,
    ) -> "OpenAIAuditProvider":
        return cls(
            load_openai_config_from_env(),
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
                response = client.responses.create(**kwargs)
                text = _extract_text_or_raise(response)
                parsed = _parse_json_response(text)
                skip_legacy = bool(
                    metadata
                    and metadata.get(SKIP_LEGACY_LLM_AUDIT_VALIDATION_METADATA_KEY)
                )
                if not skip_legacy:
                    # Legacy llm_audit / hybrid_audit contract only.
                    validate_llm_response(parsed)
                input_tokens, output_tokens = _usage_tokens(response)
                request_id = getattr(response, "id", None)
                response_model = getattr(response, "model", None) or model

                return LlmResponse(
                    parsed_response=parsed,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    actual_cost_usd=_estimate_cost_usd(
                        input_tokens, output_tokens, self._config
                    ),
                    provider_request_id=str(request_id) if request_id else None,
                    model_name=str(response_model),
                    provider_name=PROVIDER_NAME,
                )
            except LlmAuditValidationError:
                # Schema-invalid output is permanent; never retry.
                raise
            except LlmProviderError:
                # Refusal / incomplete / malformed / empty output are all
                # permanent for this invocation; never retry.
                raise
            except Exception as exc:
                last_error = exc
                if _is_auth_error(exc):
                    diagnostics = describe_openai_error(exc, api_key=self._config.api_key)
                    raise LlmProviderError(
                        f"OpenAI authentication failed; check {ENV_API_KEY} "
                        f"({diagnostics})"
                    ) from exc

                if _is_transient_error(exc) and attempt < self._config.max_retries:
                    delay = _retry_delay_seconds(attempt)
                    logger.warning(
                        "OpenAI transient error on attempt %s/%s; "
                        "retrying in %.1fs: %s",
                        attempt + 1,
                        self._config.max_retries + 1,
                        delay,
                        type(exc).__name__,
                    )
                    time.sleep(delay)
                    continue

                diagnostics = describe_openai_error(exc, api_key=self._config.api_key)

                if _is_transient_error(exc):
                    raise LlmProviderError(
                        f"OpenAI request failed after "
                        f"{self._config.max_retries + 1} attempts: {diagnostics}"
                    ) from exc

                raise LlmProviderError(
                    f"OpenAI request failed: {diagnostics}"
                ) from exc

        last_diagnostics = (
            describe_openai_error(last_error, api_key=self._config.api_key)
            if last_error is not None
            else "no error captured"
        )
        raise LlmProviderError(
            f"OpenAI request failed after retries: {last_diagnostics}"
        )


def _default_client_factory(config: OpenAIProviderConfig) -> Any:
    try:
        import openai  # noqa: PLC0415
    except ImportError as exc:
        raise LlmProviderError(
            "openai package is not installed; run: pip install openai"
        ) from exc

    return openai.OpenAI(
        api_key=config.api_key,
        timeout=config.timeout,
        max_retries=0,  # bounded retries handled in OpenAIAuditProvider
    )


def build_openai_provider_from_env() -> OpenAIAuditProvider:
    """Convenience factory for worker wiring."""
    return OpenAIAuditProvider.from_env()
