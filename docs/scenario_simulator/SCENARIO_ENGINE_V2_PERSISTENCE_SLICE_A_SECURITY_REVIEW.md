# SCENARIO_ENGINE_V2 Persistence — Slice A Independent Security Review

**Task ID:** SIM-PERSIST-V2-02B
**Model:** Sonnet High
**Baseline:** `6136673` — Complete Scenario Engine V2 vertical slice
**Scope:** Independent review only. No SQL applied, no database connection, no executable migration created, no source/test file modified.
**Reviewed artifacts:**
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md` (parent design)
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md` (Slice A contract)
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_MIGRATION_DRAFT.sql`
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_ROLLBACK_DRAFT.sql`

**Independently re-verified against:**
`supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql` (current production contract, read line-by-line), `supabase/tests/v68_scenario_attempt_persistence_verification.sql`, `supabase/tests/v68_scenario_attempt_persistence_preflight.sql`, `utils/scenario_persistence.py`, `utils/scenario_engine_v2.py`, `tests/test_scenario_persistence.py`, `tests/integration/ba201_supabase_lab.py`, plus a repository-wide search for every remaining unprotected reference to `start_or_resume_scenario_attempt_v1` (`pages/Scenario_Simulator.py`, `utils/scenario_learner_controller.py`, `tests/test_scenario_learner_controller.py`).

---

## Executive summary

The Slice A drafts are **substantially correct** and the core architectural decision — `DROP` the exact old signature, `CREATE` the new one, restore grants/comment, `NOTIFY pgrst` — is verified, independently, to be the objectively required approach; `CREATE OR REPLACE FUNCTION` alone would break every existing caller via PostgREST's `PGRST203` ambiguity error, exactly as the contract claims. Line-by-line comparison against the current production function found **zero unintentional behavioral deviations** in the preserved logic. However, this review identifies **one HIGH finding in the SQL draft** (an unverified, load-bearing assumption about `GET STACKED DIAGNOSTICS ... CONSTRAINT_NAME` behavior for a partial unique index, which the task itself correctly flagged as a specific risk to check) and **one HIGH finding in the Python serialization contract** (a real type mismatch between the documented envelope shape and the actual Engine V2 `ScenarioRunV2Snapshot.decisions` field type, which could leak internal evaluation/debrief data into the persisted envelope if implemented literally). Both are precisely scoped, concretely correctable, and do not require a redesign. Several MEDIUM/LOW findings round out completeness and defense-in-depth. **Readiness decision: CORRECTIONS_REQUIRED** — the two HIGH findings must be resolved (empirical verification or a safer design substitution) before this proceeds to an executable migration.

---

## Area 1 — Current function baseline

**Result: CONFIRMED, exact match, zero unintentional deviations.**

Independently re-read `supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql` lines 428-526 (table), 613-615 (partial index), 632-747 (insert/mutation guard trigger), 867-1167 (function+comment+grants), 859-860 (table grants). Verified against the Slice A contract and migration draft line by line:

| Property | Current (verified) | Draft's claim | Match? |
|---|---|---|---|
| Argument names/order | `p_user_email, p_scenario_version_id, p_initial_current_scene_id, p_initial_serialized_state, p_engine_version, p_scenario_content_sha256` | Identical, `p_attempt_id uuid DEFAULT NULL` appended | ✅ |
| Defaults | None | New: `p_attempt_id DEFAULT NULL` only | ✅ |
| Return columns/order | 15 columns, exact names/types listed at lines 875-890 | Copied verbatim | ✅ |
| Language | `plpgsql` | `plpgsql` | ✅ |
| Volatility | Not declared (defaults to `VOLATILE`) | Not declared (same default) | ✅ — correctly not changed; this function performs writes and must remain `VOLATILE` |
| `SECURITY INVOKER` | Yes (line 893) | Yes | ✅ |
| `search_path` | `public, pg_catalog` (line 894) | Identical | ✅ |
| Owner | Not independently determinable without a live DB (see Area 11 finding) | Not asserted | ⚠️ see SA-11-1 |
| Grants | `REVOKE ALL FROM PUBLIC/anon/authenticated` + `GRANT EXECUTE TO service_role` (lines 1164-1167) | Identical pattern for the new signature | ✅ |
| `COMMENT` | Present (lines 1140-1162) | Restored, updated to describe the new behavior | ✅ |
| Body behavior | Email normalize → version lookup/publish/hash checks → advisory lock → resume-`SELECT ... FOR UPDATE` → (if found) return → validate initial state → `INSERT ... ON CONFLICT DO NOTHING RETURNING id` → re-`SELECT ... FOR UPDATE` → return | Every line preserved verbatim except 4 documented, additive changes (new param; resume-branch conflict check; `v_new_id` source; `INSERT` exception handling replacing bare `ON CONFLICT DO NOTHING`) | ✅ scope-limited, each change independently justified in the contract §5 |
| Exception behavior | `invalid_user_email`, `invalid_scenario_version_id`, `scenario_version_not_found`, `scenario_version_not_published`, `engine_version_mismatch`, `content_hash_mismatch`, `invalid_initial_state*` (×9), `start_or_resume_failed` — all preserved verbatim | Adds `invalid_attempt_id`, `attempt_id_conflict`, `attempt_id_collision` — none of the existing exception names, wording, or `ERRCODE`s are altered | ✅ — confirmed no existing error string is renamed, reworded, or given a different `ERRCODE` |

**Two documentation-completeness NOTEs surfaced by this independent baseline pass (not present as explicit statements in the Slice A contract, though consistent with it):**

- **SA-01-1 (NOTE):** `scenario_attempts.id`'s column-level `DEFAULT gen_random_uuid()` (line 429) was already dead code for attempt creation even before Slice A — both the current and the new function always supply `id` explicitly in the `INSERT ... VALUES (v_new_id, ...)` list, so the column default is never actually invoked by this RPC in either version. No behavior change; worth one explicit sentence in the contract for completeness. Blocks migration/validation/deployment: No/No/No.
- **SA-01-2 (NOTE):** RLS is enabled with zero policies on both `scenario_attempts` and `scenario_decisions` (line 813-814), and table-level `GRANT SELECT, INSERT, UPDATE`/`GRANT SELECT, INSERT` to `service_role` (lines 859-860) are what actually make the `SECURITY INVOKER` RPCs functional when called by `service_role` — this depends on `service_role` carrying Supabase's standard `BYPASSRLS` attribute (a platform-level fact this review cannot verify without a live connection, and correctly out of scope for a "no database connection" review). Neither draft touches RLS, table grants, or role attributes, so this pre-existing dependency is unchanged and unaffected by Slice A. Recorded here as independent confirmation, not a new risk. Blocks migration/validation/deployment: No/No/No.

---

## Area 2 — Function replacement strategy

**Result: CONFIRMED CORRECT**, with one clarification and one operational caveat.

1. PostgreSQL permits `DROP FUNCTION` + `CREATE FUNCTION` for a different-arity signature inside one transaction — DDL is transactional in PostgreSQL; this is standard, supported behavior, not a special case.
2. **Dependent objects:** independently confirmed via full-repository search — no view, trigger, or other function in `supabase/migrations/` (any file) references `start_or_resume_scenario_attempt_v1`; it is called only from `utils/scenario_persistence.py`, `utils/scenario_learner_controller.py`, `pages/Scenario_Simulator.py` (documentation comment only, not a call), and the V68 SQL test files. A plain `DROP FUNCTION` (no `CASCADE`) will therefore succeed. **The draft correctly does not use `CASCADE`**, and correctly should not — if some undiscovered dependency existed, an unqualified `DROP FUNCTION` failing loudly is the safe outcome; `CASCADE` would silently destroy that dependency instead.
3. **No view/trigger/function/grant/test dependency is silently broken by the migration itself.** One test dependency (`supabase/tests/v68_scenario_attempt_persistence_verification.sql`) is broken **by the signature change**, exactly as the contract already discloses (§6) — not by the migration's mechanics.
4. Transactional DDL confirmed to hide intermediate state: a concurrent reader in a separate session sees either the pre-migration catalog (old function only, before this transaction commits) or the post-migration catalog (new function only, after commit) — never neither. This also correctly means a **failed** postcondition check (`RAISE EXCEPTION` before `COMMIT`) rolls the *entire* transaction back, restoring the six-argument function exactly, with zero observable effect to any other session (Area 10, race G, below).
5. `NOTIFY pgrst, 'reload schema';` is correctly placed **before** `COMMIT` in the draft. This is correct, not a bug: PostgreSQL defers actual delivery of a `NOTIFY` to listening backends until the issuing transaction commits — placing it before `COMMIT` inside the same transaction is the conventional, documented pattern, not a race condition.

**SA-02-1 (NOTE):** the contract's own §3.5 already discloses the brief `ACCESS EXCLUSIVE` lock window truthfully; this review found no additional locking behavior to add beyond what §3.5/§14 (Area 14, below) already states.

---

## Area 3 — PostgREST function resolution

**Result: CONFIRMED, grounded in the existing call style, not merely theoretical.**

