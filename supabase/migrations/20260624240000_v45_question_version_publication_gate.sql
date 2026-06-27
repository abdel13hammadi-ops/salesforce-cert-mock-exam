-- =============================================================================
-- V45 Phase 4E: Question-version publication gate
-- Created : 2026-06-24 24:00:00 UTC
--
-- Purpose
-- -------
-- Blocks publish_question_version_v1 when unresolved blocking audit findings
-- are tied to the exact immutable question_version_id.
--
-- Blocking finding statuses: open, accepted
-- Non-blocking: rejected, resolved, overridden
--
-- Safety guarantees
-- -----------------
--   * Gate enforced inside publish_question_version_v1 before any mutation.
--   * Advisory transaction lock serializes publish vs audit completion.
--   * Failed publish performs no content or event writes.
--   * Idempotent re-publish of an already-published version unchanged.
-- =============================================================================


-- =============================================================================
-- 1. Shared tie + eligibility helpers
-- =============================================================================

CREATE OR REPLACE FUNCTION public.audit_finding_tied_to_question_version_v1(
    p_question_version_id  uuid,
    p_run_target_question_version_id uuid,
    p_metadata             jsonb
)
RETURNS boolean
LANGUAGE sql
STABLE
SET search_path = public, pg_catalog
AS $$
    SELECT
        p_question_version_id IS NOT NULL
        AND (
            p_run_target_question_version_id = p_question_version_id
            OR NULLIF(BTRIM(p_metadata ->> 'question_version_id_a'), '')::uuid
               = p_question_version_id
            OR NULLIF(BTRIM(p_metadata ->> 'question_version_id_b'), '')::uuid
               = p_question_version_id
            OR NULLIF(BTRIM(p_metadata -> 'evidence_contract' ->> 'question_version_id'), '')::uuid
               = p_question_version_id
        );
$$;

COMMENT ON FUNCTION public.audit_finding_tied_to_question_version_v1(uuid, uuid, jsonb) IS
'Returns true when an audit finding row is explicitly tied to the exact
question_version_id via run target or persisted metadata anchors only.';


CREATE OR REPLACE FUNCTION public.count_blocking_findings_for_question_version_v1(
    p_question_version_id uuid
)
RETURNS integer
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_count integer;
BEGIN
    IF p_question_version_id IS NULL THEN
        RAISE EXCEPTION 'p_question_version_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT COUNT(*)::integer
    INTO   v_count
    FROM   public.audit_findings af
    JOIN   public.audit_runs ar
           ON ar.id = af.audit_run_id
    WHERE  af.materiality = 'blocking'
      AND  af.finding_status IN ('open', 'accepted')
      AND  public.audit_finding_tied_to_question_version_v1(
               p_question_version_id,
               ar.target_question_version_id,
               af.metadata
           );

    RETURN COALESCE(v_count, 0);
END;
$$;


CREATE OR REPLACE FUNCTION public.is_question_version_publishable_v1(
    p_question_version_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_catalog
AS $$
BEGIN
    IF p_question_version_id IS NULL THEN
        RAISE EXCEPTION 'p_question_version_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    RETURN public.count_blocking_findings_for_question_version_v1(
        p_question_version_id
    ) = 0;
END;
$$;


CREATE OR REPLACE FUNCTION public.get_question_version_publication_status_v1(
    p_question_version_id uuid
)
RETURNS TABLE (
    question_version_id       uuid,
    publishable               boolean,
    blocking_finding_count    integer,
    blocking_findings         jsonb
)
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_count integer;
BEGIN
    IF p_question_version_id IS NULL THEN
        RAISE EXCEPTION 'p_question_version_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    v_count := public.count_blocking_findings_for_question_version_v1(
        p_question_version_id
    );

    RETURN QUERY
    SELECT
        p_question_version_id,
        (v_count = 0),
        v_count,
        COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'finding_id', af.id,
                        'finding_code', af.finding_code,
                        'finding_status', af.finding_status,
                        'materiality', af.materiality,
                        'title', af.title
                    )
                    ORDER BY af.created_at ASC
                )
                FROM public.audit_findings af
                JOIN public.audit_runs ar
                  ON ar.id = af.audit_run_id
                WHERE af.materiality = 'blocking'
                  AND af.finding_status IN ('open', 'accepted')
                  AND public.audit_finding_tied_to_question_version_v1(
                        p_question_version_id,
                        ar.target_question_version_id,
                        af.metadata
                    )
            ),
            '[]'::jsonb
        );
END;
$$;


