"""
Tests for V48 AI quality audit context loaders.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_context import (
    BLIND_CONTEXT_RPC,
    COMPARISON_CONTEXT_RPC,
    AiQualityAuditContextError,
    load_blind_audit_context,
    load_comparison_audit_context,
)

_QVID = "cccccccc-0000-0000-0000-000000000001"
_RUN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_OTHER_RUN = "bbbbbbbb-0000-0000-0000-000000000002"
_OTHER_QVID = "dddddddd-0000-0000-0000-000000000002"
_CHUNK_1 = "11111111-1111-1111-1111-111111111111"
_CHUNK_2 = "22222222-2222-2222-2222-222222222222"
_RESOURCE_ID = "33333333-3333-3333-3333-333333333333"
_RESOURCE_VERSION_ID = "44444444-4444-4444-4444-444444444444"


class _FakeRpcResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


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
            rows = self._client._table_responses.get(self._table_name, [])
        if any(item[0] == "limit" for item in self._filters):
            limit = next(item[2] for item in self._filters if item[0] == "limit")
            rows = rows[: int(limit)]
        return _FakeRpcResult(rows)


class FakeSupabase:
    def __init__(self):
        self.rpc_calls: list[tuple[str, dict]] = []
        self._rpc_responses: dict[str, list] = {}
        self._rpc_errors: dict[str, str] = {}
        self._rpc_exceptions: dict[str, Exception] = {}
        self._table_responses: dict = {}
        self._table_errors: dict = {}

    def set_rpc_response(self, name: str, data: list):
        self._rpc_responses[name] = data
        self._rpc_errors.pop(name, None)
        self._rpc_exceptions.pop(name, None)

    def set_rpc_error(self, name: str, message: str):
        self._rpc_errors[name] = message
        self._rpc_responses.pop(name, None)

    def set_rpc_exception(self, name: str, exc: Exception):
        self._rpc_exceptions[name] = exc
        self._rpc_responses.pop(name, None)

    def set_table_response(self, table_name: str, data: list, *, key=None):
        self._table_responses[key or table_name] = data

    def set_table_error(self, key, message: str):
        self._table_errors[key] = message

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        if name in self._rpc_exceptions:
            raise self._rpc_exceptions[name]
        if name in self._rpc_errors:
            return _FakeRpcBuilder([], self._rpc_errors[name])
        return _FakeRpcBuilder(self._rpc_responses.get(name, []))

    def table(self, name: str):
        return _FakeTableQuery(self, name)


class _FakeRpcBuilder:
    def __init__(self, data, error=None):
        self._data = data
        self._error = error

    def execute(self):
        return _FakeRpcResult(self._data, self._error)


def _blind_row(**overrides) -> dict:
    row = {
        "question_version_id": _QVID,
        "question_id": 1067,
        "certification_exam_name": "ADM-201",
        "domain_name": "Configuration",
        "question_text": "Which feature enables this?",
        "question_type": "single",
        "select_count": 1,
        "options": [
            {"option_label": "A", "option_text": "Profiles", "display_order": 1},
            {"option_label": "B", "option_text": "Roles", "display_order": 2},
        ],
    }
    row.update(overrides)
    return row


def _comparison_row(**overrides) -> dict:
    row = _blind_row(
        explanation="Profiles control object permissions.",
        options=[
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
        stored_correct_option_labels=["A"],
    )
    row.update(overrides)
    return row


def _audit_run_row(**overrides) -> dict:
    row = {
        "id": _RUN_ID,
        "target_question_version_id": _QVID,
    }
    row.update(overrides)
    return row


def _pass_a_row(**overrides) -> dict:
    row = {
        "status": "completed",
        "result_json": {"selected_option_labels": ["A"]},
    }
    row.update(overrides)
    return row


def _evidence_set_rows(**overrides) -> list:
    rows = [
        {
            "audit_run_id": _RUN_ID,
            "resource_chunk_id": _CHUNK_2,
            "retrieval_rank": 2,
            "content_hash_at_execution": "hash-2",
            "metadata": {"role": "supporting"},
        },
        {
            "audit_run_id": _RUN_ID,
            "resource_chunk_id": _CHUNK_1,
            "retrieval_rank": 1,
            "content_hash_at_execution": "hash-1",
            "metadata": {},
        },
    ]
    if overrides:
        rows = overrides.get("rows", rows)
    return rows


def _resource_chunk_rows() -> list:
    return [
        {
            "id": _CHUNK_1,
            "chunk_text": "Profiles define default settings.",
            "resource_version_id": _RESOURCE_VERSION_ID,
            "resource_versions": {
                "resource_id": _RESOURCE_ID,
                "version_number": 1,
                "official_resources": {
                    "id": _RESOURCE_ID,
                    "title": "Salesforce Help",
                    "certification_exam_name": "ADM-201",
                },
            },
        },
        {
            "id": _CHUNK_2,
            "chunk_text": "Permission sets extend access.",
            "resource_version_id": _RESOURCE_VERSION_ID,
            "resource_versions": {
                "resource_id": _RESOURCE_ID,
                "version_number": 1,
                "official_resources": {
                    "id": _RESOURCE_ID,
                    "title": "Salesforce Help",
                    "certification_exam_name": "ADM-201",
                },
            },
        },
    ]


def _wire_comparison_tables(client: FakeSupabase):
    client.set_table_response("audit_runs", [_audit_run_row()])
    client.set_table_response("audit_run_pass_results", [_pass_a_row()])
    client.set_table_response("audit_run_evidence_set", _evidence_set_rows())
    client.set_table_response("resource_chunks", _resource_chunk_rows())


class TestBlindAuditContext(unittest.TestCase):

    def test_valid_result(self):
        client = FakeSupabase()
        client.set_rpc_response(BLIND_CONTEXT_RPC, [_blind_row()])
        result = load_blind_audit_context(client, _QVID)
        self.assertEqual(result["question_version_id"], _QVID)
        self.assertEqual(result["required_selection_count"], 1)
        self.assertEqual(result["options"][0]["option_label"], "A")
        self.assertNotIn("explanation", result)
        self.assertEqual(client.rpc_calls[0][0], BLIND_CONTEXT_RPC)

    def test_duplicate_option_labels(self):
        client = FakeSupabase()
        client.set_rpc_response(
            BLIND_CONTEXT_RPC,
            [
                _blind_row(
                    options=[
                        {"option_label": "A", "option_text": "One", "display_order": 1},
                        {"option_label": "A", "option_text": "Two", "display_order": 2},
                    ]
                )
            ],
        )
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "duplicate option_label",
        ):
            load_blind_audit_context(client, _QVID)

    def test_invalid_selection_count(self):
        client = FakeSupabase()
        client.set_rpc_response(BLIND_CONTEXT_RPC, [_blind_row(select_count=0)])
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "select_count must be a positive integer",
        ):
            load_blind_audit_context(client, _QVID)

    def test_missing_identifier(self):
        client = FakeSupabase()
        row = _blind_row()
        del row["question_id"]
        client.set_rpc_response(BLIND_CONTEXT_RPC, [row])
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "question_id",
        ):
            load_blind_audit_context(client, _QVID)

    def test_answer_key_leakage_rejected(self):
        client = FakeSupabase()
        client.set_rpc_response(
            BLIND_CONTEXT_RPC,
            [_blind_row(stored_correct_option_labels=["A"])],
        )
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "leaked forbidden field 'stored_correct_option_labels'",
        ):
            load_blind_audit_context(client, _QVID)

    def test_evidence_leakage_rejected(self):
        client = FakeSupabase()
        client.set_rpc_response(
            BLIND_CONTEXT_RPC,
            [_blind_row(frozen_evidence=[{"chunk_id": _CHUNK_1}])],
        )
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "leaked forbidden field 'frozen_evidence'",
        ):
            load_blind_audit_context(client, _QVID)

    def test_malformed_top_level_payload(self):
        client = FakeSupabase()
        client.set_rpc_response(BLIND_CONTEXT_RPC, [])
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "returned no rows",
        ):
            load_blind_audit_context(client, _QVID)


class TestComparisonAuditContext(unittest.TestCase):

    def test_valid_deterministic_evidence_ordering(self):
        client = FakeSupabase()
        client.set_rpc_response(COMPARISON_CONTEXT_RPC, [_comparison_row()])
        _wire_comparison_tables(client)

        result = load_comparison_audit_context(client, _QVID, _RUN_ID)

        self.assertEqual(result["audit_run_id"], _RUN_ID)
        self.assertEqual(result["stored_correct_option_labels"], ["A"])
        self.assertEqual(result["pass_a_selected_option_labels"], ["A"])
        ranks = [item["rank"] for item in result["frozen_evidence"]]
        chunk_ids = [item["chunk_id"] for item in result["frozen_evidence"]]
        self.assertEqual(ranks, [1, 2])
        self.assertEqual(chunk_ids, [_CHUNK_1, _CHUNK_2])
        self.assertEqual(result["frozen_evidence"][0]["authoritative_hash"], "hash-1")

    def test_unknown_correct_option_label(self):
        client = FakeSupabase()
        client.set_rpc_response(
            COMPARISON_CONTEXT_RPC,
            [_comparison_row(stored_correct_option_labels=["Z"])],
        )
        _wire_comparison_tables(client)
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "is not present in comparison options",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_duplicate_correct_option(self):
        client = FakeSupabase()
        client.set_rpc_response(
            COMPARISON_CONTEXT_RPC,
            [_comparison_row(stored_correct_option_labels=["A", "A"])],
        )
        _wire_comparison_tables(client)
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "duplicate option label",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_duplicate_chunk_id(self):
        client = FakeSupabase()
        client.set_rpc_response(COMPARISON_CONTEXT_RPC, [_comparison_row()])
        _wire_comparison_tables(client)
        client.set_table_response(
            "audit_run_evidence_set",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "resource_chunk_id": _CHUNK_1,
                    "retrieval_rank": 1,
                    "content_hash_at_execution": "hash-1",
                    "metadata": {},
                },
                {
                    "audit_run_id": _RUN_ID,
                    "resource_chunk_id": _CHUNK_1,
                    "retrieval_rank": 2,
                    "content_hash_at_execution": "hash-2",
                    "metadata": {},
                },
            ],
        )
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "duplicate chunk_id",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_duplicate_rank(self):
        client = FakeSupabase()
        client.set_rpc_response(COMPARISON_CONTEXT_RPC, [_comparison_row()])
        _wire_comparison_tables(client)
        client.set_table_response(
            "audit_run_evidence_set",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "resource_chunk_id": _CHUNK_1,
                    "retrieval_rank": 1,
                    "content_hash_at_execution": "hash-1",
                    "metadata": {},
                },
                {
                    "audit_run_id": _RUN_ID,
                    "resource_chunk_id": _CHUNK_2,
                    "retrieval_rank": 1,
                    "content_hash_at_execution": "hash-2",
                    "metadata": {},
                },
            ],
        )
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "duplicate rank",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_nonpositive_rank(self):
        client = FakeSupabase()
        client.set_rpc_response(COMPARISON_CONTEXT_RPC, [_comparison_row()])
        _wire_comparison_tables(client)
        client.set_table_response(
            "audit_run_evidence_set",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "resource_chunk_id": _CHUNK_1,
                    "retrieval_rank": 0,
                    "content_hash_at_execution": "hash-1",
                    "metadata": {},
                }
            ],
        )
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "retrieval_rank must be a positive integer",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_mismatched_run_id(self):
        client = FakeSupabase()
        client.set_rpc_response(COMPARISON_CONTEXT_RPC, [_comparison_row()])
        client.set_table_response("audit_runs", [_audit_run_row()])
        client.set_table_response("audit_run_pass_results", [_pass_a_row()])
        client.set_table_response(
            "audit_run_evidence_set",
            [
                {
                    "audit_run_id": _OTHER_RUN,
                    "resource_chunk_id": _CHUNK_1,
                    "retrieval_rank": 1,
                    "content_hash_at_execution": "hash-1",
                    "metadata": {},
                }
            ],
        )
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            f"belongs to audit run {_OTHER_RUN!r}, expected {_RUN_ID!r}",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_mismatched_question_version(self):
        client = FakeSupabase()
        client.set_rpc_response(
            COMPARISON_CONTEXT_RPC,
            [_comparison_row(question_version_id=_OTHER_QVID)],
        )
        client.set_table_response(
            "audit_runs",
            [_audit_run_row(target_question_version_id=_QVID)],
        )
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "does not match requested",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_missing_authoritative_hash(self):
        client = FakeSupabase()
        client.set_rpc_response(COMPARISON_CONTEXT_RPC, [_comparison_row()])
        _wire_comparison_tables(client)
        client.set_table_response(
            "audit_run_evidence_set",
            [
                {
                    "audit_run_id": _RUN_ID,
                    "resource_chunk_id": _CHUNK_1,
                    "retrieval_rank": 1,
                    "content_hash_at_execution": "   ",
                    "metadata": {},
                }
            ],
        )
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "content_hash_at_execution must not be empty",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_malformed_evidence_row(self):
        client = FakeSupabase()
        client.set_rpc_response(COMPARISON_CONTEXT_RPC, [_comparison_row()])
        _wire_comparison_tables(client)
        broken_chunks = _resource_chunk_rows()
        broken_chunks[0] = dict(broken_chunks[0])
        broken_chunks[0]["chunk_text"] = "   "
        client.set_table_response("resource_chunks", broken_chunks)
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "chunk_text must not be empty",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_database_rpc_error_translated(self):
        client = FakeSupabase()
        client.set_rpc_error(COMPARISON_CONTEXT_RPC, "comparison context denied")
        client.set_table_response("audit_runs", [_audit_run_row()])
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "RPC 'get_question_version_comparison_context_v1' failed",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)

    def test_database_exception_translated(self):
        client = FakeSupabase()
        client.set_rpc_exception(COMPARISON_CONTEXT_RPC, RuntimeError("network down"))
        client.set_table_response("audit_runs", [_audit_run_row()])
        with self.assertRaisesRegex(
            AiQualityAuditContextError,
            "call failed: network down",
        ):
            load_comparison_audit_context(client, _QVID, _RUN_ID)


if __name__ == "__main__":
    unittest.main()
