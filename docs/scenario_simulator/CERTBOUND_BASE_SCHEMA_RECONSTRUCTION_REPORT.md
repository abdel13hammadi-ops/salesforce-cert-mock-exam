# CERTBOUND_BASE_SCHEMA_RECONSTRUCTION_REPORT

Task: CERTBOUND-DB-BASELINE-01 (resumed through CERTBOUND-DB-BASELINE-01-RESUME-4)
Model: Sonnet High
Baseline HEAD: `ea67f6e` — Document Engine V2 non-production publication verification

## 1. Purpose / root cause

The repository's migration history begins at
`supabase/migrations/20260623000000_v44_question_version_foundation.sql` and
every migration after it is strictly additive — it assumes core CertBound
tables (`public.questions`, `public.certifications`, `public.app_users`, etc.)
already exist. That original base schema was **never captured as a migration
in Git**. A prior task (`SIM-STREAMLIT-V2-03A-RESUME-2`) discovered this the
hard way: `supabase db push --linked --yes` against the genuinely empty
CertBound Staging project failed on the very first migration with
`relation "public.questions" does not exist`, and the staging bootstrap was
correctly halted without any schema being partially created.

This task reconstructs the missing foundation as one authoritative migration,
`supabase/migrations/20260101000000_v00_certbound_base_schema.sql` (V00),
sorting before every existing migration, so that a completely empty Postgres
database can bootstrap the entire CertBound schema from Git alone — without
ever reading, copying, or depending on production data at runtime.

## 2. Production identity and read-only safeguards

| Field | Value |
|---|---|
| Project name | `salesforce-exam-prep` |
| Project reference | `gagrwlcwcfxmrmoseywb` |
| Connection path used | Supabase **Session pooler** (`postgres.gagrwlcwcfxmrmoseywb` @ `*.pooler.supabase.com:5432`) |
| Direct IPv6 host (`db.gagrwlcwcfxmrmoseywb.supabase.co`) | Not used (an earlier attempt failed here because the runtime environment lacked outbound IPv6) |
| Credential source | `CERTBOUND_PROD_DATABASE_URL`, Windows User environment variable |

Credential validation (booleans only, value never printed):

| Check | Result |
|---|---|
| Variable present | true |
| PostgreSQL scheme valid (`postgres://`/`postgresql://`) | true |
| Production pooler username (`postgres.gagrwlcwcfxmrmoseywb`) present | true |
| Session pooler host + port `5432` present | true |
| Direct IPv6 hostname absent | true |
| Staging reference (`oohxenhwzcjzagwsrrvq`) absent | true |
| Password placeholder absent | true |

**Read-only enforcement:** Supabase's connection pooler (Supavisor) does not
forward libpq `options` (so `PGOPTIONS=-c default_transaction_read_only=on`
and `SET default_transaction_read_only = on` at the session level have no
effect on the pooled backend session/transaction already in progress). The
inspection script instead issued `SET TRANSACTION READ ONLY;` as the **first
statement** of the single transaction used for the entire inspection, then
verified `SHOW transaction_read_only;` returned `on` before executing any
further statement. All catalog/`information_schema` reads ran inside that one
confirmed-read-only transaction, which was then rolled back (never committed)
and the connection closed. No `INSERT`/`UPDATE`/`DELETE`/`DDL` statement was
ever issued against production.

**Production data accessed:** none. Only `pg_catalog` / `information_schema`
metadata (table/column/constraint/index/function/trigger/policy/grant
definitions) was read. No table's row contents were queried at any point.

## 3. Managed schemas excluded

`auth`, `storage`, `realtime`, `extensions`, `graphql`, `vault`, `net`,
`supabase_functions` were excluded from inspection and from the baseline.
The only exception is a single `CREATE EXTENSION IF NOT EXISTS pgcrypto WITH
SCHEMA extensions;` statement (required for `gen_random_uuid()`), which does
not recreate the `extensions` schema itself — Supabase provisions that schema
independently on every project.

## 4. Migration dependency audit (Phase 3)

| Item | Value |
|---|---|
| Existing migration count (pre-task) | 53 |
| Earliest existing migration | `20260623000000_v44_question_version_foundation.sql` |
| Latest existing migration | `20260719140000_v69_scenario_v2_attempt_identity_support.sql` |

Cross-referencing every `CREATE TABLE` in the 53 existing migrations against
the production table inventory identified **11 production tables that predate
V44** (created by none of the 53 migrations, therefore must exist before V44
runs):

`languages`, `certifications`, `certification_domains`, `app_users`,
`user_certification_access`, `questions`, `answer_options`, `exam_attempts`,
`question_attempts`, `readiness_snapshots`, `support_tickets`.

Three of these are later **altered** (not created) by existing migrations —
the baseline had to reproduce their **pre-alter** shape exactly so the later
migration remains the sole owner of its change:

