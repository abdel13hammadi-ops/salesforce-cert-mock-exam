# Scenario Engine V2 Persistence — Slice B Database Validation Report

TASK ID: SIM-PERSIST-V2-03
BASELINE: `6136673` — Complete Scenario Engine V2 vertical slice (branch `main`, unchanged throughout this task)

## 1. Disposable target confirmation

All validation ran against a throwaway PostgreSQL 16 container started specifically for this task:

- Container name: `certbound-v69-scenario-persistence-slice-b`
- Image: `postgres:16` (pulled fresh for this task)
- Connection: local Docker container only, `docker exec ... psql -U postgres -d postgres`, no network exposure beyond the local Docker daemon
- No Supabase project reference, no Supabase URL, no service-role key, no anon key, and no production connection string were used anywhere in this task
- The container held no learner data; every scenario/version/attempt row was created fresh by this task's own fixtures
- The container was destroyed (`docker rm -f certbound-v69-scenario-persistence-slice-b`) at the end of validation; it no longer exists

`supabase start` was attempted first but failed during its own baseline migration replay with `ERROR: relation "public.questions" does not exist (SQLSTATE 42P01)` — an unrelated, pre-existing gap in the repository's full migration history (the scenario-simulator migrations assume tables created by migrations not part of this task's scope). Per the task's disposable-database requirement, the three scenario-simulator foundation migrations were instead applied directly, in order, to a bare `postgres:16` container:

1. `supabase/migrations/20260718170000_v66_scenario_definition_persistence_foundation.sql`
2. `supabase/migrations/20260719003000_v67_harden_scenario_definition_security.sql`
3. `supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql`

This reproduces the exact schema the V69 migration targets, without touching `supabase start`, Supabase Cloud, or any other repository component.

## 2. Migration filename

`supabase/migrations/20260719140000_v69_scenario_v2_attempt_identity_support.sql`

Derived exactly from the reviewed `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_MIGRATION_DRAFT.sql`, with two corrections discovered only by executing it against a live database (impossible to catch by static review alone):

- All bare `$$ ... $$` dollar-quoted blocks were replaced with uniquely named tags (`$slice_b_precheck$`, `$slice_b_func$`, `$slice_b_owner$`, `$slice_b_postcheck$`). The original draft's `$$` blocks contained the literal substring `$$` inside a `--` comment, and PostgreSQL's dollar-quote parser does not respect comments when matching delimiters — this prematurely closed the intended block and produced a syntax error.
- The `SECURITY INVOKER` baseline-fingerprint check no longer searches for the literal string `'SECURITY INVOKER'` in `pg_get_functiondef()`'s output (that function never emits `SECURITY INVOKER` explicitly — it only ever emits `SECURITY DEFINER` when applicable, since invoker-rights is the implicit default). The check now queries `pg_proc.prosecdef` directly and raises if it is `true`.

No other change was made to the migration's logic, structure, or SQL. It is not simplified relative to the reviewed draft.

## 3. Rollback artifact

`docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_B_ROLLBACK.sql`

