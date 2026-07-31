# Scenario Schema 1.1.0 — Custom Validation Contract

**Task ID:** SIM-SCHEMA-11-JSON-01  
**Schema file:** `scenario_content/schemas/1.1.0/simulation.schema.json`  
**Normative source:** `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md` (revision 2)  
**Date:** 2026-07-30

This document defines validation rules that **JSON Schema cannot safely or completely enforce**. Implementers MUST layer these checks after JSON Schema validation and before publication.

## Validation layers

| Layer | Code | Responsibility |
|---|---|---|
| JSON Schema | **JS** | Structural types, requiredness, closed objects, enums, nonempty condition groups, terminal/routing shape, legacy-field prohibition |
| Custom structural | **CS** | Duplicate IDs, condition depth/node limits, cross-field shape rules not expressible in JS |
| Graph | **GR** | Reachability, cycles, path length, routing target resolution, corrective topology |
| Semantic | **SM** | Reference resolution, flag authorization, formula weights/cycles, variant priority, band coverage |
| Publication | **PB** | Canonical hash, engine compatibility, immutable version policy, outcome reachability |
| Runtime assertion | **RT** | Hot-path defensive checks during live attempts |

**Failure severity:** `BLOCKER` rejects publish/load; `HIGH` rejects publish; `MEDIUM` warn at publish / fail at runtime depending on note; `LOW` advisory.

**Error path:** JSON Pointer-style path from document root (e.g. `/scenes/2/decision/options/1/routing/correctiveRoute/correctiveSceneId`).

---

## A. Skipped-corrective audit (runtime — not content schema)

The **scenario content schema** defines routing semantics only. It does **not** include attempt snapshot or decision-result payload fields.

At runtime (application contract, no database migration):

| Rule | Description |
|---|---|
| A skipped corrective **MUST** appear in the server-generated **decision result** as a resolved routing event (`skippedCorrective` object with `attemptedCorrectiveSceneId`, `reconvergenceSceneId`, `reason: "budget_exhausted"`). |
| The attempt snapshot **MUST** retain **counters** and sufficient **resolved routing history** for resume and audit (for example `routingResolutions[]` and/or equivalent fields on decision persistence). |
| The client **MUST NOT** submit skip events, counters, routing, or display order. |
| Replay **MUST** recompute the same skip from pinned content + counters and **MAY** verify stored routing history. |
| Skip recording **MUST NOT** increment `correctiveScenesExperienced`. |

**Owner:** Engine/persistence (RT + application snapshot contract). **Not encoded in content JSON Schema.**

---

## B. Canonical hash verification (publication)

Publication and replay verifiers **MUST** execute exactly:

1. Let `D` be the runtime scenario document.
2. Deep-copy to `D'`.
3. **Remove** from `D'`: `canonicalContentSha256`, `contentProvenance`, `publicationMetadata`.
4. Canonicalize `D'` using the repository-supported stable JSON representation matching `utils/scenario_schema.compute_canonical_content_sha256` intent:
   - `json.dumps(..., sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
5. UTF-8 encode the string.
6. Compute SHA-256; lowercase hexadecimal digest.
7. Compare to stored `D.canonicalContentSha256`.
8. **Forbidden:** embedding the digest inside the hashed object (no circular hash).

Callers of the existing helper **MUST** pass `D'` with exclusions already applied, or use a 1.1.0-aware wrapper that strips excluded keys before calling the same normalization.

---

## C. Learner presence disposition

Per SIM-SCHEMA-11-JSON-01 disposition (extends revision-2 spec §8):

| Field | Rule |
|---|---|
| `charactersPresent` | Registered `characterId` values **only**. The literal `"learner"` is **forbidden**. |
| `learnerPresent` | Required boolean on every scene. Represents learner/advisee presence separately. |
| `speakerId` | May be a registered `characterId` **or** the literal `"learner"` for dialogue lines. |

