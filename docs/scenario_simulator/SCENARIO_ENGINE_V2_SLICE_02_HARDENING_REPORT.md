# SCENARIO_ENGINE_V2 Slice 02 — Hardening Report

**Task ID:** SIM-ENGINE-V2-02  
**Model:** Composer 2.5 Fast  
**Baseline:** SIM-ENGINE-V2-01 + focused review `SCENARIO_ENGINE_V2_SLICE_01_FOCUSED_REVIEW.md`  
**Date:** 2026-07-31  

## Verdict

**HIGH findings closed.** Remaining blockers: **0**. Remaining HIGH: **0**.  
Natural corrective-budget exhaustion is proven end-to-end with replay. Persistence/UI/dispatcher work was not performed.

---

## Finding dispositions

| ID | Severity | Disposition |
|---|---|---|
| F-H-001 | HIGH | **Closed** — strict `type(x) is int` for sequence; rejects True/False/1.0/"1"/None/negatives |
| F-H-002 | HIGH | **Closed** — `authored_order`, `randomize_per_attempt_scene`, unsupported fail-closed |
| F-M-001 | MEDIUM | **Closed** — frozen Engine V2 §17 stream documented + golden vector |
| F-M-002 | MEDIUM | **Closed** — NaN/Inf/bool rejected via `_require_finite_number` |
| F-M-003 | MEDIUM | **Closed** — undeclared clear/set flags fail closed |
| F-M-005 | MEDIUM | **Closed** — `presented_dialogue_variant_id` + `next_dialogue_variant_id` |
| F-M-006 | MEDIUM | **Closed** — natural R2A then C03→skip R3A path + exact replay |

---

## Seed-contract decision (F-M-001)

Spec §17 text is ambiguous about whether the first digest is bare `SHA256(material)` or `SHA256(material || counter)`.

**Engine V2 freezes (does not invent a new algorithm):**

```
material = UTF-8(attemptId + "\n" + simulationId + "\n" + version + "\n" + canonicalContentSha256 + "\n" + sceneId)
for counter = 0, 1, 2, …:
    yield bytes of SHA256(material || uint32be(counter))
Fisher–Yates with rejection-sampled uniform byte draws
```

This matches the Slice-01 implementation already locked by tests. Spec prose should later be amended to this reading for multi-runtime alignment. `PYTHONHASHSEED` cannot affect results (no Python `hash()` of strings).

**Golden vector:**

| Input | Value |
|---|---|
| option ids | `opt-a`, `opt-b`, `opt-c` |
| attemptId | `golden-attempt` |
| simulationId | `golden-sim` |
| version | `1.0.0` |
| canonicalContentSha256 | `0123456789abcdef` × 4 |
| sceneId | `SC-GOLDEN` |
| **expected order** | `("opt-b", "opt-c", "opt-a")` |

---

## Fixture changes

`tests/fixtures/scenario_engine_v2_vslice_1_1_0.json`:

- `maxAvailableCorrectiveScenes`: 2; `maxExperiencedCorrectiveScenes`: 1; `maxScoredDecisions`: 5; `minScoredDecisions`: 4
- `SC001-C03` now routes to `SC001-C04`; suboptimal/high-risk options may enter `SC001-R3A` or skip to C04
- Added corrective `SC001-R3A` and terminal `SC001-C04`
- Remains schema 1.1.0 + layered-validator valid (loads via `build_scenario_content_v2`)

Natural exhaustion path:

`C01-a → C02-b → R2A-a → C03-b (skip R3A) → C04-a`

---

## Debrief variant contract (F-M-005)

`DebriefTraceEntry`:

- `presented_dialogue_variant_id` — variant shown in the scene of the decision
- `next_dialogue_variant_id` — variant selected for the next scene (`None` if terminal / base dialogue)

Removed ambiguous `selected_variant_id`.

---

## Tests

- Engine V2 suite: **101 passed** (was 81; +hardening coverage; fixture path updates)
- Combined focused regression: **271 passed**
- Cross-process option order: identical across two spawned processes

---

## Non-goals confirmed

Persistence modified: **No**  
Database migration: **No**  
UI work: **No**  
Compiler work: **No**  
Central dispatcher: **No**  
Protected paths: **untouched**  
Nothing staged/committed/pushed/deployed

---

## Remaining risks / next step

- Spec §17 prose still differs literally from the frozen stream — amend the normative doc in a later schema/docs task.
- Persistence layer should still pair `verify_replay_identity_v2` with replay and decide §11.3 idempotency (F-M-004 / F-M-007 from prior review, out of scope here).

**Recommended next step:** Persistence/resume integration design against the hardened Engine V2 API.
