"""
LLM provider abstraction for CertBound audit workers (Phase 8F).

Defines a stable structural interface (Protocol) so any callable matching
the signature is a valid provider.  Concrete SDK integrations (OpenAI,
Anthropic, etc.) are wired externally and injected via
``build_handler_registry(client, llm_provider=<instance>)``.

Classes
-------
  MissingProviderError — raised when no provider is injected
  LlmProviderError     — raised when the provider call itself fails
  LlmResponse          — structured result from any provider call
  LlmProvider          — Protocol (structural interface) for providers
  NoOpProvider         — sentinel that always raises MissingProviderError
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # Python < 3.8 (typing_extensions fallback)
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]


# ===========================================================================
# Exceptions
# ===========================================================================

class MissingProviderError(RuntimeError):
    """Raised when no LLM provider was injected but one is required.

    The handler raises this *before* any RPC calls so no audit run row
    is created in the database.
    """


class LlmProviderError(RuntimeError):
    """Raised when the provider call fails.

    Examples: network error, authentication failure, rate limit exceeded,
    provider-side timeout, or any other transient provider error that is
    not a response-validation issue.
    """


# When True in provider call metadata, AnthropicAuditProvider skips the legacy
# llm_audit response validator so dedicated pass validators can run upstream.
SKIP_LEGACY_LLM_AUDIT_VALIDATION_METADATA_KEY = "skip_legacy_llm_audit_validation"


# ===========================================================================
# Response dataclass
# ===========================================================================

@dataclass
class LlmResponse:
    """Structured result returned by any LLM provider.

    Attributes
    ----------
    parsed_response:
        Parsed JSON object (dict) from the model, before CertBound schema
        validation.  Must be a ``dict``; the ``validate_llm_response``
        function in ``llm_audit.py`` will verify its structure.
    input_tokens:
        Number of tokens consumed by the prompt.
    output_tokens:
        Number of tokens in the model's response.
    actual_cost_usd:
        Monetary cost of the call in USD, if the provider reports it.
    provider_request_id:
        Provider-side request identifier for tracing and debugging.
    model_name:
        Provider model identifier used for the call.
    provider_name:
        Provider identifier (e.g. ``anthropic``).
    """

    parsed_response: dict
    input_tokens: int
    output_tokens: int
    actual_cost_usd: Optional[float] = None
    provider_request_id: Optional[str] = None
    model_name: Optional[str] = None
    provider_name: Optional[str] = None


# ===========================================================================
# Provider protocol
# ===========================================================================

@runtime_checkable
class LlmProvider(Protocol):
    """Structural interface for LLM providers.

    Any callable object that matches this ``__call__`` signature is a valid
    provider.  Implementations do **not** need to subclass this Protocol;
    duck-typing is sufficient.

    Example implementations (not included here):
      * ``OpenAiProvider`` wrapping the ``openai`` SDK
      * ``AnthropicProvider`` wrapping the ``anthropic`` SDK
      * Any HTTP wrapper, local model, or mock
    """

    def __call__(
        self,
        *,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict,
        metadata: Optional[dict] = None,
    ) -> LlmResponse:
        """Invoke the LLM and return a structured response.

        Parameters
        ----------
        model_name:
            Provider-specific model identifier
            (e.g. ``'gpt-4o'``, ``'claude-3-5-sonnet-20241022'``).
        system_prompt:
            System-level instructions for the model.
        user_prompt:
            User-level content: the question, resource excerpt, and task
            description already formatted for the model.
        response_schema:
            JSON Schema object describing the expected JSON response.
            Providers that support structured output (function calling,
            ``response_format``, etc.) should pass this through.
        metadata:
            Optional extra data for provider-level tracing, parameter
            overrides (temperature, max_tokens), or A/B experiment tags.

        Returns
        -------
        LlmResponse
            Parsed JSON response and token / cost metadata.

        Raises
        ------
        LlmProviderError
            On any provider-side failure (network, auth, rate limit, etc.).
            Validation failures in the response are handled by
            ``validate_llm_response`` in ``llm_audit.py``.
        """
        ...


# ===========================================================================
# Sentinel no-op provider
# ===========================================================================

class NoOpProvider:
    """Sentinel provider installed when no real provider is configured.

    Always raises ``MissingProviderError`` so accidental invocations are
    immediately visible rather than silently dropping jobs.

    Installed by ``make_llm_audit_handler(client, llm_provider=None)``
    as an internal guard; the handler also checks for ``None`` before
    constructing the check closure, so ``NoOpProvider`` is a secondary
    safety net.
    """

    def __call__(
        self,
        *,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict,
        metadata: Optional[dict] = None,
    ) -> LlmResponse:
        raise MissingProviderError(
            "No LLM provider was configured. "
            "Pass llm_provider=<your_provider> to build_handler_registry()."
        )
