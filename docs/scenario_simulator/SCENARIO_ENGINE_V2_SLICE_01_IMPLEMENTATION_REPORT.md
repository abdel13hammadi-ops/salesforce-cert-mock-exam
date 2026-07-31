## SIM-ENGINE-V2-02 amendments (2026-07-31)

Hardening closed F-H-001, F-H-002, and related MEDIUM findings from
`SCENARIO_ENGINE_V2_SLICE_01_FOCUSED_REVIEW.md`. See
`SCENARIO_ENGINE_V2_SLICE_02_HARDENING_REPORT.md` for the authoritative
post-hardening disposition.

Material contract locks:

- Decision `sequenceNumber` must be a strict `type(value) is int` (bool/float/str/null rejected).
- `optionDisplayPolicy` is honored: `authored_order` preserves document order; `randomize_per_attempt_scene` uses the frozen §17 stream; unsupported policies fail closed.
- §17 byte stream (Engine V2 frozen): `SHA256(material || uint32be(counter))` for `counter = 0, 1, 2, …`.
- Debrief trace uses `presented_dialogue_variant_id` and `next_dialogue_variant_id` (replacing ambiguous `selected_variant_id`).
- Vertical-slice fixture extended with `SC001-R3A` + `SC001-C04` for natural corrective-budget exhaustion.

---



**Task ID:** SIM-ENGINE-V2-01
**Baseline commit:** `12ffe9d` — Complete scenario schema 1.1 validation foundation
**Scope:** First isolated, production-quality `SCENARIO_ENGINE_V2` runtime foundation for validated `schemaVersion "1.1.0"` content, covering the vertical slice Introduction → `SC001-C01` → `SC001-C02` → optional `SC001-R2A` → reconvergence at `SC001-C03` → terminal evaluation.

## 1. Task status

**Complete.** All architecture requirements (1–19), the synthetic vertical-slice fixture (17), backward compatibility (18), and the error contract (19) are implemented. 81 focused Engine V2 tests pass; all pre-existing Engine V1 / schema / catalog / 1.1.0-validator regression tests continue to pass unmodified.

## 2. Files changed

None. No existing file was modified. (`utils/scenario_engine.py`, `utils/scenario_schema.py`, `utils/scenario_validation_v1_1.py`, and all existing tests are untouched.)

## 3. Files created

