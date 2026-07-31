# Schema 1.1.0 Spec Revision 3 — Learner-Presence Alignment Report

**Task ID:** SIM-SCHEMA-11-SPEC-03  
**Date:** 2026-07-30  
**Scope:** Documentation-only alignment between normative spec, illustrative JSON, and executable JSON Schema.

---

## Purpose

SIM-SCHEMA-11-JSON-01 introduced `learnerPresent: boolean` as a required scene field and forbade the literal `"learner"` in `charactersPresent` and the character registry. Revision 2 of the normative spec still listed `"learner"` in `charactersPresent` in §23. Revision 3 closes that gap without changing scoring, routing, flags, state, outcomes, or classifier semantics.

---

## Changes applied (SCENARIO_SCHEMA_1_1_0_SPEC.md only)

| Section | Change |
|---|---|
| Header | Revision **3**; task SIM-SCHEMA-11-SPEC-03; alignment notice |
| §2 Terminology | Added **Registered character** and **Learner presence** definitions |
| §7 Character registry | `characterId` MUST NOT be `"learner"`; registry resolution rules |
| §8 Scene contract | Updated `charactersPresent`; added required `learnerPresent` |
| §8.1 (new) | Normative learner-presence rules table and dialogue distinction |
| §9.1 Dialogue | Clarified `speakerId` learner allowance vs registry prohibition |
| §20 Security | Presentation metadata explicitly includes `learnerPresent` |
| §21 Validation | Added rules 21–23 for learner presence |
| §23 Illustrative JSON | Four scenes corrected (see below) |
| §24 Compatibility matrix | Added learner-in-scene row |
| §25 Closed questions | Added learner-presence disposition |
| §27 Acceptance criteria | Illustrative JSON must conform to executable schema |
| §28 Next task | SIM-SCHEMA-11-VALIDATOR-01 |

**Unchanged:** routing pseudocode, counter order, corrective budget, outcome classifier, flags, state variables, dialogue text, option tiers, debrief seeds, scene IDs, and all scoring-related values.

---

## Normative definitions (revision 3)

### `learnerPresent`
- **Type:** boolean  
- **Required:** on every executable scene (core and corrective)  
- **Authority:** presentation/runtime metadata only  
- **Semantics:** `true` when the learner/advisee participates in the scene; `false` when absent  
- **MUST NOT** affect scoring, routing, flag evaluation, variant selection, or replay correctness by itself  
- **MUST NOT** be used as a character registry reference  

### `charactersPresent`
- **Type:** string[] of registered `characterId` values from `characters[]`  
- **MUST NOT** contain the literal `"learner"`  
- MAY be empty when no registered characters are physically present  

### `speakerId`
- MAY be a registered `characterId` **or** the literal `"learner"` in `dialogue.exchanges[]` only  
- `"learner"` as `speakerId` does **not** make the learner a canonical character  

---

## Illustrative JSON corrections (§23)

| Scene | `charactersPresent` (before → after) | `learnerPresent` |
|---|---|---|
| SC001-C01 | `["CB-CH-001", "CB-CH-002", "learner"]` → `["CB-CH-001", "CB-CH-002"]` | `true` (added) |
| SC001-C02 | `["CB-CH-001", "CB-CH-002", "learner"]` → `["CB-CH-001", "CB-CH-002"]` | `true` (added) |
| SC001-R2A | `["CB-CH-001", "CB-CH-002", "learner"]` → `["CB-CH-001", "CB-CH-002"]` | `true` (added) |
| SC001-C03 | `["CB-CH-002", "CB-CH-003", "learner"]` → `["CB-CH-002", "CB-CH-003"]` | `true` (added) |

Character registry (`characters[]`) unchanged — no `"learner"` entry added.

---

## Validation checklist

| # | Check | Result |
|---|---|---|
| 1 | No normative example uses `"learner"` in `charactersPresent` | **PASS** |
| 2 | `learnerPresent` defined as boolean | **PASS** |
| 3 | `learnerPresent` required for executable scenes | **PASS** |
| 4 | `speakerId` may still use `"learner"` | **PASS** |
| 5 | `"learner"` not added to character registry | **PASS** |
| 6 | Illustrative JSON matches executable schema on learner presence | **PASS** (structural alignment; full document validation deferred to validator task) |
| 7 | Scoring semantics unchanged | **PASS** |
| 8 | Routing semantics unchanged | **PASS** |
| 9 | No field other than learner-presence docs/examples changed | **PASS** |
| 10 | Executable JSON Schema not modified | **PASS** |

---

## Remaining `"learner"` references in spec (intentional)

All remaining `"learner"` occurrences are normative prohibitions, dialogue `speakerId` rules, or compatibility/validation wording — **not** `charactersPresent` example data.

**Remaining character-reference violations:** **0**

---

## Files touched

| Action | File |
|---|---|
| Modified | `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md` |
| Created | `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC_03_ALIGNMENT_REPORT.md` |

**Not modified:** `simulation.schema.json`, custom-validation companion, authoring report, focused review, runtime, tests.

---

## Recommended next action

**SIM-SCHEMA-11-VALIDATOR-01** — implement layered Python validators and catalog publish integration for `schemaVersion: "1.1.0"`.

---

*End of alignment report.*
