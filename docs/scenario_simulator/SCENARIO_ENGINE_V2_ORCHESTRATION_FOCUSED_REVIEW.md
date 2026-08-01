# SCENARIO_ENGINE_V2 Orchestration — Focused Production-Readiness Review

**Task ID:** SIM-PERSIST-V2-05-REVIEW-01
**Model:** Auto
**Baseline HEAD:** `a214e36` — Complete Engine V2 persistence foundation
**Scope:** Review-only. No source, test, SQL, migration, controller, UI, staging, commit, push, deploy, or production connection.

---

## Readiness decision

**CORRECTIONS_REQUIRED**

| Metric | Value |
|---|---|
| Blockers | **0** |
| Remaining / new HIGH findings | **1** |
| Medium findings | **4** |
| Low / residual findings | **3** |
| Total findings | **8** |
| Focused suite | **508 passed, 9 subtests passed** |
| Engine V1 isolated | **Yes** |
| Source/tests/SQL modified by this review | **No** |
| Production connection | **No** |

The orchestration vertical slice is architecturally sound: trusted identity + canonical replay authority, verify-only cache, CAS/idempotency fail-closed, complete RPC error-prefix coverage for start/submit, and learner-safe scene/terminal separation. It is **not** ready for a local milestone commit until the HIGH error-contract leak is closed.

---

## Pre-flight

| Check | Result |
|---|---|
| Shell | `shell-ok` |
| Branch | `main` |
| HEAD | `a214e36` |
| Staged changes | None |
| In-scope untracked | `utils/scenario_orchestration_v2.py`, `tests/test_scenario_orchestration_v2.py`, `docs/scenario_simulator/SCENARIO_ENGINE_V2_ORCHESTRATION_IMPLEMENTATION_REPORT.md` (+ this review file after write) |
| Protected paths | Untracked and untouched |
| Focused suite | `508 passed, 9 subtests passed` |

---

## Findings

### HIGH-01 — `TypeError` can escape public resume/start/submit paths

**Area:** Error contract (review area 12)
**Evidence:** `_parse_attempt_snapshot_row` builds `decisions` with:

```python
tuple(_deep_copy_json(item) if isinstance(item, Mapping) else dict(item) for item in decisions_raw)
```

Independent probe with `decisions=[123]` raised raw `TypeError` (`dict(123)`), not `ScenarioOrchestrationV2MalformedPersistenceResponseError`.

This path runs **outside** `_wrap_persistence_call` after `load_attempt_snapshot` returns, so start/resume/submit that reload trusted rows can all surface it.

**Why HIGH:** The task’s error contract and readiness bar require public orchestration APIs never leak raw `TypeError`. A malformed persistence response must fail closed as a typed orchestration error.

**Required fix:** Reject non-mapping decision elements with `ScenarioOrchestrationV2MalformedPersistenceResponseError` (or wrap the parse helper). Add a focused unit test.

---

### MEDIUM-01 — V1 `ScenarioPersistenceValidationError` leaks on invalid email

**Area:** Error contract / dependency boundary
**Evidence:** Probe calling `start_or_resume_scenario_run_v2(..., user_email="bad")` raised `utils.scenario_persistence.ScenarioPersistenceValidationError` from `normalize_scenario_persistence_email`, not an orchestration-typed error.

**Impact:** Cross-module V1 exception type escapes the V2 orchestration public surface. Meaning is acceptable (invalid email), but callers must catch V1 types to handle V2 orchestration.

**Required fix:** Catch and re-raise as `ScenarioOrchestrationV2InvalidRequestError` with causal chaining.

---

### MEDIUM-02 — Resume docstring disagrees with V69 attempt-id contract

**Area:** Start/resume API semantics
**Evidence:** `start_or_resume_scenario_run_v2` docstring claims that on resume “the RPC ignores a freshly minted id.” V69 rejects a non-null mismatched `p_attempt_id` with `attempt_id_conflict:` (fail-closed). The adapter always sends a non-null `p_attempt_id`, so omitting `attempt_id` while an in-progress attempt exists will conflict rather than silently resume.

**Impact:** Documentation/API expectation mismatch. Runtime behavior is correctly fail-closed and prefix-mapped to `ScenarioOrchestrationV2IdentityMismatchError`. Resume-by-known-id and `resume_and_replay_scenario_run_v2` remain valid.

**Required fix:** Correct the docstring to match V69 (matching id or explicit resume API; mismatched minted id conflicts).

---

### MEDIUM-03 — Frozen results still expose mutable nested dict aliases

