# SCENARIO_ENGINE_V2 Slice 01 — Focused Production-Readiness Review

**Task ID:** SIM-ENGINE-V2-REVIEW-01  
**Model:** Auto  
**Review type:** Independent, review-only (no source/test/fixture/schema modifications)  
**Baseline commit:** `12ffe9d` — Complete scenario schema 1.1 validation foundation  
**Implementation under review:** SIM-ENGINE-V2-01 (`utils/scenario_engine_v2.py` and companions)  
**Date:** 2026-07-31  

## Verdict

**Not yet ready for persistence/resume integration under the stated readiness standard.**

| Gate | Result |
|---|---|
| Blocker count | **0** |
| Unresolved HIGH count | **2** |
| Deterministic replay (same engine) | Verified |
| Invalid/stale submissions mutate state | No (probed) |
| Content immutable | Verified (probed) |
| Learner-view leakage | No (probed) |
| Corrective-budget behavior | Correct |
| Classifier seven-step order | Matches normative contract |
| Engine V1 unchanged | Confirmed |
| Persistence/UI coupling | None |

Persistence/resume may proceed **only after** the two HIGH findings below are corrected (or explicitly accepted with a narrower scope waiver). Full CB-SC-001 integration has additional follow-ups (MEDIUM/fixture topology). Eventual multi-runtime production should also lock the §17 byte-stream interpretation (MEDIUM).

---

## Summary counts

| Severity | Count |
|---|---|
| BLOCKER | 0 |
| HIGH | 2 |
| MEDIUM | 7 |
| LOW | 5 |
| NOTE | 5 |
| **Total findings** | **19** |

---

## Findings

### F-H-001 — `bool` accepted as `sequenceNumber`

- **Severity:** HIGH  
- **File / location:** `utils/scenario_engine_v2.py` — `apply_decision_v2` (~L1282); `ScenarioDecisionInputV2` (~L1068)  
- **Evidence:** Independent probe constructed `ScenarioDecisionInputV2(True, "SC001-C01", "opt-sc001-c01-a")`. Because `True == 1` in Python, the comparison against `expected_sequence_number == 1` succeeded and the decision was applied (`current_scene_id` advanced to `SC001-C02`). Dataclasses do not enforce runtime types.  
- **Impact:** A malformed client payload (`"sequenceNumber": true` after loose JSON/decoding, or a Python caller passing a bool) can mutate run state. Violates decision-input security expectations for persistence-bound APIs.  
- **Required correction:** Reject non-`int` sequence numbers, explicitly excluding `bool` (`isinstance(x, bool)` or `type(x) is int`). Apply the same strictness to non-empty `str` scene/option IDs.  
- **Blocks persistence integration:** Yes  
- **Blocks full CB-SC-001 integration:** Yes  
- **Blocks eventual production deployment:** Yes  

### F-H-002 — `optionDisplayPolicy: "authored_order"` is ignored

- **Severity:** HIGH  
- **File / location:** `utils/scenario_engine_v2.py` — `ScenarioContentV2.option_display_policy` stored at ~L388; `_enter_scene` / `deterministic_option_display_order` (~L1203–L1227, ~L775) never branch on policy  
- **Evidence:** Schema enum allows `["randomize_per_attempt_scene", "authored_order"]` (`simulation.schema.json`). Probe rebuilt the vertical-slice fixture with `"optionDisplayPolicy": "authored_order"`; `build_scenario_content_v2` accepted it; start-scene order was still shuffled (`['opt-sc001-c01-c', 'opt-sc001-c01-a', 'opt-sc001-c01-b']` ≠ authored).  
- **Impact:** Any valid 1.1.0 document declaring authored order receives randomized display. Silent contract violation; learner UX and analytics expectations diverge from content.  
- **Required correction:** When policy is `authored_order`, return the authored option-id list unchanged (still store it on the snapshot). Keep SHA-256 Fisher–Yates only for `randomize_per_attempt_scene`. Add a focused test.  
- **Blocks persistence integration:** Yes (Engine V2 claims general 1.1.0 runtime readiness; policy is already loadable)  
- **Blocks full CB-SC-001 integration:** Yes  
- **Blocks eventual production deployment:** Yes  

