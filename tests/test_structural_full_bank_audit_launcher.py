"""Tests for structural full-bank audit launcher."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.structural_audit_launcher import (
    ADM_EXAM_NAME,
    BA_EXAM_NAME,
    DEFAULT_RULESET_VERSION,
    MalformedSelectionError,
    UnknownCertificationError,
    _rows_to_targets,
    apply_max_questions,
    batch_items,
    build_deterministic_audit_payload,
    build_structural_audit_plan,
    execute_structural_audit_plan,
    extract_active_job_keys,
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

    def select(self, fields):
        self._select_fields = fields
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self._in_filters.append((field, list(values)))
        return self

    def order(self, field):
        self._order = field
        return self

    def execute(self):
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
        return _FakeResult(filtered)


class FakeSupabase:
    def __init__(self):
        self.rpc_calls = []
        self._rpc_responses = {}
        self._active_question_ids = {}
        self._table_rows = {
            "background_jobs": [],
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
            active_jobs=active_jobs,
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
            active_jobs=active_jobs,
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
            active_jobs=active_jobs,
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
            active_jobs=active_jobs,
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
            load_snapshot=lambda _qvid: _SNAPSHOT,
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
                load_snapshot=lambda _qvid: _SNAPSHOT,
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
