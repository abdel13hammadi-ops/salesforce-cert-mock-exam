# Adversarial Review — Scenario Schema 1.1.0 Normative Specification

**Task ID:** SIM-SCHEMA-11-REVIEW-01  
**Type:** Independent adversarial architecture / determinism / validation / security / replay / backward-compatibility review  
**Reviewed document:** `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md`  
**Predecessor:** `docs/scenario_simulator/CB-SC-001_engineering_compatibility_review.md`  
**Date:** 2026-07-30  
**Constraint:** Review-only. Source specification and all runtime files were left untouched.

---

## Executive verdict

**Executable JSON Schema must not proceed yet.**

The normative specification correctly identifies the four capability gaps from the compatibility review and proposes a generally sound additive direction. However, adversarial examination found **multiple blockers** that would freeze incorrect field structure, incorrect runtime ordering, or invalid fixtures into the executable schema and subsequent engine work:

1. **Counter / routing mutation order is internally contradictory** (§11.4 vs §13.4 vs §22.1) — corrective-budget counters cannot be implemented deterministically from the written rules.
2. **Canonical hash semantics are undefined and circular** if `canonicalContentSha256` (and volatile provenance fields) are inside the hashed document — contradicts the existing hasher in `utils/scenario_schema.py`.
3. **Empty condition composition (`"all": []`) is undefined** and the vertical-slice example uses it as a vacuous match.
4. **Legacy/new coexistence pairs lack exclusive precedence rules** (`nextScene`/`routing`, `isCorrect`/`evaluationTier`, `narrative`/`dialogue`, `endings[]`/`outcomeClassifier`) — validators, compiler, runtime, and UI can diverge.
5. **The illustrative vertical-slice JSON is not a valid fixture** under the specification’s own cycle and path-length rules (C03 self-loop).
6. **`optionTierInCurrentDecision` in the generic condition grammar** is evaluation-context-unsafe and should not be a dialogue/cap predicate.

**Zero-blocker, zero-unresolved-HIGH standard is not met.** Correction of the specification is required before `SIM-SCHEMA-11-JSON-01`.

---

## Finding summary

| Severity | Count |
|---|---|
| BLOCKER | 6 |
| HIGH | 11 |
| MEDIUM | 10 |
| LOW | 5 |
| NOTE | 4 |
| **Total** | **36** |

---

## Findings

### BLOCKER findings

#### ADV-B-001 — Corrective counter increment timing contradicts itself
- **Severity:** BLOCKER  
- **Affected:** §11.4, §13.4, §22.1  
- **Evidence:** §11.4 requires increment when routing selects a corrective / on entry. §13.4 orders “increment counters” *before* “resolve routing.” §22.1 calls `incrementCounters(...)` *before* `resolveNextScene(...)`. Event `corrective_scene_entered` cannot fire before the next scene is known.  
- **Impact:** Budget checks (`counterCompare … lt 3`) and replay diverge depending on which paragraph implementers trust. Silent wrong skip/enter behavior in production.  
- **Required decision:** Normative order MUST be: (1) apply state/flags for *current* decision; (2) resolve routing using *pre-entry* counters; (3) if next is corrective, increment `correctiveScenesExperienced`; (4) apply environmental entry flags for *next* scene. Remove the pre-routing counter step for corrective entry.  
- **Owner:** Spec author / engine lead  
- **Blocks JSON Schema:** Yes (event semantics affect required counter fields)  
- **Blocks runtime:** Yes  

#### ADV-B-002 — Canonical content hash semantics undefined / circular
- **Severity:** BLOCKER  
- **Affected:** §5.1 (`canonicalContentSha256`), §18, §21 rule 17  
- **Evidence:** Existing `compute_canonical_content_sha256` hashes the entire JSON document (`utils/scenario_schema.py:164-171`). Spec allows embedding `canonicalContentSha256` and requires recomputation to match. Embedding the hash inside the hashed object is circular. Spec also permits `contentProvenance.compiledAt` / `compilerVersion` and `publicationMetadata` inside the document without stating hash exclusion.  
- **Impact:** Non-reproducible hashes; publish/replay identity mismatches; silent “content changed” false positives.  
- **Required decision:** Define exact hashed bytes: exclude `canonicalContentSha256`, and either exclude all generated provenance/publication metadata from the hash or move them outside the content document (sidecar / DB columns only). Document normalization (key sort, separators) matching existing hasher.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Yes  
- **Blocks runtime:** Yes  

