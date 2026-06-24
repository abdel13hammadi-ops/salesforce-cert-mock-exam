# CertBound Background Workers

This directory contains the Python worker skeleton for the V44 Content Pipeline background job system.

## Architecture

Jobs are enqueued into `public.background_jobs` and processed by workers that communicate exclusively through RPCs. Workers never write to any table directly.

```
Enqueuer → background_jobs (pending)
               ↓  claim_background_job_v1
           (leased)
               ↓  heartbeat_background_job_v1
           (running)
               ↓  complete_background_job_v1  →  (completed)
               ↓  fail_background_job_v1      →  (pending / dead_letter)

Scheduled → recover_expired_background_jobs_v1
                reclaims stalled leased/running jobs
```

## Files

| File | Purpose |
|---|---|
| `background_worker.py` | Worker class, CLI entry point, signal handling |
| `job_handlers.py` | Handler registry; all handlers are stubs returning `NotImplementedHandler` |

## Entry Command

```bash
python -m workers.background_worker \
    --worker-id worker-$(hostname)-1 \
    [--job-types resource_ingestion,other] \
    [--once] \
    [--sleep 5.0] \
    [--lease-seconds 300] \
    [--log-level INFO]
```

| Flag | Default | Description |
|---|---|---|
| `--worker-id` | **required** | Unique identifier; used as lease owner |
| `--job-types` | all | Comma-separated list of job types to accept |
| `--once` | false | Claim one job and exit (useful for testing) |
| `--sleep` | 5.0 s | Poll interval when queue is empty |
| `--lease-seconds` | 300 | Lease duration (30–3600) |
| `--log-level` | INFO | Python logging level |

## Required Environment Variables

| Variable | Description |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Service-role secret key |

These are read directly from the environment, not from Streamlit secrets, so the worker can run outside of Streamlit.

## Handler Registry

Use `build_handler_registry(client)` to get the production registry.  It wires in real handlers for implemented types and leaves stubs for the rest.  `HANDLER_REGISTRY` is the all-stubs dict (used for testing and as a fallback).

| job_type | Phase | Status |
|---|---|---|
| `resource_ingestion` | 8B | **Implemented** — calls `ingest_resource_version_v1` |
| `candidate_promotion` | 8C | **Implemented** — calls `promote_question_candidate_v1` |
| `deterministic_audit` | — | Stub — `NotImplementedHandler` |
| `llm_audit` | — | Stub — `NotImplementedHandler` |
| `hybrid_audit` | — | Stub — `NotImplementedHandler` |
| `question_generation` | — | Stub — `NotImplementedHandler` |
| `embedding_generation` | — | Stub — `NotImplementedHandler` |
| `other` | — | Stub — `NotImplementedHandler` |

To add a real handler, create a factory function `make_<type>_handler(client)` and register it in `build_handler_registry`.  The function must match this signature:

```python
def handle_my_type(
    job_id:       str,
    payload:      dict,
    checkpoint:   dict,
    attempt:      int,
    heartbeat_fn: Callable[[], None],
) -> dict:
    ...
    return {"key": "value"}   # stored in background_jobs.result
```

Call `heartbeat_fn()` periodically for long-running work to extend the lease. Raise any exception to trigger `fail_background_job_v1`. Return a dict to trigger `complete_background_job_v1`.

## SQL Verification

`supabase/tests/v44_background_job_lifecycle_verification.sql` verifies the full RPC contract:

- Enqueue → pending
- Claim → leased
- Heartbeat → running
- Complete → completed (idempotent)
- Fail with retries → pending
- Fail at max_attempts → dead_letter
- Recover expired lease with retries → pending
- Recover expired lease at max_attempts → dead_letter
- Claim on empty queue → 0 rows

Run as `service_role` in Supabase SQL editor or psql. The script wraps all state in `BEGIN … ROLLBACK` and leaves no persistent rows.

## What Is Not Implemented Yet

- Handlers: `deterministic_audit`, `llm_audit`, `hybrid_audit`, `question_generation`, `embedding_generation`, `other`
- Heartbeat background thread (currently a single pre-dispatch call by the worker)
- Monitoring / alerting integration
- Retry backoff strategies beyond `fail_background_job_v1` defaults
