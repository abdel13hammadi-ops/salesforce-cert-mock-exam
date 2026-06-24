-- =============================================================================
-- V44 Phase 7A: Background Jobs Foundation
-- Created : 2026-06-24 02:33:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds public.background_jobs, a durable queue for all Content Pipeline
-- background work units.
--
-- Design rules
-- ------------
--   * Jobs are claimed by service-role workers using short-term leases.
--     Lease expiry allows other workers to reclaim stalled jobs.
--   * Leases prevent duplicate processing: a worker must hold a valid,
--     non-expired lease before executing or updating a job.
--   * checkpoint supports resumable execution: workers persist intermediate
--     state here so a reclaimed job can continue from the last checkpoint
--     rather than restarting from scratch.
--   * payload and result are immutable snapshots of the input and output
--     at claim and completion time; they do not reference mutable source
--     records by design.
--   * Service-role / admin access only for this phase.  No anon or
--     authenticated policies.
--
-- No RPCs are added in this phase.  No worker logic is built here.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.background_jobs (

    -- -------------------------------------------------------------------------
    -- Identity
    -- -------------------------------------------------------------------------
    id                  uuid         PRIMARY KEY DEFAULT gen_random_uuid(),

    -- -------------------------------------------------------------------------
    -- Classification
    -- -------------------------------------------------------------------------
    job_type            text         NOT NULL,
    job_status          text         NOT NULL DEFAULT 'pending',

    -- Lower priority value = higher urgency (e.g. 0 is highest).
    priority            integer      NOT NULL DEFAULT 100,

    -- -------------------------------------------------------------------------
    -- Work data
    -- -------------------------------------------------------------------------
    payload             jsonb        NOT NULL DEFAULT '{}'::jsonb,
    checkpoint          jsonb        NOT NULL DEFAULT '{}'::jsonb,
    result              jsonb        NOT NULL DEFAULT '{}'::jsonb,
    error_message       text,

    -- -------------------------------------------------------------------------
    -- Retry control
    -- -------------------------------------------------------------------------
    attempt_count       integer      NOT NULL DEFAULT 0,
    max_attempts        integer      NOT NULL DEFAULT 3,

    -- -------------------------------------------------------------------------
    -- Queue scheduling
    -- -------------------------------------------------------------------------

    -- Workers must not claim this job before available_at.
    available_at        timestamptz  NOT NULL DEFAULT now(),

    -- -------------------------------------------------------------------------
    -- Lease management
    -- -------------------------------------------------------------------------
    lease_owner         text,
    lease_expires_at    timestamptz,
    heartbeat_at        timestamptz,

    -- -------------------------------------------------------------------------
    -- Timing
    -- -------------------------------------------------------------------------
    started_at          timestamptz,
    completed_at        timestamptz,

    -- -------------------------------------------------------------------------
    -- Model / cost tracking
    -- -------------------------------------------------------------------------
    model_name          text,
    prompt_version      text,
    estimated_cost_usd  numeric,
    actual_cost_usd     numeric,
    input_tokens        integer,
    output_tokens       integer,

    -- -------------------------------------------------------------------------
    -- Provenance
    -- -------------------------------------------------------------------------
    created_by          text         NOT NULL,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    metadata            jsonb        NOT NULL DEFAULT '{}'::jsonb,


    -- =========================================================================
    -- Constraints
    -- =========================================================================

    -- job_type valid values.
    CONSTRAINT background_jobs_type_valid
        CHECK (
            job_type IN (
                'resource_ingestion',
                'deterministic_audit',
                'llm_audit',
                'hybrid_audit',
                'question_generation',
                'candidate_promotion',
                'embedding_generation',
                'other'
            )
        ),

    -- job_status valid values.
    CONSTRAINT background_jobs_status_valid
        CHECK (
            job_status IN (
                'pending',
                'leased',
                'running',
                'completed',
                'failed',
                'cancelled',
                'dead_letter'
            )
        ),

    -- priority >= 0.
    CONSTRAINT background_jobs_priority_nonnegative
        CHECK (priority >= 0),

    -- attempt_count >= 0.
    CONSTRAINT background_jobs_attempt_count_nonnegative
        CHECK (attempt_count >= 0),

    -- max_attempts > 0.
    CONSTRAINT background_jobs_max_attempts_positive
        CHECK (max_attempts > 0),

    -- attempt_count must not exceed max_attempts.
    CONSTRAINT background_jobs_attempt_count_lte_max
        CHECK (attempt_count <= max_attempts),

    -- input_tokens nullable or >= 0.
    CONSTRAINT background_jobs_input_tokens_nonnegative
        CHECK (input_tokens IS NULL OR input_tokens >= 0),

    -- output_tokens nullable or >= 0.
    CONSTRAINT background_jobs_output_tokens_nonnegative
        CHECK (output_tokens IS NULL OR output_tokens >= 0),

    -- estimated_cost_usd nullable or >= 0.
    CONSTRAINT background_jobs_estimated_cost_nonnegative
        CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),

    -- actual_cost_usd nullable or >= 0.
    CONSTRAINT background_jobs_actual_cost_nonnegative
        CHECK (actual_cost_usd IS NULL OR actual_cost_usd >= 0),

    -- leased and running statuses require lease_owner and lease_expires_at.
    CONSTRAINT background_jobs_leased_requires_lease
        CHECK (
            job_status NOT IN ('leased', 'running')
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        ),

    -- Terminal statuses require completed_at.
    CONSTRAINT background_jobs_terminal_requires_completed_at
        CHECK (
            job_status NOT IN ('completed', 'failed', 'cancelled', 'dead_letter')
            OR completed_at IS NOT NULL
        ),

    -- Non-terminal statuses must not have completed_at.
    CONSTRAINT background_jobs_nonterminal_no_completed_at
        CHECK (
            job_status IN ('completed', 'failed', 'cancelled', 'dead_letter')
            OR completed_at IS NULL
        ),

    -- completed_at must not precede started_at when both are present.
    CONSTRAINT background_jobs_completed_after_started
        CHECK (
            started_at   IS NULL
            OR completed_at IS NULL
            OR completed_at >= started_at
        ),

    -- lease_expires_at requires lease_owner.
    CONSTRAINT background_jobs_lease_expires_requires_owner
        CHECK (
            lease_expires_at IS NULL
            OR lease_owner IS NOT NULL
        ),

    -- heartbeat_at requires lease_owner.
    CONSTRAINT background_jobs_heartbeat_requires_owner
        CHECK (
            heartbeat_at IS NULL
            OR lease_owner IS NOT NULL
        )
);