#### ADV-B-003 — Empty `all` / `any` condition semantics undefined; example uses vacuous truth
- **Severity:** BLOCKER  
- **Affected:** §9.3, §23 C03 fallback variant  
- **Evidence:** Vertical slice uses `"when": { "all": [] }` with `fallback: true`. Spec never defines empty `all`/`any`. In classical logic `all([])` is vacuously true; `any([])` is false. If a non-fallback variant ever used empty `all`, it would always win.  
- **Impact:** Ambiguous validators and engines; accidental always-true conditions.  
- **Required decision:** Forbid empty `all`/`any` arrays. Fallback variants MUST omit `when` or use an explicit `when: { "always": true }` leaf reserved only for fallbacks. Update example.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Yes  
- **Blocks runtime:** Yes  

#### ADV-B-004 — Vertical-slice example contains an illegal cycle
- **Severity:** BLOCKER  
- **Affected:** §23 (`SC001-C03` option routing), §11.5 / §21 rules 5–8  
- **Evidence:** `opt-sc001-c03-slice-end` sets `primaryNextSceneId: "SC001-C03"` (self-loop). Spec requires cycle rejection and path-length bounds. Example also has no terminal path, so max path is unbounded. Spec §25 itself notes the placeholder problem but still presents §23 as “complete illustrative JSON.”  
- **Impact:** Example cannot be used as an executable fixture; would fail the specified validators.  
- **Required decision:** Represent slice boundary as either (a) a dedicated non-decision terminal sentinel for engineering fixtures only (not production), or (b) stop the slice at C03 *entry* with no decision, or (c) route C03 to `EVALUATE_ENDING` with a slice-only outcome. Do not claim the current example is schema-valid.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Yes (fixture invalid)  
- **Blocks runtime:** Yes (smoke tests cannot proceed on this fixture)  

#### ADV-B-005 — Legacy/new field coexistence lacks exclusive precedence
- **Severity:** BLOCKER  
- **Affected:** §4.4, §10.1, §8.1, §15.1  
- **Evidence:** Spec allows simultaneous presence of pairs without a single authoritative resolution table:

| Pair | Spec today | Ambiguity |
|---|---|---|
| `nextScene` vs `routing` | Normalize when routing omitted | Both present? Which wins? |
| `isCorrect` vs `evaluationTier` | Tier required; isCorrect legacy | Both present and disagree? |
| `narrative` vs `dialogue` | Dialogue required; narrative optional | Both present — which UI renders? |
| `endings[]` vs `outcomeClassifier`/`outcomes[]` | Classifier takes precedence | Display narrative from endings or outcomes? When endings alone allowed? |

- **Impact:** Compiler, JSON Schema, custom validators, engine, and UI can each choose differently — expensive to reverse after content is authored.  
- **Required decision:** For each pair, choose exactly one of: **mutually exclusive**, **both allowed with explicit winner**, **deprecated-accepted until date**, or **invalid**. Encode as MUST rules. Recommended defaults:
  - `routing` authoritative; if both present and disagree → **reject**.
  - `evaluationTier` authoritative; if both present and disagree → **reject**.
  - `dialogue` authoritative for render; if `narrative` present with `dialogue` → **reject** (or ignore narrative with warning — but pick one).
  - `outcomeClassifier` + `outcomes[]` required for 1.1.0; `endings[]` **forbidden** on 1.1.0 documents (keep only on 1.0.0).  
- **Owner:** Spec author + product  
- **Blocks JSON Schema:** Yes  
- **Blocks runtime:** Yes  

#### ADV-B-006 — `optionTierInCurrentDecision` must not live in the generic condition grammar
- **Severity:** BLOCKER  
- **Affected:** §9.3, §11.1, Appendix A  
- **Evidence:** Leaf is documented “routing only” but sits in the shared grammar used by dialogue variants and caps/guards. At dialogue-variant evaluation time there is no “current decision option.” At terminal classification time the “current decision” is ambiguous (last decision? all?). Spec provides no evaluation-context typing.  
- **Impact:** Validators cannot prove safety; authors may attach tier predicates to dialogue/caps incorrectly.  
- **Required decision:** Remove from generic grammar. Represent corrective triggers as option-owned declarative fields, e.g. `correctiveRoute.triggerOnTiers: ["suboptimal","high-risk"]` plus separate budget condition, OR restrict condition schema with a tagged context (`routingCondition` vs `entryCondition` vs `terminalCondition`) and forbid this leaf outside routing.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Yes  
- **Blocks runtime:** Yes  

