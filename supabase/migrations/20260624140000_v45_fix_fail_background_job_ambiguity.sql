-- =============================================================================
-- V45 Phase 4C: Fix fail_background_job_v1 PL/pgSQL name ambiguity
-- Created : 2026-06-24 14:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Corrects SQLSTATE 42702 in fail_background_job_v1 where unqualified
-- available_at in the UPDATE SET CASE ELSE branch collided with the
-- RETURNS TABLE output variable of the same name.
--
-- Safety guarantees
-- -----------------
--   * Function signature, validation, locking, retry, and dead-letter
--     behavior unchanged.
--   * Only fail_background_job_v1 is replaced; no table changes.
--
-- Security
-- --------
--   Privilege hardening re-applied: service_role only.
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

    UPDATE public.background_jobs AS bj
    SET    job_status        = v_final_status,
           available_at      = CASE
                                   WHEN v_final_status = 'pending'
                                   THEN v_available_at
                                   ELSE bj.available_at
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
    WHERE  bj.id = p_job_id;

    RETURN QUERY
        SELECT p_job_id, v_final_status, v_available_at, v_completed_at;
END;
$$;


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
