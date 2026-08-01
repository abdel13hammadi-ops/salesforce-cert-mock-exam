# SCENARIO_ENGINE_V2 Persistence Adapter — Focused Production-Readiness Review

**Task ID:** SIM-PERSIST-V2-04-REVIEW-01
**Model:** Auto
**Baseline HEAD:** `6136673` — Complete Scenario Engine V2 vertical slice
**Review scope:** Review-only. No source, test, SQL, migration, UI, or orchestration changes. No database connection. Nothing staged, committed, pushed, or deployed.

---

## Readiness decision

**CORRECTIONS_REQUIRED**

| Metric | Value |
|---|---|
| Blockers | **2** |
| Remaining HIGH findings | **2** (same as blockers; no additional open HIGHs) |
| New HIGH findings | **2** |
| Focused tests | **447 passed** |
| Exact envelope field contract frozen? | **Yes — frozen in this review; implementation does not yet match it** |
| Replay authoritative? | **Yes** (implementation correct once envelope keys are fixed) |
| RPC param *names* match V69? | **Yes** (7 start keys / 13 submit keys) |
| RPC envelope *contents* compatible with V69 SQL? | **No** — missing SQL-required `version` and `isComplete` |
| Engine V1 isolated? | **Yes** |
| Source/tests modified by this review? | **No** |

Orchestration must not begin until the two blockers below are corrected and re-tested.

---

## Executive summary

The adapter is a strong pure-Python foundation: decisionHistory projection, canonical-row replay authority, identity fail-closed checks, strict int/bool/nonfinite handling, learner-safe views, and Engine V1 isolation are substantially correct.

It is **not** ready for start/resume orchestration because the persisted envelope shape diverges from the **load-bearing** V68/V69 SQL contract and from the reviewed Slice A / schema §19.2 envelope:

1. Emits `scenarioVersion` instead of required `version`.
2. Emits string `status` instead of required boolean `isComplete`.

Independent probes confirmed both keys are absent from a live serialized start envelope. Calling `start_or_resume_scenario_attempt_v1` or `submit_scenario_decision_v1` with this envelope would fail identity/lifecycle validation inside PostgreSQL.

---

## Frozen envelope contract (authoritative going forward)

Resolve the `scenarioVersion` / `version` discrepancy **now**, before orchestration.

### Single authoritative field name for content version

**Freeze: `version`**

| Source | Field |
|---|---|
| `SCENARIO_SCHEMA_1_1_0_SPEC.md` §19.2 | `version` |
| `SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md` §7 | `version` |
| `SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md` §9 | `version` |
| V68 + V69 SQL (`p_initial_serialized_state->>'version'`, state identity checks) | `version` |
| Implementation + SIM-PERSIST-V2-04 task prose | `scenarioVersion` (**non-authoritative; must change**) |

SQL is decisive: the validated RPCs reject missing/mismatched `version`. The task prose that listed `scenarioVersion` is overridden by the reviewed contracts and the installed SQL.

### Lifecycle completeness field

**Freeze: `isComplete` (JSON boolean)** — not string `status`.

Evidence: Slice A §9, schema §19.2, V68/V69 lifecycle checks require `isComplete` boolean (`false` on start / non-terminal; `true` on terminal `state_after`).

### Optional-field decision

**Freeze: `routingResolutions` and `selectedVariantIdByScene` are mandatory keys on every envelope.**

- Always emit them (empty `[]` / `{}` when nothing to report).
- This matches the design doc §23 deferred Slice-D choice (“made mandatory … for simpler validation”) and does **not** contradict SQL (SQL does not inspect these keys).
- Spec §19.2 “optional” means they may be absent from older/illustrative shapes; for Engine V2 envelopeVersion 1 they are required present and well-typed.

### Exact envelopeVersion 1 key set (frozen)

Align with Slice A §9 + schema §19.2 + SQL seven-key identity/lifecycle surface:

