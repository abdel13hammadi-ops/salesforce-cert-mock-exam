# Schema 1.1.0 Focused Confirmation Review (Revision 2)

**Task ID:** SIM-SCHEMA-11-REVIEW-02  
**Type:** Focused independent confirmation review  
**Reviewed:** `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md` (revision 2 / SPEC-02)  
**Inputs:** correction report, prior adversarial review, compatibility review  
**Date:** 2026-07-30  
**Constraint:** Review-only. Spec, reports, runtime, and protected paths untouched.

---

## Executive verdict

**All six ADV-B blockers and all eleven ADV-H findings are closed.**  
**No new blockers. No new high-severity defects.**

Revision 2 is **internally consistent enough to author the executable JSON Schema and custom validators**. Remaining notes are medium/low clarifications that do **not** change content field names, types, requiredness, exclusivity, or core semantics.

| Metric | Count |
|---|---|
| Remaining blockers | **0** |
| Remaining high findings | **0** |
| New blockers | **0** |
| New high findings | **0** |
| New medium | 3 |
| New low / notes | 4 |

**Executable JSON Schema readiness:** **READY**  
**Runtime implementation readiness:** **READY to begin after JSON Schema** (application snapshot details listed as MEDIUM do not block schema encoding of content documents).

---

## A–F. Blocker revalidation

### ADV-B-001 — Corrective counter semantics — **CLOSED**

| Check | Result |
|---|---|
| Single order everywhere | **Pass.** §11.3 is the sole normative order; §11.2 resolves without incrementing; §19.3 replay mirrors §11.3; prior contradictory narratives voided. |
| Budget check before entry | **Pass.** Step 5 resolves with **pre-entry** `correctiveScenesExperienced`. |
| Increment only on committed corrective entry | **Pass.** Step 6; idempotent duplicate does not re-increment. |
| Skip: no increment + audit | **Pass.** Step 7 + §11.2 `skippedCorrective` object. |
| Exact limit | **Pass.** `max-1` allows entry; `== max` skips. |
| Duplicate / stale / interrupted | **Pass.** Explicitly covered in §11.3. |
| Illustrative JSON | **Pass.** Budget condition uses `lt 3`; counters declared separately. |
| Contradictory sentences | **None found.** |

**Note (not a reopen):** §19.2 marks `routingResolutions` **optional** while §11.2 says skip audit **MUST** be recorded on decision result **and/or** snapshot. Recording is normative; storage location has an OR. See NEW-M-001.

### ADV-B-002 — Canonical hash — **CLOSED**

| Check | Result |
|---|---|
| Digest excluded from input | **Pass.** §18.1 step 2 omits `canonicalContentSha256`. |
| Provenance / publication excluded | **Pass.** Same step. |
| Deterministic normalization | **Pass.** `sort_keys=True`, `ensure_ascii=False`, `separators=(",", ":")`, UTF-8, lowercase hex — matches `utils/scenario_schema.compute_canonical_content_sha256` **intent**. |
| Helper call shape | **Acknowledged correctly.** Existing helper hashes the whole object; callers MUST pass stripped `D'` or a wrapper. Inspected helper at `utils/scenario_schema.py:164-171`. |
| Circular hash | **Absent.** |
| Publish recompute + replay pin | **Pass.** |

**Note:** Replay pseudocode writes `hash(pinnedContent)` without restating strip — implementers must use §18.1. See NEW-M-002 (does not reopen ADV-B-002).

### ADV-B-003 — Condition groups — **CLOSED**

| Check | Result |
|---|---|
| `all`/`any` min 1 child | **Pass.** Forbidden empty arrays. |
| `not` exactly one child | **Pass** (BNF / composition forms). |
| Structural dialogue fallback | **Pass.** Base exchanges; no empty-condition fallback variant. |
| Depth / node limits | **Pass.** 8 / 64. |
| Missing refs fail closed | **Pass.** |
| Illustrative empty groups | **Absent.** Combined variant uses nonempty `all`. |

### ADV-B-004 — C03 slice boundary — **CLOSED**