-- =============================================================================
-- 2. publish_question_version_v1 — add audit publication gate
-- =============================================================================

CREATE OR REPLACE FUNCTION public.publish_question_version_v1(
    p_question_version_id  uuid,
    p_actor_email          text,
    p_reason               text,
    p_event_data           jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    question_version_id  uuid,
    question_id          integer,
    version_number       integer
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_question_id          integer;
    v_version_number       integer;
    v_question_text        text;
    v_explanation          text;
    v_category             text;
    v_difficulty           text;
    v_cognitive_level      text;
    v_concept_key          text;
    v_question_type        text;
    v_select_count         integer;
    v_language_code        text;
    v_option_count         integer;
    v_correct_count        integer;
    v_prev_published_id    uuid;
    v_max_published_vn     integer;
    v_blocking_count       integer;
BEGIN
    IF COALESCE(TRIM(p_actor_email), '') = '' THEN
        RAISE EXCEPTION 'p_actor_email must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtext(p_question_version_id::text));

    SELECT qv.question_id,
           qv.version_number,
           qv.question_text,
           qv.explanation,
           qv.category,
           qv.difficulty,
           qv.cognitive_level,
           qv.concept_key,
           qv.question_type,
           qv.select_count,
           qv.language_code
    INTO   v_question_id, v_version_number,
           v_question_text, v_explanation, v_category, v_difficulty,
           v_cognitive_level, v_concept_key, v_question_type,
           v_select_count, v_language_code
    FROM   public.question_versions qv
    WHERE  qv.id = p_question_version_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'question_version not found: %', p_question_version_id
            USING ERRCODE = 'no_data_found';
    END IF;

    PERFORM 1
    FROM   public.questions
    WHERE  id = v_question_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'live question not found for question_id=%', v_question_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM   public.question_version_events
        WHERE  question_version_id = p_question_version_id
          AND  event_type          = 'published'
        LIMIT 1
    ) THEN
        RETURN QUERY
            SELECT p_question_version_id, v_question_id, v_version_number;
        RETURN;
    END IF;

    SELECT MAX(qv.version_number)
    INTO   v_max_published_vn
    FROM   public.question_version_events qve
    JOIN   public.question_versions qv
           ON qv.id = qve.question_version_id
    WHERE  qv.question_id = v_question_id
      AND  qve.event_type = 'published';

    IF v_max_published_vn IS NOT NULL
       AND v_version_number < v_max_published_vn THEN
        RAISE EXCEPTION
            'cannot publish version % for question %: version % is already published',
            v_version_number, v_question_id, v_max_published_vn
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM   public.question_version_events
        WHERE  question_version_id = p_question_version_id
          AND  event_type          = 'approved'
        LIMIT 1
    ) THEN
        RAISE EXCEPTION
            'question_version % must have an approved event before publishing',
            p_question_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Lock blocking findings tied to this exact version before counting.
    PERFORM af.id
    FROM   public.audit_findings af
    JOIN   public.audit_runs ar
           ON ar.id = af.audit_run_id
    WHERE  af.materiality = 'blocking'
      AND  af.finding_status IN ('open', 'accepted')
      AND  public.audit_finding_tied_to_question_version_v1(
               p_question_version_id,
               ar.target_question_version_id,
               af.metadata
           )
    FOR UPDATE OF af;

    v_blocking_count := public.count_blocking_findings_for_question_version_v1(
        p_question_version_id
    );

    IF v_blocking_count > 0 THEN
        RAISE EXCEPTION
            'publication blocked: % unresolved blocking audit finding(s) for question_version %',
            v_blocking_count, p_question_version_id
            USING ERRCODE = 'invalid_parameter_value',
                  HINT = 'Reject or resolve blocking findings, or wait for review. Accepted findings remain blocking.';
    END IF;

    SELECT COUNT(*),
           SUM(CASE WHEN qov.is_correct THEN 1 ELSE 0 END)
    INTO   v_option_count, v_correct_count
    FROM   public.question_option_versions qov
    WHERE  qov.question_version_id = p_question_version_id;

    IF COALESCE(v_option_count, 0) < 2 THEN
        RAISE EXCEPTION
            'question_version % has fewer than 2 options (found %)',
            p_question_version_id, COALESCE(v_option_count, 0)
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(v_correct_count, 0) <> v_select_count THEN
        RAISE EXCEPTION
            'question_version % has % correct option(s) but select_count is %',
            p_question_version_id, COALESCE(v_correct_count, 0), v_select_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT qve.question_version_id
    INTO   v_prev_published_id
    FROM   public.question_version_events qve
    JOIN   public.question_versions qv
           ON qv.id = qve.question_version_id
    WHERE  qv.question_id = v_question_id
      AND  qve.event_type = 'published'
      AND  qve.question_version_id IS DISTINCT FROM p_question_version_id
    ORDER  BY qve.created_at DESC
    LIMIT  1;

    UPDATE public.questions
    SET    question_text   = v_question_text,
           explanation     = v_explanation,
           category        = v_category,
           difficulty      = v_difficulty,
           cognitive_level = v_cognitive_level,
           concept_key     = v_concept_key,
           question_type   = v_question_type,
           select_count    = v_select_count,
           language_code   = v_language_code,
           content_version = v_version_number
    WHERE  id = v_question_id;

    DELETE FROM public.answer_options
    WHERE  question_id = v_question_id;

    INSERT INTO public.answer_options (
        question_id,
        option_label,
        option_text,
        is_correct,
        display_order
    )
    SELECT v_question_id,
           qov.option_label,
           qov.option_text,
           qov.is_correct,
           qov.display_order
    FROM   public.question_option_versions qov
    WHERE  qov.question_version_id = p_question_version_id
    ORDER  BY qov.display_order ASC;

    INSERT INTO public.question_version_events (
        id,
        question_id,
        question_version_id,
        event_type,
        actor_email,
        reason,
        event_data
    ) VALUES (
        gen_random_uuid(),
        v_question_id,
        p_question_version_id,
        'published',
        p_actor_email,
        p_reason,
        COALESCE(p_event_data, '{}'::jsonb)
    );

    IF v_prev_published_id IS NOT NULL THEN
        INSERT INTO public.question_version_events (
            id,
            question_id,
            question_version_id,
            event_type,
            actor_email,
            reason,
            event_data
        ) VALUES (
            gen_random_uuid(),
            v_question_id,
            v_prev_published_id,
            'superseded',
            p_actor_email,
            'Superseded by version ' || v_version_number::text,
            jsonb_build_object(
                'superseded_by_version_id', p_question_version_id,
                'superseded_by_version_number', v_version_number
            )
        );
    END IF;

    RETURN QUERY
        SELECT p_question_version_id, v_question_id, v_version_number;
