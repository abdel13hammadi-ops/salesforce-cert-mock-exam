# SCENARIO_ENGINE_V2 Persistence — Slice A Implementation Contract

**Task ID:** SIM-PERSIST-V2-02A
**Model:** Sonnet High
**Baseline:** `6136673` — Complete Scenario Engine V2 vertical slice
**Scope:** Draft and contract work only. No migration applied, no database touched, no runtime/test file modified.
**Authoritative parents:** `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md` (§2, §7, §16), `docs/scenario_simulator/SCENARIO_SCHEMA_1_1_0_SPEC.md` §17/§19.

---

## 1. Current RPC — exact, verified signature

Read directly from `supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql`, lines 867–1138 (body) and 1140–1167 (comment/grants):

```sql
CREATE OR REPLACE FUNCTION public.start_or_resume_scenario_attempt_v1(
    p_user_email                text,
    p_scenario_version_id       uuid,
    p_initial_current_scene_id  text,
    p_initial_serialized_state  jsonb,
    p_engine_version            text,
    p_scenario_content_sha256   text
)
RETURNS TABLE (
    attempt_id                uuid,
    created                   boolean,
    scenario_id                uuid,
    scenario_version_id        uuid,
    status                     text,
    current_scene_id           text,
    next_sequence_number       integer,
    serialized_engine_state    jsonb,
    engine_version              text,
    scenario_content_sha256    text,
    started_at                  timestamptz,
    completed_at                timestamptz,
    abandoned_at                timestamptz,
    terminal_ending_id          text,
    terminal_result_snapshot    jsonb
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
```

**Identity signature** (the tuple PostgreSQL actually uses to distinguish/overload functions — parameter names and defaults are *not* part of it): `public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text)` — exactly six input types, no defaults today.

**Grants** (lines 1164–1167): `REVOKE ALL ... FROM PUBLIC`; `REVOKE EXECUTE ... FROM anon`; `REVOKE EXECUTE ... FROM authenticated`; `GRANT EXECUTE ... TO service_role`.

**Confirmed callers of this exact signature in this repository** (all named-parameter, all six required arguments, none omitted):

- `utils/scenario_persistence.py::start_or_resume_attempt(...)` → `_call_rpc(client, "start_or_resume_scenario_attempt_v1", {"p_user_email": ..., "p_scenario_version_id": ..., "p_initial_current_scene_id": ..., "p_initial_serialized_state": ..., "p_engine_version": ..., "p_scenario_content_sha256": ...})` — a Python `dict` sent as the PostgREST JSON body, i.e. a **named-argument** call over HTTP.
- `supabase/tests/v68_scenario_attempt_persistence_verification.sql` — calls the function **positionally** in raw `plpgsql` (e.g. line 745: `PERFORM * FROM public.start_or_resume_scenario_attempt_v1(v_email, v_draft_a_id, 'scene-start', jsonb, v_engine_version, v_hash_a)`), always with exactly six arguments in declared order, at roughly a dozen call sites.
- `to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)')` and `has_function_privilege('...', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)', 'EXECUTE')` — the same verification script also hardcodes the exact six-type signature **string** at five locations (lines 125, 352, 353, 372, 390, 425) for exact-OID resolution and grant assertions.

No stop condition is triggered here: the current RPC definition was located entirely from unprotected files, and no caller uses an argument style incompatible with the proposed additive signature (§2).

---

## 2. Proposed RPC — exact new signature

```sql
CREATE FUNCTION public.start_or_resume_scenario_attempt_v1(
    p_user_email                text,
    p_scenario_version_id       uuid,
    p_initial_current_scene_id  text,
    p_initial_serialized_state  jsonb,
    p_engine_version            text,
    p_scenario_content_sha256   text,
    p_attempt_id                uuid DEFAULT NULL
)
RETURNS TABLE ( -- byte-for-byte identical to §1 -- unchanged
    attempt_id uuid, created boolean, scenario_id uuid, scenario_version_id uuid,
    status text, current_scene_id text, next_sequence_number integer,
    serialized_engine_state jsonb, engine_version text, scenario_content_sha256 text,
    started_at timestamptz, completed_at timestamptz, abandoned_at timestamptz,
    terminal_ending_id text, terminal_result_snapshot jsonb
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
```

New identity signature: `public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid)` — **seven** input types. The new parameter is appended **last**, so every existing positional call (always exactly six arguments, in order) remains a syntactically and semantically valid call against the seven-parameter function once it is the only function of that name in the catalog (§3–§5).

---

## 3. PostgreSQL function-signature safety investigation

This section answers every numbered sub-question the task poses, in order, each backed by the PostgreSQL manual and a verified real-world PostgREST failure report (see citation at the end of this section).

### 3.1 Can `CREATE OR REPLACE FUNCTION` change the argument list?

**No — not in the way needed here.** Per the PostgreSQL manual (`CREATE FUNCTION`, "Notes"): *"It is not possible to change the name or argument types of a function this way (if you tried, you would actually be creating a new, distinct function)."* PostgreSQL identifies a function for `CREATE OR REPLACE` purposes by `(schema, name, ordered input-argument-type list)` — **parameter names and `DEFAULT` clauses are not part of that identity**. `(text, uuid, text, jsonb, text, text)` and `(text, uuid, text, jsonb, text, text, uuid)` are two different type lists, full stop.

### 3.2 Does adding a defaulted parameter create a second overload?

**Yes.** Issuing `CREATE OR REPLACE FUNCTION start_or_resume_scenario_attempt_v1(..., p_attempt_id uuid DEFAULT NULL)` against a database that already has the six-argument function does **not** replace it — PostgreSQL creates a **new, second function** with the seven-type identity signature, leaving the original six-argument function fully intact, unreferenced, and still independently callable. The database ends up with two functions sharing one name.

### 3.3 Does PostgREST/Supabase RPC dispatch become ambiguous when both signatures exist?

**Yes, and this is a real, previously-observed production failure mode, not a theoretical one.** PostgREST resolves an RPC call by matching the request's JSON body keys (or query-string keys, for `GET`) against a function's parameter names, requiring every parameter *not* supplied to have a default. A call supplying exactly the original six `p_*` keys (e.g. any existing Engine V1 call, which never sends `p_attempt_id`) now matches **both** candidates: the six-argument function (exact match) **and** the seven-argument function (matches because its seventh parameter, `p_attempt_id`, has a default and can be legitimately omitted). PostgREST has no tiebreaker for this case and returns error code **`PGRST203`** ("Could not choose the best candidate function... Try renaming the parameters or the function itself in the database so function overloading can be resolved"), surfaced to the HTTP client as a `300`/`400`-class failure — i.e., **every existing Engine V1 call to this RPC would start failing** the moment both signatures coexist in the schema PostgREST has cached. This is documented behavior (PostgREST `errors.html`, code `PGRST203`) and matches an independently reported real incident (a team added one `DEFAULT NULL` parameter via `CREATE OR REPLACE FUNCTION`, and their production API immediately began returning HTTP 400 with a `PGRST203`-class ambiguity error for every previously-working call) — see the citation at the end of this section.

**Conclusion: `CREATE OR REPLACE FUNCTION` alone is unsafe for this change.** The task's caution not to assume otherwise is correct and is affirmed here with a concrete, cited failure mode, not merely a theoretical concern.

### 3.4 Must the migration `DROP` the prior signature, `CREATE` the new one, and restore grants?

**Yes, all three, in that order, in one transaction.** There must never be a moment where both the six-argument and seven-argument functions simultaneously exist in the catalog and are both visible to a live PostgREST schema cache. Concretely:

1. `DROP FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text);` — removes the **exact** six-argument function (and, as a side effect of `DROP FUNCTION`, its `COMMENT` and every `GRANT`/`REVOKE` attached to that specific function object — these are not preserved across the drop).
2. `CREATE FUNCTION public.start_or_resume_scenario_attempt_v1(..., p_attempt_id uuid DEFAULT NULL) ...` — creates the seven-argument function as a **brand-new catalog object** (a new OID). PostgreSQL's default behavior for a newly created function is to grant `EXECUTE` to `PUBLIC` automatically (unless `ALTER DEFAULT PRIVILEGES` has been configured otherwise for this schema/role, which this repository has not done) — so the new object starts in a **more permissive** state than the old one had, until grants are re-applied.
3. Re-issue the **exact same four grant statements** the original migration used, verbatim except for the added `uuid` in the signature: `REVOKE ALL ... FROM PUBLIC`; `REVOKE EXECUTE ... FROM anon`; `REVOKE EXECUTE ... FROM authenticated`; `GRANT EXECUTE ... TO service_role`. Re-issue the `COMMENT ON FUNCTION` too (comments are not preserved across `DROP`/`CREATE` either).
4. `NOTIFY pgrst, 'reload schema';` — Supabase/PostgREST caches the function-overload resolution table in memory; even after step 3 leaves exactly one, unambiguous, correctly-permissioned function in the catalog, a running PostgREST instance will keep serving requests against its **stale in-memory schema cache** (which may still "remember" both the dropped six-argument function and the freshly created seven-argument one, or simply not know about the new parameter at all) until it reloads. Supabase's managed migration pipeline reloads the schema cache automatically after a tracked migration is applied; explicitly issuing `NOTIFY pgrst, 'reload schema'` inside the migration itself removes any dependency on that automatic behavior and is cheap, idempotent, and harmless to include regardless.

### 3.5 Does dropping the old signature introduce a transactional deployment window?

**Not for other transactions, provided the whole sequence runs inside one `BEGIN ... COMMIT`.** PostgreSQL DDL is transactional: as long as `DROP FUNCTION` → `CREATE FUNCTION` → grants → comment all execute inside the same transaction as this migration's `BEGIN;`/`COMMIT;` wrapper (matching the existing V68 migration's own convention), no other, concurrently-running transaction can ever observe an intermediate state where the function does not exist at all — under PostgreSQL's MVCC snapshot isolation, a concurrent reader sees either the pre-migration catalog (old function only) or the post-migration catalog (new function only), atomically, at the instant this migration's transaction commits.