```text
envelopeVersion
simulationId
version                          # NOT scenarioVersion
schemaVersion
canonicalContentSha256
engineVersion
currentSceneId
expectedSequenceNumber
isComplete                       # NOT status
state
counters
flags
decisionHistory                  # exactly {sequenceNumber, sceneId, optionId}[]
routingResolutions               # mandatory key; may be []
optionDisplayOrderByScene
selectedVariantIdByScene         # mandatory key; may be {}
terminalResult
```

**Also freeze:**

| Key | Decision | Rationale |
|---|---|---|
| `attemptId` | **Exclude** from envelope | Design §7 deliberately excludes it; Slice A §9 example excludes it; attempt identity lives in DB column + Engine runtime. Replay must use trusted `attempt_row_id`. |
| `decisionCount` | **Exclude** | Not in Slice A §9 / schema §19.2; derivable as `len(decisionHistory)`. |
| `status` | **Exclude** | Replaced by `isComplete`. |
| `scenarioVersion` | **Exclude** | Replaced by `version`. |

SQL seven keys that **must** remain present and correctly typed:

`simulationId`, `version`, `canonicalContentSha256`, `engineVersion`, `currentSceneId`, `isComplete`, `terminalResult`.

---

## Findings

### BLOCKER-01 — Envelope missing SQL-required `version` (uses `scenarioVersion`)

- **Severity:** BLOCKER / HIGH
- **Area:** Snapshot envelope / Start RPC / Submit RPC
- **Evidence:**
  - Probe P1: serialized envelope has `scenarioVersion`, not `version`.
  - V69 migration checks `p_initial_serialized_state->'version'` (string, trimmed, must match pinned `scenario_versions.version`).
  - V68 submit path compares `state_before->>'version'` / `state_after->>'version'` for identity.
  - Slice A §9 and schema §19.2 name the field `version`.
- **Impact:** Every start and every decision submit built from this adapter will fail SQL identity validation before any row is accepted. Two active “contracts” (task prose / implementation vs reviewed SQL+Slice A) would produce incompatible persisted envelopes.
- **Required correction:** Rename envelope field to `version`; update dataclass attribute naming as needed; reject unknown `scenarioVersion` as unexpected; add a regression test that asserts SQL seven-key presence including `version`.

### BLOCKER-02 — Envelope missing SQL-required `isComplete` (uses string `status`)

- **Severity:** BLOCKER / HIGH
- **Area:** Snapshot envelope / Start RPC / Submit RPC / Terminal state
- **Evidence:**
  - Probe P1: envelope has `status: "in_progress"`, no `isComplete`.
  - V69 start requires `isComplete` JSON boolean `false`.
  - V68 submit requires `state_before.isComplete == false`, and `state_after.isComplete` true/false consistent with terminal/non-terminal.
  - Slice A §9 / schema §19.2 use `isComplete`.
- **Impact:** Start fails `invalid_initial_state_lifecycle`; submit fails `state_lifecycle_mismatch`. Terminal vs active lifecycle cannot be expressed in the form SQL validates.
- **Required correction:** Replace `status` with boolean `isComplete` derived from `run.is_complete`; keep terminalResult null/non-null rules tied to `isComplete`; update deserialize validation and tests.

### MEDIUM-01 — RPC response parsers retain nested aliases into the response payload

- **Severity:** MEDIUM
- **Area:** Immutability / RPC response parsing
- **Evidence:** Probe P3 — after `parse_start_or_resume_rpc_response_v2`, mutating `parsed.serialized_engine_state['state'][key]` also mutated the original input `env['state']` because `_row_json_object_field` / `_row_nullable_json_object_field` only shallow-copy via `dict(value)`.
- **Impact:** Future orchestration that mutates parsed envelopes (even accidentally) can corrupt cached RPC response objects or shared fixtures; contradicts the module’s “parsing outputs do not retain aliases” claim.
- **Required correction:** Deep-copy nested JSON objects/lists on parse (or freeze with recursive thaw into new structures).

### MEDIUM-02 — `serialize_run_snapshot_v2` does not canonicalize `attemptId` to lowercase

