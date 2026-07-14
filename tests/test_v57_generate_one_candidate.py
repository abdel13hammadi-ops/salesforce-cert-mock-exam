"""
Focused tests for V57-GENERATION-04 (scripts/v57_generate_one_candidate.py).

Hermetic: no Supabase connection, no network, no real LLM provider calls.
Reuses the FakeSupabase/FakeLlmProvider infrastructure from
tests/test_question_candidate_generation.py so the runner is tested against
the same fakes the underlying service is already validated against.

The pytest-refusal and CERTBOUND_ALLOW_LIVE_AI_TEST guards live only inside
main(); this file exercises the actual delegation functions
(run_generate_one_candidate / run_retry_audits) directly with fakes, which
is the supported way to prove "the runner delegates to the real service"
without needing to spawn a subprocess or fight the safety guards.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.v57_generate_one_candidate import (
    RunnerUsageError,
    build_arg_parser,
    build_generation_request_from_args,
    format_audit_outcomes_report,
    format_generation_report,
    main,
    run_generate_one_candidate,
    run_retry_audits,
)
from tests.test_question_candidate_generation import (
    FakeLlmProvider,
    FakeQueryResult,
    FakeSupabase,
    _llm_response,
    _seed_domain,
    _valid_raw_payload,
)
from workers.certification_registry import BA_EXAM_NAME, PAB_EXAM_NAME, SCC_EXAM_NAME
from workers.official_evidence_seed import (
    BA_EVIDENCE_CONFIG_ID,
    PAB_EVIDENCE_CONFIG_ID,
    SCC_EVIDENCE_CONFIG_ID,
)
from workers.question_candidate_generation import AuditInitiationError, GenerationRequest


def _generate_args(**overrides):
    argv = [
        "generate",
        "--certification", "Salesforce Administrator",
        "--domain", "Data and Analytics Management",
        "--model", "claude-3-5-sonnet-20241022",
        "--prompt-template-id", "certbound-question-gen",
        "--prompt-version", "v1.0.0",
        "--created-by", "generation-service@certbound.internal",
        "--source-evidence", '{"resource_reference": "Salesforce Help: Standard Objects"}',
    ]
    for key, value in overrides.items():
        argv.extend([f"--{key.replace('_', '-')}", str(value)])
    return build_arg_parser().parse_args(argv)


def _retry_args(candidate_id: str, **overrides):
    argv = [
        "retry-audits",
        "--candidate-id", candidate_id,
        "--created-by", "ops@certbound.internal",
    ]
    for key, value in overrides.items():
        argv.extend([f"--{key.replace('_', '-')}", str(value)])
    return build_arg_parser().parse_args(argv)


class TestArgParsingAndRequestMapping(unittest.TestCase):
    def test_generate_args_map_to_generation_request(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        self.assertIsInstance(request, GenerationRequest)
        self.assertEqual(request.certification_exam_name, "Salesforce Administrator")
        self.assertEqual(request.domain, "Data and Analytics Management")
        self.assertEqual(request.model_name, "claude-3-5-sonnet-20241022")
        self.assertEqual(
            request.source_evidence,
            {"resource_reference": "Salesforce Help: Standard Objects"},
        )
        self.assertEqual(request.question_type, "single")
        self.assertEqual(request.select_count, 1)

    def test_invalid_source_evidence_json_rejected_before_any_io(self):
        args = _generate_args()
        args.source_evidence = "not json"
        with self.assertRaises(RunnerUsageError):
            build_generation_request_from_args(args)

    def test_optional_fields_are_bounded_by_choices(self):
        parser = build_arg_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "generate",
                    "--certification", "x",
                    "--domain", "y",
                    "--model", "m",
                    "--prompt-template-id", "p",
                    "--prompt-version", "v1",
                    "--created-by", "c",
                    "--source-evidence", "{}",
                    "--difficulty", "impossible",
                ]
            )


class TestRunnerDelegatesToRealService(unittest.TestCase):
    """Proves the runner is a thin wrapper, not a duplicate implementation."""

    def test_generate_delegates_and_persists_exactly_one_candidate(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = run_generate_one_candidate(fake, provider, args)

        self.assertEqual(len(provider.calls), 1)  # the real service called the provider
        self.assertEqual(len(fake.insert_calls), 2)  # question_candidates + candidate_events
        self.assertEqual(
            len([c for c in fake.insert_calls if c["table"] == "question_candidates"]), 1
        )
        self.assertFalse(result.deduplicated)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        report = format_generation_report(result)
        self.assertIn(result.candidate_id, report)

    def test_no_initiate_audits_flag_is_honored_by_real_service(self):
        args = _generate_args()
        args.no_initiate_audits = True
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = run_generate_one_candidate(fake, provider, args)

        self.assertEqual(result.audit_outcomes, [])
        self.assertEqual(fake.rpc_calls, [])

    def test_identical_generation_retry_returns_existing_candidate_and_no_duplicate_audits(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(
            responses=[_llm_response(_valid_raw_payload()), _llm_response(_valid_raw_payload())]
        )

        first = run_generate_one_candidate(fake, provider, args)
        rpc_calls_after_first = len(fake.rpc_calls)
        second = run_generate_one_candidate(fake, provider, args)

        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.candidate_id, second.candidate_id)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        # No additional enqueue_background_job_v1 calls happened on the
        # deduplicated retry — audits are not silently duplicated.
        self.assertEqual(len(fake.rpc_calls), rpc_calls_after_first)
        self.assertEqual(second.audit_outcomes, [])

    def test_immutable_snapshot_and_content_hash_preserved_across_dedup_retry(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(
            responses=[_llm_response(_valid_raw_payload()), _llm_response(_valid_raw_payload())]
        )

        first = run_generate_one_candidate(fake, provider, args)
        second = run_generate_one_candidate(fake, provider, args)

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.question_snapshot, second.question_snapshot)

    def test_no_live_question_bank_tables_touched(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        run_generate_one_candidate(fake, provider, args)

        for forbidden in ("questions", "answer_options", "question_versions", "question_option_versions"):
            self.assertNotIn(forbidden, fake.tables)

    def test_platform_app_builder_generate_delegates_through_existing_service(self):
        args = _generate_args(
            certification=PAB_EXAM_NAME,
            domain="Data Modeling and Management",
            source_evidence='{"resource_reference": "Salesforce Help: Relationships"}',
        )
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = run_generate_one_candidate(fake, provider, args)

        row = fake.tables["question_candidates"][0]
        self.assertEqual(row["certification_exam_name"], PAB_EXAM_NAME)
        self.assertEqual(row["metadata"]["domain"], "Data Modeling and Management")
        self.assertEqual(
            row["candidate_payload"]["provenance"]["source_evidence"]["evidence_config_id"],
            PAB_EVIDENCE_CONFIG_ID,
        )
        self.assertFalse(result.deduplicated)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)

    def test_business_analyst_generate_delegates_through_existing_service(self):
        args = _generate_args(
            certification=BA_EXAM_NAME,
            domain="Requirements",
            source_evidence='{"resource_reference": "Salesforce Help: Requirements"}',
        )
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = run_generate_one_candidate(fake, provider, args)

        row = fake.tables["question_candidates"][0]
        self.assertEqual(row["certification_exam_name"], BA_EXAM_NAME)
        self.assertEqual(row["metadata"]["domain"], "Requirements")
        self.assertEqual(
            row["candidate_payload"]["provenance"]["source_evidence"]["evidence_config_id"],
            BA_EVIDENCE_CONFIG_ID,
        )
        self.assertFalse(result.deduplicated)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)

    def test_sales_cloud_consultant_generate_delegates_through_existing_service(self):
        args = _generate_args(
            certification=SCC_EXAM_NAME,
            domain="Sales Lifecycle",
            source_evidence='{"resource_reference": "Salesforce Help: Opportunities"}',
        )
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = run_generate_one_candidate(fake, provider, args)

        row = fake.tables["question_candidates"][0]
        self.assertEqual(row["certification_exam_name"], SCC_EXAM_NAME)
        self.assertEqual(row["metadata"]["domain"], "Sales Lifecycle")
        self.assertEqual(
            row["candidate_payload"]["provenance"]["source_evidence"]["evidence_config_id"],
            SCC_EVIDENCE_CONFIG_ID,
        )
        self.assertFalse(result.deduplicated)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)


class TestAuditRetryWithoutRegeneratingCandidate(unittest.TestCase):
    def test_failed_audit_initiation_can_be_retried_without_new_candidate(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))
        fake.set_rpc_sequence(
            "enqueue_background_job_v1",
            [
                FakeQueryResult(data=[{"job_id": "det-job-1", "job_status": "pending"}]),
                FakeQueryResult(data=None, error="llm provider not configured"),
            ],
        )

        with self.assertRaises(AuditInitiationError) as ctx:
            run_generate_one_candidate(fake, provider, args)

        candidate_id = ctx.exception.candidate_id
        self.assertEqual(len(fake.tables["question_candidates"]), 1)

        fake.set_rpc_response(
            "enqueue_background_job_v1", [{"job_id": "llm-job-retry", "job_status": "pending"}]
        )
        retry_args = _retry_args(candidate_id, job_types="llm_audit")
        outcomes = run_retry_audits(fake, retry_args)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].job_type, "llm_audit")
        self.assertTrue(outcomes[0].enqueued)
        # Still exactly one candidate row: retry never regenerates/duplicates it.
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        report = format_audit_outcomes_report(outcomes)
        self.assertIn("llm_audit", report)

    def test_retry_uses_immutable_snapshot_and_content_hash_from_persisted_row(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        generated = run_generate_one_candidate(fake, provider, args)

        retry_args = _retry_args(generated.candidate_id, job_types="llm_audit")
        run_retry_audits(fake, retry_args)

        llm_calls = [
            c for c in fake.calls_for("enqueue_background_job_v1")
            if c["params"]["p_job_type"] == "llm_audit"
        ]
        self.assertEqual(len(llm_calls), 2)  # one from generation, one from retry
        retry_call = llm_calls[-1]
        self.assertEqual(retry_call["params"]["p_payload"]["target_candidate_id"], generated.candidate_id)
        self.assertEqual(retry_call["params"]["p_payload"]["question"], generated.question_snapshot)
        self.assertEqual(
            retry_call["params"]["p_metadata"]["candidate_content_hash"], generated.content_hash
        )

    def test_retry_only_enqueues_requested_job_types(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))
        generated = run_generate_one_candidate(fake, provider, args)
        rpc_calls_after_generation = len(fake.rpc_calls)

        retry_args = _retry_args(generated.candidate_id, job_types="deterministic_audit")
        outcomes = run_retry_audits(fake, retry_args)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].job_type, "deterministic_audit")
        self.assertEqual(len(fake.rpc_calls), rpc_calls_after_generation + 1)

    def test_retry_nonexistent_candidate_raises_without_side_effects(self):
        fake = FakeSupabase()
        retry_args = _retry_args("does-not-exist")
        with self.assertRaises(Exception):
            run_retry_audits(fake, retry_args)
        self.assertEqual(fake.rpc_calls, [])


class TestMainSafetyGuards(unittest.TestCase):
    def test_main_refuses_under_pytest_regardless_of_live_flag(self):
        old = os.environ.get("CERTBOUND_ALLOW_LIVE_AI_TEST")
        os.environ["CERTBOUND_ALLOW_LIVE_AI_TEST"] = "1"
        try:
            exit_code = main(["generate", "--certification", "x", "--domain", "y", "--model", "m",
                               "--prompt-template-id", "p", "--prompt-version", "v1",
                               "--created-by", "c", "--source-evidence", "{}"])
        finally:
            if old is None:
                os.environ.pop("CERTBOUND_ALLOW_LIVE_AI_TEST", None)
            else:
                os.environ["CERTBOUND_ALLOW_LIVE_AI_TEST"] = old
        self.assertEqual(exit_code, 2)

    def test_main_refuses_without_live_flag_even_outside_pytest(self):
        # Simulate "not running under pytest" to isolate the live-flag guard
        # from the pytest guard, without ever reaching a real client/network
        # call (assert_supabase_configured / build_supabase_client are never
        # invoked when this guard trips first).
        old = os.environ.pop("CERTBOUND_ALLOW_LIVE_AI_TEST", None)
        try:
            with mock.patch(
                "scripts.v57_generate_one_candidate.running_under_pytest",
                return_value=False,
            ):
                exit_code = main(
                    ["retry-audits", "--candidate-id", "x", "--created-by", "c"]
                )
        finally:
            if old is not None:
                os.environ["CERTBOUND_ALLOW_LIVE_AI_TEST"] = old
        self.assertEqual(exit_code, 2)


class TestNoSecretsInOutput(unittest.TestCase):
    def test_generation_report_never_contains_env_secret_values(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "super-secret-service-role-key-value"
        try:
            result = run_generate_one_candidate(fake, provider, args)
            report = format_generation_report(result)
        finally:
            os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
        self.assertNotIn("super-secret-service-role-key-value", report)

    def test_retry_report_never_contains_env_secret_values(self):
        args = _generate_args()
        request = build_generation_request_from_args(args)
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))
        generated = run_generate_one_candidate(fake, provider, args)

        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "another-super-secret-value"
        try:
            retry_args = _retry_args(generated.candidate_id, job_types="llm_audit")
            outcomes = run_retry_audits(fake, retry_args)
            report = format_audit_outcomes_report(outcomes)
        finally:
            os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
        self.assertNotIn("another-super-secret-value", report)


if __name__ == "__main__":
    unittest.main()