---

### HIGH findings

#### ADV-H-001 — Engine compatibility “supported versions” conflicts with repository exact-pin model
- **Severity:** HIGH  
- **Affected:** §5.2, §4.6  
- **Evidence:** V68 and `utils/scenario_engine.py` pin exact `engineVersion` (`SCENARIO_ENGINE_V1`). Replay rejects mismatches. Spec introduces `supportedEngineVersions` array suggesting range/compat sets the loader does not support.  
- **Impact:** Spec encourages a compatibility model the persistence/replay stack cannot enforce without new semantics.  
- **Required decision:** Use **exact** `requiredEngineVersion` only for 1.1.0. Drop `supportedEngineVersions` or redefine it as documentation-only, non-normative.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Yes (field presence)  
- **Blocks runtime:** Yes  

#### ADV-H-002 — Application persistence contract *does* change even without DB migration
- **Severity:** HIGH  
- **Affected:** §19  
- **Evidence:** Spec claims “shape remains valid” while requiring counters inside `state` (or nested `counters`). Current engine treats `state` as `Mapping[str, float]` and freezes floats. Mixing integer counters into float state is a semantic contract change to Python dataclasses, serialization, and any consumer assuming numeric state = dimensions.  
- **Impact:** Calling persistence “unchanged” understates risk; replay identity and snapshot validation must be extended.  
- **Required decision:** Explicitly declare **application persistence-contract change** (no SQL migration). Choose one: nested `counters` object sibling to `state` in snapshot (preferred), or typed separate map — not free-mixed float state keys. Update §19 language.  
- **Owner:** Spec + engine lead  
- **Blocks JSON Schema:** No  
- **Blocks runtime:** Yes  

#### ADV-H-003 — Moderate-cap `maxOutcomeRank` example contradicts CB-SC-001
- **Severity:** HIGH  
- **Affected:** §14.4 example CAP-P03  
- **Evidence:** CB-SC-001 CAP-P03 forces max **Partial**. Spec outcomes use rank 1=Strong, 2=Acceptable, 3=Partial, 4=Failed. Example uses `"maxOutcomeRank": 2` (= Acceptable).  
- **Impact:** Wrong encoding of approved scoring; silent product regression.  
- **Required decision:** Fix examples; define rank↔outcome mapping table; require caps to reference `maxOutcomeId` (safer) or document ranks immutably.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** No (but blocks scoring correctness)  
- **Blocks runtime:** Yes  

#### ADV-H-004 — Formula weight normalization unspecified
- **Severity:** HIGH  
- **Affected:** §14.2–14.3  
- **Evidence:** `linear_blend` does not require weights to sum to 1.0. No missing-dimension behavior. No numeric precision (float vs Decimal). Boundary bands use exact values like 88.0.  
- **Impact:** Nondeterministic band selection across languages/platforms; authorable over/under-scaled composites.  
- **Required decision:** Require weights sum to 1.0 ± epsilon or normalize explicitly; require IEEE-754 binary64 with documented comparison; define missing variable as validation failure (not runtime default). Round only after classification (already stated — keep).  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Partially (sum constraint needs custom validator)  
- **Blocks runtime:** Yes  

#### ADV-H-005 — Variable-level `outcomeWeight` conflicts with formula-level weights
- **Severity:** HIGH  
- **Affected:** §13.1, §14.2  
- **Evidence:** `stateVariables.outcomeWeight` is unused by `weighted_dimension_health`, which uses formula-declared polarity/list. Dual weight sources invite drift.  
- **Impact:** Authors set variable weights that engines ignore (or vice versa).  
- **Required decision:** Remove `outcomeWeight` from 1.1.0 variable registry **or** make formula type consume variable-level weights exclusively — not both.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Yes (field inclusion)  
- **Blocks runtime:** No if removed  

