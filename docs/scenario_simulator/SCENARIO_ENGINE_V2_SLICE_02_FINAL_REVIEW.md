# SCENARIO_ENGINE_V2 Slice 02 — Final Confirmation Review

**Task ID:** SIM-ENGINE-V2-REVIEW-02  
**Model:** Auto  
**Date:** 2026-07-31  
**Baseline commit:** `12ffe9d` — Complete scenario schema 1.1 validation foundation  
**Scope:** Narrow confirmation review only (no code/test/fixture/schema/doc edits except this report)

---

## Verdict

**READY for local milestone commit and persistence/resume design.**

| Metric | Count |
|---|---|
| Remaining blockers | **0** |
| Remaining HIGH | **0** |
| New blockers | **0** |
| New HIGH | **0** |

Both prior HIGH findings (F-H-001, F-H-002) remain closed. Material deterministic-runtime corrections remain correct. Scenario Schema 1.1.0 §17 revision 6 matches implementation and tests exactly. Focused regression: **271 passed**.

This review does **not** authorize full CB-SC-001 package integration, UI, dispatcher, or production persistence implementation — only a local milestone commit and persistence/resume **design**.

---

## Starting git status

```
## main...origin/main [ahead 18]
 M docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md
 M docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md
?? .local/
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_SLICE_01_FOCUSED_REVIEW.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_SLICE_01_IMPLEMENTATION_REPORT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_SLICE_02_HARDENING_REPORT.md
?? docs/scenario_simulator/SCENARIO_ENGINE_V2_SPEC_17_ALIGNMENT_REPORT.md
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

Protected and unrelated untracked paths were treated as out of scope and not inspected.

---

## Confirmation results

### 1. Strict sequence typing — F-H-001 — **CLOSED / PASS**

Implementation: `_require_strict_int` accepts only `type(value) is int`.

Confirmed by tests (`TestHardeningSequenceTyping`) and disposable probe:

| Case | Result |
|---|---|
| `True` / `False` | Rejected (`strict integer`) |
| `1.0`, `"1"`, `None` | Rejected |
| Negative (`-1`) | Rejected (`>= 1`) |
| Zero (`0`) | Rejected (`>= 1`) before stale/future compare |
| Stale after advance | Distinct error (`expected sequenceNumber 2, got 1`) |
| Future | Distinct error (`expected sequenceNumber 1, got 2`) |
| Rejected submissions | Do not mutate prior run (identity + expected sequence preserved) |
| Replay | Same contract via `_require_strict_int` on each decision |

### 2. Option display policy — F-H-002 — **CLOSED / PASS**

**A. `authored_order` — PASS**

- Preserves exact authored `options[].id` array order
- No seed / no shuffle
- Different attempt IDs produce identical order
- Snapshot stores authored order; replay path uses same resolution

**B. `randomize_per_attempt_scene` — PASS**

- Uses frozen §17 revision 6 stream
- Same inputs → same order
- Different attempts may differ (probe: 5 distinct orders across 12 attempt IDs)
- Replay reproduces stored per-scene order maps

**C. Unsupported policies — PASS**

- Fail closed with `ScenarioContentV2Error` containing `unsupported optionDisplayPolicy`

### 3. §17 revision 6 alignment — **PASS (exact match)**

Compared implementation (`deterministic_option_display_order` / `_sha256_byte_stream` / `_uniform_index`), golden test, `SCENARIO_SCHEMA_1_1_0_SPEC.md` §17.1–§17.9, custom validation §G, and SPEC-17 alignment report.

| Contract element | Spec §17 | Implementation | Tests |
|---|---|---|---|
| Seed field order | attemptId, simulationId, version, hash, sceneId | `"\n".join(...)` same order | golden vector |
| Newline separators / no trailing NL | Explicit | `str.join` (no trailing) | implied by golden |
| Verbatim values + UTF-8 | Explicit | `.encode("utf-8")` | yes |
| `uint32be` counter from 0 | Explicit | `counter.to_bytes(4, "big")` starting 0 | docstring + golden |
| `SHA256(material \|\| uint32be(counter))` | Explicit (no bare first block) | Exact | yes |
| Sequential byte consumption | Explicit | `yield from block` | yes |
| Rejection sampling | §17.6 | `_uniform_index` | yes |
| Backward Fisher–Yates | §17.7 | `range(len-1, 0, -1)` | yes |
| Stable option ID identity | Explicit | submissions by option id only | yes |
| Replay guarantees | Explicit | replay equality tests | yes |
| Unsupported fail-closed | Explicit | `resolve_option_display_order` | yes |

**Golden vector:** inputs match; expected order `("opt-b", "opt-c", "opt-a")` reproduced in-process and across 3 isolated interpreter processes.

**No implementation/document contradiction remains** for the normative §17 contract. Historical review docs still narrate the pre-revision-6 ambiguity for audit trail; normative source of truth is §17 revision 6.

### 4. Finite numeric defense — **PASS**

- `_is_finite_number` / `_require_finite_number` reject bool, NaN, ±Inf, non-numeric
- Covered on state deltas, formula paths, and related numeric coercion
- Failed delta application does not mutate prior state (tests assert state equality)

### 5. Declared flags — **PASS**

- Undeclared `setFlags` / `clearFlags` fail closed
- Condition `flagSet` / `flagNotSet` undeclared references fail closed
- No dynamic flag creation
- Clear-before-set semantics retained in `_apply_flag_changes`

### 6. Debrief variant semantics — **PASS**

- `DebriefTraceEntry` exposes `presented_dialogue_variant_id` and `next_dialogue_variant_id`
- No `selected_variant_id` on debrief trace entries (probe confirmed)
- Replay reproduces both fields
- Terminal / base dialogue uses `None` where appropriate
- Learner scene view does not expose variant-selection internals or hidden conditions
- Note (non-HIGH): internal `ResolvedDialogue` / `VariantSelectionEvent` still use `selected_variant_id` as an internal resolution field — not learner-facing debrief output

### 7. Natural corrective-budget exhaustion — **PASS**

Fixture + tests + probe execute without manual counter pre-setting:

```
C01-a → C02-b → R2A (experienced=1) → C03 → skip R3A (experienced still 1, skip recorded) → C04 → COMPLETE
```

| Check | Result |
|---|---|
| First corrective increments once | `correctiveScenesExperienced == 1` after R2A |
| Second corrective request skips | Lands on C04; skip event for `SC001-R3A` |
| Skip does not increment | Counter remains 1 |
| Skip event recorded | `skipped_corrective_events` length 1 |
| Replay full snapshot equality | `replayed == run` |
| No manual counter pre-setting | Natural path only |

### 8. Immutability and output safety — **PASS**

- Content deep-frozen at build; prior run snapshots immutable (frozen dataclasses; rejected ops leave caller run unchanged)
- Learner scene view keys are presentation-only; forbidden scoring/routing fields absent from keys and serialized values
- Learner terminal view limited to outcome presentation fields after completion

### 9. Regression — **PASS**

| Suite | Result |
|---|---|
| Engine V2 | Included in focused run |
| Engine V1 (`tests/test_scenario_engine.py`) | Passes; imports `utils.scenario_engine` only |
| Schema / catalog / validator | Pass |

No persistence, database, UI, compiler, or dispatcher work introduced by this review.

---

## Tests executed

```bash
python -m pytest \
  tests/test_scenario_engine_v2.py \
  tests/test_scenario_engine.py \
  tests/test_scenario_schema.py \
  tests/test_scenario_catalog.py \
  tests/test_scenario_validation_v1_1.py \
  -q
