# Engine V2 Supabase Persistence Port — Correction Report

**Task ID:** SIM-PERSIST-V2-06B
**Baseline HEAD:** `00d2773` — Complete Engine V2 orchestration vertical slice
**Authoritative input:** `docs/scenario_simulator/SCENARIO_ENGINE_V2_SUPABASE_PORT_FOCUSED_REVIEW.md`
(readiness decision: `CORRECTIONS_REQUIRED`; 1 new HIGH, 3 MEDIUM, 2 LOW findings)
**Scope:** `utils/scenario_supabase_port_v2.py`, `tests/test_scenario_supabase_port_v2.py`,
`docs/scenario_simulator/SCENARIO_ENGINE_V2_SUPABASE_PORT_IMPLEMENTATION_REPORT.md`, this report.
No SQL/migration/schema/RLS/grant change. No controller/UI change. No Engine V1 change. No
orchestration-protocol change. No database connection except the pre-existing disposable
Docker/PostgREST integration test.

## 1. Objective

Close every actionable finding from the focused review with the smallest change that fully
removes each risk, without redesigning the port or touching the orchestration protocol, SQL,
Engine V1, or the controller (not yet implemented).

## 2. Findings closed

| ID | Severity | Title | Disposition |
|---|---|---|---|
| HIGH-01 | HIGH | Unrecognized non-business PostgREST/SDK errors preserved verbatim (fail-open classification) | **Closed** — §3 |
| MEDIUM-01 | MEDIUM | Disposable integration test pins `postgrest/postgrest:latest`, not a fixed tag | **Closed** — §5 |
| MEDIUM-02 | MEDIUM | `user_email` trust-boundary warning less explicit than V1's equivalent docstring | **Closed** — §4 |
| MEDIUM-03 | MEDIUM | Integration-test skip logic can silently mask an unrelated Docker/environment failure | **Closed** — §6 |
| LOW-01 | LOW | A few realistic response/error shapes verified only by the review's temporary probes | **Closed** — §7 |
| LOW-02 | LOW | No permanent test proves zero client-side `user_email` validation | **Closed** — §7 |

Remaining blockers after this pass: **0**. Remaining HIGH findings: **0**. New HIGH findings
introduced by this correction pass: **0**.

## 3. HIGH-01 — fail-closed business-error allowlist

### 3.1 The defect

`_classify_rpc_exception`'s fallback branch was, in effect, a **blocklist**:

```python
if message:
    # "Preserved verbatim -- this is expected to be a Postgres RAISE EXCEPTION business error"
    return ScenarioSupabasePortV2RpcError(message)
```

Any message that did not match a timeout/connection/permission/authentication marker was assumed
safe and returned to the caller unchanged. The review reproduced this against a **real** disposable
PostgREST server: calling `start_or_resume_scenario_attempt_v1` with an unrecognized function
signature produced a genuine `PGRST202` error —

> `Could not find the function public.definitely_does_not_exist_xyz_v1(p_foo) in the schema
> cache`

— which was preserved verbatim, leaking the target schema (`public`), a function name pattern,
and a parameter name (`p_foo`) into what would become a learner-facing/log-facing error string in
production.

### 3.2 The fix — allowlist, not blocklist

`_classify_rpc_exception` now follows one fixed, documented, deterministic order:

1. **Approved business-error prefix** (new: `_is_approved_business_error`) — an exact,
   case-sensitive, position-0 `str.startswith` match against `_APPROVED_BUSINESS_ERROR_PREFIXES`,
   a closed 36-entry tuple. **Only this step may return the raw message text.**
2. Authentication signal (SQLSTATE/PostgREST code or text marker) — sanitized.
3. Permission signal (SQLSTATE or text marker) — sanitized.
4. Timeout signal (text marker) — sanitized.
5. Connection/transport signal (text marker) — sanitized.
6. *(Malformed response **shape**, as opposed to a raised exception, is classified separately by
   `_extract_single_attempt_row` — unchanged, not part of this exception-classification order.)*
