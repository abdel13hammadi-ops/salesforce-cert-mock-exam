"""Tests for the pure semantic concept-cluster detector (Phase 1)."""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.duplicate_question_detector import (
    DETECTION_METHOD_EXACT,
    FINDING_CODE_EXACT,
)
from workers.semantic_cluster_detector import (
    SIGNAL_CORRECT,
    SIGNAL_FULL,
    SIGNAL_STEM,
    SemanticClusterThresholds,
    _select_member_to_split,
    build_correct_answer_text,
    build_full_question_text,
    build_stem_text,
    cosine_similarity_matrix,
    count_pairwise_edges,
    derive_cluster_id,
    detect_semantic_clusters,
    detect_semantic_clusters_for_certification,
    normalized_stem,
)


ADM = "Salesforce Certified Platform Administrator"
BA = "Salesforce Certified Business Analyst"


def _entry(
    qvid: str,
    question_id: int,
    *,
    cert: str = ADM,
    stem: str,
    options: list | None = None,
    category: str | None = None,
    concept_key: str | None = None,
) -> dict:
    return {
        "question_version_id": qvid,
        "question_id": question_id,
        "certification_exam_name": cert,
        "category": category,
        "concept_key": concept_key,
        "snapshot": {
            "question_text": stem,
            "explanation": "Because.",
            "question_type": "single",
            "select_count": 1,
            "options": options
            or [
                {
                    "option_label": "A",
                    "option_text": f"Answer for {question_id}",
                    "is_correct": True,
                    "display_order": 1,
                },
                {
                    "option_label": "B",
                    "option_text": f"Distractor for {question_id}",
                    "is_correct": False,
                    "display_order": 2,
                },
            ],
        },
    }


def _unit_vector(*components: float) -> list[float]:
    norm = math.sqrt(sum(value * value for value in components))
    if norm == 0:
        raise ValueError("zero vector")
    return [value / norm for value in components]


def _multi_signal_embed_fn(
    stem_vectors: list[list[float]],
    full_vectors: list[list[float]],
    correct_vectors: list[list[float]],
):
    calls: list[str] = []

    def embed(texts):
        batch = list(texts)
        if len(calls) == 0:
            calls.append(SIGNAL_STEM)
            if len(batch) != len(stem_vectors):
                raise AssertionError("unexpected stem batch size")
            return stem_vectors
        if len(calls) == 1:
            calls.append(SIGNAL_FULL)
            if len(batch) != len(full_vectors):
                raise AssertionError("unexpected full batch size")
            return full_vectors
        if len(calls) == 2:
            calls.append(SIGNAL_CORRECT)
            if len(batch) != len(correct_vectors):
                raise AssertionError("unexpected correct batch size")
            return correct_vectors
        raise AssertionError(f"embed_fn called too many times: {calls}")

    return embed, calls


class TestTextViews(unittest.TestCase):
    def test_three_text_views_are_distinct(self):
        entry = _entry(
            "11111111-0000-0000-0000-000000000001",
            1,
            stem="What is Flow?",
            options=[
                {
                    "option_label": "A",
                    "option_text": "Automation tool",
                    "is_correct": True,
                    "display_order": 1,
                },
                {
                    "option_label": "B",
                    "option_text": "Report type",
                    "is_correct": False,
                    "display_order": 2,
                },
            ],
        )
        stem = build_stem_text(entry)
        full = build_full_question_text(entry)
        correct = build_correct_answer_text(entry)
        self.assertEqual(stem, "What is Flow?")
        self.assertIn("Automation tool", full)
        self.assertIn("Report type", full)
        self.assertEqual(correct, "Automation tool")
        self.assertNotEqual(stem, full)
        self.assertNotEqual(full, correct)


class TestCosineSimilarityMatrix(unittest.TestCase):
    def test_full_all_pairs_coverage(self):
        vectors = [
            _unit_vector(1, 0, 0),
            _unit_vector(0, 1, 0),
            _unit_vector(0, 0, 1),
            _unit_vector(1, 1, 0),
        ]
        matrix = cosine_similarity_matrix(vectors)
        self.assertEqual(len(matrix), 4)
        self.assertEqual(len(matrix[0]), 4)
        for row_index in range(4):
            self.assertAlmostEqual(matrix[row_index][row_index], 1.0)
        off_diagonal = [
            matrix[left][right]
            for left in range(4)
            for right in range(4)
            if left < right
        ]
        self.assertEqual(len(off_diagonal), 6)
        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.5,
            full_edge_threshold=0.5,
            correct_edge_threshold=0.5,
            cohesion_min_similarity=0.5,
        )
        matrices = {SIGNAL_FULL: matrix, SIGNAL_STEM: matrix, SIGNAL_CORRECT: matrix}
        self.assertEqual(count_pairwise_edges(4, matrices, thresholds), 2)