### F-M-001 — §17 SHA-256 first-block interpretation differs from literal spec text

- **Severity:** MEDIUM  
- **File / location:** `utils/scenario_engine_v2.py` — `_sha256_byte_stream` (~L737)  
- **Evidence:** Spec §17: `stream = SHA256(material) as big-endian integer bytes, extended by SHA256(material || counter) as needed`. Implementation always uses `SHA256(material || counter.to_bytes(4,"big"))` starting at `counter=0`. Probe: first 8 bytes of impl digest ≠ `SHA256(material)` alone.  
- **Impact:** Within this single Python engine, behavior is deterministic and cross-process stable (probed). A second implementation following the literal “first block without counter” reading would diverge on option order and resume verification.  
- **Required correction:** Either (a) change the stream to match a clarified normative reading, or (b) amend the spec/validator notes to define `SHA256(material || uint32be(counter))` for `counter=0,1,…` as normative, and freeze that in the engine docstring. Prefer (b) if existing V2 tests/fixtures already lock the current stream.  
- **Blocks persistence integration:** No (same engine recomputes identically)  
- **Blocks full CB-SC-001 integration:** No  
- **Blocks eventual production deployment:** Yes (if multiple runtimes or external verifiers appear)  

### F-M-002 — Non-finite state deltas are not rejected

- **Severity:** MEDIUM  
- **File / location:** `utils/scenario_engine_v2.py` — `_apply_state_deltas` / `_clamp_state_value` (~L506–L526)  
- **Evidence:** Probe applied `float('nan')` and `float('inf')` deltas via `_apply_state_deltas`. Inf clamped to the declared maximum (`100.0`); NaN interacted with `max`/`min` clamping to produce a finite but meaningless result (`0.0` in the probe). Spec §14.2 / task require finite numeric inputs.  
- **Impact:** Impossible content that bypasses validation (or future buggy compilers) can poison state without a domain error.  
- **Required correction:** Reject non-finite current values and deltas with `ScenarioContentV2Error` before clamping (`math.isfinite`).  
- **Blocks persistence integration:** No (validated content excludes NaN/Inf)  
- **Blocks full CB-SC-001 integration:** No  
- **Blocks eventual production deployment:** Yes (defense-in-depth)  

### F-M-003 — Runtime flag mutator does not fail closed on undeclared flag IDs

- **Severity:** MEDIUM  
- **File / location:** `utils/scenario_engine_v2.py` — `_apply_flag_changes` (~L529); `apply_decision_v2` (~L1312–L1315)  
- **Evidence:** Probe `_apply_flag_changes(frozenset(), set_=("flag-never-declared",))` returned that flag as set. Condition evaluation *does* fail closed on unknown flags; option set/clear paths do not.  
- **Impact:** Relies entirely on prior validation. Safe for the validated pipeline; unsafe if `apply_decision_v2` is ever called with hand-built/partially validated content.  
- **Required correction:** Validate clear/set IDs against `content.flags_spec` inside `apply_decision_v2` (or `_apply_flag_changes` with content).  
- **Blocks persistence integration:** No  
- **Blocks full CB-SC-001 integration:** No  
- **Blocks eventual production deployment:** Prefer Yes for fail-closed hardening  

### F-M-004 — Duplicate-submission semantics: reject vs idempotent return

- **Severity:** MEDIUM  
- **File / location:** `utils/scenario_engine_v2.py` — `apply_decision_v2` (~L1282)  
- **Evidence:** Spec §11.3: “Duplicate submissions: Idempotent same fingerprint → return prior result; no second increment.” Task SIM-ENGINE-V2-01 and tests require reject-without-mutation. Current code rejects stale/duplicate sequence with `ScenarioRunStateV2Error`.  
- **Impact:** Correct for the slice task; persistence/resume with client retries will need an application-layer idempotency key or an engine change to match §11.3.  
- **Required correction:** Decide at persistence design time: (a) keep reject and handle retries above the engine, or (b) implement fingerprint idempotency in `apply_decision_v2`. Document the choice against §11.3.  
- **Blocks persistence integration:** No (if the persistence layer owns idempotency)  
- **Blocks full CB-SC-001 integration:** No  
- **Blocks eventual production deployment:** Yes until the product contract is explicit  