7. **Generic unknown persistence failure** (new: `ScenarioSupabasePortV2UnknownError`) — the
   fail-closed sanitized default for **everything** not positively matched above. Message is
   always the fixed text `"persistence_error: RPC {rpc_name!r} failed unexpectedly."` — the raw
   message/code/details are **never** included.

Business errors can no longer be shadowed by the fuzzy substring heuristics in steps 2–5, because
step 1 is checked first (regression-tested — see §3.4, item 21).

### 3.3 The approved-prefix allowlist

`_APPROVED_BUSINESS_ERROR_PREFIXES` is a **hardcoded, literal** tuple of the 36 prefixes actually
present in `utils.scenario_orchestration_v2._RPC_ERROR_PREFIX_MAP` (the orchestration layer's own
authoritative classification map):

```
invalid_user_email:                  invalid_attempt_id:
invalid_scenario_version_id:         invalid_idempotency_key:
invalid_sequence_number:             invalid_expected_scene_id:
invalid_selected_option_id:          invalid_request_fingerprint:
invalid_state_before:                invalid_state_after:
invalid_is_terminal:                 invalid_resulting_scene_id:
invalid_terminal_ending_id:          invalid_terminal_result_snapshot:
invalid_terminal_fields:             invalid_initial_scene:
invalid_initial_state:               invalid_initial_state_identity:
invalid_initial_state_lifecycle:     scenario_version_not_found:
scenario_version_not_published:      engine_version_mismatch:
content_hash_mismatch:               attempt_not_found:
attempt_not_in_progress:             idempotency_key_conflict:
sequence_mismatch:                   scene_mismatch:
state_before_mismatch:               state_identity_mismatch:
state_lifecycle_mismatch:            terminal_result_mismatch:
terminal_ending_mismatch:            attempt_id_conflict:
attempt_id_collision:                start_or_resume_failed:
```

This set was derived by:

1. Importing `utils.scenario_orchestration_v2._RPC_ERROR_PREFIX_MAP` directly and printing its
   exact prefix list (36 entries, verified unique).
2. Independently extracting every `RAISE EXCEPTION '<prefix>:'` string from
   `supabase/migrations/20260719130000_..._v68_....sql` and
   `supabase/migrations/20260719140000_..._v69_....sql` via a regex scan (38 distinct prefixes).
3. Diffing the two: the migration set is exactly the orchestration set **plus**
   `attempt_insert_guard_violation:` and `decision_insert_guard_violation:` — both are
   trigger-level guards against **direct, non-RPC table mutation**. This port never calls
   `client.table(...)`, only `client.rpc(...)`, so these cannot fire through it in normal
   operation, and orchestration itself does not recognize either prefix for classification
   purposes. They are **deliberately excluded** from the port's allowlist: including them would
   provide no classification benefit (orchestration would still fall through to its own generic
   dependency error) while needlessly risking exposure of internal trigger/table implementation
   detail if one were ever erroneously raised.

**Drift protection:** `TestApprovedPrefixSetSync::test_prefix_sets_match_exactly` imports both
`_APPROVED_BUSINESS_ERROR_PREFIXES` and `_RPC_ERROR_PREFIX_MAP` and asserts set-equality. This
runs on every `pytest` invocation of this test file, so any future addition/removal of a business
prefix on either side that is not mirrored on the other now fails the suite immediately, without
requiring the two production modules to import each other.

### 3.4 Cause chaining — a related gap also closed

While implementing this correction, a second gap was found and fixed: the `.error`-attribute
response path in `_call_rpc` (`if error: raise _classify_rpc_exception(...)`) never chained
`__cause__` at all (unlike the raised-exception path, which already used `from exc`). This is now:

```python
carrier = _RpcErrorCarrier(error)
raise _classify_rpc_exception(rpc_name, carrier) from carrier
```

