# Scenario Schema 1.1.0 — Validator Focused Review

**Task ID:** SIM-SCHEMA-11-VALIDATOR-REVIEW-01  
**Date:** 2026-07-30  
**Reviewer role:** Independent production-validation review (read-only)  
**Scope:** Layered validator for content schema `1.1.0` as foundation for `SCENARIO_ENGINE_V2`

---

## Verdict

The 1.1.0 layered validator is a **credible, largely correct foundation**: version dispatch works, Engine V1 execution is blocked for normal 1.1.0 documents, JSON Schema + custom layers collect structured findings, hash stripping matches §18, graph cycle/convergence handling is sound for the vertical slice, and focused tests pass (**82/82**).

It is **not yet ready** to be treated as a complete publication-grade validation foundation under the stated readiness bar (`blocker = 0` and `high = 0`).

| Readiness gate | Result |
|---|---|
| Engine V2 implementation readiness | **NOT READY** (unresolved HIGH > 0) |
| Full CB-SC-001 publication readiness | **NOT READY** |
| Vertical-slice / Engineering V2 prototyping | **Conditionally usable** after fixing HIGH findings below |

**Totals:** 0 BLOCKER · **3 HIGH** · 8 MEDIUM · 5 LOW · 4 NOTE

---

## Findings

### VR-H-001 — Non-finite state values accepted

| Field | Value |
|---|---|
| **Severity** | HIGH |
| **Location** | `utils/scenario_validation_v1_1.py` — `_collect_semantic_findings` / `_validate_state_value_bounds` |
| **Evidence** | Independent probe: `initialState.score = float("nan")` → **0 findings**, not blocking. `float("inf")` with **no** `maximum` declared → **0 findings**. `float("inf")` with `maximum: 100` fails only via inequality (`CV-054`), not explicit finite checks. |
| **Impact** | Invalid IEEE values can pass content validation and reach Engine V2 / scoring math with undefined behavior. Normative task requirements and custom-validation intent require finite numeric values and rejection of NaN/Infinity. |
| **Required correction** | Reject non-finite numbers (`math.isfinite`) for `initialState`, state deltas, formula weights, counter bounds/initial values, and band boundaries. Prefer a dedicated rule ID (or map under `CV-054` / new `CV-05x`) with stable JSON paths. Add regression tests for NaN and Inf with and without min/max. |
| **Owner** | Validator |
| **Blocks Engine V2** | Yes |
| **Blocks full CB-SC-001 publication** | Yes |

### VR-H-002 — CV-089 “reachability” is reference presence, not bounded proof

| Field | Value |
|---|---|
| **Severity** | HIGH |
| **Location** | `utils/scenario_validation_v1_1.py` — `_collect_outcome_reachability_findings`; report claim in `SCENARIO_SCHEMA_1_1_0_VALIDATOR_IMPLEMENTATION_REPORT.md` |
| **Evidence** | Implementation marks an outcome “reachable” if it appears in `scoreBands` or cap `forceOutcomeId`/`maxOutcomeId`. Normative spec §14 / custom rule CV-089 require proving reachability via **bounded path / classification analysis**, and **fail closed** when proof is not established. Covering bands alone do not prove a composite score (or forced-cap path) can actually occur under authored deltas, caps, and guards. |
| **Impact** | Publication can accept outcomes that are never achievable. Naming/report language overstates “reachability.” Acceptable interim for tiny fixtures; insufficient for full CB-SC-001. |
| **Required correction** | Either (a) implement bounded analysis (explore scored paths / classification outcomes up to `maxScoredDecisions` / configured path bound) and fail closed when incomplete, or (b) rename the check to “outcome reference coverage,” keep fail-closed for unreferenced outcomes, and document an explicit deferred CV-089 proof gate before full CB-SC-001 publish. Do not claim full reachability until (a) ships. |
| **Owner** | Validator + publication policy |
| **Blocks Engine V2** | Yes (under readiness: unresolved HIGH) |
| **Blocks full CB-SC-001 publication** | Yes |

### VR-H-003 — `schema_version` override bypasses the explicit 1.1.0 execution guard