- **Severity:** MEDIUM
- **Area:** UUID handling
- **Evidence:** Probe P7 — with uppercase `attempt_id` passed to Engine V2, envelope emitted uppercase `attemptId`; `p_attempt_id` was lowercased; deserialize later canonicalizes.
- **Impact:** CAS / fingerprint / SQL whole-envelope equality can diverge solely due to UUID case if one side canonicalizes and the other does not. (Becomes moot if `attemptId` is removed per frozen contract; if kept, must canonicalize on serialize.)
- **Required correction:** Prefer removing `attemptId` (frozen contract). If retained temporarily, canonicalize via UUID parse on serialize.

### MEDIUM-03 — Public serializers leak raw `AttributeError` on wrong-typed inputs

- **Severity:** MEDIUM
- **Area:** Domain exception contract
- **Evidence:** Probe P12 — `serialize_decision_input_v2(None)`, `serialize_run_snapshot_v2(None)`, `serialize_learner_scene_view_v2(None)` all raise raw `AttributeError`. Module docstring claims no `AttributeError` escapes public APIs.
- **Impact:** Low in typed orchestration paths; still a contract breach and a noisy failure mode for misuse.
- **Required correction:** Guard public entrypoints and wrap into `ScenarioPersistenceV2ValidationError` / `SerializationError`.

### MEDIUM-04 — Test suite encodes the non-authoritative envelope field names

- **Severity:** MEDIUM
- **Area:** Test quality
- **Evidence:** `test_A_snapshot_serialization_succeeds` asserts `scenarioVersion` and `status`; no test asserts presence/type of SQL keys `version` / `isComplete`; tests would stay green while production RPC calls fail.
- **Impact:** Implementation-mirroring coverage hides the blockers.
- **Required correction:** After envelope fix, invert assertions to the frozen contract; add explicit SQL seven-key compatibility tests for start and submit envelopes.

### LOW-01 — Extra envelope keys vs Slice A (`attemptId`, `decisionCount`)

- **Severity:** LOW (elevated only because they expand envelope drift; SQL ignores unknown keys)
- **Area:** Snapshot envelope
- **Evidence:** Slice A §9 example and design §7 exclude `attemptId`; neither lists `decisionCount`. Implementation requires both.
- **Impact:** Not an immediate SQL failure; increases contract surface and invites attempt-id drift inside JSONB.
- **Required correction:** Remove both per frozen contract; drive replay `attempt_id=` from trusted `attempt_row_id`.

### LOW-02 — Module docstring vs deserialize thaw of `MappingProxyType`

- **Severity:** LOW
- **Area:** Strict types
- **Evidence:** Docstring says MappingProxyType is “never accepted on the way in”; `_require_json_object` thaws and accepts it (Probe P13).
- **Impact:** Harmless; documentation inaccuracy only.

### LOW-03 — Unused `_CONTENT_HASH_PATTERN_SOURCE`

- **Severity:** LOW
- Dead constant; no behavioral impact.

---

## Area-by-area results

### 1. Public API

**PASS with notes.** All 12 required functions exist with precise typed outputs for parsers (`StartOrResumeRpcResultV2`, `SubmitDecisionRpcResultV2`, `PersistedRunEnvelopeV2`). `__all__` does not expose private helpers. Responsibilities are clear. Parsers do not return raw Supabase rows as the top-level result object (they return dataclasses), but nested `serialized_engine_state` dicts are shallow-aliased (MEDIUM-01).

### 2. Domain exceptions

**PARTIAL.** Validation/RPC/UUID paths generally wrap into domain errors. Engine V2 `ScenarioReplayV2Error` is intentionally wrapped into `ScenarioPersistenceV2IdentityError` in identity verify; engine replay/state errors are re-raised unchanged from `replay_serialized_run_v2` (consistent with Slice A §8.7). Gap: MEDIUM-03 AttributeError leaks on serializer misuse.

### 3. Snapshot envelope

**FAIL (blockers).** Implementation’s 19-key shape does not match the frozen 17-key Slice A / schema / SQL contract. See BLOCKER-01/02 and LOW-01.

### 4. `scenarioVersion` / `version` decision

**Frozen: `version`.** Implementation currently wrong.

### 5. Optional-field decision

