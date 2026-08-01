# SCENARIO_ENGINE_V2 Persistence Adapter — Implementation Report

**Task ID:** SIM-PERSIST-V2-04
**Model:** Sonnet High
**Baseline:** `6136673` — Complete Scenario Engine V2 vertical slice
**Scope:** Pure Python persistence/serialization adapter only. No database migration applied. No production connection. No Supabase SQL/RLS/policy/grant changes. No Engine V1 behavior change. No UI change. Nothing staged, committed, pushed, or deployed.

---

> ## ⚠ Superseded envelope shape — see correction addendum
>
> This report documents the **original** SIM-PERSIST-V2-04 implementation, including its envelope shape (`scenarioVersion`, string `status`, `attemptId`, `decisionCount` — 19 top-level keys). That shape was found to violate the load-bearing V68/V69 SQL contract by an independent focused review
> (`docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_FOCUSED_REVIEW.md`, SIM-PERSIST-V2-04-REVIEW-01: 2 BLOCKER/HIGH findings) and was **corrected** by SIM-PERSIST-V2-04B
> (`docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_CORRECTION_REPORT.md`).
>
> **The rest of this document is a historical record and no longer describes the current envelope shape.** The current, frozen, authoritative envelope contract is:
>
> ```text
> envelopeVersion, simulationId, version, schemaVersion, engineVersion,
> canonicalContentSha256, currentSceneId, expectedSequenceNumber, isComplete,
> state, counters, flags, decisionHistory, optionDisplayOrderByScene,
> selectedVariantIdByScene, routingResolutions, terminalResult
> ```
>
> — exactly 17 keys. `scenarioVersion` → `version`; string `status` → boolean `isComplete`; `attemptId` and `decisionCount` removed entirely (attempt identity is supplied out-of-band as a trusted `attempt_row_id` parameter to `verify_persisted_attempt_identity_v2`/`replay_serialized_run_v2`, never carried inside the JSONB envelope). See the correction report for the full finding-by-finding disposition, updated public-API signatures, and updated test coverage (447 → 471 focused tests).

---

## 1. Task status

**COMPLETE.** All pre-flight checks passed, the adapter and its test suite were implemented per the reviewed contract, and the full required test command passes with `447 passed` (the required `387`-test Engine V1/V2 baseline plus `60` new focused adapter tests), with zero regressions.

## 2. Files created

- `utils/scenario_persistence_v2.py` — the Engine V2 persistence/serialization adapter.
- `tests/test_scenario_persistence_v2.py` — focused adapter tests (60 tests, covering lettered requirements A–AX).
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_IMPLEMENTATION_REPORT.md` (this file).

## 3. Files modified

**None.** `utils/scenario_persistence.py` was read-only inspected and reused (by direct import of its two already-public, engine-agnostic primitives, `generate_idempotency_key` and `compute_request_fingerprint` — endorsed explicitly by the SIM-PERSIST-V2-01 design document's own section 16), never edited. No other existing file was modified.

## 4. Repository branch

`main`.

## 5. HEAD

`6136673` — "Complete Scenario Engine V2 vertical slice" (confirmed via `git log -1 --oneline` before and after this task; unchanged throughout, since no commit was made).

## 6. Starting git status

```
## main...origin/main [ahead 19]
 M supabase/tests/v68_scenario_attempt_persistence_verification.sql
