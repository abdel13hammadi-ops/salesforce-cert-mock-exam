"""
Tests for V58-QUALITY-03D SME review packet export/import workflow, including
the V58-QUALITY-03D-R1 integrity hardening (confidence/correction/notes
requirements, unresolved second-review gating) and V58-QUALITY-03D-R2
provenance hardening (immutable-field validation, source-fixture hashing,
reviewer provenance, rejected-case gating).
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.benchmark_sme_review import (
    CLEAR_TOKEN,
    CSV_COLUMNS,
    IMMUTABLE_CONTEXT_COLUMNS,
    SME_EDITABLE_COLUMNS,
    BenchmarkSmeReviewError,
    SmeReviewExportError,
    SmeReviewImportError,
    build_export_rows,
    build_reviewed_fixture,
    compute_source_fixture_sha256,
    load_source_fixture,
    read_sme_review_csv,
    validate_sme_review_rows,
    write_export_csv,
    write_reviewed_fixture,
)
from workers.quality_benchmark import DEFAULT_FIXTURE_PATH as _  # noqa: F401  (sanity import)
from workers.quality_benchmark import load_benchmark_fixture
from workers.finding_policy import CANONICAL_FINDING_CODES

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_FIXTURE_PATH = _REPO_ROOT / "workers" / "fixtures" / "quality_benchmark_v1.json"
_SOURCE_HASH = compute_source_fixture_sha256(_SOURCE_FIXTURE_PATH)


def _blank_row_for(case_id: str, rows: list[dict]) -> dict:
    for row in rows:
        if row["case_id"] == case_id:
            return dict(row)
    raise KeyError(case_id)


def _approve_row(row: dict) -> dict:
    filled = dict(row)
    filled["sme_decision"] = "approve"
    filled["confidence"] = "high"
    return filled


def _correct_label_row(row: dict, *, answer: str = "", codes: str = "", notes: str = "fixing the key") -> dict:
    filled = dict(row)
    filled["sme_decision"] = "correct_label"
    filled["confidence"] = "medium"
    filled["sme_correct_answer"] = answer
    filled["sme_finding_codes"] = codes
    filled["sme_notes"] = notes
    return filled


def _reject_row(row: dict, *, notes: str = "case is unusable") -> dict:
    filled = dict(row)
    filled["sme_decision"] = "reject_case"
    filled["confidence"] = "high"
    filled["sme_notes"] = notes
    return filled


def _second_review_row(row: dict, *, notes: str = "not sure, needs another opinion") -> dict:
    filled = dict(row)
    filled["sme_decision"] = "needs_second_review"
    filled["confidence"] = "low"
    filled["sme_notes"] = notes
    return filled


class TestSourceFixtureUntouched(unittest.TestCase):
    """Guard: this test module must never mutate the source fixture on disk."""

    def test_source_fixture_hash_stable_across_module(self):
        before = _SOURCE_FIXTURE_PATH.read_bytes()
        fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        build_export_rows(fixture, source_fixture_sha256=_SOURCE_HASH)
        after = _SOURCE_FIXTURE_PATH.read_bytes()
        self.assertEqual(before, after)

    def test_source_fixture_hash_matches_independent_computation(self):
        import hashlib

        expected = hashlib.sha256(_SOURCE_FIXTURE_PATH.read_bytes()).hexdigest()
        self.assertEqual(_SOURCE_HASH, expected)


class TestExportRows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)

    def test_exports_all_forty_cases(self):
        self.assertEqual(len(self.rows), 40)

    def test_row_case_ids_match_fixture_case_ids(self):
        fixture_ids = {case["case_id"] for case in self.fixture["cases"]}
        row_ids = {row["case_id"] for row in self.rows}
        self.assertEqual(fixture_ids, row_ids)

    def test_every_row_has_exactly_the_expected_columns(self):
        for row in self.rows:
            self.assertEqual(set(row.keys()), set(CSV_COLUMNS))

    def test_sme_editable_columns_blank_in_export(self):
        for row in self.rows:
            for col in SME_EDITABLE_COLUMNS:
                self.assertEqual(row[col], "", f"{col} should be blank for {row['case_id']}")

    def test_source_fixture_sha256_populated_and_consistent_across_rows(self):
        hashes = {row["source_fixture_sha256"] for row in self.rows}
        self.assertEqual(hashes, {_SOURCE_HASH})

    def test_ai_derived_columns_populated_for_known_case(self):
        row = _blank_row_for("qbv1-001", self.rows)
        self.assertEqual(row["certification"], "Salesforce Certified Platform Administrator")
        self.assertIn("notification", row["question_text"].lower())
        self.assertEqual(row["option_a_text"], "In the notification bell, and delivered to the desktop or mobile app")
        self.assertEqual(row["stored_correct_answer"], "A")
        self.assertEqual(row["expected_evidence_supported_answer"], "A")
        self.assertEqual(row["known_good"], "true")
        self.assertEqual(row["expected_finding_codes"], "")
        self.assertIn("notification bell", row["evidence_excerpt"])
        self.assertTrue(row["canonical_url"].startswith("https://help.salesforce.com"))
        self.assertEqual(row["ai_drafted_label"], "known_good")

    def test_defective_case_has_populated_finding_codes_and_label(self):
        defective = next(
            row for row in self.rows
            if row["known_good"] == "false" and row["expected_finding_codes"]
        )
        self.assertIn("defective", defective["ai_drafted_label"])
        for code in defective["expected_finding_codes"].split("|"):
            self.assertIn(code, CANONICAL_FINDING_CODES)


class TestExportCsvRoundTrip(unittest.TestCase):

    def test_write_and_read_back_csv_preserves_rows(self):
        fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        rows = build_export_rows(fixture, source_fixture_sha256=_SOURCE_HASH)
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "review.csv"
            write_export_csv(rows, csv_path)
            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(list(reader.fieldnames), list(CSV_COLUMNS))
                read_rows = list(reader)
            self.assertEqual(len(read_rows), 40)

    def test_refuses_to_overwrite_existing_csv_without_flag(self):
        fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        rows = build_export_rows(fixture, source_fixture_sha256=_SOURCE_HASH)
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "review.csv"
            write_export_csv(rows, csv_path)
            with self.assertRaises(SmeReviewExportError):
                write_export_csv(rows, csv_path)
            # allow_overwrite=True should succeed
            write_export_csv(rows, csv_path, allow_overwrite=True)


class TestReadSmeReviewCsv(unittest.TestCase):

    def test_rejects_csv_with_wrong_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "bad.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["case_id", "totally_wrong_column"])
                writer.writerow(["qbv1-001", "x"])
            with self.assertRaises(SmeReviewImportError):
                read_sme_review_csv(csv_path)

    def test_missing_file_raises(self):
        with self.assertRaises(SmeReviewImportError):
            read_sme_review_csv("/no/such/path/review.csv")


class TestValidateSmeReviewRows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]

    def test_all_blank_rows_report_no_errors_and_incomplete(self):
        report = validate_sme_review_rows(self.rows, self.fixture)
        self.assertTrue(report.is_valid)
        self.assertFalse(report.is_complete)
        self.assertFalse(report.is_finalizable)
        self.assertEqual(len(report.missing_decision_case_ids), 40)
        self.assertEqual(report.completed_case_ids, [])
        self.assertIsNone(report.ai_sme_agreement_rate)
        self.assertEqual(report.source_fixture_sha256, _SOURCE_HASH)

    def test_unknown_case_id_rejected(self):
        rows = [dict(self.rows[0])]
        rows[0]["case_id"] = "qbv1-does-not-exist"
        rows[0]["sme_decision"] = "approve"
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("unknown case_id" in err for err in report.errors))

    def test_duplicate_case_id_rejected(self):
        first = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        duplicate = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        report = validate_sme_review_rows([first, duplicate], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("duplicate case_id" in err for err in report.errors))

    def test_invalid_sme_decision_rejected(self):
        row = _blank_row_for(self.case_ids[0], self.rows)
        row["sme_decision"] = "yolo_approve"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("invalid sme_decision" in err for err in report.errors))

    def test_invalid_confidence_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["confidence"] = "super_high"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("invalid confidence" in err for err in report.errors))

    def test_invalid_answer_label_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["sme_correct_answer"] = "Z"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("invalid sme_correct_answer label" in err for err in report.errors))

    def test_invalid_finding_code_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["sme_finding_codes"] = "NOT_A_REAL_CODE"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("invalid sme_finding_codes" in err for err in report.errors))

    def test_valid_finding_code_accepted(self):
        code = sorted(CANONICAL_FINDING_CODES)[0]
        row = _correct_label_row(_blank_row_for(self.case_ids[0], self.rows), codes=code)
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)

    def test_completed_decision_without_confidence_rejected(self):
        row = dict(_blank_row_for(self.case_ids[0], self.rows))
        row["sme_decision"] = "approve"
        row["confidence"] = ""
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("confidence is required" in err for err in report.errors))

    def test_approve_with_confidence_and_no_notes_is_valid(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)

    def test_correct_label_without_any_correction_rejected(self):
        row = _correct_label_row(_blank_row_for(self.case_ids[0], self.rows))
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("requires at least one of" in err for err in report.errors)
        )

    def test_correct_label_without_notes_rejected(self):
        row = _correct_label_row(
            _blank_row_for(self.case_ids[0], self.rows), answer="B", notes=""
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("requires sme_notes explaining the correction" in err for err in report.errors)
        )

    def test_correct_label_with_answer_and_notes_is_valid(self):
        row = _correct_label_row(_blank_row_for(self.case_ids[0], self.rows), answer="B")
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)

    def test_reject_case_without_notes_rejected(self):
        row = _reject_row(_blank_row_for(self.case_ids[0], self.rows), notes="")
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("sme_notes is required" in err and "reject_case" in err for err in report.errors)
        )

    def test_reject_case_with_notes_is_valid(self):
        row = _reject_row(_blank_row_for(self.case_ids[0], self.rows))
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)

    def test_needs_second_review_decision_without_notes_rejected(self):
        row = _second_review_row(_blank_row_for(self.case_ids[0], self.rows), notes="")
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any(
                "sme_notes is required" in err and "needs_second_review" in err
                for err in report.errors
            )
        )

    def test_needs_second_review_flag_true_without_notes_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["needs_second_review"] = "true"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any(
                "sme_notes is required" in err and "needs_second_review=true" in err
                for err in report.errors
            )
        )

    def test_needs_second_review_flag_true_with_notes_is_valid_but_unresolved(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["needs_second_review"] = "true"
        row["sme_notes"] = "want a second opinion just in case"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)
        self.assertIn(self.case_ids[0], report.unresolved_second_review_case_ids)

    def test_invalid_needs_second_review_value_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["needs_second_review"] = "maybe"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("invalid needs_second_review" in err for err in report.errors))

    def test_missing_case_id_field_rejected(self):
        row = dict(_blank_row_for(self.case_ids[0], self.rows))
        row["case_id"] = ""
        row["sme_decision"] = "approve"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)

    def test_agreement_and_disagreements_computed_from_real_decisions(self):
        rows = []
        for idx, case_id in enumerate(self.case_ids):
            row = dict(_blank_row_for(case_id, self.rows))
            if idx < 2:
                row["sme_decision"] = "approve"
                row["confidence"] = "high"
            elif idx == 2:
                row["sme_decision"] = "correct_label"
                row["sme_correct_answer"] = "B"
                row["confidence"] = "medium"
                row["sme_notes"] = "evidence actually supports option B, not the stored key"
            else:
                pass  # leave blank / not yet reviewed
            rows.append(row)
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_valid)
        self.assertFalse(report.is_complete)
        self.assertEqual(len(report.completed_case_ids), 3)
        self.assertAlmostEqual(report.ai_sme_agreement_rate, 2 / 3, places=5)
        self.assertEqual(len(report.disagreements), 1)
        self.assertEqual(report.disagreements[0]["case_id"], self.case_ids[2])
        self.assertEqual(len(report.missing_decision_case_ids), 37)

    def test_missing_case_row_entirely_is_reported_as_missing(self):
        rows = [_approve_row(_blank_row_for(cid, self.rows)) for cid in self.case_ids[1:]]
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertEqual(report.missing_case_ids, [self.case_ids[0]])
        self.assertFalse(report.is_complete)


class TestImmutableFieldValidation(unittest.TestCase):
    """V58-QUALITY-03D-R2: immutable CSV context columns must exactly match
    what the current source fixture would produce."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]

    def test_immutable_context_columns_cover_all_ai_derived_fields(self):
        # Sanity check that the constant used by production code and tests
        # actually excludes only case_id and the SME-editable columns.
        expected = set(CSV_COLUMNS) - set(SME_EDITABLE_COLUMNS) - {"case_id"}
        self.assertEqual(set(IMMUTABLE_CONTEXT_COLUMNS), expected)

    def test_unaltered_row_passes_immutable_check(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)

    def test_changed_question_text_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["question_text"] = row["question_text"] + " (edited by reviewer)"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("immutable field 'question_text'" in err for err in report.errors)
        )

    def test_changed_option_text_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["option_b_text"] = "A completely different, unrelated option"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("immutable field 'option_b_text'" in err for err in report.errors)
        )

    def test_changed_evidence_excerpt_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["evidence_excerpt"] = "fabricated evidence that was never in the source fixture"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("immutable field 'evidence_excerpt'" in err for err in report.errors)
        )

    def test_changed_ai_drafted_label_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["ai_drafted_label"] = "known_good_but_secretly_edited"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("immutable field 'ai_drafted_label'" in err for err in report.errors)
        )

    def test_changed_certification_or_domain_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["domain"] = "Something Else Entirely"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("immutable field 'domain'" in err for err in report.errors))

    def test_deleted_immutable_field_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["reviewer_rationale"] = ""
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("immutable field 'reviewer_rationale'" in err for err in report.errors)
        )

    def test_row_content_swapped_between_cases_rejected(self):
        """case_id correctly names case A, but the row's context fields are
        actually case B's — this must be rejected even though case_id alone
        is valid and known."""
        row_a = _blank_row_for(self.case_ids[0], self.rows)
        row_b = _blank_row_for(self.case_ids[1], self.rows)
        swapped = dict(row_b)
        swapped["case_id"] = row_a["case_id"]
        swapped["sme_decision"] = "approve"
        swapped["confidence"] = "high"
        report = validate_sme_review_rows([swapped], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("may belong to a different case" in err for err in report.errors))