| Field | Value |
|---|---|
| **Severity** | HIGH |
| **Location** | `utils/scenario_schema.py` — `_resolve_document_schema_version`, `build_scenario_content` |
| **Evidence** | `_resolve_document_schema_version` prefers the caller `schema_version` argument over `document["schemaVersion"]`. Probe: document with `"schemaVersion": "1.1.0"` + `build_scenario_content(..., schema_version="1.0.0")` does **not** raise the V2-required `ScenarioContentError`; it enters the 1.0.0 validation path and fails with 1.0.0 schema errors (e.g. missing `endings`). |
| **Impact** | Direct API callers can suppress the intentional Engine V1 guard. Normal catalog/`load_scenario_content` paths pass `schema_version=None` and remain safe. Hybrid/malicious override paths are confusing and violate “direct calls cannot bypass the guard.” |
| **Required correction** | For execution builders (`build_scenario_content` / `load_scenario_content`), gate on **document-declared** `schemaVersion` (and reject 1.1.0) before applying any override. Optionally reject overrides that disagree with the document. Keep validation-dispatch overrides only where intentional and documented. |
| **Owner** | `scenario_schema` API |
| **Blocks Engine V2** | Yes |
| **Blocks full CB-SC-001 publication** | No (load-path issue) |

---

### VR-M-001 — Finding sort layer order disagrees with pipeline / contract

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **Location** | `utils/scenario_validation_findings.py` — `_LAYER_ORDER` |
| **Evidence** | Sort order: `structural` → **`graph` → `semantic`** → `publication`. Execution and custom-validation companion: structural → **semantic → graph** → publication. |
| **Impact** | Deterministic, but first raised error via `findings[0]` may not match intended layer priority. Confuses operators and tests that assume contract order. |
| **Required correction** | Set `_LAYER_ORDER` to `json_schema < structural < semantic < graph < publication < runtime`. |
| **Owner** | Findings module |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

### VR-M-002 — Later layers always run after structural/JSON Schema failures

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **Location** | `validate_v1_1_scenario_document` |
| **Evidence** | All layers always execute. Missing scenes/routing still enter graph/semantic helpers (generally guarded by `_as_mapping` / empty skips). |
| **Impact** | No observed crash on probes, but noisy duplicates and avoidable work on deeply broken documents. |
| **Required correction** | Optionally short-circuit graph/publication (or all post-JS) when prior layers emit blockers; or document intentional collect-all policy. |
| **Owner** | Validator |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

### VR-M-003 — Publication PB rules CV-007 / CV-008 not implemented

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **Location** | `_collect_publication_findings`; companion `SCENARIO_SCHEMA_1_1_0_CUSTOM_VALIDATION.md` D.1 |
| **Evidence** | No catalog-scoped duplicate `simulationId`+`version` check; no immutable published-version mutation check. Current local catalog architecture has limited publication mutation surface. |
| **Impact** | Incomplete publication contract vs companion. Acceptable until a real publish pipeline exists; must land before production publish. |
| **Required correction** | Implement when catalog/publish boundary supports them; otherwise mark deferred in companion with explicit owner. |
| **Owner** | Catalog/publication |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | Yes |

### VR-M-004 — State variable `minimum` > `maximum` not rejected as declaration error

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **Location** | `_collect_semantic_findings` / `_validate_state_value_bounds` |
| **Evidence** | Inverted bounds only surface indirectly when `initialState` fails `CV-054`. Declaration itself is not rejected. |
| **Impact** | Authors can publish contradictory clamp metadata if initial happens to satisfy neither or both checks inconsistently. |
| **Required correction** | Reject when both bounds present and `minimum > maximum` (and same for counters). |
| **Owner** | Validator |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | Yes |

### VR-M-005 — Duplicate `setFlags` / `clearFlags` entries not rejected

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **Location** | option flag loops in `_collect_semantic_findings` |
| **Evidence** | Probe duplicating an authorized `setFlags` entry produced no flag-related finding. |
| **Impact** | Ambiguous clear-before-set / writer intent; weaker than task “duplicate set or clear references are rejected where prohibited.” |
| **Required correction** | Reject duplicate flag IDs within `setFlags` and within `clearFlags` per option (still allow set+clear of same flag if contract permits). |
| **Owner** | Validator |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

