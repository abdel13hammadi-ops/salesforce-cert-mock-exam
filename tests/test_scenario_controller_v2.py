"""Focused tests for the isolated Engine V2 learner controller
(``utils.scenario_controller_v2``).

Uses deterministic fakes only for unit tests (A-AJ), reusing
``tests.test_scenario_orchestration_v2.FakeOrchestrationPersistence`` (the
same already-validated CAS/idempotency in-memory fake the orchestration
layer's own tests use) as the injected ``persistence`` override, so these
tests never need a real Supabase/PostgREST client. A disposable, real
``postgrest-py`` + real Postgres integration test
(``TestScenarioControllerV2DisposablePostgrestSmoke``) runs only when Docker
is available (reusing the exact bootstrap already validated by
``tests.test_scenario_supabase_port_v2.TestSupabasePortDisposablePostgrestSmoke``)
and never touches production.
"""

from __future__ import annotations

import copy
import os
import sys
import unittest
import uuid
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.scenario_engine_v2 import build_scenario_content_v2
from utils.scenario_controller_v2 import (
    LearnerIdentityContextV2,
    LearnerScenarioControllerResultV2,
    LearnerScenarioControllerStateV2,
    ScenarioControllerV2AttemptNotFoundError,
    ScenarioControllerV2CorruptedAttemptError,
    ScenarioControllerV2DecisionConflictError,
    ScenarioControllerV2InvalidIdentityError,
    ScenarioControllerV2InvalidRequestError,
    ScenarioControllerV2PersistenceUnavailableError,
    ScenarioControllerV2ScenarioUnavailableError,
    ScenarioControllerV2StaleSessionError,
    ScenarioControllerV2TerminalAttemptError,
    ScenarioControllerV2UnauthenticatedError,
    ScenarioControllerV2UnexpectedInternalError,
    resume_learner_scenario_v2,
    serialize_learner_controller_result_v2,
    start_or_resume_learner_scenario_v2,
    submit_learner_scenario_choice_v2,
)
from tests.test_scenario_orchestration_v2 import (
    _EMAIL,
    _SCENARIO_VERSION_ID,
    FakeOrchestrationPersistence,
    _load_document,
    _new_attempt_id,
)

_SENSITIVE_SUBSTRINGS = (
    "sequence_mismatch",
    "scene_mismatch",
    "idempotency_key_conflict",
    "attempt_not_found",
    "attempt_not_in_progress",
    "state_before_mismatch",
    "content_hash_mismatch",
    "engine_version_mismatch",
    "psycopg2",
    "Traceback",
    "postgresql://",
    "service_role",
    "eyJ",  # JWT-like prefix
)


def _new_identity(*, email: str = _EMAIL, client: Any = "dummy-supabase-client") -> LearnerIdentityContextV2:
    return LearnerIdentityContextV2(user_email=email, supabase_client=client)


def _new_content():
    return build_scenario_content_v2(copy.deepcopy(_load_document()))


class TestLearnerIdentityContextV2(unittest.TestCase):
    def test_a_missing_email_raises_unauthenticated(self):
        with self.assertRaises(ScenarioControllerV2UnauthenticatedError):
            LearnerIdentityContextV2(user_email="", supabase_client="client")
        with self.assertRaises(ScenarioControllerV2UnauthenticatedError):
            LearnerIdentityContextV2(user_email=None, supabase_client="client")

    def test_b_email_is_normalized(self):
        identity = LearnerIdentityContextV2(user_email="  Learner@Example.COM  ", supabase_client="client")
        self.assertEqual(identity.user_email, "learner@example.com")

    def test_b2_malformed_email_raises_invalid_identity(self):
        with self.assertRaises(ScenarioControllerV2InvalidIdentityError):
            LearnerIdentityContextV2(user_email="not-an-email", supabase_client="client")

    def test_d_missing_supabase_client_fails_closed(self):
        with self.assertRaises(ScenarioControllerV2InvalidIdentityError):
            LearnerIdentityContextV2(user_email=_EMAIL, supabase_client=None)

    def test_af_identity_context_is_not_mutated(self):
        identity = _new_identity()
        original_email = identity.user_email
        original_client = identity.supabase_client
        content = _new_content()
        persistence = FakeOrchestrationPersistence(content=content)
        start_or_resume_learner_scenario_v2(
            content,
            identity=identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=persistence,
        )
        self.assertEqual(identity.user_email, original_email)
        self.assertIs(identity.supabase_client, original_client)
        with self.assertRaises(FrozenInstanceError):
            identity.user_email = "someone-else@example.com"  # type: ignore[misc]


