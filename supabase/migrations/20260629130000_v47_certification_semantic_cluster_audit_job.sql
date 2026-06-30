-- =============================================================================
-- V47 certification semantic concept-cluster audit job support
-- Created : 2026-06-29 13:00:00 UTC
--
-- Purpose
-- -------
--   * Register certification_semantic_cluster_audit background job type
--   * Durable idempotency for SEMANTIC_CONCEPT_CLUSTER_OVERSIZE findings
--   * list_semantic_concept_cluster_keys_v1 RPC for cross-run reads
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
                'question_generation',
                'candidate_promotion',
                'embedding_generation',
                'other'
            )
        );

CREATE UNIQUE INDEX IF NOT EXISTS idx_af_semantic_concept_cluster_dedupe
    ON public.audit_findings (
        (metadata->>'certification_exam_name'),
        (metadata->>'cluster_id'),
        (metadata->>'model_name'),
        (COALESCE(NULLIF(TRIM(metadata->>'ruleset_version'), ''), ''))
    )
    WHERE finding_code = 'SEMANTIC_CONCEPT_CLUSTER_OVERSIZE';

CREATE OR REPLACE FUNCTION public.list_semantic_concept_cluster_keys_v1(
    p_certification_exam_name text,
    p_ruleset_version         text DEFAULT NULL,
    p_model_name              text DEFAULT NULL
)
RETURNS TABLE (
    cluster_id        text,
    model_name        text,
    ruleset_version   text
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
    SELECT DISTINCT
        af.metadata->>'cluster_id',
        af.metadata->>'model_name',
        COALESCE(NULLIF(TRIM(af.metadata->>'ruleset_version'), ''), '')
    FROM   public.audit_findings AS af
    WHERE  af.finding_code = 'SEMANTIC_CONCEPT_CLUSTER_OVERSIZE'
      AND  af.metadata->>'certification_exam_name' = TRIM(p_certification_exam_name)
      AND  COALESCE(af.metadata->>'cluster_id', '') <> ''
      AND  COALESCE(af.metadata->>'model_name', '') <> ''
      AND  (
               p_ruleset_version IS NULL
               OR COALESCE(NULLIF(TRIM(af.metadata->>'ruleset_version'), ''), '')
                  = COALESCE(NULLIF(TRIM(p_ruleset_version), ''), '')
           )
      AND  (
               p_model_name IS NULL
               OR af.metadata->>'model_name' = TRIM(p_model_name)
           );
$$;

REVOKE ALL ON FUNCTION public.list_semantic_concept_cluster_keys_v1(text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.list_semantic_concept_cluster_keys_v1(text, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.list_semantic_concept_cluster_keys_v1(text, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.list_semantic_concept_cluster_keys_v1(text, text, text) TO service_role;

COMMENT ON FUNCTION public.list_semantic_concept_cluster_keys_v1(text, text, text) IS
'Returns canonical semantic concept-cluster keys already persisted in audit_findings
for one certification (optional ruleset_version and model_name filters).';

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

CREATE OR REPLACE FUNCTION public.complete_audit_run_v1(
    p_audit_run_id  uuid,
    p_findings      jsonb DEFAULT '[]'::jsonb,
    p_metadata      jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    audit_run_id    uuid,
    run_status      text,
    finding_count   integer,
    evidence_count  integer
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_run_status    text;
    v_target_qvid   uuid;
    v_finding_count integer;
    v_evidence_count integer;

    -- Finding loop variables.
    v_fi            integer;
    v_finding       jsonb;
    v_finding_id    uuid;
    v_finding_code  text;
    v_finding_type  text;
    v_severity      text;
    v_materiality   text;
    v_title         text;
    v_description   text;
    v_confidence    numeric;

    -- Evidence loop variables.
    v_ei            integer;
    v_evidence      jsonb;
    v_chunk_id      uuid;
    v_role          text;
    v_quote         text;
    v_relevance     numeric;

    -- Collected chunk IDs for bulk existence check.
    v_chunk_ids     uuid[] := '{}';
BEGIN
    -- -------------------------------------------------------------------------
    -- Lock the audit run.  Serialises concurrent completion calls.
    -- -------------------------------------------------------------------------
    SELECT ar.run_status, ar.target_question_version_id
    INTO   v_run_status, v_target_qvid
    FROM   public.audit_runs ar
    WHERE  ar.id = p_audit_run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_run not found: %', p_audit_run_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_target_qvid IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(hashtext(v_target_qvid::text));
    END IF;

    -- -------------------------------------------------------------------------
    -- Idempotency: if already completed, return existing counts with no writes.
    -- -------------------------------------------------------------------------
    IF v_run_status = 'completed' THEN
        SELECT COUNT(af.id)::integer,
               COUNT(afe.id)::integer
        INTO   v_finding_count, v_evidence_count
        FROM   public.audit_findings af
        LEFT JOIN public.audit_finding_evidence afe
               ON afe.finding_id = af.id
        WHERE  af.audit_run_id = p_audit_run_id;

        RETURN QUERY
            SELECT p_audit_run_id, 'completed'::text,
                   v_finding_count, v_evidence_count;
        RETURN;
    END IF;

    -- -------------------------------------------------------------------------
    -- Run must be in a transitionable state.
    -- -------------------------------------------------------------------------
    IF v_run_status NOT IN ('pending', 'running') THEN
        RAISE EXCEPTION
            'audit_run % has status %; only pending or running runs can be completed',
            p_audit_run_id, v_run_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- p_findings must be a JSON array.
    -- -------------------------------------------------------------------------
    IF jsonb_typeof(COALESCE(p_findings, '[]'::jsonb)) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_findings must be a JSON array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Validate all findings and collect resource_chunk_ids before any writes.
    -- -------------------------------------------------------------------------
    FOR v_fi IN 0 .. jsonb_array_length(COALESCE(p_findings, '[]'::jsonb)) - 1 LOOP
        v_finding      := COALESCE(p_findings, '[]'::jsonb) -> v_fi;
        v_finding_code := TRIM(v_finding ->> 'finding_code');
        v_finding_type := TRIM(v_finding ->> 'finding_type');
        v_severity     := TRIM(v_finding ->> 'severity');
        v_title        := TRIM(v_finding ->> 'title');
        v_description  := TRIM(v_finding ->> 'description');
        v_materiality  := COALESCE(NULLIF(TRIM(v_finding ->> 'materiality'), ''), 'warning');

        -- Required text fields.
        IF COALESCE(v_finding_code, '') = '' THEN
            RAISE EXCEPTION 'finding % is missing finding_code', v_fi
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_finding_type, '') = '' THEN
            RAISE EXCEPTION 'finding % (code=%) is missing finding_type',
                v_fi, v_finding_code
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_severity, '') = '' THEN
            RAISE EXCEPTION 'finding % (code=%) is missing severity',
                v_fi, v_finding_code
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_title, '') = '' THEN
            RAISE EXCEPTION 'finding % (code=%) is missing title',
                v_fi, v_finding_code
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_description, '') = '' THEN
            RAISE EXCEPTION 'finding % (code=%) is missing description',
                v_fi, v_finding_code
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- Materiality validation (defaults to warning when absent).
        IF v_materiality NOT IN ('blocking', 'warning', 'informational') THEN
            RAISE EXCEPTION 'finding % (code=%) has invalid materiality: %',
                v_fi, v_finding_code, v_materiality
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- Enum validations.
        IF v_finding_type NOT IN (
            'correctness', 'ambiguity', 'duplication', 'outdated',
            'formatting', 'coverage', 'difficulty', 'cognitive_level',
            'answer_quality', 'explanation_quality', 'source_support',
            'policy', 'other'
        ) THEN
            RAISE EXCEPTION 'finding % (code=%) has invalid finding_type: %',
                v_fi, v_finding_code, v_finding_type
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_severity NOT IN ('info', 'low', 'medium', 'high', 'critical') THEN
            RAISE EXCEPTION 'finding % (code=%) has invalid severity: %',
                v_fi, v_finding_code, v_severity
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- Optional confidence.
        IF (v_finding ->> 'confidence') IS NOT NULL THEN
            BEGIN
                v_confidence := (v_finding ->> 'confidence')::numeric;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION
                    'finding % (code=%) has non-numeric confidence',
                    v_fi, v_finding_code
                    USING ERRCODE = 'invalid_parameter_value';
            END;
            IF v_confidence < 0 OR v_confidence > 1 THEN
                RAISE EXCEPTION
                    'finding % (code=%) confidence must be in [0,1], got: %',
                    v_fi, v_finding_code, v_confidence
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END IF;

        -- Validate evidence items within this finding.
        IF (v_finding -> 'evidence') IS NOT NULL
           AND jsonb_typeof(v_finding -> 'evidence') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION
                'finding % (code=%) evidence must be a JSON array',
                v_fi, v_finding_code
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        FOR v_ei IN 0 .. jsonb_array_length(
            COALESCE(v_finding -> 'evidence', '[]'::jsonb)
        ) - 1 LOOP
            v_evidence := COALESCE(v_finding -> 'evidence', '[]'::jsonb) -> v_ei;

            -- resource_chunk_id required.
            BEGIN
                v_chunk_id := (v_evidence ->> 'resource_chunk_id')::uuid;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION
                    'finding % evidence % has invalid or missing resource_chunk_id',
                    v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END;
            IF v_chunk_id IS NULL THEN
                RAISE EXCEPTION
                    'finding % evidence % is missing resource_chunk_id',
                    v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            -- evidence_role required and valid.
            v_role := TRIM(v_evidence ->> 'evidence_role');
            IF COALESCE(v_role, '') = '' THEN
                RAISE EXCEPTION
                    'finding % evidence % is missing evidence_role',
                    v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF v_role NOT IN ('supporting', 'contradicting', 'contextual') THEN
                RAISE EXCEPTION
                    'finding % evidence % has invalid evidence_role: %',
                    v_fi, v_ei, v_role
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            -- Optional quote_text non-empty when present.
            v_quote := v_evidence ->> 'quote_text';
            IF v_quote IS NOT NULL AND TRIM(v_quote) = '' THEN
                RAISE EXCEPTION
                    'finding % evidence % has empty quote_text',
                    v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            -- Optional relevance_score in [0, 1].
            IF (v_evidence ->> 'relevance_score') IS NOT NULL THEN
                BEGIN
                    v_relevance := (v_evidence ->> 'relevance_score')::numeric;
                EXCEPTION WHEN others THEN
                    RAISE EXCEPTION
                        'finding % evidence % has non-numeric relevance_score',
                        v_fi, v_ei
                        USING ERRCODE = 'invalid_parameter_value';
                END;
                IF v_relevance < 0 OR v_relevance > 1 THEN
                    RAISE EXCEPTION
                        'finding % evidence % relevance_score must be in [0,1], got: %',
                        v_fi, v_ei, v_relevance
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
            END IF;

            -- Collect chunk ID for bulk existence check.
            v_chunk_ids := v_chunk_ids || v_chunk_id;
        END LOOP;
    END LOOP;

    -- -------------------------------------------------------------------------
    -- Bulk-confirm all referenced resource_chunk_id values exist.
    -- Done after full structural validation, before any writes.
    -- -------------------------------------------------------------------------
    IF array_length(v_chunk_ids, 1) > 0 THEN
        IF EXISTS (
            SELECT 1
            FROM   unnest(v_chunk_ids) AS cid(id)
            WHERE  NOT EXISTS (
                SELECT 1 FROM public.resource_chunks rc WHERE rc.id = cid.id
            )
        ) THEN
            RAISE EXCEPTION
                'one or more resource_chunk_id values do not exist in resource_chunks'
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    END IF;

    -- -------------------------------------------------------------------------
    -- All validation passed.  Insert findings and evidence atomically.
    -- -------------------------------------------------------------------------
    v_finding_count  := 0;
    v_evidence_count := 0;

    FOR v_fi IN 0 .. jsonb_array_length(COALESCE(p_findings, '[]'::jsonb)) - 1 LOOP
        v_finding      := COALESCE(p_findings, '[]'::jsonb) -> v_fi;
        v_finding_code := TRIM(v_finding ->> 'finding_code');
        v_finding_type := TRIM(v_finding ->> 'finding_type');
        v_severity     := TRIM(v_finding ->> 'severity');
        v_materiality  := COALESCE(NULLIF(TRIM(v_finding ->> 'materiality'), ''), 'warning');
        v_title        := TRIM(v_finding ->> 'title');
        v_description  := TRIM(v_finding ->> 'description');
        v_confidence   := (v_finding ->> 'confidence')::numeric;

        IF v_finding_code IN (
            'DUPLICATE_QUESTION_STEM_EXACT',
            'DUPLICATE_QUESTION_STEM_NEAR_EXACT'
        ) THEN
            v_finding_id := NULL;

            INSERT INTO public.audit_findings (
                id,
                audit_run_id,
                finding_code,
                finding_type,
                severity,
                materiality,
                title,
                description,
                field_path,
                confidence,
                detector_name,
                detector_version,
                metadata
            ) VALUES (
                gen_random_uuid(),
                p_audit_run_id,
                v_finding_code,
                v_finding_type,
                v_severity,
                v_materiality,
                v_title,
                v_description,
                v_finding ->> 'field_path',
                v_confidence,
                v_finding ->> 'detector_name',
                v_finding ->> 'detector_version',
                COALESCE((v_finding -> 'metadata')::jsonb, '{}'::jsonb)
            )
            ON CONFLICT (
                (metadata->>'certification_exam_name'),
                (metadata->>'question_version_id_a'),
                (metadata->>'question_version_id_b'),
                (metadata->>'detection_method'),
                (COALESCE(NULLIF(TRIM(metadata->>'ruleset_version'), ''), ''))
            ) WHERE finding_code IN (
                'DUPLICATE_QUESTION_STEM_EXACT',
                'DUPLICATE_QUESTION_STEM_NEAR_EXACT'
            )
            DO NOTHING
            RETURNING id INTO v_finding_id;

            IF v_finding_id IS NULL THEN
                CONTINUE;
            END IF;
        ELSIF v_finding_code = 'SEMANTIC_CONCEPT_CLUSTER_OVERSIZE' THEN
            v_finding_id := NULL;

            INSERT INTO public.audit_findings (
                id,
                audit_run_id,
                finding_code,
                finding_type,
                severity,
                materiality,
                title,
                description,
                field_path,
                confidence,
                detector_name,
                detector_version,
                metadata
            ) VALUES (
                gen_random_uuid(),
                p_audit_run_id,
                v_finding_code,
                v_finding_type,
                v_severity,
                v_materiality,
                v_title,
                v_description,
                v_finding ->> 'field_path',
                v_confidence,
                v_finding ->> 'detector_name',
                v_finding ->> 'detector_version',
                COALESCE((v_finding -> 'metadata')::jsonb, '{}'::jsonb)
            )
            ON CONFLICT (
                (metadata->>'certification_exam_name'),
                (metadata->>'cluster_id'),
                (metadata->>'model_name'),
                (COALESCE(NULLIF(TRIM(metadata->>'ruleset_version'), ''), ''))
            ) WHERE finding_code = 'SEMANTIC_CONCEPT_CLUSTER_OVERSIZE'
            DO NOTHING
            RETURNING id INTO v_finding_id;

            IF v_finding_id IS NULL THEN
                CONTINUE;
            END IF;
        ELSE
            v_finding_id := gen_random_uuid();

            INSERT INTO public.audit_findings (
                id,
                audit_run_id,
                finding_code,
                finding_type,
                severity,
                materiality,
                title,
                description,
                field_path,
                confidence,
                detector_name,
                detector_version,
                metadata
            ) VALUES (
                v_finding_id,
                p_audit_run_id,
                v_finding_code,
                v_finding_type,
                v_severity,
                v_materiality,
                v_title,
                v_description,
                v_finding ->> 'field_path',
                v_confidence,
                v_finding ->> 'detector_name',
                v_finding ->> 'detector_version',
                COALESCE((v_finding -> 'metadata')::jsonb, '{}'::jsonb)
            );
        END IF;

        v_finding_count := v_finding_count + 1;

        -- Insert evidence items for this finding.
        FOR v_ei IN 0 .. jsonb_array_length(
            COALESCE(v_finding -> 'evidence', '[]'::jsonb)
        ) - 1 LOOP
            v_evidence  := COALESCE(v_finding -> 'evidence', '[]'::jsonb) -> v_ei;
            v_chunk_id  := (v_evidence ->> 'resource_chunk_id')::uuid;
            v_role      := TRIM(v_evidence ->> 'evidence_role');
            v_quote     := v_evidence ->> 'quote_text';
            v_relevance := (v_evidence ->> 'relevance_score')::numeric;

            INSERT INTO public.audit_finding_evidence (
                id,
                finding_id,
                resource_chunk_id,
                evidence_role,
                quote_text,
                relevance_score,
                metadata
            ) VALUES (
                gen_random_uuid(),
                v_finding_id,
                v_chunk_id,
                v_role,
                v_quote,
                v_relevance,
                COALESCE((v_evidence -> 'metadata')::jsonb, '{}'::jsonb)
            );

            v_evidence_count := v_evidence_count + 1;
        END LOOP;
    END LOOP;

    -- -------------------------------------------------------------------------
    -- Transition the run to completed.
    -- Set started_at = now() if not already recorded.
    -- Merge caller-supplied metadata into the existing run metadata.
    -- -------------------------------------------------------------------------
    UPDATE public.audit_runs
    SET    run_status   = 'completed',
           started_at   = COALESCE(started_at, now()),
           completed_at = now(),
           metadata     = metadata || COALESCE(p_metadata, '{}'::jsonb)
    WHERE  id = p_audit_run_id;

    RETURN QUERY
        SELECT p_audit_run_id, 'completed'::text,
               v_finding_count, v_evidence_count;
END;
$$;

REVOKE ALL ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) FROM anon;
REVOKE EXECUTE ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) TO service_role;

COMMENT ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) IS
'Atomically inserts all findings and evidence for an audit run, then transitions
it to completed. Duplicate-question pair and semantic concept-cluster oversize
findings use ON CONFLICT DO NOTHING against their partial unique indexes.';