class TestSourceHashValidation(unittest.TestCase):
    """V58-QUALITY-03D-R2: source_fixture_sha256 provenance checks."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]

    def test_valid_hash_matches_current_fixture(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)
        self.assertEqual(report.source_fixture_sha256, _SOURCE_HASH)

    def test_tampered_hash_column_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["source_fixture_sha256"] = "0" * 64
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("source_fixture_sha256 mismatch" in err for err in report.errors))

    def test_source_fixture_altered_after_export_rejected(self):
        real_fixture_bytes_before = _SOURCE_FIXTURE_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_fixture_path = Path(tmpdir) / "quality_benchmark_v1.json"
            temp_fixture_path.write_bytes(_SOURCE_FIXTURE_PATH.read_bytes())

            fixture = load_source_fixture(temp_fixture_path)
            original_hash = compute_source_fixture_sha256(temp_fixture_path)
            rows = build_export_rows(fixture, source_fixture_sha256=original_hash)
            approved_row = _approve_row(dict(rows[0]))

            # Simulate the source fixture drifting after the CSV was
            # exported (still valid JSON/schema, but different bytes).
            fixture_dict = json.loads(temp_fixture_path.read_text(encoding="utf-8"))
            fixture_dict["cases"][0]["reviewer_rationale"] += " (source drifted after export)"
            temp_fixture_path.write_text(json.dumps(fixture_dict, indent=2), encoding="utf-8")

            fixture_after_drift = load_source_fixture(temp_fixture_path)
            report = validate_sme_review_rows(
                [approved_row], fixture_after_drift, source_fixture_path=temp_fixture_path
            )
            self.assertFalse(report.is_valid)
            self.assertTrue(
                any("source_fixture_sha256 mismatch" in err for err in report.errors)
            )
            self.assertNotEqual(report.source_fixture_sha256, original_hash)

        # The real repository fixture must never be touched by this test.
        self.assertEqual(_SOURCE_FIXTURE_PATH.read_bytes(), real_fixture_bytes_before)


class TestBuildReviewedFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]

    def _all_approved_rows(self):
        return [_approve_row(_blank_row_for(cid, self.rows)) for cid in self.case_ids]

    def test_incomplete_review_cannot_build_reviewed_fixture(self):
        rows = self._all_approved_rows()[:-1]  # one case missing entirely
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertFalse(report.is_complete)
        with self.assertRaises(SmeReviewImportError):
            build_reviewed_fixture(self.fixture, rows, report, reviewer_id="sme-jdoe")

    def test_invalid_review_cannot_build_reviewed_fixture(self):
        rows = self._all_approved_rows()
        rows[0] = dict(rows[0])
        rows[0]["confidence"] = "extremely_high"
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertFalse(report.is_valid)
        with self.assertRaises(SmeReviewImportError):
            build_reviewed_fixture(self.fixture, rows, report, reviewer_id="sme-jdoe")

    def test_complete_valid_review_builds_reviewed_fixture(self):
        rows = self._all_approved_rows()
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_complete)
        self.assertTrue(report.is_finalizable)
        reviewed = build_reviewed_fixture(
            self.fixture, rows, report,
            reviewer_id="sme-jdoe",
            review_imported_at_utc="2026-07-05T20:00:00Z",
        )
        self.assertTrue(reviewed["sme_reviewed"])
        self.assertEqual(reviewed["sme_review_status"], "complete")
        self.assertEqual(len(reviewed["cases"]), 40)
        for case in reviewed["cases"]:
            self.assertEqual(case["sme_review"]["decision"], "approve")
        self.assertEqual(reviewed["sme_review_summary"]["reviewed_case_count"], 40)
        self.assertEqual(reviewed["sme_review_summary"]["ai_sme_agreement_rate"], 1.0)
        self.assertEqual(reviewed["sme_review_summary"]["unresolved_second_review_case_ids"], [])
        self.assertEqual(reviewed["sme_review_summary"]["rejected_case_ids"], [])

    def test_unresolved_needs_second_review_decision_blocks_finalization(self):
        rows = self._all_approved_rows()
        rows[0] = _second_review_row(rows[0])
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_valid)
        self.assertFalse(report.is_complete)
        self.assertFalse(report.is_finalizable)
        self.assertIn(self.case_ids[0], report.unresolved_second_review_case_ids)
        with self.assertRaises(SmeReviewImportError):
            build_reviewed_fixture(self.fixture, rows, report, reviewer_id="sme-jdoe")

    def test_unresolved_needs_second_review_flag_blocks_finalization_even_when_approved(self):
        rows = self._all_approved_rows()
        rows[0] = dict(rows[0])
        rows[0]["needs_second_review"] = "true"
        rows[0]["sme_notes"] = "want a colleague to double check this one"
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_valid)
        self.assertFalse(report.is_complete)
        self.assertIn(self.case_ids[0], report.unresolved_second_review_case_ids)
        with self.assertRaises(SmeReviewImportError):
            build_reviewed_fixture(self.fixture, rows, report, reviewer_id="sme-jdoe")

    def test_fully_adjudicated_approve_and_correct_label_review_finalizes(self):
        """A complete 40-case review using only approve and correct_label
        (no reject_case, no unresolved needs_second_review) must be able to
        produce a finalized reviewed fixture."""
        rows = self._all_approved_rows()
        rows[0] = _correct_label_row(rows[0], answer="B")
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_valid)
        self.assertTrue(report.is_complete)
        self.assertTrue(report.is_finalizable)
        reviewed = build_reviewed_fixture(
            self.fixture, rows, report,
            reviewer_id="sme-jdoe",
            review_imported_at_utc="2026-07-05T20:00:00Z",
        )
        self.assertTrue(reviewed["sme_reviewed"])
        self.assertEqual(reviewed["sme_review_status"], "complete")
        self.assertEqual(reviewed["cases"][0]["sme_review"]["decision"], "correct_label")
        self.assertEqual(reviewed["sme_review_summary"]["decision_counts"]["correct_label"], 1)
        self.assertEqual(reviewed["sme_review_summary"]["rejected_case_ids"], [])

    def test_building_reviewed_fixture_does_not_mutate_source_fixture_object(self):
        rows = self._all_approved_rows()
        report = validate_sme_review_rows(rows, self.fixture)
        before = json.dumps(self.fixture, sort_keys=True)
        build_reviewed_fixture(self.fixture, rows, report, reviewer_id="sme-jdoe")
        after = json.dumps(self.fixture, sort_keys=True)
        self.assertEqual(before, after)
        self.assertNotIn("sme_review", self.fixture["cases"][0])


class TestRejectedCaseBehavior(unittest.TestCase):
    """V58-QUALITY-03D-R2: reject_case completes a review but must not allow
    the benchmark to be finalized as trusted ground truth."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]

    def _all_approved_rows(self):
        return [_approve_row(_blank_row_for(cid, self.rows)) for cid in self.case_ids]

    def test_reject_case_completes_review_but_is_not_finalizable(self):
        rows = self._all_approved_rows()
        rows[0] = _reject_row(rows[0])
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_valid)
        self.assertTrue(report.is_complete)  # review is fully adjudicated...
        self.assertFalse(report.is_finalizable)  # ...but not trusted ground truth yet
        self.assertEqual(report.rejected_case_ids, [self.case_ids[0]])

    def test_reject_case_blocks_finalization(self):
        rows = self._all_approved_rows()
        rows[0] = _reject_row(rows[0])
        report = validate_sme_review_rows(rows, self.fixture)
        with self.assertRaises(SmeReviewImportError) as ctx:
            build_reviewed_fixture(self.fixture, rows, report, reviewer_id="sme-jdoe")
        self.assertIn("reject_case", str(ctx.exception))

    def test_rejected_cases_are_not_silently_dropped(self):
        """Even though the case is rejected, it must still appear in the
        rows/report accounting rather than vanish (size integrity)."""
        rows = self._all_approved_rows()
        rows[0] = _reject_row(rows[0])
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertEqual(len(rows), 40)
        self.assertIn(self.case_ids[0], report.completed_case_ids)
        self.assertEqual(len(report.completed_case_ids), 40)


