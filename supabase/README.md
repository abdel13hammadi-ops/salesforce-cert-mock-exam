# Supabase Migrations

## Convention

- **Additive only.** Every migration adds tables, columns, indexes, or constraints. Existing tables are never dropped or structurally altered without a separate destructive-change review.
- **Filenames.** `<UTC_YYYYMMDDHHMMSS>_<description>.sql`. The UTC timestamp prefix ensures execution order is deterministic across environments.
- **Immutable once executed.** A migration that has been applied to any environment must never be edited. Fix mistakes by writing a new migration.
- **Rollback via forward migration.** There are no `down` scripts. Reversals are new additive migrations (e.g. `DROP TABLE`, column removal) that require separate destructive-change review before execution.
- **Destructive changes require review.** Any migration that drops, truncates, renames, or alters the type of a column in a table already used by the application must be reviewed and approved separately before it is applied to production.

## Verified schema facts

| Table | Column | Confirmed type | Verified |
|---|---|---|---|
| `public.questions` | `id` | `integer` (int4) | 2026-06-23 |

## Status

| File | Status | Notes |
|---|---|---|
| `20260623000000_v44_question_version_foundation.sql` | Ready — not yet applied | FK type verified as `integer` (int4). Must be applied before Phase 2. |
| `20260623182200_v44_backfill_question_versions.sql` | Ready — not yet applied | Requires Phase 1 tables to exist. Idempotent. Safe to re-run. |
| `20260623191900_v44_create_question_version_rpc.sql` | Ready — not yet applied | Requires Phase 1 tables. Service-role / admin only. No publishing. |
| `20260623192800_v44_approve_publish_question_version_rpc.sql` | Ready — not yet applied | Requires Phase 1 tables. Approval + publish RPCs. service_role only. |
| `20260623193600_v44_question_candidates.sql` | Ready — not yet applied | Additive. Staging table only; no exam delivery impact. |
| `20260623233200_v44_promote_question_candidate_rpc.sql` | Ready — not yet applied | Requires Phase 4A and Phase 3A tables/functions. service_role only. |
| `20260623233800_v44_resource_library_foundation.sql` | Ready — not yet applied | Additive. Three tables. No embeddings, no exam delivery impact. |
| `20260623234600_v44_ingest_resource_version_rpc.sql` | Ready — not yet applied | Requires Phase 5A tables. Idempotent. service_role only. |
| `20260623235100_v44_audit_foundation.sql` | Ready — not yet applied | Additive. Three tables. No RPCs. No exam delivery impact. |
| `20260624015900_v44_audit_lifecycle_rpcs.sql` | Ready — not yet applied | Requires Phase 6A tables. Two RPCs. service_role only. |
| `20260624022600_v44_fail_cancel_audit_run_rpc.sql` | Ready — not yet applied | Requires Phase 6A tables. One RPC. service_role only. |
