"""
Tests for V44 Phase 8H (hybrid audit handler) and Phase 8I (finding merge).

Coverage
--------
TestFindingMerge
  - empty inputs
  - deterministic-only findings preserved in order
  - LLM-only findings appended
  - no-overlap ordering: deterministic first, then LLM
  - exact duplicate (code + field_path + description) is merged into one
  - severity escalation: LLM higher severity wins
  - severity stays: deterministic higher severity wins
  - confidence: higher value selected; None handled
  - evidence deduplicated by (resource_chunk_id, evidence_role)
  - evidence with different roles: both kept
  - metadata deterministic provenance preserved on conflict
  - LLM detector name/version recorded in merged metadata
  - different field_path on same code: NOT deduplicated (two findings)
  - deterministic ordering stable across multiple findings
  - non-duplicate different codes: neither dropped

TestMakeHybridAuditHandler
  - no target → HandlerPayloadError
  - both targets → HandlerPayloadError
  - missing provider → MissingProviderError, zero RPC calls
  - missing required field → HandlerPayloadError
  - question not a dict → HandlerPayloadError
  - deterministic-only findings (LLM returns empty) reach complete_audit_run_v1
  - LLM-only findings (clean question) reach complete_audit_run_v1
  - merged findings from both sources reach complete_audit_run_v1
  - severity escalated in merged finding
  - provider failure triggers end_audit_run_v1 with failed status
  - LLM validation failure triggers end_audit_run_v1 with failed status
  - create_audit_run_v1 failure does not call end_ or complete_
  - token and cost values returned in handler result
  - no direct .table() calls

TestWorkerHybridAuditIntegration
  - successful result reaches complete_background_job_v1 with token/cost
  - remaining stubs still raise NotImplementedHandler
  - no direct .table() calls throughout worker execution
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.finding_merge import merge_findings, _pick_severity, _pick_confidence
from workers.job_handlers import (
    build_handler_registry,
    make_hybrid_audit_handler,
    HandlerPayloadError,
    NotImplementedHandler,
)
from workers.llm_providers import MissingProviderError, LlmResponse


# ===========================================================================
# Shared fixtures
# ===========================================================================

_AUDIT_RUN_ID   = "aaaaaaaa-0000-0000-0000-000000000001"
_TARGET_QV_ID   = "bbbbbbbb-0000-0000-0000-000000000001"
_TARGET_CAND_ID = "cccccccc-0000-0000-0000-000000000001"

_CLEAN_QUESTION = {
    "question_text": "What is the primary role of a Salesforce Administrator?",
    "explanation":   "A Salesforce Administrator configures the platform.",
    "question_type": "single",
    "select_count":  1,
    "options": [
        {"option_label": "A", "option_text": "Configure the platform",
         "is_correct": True,  "display_order": 1},
        {"option_label": "B", "option_text": "Write Java code",
         "is_correct": False, "display_order": 2},
    ],
}

_EMPTY_QUESTION_TEXT_QUESTION = {
    "question_text": "",          # triggers EMPTY_QUESTION_TEXT deterministic finding
    "explanation":   "A Salesforce Administrator configures the platform.",
    "question_type": "single",
    "select_count":  1,
    "options": [
        {"option_label": "A", "option_text": "Configure the platform",
         "is_correct": True,  "display_order": 1},
        {"option_label": "B", "option_text": "Write Java code",
         "is_correct": False, "display_order": 2},
    ],
}

_VALID_HYBRID_PAYLOAD: Dict[str, Any] = {
    "target_question_version_id": _TARGET_QV_ID,
    "created_by":      "audit-worker@certbound.io",
    "model_name":      "gpt-4o",
    "prompt_version":  "v1.0.0",
    "ruleset_version": "1.0.0",
    "system_prompt":   "You are an expert Salesforce question auditor.",
    "user_prompt":     "Audit this question.",
    "question":        _CLEAN_QUESTION,
}

_CREATE_AUDIT_RESPONSE = [{"audit_run_id": _AUDIT_RUN_ID, "run_status": "pending"}]
_COMPLETE_AUDIT_RESPONSE = [
    {"audit_run_id": _AUDIT_RUN_ID, "run_status": "completed",
     "finding_count": 0, "evidence_count": 0},
]
_END_AUDIT_RESPONSE = [
    {
        "audit_run_id": _AUDIT_RUN_ID,
        "run_status":   "failed",
        "completed_at": "2099-01-01T00:00:00+00:00",
    },
]


# ===========================================================================
# FakeLlmProvider
# ===========================================================================

@dataclass
class FakeLlmProvider:
    """Injected LLM provider for deterministic tests."""

    parsed_response: Any = field(default_factory=lambda: {"findings": []})
    input_tokens: int     = 100
    output_tokens: int    = 50
    actual_cost_usd: float = 0.005
    raise_error: Optional[Exception] = None

    def __call__(self, *, model_name, system_prompt, user_prompt,
                 response_schema, metadata=None) -> LlmResponse:
        if self.raise_error is not None:
            raise self.raise_error
        return LlmResponse(
            parsed_response=self.parsed_response,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            actual_cost_usd=self.actual_cost_usd,
        )


# ===========================================================================
# FakeSupabase (self-contained — no imports from other test files)
# ===========================================================================

class _FakeRpcResult:
    def __init__(self, data):
        self._data = data

    def execute(self):
        class _R:
            pass
        r = _R()
        r.data = self._data
        return r


class _FakeRpcBuilder:
    def __init__(self, fake, name):
        self._fake = fake
        self._name = name
        self._params = {}

    def __call__(self, name, params):
        self._name = name
        self._params = params
        return self

    def execute(self):
        self._fake._calls.append((self._name, self._params))
        if isinstance(self._fake._responses.get(self._name), Exception):
            raise self._fake._responses[self._name]
        data = self._fake._responses.get(self._name, [])
        if callable(data):
            data = data(self._params)
        return type("R", (), {"data": data})()


class FakeSupabase:
    """Minimal Supabase client fake for handler tests."""

    def __init__(self):
        self._responses: Dict[str, Any] = {}
        self._calls: List[tuple] = []

    def set_response(self, rpc_name: str, response):
        self._responses[rpc_name] = response

    def set_error_response(self, rpc_name: str, exc: Exception):
        self._responses[rpc_name] = exc

    def rpc(self, name: str, params: dict):
        builder = _FakeRpcBuilder(self, name)
        builder._name = name
        builder._params = params
        return builder

    def calls_for(self, rpc_name: str) -> List[dict]:
        return [p for n, p in self._calls if n == rpc_name]


# ===========================================================================
# Helpers
# ===========================================================================

def _det_finding(
    code: str,
    description: str = "some issue",
    severity: str = "low",
    field_path: Optional[str] = None,
    confidence: Optional[float] = None,
    evidence: Optional[list] = None,
    metadata: Optional[dict] = None,
) -> dict:
    return {
        "finding_code":     code,
        "finding_type":     "correctness",
        "severity":         severity,
        "title":            f"[DET] {code}",
        "description":      description,
        "field_path":       field_path,
        "confidence":       confidence,
        "evidence":         evidence or [],
        "metadata":         metadata if metadata is not None else {"ruleset_version": "1.0.0"},
        "detector_name":    "certbound-det",
        "detector_version": "1.0.0",
    }


def _llm_finding(
    code: str,
    description: str = "some issue",
    severity: str = "low",
    field_path: Optional[str] = None,
    confidence: Optional[float] = None,
    evidence: Optional[list] = None,
    metadata: Optional[dict] = None,
) -> dict:
    return {
        "finding_code":     code,
        "finding_type":     "correctness",
        "severity":         severity,
        "title":            f"[LLM] {code}",
        "description":      description,
        "field_path":       field_path,
        "confidence":       confidence,
        "evidence":         evidence or [],
        "metadata":         metadata if metadata is not None else {},
        "detector_name":    "gpt-4o-auditor",
        "detector_version": "v1.0.0",
    }


def _evidence(chunk_id: Optional[str], role: str) -> dict:
    return {"resource_chunk_id": chunk_id, "evidence_role": role,
            "excerpt": "test", "relevance_score": 0.9}


# ===========================================================================
# TestFindingMerge
# ===========================================================================

class TestFindingMerge(unittest.TestCase):

    def test_empty_both(self):
        self.assertEqual(merge_findings(None, None), [])
        self.assertEqual(merge_findings([], []), [])

    def test_deterministic_only(self):
        d1 = _det_finding("D001", "issue one")
        d2 = _det_finding("D002", "issue two")
        result = merge_findings([d1, d2], [])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["finding_code"], "D001")
        self.assertEqual(result[1]["finding_code"], "D002")

    def test_llm_only(self):
        l1 = _llm_finding("L001", "llm found an issue")
        result = merge_findings([], [l1])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["finding_code"], "L001")

    def test_no_overlap_ordering(self):
        d1 = _det_finding("D001", "det issue")
        d2 = _det_finding("D002", "det issue 2")
        l1 = _llm_finding("L001", "llm issue")
        result = merge_findings([d1, d2], [l1])
        self.assertEqual(len(result), 3)
        # Deterministic first, then LLM.
        self.assertEqual(result[0]["finding_code"], "D001")
        self.assertEqual(result[1]["finding_code"], "D002")
        self.assertEqual(result[2]["finding_code"], "L001")

    def test_exact_duplicate_merged_into_one(self):
        d = _det_finding("X001", "shared description", severity="low")
        l = _llm_finding("X001", "shared description", severity="low")
        result = merge_findings([d], [l])
        self.assertEqual(len(result), 1)

    def test_severity_escalation_llm_wins(self):
        d = _det_finding("X001", "shared desc", severity="low")
        l = _llm_finding("X001", "shared desc", severity="high")
        result = merge_findings([d], [l])
        self.assertEqual(result[0]["severity"], "high")

    def test_severity_deterministic_wins_when_higher(self):
        d = _det_finding("X001", "shared desc", severity="critical")
        l = _llm_finding("X001", "shared desc", severity="medium")
        result = merge_findings([d], [l])
        self.assertEqual(result[0]["severity"], "critical")

    def test_confidence_picks_higher(self):
        d = _det_finding("X001", "shared desc", confidence=0.5)
        l = _llm_finding("X001", "shared desc", confidence=0.9)
        result = merge_findings([d], [l])
        self.assertAlmostEqual(result[0]["confidence"], 0.9)

    def test_confidence_uses_llm_when_det_is_none(self):
        d = _det_finding("X001", "shared desc", confidence=None)
        l = _llm_finding("X001", "shared desc", confidence=0.75)
        result = merge_findings([d], [l])
        self.assertAlmostEqual(result[0]["confidence"], 0.75)

    def test_confidence_uses_det_when_llm_is_none(self):
        d = _det_finding("X001", "shared desc", confidence=0.6)
        l = _llm_finding("X001", "shared desc", confidence=None)
        result = merge_findings([d], [l])
        self.assertAlmostEqual(result[0]["confidence"], 0.6)

    def test_confidence_none_when_both_none(self):
        d = _det_finding("X001", "shared desc", confidence=None)
        l = _llm_finding("X001", "shared desc", confidence=None)
        result = merge_findings([d], [l])
        self.assertIsNone(result[0]["confidence"])

    def test_evidence_dedup_by_chunk_and_role(self):
        ev1 = _evidence("chunk-1", "supports")
        ev2 = _evidence("chunk-1", "supports")  # exact duplicate
        d = _det_finding("X001", "shared desc", evidence=[ev1])
        l = _llm_finding("X001", "shared desc", evidence=[ev2])
        result = merge_findings([d], [l])
        self.assertEqual(len(result[0]["evidence"]), 1)

    def test_evidence_different_roles_both_kept(self):
        ev_a = _evidence("chunk-1", "supports")
        ev_b = _evidence("chunk-1", "refutes")  # same chunk, different role
        d = _det_finding("X001", "shared desc", evidence=[ev_a])
        l = _llm_finding("X001", "shared desc", evidence=[ev_b])
        result = merge_findings([d], [l])
        self.assertEqual(len(result[0]["evidence"]), 2)

    def test_evidence_different_chunks_both_kept(self):
        ev_a = _evidence("chunk-1", "supports")
        ev_b = _evidence("chunk-2", "supports")
        d = _det_finding("X001", "shared desc", evidence=[ev_a])
        l = _llm_finding("X001", "shared desc", evidence=[ev_b])
        result = merge_findings([d], [l])
        self.assertEqual(len(result[0]["evidence"]), 2)

    def test_metadata_det_provenance_preserved(self):
        d = _det_finding("X001", "shared desc",
                         metadata={"ruleset_version": "2.0.0", "det_key": "det_val"})
        l = _llm_finding("X001", "shared desc",
                         metadata={"ruleset_version": "WRONG", "llm_key": "llm_val"})
        result = merge_findings([d], [l])
        meta = result[0]["metadata"]
        # Deterministic value wins on conflict.
        self.assertEqual(meta["ruleset_version"], "2.0.0")
        # LLM-only key is included.
        self.assertEqual(meta["llm_key"], "llm_val")
        # Deterministic-only key is included.
        self.assertEqual(meta["det_key"], "det_val")

    def test_llm_detector_recorded_in_metadata(self):
        d = _det_finding("X001", "shared desc")
        l = _llm_finding("X001", "shared desc")
        result = merge_findings([d], [l])
        meta = result[0]["metadata"]
        self.assertEqual(meta["llm_detector_name"],    "gpt-4o-auditor")
        self.assertEqual(meta["llm_detector_version"], "v1.0.0")

    def test_deterministic_identity_wins_on_merge(self):
        d = _det_finding("X001", "shared desc")
        l = _llm_finding("X001", "shared desc")
        result = merge_findings([d], [l])
        f = result[0]
        self.assertEqual(f["finding_code"],     "X001")
        self.assertEqual(f["title"],            "[DET] X001")
        self.assertEqual(f["detector_name"],    "certbound-det")
        self.assertEqual(f["detector_version"], "1.0.0")

    def test_different_field_path_not_deduped(self):
        d = _det_finding("X001", "same desc", field_path="field_a")
        l = _llm_finding("X001", "same desc", field_path="field_b")
        result = merge_findings([d], [l])
        # Key differs on field_path → two separate findings.
        self.assertEqual(len(result), 2)

    def test_different_description_not_deduped(self):
        d = _det_finding("X001", "description A")
        l = _llm_finding("X001", "description B")
        result = merge_findings([d], [l])
        self.assertEqual(len(result), 2)

    def test_stable_deterministic_ordering(self):
        findings = [_det_finding(f"D{i:03d}", f"issue {i}") for i in range(5)]
        result = merge_findings(findings, [])
        codes = [f["finding_code"] for f in result]
        self.assertEqual(codes, [f["finding_code"] for f in findings])

    def test_non_duplicate_different_codes_not_dropped(self):
        d = _det_finding("D001", "det issue")
        l = _llm_finding("L001", "llm issue")
        result = merge_findings([d], [l])
        codes = {f["finding_code"] for f in result}
        self.assertIn("D001", codes)
        self.assertIn("L001", codes)

    def test_normalisation_case_insensitive(self):
        """Findings differing only in case of description or code are deduped."""
        d = _det_finding("UPPER_CODE", "UPPER DESCRIPTION")
        l = _llm_finding("upper_code", "upper description")
        result = merge_findings([d], [l])
        self.assertEqual(len(result), 1)

    def test_normalisation_whitespace_stripped(self):
        d = _det_finding("X001", "  trimmed  ")
        l = _llm_finding("X001", "trimmed")
        result = merge_findings([d], [l])
        self.assertEqual(len(result), 1)


# ===========================================================================
# TestMakeHybridAuditHandler
# ===========================================================================

class TestMakeHybridAuditHandler(unittest.TestCase):

    def _make_fake(self):
        fake = FakeSupabase()
        fake.set_response("create_audit_run_v1",  _CREATE_AUDIT_RESPONSE)
        fake.set_response("complete_audit_run_v1", _COMPLETE_AUDIT_RESPONSE)
        fake.set_response("end_audit_run_v1",      _END_AUDIT_RESPONSE)
        return fake

    def _call(self, handler, payload=None):
        p = payload if payload is not None else dict(_VALID_HYBRID_PAYLOAD)
        return handler(
            job_id="job-1", payload=p, checkpoint={},
            attempt=1, heartbeat_fn=lambda: None,
        )

    # ------------------------------------------------------------------
    # Payload validation
    # ------------------------------------------------------------------

    def test_no_target_raises_payload_error(self):
        fake = self._make_fake()
        handler = make_hybrid_audit_handler(fake, llm_provider=FakeLlmProvider())
        payload = {k: v for k, v in _VALID_HYBRID_PAYLOAD.items()
                   if k != "target_question_version_id"}
        with self.assertRaises(HandlerPayloadError):
            self._call(handler, payload)

    def test_both_targets_raises_payload_error(self):
        fake = self._make_fake()
        handler = make_hybrid_audit_handler(fake, llm_provider=FakeLlmProvider())
        payload = {**_VALID_HYBRID_PAYLOAD, "target_candidate_id": _TARGET_CAND_ID}
        with self.assertRaises(HandlerPayloadError):
            self._call(handler, payload)

    def test_missing_provider_raises_before_rpc_calls(self):
        fake = self._make_fake()
        handler = make_hybrid_audit_handler(fake, llm_provider=None)
        with self.assertRaises(MissingProviderError):
            self._call(handler)
        self.assertEqual(fake.calls_for("create_audit_run_v1"), [])

    def test_missing_created_by_raises_payload_error(self):
        fake = self._make_fake()
        handler = make_hybrid_audit_handler(fake, llm_provider=FakeLlmProvider())
        payload = {k: v for k, v in _VALID_HYBRID_PAYLOAD.items()
                   if k != "created_by"}
        with self.assertRaises(HandlerPayloadError):
            self._call(handler, payload)

    def test_missing_model_name_raises_payload_error(self):
        fake = self._make_fake()
        handler = make_hybrid_audit_handler(fake, llm_provider=FakeLlmProvider())
        payload = {k: v for k, v in _VALID_HYBRID_PAYLOAD.items()
                   if k != "model_name"}
        with self.assertRaises(HandlerPayloadError):
            self._call(handler, payload)

    def test_missing_ruleset_version_raises_payload_error(self):
        fake = self._make_fake()
        handler = make_hybrid_audit_handler(fake, llm_provider=FakeLlmProvider())
        payload = {k: v for k, v in _VALID_HYBRID_PAYLOAD.items()
                   if k != "ruleset_version"}
        with self.assertRaises(HandlerPayloadError):
            self._call(handler, payload)

    def test_question_not_a_dict_raises_payload_error(self):
        fake = self._make_fake()
        handler = make_hybrid_audit_handler(fake, llm_provider=FakeLlmProvider())
        payload = {**_VALID_HYBRID_PAYLOAD, "question": "not a dict"}
        with self.assertRaises(HandlerPayloadError):
            self._call(handler, payload)

    def test_question_none_raises_payload_error(self):
        fake = self._make_fake()
        handler = make_hybrid_audit_handler(fake, llm_provider=FakeLlmProvider())
        payload = {**_VALID_HYBRID_PAYLOAD, "question": None}
        with self.assertRaises(HandlerPayloadError):
            self._call(handler, payload)

    # ------------------------------------------------------------------
    # Successful finding flows
    # ------------------------------------------------------------------

    def test_deterministic_only_findings_reach_complete(self):
        """A bad question triggers det findings; LLM returns nothing.
        All det findings must be sent to complete_audit_run_v1."""
        fake = FakeSupabase()
        fake.set_response("create_audit_run_v1",  _CREATE_AUDIT_RESPONSE)
        fake.set_response("complete_audit_run_v1", [
            {"audit_run_id": _AUDIT_RUN_ID, "run_status": "completed",
             "finding_count": 1, "evidence_count": 0},
        ])
        # LLM returns zero findings.
        provider = FakeLlmProvider(parsed_response={"findings": []})
        handler = make_hybrid_audit_handler(fake, llm_provider=provider)
        payload = {**_VALID_HYBRID_PAYLOAD, "question": _EMPTY_QUESTION_TEXT_QUESTION}

        result = self._call(handler, payload)

        self.assertEqual(result["run_status"], "completed")
        complete_calls = fake.calls_for("complete_audit_run_v1")
        self.assertEqual(len(complete_calls), 1)
        findings_sent = complete_calls[0].get("p_findings", [])
        # Must include the EMPTY_QUESTION_TEXT finding from the det engine.
        codes = {f["finding_code"] for f in findings_sent}
        self.assertIn("EMPTY_QUESTION_TEXT", codes)

    def test_llm_only_findings_clean_question(self):
        """Clean question produces no det findings; LLM findings are forwarded."""
        fake = FakeSupabase()
        fake.set_response("create_audit_run_v1",  _CREATE_AUDIT_RESPONSE)
        fake.set_response("complete_audit_run_v1", [
            {"audit_run_id": _AUDIT_RUN_ID, "run_status": "completed",
             "finding_count": 1, "evidence_count": 0},
        ])
        llm_finding = {
            "finding_code": "AMBIGUOUS_OPTION",
            "finding_type": "ambiguity",
            "severity":     "medium",
            "confidence":   0.8,
            "title":        "Ambiguous option wording",
            "description":  "Option A is ambiguous.",
            "field_path":   "options[0].option_text",
            "evidence":     [],
            "metadata":     {},
        }
        provider = FakeLlmProvider(
            parsed_response={"findings": [llm_finding]}
        )
        handler = make_hybrid_audit_handler(fake, llm_provider=provider)

        result = self._call(handler)

        self.assertEqual(result["run_status"], "completed")
        complete_calls = fake.calls_for("complete_audit_run_v1")
        findings_sent = complete_calls[0].get("p_findings", [])
        codes = {f["finding_code"] for f in findings_sent}
        self.assertIn("AMBIGUOUS_OPTION", codes)

    def test_merged_findings_from_both_sources(self):
        """Bad question (det finding) + LLM finding with different code → 2 findings."""
        fake = FakeSupabase()
        fake.set_response("create_audit_run_v1",  _CREATE_AUDIT_RESPONSE)
        fake.set_response("complete_audit_run_v1", [
            {"audit_run_id": _AUDIT_RUN_ID, "run_status": "completed",
             "finding_count": 2, "evidence_count": 0},
        ])
        llm_finding = {
            "finding_code": "LLM_FINDING",
            "finding_type": "correctness",
            "severity":     "low",
            "confidence":   0.7,
            "title":        "LLM finding",
            "description":  "Something the LLM found.",
            "field_path":   None,
            "evidence":     [],
            "metadata":     {},
        }
        provider = FakeLlmProvider(parsed_response={"findings": [llm_finding]})
        handler = make_hybrid_audit_handler(fake, llm_provider=provider)
        payload = {**_VALID_HYBRID_PAYLOAD, "question": _EMPTY_QUESTION_TEXT_QUESTION}

        self._call(handler, payload)

        complete_calls = fake.calls_for("complete_audit_run_v1")
        findings_sent = complete_calls[0].get("p_findings", [])
        codes = {f["finding_code"] for f in findings_sent}
        self.assertIn("EMPTY_QUESTION_TEXT", codes)
        self.assertIn("LLM_FINDING", codes)

    def test_severity_escalated_in_merged_finding(self):
        """LLM returns a higher-severity version of the same finding → escalated."""
        fake = FakeSupabase()
        fake.set_response("create_audit_run_v1",  _CREATE_AUDIT_RESPONSE)
        fake.set_response("complete_audit_run_v1", [
            {"audit_run_id": _AUDIT_RUN_ID, "run_status": "completed",
             "finding_count": 1, "evidence_count": 0},
        ])
        # Deterministic engine produces EMPTY_QUESTION_TEXT at "low" severity.
        # (The actual severity from deterministic_audit may vary; we exercise merge logic.)
        # LLM reports the same finding with "critical" severity.
        det_finding = _det_finding(
            "EMPTY_QUESTION_TEXT",
            "question_text must not be empty or whitespace-only",
            severity="low",
            field_path="question_text",
        )
        llm_finding_raw = {
            "finding_code": "EMPTY_QUESTION_TEXT",
            "finding_type": "correctness",
            "severity":     "critical",
            "confidence":   0.99,
            "title":        "Empty question text — LLM",
            "description":  "question_text must not be empty or whitespace-only",
            "field_path":   "question_text",
            "evidence":     [],
            "metadata":     {},
        }
        # We test merge_findings directly to confirm severity escalation.
        merged = merge_findings([det_finding], [llm_finding_raw])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["severity"], "critical")
        self.assertAlmostEqual(merged[0]["confidence"], 0.99)

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def test_provider_failure_triggers_end_audit_run(self):
        """LLM provider raising ends the audit run with status failed."""
        fake = self._make_fake()
        provider = FakeLlmProvider(raise_error=RuntimeError("LLM timeout"))
        handler = make_hybrid_audit_handler(fake, llm_provider=provider)

        with self.assertRaises(RuntimeError):
            self._call(handler)

        self.assertEqual(len(fake.calls_for("create_audit_run_v1")),  1)
        self.assertEqual(len(fake.calls_for("complete_audit_run_v1")), 0)
        end_calls = fake.calls_for("end_audit_run_v1")
        self.assertEqual(len(end_calls), 1)
        self.assertEqual(end_calls[0]["p_final_status"], "failed")

    def test_llm_validation_failure_triggers_end_audit_run(self):
        """Invalid LLM response raises and ends the audit run with status failed."""
        from workers.llm_audit import LlmAuditValidationError
        fake = self._make_fake()
        provider = FakeLlmProvider(parsed_response={"findings": "not a list"})
        handler = make_hybrid_audit_handler(fake, llm_provider=provider)

        with self.assertRaises(LlmAuditValidationError):
            self._call(handler)

        end_calls = fake.calls_for("end_audit_run_v1")
        self.assertEqual(len(end_calls), 1)
        self.assertEqual(end_calls[0]["p_final_status"], "failed")
        self.assertEqual(len(fake.calls_for("complete_audit_run_v1")), 0)

    def test_create_run_failure_no_end_or_complete(self):
        """Failure of create_audit_run_v1 must not trigger end_ or complete_."""
        fake = FakeSupabase()
        fake.set_error_response("create_audit_run_v1", RuntimeError("DB down"))
        provider = FakeLlmProvider()
        handler = make_hybrid_audit_handler(fake, llm_provider=provider)

        with self.assertRaises(RuntimeError):
            self._call(handler)

        self.assertEqual(len(fake.calls_for("complete_audit_run_v1")), 0)
        self.assertEqual(len(fake.calls_for("end_audit_run_v1")),      0)

    # ------------------------------------------------------------------
    # Return value
    # ------------------------------------------------------------------

    def test_token_and_cost_values_returned(self):
        fake = self._make_fake()
        provider = FakeLlmProvider(
            input_tokens=200, output_tokens=80, actual_cost_usd=0.012
        )
        handler = make_hybrid_audit_handler(fake, llm_provider=provider)
        result = self._call(handler)

        self.assertEqual(result["input_tokens"],  200)
        self.assertEqual(result["output_tokens"], 80)
        self.assertAlmostEqual(result["actual_cost_usd"], 0.012)

    # ------------------------------------------------------------------
    # No direct table access
    # ------------------------------------------------------------------

    def test_no_direct_table_calls(self):
        """Handler must not call client.table() directly."""
        fake = self._make_fake()
        provider = FakeLlmProvider()
        handler = make_hybrid_audit_handler(fake, llm_provider=provider)
        self._call(handler)
        self.assertFalse(hasattr(fake, "_table_calls"),
                         "FakeSupabase has no .table() method — any call would raise AttributeError")


# ===========================================================================
# TestWorkerHybridAuditIntegration
# ===========================================================================

class TestWorkerHybridAuditIntegration(unittest.TestCase):

    def _make_bg_job(self) -> dict:
        return {
            "job_id":           "job-aaaaaa",
            "job_type":         "hybrid_audit",
            "payload":          _VALID_HYBRID_PAYLOAD,
            "checkpoint":       {},
            "attempt_count":    1,
            "job_status":       "running",
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
        }

    def _heartbeat_response(self, job_id: str) -> dict:
        return {
            "job_id":           job_id,
            "job_status":       "running",
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
            "heartbeat_at":     "2099-01-01T00:00:00+00:00",
        }

    def _make_fake(self):
        fake = FakeSupabase()
        job = self._make_bg_job()

        fake.set_response("claim_background_job_v1",     [job])
        fake.set_response("heartbeat_background_job_v1", [
            self._heartbeat_response(job["job_id"]),
        ])
        fake.set_response("create_audit_run_v1",          _CREATE_AUDIT_RESPONSE)
        fake.set_response("complete_audit_run_v1",         _COMPLETE_AUDIT_RESPONSE)
        fake.set_response("complete_background_job_v1",   [
            {"job_id": job["job_id"], "job_status": "completed",
             "completed_at": "2099-01-01T00:00:00+00:00"},
        ])
        fake.set_response("fail_background_job_v1",       [
            {"job_id": job["job_id"], "job_status": "pending"},
        ])
        fake.set_response("end_audit_run_v1",              _END_AUDIT_RESPONSE)
        return fake

    def _make_worker(self, fake, provider):
        from workers.background_worker import BackgroundWorker
        return BackgroundWorker(
            worker_id="test-worker",
            client=fake,
            handlers=build_handler_registry(fake, llm_provider=provider),
            sleep_interval=0.0,
        )

    def test_successful_result_reaches_complete_background_job(self):
        """End-to-end: hybrid_audit job completes and calls complete_background_job_v1."""
        fake = self._make_fake()
        provider = FakeLlmProvider(input_tokens=150, output_tokens=60,
                                   actual_cost_usd=0.008)
        worker = self._make_worker(fake, provider)
        worker.run_once()

        complete_calls = fake.calls_for("complete_background_job_v1")
        self.assertEqual(len(complete_calls), 1)
        result = complete_calls[0].get("p_result", {})
        self.assertEqual(result.get("run_status"), "completed")
        self.assertEqual(result.get("input_tokens"), 150)
        self.assertEqual(result.get("output_tokens"), 60)
        self.assertAlmostEqual(result.get("actual_cost_usd"), 0.008)
        self.assertEqual(len(fake.calls_for("fail_background_job_v1")), 0)

    def test_remaining_stubs_still_not_implemented(self):
        """question_generation, embedding_generation, other remain stubs."""
        fake = self._make_fake()
        provider = FakeLlmProvider()
        registry = build_handler_registry(fake, llm_provider=provider)
        still_stubbed = ["question_generation", "embedding_generation", "other"]
        for job_type in still_stubbed:
            handler = registry[job_type]
            with self.assertRaises(NotImplementedHandler,
                                   msg=f"{job_type} must still be a stub"):
                handler(job_id="x", payload={}, checkpoint={},
                        attempt=1, heartbeat_fn=lambda: None)

    def test_no_direct_table_calls_worker_path(self):
        """BackgroundWorker must not call fake.table() during hybrid_audit processing."""
        fake = self._make_fake()
        provider = FakeLlmProvider()
        worker = self._make_worker(fake, provider)
        worker.run_once()
        self.assertFalse(
            hasattr(fake, "_table_calls"),
            "FakeSupabase has no .table() — any direct table access raises AttributeError",
        )


if __name__ == "__main__":
    unittest.main()
