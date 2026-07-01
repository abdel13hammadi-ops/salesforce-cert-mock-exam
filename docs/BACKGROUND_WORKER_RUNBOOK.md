# CertBound Background Worker Runbook

Operational guide for processing `background_jobs` outside Streamlit.

## Supported job types

| job_type | LLM required | Notes |
|---|---|---|
| `resource_ingestion` | No | Ingests resource versions via RPC |
| `candidate_promotion` | No | Promotes question candidates |
| `deterministic_audit` | No | Structural audit checks only |
| `certification_duplicate_audit` | No | Certification-wide duplicate stem scan |
| `llm_audit` | Yes | Anthropic provider when configured |
| `hybrid_audit` | Yes | Deterministic + LLM merge |
| `ai_quality_audit_smoke` | Yes | V48 Pass A/B/C orchestration via injected providers |
| `question_generation` | — | Stub (not implemented) |
| `embedding_generation` | — | Stub (not implemented) |
| `other` | — | Stub (not implemented) |

Workers communicate only through Supabase RPCs. They never write tables directly.

## Required environment variables

| Variable | Required for |
|---|---|
| `SUPABASE_URL` | All worker runs |
| `SUPABASE_SERVICE_ROLE_KEY` | All worker runs |
| `CERTBOUND_LLM_PROVIDER` | `llm_audit`, `hybrid_audit` only |
| `CERTBOUND_ANTHROPIC_API_KEY` | Anthropic LLM/hybrid jobs |
| `CERTBOUND_ALLOW_LIVE_AI_TEST` | Manual LLM/hybrid enqueue scripts only |
| `CERTBOUND_AI_QUALITY_PRIMARY_LLM_PROVIDER` | `ai_quality_audit_smoke` when included in `--job-types` (falls back to `CERTBOUND_LLM_PROVIDER`) |
| `CERTBOUND_AI_QUALITY_DISPUTE_LLM_PROVIDER` | Optional separate Pass C provider (defaults to primary) |
| `CERTBOUND_AI_QUALITY_TIMEOUT_SECONDS` | Worker-level Pass A/B/C timeout (1–3600 seconds) |

Optional tuning:

| Variable | Default | Purpose |
|---|---|---|
| `CERTBOUND_JOB_RECOVERY_INTERVAL_SECONDS` | 60 | Expired lease recovery interval |
| `CERTBOUND_JOB_RECOVERY_LIMIT` | 100 | Max jobs reclaimed per recovery call |
| `CERTBOUND_JOB_RECOVERY_RETRY_DELAY_SECONDS` | 60 | Retry delay passed to recovery RPC |

## Secure temporary PowerShell setup

Load secrets for the current session only. Do not echo values. Do not commit them.

```powershell
# From repo root — replace placeholders locally; never paste real keys into chat or docs.
$env:SUPABASE_URL = "<your-project-url>"
$env:SUPABASE_SERVICE_ROLE_KEY = Read-Host "Paste service role key (hidden)" -AsSecureString
# Convert SecureString only in-memory for the session if your tooling requires plain text:
# $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($env:SUPABASE_SERVICE_ROLE_KEY)
# $env:SUPABASE_SERVICE_ROLE_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)

# LLM/hybrid jobs only:
# $env:CERTBOUND_LLM_PROVIDER = "anthropic"
# $env:CERTBOUND_ANTHROPIC_API_KEY = Read-Host "Paste Anthropic key (hidden)" -AsSecureString
```

Deterministic audits (`deterministic_audit`, `certification_duplicate_audit`) do **not** need LLM variables.

## Production worker (separate from Streamlit)

The background worker runs as its own long-lived process. It does **not** start from Streamlit and does not read Streamlit secrets unless you export the same variables into the worker shell/service environment.

Continuous production command for AI quality audits:

```powershell
python -m workers.background_worker `
    --worker-id "certbound-ai-quality-1" `
    --job-types ai_quality_audit_smoke `
    --log-level INFO
```

Required when `--job-types` includes `ai_quality_audit_smoke`, or when `--job-types` is omitted (worker accepts all registered job types):

| Variable | Required | Notes |
|---|---|---|
| `SUPABASE_URL` | yes | Same project as the app |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | Service role only; never log |
| `CERTBOUND_AI_QUALITY_PRIMARY_LLM_PROVIDER` or `CERTBOUND_LLM_PROVIDER` | yes | Set to `anthropic` |
| `CERTBOUND_ANTHROPIC_API_KEY` | yes when provider is anthropic | Never log |
| `CERTBOUND_AI_QUALITY_DISPUTE_LLM_PROVIDER` | no | Omit to reuse the primary provider for Pass C |
| `CERTBOUND_AI_QUALITY_TIMEOUT_SECONDS` | no | 1–3600; defaults via `CERTBOUND_ANTHROPIC_TIMEOUT_SECONDS`, then 120 |

Fail-fast startup rules:

- When `ai_quality_audit_smoke` is included (explicitly or via default all-types mode), the worker exits before claiming jobs if provider configuration is missing or invalid.
- When `--job-types` excludes `ai_quality_audit_smoke`, AI quality provider variables are not required and non-AI workers start normally.

Deterministic-only worker example (no AI configuration required):

```powershell
python -m workers.background_worker `
    --worker-id "certbound-deterministic-1" `
    --job-types deterministic_audit,certification_duplicate_audit `
    --log-level INFO
```

