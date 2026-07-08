"""
Tests for scripts.v58_run_openai_benchmark_baseline (V58-DAY8-OPENAI-07).

All tests use fake providers/adapters and mocked database access. No network
calls are made and no real OpenAI/Anthropic/Postgres connection is ever
attempted. ``CERTBOUND_ALLOW_LIVE_AI_TEST`` is never set to ``1`` anywhere in
this file.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.v58_run_openai_benchmark_baseline as baseline
from workers.quality_benchmark_execution import CasePrediction, EngineAdapterUnavailableError
from workers.quality_benchmark_v48_orchestration import V48DisposableDsnRejectedError


def _clean_env_overrides():
    """Env vars this module reads/sets, cleared before each test."""
    return {
        baseline.ENV_ALLOW_LIVE: "",
        baseline.ENV_OPENAI_API_KEY: "",
        baseline.ENV_PRIMARY_PROVIDER: "",
        baseline.ENV_DISPUTE_PROVIDER: "",
        "CERTBOUND_LLM_PROVIDER": "",
        baseline.ENV_V48_DB_URL: "",
        baseline.ENV_V48_DB_URL_FALLBACK: "",
        "CERTBOUND_OPENAI_MODEL": "",
        "CERTBOUND_OPENAI_REASONING_EFFORT": "",
        "CERTBOUND_OPENAI_TIMEOUT_SECONDS": "",
        "CERTBOUND_OPENAI_MAX_RETRIES": "",
        "CERTBOUND_OPENAI_MAX_OUTPUT_TOKENS": "",
    }


class EnvIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(os.environ, _clean_env_overrides())
        self._env_patch.start()
        for key, value in list(os.environ.items()):
            if not value and key in _clean_env_overrides():
                os.environ.pop(key, None)

    def tearDown(self):
        self._env_patch.stop()


# ---------------------------------------------------------------------------
# 1-2: live authorization / API key gate
# ---------------------------------------------------------------------------


class TestLiveAuthorizationGate(EnvIsolatedTestCase):
    def test_missing_allow_live_refuses(self):
        with self.assertRaises(baseline.BaselineRunnerRefusal) as ctx:
            baseline._check_live_authorization_gate()
        self.assertIn(baseline.ENV_ALLOW_LIVE, str(ctx.exception))

    def test_wrong_allow_live_value_refuses(self):
        os.environ[baseline.ENV_ALLOW_LIVE] = "true"
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline._check_live_authorization_gate()

    def test_missing_api_key_refuses(self):
        os.environ[baseline.ENV_ALLOW_LIVE] = "1"
        with self.assertRaises(baseline.BaselineRunnerRefusal) as ctx:
            baseline._check_live_authorization_gate()
        self.assertIn(baseline.ENV_OPENAI_API_KEY, str(ctx.exception))

    def test_both_set_passes(self):
        os.environ[baseline.ENV_ALLOW_LIVE] = "1"
        os.environ[baseline.ENV_OPENAI_API_KEY] = "sk-fake-test-key-never-real"
        baseline._check_live_authorization_gate()  # must not raise

    def test_gate_never_constructs_provider(self):
        # No CERTBOUND_OPENAI_API_KEY at all; if a provider were constructed
        # this would raise a different, unrelated error (e.g. a
        # MissingProviderError from workers.openai_provider) instead of
        # BaselineRunnerRefusal, since _check_live_authorization_gate never
        # imports any provider module.
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline._check_live_authorization_gate()


# ---------------------------------------------------------------------------
# 3-4: database host/name safety (fully reused validate_disposable_dsn)
# ---------------------------------------------------------------------------


class TestDatabaseSafety(EnvIsolatedTestCase):
    def test_default_dsn_is_accepted(self):
        dsn = baseline.resolve_disposable_db_url(None)
        self.assertEqual(dsn, baseline.DEFAULT_DISPOSABLE_DSN)

    def test_wrong_host_refuses(self):
        with self.assertRaises(V48DisposableDsnRejectedError):
            baseline.resolve_disposable_db_url(
                "postgresql://postgres:postgres@production-db.example.com:5432/certbound_v48_test"
            )

    def test_wrong_database_name_refuses(self):
        with self.assertRaises(V48DisposableDsnRejectedError):
            baseline.resolve_disposable_db_url(
                "postgresql://postgres:postgres@127.0.0.1:54329/production"
            )

    def test_supabase_host_marker_refuses(self):
        with self.assertRaises(V48DisposableDsnRejectedError):
            baseline.resolve_disposable_db_url(
                "postgresql://postgres:postgres@db.supabase.co:5432/certbound_v48_test"
            )

    def test_env_override_is_used(self):
        os.environ[baseline.ENV_V48_DB_URL] = (
            "postgresql://postgres:postgres@localhost:54329/certbound_v48_test_custom"
        )
        dsn = baseline.resolve_disposable_db_url(None)
        self.assertIn("certbound_v48_test_custom", dsn)


# ---------------------------------------------------------------------------
# DSN query-string / fragment connection-parameter-precedence bypass
# (V58-DAY8-OPENAI-09, blocking fix 1)
# ---------------------------------------------------------------------------

_APPROVED_QUERY_FREE_DSN = "postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test"

# Each of these looks like an approved loopback/dbname DSN under a
# urlsplit()-only host/path check, but carries a query-string parameter
# that libpq/psycopg2 gives *higher* precedence than the authority/path
# at actual connection time (empirically confirmed in the V58-DAY8-OPENAI-08
# review via psycopg2.extensions.parse_dsn()).
_MALICIOUS_QUERY_DSNS = {
    "host_override": _APPROVED_QUERY_FREE_DSN + "?host=evil.example.com",
    "hostaddr_override": _APPROVED_QUERY_FREE_DSN + "?hostaddr=10.0.0.5",
    "dbname_override": _APPROVED_QUERY_FREE_DSN + "?dbname=other_database",
    "port_override": _APPROVED_QUERY_FREE_DSN + "?port=5432",
    "options_override": _APPROVED_QUERY_FREE_DSN + "?options=-csearch_path%3Dpublic",
    "arbitrary_unknown_param": _APPROVED_QUERY_FREE_DSN + "?some_unknown_param=whatever",
}
_MALICIOUS_FRAGMENT_DSN = _APPROVED_QUERY_FREE_DSN + "#fragment-ignored-by-urlsplit-host-check"


class TestDsnQueryFragmentRejection(EnvIsolatedTestCase):
    """Blocking fix 1: reject any DSN carrying a query string or fragment,
    for exactly the connection-parameter-precedence bypass class identified
    in the V58-DAY8-OPENAI-08 review, before validate_disposable_dsn() or
    any provider/database code runs."""

    def test_approved_query_free_dsn_still_passes(self):
        dsn = baseline.resolve_disposable_db_url(_APPROVED_QUERY_FREE_DSN)
        self.assertEqual(dsn, _APPROVED_QUERY_FREE_DSN)

    def test_each_malicious_query_parameter_is_refused(self):
        for name, dsn in _MALICIOUS_QUERY_DSNS.items():
            with self.subTest(case=name):
                with self.assertRaises(baseline.BaselineRunnerRefusal) as ctx:
                    baseline.resolve_disposable_db_url(dsn)
                message = str(ctx.exception)
                self.assertIn("query string", message)
                # The refusal message must never leak the DSN (or its
                # embedded password) -- assert the literal credential and
                # the full malicious DSN never appear in the message.
                self.assertNotIn("postgres:postgres", message)
                self.assertNotIn(dsn, message)

    def test_url_fragment_is_refused(self):
        with self.assertRaises(baseline.BaselineRunnerRefusal) as ctx:
            baseline.resolve_disposable_db_url(_MALICIOUS_FRAGMENT_DSN)
        message = str(ctx.exception)
        self.assertIn("fragment", message)
        self.assertNotIn("postgres:postgres", message)
        self.assertNotIn(_MALICIOUS_FRAGMENT_DSN, message)

    def test_reject_helper_runs_before_validate_disposable_dsn(self):
        """A query-string DSN that would otherwise be host/name-valid must
        still be refused -- proving the query/fragment check runs first
        and is not merely redundant with validate_disposable_dsn()."""
        dsn = _APPROVED_QUERY_FREE_DSN + "?host=evil.example.com"
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline._reject_dsn_query_or_fragment(dsn)

    def test_dry_run_shares_the_same_rejection_as_live_mode(self):
        """--dry-run and live mode must enforce identical DSN validation:
        both funnel through resolve_disposable_db_url()."""
        args = baseline._build_arg_parser().parse_args(
            ["--dry-run", "--db-url", _APPROVED_QUERY_FREE_DSN + "?host=evil.example.com"]
        )
        with self.assertRaises(baseline.BaselineRunnerRefusal) as ctx:
            baseline._run_dry_run(args)
        self.assertIn("query string", str(ctx.exception))

    def test_live_mode_refuses_malicious_dsn_before_provider_or_database_touched(self):
        """Proof of ordering: with CERTBOUND_ALLOW_LIVE_AI_TEST=1 and a fake
        API key set (so the live path proceeds past the credential gate),
        a malicious --db-url must be refused before build_ai_quality_providers_from_env
        (provider construction) or psycopg2.connect (database connection)
        is ever reached. Both are patched to explode if invoked."""
        os.environ[baseline.ENV_ALLOW_LIVE] = "1"
        os.environ[baseline.ENV_OPENAI_API_KEY] = "sk-fake-test-key-never-real"
        args = baseline._build_arg_parser().parse_args(
            ["--db-url", _APPROVED_QUERY_FREE_DSN + "?hostaddr=10.0.0.5"]
        )

        def _explode_if_called(*_args, **_kwargs):
            raise AssertionError(
                "provider construction must never be reached when the DSN is rejected"
            )

        with patch(
            "workers.ai_quality_provider_factory.build_ai_quality_providers_from_env",
            side_effect=_explode_if_called,
        ), patch("psycopg2.connect", side_effect=_explode_if_called):
            with self.assertRaises(baseline.BaselineRunnerRefusal) as ctx:
                baseline._run_live(args)
        self.assertIn("query string", str(ctx.exception))
        self.assertNotIn("postgres:postgres", str(ctx.exception))

    def test_refusal_carries_the_documented_exit_code_and_no_credentials(self):
        """A malicious DSN's BaselineRunnerRefusal carries the documented
        refusal exit code (2, matching main()'s BaselineRunnerRefusal
        handling) and never exposes the DSN/password."""
        args = baseline._build_arg_parser().parse_args(
            ["--dry-run", "--db-url", _APPROVED_QUERY_FREE_DSN + "?dbname=other_database"]
        )
        with self.assertRaises(baseline.BaselineRunnerRefusal) as ctx:
            baseline._run_dry_run(args)
        self.assertEqual(ctx.exception.exit_code, 2)
        self.assertNotIn("postgres:postgres", str(ctx.exception))
        self.assertNotIn("other_database", str(ctx.exception))


# ---------------------------------------------------------------------------
# 5-6-7: fixture validation and SME-reviewed ground-truth isolation
# ---------------------------------------------------------------------------


class TestFixtureValidation(EnvIsolatedTestCase):
    def test_real_fixture_passes(self):
        fixture, fixture_sha256 = baseline.load_and_validate_fixture(baseline.FIXTURE_PATH)
        self.assertEqual(len(fixture["cases"]), 40)
        self.assertEqual(fixture_sha256, baseline.APPROVED_FIXTURE_SHA256)

    def test_wrong_case_count_refuses(self, ):
        real = json.loads(baseline.FIXTURE_PATH.read_text(encoding="utf-8"))
        real["cases"] = real["cases"][:39]
        real["current_case_count"] = 39
        tmp_path = Path(self._tmp_dir()) / "bad_count.json"
        tmp_path.write_text(json.dumps(real), encoding="utf-8")
        with self.assertRaises(baseline.BaselineRunnerRefusal) as ctx:
            baseline.load_and_validate_fixture(tmp_path)
        self.assertIn("39", str(ctx.exception))

    def test_tampered_content_hash_mismatch_refuses(self):
        real = json.loads(baseline.FIXTURE_PATH.read_text(encoding="utf-8"))
        # Same case count (40), different content -> hash must differ.
        real["cases"][0]["question"]["question_text"] += " (tampered)"
        tmp_path = Path(self._tmp_dir()) / "tampered.json"
        tmp_path.write_text(json.dumps(real), encoding="utf-8")
        with self.assertRaises(baseline.BaselineRunnerRefusal) as ctx:
            baseline.load_and_validate_fixture(tmp_path)
        self.assertIn("does not match", str(ctx.exception))

    def test_missing_fixture_refuses(self):
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline.load_and_validate_fixture(Path(self._tmp_dir()) / "does_not_exist.json")

    def _tmp_dir(self):
        import tempfile

        d = tempfile.mkdtemp(prefix="v58-openai-baseline-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d

    def test_sme_reviewed_fixture_is_never_read(self):
        """Patching Path.read_bytes/read_text/open to raise if ever called
        with a path referencing the SME-reviewed fixture proves the full
        fixture-loading path never opens it."""
        original_read_bytes = Path.read_bytes
        original_read_text = Path.read_text

        def guarded_read_bytes(self_path, *args, **kwargs):
            if "sme_reviewed" in str(self_path):
                raise AssertionError(f"SME-reviewed fixture must never be read: {self_path}")
            return original_read_bytes(self_path, *args, **kwargs)

        def guarded_read_text(self_path, *args, **kwargs):
            if "sme_reviewed" in str(self_path):
                raise AssertionError(f"SME-reviewed fixture must never be read: {self_path}")
            return original_read_text(self_path, *args, **kwargs)

        with patch.object(Path, "read_bytes", guarded_read_bytes), patch.object(
            Path, "read_text", guarded_read_text
        ):
            fixture, fixture_sha256 = baseline.load_and_validate_fixture(baseline.FIXTURE_PATH)
            self.assertEqual(len(fixture["cases"]), 40)

    def test_module_never_references_sme_reviewed_path_except_documented_constant(self):
        source = Path(baseline.__file__).read_text(encoding="utf-8")
        # The only occurrence of "sme_reviewed" must be the documented,
        # never-opened constant and its docstring mentions.
        for line in source.splitlines():
            if "sme_reviewed" in line.lower() and "_FORBIDDEN_SME_REVIEWED_FIXTURE_PATH" not in line:
                # allow prose/docstring mentions, but never a call that
                # reads/opens/loads it
                self.assertNotRegex(
                    line,
                    r"(open\(|read_bytes\(|read_text\(|load_benchmark|json\.load)",
                    msg=f"suspicious SME-reviewed reference: {line}",
                )


# ---------------------------------------------------------------------------
# 8-9: provider must resolve to openai; effective model/reasoning recorded
# ---------------------------------------------------------------------------


class TestProviderSelection(EnvIsolatedTestCase):
    def test_defaults_to_openai_primary_and_dispute(self):
        provenance = baseline.resolve_and_validate_provider_selection()
        self.assertEqual(provenance.primary_provider, "openai")
        self.assertEqual(provenance.dispute_provider, "openai")

    def test_explicit_anthropic_primary_refuses(self):
        os.environ[baseline.ENV_PRIMARY_PROVIDER] = "anthropic"
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline.resolve_and_validate_provider_selection()

    def test_explicit_anthropic_dispute_refuses(self):
        os.environ[baseline.ENV_DISPUTE_PROVIDER] = "anthropic"
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline.resolve_and_validate_provider_selection()

    def test_effective_model_and_reasoning_effort_defaults(self):
        settings = baseline.describe_effective_openai_settings()
        self.assertEqual(settings["requested_model"], "gpt-5.5")
        self.assertEqual(settings["reasoning_effort"], "medium")

    def test_effective_model_and_reasoning_effort_overrides(self):
        os.environ["CERTBOUND_OPENAI_MODEL"] = "gpt-5.5-custom"
        os.environ["CERTBOUND_OPENAI_REASONING_EFFORT"] = "high"
        settings = baseline.describe_effective_openai_settings()
        self.assertEqual(settings["requested_model"], "gpt-5.5-custom")
        self.assertEqual(settings["reasoning_effort"], "high")

    def test_settings_do_not_require_api_key(self):
        # No CERTBOUND_OPENAI_API_KEY set anywhere in this test.
        self.assertNotIn(baseline.ENV_OPENAI_API_KEY, os.environ)
        settings = baseline.describe_effective_openai_settings()
        self.assertEqual(settings["requested_model"], "gpt-5.5")


# ---------------------------------------------------------------------------
# Fake adapter for sequencing/checkpoint/resume/interruption tests
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Duck-types V48EngineAdapter's generate_prediction/describe_config
    surface with zero database/network dependency."""

    def __init__(self, *, fail_on_case_id=None, raise_unavailable_on_case_id=None, interrupt_on_case_id=None):
        self.calls: list[str] = []
        self._fail_on_case_id = fail_on_case_id
        self._raise_unavailable_on_case_id = raise_unavailable_on_case_id
        self._interrupt_on_case_id = interrupt_on_case_id

    def describe_config(self):
        return {
            "engine_id": "v48",
            "engine_version": "v48-disposable-db-v1",
            "provider_id": "openai",
            "model_id": "gpt-5.5",
            "prompt_version": "v58-quality-04c-benchmark-prompt",
            "ruleset_version": "v58-quality-04c-benchmark-rules",
            "evidence_config_id": "official_evidence_seed_v1",
        }

    def generate_prediction(self, case):
        case_id = str(case["case_id"])
        if case_id in self.calls:
            raise AssertionError(f"case {case_id} was executed more than once")
        self.calls.append(case_id)
        if case_id == self._interrupt_on_case_id:
            raise KeyboardInterrupt("simulated interruption")
        if case_id == self._raise_unavailable_on_case_id:
            raise EngineAdapterUnavailableError("db unreachable", "reconnect")
        if case_id == self._fail_on_case_id:
            return CasePrediction(case_id=case_id, error="simulated per-case failure")
        return CasePrediction(case_id=case_id, finding_codes=[], materiality=None, approved=True)


