-- =============================================================================
-- V45 Phase 4B: Fix claim_background_job_v1 PL/pgSQL name ambiguity
-- Created : 2026-06-24 13:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Corrects SQLSTATE 42702 in claim_background_job_v1 where unqualified
-- attempt_count in the UPDATE SET clause collided with the RETURNS TABLE
-- output variable of the same name.
--
-- Safety guarantees
-- -----------------
--   * Function signature, validation, locking, and lease behavior unchanged.
--   * Only claim_background_job_v1 is replaced; no table changes.
--
-- Security
-- --------
--   Privilege hardening re-applied: service_role only.
-- =============================================================================


CREATE OR REPLACE FUNCTION public.claim_background_job_v1(
    p_worker_id     text,
    p_lease_seconds integer DEFAULT 300,
    p_job_types     text[]  DEFAULT NULL
)
RETURNS TABLE (
    job_id           uuid,
    job_type         text,
    payload          jsonb,
    checkpoint       jsonb,
    attempt_count    integer,
    max_attempts     integer,
    lease_expires_at timestamptz,
    model_name       text,
    prompt_version   text,
    metadata         jsonb
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
BEGIN
    -- Require non-empty worker ID.
    IF COALESCE(TRIM(p_worker_id), '') = '' THEN
        RAISE EXCEPTION 'p_worker_id must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- lease_seconds must be between 30 and 3600.
    IF COALESCE(p_lease_seconds, -1) < 30 OR COALESCE(p_lease_seconds, -1) > 3600 THEN
        RAISE EXCEPTION 'p_lease_seconds must be between 30 and 3600, got: %',
            p_lease_seconds
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Validate job_types array when supplied.
    IF p_job_types IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM   unnest(p_job_types) AS t(jt)
            WHERE  jt NOT IN (
                'resource_ingestion', 'deterministic_audit', 'llm_audit',
                'hybrid_audit', 'question_generation', 'candidate_promotion',
                'embedding_generation', 'other'
            )
        ) THEN
            RAISE EXCEPTION
                'p_job_types contains one or more invalid job_type values'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END IF;

    -- -------------------------------------------------------------------------
    -- Atomically select and claim one eligible job.
    --
    -- Concurrency strategy: FOR UPDATE SKIP LOCKED in the subquery lets each
    -- concurrent worker skip rows already locked by another worker, so N
    -- workers calling simultaneously each claim a distinct job without
    -- contention or deadlock.
    --
    -- Eligibility criteria:
    --   * job_status = 'pending'
    --   * available_at <= now()        (scheduled time has passed)
    --   * attempt_count < max_attempts (retries not exhausted)
    --   * job_type in p_job_types      (when worker is type-restricted)
    --
    -- Claim ordering (stable FIFO within a priority band):
    --   1. priority ASC  (lower value = higher urgency)
    --   2. available_at ASC
    --   3. created_at ASC
    -- -------------------------------------------------------------------------
    RETURN QUERY
    UPDATE public.background_jobs AS bj
    SET    job_status        = 'leased',
           lease_owner       = p_worker_id,
           lease_expires_at  = now() + (p_lease_seconds || ' seconds')::interval,
           heartbeat_at      = now(),
           started_at        = COALESCE(bj.started_at, now()),
           attempt_count     = bj.attempt_count + 1,
           updated_at        = now()
    WHERE  bj.id = (
        SELECT eligible.id
        FROM   public.background_jobs eligible
        WHERE  eligible.job_status     = 'pending'
          AND  eligible.available_at  <= now()
          AND  eligible.attempt_count  < eligible.max_attempts
          AND  (p_job_types IS NULL OR eligible.job_type = ANY(p_job_types))
        ORDER BY eligible.priority    ASC,
                 eligible.available_at ASC,
                 eligible.created_at  ASC
        LIMIT  1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING
        bj.id,
        bj.job_type,
        bj.payload,
        bj.checkpoint,
        bj.attempt_count,
        bj.max_attempts,
        bj.lease_expires_at,
        bj.model_name,
        bj.prompt_version,
        bj.metadata;
END;
$$;


-- =============================================================================
-- Privilege hardening — claim_background_job_v1
-- =============================================================================

REVOKE ALL ON FUNCTION public.claim_background_job_v1(
    text, integer, text[]
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.claim_background_job_v1(
    text, integer, text[]
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.claim_background_job_v1(
    text, integer, text[]
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.claim_background_job_v1(
    text, integer, text[]
) TO service_role;

COMMENT ON FUNCTION public.claim_background_job_v1(text, integer, text[]) IS
'Atomically claims one eligible pending background_job using FOR UPDATE SKIP
LOCKED.  Concurrent workers each claim a distinct job without contention.
Transitions the job to leased, sets the lease expiry, and increments
attempt_count.  Returns no row when nothing is eligible.
Only writes to background_jobs.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';
