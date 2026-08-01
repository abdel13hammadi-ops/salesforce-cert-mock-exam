# Engine V2 Supabase Persistence Port — Focused Production-Readiness Review

**Task ID:** SIM-PERSIST-V2-06-REVIEW-01
**Reviewed baseline HEAD:** `00d2773` — Complete Engine V2 orchestration vertical slice
**Files under review:** `utils/scenario_supabase_port_v2.py`, `tests/test_scenario_supabase_port_v2.py`,
`docs/scenario_simulator/SCENARIO_ENGINE_V2_SUPABASE_PORT_IMPLEMENTATION_REPORT.md`
**Review type:** Read-only + deterministic-fake unit probes + real disposable Docker/Postgres/PostgREST
probes. No source, test, SQL, or migration file was modified.

---

## Readiness decision

## **CORRECTIONS_REQUIRED**

One new HIGH finding was independently discovered and empirically confirmed against a real,
disposable PostgREST server. Everything else reviewed — protocol compatibility, client injection,
RPC parameter fidelity, trusted attempt/decision loading, immutability, no-auto-retry, Engine V1
isolation, and the real PostgREST integration itself — is sound and did not require correction.

- Blockers: **0**
- New HIGH findings: **1** (HIGH-01)
- Remaining HIGH findings from prior tasks: **0** (none exist; this is the first review of this module)
- MEDIUM findings: **3** (MEDIUM-01, MEDIUM-02, MEDIUM-03)
- LOW findings: **2** (LOW-01, LOW-02)
- **Total findings: 6**

---

## Findings

### HIGH-01 — Unrecognized non-business PostgREST/SDK errors are preserved verbatim by default (fail-open classification)

**Where:** `utils/scenario_supabase_port_v2.py`, `_classify_rpc_exception` (lines 253–281).

**Claim under test:** The module docstring and implementation report both justify verbatim
preservation of the `ScenarioSupabasePortV2RpcError` bucket on the grounds that *"every such
message originates from this repository's own committed migration SQL text, never from
externally-supplied or credential-bearing content."*

**What was found:** `_classify_rpc_exception`'s actual logic is a **blocklist**, not an
**allowlist**: it checks the extracted message/code against three finite marker sets (timeout,
connection, permission, authentication) and — if none match — falls through to:

```253:281:utils/scenario_supabase_port_v2.py
def _classify_rpc_exception(rpc_name: str, exc: BaseException) -> ScenarioSupabasePortV2Error:
    ...
    if message:
        # Preserved verbatim -- this is expected to be a Postgres
        # `RAISE EXCEPTION` business error ...
        return ScenarioSupabasePortV2RpcError(message)
    return ScenarioSupabasePortV2RpcError(f"unknown_error: RPC {rpc_name!r} failed with no extractable error message")
```

This bucket is **not actually limited to genuine repo-owned `RAISE EXCEPTION` messages** — it
catches *any* exception whose extracted text/code fails to match the blocklists, including
PostgREST-level and API-level errors that never touch this repository's SQL at all.

**Empirical proof (real PostgREST, disposable Docker environment, cleaned up afterward):**

```
RAW exception type: APIError
RAW .message: 'Could not find the function public.definitely_does_not_exist_xyz_v1(p_foo) in the schema cache'
RAW .code: 'PGRST202'
RAW .details: 'Searched for the function public.definitely_does_not_exist_xyz_v1 with parameter p_foo ...'

CLASSIFIED type: ScenarioSupabasePortV2RpcError
CLASSIFIED message (reaches the caller verbatim): 'Could not find the function public.definitely_does_not_exist_xyz_v1(p_foo) in the schema cache'
Is RpcError (verbatim-preserved bucket)? True
```