### VR-M-006 — Publication mode does not require `canonicalContentSha256`

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **Location** | `_collect_publication_findings` |
| **Evidence** | `publication=True` with absent hash → no blocking findings (hash checked only when present). |
| **Impact** | Callers can “publish-validate” draft docs without a digest. Mismatch still fails closed when hash is present (`PB-HASH`) — good. |
| **Required correction** | Require non-empty matching hash for `validate_*_for_publication` (keep draft validation on non-publication path). |
| **Owner** | Publication API |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | Yes |

### VR-M-007 — Cyclic graphs still compute path bounds that fall back to length `1`

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **Location** | `_compute_scored_path_bounds` |
| **Evidence** | In-progress memo avoids infinite recursion (good). Pure-cycle / self-loop cases can yield `min_path/max_path = 1` after memo sentinel, potentially emitting misleading `CV-074`/`CV-075` alongside `CV-072`. |
| **Impact** | No acceptance of cycles (CV-072 fires). Noise and weaker bound semantics when cycles present. |
| **Required correction** | Skip path-bound checks when cycles detected, or treat unbounded/cyclic paths as fail-closed for CV-074/075 without fake length 1. |
| **Owner** | Graph validator |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

### VR-M-008 — Top-level API lacks an unambiguous schema-only mode

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **Location** | `scenario_schema` public API |
| **Evidence** | `collect_v1_1_json_schema_findings` exists but is not exposed via `validate_scenario_document` / catalog helpers as a first-class mode. Full custom layers always run for 1.1.0. |
| **Impact** | Callers cannot request schema-only vs full validation without importing internals. |
| **Required correction** | Add `mode=` / `layers=` or document that schema-only is internal-only. |
| **Owner** | API |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

---

### VR-L-001 — Raise-on-error surfaces only `findings[0]`

| Field | Value |
|---|---|
| **Severity** | LOW |
| **Location** | `validate_scenario_document`, `validate_scenario_for_publication`, `assert_catalog_scenario_valid` |
| **Evidence** | Multi-finding collection exists; raise APIs discard the rest. Currently all emitted severities are `blocker`/`high`, so `findings[0]` is blocking in practice. |
| **Impact** | Latent footgun if advisory findings are added later with earlier sort keys. |
| **Required correction** | Raise the first finding with severity in `{blocker, high}` after sort. |
| **Owner** | API |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

### VR-L-002 — Stage ID uniqueness / option-display content checks thin

| Field | Value |
|---|---|
| **Severity** | LOW |
| **Location** | structural/semantic layers |
| **Evidence** | No custom unique-`stageId` check observed; option display policy largely relies on JSON Schema enum. |
| **Impact** | Minor gap vs exhaustive registry list in the task brief. |
| **Required correction** | Add `stageId` uniqueness if stages present; keep display-policy as schema + light semantic checks. |
| **Owner** | Validator |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

### VR-L-003 — 1.0.0 catalog load double-validates

| Field | Value |
|---|---|
| **Severity** | LOW |
| **Location** | `load_resolved_scenario_content` |
| **Evidence** | `assert_catalog_scenario_valid` then `load_scenario_content` → `_validate_and_compute_graph_metadata` again for 1.0.0. |
| **Impact** | Extra cost only; semantics OK. |
| **Required correction** | Optional later optimization. |
| **Owner** | Catalog |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

### VR-L-004 — Module size / maintainability (~2100 lines)

| Field | Value |
|---|---|
| **Severity** | LOW |
| **Location** | `utils/scenario_validation_v1_1.py` |
| **Evidence** | Clear layer functions exist (`_collect_*_findings`); still one large file with repeated registry walks. |
| **Impact** | Reviewability cost; not a correctness defect. |
| **Required correction** | Split by layer only when HIGH fixes force deeper edits. |
| **Owner** | Maintainers |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

### VR-L-005 — Some tests assert rule ID presence without path precision

