# Schema 1.1.0 JSON Authoring Report

**Task ID:** SIM-SCHEMA-11-JSON-01  
**Date:** 2026-07-30  
**Authoring only:** no runtime, compiler, migration, or spec modifications.

---

## Executive summary

Created the executable JSON Schema for CertBound Scenario Simulator content **1.1.0** and a companion custom-validation contract. The schema uses **JSON Schema Draft 2020-12** (same as 1.0.0), loads successfully via `jsonschema.Draft202012Validator`, and passes structural self-check. Synthetic validation confirms required rejections for legacy fields, empty condition groups, routing contradictions, malformed tiers/SHA-256, and learner-as-character patterns.

**Normative divergence (reported, schema not weakened):** Revision-2 illustrative JSON uses `"learner"` in `charactersPresent` and omits `learnerPresent`. Per JSON-01 disposition, the executable schema requires `learnerPresent: boolean` and forbids `"learner"` in `charactersPresent`. The spec fixture needs a future spec revision to conform.

**Database migration required:** No  
**Runtime implementation performed:** No  
**Recommended next action:** **SIM-SCHEMA-11-VALIDATOR-01** — implement layered validators (`validate_scenario_1_1_0`) and wire catalog publish path for `schemaVersion: "1.1.0"`.

---

## Files created

| File | Purpose |
|---|---|
| `scenario_content/schemas/1.1.0/simulation.schema.json` | Executable JSON Schema |
| `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md` | Custom/graph/semantic/publication/runtime rule catalog |
| `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_JSON_AUTHORING_REPORT.md` | This report |

**Files modified:** None (existing 1.0.0 schema, runtime, tests, and normative spec untouched).

---

## Schema structure summary

- **Draft:** `https://json-schema.org/draft/2020-12/schema`
- **`$id`:** `https://certbound.com/schemas/scenario_content/1.1.0/simulation.schema.json`
- **Root:** closed object (`additionalProperties: false`)
- **Reusable defs:** 55 entries in `$defs`
- **Pattern:** composition via `$ref`, `oneOf` (conditions, formulas), `allOf`+`if`/`then` (terminal routing), `not` (learner exclusion)

### Top-level required fields (17)

`simulationId`, `version`, `schemaVersion`, `requiredEngineVersion`, `certificationExamName`, `examCode`, `title`, `learnerRole`, `introduction`, `characters`, `flags`, `stateVariables`, `initialState`, `runtimeCounters`, `scenes`, `startScene`, `outcomeClassifier`, `outcomes`

### Top-level optional fields

`description`, `estimatedMinutes`, `locale`, `contentProvenance`, `canonicalContentSha256`, `publicationMetadata`, `accessibility`, `mobilePresentation`, `correctiveBudgetPolicy`, `stages`, `domains`, `optionDisplayPolicy`, `debriefTemplate`

**Forbidden at root:** `endings`, `initialState`-only 1.0 patterns without 1.1.0 replacements, legacy routing fields.

---

## `$defs` inventory (55)

| Category | Definitions |
|---|---|
| Primitives | `nonEmptyString`, `sha256Hex`, `terminalSentinel`, `evaluationTier`, `compareOp`, `statePolarity`, `sceneType`, `registeredCharacterId`, `speakerId` |
| Conditions | `condition`, `conditionAll`, `conditionAny`, `conditionNot`, `conditionFlagSet`, `conditionFlagNotSet`, `conditionStateCompare`, `conditionCounterCompare` |
| Intro/identity | `learnerRole`, `introduction` |
| Registry | `character`, `flag`, `flagWriterRef`, `stateVariable`, `runtimeCounter`, `counterIncrementOnCorrective`, `counterIncrementOnDecision`, `correctiveBudgetPolicy`, `stage`, `domain` |
| Dialogue | `exchange`, `dialogueOverride`, `dialogueVariant`, `dialogue` |
| Routing/options | `correctiveRoute`, `routing`, `debriefSeed`, `reactionDialogue`, `option`, `decision`, `correctiveMetadata`, `scene` |
| Classifier | `formulaWeightedDimensionHealth`, `formulaTierAverage`, `formulaLinearBlend`, `formulaIdentity`, `formula`, `capEffectForceOutcome`, `capEffectMaxOutcome`, `severeCap`, `moderateCap`, `strongGuard`, `scoreBand`, `outcomeClassifier`, `outcome`, `debriefTemplate` |

