# CertBound Scenario Simulator Content Schema — Normative Specification v1.1.0

**Task ID:** SIM-SCHEMA-11-SPEC-05 (illustrative corrective-budget fixture alignment after SIM-SCHEMA-11-VALIDATOR-01)  
**Status:** Draft normative specification (documentation only) — **revision 6**  
**Supersedes:** SIM-SCHEMA-11-SPEC-05 (revision 5) of this file  
**Adversarial input:** `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_ADVERSARIAL_REVIEW.md`  
**Predecessor review:** `docs/scenario_simulator/CB-SC-001_engineering_compatibility_review.md`  
**Repository:** `C:\Users\Abdel\Projects\salesforce-cert-mock-exam-latest`  
**Date:** 2026-07-31

---

## 1. Status and scope

This document is the **complete normative specification** for CertBound Scenario Simulator **content schema version `1.1.0`**. It defines the machine-checkable contract that compiled scenario JSON must satisfy before publication, and the runtime semantics that a conforming engine must implement.

**In scope:** field definitions, authority, validation, runtime semantics, bounded grammars, persistence/replay/security boundaries, and a complete internally valid illustrative vertical-slice JSON.

**Out of scope:** executable JSON Schema file, validators implementation, runtime/engine code, compiler, DB migrations, UI, Creative Studio asset transformation.

**Normative language:** MUST / MUST NOT / SHOULD / SHOULD NOT / MAY (RFC 2119).

**Correction notice:** Revision 2 closed every BLOCKER and HIGH finding from SIM-SCHEMA-11-REVIEW-01 (see `SCENARIO_SCHEMA_1_1_0_CORRECTION_REPORT.md`). Revision 3 aligned learner-presence representation with the executable JSON Schema (see `SCENARIO_SCHEMA_1_1_0_SPEC_03_ALIGNMENT_REPORT.md`). Revision 4 requires `debriefSeed` on every executable 1.1.0 option, closing JR-H-001 (see `SCENARIO_SCHEMA_1_1_0_SPEC_04_DEBRIEF_ALIGNMENT_REPORT.md`). Revision 5 aligns the §23 illustrative vertical-slice corrective-budget policy with its single authored corrective scene (see `SCENARIO_SCHEMA_1_1_0_SPEC_05_FIXTURE_ALIGNMENT_REPORT.md`). **Revision 6** freezes the exact deterministic option-display algorithm for both supported `optionDisplayPolicy` values, removing prior §17 first-block ambiguity (see `SCENARIO_ENGINE_V2_SPEC_17_ALIGNMENT_REPORT.md`). This revision **clarifies** behavior already implemented and tested by SCENARIO_ENGINE_V2; it does **not** change runtime semantics.

---

## 2. Normative terminology

| Term | Definition |
|---|---|
| **Scenario document** | One immutable JSON object representing one scenario at one content `version`. |
| **Schema version** | `schemaVersion` selecting validation/interpretation rules. |
| **Content version** | Immutable publish identity (`version`). |
| **Engine version** | Exact runtime capability identifier (e.g. `SCENARIO_ENGINE_V2`) pinned on attempts. |
| **Stable ID** | Authoritative key for routing, persistence, replay, scoring — never display position or text. |
| **Runtime-authoritative** | Computed/enforced server-side; MUST NOT be accepted from the client as truth. |
| **Presentation-only** | Display-only; MUST NOT affect scoring, routing, or replay correctness. |
| **Generated** | Produced by tooling (compiler, hasher, publisher). |
| **Core / corrective scene** | Main-spine scored scene vs remediation scene (exactly one decision; MUST NOT re-branch). |
| **Terminal sentinel** | Exact literal `EVALUATE_ENDING`. |
| **Decision history** | Ordered `(sequenceNumber, sceneId, optionId)` triples — sole trusted learner-choice replay input. |
| **Counters** | Separate integer runtime counters — **not** numeric state variables. |
| **Skipped-corrective event** | Server-generated audit record that a corrective route was eligible by option ownership but skipped because budget was exhausted. |
| **Registered character** | An entry in the scenario-level `characters[]` registry identified by `characterId`. The learner/advisee is **not** a registered character. |
| **Learner presence** | Whether the learner participates in a scene, expressed by the boolean `learnerPresent` — separate from the character registry. |

---

## 3. Design principles

1. Additive by platform version: `1.1.0` does not change `1.0.0` semantics.  
2. No silent reinterpretation of `1.0.0` fields.  
3. Stable IDs are authoritative.  
4. Deterministic runtime given pinned `(schemaVersion, content version, canonical hash, engine version)` + decision history.  
5. Declarative bounded logic only — no scripts.  
6. Validation rejects ambiguity.  
7. Hidden evaluation stays server-side.  
8. Generic schema; CB-SC-001 is a consumer, not a naming template.  
9. No speculative features.  
10. No database migration; application JSON snapshot contract **does** change and is explicitly acknowledged.

---

## 4. Compatibility with 1.0.0

### 4.1 Coexistence model

| Document `schemaVersion` | Validator | Engine |
|---|---|---|
| `"1.0.0"` | `schemas/1.0.0/simulation.schema.json` + existing custom validators | `SCENARIO_ENGINE_V1` only |
| `"1.1.0"` | Future `1.1.0` schema + validators herein | Exact `requiredEngineVersion` (normally `SCENARIO_ENGINE_V2`) |
| Other | **REJECT** | **REFUSE** |

### 4.2 Unchanged 1.0.0 semantics

When a document declares `schemaVersion: "1.0.0"`, these fields retain **exactly** their 1.0.0 meaning and authority:

- `nextScene` — authoritative routing  
- `isCorrect` — authoritative binary correctness  
- `narrative` — authoritative scene prose  
- `endings[]` — authoritative terminal classification (Min/Max/Equals)  

No silent upgrade to 1.1.0 semantics is permitted.

### 4.3 Legacy / new coexistence for schema 1.1.0 (closes ADV-B-005)

**Rule:** A `schemaVersion: "1.1.0"` document MUST use only 1.1.0 authoritative forms. Behavioral legacy fields are **prohibited**. Compiler output MUST emit only the 1.1.0 authoritative form. Compiler output MUST include a `debriefSeed` object on **every** executable option (§10). Runtime MUST NOT implement dual-read precedence.

| Pair | Schema 1.0.0 | Schema 1.1.0 |
|---|---|---|
| `nextScene` vs `routing` | `nextScene` authoritative | **`routing` required.** `nextScene` **FORBIDDEN**. Presence of `nextScene` → **reject**. |
| `isCorrect` vs `evaluationTier` | `isCorrect` authoritative | **`evaluationTier` required.** `isCorrect` **FORBIDDEN**. |
| `narrative` vs `dialogue` | `narrative` authoritative | **`dialogue` required.** `narrative` **FORBIDDEN**. |
| `endings[]` vs `outcomeClassifier`/`outcomes[]` | `endings[]` authoritative | **`outcomeClassifier` + `outcomes[]` required.** `endings[]` **FORBIDDEN**. |

**Narrow retention:** `isDetour` MAY appear on 1.1.0 scenes as **presentation/analytics only**. If present with `sceneType`, it MUST equal `(sceneType == "corrective")` or the document is rejected. It MUST NOT affect routing or scoring.

`domains[]` MAY appear on 1.1.0 for progress UI. If any scene has `domainId`, that id MUST be declared in `domains[]`.

### 4.4 Catalog and publication

- Catalog version rows MUST declare `schemaVersion`.  
- Publish validates against declared schema version **before** hashing.  
- Publication MUST reject any `schemaVersion: "1.1.0"` document whose executable option omits `debriefSeed` (§10).  
- Published `1.0.0` content MUST NOT be silently upgraded; schema change requires a new content `version`.

### 4.5 Historical replay

- Attempts pin `scenario_version_id`, `scenario_content_sha256`, and exact `engine_version` (existing V68).  
- Replay MUST use the pinned content + engine; formula/engine changes require a new engine version string.

---

## 5. Top-level scenario contract

### 5.1 Field-level table

| Field | Type | Required (1.1.0) | Authority | Purpose |
|---|---|---|---|---|
| `simulationId` | string | Yes | Authored | Stable scenario identity |
| `version` | string | Yes | Authored | Immutable content version |
| `schemaVersion` | string | Yes | Authored | MUST be `"1.1.0"` |
| `requiredEngineVersion` | string | Yes | Authored | Exact engine id (e.g. `"SCENARIO_ENGINE_V2"`) |
| `certificationExamName` | string | Yes | Authored | Certification lookup identity |
| `examCode` | string | Yes | Authored | Display exam code |
| `title` | string | Yes | Authored | Title |
| `description` | string | No | Authored | Summary |
| `estimatedMinutes` | integer | No | Authored | Duration hint |
| `locale` | string | No | Authored | BCP 47 |
| `learnerRole` | object | Yes | Authored | Non-scored role (§6) |
| `introduction` | object | Yes | Authored | Non-scored intro (§6) |
| `contentProvenance` | object | No | Generated/authored | **Excluded from canonical hash** (§18) |
| `canonicalContentSha256` | string\|null | No | Generated | **Excluded from its own hash input** (§18) |
| `publicationMetadata` | object | No | Generated | **Excluded from canonical hash**; prefer DB/sidecar |
| `accessibility` | object | No | Authored | Presentation-only |
| `mobilePresentation` | object | No | Authored | Presentation-only |
| `characters` | array | Yes | Authored | Character registry (§7); MAY be empty only if no speakers (not for CB-SC-001) |
| `flags` | array | Yes | Authored | Flag registry (§12); MAY be `[]` |
| `stateVariables` | array | Yes | Authored | Numeric variables (§13) |
| `initialState` | object | Yes | Authored | Starting numeric state |
| `runtimeCounters` | array | Yes | Authored | Counter declarations (§13.3) |
| `correctiveBudgetPolicy` | object | Yes when any corrective scene exists | Authored | Budget limits (§11) |
| `stages` | array | No | Authored | Progress labels |
| `domains` | array | Conditional | Authored | Required if any `domainId` used |
| `scenes` | array | Yes | Authored | All scenes |
| `startScene` | string | Yes | Authored | First scored scene after intro gate |
| `outcomeClassifier` | object | Yes | Authored | Terminal classifier (§14) |
| `outcomes` | array | Yes | Authored | Outcome definitions (§15) |
| `optionDisplayPolicy` | string | No | Authored | Default `"randomize_per_attempt_scene"` |
| `debriefTemplate` | object | No | Authored | Debrief section config |