### F-M-005 — `DebriefTraceEntry.selected_variant_id` records the *next* scene’s variant

- **Severity:** MEDIUM  
- **File / location:** `utils/scenario_engine_v2.py` — `apply_decision_v2` (~L1369–L1435)  
- **Evidence:** Debrief entry is built with `selected_variant_id=None`, then replaced with the dialogue selected while *entering the next scene*. Terminal decisions leave it `None`. The variant shown when the learner decided is in `variant_selections` for the *current* scene (entry-time event).  
- **Impact:** Debrief consumers that read `decisions[i].selected_variant_id` as “variant of the decided scene” will be wrong. Replay still has correct data via `variant_selections`.  
- **Required correction:** Store the decided-scene variant id on the debrief entry (from the last `VariantSelectionEvent` for `scene_id`), and optionally also record next-scene variant separately—or document the current field meaning unambiguously.  
- **Blocks persistence integration:** No  
- **Blocks full CB-SC-001 integration:** Prefer Yes before debrief UI  
- **Blocks eventual production deployment:** Yes (debrief correctness)  

### F-M-006 — Skip-path replay test does not replay a skipped-corrective history

- **Severity:** MEDIUM  
- **File / location:** `tests/test_scenario_engine_v2.py` — `test_45_replay_reproduces_skipped_corrective_path`  
- **Evidence:** Test pre-sets the counter for live skip behavior, then explicitly notes that a from-scratch replay cannot reproduce that path on this fixture; it then asserts a *corrective-entry* path instead. Runtime skip logic itself is correct (probed via `resolve_routing` at counter=1 → `SC001-C03`, `skipped=True`).  
- **Impact:** Gap in regression proof that `replay_scenario_run_v2` reconstructs skip events end-to-end. Not a runtime defect.  
- **Required correction:** Before full CB-SC-001: add either a multi-corrective fixture path that naturally exhausts budget twice, or a replay test that feeds decisions after a content graph with two corrective opportunities; keep preset tests for unit isolation.  
- **Blocks persistence integration:** No  
- **Blocks full CB-SC-001 integration:** Yes (HIGH-value integration coverage, severity MEDIUM for the engine core)  
- **Blocks eventual production deployment:** Prefer Yes until natural skip+replay is covered  

### F-M-007 — `replay_scenario_run_v2` does not call `verify_replay_identity_v2`

- **Severity:** MEDIUM  
- **File / location:** `utils/scenario_engine_v2.py` — `replay_scenario_run_v2` (~L1483); `verify_replay_identity_v2` (~L1515)  
- **Evidence:** Identity verification is a separate public function. Replay alone trusts whatever `ScenarioContentV2` instance is passed; hash/version mismatches are only caught if the caller invokes `verify_replay_identity_v2`.  
- **Impact:** Persistence layer must always pair the two. Easy to misuse.  
- **Required correction:** Either require pinned identity kwargs on `replay_scenario_run_v2`, or document a mandatory call sequence in the persistence design and add an integration helper.  
- **Blocks persistence integration:** No (if persistence owns the pairing)  
- **Blocks full CB-SC-001 integration:** No  
- **Blocks eventual production deployment:** Prefer Yes without a hard API guard  

### F-L-001 — Condition node-count bound (64) not enforced at runtime

- **Severity:** LOW  
- **File / location:** `evaluate_condition` (~L421); depth 8 enforced, node count not  
- **Evidence:** Spec §9.3 max nodes 64; runtime only checks depth.  
- **Impact:** Validated content is safe; defensive gap only.  
- **Required correction:** Optional node counter in `evaluate_condition`.  
- **Blocks persistence / CB-SC-001 / production:** No / No / Prefer later  

### F-L-002 — Fixture does not exercise `clearFlags`

- **Severity:** LOW  
- **File / location:** `tests/fixtures/scenario_engine_v2_vslice_1_1_0.json`; clear/set proven via unit helper in `test_16`  
- **Evidence:** Grep shows no `clearFlags` in the fixture; only `setFlags`. Task asked for “one flag clear and set sequence.”  
- **Impact:** Runtime clear-before-set is implemented and unit-tested; vertical-slice content proof is incomplete.  
- **Required correction:** Optional fixture option with clear+set, or accept helper-level proof for this slice.  
- **Blocks persistence / CB-SC-001 / production:** No / No / No  

