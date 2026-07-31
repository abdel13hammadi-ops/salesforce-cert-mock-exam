# Schema 1.1.0 JSON Focused Review

**Task ID:** SIM-SCHEMA-11-JSON-REVIEW-01  
**Type:** Focused independent review (review-only)  
**Reviewed:** `scenario_content/schemas/1.1.0/simulation.schema.json`, `SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md`  
**Normative baseline:** `SCENARIO_SCHEMA_1_1_0_SPEC.md` (revision 3)  
**Date:** 2026-07-30

---

## Executive verdict

The executable JSON Schema is **meta-valid**, loads with `Draft202012Validator`, resolves all `$ref` targets, and **substantially implements** revision-3 semantics. Legacy prohibition, learner presence, bounded condition grammar, routing shape, formula discriminators, and closed-object discipline are sound. The custom-validation companion is implementable, noncontradictory, and correctly assigns graph/semantic/publication rules JSON Schema cannot enforce.

**One HIGH requiredness mismatch** (`debriefSeed` on options) prevents a clean “zero high findings” readiness claim against revision 3. It does **not** block starting validator scaffolding if implementers treat the **executable schema** as authoritative and track the spec alignment fix separately.

| Metric | Count |
|---|---|
| Total findings | **8** |
| Blockers | **0** |
| High | **1** |
| Medium | **4** |
| Low | **2** |
| Note | **1** |

**Validator implementation readiness:** **CONDITIONAL READY** (proceed using executable schema; resolve JR-H-001 via spec or schema alignment task first for strict normative parity)  
**Runtime implementation readiness:** **Not in scope** (requires validator + engine V2)

---

## 1. Schema meta-validity — **PASS**

| Check | Result |
|---|---|
| Valid JSON | Pass |
| `$schema` = Draft 2020-12 | Pass |
| `Draft202012Validator.check_schema` | Pass (reproduced) |
| All `$ref` resolve | Pass — 55 `$defs`, 0 missing ref targets |
| Duplicate `$defs` names | None |
| Recursion (`condition`) | Intentional; depth/node limits in custom validation (CV-032, CV-033) |
| Repository dependency | `jsonschema>=4.23.0,<5.0.0` in `requirements.txt`; supports Draft 2020-12 `if`/`then`/`not`, `$defs`, `oneOf` |

Critical definitions are all referenced. No unused critical def identified.

---

## 2. Normative field-mapping summary

### 2.1 Exact matches (representative)

| Area | Normative (rev 3) | Schema | Status |
|---|---|---|---|
| Top-level identity/version | `simulationId`, `version`, `schemaVersion`, `requiredEngineVersion` | Same names/types/requiredness | Match |
| Learner presence | `learnerPresent` boolean required; `charactersPresent` registry IDs only | Same | Match |
| Option identity | `id`, `text`, `evaluationTier`, `feedback`, `routing` | Same | Match |
| Flag mutation | `setFlags`, `clearFlags` | Same | Match |
| Legacy forbid | `nextScene`, `isCorrect`, scene `narrative`, `endings[]` | Closed objects reject | Match |
| Condition leaves | four leaf forms only | `oneOf` + closed objects | Match |
| Engine | exact `requiredEngineVersion` | string minLength 1 | Match |
| Hash | 64 lowercase hex or null | `sha256Hex` pattern + null | Match |

### 2.2 Intentional naming (not defects)

| Review prompt term | Revision-3 normative name | Classification |
|---|---|---|
| `optionId` (on options) | `id` | **Normative** — stable option key |
| `learnerResponse` | `text` | **Normative** |
| `visibleConsequence` (immediate) | `feedback` | **Normative** — immediate consequence; `visibleConsequence` is separate optional field |
| `optionId` in flag writers | `optionId` in `allowedSetters` | **Normative** — references option `id` |

### 2.3 Mismatch table

| Field / rule | Normative rev 3 | Executable schema | Severity |
|---|---|---|---|
| `option.debriefSeed` | Optional (§10) | **Required** on every option | **HIGH** — JR-H-001 |
| `correctiveBudgetPolicy` | Required when corrective scene exists (§11.5) | Optional at root | **MEDIUM** — correctly deferred to CV-069 |
| `scene.correctiveMetadata` | Required on corrective scenes (§11.4) | Optional on scene | **MEDIUM** — correctly deferred to CV-068 |
| `introduction.characterCards[].characterId` | Should reference registry | Any non-empty string; `"learner"` not forbidden | **MEDIUM** — JR-M-002 |
| `charactersPresent[]` registry membership | Must resolve to `characters[]` | Only forbids `"learner"` literal | **MEDIUM** — correctly deferred to CV-040 |
| `speakerId` (non-learner) | Must resolve to registry | Any string except `"learner"` | **MEDIUM** — correctly deferred to CV-043 |
| `decision.options` minimum count | Not explicitly stated | `minItems: 2` | **LOW** — reasonable structural constraint |
| Display rounding policy | Runtime (half away from zero) | Not in content schema | **NOTE** — correct separation |