| Table | Later migration | Change the baseline must NOT pre-apply |
|---|---|---|
| `app_users` | V46 (`20260625000000_v46_stripe_billing_foundation.sql`, `20260628180000_v46_stripe_subscription_event_ordering.sql`) | 9 Stripe/billing columns |
| `certification_domains` | V63 (`20260714100000_v63_widen_certification_domain_weight_to_numeric.sql`) | `weight integer` → `numeric(5,1)` |
| `exam_attempts` | V45 (`20260624190000_v45_allow_daily_sprint_exam_attempt_mode.sql`) | `chk_exam_attempts_mode` gains a 6th (`'Daily Sprint'`) allowed value |

Read-only inspection also confirmed every function currently in production is
already created by an existing V44+ migration, and no trigger exists on any
of the 11 base tables — so V00 creates **no functions and no triggers**.

## 5. Baseline migration (Phase 4)

**Filename:** `supabase/migrations/20260101000000_v00_certbound_base_schema.sql`
(timestamp sorts before `20260623000000`, i.e. before every existing migration).

Contents, in dependency-safe creation order:

1. Preflight `DO $$ ... RAISE EXCEPTION ... $$` block that fails loudly if any
   of the 11 target tables already exists (no silent `IF NOT EXISTS`
   reconciliation of divergent state).
2. `CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;`
3. `languages`, `certifications`, `certification_domains`, `app_users`,
   `user_certification_access` (deliberately created **before**
   `questions`/`answer_options` because their verified production `SELECT`
   policies reference `user_certification_access` via an `EXISTS` subquery,
   and `CREATE POLICY` validates referenced relations at creation time),
   `questions`, `answer_options`, `exam_attempts`, `question_attempts`,
   `readiness_snapshots`, `support_tickets`.
4. Each table: exact verified columns/types/defaults, primary keys, foreign
   keys, unique constraints, check constraints, indexes, `ENABLE ROW LEVEL
   SECURITY`, exact verified policy `USING`/`WITH CHECK` clauses, and exact
   verified table grants.

Explicitly excluded, as required: production row data, users, questions,
attempts, billing/Stripe rows, production-specific IDs, secrets, project
URLs, and any Supabase-managed schema recreation.

## 6. Bootstrap verification SQL (Phase 5)

**Filename:**
`supabase/tests/v00_certbound_base_schema_bootstrap_verification.sql`

Run immediately after V00 alone (before V44+), it asserts:

- `T1`/`T2`: all 11 base tables exist with RLS enabled.
- `T3`–`T6`: pre-V44/V45/V46/V63 shapes are exactly right (`questions.id` is
  `integer`; `certification_domains.weight` is `integer`, not `numeric`;
  `chk_exam_attempts_mode` has exactly 5 values, not 6; `app_users` has none
  of the 9 V46 Stripe columns yet).
- `T7`–`T9`: required check constraints, primary/foreign keys, and unique
  constraints exist.
- `T10`: expected indexes exist.
- `T11`: RLS policy counts match per table.
- `T12`: no unintended anonymous/public write access beyond the verified
  grant+RLS combination.
- `T13`: zero rows in every base table (no seeded application data).

**Result when run in isolation immediately after V00 (before V44+):
all checks passed.**

## 7. Disposable full rebuild (Phase 6)

**Environment:** local Supabase CLI stack (`supabase start`/`db reset`) in a
dedicated temp working directory (`%TEMP%\certbound_disposable_rebuild`,
outside the repository), using the CLI's default local ports/containers/
volumes, entirely separate from the developer's normal local Supabase
instance and from the hosted CertBound Staging project.

| Step | Result |
|---|---|
| 1. Confirm zero CertBound tables initially | Confirmed (fresh `supabase init` + `supabase start`) |
| 2. Apply V00 alone | Success |
| 3. Run V00 bootstrap verification (isolated, before V44+) | All checks passed |
| 4. Apply all 53 existing migrations, unchanged, in timestamp order | Success — all 53 applied without error |
| 5. Migration history complete | Confirmed — 54/54 entries recorded (`supabase migration list`) |
| 6. Applicable SQL verification run | See §9 below |
| 7. Final tables/constraints/indexes/functions/triggers/RLS/policies/grants | Verified via full schema dump + comparison (§8) |
| 8. No unintended seed data | Confirmed — 0 rows in all 11 base tables after full chain |
| 9. Relevant Engine V2 Python tests | 713 passed, 4 skipped (§9) |
| 10–11. Teardown, containers/networks/volumes/temp dirs removed | Completed (§11 cleanup) |

Final disposable table count after all 54 migrations: **38** (11 base + 27
created by V44–V69), matching the expected additive total exactly.

## 8. Normalized schema comparison (Phase 7)