so `__cause__` is now populated for **both** the raised-exception and the `.error`-attribute
response shapes, for every classification outcome including the new `ScenarioSupabasePortV2UnknownError`
branch (`test_22_cause_preserved_for_error_attribute_carrier_path`,
`test_22_cause_preserved_through_unknown_error_branch`).

### 3.5 Regression tests added (26 new test methods, `TestFailClosedBusinessErrorAllowlist` +
`TestApprovedPrefixSetSync`)

Covers checklist items 1–16 and 21–22 from the task: known approved start/submit prefixes
preserved; **every** one of the 36 approved prefixes individually proven verbatim
(`subTest`-looped); near-match prefixes (wrong case, extra character, missing underscore, prefix
embedded mid-message) rejected; an unrecognized-but-plausible business-style prefix sanitized;
the exact HIGH-01 `PGRST202` reproduction (both raised-exception and `.error`-attribute shapes)
sanitized with no schema/function/parameter leak; SQL-bearing, stack-trace-shaped,
database-URL-bearing, JWT-like, service-role-token-like, host:port-bearing, and
relation/function-name-bearing unknown messages all sanitized; empty message and non-string
`.message`/`.error` content fail closed without raising; classification precedence proven
deterministic (a synthetic message containing both an approved prefix and an auth/permission
marker still classifies as the business error); `__cause__` preserved through both response
shapes for the new unknown-error branch. All 45 pre-existing tests (including the
`KeyboardInterrupt`/`SystemExit` control-flow tests, the no-auto-retry test, and every alias-
isolation test) were re-run unmodified and continue to pass.

### 3.6 Real disposable PostgREST confirmation

`TestSupabasePortDisposablePostgrestSmoke.test_real_postgrest_unknown_function_error_is_sanitized`
(new) calls the real, existing `start_or_resume_scenario_attempt_v1` RPC on the real disposable
PostgREST server with a deliberately wrong parameter name, reproducing a genuine `PGRST202` error
from an actual PostgREST instance (not a fake), and asserts:

- the port raises `ScenarioSupabasePortV2UnknownError`;
- the message contains none of `"schema cache"`, `"p_bogus_param"`, `"does not exist"`;
- `__cause__` is populated.

This passed on the first run after the fix, using the pinned image described in §5.

## 4. MEDIUM-02 — `user_email` trust boundary documentation

The module docstring's "Trust boundary" section and `load_attempt_snapshot`'s own docstring were
expanded to state explicitly, in the same spirit as `utils/scenario_persistence.py`'s existing
wording for Engine V1:

- `user_email` **must** be derived by the trusted server-side caller from its own authenticated
  application identity/session state;
- it **must never** be taken directly from an untrusted browser field, URL/query parameter, form
  input, or other client-controlled payload;
- this port forwards `user_email`, it does **not** authorize it — ownership enforcement is
  entirely the RPC's own `p_user_email` equality check;
- deriving `user_email` from the authenticated server-side session is explicitly the
  responsibility of a **future** Engine V2 controller, and is **not** implemented by this port.

No runtime behavior changed — this is a documentation-only correction, as required.

## 5. MEDIUM-01 — Docker image pinning

`postgrest/postgrest:latest` → `postgrest/postgrest:v14.16`, a fixed version tag.

**How the version was determined:** `docker run --rm --entrypoint postgrest
postgrest/postgrest:latest --version` → `PostgREST 14.16`. Cross-checked via `docker image
inspect ... --format '{{json .RepoDigests}}'` on both `:latest` and the candidate `:v14.16` tag —
both resolved to the **identical** digest
`sha256:bea1c76a856fa39d1e542d25911cf95d02fe2bf971992d033044ff209f1504b8`, confirming `:v14.16` is
exactly the version this entire test suite (including the original HIGH-01 reproduction and the
new §3.6 confirmation) was validated against.

