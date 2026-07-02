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
    GENERIC_EXAM_GUIDE_MIN_SCORE,
    MIN_DISCRIMINATIVE_OVERLAP_IDF,
    MIN_RELEVANCE_SCORE,
    RETRIEVAL_METHOD,
    AiQualityAuditEvidenceError,
    CANDIDATE_POOL_MAX,
    _build_question_query,
    _content_tokens,
    _option_content_tokens,
    build_bm25_corpus_stats,
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
    """Returns (ranked, previews, qualified_count, rejected_count, rejected_previews)."""
    return rank_question_evidence_candidates(
        candidates,
        blind_context=blind_context,
        resource_by_id=resource_by_id or {},
        max_chunks=max_chunks,
        max_characters=max_characters,
        max_chunks_per_resource=max_chunks_per_resource,
    )


def _score_with_corpus(
    blind_context: dict,
    candidates: list[dict],
    target_candidate: dict,
    *,
    resource_by_id: dict | None = None,
):
    """Score one candidate using BM25 corpus stats built from ``candidates``."""
    resource_by_id = resource_by_id or {}
    query_text = _build_question_query(blind_context)
    query_tokens = _content_tokens(query_text)
    question_tokens = _content_tokens(str(blind_context.get("question_text") or ""))
    option_tokens = _option_content_tokens(blind_context)
    question_domain = str(blind_context.get("domain_name") or "").strip()
    corpus_stats = build_bm25_corpus_stats(candidates, resource_by_id=resource_by_id)
    resource_row = resource_by_id.get(str(target_candidate["resource_id"])) or {}
    return score_evidence_candidate(
        target_candidate,
        query_text=query_text,
        query_tokens=query_tokens,
        question_tokens=question_tokens,
        option_tokens=option_tokens,
        question_domain=question_domain,
        resource_metadata=resource_row.get("metadata") or {},
        resource_title=str(resource_row.get("title") or target_candidate.get("title") or ""),
        corpus_stats=corpus_stats,
        resource_type=str(
            resource_row.get("resource_type") or target_candidate.get("resource_type") or ""
        ),
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


class TestBm25CorpusStats(unittest.TestCase):
    """Direct coverage of the internal BM25 corpus-statistics builder."""

    def test_idf_decreases_as_document_frequency_increases(self):
        rare_only = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Recycle Bin",
            chunk_text="Deleted records remain in the recyclebin for a retention period.",
        )
        common_a = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Salesforce Overview",
            chunk_text="Salesforce records store data for objects and users.",
        )
        common_b = _chunk(
            chunk_id="33333333-3333-3333-3333-333333333333",
            resource_id="cccccccc-0000-0000-0000-000000000003",
            title="Salesforce Security",
            chunk_text="Salesforce records are protected by sharing rules and profiles.",
        )
        stats = build_bm25_corpus_stats(
            [rare_only, common_a, common_b], resource_by_id={}
        )
        # "recyclebin" appears in exactly one document; "salesforce" and
        # "records" appear in more documents and must carry strictly lower IDF.
        self.assertGreater(stats.idf["recyclebin"], stats.idf["salesforce"])
        self.assertGreater(stats.idf["recyclebin"], stats.idf["records"])

    def test_generic_exam_guide_threshold_is_stricter_than_default(self):
        self.assertGreater(GENERIC_EXAM_GUIDE_MIN_SCORE, MIN_RELEVANCE_SCORE)

    def test_discriminative_overlap_guard_rejects_single_generic_term_match(self):
        """A candidate whose only overlap with the query is a single, low-IDF,
        near-universal term must not qualify merely from that generic overlap,
        even if BM25 nominally scores it above zero.
        """
        blind = {
            "question_text": "Which process defines and controls project scope?",
            "domain_name": "Project Scope Management",
            "options": [{"option_text": "Project scope statement", "option_label": "A"}],
        }
        focused = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Project Scope Management",
            chunk_text="Project scope management defines and controls the project boundaries.",
        )
        generic_overlap_only = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Business Process Improvement",
            chunk_text="Business process improvement streamlines organizational workflow.",
        )
        resource_by_id = {
            "aaaaaaaa-0000-0000-0000-000000000001": {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "title": "Project Scope Management",
                "metadata": {"topic": "project scope management"},
            },
            "bbbbbbbb-0000-0000-0000-000000000002": {
                "id": "bbbbbbbb-0000-0000-0000-000000000002",
                "title": "Business Process Improvement",
                "metadata": {"topic": "business process improvement"},
            },
        }
        breakdown = _score_with_corpus(
            blind,
            [focused, generic_overlap_only],
            generic_overlap_only,
            resource_by_id=resource_by_id,
        )
        self.assertFalse(breakdown.qualifies)
        self.assertLess(breakdown.relevance_score, MIN_RELEVANCE_SCORE)

    def test_discriminative_overlap_guard_directly_rejects_below_floor_overlap(self):
        """``_candidate_qualifies`` rejects on the discriminative-overlap guard
        specifically when overlap exists but carries too little combined IDF,
        independent of the absolute relevance-score threshold.
        """
        from workers.ai_quality_audit_evidence import _candidate_qualifies

        qualifies, reason = _candidate_qualifies(
            relevance_score=1.0,
            match_reasons=["title match"],
            overlap_count=1,
            discriminative_overlap_idf=MIN_DISCRIMINATIVE_OVERLAP_IDF - 0.01,
            resource_type="official_documentation",
            document_count=4,
        )
        self.assertFalse(qualifies)
        self.assertIn("non-generic discriminating terms", reason)


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
        ranked, _, qualified, rejected, _ = _rank(blind, [relationship, cross])
        selected_ids = {item["resource_chunk_id"] for item in ranked}
        self.assertIn(cross["resource_chunk_id"], selected_ids)
        self.assertNotIn(relationship["resource_chunk_id"], selected_ids)
        self.assertGreaterEqual(qualified, 1)
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
        ranked, _, qualified, _, _ = _rank(blind, [unrelated, notification])
        self.assertEqual(ranked[0]["resource_chunk_id"], notification["resource_chunk_id"])
        self.assertGreaterEqual(qualified, 1)

    def test_record_triggered_flow_question_excludes_custom_notification_evidence(self):
        """Custom Notification documentation must not qualify for an unrelated
        record-triggered-flow question merely through generic automation overlap.
        """
        blind = {
            "question_text": (
                "Which automation tool launches immediately when a record is created "
                "or updated?"
            ),
            "domain_name": "Process Automation",
            "options": [
                {"option_text": "Record-triggered flow", "option_label": "A"},
                {"option_text": "Custom report type", "option_label": "B"},
            ],
        }
        flow = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Record-Triggered Flows",
            chunk_text="Record-triggered flows start automatically when a record is created or updated.",
        )
        notification = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Send a Custom Notification with a Flow",
            chunk_text=(
                "Create a flow that sends a custom notification to users when a "
                "specific event occurs. Notifications can appear in the desktop and "
                "mobile app."
            ),
        )
        resource_by_id = {
            "aaaaaaaa-0000-0000-0000-000000000001": {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "title": "Record-Triggered Flows",
                "metadata": {"topic": "record-triggered flow automation"},
            },
            "bbbbbbbb-0000-0000-0000-000000000002": {
                "id": "bbbbbbbb-0000-0000-0000-000000000002",
                "title": "Send a Custom Notification with a Flow",
                "metadata": {"topic": "custom notification flow"},
            },
        }
        ranked, _, qualified, _, rejected_previews = _rank(
            blind, [notification, flow], resource_by_id=resource_by_id
        )
        self.assertEqual(ranked[0]["resource_chunk_id"], flow["resource_chunk_id"])
        self.assertEqual(qualified, 1)
        rejected_ids = {item["resource_chunk_id"] for item in rejected_previews}
        self.assertIn(notification["resource_chunk_id"], rejected_ids)

    def test_recycle_bin_question_ranks_recycle_bin_evidence_first_and_qualifies(self):
        blind = {
            "question_text": (
                "How long do deleted records remain in the Recycle Bin before "
                "permanent deletion?"
            ),
            "domain_name": "Data Management",
            "options": [
                {"option_text": "15 days", "option_label": "A"},
                {"option_text": "30 days", "option_label": "B"},
            ],
        }
        recycle = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Manage the Recycle Bin in Lightning Experience",
            chunk_text=(
                "Deleted records remain in the Recycle Bin for 15 days before "
                "permanent deletion. You can restore records from the Recycle Bin "
                "or empty it manually."
            ),
        )
        relationship = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Considerations for Object Relationships",
            chunk_text="Lookup relationships connect parent and child records across objects.",
        )
        resource_by_id = {
            "aaaaaaaa-0000-0000-0000-000000000001": {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "title": "Manage the Recycle Bin in Lightning Experience",
                "metadata": {"topic": "recycle bin data management"},
            },
            "bbbbbbbb-0000-0000-0000-000000000002": {
                "id": "bbbbbbbb-0000-0000-0000-000000000002",
                "title": "Considerations for Object Relationships",
                "metadata": {"topic": "lookup relationship delete behavior"},
            },
        }
        ranked, _, qualified, rejected, _ = _rank(
            blind, [relationship, recycle], resource_by_id=resource_by_id
        )
        self.assertEqual(ranked[0]["resource_chunk_id"], recycle["resource_chunk_id"])
        self.assertEqual(qualified, 1)
        self.assertGreater(rejected, 0)

    def test_user_story_question_prefers_user_story_evidence_over_unrelated_ba_resources(self):
        """User-story question must rank focused user-story documentation above
        unrelated broad business-analysis material."""
        blind = {
            "question_text": (
                "Which artifact captures user needs and acceptance criteria in "
                "Agile delivery?"
            ),
            "domain_name": "Agile Delivery",
            "options": [
                {"option_text": "User story", "option_label": "A"},
                {"option_text": "Gantt chart", "option_label": "B"},
            ],
        }
        story = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Writing Effective User Stories",
            chunk_text="A user story captures a user need and acceptance criteria for delivery teams.",
        )
        ba_overview = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Business Analysis Overview",
            chunk_text="Business analysis identifies needs and recommends solutions across the organization.",
        )
        resource_by_id = {
            "aaaaaaaa-0000-0000-0000-000000000001": {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "title": "Writing Effective User Stories",
                "metadata": {"topic": "user stories agile requirements"},
            },
            "bbbbbbbb-0000-0000-0000-000000000002": {
                "id": "bbbbbbbb-0000-0000-0000-000000000002",
                "title": "Business Analysis Overview",
                "metadata": {"topic": "business analysis general overview"},
            },
        }
        ranked, _, qualified, _, _ = _rank(
            blind, [ba_overview, story], resource_by_id=resource_by_id
        )
        self.assertEqual(ranked[0]["resource_chunk_id"], story["resource_chunk_id"])
        self.assertEqual(qualified, 1)

    def test_project_scope_question_ranks_scope_management_evidence_first(self):
        blind = {
            "question_text": (
                "Which process defines and controls what is and is not included "
                "in the project?"
            ),
            "domain_name": "Project Scope Management",
            "options": [
                {"option_text": "Project scope statement", "option_label": "A"},
                {"option_text": "Process map", "option_label": "B"},
            ],
        }
        scope = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Project Scope Management",
            chunk_text=(
                "Project scope management defines and controls what is and is not "
                "included in the project."
            ),
        )
        process = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Process Mapping",
            chunk_text="Process maps visualize steps, actors, and handoffs.",
        )
        resource_by_id = {
            "aaaaaaaa-0000-0000-0000-000000000001": {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "title": "Project Scope Management",
                "metadata": {"topic": "project scope management"},
            },
            "bbbbbbbb-0000-0000-0000-000000000002": {
                "id": "bbbbbbbb-0000-0000-0000-000000000002",
                "title": "Process Mapping",
                "metadata": {"topic": "process mapping technique"},
            },
        }
        ranked, _, qualified, rejected, _ = _rank(
            blind, [process, scope], resource_by_id=resource_by_id
        )
        self.assertEqual(ranked[0]["resource_chunk_id"], scope["resource_chunk_id"])
        self.assertEqual(qualified, 1)
        self.assertEqual(rejected, 1)

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
        resource_by_id = {
            "aaaaaaaa-0000-0000-0000-000000000001": {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "title": "Process Mapping",
                "metadata": {"topic": "process mapping technique"},
            },
            "bbbbbbbb-0000-0000-0000-000000000002": {
                "id": "bbbbbbbb-0000-0000-0000-000000000002",
                "title": "User Stories",
                "metadata": {"topic": "user stories agile requirements"},
            },
        }
        ranked, _, _, _, _ = _rank(blind, [story, process], resource_by_id=resource_by_id)
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
        ranked, _, qualified, _, _ = _rank(blind, weak)
        self.assertEqual(ranked, [])
        self.assertEqual(qualified, 0)

    def test_stakeholder_grid_reports_evidence_gap_when_only_weak_sources_exist(self):
        """A candidate pool with no credible focused source must return an
        evidence gap instead of accepting the best (still weak) result."""
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
        ranked, _, qualified, rejected, _ = _rank(blind, candidates)
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
        ranked, previews, qualified, rejected, _ = _rank(
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
        blind = {
            "question_text": "Which feature enables profile-based configuration defaults?",
            "domain_name": "Configuration",
            "options": [{"option_text": "Profiles", "option_label": "A"}],
        }
        shared_text = "Platform administration overview for administrators."
        candidate_match = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Administration Guide",
            chunk_text=shared_text,
        )
        candidate_miss = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Administration Guide",
            chunk_text=shared_text,
        )
        candidates = [candidate_match, candidate_miss]
        resource_by_id = {
            "aaaaaaaa-0000-0000-0000-000000000001": {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "title": "Administration Guide",
                "metadata": {"domain": "Configuration"},
            },
            "bbbbbbbb-0000-0000-0000-000000000002": {
                "id": "bbbbbbbb-0000-0000-0000-000000000002",
                "title": "Administration Guide",
                "metadata": {"domain": "Billing"},
            },
        }
        domain_match = _score_with_corpus(
            blind, candidates, candidate_match, resource_by_id=resource_by_id
        )
        domain_miss = _score_with_corpus(
            blind, candidates, candidate_miss, resource_by_id=resource_by_id
        )
        self.assertTrue(domain_match.qualifies)
        self.assertFalse(domain_miss.qualifies)
        self.assertGreater(domain_match.domain_boost, domain_miss.domain_boost)
        self.assertGreater(domain_match.relevance_score, domain_miss.relevance_score)

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
        decoy_candidates = [
            _chunk(
                chunk_id="decoy000-1111-1111-1111-111111111111",
                resource_id="decoy000-0000-0000-0000-000000000099",
                title="Manage the Recycle Bin in Lightning Experience",
                chunk_text="Deleted records remain in the recycle bin for a retention period.",
            ),
            _chunk(
                chunk_id="decoy001-1111-1111-1111-111111111111",
                resource_id="decoy001-0000-0000-0000-000000000098",
                title="B2C Commerce Overview",
                chunk_text="B2C Commerce provides storefront capabilities for retail.",
            ),
        ]

        resource_by_id = {
            f"{index:012x}-0000-0000-0000-000000000001": {
                "id": f"{index:012x}-0000-0000-0000-000000000001",
                "title": f"Profiles Guide {index}",
                "metadata": {"domain": "Configuration"},
            }
            for index in range(12)
        }
        resource_by_id.update(
            {
                "decoy000-0000-0000-0000-000000000099": {
                    "id": "decoy000-0000-0000-0000-000000000099",
                    "title": "Manage the Recycle Bin in Lightning Experience",
                    "metadata": {"topic": "recycle bin data management"},
                },
                "decoy001-0000-0000-0000-000000000098": {
                    "id": "decoy001-0000-0000-0000-000000000098",
                    "title": "B2C Commerce Overview",
                    "metadata": {"topic": "b2c commerce storefront"},
                },
            }
        )
        ranked, _, qualified, _, _ = _rank(
            self.blind_context,
            many_candidates + decoy_candidates,
            resource_by_id=resource_by_id,
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
        resource_by_id = {
            self.resource_a: {
                "id": self.resource_a,
                "title": "Profiles Help",
                "metadata": {"domain": "Configuration"},
            },
            self.resource_b: {
                "id": self.resource_b,
                "title": "Profiles Extended Guide",
                "metadata": {"domain": "Configuration"},
            },
        }
        ranked, _, _, _, _ = _rank(
            self.blind_context,
            budgeted_candidates,
            resource_by_id=resource_by_id,
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
                chunk_text=(
                    "Profiles define profile-based defaults and configuration settings "
                    f"section {index}."
                ),
                chunk_index=index,
            )
            for index in range(4)
        ]
        decoy_candidates = [
            _chunk(
                chunk_id="decoy000-1111-1111-1111-111111111111",
                resource_id="decoy000-0000-0000-0000-000000000099",
                title="Manage the Recycle Bin in Lightning Experience",
                chunk_text="Deleted records remain in the recycle bin for a retention period.",
            ),
        ]
        ranked, _, _, _, _ = _rank(
            self.blind_context,
            candidates + decoy_candidates,
            resource_by_id={
                same_resource: {
                    "id": same_resource,
                    "title": "Profiles Help",
                    "metadata": {"domain": "Configuration"},
                },
                "decoy000-0000-0000-0000-000000000099": {
                    "id": "decoy000-0000-0000-0000-000000000099",
                    "title": "Manage the Recycle Bin in Lightning Experience",
                    "metadata": {"topic": "recycle bin data management"},
                },
            },
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
        first_ranked, _, _, _, _ = _rank(self.blind_context, tied_candidates, max_chunks=2)
        second_ranked, _, _, _, _ = _rank(
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
                "chunk_text": "Profiles enable profile-based defaults and configuration settings for users.",
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


class TestBm25FocusedRetrievalScenarios(unittest.TestCase):
    """BM25 (bm25_question_match_v1) smoke-validation regressions.

    Fixtures are generic and modeled on the shapes of existing smoke resources
    (lookup-relationship delete behavior, recycle bin, B2C Commerce, exam
    guides); no question IDs, real resource IDs, or smoke-specific mappings are
    referenced here or in runtime code.
    """

    _BLIND_CONTEXT = {
        "question_text": (
            "A custom object tracks child records related to a parent object. If "
            "the parent record is deleted, the child records must remain in "
            "Salesforce, but the parent reference should be cleared automatically. "
            "Which relationship configuration should the administrator use?"
        ),
        "domain_name": "Object Manager and Lightning App Builder",
        "options": [
            {
                "option_label": "A",
                "option_text": (
                    "Lookup relationship with the delete behavior set to clear the "
                    "lookup value"
                ),
                "display_order": 1,
            },
            {
                "option_label": "B",
                "option_text": (
                    "Master-detail relationship with standard cascade delete behavior"
                ),
                "display_order": 2,
            },
        ],
    }

    _RELATIONSHIPS_METADATA = {"topic": "lookup relationship delete behavior"}

    _RELATIONSHIPS_CHUNK_TEXT = (
        "Lookup Relationships\n"
        "If the lookup field is optional, you can specify one of three behaviors to "
        "occur if the lookup record is deleted:\n"
        "Clear the value of this field. This is the default. Clearing the field is "
        "a good choice when the field doesn't have to contain a value from the "
        "associated lookup record.\n"
        "Don't allow deletion of the lookup record that's part of a lookup "
        "relationship.\n"
        "Delete this record also. Available only for a custom lookup field on a "
        "custom object."
    )

    _FLOW_CHUNK_TEXT = (
        "Send a Custom Notification with a Flow\n"
        "Create a flow that sends a custom notification to users when a specific "
        "event occurs. Notifications can appear in the desktop and mobile app."
    )

    def test_focused_query_excludes_option_strings(self):
        focused_query = _build_question_query(self._BLIND_CONTEXT)

        self.assertNotIn("clear the lookup value", focused_query.lower())
        self.assertNotIn("cascade delete", focused_query.lower())
        self.assertIn("custom object", focused_query.lower())
        self.assertIn("Object Manager and Lightning App Builder", focused_query)

    def test_object_relationship_deletion_question_ranks_focused_source_first_and_qualifies(self):
        relationships = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Considerations for Object Relationships",
            chunk_text=self._RELATIONSHIPS_CHUNK_TEXT,
        )
        notification = _chunk(
            chunk_id="22222222-2222-2222-2222-222222222222",
            resource_id="bbbbbbbb-0000-0000-0000-000000000002",
            title="Send a Custom Notification with a Flow",
            chunk_text=self._FLOW_CHUNK_TEXT,
        )
        resource_by_id = {
            "aaaaaaaa-0000-0000-0000-000000000001": {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "title": "Considerations for Object Relationships",
                "metadata": self._RELATIONSHIPS_METADATA,
            },
            "bbbbbbbb-0000-0000-0000-000000000002": {
                "id": "bbbbbbbb-0000-0000-0000-000000000002",
                "title": "Send a Custom Notification with a Flow",
                "metadata": {"topic": "custom notification flow"},
            },
        }
        ranked, previews, qualified, rejected, rejected_previews = _rank(
            self._BLIND_CONTEXT, [notification, relationships], resource_by_id=resource_by_id
        )

        self.assertEqual(ranked[0]["resource_chunk_id"], relationships["resource_chunk_id"])
        self.assertEqual(qualified, 1)
        self.assertEqual(rejected, 1)
        self.assertIn("question-text overlap", previews[0]["match_reasons"])
        rejected_ids = {item["resource_chunk_id"] for item in rejected_previews}
        self.assertIn(notification["resource_chunk_id"], rejected_ids)

    def test_generic_exam_guide_cannot_displace_focused_official_source(self):
        relationships = _chunk(
            chunk_id="11111111-1111-1111-1111-111111111111",
            resource_id="aaaaaaaa-0000-0000-0000-000000000001",
            title="Considerations for Object Relationships",
            chunk_text=self._RELATIONSHIPS_CHUNK_TEXT,
        )
        exam_guide = _chunk(
            chunk_id="33333333-3333-3333-3333-333333333333",
            resource_id="cccccccc-0000-0000-0000-000000000003",
            title="Administrator Certification Exam Guide",
            chunk_text=(
                "This exam guide covers topics including object relationships, "
                "security, and automation at a high level for certification "
                "preparation."
            ),
            resource_type="exam_guide",
        )
        resource_by_id = {
            "aaaaaaaa-0000-0000-0000-000000000001": {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "title": "Considerations for Object Relationships",
                "metadata": self._RELATIONSHIPS_METADATA,
            },
            "cccccccc-0000-0000-0000-000000000003": {
                "id": "cccccccc-0000-0000-0000-000000000003",
                "title": "Administrator Certification Exam Guide",
                "metadata": {"topic": "exam guide"},
                "resource_type": "exam_guide",
            },
        }
        ranked, _, qualified, _, rejected_previews = _rank(
            self._BLIND_CONTEXT, [exam_guide, relationships], resource_by_id=resource_by_id
        )

        rejected_ids = {item["resource_chunk_id"] for item in rejected_previews}
        self.assertIn(exam_guide["resource_chunk_id"], rejected_ids)
        if ranked:
            self.assertEqual(ranked[0]["resource_chunk_id"], relationships["resource_chunk_id"])
        self.assertLessEqual(qualified, 1)

    def test_b2c_commerce_does_not_qualify_for_unrelated_general_requirements_question(self):
        blind = {
            "question_text": (
                "Which technique is used to elicit and document business "
                "requirements from stakeholders?"
            ),
            "domain_name": "Requirements Elicitation",
            "options": [
                {"option_text": "Interview", "option_label": "A"},
                {"option_text": "Survey", "option_label": "B"},
            ],
        }
        requirements_doc = _chunk(
            chunk_id="44444444-4444-4444-4444-444444444444",
            resource_id="dddddddd-0000-0000-0000-000000000004",
            title="Eliciting Business Requirements",
            chunk_text=(
                "Interviews and surveys are common techniques for eliciting "
                "business requirements from stakeholders."
            ),
        )
        b2c_commerce = _chunk(
            chunk_id="55555555-5555-5555-5555-555555555555",
            resource_id="eeeeeeee-0000-0000-0000-000000000005",
            title="B2C Commerce Overview",
            chunk_text=(
                "B2C Commerce provides storefront capabilities for "
                "business-to-consumer retail experiences."
            ),
        )
        resource_by_id = {
            "dddddddd-0000-0000-0000-000000000004": {
                "id": "dddddddd-0000-0000-0000-000000000004",
                "title": "Eliciting Business Requirements",
                "metadata": {"topic": "requirements elicitation techniques"},
            },
            "eeeeeeee-0000-0000-0000-000000000005": {
                "id": "eeeeeeee-0000-0000-0000-000000000005",
                "title": "B2C Commerce Overview",
                "metadata": {"topic": "b2c commerce storefront"},
            },
        }
        ranked, _, qualified, _, rejected_previews = _rank(
            blind, [b2c_commerce, requirements_doc], resource_by_id=resource_by_id
        )

        self.assertEqual(qualified, 1)
        self.assertEqual(ranked[0]["resource_chunk_id"], requirements_doc["resource_chunk_id"])
        rejected_ids = {item["resource_chunk_id"] for item in rejected_previews}
        self.assertIn(b2c_commerce["resource_chunk_id"], rejected_ids)

    def test_exact_domain_metadata_without_question_support_is_rejected(self):
        """A generic same-domain resource must not qualify from domain metadata
        and generic pool vocabulary alone when the question topic is unrelated."""
        blind = {
            "question_text": (
                "In what order does the platform evaluate validation rules when a "
                "record has multiple active rules?"
            ),
            "domain_name": "Data Management",
            "options": [{"option_text": "Alphabetical order by rule name", "option_label": "A"}],
        }
        domain_only = _chunk(
            chunk_id="ffffffff-6666-6666-6666-666666666666",
            resource_id="99999999-0000-0000-0000-000000000015",
            title="General Data Management Overview",
            chunk_text=(
                "Data management encompasses storage, organization, and retrieval of "
                "business records across the organization."
            ),
        )
        decoys = [
            _chunk(
                chunk_id=f"{index:012x}-6666-6666-6666-666666666667",
                resource_id=f"99999999-{index:04x}-0000-0000-000000000015",
                title=title,
                chunk_text=text,
            )
            for index, title, text in (
                (
                    1,
                    "Data Management Best Practices",
                    "Data management best practices include deduplication and archiving.",
                ),
                (
                    2,
                    "Data Management Storage Limits",
                    "Data management storage limits vary by edition and license type.",
                ),
                (
                    3,
                    "Data Management Import Tools",
                    "Data management import tools help load records efficiently.",
                ),
            )
        ]
        resource_by_id = {
            "99999999-0000-0000-0000-000000000015": {
                "id": "99999999-0000-0000-0000-000000000015",
                "title": "General Data Management Overview",
                "metadata": {"domain": "Data Management"},
            },
            "99999999-0001-0000-0000-000000000015": {
                "id": "99999999-0001-0000-0000-000000000015",
                "title": "Data Management Best Practices",
                "metadata": {"domain": "Data Management"},
            },
            "99999999-0002-0000-0000-000000000015": {
                "id": "99999999-0002-0000-0000-000000000015",
                "title": "Data Management Storage Limits",
                "metadata": {"domain": "Data Management"},
            },
            "99999999-0003-0000-0000-000000000015": {
                "id": "99999999-0003-0000-0000-000000000015",
                "title": "Data Management Import Tools",
                "metadata": {"domain": "Data Management"},
            },
        }
        ranked, _, qualified, rejected, _ = _rank(
            blind, [domain_only, *decoys], resource_by_id=resource_by_id
        )
        domain_breakdown = _score_with_corpus(
            blind,
            [domain_only, *decoys],
            domain_only,
            resource_by_id=resource_by_id,
        )

        self.assertEqual(qualified, 0)
        self.assertEqual(ranked, [])
        self.assertEqual(rejected, len(decoys) + 1)
        self.assertFalse(domain_breakdown.qualifies)
        self.assertIn("exact domain match", domain_breakdown.match_reasons)

    def test_same_domain_exam_guide_cannot_qualify_from_domain_metadata_alone(self):
        blind = {
            "question_text": (
                "In what order does the platform evaluate validation rules when a "
                "record has multiple active rules?"
            ),
            "domain_name": "Data Management",
            "options": [{"option_text": "Alphabetical order by rule name", "option_label": "A"}],
        }
        exam_guide = _chunk(
            chunk_id="11112222-7777-7777-7777-777777777777",
            resource_id="aaaa9999-0000-0000-0000-000000000016",
            title="Administrator Certification Exam Guide",
            chunk_text=(
                "This exam guide summarizes topics across security, automation, and "
                "data management at a high level for certification preparation."
            ),
            resource_type="exam_guide",
        )
        decoys = [
            _chunk(
                chunk_id=f"{index:012x}-6666-6666-6666-666666666667",
                resource_id=f"99999999-{index:04x}-0000-0000-000000000015",
                title=title,
                chunk_text=text,
            )
            for index, title, text in (
                (
                    1,
                    "Data Management Best Practices",
                    "Data management best practices include deduplication and archiving.",
                ),
                (
                    2,
                    "Data Management Storage Limits",
                    "Data management storage limits vary by edition and license type.",
                ),
                (
                    3,
                    "General Data Management Overview",
                    "Data management encompasses storage and retrieval of business records.",
                ),
            )
        ]
        resource_by_id = {
            "aaaa9999-0000-0000-0000-000000000016": {
                "id": "aaaa9999-0000-0000-0000-000000000016",
                "title": "Administrator Certification Exam Guide",
                "metadata": {"domain": "Data Management"},
                "resource_type": "exam_guide",
            },
            "99999999-0001-0000-0000-000000000015": {
                "id": "99999999-0001-0000-0000-000000000015",
                "title": "Data Management Best Practices",
                "metadata": {"domain": "Data Management"},
            },
            "99999999-0002-0000-0000-000000000015": {
                "id": "99999999-0002-0000-0000-000000000015",
                "title": "Data Management Storage Limits",
                "metadata": {"domain": "Data Management"},
            },
            "99999999-0003-0000-0000-000000000015": {
                "id": "99999999-0003-0000-0000-000000000015",
                "title": "General Data Management Overview",
                "metadata": {"domain": "Data Management"},
            },
        }
        ranked, _, qualified, _, _ = _rank(
            blind, [exam_guide, *decoys], resource_by_id=resource_by_id
        )
        exam_breakdown = _score_with_corpus(
            blind,
            [exam_guide, *decoys],
            exam_guide,
            resource_by_id=resource_by_id,
        )

        self.assertEqual(qualified, 0)
        self.assertEqual(ranked, [])
        self.assertFalse(exam_breakdown.qualifies)
        self.assertIn("exact domain match", exam_breakdown.match_reasons)

    def test_recycle_bin_question_ranks_focused_source_first_and_qualifies(self):
        blind = {
            "question_text": (
                "How long do deleted records remain in the Recycle Bin before "
                "permanent deletion?"
            ),
            "domain_name": "Data Management",
            "options": [
                {"option_text": "15 days", "option_label": "A"},
                {"option_text": "30 days", "option_label": "B"},
            ],
        }
        recycle = _chunk(
            chunk_id="33333333-3333-3333-3333-333333333333",
            resource_id="cccccccc-0000-0000-0000-000000000003",
            title="Manage the Recycle Bin in Lightning Experience",
            chunk_text=(
                "Deleted records remain in the Recycle Bin for 15 days before "
                "permanent deletion. You can restore records from the Recycle Bin "
                "or empty it manually."
            ),
        )
        relationship = _chunk(
            chunk_id="44444444-4444-4444-4444-444444444444",
            resource_id="dddddddd-0000-0000-0000-000000000004",
            title="Considerations for Object Relationships",
            chunk_text="Lookup relationships connect parent and child records across objects.",
        )
        resource_by_id = {
            "cccccccc-0000-0000-0000-000000000003": {
                "id": "cccccccc-0000-0000-0000-000000000003",
                "title": "Manage the Recycle Bin in Lightning Experience",
                "metadata": {"topic": "recycle bin data management"},
            },
            "dddddddd-0000-0000-0000-000000000004": {
                "id": "dddddddd-0000-0000-0000-000000000004",
                "title": "Considerations for Object Relationships",
                "metadata": {"topic": "lookup relationship delete behavior"},
            },
        }
        ranked, previews, qualified, _, _ = _rank(
            blind, [relationship, recycle], resource_by_id=resource_by_id
        )

        self.assertEqual(ranked[0]["resource_chunk_id"], recycle["resource_chunk_id"])
        self.assertEqual(qualified, 1)
        self.assertGreaterEqual(previews[0]["relevance_score"], MIN_RELEVANCE_SCORE)

    def test_user_story_and_scope_management_focused_sources_qualify(self):
        story_blind = {
            "question_text": (
                "Which artifact captures user needs and acceptance criteria in "
                "Agile delivery?"
            ),
            "domain_name": "Agile Delivery",
            "options": [
                {"option_text": "User story", "option_label": "A"},
                {"option_text": "Gantt chart", "option_label": "B"},
            ],
        }
        story = _chunk(
            chunk_id="55555555-5555-5555-5555-555555555555",
            resource_id="eeeeeeee-0000-0000-0000-000000000005",
            title="Writing Effective User Stories",
            chunk_text=(
                "A user story captures a user need and acceptance criteria for "
                "delivery teams."
            ),
        )
        ba_overview = _chunk(
            chunk_id="66666666-6666-6666-6666-666666666666",
            resource_id="ffffffff-0000-0000-0000-000000000006",
            title="Business Analysis Overview",
            chunk_text=(
                "Business analysis identifies needs and recommends solutions across "
                "the organization."
            ),
        )
        story_resource_by_id = {
            "eeeeeeee-0000-0000-0000-000000000005": {
                "id": "eeeeeeee-0000-0000-0000-000000000005",
                "title": "Writing Effective User Stories",
                "metadata": {"topic": "user stories agile requirements"},
            },
            "ffffffff-0000-0000-0000-000000000006": {
                "id": "ffffffff-0000-0000-0000-000000000006",
                "title": "Business Analysis Overview",
                "metadata": {"topic": "business analysis general overview"},
            },
        }
        story_ranked, story_previews, story_qualified, _, _ = _rank(
            story_blind, [ba_overview, story], resource_by_id=story_resource_by_id
        )
        self.assertEqual(story_ranked[0]["resource_chunk_id"], story["resource_chunk_id"])
        self.assertEqual(story_qualified, 1)
        self.assertGreaterEqual(story_previews[0]["relevance_score"], MIN_RELEVANCE_SCORE)

        scope_blind = {
            "question_text": (
                "Which process defines and controls what is and is not included "
                "in the project?"
            ),
            "domain_name": "Project Scope Management",
            "options": [
                {"option_text": "Project scope statement", "option_label": "A"},
                {"option_text": "Process map", "option_label": "B"},
            ],
        }
        scope = _chunk(
            chunk_id="77777777-7777-7777-7777-777777777777",
            resource_id="11111111-0000-0000-0000-000000000007",
            title="Project Scope Management",
            chunk_text=(
                "Project scope management defines and controls what is and is not "
                "included in the project."
            ),
        )
        process = _chunk(
            chunk_id="88888888-8888-8888-8888-888888888888",
            resource_id="22222222-0000-0000-0000-000000000008",
            title="Process Mapping",
            chunk_text="Process maps visualize steps, actors, and handoffs.",
        )
        scope_resource_by_id = {
            "11111111-0000-0000-0000-000000000007": {
                "id": "11111111-0000-0000-0000-000000000007",
                "title": "Project Scope Management",
                "metadata": {"topic": "project scope management"},
            },
            "22222222-0000-0000-0000-000000000008": {
                "id": "22222222-0000-0000-0000-000000000008",
                "title": "Process Mapping",
                "metadata": {"topic": "process mapping technique"},
            },
        }
        scope_ranked, scope_previews, scope_qualified, _, _ = _rank(
            scope_blind, [process, scope], resource_by_id=scope_resource_by_id
        )
        self.assertEqual(scope_ranked[0]["resource_chunk_id"], scope["resource_chunk_id"])
        self.assertEqual(scope_qualified, 1)
        self.assertGreaterEqual(scope_previews[0]["relevance_score"], MIN_RELEVANCE_SCORE)

    def test_record_triggered_flow_excludes_custom_notification_negative_control(self):
        blind = {
            "question_text": (
                "Which automation tool launches immediately when a record is created "
                "or updated?"
            ),
            "domain_name": "Process Automation",
            "options": [
                {"option_text": "Record-triggered flow", "option_label": "A"},
                {"option_text": "Custom report type", "option_label": "B"},
            ],
        }
        flow = _chunk(
            chunk_id="99999999-9999-9999-9999-999999999999",
            resource_id="33333333-0000-0000-0000-000000000009",
            title="Record-Triggered Flows",
            chunk_text=(
                "Record-triggered flows start automatically when a record is created "
                "or updated."
            ),
        )
        notification = _chunk(
            chunk_id="aaaaaaaa-1111-1111-1111-111111111111",
            resource_id="44444444-0000-0000-0000-000000000010",
            title="Send a Custom Notification with a Flow",
            chunk_text=self._FLOW_CHUNK_TEXT,
        )
        resource_by_id = {
            "33333333-0000-0000-0000-000000000009": {
                "id": "33333333-0000-0000-0000-000000000009",
                "title": "Record-Triggered Flows",
                "metadata": {"topic": "record-triggered flow automation"},
            },
            "44444444-0000-0000-0000-000000000010": {
                "id": "44444444-0000-0000-0000-000000000010",
                "title": "Send a Custom Notification with a Flow",
                "metadata": {"topic": "custom notification flow"},
            },
        }
        ranked, _, qualified, _, rejected_previews = _rank(
            blind, [notification, flow], resource_by_id=resource_by_id
        )

        self.assertEqual(ranked[0]["resource_chunk_id"], flow["resource_chunk_id"])
        self.assertEqual(qualified, 1)
        rejected_ids = {item["resource_chunk_id"] for item in rejected_previews}
        self.assertIn(notification["resource_chunk_id"], rejected_ids)

    def test_candidate_pool_without_credible_evidence_returns_zero_qualified(self):
        blind = {
            "question_text": "Which grid plots stakeholder power against interest?",
            "domain_name": "Stakeholder Engagement",
            "options": [{"option_text": "Power/Interest Grid", "option_label": "A"}],
        }
        weak = _chunk(
            chunk_id="eeeeeeee-5555-5555-5555-555555555555",
            resource_id="88888888-0000-0000-0000-000000000014",
            title="Business Analysis Overview",
            chunk_text="Business analysis identifies needs and recommends solutions.",
        )
        ranked, _, qualified, rejected, _ = _rank(blind, [weak])

        self.assertEqual(ranked, [])
        self.assertEqual(qualified, 0)
        self.assertEqual(rejected, 1)


if __name__ == "__main__":
    unittest.main()
