-- =============================================================================
-- V44 Phase 3A: create_question_version_v1 RPC
-- Created : 2026-06-23 19:19:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds one PostgreSQL function that creates a new immutable question version
-- and its option versions in a single atomic call.
--
-- The function does NOT modify public.questions, public.answer_options,
-- public.exam_attempts, or public.question_attempts.
-- Publishing logic is deferred to Phase 3B.
--
-- Security
-- --------
-- EXECUTE is revoked from PUBLIC immediately after creation.
-- This function is intended for service-role and admin-role callers only.
-- Service-role connections bypass RLS on the version tables; no additional
-- policies are required for this phase.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Function: public.create_question_version_v1
-- ---------------------------------------------------------------------------

-- Service-role / admin execution only.
-- Do not grant to anon or authenticated roles.

CREATE OR REPLACE FUNCTION public.create_question_version_v1(
    p_question_id     integer,
    p_question_text   text,
    p_explanation     text,
    p_category        text,
    p_difficulty      text,
    p_cognitive_level text,
    p_concept_key     text,
    p_question_type   text,
    p_select_count    integer,
    p_language_code   text,
    p_source_type     text,
    p_created_by      text,
    p_metadata        jsonb,
    p_options         jsonb
)
RETURNS TABLE (question_version_id uuid, version_number integer)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_version_id     uuid;
    v_version_number integer;
    v_content_hash   text;
    v_opt            jsonb;
    v_label          text;
    v_opt_text       text;
    v_is_correct     boolean;
    v_display_order  integer;
    v_correct_count  integer  := 0;
    v_label_set      text[]   := '{}';
    v_order_set      integer[] := '{}';
    v_i              integer;