---

## Normative mappings

| Spec section | Schema encoding |
|---|---|
| §5 Top-level | Root properties + requiredness |
| §6 Introduction / learnerRole | `$defs/introduction`, `$defs/learnerRole` |
| §7 Characters | `$defs/character`; `"learner"` forbidden as `characterId` |
| §8 Scenes | `$defs/scene`; **`learnerPresent` added per JSON-01**; no scene `narrative` |
| §9 Dialogue/conditions | `$defs/dialogue`, bounded `$defs/condition` |
| §10 Options | `$defs/option`; tiers enum; `debriefSeed` required |
| §11 Routing | `$defs/routing`, `$defs/correctiveRoute`; terminal if/then |
| §12 Flags | `$defs/flag`; boolean only; `initialValue: false` |
| §13 State/counters | `$defs/stateVariable`, `$defs/runtimeCounter` |
| §14 Classifier | `$defs/outcomeClassifier` + formula oneOf |
| §15 Outcomes | `$defs/outcome` |
| §17 Display policy | `optionDisplayPolicy` enum |
| §18 Hash/provenance | `canonicalContentSha256` pattern; open provenance objects |
| §4.3 Legacy forbid | Closed objects exclude legacy fields |

---

## Custom-validator boundary

**67 rules** cataloged in `SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md` (IDs CV-001..CV-107).

| Layer | Approx. count |
|---|---|
| JSON Schema (JS) | ~25 structural rules |
| Custom structural (CS) | ~12 |
| Graph (GR) | ~10 |
| Semantic (SM) | ~22 |
| Publication (PB) | ~8 |
| Runtime (RT) | ~12 |

Skipped-corrective audit, hash verification, display-order runtime, and debrief computed fields are documented in the companion — **not** in content schema.

---

## Synthetic validation results

Environment: Python 3.12, `jsonschema` Draft202012Validator.

| Test | Expected | Result |
|---|---|---|
| Schema parse | OK | **PASS** |
| `check_schema` | OK | **PASS** |
| Minimal valid 1.1.0 synthetic document | Accept | **PASS** |
| Empty `all: []` | Reject | **PASS** (rejected) |
| Empty `any: []` | Reject | **PASS** (rejected) |
| Prohibited `nextScene` on option | Reject | **PASS** (rejected) |
| Prohibited `isCorrect` | Reject | **PASS** (rejected) |
| Prohibited scene `narrative` | Reject | **PASS** (rejected) |
| Prohibited root `endings` | Reject | **PASS** (rejected) |
| Terminal + correctiveRoute + wrong sentinel | Reject | **PASS** (rejected) |
| Malformed `evaluationTier` | Reject | **PASS** (rejected) |
| Missing `primaryNextSceneId` | Reject | **PASS** (rejected) |
| Malformed SHA-256 | Reject | **PASS** (rejected) |
| `"learner"` as `characterId` | Reject | **PASS** (rejected) |
| `"learner"` in `charactersPresent` | Reject | **PASS** (rejected) |

---

## Illustrative JSON (spec §23) — representability

**Does not validate** against executable schema without normative/example updates:

1. **Missing `learnerPresent`** on all four scenes (now required).
2. **`"learner"` in `charactersPresent`** on all four scenes (forbidden per JSON-01 disposition).

All other structures in the fixture are representable. To conform, each scene would need e.g. `"learnerPresent": true` and `charactersPresent` lists containing only registered IDs (e.g. `["CB-CH-001", "CB-CH-002"]` without `"learner"`).

**Action:** Report mismatch; do **not** modify normative spec in this task. Future **SIM-SCHEMA-11-SPEC-03** (editorial) should update §8 and §23 example.

---

## Known JSON Schema limitations

Documented in companion §E. Key gaps requiring custom validators:

- Duplicate ID detection across arrays
- Reference resolution and flag authorization
- Graph cycles/reachability/path length
- Conditional `correctiveBudgetPolicy` / `correctiveMetadata`
- Formula weight sum ± 1e-9
- Outcome reachability (publication fail-closed)
- Canonical hash verification
- Condition depth (8) and node count (64)

Recursive condition `$ref` is bounded in practice by custom validation; JSON Schema alone does not count nodes.

---

## Readiness

| Gate | Status |
|---|---|
| Valid JSON schema file | **Ready** |
| Loads in repository jsonschema library | **Ready** |
| Legacy prohibition encoded | **Ready** |
| Companion custom rules documented | **Ready** |
| Validator Python module | **Not started** (next task) |
| Engine V2 runtime | **Not started** |

