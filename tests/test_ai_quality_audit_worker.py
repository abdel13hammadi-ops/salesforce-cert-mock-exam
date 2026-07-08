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
    build_confirmed_findings_for_completion,
    process_ai_quality_audit_job,
    validate_job_payload,
)
from workers.llm_providers import LlmProviderError, LlmResponse
from workers.llm_audit import LlmAuditValidationError

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


def _option_judgment(label: str, verdict: str, *, chunk_ids=None, rationale=None) -> dict:
    return {
        "option_label": label,
        "verdict": verdict,
        "citation_chunk_ids": list(chunk_ids or []),
        "evidence_rationale": rationale or f"Evidence assessment for option {label}.",
    }


def _correctness_result(
    *,
    supported=("A",),
    not_supported=("B",),
    insufficient=(),
    evidence_sufficient=True,
    abstention_reason=None,
    citation_chunk_id=_CHUNK_1,
) -> dict:
    """Default V60 specialist response fixture. With the default arguments
    this represents a fully-confirmed, decisive answer (supported == stored
    correct set {"A"} on the default single-select fixture), so
    ``derive_correctness_finding`` returns ``None`` (no correctness finding)
    -- i.e. this is the "specialist agrees with the stored key" filler used
    by tests that are not specifically exercising the correctness detector.
    """
    judgments = []
    for label in supported:
        judgments.append(
            _option_judgment(label, "SUPPORTED_AS_CORRECT", chunk_ids=[citation_chunk_id])
        )
    for label in not_supported:
        judgments.append(
            _option_judgment(label, "NOT_SUPPORTED_AS_CORRECT", chunk_ids=[citation_chunk_id])
        )
    for label in insufficient:
        judgments.append(_option_judgment(label, "INSUFFICIENT_EVIDENCE"))
    return {
        "option_judgments": judgments,
        "evidence_sufficient_for_decision": evidence_sufficient,
        "abstention_reason": abstention_reason,
    }


def _correctness_result_abstain(reason: str = "Evidence does not address every option.") -> dict:
    return _correctness_result(
        supported=(),
        not_supported=(),
        insufficient=("A", "B"),
        evidence_sufficient=False,
        abstention_reason=reason,
    )


def _blocking_finding(**overrides) -> dict:
    # V60: WRONG_ANSWER_KEY/UNSUPPORTED_ANSWER/MULTIPLE_DEFENSIBLE_ANSWERS are
    # now owned exclusively by the specialized correctness detector and are
    # discarded if the general judge proposes them (see
    # ``merge_pass_b_findings``). This fixture represents a generic
    # *general-judge* blocking finding used throughout this file to exercise
    # dispute-routing/Pass-C/canonical-materiality machinery that is
    # agnostic to which specific defect triggered it, so its default code
    # uses a non-correctness blocking code instead.
    base = {
        "finding_ref": "F1",
        "finding_code": "EXPLANATION_MISSING",
        "finding_type": "explanation_quality",
        "severity": "high",
        "materiality": "blocking",
        "title": "Explanation missing",
        "description": "No explanation was provided for the correct answer.",
        "evidence_chunk_ids": [_CHUNK_1],
        "metadata": {},
    }
    base.update(overrides)
    return base


def _warning_finding(**overrides) -> dict:
    base = {
        "finding_ref": "F1",
        "finding_code": "WEAK_DISTRACTORS",
        "finding_type": "answer_quality",
        "severity": "low",
        "materiality": "warning",
        "title": "Weak distractors",
        "description": "One distractor is obviously implausible.",
        "evidence_chunk_ids": [_CHUNK_1],
        "metadata": {"note": "distractor-quality"},
    }
    base.update(overrides)
    return base