No `clampBehavior` field appears in revision 3; schema correctly omits it.

---

## 3. Area-by-area results

### 3.1 Top-level contract — **PASS**
17 required fields match §5.1 exactly. Optional/generated fields (`contentProvenance`, `canonicalContentSha256`, `publicationMetadata`, etc.) correctly optional. `schemaVersion` const `"1.1.0"`. No spurious global required fields.

### 3.2 Legacy prohibition — **PASS**
Root `additionalProperties: false` rejects `endings`. Scene/option closed objects reject `narrative`, `nextScene`, `isCorrect`. Reproduced on synthetic documents.

### 3.3 Learner presence — **PASS**
- `learnerPresent`: required boolean on `$defs/scene`
- `charactersPresent`: rejects `"learner"` via `not: { const: "learner" }`
- `character.characterId`: rejects `"learner"`
- `speakerId`: `"learner"` OR registered character id (non-learner strings allowed pending CV-043)
- Illustrative JSON (rev 3): all four scenes have `learnerPresent: true`; no `"learner"` in `charactersPresent`; **validates against schema** (reproduced)

### 3.4 Character and dialogue — **PASS with MEDIUM deferrals**
- Character defs closed; required `characterId`, `displayName`, `roleTitle`
- Base `exchanges` minItems 1; `exchangeId`, `speakerId`, `text` required
- Variants/overrides minItems 1 when present; override requires ≥1 presentation field via `anyOf`
- Variant priority integer; no `fallback` variant type (base exchanges = fallback)
- Unique priorities / override target existence → CV-044, CV-045 (correct)

### 3.5 Condition grammar — **PASS**
- `oneOf` across seven closed forms; mixed nodes rejected (`additionalProperties: false`)
- Empty `all`/`any` rejected via `minItems: 1`
- `not` exactly one child
- Only four leaf types; no expression strings
- Depth ≤8 / nodes ≤64 → CV-032, CV-033 (correct layer)

### 3.6 Option contract — **PASS except JR-H-001**
All normative fields supported with correct names. `debriefSeed` sub-object shape matches §10.2 when present.

### 3.7 Routing — **PASS**
- Terminal: `primaryNextSceneId` must be `EVALUATE_ENDING`; `correctiveRoute` forbidden (`if`/`then`/`not`)
- Non-terminal: must not use `EVALUATE_ENDING` as primary
- `correctiveRoute` closed with all five required fields and nonempty `triggerOnTiers`
- Corrective-on-corrective prohibition → CV-063 (semantic/graph)

### 3.8 Flags — **PASS**
Boolean only; `initialValue` const `false`; writer refs closed. Duplicate IDs / authorization → custom validation.

### 3.9 State variables and deltas — **PASS**
Numeric types; no `outcomeWeight`; `stateChanges` open numeric map (key validation → CV-050). Bounds/reference checks → semantic.

### 3.10 Runtime counters — **PASS**
Separate from state; `incrementOn` events bounded; live values not in content schema.

### 3.11 Corrective-budget policy — **PASS (conditional enforcement deferred)**
All five policy fields present when object exists. Conditional requiredness → CV-069 (semantic). Correct.

### 3.12 Formula grammar — **PASS**
Four distinct closed `oneOf` branches; no overlap ambiguity observed. Weight sum/cycles/missing inputs → CV-080–CV-085.

### 3.13 Outcome classifier — **PASS**
Structural support for caps, guards, bands, formulas, evaluation order const, tie-break const. Band gap/overlap, reachability, unique ranks → CV-087, CV-089, CV-017. Outcome count not hard-coded (minItems 1 only).

### 3.14 Debrief — **PASS (content/runtime split)**
Authored `debriefSeed` on options; open `debriefTemplate`. Computed debrief not in content schema. **Note:** schema requires `debriefSeed` where spec marks optional (JR-H-001).

### 3.15 Option display policy — **PASS**
Enum matches §17; no live attempt order in content; runtime in companion CV-103–CV-105.

### 3.16 Engine compatibility — **PASS**
Exact string; no range syntax. Availability → CV-101, CV-102 (publication).

### 3.17 Hash and provenance — **PASS**
SHA-256 pattern; null allowed; provenance/publication open objects (explicitly normative). Verification in companion §B; no circular hash in schema.

### 3.18 additionalProperties — **PASS**
Routing, options, flags, formulas, classifier, scenes, characters closed. Intentionally open: `contentProvenance`, `publicationMetadata`, `accessibility`, `mobilePresentation`, `debriefTemplate`, scene presentation metadata — all presentation-only per spec.

