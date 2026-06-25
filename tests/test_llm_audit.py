"""
Tests for Phase 8F (LLM provider abstraction + strict response schema) and
Phase 8G (llm_audit worker handler).

No real external LLM is called.  All provider calls use FakeLlmProvider,
which returns a configurable LlmResponse or raises a configurable exception.

Coverage
--------
validate_llm_response (unit):
  * not-a-dict response raises LlmAuditValidationError
  * unknown top-level key raises LlmAuditValidationError
  * findings not a list raises LlmAuditValidationError
  * valid empty findings list → empty list returned
  * valid findings with all optional fields → normalised list returned
  * invalid finding_type raises LlmAuditValidationError
  * invalid severity raises LlmAuditValidationError
  * invalid confidence (out of range) raises LlmAuditValidationError
  * invalid confidence (boolean) raises LlmAuditValidationError
  * invalid evidence_role raises LlmAuditValidationError
  * invalid relevance_score (out of range) raises LlmAuditValidationError
  * evidence with valid fields → normalised dict returned

make_llm_audit_handler (unit):
  * exactly one target required
  * both targets → HandlerPayloadError
  * missing provider raises MissingProviderError before any RPC call
  * missing required payload field raises HandlerPayloadError
  * valid zero-finding response → success
  * valid findings with evidence → complete_audit_run_v1 receives findings
  * provider exception after run creation → end_audit_run_v1 called, no complete
  * create-run failure → no end or complete called
  * token and cost values forwarded in result
  * no direct .table() calls

Worker integration:
  * successful llm_audit result reaches complete_background_job_v1
  * remaining handlers (question_generation, embedding_generation, other) not implemented
"""

from __future__ import annotations

import os
import sys
import unittest

# Make project root importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.llm_providers import (
    LlmResponse,
    LlmProviderError,
    MissingProviderError,
)
from workers.llm_audit import (
    LlmAuditValidationError,
    validate_llm_response,
    AUDIT_RESPONSE_SCHEMA,
    ALLOWED_FINDING_TYPES,
    ALLOWED_SEVERITIES,
    ALLOWED_EVIDENCE_ROLES,
)
from workers.job_handlers import (
    HandlerPayloadError,
    NotImplementedHandler,
    build_handler_registry,
    make_llm_audit_handler,
)
from workers.background_worker import BackgroundWorker


# ===========================================================================
# Shared test fixtures
# ===========================================================================

_AUDIT_RUN_ID  = "cccccccc-0000-0000-0000-000000000099"
_QUESTION_VSID = "dddddddd-0000-0000-0000-000000000001"
_CHUNK_ID      = "eeeeeeee-0000-0000-0000-000000000001"

_CREATE_RESPONSE  = [{"audit_run_id": _AUDIT_RUN_ID, "run_status": "pending"}]
_COMPLETE_RESPONSE = [
    {
        "audit_run_id":  _AUDIT_RUN_ID,
        "run_status":    "completed",
        "finding_count":  0,
        "evidence_count": 0,
    }
]
_END_RESPONSE = [
    {
        "audit_run_id": _AUDIT_RUN_ID,
        "run_status":   "failed",
        "completed_at": "2099-01-01T00:00:00+00:00",
    }
]

# A minimal valid LLM job payload.
VALID_LLM_PAYLOAD = {
    "target_question_version_id": _QUESTION_VSID,
    "created_by":                 "audit-worker@certbound.io",
    "model_name":                 "gpt-4o",
    "prompt_version":             "v1.0.0",
    "system_prompt":  "You are an expert Salesforce certification question auditor.",
    "user_prompt":    "Audit the following question for quality and correctness.",
}

# A valid LLM response with no findings.
ZERO_FINDINGS_RESPONSE = {"findings": []}

# A valid finding without evidence.
VALID_FINDING = {
    "finding_code": "AMBIGUOUS_WORDING",
    "finding_type": "ambiguity",
    "severity":     "medium",
    "title":        "Ambiguous question wording",
    "description":  "The question could be interpreted in multiple ways.",
}

