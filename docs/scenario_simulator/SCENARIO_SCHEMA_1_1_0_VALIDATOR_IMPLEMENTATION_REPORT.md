# SCENARIO_SCHEMA_1_1_0 Validator Implementation Report

**Task ID:** SIM-SCHEMA-11-VALIDATOR-01  
**Date:** 2026-07-30  
**Status:** COMPLETE

---

## 1. Task status

Production-quality layered validation for CertBound Scenario Simulator schema **1.1.0** is implemented and integrated at the catalog/publication validation boundary. Engine V2 runtime execution is **not** implemented.

## 2. Files changed

| File | Change |
|---|---|
| `utils/scenario_schema.py` | Version dispatch, findings API, publication API, 1.1.0 engine guard on `build_scenario_content` |
| `utils/scenario_catalog.py` | Catalog validation helpers; pre-load validation in `load_resolved_scenario_content` |

## 3. Files created

| File | Purpose |
|---|---|
| `utils/scenario_validation_findings.py` | `ValidationFinding` dataclass, deterministic sort, blocking check |
| `utils/scenario_validation_v1_1.py` | Layered 1.1.0 validators (JSON Schema, structural, semantic, graph, publication) |
| `tests/test_scenario_validation_v1_1.py` | 43+ focused validation test cases |
| `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_VALIDATOR_IMPLEMENTATION_REPORT.md` | This report |

## 4. Repository branch

`main` (ahead of `origin/main`; no commit created for this task)

## 5. Starting git status

Branch `main`, ahead 17; unrelated untracked `.local/`, `local_only/`, docs/schema work present. No validator files existed at task start.

## 6. Ending git status

Modified: `utils/scenario_schema.py`, `utils/scenario_catalog.py`  
Created (untracked): `utils/scenario_validation_findings.py`, `utils/scenario_validation_v1_1.py`, `tests/test_scenario_validation_v1_1.py`, this report  
Protected paths untouched; nothing staged, committed, pushed, or deployed.

## 7. Validator API

| Function | Module | Behavior |
|---|---|---|
| `validate_scenario_document(document, *, schema_version=None)` | `scenario_schema` | Raises `ScenarioValidationError` on blocking findings |
| `validate_scenario_for_publication(document, *, schema_version=None)` | `scenario_schema` | Publication layer for 1.1.0; 1.0.0 delegates to document validation |
| `collect_scenario_validation_findings(document, *, schema_version=None, publication=False)` | `scenario_schema` | Returns sorted `tuple[ValidationFinding, ...]` |
| `validate_v1_1_scenario_document(document, *, publication=False)` | `scenario_validation_v1_1` | Full layered 1.1.0 pipeline |
| `validate_v1_1_scenario_for_publication(document)` | `scenario_validation_v1_1` | Document validation + publication layer |
| `collect_v1_1_json_schema_findings(document)` | `scenario_validation_v1_1` | JSON Schema layer only |
| `compute_canonical_content_sha256_v1_1(document)` | `scenario_validation_v1_1` | Strips hash/provenance/publication fields before SHA-256 |
| `validate_catalog_scenario_document(document, *, publication=False)` | `scenario_catalog` | Catalog-boundary validation |
| `validate_catalog_scenario_file(path, *, publication=False)` | `scenario_catalog` | Load + validate |
| `assert_catalog_scenario_valid(document, *, publication=False)` | `scenario_catalog` | Raises on blocking findings |

## 8. Version-dispatch implementation

Explicit dispatch in `validate_scenario_document`, `collect_scenario_validation_findings`, and `validate_scenario_for_publication`:

- `"1.0.0"` → existing `_validate_and_compute_graph_metadata` path (unchanged semantics)
- `"1.1.0"` → lazy import of `scenario_validation_v1_1` (avoids circular import)
- Any other version → fail closed with `CV-001` / `ScenarioValidationError`
- No silent upgrade or fallback

## 9. JSON Schema validation result

- Draft 2020-12 via `Draft202012Validator`
- Schema cached in `_SCHEMA_CACHE` keyed by `"1.1.0"`
- Local `$defs` resolved from `scenario_content/schemas/1.1.0/simulation.schema.json`
- **All** JSON Schema errors collected (not fail-fast)
- Errors mapped to CV-001..CV-062 where applicable; remainder tagged `JS-SCHEMA`
- JSON Pointer paths preserved
- Input document never mutated; `default` keywords are not applied

## 10. Structural-validation result

Implemented in `_collect_structural_findings`: duplicate IDs (scenes, options, characters, flags, outcomes, exchanges, caps/guards), condition depth ≤ 8, node count ≤ 64, prohibited legacy fields, executable-code heuristic, empty `all`/`any`, `not` arity.

## 11. Graph-validation result

Implemented in `_collect_graph_findings`: startScene resolution, union adjacency (primary + corrective + skip), cycle detection (CV-072), unreachable core scenes (CV-073), corrective topology (CV-064..CV-066), corrective→corrective prohibition, reconvergence consistency, scored path bounds vs `correctiveBudgetPolicy` (CV-074/075). Path-bound computation is cycle-safe via in-progress memo placeholders.

## 12. Semantic-validation result

Implemented in `_collect_semantic_findings`: speaker/learner rules, dialogue variants/overrides, flag authorization, state/counter bounds and references, formula acyclicity and weight sums, outcome band contiguity, debrief seed references, corrective metadata, option display policy declarations.

## 13. Publication-validation result

