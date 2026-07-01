"""
CLI tests for scripts/run_ai_quality_audit_smoke.py (no live DB/providers).
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.run_ai_quality_audit_smoke import (
    build_create_run_params,
    main,
    resolve_question_version_ids,
)
from workers.anthropic_provider import DEFAULT_MODEL, ENV_MODEL

_REQUIRED = 10
_PROVIDER_ENV = {
    "CERTBOUND_LLM_PROVIDER": "anthropic",
}


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
        with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
            with redirect_stdout(buffer):
                rc = main(_cli_args_with_ids())

        self.assertEqual(rc, 0)
        output = buffer.getvalue()
        self.assertIn("AI quality audit smoke dry-run", output)
        self.assertIn(f"primary_model_name: {DEFAULT_MODEL}", output)
        self.assertIn(f"dispute_model_name: {DEFAULT_MODEL}", output)
        self.assertIn("No jobs enqueued (dry-run).", output)
        self.assertNotIn("smoke-primary-model", output)
        self.assertNotIn("smoke-dispute-model", output)

    def test_dry_run_shows_configured_anthropic_model(self):
        configured_model = "claude-test-model-v1"
        env = dict(_PROVIDER_ENV)
        env[ENV_MODEL] = configured_model

        buffer = io.StringIO()
        with patch.dict(os.environ, env, clear=False):
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
        )
        self.assertEqual(params["p_primary_model_name"], DEFAULT_MODEL)
        self.assertEqual(params["p_dispute_model_name"], DEFAULT_MODEL)
        self.assertNotIn("smoke-primary-model", params.values())
        self.assertNotIn("smoke-dispute-model", params.values())

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
            with redirect_stdout(buffer):
                rc = main(["--seed", "42"])

        self.assertEqual(rc, 0)
        for qvid in ids:
            self.assertIn(qvid.lower(), buffer.getvalue())
        self.assertIn(f"primary_model_name: {DEFAULT_MODEL}", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