**Frozen: always emit `routingResolutions` + `selectedVariantIdByScene`.** Always-emitting is safe and preferable. No contradiction with SQL.

### 6. decisionHistory

**PASS.** Serializer projects exactly three fields; deserializer rejects extras (`evaluationTier`, `debriefSeed`, `stateDelta`, flags, etc.); runtime `DebriefTraceEntry.evaluation_tier` exists but never enters JSON (probe + tests U–Y). Canonical decision rows drive replay (test Z / probe P9).

### 7. Authoritative vs cached data

**PASS.** `replay_serialized_run_v2` deserializes envelope for verification only; reconstruction uses `replay_scenario_run_v2(content, attempt_id=..., decisions=canonical)`; cache fields compared after recompute; corrupted state/order/counters/history cannot influence reconstruction.

### 8. Identity verification

**PASS (structure).** Uses DB columns for hash/engine version via `verify_replay_identity_v2`; cross-checks envelope copies; attempt id cross-check present. After `attemptId` removal, attempt comparison should be “replay uses `attempt_row_id`” only (envelope copy check removed or replaced).

### 9. Strict types

**PASS for trusted paths.** Bool-as-int rejected; NaN/±Inf rejected; ints exact; state coerced to finite float; flags sorted lists; nested JSON-native assertion on serialize. MappingProxyType thaw on input is permissive (LOW-02).

### 10. UUID handling

**PARTIAL.** Nil rejected on start params; UUIDv4 required for idempotency; malformed UUID → domain error; RPC `p_attempt_id` lowercased. Serialize path does not canonicalize envelope `attemptId` (MEDIUM-02). No collision disclosure observed.

### 11. Cache comparison

**PASS for intended semantics.** Exact Python `==` after normalization; dict key order irrelevant; lists positional; flags sorted; no numeric tolerance (correct for deterministic engine arithmetic). `-0.0 == 0.0` (probe P5). Int/float state inputs normalize to float on deserialize (probe P6). JSONB CAS will see adapter-emitted floats for state after correction; acceptable and consistent with V1 pattern. Duplicate JSON object keys are a parser-layer concern (Python `json` last-wins) — out of adapter scope once payload is a `dict`.

### 12. Option order

**PASS.** Replay recomputes; cache verifies; duplicates rejected at deserialize; unvisited scenes not required (probe P8: only visited/current scenes appear); missing/reordered visited scenes → cache mismatch (test AC). Attempt identity remains pinned via engine `attempt_id` for order algorithm.

### 13. Terminal state

**PASS logically; FAIL wire shape.** Active/completed terminalResult consistency enforced; terminal mismatch distinct error; terminal cache does not feed classifier. Wire shape must use `isComplete` (BLOCKER-02).

### 14. Start RPC params

**PASS key set / FAIL payload.** Exactly seven keys; `p_attempt_id` matches runtime; inputs not mutated; envelope JSON-native. Embedded envelope fails SQL identity/lifecycle (blockers).

### 15. Submit RPC params

**PASS key set / FAIL payload.** Thirteen keys match existing submit RPC; sequence/scene/option server-derived; fingerprint stable via reused `compute_request_fingerprint` (probe P10); no client hidden-value parameters. `p_state_before` / `p_state_after` lack `version`/`isComplete` (probe P10) → SQL rejection.

### 16. RPC response parsing

**PASS with MEDIUM-01.** Empty/multi-row/missing/wrong-type/status/engine/attempt mismatch covered by tests; bool-as-int rejected for bool fields; shallow nested aliasing remains.

### 17. Learner-safe output

**PASS.** Scene/terminal serializers expose only view fields; probe P11 found no forbidden hidden keys; terminal exactly four keys.

### 18. Immutability

**PARTIAL.** Serialize does not mutate snapshots/decisions; deserialize does not mutate input mapping; replay does not mutate decision rows/envelope (tests AK–AM). Parser nested aliasing fails (MEDIUM-01). Serialize output nested structures are new containers (probe P2 pass for source isolation).

### 19. Engine V1 isolation