**One real, standard, low-risk caveat, stated honestly rather than glossed over:** `DROP FUNCTION` acquires an `ACCESS EXCLUSIVE` lock on the function's `pg_proc` row. Any other session attempting to **begin** a new call to this function (i.e., PostgREST planning/dispatching a fresh RPC request) needs at least an `ACCESS SHARE` lock on that same catalog row, and will therefore **block**, briefly, until this migration's transaction commits or rolls back. A call already past that initial resolution/planning step when the migration begins is not retroactively aborted. This is standard, expected behavior for replacing any actively used function-based API in PostgreSQL — not a defect specific to this design — and is the same class of brief, sub-second contention every prior migration touching an existing, live RPC in this repository already accepts implicitly. No table-level lock, no row-level lock on `scenario_attempts`/`scenario_decisions` data, and no in-flight `submit_scenario_decision_v1`/`get_scenario_attempt_v1`/`abandon_scenario_attempt_v1` call (different function objects, untouched by this migration) is affected at all.

### 3.6 Do named-argument callers remain compatible?

**Yes**, unconditionally, once exactly one signature exists post-migration (§3.4). `utils/scenario_persistence.py`'s existing `start_or_resume_attempt(...)` — and any future `utils/scenario_persistence_v2.py` equivalent — sends a JSON object keyed by parameter name. Since all six original parameter **names** are unchanged (only a seventh, new, defaulted name is appended) and there is only one function definition, PostgREST resolves the six-key call to the (now seven-parameter) function with `p_attempt_id` implicitly `NULL`, identical in every observable respect to today's behavior.

### 3.7 Do positional callers remain compatible?

**Yes**, for the same reason, for raw SQL positional calls (the only positional-calling convention found in this repository — see `supabase/tests/v68_scenario_attempt_persistence_verification.sql`). A six-argument positional call resolves unambiguously to the seven-parameter function with its trailing `p_attempt_id` defaulted to `NULL`, provided (again) that no six-argument overload remains in the catalog to compete for that resolution. PostgREST itself never exposes a positional calling convention over HTTP — every PostgREST/Supabase-JS `.rpc(...)` call is JSON-object-keyed — so positional compatibility matters only for direct SQL callers (tests, manual `psql`/SQL-editor use), not for any application code path.

### 3.8 Must rollback explicitly drop the new signature and recreate the original?

**Yes.** Because `CREATE OR REPLACE FUNCTION` cannot "downgrade" a seven-argument function back to six arguments any more than it can upgrade six to seven (§3.1), rollback must mirror the forward migration exactly: `DROP FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid);` then `CREATE FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) ...` with the **original, byte-for-byte body**, then the original grants/comment, then `NOTIFY pgrst, 'reload schema';`. See `SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_ROLLBACK_DRAFT.sql`.

**Citation for §3.2/§3.3:** PostgreSQL 18 documentation, `CREATE FUNCTION`, "Notes" section (`https://www.postgresql.org/docs/current/sql-createfunction.html`); PostgREST error reference, code `PGRST203` (`https://postgrest.org/en/stable/references/errors.html`); PostgREST functions API reference on overloading and schema-cache reloading (`https://docs.postgrest.org/en/stable/references/api/functions.html`, `https://docs.postgrest.org/en/latest/references/schema_cache.html`); and an independently reported real-world incident matching this exact scenario (adding one `DEFAULT`-valued parameter via `CREATE OR REPLACE FUNCTION` immediately produced HTTP 400 `PGRST203` ambiguity errors in production; resolved by explicit `DROP FUNCTION IF EXISTS ...` + `NOTIFY pgrst, 'reload schema'`) — Dexter Lung, *"CREATE OR REPLACE didn't replace: one optional parameter, and my API 400'd in production,"* DEV Community.

### 3.9 Function owner preservation (SA-11-1 correction, SIM-PERSIST-V2-02C)

`DROP FUNCTION` + `CREATE FUNCTION` assigns ownership of the new catalog object to whichever role executes the `CREATE` statement — this is standard PostgreSQL behavior, not a defect, but it means the new function's owner is only *guaranteed* to match the old function's owner if the migration happens to run through the exact same role/pathway the original V68 migration used. Independent security review (SA-11-1) correctly flagged that neither the original migration nor rollback draft verified or pinned this.

**Exact current owner — how it is determined, and why it is not guessed:** this task, like SIM-PERSIST-V2-02A/02B before it, has no live database connection and is expressly forbidden from guessing a role name. `supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql` (the same migration that creates the six-argument baseline function) documents, in its own header (lines 303-305, 830-831), that "*Table/role ownership (the object owner, typically `postgres`) is never touched by any REVOKE/GRANT in this migration*" — this is the strongest documentary evidence available without a live connection, and it is *consistent with, but weaker than proof of*, the actual live value. Rather than hardcode `'postgres'` (or any other name) into the migration/rollback SQL based on that documentation, **both drafts instead capture the owner dynamically, at migration-apply time, from the live `pg_proc.proowner` catalog column**, and restore that exact captured value onto the replacement function via an explicit `ALTER FUNCTION ... OWNER TO`:

