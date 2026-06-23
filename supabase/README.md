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
