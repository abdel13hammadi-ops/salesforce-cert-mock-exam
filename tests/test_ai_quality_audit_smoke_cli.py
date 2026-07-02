"""
CLI tests for scripts/run_ai_quality_audit_smoke.py (no live DB/providers).
"""

from __future__ import annotations

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
    main,
    prepare_all_smoke_evidence,
    resolve_question_version_ids,
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
            "retrieval_method": "lexical_question_match_v2",
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
        prepare_all_smoke_evidence=MagicMock(
            return_value=_default_evidence_summaries(ids),
        ),
        _load_client_for_evidence_preview=MagicMock(return_value=object()),
    )


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
        self.assertIn("selected=2", output)
        self.assertIn("method=lexical_question_match_v2", output)
        self.assertIn("estimated_tokens=", output)
        self.assertIn("reasons=", output)
        self.assertIn("No jobs enqueued (dry-run).", output)
        self.assertNotIn("smoke-primary-model", output)
        self.assertNotIn("smoke-dispute-model", output)

    def test_dry_run_fails_when_evidence_preparation_fails(self):
        from workers.ai_quality_audit_evidence import AiQualityAuditEvidenceError

        stderr_buffer = io.StringIO()
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with patch(
                "scripts.run_ai_quality_audit_smoke._load_client_for_evidence_preview",
                return_value=object(),
            ):
                with patch(
                    "scripts.run_ai_quality_audit_smoke.prepare_all_smoke_evidence",
                    side_effect=AiQualityAuditEvidenceError("zero chunks"),
                ):
                    with redirect_stderr(stderr_buffer):
                        rc = main(_cli_args_with_ids())

        self.assertEqual(rc, 1)
        self.assertIn("zero chunks", stderr_buffer.getvalue())

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
        from workers.ai_quality_audit_evidence import AiQualityAuditEvidenceError

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


if __name__ == "__main__":
    unittest.main()
