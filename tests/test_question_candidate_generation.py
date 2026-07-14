"""
Unit tests for workers/question_candidate_generation.py (V57 Phase 1).

Fully hermetic: no Supabase connection, no network, no real LLM provider.
The Supabase client is replaced with a FakeSupabase supporting both
.table(...) and .rpc(...) calls, matching the shapes actually used by the
module under test.

Run:
    python -m pytest tests/test_question_candidate_generation.py -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.certification_registry import (
    BA_EXAM_NAME,
    PAB_EXAM_NAME,
    SCC_EXAM_NAME,
    get_business_analyst_definition,
    get_platform_app_builder_definition,
    get_sales_cloud_consultant_definition,
    validate_generation_request_certification,
)
from workers.official_evidence_seed import (
    BA_EVIDENCE_CONFIG_ID,
    PAB_EVIDENCE_CONFIG_ID,
    SCC_EVIDENCE_CONFIG_ID,
)
from workers.anthropic_provider import AnthropicAuditProvider, AnthropicProviderConfig
from workers.llm_audit import LlmAuditValidationError
from workers.llm_providers import LlmResponse, SKIP_LEGACY_LLM_AUDIT_VALIDATION_METADATA_KEY
from workers.question_candidate_generation import (
    AuditInitiationError,
    AuditRetryContext,
    CandidatePersistenceError,
    CandidateProvenanceEventError,
    CandidateValidationError,
    GenerationRequest,
    QuestionCandidateRepository,
    GENERATION_RESPONSE_SCHEMA,
    build_generation_prompt,
    build_llm_audit_prompt,
    compute_candidate_content_hash,
    enqueue_candidate_audits,
    generate_and_persist_candidate,
    generation_request_from_candidate_row,
    load_audit_retry_context,
    question_snapshot_from_candidate_row,
    retry_candidate_audits,
    validate_generated_payload,
    validate_generation_request,
)


# ===========================================================================
# Fake Supabase infrastructure (.table() + .rpc())
# ===========================================================================

_FORBIDDEN_TABLES = frozenset(
    {"questions", "answer_options", "question_versions", "question_option_versions"}
)


class FakeQueryResult:
    def __init__(self, data: Optional[List[dict]] = None, error: Any = None) -> None:
        self.data = data if data is not None else []
        self.error = error


class FakeRpcBuilder:
    def __init__(self, result: FakeQueryResult) -> None:
        self._result = result

    def execute(self) -> FakeQueryResult:
        return self._result


class FakeInsert:
    def __init__(self, client: "FakeSupabase", table_name: str, payload: dict) -> None:
        self._client = client
        self._table_name = table_name
        self._payload = payload

    def execute(self) -> FakeQueryResult:
        error = self._client.insert_errors.get(self._table_name)
        if error is not None:
            if isinstance(error, Exception):
                raise error
            return FakeQueryResult(data=None, error=error)
        row = dict(self._payload)
        row.setdefault("id", f"{self._table_name}-{self._client.next_id}")
        self._client.next_id += 1
        row.setdefault("created_at", "2026-01-01T00:00:00+00:00")
        row.setdefault("updated_at", "2026-01-01T00:00:00+00:00")
        self._client.tables.setdefault(self._table_name, []).append(row)
        self._client.insert_calls.append({"table": self._table_name, "payload": dict(self._payload)})
        return FakeQueryResult(data=[row])


class FakeTable:
    def __init__(self, client: "FakeSupabase", name: str) -> None:
        if name in _FORBIDDEN_TABLES:
            raise RuntimeError(
                f"test guard: direct access to live table {name!r} is forbidden"
            )
        self._client = client
        self._name = name
        self._filters: Dict[str, Any] = {}

    def select(self, *_args, **_kwargs) -> "FakeTable":
        return self

    def eq(self, key: str, value: Any) -> "FakeTable":
        self._filters[key] = value
        return self

    def limit(self, _n: int) -> "FakeTable":
        return self

    def insert(self, payload: dict) -> FakeInsert:
        return FakeInsert(self._client, self._name, payload)

    def execute(self) -> FakeQueryResult:
        rows = self._client.tables.get(self._name, [])
        matched = [
            row for row in rows
            if all(row.get(k) == v for k, v in self._filters.items())
        ]
        return FakeQueryResult(data=matched)


class FakeSupabase:
    """Captures .table()/.rpc() calls; forbids direct access to live tables."""

    def __init__(self) -> None:
        self.tables: Dict[str, List[dict]] = {
            "question_candidates": [],
            "question_candidate_events": [],
            "certification_domains": [],
        }
        self.next_id = 1
        self.insert_errors: Dict[str, Any] = {}
        self.insert_calls: List[dict] = []
        self.rpc_calls: List[Dict[str, Any]] = []
        self._rpc_defaults: Dict[str, FakeQueryResult] = {}
        self._rpc_sequences: Dict[str, List[FakeQueryResult]] = {}

    # -- configuration -----------------------------------------------------

    def add_certification_domain(self, exam_name: str, domain_name: str, is_active: bool = True) -> None:
        self.tables["certification_domains"].append(
            {"exam_name": exam_name, "domain_name": domain_name, "is_active": is_active}
        )

    def set_insert_error(self, table_name: str, error: Any) -> None:
        self.insert_errors[table_name] = error

    def set_rpc_response(self, name: str, data: List[dict]) -> None:
        self._rpc_defaults[name] = FakeQueryResult(data=data)

    def set_rpc_error(self, name: str, error: str) -> None:
        self._rpc_defaults[name] = FakeQueryResult(data=None, error=error)

    def set_rpc_sequence(self, name: str, results: List[FakeQueryResult]) -> None:
        self._rpc_sequences[name] = list(results)

    # -- client surface ------------------------------------------------------

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)

    def rpc(self, name: str, params: Optional[dict] = None) -> FakeRpcBuilder:
        self.rpc_calls.append({"name": name, "params": params or {}})
        if name in self._rpc_sequences and self._rpc_sequences[name]:
            return FakeRpcBuilder(self._rpc_sequences[name].pop(0))
        if name in self._rpc_defaults:
            return FakeRpcBuilder(self._rpc_defaults[name])
        if name == "enqueue_background_job_v1":
            job_id = f"job-{len(self.rpc_calls)}"
            return FakeRpcBuilder(FakeQueryResult(data=[{"job_id": job_id, "job_status": "pending"}]))
        return FakeRpcBuilder(FakeQueryResult(data=[]))

    # -- introspection -------------------------------------------------------

    def calls_for(self, rpc_name: str) -> List[Dict[str, Any]]:
        return [c for c in self.rpc_calls if c["name"] == rpc_name]


# ===========================================================================
# Fake LLM provider
# ===========================================================================

class FakeLlmProvider:
    def __init__(
        self,
        response: Optional[LlmResponse] = None,
        responses: Optional[List[LlmResponse]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.response = response
        self.responses = list(responses) if responses is not None else None
        self.error = error
        self.calls: List[dict] = []

    def __call__(self, *, model_name, system_prompt, user_prompt, response_schema, metadata=None) -> LlmResponse:
        self.calls.append(
            {
                "model_name": model_name,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_schema": response_schema,
                "metadata": metadata,
            }
        )
        if self.error is not None:
            raise self.error
        if self.responses is not None:
            return self.responses.pop(0)
        assert self.response is not None
        return self.response


# ===========================================================================
# Shared fixtures
# ===========================================================================

def _valid_options() -> List[dict]:
    return [
        {"option_label": "A", "option_text": "Custom object", "is_correct": False, "display_order": 1},
        {"option_label": "B", "option_text": "Standard object", "is_correct": True, "display_order": 2},
        {"option_label": "C", "option_text": "External object", "is_correct": False, "display_order": 3},
        {"option_label": "D", "option_text": "Big object", "is_correct": False, "display_order": 4},
    ]


def _valid_raw_payload(**overrides) -> dict:
    payload = {
        "question_text": "Which object type is built into Salesforce by default?",
        "explanation": "Standard objects such as Account ship with every org.",
        "options": _valid_options(),
    }
    payload.update(overrides)
    return payload


def _make_request(**overrides) -> GenerationRequest:
    kwargs = dict(
        certification_exam_name="Salesforce Administrator",
        domain="Data and Analytics Management",
        prompt_template_id="certbound-question-gen",
        prompt_version="v1.0.0",
        model_name="claude-3-5-sonnet-20241022",
        created_by="generation-service@certbound.internal",
        source_evidence={"resource_reference": "Salesforce Help: Standard Objects"},
    )
    kwargs.update(overrides)
    return GenerationRequest(**kwargs)


def _llm_response(parsed: dict, **overrides) -> LlmResponse:
    kwargs = dict(
        parsed_response=parsed,
        input_tokens=120,
        output_tokens=80,
        actual_cost_usd=0.002,
        provider_request_id="req-123",
        model_name="claude-3-5-sonnet-20241022",
        provider_name="anthropic",
    )
    kwargs.update(overrides)
    return LlmResponse(**kwargs)


def _seed_domain(fake: FakeSupabase, request: GenerationRequest) -> None:
    fake.add_certification_domain(request.certification_exam_name, request.domain)


# ===========================================================================
# validate_generation_request
# ===========================================================================

class TestValidateGenerationRequest(unittest.TestCase):
    def test_valid_request_passes(self):
        validate_generation_request(_make_request())  # no exception

    def test_invalid_question_type_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(_make_request(question_type="essay"))

    def test_single_requires_select_count_one(self):
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(_make_request(question_type="single", select_count=2))

    def test_multiple_requires_select_count_at_least_two(self):
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(
                _make_request(question_type="multiple", select_count=1)
            )

    def test_missing_source_evidence_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(_make_request(source_evidence={}))

    def test_non_serializable_request_metadata_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(
                _make_request(request_metadata={"bad": object()})
            )

    def test_invalid_difficulty_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(_make_request(difficulty="impossible"))

    def test_blank_created_by_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(_make_request(created_by="  "))


# ===========================================================================
# validate_generated_payload
# ===========================================================================

class TestValidateGeneratedPayload(unittest.TestCase):
    def test_valid_payload_normalizes_cleanly(self):
        validated = validate_generated_payload(_valid_raw_payload(), request=_make_request())
        self.assertEqual(validated["question_type"], "single")
        self.assertEqual(validated["select_count"], 1)
        self.assertEqual(len(validated["options"]), 4)
        self.assertEqual(sum(1 for o in validated["options"] if o["is_correct"]), 1)

    def test_non_dict_output_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload("not a dict", request=_make_request())

    def test_blank_stem_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(
                _valid_raw_payload(question_text="   "), request=_make_request()
            )

    def test_too_few_options_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(
                _valid_raw_payload(options=_valid_options()[:1]), request=_make_request()
            )

    def test_too_many_options_rejected(self):
        many = _valid_options() * 3  # 12 options, exceeds MAX_OPTIONS
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(_valid_raw_payload(options=many), request=_make_request())

    def test_duplicate_option_labels_rejected(self):
        options = _valid_options()
        options[1]["option_label"] = options[0]["option_label"]
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(_valid_raw_payload(options=options), request=_make_request())

    def test_duplicate_normalized_option_text_rejected(self):
        options = _valid_options()
        options[1]["option_text"] = "  " + options[0]["option_text"].upper() + "  "
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(_valid_raw_payload(options=options), request=_make_request())

    def test_missing_correct_answer_rejected(self):
        options = _valid_options()
        for opt in options:
            opt["is_correct"] = False
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(_valid_raw_payload(options=options), request=_make_request())

    def test_multiple_correct_answers_rejected_for_single(self):
        options = _valid_options()
        options[0]["is_correct"] = True  # now two correct options
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(_valid_raw_payload(options=options), request=_make_request())

    def test_correct_option_labels_referencing_unknown_label_rejected(self):
        options = _valid_options()
        payload = _valid_raw_payload(options=options, correct_option_labels=["Z"])
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(payload, request=_make_request())

    def test_duplicate_display_order_rejected(self):
        options = _valid_options()
        options[1]["display_order"] = options[0]["display_order"]
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(_valid_raw_payload(options=options), request=_make_request())

    def test_oversized_question_text_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(
                _valid_raw_payload(question_text="x" * 5000), request=_make_request()
            )

    def test_oversized_option_text_rejected(self):
        options = _valid_options()
        options[0]["option_text"] = "x" * 2000
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(_valid_raw_payload(options=options), request=_make_request())

    def test_non_boolean_is_correct_rejected(self):
        options = _valid_options()
        options[0]["is_correct"] = "yes"
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(_valid_raw_payload(options=options), request=_make_request())

    def test_missing_explanation_rejected(self):
        with self.assertRaises(CandidateValidationError):
            validate_generated_payload(
                _valid_raw_payload(explanation=""), request=_make_request()
            )

    def test_multiple_question_type_requires_matching_correct_count(self):
        options = _valid_options()
        options[0]["is_correct"] = True  # two correct now
        request = _make_request(question_type="multiple", select_count=2)
        validated = validate_generated_payload(_valid_raw_payload(options=options), request=request)
        self.assertEqual(validated["select_count"], 2)


# ===========================================================================
# compute_candidate_content_hash
# ===========================================================================

class TestContentHash(unittest.TestCase):
    def test_same_content_same_hash(self):
        request = _make_request()
        v1 = validate_generated_payload(_valid_raw_payload(), request=request)
        v2 = validate_generated_payload(_valid_raw_payload(), request=request)
        self.assertEqual(compute_candidate_content_hash(v1), compute_candidate_content_hash(v2))

    def test_option_order_does_not_change_hash(self):
        # Same (label, text, is_correct, display_order) tuples, but the
        # *array* order in the model output is reversed. Since
        # validate_generated_payload always sorts by display_order before
        # hashing, the resulting hash must be identical either way.
        request = _make_request()
        options_a = _valid_options()
        options_b = list(reversed(_valid_options()))
        v1 = validate_generated_payload(_valid_raw_payload(options=options_a), request=request)
        v2 = validate_generated_payload(_valid_raw_payload(options=options_b), request=request)
        self.assertEqual(compute_candidate_content_hash(v1), compute_candidate_content_hash(v2))

    def test_different_text_changes_hash(self):
        request = _make_request()
        v1 = validate_generated_payload(_valid_raw_payload(), request=request)
        options = _valid_options()
        options[0]["option_text"] = "Completely different wording here"
        v2 = validate_generated_payload(_valid_raw_payload(options=options), request=request)
        self.assertNotEqual(compute_candidate_content_hash(v1), compute_candidate_content_hash(v2))


# ===========================================================================
# QuestionCandidateRepository
# ===========================================================================

class TestQuestionCandidateRepository(unittest.TestCase):
    def test_certification_domain_exists_true(self):
        fake = FakeSupabase()
        fake.add_certification_domain("Salesforce Administrator", "Security and Access")
        repo = QuestionCandidateRepository(fake)
        self.assertTrue(
            repo.certification_domain_exists("Salesforce Administrator", "Security and Access")
        )

    def test_certification_domain_exists_false(self):
        fake = FakeSupabase()
        repo = QuestionCandidateRepository(fake)
        self.assertFalse(
            repo.certification_domain_exists("Salesforce Administrator", "Nonexistent Domain")
        )

    def test_find_by_content_hash_none_when_absent(self):
        fake = FakeSupabase()
        repo = QuestionCandidateRepository(fake)
        self.assertIsNone(repo.find_by_content_hash("Salesforce Administrator", "abc123"))

    def test_insert_candidate_error_propagates(self):
        fake = FakeSupabase()
        fake.set_insert_error("question_candidates", "constraint violation")
        repo = QuestionCandidateRepository(fake)
        with self.assertRaises(CandidatePersistenceError):
            repo.insert_candidate({"certification_exam_name": "x"})

    def test_insert_event_error_carries_candidate_id(self):
        fake = FakeSupabase()
        fake.set_insert_error("question_candidate_events", "fk violation")
        repo = QuestionCandidateRepository(fake)
        with self.assertRaises(CandidateProvenanceEventError) as ctx:
            repo.insert_event("candidate-1", event_type="created")
        self.assertEqual(ctx.exception.candidate_id, "candidate-1")


# ===========================================================================
# generate_and_persist_candidate — end to end
# ===========================================================================

class TestGenerateAndPersistCandidate(unittest.TestCase):
    def _fake_with_domain(self, request: GenerationRequest) -> FakeSupabase:
        fake = FakeSupabase()
        _seed_domain(fake, request)
        return fake

    def test_successful_single_candidate_persistence(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        self.assertFalse(result.deduplicated)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        self.assertEqual(fake.tables["question_candidates"][0]["id"], result.candidate_id)
        # Proves single-row atomicity: exactly one question_candidates
        # insert (options live in the same row via candidate_payload), plus
        # exactly one provenance event insert.
        candidate_inserts = [c for c in fake.insert_calls if c["table"] == "question_candidates"]
        self.assertEqual(len(candidate_inserts), 1)
        event_inserts = [c for c in fake.insert_calls if c["table"] == "question_candidate_events"]
        self.assertEqual(len(event_inserts), 1)

    def test_candidate_option_persistence(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        generate_and_persist_candidate(fake, provider, request)

        row = fake.tables["question_candidates"][0]
        options = row["candidate_payload"]["options"]
        self.assertEqual(len(options), 4)
        self.assertEqual(sum(1 for o in options if o["is_correct"]), 1)
        self.assertEqual([o["display_order"] for o in options], [1, 2, 3, 4])

    def test_provenance_persistence_and_retrievability(self):
        request = _make_request(generation_request_id="req-42")
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        generate_and_persist_candidate(fake, provider, request)

        row = fake.tables["question_candidates"][0]
        provenance = row["candidate_payload"]["provenance"]
        self.assertEqual(provenance["model_name"], request.model_name)
        self.assertEqual(provenance["prompt_template_id"], request.prompt_template_id)
        self.assertEqual(provenance["prompt_version"], request.prompt_version)
        self.assertEqual(provenance["generation_request_id"], "req-42")
        self.assertEqual(provenance["source_evidence"], request.source_evidence)
        self.assertIn("generated_at", provenance)
        self.assertEqual(row["metadata"]["domain"], request.domain)
        self.assertEqual(row["source_type"], "generated")

    def test_certification_domain_relationship_validated(self):
        request = _make_request()
        fake = FakeSupabase()  # no certification_domains row seeded
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        with self.assertRaises(CandidateValidationError):
            generate_and_persist_candidate(fake, provider, request)

        self.assertEqual(provider.calls, [])  # provider never called
        self.assertEqual(fake.tables["question_candidates"], [])

    def test_malformed_output_creates_no_candidate(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response({"question_text": "   "}))

        with self.assertRaises(CandidateValidationError):
            generate_and_persist_candidate(fake, provider, request)

        self.assertEqual(fake.tables["question_candidates"], [])
        self.assertEqual(fake.tables["question_candidate_events"], [])
        self.assertEqual(fake.rpc_calls, [])  # no audit enqueue attempted

    def test_duplicate_options_rejected_no_partial_rows(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        options = _valid_options()
        options[1]["option_label"] = options[0]["option_label"]
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload(options=options)))

        with self.assertRaises(CandidateValidationError):
            generate_and_persist_candidate(fake, provider, request)

        self.assertEqual(fake.tables["question_candidates"], [])

    def test_invalid_correct_answer_reference_rejected(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(
            response=_llm_response(_valid_raw_payload(correct_option_labels=["Q"]))
        )

        with self.assertRaises(CandidateValidationError):
            generate_and_persist_candidate(fake, provider, request)

        self.assertEqual(fake.tables["question_candidates"], [])

    def test_atomic_rollback_when_candidate_insert_fails(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        fake.set_insert_error("question_candidates", "simulated db failure")
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        with self.assertRaises(CandidatePersistenceError):
            generate_and_persist_candidate(fake, provider, request)

        self.assertEqual(fake.tables["question_candidates"], [])
        self.assertEqual(fake.tables["question_candidate_events"], [])
        self.assertEqual(fake.rpc_calls, [])

    def test_provenance_event_failure_after_candidate_insert_is_observable(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        fake.set_insert_error("question_candidate_events", "simulated event failure")
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        with self.assertRaises(CandidateProvenanceEventError) as ctx:
            generate_and_persist_candidate(fake, provider, request)

        # The candidate row itself is NOT rolled back: options + question
        # fields share one row, so the row that was written is already
        # complete and valid even though its provenance event failed.
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        self.assertEqual(ctx.exception.candidate_id, fake.tables["question_candidates"][0]["id"])

    def test_no_insertion_into_live_question_bank_tables(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        generate_and_persist_candidate(fake, provider, request)

        for forbidden in ("questions", "answer_options", "question_versions", "question_option_versions"):
            self.assertNotIn(forbidden, fake.tables)

    def test_audit_jobs_target_exact_candidate_and_snapshot(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        det_calls = fake.calls_for("enqueue_background_job_v1")
        det_call = next(c for c in det_calls if c["params"]["p_job_type"] == "deterministic_audit")
        llm_call = next(c for c in det_calls if c["params"]["p_job_type"] == "llm_audit")

        self.assertEqual(det_call["params"]["p_payload"]["target_candidate_id"], result.candidate_id)
        self.assertEqual(det_call["params"]["p_payload"]["question"], result.question_snapshot)
        self.assertEqual(llm_call["params"]["p_payload"]["target_candidate_id"], result.candidate_id)
        self.assertEqual(llm_call["params"]["p_payload"]["question"], result.question_snapshot)
        self.assertEqual(len(result.audit_outcomes), 2)
        self.assertTrue(all(o.enqueued for o in result.audit_outcomes))

    def test_retry_generation_is_idempotent_via_content_hash(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(
            responses=[_llm_response(_valid_raw_payload()), _llm_response(_valid_raw_payload())]
        )

        first = generate_and_persist_candidate(fake, provider, request)
        second = generate_and_persist_candidate(fake, provider, request)

        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.candidate_id, second.candidate_id)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)

    def test_deduplicated_retry_does_not_enqueue_duplicate_audits_by_default(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(
            responses=[_llm_response(_valid_raw_payload()), _llm_response(_valid_raw_payload())]
        )

        first = generate_and_persist_candidate(fake, provider, request)
        self.assertEqual(len(first.audit_outcomes), 2)
        enqueue_calls_after_first = len(fake.calls_for("enqueue_background_job_v1"))

        second = generate_and_persist_candidate(fake, provider, request)

        self.assertTrue(second.deduplicated)
        self.assertEqual(second.audit_outcomes, [])
        self.assertEqual(
            len(fake.calls_for("enqueue_background_job_v1")), enqueue_calls_after_first
        )
        self.assertEqual(len(fake.tables["question_candidates"]), 1)

    def test_deduplicated_retry_can_opt_into_reenqueuing_audits(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(
            responses=[_llm_response(_valid_raw_payload()), _llm_response(_valid_raw_payload())]
        )

        generate_and_persist_candidate(fake, provider, request)
        enqueue_calls_after_first = len(fake.calls_for("enqueue_background_job_v1"))

        second = generate_and_persist_candidate(
            fake, provider, request, initiate_audits_on_duplicate=True
        )

        self.assertTrue(second.deduplicated)
        self.assertEqual(len(second.audit_outcomes), 2)
        self.assertEqual(
            len(fake.calls_for("enqueue_background_job_v1")), enqueue_calls_after_first + 2
        )

    def test_audit_initiation_failure_is_observable_and_does_not_roll_back_candidate(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))
        fake.set_rpc_sequence(
            "enqueue_background_job_v1",
            [
                FakeQueryResult(data=[{"job_id": "det-job-1", "job_status": "pending"}]),
                FakeQueryResult(data=None, error="llm provider not configured"),
            ],
        )

        with self.assertRaises(AuditInitiationError) as ctx:
            generate_and_persist_candidate(fake, provider, request)

        # Candidate persistence is not rolled back for an audit failure.
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        candidate_id = ctx.exception.candidate_id
        outcomes = {o.job_type: o for o in ctx.exception.outcomes}
        self.assertTrue(outcomes["deterministic_audit"].enqueued)
        self.assertFalse(outcomes["llm_audit"].enqueued)

        # Retry only the failed job type — no new candidate is generated.
        fake.set_rpc_response(
            "enqueue_background_job_v1", [{"job_id": "llm-job-retry", "job_status": "pending"}]
        )
        persisted_row = fake.tables["question_candidates"][0]
        retry_snapshot = {
            "question_text": persisted_row["question_text"],
            "explanation": persisted_row["explanation"],
            "question_type": persisted_row["question_type"],
            "select_count": persisted_row["select_count"],
            "options": persisted_row["candidate_payload"]["options"],
            "certification_exam_name": persisted_row["certification_exam_name"],
            "domain": request.domain,
        }
        retry_outcomes = enqueue_candidate_audits(
            fake,
            candidate_id=candidate_id,
            question_snapshot=retry_snapshot,
            request=request,
            content_hash=persisted_row["content_hash"],
            job_types={"llm_audit"},
        )

        self.assertEqual(len(retry_outcomes), 1)
        self.assertTrue(retry_outcomes[0].enqueued)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)  # still just one candidate


# ===========================================================================
# retry_candidate_audits / load_audit_retry_context — public retry API
# ===========================================================================

class TestAuditRetryPublicApi(unittest.TestCase):
    def _fake_with_domain(self, request: GenerationRequest) -> FakeSupabase:
        fake = FakeSupabase()
        _seed_domain(fake, request)
        return fake

    def test_load_audit_retry_context_rebuilds_exact_snapshot_and_hash(self):
        request = _make_request(generation_request_id="req-77")
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        generated = generate_and_persist_candidate(fake, provider, request)
        context = load_audit_retry_context(fake, generated.candidate_id)

        self.assertIsInstance(context, AuditRetryContext)
        self.assertEqual(context.candidate_id, generated.candidate_id)
        self.assertEqual(context.content_hash, generated.content_hash)
        self.assertEqual(context.question_snapshot, generated.question_snapshot)
        self.assertEqual(context.request.certification_exam_name, request.certification_exam_name)
        self.assertEqual(context.request.domain, request.domain)
        self.assertEqual(context.request.model_name, request.model_name)
        self.assertEqual(context.request.generation_request_id, "req-77")
        self.assertEqual(context.request.source_evidence, request.source_evidence)

    def test_load_audit_retry_context_missing_candidate_raises(self):
        fake = FakeSupabase()
        with self.assertRaises(CandidatePersistenceError):
            load_audit_retry_context(fake, "nonexistent-id")

    def test_retry_candidate_audits_never_touches_question_candidates_table(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))
        generated = generate_and_persist_candidate(fake, provider, request, initiate_audits=False)

        insert_calls_before = len(fake.insert_calls)
        outcomes = retry_candidate_audits(
            fake, generated.candidate_id, created_by="ops@certbound.internal"
        )

        self.assertEqual({o.job_type for o in outcomes}, {"deterministic_audit", "llm_audit"})
        self.assertTrue(all(o.enqueued for o in outcomes))
        # No new row was inserted anywhere — retry is a pure read + enqueue.
        self.assertEqual(len(fake.insert_calls), insert_calls_before)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)

    def test_retry_candidate_audits_narrows_to_requested_job_types(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))
        generated = generate_and_persist_candidate(fake, provider, request, initiate_audits=False)

        outcomes = retry_candidate_audits(
            fake,
            generated.candidate_id,
            job_types={"llm_audit"},
            created_by="ops@certbound.internal",
        )

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].job_type, "llm_audit")
        self.assertEqual(len(fake.calls_for("enqueue_background_job_v1")), 1)

    def test_retry_created_by_override_reflects_retry_caller(self):
        request = _make_request(created_by="generator@certbound.internal")
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))
        generated = generate_and_persist_candidate(fake, provider, request, initiate_audits=False)

        retry_candidate_audits(
            fake,
            generated.candidate_id,
            job_types={"deterministic_audit"},
            created_by="retry-operator@certbound.internal",
        )

        call = fake.calls_for("enqueue_background_job_v1")[0]
        self.assertEqual(call["params"]["p_created_by"], "retry-operator@certbound.internal")

    def test_generation_request_from_candidate_row_round_trips_bounded_fields(self):
        request = _make_request(
            question_type="multiple",
            select_count=2,
            difficulty="hard",
            cognitive_level="analysis",
            concept_key="sharing-rules",
        )
        fake = self._fake_with_domain(request)
        options = _valid_options()
        options[0]["is_correct"] = True  # two correct options for select_count=2
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload(options=options)))

        generate_and_persist_candidate(fake, provider, request, initiate_audits=False)
        row = fake.tables["question_candidates"][0]
        rebuilt = generation_request_from_candidate_row(row)

        self.assertEqual(rebuilt.certification_exam_name, request.certification_exam_name)
        self.assertEqual(rebuilt.domain, request.domain)
        self.assertEqual(rebuilt.model_name, request.model_name)
        self.assertEqual(rebuilt.prompt_template_id, request.prompt_template_id)
        self.assertEqual(rebuilt.prompt_version, request.prompt_version)
        self.assertEqual(rebuilt.question_type, "multiple")
        self.assertEqual(rebuilt.select_count, 2)
        self.assertEqual(rebuilt.difficulty, "hard")
        self.assertEqual(rebuilt.cognitive_level, "analysis")
        self.assertEqual(rebuilt.concept_key, "sharing-rules")

    def test_question_snapshot_from_candidate_row_is_public_and_matches_generation(self):
        request = _make_request()
        fake = self._fake_with_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))
        generated = generate_and_persist_candidate(fake, provider, request, initiate_audits=False)

        row = fake.tables["question_candidates"][0]
        snapshot = question_snapshot_from_candidate_row(row)
        self.assertEqual(snapshot, generated.question_snapshot)


# ===========================================================================
# enqueue_candidate_audits — standalone unit tests
# ===========================================================================

class TestEnqueueCandidateAudits(unittest.TestCase):
    def _snapshot(self) -> dict:
        return {
            "question_text": "Q?",
            "explanation": "Because.",
            "question_type": "single",
            "select_count": 1,
            "options": _valid_options(),
            "certification_exam_name": "Salesforce Administrator",
            "domain": "Security and Access",
        }

    def test_enqueues_both_job_types_with_correct_params(self):
        fake = FakeSupabase()
        request = _make_request()
        outcomes = enqueue_candidate_audits(
            fake,
            candidate_id="cand-1",
            question_snapshot=self._snapshot(),
            request=request,
            content_hash="hash-1",
        )
        self.assertEqual({o.job_type for o in outcomes}, {"deterministic_audit", "llm_audit"})
        self.assertTrue(all(o.enqueued for o in outcomes))
        self.assertEqual(len(fake.calls_for("enqueue_background_job_v1")), 2)

    def test_partial_failure_raises_with_outcomes(self):
        fake = FakeSupabase()
        fake.set_rpc_sequence(
            "enqueue_background_job_v1",
            [
                FakeQueryResult(data=[{"job_id": "det-1", "job_status": "pending"}]),
                FakeQueryResult(data=None, error="boom"),
            ],
        )
        request = _make_request()
        with self.assertRaises(AuditInitiationError) as ctx:
            enqueue_candidate_audits(
                fake,
                candidate_id="cand-1",
                question_snapshot=self._snapshot(),
                request=request,
                content_hash="hash-1",
            )
        self.assertEqual(ctx.exception.candidate_id, "cand-1")
        self.assertEqual(len(ctx.exception.outcomes), 2)

    def test_unsupported_job_type_rejected(self):
        fake = FakeSupabase()
        request = _make_request()
        with self.assertRaises(CandidateValidationError):
            enqueue_candidate_audits(
                fake,
                candidate_id="cand-1",
                question_snapshot=self._snapshot(),
                request=request,
                content_hash="hash-1",
                job_types={"bogus_audit"},
            )
        self.assertEqual(fake.rpc_calls, [])

    def test_deterministic_only_retry_does_not_touch_llm_audit(self):
        fake = FakeSupabase()
        request = _make_request()
        outcomes = enqueue_candidate_audits(
            fake,
            candidate_id="cand-1",
            question_snapshot=self._snapshot(),
            request=request,
            content_hash="hash-1",
            job_types={"deterministic_audit"},
        )
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].job_type, "deterministic_audit")
        self.assertEqual(len(fake.calls_for("enqueue_background_job_v1")), 1)


# ===========================================================================
# Prompt builders — smoke tests
# ===========================================================================

class TestPromptBuilders(unittest.TestCase):
    def test_build_generation_prompt_includes_bounded_fields(self):
        request = _make_request()
        system_prompt, user_prompt = build_generation_prompt(request)
        self.assertIn("JSON", system_prompt)
        self.assertIn(request.certification_exam_name, user_prompt)
        self.assertIn(request.domain, user_prompt)

    def test_build_llm_audit_prompt_includes_question_snapshot(self):
        request = _make_request()
        snapshot = {
            "question_text": "Q?",
            "explanation": "Because.",
            "question_type": "single",
            "select_count": 1,
            "options": _valid_options(),
            "certification_exam_name": request.certification_exam_name,
            "domain": request.domain,
        }
        system_prompt, user_prompt = build_llm_audit_prompt(snapshot, request)
        self.assertIn("auditor", system_prompt.lower())
        self.assertIn(request.certification_exam_name, user_prompt)


def _make_anthropic_provider_config(**overrides) -> AnthropicProviderConfig:
    base = dict(
        api_key="test-key-not-logged",
        model="claude-sonnet-4-6",
        timeout=30.0,
        max_output_tokens=1024,
        max_retries=0,
        input_cost_per_mtok=3.0,
        output_cost_per_mtok=15.0,
    )
    base.update(overrides)
    return AnthropicProviderConfig(**base)


def _make_anthropic_message(*, text: str):
    return SimpleNamespace(
        id="msg_generation_test_001",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=120, output_tokens=80),
    )


def _make_anthropic_client(*messages):
    client = MagicMock()
    client.messages.create = MagicMock(side_effect=list(messages))
    return client


class TestGenerateAndPersistCandidateAnthropicProvider(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch("workers.anthropic_provider._is_transient_error", lambda _exc: False),
            patch("workers.anthropic_provider._is_auth_error", lambda _exc: False),
            patch("workers.anthropic_provider.time.sleep"),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def test_anthropic_provider_rejects_generation_payload_without_skip_flag(self):
        request = _make_request()
        payload = json.dumps(_valid_raw_payload())
        client = _make_anthropic_client(_make_anthropic_message(text=payload))
        provider = AnthropicAuditProvider(_make_anthropic_provider_config(), client=client)

        with self.assertRaises(LlmAuditValidationError):
            provider(
                model_name=request.model_name,
                system_prompt="Generate one question.",
                user_prompt="Certification context.",
                response_schema=GENERATION_RESPONSE_SCHEMA,
                metadata={
                    "certification_exam_name": request.certification_exam_name,
                    "domain": request.domain,
                    "prompt_template_id": request.prompt_template_id,
                    "generation_request_id": request.generation_request_id,
                },
            )

    def test_generate_and_persist_candidate_passes_skip_flag_to_anthropic_provider(self):
        request = _make_request()
        fake = FakeSupabase()
        _seed_domain(fake, request)
        payload = json.dumps(_valid_raw_payload())
        client = _make_anthropic_client(_make_anthropic_message(text=payload))
        provider = AnthropicAuditProvider(_make_anthropic_provider_config(), client=client)

        result = generate_and_persist_candidate(
            fake,
            provider,
            request,
            initiate_audits=False,
        )

        self.assertFalse(result.deduplicated)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        self.assertEqual(
            fake.tables["question_candidates"][0]["question_text"],
            _valid_raw_payload()["question_text"],
        )

    def test_generation_provider_call_includes_skip_legacy_validation_flag(self):
        request = _make_request()
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        generate_and_persist_candidate(fake, provider, request, initiate_audits=False)

        metadata = provider.calls[0]["metadata"]
        self.assertTrue(metadata.get(SKIP_LEGACY_LLM_AUDIT_VALIDATION_METADATA_KEY))


# ===========================================================================
# Platform App Builder generation vertical slice (PAB-EXP-05)
# ===========================================================================

_PAB_DOMAINS = tuple(
    domain.domain_name for domain in get_platform_app_builder_definition().domains
)


def _make_pab_request(**overrides) -> GenerationRequest:
    kwargs = dict(
        certification_exam_name=PAB_EXAM_NAME,
        domain="Salesforce Fundamentals",
        prompt_template_id="certbound-question-gen",
        prompt_version="v1.0.0",
        model_name="claude-3-5-sonnet-20241022",
        created_by="generation-service@certbound.internal",
        source_evidence={"resource_reference": "Salesforce Help: Standard Objects"},
    )
    kwargs.update(overrides)
    return GenerationRequest(**kwargs)


def _seed_pab_domain(fake: FakeSupabase, request: GenerationRequest) -> None:
    fake.add_certification_domain(request.certification_exam_name, request.domain)


class TestPlatformAppBuilderGeneration(unittest.TestCase):
    """Deterministic local smoke for the PAB generation vertical slice."""

    def _fake_with_pab_domain(self, request: GenerationRequest) -> FakeSupabase:
        fake = FakeSupabase()
        _seed_pab_domain(fake, request)
        return fake

    def test_all_five_registered_domains_are_accepted(self):
        for domain in _PAB_DOMAINS:
            with self.subTest(domain=domain):
                request = _make_pab_request(domain=domain)
                validate_generation_request(request)

    def test_administrator_domain_is_rejected_for_platform_app_builder(self):
        request = _make_pab_request(domain="Data and Analytics Management")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_unknown_certification_is_rejected(self):
        request = GenerationRequest(
            certification_exam_name="Salesforce Certified Data Cloud Consultant",
            domain="Any Domain",
            prompt_template_id="certbound-question-gen",
            prompt_version="v1.0.0",
            model_name="claude-test",
            created_by="tester@certbound.internal",
            source_evidence={"resource_reference": "Salesforce Help"},
        )
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_wrong_evidence_config_id_is_rejected(self):
        request = _make_pab_request(
            source_evidence={
                "evidence_config_id": "official-evidence-seed-v1",
                "resource_reference": "Salesforce Help",
            }
        )
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_valid_request_resolves_official_evidence_pab_v1(self):
        request = _make_pab_request()
        fake = self._fake_with_pab_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        row = fake.tables["question_candidates"][0]
        provenance = row["candidate_payload"]["provenance"]
        self.assertEqual(
            provenance["source_evidence"]["evidence_config_id"],
            PAB_EVIDENCE_CONFIG_ID,
        )
        self.assertEqual(row["certification_exam_name"], PAB_EXAM_NAME)
        self.assertEqual(row["metadata"]["domain"], request.domain)
        self.assertFalse(result.deduplicated)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        self.assertEqual(len(fake.tables["question_candidate_events"]), 1)

    def test_candidate_only_persistence_never_touches_live_question_tables(self):
        request = _make_pab_request(domain="User Interface")
        fake = self._fake_with_pab_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        generate_and_persist_candidate(fake, provider, request)

        for forbidden in (
            "questions",
            "answer_options",
            "question_versions",
            "question_option_versions",
        ):
            self.assertNotIn(forbidden, fake.tables)

    def test_exact_duplicate_handling_unchanged(self):
        request = _make_pab_request(domain="App Deployment")
        fake = self._fake_with_pab_domain(request)
        provider = FakeLlmProvider(
            responses=[_llm_response(_valid_raw_payload()), _llm_response(_valid_raw_payload())]
        )

        first = generate_and_persist_candidate(fake, provider, request)
        second = generate_and_persist_candidate(fake, provider, request)

        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.candidate_id, second.candidate_id)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)

    def test_audit_enqueue_carries_immutable_candidate_snapshot(self):
        request = _make_pab_request(domain="Business Logic and Process Automation")
        fake = self._fake_with_pab_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        det_call, llm_call = fake.calls_for("enqueue_background_job_v1")
        self.assertEqual(
            det_call["params"]["p_payload"]["target_candidate_id"],
            result.candidate_id,
        )
        self.assertEqual(
            det_call["params"]["p_payload"]["question"],
            result.question_snapshot,
        )
        self.assertEqual(
            llm_call["params"]["p_payload"]["question"],
            result.question_snapshot,
        )
        self.assertEqual(
            result.question_snapshot["certification_exam_name"],
            PAB_EXAM_NAME,
        )
        self.assertEqual(result.question_snapshot["domain"], request.domain)


# ===========================================================================
# Business Analyst generation vertical slice (BA-EXP-04)
# ===========================================================================

_BA_DOMAINS = tuple(
    domain.domain_name for domain in get_business_analyst_definition().domains
)


def _make_ba_request(**overrides) -> GenerationRequest:
    kwargs = dict(
        certification_exam_name=BA_EXAM_NAME,
        domain="Customer Discovery",
        prompt_template_id="certbound-question-gen",
        prompt_version="v1.0.0",
        model_name="claude-3-5-sonnet-20241022",
        created_by="generation-service@certbound.internal",
        source_evidence={"resource_reference": "Salesforce Help: Stakeholders"},
    )
    kwargs.update(overrides)
    return GenerationRequest(**kwargs)


def _seed_ba_domain(fake: FakeSupabase, request: GenerationRequest) -> None:
    fake.add_certification_domain(request.certification_exam_name, request.domain)


class TestBusinessAnalystGeneration(unittest.TestCase):
    """Deterministic local smoke for the BA generation vertical slice."""

    def _fake_with_ba_domain(self, request: GenerationRequest) -> FakeSupabase:
        fake = FakeSupabase()
        _seed_ba_domain(fake, request)
        return fake

    def test_all_six_registered_domains_are_accepted(self):
        for domain in _BA_DOMAINS:
            with self.subTest(domain=domain):
                request = _make_ba_request(domain=domain)
                validate_generation_request(request)

    def test_administrator_domain_is_rejected_for_business_analyst(self):
        request = _make_ba_request(domain="Data and Analytics Management")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_platform_app_builder_domain_is_rejected_for_business_analyst(self):
        request = _make_ba_request(domain="User Interface")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_wrong_evidence_config_id_is_rejected(self):
        request = _make_ba_request(
            source_evidence={
                "evidence_config_id": "official-evidence-seed-v1",
                "resource_reference": "Salesforce Help",
            }
        )
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_valid_request_resolves_official_evidence_ba_v1(self):
        request = _make_ba_request(domain="Requirements")
        fake = self._fake_with_ba_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        row = fake.tables["question_candidates"][0]
        provenance = row["candidate_payload"]["provenance"]
        self.assertEqual(
            provenance["source_evidence"]["evidence_config_id"],
            BA_EVIDENCE_CONFIG_ID,
        )
        self.assertEqual(row["certification_exam_name"], BA_EXAM_NAME)
        self.assertEqual(row["metadata"]["domain"], request.domain)
        self.assertFalse(result.deduplicated)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        self.assertEqual(len(fake.tables["question_candidate_events"]), 1)

    def test_candidate_only_persistence_never_touches_live_question_tables(self):
        request = _make_ba_request(domain="User Stories")
        fake = self._fake_with_ba_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        generate_and_persist_candidate(fake, provider, request)

        for forbidden in _FORBIDDEN_TABLES:
            self.assertNotIn(forbidden, fake.tables)

    def test_exact_duplicate_handling_unchanged(self):
        request = _make_ba_request(domain="Business Process Mapping")
        fake = self._fake_with_ba_domain(request)
        provider = FakeLlmProvider(
            responses=[_llm_response(_valid_raw_payload()), _llm_response(_valid_raw_payload())]
        )

        first = generate_and_persist_candidate(fake, provider, request)
        second = generate_and_persist_candidate(fake, provider, request)

        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.candidate_id, second.candidate_id)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)

    def test_audit_enqueue_carries_immutable_candidate_snapshot(self):
        request = _make_ba_request(domain="User Acceptance")
        fake = self._fake_with_ba_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        det_call, llm_call = fake.calls_for("enqueue_background_job_v1")
        self.assertEqual(
            det_call["params"]["p_payload"]["target_candidate_id"],
            result.candidate_id,
        )
        self.assertEqual(
            det_call["params"]["p_payload"]["question"],
            result.question_snapshot,
        )
        self.assertEqual(
            llm_call["params"]["p_payload"]["question"],
            result.question_snapshot,
        )
        self.assertEqual(
            result.question_snapshot["certification_exam_name"],
            BA_EXAM_NAME,
        )
        self.assertEqual(result.question_snapshot["domain"], request.domain)

    def test_platform_app_builder_generation_unchanged_after_ba_registration(self):
        request = _make_pab_request(domain="App Deployment")
        fake = FakeSupabase()
        _seed_pab_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        provenance = fake.tables["question_candidates"][0]["candidate_payload"]["provenance"]
        self.assertEqual(
            provenance["source_evidence"]["evidence_config_id"],
            PAB_EVIDENCE_CONFIG_ID,
        )
        self.assertFalse(result.deduplicated)


# ===========================================================================
# Sales Cloud Consultant generation vertical slice (SCC-EXP-06)
# ===========================================================================

_SCC_DOMAINS = tuple(
    domain.domain_name for domain in get_sales_cloud_consultant_definition().domains
)


def _make_scc_request(**overrides) -> GenerationRequest:
    kwargs = dict(
        certification_exam_name=SCC_EXAM_NAME,
        domain="Sales Lifecycle",
        prompt_template_id="certbound-question-gen",
        prompt_version="v1.0.0",
        model_name="claude-3-5-sonnet-20241022",
        created_by="generation-service@certbound.internal",
        source_evidence={"resource_reference": "Salesforce Help: Opportunities"},
    )
    kwargs.update(overrides)
    return GenerationRequest(**kwargs)


def _seed_scc_domain(fake: FakeSupabase, request: GenerationRequest) -> None:
    fake.add_certification_domain(request.certification_exam_name, request.domain)


class TestSalesCloudConsultantGeneration(unittest.TestCase):
    """Deterministic local smoke for the SCC generation vertical slice."""

    def _fake_with_scc_domain(self, request: GenerationRequest) -> FakeSupabase:
        fake = FakeSupabase()
        _seed_scc_domain(fake, request)
        return fake

    def test_sales_con_201_resolves_to_canonical_certification(self):
        canonical = validate_generation_request_certification(
            certification_exam_name="Sales-Con-201",
            domain="Sales Lifecycle",
        )
        self.assertEqual(canonical, SCC_EXAM_NAME)

    def test_existing_scc_aliases_resolve(self):
        for alias in (
            "Sales-Con-201",
            "sales-con-201",
            "Salesforce Sales Cloud Consultant",
            "scc",
        ):
            with self.subTest(alias=alias):
                canonical = validate_generation_request_certification(
                    certification_exam_name=alias,
                    domain="Data Management",
                )
                self.assertEqual(canonical, SCC_EXAM_NAME)

    def test_all_five_registered_domains_are_accepted(self):
        for domain in _SCC_DOMAINS:
            with self.subTest(domain=domain):
                request = _make_scc_request(domain=domain)
                validate_generation_request(request)

    def test_blank_domain_is_rejected(self):
        request = _make_scc_request(domain="   ")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_administrator_domain_is_rejected_for_sales_cloud_consultant(self):
        request = _make_scc_request(domain="Data and Analytics Management")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_platform_app_builder_domain_is_rejected_for_sales_cloud_consultant(self):
        request = _make_scc_request(domain="User Interface")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_business_analyst_domain_is_rejected_for_sales_cloud_consultant(self):
        request = _make_scc_request(domain="Requirements")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_arbitrary_domain_is_rejected(self):
        request = _make_scc_request(domain="Totally Made Up Domain")
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_wrong_evidence_config_id_is_rejected(self):
        request = _make_scc_request(
            source_evidence={
                "evidence_config_id": "official-evidence-seed-v1",
                "resource_reference": "Salesforce Help",
            }
        )
        with self.assertRaises(CandidateValidationError):
            validate_generation_request(request)

    def test_valid_request_resolves_official_evidence_scc_v1(self):
        request = _make_scc_request(domain="Consulting & Implementation Strategies")
        fake = self._fake_with_scc_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        row = fake.tables["question_candidates"][0]
        provenance = row["candidate_payload"]["provenance"]
        self.assertEqual(
            provenance["source_evidence"]["evidence_config_id"],
            SCC_EVIDENCE_CONFIG_ID,
        )
        self.assertEqual(row["certification_exam_name"], SCC_EXAM_NAME)
        self.assertEqual(row["metadata"]["domain"], request.domain)
        self.assertFalse(result.deduplicated)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)
        self.assertEqual(len(fake.tables["question_candidate_events"]), 1)

    def test_candidate_only_persistence_never_touches_live_question_tables(self):
        request = _make_scc_request(domain="Predictive and Generative AI")
        fake = self._fake_with_scc_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        generate_and_persist_candidate(fake, provider, request)

        for forbidden in _FORBIDDEN_TABLES:
            self.assertNotIn(forbidden, fake.tables)

    def test_exact_duplicate_handling_unchanged(self):
        request = _make_scc_request(domain="Practical Application of Sales Cloud Expertise")
        fake = self._fake_with_scc_domain(request)
        provider = FakeLlmProvider(
            responses=[_llm_response(_valid_raw_payload()), _llm_response(_valid_raw_payload())]
        )

        first = generate_and_persist_candidate(fake, provider, request)
        second = generate_and_persist_candidate(fake, provider, request)

        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.candidate_id, second.candidate_id)
        self.assertEqual(len(fake.tables["question_candidates"]), 1)

    def test_audit_enqueue_carries_immutable_candidate_snapshot(self):
        request = _make_scc_request(domain="Data Management")
        fake = self._fake_with_scc_domain(request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        det_call, llm_call = fake.calls_for("enqueue_background_job_v1")
        self.assertEqual(
            det_call["params"]["p_payload"]["target_candidate_id"],
            result.candidate_id,
        )
        self.assertEqual(
            det_call["params"]["p_payload"]["question"],
            result.question_snapshot,
        )
        self.assertEqual(
            llm_call["params"]["p_payload"]["question"],
            result.question_snapshot,
        )
        self.assertEqual(
            result.question_snapshot["certification_exam_name"],
            SCC_EXAM_NAME,
        )
        self.assertEqual(result.question_snapshot["domain"], request.domain)

    def test_administrator_generation_unchanged_after_scc_registration(self):
        request = _make_request()
        fake = FakeSupabase()
        _seed_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        provenance = fake.tables["question_candidates"][0]["candidate_payload"]["provenance"]
        self.assertNotIn("evidence_config_id", provenance["source_evidence"])
        self.assertFalse(result.deduplicated)

    def test_business_analyst_generation_unchanged_after_scc_registration(self):
        request = _make_ba_request(domain="User Stories")
        fake = FakeSupabase()
        _seed_ba_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        provenance = fake.tables["question_candidates"][0]["candidate_payload"]["provenance"]
        self.assertEqual(
            provenance["source_evidence"]["evidence_config_id"],
            BA_EVIDENCE_CONFIG_ID,
        )
        self.assertFalse(result.deduplicated)

    def test_platform_app_builder_generation_unchanged_after_scc_registration(self):
        request = _make_pab_request(domain="App Deployment")
        fake = FakeSupabase()
        _seed_pab_domain(fake, request)
        provider = FakeLlmProvider(response=_llm_response(_valid_raw_payload()))

        result = generate_and_persist_candidate(fake, provider, request)

        provenance = fake.tables["question_candidates"][0]["candidate_payload"]["provenance"]
        self.assertEqual(
            provenance["source_evidence"]["evidence_config_id"],
            PAB_EVIDENCE_CONFIG_ID,
        )
        self.assertFalse(result.deduplicated)


if __name__ == "__main__":
    unittest.main()
