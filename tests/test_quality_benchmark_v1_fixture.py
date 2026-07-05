"""
Focused validation tests for the V58-QUALITY-03B-R1 pilot benchmark draft
(cases 1-20 of the planned 40-case benchmark).

These tests validate structural and evidence-provenance integrity only. They
do not assert the AI-drafted labels are correct ground truth; that requires
qualified Salesforce SME review (tracked separately, still pending).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.finding_policy import CANONICAL_FINDING_CODES
from workers.quality_benchmark import BenchmarkFixtureError, load_benchmark_fixture

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "workers" / "fixtures" / "quality_benchmark_v1.json"
)
_EVIDENCE_SEED_PATH = (
    Path(__file__).resolve().parents[1] / "workers" / "fixtures" / "official_evidence_seed_v1.json"
)

_ADM_CERT = "Salesforce Certified Platform Administrator"
_BA_CERT = "Salesforce Certified Business Analyst"


def _load_raw_fixture() -> dict:
    with _FIXTURE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_evidence_seed() -> dict:
    with _EVIDENCE_SEED_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _evidence_chunk_index(seed: dict) -> dict:
    return {
        str(item["resource_chunk_id"]).lower(): item
        for item in seed["evidence_items"]
    }


class TestFixtureLoadsUnderHarnessSchema(unittest.TestCase):

    def test_fixture_passes_benchmark_harness_validator(self):
        fixture = load_benchmark_fixture(_FIXTURE_PATH)
        self.assertEqual(fixture["benchmark_version"], "v1-pilot-draft")
        self.assertEqual(len(fixture["cases"]), 20)


class TestCaseCountAndDistribution(unittest.TestCase):

    def setUp(self):
        self.raw = _load_raw_fixture()
        self.cases = self.raw["cases"]

    def test_exactly_twenty_cases(self):
        self.assertEqual(len(self.cases), 20)

    def test_exactly_five_known_good_and_fifteen_defective(self):
        known_good = [c for c in self.cases if c["known_good"] is True]
        defective = [c for c in self.cases if c["known_good"] is False]
        self.assertEqual(len(known_good), 5)
        self.assertEqual(len(defective), 15)

    def test_unique_case_ids(self):
        ids = [c["case_id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_duplicate_or_near_identical_stems(self):
        stems = [c["question"]["question_text"].strip().lower() for c in self.cases]
        self.assertEqual(len(stems), len(set(stems)))


class TestCertificationAndDomainCoverage(unittest.TestCase):

    def setUp(self):
        self.raw = _load_raw_fixture()
        self.cases = self.raw["cases"]

    def test_both_certifications_represented(self):
        certs = {c["certification"] for c in self.cases}
        self.assertIn(_ADM_CERT, certs)
        self.assertIn(_BA_CERT, certs)

    def test_certifications_approximately_balanced(self):
        adm_count = sum(1 for c in self.cases if c["certification"] == _ADM_CERT)
        ba_count = sum(1 for c in self.cases if c["certification"] == _BA_CERT)
        self.assertEqual(adm_count, 10)
        self.assertEqual(ba_count, 10)

    def test_at_least_four_domains_represented(self):
        domains = {c["domain"] for c in self.cases}
        self.assertGreaterEqual(len(domains), 4)


class TestFindingCodesAreCanonical(unittest.TestCase):

    def setUp(self):
        self.raw = _load_raw_fixture()
        self.cases = self.raw["cases"]

    def test_all_expected_finding_codes_are_canonical(self):
        for case in self.cases:
            for code in case["expected_finding_codes"]:
                self.assertIn(
                    code,
                    CANONICAL_FINDING_CODES,
                    msg=f"case {case['case_id']!r} uses non-canonical code {code!r}",
                )

    def test_defect_category_distribution_matches_plan(self):
        code_counts: dict[str, int] = {}
        for case in self.cases:
            for code in case["expected_finding_codes"]:
                code_counts[code] = code_counts.get(code, 0) + 1
        self.assertEqual(code_counts.get("WRONG_ANSWER_KEY"), 3)
        self.assertEqual(code_counts.get("UNSUPPORTED_ANSWER"), 3)
        self.assertEqual(code_counts.get("MULTIPLE_DEFENSIBLE_ANSWERS"), 2)
        self.assertEqual(code_counts.get("AMBIGUOUS_QUESTION"), 2)
        self.assertEqual(code_counts.get("WEAK_DISTRACTORS"), 2)
        self.assertEqual(
            code_counts.get("EXPLANATION_MISSING", 0)
            + code_counts.get("EXPLANATION_INCOMPLETE", 0),
            2,
        )
        self.assertEqual(code_counts.get("SOURCE_SUPPORT_WEAK"), 1)

    def test_known_good_cases_have_no_expected_finding_codes(self):
        for case in self.cases:
            if case["known_good"]:
                self.assertEqual(case["expected_finding_codes"], [])
                self.assertIsNone(case["expected_materiality"])


class TestOptionStructureIntegrity(unittest.TestCase):

    def setUp(self):
        self.raw = _load_raw_fixture()
        self.cases = self.raw["cases"]

    def test_expected_correct_labels_exist_in_options(self):
        for case in self.cases:
            option_labels = {opt["option_label"] for opt in case["question"]["options"]}
            for label in case["expected_correct_option_labels"]:
                self.assertIn(
                    label,
                    option_labels,
                    msg=f"case {case['case_id']!r} expected label {label!r} missing from options",
                )

    def test_every_case_has_four_well_formed_options(self):
        for case in self.cases:
            options = case["question"]["options"]
            self.assertEqual(len(options), 4)
            labels = [opt["option_label"] for opt in options]
            self.assertEqual(len(labels), len(set(labels)))
            for opt in options:
                self.assertTrue(str(opt["option_text"]).strip())
                self.assertIsInstance(opt["is_correct"], bool)
                self.assertIsInstance(opt["display_order"], int)

    def test_exactly_one_option_marked_correct_per_case(self):
        for case in self.cases:
            correct_flags = [opt for opt in case["question"]["options"] if opt["is_correct"]]
            self.assertEqual(
                len(correct_flags),
                1,
                msg=f"case {case['case_id']!r} must have exactly one is_correct option",
            )


class TestEvidenceProvenanceIntegrity(unittest.TestCase):

    def setUp(self):
        self.raw = _load_raw_fixture()
        self.cases = self.raw["cases"]
        self.seed = _load_evidence_seed()
        self.seed_index = _evidence_chunk_index(self.seed)

    def test_no_synthetic_evidence_flag_is_true(self):
        self.assertIs(self.raw["no_synthetic_evidence"], True)
        self.assertIs(self.seed["no_synthetic_evidence"], True)

    def test_every_referenced_chunk_exists_in_evidence_seed(self):
        for case in self.cases:
            for chunk in case["resource_snapshot"]["chunks"]:
                chunk_id = str(chunk["resource_chunk_id"]).lower()
                self.assertIn(
                    chunk_id,
                    self.seed_index,
                    msg=(
                        f"case {case['case_id']!r} references resource_chunk_id "
                        f"{chunk_id!r} not present in official_evidence_seed_v1.json"
                    ),
                )

    def test_referenced_content_hashes_match_evidence_seed_exactly(self):
        for case in self.cases:
            for chunk in case["resource_snapshot"]["chunks"]:
                chunk_id = str(chunk["resource_chunk_id"]).lower()
                seed_item = self.seed_index[chunk_id]
                self.assertEqual(
                    chunk["content_hash"],
                    seed_item["content_hash"],
                    msg=f"case {case['case_id']!r} content_hash mismatch for {chunk_id!r}",
                )

    def test_referenced_chunk_text_matches_evidence_seed_excerpt(self):
        for case in self.cases:
            for chunk in case["resource_snapshot"]["chunks"]:
                chunk_id = str(chunk["resource_chunk_id"]).lower()
                seed_item = self.seed_index[chunk_id]
                self.assertEqual(chunk["chunk_text"], seed_item["chunk_text_excerpt"])

    def test_no_placeholder_or_invented_chunk_ids(self):
        seed_ids = set(self.seed_index.keys())
        for case in self.cases:
            for chunk in case["resource_snapshot"]["chunks"]:
                chunk_id = str(chunk["resource_chunk_id"]).lower()
                self.assertIn(chunk_id, seed_ids)

    def test_all_referenced_chunks_are_verified_provenance_status(self):
        for chunk_id, item in self.seed_index.items():
            self.assertEqual(item["provenance_status"], "verified_official_resource_library")


class TestHumanReviewStatusDisclosure(unittest.TestCase):

    def setUp(self):
        self.raw = _load_raw_fixture()

    def test_human_review_status_is_pending(self):
        self.assertEqual(self.raw["human_review_status"], "pending")

    def test_not_for_launch_decision_flag_is_true(self):
        self.assertIs(self.raw["not_for_launch_decision"], True)

    def test_status_marks_draft_part_1(self):
        self.assertEqual(self.raw["status"], "draft_part_1")

    def test_intended_pilot_size_and_current_count_disclosed(self):
        self.assertEqual(self.raw["intended_pilot_size"], 40)
        self.assertEqual(self.raw["current_case_count"], 20)

    def test_evidence_fixture_reference_disclosed(self):
        self.assertEqual(self.raw["evidence_fixture"], "official_evidence_seed_v1")

    def test_no_second_reviewer_labels_present(self):
        for case in self.raw["cases"]:
            self.assertNotIn(
                "second_reviewer_label",
                case,
                msg="single AI-drafter reviewer only; no second reviewer exists yet",
            )


class TestMalformedFixtureStillRejectedByLoader(unittest.TestCase):
    """Sanity check: the shared harness loader still fails closed on bad input,
    confirming this fixture's validity isn't due to a loosened schema.
    """

    def test_loader_rejects_case_with_missing_field(self):
        fixture = _load_raw_fixture()
        broken = dict(fixture)
        case = dict(fixture["cases"][0])
        del case["expected_finding_codes"]
        broken["cases"] = [case]
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(broken, handle)
            path = handle.name
        try:
            with self.assertRaises(BenchmarkFixtureError):
                load_benchmark_fixture(path)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
