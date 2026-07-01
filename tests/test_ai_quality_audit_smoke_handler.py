"""
Smoke handler registration and payload tests for V48 AI quality audit.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_worker import AiQualityAuditProviders
from workers.job_handlers import (
    HANDLER_REGISTRY,
    HandlerPayloadError,
    build_handler_registry,
    make_ai_quality_audit_smoke_handler,
)
from workers.llm_providers import LlmProviderError, MissingProviderError

_QVID = "cccccccc-0000-0000-0000-000000000001"
_RUN_ID = "aaaaaaaa-0000-0000-0000-000000000001"


class _NoOpClient:
    def rpc(self, name, params):
        raise AssertionError(f"unexpected RPC {name!r}")


def _valid_payload(**overrides) -> dict:
    base = {
        "audit_run_id": _RUN_ID,
        "question_version_id": _QVID,
    }
    base.update(overrides)
    return base


def _noop_provider(**_kwargs):
    raise AssertionError("provider must not be called")


class TestAiQualityAuditSmokeHandler(unittest.TestCase):

    def test_handler_registered_in_stub_registry(self):
        self.assertIn("ai_quality_audit_smoke", HANDLER_REGISTRY)
        self.assertTrue(callable(HANDLER_REGISTRY["ai_quality_audit_smoke"]))

    def test_handler_registered_in_build_handler_registry(self):
        registry = build_handler_registry(_NoOpClient())
        self.assertIn("ai_quality_audit_smoke", registry)
        self.assertIs(
            registry["ai_quality_audit_smoke"].__name__,
            "handle_ai_quality_audit_smoke",
        )

    def test_malformed_payload_rejected(self):
        handler = make_ai_quality_audit_smoke_handler(
            _NoOpClient(),
            ai_quality_providers=AiQualityAuditProviders(
                primary=_noop_provider,
                dispute=_noop_provider,
            ),
        )
        with self.assertRaises(HandlerPayloadError):
            handler("job-1", {"question_version_id": _QVID}, {}, 1, lambda: None)
        with self.assertRaises(HandlerPayloadError):
            handler(
                "job-1",
                _valid_payload(audit_run_id="not-a-uuid"),
                {},
                1,
                lambda: None,
            )

    def test_missing_provider_raises_before_rpc(self):
        handler = make_ai_quality_audit_smoke_handler(
            _NoOpClient(),
            ai_quality_providers=None,
        )
        with self.assertRaises(MissingProviderError):
            handler("job-1", _valid_payload(), {}, 1, lambda: None)


if __name__ == "__main__":
    unittest.main()
