# SCENARIO_SCHEMA_1_1_0 Validator Correction Report

**Task ID:** SIM-SCHEMA-11-VALIDATOR-02  
**Date:** 2026-07-30  
**Status:** COMPLETE  
**Closes review:** `SCENARIO_SCHEMA_1_1_0_VALIDATOR_FOCUSED_REVIEW.md` HIGH findings VR-H-001, VR-H-002, VR-H-003 plus material MEDIUMs

---

## 1. Task status

COMPLETE. Focused suite: **99 passed**. Remaining blockers: **0**. Remaining highs from VALIDATOR-REVIEW-01: **0**.

## 2. Files changed

| File | Change |
|---|---|
| `utils/scenario_validation_findings.py` | Layer sort order: semantic before graph; `first_blocking_finding` |
| `utils/scenario_schema.py` | Document-authoritative `schemaVersion`; hint equality; V1 guard on declared version |
| `utils/scenario_catalog.py` | Raise first blocking finding |
| `utils/scenario_validation_v1_1.py` | Finite numbers, dup flags, cycle/path skip, required hash, bounded CV-089 |
| `tests/test_scenario_validation_v1_1.py` | Targeted regressions for all corrections |
| `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_VALIDATOR_IMPLEMENTATION_REPORT.md` | Reachability honesty update |

## 3. Files created

- `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_VALIDATOR_CORRECTION_REPORT.md`

## 4–6. Git

Branch `main` (ahead 17). No stage/commit/push/deploy.

## 7. VR-H-001 disposition — CLOSED

Rule ID **`CV-FIN`** rejects NaN, ±Inf, and bool-as-number for state bounds/initial/deltas, formula weights, band bounds, condition compare values, and counters. Inverted min>max → **`CV-054`**. Bool ranks/priorities rejected.

## 8. VR-H-002 disposition — CLOSED

**CV-089** now means actual bounded path+classification reachability.  
**CV-089R** (semantic) covers reference presence separately.  
Reference-only / band-overlap unions removed. Bound exhaustion fails closed.

## 9. VR-H-003 disposition — CLOSED

Document `schemaVersion` is authoritative. Conflicting hints → structured `CV-001` / `ScenarioValidationError`. `build_scenario_content` guards on declared 1.1.0 before any override path.

## 10–11. Remaining blocker / high counts

**0 / 0** (review HIGHs closed)

## 12–13. Non-finite / bool-as-number

Covered by `CV-FIN` + tests.

## 14. Schema-version authority decision

Hints retained for API compatibility but must **exactly match** the document. Missing document version fails closed.

## 15. Engine V1 guard result

Default and override-mismatch paths cannot execute 1.1.0 as V1 content. Matching-hint 1.0.0 documents unchanged.

## 16–19. Outcome reachability

| Item | Detail |
|---|---|
| Algorithm | DFS/stack exploration of scenes with flag clear-before-set, state clamp, counters, corrective routing, terminal `v1_seven_step` classification |
| Bounds | `maxScoredDecisions` depth; `_MAX_REACHABILITY_STATES = 5000` |
| State key | scene, flags, state, counters, tier history, corrective_used |
| Exhaustion | `CV-089` blocker at `/outcomes` — fail closed; no reference downgrade |

## 20–22. Cap / guard / corrective

Cap force recognized via classification on paths that set triggering flags. Guards applied in classifier. Corrective budget affects explored paths via routing simulation.

## 23. Finding-order correction — CLOSED (VR-M-001)

Order: json_schema → structural → semantic → graph → publication → runtime, then path / rule / message / identifier.

## 24. Cycle/path-bound correction — CLOSED (VR-M-004)

CV-072 present ⇒ skip CV-074/075 path-bound computation.

## 25. Duplicate flag-reference correction — CLOSED (VR-M-005)

Duplicates within `setFlags` or within `clearFlags` → `CV-051`. Set+clear same flag allowed.

## 26. Publication-hash correction — CLOSED (VR-M-006)

Missing hash → `CV-HASH`. Mismatch → `PB-HASH`. CV-007/008 deferred (no catalog publish context).

## 27–28. API / catalog

Compatible; hints equality-enforced; catalog uses first blocking finding.

## 29–32. Tests

Created/updated reachability, finite, hint, ordering, cycle, flag, hash tests.  
Executed: `pytest tests/test_scenario_validation_v1_1.py tests/test_scenario_schema.py tests/test_scenario_catalog.py -q` → **99 passed**.  
1.0.0 BA path unchanged.

## 33–36. Scope

Runtime Engine V2: **No**  
Persistence modified: **No**  
DB migration: **No**  
Risky areas touched: **Yes** (shared schema/catalog entry points; guarded)

## 37–39. Safety

No out-of-scope file edits beyond listed. Protected paths untouched. Nothing staged/committed/pushed/deployed.

## 40–41. Errors / stop conditions

No stop conditions. Honest CV-089 correctly reports §23 vslice high outcomes unreachable (composites ~25–38 → failed band only); documented as remaining authoring risk, not a validator defect.

## 42. Remaining risks

1. §23 fixture not fully CV-089-publishable until scoring/bands/content adjusted.  
2. CV-007/008 await catalog publish pipeline.  
3. Engine V2 still unimplemented.

## 43–44. Next step

Proceed to **SCENARIO_ENGINE_V2** runtime implementation using this validator as the content gate; separately author a publishable vertical-slice scoring pass if §23 must pass CV-089 as-is.