**Area:** Internal result safety / immutability
**Evidence:** `TrustedAttemptSnapshotV2.serialized_engine_state`, `decisions` dicts, and `ScenarioOrchestrationSubmissionContextV2.cached_envelope` are plain `dict`s inside `frozen=True` dataclasses. Probe confirmed mutating `cached_envelope` does **not** mutate the fake store (deep-copy isolation works), but callers can still mutate returned nested structures in place.

**Impact:** Not a persistence-authority bug today (deep copies on ingest). It weakens the “immutable typed results” claim and could confuse future server callers.

**Recommended fix:** Freeze nested JSON with `MappingProxyType` / deep-freeze, or document that nested dicts are caller-owned copies.

---

### MEDIUM-04 — Public results include full server `run` snapshots

**Area:** Learner-safe vs internal result separation
**Evidence:** `StartOrResumeScenarioRunResultV2` / `SubmitScenarioDecisionResultV2` expose `run: ScenarioRunV2Snapshot` (state, flags, counters, `DebriefTraceEntry` history) beside `learner_view`.

**Impact:** Correct for server-side CAS submission context, and `learner_view` itself is clean (probe: scene/terminal serializers show zero hidden-key hits; terminal keys exactly `outcomeId`, `outcomeTitle`, `narrative`, `displayScore`). Residual risk if a future controller JSON-serializes the whole result object to clients.

**Recommended fix:** Document a hard controller rule: serialize only `learner_view` (via adapter serializers). Optionally nest `run` under an explicitly named `server_state` field.

---

### LOW-01 — `created` flag is taken from RPC without independent insert proof

**Area:** Start flow
After reload/replay, `created=rpc_result.created` is returned. Replay authority does not depend on this flag; spoofing a fake would not skip verification. Against real SQL the flag is authoritative. Acceptable residual; optional assert against empty/non-empty decision history when useful.

---

### LOW-02 — Guard-violation prefixes unmapped (fail-closed via generic)

**Area:** RPC error-prefix coverage
V1 maps `attempt_insert_guard_violation:` / `decision_insert_guard_violation:`. Orchestration omits them. Those prefixes are not raised on the normal start/submit success path; if they appear, unknown-prefix handling yields `ScenarioOrchestrationV2PersistenceDependencyError` (fail-closed). Optional add for parity.

---

### LOW-03 — Test-quality gaps around the HIGH leak and smoke cleanup edge

**Area:** Test quality / disposable smoke
- No unit test for non-mapping `decisions` elements (the HIGH-01 case).
- `TestEngineV1Isolation` only imports V1 modules; it does not assert byte-level V1 file immutability (git/status review covers that).
- Disposable smoke `tearDownClass` uses `docker rm -f` with `check=True` (adequate; `rm -f` is idempotent). Role bootstrap and JSON datetime round-trip are correctly confined to the test-only psycopg2 adapter and do not leak into the production port.
- Fake persistence mirrors CAS/idempotency; sequence/scene typed-conflict tests also force `submit_raise`, so mapping is covered even if the fake diverges.

---

## Area-by-area results

### 1. Public API and types — PASS (with MEDIUM-04 note)

Four required entry points exist; results are frozen dataclasses; raw RPC dicts do not escape typed parsers; `learner_view` is separate from `submission_context` / `run`.

### 2. Dependency port — PASS

`ScenarioOrchestrationV2PersistencePort` exposes only start RPC, submit RPC, and trusted attempt+decisions load. No env reads, no global client. Test-only psycopg2 helper lives in the test module only. A real Supabase adapter can implement the protocol unchanged.

### 3. Start flow — PASS

Non-nil UUID selected before Engine init; same UUID in Engine and `p_attempt_id`; 7-key RPC params; exact 17-key envelope (probe); RPC parsed then **reload + replay** before return; identity fields cross-checked.

### 4. Resume flow — PASS

Trusted row identity authoritative; envelope verify-only via `replay_serialized_run_v2`; mismatch fail-closed; no silent repair/overwrite. Docstring issue tracked as MEDIUM-02.

### 5. Canonical decision loading — PASS

Strict int sequences from 1, gap/dup/bool/wrong-attempt/empty-scene reject; returns immutable `ScenarioDecisionInputV2` triples only. `get_scenario_attempt_v1` omits idempotency keys from decision JSON (by design); no unrelated columns returned to controllers.

### 6. Submit flow — PASS

Visible-option gate; local `apply_decision_v2`; 13-key RPC params (probe); parse; mandatory reload/replay; `_assert_runs_equivalent` before success. RPC success alone cannot return success.

