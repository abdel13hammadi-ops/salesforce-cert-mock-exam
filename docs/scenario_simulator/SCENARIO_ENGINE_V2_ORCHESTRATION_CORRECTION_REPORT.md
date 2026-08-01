# SCENARIO_ENGINE_V2 Orchestration Service — Correction Report

**Task ID:** SIM-PERSIST-V2-05B
**Model:** Sonnet High
**Baseline:** `a214e36` — Complete Engine V2 persistence foundation
**Scope:** Minimal correction pass closing all actionable findings from `SCENARIO_ENGINE_V2_ORCHESTRATION_FOCUSED_REVIEW.md` (HIGH-01, MEDIUM-01, MEDIUM-02). No redesign, no persistence-protocol change, no RPC-shape change, no SQL/migration/RLS/schema/controller/UI change, no database connection, no Engine V1 change. Nothing staged, committed, pushed, or deployed.

---

## 1. Task status

**COMPLETE.** All three findings (HIGH-01, MEDIUM-01, MEDIUM-02) are closed. 16 new regression tests were added. The full focused test command passes with zero regressions.

## 2. Files modified

- `utils/scenario_orchestration_v2.py`
- `tests/test_scenario_orchestration_v2.py`
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_IMPLEMENTATION_REPORT.md`

## 3. File created

- `docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_CORRECTION_REPORT.md` (this file).

## 4. Repository branch

`main`.

## 5. HEAD

`a214e36` — unchanged throughout (no commit was made).

## 6. Starting git status

```
## main...origin/main [ahead 20]
?? .local/
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_FOCUSED_REVIEW.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_IMPLEMENTATION_REPORT.md
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

(all pre-existing untracked paths from the prior `SIM-PERSIST-V2-05`/`-REVIEW-01` tasks; confirmed unchanged in kind throughout this task.)

## 7. Ending git status

Identical set of untracked paths, plus this new correction report file (see Section 41).

## 8. Shell pre-flight result

`Write-Output "shell-ok"`, `git status --short --branch`, and `git log -1 --oneline` all succeeded. Branch `main`, HEAD `a214e36`, nothing staged, only the expected orchestration files (plus the pre-existing unrelated protected paths) untracked.

## 9. Baseline tests executed

```
python -m pytest tests/test_scenario_orchestration_v2.py tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q
```

## 10. Baseline test results

`508 passed, 9 subtests passed` — matched the required current-state baseline exactly before any edit was made.

## 11. HIGH-01 disposition

**CLOSED.** `_parse_attempt_snapshot_row` previously built its `decisions` tuple with:

```python
decisions=tuple(_deep_copy_json(item) if isinstance(item, Mapping) else dict(item) for item in decisions_raw)
```

Any non-mapping element (`int`, `str`, `list`, `bool`, `None`, or an arbitrary object) reached the bare `dict(item)` fallback, which raises a raw `TypeError` for every one of those types except an iterable of 2-tuples (an obscure coercion path that was never intended). The fix replaces the fallback with an explicit per-element validation loop: every element must be a `Mapping`, or the parser raises `ScenarioOrchestrationV2MalformedPersistenceResponseError` immediately, before any further elements are processed and before the input list or any of its elements is touched. No element is silently skipped — the loop fails closed on the first invalid element it encounters, exactly like every other field-level check already present in this function.

## 12. MEDIUM-01 disposition