class TestReviewerProvenance(unittest.TestCase):
    """V58-QUALITY-03D-R2: reviewer identifier + timestamp provenance."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]

    def _all_approved_rows(self):
        return [_approve_row(_blank_row_for(cid, self.rows)) for cid in self.case_ids]

    def test_missing_reviewer_id_rejected(self):
        rows = self._all_approved_rows()
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_finalizable)
        with self.assertRaises(SmeReviewImportError):
            build_reviewed_fixture(self.fixture, rows, report, reviewer_id="")
        with self.assertRaises(SmeReviewImportError):
            build_reviewed_fixture(self.fixture, rows, report, reviewer_id="   ")

    def test_reviewer_provenance_written_correctly(self):
        rows = self._all_approved_rows()
        report = validate_sme_review_rows(rows, self.fixture)
        reviewed = build_reviewed_fixture(
            self.fixture, rows, report,
            reviewer_id="sme-jdoe",
            review_imported_at_utc="2026-07-05T20:00:00Z",
        )
        self.assertEqual(reviewed["sme_reviewer_id"], "sme-jdoe")
        self.assertEqual(reviewed["source_fixture_sha256"], _SOURCE_HASH)
        self.assertEqual(reviewed["review_imported_at_utc"], "2026-07-05T20:00:00Z")
        summary = reviewed["sme_review_summary"]
        self.assertEqual(summary["sme_reviewer_id"], "sme-jdoe")
        self.assertEqual(summary["source_fixture_sha256"], _SOURCE_HASH)
        self.assertEqual(summary["review_imported_at_utc"], "2026-07-05T20:00:00Z")

    def test_reviewer_id_is_stripped(self):
        rows = self._all_approved_rows()
        report = validate_sme_review_rows(rows, self.fixture)
        reviewed = build_reviewed_fixture(
            self.fixture, rows, report, reviewer_id="  sme-jdoe  ",
            review_imported_at_utc="2026-07-05T20:00:00Z",
        )
        self.assertEqual(reviewed["sme_reviewer_id"], "sme-jdoe")

    def test_timestamp_auto_generated_when_not_supplied(self):
        rows = self._all_approved_rows()
        report = validate_sme_review_rows(rows, self.fixture)
        reviewed = build_reviewed_fixture(self.fixture, rows, report, reviewer_id="sme-jdoe")
        timestamp = reviewed["review_imported_at_utc"]
        parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNotNone(parsed)


class TestWriteReviewedFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]

    def _reviewed_payload(self):
        rows = [_approve_row(_blank_row_for(cid, self.rows)) for cid in self.case_ids]
        report = validate_sme_review_rows(rows, self.fixture)
        return build_reviewed_fixture(
            self.fixture, rows, report,
            reviewer_id="sme-jdoe",
            review_imported_at_utc="2026-07-05T20:00:00Z",
        )

    def test_refuses_to_write_over_source_fixture_path(self):
        payload = self._reviewed_payload()
        with self.assertRaises(SmeReviewImportError):
            write_reviewed_fixture(
                _SOURCE_FIXTURE_PATH,
                payload,
                source_fixture_path=_SOURCE_FIXTURE_PATH,
                allow_overwrite=True,
            )
        # Confirm the real source fixture file was never touched.
        reloaded = load_source_fixture(_SOURCE_FIXTURE_PATH)
        self.assertNotIn("sme_review", reloaded["cases"][0])

    def test_writes_to_new_path_and_refuses_overwrite_without_flag(self):
        payload = self._reviewed_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "reviewed.json"
            write_reviewed_fixture(
                out_path, payload, source_fixture_path=_SOURCE_FIXTURE_PATH
            )
            self.assertTrue(out_path.exists())
            with self.assertRaises(SmeReviewImportError):
                write_reviewed_fixture(
                    out_path, payload, source_fixture_path=_SOURCE_FIXTURE_PATH
                )
            write_reviewed_fixture(
                out_path,
                payload,
                source_fixture_path=_SOURCE_FIXTURE_PATH,
                allow_overwrite=True,
            )


class TestDecisionCoherence(unittest.TestCase):
    """V58-QUALITY-03D-R3: approve cannot carry corrections, and correct_label
    cannot be a no-op (must materially differ from the AI-drafted value)."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]

    def test_approve_with_answer_correction_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["sme_correct_answer"] = "B"
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("approve must not include" in err for err in report.errors)
        )

    def test_approve_with_finding_code_correction_rejected(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["sme_finding_codes"] = sorted(CANONICAL_FINDING_CODES)[0]
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("approve must not include" in err for err in report.errors)
        )

    def test_approve_with_no_corrections_is_still_valid(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)

    def test_correct_label_answer_matching_ai_value_is_noop_and_rejected(self):
        # qbv1-001's AI-drafted expected_correct_option_labels is ["A"].
        row = _correct_label_row(_blank_row_for("qbv1-001", self.rows), answer="A")
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("must materially differ" in err for err in report.errors))

    def test_correct_label_codes_matching_ai_value_is_noop_and_rejected(self):
        # qbv1-004's AI-drafted expected_finding_codes is ["WRONG_ANSWER_KEY"].
        row = _correct_label_row(_blank_row_for("qbv1-004", self.rows), codes="WRONG_ANSWER_KEY")
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("must materially differ" in err for err in report.errors))

    def test_correct_label_answer_and_codes_both_matching_ai_value_is_noop_and_rejected(self):
        row = _correct_label_row(
            _blank_row_for("qbv1-004", self.rows), answer="A", codes="WRONG_ANSWER_KEY"
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(any("must materially differ" in err for err in report.errors))

    def test_correct_label_answer_differs_from_ai_value_is_accepted(self):
        row = _correct_label_row(_blank_row_for("qbv1-001", self.rows), answer="B")
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)

    def test_correct_label_codes_differ_from_ai_value_is_accepted(self):
        row = _correct_label_row(_blank_row_for("qbv1-004", self.rows), codes="WEAK_DISTRACTORS")
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)

    def test_correct_label_codes_change_with_matching_answer_is_accepted(self):
        # Answer restated identically to the AI value, but the finding-code
        # correction is a real change — this is not a no-op overall.
        row = _correct_label_row(
            _blank_row_for("qbv1-004", self.rows), answer="A", codes="WEAK_DISTRACTORS"
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)