### F-L-003 — Public surface exports many low-level helpers

- **Severity:** LOW  
- **File / location:** `__all__` (~L72)  
- **Evidence:** Exports `evaluate_condition`, `resolve_routing`, `compute_*`, `classify_outcome`, etc., alongside pipeline APIs.  
- **Impact:** Useful for tests; persistence callers might over-bind to internals.  
- **Required correction:** Document “pipeline vs primitive” tiers; optionally narrow `__all__` later.  
- **Blocks persistence / CB-SC-001 / production:** No / No / No  

### F-L-004 — `load_scenario_content_v2` performs file I/O inside the engine module

- **Severity:** LOW  
- **File / location:** `load_scenario_content_v2` (~L393)  
- **Evidence:** Reads JSON from disk. Core `start`/`apply`/`replay` remain pure.  
- **Impact:** Slight purity narrative dilution; not used by runtime transitions.  
- **Required correction:** Move loader beside catalog utilities in a later cleanup, or leave with a docstring noting it is a convenience.  
- **Blocks persistence / CB-SC-001 / production:** No / No / No  

### F-L-005 — `evaluationOrder` not re-checked at classification time

- **Severity:** LOW  
- **File / location:** `classify_outcome` (~L926)  
- **Evidence:** Content is validated earlier; `classify_outcome` does not assert `evaluationOrder == "v1_seven_step"`.  
- **Impact:** Negligible under validated content.  
- **Required correction:** Optional defensive assert.  
- **Blocks persistence / CB-SC-001 / production:** No / No / No  

### F-N-001 — No central schema→engine dispatcher

- **Severity:** NOTE  
- **Evidence:** `utils/scenario_engine_v2.py` never imports V1; V1 `build_scenario_content` rejects 1.1.0; V2 builder rejects non-1.1.0 / non-V2 engine. No shared silent fallback.  
- **Assessment:** **A — No issue until application integration.** Callers must select the module by declared `schemaVersion`. Absence does not create an unsafe execution path today. Add a thin dispatcher when the first application entrypoint must accept either schema (persistence or learner controller), not as a prerequisite for designing snapshot JSON.  

### F-N-002 — Snapshot embeds live `ScenarioContentV2`

- **Severity:** NOTE  
- **Evidence:** `ScenarioRunV2Snapshot.content` holds frozen document + indices. Not directly `json.dumps`-able without thawing/stripping. Probe showed runtime fields (state, flags, counters, histories, orders, identity) serialize cleanly after thaw.  
- **Guidance:** Persist identity + decision history + audit extras; reload content by hash; recompute or verify derived fields. Do not persist the Python content object.  

### F-N-003 — Score-band recalibration was legitimate fixture authoring

- **Severity:** NOTE  
- **Evidence:** Fixture `description` documents recalibration from §23 illustrative thresholds; classifier code unchanged; `CV-089` still enforced via `build_scenario_content_v2` → `validate_v1_1_scenario_document`. Multiple outcomes reachable (tests + probe).  

### F-N-004 — Corrective-skip via pre-set counter is acceptable for this slice

- **Severity:** NOTE  
- **Evidence:** Single-slot budget + single corrective scene means a fresh linear playthrough cannot naturally hit skip after a prior entry without a second corrective opportunity. `resolve_routing` at `correctiveScenesExperienced=1` skips correctly. Preset tests prove the runtime rule; they are not evidence of a runtime defect. Natural multi-corrective coverage belongs with CB-SC-001 / a richer fixture (see F-M-006).  

### F-N-005 — Serialization shapes for persistence

- **Severity:** NOTE  
- **Types observed:** `MappingProxyType`, `tuple`, `frozenset`, nested frozen dataclasses. All convertible via a deterministic thaw (lists for sequences/sets; plain dicts for maps). Floats are float64; store as JSON numbers; recompute classification from unrounded composite rather than trusting display ints alone. No enums or exception objects in snapshots.  

---

## Mandatory area results

### 1. Module boundary