`PGRST202` ("function not found in schema cache") is PostgREST's real, well-documented response
for any RPC-name/parameter mismatch — a realistic future event (e.g. a client/server version skew,
a schema-cache reload race after a deploy, or a typo'd RPC name), **not** a contrived edge case.
It leaked the schema name (`public`), function name, and parameter name verbatim through the
"verbatim-preserved business error" path — exactly the category (*"schema/table/relation names"*)
this task's own error-message-safety probe list calls out as unsafe to expose.

A static cross-check against every `RAISE EXCEPTION ... USING ERRCODE = ...` in
`20260719130000_..._v68_....sql` and `20260719140000_..._v69_....sql` confirms the actual
business-error SQLSTATEs this schema ever raises through the RPC (not the trigger-guard-only
codes, which are unreachable through this port since it never calls `client.table(...)`) form a
small, closed, already-known set: `invalid_parameter_value` (22023), `no_data_found` (P0002),
`integrity_constraint_violation` (23000), `unique_violation` (23505), `internal_error` (XX000).
None of these currently collide with the permission/authentication marker sets (confirmed by
grepping every `RAISE EXCEPTION` string in both migrations for the exact marker text — zero
matches), so **no currently-reachable business error is misclassified today**. The defect is
architectural, not a currently-triggered incident: *any future or unanticipated
non-business error automatically inherits "safe to show verbatim" status merely by not matching
the blocklist*, which is the wrong default for an error-sanitization boundary.

**Why HIGH:** This directly matches this task's own explicit HIGH trigger — *"unknown database
failures are swallowed [i.e. mis-trusted]"* / *"sensitive database error details are exposed to
learner-facing code"* — and was demonstrated against a real server, not merely reasoned about.

**Recommended correction (for a future, narrowly-scoped task):** Invert the default. Only
preserve a message verbatim when there is positive evidence it is a genuine repo-owned business
error — e.g. `.code` is a member of the small closed SQLSTATE set actually used by these
migrations' business `RAISE EXCEPTION`s (as enumerated above, expressed as PostgREST's numeric or
symbolic SQLSTATE depending on what the SDK surfaces), **or** the message matches one of
`utils.scenario_orchestration_v2`'s own known business-prefix patterns (`sequence_mismatch:`,
`attempt_not_found:`, `scene_mismatch:`, etc. — already an authoritative, enumerable list).
Everything else — including any message that doesn't match — should fall through to a new,
generic, sanitized `unknown_error:` message (mirroring the existing "no extractable message"
branch), never the raw text. This is additive to the existing typed-exception hierarchy; no
existing exception class needs to change shape, only the fallback branch's decision logic.

---

### MEDIUM-01 — Disposable integration test pins `postgrest/postgrest:latest`, not a fixed tag

**Where:** `tests/test_scenario_supabase_port_v2.py`, `TestSupabasePortDisposablePostgrestSmoke._start_postgrest`
(and `_postgrest_image_available`).

Confirmed by direct inspection (lines 721–746, 921) that the disposable integration test always
pulls/uses `postgrest/postgrest:latest`. Re-running the exact same test in this review (see
"Independent probes / re-execution" below) shows it currently resolves successfully and the
resulting server's behavior matches everything the implementation assumes (RPC semantics,
`.data`/error shapes, `PGRST202` format, etc.). This is a **MEDIUM**, not HIGH, finding per this
task's own classification guidance: it creates a non-reproducible validation baseline (a future
PostgREST major-version bump could silently change error-response shapes, potentially reopening
or altering HIGH-01's exact trigger condition, without any code change in this repository to flag
it), but it does not currently invalidate the SDK/PostgREST-compatibility claim — the test today
genuinely exercises real `postgrest-py` + real PostgREST + real Postgres, and I independently
re-ran it in this review and it passed cleanly with proper cleanup (see below).

**Recommendation:** Pin to a specific, currently-compatible tag (e.g. a `postgrest/postgrest:v12.x.y`
release digest) for future CI reproducibility, with a deliberate, reviewed upgrade path when
bumping it.

---

### MEDIUM-02 — `user_email` trust-boundary warning is accurate but less explicit than V1's equivalent docstring

**Where:** `utils/scenario_supabase_port_v2.py` module docstring and `load_attempt_snapshot` docstring.

The module correctly documents that the injected **client** must be an already-authenticated,
trusted, server-side `service_role` client (never a browser session) — this is accurate, matches
the V68/V69 RLS/GRANT reality exactly (independently re-verified in this review, see
"Authentication and ownership boundary" below), and is not itself a defect.

What is *not* equally explicit is a parallel, unmissable statement that the **`user_email` string
itself** must be sourced from an already-authenticated session by whichever caller eventually
wires a real controller to this port — the docstring states only that this port "never evaluates
it as an authorization decision itself," which is true but passive. `utils/scenario_persistence.py`
(V1) carries a more affirmative instruction for the equivalent parameter (callers must obtain it
from "the existing verified-session access-control layer," never unauthenticated/browser-supplied
input). Nothing in this port's *code* is unsafe today — there is no client-side ownership shortcut
anywhere in this module, confirmed by full read-through — but the next task (controller
integration) is exactly the moment an unauthenticated value could get wired in by mistake, and the
current docstring doesn't proactively guard against that as strongly as V1's does.