class TestDeterministicClusterIds(unittest.TestCase):
    def test_cluster_id_is_stable_and_order_independent(self):
        ids_a = (
            "bbbbbbbb-0000-0000-0000-000000000002",
            "aaaaaaaa-0000-0000-0000-000000000001",
        )
        ids_b = (
            "aaaaaaaa-0000-0000-0000-000000000001",
            "bbbbbbbb-0000-0000-0000-000000000002",
        )
        self.assertEqual(derive_cluster_id(ids_a), derive_cluster_id(ids_b))
        self.assertEqual(len(derive_cluster_id(ids_a)), 64)


class TestAllowedClusterSizes(unittest.TestCase):
    def _detect(self, entries, vectors):
        embed_fn, _calls = _multi_signal_embed_fn(vectors, vectors, vectors)
        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.80,
            full_edge_threshold=0.80,
            correct_edge_threshold=0.80,
            cohesion_min_similarity=0.75,
        )
        return detect_semantic_clusters_for_certification(
            entries,
            embed_fn=embed_fn,
            thresholds=thresholds,
        )

    def test_two_question_allowed_cluster(self):
        entries = [
            _entry("11111111-0000-0000-0000-000000000001", 1, stem="Topic A"),
            _entry("22222222-0000-0000-0000-000000000002", 2, stem="Topic B"),
        ]
        shared = _unit_vector(1, 0)
        result = self._detect(entries, [shared, shared])
        self.assertEqual(len(result.clusters), 1)
        self.assertEqual(result.clusters[0].cluster_size, 2)
        self.assertFalse(result.clusters[0].is_review_candidate)
        self.assertEqual(result.allowed_clusters, result.clusters)
        self.assertEqual(result.review_candidates, [])

    def test_three_question_allowed_cluster(self):
        entries = [
            _entry(f"{index:08d}0000-0000-0000-00000000000{index}", index, stem=f"Topic {index}")
            for index in range(1, 4)
        ]
        shared = _unit_vector(1, 0)
        result = self._detect(entries, [shared, shared, shared])
        self.assertEqual(len(result.clusters), 1)
        self.assertEqual(result.clusters[0].cluster_size, 3)
        self.assertFalse(result.clusters[0].is_review_candidate)
        self.assertEqual(result.allowed_clusters, result.clusters)
        self.assertEqual(result.review_candidates, [])

    def test_four_question_review_cluster(self):
        entries = [
            _entry(
                f"aaaaaaa{index}-0000-0000-0000-00000000000{index}",
                index,
                stem=f"Topic {index}",
            )
            for index in range(1, 5)
        ]
        shared = _unit_vector(1, 0)
        result = self._detect(entries, [shared, shared, shared, shared])
        self.assertEqual(len(result.clusters), 1)
        self.assertEqual(result.clusters[0].cluster_size, 4)
        self.assertTrue(result.clusters[0].is_review_candidate)
        self.assertEqual(result.review_candidates, result.clusters)
        self.assertEqual(result.allowed_clusters, [])


