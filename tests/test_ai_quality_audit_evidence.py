"""
Tests for V48 smoke evidence retrieval, question ranking, and hash canonicalization.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_evidence import (
    RETRIEVAL_METHOD,
    AiQualityAuditEvidenceError,
    CANDIDATE_POOL_MAX,
    compute_evidence_set_hash,
    empty_evidence_set_hash,
    prepare_smoke_evidence_set,
    rank_question_evidence_candidates,
    score_evidence_candidate,
)


class TestEvidenceSetHash(unittest.TestCase):

    def test_empty_hash_matches_canonical_array(self):
        self.assertEqual(
            empty_evidence_set_hash(),
            compute_evidence_set_hash([]),
        )

    def test_ranked_hash_is_deterministic(self):
        rows = [
            {
                "retrieval_rank": 2,
                "resource_chunk_id": "22222222-2222-2222-2222-222222222222",
                "content_hash": "b" * 64,
            },
            {
                "retrieval_rank": 1,
                "resource_chunk_id": "11111111-1111-1111-1111-111111111111",
                "content_hash": "a" * 64,
            },
        ]
        first = compute_evidence_set_hash(rows)
        second = compute_evidence_set_hash(list(reversed(rows)))
        self.assertEqual(first, second)


class TestQuestionEvidenceRanking(unittest.TestCase):

    def setUp(self):
        self.blind_context = {
            "question_text": "Which Salesforce feature enables profile-based defaults?",
            "domain_name": "Configuration",
            "options": [
                {"option_label": "A", "option_text": "Profiles", "display_order": 1},
                {"option_label": "B", "option_text": "Roles", "display_order": 2},
            ],
        }
        self.resource_a = "aaaaaaaa-0000-0000-0000-000000000001"
        self.resource_b = "bbbbbbbb-0000-0000-0000-000000000002"
        self.chunk_relevant = "11111111-1111-1111-1111-111111111111"
        self.chunk_unrelated = "22222222-2222-2222-2222-222222222222"

    def _candidates(self):
        return [
            {
                "resource_chunk_id": self.chunk_unrelated,
                "resource_id": self.resource_b,
                "content_hash": "b" * 64,
                "chunk_index": 0,
                "certification_exam_name": "ADM-201",
                "chunk_text": "Billing rules and invoice schedules for revenue cloud.",
                "title": "Billing Overview",
            },
            {
                "resource_chunk_id": self.chunk_relevant,
                "resource_id": self.resource_a,
                "content_hash": "a" * 64,
                "chunk_index": 0,
                "certification_exam_name": "ADM-201",
                "chunk_text": "Profiles define default settings and object permissions.",
                "title": "Profiles Help",
            },
        ]

    def test_relevant_chunk_ranks_above_unrelated_chunk(self):
        ranked, previews = rank_question_evidence_candidates(
            self._candidates(),
            blind_context=self.blind_context,
            resource_by_id={
                self.resource_a: {
                    "id": self.resource_a,
                    "title": "Profiles Help",
                    "metadata": {"domain": "Configuration"},
                },
                self.resource_b: {
                    "id": self.resource_b,
                    "title": "Billing Overview",
                    "metadata": {"domain": "Billing"},
                },
            },
            max_chunks=2,
            max_characters=10_000,
        )

        self.assertEqual(ranked[0]["resource_chunk_id"], self.chunk_relevant)
        self.assertGreater(
            ranked[0]["relevance_score"],
            ranked[1]["relevance_score"],
        )
        self.assertEqual(previews[0]["title"], "Profiles Help")

    def test_domain_match_is_preferred(self):
        domain_match = score_evidence_candidate(
            {
                "chunk_text": "General admin overview.",
                "title": "Admin Guide",
            },
            query_text="Profiles configuration defaults",
            query_tokens=("profiles", "configuration", "defaults"),
            question_domain="Configuration",
            resource_metadata={"domain": "Configuration"},
            resource_title="Admin Guide",
        )
        domain_miss = score_evidence_candidate(
            {
                "chunk_text": "General admin overview.",
                "title": "Admin Guide",
            },
            query_text="Profiles configuration defaults",
            query_tokens=("profiles", "configuration", "defaults"),
            question_domain="Configuration",
            resource_metadata={"domain": "Billing"},
            resource_title="Admin Guide",
        )
        self.assertGreater(domain_match, domain_miss)

    def test_top_k_limit_is_enforced(self):
        many_candidates = []
        for index in range(5):
            many_candidates.append(
                {
                    "resource_chunk_id": f"{index:012x}-1111-1111-1111-111111111111",
                    "resource_id": f"{index:012x}-0000-0000-0000-000000000001",
                    "content_hash": "a" * 64,
                    "chunk_index": 0,
                    "certification_exam_name": "ADM-201",
                    "chunk_text": f"Profiles topic variant {index}",
                    "title": f"Profiles {index}",
                }
            )

        ranked, _ = rank_question_evidence_candidates(
            many_candidates,
            blind_context=self.blind_context,
            resource_by_id={},
            max_chunks=2,
            max_characters=10_000,
        )
        self.assertEqual(len(ranked), 2)

    def test_character_budget_is_enforced(self):
        budgeted_candidates = [
            {
                "resource_chunk_id": self.chunk_relevant,
                "resource_id": self.resource_a,
                "content_hash": "a" * 64,
                "chunk_index": 0,
                "certification_exam_name": "ADM-201",
                "chunk_text": "Profiles define defaults.",
                "title": "Profiles Help",
            },
            {
                "resource_chunk_id": self.chunk_unrelated,
                "resource_id": self.resource_b,
                "content_hash": "b" * 64,
                "chunk_index": 0,
                "certification_exam_name": "ADM-201",
                "chunk_text": "Billing rules and invoice schedules.",
                "title": "Billing Overview",
            },
        ]
        ranked, _ = rank_question_evidence_candidates(
            budgeted_candidates,
            blind_context=self.blind_context,
            resource_by_id={},
            max_chunks=2,
            max_characters=30,
        )
        total_chars = sum(len(item["chunk_text"]) for item in ranked)
        self.assertEqual(len(ranked), 1)
        self.assertLessEqual(total_chars, 30)

    def test_equal_scores_use_deterministic_tie_break(self):
        tied_candidates = [
            {
                "resource_chunk_id": self.chunk_unrelated,
                "resource_id": self.resource_b,
                "content_hash": "b" * 64,
                "chunk_index": 0,
                "certification_exam_name": "ADM-201",
                "chunk_text": "Same text",
                "title": "Same",
            },
            {
                "resource_chunk_id": self.chunk_relevant,
                "resource_id": self.resource_a,
                "content_hash": "a" * 64,
                "chunk_index": 0,
                "certification_exam_name": "ADM-201",
                "chunk_text": "Same text",
                "title": "Same",
            },
        ]
        first_ranked, _ = rank_question_evidence_candidates(
            tied_candidates,
            blind_context=self.blind_context,
            resource_by_id={},
            max_chunks=2,
            max_characters=10_000,
        )
        second_ranked, _ = rank_question_evidence_candidates(
            list(reversed(tied_candidates)),
            blind_context=self.blind_context,
            resource_by_id={},
            max_chunks=2,
            max_characters=10_000,
        )
        self.assertEqual(
            [item["resource_chunk_id"] for item in first_ranked],
            [item["resource_chunk_id"] for item in second_ranked],
        )


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
    def __init__(self, client, table_name):
        self._client = client
        self._table_name = table_name
        self._filters = []

    def select(self, _fields):
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def execute(self):
        rows = list(self._client._tables.get(self._table_name, []))
        for op, field, value in self._filters:
            if op == "eq":
                rows = [row for row in rows if row.get(field) == value]
        return _FakeRpcResult(rows)


class EvidenceFakeClient:
    def __init__(self):
        self._tables = {}
        self.rpc_calls = []

    def set_table(self, name, rows):
        self._tables[name] = list(rows)

    def table(self, name):
        return _FakeTableQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        if name == "get_question_version_blind_context_v1":
            return _FakeRpcBuilder(
                [
                    {
                        "question_version_id": params["p_question_version_id"],
                        "question_id": 1,
                        "certification_exam_name": "ADM-201",
                        "domain_name": "Configuration",
                        "question_text": "Which feature enables profile-based defaults?",
                        "question_type": "single",
                        "select_count": 1,
                        "options": [
                            {
                                "option_label": "A",
                                "option_text": "Profiles",
                                "display_order": 1,
                            }
                        ],
                    }
                ]
            )
        if name == "list_audit_candidate_resource_chunks_v1":
            return _FakeRpcBuilder(self._candidate_rows)
        raise AssertionError(f"unexpected rpc {name!r}")


class TestPrepareSmokeEvidenceSet(unittest.TestCase):

    def setUp(self):
        self.client = EvidenceFakeClient()
        self.qvid = "cccccccc-0000-0000-0000-000000000001"
        self.resource_id = "aaaaaaaa-0000-0000-0000-000000000001"
        self.chunk_id = "11111111-1111-1111-1111-111111111111"
        self.client.set_table(
            "official_resources",
            [
                {
                    "id": self.resource_id,
                    "certification_exam_name": "ADM-201",
                    "title": "Profiles Help",
                    "metadata": {"domain": "Configuration"},
                    "is_active": True,
                }
            ],
        )
        self.client._candidate_rows = [
            {
                "resource_chunk_id": self.chunk_id,
                "resource_id": self.resource_id,
                "resource_version_id": "bbbbbbbb-0000-0000-0000-000000000001",
                "resource_version_number": 1,
                "certification_exam_name": "ADM-201",
                "resource_type": "official_documentation",
                "title": "Profiles Help",
                "canonical_url": "https://example.com",
                "chunk_index": 0,
                "content_hash": "a" * 64,
                "chunk_text": "Profiles define default settings for users.",
            }
        ]

    def test_prepares_ranked_evidence_and_hash(self):
        prepared = prepare_smoke_evidence_set(self.client, self.qvid)

        self.assertEqual(len(prepared.evidence_chunks), 1)
        self.assertEqual(prepared.evidence_chunks[0]["retrieval_rank"], 1)
        self.assertEqual(
            prepared.evidence_chunks[0]["resource_chunk_id"],
            self.chunk_id,
        )
        self.assertEqual(len(prepared.evidence_set_hash), 64)
        self.assertEqual(prepared.retrieval_method, RETRIEVAL_METHOD)
        self.assertGreater(prepared.total_evidence_characters, 0)
        self.assertGreater(prepared.estimated_tokens, 0)
        summary = prepared.to_summary_dict()
        self.assertEqual(summary["selected_chunk_ids"], [self.chunk_id.lower()])
        self.assertEqual(summary["source_titles"], ["Profiles Help"])
        candidate_call = self.client.rpc_calls[1]
        self.assertEqual(candidate_call[0], "list_audit_candidate_resource_chunks_v1")
        self.assertEqual(candidate_call[1]["p_max_chunks"], CANDIDATE_POOL_MAX)

    def test_refuses_empty_evidence_by_default(self):
        self.client._candidate_rows = []
        with self.assertRaisesRegex(
            AiQualityAuditEvidenceError,
            "evidence retrieval returned zero chunks",
        ):
            prepare_smoke_evidence_set(self.client, self.qvid)

    def test_rejects_certification_mismatch_in_candidates(self):
        self.client._candidate_rows[0]["certification_exam_name"] = "OTHER-EXAM"
        with self.assertRaisesRegex(AiQualityAuditEvidenceError, "certification mismatch"):
            prepare_smoke_evidence_set(self.client, self.qvid)


if __name__ == "__main__":
    unittest.main()
