"""
Tests for V48 AI quality provider factory and background worker startup wiring.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_provider_factory import (
    ENV_DISPUTE_PROVIDER,
    ENV_PRIMARY_PROVIDER,
    ENV_TIMEOUT_SECONDS,
    AiQualityProviderConfigError,
    ai_quality_providers_required,
    build_ai_quality_providers_from_env,
)
from workers.ai_quality_audit_worker import AiQualityAuditProviders
from workers.llm_provider_factory import ENV_LLM_PROVIDER


class TestAiQualityProvidersRequired(unittest.TestCase):

    def test_all_job_types_requires_ai_quality_config(self):
        self.assertTrue(ai_quality_providers_required(None))

    def test_explicit_ai_quality_job_type_requires_config(self):
        self.assertTrue(
            ai_quality_providers_required(["deterministic_audit", "ai_quality_audit_smoke"])
        )

    def test_excluding_ai_quality_does_not_require_config(self):
        self.assertFalse(ai_quality_providers_required(["deterministic_audit"]))


class TestBuildAiQualityProvidersFromEnv(unittest.TestCase):

    def test_not_required_returns_none_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(build_ai_quality_providers_from_env(required=False))

    def test_missing_primary_provider_fails_when_required(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop(ENV_PRIMARY_PROVIDER, None)
            os.environ.pop(ENV_LLM_PROVIDER, None)
            with self.assertRaises(AiQualityProviderConfigError) as ctx:
                build_ai_quality_providers_from_env(required=True)
        self.assertIn(ENV_PRIMARY_PROVIDER, str(ctx.exception))
        self.assertIn(ENV_LLM_PROVIDER, str(ctx.exception))
        self.assertNotIn("sk-ant-secret", str(ctx.exception))

    def test_primary_from_llm_provider_fallback(self):
        primary = MagicMock()
        dispute = MagicMock()
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "anthropic",
                "CERTBOUND_ANTHROPIC_API_KEY": "sk-ant-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                side_effect=[primary, dispute],
            ) as build_mock:
                providers = build_ai_quality_providers_from_env(required=True)

        self.assertIsInstance(providers, AiQualityAuditProviders)
        self.assertIs(providers.primary, primary)
        self.assertIs(providers.dispute, primary)
        build_mock.assert_called_once()

    def test_explicit_primary_provider_name(self):
        primary = MagicMock()
        with patch.dict(
            os.environ,
            {
                ENV_PRIMARY_PROVIDER: "anthropic",
                "CERTBOUND_ANTHROPIC_API_KEY": "sk-ant-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                return_value=primary,
            ):
                providers = build_ai_quality_providers_from_env(required=True)

        self.assertIs(providers.primary, primary)
        self.assertIs(providers.dispute, primary)

    def test_separate_dispute_provider_built_when_configured(self):
        primary = MagicMock(name="primary")
        dispute = MagicMock(name="dispute")
        with patch.dict(
            os.environ,
            {
                ENV_PRIMARY_PROVIDER: "anthropic",
                ENV_DISPUTE_PROVIDER: "anthropic",
                "CERTBOUND_ANTHROPIC_API_KEY": "sk-ant-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                side_effect=[primary, dispute],
            ) as build_mock:
                providers = build_ai_quality_providers_from_env(required=True)

        self.assertIs(providers.primary, primary)
        self.assertIs(providers.dispute, dispute)
        self.assertEqual(build_mock.call_count, 2)

    def test_timeout_from_ai_quality_env(self):
        primary = MagicMock()
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "anthropic",
                ENV_TIMEOUT_SECONDS: "45",
                "CERTBOUND_ANTHROPIC_API_KEY": "sk-ant-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                return_value=primary,
            ):
                providers = build_ai_quality_providers_from_env(required=True)

        self.assertEqual(providers.timeout_seconds, 45.0)

    def test_timeout_falls_back_to_anthropic_timeout_env(self):
        primary = MagicMock()
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "anthropic",
                "CERTBOUND_ANTHROPIC_TIMEOUT_SECONDS": "90",
                "CERTBOUND_ANTHROPIC_API_KEY": "sk-ant-secret",
            },
            clear=False,
        ):
            os.environ.pop(ENV_TIMEOUT_SECONDS, None)
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                return_value=primary,
            ):
                providers = build_ai_quality_providers_from_env(required=True)

        self.assertEqual(providers.timeout_seconds, 90.0)

    def test_malformed_timeout_fails_before_use(self):
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "anthropic",
                ENV_TIMEOUT_SECONDS: "not-a-number",
                "CERTBOUND_ANTHROPIC_API_KEY": "sk-ant-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                return_value=MagicMock(),
            ):
                with self.assertRaises(AiQualityProviderConfigError) as ctx:
                    build_ai_quality_providers_from_env(required=True)

        self.assertIn(ENV_TIMEOUT_SECONDS, str(ctx.exception))
        self.assertNotIn("sk-ant-secret", str(ctx.exception))

    def test_timeout_out_of_range_fails(self):
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "anthropic",
                ENV_TIMEOUT_SECONDS: "0",
                "CERTBOUND_ANTHROPIC_API_KEY": "sk-ant-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                return_value=MagicMock(),
            ):
                with self.assertRaises(AiQualityProviderConfigError):
                    build_ai_quality_providers_from_env(required=True)

    def test_openai_primary_from_llm_provider_fallback(self):
        primary = MagicMock()
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "openai",
                "CERTBOUND_OPENAI_API_KEY": "sk-openai-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.openai_provider.build_openai_provider_from_env",
                return_value=primary,
            ) as build_mock:
                providers = build_ai_quality_providers_from_env(required=True)

        self.assertIsInstance(providers, AiQualityAuditProviders)
        self.assertIs(providers.primary, primary)
        self.assertIs(providers.dispute, primary)
        build_mock.assert_called_once()

    def test_openai_primary_explicit_provider_name(self):
        primary = MagicMock()
        with patch.dict(
            os.environ,
            {
                ENV_PRIMARY_PROVIDER: "openai",
                "CERTBOUND_OPENAI_API_KEY": "sk-openai-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.openai_provider.build_openai_provider_from_env",
                return_value=primary,
            ):
                providers = build_ai_quality_providers_from_env(required=True)

        self.assertIs(providers.primary, primary)
        self.assertIs(providers.dispute, primary)

    def test_openai_primary_anthropic_dispute_mixed_configuration(self):
        primary = MagicMock(name="openai-primary")
        dispute = MagicMock(name="anthropic-dispute")
        with patch.dict(
            os.environ,
            {
                ENV_PRIMARY_PROVIDER: "openai",
                ENV_DISPUTE_PROVIDER: "anthropic",
                "CERTBOUND_OPENAI_API_KEY": "sk-openai-secret",
                "CERTBOUND_ANTHROPIC_API_KEY": "sk-ant-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.openai_provider.build_openai_provider_from_env",
                return_value=primary,
            ) as openai_mock:
                with patch(
                    "workers.anthropic_provider.build_anthropic_provider_from_env",
                    return_value=dispute,
                ) as anthropic_mock:
                    providers = build_ai_quality_providers_from_env(required=True)

        self.assertIs(providers.primary, primary)
        self.assertIs(providers.dispute, dispute)
        openai_mock.assert_called_once()
        anthropic_mock.assert_called_once()

    def test_anthropic_primary_openai_dispute_mixed_configuration(self):
        primary = MagicMock(name="anthropic-primary")
        dispute = MagicMock(name="openai-dispute")
        with patch.dict(
            os.environ,
            {
                ENV_PRIMARY_PROVIDER: "anthropic",
                ENV_DISPUTE_PROVIDER: "openai",
                "CERTBOUND_ANTHROPIC_API_KEY": "sk-ant-secret",
                "CERTBOUND_OPENAI_API_KEY": "sk-openai-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.anthropic_provider.build_anthropic_provider_from_env",
                return_value=primary,
            ) as anthropic_mock:
                with patch(
                    "workers.openai_provider.build_openai_provider_from_env",
                    return_value=dispute,
                ) as openai_mock:
                    providers = build_ai_quality_providers_from_env(required=True)

        self.assertIs(providers.primary, primary)
        self.assertIs(providers.dispute, dispute)
        anthropic_mock.assert_called_once()
        openai_mock.assert_called_once()

    def test_openai_primary_and_dispute_both_configured_builds_two_instances(self):
        primary = MagicMock(name="openai-primary")
        dispute = MagicMock(name="openai-dispute")
        with patch.dict(
            os.environ,
            {
                ENV_PRIMARY_PROVIDER: "openai",
                ENV_DISPUTE_PROVIDER: "openai",
                "CERTBOUND_OPENAI_API_KEY": "sk-openai-secret",
            },
            clear=False,
        ):
            with patch(
                "workers.openai_provider.build_openai_provider_from_env",
                side_effect=[primary, dispute],
            ) as build_mock:
                providers = build_ai_quality_providers_from_env(required=True)

        self.assertIs(providers.primary, primary)
        self.assertIs(providers.dispute, dispute)
        self.assertEqual(build_mock.call_count, 2)

    def test_openai_missing_api_key_surfaces_as_config_error(self):
        with patch.dict(
            os.environ,
            {ENV_LLM_PROVIDER: "openai"},
            clear=False,
        ):
            os.environ.pop("CERTBOUND_OPENAI_API_KEY", None)
            with self.assertRaises(AiQualityProviderConfigError) as ctx:
                build_ai_quality_providers_from_env(required=True)
        self.assertNotIn("sk-openai", str(ctx.exception))


class TestAiQualityModelProvenance(unittest.TestCase):

    def test_resolves_configured_anthropic_model(self):
        configured_model = "claude-provenance-test"
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "anthropic",
                "CERTBOUND_ANTHROPIC_MODEL": configured_model,
            },
            clear=False,
        ):
            from workers.ai_quality_provider_factory import (
                resolve_ai_quality_model_provenance_from_env,
            )

            provenance = resolve_ai_quality_model_provenance_from_env()

        self.assertEqual(provenance.primary_model_name, configured_model)
        self.assertEqual(provenance.dispute_model_name, configured_model)
        self.assertTrue(provenance.dispute_reuses_primary)

    def test_uses_anthropic_default_when_model_env_absent(self):
        with patch.dict(os.environ, {ENV_LLM_PROVIDER: "anthropic"}, clear=False):
            os.environ.pop("CERTBOUND_ANTHROPIC_MODEL", None)
            from workers.ai_quality_provider_factory import (
                resolve_ai_quality_model_provenance_from_env,
            )
            from workers.anthropic_provider import DEFAULT_MODEL

            provenance = resolve_ai_quality_model_provenance_from_env()

        self.assertEqual(provenance.primary_model_name, DEFAULT_MODEL)
        self.assertEqual(provenance.dispute_model_name, DEFAULT_MODEL)

    def test_dispute_fallback_records_primary_model(self):
        with patch.dict(
            os.environ,
            {
                ENV_PRIMARY_PROVIDER: "anthropic",
                "CERTBOUND_ANTHROPIC_MODEL": "claude-shared-model",
            },
            clear=False,
        ):
            os.environ.pop(ENV_DISPUTE_PROVIDER, None)
            from workers.ai_quality_provider_factory import (
                resolve_ai_quality_model_provenance_from_env,
            )

            provenance = resolve_ai_quality_model_provenance_from_env()

        self.assertEqual(provenance.primary_model_name, "claude-shared-model")
        self.assertEqual(provenance.dispute_model_name, "claude-shared-model")
        self.assertTrue(provenance.dispute_reuses_primary)

    def test_missing_provider_configuration_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop(ENV_PRIMARY_PROVIDER, None)
            os.environ.pop(ENV_LLM_PROVIDER, None)
            from workers.ai_quality_provider_factory import (
                resolve_ai_quality_model_provenance_from_env,
            )

            with self.assertRaises(AiQualityProviderConfigError):
                resolve_ai_quality_model_provenance_from_env()

    def test_resolves_configured_openai_model_without_api_key(self):
        configured_model = "gpt-provenance-test"
        with patch.dict(
            os.environ,
            {
                ENV_LLM_PROVIDER: "openai",
                "CERTBOUND_OPENAI_MODEL": configured_model,
            },
            clear=False,
        ):
            os.environ.pop("CERTBOUND_OPENAI_API_KEY", None)
            from workers.ai_quality_provider_factory import (
                resolve_ai_quality_model_provenance_from_env,
            )

            provenance = resolve_ai_quality_model_provenance_from_env()

        self.assertEqual(provenance.primary_model_name, configured_model)
        self.assertEqual(provenance.dispute_model_name, configured_model)
        self.assertTrue(provenance.dispute_reuses_primary)

    def test_uses_openai_default_when_model_env_absent(self):
        with patch.dict(os.environ, {ENV_LLM_PROVIDER: "openai"}, clear=False):
            os.environ.pop("CERTBOUND_OPENAI_MODEL", None)
            from workers.ai_quality_provider_factory import (
                resolve_ai_quality_model_provenance_from_env,
            )
            from workers.openai_provider import DEFAULT_MODEL

            provenance = resolve_ai_quality_model_provenance_from_env()

        self.assertEqual(provenance.primary_model_name, DEFAULT_MODEL)
        self.assertEqual(provenance.dispute_model_name, DEFAULT_MODEL)

    def test_mixed_provider_provenance_resolves_distinct_models(self):
        with patch.dict(
            os.environ,
            {
                ENV_PRIMARY_PROVIDER: "openai",
                ENV_DISPUTE_PROVIDER: "anthropic",
                "CERTBOUND_OPENAI_MODEL": "gpt-primary-model",
                "CERTBOUND_ANTHROPIC_MODEL": "claude-dispute-model",
            },
            clear=False,
        ):
            from workers.ai_quality_provider_factory import (
                resolve_ai_quality_model_provenance_from_env,
            )

            provenance = resolve_ai_quality_model_provenance_from_env()

        self.assertEqual(provenance.primary_model_name, "gpt-primary-model")
        self.assertEqual(provenance.dispute_model_name, "claude-dispute-model")
        self.assertEqual(provenance.primary_provider, "openai")
        self.assertEqual(provenance.dispute_provider, "anthropic")
        self.assertFalse(provenance.dispute_reuses_primary)


class TestBackgroundWorkerAiQualityWiring(unittest.TestCase):

    def test_main_injects_ai_quality_providers_when_all_job_types(self):
        fake_client = MagicMock()
        fake_llm = MagicMock()
        fake_ai = AiQualityAuditProviders(
            primary=MagicMock(),
            dispute=MagicMock(),
            timeout_seconds=30.0,
        )
        fake_registry = {"ai_quality_audit_smoke": MagicMock()}

        with patch("workers.background_worker.build_supabase_client", return_value=fake_client):
            with patch(
                "workers.llm_provider_factory.build_llm_provider_from_env",
                return_value=fake_llm,
            ):
                with patch(
                    "workers.ai_quality_provider_factory.build_ai_quality_providers_from_env",
                    return_value=fake_ai,
                ) as ai_mock:
                    with patch(
                        "workers.job_handlers.build_handler_registry",
                        return_value=fake_registry,
                    ) as registry_mock:
                        with patch("workers.background_worker.BackgroundWorker"):
                            from workers.background_worker import main

                            main(["--worker-id", "test-worker", "--once"])

        ai_mock.assert_called_once_with(required=True)
        registry_mock.assert_called_once_with(
            fake_client,
            llm_provider=fake_llm,
            ai_quality_providers=fake_ai,
        )

    def test_main_skips_ai_quality_config_when_job_type_excluded(self):
        fake_client = MagicMock()
        fake_registry = {"deterministic_audit": MagicMock()}

        with patch("workers.background_worker.build_supabase_client", return_value=fake_client):
            with patch(
                "workers.llm_provider_factory.build_llm_provider_from_env",
                return_value=None,
            ):
                with patch(
                    "workers.ai_quality_provider_factory.build_ai_quality_providers_from_env",
                    return_value=None,
                ) as ai_mock:
                    with patch(
                        "workers.job_handlers.build_handler_registry",
                        return_value=fake_registry,
                    ) as registry_mock:
                        with patch("workers.background_worker.BackgroundWorker") as worker_cls:
                            from workers.background_worker import main

                            main([
                                "--worker-id",
                                "test-worker",
                                "--job-types",
                                "deterministic_audit",
                                "--once",
                            ])

        ai_mock.assert_called_once_with(required=False)
        registry_mock.assert_called_once_with(
            fake_client,
            llm_provider=None,
            ai_quality_providers=None,
        )
        worker_cls.assert_called_once()

    def test_main_fails_before_worker_when_ai_quality_config_missing(self):
        with patch(
            "workers.ai_quality_provider_factory.build_ai_quality_providers_from_env",
            side_effect=AiQualityProviderConfigError(
                "CERTBOUND_LLM_PROVIDER is not set; cannot create Anthropic provider"
            ),
        ):
            with patch("workers.background_worker.build_supabase_client") as client_mock:
                with patch("workers.background_worker.BackgroundWorker") as worker_cls:
                    from workers.background_worker import main

                    with self.assertRaises(AiQualityProviderConfigError) as ctx:
                        main(["--worker-id", "test-worker", "--once"])

        client_mock.assert_not_called()
        worker_cls.assert_not_called()
        self.assertNotIn("sk-ant", str(ctx.exception))

    def test_existing_llm_provider_wiring_unchanged(self):
        fake_client = MagicMock()
        fake_provider = MagicMock()
        fake_registry = {"llm_audit": MagicMock()}

        with patch("workers.background_worker.build_supabase_client", return_value=fake_client):
            with patch(
                "workers.llm_provider_factory.build_llm_provider_from_env",
                return_value=fake_provider,
            ) as provider_mock:
                with patch(
                    "workers.ai_quality_provider_factory.build_ai_quality_providers_from_env",
                    return_value=MagicMock(),
                ):
                    with patch(
                        "workers.job_handlers.build_handler_registry",
                        return_value=fake_registry,
                    ) as registry_mock:
                        with patch("workers.background_worker.BackgroundWorker"):
                            from workers.background_worker import main

                            main(["--worker-id", "test-worker", "--once"])

        provider_mock.assert_called_once()
        self.assertIs(registry_mock.call_args.kwargs["llm_provider"], fake_provider)


if __name__ == "__main__":
    unittest.main()
