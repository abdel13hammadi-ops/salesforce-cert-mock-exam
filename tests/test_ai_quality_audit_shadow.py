"""Tests for offline hybrid_question_match_v2 shadow classification (Stage 1)."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_evidence import RETRIEVAL_METHOD, replay_bm25_candidate_from_record
from workers.ai_quality_audit_shadow import (
    CONFIDENCE_CLASS_NO_STRUCTURAL,
    CONFIDENCE_CLASS_SEMANTIC_REVIEW,
    CONFIDENCE_CLASS_V1_SUFFICIENT,
    PROPOSED_RETRIEVAL_METHOD,
    SHADOW_CLASSIFICATION_SCHEMA_VERSION,
    classify_question_shadow_from_replay_record,
    dumps_shadow_classification,
)

_FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "v48_retrieval_replay_v1.json",
)

_STRICT_FUNCTIONAL = frozenset(
    {
        "39b3fd46-a448-49c5-bc26-303b0a4f4497",
        "a8b305ed-b342-4e12-9ee2-47ab29db6ea2",
        "3cb5b76e-c803-4a44-b621-e38fafe56211",
    }
)

_REQUIRED_QUESTION_KEYS = frozenset(
    {
        "schema_version",
        "question_version_id",
        "baseline_retrieval_method",
        "proposed_retrieval_method",
        "confidence_class",
        "candidate_count",
        "qualified_count_v1",
        "structural_candidate_count",
        "candidates",
    }
)

_REQUIRED_CANDIDATE_KEYS = frozenset(
    {
        "title",
        "resource_type",
        "relevance_score",
        "applicable_threshold",
        "l1_structural_guards_pass",
        "l2_relevance_gate_pass",
        "qualified_v1",
        "rejection_reason",
        "match_reasons",
    }
)


def _load_fixture() -> dict:
    with open(_FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


class TestShadowClassificationFromFrozenReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _load_fixture()
        cls.question_records = {
            str(record["question_version_id"]): record
            for record in cls.fixture["questions"]
        }
        cls.classifications = {
            question_version_id: classify_question_shadow_from_replay_record(record)
            for question_version_id, record in sorted(cls.question_records.items())
        }

    def test_all_ten_questions_classified(self):
        self.assertEqual(len(self.classifications), 10)
        self.assertEqual(set(self.classifications), set(self.question_records))

    def test_all_two_hundred_fifty_candidates_represented_once(self):
        total = 0
        for question_version_id, result in self.classifications.items():
            record = self.question_records[question_version_id]
            with self.subTest(question_version_id=question_version_id):
                self.assertEqual(result["candidate_count"], len(record["candidates"]))
                self.assertEqual(len(result["candidates"]), len(record["candidates"]))
                total += len(result["candidates"])
        self.assertEqual(total, 250)

    def test_output_shape(self):
        for question_version_id, result in self.classifications.items():
            with self.subTest(question_version_id=question_version_id):
                self.assertEqual(_REQUIRED_QUESTION_KEYS, set(result.keys()))
                self.assertEqual(result["schema_version"], SHADOW_CLASSIFICATION_SCHEMA_VERSION)
                self.assertEqual(result["question_version_id"], question_version_id)
                self.assertEqual(result["baseline_retrieval_method"], RETRIEVAL_METHOD)
                self.assertEqual(result["proposed_retrieval_method"], PROPOSED_RETRIEVAL_METHOD)
                for candidate in result["candidates"]:
                    self.assertEqual(_REQUIRED_CANDIDATE_KEYS, set(candidate.keys()))

    def test_strict_functional_questions_are_v1_sufficient(self):
        for question_version_id in sorted(_STRICT_FUNCTIONAL):
            with self.subTest(question_version_id=question_version_id):
                result = self.classifications[question_version_id]
                self.assertEqual(result["confidence_class"], CONFIDENCE_CLASS_V1_SUFFICIENT)
                self.assertGreater(result["qualified_count_v1"], 0)

    def test_qualified_v1_matches_frozen_replay_baseline(self):
        for question_version_id, record in self.question_records.items():
            result = self.classifications[question_version_id]
            with self.subTest(question_version_id=question_version_id):
                for candidate_record, candidate_result in zip(
                    record["candidates"], result["candidates"]
                ):
                    replay = replay_bm25_candidate_from_record(
                        record, candidate_record
                    )
                    self.assertEqual(
                        candidate_result["qualified_v1"],
                        replay["qualified"],
                    )
                    self.assertEqual(
                        candidate_result["relevance_score"],
                        replay["relevance_score"],
                    )
                    self.assertEqual(
                        candidate_result["applicable_threshold"],
                        replay["applicable_threshold"],
                    )
                    self.assertEqual(
                        candidate_result["rejection_reason"],
                        replay["rejection_reason"],
                    )
                    self.assertEqual(
                        candidate_result["match_reasons"],
                        replay["match_reasons"],
                    )

    def test_l1_l2_layers_are_consistent_with_qualified_v1(self):
        for question_version_id, result in self.classifications.items():
            with self.subTest(question_version_id=question_version_id):
                for candidate in result["candidates"]:
                    if candidate["qualified_v1"]:
                        self.assertTrue(candidate["l1_structural_guards_pass"])
                        self.assertTrue(candidate["l2_relevance_gate_pass"])
                    if not candidate["l1_structural_guards_pass"]:
                        self.assertFalse(candidate["qualified_v1"])
                    if not candidate["l2_relevance_gate_pass"]:
                        self.assertFalse(candidate["qualified_v1"])

    def test_confidence_class_rules(self):
        for question_version_id, result in self.classifications.items():
            with self.subTest(question_version_id=question_version_id):
                if result["qualified_count_v1"] > 0:
                    expected = CONFIDENCE_CLASS_V1_SUFFICIENT
                elif result["structural_candidate_count"] > 0:
                    expected = CONFIDENCE_CLASS_SEMANTIC_REVIEW
                else:
                    expected = CONFIDENCE_CLASS_NO_STRUCTURAL
                self.assertEqual(result["confidence_class"], expected)

    def test_structural_candidate_count_matches_l1_passes(self):
        for question_version_id, result in self.classifications.items():
            l1_count = sum(
                1 for candidate in result["candidates"] if candidate["l1_structural_guards_pass"]
            )
            with self.subTest(question_version_id=question_version_id):
                self.assertEqual(result["structural_candidate_count"], l1_count)

    def test_classification_is_byte_for_byte_deterministic(self):
        payloads: list[str] = []
        for question_version_id in sorted(self.question_records):
            first = classify_question_shadow_from_replay_record(
                self.question_records[question_version_id]
            )
            second = classify_question_shadow_from_replay_record(
                self.question_records[question_version_id]
            )
            first_payload = dumps_shadow_classification(first)
            second_payload = dumps_shadow_classification(second)
            self.assertEqual(first_payload, second_payload)
            payloads.append(first_payload)
        self.assertEqual(len(set(payloads)), len(payloads))


class TestShadowClassificationReport(unittest.TestCase):
    """Emit classification summary used by manual verification."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _load_fixture()

    def test_print_classification_summary(self):
        summary: list[str] = []
        for record in sorted(
            self.fixture["questions"], key=lambda item: item["question_version_id"]
        ):
            result = classify_question_shadow_from_replay_record(record)
            summary.append(
                f"{result['question_version_id']}: {result['confidence_class']} "
                f"(qualified_v1={result['qualified_count_v1']}, "
                f"structural={result['structural_candidate_count']})"
            )
        # Keep one assertion so unittest reports pass; summary is for local runs.
        self.assertEqual(len(summary), 10)


if __name__ == "__main__":
    unittest.main()