**Recommendation:** Add one explicit sentence to the module docstring and/or
`load_attempt_snapshot`'s docstring stating that `user_email` must be obtained by the caller from
an already-authenticated session (mirroring V1's phrasing), before this port is wired into a
controller.

---

### MEDIUM-03 — Integration-test skip logic can silently mask an unrelated Docker/environment failure

**Where:** `tests/test_scenario_supabase_port_v2.py`, `_postgrest_image_available` (lines 721–746).

```721:746:tests/test_scenario_supabase_port_v2.py
def _postgrest_image_available() -> bool:
    try:
        subprocess.run(["docker", "image", "inspect", "postgrest/postgrest:latest"], ...)
        return True
    except Exception:
        pass
    try:
        subprocess.run(["docker", "pull", "postgrest/postgrest:latest"], ...)
        return True
    except Exception:
        return False
```

Both `except Exception` clauses are broad and silent (no logged reason). This is deliberate per
the task's own "escape valve" instruction (skip, never fail, when Docker/network access is
unavailable) and is *correct* behavior for that specific case. The residual risk is narrower than
"integration never runs": if Docker is genuinely installed and was previously working but degrades
(daemon crashed, disk full, corrupted image cache, unrelated `docker` CLI error), this same code
path silently reports "image unavailable" and the entire `TestSupabasePortDisposablePostgrestSmoke`
class is skipped rather than failed — which could, over time in CI, quietly erode the "real
SDK-level validation ran" guarantee without a visible signal distinguishing "expected offline
environment" from "broken local Docker install." In this review's own environment, Docker was
available and the real integration test was independently re-run and **passed** (see below), so
this is a test-quality/CI-signal recommendation, not a current defect.

**Recommendation:** Log the caught exception's text (to stderr/test output) before returning
`False`, so a skip caused by an unexpected local Docker failure is visually distinguishable from a
skip caused by genuine environment unavailability, without changing pass/fail semantics.

---

### LOW-01 — A few realistic response/error shapes are verified only by this review's temporary probes, not by permanent tests

Confirmed via 18 independent temporary probes (all passed, see below) that the following already
work correctly, but none of them exist as permanent regression tests in
`tests/test_scenario_supabase_port_v2.py`:

- `data=None` (null) for `call_start_or_resume_scenario_attempt_v1` / `call_submit_scenario_decision_v1`
  (correctly passed through as `None` without raising — downstream adapter parser rejects it).
- `data=None` for `load_attempt_snapshot` (correctly fails closed with
  `ScenarioSupabasePortV2MalformedResponseError`).
- A SQL-statement-bearing and an internal-host:port-bearing error message for the sanitized
  categories (correctly scrubbed).
- The exact HIGH-01 scenario (a `PGRST202`-shaped unrecognized error), which should gain a
  permanent regression test once HIGH-01 is corrected, asserting the *new* sanitized behavior.

