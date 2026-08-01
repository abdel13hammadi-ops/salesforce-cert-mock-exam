# Engine V2 Supabase Persistence Port — Implementation Report

**Task ID:** SIM-PERSIST-V2-06
**Baseline HEAD:** `00d2773` — Complete Engine V2 orchestration vertical slice
**Scope:** `utils/scenario_supabase_port_v2.py`, `tests/test_scenario_supabase_port_v2.py`, this report only.

> **Amended by SIM-PERSIST-V2-06B.** The independent review
> (`SCENARIO_ENGINE_V2_SUPABASE_PORT_FOCUSED_REVIEW.md`) found one HIGH finding (HIGH-01:
> unknown/unrecognized errors were preserved verbatim by a trust-by-default fallback) and several
> MEDIUM/LOW findings against the design originally described below. All were closed by a
> follow-up correction pass — see `SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md` for the
> full before/after detail. §4.3 and §6 below have been updated in place to describe the
> **corrected, current** design; the rest of this report (trust boundary, public API, RPC-call
> methods, `load_attempt_snapshot` projection, immutability) is unchanged by the correction pass
> and still describes the shipped behavior accurately.

## 1. Objective

Implement a production-quality, Supabase-client-backed concrete implementation of
`utils.scenario_orchestration_v2.ScenarioOrchestrationV2PersistencePort`, connecting the
already-committed Engine V2 orchestration service to the existing, unmodified
`start_or_resume_scenario_attempt_v1` / `submit_scenario_decision_v1` / `get_scenario_attempt_v1`
RPCs, without touching SQL, migrations, the orchestration protocol, the Engine V2 controller,
Streamlit UI, or Engine V1.

## 2. Trust-boundary discovery (drove the entire design)

Before writing any code, the V68/V69 migrations were re-read specifically for their
RLS/GRANT sections. Three facts fully determined this port's design:

1. `supabase/migrations/20260719130000_..._v68_....sql` (lines ~849–860) `REVOKE ALL` on
   `scenario_attempts`/`scenario_decisions` from `PUBLIC`, `anon`, `authenticated`, **and**
   `service_role` first, then re-grants only a minimum privilege set back to `service_role`
   alone.