# A valid finding with evidence.
VALID_FINDING_WITH_EVIDENCE = {
    **VALID_FINDING,
    "confidence":       0.85,
    "detector_name":    "gpt-4o-auditor",
    "detector_version": "v1.0.0",
    "metadata":         {"model_temperature": 0.2},
    "evidence": [
        {
            "resource_chunk_id": _CHUNK_ID,
            "evidence_role":     "supporting",
            "quote_text":        "Relevant excerpt from the resource.",
            "relevance_score":   0.9,
            "metadata":          {"page": 42},
        }
    ],
}


# ===========================================================================
# Fake infrastructure (self-contained in this file)
# ===========================================================================

class FakeLlmProvider:
    """Configurable fake LLM provider for testing.

    Does NOT make real network calls.  Returns a pre-configured LlmResponse
    or raises a pre-configured exception.
    """

    def __init__(
        self,
        *,
        parsed_response: dict | None = None,
        input_tokens: int = 1000,
        output_tokens: int = 200,
        actual_cost_usd: float | None = 0.003,
        provider_request_id: str | None = "fake-req-001",
        error: Exception | None = None,
    ) -> None:
        self._parsed_response    = parsed_response if parsed_response is not None else {}
        self._input_tokens       = input_tokens
        self._output_tokens      = output_tokens
        self._actual_cost_usd    = actual_cost_usd
        self._provider_request_id = provider_request_id
        self._error              = error
        self.calls: list         = []

    def __call__(self, *, model_name, system_prompt, user_prompt,
                 response_schema, metadata=None):
        self.calls.append({
            "model_name":    model_name,
            "system_prompt": system_prompt,
            "user_prompt":   user_prompt,
            "metadata":      metadata,
        })
        if self._error is not None:
            raise self._error
        return LlmResponse(
            parsed_response=self._parsed_response,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            actual_cost_usd=self._actual_cost_usd,
            provider_request_id=self._provider_request_id,
        )


class _FakeRpcResult:
    def __init__(self, data, error=None):
        self.data  = data
        self.error = error


class _FakeRpcBuilder:
    def __init__(self, data, error=None):
        self._data  = data
        self._error = error

    def execute(self):
        return _FakeRpcResult(self._data, self._error)


class FakeSupabase:
    """Minimal Supabase fake for LLM audit tests.

    Does NOT expose .table() — calling it raises AttributeError.
    """

    def __init__(self):
        self._responses: dict = {}
        self._errors: dict    = {}
        self.rpc_calls: list  = []

    def set_response(self, name: str, data: list) -> None:
        self._responses[name] = data
        self._errors.pop(name, None)

    def set_error(self, name: str, message: str) -> None:
        self._errors[name] = message
        self._responses.pop(name, None)

    def rpc(self, name: str, params: dict) -> _FakeRpcBuilder:
        self.rpc_calls.append({"name": name, "params": params})
        if name in self._errors:
            return _FakeRpcBuilder(data=[], error=self._errors[name])
        return _FakeRpcBuilder(data=self._responses.get(name, []))

    def calls_for(self, name: str) -> list:
        return [c for c in self.rpc_calls if c["name"] == name]

    @property
    def called_rpc_names(self) -> list:
        return [c["name"] for c in self.rpc_calls]

    # Alias used by BackgroundWorker integration tests.
    def set_error_response(self, name: str, message: str) -> None:
        self.set_error(name, message)


def _make_supabase(*, create_error: bool = False) -> FakeSupabase:
    """Return a FakeSupabase with standard audit run RPC responses."""
    fake = FakeSupabase()
    if create_error:
        fake.set_error("create_audit_run_v1", "target not found")
    else:
        fake.set_response("create_audit_run_v1",  _CREATE_RESPONSE)
    fake.set_response("complete_audit_run_v1", _COMPLETE_RESPONSE)
    fake.set_response("end_audit_run_v1",      _END_RESPONSE)
    return fake


# ===========================================================================
# Section 1 — validate_llm_response (unit)
# ===========================================================================

