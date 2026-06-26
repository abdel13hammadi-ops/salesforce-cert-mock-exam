-- =============================================================================
-- V45 certification-wide duplicate question audit job support
-- Created : 2026-06-24 16:00:00 UTC
--
-- Purpose
-- -------
--   * Register certification_duplicate_audit background job type
--   * list_certification_current_question_versions_v1 RPC for latest version
--     per active live question in one certification
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
                'question_generation',
                'candidate_promotion',
                'embedding_generation',
                'other'
            )
        );

CREATE OR REPLACE FUNCTION public.list_certification_current_question_versions_v1(
    p_certification_exam_name text
)
RETURNS TABLE (
    question_version_id  uuid,
    question_id          integer,
    certification_exam_name text,
    question_text        text,
    category             text,
    version_number       integer
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
    SELECT
        current_qv.id,
        q.id,
        q.exam_name,
        current_qv.question_text,
        current_qv.category,
        current_qv.version_number
    FROM   public.questions AS q
    JOIN LATERAL (
        SELECT qv.id,
               qv.question_text,
               qv.category,
               qv.version_number
        FROM   public.question_versions AS qv
        WHERE  qv.question_id = q.id
        ORDER  BY qv.version_number DESC,
                 qv.created_at DESC,
                 qv.id DESC
        LIMIT  1
    ) AS current_qv ON TRUE
    WHERE  TRIM(q.exam_name) = TRIM(p_certification_exam_name)
      AND  q.is_active = TRUE;
$$;

REVOKE ALL ON FUNCTION public.list_certification_current_question_versions_v1(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.list_certification_current_question_versions_v1(text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.list_certification_current_question_versions_v1(text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.list_certification_current_question_versions_v1(text) TO service_role;

COMMENT ON FUNCTION public.list_certification_current_question_versions_v1(text) IS
'Returns the latest question_version row (highest version_number) for each active
live question in one certification (exam_name). Tie-breakers: created_at, id.
Does not require question_version_events.';

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
'Inserts one pending background_jobs row.  Validates job_type including
certification_duplicate_audit.  Execute permission: service_role only.';

-- =============================================================================
-- claim_background_job_v1 — include certification_duplicate_audit in allowlist
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
LOCKED.  Validates p_job_types including certification_duplicate_audit.
Execute permission: service_role only.';