2. Both tables have `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` with **zero** policies (no
   `auth.uid()`-based policy exists for either table — confirmed by an explicit repository
   comment: *"RLS is enabled on both new tables with ZERO anon/authenticated policies. No
   auth.uid()-based policy is added."*).
3. All four scenario-attempt RPCs are `GRANT EXECUTE ... TO service_role` only (revoked from
   `anon`/`authenticated`), and `get_scenario_attempt_v1` is `SECURITY INVOKER`.

Together these prove this schema is a **trusted-server-backend** design, not a per-session-RLS
design: ownership is enforced entirely *inside* each RPC's own `p_user_email` equality check
against `scenario_attempts.user_email`, never by Postgres deciding which rows a given database
session may see. This is exactly the "service-role-only access through a trusted server
backend" case the task explicitly anticipated and asked to be documented rather than silently
supported alongside an RLS model. It also confirms (independently re-discovered during the real
PostgREST integration run — see §6) that real hosted Supabase provisions its `service_role`
Postgres role with `BYPASSRLS` out of band; the migrations never need to grant that themselves.

Consequences baked into the implementation:

- The port **never** calls `client.table(...)` — every operation is a `client.rpc(...)` call,
  mirroring the exact discipline `utils/scenario_persistence.py`'s own docstring already
  documents for Engine V1 ("It also never writes directly to `scenario_attempts` or
  `scenario_decisions` — every mutation goes through `client.rpc(...)`, never
  `client.table(...)`"). This port extends that same discipline to reads.
- The constructor **requires** an already-authenticated client (no default parameter, no
  `client: Any = None` fallback like V1's `_resolve_client` uses) — raises `ValueError` if
  `None` is passed. This port never creates, resolves, or falls back to a default/global/admin
  client, unlike `utils.scenario_persistence._resolve_client`.
- `user_email` is forwarded to the RPCs verbatim as their trusted parameter; this port never
  evaluates it as an authorization decision itself.

## 3. Public API

```python
class SupabaseScenarioOrchestrationV2Port:
    def __init__(self, client: SupabaseRpcClientProtocol) -> None: ...
    def call_start_or_resume_scenario_attempt_v1(self, params: Mapping[str, Any]) -> Any: ...
    def call_submit_scenario_decision_v1(self, params: Mapping[str, Any]) -> Any: ...
    def load_attempt_snapshot(self, *, user_email: str, attempt_id: str) -> Dict[str, Any]: ...
```

This satisfies `ScenarioOrchestrationV2PersistencePort` exactly (structural `Protocol`, no
inheritance required). No unused methods were added.

## 4. Design decisions

### 4.1 RPC-call methods are thin and non-validating

`call_start_or_resume_scenario_attempt_v1` / `call_submit_scenario_decision_v1`:

- deep-copy the caller's `params` before sending (so the caller's own mapping — and every
  nested envelope/state object inside it — is never mutated by this port or by whatever the
  injected client does with it);
- call `client.rpc(name, params).execute()` with the exact, unmodified RPC name and params;
- extract `.data`, deep-copy it, and return it as-is (list, mapping, empty list, multi-row list
  — whatever shape the SDK returned).

They deliberately **do not** pre-validate response shape (empty/multi-row/wrong-type). That
validation already lives in `utils.scenario_persistence_v2.parse_start_or_resume_rpc_response_v2`
/ `parse_submit_decision_rpc_response_v2` (`_require_single_row`), which already handles list-of-
one, bare-mapping, empty-list, and multi-row forms correctly. Duplicating that logic here would
only create a second place that could silently drift out of sync with the first.

### 4.2 `load_attempt_snapshot` resolves to one row itself, then projects

Unlike the two RPC-call methods, the orchestration protocol requires `load_attempt_snapshot` to
return a single `Mapping` (not a raw RPC list) — `utils.scenario_orchestration_v2._parse_attempt_snapshot_row`
takes `row: Mapping[str, Any]` directly. So this port:

1. Calls `get_scenario_attempt_v1` via `_call_rpc`.
2. Resolves the response to exactly one row itself, raising a typed error for zero rows
   (`ScenarioSupabasePortV2NoAttemptRowError`) or more than one row
   (`ScenarioSupabasePortV2MultipleAttemptRowsError`) — deliberately worded `malformed_response:`
   rather than reusing the RPC's own `attempt_not_found:` prefix, so this purely-defensive,
   should-never-happen-in-real-use path can never be mistaken for that RPC's actual business
   rejection (which already raises its own `attempt_not_found:` exception before ever returning
   a row, and is instead classified by `_call_rpc`'s normal exception path).
3. **Projects** both the attempt row and every decision element down to an explicit, minimal,
   approved key set, before returning:
   - Attempt row → exactly `attempt_id, scenario_id, scenario_version_id, status,
     current_scene_id, next_sequence_number, serialized_engine_state, engine_version,
     scenario_content_sha256, decisions` — matching exactly what
     `_parse_attempt_snapshot_row` reads. `user_email`, `started_at`, `updated_at`,
     `completed_at`, `abandoned_at`, `terminal_ending_id`, `terminal_result_snapshot` (none of
     which orchestration needs) are dropped even though the RPC returns them.
   - Each decision → exactly `sequenceNumber, expectedSceneId, selectedOptionId` — the RPC
     already excludes `idempotency_key`/`request_fingerprint` by design (per its own comment),
     but additionally returns `stateBefore, stateAfter, resultingSceneId, isTerminal,
     terminalEndingId, createdAt`, none of which `load_canonical_scenario_decisions_v2` needs;
     this port drops them too.

   This projection is a second, independent guarantee (on top of the RPC's own already-reduced
   shape) that no unapproved column can ever reach orchestration through this port, even if the
   RPC's own return shape were ever accidentally widened in the future. A malformed shape (a
   non-list `decisions`, or a non-mapping decision element) is preserved as-is rather than
   coerced or dropped, so orchestration's own HIGH-01-hardened, fail-closed decision validation
   still gets the chance to reject it with its own typed error.

### 4.3 Error classification — fail-closed ALLOWLIST (corrected by SIM-PERSIST-V2-06B)

> **HIGH-01 (closed).** The design originally shipped here was a **blocklist**: any message that
> did *not* match a timeout/connection/permission/authentication marker was assumed to be a safe
> business error and preserved verbatim. This was proven unsafe against a **real** disposable
> PostgREST server — an unrecognized `PGRST202` "function not found in schema cache" error leaked
> the target schema, function, and parameter names verbatim. The classifier has been corrected to
> a fail-closed **allowlist**: verbatim preservation now requires a positive, exact,
> case-sensitive prefix match against a closed set; everything else is sanitized. See
> `SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md` for the full incident/fix writeup.

`_classify_rpc_exception` never lets a raw SDK/PostgREST exception escape a public method, and
follows one fixed, deterministic classification order:

| Order | Category | Trigger | Message policy |
|---|---|---|---|
| 1 | `ScenarioSupabasePortV2RpcError` | Message starts with (exact, case-sensitive, position-0 match) one of the 36 entries in `_APPROVED_BUSINESS_ERROR_PREFIXES` | **Verbatim preserved.** This closed set is a plain, hardcoded copy of `utils.scenario_orchestration_v2._RPC_ERROR_PREFIX_MAP`'s prefixes (kept deliberately uncoupled at import time; `TestApprovedPrefixSetSync` structurally guards against drift) — every prefix the V68/V69 RPCs can genuinely raise. Safe to preserve verbatim because it originates exclusively from this repository's own committed migration SQL text. |
| 2 | `ScenarioSupabasePortV2AuthenticationError` | Auth-related SQLSTATE/PostgREST codes or "jwt"/"unauthorized"/... markers | Generic, sanitized text only |
| 3 | `ScenarioSupabasePortV2PermissionError` | SQLSTATE `42501` or "permission denied"/"insufficient_privilege" markers | Generic, sanitized text only |
| 4 | `ScenarioSupabasePortV2TransportError` (timeout) | `timeout`/`timed out` markers | Generic, sanitized text only |
| 5 | `ScenarioSupabasePortV2TransportError` (connection) | `connection refused`/`could not connect`/... markers | Generic, sanitized text only |
| — | `ScenarioSupabasePortV2MalformedResponseError` (+ 2 subclasses) | Response *shape* (not a raised exception) this port cannot interpret at the `load_attempt_snapshot` boundary — classified separately, not part of this ordered list | This port's own fixed text only |
| 7 | `ScenarioSupabasePortV2UnknownError` (new) | Anything not matched above — unrecognized PostgREST codes (`PGRST202`, ...), stack-trace-shaped text, empty messages, non-string payloads, or any other unanticipated shape | **Fail-closed sanitized default.** Deliberately never includes the raw message/code/details — this is the branch HIGH-01 was found in and is now the safe default rather than the unsafe one. |

Step 1 is checked first specifically so a genuine business message can never be shadowed by one
of the fuzzy substring heuristics in steps 2–5 (regression-tested by
`test_21_business_prefix_wins_even_if_message_also_contains_*`). Steps 2–5's marker-matching logic
is unchanged from the original implementation — only their position in the order (now explicitly
documented and tested) and the introduction of step 7 as a distinct type changed.

A response returned via `.error` (rather than a raised exception) is wrapped in an internal
`_RpcErrorCarrier` so it flows through the identical classification path; this path now also
chains `__cause__` to that carrier (previously it did not — closed alongside HIGH-01).

Because the orchestration layer's own `_map_persistence_exception` catches *any* `Exception`
raised by the injected port and classifies purely by extracted message-prefix text (never by
Python exception type), this design requires no coupling between the two modules: the port never
imports `utils.scenario_orchestration_v2`, and the orchestration layer never imports this port
module — only the `Protocol`'s structural shape connects them.

### 4.4 Immutability

- Outbound: every `params` mapping is `copy.deepcopy`'d before being handed to
  `client.rpc(...)`, so the caller's own object (and every nested envelope) is never mutated by
  this port or the underlying SDK.
- Inbound: `.data` is `copy.deepcopy`'d immediately after extraction, before any further
  processing or return, so the returned value shares no mutable structure with the raw SDK
  response object in either direction.
- `load_attempt_snapshot`'s projection builds a brand-new `dict`/`list` from deep-copied leaf
  values — no aliasing to the original row/decision mappings.

## 5. Unit tests (`tests/test_scenario_supabase_port_v2.py`)

44 deterministic-fake-backed tests covering the full A–AH checklist (constructor validation;
exact RPC names/param counts/param immutability; list/empty/multi-row response passthrough for
the two RPC-call methods; verbatim-vs-sanitized error classification for known-prefix,
unknown, raw-SDK, timeout, permission, and authentication failures; control-flow exception
(`KeyboardInterrupt`/`SystemExit`) passthrough; trusted-attempt-ID filtering; approved-column
projection for both the attempt row and each decision row; zero/multiple-attempt-row rejection;
ascending-order preservation; empty-decision-list acceptance; nested alias isolation in both
directions for serialized state, terminal result, and decision rows; no environment-variable
read (source-text assertion); no global client (two independent port/client pairs never share
state); no automatic retry (exactly one `.rpc()` call per attempt); Engine V1 files unchanged
and their tests still importable; and error-message scrubbing of embedded connection strings,
JWTs, and internal relation names for the sanitized categories) plus one additional
`TestPortSatisfiesOrchestrationProtocolEndToEnd` test that drives the **real, committed**
`start_or_resume_scenario_run_v2` → `submit_scenario_decision_v2` →
`resume_and_replay_scenario_run_v2` → idempotent-retry → stale-conflict flow through this port,
backed by `tests.test_scenario_orchestration_v2.FakeOrchestrationPersistence`'s already-validated
CAS/idempotency business logic wrapped behind a raw `.rpc(name, params).execute()` surface —
proving genuine protocol compatibility, not just structural type-matching.

## 6. Disposable integration test — REAL PostgREST, not psycopg2

Per the task's explicit instruction not to "weaken the test by pretending direct psycopg2 is the
Supabase SDK port," this task used the actual `postgrest` Python package's
`SyncPostgrestClient` (the same client `supabase-py` wraps internally for `.rpc(...)` calls —
`supabase-py` 2.31.0 and `postgrest` 2.31.0 were already present in the environment) against a
**real, disposable PostgREST server**, which in turn talks to a **real, disposable Postgres 16**
container.

> **MEDIUM-01 (closed).** The Docker image was originally `postgrest/postgrest:latest`, a
> floating tag. It is now pinned to `postgrest/postgrest:v14.16` — the exact version `:latest`
> resolved to at implementation time (`docker run --rm --entrypoint postgrest
> postgrest/postgrest:latest --version` → `PostgREST 14.16`; confirmed identical via
> `RepoDigests` on both tags: `sha256:bea1c76a856fa39d1e542d25911cf95d02fe2bf971992d033044ff209f1504b8`).
> A future PostgREST release can no longer silently change this test's behavior. No test-only
> environment-variable override was added (no other disposable-integration test in this
> repository uses that pattern).
>
> **MEDIUM-03 (closed).** Skip semantics were tightened: `_docker_available()` now only checks
> that the `docker` CLI exists and the daemon responds. Pulling the pinned image is now a
> `setUpClass` step (`_ensure_postgrest_image`) that raises `unittest.SkipTest` with an explicit
> reason ONLY when the image genuinely cannot be obtained (no container/network exists yet at
> that point) — every other failure (migrations, RPC, replay, assertions) propagates as a real
> test failure/error, never a skip. `TestIntegrationSkipSemantics` regression-tests both halves of
> this contract without needing Docker itself.

`TestSupabasePortDisposablePostgrestSmoke` (gated by `@unittest.skipUnless` on Docker
CLI/daemon availability only) does the following, fully automated inside
`setUpClass`/`tearDownClass`:

1. Creates a throwaway Docker network joining two fresh containers.
2. Starts a disposable `postgres:16` container.
3. Bootstraps `anon`, `authenticated`, `service_role` (`NOLOGIN`) and a login `authenticator`
   role (PostgREST's connecting role), granting it membership in all three.
4. **Grants `service_role` the `BYPASSRLS` attribute explicitly.** This was a genuine discovery
   made *during* this task: real hosted Supabase provisions `service_role` with `BYPASSRLS` out
   of band (this repository's migrations never need to, and don't, grant it — see §2). A bare
   `postgres:16` container does not set this automatically, so without this one line the first
   real-RPC call failed with `scenario_version_not_found:` even though the row existed and
   `service_role` had the relevant table `SELECT` grant — RLS-enabled-with-zero-policies still
   hides all rows from a non-owner, non-`BYPASSRLS` role regardless of `GRANT`s. Adding this one
   line made every subsequent RPC call succeed exactly as on real Supabase.
5. Applies the exact, unmodified V66–V69 migrations (byte-identical files from `supabase/migrations/`).
6. Seeds one published scenario/version via direct SQL (a superuser-only, test-fixture-only
   step, not part of the port itself).
7. Starts a disposable `postgrest/postgrest:v14.16` (pinned) container pointed at that Postgres
   via the `authenticator` role, with `PGRST_DB_ANON_ROLE=anon` and a throwaway HS256
   `PGRST_JWT_SECRET`.
8. Mints a `service_role`-claimed JWT (via `PyJWT`, already present) signed with that same
   secret — exactly how a real trusted server backend would authenticate to Supabase.
9. Constructs `postgrest.SyncPostgrestClient(base_url, headers={"Authorization": f"Bearer
   {token}"})` and wraps it in `SupabaseScenarioOrchestrationV2Port`.
10. Runs the real, committed orchestration public API end-to-end through that port: start
    (`created=True`), submit (`sequence_number=1`), resume/replay (recomputed
    `expected_sequence_number` matches), idempotent same-key retry (`idempotent_replay=True`,
    no duplicate row), and a stale re-submission (`ScenarioOrchestrationV2SequenceConflictError`
    raised, fail-closed).
10b. **(Added by SIM-PERSIST-V2-06B.)** A second test,
    `test_real_postgrest_unknown_function_error_is_sanitized`, calls the real, existing
    `start_or_resume_scenario_attempt_v1` RPC with a deliberately wrong parameter name, which
    causes real PostgREST to return a genuine `PGRST202` "function not found in schema cache"
    error — the exact real-world shape HIGH-01 was found against — and asserts the port raises
    `ScenarioSupabasePortV2UnknownError` with no schema/function/parameter detail in the message
    and `__cause__` populated.
11. `tearDownClass` always removes both containers and the network, even if `setUpClass` failed
    partway through (`setUpClass` wraps its own body in `try/except` that tears everything down
    and re-raises).

This test was run manually step-by-step first (to discover and fix the `BYPASSRLS` gap) and then
as the automated, permanent `pytest` test — **both passed**, and container/network cleanup was
verified via `docker ps -a`/`docker network ls` afterward (empty). It is part of the regular
`pytest tests/test_scenario_supabase_port_v2.py` run — no separate/manual step is required to
reproduce it (as long as Docker and outbound access to Docker Hub are available).

## 7. Files

- **Created (SIM-PERSIST-V2-06):** `utils/scenario_supabase_port_v2.py`,
  `tests/test_scenario_supabase_port_v2.py`, this report.
- **Created (SIM-PERSIST-V2-06B correction pass):**
  `docs/scenario_simulator/SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md`.
- **Modified (SIM-PERSIST-V2-06B correction pass):** `utils/scenario_supabase_port_v2.py`,
  `tests/test_scenario_supabase_port_v2.py`, this report (§4.3, §6, §7, §8 above/below).
- **Untouched:** Engine V2 controller, Streamlit UI, all SQL/migrations, Engine V1
  (`utils/scenario_persistence.py`, `utils/scenario_learner_controller.py`), the orchestration
  protocol (`ScenarioOrchestrationV2PersistencePort` was not changed — no incompatibility was
  found).

## 8. Test results

Original (SIM-PERSIST-V2-06):
`pytest tests/test_scenario_supabase_port_v2.py tests/test_scenario_orchestration_v2.py tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q`
→ **569 passed, 9 subtests passed** (524 baseline + 44 new unit tests + 1 real disposable
PostgREST integration test).

After the SIM-PERSIST-V2-06B correction pass, the same command →
**600 passed, 45 subtests passed** (569 baseline + 31 new/updated tests, including one additional
real disposable PostgREST integration test proving genuine `PGRST202` sanitization end-to-end).
See `SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md` for the itemized breakdown.