- **Exactly one overload after migration:** guaranteed by the DROP-then-CREATE ordering (Area 2) plus the migration draft's own postcondition (`SELECT count(*) ... = 1`), independently re-verified as syntactically and semantically correct.
- **Six-key JSON RPC request resolves to the seven-argument function:** confirmed — this is exactly how PostgREST already resolves calls today for parameters with `DEFAULT`s elsewhere in this schema (the pattern is not new to this repository), and is exactly what the existing `utils/scenario_persistence.py::start_or_resume_attempt(...)` call — verified, at `_call_rpc(client, "start_or_resume_scenario_attempt_v1", {6 named keys})` — will continue to do unmodified.
- **Seven-key request resolves to the same function:** trivially true once `p_attempt_id` is included in the future V2 adapter's call dict.
- **Installed-PostgREST-version support for omitted optional parameters:** this specific repository's own `submit_scenario_decision_v1` grant/comment text and the general PostgREST overloading documentation both describe this as core, long-supported PostgREST behavior (not a recent/edge feature) — low residual risk, but genuinely unverifiable without knowing the exact deployed PostgREST version, which this review was not permitted to query. Recorded as **SA-03-1 (LOW):** confirm the deployed Supabase project's PostgREST version supports default-valued trailing parameters (it has for many major versions) as a one-line pre-deployment sanity check, not a design change. Blocks migration/validation/deployment: No/No/No.
- **Parameter names exactly match JSON RPC keys:** confirmed — all six existing names are unchanged; `p_attempt_id` is the new key, spelled identically in the contract's decision/documentation and the draft SQL.
- **Schema cache reload behavior:** `NOTIFY pgrst, 'reload schema'` is the documented, correct mechanism (Area 2, point 5).
- **`PGRST203` cannot occur after migration:** confirmed, conditioned entirely on the postcondition-verified "exactly one overload" invariant — if that invariant ever failed to hold (e.g., migration re-run, manual drift), `PGRST203` would return; the postcondition check is exactly what prevents this from ever being silently missed.
- **Stale schema-cache behavior and recovery:** the contract's §3.4 discloses this only briefly. This review adds the missing detail (Area 10, race H, below) — the exact, safe failure mode (`PGRST202`/`undefined_function`, cleanly retryable, no data corruption) for a request arriving in the brief window between `COMMIT` and the cache actually finishing its reload, which is a genuine but self-healing, non-corrupting risk window common to any PostgREST function-signature change (not unique to this migration). **SA-03-2 (LOW):** recommend the contract document explicitly name the expected client-visible error class (`PGRST202`) and confirm it is safely retryable, for operational runbook completeness. Blocks migration/validation/deployment: No/No/No.

---

## Area 4 — Positional and named caller compatibility / caller inventory

**Result: CONFIRMED compatible, with one inventory-completeness gap (inconsequential in practice).**

Full inventory, independently re-derived via repository-wide search for `start_or_resume_scenario_attempt_v1` (protected paths excluded):

| File | Call style | Hardcodes exact 6-arg identity? | Needs update for Slice B/C? |
|---|---|---|---|
| `utils/scenario_persistence.py` | Named JSON dict (6 keys) | No | No — compatible unmodified |
| `utils/scenario_learner_controller.py` | Delegates to `scenario_persistence.py` | No | No |
| `pages/Scenario_Simulator.py` | Documentation comment only (line 51), not a call | No | No |
| `supabase/tests/v68_scenario_attempt_persistence_verification.sql` | Positional, 6 args, ~12 call sites; **also** hardcodes `to_regprocedure('...(text,uuid,text,jsonb,text,text)')` / `has_function_privilege('...', '...(text,uuid,text,jsonb,text,text)', ...)` at 5 locations (lines 125, 352-353, 372, 390, 425) | **Yes** | **Yes** — already flagged in the Slice A contract §6 |
| `supabase/tests/v68_scenario_attempt_persistence_preflight.sql` | Checks function existence **by `proname` only** (lines 110-121, 124-136), not exact identity args | No | **No** — this file preflight-checks a *pre-V68* clean state (i.e., that nothing named this already exists); it is not re-run post-deployment and, even if it were, a name-only check remains valid regardless of arity. **Not mentioned in the Slice A contract's caller inventory (§1/§6)** — an inventory-completeness gap, but with **zero functional consequence**. |
| `tests/integration/ba201_supabase_lab.py` (`_EXPECTED_FUNCTIONS`, lines 1379-1418) | Checks function existence and per-role `has_function_privilege` **by `proname` only**, aggregated with `bool_or` across any overload | No | **No** — remains correct after migration; also not mentioned in the contract's inventory |
| `tests/test_scenario_persistence.py` (~30 call sites) | Mocked/fake Supabase client — asserts the RPC **name string** and the **set of dict keys** `utils/scenario_persistence.py` sends; never touches a real PostgreSQL function signature | No | **No** — structurally immune to this migration; these tests validate the Python adapter's own behavior, not RPC dispatch, and remain valid and unmodified regardless of the SQL signature change |
| `tests/test_scenario_learner_controller.py` | Same mocked-client pattern | No | No |

**SA-04-1 (LOW):** the Slice A contract's caller inventory (§1, §6) omits `supabase/tests/v68_scenario_attempt_persistence_preflight.sql` and `tests/integration/ba201_supabase_lab.py`. Both were independently confirmed to check function existence/grants **by name only** (not exact argument-type identity), so this omission has **no functional consequence** — neither file requires any change for this migration. Classified LOW rather than MEDIUM specifically because the impact is zero; this is a documentation-completeness gap only, corrected here. Blocks migration/validation/deployment: No/No/No.

**Files that must be updated when implementation begins (Slice B/C), confirmed complete list:**
1. `supabase/tests/v68_scenario_attempt_persistence_verification.sql` — update the 5 hardcoded six-type signature strings to the new seven-type signature, and add the new `p_attempt_id` test cases (Area 21).
2. No other file in this inventory requires modification.

---

## Area 5 — Caller-supplied attempt-ID security

**Result: CONFIRMED SAFE**, ownership boundary independently verified, with one LOW usability note.

- **UUID used only when inserting a genuinely new attempt:** confirmed — `p_attempt_id` is read in exactly two places in the draft: the resume-branch conflict check (only compares, never assigns) and `v_new_id := COALESCE(p_attempt_id, gen_random_uuid())` inside the create branch only.
- **Existing in-progress attempt resolved by ownership+scenario identity before `p_attempt_id` can affect anything:** confirmed by code order — the resume-branch `SELECT ... WHERE sa.user_email = v_user_email AND sa.scenario_version_id = p_scenario_version_id AND sa.status = 'in_progress'` runs, and its `FOUND`/not-`FOUND` branch is decided, **before** `p_attempt_id` is ever read for anything beyond that branch's own conflict check.
- **A caller cannot supply another user's attempt UUID and receive information about that attempt:** confirmed. Two exhaustive cases: (a) the other user's attempt is `in_progress` — the caller's own resume-branch lookup is scoped to `v_user_email` (the caller's own normalized identity) and will never find it, so the conflict-check branch (which only compares against the *caller's own* found row) is never reached for that row; the caller instead falls through to the create branch, attempts `INSERT ... id = p_attempt_id`, and hits the `scenario_attempts_pkey` violation → `attempt_id_collision`, a message that names no owner, email, scenario, or status. (b) the other user's attempt is `completed`/`abandoned` — same outcome (PK collision), since the resume-branch query's `status = 'in_progress'` filter would never have matched it anyway even for its true owner.
- **Conflict errors reveal no owner/email/scenario/status/existence detail:** independently re-read both new error messages verbatim — `'attempt_id_conflict: supplied p_attempt_id does not match the caller''s existing in_progress attempt for this scenario version'` and `'attempt_id_collision: the supplied p_attempt_id is already in use'` — neither interpolates `%` with any row data (unlike several pre-existing messages in this same function, e.g. `scenario_version_not_found: scenario_versions % does not exist`, which do interpolate an already-caller-supplied value, not a secret). Confirmed no information disclosure by message content.
- **Timing/control-flow side channel:** considered explicitly, as instructed. A cross-owner collision (Area 5, case (a)) and a same-owner nil/malformed check both fail relatively early and cheaply; a genuine collision against an existing row still requires one full `INSERT` attempt (index probe) to detect, versus a same-owner conflict which is detected by an already-materialized `SELECT ... FOR UPDATE` result. These paths have different costs, but neither leaks *which* case occurred through timing alone in any way an attacker could act on beyond what the error *message itself* already discloses (nothing) — this is judged a theoretical, not practical, side channel, and not unique to this migration (the existing scenario-version/email-format checks already have non-uniform cost profiles). **SA-05-1 (NOTE)**, no correction required.
- **Nil UUID rejected:** confirmed, `p_attempt_id = '00000000-0000-0000-0000-000000000000'::uuid` check present, runs before any lookup.
- **Malformed UUIDs rejected before body execution:** confirmed — `p_attempt_id uuid` is a typed SQL parameter; PostgreSQL/PostgREST perform the `text → uuid` cast during argument binding, before the function body's first statement runs, for a malformed string.
- **Supplied UUID cannot modify an existing row:** confirmed — the only two code paths that read `p_attempt_id` are a **comparison** (resume conflict check, never a write) and an **insert value** (create branch only, and only for a row that does not yet exist by definition of reaching that branch, given the PK guarantees no pre-existing row shares that id or the insert would fail). There is no `UPDATE ... WHERE id = p_attempt_id` anywhere in this function.
- **Completed/abandoned attempts cannot be reopened through UUID reuse:** confirmed structurally — the resume-branch `SELECT` filters `status = 'in_progress'` unconditionally (unchanged from today), so a `p_attempt_id` matching a `completed`/`abandoned` row can never route into the resume branch at all; it can only ever reach the create branch, where it collides on the `PRIMARY KEY` and is rejected — it is never "reopened," merely blocked.

**SA-05-2 (LOW/NOTE):** `attempt_id_conflict` never reveals the caller's own correct existing attempt id, even though it is the caller's own data with no cross-owner disclosure risk in this specific branch (the compared row is always already scoped to the caller). A future usability improvement (not a security requirement) would let the Python adapter self-correct in one round trip by including the correct id in the error `DETAIL`. Non-blocking. Blocks migration/validation/deployment: No/No/No.

---

## Area 6 — Start idempotency

**Result: CONFIRMED for the primary claim; ONE test-plan gap identified (not an SQL defect).**

| Case | SQL draft's actual behavior | Matches contract's claim? |
|---|---|---|
| Same UUID, same owner, same scenario version, first request already committed, retried | Resume branch finds the row, `p_attempt_id` equality check passes, returns identical row, `created=false` | ✅ Yes — genuinely idempotent |
| Same UUID retry after response timeout (request committed, response lost) | Identical to above — the retry's own resume-branch `SELECT` finds the just-committed row | ✅ Yes |
| Same UUID, different owner | Caller's own resume-branch scoped by `v_user_email` never finds the original row; falls to create branch; `PRIMARY KEY` collision → `attempt_id_collision` | ✅ Fails safely, not falsely "idempotent" |
| Same UUID, different scenario version (same owner) | Caller's own resume-branch scoped by `p_scenario_version_id` never finds the original row (different version in `WHERE`); falls to create branch; `PRIMARY KEY` collision → `attempt_id_collision` | ✅ Fails safely — **but see SA-06-1 below: this exact path is not yet its own explicit test case** |
| Same UUID, different initial scene (same owner+version, resuming) | Resume branch is reached first; `p_initial_current_scene_id`/`p_initial_serialized_state` are — exactly as in the pre-existing, unchanged contract — never consulted on the resume branch; the persisted state wins unconditionally | ✅ Safe, matches pre-existing V1 behavior, unrelated to the `p_attempt_id` change itself |
| Same UUID, different content hash / engine version (same owner+version) | These are validated **unconditionally, before the resume/create branch decision**, against the *scenario version's own pinned* values (not the existing attempt's stored values) — a mismatch here is rejected with the pre-existing `content_hash_mismatch`/`engine_version_mismatch` errors regardless of `p_attempt_id` or resume/create branch | ✅ Pre-existing logic, correctly untouched, correctly does not interact with the new parameter |
| Same UUID supplied after an existing in-progress row already exists under a *different*, previously auto-generated UUID | Resume branch finds the existing row (id = X); supplied `p_attempt_id` (= Y ≠ X) fails the equality check → `attempt_id_conflict` | ✅ Fails safely, does not silently "adopt" the new id or create a duplicate |

