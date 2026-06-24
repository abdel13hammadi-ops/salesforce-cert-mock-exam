-- =============================================================================
-- V44 Phase 6C: end_audit_run_v1 RPC
-- Created : 2026-06-24 02:26:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds public.end_audit_run_v1, which transitions an audit run to a terminal
-- failure or cancellation state.
--
-- Safety guarantees
-- -----------------
--   * Only audit_runs is updated.
--   * audit_findings, audit_finding_evidence, question_versions,
--     question_candidates, resource_versions, resource_chunks,
--     exam_attempts, and question_attempts are never touched.
--   * No findings or evidence are created.
--   * Idempotent: re-requesting the same terminal status returns current
--     state without any write.
--   * Concurrent calls serialise via FOR UPDATE on audit_runs.
--
-- Transition rules (enforced in order)
-- -------------------------------------
--   pending  → failed     ✓
--   pending  → cancelled  ✓
--   running  → failed     ✓
--   running  → cancelled  ✓
--   failed   → failed     idempotent (no write)
--   cancelled→ cancelled  idempotent (no write)
--   completed→ any        rejected
--   failed   → cancelled  rejected (cross-terminal)
--   cancelled→ failed     rejected (cross-terminal)
--
-- Security
-- --------
--   EXECUTE revoked from PUBLIC, anon, authenticated.
--   service_role is the only granted caller.
-- =============================================================================


CREATE OR REPLACE FUNCTION public.end_audit_run_v1(
    p_audit_run_id  uuid,
    p_final_status  text,
    p_reason        text,
    p_metadata      jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    audit_run_id  uuid,
    run_status    text,
    completed_at  timestamptz
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_run_status    text;
    v_completed_at  timestamptz;
BEGIN
    -- -------------------------------------------------------------------------
    -- p_final_status must be failed or cancelled.
    -- -------------------------------------------------------------------------
    IF p_final_status NOT IN ('failed', 'cancelled') THEN
        RAISE EXCEPTION
            'p_final_status must be failed or cancelled, got: %', p_final_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Require non-empty reason.
    -- -------------------------------------------------------------------------
    IF COALESCE(TRIM(p_reason), '') = '' THEN
        RAISE EXCEPTION 'p_reason must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Lock the audit run and load its current state.
    -- -------------------------------------------------------------------------
    SELECT ar.run_status, ar.completed_at
    INTO   v_run_status, v_completed_at
    FROM   public.audit_runs ar
    WHERE  ar.id = p_audit_run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_run not found: %', p_audit_run_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- -------------------------------------------------------------------------
    -- Idempotency: already in the requested terminal status — return as-is.
    -- -------------------------------------------------------------------------
    IF v_run_status = p_final_status THEN
        RETURN QUERY
            SELECT p_audit_run_id, v_run_status, v_completed_at;
        RETURN;
    END IF;

    -- -------------------------------------------------------------------------
    -- Reject transition from completed (terminal, succeeded).
    -- -------------------------------------------------------------------------
    IF v_run_status = 'completed' THEN
        RAISE EXCEPTION
            'audit_run % is already completed and cannot be transitioned to %',
            p_audit_run_id, p_final_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Reject cross-terminal transitions (failed ↔ cancelled).
    -- -------------------------------------------------------------------------
    IF v_run_status IN ('failed', 'cancelled') THEN
        RAISE EXCEPTION
            'audit_run % is already in terminal state % and cannot be changed to %',
            p_audit_run_id, v_run_status, p_final_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Only pending and running runs may transition.
    -- (Covers any unexpected status values not matched above.)
    -- -------------------------------------------------------------------------
    IF v_run_status NOT IN ('pending', 'running') THEN
        RAISE EXCEPTION
            'audit_run % has unexpected status % and cannot be transitioned',
            p_audit_run_id, v_run_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Apply the transition.
    --   * started_at set to now() if not already recorded.
    --   * completed_at set to now().
    --   * Metadata merged with caller-supplied values; reason recorded
    --     under a stable key for auditability.
    -- -------------------------------------------------------------------------
    UPDATE public.audit_runs
    SET    run_status   = p_final_status,
           started_at   = COALESCE(started_at, now()),
           completed_at = now(),
           metadata     = metadata
                       || COALESCE(p_metadata, '{}'::jsonb)
                       || jsonb_build_object('reason', p_reason)
    WHERE  id = p_audit_run_id
    RETURNING public.audit_runs.completed_at INTO v_completed_at;

    RETURN QUERY
        SELECT p_audit_run_id, p_final_status, v_completed_at;
END;
$$;


-- =============================================================================
-- Privilege hardening
-- =============================================================================

REVOKE ALL ON FUNCTION public.end_audit_run_v1(
    uuid, text, text, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.end_audit_run_v1(
    uuid, text, text, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.end_audit_run_v1(
    uuid, text, text, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.end_audit_run_v1(
    uuid, text, text, jsonb
) TO service_role;

COMMENT ON FUNCTION public.end_audit_run_v1(uuid, text, text, jsonb) IS
'Transitions an audit_run to failed or cancelled.  Only pending or running
runs may transition.  Idempotent: re-requesting the same terminal status
returns current state without any write.  Cross-terminal transitions
(failed↔cancelled) and transitions from completed are rejected.
Only audit_runs is updated; no findings, evidence, or other tables are
touched.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';
