# Engine V2 Learner Controller — Implementation Report

**Task ID:** SIM-CONTROLLER-V2-01
**Baseline HEAD:** `959647e` — Complete Engine V2 Supabase persistence port
**Files created:**
- `utils/scenario_controller_v2.py`
- `tests/test_scenario_controller_v2.py`
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_CONTROLLER_IMPLEMENTATION_REPORT.md`

**Files modified:** none. `utils/scenario_learner_controller.py` (Engine V1) was not touched.

---

## 1. Objective

Implement an isolated Engine V2 learner controller (`SIM-CONTROLLER-V2-01`) that is the single application-facing boundary between a trusted server-side caller and:

- `utils.scenario_orchestration_v2.start_or_resume_scenario_run_v2`
- `utils.scenario_orchestration_v2.submit_scenario_decision_v2`
- `utils.scenario_orchestration_v2.resume_and_replay_scenario_run_v2`
- `utils.scenario_supabase_port_v2.SupabaseScenarioOrchestrationV2Port`

No Streamlit page layout, no Engine V1 changes, no SQL/migration/schema changes, no production connection, nothing pushed or deployed.

---

## 2. Public API

`utils/scenario_controller_v2.py` exposes exactly:

1. `start_or_resume_learner_scenario_v2(content, *, identity, scenario_version_id, attempt_id=None, persistence=None) -> LearnerScenarioControllerResultV2`
2. `resume_learner_scenario_v2(content, *, identity, attempt_id, persistence=None) -> LearnerScenarioControllerResultV2`
3. `submit_learner_scenario_choice_v2(content, *, identity, state, selected_option_id, idempotency_key=None, persistence=None) -> LearnerScenarioControllerResultV2`
4. `serialize_learner_controller_result_v2(result) -> Dict[str, Any]` — the **only** function permitted to return a raw `dict`.

All four delegate entirely to the existing orchestration API — this module performs no replay, RPC-parameter construction, CAS, or idempotency logic of its own. `LearnerScenarioControllerResultV2` and `LearnerScenarioControllerStateV2` are frozen dataclasses.

---

## 3. Trusted identity design

`LearnerIdentityContextV2` is a frozen dataclass with exactly two fields: `user_email: str`, `supabase_client: Any`. Validation happens entirely in `__post_init__` (construction is the fail-closed boundary):

- `supabase_client is None` → `ScenarioControllerV2InvalidIdentityError` (fails closed — a required server-side dependency is missing).
- `user_email` missing/empty/non-string → `ScenarioControllerV2UnauthenticatedError` (no session at all).
- `user_email` present but fails `utils.scenario_persistence.normalize_scenario_persistence_email`'s existing `lower(btrim(...))` + `"@"` check → `ScenarioControllerV2InvalidIdentityError`.
- On success, `user_email` is replaced (via `object.__setattr__`, the one exception to the frozen-dataclass rule, used only once at construction) with its normalized form. The caller's raw input string is never retained.

Every public entry point calls `_require_identity(identity)`, which raises `ScenarioControllerV2UnauthenticatedError` unless `identity` is genuinely a `LearnerIdentityContextV2` instance — a raw email string, dict, or list passed as `identity` is structurally rejected, never silently accepted as identity (closes the "browser-supplied email" requirement).

`submit_learner_scenario_choice_v2` additionally requires `identity.user_email == state.user_email` (the identity bound into the retained controller state), failing closed with `ScenarioControllerV2InvalidIdentityError` on mismatch — a retained state can never be submitted under a different learner's identity.

This module never reads an environment variable, never constructs or caches a global Supabase client, and never stores a service-role key/token on any dataclass — `supabase_client` is an opaque object supplied by the caller on every `LearnerIdentityContextV2` construction.

---

## 4. Supabase port injection

`_build_port(identity, persistence)`:

- returns `persistence` unchanged if the caller supplied an explicit `ScenarioOrchestrationV2PersistencePort` (used throughout this module's own tests via `FakeOrchestrationPersistence`, and by any future caller that wants dependency injection for its own tests);
- otherwise constructs exactly one `SupabaseScenarioOrchestrationV2Port(identity.supabase_client)` for that single call.

A port instance is never cached, pooled, or reused across calls.

---

## 5. Controller state

`LearnerScenarioControllerStateV2` (frozen):

| Field | Purpose |
|---|---|
| `user_email` | trusted identity binding, checked again on submit |
| `attempt_id` | trusted attempt id (server-side only, see §7) |
| `is_complete` | completion flag |
| `submission_context` | the orchestration layer's own `ScenarioOrchestrationSubmissionContextV2` — `None` exactly when `is_complete` is `True` |
| `learner_view` | the orchestration layer's own `ScenarioOrchestrationLearnerViewV2` |

`submission_context` being forced to `None` on completion is a deliberate controller-level fail-closed rule: `submit_learner_scenario_choice_v2` refuses (`ScenarioControllerV2TerminalAttemptError`) whenever `state.submission_context is None`, **before** ever calling `persistence` — a terminal attempt can never even attempt a database round trip through this controller.

`LearnerScenarioControllerResultV2` wraps `state` plus `last_idempotency_key` (populated only by submit, `None` for start/resume) so a caller can retain the exact key for an explicit retry.

---

## 6. Start / resume / submit flows

- **Start**: validates identity + scenario-version-id/attempt-id shape, builds/receives the port, calls `start_or_resume_scenario_run_v2` exactly once, and reshapes its typed result into `LearnerScenarioControllerResultV2`.
- **Resume**: requires a non-empty `attempt_id`, calls `resume_and_replay_scenario_run_v2` exactly once, then builds the submission context/learner view using the **same private helpers** (`_build_submission_context`, `_build_learner_view`) the orchestration layer's own `start_or_resume_scenario_run_v2` uses internally — this avoids re-deriving that logic in the controller while never modifying `utils/scenario_orchestration_v2.py`.
- **Submit**: validates state type, identity consistency, terminal state (fail-closed before any call), a non-empty `selected_option_id`, and (defensively) idempotency-key format, then calls `submit_scenario_decision_v2` exactly once.

None of the three flows call the persistence dependency more than once per controller call.

---

## 7. Learner-safe serialization and the `attemptId` decision

`serialize_learner_controller_result_v2` produces exactly:

- active: `{"isComplete": False, "currentScene": {...}, "expectedSequenceNumber": <int>}`
- terminal: `{"isComplete": True, "terminalResult": {"outcomeId", "outcomeTitle", "narrative", "displayScore"}}`

**Decision: `attemptId` is never included in the serialized output.** It lives only in `LearnerScenarioControllerStateV2.attempt_id`, retained in trusted server-side session state (mirroring `utils.scenario_learner_controller.ScenarioAttemptView.attempt_id`'s own documented "never render to the learner" contract). There is no V2 Streamlit page yet and no existing requirement for a client-visible opaque attempt identifier, so the safer default is used.

`currentScene` is built field-by-field from the orchestration layer's own `LearnerSceneView` (already excludes evaluation tiers, state deltas, routing, formulas, and debrief seeds by construction — see `utils/scenario_engine_v2.py`). Every nested value is rebuilt through `_plain_json_value`, which recursively converts `MappingProxyType`/`tuple` into fresh `dict`/`list` — the returned dict shares no mutable structure with `result.state`.

---

## 8. Error contract

`ScenarioControllerV2Error` and 11 subclasses (`Unauthenticated`, `InvalidIdentity`, `InvalidRequest`, `AttemptNotFound`, `StaleSession`, `DecisionConflict`, `ScenarioUnavailable`, `PersistenceUnavailable`, `CorruptedAttempt`, `TerminalAttempt`, `UnexpectedInternal`). Every raised instance carries a **fixed, generic** message from a small module-level constant table — never the underlying exception's own text. `_map_orchestration_error` is a closed `isinstance`/prefix mapping from every `ScenarioOrchestrationV2Error` subtype (inspecting `attempt_not_found:` / `scenario_version_not_found:` / `scenario_version_not_published:` / `attempt_not_in_progress:` prefixes only internally, never re-exposing them) to exactly one controller error type. `_run_controller_step` is the single error boundary every entry point routes through; it only ever catches `Exception` (never `BaseException`), so `KeyboardInterrupt`/`SystemExit` are never intercepted. The original exception is always attached via `raise ... from exc`.

One defensive addition beyond straightforward mapping: `_validate_idempotency_key` rejects a malformed/non-UUIDv4 key **before** calling orchestration, because `utils.scenario_persistence_v2.build_submit_decision_rpc_params_v2` raises a raw `ScenarioPersistenceV2ValidationError` (not a `ScenarioOrchestrationV2Error`) for that exact case — closing that gap defensively here avoids modifying the orchestration/persistence-v2 modules, which are out of scope for this task.

---

## 9. Immutability / aliasing

- `LearnerIdentityContextV2` and `LearnerScenarioControllerStateV2`/`LearnerScenarioControllerResultV2` are all frozen dataclasses.
- `serialize_learner_controller_result_v2` never returns a reference into `state.learner_view` — every list/dict is freshly constructed by `_plain_json_value`/`_serialize_scene_view`.
- No credential or Supabase client object is ever placed into the serialized output.

---

## 10. Engine V1 isolation

`utils/scenario_learner_controller.py` was not read for modification purposes beyond the authorized read, and was not modified. `utils/scenario_controller_v2.py` does not import it. `test_aj_v1_controller_module_not_imported_or_modified` asserts the non-import relationship in both directions.

---

## 11. Tests

`tests/test_scenario_controller_v2.py`: **47 passed, 2 skipped** (the 2 skips are inherited `TestSupabasePortDisposablePostgrestSmoke` port-level test methods, deliberately no-op'd in the controller-focused disposable subclass — see file comments). Covers requirements A–AJ using `FakeOrchestrationPersistence` (imported from `tests.test_scenario_orchestration_v2`), plus:

- a real disposable Docker/PostgREST/Postgres integration test (`TestScenarioControllerV2DisposablePostgrestSmoke`, subclassing the port's own validated bootstrap with distinct container/network/port names) exercising start → serialize → submit → serialize → resume → retry → stale-conflict end-to-end through the new controller APIs only, confirming the stale-conflict error message is fully sanitized against real PostgREST error text.

Full suite after implementation: **652 passed, 2 skipped, 45 subtests passed** (baseline 605 passed / 45 subtests + 47 new).

---

## 12. Scope confirmation

- No SQL/migration/schema/RLS/grant changes.
- No production connection; disposable Docker Postgres/PostgREST only, destroyed in `tearDownClass`.
- No Streamlit UI added.
- No files modified outside the three listed above.
- Protected paths (`.local/`, `local_only/`, etc.) untouched.
- Nothing staged, committed, pushed, or deployed.
