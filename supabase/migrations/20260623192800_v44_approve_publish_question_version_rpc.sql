-- =============================================================================
-- V44 Phase 3B: approve_question_version_v1 and publish_question_version_v1
-- Created : 2026-06-23 19:28:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds two RPCs that complete the immutable-version governance cycle:
--
--   approve_question_version_v1  — appends an approved event; never mutates
--                                   question_versions or live tables.
--   publish_question_version_v1  — atomically promotes an approved version
--                                   into the live questions / answer_options
--                                   tables and records a published event.
--
-- Constraints maintained
-- ----------------------
--   * question_versions and question_option_versions are never updated or
--     deleted by either function.
--   * question_attempts and exam_attempts are never touched.
--   * Publishing requires a prior approved event for the exact version.
--   * Publishing an older version after a newer one is rejected.
--   * Both functions are idempotent for their primary event.
--
-- Security
-- --------
--   EXECUTE is revoked from PUBLIC, anon, and authenticated.
--   service_role is the only granted caller.
-- =============================================================================


-- =============================================================================
-- 1. approve_question_version_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.approve_question_version_v1(
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
    v_question_id    integer;
    v_version_number integer;
BEGIN
    -- Require non-empty actor email.
    IF COALESCE(TRIM(p_actor_email), '') = '' THEN
        RAISE EXCEPTION 'p_actor_email must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Load and lock the version row.  FOR UPDATE serialises concurrent approval
    -- or publish calls that target the same version.
    SELECT qv.question_id, qv.version_number
    INTO   v_question_id, v_version_number
    FROM   public.question_versions qv
    WHERE  qv.id = p_question_version_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'question_version not found: %', p_question_version_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Idempotency: if an approved event already exists for this version,
    -- return the version identity without inserting a duplicate event.
    IF EXISTS (
        SELECT 1
        FROM   public.question_version_events
        WHERE  question_version_id = p_question_version_id
          AND  event_type          = 'approved'
        LIMIT 1
    ) THEN
        RETURN QUERY
            SELECT p_question_version_id, v_question_id, v_version_number;
        RETURN;
    END IF;

    -- Insert append-only approved event.
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
        'approved',
        p_actor_email,
        p_reason,
        COALESCE(p_event_data, '{}'::jsonb)
    );

    RETURN QUERY
        SELECT p_question_version_id, v_question_id, v_version_number;
END;
$$;


-- =============================================================================
-- 2. publish_question_version_v1
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
    -- Version fields loaded from question_versions.
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

    -- Counts from question_option_versions.
    v_option_count         integer;
    v_correct_count        integer;

    -- Previously published version for the same question (if any).
    v_prev_published_id    uuid;
    v_max_published_vn     integer;
BEGIN
    -- Require non-empty actor email.
    IF COALESCE(TRIM(p_actor_email), '') = '' THEN
        RAISE EXCEPTION 'p_actor_email must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Load and lock the question_version row.
    -- Locking order: question_versions first, then questions.
    -- Consistent with create_question_version_v1 which locks questions first
    -- but never locks question_versions, so no cycle is possible.
    -- -------------------------------------------------------------------------
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

    -- Lock the live questions row to serialise concurrent publish calls that
    -- target the same question.
    PERFORM 1
    FROM    public.questions
    WHERE   id = v_question_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'live question not found for question_id=%', v_question_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- -------------------------------------------------------------------------
    -- Idempotency: if this exact version already has a published event,
    -- return without re-writing any rows or inserting duplicate events.
    -- -------------------------------------------------------------------------
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

    -- -------------------------------------------------------------------------
    -- Reject publishing an older version after a newer one has been published.
    -- Find the highest version_number that has a published event for this
    -- question, excluding the current version (already confirmed not published).
    -- -------------------------------------------------------------------------
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

    -- -------------------------------------------------------------------------
    -- Require a prior approved event for this exact version.
    -- -------------------------------------------------------------------------
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

    -- -------------------------------------------------------------------------
    -- Validate option set.
    -- -------------------------------------------------------------------------
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

    -- -------------------------------------------------------------------------
    -- Capture the previously published version for this question (if any).
    -- Used below to insert a superseded event.  Only one superseded event is
    -- inserted: for the version with the most recent published event.
    -- -------------------------------------------------------------------------
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

    -- -------------------------------------------------------------------------
    -- Promote the version into the live questions table.
    -- Only the fields listed in the spec are updated; other questions columns
    -- (e.g. is_active, exam_name, external_key) are left unchanged.
    -- -------------------------------------------------------------------------
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

    -- -------------------------------------------------------------------------
    -- Atomically replace live answer_options for this question.
    -- Delete all current options, then insert from the immutable version set.
    -- question_option_versions rows are not touched.
    -- -------------------------------------------------------------------------
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

    -- -------------------------------------------------------------------------
    -- Insert published event for the new version.
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
        v_question_id,
        p_question_version_id,
        'published',
        p_actor_email,
        p_reason,
        COALESCE(p_event_data, '{}'::jsonb)
    );

    -- -------------------------------------------------------------------------
    -- Insert superseded event for the previously published version, if one
    -- exists and is different from the version being published now.
    -- -------------------------------------------------------------------------
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
-- Privilege hardening — approve_question_version_v1
-- =============================================================================

REVOKE ALL ON FUNCTION public.approve_question_version_v1(
    uuid, text, text, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.approve_question_version_v1(
    uuid, text, text, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.approve_question_version_v1(
    uuid, text, text, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.approve_question_version_v1(
    uuid, text, text, jsonb
) TO service_role;

COMMENT ON FUNCTION public.approve_question_version_v1(uuid, text, text, jsonb) IS
'Appends an approved event for a question version.  Idempotent: a second call
for the same version returns the version identity without creating a duplicate
event.  Does not modify question_versions or live questions / answer_options.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';


-- =============================================================================
-- Privilege hardening — publish_question_version_v1
-- =============================================================================

REVOKE ALL ON FUNCTION public.publish_question_version_v1(
    uuid, text, text, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.publish_question_version_v1(
    uuid, text, text, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.publish_question_version_v1(
    uuid, text, text, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.publish_question_version_v1(
    uuid, text, text, jsonb
) TO service_role;

COMMENT ON FUNCTION public.publish_question_version_v1(uuid, text, text, jsonb) IS
'Promotes an approved question version into the live questions and answer_options
tables.  Requires a prior approved event.  Rejects publishing a version whose
version_number is lower than an already-published version for the same question.
Idempotent: re-publishing the same version returns identity without re-writing
rows or inserting duplicate events.  Inserts a superseded event for the
previously published version when one exists.
Does not modify question_versions or question_option_versions.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';