class TestEffectiveLabelResolution(unittest.TestCase):
    """V58-QUALITY-03D-R3: the finalized fixture's per-case effective label
    (the exact fields workers.quality_benchmark's loader/scoring consume)
    must reflect the SME's decision, with the original AI-drafted label
    preserved separately for provenance."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]
        cls.cases_by_id = {case["case_id"]: case for case in cls.fixture["cases"]}

    def _all_approved_rows(self):
        return [_approve_row(_blank_row_for(cid, self.rows)) for cid in self.case_ids]

    def _build(self, rows):
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_valid, report.errors)
        self.assertTrue(report.is_finalizable)
        return build_reviewed_fixture(
            self.fixture, rows, report,
            reviewer_id="sme-jdoe",
            review_imported_at_utc="2026-07-05T20:00:00Z",
        )

    def test_approve_keeps_ai_drafted_effective_label_unchanged(self):
        rows = self._all_approved_rows()
        reviewed = self._build(rows)
        ai_case = self.cases_by_id["qbv1-001"]
        reviewed_case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-001")
        self.assertEqual(
            reviewed_case["expected_correct_option_labels"],
            ai_case["expected_correct_option_labels"],
        )
        self.assertEqual(
            reviewed_case["expected_finding_codes"], ai_case["expected_finding_codes"]
        )
        self.assertEqual(reviewed_case["expected_materiality"], ai_case["expected_materiality"])
        self.assertEqual(reviewed_case["known_good"], ai_case["known_good"])
        self.assertEqual(reviewed_case["reviewer_label"], ai_case["reviewer_label"])

    def test_ai_drafted_reviewer_label_preserved_for_approve(self):
        rows = self._all_approved_rows()
        reviewed = self._build(rows)
        ai_case = self.cases_by_id["qbv1-001"]
        reviewed_case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-001")
        self.assertIn("ai_drafted_reviewer_label", reviewed_case)
        provenance = reviewed_case["ai_drafted_reviewer_label"]
        self.assertEqual(
            provenance["expected_correct_option_labels"],
            ai_case["expected_correct_option_labels"],
        )
        self.assertEqual(provenance["expected_finding_codes"], ai_case["expected_finding_codes"])
        self.assertEqual(provenance["known_good"], ai_case["known_good"])

    def test_correct_label_answer_only_replaces_labels_inherits_codes(self):
        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-001")
        rows[idx] = _correct_label_row(rows[idx], answer="B")
        reviewed = self._build(rows)
        ai_case = self.cases_by_id["qbv1-001"]
        reviewed_case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-001")

        self.assertEqual(reviewed_case["expected_correct_option_labels"], ["B"])
        # Codes correction left blank -> inherits the AI-drafted codes (empty).
        self.assertEqual(
            reviewed_case["expected_finding_codes"], ai_case["expected_finding_codes"]
        )
        self.assertEqual(reviewed_case["known_good"], True)
        self.assertIsNone(reviewed_case["expected_materiality"])
        self.assertEqual(
            reviewed_case["reviewer_label"],
            {"known_good": True, "expected_finding_codes": []},
        )
        # AI provenance preserved and distinct from the resolved effective label.
        self.assertEqual(
            reviewed_case["ai_drafted_reviewer_label"]["expected_correct_option_labels"], ["A"]
        )

    def test_correct_label_codes_only_replaces_codes_inherits_answer_and_recalculates_materiality(self):
        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-004")
        rows[idx] = _correct_label_row(rows[idx], codes="WEAK_DISTRACTORS")
        reviewed = self._build(rows)
        ai_case = self.cases_by_id["qbv1-004"]
        reviewed_case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-004")

        # Answer correction left blank -> inherits the AI-drafted answer label.
        self.assertEqual(
            reviewed_case["expected_correct_option_labels"],
            ai_case["expected_correct_option_labels"],
        )
        self.assertEqual(reviewed_case["expected_finding_codes"], ["WEAK_DISTRACTORS"])
        # AI drafted this case as WRONG_ANSWER_KEY/blocking; SME's corrected
        # code is warning-level, so materiality must be recalculated, not
        # inherited from the AI draft.
        self.assertEqual(ai_case["expected_materiality"], "blocking")
        self.assertEqual(reviewed_case["expected_materiality"], "warning")
        self.assertFalse(reviewed_case["known_good"])
        self.assertEqual(
            reviewed_case["reviewer_label"],
            {"known_good": False, "expected_finding_codes": ["WEAK_DISTRACTORS"]},
        )

    def test_correct_label_codes_upgraded_to_blocking_recalculates_materiality(self):
        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-009")
        rows[idx] = _correct_label_row(rows[idx], codes="WRONG_ANSWER_KEY")
        reviewed = self._build(rows)
        ai_case = self.cases_by_id["qbv1-009"]
        reviewed_case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-009")

        self.assertEqual(ai_case["expected_finding_codes"], ["WEAK_DISTRACTORS"])
        self.assertEqual(ai_case["expected_materiality"], "warning")
        self.assertEqual(reviewed_case["expected_finding_codes"], ["WRONG_ANSWER_KEY"])
        self.assertEqual(reviewed_case["expected_materiality"], "blocking")

    def test_correct_label_manual_csv_materiality_style_input_is_ignored(self):
        # There is no materiality column for the SME to fill in at all; this
        # test documents that materiality is always derived, never trusted
        # from anything resembling a manually supplied value.
        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-009")
        rows[idx] = _correct_label_row(rows[idx], codes="WRONG_ANSWER_KEY")
        reviewed = self._build(rows)
        reviewed_case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-009")
        self.assertEqual(
            reviewed_case["expected_materiality"],
            "blocking",
            "materiality must come from workers.finding_policy's canonical "
            "code-level policy applied to the resolved finding codes",
        )


class TestEndToEndLoaderResolution(unittest.TestCase):
    """V58-QUALITY-03D-R3 acceptance criterion: build a complete 40-case SME
    review that changes at least one answer label and at least one
    finding-code set, then load the finalized reviewed fixture using the
    *same loader* the benchmark harness uses, and confirm the harness-facing
    case label is the SME-resolved label, not the original AI label."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]

    def test_loader_exposes_sme_resolved_label_not_ai_label(self):
        rows = [_approve_row(_blank_row_for(cid, self.rows)) for cid in self.case_ids]

        answer_case_idx = self.case_ids.index("qbv1-001")
        rows[answer_case_idx] = _correct_label_row(rows[answer_case_idx], answer="B")

        codes_case_idx = self.case_ids.index("qbv1-009")
        rows[codes_case_idx] = _correct_label_row(rows[codes_case_idx], codes="WRONG_ANSWER_KEY")

        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_valid, report.errors)
        self.assertTrue(report.is_finalizable)
        reviewed = build_reviewed_fixture(
            self.fixture, rows, report,
            reviewer_id="sme-jdoe",
            review_imported_at_utc="2026-07-05T20:00:00Z",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "quality_benchmark_v1_sme_reviewed.json"
            write_reviewed_fixture(
                out_path, reviewed, source_fixture_path=_SOURCE_FIXTURE_PATH
            )

            # This is the exact function workers.quality_benchmark's benchmark
            # harness uses to load a fixture before running mock adapters.
            loaded = load_benchmark_fixture(out_path)

        loaded_cases_by_id = {case["case_id"]: case for case in loaded["cases"]}

        answer_case = loaded_cases_by_id["qbv1-001"]
        self.assertEqual(answer_case["expected_correct_option_labels"], ["B"])
        self.assertNotEqual(answer_case["expected_correct_option_labels"], ["A"])
        self.assertEqual(
            answer_case["ai_drafted_reviewer_label"]["expected_correct_option_labels"], ["A"]
        )

        codes_case = loaded_cases_by_id["qbv1-009"]
        self.assertEqual(codes_case["expected_finding_codes"], ["WRONG_ANSWER_KEY"])
        self.assertNotEqual(codes_case["expected_finding_codes"], ["WEAK_DISTRACTORS"])
        self.assertEqual(codes_case["expected_materiality"], "blocking")
        self.assertEqual(
            codes_case["ai_drafted_reviewer_label"]["expected_finding_codes"],
            ["WEAK_DISTRACTORS"],
        )

        # Confirm the loader's own schema validation actually accepted these
        # resolved fields (not just that our JSON happens to contain them):
        # expected_correct_option_labels must be valid option labels, and
        # reviewer_label must be structurally consistent with known_good /
        # expected_finding_codes for every one of the 40 cases.
        self.assertEqual(len(loaded["cases"]), 40)
        for case in loaded["cases"]:
            self.assertEqual(
                case["reviewer_label"]["known_good"], case["known_good"]
            )
            self.assertEqual(
                set(case["reviewer_label"]["expected_finding_codes"]),
                set(case["expected_finding_codes"]),
            )


class TestExportCliSafetyGate(unittest.TestCase):

    def test_export_main_refuses_under_pytest_by_default(self):
        from scripts.v58_export_benchmark_sme_review import main as export_main

        with tempfile.TemporaryDirectory() as tmpdir:
            code = export_main(["--output", str(Path(tmpdir) / "out.csv")])
        self.assertEqual(code, 2)

    def test_export_main_runs_when_pytest_gate_patched(self):
        from scripts import v58_export_benchmark_sme_review as export_script

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "out.csv"
            with patch.object(export_script, "_running_under_pytest", return_value=False):
                code = export_script.main(["--output", str(out_path)])
            self.assertEqual(code, 0)
            self.assertTrue(out_path.exists())
            with out_path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            self.assertEqual(len(rows), 40)
            for row in rows:
                for col in SME_EDITABLE_COLUMNS:
                    self.assertEqual(row[col], "")
                self.assertEqual(row["source_fixture_sha256"], _SOURCE_HASH)


class TestImportCliSafetyGate(unittest.TestCase):

    def _write_all_approved_csv(self, csv_path: Path):
        from scripts import v58_export_benchmark_sme_review as export_script

        with patch.object(export_script, "_running_under_pytest", return_value=False):
            export_script.main(["--output", str(csv_path)])
        with csv_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        for row in rows:
            row["sme_decision"] = "approve"
            row["confidence"] = "high"
        write_export_csv(rows, csv_path, allow_overwrite=True)

    def test_import_main_refuses_under_pytest_by_default(self):
        from scripts.v58_import_benchmark_sme_review import main as import_main

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "review.csv"
            self._write_all_approved_csv(csv_path)
            code = import_main(["--review", str(csv_path)])
        self.assertEqual(code, 2)

    def test_import_main_report_only_never_writes_fixture(self):
        from scripts import v58_import_benchmark_sme_review as import_script

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "review.csv"
            self._write_all_approved_csv(csv_path)
            out_path = Path(tmpdir) / "reviewed.json"
            with patch.object(import_script, "_running_under_pytest", return_value=False):
                code = import_script.main(
                    ["--review", str(csv_path), "--output", str(out_path), "--report-only"]
                )
            self.assertEqual(code, 0)
            self.assertFalse(out_path.exists())

    def test_import_main_incomplete_review_does_not_write_fixture(self):
        from scripts import v58_import_benchmark_sme_review as import_script

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "review.csv"
            self._write_all_approved_csv(csv_path)
            # Blank out one case's decision to simulate a partial review.
            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            rows[0]["sme_decision"] = ""
            rows[0]["confidence"] = ""
            write_export_csv(rows, csv_path, allow_overwrite=True)

            out_path = Path(tmpdir) / "reviewed.json"
            with patch.object(import_script, "_running_under_pytest", return_value=False):
                code = import_script.main(
                    ["--review", str(csv_path), "--output", str(out_path), "--reviewer-id", "sme-jdoe"]
                )
            self.assertEqual(code, 0)
            self.assertFalse(out_path.exists())

    def test_import_main_complete_review_writes_fixture(self):
        from scripts import v58_import_benchmark_sme_review as import_script

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "review.csv"
            self._write_all_approved_csv(csv_path)
            out_path = Path(tmpdir) / "reviewed.json"
            with patch.object(import_script, "_running_under_pytest", return_value=False):
                code = import_script.main(
                    ["--review", str(csv_path), "--output", str(out_path), "--reviewer-id", "sme-jdoe"]
                )
            self.assertEqual(code, 0)
            self.assertTrue(out_path.exists())
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["sme_reviewed"])
            self.assertEqual(len(payload["cases"]), 40)
            self.assertEqual(payload["sme_reviewer_id"], "sme-jdoe")
            self.assertEqual(payload["source_fixture_sha256"], _SOURCE_HASH)
            datetime.strptime(payload["review_imported_at_utc"], "%Y-%m-%dT%H:%M:%SZ")

    def test_import_main_missing_reviewer_id_blocks_finalization(self):
        from scripts import v58_import_benchmark_sme_review as import_script

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "review.csv"
            self._write_all_approved_csv(csv_path)
            out_path = Path(tmpdir) / "reviewed.json"
            with patch.object(import_script, "_running_under_pytest", return_value=False):
                code = import_script.main(["--review", str(csv_path), "--output", str(out_path)])
            self.assertEqual(code, 1)
            self.assertFalse(out_path.exists())

    def test_import_main_reject_case_blocks_finalization_with_explanatory_message(self):
        from scripts import v58_import_benchmark_sme_review as import_script

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "review.csv"
            self._write_all_approved_csv(csv_path)
            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            rows[0]["sme_decision"] = "reject_case"
            rows[0]["sme_notes"] = "not usable"
            write_export_csv(rows, csv_path, allow_overwrite=True)

            out_path = Path(tmpdir) / "reviewed.json"
            with patch.object(import_script, "_running_under_pytest", return_value=False):
                code = import_script.main(
                    ["--review", str(csv_path), "--output", str(out_path), "--reviewer-id", "sme-jdoe"]
                )
            self.assertEqual(code, 0)
            self.assertFalse(out_path.exists())

    def test_import_main_invalid_csv_rejected_with_nonzero_exit(self):
        from scripts import v58_import_benchmark_sme_review as import_script

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "review.csv"
            self._write_all_approved_csv(csv_path)
            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            rows[0]["case_id"] = "qbv1-totally-unknown"
            write_export_csv(rows, csv_path, allow_overwrite=True)

            out_path = Path(tmpdir) / "reviewed.json"
            with patch.object(import_script, "_running_under_pytest", return_value=False):
                code = import_script.main(["--review", str(csv_path), "--output", str(out_path)])
            self.assertEqual(code, 1)
            self.assertFalse(out_path.exists())


