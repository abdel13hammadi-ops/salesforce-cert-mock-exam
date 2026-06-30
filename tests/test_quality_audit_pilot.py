"""
Tests for deterministic quality-audit pilot smoke-question selection.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.quality_audit_pilot import (
    DEFAULT_SMOKE_SEED,
    PILOT_CERTIFICATIONS,
    QUESTIONS_PER_CERTIFICATION,
    allocate_weighted_domain_slots,
    format_quality_audit_smoke_selection,
    is_multi_select,
    public_options,
    select_quality_audit_smoke_questions,
    select_smoke_questions_for_certification,
)
from workers.run_quality_audit_smoke_selection import main
from workers.structural_audit_launcher import ADM_EXAM_NAME, BA_EXAM_NAME

_ADM_DOMAINS = [
    {"domain_name": "Configuration and Setup", "weight": 25.0},
    {"domain_name": "Object Manager and Lightning App Builder", "weight": 20.0},
    {"domain_name": "Workflow and Process Automation", "weight": 20.0},
    {"domain_name": "Data and Analytics Management", "weight": 20.0},
    {"domain_name": "Sales and Marketing Applications", "weight": 15.0},
]

_BA_DOMAINS = [
    {"domain_name": "Business Analysis Planning and Monitoring", "weight": 25.0},
    {"domain_name": "Elicitation and Collaboration", "weight": 25.0},
    {"domain_name": "Requirements Life Cycle Management", "weight": 25.0},
    {"domain_name": "Strategy Analysis", "weight": 25.0},
]


def _qvid(prefix: str, index: int) -> str:
    return f"{prefix}{index:012x}"


def _build_rows(exam_name: str, prefix: str, specs: list[tuple[str, int, str]]) -> list[dict]:
    rows = []
    for index, (category, question_id, stem) in enumerate(specs, start=1):
        rows.append({
            "question_version_id": _qvid(prefix, index),
            "question_id": question_id,
            "certification_exam_name": exam_name,
            "question_text": stem,
            "category": category,
            "version_number": 1,
        })
    return rows


def _build_snapshots(prefix: str, specs: list[tuple[str, str, int]]) -> tuple[list[dict], list[dict]]:
    versions = []
    options = []
    for index, (question_type, stem, select_count) in enumerate(specs, start=1):
        qvid = _qvid(prefix, index)
        versions.append({
            "id": qvid,
            "question_text": stem,
            "explanation": "Hidden explanation.",
            "question_type": question_type,
            "select_count": select_count,
        })
        options.extend([
            {
                "question_version_id": qvid,
                "option_label": "A",
                "option_text": f"{stem} option A",
                "is_correct": True,
                "display_order": 1,
            },
            {
                "question_version_id": qvid,
                "option_label": "B",
                "option_text": f"{stem} option B",
                "is_correct": False,
                "display_order": 2,
            },
        ])
    return versions, options


_ADM_SPECS = [
    ("Configuration and Setup", 1, "ADM setup one"),
    ("Configuration and Setup", 2, "ADM setup two"),
    ("Configuration and Setup", 3, "ADM setup three"),
    ("Object Manager and Lightning App Builder", 4, "ADM object one"),
    ("Object Manager and Lightning App Builder", 5, "ADM object two"),
    ("Workflow and Process Automation", 6, "ADM workflow one"),
    ("Workflow and Process Automation", 7, "ADM workflow multi"),
    ("Data and Analytics Management", 8, "ADM data one"),
    ("Data and Analytics Management", 9, "ADM data two"),
    ("Sales and Marketing Applications", 10, "ADM sales one"),
    ("Sales and Marketing Applications", 11, "ADM sales two"),
]

_ADM_SNAPSHOT_SPECS = [
    ("single", "ADM setup one", 1),
    ("single", "ADM setup two", 1),
    ("single", "ADM setup three", 1),
    ("single", "ADM object one", 1),
    ("single", "ADM object two", 1),
    ("single", "ADM workflow one", 1),
    ("multiple", "ADM workflow multi", 2),
    ("single", "ADM data one", 1),
    ("single", "ADM data two", 1),
    ("single", "ADM sales one", 1),
    ("single", "ADM sales two", 1),
]

_BA_SPECS = [
    ("Business Analysis Planning and Monitoring", 101, "BA planning one"),
    ("Business Analysis Planning and Monitoring", 102, "BA planning two"),
    ("Elicitation and Collaboration", 103, "BA elicitation one"),
    ("Elicitation and Collaboration", 104, "BA elicitation multi"),
    ("Requirements Life Cycle Management", 105, "BA requirements one"),
    ("Requirements Life Cycle Management", 106, "BA requirements two"),
    ("Strategy Analysis", 107, "BA strategy one"),
    ("Strategy Analysis", 108, "BA strategy two"),
]

_BA_SNAPSHOT_SPECS = [
    ("single", "BA planning one", 1),
    ("single", "BA planning two", 1),
    ("single", "BA elicitation one", 1),
    ("multiple", "BA elicitation multi", 2),
    ("single", "BA requirements one", 1),
    ("single", "BA requirements two", 1),
    ("single", "BA strategy one", 1),
    ("single", "BA strategy two", 1),
]

_ADM_ROWS = _build_rows(ADM_EXAM_NAME, "aaaaaaaa0000", _ADM_SPECS)
_BA_ROWS = _build_rows(BA_EXAM_NAME, "bbbbbbbb0000", _BA_SPECS)
_ADM_VERSIONS, _ADM_OPTIONS = _build_snapshots("aaaaaaaa0000", _ADM_SNAPSHOT_SPECS)
_BA_VERSIONS, _BA_OPTIONS = _build_snapshots("bbbbbbbb0000", _BA_SNAPSHOT_SPECS)


class FakeQuery:
    def __init__(self, client: "FakeSupabase", table_name: str):
        self.client = client
        self.table_name = table_name
        self._operation = "select"
        self._filters = []

    def select(self, *_args, **_kwargs):
        self._operation = "select"
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self._filters.append(("in", field, tuple(values)))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def insert(self, *_args, **_kwargs):
        self._operation = "insert"
        return self

    def update(self, *_args, **_kwargs):
        self._operation = "update"
        return self

    def execute(self):
        self.client.table_calls.append({
            "table": self.table_name,
            "operation": self._operation,
            "filters": list(self._filters),
        })
        rows = list(self.client.table_rows.get(self.table_name, []))
        for op, field, value in self._filters:
            if op == "eq":
                rows = [row for row in rows if row.get(field) == value]
            elif op == "in":
                rows = [row for row in rows if row.get(field) in value]
        return _FakeResult(rows)


class _FakeResult:
    def __init__(self, data):
        self.data = data
        self.error = None


class _FakeRpcBuilder:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return _FakeResult(self._data)


class FakeSupabase:
    def __init__(self):
        self.rpc_calls = []
        self.table_calls = []
        self.table_rows = {
            "certification_domains": [],
            "question_versions": [],
            "question_option_versions": [],
        }
        self._loader_rows = {}

    def set_certification(self, exam_name: str, *, domains: list[dict], loader_rows: list[dict]):
        for domain in domains:
            self.table_rows["certification_domains"].append({
                "exam_name": exam_name,
                "domain_name": domain["domain_name"],
                "weight": domain["weight"],
                "is_active": True,
            })
        self._loader_rows[exam_name] = loader_rows

    def set_snapshots(self, versions: list[dict], options: list[dict]):
        self.table_rows["question_versions"] = versions
        self.table_rows["question_option_versions"] = options

    def rpc(self, name, params):
        self.rpc_calls.append({"name": name, "params": params})
        if name == "list_certification_current_question_versions_v1":
            exam = params.get("p_certification_exam_name")
            return _FakeRpcBuilder(self._loader_rows.get(exam, []))
        if name == "enqueue_background_job_v1":
            raise AssertionError("enqueue_background_job_v1 must not be called")
        return _FakeRpcBuilder([])

    def table(self, name):
        return FakeQuery(self, name)


def _build_fake_client() -> FakeSupabase:
    fake = FakeSupabase()
    fake.set_certification(ADM_EXAM_NAME, domains=_ADM_DOMAINS, loader_rows=_ADM_ROWS)
    fake.set_certification(BA_EXAM_NAME, domains=_BA_DOMAINS, loader_rows=_BA_ROWS)
    fake.set_snapshots(_ADM_VERSIONS + _BA_VERSIONS, _ADM_OPTIONS + _BA_OPTIONS)
    return fake


class TestQualityAuditPilotSelection(unittest.TestCase):
    def setUp(self):
        self.client = _build_fake_client()

    def test_exactly_five_per_certification(self):
        selection = select_quality_audit_smoke_questions(self.client, seed=42)
        self.assertEqual(len(selection.certifications), 2)
        for cert in selection.certifications:
            self.assertEqual(len(cert.selected), QUESTIONS_PER_CERTIFICATION)

    def test_reproducible_with_same_seed(self):
        first = select_quality_audit_smoke_questions(self.client, seed=42)
        second = select_quality_audit_smoke_questions(self.client, seed=42)
        first_ids = [
            [item.question_version_id for item in cert.selected]
            for cert in first.certifications
        ]
        second_ids = [
            [item.question_version_id for item in cert.selected]
            for cert in second.certifications
        ]
        self.assertEqual(first_ids, second_ids)

    def test_different_seed_changes_selection(self):
        first = select_quality_audit_smoke_questions(self.client, seed=42)
        second = select_quality_audit_smoke_questions(self.client, seed=99)
        first_ids = {
            item.question_version_id
            for cert in first.certifications
            for item in cert.selected
        }
        second_ids = {
            item.question_version_id
            for cert in second.certifications
            for item in cert.selected
        }
        self.assertNotEqual(first_ids, second_ids)

    def test_weighted_domain_allocation(self):
        slots = allocate_weighted_domain_slots(
            {"Heavy": 80.0, "Light": 20.0},
            {"Heavy": 5, "Light": 5},
            5,
        )
        self.assertEqual(slots, {"Heavy": 4, "Light": 1})

        selection = select_smoke_questions_for_certification(
            self.client,
            certification_exam_name=ADM_EXAM_NAME,
            seed=42,
        )
        self.assertEqual(sum(selection.domain_allocation.values()), 5)
        self.assertGreaterEqual(len(selection.domain_allocation), 2)

    def test_allocate_weighted_domain_slots_uses_largest_remainder(self):
        allocation = allocate_weighted_domain_slots(
            {
                "Heavy": 50.0,
                "Light": 10.0,
            },
            {"Heavy": 10, "Light": 10},
            5,
        )
        self.assertEqual(sum(allocation.values()), 5)
        self.assertGreater(allocation["Heavy"], allocation["Light"])

    def test_uses_immutable_question_version_ids(self):
        selection = select_quality_audit_smoke_questions(self.client, seed=42)
        allowed = {row["question_version_id"] for row in _ADM_ROWS + _BA_ROWS}
        selected = {
            item.question_version_id
            for cert in selection.certifications
            for item in cert.selected
        }
        self.assertTrue(selected.issubset(allowed))

    def test_includes_multi_select_when_available(self):
        selection = select_quality_audit_smoke_questions(self.client, seed=42)
        for cert in selection.certifications:
            self.assertTrue(
                any(
                    is_multi_select(item.question_type, item.select_count)
                    for item in cert.selected
                ),
                cert.certification_exam_name,
            )

    def test_output_strips_sensitive_fields(self):
        selection = select_quality_audit_smoke_questions(self.client, seed=42)
        rendered = format_quality_audit_smoke_selection(selection)
        self.assertNotIn("is_correct", rendered)
        self.assertNotIn("Hidden explanation", rendered)
        self.assertNotIn("explanation:", rendered.lower())
        for cert in selection.certifications:
            for item in cert.selected:
                for option in item.options:
                    self.assertNotIn("is_correct", option)
                cleaned = public_options([
                    {
                        "option_label": "A",
                        "option_text": "Text",
                        "is_correct": True,
                        "display_order": 1,
                    }
                ])
                self.assertEqual(cleaned, [{
                    "option_label": "A",
                    "option_text": "Text",
                    "display_order": 1,
                }])

    def test_no_writes_or_enqueue_calls(self):
        select_quality_audit_smoke_questions(self.client, seed=42)
        rpc_names = [call["name"] for call in self.client.rpc_calls]
        self.assertNotIn("enqueue_background_job_v1", rpc_names)
        for call in self.client.table_calls:
            self.assertEqual(call["operation"], "select")

    @patch("workers.run_quality_audit_smoke_selection.create_supabase_admin_client")
    def test_cli_prints_ten_questions(self, client_mock):
        fake = _build_fake_client()
        client_mock.return_value = fake
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = main(["--seed", "42"])
        self.assertEqual(rc, 0)
        output = buffer.getvalue()
        self.assertIn("seed: 42", output)
        self.assertIn(ADM_EXAM_NAME, output)
        self.assertIn(BA_EXAM_NAME, output)
        self.assertEqual(output.count("question_version_id:"), 10)
        self.assertIn("domain_allocation:", output)

    def test_default_seed_is_forty_two(self):
        self.assertEqual(DEFAULT_SMOKE_SEED, 42)

    def test_pilot_certifications_are_adm_and_ba(self):
        self.assertEqual(
            PILOT_CERTIFICATIONS,
            (ADM_EXAM_NAME, BA_EXAM_NAME),
        )


if __name__ == "__main__":
    unittest.main()
