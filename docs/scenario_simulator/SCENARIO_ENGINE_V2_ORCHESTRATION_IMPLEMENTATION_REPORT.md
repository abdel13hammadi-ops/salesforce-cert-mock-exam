# SCENARIO_ENGINE_V2 Orchestration Service — Implementation Report

**Task ID:** SIM-PERSIST-V2-05
**Model:** Sonnet High
**Baseline:** `a214e36` — Complete Engine V2 persistence foundation
**Scope:** Pure Python orchestration service composing the committed Engine V2 runtime and V2 persistence adapter. No SQL/migration/RLS change. No production connection. No Engine V1 behavior change. No Streamlit UI. Nothing staged, committed, pushed, or deployed.

---

## 1. Task status

**COMPLETE.** The orchestration module, its focused unit test suite, and a real disposable-PostgreSQL smoke test were implemented, and every required test command passes with zero regressions.

## 2. Files created

- `utils/scenario_orchestration_v2.py` — the Engine V2 start/resume/submit orchestration service.
- `tests/test_scenario_orchestration_v2.py` — 36 deterministic-fake unit tests (lettered requirements A–AJ) plus one real disposable-PostgreSQL smoke test (`TestScenarioOrchestrationV2DisposableSmoke`, skipped automatically when Docker is unavailable).
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_IMPLEMENTATION_REPORT.md` (this file).

## 3. Files modified

**None.** `utils/scenario_engine_v2.py`, `utils/scenario_persistence_v2.py`, and `utils/scenario_persistence.py` were read-only inspected and reused by import (the last only for `normalize_scenario_persistence_email`, an already-public, engine-agnostic primitive). No Engine V1 controller, UI, or SQL file was touched.

## 4. Repository branch

`main`.

## 5. Starting HEAD

`a214e36` — "Complete Engine V2 persistence foundation".

## 6. Ending HEAD

`a214e36` (unchanged — no commit was made in this task).

## 7. Starting git status

```
## main...origin/main [ahead 20]
?? .local/
?? local_only/
?? scripts/v58_run_combined_policy_evaluation.py
?? structural_audit_state.json
?? tests/test_combined_policy_evaluator.py
?? v68_corrected_review_bundle/
?? v68_final_review_bundle/
?? v68_review_bundle/
?? workers/combined_policy_evaluator.py
```

(all pre-existing, unrelated protected paths; confirmed untouched throughout this task).

## 8. Ending git status

```
## main...origin/main [ahead 20]
?? .local/
?? local_only/
?? scripts/v58_run_combined_policy_evaluation.py
?? structural_audit_state.json
?? tests/test_combined_policy_evaluator.py
?? tests/test_scenario_orchestration_v2.py
?? utils/scenario_orchestration_v2.py
?? v68_corrected_review_bundle/
?? v68_final_review_bundle/
?? v68_review_bundle/
?? workers/combined_policy_evaluator.py
```

Only the two new files are added; nothing is staged.

## 9. Shell pre-flight result

`Write-Output "shell-ok"`, `git status --short --branch`, and `git log -1 --oneline` all succeeded. Branch `main`, HEAD `a214e36`, nothing staged, only the same unrelated protected paths untracked.

## 10. Baseline tests executed

```
python -m pytest tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q
```

## 11. Baseline test results

`471 passed, 9 subtests passed` — matches the required baseline exactly.

## 12. Orchestration public API

```python
start_or_resume_scenario_run_v2(content, *, persistence, user_email, scenario_version_id, attempt_id=None) -> StartOrResumeScenarioRunResultV2
submit_scenario_decision_v2(content, *, persistence, submission_context, selected_option_id, idempotency_key=None) -> SubmitScenarioDecisionResultV2
load_canonical_scenario_decisions_v2(decision_rows, *, attempt_id) -> Tuple[ScenarioDecisionInputV2, ...]
resume_and_replay_scenario_run_v2(content, *, persistence, user_email, attempt_id) -> Tuple[ScenarioRunV2Snapshot, TrustedAttemptSnapshotV2]
```

All four required entry points are implemented as explicit, fully typed functions. Every return value is a frozen dataclass (`StartOrResumeScenarioRunResultV2`, `SubmitScenarioDecisionResultV2`, `TrustedAttemptSnapshotV2`, `ScenarioOrchestrationSubmissionContextV2`, `ScenarioOrchestrationLearnerViewV2`) — no raw Supabase dict is ever returned to a caller.

## 13. Dependency-injection design

`ScenarioOrchestrationV2PersistencePort` is a `typing.Protocol` with exactly three methods:

- `call_start_or_resume_scenario_attempt_v1(params)`
- `call_submit_scenario_decision_v1(params)`
- `load_attempt_snapshot(*, user_email, attempt_id)` (the `get_scenario_attempt_v1` row shape, including its `decisions` array)

No Supabase client is ever instantiated inside `utils/scenario_orchestration_v2.py`, and the module never reads an environment variable. Unit tests inject `FakeOrchestrationPersistence`, an in-memory stateful fake that reproduces the RPCs' CAS/idempotency semantics. The disposable-database smoke test injects `_PostgresOrchestrationPersistence`, a thin psycopg2-backed adapter that calls the real installed SQL functions over a real (disposable) connection — proving the port abstraction is satisfied by both a fake and a real backend without any orchestration-layer change.

## 14. Start-flow result

`start_or_resume_scenario_run_v2` follows the exact 12-step contract: validates content identity, accepts/mints one attempt UUID before Engine V2 initialization, calls `start_scenario_run_v2` with that UUID, serializes the frozen 17-key envelope via `serialize_run_snapshot_v2`, builds the exact 7-key RPC parameter set via `build_start_or_resume_rpc_params_v2`, calls the injected start RPC, parses the response via `parse_start_or_resume_rpc_response_v2`, then **unconditionally** reloads trusted attempt identity and canonical decisions and replays from content, never trusting the start RPC's own envelope as authority. Verified by tests A–F.

## 15. Stable attempt-UUID result

Confirmed by tests A and B: a caller-supplied or freshly minted UUID is used identically for Engine V2 initialization (`run.attempt_id`) and the RPC's `p_attempt_id` parameter — no re-minting occurs between the two.

## 16. Resume-flow result

`resume_and_replay_scenario_run_v2` loads the trusted attempt row (via the injected `load_attempt_snapshot`), validates its identity against the immutable content (engine version, content hash), loads and validates canonical decisions via `load_canonical_scenario_decisions_v2`, and calls `replay_serialized_run_v2` to reconstruct the run purely from content + canonical decisions, using the persisted envelope strictly as a fail-closed verification cache. Verified by tests G–J: canonical decisions are loaded and replayed (G), the persisted cache is never trusted as authority and a corrupted cache is rejected (H, I), and a trusted-identity mismatch (content hash) is rejected (J).

## 17. Canonical-decision loading result

`load_canonical_scenario_decisions_v2` validates, for every row: matching attempt ID (when present on the row), a strict (non-bool) integer sequence number starting at 1 with no gaps or duplicates, and non-empty scene/option IDs — mapping the database's `expectedSceneId`/`selectedOptionId` field names to Engine V2's `sceneId`/`optionId` shape. Verified by tests K–O (gap rejection, duplicate rejection, bool-sequence rejection, wrong-attempt-ID rejection, and non-mutation of the input rows).

## 18. Replay-authority result

Every code path (`start_or_resume_scenario_run_v2`, `submit_scenario_decision_v2`) reconstructs the run from immutable content plus canonical `scenario_decisions` rows via `replay_serialized_run_v2`, and only afterward compares the result against the persisted envelope for a fail-closed sanity check — the persisted JSON is never the reconstruction source. Confirmed by test H (cache is ignored as authority) and test AB (a real RPC success followed by a corrupted persisted cache still fails closed).

## 19. Submit-flow result

`submit_scenario_decision_v2` follows the exact 14-step contract: validates the selected option is one of the caller's current learner-visible options, generates (or accepts, for retries) a UUIDv4 idempotency key, applies the decision locally via `apply_decision_v2` to compute `run_after`, serializes before/after envelopes, builds the exact 13-key RPC parameter set via `build_submit_decision_rpc_params_v2`, calls the injected submit RPC, parses the response, and then **unconditionally** reloads trusted identity + canonical decisions and replays, asserting the replay equals the locally computed `run_after` before returning a result. Verified by tests P–AB.

## 20. CAS result

The submit RPC's CAS contract (expected sequence, expected scene, state-before envelope) is preserved unmodified — the orchestration layer never fabricates or bypasses these fields; they are derived entirely from `run_before`/`decision`. A stale sequence or scene response from the injected RPC is mapped to a specific typed conflict (`ScenarioOrchestrationV2SequenceConflictError` / `ScenarioOrchestrationV2SceneConflictError`) rather than silently retried. Verified by tests Y and Z.

## 21. Idempotency result

A first submission mints a UUIDv4 key (test V); an identical retry with the same key returns `idempotent_replay=True` without creating a second decision row (test W); a retry that reuses the same key with a materially different request (a different visible-option set implying a different fingerprint) fails closed with `ScenarioOrchestrationV2IdempotencyConflictError` (test X). No key is ever auto-regenerated on an explicit retry — `submit_scenario_decision_v2` only mints a key when the caller passes `idempotency_key=None`.

## 22. Conflict-classification result

RPC error messages are classified via a prefix map (mirroring `utils.scenario_persistence`'s own `_ERROR_PREFIX_MAP` convention) into the module's explicit domain exceptions: `ScenarioOrchestrationV2InvalidRequestError`, `ScenarioOrchestrationV2IdentityMismatchError`, `ScenarioOrchestrationV2StaleRunError`, `ScenarioOrchestrationV2SequenceConflictError`, `ScenarioOrchestrationV2SceneConflictError`, `ScenarioOrchestrationV2IdempotencyConflictError`, `ScenarioOrchestrationV2TerminalMismatchError`, and a catch-all `ScenarioOrchestrationV2PersistenceDependencyError` for anything unrecognized. No stale decision is ever automatically retried with recomputed state — the caller always receives a typed exception and must explicitly reload.

## 23. Learner-safe result

`ScenarioOrchestrationLearnerViewV2` exposes only `scene_view: Optional[LearnerSceneView]` or `terminal_view: Optional[LearnerTerminalView]` (mutually exclusive), both produced by Engine V2's own `build_learner_scene_view`/`build_learner_terminal_view` — the same learner-safe projections Engine V1's controller already relies on. No `state`, `counters`, `flags`, `decisionHistory`, `routingResolutions`, content hash, or raw RPC/database row is reachable from this object. Verified by tests AC–AF (active-scene view, terminal view, no hidden fields when serialized via `serialize_learner_scene_view_v2`, and no raw dict escapes the typed result objects).

## 24. Error-contract result

Nine explicit orchestration exceptions are defined, all descending from `ScenarioOrchestrationV2Error`, covering every category the task requires (invalid request, malformed persistence response, identity mismatch, canonical sequence error, stale/CAS conflict, sequence conflict, scene conflict, idempotency conflict, replay mismatch, terminal mismatch, unavailable dependency). Every persistence-dependency exception is wrapped via `_map_persistence_exception`/`_wrap_persistence_call` with causal chaining (`__cause__` set), so a raw `KeyError`/`TypeError`/`ValueError`/`AttributeError`/UUID error or Supabase/psycopg2 implementation error never escapes the module. Verified by test AH.

## 25. Immutability result

Every dataclass returned by the module is `frozen=True`. RPC parameter dicts and trusted attempt rows are deep-copied (`_deep_copy_json`) on the way in, so mutating a caller-owned dict after the call (test AG) or the original fixture document (test AI) cannot affect the orchestration result. Verified by tests AG, AI, and O (canonical decision rows are not mutated during validation).

## 26. Engine V1 isolation result

No file under `utils/` other than `utils/scenario_orchestration_v2.py` itself references the new module (confirmed via a repository-wide grep for `scenario_orchestration_v2`, which returns only the new module's own test file). `utils/scenario_learner_controller.py` and Engine V1's RPC call flow are unmodified. Verified by test AJ (Engine V1 test modules remain importable and unaffected).

## 27. Unit tests created

36 deterministic-fake unit tests in `tests/test_scenario_orchestration_v2.py`, covering lettered requirements A through AJ, organized into `TestStartFlow`, `TestResumeFlow`, `TestCanonicalDecisionLoading`, `TestSubmitFlow`, `TestLearnerSafeResults`, `TestErrorContractAndImmutability`, and `TestEngineV1Isolation`.

## 28. Unit tests executed

```
python -m pytest tests/test_scenario_orchestration_v2.py tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q
```

## 29. Unit test results

`508 passed, 9 subtests passed` (the `471`-test/`9`-subtest baseline plus `37` new orchestration tests: 36 fake-backed unit tests plus 1 real disposable-database smoke test that ran successfully because Docker was available in this environment).

## 30. Disposable database identity

A throwaway `postgres:16` Docker container, name `certbound-v2-orchestration-smoke`, created fresh by the test's own `setUpClass` (`docker rm -f` followed by `docker run -d ... postgres:16`), reachable only via a host-local port mapping (`127.0.0.1:55432`). No Supabase project reference, URL, anon key, or service-role key was used anywhere.

## 31. Production target excluded

Confirmed: the persistence port used for the smoke test (`_PostgresOrchestrationPersistence`) connects only to `127.0.0.1:55432`, a locally Docker-published port with no external exposure. No production connection string, hostname, or credential appears anywhere in the test file.

## 32. Baseline migrations applied

In order, against the fresh container: `20260718170000_v66_scenario_definition_persistence_foundation.sql`, `20260719003000_v67_harden_scenario_definition_security.sql`, `20260719130000_v68_scenario_attempt_persistence_foundation.sql`, `20260719140000_v69_scenario_v2_attempt_identity_support.sql` — the same four-migration baseline validated by SIM-PERSIST-V2-03's Slice B report. (A bare `postgres:16` image has none of Supabase's built-in `anon`/`authenticated`/`service_role` roles, so the test's `setUpClass` first creates those three roles with a small idempotent `DO $$ ... $$` block before applying the migrations — no migration file was edited to do this.)

## 33. Real start-RPC result

`start_or_resume_scenario_attempt_v1` was called through the orchestration service's injected port with parameters built by `build_start_or_resume_rpc_params_v2`, against the real installed SQL function. It returned `created=True` for a fresh attempt UUID.

## 34. Persisted attempt-ID result

The RPC-returned `attempt_id` and the trusted `get_scenario_attempt_v1` reload both equal the caller-supplied Engine V2 attempt UUID (`start.attempt_id == self.attempt_id`), confirmed by an explicit assertion in `test_disposable_start_submit_resume_idempotency_and_conflict`.

## 35. Real submit-RPC result

`submit_scenario_decision_v1` was called through the orchestration service with parameters built by `build_submit_decision_rpc_params_v2`, against the real installed SQL function, and returned a successful decision acceptance.

## 36. Canonical row-load result

After the real submit, `load_attempt_snapshot`'s real `decisions` array (loaded through `get_scenario_attempt_v1`) contains exactly one row, confirmed by an explicit length assertion.

## 37. Resume/replay result

`resume_and_replay_scenario_run_v2` was called against the same real database afterward; its recomputed `expected_sequence_number` equals the value returned by the real submit, confirming replay-from-canonical-rows reproduces the true post-submit state.

## 38. Same-key retry result

The same submission was retried with the identical idempotency key returned by the first real submit call; the result's `idempotent_replay` flag was `True`.

## 39. Duplicate-decision result

After the retry, the real `decisions` array still contains exactly one row (asserted again) — the retry did not insert a duplicate `scenario_decisions` row or advance the sequence a second time.

## 40. Stale/conflict result

A further submission was attempted against the same (now stale, already-submitted) `submission_context`; it raised `ScenarioOrchestrationV2SequenceConflictError`, confirming the real database's sequence-mismatch response is correctly classified into a typed, fail-closed exception rather than silently retried.

## 41. Disposable database cleanup result

`tearDownClass` ran `docker rm -f certbound-v2-orchestration-smoke` unconditionally; `docker ps -a --filter name=certbound-v2-orchestration-smoke` was confirmed empty afterward.

## 42. Database/schema objects changed

None, beyond the disposable container's own throwaway schema, which no longer exists.

## 43. SQL/migrations modified

None. All four migration files were applied unmodified, read-only, against the disposable container only.

## 44. UI modified

None. No Streamlit file was touched.

## 45. Files modified outside scope

None. Only the three files listed in Section 2 were created; no existing file was modified.

## 46. Protected paths untouched

Confirmed via `git status --short --branch` before and after this task: `.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`, `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`, `workers/combined_policy_evaluator.py`, `v68_corrected_review_bundle/`, `v68_final_review_bundle/`, and `v68_review_bundle/` all remain in the exact same untracked state as the starting status, and none of their contents were ever read, searched, or executed.

## 47. Nothing staged, committed, pushed, or deployed

Confirmed: `git status --short --branch` shows only new untracked files; no `git add`, `git commit`, or `git push` was ever run.

## 48. Errors encountered

During the disposable-database smoke test build-out: (a) a bare `postgres:16` image lacks Supabase's `anon`/`authenticated`/`service_role` roles referenced by the migrations' `GRANT`/`REVOKE` statements, requiring a small idempotent role-bootstrap step; (b) `scenario_versions`' lifecycle column is named `lifecycle_status`, not `status`; (c) raw `psycopg2` returns native `datetime` objects for `timestamptz` columns where the real Supabase/PostgREST client always returns ISO-8601 strings over JSON, requiring a JSON round-trip in the smoke test's RPC helper to faithfully reproduce the real wire shape; (d) the smoke test's `psycopg2` connections initially ran without `autocommit`, so writes from one RPC call were rolled back before the next call's connection could see them. All four were fixed in the test file itself; none required any change to `utils/scenario_orchestration_v2.py` or any migration.

## 49. Stop conditions encountered

None. No stop condition (unavailable shell, failing baseline, unsupported RPC contract, un-reconstructable canonical decisions, unverifiable trusted identity, adapter/RPC contract conflict, required SQL/RLS/schema change, required Engine V1 change, unprovable-disposable database, production credentials, or protected-path need) was encountered.

## 50. Remaining risks

The disposable-smoke `_PostgresOrchestrationPersistence` helper is test-only scaffolding (raw `psycopg2`, not a real Supabase client) — a future task wiring this orchestration service into the actual application should adapt a real Supabase Python client's `.rpc(...).execute()` calls to the same three-method `ScenarioOrchestrationV2PersistencePort` protocol rather than reusing this test helper directly. The orchestration layer's error-prefix map is intentionally scoped to the two RPCs this module calls; if `start_or_resume_scenario_attempt_v1` or `submit_scenario_decision_v1` gain new RPC-raised error prefixes in a future migration, this module's `_RPC_ERROR_PREFIX_MAP` will need a matching update (an unmapped prefix currently falls back to the generic `ScenarioOrchestrationV2PersistenceDependencyError`, which is safe but less specific).

## 51. Git status

```
## main...origin/main [ahead 20]
?? .local/
?? local_only/
?? scripts/v58_run_combined_policy_evaluation.py
?? structural_audit_state.json
?? tests/test_combined_policy_evaluator.py
?? tests/test_scenario_orchestration_v2.py
?? utils/scenario_orchestration_v2.py
?? v68_corrected_review_bundle/
?? v68_final_review_bundle/
?? v68_review_bundle/
?? workers/combined_policy_evaluator.py
```

(This report itself, once written, will additionally appear as `?? docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_IMPLEMENTATION_REPORT.md`.)

## 52. Recommended next task

A short, focused review task (mirroring SIM-PERSIST-V2-04's own review/correction cycle) to independently audit this orchestration module's RPC error-prefix coverage and its learner-safe serialization boundary against the frozen V69 SQL contract, before any real controller or UI code is wired to call `start_or_resume_scenario_run_v2`/`submit_scenario_decision_v2`.

---

## Addendum — SIM-PERSIST-V2-05B correction pass

The independent review `SIM-PERSIST-V2-05-REVIEW-01` (see `SCENARIO_ENGINE_V2_ORCHESTRATION_FOCUSED_REVIEW.md`) found one HIGH and two MEDIUM findings against the implementation described above. `SIM-PERSIST-V2-05B` closed all three with a minimal, behavior-preserving correction pass (see `SCENARIO_ENGINE_V2_ORCHESTRATION_CORRECTION_REPORT.md` for full detail):

- **HIGH-01** — `_parse_attempt_snapshot_row`'s decisions parsing (Section 17/121-line reference above) previously called `dict(item)` on every element of the persisted `decisions` array, letting a raw `TypeError` escape for any non-mapping element (int, string, list, bool, `None`). It now explicitly validates each element is a `Mapping` before copying it, raising `ScenarioOrchestrationV2MalformedPersistenceResponseError` otherwise.
- **MEDIUM-01** — `normalize_scenario_persistence_email` (reused from `utils.scenario_persistence`, Section 3/22) previously let its own `ScenarioPersistenceValidationError` escape every V2 entry point that validates an email. A new `_normalize_email_or_raise` helper now translates that exception into `ScenarioOrchestrationV2InvalidRequestError` with the original preserved as `__cause__`, applied at `start_or_resume_scenario_run_v2`, `resume_and_replay_scenario_run_v2`, and (defense-in-depth) `submit_scenario_decision_v2`.
- **MEDIUM-02** — `start_or_resume_scenario_run_v2`'s docstring (Section 14) previously stated that "the RPC ignores a freshly minted id" on resume, which does not match V69's actual `attempt_id_conflict`/`attempt_id_collision` behavior. The docstring was corrected to describe the three actual outcomes (fresh create, matching resume, and the two distinct fail-closed conflict cases) with no runtime behavior change.

The unit test suite in Section 27 grew from 36 to 52 deterministic-fake tests (16 new regression tests covering the three corrections); the required test command in Section 28 now reports `524 passed, 9 subtests passed` in place of the prior `508 passed, 9 subtests passed`. No other section of this report changed; RPC shapes, the persistence protocol, and Engine V1 remain exactly as originally described.
