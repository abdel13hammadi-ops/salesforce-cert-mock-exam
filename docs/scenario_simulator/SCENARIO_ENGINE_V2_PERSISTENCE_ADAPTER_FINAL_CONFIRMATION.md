# SCENARIO_ENGINE_V2 Persistence Adapter — Final Confirmation

**Task ID:** SIM-PERSIST-V2-04C
**Model:** Auto
**Baseline HEAD:** `6136673` — Complete Scenario Engine V2 vertical slice
**Scope:** Review-only confirmation of the SIM-PERSIST-V2-04B wire-contract correction. No source, test, SQL, migration, orchestration, staging, commit, push, deploy, or database work.

---

## Readiness decision

**READY_FOR_LOCAL_MILESTONE_COMMIT**

| Metric | Value |
|---|---|
| Blockers | **0** |
| Remaining HIGH findings | **0** |
| New HIGH findings | **0** |
| Total findings this confirmation | **0** |
| Exact 17-key envelope confirmed | **Yes** |
| Superseded keys rejected | **Yes** |
| Trusted attempt-row identity confirmed | **Yes** |
| Deep-copy isolation confirmed | **Yes** |
| Serializer error wrapping confirmed | **Yes** |
| Replay authoritative | **Yes** |
| Start RPC 7-key / Submit RPC 13-key | **Yes** |
| Focused tests | **471 passed, 9 subtests passed** |
| Engine V1 isolated | **Yes** |
| Source/tests/SQL modified by this confirmation | **No** |
| Database connection | **No** |

SIM-PERSIST-V2-04B closed every blocker/high finding from SIM-PERSIST-V2-04-REVIEW-01. The corrected adapter is ready for a local milestone commit and subsequent start/resume orchestration.

---

## Pre-flight

| Check | Result |
|---|---|
| Shell | `shell-ok` |
| Branch | `main` |
| HEAD | `6136673` |
| Staged changes | None |
| In-scope uncommitted work | Expected persistence/V69 docs, adapter, tests, and prior Slice A/B artifacts only |
| Focused suite | `471 passed, 9 subtests passed` |

---

## Area-by-area confirmation

### 1. Exact envelope shape — PASS

Independent serialize probe and `_ENVELOPE_V1_KEYS` both yield exactly these 17 keys:

```text
envelopeVersion
simulationId
version
schemaVersion
engineVersion
canonicalContentSha256
currentSceneId
expectedSequenceNumber
isComplete
state
counters
flags
decisionHistory
optionDisplayOrderByScene
selectedVariantIdByScene
routingResolutions
terminalResult
```

Excluded and rejected as unexpected (no dual-key compatibility aliases):

- `scenarioVersion`
- `status`
- `attemptId`
- `decisionCount`

Unknown keys fail closed; missing keys fail closed. Matches V69 SQL load-bearing keys (`->>'version'`, `jsonb_typeof(...->'isComplete') = 'boolean'`), Slice A §9, and schema §19.2.

### 2. Completion semantics — PASS

- `isComplete` validated with `_require_strict_bool` (exact `bool` only).
- Independent probes reject `0`, `1`, `"true"`, `"in_progress"`, `None`.
- Active serialize emits `isComplete: false` with `terminalResult: null`.
- Completed serialize emits `isComplete: true` with a valid `terminalResult`.
- Active envelope with non-null `terminalResult` rejected.
- Completed envelope with null `terminalResult` rejected.
- Active/`true` and completed/`false` inversions rejected by deserialize invariants.

### 3. Trusted attempt identity — PASS

- `PersistedRunEnvelopeV2` has no `attempt_id` attribute; envelope JSON never contains `attemptId`.
- `verify_persisted_attempt_identity_v2(..., attempt_row_id=...)` validates/canonicalizes the trusted UUID separately and does not compare it to any envelope field.
- `replay_serialized_run_v2(..., attempt_row_id=...)` canonicalizes then passes `attempt_id=trusted_attempt_id` into `replay_scenario_run_v2`.
- Injected/forged `attemptId` in a raw envelope is rejected as `unexpected_field` before identity/replay logic.
- RPC parsers check response `attempt_id` against `expected_attempt_id` when supplied.
- `p_attempt_id` remains a separate start-RPC parameter, never duplicated inside the envelope.

### 4. Decision authority — PASS

- Serialized `decisionHistory` entries contain exactly `{sequenceNumber, sceneId, optionId}`.
- Replay deserializes `canonical_decision_rows`, never envelope `decisionHistory`, for reconstruction.
- Corrupting envelope history while supplying fuller canonical rows produces `ScenarioPersistenceV2CacheMismatchError` (fail closed), proving cache cannot drive replay.
- `decisionCount` removal introduced no CAS/sequence regression: sequence authority remains RPC/`expectedSequenceNumber` + canonical rows; no adapter or validated SQL path requires envelope `decisionCount`.

### 5. Replay and cache — PASS

- Reconstruction path: deserialize envelope → verify identity → deserialize canonical decisions → `replay_scenario_run_v2` → re-serialize → compare cache fields → compare terminal separately.
- Cache-compared fields include `currentSceneId`, `expectedSequenceNumber`, `isComplete`, `decisionHistory`, option order, variants, state, counters, flags, routing.
- Terminal mismatch raises distinct `ScenarioPersistenceV2TerminalMismatchError`.
- Existing tests Z–AF2 remain green; independent probe confirmed cache-mismatch fail-closed.

### 6. Required cache fields — PASS

- `routingResolutions` always present (empty list on a fresh start).
- `selectedVariantIdByScene` always present (object; may be empty or contain scene keys).
- Omitting either key fails deserialize.

