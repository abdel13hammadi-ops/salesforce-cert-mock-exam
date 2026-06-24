-- =============================================================================
-- V44 Phase 7C: Background Job Lifecycle RPCs
-- Created : 2026-06-24 02:42:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds four service-role-only functions that manage the active lifecycle of
-- background jobs after they have been claimed:
--
--   heartbeat_background_job_v1        — renews a lease and records liveness
--   complete_background_job_v1         — marks a job as successfully done
--   fail_background_job_v1             — marks a job as failed and schedules
--                                        a retry or dead-letters it
--   recover_expired_background_jobs_v1 — reclaims stalled jobs whose leases
--                                        have expired
--
-- Safety guarantees
-- -----------------
--   * Only background_jobs is updated.  No other table is touched.
--   * No rows are deleted.
--   * complete_background_job_v1 is idempotent for the completed terminal state.
--
-- Security
-- --------
--   EXECUTE revoked from PUBLIC, anon, authenticated.
--   service_role is the only granted caller.
-- =============================================================================


-- =============================================================================
-- 1. heartbeat_background_job_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.heartbeat_background_job_v1(
    p_job_id         uuid,
    p_worker_id      text,
    p_lease_seconds  integer DEFAULT 300,
    p_checkpoint     jsonb   DEFAULT NULL
)
RETURNS TABLE (
    job_id           uuid,
    job_status       text,
    lease_expires_at timestamptz,
    heartbeat_at     timestamptz
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_status          text;
    v_owner           text;
    v_lease_expires   timestamptz;
    v_new_expires     timestamptz;
    v_new_heartbeat   timestamptz;
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

    -- Lock the job row.
    SELECT bj.job_status, bj.lease_owner, bj.lease_expires_at
    INTO   v_status, v_owner, v_lease_expires
    FROM   public.background_jobs bj
    WHERE  bj.id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'background_job not found: %', p_job_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Job must be leased or running.
    IF v_status NOT IN ('leased', 'running') THEN
        RAISE EXCEPTION
            'job % has status %; heartbeat requires leased or running',
            p_job_id, v_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Lease owner must match.
    IF v_owner IS DISTINCT FROM p_worker_id THEN
        RAISE EXCEPTION
            'job % is owned by %, not %',
            p_job_id, v_owner, p_worker_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Lease must not already be expired.
    IF v_lease_expires IS NOT NULL AND v_lease_expires < now() THEN
        RAISE EXCEPTION 'lease for job % expired at %', p_job_id, v_lease_expires
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    v_new_expires   := now() + (p_lease_seconds || ' seconds')::interval;
    v_new_heartbeat := now();

    UPDATE public.background_jobs
    SET    job_status        = 'running',
           lease_expires_at  = v_new_expires,
           heartbeat_at      = v_new_heartbeat,
           checkpoint        = CASE
                                   WHEN p_checkpoint IS NOT NULL
                                   THEN checkpoint || p_checkpoint
                                   ELSE checkpoint
                               END,
           updated_at        = now()
    WHERE  id = p_job_id;

    RETURN QUERY SELECT p_job_id, 'running'::text, v_new_expires, v_new_heartbeat;
END;
$$;


-- =============================================================================
-- 2. complete_background_job_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.complete_background_job_v1(
    p_job_id          uuid,
    p_worker_id       text,
    p_result          jsonb    DEFAULT '{}'::jsonb,
    p_checkpoint      jsonb    DEFAULT NULL,
    p_actual_cost_usd numeric  DEFAULT NULL,
    p_input_tokens    integer  DEFAULT NULL,
    p_output_tokens   integer  DEFAULT NULL,
    p_metadata        jsonb    DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    job_id        uuid,
    job_status    text,
    completed_at  timestamptz
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_status        text;
    v_owner         text;
    v_lease_expires timestamptz;
    v_completed_at  timestamptz;
BEGIN
    -- Require non-empty worker ID.
    IF COALESCE(TRIM(p_worker_id), '') = '' THEN
        RAISE EXCEPTION 'p_worker_id must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Validate optional costs and tokens when provided.
    IF p_actual_cost_usd IS NOT NULL AND p_actual_cost_usd < 0 THEN
        RAISE EXCEPTION 'p_actual_cost_usd must be >= 0, got: %', p_actual_cost_usd
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_input_tokens IS NOT NULL AND p_input_tokens < 0 THEN
        RAISE EXCEPTION 'p_input_tokens must be >= 0, got: %', p_input_tokens
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_output_tokens IS NOT NULL AND p_output_tokens < 0 THEN
        RAISE EXCEPTION 'p_output_tokens must be >= 0, got: %', p_output_tokens
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Lock the job row.
    SELECT bj.job_status, bj.lease_owner, bj.lease_expires_at, bj.completed_at
    INTO   v_status, v_owner, v_lease_expires, v_completed_at
    FROM   public.background_jobs bj
    WHERE  bj.id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'background_job not found: %', p_job_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Idempotency: if already completed, return current state with no write.
    IF v_status = 'completed' THEN
        RETURN QUERY SELECT p_job_id, 'completed'::text, v_completed_at;
        RETURN;
    END IF;

    -- Job must be leased or running.
    IF v_status NOT IN ('leased', 'running') THEN
        RAISE EXCEPTION
            'job % has status %; completion requires leased or running',
            p_job_id, v_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Lease owner must match.
    IF v_owner IS DISTINCT FROM p_worker_id THEN
        RAISE EXCEPTION 'job % is owned by %, not %', p_job_id, v_owner, p_worker_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Lease must not be expired.
    IF v_lease_expires IS NOT NULL AND v_lease_expires < now() THEN
        RAISE EXCEPTION 'lease for job % expired at %', p_job_id, v_lease_expires
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    UPDATE public.background_jobs
    SET    job_status        = 'completed',
           result            = COALESCE(p_result, '{}'::jsonb),
           checkpoint        = CASE
                                   WHEN p_checkpoint IS NOT NULL
                                   THEN checkpoint || p_checkpoint
                                   ELSE checkpoint
                               END,
           actual_cost_usd  = COALESCE(p_actual_cost_usd, actual_cost_usd),
           input_tokens      = COALESCE(p_input_tokens, input_tokens),
           output_tokens     = COALESCE(p_output_tokens, output_tokens),
           metadata          = metadata || COALESCE(p_metadata, '{}'::jsonb),
           completed_at      = now(),
           lease_owner       = NULL,
           lease_expires_at  = NULL,
           heartbeat_at      = NULL,
           updated_at        = now()
    WHERE  id = p_job_id
    RETURNING background_jobs.completed_at INTO v_completed_at;

    RETURN QUERY SELECT p_job_id, 'completed'::text, v_completed_at;
END;
$$;


-- =============================================================================
-- 3. fail_background_job_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.fail_background_job_v1(
    p_job_id               uuid,
    p_worker_id            text,
    p_error_message        text,
    p_retry_delay_seconds  integer DEFAULT 60,
    p_checkpoint           jsonb   DEFAULT NULL,
    p_metadata             jsonb   DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    job_id        uuid,
    job_status    text,
    available_at  timestamptz,
    completed_at  timestamptz
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_status        text;
    v_owner         text;
    v_lease_expires timestamptz;
    v_attempt_count integer;
    v_max_attempts  integer;
    v_final_status  text;
    v_available_at  timestamptz;
    v_completed_at  timestamptz;
BEGIN
    -- Require non-empty worker ID.
    IF COALESCE(TRIM(p_worker_id), '') = '' THEN
        RAISE EXCEPTION 'p_worker_id must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Require non-empty error message.
    IF COALESCE(TRIM(p_error_message), '') = '' THEN
        RAISE EXCEPTION 'p_error_message must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- retry_delay_seconds must be between 0 and 86400.
    IF COALESCE(p_retry_delay_seconds, -1) < 0
       OR COALESCE(p_retry_delay_seconds, -1) > 86400 THEN
        RAISE EXCEPTION
            'p_retry_delay_seconds must be between 0 and 86400, got: %',
            p_retry_delay_seconds
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Lock the job row.
    SELECT bj.job_status, bj.lease_owner, bj.lease_expires_at,
           bj.attempt_count, bj.max_attempts
    INTO   v_status, v_owner, v_lease_expires, v_attempt_count, v_max_attempts
    FROM   public.background_jobs bj
    WHERE  bj.id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'background_job not found: %', p_job_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Job must be leased or running.
    IF v_status NOT IN ('leased', 'running') THEN
        RAISE EXCEPTION
            'job % has status %; fail requires leased or running',
            p_job_id, v_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Lease owner must match.
    IF v_owner IS DISTINCT FROM p_worker_id THEN
        RAISE EXCEPTION 'job % is owned by %, not %', p_job_id, v_owner, p_worker_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Determine final state: retry or dead-letter.
    IF v_attempt_count < v_max_attempts THEN
        v_final_status := 'pending';
        v_available_at := now() + (p_retry_delay_seconds || ' seconds')::interval;
        v_completed_at := NULL;
    ELSE
        v_final_status := 'dead_letter';
        v_available_at := NULL;   -- retain existing available_at for dead-letter
        v_completed_at := now();
    END IF;

    UPDATE public.background_jobs
    SET    job_status        = v_final_status,
           available_at      = CASE
                                   WHEN v_final_status = 'pending'
                                   THEN v_available_at
                                   ELSE available_at
                               END,
           completed_at      = v_completed_at,
           error_message     = p_error_message,
           checkpoint        = CASE
                                   WHEN p_checkpoint IS NOT NULL
                                   THEN checkpoint || p_checkpoint
                                   ELSE checkpoint
                               END,
           metadata          = metadata || COALESCE(p_metadata, '{}'::jsonb),
           lease_owner       = NULL,
           lease_expires_at  = NULL,
           heartbeat_at      = NULL,
           updated_at        = now()
    WHERE  id = p_job_id;

    RETURN QUERY
        SELECT p_job_id, v_final_status, v_available_at, v_completed_at;
END;
$$;


-- =============================================================================
-- 4. recover_expired_background_jobs_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.recover_expired_background_jobs_v1(
    p_limit               integer DEFAULT 100,
    p_retry_delay_seconds integer DEFAULT 60
)
RETURNS TABLE (
    recovered_count   integer,
    dead_letter_count integer
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_recovered   integer := 0;
    v_dead_letter integer := 0;
BEGIN
    -- limit must be between 1 and 1000.
    IF COALESCE(p_limit, 0) < 1 OR COALESCE(p_limit, 0) > 1000 THEN
        RAISE EXCEPTION 'p_limit must be between 1 and 1000, got: %', p_limit
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- retry_delay_seconds must be between 0 and 86400.
    IF COALESCE(p_retry_delay_seconds, -1) < 0
       OR COALESCE(p_retry_delay_seconds, -1) > 86400 THEN
        RAISE EXCEPTION
            'p_retry_delay_seconds must be between 0 and 86400, got: %',
            p_retry_delay_seconds
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Atomically reclaim expired jobs using FOR UPDATE SKIP LOCKED.
    -- A job is expired when it is in leased or running state and its
    -- lease_expires_at is in the past.  SKIP LOCKED ensures concurrent
    -- recovery calls each process a distinct set without contention.
    -- -------------------------------------------------------------------------
    WITH expired AS (
        SELECT bj.id,
               bj.attempt_count,
               bj.max_attempts
        FROM   public.background_jobs bj
        WHERE  bj.job_status    IN ('leased', 'running')
          AND  bj.lease_expires_at < now()
        ORDER  BY bj.lease_expires_at ASC
        LIMIT  p_limit
        FOR UPDATE SKIP LOCKED
    ),
    reclaimed AS (
        UPDATE public.background_jobs bj
        SET    job_status        = CASE
                                       WHEN e.attempt_count < e.max_attempts
                                       THEN 'pending'
                                       ELSE 'dead_letter'
                                   END,
               available_at      = CASE
                                       WHEN e.attempt_count < e.max_attempts
                                       THEN now() + (p_retry_delay_seconds || ' seconds')::interval
                                       ELSE bj.available_at
                                   END,
               completed_at      = CASE
                                       WHEN e.attempt_count < e.max_attempts
                                       THEN NULL
                                       ELSE now()
                                   END,
               error_message     = 'Lease expired: reclaimed by recover_expired_background_jobs_v1',
               lease_owner       = NULL,
               lease_expires_at  = NULL,
               heartbeat_at      = NULL,
               updated_at        = now()
        FROM   expired e
        WHERE  bj.id = e.id
        RETURNING bj.job_status
    )
    SELECT
        COUNT(*) FILTER (WHERE job_status = 'pending')::integer,
        COUNT(*) FILTER (WHERE job_status = 'dead_letter')::integer
    INTO v_recovered, v_dead_letter
    FROM reclaimed;

    RETURN QUERY SELECT COALESCE(v_recovered, 0), COALESCE(v_dead_letter, 0);
END;
$$;


-- =============================================================================
-- Privilege hardening — heartbeat_background_job_v1
-- =============================================================================

REVOKE ALL ON FUNCTION public.heartbeat_background_job_v1(
    uuid, text, integer, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.heartbeat_background_job_v1(
    uuid, text, integer, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.heartbeat_background_job_v1(
    uuid, text, integer, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.heartbeat_background_job_v1(
    uuid, text, integer, jsonb
) TO service_role;

COMMENT ON FUNCTION public.heartbeat_background_job_v1(uuid, text, integer, jsonb) IS
'Renews a job lease and transitions status to running.  Rejects expired leases
and owner mismatches.  Merges checkpoint only when supplied.
Only writes to background_jobs.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';


-- =============================================================================
-- Privilege hardening — complete_background_job_v1
-- =============================================================================

REVOKE ALL ON FUNCTION public.complete_background_job_v1(
    uuid, text, jsonb, jsonb, numeric, integer, integer, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.complete_background_job_v1(
    uuid, text, jsonb, jsonb, numeric, integer, integer, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.complete_background_job_v1(
    uuid, text, jsonb, jsonb, numeric, integer, integer, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.complete_background_job_v1(
    uuid, text, jsonb, jsonb, numeric, integer, integer, jsonb
) TO service_role;

COMMENT ON FUNCTION public.complete_background_job_v1(
    uuid, text, jsonb, jsonb, numeric, integer, integer, jsonb
) IS
'Marks a background_job as completed.  Stores result, costs, and tokens.
Clears lease fields.  Idempotent: re-completing an already-completed job
returns current state without any write.
Only writes to background_jobs.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';


-- =============================================================================
-- Privilege hardening — fail_background_job_v1
-- =============================================================================

REVOKE ALL ON FUNCTION public.fail_background_job_v1(
    uuid, text, text, integer, jsonb, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.fail_background_job_v1(
    uuid, text, text, integer, jsonb, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.fail_background_job_v1(
    uuid, text, text, integer, jsonb, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.fail_background_job_v1(
    uuid, text, text, integer, jsonb, jsonb
) TO service_role;

COMMENT ON FUNCTION public.fail_background_job_v1(
    uuid, text, text, integer, jsonb, jsonb
) IS
'Records a job failure.  Schedules a retry (pending) when attempts remain,
or moves to dead_letter when max_attempts is reached.  Clears lease fields.
Only writes to background_jobs.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';


-- =============================================================================
-- Privilege hardening — recover_expired_background_jobs_v1
-- =============================================================================

REVOKE ALL ON FUNCTION public.recover_expired_background_jobs_v1(
    integer, integer
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.recover_expired_background_jobs_v1(
    integer, integer
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.recover_expired_background_jobs_v1(
    integer, integer
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.recover_expired_background_jobs_v1(
    integer, integer
) TO service_role;

COMMENT ON FUNCTION public.recover_expired_background_jobs_v1(integer, integer) IS
'Reclaims leased/running jobs whose leases have expired using FOR UPDATE SKIP
LOCKED.  Retries eligible jobs (pending) or dead-letters exhausted ones.
Concurrent calls each reclaim a distinct set without contention.
Only writes to background_jobs.  No rows are deleted.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';