class TestChainMergingSplit(unittest.TestCase):
    def test_chain_merging_is_split_by_cohesion(self):
        entries = [
            _entry("11111111-0000-0000-0000-000000000001", 1, stem="Scenario A"),
            _entry("22222222-0000-0000-0000-000000000002", 2, stem="Scenario B"),
            _entry("33333333-0000-0000-0000-000000000003", 3, stem="Scenario C"),
        ]
        stem_vectors = [
            _unit_vector(0.95, 0.31, 0),
            _unit_vector(1, 0, 0),
            _unit_vector(0.805, 0, 0.593),
        ]
        embed_fn, _calls = _multi_signal_embed_fn(
            stem_vectors,
            stem_vectors,
            stem_vectors,
        )
        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.80,
            full_edge_threshold=0.80,
            correct_edge_threshold=0.80,
            cohesion_min_similarity=0.77,
            cohesion_signal=SIGNAL_STEM,
        )
        result = detect_semantic_clusters_for_certification(
            entries,
            embed_fn=embed_fn,
            thresholds=thresholds,
        )
        self.assertEqual(len(result.clusters), 1)
        cluster = result.clusters[0]
        self.assertEqual(cluster.cluster_size, 2)
        self.assertIn("11111111-0000-0000-0000-000000000001", cluster.question_version_ids)
        self.assertIn("22222222-0000-0000-0000-000000000002", cluster.question_version_ids)
        self.assertNotIn("33333333-0000-0000-0000-000000000003", cluster.question_version_ids)


class TestCorrectAnswerDoesNotCreateEdges(unittest.TestCase):
    def test_identical_correct_answer_text_does_not_cluster(self):
        shared_correct = "Use a validation rule"
        entries = [
            _entry(
                "11111111-0000-0000-0000-000000000001",
                1,
                stem="How do you enforce field rules on insert?",
                options=[
                    {
                        "option_label": "A",
                        "option_text": shared_correct,
                        "is_correct": True,
                        "display_order": 1,
                    },
                    {
                        "option_label": "B",
                        "option_text": "Use a report",
                        "is_correct": False,
                        "display_order": 2,
                    },
                ],
            ),
            _entry(
                "22222222-0000-0000-0000-000000000002",
                2,
                stem="Which automation updates related records nightly?",
                options=[
                    {
                        "option_label": "A",
                        "option_text": shared_correct,
                        "is_correct": True,
                        "display_order": 1,
                    },
                    {
                        "option_label": "B",
                        "option_text": "Use a dashboard",
                        "is_correct": False,
                        "display_order": 2,
                    },
                ],
            ),
        ]
        shared_correct_vector = _unit_vector(1, 0)
        embed_fn, _calls = _multi_signal_embed_fn(
            [_unit_vector(1, 0), _unit_vector(0, 1)],
            [_unit_vector(0, 1), _unit_vector(1, 0)],
            [shared_correct_vector, shared_correct_vector],
        )
        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.80,
            full_edge_threshold=0.80,
            correct_edge_threshold=0.80,
            cohesion_min_similarity=0.75,
        )
        result = detect_semantic_clusters_for_certification(
            entries,
            embed_fn=embed_fn,
            thresholds=thresholds,
        )
        self.assertEqual(result.clusters, [])
        self.assertEqual(build_correct_answer_text(entries[0]), build_correct_answer_text(entries[1]))


class TestCohesionSplitPreservesCore(unittest.TestCase):
    def test_split_removes_endpoint_with_lower_medoid_similarity(self):
        matrix = [
            [1.0, 0.92, 0.50],
            [0.92, 1.0, 0.70],
            [0.50, 0.70, 1.0],
        ]
        removed = _select_member_to_split([0, 1, 2], medoid=1, matrix=matrix, cohesion_min=0.75)
        self.assertEqual(removed, 2)

    def test_split_tie_breaks_on_lower_index(self):
        matrix = [
            [1.0, 0.80, 0.50],
            [0.80, 1.0, 0.50],
            [0.50, 0.50, 1.0],
        ]
        removed = _select_member_to_split([0, 1, 2], medoid=2, matrix=matrix, cohesion_min=0.75)
        self.assertEqual(removed, 0)

    def test_integration_preserves_core_partner(self):
        entries = [
            _entry("11111111-0000-0000-0000-000000000001", 1, stem="Core hub"),
            _entry("22222222-0000-0000-0000-000000000002", 2, stem="Core partner"),
            _entry("33333333-0000-0000-0000-000000000003", 3, stem="Weak tail"),
        ]
        stem_vectors = [
            _unit_vector(0.95, 0.31, 0),
            _unit_vector(1, 0, 0),
            _unit_vector(0.805, 0, 0.593),
        ]
        embed_fn, _calls = _multi_signal_embed_fn(
            stem_vectors,
            stem_vectors,
            stem_vectors,
        )
        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.80,
            full_edge_threshold=0.80,
            correct_edge_threshold=0.80,
            cohesion_min_similarity=0.77,
            cohesion_signal=SIGNAL_STEM,
        )
        result = detect_semantic_clusters_for_certification(
            entries,
            embed_fn=embed_fn,
            thresholds=thresholds,
        )
        self.assertEqual(len(result.clusters), 1)
        cluster = result.clusters[0]
        self.assertEqual(cluster.cluster_size, 2)
        self.assertIn("11111111-0000-0000-0000-000000000001", cluster.question_version_ids)
        self.assertIn("22222222-0000-0000-0000-000000000002", cluster.question_version_ids)
        self.assertNotIn("33333333-0000-0000-0000-000000000003", cluster.question_version_ids)