**Normative divergence:** Revision-2 illustrative JSON lists `"learner"` inside `charactersPresent` and omits `learnerPresent`. That fixture **does not conform** to this executable schema until the normative spec example is updated in a future spec revision. **Do not weaken the schema** to accept `"learner"` in `charactersPresent`.

---

## D. Rule catalog

### D.1 Identity, versioning, and legacy prohibition

| ID | Description | Layer | Severity | Error path | Fail publish | Runtime recheck |
|---|---|---|---|---|---|---|
| CV-001 | `schemaVersion` MUST be `"1.1.0"` | JS | BLOCKER | `/schemaVersion` | Yes | Yes |
| CV-002 | Prohibit root `endings[]` | JS | BLOCKER | `/endings` | Yes | Yes |
| CV-003 | Prohibit option `nextScene` | JS | BLOCKER | `.../options/*/nextScene` | Yes | Yes |
| CV-004 | Prohibit option `isCorrect` | JS | BLOCKER | `.../options/*/isCorrect` | Yes | Yes |
| CV-005 | Prohibit scene `narrative` | JS | BLOCKER | `/scenes/*/narrative` | Yes | Yes |
| CV-006 | Prohibit `optionTierInCurrentDecision` anywhere | CS | BLOCKER | `/**/optionTierInCurrentDecision` | Yes | Yes |
| CV-007 | Duplicate `simulationId`+`version` within catalog scope | PB | HIGH | `/simulationId`, `/version` | Yes | No |
| CV-008 | Immutable published version MUST NOT be mutated in place | PB | BLOCKER | `/version` | Yes | No |

### D.2 Duplicate IDs (structural)

| ID | Description | Layer | Severity | Error path | Fail publish | Runtime recheck |
|---|---|---|---|---|---|---|
| CV-010 | Unique scene `id` | CS | BLOCKER | `/scenes/*/id` | Yes | Yes |
| CV-011 | Unique option `id` within scene | CS | BLOCKER | `/scenes/*/decision/options/*/id` | Yes | Yes |
| CV-012 | Unique `flagId` | CS | BLOCKER | `/flags/*/flagId` | Yes | Yes |
| CV-013 | Unique `stateVariables.key` | CS | BLOCKER | `/stateVariables/*/key` | Yes | Yes |
| CV-014 | Unique `runtimeCounters.counterId` | CS | BLOCKER | `/runtimeCounters/*/counterId` | Yes | Yes |
| CV-015 | Unique `characterId` | CS | BLOCKER | `/characters/*/characterId` | Yes | Yes |
| CV-016 | Unique `outcomeId` | CS | BLOCKER | `/outcomes/*/outcomeId` | Yes | Yes |
| CV-017 | Unique `classificationRank` across outcomes | SM | BLOCKER | `/outcomes/*/classificationRank` | Yes | No |
| CV-018 | Unique `exchangeId` within scene | CS | BLOCKER | `/scenes/*/dialogue/exchanges/*/exchangeId` | Yes | Yes |
| CV-019 | Unique `variantId` within scene | CS | BLOCKER | `/scenes/*/dialogue/variants/*/variantId` | Yes | Yes |
| CV-020 | Unique cap/guard ids within classifier | CS | BLOCKER | `/outcomeClassifier/*Caps/*/capId`, `.../strongGuards/*/guardId` | Yes | No |

### D.3 Condition grammar