| Check | Result |
|---|---|
| Self-loop | **Removed.** |
| C03 terminal | **Pass.** All three options: `terminal: true`, `primaryNextSceneId: "EVALUATE_ENDING"`. |
| Dual description as nonterminal boundary | **Absent.** Text says executable scene that terminates for fixture validity. |
| Cycle validation weakened | **No.** |
| Fixture usability | **Pass.** Valid DAG; min 3 / max 4 scored decisions matches paths. |

### ADV-B-005 — Legacy/new exclusivity — **CLOSED**

| Pair | 1.1.0 rule | Example compliance |
|---|---|---|
| `nextScene` vs `routing` | `nextScene` **FORBIDDEN** | No `nextScene` |
| `isCorrect` vs `evaluationTier` | `isCorrect` **FORBIDDEN** | No `isCorrect` |
| `narrative` vs `dialogue` | Scene `narrative` **FORBIDDEN** | No scene narrative; outcomes may have `narrative` (distinct field — OK) |
| `endings[]` vs classifier | `endings[]` **FORBIDDEN** | No `endings` |

1.0.0 authority unchanged (§4.2). No runtime dual-read. Field tables, matrix §24, and pseudocode agree.

### ADV-B-006 — Tier-based routing — **CLOSED**

| Check | Result |
|---|---|
| `optionTierInCurrentDecision` removed | **Pass** (grammar, appendix, examples). |
| Equivalent generic leaf | **None found.** |
| `triggerOnTiers` option-owned | **Pass.** On `correctiveRoute` only. |
| Server-resolved tier | **Pass.** |
| Conflict with budget | **None.** Tier gate then budget gate — sequential, deterministic. |
| Options without correctiveRoute | **Pass.** C01 all; C02-A; all R2A; all C03. |

**Assessment of necessity:** `triggerOnTiers` is slightly redundant with “only put `correctiveRoute` on triggering options,” but it makes the tier gate explicit and validatable (`evaluationTier ∈ triggerOnTiers`). **Not ambiguous enough to require removal.**

---

## ADV-H closure result — **ALL CLOSED**

| ID | Topic | Confirmation |
|---|---|---|
| ADV-H-001 | Engine compat | Exact `requiredEngineVersion`; ranges removed |
| ADV-H-002 | Persistence | No DB migration; application snapshot **CHANGED**; sibling `counters` |
| ADV-H-003 | Cap ranks | `maxOutcomeId: "partial_resolution"` in example |
| ADV-H-004 | Formula precision | float64; weights sum 1±1e-9; missing reject; round after classify; half-away-from-zero |
| ADV-H-005 | Variable weights | `outcomeWeight` removed |
| ADV-H-006 | Variant priority | Unique priorities required |
| ADV-H-007 | Multi-flag | First-match + authored `c03-both-flags` combined variant |
| ADV-H-008 | Env flags | Before variant selection (§11.3 step 8, §9.2) |
| ADV-H-009 | Condition DoS | Depth 8 / nodes 64 |
| ADV-H-010 | Domains | Conditional requirement if `domainId` used |
| ADV-H-011 | Shuffle | Seed includes hash; Fisher–Yates; store `optionDisplayOrder`; submit optionId |

---

## Corrected model checks

### 1. Counters — **PASS**
Separate `runtimeCounters` declarations; snapshot sibling `counters`; init/increment/replay/validation access defined via §11.3 + §13.2–13.3.

### 2. Skipped-corrective audit — **PASS with MEDIUM clarification**
- Replay-derived determination: **yes** (recompute from content + counters).  
- Server-generated routing result: **yes** (`skippedCorrective` object).  
- Snapshot verification: **MAY** verify stored event.  
- Normative recording: **MUST** record somewhere (decision result and/or snapshot).  
See NEW-M-001 for location OR.

### 3. Dialogue variants — **PASS**
Base fallback; unique priorities; ascending first-match; env flags from committed prior + entry flags; stable `exchangeId` overrides; replay-derived; `selectedVariantId` optional audit only.

### 4. Formula model — **PASS**
Python float64 target clear; weight tolerance defined; missing/zero-divisor/cycles reject; unrounded classify; display round after. Cross-language bit-identity not claimed — acceptable for CertBound Python runtime (NEW-N-001).

### 5. Outcome classifier — **PASS**
Seven-step order explicit; `maxOutcomeId` preferred; severe stop; bands contiguous in example; unique ranks/ids; fail-closed reachability.

