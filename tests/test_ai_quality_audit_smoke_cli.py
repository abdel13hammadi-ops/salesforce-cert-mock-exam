"""
CLI tests for scripts/run_ai_quality_audit_smoke.py (no live DB/providers).
"""

from __future__ import annotations

import json
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.run_ai_quality_audit_smoke import (
    build_create_run_params,
    execute_smoke_batch,
    format_dry_run_report,
    main,
    prepare_all_smoke_evidence,
    prepare_all_smoke_evidence_dry_run,
    resolve_question_version_ids,
)
from workers.ai_quality_audit_evidence import (
    AiQualityAuditEvidenceError,
    RETRIEVAL_REPLAY_EXPORT_BEGIN,
    RETRIEVAL_REPLAY_EXPORT_END,
)
from workers.anthropic_provider import DEFAULT_MODEL, ENV_MODEL

_REQUIRED = 10
_PROVIDER_ENV = {
    "CERTBOUND_LLM_PROVIDER": "anthropic",
}


def _default_evidence_summaries(ids=None):
    ids = ids or _ten_unique_ids()
    return [
        {
            "question_version_id": qvid,
            "chunk_count": 2,
            "evidence_count": 2,
            "evidence_set_hash": "c" * 64,
            "retrieval_method": "bm25_question_match_v1",
            "total_evidence_characters": 1200,
            "estimated_tokens": 300,
            "candidate_count": 20,
            "qualified_candidate_count": 3,
            "selected_count": 2,
            "rejected_below_threshold_count": 17,
            "selected_chunk_ids": [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
            "source_titles": ["Help 1", "Help 2"],
            "chunk_previews": [
                {
                    "resource_chunk_id": "11111111-1111-1111-1111-111111111111",
                    "retrieval_rank": 1,
                    "title": "Help 1",
                    "relevance_score": 0.42,
                    "match_reasons": ["question-text overlap"],
                },
                {
                    "resource_chunk_id": "22222222-2222-2222-2222-222222222222",
                    "retrieval_rank": 2,
                    "title": "Help 2",
                    "relevance_score": 0.31,
                    "match_reasons": ["title match"],
                },
            ],
            "evidence_chunks": [
                {
                    "resource_chunk_id": "11111111-1111-1111-1111-111111111111",
                    "retrieval_rank": 1,
                },
                {
                    "resource_chunk_id": "22222222-2222-2222-2222-222222222222",
                    "retrieval_rank": 2,
                },
            ],
        }
        for qvid in ids
    ]


def _patch_evidence_preview(ids=None):
    return patch.multiple(
        "scripts.run_ai_quality_audit_smoke",
        prepare_all_smoke_evidence_dry_run=MagicMock(
            return_value=_default_evidence_summaries(ids),
        ),
        _load_client_for_evidence_preview=MagicMock(return_value=object()),
    )


def _mixed_evidence_summaries_with_gap(ids=None):
    summaries = _default_evidence_summaries(ids)
    gap_index = 2
    gap_qvid = summaries[gap_index]["question_version_id"]
    summaries[gap_index] = {
        "question_version_id": gap_qvid,
        "certification_exam_name": "Salesforce Certified Platform Administrator",
        "candidate_count": 24,
        "qualified_candidate_count": 0,
        "rejected_below_threshold_count": 24,
        "selected_count": 0,
        "evidence_count": 0,
        "chunk_count": 0,
        "evidence_set_hash": "d" * 64,
        "total_evidence_characters": 0,
        "estimated_tokens": 0,
        "retrieval_method": "lexical_question_match_v2",
        "chunk_previews": [],
        "rejected_previews": [
            {
                "retrieval_rank": 1,
                "resource_chunk_id": "33333333-3333-3333-3333-333333333333",
                "relevance_score": 0.121561,
                "applicable_threshold": 0.20,
                "title": "Considerations for Object Relationships",
                "match_reasons": ["question-text overlap", "metadata/feature match"],
                "rejection_reason": "relevance score 0.121561 below threshold 0.20",
            },
            {
                "retrieval_rank": 2,
                "resource_chunk_id": "44444444-4444-4444-4444-444444444444",
                "relevance_score": 0.082021,
                "applicable_threshold": 0.20,
                "title": "Flow Builder Overview",
                "match_reasons": ["question-text overlap"],
                "rejection_reason": "relevance score 0.082021 below threshold 0.20",
            },
        ],
        "evidence_gap": True,
        "evidence_gap_reason": (
            f"evidence retrieval returned zero qualified chunks for question_version "
            f"{gap_qvid!r} (certification='Salesforce Certified Platform Administrator', "
            "candidates=24, qualified=0, rejected=24)"
        ),
    }
    return summaries


def _ten_unique_ids() -> list[str]:
    return [f"00000000-0000-0000-0000-{index:012x}" for index in range(1, _REQUIRED + 1)]


def _cli_args_with_ids(*, extra: list[str] | None = None) -> list[str]:
    cli_args = list(extra or [])
    for qvid in _ten_unique_ids():
        cli_args.extend(["--question-version-id", qvid])
    return cli_args


class _FakeSelection:
    def __init__(self, ids):
        self.certifications = [type("Cert", (), {"selected": [
            type("Question", (), {"question_version_id": qvid})()
            for qvid in ids
        ]})()]


class TestResolveQuestionVersionIds(unittest.TestCase):

    def test_requires_exactly_ten_explicit_ids(self):
        ids = _ten_unique_ids()
        resolved = resolve_question_version_ids(
            explicit_ids=ids,
            seed=None,
            selection_loader=lambda seed: (_ for _ in ()).throw(RuntimeError("unused")),
        )
        self.assertEqual(resolved, [item.lower() for item in ids])

    def test_rejects_wrong_count(self):
        with self.assertRaisesRegex(ValueError, "exactly 10"):
            resolve_question_version_ids(
                explicit_ids=_ten_unique_ids()[:9],
                seed=None,
                selection_loader=lambda seed: (_ for _ in ()).throw(RuntimeError("unused")),
            )

    def test_rejects_duplicate_ids(self):
        ids = _ten_unique_ids()
        ids[9] = ids[0]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            resolve_question_version_ids(
                explicit_ids=ids,
                seed=None,
                selection_loader=lambda seed: (_ for _ in ()).throw(RuntimeError("unused")),
            )


class TestSmokeCliMain(unittest.TestCase):

    def test_dry_run_is_default(self):
        buffer = io.StringIO()
        evidence_summary = [
            {
                "question_version_id": qvid,
                "chunk_count": 2,
                "evidence_set_hash": "c" * 64,
            }
            for qvid in _ten_unique_ids()
        ]
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with _patch_evidence_preview():
                with redirect_stdout(buffer):
                    rc = main(_cli_args_with_ids())

        self.assertEqual(rc, 0)
        output = buffer.getvalue()
        self.assertIn("AI quality audit smoke dry-run", output)
        self.assertIn(f"primary_model_name: {DEFAULT_MODEL}", output)
        self.assertIn(f"dispute_model_name: {DEFAULT_MODEL}", output)
        self.assertIn("evidence_freeze_preview:", output)
        self.assertIn("evidence_summary:", output)
        self.assertIn("questions_with_qualified_evidence: 10", output)
        self.assertIn("questions_with_evidence_gaps: 0", output)
        self.assertIn("selected=2", output)
        self.assertIn("method=bm25_question_match_v1", output)
        self.assertIn("estimated_tokens=", output)
        self.assertIn("reasons=", output)
        self.assertIn("No jobs enqueued (dry-run).", output)
        self.assertNotIn("smoke-primary-model", output)
        self.assertNotIn("smoke-dispute-model", output)

    def test_dry_run_fails_when_evidence_client_unavailable(self):
        stderr_buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with patch(
                "scripts.run_ai_quality_audit_smoke._load_client_for_evidence_preview",
                side_effect=RuntimeError("no preview client"),
            ):
                with redirect_stderr(stderr_buffer):
                    rc = main(_cli_args_with_ids())

        self.assertEqual(rc, 1)
        self.assertIn("no preview client", stderr_buffer.getvalue())

    def test_dry_run_continues_and_reports_all_questions_when_gaps_exist(self):
        ids = _ten_unique_ids()
        mixed_summaries = _mixed_evidence_summaries_with_gap(ids)
        gap_qvid = mixed_summaries[2]["question_version_id"]

        buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with patch(
                "scripts.run_ai_quality_audit_smoke._load_client_for_evidence_preview",
                return_value=object(),
            ):
                with patch(
                    "scripts.run_ai_quality_audit_smoke.prepare_all_smoke_evidence_dry_run",
                    return_value=mixed_summaries,
                ) as dry_run_mock:
                    with redirect_stdout(buffer):
                        rc = main(_cli_args_with_ids())

        self.assertEqual(rc, 1)
        dry_run_mock.assert_called_once()
        output = buffer.getvalue()
        for qvid in ids:
            self.assertIn(qvid, output)
        self.assertIn("EVIDENCE_GAP", output)
        self.assertIn(gap_qvid, output)
        self.assertIn("rejected_rank=1", output)
        self.assertIn("33333333-3333-3333-3333-333333333333", output)
        self.assertIn("score=0.121561", output)
        self.assertIn("threshold=0.2", output)
        self.assertIn("Considerations for Object Relationships", output)
        self.assertIn("rejection=relevance score 0.121561 below threshold 0.20", output)
        self.assertIn("questions_with_qualified_evidence: 9", output)
        self.assertIn("questions_with_evidence_gaps: 1", output)
        self.assertIn(f"    - {gap_qvid}: EVIDENCE_GAP", output)
        self.assertIn("Dry-run completed with 1 evidence gap(s). Exit code 1.", output)
        self.assertIn("No jobs enqueued (dry-run).", output)

    @patch("scripts.run_ai_quality_audit_smoke.prepare_smoke_evidence_set")
    def test_prepare_all_smoke_evidence_dry_run_continues_after_zero_qualified(
        self,
        prepare_mock,
    ):
        ids = _ten_unique_ids()[:3]
        prepared_ok = MagicMock()
        prepared_ok.qualified_candidate_count = 2
        prepared_ok.to_summary_dict.return_value = {
            "question_version_id": ids[0],
            "qualified_candidate_count": 2,
            "selected_count": 2,
            "evidence_gap": False,
        }
        prepared_gap = MagicMock()
        prepared_gap.question_version_id = ids[1]
        prepared_gap.certification_exam_name = "Cert A"
        prepared_gap.candidate_count = 24
        prepared_gap.qualified_candidate_count = 0
        prepared_gap.rejected_below_threshold_count = 24
        prepared_gap.to_summary_dict.return_value = {
            "question_version_id": ids[1],
            "certification_exam_name": "Cert A",
            "candidate_count": 24,
            "qualified_candidate_count": 0,
            "rejected_below_threshold_count": 24,
            "selected_count": 0,
        }
        prepared_ok_2 = MagicMock()
        prepared_ok_2.qualified_candidate_count = 1
        prepared_ok_2.to_summary_dict.return_value = {
            "question_version_id": ids[2],
            "qualified_candidate_count": 1,
            "selected_count": 1,
        }
        prepare_mock.side_effect = [prepared_ok, prepared_gap, prepared_ok_2]

        client = object()
        summaries = prepare_all_smoke_evidence_dry_run(client, ids)

        self.assertEqual(len(summaries), 3)
        self.assertFalse(summaries[0].get("evidence_gap"))
        self.assertTrue(summaries[1].get("evidence_gap"))
        self.assertFalse(summaries[2].get("evidence_gap"))
        self.assertEqual(prepare_mock.call_count, 3)
        prepare_mock.assert_any_call(client, ids[1], allow_no_evidence=True)

    @patch("scripts.run_ai_quality_audit_smoke.prepare_smoke_evidence_set")
    def test_prepare_all_smoke_evidence_stops_on_zero_qualified(self, prepare_mock):
        ids = _ten_unique_ids()[:2]
        prepare_mock.side_effect = AiQualityAuditEvidenceError("zero chunks")

        client = object()
        with self.assertRaisesRegex(AiQualityAuditEvidenceError, "zero chunks"):
            prepare_all_smoke_evidence(client, ids)

        prepare_mock.assert_called_once_with(client, ids[0])

    def test_format_dry_run_report_includes_top_rejected_candidates_for_gaps(self):
        ids = _ten_unique_ids()
        gap_summary = _mixed_evidence_summaries_with_gap(ids)[2]
        report = format_dry_run_report(
            question_version_ids=ids,
            prompt_version="v48-smoke-prompt-v1",
            ruleset_version="v48-smoke-ruleset-v1",
            primary_model_name=DEFAULT_MODEL,
            dispute_model_name=DEFAULT_MODEL,
            pilot_batch_id="v48-ai-quality-smoke",
            created_by="ai-quality-smoke-cli",
            evidence_summaries=_default_evidence_summaries(ids)[:2] + [gap_summary],
        )

        self.assertIn("rejected_rank=1", report)
        self.assertIn("rejected_rank=2", report)
        self.assertNotIn("rejected_rank=3", report)
        self.assertIn("score=0.121561", report)
        self.assertIn("threshold=0.2", report)
        self.assertIn("rejection=relevance score 0.121561 below threshold 0.20", report)
        self.assertIn("selected=2", report)

    def test_dry_run_shows_configured_anthropic_model(self):
        configured_model = "claude-test-model-v1"
        env = dict(_PROVIDER_ENV)
        env[ENV_MODEL] = configured_model

        buffer = io.StringIO()
        with patch.dict(os.environ, env, clear=False):
            with _patch_evidence_preview():
                with redirect_stdout(buffer):
                    rc = main(_cli_args_with_ids())

        self.assertEqual(rc, 0)
        output = buffer.getvalue()
        self.assertIn(f"primary_model_name: {configured_model}", output)
        self.assertIn(f"dispute_model_name: {configured_model}", output)

    def test_dry_run_rejects_missing_provider_configuration(self):
        stderr_buffer = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CERTBOUND_LLM_PROVIDER", None)
            with redirect_stderr(stderr_buffer):
                rc = main(_cli_args_with_ids())

        self.assertEqual(rc, 1)
        self.assertIn("CERTBOUND_LLM_PROVIDER", stderr_buffer.getvalue())

    def test_removed_cli_model_override_is_not_accepted(self):
        stderr_buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with redirect_stderr(stderr_buffer):
                with self.assertRaises(SystemExit) as ctx:
                    main(
                        _cli_args_with_ids(extra=["--primary-model-name", "fake-model"])
                    )

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("primary-model-name", stderr_buffer.getvalue())

    def test_dry_run_does_not_enqueue_or_execute_batch(self):
        buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with _patch_evidence_preview():
                with patch("scripts.run_ai_quality_audit_smoke.execute_smoke_batch") as execute_mock:
                    with redirect_stdout(buffer):
                        rc = main(_cli_args_with_ids())

        self.assertEqual(rc, 0)
        execute_mock.assert_not_called()

    def test_create_run_params_use_resolved_models_not_placeholders(self):
        params = build_create_run_params(
            question_version_id=_ten_unique_ids()[0],
            prompt_version="v48-smoke-prompt-v1",
            ruleset_version="v48-smoke-ruleset-v1",
            primary_model_name=DEFAULT_MODEL,
            dispute_model_name=DEFAULT_MODEL,
            pilot_batch_id="v48-ai-quality-smoke",
            created_by="ai-quality-smoke-cli",
            evidence_set_hash="a" * 64,
            evidence_chunks=[
                {
                    "resource_chunk_id": "11111111-1111-1111-1111-111111111111",
                    "retrieval_rank": 1,
                }
            ],
        )
        self.assertEqual(params["p_primary_model_name"], DEFAULT_MODEL)
        self.assertEqual(params["p_dispute_model_name"], DEFAULT_MODEL)
        self.assertEqual(len(params["p_evidence_chunks"]), 1)
        self.assertNotIn("smoke-primary-model", params.values())
        self.assertNotIn("smoke-dispute-model", params.values())

    def test_execute_refuses_empty_evidence_summary(self):
        client = object()
        qvid = _ten_unique_ids()[0]
        with self.assertRaisesRegex(
            AiQualityAuditEvidenceError,
            "refusing to enqueue smoke job",
        ):
            execute_smoke_batch(
                client,
                question_version_ids=[qvid],
                prompt_version="v48-smoke-prompt-v1",
                ruleset_version="v48-smoke-ruleset-v1",
                primary_model_name=DEFAULT_MODEL,
                dispute_model_name=DEFAULT_MODEL,
                pilot_batch_id="v48-ai-quality-smoke",
                created_by="ai-quality-smoke-cli",
                evidence_summaries=[
                    {
                        "question_version_id": qvid,
                        "chunk_count": 0,
                        "evidence_set_hash": "a" * 64,
                        "evidence_chunks": [],
                    }
                ],
            )

    def test_execute_requires_confirm(self):
        stderr_buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with redirect_stderr(stderr_buffer):
                rc = main(_cli_args_with_ids(extra=["--execute"]))

        self.assertEqual(rc, 1)
        self.assertIn("--execute requires --confirm", stderr_buffer.getvalue())

    @patch("scripts.run_ai_quality_audit_smoke._load_client_for_selection")
    @patch("scripts.run_ai_quality_audit_smoke.select_quality_audit_smoke_questions")
    def test_seed_dry_run_loads_ten_ids(self, selection_mock, _client_mock):
        ids = _ten_unique_ids()
        selection_mock.return_value = _FakeSelection(ids)
        _client_mock.return_value = object()

        buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with _patch_evidence_preview():
                with redirect_stdout(buffer):
                    rc = main(["--seed", "42"])

        self.assertEqual(rc, 0)
        for qvid in ids:
            self.assertIn(qvid.lower(), buffer.getvalue())
        self.assertIn(f"primary_model_name: {DEFAULT_MODEL}", buffer.getvalue())


class TestRetrievalReplayExportCli(unittest.TestCase):

    def test_export_retrieval_replay_rejected_with_execute(self):
        stderr_buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with redirect_stderr(stderr_buffer):
                rc = main(
                    _cli_args_with_ids(
                        extra=["--export-retrieval-replay", "-", "--execute", "--confirm"]
                    )
                )

        self.assertEqual(rc, 1)
        self.assertIn(
            "--export-retrieval-replay is allowed only in dry-run mode",
            stderr_buffer.getvalue(),
        )

    def test_dry_run_export_writes_compact_json_to_stdout(self):
        replay_export = {
            "export_version": 1,
            "questions": [{"question_version_id": _ten_unique_ids()[0], "candidates": []}],
            "retrieval_method": "bm25_question_match_v1",
        }
        buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with _patch_evidence_preview():
                with patch(
                    "scripts.run_ai_quality_audit_smoke.prepare_smoke_retrieval_replay_export",
                    return_value=replay_export,
                ) as export_mock:
                    with redirect_stdout(buffer):
                        rc = main(
                            _cli_args_with_ids(extra=["--export-retrieval-replay", "-"])
                        )

        self.assertEqual(rc, 0)
        export_mock.assert_called_once()
        output = buffer.getvalue()
        self.assertIn("AI quality audit smoke dry-run", output)
        self.assertIn(RETRIEVAL_REPLAY_EXPORT_BEGIN, output)
        self.assertIn(RETRIEVAL_REPLAY_EXPORT_END, output)
        exported_json = output.split(RETRIEVAL_REPLAY_EXPORT_BEGIN, 1)[1].split(
            RETRIEVAL_REPLAY_EXPORT_END,
            1,
        )[0].strip()
        self.assertEqual(json.loads(exported_json), replay_export)

    def test_dry_run_without_export_leaves_behavior_unchanged(self):
        buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with _patch_evidence_preview():
                with patch(
                    "scripts.run_ai_quality_audit_smoke.prepare_smoke_retrieval_replay_export"
                ) as export_mock:
                    with redirect_stdout(buffer):
                        rc = main(_cli_args_with_ids())

        self.assertEqual(rc, 0)
        export_mock.assert_not_called()
        self.assertNotIn(RETRIEVAL_REPLAY_EXPORT_BEGIN, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