| ID | Description | Layer | Severity | Error path | Fail publish | Runtime recheck |
|---|---|---|---|---|---|---|
| CV-030 | Empty `all`/`any` forbidden | JS | BLOCKER | `/**/when/all`, `/**/when/any` | Yes | Yes |
| CV-031 | `not` has exactly one child | JS | BLOCKER | `/**/when/not` | Yes | Yes |
| CV-032 | Max nesting depth ≤ 8 | CS | BLOCKER | condition path | Yes | Yes |
| CV-033 | Max condition nodes ≤ 64 per tree | CS | BLOCKER | condition path | Yes | Yes |
| CV-034 | `flagSet`/`flagNotSet` reference registered flags | SM | BLOCKER | leaf path | Yes | Yes |
| CV-035 | `stateCompare.variableId` references `stateVariables` | SM | BLOCKER | leaf path | Yes | Yes |
| CV-036 | `counterCompare.counterId` references `runtimeCounters` | SM | BLOCKER | leaf path | Yes | Yes |
| CV-037 | No executable code / arbitrary expression strings | JS+CS | BLOCKER | `/**` | Yes | Yes |

### D.4 Characters, dialogue, variants

| ID | Description | Layer | Severity | Error path | Fail publish | Runtime recheck |
|---|---|---|---|---|---|---|
| CV-040 | `charactersPresent` ids ⊆ character registry | SM | BLOCKER | `/scenes/*/charactersPresent/*` | Yes | No |
| CV-041 | `charactersPresent` MUST NOT contain `"learner"` | JS | BLOCKER | `/scenes/*/charactersPresent/*` | Yes | No |
| CV-042 | `learnerPresent` required on every scene | JS | BLOCKER | `/scenes/*/learnerPresent` | Yes | No |
| CV-043 | `speakerId` (non-learner) resolves to registry | SM | BLOCKER | `.../exchanges/*/speakerId` | Yes | No |
| CV-044 | Variant priorities unique within scene | SM | BLOCKER | `/scenes/*/dialogue/variants/*/priority` | Yes | No |
| CV-045 | Override `exchangeId` exists in base exchanges | SM | BLOCKER | `.../variants/*/overrides/*/exchangeId` | Yes | No |
| CV-046 | Overrides MUST NOT add/remove/reorder exchanges | SM | BLOCKER | variant path | Yes | No |
| CV-047 | Multi-flag combined variants required when co-occurring text differs | SM | HIGH | scene dialogue | Yes | No |
| CV-048 | Environmental flags applied before variant selection (runtime order) | RT | BLOCKER | N/A runtime | No | Yes |

### D.5 Options, flags, state

| ID | Description | Layer | Severity | Error path | Fail publish | Runtime recheck |
|---|---|---|---|---|---|---|
| CV-050 | `stateChanges` keys ⊆ `stateVariables` | SM | BLOCKER | `.../options/*/stateChanges/*` | Yes | Yes |
| CV-051 | `setFlags`/`clearFlags` reference registered flags | SM | BLOCKER | option path | Yes | Yes |
| CV-052 | Flag set/clear authorized by `allowedSetters`/`allowedClearers` | SM | HIGH | `/flags/*` | Yes | Yes |
| CV-053 | `initialState` keys ⊆ `stateVariables` | SM | BLOCKER | `/initialState/*` | Yes | Yes |
| CV-054 | `initialState` values within declared min/max | SM | BLOCKER | `/initialState/*` | Yes | Yes |
| CV-055 | Counters MUST NOT appear in `stateVariables` or `initialState` | SM | BLOCKER | cross-object | Yes | Yes |
| CV-056 | `debriefSeed.strongestOptionId` resolves in same scene | SM | HIGH | `.../debriefSeed/strongestOptionId` | Yes | No |
| CV-057 | Client MUST NOT submit tiers, deltas, flags (security) | RT | BLOCKER | RPC payload | No | Yes |

### D.6 Routing and corrective budget