### 6. Randomized display — **PASS**
Seed inputs complete; Fisher–Yates + SHA256 stream; server returns + stores order; optionId submission; resume/replay verify; content hash in seed + attempt pin prevents silent historical reshuffle; copied attempt / instructor behavior defined.

### 7. Persistence — **PASS**
No DB migration; application jsonb contract acknowledged; counters separate; pins and reconstructible derived state; client hidden-state rejected; replay uses pinned content.

### 8. Validation ownership — **PASS**
§21 classifies layers. Does **not** claim JSON Schema alone enforces reachability, cycles, hash, or engine availability.

---

## Illustrative JSON — **PASS (internally valid)**

Manual checks against normative tables:

| Check | Result |
|---|---|
| Defined fields only | Pass |
| Required top-level / scene / option fields | Pass |
| Prohibited legacy fields | Absent |
| Unique scene/option/flag/variant/exchange/outcome ids | Pass |
| Flag / state / counter references | Resolve |
| Nonempty conditions | Pass |
| Corrective routing / reconvergence equality | Pass |
| C03 → `EVALUATE_ENDING` | Pass |
| Classifier complete for fixture | Pass (severe/moderate/guards/bands/outcomes) |
| Variant override targets | Resolve (`ex-002`) |
| Hash placeholders | Omitted correctly; applied at publish |
| Engine compatibility | `requiredEngineVersion: SCENARIO_ENGINE_V2` |

**Minor:** `charactersPresent` includes `"learner"` while `characters[]` has no learner entry. Spec allows `speakerId: "learner"`; `charactersPresent` wording says characterId list. See NEW-M-003 — does not invalidate routing/scoring.

---

## Path traces

| # | Trace | Deterministic unique result? |
|---|---|---|
| 1 | C01 normal → C02 | **Yes** |
| 2 | C02-A non-corrective → C03 | **Yes** |
| 3 | C02-B/C corrective, budget available → R2A + increment | **Yes** |
| 4 | C02-B/C, budget exhausted → C03 + skip event, no increment | **Yes** |
| 5 | R2A → C03 reconvergence | **Yes** |
| 6 | Clear then set same flag | **Yes** (clear-before-set → ends set) |
| 7 | Multi-matching dialogue variants | **Yes** (unique priority; first match; combined variant at 5) |
| 8 | C03 terminal classification | **Yes** (seven-step; CAP-F01 if unsupported-date flag) |
| 9 | Resume with stored display order | **Yes** (verify stored order) |
| 10 | Replay with pinned hash | **Yes** (identity assert; strip per §18) |
| 11 | Duplicate submission | **Yes** (idempotent; no second increment) |
| 12 | Stale sequence | **Yes** (reject; no mutation) |

---

## New findings (post-correction only)

### NEW-M-001 — Skip-audit storage location uses “and/or” vs optional `routingResolutions`
- **Severity:** MEDIUM  
- **Affected:** §11.2, §19.2  
- **Evidence:** MUST record skip on decision result **and/or** snapshot; snapshot field listed as optional.  
- **Impact:** Implementers may disagree where the normative audit lives; does not change content schema fields.  
- **Required correction:** Before runtime persistence work, pick one: (a) require `routingResolutions` on V2 snapshots, or (b) require skip fields on the decision persistence outcome only and keep snapshot optional.  
- **Owner:** Engine/persistence lead  
- **Blocks JSON Schema:** **No**  
- **Blocks runtime:** No (blocks only the persistence-detail task)

### NEW-M-002 — Replay pseudocode `hash(pinnedContent)` omits explicit strip
- **Severity:** MEDIUM  
- **Affected:** §19.3 vs §18.1  
- **Evidence:** §18 requires hashing `D'`; replay line says `hash(pinnedContent)`.  
- **Impact:** Naive call including digest would fail verification.  
- **Required correction:** Editorial: `hash(stripExcluded(pinnedContent))` in §19.3.  
- **Owner:** Spec (editorial)  
- **Blocks JSON Schema:** **No**  
- **Blocks runtime:** No if §18 followed

