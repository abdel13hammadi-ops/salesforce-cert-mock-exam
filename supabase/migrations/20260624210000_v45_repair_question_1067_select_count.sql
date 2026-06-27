-- =============================================================================
-- V45 production-data repair: question 1067 multi-select answer key
-- Created : 2026-06-24 21:00:00 UTC
--
-- Purpose
-- -------
-- Repair verified corrupted live question 1067 where select_count = 4 while the
-- stem requires Select TWO and only options B and C are correct per the
-- explanation.  Appends immutable version 2; does not mutate version 1.
--
-- Safety
-- ------
--   * Aborts unless live rows and version 1 match verified production exactly
--   * Idempotent when version 2 and the live repair already exist
--   * One-time helper function is dropped after invocation
-- =============================================================================

CREATE OR REPLACE FUNCTION public.repair_question_1067_answer_key_v1()
RETURNS TABLE (
    question_id         integer,
    version_number      integer,
    question_version_id uuid,
    repair_status       text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    c_question_id   constant integer := 1067;
    c_option_ids    constant integer[] := ARRAY[4354, 4355, 4356, 4357, 4358];
    c_option_labels constant text[] := ARRAY['A', 'B', 'C', 'D', 'E'];
    c_option_orders constant integer[] := ARRAY[1, 2, 3, 4, 5];
    c_option_texts  constant text[] := ARRAY[
        $txt$The external third-party marketing agency's graphic design intern.$txt$,
        $txt$The Data Privacy and Compliance Officer responsible for health information regulations.$txt$,
        $txt$A senior representative from the clinical intake nursing team who executes the daily workflow.$txt$,
        $txt$The junior database developer who manages legacy archived backups.$txt$,
        $txt$The hardware technician who manages the corporate laptop inventory.$txt$
    ];
    c_option_correct constant boolean[] := ARRAY[true, true, true, true, false];
    c_expected_question_text constant text := $txt$A Salesforce BA is planning a requirements elicitation workshop for a healthcare client. The project involves highly sensitive patient intake workflows. Which two stakeholders must the BA ensure are included as core active contributors in these sessions? (Select TWO)$txt$;
    c_expected_explanation constant text := $txt$For a sensitive healthcare project, the BA must include the operational experts who execute the daily workflow (C) to ensure functional accuracy, and the compliance officer (B) to ensure the proposed process adheres to data privacy laws. Options A, D, and E represent ancillary or technical roles that do not own or execute the patient intake business process.$txt$;

    v_q                     public.questions%ROWTYPE;
    v_v1_id                 uuid;
    v_v1_hash               text;
    v_v1_question_text      text;
    v_v1_explanation        text;
    v_v1_select_count       integer;
    v_v2_id                 uuid;
    v_v2_exists             boolean := false;
    v_live_correct_count    integer;
    v_live_correct_labels   text[];
    v_live_option_count     integer;
    v_v1_option_count       integer;
    v_content_hash          text;
    v_idx                   integer;
BEGIN
    SELECT q.*
    INTO   v_q
    FROM   public.questions AS q
    WHERE  q.id = c_question_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'repair precondition failed: question % not found', c_question_id
            USING ERRCODE = 'no_data_found';
    END IF;

    SELECT qv.id,
           qv.content_hash,
           qv.question_text,
           qv.explanation,
           qv.select_count
    INTO   v_v1_id,
           v_v1_hash,
           v_v1_question_text,
           v_v1_explanation,
           v_v1_select_count
    FROM   public.question_versions qv
    WHERE  qv.question_id = c_question_id
      AND  qv.version_number = 1;

    IF v_v1_id IS NULL THEN
        RAISE EXCEPTION 'repair precondition failed: question % is missing version 1', c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM   public.question_versions qv
        WHERE  qv.question_id = c_question_id
          AND  qv.version_number > 2
    ) THEN
        RAISE EXCEPTION 'repair precondition failed: question % has unexpected version > 2', c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM   public.question_versions qv
        WHERE  qv.question_id = c_question_id
          AND  qv.version_number = 2
          AND  qv.select_count = 2
    )
    INTO v_v2_exists;

    SELECT COUNT(*)
    INTO   v_live_option_count
    FROM   public.answer_options ao
    WHERE  ao.question_id = c_question_id;

    IF v_live_option_count <> 5 THEN
        RAISE EXCEPTION
            'repair precondition failed: question % must have exactly 5 live answer_options, found %',
            c_question_id, v_live_option_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM   public.answer_options ao
        WHERE  ao.question_id = c_question_id
          AND  ao.id <> ALL (c_option_ids)
    ) THEN
        RAISE EXCEPTION
            'repair precondition failed: question % live answer_options must be exactly ids %',
            c_question_id, c_option_ids
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    FOR v_idx IN 1 .. array_length(c_option_ids, 1) LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM   public.answer_options ao
            WHERE  ao.question_id = c_question_id
              AND  ao.id = c_option_ids[v_idx]
              AND  ao.option_label = c_option_labels[v_idx]
              AND  ao.option_text = c_option_texts[v_idx]
              AND  ao.display_order = c_option_orders[v_idx]
              AND  ao.is_correct IS NOT DISTINCT FROM c_option_correct[v_idx]
        ) THEN
            RAISE EXCEPTION
                'repair precondition failed: question % live option % (id %, label %, order %) must match verified text, order, and corrupted correctness',
                c_question_id,
                v_idx,
                c_option_ids[v_idx],
                c_option_labels[v_idx],
                c_option_orders[v_idx]
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END LOOP;

    SELECT COUNT(*)
    INTO   v_v1_option_count
    FROM   public.question_option_versions qov
    WHERE  qov.question_version_id = v_v1_id;

    IF v_v1_option_count <> 5 THEN
        RAISE EXCEPTION
            'repair precondition failed: question % version 1 must contain exactly 5 option snapshots, found %',
            c_question_id, v_v1_option_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_v1_select_count <> 4 THEN
        RAISE EXCEPTION
            'repair precondition failed: question % version 1 select_count must be 4, got %',
            c_question_id, v_v1_select_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_q.question_text IS DISTINCT FROM c_expected_question_text THEN
        RAISE EXCEPTION
            'repair precondition failed: question % live stem must match verified production text',
            c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_q.explanation IS DISTINCT FROM c_expected_explanation THEN
        RAISE EXCEPTION
            'repair precondition failed: question % live explanation must match verified production text',
            c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_v1_question_text IS DISTINCT FROM c_expected_question_text THEN
        RAISE EXCEPTION
            'repair precondition failed: question % version 1 stem must match verified production text',
            c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_v1_explanation IS DISTINCT FROM c_expected_explanation THEN
        RAISE EXCEPTION
            'repair precondition failed: question % version 1 explanation must match verified production text',
            c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_v1_question_text IS DISTINCT FROM v_q.question_text THEN
        RAISE EXCEPTION
            'repair precondition failed: question % live stem must match immutable version 1 snapshot',
            c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_v1_explanation IS DISTINCT FROM v_q.explanation THEN
        RAISE EXCEPTION
            'repair precondition failed: question % live explanation must match immutable version 1 snapshot',
            c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    FOR v_idx IN 1 .. array_length(c_option_ids, 1) LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM   public.question_option_versions qov
            WHERE  qov.question_version_id = v_v1_id
              AND  qov.option_label = c_option_labels[v_idx]
              AND  qov.option_text = c_option_texts[v_idx]
              AND  qov.display_order = c_option_orders[v_idx]
              AND  qov.is_correct IS NOT DISTINCT FROM c_option_correct[v_idx]
        ) THEN
            RAISE EXCEPTION
                'repair precondition failed: question % version 1 option % (label %, order %) must match verified corrupted snapshot',
                c_question_id,
                v_idx,
                c_option_labels[v_idx],
                c_option_orders[v_idx]
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END LOOP;

    SELECT COUNT(*) FILTER (WHERE ao.is_correct)
    INTO   v_live_correct_count
    FROM   public.answer_options ao
    WHERE  ao.question_id = c_question_id;

    SELECT ARRAY_AGG(
               ao.option_label::text
               ORDER BY ao.display_order, ao.option_label
           )
    INTO   v_live_correct_labels
    FROM   public.answer_options AS ao
    WHERE  ao.question_id = c_question_id
      AND  ao.is_correct;

    IF v_q.select_count = 2
       AND v_live_correct_count = 2
       AND v_live_correct_labels = ARRAY['B', 'C']::text[] THEN
        IF v_v2_exists THEN
            SELECT qv.id
            INTO   v_v2_id
            FROM   public.question_versions qv
            WHERE  qv.question_id = c_question_id
              AND  qv.version_number = 2;

            RETURN QUERY
            SELECT c_question_id, 2, v_v2_id, 'already_repaired'::text;
            RETURN;
        END IF;

        RAISE EXCEPTION
            'repair blocked: live question % already corrected but immutable version 2 is missing',
            c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_q.question_type <> 'multiple' THEN
        RAISE EXCEPTION
            'repair precondition failed: question % question_type must be multiple, got %',
            c_question_id, v_q.question_type
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_q.select_count <> 4 THEN
        RAISE EXCEPTION
            'repair precondition failed: question % select_count must be 4 before repair, got %',
            c_question_id, v_q.select_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(v_q.is_active, false) IS NOT TRUE THEN
        RAISE EXCEPTION
            'repair precondition failed: question % must remain active',
            c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(v_q.quality_status, '') <> 'approved' THEN
        RAISE EXCEPTION
            'repair precondition failed: question % quality_status must be approved, got %',
            c_question_id, v_q.quality_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_v2_exists THEN
        RAISE EXCEPTION
            'repair blocked: question % already has version 2 but live rows remain corrupted',
            c_question_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    UPDATE public.questions AS q
    SET    select_count    = 2,
           content_version = 2
    WHERE  q.id = c_question_id;

    UPDATE public.answer_options AS ao
    SET    is_correct = CASE
               WHEN ao.id IN (4355, 4356) THEN TRUE
               ELSE FALSE
           END
    WHERE  ao.question_id = c_question_id
      AND  ao.id = ANY (c_option_ids);

    SELECT q.*
    INTO   v_q
    FROM   public.questions q
    WHERE  q.id = c_question_id;

    SELECT md5(
        COALESCE(v_q.question_text,   '') || E'\x01' ||
        COALESCE(v_q.explanation,     '') || E'\x01' ||
        COALESCE(v_q.category,        '') || E'\x01' ||
        COALESCE(v_q.difficulty,      '') || E'\x01' ||
        COALESCE(v_q.question_type,   '') || E'\x01' ||
        v_q.select_count::text            || E'\x01' ||
        COALESCE(v_q.language_code,   '') || E'\x01' ||
        COALESCE(v_q.cognitive_level, '') || E'\x01' ||
        COALESCE(v_q.concept_key,     '') || E'\x01' ||
        COALESCE(
            (
                SELECT string_agg(
                    ao.option_label || E'\x02' ||
                    ao.option_text  || E'\x02' ||
                    ao.is_correct::text,
                    E'\x03'
                    ORDER BY ao.display_order ASC, ao.option_label ASC
                )
                FROM public.answer_options ao
                WHERE ao.question_id = c_question_id
            ),
            ''
        )
    )
    INTO v_content_hash;

    v_v2_id := gen_random_uuid();

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
        supersedes_version_id,
        metadata
    ) VALUES (
        v_v2_id,
        c_question_id,
        2,
        v_q.question_text,
        v_q.explanation,
        v_q.category,
        v_q.difficulty,
        v_q.cognitive_level,
        v_q.concept_key,
        v_q.question_type,
        2,
        v_q.language_code,
        v_content_hash,
        'production_data_repair',
        'system:v45_repair_question_1067',
        v_v1_id,
        jsonb_build_object(
            'repair_migration', '20260624210000_v45_repair_question_1067_select_count',
            'prior_version_number', 1,
            'prior_version_id', v_v1_id,
            'prior_content_hash', v_v1_hash,
            'correct_option_labels', ARRAY['B', 'C'],
            'correct_option_ids', ARRAY[4355, 4356]
        )
    );

    INSERT INTO public.question_option_versions (
        id,
        question_version_id,
        option_label,
        option_text,
        is_correct,
        display_order
    )
    SELECT
        gen_random_uuid(),
        v_v2_id,
        ao.option_label,
        ao.option_text,
        ao.is_correct,
        ao.display_order
    FROM public.answer_options ao
    WHERE ao.question_id = c_question_id
    ORDER BY ao.display_order ASC, ao.option_label ASC;

    IF (SELECT COUNT(*) FROM public.question_option_versions qov WHERE qov.question_version_id = v_v2_id) <> 5 THEN
        RAISE EXCEPTION 'repair failed: version 2 must contain exactly 5 option snapshots'
            USING ERRCODE = 'data_exception';
    END IF;

    IF (SELECT COUNT(*) FROM public.question_option_versions qov WHERE qov.question_version_id = v_v2_id AND qov.is_correct) <> 2 THEN
        RAISE EXCEPTION 'repair failed: version 2 must contain exactly 2 correct options'
            USING ERRCODE = 'data_exception';
    END IF;

    INSERT INTO public.question_version_events (
        id, question_id, question_version_id, event_type, actor_email, reason, event_data
    )
    SELECT
        gen_random_uuid(), c_question_id, v_v2_id, ev.event_type, ev.actor_email, ev.reason, ev.event_data
    FROM (
        VALUES
            (
                'created'::text,
                'system:v45_repair_question_1067'::text,
                'Corrected immutable version 2 appended for select_count repair'::text,
                jsonb_build_object('version_number', 2, 'source_type', 'production_data_repair')
            ),
            (
                'approved'::text,
                'system:v45_repair_question_1067'::text,
                'Approved corrected answer key for question 1067'::text,
                jsonb_build_object('version_number', 2, 'repair_reason', 'select_count_mismatch')
            ),
            (
                'published'::text,
                'system:v45_repair_question_1067'::text,
                'Published corrected answer key to live question 1067'::text,
                jsonb_build_object('version_number', 2, 'live_select_count', 2)
            ),
            (
                'override_applied'::text,
                'system:v45_repair_question_1067'::text,
                'Production data repair applied for contradictory multi-select answer key'::text,
                jsonb_build_object(
                    'migration', '20260624210000_v45_repair_question_1067_select_count',
                    'previous_select_count', 4,
                    'new_select_count', 2,
                    'correct_option_labels', ARRAY['B', 'C']
                )
            )
    ) AS ev(event_type, actor_email, reason, event_data)
    WHERE NOT EXISTS (
        SELECT 1
        FROM   public.question_version_events qve
        WHERE  qve.question_version_id = v_v2_id
          AND  qve.event_type = ev.event_type
          AND  qve.actor_email = ev.actor_email
    );

    IF NOT EXISTS (
        SELECT 1
        FROM   public.question_version_events qve
        WHERE  qve.question_version_id = v_v1_id
          AND  qve.event_type = 'superseded'
    ) THEN
        INSERT INTO public.question_version_events (
            id, question_id, question_version_id, event_type, actor_email, reason, event_data
        ) VALUES (
            gen_random_uuid(),
            c_question_id,
            v_v1_id,
            'superseded',
            'system:v45_repair_question_1067',
            'Superseded by corrected version 2',
            jsonb_build_object(
                'superseded_by_version_id', v_v2_id,
                'superseded_by_version_number', 2,
                'repair_migration', '20260624210000_v45_repair_question_1067_select_count'
            )
        );
    END IF;

    IF (SELECT q.select_count FROM public.questions AS q WHERE q.id = c_question_id) <> 2 THEN
        RAISE EXCEPTION 'repair verification failed: live select_count is not 2'
            USING ERRCODE = 'data_exception';
    END IF;

    IF (
        SELECT ARRAY_AGG(
                   ao.option_label::text
                   ORDER BY ao.display_order, ao.option_label
               )
        FROM   public.answer_options AS ao
        WHERE  ao.question_id = c_question_id
          AND  ao.is_correct
    ) IS DISTINCT FROM ARRAY['B', 'C']::text[] THEN
        RAISE EXCEPTION 'repair verification failed: live correct options are not B and C'
            USING ERRCODE = 'data_exception';
    END IF;

    RETURN QUERY
    SELECT c_question_id, 2, v_v2_id, 'repaired'::text;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM   public.questions AS q
        WHERE  q.id = 1067
    ) THEN
        RAISE NOTICE 'repair_question_1067_answer_key_v1 skipped: question 1067 not present in this database';
    ELSE
        PERFORM public.repair_question_1067_answer_key_v1();
    END IF;
END;
$$;

DROP FUNCTION IF EXISTS public.repair_question_1067_answer_key_v1();