| ID | Description | Layer | Severity | Error path | Fail publish | Runtime recheck |
|---|---|---|---|---|---|---|
| CV-060 | `primaryNextSceneId` targets resolve (scene or `EVALUATE_ENDING`) | GR | BLOCKER | routing path | Yes | Yes |
| CV-061 | Terminal ⇒ `primaryNextSceneId == EVALUATE_ENDING` and no `correctiveRoute` | JS+SM | BLOCKER | routing path | Yes | Yes |
| CV-062 | Non-terminal MUST NOT use `EVALUATE_ENDING` as primary | JS+SM | BLOCKER | routing path | Yes | Yes |
| CV-063 | `correctiveRoute` forbidden on corrective-scene options | SM | BLOCKER | option routing | Yes | Yes |
| CV-064 | Corrective scene options share same reconvergence; no re-branch | GR | BLOCKER | corrective scenes | Yes | Yes |
| CV-065 | No corrective→corrective routing | GR | BLOCKER | graph edge | Yes | Yes |
| CV-066 | `whenCorrectiveSkippedNextSceneId == reconvergenceSceneId == primaryNextSceneId` when correctiveRoute present | SM | BLOCKER | correctiveRoute | Yes | Yes |
| CV-067 | `correctiveSceneId` references `sceneType: "corrective"` | SM | BLOCKER | correctiveRoute | Yes | Yes |
| CV-068 | `correctiveMetadata` required on corrective scenes; `mayRebranch == false` | SM | BLOCKER | `/scenes/*` | Yes | Yes |
| CV-069 | `correctiveBudgetPolicy` required when any corrective scene exists | SM | BLOCKER | `/correctiveBudgetPolicy` | Yes | No |
| CV-070 | `experiencedCounterId` references declared counter | SM | BLOCKER | policy path | Yes | No |
| CV-071 | `maxExperiencedCorrectiveScenes` ≤ `maxAvailableCorrectiveScenes` | SM | HIGH | policy path | Yes | No |
| CV-072 | Union graph acyclic (primary + corrective + skip edges) | GR | BLOCKER | graph | Yes | Yes |
| CV-073 | All non-corrective scenes reachable from `startScene` | GR | BLOCKER | graph | Yes | No |
| CV-074 | Max path length ≤ `maxScoredDecisions` | GR | BLOCKER | graph | Yes | No |
| CV-075 | Min path length ≥ `minScoredDecisions` | GR | BLOCKER | graph | Yes | No |
| CV-076 | Corrective counter increment order (§11.3) | RT | BLOCKER | N/A | No | Yes |

### D.7 Formulas and outcome classifier

| ID | Description | Layer | Severity | Error path | Fail publish | Runtime recheck |
|---|---|---|---|---|---|---|
| CV-080 | Formula variable references exist | SM | BLOCKER | classifier formulas | Yes | Yes |
| CV-081 | `linear_blend` weights sum to 1.0 ± 1e-9 | SM | BLOCKER | compositeFormula | Yes | Yes |
| CV-082 | Formula dependency graph acyclic | SM | BLOCKER | classifier | Yes | Yes |
| CV-083 | Missing formula inputs reject at runtime | RT | BLOCKER | runtime | No | Yes |
| CV-084 | Division by zero / zero scoredDecisionCount at terminal | RT | BLOCKER | runtime | No | Yes |
| CV-085 | Classification uses unrounded composite; display round after | RT | BLOCKER | runtime | No | Yes |
| CV-086 | Cap/guard/band outcome references resolve | SM | BLOCKER | classifier | Yes | No |
| CV-087 | Score bands cover (−∞,+∞) without gaps/overlaps | SM | BLOCKER | scoreBands | Yes | No |
| CV-088 | Seven-step evaluation order (`v1_seven_step`) | RT | BLOCKER | runtime | No | Yes |
| CV-089 | Every outcome reachable (fail-closed publication) | PB | BLOCKER | `/outcomes/*` | Yes | No |
| CV-090 | `isDetour` if present MUST match `(sceneType == "corrective")` | SM | HIGH | `/scenes/*/isDetour` | Yes | No |

### D.8 Domains, engine, display order, debrief

