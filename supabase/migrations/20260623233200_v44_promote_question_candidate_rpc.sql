-- =============================================================================
-- V44 Phase 4B: promote_question_candidate_v1 RPC
-- Created : 2026-06-23 23:32:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds:
--   1. public.question_candidate_events  — append-only event log for the
--      question_candidates lifecycle (created alongside this RPC because it
--      is first consumed here).
--   2. public.promote_question_candidate_v1 — promotes an approved candidate
--      into an immutable question version by calling create_question_version_v1.
--
-- Safety guarantees
-- -----------------
--   * Does NOT publish to live questions or answer_options.
--   * Does NOT modify question_versions or question_option_versions after
--     creation.
--   * Does NOT touch exam_attempts or question_attempts.
--   * Idempotent: re-promoting an already-promoted candidate returns the
--     existing version identity without creating another version or event.
--   * Concurrent calls for the same candidate serialise via FOR UPDATE.
-- =============================================================================


-- =============================================================================
-- 1. question_candidate_events
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.question_candidate_events (
    id           uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id uuid         NOT NULL
                                  REFERENCES public.question_candidates(id),
    event_type   text         NOT NULL,
    actor_email  text,
    reason       text,
    event_data   jsonb        NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT qce_event_type_valid
        CHECK (
            event_type IN (
                'created',
                'audit_requested',
                'audit_failed',
                'review_requested',
                'approved',
                'rejected',
                'promoted',
                'override_applied'
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_qce_candidate_created
    ON public.question_candidate_events (candidate_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_qce_event_type
    ON public.question_candidate_events (event_type);

ALTER TABLE public.question_candidate_events ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.question_candidate_events IS
'Append-only event log for the question_candidates lifecycle.
Never updated or deleted; new events are always inserted.
Service-role / admin access only for this phase.';


-- =============================================================================
-- 2. promote_question_candidate_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.promote_question_candidate_v1(
    p_candidate_id  uuid,
    p_actor_email   text,
    p_reason        text,
    p_event_data    jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    candidate_id         uuid,
    question_version_id  uuid,
    question_id          integer,
    version_number       integer
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    -- Candidate row fields.
    v_candidate_status      text;
    v_target_question_id    integer;
    v_question_text         text;
    v_explanation           text;
    v_category              text;
    v_difficulty            text;
    v_cognitive_level       text;
    v_concept_key           text;
    v_question_type         text;
    v_select_count          integer;
    v_language_code         text;
    v_source_type           text;
    v_candidate_metadata    jsonb;
    v_candidate_payload     jsonb;
    v_promoted_version_id   uuid;

    -- Validated options from candidate_payload.
    v_options               jsonb;
    v_opt                   jsonb;
    v_i                     integer;
    v_label                 text;
    v_opt_text              text;
    v_is_correct            boolean;
    v_display_order         integer;
    v_correct_count         integer   := 0;
    v_label_set             text[]    := '{}';
    v_order_set             integer[] := '{}';

    -- Results from create_question_version_v1.
    v_version_id            uuid;
    v_version_number        integer;

    -- Merged metadata passed to create_question_version_v1.
    v_merged_metadata       jsonb;
BEGIN
    -- -------------------------------------------------------------------------
    -- 1. Require non-empty actor email.
    -- -------------------------------------------------------------------------
    IF COALESCE(TRIM(p_actor_email), '') = '' THEN
        RAISE EXCEPTION 'p_actor_email must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- 2. Load and lock the candidate row.
    --    FOR UPDATE serialises concurrent promotion calls for the same
    --    candidate; the second caller will wait, then take the idempotent path.
    -- -------------------------------------------------------------------------
    SELECT
        c.candidate_status,
        c.target_question_id,
        c.question_text,
        c.explanation,
        c.category,
        c.difficulty,
        c.cognitive_level,
        c.concept_key,
        c.question_type,
        c.select_count,
        c.language_code,
        c.source_type,
        c.metadata,
        c.candidate_payload,
        c.promoted_question_version_id
    INTO
        v_candidate_status,
        v_target_question_id,
        v_question_text,
        v_explanation,
        v_category,
        v_difficulty,
        v_cognitive_level,
        v_concept_key,
        v_question_type,
        v_select_count,
        v_language_code,
        v_source_type,
        v_candidate_metadata,
        v_candidate_payload,
        v_promoted_version_id
    FROM public.question_candidates c
    WHERE c.id = p_candidate_id
    FOR UPDATE;

    -- -------------------------------------------------------------------------
    -- 3. Candidate must exist.
    -- -------------------------------------------------------------------------
    IF NOT FOUND THEN
        RAISE EXCEPTION 'candidate not found: %', p_candidate_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- -------------------------------------------------------------------------
    -- Idempotency: if already promoted, return the existing version identity
    -- without creating another version, updating the candidate, or inserting
    -- a duplicate event.
    -- -------------------------------------------------------------------------
    IF v_candidate_status = 'promoted' AND v_promoted_version_id IS NOT NULL THEN
        RETURN QUERY
            SELECT p_candidate_id,
                   v_promoted_version_id,
                   qv.question_id,
                   qv.version_number
            FROM   public.question_versions qv
            WHERE  qv.id = v_promoted_version_id;
        RETURN;
    END IF;

    -- -------------------------------------------------------------------------
    -- 4. Candidate status must be approved.
    -- -------------------------------------------------------------------------
    IF v_candidate_status <> 'approved' THEN
        RAISE EXCEPTION
            'candidate % has status %, must be approved before promotion',
            p_candidate_id, v_candidate_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- 5. Candidate must not have a stale promoted_question_version_id.
    --    (Guards against a data inconsistency where status ≠ promoted but
    --    version_id is already set.)
    -- -------------------------------------------------------------------------
    IF v_promoted_version_id IS NOT NULL THEN
        RAISE EXCEPTION
            'candidate % has promoted_question_version_id set without promoted status',
            p_candidate_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- 6. Candidate must have a non-null target_question_id.
    -- -------------------------------------------------------------------------
    IF v_target_question_id IS NULL THEN
        RAISE EXCEPTION
            'candidate % has null target_question_id; a target is required for promotion',
            p_candidate_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- 7. Confirm the target live question exists.
    -- -------------------------------------------------------------------------
    PERFORM 1 FROM public.questions WHERE id = v_target_question_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'target question not found: id=%', v_target_question_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- -------------------------------------------------------------------------
    -- 8. Extract options from candidate_payload -> 'options'.
    -- -------------------------------------------------------------------------
    v_options := v_candidate_payload -> 'options';

    -- -------------------------------------------------------------------------
    -- 9. Validate options array.
    -- -------------------------------------------------------------------------
    IF jsonb_typeof(v_options) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION
            'candidate_payload.options must be a JSON array for candidate %',
            p_candidate_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_array_length(v_options) < 2 THEN
        RAISE EXCEPTION 'candidate % has fewer than 2 options (found %)',
            p_candidate_id, jsonb_array_length(v_options)
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    FOR v_i IN 0 .. jsonb_array_length(v_options) - 1 LOOP
        v_opt      := v_options -> v_i;
        v_label    := TRIM(v_opt ->> 'option_label');
        v_opt_text := TRIM(v_opt ->> 'option_text');

        BEGIN
            v_is_correct    := (v_opt ->> 'is_correct')::boolean;
            v_display_order := (v_opt ->> 'display_order')::integer;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION
                'option % in candidate % has invalid is_correct or display_order',
                v_i, p_candidate_id
                USING ERRCODE = 'invalid_parameter_value';
        END;

        IF COALESCE(v_label, '') = '' THEN
            RAISE EXCEPTION
                'option % in candidate % has empty or missing option_label',
                v_i, p_candidate_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF COALESCE(v_opt_text, '') = '' THEN
            RAISE EXCEPTION
                'option % (label=%) in candidate % has empty or missing option_text',
                v_i, v_label, p_candidate_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF COALESCE(v_display_order, 0) <= 0 THEN
            RAISE EXCEPTION
                'option % (label=%) in candidate % has invalid display_order: %, must be > 0',
                v_i, v_label, p_candidate_id, v_display_order
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_label = ANY(v_label_set) THEN
            RAISE EXCEPTION 'duplicate option_label in candidate %: %',
                p_candidate_id, v_label
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        v_label_set := v_label_set || v_label;

        IF v_display_order = ANY(v_order_set) THEN
            RAISE EXCEPTION 'duplicate display_order in candidate %: %',
                p_candidate_id, v_display_order
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        v_order_set := v_order_set || v_display_order;

        IF v_is_correct THEN
            v_correct_count := v_correct_count + 1;
        END IF;
    END LOOP;

    IF v_correct_count <> v_select_count THEN
        RAISE EXCEPTION
            'candidate % has % correct option(s) but select_count is %',
            p_candidate_id, v_correct_count, v_select_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- 10. Create the immutable version by calling create_question_version_v1.
    --     Metadata is the candidate's metadata merged with the supplied
    --     event_data (event_data keys override candidate keys on conflict).
    -- -------------------------------------------------------------------------
    v_merged_metadata :=
        COALESCE(v_candidate_metadata, '{}'::jsonb)
        || COALESCE(p_event_data, '{}'::jsonb);

    SELECT qv.question_version_id,
           qv.version_number
    INTO   v_version_id,
           v_version_number
    FROM   public.create_question_version_v1(
        p_question_id     => v_target_question_id,
        p_question_text   => v_question_text,
        p_explanation     => v_explanation,
        p_category        => v_category,
        p_difficulty      => v_difficulty,
        p_cognitive_level => v_cognitive_level,
        p_concept_key     => v_concept_key,
        p_question_type   => v_question_type,
        p_select_count    => v_select_count,
        p_language_code   => v_language_code,
        p_source_type     => v_source_type,
        p_created_by      => p_actor_email,
        p_metadata        => v_merged_metadata,
        p_options         => v_options
    ) AS qv;

    -- -------------------------------------------------------------------------
    -- 11. Update the candidate row only.
    --     question_versions and question_option_versions are not touched here.
    -- -------------------------------------------------------------------------
    UPDATE public.question_candidates
    SET    candidate_status             = 'promoted',
           promoted_question_version_id = v_version_id,
           updated_at                  = now()
    WHERE  id = p_candidate_id;

    -- -------------------------------------------------------------------------
    -- 12. Insert promoted event into question_candidate_events.
    -- -------------------------------------------------------------------------
    INSERT INTO public.question_candidate_events (
        id,
        candidate_id,
        event_type,
        actor_email,
        reason,
        event_data
    ) VALUES (
        gen_random_uuid(),
        p_candidate_id,
        'promoted',
        p_actor_email,
        p_reason,
        jsonb_build_object(
            'question_version_id', v_version_id,
            'question_id',         v_target_question_id,
            'version_number',      v_version_number
        ) || COALESCE(p_event_data, '{}'::jsonb)
    );

    RETURN QUERY
        SELECT p_candidate_id, v_version_id, v_target_question_id, v_version_number;
END;
$$;


-- =============================================================================
-- Privilege hardening
-- =============================================================================

REVOKE ALL ON FUNCTION public.promote_question_candidate_v1(
    uuid, text, text, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.promote_question_candidate_v1(
    uuid, text, text, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.promote_question_candidate_v1(
    uuid, text, text, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.promote_question_candidate_v1(
    uuid, text, text, jsonb
) TO service_role;

COMMENT ON FUNCTION public.promote_question_candidate_v1(uuid, text, text, jsonb) IS
'Promotes an approved question_candidate into an immutable question_version by
calling create_question_version_v1.  Does not publish to live questions or
answer_options.  Idempotent: re-promoting an already-promoted candidate returns
the existing version identity without creating a duplicate.  Concurrent calls
for the same candidate serialise via FOR UPDATE on the candidate row.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';