class TestPairedIdSorting(unittest.TestCase):
    def test_question_ids_stay_paired_when_version_ids_sort_differently(self):
        entries = [
            _entry(
                "zzzzzzzz-0000-0000-0000-000000000099",
                1,
                stem="Topic late id",
            ),
            _entry(
                "aaaaaaaa-0000-0000-0000-000000000001",
                99,
                stem="Topic early id",
            ),
        ]
        shared = _unit_vector(1, 0)
        embed_fn, _calls = _multi_signal_embed_fn([shared, shared], [shared, shared], [shared, shared])
        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.80,
            full_edge_threshold=0.80,
            correct_edge_threshold=0.80,
            cohesion_min_similarity=0.75,
        )
        result = detect_semantic_clusters_for_certification(
            entries,
            embed_fn=embed_fn,
            thresholds=thresholds,
        )
        cluster = result.clusters[0]
        self.assertEqual(
            list(cluster.question_version_ids),
            [
                "aaaaaaaa-0000-0000-0000-000000000001",
                "zzzzzzzz-0000-0000-0000-000000000099",
            ],
        )
        self.assertEqual(list(cluster.question_ids), [99, 1])
        self.assertNotEqual(list(cluster.question_ids), [1, 99])


class TestCertificationIsolation(unittest.TestCase):
    def test_clusters_do_not_cross_certifications(self):
        entries = [
            _entry(
                "11111111-0000-0000-0000-000000000001",
                1,
                cert=ADM,
                stem="Shared stem",
            ),
            _entry(
                "22222222-0000-0000-0000-000000000002",
                2,
                cert=ADM,
                stem="Shared stem two",
            ),
            _entry(
                "33333333-0000-0000-0000-000000000003",
                101,
                cert=BA,
                stem="Shared stem",
            ),
            _entry(
                "44444444-0000-0000-0000-000000000004",
                102,
                cert=BA,
                stem="Shared stem two",
            ),
        ]

        def embed(texts):
            return [_unit_vector(1, 0) for _ in texts]

        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.80,
            full_edge_threshold=0.80,
            correct_edge_threshold=0.80,
            cohesion_min_similarity=0.75,
        )
        results = detect_semantic_clusters(
            entries,
            embed_fn=embed,
            thresholds=thresholds,
        )
        self.assertEqual(len(results), 2)
        by_cert = {result.certification_exam_name: result for result in results}
        self.assertEqual(set(by_cert), {ADM, BA})
        adm_result = by_cert[ADM]
        ba_result = by_cert[BA]
        self.assertEqual(len(adm_result.clusters), 1)
        self.assertEqual(len(ba_result.clusters), 1)
        self.assertEqual(adm_result.clusters[0].cluster_size, 2)
        self.assertEqual(ba_result.clusters[0].cluster_size, 2)
        adm_qvids = set(adm_result.clusters[0].question_version_ids)
        ba_qvids = set(ba_result.clusters[0].question_version_ids)
        self.assertTrue(adm_qvids.issubset({"11111111-0000-0000-0000-000000000001", "22222222-0000-0000-0000-000000000002"}))
        self.assertTrue(ba_qvids.issubset({"33333333-0000-0000-0000-000000000003", "44444444-0000-0000-0000-000000000004"}))