#### ADV-H-006 — Dialogue variant same-priority mutual exclusivity “MAY” is too weak
- **Severity:** HIGH  
- **Affected:** §9.2  
- **Evidence:** “MUST reject same priority unless conditions are provably mutually exclusive” + “static analysis MAY require mutual exclusivity.” Two statements conflict. Proving mutual exclusivity of arbitrary conditions is hard.  
- **Impact:** Ambiguous validator strength; two matching variants at same priority undefined if exclusivity proof fails.  
- **Required decision:** Require **unique priorities** for all non-fallback variants. Drop mutual-exclusivity exception for 1.1.0.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Yes  
- **Blocks runtime:** Yes  

#### ADV-H-007 — Two matching variants with different priorities: override merge vs full replace undocumented for multi-flag CB-SC-001
- **Severity:** HIGH  
- **Affected:** §9.2, CB-SC-001 C03 (verbal-handoff + sales-reengaged)  
- **Evidence:** Spec selects **first** matching variant only. CB-SC-001 C03 can have both flags set and needs possibly combined dialogue effects. First-match drops secondary variant content.  
- **Impact:** Semantic loss vs Creative Studio product if both variants matter.  
- **Required decision:** Product/spec must choose: (a) first-match only with authored priority encoding combined cases as separate variants, or (b) ordered merge of overrides from all matching variants. For 1.1.0 recommend **(a)** with compiler-generated combination variants — document that authors/compilers MUST emit explicit combined variants for multi-flag scenes.  
- **Owner:** Spec + Creative Studio  
- **Blocks JSON Schema:** No  
- **Blocks runtime:** Yes (behavior choice)  

#### ADV-H-008 — Environmental flags ordering vs dialogue variants incompletely closed
- **Severity:** HIGH  
- **Affected:** §8.1, §13.4, §25 Q3  
- **Evidence:** Spec “recommends” entry flags before variant selection but leaves open in §25. C09 environmental flag is product-required.  
- **Impact:** Variant selection can miss entry flags if implementers follow a different order.  
- **Required decision:** Close as MUST: on scene entry, apply `environmentalFlagsOnEntry`, then select variants.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** No  
- **Blocks runtime:** Yes  

#### ADV-H-009 — Condition recursion / DoS bounds missing
- **Severity:** HIGH  
- **Affected:** §9.3, §20  
- **Evidence:** Nested `all`/`any`/`not` unbounded. No max depth/size. Security section claims bounded grammar but does not bound nesting.  
- **Impact:** Pathological content can stress validators/engines.  
- **Required decision:** Cap depth (e.g. ≤8) and node count (e.g. ≤64) as static validation MUST.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Custom validator  
- **Blocks runtime:** Yes  

#### ADV-H-010 — `domains` optional while scene `domainId` / domain performance still assumed
- **Severity:** HIGH  
- **Affected:** §5.1, §8.1, current engine `compute_domain_performance`  
- **Evidence:** 1.1.0 makes `domains` optional; scenes retain optional `domainId`. Current terminal results aggregate by `domain_id`. Empty/missing domains break learner completion views.  
- **Impact:** Runtime NPEs or empty domain breakdowns; BA-201 migration hazard.  
- **Required decision:** If any scene has `domainId`, `domains[]` MUST declare it. Or require `domains` whenever completion UI expects domain performance.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** Yes  
- **Blocks runtime:** Yes  

#### ADV-H-011 — Randomized display seed omits `schemaVersion` / content hash; QA reproducibility incomplete
- **Severity:** HIGH  
- **Affected:** §17.2  
- **Evidence:** Seed = `SHA256(attemptId:sceneId:simulationId:version)`. Attempt already pins content hash and engine version. Seed ignores hash — usually OK if version is immutable, but two different compiled payloads must never share `version` (already forbidden). Larger issue: seed algorithm and shuffle primitive (`deterministicShuffle`) are unspecified (Fisher–Yates with which PRNG?).  
- **Impact:** Cross-language non-reproducible order; accessibility/QA drift.  
- **Required decision:** Specify exact shuffle algorithm and PRNG (e.g. SHA256 counter stream). Include `canonicalContentSha256` in seed **or** rely on pinned attempt content only with documented algorithm. Persist nothing.  
- **Owner:** Spec author  
- **Blocks JSON Schema:** No  
- **Blocks runtime:** Yes (UI contract)  

---

### MEDIUM findings

