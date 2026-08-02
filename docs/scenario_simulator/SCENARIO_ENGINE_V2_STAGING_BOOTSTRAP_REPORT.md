# SCENARIO_ENGINE_V2_STAGING_BOOTSTRAP_REPORT

Task: SIM-STREAMLIT-V2-03A  
Model: Sonnet High  
Baseline HEAD: `ea67f6e` — Document Engine V2 non-production publication verification

## 1. Purpose

Bootstrap the empty CertBound Staging Supabase database with the repository's
committed migrations required by the current application and Engine V2 scenario
simulator, then verify schema/RPC/RLS readiness. CB-SC-001 publication was
intentionally excluded from this task.

## 2. Task status

**STOPPED (RESUME ATTEMPT 2) — migrations still not applied; base schema gap discovered**

### Attempt 1 (SIM-STREAMLIT-V2-03A)

Bootstrap verification reached the staging target and confirmed an empty project
state, but migration execution could not proceed because neither of the
repository-supported remote migration credentials was available locally:

- `SUPABASE_ACCESS_TOKEN` (for `supabase link` + `supabase db push --linked`)
- `DATABASE_URL` / `SUPABASE_DB_PASSWORD` (for `supabase db push --db-url`)

API keys (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`) are
present and working, but those alone are insufficient for DDL migration apply.

### Attempt 2 (SIM-STREAMLIT-V2-03A-RESUME)

Both `SUPABASE_ACCESS_TOKEN` and `SUPABASE_DB_PASSWORD` were confirmed present
(Windows User environment variables, Cursor fully restarted beforehand).
Target identity was re-verified (unchanged, still empty). `supabase link
--project-ref oohxenhwzcjzagwsrrvq` **succeeded** and confirmed the linked
project reference. `supabase db push --linked --dry-run` **failed** with a
Postgres authentication error:

```
failed to connect to postgres: failed to connect to
`host=aws-0-us-east-1.pooler.supabase.com user=postgres.oohxenhwzcjzagwsrrvq database=postgres`:
failed SASL auth (FATAL: password authentication failed for user "postgres" (SQLSTATE 28P01))
Connect to your database by setting the env var correctly: SUPABASE_DB_PASSWORD
```

Per the task's explicit stop condition ("stop if the dry-run fails"), execution
halted immediately. No migration was applied, no schema was touched, no retry
was attempted. The failure is a **credential value problem** (the configured
`SUPABASE_DB_PASSWORD` value is being rejected by Postgres for the `postgres`
role on this project), not a migration content or target-identity problem.

## 3. Pre-flight

```
Write-Output "shell-ok"
git status --short --branch
git log -1 --oneline
```

Result (both attempts): branch `main`, HEAD `ea67f6e`, nothing staged, only
protected/unrelated untracked paths remain (plus this report, and — after
Attempt 2's `supabase link` — a new untracked `supabase/.temp/` directory
containing only non-secret CLI link metadata: project ref and pooler hostname,
no password).

Focused baseline tests (re-run identically in both attempts):

```
python -m pytest tests/test_scenario_streamlit_v2.py tests/test_scenario_controller_v2.py tests/test_scenario_supabase_port_v2.py tests/test_scenario_catalog.py -q -rs
```

Result: **189 passed, 4 skipped, 39 subtests passed** (4 documented inherited skips) — identical both times.

### Credential presence (Attempt 2)

| Variable | Present |
|---|---|
| `SUPABASE_ACCESS_TOKEN` | true |
| `SUPABASE_DB_PASSWORD` | true |

## 4. Target identity verification

| Field | Safe value |
|---|---|
| Hostname | `oohxenhwzcjzagwsrrvq.supabase.co` |
| Project reference | `oohxenhwzcjzagwsrrvq` |
| Environment label | CertBound Staging candidate (user-authorized bootstrap) |
| Remote hosted Supabase | Yes |
| Old placeholder excluded | Yes — not `qa-local-placeholder.supabase.co` |

### Evidence target is staging (independent signals)

1. **Configured project reference changed** from the prior placeholder ref to
   `oohxenhwzcjzagwsrrvq`.
2. **User explicitly authorized** CertBound Staging bootstrap in task
   SIM-STREAMLIT-V2-03A.
3. **Empty/new project state** confirmed via API (no CertBound public tables;
   zero auth users).
4. **Known production project name differs** — historical production project
   name documented as `salesforce-exam-prep`; no production ref is committed in
   repository configuration, and the configured ref is not the old placeholder.

### Evidence production is excluded

- Configured hostname is not the old local placeholder.
- No production Supabase hostname/reference is committed in repository files.
- Historical production project name (`salesforce-exam-prep`) is distinct from
  the newly configured staging ref.
- Starting database state shows zero auth users and zero CertBound application
  tables — inconsistent with a live learner production database.
- No Render deployment or production environment access was performed.

## 5. Credential safety

- No service-role key, anon key, JWT, password, or connection string was
  printed.
- `.streamlit/secrets.toml` contents were not echoed.
- Only presence/absence metadata and safe hostnames/refs were recorded.

| Credential material | Status |
|---|---|
| `SUPABASE_URL` | present |
| `SUPABASE_ANON_KEY` | present |
| `SUPABASE_SERVICE_ROLE_KEY` | present |
| `SUPABASE_ACCESS_TOKEN` | missing (Attempt 1) → present (Attempt 2) |
| `SUPABASE_DB_PASSWORD` | missing (Attempt 1) → present (Attempt 2) |
| `DATABASE_URL` | missing (both attempts) |

Neither credential value was printed, echoed, logged, or included in this
report in either attempt.

## 6. Starting database state (read-only)

Inspected via Supabase API using the configured service-role client (no writes,
no learner rows accessed):

| Check | Result |
|---|---|
| API connection | OK |
| CertBound public tables discovered | 0 |
| Auth users | 0 |
| Scenario records | 0 (table absent) |
| Scenario attempts | 0 (table absent) |
| Scenario decisions | 0 (table absent) |
| Migration history table | not present / not reachable pre-bootstrap |

Expected empty-project posture confirmed. No production-like learner data
observed.

### Re-verification (Attempt 2, immediately before link/dry-run)

| Check | Result |
|---|---|
| Hostname matches `oohxenhwzcjzagwsrrvq.supabase.co` | true |
| Project ref matches `oohxenhwzcjzagwsrrvq` | true |
| Old placeholder selected | false |
| Public tables found | 0 |
| Auth users | 0 |
| Empty project confirmed | true |

Target identity and empty state were unchanged from Attempt 1.

## 7. Migration workflow selected

**Repository-supported Supabase CLI workflow**

Preferred path once credentials are supplied:

```
supabase link --project-ref oohxenhwzcjzagwsrrvq --yes
supabase db push --linked --yes
```

Fallback path:

```
supabase db push --db-url [REDACTED] --yes
```

Manual dashboard copy/paste of individual SQL files was **not** used.

## 8. Migration plan

| Item | Value |
|---|---|
| Migration directory | `supabase/migrations/` |
| Migration count | **53** |
| Order | UTC timestamp filename order (deterministic) |
| Modify migration files | No |
| Create new migrations | No |
| Seed CB-SC-001 | No |
| Create learner attempts/decisions | No |

### Engine V2 / simulator-critical migrations

- `20260718170000_v66_scenario_definition_persistence_foundation.sql`
- `20260719003000_v67_harden_scenario_definition_security.sql`
- `20260719130000_v68_scenario_attempt_persistence_foundation.sql`
- `20260719140000_v69_scenario_v2_attempt_identity_support.sql`

### Destructive-operation review

Repository scan result:

- **No `DROP TABLE` migrations** in the committed set.
- Controlled `DROP FUNCTION` / `DROP TRIGGER IF EXISTS` statements exist in
  later corrective migrations (expected replacement pattern, not data destruction).
- Some RPC bodies contain scoped `DELETE` statements for publish/repair flows;
  no table truncation migrations identified.
- No migration requires production-only secrets.

Because migrations were **not applied**, no rollback/recovery action was
required beyond leaving the project unchanged.

## 9. Migration execution result

**Not applied in any attempt** (Attempt 3 executed `supabase db push
--linked --yes`, but it failed on the first file and rolled back cleanly —
see Attempt 3 below).

### Attempt 1

Stopped at credential gate. Attempted CLI link without token:

```
supabase link --project-ref oohxenhwzcjzagwsrrvq --yes
```

Result: access token required (`supabase login` or `SUPABASE_ACCESS_TOKEN`).

### Attempt 2

```
supabase link --project-ref oohxenhwzcjzagwsrrvq
```

Result: **succeeded** — `{"project_ref":"oohxenhwzcjzagwsrrvq","message":""}`.
Linked project reference matches the verified staging target exactly.

```
supabase db push --linked --dry-run
```

Result: **failed** —

```
DRY RUN: migrations will *not* be pushed to the database.
Connecting to remote database...
failed to connect to postgres: failed to connect to
`host=aws-0-us-east-1.pooler.supabase.com user=postgres.oohxenhwzcjzagwsrrvq database=postgres`:
failed SASL auth (FATAL: password authentication failed for user "postgres" (SQLSTATE 28P01))
Connect to your database by setting the env var correctly: SUPABASE_DB_PASSWORD
```

Execution halted per the explicit stop condition ("stop if the dry-run fails").
`supabase db push --linked --yes` was **not** run. No migration was applied,
no pending migration list was obtained, and no schema/data was touched.

This is a Postgres role-password rejection for the `postgres` role on the
linked project — not a target-identity mismatch (the link step independently
confirmed the correct project ref via the Management API path, which uses
`SUPABASE_ACCESS_TOKEN`, before the dry-run's direct-Postgres connection using
`SUPABASE_DB_PASSWORD` failed).

### Attempt 3 (SIM-STREAMLIT-V2-03A-RESUME-2)

Pre-flight repeated and confirmed identical to Attempts 1–2:

- Branch `main`, HEAD `ea67f6e`, nothing staged (only protected/unrelated
  untracked paths, this report, and the CLI-generated `supabase/.temp/`
  metadata directory).
- `SUPABASE_ACCESS_TOKEN` present, `SUPABASE_DB_PASSWORD` present (Windows
  User environment variables, corrected value, Cursor fully restarted per
  user statement).
- Baseline focused tests: **189 passed, 4 skipped, 39 subtests passed**
  (identical to Attempts 1–2).
- Existing link metadata (`supabase/.temp/project-ref`) already contained
  `oohxenhwzcjzagwsrrvq`, matching the expected target exactly — no relink
  was performed, per instruction to relink only if verification proved the
  link missing or incorrect.
- Independent read-only target re-verification (service-role API probe):
  hostname and project ref match expected values, project confirmed empty
  (0 public tables, 0 auth users).

`supabase db push --linked --dry-run` **succeeded this time** — the
`SUPABASE_DB_PASSWORD` correction resolved the prior authentication failure.
The dry-run connected to the remote database and listed all 53 local
migrations as pending, with no authentication error, no target mismatch, and
no ambiguous history. A pre-apply scan of the dry-run's migration ordering
found no unexpected destructive statements and matched the repository's
53-file migration count exactly.

Per the task instructions, the dry-run being safe authorized proceeding to:

```
supabase db push --linked --yes
```

**This failed on the very first migration** (`20260623000000_v44_question_version_foundation.sql`):

```
Applying migration 20260623000000_v44_question_version_foundation.sql...
ERROR: relation "public.questions" does not exist (SQLSTATE 42P01)
At statement: 0
CREATE TABLE IF NOT EXISTS public.question_versions (
    ...
    question_id integer NOT NULL REFERENCES public.questions(id),
    ...
```

**Root cause — critical, unplanned blocker:** the committed migration set in
`supabase/migrations/` is not a self-contained schema bootstrap. It is a
sequence of *additive* migrations designed to layer on top of a pre-existing
base schema (`public.questions` at minimum, and by extension the other core
application tables such as `certifications`, `app_users`, etc.) that was
never captured as a migration in this repository. This is corroborated by
`supabase/README.md`'s own "Verified schema facts" table, which records
`public.questions.id` as `integer (int4)` "Verified: 2026-06-23" — i.e.
verified directly against a live database where that table already existed,
not created by any file in `supabase/migrations/`. The staging project
`oohxenhwzcjzagwsrrvq`, being a genuinely empty new project, has no such
base schema, so the very first additive migration fails immediately.

**This is not a credential, target-identity, or destructive-SQL problem.**
It is a structural gap between what the repository's migration set assumes
and what a truly empty Supabase project contains. Per the task's stop
conditions (and the explicit prohibition on modifying migrations or source),
execution halted immediately. No workaround, schema reconstruction, or new
migration was written or attempted.

**Post-failure state verification (critical safety check):**

| Check | Result |
|---|---|
| `public_table_count` (API probe) | 0 (unchanged) |
| `auth_users_count` | 0 (unchanged) |
| `supabase migration list --linked` | all 53 entries show empty `remote` field — nothing recorded as applied |
| Partial/orphaned objects | None found — the failed migration's transaction rolled back cleanly |

The database was left in exactly the same empty state as before the push
attempt. The failure was safe: Supabase CLI applies each migration file in
its own transaction, so the single failing statement caused a clean rollback
of that migration only, and no subsequent migration was attempted.

## 10. Post-migration verification

Not reached in any of the three attempts (migrations still not applied — in
Attempt 3, migration application was attempted but failed on the first file
and rolled back cleanly, so there is no post-migration schema to verify).
Pre-bootstrap application diagnostic (read-only, unchanged across all three
attempts):

| Check | Result |
|---|---|
| Supabase admin client creation | OK (API reachable) |
| `diagnose_cb_sc001_publication_readiness(...)` | `ready = false` |
| Findings | `["scenarios_lookup_failed"]` |

This is the expected pre-bootstrap state because the `scenarios` table does not
exist yet. This diagnostic was re-confirmed read-only in Attempt 3 as the
final state check (project remains unchanged, so the finding is identical).
No RLS, grants, or RPC verification could be performed because no schema
objects exist. Full post-migration verification (migration history, required
CertBound tables, Engine V2 RPCs, RLS, grants, row counts) cannot proceed
until the base-schema gap described in Attempt 3 is resolved.

## 11. Files modified

**None** in any of the three attempts — only this report was created/updated.
No source, migration, test, or environment file was modified. `supabase/.temp/`
is CLI-generated local link metadata (project ref + pooler hostname only, no
password), not a repository source file, and was not staged.

## 12. Git safety

Ending status unchanged except for this report file and the untracked
`supabase/.temp/` CLI metadata directory:

- Branch: `main`
- HEAD: `ea67f6e`
- Nothing staged, committed, pushed, or deployed

## 13. Remaining risks

1. **Migration apply is still blocked** — not by credentials (both
   `SUPABASE_ACCESS_TOKEN` and `SUPABASE_DB_PASSWORD` now work correctly and
   the dry-run succeeds), but by a **missing base schema**. The committed
   migrations in `supabase/migrations/` assume `public.questions` (and other
   core application tables) already exist; a genuinely empty Supabase project
   has no such tables, so the first migration fails immediately.
2. **No migration in this repository creates the base schema** (`questions`,
   and by extension `certifications`, `app_users`, and any other tables the
   V44+ migrations reference by foreign key or RPC). This must be sourced
   from outside this task's scope — e.g. a schema dump/export from the
   existing production database, or a new "V0 base schema" migration
   authored and reviewed separately — before this staging bootstrap can
   proceed. This task does not write or propose that migration, per its
   explicit prohibition on modifying migrations/source.
3. Project identity is proven via three independent credential paths across
   attempts (`SUPABASE_ACCESS_TOKEN` via `supabase link`,
   `SUPABASE_SERVICE_ROLE_KEY` via the PostgREST API probe, and now
   `SUPABASE_DB_PASSWORD` via a successful dry-run direct-Postgres
   connection) — all agree on `oohxenhwzcjzagwsrrvq` and an empty starting
   state.
4. The database was left unchanged by the failed apply attempt (verified:
   0 public tables, 0 auth users, empty `remote` field for every migration in
   `supabase migration list --linked`), so there is no cleanup or rollback
   action required before a future attempt.
5. After a base schema is established (by whatever means the user/team
   decides is appropriate, outside this task), the existing 53 migrations in
   `supabase/migrations/` should apply without further changes — the dry-run
   confirmed their ordering and count are otherwise consistent and no
   authentication or target-identity issue remains.
6. After migrations succeed, repository SQL verification scripts under
   `supabase/tests/` should be run manually against staging (service-role
   only, read-only or transactional rollback scripts as documented per file).

## 14. Staging bootstrap readiness decision

**BLOCKED — BASE_SCHEMA_MISSING (not a credential or target-identity issue)**

Credentials are fully resolved: `supabase db push --linked --dry-run`
succeeded, connecting to the correct target and listing all 53 committed
migrations as pending. Applying those migrations (`supabase db push --linked
--yes`) failed immediately on the first file because the migration set is
additive-only and requires a pre-existing base schema
(`public.questions` at minimum) that does not exist on this empty project and
is not created by any file in `supabase/migrations/`. The database was left
unchanged (verified 0 tables, 0 auth users, no migration recorded as
applied). CB-SC-001 was not published. No source, migration, or test file was
modified. No destructive SQL was executed or observed.

## 15. Recommended next task

A new task is required to resolve the base-schema gap before staging
bootstrap can be completed — this is an architectural/process decision
outside this task's scope (no migration authoring or schema modification was
permitted here). Suggested options for the user to choose from, to be
authorized explicitly in a follow-up task:

1. **Export the current production schema** (structure only, no data) for
   the base tables (`questions`, `certifications`, `app_users`, etc.) and
   apply it to staging as a one-time, separately reviewed bootstrap step
   before running `supabase db push`.
2. **Author a new "V0 base schema" migration** capturing the base tables as
   they exist today, add it to `supabase/migrations/` with an earlier
   timestamp than `20260623000000`, and have it reviewed/approved before
   any staging push (per `supabase/README.md`'s destructive/foundational
   change review convention).
3. **Use `supabase db pull`** against a project that already has the base
   schema (if the team has API/DB access to one that is safe to read from)
   to generate a matching base migration automatically.

Once a base schema strategy is chosen and executed, re-run
`supabase db push --linked --dry-run` (link and credentials do not need to
be redone) followed by `--yes`, then proceed with full post-migration
verification (migration history, required tables, Engine V2 RPCs, RLS,
grants, row counts) exactly as originally planned, and only after that
proceed to **SIM-STREAMLIT-V2-03B** for CB-SC-001 publication.

## 16. Base-schema gap resolved (CERTBOUND-DB-BASELINE-01)

Option 2 from §15 above was selected and executed as task
`CERTBOUND-DB-BASELINE-01` (see
`docs/scenario_simulator/CERTBOUND_BASE_SCHEMA_RECONSTRUCTION_REPORT.md` for
the full account). Summary relevant to this report:

- A new migration, `supabase/migrations/20260101000000_v00_certbound_base_schema.sql`
  (timestamp sorts before `20260623000000`), was authored from a schema-only,
  read-only inspection of production (`salesforce-exam-prep`,
  `gagrwlcwcfxmrmoseywb`) over the Supabase Session pooler, with an explicit
  `SET TRANSACTION READ ONLY;` enforced for the entire inspection. No
  production row data was read or copied at any point.
- It reconstructs the 11 CertBound tables that predate V44
  (`languages`, `certifications`, `certification_domains`, `app_users`,
  `user_certification_access`, `questions`, `answer_options`,
  `exam_attempts`, `question_attempts`, `readiness_snapshots`,
  `support_tickets`) in their exact pre-V44 (and pre-V45/V46/V63, where those
  later migrations alter them) shape.
- Proven, in a fully disposable local Supabase CLI environment (never
  staging, never production): the new migration applies to a genuinely empty
  database, all 53 existing migrations then apply completely unchanged
  (54/54 recorded in migration history), and the resulting schema matches
  production with **zero unexplained differences** across every V00 base
  table's columns, constraints, indexes, RLS, policies, and grants.
- CertBound Staging (`oohxenhwzcjzagwsrrvq`) was **not** touched by
  `CERTBOUND-DB-BASELINE-01` — this section only records that the blocker
  documented in §13–§15 above is now resolved in the repository.

**Updated readiness for a future staging bootstrap attempt:** the base-schema
gap that blocked `SIM-STREAMLIT-V2-03A-RESUME-2` no longer exists in Git. A
future staging-bootstrap task can re-run `supabase db push --linked
--dry-run` followed by `--yes` against CertBound Staging; V00 will apply
first (it sorts before V44) and create the 11 base tables, after which all 53
existing migrations are expected to apply exactly as they did in the
disposable full-chain proof. This has not yet been attempted against the
actual staging project as of this writing — that remains the next task's
responsibility.