No test-only environment-variable override was added: no other disposable-integration test in
this repository (`tests/test_scenario_orchestration_v2.py`'s psycopg2-backed smoke test included)
uses that pattern, so none was introduced here, per the task's explicit condition ("only if the
repository already uses that pattern").

## 6. MEDIUM-03 — Integration skip semantics

**Before:** `_postgrest_image_available()` combined "can we reach Docker Hub" with the actual
image pull, gated behind `@unittest.skipUnless`, evaluated at **module import time** (i.e. before
`setUpClass`/any assertion ever runs) — broad `except Exception: return False` meant almost any
failure in that function silently skipped the whole class.

**After:**

- `_docker_available()` now checks **only** that the `docker` CLI exists on `PATH` and `docker
  info` succeeds — this is the sole condition allowed to skip the class via
  `@unittest.skipUnless`.
- Pulling the pinned image moved into `setUpClass` as its own first step,
  `cls._ensure_postgrest_image()`. This is now the **only** place in the entire class allowed to
  raise `unittest.SkipTest`, and only when the pinned image is genuinely absent locally **and**
  cannot be pulled (network/registry unavailable) — at this point in `setUpClass` no container or
  network has been created yet, so no cleanup is required before the skip.
- Every subsequent `setUpClass` step (`_start_postgres`, `_bootstrap_roles`, migrations, seeding,
  `_start_postgrest`, `_wait_for_postgrest_ready`) remains wrapped in the pre-existing
  `try/except Exception: cleanup(); raise` — any failure there is a real test **error**, never a
  skip, and cleanup still runs via that `except` block and via `tearDownClass`.

**Regression tests** (`TestIntegrationSkipSemantics`, no Docker required):

- `test_image_genuinely_unavailable_raises_skip_test_not_a_failure` — mocks `subprocess.run` to
  always fail and asserts `_ensure_postgrest_image()` raises `unittest.SkipTest`.
- `test_failure_after_image_check_propagates_and_still_cleans_up` — mocks
  `_ensure_postgrest_image` to succeed and `_start_postgres` to raise `RuntimeError`, then asserts
  `setUpClass()` re-raises that `RuntimeError` (not a skip) and that `_cleanup_containers` was
  still invoked.

## 7. LOW-01 / LOW-02 — test-quality gaps

Promoted the review's temporary probes to permanent tests (`TestReviewClosureLow01Low02`):

- **LOW-01:** `data=None` passthrough for `call_start_or_resume_scenario_attempt_v1` and
  `call_submit_scenario_decision_v1` (returns `None`, does not raise); `data=None` for
  `load_attempt_snapshot` (fails closed with `ScenarioSupabasePortV2MalformedResponseError`). The
  HIGH-01 `PGRST202` shape itself is now permanently covered by §3.5/§3.6 above.
- **LOW-02:** `load_attempt_snapshot(user_email="not-an-email", ...)` and
  `load_attempt_snapshot(user_email="", ...)` both complete without raising and forward the value
  to `client.rpc(...)`'s `p_user_email` parameter completely unchanged — machine-verifying "zero
  client-side email validation" rather than relying on code inspection alone.

## 8. Error contract verification

Confirmed by inspection and by the existing/new tests that no public port API leaks a raw
PostgREST `APIError`, Supabase SDK exception, HTTP error, timeout/connection exception, database
URL, JWT, service-role token, SQL text, relation/function name, schema-cache detail, stack trace,
or host/port. `KeyboardInterrupt` and `SystemExit` are not caught anywhere in `_classify_rpc_exception`
or `_call_rpc` (both only catch `Exception`, never `BaseException`) —
`test_control_flow_exceptions_not_swallowed` / `test_system_exit_not_swallowed` (pre-existing,
still passing) confirm this directly.

## 9. Test results

```
pytest tests/test_scenario_supabase_port_v2.py tests/test_scenario_orchestration_v2.py \
       tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py \
       tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q
```

- **Before this pass:** 569 passed, 9 subtests passed.
- **After this pass:** **605 passed, 45 subtests passed** (36 new subtests all from
  `test_3_every_approved_business_prefix_preserved_verbatim`'s 36-entry loop over the full
  approved-prefix allowlist).

`tests/test_scenario_supabase_port_v2.py` alone: **81 passed, 36 subtests passed** (up from 45
passed / 0 subtests), including both real disposable-PostgREST integration tests (original
CAS/idempotency flow + new HIGH-01 real-server confirmation). Docker containers/network confirmed
removed after every run (`docker ps -a` / `docker network ls` empty for the
`certbound-v2-port-smoke-*` names).

Temporary probes (outside the repository, under the OS temp directory, deleted immediately after
use) independently re-confirmed: approved-prefix verbatim preservation, unknown-prefix
sanitization, `PGRST202` sanitization, stack-trace sanitization, SQL-bearing sanitization,
credential-bearing sanitization, authentication/permission/timeout/transport classification, and
`__cause__` preservation — 11/11 passed.

## 10. Files

- **Modified:** `utils/scenario_supabase_port_v2.py`, `tests/test_scenario_supabase_port_v2.py`,
  `docs/scenario_simulator/SCENARIO_ENGINE_V2_SUPABASE_PORT_IMPLEMENTATION_REPORT.md`.
- **Created:** this report.
- **Untouched:** SQL/migrations/schema/RLS/grants, Engine V2 controller (not yet implemented),
  Streamlit UI, Engine V1 (`utils/scenario_persistence.py`, `utils/scenario_learner_controller.py`),
  the orchestration protocol (`utils/scenario_orchestration_v2.py` was read but not modified — no
  incompatibility was found; only its `_RPC_ERROR_PREFIX_MAP` was read to derive the port's
  allowlist).

## 11. Acceptance criteria checklist

| # | Criterion | Result |
|---|---|---|
| 1 | HIGH-01 closed | ✅ |
| 2 | Remaining blockers: 0 | ✅ |
| 3 | Remaining HIGH findings: 0 | ✅ |
| 4 | Unknown errors fail closed with safe generic messages | ✅ |
| 5 | Only approved business prefixes remain verbatim | ✅ |
| 6 | PGRST202 and schema-cache errors sanitized | ✅ (both synthetic and real-server) |
| 7 | Credentials, SQL, schema, relation, host, stack details cannot leak | ✅ |
| 8 | Original exceptions remain available through `__cause__` | ✅ (both response shapes) |
| 9 | Authentication/permission/timeout/transport classifications remain correct | ✅ |
| 10 | No automatic retries | ✅ (unchanged; re-tested) |
| 11 | Trusted server-side `user_email` boundary documented | ✅ |
| 12 | PostgREST image pinned | ✅ (`v14.16`, digest-matched to prior `:latest`) |
| 13 | Integration skip semantics fail-safe | ✅ |
| 14 | Real disposable PostgREST smoke passes | ✅ (both tests) |
| 15 | Engine V1 remains unchanged | ✅ |
| 16 | All focused tests pass | ✅ 605 passed / 45 subtests |
| 17 | No SQL/migration/schema/RLS/controller/UI changes | ✅ |
| 18 | Protected paths untouched | ✅ |
| 19 | Nothing staged, committed, pushed, or deployed | ✅ |

## 12. Recommended next task

Re-review only the delta (HIGH-01 correction, MEDIUM-01/02/03, LOW-01/02 closures) against this
report and the diff of `utils/scenario_supabase_port_v2.py` / `tests/test_scenario_supabase_port_v2.py`,
rather than a full re-review, then proceed to local milestone commit staging for the Supabase
persistence port (mirroring the orchestration layer's own `SIM-PERSIST-V2-05C` /
`SIM-PERSIST-V2-05D` sequence) before beginning Engine V2 controller implementation.
