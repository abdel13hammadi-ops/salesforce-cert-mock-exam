"""
Tests for V48 smoke evidence retrieval, precision ranking, and hash canonicalization.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_evidence import (
    DEFAULT_MAX_CHUNKS_PER_RESOURCE,
    DEFAULT_MAX_EVIDENCE_CHARACTERS,
    DEFAULT_MAX_EVIDENCE_CHUNKS,
    RETRIEVAL_METHOD,
    AiQualityAuditEvidenceError,
    CANDIDATE_POOL_MAX,
    compute_evidence_set_hash,
    empty_evidence_set_hash,
    prepare_smoke_evidence_set,
    rank_question_evidence_candidates,
    score_evidence_candidate,
)


def _chunk(
    *,
    chunk_id: str,
    resource_id: str,
    title: str,
    chunk_text: str,
    chunk_index: int = 0,
    resource_type: str = "official_documentation",
    content_hash: str = "a" * 64,
) -> dict:
    return {
        "resource_chunk_id": chunk_id,
        "resource_id": resource_id,
        "content_hash": content_hash,
        "chunk_index": chunk_index,
        "certification_exam_name": "CERT-TEST",
        "chunk_text": chunk_text,
        "title": title,
        "resource_type": resource_type,
    }


def _rank(
    blind_context: dict,
    candidates: list[dict],
    *,
    resource_by_id: dict | None = None,
    max_chunks: int = DEFAULT_MAX_EVIDENCE_CHUNKS,
    max_characters: int = DEFAULT_MAX_EVIDENCE_CHARACTERS,
    max_chunks_per_resource: int = DEFAULT_MAX_CHUNKS_PER_RESOURCE,
):
    return rank_question_evidence_candidates(
        candidates,
        blind_context=blind_context,
        resource_by_id=resource_by_id or {},
        max_chunks=max_chunks,
        max_characters=max_characters,
        max_chunks_per_resource=max_chunks_per_resource,
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


class TestPrecisionRegressionScenarios(unittest.TestCase):

    def test_cross_filter_question_excludes_unrelated_relationship_chunks(self):
        blind = {
            "question_text": "Which dashboard component lets users filter one chart by selecting another?",
            "domain_name": "Reports and Dashboards",
            "options": [
                {"option_text": "Cross filter", "option_label": "A"},
                {"option_text": "Bucket field", "option_label": "B"},
            ],
        }
        cross = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Dashboard Cross Filters",
            chunk_text="Use cross filters so selecting a dashboard component filters other components.",
        )
        relationship = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Object Relationships",
            chunk_text="Master-detail and lookup relationships define how records relate.",
        )
        ranked, _, qualified, rejected = _rank(blind, [relationship, cross])
        selected_ids = {item["resource_chunk_id"] for item in ranked}
        self.assertIn(cross["resource_chunk_id"], selected_ids)
        self.assertNotIn(relationship["resource_chunk_id"], selected_ids)
        self.assertGreater(rejected, 0)

    def test_notification_flow_question_prefers_notification_evidence(self):
        blind = {
            "question_text": "Which automation sends email alerts when an opportunity closes?",
            "domain_name": "Automation",
            "options": [
                {"option_text": "Email alert", "option_label": "A"},
                {"option_text": "Approval process", "option_label": "B"},
            ],
        }
        notification = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Email Alerts",
            chunk_text="Email alerts notify users when records meet defined criteria.",
        )
        unrelated = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Approval Processes",
            chunk_text="Approval processes route records for manager approval.",
        )
        ranked, _, _, _ = _rank(blind, [unrelated, notification])
        self.assertEqual(ranked[0]["resource_chunk_id"], notification["resource_chunk_id"])

    def test_record_triggered_flow_question_prefers_flow_evidence(self):
        blind = {
            "question_text": "Which tool launches when a record is created or updated?",
            "domain_name": "Automation",
            "options": [
                {"option_text": "Record-triggered flow", "option_label": "A"},
                {"option_text": "Report type", "option_label": "B"},
            ],
        }
        flow = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Record-Triggered Flows",
            chunk_text="Record-triggered flows start when a record is created or updated.",
        )
        report = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Custom Report Types",
            chunk_text="Custom report types define report fields and relationships.",
        )
        ranked, _, _, _ = _rank(blind, [report, flow])
        self.assertEqual(ranked[0]["resource_chunk_id"], flow["resource_chunk_id"])

    def test_recycle_bin_question_excludes_relationship_chunks(self):
        blind = {
            "question_text": "How long do deleted records remain in the Recycle Bin?",
            "domain_name": "Data Management",
            "options": [
                {"option_text": "15 days", "option_label": "A"},
                {"option_text": "30 days", "option_label": "B"},
            ],
        }
        recycle = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Recycle Bin",
            chunk_text="Deleted records remain in the Recycle Bin for 15 days before permanent deletion.",
        )
        relationship = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Object Relationships",
            chunk_text="Lookup relationships connect parent and child records.",
        )
        ranked, _, _, rejected = _rank(blind, [relationship, recycle])
        self.assertEqual(ranked[0]["resource_chunk_id"], recycle["resource_chunk_id"])
        self.assertGreater(rejected, 0)

    def test_user_story_question_prefers_user_story_evidence(self):
        blind = {
            "question_text": "Which artifact captures user needs in Agile projects?",
            "domain_name": "Agile",
            "options": [
                {"option_text": "User story", "option_label": "A"},
                {"option_text": "Gantt chart", "option_label": "B"},
            ],
        }
        story = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="User Stories",
            chunk_text="A user story captures a user need and acceptance criteria for delivery.",
        )
        scope = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Project Scope Statement",
            chunk_text="Project scope defines boundaries, deliverables, and exclusions.",
        )
        ranked, _, _, _ = _rank(blind, [scope, story])
        self.assertEqual(ranked[0]["resource_chunk_id"], story["resource_chunk_id"])

    def test_project_scope_question_prefers_scope_evidence(self):
        blind = {
            "question_text": "Which document defines project boundaries and deliverables?",
            "domain_name": "Business Analysis",
            "options": [
                {"option_text": "Project scope statement", "option_label": "A"},
                {"option_text": "Process map", "option_label": "B"},
            ],
        }
        scope = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Project Scope Statement",
            chunk_text="The project scope statement defines boundaries, deliverables, and exclusions.",
        )
        process = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Process Mapping",
            chunk_text="Process maps visualize steps, actors, and handoffs.",
        )
        ranked, _, _, _ = _rank(blind, [process, scope])
        self.assertEqual(ranked[0]["resource_chunk_id"], scope["resource_chunk_id"])

    def test_process_map_question_prefers_process_mapping_evidence(self):
        blind = {
            "question_text": "Which technique visualizes steps, actors, and handoffs?",
            "domain_name": "Business Analysis",
            "options": [
                {"option_text": "Process map", "option_label": "A"},
                {"option_text": "User story", "option_label": "B"},
            ],
        }
        process = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Process Mapping",
            chunk_text="Process maps visualize steps, actors, and handoffs in a business process.",
        )
        story = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="User Stories",
            chunk_text="User stories capture user needs for agile delivery.",
        )
        ranked, _, _, _ = _rank(blind, [story, process])
        self.assertEqual(ranked[0]["resource_chunk_id"], process["resource_chunk_id"])

    def test_stakeholder_grid_excludes_weak_broad_ba_material(self):
        blind = {
            "question_text": "Which grid plots stakeholder power against interest?",
            "domain_name": "Stakeholder Engagement",
            "options": [
                {"option_text": "Power/Interest Grid", "option_label": "A"},
                {"option_text": "RACI matrix", "option_label": "B"},
            ],
        }
        weak = [
            _chunk(
                chunk_id=f"{index:012x}-2222-2222-2222-222222222222",
                resource_id=f"{index:012x}-0000-0000-0000-000000000002",
                title=title,
                chunk_text=text,
            )
            for index, title, text in (
                (1, "Project Scope Statement", "Project scope defines boundaries and deliverables."),
                (2, "User Stories", "User stories capture user needs for agile delivery."),
                (3, "Process Mapping", "Process maps visualize steps and handoffs."),
            )
        ]
        ranked, _, qualified, _ = _rank(blind, weak)
        self.assertEqual(ranked, [])
        self.assertEqual(qualified, 0)

    def test_stakeholder_grid_reports_evidence_gap_when_only_weak_sources_exist(self):
        blind = {
            "question_text": "Which grid plots stakeholder power against interest?",
            "domain_name": "Stakeholder Engagement",
            "options": [{"option_text": "Power/Interest Grid", "option_label": "A"}],
        }
        candidates = [
            _chunk(
                chunk_id="22222222-2222-2222-2222-222222222222",
                resource_id="bbbbbbbb-0000-0000-0000-000000000002",
                title="Business Analysis Overview",
                chunk_text="Business analysis identifies needs and recommends solutions.",
            )
        ]
        ranked, _, qualified, rejected = _rank(blind, candidates)
        self.assertEqual(ranked, [])
        self.assertEqual(qualified, 0)
        self.assertEqual(rejected, 1)


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
            _chunk(
                chunk_id=self.chunk_unrelated,
                resource_id=self.resource_b,
                title="Billing Overview",
                chunk_text="Billing rules and invoice schedules for revenue cloud.",
            ),
            _chunk(
                chunk_id=self.chunk_relevant,
                resource_id=self.resource_a,
                title="Profiles Help",
                chunk_text="Profiles define default settings and object permissions.",
            ),
        ]

    def test_relevant_chunk_ranks_above_unrelated_chunk(self):
        ranked, previews, qualified, rejected = _rank(
            self.blind_context,
            self._candidates(),
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
        )

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["resource_chunk_id"], self.chunk_relevant)
        self.assertEqual(qualified, 1)
        self.assertEqual(rejected, 1)
        self.assertEqual(previews[0]["title"], "Profiles Help")
        self.assertTrue(previews[0]["match_reasons"])

    def test_domain_match_is_preferred(self):
        shared_chunk = {
            "chunk_text": "Platform administration overview for administrators.",
            "title": "Administration Guide",
        }
        domain_match = score_evidence_candidate(
            shared_chunk,
            query_text="Profiles configuration defaults",
            query_tokens=("profiles", "configuration", "defaults"),
            question_tokens=("profiles", "configuration", "defaults"),
            option_tokens=("profiles",),
            question_domain="Configuration",
            resource_metadata={"domain": "Configuration"},
            resource_title="Administration Guide",
        )
        domain_miss = score_evidence_candidate(
            shared_chunk,
            query_text="Profiles configuration defaults",
            query_tokens=("profiles", "configuration", "defaults"),
            question_tokens=("profiles", "configuration", "defaults"),
            option_tokens=("profiles",),
            question_domain="Configuration",
            resource_metadata={"domain": "Billing"},
            resource_title="Administration Guide",
        )
        self.assertTrue(domain_match.qualifies)
        self.assertFalse(domain_miss.qualifies)

    def test_top_k_limit_is_enforced_without_filling_weak_candidates(self):
        many_candidates = []
        for index in range(12):
            many_candidates.append(
                _chunk(
                    chunk_id=f"{index:012x}-1111-1111-1111-111111111111",
                    resource_id=f"{index:012x}-0000-0000-0000-000000000001",
                    title=f"Profiles Guide {index}",
                    chunk_text=(
                        f"Profiles define profile-based defaults and configuration settings "
                        f"section {index}."
                    ),
                )
            )

        ranked, _, qualified, _ = _rank(
            self.blind_context,
            many_candidates,
            max_chunks=DEFAULT_MAX_EVIDENCE_CHUNKS,
        )
        self.assertLessEqual(len(ranked), DEFAULT_MAX_EVIDENCE_CHUNKS)
        self.assertLess(len(ranked), len(many_candidates))
        self.assertGreater(qualified, len(ranked))

    def test_character_budget_is_enforced(self):
        budgeted_candidates = [
            _chunk(
                chunk_id=self.chunk_relevant,
                resource_id=self.resource_a,
                title="Profiles Help",
                chunk_text="Profiles define defaults.",
            ),
            _chunk(
                chunk_id=self.chunk_unrelated,
                resource_id=self.resource_b,
                title="Profiles Extended Guide",
                chunk_text="Profiles define defaults and permissions in detail.",
            ),
        ]
        ranked, _, _, _ = _rank(
            self.blind_context,
            budgeted_candidates,
            max_chunks=2,
            max_characters=30,
        )
        total_chars = sum(len(item["chunk_text"]) for item in ranked)
        self.assertEqual(len(ranked), 1)
        self.assertLessEqual(total_chars, 30)

    def test_per_resource_cap_is_enforced(self):
        same_resource = self.resource_a
        candidates = [
            _chunk(
                chunk_id=f"{index:012x}-1111-1111-1111-111111111111",
                resource_id=same_resource,
                title="Profiles Help",
                chunk_text=f"Profiles define defaults section {index}.",
                chunk_index=index,
            )
            for index in range(4)
        ]
        ranked, _, _, _ = _rank(
            self.blind_context,
            candidates,
            max_chunks=4,
            max_chunks_per_resource=2,
        )
        self.assertEqual(len(ranked), 2)

    def test_equal_scores_use_deterministic_tie_break(self):
        tied_candidates = [
            _chunk(
                chunk_id=self.chunk_unrelated,
                resource_id=self.resource_b,
                title="Profiles Guide",
                chunk_text="Profiles define default settings and permissions.",
            ),
            _chunk(
                chunk_id=self.chunk_relevant,
                resource_id=self.resource_a,
                title="Profiles Guide",
                chunk_text="Profiles define default settings and permissions.",
            ),
        ]
        first_ranked, _, _, _ = _rank(self.blind_context, tied_candidates, max_chunks=2)
        second_ranked, _, _, _ = _rank(
            self.blind_context,
            list(reversed(tied_candidates)),
            max_chunks=2,
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
                    "resource_type": "official_documentation",
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
        self.assertEqual(prepared.candidate_count, 1)
        self.assertEqual(prepared.qualified_candidate_count, 1)
        self.assertEqual(prepared.selected_count, 1)
        summary = prepared.to_summary_dict()
        self.assertEqual(summary["selected_chunk_ids"], [self.chunk_id.lower()])
        self.assertEqual(summary["source_titles"], ["Profiles Help"])
        self.assertIn("match_reasons", summary["chunk_previews"][0])
        candidate_call = self.client.rpc_calls[1]
        self.assertEqual(candidate_call[0], "list_audit_candidate_resource_chunks_v1")
        self.assertEqual(candidate_call[1]["p_max_chunks"], CANDIDATE_POOL_MAX)

    def test_refuses_empty_evidence_by_default(self):
        self.client._candidate_rows = []
        with self.assertRaisesRegex(
            AiQualityAuditEvidenceError,
            "zero qualified chunks",
        ):
            prepare_smoke_evidence_set(self.client, self.qvid)

    def test_rejects_certification_mismatch_in_candidates(self):
        self.client._candidate_rows[0]["certification_exam_name"] = "OTHER-EXAM"
        with self.assertRaisesRegex(AiQualityAuditEvidenceError, "certification mismatch"):
            prepare_smoke_evidence_set(self.client, self.qvid)


if __name__ == "__main__":
    unittest.main()
