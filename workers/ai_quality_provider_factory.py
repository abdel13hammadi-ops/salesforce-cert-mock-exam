"""
AI quality audit provider factory for CertBound background workers (V48).

Builds ``AiQualityAuditProviders`` from environment configuration for injection
into ``build_handler_registry``. Handlers never instantiate providers directly.

Environment
-----------
CERTBOUND_AI_QUALITY_PRIMARY_LLM_PROVIDER
    Optional. When unset, falls back to ``CERTBOUND_LLM_PROVIDER``.
    Supported values: ``anthropic``, ``openai``.

CERTBOUND_AI_QUALITY_DISPUTE_LLM_PROVIDER
    Optional. When unset, the primary provider callable is reused for Pass C.

CERTBOUND_AI_QUALITY_TIMEOUT_SECONDS
    Worker-level provider timeout in seconds (1–3600). When unset, falls back to
    ``CERTBOUND_ANTHROPIC_TIMEOUT_SECONDS``, then 120.

CERTBOUND_LLM_PROVIDER / CERTBOUND_ANTHROPIC_*
    Reused for Anthropic primary (and dispute when configured as anthropic).

Secrets are never logged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

from workers.ai_quality_audit_worker import AiQualityAuditProviders
from workers.llm_provider_factory import ENV_LLM_PROVIDER, SUPPORTED_LLM_PROVIDERS
from workers.llm_providers import MissingProviderError

logger = logging.getLogger(__name__)

AI_QUALITY_JOB_TYPE = "ai_quality_audit_smoke"

ENV_PRIMARY_PROVIDER = "CERTBOUND_AI_QUALITY_PRIMARY_LLM_PROVIDER"
ENV_DISPUTE_PROVIDER = "CERTBOUND_AI_QUALITY_DISPUTE_LLM_PROVIDER"
ENV_TIMEOUT_SECONDS = "CERTBOUND_AI_QUALITY_TIMEOUT_SECONDS"
ENV_ANTHROPIC_TIMEOUT_SECONDS = "CERTBOUND_ANTHROPIC_TIMEOUT_SECONDS"

MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 3600.0
DEFAULT_TIMEOUT_SECONDS = 120.0


class AiQualityProviderConfigError(ValueError):
    """Raised when AI quality provider configuration is missing or invalid."""


@dataclass(frozen=True)
class AiQualityModelProvenance:
    """Resolved audit-run model identifiers aligned with provider configuration."""

    primary_model_name: str
    dispute_model_name: str
    primary_provider: str
    dispute_provider: str
    dispute_reuses_primary: bool


def ai_quality_providers_required(job_types: Optional[List[str]]) -> bool:
    """Return True when the worker may claim ``ai_quality_audit_smoke`` jobs."""
    if job_types is None:
        return True
    return AI_QUALITY_JOB_TYPE in job_types


def _resolve_provider_name(*env_names: str) -> str:
    for name in env_names:
        raw = os.environ.get(name, "").strip().lower()
        if raw:
            return raw
    return ""


def _parse_timeout_seconds() -> float:
    raw = os.environ.get(ENV_TIMEOUT_SECONDS, "").strip()
    if not raw:
        raw = os.environ.get(ENV_ANTHROPIC_TIMEOUT_SECONDS, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS

    try:
        value = float(raw)
    except ValueError as exc:
        raise AiQualityProviderConfigError(
            f"{ENV_TIMEOUT_SECONDS} must be a number"
        ) from exc

    if value < MIN_TIMEOUT_SECONDS or value > MAX_TIMEOUT_SECONDS:
        raise AiQualityProviderConfigError(
            f"{ENV_TIMEOUT_SECONDS} must be between "
            f"{MIN_TIMEOUT_SECONDS:g} and {MAX_TIMEOUT_SECONDS:g} seconds"
        )
    return value


def _build_provider_callable(provider_name: str):
    if provider_name not in SUPPORTED_LLM_PROVIDERS:
        raise AiQualityProviderConfigError(
            f"Unsupported AI quality LLM provider {provider_name!r}; "
            f"supported values: {sorted(SUPPORTED_LLM_PROVIDERS)}"
        )

    if provider_name == "anthropic":
        from workers.anthropic_provider import build_anthropic_provider_from_env  # noqa: PLC0415

        try:
            return build_anthropic_provider_from_env()
        except MissingProviderError as exc:
            raise AiQualityProviderConfigError(str(exc)) from exc

    if provider_name == "openai":
        from workers.openai_provider import build_openai_provider_from_env  # noqa: PLC0415

        try:
            return build_openai_provider_from_env()
        except MissingProviderError as exc:
            raise AiQualityProviderConfigError(str(exc)) from exc

    raise AiQualityProviderConfigError(
        f"Unsupported AI quality LLM provider {provider_name!r}; "
        f"supported values: {sorted(SUPPORTED_LLM_PROVIDERS)}"
    )


def _resolve_model_for_provider(provider_name: str) -> str:
    if provider_name == "anthropic":
        from workers.anthropic_provider import resolve_anthropic_model_from_env  # noqa: PLC0415

        return resolve_anthropic_model_from_env()

    if provider_name == "openai":
        from workers.openai_provider import resolve_openai_model_from_env  # noqa: PLC0415

        return resolve_openai_model_from_env()

    raise AiQualityProviderConfigError(
        f"Unsupported AI quality LLM provider {provider_name!r}; "
        f"supported values: {sorted(SUPPORTED_LLM_PROVIDERS)}"
    )


def resolve_ai_quality_model_provenance_from_env() -> AiQualityModelProvenance:
    """Resolve audit-run model names from the same configuration as providers."""
    primary_provider = _resolve_provider_name(ENV_PRIMARY_PROVIDER, ENV_LLM_PROVIDER)
    if not primary_provider:
        raise AiQualityProviderConfigError(
            f"{AI_QUALITY_JOB_TYPE} requires "
            f"{ENV_PRIMARY_PROVIDER} or {ENV_LLM_PROVIDER} "
            f"to be set to a supported provider (e.g. anthropic)"
        )

    primary_model_name = _resolve_model_for_provider(primary_provider)

    dispute_provider = _resolve_provider_name(ENV_DISPUTE_PROVIDER)
    if not dispute_provider:
        return AiQualityModelProvenance(
            primary_model_name=primary_model_name,
            dispute_model_name=primary_model_name,
            primary_provider=primary_provider,
            dispute_provider=primary_provider,
            dispute_reuses_primary=True,
        )

    dispute_model_name = _resolve_model_for_provider(dispute_provider)
    return AiQualityModelProvenance(
        primary_model_name=primary_model_name,
        dispute_model_name=dispute_model_name,
        primary_provider=primary_provider,
        dispute_provider=dispute_provider,
        dispute_reuses_primary=False,
    )


def build_ai_quality_providers_from_env(*, required: bool) -> Optional[AiQualityAuditProviders]:
    """Return configured providers, or ``None`` when not required.

    Raises
    ------
    AiQualityProviderConfigError
        When ``required`` is True and configuration is missing or invalid.
    """
    if not required:
        return None

    primary_name = _resolve_provider_name(ENV_PRIMARY_PROVIDER, ENV_LLM_PROVIDER)
    if not primary_name:
        raise AiQualityProviderConfigError(
            f"{AI_QUALITY_JOB_TYPE} requires "
            f"{ENV_PRIMARY_PROVIDER} or {ENV_LLM_PROVIDER} "
            f"to be set to a supported provider (e.g. anthropic)"
        )

    primary = _build_provider_callable(primary_name)

    dispute_name = _resolve_provider_name(ENV_DISPUTE_PROVIDER)
    if not dispute_name:
        dispute = primary
    else:
        dispute = _build_provider_callable(dispute_name)

    timeout_seconds = _parse_timeout_seconds()

    logger.info(
        "AI quality providers configured: primary=%s dispute=%s timeout_seconds=%s",
        primary_name,
        dispute_name or primary_name,
        timeout_seconds,
    )

    return AiQualityAuditProviders(
        primary=primary,
        dispute=dispute,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "AI_QUALITY_JOB_TYPE",
    "ENV_DISPUTE_PROVIDER",
    "ENV_PRIMARY_PROVIDER",
    "ENV_TIMEOUT_SECONDS",
    "AiQualityModelProvenance",
    "AiQualityProviderConfigError",
    "ai_quality_providers_required",
    "build_ai_quality_providers_from_env",
    "resolve_ai_quality_model_provenance_from_env",
]