BEGIN
    -- -------------------------------------------------------------------------
    -- 1. Confirm question exists.
    --    SELECT FOR UPDATE locks this question's row for the duration of the
    --    transaction, serialising concurrent version-number creation for the
    --    same question_id.  Calls for different question_ids proceed in
    --    parallel without contention.
    -- -------------------------------------------------------------------------
    PERFORM 1
    FROM public.questions
    WHERE id = p_question_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'question not found: id=%', p_question_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- -------------------------------------------------------------------------
    -- 2. Calculate next version number.
    --    The FOR UPDATE lock above ensures no concurrent call can read the
    --    same MAX and generate the same version number.
    -- -------------------------------------------------------------------------
    SELECT COALESCE(MAX(qv.version_number), 0) + 1
    INTO   v_version_number
    FROM   public.question_versions qv
    WHERE  qv.question_id = p_question_id;

    -- -------------------------------------------------------------------------
    -- 3. Validate question fields.
    -- -------------------------------------------------------------------------
    IF COALESCE(TRIM(p_question_text), '') = '' THEN
        RAISE EXCEPTION 'question_text must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_question_type NOT IN ('single', 'multiple') THEN
        RAISE EXCEPTION 'question_type must be single or multiple, got: %', p_question_type
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_difficulty IS NOT NULL
       AND p_difficulty NOT IN ('easy', 'medium', 'hard') THEN
        RAISE EXCEPTION 'difficulty must be easy, medium, or hard when provided, got: %', p_difficulty
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_cognitive_level IS NOT NULL
       AND p_cognitive_level NOT IN (
           'recall', 'understanding', 'application', 'analysis', 'judgment'
       ) THEN
        RAISE EXCEPTION 'cognitive_level invalid when provided, got: %', p_cognitive_level
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(p_select_count, 0) <= 0 THEN
        RAISE EXCEPTION 'select_count must be > 0, got: %', p_select_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- 4. Validate options array.
    -- -------------------------------------------------------------------------
    IF jsonb_typeof(p_options) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_options must be a JSON array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_array_length(p_options) < 2 THEN
        RAISE EXCEPTION 'p_options must contain at least 2 options, got: %',
            jsonb_array_length(p_options)
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Iterate options: validate each field, accumulate correct-answer count.
    FOR v_i IN 0 .. jsonb_array_length(p_options) - 1 LOOP
        v_opt           := p_options -> v_i;
        v_label         := TRIM(v_opt ->> 'option_label');
        v_opt_text      := TRIM(v_opt ->> 'option_text');

        -- Cast failures here surface as PostgreSQL type errors,
        -- giving the caller an informative message.
        BEGIN
            v_is_correct    := (v_opt ->> 'is_correct')::boolean;
            v_display_order := (v_opt ->> 'display_order')::integer;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'option % has invalid is_correct or display_order: %',
                v_i, v_opt
                USING ERRCODE = 'invalid_parameter_value';
        END;

        IF COALESCE(v_label, '') = '' THEN
            RAISE EXCEPTION 'option % has empty or missing option_label', v_i
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF COALESCE(v_opt_text, '') = '' THEN
            RAISE EXCEPTION 'option % (label=%) has empty or missing option_text',
                v_i, v_label
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF COALESCE(v_display_order, 0) <= 0 THEN
            RAISE EXCEPTION 'option % (label=%) has invalid display_order: %, must be > 0',
                v_i, v_label, v_display_order
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- Duplicate option_label check.
        IF v_label = ANY(v_label_set) THEN
            RAISE EXCEPTION 'duplicate option_label in p_options: %', v_label
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        v_label_set := v_label_set || v_label;

        -- Duplicate display_order check.
        IF v_display_order = ANY(v_order_set) THEN
            RAISE EXCEPTION 'duplicate display_order in p_options: %', v_display_order
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        v_order_set := v_order_set || v_display_order;

        IF v_is_correct THEN
            v_correct_count := v_correct_count + 1;
        END IF;
    END LOOP;

    -- Correct-answer count must exactly equal select_count.
    IF v_correct_count <> p_select_count THEN
        RAISE EXCEPTION
            'exactly p_select_count (%) options must have is_correct = true, found: %',
            p_select_count, v_correct_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- 5. Generate deterministic content_hash.
    --    Same separator scheme as the Phase 2 backfill migration:
    --      SOH (x01) between question fields
    --      STX (x02) between fields within one option
    --      ETX (x03) between option rows
    --    Options sorted by display_order ASC then option_label ASC to match
    --    the ordering used in the backfill migration.
    -- -------------------------------------------------------------------------
    SELECT md5(
        COALESCE(p_question_text,   '') || E'\x01' ||
        COALESCE(p_explanation,     '') || E'\x01' ||
        COALESCE(p_category,        '') || E'\x01' ||
        COALESCE(p_difficulty,      '') || E'\x01' ||
        COALESCE(p_question_type,   '') || E'\x01' ||
        p_select_count::text            || E'\x01' ||
        COALESCE(p_language_code,   '') || E'\x01' ||
        COALESCE(p_cognitive_level, '') || E'\x01' ||
        COALESCE(p_concept_key,     '') || E'\x01' ||
        COALESCE(
            (
                SELECT string_agg(
                    (elem ->> 'option_label') || E'\x02' ||
                    (elem ->> 'option_text')  || E'\x02' ||
                    (elem ->> 'is_correct'),
                    E'\x03'
                    ORDER BY (elem ->> 'display_order')::integer ASC,
                             (elem ->> 'option_label')           ASC
                )
                FROM jsonb_array_elements(p_options) AS elem
            ),
            ''
        )
    ) INTO v_content_hash;

    -- -------------------------------------------------------------------------
    -- 6. Insert question_versions row.
    -- -------------------------------------------------------------------------
    v_version_id := gen_random_uuid();

    INSERT INTO public.question_versions (
        id,
        question_id,
        version_number,
        question_text,
        explanation,
        category,
        difficulty,
        cognitive_level,
        concept_key,
        question_type,
        select_count,
        language_code,
        content_hash,
        source_type,
        created_by,
        metadata
    ) VALUES (
        v_version_id,
        p_question_id,
        v_version_number,
        p_question_text,
        p_explanation,
        p_category,
        p_difficulty,
        p_cognitive_level,
        p_concept_key,
        p_question_type,
        p_select_count,
        p_language_code,
        v_content_hash,
        p_source_type,
        p_created_by,
        COALESCE(p_metadata, '{}'::jsonb)
    );

    -- -------------------------------------------------------------------------
    -- 7. Insert option_versions rows.
    --    Each row is inserted from the validated, already-dereferenced option
    --    values rather than re-parsing the jsonb.
    -- -------------------------------------------------------------------------
    FOR v_i IN 0 .. jsonb_array_length(p_options) - 1 LOOP
        v_opt := p_options -> v_i;
        INSERT INTO public.question_option_versions (
            id,
            question_version_id,
            option_label,
            option_text,
            is_correct,
            display_order
        ) VALUES (
            gen_random_uuid(),
            v_version_id,
            TRIM(v_opt ->> 'option_label'),
            TRIM(v_opt ->> 'option_text'),
            (v_opt ->> 'is_correct')::boolean,
            (v_opt ->> 'display_order')::integer
        );
    END LOOP;

    -- -------------------------------------------------------------------------
    -- 8. Insert governance event.
    -- -------------------------------------------------------------------------
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
        p_question_id,
        v_version_id,
        'created',
        p_created_by,
        'New immutable question version created',
        jsonb_build_object(
            'version_number', v_version_number,
            'source_type',    p_source_type
        )
    );

    -- -------------------------------------------------------------------------
    -- 9. Return new version ID and version number.
    -- -------------------------------------------------------------------------
    RETURN QUERY SELECT v_version_id, v_version_number;
END;
$$;

-- ---------------------------------------------------------------------------
-- Privilege hardening
--
-- PostgreSQL grants EXECUTE to PUBLIC by default when a function is created.
-- Strip that grant, then remove from the two Supabase application roles,
-- and restrict execution to service_role only.
-- ---------------------------------------------------------------------------

REVOKE ALL ON FUNCTION public.create_question_version_v1(
    integer, text, text, text, text, text, text, text, integer,
    text, text, text, jsonb, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.create_question_version_v1(
    integer, text, text, text, text, text, text, text, integer,
    text, text, text, jsonb, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.create_question_version_v1(
    integer, text, text, text, text, text, text, text, integer,
    text, text, text, jsonb, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.create_question_version_v1(
    integer, text, text, text, text, text, text, text, integer,
    text, text, text, jsonb, jsonb
) TO service_role;

COMMENT ON FUNCTION public.create_question_version_v1(
    integer, text, text, text, text, text, text, text, integer,
    text, text, text, jsonb, jsonb
) IS
'Creates one immutable question version and its option versions atomically.
Reads questions row with FOR UPDATE to serialise concurrent version-number
creation for the same question.  Does not modify questions, answer_options,
exam_attempts, or question_attempts.
Execute permission: service_role only.
PUBLIC, anon, and authenticated are explicitly revoked.
Publishing to live tables is deferred to Phase 3B.';