### NEW-M-003 — `"learner"` in `charactersPresent` vs character registry
- **Severity:** MEDIUM  
- **Affected:** §8, §23 example  
- **Evidence:** Example lists `"learner"` in `charactersPresent`; registry has only CB-CH-* ids; `speakerId` specially allows `"learner"`.  
- **Impact:** Over-strict characterPresent validation could reject a valid fixture.  
- **Required correction:** Allow `"learner"` in `charactersPresent`, or omit it from the array in fixtures.  
- **Owner:** Spec (editorial) / JSON Schema author  
- **Blocks JSON Schema:** **No** if schema encodes the special case  
- **Blocks runtime:** No

### NEW-L-001 — Leftover phrase “Non-fallback (all) variants” after fallback removal  
### NEW-L-002 — Rounding sentence still mentions half-even before mandating half-away-from-zero (harmless)  
### NEW-N-001 — float64 is Python-centric; cross-language bit identity not claimed (acceptable)  
### NEW-N-002 — `triggerOnTiers` is mildly redundant with option-owned routes but not ambiguous  

---

## Readiness standard checklist

| Gate | Status |
|---|---|
| Remaining blockers = 0 | **Met** |
| Remaining high = 0 | **Met** |
| New blockers = 0 | **Met** |
| New high = 0 | **Met** |
| Illustrative example internally valid | **Met** |
| No unresolved question changing field names/types/requiredness/exclusivity/semantics | **Met** (MEDIUM items are storage-location / editorial / learner-id special case) |

---

## Recommended next action

**SIM-SCHEMA-11-JSON-01** — Author `scenario_content/schemas/1.1.0/simulation.schema.json` plus a companion custom-validation document covering graph, semantic, publication, and hash rules from §21.

Optionally fold NEW-M-001…003 as one-line editorials in a tiny SPEC-02.1 patch **before or during** JSON Schema authoring; they are not blockers for starting JSON Schema.

---

## Completion report

1. **Task status:** Complete (review-only)  
2. **Review file created:** `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_FOCUSED_REVIEW.md`  
3. **Repository branch:** `main` (ahead 17)  
4. **Starting git status:** untracked `docs/scenario_simulator/` + out-of-scope items; clean tracked tree  
5. **Ending git status:** same + this focused review under `docs/scenario_simulator/`  
6. **Remaining blockers:** 0  
7. **Remaining highs:** 0  
8. **New blockers:** 0  
9. **New highs:** 0  
10. **ADV-B-001:** CLOSED  
11. **ADV-B-002:** CLOSED  
12. **ADV-B-003:** CLOSED  
13. **ADV-B-004:** CLOSED  
14. **ADV-B-005:** CLOSED  
15. **ADV-B-006:** CLOSED  
16. **ADV-H closure:** All 11 CLOSED  
17. **Counter semantics:** Pass  
18. **Counter storage:** Pass (sibling object)  
19. **Skipped-corrective audit:** Pass (NEW-M-001 location OR)  
20. **Canonical hash:** Pass (helper strip acknowledged)  
21. **Legacy/new exclusivity:** Pass  
22. **Engine compatibility:** Pass (exact pin)  
23. **Dialogue variants:** Pass  
24. **Condition grammar:** Pass  
25. **Formula grammar:** Pass  
26. **Routing:** Pass  
27. **Flag registry:** Pass  
28. **State variables:** Pass  
29. **Outcome classifier:** Pass  
30. **Randomized display:** Pass  
31. **Persistence boundary:** Pass (no DB migration; app contract changed)  
32. **Security boundary:** Pass  
33. **Validation ownership:** Pass  
34. **Illustrative JSON:** Pass (NEW-M-003 learner note)  
35. **Path traces:** All 12 deterministic  
36. **Executable JSON Schema readiness:** **READY**  
37. **Runtime implementation readiness:** Ready after schema (persist detail MEDIUM open)  
38. **Files modified:** None (created this review only)  
39. **Source files untouched:** Spec, correction report, adversarial review, compatibility review unchanged  
40. **Protected paths untouched:** Confirmed  
41. **Nothing staged/committed/pushed/deployed:** Confirmed  
42. **Errors:** None  
43. **Remaining risks:** Skip-audit storage location; hash strip in replay pseudocode wording; learner in charactersPresent; deferred LOW polish  
44. **Recommended next action:** **SIM-SCHEMA-11-JSON-01**

---

*End of focused confirmation review.*
