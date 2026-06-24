-- =============================================================================
-- V44 Phase 7B: Background Job Enqueue and Claim RPCs
-- Created : 2026-06-24 02:37:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds two service-role-only functions:
--
--   enqueue_background_job_v1 — inserts one pending job into background_jobs
--   claim_background_job_v1   — atomically claims one eligible job using
--                               FOR UPDATE SKIP LOCKED, preventing duplicate
--                               processing across concurrent workers
--
-- Safety guarantees
-- -----------------
--   * Only background_jobs is written to.
--   * No audit, question, candidate, resource, or attempt tables are touched.
--   * No worker logic is implemented here.
--
-- Security
-- --------
--   EXECUTE revoked from PUBLIC, anon, authenticated.
--   service_role is the only granted caller.
-- =============================================================================


-- =============================================================================
-- 1. enqueue_background_job_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.enqueue_background_job_v1(
    p_job_type          text,
    p_payload           jsonb      DEFAULT '{}'::jsonb,
    p_priority          integer    DEFAULT 100,
    p_max_attempts      integer    DEFAULT 3,
    p_available_at      timestamptz DEFAULT now(),
    p_created_by        text       DEFAULT NULL,
    p_model_name        text       DEFAULT NULL,
    p_prompt_version    text       DEFAULT NULL,
    p_estimated_cost_usd numeric   DEFAULT NULL,
    p_metadata          jsonb      DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    job_id      uuid,
    job_status  text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_job_id uuid;
BEGIN
    -- Require non-empty created_by.
    IF COALESCE(TRIM(p_created_by), '') = '' THEN
        RAISE EXCEPTION 'p_created_by must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Validate job_type.
    IF p_job_type NOT IN (
        'resource_ingestion', 'deterministic_audit', 'llm_audit',
        'hybrid_audit', 'question_generation', 'candidate_promotion',
        'embedding_generation', 'other'
    ) THEN
        RAISE EXCEPTION 'invalid job_type: %', p_job_type
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- priority >= 0.
    IF COALESCE(p_priority, -1) < 0 THEN
        RAISE EXCEPTION 'p_priority must be >= 0, got: %', p_priority
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- max_attempts > 0.
    IF COALESCE(p_max_attempts, 0) <= 0 THEN
        RAISE EXCEPTION 'p_max_attempts must be > 0, got: %', p_max_attempts
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- estimated_cost_usd nullable or >= 0.
    IF p_estimated_cost_usd IS NOT NULL AND p_estimated_cost_usd < 0 THEN
        RAISE EXCEPTION 'p_estimated_cost_usd must be >= 0, got: %',
            p_estimated_cost_usd
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Insert the job in pending state.
    v_job_id := gen_random_uuid();

    INSERT INTO public.background_jobs (
        id,
        job_type,
        job_status,
        priority,
        payload,
        max_attempts,
        available_at,
        model_name,
        prompt_version,
        estimated_cost_usd,
        created_by,
        metadata
    ) VALUES (
        v_job_id,
        p_job_type,
        'pending',
        COALESCE(p_priority, 100),
        COALESCE(p_payload, '{}'::jsonb),
        COALESCE(p_max_attempts, 3),
        COALESCE(p_available_at, now()),
        p_model_name,
        p_prompt_version,
        p_estimated_cost_usd,
        p_created_by,
        COALESCE(p_metadata, '{}'::jsonb)
    );

    RETURN QUERY SELECT v_job_id, 'pending'::text;
END;
$$;


-- =============================================================================
-- 2. claim_background_job_v1
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
    UPDATE public.background_jobs
    SET    job_status        = 'leased',
           lease_owner       = p_worker_id,
           lease_expires_at  = now() + (p_lease_seconds || ' seconds')::interval,
           heartbeat_at      = now(),
           started_at        = COALESCE(started_at, now()),
           attempt_count     = attempt_count + 1,
           updated_at        = now()
    WHERE  id = (
        SELECT bj.id
        FROM   public.background_jobs bj
        WHERE  bj.job_status     = 'pending'
          AND  bj.available_at  <= now()
          AND  bj.attempt_count  < bj.max_attempts
          AND  (p_job_types IS NULL OR bj.job_type = ANY(p_job_types))
        ORDER BY bj.priority    ASC,
                 bj.available_at ASC,
                 bj.created_at  ASC
        LIMIT  1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING
        background_jobs.id,
        background_jobs.job_type,
        background_jobs.payload,
        background_jobs.checkpoint,
        background_jobs.attempt_count,
        background_jobs.max_attempts,
        background_jobs.lease_expires_at,
        background_jobs.model_name,
        background_jobs.prompt_version,
        background_jobs.metadata;
END;
$$;


-- =============================================================================
-- Privilege hardening — enqueue_background_job_v1
-- =============================================================================

REVOKE ALL ON FUNCTION public.enqueue_background_job_v1(
    text, jsonb, integer, integer, timestamptz, text, text, text, numeric, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.enqueue_background_job_v1(
    text, jsonb, integer, integer, timestamptz, text, text, text, numeric, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.enqueue_background_job_v1(
    text, jsonb, integer, integer, timestamptz, text, text, text, numeric, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.enqueue_background_job_v1(
    text, jsonb, integer, integer, timestamptz, text, text, text, numeric, jsonb
) TO service_role;

COMMENT ON FUNCTION public.enqueue_background_job_v1(
    text, jsonb, integer, integer, timestamptz, text, text, text, numeric, jsonb
) IS
'Inserts one pending background_job.  Validates job_type, priority, and
max_attempts.  Returns the new job_id and status=pending.
Only writes to background_jobs.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';


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