**Removed vs SPEC-01:** `engineCompatibility.supportedEngineVersions` (closes ADV-H-001). Use scalar `requiredEngineVersion` only.

### 5.2 Engine compatibility (closes ADV-H-001)

```json
"requiredEngineVersion": "SCENARIO_ENGINE_V2"
```

- Loaders and publishers MUST require **exact** string equality with the running engine’s `ENGINE_VERSION`.  
- Ranges and compatibility sets are **not** supported by the current repository loader/persistence model and MUST NOT be authored.  
- New attempts pin this exact string; replay rejects mismatches.

---

## 6. Introduction contract

Non-scored. No state, flags, counters, routing, or sequence numbers. Learner MUST pass Start Scenario gate before `startScene`.

| Field | Required | Purpose |
|---|---|---|
| `companyIntroduction` | Yes | Company/world markdown |
| `projectBriefing` | Yes | `{ title, summary, customerName?, timelineContext? }` |
| `characterCards` | No | `{ characterId, introText, displayOrder? }[]` |
| `artifactPreviews` | No | Display-only |
| `startGate` | Yes | `{ headline, body, confirmButtonLabel, cancelBehavior? }` — `cancelBehavior` enum: `"return_to_catalog"` only in 1.1.0 |
| `knownRisks` / `successCriteria` / `authorityBoundaries` / `responsibilities` | No | string arrays |

`learnerRole`: `{ title, summary, reportingLine?, mandate? }` — all non-scored.

---

## 7. Character and visual contract

| Field | Required | Purpose |
|---|---|---|
| `characterId` | Yes | Stable id |
| `displayName` / `roleTitle` | Yes | Display |
| `affiliation` | No | Org |
| `portraitAssetRef` / `defaultExpressionRef` | No | Asset refs — **no binary data** |
| `accessibilityDescription` | No | a11y |
| `isRoleOnly` | No | No direct dialogue |

**Character registry rules:**

- Every `characterId` MUST be unique within `characters[]`.
- `characterId` MUST NOT be the literal `"learner"`. The learner/advisee is represented separately (§8) and is **not** a canonical registry character.
- `charactersPresent` (§8) and all character-reference validation MUST resolve ids against this registry only.

Missing assets → text fallback; engine MUST NOT error; validation checks reference format only.

---

## 8. Scene contract

| Field | Required | Authority | Purpose |
|---|---|---|---|
| `id` | Yes | Authored | Stable scene id |
| `sceneType` | Yes | Authored | `"core"` \| `"corrective"` |
| `stageId` | No | Authored | Progress (presentation) |
| `authoredOrder` | No | Authored | Presentation-only |
| `title` | Yes | Authored | Title |
| `domainId` | No | Authored | Progress; if set, must exist in `domains` |
| `setting` | Yes | Authored | Markdown |
| `charactersPresent` | Yes | Authored | Registered `characterId` list only; MUST NOT contain `"learner"` (§8.1) |
| `learnerPresent` | Yes | Presentation | Boolean — whether the learner/advisee is present in the scene (§8.1) |
| `enteringStateDescription` | No | Authored | Markdown |
| `environmentalFlagsOnEntry` | No | Authored | Registered flags set on entry |
| `dialogue` | Yes | Authored | §9 — **narrative FORBIDDEN** |
| `decision` | Yes | Authored | Prompt + options |
| `visibleConsequence` | No | Authored | Post-decision prose |
| `artifactReferences` | No | Presentation | Artifact ids |
| `progressMetadata` / `accessibility` / `mobilePresentation` | No | Presentation | |
| `isDetour` | No | Presentation | Must match `sceneType` if present |
| `explanation` | No | Authored | Post-decision synthesis |
| `correctiveMetadata` | Yes if corrective | Authored | §11 |

**Ordering:** `scenes[]` order and `authoredOrder` do **not** affect play order. `dialogue.exchanges[]` order is sequential. `decision.options[]` order is authored canonical / default display when policy is `authored_order`. Variant priority order is runtime-authoritative for selection.

### 8.1 Learner presence (closes JSON-01 alignment)

Every executable scene (core and corrective) MUST declare both `charactersPresent` and `learnerPresent`.

| Field | Type | Required | Authority | Rules |
|---|---|---|---|---|
| `charactersPresent` | string[] | Yes | Authored | Each entry MUST be a registered `characterId` from `characters[]`. The literal `"learner"` MUST NOT appear. MAY be empty when no registered characters are physically present. |
| `learnerPresent` | boolean | Yes | Presentation | `true` when the learner/advisee participates in the scene; `false` when absent. Presentation/runtime metadata only. MUST NOT affect scoring, routing, flag evaluation, variant selection, or replay correctness by itself. MUST NOT be used as a character registry reference. |

**Dialogue distinction:** `dialogue.exchanges[].speakerId` MAY be `"learner"` to attribute learner speech or a learner response line. This does **not** make `"learner"` a canonical character and MUST NOT be listed in `charactersPresent` or `characters[]`.

Validators MUST reject `"learner"` in `charactersPresent`, in `characters[].characterId`, or anywhere `"learner"` is used as a registry character reference.

---

## 9. Dialogue and variant contract (closes ADV-H-006, ADV-H-007, ADV-H-008)

### 9.1 Exchanges

| Field | Required | Purpose |
|---|---|---|
| `exchangeId` | Yes | Stable within scene |
| `speakerId` | Yes | Registered `characterId` **or** the literal `"learner"` for learner speech (§8.1) |
| `text` | Yes | Markdown |
| `audienceId` / `communicationType` / `expressionRef` / `bodyLanguage` / `tone` / `visualFocus` / `accessibilityNotes` | No | Presentation |

`communicationType` MAY be free string in 1.1.0 (closed enum deferred).

When `speakerId` is a registered `characterId`, that id MUST exist in `characters[]`. When `speakerId` is `"learner"`, the exchange represents learner speech; `"learner"` MUST NOT appear in `charactersPresent` or the character registry.

### 9.2 Variants — deterministic model

**Structural fallback:** Base `dialogue.exchanges` is the default render when no conditional variant matches.

**Conditional variants** are the only variant objects. They MUST NOT use empty conditions. There is **no** `fallback: true` variant and **no** `"when": { "always": true }` in 1.1.0 — those patterns are rejected as redundant with the structural base.

```json
"variants": [
  {
    "variantId": "c03-verbal-handoff",
    "priority": 10,
    "when": { "flagSet": "flag-verbal-handoff-only" },
    "overrides": [
      { "exchangeId": "ex-002", "text": "…", "tone": "skeptical" }
    ]
  }
]
```

| Rule | Normative |
|---|---|
| Priority uniqueness | Non-fallback (all) variants MUST have **unique** `priority` integers. Lower number = higher precedence. |
| Selection | Evaluate variants in ascending `priority`. Select the **first** whose `when` is true. If none, use base exchanges. |
| Overrides | MAY only **replace fields** on an existing `exchangeId`. MUST NOT add, remove, or reorder exchanges. Unknown `exchangeId` → **reject** at validation. |
| Multi-flag scenes | First-match only. Compilers/authors MUST emit **explicit combined variants** when multiple flags can co-occur and combined text is required (closes ADV-H-007). |
| Environmental flags | On scene entry, apply `environmentalFlagsOnEntry` **before** variant selection (closes ADV-H-008). |
| Persistence | Variant selection is replay-derived. Server MAY include `selectedVariantId` (or `null`) in the scene response / attempt snapshot for audit — presentation metadata, not scoring input. |

### 9.3 Bounded condition grammar (closes ADV-B-003, ADV-B-006, ADV-H-009)

**Contexts:** `entryCondition` (dialogue variants), `budgetCondition` (corrective budget remaining), `terminalCondition` (caps/guards).  
**NOT permitted:** option-self tier inspection in this grammar.

#### Composition

```
{ "all": [ Condition, ... ] }   // minItems: 1
{ "any": [ Condition, ... ] }   // minItems: 1
{ "not": Condition }
```

**FORBIDDEN:** `"all": []`, `"any": []` (closes ADV-B-003).

#### Leaves

| Form | Meaning | Allowed contexts |
|---|---|---|
| `{ "flagSet": "<flagId>" }` | Flag active | all |
| `{ "flagNotSet": "<flagId>" }` | Flag inactive | all |
| `{ "stateCompare": { "variableId", "op", "value" } }` | Numeric compare | all |
| `{ "counterCompare": { "counterId", "op", "value" } }` | Counter compare | all |

