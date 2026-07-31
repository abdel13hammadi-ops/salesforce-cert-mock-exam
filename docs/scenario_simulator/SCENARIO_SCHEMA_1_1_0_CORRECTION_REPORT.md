# Schema 1.1.0 Specification Correction Report

**Task ID:** SIM-SCHEMA-11-SPEC-02  
**Corrected file:** `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md` (revision 2)  
**Adversarial input:** `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_ADVERSARIAL_REVIEW.md`  
**Date:** 2026-07-30  
**Scope:** Documentation only — no executable JSON Schema, runtime, compiler, migrations, or staging/commit/push/deploy.

---

## Summary

Every **BLOCKER** (ADV-B-001…006) and **HIGH** (ADV-H-001…011) finding from SIM-SCHEMA-11-REVIEW-01 was closed by rewriting the normative specification. Selected MEDIUM items that would leave the contract inconsistent were also closed (legacy forbid completeness, debrief strongestOptionId validation, classificationTrace storage note, fingerprint independence).

**Remaining blockers:** 0  
**Remaining unresolved highs:** 0  
**Executable JSON Schema readiness:** Ready for a confirmation re-review (SIM-SCHEMA-11-REVIEW-02), then JSON Schema authoring.

---

## ADV-B dispositions

### ADV-B-001 — Corrective counter order — CLOSED
- Single normative order in §11.3: apply state/flags → increment tier counters → resolve routing with **pre-entry** corrective count → increment `correctiveScenesExperienced` **only on committed corrective entry** → record skip without increment → environmental flags → variants.
- Contradictory pre-routing corrective increments removed.
- Counters stored in sibling `counters` object, not float `state`.
- Skipped-corrective audit event defined on decision/snapshot; replay recomputes and may verify.

### ADV-B-002 — Canonical hash — CLOSED
- §18: hash `D'` with `canonicalContentSha256`, `contentProvenance`, and `publicationMetadata` **omitted**.
- Normalization matches repository `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))` UTF-8 SHA-256 lowercase hex.
- Circular embedding forbidden; publisher recomputes and compares.

### ADV-B-003 — Empty condition groups — CLOSED
- `"all": []` and `"any": []` **FORBIDDEN**.
- Structural fallback = base `dialogue.exchanges`; no empty-condition fallback variant.
- Min children 1; max depth 8; max nodes 64.
- Example updated (combined multi-flag variant uses nonempty `all`).

### ADV-B-004 — C03 self-loop — CLOSED
- C03 is an executable core scene with three options routing to `EVALUATE_ENDING`.
- No self-loop; cycle validation unchanged.
- Slice remains a complete minimal valid fixture with classifier + four outcomes.

### ADV-B-005 — Legacy/new coexistence — CLOSED
- §4.3 exclusive table: on 1.1.0, `routing`, `evaluationTier`, `dialogue`, `outcomeClassifier`+`outcomes[]` required; `nextScene`, `isCorrect`, `narrative`, `endings[]` **FORBIDDEN**.
- No runtime dual-read; compiler emits 1.1.0 form only.
- `isDetour` retained presentation-only with consistency check.

### ADV-B-006 — optionTierInCurrentDecision — CLOSED
- Removed from grammar entirely.
- Corrective eligibility via option-owned `correctiveRoute.triggerOnTiers` plus separate `budgetCondition`.

---

## ADV-H dispositions