- **Forward migration** (`SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_MIGRATION_DRAFT.sql`, section 1d): `SELECT pg_get_userbyid(p.proowner) INTO v_old_owner_name FROM pg_proc p WHERE p.oid = <six-arg OID>` — captured *before* the `DROP`, stashed in a transaction-local GUC (`slice_a.captured_owner`, mirroring this same function's own `certbound.scenario_attempt_insert_guard` convention), then applied via `ALTER FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid) OWNER TO <captured>` (section 3b) immediately after the `CREATE`, and independently re-verified equal in the postcondition block (section 6).
- **Rollback** (`SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_ROLLBACK_DRAFT.sql`, section 1b/3b/6): the identical pattern, symmetrically, capturing the seven-argument function's owner before its `DROP` and restoring it onto the recreated six-argument function.

This design is correct **regardless of which role actually owns the function in any given environment** — it never depends on the "typically `postgres`" documentation being accurate, and never requires this task to determine or assert a specific role name. **Because the function is `SECURITY INVOKER` (not `DEFINER`), owner drift has no bearing on the function's own runtime privilege behavior** (it never executes "as" its owner); the risk this closes is narrower — who may subsequently `ALTER`/`DROP`/re-`GRANT` this specific object without needing to be a superuser — but it is exactly the gap SA-11-1 identified, and it is now closed by construction rather than by assumption.

### 3.10 Baseline body/fingerprint precondition (SA-12-1 correction, SIM-PERSIST-V2-02C)

Independent security review (SA-12-1) correctly found that checking only the function's *input-type signature* (via `to_regprocedure`) is insufficient to prove the installed function's *body* has not silently diverged from the exact text this draft assumes it is replacing — a real, not hypothetical, risk for this exact function family, since the V68 migration's own header records that "*V66 and V67 were manually applied to production, not through a tracked migration runner*."

**Design chosen, and why an exact hash is not hardcoded:** the review's own suggested remedy — `md5(pg_get_functiondef(...))` compared against a known-good constant — cannot safely be implemented in a draft-only task with no live database connection: PostgreSQL's DDL deparser (`ruleutils.c`) reformats a function's *header* clauses (argument list spacing, `RETURNS TABLE` column layout, `SET search_path = ...` → `SET search_path TO ...` normalization) in ways this task cannot reproduce byte-for-byte by hand with confidence, and a wrong hardcoded hash would itself cause a **false-positive** abort against a genuinely correct baseline — a new, self-inflicted risk the task's own stop conditions warn against ("the existing function body differs materially from the assumed baseline" must be a *true* finding, not an artifact of an incorrectly-guessed hash). This task therefore adopts the third option the task instructions explicitly permit: **"verified migration-version dependency plus material definition checks."**

Both drafts (section 1e / 1c respectively) now compute `pg_get_functiondef()` for the function being replaced, normalize whitespace (`regexp_replace(..., '\s+', ' ', 'g')`), and assert the presence of an array of **material, verbatim fragments** of the expected baseline body — every distinguishing `RAISE EXCEPTION` message, `SECURITY INVOKER`, the `ON CONFLICT DO NOTHING` shape (forward migration) or the Slice-A-specific `attempt_id_collision`/`attempt_id_conflict` messages (rollback), and two distinguishing return-shape column names (`terminal_result_snapshot`, `serialized_engine_state`). This is reliable specifically because **plpgsql function bodies are stored and returned verbatim** by `pg_get_functiondef()` (`prosrc` is not reparsed/reformatted for the `AS $$ ... $$` interior — only the function's header is reformatted), so every RAISE-message fragment is an exact, dependable fingerprint once whitespace runs are normalized. A single missing fragment aborts the migration/rollback **before** the `DROP` ever executes.

At minimum, this precondition (combined with the unchanged signature-identity check, §3.4/§1b) verifies: signature (via `to_regprocedure`, unchanged), return shape (via the two column-name markers), `SECURITY INVOKER` (explicit marker), and body content (every `RAISE EXCEPTION` marker). It does **not** independently re-verify `search_path` or grants via the fingerprint markers specifically (those are already independently verified by the existing, unchanged postcondition block's `has_function_privilege` checks and this same section's re-application of `SET search_path = public, pg_catalog` on the `CREATE`) or owner (verified separately, §3.9) — this is intentional layering, not a gap: each property is verified by the mechanism best suited to it, rather than folding every property into one fragile combined check.

**Recommended follow-up, not required to unblock this task:** once a disposable database matching the target PostgreSQL major version is available (Slice B/C), capture the real `pg_get_functiondef()` output once, compute its exact hash, and add it as a *stricter, additional* precondition alongside — not instead of — the marker-based check above, mirroring SA-08-1's own "empirically verify, or adopt the safer design" resolution pattern.

---

## 4. Attempt-ID semantics — exact required behavior

| Case | Behavior | Where enforced |
|---|---|---|
| **New attempt, `p_attempt_id` supplied** | Used as the new row's `id`, exactly | New `INSERT` path (§5) |
| **New attempt, `p_attempt_id` NULL** | `gen_random_uuid()`, exactly as today — Engine V1 unaffected | New `INSERT` path (§5) |
| **Resume, `p_attempt_id` NULL** | Ignored; returns the existing row's own id, exactly as today | Unchanged resume branch |
| **Resume, `p_attempt_id` equals the existing row's id** | Idempotent — treated identically to `p_attempt_id` being omitted; returns the existing row | New, explicit equality check (§5) |
| **Resume, `p_attempt_id` supplied but differs from the existing row's id** | **Rejected**, fail closed: `attempt_id_conflict: supplied p_attempt_id does not match the caller's existing in_progress attempt` | New, explicit check, before returning anything (§5) |
| **New attempt, `p_attempt_id` collides with any existing row's `id` (any owner)** | **Rejected**, fail closed: `attempt_id_collision: the supplied p_attempt_id is already in use` — message reveals nothing about the colliding row's owner, scenario, or status | `PRIMARY KEY` constraint + explicit exception-handler branch (§5) |
| **Two concurrent new-attempt calls, same `p_attempt_id`, same owner** | Exactly one wins the `PRIMARY KEY` insert; the other's insert fails with `unique_violation` on `scenario_attempts_pkey`, mapped to `attempt_id_collision` — **not** silently treated as "someone else already started," because that would incorrectly hand back state associated with a different id than the one it explicitly asked for | Same exception-handler branch (§5) — this is a deliberate, cautious design choice: id equality is never assumed to imply request equality |
| **Two concurrent new-attempt calls, same owner+version, no `p_attempt_id` (Engine V1, unchanged)** | Existing partial-unique-index de-duplication (`idx_scenario_attempts_one_in_progress`) — one wins, the other transparently resumes the winner's row, `created=false` | Unchanged from today, still reachable via the same exception-handler branch (§5) |
| **UUID collision "across attempts" for a different owner** | Structurally impossible to succeed silently — a caller-supplied `p_attempt_id` that happens to already belong to a different owner's row still hits the global `PRIMARY KEY` uniqueness check first (ownership is never consulted before that check even runs), so it fails exactly like any other collision, never granting access to the other owner's row | `PRIMARY KEY` constraint (global, owner-agnostic) |

**Ownership boundary preserved:** at no point does a supplied `p_attempt_id` let a caller read, resume, or influence any row it does not already own — the resume-conflict check (row 5) only ever compares against a row already scoped by `WHERE user_email = v_user_email` (the caller's own normalized identity), and the collision check (row 6) never reveals anything about the other row beyond the fact that the id is taken.

---

## 5. Migration body — narrative description (see the `.sql` draft for the literal text)

Relative to the current body (§1), exactly four changes, all additive/narrowing, none removing an existing check:

1. **New parameter** `p_attempt_id uuid DEFAULT NULL` appended after `p_scenario_content_sha256`.
2. **New resume-branch check**, immediately inside the existing `IF FOUND THEN` block, before its `RETURN QUERY`: reject a non-matching supplied `p_attempt_id` (§4, rows 4–5).
3. **`v_new_id` assignment changed** from `v_new_id := gen_random_uuid();` to `v_new_id := COALESCE(p_attempt_id, gen_random_uuid());`, with a new, minimal defensive check that a supplied `p_attempt_id` is not the nil UUID (`00000000-0000-0000-0000-000000000000`) — `invalid_attempt_id: p_attempt_id must not be the nil UUID` — mirroring this codebase's existing strict-input-validation convention (e.g. `utils/scenario_persistence.py`'s `_require_uuid4_str`) rather than silently accepting a degenerate value no legitimate caller would ever supply.
4. **`INSERT` changed** from a bare `ON CONFLICT DO NOTHING` (which the original code's own comment explains was chosen only to avoid an unrelated PL/pgSQL output-variable-name ambiguity, and which — as an unintended side effect — would have silently swallowed a `PRIMARY KEY` collision too) to an explicit `BEGIN ... EXCEPTION WHEN unique_violation THEN ...` block. **SA-08-1 correction (SIM-PERSIST-V2-02C):** independent security review found the original draft's classification logic — a bare string match of `GET STACKED DIAGNOSTICS ... CONSTRAINT_NAME` against `'scenario_attempts_pkey'`/`'idx_scenario_attempts_one_in_progress'` — rested on an unverified assumption about how PostgreSQL populates that diagnostic for a *partial* unique index, which the review was explicitly barred from empirically confirming (no live database connection permitted for that task). The corrected classification instead re-derives the answer structurally, from data this function already fully controls, and uses `CONSTRAINT_NAME` only as a defense-in-depth secondary signal:
   - On `unique_violation`, first re-query `SELECT EXISTS (... WHERE user_email = v_user_email AND scenario_version_id = p_scenario_version_id AND status = 'in_progress')` — the exact scope the partial index enforces. PostgreSQL's own unique-index conflict-checking blocks a concurrent inserter until the *other* transaction's outcome is fully resolved before ever raising `unique_violation`, so this re-query is guaranteed to see a genuine race's winning row.
   - If that row **exists**: the partial-index race occurred (structurally the only possible explanation for an existing in-progress row at this exact key) — falls through exactly as `ON CONFLICT DO NOTHING` did before (§4, row 8).
   - If that row **does not exist**: the partial index is structurally ruled out, so the violation must be the `PRIMARY KEY` (the only other unique-enforcing object on this table) — re-raised as `attempt_id_collision` (§4, row 6) — **unless** `CONSTRAINT_NAME` names some third, currently-nonexistent unique-enforcing object, in which case the handler fails closed with a generic `start_or_resume_failed` error rather than ever mislabeling an unrecognized violation as `attempt_id_collision`.

   Same observable behavior for every Engine V1 call, which never supplies `p_attempt_id` and therefore can only ever hit the "row exists" branch (a `gen_random_uuid()` colliding with an existing primary key is a ~2⁻¹²² probability event that the old code would have silently mishandled anyway; the new code now handles it correctly instead of silently producing a misleading generic `start_or_resume_failed`, a strict improvement, not a regression, for Engine V1 too).
5. **New consistency fix, directly related to SA-08-1:** the exception-handler fallthrough path (point 4 above, "row exists" branch) now applies the **identical** `p_attempt_id`-vs-resolved-row equality check the resume branch (point 2 above) already applied, immediately before the final `RETURN QUERY`. Pre-correction, a caller that lost an active-attempt race while supplying a `p_attempt_id` different from the actual winner's id was silently handed the winner's id instead of its own — safe (never another owner's data) but inconsistent with the resume branch's own stricter behavior. The corrected function now uniformly rejects this case with `attempt_id_conflict` in both branches: id equality is never assumed to imply request equality anywhere in this function. A caller that omits `p_attempt_id` is completely unaffected.

Every other line of the function body — email normalization, scenario-version lookup and publication/engine-version/hash checks, the advisory lock, the resume `SELECT ... FOR UPDATE`, every `p_initial_serialized_state` IDENTITY/LIFECYCLE check, the final re-`SELECT ... FOR UPDATE` and `RETURN QUERY` — is **byte-for-byte unchanged**.

---

## 6. Engine V1 compatibility — proof

| Requirement | Proof |
|---|---|
| Omitted `p_attempt_id` preserves current behavior | `p_attempt_id DEFAULT NULL`; the new resume-branch check is a no-op when `p_attempt_id IS NULL` (the `IF` condition requires `p_attempt_id IS NOT NULL`); `v_new_id := COALESCE(NULL, gen_random_uuid())` = `gen_random_uuid()`, identical to today |
| Existing positional/named calls remain valid | §3.6/§3.7 — yes, unconditionally, once exactly one signature exists (guaranteed by DROP-then-CREATE, §3.4) |
| Return shape unchanged | `RETURNS TABLE (...)` block is copied verbatim, same 15 columns, same names, same order, same types |
| Existing Engine V1 persistence tests remain valid | `utils/scenario_persistence.py`'s Python-level tests (none of which reference `p_attempt_id`) are unaffected — no Python file is modified by this slice. **Caveat, stated honestly:** `supabase/tests/v68_scenario_attempt_persistence_verification.sql` hardcodes the **old six-type signature string** in five places (`to_regprocedure`/`has_function_privilege` calls) for exact-OID resolution; after this migration is applied to any database, those five calls will raise `undefined_function`/resolve to `NULL` rather than silently continuing to pass, because the old signature genuinely no longer exists. **This is not a "test still valid" case** — updating that script to reference the new seven-type signature (and adding the new `p_attempt_id` test cases, §9) is explicitly **in scope for Slice B** (independent migration/RPC review), not this Slice A contract-drafting task, which is expressly forbidden from modifying tests. This is called out here so Slice B does not discover it cold. |
| No Engine V1 Python file requires modification in this slice | Confirmed — `utils/scenario_persistence.py`, `utils/scenario_learner_controller.py`, `utils/scenario_engine.py`, and `pages/Scenario_Simulator.py` are untouched; none of them ever supplies a seventh RPC argument, so all continue to compile and run unchanged against the new signature |

---

## 7. Concurrency contract — interaction with the new parameter

| Existing mechanism | Interaction with `p_attempt_id` |
|---|---|
| `pg_advisory_xact_lock(hashtext(user_email || ':' || scenario_version_id))` | **Unaffected.** Still taken first, still keyed only on `(user_email, scenario_version_id)` — `p_attempt_id` plays no role in lock acquisition, so the existing "at most one winner per (learner, version) pair" guarantee is unchanged |
| `idx_scenario_attempts_one_in_progress` partial unique index | **Unaffected as a constraint.** Its role in the create-path exception handling (§5, point 4) is preserved exactly; a caller-supplied `p_attempt_id` does not change which row this index considers a conflict — it is still `(user_email, scenario_version_id) WHERE status='in_progress'` |
| `scenario_attempts_pkey` (`PRIMARY KEY (id)`) | **Newly load-bearing for this feature.** Previously only ever violated by an astronomically unlikely `gen_random_uuid()` collision (silently, incorrectly swallowed by the old bare `ON CONFLICT DO NOTHING`); now a realistic, correctly-handled path for a caller-supplied `p_attempt_id` reuse (§4, row 6) |
| `SELECT ... FOR UPDATE` (resume branch, and the re-`SELECT` after insert) | **Unaffected in mechanism**, but now also the point where the new resume-conflict check (§4, rows 4–5) runs, still under the same row lock, so a concurrent resume attempt with a conflicting `p_attempt_id` is serialized exactly like every other resume-branch check |
| Active-attempt reuse (an attempt already `in_progress` for this `(user_email, scenario_version_id)`) | **Unchanged** as the trigger for taking the resume branch at all; only *what happens once that branch is taken* gained the new conflict check |
| **Same UUID from two concurrent new-attempt requests, same owner** | **New case, fully specified (§4, row 7):** the `PRIMARY KEY` constraint (not the partial unique index) resolves the race — exactly one `INSERT` can ever succeed with that `id`; the loser receives `attempt_id_collision`, not a silent "resumed the winner's attempt," because the winner's row is not guaranteed to be `(user_email, scenario_version_id)`-equal to what the loser expected merely by virtue of sharing an id (in practice, for a legitimate client that mints its own UUIDv4 once per genuinely new attempt, this case should never occur outside of a client-side bug or a deliberate replay-with-same-id test) |
| **Different UUIDs from two concurrent new-attempt requests, same owner+version (SA-08-1 correction)** | The partial index resolves the race (same key, same scope the advisory lock already serializes in the ordinary case); the loser's own exception-handler fallthrough now applies the identical `p_attempt_id`-vs-winner-id equality check the resume branch already applied (§5, point 5) — the loser is rejected with `attempt_id_conflict`, **not** silently handed the winner's different id. **This corrects the original Slice A draft's §13 test-8 assumption** ("the loser resumes the winner's actual id, not its own supplied one") — that behavior is now superseded; see the corrected test 8 in §13. |

**This migration does not weaken any existing concurrency guarantee.** Every guarantee in `SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md` §6 (row lock ordering, CAS checks, atomic terminal transition) is untouched — `submit_scenario_decision_v1`, `get_scenario_attempt_v1`, and `abandon_scenario_attempt_v1` are not modified by this slice at all.

---

## 8. Python serialization / replay-adapter API contract (`utils/scenario_persistence_v2.py`, design only — not implemented in this slice)

General rules applying to every function below (per task "STRICT TYPES" section): sequence numbers are exact Python `int`, never `bool` (Python's `bool` is an `int` subclass — every function explicitly rejects it, mirroring `utils/scenario_persistence.py`'s existing `_require_strict_int`); attempt/idempotency identifiers are validated as real UUIDs (version-4 specifically for idempotency keys, mirroring `_require_uuid4_str`); every string identifier (`scene_id`, `option_id`, `simulation_id`, etc.) must be an actual `str`, non-empty after `.strip()`, and already trimmed (no silent normalization — mirroring `_require_nonempty_str`); every numeric value in `state`/`counters` must be finite (`math.isfinite`; `NaN`/`Infinity`/`-Infinity` rejected); no function accepts or returns `MappingProxyType`, `frozenset`, a `tuple` (outside of intermediate Python-side computation — every persisted/returned JSON value uses plain `list`/`dict`/`str`/`int`/`float`/`bool`/`None`), a dataclass instance, an `Enum` member, or an exception object anywhere inside a JSON-compatible return value.

### 8.1 `serialize_run_snapshot_v2(run: ScenarioRunV2Snapshot) -> Dict[str, Any]`

- **Input:** a `ScenarioRunV2Snapshot` (the existing, unmodified Engine V2 dataclass).
- **Output:** the exact envelope shape in §10, as a plain `dict` (JSON-round-trippable via `json.dumps(..., allow_nan=False)`).
- **Validation behavior:** none of the engine's own invariants are re-validated here (the engine already guarantees them) — this function's only job is type conversion (`frozenset[str]` → sorted `list[str]`; `Mapping[str, tuple[str, ...]]` → `dict[str, list[str]]`; `Mapping[str, float]`/`Mapping[str, int]` → plain `dict`). It **does** assert, defensively, that every numeric value it emits is finite (raises `ScenarioPersistenceV2SerializationError` — new, V2-adapter-local exception — if the engine ever produced a non-finite number, which should be structurally impossible given Engine V2's own `_require_finite_number` checks, but is asserted here as a fail-closed boundary rather than trusted silently).
- **SA-16-1 correction (SIM-PERSIST-V2-02C) — `decisionHistory` projection, MANDATORY, not optional:** `run.decisions` is typed `tuple[DebriefTraceEntry, ...]` (`utils/scenario_engine_v2.py` lines 1223-1254), **never** `tuple[ScenarioDecisionInputV2, ...]`. Each `DebriefTraceEntry` carries fifteen fields: `sequence_number, scene_id, option_id, evaluation_tier, debrief_seed, state_delta, state_after, flags_cleared, flags_set, next_scene_id, entered_corrective, skipped_corrective, presented_dialogue_variant_id, next_dialogue_variant_id, competency_tags`. `serialize_run_snapshot_v2` **MUST** project each element of `run.decisions` down to **exactly**:
  ```json
  {"sequenceNumber": entry.sequence_number, "sceneId": entry.scene_id, "optionId": entry.option_id}
  ```
  explicitly discarding every one of the remaining twelve fields — `evaluationTier`, `debriefSeed`, `stateDelta`, `stateAfter`, `flagsCleared`, `flagsSet`, `nextSceneId`, `enteredCorrective`, `skippedCorrective`, `presentedDialogueVariantId`, `nextDialogueVariantId`, `competencyTags` — **none of these may ever appear in the persisted envelope's `decisionHistory` array, in any form, under any key name.** This projection is not a generic "type conversion" (unlike the `frozenset`/`Mapping` thaws described above) — it is a **mandatory field-subset selection**, and an implementation that instead serializes each `DebriefTraceEntry`'s full `__dict__`/`dataclasses.asdict()` output is **non-compliant with this contract**, not merely suboptimal. See §14 test 13 (new) for the required negative test.
- **Domain exceptions:** `ScenarioPersistenceV2SerializationError` (defensive, should never fire in practice).
- **Content/hash identity checks:** none performed here — identity fields are copied verbatim from `run.content`/`run`, not re-verified (verification is `verify_persisted_attempt_identity_v2`'s job, §8.8).
- **Pure:** Yes — no I/O, no mutation of `run`.
- **Input mutation forbidden:** Yes, and structurally guaranteed — `run` is a frozen dataclass; every field read is immutable (`Mapping`/`frozenset`/`tuple`) or copied into a new plain container before being placed in the output `dict`.

### 8.2 `deserialize_run_snapshot_v2(payload: Mapping[str, Any]) -> PersistedRunEnvelopeV2`

- **Input:** an arbitrary `Mapping[str, Any]` — untrusted (a JSONB value read back from `scenario_attempts.serialized_engine_state` or `scenario_decisions.state_before`/`state_after`).
- **Output:** a **new, frozen dataclass** `PersistedRunEnvelopeV2` (not a reconstructed `ScenarioRunV2Snapshot` — see rationale below) exposing exactly the fields in §10, strictly typed and validated, e.g. `envelope_version: int`, `simulation_id: str`, `version: str`, `schema_version: str`, `canonical_content_sha256: str`, `engine_version: str`, `current_scene_id: str | None`, `is_complete: bool`, `decision_history: tuple[DecisionTripleV2, ...]`, `option_display_order_by_scene: Mapping[str, tuple[str, ...]]`, `terminal_result: TerminalSummaryV2 | None`.
- **Why not a `ScenarioRunV2Snapshot`:** that dataclass requires a `content: ScenarioContentV2` field the envelope never carries (content is loaded separately, from the pinned `scenario_version_id`, per the parent design's §3 source-of-truth principle) — `deserialize_run_snapshot_v2` therefore returns a **strictly-typed, validated, but non-authoritative** view of exactly what was persisted, never a "reconstructed run." Reconstructing an actual, authoritative `ScenarioRunV2Snapshot` is always `replay_serialized_run_v2`'s job (§8.7), never this function's.
- **Validation behavior:** structural + strict-type validation of every required key in §10 group A/B (missing key, wrong JSON type, non-finite number, non-UUID `attemptId` if present, wrong `engineVersion` literal, malformed `canonicalContentSha256` format) — **never** a semantic/content check (it never loads or compares against actual scenario content). **SA-16-1 correction:** each element of `decisionHistory` is validated via `deserialize_decision_input_v2` (§8.4) — exactly the three permitted keys (`sequenceNumber`, `sceneId`, `optionId`), nothing more — so a corrupted or maliciously-crafted envelope smuggling any excluded internal field (`evaluationTier`, `debriefSeed`, `stateDelta`, etc. — see §9's exact exclusion list) inside a `decisionHistory` element is rejected here, at the deserialization boundary, before it can ever reach `replay_serialized_run_v2` or any other downstream consumer.
- **Domain exceptions:** `ScenarioPersistenceV2ValidationError` (one exception class, per malformed field, mirroring `utils/scenario_persistence.py`'s `ScenarioPersistenceValidationError` convention exactly, including a `invalid_<field>:`-prefixed message).
- **Content/hash identity checks:** format-only (`canonicalContentSha256` matches `^[0-9a-f]{64}$`); cross-referencing against the actually-loaded content is `verify_persisted_attempt_identity_v2`'s job.
- **Pure:** Yes.
- **Input mutation forbidden:** Yes — `payload` is read-only; nothing is written back into it.

### 8.3 `serialize_decision_input_v2(decision: ScenarioDecisionInputV2) -> Dict[str, Any]`

- **Input:** the existing, unmodified `ScenarioDecisionInputV2(sequence_number: int, scene_id: str, option_id: str)`.
- **Output:** `{"sequenceNumber": int, "sceneId": str, "optionId": str}` — exactly three keys, matching §11.
- **Validation behavior:** re-validates strictly on the way out (never trusts that an in-memory dataclass instance was constructed correctly) — `sequence_number` must be a real `int` (not `bool`) `>= 1`; `scene_id`/`option_id` must be non-empty, already-trimmed `str`.
- **Domain exceptions:** `ScenarioPersistenceV2ValidationError`.
- **Pure:** Yes. **Input mutation forbidden:** Yes (frozen dataclass input).

### 8.4 `deserialize_decision_input_v2(payload: Mapping[str, Any]) -> ScenarioDecisionInputV2`

- **Input:** untrusted `Mapping[str, Any]` (a client request body, or one element of a persisted `decisionHistory` array).
- **Output:** `ScenarioDecisionInputV2`.
- **Validation behavior:** rejects any key beyond `sequenceNumber`/`sceneId`/`optionId` (extra keys, e.g. a client attempting to smuggle `evaluationTier`, are a hard, fail-closed rejection — never silently ignored) — enforces the "do not serialize/accept client-supplied derived values" requirement structurally, not just by omission. Strict types identical to §8.3.
- **Domain exceptions:** `ScenarioPersistenceV2ValidationError`, including a distinct `unexpected_field:` message for a forbidden extra key.
- **Pure:** Yes. **Input mutation forbidden:** Yes.

### 8.5 `serialize_learner_scene_view_v2(view: LearnerSceneView) -> Dict[str, Any]`

- **Input:** the existing, unmodified `LearnerSceneView` (already the learner-safe boundary — see `utils/scenario_engine_v2.py::build_learner_scene_view`).
- **Output:** a plain JSON `dict` mirroring every `LearnerSceneView` field 1:1 (`sceneId`, `title`, `setting`, `dialogueExchanges`, `charactersPresent`, `learnerPresent`, `decisionPrompt`, `options` — each a `{"id", "title", "text"}` object — `progressMetadata`, `accessibility`, `mobilePresentation`, `expectedSequenceNumber`, `isComplete`).
- **Validation behavior:** none beyond type conversion — `LearnerSceneView` is already the exclusion boundary; this function must **never** read any field from `run`/`scene`/`option` directly, only from the already-built `view`, so it is structurally impossible for it to leak a field `build_learner_scene_view` deliberately excluded.
- **Domain exceptions:** none expected (defensive `ScenarioPersistenceV2SerializationError` only, mirroring §8.1).
- **Pure:** Yes. **Input mutation forbidden:** Yes.

### 8.6 `serialize_terminal_view_v2(view: LearnerTerminalView) -> Dict[str, Any]`

- **Input:** `LearnerTerminalView(outcome_id, outcome_title, narrative, display_score)`.
- **Output:** `{"outcomeId": str, "outcomeTitle": str, "narrative": str, "displayScore": int}` — exactly four keys, no more (this is the strict upper bound the parent design's §11/§14 already committed to — no evaluation tier, no debrief seed, no classification trace).
- **Pure:** Yes. **Input mutation forbidden:** Yes.

### 8.7 `replay_serialized_run_v2(content: ScenarioContentV2, *, attempt_id: str, decision_history_payload: Sequence[Mapping[str, Any]]) -> ScenarioRunV2Snapshot`

- **Input:** already-loaded, already-validated `ScenarioContentV2`; the attempt's stable identity string; the **raw, untrusted** `decisionHistory` JSON array (from a persisted envelope or directly from ordered `scenario_decisions` rows).
- **Output:** a fully reconstructed, authoritative `ScenarioRunV2Snapshot` — the **only** function in this contract that produces one.
- **Validation behavior:** (1) `deserialize_decision_input_v2` each element strictly (§8.4); (2) delegate ordering/gap/duplicate validation and the actual replay entirely to the existing, unmodified `_validate_decision_sequence_v2` + `replay_scenario_run_v2` (never reimplemented here); (3) never accepts a pre-built `state`/`flags`/`counters` shortcut — always replays from `content`'s own initial state forward, decision by decision, regardless of what a persisted envelope's cached fields say.
- **Domain exceptions:** re-raises `ScenarioRunStateV2Error`/`ScenarioReplayV2Error` unchanged (does not wrap them in a new V2-adapter exception, so callers already handling Engine V2's own exception types need no new handling).
- **Content/hash identity checks:** none performed here — identity must already have been verified by `verify_persisted_attempt_identity_v2` (§8.8) **before** this function is ever called; this function trusts that `content` is already the correct, pinned content.
- **Pure:** Yes (no I/O; deterministic given identical inputs, per the already-frozen §17 contract).
- **Input mutation forbidden:** Yes.

### 8.8 `verify_persisted_attempt_identity_v2(content: ScenarioContentV2, *, attempt_row_id: str, attempt_row_engine_version: str, attempt_row_scenario_content_sha256: str, envelope: PersistedRunEnvelopeV2) -> None`

- **Input:** the freshly-loaded content; the three identity columns read directly from the `scenario_attempts` row (`id`, `engine_version`, `scenario_content_sha256` — **not** from inside the JSONB envelope, which is untrusted cache); the already-`deserialize_run_snapshot_v2`-parsed envelope.
- **Output:** `None` — raises on any mismatch, returns nothing on success (a pure assertion function, mirroring `verify_replay_identity_v2`'s own contract exactly).
- **Validation behavior:** (1) delegates the content-vs-pinned-metadata check entirely to the existing, unmodified `verify_replay_identity_v2(content, pinned_simulation_id=envelope.simulation_id, pinned_version=envelope.version, pinned_schema_version=envelope.schema_version, pinned_canonical_content_sha256=attempt_row_scenario_content_sha256, pinned_engine_version=attempt_row_engine_version)` — always sourcing the *hash* and *engine version* from the **database column**, never from the envelope's own copy of them, so a corrupted/stale envelope copy can never mask a real drift; (2) a **new** check, not present in the V1 adapter, that `envelope`'s own `canonicalContentSha256`/`engineVersion` fields (inside the JSONB) also agree with the two column values — a disagreement between the two independently-stored copies is itself evidence of corruption and is rejected, even before content is re-verified; (3) a **new** check that the attempt row's own `id` equals the attempt identity Engine V2 will replay with (`attempt_row_id`) — trivial by construction in this design (§2 of the parent design document), but asserted here defensively rather than assumed.
- **Domain exceptions:** re-raises `ScenarioReplayV2Error` (from the delegated call) unchanged; raises a new, focused `ScenarioPersistenceV2IdentityError` for the two new checks in point (2)/(3) above, each with a distinct, named mismatch field.
- **Pure:** Yes. **Input mutation forbidden:** Yes.

---

## 9. Snapshot envelope — exact contract

```json
{
  "envelopeVersion": 1,
  "simulationId": "cb-sc-001-onboarding-handoff-vslice",
  "version": "0.2.0-vslice",
  "schemaVersion": "1.1.0",
  "canonicalContentSha256": "…64 lowercase hex…",
  "engineVersion": "SCENARIO_ENGINE_V2",
  "currentSceneId": "SC001-C02",
  "expectedSequenceNumber": 2,
  "isComplete": false,
  "state": { "customerConfidence": 72.0 },
  "counters": { "correctiveScenesExperienced": 1 },
  "flags": ["flag-verbal-handoff-only"],
  "decisionHistory": [
    { "sequenceNumber": 1, "sceneId": "SC001-C01", "optionId": "opt-sc001-c01-a" }
  ],
  "routingResolutions": [
    { "sequenceNumber": 1, "nextSceneId": "SC001-C02", "enteredCorrective": false, "skippedCorrective": false }
  ],
  "optionDisplayOrderByScene": { "SC001-C02": ["opt-sc001-c02-b", "opt-sc001-c02-a"] },
  "selectedVariantIdByScene": { "SC001-C02": null },
  "terminalResult": null
}
```

Field-by-field grouping, exactly as requested:

**A. Authoritative persisted identity and decisions** (never overridden by a replay result — a disagreement here is a hard identity failure, not a state to recompute):
`simulationId`, `version`, `schemaVersion`, `canonicalContentSha256`, `engineVersion`, `decisionHistory` (the triples only — this is the one field inside the envelope that is genuinely "learner truth"; everything else in the envelope is derivable from it).

**SA-16-1 correction (SIM-PERSIST-V2-02C) — `decisionHistory` hidden-data restriction, normative:** every element of `decisionHistory` MUST be **exactly** `{"sequenceNumber": int, "sceneId": str, "optionId": str}` — three keys, no more. The following fields, all present on the engine's own internal `DebriefTraceEntry` per decision, **MUST NEVER** appear anywhere inside `decisionHistory`, under any key name, in any persisted envelope: `evaluationTier` (or `evaluation_tier`), `debriefSeed` (`debrief_seed`), `stateDelta`/`stateAfter` (`state_delta`/`state_after`), `flagsCleared`/`flagsSet` (`flags_cleared`/`flags_set`), `nextSceneId` (`next_scene_id`), `enteredCorrective`/`skippedCorrective` (`entered_corrective`/`skipped_corrective`), `presentedDialogueVariantId`/`nextDialogueVariantId`, `competencyTags` (`competency_tags`). The canonical `scenario_decisions` database rows (unchanged, unmodified by this slice) remain the sole authoritative source for any of this excluded information if it is ever needed — this envelope is a replay-verified cache, never a substitute for those rows, and MUST NOT be treated as authoritative for anything beyond the three permitted keys above. `deserialize_run_snapshot_v2` (§8.2) enforces this on the way **in** too: an untrusted payload containing any of the excluded keys inside a `decisionHistory` element is a hard, fail-closed validation error (`ScenarioPersistenceV2ValidationError`, `unexpected_field:`-prefixed, mirroring §8.4's existing extra-key rejection for individual decision submissions), never silently stripped or silently accepted.

**B. Replay-derived cached state** (always re-verified against a fresh `replay_serialized_run_v2` call before being trusted for anything beyond a fast, provisional render; never treated as a second source of truth; a mismatch is a fail-closed replay error, §9 of the parent design):
`currentSceneId`, `expectedSequenceNumber`, `isComplete`, `state`, `counters`, `flags`, `routingResolutions`, `optionDisplayOrderByScene`, `selectedVariantIdByScene`, `terminalResult` (minimal summary — see below).

**C. Learner-safe output** (never this envelope directly — always the separately-built `LearnerSceneView`/`LearnerTerminalView`, serialized via §8.5/§8.6; the envelope above is a server/database-only artifact and must never be returned verbatim to a client).

`envelopeVersion` (new relative to the parent design's §7 sketch, added per this task's explicit ask): an integer, starting at `1`, versioning the **shape of this JSON envelope itself** — independent of `schemaVersion` (scenario content) and `engineVersion` (the Python engine). A future change to which keys this envelope contains bumps `envelopeVersion`; `deserialize_run_snapshot_v2` rejects an unrecognized value with `ScenarioPersistenceV2ValidationError` (`unsupported_envelope_version:`) rather than guessing at a shape it was not written to parse — fail closed, matching this codebase's existing "unsupported policy identifiers fail closed" convention (§17 of the schema spec).

`terminalResult`, when non-null, is the same minimal summary the parent design's §7 already specifies: `{"endingId": str, "displayScore": int, "engineVersion": str, "canonicalContentSha256": str}` — never the full `ScenarioTerminalResultV2` (which embeds every `DebriefTraceEntry` and the internal `ClassificationTrace`, neither of which is ever persisted — §11 of the parent design, reaffirmed here).

**Cache-verification and CAS use:** the whole envelope (as a JSONB value) continues to serve as the existing `state_before`/`serialized_engine_state` whole-object-equality CAS token (`SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md` §6) — no change to how the RPCs use it, only to what it contains.

---

## 10. Decision serialization — exact contract

Persisted decision request/response fields (matching §11 of the parent design, restated exactly for this slice):

```json
{
  "attemptId": "5e6a9e2e-....-....-....-............",
  "expectedSequenceNumber": 2,
  "expectedSceneId": "SC001-C02",
  "selectedOptionId": "opt-sc001-c02-b",
  "idempotencyKey": "b7f6c1d4-....-4...-....-............"
}
```

Exactly these five fields — **never** `state`, `flags`, `evaluationTier`, `resultingSceneId`, `isTerminal`, or any other server-derived value on the way **in** from a client (§8.4 enforces this by rejecting any unexpected key, not merely by omission from a type hint). The **persisted** `scenario_decisions` row additionally carries the server-computed `state_before`/`state_after` envelopes (§9) and `resulting_scene_id`/`is_terminal`/`terminal_ending_id` — all computed by the service, never accepted from the client, exactly mirroring the already-implemented, unmodified V68 decision-submission RPC contract.

---

## 11. Idempotency contract

### 11.1 Start-attempt idempotency (new, via `p_attempt_id`, §4)

**SA-06-1 correction (SIM-PERSIST-V2-02C) — exact idempotency scope, corrected claim:** the original Slice A draft's framing risked being read as "supplying the same `p_attempt_id` alone makes a start request idempotent." That is **not, and was never intended to be, an unconditional claim.** The precise, corrected statement is: **a safe retry with the same supplied `p_attempt_id` returns the original attempt if, and only if, the retry's full request identity — `user_email` (owner), `scenario_version_id`, `engine_version`, and `scenario_content_sha256` (content hash) — matches the identity already bound to that id.** This is not a new mechanism added by this correction; it is what the SQL draft's existing, unchanged validation order already implements and always has (the table below traces exactly which check enforces each part of that identity match) — this correction only removes the ambiguity in how the claim was **worded**, and adds the missing explicit test case (§13, new test) proving the "same id, different scenario version" path specifically. **A retry using the same `p_attempt_id` with ANY mismatched identity component fails closed — it is never treated as a safe retry, and never silently "adopts" the retry's new values.**

| Retry scenario | Identity component that differs | Behavior |
|---|---|---|
| Same `p_attempt_id`, identical start identity (same owner, `scenario_version_id`, `engine_version`, `scenario_content_sha256`), retried after the first call already committed | none — full match | **Idempotent** — the second call takes the resume branch (a row now exists), the equality check (§4, row 4) passes trivially, the original row is returned unchanged, `created=false` |
| Retry after the first call's `INSERT` committed but the response was lost (timeout) | none — full match | Identical to the row above — safe, idempotent, no duplicate row (the `PRIMARY KEY` guarantees this even before the resume-branch logic is reached, since the retry's own advisory lock + resume-branch `SELECT` will find the just-created row) |
| Same `p_attempt_id`, but the client's retry supplies a *different* `p_initial_serialized_state`/`p_initial_current_scene_id` | not an identity component (these are create-only inputs, never compared against an existing row) | **Still safe**, by the existing, unchanged contract: those two fields are documented as "used ONLY when a new attempt is actually created" and are silently ignored on the resume branch — the originally persisted state always wins, never the retry's possibly-stale belief |
| Same `p_attempt_id`, **different owner** (`user_email`) | owner | **Fails closed** — the caller's own resume-branch lookup is scoped to its own `v_user_email` and never finds the original row; falls to the create branch and hits `attempt_id_collision` on the `PRIMARY KEY`. Never treated as a retry. |
| Same `p_attempt_id`, **different `scenario_version_id`** (same owner) | scenario version | **Fails closed** — the resume-branch lookup is scoped to the *retry's own* `p_scenario_version_id` and never finds the original row (which was created under a different version); falls to the create branch and hits `attempt_id_collision` on the `PRIMARY KEY`. Never treated as a retry. **This exact case is now its own explicit SQL test** (§13, new test), closing the gap the independent security review identified (SA-06-1). |
| Same `p_attempt_id`, **different `engine_version`** or **different `scenario_content_sha256`** (same owner+version) | content/engine identity | **Fails closed**, and even earlier than the two rows above — these are validated unconditionally, *before* the resume/create branch decision is ever reached, against the scenario version's own pinned values, raising the pre-existing `engine_version_mismatch`/`content_hash_mismatch` errors regardless of `p_attempt_id`. Never treated as a retry. |
| Retry **before** the first call ever committed (e.g., the first call is still in-flight, or definitely failed with no commit) | n/a | Proceeds as an ordinary new-attempt call; if the first call is still in-flight, the advisory lock serializes the two, and the second to acquire the lock takes the resume branch once the first commits, or the create branch if the first rolled back |
| UUID collision **across attempts** (a supplied `p_attempt_id` matching a **different**, unrelated existing row, any status) | owner and/or version and/or status, generically | **Not** treated as a safe retry — `attempt_id_collision` (§4, row 6) — id equality alone is never treated as sufficient evidence of request equality across different owners/versions/statuses |

**If the SQL draft could not safely implement this** (e.g., if returning the original row after a committed retry required comparing fields the function cannot already cheaply access), this section would be required to say so explicitly and redesign the branch. It does not: every identity component above is already validated by an existing, unchanged check that runs before or as part of the resume/create decision, so no new column, index, or validation logic is required to make this claim precise — only the wording needed correction.

### 11.2 Decision-submission idempotency (existing, unchanged — restated for completeness)

Reused exactly as already hardened in `submit_scenario_decision_v1` and `utils/scenario_persistence.py` (SIM-PERSIST-04C/04E/04F), and as already documented in `SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md` §13:

| Case | Behavior |
|---|---|
| Same idempotency key + identical request (all eight bound fields match) | Safe replay — returns the original committed result, no new row |
| Same key + different `selectedOptionId` | `idempotency_key_conflict` — fails closed |
| Same key + different `expectedSceneId` | `idempotency_key_conflict` — fails closed |
| Same key + different `expectedSequenceNumber` | `idempotency_key_conflict` — fails closed |
| Same `expectedSequenceNumber` + a genuinely different idempotency key (a new, distinct decision attempted at a sequence number that has already advanced) | `sequence_mismatch` — fails closed, distinct from an idempotency conflict; the client's view was simply stale |
| Retry after commit, before the response was read | Safe replay (same as row 1) |
| Retry before commit (first call never wrote anything) | Proceeds as a normal new submission — the idempotency-key lookup finds nothing |
| Idempotency-key reuse **across two different attempts** | **Not** a conflict at all — uniqueness is scoped to `(attempt_id, idempotency_key)`; a UUID reused across unrelated attempts is expected and harmless (unlike attempt-id collision, §11.1, which is scoped globally via the table's `PRIMARY KEY`) |

---

## 12. Concurrency contract summary

See §7 for the new `p_attempt_id`-specific interactions. Nothing about `submit_scenario_decision_v1`'s own row-lock/CAS/idempotency mechanism (`SCENARIO_ENGINE_V2_PERSISTENCE_RESUME_DESIGN.md` §6) changes in this slice — it is not modified.

---

## 13. SQL / RPC test plan (for Slice B/C — specified here, not executed)

**SA-21-1 correction (SIM-PERSIST-V2-02C):** the original 12-test plan below is retained (tests 1-12, with test 8 corrected in place per SA-08-1/the §7 concurrency-contract correction), and extended with tests 13-22 to close the gaps the independent security review identified (SA-06-1, SA-08-1, SA-11-1, SA-12-1, SA-21-1). All exercised inside a single `BEGIN ... ROLLBACK` transaction against a disposable database with this migration already applied, mirroring `supabase/tests/v68_scenario_attempt_persistence_verification.sql`'s own conventions (focused exception-message matching via `SQLERRM LIKE '<prefix>:%'`, never a broad `WHEN OTHERS` counted as a pass, `to_regprocedure`-based exact-signature resolution).

1. **Existing V1 call without `p_attempt_id`** — six positional arguments, no seventh — succeeds, creates a row, `attempt_id` is some server-generated UUID, all 15 return columns match the pre-migration shape exactly.
2. **New V2 call with a supplied UUID** — seven arguments (or six named + `p_attempt_id`) — succeeds, `created = true`.
3. **Persisted row `id` equals the supplied UUID** — `SELECT id FROM scenario_attempts WHERE id = :supplied_uuid` finds exactly one row, and the RPC's own returned `attempt_id` equals it.
4. **Resume returns the same ID** — a second call for the same `(user_email, scenario_version_id)`, with `p_attempt_id` omitted, returns `attempt_id = :supplied_uuid` (the id from test 2) and `created = false`.
5. **Conflicting supplied ID on resume fails** — a third call for the same `(user_email, scenario_version_id)`, now supplying a *different*, fresh UUID as `p_attempt_id`, raises `attempt_id_conflict:` and creates no row, changes no row.
6. **Duplicate UUID across owners fails safely (primary-key collision classification)** — learner B calls with `p_attempt_id` equal to learner A's existing attempt id (from test 2); raises `attempt_id_collision:`; the error message is asserted to **not** contain learner A's email, scenario id, or status; learner A's row is verified unchanged afterward. This is the canonical **primary-key collision** classification case (SA-08-1): assert additionally that `SELECT EXISTS (... WHERE user_email = <learner B's email> AND scenario_version_id = <target> AND status = 'in_progress')` is `false` immediately before the call, proving the corrected handler's "no active row for this caller ⇒ must be the PRIMARY KEY" branch is exercised, not merely its externally-observable outcome.
7. **Retry with same identity is idempotent (exact retry, SA-06-1)** — repeating test 2's exact call (same `p_attempt_id`, same owner, same `scenario_version_id`, same `engine_version`, same `scenario_content_sha256`, same six other arguments) a second time (simulating a lost-response retry) returns the identical row, `created = false`, and `SELECT count(*) FROM scenario_attempts WHERE id = :supplied_uuid` is exactly `1`.
8. **Concurrent starts, different supplied UUIDs, do not create duplicates, and the loser fails closed (SA-08-1 correction — supersedes the pre-correction expectation)** — the existing V68 script's own two-sequential-`INSERT`-attempts simulation (its established technique for exercising the partial-unique-index race inside a single-session transaction), re-run with `p_attempt_id` supplied on both simulated callers using two *different* fresh UUIDs for the same `(user_email, scenario_version_id)` — exactly one row is created (the winner's); the **loser now receives `attempt_id_conflict:`**, not a silent "resumed the winner's attempt" (this supersedes the original Slice A draft's pre-correction expectation that the loser would silently adopt the winner's id — see §5 point 5 and §7). The loser's candidate id is never persisted anywhere; the winner's row is verified unchanged.
9. **Grants remain service-role only** — `has_function_privilege('anon'|'authenticated', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)', 'EXECUTE')` is `false` for both; `has_function_privilege('service_role', ..., 'EXECUTE')` is `true`; `to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)')` (the **old** signature) is asserted `IS NULL` (proving it was actually dropped, not merely shadowed).
10. **No direct table access is introduced** — `pg_proc.prosecdef` is `false` (still `SECURITY INVOKER`); the function's source text (`pg_get_functiondef`) is asserted to contain no `client.table`-equivalent bypass pattern (not meaningful for SQL, but the test asserts the function body contains exactly the same `INSERT INTO public.scenario_attempts (...)` statement shape as before, not a new, separate write path).
11. **Rollback restores original signature, owner, and grants, end-to-end (extended per SA-21-1 item 4)** — after applying the rollback script inside a nested test: `to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)')` `IS NULL` and `to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)')` `IS NOT NULL`; a plain six-argument call succeeds exactly as it did before this migration was ever applied (full Engine V1 round trip: create, resume, verify return shape — not merely signature presence); `pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid = to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)')::oid))` equals the owner captured immediately before the rollback ran (SA-11-1); and the four `REVOKE`/`GRANT` statements are re-verified identical to test 9's assertions.
12. **PostgREST RPC resolution is unambiguous** — `SELECT count(*) FROM pg_proc WHERE proname = 'start_or_resume_scenario_attempt_v1'` is exactly `1` immediately after the migration (proving no lingering second overload), matching the real-world-incident mitigation pattern cited in §3.
13. **Same UUID, different owner (SA-06-1/SA-21-1)** — after test 2 creates learner A's attempt with `p_attempt_id = X`, learner B calls with the identical `p_attempt_id = X` but B's own email/scenario arguments; raises `attempt_id_collision:`; learner A's row is unchanged; distinguishes this idempotency-boundary case from test 6's cross-owner scenario by asserting the *specific* failure reason (owner mismatch prevents the resume-branch lookup from ever finding the row) via the pre-call `SELECT EXISTS` assertion pattern from test 6.
14. **Same UUID, different `scenario_version_id`, same owner (SA-06-1 — closes the exact gap the security review identified)** — learner A, having created attempt `X` under `scenario_version_id = V1` (test 2), calls again with `p_attempt_id = X` but `p_scenario_version_id = V2` (a different, valid, published version); raises `attempt_id_collision:` (never `attempt_id_conflict:`, since the resume-branch lookup is scoped to `V2` and never finds the `V1` row); attempt `X`'s row (still under `V1`) is verified unchanged.
15. **Same UUID, different `engine_version`, same owner+version** — learner A retries with `p_attempt_id = X`, correct `scenario_version_id`, but a stale/incorrect `p_engine_version`; raises the pre-existing `engine_version_mismatch:` (unconditional check, runs before the resume/create branch is ever reached) — never treated as a retry, never `attempt_id_collision`/`attempt_id_conflict`.
16. **Same UUID, different `scenario_content_sha256`, same owner+version** — identical structure to test 15, asserting the pre-existing `content_hash_mismatch:` fires instead.
17. **Unknown unique-violation fail-closed behavior (SA-08-1, best-effort)** — since no third unique-enforcing object exists on `scenario_attempts` today (independently confirmed, §8 of the security review), this exact branch cannot be triggered against the real, unmodified schema. As a best-effort proxy inside the disposable-DB test session only (never applied to any tracked migration), add a temporary, session-local unique constraint unrelated to `(user_email, scenario_version_id)`/`id` that the INSERT can be made to violate, and assert the resulting error is the generic `start_or_resume_failed: unexpected unique constraint violation (...)` with `ERRCODE = 'internal_error'` — **never** `attempt_id_collision`. Drop the temporary constraint at the end of the test regardless of outcome.
18. **Function-owner preservation, forward migration (SA-11-1)** — capture `pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid = <six-arg OID>))` immediately before applying this migration; after applying it, assert `pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid = <seven-arg OID>))` is identical.
19. **Baseline-fingerprint precondition rejects a materially modified function (SA-12-1)** — inside a nested, rolled-back test transaction: `CREATE OR REPLACE FUNCTION` the installed six-argument function with a body that changes (or removes) one of the material marker fragments (e.g., alters the `invalid_user_email:` message text) without changing its signature; then attempt to apply this migration; assert it aborts with the `SLICE-A PRECONDITION FAILED: baseline-fingerprint marker not found ...` message, and that the six-argument function is left completely untouched (the abort happens before the `DROP`).
20. **Existing six-argument SQL call after migration (already covered by test 1; restated here per SA-21-1 item 5 for explicit cross-reference)** — no new assertion beyond test 1; recorded here so the correction sequence's checklist is self-contained.
21. **Rollback owner and grant restoration (already covered by the extended test 11; restated here per SA-21-1 for explicit cross-reference)** — no new assertion beyond test 11.
22. **PostgREST schema-cache reload behavior (operational, not automatable as a single-session SQL test — SA-21-1 item 6, restated from the security review)** — cannot be exercised inside a `BEGIN ... ROLLBACK` SQL test (it requires an actual running PostgREST process and its cache-reload timing, not merely the underlying PostgreSQL catalog state). Documented here as a **required manual deployment-runbook step** for Slice C, not an automated test-suite entry: immediately after applying the migration but *before* the `NOTIFY pgrst, 'reload schema'` has had time to propagate, attempt one real RPC call and confirm the expected, safely-retryable `PGRST202`/`undefined_function`-class error (Area 10, race H of the security review); then confirm success on retry once the cache has reloaded.

## 14. Python contract test plan (for Slice D — specified here, not executed)

1. **Snapshot round trip** — `deserialize_run_snapshot_v2(serialize_run_snapshot_v2(run))` recovers every field the envelope contract (§9) declares, exactly, for a run produced by `start_scenario_run_v2`/`apply_decision_v2`/`replay_scenario_run_v2` across empty, partial, and complete histories using the existing `tests/fixtures/scenario_engine_v2_vslice_1_1_0.json` fixture.
2. **Decision round trip** — `deserialize_decision_input_v2(serialize_decision_input_v2(decision))` equals the original `ScenarioDecisionInputV2`, for several `(sequence_number, scene_id, option_id)` combinations from the existing fixture.
3. **Strict integer typing** — `serialize_decision_input_v2`/`deserialize_decision_input_v2` both reject `sequence_number=True`/`sequence_number=1.0`/`sequence_number="1"` with `ScenarioPersistenceV2ValidationError`, never silently coercing.
4. **UUID validation** — `deserialize_decision_input_v2`-adjacent idempotency-key handling (mirroring `_require_uuid4_str`) rejects a non-UUID string, a UUID v1/v5 string (wrong version), and a `uuid.UUID` object passed where a `str` was expected.
5. **NaN/Infinity rejection** — `serialize_run_snapshot_v2` raises when given a (synthetically constructed, invalid) `ScenarioRunV2Snapshot` whose `state` mapping contains `float("nan")`/`float("inf")`; `deserialize_run_snapshot_v2` raises when parsing a payload whose `state` values were smuggled in as JSON-invalid Python floats before serialization (both directions covered, not just one).
6. **`MappingProxyType`/`frozenset` thaw behavior** — every output of `serialize_run_snapshot_v2` is asserted, via `json.dumps(..., allow_nan=False)` succeeding without a `TypeError`, to contain no `MappingProxyType`/`frozenset`/`tuple`/dataclass instance anywhere in its structure (a recursive type-walk assertion, not merely a top-level check).
7. **No input mutation** — for every function in §8, the input object's identity/equality (`==`, and for mutable inputs, a deep-copy-before/after comparison) is asserted unchanged after the call; frozen-dataclass inputs make this structurally guaranteed for §8.1/8.3/8.5/8.6, and is explicitly asserted for the `Mapping`-typed untrusted inputs to §8.2/8.4/8.7/8.8.
8. **Hash mismatch rejection** — `verify_persisted_attempt_identity_v2` raises when `attempt_row_scenario_content_sha256` disagrees with a freshly-loaded `content.canonical_content_sha256`, and separately when the envelope's own `canonicalContentSha256` disagrees with the column value (two distinct test cases, §8.8 point 2).
9. **Attempt identity mismatch rejection** — `verify_persisted_attempt_identity_v2` raises when `attempt_row_id` does not match the attempt identity Engine V2 would replay with (constructed test double, since this should be structurally unreachable in the real service — see §8.8 point 3).
10. **Replay reconstruction equality** — for every decision-history prefix (empty, 1 decision, ..., N decisions, complete) of a fixture-driven run, `replay_serialized_run_v2(content, attempt_id=..., decision_history_payload=serialize_run_snapshot_v2(run)["decisionHistory"])` produces a `ScenarioRunV2Snapshot` field-for-field equal (via `dataclasses.asdict`-style structural comparison) to the original `run` at that same point.
11. **Terminal outcome agreement** — for a completed fixture run, the envelope's minimal `terminalResult` summary (§9) matches `run.terminal_result.outcome_id`/`display_score` exactly, and `verify_persisted_attempt_identity_v2` + a full replay agree on `outcome_id` even when only the minimal summary (not the full `ScenarioTerminalResultV2`) was ever persisted.
12. **Learner-safe serialization excludes hidden fields** — `serialize_learner_scene_view_v2`/`serialize_terminal_view_v2` output, recursively key-walked, contains **none** of: `evaluationTier`, `debriefSeed`, `stateDelta`/`stateChanges`, `flagsSet`/`flagsCleared`/`setFlags`/`clearFlags`, `presentedDialogueVariantId`/`nextDialogueVariantId`, `competencyTags`, `classification`/`classificationTrace`, `severeCapId`/`moderateCapId`/`disqualifiedOutcomeIds`/`guardTieBreakApplied` — mirroring `tests/test_scenario_engine_v2.py::TestLearnerSafeViews`'s existing exclusion list exactly, extended to the JSON-serialized form.

**SA-21-1 correction (SIM-PERSIST-V2-02C):** tests 13-20 below are added to close the gaps the independent security review identified (SA-16-1, and Area 21 items 7-10).

13. **`decisionHistory` excludes every `DebriefTraceEntry`-internal field (SA-16-1, the primary required test)** — construct a fixture-driven run with at least one decision whose underlying `DebriefTraceEntry` has non-default, easily-detectable values for every one of its twelve excluded fields (e.g., a distinctive `evaluation_tier` string, a non-empty `debrief_seed` mapping, non-zero `state_delta` values, non-empty `flags_cleared`/`flags_set`, non-`None` `presented_dialogue_variant_id`, non-empty `competency_tags`). Call `serialize_run_snapshot_v2(run)`, then recursively key-walk the resulting `decisionHistory` array specifically (distinct from test 12, which covers the learner-safe *views*, not the persisted *envelope*) and assert it contains **none** of: `evaluationTier`, `debriefSeed`, `stateDelta`, `stateAfter`, `flagsCleared`, `flagsSet`, `nextSceneId`, `enteredCorrective`, `skippedCorrective`, `presentedDialogueVariantId`, `nextDialogueVariantId`, `competencyTags` (or their `snake_case` equivalents) — anywhere in the structure, under any key name — and assert each element contains **exactly** the three keys `sequenceNumber`, `sceneId`, `optionId`.
14. **`decisionHistory` excludes evaluation tier specifically (explicit sub-case of test 13, per the task's own itemized ask)** — a standalone, narrowly-scoped assertion that `"evaluationTier"` does not appear as a substring anywhere in `json.dumps(serialize_run_snapshot_v2(run)["decisionHistory"])`, kept separate from test 13's broader recursive walk for a more targeted failure message if this specific field ever regresses.
15. **`decisionHistory` excludes debrief seed specifically (explicit sub-case of test 13)** — identical structure to test 14, for `"debriefSeed"`.
16. **`decisionHistory` excludes state delta and flags specifically (explicit sub-case of test 13)** — identical structure to test 14, asserting the joint absence of `"stateDelta"`, `"stateAfter"`, `"flagsCleared"`, and `"flagsSet"`.
17. **`deserialize_decision_input_v2` rejects a `decisionHistory` element smuggling an excluded field** — a payload `{"sequenceNumber": 1, "sceneId": "SC001-C01", "optionId": "opt-a", "evaluationTier": "exceeds_expectations"}` (an otherwise-valid decision triple with one excluded field appended) raises `ScenarioPersistenceV2ValidationError` with an `unexpected_field:`-prefixed message (§8.4), proving the exclusion is enforced on untrusted input, not merely on the engine's own well-formed output.
18. **Corrupted envelope ignored for replay but reported (Area 21 item 8)** — construct a syntactically-valid-but-semantically-corrupted envelope payload (e.g., `decisionHistory` present and internally consistent, but `currentSceneId` set to a scene id that does not match where that `decisionHistory` would actually leave the run) and assert that `replay_serialized_run_v2` — which never consults the envelope's cached `currentSceneId`/`state`/`counters`/`flags` at all (§8.7) — produces the *correct*, replay-derived scene id regardless, while the caller-level cache-agreement comparison (§19 of the parent design; step 5 of §8's replay/identity sequence, named explicitly per SA-19-1's recommendation) detects and reports the mismatch rather than silently trusting the corrupted cached field. Explicitly proves the "cache is never authoritative" principle with an adversarial, not merely well-formed, input.
19. **Completed-outcome mismatch fails closed (Area 21 item 9, negative counterpart of test 11)** — construct a persisted envelope whose minimal `terminalResult` summary (`outcomeId`/`displayScore`) disagrees with what a fresh `replay_serialized_run_v2` + engine evaluation actually produces for the same canonical decision history; assert this disagreement is detected and raised (not silently trusted) by the caller-level comparison step, extending test 11's positive-agreement case with its required negative counterpart.
20. **JSONB round-trip equality (Area 21 item 7, SA-17-1)** — for two independently-constructed-but-logically-identical `ScenarioRunV2Snapshot` values (same fields, but built via different code paths so their underlying Python `dict`/`Mapping` key-insertion order may differ), assert `json.loads(json.dumps(serialize_run_snapshot_v2(a)))` equals `json.loads(json.dumps(serialize_run_snapshot_v2(b)))` as Python values (`==`), even though the raw `json.dumps(...)` *string* output may legitimately differ in key order — proving Python-side dict key-insertion order is never relied upon for equality, only `jsonb`-normalized (or `json.loads`-normalized, as a test-only proxy for it) comparison.

---

## 15. Files created / modified

**Created (SIM-PERSIST-V2-02A):**
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md` (this file)
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_MIGRATION_DRAFT.sql`
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_ROLLBACK_DRAFT.sql`

**Created (SIM-PERSIST-V2-02B, review-only):**
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_SECURITY_REVIEW.md`

**Modified (SIM-PERSIST-V2-02C — this correction pass):**
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md` (this file — §8.1/§8.2/§9 for SA-16-1; §3.9/§3.10 added for SA-11-1/SA-12-1; §4/§5/§7 for SA-08-1's related consistency fix; §11.1 for SA-06-1; §13/§14 for SA-21-1)
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_MIGRATION_DRAFT.sql` (SA-08-1, SA-11-1, SA-12-1)
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_ROLLBACK_DRAFT.sql` (SA-11-1, SA-12-1, applied symmetrically)

**Created (SIM-PERSIST-V2-02C):**
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CORRECTION_REPORT.md`

No table, column, index, trigger, RLS policy, application runtime file, or test file has ever been changed across any of these tasks. No migration has been applied; no database (production or disposable) has been connected to.
