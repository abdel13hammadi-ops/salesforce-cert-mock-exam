"""
LLM provider factory for CertBound background workers (V45 Phase 2).

Constructs a provider instance from environment configuration for injection
into ``build_handler_registry``. Handlers never instantiate providers directly.

Environment
---------
CERTBOUND_LLM_PROVIDER
    When unset, returns ``None`` and AI audit handlers remain disabled
    (they raise ``MissingProviderError`` before any audit RPC).
    When set to ``anthropic``, constructs ``AnthropicAuditProvider`` using
    existing ``CERTBOUND_ANTHROPIC_*`` variables.

Unknown provider values raise ``UnknownLlmProviderError`` at startup.
Secrets are never logged.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from workers.llm_providers import MissingProviderError

logger = logging.getLogger(__name__)

ENV_LLM_PROVIDER = "CERTBOUND_LLM_PROVIDER"

SUPPORTED_LLM_PROVIDERS = frozenset({"anthropic"})


class UnknownLlmProviderError(ValueError):
    """Raised when CERTBOUND_LLM_PROVIDER names an unsupported provider."""


def build_llm_provider_from_env():
    """Return a configured LLM provider, or ``None`` when AI is disabled.

    Raises
    ------
    UnknownLlmProviderError
        When ``CERTBOUND_LLM_PROVIDER`` is set to an unsupported value.
    MissingProviderError
        When a supported provider is selected but required credentials are
        absent (e.g. missing ``CERTBOUND_ANTHROPIC_API_KEY``).
    """
    raw = os.environ.get(ENV_LLM_PROVIDER, "").strip().lower()
    if not raw:
        logger.info(
            "%s is unset; llm_audit and hybrid_audit handlers are disabled",
            ENV_LLM_PROVIDER,
        )
        return None

    if raw not in SUPPORTED_LLM_PROVIDERS:
        raise UnknownLlmProviderError(
            f"Unsupported {ENV_LLM_PROVIDER}={raw!r}; "
            f"supported values: {sorted(SUPPORTED_LLM_PROVIDERS)}"
        )

    if raw == "anthropic":
        from workers.anthropic_provider import build_anthropic_provider_from_env  # noqa: PLC0415

        provider = build_anthropic_provider_from_env()
        logger.info("LLM provider configured: %s", raw)
        return provider

    raise UnknownLlmProviderError(
        f"Unsupported {ENV_LLM_PROVIDER}={raw!r}; "
        f"supported values: {sorted(SUPPORTED_LLM_PROVIDERS)}"
    )