Derived exactly from the reviewed `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_ROLLBACK_DRAFT.sql`, placed in the same `docs/scenario_simulator/` documentation location as the other Slice A/B design artifacts (this repository has no separate `rollback/` directory convention — rollback SQL for prior migrations, e.g. V67's hotfix narrative, is likewise documented rather than auto-applied as a forward migration). It is **not** registered under `supabase/migrations/` and will never be applied automatically. The same two live-execution corrections as the migration were required:

- Named dollar-quote tags (`$slice_b_rollback_precheck$`, `$slice_b_rollback_func$`, `$slice_b_rollback_owner$`, `$slice_b_rollback_postcheck$`).
- The `SECURITY INVOKER` check uses `pg_proc.prosecdef` instead of a text-marker match.
- One further fix specific to the rollback: its `v_markers` array checks for the literal substring `caller's` (via SQL string literal `'caller''s'`) inside the *stored* function body. `pg_get_functiondef()`'s `prosrc` output preserves the raw, doubly-escaped `''` a source file's `RAISE EXCEPTION '...caller''s...'` literal actually contains, so the marker literal itself needed four quote characters (`'caller''''s'`) to correctly represent two literal apostrophes when compared against the raw stored text.

## 4. Exact objects changed

Confirmed by an explicit `pg_dump --schema-only` and `pg_get_functiondef` diff across three states — the original post-migration state, the post-rollback (pre-migration) state, and the post-reapply state — all captured from the *same* running container so the comparison is exact, not reconstructed from documentation:

| Comparison | Result |
|---|---|
| `scenario_attempts` + `scenario_decisions` table DDL: post-rollback vs. original post-migration | **0 differences** |
| `scenario_attempts` + `scenario_decisions` table DDL: original post-migration vs. post-reapply | **0 differences** |
| `get_scenario_attempt_v1` + `abandon_scenario_attempt_v1` definitions: post-rollback vs. original post-migration | **0 differences** |
| `get_scenario_attempt_v1` + `abandon_scenario_attempt_v1` definitions: original post-migration vs. post-reapply | **0 differences** |

Only `start_or_resume_scenario_attempt_v1`'s signature (six-argument → seven-argument, with `p_attempt_id uuid DEFAULT NULL` appended last), body, `COMMENT`, and the associated `REVOKE`/`GRANT` statements change. No table, column, index, constraint, trigger, RLS policy, or unrelated function/grant changed in either direction.

## 5. Pre-migration verification result

The **original, unmodified** `supabase/tests/v68_scenario_attempt_persistence_verification.sql` (copied before any edits in this task) was run against the freshly-applied V66/V67/V68 baseline, before the V69 migration was applied:

```
V1 PASSED ... V62 PASSED
V63 PASSED: no residual test data remains after ROLLBACK.
VERIFICATION SUMMARY: all V1-V63 checks passed for the V68 scenario attempt persistence foundation, as corrected by SIM-PERSIST-04F.
```

All 63 pre-existing checks passed, exit code 0.

## 6. Migration application result

`psql -v ON_ERROR_STOP=1 -f 20260719140000_v69_scenario_v2_attempt_identity_support.sql` completed with exit code 0, transactionally (single `BEGIN ... COMMIT`), no warnings. Post-application spot check confirmed:

- `start_or_resume_scenario_attempt_v1` resolves to exactly one signature: `p_user_email text, p_scenario_version_id uuid, p_initial_current_scene_id text, p_initial_serialized_state jsonb, p_engine_version text, p_scenario_content_sha256 text, p_attempt_id uuid`.
- `SECURITY INVOKER` retained (`prosecdef = false`).
- Owner unchanged (`postgres`, matching the pre-migration owner and matching the untouched `get_scenario_attempt_v1`'s owner).
- No `CASCADE` used anywhere in the migration.

## 7. All SQL test results

The **updated** `supabase/tests/v68_scenario_attempt_persistence_verification.sql` was run in full against the post-migration database (`docker exec -w /tmp/repo/supabase/tests ... psql -f v68_scenario_attempt_persistence_verification.sql`, working directory set so the file's own `\i` includes of the rollback artifact and the migration resolve correctly relative to the checked-out repository layout). Full run, single invocation, exit code 0:

- **V1–V17** (read-only introspection, now asserting the seven-argument signature everywhere the six-argument signature was previously hardcoded — 6 occurrences updated): all PASSED.
- **V18–V62** (row-level Engine V1 behavior, inside `BEGIN ... ROLLBACK`): all PASSED, unmodified from the original file except for the signature-string updates above.
- **SB0–SBV** (new, inside the same transaction): all PASSED —
  - SB-A/D: six-argument positional call still succeeds, still generates a server UUID.
  - SB-B/C: seven-argument call with a supplied UUID succeeds; persisted id equals the supplied UUID.
  - SB-E/F: resume/retry with the identical supplied UUID and matching request identity is idempotent.
  - SB-G/H/I: same UUID with a different scenario version / engine_version / content hash all fail closed (`attempt_id_collision`, `engine_version_mismatch`, `content_hash_mismatch` respectively).
  - SB-J/M: cross-owner UUID collision → `attempt_id_collision`, leaks nothing about the colliding row, original owner's row unchanged.
  - SB-K: existing active attempt + conflicting UUID → `attempt_id_conflict`.
  - SB-N: an unrelated, unknown unique-constraint violation (via a temporary, dedicated-fixture partial unique index) fails closed with the generic `start_or_resume_failed: unexpected unique constraint violation (...)` message, never mislabeled `attempt_id_collision`.
  - SB-L: structural proxy confirming the ordinary, non-racing create path is unaffected by the exception handler's re-query branch (see Section 9 for the full concurrency picture).
  - SB-O/P/Q/R/S/T/U/V: owner, `SECURITY INVOKER`, `search_path`, grants, single-overload, 15-column return shape, and "no table/index/trigger/RLS change" all confirmed on the live seven-argument function.
- **V63**: PASSED — no residual test data after `ROLLBACK`.
- **SB-W/Z**: after applying the rollback artifact, PASSED — original six-argument function restored, no seven-argument overload remains, owner/`SECURITY INVOKER`/grants restored.
- **SB-X/Y**: PASSED — an ordinary six-argument call succeeds post-rollback; a seven-argument call fails with `undefined_function` post-rollback.
- **SB-REAPPLY**: PASSED — migration reapplies cleanly; seven-argument signature, `SECURITY INVOKER`, and grants are intact in the final state.

Final console line: `SLICE B VERIFICATION SUMMARY: all SB0-SBV and SB-W/X/Y/Z/REAPPLY checks passed for the V69 Engine V2 attempt-identity migration.`

## 8. RPC compatibility results

`supabase start` (and therefore a local PostgREST instance) could not be brought up against this repository's current migration history (Section 1). RPC compatibility was instead exercised directly at the SQL level — the layer PostgREST itself calls into — via positional-argument calls exactly matching how PostgREST invokes `rpc/start_or_resume_scenario_attempt_v1`:

- Six-argument positional compatibility: SB-A/D, SB-X.
- Seven-argument supplied-UUID compatibility: SB-B/C, SB-E/F.
- Single-overload requirement (a precondition for PostgREST to resolve the RPC unambiguously): SB-T, and re-confirmed post-rollback (SB-W) and post-reapply (SB-REAPPLY).

Named-argument-style PostgREST calls were not exercised against a live PostgREST/HTTP layer; this is recorded as a limitation in Section 12.

## 9. Concurrency results

Genuine two-session concurrency was exercised using two independent `docker exec ... psql` processes per case, each opening its own connection/transaction and synchronized with `pg_sleep(2)` so both reliably overlap before calling the RPC:

| Case | Result |
|---|---|
| Same owner, same supplied UUID, concurrent | One session: `created=true`. Other session: `created=false`, identical `attempt_id`. No error, no duplicate row. |
| Same owner, two different supplied UUIDs, concurrent | Winner: `created=true` with its own UUID. Loser: `attempt_id_conflict` (deterministic, fail-closed). |
| **Different owners, same supplied UUID, concurrent** | Winner: `created=true`. Loser: `attempt_id_collision` — this is the one case **not** serialized by the `pg_advisory_xact_lock` (which is keyed per-owner), so this genuinely raced at the database's own unique-index level and hit the RPC's real `EXCEPTION WHEN unique_violation` handler under true concurrent load, not a single-session proxy. The `CONTEXT` line in the loser's error confirmed execution reached the `attempt_id_collision` `RAISE` inside the exception handler, not the ordinary conflict check. |

Result: exactly one active attempt existed in every case; no duplicate decisions or attempts were created; winner/loser behavior was deterministic; the loser's error never disclosed the winning owner's email, status, or any other row data.

**Unresolved limitation, stated explicitly and not fabricated:** the "ordinary active-attempt race is not mislabeled as UUID collision" branch (`v_active_exists = true` inside the `unique_violation` handler) could not be triggered via genuine two-session concurrency through the RPC itself. This is a structural property of the migration's own design, not a gap in test effort: `pg_advisory_xact_lock(hashtext(user_email || ':' || scenario_version_id))` is acquired before the resume-branch `SELECT`, for the caller's full transaction duration, so two concurrent RPC callers for the *same* `(user_email, scenario_version_id)` can never both be inside the lock-protected section at once — the second caller blocks until the first's transaction fully resolves, and by the time it proceeds, the first caller's row is already visible to its own resume-branch `SELECT`, so that caller never reaches its own `INSERT` while a same-key row is in flight. The branch is real, reachable defense-in-depth against a lower-level bypass (e.g., a direct `INSERT` racing the RPC), but not reachable by two ordinary concurrent RPC calls — a structural guarantee this session confirmed by design analysis and by the "different owners" test above (the one legitimate way two callers can hit the same `unique_violation` handler concurrently). The deterministic, single-session SQL proxy for this exact branch classification (SB-N, plus SB-J/M for the sibling PK-collision branch) remains the primary coverage for it, exactly as the reviewed contract's own SQL test plan anticipated.

## 10. Rollback result

Applying `SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_B_ROLLBACK.sql` completed with exit code 0, transactionally. Verified:

- The original six-argument `start_or_resume_scenario_attempt_v1` was recreated with the exact original body (byte-identical `pg_get_functiondef` versus a pre-migration capture).
- No seven-argument overload remained.
- Owner, `SECURITY INVOKER`, `search_path`, and grants (service_role only) were restored exactly.
- The original, unmodified V68 verification script (V1–V63) was rerun against the rolled-back database and passed in full — the strongest available proof that rollback restores the *exact* original contract, not just the function signature.

## 11. Reapply result

Reapplying `20260719140000_v69_scenario_v2_attempt_identity_support.sql` after rollback completed with exit code 0. The seven-argument function, `SECURITY INVOKER`, owner, and grants were reconfirmed identical to the original application (Section 4's diff: 0 differences). The full updated verification script (V1–V63, SB0–SBV, SB-W/X/Y/Z/REAPPLY) was then rerun once more in the same invocation and passed in full, leaving the database in the fully-migrated final state.

## 12. Object diff result

See Section 4. `scenario_attempts`, `scenario_decisions`, their indexes (`idx_scenario_attempts_one_in_progress`, `idx_scenario_attempts_scenario_version_id`, `idx_scenario_attempts_user_email_status`, both primary keys, both `scenario_decisions` unique constraints), triggers (`trg_guard_scenario_attempt_mutation`, `trg_guard_scenario_decision_immutability`), and RLS status (disabled, zero policies, matching V68) are unchanged in every direction of the migration/rollback/reapply cycle.

## 13. Cleanup/disposal result

`docker rm -f certbound-v69-scenario-persistence-slice-b` was run after all validation completed; `docker ps -a` confirms the container no longer exists. No data from this task persists anywhere outside this repository's own working tree.

## 14. Unresolved limitations

- **PostgREST/HTTP-layer RPC compatibility** (named-argument calls, `Prefer` headers, actual HTTP round-trip) was not exercised, because `supabase start` cannot currently reproduce this repository's full schema (Section 1, pre-existing gap unrelated to this task). SQL-level positional-argument compatibility, which is what PostgREST itself ultimately executes, was fully exercised instead.
- **The advisory-lock-protected "ordinary active-attempt race" branch** (Section 9) could not be triggered via genuine multi-session concurrency through the RPC itself, by design — the lock structurally prevents it for legitimate callers. Coverage for that exact classification branch remains the deterministic SQL-level proxy (SB-N) plus the fully genuine, two-session "different owners, same UUID" race (which exercises the sibling PK-collision branch of the very same exception handler under real concurrent load).

## 15. Files created / modified in this task

Created:
- `supabase/migrations/20260719140000_v69_scenario_v2_attempt_identity_support.sql`
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_B_ROLLBACK.sql`
- `docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_B_DB_VALIDATION_REPORT.md` (this file)

Modified:
- `supabase/tests/v68_scenario_attempt_persistence_verification.sql`

No production database was connected to, modified, or affected. No Python source or Python test files were modified. Nothing was staged, committed, pushed, or deployed.