### 7. Start RPC shape — PASS

`build_start_or_resume_rpc_params_v2` returns exactly:

```text
p_user_email
p_scenario_version_id
p_initial_current_scene_id
p_initial_serialized_state
p_engine_version
p_scenario_content_sha256
p_attempt_id
```

Embedded `p_initial_serialized_state` is the exact 17-key contract and excludes all superseded fields.

### 8. Submit RPC shape — PASS

`build_submit_decision_rpc_params_v2` returns exactly the existing 13-key contract. Both `p_state_before` and `p_state_after` use the corrected 17-key envelope. Request fingerprint is deterministic for identical inputs (64 lowercase hex) via reused `compute_request_fingerprint`.

### 9. Parser alias isolation — PASS

Both parsers route nested JSON through `_row_json_object_field` / `_row_nullable_json_object_field`, which return `copy.deepcopy(...)`. Independent probes proved:

- mutating raw response nested `state`/`flags` after parse does not change parsed output;
- mutating parsed mutable nested values does not change the raw response;
- mutating nested nullable `terminal_result_snapshot` after submit parse does not change parsed terminal result.

### 10. Serializer error contract — PASS

All four public serializers are wrapped by `_wrap_serialization_boundary_errors`:

- `serialize_run_snapshot_v2`
- `serialize_decision_input_v2`
- `serialize_learner_scene_view_v2`
- `serialize_learner_terminal_view_v2`

Malformed objects/`None` raise `ScenarioPersistenceV2SerializationError`, not raw `AttributeError`/`TypeError`/`KeyError`/`ValueError`/`IndexError`. Wrapper does not catch `KeyboardInterrupt` or `SystemExit` (independent probes). Already-domain `ScenarioPersistenceV2ValidationError` still passes through unwrapped.

### 11. Strict types — PASS

Independent probes confirmed:

| Probe | Result |
|---|---|
| `isComplete` rejects `0`/`1` | PASS |
| `sequenceNumber` rejects `True`/`False` | PASS |
| NaN / ±Infinity rejected | PASS |
| Malformed UUID → domain error | PASS |
| Nil attempt UUID rejected on start RPC create path | PASS |

### 12. Engine V1 isolation — PASS

- `git diff` empty for `utils/scenario_persistence.py` and `utils/scenario_engine_v2.py`.
- `utils/scenario_persistence.py` does not import `scenario_persistence_v2`.
- Learner controller still imports V1 persistence only.
- V2 adapter imports only V1’s engine-agnostic `compute_request_fingerprint` / `generate_idempotency_key`.
- Focused V1 persistence + learner-controller suites remain green within the 471-test run.

### 13. Test quality — PASS

The SIM-PERSIST-V2-04B added/rewritten tests assert the frozen contract rather than only mirroring internals:

- exact envelope key set (`_FROZEN_ENVELOPE_KEYS`);
- rejection of `scenarioVersion` / `status` / `attemptId` / `decisionCount`;
- bool/int/`string` `isComplete` rejection;
- trusted attempt-row identity separated from option-order randomization;
- forged envelope `attemptId` structural rejection;
- start/submit SQL-compatible embedded envelope shape;
- bidirectional deep-copy isolation including nullable terminal snapshot;
- serializer wrapping plus non-catch of control-flow exceptions;
- existing replay/cache/terminal and Engine V1 isolation coverage retained.

---

## Independent temporary probes

53 in-memory Python probes executed via a one-shot `python -c` invocation (no files written). Result: **53/53 PASS**. Covered:

- exact serialized key set
- `scenarioVersion` / `status` / `attemptId` / `decisionCount` rejection
- bool/int/`None` `isComplete` rejection
- sequence bool rejection
- NaN/infinity rejection
- forged attempt identity
- nested parser aliasing (both directions + nullable terminal)
- serializer error wrapping + KeyboardInterrupt/SystemExit non-catch
- seven-key start / thirteen-key submit shapes
- fingerprint determinism
- nil/malformed UUID domain errors
- replay cache fail-closed
- V1 surface isolation smoke

Temporary artifacts removed: none created on disk; recursive probe-file scan found none.

---

## Focused tests executed

```text
python -m pytest \
  tests/test_scenario_persistence_v2.py \
  tests/test_scenario_engine_v2.py \
  tests/test_scenario_persistence.py \
  tests/test_scenario_learner_controller.py \
  -q
```

**Result:** `471 passed, 9 subtests passed` in ~4.4s.

---

## Remaining risks (non-blocking for local milestone commit)

1. End-to-end adapter ↔ PostgREST ↔ SQL path still unexercised against a live disposable database; that belongs to start/resume orchestration.
2. Whole-envelope JSONB CAS remains sensitive to future non-canonical serialization drift; keep `serialize_run_snapshot_v2` as the sole envelope emitter.
3. Orchestration layer is still unimplemented (explicitly out of this confirmation’s scope).

None of these reopen a blocker/high finding against the corrected wire contract.

---

## Recommended next task

1. Local milestone commit of the corrected adapter + focused tests + persistence docs (when the user explicitly requests a commit).
2. Then **SIM-PERSIST-V2-05 — start/resume orchestration**, beginning with a disposable-database smoke of one real `start_or_resume_scenario_attempt_v1` and one real `submit_scenario_decision_v1` built from this adapter’s RPC param builders.

---

## Confirmation hygiene

| Check | Result |
|---|---|
| Source/tests/SQL/migrations modified | No |
| Only file created | This confirmation report |
| Protected paths inspected | No |
| Database connection | No |
| Staged / committed / pushed / deployed | No |
| Branch / HEAD | `main` / `6136673` |
