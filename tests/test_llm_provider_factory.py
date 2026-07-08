"""
Tests for V45 Phase 2 worker LLM provider wiring.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.llm_provider_factory import (
    ENV_LLM_PROVIDER,
    UnknownLlmProviderError,
    build_llm_provider_from_env,
)
from workers.llm_providers import MissingProviderError


class TestBuildLlmProviderFromEnv(unittest.TestCase):

    def test_unset_provider_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop(ENV_LLM_PROVIDER, None)
            self.assertIsNone(build_llm_provider_from_env())

    def test_unknown_provider_rejected_at_startup(self):
        with patch.dict(os.environ, {ENV_LLM_PROVIDER: "azure-openai"}, clear=False):
            with self.assertRaises(UnknownLlmProviderError):
                build_llm_provider_from_env()

    def test_anthropic_provider_wired_when_configured(self):
        fake_provider = MagicMock()
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "anthropic",
                "CERTBOUND_ANTHROPIC_API_KEY": "secret-key",
            },
            clear=False,
        ):
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                return_value=fake_provider,
            ) as build_mock:
                provider = build_llm_provider_from_env()

        self.assertIs(provider, fake_provider)
        build_mock.assert_called_once()

    def test_anthropic_selected_without_api_key_raises(self):
        with patch.dict(os.environ, {ENV_LLM_PROVIDER: "anthropic"}, clear=False):
            os.environ.pop("CERTBOUND_ANTHROPIC_API_KEY", None)
            with self.assertRaises(MissingProviderError):
                build_llm_provider_from_env()

    def test_openai_provider_wired_when_configured(self):
        fake_provider = MagicMock()
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "openai",
                "CERTBOUND_OPENAI_API_KEY": "secret-key",
            },
            clear=False,
        ):
            with patch(
                "workers.openai_provider.build_openai_provider_from_env",
                return_value=fake_provider,
            ) as build_mock:
                provider = build_llm_provider_from_env()

        self.assertIs(provider, fake_provider)
        build_mock.assert_called_once()

    def test_openai_selected_without_api_key_raises(self):
        with patch.dict(os.environ, {ENV_LLM_PROVIDER: "openai"}, clear=False):
            os.environ.pop("CERTBOUND_OPENAI_API_KEY", None)
            with self.assertRaises(MissingProviderError):
                build_llm_provider_from_env()

    def test_anthropic_wiring_unchanged_after_openai_support_added(self):
        fake_provider = MagicMock()
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "anthropic",
                "CERTBOUND_ANTHROPIC_API_KEY": "secret-key",
            },
            clear=False,
        ):
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                return_value=fake_provider,
            ) as build_mock:
                provider = build_llm_provider_from_env()

        self.assertIs(provider, fake_provider)
        build_mock.assert_called_once()


class TestBackgroundWorkerProviderWiring(unittest.TestCase):

    def test_main_passes_provider_into_handler_registry(self):
        fake_client = MagicMock()
        fake_provider = MagicMock()
        fake_ai_quality = MagicMock()
        fake_registry = {"llm_audit": MagicMock()}

        with patch("workers.background_worker.build_supabase_client", return_value=fake_client):
            with patch(
                "workers.llm_provider_factory.build_llm_provider_from_env",
                return_value=fake_provider,
            ) as provider_mock:
                with patch(
                    "workers.ai_quality_provider_factory.build_ai_quality_providers_from_env",
                    return_value=fake_ai_quality,
                ) as ai_mock:
                    with patch(
                        "workers.job_handlers.build_handler_registry",
                        return_value=fake_registry,
                    ) as registry_mock:
                        with patch("workers.background_worker.BackgroundWorker") as worker_cls:
                            from workers.background_worker import main

                            main(["--worker-id", "test-worker", "--once"])

        provider_mock.assert_called_once()
        ai_mock.assert_called_once_with(required=True)
        registry_mock.assert_called_once_with(
            fake_client,
            llm_provider=fake_provider,
            ai_quality_providers=fake_ai_quality,
        )
        worker_cls.assert_called_once()
        self.assertIs(
            worker_cls.call_args.kwargs["handlers"],
            fake_registry,
        )


if __name__ == "__main__":
    unittest.main()