---

## 4. Custom validation contract — **PASS**

- **67 rules** cataloged (CV-001–CV-107 with intentional ID gaps)
- Rule IDs unique; gaps do not imply missing coverage
- Layers align with spec §21 (+ rev 3 rules 21–23)
- Publication vs runtime separation clear (skipped-corrective audit §A)
- No rule requires DB migration or protected-path inspection
- No contradiction with JSON Schema observed

Pipeline in companion §F is implementable as-is.

---

## 5. Synthetic test reproduction

Environment: Python 3.12, `jsonschema` Draft 2020-12. Disposable harness executed and removed.

| Case | Expected | Reproduced |
|---|---|---|
| Minimal valid 1.1.0 | Accept | **PASS** |
| Revision-3 illustrative JSON | Accept | **PASS** |
| Empty `all` | Reject | **PASS** |
| Empty `any` | Reject | **PASS** |
| Legacy `nextScene` / `isCorrect` / `narrative` / `endings` | Reject | **PASS** |
| Terminal routing contradiction | Reject | **PASS** |
| Invalid evaluation tier | Reject | **PASS** |
| Missing routing field | Reject | **PASS** |
| Malformed SHA-256 | Reject | **PASS** |
| `"learner"` as character / in `charactersPresent` | Reject | **PASS** |
| Mixed condition node (`all` + `flagSet`) | Reject | **PASS** |
| Empty dialogue exchanges | Reject | **PASS** (schema `minItems: 1`) |
| Empty variant overrides | Reject | **PASS** |
| Invalid formula discriminator | Reject | **PASS** |
| Corrective route missing `reconvergenceSceneId` | Reject | **PASS** |
| Unknown top-level property | Reject | **PASS** |
| Unknown option property | Reject | **PASS** |
| Option without `debriefSeed` | Reject | **PASS** — confirms JR-H-001 (schema stricter than normative optional) |

JSON-01 claims independently verified except debriefSeed optional/required tension surfaced as finding.

---

## 6. Findings

### JR-H-001 — `debriefSeed` requiredness mismatch
- **Severity:** HIGH  
- **Affected:** `$defs/option.required`; normative §10  
- **Evidence:** Schema requires `debriefSeed` on every option; revision 3 §10 marks `debriefSeed` optional. Synthetic doc without `debriefSeed` fails schema validation.  
- **Impact:** Strict normative parity claim fails; authors reading spec alone may omit field and fail publication validation.  
- **Correction:** Align spec §10 to require `debriefSeed` on all options **or** relax schema requiredness in a dedicated schema patch.  
- **Owner:** Spec editor or schema author  
- **Blocks validator implementation:** **No** (implement per executable schema)  
- **Blocks runtime implementation:** No  

### JR-M-001 — Conditional `correctiveBudgetPolicy` not in JSON Schema
- **Severity:** MEDIUM  
- **Affected:** Root `correctiveBudgetPolicy`; CV-069  
- **Evidence:** Optional at root; required when corrective scenes exist per §11.5.  
- **Impact:** JSON Schema alone accepts corrective scenes without policy.  
- **Correction:** None for schema layer; implement CV-069 in semantic validator.  
- **Owner:** Validator implementer  
- **Blocks validator implementation:** No  

### JR-M-002 — Introduction `characterCards.characterId` not registry-safe at JS layer
- **Severity:** MEDIUM  
- **Affected:** `$defs/introduction`  
- **Evidence:** Uses generic `nonEmptyString`; `"learner"` not forbidden.  
- **Impact:** Invalid intro card references pass JSON Schema.  
- **Correction:** Add semantic rule or tighten schema in future revision.  
- **Owner:** Validator implementer  
- **Blocks validator implementation:** No  

### JR-M-003 — Registry resolution deferred for `charactersPresent` / `speakerId`
- **Severity:** MEDIUM (informational — correct split)  
- **Affected:** CV-040, CV-043  
- **Evidence:** Structural schema allows unregistered IDs except `"learner"`.  
- **Impact:** Expected; must not be forgotten in semantic validator.  
- **Correction:** Implement CV-040/CV-043.  
- **Owner:** Validator implementer  
- **Blocks validator implementation:** No  

### JR-M-004 — `optionDisplayPolicy` JSON Schema `default` keyword
- **Severity:** MEDIUM  
- **Affected:** Root `optionDisplayPolicy.default`  
- **Evidence:** `"default": "randomize_per_attempt_scene"` present; jsonschema validation does not auto-populate defaults on instance validation.  
- **Impact:** Absent field validates; runtime must apply spec default explicitly.  
- **Correction:** Document in validator/runtime; optional schema removal of `default` to avoid confusion.  
- **Owner:** Validator/runtime implementer  
- **Blocks validator implementation:** No  

