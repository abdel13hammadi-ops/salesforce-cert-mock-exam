"""
Orchestration tests for V48 AI quality audit worker (mocks/fakes only).
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_worker import (
    AiQualityAuditProviders,
    AiQualityAuditWorkerError,
    process_ai_quality_audit_job,
    validate_job_payload,
)
from workers.llm_providers import LlmProviderError, LlmResponse

_QVID = "cccccccc-0000-0000-0000-000000000001"
_RUN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_CHUNK_1 = "11111111-1111-1111-1111-111111111111"
_CHUNK_2 = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# Fake Supabase (table eq/in/limit filtering; scripted claim queue)
# ---------------------------------------------------------------------------


class _FakeRpcResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


class _FakeRpcBuilder:
    def __init__(self, data, error=None):
        self._data = data
        self._error = error

    def execute(self):
        return _FakeRpcResult(self._data, self._error)


class _FakeTableQuery:
    def __init__(self, client, table_name: str):
        self._client = client
        self._table_name = table_name
        self._select = None
        self._filters: list[tuple[str, str, object]] = []
        self._order_field = None

    def select(self, fields: str):
        self._select = fields
        return self

    def eq(self, field: str, value: object):
        self._filters.append(("eq", field, value))
        return self

    def in_(self, field: str, values: list):
        self._filters.append(("in", field, tuple(values)))
        return self

    def order(self, field: str):
        self._order_field = field
        return self

    def limit(self, count: int):
        self._filters.append(("limit", "", count))
        return self

    def execute(self):
        key = (self._table_name, self._select, tuple(self._filters), self._order_field)
        if key in self._client._table_errors:
            return _FakeRpcResult([], error=self._client._table_errors[key])
        rows = self._client._table_responses.get(key)
        if rows is None:
            rows = list(self._client._table_responses.get(self._table_name, []))

        filtered = list(rows)
        limit_count = None
        for op, field, value in self._filters:
            if op == "eq":
                filtered = [row for row in filtered if row.get(field) == value]
            elif op == "in":
                filtered = [row for row in filtered if row.get(field) in value]
            elif op == "limit":
                limit_count = int(value)
        if limit_count is not None:
            filtered = filtered[:limit_count]
        return _FakeRpcResult(filtered)


class OrchestrationFakeSupabase:
    def __init__(self):
        self.rpc_calls: list[tuple[str, dict]] = []
        self.claim_queue: list[dict] = []
        self.record_calls: list[dict] = []
        self.persist_trigger_calls: list[dict] = []
        self.complete_calls: list[dict] = []
        self.record_rpc_error: str | None = None
        self._table_responses: dict = {}
        self._table_errors: dict = {}

    def enqueue_claims(self, *claims: dict) -> None:
        self.claim_queue.extend(claims)

    def set_table_response(self, table_name: str, data: list, *, key=None):
        self._table_responses[key or table_name] = data

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        if name == "claim_ai_quality_audit_pass_v1":
            if not self.claim_queue:
                raise RuntimeError("claim queue exhausted")
            return _FakeRpcBuilder([self.claim_queue.pop(0)])
        if name == "record_audit_pass_result_v1":
            if self.record_rpc_error:
                raise RuntimeError(self.record_rpc_error)
            self.record_calls.append(dict(params))
            return _FakeRpcBuilder([{"status": params.get("p_status")}])
        if name == "persist_audit_run_dispute_trigger_v1":
            self.persist_trigger_calls.append(dict(params))
            return _FakeRpcBuilder([{"reason_code": params.get("p_reason_code")}])
        if name == "complete_ai_quality_audit_run_v1":
            self.complete_calls.append(dict(params))
            row = {
                "run_status": "completed",
                "finding_count": len(params.get("p_confirmed_findings") or []),
                "evidence_count": 0,
            }
            return _FakeRpcBuilder([row])
        return _FakeRpcBuilder([{}])

    def table(self, name: str):
        return _FakeTableQuery(self, name)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _job_payload(**overrides) -> dict:
    base = {
        "audit_run_id": _RUN_ID,
        "question_version_id": _QVID,
    }
    base.update(overrides)
    return base


def _claim(action: str, pass_code: str | None = None, **overrides) -> dict:
    row = {
        "action": action,
        "pass_code": pass_code,
        "run_status": "running",
        "lease_token": "lease-token-1",
        "model_name": "test-primary-model",
    }
    row.update(overrides)
    return row


def _llm_response(parsed: dict, **overrides) -> LlmResponse:
    defaults = {
        "parsed_response": parsed,
        "input_tokens": 100,
        "output_tokens": 40,
        "provider_request_id": "provider-req-1",
    }
    defaults.update(overrides)
    return LlmResponse(**defaults)


def _pass_a_result() -> dict:
    return {"selected_option_labels": ["A"]}


def _pass_b_result(*, findings=None) -> dict:
    return {
        "selected_option_labels": ["A"],
        "proposed_findings": findings if findings is not None else [],
    }


def _blocking_finding(**overrides) -> dict:
    base = {
        "finding_ref": "F1",
        "finding_code": "WRONG_ANSWER_KEY",
        "finding_type": "correctness",
        "severity": "high",
        "materiality": "blocking",
        "title": "Wrong answer key",
        "description": "Marked correct option is wrong.",
        "evidence_chunk_ids": [_CHUNK_1],
        "metadata": {},
    }
    base.update(overrides)
    return base


def _warning_only_blocking_finding() -> dict:
    return _blocking_finding(
        finding_code="SOURCE_SUPPORT_WEAK",
        finding_type="source_support",
        severity="medium",
        materiality="blocking",
        title="Weak source support",
        description="Official source does not strongly support the explanation.",
    )


def _pass_c_normal_resolved(**overrides) -> dict:
    base = {
        "resolution_type": "NORMAL_DISPUTE",
        "resolution_status": "RESOLVED",
        "substituted_for_passes": [],
        "confirmed_finding_refs": ["F1"],
    }
    base.update(overrides)
    return base


def _pass_c_unresolved() -> dict:
    return {
        "resolution_type": "NORMAL_DISPUTE",
        "resolution_status": "UNRESOLVED",
        "substituted_for_passes": [],
        "confirmed_finding_refs": [],
    }


def _pass_c_pass_a_substitution(**overrides) -> dict:
    base = {
        "resolution_type": "PASS_A_SUBSTITUTION",
        "resolution_status": "RESOLVED",
        "substituted_for_passes": ["A", "B"],
        "confirmed_finding_refs": ["F1"],
        "proposed_findings": [_blocking_finding()],
    }
    base.update(overrides)
    return base


def _pass_c_pass_b_substitution(**overrides) -> dict:
    base = {
        "resolution_type": "PASS_B_SUBSTITUTION",
        "resolution_status": "RESOLVED",
        "substituted_for_passes": ["B"],
        "confirmed_finding_refs": ["F1"],
        "proposed_findings": [_blocking_finding()],
    }
    base.update(overrides)
    return base


MIN_BLIND_CONTEXT = {
    "question_version_id": _QVID,
    "certification_exam_name": "ADM-201",
    "domain_name": "Configuration",
    "question_text": "Which feature enables this?",
    "question_type": "single",
    "required_selection_count": 1,
    "options": [
        {"option_label": "A", "option_text": "Profiles", "display_order": 1},
        {"option_label": "B", "option_text": "Roles", "display_order": 2},
    ],
    "audit_run_id": _RUN_ID,
}

MIN_COMPARISON_CONTEXT = {
    "question_version_id": _QVID,
    "audit_run_id": _RUN_ID,
    "certification_exam_name": "ADM-201",
    "domain_name": "Configuration",
    "question_text": "Which feature enables this?",
    "question_type": "single",
    "required_selection_count": 1,
    "options": [
        {
            "option_label": "A",
            "option_text": "Profiles",
            "display_order": 1,
            "is_correct": True,
        },
        {
            "option_label": "B",
            "option_text": "Roles",
            "display_order": 2,
            "is_correct": False,
        },
    ],
    "stored_correct_option_labels": ["A"],
    "pass_a_selected_option_labels": ["A"],
    "explanation": "Profiles control object permissions.",
    "frozen_evidence": [
        {
            "rank": 1,
            "chunk_id": _CHUNK_1,
            "chunk_text": "Profiles define default settings.",
            "authoritative_hash": "hash-1",
        },
        {
            "rank": 2,
            "chunk_id": _CHUNK_2,
            "chunk_text": "Permission sets extend access.",
            "authoritative_hash": "hash-2",
        },
    ],
}


def _wire_normal_no_dispute_tables(client: OrchestrationFakeSupabase) -> None:
    client.set_table_response(
        "audit_run_pass_results",
        [
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "A",
                "status": "completed",
                "result_json": _pass_a_result(),
            },
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "B",
                "status": "completed",
                "result_json": _pass_b_result(),
            },
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "C",
                "status": "skipped",
                "result_json": None,
            },
        ],
    )
    client.set_table_response("audit_run_dispute_triggers", [])
    client.set_table_response("audit_findings", [])
    client.set_table_response("audit_finding_evidence", [])


def _wire_resolved_dispute_tables(client: OrchestrationFakeSupabase) -> None:
    finding = _blocking_finding()
    client.set_table_response(
        "audit_run_pass_results",
        [
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "A",
                "status": "completed",
                "result_json": _pass_a_result(),
            },
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "B",
                "status": "completed",
                "result_json": _pass_b_result(findings=[finding]),
            },
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "C",
                "status": "completed",
                "result_json": _pass_c_normal_resolved(),
            },
        ],
    )
    client.set_table_response(
        "audit_run_dispute_triggers",
        [
            {
                "audit_run_id": _RUN_ID,
                "reason_code": "BLOCKING_DEFECT_PROPOSED",
                "source_pass_code": "B",
                "trigger_reason": "Pass B proposed one or more blocking findings",
                "finding_refs": ["F1"],
            }
        ],
    )
    client.set_table_response("audit_findings", [{"id": "finding-1"}])
    client.set_table_response("audit_finding_evidence", [])


def _wire_pass_a_substitution_tables(client: OrchestrationFakeSupabase) -> None:
    client.set_table_response(
        "audit_run_pass_results",
        [
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "A",
                "status": "schema_invalid",
                "result_json": None,
            },
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "B",
                "status": "completed",
                "result_json": _pass_b_result(),
            },
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "C",
                "status": "completed",
                "result_json": _pass_c_pass_a_substitution(),
            },
        ],
    )
    client.set_table_response(
        "audit_run_dispute_triggers",
        [
            {
                "audit_run_id": _RUN_ID,
                "reason_code": "PASS_A_SCHEMA_INVALID",
                "source_pass_code": "A",
                "trigger_reason": (
                    "Pass A response failed schema validation after two attempts"
                ),
                "finding_refs": [],
            }
        ],
    )
    client.set_table_response("audit_findings", [{"id": "finding-1"}])
    client.set_table_response("audit_finding_evidence", [])


def _wire_pass_b_substitution_tables(client: OrchestrationFakeSupabase) -> None:
    client.set_table_response(
        "audit_run_pass_results",
        [
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "A",
                "status": "completed",
                "result_json": _pass_a_result(),
            },
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "B",
                "status": "schema_invalid",
                "result_json": None,
            },
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "C",
                "status": "completed",
                "result_json": _pass_c_pass_b_substitution(),
            },
        ],
    )
    client.set_table_response(
        "audit_run_dispute_triggers",
        [
            {
                "audit_run_id": _RUN_ID,
                "reason_code": "PASS_B_SCHEMA_INVALID",
                "source_pass_code": "B",
                "trigger_reason": (
                    "Pass B response failed schema validation after two attempts"
                ),
                "finding_refs": [],
            }
        ],
    )
    client.set_table_response("audit_findings", [{"id": "finding-1"}])
    client.set_table_response("audit_finding_evidence", [])


class _SequenceProvider:
    def __init__(self, items):
        self._items = list(items)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _HangingProvider:
    """Blocks longer than the configured worker timeout to force timeout failure."""

    def __init__(self, *, response_factory=_pass_a_result):
        self.calls: list[dict] = []
        self._response_factory = response_factory

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        timeout = (kwargs.get("metadata") or {}).get("timeout_seconds")
        if timeout is not None:
            time.sleep(float(timeout) + 0.5)
        return _llm_response(self._response_factory())


def _wire_pass_c_prerequisite_tables(client: OrchestrationFakeSupabase) -> None:
    client.set_table_response(
        "audit_run_pass_results",
        [
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "A",
                "status": "completed",
                "result_json": _pass_a_result(),
            },
            {
                "audit_run_id": _RUN_ID,
                "pass_code": "B",
                "status": "completed",
                "result_json": _pass_b_result(findings=[_blocking_finding()]),
            },
        ],
    )
    client.set_table_response(
        "audit_run_dispute_triggers",
        [
            {
                "audit_run_id": _RUN_ID,
                "reason_code": "BLOCKING_DEFECT_PROPOSED",
                "source_pass_code": "B",
                "trigger_reason": "Pass B proposed one or more blocking findings",
                "finding_refs": ["F1"],
            }
        ],
    )


def _providers_with_timeout(primary, dispute=None, *, timeout_seconds=0.1):
    return AiQualityAuditProviders(
        primary=primary,
        dispute=dispute or (lambda **_: _llm_response(_pass_c_normal_resolved())),
        timeout_seconds=timeout_seconds,
    )


def _assert_timeout_failure(testcase, client, provider, *, pass_code):
    testcase.assertEqual(len(provider.calls), 1)
    testcase.assertEqual(
        provider.calls[0]["metadata"]["timeout_seconds"],
        0.1,
    )
    testcase.assertEqual(client.record_calls[0]["p_status"], "failed")
    testcase.assertEqual(
        client.record_calls[0]["p_last_error"]["error_code"],
        "LLM_PROVIDER_ERROR",
    )
    testcase.assertIn(
        "provider call timed out after 0.1 seconds",
        client.record_calls[0]["p_last_error"]["message"],
    )
    testcase.assertEqual(client.record_calls[0]["p_pass_code"], pass_code)


def _run_job(client, providers, **kwargs):
    patches = (
        patch(
            "workers.ai_quality_audit_worker.load_blind_audit_context",
            return_value=dict(MIN_BLIND_CONTEXT),
        ),
        patch(
            "workers.ai_quality_audit_worker.load_comparison_audit_context",
            return_value=dict(MIN_COMPARISON_CONTEXT),
        ),
    )
    with patches[0], patches[1]:
        return process_ai_quality_audit_job(
            client,
            _job_payload(),
            providers,
            worker_id="test-worker",
            **kwargs,
        )


class TestAiQualityAuditWorkerOrchestration(unittest.TestCase):

    def test_full_normal_no_dispute_path(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("SKIP_PASS_C", "C"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
        _wire_normal_no_dispute_tables(client)

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_pass_b_result()),
            ]
        )
        dispute = _SequenceProvider([])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(result["run_status"], "completed")
        self.assertEqual(result["passes_executed"], ["A", "B"])
        self.assertEqual(result["completion_shape"], "NORMAL_NO_DISPUTE")
        self.assertEqual(len(client.complete_calls), 1)
        self.assertEqual(client.complete_calls[0]["p_confirmed_findings"], [])
        self.assertEqual(len(client.persist_trigger_calls), 0)
        self.assertEqual(dispute.calls, [])

    def test_full_resolved_dispute_path(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
        _wire_resolved_dispute_tables(client)

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_pass_b_result(findings=[_blocking_finding()])),
            ]
        )
        dispute = _SequenceProvider([_llm_response(_pass_c_normal_resolved())])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(result["run_status"], "completed")
        self.assertEqual(result["passes_executed"], ["A", "B", "C"])
        self.assertEqual(result["completion_shape"], "NORMAL_DISPUTE")
        self.assertEqual(len(client.persist_trigger_calls), 1)
        self.assertEqual(
            client.persist_trigger_calls[0]["p_reason_code"],
            "BLOCKING_DEFECT_PROPOSED",
        )
        self.assertEqual(len(dispute.calls), 1)
        confirmed = client.complete_calls[0]["p_confirmed_findings"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["finding_ref"], "F1")

    def test_unresolved_dispute_inconclusive_zero_findings(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        client.set_table_response(
            "audit_run_pass_results",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "pass_code": "B",
                    "status": "completed",
                    "result_json": _pass_b_result(findings=[_blocking_finding()]),
                },
            ],
        )
        client.set_table_response(
            "audit_run_dispute_triggers",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "reason_code": "BLOCKING_DEFECT_PROPOSED",
                    "source_pass_code": "B",
                    "trigger_reason": "Pass B proposed one or more blocking findings",
                    "finding_refs": ["F1"],
                }
            ],
        )

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_pass_b_result(findings=[_blocking_finding()])),
            ]
        )
        dispute = _SequenceProvider([_llm_response(_pass_c_unresolved())])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(result["passes_executed"], ["A", "B", "C"])
        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(len(client.complete_calls), 0)

    def test_pass_a_first_schema_invalid_then_success(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_A", "A"),
            _claim("WAIT"),
        )

        primary = _SequenceProvider(
            [
                _llm_response({"selected_option_labels": ["Z"]}),
                _llm_response(_pass_a_result()),
            ]
        )

        with self.assertRaises(AiQualityAuditWorkerError):
            _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
            )

        self.assertEqual(len(primary.calls), 2)
        self.assertEqual(client.record_calls[0]["p_status"], "schema_invalid")
        self.assertEqual(client.record_calls[1]["p_status"], "completed")

    def test_pass_a_twice_schema_invalid_triggers_substitution(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_A", "A"),
            _claim("NEEDS_DISPUTE_TRIGGER_A"),
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
        _wire_pass_a_substitution_tables(client)

        primary = _SequenceProvider(
            [
                _llm_response({"selected_option_labels": ["Z"]}),
                _llm_response({"selected_option_labels": ["Z"]}),
            ]
        )
        dispute = _SequenceProvider([_llm_response(_pass_c_pass_a_substitution())])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(result["completion_shape"], "PASS_A_SUBSTITUTION")
        self.assertEqual(result["passes_executed"], ["A", "A", "C"])
        trigger_calls = [
            call for call in client.persist_trigger_calls
            if call["p_reason_code"] == "PASS_A_SCHEMA_INVALID"
        ]
        self.assertEqual(len(trigger_calls), 1)
        self.assertEqual(len(dispute.calls), 1)

    def test_pass_b_first_schema_invalid_then_success(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("WAIT"),
        )

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response({"selected_option_labels": ["Z"]}),
                _llm_response(_pass_b_result()),
            ]
        )

        with self.assertRaises(AiQualityAuditWorkerError):
            _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
            )

        pass_b_records = [
            call for call in client.record_calls if call["p_pass_code"] == "B"
        ]
        self.assertEqual(pass_b_records[0]["p_status"], "schema_invalid")
        self.assertEqual(pass_b_records[1]["p_status"], "completed")

    def test_pass_b_twice_schema_invalid_triggers_substitution(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("NEEDS_DISPUTE_TRIGGER_B"),
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
        _wire_pass_b_substitution_tables(client)

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response({"selected_option_labels": ["Z"]}),
                _llm_response({"selected_option_labels": ["Z"]}),
            ]
        )
        dispute = _SequenceProvider([_llm_response(_pass_c_pass_b_substitution())])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(result["completion_shape"], "PASS_B_SUBSTITUTION")
        trigger_calls = [
            call for call in client.persist_trigger_calls
            if call["p_reason_code"] == "PASS_B_SCHEMA_INVALID"
        ]
        self.assertEqual(len(trigger_calls), 1)

    def test_provider_failure_recorded_correctly(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("WAIT"),
        )

        primary = _SequenceProvider([LlmProviderError("provider timeout")])

        with self.assertRaises(AiQualityAuditWorkerError):
            _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
            )

        self.assertEqual(client.record_calls[0]["p_status"], "failed")
        self.assertEqual(
            client.record_calls[0]["p_last_error"]["error_code"],
            "LLM_PROVIDER_ERROR",
        )
        self.assertIn("provider timeout", client.record_calls[0]["p_last_error"]["message"])

    def test_stale_lease_token_aborts_safely(self):
        client = OrchestrationFakeSupabase()
        client.record_rpc_error = "lease token mismatch for pass A"
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
        )

        primary = _SequenceProvider([_llm_response(_pass_a_result())])

        with self.assertRaisesRegex(AiQualityAuditWorkerError, "lease token mismatch"):
            _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
            )

    def test_comparison_context_not_loaded_before_pass_a_completion(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("WAIT"),
        )

        primary = _SequenceProvider([_llm_response(_pass_a_result())])

        with patch(
            "workers.ai_quality_audit_worker.load_blind_audit_context",
            return_value=dict(MIN_BLIND_CONTEXT),
        ) as load_blind, patch(
            "workers.ai_quality_audit_worker.load_comparison_audit_context",
        ) as load_comparison:
            with self.assertRaises(AiQualityAuditWorkerError):
                process_ai_quality_audit_job(
                    client,
                    _job_payload(),
                    AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
                    worker_id="test-worker",
                )
            load_blind.assert_called_once()
            load_comparison.assert_not_called()

    def test_pass_c_not_called_when_database_says_skip_pass_c(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("SKIP_PASS_C", "C"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
        _wire_normal_no_dispute_tables(client)

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_pass_b_result()),
            ]
        )
        dispute = _SequenceProvider([])

        _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(len(dispute.calls), 0)
        pass_codes = [call["p_pass_code"] for call in client.record_calls]
        self.assertEqual(pass_codes, ["A", "B"])

    def test_blocking_finding_requires_pass_c_confirmation_path(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
        _wire_resolved_dispute_tables(client)

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_pass_b_result(findings=[_blocking_finding()])),
            ]
        )
        dispute = _SequenceProvider([_llm_response(_pass_c_normal_resolved())])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(result["passes_executed"], ["A", "B", "C"])
        self.assertEqual(
            client.persist_trigger_calls[0]["p_reason_code"],
            "BLOCKING_DEFECT_PROPOSED",
        )
        self.assertEqual(len(dispute.calls), 1)
        self.assertEqual(dispute.calls[0]["metadata"]["pass_code"], "C")

    def test_warning_only_finding_cannot_become_blocking(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("WAIT"),
        )

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(
                    _pass_b_result(findings=[_warning_only_blocking_finding()])
                ),
            ]
        )

        with self.assertRaises(AiQualityAuditWorkerError):
            _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
            )

        pass_b_record = next(
            call for call in client.record_calls if call["p_pass_code"] == "B"
        )
        self.assertEqual(pass_b_record["p_status"], "schema_invalid")
        self.assertEqual(len(client.persist_trigger_calls), 0)

    def test_malformed_job_payload_rejected(self):
        client = OrchestrationFakeSupabase()
        with self.assertRaises(AiQualityAuditWorkerError):
            validate_job_payload({"audit_run_id": "not-a-uuid", "question_version_id": _QVID})
        with self.assertRaises(AiQualityAuditWorkerError):
            validate_job_payload({"question_version_id": _QVID})
        self.assertEqual(len(client.rpc_calls), 0)

    def test_no_third_provider_attempt_on_same_pass(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_A", "A"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        primary = _SequenceProvider(
            [
                LlmProviderError("first failure"),
                LlmProviderError("second failure"),
            ]
        )

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(len(primary.calls), 2)
        failed_records = [
            call for call in client.record_calls if call["p_status"] == "failed"
        ]
        self.assertEqual(len(failed_records), 2)


class TestAiQualityAuditProviderTimeout(unittest.TestCase):

    def test_pass_a_primary_timeout_recorded_as_failed(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("WAIT"),
        )
        primary = _HangingProvider()

        with self.assertRaises(AiQualityAuditWorkerError):
            _run_job(client, _providers_with_timeout(primary))

        _assert_timeout_failure(self, client, primary, pass_code="A")

    def test_pass_b_primary_timeout_recorded_as_failed(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_B", "B"),
            _claim("WAIT"),
        )
        primary = _HangingProvider(response_factory=_pass_b_result)

        with self.assertRaises(AiQualityAuditWorkerError):
            _run_job(client, _providers_with_timeout(primary))

        _assert_timeout_failure(self, client, primary, pass_code="B")

    def test_pass_c_dispute_timeout_recorded_as_failed(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("WAIT"),
        )
        _wire_pass_c_prerequisite_tables(client)
        dispute = _HangingProvider(response_factory=_pass_c_normal_resolved)

        with self.assertRaises(AiQualityAuditWorkerError):
            _run_job(
                client,
                _providers_with_timeout(_SequenceProvider([]), dispute=dispute),
            )

        _assert_timeout_failure(self, client, dispute, pass_code="C")


if __name__ == "__main__":
    unittest.main()