class TestStartOrResumeLearnerScenarioV2(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _new_content()
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.identity = _new_identity()

    def test_a_authenticated_identity_required(self):
        with self.assertRaises(ScenarioControllerV2UnauthenticatedError):
            start_or_resume_learner_scenario_v2(
                self.content,
                identity=None,
                scenario_version_id=_SCENARIO_VERSION_ID,
                attempt_id=_new_attempt_id(),
                persistence=self.persistence,
            )

    def test_c_browser_supplied_email_not_accepted_as_identity(self):
        """Passing a raw string/dict where an identity object is required
        is rejected structurally -- there is no code path that treats a
        bare string as identity."""
        for bogus_identity in (_EMAIL, {"user_email": _EMAIL}, ["not", "an", "identity"]):
            with self.assertRaises(ScenarioControllerV2UnauthenticatedError):
                start_or_resume_learner_scenario_v2(
                    self.content,
                    identity=bogus_identity,
                    scenario_version_id=_SCENARIO_VERSION_ID,
                    attempt_id=_new_attempt_id(),
                    persistence=self.persistence,
                )

    def test_d_missing_supabase_client_fails_closed_without_persistence_override(self):
        with self.assertRaises(ScenarioControllerV2InvalidIdentityError):
            _new_identity(client=None)

    def test_e_start_calls_orchestration_once(self):
        start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=self.persistence,
        )
        self.assertEqual(len(self.persistence.start_calls), 1)

    def test_f_start_uses_trusted_identity_email(self):
        start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=self.persistence,
        )
        self.assertEqual(self.persistence.start_calls[0]["p_user_email"], _EMAIL)

    def test_g_start_returns_active_learner_safe_scene(self):
        result = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=self.persistence,
        )
        self.assertFalse(result.state.is_complete)
        self.assertIsNotNone(result.state.submission_context)
        serialized = serialize_learner_controller_result_v2(result)
        self.assertFalse(serialized["isComplete"])
        self.assertIn("currentScene", serialized)
        self.assertIn("expectedSequenceNumber", serialized)
        self.assertGreater(len(serialized["currentScene"]["options"]), 0)

    def test_h_start_returns_terminal_learner_safe_result_where_applicable(self):
        """Drive the fixture's happy path to completion via start ->
        submit* -> a fresh start (resume) on the now-complete attempt."""
        from tests.test_scenario_orchestration_v2 import HAPPY_PATH_DECISIONS

        attempt_id = _new_attempt_id()
        result = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=attempt_id,
            persistence=self.persistence,
        )
        for _, _, option_id in HAPPY_PATH_DECISIONS:
            result = submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=result.state,
                selected_option_id=option_id,
                persistence=self.persistence,
            )

        self.assertTrue(result.state.is_complete)
        resumed = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=attempt_id,
            persistence=self.persistence,
        )
        self.assertTrue(resumed.state.is_complete)
        self.assertIsNone(resumed.state.submission_context)
        serialized = serialize_learner_controller_result_v2(resumed)
        self.assertTrue(serialized["isComplete"])
        self.assertIn("terminalResult", serialized)
        self.assertNotIn("currentScene", serialized)
        self.assertNotIn("expectedSequenceNumber", serialized)