?? .local/
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CORRECTION_REPORT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_FINAL_REVIEW.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_MIGRATION_DRAFT.sql
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_ROLLBACK_DRAFT.sql
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_SECURITY_REVIEW.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_B_DB_VALIDATION_REPORT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_B_ROLLBACK.sql
?? local_only/
?? scripts/v58_run_combined_policy_evaluation.py
?? structural_audit_state.json
?? supabase/migrations/20260719140000_v69_scenario_v2_attempt_identity_support.sql
?? tests/test_combined_policy_evaluator.py
?? v68_corrected_review_bundle/
?? v68_final_review_bundle/
?? v68_review_bundle/
?? workers/combined_policy_evaluator.py
```

Nothing was staged. The validated V69 SQL files (migration, rollback, DB validation report, updated V68 verification script) were present, untracked/modified, and untouched by this task, exactly as expected from the prior `SIM-PERSIST-V2-03` task.

## 7. Ending git status

Identical to the starting status, plus exactly two new untracked files this task created (`utils/scenario_persistence_v2.py`, `tests/test_scenario_persistence_v2.py`) and this report (`docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_IMPLEMENTATION_REPORT.md`). Nothing staged, committed, pushed, or deployed. All previously-untracked files (`.local/`, `local_only/`, the protected Python files, the V69 SQL/design documents) remain exactly as they were.

## 8. Shell pre-flight result

Shell responded normally throughout. `git status --short --branch` and `git log -1 --oneline` both returned real, immediate output confirming branch `main`, HEAD `6136673`, nothing staged, and the V69 SQL files present but untouched.

## 9. Baseline tests executed

```
python -m pytest tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q
```

## 10. Baseline test results

`387 passed` (confirmed before any file was created or modified).

## 11. Adapter public API

All twelve required functions were implemented in `utils/scenario_persistence_v2.py`:

1. `serialize_run_snapshot_v2(run: ScenarioRunV2Snapshot) -> Dict[str, Any]`
2. `deserialize_run_snapshot_v2(payload: Mapping[str, Any]) -> PersistedRunEnvelopeV2`
3. `serialize_decision_input_v2(decision: ScenarioDecisionInputV2) -> Dict[str, Any]`
4. `deserialize_decision_input_v2(payload: Mapping[str, Any]) -> ScenarioDecisionInputV2`
5. `serialize_learner_scene_view_v2(view: LearnerSceneView) -> Dict[str, Any]`
6. `serialize_learner_terminal_view_v2(view: LearnerTerminalView) -> Dict[str, Any]`
7. `replay_serialized_run_v2(content, *, attempt_row_id, attempt_row_engine_version, attempt_row_scenario_content_sha256, canonical_decision_rows, cached_envelope_payload) -> ScenarioRunV2Snapshot`
8. `verify_persisted_attempt_identity_v2(content, *, attempt_row_id, attempt_row_engine_version, attempt_row_scenario_content_sha256, envelope) -> None`
9. `build_start_or_resume_rpc_params_v2(run, *, user_email, scenario_version_id) -> Dict[str, Any]`
10. `parse_start_or_resume_rpc_response_v2(data, *, expected_attempt_id=None) -> StartOrResumeRpcResultV2`
11. `build_submit_decision_rpc_params_v2(run_before, run_after, decision, *, user_email, idempotency_key=None) -> Dict[str, Any]`
12. `parse_submit_decision_rpc_response_v2(data, *, expected_attempt_id=None) -> SubmitDecisionRpcResultV2`

Every function uses actual Engine V2 type names/field names (`ScenarioRunV2Snapshot`, `ScenarioDecisionInputV2`, `LearnerSceneView`, `LearnerTerminalView`) imported directly from `utils/scenario_engine_v2.py` — no duplicate runtime model was invented. Two additional small, JSON-oriented dataclasses were introduced only where the engine has no equivalent already-public type: `TerminalSummaryV2` (the minimal 4-field terminal summary) and `RoutingResolutionRecordV2` (the minimal 4-field routing-audit record, deliberately narrower than the engine's own `RoutingResolutionEvent`, which also carries `scene_id`/`option_id`).

## 12. Snapshot-envelope implementation

`envelopeVersion` 1 is a fixed, exactly-19-key JSON object (`envelopeVersion`, `attemptId`, `simulationId`, `scenarioVersion`, `schemaVersion`, `engineVersion`, `canonicalContentSha256`, `expectedSequenceNumber`, `currentSceneId`, `status`, `decisionCount`, `decisionHistory`, `optionDisplayOrderByScene`, `selectedVariantIdByScene`, `routingResolutions`, `state`, `counters`, `flags`, `terminalResult`) — matching every field the task's ARCHITECTURE RULES §2 lists. `deserialize_run_snapshot_v2` rejects any unknown top-level key and any missing required key (tests C, D, E, AJ). An unsupported `envelopeVersion` fails closed with `unsupported_envelope_version:` (test D).

## 13. decisionHistory implementation

Each `decisionHistory` element is a mandatory field-subset projection of Engine V2's own `DebriefTraceEntry` down to **exactly** `{sequenceNumber, sceneId, optionId}` — `serialize_run_snapshot_v2` never serializes a `DebriefTraceEntry`'s `__dict__`/`dataclasses.asdict()` output; it builds the three-key dict explicitly. `deserialize_decision_input_v2` (used both for standalone decision inputs and for every `decisionHistory` element) hard-rejects any of the twelve excluded fields (`evaluationTier`, `debriefSeed`, `stateDelta`, `stateAfter`, `flagsCleared`, `flagsSet`, `nextSceneId`, `enteredCorrective`, `skippedCorrective`, `presentedDialogueVariantId`, `nextDialogueVariantId`, `competencyTags`) with an `unexpected_field:`-prefixed error — enforced on both serialization (mandatory projection, test U/U2) and deserialization (hard rejection, tests V/W/X/Y).

## 14. Strict-type implementation

- Sequence numbers, `expectedSequenceNumber`, `decisionCount`, and counter values all use `type(value) is int`-equivalent checks (`isinstance(value, bool) or not isinstance(value, int)` → reject) — `bool`, `float`, numeric strings, and `None` are all rejected (tests J, K).
- `state` values must be `type(value) in (int, float)` and `math.isfinite(value)` — `NaN`/`Infinity`/`-Infinity` rejected on both the serialization boundary (defensive assertion) and the deserialization boundary (untrusted-input rejection) (tests L, M, N).
- Every persisted/returned structure is asserted JSON-native via a recursive `_assert_json_native` walk inside `serialize_run_snapshot_v2` — this is a defensive boundary check, not the primary validation mechanism (per the task's explicit instruction not to rely on `json.dumps` errors as the domain contract).

## 15. UUID implementation

Attempt IDs and idempotency keys are validated via `uuid.UUID(...)` parsing and always serialized as canonical lowercase strings (test R). A nil UUID (`00000000-0000-0000-0000-000000000000`) is rejected specifically wherever a *new* attempt identity is required (`build_start_or_resume_rpc_params_v2`, via `allow_nil=False`) (test S). Idempotency keys are required to be UUID version 4 specifically, mirroring `utils/scenario_persistence.py`'s own `_require_uuid4_str` convention (test T). No function ever discloses whether a colliding UUID exists elsewhere — collision handling is exclusively the RPC/SQL layer's job (already validated in `SIM-PERSIST-V2-03`), not this adapter's.

## 16. Serialization behavior

`serialize_run_snapshot_v2` performs only type conversion (`frozenset[str]` → sorted `list[str]` for `flags`; `Mapping[str, tuple[str, ...]]` → `dict[str, list[str]]` for `optionDisplayOrderByScene`; `Mapping[str, float]`/`Mapping[str, int]` → plain `dict`) plus the mandatory `decisionHistory` projection and a defensive finite-number assertion — it never re-validates the engine's own invariants. `serialize_decision_input_v2` re-validates strictly on the way out rather than trusting the in-memory dataclass. `serialize_learner_scene_view_v2`/`serialize_learner_terminal_view_v2` read only from the already-learner-safe `LearnerSceneView`/`LearnerTerminalView` views — never from `run`/`scene`/`option` directly — so they are structurally incapable of leaking a field those views already exclude.

## 17. Deserialization behavior

`deserialize_run_snapshot_v2` rejects: unknown top-level keys (test AJ), missing required fields (test E), wrong JSON types, nonfinite values (tests L/M/N), extra fields inside `decisionHistory` elements (delegated to `deserialize_decision_input_v2`, tests V–Y), malformed `optionDisplayOrderByScene` (wrong types, duplicate option ids — test AI), terminal fields present on an `in_progress` attempt (test AG), and missing terminal fields on a `completed` attempt (test AH). It returns an immutable `PersistedRunEnvelopeV2` (`MappingProxyType`-wrapped mappings, tuples for ordered sequences, `frozenset` for flags) and never mutates the input `Mapping` (test AL).

## 18. Identity-verification behavior

`verify_persisted_attempt_identity_v2` compares trusted **database columns** (`attempt_row_id`, `attempt_row_engine_version`, `attempt_row_scenario_content_sha256`) — never the envelope's own copies — against the freshly loaded content, by delegating to the existing, unmodified `verify_replay_identity_v2(...)` (tests F, I). It additionally cross-checks the envelope's own `canonicalContentSha256`/`engineVersion`/`attemptId` copies against those same trusted columns, so a corrupted/stale envelope copy can never mask a real drift (tests H, G). Any mismatch raises `ScenarioPersistenceV2IdentityError`, a persistence-specific domain exception, fail-closed.

## 19. Replay behavior

`replay_serialized_run_v2` performs the exact required sequence: (1) deserialize + validate the cached envelope's shape; (2) verify trusted persisted identity; (3) deserialize the **canonical** `scenario_decisions` rows (never the envelope's own `decisionHistory`); (4) call the existing, unmodified `replay_scenario_run_v2(...)` from immutable content; (5) recompute the full envelope from the freshly replayed run; (6) compare every non-terminal cache field against the persisted envelope, failing closed with `ScenarioPersistenceV2CacheMismatchError` on any disagreement (tests AB, AC, AD); (7) separately compare the completed outcome against the persisted terminal summary, failing closed with `ScenarioPersistenceV2TerminalMismatchError` (test AE); (8) return the recomputed, authoritative `ScenarioRunV2Snapshot` (tests AF, AF2). Test Z proves canonical decision rows — not the envelope's own (possibly stale) `decisionHistory` — drive reconstruction: a deliberately mismatched envelope `decisionHistory` is caught as a cache disagreement rather than silently accepted as the replay input. Test AA proves a corrupted cached `state` value never reaches the engine as an input (the only observable effect is a focused domain error, never an engine-internal crash).

## 20. Cache-comparison behavior

Comparison is exact, canonical-JSON-shape equality (`==`) on already-normalized plain `dict`/`list`/`str`/`int`/`float`/`bool`/`None` values — never Python object identity, never dependent on dictionary insertion order (Python's own `dict.__eq__` is already order-independent), and never a numeric tolerance. This is documented explicitly in the module's own docstring: Engine V2's state arithmetic is deterministic IEEE-754 double-precision arithmetic given identical inputs, so an exact comparison is correct and safest — a tolerance could silently hide a genuine score/state divergence. Ordered fields (`decisionHistory`, `optionDisplayOrderByScene` values, `routingResolutions`) are compared positionally (as JSON lists); unordered fields (`state`, `counters`) are compared as plain dicts; `flags` is always emitted as a sorted list, so two logically-identical flag sets always compare equal regardless of the underlying `frozenset`'s arbitrary iteration order (test P).

## 21. Option-order verification

`optionDisplayOrderByScene` is included in the persisted envelope's cache-comparison field set (`_NON_TERMINAL_CACHE_KEYS`); replay always recomputes it fresh via the engine's own `replay_scenario_run_v2` → `resolve_option_display_order`/`deterministic_option_display_order`, and a corrupted/reordered cached value is caught as a cache mismatch (test AC), never silently trusted or used to reconstruct. Duplicate option ids within one scene's cached order are rejected structurally at deserialization time (test AI), independent of replay.

## 22. Terminal-outcome verification

Handled as a distinct step, separate from the general cache comparison, using its own dedicated exception class (`ScenarioPersistenceV2TerminalMismatchError`) so callers can distinguish "the engine's current state disagrees" from "the recorded final outcome disagrees" (tests AE, AF).

## 23. Start RPC parameter result

`build_start_or_resume_rpc_params_v2` produces exactly the seven named JSON arguments the validated V69 RPC expects (`p_user_email`, `p_scenario_version_id`, `p_initial_current_scene_id`, `p_initial_serialized_state`, `p_engine_version`, `p_scenario_content_sha256`, `p_attempt_id`) — verified by test AN (`len(params) == 7`, exact key set). `p_attempt_id` is always the same UUID string already bound to `run.attempt_id` (canonicalized), never re-minted (test AO). No hidden state is sent outside `p_initial_serialized_state`; no extra parameters exist.

## 24. Submit RPC parameter result

`build_submit_decision_rpc_params_v2` reuses the existing, unmodified `submit_scenario_decision_v1` RPC's validated 13-key contract exactly (`p_user_email`, `p_attempt_id`, `p_idempotency_key`, `p_expected_sequence_number`, `p_expected_scene_id`, `p_selected_option_id`, `p_request_fingerprint`, `p_state_before`, `p_state_after`, `p_is_terminal`, `p_resulting_scene_id`, `p_terminal_ending_id`, `p_terminal_result_snapshot`) — every field is derived from the two server-computed run snapshots (`run_before`/`run_after`) and the already-applied `decision`; there is no parameter, and no code path, through which a caller could inject a client-supplied tier, routing, state, flags, score, or outcome (test AP). The request fingerprint is computed by direct reuse of `utils.scenario_persistence.compute_request_fingerprint(...)`, per the SIM-PERSIST-V2-01 design document's own explicit reuse recommendation.

## 25. RPC response parsing

Both `parse_start_or_resume_rpc_response_v2` and `parse_submit_decision_rpc_response_v2` validate: exactly one row (reject empty — test AQ; reject multi-row — test AR), every required field present and correctly typed (reject missing — test AS; reject wrong type — test AT), identity match against an optionally-supplied expected attempt id, engine-version compatibility (via the returned `engine_version` field and/or the embedded `serialized_engine_state.engineVersion`), and a recognized lifecycle status. Both return typed, frozen dataclasses (`StartOrResumeRpcResultV2`, `SubmitDecisionRpcResultV2`) — no raw Supabase dict is ever returned to a caller.

## 26. Learner-scene serialization

`serialize_learner_scene_view_v2` mirrors `LearnerSceneView` field-for-field, thawing any `MappingProxyType`/tuple substructure (`progressMetadata`, `accessibility`, `mobilePresentation`, `dialogueExchanges`) into plain `dict`/`list`. A recursive key-walk test (AV) confirms none of the eighteen forbidden hidden-field names appear anywhere in the output.

## 27. Learner-terminal serialization

`serialize_learner_terminal_view_v2` produces exactly four keys (`outcomeId`, `outcomeTitle`, `narrative`, `displayScore`) — verified by test AW's exact-key-set assertion plus the same recursive forbidden-field walk.

## 28. Domain-error contract

Seven focused exception classes were introduced, all subclassing `ScenarioPersistenceV2Error`: `ScenarioPersistenceV2SerializationError`, `ScenarioPersistenceV2ValidationError`, `ScenarioPersistenceV2IdentityError`, `ScenarioPersistenceV2CacheMismatchError`, `ScenarioPersistenceV2TerminalMismatchError`, `ScenarioPersistenceV2RpcResponseError`. No raw `KeyError`/`TypeError`/`ValueError`/`AttributeError`/`json.JSONDecodeError`/dataclass-conversion error escapes any public function uncaught — every failure path in this module raises one of the above. Engine V2's own exception types (`ScenarioReplayV2Error`, `ScenarioRunStateV2Error`) are deliberately re-raised unchanged from inside `replay_serialized_run_v2`'s delegated calls (mirroring the reviewed Slice A contract §8.7) — they are already focused, hardened domain errors, not raw/ambiguous builtins.

## 29. Content immutability

`ScenarioContentV2` is never mutated by any function in this module — every read is a plain attribute/key access, and `content` is a frozen dataclass with deep-frozen (`MappingProxyType`/tuple) substructures at the engine layer already.

## 30. Snapshot immutability

Test AK explicitly captures `run.state`/`run.counters`/`run.flags`/`run.decisions`/`run.option_display_order_by_scene` by value before calling `serialize_run_snapshot_v2` and asserts they are unchanged afterward. This is also structurally guaranteed: `ScenarioRunV2Snapshot` is a frozen dataclass whose every container field is already immutable (`MappingProxyType`/`tuple`/`frozenset`).

## 31. Decision-row immutability

Test AM captures a deep copy of both the untrusted `canonical_decision_rows` list and the `cached_envelope_payload` dict before calling `replay_serialized_run_v2` and asserts both are unchanged afterward. No function in this module ever assigns back into an input `Mapping`/`list`.

## 32. Engine V1 isolation

`utils/scenario_persistence.py` was not modified. This module reuses exactly two already-public functions from it (`generate_idempotency_key`, `compute_request_fingerprint`) via direct import — a one-directional dependency the SIM-PERSIST-V2-01 design document's own section 16 explicitly endorses. Test AU statically confirms `utils/scenario_persistence.py`'s own source text contains no reference to `scenario_persistence_v2` (i.e., V1 never imports V2). Test AU2 confirms V1's own public surface (`start_or_resume_attempt`, `submit_decision`, `REQUIRED_SERIALIZED_STATE_KEYS`) is completely unchanged.

## 33. Tests created

`tests/test_scenario_persistence_v2.py` — 60 tests across 14 `TestCase` classes, covering every lettered item A through AX (several letters have one primary test plus a closely related secondary test, e.g. `test_U`/`test_U2`, `test_AF`/`test_AF2`, `test_O`/`test_O2`, `test_AP`/`test_AP2`, and additional non-lettered RPC-response happy-path/edge-case tests for completeness).

## 34. Tests executed

```
python -m pytest tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q
```

## 35. Test results

`447 passed` (387 pre-existing + 60 new), zero failures, zero errors, zero skips (all `skipTest` guard conditions were not triggered against the actual fixture).

## 36. Engine V1 regression result

**Zero regressions.** All tests in `tests/test_scenario_persistence.py` and `tests/test_scenario_learner_controller.py` (the two Engine-V1-facing suites) pass identically to the pre-work baseline.

## 37. Database connection made

**No.** This module never instantiates a Supabase client, never calls `client.rpc(...)`, never reads an environment variable, and never opens a PostgreSQL connection. Every RPC-response test in `tests/test_scenario_persistence_v2.py` uses a hand-constructed, in-memory dict/list standing in for a Supabase response — no network or database I/O occurs anywhere in this task.

## 38. SQL/migration modified

**No.** No file under `supabase/` was created, modified, or applied.

## 39. UI modified

**No.** No file under `pages/`, `components/`, or any Streamlit UI module was touched.

## 40. Risky areas touched

None beyond the two new, additive files. No shared/existing file was modified.

## 41. Files modified outside intended scope

**None.**

## 42. Protected paths untouched

Confirmed. No file under `.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`, `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`, `workers/combined_policy_evaluator.py`, `v68_corrected_review_bundle/`, `v68_final_review_bundle/`, or `v68_review_bundle/` was inspected, opened, searched, executed, modified, staged, or referenced by this task.

## 43. Nothing staged, committed, pushed, or deployed

Confirmed — only `Read`/`Grep`/`Write`/`Shell` (read-only `git status`/`git log`/`pytest`/smoke-test) tool calls were made. No `git add`/`git commit`/`git push`/deployment command was issued.

## 44. Errors encountered

None. The shell was responsive throughout; every command returned a real exit status on the first attempt.

## 45. Stop conditions encountered

**None.** The reviewed envelope contract did not conflict with actual Engine V2 types (confirmed by direct inspection of `utils/scenario_engine_v2.py`, matching the SIM-PERSIST-V2-02C correction's own independently-verified `DebriefTraceEntry`/`ScenarioRunV2Snapshot` field lists exactly). The required submit-RPC parameters were derivable without any change to the existing RPC. Exact replay cache comparison was fully specifiable without any new persistence-schema decision (exact equality after canonical serialization, documented explicitly). Existing Engine V1 persistence required no change. No database connection became necessary. No protected path needed inspection.

## 46. Remaining risks

1. **Not yet exercised against a live/disposable database.** This task is explicitly scoped to pure-Python construction/parsing only (per its own "NO DATABASE CALLS" rule) — the RPC parameter shapes and response-parsing logic have not been exercised against a real PostgREST/Supabase RPC call. The prior task (`SIM-PERSIST-V2-03`) validated the underlying SQL contract (the V69 migration) directly; a follow-on task should exercise this Python adapter's `build_start_or_resume_rpc_params_v2`/`parse_start_or_resume_rpc_response_v2` pair (and the submit equivalents) against that same disposable database to close the loop end-to-end.
2. **No learner controller / orchestration layer yet.** This task deliberately does not implement the Engine-V2-facing equivalent of `utils/scenario_learner_controller.py`'s prepare/submit pattern (start/resume workflow, decision-submission orchestration) — that is explicitly out of scope here and remains the natural next implementation slice.
3. **`selectedVariantIdByScene`/`routingResolutions` are always emitted (not optional).** The reviewed Slice A contract (§19.2 of the schema spec) marks these as optional; this implementation always includes them (as empty dict/list when there is nothing yet to report) for a simpler, uniform envelope shape and simpler validation, consistent with the design document's own §23 remaining-uncertainty note that this exact choice was deferred to Slice D implementation time.
4. **Envelope `scenarioVersion` field naming.** The task's own ARCHITECTURE RULES §2 names this field `scenarioVersion`; the SIM-PERSIST-V2-01/02 design documents' own illustrative envelope JSON uses the bare key `version` for the same underlying value (`content.version`). This implementation follows the current task's explicit, authoritative field list (`scenarioVersion`) since it is the newest and most specific instruction; this is noted here in case a future task reconciling this adapter against the original SQL-facing design documents needs to be aware of the naming difference (it does not affect the RPC contract itself, since the envelope is an opaque JSONB blob to SQL).

## 47. Git status

```
## main...origin/main [ahead 19]
 M supabase/tests/v68_scenario_attempt_persistence_verification.sql