**Pass with NOTE (F-N-001).** Engine V2 is additive and isolated. No V1 import. No implicit fallback. Engine identity checks are consistent (`ENGINE_VERSION` / `requiredEngineVersion` / schema gate). Public pipeline APIs are clear enough for persistence design. Dispatcher: recommendation **A**.

### 2. Public API

**Pass with LOW (F-L-003, F-L-004).** Pipeline: `build_scenario_content_v2` / `load_scenario_content_v2`, `start_scenario_run_v2`, `apply_decision_v2`, learner views, `replay_scenario_run_v2`, `verify_replay_identity_v2`, `build_debrief_trace`, classification helpers, domain exceptions. Content hash computed at build via `compute_canonical_content_sha256_v1_1`.

### 3. Immutability

**Pass.** Deep-freeze at build; frozen dataclasses; probes confirmed: source document unchanged; prior snapshot unchanged after apply/reject; `MappingProxyType` blocks state and dialogue mutation; learner views do not alias mutable server dicts.

### 4. Runtime-state contract

**Pass with guidance (F-N-002, F-N-005).** Fields are necessary and deterministic.  

| Field | Persist? | Recompute on resume? |
|---|---|---|
| Identity (sim/version/schema/hash/engine) | **Persist (pin)** | Verify |
| `attempt_id` | **Persist** | Seed for order |
| `decisionHistory` (seq/scene/option) | **Persist (learner truth)** | Drive replay |
| `current_scene_id` / `expected_sequence` | Optional cache | **Recompute** via replay |
| `state` / `flags` / `counters` | Optional cache | **Recompute**; may verify |
| `tier_history` | Optional | **Recompute** |
| `option_display_order_by_scene` | **Persist (spec §17)** | Recompute + verify |
| `variant_selections` | Optional audit | **Recompute** + verify |
| `routing_resolutions` / corrective / skip events | Optional audit | **Recompute** + verify |
| `terminal_result` / `classificationTrace` | **Persist at completion** (spec §16.2) | May recompute for verify |
| Full `ScenarioContentV2` object | **Do not persist** | Reload by pin |

### 5. Initialization

**Pass.** Requires schema 1.1.0 + `SCENARIO_ENGINE_V2`; runs full layered validation; initializes state/flags/counters separately; deterministic first variant/order; learner view hides scoring fields.

### 6. Decision-input security

**Fail HIGH on bool sequence (F-H-001); otherwise pass.** Structurally only seq/scene/option. Empty/whitespace IDs rejected. No client tier/state/flags/routing/outcome fields. Display index cannot be submitted as identity.

### 7. Exact transition order

**Pass.** Implementation matches the task’s 16-step order and aligns with normative §11.3 (state → flags → tier counters → route with pre-entry corrective counter → corrective increment/skip → environmental flags + variant on next scene → classify on terminal). Sequence increments once per successful apply. Reject paths mutate nothing.

### 8. State deltas and clamping

**Pass with MEDIUM (F-M-002).** Declared deltas apply once; unknown keys fail closed; bool rejected by `_is_finite_number` in some helpers but `float(True)` still possible if content bypasses validation; bounds clamp correctly for finite values; prior state unchanged; debrief records authored delta and clamped `state_after`.

### 9. Flags

**Pass with MEDIUM (F-M-003) and LOW (F-L-002).** Clear-before-set correct; same id clear+set ends set; dialogue uses committed post-decision flags / entry flags per §9.2; learner views do not leak flag IDs (probed).

### 10. Counters

**Pass.** Distinct from state; corrective increments once on entry only; skip does not increment; normal routes do not; replay reproduces; rejects do not increment; bounds clamped via counter spec.

### 11. Corrective routing

**Pass (runtime).** Option-owned `triggerOnTiers`; budget condition; targets; reconvergence; corrective re-branch defensively rejected; histories recorded.  

**Skip fixture assessment (F-N-004 / F-M-006):** Acceptable for this isolated slice as a unit proof of the runtime rule via pre-set counter; **not** a runtime defect; **missing HIGH-value integration/replay coverage** before full CB-SC-001. Do not require altering the current fixture solely for slice sign-off.

### 12. Condition evaluation

