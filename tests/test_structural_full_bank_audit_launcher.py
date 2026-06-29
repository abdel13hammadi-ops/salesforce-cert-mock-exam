"""Tests for structural full-bank audit launcher."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.structural_audit_launcher import (
    ADM_EXAM_NAME,
    BA_EXAM_NAME,
    DEFAULT_RULESET_VERSION,
    DEFAULT_SNAPSHOT_PAGE_SIZE,
    MalformedSelectionError,
    StructuralAuditEnqueueError,
    UnknownCertificationError,
    EnqueueState,
    VersionTarget,
    _rows_to_targets,
    apply_max_questions,
    atomic_write_enqueue_state,
    batch_items,
    build_deterministic_audit_payload,
    build_structural_audit_plan,
    enqueue_deterministic_audit_job,
    execute_structural_audit_plan,
    extract_active_job_keys,
    extract_retryable_job_keys,
    load_completed_deterministic_audit_keys,
    load_completed_duplicate_audit_keys,
    load_question_version_snapshots_bulk,
    load_resume_state,
    load_version_targets_for_certifications,
    resolve_certification_scope,
)
from workers.run_structural_full_bank_audit import main, run_launcher

_ADM_QV_1 = "aaaaaaaa-0000-0000-0000-000000000001"
_ADM_QV_2 = "aaaaaaaa-0000-0000-0000-000000000002"
_BA_QV_1 = "bbbbbbbb-0000-0000-0000-000000000001"

_ADM_ROWS = [
    {
        "question_version_id": _ADM_QV_1,
        "question_id": 1,
        "certification_exam_name": ADM_EXAM_NAME,
        "question_text": "ADM question one",
        "category": "Automation",
        "version_number": 2,
    },
    {
        "question_version_id": _ADM_QV_2,
        "question_id": 2,
        "certification_exam_name": ADM_EXAM_NAME,
        "question_text": "ADM question two",
        "category": "Setup",
        "version_number": 1,
    },
]

_BA_ROWS = [
    {
        "question_version_id": _BA_QV_1,
        "question_id": 101,
        "certification_exam_name": BA_EXAM_NAME,
        "question_text": "BA question one",
        "category": "Strategy",
        "version_number": 1,
    },
]

_SNAPSHOT = {
    "question_text": "Stem",
    "explanation": "Because.",
    "question_type": "single",
    "select_count": 1,
    "options": [
        {
            "option_label": "A",
            "option_text": "Answer",
            "is_correct": True,
            "display_order": 1,
        }
    ],
}


class _FakeResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


class _FakeQuery:
    def __init__(self, client, table_name: str):
        self._client = client
        self._table_name = table_name
        self._filters = []
        self._select_fields = "*"
        self._in_filters = []
        self._order = None
        self._json_filters = []

    def select(self, fields):
        self._select_fields = fields
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self._in_filters.append((field, list(values)))
        return self

    def filter(self, field, op, value):
        self._json_filters.append((field, op, value))
        return self

    def order(self, field):
        self._order = field
        return self

    def execute(self):
        self._client.table_execute_counts[self._table_name] = (
            self._client.table_execute_counts.get(self._table_name, 0) + 1
        )
        rows = self._client._table_rows.get(self._table_name, [])
        if self._table_name == "questions" and self._client._active_question_ids:
            scoped_rows = []
            for exam_name, ids in self._client._active_question_ids.items():
                for qid in ids:
                    scoped_rows.append({"id": qid, "exam_name": exam_name, "is_active": True})
            rows = scoped_rows
        filtered = rows
        for op, field, value in self._filters:
            if op == "eq":
                filtered = [row for row in filtered if row.get(field) == value]
        for field, values in self._in_filters:
            filtered = [row for row in filtered if row.get(field) in values]
        for field, op, value in self._json_filters:
            if op != "eq" or "->>" not in field:
                continue
            json_key = field.split("->>", 1)[1].strip().strip("'").strip('"')
            filtered = [
                row
                for row in filtered
                if str((row.get("metadata") or {}).get(json_key)) == str(value)
            ]
        return _FakeResult(filtered)


class FakeSupabase:
    def __init__(self):
        self.rpc_calls = []
        self.table_execute_counts = {}
        self._rpc_responses = {}
        self._active_question_ids = {}
        self._table_rows = {
            "background_jobs": [],
            "audit_runs": [],
            "question_versions": [
                {
                    "id": _ADM_QV_1,
                    "question_text": "Stem",
                    "explanation": "Because.",
                    "question_type": "single",
                    "select_count": 1,
                }
            ],
            "question_option_versions": [
                {
                    "question_version_id": _ADM_QV_1,
                    "option_label": "A",
                    "option_text": "Answer",
                    "is_correct": True,
                    "display_order": 1,
                }
            ],
        }

    def set_loader_rows(self, exam_name: str, rows: list):
        self._rpc_responses[("list_certification_current_question_versions_v1", exam_name)] = rows

    def set_active_question_ids(self, exam_name: str, question_ids: list):
        self._active_question_ids[exam_name] = list(question_ids)

    def set_rpc_response(self, name: str, rows: list):
        self._rpc_responses[name] = rows

    def rpc(self, name, params):
        self.rpc_calls.append({"name": name, "params": params})
        if name == "list_certification_current_question_versions_v1":
            exam = params.get("p_certification_exam_name")
            rows = self._rpc_responses.get((name, exam), [])
            return _FakeRpcBuilder(rows)
        rows = self._rpc_responses.get(name, [{"job_id": "job-1", "job_status": "pending"}])
        return _FakeRpcBuilder(rows)

    def table(self, name):
        return _FakeQuery(self, name)


class _FakeRpcBuilder:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return _FakeResult(self._data)


class TestCertificationScope(unittest.TestCase):
    def test_adm_only(self):
        self.assertEqual(resolve_certification_scope("adm"), [ADM_EXAM_NAME])
        self.assertEqual(resolve_certification_scope("ADM-201"), [ADM_EXAM_NAME])

    def test_ba_only(self):
        self.assertEqual(resolve_certification_scope("ba"), [BA_EXAM_NAME])
        self.assertEqual(resolve_certification_scope("BA-201"), [BA_EXAM_NAME])

    def test_combined_selection(self):
        self.assertEqual(
            resolve_certification_scope("both"),
            [ADM_EXAM_NAME, BA_EXAM_NAME],
        )

    def test_unknown_certification_rejected(self):
        with self.assertRaises(UnknownCertificationError):
            resolve_certification_scope("unknown-cert")


class TestVersionSelection(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()
        self.client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS)
        self.client.set_loader_rows(BA_EXAM_NAME, _BA_ROWS)
        self.client.set_active_question_ids(ADM_EXAM_NAME, [1, 2])
        self.client.set_active_question_ids(BA_EXAM_NAME, [101])

    def test_current_version_only_targets(self):
        targets, missing = load_version_targets_for_certifications(
            self.client,
            [ADM_EXAM_NAME],
        )
        self.assertEqual(missing, [])
        self.assertEqual(len(targets), 2)
        self.assertEqual({t.question_id for t in targets}, {1, 2})
        self.assertEqual(
            {t.question_version_id for t in targets},
            {_ADM_QV_1, _ADM_QV_2},
        )

    def test_malformed_selection_missing_version(self):
        self.client.set_loader_rows(ADM_EXAM_NAME, [])
        self.client.set_active_question_ids(ADM_EXAM_NAME, [9])
        with self.assertRaises(MalformedSelectionError):
            build_structural_audit_plan(
                self.client,
                certification_scope="adm",
            )

    def test_malformed_selection_duplicate_question_ids(self):
        rows = [
            {
                "question_version_id": _ADM_QV_1,
                "question_id": 1,
                "certification_exam_name": ADM_EXAM_NAME,
                "question_text": "A",
                "category": "Automation",
                "version_number": 2,
            },
            {
                "question_version_id": "cccccccc-0000-0000-0000-000000000099",
                "question_id": 1,
                "certification_exam_name": ADM_EXAM_NAME,
                "question_text": "A duplicate",
                "category": "Automation",
                "version_number": 3,
            },
        ]
        with self.assertRaises(MalformedSelectionError):
            _rows_to_targets(rows, ADM_EXAM_NAME)


class TestBatchingAndLimits(unittest.TestCase):
    def test_batch_items_default_25(self):
        items = list(range(60))
        batches = batch_items(items, 25)
        self.assertEqual(len(batches), 3)
        self.assertEqual(len(batches[0]), 25)
        self.assertEqual(len(batches[1]), 25)
        self.assertEqual(len(batches[2]), 10)

    def test_max_questions_limit(self):
        targets = [
            MagicMock(question_version_id=f"v{i}", question_id=i)
            for i in range(10)
        ]
        limited = apply_max_questions(targets, 3)
        self.assertEqual(len(limited), 3)


class TestIdempotency(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()
        self.client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS)
        self.client.set_loader_rows(BA_EXAM_NAME, _BA_ROWS)
        self.client.set_active_question_ids(ADM_EXAM_NAME, [1, 2])
        self.client.set_active_question_ids(BA_EXAM_NAME, [101])

    def test_skip_pending_running_deterministic_jobs(self):
        active_jobs = [
            {
                "job_type": "deterministic_audit",
                "job_status": "running",
                "payload": {
                    "target_question_version_id": _ADM_QV_1,
                    "ruleset_version": DEFAULT_RULESET_VERSION,
                },
            }
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
            background_jobs=active_jobs,
        )
        self.assertEqual(plan.skipped_pending_deterministic, [_ADM_QV_1])
        self.assertEqual(len(plan.deterministic_jobs_to_enqueue), 1)
        self.assertEqual(
            plan.deterministic_jobs_to_enqueue[0].question_version_id,
            _ADM_QV_2,
        )

    def test_completed_jobs_do_not_block_same_ruleset(self):
        active_jobs = [
            {
                "job_type": "deterministic_audit",
                "job_status": "completed",
                "payload": {
                    "target_question_version_id": _ADM_QV_1,
                    "ruleset_version": DEFAULT_RULESET_VERSION,
                },
            }
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
            background_jobs=active_jobs,
        )
        self.assertEqual(plan.skipped_pending_deterministic, [])
        self.assertEqual(len(plan.deterministic_jobs_to_enqueue), 2)

    def test_completed_jobs_do_not_block_new_ruleset(self):
        active_jobs = [
            {
                "job_type": "deterministic_audit",
                "job_status": "running",
                "payload": {
                    "target_question_version_id": _ADM_QV_1,
                    "ruleset_version": "2.0.0",
                },
            }
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
            ruleset_version=DEFAULT_RULESET_VERSION,
            background_jobs=active_jobs,
        )
        self.assertEqual(len(plan.deterministic_jobs_to_enqueue), 2)

    def test_skip_pending_duplicate_certification_scan(self):
        active_jobs = [
            {
                "job_type": "certification_duplicate_audit",
                "job_status": "pending",
                "payload": {
                    "certification_exam_name": ADM_EXAM_NAME,
                    "ruleset_version": DEFAULT_RULESET_VERSION,
                },
            }
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="both",
            background_jobs=active_jobs,
        )
        self.assertEqual(plan.skipped_pending_duplicate, [ADM_EXAM_NAME])
        self.assertEqual(plan.duplicate_certifications_to_enqueue, [BA_EXAM_NAME])


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()
        self.client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS[:1])
        self.client.set_active_question_ids(ADM_EXAM_NAME, [1])

    def test_dry_run_creates_no_jobs(self):
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
        )
        summary = execute_structural_audit_plan(self.client, plan, dry_run=True)
        self.assertTrue(summary.dry_run)
        self.assertEqual(summary.enqueued_deterministic_jobs, 0)
        self.assertEqual(summary.enqueued_duplicate_scans, 0)
        enqueue_calls = [
            call for call in self.client.rpc_calls
            if call["name"] == "enqueue_background_job_v1"
        ]
        self.assertEqual(enqueue_calls, [])

    def test_enqueue_calls_deterministic_and_duplicate_jobs(self):
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
        )
        summary = execute_structural_audit_plan(
            self.client,
            plan,
            dry_run=False,
        )
        self.assertEqual(summary.enqueued_deterministic_jobs, 1)
        self.assertEqual(summary.enqueued_duplicate_scans, 1)
        enqueue_calls = [
            call for call in self.client.rpc_calls
            if call["name"] == "enqueue_background_job_v1"
        ]
        self.assertEqual(len(enqueue_calls), 2)
        det_payload = enqueue_calls[0]["params"]["p_payload"]
        self.assertEqual(det_payload["target_question_version_id"], _ADM_QV_1)
        self.assertEqual(det_payload["question"], _SNAPSHOT)
        dup_payload = enqueue_calls[1]["params"]["p_payload"]
        self.assertEqual(dup_payload["certification_exam_name"], ADM_EXAM_NAME)

    def test_no_llm_provider_is_invoked(self):
        with patch("workers.llm_provider_factory.build_llm_provider_from_env") as factory:
            plan = build_structural_audit_plan(
                self.client,
                certification_scope="adm",
            )
            execute_structural_audit_plan(
                self.client,
                plan,
                dry_run=False,
            )
            factory.assert_not_called()

    def test_run_launcher_dry_run_integration(self):
        self.client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS)
        self.client.set_loader_rows(BA_EXAM_NAME, _BA_ROWS)
        self.client.set_active_question_ids(ADM_EXAM_NAME, [1, 2])
        self.client.set_active_question_ids(BA_EXAM_NAME, [101])
        self.client._table_rows["background_jobs"] = []
        summary = run_launcher(
            self.client,
            certification="both",
            created_by="admin@test.com",
            ruleset_version=DEFAULT_RULESET_VERSION,
            batch_size=25,
            max_questions=None,
            state_file=None,
            resume=False,
            enqueue=False,
            confirm=False,
        )
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["selected_live_questions"], 3)
        self.assertEqual(summary["duplicate_scans_to_enqueue"], 2)
        enqueue_calls = [
            call for call in self.client.rpc_calls
            if call["name"] == "enqueue_background_job_v1"
        ]
        self.assertEqual(enqueue_calls, [])


class TestBulkSnapshotLoading(unittest.TestCase):
    _QV_2 = "aaaaaaaa-0000-0000-0000-000000000002"
    _QV_3 = "aaaaaaaa-0000-0000-0000-000000000003"

    def setUp(self):
        self.client = FakeSupabase()
        self.client._table_rows["question_versions"] = [
            {
                "id": _ADM_QV_1,
                "question_text": "Stem one",
                "explanation": "Because one.",
                "question_type": "single",
                "select_count": 1,
            },
            {
                "id": self._QV_2,
                "question_text": "Stem two",
                "explanation": "Because two.",
                "question_type": "single",
                "select_count": 1,
            },
            {
                "id": self._QV_3,
                "question_text": "Stem three",
                "explanation": "Because three.",
                "question_type": "single",
                "select_count": 1,
            },
        ]
        self.client._table_rows["question_option_versions"] = [
            {
                "question_version_id": _ADM_QV_1,
                "option_label": "A",
                "option_text": "Answer A",
                "is_correct": True,
                "display_order": 1,
            },
            {
                "question_version_id": self._QV_2,
                "option_label": "B",
                "option_text": "Answer B",
                "is_correct": False,
                "display_order": 2,
            },
            {
                "question_version_id": self._QV_2,
                "option_label": "A",
                "option_text": "Answer A2",
                "is_correct": True,
                "display_order": 1,
            },
            {
                "question_version_id": self._QV_3,
                "option_label": "C",
                "option_text": "Answer C",
                "is_correct": True,
                "display_order": 3,
            },
            {
                "question_version_id": self._QV_3,
                "option_label": "A",
                "option_text": "Answer A3",
                "is_correct": False,
                "display_order": 1,
            },
        ]

    def test_bulk_load_uses_paginated_queries_not_n_plus_one(self):
        ids = [_ADM_QV_1, self._QV_2, self._QV_3]
        snapshots = load_question_version_snapshots_bulk(
            self.client,
            ids,
            page_size=DEFAULT_SNAPSHOT_PAGE_SIZE,
        )
        self.assertEqual(set(snapshots.keys()), set(ids))
        self.assertEqual(
            self.client.table_execute_counts.get("question_versions", 0),
            1,
        )
        self.assertEqual(
            self.client.table_execute_counts.get("question_option_versions", 0),
            1,
        )

    def test_option_ordering_is_stable(self):
        snapshots = load_question_version_snapshots_bulk(
            self.client,
            [self._QV_2, self._QV_3],
        )
        qv2_labels = [opt["option_label"] for opt in snapshots[self._QV_2]["options"]]
        qv3_labels = [opt["option_label"] for opt in snapshots[self._QV_3]["options"]]
        self.assertEqual(qv2_labels, ["A", "B"])
        self.assertEqual(qv3_labels, ["A", "C"])

    def test_pagination_splits_large_id_sets(self):
        ids = [f"aaaaaaaa-0000-0000-0000-{index:012d}" for index in range(150)]
        for qvid in ids:
            self.client._table_rows["question_versions"].append(
                {
                    "id": qvid,
                    "question_text": "Stem",
                    "explanation": "Because.",
                    "question_type": "single",
                    "select_count": 1,
                }
            )
            self.client._table_rows["question_option_versions"].append(
                {
                    "question_version_id": qvid,
                    "option_label": "A",
                    "option_text": "Answer",
                    "is_correct": True,
                    "display_order": 1,
                }
            )
        load_question_version_snapshots_bulk(
            self.client,
            ids,
            page_size=100,
        )
        self.assertEqual(
            self.client.table_execute_counts.get("question_versions", 0),
            2,
        )
        self.assertEqual(
            self.client.table_execute_counts.get("question_option_versions", 0),
            2,
        )


class TestEnqueueStatePersistence(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()
        self.client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS)
        self.client.set_active_question_ids(ADM_EXAM_NAME, [1, 2])
        self.client._table_rows["question_versions"].append(
            {
                "id": _ADM_QV_2,
                "question_text": "Stem two",
                "explanation": "Because two.",
                "question_type": "single",
                "select_count": 1,
            }
        )
        self.client._table_rows["question_option_versions"].append(
            {
                "question_version_id": _ADM_QV_2,
                "option_label": "A",
                "option_text": "Answer two",
                "is_correct": True,
                "display_order": 1,
            }
        )

    def _plan_for_two_jobs(self):
        return build_structural_audit_plan(
            self.client,
            certification_scope="adm",
        )

    def test_state_file_exists_before_first_enqueue(self):
        events = []

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")

            def tracking_enqueue(_client, _params):
                self.assertTrue(os.path.exists(state_path))
                events.append("enqueue")
                return {"job_id": "job-1", "job_status": "pending"}

            plan = self._plan_for_two_jobs()
            execute_structural_audit_plan(
                self.client,
                plan,
                dry_run=False,
                state_file=state_path,
                progress_writer=None,
                enqueue_deterministic_fn=tracking_enqueue,
                enqueue_duplicate_fn=lambda *_args, **_kwargs: {
                    "job_id": "dup-1",
                    "job_status": "pending",
                },
            )
            self.assertIn("enqueue", events)

    def test_state_updates_after_every_successful_enqueue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan = self._plan_for_two_jobs()
            execute_structural_audit_plan(
                self.client,
                plan,
                dry_run=False,
                state_file=state_path,
                progress_writer=None,
                enqueue_duplicate_fn=lambda *_args, **_kwargs: {
                    "job_id": "dup-1",
                    "job_status": "pending",
                },
            )
            with open(state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(
                set(state["enqueued_version_ids"]),
                {_ADM_QV_1, _ADM_QV_2},
            )
            self.assertEqual(
                state["enqueued_duplicate_scans"],
                [
                    {
                        "certification_exam_name": ADM_EXAM_NAME,
                        "ruleset_version": DEFAULT_RULESET_VERSION,
                    }
                ],
            )

    def test_atomic_write_uses_temp_file_and_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan = self._plan_for_two_jobs()
            real_mkstemp = tempfile.mkstemp
            temp_paths = []

            def tracking_mkstemp(*args, **kwargs):
                fd, path = real_mkstemp(*args, **kwargs)
                temp_paths.append(path)
                return fd, path

            with patch(
                "workers.structural_audit_launcher.tempfile.mkstemp",
                side_effect=tracking_mkstemp,
            ):
                with patch("workers.structural_audit_launcher.os.replace") as replace:
                    execute_structural_audit_plan(
                        self.client,
                        plan,
                        dry_run=False,
                        state_file=state_path,
                        progress_writer=None,
                        enqueue_duplicate_fn=lambda *_args, **_kwargs: {
                            "job_id": "dup-1",
                            "job_status": "pending",
                        },
                    )
                    self.assertTrue(temp_paths)
                    self.assertTrue(replace.called)
                    for call in replace.call_args_list:
                        self.assertIn(".structural_audit_state.", call.args[0])
                        self.assertEqual(call.args[1], state_path)

    def test_state_file_never_contains_credentials_or_question_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan = self._plan_for_two_jobs()
            execute_structural_audit_plan(
                self.client,
                plan,
                dry_run=False,
                state_file=state_path,
                progress_writer=None,
                enqueue_duplicate_fn=lambda *_args, **_kwargs: {
                    "job_id": "dup-1",
                    "job_status": "pending",
                },
            )
            with open(state_path, encoding="utf-8") as handle:
                raw = handle.read()
            self.assertNotIn("SUPABASE", raw)
            self.assertNotIn("service-role", raw.lower())
            self.assertNotIn("Stem", raw)
            self.assertNotIn("question_text", raw)
            self.assertNotIn("option_text", raw)

    def test_dry_run_creates_no_state_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan = self._plan_for_two_jobs()
            execute_structural_audit_plan(
                self.client,
                plan,
                dry_run=True,
                state_file=state_path,
            )
            self.assertFalse(os.path.exists(state_path))


class TestResumeAndPartialFailure(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()
        self.client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS)
        self.client.set_loader_rows(BA_EXAM_NAME, _BA_ROWS)
        self.client.set_active_question_ids(ADM_EXAM_NAME, [1, 2])
        self.client.set_active_question_ids(BA_EXAM_NAME, [101])
        self.client._table_rows["question_versions"].append(
            {
                "id": _ADM_QV_2,
                "question_text": "Stem two",
                "explanation": "Because two.",
                "question_type": "single",
                "select_count": 1,
            }
        )
        self.client._table_rows["question_option_versions"].append(
            {
                "question_version_id": _ADM_QV_2,
                "option_label": "A",
                "option_text": "Answer two",
                "is_correct": True,
                "display_order": 1,
            }
        )

    def test_missing_state_file_with_resume_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "missing-state.json")
            state = load_resume_state(missing_path, resume=True)
            self.assertEqual(state, {})
            import io
            from contextlib import redirect_stderr

            buffer = io.StringIO()
            with redirect_stderr(buffer):
                summary = run_launcher(
                    self.client,
                    certification="adm",
                    created_by="admin@test.com",
                    ruleset_version=DEFAULT_RULESET_VERSION,
                    batch_size=25,
                    max_questions=None,
                    state_file=missing_path,
                    resume=True,
                    enqueue=False,
                    confirm=False,
                )
            self.assertIn("WARNING", buffer.getvalue())
            self.assertEqual(summary["new_deterministic_jobs"], 2)

    def test_resume_skips_state_recorded_and_active_jobs(self):
        active_jobs = [
            {
                "job_type": "deterministic_audit",
                "job_status": "running",
                "payload": {
                    "target_question_version_id": _ADM_QV_1,
                    "ruleset_version": DEFAULT_RULESET_VERSION,
                },
            }
        ]
        resume_state = {
            "enqueued_version_ids": [_ADM_QV_2],
            "enqueued_duplicate_scans": [
                {
                    "certification_exam_name": ADM_EXAM_NAME,
                    "ruleset_version": DEFAULT_RULESET_VERSION,
                }
            ],
        }
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="both",
            background_jobs=active_jobs,
            resume_state=resume_state,
        )
        self.assertEqual(
            {target.question_version_id for target in plan.deterministic_jobs_to_enqueue},
            {_BA_QV_1},
        )
        self.assertEqual(plan.duplicate_certifications_to_enqueue, [BA_EXAM_NAME])

    def test_partial_failure_preserves_completed_state(self):
        call_count = {"n": 0}

        def flaky_enqueue(_client, _params):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("rpc failed")
            return {"job_id": f"job-{call_count['n']}", "job_status": "pending"}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan = build_structural_audit_plan(
                self.client,
                certification_scope="adm",
            )
            with self.assertRaises(StructuralAuditEnqueueError) as ctx:
                execute_structural_audit_plan(
                    self.client,
                    plan,
                    dry_run=False,
                    state_file=state_path,
                    progress_writer=None,
                    enqueue_deterministic_fn=flaky_enqueue,
                )
            self.assertEqual(ctx.exception.failed_target, _ADM_QV_2)
            with open(state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["enqueued_version_ids"], [_ADM_QV_1])

    def test_resume_after_partial_failure_skips_completed_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            atomic_write_enqueue_state(
                state_path,
                EnqueueState(
                    certification_exam_names=[ADM_EXAM_NAME],
                    ruleset_version=DEFAULT_RULESET_VERSION,
                    created_by="admin@test.com",
                    enqueued_version_ids=[_ADM_QV_1],
                ),
            )
            resume_state = load_resume_state(state_path, resume=True)
            plan = build_structural_audit_plan(
                self.client,
                certification_scope="adm",
                resume_state=resume_state,
            )
            self.assertEqual(len(plan.deterministic_jobs_to_enqueue), 1)
            self.assertEqual(
                plan.deterministic_jobs_to_enqueue[0].question_version_id,
                _ADM_QV_2,
            )


class TestAuditCompletionIdempotency(unittest.TestCase):
    def setUp(self):
        self.client = FakeSupabase()
        self.client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS)
        self.client.set_loader_rows(BA_EXAM_NAME, _BA_ROWS)
        self.client.set_active_question_ids(ADM_EXAM_NAME, [1, 2])
        self.client.set_active_question_ids(BA_EXAM_NAME, [101])

    def _completed_det_row(self, qvid, *, ruleset=DEFAULT_RULESET_VERSION, metadata=None):
        return {
            "audit_type": "deterministic",
            "run_status": "completed",
            "target_question_version_id": qvid,
            "ruleset_version": ruleset,
            "metadata": metadata or {},
        }

    def _completed_dup_row(self, cert, *, ruleset=DEFAULT_RULESET_VERSION):
        return {
            "audit_type": "deterministic",
            "run_status": "completed",
            "ruleset_version": ruleset,
            "metadata": {
                "scan_type": "duplicate_question_stem",
                "certification_exam_name": cert,
            },
        }

    def test_completed_deterministic_audit_same_ruleset_skipped(self):
        self.client._table_rows["audit_runs"] = [
            self._completed_det_row(_ADM_QV_1),
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
        )
        self.assertIn(_ADM_QV_1, plan.skipped_completed_deterministic)
        self.assertEqual(
            plan.deterministic_jobs_to_enqueue[0].question_version_id,
            _ADM_QV_2,
        )

    def test_different_ruleset_completed_audit_does_not_block(self):
        self.client._table_rows["audit_runs"] = [
            self._completed_det_row(_ADM_QV_1, ruleset="2.0.0"),
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
        )
        self.assertEqual(plan.skipped_completed_deterministic, [])
        self.assertEqual(len(plan.deterministic_jobs_to_enqueue), 2)

    def test_stale_version_completed_audit_does_not_block(self):
        stale_qvid = "cccccccc-0000-0000-0000-000000009999"
        self.client._table_rows["audit_runs"] = [
            self._completed_det_row(stale_qvid),
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
        )
        self.assertEqual(plan.skipped_completed_deterministic, [])
        self.assertEqual(len(plan.deterministic_jobs_to_enqueue), 2)

    def test_failed_job_remains_retryable(self):
        failed_jobs = [
            {
                "job_type": "deterministic_audit",
                "job_status": "dead_letter",
                "payload": {
                    "target_question_version_id": _ADM_QV_1,
                    "ruleset_version": DEFAULT_RULESET_VERSION,
                },
            }
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
            background_jobs=failed_jobs,
        )
        self.assertEqual(
            plan.deterministic_jobs_to_enqueue[0].question_version_id,
            _ADM_QV_1,
        )
        self.assertEqual(plan.retryable_failed_deterministic, [_ADM_QV_1])

    def test_completed_background_job_without_completed_audit_does_not_block(self):
        completed_jobs = [
            {
                "job_type": "deterministic_audit",
                "job_status": "completed",
                "payload": {
                    "target_question_version_id": _ADM_QV_1,
                    "ruleset_version": DEFAULT_RULESET_VERSION,
                },
            }
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="adm",
            background_jobs=completed_jobs,
        )
        self.assertEqual(plan.skipped_pending_deterministic, [])
        self.assertEqual(plan.skipped_completed_deterministic, [])
        self.assertEqual(len(plan.deterministic_jobs_to_enqueue), 2)

    def test_completed_duplicate_audit_skipped(self):
        self.client._table_rows["audit_runs"] = [
            self._completed_dup_row(ADM_EXAM_NAME),
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="both",
        )
        self.assertIn(ADM_EXAM_NAME, plan.skipped_completed_duplicate)
        self.assertEqual(plan.duplicate_certifications_to_enqueue, [BA_EXAM_NAME])

    def test_different_ruleset_duplicate_audit_can_run(self):
        self.client._table_rows["audit_runs"] = [
            self._completed_dup_row(ADM_EXAM_NAME, ruleset="2.0.0"),
        ]
        plan = build_structural_audit_plan(
            self.client,
            certification_scope="both",
        )
        self.assertEqual(plan.skipped_completed_duplicate, [])
        self.assertEqual(
            plan.duplicate_certifications_to_enqueue,
            [ADM_EXAM_NAME, BA_EXAM_NAME],
        )

    def test_production_completed_audits_not_re_enqueued(self):
        adm_count = 400
        ba_count = 344
        adm_rows = []
        ba_rows = []
        audit_runs = []

        for index in range(adm_count):
            qvid = f"adm-{index:04d}-0000-0000-0000-000000000000"
            adm_rows.append(
                {
                    "question_version_id": qvid,
                    "question_id": index + 1,
                    "certification_exam_name": ADM_EXAM_NAME,
                    "question_text": f"ADM {index}",
                    "category": "Setup",
                    "version_number": 1,
                }
            )
            if index < 112:
                audit_runs.append(self._completed_det_row(qvid))

        for index in range(ba_count):
            qvid = f"ba-{index:04d}-0000-0000-0000-000000000000"
            ba_rows.append(
                {
                    "question_version_id": qvid,
                    "question_id": 1000 + index + 1,
                    "certification_exam_name": BA_EXAM_NAME,
                    "question_text": f"BA {index}",
                    "category": "Strategy",
                    "version_number": 1,
                }
            )
            if index < 112:
                audit_runs.append(self._completed_det_row(qvid))

        client = FakeSupabase()
        client.set_loader_rows(ADM_EXAM_NAME, adm_rows)
        client.set_loader_rows(BA_EXAM_NAME, ba_rows)
        client.set_active_question_ids(
            ADM_EXAM_NAME,
            [row["question_id"] for row in adm_rows],
        )
        client.set_active_question_ids(
            BA_EXAM_NAME,
            [row["question_id"] for row in ba_rows],
        )
        client._table_rows["audit_runs"] = audit_runs

        plan = build_structural_audit_plan(
            client,
            certification_scope="both",
            background_jobs=[],
        )
        self.assertEqual(len(plan.skipped_completed_deterministic), 224)
        self.assertEqual(len(plan.deterministic_jobs_to_enqueue), 520)
        self.assertEqual(plan.duplicate_certifications_to_enqueue, [ADM_EXAM_NAME, BA_EXAM_NAME])

    def test_dry_run_performs_no_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan = build_structural_audit_plan(
                self.client,
                certification_scope="adm",
            )
            summary = execute_structural_audit_plan(
                self.client,
                plan,
                dry_run=True,
                state_file=state_path,
            )
            self.assertTrue(summary.dry_run)
            self.assertFalse(os.path.exists(state_path))
            enqueue_calls = [
                call for call in self.client.rpc_calls
                if call["name"] == "enqueue_background_job_v1"
            ]
            self.assertEqual(enqueue_calls, [])


class TestProductionScaleDryRun(unittest.TestCase):
    def test_744_versions_with_224_active_plans_remaining_work(self):
        adm_count = 400
        ba_count = 344
        adm_rows = []
        ba_rows = []
        active_jobs = []

        for index in range(adm_count):
            qvid = f"adm-{index:04d}-0000-0000-0000-000000000000"
            adm_rows.append(
                {
                    "question_version_id": qvid,
                    "question_id": index + 1,
                    "certification_exam_name": ADM_EXAM_NAME,
                    "question_text": f"ADM {index}",
                    "category": "Setup",
                    "version_number": 1,
                }
            )
            if index < 112:
                active_jobs.append(
                    {
                        "job_type": "deterministic_audit",
                        "job_status": "pending",
                        "payload": {
                            "target_question_version_id": qvid,
                            "ruleset_version": DEFAULT_RULESET_VERSION,
                        },
                    }
                )

        for index in range(ba_count):
            qvid = f"ba-{index:04d}-0000-0000-0000-000000000000"
            ba_rows.append(
                {
                    "question_version_id": qvid,
                    "question_id": 1000 + index + 1,
                    "certification_exam_name": BA_EXAM_NAME,
                    "question_text": f"BA {index}",
                    "category": "Strategy",
                    "version_number": 1,
                }
            )
            if index < 112:
                active_jobs.append(
                    {
                        "job_type": "deterministic_audit",
                        "job_status": "running",
                        "payload": {
                            "target_question_version_id": qvid,
                            "ruleset_version": DEFAULT_RULESET_VERSION,
                        },
                    }
                )

        client = FakeSupabase()
        client.set_loader_rows(ADM_EXAM_NAME, adm_rows)
        client.set_loader_rows(BA_EXAM_NAME, ba_rows)
        client.set_active_question_ids(
            ADM_EXAM_NAME,
            [row["question_id"] for row in adm_rows],
        )
        client.set_active_question_ids(
            BA_EXAM_NAME,
            [row["question_id"] for row in ba_rows],
        )

        plan = build_structural_audit_plan(
            client,
            certification_scope="both",
            background_jobs=active_jobs,
        )
        self.assertEqual(plan.selected_live_questions, 744)
        self.assertEqual(len(plan.skipped_pending_deterministic), 224)
        self.assertEqual(len(plan.deterministic_jobs_to_enqueue), 520)
        self.assertEqual(plan.duplicate_certifications_to_enqueue, [ADM_EXAM_NAME, BA_EXAM_NAME])


class TestPayloadShape(unittest.TestCase):
    def test_deterministic_payload_contains_version_target(self):
        target = MagicMock(
            question_version_id=_ADM_QV_1,
            certification_exam_name=ADM_EXAM_NAME,
            question_id=1,
        )
        payload = build_deterministic_audit_payload(
            target,
            question_snapshot=_SNAPSHOT,
            created_by="admin@test.com",
            ruleset_version=DEFAULT_RULESET_VERSION,
        )
        self.assertEqual(payload["target_question_version_id"], _ADM_QV_1)
        self.assertEqual(payload["ruleset_version"], DEFAULT_RULESET_VERSION)
        self.assertNotIn("model_name", payload)
        self.assertNotIn("system_prompt", payload)


class TestActiveJobKeyExtraction(unittest.TestCase):
    def test_extract_active_job_keys(self):
        jobs = [
            {
                "job_type": "deterministic_audit",
                "job_status": "pending",
                "payload": {
                    "target_question_version_id": _ADM_QV_1,
                    "ruleset_version": "1.0.0",
                },
            },
            {
                "job_type": "certification_duplicate_audit",
                "job_status": "leased",
                "payload": {
                    "certification_exam_name": ADM_EXAM_NAME,
                    "ruleset_version": "1.0.0",
                },
            },
        ]
        det, dup = extract_active_job_keys(jobs, ruleset_version="1.0.0")
        self.assertIn((_ADM_QV_1, "1.0.0"), det)
        self.assertIn((ADM_EXAM_NAME, "1.0.0"), dup)


class TestSupabaseConfiguration(unittest.TestCase):
    def test_main_uses_shared_admin_client(self):
        fake_client = FakeSupabase()
        fake_client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS[:1])
        fake_client.set_active_question_ids(ADM_EXAM_NAME, [1])
        fake_client._table_rows["background_jobs"] = []

        with patch(
            "workers.run_structural_full_bank_audit.create_supabase_admin_client",
            return_value=fake_client,
        ) as factory:
            exit_code = main(["--certification", "adm"])
            factory.assert_called_once()
        self.assertEqual(exit_code, 0)

    def test_environment_configuration(self):
        with patch("utils.access_control._secret") as secret:
            secret.side_effect = lambda name, default="": {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "service-role-key-value",
            }.get(name, default)
            with patch("utils.access_control.create_client") as create_client:
                from utils.access_control import create_supabase_admin_client

                create_supabase_admin_client()
                create_client.assert_called_once_with(
                    "https://example.supabase.co",
                    "service-role-key-value",
                )

    def test_streamlit_secrets_configuration(self):
        def fake_secret(name, default=""):
            secrets = {
                "SUPABASE_URL": "https://secrets.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "secrets-service-role-key",
            }
            return secrets.get(name, default)

        with patch.dict(os.environ, {}, clear=True):
            with patch("utils.access_control._secret", side_effect=fake_secret):
                with patch("utils.access_control.create_client") as create_client:
                    from utils.access_control import create_supabase_admin_client

                    create_supabase_admin_client()
                    create_client.assert_called_once_with(
                        "https://secrets.supabase.co",
                        "secrets-service-role-key",
                    )

    def test_missing_configuration_fails_clearly(self):
        with patch("utils.access_control._secret", return_value=""):
            from utils.access_control import (
                SupabaseAdminConfigError,
                create_supabase_admin_client,
            )

            with self.assertRaises(SupabaseAdminConfigError) as ctx:
                create_supabase_admin_client()
            message = str(ctx.exception)
            self.assertIn("SUPABASE_URL", message)
            self.assertIn(".streamlit/secrets.toml", message)
            self.assertNotIn("service-role-key-value", message)
            self.assertNotIn("secrets-service-role-key", message)

    def test_main_missing_configuration_exit_code(self):
        with patch(
            "workers.run_structural_full_bank_audit.create_supabase_admin_client",
            side_effect=__import__(
                "utils.access_control",
                fromlist=["SupabaseAdminConfigError"],
            ).SupabaseAdminConfigError(
                "Missing Supabase admin configuration."
            ),
        ):
            exit_code = main(["--certification", "adm"])
        self.assertEqual(exit_code, 1)

    def test_json_summary_never_includes_credentials(self):
        fake_client = FakeSupabase()
        fake_client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS[:1])
        fake_client.set_active_question_ids(ADM_EXAM_NAME, [1])
        fake_client._table_rows["background_jobs"] = []

        with patch(
            "workers.run_structural_full_bank_audit.create_supabase_admin_client",
            return_value=fake_client,
        ):
            import io
            from contextlib import redirect_stdout

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = main(["--certification", "adm", "--json-summary"])
        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertNotIn("service-role", output.lower())
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", output)

    def test_main_dry_run_does_not_enqueue(self):
        fake_client = FakeSupabase()
        fake_client.set_loader_rows(ADM_EXAM_NAME, _ADM_ROWS[:1])
        fake_client.set_active_question_ids(ADM_EXAM_NAME, [1])
        fake_client._table_rows["background_jobs"] = []

        with patch(
            "workers.run_structural_full_bank_audit.create_supabase_admin_client",
            return_value=fake_client,
        ):
            exit_code = main(["--certification", "adm"])
        self.assertEqual(exit_code, 0)
        enqueue_calls = [
            call for call in fake_client.rpc_calls
            if call["name"] == "enqueue_background_job_v1"
        ]
        self.assertEqual(enqueue_calls, [])


if __name__ == "__main__":
    unittest.main()
