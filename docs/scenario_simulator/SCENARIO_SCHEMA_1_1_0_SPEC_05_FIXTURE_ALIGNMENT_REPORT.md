# SCENARIO_SCHEMA_1_1_0 Spec Revision 5 — Illustrative Fixture Alignment Report

**Task ID:** SIM-SCHEMA-11-SPEC-05  
**Date:** 2026-07-30  
**Status:** COMPLETE

---

## Summary

Closed the remaining normative illustrative-fixture inconsistency identified during SIM-SCHEMA-11-VALIDATOR-01. The §23 vertical-slice JSON now validates unchanged against the executable schema and layered validator. CV-071 was not weakened; test-time normalization was removed.

## Original inconsistency

Revision-4 §23 illustrative JSON declared:

| Field | Value |
|---|---|
| `maxAvailableCorrectiveScenes` | 1 |
| `maxExperiencedCorrectiveScenes` | **3** |

This violated **CV-071** (`maxExperiencedCorrectiveScenes` ≤ `maxAvailableCorrectiveScenes`) and required test-time patching in `load_vslice_fixture()`.

## Corrected illustrative values

| Field | Before | After |
|---|---|---|
| `correctiveBudgetPolicy.maxExperiencedCorrectiveScenes` | 3 | **1** |
| `runtimeCounters[correctiveScenesExperienced].maximum` | 3 | **1** |
| `budgetCondition.counterCompare.value` (both C02 corrective routes) | 3 | **1** |

`maxAvailableCorrectiveScenes` remains **1** (one authored corrective scene: `SC001-R2A`).

## Full production policy confirmation

§11.5 **unchanged** for full CB-SC-001 production contract:

| Field | Production value |
|---|---|
| `maxAvailableCorrectiveScenes` | 5 |
| `maxExperiencedCorrectiveScenes` | 3 |

Narrative in §11.5 and §23 now explicitly distinguishes production limits from the smaller illustrative fixture.

## Test-time normalization removed

Deleted the `load_vslice_fixture()` block that overwrote `maxExperiencedCorrectiveScenes` with `maxAvailableCorrectiveScenes`. Tests now validate the authoritative spec JSON exactly as authored.

## CV-071 unchanged

No validator or custom-validation contract changes. CV-071 semantics preserved.

## Normative example validation result

§23 illustrative JSON passes `collect_scenario_validation_findings()` and `validate_scenario_for_publication()` (with computed canonical hash) without mutation.

## Files modified

- `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md` (revision 5)
- `tests/test_scenario_validation_v1_1.py` (removed normalization)

## File created

- `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC_05_FIXTURE_ALIGNMENT_REPORT.md`

## Verdict

| Metric | Count |
|---|---|
| Remaining blockers | 0 |
| Remaining HIGH | 0 |

Runtime implementation: **No**  
Persistence modified: **No**  
Database migration required: **No**  
Protected paths untouched: **Yes**  
Nothing staged, committed, pushed, or deployed: **Yes**

## Recommended next action

Proceed with **SCENARIO_ENGINE_V2** runtime implementation using validated 1.1.0 content from the catalog/publication boundary.
