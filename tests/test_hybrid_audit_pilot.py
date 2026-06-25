"""
Hermetic tests for V45 Phase 4A hybrid audit enqueue pilot.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.run_hybrid_audit_pilot import (
    _ENQUEUE_PARAM_NAMES,
    _ENQUEUE_RPC,
    assert_anthropic_configured,
    assert_pilot_allowed,
    assert_supabase_configured,
    build_enqueue_params,
    enqueue_hybrid_audit_job,
    format_enqueue_report,
    load_payload_file,
    main as pilot_main,
    run_enqueue_pilot,
    running_under_pytest,
    validate_hybrid_audit_payload,
)

_TARGET_QV_ID = "bbbbbbbb-0000-0000-0000-000000000001"
_TARGET_CAND_ID = "cccccccc-0000-0000-0000-000000000001"

_VALID_PAYLOAD = {
    "target_question_version_id": _TARGET_QV_ID,
    "created_by": "audit-worker@certbound.io",
    "model_name": "claude-sonnet-4-6",
    "prompt_version": "v1.0.0",
    "ruleset_version": "1.0.0",
    "system_prompt": "You are a CertBound certification question auditor.",
    "user_prompt": "Audit this question for material defects.",
    "question": {
        "question_text": "What is Salesforce?",
        "explanation": "Salesforce is a CRM platform.",
        "question_type": "single",
        "select_count": 1,
        "options": [
            {
                "option_label": "A",
                "option_text": "CRM",
                "is_correct": True,
                "display_order": 1,
            },
            {
                "option_label": "B",
                "option_text": "ERP",
                "is_correct": False,
                "display_order": 2,
            },
        ],
    },
}


class FakeRpcResult:
    def __init__(self, data=None, error=None):
        self.data = data if data is not None else []
        self.error = error


class FakeRpcBuilder:
    def __init__(self, client, name, params):
        self._client = client
        self._name = name
        self._params = params

    def execute(self):
        self._client.rpc_calls.append((self._name, self._params))
        response = self._client.responses.get(self._name)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSupabase:
    def __init__(self):
        self.rpc_calls: list[tuple[str, dict]] = []
        self.responses: dict = {}

    def set_response(self, name: str, data: list):
        self.responses[name] = FakeRpcResult(data=data)

    def set_error(self, name: str, message: str):
        self.responses[name] = FakeRpcResult(data=[], error=message)

    def rpc(self, name: str, params: dict):
        return FakeRpcBuilder(self, name, params)


class TestHybridAuditPayloadValidation(unittest.TestCase):

    def test_accepts_question_version_target(self):
        target_type, target_id = validate_hybrid_audit_payload(_VALID_PAYLOAD)
        self.assertEqual(target_type, "question_version")
        self.assertEqual(target_id, _TARGET_QV_ID)

    def test_accepts_candidate_target(self):
        payload = {
            **_VALID_PAYLOAD,
            "target_question_version_id": None,
            "target_candidate_id": _TARGET_CAND_ID,
        }
        target_type, target_id = validate_hybrid_audit_payload(payload)
        self.assertEqual(target_type, "candidate")
        self.assertEqual(target_id, _TARGET_CAND_ID)

    def test_rejects_both_targets(self):
        payload = {
            **_VALID_PAYLOAD,
            "target_candidate_id": _TARGET_CAND_ID,
        }
        with self.assertRaisesRegex(
            ValueError,
            "exactly one of target_question_version_id or target_candidate_id",
        ):
            validate_hybrid_audit_payload(payload)

    def test_rejects_no_target(self):
        payload = {**_VALID_PAYLOAD, "target_question_version_id": ""}
        with self.assertRaisesRegex(
            ValueError,
            "exactly one of target_question_version_id or target_candidate_id",
        ):
            validate_hybrid_audit_payload(payload)


class TestEnqueueRpcContract(unittest.TestCase):

    def test_build_enqueue_params_uses_exact_rpc_parameter_names(self):
        params = build_enqueue_params(
            _VALID_PAYLOAD,
            available_at="2026-06-24T12:00:00+00:00",
            estimated_cost_usd=0.01,
            metadata={"pilot": "v45-4a"},
        )
        self.assertEqual(set(params.keys()), set(_ENQUEUE_PARAM_NAMES))
        self.assertEqual(params["p_job_type"], "hybrid_audit")
        self.assertIs(params["p_payload"], _VALID_PAYLOAD)
        self.assertEqual(params["p_created_by"], _VALID_PAYLOAD["created_by"])
        self.assertEqual(params["p_model_name"], _VALID_PAYLOAD["model_name"])
        self.assertEqual(params["p_prompt_version"], _VALID_PAYLOAD["prompt_version"])

    def test_enqueue_calls_exact_rpc_once(self):
        fake = FakeSupabase()
        fake.set_response(
            _ENQUEUE_RPC,
            [{"job_id": "aaaaaaaa-0000-0000-0000-000000000099", "job_status": "pending"}],
        )
        params = build_enqueue_params(
            _VALID_PAYLOAD,
            available_at="2026-06-24T12:00:00+00:00",
        )

        row = enqueue_hybrid_audit_job(fake, params)

        self.assertEqual(len(fake.rpc_calls), 1)
        rpc_name, rpc_params = fake.rpc_calls[0]
        self.assertEqual(rpc_name, "enqueue_background_job_v1")
        self.assertEqual(set(rpc_params.keys()), set(_ENQUEUE_PARAM_NAMES))
        self.assertEqual(row["job_id"], "aaaaaaaa-0000-0000-0000-000000000099")
        self.assertEqual(row["job_status"], "pending")

    def test_run_enqueue_pilot_printable_report(self):
        fake = FakeSupabase()
        fake.set_response(
            _ENQUEUE_RPC,
            [{"job_id": "aaaaaaaa-0000-0000-0000-000000000099", "job_status": "pending"}],
        )

        report = run_enqueue_pilot(
            fake,
            _VALID_PAYLOAD,
            available_at="2026-06-24T12:00:00+00:00",
        )

        self.assertEqual(len(fake.rpc_calls), 1)
        self.assertEqual(
            report,
            format_enqueue_report(
                {
                    "job_id": "aaaaaaaa-0000-0000-0000-000000000099",
                    "job_status": "pending",
                },
                target_type="question_version",
                target_id=_TARGET_QV_ID,
                model_name=_VALID_PAYLOAD["model_name"],
                prompt_version=_VALID_PAYLOAD["prompt_version"],
            ),
        )


class TestSafetyGuards(unittest.TestCase):

    def test_assert_pilot_allowed_blocks_pytest(self):
        with patch(
            "workers.run_hybrid_audit_pilot.running_under_pytest",
            return_value=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "under pytest"):
                assert_pilot_allowed()

    def test_assert_pilot_allowed_blocks_missing_live_flag(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "workers.run_hybrid_audit_pilot.running_under_pytest",
                return_value=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "CERTBOUND_ALLOW_LIVE_AI_TEST"):
                    assert_pilot_allowed()

    def test_assert_supabase_configured_requires_env(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SUPABASE_URL"):
                assert_supabase_configured()

    @patch("workers.run_hybrid_audit_pilot.build_llm_provider_from_env", return_value=None)
    def test_assert_anthropic_configured_requires_provider(self, _mock_provider):
        with self.assertRaisesRegex(RuntimeError, "Anthropic is not configured"):
            assert_anthropic_configured()

    @patch("workers.run_hybrid_audit_pilot.build_supabase_client")
    @patch("workers.run_hybrid_audit_pilot.build_llm_provider_from_env")
    def test_main_refuses_under_pytest(self, _mock_provider, _mock_client):
        with patch(
            "workers.run_hybrid_audit_pilot.running_under_pytest",
            return_value=True,
        ):
            self.assertEqual(pilot_main([]), 2)

    @patch("workers.run_hybrid_audit_pilot.build_supabase_client")
    @patch("workers.run_hybrid_audit_pilot.build_llm_provider_from_env")
    def test_main_refuses_without_live_flag(self, _mock_provider, _mock_client):
        env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-key",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "workers.run_hybrid_audit_pilot.running_under_pytest",
                return_value=False,
            ):
                self.assertEqual(pilot_main([]), 1)

    @patch("workers.run_hybrid_audit_pilot.build_llm_provider_from_env", return_value=object())
    def test_main_refuses_without_supabase_env(self, _mock_provider):
        env = {"CERTBOUND_ALLOW_LIVE_AI_TEST": "1"}
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "workers.run_hybrid_audit_pilot.running_under_pytest",
                return_value=False,
            ):
                self.assertEqual(pilot_main([]), 1)


class TestPayloadFileLoading(unittest.TestCase):

    def test_load_payload_file_round_trip(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(_VALID_PAYLOAD, handle)
            path = Path(handle.name)
        try:
            loaded = load_payload_file(path)
            self.assertEqual(loaded, _VALID_PAYLOAD)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