def _informational_finding(**overrides) -> dict:
    base = {
        "finding_ref": "F2",
        "finding_code": "OTHER_REVIEW_NEEDED",
        "finding_type": "other",
        "severity": "info",
        "materiality": "informational",
        "title": "Minor polish issue",
        "description": "Cosmetic wording could be tightened.",
        "evidence_chunk_ids": [_CHUNK_2],
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


def _explanation_missing_reported_as_warning() -> dict:
    """V59-FINDING-01 regression fixture: the exact qbv1-010 failure
    pattern -- a provider proposes ``EXPLANATION_MISSING`` and self-reports
    ``materiality='warning'``, which contradicts canonical policy
    (``workers.finding_policy.assign_materiality`` requires 'blocking').
    """
    return _blocking_finding(
        finding_code="EXPLANATION_MISSING",
        finding_type="explanation_quality",
        severity="medium",
        materiality="warning",
        title="Explanation missing",
        description="No explanation was provided for the correct answer.",
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
                _llm_response(_correctness_result()),
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
                _llm_response(_correctness_result()),
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
                _llm_response(_correctness_result()),
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
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        primary = _SequenceProvider(
            [
                _llm_response({"selected_option_labels": ["Z"]}),
                _llm_response(_pass_a_result()),
            ]
        )

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        self.assertEqual(result["run_status"], "inconclusive")
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
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_correctness_result()),
                _llm_response({"selected_option_labels": ["Z"]}),
                _llm_response(_correctness_result()),
                _llm_response(_pass_b_result()),
            ]
        )

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        self.assertEqual(result["run_status"], "inconclusive")
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
                _llm_response(_correctness_result()),
                _llm_response({"selected_option_labels": ["Z"]}),
                _llm_response(_correctness_result()),
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
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        primary = _SequenceProvider([LlmProviderError("provider timeout")])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        self.assertEqual(result["run_status"], "inconclusive")
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
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        primary = _SequenceProvider([_llm_response(_pass_a_result())])

        with patch(
            "workers.ai_quality_audit_worker.load_blind_audit_context",
            return_value=dict(MIN_BLIND_CONTEXT),
        ) as load_blind, patch(
            "workers.ai_quality_audit_worker.load_comparison_audit_context",
        ) as load_comparison:
            result = process_ai_quality_audit_job(
                client,
                _job_payload(),
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
                worker_id="test-worker",
            )
            self.assertEqual(result["run_status"], "inconclusive")
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
                _llm_response(_correctness_result()),
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
                _llm_response(_correctness_result()),
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
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_correctness_result()),
                _llm_response(
                    _pass_b_result(findings=[_warning_only_blocking_finding()])
                ),
            ]
        )

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        self.assertEqual(result["run_status"], "inconclusive")
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


class TestCanonicalMaterialityDrivesDisputeRouting(unittest.TestCase):
    """V59-FINDING-01 end-to-end: canonicalization happens at schema
    validation (``workers.ai_quality_audit_schemas``), so its effect must be
    visible in the worker's own pass-sequencing decisions -- specifically,
    whether a proposed ``EXPLANATION_MISSING`` finding trips the
    ``BLOCKING_DEFECT_PROPOSED`` dispute trigger. Before this fix, a
    provider self-reporting 'warning' for ``EXPLANATION_MISSING`` (exactly
    the confirmed qbv1-010 pattern) would never trip this trigger and the
    run would silently complete via NORMAL_NO_DISPUTE/SKIP_PASS_C.
    """

    def test_explanation_missing_reported_as_warning_still_triggers_blocking_dispute(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
        # Represents what the RPC would actually have persisted for Pass B:
        # the *canonicalized* (blocking) materiality, since
        # record_audit_pass_result_v1 only ever sees the already-validated
        # result_json produced by workers.ai_quality_audit_schemas.
        canonicalized_finding = dict(
            _explanation_missing_reported_as_warning(), materiality="blocking"
        )
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
                    "result_json": _pass_b_result(findings=[canonicalized_finding]),
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

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_correctness_result()),
                # The *provider's own* response still self-reports 'warning'
                # -- this is exactly what real schema validation (not
                # bypassed here) must canonicalize to 'blocking' before
                # process_ai_quality_audit_job ever inspects materiality.
                _llm_response(
                    _pass_b_result(findings=[_explanation_missing_reported_as_warning()])
                ),
            ]
        )
        dispute = _SequenceProvider([_llm_response(_pass_c_normal_resolved())])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(result["completion_shape"], "NORMAL_DISPUTE")
        self.assertEqual(len(client.persist_trigger_calls), 1)
        self.assertEqual(
            client.persist_trigger_calls[0]["p_reason_code"],
            "BLOCKING_DEFECT_PROPOSED",
        )
        self.assertEqual(client.persist_trigger_calls[0]["p_finding_refs"], ["F1"])
        confirmed = client.complete_calls[0]["p_confirmed_findings"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["finding_code"], "EXPLANATION_MISSING")
        self.assertEqual(confirmed[0]["materiality"], "blocking")