**Pass with LOW (F-L-001).** All seven forms; unknown refs fail closed; malformed nodes raise domain errors; depth 8 enforced; no arbitrary expressions; bool excluded from numeric compares via `_is_finite_number`.

### 13. Dialogue variants

**Pass with MEDIUM field-semantics note (F-M-005).** Fallback base exchanges; ascending priority; unique priority enforced; overrides preserve exchange IDs; selected id stored in `variant_selections`; conditions not exposed to learners; replay matches.

### 14. Option display order

**Pass for `randomize_per_attempt_scene`; HIGH gap for `authored_order` (F-H-002); MEDIUM stream ambiguity (F-M-001).** Seed material matches §17 field list; Fisher–Yates with rejection sampling; stable IDs; same attempt ⇒ same order; different attempts can differ; stored on snapshot; submissions by option id; cross-process determinism probed (`PYTHONHASHSEED` irrelevant — no `hash()` of str used).

### 15. Formulas

**Pass with MEDIUM finite-input note on state path (F-M-002).** Four types implemented; weight sum checked; zero scored decisions fail closed; missing variables fail closed; no display rounding inside composite; cycles rejected at validation (not re-walked as a graph in runtime identity formula — acceptable given validated content).

### 16. Outcome classifier

**Pass.** Severe first and dominant (probe: severe+moderate → `failed_resolution`, moderate not applied as override); moderate tightest max; guards disqualify; band inclusivity exact at 36/32/29 boundaries (probed); ranks lower-number-better; tie-break deterministic; display rounding after classification via `round_half_away_from_zero`; replay-stable.

### 17. Terminal behavior

**Pass.** Terminal only via `EVALUATE_ENDING`; further submissions rejected; terminal learner view only after completion; fields limited to outcome id/title/narrative/display score.

### 18. Debrief trace

**Pass with MEDIUM (F-M-005).** Entries include scene/option/tier/seed/delta/state_after/flags/routing/corrective flags/competency tags; classifier effects live on `terminal_result.classification`; `build_debrief_trace` refuses incomplete runs; frozen/proxied nested maps.

### 19. Replay

**Pass with MEDIUM (F-M-006, F-M-007).** Recomputes exclusively via `start` + `apply` (does not trust stored derived state). Normal/corrective/terminal/variant/order/outcome covered. Rejects bad sequence, post-terminal, hash mismatch (via verify helper), reordered history (probed). Skip end-to-end replay coverage weak (F-M-006).

### 20. Error contract

**Pass for reviewed paths.** Domain exceptions for unsupported schema/engine, invalid content, stale/future sequence, scene mismatch, unknown option, invalid replay, terminal submission, classifier failure. Malformed document raises `ScenarioEngineV2Error` rather than raw `KeyError`. Residual risk: unexpected types deep in content could still surface `TypeError` in edge cases—not observed in probes.

### 21. Learner-safe output

**Pass.** Probed forbidden-token scan clean for scene and terminal views. Options returned in display order with stable ids.

### 22. Serialization readiness

**Ready with thaw layer (F-N-002, F-N-005).** No DB schema designed. Persistence must convert proxies/tuples/frozensets and must not embed the live content object.

### 23. Version-dispatch recommendation

**A. No issue until application integration.**  
Evidence: V1 and V2 loaders each fail closed on the other’s schema; no shared execution function reinterprets content. Add a dispatcher when a single HTTP/controller entrypoint must accept either version—not as a blocker for snapshot JSON design.

### 24. Fixture quality

**Pass for slice purpose, with LOW clearFlags gap (F-L-002) and NOTE on skip topology (F-N-004).** Demonstrates normal transition, corrective entry, reconvergence, variants, tiers, deterministic ordering, ≥2 outcomes, full validation. Skip proven via preset, not natural double-trigger. Threshold recalibration is documented fixture authoring, not classifier weakening.

### 25. Test quality

**Strong overall (81 tests, all required scenarios mapped).** Exact assertions dominate. Weak spots: F-M-006 (`test_45`); limited nested-alias tests (independent probes filled this); no serialization tests; no `authored_order` test; no bool-sequence negative test; classifier band edges covered lightly in unit tests (probes confirmed). Replay tests correctly compare full recomputed snapshots rather than trusting partial fields—with the skip-path exception above.