**PASS.** `utils/scenario_persistence.py` unchanged (`git diff` empty); no V1→V2 import; no Engine V1 import from V2 adapter; fingerprint/idempotency reuse is semantically compatible (same canonical JSON fingerprint formula).

### 20. Test quality

**PARTIAL.** Broad A–AX coverage is real and valuable for projection, replay authority, strict types, and RPC shape. Weaknesses: asserts wrong envelope field names (MEDIUM-04); missing SQL seven-key tests; missing nested parse-alias test; missing serializer AttributeError wrapping test; missing independent fingerprint assertion (behavior OK in probe); missing `version` vs `scenarioVersion` conflict test that would have caught BLOCKER-01.

---

## Independent probes executed

| ID | Probe | Result |
|---|---|---|
| P1 | `scenarioVersion` vs `version`; `status` vs `isComplete` | **FAIL for SQL** — missing `version`, `isComplete` |
| P2 | Nested alias isolation on serialize | PASS |
| P3 | Nested alias isolation on RPC parse | **FAIL** — shallow copy aliases nested state |
| P4 | Bool-as-int rejection | PASS |
| P5 | `-0.0` vs `0.0` | Equal under Python `==` (documented) |
| P6 | int `1` vs float `1.0` state | Both become float; equal |
| P7 | UUID normalization | RPC params OK; envelope attemptId not lowercased |
| P8 | Unvisited option-order scenes | Only visited/current scenes present |
| P9 | Corrupted envelope history | Cache mismatch; canonical rows authoritative |
| P10 | Fingerprint stability | Stable 64-hex; submit envelope still missing SQL keys |
| P11 | Learner hidden-key leakage | None |
| P12 | Raw AttributeError on bad serializer input | **FAIL** — leaks AttributeError |
| P13 | MappingProxyType deserialize | Accepted via thaw |

No temporary files remained after probes (in-memory only).

---

## Focused tests

```text
python -m pytest tests/test_scenario_persistence_v2.py \
  tests/test_scenario_engine_v2.py \
  tests/test_scenario_persistence.py \
  tests/test_scenario_learner_controller.py -q
```

**Result:** `447 passed` in ~4.05s.

---

## Required correction sequence

1. **Envelope wire-shape correction (blockers):**
   - `scenarioVersion` → `version`
   - `status` → `isComplete` (bool)
   - Remove `attemptId` and `decisionCount` (frozen contract)
   - Keep `routingResolutions` / `selectedVariantIdByScene` always present
2. **Replay identity:** pass `attempt_id=attempt_row_id` (trusted column), not an envelope field.
3. **RPC parse deep-copy** nested JSON (MEDIUM-01).
4. **Serializer domain-error wrapping** for wrong input types (MEDIUM-03).
5. **Canonicalize any remaining UUID strings** on serialize boundaries.
6. **Rewrite/extend tests:** assert frozen keys; SQL seven-key presence/types on start+submit envelopes; nested parse non-aliasing; no `scenarioVersion`/`status` acceptance.
7. Re-run the 447-focused suite (expect count change with new tests) and a disposable SQL smoke of one start + one submit using adapter-built envelopes (next task).

Do **not** change V68/V69 SQL to accept `scenarioVersion`/`status` — the SQL contract is validated and shared with Engine V1.

---

## Remaining risks (after corrections)

1. End-to-end adapter↔PostgREST↔SQL path still unexercised until orchestration + disposable DB smoke.
2. Whole-envelope JSONB CAS is sensitive to any non-canonical float/key formatting; keep serialize deterministic.
3. Orchestration layer still missing (explicitly out of scope here).

---

## Recommended next task

**SIM-PERSIST-V2-04B — Envelope wire-shape correction** against the frozen contract in this review (must land before any start/resume orchestration). Then **SIM-PERSIST-V2-05** orchestration against the disposable DB from SIM-PERSIST-V2-03.

---

## Review hygiene

| Check | Result |
|---|---|
| Source/tests/contracts modified by this review | No (only this review file created) |
| Protected paths inspected | No |
| Database connection | No |
| Staged / committed / pushed / deployed | No |
| Branch / HEAD | `main` / `6136673` |