class TestClearToken(unittest.TestCase):
    """V58-SME-IMPORT-01: CLEAR control token in sme_finding_codes.

    Semantics:
      blank / ""     → inherit AI-drafted findings (unchanged behaviour)
      "CLEAR"        → replace AI-drafted findings with []
      "CODE1|CODE2"  → replace AI-drafted findings with that list

    qbv1-015 AI label: expected_correct_option_labels=["A","B"],
                       expected_finding_codes=["MULTIPLE_DEFENSIBLE_ANSWERS"]
    qbv1-017 AI label: expected_correct_option_labels=["A","B","C"],
                       expected_finding_codes=["AMBIGUOUS_QUESTION"]
    qbv1-001 AI label: expected_correct_option_labels=["A"],
                       expected_finding_codes=[] (known_good=true)
    """

    @classmethod
    def setUpClass(cls):
        cls.fixture = load_source_fixture(_SOURCE_FIXTURE_PATH)
        cls.rows = build_export_rows(cls.fixture, source_fixture_sha256=_SOURCE_HASH)
        cls.case_ids = [case["case_id"] for case in cls.fixture["cases"]]
        cls.cases_by_id = {case["case_id"]: case for case in cls.fixture["cases"]}

    def _all_approved_rows(self):
        return [_approve_row(_blank_row_for(cid, self.rows)) for cid in self.case_ids]

    def _build_complete(self, rows):
        report = validate_sme_review_rows(rows, self.fixture)
        self.assertTrue(report.is_valid, report.errors)
        self.assertTrue(report.is_finalizable)
        return build_reviewed_fixture(
            self.fixture, rows, report,
            reviewer_id="sme-jdoe",
            review_imported_at_utc="2026-07-05T20:00:00Z",
        )

    # ------------------------------------------------------------------
    # Acceptance criterion 1: blank still inherits
    # ------------------------------------------------------------------

    def test_blank_sme_finding_codes_still_inherits_ai_findings(self):
        row = _correct_label_row(
            _blank_row_for("qbv1-015", self.rows), answer="A", codes="", notes="only A is correct"
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid)
        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-015")
        rows[idx] = row
        reviewed = self._build_complete(rows)
        case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-015")
        # blank codes → inherits AI finding codes
        self.assertEqual(
            case["expected_finding_codes"],
            self.cases_by_id["qbv1-015"]["expected_finding_codes"],
        )
        self.assertFalse(case["known_good"])

    # ------------------------------------------------------------------
    # Acceptance criterion 2: CLEAR resolves to []
    # ------------------------------------------------------------------

    def test_clear_resolves_effective_finding_codes_to_empty_list(self):
        row = _correct_label_row(
            _blank_row_for("qbv1-015", self.rows),
            answer="A",
            codes=CLEAR_TOKEN,
            notes="only A is correct; no defect remains",
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid, report.errors)
        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-015")
        rows[idx] = row
        reviewed = self._build_complete(rows)
        case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-015")
        self.assertEqual(case["expected_finding_codes"], [])
        self.assertTrue(case["known_good"])
        self.assertIsNone(case["expected_materiality"])

    # ------------------------------------------------------------------
    # Acceptance criterion 3: CLEAR rejected for approve
    # ------------------------------------------------------------------

    def test_clear_rejected_for_approve(self):
        row = _approve_row(_blank_row_for(self.case_ids[0], self.rows))
        row["sme_finding_codes"] = CLEAR_TOKEN
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("approve must not include" in err for err in report.errors),
            report.errors,
        )

    # ------------------------------------------------------------------
    # Acceptance criterion 4: CLEAR rejected for reject_case
    # ------------------------------------------------------------------

    def test_clear_rejected_for_reject_case(self):
        row = _reject_row(_blank_row_for(self.case_ids[0], self.rows))
        row["sme_finding_codes"] = CLEAR_TOKEN
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("CLEAR" in err and "correct_label" in err for err in report.errors),
            report.errors,
        )

    # ------------------------------------------------------------------
    # Acceptance criterion 5: CLEAR rejected for needs_second_review
    # ------------------------------------------------------------------

    def test_clear_rejected_for_needs_second_review(self):
        row = _second_review_row(_blank_row_for(self.case_ids[0], self.rows))
        row["sme_finding_codes"] = CLEAR_TOKEN
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("CLEAR" in err and "correct_label" in err for err in report.errors),
            report.errors,
        )

    # ------------------------------------------------------------------
    # Acceptance criterion 6: CLEAR rejected when combined with a code
    # ------------------------------------------------------------------

    def test_clear_rejected_when_combined_with_finding_code(self):
        row = _correct_label_row(
            _blank_row_for("qbv1-015", self.rows),
            answer="A",
            codes="CLEAR|WRONG_ANSWER_KEY",
            notes="testing invalid combination",
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("CLEAR cannot be combined" in err for err in report.errors),
            report.errors,
        )

    def test_clear_rejected_when_code_precedes_clear(self):
        row = _correct_label_row(
            _blank_row_for("qbv1-015", self.rows),
            answer="A",
            codes="WEAK_DISTRACTORS|CLEAR",
            notes="testing invalid combination order",
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("CLEAR cannot be combined" in err for err in report.errors),
            report.errors,
        )

    # ------------------------------------------------------------------
    # Acceptance criterion 7: CLEAR rejected as no-op
    # ------------------------------------------------------------------

    def test_clear_rejected_when_ai_findings_already_empty_and_no_other_change(self):
        # qbv1-001 is known_good with expected_finding_codes=[].
        # CLEAR with no answer change → resolved_codes=[] = ai_codes=[] → no-op.
        self.assertEqual(self.cases_by_id["qbv1-001"]["expected_finding_codes"], [])
        row = _correct_label_row(
            _blank_row_for("qbv1-001", self.rows),
            answer="",
            codes=CLEAR_TOKEN,
            notes="AI findings already empty",
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("must materially differ" in err for err in report.errors),
            report.errors,
        )

    def test_clear_accepted_when_answer_also_changes_on_known_good_case(self):
        # Same case but with a different answer — CLEAR produces codes=[] (same
        # as AI) but the answer change makes the correction material overall.
        row = _correct_label_row(
            _blank_row_for("qbv1-001", self.rows),
            answer="B",
            codes=CLEAR_TOKEN,
            notes="answer correction; findings stay empty",
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid, report.errors)

    # ------------------------------------------------------------------
    # Acceptance criterion 8: existing canonical replacement unchanged
    # ------------------------------------------------------------------

    def test_canonical_code_replacement_unchanged_when_no_clear(self):
        row = _correct_label_row(
            _blank_row_for("qbv1-015", self.rows),
            codes="WEAK_DISTRACTORS",
            notes="only one defensible answer; distractors are weak",
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid, report.errors)
        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-015")
        rows[idx] = row
        reviewed = self._build_complete(rows)
        case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-015")
        self.assertEqual(case["expected_finding_codes"], ["WEAK_DISTRACTORS"])
        self.assertFalse(case["known_good"])
        self.assertEqual(case["expected_materiality"], "warning")

    # ------------------------------------------------------------------
    # Acceptance criterion 10 (regression): qbv1-015 and qbv1-017
    # ------------------------------------------------------------------

    def test_qbv1_015_clear_regression(self):
        """qbv1-015: SME corrects answer to A and clears all finding codes.

        AI label: expected_correct_option_labels=["A","B"],
                  expected_finding_codes=["MULTIPLE_DEFENSIBLE_ANSWERS"]
        Expected effective result: answer=["A"], findings=[], known_good=true
        """
        row = _correct_label_row(
            _blank_row_for("qbv1-015", self.rows),
            answer="A",
            codes=CLEAR_TOKEN,
            notes=(
                "A is the only defensible answer per the evidence. "
                "The MULTIPLE_DEFENSIBLE_ANSWERS finding is incorrect; "
                "no defect remains."
            ),
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid, report.errors)

        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-015")
        rows[idx] = row
        reviewed = self._build_complete(rows)
        case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-015")

        self.assertEqual(case["expected_correct_option_labels"], ["A"])
        self.assertEqual(case["expected_finding_codes"], [])
        self.assertTrue(case["known_good"])
        self.assertIsNone(case["expected_materiality"])
        self.assertEqual(case["reviewer_label"], {"known_good": True, "expected_finding_codes": []})
        # AI-drafted label must be preserved for provenance.
        self.assertEqual(
            case["ai_drafted_reviewer_label"]["expected_finding_codes"],
            ["MULTIPLE_DEFENSIBLE_ANSWERS"],
        )
        self.assertFalse(case["ai_drafted_reviewer_label"]["known_good"])

    def test_qbv1_017_clear_regression(self):
        """qbv1-017: SME corrects answer to A and clears all finding codes.

        AI label: expected_correct_option_labels=["A","B","C"],
                  expected_finding_codes=["AMBIGUOUS_QUESTION"]
        Expected effective result: answer=["A"], findings=[], known_good=true
        """
        row = _correct_label_row(
            _blank_row_for("qbv1-017", self.rows),
            answer="A",
            codes=CLEAR_TOKEN,
            notes=(
                "A is the sole evidence-supported answer. "
                "The AMBIGUOUS_QUESTION finding is incorrect; "
                "no defect remains."
            ),
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertTrue(report.is_valid, report.errors)

        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-017")
        rows[idx] = row
        reviewed = self._build_complete(rows)
        case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-017")

        self.assertEqual(case["expected_correct_option_labels"], ["A"])
        self.assertEqual(case["expected_finding_codes"], [])
        self.assertTrue(case["known_good"])
        self.assertIsNone(case["expected_materiality"])
        self.assertEqual(case["reviewer_label"], {"known_good": True, "expected_finding_codes": []})
        # AI-drafted label must be preserved for provenance.
        self.assertEqual(
            case["ai_drafted_reviewer_label"]["expected_finding_codes"],
            ["AMBIGUOUS_QUESTION"],
        )
        self.assertFalse(case["ai_drafted_reviewer_label"]["known_good"])

    # ------------------------------------------------------------------
    # Extra: lowercase CLEAR must be rejected (no normalisation of codes)
    # ------------------------------------------------------------------

    def test_lowercase_clear_rejected_as_invalid_code(self):
        row = _correct_label_row(
            _blank_row_for("qbv1-015", self.rows),
            answer="A",
            codes="clear",
            notes="testing lowercase is not accepted",
        )
        report = validate_sme_review_rows([row], self.fixture)
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("invalid sme_finding_codes" in err for err in report.errors),
            report.errors,
        )

    # ------------------------------------------------------------------
    # Extra: sme_review payload records finding_codes=[] for CLEAR
    # ------------------------------------------------------------------

    def test_sme_review_payload_records_empty_finding_codes_for_clear(self):
        rows = self._all_approved_rows()
        idx = self.case_ids.index("qbv1-015")
        rows[idx] = _correct_label_row(
            rows[idx],
            answer="A",
            codes=CLEAR_TOKEN,
            notes="clearing findings",
        )
        reviewed = self._build_complete(rows)
        case = next(c for c in reviewed["cases"] if c["case_id"] == "qbv1-015")
        self.assertEqual(case["sme_review"]["finding_codes"], [])
        self.assertEqual(case["sme_review"]["decision"], "correct_label")


if __name__ == "__main__":
    unittest.main()
