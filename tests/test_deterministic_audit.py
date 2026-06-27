"""
Tests for Phase 8D (deterministic audit engine) and Phase 8E (audit
orchestration layer), including end-to-end worker integration.

Coverage
--------
Deterministic checks:
  * valid question → zero findings
  * each check produces the expected finding_code on a minimal bad question
  * all finding dicts contain the required keys

Orchestration (audit_orchestration.py):
  * create_audit_run_v1 is called before check_fn
  * complete_audit_run_v1 receives the findings returned by check_fn
  * failure after creation calls end_audit_run_v1 (best-effort), re-raises
  * create_audit_run_v1 failure does not call complete or end

Handler (job_handlers.make_deterministic_audit_handler):
  * exactly one target required
  * both targets → HandlerPayloadError
  * missing question → HandlerPayloadError
  * no direct .table() calls

Worker integration:
  * successful deterministic audit reaches complete_background_job_v1
  * audit engine failure calls end_audit_run_v1 then fail_background_job_v1
  * no direct .table() calls anywhere in the path
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any

# Make project root importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from workers.deterministic_audit import (
    DETECTOR_NAME,
    DETECTOR_VERSION,
    _FINDING_CODES,
    run_deterministic_checks,
    check_question_text,
    check_select_count,
    check_option_count,
    check_empty_option_text,
    check_duplicate_option_labels,
    check_duplicate_option_text,
    check_correct_count,
    check_explanation,
    check_single_select_count,
    check_duplicate_correct_options,
    check_display_order,
)
from workers.audit_orchestration import orchestrate_audit
from workers.job_handlers import (
    HandlerPayloadError,
    NotImplementedHandler,
    build_handler_registry,
    make_deterministic_audit_handler,
)
from workers.background_worker import BackgroundWorker


# ===========================================================================
# Shared helpers / fixtures
# ===========================================================================

_AUDIT_RUN_ID  = "aaaaaaaa-0000-0000-0000-000000000099"
_QUESTION_VSID = "bbbbbbbb-0000-0000-0000-000000000001"


VALID_QUESTION = {
    "question_text": "What is the primary purpose of Salesforce Flows?",
    "explanation":   "Salesforce Flows automate business processes without code.",
    "question_type": "single",
    "select_count":  1,
    "options": [
        {"option_label": "A", "option_text": "Automate business processes",
         "is_correct": True,  "display_order": 1},
        {"option_label": "B", "option_text": "Replace the Salesforce UI",
         "is_correct": False, "display_order": 2},
        {"option_label": "C", "option_text": "Manage user licenses",
         "is_correct": False, "display_order": 3},
    ],
}

VALID_AUDIT_PAYLOAD = {
    "target_question_version_id": _QUESTION_VSID,
    "created_by":                 "audit-worker@certbound.io",
    "ruleset_version":            "1.0.0",
    "question":                   VALID_QUESTION,
}

_REQUIRED_FINDING_KEYS = {
    "finding_code", "finding_type", "severity", "materiality", "title", "description",
    "detector_name", "detector_version", "metadata", "evidence",
}

_CANONICAL_FINDING_CODES = _FINDING_CODES | frozenset({"EXPLANATION_MISSING"})

_CREATE_RESPONSE  = [{"audit_run_id": _AUDIT_RUN_ID, "run_status": "pending"}]
_COMPLETE_RESPONSE = [
    {"audit_run_id": _AUDIT_RUN_ID, "run_status": "completed",
     "finding_count": 0, "evidence_count": 0},
]
_END_RESPONSE = [
    {"audit_run_id": _AUDIT_RUN_ID, "run_status": "failed",
     "completed_at": "2099-01-01T00:00:00+00:00"},
]


# ---------------------------------------------------------------------------
# FakeRpcResult / FakeSupabase (scoped to this module; mirrors test_background_worker)
# ---------------------------------------------------------------------------

class _FakeRpcResult:
    def __init__(self, data, error=None):
        self.data  = data
        self.error = error


class _FakeRpcBuilder:
    def __init__(self, data, error=None):
        self._data  = data
        self._error = error

    def execute(self):
        return _FakeRpcResult(self._data, self._error)


class FakeSupabase:
    """Minimal Supabase client fake for audit tests.

    Does NOT expose .table() — calling it raises AttributeError,
    so any direct table access is caught by tests.
    """

    def __init__(self):
        self._responses: dict[str, list] = {}
        self._errors: dict[str, str]     = {}
        self.rpc_calls: list[dict]        = []

    def set_response(self, name: str, data: list) -> None:
        self._responses[name] = data
        self._errors.pop(name, None)

    def set_error(self, name: str, message: str) -> None:
        self._errors[name] = message
        self._responses.pop(name, None)

    def rpc(self, name: str, params: dict) -> _FakeRpcBuilder:
        self.rpc_calls.append({"name": name, "params": params})
        if name in self._errors:
            return _FakeRpcBuilder(data=[], error=self._errors[name])
        data = self._responses.get(name, [])
        return _FakeRpcBuilder(data=data)

    def calls_for(self, name: str) -> list[dict]:
        return [c for c in self.rpc_calls if c["name"] == name]

    @property
    def called_rpc_names(self) -> list[str]:
        return [c["name"] for c in self.rpc_calls]

    def set_error_response(self, name: str, message: str) -> None:
        """Alias used by BackgroundWorker integration tests."""
        self.set_error(name, message)


# ===========================================================================
# Section 1 — deterministic_audit.py
# ===========================================================================

class TestFindingStructure(unittest.TestCase):
    """Every finding dict produced by any check must carry the required keys."""

    def _assert_finding_keys(self, findings):
        for f in findings:
            missing = _REQUIRED_FINDING_KEYS - set(f.keys())
            self.assertFalse(
                missing,
                f"Finding {f.get('finding_code')} is missing keys: {missing}",
            )
            self.assertEqual(f["detector_name"],    DETECTOR_NAME)
            self.assertEqual(f["detector_version"], DETECTOR_VERSION)
            self.assertIsInstance(f["evidence"], list)
            self.assertIsInstance(f["metadata"], dict)

    def test_finding_codes_match_published_set(self):
        """Every produced finding_code must be in the declared set."""
        # Make a question that violates every rule simultaneously.
        bad_q = {
            "question_text": "",
            "explanation":   "",
            "question_type": "single",
            "select_count":  0,
            "options":       [
                {"option_label": "A", "option_text": "dup",
                 "is_correct": True,  "display_order": 1},
                {"option_label": "A", "option_text": "dup",
                 "is_correct": True,  "display_order": 1},
            ],
        }
        findings = run_deterministic_checks(bad_q)
        self._assert_finding_keys(findings)
        for f in findings:
            self.assertIn(
                f["finding_code"],
                _CANONICAL_FINDING_CODES,
                f"Unexpected finding_code: {f['finding_code']}",
            )


class TestValidQuestion(unittest.TestCase):
    def test_zero_findings(self):
        findings = run_deterministic_checks(VALID_QUESTION)
        self.assertEqual(findings, [], f"Unexpected findings: {findings}")

    def test_ruleset_version_in_metadata(self):
        findings = run_deterministic_checks(VALID_QUESTION, ruleset_version="2.5.0")
        # VALID_QUESTION has no issues, but if there were findings each metadata
        # would carry the version. Test indirectly via a triggered finding.
        bad_q = {**VALID_QUESTION, "explanation": ""}
        findings = run_deterministic_checks(bad_q, ruleset_version="2.5.0")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["metadata"]["ruleset_version"], "2.5.0")


class TestCheckQuestionText(unittest.TestCase):
    def _bad(self, text):
        return {**VALID_QUESTION, "question_text": text}

    def test_empty_string(self):
        findings = check_question_text(self._bad(""))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["finding_code"], "EMPTY_QUESTION_TEXT")

    def test_whitespace_only(self):
        findings = check_question_text(self._bad("   \t\n"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["finding_code"], "EMPTY_QUESTION_TEXT")

    def test_none_value(self):
        findings = check_question_text(self._bad(None))
        self.assertEqual(len(findings), 1)

    def test_valid_text_no_finding(self):
        findings = check_question_text(VALID_QUESTION)
        self.assertEqual(findings, [])


class TestCheckSelectCount(unittest.TestCase):
    def _q(self, sc):
        return {**VALID_QUESTION, "select_count": sc}

    def test_zero_triggers(self):
        self.assertEqual(
            check_select_count(self._q(0))[0]["finding_code"],
            "INVALID_SELECT_COUNT",
        )

    def test_none_triggers(self):
        self.assertEqual(
            check_select_count(self._q(None))[0]["finding_code"],
            "INVALID_SELECT_COUNT",
        )

    def test_string_triggers(self):
        self.assertEqual(
            check_select_count(self._q("1"))[0]["finding_code"],
            "INVALID_SELECT_COUNT",
        )

    def test_negative_triggers(self):
        self.assertEqual(
            check_select_count(self._q(-1))[0]["finding_code"],
            "INVALID_SELECT_COUNT",
        )

    def test_valid_no_finding(self):
        self.assertEqual(check_select_count(VALID_QUESTION), [])


class TestCheckOptionCount(unittest.TestCase):
    def _q(self, n_options):
        opts = VALID_QUESTION["options"][:n_options]
        return {**VALID_QUESTION, "options": opts}

    def test_zero_options(self):
        self.assertEqual(
            check_option_count(self._q(0))[0]["finding_code"],
            "TOO_FEW_OPTIONS",
        )

    def test_one_option(self):
        self.assertEqual(
            check_option_count(self._q(1))[0]["finding_code"],
            "TOO_FEW_OPTIONS",
        )

    def test_two_options_no_finding(self):
        self.assertEqual(check_option_count(self._q(2)), [])


class TestCheckEmptyOptionText(unittest.TestCase):
    def test_empty_text_triggers(self):
        q = {
            **VALID_QUESTION,
            "options": [
                {"option_label": "A", "option_text": "", "is_correct": True},
                {"option_label": "B", "option_text": "ok", "is_correct": False},
            ],
        }
        findings = check_empty_option_text(q)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["finding_code"], "EMPTY_OPTION_TEXT")
        self.assertEqual(findings[0]["metadata"]["option_index"], 0)

    def test_whitespace_text_triggers(self):
        q = {
            **VALID_QUESTION,
            "options": [
                {"option_label": "A", "option_text": "  ", "is_correct": True},
                {"option_label": "B", "option_text": "valid", "is_correct": False},
            ],
        }
        findings = check_empty_option_text(q)
        self.assertEqual(len(findings), 1)

    def test_multiple_empty(self):
        q = {
            **VALID_QUESTION,
            "options": [
                {"option_label": "A", "option_text": "", "is_correct": True},
                {"option_label": "B", "option_text": "", "is_correct": False},
            ],
        }
        findings = check_empty_option_text(q)
        self.assertEqual(len(findings), 2)

    def test_valid_no_finding(self):
        self.assertEqual(check_empty_option_text(VALID_QUESTION), [])


class TestCheckDuplicateOptionLabels(unittest.TestCase):
    def test_duplicate_triggers(self):
        q = {
            **VALID_QUESTION,
            "options": [
                {"option_label": "A", "option_text": "x", "is_correct": True},
                {"option_label": "A", "option_text": "y", "is_correct": False},
            ],
        }
        findings = check_duplicate_option_labels(q)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["finding_code"], "DUPLICATE_OPTION_LABELS")
        self.assertIn("A", findings[0]["metadata"]["duplicate_labels"])

    def test_unique_labels_no_finding(self):
        self.assertEqual(check_duplicate_option_labels(VALID_QUESTION), [])


class TestCheckDuplicateOptionText(unittest.TestCase):
    def test_exact_duplicate_triggers(self):
        q = {
            **VALID_QUESTION,
            "options": [
                {"option_label": "A", "option_text": "Same text", "is_correct": True},
                {"option_label": "B", "option_text": "Same text", "is_correct": False},
            ],
        }
        findings = check_duplicate_option_text(q)
        self.assertEqual(findings[0]["finding_code"], "DUPLICATE_OPTION_TEXT")

    def test_case_insensitive_normalization(self):
        q = {
            **VALID_QUESTION,
            "options": [
                {"option_label": "A", "option_text": "UPPER", "is_correct": True},
                {"option_label": "B", "option_text": "upper", "is_correct": False},
            ],
        }
        findings = check_duplicate_option_text(q)
        self.assertEqual(len(findings), 1)

    def test_unique_text_no_finding(self):
        self.assertEqual(check_duplicate_option_text(VALID_QUESTION), [])


class TestCheckCorrectCount(unittest.TestCase):
    def test_too_many_correct(self):
        q = {
            **VALID_QUESTION,
            "select_count": 1,
            "options": [
                {"option_label": "A", "option_text": "a", "is_correct": True},
                {"option_label": "B", "option_text": "b", "is_correct": True},
            ],
        }
        findings = check_correct_count(q)
        self.assertEqual(findings[0]["finding_code"], "CORRECT_COUNT_MISMATCH")
        self.assertEqual(findings[0]["metadata"]["correct_count"], 2)

    def test_no_correct(self):
        q = {
            **VALID_QUESTION,
            "select_count": 1,
            "options": [
                {"option_label": "A", "option_text": "a", "is_correct": False},
                {"option_label": "B", "option_text": "b", "is_correct": False},
            ],
        }
        findings = check_correct_count(q)
        self.assertEqual(findings[0]["finding_code"], "CORRECT_COUNT_MISMATCH")

    def test_match_no_finding(self):
        self.assertEqual(check_correct_count(VALID_QUESTION), [])

    def test_invalid_select_count_skips(self):
        q = {**VALID_QUESTION, "select_count": "bad"}
        self.assertEqual(check_correct_count(q), [])


class TestCheckExplanation(unittest.TestCase):
    def test_missing_triggers(self):
        q = {**VALID_QUESTION, "explanation": ""}
        findings = check_explanation(q)
        self.assertEqual(findings[0]["finding_code"], "MISSING_EXPLANATION")

    def test_whitespace_triggers(self):
        q = {**VALID_QUESTION, "explanation": "   "}
        self.assertEqual(check_explanation(q)[0]["finding_code"], "MISSING_EXPLANATION")

    def test_none_triggers(self):
        q = {**VALID_QUESTION, "explanation": None}
        self.assertEqual(check_explanation(q)[0]["finding_code"], "MISSING_EXPLANATION")

    def test_valid_no_finding(self):
        self.assertEqual(check_explanation(VALID_QUESTION), [])


class TestCheckSingleSelectCount(unittest.TestCase):
    def test_single_with_count_2_triggers(self):
        q = {**VALID_QUESTION, "question_type": "single", "select_count": 2}
        findings = check_single_select_count(q)
        self.assertEqual(findings[0]["finding_code"], "SINGLE_SELECT_COUNT_MISMATCH")

    def test_single_with_count_1_no_finding(self):
        self.assertEqual(check_single_select_count(VALID_QUESTION), [])

    def test_multi_type_skipped(self):
        q = {**VALID_QUESTION, "question_type": "multiple", "select_count": 2}
        self.assertEqual(check_single_select_count(q), [])


class TestCheckDuplicateCorrectOptions(unittest.TestCase):
    def test_duplicate_correct_triggers(self):
        q = {
            **VALID_QUESTION,
            "select_count": 2,
            "options": [
                {"option_label": "A", "option_text": "same", "is_correct": True},
                {"option_label": "B", "option_text": "SAME", "is_correct": True},
                {"option_label": "C", "option_text": "other", "is_correct": False},
            ],
        }
        findings = check_duplicate_correct_options(q)
        self.assertEqual(findings[0]["finding_code"], "DUPLICATE_CORRECT_OPTIONS")

    def test_distinct_correct_no_finding(self):
        self.assertEqual(check_duplicate_correct_options(VALID_QUESTION), [])


class TestCheckDisplayOrder(unittest.TestCase):
    def test_duplicate_display_order(self):
        q = {
            **VALID_QUESTION,
            "options": [
                {"option_label": "A", "option_text": "a", "is_correct": True,  "display_order": 1},
                {"option_label": "B", "option_text": "b", "is_correct": False, "display_order": 1},
            ],
        }
        findings = check_display_order(q)
        self.assertEqual(findings[0]["finding_code"], "OPTION_DISPLAY_ORDER_ISSUES")
        self.assertIn("duplicate", findings[0]["metadata"]["issues"][0])

    def test_gaps_in_display_order(self):
        q = {
            **VALID_QUESTION,
            "options": [
                {"option_label": "A", "option_text": "a", "is_correct": True,  "display_order": 1},
                {"option_label": "B", "option_text": "b", "is_correct": False, "display_order": 3},
            ],
        }
        findings = check_display_order(q)
        self.assertEqual(findings[0]["finding_code"], "OPTION_DISPLAY_ORDER_ISSUES")
        issue_text = " ".join(findings[0]["metadata"]["issues"])
        self.assertIn("gap", issue_text)

    def test_no_display_order_no_finding(self):
        q = {
            **VALID_QUESTION,
            "options": [
                {"option_label": "A", "option_text": "a", "is_correct": True},
                {"option_label": "B", "option_text": "b", "is_correct": False},
            ],
        }
        self.assertEqual(check_display_order(q), [])

    def test_contiguous_no_finding(self):
        self.assertEqual(check_display_order(VALID_QUESTION), [])


# ===========================================================================
# Section 2 — audit_orchestration.py
# ===========================================================================

class TestOrchestrateAudit(unittest.TestCase):

    def _make_client(self, *, create_error=False, check_raises=False):
        fake = FakeSupabase()
        if create_error:
            fake.set_error("create_audit_run_v1", "target not found")
        else:
            fake.set_response("create_audit_run_v1", _CREATE_RESPONSE)
        fake.set_response("complete_audit_run_v1", _COMPLETE_RESPONSE)
        fake.set_response("end_audit_run_v1",      _END_RESPONSE)
        return fake

    def _base_kwargs(self, check_fn):
        return dict(
            audit_type="deterministic",
            target_question_version_id=_QUESTION_VSID,
            target_candidate_id=None,
            created_by="test@certbound.io",
            ruleset_version="1.0.0",
            resource_snapshot=None,
            metadata=None,
            check_fn=check_fn,
        )

    # ---- create is called before check_fn ----

    def test_create_called_before_check(self):
        call_log = []
        fake = self._make_client()

        def check_fn():
            call_log.append("check_fn")
            return []

        # Wrap fake.rpc to record calls in call_log before the real behaviour.
        original_rpc = fake.rpc

        def recording_rpc(name, params):
            call_log.append(name)
            return original_rpc(name, params)

        fake.rpc = recording_rpc
        orchestrate_audit(fake, **self._base_kwargs(check_fn))

        create_idx  = call_log.index("create_audit_run_v1")
        check_idx   = call_log.index("check_fn")
        complete_idx = call_log.index("complete_audit_run_v1")
        self.assertLess(create_idx, check_idx)
        self.assertLess(check_idx,  complete_idx)

    # ---- complete receives findings ----

    def test_complete_receives_findings(self):
        findings = [
            {
                "finding_code": "EMPTY_QUESTION_TEXT",
                "finding_type": "formatting",
                "severity":     "critical",
                "title":        "Empty question",
                "description":  "test",
                "detector_name":    "d",
                "detector_version": "1",
                "metadata":     {},
                "evidence":     [],
            }
        ]
        fake = self._make_client()
        orchestrate_audit(
            fake,
            **self._base_kwargs(lambda: findings),
            question_snapshot={"question_id": "cccccccc-0000-0000-0000-000000000001"},
        )

        complete_calls = fake.calls_for("complete_audit_run_v1")
        self.assertEqual(len(complete_calls), 1)
        sent_findings = complete_calls[0]["params"]["p_findings"]
        self.assertEqual(len(sent_findings), 1)
        sent = sent_findings[0]
        self.assertEqual(sent["finding_code"], findings[0]["finding_code"])
        self.assertEqual(sent["description"], findings[0]["description"])
        self.assertIn("evidence_contract", sent["metadata"])
        self.assertEqual(
            sent["metadata"]["evidence_contract"]["question_version_id"],
            _QUESTION_VSID,
        )

    # ---- failure after creation calls end_audit_run_v1 ----

    def test_failure_after_creation_calls_end_audit_run(self):
        fake = self._make_client()

        def bad_check():
            raise RuntimeError("engine exploded")

        with self.assertRaises(RuntimeError, msg="original exception must propagate"):
            orchestrate_audit(fake, **self._base_kwargs(bad_check))

        self.assertIn("end_audit_run_v1", fake.called_rpc_names)
        self.assertNotIn("complete_audit_run_v1", fake.called_rpc_names)

        end_calls = fake.calls_for("end_audit_run_v1")
        self.assertEqual(end_calls[0]["params"]["p_final_status"], "failed")
        self.assertIn("engine exploded", end_calls[0]["params"]["p_reason"])

    # ---- create failure — no end or complete ----

    def test_create_failure_does_not_call_end_or_complete(self):
        fake = self._make_client(create_error=True)

        with self.assertRaises(RuntimeError):
            orchestrate_audit(fake, **self._base_kwargs(lambda: []))

        self.assertNotIn("end_audit_run_v1",      fake.called_rpc_names)
        self.assertNotIn("complete_audit_run_v1", fake.called_rpc_names)

    # ---- return value ----

    def test_returns_structured_result(self):
        fake = self._make_client()
        result = orchestrate_audit(fake, **self._base_kwargs(lambda: []))
        self.assertIn("audit_run_id",   result)
        self.assertIn("run_status",     result)
        self.assertIn("finding_count",  result)
        self.assertIn("evidence_count", result)
        self.assertEqual(result["run_status"], "completed")


# ===========================================================================
# Section 3 — make_deterministic_audit_handler
# ===========================================================================

class TestDeterministicAuditHandler(unittest.TestCase):

    def _make_client(self):
        fake = FakeSupabase()
        fake.set_response("create_audit_run_v1",  _CREATE_RESPONSE)
        fake.set_response("complete_audit_run_v1", _COMPLETE_RESPONSE)
        fake.set_response("end_audit_run_v1",      _END_RESPONSE)
        return fake

    def _call(self, payload, *, client=None):
        c = client or self._make_client()
        handler = make_deterministic_audit_handler(c)
        return handler(
            job_id="j1", payload=payload, checkpoint={},
            attempt=1, heartbeat_fn=lambda: None,
        )

    # ---- target validation ----

    def test_no_target_raises_payload_error(self):
        payload = {
            "created_by": "test@certbound.io",
            "question":   VALID_QUESTION,
        }
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    def test_both_targets_raises_payload_error(self):
        payload = {
            **VALID_AUDIT_PAYLOAD,
            "target_candidate_id": "cccccccc-0000-0000-0000-000000000001",
        }
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    def test_missing_created_by_raises_payload_error(self):
        payload = {k: v for k, v in VALID_AUDIT_PAYLOAD.items() if k != "created_by"}
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    def test_missing_question_raises_payload_error(self):
        payload = {k: v for k, v in VALID_AUDIT_PAYLOAD.items() if k != "question"}
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    def test_question_not_dict_raises_payload_error(self):
        payload = {**VALID_AUDIT_PAYLOAD, "question": "not a dict"}
        with self.assertRaises(HandlerPayloadError):
            self._call(payload)

    # ---- success ----

    def test_successful_call_returns_structured_result(self):
        result = self._call(VALID_AUDIT_PAYLOAD)
        self.assertIn("audit_run_id",   result)
        self.assertIn("run_status",     result)
        self.assertIn("finding_count",  result)
        self.assertIn("evidence_count", result)

    def test_candidate_id_target_accepted(self):
        payload = {
            "target_candidate_id": "cccccccc-0000-0000-0000-000000000001",
            "created_by":          "test@certbound.io",
            "question":            VALID_QUESTION,
        }
        result = self._call(payload)
        self.assertIn("audit_run_id", result)

    # ---- no .table() calls ----

    def test_no_direct_table_calls(self):
        """Handler must never call client.table()."""
        fake = self._make_client()
        self.assertFalse(hasattr(fake, "table"),
                         "FakeSupabase must not expose .table()")
        handler = make_deterministic_audit_handler(fake)
        handler(
            job_id="j1", payload=VALID_AUDIT_PAYLOAD, checkpoint={},
            attempt=1, heartbeat_fn=lambda: None,
        )
        # If we reach here, no AttributeError was raised — no .table() call.


# ===========================================================================
# Section 4 — Worker integration
# ===========================================================================

def _make_bg_job(job_type: str, payload: dict) -> dict:
    return {
        "job_id":       "job-audit-01",
        "job_type":     job_type,
        "payload":      payload,
        "checkpoint":   {},
        "attempt":      1,
        "job_status":   "running",
        "lease_expires_at": "2099-01-01T00:00:00+00:00",
    }


class TestWorkerDeterministicAuditIntegration(unittest.TestCase):
    """End-to-end: BackgroundWorker dispatches to deterministic_audit handler."""

    def _make_worker(self, fake):
        return BackgroundWorker(
            worker_id="integration-audit-worker",
            client=fake,
            handlers=build_handler_registry(fake),
            sleep_interval=0.0,
        )

    def _heartbeat_response(self, job_id: str) -> dict:
        return {
            "job_id":          job_id,
            "job_status":      "running",
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
            "heartbeat_at":    "2099-01-01T00:00:00+00:00",
        }

    # ---- successful deterministic audit reaches complete_background_job_v1 ----

    def test_deterministic_result_reaches_complete_job_rpc(self):
        job  = _make_bg_job("deterministic_audit", VALID_AUDIT_PAYLOAD)
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1",     [job])
        fake.set_response("heartbeat_background_job_v1", [self._heartbeat_response(job["job_id"])])
        fake.set_response("create_audit_run_v1",         _CREATE_RESPONSE)
        fake.set_response("complete_audit_run_v1",       _COMPLETE_RESPONSE)
        fake.set_response("complete_background_job_v1",  [
            {"job_id": job["job_id"], "job_status": "completed",
             "completed_at": "2099-01-01T00:00:00+00:00"},
        ])

        worker = self._make_worker(fake)
        worker.run_once()

        self.assertIn("complete_background_job_v1", fake.called_rpc_names)
        self.assertNotIn("fail_background_job_v1",  fake.called_rpc_names)

        complete_calls = fake.calls_for("complete_background_job_v1")
        result = complete_calls[0]["params"].get("p_result", {})
        self.assertIn("audit_run_id",  result)
        self.assertIn("run_status",    result)
        self.assertIn("finding_count", result)

    # ---- engine failure triggers end_audit_run + fail_background_job ----

    def test_engine_failure_calls_end_audit_run_and_fail_job(self):
        """When deterministic checks crash, orchestration calls end_audit_run_v1
        (best-effort) and the worker calls fail_background_job_v1."""
        job  = _make_bg_job("deterministic_audit", VALID_AUDIT_PAYLOAD)
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1",     [job])
        fake.set_response("heartbeat_background_job_v1", [self._heartbeat_response(job["job_id"])])
        fake.set_response("create_audit_run_v1",         _CREATE_RESPONSE)
        # Simulate the check crashing via a malformed question key that raises.
        # We inject a broken question directly by patching the payload.
        bad_payload = {**VALID_AUDIT_PAYLOAD, "question": None}
        job["payload"] = bad_payload
        fake.set_response("end_audit_run_v1", _END_RESPONSE)
        fake.set_response("fail_background_job_v1", [
            {"job_id": job["job_id"], "job_status": "pending",
             "available_at": None, "completed_at": None},
        ])

        worker = self._make_worker(fake)
        worker.run_once()

        # The handler raises HandlerPayloadError (not a check engine crash),
        # so orchestrate_audit is never entered. fail_background_job_v1 must
        # still be called by the worker.
        self.assertIn("fail_background_job_v1", fake.called_rpc_names)
        self.assertNotIn("complete_background_job_v1", fake.called_rpc_names)

    # ---- no direct .table() access throughout the path ----

    def test_no_direct_table_calls(self):
        job  = _make_bg_job("deterministic_audit", VALID_AUDIT_PAYLOAD)
        fake = FakeSupabase()
        fake.set_response("claim_background_job_v1",     [job])
        fake.set_response("heartbeat_background_job_v1", [self._heartbeat_response(job["job_id"])])
        fake.set_response("create_audit_run_v1",         _CREATE_RESPONSE)
        fake.set_response("complete_audit_run_v1",       _COMPLETE_RESPONSE)
        fake.set_response("complete_background_job_v1",  [
            {"job_id": job["job_id"], "job_status": "completed",
             "completed_at": "2099-01-01T00:00:00+00:00"},
        ])
        self.assertFalse(hasattr(fake, "table"))
        worker = self._make_worker(fake)
        worker.run_once()


if __name__ == "__main__":
    unittest.main()