**SA-06-1 (MEDIUM):** the "same UUID, different scenario version, same owner" row above is a materially distinct code path from "duplicate UUID across owners" (it goes through a *different* combination of resume-branch miss reasons and lands on the exact same `attempt_id_collision` outcome for a different underlying reason) and is not, today, its own explicit SQL test case in the contract's §13 (only tests 5 and 6 are adjacent, neither is this exact combination). **Required correction:** add this as its own explicit test case in Slice B/C's SQL test plan before implementation. This is a test-plan completeness gap, not a defect in the SQL itself (the behavior is already correct, per the table above). Blocks executable migration: No. Blocks disposable DB validation: No. Blocks production deployment: recommended, not strictly required, since the underlying behavior is already correct and exercised transitively by the existing collision test with a slightly different setup.

**Conclusion on the idempotency claim itself: the SQL draft does implement identical-request-binding idempotency for the one case that matters (same owner, same version, same id, retried) — the contract's claim is accurate, not overstated.**

---

## Area 7 — Existing active-attempt reuse

**Result: CONFIRMED, unchanged mechanism, correctly extended.**

- Existing in-progress attempt is returned before any insert is attempted: confirmed, unchanged code order (resume-branch `SELECT ... FOR UPDATE` runs strictly before the create-branch validation/insert).
- Supplied `p_attempt_id` must match that attempt's id: confirmed (Area 5/6).
- Omitted `p_attempt_id` retains current behavior: confirmed (`COALESCE(NULL, gen_random_uuid())` = today's `gen_random_uuid()`; the new conflict check's `IF p_attempt_id IS NOT NULL ...` guard is a no-op when `NULL`).
- Simultaneous starts, same learner/scenario, do not create duplicate active attempts: confirmed via the **unchanged** `pg_advisory_xact_lock(hashtext(user_email || ':' || scenario_version_id))` serialization, which runs before either branch is even considered, and is not read from or influenced by `p_attempt_id` at all.
- **Two different UUIDs supplied concurrently cannot bypass the partial unique index:** confirmed — the advisory lock already serializes the two callers to the same (user, version) key regardless of what `p_attempt_id` each supplies; whichever acquires the lock first will, in the overwhelming majority of cases, either find the other's just-created row on its own resume-branch `SELECT` (if the other has already committed) or successfully create its own row (if it is first). The narrow residual race this migration's exception handler defends against (both reach the `INSERT` before either's advisory lock fully serializes them — theoretically only reachable via an `hashtext()` collision between two *different* (user, version) keys, an existing, unchanged, pre-Slice-A characteristic) is handled correctly by the partial-index branch, **contingent on Area 8's finding below**.
- **Losing requests return or fail deterministically:** confirmed — a losing create-branch request either falls through cleanly to return the winner's row (`idx_scenario_attempts_one_in_progress` case) or fails closed with a named, ownership-safe error (`scenario_attempts_pkey` case) — never an ambiguous or silently-wrong result, contingent on Area 8.

---

## Area 8 — Unique-violation handling

**Result: PARTIALLY CONFIRMED — one HIGH finding.** This is the single most consequential open question in this review, and the task's own framing correctly anticipated it.

**What is independently confirmed from PostgreSQL's documented behavior and this repository's actual schema:**
- `scenario_attempts.id`'s `PRIMARY KEY` is declared inline (`id uuid PRIMARY KEY DEFAULT gen_random_uuid()`, line 429) with **no explicit constraint name** — PostgreSQL's default naming convention for an unnamed primary key is `<table>_pkey`, i.e. **`scenario_attempts_pkey`**, exactly as the draft assumes. This naming convention is deterministic and well-documented, not a guess.
- `idx_scenario_attempts_one_in_progress` (line 613) is confirmed, independently, to be created via a bare `CREATE UNIQUE INDEX IF NOT EXISTS idx_scenario_attempts_one_in_progress ON public.scenario_attempts (user_email, scenario_version_id) WHERE status = 'in_progress';` — a **partial** unique index with **no backing `pg_constraint` row** (it was never declared via `ALTER TABLE ... ADD CONSTRAINT`).
- A repository-wide check confirms `scenario_attempts` has **no other** unique-enforcing object besides these two (the two `UNIQUE` constraints on `scenario_decisions`, lines 547-551, are a different table and irrelevant here) — so the handler's `ELSE RAISE;` fail-closed branch for "any other unique constraint" is currently unreachable dead code in practice, but correctly present as defense-in-depth for a future schema change.

**What is NOT independently verified, and is the actual open risk:**
PostgreSQL's unique-violation error machinery is documented to populate the `constraint_name` diagnostic field (retrievable via `GET STACKED DIAGNOSTICS v_name = CONSTRAINT_NAME;`) with the causing **index's own name**, for both formally-declared `UNIQUE`/`PRIMARY KEY` table constraints *and* bare, standalone unique indexes (including partial ones) — this is well-precedented, commonly-relied-upon PostgreSQL behavior, not merely theoretical. **However, this review was explicitly barred from connecting to a database, and therefore cannot empirically confirm** that `GET STACKED DIAGNOSTICS` returns the exact literal strings `'scenario_attempts_pkey'` and `'idx_scenario_attempts_one_in_progress'` (unqualified, unquoted, with no schema prefix) on the specific PostgreSQL major version this Supabase project runs.

**Impact if the assumption is wrong, precisely distinguished by which string fails to match:**
- If the **primary-key** branch's string comparison fails: the `ELSE RAISE;` fallback re-raises the original, unmodified `unique_violation` — a caller-supplied-UUID collision still fails (fail-closed is preserved; no security regression, no silent wrong-data return), just with a less-friendly, generic Postgres error message instead of `attempt_id_collision`. **Low practical severity for this branch specifically.**
- If the **partial-index** branch's string comparison fails: an entirely *ordinary, benign, expected* concurrent-duplicate-start race (the exact case the original `ON CONFLICT DO NOTHING` was designed to swallow silently) would instead **raise an unhandled error to the calling application** — a genuine **functional regression** relative to today's production behavior, and a direct violation of this task's own readiness bullet "concurrent starts remain safe." **This is the higher-severity half of the risk**, and is exactly what the task's own hint — "a partial unique index may not behave like a named table constraint in exception diagnostics" — was warning against.

**Finding SA-08-1 (HIGH):**
- **File/section:** `SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_MIGRATION_DRAFT.sql`, the `EXCEPTION WHEN unique_violation THEN ... GET STACKED DIAGNOSTICS v_constraint_name = CONSTRAINT_NAME; IF v_constraint_name = 'scenario_attempts_pkey' THEN ... ELSIF v_constraint_name = 'idx_scenario_attempts_one_in_progress' THEN ...` block.
- **Evidence:** as above — a well-precedented but not-in-this-review empirically-verified assumption, explicitly called out as a risk by the task itself.
- **Impact:** worst case is a functional regression in the concurrent-duplicate-start path (raises instead of gracefully resuming); best case (and most likely, per general PostgreSQL knowledge) is that it works exactly as designed. No data-integrity or ownership-boundary failure mode exists in *either* outcome — this is a fail-closed-vs-fail-unfriendly distinction, not a fail-closed-vs-fail-open one.
- **Required correction (either is sufficient):**
  1. **Empirically verify** both exact `CONSTRAINT_NAME` values against a disposable database running the same PostgreSQL major version as the target Supabase project, as part of Slice C's SQL test plan (already specified as test case 8 in the contract — strengthen that test to assert the exact string value, not merely the overall outcome), **before** trusting this in production; or
  2. **Adopt a more robust design that does not depend on Postgres's exact diagnostic string formatting at all:** after catching `unique_violation` (regardless of which constraint), issue one additional, explicit `SELECT EXISTS (SELECT 1 FROM public.scenario_attempts WHERE user_email = v_user_email AND scenario_version_id = p_scenario_version_id AND status = 'in_progress')`. If `TRUE`, treat as the ordinary concurrent-race case (fall through to the re-`SELECT`, exactly as before). If `FALSE`, the conflict cannot have been the partial index (no in-progress row exists for this owner+version, by definition), so it must be the `PRIMARY KEY` — raise `attempt_id_collision` directly, without needing to string-match `CONSTRAINT_NAME` at all. This is safe because PostgreSQL's index-insertion conflict-checking blocks a concurrent inserter until the *other* transaction's outcome (commit or rollback) is known before raising `unique_violation` — so by the time this `SELECT` runs, if the partial-index race is what actually happened, the winner's row is guaranteed already visible.
- **Blocks executable migration:** No (the migration can be drafted with either the string-match approach — pending verification — or the safer re-query alternative).
- **Blocks disposable DB validation:** No — this is precisely the class of thing disposable-DB validation exists to confirm.
- **Blocks production deployment:** **Yes**, until either the exact strings are empirically confirmed or the safer re-query design (recommendation 2) is adopted.

---

## Area 9 — Ownership and information disclosure

**Result: CONFIRMED, no disclosure found.** Fully covered by Area 5's detailed walkthrough; restated here for completeness against this area's specific checklist:
- Whether a supplied UUID exists: not disclosed (both `attempt_id_conflict` and `attempt_id_collision` are worded identically regardless of whether the colliding/conflicting row belongs to the caller or another owner, or whether it is `in_progress`/`completed`/`abandoned`).
- Another user's email/scenario/status/content identity: never interpolated into any new error message (confirmed by reading both messages verbatim, Area 5).
- Timing/control-flow: considered explicitly (Area 5, SA-05-1), judged theoretical/non-actionable, consistent with this function's pre-existing non-uniform-cost error paths.

---

## Area 10 — Concurrency race analysis

| Race | Expected result | Confirmed? |
|---|---|---|
| **A.** Two calls, same owner/scenario, same supplied UUID | Advisory lock serializes them; the second sees the first's committed row on its own resume-branch `SELECT` (id matches, no conflict) → idempotent resume, `created=false` for the second | ✅ |
| **B.** Two calls, same owner/scenario, different supplied UUIDs | Advisory lock serializes them; the second's resume-branch `SELECT` finds the first's committed row; its own, *different* `p_attempt_id` fails the equality check → `attempt_id_conflict` | ✅ — correctly **not** silently merged or silently creating a second row |
| **C.** Two calls, different owners, same supplied UUID | Not serialized by the advisory lock (different hash key); both proceed to their own create branches; exactly one wins the `PRIMARY KEY` insert; the loser gets `attempt_id_collision`, revealing nothing about the winner | ✅ |
| **D.** Existing in-progress attempt + retry with matching UUID | Resume branch, equality check passes, idempotent | ✅ |
| **E.** Existing in-progress attempt + retry with conflicting UUID | Resume branch, equality check fails → `attempt_id_conflict` | ✅ |
| **F.** One transaction commits while another waits on the advisory lock | Standard, unchanged `pg_advisory_xact_lock` semantics — the waiter proceeds only after the holder's transaction ends (commit or rollback), then re-evaluates the resume/create decision fresh | ✅ — unaffected by Slice A |
| **G.** Transaction fails after `DROP`/`CREATE` but before `COMMIT` (this migration's own deployment, not an application-level race) | The **entire** migration transaction rolls back — `DROP FUNCTION` and `CREATE FUNCTION` are both undone atomically; the original six-argument function is left exactly as it was, as if the migration had never run. No other session ever observes an intermediate or missing-function state, because nothing was ever committed | ✅ — this is precisely why the draft's postcondition checks run *before* `COMMIT` rather than as a separate follow-up step |
| **H.** PostgREST request during schema-cache reload | A request using PostgREST's still-stale cache (describing the *old* six-argument signature) constructs a six-argument SQL call; PostgreSQL's live catalog (already migrated) has no such function anymore → `undefined_function` (SQLSTATE `42883`), surfaced by PostgREST as a `PGRST202`-class error. **Safely retryable, no data corruption, no partial write** — this is the expected, self-healing, universally-applicable failure mode for *any* PostgREST function-signature change during its cache-reload window, not unique to this migration. See SA-03-2. | ✅ — documented here in full for the first time; the Slice A contract only briefly alluded to this |

---

## Area 11 — Grants and security

**Result: CONFIRMED for everything except function ownership, which cannot be verified without a database connection.**

- `SECURITY INVOKER` preserved: ✅ (identical text, `SET search_path = public, pg_catalog` preserved: ✅).
- `REVOKE ALL FROM PUBLIC` / no `EXECUTE` for `anon` / no `EXECUTE` for `authenticated` / `EXECUTE` only for `service_role`: ✅, all four re-issued verbatim (with the updated 7-type signature) in the draft, and independently asserted by the draft's own postcondition block.
- No table grants changed: ✅ — the draft touches only the function object; `GRANT SELECT, INSERT, UPDATE ON TABLE public.scenario_attempts TO service_role` / `GRANT SELECT, INSERT ON TABLE public.scenario_decisions TO service_role` (lines 859-860 of the current migration) are never referenced or altered.
- No RLS policy added: ✅ — confirmed no `CREATE POLICY`/`ALTER TABLE ... (ENABLE|DISABLE) ROW LEVEL SECURITY` statement anywhere in either draft.
- No direct client write path appears: ✅ — `anon`/`authenticated` continue to have zero grants on both the function and the tables; the only path into `scenario_attempts` remains this one `SECURITY INVOKER` RPC, callable only by `service_role`.

**Finding SA-11-1 (MEDIUM):** `DROP FUNCTION` + `CREATE FUNCTION` sets the new function's owner to whichever role executes the `CREATE` statement — this is standard PostgreSQL behavior, not a defect, but it means the new function's owner is only *guaranteed* to match the old function's owner if the Slice B/C migration is applied through the exact same role/pathway the original V68 migration used (typically a fixed migration-runner role in Supabase, e.g. `postgres`). Neither draft verifies or pins this. Because the function is `SECURITY INVOKER` (not `DEFINER`), an unexpected owner has **no bearing on the function's own runtime privilege behavior** (it never executes "as" its owner) — this materially limits the severity relative to what it would be for a `SECURITY DEFINER` function. The residual concern is narrower: an unexpected owner changes *who may subsequently `ALTER`/`DROP`/re-`GRANT` on this specific function* without needing to be a superuser. **Required correction:** add a precondition step capturing the old function's `pg_proc.proowner` (e.g. via `RAISE NOTICE` or a captured variable) and a postcondition asserting the new function's owner is identical, or — more robustly — an explicit `ALTER FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid) OWNER TO <expected-role>;` immediately after `CREATE FUNCTION`, pinning the owner explicitly rather than relying on "whichever role happens to run this." Blocks executable migration: No. Blocks disposable DB validation: No. Blocks production deployment: **Yes**, until an explicit owner check or pin is added — this is a real, if narrow, gap relative to the readiness standard's "grants and SECURITY INVOKER behavior remain unchanged" bullet, which implicitly extends to "who controls this object" not just "what it does when invoked."

---

## Area 12 — Precondition and postcondition checks

**Result: CONFIRMED sufficient for signature-identity safety; ONE gap identified for definition-fingerprint safety.**

The draft's preconditions check: required V68 objects exist; the exact old six-type signature exists (`to_regprocedure`); no other overload of this name already exists; the new seven-type signature does not already exist. The postconditions check: exactly one overload exists; the old signature is absent; the new signature is present; `anon`/`authenticated` cannot execute; `service_role` can execute.

**Comparing only identity signatures is *not* fully sufficient** to guarantee "this migration will not silently overwrite an unknown newer migration's changes to this same function," because `to_regprocedure` matching the expected six-type identity says nothing about whether the function's *body* has already been modified by some other, undocumented change (e.g., a hotfix applied directly via the Supabase SQL editor, bypassing the tracked-migration history — which the current migration's own header already documents as having happened for V66/V67 in this exact project's history: *"V66 and V67 were manually applied to production, not through a tracked migration runner."*). **Finding SA-12-1 (MEDIUM):** given this project's own documented history of untracked manual hotfixes to this exact function family, a precondition that only checks the function's *identity signature* (not its *body*) cannot detect the case where the currently-installed six-argument function's body has silently diverged from the exact text this draft assumes it is replacing. **Required correction:** add a precondition computing `md5(pg_get_functiondef(to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)')))` (or equivalent) and comparing it against a known-good hash of the exact body this draft was written against, failing loudly on any mismatch rather than silently overwriting an unknown, possibly-hotfixed body. Blocks executable migration: No. Blocks disposable DB validation: No. Blocks production deployment: **recommended strongly, not strictly mandatory** if the Slice B/C implementer manually re-diffs the live production function body against this draft's assumed baseline immediately before deployment as a one-time manual step — but an automated fingerprint check is safer given this project's own history of untracked manual changes to this function family.

Everything else in this area (return shape, `SECURITY INVOKER`/`search_path` preservation, grants, "no overload remains") is independently confirmed adequately checked by the existing postcondition block (Area 11).

---

## Area 13 — Rollback correctness

**Result: CONFIRMED, byte-for-byte comparison performed.**

Compared `SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_ROLLBACK_DRAFT.sql`'s recreated function body, line by line, against `supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql` lines 867-1167 (the actual, current production text) — **identical**, including every comment, every `RAISE EXCEPTION` message and `ERRCODE`, the `ON CONFLICT DO NOTHING RETURNING id INTO v_new_id` pattern, and the original `COMMENT ON FUNCTION` text word-for-word.
- New (seven-arg) signature dropped exactly: ✅ (`DROP FUNCTION ...(text, uuid, text, jsonb, text, text, uuid)`).
- Old (six-arg) signature recreated exactly, original body restored: ✅.
- Original return shape restored: ✅ (never changed by the forward migration either, so this is trivially satisfied).
- Original grants and `COMMENT` restored: ✅, textually identical to lines 1140-1167.
- Original owner/search_path/security behavior: `search_path`/`SECURITY INVOKER` restored textually (✅); **owner restoration is subject to the identical caveat as SA-11-1** — rollback's `CREATE FUNCTION` will set the owner to whichever role executes the rollback, which should match the original pre-Slice-A owner only if applied through the same pathway. Same required correction as SA-11-1 applies symmetrically to the rollback draft.
- No data modified: ✅ — confirmed neither draft contains any `UPDATE`/`DELETE`/`INSERT` against `scenario_attempts`/`scenario_decisions` data rows; only function/comment/grant DDL.
- Six-argument callers work after rollback: ✅ (identical signature restored).
- Seven-argument callers fail after rollback: ✅, and fail in the *expected*, safe way — a call still supplying `p_attempt_id` after rollback fails PostgREST's own argument-to-function-name resolution (`PGRST202`/`undefined_function`), not a silent misinterpretation of the seventh argument.
- PostgREST cache reload triggered: ✅ (`NOTIFY pgrst, 'reload schema';` present, correctly before `COMMIT`).

**Same finding as SA-12-1 applies to the rollback's own precondition:** it checks the seven-arg signature exists and the six-arg does not, but not the seven-arg function's *body* — if some other change had been applied to the seven-arg function between forward-migration and rollback, the rollback would silently discard it without comment. Recorded jointly under SA-12-1 rather than duplicated as a new finding.

---

## Area 14 — Transaction and deployment window

**Result: CONFIRMED, the report's characterization is accurate and appropriately non-overstated.**

- **Active executions are not aborted:** a session already mid-execution of the old function (past its initial parse/plan/lock-acquisition step) is not retroactively cancelled by a concurrent `DROP FUNCTION` — this is standard PostgreSQL behavior for replacing any function, not specific to this migration.
- **New calls block, they do not fail, during the migration's own transaction:** a new call attempting to resolve/plan `start_or_resume_scenario_attempt_v1` while this migration's transaction holds its `ACCESS EXCLUSIVE` lock on the function's catalog row will wait for that lock, then proceed normally once the migration transaction ends (commit *or* rollback) — it does not receive an error due to the lock itself.
- **Transactional DDL hides intermediate state from other sessions:** confirmed (Area 2, Area 10 race G) — no other session can ever observe "function does not exist" as an intermediate state; it observes either the fully-old or fully-new catalog.
- **Is a low-traffic window necessary or merely prudent?** **Merely prudent**, not necessary for correctness — the transactional guarantees above mean no caller can observe corruption or an inconsistent function definition regardless of traffic level; a low-traffic window only reduces the *number* of callers that experience the brief (sub-second, typically) lock-wait and the (also typically sub-second to low-single-digit-second) `PGRST202` cache-staleness window (Area 10, race H), both of which are safely retryable, not corrupting.
- **Expected rollback-deployment behavior:** identical transactional guarantees apply symmetrically to the rollback script — it is exactly as safe to apply as the forward migration, for the same reasons.

No correction required for this area; the contract's own §3.5 already stated this accurately, and this review found nothing to add beyond the race-H detail already captured in Area 10.

---

## Area 15 — Engine V1 compatibility

**Result: CONFIRMED**, independently re-verified against actual callers and tests (Area 4's full inventory), with the one already-disclosed exception restated for completeness.

- Calls omitting `p_attempt_id` behave identically: ✅ (Area 4, Area 6).
- Generated-UUID behavior unchanged: ✅ (`COALESCE(NULL, gen_random_uuid())`).
- Existing active-attempt reuse unchanged: ✅ (Area 7).
- Return fields/JSON values unchanged: ✅ — `RETURNS TABLE` block copied verbatim, 15 columns, same names/types/order.
- **Existing error codes/messages relied upon by callers remain unchanged:** independently verified against `tests/test_scenario_persistence.py`'s ~30 mocked-client assertions (Area 4) — every existing error string (`invalid_user_email`, `scenario_version_not_found`, `scenario_version_not_published`, `engine_version_mismatch`, `content_hash_mismatch`, every `invalid_initial_state_*` variant, `attempt_insert_guard_violation`, `start_or_resume_failed`) is reproduced **verbatim, character-for-character** in the migration draft — none renamed, reworded, or given a different `ERRCODE`. **No changed error behavior for any existing case.** The only genuinely new error behavior is for the two entirely new failure modes (`invalid_attempt_id`, `attempt_id_conflict`, `attempt_id_collision`) that cannot be triggered by any Engine V1 caller, since none of them ever supplies `p_attempt_id`.
- No Engine V1 Python file requires modification: ✅, confirmed by the full caller inventory in Area 4 — none of `utils/scenario_persistence.py`, `utils/scenario_learner_controller.py`, `pages/Scenario_Simulator.py` needs any change.
- **Restated exception (already disclosed in the Slice A contract §6, independently re-confirmed here):** `supabase/tests/v68_scenario_attempt_persistence_verification.sql` hardcodes the old six-type signature string at 5 locations and will raise `undefined_function`/resolve `to_regprocedure(...)` to `NULL` if re-run after this migration — correctly scoped to Slice B (test file update), not this review or Slice A.

---

## Area 16 — Serialization contract

**Result: PARTIALLY CONFIRMED — one HIGH finding.**

Independently re-verified field names against `utils/scenario_engine_v2.py`, not merely trusted from the Slice A contract's own prose:

- `ScenarioDecisionInputV2(sequence_number: int, scene_id: str, option_id: str)` — confirmed exact match to the contract §8.3/§8.4/§10.
- `LearnerSceneView`/`LearnerOptionView`/`LearnerTerminalView` field names — confirmed exact match to the contract §8.5/§8.6 (independently re-read at `utils/scenario_engine_v2.py` lines 1696-1726).
- `verify_replay_identity_v2`, `replay_scenario_run_v2`, `_validate_decision_sequence_v2`, `start_scenario_run_v2`, `apply_decision_v2` all confirmed to exist with the names the contract's §8.7/§8.8 assumes.
- Authoritative-vs-cached separation (envelope groups A/B/C, contract §9): confirmed conceptually sound and consistent with the parent design's source-of-truth principle.
- Strict integer/finite-number/UUID rules: confirmed enforceable given the existing `utils/scenario_persistence.py::_require_strict_int/_require_uuid4_str/_require_nonempty_str` helpers the contract explicitly proposes mirroring — these helpers were independently re-confirmed to exist with those exact names and the described semantics.
- Thawing immutable structures (`frozenset` → sorted `list`, `MappingProxyType`/`Mapping` → `dict`, `tuple` → `list`) cannot alter semantics **provided the thaw is order-preserving where order is meaningful** — confirmed sound for `flags` (a `frozenset`, inherently unordered, sorting is a safe, deterministic canonicalization) and for `decisionHistory`/`optionDisplayOrderByScene` (already ordered `tuple`s in the engine, converted to ordered `list`s, order preserved) — no semantic-altering thaw identified.
- Deserialization never trusting derived cache as authoritative: confirmed by design (§8.2's explicit choice to return a non-authoritative `PersistedRunEnvelopeV2`, never a reconstructed `ScenarioRunV2Snapshot`, and §8.7's explicit statement that replay always starts from `content`'s own initial state, never a cached shortcut).

**Finding SA-16-1 (HIGH):**
- **File/section:** `SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md` §8.1 (`serialize_run_snapshot_v2`) and §9 (envelope `decisionHistory` field).
- **Evidence:** independently re-read `utils/scenario_engine_v2.py`, confirming `ScenarioRunV2Snapshot.decisions` is typed `tuple[DebriefTraceEntry, ...]` (line 1288-1289), **not** `tuple[ScenarioDecisionInputV2, ...]`. `DebriefTraceEntry` (lines 1224-1249) carries nine fields per decision: `sequence_number, scene_id, option_id, evaluation_tier, debrief_seed, state_delta, state_after, flags_cleared, flags_set, next_scene_id`, plus (per its own docstring) `presented_dialogue_variant_id`/`next_dialogue_variant_id`. The contract's §8.1 describes only generic "type conversion" (`frozenset → list`, `Mapping → dict`) and never explicitly instructs an implementer to **project** each `DebriefTraceEntry` down to exactly the three fields (`sequence_number, scene_id, option_id`) the envelope's own §9/§10 correctly documents as the minimal, authoritative decision shape.
- **Impact:** an implementer following §8.1 literally, without independently re-discovering this exact field-type mismatch (which required direct source inspection to find — it is not evident from the contract's prose alone), could plausibly iterate `run.decisions` and serialize each `DebriefTraceEntry`'s full field set into the persisted `decisionHistory` array — silently leaking `evaluation_tier`, `debrief_seed`, `state_delta`, `state_after`, `flags_cleared`, `flags_set`, `next_scene_id`, and dialogue-variant identifiers into a JSONB column the contract's own §9 groups under "A. Authoritative persisted identity and decisions" and explicitly never intends to carry derived/internal scoring data. This would violate the parent design's and this contract's own stated principle (decisions are meant to be a minimal, re-derivable record; evaluation/debrief data is meant to be recomputed on demand, never persisted) and could constitute an unintended internal-data leak if the envelope is ever exposed through any debug/admin/support tooling.
- **Required correction:** amend contract §8.1 to explicitly state: *"`serialize_run_snapshot_v2` MUST project each element of `run.decisions` (`tuple[DebriefTraceEntry, ...]`) down to exactly `{sequenceNumber: entry.sequence_number, sceneId: entry.scene_id, optionId: entry.option_id}`, explicitly discarding `evaluation_tier`, `debrief_seed`, `state_delta`, `state_after`, `flags_cleared`, `flags_set`, `next_scene_id`, `presented_dialogue_variant_id`, and `next_dialogue_variant_id` — none of these ever appear in the persisted envelope, in any form."* Add a corresponding Python contract test (Area 21) asserting this negatively (recursive key-walk of the persisted envelope must never contain any of these nine field names, mirroring the existing learner-safe-view exclusion test already specified in the contract's §14 item 12).
- **Blocks executable migration:** No (this is a Python/serialization-contract issue, unrelated to the SQL migration).
- **Blocks disposable DB validation:** No.
- **Blocks production deployment:** **Yes**, for the persistence-adapter implementation track specifically (Slice D) — the SQL migration track (Slices B/C) is unaffected and may proceed independently once Area 8's finding is resolved.

---

## Area 17 — Snapshot envelope

**Result: CONFIRMED reasonable**, with prior findings cross-referenced, no new envelope-specific defect found.

- **Necessity of storing the full replay-derived snapshot:** justified — it exists purely as a CAS token and fast-path cache (never authoritative), consistent with the existing, unchanged V68 `serialized_engine_state` CAS mechanism this design deliberately reuses rather than replaces.
- **Envelope size:** for the one fixture scenario examined in the parent materials (`tests/fixtures/scenario_engine_v2_vslice_1_1_0.json`-scale content), the documented field set (a handful of state/counter/flag entries, an `optionDisplayOrderByScene` map bounded by the scene count, and a `decisionHistory` array bounded by attempt length) is small and bounded by content size, not unbounded — no red flag found. This review did not measure actual byte sizes (no DB, no execution), so this is a structural, not empirical, assessment.
- **Option order per scene preserved/reproducible:** confirmed by design — `optionDisplayOrderByScene` is explicitly listed as replay-derived cached state (§9 group B), always re-verifiable by replay per the frozen §17 deterministic-ordering algorithm; not a new risk.
- **Mapping/list ordering determinism for CAS equality:** this is a **genuine, unresolved-in-the-contract subtlety**, but not to a degree that changes the overall assessment: **SA-17-1 (MEDIUM)** — the contract does not explicitly state whether `json.dumps` key ordering is significant for the whole-object CAS comparison the RPCs already perform today (V68's existing mechanism, unchanged by Slice A, compares `serialized_engine_state` as JSONB, and PostgreSQL's `jsonb` type — unlike `json` — already normalizes key order and whitespace on storage, making key-insertion-order differences a non-issue for `jsonb`-vs-`jsonb` equality *at the database layer*). The residual risk is entirely at the **Python** layer, if any code path ever compares two Python-side serialized dicts for equality *before* they reach `jsonb` storage (e.g., an idempotency check computed client-side) — the contract should explicitly state that Python-side dict key ordering must never be relied upon for equality; only `jsonb`-normalized database-side comparison should ever be trusted for CAS. Required correction: add one explicit sentence to contract §9 stating this. Blocks migration/validation/deployment: No/No/No.
- **Floating-point serialization / replay mismatch:** the contract's strict-finite-number rule (no `NaN`/`Infinity`) is necessary but not, by itself, sufficient to guarantee exact replay equality for `float` values that are *finite* but subject to serialization round-trip imprecision (e.g., a value that, after `json.dumps`/`json.loads`, differs in its last bit from the in-memory `float`). This is a pre-existing characteristic of any JSON-based float persistence, not introduced by Slice A, and is already implicitly bounded by Engine V2's own scoring/classification logic operating on bounded-precision, human-meaningful scores (not requiring bit-exact float reproduction) — judged **LOW**, not blocking, but worth one explicit contract sentence acknowledging the bound. **SA-17-2 (LOW).**
- **No hidden learner-sensitive content returned to clients:** confirmed by design separation (§9 group C never returns the envelope itself; only `LearnerSceneView`/`LearnerTerminalView` outputs are ever client-facing) — reinforced, not contradicted, by SA-16-1's finding (which is about what's persisted, not what's returned to a client; SA-16-1's leaked fields would still never reach a learner directly, since the learner-safe serializers are separate functions — but persisting them at all is still the documented concern).
- **Envelope versioning supports future additive changes:** confirmed — `envelopeVersion` (contract §9) is a sound, minimal, fail-closed-on-unknown-value mechanism.
- **Redundant or dangerous fields:** none identified as outright redundant; `terminalResult`'s minimal-summary shape is appropriately minimal, not dangerous, contingent on SA-16-1's correction being applied consistently to `decisionHistory` as well.

---

## Area 18 — Decision serialization

**Result: CONFIRMED**, contract §10 already specifies exactly the required five-field shape (`attemptId, expectedSequenceNumber, expectedSceneId, selectedOptionId, idempotencyKey`) and explicitly rejects extra/derived keys (§8.4). Cross-checked against the existing, unchanged `submit_scenario_decision_v1` contract (V68) for the idempotency-key/UUIDv4 convention — consistent. Sequence starting at 1: consistent with `scenario_attempts.next_sequence_number NOT NULL DEFAULT 1` and its `CHECK (next_sequence_number >= 1)` constraint (line 465-466, independently re-read). `bool`-as-`int` rejection and nonempty-ID rules: consistent with the existing, independently-re-confirmed `_require_strict_int`/`_require_nonempty_str` helper semantics. No new finding beyond SA-16-1 (which concerns the *run snapshot's* `decisionHistory`, not the decision*-submission* request contract in this area, which is already minimal and correct as specified).

---

## Area 19 — Replay and identity verification

**Result: CONFIRMED**, the proposed seven-step sequence is sound and matches the contract's §8.7/§8.8 exactly: load content identity from trusted DB columns (never the envelope) → load canonical decisions from trusted rows → verify identity (delegating to the existing, unmodified `verify_replay_identity_v2`, sourced from DB columns per §8.8 point 1) → replay from `content`'s own initial state (§8.7, never a cached shortcut) → the contract does not (yet) explicitly specify step 5 ("compare replay result with cached envelope") as its own named function — this is implicitly the caller's responsibility (compare `replay_serialized_run_v2`'s output against `deserialize_run_snapshot_v2`'s cached fields) rather than a single contract function. **SA-19-1 (LOW):** recommend the contract name this comparison step explicitly (even if it remains "caller's responsibility, using the two existing outputs" rather than a ninth new function) so a future implementer does not have to infer it. Blocks migration/validation/deployment: No/No/No. No value from the cached envelope is used as an input to replay itself: confirmed by design (§8.7 signature takes only `content`, `attempt_id`, and the raw `decision_history_payload` — never the envelope's cached `state`/`counters`/`flags`/`currentSceneId`).

---

## Area 20 — Failure behavior

**Result: CONFIRMED for the cases already covered by Areas 6/10; no new failure mode identified beyond what those areas already classify as safely-retryable-or-fail-closed.** Every case listed in this area (response timeout after/before commit, retry with matching/conflicting UUID, function unavailable during deployment, stale PostgREST cache, content hash mismatch, rollback after partial execution) maps directly onto a case already analyzed in Areas 6, 8, 10, 13, or 14 above, and in every case this review's independent analysis reaches the same conclusion the contract already claims: **safely retryable, or fails closed, never silently corrupting.** The two exceptions requiring correction before full confidence are exactly SA-08-1 (unique-violation classification) and SA-16-1 (serialization leak risk) — both already covered above, not new failure modes but refinements of already-identified ones. "Replay mismatch" / "cache mismatch" fail closed by design (`verify_replay_identity_v2`/`ScenarioReplayV2Error`, unmodified, reused) — confirmed consistent, no gap found beyond SA-19-1's minor documentation note.

---

## Area 21 — Test plan quality

**Result: the existing 12+12 test plan (contract §13/§14) is a solid baseline; this review identifies concrete gaps, several already flagged above and consolidated here for the required correction sequence.**

**Missing SQL test cases (add to §13):**
1. **"Same UUID, different scenario version, same owner"** as its own explicit case, distinct from "different owner" (SA-06-1).
2. **Partial-unique-index conflict classification, asserted explicitly:** a test that forces the `idx_scenario_attempts_one_in_progress` branch specifically (not the PK branch) and asserts, via `RAISE NOTICE`/an output column, that `v_constraint_name` was actually resolved to that exact literal string — not merely that the overall outcome "looked right" (SA-08-1's required empirical verification, made concrete as a test).
3. **Function-owner preservation:** capture `pg_proc.proowner`/`pg_get_userbyid(proowner)` before the migration and assert equality after (SA-11-1/SA-12-1).
4. **Rollback validation as a full round-trip test** (already present as test 11, but should be extended to also re-verify Engine V1 behavior end-to-end post-rollback, not merely signature presence).
5. **Existing six-argument SQL call succeeding after migration** (already present as test 1 — confirmed adequate).

**Missing/operational, not automatable as a pure SQL test:**
6. **Stale schema-cache behavior:** cannot be exercised inside a single-session `BEGIN...ROLLBACK` SQL test (it requires an actual PostgREST process and its cache-reload timing) — recommend this be a documented, manual deployment-runbook verification step instead (attempt one RPC call immediately after migration, before any explicit `NOTIFY`/reload, and confirm the expected `PGRST202`-class error, then confirm success after reload) rather than an automated test-suite entry.

**Missing Python test cases (add to §14):**
7. **JSONB cache equality:** an explicit test that two independently-serialized-then-`json.dumps`'d envelopes for identical logical state compare equal once round-tripped through a `jsonb`-equivalent normalization (e.g., `json.loads` both and compare, not raw string compare) — guards against SA-17-1's key-ordering subtlety.
8. **Corrupted envelope ignored for replay but reported:** a test that `deserialize_run_snapshot_v2` on a syntactically-valid-but-semantically-corrupted envelope (e.g., `decisionHistory` present but `currentSceneId` inconsistent with it) either raises during deserialization/verification or is *never silently trusted* by `replay_serialized_run_v2` (which must always replay from `content`+`decisionHistory` regardless of the envelope's other cached fields) — i.e., explicitly prove the "cache is never authoritative" principle with an adversarial, not just a well-formed, input.
9. **Completed-outcome mismatch (negative case):** the existing test 11 ("terminal outcome agreement") only proves agreement on a well-formed fixture; add its negative counterpart — a persisted envelope whose minimal `terminalResult` summary disagrees with what a fresh replay actually produces — and assert this is detected and rejected (not silently trusted), extending `verify_persisted_attempt_identity_v2`'s scope or documenting which function is responsible for this specific check.
10. **`decisionHistory` never contains hidden `DebriefTraceEntry` fields** (SA-16-1's required correction, made concrete as a test): recursive key-walk of `serialize_run_snapshot_v2`'s output asserting absence of `evaluationTier`, `debriefSeed`, `stateDelta`, `stateAfter`, `flagsCleared`, `flagsSet`, `nextSceneId`, `presentedDialogueVariantId`, `nextDialogueVariantId` anywhere in the `decisionHistory` array specifically (distinct from the existing test 12, which covers the learner-safe *views*, not the persisted *envelope*).
11. **Current Python named-argument call after migration:** none of the existing Python unit tests (`tests/test_scenario_persistence.py`) exercise a real database — recommend one **integration-level** test (gated, not part of the default unit suite, mirroring `tests/integration/ba201_supabase_lab.py`'s existing pattern) that calls the actual migrated RPC via `utils/scenario_persistence.py::start_or_resume_attempt(...)` unmodified, against a disposable database, and confirms success — this is the only way to close the gap between "the Python adapter sends the right dict" (already proven by unit tests) and "the live, migrated RPC actually accepts it" (not proven by any existing automated test).

**SA-21-1 (MEDIUM):** consolidating the above, the test plan is incomplete in exactly the ways this review's own mandate anticipated (items 1-3 and 7-10 above were explicitly hinted at in the task prompt). None of these gaps invalidate the SQL/Python design itself; they are additions required before the test plan can be considered complete enough to sign off on production deployment. Blocks executable migration: No. Blocks disposable DB validation: No. Blocks production deployment: Yes, until items 1-3 and 7-10 are added.

---

## Area 22 — Migration scope

**Result: CONFIRMED — no hidden requirement found.** The actual safe implementation requires exactly: one function signature/body replacement (this draft), `COMMENT`/grant restoration (present), PostgREST schema reload (present), and verification-test updates (Area 4/21, correctly scoped to Slice B, not this draft). Independently confirmed **no** hidden requirement for: table changes (none — `scenario_attempts`/`scenario_decisions` DDL is untouched), columns (none), indexes (none — `idx_scenario_attempts_one_in_progress` is read, never altered), triggers (none — `trg_guard_scenario_attempt_mutation`/`trg_guard_scenario_decision_immutability` are read for compatibility, never altered), RLS (none), policies (none — confirmed zero exist today and none are proposed), new RPCs (none — `get_scenario_attempt_v1`/`submit_scenario_decision_v1`/`abandon_scenario_attempt_v1` are untouched and unaffected), or application-caller changes (Area 4/15 — confirmed zero Python files require modification).

---

## Area 23 — Scope and safety of this review itself

- No SQL was applied: confirmed — no database-executing command was run at any point in this review.
- No database connection occurred: confirmed.
- No executable migration was created: confirmed — only the review document listed below was created; the two `.sql` drafts already existed from the prior task and were read, not modified (independently re-read, not re-written, during this review).
- No source or test file changed: confirmed — this review only used read-only tools (`Read`, `Grep`, `Glob`) against `utils/`, `pages/`, `tests/`, and `supabase/` files, and created exactly one new file.
- Protected paths remained untouched: confirmed — `.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`, `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`, `workers/combined_policy_evaluator.py`, `v68_corrected_review_bundle/`, `v68_final_review_bundle/`, `v68_review_bundle/` were never inspected, opened, searched, executed, modified, staged, or referenced at any point.
- Nothing staged, committed, pushed, or deployed: confirmed by the ending `git status` (see completion report below).

---

## Findings summary

| ID | Severity | Area | Blocks migration | Blocks disposable DB validation | Blocks production |
|---|---|---|---|---|---|
| SA-01-1 | NOTE | 1 | No | No | No |
| SA-01-2 | NOTE | 1 | No | No | No |
| SA-02-1 | NOTE | 2 | No | No | No |
| SA-03-1 | LOW | 3 | No | No | No |
| SA-03-2 | LOW | 3 | No | No | No |
| SA-04-1 | LOW | 4 | No | No | No |
| SA-05-1 | NOTE | 5 | No | No | No |
| SA-05-2 | LOW | 5 | No | No | No |
| SA-06-1 | MEDIUM | 6 | No | No | Recommended |
| **SA-08-1** | **HIGH** | 8 | No | No | **Yes** |
| SA-11-1 | MEDIUM | 11/13 | No | No | Yes |
| SA-12-1 | MEDIUM | 12/13 | No | No | Recommended |
| SA-17-1 | MEDIUM | 17 | No | No | No |
| SA-17-2 | LOW | 17 | No | No | No |
| SA-19-1 | LOW | 19 | No | No | No |
| **SA-16-1** | **HIGH** | 16 | No | No | **Yes (Python track)** |
| SA-21-1 | MEDIUM | 21 | No | No | Yes |

**Totals: 17 findings — 0 BLOCKER, 2 HIGH, 5 MEDIUM, 6 LOW, 4 NOTE.**

---

## Readiness decision

**CORRECTIONS_REQUIRED**

Rationale: blocker count is 0, and the core architecture (DROP-then-CREATE, grant/comment restoration, PostgREST-ambiguity avoidance, ownership-safe collision handling, unchanged Engine V1 return/error contract) is independently confirmed sound. However, the readiness standard explicitly requires **zero unresolved HIGH findings**, and this review identified two: **SA-08-1** (unverified `CONSTRAINT_NAME` assumption for the partial unique index — directly affects "concurrent starts remain safe" and "unique-violation classification safe") and **SA-16-1** (a real type mismatch that could leak internal debrief/evaluation fields into the persisted envelope if the Python contract is implemented literally — affects the "authoritative vs. cached vs. learner-safe" data-minimization guarantee at the heart of the whole design). Both are narrow, concretely correctable, and do not require redesigning the migration or the persistence architecture — hence **CORRECTIONS_REQUIRED**, not **REDESIGN_REQUIRED**.

---

## Required correction sequence

1. **SA-08-1:** either empirically verify the exact `CONSTRAINT_NAME` strings on a disposable database matching the target PostgreSQL version, or adopt the safer re-query-based classification design (recommended) — before finalizing the executable migration.
2. **SA-16-1:** amend the Python serialization contract (§8.1/§9) to explicitly require projecting `DebriefTraceEntry` down to the three-field minimal shape for `decisionHistory`, and add the corresponding negative test — before Slice D (Python adapter) implementation begins.
3. **SA-11-1 / SA-12-1:** add owner-preservation and body-fingerprint precondition/postcondition checks to both the migration and rollback drafts.
4. **SA-06-1 / SA-21-1:** extend the SQL and Python test plans with the specific missing cases enumerated in Area 21.
5. Re-review (or a lighter-weight confirmation pass) after corrections 1-2 are applied, before proceeding to draft an executable migration file.

---

## Required completion report

1. **Task status:** Complete.
2. **Review file created:** `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_SECURITY_REVIEW.md`.
3. **Repository branch:** `main`.
4. **Starting git status:** clean tracked tree; untracked-only entries identical to the set present at the start of the prior (SIM-PERSIST-V2-02A) task, plus that task's three Slice A output files.
5. **Ending git status:** identical to starting, plus this one new review file. (The Shell tool became unresponsive partway through this review — see "Errors encountered" — so the ending status could not be re-confirmed via a live command in this final pass; it is reported based on the last successful check at the start of this task plus the single `Write` this task performed, and should be spot-checked by the user/orchestrator if in doubt.)
6. **Readiness decision:** `CORRECTIONS_REQUIRED`.
7. **Total findings:** 17.
8. **Blocker count:** 0.
9. **High count:** 2 (SA-08-1, SA-16-1).
10. **Medium count:** 5 (SA-06-1, SA-11-1, SA-12-1, SA-17-1, SA-21-1).
11. **Low count:** 6 (SA-03-1, SA-03-2, SA-04-1, SA-05-2, SA-17-2, SA-19-1).
12. **Note count:** 4 (SA-01-1, SA-01-2, SA-02-1, SA-05-1).
13. **Current-function baseline result:** Confirmed exact match, zero unintentional deviations (Area 1).
14. **Replacement-strategy result:** Confirmed correct (Area 2).
15. **PostgREST-resolution result:** Confirmed, grounded in actual call style, `PGRST203` risk correctly eliminated by design (Area 3).
16. **Positional-caller result:** Confirmed compatible (Area 4).
17. **Named-caller result:** Confirmed compatible (Area 4).
18. **Attempt-ID security result:** Confirmed safe, ownership boundary independently verified (Area 5).
19. **Start-idempotency result:** Confirmed accurate for the primary claim; one test-plan gap (SA-06-1) (Area 6).
20. **Active-attempt reuse result:** Confirmed unchanged and correctly extended (Area 7).
21. **Unique-violation result:** Partially confirmed — one HIGH finding, SA-08-1 (Area 8).
22. **Ownership/isolation result:** Confirmed no disclosure (Area 9).
23. **Concurrency result:** All 8 races (A-H) analyzed and confirmed with expected outcomes (Area 10).
24. **Grant/security result:** Confirmed (Area 11).
25. **Function-owner result:** Not verifiable without a database connection; gap identified, SA-11-1 (Area 11).
26. **Precondition-check result:** Sufficient for signature identity; gap identified for body-fingerprint safety, SA-12-1 (Area 12).
27. **Postcondition-check result:** Confirmed sufficient (Area 12).
28. **Rollback result:** Confirmed byte-for-byte exact, same owner caveat as SA-11-1 applies (Area 13).
29. **Deployment-window result:** Confirmed accurate, non-overstated; low-traffic window prudent not necessary (Area 14).
30. **Engine V1 compatibility result:** Confirmed, including verbatim error-message preservation (Area 15).
31. **Serialization-contract result:** Partially confirmed — one HIGH finding, SA-16-1 (Area 16).
32. **Snapshot-envelope result:** Confirmed reasonable, two MEDIUM/LOW refinements (SA-17-1, SA-17-2) (Area 17).
33. **Decision-serialization result:** Confirmed (Area 18).
34. **Replay-verification result:** Confirmed, one documentation note (SA-19-1) (Area 19).
35. **Failure-recovery result:** Confirmed, no new failure mode beyond SA-08-1/SA-16-1 (Area 20).
36. **SQL-test-plan result:** Solid baseline, concrete gaps identified (Area 21, items 1-6).
37. **Python-test-plan result:** Solid baseline, concrete gaps identified (Area 21, items 7-11).
38. **Migration-scope result:** Confirmed no hidden table/column/index/trigger/RLS/policy/RPC/caller requirement (Area 22).
39. **Existing tests executed:** None — the Shell tool became unresponsive when attempting `python -m pytest tests/test_scenario_engine_v2.py tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py -q` (see "Errors encountered"). Field-name/signature assumptions were instead validated by direct, independent source inspection of `utils/scenario_engine_v2.py` (dataclass field lists for `ScenarioDecisionInputV2`, `ScenarioRunV2Snapshot`, `DebriefTraceEntry`, `LearnerSceneView`/`LearnerOptionView`/`LearnerTerminalView`, `ScenarioContentV2`, and function signatures for `start_scenario_run_v2`, `apply_decision_v2`, `replay_scenario_run_v2`, `verify_replay_identity_v2`, `_validate_decision_sequence_v2`) and `utils/scenario_persistence.py` (helper function names/signatures) — this is what surfaced SA-16-1.
40. **Test results:** Not executed (see item 39); no test failures to report since no test was run.
41. **Files modified:** None. Files created: exactly one (`SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_SECURITY_REVIEW.md`).
42. **Confirmation source/test/draft files untouched:** Confirmed — all reviewed files (`utils/scenario_persistence.py`, `utils/scenario_engine_v2.py`, `supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql`, both V68 test files, both Slice A `.sql` drafts, the Slice A contract `.md`, `tests/test_scenario_persistence.py`, `tests/integration/ba201_supabase_lab.py`, `pages/Scenario_Simulator.py`, `utils/scenario_learner_controller.py`) were opened only via read-only tools; none were edited.
43. **Confirmation protected paths untouched:** Confirmed — none of `.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`, `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`, `workers/combined_policy_evaluator.py`, `v68_corrected_review_bundle/`, `v68_final_review_bundle/`, `v68_review_bundle/` were inspected, opened, searched, executed, modified, staged, or referenced.
44. **Confirmation no database connection or SQL execution:** Confirmed — every tool call in this task was `Read`, `Grep`, `Glob`, `Write` (once, for this review file), or `Shell` (which became unresponsive and returned no output for every attempted command, including the requested pytest run and routine `git status` checks — no command it may or may not have silently executed touched a database, since none of the attempted commands were database-related in nature — they were `git status`, `echo`, `Get-ChildItem`, and the specified `pytest` invocation, none of which connect to Supabase or any database).
45. **Confirmation nothing staged, committed, pushed, or deployed:** No `git add`/`commit`/`push`/deploy command was ever issued in this task.
46. **Errors encountered:** The `Shell` tool became unresponsive partway through this task (returned "no exit status" for every subsequent command, including simple `echo`/`git status` sanity checks, across roughly six retries spaced with waits). This did not block completion of the review's substantive content — every finding in this document is grounded in `Read`/`Grep`/`Glob` evidence, which remained fully functional throughout — but it did prevent (a) running the requested read-only pytest command, and (b) re-confirming the ending `git status` with a live command in this task's final pass (item 5 above explains the fallback basis for that report line).
47. **Stop conditions encountered:** None of the task's enumerated stop conditions apply — no BLOCKER-severity finding was identified, no redesign is required, and the current RPC/contract/drafts were all locatable and reviewable entirely from unprotected files.
48. **Remaining risks:** (1) SA-08-1 and SA-16-1 remain open until their required corrections are applied — see "Required correction sequence." (2) The ending `git status` in this report is not independently re-confirmed by a live command due to the Shell outage (item 46); a follow-up `git status --short --branch` is recommended before proceeding to any next task, purely as a sanity check, not because any action in this task plausibly changed it (only one `Write` call was made, creating the one new review file).
49. **Required correction sequence:** See dedicated section above (5 steps).
50. **Recommended next task:** A short, targeted corrections pass (not a full re-review) that (a) resolves SA-08-1 by adopting the re-query-based unique-violation classification design in the migration draft, (b) amends the Slice A contract's §8.1/§9 per SA-16-1's exact wording, (c) adds the SA-11-1/SA-12-1 owner-preservation and body-fingerprint checks, and (d) extends the SQL/Python test plans per Area 21's itemized gaps — after which a brief confirmation pass (not a full independent re-review) should suffice before drafting the first executable migration file.