class TestNormalNoDisputeFindingPersistence(unittest.TestCase):
    """Covers the warning-persistence fix: NORMAL_NO_DISPUTE must preserve
    valid non-blocking Pass B proposals instead of discarding them, while
    still refusing to persist any blocking-materiality proposal without
    Pass C confirmation.
    """

    def test_single_warning_finding_persists_end_to_end(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("SKIP_PASS_C", "C"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
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
                    "result_json": _pass_b_result(findings=[_warning_finding()]),
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
        client.set_table_response("audit_findings", [{"id": "finding-1"}])
        client.set_table_response("audit_finding_evidence", [{"id": "ev-1"}])

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_correctness_result()),
                _llm_response(_pass_b_result(findings=[_warning_finding()])),
            ]
        )
        dispute = _SequenceProvider([])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(result["run_status"], "completed")
        self.assertEqual(result["completion_shape"], "NORMAL_NO_DISPUTE")
        self.assertEqual(len(client.persist_trigger_calls), 0)
        self.assertEqual(dispute.calls, [])
        confirmed = client.complete_calls[0]["p_confirmed_findings"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["finding_ref"], "F1")
        self.assertEqual(confirmed[0]["finding_code"], "WEAK_DISTRACTORS")
        self.assertEqual(confirmed[0]["materiality"], "warning")

    def test_multiple_non_blocking_findings_all_persist(self):
        pass_b_result = _pass_b_result(
            findings=[_warning_finding(), _informational_finding()]
        )
        client = OrchestrationFakeSupabase()
        client.set_table_response(
            "audit_run_pass_results",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "pass_code": "B",
                    "status": "completed",
                    "result_json": pass_b_result,
                },
            ],
        )

        confirmed = build_confirmed_findings_for_completion(
            client,
            audit_run_id=_RUN_ID,
            completion_shape="NORMAL_NO_DISPUTE",
        )

        self.assertEqual(len(confirmed), 2)
        refs = {item["finding_ref"] for item in confirmed}
        self.assertEqual(refs, {"F1", "F2"})
        materialities = {item["finding_ref"]: item["materiality"] for item in confirmed}
        self.assertEqual(materialities["F1"], "warning")
        self.assertEqual(materialities["F2"], "informational")

    def test_no_proposed_findings_persists_none(self):
        client = OrchestrationFakeSupabase()
        client.set_table_response(
            "audit_run_pass_results",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "pass_code": "B",
                    "status": "completed",
                    "result_json": _pass_b_result(findings=[]),
                },
            ],
        )

        confirmed = build_confirmed_findings_for_completion(
            client,
            audit_run_id=_RUN_ID,
            completion_shape="NORMAL_NO_DISPUTE",
        )

        self.assertEqual(confirmed, [])

    def test_blocking_finding_never_persists_through_normal_no_dispute(self):
        # Defense-in-depth: even if a blocking-materiality proposal somehow
        # reaches completion without a dispute trigger, it must be excluded
        # rather than persisted. Mixed with a valid warning finding to prove
        # the filter is selective, not all-or-nothing.
        pass_b_result = _pass_b_result(
            findings=[
                _blocking_finding(finding_ref="F1"),
                _warning_finding(finding_ref="F2"),
            ]
        )
        client = OrchestrationFakeSupabase()
        client.set_table_response(
            "audit_run_pass_results",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "pass_code": "B",
                    "status": "completed",
                    "result_json": pass_b_result,
                },
            ],
        )

        confirmed = build_confirmed_findings_for_completion(
            client,
            audit_run_id=_RUN_ID,
            completion_shape="NORMAL_NO_DISPUTE",
        )

        refs = {item["finding_ref"] for item in confirmed}
        self.assertNotIn("F1", refs)
        codes = {item["finding_code"] for item in confirmed}
        self.assertNotIn("WRONG_ANSWER_KEY", codes)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["finding_code"], "WEAK_DISTRACTORS")

    def test_evidence_and_provenance_survive_conversion(self):
        finding = _warning_finding(
            evidence_chunk_ids=[_CHUNK_1, _CHUNK_2],
            metadata={"source_support_context": {"attempted_retrieval": 1}},
        )
        client = OrchestrationFakeSupabase()
        client.set_table_response(
            "audit_run_pass_results",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "pass_code": "B",
                    "status": "completed",
                    "result_json": _pass_b_result(findings=[finding]),
                },
            ],
        )

        confirmed = build_confirmed_findings_for_completion(
            client,
            audit_run_id=_RUN_ID,
            completion_shape="NORMAL_NO_DISPUTE",
        )

        self.assertEqual(len(confirmed), 1)
        row = confirmed[0]
        self.assertEqual(
            row["evidence"],
            [
                {"resource_chunk_id": _CHUNK_1, "evidence_role": "supporting"},
                {"resource_chunk_id": _CHUNK_2, "evidence_role": "supporting"},
            ],
        )
        self.assertEqual(
            row["metadata"],
            {"source_support_context": {"attempted_retrieval": 1}},
        )
        self.assertEqual(row["finding_code"], "WEAK_DISTRACTORS")
        self.assertEqual(row["title"], finding["title"])
        self.assertEqual(row["description"], finding["description"])

    def test_normal_dispute_shape_behavior_unchanged(self):
        # NORMAL_DISPUTE must still only confirm Pass-C-confirmed refs; this
        # path is untouched by the NORMAL_NO_DISPUTE fix.
        finding = _blocking_finding()
        client = OrchestrationFakeSupabase()
        client.set_table_response(
            "audit_run_pass_results",
            [
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

        confirmed = build_confirmed_findings_for_completion(
            client,
            audit_run_id=_RUN_ID,
            completion_shape="NORMAL_DISPUTE",
        )

        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["finding_ref"], "F1")

    def test_normal_dispute_with_no_confirmed_refs_persists_none(self):
        client = OrchestrationFakeSupabase()
        client.set_table_response(
            "audit_run_pass_results",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "pass_code": "B",
                    "status": "completed",
                    "result_json": _pass_b_result(findings=[_blocking_finding()]),
                },
                {
                    "audit_run_id": _RUN_ID,
                    "pass_code": "C",
                    "status": "completed",
                    "result_json": _pass_c_unresolved(),
                },
            ],
        )

        confirmed = build_confirmed_findings_for_completion(
            client,
            audit_run_id=_RUN_ID,
            completion_shape="NORMAL_DISPUTE",
        )

        self.assertEqual(confirmed, [])