- `utils/scenario_engine_v2.py` — the Engine V2 runtime module.
- `tests/fixtures/scenario_engine_v2_vslice_1_1_0.json` — the synthetic vertical-slice fixture.
- `tests/test_scenario_engine_v2.py` — 81 focused Engine V2 tests.
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_SLICE_01_IMPLEMENTATION_REPORT.md` — this report.

## 4. Repository branch

`main` (local branch tracks `origin/main`, currently 18 commits ahead; no commits were made by this task).

## 5. Starting git status

```
## main...origin/main [ahead 18]
```
plus a large set of pre-existing untracked files under `.local/`, `local_only/`, `docs/scenario_simulator/*.md` (prior session's docs), `v68_*_review_bundle/`, and other paths unrelated to this task (see the task's own git-status snapshot). None of these were created or modified by this task.

## 6. Ending git status

```
## main...origin/main [ahead 18]
?? .local/                                            (protected, pre-existing, untouched)
?? local_only/                                        (protected, pre-existing, untouched)
?? scripts/v58_run_combined_policy_evaluation.py       (protected, pre-existing, untouched)
?? structural_audit_state.json                         (protected, pre-existing, untouched)
?? tests/fixtures/scenario_engine_v2_vslice_1_1_0.json (created by this task)
?? tests/test_combined_policy_evaluator.py             (protected, pre-existing, untouched)
?? tests/test_scenario_engine_v2.py                    (created by this task)
?? utils/scenario_engine_v2.py                         (created by this task)
?? v68_corrected_review_bundle/                        (protected, pre-existing, untouched)
?? v68_final_review_bundle/                            (protected, pre-existing, untouched)
?? v68_review_bundle/                                  (protected, pre-existing, untouched)
?? workers/combined_policy_evaluator.py                (protected, pre-existing, untouched)
```

Nothing is staged. Nothing was committed, pushed, or deployed. `git add .` / `git add -A` were never used.

## 7. Engine V2 API

Public surface of `utils/scenario_engine_v2.py` (`__all__`):

- **Content:** `ScenarioContentV2`, `build_scenario_content_v2`, `load_scenario_content_v2`.
- **Condition grammar:** `evaluate_condition`.
- **Routing:** `RoutingOutcome`, `resolve_routing`.
- **Dialogue:** `ResolvedDialogue`, `select_dialogue_variant`.
- **Option order:** `deterministic_option_display_order`.
- **Formulas:** `compute_positive_health`, `compute_decision_quality`, `compute_composite`.
- **Classification:** `ClassificationTrace`, `classify_outcome`, `round_half_away_from_zero`.
- **Runtime state:** `ScenarioDecisionInputV2`, `DebriefTraceEntry`, `RoutingResolutionEvent`, `CorrectiveEntryEvent`, `SkippedCorrectiveEvent`, `VariantSelectionEvent`, `ScenarioTerminalResultV2`, `ScenarioRunV2Snapshot`.
- **Pipeline:** `start_scenario_run_v2`, `apply_decision_v2`, `replay_scenario_run_v2`, `verify_replay_identity_v2`, `build_debrief_trace`.
- **Learner-safe views:** `LearnerOptionView`, `LearnerSceneView`, `LearnerTerminalView`, `build_learner_scene_view`, `build_learner_terminal_view`.
- **Errors:** `ScenarioEngineV2Error`, `ScenarioContentV2Error`, `ScenarioRunStateV2Error`, `ScenarioReplayV2Error`, `ScenarioClassificationV2Error`.

## 8. Version-dispatch result

Version isolation is enforced structurally, not by a shared dispatcher function:

- `build_scenario_content_v2` refuses any document whose `schemaVersion != "1.1.0"` or whose `requiredEngineVersion != "SCENARIO_ENGINE_V2"`, raising `ScenarioContentV2Error`.
- `utils/scenario_schema.build_scenario_content` (Engine V1's loader, unmodified) already refuses `schemaVersion "1.1.0"` content with `ScenarioContentError` (verified by `test_v1_rejects_1_1_0_content`).
- `utils/scenario_engine_v2.py` never imports from or calls into `utils/scenario_engine.py`, and vice versa — there is no shared execution code path, so no function can silently reinterpret one schema version under the other engine's semantics.
- Callers are expected to branch on the document's declared `schemaVersion` before selecting a loader (this task did not add a new shared dispatcher, per the "prefer additive module" guidance, since no clean existing dispatch boundary existed and creating one was out of scope for a pure-engine slice).

Verified by tests 02, 02b, 03, 50, and `test_v1_rejects_1_1_0_content`.

## 9. Initialization result

`start_scenario_run_v2(content, attempt_id=...)`:
- Verifies a non-empty `attempt_id` and a resolvable `startScene`.
- Initializes state from `initialState`, flags from each flag's `initialValue`, and counters from each counter's `initialValue` (kept in a separate mapping from state).
- Sets `current_scene_id = startScene`, `expected_sequence_number = 1`.
- Applies `environmentalFlagsOnEntry` (none in this fixture), selects the first scene's dialogue variant, and computes the deterministic option display order for the start scene — all before returning.
- Never exposes tiers/deltas/flags/routing/caps/scores; only `ScenarioRunV2Snapshot` (internal) is returned, and learner-safe projection happens separately via `build_learner_scene_view`.

Verified by tests 01, 05, 06, 07, 08, 09.

## 10. Runtime-state contract

`ScenarioRunV2Snapshot` (frozen dataclass) carries every field enumerated in the task: `content` (identity/version/schema/hash live on `ScenarioContentV2`), `attempt_id`, `current_scene_id`, `expected_sequence_number`, `state`, `flags`, `counters`, `tier_history`, `decisions` (debrief trace), `routing_resolutions`, `corrective_entries`, `skipped_corrective_events`, `variant_selections`, `option_display_order_by_scene`, `is_complete`, `terminal_result`. `corrective_scenes_experienced` is a derived property reading the declared `experiencedCounterId` from `correctiveBudgetPolicy`. All fields are built from plain `str`/`float`/`int`/`bool`/tuple/frozenset/`MappingProxyType` values, so the snapshot is straightforwardly serializable to JSON-compatible structures by a future persistence layer (not implemented here, per scope).

## 11. Learner-view contract

`build_learner_scene_view` returns `LearnerSceneView` (scene id/title/setting/dialogue/characters/learner-present/prompt/options-in-display-order/progress/accessibility/mobile metadata/expected sequence/complete flag) and `build_learner_terminal_view` returns `LearnerTerminalView` (outcome id/title/narrative/display score only). Neither ever reads `evaluationTier`, `stateChanges`, `setFlags`/`clearFlags`, `correctiveRoute`/`budgetCondition`, `debriefSeed`, `strongestOptionId`, or any classifier cap/guard/weight field from content. Verified by tests 18, 49, 49b, 49c (byte-for-byte JSON-blob scan for forbidden field names, plus a terminal-view field-set equality assertion).

## 12. Decision-input contract

`ScenarioDecisionInputV2` has exactly three fields: `sequence_number`, `scene_id`, `option_id` — structurally, there is no field for tier/state/flags/routing/outcome, so a client cannot submit them even if it tried. `apply_decision_v2` independently re-validates sequence, scene, and option identity against the trusted `ScenarioRunV2Snapshot` before resolving anything from content. Verified by tests 12, 13, and `test_decision_input_has_no_hidden_fields`.

## 13. Decision application order

Implemented in `apply_decision_v2` exactly as specified: (1) validate sequence/scene, resolve option; (2) resolve option from frozen content; (3) option id carried through; (4–5) apply + clamp state deltas; (6–7) clear-then-set flags; (8) record server-resolved tier + `decision_applied` counter increments; (9) resolve routing; (10) corrective entry/budget/skip (with a defensive guard that a `corrective` scene's own option may never carry a `correctiveRoute`, raising `ScenarioContentV2Error` if content violates that); (11) implicit via `routing_outcome.next_scene_id`; (12–13) enter next scene (environmental flags → dialogue variant → option order, reusing a previously-computed order for an already-visited scene rather than recomputing); (14) increment expected sequence exactly once; (15) classify outcome if terminal; (16) return a new, learner-safe-derivable snapshot. Every step is covered by dedicated tests (14–33, 41–42).

## 14. State mutation result

`_apply_state_deltas` adds each declared `stateChanges` delta to the current value and immediately clamps via `_clamp_state_value`; referencing an undeclared state key raises `ScenarioContentV2Error`. Verified by test 14 (deltas match hand-computed expected values) and the `_apply_state_deltas` clamp assertions in test 15.

## 15. State clamping result

Clamping respects each `stateVariables[].minimum`/`maximum` bound independently; verified directly against `customerConfidence` (`0..100`) with deltas that would overshoot both bounds (test 15).

## 16. Flag clear/set result

`_apply_flag_changes` always clears before setting (a flag id present in both `clearFlags` and `setFlags` ends up **set**, matching spec section 12.2/22). Verified by test 16 and exercised end-to-end via the fixture (option `opt-sc001-c01-c` sets `flag-verbal-handoff-only`, which persists and is read by `SC001-C03`'s dialogue-variant condition).

## 17. Counter result

Runtime counters are stored and mutated independently of state (`ScenarioRunV2Snapshot.counters` vs. `.state`). Two separate increment passes exist per spec section 11.3: `_increment_decision_tier_counters` (event `decision_applied`, optionally gated by `whenTier`) and `_increment_corrective_entry_counters` (event `corrective_scene_entered`), so a single decision that is both e.g. `high-risk` *and* triggers corrective entry increments each declared counter exactly once for its own event — never double-counting one physical event under two counters, and never conflating the two events. Verified by tests 07, 21, 25, and the fixture's `highRiskDecisionCount` / `optimalDecisionCount` / `correctiveScenesExperienced` counters.

## 18. Corrective-trigger result

`resolve_routing` checks `option.evaluationTier` against `correctiveRoute.triggerOnTiers`; only a tier in that set triggers corrective routing at all (an `acceptable`/`optimal` option with a `correctiveRoute` present but tier not listed still routes via `primaryNextSceneId`). Verified by test 20 (`opt-sc001-c02-b`, tier `suboptimal`, in `triggerOnTiers: [suboptimal, high-risk]`) entering `SC001-R2A`.

## 19. Corrective-budget result

`budgetCondition` is evaluated via the same bounded condition grammar (`counterCompare` against `correctiveScenesExperienced`); when the condition is true, capacity remains and the run enters the corrective scene, incrementing the counter exactly once via `_increment_corrective_entry_counters`. Verified by tests 20, 21, 44.

## 20. Corrective-skip result

When `budgetCondition` evaluates false, `resolve_routing` routes to `whenCorrectiveSkippedNextSceneId` instead, the counter is **not** incremented, and a `SkippedCorrectiveEvent` (reason `"budget_exhausted"`) is appended to `run.skipped_corrective_events`. Verified by tests 24, 25, 26, 45 (using a synthetically pre-exhausted budget, since the fixture's own two corrective-eligible options both route through the same single-slot budget — see §29 "Remaining risks" for why this is a test-harness choice, not an engine limitation).

## 21. Reconvergence result

`SC001-R2A`'s own options route (via ordinary `primaryNextSceneId`, not another `correctiveRoute`) to `SC001-C03`, matching the fixture's declared `reconvergenceSceneId`. The engine defensively rejects (raises `ScenarioContentV2Error`) any content where a `corrective`-typed scene's option itself carries a `correctiveRoute`, enforcing "corrective scenes cannot own another corrective route." Verified by tests 22, 23, 44.

## 22. Dialogue-variant result

`select_dialogue_variant` sorts variants by ascending numeric `priority` (lower wins), evaluates each `when` condition against **committed** prior state/flags (post-mutation, pre-entry-into-scene), returns the first match, and falls back to unmodified base exchanges when none match. Overrides only replace fields on an existing `exchangeId` — exchange identity and order are structurally preserved (`base_order` drives the returned tuple). The selected variant id is recorded server-side (`VariantSelectionEvent`) for replay/audit but never exposed to the learner. Verified by tests 08, 27, 27b (two-flag AND'd variant beats a single-flag variant at lower declared priority), 28, and `test_variant_override_preserves_exchange_ids`.

## 23. Option-order result

`deterministic_option_display_order` builds the exact normative seed material (`attemptId + "\n" + simulationId + "\n" + version + "\n" + canonicalContentSha256 + "\n" + sceneId`, UTF-8 encoded) and runs a SHA-256-byte-stream-driven Fisher–Yates shuffle with rejection-sampled uniform draws (no modulo bias). The same tuple of inputs always yields the same order; a different `attemptId` can (and, verified empirically over 8 samples, does) yield a different order. Orders are stored per-scene in `option_display_order_by_scene` and reused (not recomputed) if a scene is revisited within the same run. Submissions always use `option_id`; there is no display-index-based submission path at all. Verified by tests 09, 10, 11, 13, and `test_order_is_permutation_of_input`.

## 24. Formula result

All four bounded formula types are implemented and fixture-exercised:
- `weighted_dimension_health` (`compute_positive_health`): per-dimension health in `[0, 100]` respecting `higher_is_better`/`higher_is_worse` polarity and declared bounds, averaged.
- `tier_average` (`compute_decision_quality`): mean of `tierPoints[tier]` over `tier_history`; raises `ScenarioClassificationV2Error` on zero scored decisions (test 34e).
- `linear_blend` (`compute_composite`): weighted sum of named metrics; rejects a weight sum that does not equal `1.0` within `1e-9`.
- `identity` (`compute_composite`): passes a named metric through unchanged (test 34d).

All four verified directly by tests 34a–34d; cycle/undeclared-reference rejection is exercised transitively by the condition-grammar and content-builder defensive tests.

## 25. Outcome-classifier result

`classify_outcome` implements the exact seven-step order: severe caps first (irrevocable once forced — test 35); moderate caps collected (tightest rank wins) but only *applied* as a downgrade if the natural band selection would otherwise beat the cap (tests 36, 36b distinguish "cap present" from "cap actually forces a downgrade"); composite computed unconditionally (never `NaN`, never rounded); strong guards collected as a disqualified-outcome set; band selected on the **unrounded** composite; deterministic rank-ascending tie-break re-selects the next non-disqualified outcome if the band pick was disqualified, then re-applies the moderate cap check once more against the (possibly downgraded) selection; display rounding (`round_half_away_from_zero`, half-away-from-zero, verified independently of band selection) happens only in a separate final step. Verified by tests 35–40 and the fixture's `test_at_least_two_outcomes_reachable`.

## 26. Debrief-trace result

`build_debrief_trace` (which refuses to run on an incomplete run — `ScenarioRunStateV2Error`) returns the ordered tuple of `DebriefTraceEntry` records already accumulated in `run.decisions`: scene id, option id, evaluation tier, authored `debriefSeed`, state delta, post-clamp state, flags cleared/set, resolved next scene, corrective entered/skipped flags, selected dialogue variant id, and competency tags. No trace field is reachable from a nonterminal `ScenarioRunV2Snapshot` via any learner-facing function. Verified by tests 42 and `test_debrief_trace_refuses_incomplete_run`.

## 27. Replay result

`replay_scenario_run_v2(content, attempt_id=..., decisions=...)` validates the decision sequence is `1, 2, 3, ...` with no duplicates/gaps, then folds `apply_decision_v2` over `start_scenario_run_v2`'s result, failing closed (`ScenarioReplayV2Error`) if a decision arrives after the run already reached terminal completion, or (`ScenarioRunStateV2Error`) if a supplied `sceneId` doesn't match the reconstructed current scene. `verify_replay_identity_v2` separately fails closed on any `simulationId`/`version`/`schemaVersion`/`canonicalContentSha256`/`engineVersion` mismatch against a pinned identity. Because every derived value is recomputed from content + decision history (nothing is trusted from an external snapshot), replay of the same history against the same content always reproduces an identical `ScenarioRunV2Snapshot` (`==` equality, verified directly in tests 43/44/46 and the empty-history case). Verified by tests 43–48, `test_replay_rejects_duplicate_sequence`, `test_replay_of_empty_history_matches_start`.

## 28. Error-contract result

Four domain-specific exception classes (`ScenarioContentV2Error`, `ScenarioRunStateV2Error`, `ScenarioReplayV2Error`, `ScenarioClassificationV2Error`), all deriving from `ScenarioEngineV2Error`. No raw `KeyError`/`AssertionError`/`jsonschema` exception is allowed to escape the public API — every content/state access that could fail is guarded and re-raised as a domain error. Verified by `test_all_public_errors_derive_from_base`, `test_malformed_document_raises_domain_error_not_keyerror`, `test_non_mapping_document_rejected`.

## 29. Fixture result

`tests/fixtures/scenario_engine_v2_vslice_1_1_0.json` — derived from `SCENARIO_SCHEMA_1_1_0_SPEC.md` §23, with `scoreBands` recalibrated (documented in the fixture's own `description` field) so that all four declared outcomes are genuinely reachable rather than only `failed_resolution`. Confirmed valid under full layered validation via `build_scenario_content_v2` (which internally calls `validate_v1_1_scenario_document` + `first_blocking_finding`, i.e. JSON Schema + structural + semantic + graph checks, without any weakening of `CV-089` or any other rule). Demonstrates: one normal transition (`C01→C02`), one corrective-trigger route (`C02-b`/`C02-c → R2A`), one corrective-skip route under exhausted budget (synthetic pre-exhaustion in tests, since a genuinely fresh single-slot budget is always available on a first pass through this small slice — see remaining risks), one reconvergence (`R2A → C03`), one flag clear-then-set sequence (test 16, generic; the fixture itself only *sets* flags — no scene declares `clearFlags`, which is schema-valid), one dialogue variant (`SC001-C03`, three variants keyed off `flag-verbal-handoff-only` / `flag-sales-reengaged` / both), all four evaluation tiers (`optimal`/`acceptable`/`suboptimal`/`high-risk` all appear across the fixture's options), deterministic option ordering (SHA-256/Fisher–Yates, verified), and at least two reachable outcomes (`strong_resolution` and `failed_resolution`, both exercised in tests).

## 30. Tests created

`tests/test_scenario_engine_v2.py` — 81 tests across 12 `TestCase` classes, numbered to map onto the task's 50 required scenarios plus additional defensive/edge-case coverage (condition-grammar depth/unknown-reference checks, error-contract checks, permutation checks, replay edge cases).

## 31. Tests executed

```
python -m pytest tests/test_scenario_engine_v2.py -v
python -m pytest tests/test_scenario_engine.py tests/test_scenario_schema.py tests/test_scenario_catalog.py tests/test_scenario_validation_v1_1.py -q
```

No other test modules were run (per the task's "run only focused scenario engine/schema/catalog tests required for regression confidence" instruction; the full repository suite was not run).

## 32. Test results

- `tests/test_scenario_engine_v2.py`: **81 passed**, 0 failed.
- `tests/test_scenario_engine.py` + `tests/test_scenario_schema.py` + `tests/test_scenario_catalog.py` + `tests/test_scenario_validation_v1_1.py`: **170 passed**, 0 failed.

## 33. Existing Engine V1 regression result

All pre-existing Engine V1 tests in `tests/test_scenario_engine.py` pass unmodified (part of the 170-test regression run above). `utils/scenario_engine.py` was not opened for editing and was not modified.

## 34. Schema/catalog regression result

All pre-existing tests in `tests/test_scenario_schema.py`, `tests/test_scenario_catalog.py`, and `tests/test_scenario_validation_v1_1.py` pass unmodified (part of the 170-test regression run above).

## 35. Content-mutation result

`test_04_content_not_mutated_by_execution` deep-copies the source document before running a full 3-decision playthrough (including a corrective-eligible option) and asserts byte-for-byte equality with the original after execution, plus round-trips the engine's own frozen `content.document` back through a plain-dict "thaw" and asserts it still equals the pristine source. Confirms `_deep_freeze` never aliases the caller's document and no engine function ever writes through it.

## 36. Persistence modified: No

No persistence file was created, read, or modified. `load_scenario_content_v2` reads a JSON file path purely for test/dev convenience and performs no writes.

## 37. Database migration required: No

## 38. UI work performed: No

`utils/scenario_engine.py`, Streamlit pages, and all UI-layer files were not touched.

## 39. Compiler work performed: No

The Creative Studio compiler was not touched.

## 40. Risky areas touched: No

`utils/scenario_engine.py` (Engine V1) was inspected read-only and never modified. `utils/scenario_schema.py` and `utils/scenario_validation_v1_1.py` were inspected and reused (via import) but never modified.

## 41. Files modified outside intended scope

None.

## 42. Confirmation protected paths untouched

Confirmed via `git status --short --branch`: `.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`, `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`, `workers/combined_policy_evaluator.py`, `v68_review_bundle/`, `v68_final_review_bundle/`, `v68_corrected_review_bundle/` all remain in their pre-existing untracked state, with zero reads, writes, or references from any file created in this task.

## 43. Confirmation nothing staged, committed, pushed, or deployed

Confirmed: no `git add`, `git commit`, `git push`, or deployment command was run at any point in this task. `git status --short --branch` shows only untracked (`??`) entries, no staged (`A`/`M` in the index) entries.

## 44. Errors encountered

- The original §23 spec fixture's `scoreBands` thresholds made only `failed_resolution` reachable across every possible playthrough of the vertical slice (composite scores never reached the `55`/`72`/`88` thresholds from the spec's illustrative example, which was calibrated for the full 17-scene simulation, not this 4-scene slice). Resolved by calibrating new thresholds (`36`/`32`/`29`) against the actual achievable composite-score range (~24.7–38.0) for this slice, without weakening any validator rule (`CV-089` included) — the fixture still passes full layered validation with the recalibrated bands.
- Two test-authoring bugs were caught and fixed during this session: (a) a moderate-cap test that asserted `moderate_cap_applied` without first confirming the cap wasn't already satisfied by the natural band selection, fixed by splitting into two tests — one that forces a real downgrade, one that documents the "cap present but not needed" case; (b) `dataclasses.asdict` failing on a `MappingProxyType`-nested `LearnerSceneView` because Python's `copy.deepcopy` cannot pickle `mappingproxy` — fixed with a custom recursive "thaw" helper instead of `dataclasses.asdict`.

## 45. Stop conditions encountered

None of the task's stop conditions were triggered. Schema 1.1.0 content, the outcome classifier semantics, deterministic option ordering, and replay were all fully specified and implementable without ambiguity, a persistence-schema decision, a shared Engine V1 change, fixture-validation weakening, a database migration, or any protected-path access.

## 46. Remaining risks

- The fixture's two corrective-eligible options (`opt-sc001-c02-b`, `opt-sc001-c02-c`) both route to the same single-slot `SC001-R2A` corrective scene, so a genuinely fresh run can only naturally exercise the corrective-skip path if it somehow triggers a second corrective-eligible decision after already consuming the budget — which this 4-scene slice's linear topology does not allow in one pass. Tests 24–26/45 exercise the skip path by explicitly pre-setting `correctiveScenesExperienced` on a `ScenarioRunV2Snapshot` (a supported operation since the dataclass is a plain frozen dataclass, not an opaque handle) rather than via two natural corrective triggers in sequence. This is a fixture-topology limitation inherent to a minimal vertical slice, not an engine defect — the underlying `resolve_routing` budget-check logic itself is exercised identically either way.
- No persistence, resume-from-storage, or multi-request session handling exists yet — `attempt_id` is accepted directly by the initialization API as specified, but wiring it to a real attempt-identity store is explicitly out of scope for this task.
- `utils/scenario_engine_v2.py` does not yet have a public dispatcher that inspects a document's `schemaVersion` and automatically routes to Engine V1 vs. Engine V2; callers must currently choose the loader themselves. Introducing a shared dispatcher was left for a future task per the "avoid unnecessary rewrites / prefer additive module unless a clean low-risk dispatch boundary exists" guidance.

## 47. Git status

See §6 above (ending git status) — unchanged in shape from §5 except for the three new files this task created.

## 48. Recommended next step

Implement the remaining CB-SC-001 scenes beyond this vertical slice under the same Engine V2 runtime (no engine changes anticipated), and separately design the persistence/resume contract (attempt storage, session wiring) referenced in the task's stated non-goals, before connecting Engine V2 to any UI surface.