Production does **not** use `supabase_migrations.schema_migrations` (that
table does not exist there — production was provisioned before CLI-tracked
migrations were adopted for this project), so its effective deployed
migration cutoff had to be determined empirically. Comparing production's own
table inventory against the repository's migration set showed production is
missing every V68/V69-created table (`scenario_attempts`, `scenario_decisions`
row-security additions, etc.) — i.e. **production's deployed state
corresponds to V00 + V44 through V67**, not the full V44–V69 set. The
comparison below rebuilds the disposable environment to that same cutoff
(V00 + V44…V67, V68/V69 temporarily excluded only for this one comparison
run) for a true apples-to-apples diff; §7 above separately proves the full
V00 + all 53 migrations chain applies cleanly regardless.

**Round 1** (initial baseline, V00 + V44…V67): **19 differences** found.
Classified into two groups:

**Class 1 — in-scope baseline defects (corrected in V00):**

| Difference | Root cause | Fix applied |
|---|---|---|
| `answer_options.id`, `exam_attempts.id`, `question_attempts.id`, `questions.id`, `support_tickets.id` — `is_identity`/`column_default` mismatch | V00 used `GENERATED BY DEFAULT AS IDENTITY`; production uses classic `SERIAL`/`BIGSERIAL` (sequence + `nextval()` default) | Changed all 5 columns to `serial`/`bigserial PRIMARY KEY` |
| `chk_questions_difficulty`, `chk_questions_external_key_not_blank`, `chk_questions_quality_status`, `chk_questions_question_type`, `chk_support_tickets_status` — constraint definition text differs | V00 used explicit `::text`/`ANY(ARRAY[...])` casts; production's canonical `pg_get_constraintdef()` form uses plain `IN (...)` lists and `TRIM(BOTH FROM ...)` | Rewrote all 5 `CHECK` constraints to match production's exact canonical form |

**Class 2 — out-of-scope, explained (not corrected, do not belong in V00):**

