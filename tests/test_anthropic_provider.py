"""
Unit tests for V45 Phase 1 Anthropic audit provider.

All tests use injected mock clients. No real network calls are made.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.anthropic_provider import (
    ENV_API_KEY,
    ENV_INPUT_COST,
    ENV_MAX_OUTPUT_TOKENS,
    ENV_MAX_RETRIES,
    ENV_MODEL,
    ENV_OUTPUT_COST,
    ENV_TIMEOUT,
    AnthropicAuditProvider,
    AnthropicProviderConfig,
    load_anthropic_config_from_env,
    normalize_schema_for_anthropic,
)
from workers.llm_audit import AUDIT_RESPONSE_SCHEMA, LlmAuditValidationError
from workers.llm_providers import LlmProviderError, MissingProviderError


class _RateLimitError(Exception):
    pass


class _AuthenticationError(Exception):
    pass


class _TimeoutError(Exception):
    pass


def _make_config(**overrides) -> AnthropicProviderConfig:
    base = dict(
        api_key="test-key-not-logged",
        model="claude-sonnet-4-6",
        timeout=30.0,
        max_output_tokens=1024,
        max_retries=2,
        input_cost_per_mtok=3.0,
        output_cost_per_mtok=15.0,
    )
    base.update(overrides)
    return AnthropicProviderConfig(**base)


def _make_message(
    *,
    text: str | None = None,
    input_tokens: int = 120,
    output_tokens: int = 45,
    message_id: str = "msg_test_001",
):
    payload = text if text is not None else json.dumps({"findings": []})
    return SimpleNamespace(
        id=message_id,
        content=[SimpleNamespace(type="text", text=payload)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


def _make_client(create_side_effect):
    client = MagicMock()
    client.messages.create = MagicMock(side_effect=create_side_effect)
    return client


def _transient(exc: Exception) -> bool:
    return isinstance(exc, (_RateLimitError, _TimeoutError))


def _auth(exc: Exception) -> bool:
    return isinstance(exc, _AuthenticationError)


def _collect_object_schemas(node: object, found: list | None = None) -> list:
    if found is None:
        found = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            _collect_object_schemas(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_object_schemas(item, found)
    return found


def _finding_item_schema(schema: dict) -> dict:
    return schema["properties"]["findings"]["items"]


def _evidence_item_schema(schema: dict) -> dict:
    return _finding_item_schema(schema)["properties"]["evidence"]["items"]


class TestNormalizeSchemaForAnthropic(unittest.TestCase):

    def test_root_object_receives_additional_properties_false(self):
        normalized = normalize_schema_for_anthropic(AUDIT_RESPONSE_SCHEMA)
        self.assertFalse(normalized["additionalProperties"])

    def test_nested_finding_objects_receive_additional_properties_false(self):
        normalized = normalize_schema_for_anthropic(AUDIT_RESPONSE_SCHEMA)
        finding = _finding_item_schema(normalized)
        self.assertEqual(finding["type"], "object")
        self.assertFalse(finding["additionalProperties"])

    def test_nested_metadata_objects_receive_additional_properties_false(self):
        normalized = normalize_schema_for_anthropic(AUDIT_RESPONSE_SCHEMA)
        finding_meta = _finding_item_schema(normalized)["properties"]["metadata"]
        evidence_meta = _evidence_item_schema(normalized)["properties"]["metadata"]
        self.assertFalse(finding_meta["additionalProperties"])
        self.assertFalse(evidence_meta["additionalProperties"])

    def test_array_item_objects_receive_additional_properties_false(self):
        normalized = normalize_schema_for_anthropic(AUDIT_RESPONSE_SCHEMA)
        evidence = _evidence_item_schema(normalized)
        self.assertEqual(evidence["type"], "object")
        self.assertFalse(evidence["additionalProperties"])

    def test_defs_objects_receive_additional_properties_false(self):
        schema = {
            "type": "object",
            "$defs": {
                "Finding": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                }
            },
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Finding"},
                }
            },
        }
        normalized = normalize_schema_for_anthropic(schema)
        self.assertFalse(normalized["additionalProperties"])
        self.assertFalse(normalized["$defs"]["Finding"]["additionalProperties"])

    def test_source_schema_is_not_mutated(self):
        before = copy.deepcopy(AUDIT_RESPONSE_SCHEMA)
        normalize_schema_for_anthropic(AUDIT_RESPONSE_SCHEMA)
        self.assertEqual(AUDIT_RESPONSE_SCHEMA, before)
        self.assertNotIn(
            "additionalProperties",
            AUDIT_RESPONSE_SCHEMA["properties"]["findings"]["items"],
        )
        self.assertNotIn(
            "additionalProperties",
            AUDIT_RESPONSE_SCHEMA["properties"]["findings"]["items"]["properties"]["metadata"],
        )

    def test_non_object_nodes_are_not_modified(self):
        normalized = normalize_schema_for_anthropic(AUDIT_RESPONSE_SCHEMA)
        confidence = _finding_item_schema(normalized)["properties"]["confidence"]
        self.assertEqual(confidence["type"], "number")
        self.assertNotIn("additionalProperties", confidence)

    def test_all_object_nodes_in_normalized_schema_are_closed(self):
        normalized = normalize_schema_for_anthropic(AUDIT_RESPONSE_SCHEMA)
        for obj_schema in _collect_object_schemas(normalized):
            self.assertFalse(
                obj_schema.get("additionalProperties"),
                f"object schema missing additionalProperties=false: {obj_schema!r}",
            )


class TestAnthropicProviderConfig(unittest.TestCase):

    def test_api_key_absent_raises_missing_provider_error(self):
        env = {
            ENV_API_KEY: "",
            ENV_MODEL: "claude-sonnet-4-6",
            ENV_TIMEOUT: "30",
            ENV_MAX_OUTPUT_TOKENS: "1024",
            ENV_MAX_RETRIES: "2",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop(ENV_API_KEY, None)
            with self.assertRaises(MissingProviderError):
                load_anthropic_config_from_env()


class TestAnthropicAuditProvider(unittest.TestCase):

    def setUp(self):
        self._patches = [
            patch("workers.anthropic_provider._is_transient_error", _transient),
            patch("workers.anthropic_provider._is_auth_error", _auth),
            patch("workers.anthropic_provider.time.sleep"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _call(self, provider, **kwargs):
        return provider(
            model_name=kwargs.pop("model_name", "claude-sonnet-4-6"),
            system_prompt="Audit system instructions.",
            user_prompt="Audit this question.",
            response_schema=AUDIT_RESPONSE_SCHEMA,
            metadata={
                "question": {
                    "question_text": "Sample?",
                    "question_type": "single",
                    "select_count": 1,
                    "options": [],
                },
                "resource_snapshot": {
                    "chunks": [
                        {
                            "resource_chunk_id": "11111111-1111-1111-1111-111111111111",
                            "chunk_text": "Official excerpt.",
                        }
                    ]
                },
            },
            **kwargs,
        )

    def test_successful_structured_response(self):
        client = _make_client([_make_message()])
        provider = AnthropicAuditProvider(_make_config(), client=client)

        response = self._call(provider)

        self.assertEqual(response.parsed_response, {"findings": []})
        self.assertEqual(response.provider_name, "anthropic")
        self.assertEqual(response.model_name, "claude-sonnet-4-6")
        self.assertEqual(response.provider_request_id, "msg_test_001")
        client.messages.create.assert_called_once()
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-sonnet-4-6")
        self.assertIn("output_config", kwargs)
        sent_schema = kwargs["output_config"]["format"]["schema"]
        for obj_schema in _collect_object_schemas(sent_schema):
            self.assertFalse(obj_schema.get("additionalProperties"))
        self.assertIn("Question snapshot", kwargs["messages"][0]["content"])
        self.assertIn("Official evidence", kwargs["messages"][0]["content"])

    def test_token_usage_and_cost_capture(self):
        client = _make_client([
            _make_message(input_tokens=200, output_tokens=80),
        ])
        provider = AnthropicAuditProvider(_make_config(), client=client)

        response = self._call(provider)

        self.assertEqual(response.input_tokens, 200)
        self.assertEqual(response.output_tokens, 80)
        expected_cost = (200 * 3.0 / 1_000_000) + (80 * 15.0 / 1_000_000)
        self.assertAlmostEqual(response.actual_cost_usd, expected_cost)

    def test_timeout_is_not_retried_as_validation_failure(self):
        client = _make_client([_TimeoutError("timed out")])
        provider = AnthropicAuditProvider(
            _make_config(max_retries=0),
            client=client,
        )

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        self.assertIn("failed after", str(ctx.exception).lower())
        self.assertEqual(client.messages.create.call_count, 1)

    def test_rate_limit_then_success(self):
        client = _make_client([
            _RateLimitError("rate limited"),
            _make_message(input_tokens=10, output_tokens=5),
        ])
        provider = AnthropicAuditProvider(_make_config(max_retries=2), client=client)

        response = self._call(provider)

        self.assertEqual(response.input_tokens, 10)
        self.assertEqual(client.messages.create.call_count, 2)

    def test_exhausted_retries_on_transient_errors(self):
        client = _make_client([
            _RateLimitError("rate limited"),
            _RateLimitError("rate limited"),
            _RateLimitError("rate limited"),
        ])
        provider = AnthropicAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        self.assertIn("after", str(ctx.exception).lower())
        self.assertEqual(client.messages.create.call_count, 3)

    def test_authentication_failure_not_retried(self):
        client = _make_client([_AuthenticationError("invalid x-api-key")])
        provider = AnthropicAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        self.assertIn("authentication failed", str(ctx.exception).lower())
        self.assertEqual(client.messages.create.call_count, 1)

    def test_malformed_response_raises_provider_error(self):
        client = _make_client([_make_message(text="not-json")])
        provider = AnthropicAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        self.assertIn("valid json", str(ctx.exception).lower())
        self.assertEqual(client.messages.create.call_count, 1)

    def test_schema_invalid_response_raises_validation_error(self):
        client = _make_client([_make_message(text=json.dumps({"findings": "bad"}))])
        provider = AnthropicAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmAuditValidationError):
            self._call(provider)

        self.assertEqual(client.messages.create.call_count, 1)

    def test_empty_response_raises_provider_error(self):
        empty_message = SimpleNamespace(
            id="msg_empty",
            content=[],
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )
        client = _make_client([empty_message])
        provider = AnthropicAuditProvider(_make_config(max_retries=0), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        self.assertIn("no text content", str(ctx.exception).lower())


class TestAnthropicProviderEnvDefaults(unittest.TestCase):

    def test_env_defaults_loaded(self):
        env = {
            ENV_API_KEY: "secret-key",
            ENV_MODEL: "claude-sonnet-4-6",
            ENV_TIMEOUT: "90",
            ENV_MAX_OUTPUT_TOKENS: "2048",
            ENV_MAX_RETRIES: "4",
            ENV_INPUT_COST: "3.0",
            ENV_OUTPUT_COST: "15.0",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_anthropic_config_from_env()

        self.assertEqual(config.model, "claude-sonnet-4-6")
        self.assertEqual(config.timeout, 90.0)
        self.assertEqual(config.max_output_tokens, 2048)
        self.assertEqual(config.max_retries, 4)
        self.assertEqual(config.input_cost_per_mtok, 3.0)
        self.assertEqual(config.output_cost_per_mtok, 15.0)


if __name__ == "__main__":
    unittest.main()