#### ADV-M-001 — `isCorrect: true → acceptable` derivation reinterprets 1.0.0 meaning
- **Severity:** MEDIUM  
- **Affected:** §4.4  
- **Evidence:** 1.0.0 `isCorrect: true` means best-practice. Mapping to `acceptable` (3 pts) not `optimal` (4) changes scoring if used in 1.1.0 fixtures.  
- **Required decision:** Forbid `isCorrect` on 1.1.0 documents entirely; do not derive tiers. Keep binary scoring only under schema 1.0.0 / engine V1.  

#### ADV-M-002 — Flag `consumers[]` as required metadata creates authoring drift
- **Severity:** MEDIUM  
- **Affected:** §12  
- **Evidence:** Consumers can be derived by static scan of variants/caps. Requiring hand-maintained lists drifts from reality.  
- **Required decision:** Treat `consumers` as **documentation-only or compiler-generated**, never runtime-authoritative. Validation SHOULD recompute and MAY warn on mismatch.  

#### ADV-M-003 — `allowedSetters` / `allowedClearers` requiredness ambiguous
- **Severity:** MEDIUM  
- **Affected:** §12.1–12.3  
- **Evidence:** Table does not mark required; validation says “when specified.”  
- **Required decision:** For 1.1.0 production content, require setters for every flag that is set; require clearers iff `sticky: false`.  

#### ADV-M-004 — Outcome reachability default “reject vs warn” still open
- **Severity:** MEDIUM  
- **Affected:** §21.13, §25  
- **Required decision:** For publish pipeline: **warn** on unreachable outcomes; **reject** only if *no* outcome is reachable. Document.  

#### ADV-M-005 — Path-length static analysis with conditional routing underspecified
- **Severity:** MEDIUM  
- **Affected:** §11.5  
- **Evidence:** “All admissible routing resolutions” is exponential; need definition of admissible edges (both corrective and skip edges always included).  
- **Required decision:** Build graph with *both* corrective and skip edges always present; run DAG bounds on that union graph (matches current reconvergent analysis style).  

#### ADV-M-006 — Introduction / characters / flags required for all 1.1.0 scenarios may be over-broad
- **Severity:** MEDIUM  
- **Affected:** §5.1  
- **Evidence:** Spec principle 9 says no speculative features; forcing full intro/character/flag registries on every future simple scenario increases cost.  
- **Required decision:** Keep required for CB-SC-001-class simulators **or** introduce a profile flag; avoid requiring empty registries. Prefer allowing empty `flags: []` and minimal intro.  

#### ADV-M-007 — Debrief `strongestOptionId` not validated against scene options / tier
- **Severity:** MEDIUM  
- **Affected:** §10.3, §16  
- **Required decision:** MUST reference an option in the same scene; SHOULD equal a unique `optimal` option when one exists.  

#### ADV-M-008 — TerminalResult schema not extended for composite / caps fired
- **Severity:** MEDIUM  
- **Affected:** §19, current `serialize_terminal_result`  
- **Evidence:** Debrief needs which caps/guards fired and unrounded composite. Not listed in persistence fields.  
- **Required decision:** Extend **application** terminal snapshot (still jsonb) with `classificationTrace` computed at completion and stored once — or require full recompute from pinned content (acceptable if engine version pinned). Pick one.  

#### ADV-M-009 — Duplicate/stale submission behavior deferred to V68 without schema-level statement
- **Severity:** MEDIUM  
- **Affected:** §20, path trace 10  
- **Evidence:** Spec asserts RPC behavior but does not state that 1.1.0 routing changes must not alter idempotency fingerprint fields.  
- **Required decision:** Affirm fingerprint continues to cover optionId/scene/sequence/state snapshots only — not display order or dialogue variant.  

#### ADV-M-010 — Accessibility/mobile metadata in schema vs “NOT REQUIRED AT RUNTIME” from compatibility review
- **Severity:** MEDIUM  
- **Affected:** §5, §8, compatibility review §5 matrix  
- **Evidence:** Compatibility review classified a11y/mobile as not schema-required; 1.1.0 adds optional objects. Acceptable if presentation-only, but risk of over-encoding UI.  
- **Required decision:** Keep optional; MUST NOT affect scoring/routing (already stated — reinforce in security section).  

---

### LOW findings