class TestResumeLearnerScenarioV2(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _new_content()
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.identity = _new_identity()
        self.start_result = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=self.persistence,
        )
        self.attempt_id = self.start_result.state.attempt_id

    def test_i_resume_requires_trusted_attempt_id(self):
        for bogus in (None, "", "   "):
            with self.assertRaises(ScenarioControllerV2InvalidRequestError):
                resume_learner_scenario_v2(
                    self.content,
                    identity=self.identity,
                    attempt_id=bogus,
                    persistence=self.persistence,
                )

    def test_j_resume_calls_canonical_replay_path(self):
        self.persistence.load_calls.clear()
        result = resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            attempt_id=self.attempt_id,
            persistence=self.persistence,
        )
        self.assertEqual(len(self.persistence.load_calls), 1)
        self.assertEqual(
            result.state.submission_context.expected_sequence_number,
            self.start_result.state.submission_context.expected_sequence_number,
        )

    def test_k_resume_identity_mismatch_fails_closed(self):
        self.persistence.identity_override = {"attempt_id": _new_attempt_id()}
        with self.assertRaises(ScenarioControllerV2StaleSessionError) as ctx:
            resume_learner_scenario_v2(
                self.content,
                identity=self.identity,
                attempt_id=self.attempt_id,
                persistence=self.persistence,
            )
        message = str(ctx.exception)
        for sensitive in _SENSITIVE_SUBSTRINGS:
            self.assertNotIn(sensitive, message)

    def test_w_persistence_unavailable_maps_safely_on_resume(self):
        self.persistence.load_raise = "some_unexpected_db_error: connection refused at postgresql://host:5432"
        with self.assertRaises(ScenarioControllerV2PersistenceUnavailableError) as ctx:
            resume_learner_scenario_v2(
                self.content,
                identity=self.identity,
                attempt_id=self.attempt_id,
                persistence=self.persistence,
            )
        message = str(ctx.exception)
        self.assertNotIn("postgresql://", message)
        self.assertNotIn("connection refused", message)

    def test_x_corrupted_replay_maps_safely(self):
        self.persistence.cache_corrupt_for = self.attempt_id
        with self.assertRaises(ScenarioControllerV2CorruptedAttemptError) as ctx:
            resume_learner_scenario_v2(
                self.content,
                identity=self.identity,
                attempt_id=self.attempt_id,
                persistence=self.persistence,
            )
        message = str(ctx.exception)
        for sensitive in _SENSITIVE_SUBSTRINGS:
            self.assertNotIn(sensitive, message)

    def test_z_raw_database_exception_is_not_learner_visible(self):
        self.persistence.load_raise = "attempt_not_found: no row for user_email=learner@example.com"
        with self.assertRaises(ScenarioControllerV2AttemptNotFoundError) as ctx:
            resume_learner_scenario_v2(
                self.content,
                identity=self.identity,
                attempt_id=self.attempt_id,
                persistence=self.persistence,
            )
        message = str(ctx.exception)
        self.assertNotIn("attempt_not_found", message)
        self.assertNotIn(_EMAIL, message)