**CLOSED.** `normalize_scenario_persistence_email` (Engine V1's `utils.scenario_persistence` helper, reused as-is by V2 orchestration for its exact `lower(btrim(...))` normalization and `@`-presence check) raises its own `ScenarioPersistenceValidationError` for invalid input. That V1-specific exception type previously propagated unchanged out of `start_or_resume_scenario_run_v2` and `resume_and_replay_scenario_run_v2` — a V1 exception type escaping a V2 public entry point, violating the module's own error-contract intent. A new internal helper, `_normalize_email_or_raise`, wraps every call site: it calls the unmodified V1 validator and, on `ScenarioPersistenceValidationError`, re-raises `ScenarioOrchestrationV2InvalidRequestError` with the original exception attached via `raise ... from exc` (visible as `__cause__`). Applied at all three points where this module accepts/uses a caller-facing `user_email`: `start_or_resume_scenario_run_v2`, `resume_and_replay_scenario_run_v2`, and — as defense-in-depth, since a caller-supplied `submission_context` is nominally trusted server-side state but is still a plain constructible dataclass — `submit_scenario_decision_v2`. Valid-email behavior (normalization: trim + lowercase) is byte-for-byte unchanged; only the invalid-email exception *type* changed.

## 13. MEDIUM-02 disposition

**CLOSED.** `start_or_resume_scenario_run_v2`'s docstring previously stated: *"For a resume, the RPC ignores a freshly minted id and returns the existing row's id"* — this does not match the V69 `start_or_resume_scenario_attempt_v1` SQL contract, which:

- creates a **new** attempt when the caller has no existing in-progress attempt for that user + scenario version (using the supplied/minted `p_attempt_id`, unless that id already collides with an unrelated existing row, in which case it fails closed with `attempt_id_collision`);
- **resumes** only when the caller already has an existing in-progress attempt for that user + scenario version *and* the supplied `p_attempt_id` matches that attempt's own id;
- fails closed with `attempt_id_conflict` (not a silent resume) when the caller has an existing in-progress attempt but supplies a *different*, non-matching `p_attempt_id`.

The docstring was rewritten to describe these three outcomes precisely, to state plainly that the envelope itself carries no attempt identity (identity is driven entirely by `p_attempt_id` plus trusted user/scenario-version identity), and to note that this function never trusts the RPC's own response as authority regardless of outcome (it always reloads + replays afterward). This is a documentation-only change; `start_or_resume_scenario_run_v2`'s runtime behavior, its RPC parameters, and the V69 migration are all unmodified.

## 14. Remaining blocker count

**0.**

## 15. Remaining high count

**0.**

## 16. Malformed-decision validation result

Every non-`Mapping` element in a persisted `decisions` array (`int`, `str`, `list`, `bool`, `None`) now raises `ScenarioOrchestrationV2MalformedPersistenceResponseError` via `resume_and_replay_scenario_run_v2` → `_parse_attempt_snapshot_row`. Verified by five new dedicated tests (one per rejected type) plus a sixth test proving a malformed element following a *valid* one is not silently dropped.

## 17. Raw TypeError result

Confirmed not to escape: a dedicated regression test (`test_raw_type_error_does_not_escape_malformed_decision_parsing`) asserts the call raises `ScenarioOrchestrationV2MalformedPersistenceResponseError` and explicitly fails the test if a `TypeError` is caught instead. A standalone temporary probe (Section 28) independently reproduced the same result outside the test framework.

## 18. Input immutability result

`test_malformed_decisions_input_is_not_mutated` deep-copies the persistence fake's raw `decisions` list before the failing call and asserts it is byte-for-byte unchanged afterward — the new validation loop never writes to `decisions_raw` or its elements; it only reads (`isinstance` check) and, for accepted elements, deep-copies into a new local list.

## 19. Invalid-email translation result

`normalize_scenario_persistence_email`'s `ScenarioPersistenceValidationError` no longer escapes any of the three points where this module validates a `user_email`: `start_or_resume_scenario_run_v2`, `resume_and_replay_scenario_run_v2` (called directly, and indirectly by `start_or_resume_scenario_run_v2`/`submit_scenario_decision_v2`'s own reload step), and `submit_scenario_decision_v2`. All three are covered by dedicated regression tests plus one test that explicitly asserts the raw V1 exception type is never caught by callers expecting it.

## 20. Cause-chaining result