#### ADV-L-001 — Expression catalog dual declaration (scenario-level vs inline) underspecified  
#### ADV-L-002 — `cancelBehavior` enum not closed  
#### ADV-L-003 — `communicationType` free string vs enum  
#### ADV-L-004 — Stage IDs unused by routing (OK) but no validation that `stageId` is declared in `stages[]`  
#### ADV-L-005 — Spec says “additive” while making many new fields required — additive for *platform*, not for *document shape*; clarify wording  

---

### NOTE findings

#### ADV-N-001 — Spec correctly rejects DB migration; that claim holds for SQL DDL.  
#### ADV-N-002 — Stable option IDs and V68 submission contracts remain sound.  
#### ADV-N-003 — No unrestricted executable expressions — direction is correct once nesting bounds and leaf-context typing land.  
#### ADV-N-004 — CB-SC-001 seven dimensions are correctly *not* hard-coded into the generic schema.  

---

## Review area results

### 1. Additive compatibility
**Result: PARTIALLY SOUND — not yet safe.**  
1.0.0 load path via `schemaVersion` is correctly preserved in prose. Catalog already stores per-version `schemaVersion`. Historical replay pins engine/content hash today. Gaps: exact engine-compat field, hash circularity, and legacy coexistence ambiguity threaten silent divergence when 1.1.0 content is introduced beside 1.0.0.

### 2. Legacy/new coexistence
**Result: FAIL (BLOCKER).** See ADV-B-005. Explicit precedence table required.

### 3. Top-level contract
**Result: MOSTLY NECESSARY; trim candidates:** `supportedEngineVersions`, variable `outcomeWeight`, hand-authored `consumers`, embedded `canonicalContentSha256`, volatile `compiledAt` inside hashed content. `accessibility`/`mobilePresentation` acceptable as presentation-only.

### 4. Engine compatibility
**Result: FAIL vs repository.** Exact pin only (ADV-H-001). Recommend drop range array.

### 5. Dialogue variants
**Result: WORKABLE with fixes.** First-match + unique priorities + no empty conditions + explicit multi-flag combined variants (ADV-B-003, ADV-H-006, ADV-H-007). Override model cannot change exchange count (override by `exchangeId` only) — good; document that unknown `exchangeId` rejects.

### 6. Condition grammar
**Result: FAIL until leaf-context split and empty-array ban** (ADV-B-003, ADV-B-006, ADV-H-009).

### 7. Formula grammar
**Result: SUFFICIENT for CB-SC-001 with precision/normalization fixes** (ADV-H-004, ADV-H-005). Types are not over-general once weights are constrained.

### 8. Routing
**Result: SOUND DIRECTION; fix trigger leaf and counter timing.** Terminal exclusivity is stated. Corrective no-rebranch is stated. Self-loop example violates cycle rule. `primaryNextSceneId` redundancy when `correctiveRoute` present should be clarified (must equal skip target).

### 9. Corrective budget
**Result: FAIL until increment timing closed** (ADV-B-001). Skipped-corrective as pure replay derivation is **correct** and should remain — do not add DB events.

### 10. Flag registry
**Result: SOUND with metadata demotion** (ADV-M-002, ADV-M-003). Clear-before-set is correct and matches needed clear+set atomicity.

### 11. State model
**Result: SOUND with type/precision and weight cleanup.** Prefer numbers as JSON numbers interpreted as float64; CB-SC-001 deltas are integers but engine already uses float. Counters must not share the float state map.

### 12. Outcome classifier
**Result: ENCODABLE; example bugs and rank semantics need hardening** (ADV-H-003). Seven-step order matches Creative Studio. Tie-break prose is mostly complete.

### 13. Debrief
**Result: MOSTLY SOUND.** Authored seeds + computed impacts is correct. Persist-or-recompute for classification trace still open (ADV-M-008). Seeds must be pinned by content hash — satisfied if content immutable.

### 14. Randomized display
**Result: RIGHT PERSISTENCE CHOICE (do not store); algorithm underspecified** (ADV-H-011). Attempt ID is stable UUID from V68 — suitable seed input. Resume regenerates identically if algorithm fixed.

### 15. Versioning / hashing
**Result: FAIL until hash byte definition** (ADV-B-002). Provenance belongs outside canonical hash or in sidecar.

### 16. Persistence boundary
**Result: NO DB MIGRATION REQUIRED — CONFIRMED.** Application payload/engine snapshot contract **does** change (ADV-H-002). Spec must not say “unchanged.”