class TestSubmitLearnerScenarioChoiceV2(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _new_content()
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.identity = _new_identity()
        self.start_result = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=self.persistence,
        )
        self.option_id = self.start_result.state.learner_view.scene_view.options[0].id

    def test_l_submit_requires_controller_state(self):
        for bogus_state in (None, {"attempt_id": "x"}, "not-a-state"):
            with self.assertRaises(ScenarioControllerV2InvalidRequestError):
                submit_learner_scenario_choice_v2(
                    self.content,
                    identity=self.identity,
                    state=bogus_state,
                    selected_option_id=self.option_id,
                    persistence=self.persistence,
                )

    def test_m_submit_validates_option_id(self):
        for bogus_option in (None, "", "   ", 123):
            with self.assertRaises(ScenarioControllerV2InvalidRequestError):
                submit_learner_scenario_choice_v2(
                    self.content,
                    identity=self.identity,
                    state=self.start_result.state,
                    selected_option_id=bogus_option,
                    persistence=self.persistence,
                )

    def test_n_unknown_option_rejected_before_persistence_call(self):
        self.persistence.submit_calls.clear()
        with self.assertRaises(ScenarioControllerV2InvalidRequestError):
            submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=self.start_result.state,
                selected_option_id="totally-unknown-option-id",
                persistence=self.persistence,
            )
        self.assertEqual(len(self.persistence.submit_calls), 0)

    def test_o_idempotency_key_is_uuidv4(self):
        result = submit_learner_scenario_choice_v2(
            self.content,
            identity=self.identity,
            state=self.start_result.state,
            selected_option_id=self.option_id,
            persistence=self.persistence,
        )
        parsed = uuid.UUID(result.last_idempotency_key)
        self.assertEqual(parsed.version, 4)

    def test_p_explicit_retry_preserves_provided_key(self):
        first = submit_learner_scenario_choice_v2(
            self.content,
            identity=self.identity,
            state=self.start_result.state,
            selected_option_id=self.option_id,
            persistence=self.persistence,
        )
        retry = submit_learner_scenario_choice_v2(
            self.content,
            identity=self.identity,
            state=self.start_result.state,
            selected_option_id=self.option_id,
            idempotency_key=first.last_idempotency_key,
            persistence=self.persistence,
        )
        self.assertEqual(retry.last_idempotency_key, first.last_idempotency_key)

    def test_q_submit_calls_orchestration_once(self):
        submit_learner_scenario_choice_v2(
            self.content,
            identity=self.identity,
            state=self.start_result.state,
            selected_option_id=self.option_id,
            persistence=self.persistence,
        )
        self.assertEqual(len(self.persistence.submit_calls), 1)

    def test_r_successful_submit_returns_next_learner_safe_scene(self):
        result = submit_learner_scenario_choice_v2(
            self.content,
            identity=self.identity,
            state=self.start_result.state,
            selected_option_id=self.option_id,
            persistence=self.persistence,
        )
        self.assertFalse(result.state.is_complete)
        serialized = serialize_learner_controller_result_v2(result)
        self.assertEqual(serialized["expectedSequenceNumber"], 2)

    def test_s_terminal_submit_returns_terminal_learner_safe_result(self):
        from tests.test_scenario_orchestration_v2 import HAPPY_PATH_DECISIONS

        result = self.start_result
        for _, _, option_id in HAPPY_PATH_DECISIONS:
            result = submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=result.state,
                selected_option_id=option_id,
                persistence=self.persistence,
            )
        self.assertTrue(result.state.is_complete)
        serialized = serialize_learner_controller_result_v2(result)
        self.assertTrue(serialized["isComplete"])
        self.assertIn("terminalResult", serialized)

    def test_t_stale_sequence_maps_to_stable_stale_session_error(self):
        submit_learner_scenario_choice_v2(
            self.content,
            identity=self.identity,
            state=self.start_result.state,
            selected_option_id=self.option_id,
            persistence=self.persistence,
        )
        # Reusing the ORIGINAL (now stale) state for a second, non-idempotent
        # submission reproduces a real sequence_mismatch from the fake's own
        # CAS enforcement.
        with self.assertRaises(ScenarioControllerV2StaleSessionError) as ctx:
            submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=self.start_result.state,
                selected_option_id=self.option_id,
                persistence=self.persistence,
            )
        message = str(ctx.exception)
        for sensitive in _SENSITIVE_SUBSTRINGS:
            self.assertNotIn(sensitive, message)

    def test_u_scene_conflict_maps_safely(self):
        self.persistence.submit_raise = "scene_mismatch: expected SC001-C01, got SC999"
        with self.assertRaises(ScenarioControllerV2DecisionConflictError) as ctx:
            submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=self.start_result.state,
                selected_option_id=self.option_id,
                persistence=self.persistence,
            )
        message = str(ctx.exception)
        self.assertNotIn("scene_mismatch", message)
        self.assertNotIn("SC001-C01", message)

    def test_v_idempotency_conflict_maps_safely(self):
        self.persistence.submit_raise = "idempotency_key_conflict: reused key with different request"
        with self.assertRaises(ScenarioControllerV2DecisionConflictError) as ctx:
            submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=self.start_result.state,
                selected_option_id=self.option_id,
                persistence=self.persistence,
            )
        self.assertNotIn("idempotency_key_conflict", str(ctx.exception))

    def test_w_persistence_unavailable_maps_safely(self):
        self.persistence.submit_raise = "some_backend_error: 500 at host db.internal:5432"
        with self.assertRaises(ScenarioControllerV2PersistenceUnavailableError) as ctx:
            submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=self.start_result.state,
                selected_option_id=self.option_id,
                persistence=self.persistence,
            )
        message = str(ctx.exception)
        self.assertNotIn("db.internal", message)
        self.assertNotIn("500", message)

    def test_y_raw_rpc_prefix_is_not_learner_visible(self):
        self.persistence.submit_raise = "sequence_mismatch: expected 1, got 4"
        with self.assertRaises(ScenarioControllerV2StaleSessionError) as ctx:
            submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=self.start_result.state,
                selected_option_id=self.option_id,
                persistence=self.persistence,
            )
        self.assertNotIn("sequence_mismatch", str(ctx.exception))

    def test_terminal_attempt_rejected_before_persistence_call(self):
        from tests.test_scenario_orchestration_v2 import HAPPY_PATH_DECISIONS

        result = self.start_result
        for _, _, option_id in HAPPY_PATH_DECISIONS:
            result = submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=result.state,
                selected_option_id=option_id,
                persistence=self.persistence,
            )
        self.assertTrue(result.state.is_complete)
        self.persistence.submit_calls.clear()
        with self.assertRaises(ScenarioControllerV2TerminalAttemptError):
            submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=result.state,
                selected_option_id="does-not-matter",
                persistence=self.persistence,
            )
        self.assertEqual(len(self.persistence.submit_calls), 0)

    def test_identity_mismatch_on_submit_fails_closed(self):
        other_identity = _new_identity(email="different-learner@example.com")
        with self.assertRaises(ScenarioControllerV2InvalidIdentityError):
            submit_learner_scenario_choice_v2(
                self.content,
                identity=other_identity,
                state=self.start_result.state,
                selected_option_id=self.option_id,
                persistence=self.persistence,
            )

    def test_malformed_idempotency_key_rejected(self):
        for bogus_key in ("not-a-uuid", "11111111-1111-1111-1111-111111111111", 12345):
            with self.assertRaises(ScenarioControllerV2InvalidRequestError):
                submit_learner_scenario_choice_v2(
                    self.content,
                    identity=self.identity,
                    state=self.start_result.state,
                    selected_option_id=self.option_id,
                    idempotency_key=bogus_key,
                    persistence=self.persistence,
                )


class TestLearnerSafeOutputAndAliasIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _new_content()
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.identity = _new_identity()
        self.result = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=self.persistence,
        )

    def test_aa_content_hash_is_not_learner_visible(self):
        serialized = serialize_learner_controller_result_v2(self.result)
        blob = repr(serialized)
        self.assertNotIn(self.content.canonical_content_sha256, blob)

    def test_ab_engine_state_flags_counters_not_learner_visible(self):
        serialized = serialize_learner_controller_result_v2(self.result)
        blob = repr(serialized)
        for hidden in ("evaluationTier", "stateChanges", "debriefSeed", "routing", "counters", "flags"):
            self.assertNotIn(hidden, blob)

    def test_ac_supabase_client_or_token_not_serialized(self):
        serialized = serialize_learner_controller_result_v2(self.result)
        blob = repr(serialized)
        self.assertNotIn("dummy-supabase-client", blob)
        self.assertNotIn(self.result.state.attempt_id, blob)

    def test_ad_learner_output_mutation_cannot_change_controller_state(self):
        serialized = serialize_learner_controller_result_v2(self.result)
        serialized["currentScene"]["options"].append({"id": "injected", "title": None, "text": "hack"})
        serialized["currentScene"]["title"] = "mutated"
        re_serialized = serialize_learner_controller_result_v2(self.result)
        self.assertNotEqual(len(re_serialized["currentScene"]["options"]), len(serialized["currentScene"]["options"]))
        self.assertNotEqual(re_serialized["currentScene"]["title"], "mutated")

    def test_ae_controller_state_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            self.result.state.attempt_id = "hacked"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            self.result.state = self.result.state  # type: ignore[misc]

    def test_ag_orchestration_result_not_mutated_by_serialization(self):
        before = copy.deepcopy(serialize_learner_controller_result_v2(self.result))
        # Calling serialize repeatedly must be idempotent/pure.
        serialize_learner_controller_result_v2(self.result)
        after = serialize_learner_controller_result_v2(self.result)
        self.assertEqual(before, after)

    def test_attempt_id_not_exposed_in_serialized_output(self):
        serialized = serialize_learner_controller_result_v2(self.result)
        self.assertNotIn("attemptId", serialized)