| Field | Value |
|---|---|
| **Severity** | LOW |
| **Location** | `tests/test_scenario_validation_v1_1.py` |
| **Evidence** | Many cases use `findings_with_rule(...); assertTrue(...)` without asserting JSON Pointer / identifier. Stronger cases exist (guard message, hash, ordering). |
| **Impact** | Regressions could change paths unnoticed. |
| **Required correction** | Strengthen high-value cases (graph targets, hash, NaN, publication) with path assertions. |
| **Owner** | Tests |
| **Blocks Engine V2** | No |
| **Blocks full CB-SC-001 publication** | No |

---

### VR-N-001 — Lazy import is a sound circular-import fix

`scenario_schema` lazy-imports `scenario_validation_v1_1` inside dispatch functions. v1_1 no longer imports schema at module import for `REPO_ROOT`/`load_json_document`. No initialization defect observed.

### VR-N-002 — Engine V1 catalog execution path is blocked for normal 1.1.0 content

`build_scenario_content` / `load_scenario_content` raise for document `schemaVersion == "1.1.0"`. `load_resolved_scenario_content` validates then loads; valid 1.1.0 cannot become `ScenarioContent`. Invalid 1.1.0 fails closed at assert. No cache bypass found.

### VR-N-003 — Hash procedure matches §18 for reviewed cases

Independent recomputation of `compute_canonical_content_sha256_v1_1` matched stripped deepcopy + `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))` + UTF-8 SHA-256 lowercase. Input not mutated. 1.0.0 `compute_canonical_content_sha256` untouched.

### VR-N-004 — Graph convergence and condition bounds behave as required in probes

Legal diamond convergence: no blocking findings. Depth > 8 → `CV-032`. Node count > 64 → `CV-033`. Self-loop / cycle → `CV-072` without hang after memo fix.

---

## Area-by-area results

### 1. Version dispatch — PASS with note

| Case | Result |
|---|---|
| `1.0.0` → legacy path | Pass |
| `1.1.0` → layered path | Pass |
| Unsupported → fail closed | Pass (`CV-001`) |
| Absent → defaults to `1.0.0` | Fail-closed via 1.0.0 schema (safe) |
| Silent fallback | None observed |
| Lazy import | Sound (VR-N-001) |
| Override hazard | **VR-H-003** |

### 2. Engine V1 execution guard — PASS for default paths; HIGH on override

Default/`None` override paths safe. Catalog cannot return executable 1.1.0 `ScenarioContent`. See **VR-H-003**.

### 3. Validator API — PASS with MEDIUM gaps

Clear split: collect vs raise; publication flag includes lower layers for 1.1.0. Schema-only mode not first-class (**VR-M-008**). Raise surfaces one finding (**VR-L-001**).

### 4. Structured findings — PASS

Frozen dataclass; stable fields; deterministic sort; JSON Pointer paths; no stack traces in messages. Sort layer order bug: **VR-M-001**.

### 5. JSON Schema layer — PASS

Draft 2020-12; cached schema; local `$defs`; all errors collected; no mutation / no default injection confirmed by tests + probes.

### 6. Layer execution order — PASS with MEDIUM notes

Runs JS → structural → semantic → graph → publication. Sort order mismatch **VR-M-001**; always-run later layers **VR-M-002**.

### 7. ID / reference validation — PASS with LOW gaps

Core registries covered. Learner forbidden in registry via JSON Schema; custom layer covers `charactersPresent` / speakers. Stage uniqueness thin (**VR-L-002**).

### 8. Dialogue — PASS

Priorities (`CV-044`), override targets (`CV-045`), duplicate overrides (`CV-046`), learner speaker allowed, unknown speaker blocked. Aligns with companion (note: earlier informal notes swapped CV-044/045; **implementation matches companion**).

### 9. Condition — PASS

Depth/node limits enforced; leaf refs resolved; recursion safe in probes; empty groups primarily via JSON Schema.

### 10. Graph — PASS with MEDIUM on bounds-after-cycle

Targets, reachability, cycles, corrective topology, path policy checks present. Memo placeholders stop recursion. Legal convergence accepted. Cyclic bound fallback: **VR-M-007**.

