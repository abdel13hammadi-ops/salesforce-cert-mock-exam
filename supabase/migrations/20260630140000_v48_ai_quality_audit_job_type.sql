-- =============================================================================
-- V48 Phase 3: ai_quality_audit_smoke background job type
-- Created : 2026-06-30 14:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Registers exactly one new background job type, ai_quality_audit_smoke,
-- for the ten-question AI quality-audit smoke batch.
--
-- Scope
-- -----
--   * Updates the background_jobs_type_valid table CHECK constraint.
--   * Updates the explicit job_type allowlists inlined in
--     enqueue_background_job_v1 and claim_background_job_v1 (the only two
--     RPCs in this repository found to contain such an allowlist — see
--     20260624023700_v44_background_job_enqueue_claim_rpcs.sql and the v45/
--     v47 CREATE OR REPLACE updates to them).
--   * heartbeat_background_job_v1, complete_background_job_v1,
--     fail_background_job_v1, and recover_expired_background_jobs_v1 were
--     inspected (20260624024200_v44_background_job_lifecycle_rpcs.sql) and
--     contain no job_type allowlist of any kind — they operate purely on
--     job_status/lease fields and are intentionally left untouched.
--   * Every previously registered job type is preserved unchanged in both
--     the table CHECK and both RPC allowlists.
-- =============================================================================

ALTER TABLE public.background_jobs
    DROP CONSTRAINT IF EXISTS background_jobs_type_valid;

ALTER TABLE public.background_jobs
    ADD CONSTRAINT background_jobs_type_valid
        CHECK (
            job_type IN (
                'resource_ingestion',
                'deterministic_audit',
                'llm_audit',
                'hybrid_audit',
                'certification_duplicate_audit',
                'certification_semantic_cluster_audit',
                'ai_quality_audit_smoke',
                'question_generation',
                'candidate_promotion',
                'embedding_generation',
                'other'
            )
        );

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
    IF COALESCE(TRIM(p_created_by), '') = '' THEN
        RAISE EXCEPTION 'p_created_by must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_job_type NOT IN (
        'resource_ingestion', 'deterministic_audit', 'llm_audit',
        'hybrid_audit', 'certification_duplicate_audit',
        'certification_semantic_cluster_audit',
        'ai_quality_audit_smoke',
        'question_generation', 'candidate_promotion',
        'embedding_generation', 'other'
    ) THEN
        RAISE EXCEPTION 'invalid job_type: %', p_job_type
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(p_priority, -1) < 0 THEN
        RAISE EXCEPTION 'p_priority must be >= 0, got: %', p_priority
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(p_max_attempts, 0) <= 0 THEN
        RAISE EXCEPTION 'p_max_attempts must be > 0, got: %', p_max_attempts
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_estimated_cost_usd IS NOT NULL AND p_estimated_cost_usd < 0 THEN
        RAISE EXCEPTION 'p_estimated_cost_usd must be >= 0, got: %',
            p_estimated_cost_usd
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    v_job_id := gen_random_uuid();

    INSERT INTO public.background_jobs (
        id,
        job_type,
        job_status,
        priority,
        payload,
        max_attempts,
        available_at,
        created_by,
        model_name,
        prompt_version,
        estimated_cost_usd,
        metadata
    ) VALUES (
        v_job_id,
        p_job_type,
        'pending',
        COALESCE(p_priority, 100),
        COALESCE(p_payload, '{}'::jsonb),
        COALESCE(p_max_attempts, 3),
        COALESCE(p_available_at, now()),
        TRIM(p_created_by),
        p_model_name,
        p_prompt_version,
        p_estimated_cost_usd,
        COALESCE(p_metadata, '{}'::jsonb)
    );

    RETURN QUERY
        SELECT v_job_id, 'pending'::text;
END;
$$;

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
    IF COALESCE(TRIM(p_worker_id), '') = '' THEN
        RAISE EXCEPTION 'p_worker_id must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(p_lease_seconds, -1) < 30 OR COALESCE(p_lease_seconds, -1) > 3600 THEN
        RAISE EXCEPTION 'p_lease_seconds must be between 30 and 3600, got: %',
            p_lease_seconds
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_job_types IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM   unnest(p_job_types) AS t(jt)
            WHERE  jt NOT IN (
                'resource_ingestion', 'deterministic_audit', 'llm_audit',
                'hybrid_audit', 'certification_duplicate_audit',
                'certification_semantic_cluster_audit',
                'ai_quality_audit_smoke',
                'question_generation', 'candidate_promotion',
                'embedding_generation', 'other'
            )
        ) THEN
            RAISE EXCEPTION
                'p_job_types contains one or more invalid job_type values'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END IF;

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

COMMENT ON CONSTRAINT background_jobs_type_valid ON public.background_jobs IS
'V48: added ai_quality_audit_smoke for the ten-question AI quality-audit
smoke batch. All prior job types preserved.';

COMMENT ON FUNCTION public.enqueue_background_job_v1(
    text, jsonb, integer, integer, timestamptz, text, text, text, numeric, jsonb
) IS
'Inserts one pending background_job. Validates job_type, priority, and
max_attempts. Returns the new job_id and status=pending.
Only writes to background_jobs. job_type allowlist includes
ai_quality_audit_smoke as of V48.
Execute permission: service_role only. PUBLIC, anon, authenticated revoked.';

COMMENT ON FUNCTION public.claim_background_job_v1(text, integer, text[]) IS
'Atomically claims one eligible pending background_job using FOR UPDATE SKIP
LOCKED. Concurrent workers each claim a distinct job without contention.
Transitions the job to leased, sets the lease expiry, and increments
attempt_count. Returns no row when nothing is eligible. job_type allowlist
includes ai_quality_audit_smoke as of V48.
Only writes to background_jobs.
Execute permission: service_role only. PUBLIC, anon, authenticated revoked.';

-- -----------------------------------------------------------------------------
-- No privilege changes are required: CREATE OR REPLACE preserves the
-- existing REVOKE/GRANT state of both functions (PostgreSQL does not reset
-- privileges on REPLACE), and both retain their original signatures.
-- heartbeat_background_job_v1, complete_background_job_v1,
-- fail_background_job_v1, and recover_expired_background_jobs_v1 are
-- intentionally not modified by this migration.
-- -----------------------------------------------------------------------------