class TestControlFlowExceptionsPropagate(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _new_content()
        self.identity = _new_identity()

    def test_ah_keyboard_interrupt_propagates(self):
        class _RaisingPersistence(FakeOrchestrationPersistence):
            def call_start_or_resume_scenario_attempt_v1(self, params):
                raise KeyboardInterrupt()

        persistence = _RaisingPersistence(content=self.content)
        with self.assertRaises(KeyboardInterrupt):
            start_or_resume_learner_scenario_v2(
                self.content,
                identity=self.identity,
                scenario_version_id=_SCENARIO_VERSION_ID,
                attempt_id=_new_attempt_id(),
                persistence=persistence,
            )

    def test_ai_system_exit_propagates(self):
        class _RaisingPersistence(FakeOrchestrationPersistence):
            def call_start_or_resume_scenario_attempt_v1(self, params):
                raise SystemExit(1)

        persistence = _RaisingPersistence(content=self.content)
        with self.assertRaises(SystemExit):
            start_or_resume_learner_scenario_v2(
                self.content,
                identity=self.identity,
                scenario_version_id=_SCENARIO_VERSION_ID,
                attempt_id=_new_attempt_id(),
                persistence=persistence,
            )

    def test_raw_persistence_exception_is_sanitized_and_chained(self):
        """A raw, unexpected exception from the injected persistence
        dependency is never returned to the caller verbatim -- it is first
        classified by the orchestration layer's own error boundary (as
        ``ScenarioOrchestrationV2PersistenceDependencyError``, since it does
        not match any recognized RPC business prefix) and then re-mapped by
        this module into a stable, sanitized
        ``ScenarioControllerV2PersistenceUnavailableError``. Either way, the
        secret-bearing raw message must never appear in the public
        exception text, and the original exception must remain reachable
        via the exception chain for server-side logging."""
        class _RaisingPersistence(FakeOrchestrationPersistence):
            def call_start_or_resume_scenario_attempt_v1(self, params):
                raise RuntimeError("raw unexpected failure with secret=abc123")

        persistence = _RaisingPersistence(content=self.content)
        with self.assertRaises(ScenarioControllerV2PersistenceUnavailableError) as ctx:
            start_or_resume_learner_scenario_v2(
                self.content,
                identity=self.identity,
                scenario_version_id=_SCENARIO_VERSION_ID,
                attempt_id=_new_attempt_id(),
                persistence=persistence,
            )
        self.assertNotIn("secret=abc123", str(ctx.exception))
        self.assertIsNotNone(ctx.exception.__cause__)

    def test_unexpected_internal_failure_wrapped_and_sanitized(self):
        """A genuinely unexpected failure INSIDE this controller module
        itself (not classified by the orchestration layer at all, since it
        never reaches that layer) is wrapped as
        ``ScenarioControllerV2UnexpectedInternalError`` with a fixed,
        generic message -- never the raw exception text -- while the
        original exception remains available via ``__cause__``."""
        from unittest import mock

        persistence = FakeOrchestrationPersistence(content=self.content)
        with mock.patch(
            "utils.scenario_controller_v2.start_or_resume_scenario_run_v2",
            side_effect=ValueError("raw internal bug with secret=xyz789"),
        ):
            with self.assertRaises(ScenarioControllerV2UnexpectedInternalError) as ctx:
                start_or_resume_learner_scenario_v2(
                    self.content,
                    identity=self.identity,
                    scenario_version_id=_SCENARIO_VERSION_ID,
                    attempt_id=_new_attempt_id(),
                    persistence=persistence,
                )
        self.assertNotIn("secret=xyz789", str(ctx.exception))
        self.assertIsNotNone(ctx.exception.__cause__)


class TestEngineV1IsolationAJ(unittest.TestCase):
    def test_aj_v1_controller_module_not_imported_or_modified(self):
        """This module must never import Engine V1's controller, and V1's
        own controller must never import this V2 controller."""
        import utils.scenario_controller_v2 as v2_controller
        import utils.scenario_learner_controller as v1_controller

        self.assertNotIn("scenario_learner_controller", vars(v2_controller))
        v1_source_globals = v1_controller.__dict__
        self.assertNotIn("scenario_controller_v2", v1_source_globals)


class TestControllerReviewRegressionGaps(unittest.TestCase):
    """Non-blocking review gaps folded into permanent regressions."""

    def setUp(self) -> None:
        self.content = _new_content()
        self.persistence = FakeOrchestrationPersistence(content=self.content)
        self.identity = _new_identity()

    def test_scenario_version_not_found_maps_to_scenario_unavailable(self):
        from utils.scenario_orchestration_v2 import ScenarioOrchestrationV2InvalidRequestError

        with patch(
            "utils.scenario_controller_v2.start_or_resume_scenario_run_v2",
            side_effect=ScenarioOrchestrationV2InvalidRequestError(
                "scenario_version_not_found: missing published version"
            ),
        ):
            with self.assertRaises(ScenarioControllerV2ScenarioUnavailableError) as ctx:
                start_or_resume_learner_scenario_v2(
                    self.content,
                    identity=self.identity,
                    scenario_version_id=_SCENARIO_VERSION_ID,
                    attempt_id=_new_attempt_id(),
                    persistence=self.persistence,
                )
        self.assertNotIn("scenario_version_not_found", str(ctx.exception))

    def test_scenario_version_not_published_maps_to_scenario_unavailable(self):
        from utils.scenario_orchestration_v2 import ScenarioOrchestrationV2InvalidRequestError

        with patch(
            "utils.scenario_controller_v2.start_or_resume_scenario_run_v2",
            side_effect=ScenarioOrchestrationV2InvalidRequestError(
                "scenario_version_not_published: version is draft"
            ),
        ):
            with self.assertRaises(ScenarioControllerV2ScenarioUnavailableError):
                start_or_resume_learner_scenario_v2(
                    self.content,
                    identity=self.identity,
                    scenario_version_id=_SCENARIO_VERSION_ID,
                    attempt_id=_new_attempt_id(),
                    persistence=self.persistence,
                )

    def test_canonical_decision_sequence_error_maps_to_corrupted_attempt(self):
        from utils.scenario_orchestration_v2 import ScenarioOrchestrationV2CanonicalDecisionSequenceError

        with patch(
            "utils.scenario_controller_v2.resume_and_replay_scenario_run_v2",
            side_effect=ScenarioOrchestrationV2CanonicalDecisionSequenceError("sequence gap at 3"),
        ):
            with self.assertRaises(ScenarioControllerV2CorruptedAttemptError) as ctx:
                resume_learner_scenario_v2(
                    self.content,
                    identity=self.identity,
                    attempt_id=_new_attempt_id(),
                    persistence=self.persistence,
                )
        self.assertNotIn("sequence gap", str(ctx.exception))

    def test_controller_state_is_intentionally_not_json_or_pickle_serializable(self):
        start_result = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=_new_attempt_id(),
            persistence=self.persistence,
        )
        import json
        import pickle

        with self.assertRaises((TypeError, pickle.PicklingError)):
            pickle.dumps(start_result.state)
        with self.assertRaises(TypeError):
            json.dumps(start_result.state)

    def test_resume_from_attempt_id_only_after_process_loss(self):
        attempt_id = _new_attempt_id()
        initial = start_or_resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            scenario_version_id=_SCENARIO_VERSION_ID,
            attempt_id=attempt_id,
            persistence=self.persistence,
        )
        option_id = initial.state.learner_view.scene_view.options[0].id
        after_submit = submit_learner_scenario_choice_v2(
            self.content,
            identity=self.identity,
            state=initial.state,
            selected_option_id=option_id,
            persistence=self.persistence,
        )
        retained_attempt_id = str(attempt_id)
        del initial, after_submit
        recovered = resume_learner_scenario_v2(
            self.content,
            identity=self.identity,
            attempt_id=retained_attempt_id,
            persistence=self.persistence,
        )
        self.assertFalse(recovered.state.is_complete)
        self.assertEqual(recovered.state.submission_context.expected_sequence_number, 2)

    def test_serialize_invalid_input_rejected(self):
        for bogus in (None, {"state": "not-a-result"}, "not-a-result"):
            with self.subTest(bogus=type(bogus).__name__):
                with self.assertRaises(ScenarioControllerV2InvalidRequestError):
                    serialize_learner_controller_result_v2(bogus)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Disposable REAL PostgREST + real Postgres controller integration smoke