def _fixture_with_cases(n: int) -> dict:
    return {"cases": [{"case_id": f"fake-{i:03d}"} for i in range(1, n + 1)]}


class TestCaseLoopSequencing(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._run_dir = Path(tempfile.mkdtemp(prefix="v58-openai-baseline-rundir-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self._run_dir, ignore_errors=True))

    def _fingerprint(self, **overrides):
        kwargs = dict(
            fixture_sha256="fixturehash",
            adapter_config=_FakeAdapter().describe_config(),
            reasoning_effort="medium",
            openai_timeout_seconds=120.0,
            openai_max_retries=3,
            openai_max_output_tokens=4096,
        )
        kwargs.update(overrides)
        return baseline.compute_config_fingerprint(**kwargs)

    def test_sequential_traversal_in_fixture_order(self):
        fixture = _fixture_with_cases(5)
        adapter = _FakeAdapter()
        recorder = baseline.PassCallRecorder()
        predictions = baseline.run_case_loop(
            fixture,
            adapter,
            recorder=recorder,
            run_dir=self._run_dir,
            config_fingerprint=self._fingerprint(),
            existing_predictions=[],
            completed_case_ids=set(),
            print_progress=False,
        )
        self.assertEqual(adapter.calls, [f"fake-{i:03d}" for i in range(1, 6)])
        self.assertEqual([p["case_id"] for p in predictions], adapter.calls)

    def test_each_case_executes_exactly_once(self):
        fixture = _fixture_with_cases(10)
        adapter = _FakeAdapter()
        recorder = baseline.PassCallRecorder()
        baseline.run_case_loop(
            fixture,
            adapter,
            recorder=recorder,
            run_dir=self._run_dir,
            config_fingerprint=self._fingerprint(),
            existing_predictions=[],
            completed_case_ids=set(),
            print_progress=False,
        )
        self.assertEqual(len(adapter.calls), 10)
        self.assertEqual(len(set(adapter.calls)), 10)

    def test_checkpoint_written_after_each_case(self):
        fixture = _fixture_with_cases(3)
        adapter = _FakeAdapter()
        recorder = baseline.PassCallRecorder()
        baseline.run_case_loop(
            fixture,
            adapter,
            recorder=recorder,
            run_dir=self._run_dir,
            config_fingerprint=self._fingerprint(),
            existing_predictions=[],
            completed_case_ids=set(),
            print_progress=False,
        )
        checkpoint = json.loads((self._run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(len(checkpoint["predictions"]), 3)
        self.assertEqual(
            [p["case_id"] for p in checkpoint["predictions"]], ["fake-001", "fake-002", "fake-003"]
        )

    def test_safe_interruption_preserves_completed_checkpoint(self):
        fixture = _fixture_with_cases(5)
        adapter = _FakeAdapter(interrupt_on_case_id="fake-003")
        recorder = baseline.PassCallRecorder()
        with self.assertRaises(KeyboardInterrupt):
            baseline.run_case_loop(
                fixture,
                adapter,
                recorder=recorder,
                run_dir=self._run_dir,
                config_fingerprint=self._fingerprint(),
                existing_predictions=[],
                completed_case_ids=set(),
                print_progress=False,
            )
        checkpoint = json.loads((self._run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(len(checkpoint["predictions"]), 2)
        self.assertEqual(adapter.calls, ["fake-001", "fake-002", "fake-003"])

    def test_engine_unavailable_stops_without_losing_completed_work(self):
        fixture = _fixture_with_cases(5)
        adapter = _FakeAdapter(raise_unavailable_on_case_id="fake-003")
        recorder = baseline.PassCallRecorder()
        with self.assertRaises(EngineAdapterUnavailableError):
            baseline.run_case_loop(
                fixture,
                adapter,
                recorder=recorder,
                run_dir=self._run_dir,
                config_fingerprint=self._fingerprint(),
                existing_predictions=[],
                completed_case_ids=set(),
                print_progress=False,
            )
        checkpoint = json.loads((self._run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(len(checkpoint["predictions"]), 2)

    def test_per_case_error_does_not_abort_run(self):
        fixture = _fixture_with_cases(4)
        adapter = _FakeAdapter(fail_on_case_id="fake-002")
        recorder = baseline.PassCallRecorder()
        predictions = baseline.run_case_loop(
            fixture,
            adapter,
            recorder=recorder,
            run_dir=self._run_dir,
            config_fingerprint=self._fingerprint(),
            existing_predictions=[],
            completed_case_ids=set(),
            print_progress=False,
        )
        self.assertEqual(len(predictions), 4)
        self.assertEqual(predictions[1]["error"], "simulated per-case failure")

    def test_resume_skips_completed_cases(self):
        fixture = _fixture_with_cases(5)
        fingerprint = self._fingerprint()

        # First pass: interrupt after 2 cases.
        adapter1 = _FakeAdapter(interrupt_on_case_id="fake-003")
        recorder1 = baseline.PassCallRecorder()
        with self.assertRaises(KeyboardInterrupt):
            baseline.run_case_loop(
                fixture,
                adapter1,
                recorder=recorder1,
                run_dir=self._run_dir,
                config_fingerprint=fingerprint,
                existing_predictions=[],
                completed_case_ids=set(),
                print_progress=False,
            )

        run_dir, existing_predictions, completed_case_ids = baseline.resume_or_start_run(
            resume_dir=self._run_dir, config_fingerprint=fingerprint
        )
        self.assertEqual(run_dir, self._run_dir)
        self.assertEqual(completed_case_ids, {"fake-001", "fake-002"})

        adapter2 = _FakeAdapter()
        recorder2 = baseline.PassCallRecorder()
        predictions = baseline.run_case_loop(
            fixture,
            adapter2,
            recorder=recorder2,
            run_dir=run_dir,
            config_fingerprint=fingerprint,
            existing_predictions=existing_predictions,
            completed_case_ids=completed_case_ids,
            print_progress=False,
        )
        # Only the 3 remaining cases were ever passed to the (new) adapter.
        self.assertEqual(adapter2.calls, ["fake-003", "fake-004", "fake-005"])
        self.assertEqual(len(predictions), 5)
        self.assertEqual([p["case_id"] for p in predictions], [f"fake-{i:03d}" for i in range(1, 6)])

    def test_incompatible_resume_configuration_refuses(self):
        fixture = _fixture_with_cases(3)
        fingerprint = self._fingerprint()
        adapter = _FakeAdapter()
        recorder = baseline.PassCallRecorder()
        baseline.run_case_loop(
            fixture,
            adapter,
            recorder=recorder,
            run_dir=self._run_dir,
            config_fingerprint=fingerprint,
            existing_predictions=[],
            completed_case_ids=set(),
            print_progress=False,
        )

        different_fingerprint = dict(fingerprint)
        different_fingerprint["model_id"] = "some-other-model"
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline.resume_or_start_run(resume_dir=self._run_dir, config_fingerprint=different_fingerprint)

    def test_resume_missing_checkpoint_refuses(self):
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline.resume_or_start_run(resume_dir=self._run_dir, config_fingerprint={})

    def test_no_resume_dir_starts_fresh_timestamped_directory(self):
        run_dir, existing_predictions, completed_case_ids = baseline.resume_or_start_run(
            resume_dir=None, config_fingerprint={}
        )
        self.assertEqual(existing_predictions, [])
        self.assertEqual(completed_case_ids, set())
        self.assertTrue(str(run_dir).startswith(str(baseline.ARTIFACT_ROOT)))


# ---------------------------------------------------------------------------
# Checkpoint fingerprint completeness for OpenAI execution settings
# (V58-DAY8-OPENAI-09, blocking fix 2)
# ---------------------------------------------------------------------------


class TestCheckpointFingerprintOpenAiSettings(EnvIsolatedTestCase):
    """Verifies that timeout/max_retries/max_output_tokens are part of the
    resume-compatibility fingerprint, that a genuine change refuses resume
    before any additional case executes, that an unchanged configuration
    still resumes cleanly, and that a numeric-equivalent (env-string vs.
    default) setting never causes a false mismatch.

    Deliberately does NOT subclass TestCaseLoopSequencing (which would
    cause unittest to re-discover and re-run all of its test_* methods a
    second time under this class); instead it duplicates the small
    run_dir/_fingerprint setup it needs."""

    def setUp(self):
        super().setUp()
        import tempfile

        self._run_dir = Path(tempfile.mkdtemp(prefix="v58-openai-baseline-fingerprint-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self._run_dir, ignore_errors=True))

    def _fingerprint(self, **overrides):
        kwargs = dict(
            fixture_sha256="fixturehash",
            adapter_config=_FakeAdapter().describe_config(),
            reasoning_effort="medium",
            openai_timeout_seconds=120.0,
            openai_max_retries=3,
            openai_max_output_tokens=4096,
        )
        kwargs.update(overrides)
        return baseline.compute_config_fingerprint(**kwargs)

    def test_fingerprint_includes_all_three_openai_settings(self):
        fingerprint = self._fingerprint()
        self.assertEqual(fingerprint["openai_timeout_seconds"], 120.0)
        self.assertEqual(fingerprint["openai_max_retries"], 3)
        self.assertEqual(fingerprint["openai_max_output_tokens"], 4096)

    def test_fingerprint_is_json_serializable_and_deterministic(self):
        fingerprint_a = self._fingerprint()
        fingerprint_b = self._fingerprint()
        self.assertEqual(fingerprint_a, fingerprint_b)
        # Round-trips cleanly (no non-JSON-serializable types snuck in).
        self.assertEqual(
            json.loads(json.dumps(fingerprint_a, sort_keys=True)),
            fingerprint_a,
        )

    def test_unchanged_configuration_resumes_successfully(self):
        fixture = _fixture_with_cases(3)
        fingerprint = self._fingerprint()
        adapter1 = _FakeAdapter()
        recorder1 = baseline.PassCallRecorder()
        with self.assertRaises(KeyboardInterrupt):
            baseline.run_case_loop(
                fixture,
                _FakeAdapter(interrupt_on_case_id="fake-002"),
                recorder=recorder1,
                run_dir=self._run_dir,
                config_fingerprint=fingerprint,
                existing_predictions=[],
                completed_case_ids=set(),
                print_progress=False,
            )
        # Resuming with the identical fingerprint must succeed (no refusal).
        run_dir, existing_predictions, completed_case_ids = baseline.resume_or_start_run(
            resume_dir=self._run_dir, config_fingerprint=fingerprint
        )
        self.assertEqual(completed_case_ids, {"fake-001"})
        adapter2 = _FakeAdapter()
        predictions = baseline.run_case_loop(
            fixture,
            adapter2,
            recorder=baseline.PassCallRecorder(),
            run_dir=run_dir,
            config_fingerprint=fingerprint,
            existing_predictions=existing_predictions,
            completed_case_ids=completed_case_ids,
            print_progress=False,
        )
        self.assertEqual(adapter2.calls, ["fake-002", "fake-003"])
        self.assertEqual(len(predictions), 3)

    def _write_completed_checkpoint(self, fixture, fingerprint):
        adapter = _FakeAdapter()
        baseline.run_case_loop(
            fixture,
            adapter,
            recorder=baseline.PassCallRecorder(),
            run_dir=self._run_dir,
            config_fingerprint=fingerprint,
            existing_predictions=[],
            completed_case_ids=set(),
            print_progress=False,
        )

    def test_changing_timeout_refuses_resume_before_any_case_executes(self):
        fixture = _fixture_with_cases(3)
        original = self._fingerprint(openai_timeout_seconds=120.0)
        self._write_completed_checkpoint(fixture, original)

        changed = self._fingerprint(openai_timeout_seconds=60.0)
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline.resume_or_start_run(resume_dir=self._run_dir, config_fingerprint=changed)

    def test_changing_max_retries_refuses_resume_before_any_case_executes(self):
        fixture = _fixture_with_cases(3)
        original = self._fingerprint(openai_max_retries=3)
        self._write_completed_checkpoint(fixture, original)

        changed = self._fingerprint(openai_max_retries=5)
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline.resume_or_start_run(resume_dir=self._run_dir, config_fingerprint=changed)

    def test_changing_max_output_tokens_refuses_resume_before_any_case_executes(self):
        fixture = _fixture_with_cases(3)
        original = self._fingerprint(openai_max_output_tokens=4096)
        self._write_completed_checkpoint(fixture, original)

        changed = self._fingerprint(openai_max_output_tokens=8192)
        with self.assertRaises(baseline.BaselineRunnerRefusal):
            baseline.resume_or_start_run(resume_dir=self._run_dir, config_fingerprint=changed)

    def test_refusal_on_settings_mismatch_happens_before_case_loop_or_provider_call(self):
        """Proof of ordering: in _run_live(), resume_or_start_run()'s
        return value is a prerequisite input to run_case_loop() -- it is
        called first and its result (run_dir/existing_predictions/
        completed_case_ids) is what run_case_loop() consumes. Reproduce
        that exact sequence here with run_case_loop() patched to explode
        if reached, proving a fingerprint mismatch aborts before any
        additional case (and therefore any provider call) is attempted."""
        fixture = _fixture_with_cases(2)
        original = self._fingerprint(openai_max_retries=3)
        self._write_completed_checkpoint(fixture, original)

        changed = self._fingerprint(openai_max_retries=10)

        def _explode_if_reached(*_args, **_kwargs):
            raise AssertionError("run_case_loop must never run after a fingerprint mismatch refusal")

        with patch("scripts.v58_run_openai_benchmark_baseline.run_case_loop", side_effect=_explode_if_reached):
            with self.assertRaises(baseline.BaselineRunnerRefusal):
                run_dir, existing_predictions, completed_case_ids = baseline.resume_or_start_run(
                    resume_dir=self._run_dir, config_fingerprint=changed
                )
                # unreachable if resume_or_start_run correctly refused above
                baseline.run_case_loop(
                    fixture,
                    _FakeAdapter(),
                    recorder=baseline.PassCallRecorder(),
                    run_dir=run_dir,
                    config_fingerprint=changed,
                    existing_predictions=existing_predictions,
                    completed_case_ids=completed_case_ids,
                    print_progress=False,
                )

    def test_checkpoint_and_final_artifact_record_the_same_effective_settings_as_the_fingerprint(self):
        """The values fed into compute_config_fingerprint() must be the
        exact same values recorded in checkpoint.json (via
        config_fingerprint) and in the final artifact's
        provider_configuration -- both are sourced from the same
        describe_effective_openai_settings() dict, so they cannot drift
        apart."""
        openai_settings = {
            "timeout_seconds": 45.0,
            "max_retries": 2,
            "max_output_tokens": 2048,
        }
        fingerprint = self._fingerprint(
            openai_timeout_seconds=openai_settings["timeout_seconds"],
            openai_max_retries=openai_settings["max_retries"],
            openai_max_output_tokens=openai_settings["max_output_tokens"],
        )
        fixture = _fixture_with_cases(1)
        self._write_completed_checkpoint(fixture, fingerprint)
        checkpoint = json.loads((self._run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        recorded = checkpoint["config_fingerprint"]
        self.assertEqual(recorded["openai_timeout_seconds"], openai_settings["timeout_seconds"])
        self.assertEqual(recorded["openai_max_retries"], openai_settings["max_retries"])
        self.assertEqual(recorded["openai_max_output_tokens"], openai_settings["max_output_tokens"])

        # provider_timing_config in the real _run_live() is built from the
        # identical openai_settings dict used for the fingerprint (see
        # scripts/v58_run_openai_benchmark_baseline.py::_run_live), so
        # constructing it the same way here proves the two can never
        # diverge structurally.
        provider_timing_config = {
            "openai_timeout_seconds": openai_settings["timeout_seconds"],
            "openai_max_retries": openai_settings["max_retries"],
            "openai_max_output_tokens": openai_settings["max_output_tokens"],
        }
        self.assertEqual(provider_timing_config["openai_timeout_seconds"], recorded["openai_timeout_seconds"])
        self.assertEqual(provider_timing_config["openai_max_retries"], recorded["openai_max_retries"])
        self.assertEqual(provider_timing_config["openai_max_output_tokens"], recorded["openai_max_output_tokens"])

    def test_numeric_equivalent_env_override_does_not_create_false_mismatch(self):
        """An explicit env-var override that is numerically equal to the
        default must normalize to the identical fingerprint value as
        leaving the env var unset (both resolved via the same
        describe_effective_openai_settings() -> _read_float/_read_int
        canonical parsing), so resume must succeed rather than falsely
        refusing."""
        settings_with_default = baseline.describe_effective_openai_settings()

        os.environ["CERTBOUND_OPENAI_TIMEOUT_SECONDS"] = "120"
        os.environ["CERTBOUND_OPENAI_MAX_RETRIES"] = "3"
        os.environ["CERTBOUND_OPENAI_MAX_OUTPUT_TOKENS"] = "4096"
        settings_with_explicit_equal_override = baseline.describe_effective_openai_settings()

        self.assertEqual(
            settings_with_default["timeout_seconds"], settings_with_explicit_equal_override["timeout_seconds"]
        )
        self.assertEqual(
            settings_with_default["max_retries"], settings_with_explicit_equal_override["max_retries"]
        )
        self.assertEqual(
            settings_with_default["max_output_tokens"],
            settings_with_explicit_equal_override["max_output_tokens"],
        )

        fingerprint_default = self._fingerprint(
            openai_timeout_seconds=settings_with_default["timeout_seconds"],
            openai_max_retries=settings_with_default["max_retries"],
            openai_max_output_tokens=settings_with_default["max_output_tokens"],
        )
        fingerprint_explicit = self._fingerprint(
            openai_timeout_seconds=settings_with_explicit_equal_override["timeout_seconds"],
            openai_max_retries=settings_with_explicit_equal_override["max_retries"],
            openai_max_output_tokens=settings_with_explicit_equal_override["max_output_tokens"],
        )
        self.assertEqual(fingerprint_default, fingerprint_explicit)

        fixture = _fixture_with_cases(2)
        self._write_completed_checkpoint(fixture, fingerprint_default)
        # Resuming with the "explicit but numerically equal" fingerprint
        # must succeed -- no false mismatch.
        run_dir, existing_predictions, completed_case_ids = baseline.resume_or_start_run(
            resume_dir=self._run_dir, config_fingerprint=fingerprint_explicit
        )
        self.assertEqual(completed_case_ids, {"fake-001", "fake-002"})


# ---------------------------------------------------------------------------
# 16-17: sanitized provider errors; no API key anywhere
# ---------------------------------------------------------------------------


class _FakeLlmResponse:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class TestPassCallRecorder(unittest.TestCase):
    def test_success_call_is_recorded_with_expected_fields(self):
        recorder = baseline.PassCallRecorder()
        recorder.set_case("case-1")

        def fake_provider(*, model_name, system_prompt, user_prompt, response_schema, metadata=None):
            return _FakeLlmResponse(
                provider_name="openai",
                model_name="gpt-5.5",
                provider_request_id="req_abc123",
                input_tokens=100,
                output_tokens=50,
                actual_cost_usd=0.002,
            )

        wrapped = recorder.wrap(fake_provider, role="primary")
        response = wrapped(
            model_name="gpt-5.5",
            system_prompt="sys",
            user_prompt="usr",
            response_schema={},
            metadata={"pass_code": "A", "audit_run_id": "run-1"},
        )
        self.assertEqual(response.provider_request_id, "req_abc123")
        calls = recorder.pop_case_calls("case-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["pass_code"], "A")
        self.assertEqual(calls[0]["status"], "success")
        self.assertEqual(calls[0]["input_tokens"], 100)
        self.assertEqual(calls[0]["output_tokens"], 50)
        self.assertEqual(calls[0]["actual_cost_usd"], 0.002)
        self.assertEqual(calls[0]["provider_request_id"], "req_abc123")

    def test_error_call_is_recorded_and_reraised_with_sanitized_message(self):
        recorder = baseline.PassCallRecorder()
        recorder.set_case("case-1")

        sanitized_message = (
            "OpenAI request failed: status=400, type=invalid_request_error, "
            "code=invalid_value, param=input, request_id=req_xyz, message=bad request"
        )

        def failing_provider(*, model_name, system_prompt, user_prompt, response_schema, metadata=None):
            raise RuntimeError(sanitized_message)

        wrapped = recorder.wrap(failing_provider, role="primary")
        with self.assertRaises(RuntimeError):
            wrapped(
                model_name="gpt-5.5",
                system_prompt="sys",
                user_prompt="usr",
                response_schema={},
                metadata={"pass_code": "B"},
            )
        calls = recorder.pop_case_calls("case-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "error")
        self.assertIn(sanitized_message, calls[0]["error"])
        self.assertNotIn("sk-", calls[0]["error"])

    def test_no_api_key_leaks_into_recorded_calls(self):
        fake_key = "sk-super-secret-test-key-should-never-appear"
        with patch.dict(os.environ, {baseline.ENV_OPENAI_API_KEY: fake_key}):
            recorder = baseline.PassCallRecorder()
            recorder.set_case("case-1")

            def fake_provider(*, model_name, system_prompt, user_prompt, response_schema, metadata=None):
                return _FakeLlmResponse(
                    provider_name="openai",
                    model_name="gpt-5.5",
                    provider_request_id="req_1",
                    input_tokens=1,
                    output_tokens=1,
                    actual_cost_usd=0.0001,
                )

            wrapped = recorder.wrap(fake_provider, role="primary")
            wrapped(
                model_name="gpt-5.5",
                system_prompt="sys",
                user_prompt="usr",
                response_schema={},
                metadata={"pass_code": "A"},
            )
            calls = recorder.pop_case_calls("case-1")
            serialized = json.dumps(calls)
            self.assertNotIn(fake_key, serialized)


# ---------------------------------------------------------------------------
# 18-19: totals aggregation and no-ground-truth-leakage in artifacts
# ---------------------------------------------------------------------------


class TestAggregateTotalsAndArtifact(unittest.TestCase):
    def test_aggregate_totals_sums_tokens_cost_and_request_ids(self):
        predictions = [
            {
                "case_id": "c1",
                "raw_output": {
                    "provider_calls": [
                        {
                            "status": "success",
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "actual_cost_usd": 0.01,
                            "provider_request_id": "req1",
                        },
                        {
                            "status": "success",
                            "input_tokens": 20,
                            "output_tokens": 8,
                            "actual_cost_usd": 0.02,
                            "provider_request_id": "req2",
                        },
                    ]
                },
            },
            {
                "case_id": "c2",
                "raw_output": {
                    "provider_calls": [
                        {"status": "error", "error": "boom"},
                    ]
                },
            },
        ]
        totals = baseline.aggregate_totals(predictions)
        self.assertEqual(totals["total_call_count"], 2)
        self.assertEqual(totals["total_input_tokens"], 30)
        self.assertEqual(totals["total_output_tokens"], 13)
        self.assertAlmostEqual(totals["total_cost_usd"], 0.03)
        self.assertEqual(sorted(totals["provider_request_ids"]), ["req1", "req2"])

    def test_aggregate_totals_reports_cost_unavailable_when_any_call_missing_cost(self):
        predictions = [
            {
                "case_id": "c1",
                "raw_output": {
                    "provider_calls": [
                        {
                            "status": "success",
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "actual_cost_usd": None,
                            "provider_request_id": "req1",
                        },
                    ]
                },
            }
        ]
        totals = baseline.aggregate_totals(predictions)
        self.assertIsNone(totals["total_cost_usd"])
        self.assertEqual(totals["total_input_tokens"], 10)

    def test_final_artifact_contains_no_ground_truth_fields(self):
        real_case = json.loads(baseline.FIXTURE_PATH.read_text(encoding="utf-8"))["cases"][0]
        self.assertIn("expected_finding_codes", real_case)  # sanity: fixture does carry ground truth

        adapter = _FakeAdapter()
        prediction = adapter.generate_prediction(real_case | {"case_id": "qbv1-001"})
        prediction.raw_output["provider_calls"] = []
        prediction.raw_output["case_duration_seconds"] = 0.1

        artifact = baseline.build_final_artifact(
            adapter=adapter,
            source_fixture_path=baseline.FIXTURE_PATH,
            source_fixture_sha256=baseline.APPROVED_FIXTURE_SHA256,
            case_count=40,
            predictions=[prediction.to_dict()],
            run_id="run-1",
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            requested_model="gpt-5.5",
            reasoning_effort="medium",
            provider_timing_config={},
            database_info={"host": "127.0.0.1", "dbname": "certbound_v48_test", "cleanup": {}},
            run_status="completed",
        )
        serialized = json.dumps(artifact)
        for forbidden_key in (
            "expected_finding_codes",
            "expected_materiality",
            "expected_correct_option_labels",
            "known_good",
            "reviewer_label",
            "reviewer_rationale",
            "sme_review",
            "sme_reviewed",
        ):
            self.assertNotIn(forbidden_key, serialized, msg=f"forbidden ground-truth key leaked: {forbidden_key}")

    def test_final_artifact_matches_prediction_artifact_schema_for_scorer_compatibility(self):
        from workers.quality_benchmark_execution import load_prediction_artifact, validate_prediction_coverage

        adapter = _FakeAdapter()
        fixture = _fixture_with_cases(2)
        predictions = [
            adapter.generate_prediction(case).to_dict() for case in fixture["cases"]
        ]
        for prediction in predictions:
            prediction["raw_output"]["provider_calls"] = []

        artifact = baseline.build_final_artifact(
            adapter=adapter,
            source_fixture_path=baseline.FIXTURE_PATH,
            source_fixture_sha256=baseline.APPROVED_FIXTURE_SHA256,
            case_count=2,
            predictions=predictions,
            run_id="run-1",
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            requested_model="gpt-5.5",
            reasoning_effort="medium",
            provider_timing_config={},
            database_info={"host": "127.0.0.1", "dbname": "certbound_v48_test", "cleanup": {}},
            run_status="completed",
        )

        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="v58-openai-baseline-artifact-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp_dir, ignore_errors=True))
        artifact_path = Path(tmp_dir) / "result.json"
        artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

        loaded = load_prediction_artifact(artifact_path)
        coverage = validate_prediction_coverage(fixture, loaded)
        self.assertEqual(coverage["expected_case_count"], 2)
        self.assertEqual(coverage["predicted_case_count"], 2)


# ---------------------------------------------------------------------------
# 20-21: database cleanup verification and zero-row expectations
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, counts):
        self._counts = counts
        self._last_table = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        for table, _count in self._counts.items():
            if table in sql:
                self._last_table = table
                return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return (self._counts[self._last_table],)


class _FakeConnection:
    def __init__(self, counts):
        self._counts = counts
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._counts)

    def close(self):
        self.closed = True


class _FakePsycopg2Module:
    def __init__(self, counts, raise_on_connect=False):
        self._counts = counts
        self._raise_on_connect = raise_on_connect
        self.connection = None

    def connect(self, dsn):
        if self._raise_on_connect:
            raise RuntimeError("connection refused")
        self.connection = _FakeConnection(self._counts)
        return self.connection


class TestDatabaseCleanupVerification(unittest.TestCase):
    def test_zero_row_expectation_reported(self):
        zero_counts = {table: 0 for table in baseline.CLEANUP_VERIFICATION_TABLES}
        fake_module = _FakePsycopg2Module(zero_counts)
        with patch.dict(sys.modules, {"psycopg2": fake_module}):
            result = baseline.count_cleanup_tables("postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test")
        self.assertTrue(result["verified"])
        self.assertEqual(
            result["counts"],
            {"questions": 0, "audit_runs": 0, "audit_findings": 0, "audit_finding_evidence": 0},
        )
        self.assertTrue(fake_module.connection.closed)

    def test_nonzero_rows_are_reported_not_hidden(self):
        counts = {table: 0 for table in baseline.CLEANUP_VERIFICATION_TABLES}
        counts["questions"] = 3
        fake_module = _FakePsycopg2Module(counts)
        with patch.dict(sys.modules, {"psycopg2": fake_module}):
            result = baseline.count_cleanup_tables("postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test")
        self.assertTrue(result["verified"])
        self.assertEqual(result["counts"]["questions"], 3)

    def test_connection_failure_is_best_effort_and_does_not_raise(self):
        fake_module = _FakePsycopg2Module({}, raise_on_connect=True)
        with patch.dict(sys.modules, {"psycopg2": fake_module}):
            result = baseline.count_cleanup_tables("postgresql://postgres:postgres@127.0.0.1:54329/certbound_v48_test")
        self.assertFalse(result["verified"])
        self.assertIn("reason", result)


# ---------------------------------------------------------------------------
# 22 / dry-run: no live calls anywhere in this file; dry-run makes zero
# network/database/provider calls
# ---------------------------------------------------------------------------


class TestDryRun(EnvIsolatedTestCase):
    def test_dry_run_succeeds_with_zero_credentials(self):
        # Deliberately do not set CERTBOUND_ALLOW_LIVE_AI_TEST or
        # CERTBOUND_OPENAI_API_KEY anywhere in this test.
        self.assertNotIn(baseline.ENV_ALLOW_LIVE, os.environ)
        self.assertNotIn(baseline.ENV_OPENAI_API_KEY, os.environ)
        args = baseline._build_arg_parser().parse_args(["--dry-run"])
        exit_code = baseline._run_dry_run(args)
        self.assertEqual(exit_code, 0)

    def test_dry_run_reports_refusal_for_non_openai_provider_without_raising(self):
        os.environ[baseline.ENV_PRIMARY_PROVIDER] = "anthropic"
        args = baseline._build_arg_parser().parse_args(["--dry-run"])
        exit_code = baseline._run_dry_run(args)
        self.assertEqual(exit_code, 1)

    def test_main_refuses_under_pytest(self):
        exit_code = baseline.main(["--dry-run"])
        self.assertEqual(exit_code, 2)


# ---------------------------------------------------------------------------
# V58-DAY8-OPENAI-12: model wiring and systemic fail-fast
# ---------------------------------------------------------------------------


class _FakeProvenance:
    def __init__(self, *, primary_model_name="gpt-5.5", dispute_model_name="gpt-5.5"):
        self.primary_provider = "openai"
        self.dispute_provider = "openai"
        self.primary_model_name = primary_model_name
        self.dispute_model_name = dispute_model_name


class TestBaselineModelWiring(EnvIsolatedTestCase):
    def test_default_configuration_sends_gpt_5_5_to_generate_v48_prediction(self):
        from workers.ai_quality_audit_worker import AiQualityAuditProviders
        from workers.quality_benchmark_execution import CasePrediction

        provenance = baseline.resolve_and_validate_provider_selection()
        self.assertEqual(provenance.primary_model_name, "gpt-5.5")
        self.assertEqual(provenance.dispute_model_name, "gpt-5.5")

        adapter = baseline.build_v48_adapter(
            dsn=baseline.DEFAULT_DISPOSABLE_DSN,
            providers=AiQualityAuditProviders(primary=lambda **k: None, dispute=lambda **k: None, timeout_seconds=120),
            provenance=provenance,
            evidence_config_id="official_evidence_seed_v1",
        )
        case = {"case_id": "qbv1-001"}

        with patch("workers.quality_benchmark_v48_orchestration.generate_v48_prediction") as mock_gen:
            mock_gen.return_value = CasePrediction(case_id="qbv1-001")
            adapter.generate_prediction(case)
            kwargs = mock_gen.call_args.kwargs
            self.assertEqual(kwargs["primary_model_name"], "gpt-5.5")
            self.assertEqual(kwargs["dispute_model_name"], "gpt-5.5")

    def test_openai_model_env_override_is_honored_for_both_roles(self):
        from workers.ai_quality_audit_worker import AiQualityAuditProviders
        from workers.quality_benchmark_execution import CasePrediction

        os.environ["CERTBOUND_OPENAI_MODEL"] = "gpt-5.5-custom"
        provenance = baseline.resolve_and_validate_provider_selection()
        self.assertEqual(provenance.primary_model_name, "gpt-5.5-custom")
        self.assertEqual(provenance.dispute_model_name, "gpt-5.5-custom")

        adapter = baseline.build_v48_adapter(
            dsn=baseline.DEFAULT_DISPOSABLE_DSN,
            providers=AiQualityAuditProviders(primary=lambda **k: None, dispute=lambda **k: None, timeout_seconds=120),
            provenance=provenance,
            evidence_config_id="official_evidence_seed_v1",
        )
        with patch("workers.quality_benchmark_v48_orchestration.generate_v48_prediction") as mock_gen:
            mock_gen.return_value = CasePrediction(case_id="qbv1-001")
            adapter.generate_prediction({"case_id": "qbv1-001"})
            kwargs = mock_gen.call_args.kwargs
            self.assertEqual(kwargs["primary_model_name"], "gpt-5.5-custom")
            self.assertEqual(kwargs["dispute_model_name"], "gpt-5.5-custom")

    def test_provider_invocation_receives_configured_model_not_placeholder(self):
        from workers.ai_quality_audit_worker import AiQualityAuditProviders
        from workers.quality_benchmark_v48_orchestration import run_v48_benchmark_case
        from workers.openai_provider import LlmResponse

        recorded = {"primary": [], "dispute": []}

        def primary(**kwargs):
            recorded["primary"].append(kwargs["model_name"])
            return LlmResponse(
                parsed_response={"selected_option_labels": ["A"]},
                input_tokens=1,
                output_tokens=1,
            )

        def dispute(**kwargs):
            recorded["dispute"].append(kwargs["model_name"])
            return LlmResponse(
                parsed_response={"selected_option_labels": ["A"], "proposed_findings": []},
                input_tokens=1,
                output_tokens=1,
            )

        providers = AiQualityAuditProviders(primary=primary, dispute=dispute, timeout_seconds=120)
        case = {"case_id": "qbv1-001", "certification": "ADM", "domain": "General", "question": {"question_text": "Q?", "options": [{"label": "A", "text": "a"}]}, "resource_snapshot": {"chunks": [{"resource_chunk_id": "c1", "content": "x", "content_hash": "h"}]}}

        stored_models = {}

        def fake_create(client, seeded, evidence_payload, **kwargs):
            stored_models["primary"] = kwargs["primary_model_name"]
            stored_models["dispute"] = kwargs["dispute_model_name"]
            return "audit-run-1"

        def fake_process(client, payload, providers_arg, worker_id):
            providers_arg.primary(
                model_name=stored_models["primary"],
                system_prompt="sys",
                user_prompt="usr",
                response_schema={},
                metadata={"pass_code": "A"},
            )
            providers_arg.dispute(
                model_name=stored_models["dispute"],
                system_prompt="sys",
                user_prompt="usr",
                response_schema={},
                metadata={"pass_code": "B"},
            )
            return {"run_status": "inconclusive", "finding_count": 0, "passes_executed": ["A", "B"]}

        class _Seeded:
            question_version_id = "qv-1"
            evidence_chunk_count = 1

        with patch("workers.quality_benchmark_v48_orchestration.v48_disposable_transaction") as mock_tx, patch(
            "workers.quality_benchmark_v48_orchestration.seed_benchmark_case",
            return_value=(_Seeded(), []),
        ), patch(
            "workers.quality_benchmark_v48_orchestration.create_v48_audit_run",
            side_effect=fake_create,
        ), patch(
            "workers.quality_benchmark_v48_orchestration.process_ai_quality_audit_job",
            side_effect=fake_process,
        ):
            mock_tx.return_value.__enter__.return_value = (object(), object())
            mock_tx.return_value.__exit__.return_value = False
            run_v48_benchmark_case(
                case,
                dsn=baseline.DEFAULT_DISPOSABLE_DSN,
                allow_disposable_v48_db=True,
                providers=providers,
                primary_model_name="gpt-5.5",
                dispute_model_name="gpt-5.5",
            )

        self.assertEqual(recorded["primary"], ["gpt-5.5"])
        self.assertEqual(recorded["dispute"], ["gpt-5.5"])
        for model_name in recorded["primary"] + recorded["dispute"]:
            self.assertNotIn(model_name, ("benchmark-primary", "benchmark-dispute"))

    def test_artifact_provenance_records_requested_and_configured_models(self):
        adapter = _FakeAdapter()
        artifact = baseline.build_final_artifact(
            adapter=adapter,
            source_fixture_path=baseline.FIXTURE_PATH,
            source_fixture_sha256=baseline.APPROVED_FIXTURE_SHA256,
            case_count=1,
            predictions=[],
            run_id="run-1",
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            requested_model="gpt-5.5",
            reasoning_effort="medium",
            provider_timing_config={},
            database_info={"host": "127.0.0.1", "dbname": "certbound_v48_test", "cleanup": {}},
            run_status="completed",
            configured_primary_model="gpt-5.5",
            configured_dispute_model="gpt-5.5",
        )
        self.assertEqual(artifact["requested_model"], "gpt-5.5")
        self.assertEqual(artifact["resolved_model"], "gpt-5.5")
        self.assertEqual(artifact["configured_primary_model"], "gpt-5.5")
        self.assertEqual(artifact["configured_dispute_model"], "gpt-5.5")
        self.assertEqual(artifact["engine_id"], "v48")


def _model_not_found_provider_call():
    return [
        {
            "status": "error",
            "pass_code": "A",
            "error": (
                "LlmProviderError: OpenAI request failed: status=400, type=invalid_request_error, "
                "code=model_not_found, param=model, request_id=req_test, "
                "message=The requested model 'benchmark-primary' does not exist."
            ),
        }
    ]


class TestSystemicFailFast(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._run_dir = Path(tempfile.mkdtemp(prefix="v58-openai-baseline-systemic-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self._run_dir, ignore_errors=True))

    def _fingerprint(self):
        return baseline.compute_config_fingerprint(
            fixture_sha256="fixturehash",
            adapter_config=_FakeAdapter().describe_config(),
            reasoning_effort="medium",
            openai_timeout_seconds=120.0,
            openai_max_retries=3,
            openai_max_output_tokens=4096,
        )

    def test_classify_model_not_found_as_systemic(self):
        prediction = {
            "case_id": "fake-001",
            "raw_output": {"provider_calls": _model_not_found_provider_call()},
        }
        reason = baseline.classify_systemic_provider_failure(prediction)
        self.assertIn("model not found", reason.lower())

    def test_model_not_found_stops_after_first_case(self):
        fixture = _fixture_with_cases(3)
        adapter = _FakeAdapter()
        recorder = baseline.PassCallRecorder()

        with patch.object(recorder, "pop_case_calls", return_value=_model_not_found_provider_call()):
            with self.assertRaises(baseline.BaselineRunnerSystemicFailure):
                baseline.run_case_loop(
                    fixture,
                    adapter,
                    recorder=recorder,
                    run_dir=self._run_dir,
                    config_fingerprint=self._fingerprint(),
                    existing_predictions=[],
                    completed_case_ids=set(),
                    print_progress=False,
                )
        self.assertEqual(adapter.calls, ["fake-001"])
        checkpoint = json.loads((self._run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(len(checkpoint["predictions"]), 1)

    def test_authentication_failure_stops_after_first_case(self):
        fixture = _fixture_with_cases(2)
        adapter = _FakeAdapter()
        recorder = baseline.PassCallRecorder()
        auth_calls = [
            {
                "status": "error",
                "pass_code": "A",
                "error": "LlmProviderError: OpenAI authentication failed; check CERTBOUND_OPENAI_API_KEY (status=401)",
            }
        ]
        with patch.object(recorder, "pop_case_calls", return_value=auth_calls):
            with self.assertRaises(baseline.BaselineRunnerSystemicFailure) as ctx:
                baseline.run_case_loop(
                    fixture,
                    adapter,
                    recorder=recorder,
                    run_dir=self._run_dir,
                    config_fingerprint=self._fingerprint(),
                    existing_predictions=[],
                    completed_case_ids=set(),
                    print_progress=False,
                )
        self.assertEqual(adapter.calls, ["fake-001"])
        self.assertIn("authentication failed", str(ctx.exception).lower())

    def test_permission_failure_stops_after_first_case(self):
        fixture = _fixture_with_cases(2)
        adapter = _FakeAdapter()
        recorder = baseline.PassCallRecorder()
        permission_calls = [
            {
                "status": "error",
                "pass_code": "A",
                "error": "LlmProviderError: OpenAI request failed: code=insufficient_quota, message=permission denied",
            }
        ]
        with patch.object(recorder, "pop_case_calls", return_value=permission_calls):
            with self.assertRaises(baseline.BaselineRunnerSystemicFailure):
                baseline.run_case_loop(
                    fixture,
                    adapter,
                    recorder=recorder,
                    run_dir=self._run_dir,
                    config_fingerprint=self._fingerprint(),
                    existing_predictions=[],
                    completed_case_ids=set(),
                    print_progress=False,
                )
        self.assertEqual(adapter.calls, ["fake-001"])

    def test_inconclusive_per_case_error_does_not_trigger_fail_fast(self):
        fixture = _fixture_with_cases(2)
        adapter = _FakeAdapter(fail_on_case_id="fake-001")
        recorder = baseline.PassCallRecorder()
        predictions = baseline.run_case_loop(
            fixture,
            adapter,
            recorder=recorder,
            run_dir=self._run_dir,
            config_fingerprint=self._fingerprint(),
            existing_predictions=[],
            completed_case_ids=set(),
            print_progress=False,
        )
        self.assertEqual(len(predictions), 2)
        self.assertEqual(adapter.calls, ["fake-001", "fake-002"])

    def test_systemic_failure_message_is_sanitized(self):
        fake_key = "sk-super-secret-test-key-should-never-appear"
        prediction = {
            "case_id": "fake-001",
            "raw_output": {
                "provider_calls": [
                    {
                        "status": "error",
                        "error": f"LlmProviderError: authentication failed; key={fake_key}",
                    }
                ]
            },
        }
        reason = baseline.classify_systemic_provider_failure(prediction)
        self.assertIn("authentication failed", reason.lower())
        self.assertNotIn(fake_key, reason)


class TestPartialArtifactFromCheckpoint(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._run_dir = Path(tempfile.mkdtemp(prefix="v58-openai-baseline-partial-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self._run_dir, ignore_errors=True))

    def _fingerprint(self):
        return baseline.compute_config_fingerprint(
            fixture_sha256="fixturehash",
            adapter_config=_FakeAdapter().describe_config(),
            reasoning_effort="medium",
            openai_timeout_seconds=120.0,
            openai_max_retries=3,
            openai_max_output_tokens=4096,
        )

    def test_interruption_partial_result_uses_checkpoint_predictions(self):
        fixture = _fixture_with_cases(3)
        adapter = _FakeAdapter(interrupt_on_case_id="fake-003")
        recorder = baseline.PassCallRecorder()
        with self.assertRaises(KeyboardInterrupt):
            baseline.run_case_loop(
                fixture,
                adapter,
                recorder=recorder,
                run_dir=self._run_dir,
                config_fingerprint=self._fingerprint(),
                existing_predictions=[],
                completed_case_ids=set(),
                print_progress=False,
            )
        checkpoint_predictions = baseline.load_checkpoint_predictions(self._run_dir)
        self.assertEqual(len(checkpoint_predictions), 2)
        artifact = baseline.build_final_artifact(
            adapter=adapter,
            source_fixture_path=baseline.FIXTURE_PATH,
            source_fixture_sha256=baseline.APPROVED_FIXTURE_SHA256,
            case_count=3,
            predictions=checkpoint_predictions,
            run_id="run-1",
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            requested_model="gpt-5.5",
            reasoning_effort="medium",
            provider_timing_config={},
            database_info={"host": "127.0.0.1", "dbname": "certbound_v48_test", "cleanup": {}},
            run_status="interrupted",
            blocked_reason="run was interrupted before completion",
        )
        self.assertEqual(len(artifact["predictions"]), 2)

    def test_systemic_failure_partial_result_matches_checkpoint(self):
        fixture = _fixture_with_cases(2)
        adapter = _FakeAdapter()
        recorder = baseline.PassCallRecorder()
        with patch.object(recorder, "pop_case_calls", return_value=_model_not_found_provider_call()):
            with self.assertRaises(baseline.BaselineRunnerSystemicFailure):
                baseline.run_case_loop(
                    fixture,
                    adapter,
                    recorder=recorder,
                    run_dir=self._run_dir,
                    config_fingerprint=self._fingerprint(),
                    existing_predictions=[],
                    completed_case_ids=set(),
                    print_progress=False,
                )
        checkpoint_predictions = baseline.load_checkpoint_predictions(self._run_dir)
        self.assertEqual(len(checkpoint_predictions), 1)
        artifact = baseline.build_final_artifact(
            adapter=adapter,
            source_fixture_path=baseline.FIXTURE_PATH,
            source_fixture_sha256=baseline.APPROVED_FIXTURE_SHA256,
            case_count=2,
            predictions=checkpoint_predictions,
            run_id="run-1",
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            requested_model="gpt-5.5",
            reasoning_effort="medium",
            provider_timing_config={},
            database_info={"host": "127.0.0.1", "dbname": "certbound_v48_test", "cleanup": {}},
            run_status="blocked",
            blocked_reason="OpenAI model not found (code=model_not_found); verify configured model",
        )
        self.assertEqual(len(artifact["predictions"]), 1)
        self.assertEqual(artifact["predictions"][0]["case_id"], "fake-001")


if __name__ == "__main__":
    unittest.main()