### 17. Security / trust
**Result: DIRECTIONALLY CORRECT.** Client still submits option ID + sequence; server resolves. Add nesting bounds; remove unsafe generic leaves.

### 18. Static validation
**Result: INCOMPLETE ASSIGNMENT.** Rules 2,4–8,11–15,17–19 are **custom/graph/semantic**, not JSON Schema. Rules 1 and structural requiredness are JSON Schema. Spec §21 should be reclassified before JSON Schema task.

### 19. Vertical-slice example
**Result: UNSUITABLE AS EXECUTABLE FIXTURE** (ADV-B-003, ADV-B-004). Useful as narrative illustration only after fixes.

### 20. Open-question dispositions

| Question | Disposition | Owner | Deadline | Temporary constraint |
|---|---|---|---|---|
| Counter storage shape | **Decide now:** sibling `counters` object in snapshot, not mixed into float `state` | Engine lead | Before JSON Schema | Forbid counter keys in `stateVariables` |
| C03 slice boundary | **Decide now:** no self-loop; entry-only or terminal fixture option | Spec author | Before fixtures | Reject cyclic examples |
| Environmental flag order | **Decide now:** flags-on-entry → then variants | Spec author | Before runtime | MUST order |
| `endings[]` coexistence | **Decide now:** forbid on 1.1.0 documents | Spec author | Before JSON Schema | 1.0.0 only |
| Outcome reachability | **Decide now:** warn if some unreachable; reject if none | Spec author | Before publish tooling | — |
| Compiler `nextScene` normalization | **Decide now:** compiler emits `routing` only; runtime 1.1.0 rejects raw `nextScene` | Spec + compiler | Before compiler | Dual-read forbidden |

---

## Required path traces

### Trace 1 — C01 normal transition
Option A/B/C → `primaryNextSceneId: SC001-C02`, no correctiveRoute.  
**Result:** Supported. State/flags apply; sequence increments.  
**Issue:** None beyond general counter-order bug (not implicated here).

### Trace 2 — C02 corrective with budget available
Option B/C with `correctiveRoute.triggerWhen` including tier + `correctiveScenesExperienced < 3`.  
**Result:** Intended to enter R2A.  
**Issue:** ADV-B-001 / ADV-B-006 — trigger leaf and counter timing unsafe until fixed.

### Trace 3 — C02 corrective with budget exhausted
When counter ≥ 3, condition false → `whenCorrectiveSkippedNextSceneId: SC001-C03`.  
**Result:** Conceptually correct; **replay-derived skip** is the right persistence choice.  
**Issue:** Depends on ADV-B-001 for counter correctness at check time.

### Trace 4 — R2A reconvergence
All R2A options → C03; `mayRebranch: false`.  
**Result:** Supported and aligned with BCM.  
**Issue:** None unique.

### Trace 5 — Set and clear same flag in one option
Clear-before-set (§12.2) → clear then set leaves flag **set**.  
**Result:** Deterministic and correct.  
**Issue:** None.

### Trace 6 — Dialogue variant with two matching conditions
C03 with both `flag-verbal-handoff-only` and `flag-sales-reengaged`: first-match by priority wins; second ignored.  
**Result:** Deterministic but may lose Creative Studio combined semantics (ADV-H-007).  
**Issue:** HIGH — needs combined authored variants.

### Trace 7 — Terminal outcome classification
Seven-step order matches SOS.  
**Issue:** Rank example error (ADV-H-003); precision (ADV-H-004); endings coexistence (ADV-B-005).

### Trace 8 — Replay after content version changes
Pinned `scenario_version_id` + sha256 + engineVersion reject mismatched content — **existing V68 behavior holds**.  
**Issue:** Hash circularity could falsely break publish/pin (ADV-B-002).

### Trace 9 — Randomized options after resume
Same attemptId+sceneId regenerates order if algorithm fixed.  
**Issue:** Shuffle algorithm unspecified (ADV-H-011). Do not persist order — correct.

### Trace 10 — Duplicate and stale submission
V68 idempotency + sequence/scene/state_before checks remain valid; display order not in fingerprint — correct.  
**Issue:** Spec should explicitly state fingerprint independence from presentation (ADV-M-009).

---

## Readiness assessment

