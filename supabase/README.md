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
| `20260624023300_v44_background_jobs_foundation.sql` | Ready — not yet applied | Additive. One table. No RPCs. No exam delivery impact. |
| `20260624023700_v44_background_job_enqueue_claim_rpcs.sql` | Ready — not yet applied | Requires Phase 7A table. Two RPCs. service_role only. |
| `20260624024200_v44_background_job_lifecycle_rpcs.sql` | Ready — not yet applied | Requires Phase 7A table. Four RPCs: heartbeat, complete, fail, recover. service_role only. |
| `20260624120000_v45_audit_finding_materiality.sql` | Ready — not yet applied | Adds `audit_findings.materiality` column, CHECK, backfill, composite index, updates `complete_audit_run_v1`. Requires Phase 6A tables. |
| `supabase/tests/v44_background_job_lifecycle_verification.sql` | Phase 7D — verification script | Run as service_role. Wraps all state in BEGIN…ROLLBACK. Covers 10 lifecycle assertions. No pgTAP. |
| `supabase/tests/v45_audit_finding_materiality_verification.sql` | V45 Phase 3 — verification script | Run as service_role after materiality migration. Asserts column, CHECK, index, RPC source, backfill. |
| `workers/` (Phase 8A) | Python worker skeleton | `background_worker.py` + `job_handlers.py`. All handlers were stubs. No real job execution yet. |
| `workers/job_handlers.py` (Phase 8B) | resource_ingestion handler | `make_resource_ingestion_handler(client)` calls `ingest_resource_version_v1`. Payload validated before RPC. |
| `workers/job_handlers.py` (Phase 8C) | candidate_promotion handler | `make_candidate_promotion_handler(client)` calls `promote_question_candidate_v1`. Payload validated before RPC. |
| `workers/deterministic_audit.py` (Phase 8D) | Deterministic audit engine | 11 pure-function checks; 11 finding codes (e.g. `EMPTY_QUESTION_TEXT`, `CORRECT_COUNT_MISMATCH`). No RPCs. |
| `workers/audit_orchestration.py` (Phase 8E) | Audit orchestration layer | `orchestrate_audit()` manages create→check→complete lifecycle; calls `end_audit_run_v1` on failure. |
| `workers/llm_providers.py` (Phase 8F) | LLM provider abstraction | `LlmProvider` Protocol, `LlmResponse` dataclass, `MissingProviderError`, `NoOpProvider` sentinel. |
| `workers/llm_audit.py` (Phase 8F) | Strict LLM response schema | `AUDIT_RESPONSE_SCHEMA`, `validate_llm_response()`, `LlmAuditValidationError`. 13 allowed finding types, 5 severities, 3 evidence roles. |
| `workers/job_handlers.py` (Phase 8G) | `llm_audit` handler | `make_llm_audit_handler(client, llm_provider)` calls injected provider, validates response, orchestrates audit RPCs. Raises `MissingProviderError` before any RPC if no provider. |
| `workers/finding_merge.py` (Phase 8I) | Finding merge logic | `merge_findings(det, llm)` deduplicates by (code, field_path, description); severity/confidence escalation; evidence union by (chunk_id, role); deterministic identity wins; metadata provenance preserved. |
| `workers/job_handlers.py` (Phase 8H) | `hybrid_audit` handler | `make_hybrid_audit_handler(client, llm_provider)` runs det checks → LLM call → validates response → `merge_findings()` → `complete_audit_run_v1`. Returns audit counts + token/cost. Raises `MissingProviderError` before any RPC if no provider. |
| `workers/anthropic_provider.py` (V45 Phase 1) | Anthropic audit provider | `AnthropicAuditProvider` uses official Anthropic SDK + Messages API structured JSON output. Env: `CERTBOUND_ANTHROPIC_*`. Manual smoke: `CERTBOUND_ALLOW_LIVE_AI_TEST=1 python -m workers.smoke_anthropic_audit`. |
| `workers/llm_provider_factory.py` (V45 Phase 2) | Worker LLM wiring | `build_llm_provider_from_env()` reads `CERTBOUND_LLM_PROVIDER=anthropic`, injects provider into `build_handler_registry`. |
| `workers/run_audit_calibration.py` (V45 Phase 2) | Calibration pilot | Dry-run five-case calibration via `python -m workers.run_audit_calibration`. Requires `CERTBOUND_ALLOW_LIVE_AI_TEST=1`. Fixture: `workers/fixtures/audit_calibration_cases.json`. |

## V45 Phase 3 — audit finding materiality

Migration `20260624120000_v45_audit_finding_materiality.sql`:

- Adds `public.audit_findings.materiality text NOT NULL DEFAULT 'warning'`
- CHECK: `blocking`, `warning`, `informational` only
- Backfill: default `warning`; explicit structural/correctness `finding_code` values → `blocking`
- Index: `idx_af_run_materiality_status (audit_run_id, materiality, finding_status)`
- Updates `complete_audit_run_v1` to read top-level `materiality`, default missing to `warning`, reject invalid values, persist to column

Verification (after apply):

```bash
psql "$DATABASE_URL" -f supabase/tests/v45_audit_finding_materiality_verification.sql
```

### Compatibility note

- New worker rows persist canonical `EXPLANATION_MISSING` (not legacy `MISSING_EXPLANATION`).
- Backfill maps both codes to `blocking` when present historically.
- No active in-repo Streamlit/admin consumer queries `audit_findings` by legacy code today.
