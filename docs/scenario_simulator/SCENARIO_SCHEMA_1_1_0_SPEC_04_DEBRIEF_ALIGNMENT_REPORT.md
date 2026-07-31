# Schema 1.1.0 Spec Revision 4 — DebriefSeed Alignment Report

**Task ID:** SIM-SCHEMA-11-SPEC-04  
**Date:** 2026-07-30  
**Scope:** Documentation-only correction closing JR-H-001

---

## Purpose

SIM-SCHEMA-11-JSON-REVIEW-01 identified **JR-H-001**: the executable JSON Schema requires `debriefSeed` on every option while revision-3 normative §10 marked it optional. Revision 4 aligns the normative specification with the executable schema and approved product contract without modifying the schema.

---

## Product decision (locked)

`debriefSeed` is **REQUIRED** for every executable learner option in schema 1.1.0.

Every decision must support a detailed final debrief explaining the learner’s choice, strongest-option rationale, alternative weaknesses, immediate/later consequences, competency impact, state/flag impact, and cap/guard relevance where applicable.

---

## Changes applied (`SCENARIO_SCHEMA_1_1_0_SPEC.md` only)

| Section | Change |
|---|---|
| Header | Revision **4**; task SIM-SCHEMA-11-SPEC-04 |
| §1 Correction notice | JR-H-001 closure reference |
| §4.3 | Compiler MUST emit `debriefSeed` on every option |
| §4.4 | Publication MUST reject options missing `debriefSeed` |
| §10 | Introductory authority paragraph; field table `debriefSeed` → **Yes** |
| §10.2 | Expanded seed contract table matching executable `$defs/debriefSeed` |
| §16 | Split authored vs computed debrief; mandatory seed rule |
| §21 | Added validation rule 24 (JSON Schema + Publication) |
| §23 | Commentary: every fixture option includes `debriefSeed` |
| §24 | Compatibility row: option debrief required in 1.1.0 |
| §25 | Closed question: option debrief seeds |
| §27 | Acceptance criteria updated |

**Unchanged:** scoring, routing, flags, state, formulas, persistence, illustrative option values, executable JSON Schema, custom-validation companion.

---

## Field-structure alignment (normative §10.2 vs `$defs/debriefSeed`)

| Field | Schema required | Normative rev 4 | Type |
|---|---|---|---|
| `strongestOptionId` | Yes | Yes | non-empty string |
| `whyStronger` | Yes | Yes | non-empty string (markdown) |
| `immediateConsequence` | Yes | Yes | non-empty string (markdown) |
| `whyWeaker` | No | No | string |
| `laterConsequence` | No | No | string |
| `competencyImpact` | No | No | string |
| `stateImpactSummary` | No | No | string |
| `capGuardEffect` | No | No | string |

`additionalProperties: false` on seed object — implied by executable schema; normative references executable shape.

Option-level required fields in schema `$defs/option`: `id`, `text`, `evaluationTier`, `feedback`, `routing`, **`debriefSeed`** — all reflected in §10.

---

## Targeted validation

### Illustrative JSON (§23)

| Check | Result |
|---|---|
| Executable options counted | **12** (`opt-sc001-*`) |
| Options with `debriefSeed` | **12** |
| Missing `debriefSeed` | **0** |
| Each seed has required subfields | **Yes** (all include `strongestOptionId`, `whyStronger`, `immediateConsequence`) |

Manual grep verification; full JSON Schema validation deferred to validator task (schema unchanged).

### Normative authority statements

| Requirement | Present in rev 4 |
|---|---|
| Every executable option MUST contain `debriefSeed` | §10, §16.1 |
| Publication MUST reject missing `debriefSeed` | §4.4, §16.1, §21 rule 24 |
| Runtime may add computed debrief; seeds mandatory | §10.2, §16.2 |
| 1.0.0 behavior unchanged | §10 intro, §24 |
| Applies only to 1.1.0 executable options | §10 intro |

---

## JR-H-001 disposition

**CLOSED.** Normative requiredness now matches executable schema. No schema relaxation required.

---

## Readiness impact

| Metric | Before SPEC-04 | After SPEC-04 |
|---|---|---|
| Remaining HIGH (JR-H-001) | 1 | **0** |
| Remaining BLOCKER | 0 | **0** |
| MEDIUM (JR-M-001..004) | 4 | **4** (unchanged) |

**Validator implementation readiness:** **READY** (strict 0 HIGH gate met).

---

## Files touched

| Action | File |
|---|---|
| Modified | `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md` |
| Created | `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC_04_DEBRIEF_ALIGNMENT_REPORT.md` |

**Not modified:** `simulation.schema.json`, custom-validation companion, JSON focused review, runtime, tests.

---

## Recommended next action

**SIM-SCHEMA-11-VALIDATOR-01** — implement layered Python validators and catalog publish integration for `schemaVersion: "1.1.0"`.

---

*End of debrief alignment report.*