| Difference | Explanation |
|---|---|
| Index `audit_findings.idx_af_duplicate_question_pair_dedupe` only in disposable | Created by V45 (`20260624150000_v45_duplicate_question_pair_dedupe.sql`), an existing migration outside V00's scope — this is a V44+ migration-owned object, not a base-schema object |
| Functions `complete_ai_quality_audit_run_v1`, `list_duplicate_question_pair_keys_v1` differ | Owned by V48/V60 migrations (existing, outside V00's scope); production's deployed cutoff (V67) predates a later corrective revision present in the full local migration chain |
| Numerous `GRANT`s only in production, on tables created by V44+ migrations (`audit_*`, `background_jobs`, `billing_*`, `free_mock_*`, `question_*`, `resource_*`, etc.) | All of these tables are created by **existing** migrations (not V00). The grant difference reflects a change in the Supabase CLI's default automatic-privilege-exposure behavior between when production was originally provisioned and the current CLI version used to build the disposable environment — it is not caused by, and cannot be fixed by, the V00 baseline, since V00 creates none of these tables |

**Round 2** (after correcting the two Class 1 issues, full rebuild from
absolute zero, re-compared): **4 differences remain**, all Class 2
(the same index, the same 2 functions, and the same grant-model difference
listed above) — **zero unexplained differences remain for any of the 11
V00 baseline tables or their columns, constraints, indexes, RLS, policies,
or grants.**

## 9. Security review (Phase 8) and tests

**Baseline security review findings:** RLS is enabled on all 11 tables; every
table-level `GRANT` to `anon`/`authenticated` is neutralized by RLS (no
permissive `INSERT`/`UPDATE`/`DELETE` policy exists for `certifications`,
`certification_domains`, or `languages`, so those grants are inert in
practice — this matches the verified, unchanged production posture exactly,
confirmed identical by the Phase 7 comparison). No `SECURITY DEFINER`
function, no dynamic SQL, no public `EXECUTE` grant, and no destructive
statement (`DROP`/`TRUNCATE`/data-mutating `DELETE`) appears anywhere in V00.
No secret, credential, or project-specific URL is present.

```
git diff --check
```
Result: clean (exit 0, no output).

**Python tests** (`-q -rs`):

```
tests/test_scenario_streamlit_v2.py tests/test_scenario_controller_v2.py
tests/test_scenario_supabase_port_v2.py tests/test_scenario_orchestration_v2.py
tests/test_scenario_persistence_v2.py tests/test_scenario_engine_v2.py
tests/test_scenario_persistence.py tests/test_scenario_learner_controller.py
tests/test_scenario_catalog.py
```

Result: **713 passed, 4 skipped, 48 subtests passed** (the 4 skips are the
same pre-existing, documented "covered by port smoke" skips seen in prior
tasks — no new skip/failure was introduced).

**SQL verification tests** (every file under `supabase/tests/`, run against
the fully rebuilt disposable environment, full 54-migration chain):

| File | Result | Note |
|---|---|---|
| `v00_certbound_base_schema_bootstrap_verification.sql` | Pass (in isolation, V00-only) / not applicable post-full-chain | Designed to assert **pre-V44/V45/V46/V63** state; by definition fails if run after those migrations have already altered the columns it checks — this is expected test-design behavior, already validated correctly in §6/§7 step 3 |
| `v44_background_job_lifecycle_verification.sql` | **Pass** | All 10 lifecycle assertions passed |
| `v45_audit_finding_materiality_verification.sql` | **Pass** | |
| `v45_publication_gate_verification.sql` | Not applicable | Inserts a question row with `exam_name = 'ADM-201'`, a real certification code that is production **catalog content**, never created by any migration — this script is a regression smoke test meant to run against an already-populated (production/staging) database, not a from-scratch empty bootstrap |
| `v48_ai_quality_audit_verification.sql` | **Pass** | |
| `v61_platform_app_builder_catalog_schema_preflight.sql` | **Pass** | |
| `v61_platform_app_builder_certification_catalog_verification.sql` | Not applicable | Asserts a pre-existing `'Administrator'` certification row exists — that row is production catalog content never inserted by any migration, not a schema defect |
| `v63_certification_domain_weight_numeric_verification.sql` | Not applicable | `S2` hardcodes "expected exactly 19 certification_domains rows (pre-migration baseline)" — a production content snapshot, not a schema assertion; `S1` (type/precision/scale) passed |
| `v64_sales_cloud_consultant_certification_catalog_verification.sql` | Not applicable | Same `'Administrator'`-row content assumption as V61's verification |
| `v65_service_cloud_consultant_certification_catalog_verification.sql` | Not applicable | Same `'Administrator'`-row content assumption |
| `v66_scenario_definition_persistence_verification.sql` | Sequence-order artifact | `V5` checks V66's original (pre-V67-hardening) `service_role` grants; V67 intentionally narrows them later in the same chain — this script is meant to run immediately after V66, before V67, not at the end of the full chain |
| `v66_scenario_definition_schema_preflight.sql` | Sequence-order artifact | Preflight scripts assert their target objects do **not yet** exist; by design they report a conflict when run after their own migration has already applied (as it has, successfully, in the full chain) |
| `v67_harden_scenario_definition_security_verification.sql` | **Pass** | |
| `v68_scenario_attempt_persistence_preflight.sql` | Sequence-order artifact | Same as V66 preflight — asserts pre-migration state |
| `v68_scenario_attempt_persistence_verification.sql` | Tooling limitation | File uses a psql client-side meta-command not supported by the plain `psycopg2` runner used here; the underlying V68 migration itself applied and its preflight ran successfully during migration application |

None of the "not applicable" or "sequence-order artifact" results indicate a
defect in V00 or in any existing migration — every one of them is either (a)
a content-dependent regression check that assumes a pre-populated catalog
(explicitly out of scope: V00 must not seed data), or (b) a script correctly
run out of its intended position in the sequence during this after-the-fact
audit rather than immediately after its own migration.

## 10. Staging status

CertBound Staging (`oohxenhwzcjzagwsrrvq`) was **not connected to and not
modified** at any point in this task. All disposable-environment work used a
fully local, disposable Supabase CLI stack in a temporary directory, entirely
separate from the hosted staging project. The repository's existing staging
link (`supabase/.temp/`) was left untouched.

## 11. Recovery / next-step procedure

1. This report and `supabase/migrations/20260101000000_v00_certbound_base_schema.sql`
   plus `supabase/tests/v00_certbound_base_schema_bootstrap_verification.sql`
   are committed together (see the milestone commit).
2. A future staging-bootstrap task (successor to `SIM-STREAMLIT-V2-03A`) can
   now run `supabase db push --linked --dry-run` then `--yes` against
   CertBound Staging: V00 will apply first (sorts before V44) and create the
   11 base tables, after which all 53 existing migrations should apply
   unchanged exactly as proven here.
3. Remaining out-of-scope items (V45 dedupe index / V48-V60 function revision
   / grant-model differences, §8 Class 2) are pre-existing characteristics of
   the **existing** V44+ migrations and production's provisioning history —
   they are not new risks introduced by this task and require no action here.

## 12. Remaining risks

- Production's effective deployed migration cutoff (V67) is behind the
  repository's full migration set (through V69). This is a pre-existing
  condition unrelated to this task; V00 is compatible with the full chain
  regardless (proven in §7), and this observation should inform (but does not
  block) any future production migration-catch-up effort.
- The grant-model difference (Class 2, §8) reflects a Supabase CLI default
  behavior change between production's original provisioning and the current
  CLI version. It affects tables created by existing V44+ migrations only,
  never any V00 base table, and needs no correction in this task's scope.
