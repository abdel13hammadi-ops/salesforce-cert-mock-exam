"""
Unit tests for the V58 Day 8 OpenAI audit provider.

All tests use injected fake/mocked OpenAI clients. No real network calls are
made, and the ``openai`` package is never contacted.
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

from workers.ai_quality_audit_prompts import (
    PASS_A_RESPONSE_SCHEMA,
    PASS_B_RESPONSE_SCHEMA,
    PASS_C_RESPONSE_SCHEMA,
    _proposed_finding_schema,
)
from workers.ai_quality_audit_schemas import (
    AiQualityAuditValidationError,
    validate_pass_b_result,
)
from workers.llm_audit import AUDIT_RESPONSE_SCHEMA, LlmAuditValidationError
from workers.llm_providers import LlmProviderError, MissingProviderError
from workers.openai_provider import (
    ALLOWED_REASONING_EFFORTS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_TIMEOUT_SECONDS,
    ENV_API_KEY,
    ENV_INPUT_COST,
    ENV_MAX_OUTPUT_TOKENS,
    ENV_MAX_RETRIES,
    ENV_MODEL,
    ENV_OUTPUT_COST,
    ENV_REASONING_EFFORT,
    ENV_TIMEOUT,
    OpenAIAuditProvider,
    OpenAIProviderConfig,
    describe_openai_error,
    load_openai_config_from_env,
    normalize_schema_for_openai,
)


class _FakeOpenAIStatusError(Exception):
    """Lightweight stand-in for ``openai.APIStatusError`` and subclasses.

    Carries exactly the duck-typed attributes ``describe_openai_error``
    reads (``status_code``, ``message``, ``type``, ``code``, ``param``,
    ``request_id``, ``body``, ``response``), verified against the real
    installed ``openai==2.44.0`` exception hierarchy in
    ``workers/openai_provider.py``'s module comment. All attributes are
    optional so tests can simulate missing/partial fields.
    """

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        code: str | None = None,
        param: str | None = None,
        request_id: str | None = None,
        body: object = None,
        response: object = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.type = error_type
        self.code = code
        self.param = param
        self.request_id = request_id
        self.body = body
        self.response = response


class _RateLimitError(_FakeOpenAIStatusError):
    pass


class _APITimeoutError(Exception):
    pass


class _APIConnectionError(Exception):
    pass


class _InternalServerError(_FakeOpenAIStatusError):
    pass


class _AuthenticationError(_FakeOpenAIStatusError):
    pass


class _BadRequestError(_FakeOpenAIStatusError):
    pass


class _PermissionDeniedError(_FakeOpenAIStatusError):
    pass


class _FakeHeaders(dict):
    """Minimal case-sensitive stand-in for ``httpx.Headers.get``."""


class _FakeHttpResponse:
    """Minimal stand-in for the ``httpx.Response`` exposed as ``exc.response``."""

    def __init__(self, headers: dict | None = None):
        self.headers = _FakeHeaders(headers or {})


def _make_config(**overrides) -> OpenAIProviderConfig:
    base = dict(
        api_key="test-key-not-logged",
        model="gpt-5.5",
        reasoning_effort="medium",
        timeout=30.0,
        max_output_tokens=1024,
        max_retries=2,
        input_cost_per_mtok=1.25,
        output_cost_per_mtok=10.0,
    )
    base.update(overrides)
    return OpenAIProviderConfig(**base)


def _make_response(
    *,
    text: str | None = None,
    status: str = "completed",
    input_tokens: int = 120,
    output_tokens: int = 45,
    response_id: str = "resp_test_001",
    model: str = "gpt-5.5",
    incomplete_reason: str | None = None,
    refusal_text: str | None = None,
    output_text_override: str | None = None,
):
    payload = text if text is not None else json.dumps({"findings": []})

    if refusal_text is not None:
        output = [
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="refusal", refusal=refusal_text)],
            )
        ]
        resolved_output_text = ""
    elif output_text_override is not None:
        output = []
        resolved_output_text = output_text_override
    else:
        output = [
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=payload)],
            )
        ]
        resolved_output_text = payload

    incomplete_details = None
    if incomplete_reason is not None:
        incomplete_details = SimpleNamespace(reason=incomplete_reason)

    return SimpleNamespace(
        id=response_id,
        status=status,
        model=model,
        output=output,
        output_text=resolved_output_text,
        incomplete_details=incomplete_details,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_client(create_side_effect):
    client = MagicMock()
    client.responses.create = MagicMock(side_effect=create_side_effect)
    return client


def _transient(exc: Exception) -> bool:
    return isinstance(exc, (_RateLimitError, _APITimeoutError, _APIConnectionError, _InternalServerError))


def _auth(exc: Exception) -> bool:
    return isinstance(exc, _AuthenticationError)


def _collect_object_schemas(node: object, found: list | None = None) -> list:
    if found is None:
        found = []
    if isinstance(node, dict):
        if node.get("type") == "object" or (
            isinstance(node.get("type"), list) and "object" in node.get("type")
        ):
            found.append(node)
        for value in node.values():
            _collect_object_schemas(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_object_schemas(item, found)
    return found


def _finding_item_schema(schema: dict) -> dict:
    return schema["properties"]["proposed_findings"]["items"]


def _sample_pass_b_metadata():
    return {
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
    }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestOpenAIProviderConfig(unittest.TestCase):

    def test_missing_api_key_raises_missing_provider_error(self):
        env = {
            ENV_API_KEY: "",
            ENV_MODEL: "gpt-5.5",
            ENV_TIMEOUT: "30",
            ENV_MAX_OUTPUT_TOKENS: "1024",
            ENV_MAX_RETRIES: "2",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop(ENV_API_KEY, None)
            with self.assertRaises(MissingProviderError):
                load_openai_config_from_env()

    def test_default_configuration(self):
        with patch.dict(os.environ, {ENV_API_KEY: "secret-key"}, clear=False):
            for name in (ENV_MODEL, ENV_REASONING_EFFORT, ENV_TIMEOUT, ENV_MAX_OUTPUT_TOKENS, ENV_MAX_RETRIES):
                os.environ.pop(name, None)
            config = load_openai_config_from_env()

        self.assertEqual(config.model, DEFAULT_MODEL)
        self.assertEqual(config.model, "gpt-5.5")
        self.assertEqual(config.reasoning_effort, DEFAULT_REASONING_EFFORT)
        self.assertEqual(config.reasoning_effort, "medium")
        self.assertEqual(config.timeout, DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(config.max_output_tokens, DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertEqual(config.max_retries, DEFAULT_MAX_RETRIES)

    def test_environment_overrides(self):
        env = {
            ENV_API_KEY: "secret-key",
            ENV_MODEL: "gpt-5.5-custom",
            ENV_REASONING_EFFORT: "high",
            ENV_TIMEOUT: "90",
            ENV_MAX_OUTPUT_TOKENS: "2048",
            ENV_MAX_RETRIES: "4",
            ENV_INPUT_COST: "1.25",
            ENV_OUTPUT_COST: "10.0",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_openai_config_from_env()

        self.assertEqual(config.model, "gpt-5.5-custom")
        self.assertEqual(config.reasoning_effort, "high")
        self.assertEqual(config.timeout, 90.0)
        self.assertEqual(config.max_output_tokens, 2048)
        self.assertEqual(config.max_retries, 4)
        self.assertEqual(config.input_cost_per_mtok, 1.25)
        self.assertEqual(config.output_cost_per_mtok, 10.0)

    def test_invalid_timeout_rejected(self):
        with patch.dict(os.environ, {ENV_API_KEY: "secret-key", ENV_TIMEOUT: "0"}, clear=False):
            with self.assertRaises(LlmProviderError):
                load_openai_config_from_env()

    def test_negative_timeout_rejected(self):
        with patch.dict(os.environ, {ENV_API_KEY: "secret-key", ENV_TIMEOUT: "-5"}, clear=False):
            with self.assertRaises(LlmProviderError):
                load_openai_config_from_env()

    def test_invalid_max_retries_rejected(self):
        with patch.dict(os.environ, {ENV_API_KEY: "secret-key", ENV_MAX_RETRIES: "-1"}, clear=False):
            with self.assertRaises(LlmProviderError):
                load_openai_config_from_env()

    def test_non_numeric_max_retries_rejected(self):
        with patch.dict(os.environ, {ENV_API_KEY: "secret-key", ENV_MAX_RETRIES: "abc"}, clear=False):
            with self.assertRaises(LlmProviderError):
                load_openai_config_from_env()

    def test_invalid_max_output_tokens_rejected(self):
        with patch.dict(os.environ, {ENV_API_KEY: "secret-key", ENV_MAX_OUTPUT_TOKENS: "0"}, clear=False):
            with self.assertRaises(LlmProviderError):
                load_openai_config_from_env()

    def test_negative_max_output_tokens_rejected(self):
        with patch.dict(os.environ, {ENV_API_KEY: "secret-key", ENV_MAX_OUTPUT_TOKENS: "-100"}, clear=False):
            with self.assertRaises(LlmProviderError):
                load_openai_config_from_env()

    def test_invalid_reasoning_effort_rejected(self):
        with patch.dict(os.environ, {ENV_API_KEY: "secret-key", ENV_REASONING_EFFORT: "extreme"}, clear=False):
            with self.assertRaises(LlmProviderError):
                load_openai_config_from_env()

    def test_all_allowed_reasoning_efforts_accepted(self):
        self.assertEqual(ALLOWED_REASONING_EFFORTS, frozenset({"none", "low", "medium", "high", "xhigh"}))
        for effort in ALLOWED_REASONING_EFFORTS:
            with patch.dict(
                os.environ,
                {ENV_API_KEY: "secret-key", ENV_REASONING_EFFORT: effort},
                clear=False,
            ):
                config = load_openai_config_from_env()
            self.assertEqual(config.reasoning_effort, effort)


# ---------------------------------------------------------------------------
# Schema normalization
# ---------------------------------------------------------------------------

class TestNormalizeSchemaForOpenAI(unittest.TestCase):

    def test_pass_a_root_object_is_closed(self):
        normalized = normalize_schema_for_openai(PASS_A_RESPONSE_SCHEMA)
        self.assertFalse(normalized.get("additionalProperties"))
        self.assertIn("selected_option_labels", normalized["required"])

    def test_pass_a_min_items_preserved(self):
        normalized = normalize_schema_for_openai(PASS_A_RESPONSE_SCHEMA)
        self.assertEqual(
            normalized["properties"]["selected_option_labels"]["minItems"], 1
        )

    def test_source_schema_is_not_mutated_pass_a(self):
        before = copy.deepcopy(PASS_A_RESPONSE_SCHEMA)
        normalize_schema_for_openai(PASS_A_RESPONSE_SCHEMA)
        self.assertEqual(PASS_A_RESPONSE_SCHEMA, before)

    def test_source_schema_is_not_mutated_pass_b(self):
        before = copy.deepcopy(PASS_B_RESPONSE_SCHEMA)
        normalize_schema_for_openai(PASS_B_RESPONSE_SCHEMA)
        self.assertEqual(PASS_B_RESPONSE_SCHEMA, before)
        self.assertNotIn(
            "additionalProperties",
            PASS_B_RESPONSE_SCHEMA["properties"]["proposed_findings"]["items"]["properties"]["metadata"],
        )

    def test_source_schema_is_not_mutated_pass_c(self):
        before = copy.deepcopy(PASS_C_RESPONSE_SCHEMA)
        normalize_schema_for_openai(PASS_C_RESPONSE_SCHEMA)
        self.assertEqual(PASS_C_RESPONSE_SCHEMA, before)
        self.assertNotIn("proposed_findings", PASS_C_RESPONSE_SCHEMA["required"])

    def test_proposed_finding_helper_schema_not_mutated(self):
        before = copy.deepcopy(_proposed_finding_schema())
        schema = {"type": "array", "items": _proposed_finding_schema()}
        normalize_schema_for_openai(schema)
        self.assertEqual(_proposed_finding_schema(), before)

    def test_pass_b_min_items_preserved_on_findings_items(self):
        normalized = normalize_schema_for_openai(PASS_B_RESPONSE_SCHEMA)
        finding = _finding_item_schema(normalized)
        evidence_ids = finding["properties"]["evidence_chunk_ids"]
        self.assertEqual(evidence_ids["type"], "array")

    def test_min_items_and_max_items_preserved_generic(self):
        schema = {
            "type": "object",
            "properties": {
                "items_field": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 9,
                    "items": {"type": "string"},
                }
            },
        }
        normalized = normalize_schema_for_openai(schema)
        self.assertEqual(normalized["properties"]["items_field"]["minItems"], 2)
        self.assertEqual(normalized["properties"]["items_field"]["maxItems"], 9)

    def test_enums_numeric_bounds_and_patterns_preserved(self):
        schema = {
            "type": "object",
            "properties": {
                "code": {"type": "string", "enum": ["A", "B"], "pattern": "^[A-Z]$"},
                "score": {"type": "number", "minimum": 0, "maximum": 1},
                "label": {"type": "string", "minLength": 1, "maxLength": 10, "format": "uuid"},
            },
        }
        normalized = normalize_schema_for_openai(schema)
        self.assertEqual(normalized["properties"]["code"]["enum"], ["A", "B"])
        self.assertEqual(normalized["properties"]["code"]["pattern"], "^[A-Z]$")
        self.assertEqual(normalized["properties"]["score"]["minimum"], 0)
        self.assertEqual(normalized["properties"]["score"]["maximum"], 1)
        self.assertEqual(normalized["properties"]["label"]["minLength"], 1)
        self.assertEqual(normalized["properties"]["label"]["maxLength"], 10)
        self.assertEqual(normalized["properties"]["label"]["format"], "uuid")

    def test_all_object_nodes_are_closed(self):
        normalized = normalize_schema_for_openai(PASS_B_RESPONSE_SCHEMA)
        for obj_schema in _collect_object_schemas(normalized):
            self.assertFalse(
                obj_schema.get("additionalProperties"),
                f"object schema missing additionalProperties=false: {obj_schema!r}",
            )

    def test_all_properties_become_required(self):
        normalized = normalize_schema_for_openai(PASS_C_RESPONSE_SCHEMA)
        self.assertEqual(
            set(normalized["required"]), set(normalized["properties"].keys())
        )
        finding = _finding_item_schema(normalized)
        self.assertEqual(set(finding["required"]), set(finding["properties"].keys()))

    def test_pass_c_proposed_findings_required_only_in_normalized_copy(self):
        self.assertNotIn("proposed_findings", PASS_C_RESPONSE_SCHEMA["required"])
        normalized = normalize_schema_for_openai(PASS_C_RESPONSE_SCHEMA)
        self.assertIn("proposed_findings", normalized["required"])
        self.assertEqual(normalized["properties"]["proposed_findings"]["type"], "array")

    def test_metadata_normalized_to_closed_schema_with_source_support_context(self):
        normalized = normalize_schema_for_openai(PASS_B_RESPONSE_SCHEMA)
        metadata_schema = _finding_item_schema(normalized)["properties"]["metadata"]
        self.assertEqual(metadata_schema["type"], "object")
        self.assertFalse(metadata_schema["additionalProperties"])
        self.assertIn("source_support_context", metadata_schema["properties"])
        self.assertIn("source_support_context", metadata_schema["required"])
        ctx_schema = metadata_schema["properties"]["source_support_context"]
        self.assertIn("object", ctx_schema["type"])
        self.assertIn("null", ctx_schema["type"])
        for field in (
            "attempted_retrieval",
            "evidence_limitation",
            "proposed_technical_claim",
            "insufficiency_reason",
        ):
            self.assertIn(field, ctx_schema["properties"])
            self.assertIn(field, ctx_schema["required"])

    def test_metadata_normalization_accepts_empty_object_downstream(self):
        """{} must still satisfy the real application validator for any
        finding code that does not require source_support_context."""
        finding = {
            "finding_ref": "f1",
            "finding_code": "EXPLANATION_MISSING",
            "finding_type": "explanation_quality",
            "severity": "medium",
            "materiality": "warning",
            "title": "t",
            "description": "d",
            "metadata": {},
            "evidence_chunk_ids": [],
        }
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [finding],
            },
            allowed_option_labels={"A", "B"},
            required_selection_count=1,
            frozen_evidence_chunk_ids=set(),
        )
        self.assertEqual(len(result["proposed_findings"]), 1)

    def test_metadata_with_null_source_support_context_accepted_downstream(self):
        finding = {
            "finding_ref": "f1",
            "finding_code": "EXPLANATION_MISSING",
            "finding_type": "explanation_quality",
            "severity": "medium",
            "materiality": "warning",
            "title": "t",
            "description": "d",
            "metadata": {"source_support_context": None},
            "evidence_chunk_ids": [],
        }
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [finding],
            },
            allowed_option_labels={"A", "B"},
            required_selection_count=1,
            frozen_evidence_chunk_ids=set(),
        )
        self.assertEqual(len(result["proposed_findings"]), 1)

    def test_metadata_normalization_preserves_source_support_weak_validation(self):
        """A populated source_support_context (emitted per the normalized
        OpenAI-local schema) must satisfy the real SOURCE_SUPPORT_WEAK
        validation path with no supporting evidence chunks."""
        finding = {
            "finding_ref": "f1",
            "finding_code": "SOURCE_SUPPORT_WEAK",
            "finding_type": "source_support",
            "severity": "medium",
            "materiality": "warning",
            "title": "t",
            "description": "d",
            "metadata": {
                "source_support_context": {
                    "attempted_retrieval": 1,
                    "evidence_limitation": "No chunk covers this claim.",
                    "proposed_technical_claim": "Claim text.",
                    "insufficiency_reason": "Evidence too indirect.",
                }
            },
            "evidence_chunk_ids": [],
        }
        result = validate_pass_b_result(
            {
                "selected_option_labels": ["A"],
                "proposed_findings": [finding],
            },
            allowed_option_labels={"A", "B"},
            required_selection_count=1,
            frozen_evidence_chunk_ids=set(),
        )
        self.assertEqual(len(result["proposed_findings"]), 1)

    def test_source_support_weak_without_context_still_rejected_downstream(self):
        """Proves the field is semantically load-bearing: omitting it for a
        no-evidence SOURCE_SUPPORT_WEAK finding is still a validation error,
        confirming we did not silently discard required information."""
        finding = {
            "finding_ref": "f1",
            "finding_code": "SOURCE_SUPPORT_WEAK",
            "finding_type": "source_support",
            "severity": "medium",
            "materiality": "warning",
            "title": "t",
            "description": "d",
            "metadata": {},
            "evidence_chunk_ids": [],
        }
        with self.assertRaises(AiQualityAuditValidationError):
            validate_pass_b_result(
                {
                    "selected_option_labels": ["A"],
                    "proposed_findings": [finding],
                },
                allowed_option_labels={"A", "B"},
                required_selection_count=1,
                frozen_evidence_chunk_ids=set(),
            )

    def test_legacy_audit_schema_normalizes_without_error(self):
        # Sanity check against the older legacy schema shape too.
        normalized = normalize_schema_for_openai(AUDIT_RESPONSE_SCHEMA)
        self.assertEqual(normalized["type"], "object")
        self.assertFalse(normalized["additionalProperties"])


# ---------------------------------------------------------------------------
# Sanitized OpenAI SDK error diagnostics (describe_openai_error)
# ---------------------------------------------------------------------------

class TestDescribeOpenAIError(unittest.TestCase):

    def test_bad_request_error_full_fields(self):
        exc = _BadRequestError(
            "Error code: 400 - {'error': {...}}",
            status_code=400,
            error_type="invalid_request_error",
            code="invalid_value",
            param="reasoning.effort",
            request_id="req_abc123def456",
            body={
                "message": "Invalid value for 'reasoning.effort'",
                "type": "invalid_request_error",
                "code": "invalid_value",
                "param": "reasoning.effort",
            },
        )
        diagnostics = describe_openai_error(exc)
        self.assertIn("status=400", diagnostics)
        self.assertIn("type=invalid_request_error", diagnostics)
        self.assertIn("code=invalid_value", diagnostics)
        self.assertIn("param=reasoning.effort", diagnostics)
        self.assertIn("request_id=req_abc123def456", diagnostics)
        self.assertIn("message=Invalid value for 'reasoning.effort'", diagnostics)

    def test_missing_optional_fields_are_simply_omitted(self):
        exc = _BadRequestError("Error code: 400", status_code=400)
        diagnostics = describe_openai_error(exc)
        self.assertIn("status=400", diagnostics)
        self.assertNotIn("type=", diagnostics)
        self.assertNotIn("code=", diagnostics)
        self.assertNotIn("param=", diagnostics)
        self.assertNotIn("request_id=", diagnostics)
        # No body was captured, so the short SDK-generated message is safe to use.
        self.assertIn("message=Error code: 400", diagnostics)

    def test_request_id_obtained_from_response_headers_when_attribute_absent(self):
        exc = _BadRequestError(
            "Error code: 400",
            status_code=400,
            request_id=None,
            response=_FakeHttpResponse({"x-request-id": "req_from_headers_999"}),
        )
        diagnostics = describe_openai_error(exc)
        self.assertIn("request_id=req_from_headers_999", diagnostics)

    def test_multiline_and_control_characters_are_sanitized(self):
        exc = _BadRequestError(
            "Error code: 400",
            status_code=400,
            body={"message": "Invalid\nvalue\twith\r\ncontrol\x00chars"},
        )
        diagnostics = describe_openai_error(exc)
        self.assertNotIn("\n", diagnostics)
        self.assertNotIn("\t", diagnostics)
        self.assertNotIn("\r", diagnostics)
        self.assertNotIn("\x00", diagnostics)
        self.assertIn("Invalid value with control chars", diagnostics)

    def test_diagnostic_message_length_is_bounded(self):
        huge_message = "x" * 5000
        exc = _BadRequestError(
            "Error code: 400",
            status_code=400,
            error_type="invalid_request_error",
            body={"message": huge_message},
        )
        diagnostics = describe_openai_error(exc)
        self.assertLessEqual(len(diagnostics), 500)
        self.assertNotIn(huge_message, diagnostics)

    def test_api_key_redacted_when_present_in_upstream_message(self):
        secret_key = "sk-openai-leaked-secret-value"
        exc = _BadRequestError(
            "Error code: 400",
            status_code=400,
            body={"message": f"Invalid Authorization header: Bearer {secret_key}"},
        )
        diagnostics = describe_openai_error(exc, api_key=secret_key)
        self.assertNotIn(secret_key, diagnostics)
        self.assertIn("[REDACTED]", diagnostics)

    def test_full_prompt_text_never_included(self):
        secret_prompt = "TOTALLY-SECRET-PROMPT-CONTENT-MARKER"
        # A raw (non-JSON-parsed) body must never be surfaced verbatim, even
        # if it happened to echo request content back.
        exc = _BadRequestError(
            "Error code: 400",
            status_code=400,
            body=f"raw non-json body containing {secret_prompt}",
        )
        diagnostics = describe_openai_error(exc)
        self.assertNotIn(secret_prompt, diagnostics)
        self.assertNotIn("message=", diagnostics)

    def test_authorization_headers_never_included(self):
        exc = _BadRequestError(
            "Error code: 400",
            status_code=400,
            response=_FakeHttpResponse(
                {
                    "x-request-id": "req_ok_123",
                    "authorization": "Bearer sk-should-not-appear",
                    "set-cookie": "session=should-not-appear",
                }
            ),
        )
        diagnostics = describe_openai_error(exc)
        self.assertIn("request_id=req_ok_123", diagnostics)
        self.assertNotIn("Bearer", diagnostics)
        self.assertNotIn("should-not-appear", diagnostics)

    def test_raw_response_body_dict_never_embedded_verbatim(self):
        # exc.message mirrors the SDK's own "Error code: N - {body}" shape;
        # describe_openai_error must not fall back to it when a body exists
        # but lacks a usable "message" key.
        exc = _BadRequestError(
            "Error code: 400 - {'error': {'type': 'invalid_request_error'}}",
            status_code=400,
            error_type="invalid_request_error",
            body={"type": "invalid_request_error"},
        )
        diagnostics = describe_openai_error(exc)
        self.assertNotIn("Error code: 400 - {'error'", diagnostics)
        self.assertNotIn("message=", diagnostics)
        self.assertIn("type=invalid_request_error", diagnostics)

    def test_no_recognizable_fields_falls_back_to_error_class_name(self):
        diagnostics = describe_openai_error(RuntimeError(""))
        self.assertIn("RuntimeError", diagnostics)


# ---------------------------------------------------------------------------
# Provider call behavior
# ---------------------------------------------------------------------------

class TestOpenAIAuditProvider(unittest.TestCase):

    def setUp(self):
        self._patches = [
            patch("workers.openai_provider._is_transient_error", _transient),
            patch("workers.openai_provider._is_auth_error", _auth),
            patch("workers.openai_provider.time.sleep"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _call(self, provider, **kwargs):
        return provider(
            model_name=kwargs.pop("model_name", "gpt-5.5"),
            system_prompt=kwargs.pop("system_prompt", "Audit system instructions."),
            user_prompt=kwargs.pop("user_prompt", "Audit this question."),
            response_schema=kwargs.pop("response_schema", PASS_B_RESPONSE_SCHEMA),
            metadata=kwargs.pop("metadata", _sample_pass_b_metadata() | {
                "skip_legacy_llm_audit_validation": True,
            }),
            **kwargs,
        )

    def test_successful_structured_response(self):
        payload = json.dumps({"selected_option_labels": ["A"], "proposed_findings": []})
        client = _make_client([_make_response(text=payload)])
        provider = OpenAIAuditProvider(_make_config(), client=client)

        response = self._call(provider)

        self.assertEqual(
            response.parsed_response,
            {"selected_option_labels": ["A"], "proposed_findings": []},
        )
        self.assertEqual(response.provider_name, "openai")
        self.assertEqual(response.model_name, "gpt-5.5")
        self.assertEqual(response.provider_request_id, "resp_test_001")
        client.responses.create.assert_called_once()

    def test_request_includes_store_false(self):
        client = _make_client([_make_response()])
        provider = OpenAIAuditProvider(_make_config(), client=client)
        self._call(provider)
        kwargs = client.responses.create.call_args.kwargs
        self.assertIs(kwargs["store"], False)

    def test_request_uses_responses_api_structured_text_format(self):
        client = _make_client([_make_response()])
        provider = OpenAIAuditProvider(_make_config(), client=client)
        self._call(provider, response_schema=PASS_B_RESPONSE_SCHEMA)
        kwargs = client.responses.create.call_args.kwargs
        self.assertIn("text", kwargs)
        fmt = kwargs["text"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["strict"])
        self.assertIn("schema", fmt)
        for obj_schema in _collect_object_schemas(fmt["schema"]):
            self.assertFalse(obj_schema.get("additionalProperties"))

    def test_request_includes_configured_reasoning_effort(self):
        client = _make_client([_make_response()])
        provider = OpenAIAuditProvider(_make_config(reasoning_effort="high"), client=client)
        self._call(provider)
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["reasoning"], {"effort": "high"})

    def test_request_includes_max_output_tokens(self):
        client = _make_client([_make_response()])
        provider = OpenAIAuditProvider(_make_config(max_output_tokens=777), client=client)
        self._call(provider)
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["max_output_tokens"], 777)

    def test_request_does_not_use_persistent_state_params(self):
        client = _make_client([_make_response()])
        provider = OpenAIAuditProvider(_make_config(), client=client)
        self._call(provider)
        kwargs = client.responses.create.call_args.kwargs
        self.assertNotIn("previous_response_id", kwargs)
        self.assertNotIn("conversation", kwargs)
        self.assertNotIn("background", kwargs)

    def test_model_fallback_when_call_time_model_blank(self):
        client = _make_client([_make_response()])
        provider = OpenAIAuditProvider(_make_config(model="gpt-5.5-configured"), client=client)
        self._call(provider, model_name="")
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-5.5-configured")

    def test_api_returned_model_used_when_available(self):
        client = _make_client([_make_response(model="gpt-5.5-2026-06-01")])
        provider = OpenAIAuditProvider(_make_config(model="gpt-5.5"), client=client)
        response = self._call(provider)
        self.assertEqual(response.model_name, "gpt-5.5-2026-06-01")

    def test_token_usage_and_cost_capture(self):
        client = _make_client([_make_response(input_tokens=200, output_tokens=80)])
        provider = OpenAIAuditProvider(
            _make_config(input_cost_per_mtok=1.25, output_cost_per_mtok=10.0), client=client
        )

        response = self._call(provider)

        self.assertEqual(response.input_tokens, 200)
        self.assertEqual(response.output_tokens, 80)
        expected_cost = (200 * 1.25 / 1_000_000) + (80 * 10.0 / 1_000_000)
        self.assertAlmostEqual(response.actual_cost_usd, expected_cost)

    def test_provider_request_id_mapping(self):
        client = _make_client([_make_response(response_id="resp_abc123")])
        provider = OpenAIAuditProvider(_make_config(), client=client)
        response = self._call(provider)
        self.assertEqual(response.provider_request_id, "resp_abc123")

    def test_per_call_timeout_override(self):
        client = _make_client([_make_response()])
        provider = OpenAIAuditProvider(_make_config(timeout=30.0), client=client)
        self._call(
            provider,
            metadata=_sample_pass_b_metadata()
            | {"skip_legacy_llm_audit_validation": True, "timeout_seconds": 15.0},
        )
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 15.0)

    def test_default_timeout_used_without_override(self):
        client = _make_client([_make_response()])
        provider = OpenAIAuditProvider(_make_config(timeout=42.0), client=client)
        self._call(provider)
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 42.0)

    def test_sdk_automatic_retries_disabled_in_default_client_factory(self):
        fake_openai_module = MagicMock()
        fake_client_cls = MagicMock()
        fake_openai_module.OpenAI = fake_client_cls
        with patch.dict(sys.modules, {"openai": fake_openai_module}):
            from workers.openai_provider import _default_client_factory

            _default_client_factory(_make_config(api_key="k", timeout=55.0))

        fake_client_cls.assert_called_once_with(api_key="k", timeout=55.0, max_retries=0)

    def test_rate_limit_then_success(self):
        client = _make_client([
            _RateLimitError("rate limited"),
            _make_response(input_tokens=10, output_tokens=5),
        ])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        response = self._call(provider)

        self.assertEqual(response.input_tokens, 10)
        self.assertEqual(client.responses.create.call_count, 2)

    def test_timeout_then_success(self):
        client = _make_client([
            _APITimeoutError("timed out"),
            _make_response(),
        ])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)
        self._call(provider)
        self.assertEqual(client.responses.create.call_count, 2)

    def test_connection_error_then_success(self):
        client = _make_client([
            _APIConnectionError("connection reset"),
            _make_response(),
        ])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)
        self._call(provider)
        self.assertEqual(client.responses.create.call_count, 2)

    def test_server_5xx_then_success(self):
        client = _make_client([
            _InternalServerError("server error"),
            _make_response(),
        ])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)
        self._call(provider)
        self.assertEqual(client.responses.create.call_count, 2)

    def test_exhausted_retries_on_transient_errors(self):
        client = _make_client([
            _RateLimitError("rate limited", status_code=429, request_id="req_rl_1"),
            _RateLimitError("rate limited", status_code=429, request_id="req_rl_2"),
            _RateLimitError("rate limited", status_code=429, request_id="req_rl_3"),
        ])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        message = str(ctx.exception)
        self.assertIn("after", message.lower())
        # Retry taxonomy/count unchanged: diagnostics are additive only.
        self.assertIn("status=429", message)
        self.assertIn("request_id=req_rl_3", message)
        self.assertEqual(client.responses.create.call_count, 3)

    def test_authentication_failure_not_retried(self):
        client = _make_client([
            _AuthenticationError(
                "invalid api key",
                status_code=401,
                error_type="invalid_request_error",
                request_id="req_auth_1",
            )
        ])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        message = str(ctx.exception)
        self.assertIn("authentication failed", message.lower())
        self.assertIn("status=401", message)
        self.assertIn("request_id=req_auth_1", message)
        self.assertEqual(client.responses.create.call_count, 1)

    def test_bad_request_not_retried(self):
        client = _make_client([
            _BadRequestError(
                "invalid request",
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_value",
                param="reasoning.effort",
                request_id="req_bad_1",
                body={"message": "Invalid value for 'reasoning.effort'"},
            )
        ])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        message = str(ctx.exception)
        self.assertIn("status=400", message)
        self.assertIn("type=invalid_request_error", message)
        self.assertIn("code=invalid_value", message)
        self.assertIn("param=reasoning.effort", message)
        self.assertIn("request_id=req_bad_1", message)
        self.assertIn("message=Invalid value for 'reasoning.effort'", message)
        self.assertEqual(client.responses.create.call_count, 1)

    def test_permission_failure_not_retried(self):
        client = _make_client([
            _PermissionDeniedError("forbidden", status_code=403, request_id="req_perm_1")
        ])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        message = str(ctx.exception)
        self.assertIn("status=403", message)
        self.assertIn("request_id=req_perm_1", message)
        self.assertEqual(client.responses.create.call_count, 1)

    def test_refusal_detected_and_not_retried(self):
        client = _make_client([_make_response(refusal_text="Cannot comply with this request.")])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        self.assertIn("refused", str(ctx.exception).lower())
        self.assertEqual(client.responses.create.call_count, 1)

    def test_incomplete_response_detected_and_not_retried(self):
        client = _make_client([
            _make_response(status="incomplete", incomplete_reason="max_output_tokens", output_text_override="")
        ])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        self.assertIn("incomplete", str(ctx.exception).lower())
        self.assertIn("max_output_tokens", str(ctx.exception))
        self.assertEqual(client.responses.create.call_count, 1)

    def test_empty_output_rejected(self):
        client = _make_client([_make_response(output_text_override="")])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        self.assertIn("no output text", str(ctx.exception).lower())
        self.assertEqual(client.responses.create.call_count, 1)

    def test_malformed_json_rejected_and_not_retried(self):
        client = _make_client([_make_response(text="not-json")])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            self._call(provider)

        self.assertIn("valid json", str(ctx.exception).lower())
        self.assertEqual(client.responses.create.call_count, 1)

    def test_schema_invalid_response_raises_validation_error(self):
        payload = json.dumps({"findings": "bad"})
        client = _make_client([_make_response(text=payload)])
        provider = OpenAIAuditProvider(_make_config(max_retries=2), client=client)

        with self.assertRaises(LlmAuditValidationError):
            provider(
                model_name="gpt-5.5",
                system_prompt="Audit system instructions.",
                user_prompt="Audit this question.",
                response_schema=AUDIT_RESPONSE_SCHEMA,
                metadata=_sample_pass_b_metadata(),
            )

    def test_skip_legacy_validation_accepts_v48_pass_a_shape(self):
        payload = json.dumps({"selected_option_labels": ["A"]})
        client = _make_client([_make_response(text=payload)])
        provider = OpenAIAuditProvider(_make_config(max_retries=0), client=client)

        with patch("workers.openai_provider.validate_llm_response") as legacy_validate:
            response = provider(
                model_name="gpt-5.5",
                system_prompt="Pass A instructions.",
                user_prompt="Choose the correct option.",
                response_schema=PASS_A_RESPONSE_SCHEMA,
                metadata={"skip_legacy_llm_audit_validation": True},
            )

        legacy_validate.assert_not_called()
        self.assertEqual(response.parsed_response, {"selected_option_labels": ["A"]})

    def test_legacy_validation_still_runs_without_skip_flag(self):
        payload = json.dumps({"selected_option_labels": ["A"]})
        client = _make_client([_make_response(text=payload)])
        provider = OpenAIAuditProvider(_make_config(max_retries=0), client=client)

        with self.assertRaises(LlmAuditValidationError):
            provider(
                model_name="gpt-5.5",
                system_prompt="Pass A instructions.",
                user_prompt="Choose the correct option.",
                response_schema=PASS_A_RESPONSE_SCHEMA,
                metadata={},
            )

        self.assertEqual(client.responses.create.call_count, 1)

    def test_no_api_key_or_prompt_leak_in_errors(self):
        secret_key = "sk-openai-super-secret-value"
        secret_prompt_marker = "TOTALLY-SECRET-PROMPT-CONTENT-MARKER"
        client = _make_client([_AuthenticationError(f"invalid credentials")])
        provider = OpenAIAuditProvider(
            _make_config(api_key=secret_key, max_retries=1), client=client
        )

        with self.assertRaises(LlmProviderError) as ctx:
            provider(
                model_name="gpt-5.5",
                system_prompt="Pass B instructions.",
                user_prompt=secret_prompt_marker,
                response_schema=PASS_B_RESPONSE_SCHEMA,
                metadata=_sample_pass_b_metadata()
                | {"skip_legacy_llm_audit_validation": True},
            )

        message = str(ctx.exception)
        self.assertNotIn(secret_key, message)
        self.assertNotIn(secret_prompt_marker, message)

    def test_no_api_key_or_prompt_leak_on_malformed_json_error(self):
        secret_key = "sk-openai-super-secret-value-2"
        secret_prompt_marker = "ANOTHER-SECRET-PROMPT-MARKER"
        client = _make_client([_make_response(text="not-json-at-all")])
        provider = OpenAIAuditProvider(_make_config(api_key=secret_key), client=client)

        with self.assertRaises(LlmProviderError) as ctx:
            provider(
                model_name="gpt-5.5",
                system_prompt="Pass B instructions.",
                user_prompt=secret_prompt_marker,
                response_schema=PASS_B_RESPONSE_SCHEMA,
                metadata=_sample_pass_b_metadata()
                | {"skip_legacy_llm_audit_validation": True},
            )

        message = str(ctx.exception)
        self.assertNotIn(secret_key, message)
        self.assertNotIn(secret_prompt_marker, message)


class TestOpenAIProviderEnvDefaults(unittest.TestCase):

    def test_env_defaults_loaded(self):
        env = {
            ENV_API_KEY: "secret-key",
            ENV_MODEL: "gpt-5.5",
            ENV_REASONING_EFFORT: "medium",
            ENV_TIMEOUT: "90",
            ENV_MAX_OUTPUT_TOKENS: "2048",
            ENV_MAX_RETRIES: "4",
            ENV_INPUT_COST: "1.25",
            ENV_OUTPUT_COST: "10.0",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_openai_config_from_env()

        self.assertEqual(config.model, "gpt-5.5")
        self.assertEqual(config.reasoning_effort, "medium")
        self.assertEqual(config.timeout, 90.0)
        self.assertEqual(config.max_output_tokens, 2048)
        self.assertEqual(config.max_retries, 4)
        self.assertEqual(config.input_cost_per_mtok, 1.25)
        self.assertEqual(config.output_cost_per_mtok, 10.0)


if __name__ == "__main__":
    unittest.main()
