"""
Tests for certification-wide semantic concept-cluster audit background job handler.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.background_worker import BackgroundWorker
from workers.finding_policy import assign_materiality
from workers.job_handlers import (
    HANDLER_REGISTRY,
    HandlerPayloadError,
    build_handler_registry,
    make_certification_semantic_cluster_audit_handler,
)
from workers.run_certification_semantic_cluster_audit import (
    _ENQUEUE_RPC,
    _JOB_TYPE,
    build_enqueue_params,
    build_payload,
    main,
    run_dry_run,
    validate_payload,
)
from workers.semantic_cluster_detector import (
    DEFAULT_MODEL_NAME,
    FINDING_CODE_SEMANTIC_OVERSIZE,
    MAX_ALLOWED_CLUSTER_SIZE,
    SCAN_TYPE_SEMANTIC_CLUSTER,
    SemanticCluster,
    SemanticClusterDetectionResult,
    SemanticClusterThresholds,
    SignalSimilarityStats,
    build_oversize_cluster_findings,
    build_semantic_cluster_oversize_finding,
    build_semantic_cluster_thresholds,
    cluster_dedupe_key,
    filter_unpersisted_semantic_cluster_findings,
    merge_certification_entries_with_snapshots,
    orchestrate_certification_semantic_cluster_audit,
    resolve_sentence_transformers_package_version,
    SIGNAL_FULL,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260629130000_v47_certification_semantic_cluster_audit_job.sql"
)

_CERT = "Platform Admin"
_MODEL = "test-model"
_RULESET = "1.0.0"
_QV_A = "aaaaaaaa-0000-0000-0000-000000000001"
_QV_B = "bbbbbbbb-0000-0000-0000-000000000002"
_QV_C = "cccccccc-0000-0000-0000-000000000003"
_QV_D = "dddddddd-0000-0000-0000-000000000004"

_VALID_PAYLOAD = {
    "certification_exam_name": _CERT,
    "created_by": "audit-worker@certbound.io",
    "ruleset_version": _RULESET,
    "model_name": _MODEL,
}

_LOADER_ROWS = [
    {
        "question_version_id": _QV_A,
        "question_id": 1,
        "certification_exam_name": _CERT,
        "question_text": "What is Flow?",
        "category": "Automation",
        "version_number": 2,
    },
    {
        "question_version_id": _QV_B,
        "question_id": 2,
        "certification_exam_name": _CERT,
        "question_text": "How does Flow work?",
        "category": "Platform",
        "version_number": 1,
    },
]

_SNAPSHOT = {
    "question_text": "Stem text",
    "explanation": "Explanation",
    "question_type": "single",
    "select_count": 1,
    "options": [
        {
            "option_label": "A",
            "option_text": "Correct",
            "is_correct": True,
            "display_order": 1,
        },
        {
            "option_label": "B",
            "option_text": "Wrong",
            "is_correct": False,
            "display_order": 2,
        },
    ],
}

_CREATE_RESPONSE = [{"audit_run_id": "audit-run-semantic-001"}]
_COMPLETE_RESPONSE = [{
    "run_status": "completed",
    "finding_count": 1,
    "evidence_count": 0,
}]


def _stats(value: float = 0.9) -> SignalSimilarityStats:
    return SignalSimilarityStats(min=value, max=value, average=value)


def _cluster(size: int, qvids: list[str], qids: list[int]) -> SemanticCluster:
    return SemanticCluster(
        cluster_id=f"cluster-{size}",
        certification_exam_name=_CERT,
        question_version_ids=tuple(qvids),
        question_ids=tuple(qids),
        cluster_size=size,
        stem_similarity=_stats(),
        full_similarity=_stats(),
        correct_similarity=_stats(),
        categories=("Automation",),
        concept_keys=(),
        is_review_candidate=size > MAX_ALLOWED_CLUSTER_SIZE,
    )


class FakeTableQuery:
    def __init__(self, parent: "FakeSupabase", table_name: str):
        self.parent = parent
        self.table_name = table_name
        self.filters = []
        self._operation = "select"

    def select(self, *_args, **_kwargs):
        self._operation = "select"
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self.filters.append(("in", field, tuple(values)))
        return self

    def filter(self, field, _op, value):
        self.filters.append(("filter", field, value))
        return self

    def update(self, _values):
        self._operation = "update"
        return self

    def execute(self):
        self.parent.table_calls.append({
            "table": self.table_name,
            "operation": self._operation,
            "filters": list(self.filters),
        })
        rows = self.parent.table_responses.get(self.table_name, [])
        return _FakeResult(rows)


class FakeSupabase:
    def __init__(self):
        self.calls = []
        self.table_calls = []
        self.responses: dict[str, list] = {}
        self.table_responses: dict[str, list] = {}
        self.errors: dict[str, str] = {}

    def set_response(self, name: str, rows: list):
        self.responses[name] = rows

    def set_table_response(self, name: str, rows: list):
        self.table_responses[name] = rows

    def set_error(self, name: str, message: str):
        self.errors[name] = message

    def rpc(self, name, params):
        self.calls.append({"name": name, "params": params})
        return self

    def table(self, name):
        return FakeTableQuery(self, name)

    def execute(self):
        call = self.calls[-1]
        name = call["name"]
        if name in self.errors:
            return _FakeResult([], error=self.errors[name])
        return _FakeResult(self.responses.get(name, []))


class _FakeResult:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


def _make_handler_client(rows=None, *, use_default_rows: bool = True):
    fake = FakeSupabase()
    if rows is None and use_default_rows:
        rows = _LOADER_ROWS
    elif rows is None:
        rows = []
    fake.set_response("list_certification_current_question_versions_v1", rows)
    fake.set_response("list_semantic_concept_cluster_keys_v1", [])
    fake.set_response("create_audit_run_v1", _CREATE_RESPONSE)
    fake.set_response("complete_audit_run_v1", _COMPLETE_RESPONSE)
    handler = make_certification_semantic_cluster_audit_handler(fake)
    return fake, handler


class TestMigrationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_registers_job_type_and_cluster_dedupe_index(self):
        self.assertIn("'certification_semantic_cluster_audit'", self.sql)
        self.assertIn("idx_af_semantic_concept_cluster_dedupe", self.sql)
        self.assertIn("list_semantic_concept_cluster_keys_v1", self.sql)
        self.assertIn("'SEMANTIC_CONCEPT_CLUSTER_OVERSIZE'", self.sql)
        self.assertIn("metadata->>'cluster_id'", self.sql)
        self.assertIn("metadata->>'model_name'", self.sql)

    def test_enqueue_and_claim_allowlists_include_semantic_job_type(self):
        self.assertIn(
            "'certification_semantic_cluster_audit'",
            self.sql.split("enqueue_background_job_v1")[1],
        )
        self.assertIn(
            "'certification_semantic_cluster_audit'",
            self.sql.split("claim_background_job_v1")[1],
        )


class TestEnqueuePayload(unittest.TestCase):
    def test_validate_payload_requires_certification_exam_name(self):
        with self.assertRaises(ValueError):
            validate_payload({})
        cert = validate_payload({"certification_exam_name": _CERT})
        self.assertEqual(cert, _CERT)

    def test_build_enqueue_params_use_semantic_cluster_job_type(self):
        payload = build_payload(
            certification_exam_name=_CERT,
            created_by="ops@certbound.io",
            model_name=_MODEL,
        )
        params = build_enqueue_params(payload, created_by="ops@certbound.io")
        self.assertEqual(params["p_job_type"], _JOB_TYPE)
        self.assertEqual(params["p_payload"]["certification_exam_name"], _CERT)
        self.assertEqual(params["p_model_name"], _MODEL)


class TestHandlerRegistration(unittest.TestCase):
    def test_stub_registry_includes_semantic_cluster_job_type(self):
        self.assertIn("certification_semantic_cluster_audit", HANDLER_REGISTRY)

    def test_build_handler_registry_injects_semantic_handler(self):
        fake = FakeSupabase()
        registry = build_handler_registry(fake)
        self.assertIn("certification_semantic_cluster_audit", registry)
        self.assertTrue(callable(registry["certification_semantic_cluster_audit"]))


class TestSnapshotMerge(unittest.TestCase):
    def test_merge_attaches_immutable_snapshots(self):
        rows = [_LOADER_ROWS[0]]
        merged = merge_certification_entries_with_snapshots(
            rows,
            {_QV_A: _SNAPSHOT},
        )
        self.assertEqual(merged[0]["snapshot"], _SNAPSHOT)


class TestFindingPolicy(unittest.TestCase):
    def test_oversize_cluster_finding_is_warning_not_blocking(self):
        cluster = _cluster(4, [_QV_A, _QV_B, _QV_C, _QV_D], [1, 2, 3, 4])
        finding = build_semantic_cluster_oversize_finding(
            cluster,
            ruleset_version=_RULESET,
            model_name=_MODEL,
            model_revision="abc123",
            sentence_transformers_version="3.4.1",
            thresholds=build_semantic_cluster_thresholds(),
            question_count=100,
        )
        self.assertEqual(finding["finding_code"], FINDING_CODE_SEMANTIC_OVERSIZE)
        self.assertEqual(finding["finding_type"], "duplication")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["materiality"], "warning")
        self.assertEqual(assign_materiality(finding), "warning")

    def test_metadata_includes_reproducibility_fields(self):
        cluster = _cluster(4, [_QV_A, _QV_B, _QV_C, _QV_D], [1, 2, 3, 4])
        thresholds = build_semantic_cluster_thresholds()
        finding = build_semantic_cluster_oversize_finding(
            cluster,
            ruleset_version=_RULESET,
            model_name=_MODEL,
            model_revision="rev-1",
            sentence_transformers_version="3.4.1",
            thresholds=thresholds,
            question_count=42,
        )
        metadata = finding["metadata"]
        self.assertEqual(metadata["scan_type"], SCAN_TYPE_SEMANTIC_CLUSTER)
        self.assertEqual(metadata["certification_exam_name"], _CERT)
        self.assertEqual(metadata["model_name"], _MODEL)
        self.assertEqual(metadata["model_revision"], "rev-1")
        self.assertEqual(metadata["sentence_transformers_version"], "3.4.1")
        self.assertEqual(metadata["ruleset_version"], _RULESET)
        self.assertEqual(metadata["stem_threshold"], thresholds.stem_edge_threshold)
        self.assertEqual(
            metadata["full_question_threshold"],
            thresholds.full_edge_threshold,
        )
        self.assertEqual(metadata["cohesion_signal"], thresholds.cohesion_signal)
        self.assertEqual(metadata["cohesion_threshold"], thresholds.cohesion_min_similarity)
        self.assertEqual(metadata["maximum_allowed_cluster_size"], MAX_ALLOWED_CLUSTER_SIZE)
        self.assertEqual(metadata["question_count"], 42)
        self.assertEqual(metadata["cluster_id"], cluster.cluster_id)
        self.assertEqual(metadata["question_version_ids"], list(cluster.question_version_ids))
        self.assertEqual(metadata["question_ids"], list(cluster.question_ids))
        self.assertIn("stem", metadata["pairwise_similarity_stats"])
        self.assertIn("full", metadata["pairwise_similarity_stats"])
        self.assertIn("correct", metadata["pairwise_similarity_stats"])


class TestOversizeFindingSelection(unittest.TestCase):
    def test_allowed_clusters_do_not_produce_findings(self):
        detection = SemanticClusterDetectionResult(
            certification_exam_name=_CERT,
            clusters=[
                _cluster(2, [_QV_A, _QV_B], [1, 2]),
                _cluster(3, [_QV_A, _QV_B, _QV_C], [1, 2, 3]),
            ],
            allowed_clusters=[
                _cluster(2, [_QV_A, _QV_B], [1, 2]),
                _cluster(3, [_QV_A, _QV_B, _QV_C], [1, 2, 3]),
            ],
            review_candidates=[],
            lexical_findings=[{"finding_code": "DUPLICATE_QUESTION_STEM_EXACT"}],
        )
        findings = build_oversize_cluster_findings(
            detection,
            ruleset_version=_RULESET,
            model_name=_MODEL,
            model_revision=None,
            sentence_transformers_version="3.4.1",
            thresholds=build_semantic_cluster_thresholds(),
            question_count=3,
        )
        self.assertEqual(findings, [])

    def test_review_candidates_of_size_four_or_more_produce_findings(self):
        cluster = _cluster(4, [_QV_A, _QV_B, _QV_C, _QV_D], [1, 2, 3, 4])
        detection = SemanticClusterDetectionResult(
            certification_exam_name=_CERT,
            clusters=[cluster],
            allowed_clusters=[],
            review_candidates=[cluster],
            lexical_findings=[],
        )
        findings = build_oversize_cluster_findings(
            detection,
            ruleset_version=_RULESET,
            model_name=_MODEL,
            model_revision=None,
            sentence_transformers_version="3.4.1",
            thresholds=build_semantic_cluster_thresholds(),
            question_count=4,
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["finding_code"], FINDING_CODE_SEMANTIC_OVERSIZE)


class TestIdempotencyHelpers(unittest.TestCase):
    def test_filter_unpersisted_semantic_cluster_findings(self):
        cluster = _cluster(4, [_QV_A, _QV_B, _QV_C, _QV_D], [1, 2, 3, 4])
        finding = build_semantic_cluster_oversize_finding(
            cluster,
            ruleset_version=_RULESET,
            model_name=_MODEL,
            model_revision=None,
            sentence_transformers_version="3.4.1",
            thresholds=build_semantic_cluster_thresholds(),
            question_count=4,
        )
        key = cluster_dedupe_key(_CERT, cluster.cluster_id, _MODEL, _RULESET)
        filtered = filter_unpersisted_semantic_cluster_findings([finding], [key])
        self.assertEqual(filtered, [])


class TestSemanticClusterAuditHandler(unittest.TestCase):
    def test_missing_certification_payload_rejected(self):
        _, handler = _make_handler_client()
        with self.assertRaises(HandlerPayloadError):
            handler(
                job_id="job-1",
                payload={"created_by": "ops@certbound.io"},
                checkpoint={},
                attempt=1,
                heartbeat_fn=lambda: None,
            )

    def test_empty_bank_rejected(self):
        fake, handler = _make_handler_client(None, use_default_rows=False)
        with self.assertRaises(HandlerPayloadError):
            handler(
                job_id="job-1",
                payload=_VALID_PAYLOAD,
                checkpoint={},
                attempt=1,
                heartbeat_fn=lambda: None,
            )

    @patch(
        "workers.structural_audit_launcher.load_question_version_snapshots_bulk",
        return_value={_QV_A: _SNAPSHOT, _QV_B: _SNAPSHOT},
    )
    @patch(
        "workers.semantic_cluster_detector.orchestrate_certification_semantic_cluster_audit",
        return_value={
            "audit_run_id": "audit-1",
            "run_status": "completed",
            "finding_count": 0,
            "evidence_count": 0,
        },
    )
    def test_handler_scopes_to_one_certification_and_loads_snapshots(
        self,
        orchestrate_mock,
        snapshots_mock,
    ):
        fake, handler = _make_handler_client()
        result = handler(
            job_id="job-1",
            payload=_VALID_PAYLOAD,
            checkpoint={},
            attempt=1,
            heartbeat_fn=lambda: None,
        )
        self.assertEqual(
            fake.calls[0]["params"]["p_certification_exam_name"],
            _CERT,
        )
        snapshots_mock.assert_called_once()
        passed_entries = orchestrate_mock.call_args.kwargs["entries"]
        self.assertEqual(len(passed_entries), 2)
        self.assertTrue(all(row["certification_exam_name"] == _CERT for row in passed_entries))
        self.assertIn("snapshot", passed_entries[0])
        self.assertEqual(result["question_count"], 2)
        self.assertEqual(result["certification_exam_name"], _CERT)

    @patch(
        "workers.structural_audit_launcher.load_question_version_snapshots_bulk",
        return_value={_QV_A: _SNAPSHOT, _QV_B: _SNAPSHOT},
    )
    @patch(
        "workers.semantic_cluster_detector.orchestrate_certification_semantic_cluster_audit",
        return_value={
            "audit_run_id": "audit-1",
            "run_status": "completed",
            "finding_count": 0,
            "evidence_count": 0,
        },
    )
    def test_handler_does_not_update_live_questions(self, _orchestrate_mock, _snapshots_mock):
        fake, handler = _make_handler_client()
        handler(
            job_id="job-1",
            payload=_VALID_PAYLOAD,
            checkpoint={},
            attempt=1,
            heartbeat_fn=lambda: None,
        )
        for call in fake.table_calls:
            self.assertNotEqual(call["table"], "questions")
            self.assertNotEqual(call["operation"], "update")


class TestOrchestration(unittest.TestCase):
    @patch("workers.audit_orchestration.orchestrate_audit")
    @patch("workers.semantic_cluster_detector.fetch_persisted_semantic_cluster_keys")
    @patch("workers.semantic_cluster_detector.detect_semantic_clusters_for_certification")
    def test_orchestration_persists_only_oversize_clusters_and_skips_existing(
        self,
        detect_mock,
        fetch_keys_mock,
        orchestrate_mock,
    ):
        cluster = _cluster(4, [_QV_A, _QV_B, _QV_C, _QV_D], [1, 2, 3, 4])
        detect_mock.return_value = SemanticClusterDetectionResult(
            certification_exam_name=_CERT,
            clusters=[cluster],
            allowed_clusters=[],
            review_candidates=[cluster],
            lexical_findings=[{"finding_code": "DUPLICATE_QUESTION_STEM_EXACT"}],
        )
        fetch_keys_mock.return_value = set()
        orchestrate_mock.return_value = {
            "audit_run_id": "audit-1",
            "run_status": "completed",
            "finding_count": 1,
            "evidence_count": 0,
        }

        def embed_fn(texts):
            return [[1.0, 0.0] for _ in texts]

        fake = FakeSupabase()
        entries = [
            {**_LOADER_ROWS[0], "snapshot": _SNAPSHOT},
            {**_LOADER_ROWS[1], "snapshot": _SNAPSHOT},
        ]
        result = orchestrate_certification_semantic_cluster_audit(
            fake,
            entries=entries,
            created_by="worker@certbound.io",
            ruleset_version=_RULESET,
            model_name=_MODEL,
            embed_fn=embed_fn,
        )
        self.assertEqual(result["finding_count"], 1)
        passed_findings = orchestrate_mock.call_args.kwargs["check_fn"]()
        self.assertEqual(len(passed_findings), 1)
        self.assertEqual(passed_findings[0]["finding_code"], FINDING_CODE_SEMANTIC_OVERSIZE)
        self.assertNotIn(
            "DUPLICATE_QUESTION_STEM_EXACT",
            {item["finding_code"] for item in passed_findings},
        )

    @patch("workers.audit_orchestration.orchestrate_audit")
    @patch("workers.semantic_cluster_detector.fetch_persisted_semantic_cluster_keys")
    @patch("workers.semantic_cluster_detector.detect_semantic_clusters_for_certification")
    def test_orchestration_idempotent_rerun_filters_existing_cluster(
        self,
        detect_mock,
        fetch_keys_mock,
        orchestrate_mock,
    ):
        cluster = _cluster(4, [_QV_A, _QV_B, _QV_C, _QV_D], [1, 2, 3, 4])
        detect_mock.return_value = SemanticClusterDetectionResult(
            certification_exam_name=_CERT,
            clusters=[cluster],
            allowed_clusters=[],
            review_candidates=[cluster],
            lexical_findings=[],
        )
        fetch_keys_mock.return_value = {
            cluster_dedupe_key(_CERT, cluster.cluster_id, _MODEL, _RULESET)
        }
        orchestrate_mock.return_value = {
            "audit_run_id": "audit-2",
            "run_status": "completed",
            "finding_count": 0,
            "evidence_count": 0,
        }

        entries = [{**_LOADER_ROWS[0], "snapshot": _SNAPSHOT}]
        orchestrate_certification_semantic_cluster_audit(
            FakeSupabase(),
            entries=entries,
            created_by="worker@certbound.io",
            embed_fn=lambda texts: [[1.0, 0.0] for _ in texts],
            model_name=_MODEL,
        )
        passed_findings = orchestrate_mock.call_args.kwargs["check_fn"]()
        self.assertEqual(passed_findings, [])


class TestDryRunCli(unittest.TestCase):
    @patch("workers.run_certification_semantic_cluster_audit.create_supabase_admin_client")
    @patch(
        "workers.run_certification_semantic_cluster_audit.load_certification_current_question_versions",
        return_value=_LOADER_ROWS,
    )
    def test_dry_run_does_not_enqueue_or_load_model(self, _loader_mock, client_mock):
        fake = FakeSupabase()
        fake.set_table_response("audit_runs", [])
        fake.set_table_response("background_jobs", [])
        client_mock.return_value = fake

        with patch(
            "workers.semantic_cluster_detector.create_sentence_transformer_embed_fn",
        ) as embed_factory:
            rc = main([
                "--certification-exam-name",
                _CERT,
            ])
        self.assertEqual(rc, 0)
        embed_factory.assert_not_called()
        rpc_names = [call["name"] for call in fake.calls]
        self.assertNotIn("enqueue_background_job_v1", rpc_names)

    @patch("workers.run_certification_semantic_cluster_audit.create_supabase_admin_client")
    @patch(
        "workers.run_certification_semantic_cluster_audit.load_certification_current_question_versions",
        return_value=_LOADER_ROWS,
    )
    def test_dry_run_does_not_require_direct_env_vars(self, _loader_mock, client_mock):
        fake = FakeSupabase()
        fake.set_table_response("audit_runs", [])
        fake.set_table_response("background_jobs", [])
        client_mock.return_value = fake

        with patch.dict(os.environ, {}, clear=True):
            rc = main(["--certification-exam-name", _CERT])

        self.assertEqual(rc, 0)
        client_mock.assert_called_once()

    @patch(
        "workers.run_certification_semantic_cluster_audit.create_supabase_admin_client",
        side_effect=__import__(
            "utils.access_control",
            fromlist=["SupabaseAdminConfigError"],
        ).SupabaseAdminConfigError(
            "Missing Supabase admin configuration."
        ),
    )
    def test_enqueue_fails_clearly_when_supabase_unavailable(self, _client_mock):
        import io
        from contextlib import redirect_stderr

        buffer = io.StringIO()
        with patch.dict(os.environ, {"CERTBOUND_ALLOW_JOB_ENQUEUE": "1"}, clear=True):
            with redirect_stderr(buffer):
                rc = main([
                    "--certification-exam-name",
                    _CERT,
                    "--enqueue",
                ])

        self.assertEqual(rc, 1)
        self.assertIn("Missing Supabase admin configuration", buffer.getvalue())

    def test_run_dry_run_report_skips_model_download(self):
        fake = FakeSupabase()
        fake.set_response("list_certification_current_question_versions_v1", _LOADER_ROWS)
        fake.set_table_response("audit_runs", [])
        fake.set_table_response("background_jobs", [])
        report = run_dry_run(
            fake,
            certification_exam_name=_CERT,
            ruleset_version=_RULESET,
            model_name=_MODEL,
            thresholds=build_semantic_cluster_thresholds(),
        )
        self.assertIn("mode: dry-run", report)
        self.assertIn("model_download: skipped", report)


class TestLazyImportBehavior(unittest.TestCase):
    def test_resolve_sentence_transformers_package_version_without_import(self):
        with patch.dict(sys.modules, {"sentence_transformers": MagicMock()}):
            version = resolve_sentence_transformers_package_version()
        self.assertIn(version, {"unknown", "3.4.1"})


class TestWorkerIntegration(unittest.TestCase):
    def _heartbeat_response(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "job_status": "running",
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
            "heartbeat_at": "2099-01-01T00:00:00+00:00",
        }

    def _make_job(self, payload: dict) -> dict:
        return {
            "job_id": "job-semantic-01",
            "job_type": _JOB_TYPE,
            "payload": payload,
            "checkpoint": {},
            "attempt": 1,
            "job_status": "running",
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
        }

    @patch(
        "workers.structural_audit_launcher.load_question_version_snapshots_bulk",
        return_value={_QV_A: _SNAPSHOT, _QV_B: _SNAPSHOT},
    )
    @patch(
        "workers.semantic_cluster_detector.orchestrate_certification_semantic_cluster_audit",
        return_value={
            "audit_run_id": "audit-run-semantic-001",
            "run_status": "completed",
            "finding_count": 0,
            "evidence_count": 0,
            "certification_exam_name": _CERT,
            "model_name": _MODEL,
        },
    )
    def test_worker_dispatches_semantic_cluster_audit_and_completes(
        self,
        _orchestrate_mock,
        _snapshots_mock,
    ):
        job = self._make_job(_VALID_PAYLOAD)
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1", [job])
        fake.set_response("heartbeat_background_job_v1", [self._heartbeat_response(job["job_id"])])
        fake.set_response("list_certification_current_question_versions_v1", _LOADER_ROWS)
        fake.set_response("complete_background_job_v1", [{
            "job_id": job["job_id"],
            "job_status": "completed",
            "completed_at": "2099-01-01T00:00:00+00:00",
        }])

        worker = BackgroundWorker(
            worker_id="integration-semantic-worker",
            client=fake,
            handlers=build_handler_registry(fake),
            sleep_interval=0.0,
        )
        worker.run_once()

        self.assertIn("complete_background_job_v1", [c["name"] for c in fake.calls])
        self.assertNotIn("fail_background_job_v1", [c["name"] for c in fake.calls])


class TestEnqueueUtility(unittest.TestCase):
    def test_enqueue_rpc_name(self):
        self.assertEqual(_ENQUEUE_RPC, "enqueue_background_job_v1")


if __name__ == "__main__":
    unittest.main()