class TestValidateLlmResponse(unittest.TestCase):

    # ---- top-level structure ----

    def test_not_a_dict_raises(self):
        with self.assertRaises(LlmAuditValidationError):
            validate_llm_response([])

    def test_not_a_dict_list_raises(self):
        with self.assertRaises(LlmAuditValidationError):
            validate_llm_response("findings: []")

    def test_unknown_top_level_key_raises(self):
        with self.assertRaises(LlmAuditValidationError) as ctx:
            validate_llm_response({"findings": [], "extra": "bad"})
        self.assertIn("unknown top-level", str(ctx.exception))

    def test_findings_not_list_raises(self):
        with self.assertRaises(LlmAuditValidationError):
            validate_llm_response({"findings": "oops"})

    def test_findings_null_raises(self):
        with self.assertRaises(LlmAuditValidationError):
            validate_llm_response({"findings": None})

    def test_valid_empty_findings(self):
        result = validate_llm_response({"findings": []})
        self.assertEqual(result, [])

    # ---- valid finding structure ----

    def test_valid_finding_minimal(self):
        result = validate_llm_response({"findings": [VALID_FINDING]})
        self.assertEqual(len(result), 1)
        f = result[0]
        self.assertEqual(f["finding_code"], "AMBIGUOUS_WORDING")
        self.assertEqual(f["finding_type"], "ambiguity")
        self.assertEqual(f["severity"],     "medium")
        self.assertIsNone(f["confidence"])
        self.assertEqual(f["evidence"],     [])

    def test_valid_finding_with_all_optional_fields(self):
        result = validate_llm_response({"findings": [VALID_FINDING_WITH_EVIDENCE]})
        self.assertEqual(len(result), 1)
        f = result[0]
        self.assertAlmostEqual(f["confidence"], 0.85)
        self.assertEqual(len(f["evidence"]), 1)
        ev = f["evidence"][0]
        self.assertEqual(ev["resource_chunk_id"], _CHUNK_ID)
        self.assertEqual(ev["evidence_role"],     "supporting")
        self.assertAlmostEqual(ev["relevance_score"], 0.9)

    def test_confidence_zero_accepted(self):
        f = {**VALID_FINDING, "confidence": 0.0}
        result = validate_llm_response({"findings": [f]})
        self.assertEqual(result[0]["confidence"], 0.0)

    def test_confidence_one_accepted(self):
        f = {**VALID_FINDING, "confidence": 1.0}
        result = validate_llm_response({"findings": [f]})
        self.assertEqual(result[0]["confidence"], 1.0)

    # ---- invalid finding_type ----

    def test_invalid_finding_type_raises(self):
        f = {**VALID_FINDING, "finding_type": "made_up_type"}
        with self.assertRaises(LlmAuditValidationError) as ctx:
            validate_llm_response({"findings": [f]})
        self.assertIn("finding_type", str(ctx.exception))

    # ---- invalid severity ----

    def test_invalid_severity_raises(self):
        f = {**VALID_FINDING, "severity": "disaster"}
        with self.assertRaises(LlmAuditValidationError) as ctx:
            validate_llm_response({"findings": [f]})
        self.assertIn("severity", str(ctx.exception))

    # ---- invalid confidence ----

    def test_confidence_above_1_raises(self):
        f = {**VALID_FINDING, "confidence": 1.1}
        with self.assertRaises(LlmAuditValidationError) as ctx:
            validate_llm_response({"findings": [f]})
        self.assertIn("confidence", str(ctx.exception))

    def test_confidence_below_0_raises(self):
        f = {**VALID_FINDING, "confidence": -0.1}
        with self.assertRaises(LlmAuditValidationError):
            validate_llm_response({"findings": [f]})

    def test_confidence_boolean_raises(self):
        f = {**VALID_FINDING, "confidence": True}
        with self.assertRaises(LlmAuditValidationError) as ctx:
            validate_llm_response({"findings": [f]})
        self.assertIn("confidence", str(ctx.exception))

    def test_confidence_string_raises(self):
        f = {**VALID_FINDING, "confidence": "0.9"}
        with self.assertRaises(LlmAuditValidationError):
            validate_llm_response({"findings": [f]})

    # ---- invalid evidence_role ----

    def test_invalid_evidence_role_raises(self):
        finding = {
            **VALID_FINDING,
            "evidence": [
                {"resource_chunk_id": _CHUNK_ID, "evidence_role": "hearsay"}
            ],
        }
        with self.assertRaises(LlmAuditValidationError) as ctx:
            validate_llm_response({"findings": [finding]})
        self.assertIn("evidence_role", str(ctx.exception))

    # ---- invalid relevance_score ----

    def test_relevance_above_1_raises(self):
        finding = {
            **VALID_FINDING,
            "evidence": [
                {"resource_chunk_id": _CHUNK_ID,
                 "evidence_role": "supporting", "relevance_score": 2.0}
            ],
        }
        with self.assertRaises(LlmAuditValidationError) as ctx:
            validate_llm_response({"findings": [finding]})
        self.assertIn("relevance_score", str(ctx.exception))

    def test_relevance_boolean_raises(self):
        finding = {
            **VALID_FINDING,
            "evidence": [
                {"resource_chunk_id": _CHUNK_ID,
                 "evidence_role": "supporting", "relevance_score": True}
            ],
        }
        with self.assertRaises(LlmAuditValidationError):
            validate_llm_response({"findings": [finding]})

    # ---- missing required finding field ----

    def test_missing_finding_code_raises(self):
        f = {k: v for k, v in VALID_FINDING.items() if k != "finding_code"}
        with self.assertRaises(LlmAuditValidationError) as ctx:
            validate_llm_response({"findings": [f]})
        self.assertIn("finding_code", str(ctx.exception))

    def test_missing_resource_chunk_id_raises(self):
        finding = {
            **VALID_FINDING,
            "evidence": [{"evidence_role": "supporting"}],
        }
        with self.assertRaises(LlmAuditValidationError) as ctx:
            validate_llm_response({"findings": [finding]})
        self.assertIn("resource_chunk_id", str(ctx.exception))