# ---------------------------------------------------------------------------

try:
    # Imported under a non-``Test``-prefixed alias so pytest's default
    # collection does not ALSO re-discover and re-run
    # ``TestSupabasePortDisposablePostgrestSmoke`` a second time merely
    # because it is now a name bound in this module's namespace too.
    from tests.test_scenario_supabase_port_v2 import (
        TestSupabasePortDisposablePostgrestSmoke as _PortDisposableSmokeBase,
        _docker_available,
    )

    _DISPOSABLE_BASE_AVAILABLE = True
except Exception:  # pragma: no cover - defensive only
    _DISPOSABLE_BASE_AVAILABLE = False


if _DISPOSABLE_BASE_AVAILABLE:

    @unittest.skipUnless(
        _docker_available(), "docker CLI not found or daemon not responding -- genuine environment gap"
    )
    class TestScenarioControllerV2DisposablePostgrestSmoke(_PortDisposableSmokeBase):
        """Reuses the exact disposable Postgres/PostgREST bootstrap already
        validated by ``TestSupabasePortDisposablePostgrestSmoke`` (real
        migrations, real ``service_role`` BYPASSRLS bootstrap, real
        ``postgrest-py`` client), but drives the NEW controller APIs
        end-to-end instead of the orchestration layer directly. Distinct
        container/network names and host ports avoid any collision with
        the port-level smoke test when both run in the same session."""

        NETWORK = "certbound-v2-controller-smoke-net"
        PG_CONTAINER = "certbound-v2-controller-smoke-pg"
        POSTGREST_CONTAINER = "certbound-v2-controller-smoke-postgrest"
        PG_HOST_PORT = 55436
        POSTGREST_HOST_PORT = 33004

        def setUp(self) -> None:
            from postgrest import SyncPostgrestClient

            token = self._mint_service_role_jwt()
            client = SyncPostgrestClient(
                f"http://127.0.0.1:{self.POSTGREST_HOST_PORT}",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.identity = LearnerIdentityContextV2(
                user_email=f"controller-smoke-{uuid.uuid4().hex[:8]}@example.com",
                supabase_client=client,
            )
            self.attempt_id = str(uuid.uuid4())

        # Inherited from TestSupabasePortDisposablePostgrestSmoke -- that
        # class already exercises the raw port + orchestration layer
        # end-to-end; this subclass exists to exercise the NEW controller
        # APIs instead, via its own test method below, so the two inherited
        # port-level test methods (which reference ``self.port``, never set
        # by this subclass's own ``setUp``) are explicitly skipped here
        # rather than accidentally inherited and failing for the wrong
        # reason.
        def test_real_postgrest_start_submit_resume_idempotency_and_conflict(self):
            self.skipTest(
                "covered by TestSupabasePortDisposablePostgrestSmoke; this subclass exercises the controller instead"
            )

        def test_real_postgrest_unknown_function_error_is_sanitized(self):
            self.skipTest(
                "covered by TestSupabasePortDisposablePostgrestSmoke; this subclass exercises the controller instead"
            )

        def test_real_controller_start_submit_resume_retry_and_stale_conflict(self):
            start_result = start_or_resume_learner_scenario_v2(
                self.content,
                identity=self.identity,
                scenario_version_id=self.scenario_version_id,
                attempt_id=self.attempt_id,
            )
            self.assertFalse(start_result.state.is_complete)
            serialized_start = serialize_learner_controller_result_v2(start_result)
            self.assertFalse(serialized_start["isComplete"])
            option_id = serialized_start["currentScene"]["options"][0]["id"]

            submit_result = submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=start_result.state,
                selected_option_id=option_id,
            )
            serialize_learner_controller_result_v2(submit_result)

            resumed_result = resume_learner_scenario_v2(
                self.content,
                identity=self.identity,
                attempt_id=self.attempt_id,
            )
            self.assertEqual(resumed_result.state.is_complete, submit_result.state.is_complete)
            if not resumed_result.state.is_complete:
                self.assertEqual(
                    resumed_result.state.submission_context.expected_sequence_number,
                    submit_result.state.submission_context.expected_sequence_number,
                )

            retry_result = submit_learner_scenario_choice_v2(
                self.content,
                identity=self.identity,
                state=start_result.state,
                selected_option_id=option_id,
                idempotency_key=submit_result.last_idempotency_key,
            )
            self.assertEqual(retry_result.state.is_complete, submit_result.state.is_complete)

            with self.assertRaises(ScenarioControllerV2StaleSessionError) as ctx:
                submit_learner_scenario_choice_v2(
                    self.content,
                    identity=self.identity,
                    state=start_result.state,
                    selected_option_id=option_id,
                )
            message = str(ctx.exception)
            for sensitive in _SENSITIVE_SUBSTRINGS:
                self.assertNotIn(sensitive, message)

    # ``_PortDisposableSmokeBase`` is a ``unittest.TestCase`` subclass, so
    # pytest's unittest integration collects it from THIS module's
    # namespace too (regardless of the non-``Test``-prefixed alias name --
    # unittest-style discovery ignores pytest's own naming-convention
    # settings), which would otherwise re-run
    # ``TestSupabasePortDisposablePostgrestSmoke``'s own two test methods a
    # second time here. Removing the name after
    # ``TestScenarioControllerV2DisposablePostgrestSmoke`` has already
    # captured it as a base class (a class's MRO does not depend on its
    # base still being reachable by name) prevents that duplicate run
    # without weakening the subclass itself.
    del _PortDisposableSmokeBase


if __name__ == "__main__":
    unittest.main()