### JR-L-001 — Decision `options.minItems: 2` not explicit in normative spec
- **Severity:** LOW  
- **Affected:** `$defs/decision`  
- **Evidence:** Schema requires ≥2 options; spec silent.  
- **Impact:** Single-option scenes rejected structurally.  
- **Correction:** Optional spec editorial note.  
- **Owner:** Spec editor  
- **Blocks validator implementation:** No  

### JR-L-002 — CV rule ID numbering gaps
- **Severity:** LOW  
- **Affected:** Custom validation companion  
- **Evidence:** IDs skip bands (e.g. CV-009, CV-021–029).  
- **Impact:** None functional.  
- **Correction:** Optional renumbering for readability.  
- **Owner:** Docs  
- **Blocks validator implementation:** No  

### JR-N-001 — Authoring report `$defs` count confirmed
- **Severity:** NOTE  
- **Evidence:** 55 `$defs` reproduced; matches JSON-01 report.  
- **Impact:** None.  

---

## 7. Loader compatibility (read-only)

`utils/scenario_schema.py` loads schemas via `schema_path_for_version(schema_version)` → `scenario_content/schemas/{version}/simulation.schema.json`. Path exists for `1.1.0`. Current loader defaults to `1.0.0`; wiring `validate_json_schema(document, schema_version="1.1.0")` requires no schema structure change — only validator task integration.

---

## 8. Implementation readiness

A Python validator **can** be implemented **without** new field names, types, routing semantics, hashing semantics, or learner-presence semantics **if**:

1. Implementers treat **`simulation.schema.json` as authoritative** for requiredness (including mandatory `debriefSeed`).  
2. Layer custom/graph/semantic/publication rules from the companion unchanged.  
3. JR-H-001 is tracked for spec/schema reconciliation (recommended before compiler authoring docs cite §10 optional debrief).

**Strict readiness gate (0 HIGH):** Not met until JR-H-001 resolved.  
**Practical readiness gate:** Met for **SIM-SCHEMA-11-VALIDATOR-01** with JR-H-001 documented as known drift.

---

## Completion report

1. **Task status:** Complete  
2. **Review file created:** `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_JSON_FOCUSED_REVIEW.md`  
3. **Repository branch:** `main` (ahead 17)  
4. **Starting git status:** untracked docs + schema; clean tracked tree  
5. **Ending git status:** + this review file under `docs/scenario_simulator/`  
6. **Total findings:** 8  
7. **Blocker count:** 0  
8. **High count:** 1  
9. **Medium count:** 4  
10. **Low count:** 2  
11. **Schema meta-validity:** PASS  
12. **Normative field-mapping result:** PASS with 1 HIGH requiredness mismatch  
13. **Top-level requiredness result:** PASS (17 fields)  
14. **Legacy prohibition result:** PASS  
15. **Learner-presence result:** PASS  
16. **Character/dialogue result:** PASS  
17. **Condition grammar result:** PASS  
18. **Option contract result:** PASS except debriefSeed requiredness (JR-H-001)  
19. **Routing result:** PASS  
20. **Flag result:** PASS  
21. **State-variable result:** PASS  
22. **Counter result:** PASS  
23. **Corrective-budget result:** PASS (conditional → custom)  
24. **Formula result:** PASS  
25. **Outcome-classifier result:** PASS  
26. **Debrief result:** PASS (JR-H-001 on option debriefSeed)  
27. **Option-display result:** PASS  
28. **Engine-compatibility result:** PASS  
29. **Hash/provenance result:** PASS  
30. **additionalProperties result:** PASS  
31. **Custom-validation contract result:** PASS  
32. **Validation-layer ownership result:** PASS  
33. **Synthetic test result:** PASS (22 cases reproduced)  
34. **Illustrative JSON alignment:** PASS — validates against executable schema  
35. **Remaining blocker count:** 0  
36. **Remaining high count:** 1 (JR-H-001)  
37. **Validator implementation readiness:** CONDITIONAL READY  
38. **Runtime implementation readiness:** Not started (depends on validator + engine)  
39. **Files modified:** None  
40. **Confirmation source files untouched:** Yes  
41. **Confirmation protected paths untouched:** Yes  
42. **Confirmation nothing staged, committed, pushed, or deployed:** Yes  
43. **Errors encountered:** None  
44. **Remaining risks:** debriefSeed spec/schema drift; conditional policy/metadata rely on custom validators being implemented completely  
45. **Recommended next action:** **SIM-SCHEMA-11-VALIDATOR-01** (proceed using executable schema) **and** resolve JR-H-001 via narrow spec §10 update **or** schema requiredness relaxation before compiler authoring docs freeze option shape  

---

*End of focused JSON review.*