Implemented in `_collect_publication_findings_without_reachability` and `_collect_bounded_outcome_reachability_findings`: exact schema version, `requiredEngineVersion` must be `SCENARIO_ENGINE_V2`, required matching `canonicalContentSha256` (`CV-HASH` / `PB-HASH`), and **bounded path-classification outcome reachability** (CV-089) that proves outcomes via simulated complete paths (not reference-only). Reference coverage remains `CV-089R` in the semantic layer.

## 14. Structured-finding format

```python
@dataclass(frozen=True)
class ValidationFinding:
    rule_id: str          # e.g. CV-072, PB-HASH, JS-SCHEMA
    layer: str            # json_schema | structural | graph | semantic | publication | runtime
    severity: str         # blocker | high | medium | low | note
    path: str             # JSON Pointer, e.g. /scenes/0/id
    message: str
    identifier: str | None = None
```

## 15. Deterministic error-ordering result

`sort_validation_findings` orders by: layer → path → rule_id → message. Verified in test 42.

## 16. Legacy 1.0.0 compatibility

All 23 existing `test_scenario_schema.py` tests pass unchanged. BA-201 canonical hash, graph metadata, and validation semantics preserved.

## 17. 1.1.0 engine-execution guard

`build_scenario_content` and `load_scenario_content` raise `ScenarioContentError` when `schemaVersion` is `"1.1.0"`, preventing silent SCENARIO_ENGINE_V1 execution.

## 18. Character-reference validation

Duplicate `characterId` (CV-015), unknown speakers (CV-043/044), `"learner"` forbidden in registry and `charactersPresent` (CV-041), allowed as dialogue `speakerId`.

## 19. Dialogue validation

Unique exchange IDs (CV-018), unique variant priorities (CV-045), override target resolution (CV-044), non-empty base exchanges, condition references in variants.

## 20. Condition validation

Depth ≤ 8 (CV-032), nodes ≤ 64 (CV-033), leaf reference resolution (CV-034..036), empty `all`/`any` rejected.

## 21. Flag validation

Unique flag IDs, reference resolution, allowedSetters/allowedClearers authorization (CV-051/052), duplicate set/clear rejection.

## 22. State validation

Unique keys, bounds checks (CV-054/055), delta reference resolution (CV-050), finite numeric values.

## 23. Counter validation

Separate from state variables; reference resolution in conditions and corrective budget (CV-036/107).

## 24. Corrective-budget validation

Counter existence, experienced ≤ available (CV-071), path limits (CV-074), trigger/skip/reconvergence structure (CV-066/067).

## 25. Formula validation

Four bounded types only; acyclic dependencies (CV-082); linear_blend weight sum ± 1e-9 (CV-081).

## 26. Outcome validation

Unique IDs/ranks, band contiguity without gaps/overlaps (CV-087), cap/guard references (CV-086), reference coverage (`CV-089R`), and publication-time bounded path reachability (`CV-089`).

## 27. Option-display validation

Supported policy enum and seed-input declarations validated; no runtime shuffle implemented.

## 28. Hash validation

1.1.0 canonical procedure strips `canonicalContentSha256`, `contentProvenance`, `publicationMetadata`; stable JSON + UTF-8 SHA-256 lowercase hex. 1.0.0 hash semantics unchanged.

## 29. Default-no-mutation validation

Regression test confirms deep equality before/after validation; JSON Schema `default` never injected.

## 30. Catalog integration

`load_resolved_scenario_content` validates before loading. Catalog helpers expose validation without building `ScenarioContent`. 1.0.0 catalog behavior unchanged.

## 31. Tests created

`tests/test_scenario_validation_v1_1.py` — 43 grouped cases covering all task-required scenarios.

## 32. Tests executed

```
python -m pytest tests/test_scenario_validation_v1_1.py tests/test_scenario_schema.py tests/test_scenario_catalog.py -q
```

## 33. Test results

**82 passed** in 0.83s (43 new + 39 existing).

## 34. Errors encountered

1. Circular import between `scenario_schema` and `scenario_validation_v1_1` — resolved via lazy imports and local helpers in v1_1 module.
2. `_compute_scored_path_bounds` infinite recursion on self-loops — resolved via in-progress memo placeholders.
3. Spec §23 fixture violates CV-071 (`maxExperiencedCorrectiveScenes` > `maxAvailableCorrectiveScenes`) — test loader normalizes for conformance testing; documented.

## 35. Stop conditions encountered

None.

## 36. Database migration required

**No**

## 37. Persistence files modified

**No**

## 38. Runtime engine behavior implemented

**No** (validation and execution guard only)

## 39. Risky areas touched

**Yes** — `scenario_schema.py` and `scenario_catalog.py` are shared validation/load boundaries; guarded with lazy imports and explicit 1.1.0 rejection in `build_scenario_content`.

## 40. Files modified outside intended scope

None beyond listed files.

## 41. Confirmation protected paths untouched

Confirmed: `.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`, `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`, `workers/combined_policy_evaluator.py` not inspected or modified.

## 42. Confirmation nothing staged, committed, pushed, or deployed

Confirmed.

## 43. Remaining risks

1. Spec §23 illustrative JSON fails honest CV-089 publication for high score-band outcomes (path composites stay in the failed band); content scoring/bands need a future authoring pass before treating that fixture as fully publishable.
2. Catalog-scoped CV-007 / immutable CV-008 are deferred until a real multi-document publish pipeline provides catalog context.
3. Engine V2 execution remains unimplemented; 1.1.0 content validates but cannot run.

## 44. Git status

See section 6.

## 45. Recommended next step

**SCENARIO_ENGINE_V2** runtime implementation: routing execution, corrective-budget increment, four-tier scoring, outcome classification, dialogue variant selection, and option randomization — using validated 1.1.0 content from this boundary.