class TestLexicalDuplicateDelegation(unittest.TestCase):
    def test_exact_cosmetic_duplicate_is_delegated_to_lexical_detector(self):
        entries = [
            _entry(
                "11111111-0000-0000-0000-000000000001",
                1,
                stem="What is Salesforce Flow?",
            ),
            _entry(
                "22222222-0000-0000-0000-000000000002",
                2,
                stem="what is salesforce flow",
            ),
        ]
        embed_fn, _calls = _multi_signal_embed_fn(
            [_unit_vector(1, 0), _unit_vector(0, 1)],
            [_unit_vector(1, 0), _unit_vector(0, 1)],
            [_unit_vector(1, 0), _unit_vector(0, 1)],
        )
        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.99,
            full_edge_threshold=0.99,
            correct_edge_threshold=0.99,
            cohesion_min_similarity=0.95,
        )
        result = detect_semantic_clusters_for_certification(
            entries,
            embed_fn=embed_fn,
            thresholds=thresholds,
        )
        self.assertEqual(result.clusters, [])
        self.assertEqual(len(result.lexical_findings), 1)
        finding = result.lexical_findings[0]
        self.assertEqual(finding["finding_code"], FINDING_CODE_EXACT)
        self.assertEqual(
            finding["metadata"]["detection_method"],
            DETECTION_METHOD_EXACT,
        )
        self.assertEqual(
            normalized_stem(entries[0]),
            normalized_stem(entries[1]),
        )


class TestStableOutputOrdering(unittest.TestCase):
    def test_clusters_and_members_are_sorted_deterministically(self):
        entries = [
            _entry("cccccccc-0000-0000-0000-000000000003", 3, stem="Topic three"),
            _entry("aaaaaaaa-0000-0000-0000-000000000001", 1, stem="Topic one"),
            _entry("dddddddd-0000-0000-0000-000000000004", 4, stem="Topic four"),
            _entry("bbbbbbbb-0000-0000-0000-000000000002", 2, stem="Topic two"),
        ]
        embed_fn, _calls = _multi_signal_embed_fn(
            [
                _unit_vector(1, 0),
                _unit_vector(1, 0),
                _unit_vector(0, 1),
                _unit_vector(0, 1),
            ],
            [
                _unit_vector(1, 0),
                _unit_vector(1, 0),
                _unit_vector(0, 1),
                _unit_vector(0, 1),
            ],
            [
                _unit_vector(1, 0),
                _unit_vector(1, 0),
                _unit_vector(0, 1),
                _unit_vector(0, 1),
            ],
        )
        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.80,
            full_edge_threshold=0.80,
            correct_edge_threshold=0.80,
            cohesion_min_similarity=0.75,
        )
        result = detect_semantic_clusters_for_certification(
            entries,
            embed_fn=embed_fn,
            thresholds=thresholds,
        )
        self.assertEqual(len(result.clusters), 2)
        cluster_ids = [cluster.cluster_id for cluster in result.clusters]
        self.assertEqual(cluster_ids, sorted(cluster_ids))
        for cluster in result.clusters:
            self.assertEqual(
                list(cluster.question_version_ids),
                sorted(cluster.question_version_ids),
            )
            paired = sorted(
                zip(cluster.question_version_ids, cluster.question_ids),
                key=lambda item: item[0],
            )
            self.assertEqual(list(cluster.question_version_ids), [item[0] for item in paired])
            self.assertEqual(list(cluster.question_ids), [item[1] for item in paired])


class TestClusterStatistics(unittest.TestCase):
    def test_pairwise_similarity_stats_are_populated(self):
        entries = [
            _entry("11111111-0000-0000-0000-000000000001", 1, stem="A"),
            _entry("22222222-0000-0000-0000-000000000002", 2, stem="B"),
        ]
        vectors = [_unit_vector(1, 0), _unit_vector(0.9, 0.436)]
        embed_fn, _calls = _multi_signal_embed_fn(vectors, vectors, vectors)
        thresholds = SemanticClusterThresholds(
            stem_edge_threshold=0.80,
            full_edge_threshold=0.80,
            correct_edge_threshold=0.80,
            cohesion_min_similarity=0.75,
        )
        result = detect_semantic_clusters_for_certification(
            entries,
            embed_fn=embed_fn,
            thresholds=thresholds,
        )
        cluster = result.clusters[0]
        self.assertAlmostEqual(cluster.stem_similarity.min, cluster.stem_similarity.max)
        self.assertAlmostEqual(cluster.stem_similarity.average, cluster.stem_similarity.min)
        self.assertGreater(cluster.full_similarity.min, 0.0)


if __name__ == "__main__":
    unittest.main()
