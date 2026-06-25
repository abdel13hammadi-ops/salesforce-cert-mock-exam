-- =============================================================================
-- V45 Phase 3: Persist audit-finding materiality
-- Created : 2026-06-24 12:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
--   * Add first-class materiality column to audit_findings
--   * Backfill existing rows conservatively
--   * Index for admin filtering by run + materiality + status
--   * Update complete_audit_run_v1 to validate and persist materiality
--
-- Backfill policy
-- ----------------
--   * Default all existing rows to warning
--   * Promote only explicit structural / correctness finding_code values
--     to blocking (conservative, code-based list only)
-- =============================================================================


-- =============================================================================
-- 1. Column, backfill, constraint, index
-- =============================================================================

ALTER TABLE public.audit_findings
    ADD COLUMN IF NOT EXISTS materiality text NOT NULL DEFAULT 'warning';

COMMENT ON COLUMN public.audit_findings.materiality IS
'Publication impact: blocking, warning, or informational. Assigned by worker
policy and validated by complete_audit_run_v1 before insert.';

-- Conservative backfill: explicit blocking codes only; everything else warning.
UPDATE public.audit_findings
SET    materiality = 'blocking'
WHERE  finding_code IN (
    'WRONG_ANSWER_KEY',
    'UNSUPPORTED_ANSWER',
    'MULTIPLE_DEFENSIBLE_ANSWERS',
    'EXPLANATION_MISSING',
    'MISSING_EXPLANATION',
    'OUTDATED_CONTENT',
    'EMPTY_QUESTION_TEXT',
    'INVALID_SELECT_COUNT',
    'TOO_FEW_OPTIONS',
    'EMPTY_OPTION_TEXT',
    'DUPLICATE_OPTION_LABELS',
    'DUPLICATE_OPTION_TEXT',
    'CORRECT_COUNT_MISMATCH',
    'SINGLE_SELECT_COUNT_MISMATCH',
    'DUPLICATE_CORRECT_OPTIONS',
    'OPTION_DISPLAY_ORDER_ISSUES'
);

ALTER TABLE public.audit_findings
    ADD CONSTRAINT audit_findings_materiality_valid
        CHECK (materiality IN ('blocking', 'warning', 'informational'));

CREATE INDEX IF NOT EXISTS idx_af_run_materiality_status
    ON public.audit_findings (audit_run_id, materiality, finding_status);


-- =============================================================================
-- 2. complete_audit_run_v1 - validate and persist materiality
-- =============================================================================

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
    SELECT ar.run_status
    INTO   v_run_status
    FROM   public.audit_runs ar
    WHERE  ar.id = p_audit_run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_run not found: %', p_audit_run_id
            USING ERRCODE = 'no_data_found';
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


-- =============================================================================
-- Privilege hardening — complete_audit_run_v1
-- =============================================================================

REVOKE ALL ON FUNCTION public.complete_audit_run_v1(
    uuid, jsonb, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.complete_audit_run_v1(
    uuid, jsonb, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.complete_audit_run_v1(
    uuid, jsonb, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.complete_audit_run_v1(
    uuid, jsonb, jsonb
) TO service_role;

COMMENT ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) IS
'Atomically inserts all findings and evidence for an audit run, then
transitions it to completed.  Validates the full payload (including
materiality on each finding) before any write.  Missing materiality defaults
to warning; invalid materiality raises invalid_parameter_value.  Idempotent on
already-completed runs.  Execute permission: service_role only.  PUBLIC, anon,
authenticated revoked.';