?? .local/
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_ADAPTER_IMPLEMENTATION_REPORT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CORRECTION_REPORT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_FINAL_REVIEW.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_MIGRATION_DRAFT.sql
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_ROLLBACK_DRAFT.sql
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_SECURITY_REVIEW.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_B_DB_VALIDATION_REPORT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_B_ROLLBACK.sql
?? local_only/
?? scripts/v58_run_combined_policy_evaluation.py
?? structural_audit_state.json
?? supabase/migrations/20260719140000_v69_scenario_v2_attempt_identity_support.sql
?? tests/test_combined_policy_evaluator.py
?? tests/test_scenario_persistence_v2.py
?? utils/scenario_persistence_v2.py
?? v68_corrected_review_bundle/
?? v68_final_review_bundle/
?? v68_review_bundle/
?? workers/combined_policy_evaluator.py
```

Nothing staged. Nothing committed. Nothing pushed. Nothing deployed.

## 48. Recommended next task

**SIM-PERSIST-V2-05 (proposed): Engine-V2-facing start/resume and decision-submission services**, mirroring `utils/scenario_learner_controller.py`'s existing prepare/submit pattern for Engine V1: implement the thin orchestration layer that (a) resolves published/pinned scenario content, (b) calls `start_scenario_run_v2`/`replay_scenario_run_v2` as appropriate, (c) calls this task's adapter functions to build RPC parameters, (d) actually invokes the Supabase RPC client (the first point in this whole persistence effort where a real database call occurs), and (e) parses the response back through this adapter's typed result objects. This should be validated end-to-end against the same disposable database `SIM-PERSIST-V2-03` already set up and tore down, closing remaining risk #1 above.