# ===========================================================================
# Section 2 — make_llm_audit_handler (unit)
# ===========================================================================

class TestMakeLlmAuditHandler(unittest.TestCase):

    def _make_provider(self, parsed_response=None, error=None, **kwargs):
        return FakeLlmProvider(
            parsed_response=parsed_response or ZERO_FINDINGS_RESPONSE,
            error=error,
            **kwargs,
        )

    def _call(self, payload, *, client=None, provider=None):
        c = client or _make_supabase()
        p = provider if provider is not None else self._make_provider()
        handler = make_llm_audit_handler(c, llm_provider=p)
        return handler(
            job_id="j-llm-01", payload=payload, checkpoint={},
            attempt=1, heartbeat_fn=lambda: None,
        )

    # ---- target validation ----

    def test_no_target_raises_payload_error(self):
        payload = {k: v for k, v in VALID_LLM_PAYLOAD.items()
                   if k != "target_question_version_id"}
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    def test_both_targets_raises_payload_error(self):
        payload = {
            **VALID_LLM_PAYLOAD,
            "target_candidate_id": "ffffffff-0000-0000-0000-000000000001",
        }
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    # ---- missing provider guard ----

    def test_missing_provider_raises_before_rpc_calls(self):
        """llm_provider=None must raise MissingProviderError before any RPC."""
        fake = _make_supabase()
        handler = make_llm_audit_handler(fake, llm_provider=None)
        with self.assertRaises(MissingProviderError):
            handler(
                job_id="j", payload=VALID_LLM_PAYLOAD,
                checkpoint={}, attempt=1, heartbeat_fn=lambda: None,
            )
        # No RPCs must have been called.
        self.assertEqual(fake.rpc_calls, [],
                         "No RPC must be called when provider is missing")

    # ---- missing required payload fields ----

    def test_missing_created_by_raises_payload_error(self):
        payload = {k: v for k, v in VALID_LLM_PAYLOAD.items() if k != "created_by"}
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    def test_missing_model_name_raises_payload_error(self):
        payload = {k: v for k, v in VALID_LLM_PAYLOAD.items() if k != "model_name"}
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    def test_missing_prompt_version_raises_payload_error(self):
        payload = {k: v for k, v in VALID_LLM_PAYLOAD.items() if k != "prompt_version"}
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    def test_missing_system_prompt_raises_payload_error(self):
        payload = {k: v for k, v in VALID_LLM_PAYLOAD.items() if k != "system_prompt"}
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    def test_missing_user_prompt_raises_payload_error(self):
        payload = {k: v for k, v in VALID_LLM_PAYLOAD.items() if k != "user_prompt"}
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    # ---- valid zero-finding response ----

    def test_valid_zero_finding_response(self):
        result = self._call(VALID_LLM_PAYLOAD)
        self.assertIn("audit_run_id",   result)
        self.assertIn("run_status",     result)
        self.assertIn("finding_count",  result)
        self.assertIn("evidence_count", result)
        self.assertIn("input_tokens",   result)
        self.assertIn("output_tokens",  result)
        self.assertIn("actual_cost_usd", result)

    # ---- valid findings with evidence reach complete_audit_run_v1 ----

    def test_valid_findings_reach_complete_run_rpc(self):
        provider = self._make_provider(
            parsed_response={"findings": [VALID_FINDING_WITH_EVIDENCE]}
        )
        fake = _make_supabase()
        handler = make_llm_audit_handler(fake, llm_provider=provider)
        handler(
            job_id="j", payload=VALID_LLM_PAYLOAD,
            checkpoint={}, attempt=1, heartbeat_fn=lambda: None,
        )
        complete_calls = fake.calls_for("complete_audit_run_v1")
        self.assertEqual(len(complete_calls), 1)
        sent_findings = complete_calls[0]["params"]["p_findings"]
        self.assertEqual(len(sent_findings), 1)
        self.assertEqual(sent_findings[0]["finding_code"], "AMBIGUOUS_WORDING")

    # ---- token and cost values forwarded in result ----

    def test_token_and_cost_values_returned(self):
        provider = self._make_provider(
            parsed_response=ZERO_FINDINGS_RESPONSE,
            input_tokens=1234,
            output_tokens=567,
            actual_cost_usd=0.042,
            provider_request_id="req-xyz",
        )
        result = self._call(VALID_LLM_PAYLOAD, provider=provider)
        self.assertEqual(result["input_tokens"],    1234)
        self.assertEqual(result["output_tokens"],   567)
        self.assertAlmostEqual(result["actual_cost_usd"], 0.042)

    # ---- provider exception after run creation triggers end_audit_run_v1 ----

    def test_provider_exception_triggers_end_audit_run(self):
        provider = self._make_provider(
            error=LlmProviderError("rate limit exceeded")
        )
        fake = _make_supabase()
        handler = make_llm_audit_handler(fake, llm_provider=provider)
        with self.assertRaises(LlmProviderError):
            handler(
                job_id="j", payload=VALID_LLM_PAYLOAD,
                checkpoint={}, attempt=1, heartbeat_fn=lambda: None,
            )
        self.assertIn("create_audit_run_v1", fake.called_rpc_names)
        self.assertIn("end_audit_run_v1",    fake.called_rpc_names)
        self.assertNotIn("complete_audit_run_v1", fake.called_rpc_names)

        end_calls = fake.calls_for("end_audit_run_v1")
        self.assertEqual(end_calls[0]["params"]["p_final_status"], "failed")
        self.assertIn("rate limit", end_calls[0]["params"]["p_reason"])

    # ---- validation error in provider response triggers end_audit_run_v1 ----

    def test_invalid_provider_response_triggers_end_audit_run(self):
        provider = self._make_provider(
            parsed_response={"findings": [{"bad": "structure"}]}
        )
        fake = _make_supabase()
        handler = make_llm_audit_handler(fake, llm_provider=provider)
        with self.assertRaises(LlmAuditValidationError):
            handler(
                job_id="j", payload=VALID_LLM_PAYLOAD,
                checkpoint={}, attempt=1, heartbeat_fn=lambda: None,
            )
        self.assertIn("end_audit_run_v1",    fake.called_rpc_names)
        self.assertNotIn("complete_audit_run_v1", fake.called_rpc_names)

    # ---- create-run failure does not trigger end or complete ----

    def test_create_run_failure_no_end_or_complete(self):
        fake = _make_supabase(create_error=True)
        provider = self._make_provider()
        handler = make_llm_audit_handler(fake, llm_provider=provider)
        with self.assertRaises(RuntimeError):
            handler(
                job_id="j", payload=VALID_LLM_PAYLOAD,
                checkpoint={}, attempt=1, heartbeat_fn=lambda: None,
            )
        self.assertNotIn("end_audit_run_v1",      fake.called_rpc_names)
        self.assertNotIn("complete_audit_run_v1", fake.called_rpc_names)

    # ---- no direct .table() calls ----

    def test_no_direct_table_calls(self):
        self.assertFalse(hasattr(_make_supabase(), "table"),
                         "FakeSupabase must not expose .table()")
        # Just verifies no AttributeError is raised during a successful call.
        self._call(VALID_LLM_PAYLOAD)