### 11. Corrective budget — PASS

CV-071, counter resolution, no re-branch metadata, skip/reconvergence equality, slice policy 1/1 and production 5/3 distinguished in spec (SPEC-05). Validator semantics unchanged.

### 12. Flags — PASS with MEDIUM on duplicates

Unknown refs and authorization work. Duplicate set/clear: **VR-M-005**.

### 13. State / counters — PARTIAL (HIGH on finite)

Separation, unknown refs, initial bounds mostly good. Non-finite: **VR-H-001**. min>max declaration: **VR-M-004**. Bool counter initial rejected by JSON Schema in probe.

### 14. Formula — PASS for shipped checks

Four types handled; linear_blend ±1e-9; dependency cycle detection; variable refs. Limited identity/tier_average extra checks (schema carries much of shape).

### 15. Outcome — PARTIAL (HIGH on reachability proof)

Bands, ranks, cap/guard refs solid. Reachability proof: **VR-H-002**.

### 16. Hash — PASS

Matches §18 exclusions and serialization. 1.0.0 unchanged.

### 17. Publication — PARTIAL

Engine allow-list + optional hash verify + outcome reference coverage. Missing required hash, CV-007/008: **VR-M-003**, **VR-M-006**.

### 18. Catalog integration — PASS

Validate-before-load; 1.0.0 still works; 1.1.0 cannot execute via load; no DB coupling.

### 19. Maintainability — ACCEPTABLE

Layered helpers present; file large but coherent (**VR-L-004**).

### 20. Performance / DoS — PASS for configured bounds

Condition depth/node caps apply. Graph DFS + memoized path bounds. Schema cached. No hang on cycle probes.

### 21. Test quality — GOOD with gaps

Rule IDs asserted widely; mutation/guard/ordering/hash covered. Missing NaN/Inf, override-guard, required publication hash, true reachability, path assertions (**VR-L-005**, gaps for HIGHs).

### 22. Independent checks executed

| Check | Result |
|---|---|
| Focused pytest suite | **82 passed** |
| Hash determinism + no mutation | Pass |
| Findings order stability | Pass |
| Deep/wide conditions | `CV-032` / `CV-033` |
| Legal convergence | Pass |
| NaN / Inf (no max) | **Accepted — VR-H-001** |
| Bool counter | Rejected by JS |
| schema_version override | Guard bypassed — **VR-H-003** |
| Vslice raw (post SPEC-05) | Pass, experienced=1 |
| Unsupported version | Fail closed |

### 23. Backward compatibility — PASS

`test_scenario_schema.py` + `test_scenario_catalog.py` included in 82 passing. 1.0.0 hash/graph semantics not altered by review probes.

### 24. Scope / safety — PASS

No Engine V2 runtime, persistence, migrations, auth/billing/deploy changes in this review. Protected paths not inspected. Only this review file created for the task.

---

## Recommended correction sequence

1. **VR-H-001** — finite-number rejection + tests  
2. **VR-H-003** — document-declared schemaVersion execution guard  
3. **VR-H-002** — bounded outcome reachability proof **or** honest deferral + rename until full CB-SC-001  
4. **VR-M-001** — fix `_LAYER_ORDER`  
5. **VR-M-004 / VR-M-005 / VR-M-006 / VR-M-007** — bounds declaration, duplicate flags, required publish hash, cycle/path interaction  
6. **VR-M-003** — CV-007/008 when publish pipeline exists  
7. Strengthen tests for the above  

## Recommended next action

Do **not** start SCENARIO_ENGINE_V2 against this validator as a publication-complete foundation until **VR-H-001**, **VR-H-002**, and **VR-H-003** are closed (or VR-H-002 explicitly deferred with renamed semantics and a tracked publication gate).

After those HIGH fixes, re-run this review checklist and the focused pytest command; then Engine V2 runtime work can begin with the validator as the content gate.

---

## Review process notes

- Review-only: no source/test/schema/spec modifications.  
- Temporary probes were in-memory Python only; no persistent temp files left.  
- Passing tests were not treated as proof; critical behaviors were independently exercised.