---

## Completion report

1. **Task status:** Complete (authoring only)
2. **Files created:** 3 (schema + custom validation + this report)
3. **Repository branch:** `main` (ahead 17)
4. **Starting git status:** untracked `docs/scenario_simulator/` + out-of-scope items
5. **Ending git status:** + `scenario_content/schemas/1.1.0/simulation.schema.json` + 2 new docs under `docs/scenario_simulator/`
6. **JSON Schema draft/version:** Draft **2020-12**
7. **Schema file parse result:** **PASS**
8. **Schema library load result:** **PASS** (`Draft202012Validator.check_schema`)
9. **`$defs` count:** **55**
10. **Top-level required fields:** 17 (listed above)
11. **Legacy-field prohibition result:** **PASS** (`nextScene`, `isCorrect`, scene `narrative`, `endings` rejected via closed objects)
12. **Dialogue schema result:** **PASS** (nonempty exchanges; variants/overrides/conditions encoded)
13. **Condition grammar result:** **PASS** (bounded oneOf; empty all/any rejected)
14. **Option schema result:** **PASS** (`id`, `text`, `evaluationTier`, `feedback`, `routing`, `debriefSeed`)
15. **Routing schema result:** **PASS** (terminal if/then; correctiveRoute shape)
16. **Flag schema result:** **PASS** (boolean, initial false, writer refs)
17. **State-variable schema result:** **PASS** (numeric contract; no outcomeWeight)
18. **Counter schema result:** **PASS** (separate declarations; incrementOn events)
19. **Corrective-budget schema result:** **PASS** (policy object; conditional requirement in custom validation)
20. **Formula schema result:** **PASS** (four bounded types)
21. **Outcome-classifier schema result:** **PASS** (seven-step metadata + caps/guards/bands)
22. **Outcome schema result:** **PASS** (generic outcomes; narrative allowed on outcomes not scenes)
23. **Debrief schema result:** **PASS** (`debriefSeed` on options; template open object)
24. **Randomized-display schema result:** **PASS** (policy enum; runtime in companion)
25. **Engine compatibility result:** **PASS** (exact `requiredEngineVersion` string)
26. **Hash/provenance result:** **PASS** (SHA-256 pattern; verification in companion)
27. **Learner-presence result:** **PASS** (`learnerPresent` required; `"learner"` forbidden in registry/present arrays; allowed in `speakerId`)
28. **Custom-validation rule count:** **67**
29. **Validation-layer classification:** JS / CS / GR / SM / PB / RT (companion §D)
30. **Minimal valid synthetic document result:** **PASS**
31. **Empty-all rejection result:** **PASS**
32. **Empty-any rejection result:** **PASS**
33. **Legacy nextScene rejection result:** **PASS**
34. **Legacy isCorrect rejection result:** **PASS**
35. **Legacy narrative rejection result:** **PASS**
36. **Legacy endings rejection result:** **PASS**
37. **Terminal/routing contradiction rejection result:** **PASS**
38. **Evaluation-tier rejection result:** **PASS**
39. **Missing-routing-field rejection result:** **PASS**
40. **SHA-256 rejection result:** **PASS**
41. **Learner-as-character rejection result:** **PASS**
42. **Normative divergence:** Illustrative JSON missing `learnerPresent`; uses `"learner"` in `charactersPresent` (JSON-01 disposition; spec §8/§23 not yet updated)
43. **Database migration required:** **No**
44. **Runtime implementation performed:** **No**
45. **Files modified:** **None**
46. **Confirmation existing source files untouched:** **Yes** (1.0.0 schema, runtime, tests, normative spec unchanged)
47. **Confirmation protected paths untouched:** **Yes**
48. **Confirmation nothing staged, committed, pushed, or deployed:** **Yes**
49. **Errors encountered:** None blocking
50. **Remaining risks:** Spec example/schema mismatch until spec editorial pass; custom validators not yet implemented; conditional correctiveBudgetPolicy not expressible purely in JS
51. **Recommended next action:** **SIM-SCHEMA-11-VALIDATOR-01** — implement Python layered validator + publish integration; optionally **SIM-SCHEMA-11-SPEC-03** to align §8/§23 with `learnerPresent`

---

*End of JSON authoring report.*