# ===========================================================================
# Section 3 — Worker integration
# ===========================================================================

def _make_bg_job(job_type: str, payload: dict) -> dict:
    return {
        "job_id":           "job-llm-01",
        "job_type":         job_type,
        "payload":          payload,
        "checkpoint":       {},
        "attempt_count":    1,
        "job_status":       "running",
        "lease_expires_at": "2099-01-01T00:00:00+00:00",
    }


class TestWorkerLlmAuditIntegration(unittest.TestCase):
    """End-to-end: BackgroundWorker dispatches to llm_audit handler."""

    def _heartbeat_response(self, job_id: str) -> dict:
        return {
            "job_id":           job_id,
            "job_status":       "running",
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
            "heartbeat_at":     "2099-01-01T00:00:00+00:00",
        }

    def _make_worker(self, fake, provider):
        return BackgroundWorker(
            worker_id="integration-llm-worker",
            client=fake,
            handlers=build_handler_registry(fake, llm_provider=provider),
            sleep_interval=0.0,
        )

    # ---- successful result reaches complete_background_job_v1 ----

    def test_successful_result_reaches_complete_background_job(self):
        job      = _make_bg_job("llm_audit", VALID_LLM_PAYLOAD)
        provider = FakeLlmProvider(
            parsed_response=ZERO_FINDINGS_RESPONSE,
            input_tokens=800,
            output_tokens=150,
            actual_cost_usd=0.002,
        )
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1",     [job])
        fake.set_response("heartbeat_background_job_v1", [
            self._heartbeat_response(job["job_id"]),
        ])
        fake.set_response("create_audit_run_v1",  _CREATE_RESPONSE)
        fake.set_response("complete_audit_run_v1", _COMPLETE_RESPONSE)
        fake.set_response("complete_background_job_v1", [
            {"job_id": job["job_id"], "job_status": "completed",
             "completed_at": "2099-01-01T00:00:00+00:00"},
        ])

        worker = self._make_worker(fake, provider)
        worker.run_once()

        self.assertIn("complete_background_job_v1", fake.called_rpc_names)
        self.assertNotIn("fail_background_job_v1",  fake.called_rpc_names)

        complete_calls = fake.calls_for("complete_background_job_v1")
        result = complete_calls[0]["params"].get("p_result", {})
        self.assertIn("audit_run_id",   result)
        self.assertIn("input_tokens",   result)
        self.assertIn("output_tokens",  result)
        self.assertIn("actual_cost_usd", result)

    # ---- remaining handlers still not implemented ----

    def test_remaining_handlers_not_implemented(self):
        """question_generation, embedding_generation, other
        must still raise NotImplementedHandler."""
        fake     = FakeSupabase()
        provider = FakeLlmProvider(parsed_response=ZERO_FINDINGS_RESPONSE)
        registry = build_handler_registry(fake, llm_provider=provider)

        for job_type in ("question_generation",
                         "embedding_generation", "other"):
            with self.assertRaises(
                NotImplementedHandler,
                msg=f"{job_type} must still raise NotImplementedHandler",
            ):
                registry[job_type](
                    job_id="x", payload={}, checkpoint={},
                    attempt=1, heartbeat_fn=lambda: None,
                )

    # ---- no direct .table() calls ----

    def test_no_direct_table_calls_in_worker_path(self):
        job      = _make_bg_job("llm_audit", VALID_LLM_PAYLOAD)
        provider = FakeLlmProvider(parsed_response=ZERO_FINDINGS_RESPONSE)
        fake     = FakeSupabase()
        self.assertFalse(hasattr(fake, "table"))

        fake.set_response("claim_background_job_v1",     [job])
        fake.set_response("heartbeat_background_job_v1", [
            self._heartbeat_response(job["job_id"]),
        ])
        fake.set_response("create_audit_run_v1",  _CREATE_RESPONSE)
        fake.set_response("complete_audit_run_v1", _COMPLETE_RESPONSE)
        fake.set_response("complete_background_job_v1", [
            {"job_id": job["job_id"], "job_status": "completed",
             "completed_at": "2099-01-01T00:00:00+00:00"},
        ])
        worker = self._make_worker(fake, provider)
        worker.run_once()
        # If we reached here, no AttributeError for .table() was raised.


if __name__ == "__main__":
    unittest.main()