**Recommendation:** Promote these probes to permanent unit tests in a future correction pass
(trivial additions; no design changes required beyond HIGH-01's own fix).

---

### LOW-02 — No permanent test proves the port performs zero client-side `user_email` validation

The design (correctly, per the trust-boundary discussion above) forwards `user_email` verbatim
with no client-side format/ownership check. This is true by inspection (no such validation code
exists anywhere in the module) but is not independently pinned by a test that, e.g., passes an
obviously malformed string (`"not-an-email"`) through `load_attempt_snapshot` and asserts it
reaches `client.rpc(...)` completely unchanged rather than being rejected or normalized locally.

**Recommendation:** Add one such test alongside the MEDIUM-02 documentation fix, so the
"verbatim forwarding, zero authorization logic" claim is machine-verified, not just
docstring-asserted.

---

## Review-area-by-review-area results

### 1. Protocol compatibility — **PASS**

Directly diffed the port's public surface against the protocol definition:

```187:202:utils/scenario_orchestration_v2.py
class ScenarioOrchestrationV2PersistencePort(Protocol):
    def call_start_or_resume_scenario_attempt_v1(self, params: Mapping[str, Any]) -> Any: ...
    def call_submit_scenario_decision_v1(self, params: Mapping[str, Any]) -> Any: ...
    def load_attempt_snapshot(self, *, user_email: str, attempt_id: str) -> Mapping[str, Any]: ...
```

The port implements exactly these three methods with matching signatures (`load_attempt_snapshot`
returns `Dict[str, Any]`, a valid `Mapping[str, Any]` subtype). No missing method, no extra public
method, no orchestration logic duplicated (response-shape validation is deliberately left to
`utils.scenario_persistence_v2`'s existing parsers, per the module's own documented rationale), no
controller/UI concern present. Field-name alignment was independently re-verified against
`_parse_attempt_snapshot_row` and `load_canonical_scenario_decisions_v2` — every key the port
projects (`attempt_id`, `scenario_id`, `scenario_version_id`, `status`, `current_scene_id`,
`next_sequence_number`, `serialized_engine_state`, `engine_version`, `scenario_content_sha256`,
`decisions`, and per-decision `sequenceNumber`/`expectedSceneId`/`selectedOptionId`) is exactly
what the orchestration-side parsers read — zero drift.

### 2. Client injection — **PASS**

`__init__` raises `ValueError` on `None`, stores the client on a private `_client` attribute never
exposed publicly, reads no environment variables (confirmed both by source inspection and by the
module's own `test_ad_module_never_reads_environment_variables` test), creates no global/default
client, and has no fallback path. No exception class or public method ever references or embeds
`self._client`, so no token/credential retained on the injected client object can leak through
this port's own objects or error messages.

### 3. Authentication and ownership boundary — **PASS** (production trust boundary frozen; see MEDIUM-02)

Independently re-verified the V68 migration's RLS/GRANT reality directly:

```849:860:supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql
REVOKE ALL ON TABLE public.scenario_attempts FROM PUBLIC;
... FROM anon; ... FROM authenticated; ... FROM service_role;
GRANT SELECT, INSERT, UPDATE ON TABLE public.scenario_attempts TO service_role;
GRANT SELECT, INSERT ON TABLE public.scenario_decisions TO service_role;
```

Both tables: RLS enabled, zero policies, all four scenario-attempt RPCs `GRANT EXECUTE ... TO
service_role` only. This **is** exactly a trusted-server-backend design (never a per-session-RLS
design), exactly as the port's docstring states. The port never calls `client.table(...)`, only
`client.rpc(...)`, for both reads and writes — confirmed by full source read. `user_email` is
forwarded verbatim, never evaluated as an authorization decision by this port. No client-side
ownership substitute exists anywhere in the module. **Production client boundary is frozen as:**
this port must only ever be constructed with an already-authenticated `service_role` client
obtained by a trusted server-side process out of band (e.g. a Streamlit backend holding the
service-role key), never a browser-supplied/anon/authenticated-role session. See MEDIUM-02 for the
one documentation-completeness gap found (the `user_email`-sourcing half of this same boundary is
correct but less forcefully stated than the client-sourcing half).

### 4. Start RPC — **PASS**

Exact name (`start_or_resume_scenario_attempt_v1`), exactly the seven caller-supplied params
forwarded unchanged (deep-copied outbound, confirmed both by unit tests B/C and this review's
alias-isolation probes), no automatic retry (confirmed by `test_af_no_automatic_retry_on_failure`
and by `_FakeSupabaseClient`'s own "no auto-retry allowed" assertion design), and this review's own
probes confirm business-prefix messages (`sequence_mismatch:`, `attempt_id_conflict:`) survive
classification verbatim and unmodified.

### 5. Submit RPC — **PASS**

Exact name (`submit_scenario_decision_v1`), exactly thirteen params forwarded unchanged, no
automatic retry, idempotency/stale-state prefixes (`idempotency_key_conflict:`,
`sequence_mismatch:`, `scene_mismatch:`) preserved verbatim per the same classification path as
the start RPC (there is only one shared `_call_rpc`/`_classify_rpc_exception` implementation), and
unknown failures fail closed to a typed exception (modulo HIGH-01's verbatim-vs-sanitized nuance —
they still fail closed as an *exception*, they just aren't always sanitized text).

### 6. Attempt snapshot loading — **PASS**

`p_attempt_id` and `p_user_email` both forwarded exactly as received; approved-column projection
independently re-verified against `_APPROVED_ATTEMPT_ROW_KEYS`; zero rows raise
`ScenarioSupabasePortV2NoAttemptRowError`, multiple rows raise
`ScenarioSupabasePortV2MultipleAttemptRowsError` (both independently reproduced by this review's
probes); nested `serialized_engine_state` is deep-copied (probe confirmed multi-level nested-list
alias isolation in both directions); raw SDK response object never escapes (only its
deep-copied `.data` payload is touched, and only after `.error` has already been checked).

### 7. Canonical decision loading — **PASS**

Decisions arrive bundled inside the single `get_scenario_attempt_v1` call (no separate query to
filter independently — confirmed this is by design, not an oversight, since the RPC itself already
scopes decisions to the requested attempt). Ascending order is *preserved*, never re-sorted
(`_project_decision_rows` iterates the RPC's own list order unchanged — independently re-read).
Only the approved key triple crosses the boundary. Empty list is accepted for a new attempt.
Malformed rows (non-list `decisions`, non-mapping element) are deep-copied and passed through
**unmodified and unmasked** rather than silently coerced/dropped/repaired — confirmed by direct
source read of `_project_decision_rows`'s explicit else-branch and its docstring, which exists
precisely so orchestration's own HIGH-01-hardened decision validation (from the prior
SIM-PERSIST-V2-05B task) still gets to reject it with its own typed error.

### 8. Response normalization — **PASS**

All eight named shapes were exercised (list, mapping, null, empty list, multi-row, raised
exception, `.error`-attribute response) across the existing unit tests plus this review's own
probes (`data=None` for both RPC-call methods and for `load_attempt_snapshot`, mapping `.data` for
submit). No shape is ever silently treated as success when it should not be — `.error` is checked
before `.data` is even read, and `load_attempt_snapshot`'s own row-resolution step positively
requires a `Mapping` before returning anything.

### 9. Error classification — **CORRECTIONS REQUIRED (HIGH-01)**

Known business prefixes remain available to orchestration (verbatim, confirmed). Timeout,
connection, permission, and authentication all correctly classify and sanitize (confirmed via unit
tests K–P and this review's own probes). Raw SDK exception objects never escape (`isinstance`
checks in this review's probes confirm the returned exception is never the original SDK exception
type). Causal chaining is preserved (`from exc` on every classification raise, confirmed by
`test_m_raw_supabase_exception_does_not_escape`'s `__cause__` assertion). **However**, "unknown
failures fail closed" is only true in the sense that an *exception* is always raised (never a
false success) — it is **not** true in the stronger sense the docstring implies, that unknown
failures are *sanitized*. See HIGH-01.

### 10. Error-message safety — **PASS for classified categories; see HIGH-01 for the unclassified fallback**

Independently probed (beyond the existing AH unit tests) with: a full `postgres://user:pass@host:port/db`
connection string, a JWT-shaped token, a SQL-relation-name-bearing permission error, an
internal `host:port` string, and a stack-trace-shaped message. Every one of these was correctly
scrubbed **when it happened to match a timeout/connection/permission/authentication marker**. The
stack-trace probe was deliberately constructed to match *none* of those markers, and — as
predicted and as HIGH-01 describes — it was preserved verbatim. This is the same root cause as
HIGH-01, demonstrated from the "message content" angle rather than the "real PostgREST error
code" angle.

### 11. Error-heuristic robustness — **CORRECTIONS REQUIRED (HIGH-01); no other collision found**

Grepped every `RAISE EXCEPTION` string in both `20260719130000_..._v68_....sql` and
`20260719140000_..._v69_....sql` (over 90 distinct business/precondition/postcondition messages)
for literal overlap with every marker in `_TIMEOUT_MARKERS`, `_CONNECTION_MARKERS`,
`_PERMISSION_MARKERS`, and `_AUTHENTICATION_MARKERS`: **zero matches**. Cross-checked every
`USING ERRCODE = ...` clause attached to a *reachable-through-the-RPC* business exception (as
opposed to trigger-only direct-table-mutation guards, which this port never reaches since it never
calls `client.table(...)`) against `_PERMISSION_CODES`/`_AUTHENTICATION_CODES`: also zero matches.
**No current business error is misclassified as authentication/permission/transport today.** The
finding is the inverse direction — see HIGH-01 — and is the one place this heuristic's design is
not robust to *future or unanticipated* errors, empirically confirmed against a real server.

### 12. Alias isolation — **PASS**

Re-ran and extended the existing Y–AC unit tests with an independent multi-level probe (list
containing a dict, three levels deep) mutating both the source and the returned copy in both
directions after the call returns: no shared mutable structure in either direction, in either the
`load_attempt_snapshot` path or the raw RPC-call paths.

### 13. No auto-retry — **PASS**

`_call_rpc` calls `self._client.rpc(...).execute()` exactly once per invocation, with no loop,
recursion, or retry helper anywhere in the module. `_FakeSupabaseClient.rpc` itself raises an
`AssertionError` ("no auto-retry allowed") if called more times than responses were queued for a
given RPC name, and the full suite (including the real-PostgREST-backed
`TestPortSatisfiesOrchestrationProtocolEndToEnd` and `TestSupabasePortDisposablePostgrestSmoke`
classes) passes under that constraint.

### 14. Real PostgREST integration — **PASS (independently re-executed in this review)**

Re-ran `TestSupabasePortDisposablePostgrestSmoke` in isolation in this review:

```
tests/test_scenario_supabase_port_v2.py::TestSupabasePortDisposablePostgrestSmoke::test_real_postgrest_start_submit_resume_idempotency_and_conflict PASSED [100%]
1 passed in 4.61s
```

Confirmed via `docker ps -a`/`docker network ls` immediately afterward that **no container or
network remained** — cleanup ran correctly. Also independently reused this exact scaffolding
(`setUpClass`/`tearDownClass`) in a throwaway review-only script to run the HIGH-01 probe against
the same real, disposable Postgres+PostgREST stack, and confirmed teardown succeeded there too.
Both real Docker containers, the join network, production credentials (absent by construction —
only throwaway, rotated-per-run local secrets are used), unmodified V66–V69 migrations, and the
genuine `postgrest.SyncPostgrestClient`/`postgrest.APIError` types were all independently
reconfirmed, not merely re-read from the prior task's report.

### 15. BYPASSRLS test bootstrap — **PASS**

`ALTER ROLE service_role BYPASSRLS;` exists only inside
`TestSupabasePortDisposablePostgrestSmoke._bootstrap_roles`, a test-only classmethod that only
ever runs against the test's own just-created, uniquely-named disposable container
(`certbound-v2-port-smoke-pg`) reached over `127.0.0.1:{PG_HOST_PORT}` — it cannot reach any other
database. No production port code anywhere alters roles, grants, or permissions; the reviewed
production module (`utils/scenario_supabase_port_v2.py`) contains no DDL/role-management statements
at all. The docstring's explanation of *why* this line is necessary (real hosted Supabase grants
`BYPASSRLS` to `service_role` out of band; a bare `postgres:16` container does not) was
independently corroborated by the RLS/GRANT re-read in review area 3.

### 16. Protected-path incident — **CLEAN (process finding recorded)**

Path-level-only check (per this task's explicit instruction not to inspect `.local/` contents)
confirmed no residual artifact from the prior task's temporary `.local/_manual_seed_fixture.py`
incident exists:

```
git status --porcelain=v1 --untracked-files=all -- .local | Select-String "_manual_seed_fixture|_manual_port_smoke"
(no output)
```

`.local/` itself remains untracked in git status identically to the pre-task baseline (it is an
unrelated, pre-existing protected path, not something newly created by this task). All temporary
artifacts created during the prior implementation task and during this review were created under
`%TEMP%` and removed immediately after use. **Process finding (no technical defect):** this
confirms the corrective action taken mid-task (moving the seed script out of `.local/` after the
initial mistake) was durable and left no trace — worth noting explicitly in this review's record
since the original task summary flagged the incident.

### 17. Engine V1 isolation — **PASS**

`grep -r "scenario_supabase_port_v2"` across the repository returns matches only in the port's own
implementation report and its own test file — no V1 source file, controller, or UI file references
it. `utils/scenario_persistence.py` and `utils/scenario_learner_controller.py` are byte-identical
to their pre-task state (untouched by this task, confirmed by `git status` showing no modification
markers against tracked files). The full baseline+new suite (569 tests, 9 subtests) passes,
including all pre-existing V1 tests unchanged.

### 18. Test quality — **CORRECTIONS RECOMMENDED (LOW-01, LOW-02, MEDIUM-03)**

45 tests collected and independently re-run (confirmed via `pytest --collect-only`). Strengths:
tests assert exact RPC method names and exact param-key sets (not just "was called"), alias
isolation is tested bidirectionally with real mutation after the call returns (not merely
`assertIsNot`), and `TestPortSatisfiesOrchestrationProtocolEndToEnd` genuinely drives the real,
committed public orchestration API rather than testing the port in isolation. Gaps found (see
LOW-01, LOW-02, MEDIUM-03 above): missing permanent tests for null `.data` on the two RPC-call
methods, missing a permanent test for the exact HIGH-01 shape, missing an explicit
"`user_email` is never validated client-side" test, and skip-vs-fail semantics for the Docker
image-availability check.

### 19. Docker image pinning — **MEDIUM (MEDIUM-01)**

See MEDIUM-01 above.

### 20. Independent probes — **18/18 passed in this review**

All probes were written to a throwaway script under `%TEMP%` (never inside the repository) and
deleted immediately after each run. Summary (raw pass/fail output is reproduced inline above where
most informative):

| # | Probe | Result |
|---|---|---|
| 1 | Unknown exception → typed `RpcError`, message preserved | PASS |
| 2 | Timeout exception → `TransportError` | PASS |
| 3 | Permission error → `PermissionError`, relation name scrubbed | PASS |
| 4 | Authentication error → `AuthenticationError`, timestamp scrubbed | PASS |
| 5 | Known RPC prefix via `.error` attribute → verbatim `RpcError` | PASS |
| 6 | Nested (3-level) alias isolation, both directions | PASS |
| 7 | Null `.data` for RPC-call methods (no crash, passthrough) | PASS |
| 7b | Null `.data` for `load_attempt_snapshot` (fails closed) | PASS |
| 8 | Mapping `.data` for submit RPC normalized | PASS |
| 9 | Multiple-row attempt response rejected | PASS |
| 10 | Credential-bearing (`postgres://user:pass@host`) URL scrubbed | PASS |
| 11 | SQL-bearing + embedded-PII permission error scrubbed | PASS |
| 12 | Internal `host:port` string scrubbed (transport path) | PASS |
| 13 | Stack-trace-shaped unknown exception → preserved verbatim (confirms HIGH-01) | PASS (behavior confirmed, flagged as HIGH-01) |
| 14 | Real PostgREST `PGRST202` unknown-function error → preserved verbatim (confirms HIGH-01) | PASS (behavior confirmed, flagged as HIGH-01) |
| 15 | Real-PostgREST end-to-end smoke test re-run in isolation | PASS |
| 16 | Disposable Docker/network cleanup verified after both real-PostgREST runs | PASS |
| 17–18 | Protected-path (`.local/`) residue check (path-level only) | PASS (clean) |

All temporary artifacts (`_review_probes_supabase_port_v2.py`,
`_review_probes_2_supabase_port_v2.py`, `_review_probe_pgrst_unknown_fn.py`) were created under
`C:\Users\Abdel\AppData\Local\Temp\` and deleted at the end of each probe session — confirmed
removed.

---

## Required correction sequence (recommended next task)

1. **HIGH-01 (required before milestone commit):** Invert `_classify_rpc_exception`'s fallback
   default from "trust unless blocklisted" to "sanitize unless allowlisted," using the closed set
   of business SQLSTATEs/prefixes enumerated above. Add a permanent regression test reproducing
   the exact `PGRST202` shape captured in this review (a fake exception is sufficient; the real
   PostgREST re-run in this review already proves the shape is realistic).
2. **MEDIUM-02:** Add one explicit `user_email`-sourcing sentence to the module docstring, mirroring
   V1's phrasing, before controller integration begins.
3. **MEDIUM-01 / MEDIUM-03 / LOW-01 / LOW-02:** Address opportunistically in the same pass (all are
   small, additive, and do not touch the exception hierarchy's public shape or the RPC contracts).
4. Re-run the full focused suite
   (`tests/test_scenario_supabase_port_v2.py tests/test_scenario_orchestration_v2.py
   tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py
   tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q`) and confirm
   569+ tests / 9 subtests still pass, plus the new HIGH-01 regression test.
5. Re-review only the delta (this document's HIGH-01/MEDIUM-02 sections) rather than a full
   re-review, once corrected.

Only after step 1 (and ideally 2) closes should this module be considered
`READY_FOR_LOCAL_MILESTONE_COMMIT`.
