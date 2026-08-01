# Engine V2 Supabase Persistence Port — Final Confirmation

**Task ID:** SIM-PERSIST-V2-06C
**Baseline HEAD:** `00d2773` — Complete Engine V2 orchestration vertical slice
**Review type:** Delta-only confirmation of SIM-PERSIST-V2-06B correction pass
**Authoritative inputs:**
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_SUPABASE_PORT_FOCUSED_REVIEW.md`
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md`
- `utils/scenario_supabase_port_v2.py`
- `tests/test_scenario_supabase_port_v2.py`
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_SUPABASE_PORT_IMPLEMENTATION_REPORT.md`
- `utils/scenario_orchestration_v2.py`
- V68/V69 migrations (prefix-contract cross-check only)

This is review-only work. No source, tests, SQL, migrations, controller, or UI were modified.

---

## Readiness decision

## **READY_FOR_LOCAL_MILESTONE_COMMIT**

| Metric | Value |
|---|---|
| Blockers | **0** |
| Remaining HIGH findings | **0** |
| New HIGH findings | **0** |
| Total findings in this confirmation | **0** |
| Focused suite | **605 passed, 45 subtests passed** |
| Real pinned PostgREST tests | **2/2 passed** |
| Independent probes | **31/31 passed** |

HIGH-01 from the focused review is fully closed. Unknown/unrecognized PostgREST/SDK/database errors fail closed with sanitized typed errors. Only the approved 36 orchestration-synced business prefixes remain verbatim. Real disposable PostgREST `PGRST202` sanitization is confirmed. Cause chaining, trust-boundary documentation, image pinning, and fail-safe skip semantics are confirmed.

---

## Confirmation-area results

### 1. Approved business prefixes — **PASS**

- Port allowlist `_APPROVED_BUSINESS_ERROR_PREFIXES` contains exactly **36** prefixes.
- Independent probe confirmed set-equality with `utils.scenario_orchestration_v2._RPC_ERROR_PREFIX_MAP`.
- Matching uses exact, case-sensitive `str.startswith` only (`_is_approved_business_error`).
- Near-match rejection confirmed for wrong case, extra character, and mid-message embedding.
- `TestApprovedPrefixSetSync` remains an effective structural drift guard.
- Migration-only trigger prefixes (`attempt_insert_guard_violation:`, `decision_insert_guard_violation:`) remain intentionally excluded; they are not orchestration-classified and are not reachable through this RPC-only port under normal operation.

### 2. Unknown error fallback — **PASS (HIGH-01 closed)**

Unrecognized errors raise `ScenarioSupabasePortV2UnknownError` with stable generic text:

`persistence_error: RPC {rpc_name!r} failed unexpectedly.`

Independent probes confirmed sanitization (no raw content survival) for:

- arbitrary unknown prefix
- PGRST202 function-not-found
- schema-cache text
- raw SQL
- stack-trace-shaped text
- database URL / credential-bearing text
- JWT-like value
- service-role-like token
- host and port
- schema/table/relation/function names
- empty message
- non-string `.error` / `.message` content (covered by permanent tests)

Real disposable PostgREST confirmation:

`TestSupabasePortDisposablePostgrestSmoke.test_real_postgrest_unknown_function_error_is_sanitized` **PASSED**.

### 3. Classification precedence — **PASS**

Documented and implemented order in `_classify_rpc_exception`:

1. approved business-error prefix (verbatim)
2. authentication (sanitized)
3. permission (sanitized)
4. timeout (sanitized)
5. connection/transport (sanitized)
6. malformed response shape handled separately by `_extract_single_attempt_row`
7. sanitized generic unknown failure (`ScenarioSupabasePortV2UnknownError`)

Confirmed: a known business prefix is not reclassified by auth/permission words or codes appearing later in the message/body.

### 4. Cause chaining — **PASS**

Both public failure paths chain `__cause__`:

- raised SDK/PostgREST exception: `raise _classify_rpc_exception(...) from exc`
- response `.error` attribute: `raise _classify_rpc_exception(...) from carrier`

Independent probes confirmed both paths. Raw SDK/PostgREST exception objects do not escape as the public exception type.

### 5. Control-flow exceptions — **PASS**

`_call_rpc` catches only `Exception`, not `BaseException`. Permanent tests still prove `KeyboardInterrupt` and `SystemExit` propagate. Independent source inspection confirmed no `except BaseException`.

### 6. User-email trust boundary — **PASS (MEDIUM-02 closed)**

Module docstring and `load_attempt_snapshot` docstring explicitly state:

- trusted server-side `service_role` client injection is required
- `user_email` must come from trusted authenticated server-side identity/session state
- browser/query/form/client-controlled payloads are not identity proof
- the port forwards `user_email` and does not authorize it
- future controller owns deriving email from authenticated server-side context

Permanent LOW-02 tests confirm zero client-side email validation/normalization in the port itself.

### 7. Pinned PostgREST image — **PASS (MEDIUM-01 closed)**

- Active integration image constant: `postgrest/postgrest:v14.16`
- No `:latest` remains in the active container-start path
- Remaining `:latest` mentions are historical comments explaining why the pin was chosen
- Implementation/correction reports document the pin and digest cross-check
- Pin is test-only; production runtime code never pulls or references Docker images

### 8. Skip semantics — **PASS (MEDIUM-03 closed)**

- Class-level skip only when Docker CLI/daemon is genuinely unavailable
- Image pull failure before container/network creation may raise explicit `unittest.SkipTest`
- After disposable startup begins, migration/startup/RPC/replay/assertion failures propagate as real failures/errors
- `setUpClass` failure path and `tearDownClass` both clean containers/network
- `TestIntegrationSkipSemantics` permanently covers both halves of this contract

### 9. Regression safety — **PASS**

Focused suite (605/45) includes unchanged coverage for:

- exact 7-key start RPC behavior
- exact 13-key submit RPC behavior
- no-auto-retry
- alias isolation
- canonical decision loading / orchestration protocol compatibility
- Engine V1 isolation (no V1 imports of the V2 port; V1 tests still pass)

No orchestration protocol, SQL, migration, controller, or Engine V1 source changes were required or performed for this confirmation.

### 10. Real pinned PostgREST confirmation — **PASS**

Executed:

1. `test_real_postgrest_start_submit_resume_idempotency_and_conflict` — **PASSED**
2. `test_real_postgrest_unknown_function_error_is_sanitized` — **PASSED**

Confirmed disposable-only identity (`certbound-v2-port-smoke-*`), no production credentials/project references, and post-run cleanup with no remaining containers/networks matching those names.

---

## Finding disposition vs focused review

| ID | Original severity | Confirmation disposition |
|---|---|---|
| HIGH-01 | HIGH | **Closed** — fail-closed allowlist confirmed by code, permanent tests, independent probes, and real PostgREST |
| MEDIUM-01 | MEDIUM | **Closed** — pinned `v14.16` |
| MEDIUM-02 | MEDIUM | **Closed** — trust-boundary docs explicit |
| MEDIUM-03 | MEDIUM | **Closed** — fail-safe skip semantics |
| LOW-01 | LOW | **Closed** — permanent null-data / sanitization coverage present |
| LOW-02 | LOW | **Closed** — permanent zero client-side email-validation tests present |

No new findings were opened by this confirmation review.

---

## Residual risks (non-blocking)

1. The port’s approved-prefix list is a hardcoded copy of orchestration’s map (by design, zero import coupling). Drift is machine-guarded by `TestApprovedPrefixSetSync`, but future prefix additions still require updating both lists.
2. Authentication marker matching remains substring-based for non-business errors (e.g. `"jwt"`). This can route some unknown credential-bearing messages into the sanitized authentication bucket rather than the unknown bucket. Public text remains sanitized either way; this is not a HIGH-01 reopen.

---

## Tests and probes executed

| Run | Result |
|---|---|
| Focused 6-module suite | **605 passed, 45 subtests passed** |
| Real pinned PostgREST class (2 tests) | **2 passed** |
| Independent temp probes (outside repo) | **31/31 passed** |
| Disposable cleanup check | **no remaining smoke containers/networks** |

Temporary probe artifacts were created under the OS temp directory and deleted after use.

---

## Scope confirmation

- Source/tests/contracts: **untouched by this review**
- Protected paths: **untouched / not inspected**
- Production connection: **none**
- SQL/migrations/controller/UI: **untouched**
- Staging/commit/push/deploy: **none**

---

## Recommended next task

Create the local milestone commit for the Engine V2 Supabase persistence port vertical slice (implementation + focused review + correction report + this final confirmation), staging only the approved in-scope files, with no push/deploy.
