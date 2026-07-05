"""
Tests for V58-QUALITY-03A dual-engine quality benchmark harness.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.quality_benchmark import (
    BENCHMARK_VERSION,
    DEFAULT_FIXTURE_PATH,
    ENGINE_LEGACY,
    ENGINE_V48,
    BenchmarkFixtureError,
    LegacyBenchmarkAdapter,
    V48BenchmarkAdapter,
    compute_benchmark_metrics,
    dumps_run_report,
    load_benchmark_fixture,
    run_quality_benchmark,
    serialize_run_report,
)
from workers.run_quality_benchmark import main as benchmark_main


def _load_default_fixture() -> dict:
    return load_benchmark_fixture(DEFAULT_FIXTURE_PATH)


class TestBenchmarkFixture(unittest.TestCase):

    def test_default_fixture_loads_eight_cases(self):
        fixture = _load_default_fixture()
        self.assertEqual(fixture["benchmark_version"], BENCHMARK_VERSION)
        self.assertEqual(len(fixture["cases"]), 8)

    def test_fixture_documents_synthetic_evidence(self):
        fixture = _load_default_fixture()
        note = fixture["fixture_metadata"]["evidence_note"].lower()
        self.assertIn("synthetic", note)
        self.assertIn("not official", note)

    def test_duplicate_case_id_rejected(self):
        fixture = _load_default_fixture()
        broken = dict(fixture)
        broken["cases"] = [fixture["cases"][0], fixture["cases"][0]]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(broken, handle)
            path = handle.name
        try:
            with self.assertRaises(BenchmarkFixtureError):
                load_benchmark_fixture(path)
        finally:
            os.remove(path)

    def test_missing_required_field_rejected(self):
        payload = {
            "benchmark_version": BENCHMARK_VERSION,
            "cases": [{
                "case_id": "broken-case",
                "benchmark_version": BENCHMARK_VERSION,
                "certification": "Administrator",
                "domain": "Security",
                "defect_category": "none",
                "known_good": True,
                "expected_correct_option_labels": ["A"],
                "expected_finding_codes": [],
                "reviewer_label": {"known_good": True, "expected_finding_codes": []},
                "resource_snapshot": {
                    "chunks": [{
                        "resource_chunk_id": "cccccccc-0001-0001-0001-000000000001",
                        "chunk_index": 0,
                        "chunk_text": "Synthetic evidence.",
                    }]
                },
            }],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            path = handle.name
        try:
            with self.assertRaises(BenchmarkFixtureError):
                load_benchmark_fixture(path)
        finally:
            os.remove(path)

    def test_benchmark_version_mismatch_rejected(self):
        fixture = _load_default_fixture()
        broken = dict(fixture)
        case = dict(fixture["cases"][0])
        case["benchmark_version"] = "wrong-version"
        broken["cases"] = [case]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(broken, handle)
            path = handle.name
        try:
            with self.assertRaises(BenchmarkFixtureError):
                load_benchmark_fixture(path)
        finally:
            os.remove(path)


class TestLegacyBenchmarkAdapter(unittest.TestCase):

    def setUp(self):
        self.fixture = _load_default_fixture()
        self.adapter = LegacyBenchmarkAdapter()

    def test_known_good_has_no_blocking_findings(self):
        case = next(c for c in self.fixture["cases"] if c["case_id"] == "harness-001-known-good")
        result = self.adapter.evaluate(case, ruleset_version="1.0.0")
        self.assertTrue(result.known_good)
        self.assertEqual(result.blocking_count, 0)
        self.assertFalse(result.false_rejection)
        self.assertTrue(result.detection_success)

    def test_wrong_answer_key_detected(self):
        case = next(c for c in self.fixture["cases"] if c["case_id"] == "harness-002-wrong-answer-key")
        result = self.adapter.evaluate(case, ruleset_version="1.0.0")
        self.assertIn("WRONG_ANSWER_KEY", result.finding_codes)
        self.assertEqual(result.blocking_count, 1)
        self.assertTrue(result.detection_success)
        self.assertFalse(result.false_approval)

    def test_explanation_defect_from_deterministic_path(self):
        case = next(
            c for c in self.fixture["cases"] if c["case_id"] == "harness-006-explanation-defect"
        )
        result = self.adapter.evaluate(case, ruleset_version="1.0.0")
        self.assertIn("EXPLANATION_MISSING", result.finding_codes)
        self.assertEqual(result.blocking_count, 1)
        self.assertTrue(result.detection_success)


class TestV48BenchmarkAdapter(unittest.TestCase):

    def setUp(self):
        self.fixture = _load_default_fixture()
        self.adapter = V48BenchmarkAdapter()

    def test_known_good_passes_without_findings(self):
        case = next(c for c in self.fixture["cases"] if c["case_id"] == "harness-001-known-good")
        result = self.adapter.evaluate(case)
        self.assertEqual(result.finding_codes, [])
        self.assertFalse(result.false_rejection)

    def test_weak_distractors_reported_as_warning(self):
        case = next(
            c for c in self.fixture["cases"] if c["case_id"] == "harness-005-weak-distractors"
        )
        result = self.adapter.evaluate(case)
        self.assertIn("WEAK_DISTRACTORS", result.finding_codes)
        self.assertEqual(result.warning_count, 1)
        self.assertEqual(result.blocking_count, 0)
        self.assertTrue(result.detection_success)


class TestBenchmarkMetrics(unittest.TestCase):

    def setUp(self):
        self.fixture = _load_default_fixture()

    def test_metrics_distinguish_false_approval_and_rejection(self):
        legacy_report = run_quality_benchmark(
            self.fixture,
            ENGINE_LEGACY,
            execution_timestamp="2026-07-05T18:00:00+00:00",
        )
        metrics = legacy_report.metrics
        self.assertEqual(metrics.total_cases, 8)
        self.assertEqual(metrics.known_good_cases, 2)
        self.assertEqual(metrics.defective_cases, 6)
        self.assertEqual(metrics.false_approvals, 0)
        self.assertEqual(metrics.false_rejections, 0)
        self.assertEqual(metrics.overall_recall_detected, 6)
        self.assertEqual(metrics.overall_recall_total, 6)
        self.assertEqual(metrics.blocking_category_detected, 3)
        self.assertEqual(metrics.blocking_category_total, 3)

    def test_recall_by_defect_category_includes_raw_counts(self):
        report = run_quality_benchmark(
            self.fixture,
            ENGINE_V48,
            execution_timestamp="2026-07-05T18:00:00+00:00",
        )
        ambiguity = report.metrics.recall_by_defect_category["ambiguity"]
        self.assertEqual(ambiguity["detected"], 2)
        self.assertEqual(ambiguity["total"], 2)
        self.assertEqual(ambiguity["recall"], 1.0)
        self.assertIn("2/2", ambiguity["note"])

    def test_reviewer_agreement_counts_dual_label_cases(self):
        report = run_quality_benchmark(
            self.fixture,
            ENGINE_LEGACY,
            execution_timestamp="2026-07-05T18:00:00+00:00",
        )
        metrics = report.metrics
        self.assertEqual(metrics.reviewer_agreement_cases, 2)
        self.assertEqual(metrics.reviewer_agreement_matches, 1)
        self.assertEqual(metrics.reviewer_agreement_rate, 0.5)
        self.assertIn("1/2", metrics.reviewer_agreement_note)

    def test_zero_division_rates_are_none_with_notes(self):
        metrics = compute_benchmark_metrics([], [])
        self.assertIsNone(metrics.false_approval_rate)
        self.assertEqual(metrics.false_approval_note, "0/0 defective cases missed")
        self.assertIsNone(metrics.reviewer_agreement_rate)
        self.assertEqual(metrics.reviewer_agreement_note, "0/0 dual-reviewer label pairs agreeing")


class TestBenchmarkSerialization(unittest.TestCase):

    def test_run_report_is_json_serializable_and_deterministic(self):
        fixture = _load_default_fixture()
        report = run_quality_benchmark(
            fixture,
            ENGINE_LEGACY,
            execution_timestamp="2026-07-05T18:00:00+00:00",
        )
        first = dumps_run_report(report)
        second = dumps_run_report(report)
        self.assertEqual(first, second)
        json.loads(first)

        payload = serialize_run_report(report)
        self.assertEqual(payload["engine"], ENGINE_LEGACY)
        self.assertEqual(payload["case_count"], 8)
        self.assertEqual(payload["case_results"][0]["case_id"], "harness-001-known-good")


class TestBenchmarkSafetyGate(unittest.TestCase):

    def test_main_runs_mock_mode_without_live_flag(self):
        with patch(
            "workers.run_quality_benchmark._running_under_pytest",
            return_value=False,
        ):
            code = benchmark_main(["--engine", "legacy"])
        self.assertEqual(code, 0)

    def test_main_refuses_under_pytest(self):
        with patch(
            "workers.run_quality_benchmark._running_under_pytest",
            return_value=True,
        ):
            code = benchmark_main([])
        self.assertEqual(code, 2)

    def test_main_refuses_live_without_authorization(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CERTBOUND_ALLOW_LIVE_AI_TEST", None)
            with patch(
                "workers.run_quality_benchmark._running_under_pytest",
                return_value=False,
            ):
                code = benchmark_main(["--engine", "legacy", "--live"])
        self.assertEqual(code, 1)

    def test_main_refuses_live_even_with_authorization_in_this_task(self):
        with patch.dict(os.environ, {"CERTBOUND_ALLOW_LIVE_AI_TEST": "1"}, clear=False):
            with patch(
                "workers.run_quality_benchmark._running_under_pytest",
                return_value=False,
            ):
                code = benchmark_main(["--engine", "v48", "--live"])
        self.assertEqual(code, 1)


class TestBenchmarkRun(unittest.TestCase):

    def test_both_engines_evaluate_same_fixture(self):
        fixture = _load_default_fixture()
        legacy = run_quality_benchmark(
            fixture,
            ENGINE_LEGACY,
            execution_timestamp="2026-07-05T18:00:00+00:00",
        )
        v48 = run_quality_benchmark(
            fixture,
            ENGINE_V48,
            execution_timestamp="2026-07-05T18:00:00+00:00",
        )
        self.assertEqual(legacy.case_count, v48.case_count)
        self.assertEqual(legacy.engine, ENGINE_LEGACY)
        self.assertEqual(v48.engine, ENGINE_V48)


if __name__ == "__main__":
    unittest.main()