| ID | Description | Layer | Severity | Error path | Fail publish | Runtime recheck |
|---|---|---|---|---|---|---|
| CV-100 | If any scene has `domainId`, `domains[]` required and id resolves | SM | HIGH | `/domains`, scene | Yes | No |
| CV-101 | `requiredEngineVersion` exact match with running engine | PB | BLOCKER | `/requiredEngineVersion` | Yes | Yes |
| CV-102 | Unsupported engine contract rejected at publish | PB | BLOCKER | `/requiredEngineVersion` | Yes | Yes |
| CV-103 | Randomized display: Fisher–Yates + SHA256 seed per §17 | RT | BLOCKER | runtime | No | Yes |
| CV-104 | Server stores `optionDisplayOrder`; client submits `optionId` only | RT | BLOCKER | snapshot/RPC | No | Yes |
| CV-105 | Replay regenerates and verifies display order | RT | HIGH | runtime | No | Yes |
| CV-106 | Debrief computed fields derived from pinned content + history | RT | MEDIUM | debrief | No | Yes |
| CV-107 | `introduction` present; `startScene` is scored core scene | SM | HIGH | `/startScene` | Yes | Yes |

---

## E. JSON Schema limitations (explicit)

JSON Schema **does not** enforce:

- Graph reachability, cycles, or path-length bounds
- Outcome reachability
- Canonical hash correctness
- Engine availability at publish time
- Formula weight sum tolerance (`1e-9`)
- Duplicate IDs across arrays
- Flag setter/clearer authorization
- Variant priority uniqueness
- Reference resolution (flags, variables, counters, scenes, outcomes)
- Conditional requirement of `correctiveBudgetPolicy` when corrective scenes exist
- Conditional requirement of `correctiveMetadata` on corrective scenes
- `isDetour` consistency with `sceneType`
- `domains[]` conditional requirement
- Live attempt snapshot fields (counters, routing history, display order)

---

## F. Recommended validator pipeline

```
1. Parse JSON
2. Draft202012Validator (simulation.schema.json)
3. Custom structural validator (CV-010..020, CV-032..033)
4. Semantic reference validator (CV-034..056, CV-066..071, CV-080..087, CV-100, CV-107)
5. Graph validator (CV-060..065, CV-072..075)
6. Publication validator (CV-007..008, CV-089, CV-101..102, canonical hash §B)
7. Runtime assertions during live attempts (CV-048, CV-057, CV-076, CV-083..085, CV-088, CV-103..106)
```

**Custom-validation rule count:** **67** cataloged rules (CV-001..CV-107, excluding gaps by design).

---

## G. Randomized option display (content vs runtime)

**Content schema encodes:**

- `optionDisplayPolicy` enum: `randomize_per_attempt_scene` | `authored_order`

**Runtime (not in content document):**

Normative algorithm: **`SCENARIO_SCHEMA_1_1_0_SPEC.md` §17** (revision 6).

Summary for validator cross-reference (CV-103):

- **`authored_order`:** return `scene.decision.options[].id` in authored array order; no seed; no shuffle; attempt identity does not affect order.
- **`randomize_per_attempt_scene`:** seed material = UTF-8(`attemptId + "\n" + simulationId + "\n" + version + "\n" + canonicalContentSha256 + "\n" + sceneId`); digest stream = `SHA256(seedMaterialBytes || uint32be(counter))` for `counter = 0, 1, 2, …`; Fisher–Yates backward shuffle with rejection-sampled uniform byte draws (§17.6–§17.7).
- **Unsupported policies:** fail closed.
- Server returns resolved option id order; snapshot stores per-scene map; submissions use `optionId` only.
- Conformance golden vector: §17.9 → `["opt-b", "opt-c", "opt-a"]`.

---

## H. Debrief boundary

**Authored in content:** `debriefSeed` on options, `debriefTemplate`, outcome narratives/recommendations.

**Computed / replay-derived (not content fields):** applied tiers, cap/guard trace, corrective/skip history, selected variant audit, classification trace, rounded display score.

---

*End of custom validation contract.*
