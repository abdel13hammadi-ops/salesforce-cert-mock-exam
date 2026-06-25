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
| `job_handlers.py` | Handler registry, factory functions, `build_handler_registry(client, llm_provider)` |
| `deterministic_audit.py` | Phase 8D — pure structural check functions; no Supabase calls |
| `audit_orchestration.py` | Phase 8E — `orchestrate_audit()` lifecycle: create → check → complete/fail |
| `llm_providers.py` | Phase 8F — `LlmProvider` Protocol, `LlmResponse`, `MissingProviderError`, `NoOpProvider` |
| `llm_audit.py` | Phase 8F — strict JSON response schema, `validate_llm_response()`, `LlmAuditValidationError` |
| `finding_merge.py` | Phase 8I — `merge_findings()`: dedup by (code, field_path, description), severity/confidence escalation, evidence union, metadata provenance |
| `anthropic_provider.py` | Phase V45 — production Anthropic Messages API provider with structured JSON output |
| `smoke_anthropic_audit.py` | Phase V45 — manual live smoke test (never run under pytest) |

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
| `deterministic_audit` | 8D/8E | **Implemented** — runs structural checks via `orchestrate_audit()` |
| `llm_audit` | 8G | **Implemented** — calls injected `LlmProvider`, validates response via `validate_llm_response()` |
| `hybrid_audit` | 8H/8I | **Implemented** — det checks + LLM call + `merge_findings()`, orchestrates audit lifecycle |
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

## LLM Provider Injection

`llm_audit` and `hybrid_audit` both require a real provider at runtime. Wire one in:

```python
from workers.llm_providers import LlmResponse

class MyOpenAiProvider:
    def __call__(self, *, model_name, system_prompt, user_prompt,
                 response_schema, metadata=None):
        # ... call OpenAI SDK ...
        return LlmResponse(parsed_response={...}, input_tokens=..., output_tokens=...)

worker = BackgroundWorker(
    worker_id="my-worker",
    client=supabase_client,
    handlers=build_handler_registry(supabase_client, llm_provider=MyOpenAiProvider()),
)
```

Without a provider, the handler raises `MissingProviderError` before any RPC call.

## Anthropic Provider (V45 Phase 1)

Production provider: `AnthropicAuditProvider` in `workers/anthropic_provider.py`.

Wire it into the worker:

```python
from workers.anthropic_provider import build_anthropic_provider_from_env
from workers.job_handlers import build_handler_registry

provider = build_anthropic_provider_from_env()
worker = BackgroundWorker(
    worker_id="my-worker",
    client=supabase_client,
    handlers=build_handler_registry(supabase_client, llm_provider=provider),
)
```

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `CERTBOUND_ANTHROPIC_API_KEY` | **yes** | — | Anthropic API key (never logged) |
| `CERTBOUND_ANTHROPIC_MODEL` | no | `claude-sonnet-4-6` | Default model ID |
| `CERTBOUND_ANTHROPIC_TIMEOUT_SECONDS` | no | `120` | Per-request timeout (seconds) |
| `CERTBOUND_ANTHROPIC_MAX_OUTPUT_TOKENS` | no | `4096` | Max output tokens per request |
| `CERTBOUND_ANTHROPIC_MAX_RETRIES` | no | `3` | Max retries for transient failures |
| `CERTBOUND_ANTHROPIC_INPUT_COST_PER_MTOK` | no | — | USD per 1M input tokens (cost estimate) |
| `CERTBOUND_ANTHROPIC_OUTPUT_COST_PER_MTOK` | no | — | USD per 1M output tokens (cost estimate) |
| `CERTBOUND_ALLOW_LIVE_AI_TEST` | smoke test only | — | Must be `1` for manual smoke test |

### Retry policy

Retries with exponential backoff (max 30s jitter) only for transient Anthropic errors: rate limits, timeouts, connection errors, and 5xx server errors.

Does **not** retry authentication failures, bad requests, malformed JSON, empty responses, or schema-invalid output (`validate_llm_response` failures).

### Manual smoke test

Refuses to call the live API unless explicitly enabled:

```bash
set CERTBOUND_ALLOW_LIVE_AI_TEST=1
set CERTBOUND_ANTHROPIC_API_KEY=your-key-here
python -m workers.smoke_anthropic_audit
```

Optional pricing for cost output:

```bash
set CERTBOUND_ANTHROPIC_INPUT_COST_PER_MTOK=3.0
set CERTBOUND_ANTHROPIC_OUTPUT_COST_PER_MTOK=15.0
```

Prints model, duration, token usage, estimated cost, and validated findings. Not collected or executed by pytest.

## Finding Merge (`finding_merge.py`)

`merge_findings(deterministic, llm)` produces a single deduplicated list from two finding sources.

Deduplication key: `(normalized finding_code, normalized field_path, normalized description)`.

| Rule | Behaviour |
|---|---|
| Severity | Higher-ranked severity wins (`info < low < medium < high < critical`) |
| Confidence | Higher numeric value wins; `None` only when both are `None` |
| Evidence | Combined; deduped by `(resource_chunk_id, evidence_role)` |
| Metadata | LLM metadata is the base; deterministic values overwrite on conflict |
| Detector provenance | LLM `detector_name`/`detector_version` stored under `llm_detector_*` metadata keys |
| Identity | Deterministic `finding_code`, `finding_type`, `title`, `description`, `field_path` win |
| Ordering | Deterministic findings first (stable), unmatched LLM findings appended |
| Non-duplicates | Never silently dropped |

## What Is Not Implemented Yet

- Handlers: `question_generation`, `embedding_generation`, `other`
- OpenAI or other SDK providers — follow the Anthropic pattern in `anthropic_provider.py`
- Heartbeat background thread (currently a single pre-dispatch call by the worker)
- Monitoring / alerting integration
- Retry backoff strategies beyond `fail_background_job_v1` defaults