`_normalize_email_or_raise` uses `raise ScenarioOrchestrationV2InvalidRequestError(...) from exc`, so `exc.__cause__` is always the original `ScenarioPersistenceValidationError`. Verified by `test_original_validation_exception_is_available_through_cause`, which asserts `isinstance(exc.__cause__, ScenarioPersistenceValidationError)`.

## 21. BaseException control-flow result

`_wrap_persistence_call`/`_map_persistence_exception` (the module's only broad exception-handling boundary) catch `Exception`, never `BaseException` — `KeyboardInterrupt` and `SystemExit` (both direct `BaseException` subclasses, not `Exception` subclasses) were already unaffected by this boundary and remain so after this correction pass. `_normalize_email_or_raise` similarly only catches the specific `ScenarioPersistenceValidationError` type, not a broad `Exception`/`BaseException`. Verified by two new tests injecting `KeyboardInterrupt`/`SystemExit` from a persistence-port method and asserting each propagates unchanged out of `start_or_resume_scenario_run_v2`.

## 22. Docstring correction result

`start_or_resume_scenario_run_v2`'s docstring now accurately describes new-attempt creation, matching resume, and both distinct V69 fail-closed conflict cases (`attempt_id_conflict` vs. `attempt_id_collision`), with no runtime code changed. See Section 13.

## 23. RPC-shape result

Unchanged. `_START_RPC_KEYS` (7 keys) and `_SUBMIT_RPC_KEYS` (13 keys) are asserted identical before and after this correction pass by the pre-existing tests `test_c_start_rpc_receives_exactly_seven_parameters` and `test_r_submit_rpc_receives_exactly_thirteen_parameters`, both of which still pass unmodified.

## 24. CAS/idempotency/replay regression result

All pre-existing CAS (`Y`, `Z`), idempotency (`V`, `W`, `X`), and replay (`G`–`J`, `AA`, `AB`) tests pass unmodified — none of their assertions were weakened or altered by this correction pass.

## 25. Tests added

16 new tests across three new test classes in `tests/test_scenario_orchestration_v2.py`:

- `TestMalformedPersistedDecisionElements` (7 tests): non-mapping decision elements (`int`, `str`, `list`, `None`, `bool`), malformed-element-not-skipped, input-not-mutated, and raw-`TypeError`-does-not-escape.
- `TestInvalidEmailTranslation` (6 tests): invalid email during start (with and without an explicit V1-exception-type check), invalid email during resume, invalid email during submit, `__cause__` preservation, and valid-email behavior unchanged.
- `TestControlFlowExceptionsNotSwallowed` (2 tests): `KeyboardInterrupt` and `SystemExit` injected from the persistence port both propagate unchanged.

Fake-backed orchestration unit tests grew from 36 to 52 (plus the pre-existing, environment-gated disposable-database smoke test, unchanged).

## 26. Tests executed

```
python -m pytest tests/test_scenario_orchestration_v2.py tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q
```

## 27. Test results

`524 passed, 9 subtests passed` (the prior `508`-test/`9`-subtest state plus the 16 new regression tests; zero failures, zero regressions, zero skips beyond the pre-existing environment-gated smoke test class which continued to run since Docker remained available).

## 28. Additional probes executed

A temporary script, `_tmp_correction_probes.py`, was created at the repository root and run standalone (not via `pytest`) to independently reproduce both fixes outside the permanent test suite:

- **Probe 1** (non-mapping decision element): seeded a fake persistence attempt whose `decisions` array is `[123]`, called `resume_and_replay_scenario_run_v2`, and confirmed the result was `ScenarioOrchestrationV2MalformedPersistenceResponseError` (not a raw `TypeError`). Output: `PROBE1_NONMAPPING_DECISION DOMAIN_ERROR_OK`.
- **Probe 2** (invalid email): called `start_or_resume_scenario_run_v2` with `user_email="not-an-email"`, confirmed the result was `ScenarioOrchestrationV2InvalidRequestError` (not a raw `ScenarioPersistenceValidationError`), and confirmed `exc.__cause__` was exactly `ScenarioPersistenceValidationError`. Output: `PROBE2_INVALID_EMAIL DOMAIN_ERROR_OK CAUSE= ScenarioPersistenceValidationError`.

## 29. Temporary artifacts removed

`_tmp_correction_probes.py` was deleted immediately after both probes printed their results; confirmed absent afterward via a filesystem search and via `git status --short --branch` (it never appeared as an untracked file in the final status).

## 30. Engine V1 regression result

`python -m pytest tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q` (run as part of the combined command in Sections 9 and 26) passed unmodified both before and after this correction pass — same pass count, same assertions, no Engine V1 file touched.

## 31. Database connection made

None. No Docker container, PostgreSQL instance, or Supabase project was created, started, or connected to during this correction task (the pre-existing, environment-gated disposable-smoke test class in the test file was not re-run standalone; it is exercised only as part of the full pytest command in Sections 9/26, where it continued to pass because Docker remained available in this environment — no new database interaction was introduced by this task's edits).

## 32. SQL/migration modified

None.

## 33. Controller/UI modified

None.

## 34. Files modified outside scope

None. Only the three files listed in Section 2 were modified, plus the one new file listed in Section 3.

## 35. Protected paths untouched

Confirmed via `git status --short --branch` before and after this task: `.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`, `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`, `workers/combined_policy_evaluator.py`, `v68_corrected_review_bundle/`, `v68_final_review_bundle/`, and `v68_review_bundle/` all remain in the exact same untracked state as the starting status; none of their contents were read, searched, executed, or modified.

## 36. Nothing staged, committed, pushed, or deployed

Confirmed: `git status --short --branch` shows only untracked files, no staged changes; no `git add`, `git commit`, or `git push` was ever run.

## 37. Errors encountered

None requiring architecture changes. One internal drafting correction: an initial docstring draft for MEDIUM-02 conflated the V69 `attempt_id_conflict` and `attempt_id_collision` error prefixes (which represent two distinct fail-closed scenarios — an existing-attempt CAS-style conflict versus a cross-owner id collision, respectively, per `supabase/migrations/20260719140000_v69_scenario_v2_attempt_identity_support.sql` and `supabase/tests/v68_scenario_attempt_persistence_verification.sql`); this was corrected before finalizing so the docstring distinguishes both cases accurately.

## 38. Stop conditions encountered

None. Fixing the error contract did not require changing the persistence protocol; fixing invalid-email translation did not require changing Engine V1; RPC shapes did not need to change; no database access was required for the source-code corrections; no protected path needed inspection; all baseline and post-change tests passed without requiring any architecture change.

## 39. Remaining risks

Unchanged from the original implementation report's Section 50: the disposable-smoke `_PostgresOrchestrationPersistence` helper remains test-only scaffolding, and the RPC error-prefix map remains scoped to the two RPCs this module calls today (an unmapped future prefix still fails closed generically via `ScenarioOrchestrationV2PersistenceDependencyError`, which is safe but non-specific). No new risk was introduced by this correction pass; the three closed findings were the only outstanding actionable items from the focused review.

## 40. Git status

```
## main...origin/main [ahead 20]
?? .local/
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_CORRECTION_REPORT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_FOCUSED_REVIEW.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_IMPLEMENTATION_REPORT.md
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

## 41. Recommended next task

Wire a real Supabase-client-backed implementation of `ScenarioOrchestrationV2PersistencePort` (adapting `.rpc(...).execute()` calls to the same three-method protocol already proven against both the deterministic fake and the disposable-PostgreSQL smoke adapter), then integrate `start_or_resume_scenario_run_v2`/`submit_scenario_decision_v2` behind a new, separately reviewed Engine V2 controller — keeping that controller wiring fully isolated from the existing Engine V1 `scenario_learner_controller.py`.