class TestAiQualityAuditSchemaRouting(unittest.TestCase):

    def test_pass_a_selected_option_labels_succeeds(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        primary = _SequenceProvider([_llm_response({"selected_option_labels": ["A"]})])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(client.record_calls[0]["p_status"], "completed")
        self.assertEqual(
            client.record_calls[0]["p_result_json"],
            {"selected_option_labels": ["A"]},
        )

    def test_pass_a_never_invokes_legacy_llm_audit_validator(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        primary = _SequenceProvider([_llm_response(_pass_a_result())])

        with patch("workers.llm_audit.validate_llm_response") as legacy_validate:
            _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
            )

        legacy_validate.assert_not_called()
        self.assertTrue(
            all(
                call["metadata"].get("skip_legacy_llm_audit_validation")
                for call in primary.calls
            )
        )

    def test_invalid_pass_a_json_records_schema_invalid_before_return(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        class _NonObjectProvider:
            calls: list = []

            def __call__(self, **kwargs):
                self.calls.append(kwargs)
                return _llm_response(["not-an-object"])

        provider = _NonObjectProvider()

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=provider, dispute=lambda **_: None),
        )

        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(len(client.record_calls), 1)
        self.assertEqual(client.record_calls[0]["p_status"], "schema_invalid")
        self.assertIsNotNone(client.record_calls[0].get("p_schema_validation_errors"))

    def test_invalid_pass_a_shape_records_schema_invalid_before_return(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        primary = _SequenceProvider([_llm_response({"unexpected": True})])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(client.record_calls[0]["p_status"], "schema_invalid")

    def test_pass_b_uses_dedicated_validator(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_B", "B"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        primary = _SequenceProvider(
            [_llm_response(_correctness_result()), _llm_response(_pass_b_result())]
        )

        with patch(
            "workers.ai_quality_audit_worker.validate_pass_b_result",
            wraps=__import__(
                "workers.ai_quality_audit_schemas", fromlist=["validate_pass_b_result"]
            ).validate_pass_b_result,
        ) as validate_b:
            _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
            )

        validate_b.assert_called_once()
        self.assertEqual(client.record_calls[0]["p_status"], "completed")

    def test_pass_c_uses_dedicated_validator(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        _wire_pass_c_prerequisite_tables(client)
        dispute = _SequenceProvider([_llm_response(_pass_c_normal_resolved())])

        with patch(
            "workers.ai_quality_audit_worker.validate_pass_c_result",
            wraps=__import__(
                "workers.ai_quality_audit_schemas", fromlist=["validate_pass_c_result"]
            ).validate_pass_c_result,
        ) as validate_c:
            _run_job(
                client,
                AiQualityAuditProviders(primary=_SequenceProvider([]), dispute=dispute),
            )

        validate_c.assert_called_once()
        self.assertEqual(client.record_calls[0]["p_status"], "completed")

    def test_legacy_validation_error_records_schema_invalid_not_running(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        class _LegacyRejectingProvider:
            def __call__(self, **kwargs):
                exc = LlmAuditValidationError("legacy rejection")
                exc.parsed_response = {"selected_option_labels": ["A"]}
                raise exc

        result = _run_job(
            client,
            AiQualityAuditProviders(
                primary=_LegacyRejectingProvider(),
                dispute=lambda **_: None,
            ),
        )

        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(len(client.record_calls), 1)
        self.assertEqual(client.record_calls[0]["p_status"], "schema_invalid")


class TestAiQualityAuditWaitCoordination(unittest.TestCase):

    def test_wait_causes_no_provider_call(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("WAIT"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        primary = _SequenceProvider([_llm_response(_pass_a_result())])

        with patch("workers.ai_quality_audit_worker.time.sleep") as sleep_mock:
            result = _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
                wait_poll_seconds=0.01,
            )

        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(len(primary.calls), 0)
        sleep_mock.assert_called_once()

    def test_wait_followed_by_executable_action_continues_safely(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("WAIT"),
            _claim("EXECUTE_PASS_A", "A"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        primary = _SequenceProvider([_llm_response(_pass_a_result())])

        with patch("workers.ai_quality_audit_worker.time.sleep"):
            result = _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
                wait_poll_seconds=0.01,
            )

        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(client.record_calls[0]["p_status"], "completed")

    def test_repeated_wait_is_bounded(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(*[_claim("WAIT")] * 3)

        with patch("workers.ai_quality_audit_worker.time.sleep"):
            with self.assertRaisesRegex(
                AiQualityAuditWorkerError,
                "exceeded bounded WAIT polling",
            ):
                _run_job(
                    client,
                    AiQualityAuditProviders(
                        primary=_SequenceProvider([]),
                        dispute=lambda **_: None,
                    ),
                    wait_poll_seconds=0.01,
                    max_wait_polls=2,
                )

    def test_wait_invokes_heartbeat_fn(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("WAIT"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        heartbeats: list[str] = []

        with patch("workers.ai_quality_audit_worker.time.sleep"):
            process_ai_quality_audit_job(
                client,
                _job_payload(),
                AiQualityAuditProviders(
                    primary=_SequenceProvider([]),
                    dispute=lambda **_: None,
                ),
                worker_id="test-worker",
                heartbeat_fn=lambda: heartbeats.append("beat"),
                wait_poll_seconds=0.01,
            )

        self.assertGreaterEqual(len(heartbeats), 2)


class TestPassBRetryFeedback(unittest.TestCase):

    def test_pass_b_retry_includes_prior_schema_error_in_provider_prompt(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_B", "B", is_retry=True),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        invalid_source_support = {
            "selected_option_labels": ["A"],
            "proposed_findings": [
                {
                    "finding_ref": "F2",
                    "finding_code": "SOURCE_SUPPORT_WEAK",
                    "finding_type": "source_support",
                    "severity": "medium",
                    "materiality": "warning",
                    "title": "Weak source support",
                    "description": "No supporting chunk.",
                    "evidence_chunk_ids": [],
                    "metadata": {},
                }
            ],
        }
        valid_source_support = {
            "selected_option_labels": ["A"],
            "proposed_findings": [
                {
                    "finding_ref": "F2",
                    "finding_code": "SOURCE_SUPPORT_WEAK",
                    "finding_type": "source_support",
                    "severity": "medium",
                    "materiality": "warning",
                    "title": "Weak source support",
                    "description": "No supporting chunk.",
                    "evidence_chunk_ids": [],
                    "metadata": {
                        "source_support_context": {
                            "attempted_retrieval": 2,
                            "evidence_limitation": "Frozen evidence did not substantiate the claim.",
                            "proposed_technical_claim": "The explanation overstates source support.",
                            "insufficiency_reason": "No frozen chunk directly supports the explanation.",
                        }
                    },
                }
            ],
        }

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_correctness_result()),
                _llm_response(invalid_source_support),
                _llm_response(_correctness_result()),
                _llm_response(valid_source_support),
            ]
        )

        with patch(
            "workers.ai_quality_audit_worker.build_pass_b_prompt",
            wraps=__import__(
                "workers.ai_quality_audit_prompts", fromlist=["build_pass_b_prompt"]
            ).build_pass_b_prompt,
        ) as build_b:
            _run_job(
                client,
                AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
            )

        self.assertEqual(len(primary.calls), 5)
        retry_kwargs = build_b.call_args_list[1].kwargs
        self.assertIn("source_support_context", retry_kwargs["retry_schema_errors"][0])
        pass_b_records = [
            call for call in client.record_calls if call["p_pass_code"] == "B"
        ]
        self.assertEqual(pass_b_records[0]["p_status"], "schema_invalid")
        self.assertEqual(pass_b_records[1]["p_status"], "completed")


class TestAiQualityAuditProviderTimeout(unittest.TestCase):

    def test_pass_a_primary_timeout_recorded_as_failed(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        primary = _HangingProvider()

        result = _run_job(client, _providers_with_timeout(primary))

        self.assertEqual(result["run_status"], "inconclusive")
        _assert_timeout_failure(self, client, primary, pass_code="A")

    def test_pass_b_primary_timeout_recorded_as_failed(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_B", "B"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        primary = _HangingProvider(response_factory=_pass_b_result)

        result = _run_job(client, _providers_with_timeout(primary))

        self.assertEqual(result["run_status"], "inconclusive")
        _assert_timeout_failure(self, client, primary, pass_code="B")

    def test_pass_c_dispute_timeout_recorded_as_failed(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        _wire_pass_c_prerequisite_tables(client)
        dispute = _HangingProvider(response_factory=_pass_c_normal_resolved)

        result = _run_job(
            client,
            _providers_with_timeout(_SequenceProvider([]), dispute=dispute),
        )

        self.assertEqual(result["run_status"], "inconclusive")
        _assert_timeout_failure(self, client, dispute, pass_code="C")


class TestPassBCompositeExecution(unittest.TestCase):
    """V60-IMPL-01: composite specialist + general-judge Pass B worker path."""

    def test_specialist_and_general_calls_execute_in_intended_order(self):
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
                _llm_response(_correctness_result()),
                _llm_response(_pass_b_result()),
            ]
        )

        _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        # calls[0] is Pass A; the two Pass B sub-calls must follow in order:
        # specialist (correctness_detector) strictly before the general
        # judge (general_quality_judge).
        self.assertEqual(len(primary.calls), 3)
        sub_calls = [
            (call.get("metadata") or {}).get("pass_b_sub_call") for call in primary.calls[1:]
        ]
        self.assertEqual(sub_calls, ["correctness_detector", "general_quality_judge"])

    def test_one_logical_pass_b_result_is_persisted(self):
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
                _llm_response(_correctness_result()),
                _llm_response(_pass_b_result()),
            ]
        )

        _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        pass_b_records = [
            call for call in client.record_calls if call["p_pass_code"] == "B"
        ]
        self.assertEqual(len(pass_b_records), 1)
        self.assertEqual(pass_b_records[0]["p_status"], "completed")

    def test_both_sub_call_telemetry_records_remain_visible(self):
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
                _llm_response(_correctness_result(), provider_request_id="req-correctness-1"),
                _llm_response(_pass_b_result(), provider_request_id="req-general-1"),
            ]
        )

        _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        pass_b_record = next(
            call for call in client.record_calls if call["p_pass_code"] == "B"
        )
        composite = pass_b_record["p_metadata"]["pass_b_composite"]
        self.assertIn("correctness_detector", composite)
        self.assertIn("general_quality_judge", composite)
        for label in ("correctness_detector", "general_quality_judge"):
            telemetry = composite[label]
            self.assertEqual(telemetry["status"], "completed")
            self.assertIsInstance(telemetry["duration_ms"], int)
            self.assertIn("provider_request_id", telemetry)
            self.assertIn("input_tokens", telemetry)
            self.assertIn("output_tokens", telemetry)
        self.assertEqual(
            composite["correctness_detector"]["provider_request_id"], "req-correctness-1"
        )
        self.assertEqual(
            composite["general_quality_judge"]["provider_request_id"], "req-general-1"
        )
        # Top-level RPC fields combine both sub-calls' token counts rather
        # than reporting only the general judge's.
        self.assertEqual(pass_b_record["p_input_tokens"], 200)
        self.assertEqual(pass_b_record["p_output_tokens"], 80)

    def test_pass_b_metadata_preserves_specialist_structured_output(self):
        from workers.ai_quality_audit_worker import build_pass_b_benchmark_telemetry

        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("SKIP_PASS_C", "C"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
        _wire_normal_no_dispute_tables(client)

        correctness_payload = _correctness_result()
        general_payload = _pass_b_result(findings=[_warning_finding()])
        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(correctness_payload),
                _llm_response(general_payload),
            ]
        )

        _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        pass_b_record = next(
            call for call in client.record_calls if call["p_pass_code"] == "B"
        )
        composite = pass_b_record["p_metadata"]["pass_b_composite"]
        specialist_structured = composite["correctness_detector"]["structured_output"]
        self.assertEqual(
            specialist_structured["evidence_sufficient_for_decision"],
            True,
        )
        self.assertEqual(len(specialist_structured["option_judgments"]), 2)
        for judgment in specialist_structured["option_judgments"]:
            self.assertIn(judgment["verdict"], {
                "SUPPORTED_AS_CORRECT",
                "NOT_SUPPORTED_AS_CORRECT",
                "INSUFFICIENT_EVIDENCE",
            })
            self.assertIn("option_label", judgment)
            self.assertIn("citation_chunk_ids", judgment)
            self.assertIn("evidence_rationale", judgment)

        general_structured = composite["general_quality_judge"]["structured_output"]
        self.assertEqual(
            general_structured["selected_option_labels"],
            general_payload["selected_option_labels"],
        )
        self.assertEqual(
            general_structured["proposed_findings"],
            general_payload["proposed_findings"],
        )

        artifact = build_pass_b_benchmark_telemetry(
            {
                "attempt_count": 1,
                "metadata": pass_b_record["p_metadata"],
            }
        )
        self.assertIn("correctness_specialist", artifact)
        self.assertIn("general_judge", artifact)
        self.assertEqual(
            artifact["correctness_specialist"]["structured_output"],
            specialist_structured,
        )
        self.assertEqual(
            artifact["general_judge"]["structured_output"],
            general_structured,
        )
        self.assertEqual(
            artifact["correctness_specialist"]["provider_call"]["attempt_count"],
            1,
        )

    def test_specialist_structured_output_preserved_when_general_judge_fails(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("NEEDS_DISPUTE_TRIGGER_B"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        _wire_normal_no_dispute_tables(client)

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_correctness_result()),
                _llm_response({"not_a_valid_field": True}),
                _llm_response(_correctness_result()),
                _llm_response({"not_a_valid_field": True}),
            ]
        )

        _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        pass_b_records = [
            call for call in client.record_calls if call["p_pass_code"] == "B"
        ]
        self.assertGreaterEqual(len(pass_b_records), 1)
        composite = pass_b_records[0]["p_metadata"]["pass_b_composite"]
        specialist = composite["correctness_detector"]
        self.assertEqual(specialist["status"], "completed")
        self.assertIn("structured_output", specialist)
        self.assertIn("option_judgments", specialist["structured_output"])
        general = composite["general_quality_judge"]
        self.assertEqual(general["status"], "schema_invalid")
        self.assertIn("errors", general["structured_output"])

    def test_specialist_blocking_finding_triggers_pass_c_routing(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_READY_TO_COMPLETE"),
        )
        _wire_resolved_dispute_tables(client)

        # Specialist disagrees with the stored key (supports "B" instead of
        # the stored-correct "A"), so derive_correctness_finding produces a
        # real WRONG_ANSWER_KEY finding -- the general judge itself proposes
        # nothing.
        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_correctness_result(supported=("B",), not_supported=("A",))),
                _llm_response(_pass_b_result()),
            ]
        )
        dispute = _SequenceProvider([_llm_response(_pass_c_normal_resolved())])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        self.assertEqual(result["passes_executed"], ["A", "B", "C"])
        self.assertEqual(len(client.persist_trigger_calls), 1)
        self.assertEqual(
            client.persist_trigger_calls[0]["p_reason_code"],
            "BLOCKING_DEFECT_PROPOSED",
        )
        pass_b_record = next(
            call for call in client.record_calls if call["p_pass_code"] == "B"
        )
        self.assertEqual(
            pass_b_record["p_result_json"]["proposed_findings"][0]["finding_code"],
            "WRONG_ANSWER_KEY",
        )

    def test_specialist_abstention_does_not_silently_auto_approve(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("EXECUTE_PASS_C", "C", model_name="test-dispute-model"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        client.set_table_response(
            "audit_run_dispute_triggers",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "reason_code": "BLOCKING_DEFECT_PROPOSED",
                    "source_pass_code": "B",
                    "trigger_reason": "Pass B proposed one or more blocking findings",
                    "finding_refs": ["FC1"],
                }
            ],
        )

        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response(_correctness_result_abstain()),
                _llm_response(_pass_b_result()),
            ]
        )
        dispute = _SequenceProvider([_llm_response(_pass_c_unresolved())])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        # Abstention must never silently auto-approve: it produces a real
        # blocking finding that routes through the existing dispute/Pass C
        # machinery, and an unresolved dispute keeps the run inconclusive
        # with zero confirmed findings -- never "completed".
        self.assertEqual(len(client.persist_trigger_calls), 1)
        self.assertEqual(
            client.persist_trigger_calls[0]["p_reason_code"],
            "BLOCKING_DEFECT_PROPOSED",
        )
        pass_b_record = next(
            call for call in client.record_calls if call["p_pass_code"] == "B"
        )
        proposed = pass_b_record["p_result_json"]["proposed_findings"]
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["finding_code"], "OTHER_REVIEW_NEEDED")
        self.assertEqual(proposed[0]["materiality"], "blocking")
        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(len(client.complete_calls), 0)

    def test_specialist_schema_invalid_fails_whole_pass_b_attempt(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_A", "A"),
            _claim("EXECUTE_PASS_B", "B"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )

        # Specialist response is missing option_judgments entirely --
        # schema-invalid -- so the whole Pass B attempt fails without ever
        # calling the general judge.
        primary = _SequenceProvider(
            [
                _llm_response(_pass_a_result()),
                _llm_response({"not_a_valid_field": True}),
            ]
        )

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=lambda **_: None),
        )

        self.assertEqual(result["run_status"], "inconclusive")
        self.assertEqual(len(primary.calls), 2)
        pass_b_record = next(
            call for call in client.record_calls if call["p_pass_code"] == "B"
        )
        self.assertEqual(pass_b_record["p_status"], "schema_invalid")
        composite = pass_b_record["p_metadata"]["pass_b_composite"]
        self.assertIn("correctness_detector", composite)
        self.assertNotIn("general_quality_judge", composite)
        self.assertEqual(composite["correctness_detector"]["status"], "schema_invalid")

    def test_specialist_provider_timeout_fails_whole_pass_b_attempt(self):
        client = OrchestrationFakeSupabase()
        client.enqueue_claims(
            _claim("EXECUTE_PASS_B", "B"),
            _claim("RUN_INCONCLUSIVE", run_status="inconclusive"),
        )
        primary = _HangingProvider(response_factory=_correctness_result)

        result = _run_job(
            client,
            _providers_with_timeout(primary),
        )

        self.assertEqual(result["run_status"], "inconclusive")
        # Only the specialist call is made; the timeout fails the whole
        # attempt before the general judge is ever invoked.
        _assert_timeout_failure(self, client, primary, pass_code="B")

    def test_pass_a_and_pass_c_behavior_remain_unchanged(self):
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
                _llm_response(_correctness_result()),
                _llm_response(_pass_b_result(findings=[_blocking_finding()])),
            ]
        )
        dispute = _SequenceProvider([_llm_response(_pass_c_normal_resolved())])

        result = _run_job(
            client,
            AiQualityAuditProviders(primary=primary, dispute=dispute),
        )

        # Pass A remains a single un-split call; Pass C remains a single
        # un-split dispute call, exactly as before V60.
        pass_a_calls = [
            call for call in client.record_calls if call["p_pass_code"] == "A"
        ]
        pass_c_calls = [
            call for call in client.record_calls if call["p_pass_code"] == "C"
        ]
        self.assertEqual(len(pass_a_calls), 1)
        self.assertEqual(len(pass_c_calls), 1)
        self.assertEqual(len(dispute.calls), 1)
        self.assertEqual(result["completion_shape"], "NORMAL_DISPUTE")


if __name__ == "__main__":
    unittest.main()