| Gate | Status |
|---|---|
| Zero blockers | **FAIL** (6 blockers) |
| Zero unresolved HIGH | **FAIL** (11 HIGH) |
| Legacy/new precedence explicit | **FAIL** |
| Routing deterministic | **FAIL** until ADV-B-001/006 |
| Condition/formula bounded & unambiguous | **FAIL** until empty-array ban, depth caps, weight rules |
| Hash semantics defined | **FAIL** |
| Replay/version semantics defined | **PARTIAL** (good pin model; bad hash) |
| Field structure stable | **FAIL** |

**Executable JSON Schema readiness:** **NOT READY**  
**Runtime implementation readiness:** **NOT READY**

---

## Recommended correction sequence

1. Resolve ADV-B-005 coexistence table (forbid `endings[]`/`nextScene`/`isCorrect` on 1.1.0 documents).  
2. Fix ADV-B-001 mutation/counter order and rewrite §22.1.  
3. Fix ADV-B-002 hash byte definition; move provenance out of hash.  
4. Fix ADV-B-003 / ADV-B-006 condition grammar (ban empty compositions; remove tier leaf from generic grammar).  
5. Replace §23 vertical-slice fixture (ADV-B-004).  
6. Resolve all HIGH items (engine exact pin, counters sibling map, formula precision, unique variant priorities, multi-flag policy, env-flag order, shuffle algorithm, domains rule, cap rank examples).  
7. Reclassify §21 validators (JSON Schema vs custom).  
8. Re-run adversarial review (SIM-SCHEMA-11-REVIEW-02) before JSON Schema encoding.

---

## Recommended next action

**SIM-SCHEMA-11-SPEC-02 — Correct the normative specification** against ADV-B-* and ADV-H-* findings only. Do **not** author `simulation.schema.json` until a follow-up review records zero blockers and zero unresolved highs.

---

## Completion report

1. **Task status:** Complete (review-only)  
2. **Review file created:** `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_ADVERSARIAL_REVIEW.md`  
3. **Repository branch:** `main` (ahead 17 of `origin/main`)  
4. **Starting git status:** clean tracked tree; untracked `.local/`, `docs/scenario_simulator/`, `local_only/`, v68 bundles, protected policy files  
5. **Ending git status:** same + this adversarial review under `docs/scenario_simulator/`  
6. **Total findings:** 36  
7. **Blocker count:** 6  
8. **High count:** 11  
9. **Medium count:** 10  
10. **Low count:** 5 (+ 4 notes)  
11. **Additive-compatibility:** Partially sound; not yet safe  
12. **Legacy/new coexistence:** Fail — precedence table required  
13. **Engine compatibility:** Fail — exact pin only  
14. **Dialogue-variant:** Workable with unique priorities + multi-flag policy  
15. **Condition-grammar:** Fail until empty-array ban + leaf-context split  
16. **Formula-grammar:** Sufficient with normalization/precision fixes  
17. **Routing:** Sound direction; counter/trigger issues block  
18. **Corrective-budget:** Fail until increment timing closed; skip replay-derived OK  
19. **Flag-registry:** Sound; demote consumers  
20. **State-model:** Sound with counter separation  
21. **Outcome-classifier:** Encodable; harden ranks/examples  
22. **Debrief:** Mostly sound; classification-trace persist/recompute open  
23. **Randomized-display:** Right persistence choice; specify shuffle  
24. **Version/hash:** Fail — circular/undefined hash  
25. **Persistence:** No DB migration; **yes** application payload change  
26. **Security:** Directionally correct; add depth bounds  
27. **Static-validation:** Incomplete JSON Schema vs custom assignment  
28. **Vertical-slice example:** Unsuitable as executable fixture  
29. **Open-question dispositions:** All six closed with recommended decisions above  
30. **Path-trace results:** Traces 1/4/5 OK; 2/3/6/7/8/9 blocked or conditional on findings; 10 OK with clarification  
31. **Executable JSON Schema readiness:** **NOT READY**  
32. **Runtime implementation readiness:** **NOT READY**  
33. **Files modified:** None (created this review only)  
34. **Source files untouched:** Spec + compatibility review unchanged  
35. **Protected paths untouched:** Confirmed  
36. **Nothing staged/committed/pushed/deployed:** Confirmed  
37. **Errors encountered:** None  
38. **Recommended correction sequence:** See section above  
39. **Recommended next action:** SIM-SCHEMA-11-SPEC-02 corrective rewrite of the normative spec  

---

*End of adversarial review.*