`op` ∈ `gte|lte|gt|lt|eq`. `value` MUST be a JSON number. References to undeclared variables/flags/counters → **reject** at validation; runtime MUST NOT default.

#### Bounds

| Limit | Value |
|---|---|
| Max nesting depth | 8 |
| Max condition nodes per tree | 64 |
| Min children in `all`/`any` | 1 |

**Removed:** `optionTierInCurrentDecision` (closes ADV-B-006).

Evaluation order within `all`: left-to-right short-circuit on false. Within `any`: left-to-right short-circuit on true.

---

## 10. Option contract

Every executable learner option in a `schemaVersion: "1.1.0"` document MUST include a `debriefSeed` object. Omission is invalid for publication and MUST be rejected by JSON Schema validation and publication validators. This requiredness applies only to 1.1.0; `schemaVersion: "1.0.0"` options do not define `debriefSeed` and retain unchanged 1.0.0 semantics.

| Field | Required | Purpose |
|---|---|---|
| `id` | Yes | Stable option id (unique within scene) |
| `title` | No | Short card title |
| `text` | Yes | Learner response text |
| `evaluationTier` | Yes | `optimal\|acceptable\|suboptimal\|high-risk` |
| `stateChanges` | No | Deltas keyed by state variable `key` |
| `setFlags` / `clearFlags` | No | Registered flag ids |
| `feedback` | Yes | Immediate consequence |
| `reactionDialogue` | No | Structured reaction |
| `visibleConsequence` | No | Extra prose |
| `routing` | Yes | §11 — **`nextScene` FORBIDDEN** |
| `debriefSeed` | **Yes** | Authored debrief prose for final debrief (§10.2, §16) |
| `competencyTags` | No | Tags |

**FORBIDDEN on 1.1.0:** `isCorrect`, `nextScene`.

### 10.1 Evaluation tiers

Default points: optimal=4, acceptable=3, suboptimal=1, high-risk=0. Overridable only via `outcomeClassifier.tierPoints`. Tier is server-resolved from content by `optionId`; clients MUST NOT submit tier.

### 10.2 Debrief seed (required on every option)

Each executable option MUST contain exactly one `debriefSeed` object matching the executable schema `$defs/debriefSeed` shape.

**Product rationale:** The approved product contract requires a detailed final debrief for every decision. An option without authored debrief seeds cannot reliably explain the learner’s selected action, why a stronger option was stronger, why alternatives were weaker or riskier, immediate and later consequences, competency impact, state/flag impact, or cap/guard relevance.

| Field | Required | Purpose |
|---|---|---|
| `strongestOptionId` | Yes | Strongest-option explanation anchor; MUST be an `id` in the same scene’s options |
| `whyStronger` | Yes | Markdown — why the strongest option is stronger |
| `immediateConsequence` | Yes | Markdown — immediate consequence of selecting this option |
| `whyWeaker` | No | Markdown — why this option is weaker than the strongest |
| `laterConsequence` | No | Markdown — later consequence seed |
| `competencyImpact` | No | Markdown — competency impact seed |
| `stateImpactSummary` | No | Markdown — state and flag impact seed |
| `capGuardEffect` | No | Markdown — cap or guard explanation seed |

If exactly one `optimal` option exists, `strongestOptionId` SHOULD equal it (publication warning if not).

**Authority:** Authored seeds are mandatory content. Runtime debrief output MAY add computed information (tiers, applied caps/guards, path replay, rounded scores) but MUST NOT substitute for missing authored seeds.

---

## 11. Routing and corrective-budget contract (closes ADV-B-001, ADV-B-006)

### 11.1 Routing object

```json
"routing": {
  "terminal": false,
  "primaryNextSceneId": "SC001-C03",
  "correctiveRoute": {
    "triggerOnTiers": ["suboptimal", "high-risk"],
    "budgetCondition": {
      "counterCompare": {
        "counterId": "correctiveScenesExperienced",
        "op": "lt",
        "value": 3
      }
    },
    "correctiveSceneId": "SC001-R2A",
    "whenCorrectiveSkippedNextSceneId": "SC001-C03",
    "reconvergenceSceneId": "SC001-C03"
  }
}
```

| Field | Rules |
|---|---|
| `terminal` | Required bool. If `true`: `primaryNextSceneId` MUST be `EVALUATE_ENDING`; `correctiveRoute` MUST be absent. |
| `primaryNextSceneId` | Required. Scene id or `EVALUATE_ENDING`. |
| `correctiveRoute` | Optional. **FORBIDDEN** on options belonging to `sceneType: "corrective"` scenes. |
| `triggerOnTiers` | Required if correctiveRoute present. Non-empty subset of the four tiers. Option triggers corrective consideration **iff** its own `evaluationTier` is in this list — encoded on the option, not via generic conditions. |
| `budgetCondition` | Required if correctiveRoute present. Condition grammar (§9.3). Typically `correctiveScenesExperienced < max`. |
| `correctiveSceneId` | Required; MUST be `sceneType: "corrective"`. |
| `whenCorrectiveSkippedNextSceneId` | Required; MUST equal `reconvergenceSceneId` and MUST equal `primaryNextSceneId`. |
| `reconvergenceSceneId` | Required; fixed post-corrective core scene. |

### 11.2 Normative routing resolution (single authoritative algorithm)

```
function resolveRouting(option, counters, flags, state):
  if option.routing.terminal:
    return { next: EVALUATE_ENDING, enteredCorrective: false, skippedCorrective: null }

  cr = option.routing.correctiveRoute
  if cr is absent:
    return { next: option.routing.primaryNextSceneId, enteredCorrective: false, skippedCorrective: null }

  tierMatches = option.evaluationTier in cr.triggerOnTiers
  if not tierMatches:
    return { next: option.routing.primaryNextSceneId, enteredCorrective: false, skippedCorrective: null }

  budgetOk = evaluate(cr.budgetCondition, { counters, flags, state })
  if budgetOk:
    return {
      next: cr.correctiveSceneId,
      enteredCorrective: true,
      skippedCorrective: null
    }
  else:
    return {
      next: cr.whenCorrectiveSkippedNextSceneId,
      enteredCorrective: false,
      skippedCorrective: {
        attemptedCorrectiveSceneId: cr.correctiveSceneId,
        reconvergenceSceneId: cr.reconvergenceSceneId,
        reason: "budget_exhausted"
      }
    }
```

**Skipped-corrective audit:** When `skippedCorrective` is non-null, the server MUST record it on the decision result and/or attempt snapshot (`routingResolution.skippedCorrective`). It MUST NOT increment `correctiveScenesExperienced`. Replay MUST recompute the same skip from content + counters and MAY verify against the stored event.

### 11.3 Corrective counter semantics (closes ADV-B-001) — single normative order

Per decision application, **exactly** this order:

1. Validate option belongs to current scene; reject stale sequence/scene.  
2. Apply `stateChanges`; clamp each numeric variable.  
3. Apply `clearFlags` then `setFlags`.  
4. Increment **decision-tier counters** only (`highRiskDecisionCount`, `optimalDecisionCount`, etc.) for this decision’s tier.  
5. Resolve routing using **pre-entry** `correctiveScenesExperienced` (and other counters/state/flags as of step 4).  
6. If `enteredCorrective`:
   - increment `correctiveScenesExperienced` by 1 **exactly once** when entry is **committed** with the decision write;
   - do not increment on duplicate idempotent replay of the same decision.  
7. If `skippedCorrective`: record skip event; do **not** increment.  
8. If next is a scene (not terminal): apply that scene’s `environmentalFlagsOnEntry`; select dialogue variant.  
9. Persist decision + updated snapshot; on terminal, classify outcome.

**Duplicate submissions:** Idempotent same fingerprint → return prior result; no second increment.  
**Interrupted writes:** No commit → no increment; retry uses same prepared request.  
**Stale sequence:** Reject; no mutation.  
**Exact budget boundary:** When `correctiveScenesExperienced == max - 1`, entry allowed and increments to max. When `== max`, budgetCondition fails → skip, no increment.  
**Corrective completion:** Completing a corrective decision does not increment again (increment already happened on entry).  
**No corrective route / tier not in triggerOnTiers:** primary route; no skip event.

**All other increment narratives in prior drafts are void.**

### 11.4 Corrective scene metadata

```json
"correctiveMetadata": {
  "triggerSceneId": "SC001-C02",
  "reconvergenceSceneId": "SC001-C03",
  "mayRebranch": false
}
```

`mayRebranch` MUST be `false`. All options in a corrective scene MUST route to the same reconvergence scene with no `correctiveRoute`.

### 11.5 Budget policy

**Full CB-SC-001 production contract** (five authored corrective scenes; up to three experienced per attempt):

```json
"correctiveBudgetPolicy": {
  "maxAvailableCorrectiveScenes": 5,
  "maxExperiencedCorrectiveScenes": 3,
  "maxScoredDecisions": 15,
  "minScoredDecisions": 12,
  "experiencedCounterId": "correctiveScenesExperienced"
}
```

**Illustrative vertical-slice fixture** (§23): authors exactly one corrective scene (`SC001-R2A`); therefore `maxAvailableCorrectiveScenes` and `maxExperiencedCorrectiveScenes` are both **1**. This smaller fixture does not change the full production limits above.

### 11.6 Graph validation

Reject: missing targets; corrective→corrective; `mayRebranch != false`; reconvergence ≠ skip target ≠ primary when correctiveRoute present; cycles under the union graph of **all** primary, corrective, and skip edges; unreachable scenes from `startScene`; max path length > `maxScoredDecisions`; min path length < `minScoredDecisions`.