END;
$$;


-- =============================================================================
-- 3. complete_audit_run_v1 — advisory lock for publish race
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



-- =============================================================================
-- Privilege hardening
-- =============================================================================

REVOKE ALL ON FUNCTION public.audit_finding_tied_to_question_version_v1(uuid, uuid, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.audit_finding_tied_to_question_version_v1(uuid, uuid, jsonb) FROM anon;
REVOKE EXECUTE ON FUNCTION public.audit_finding_tied_to_question_version_v1(uuid, uuid, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.audit_finding_tied_to_question_version_v1(uuid, uuid, jsonb) TO service_role;

REVOKE ALL ON FUNCTION public.count_blocking_findings_for_question_version_v1(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.count_blocking_findings_for_question_version_v1(uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.count_blocking_findings_for_question_version_v1(uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.count_blocking_findings_for_question_version_v1(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.is_question_version_publishable_v1(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.is_question_version_publishable_v1(uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.is_question_version_publishable_v1(uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.is_question_version_publishable_v1(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.get_question_version_publication_status_v1(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_question_version_publication_status_v1(uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.get_question_version_publication_status_v1(uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_question_version_publication_status_v1(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.publish_question_version_v1(uuid, text, text, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.publish_question_version_v1(uuid, text, text, jsonb) FROM anon;
REVOKE EXECUTE ON FUNCTION public.publish_question_version_v1(uuid, text, text, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.publish_question_version_v1(uuid, text, text, jsonb) TO service_role;

REVOKE ALL ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) FROM anon;
REVOKE EXECUTE ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.complete_audit_run_v1(uuid, jsonb, jsonb) TO service_role;

COMMENT ON FUNCTION public.is_question_version_publishable_v1(uuid) IS
'Returns true when no blocking audit findings in open/accepted status are tied
to the exact question_version_id. Execute permission: service_role only.';

COMMENT ON FUNCTION public.get_question_version_publication_status_v1(uuid) IS
'Returns publishable flag, blocking count, and blocking finding summaries for
admin review UI. Execute permission: service_role only.';

COMMENT ON FUNCTION public.publish_question_version_v1(uuid, text, text, jsonb) IS
'Promotes an approved question version into live tables.  Rejects publication
when unresolved blocking audit findings are tied to the exact version.
Execute permission: service_role only.';