| ID | Disposition |
|---|---|
| ADV-H-001 Engine compat | Exact `requiredEngineVersion` only; `supportedEngineVersions` removed |
| ADV-H-002 Persistence | Explicit **application snapshot change**; sibling `counters`; no DB migration |
| ADV-H-003 Cap ranks | Prefer `maxOutcomeId`; CAP-P03 example uses `partial_resolution` |
| ADV-H-004 Formula precision | float64; linear_blend weights sum to 1±1e-9; missing inputs reject; round after classify |
| ADV-H-005 Variable weights | `outcomeWeight` removed; weights only in formulas |
| ADV-H-006 Variant priority | Unique priorities required; no mutual-exclusivity exception |
| ADV-H-007 Multi-flag | First-match; compilers MUST emit combined variants (example includes `c03-both-flags`) |
| ADV-H-008 Env flags | MUST apply on entry before variant selection |
| ADV-H-009 Condition DoS | Depth ≤8, nodes ≤64 |
| ADV-H-010 Domains | If any `domainId`, `domains[]` MUST declare it |
| ADV-H-011 Shuffle | Fisher–Yates + SHA256 stream; seed includes content hash; store `optionDisplayOrder` in snapshot |

---

## Key decisions (normative)

| Topic | Decision |
|---|---|
| Counter semantics | §11.3 single order; increment corrective only on committed entry |
| Counter storage | Snapshot sibling `counters` |
| Skipped corrective | Server-generated event; no counter increment; replay-derived + optional verify |
| Canonical hash | Exclude digest + provenance + publication metadata |
| Legacy/new | Forbid behavioral legacy fields on 1.1.0 |
| Engine | Exact string pin |
| Dialogue variants | Base exchanges = fallback; unique priorities; overrides replace fields only |
| Conditions | Nonempty groups; no tier leaf |
| Formulas | Bounded types; weight sum; float64 |
| Routing | `triggerOnTiers` + `budgetCondition`; terminal XOR correctiveRoute |
| Flags | Runtime vs validation vs advisory metadata split |
| State | float64; no variable-level outcome weights |
| Classifier | Seven-step; `maxOutcomeId`; fail-closed reachability |
| Randomized display | Server shuffle; store order in snapshot; submit optionId |
| Persistence | No DB migration; application jsonb contract changes |
| Validation ownership | §21 classification table |
| Replay | §19.3 includes pins, counters, skips, variants, display order, classifier |
| Security | Client submits optionId/sequence only; server resolves hidden fields |
| Vertical slice | Valid terminating C03; no self-loop |

---

## Validation checklist

1. All six ADV-B closed — **Yes**  
2. All eleven ADV-H closed — **Yes**  
3. No contradictory counter increment — **Yes**  
4. Counters separate from numeric state — **Yes**  
5. Skipped corrective does not increment — **Yes**  
6. Hash noncircular — **Yes**  
7. Empty all/any invalid — **Yes**  
8. optionTierInCurrentDecision removed — **Yes**  
9. C03 self-loop removed — **Yes**  
10. Legacy/new explicit — **Yes**  
11. 1.0.0 unchanged — **Yes**  
12. Dialogue fallback/priority deterministic — **Yes**  
13. Formula precision/missing-input defined — **Yes**  
14. Terminal/routing consistent — **Yes**  
15. Randomized order deterministic + auditable — **Yes**  
16. No DB migration — **Yes**  
17. Application snapshot changes acknowledged — **Yes**  
18. Static validation classified — **Yes**  
19. Illustrative JSON internally valid per revised rules — **Yes**  
20. No executable JSON Schema created — **Yes**  
21. No runtime implementation — **Yes**  
22. Protected paths untouched — **Yes**  
23. Nothing staged/committed/pushed/deployed — **Yes**

---

## Remaining risks (non-blocking)

- Confirmation re-review should spot-check hash wrapper wording vs future code (`D'` strip before existing helper).  
- Full CB-SC-001 product content still needs compiler + engine V2 (out of scope).  
- Outcome reachability “fail closed” may be strict for very large future graphs — acceptable for 1.1.0 bounded scenarios.  
- MEDIUM/LOW items not fully rewritten (e.g. free-string `communicationType`) remain deferred without blocking JSON Schema shape.

---

## Recommended next action

**SIM-SCHEMA-11-REVIEW-02** — short confirmation adversarial pass against revision 2.  
If zero blockers and zero unresolved highs: **SIM-SCHEMA-11-JSON-01** — author `scenario_content/schemas/1.1.0/simulation.schema.json` + custom-validation companion.