## Process one job (`--once`)

```powershell
python -m workers.background_worker `
    --worker-id "manual-verify-1" `
    --job-types deterministic_audit `
    --once `
    --log-level INFO
```

Replace `--job-types` with the enqueued job type. Omit `--job-types` to accept any supported type.

## Verify job status

After enqueue, confirm processing in Supabase SQL editor (service role):

```sql
SELECT id, job_type, job_status, attempt_count, max_attempts,
       error_message, result, started_at, completed_at
FROM public.background_jobs
WHERE id = '<job-id-from-enqueue>'
LIMIT 1;
```

For audit jobs, also verify:

```sql
SELECT ar.id, ar.run_status, ar.target_question_version_id,
       COUNT(af.id) AS finding_count
FROM public.audit_runs ar
LEFT JOIN public.audit_findings af ON af.audit_run_id = ar.id
WHERE ar.id = (SELECT (result->>'audit_run_id')::uuid
               FROM public.background_jobs
               WHERE id = '<job-id-from-enqueue>')
GROUP BY ar.id, ar.run_status, ar.target_question_version_id;
```

Expected terminal states: `completed` or `dead_letter`.

## Retry and recovery behavior

- `fail_background_job_v1` requeues with delay until `max_attempts`, then `dead_letter`.
- `recover_expired_background_jobs_v1` reclaims stalled `leased`/`running` jobs whose lease expired.
- Worker calls recovery on startup and periodically (see env vars above).
- Idempotent handlers (e.g. audit completion with duplicate-pair guards) safe on retry.

## HTTP/2 transient failures

If Supabase/PostgREST calls fail with transient network or HTTP/2 errors:

1. Confirm `SUPABASE_URL` and service role key are loaded in the **current** shell session.
2. Retry `--once` after a short pause; failed jobs return to `pending` when retries remain.
3. Check `error_message` on the job row before re-enqueueing.
4. Avoid running multiple workers with overlapping `--job-types` on the same queue during manual verification.

## Clear temporary environment variables

End of session cleanup (PowerShell):

```powershell
Remove-Item Env:SUPABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:SUPABASE_SERVICE_ROLE_KEY -ErrorAction SilentlyContinue
Remove-Item Env:CERTBOUND_LLM_PROVIDER -ErrorAction SilentlyContinue
Remove-Item Env:CERTBOUND_ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:CERTBOUND_ALLOW_LIVE_AI_TEST -ErrorAction SilentlyContinue
```

Close the terminal to discard any in-memory secrets.

## Security rules

- Never commit or paste service-role credentials, Anthropic keys, or production URLs into the repository.
- Never log secret values in worker stdout.
- Use service role only on trusted operator machines or CI secrets stores.
- Streamlit app secrets and worker shell env vars are separate; loading one does not load the other.

## Enqueue reference (operator)

Jobs are created via `enqueue_background_job_v1` (service role). Example job types:

- `deterministic_audit` — payload requires `target_question_version_id`, `created_by`, `question` snapshot
- `hybrid_audit` — use `workers/run_hybrid_audit_pilot.py` enqueue helper pattern
- `certification_duplicate_audit` — use `workers/run_certification_duplicate_audit.py` enqueue helper pattern
- `resource_ingestion` — use `workers/run_resource_ingestion.py` (dry-run by default)

Enqueue only; run the worker separately with `--once` or continuous mode.

### Official resource ingestion (`resource_ingestion`)

Requires an existing `official_resources` catalog row and a local UTF-8 text/Markdown file (already extracted). The CLI chunks text, computes SHA-256 hashes, validates the payload, and enqueues a job. It does not fetch URLs, parse PDFs, or run the worker.

Dry-run (read-only report; no enqueue):

```powershell
python -m workers.run_resource_ingestion `
    --resource-id "<official-resources-uuid>" `
    --input-file "path\to\exam-guide.txt" `
    --created-by "you@example.com"
```

Enqueue after review:

```powershell
$env:CERTBOUND_ALLOW_JOB_ENQUEUE = "1"
python -m workers.run_resource_ingestion `
    --resource-id "<official-resources-uuid>" `
    --input-file "path\to\exam-guide.txt" `
    --created-by "you@example.com" `
    --source-url "https://help.salesforce.com/..." `
    --source-external-version "Winter '26" `
    --enqueue
```

Process one queued job:

```powershell
python -m workers.background_worker `
    --worker-id "manual-ingest-1" `
    --job-types resource_ingestion `
    --once `
    --log-level INFO
```

Verify: confirm `background_jobs.job_status = completed` and inspect `result.resource_version_id`, then query `resource_versions` and `resource_chunks` for that version.
