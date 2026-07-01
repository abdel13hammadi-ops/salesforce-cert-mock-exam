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
    main,
    resolve_question_version_ids,
)

_REQUIRED = 10


def _ten_unique_ids() -> list[str]:
    return [f"00000000-0000-0000-0000-{index:012x}" for index in range(1, _REQUIRED + 1)]


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
        ids = _ten_unique_ids()
        cli_args = []
        for qvid in ids:
            cli_args.extend(["--question-version-id", qvid])

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = main(cli_args)

        self.assertEqual(rc, 0)
        output = buffer.getvalue()
        self.assertIn("AI quality audit smoke dry-run", output)
        self.assertIn("No jobs enqueued (dry-run).", output)

    def test_execute_requires_confirm(self):
        ids = _ten_unique_ids()
        cli_args = ["--execute"]
        for qvid in ids:
            cli_args.extend(["--question-version-id", qvid])

        stderr_buffer = io.StringIO()
        with redirect_stderr(stderr_buffer):
            rc = main(cli_args)

        self.assertEqual(rc, 1)
        self.assertIn("--execute requires --confirm", stderr_buffer.getvalue())

    @patch("scripts.run_ai_quality_audit_smoke._load_client_for_selection")
    @patch("scripts.run_ai_quality_audit_smoke.select_quality_audit_smoke_questions")
    def test_seed_dry_run_loads_ten_ids(self, selection_mock, _client_mock):
        ids = _ten_unique_ids()
        selection_mock.return_value = _FakeSelection(ids)
        _client_mock.return_value = object()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = main(["--seed", "42"])

        self.assertEqual(rc, 0)
        for qvid in ids:
            self.assertIn(qvid.lower(), buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