Legal convergence (many edges into one scene) is **not** a cycle.

---

## 12. Flag registry (closes ADV-H / consumers)

### 12.1 Metadata classes

| Class | Fields | Role |
|---|---|---|
| **Runtime-authoritative** | `flagId`, `valueType` (`"boolean"` only), `initialValue` (MUST be `false`) | Engine |
| **Validation-authoritative** | `allowedSetters`, `allowedClearers`, `sticky` | Publish validators |
| **Documentation/advisory** | `description`, `consumers`, `debriefRelevant` | Docs/compiler hints only — **never runtime-authoritative** |

Validators MUST recompute consumers by scanning variants/caps/options and MAY warn if advisory `consumers` mismatches. Runtime MUST NOT depend on `consumers`.

### 12.2 Runtime

Active flags = set of true flag ids. Per decision: clear then set. Replay-derivable from decision history + content (including environmental entry flags reconstructed during replay).

---

## 13. State variables and counters (closes ADV-H-002, ADV-H-005)

### 13.1 State variables

| Field | Required | Notes |
|---|---|---|
| `key` | Yes | Id |
| `displayName` / `description` | No | |
| `polarity` | No | `higher_is_better` \| `higher_is_worse` — used by classifier formulas when referenced |
| `minimum` / `maximum` | No | Inclusive clamps |
| `learnerVisibleDuringRun` | No | Default false |
| `debriefVisible` | No | Default true |

**REMOVED:** `outcomeWeight` on variables (closes ADV-H-005). Weights live only in classifier formulas.

**Numeric type:** JSON numbers. Engine interprets state as IEEE-754 binary64 (`float`). Authored deltas MAY be integers; stored/computed as float64. Equality comparisons in conditions use exact float64 equality for `eq`.

### 13.2 Counters — separate storage (closes ADV-B-001 / ADV-H-002)

```json
"runtimeCounters": [
  {
    "counterId": "correctiveScenesExperienced",
    "initialValue": 0,
    "minimum": 0,
    "maximum": 3,
    "incrementOn": [{ "event": "corrective_scene_entered" }]
  },
  {
    "counterId": "highRiskDecisionCount",
    "initialValue": 0,
    "incrementOn": [{ "event": "decision_applied", "whenTier": "high-risk" }]
  },
  {
    "counterId": "optimalDecisionCount",
    "initialValue": 0,
    "incrementOn": [{ "event": "decision_applied", "whenTier": "optimal" }]
  }
]
```

Counters MUST NOT appear in `stateVariables` or `initialState`.  
In application snapshots they MUST live in a sibling object:

```json
{
  "state": { "...numeric variables only..." },
  "counters": { "correctiveScenesExperienced": 0, "highRiskDecisionCount": 0, "optimalDecisionCount": 0 },
  "flags": [],
  "...": "..."
}
```

This is an **application persistence-contract change** with **no database migration** (jsonb remains opaque).

### 13.3 Mutation order

See §11.3 — single authoritative order.

---

## 14. Outcome classifier (closes ADV-H-003, ADV-H-004)

### 14.1 Structure

Required: `evaluationOrder` (`"v1_seven_step"`), `tierPoints`, `positiveHealthFormula`, `decisionQualityFormula`, `compositeFormula`, `severeCaps`, `moderateCaps`, `strongGuards`, `scoreBands`, `tieBreakRules` (`"v1_default"`).

### 14.2 Formula grammar (bounded)

| Type | Rules |
|---|---|
| `weighted_dimension_health` | `dimensions: [{ variableId, polarity }]`, min 1. Each variableId MUST exist. Equal weight 1/n. Missing state → validation failure / runtime assert. |
| `tier_average` | `divisor: "scoredDecisionCount"`. If count=0 → runtime assert (terminal requires ≥1 decision). Uses `tierPoints`. |
| `linear_blend` | `terms: [{ metric, weight }]`. Metrics ∈ {`positiveHealth`,`decisionQuality`}. **Weights MUST sum to 1.0 ± 1e-9** or publication rejects. No other normalization. |
| `identity` | `{ source }` pass-through |

**Forbidden:** deeper arithmetic trees; scripts; variable-level weight fields.

**Precision:** All intermediate metrics and `compositeScoreUnrounded` use float64. Band selection uses unrounded composite. Display rounding (nearest integer, half away from zero or half-even — MUST pick **half away from zero** for 1.1.0) occurs **only after** classification.

**Clamping:** Dimensions already clamped per decision; formulas do not re-clamp unless a formula type explicitly says so (none do in 1.1.0).

**Cycles:** Formula dependency graph MUST be acyclic (static check).

### 14.3 Caps / guards / bands

- Severe caps: array order; first match forces `forceOutcomeId`; **stop**.  
- Moderate caps: collect tightest `maxOutcomeId` (preferred) or `maxOutcomeRank`; continue.  
- Prefer `maxOutcomeId` over ranks. If using ranks: Strong=1, Acceptable=2, Partial=3, Failed=4 for CB-SC-001-shaped outcomes; **CAP-P* must use Partial (=3 / `partial_resolution`)**, not Acceptable.  
- Strong guards: `disqualifyOutcomeIds` only; do not force Failed.  
- Score bands: MUST cover (−∞,+∞) without gaps/overlaps; each `outcomeId` unique; each outcome’s `classificationRank` unique.  
- Tie-break (`v1_default`): severe > moderate max > guards (Strong→Acceptable unless moderate/severe requires lower) > unrounded band > inclusive boundaries as CB-SC-001.

### 14.4 Evaluation order

1. Severe caps  
2. Moderate caps  
3. Compute positiveHealth, decisionQuality, compositeScoreUnrounded  
4. Strong guards  
5. Numerical band  
6. Tie-break  
7. Display round + attach outcomeId + classificationTrace  

### 14.5 Reachability

Publication semantic validator MUST prove every declared outcome is reachable when exhaustive traversal of the bounded graph is tractable (decision path count within a configured bound, default: explore all paths up to `maxScoredDecisions`). If analysis cannot establish reachability, **fail closed** (reject publish). Unreachable *some* outcomes after full analysis → reject. (Closes open question; stricter than warn.)

---

## 15. Outcome contract

| Field | Required |
|---|---|
| `outcomeId`, `title`, `classificationRank`, `narrative` | Yes |
| `consequenceSummary`, `recommendations`, `visualMetadata` | No |

`endings[]` **FORBIDDEN** on 1.1.0. Any number of outcomes allowed; CB-SC-001 uses four.

---

## 16. Debrief contract

### 16.1 Authored content (required)

- **`debriefSeed` on every executable option** (§10.2) — mandatory for `schemaVersion: "1.1.0"`.  
- **`debriefTemplate`** (optional scenario-level section config).  
- Outcome narratives, recommendations, and related outcome prose (§15).

Publication MUST reject any 1.1.0 option missing `debriefSeed` or missing required seed subfields (`strongestOptionId`, `whyStronger`, `immediateConsequence`).

### 16.2 Computed / replay-derived (not content fields)

Computed at debrief from pinned content + decision history (+ stored `classificationTrace` if present): resolved evaluation tiers, state/flag impacts, caps/guards fired, corrective/skip history, path replay, display-rounded scores, and any presentation assembly not authored in seeds.

Runtime debrief output MAY enrich authored seeds with computed information but authored seeds remain mandatory and authoritative for option-level explanation content.

ClassificationTrace SHOULD be stored once at completion inside `terminalResult` jsonb (application contract extension, no SQL migration) to avoid re-deriving display scores after future engine changes; identity still pinned by engineVersion.

---

## 17. Option display order (closes ADV-H-011)

**Revision 6 note:** Prior text described the SHA-256 byte stream as `SHA256(material)` followed by `SHA256(material || counter)`, which was ambiguous about whether the first digest omitted the counter suffix. The normative algorithm below matches the hardened SCENARIO_ENGINE_V2 implementation and its golden-vector conformance test. **No runtime semantic change** — this revision documents existing behavior.

### 17.1 Supported policies

`optionDisplayPolicy` MUST be one of the schema-declared enum values. Any other value MUST be rejected at content load or runtime (fail closed).

| Policy | Behavior |
|---|---|
| `authored_order` | §17.2 |
| `randomize_per_attempt_scene` | §17.3–§17.8 (default) |

### 17.2 Policy: `authored_order`

When `optionDisplayPolicy` is `authored_order`:

1. The server MUST return options in the **authored document-array order** of `scene.decision.options[]` (each entry's `id` field, in array index order).
2. No seed material is calculated.
3. No shuffle is performed.
4. Stable attempt identity (`attemptId`) MUST NOT affect display order.
5. Repeated initialization and replay with the same content MUST produce the same authored order.
6. The server MUST still include `optionDisplayOrder: string[]` (option ids) in the scene response and attempt snapshot.
7. Client submissions MUST use **optionId only** — never display index or position.
8. Stable option IDs remain the submission identity regardless of display order.

### 17.3 Policy: `randomize_per_attempt_scene` — overview

When `optionDisplayPolicy` is `randomize_per_attempt_scene`:

1. The server derives display order using the **fully specified** deterministic algorithm in §17.4–§17.8.
2. The server MUST include `optionDisplayOrder: string[]` in the scene response and current attempt snapshot.
3. Client submissions MUST use **optionId only** — never index.
4. Replay MUST regenerate order with the same algorithm and MUST verify equality with stored `optionDisplayOrder` when present.
5. Accessibility announces in displayed order. Analytics join on optionId.
6. Copied attempts (new `attemptId`) get new orders. Mid-scene resume uses snapshot order (verified).
7. Implementations MUST NOT use language-runtime hash functions (e.g. Python `hash()`), process-local PRNG state, or wall-clock time. `PYTHONHASHSEED` and similar environment variables are irrelevant.

**No DB migration** — snapshot jsonb holds `optionDisplayOrder` / per-scene map.

### 17.4 Seed material (exact field order)

Let:

- `attemptId` = stable attempt identity string supplied by the server/persistence layer (non-empty for a live attempt).
- `simulationId` = published scenario `simulationId`.
- `version` = published scenario content `version`.
- `canonicalContentSha256` = published lowercase hex digest pinned on the attempt (§18).
- `sceneId` = current scene `id`.

Construct seed material as a **single UTF-8 byte sequence**:

```
seedMaterialBytes = UTF8(
  attemptId + "\n" +
  simulationId + "\n" +
  version + "\n" +
  canonicalContentSha256 + "\n" +
  sceneId
)
```

Rules:

- Separator is exactly one U+000A newline character (`"\n"`) between consecutive fields; **no** trailing newline after the final field.
- Fields are concatenated **verbatim** as stored in the pinned content/attempt identity — no trimming, case folding, or Unicode normalization beyond UTF-8 encoding of the source strings.
- Empty strings are permitted if the underlying field is empty (not recommended for production attempts, but encoding proceeds normally).
- Encoding MUST be UTF-8.

### 17.5 SHA-256 digest block stream

Define an unbounded deterministic byte stream from `seedMaterialBytes`:

```
counter ← 0
loop forever:
  block ← SHA256( seedMaterialBytes || uint32be(counter) )
  emit block[0], block[1], …, block[31]   // 32 bytes, big-endian digest as raw bytes
  counter ← counter + 1
```

Where:

- `counter` starts at **0** and increments by **1** after each digest.
- `uint32be(counter)` is the counter encoded as **4 unsigned big-endian bytes** (values 0…4294967295).
- The **first** digest uses `counter = 0` (there is no separate bare `SHA256(seedMaterialBytes)` block).
- When more bytes are needed, the next digest uses the next counter value; unused bytes from a prior digest are **not** retained — consumption is strictly sequential through the concatenated stream of all digest bytes in counter order.

### 17.6 Uniform index draw (rejection sampling)

To obtain a uniform integer `j` in `[0, upperInclusive]` inclusive:

1. Let `n = upperInclusive + 1`. If `n > 256`, the engine MUST fail closed (scenes MUST NOT exceed 256 options under this algorithm).
2. Let `limit = floor(256 / n) * n`.
3. Read the next byte `b` from the digest stream (§17.5).
4. If `b >= limit`, discard `b` and repeat step 3.
5. Return `j = b mod n`.

This removes modulo bias. Draws are deterministic given the seed material and prior consumption.

### 17.7 Fisher–Yates shuffle

Input: `optionIds` = list of stable option id strings in **authored document-array order** (`scene.decision.options[].id`).

Algorithm (in-place on a mutable copy `order`):

```
order ← copy(optionIds)
stream ← digest byte stream from §17.5 using seedMaterialBytes from §17.4
for index from len(order)-1 down to 1:
  swapIndex ← uniform index draw from stream with upperInclusive = index   // §17.6
  swap order[index] and order[swapIndex]
return order as tuple/list of option ids
```

Properties:

- Iteration is **backward** from `len-1` to `1` (inclusive).
- Each draw consumes however many stream bytes rejection sampling requires (§17.6).
- Output is a permutation of the input option ids; ids themselves are unchanged — only order changes.

### 17.8 Identity, replay, and unsupported policies

**Determinism:** Identical `(simulationId, version, canonicalContentSha256, attemptId, sceneId, optionIds, policy)` MUST produce identical `optionDisplayOrder`.

**Attempt variance:** Different `attemptId` values MAY produce different valid orders under `randomize_per_attempt_scene`.

**Submission identity:** Learners submit stable `optionId` values only; display position MUST NOT be accepted as identity.

**Replay:** Replay recomputes order from pinned content identity + `attemptId` + decision history context; stored per-scene order maps MUST match recomputation when present.

**Unsupported policy:** If `optionDisplayPolicy` is not one of the supported enum values, load/runtime MUST fail closed with a domain error — MUST NOT silently default to shuffle or authored order.

### 17.9 Conformance golden vector

Implementations SHOULD verify the following fixed case (matches SCENARIO_ENGINE_V2 hardened test):

| Field | Value |
|---|---|
| `optionIds` (authored order) | `["opt-a", "opt-b", "opt-c"]` |
| `attemptId` | `golden-attempt` |
| `simulationId` | `golden-sim` |
| `version` | `1.0.0` |
| `canonicalContentSha256` | `0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef` |
| `sceneId` | `SC-GOLDEN` |
| `optionDisplayPolicy` | `randomize_per_attempt_scene` |

**Expected resulting order:** `["opt-b", "opt-c", "opt-a"]`

Any conforming implementation using §17.4–§17.7 MUST reproduce this order exactly.

---

## 18. Versioning and canonical hash (closes ADV-B-002)

### 18.1 Hash input — noncircular

Let `D` be the published runtime scenario document.

1. Create `D'` = deep copy of `D`.  
2. **Remove** (omit) from `D'`: `canonicalContentSha256`, `contentProvenance`, `publicationMetadata`.  
3. Canonicalize `D'` with the repository-supported approach matching `utils/scenario_schema.compute_canonical_content_sha256` intent:
   - `json.dumps(..., sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
   - UTF-8 encode  
4. `digest = SHA-256(bytes).hexdigest()` lowercase.  
5. Write `digest` into published `D.canonicalContentSha256`.  
6. Publication recomputes steps 1–4 and **MUST** equal stored digest.  
7. Attempts pin the published digest; replay verifies.

**Included in hash:** all authored runtime fields (scenes, classifier, flags, intro, etc.).  
**Excluded:** `canonicalContentSha256`, `contentProvenance`, `publicationMetadata` (generated/volatile).

**Note:** The existing helper hashes an entire in-memory document. Callers MUST pass `D'` (with exclusions already applied), or a 1.1.0-aware wrapper MUST strip excluded keys before calling the same normalization. Embedding the digest inside the hashed object is **forbidden**.

### 18.2 Provenance / publication

Live in excluded fields or DB/sidecar. Compiler MUST normalize and validate **before** hashing and publication.

---

## 19. Persistence and replay boundary (closes ADV-H-002)

### 19.1 Database

**No migration required.** Tables/RPCs remain content-agnostic jsonb.

### 19.2 Application snapshot contract — **CHANGED**

`serialize_run_snapshot` for engine V2 MUST include:

| Field | Notes |
|---|---|
| Identity | `simulationId`, `version`, `schemaVersion`, `canonicalContentSha256`, `engineVersion` |
| `currentSceneId` | |
| `state` | **Numeric variables only** |
| `counters` | **Separate object** |
| `flags` | string[] |
| `decisionHistory` | triples only (learner truth) |
| `routingResolutions` (optional array) | per-decision `{ sequenceNumber, nextSceneId, enteredCorrective, skippedCorrective }` for audit/verify |
| `optionDisplayOrderByScene` | map sceneId → optionId[] |
| `selectedVariantIdByScene` | optional audit map |
| `isComplete` / `terminalResult` | terminalResult MAY include `classificationTrace` |

Client-supplied state, counters, tiers, flags, routing, outcomes, display order → **rejected**; server computes.

### 19.3 Replay pseudocode

```
function replay(pinnedContent, pinnedMeta, decisionHistory, storedSnapshotExtras?):
  assert pinnedContent.schemaVersion == pinnedMeta.schemaVersion
  assert hash(pinnedContent) == pinnedMeta.canonicalContentSha256
  assert pinnedContent.requiredEngineVersion == pinnedMeta.engineVersion == runningEngine
  run = start(pinnedContent)  // state, counters, flags initial
  for d in decisionHistory:
    assert run.currentSceneId == d.sceneId
    option = lookup(d.sceneId, d.optionId)
    // steps §11.3
    apply state/flags; increment tier counters; resolve routing;
    if enteredCorrective: increment correctiveScenesExperienced
    if skippedCorrective: record/verify skip
    if storedSnapshotExtras: verify routingResolutions[d.seq] matches
    advance scene; apply environmental flags; select variant
    regenerate optionDisplayOrder; verify vs stored if present
  if complete: classifyOutcome; verify terminal if stored
  return run
```

---

## 20. Security contract

Client submits only: attempt identity, expected sequence, expected scene id (per current RPC), selected `optionId`, idempotency key, and server-echoed state fingerprints as required by V68 — **not** tiers, deltas, flags, counters, routing, outcomes, or display order.

Server resolves all hidden properties. Stale/mismatched scene/sequence/state_before → reject. Bounded grammar depth/size enforced at publication. No executable content. Presentation metadata — including `learnerPresent`, character visuals, and display order — MUST NOT alter scoring, routing, flag evaluation, or replay correctness.

---

## 21. Static validation responsibilities

| # | Rule | Owner |
|---|---|---|
| 1 | JSON Schema structural validity | **JSON Schema** |
| 2 | Required/forbidden legacy fields on 1.1.0 | **JSON Schema** + custom |
| 3 | Duplicate ids (scene/option/flag/outcome/variant/exchange) | **Custom structural** |
| 4 | Routing targets resolve | **Graph** |
| 5 | Acyclicity (union of primary/corrective/skip edges) | **Graph** |
| 6 | Reachability from startScene | **Graph** |
| 7 | Corrective no-rebranch / reconvergence match | **Semantic** |
| 8 | Path length vs budget policy | **Graph** |
| 9 | stateChanges ⊆ stateVariables | **Semantic** |
| 10 | Flag refs registered; writers/clearers ⊆ allowed | **Semantic** |
| 11 | Caps/guards/bands coverage, unique ranks | **Semantic** |
| 12 | Outcome reachability (fail closed) | **Publication semantic** |
| 13 | Variant unique priorities; override targets exist | **Semantic** |
| 14 | Empty all/any forbidden; depth/size bounds | **Custom structural** |
| 15 | Terminal ⇔ EVALUATE_ENDING; no correctiveRoute | **Semantic** |
| 16 | Exact requiredEngineVersion supported by publisher | **Publication** |
| 17 | Canonical hash recompute match | **Publication** |
| 18 | Introduction present; startScene scored | **Semantic** |
| 19 | Formula weight sum; acyclic metrics | **Semantic** |
| 20 | Runtime assert: option in scene; sequence | **Runtime assertion** |
| 21 | `learnerPresent` required; boolean on every executable scene | **JSON Schema** |
| 22 | `charactersPresent` ⊆ registered characters; MUST NOT contain `"learner"`; `characterId` MUST NOT be `"learner"` | **JSON Schema** + **Semantic** |
| 23 | `speakerId: "learner"` allowed only in dialogue exchanges, not as registry reference | **Semantic** |
| 24 | Every executable option MUST include `debriefSeed` with required subfields | **JSON Schema** + **Publication** |

JSON Schema **cannot** enforce graph reachability, cycles, outcome reachability, hashing, or engine availability.

---

## 22. Runtime semantic rules

See §11.3 (decision application), §11.2 (routing), §14.4 (classification), §9.2 (variants), §17 (display order), §19.3 (replay).

Clear-before-set remains normative for flags.

---

## 23. Illustrative vertical-slice JSON (internally valid)

**Representation choice (ADV-B-004):** Include C01, C02, R2A, and C03 as executable scenes. C03 demonstrates flag-dependent dialogue, then terminates via `EVALUATE_ENDING` with a minimal complete classifier — **no self-loop**. Every option in this fixture includes a required `debriefSeed` per §10.2. This document is a **publishable-shaped engineering fixture** for the vertical slice (not the full CB-SC-001 product).

**Corrective-budget scope (revision 5):** This fixture authors **one** corrective scene (`SC001-R2A`). Its `correctiveBudgetPolicy` therefore sets `maxAvailableCorrectiveScenes: 1` and `maxExperiencedCorrectiveScenes: 1`, satisfying CV-071. The full CB-SC-001 production contract (§11.5) remains five authored / three experienced maximum.

```json
{
  "simulationId": "cb-sc-001-onboarding-handoff-vslice",
  "version": "0.2.0-vslice",
  "schemaVersion": "1.1.0",
  "requiredEngineVersion": "SCENARIO_ENGINE_V2",
  "certificationExamName": "Salesforce Certified Business Analyst",
  "examCode": "BA-201",
  "title": "Customer Onboarding Handoff (Vertical Slice)",
  "description": "Engineering vertical slice: C01–C03 + R2A; C03 terminates for fixture validity.",
  "estimatedMinutes": 15,
  "locale": "en-US",
  "optionDisplayPolicy": "randomize_per_attempt_scene",
  "learnerRole": {
    "title": "Embedded Business Analyst",
    "summary": "You advise Elena Vasquez during a high-stakes onboarding handoff."
  },
  "introduction": {
    "companyIntroduction": "Meridian Health is a regional clinic network adopting Salesforce Service Cloud.",
    "projectBriefing": {
      "title": "Crestline Onboarding Handoff",
      "summary": "Sales closed Crestline; CS must onboard under month-end pressure.",
      "customerName": "Crestline Manufacturing"
    },
    "characterCards": [
      { "characterId": "CB-CH-002", "introText": "Elena Vasquez leads Customer Success.", "displayOrder": 1 },
      { "characterId": "CB-CH-001", "introText": "Marcus Chen owns the Sales handoff.", "displayOrder": 2 }
    ],
    "knownRisks": ["Incomplete handoff package", "Month-end customer expectation"],
    "successCriteria": ["Documented ownership", "Aligned internal story before customer updates"],
    "startGate": {
      "headline": "Ready to begin?",
      "body": "You will advise Elena scene by scene. Outcomes appear only at the end.",
      "confirmButtonLabel": "Start Scenario",
      "cancelBehavior": "return_to_catalog"
    }
  },
  "characters": [
    { "characterId": "CB-CH-001", "displayName": "Marcus Chen", "roleTitle": "Sales Representative", "accessibilityDescription": "Sales representative near door" },
    { "characterId": "CB-CH-002", "displayName": "Elena Vasquez", "roleTitle": "Customer Success Manager", "accessibilityDescription": "Customer success manager with laptop" },
    { "characterId": "CB-CH-003", "displayName": "Jordan Blake", "roleTitle": "Operations Coordinator", "accessibilityDescription": "Operations coordinator" }
  ],
  "flags": [
    {
      "flagId": "flag-verbal-handoff-only",
      "valueType": "boolean",
      "initialValue": false,
      "sticky": true,
      "allowedSetters": [{ "sceneId": "SC001-C01", "optionId": "opt-sc001-c01-c" }],
      "allowedClearers": [],
      "debriefRelevant": true
    },
    {
      "flagId": "flag-unsupported-customer-date",
      "valueType": "boolean",
      "initialValue": false,
      "sticky": true,
      "allowedSetters": [{ "sceneId": "SC001-C02", "optionId": "opt-sc001-c02-c" }],
      "allowedClearers": []
    },
    {
      "flagId": "flag-sales-reengaged",
      "valueType": "boolean",
      "initialValue": false,
      "sticky": true,
      "allowedSetters": [
        { "sceneId": "SC001-R2A", "optionId": "opt-sc001-r2a-a" },
        { "sceneId": "SC001-R2A", "optionId": "opt-sc001-r2a-b" }
      ],
      "allowedClearers": []
    }
  ],
  "stateVariables": [
    { "key": "customerConfidence", "displayName": "Customer Confidence", "polarity": "higher_is_better", "minimum": 0, "maximum": 100, "learnerVisibleDuringRun": false },
    { "key": "operationalRisk", "displayName": "Operational Risk", "polarity": "higher_is_worse", "minimum": 0, "maximum": 100, "learnerVisibleDuringRun": false },
    { "key": "dataQuality", "displayName": "Data Quality", "polarity": "higher_is_better", "minimum": 0, "maximum": 100, "learnerVisibleDuringRun": false },
    { "key": "scheduleImpact", "displayName": "Schedule Impact", "polarity": "higher_is_worse", "minimum": 0, "maximum": 100, "learnerVisibleDuringRun": false },
    { "key": "complianceExposure", "displayName": "Compliance Exposure", "polarity": "higher_is_worse", "minimum": 0, "maximum": 100, "learnerVisibleDuringRun": false },
    { "key": "requirementsClarity", "displayName": "Requirements Clarity", "polarity": "higher_is_better", "minimum": 0, "maximum": 100, "learnerVisibleDuringRun": false },
    { "key": "stakeholderAlignment", "displayName": "Stakeholder Alignment", "polarity": "higher_is_better", "minimum": 0, "maximum": 100, "learnerVisibleDuringRun": false }
  ],
  "initialState": {
    "customerConfidence": 68,
    "operationalRisk": 42,
    "dataQuality": 44,
    "scheduleImpact": 38,
    "complianceExposure": 32,
    "requirementsClarity": 48,
    "stakeholderAlignment": 52
  },
  "runtimeCounters": [
    { "counterId": "correctiveScenesExperienced", "initialValue": 0, "minimum": 0, "maximum": 1, "incrementOn": [{ "event": "corrective_scene_entered" }] },
    { "counterId": "highRiskDecisionCount", "initialValue": 0, "incrementOn": [{ "event": "decision_applied", "whenTier": "high-risk" }] },
    { "counterId": "optimalDecisionCount", "initialValue": 0, "incrementOn": [{ "event": "decision_applied", "whenTier": "optimal" }] }
  ],
  "correctiveBudgetPolicy": {
    "maxAvailableCorrectiveScenes": 1,
    "maxExperiencedCorrectiveScenes": 1,
    "maxScoredDecisions": 4,
    "minScoredDecisions": 3,
    "experiencedCounterId": "correctiveScenesExperienced"
  },
  "stages": [
    { "stageId": "stage-1", "label": "Handoff Initiation" },
    { "stageId": "stage-2", "label": "Operations Readiness" }
  ],
  "startScene": "SC001-C01",
  "scenes": [
    {
      "id": "SC001-C01",
      "sceneType": "core",
      "stageId": "stage-1",
      "title": "The Closed Deal",
      "setting": "Small conference room; CRM summary on wall display.",
      "charactersPresent": ["CB-CH-001", "CB-CH-002"],
      "learnerPresent": true,
      "dialogue": {
        "exchanges": [
          { "exchangeId": "ex-001", "speakerId": "CB-CH-001", "text": "Crestline is closed. Ops can take it from the CRM notes.", "tone": "hurried" },
          { "exchangeId": "ex-002", "speakerId": "CB-CH-002", "text": "Marcus, before we hand off—what is still open?", "tone": "professional" }
        ]
      },
      "decision": {
        "prompt": "What do you recommend Elena do next?",
        "options": [
          {
            "id": "opt-sc001-c01-a",
            "text": "Document open gaps in writing before any Operations handoff.",
            "evaluationTier": "optimal",
            "stateChanges": { "customerConfidence": 2, "operationalRisk": -3, "dataQuality": 5, "scheduleImpact": 1, "complianceExposure": -2, "requirementsClarity": 6, "stakeholderAlignment": 4 },
            "feedback": "Elena opens a structured gap list.",
            "routing": { "terminal": false, "primaryNextSceneId": "SC001-C02" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-c01-a", "whyStronger": "Documentation before handoff reduces rework.", "immediateConsequence": "Team aligns on written gaps." }
          },
          {
            "id": "opt-sc001-c01-b",
            "text": "Proceed on Marcus's verbal summary; Elena logs follow-ups async.",
            "evaluationTier": "acceptable",
            "stateChanges": { "scheduleImpact": 2, "requirementsClarity": 2, "stakeholderAlignment": 1 },
            "feedback": "Elena accepts but notes follow-up risk.",
            "routing": { "terminal": false, "primaryNextSceneId": "SC001-C02" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-c01-a", "whyStronger": "Async logging delays accountability.", "immediateConsequence": "Handoff proceeds on verbal summary." }
          },
          {
            "id": "opt-sc001-c01-c",
            "text": "Treat CRM as sufficient to avoid delaying Marcus's pipeline.",
            "evaluationTier": "high-risk",
            "stateChanges": { "customerConfidence": -1, "operationalRisk": 4, "dataQuality": -4, "scheduleImpact": 3, "complianceExposure": 3, "requirementsClarity": -3, "stakeholderAlignment": -2 },
            "setFlags": ["flag-verbal-handoff-only"],
            "feedback": "Elena hesitates; the folder remains thin.",
            "routing": { "terminal": false, "primaryNextSceneId": "SC001-C02" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-c01-a", "whyStronger": "CRM completeness is not handoff quality.", "immediateConsequence": "Verbal-handoff flag set." }
          }
        ]
      }
    },
    {
      "id": "SC001-C02",
      "sceneType": "core",
      "stageId": "stage-1",
      "title": "Missing Handoff Information",
      "setting": "Same conference room; empty handoff fields on screen.",
      "charactersPresent": ["CB-CH-001", "CB-CH-002"],
      "learnerPresent": true,
      "dialogue": {
        "exchanges": [
          { "exchangeId": "ex-001", "speakerId": "CB-CH-002", "text": "Agreement link is missing. Who owns onboarding?", "tone": "firm" }
        ],
        "variants": [
          {
            "variantId": "c02-after-documented",
            "priority": 10,
            "when": { "flagNotSet": "flag-verbal-handoff-only" },
            "overrides": [{ "exchangeId": "ex-001", "text": "We need CS ownership documented before Marcus leaves.", "tone": "firm" }]
          }
        ]
      },
      "decision": {
        "prompt": "How should Elena assign ownership and handle gaps?",
        "options": [
          {
            "id": "opt-sc001-c02-a",
            "text": "Assign CS ownership and document gaps before Operations review.",
            "evaluationTier": "acceptable",
            "stateChanges": { "customerConfidence": 2, "operationalRisk": -4, "dataQuality": 6, "requirementsClarity": 5, "stakeholderAlignment": 5 },
            "feedback": "Elena captures owners and open items.",
            "routing": { "terminal": false, "primaryNextSceneId": "SC001-C03" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-c02-a", "whyStronger": "Ownership plus documentation closes the gap.", "immediateConsequence": "Direct path to Operations readiness." }
          },
          {
            "id": "opt-sc001-c02-b",
            "text": "Accept verbal handoff; defer gap documentation to Operations.",
            "evaluationTier": "suboptimal",
            "stateChanges": { "operationalRisk": 3, "dataQuality": -2, "scheduleImpact": 2, "stakeholderAlignment": -1 },
            "feedback": "Marcus leaves; gaps remain undocumented.",
            "routing": {
              "terminal": false,
              "primaryNextSceneId": "SC001-C03",
              "correctiveRoute": {
                "triggerOnTiers": ["suboptimal", "high-risk"],
                "budgetCondition": { "counterCompare": { "counterId": "correctiveScenesExperienced", "op": "lt", "value": 1 } },
                "correctiveSceneId": "SC001-R2A",
                "whenCorrectiveSkippedNextSceneId": "SC001-C03",
                "reconvergenceSceneId": "SC001-C03"
              }
            },
            "debriefSeed": { "strongestOptionId": "opt-sc001-c02-a", "whyStronger": "Deferring documentation shifts risk to Operations.", "immediateConsequence": "Corrective Sales Pushback may trigger." }
          },
          {
            "id": "opt-sc001-c02-c",
            "text": "Confirm month-end install timing to reassure Sales.",
            "evaluationTier": "high-risk",
            "stateChanges": { "customerConfidence": -6, "operationalRisk": 8, "dataQuality": -5, "scheduleImpact": 10, "complianceExposure": 5, "requirementsClarity": -4, "stakeholderAlignment": -6 },
            "setFlags": ["flag-unsupported-customer-date"],
            "feedback": "Elena repeats an unsupported customer date.",
            "routing": {
              "terminal": false,
              "primaryNextSceneId": "SC001-C03",
              "correctiveRoute": {
                "triggerOnTiers": ["suboptimal", "high-risk"],
                "budgetCondition": { "counterCompare": { "counterId": "correctiveScenesExperienced", "op": "lt", "value": 1 } },
                "correctiveSceneId": "SC001-R2A",
                "whenCorrectiveSkippedNextSceneId": "SC001-C03",
                "reconvergenceSceneId": "SC001-C03"
              }
            },
            "debriefSeed": { "strongestOptionId": "opt-sc001-c02-a", "whyStronger": "Customer dates require internal alignment first.", "immediateConsequence": "Unsupported date flag set." }
          }
        ]
      }
    },
    {
      "id": "SC001-R2A",
      "sceneType": "corrective",
      "stageId": "stage-1",
      "title": "Sales Pushback",
      "setting": "Sales floor; Marcus on headset.",
      "charactersPresent": ["CB-CH-001", "CB-CH-002"],
      "learnerPresent": true,
      "correctiveMetadata": {
        "triggerSceneId": "SC001-C02",
        "reconvergenceSceneId": "SC001-C03",
        "mayRebranch": false
      },
      "dialogue": {
        "exchanges": [
          { "exchangeId": "ex-001", "speakerId": "CB-CH-001", "text": "I do not have time for another checklist.", "tone": "defensive" }
        ]
      },
      "decision": {
        "prompt": "How should Elena re-engage Marcus?",
        "options": [
          {
            "id": "opt-sc001-r2a-a",
            "text": "Send structured email with numbered gaps and customer-impact framing.",
            "evaluationTier": "acceptable",
            "stateChanges": { "customerConfidence": 1, "dataQuality": 3, "stakeholderAlignment": 4 },
            "setFlags": ["flag-sales-reengaged"],
            "feedback": "Marcus agrees to respond after his call.",
            "routing": { "terminal": false, "primaryNextSceneId": "SC001-C03" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-r2a-a", "whyStronger": "Structured requests restore Sales engagement.", "immediateConsequence": "Sales re-engaged flag set." }
          },
          {
            "id": "opt-sc001-r2a-b",
            "text": "Escalate to Sales Manager for handoff enforcement.",
            "evaluationTier": "acceptable",
            "stateChanges": { "customerConfidence": 2, "operationalRisk": -3, "stakeholderAlignment": 5 },
            "setFlags": ["flag-sales-reengaged"],
            "feedback": "Sales leadership looped in.",
            "routing": { "terminal": false, "primaryNextSceneId": "SC001-C03" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-r2a-a", "whyStronger": "Escalation restores accountability.", "immediateConsequence": "Reconverges at Operations readiness." }
          },
          {
            "id": "opt-sc001-r2a-c",
            "text": "Accept delay and document risk for later follow-up.",
            "evaluationTier": "suboptimal",
            "stateChanges": { "operationalRisk": 3, "scheduleImpact": 2, "stakeholderAlignment": -2 },
            "feedback": "Gap remains unresolved.",
            "routing": { "terminal": false, "primaryNextSceneId": "SC001-C03" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-r2a-a", "whyStronger": "Delayed follow-up preserves handoff risk.", "immediateConsequence": "Reconverges without sales-reengaged flag." }
          }
        ]
      }
    },
    {
      "id": "SC001-C03",
      "sceneType": "core",
      "stageId": "stage-2",
      "title": "Operations Readiness Meeting",
      "setting": "Operations war room; checklist on wall display.",
      "charactersPresent": ["CB-CH-002", "CB-CH-003"],
      "learnerPresent": true,
      "enteringStateDescription": "Slice terminal scene — demonstrates flag-dependent dialogue, then ends.",
      "dialogue": {
        "exchanges": [
          { "exchangeId": "ex-002", "speakerId": "CB-CH-003", "text": "Walk me through what Sales actually handed over.", "tone": "neutral" }
        ],
        "variants": [
          {
            "variantId": "c03-both-flags",
            "priority": 5,
            "when": {
              "all": [
                { "flagSet": "flag-verbal-handoff-only" },
                { "flagSet": "flag-sales-reengaged" }
              ]
            },
            "overrides": [{ "exchangeId": "ex-002", "text": "CRM looks complete but the folder is thin — and Marcus is only now re-engaging.", "tone": "skeptical" }]
          },
          {
            "variantId": "c03-verbal-handoff",
            "priority": 10,
            "when": { "flagSet": "flag-verbal-handoff-only" },
            "overrides": [{ "exchangeId": "ex-002", "text": "CRM says complete, but the folder's thin.", "tone": "skeptical" }]
          },
          {
            "variantId": "c03-sales-reengaged",
            "priority": 20,
            "when": { "flagSet": "flag-sales-reengaged" },
            "overrides": [{ "exchangeId": "ex-002", "text": "Marcus is re-engaging, but we have not validated the package yet.", "tone": "cautious" }]
          }
        ]
      },
      "decision": {
        "prompt": "How should Elena open the Operations readiness discussion?",
        "options": [
          {
            "id": "opt-sc001-c03-a",
            "text": "Open with a numbered gap list and request hold until minimum threshold met.",
            "evaluationTier": "optimal",
            "stateChanges": { "customerConfidence": 2, "operationalRisk": -4, "dataQuality": 5, "requirementsClarity": 4, "stakeholderAlignment": 5 },
            "feedback": "Jordan engages with the gap list.",
            "routing": { "terminal": true, "primaryNextSceneId": "EVALUATE_ENDING" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-c03-a", "whyStronger": "Evidence-first framing protects Operations readiness.", "immediateConsequence": "Slice ends — outcome classified." }
          },
          {
            "id": "opt-sc001-c03-b",
            "text": "Present what Sales provided and ask Jordan to prioritize remaining blockers.",
            "evaluationTier": "acceptable",
            "stateChanges": { "customerConfidence": 1, "operationalRisk": -1, "dataQuality": 2, "stakeholderAlignment": 2 },
            "feedback": "Discussion proceeds collaboratively.",
            "routing": { "terminal": true, "primaryNextSceneId": "EVALUATE_ENDING" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-c03-a", "whyStronger": "Asking Ops to discover gaps is weaker than bringing evidence.", "immediateConsequence": "Slice ends." }
          },
          {
            "id": "opt-sc001-c03-c",
            "text": "Recommend Jordan proceed while Sales completes documentation in parallel.",
            "evaluationTier": "high-risk",
            "stateChanges": { "customerConfidence": -1, "operationalRisk": 5, "dataQuality": -3, "scheduleImpact": 4, "stakeholderAlignment": -3 },
            "feedback": "Jordan objects to parallel execution risk.",
            "routing": { "terminal": true, "primaryNextSceneId": "EVALUATE_ENDING" },
            "debriefSeed": { "strongestOptionId": "opt-sc001-c03-a", "whyStronger": "Parallel undocumented work raises operational risk.", "immediateConsequence": "Slice ends." }
          }
        ]
      }
    }
  ],
  "outcomeClassifier": {
    "evaluationOrder": "v1_seven_step",
    "tierPoints": { "optimal": 4, "acceptable": 3, "suboptimal": 1, "high-risk": 0 },
    "positiveHealthFormula": {
      "type": "weighted_dimension_health",
      "dimensions": [
        { "variableId": "customerConfidence", "polarity": "higher_is_better" },
        { "variableId": "dataQuality", "polarity": "higher_is_better" },
        { "variableId": "requirementsClarity", "polarity": "higher_is_better" },
        { "variableId": "stakeholderAlignment", "polarity": "higher_is_better" },
        { "variableId": "operationalRisk", "polarity": "higher_is_worse" },
        { "variableId": "scheduleImpact", "polarity": "higher_is_worse" },
        { "variableId": "complianceExposure", "polarity": "higher_is_worse" }
      ]
    },
    "decisionQualityFormula": { "type": "tier_average", "divisor": "scoredDecisionCount" },
    "compositeFormula": {
      "type": "linear_blend",
      "terms": [
        { "metric": "positiveHealth", "weight": 0.55 },
        { "metric": "decisionQuality", "weight": 0.45 }
      ]
    },
    "severeCaps": [
      {
        "capId": "CAP-F01",
        "when": { "flagSet": "flag-unsupported-customer-date" },
        "effect": { "forceOutcomeId": "failed_resolution" }
      }
    ],
    "moderateCaps": [
      {
        "capId": "CAP-P03",
        "when": { "stateCompare": { "variableId": "operationalRisk", "op": "gte", "value": 75 } },
        "effect": { "maxOutcomeId": "partial_resolution" }
      }
    ],
    "strongGuards": [
      {
        "guardId": "GRD-S01",
        "when": { "counterCompare": { "counterId": "highRiskDecisionCount", "op": "gte", "value": 1 } },
        "effect": { "disqualifyOutcomeIds": ["strong_resolution"] }
      }
    ],
    "scoreBands": [
      { "outcomeId": "strong_resolution", "minInclusive": 88, "maxExclusive": null },
      { "outcomeId": "acceptable_resolution", "minInclusive": 72, "maxExclusive": 88 },
      { "outcomeId": "partial_resolution", "minInclusive": 55, "maxExclusive": 72 },
      { "outcomeId": "failed_resolution", "minInclusive": null, "maxExclusive": 55 }
    ],
    "tieBreakRules": "v1_default"
  },
  "outcomes": [
    { "outcomeId": "strong_resolution", "title": "Strong Resolution", "classificationRank": 1, "narrative": "Slice outcome — strong path." },
    { "outcomeId": "acceptable_resolution", "title": "Acceptable Resolution", "classificationRank": 2, "narrative": "Slice outcome — acceptable path." },
    { "outcomeId": "partial_resolution", "title": "Partial Resolution", "classificationRank": 3, "narrative": "Slice outcome — partial path." },
    { "outcomeId": "failed_resolution", "title": "Failed Resolution", "classificationRank": 4, "narrative": "Slice outcome — failed path (e.g. unsupported customer date)." }
  ]
}
```

`contentProvenance` / `canonicalContentSha256` / `publicationMetadata` are omitted above; they are applied at publish after hashing `D'` per §18.

---

## 24. Backward-compatibility matrix

| Capability | 1.0.0 | 1.1.0 |
|---|---|---|
| `nextScene` | Required / authoritative | **Forbidden** |
| `routing` | — | Required |
| `isCorrect` | Required | **Forbidden** |
| `evaluationTier` | — | Required |
| `narrative` | Required | **Forbidden** |
| `dialogue` | — | Required |
| `endings[]` | Required | **Forbidden** |
| `outcomeClassifier` / `outcomes[]` | — | Required |
| Engine | `SCENARIO_ENGINE_V1` | Exact `requiredEngineVersion` |
| Counters in snapshot | — | Sibling `counters` object |
| Learner in scene | — | `learnerPresent: boolean` required; `"learner"` forbidden in `charactersPresent` and character registry; `speakerId: "learner"` allowed in dialogue only |
| Option debrief | — | `debriefSeed` **required** on every executable option |
| DB schema | V68 | Unchanged |

---

## 25. Open engineering questions — closed

| # | Disposition |
|---|---|
| Counter storage | Sibling `counters` in application snapshot — not mixed into float `state` |
| C03 boundary | Executable C03 terminating at `EVALUATE_ENDING` — no self-loop |
| Environmental flags | Apply on entry **before** dialogue variant selection |
| `endings[]` | Forbidden on 1.1.0 when classifier/outcomes present (always for 1.1.0) |
| Outcome reachability | Publication fail-closed when not proven |
| Compiler normalization | Validate + normalize **before** hash and publish |
| Learner presence | `learnerPresent` boolean required per scene; `"learner"` not a registry character; aligned with executable JSON Schema (SIM-SCHEMA-11-JSON-01) |
| Option debrief seeds | `debriefSeed` required on every executable 1.1.0 option; closes JR-H-001 (SIM-SCHEMA-11-SPEC-04) |

---

## 26. Explicit non-goals

Executable JSON Schema file; engine implementation; compiler; DB migrations; UI; Creative Studio transforms; LLM runtime dialogue.

---

## 27. Acceptance criteria

All ADV-B and ADV-H findings closed; single counter order; separate counters; noncircular hash; no empty all/any; no optionTier leaf; no C03 self-loop; explicit legacy forbid rules; classified validators; valid illustrative JSON conforming to executable JSON Schema including learner-presence and required `debriefSeed` on every option; no DB migration; application snapshot changes acknowledged.

---

## 28. Recommended next implementation task

**SIM-SCHEMA-11-VALIDATOR-01**: implement layered Python validators and catalog publish integration for `schemaVersion: "1.1.0"`, using `scenario_content/schemas/1.1.0/simulation.schema.json` and `SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md`.

---

## Appendix A — Condition grammar (BNF)

```
Condition ::= { "all": [ Condition+ ] } | { "any": [ Condition+ ] } | { "not": Condition } | Leaf
Leaf      ::= { "flagSet": FlagId } | { "flagNotSet": FlagId }
            | { "stateCompare": { "variableId": Id, "op": Op, "value": Number } }
            | { "counterCompare": { "counterId": Id, "op": Op, "value": Number } }
```

Empty `all`/`any` invalid. No `optionTierInCurrentDecision`.

## Appendix B — Formula grammar (BNF)

```
Formula ::= weighted_dimension_health | tier_average | linear_blend | identity
```

Weights in `linear_blend` MUST sum to 1.0 ± 1e-9.

---

*End of corrected normative specification (revision 4).*