```

**Result:** `271 passed in 3.23s`

### Disposable probes (removed afterward)

Temporary probe script exercised:

- bool sequence rejection + no mutation
- authored order across attempts
- golden vector
- natural corrective skip + replay equality
- learner hidden-field leakage
- prior-state immutability
- unsupported policy fail-closed
- attempt-variance under randomization
- cross-process golden order (3 isolated interpreters)

All probes **PASS**. Temporary artifact deleted: `_tmp_v2_final_review_probe.py`.

---

## Readiness

| Question | Answer |
|---|---|
| Persistence/resume **design** readiness | **Yes** |
| Local milestone commit readiness | **Yes** (when user explicitly requests commit) |
| Full CB-SC-001 integration readiness | **No** — UI, persistence implementation, dispatcher, and package wiring remain out of scope |

---

## Remaining risks (non-blocking)

1. Persistence layer must pin `attemptId`, content hash, and per-scene `optionDisplayOrder` exactly to preserve §17 replay.
2. Non-Python runtimes must implement §17.4–§17.7 identically; use §17.9 golden vector as conformance gate.
3. Internal `selected_variant_id` on non-debrief structures could confuse future API authors — keep learner/debrief surfaces on the two explicit fields.
4. Working tree still contains many unrelated untracked/protected paths; milestone commit must carefully stage only Engine V2 + §17 alignment artifacts.

---

## Files modified by this task

**None** (source/test/fixture/schema/docs other than this report untouched).

## File created by this task

- `docs/scenario_simulator/SCENARIO_ENGINE_V2_SLICE_02_FINAL_REVIEW.md`

---

## Completion report checklist

1. **Task status:** COMPLETE — READY for local milestone commit and persistence/resume design  
2. **Review file created:** `docs/scenario_simulator/SCENARIO_ENGINE_V2_SLICE_02_FINAL_REVIEW.md`  
3. **Repository branch:** `main` (ahead 18 of `origin/main`)  
4. **Starting git status:** see above  
5. **Ending git status:** starting status plus this new untracked review file only (from this task)  
6. **Remaining blocker count:** 0  
7. **Remaining high count:** 0  
8. **New blocker count:** 0  
9. **New high count:** 0  
10. **F-H-001 result:** CLOSED / PASS  
11. **F-H-002 result:** CLOSED / PASS  
12. **Strict sequence typing result:** PASS  
13. **authored_order result:** PASS  
14. **randomize_per_attempt_scene result:** PASS  
15. **Unsupported-policy result:** PASS  
16. **§17 implementation alignment:** PASS (exact)  
17. **Seed-material result:** PASS  
18. **Counter/digest-block result:** PASS  
19. **Fisher–Yates result:** PASS  
20. **Golden-vector result:** PASS (`opt-b`, `opt-c`, `opt-a`)  
21. **Cross-process determinism result:** PASS (3/3 identical)  
22. **Finite-number result:** PASS  
23. **Undeclared-flag result:** PASS  
24. **Debrief-variant result:** PASS  
25. **Natural corrective-exhaustion result:** PASS  
26. **Natural skip replay result:** PASS  
27. **Content immutability result:** PASS  
28. **Prior-state immutability result:** PASS  
29. **Learner-output safety result:** PASS  
30. **Focused tests executed:** Engine V2 + Engine V1 + schema + catalog + validation v1.1  
31. **Test results:** 271 passed  
32. **Engine V1 regression result:** PASS  
33. **Schema/catalog/validator regression result:** PASS  
34. **Persistence-integration readiness:** Design-ready only (not implemented)  
35. **Full CB-SC-001 integration readiness:** No  
36. **Files modified:** None  
37. **Confirmation source/test/fixture/schema files untouched:** Confirmed  
38. **Confirmation protected paths untouched:** Confirmed  
39. **Confirmation nothing staged, committed, pushed, or deployed:** Confirmed  
40. **Errors encountered:** Disposable probe script initially failed on `asdict(mappingproxy)` and Windows `multiprocessing` spawn; rewritten to field-walk + subprocess isolation; probes then passed. Temporary script removed.  
41. **Remaining risks:** See section above  
42. **Recommended next action:** Explicit local milestone commit of Engine V2 hardening + §17 revision 6 documentation (when requested), then begin persistence/resume design against the hardened Engine V2 API and frozen §17 contract.

---

*End of SIM-ENGINE-V2-REVIEW-02 final confirmation review.*