### 7. Idempotency — PASS

Same-key identical retry succeeds; same-key changed request fail-closed (probe); explicit retry does not mint a new key; duplicate row prevented by fake/SQL semantics; post-retry replay still verifies state.

### 8. CAS / stale state — PASS

Sequence/scene/`state_before` map to typed conflicts; no automatic retry with recomputed state.

### 9. RPC error-prefix coverage — PASS

All documented start (`V69`) and submit (`V68`) RPC exception prefixes are present in `_RPC_ERROR_PREFIX_MAP` (probe: `MISSING_START []`, `MISSING_SUBMIT []`). Unknown prefixes → `ScenarioOrchestrationV2PersistenceDependencyError`. No misclassification that could yield success or silent retry.

### 10. Learner-safe boundary — PASS

Recursive hidden-key probes on serialized scene/terminal views: no hits. Terminal payload is exactly four approved keys.

### 11. Internal result safety — PASS with MEDIUM-03/04

Minimum trusted state retained for next submit; nested mutability and full `run` exposure noted above.

### 12. Error contract — FAIL (HIGH-01, MEDIUM-01)

RPC/dependency failures wrap correctly (probe: `DEP_WRAP True`). UUID parsing wraps to orchestration invalid-request. **TypeError leak** and **V1 validation leak** violate the closed error surface.

### 13. Immutability / aliasing — PASS with MEDIUM-03

Source content and decision rows immutable (probe); RPC param mutation does not alias into returned run; store isolation holds.

### 14. Terminal flow — PASS

Happy-path terminal completion yields terminal learner view only; no next scene; serializer excludes internal trace.

### 15. Engine V1 isolation — PASS

No V1 imports of orchestration_v2; learner controller untouched; V1 tests pass in the 508 suite; git shows no V1 file modifications.

### 16. Test quality — PASS with LOW-03

Letter coverage A–AJ is real and includes reload/replay fail-closed (AB). Gaps: HIGH-01 case, deeper V1 isolation assertion.

### 17. Disposable smoke — PASS (static review)

Container `certbound-v2-orchestration-smoke`, local port only, V66–V69 + role bootstrap in throwaway DB, autocommit + JSON round-trip confined to test adapter, start/submit/resume/idempotent retry/stale conflict covered, `tearDownClass` destroys container. No production credentials. This review did not re-run Docker smoke (unit suite already includes it when Docker is available; prior task run passed).

---

## Independent probes executed

Temporary script `_tmp_orch_review_probes.py` (removed after run) confirmed:

| Probe | Result |
|---|---|
| Unknown RPC prefix | `ScenarioOrchestrationV2PersistenceDependencyError` |
| All start/submit documented prefixes | Mapped; none missing |
| Stale sequence/scene | Typed conflicts |
| Idempotency changed request | Fail-closed |
| Nested alias isolation | Pass |
| Malformed canonical rows | Fail-closed |
| Terminal learner output | Four safe keys; no hidden hits |
| Dependency exception wrap | Pass with `__cause__` |
| Content / decision-row immutability | Pass |
| Non-mapping decision element | **TypeError leak (HIGH-01)** |
| Invalid email | **V1 validation leak (MEDIUM-01)** |

---

## Required correction sequence

1. **HIGH-01:** Fail-closed parse of non-mapping `decisions` elements; wrap any residual parse errors as `ScenarioOrchestrationV2MalformedPersistenceResponseError`.
2. Add unit test(s) for HIGH-01.
3. **MEDIUM-01:** Wrap `normalize_scenario_persistence_email` failures as `ScenarioOrchestrationV2InvalidRequestError`.
4. **MEDIUM-02:** Fix resume/`attempt_id` docstring to match V69.
5. (Recommended) Freeze or document nested dict mutability; document controller serialization rule for `learner_view` only.
6. Re-run the focused 508-test suite + the same independent probes.
7. Re-review for READY_FOR_LOCAL_MILESTONE_COMMIT.

---

## Recommended next task

**SIM-PERSIST-V2-05B** — close HIGH-01 and MEDIUM-01/02 only (minimal correction pass), then a short confirmation review before any local milestone commit or Supabase production adapter work.

---

## Confirmations

| Item | Status |
|---|---|
| Source / tests / SQL / migrations untouched by this review | Yes |
| Protected paths untouched | Yes |
| No production connection | Yes |
| Nothing staged, committed, pushed, or deployed | Yes |
| Temporary probe artifacts removed | Yes (`_tmp_orch_review_probes.py` deleted) |