-- =============================================================================
-- Indexes
-- =============================================================================

-- Primary queue-processing index: workers filter by status and available_at,
-- then order by priority then created_at for stable FIFO within a priority band.
CREATE INDEX IF NOT EXISTS idx_bj_queue
    ON public.background_jobs (job_status, available_at, priority, created_at);

-- Lease-expiry scanner: finds jobs whose leases have expired so they can be
-- reclaimed by another worker.
CREATE INDEX IF NOT EXISTS idx_bj_lease_expires
    ON public.background_jobs (lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_bj_job_type
    ON public.background_jobs (job_type);

CREATE INDEX IF NOT EXISTS idx_bj_created_at
    ON public.background_jobs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bj_created_by
    ON public.background_jobs (created_by);

-- =============================================================================
-- Row-Level Security
--
-- Service-role workers bypass RLS.  No anon or authenticated policies are
-- added in this phase.  Worker-specific policies will be added alongside
-- the Phase 7B claim/complete RPCs.
-- =============================================================================

ALTER TABLE public.background_jobs ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.background_jobs IS
'Durable queue for all Content Pipeline background work units.
Jobs are claimed by service-role workers using short-term leases.
Leases prevent duplicate processing; lease expiry allows stalled jobs to
be reclaimed.
checkpoint supports resumable execution across worker restarts.
payload and result are immutable snapshots, not references to mutable
source records.
Service-role / admin access only for this phase.';
