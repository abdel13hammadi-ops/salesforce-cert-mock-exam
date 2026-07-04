"""Tests for V48 authoritative hybrid replay embedding text resolution."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock
from typing import Any, Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.ai_quality_audit_evidence import _build_question_query
from workers.ai_quality_audit_hybrid_replay import build_replay_candidate_identity
from workers.ai_quality_audit_shadow import classify_question_shadow_from_replay_record
from workers.embedding_cache import build_cache_identity, hash_embedding_input
from workers.embedding_providers import OPENAI_PROVIDER_NAME
from workers.v48_hybrid_replay_authoritative_text import (
    DEFAULT_VALIDATED_MODEL_VERSION,
    FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
    FAILURE_STAGE_AUTHORITATIVE_MATCHING,
    FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION,
    FAILURE_STAGE_AUTHORITATIVE_RESOURCE_RESOLUTION,
    MAX_AUTHORITATIVE_CANDIDATE_CHUNKS,
    AuthoritativeEmbeddingTextError,
    AuthoritativeEmbeddingTextResolver,
    assert_execute_resolver_is_authoritative,
    build_fixture_embedding_text_resolver,
    build_supabase_authoritative_embedding_text_resolver,
    compute_authoritative_content_hash,
    normalize_embedding_input_text,
    sanitize_error_detail,
    validate_authoritative_candidate_chunk_limit,
    _classify_supabase_loader_message,
    _match_live_candidate_chunk_text,
    _selected_semantic_review_bindings,
    _wrap_context_loader_error,
    _wrap_evidence_loader_error,
    CandidateTextBinding,
)
from workers.ai_quality_audit_context import AiQualityAuditContextError
from workers.ai_quality_audit_evidence import AiQualityAuditEvidenceError

_FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "v48_retrieval_replay_v1.json",
)


def _load_fixture() -> dict[str, Any]:
    import json

    with open(_FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


class TestAuthoritativeEmbeddingTextHelpers(unittest.TestCase):
    def test_normalize_and_hash_are_deterministic(self):
        first = compute_authoritative_content_hash("  Example   query text  ")
        second = compute_authoritative_content_hash("Example query text")
        self.assertEqual(first, second)
        self.assertEqual(normalize_embedding_input_text(" a  b "), "a b")

    def test_changed_content_changes_cache_identity(self):
        shared = {
            "embedding_provider_name": OPENAI_PROVIDER_NAME,
            "embedding_model_name": "text-embedding-3-small",
            "embedding_model_version": DEFAULT_VALIDATED_MODEL_VERSION,
            "embedding_dimensions": 3,
        }
        first = build_cache_identity(
            text="Authority query A",
            content_scope="query",
            **shared,
        )
        second = build_cache_identity(
            text="Authority query B",
            content_scope="query",
            **shared,
        )
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.content_hash, hash_embedding_input("Authority query A"))

    def test_empty_text_fails_closed(self):
        with self.assertRaises(AuthoritativeEmbeddingTextError):
            compute_authoritative_content_hash("   ")


class TestAuthoritativeEmbeddingTextResolver(unittest.TestCase):
    def test_from_resolved_texts_success(self):
        fixture = _load_fixture()
        record = fixture["questions"][0]
        question_version_id = str(record["question_version_id"])
        shadow = classify_question_shadow_from_replay_record(record)
        candidate = shadow["candidates"][0]
        identity = build_replay_candidate_identity(
            question_version_id=question_version_id,
            candidate_position=0,
            title=str(candidate["title"]),
            resource_type=str(candidate.get("resource_type") or ""),
        )
        resolver = AuthoritativeEmbeddingTextResolver.from_resolved_texts(
            fixture,
            candidate_limit=2,
            question_text_by_id={
                question_version_id: "Question stem with domain context",
            },
            candidate_text_by_identity={
                identity: "Official resource chunk body text",
            },
        )
        self.assertTrue(resolver.authoritative_text_used)
        self.assertEqual(
            resolver.resolve_question_embedding_text(question_version_id),
            "Question stem with domain context",
        )
        self.assertEqual(
            resolver.resolve_candidate_embedding_text(identity),
            "Official resource chunk body text",
        )
        self.assertTrue(resolver.replay_content_set_hash)

    def test_missing_question_text_in_prepare_fails_for_empty_value(self):
        fixture = _load_fixture()
        with self.assertRaises(AuthoritativeEmbeddingTextError):
            AuthoritativeEmbeddingTextResolver.from_resolved_texts(
                fixture,
                candidate_limit=2,
                question_text_by_id={"1f181e6e-28dc-41d9-a31b-5512b5948f7d": "   "},
                candidate_text_by_identity={},
            )

    def test_execute_rejects_fixture_resolver(self):
        fixture = _load_fixture()
        with self.assertRaises(AuthoritativeEmbeddingTextError):
            assert_execute_resolver_is_authoritative(
                build_fixture_embedding_text_resolver(fixture)
            )

    def test_duplicate_candidate_resolution_fails_closed(self):
        fixture = _load_fixture()
        record = next(
            question
            for question in fixture["questions"]
            if classify_question_shadow_from_replay_record(question)["confidence_class"]
            == "semantic_review_candidate"
        )
        binding = _selected_semantic_review_bindings(record, candidate_limit=2)[0]
        resolver = AuthoritativeEmbeddingTextResolver(
            fixture,
            candidate_limit=2,
            question_text_loader=lambda _qid: "Question text",
            candidate_pool_loader=lambda _qid: (
                [
                    {
                        "resource_chunk_id": "11111111-1111-1111-1111-111111111111",
                        "resource_id": "22222222-2222-2222-2222-222222222222",
                        "chunk_text": "Chunk A",
                        "title": binding.title,
                        "resource_type": binding.resource_type,
                        "chunk_index": 0,
                        "certification_exam_name": "test",
                        "content_hash": "abc",
                    },
                    {
                        "resource_chunk_id": "33333333-3333-3333-3333-333333333333",
                        "resource_id": "44444444-4444-4444-4444-444444444444",
                        "chunk_text": "Chunk B",
                        "title": binding.title,
                        "resource_type": binding.resource_type,
                        "chunk_index": 1,
                        "certification_exam_name": "test",
                        "content_hash": "def",
                    },
                ],
                {},
                {
                    "question_text": "Question text",
                    "domain_name": "Domain",
                    "options": [],
                },
            ),
        )
        with self.assertRaises(AuthoritativeEmbeddingTextError):
            resolver.prepare()


class TestAuthoritativeCandidateMatching(unittest.TestCase):
    def test_missing_chunk_match_fails_closed(self):
        fixture = _load_fixture()
        record = next(
            question
            for question in fixture["questions"]
            if classify_question_shadow_from_replay_record(question)["confidence_class"]
            == "semantic_review_candidate"
        )
        binding = _selected_semantic_review_bindings(record, candidate_limit=1)[0]
        blind_context = {
            "question_text": "Question text",
            "domain_name": "Domain",
            "options": [],
        }
        with self.assertRaises(AuthoritativeEmbeddingTextError):
            _match_live_candidate_chunk_text(
                binding=binding,
                question_record=record,
                blind_context=blind_context,
                live_candidates=[],
                resource_by_id={},
            )

    def test_query_text_source_matches_bm25_query_builder(self):
        blind_context = {
            "question_text": "What is a lookup relationship?",
            "domain_name": "Data Modeling",
            "options": [{"option_label": "A", "option_text": "Ignore", "display_order": 1}],
        }
        query_text = _build_question_query(blind_context)
        self.assertIn("lookup relationship", query_text.lower())
        self.assertNotIn("Ignore", query_text)


class TestAuthoritativeLoaderErrorClassification(unittest.TestCase):
    def test_context_loader_error_is_wrapped_with_question_stage(self):
        wrapped = _wrap_context_loader_error(
            AiQualityAuditContextError("RPC 'get_question_version_blind_context_v1' returned no rows")
        )
        self.assertEqual(wrapped.failure_stage, FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION)
        self.assertEqual(wrapped.error_code, "authoritative_question_context_malformed")
        self.assertEqual(wrapped.error_type, "rpc_response")

    def test_unauthorized_context_error_uses_supabase_auth_code(self):
        wrapped = _wrap_context_loader_error(
            AiQualityAuditContextError("RPC call failed: 401 unauthorized")
        )
        self.assertEqual(wrapped.error_code, "supabase_unauthorized")
        self.assertEqual(wrapped.error_type, "supabase_auth")

    def test_active_resource_loader_error_uses_resource_stage(self):
        wrapped = _wrap_evidence_loader_error(
            AiQualityAuditEvidenceError("official_resources lookup failed: connection reset"),
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_RESOURCE_RESOLUTION,
            default_error_code="authoritative_active_resource_failed",
        )
        self.assertEqual(wrapped.failure_stage, FAILURE_STAGE_AUTHORITATIVE_RESOURCE_RESOLUTION)
        self.assertEqual(wrapped.error_code, "supabase_transport_failed")
        self.assertEqual(wrapped.error_type, "network_transport")

    def test_candidate_chunk_loader_error_uses_chunk_stage(self):
        wrapped = _wrap_evidence_loader_error(
            AiQualityAuditEvidenceError("RPC 'list_candidate_chunks_v1' row 0 is malformed"),
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
            default_error_code="authoritative_candidate_chunk_rpc_failed",
        )
        self.assertEqual(wrapped.failure_stage, FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION)
        self.assertEqual(wrapped.error_code, "authoritative_candidate_chunk_malformed")
        self.assertEqual(wrapped.error_type, "rpc_response")

    def test_supabase_resolver_wraps_context_loader_failures(self):
        fixture = _load_fixture()
        record = next(
            question
            for question in fixture["questions"]
            if classify_question_shadow_from_replay_record(question)["confidence_class"]
            == "semantic_review_candidate"
        )
        question_version_id = str(record["question_version_id"])

        class _Client:
            def rpc(self, *_args, **_kwargs):
                raise RuntimeError("should not be called directly")

        resolver = build_supabase_authoritative_embedding_text_resolver(
            _Client(),
            fixture,
            candidate_limit=2,
        )
        with mock.patch(
            "workers.v48_hybrid_replay_authoritative_text.load_blind_audit_context",
            side_effect=AiQualityAuditContextError("RPC call failed: timeout"),
        ):
            with self.assertRaises(AuthoritativeEmbeddingTextError) as ctx:
                resolver.prepare()
        self.assertEqual(ctx.exception.failure_stage, FAILURE_STAGE_AUTHORITATIVE_QUESTION_RESOLUTION)
        self.assertEqual(ctx.exception.error_code, "supabase_transport_failed")

    def test_supabase_resolver_wraps_active_resource_failures(self):
        fixture = _load_fixture()
        record = next(
            question
            for question in fixture["questions"]
            if classify_question_shadow_from_replay_record(question)["confidence_class"]
            == "semantic_review_candidate"
        )
        question_version_id = str(record["question_version_id"])
        blind_context = {
            "question_version_id": question_version_id,
            "certification_exam_name": "Salesforce Certified Administrator",
            "question_text": "Sample stem",
            "domain_name": "Configuration",
            "options": [],
        }

        class _Client:
            def rpc(self, *_args, **_kwargs):
                raise RuntimeError("should not be called directly")

        resolver = build_supabase_authoritative_embedding_text_resolver(
            _Client(),
            fixture,
            candidate_limit=2,
        )
        with mock.patch(
            "workers.v48_hybrid_replay_authoritative_text.load_blind_audit_context",
            return_value=blind_context,
        ), mock.patch(
            "workers.v48_hybrid_replay_authoritative_text._load_active_resources",
            side_effect=AiQualityAuditEvidenceError("official_resources lookup failed: forbidden"),
        ):
            with self.assertRaises(AuthoritativeEmbeddingTextError) as ctx:
                resolver.prepare()
        self.assertEqual(ctx.exception.failure_stage, FAILURE_STAGE_AUTHORITATIVE_RESOURCE_RESOLUTION)
        self.assertEqual(ctx.exception.error_code, "supabase_unauthorized")

    def test_missing_match_reports_specific_error_code(self):
        fixture = _load_fixture()
        record = next(
            question
            for question in fixture["questions"]
            if classify_question_shadow_from_replay_record(question)["confidence_class"]
            == "semantic_review_candidate"
        )
        binding = _selected_semantic_review_bindings(record, candidate_limit=1)[0]
        with self.assertRaises(AuthoritativeEmbeddingTextError) as ctx:
            _match_live_candidate_chunk_text(
                binding=binding,
                question_record=record,
                blind_context={
                    "question_text": "Question text",
                    "domain_name": "Domain",
                    "options": [],
                },
                live_candidates=[],
                resource_by_id={},
            )
        self.assertEqual(ctx.exception.error_code, "authoritative_candidate_not_found")
        self.assertEqual(ctx.exception.failure_stage, FAILURE_STAGE_AUTHORITATIVE_MATCHING)

    def test_sanitize_error_detail_redacts_secrets(self):
        detail = sanitize_error_detail(
            "transport exploded with sk-secret-key-not-real at https://user:pass@example.supabase.co"
        )
        self.assertNotIn("sk-secret-key-not-real", detail)
        self.assertNotIn("pass@", detail)
        self.assertIn("[redacted]", detail)


class TestAuthoritativeCandidateChunkLimit(unittest.TestCase):
    def test_default_limit_matches_rpc_contract(self):
        self.assertEqual(MAX_AUTHORITATIVE_CANDIDATE_CHUNKS, 200)
        self.assertEqual(
            validate_authoritative_candidate_chunk_limit(
                MAX_AUTHORITATIVE_CANDIDATE_CHUNKS
            ),
            200,
        )

    def test_limit_below_one_fails_before_rpc(self):
        with self.assertRaises(AuthoritativeEmbeddingTextError) as ctx:
            validate_authoritative_candidate_chunk_limit(0)
        self.assertEqual(ctx.exception.error_code, "authoritative_chunk_request_invalid")
        self.assertEqual(ctx.exception.error_type, "input_validation")
        self.assertEqual(ctx.exception.failure_stage, FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION)

    def test_limit_above_two_hundred_fails_before_rpc(self):
        with self.assertRaises(AuthoritativeEmbeddingTextError) as ctx:
            validate_authoritative_candidate_chunk_limit(250)
        self.assertEqual(ctx.exception.error_code, "authoritative_chunk_request_invalid")
        self.assertEqual(ctx.exception.error_type, "input_validation")
        self.assertIn("250", ctx.exception.error_detail)

    def test_resolver_requests_at_most_two_hundred_chunks(self):
        fixture = _load_fixture()
        record = next(
            question
            for question in fixture["questions"]
            if classify_question_shadow_from_replay_record(question)["confidence_class"]
            == "semantic_review_candidate"
        )
        question_version_id = str(record["question_version_id"])
        blind_context = {
            "question_version_id": question_version_id,
            "certification_exam_name": "Salesforce Certified Administrator",
            "question_text": "Sample stem",
            "domain_name": "Configuration",
            "options": [],
        }
        captured: dict[str, int] = {}

        def _capture_list_candidate_chunks(_client, **kwargs: Any) -> list[dict[str, Any]]:
            captured["max_chunks"] = int(kwargs["max_chunks"])
            return []

        class _Client:
            def rpc(self, *_args, **_kwargs):
                raise RuntimeError("should not be called directly")

        resolver = build_supabase_authoritative_embedding_text_resolver(
            _Client(),
            fixture,
            candidate_limit=2,
        )
        with mock.patch(
            "workers.v48_hybrid_replay_authoritative_text.load_blind_audit_context",
            return_value=blind_context,
        ), mock.patch(
            "workers.v48_hybrid_replay_authoritative_text._load_active_resources",
            return_value=[{"id": "22222222-2222-2222-2222-222222222222", "title": "t", "metadata": {}, "resource_type": "doc"}],
        ), mock.patch(
            "workers.v48_hybrid_replay_authoritative_text._list_candidate_chunks",
            side_effect=_capture_list_candidate_chunks,
        ):
            with self.assertRaises(AuthoritativeEmbeddingTextError):
                resolver.prepare()
        self.assertEqual(captured.get("max_chunks"), 200)

    def test_postgresql_22023_is_rpc_validation_not_transport(self):
        message = (
            "RPC 'list_audit_candidate_resource_chunks_v1' call failed: "
            "22023: p_max_chunks must be between 1 and 200, got: 250"
        )
        code, error_type = _classify_supabase_loader_message(message)
        self.assertEqual(code, "rpc_parameter_invalid")
        self.assertEqual(error_type, "rpc_validation")
        wrapped = _wrap_evidence_loader_error(
            AiQualityAuditEvidenceError(message),
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
            default_error_code="authoritative_candidate_chunk_rpc_failed",
        )
        self.assertEqual(wrapped.error_code, "rpc_parameter_invalid")
        self.assertEqual(wrapped.error_type, "rpc_validation")
        self.assertEqual(wrapped.failure_stage, FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION)
        self.assertNotIn("network", wrapped.error_type)

    def test_connection_failure_remains_transport(self):
        wrapped = _wrap_evidence_loader_error(
            AiQualityAuditEvidenceError(
                "RPC 'list_audit_candidate_resource_chunks_v1' call failed: connection reset"
            ),
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
            default_error_code="authoritative_candidate_chunk_rpc_failed",
        )
        self.assertEqual(wrapped.error_code, "supabase_transport_failed")
        self.assertEqual(wrapped.error_type, "network_transport")

    def test_rpc_validation_detail_is_sanitized_without_raw_dict(self):
        raw = (
            "RPC 'list_audit_candidate_resource_chunks_v1' rejected: "
            "{'code': '22023', 'details': 'secret payload', 'hint': None}"
        )
        wrapped = _wrap_evidence_loader_error(
            AiQualityAuditEvidenceError(raw),
            failure_stage=FAILURE_STAGE_AUTHORITATIVE_CHUNK_RESOLUTION,
            default_error_code="authoritative_candidate_chunk_rpc_failed",
        )
        self.assertEqual(wrapped.error_type, "rpc_validation")
        self.assertNotIn("secret payload", wrapped.error_detail)
        self.assertLessEqual(len(wrapped.error_detail), 240)


if __name__ == "__main__":
    unittest.main()