---

## Tests and probes executed

### Focused suite

```text
python -m pytest \
  tests/test_scenario_engine_v2.py \
  tests/test_scenario_engine.py \
  tests/test_scenario_schema.py \
  tests/test_scenario_catalog.py \
  tests/test_scenario_validation_v1_1.py \
  -q
```

**Result:** `251 passed` in ~3.0s.

### Independent disposable probes (removed after run)

| Probe | Result |
|---|---|
| Prior-state immutability + stale reject | Pass |
| Nested alias / MappingProxyType | Pass |
| Bool `sequenceNumber=True` | **Accepted (F-H-001)** |
| Empty/malformed IDs | Rejected |
| `authored_order` policy | **Ignored (F-H-002)** |
| SHA-256 first block vs literal | **Diverges (F-M-001)** |
| Classifier band boundaries | Pass (exact) |
| Severe dominates moderate | Pass |
| Reordered replay history | Rejected |
| JSON thaw serialize runtime fields | Pass |
| Learner hidden-field leakage | Pass |
| Preset vs natural corrective skip | Runtime skip correct; natural double-skip impossible on fixture |
| Cross-process option order | Identical |

Temporary file `_scratch_review_probes.py` was deleted after probes. No source/test/fixture changes.

---

## Scope and safety confirmation

| Check | Result |
|---|---|
| SIM-ENGINE-V2-01 modified existing tracked files | **No** (`git status` shows only new untracked V2 artifacts + pre-existing unrelated untracked paths) |
| Persistence / DB / UI / compiler / deploy work in V2-01 | **No** |
| This review modified source/tests/fixtures/schemas | **No** |
| Protected paths inspected or touched | **No** |
| Staged / committed / pushed / deployed | **No** |

### Starting git status (review task)

```text
## main...origin/main [ahead 18]
?? .local/
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_SLICE_01_IMPLEMENTATION_REPORT.md
?? local_only/
?? scripts/v58_run_combined_policy_evaluation.py
?? structural_audit_state.json
?? tests/fixtures/scenario_engine_v2_vslice_1_1_0.json
?? tests/test_combined_policy_evaluator.py
?? tests/test_scenario_engine_v2.py
?? utils/scenario_engine_v2.py
?? v68_corrected_review_bundle/
?? v68_final_review_bundle/
?? v68_review_bundle/
?? workers/combined_policy_evaluator.py
```

`HEAD` at review start: `12ffe9d Complete scenario schema 1.1 validation foundation`.

### Ending git status (expected after this review)

Same as start, plus:

```text
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_SLICE_01_FOCUSED_REVIEW.md
```

---

## Recommended correction sequence

1. **F-H-001** — Strict runtime typing for `sequence_number` / IDs (reject `bool`). Add tests.  
2. **F-H-002** — Honor `authored_order` in option display resolution. Add tests.  
3. **F-M-001** — Lock §17 byte-stream interpretation in spec and engine docstring (prefer document-current behavior if already test-locked).  
4. **F-M-002 / F-M-003** — Fail closed on non-finite deltas and undeclared flag mutations.  
5. **F-M-005** — Fix or document debrief variant-id semantics.  
6. **F-M-006** — Add natural skip+replay coverage before full CB-SC-001.  
7. **F-M-004 / F-M-007** — Decide idempotency and identity-verify API shape as part of persistence design.  

## Recommended next action

1. Apply corrections **1–2** (both HIGHs) in a small Engine V2 hardening task.  
2. Re-run the focused 251-test command plus bool/`authored_order` probes.  
3. Proceed to **persistence/resume integration design** (snapshot thaw, pin identity, decision history, option-order verify) **without** requiring a central dispatcher yet.  
4. Defer natural multi-corrective skip coverage and debrief-field cleanup to the CB-SC-001 / debrief track unless persistence needs those fields immediately.

## Persistence-integration readiness

**No** — unresolved HIGH count is 2 (`F-H-001`, `F-H-002`).

## Full CB-SC-001 integration readiness

**No** — requires HIGH fixes above, plus natural corrective-budget exhaustion/replay coverage (`F-M-006`), and preferably debrief variant semantics (`F-M-005`).
