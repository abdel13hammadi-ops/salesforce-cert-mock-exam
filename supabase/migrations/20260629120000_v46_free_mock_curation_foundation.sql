-- =============================================================================
-- V46 Phase 1: Free-mock curation foundation
-- Created : 2026-06-29 12:00:00 UTC
--
-- Purpose
-- -------
-- Versioned, immutable free-mock set curation separate from live learner
-- runtime (which still uses questions.free_mock_exam until a later phase).
--
-- Tables
--   free_mock_sets        — draft / published / retired set headers
--   free_mock_set_items   — ordered slots (1..15) referencing questions.id
--
-- Guarantees
--   * One draft per certification + language at a time
--   * One published set per certification + language at a time
--   * Published + retired rows are immutable (items locked; headers only retire)
--   * Publish validates atomically; failures leave no partial publish
--
-- Security
--   RLS enabled; service_role-only RPC EXECUTE (application admin gate)
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. free_mock_sets
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.free_mock_sets (
    id               uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    exam_name        text         NOT NULL,
    language_code    text         NOT NULL DEFAULT 'en',
    version_number   integer,
    status           text         NOT NULL DEFAULT 'draft',
    created_at       timestamptz  NOT NULL DEFAULT now(),
    created_by       text         NOT NULL,
    updated_at       timestamptz  NOT NULL DEFAULT now(),
    published_at     timestamptz,
    published_by     text,
    publish_reason   text,
    retired_at       timestamptz,

    CONSTRAINT free_mock_sets_status_valid
        CHECK (status IN ('draft', 'published', 'retired')),

    CONSTRAINT free_mock_sets_language_nonempty
        CHECK (TRIM(language_code) <> ''),

    CONSTRAINT free_mock_sets_created_by_nonempty
        CHECK (TRIM(created_by) <> ''),

    CONSTRAINT free_mock_sets_version_when_not_draft
        CHECK (
            (status = 'draft' AND version_number IS NULL)
            OR (status IN ('published', 'retired') AND version_number IS NOT NULL AND version_number > 0)
        ),

    CONSTRAINT free_mock_sets_publish_metadata
        CHECK (
            status <> 'published'
            OR (
                published_at IS NOT NULL
                AND published_by IS NOT NULL
                AND TRIM(published_by) <> ''
                AND publish_reason IS NOT NULL
                AND TRIM(publish_reason) <> ''
            )
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_free_mock_sets_version
    ON public.free_mock_sets (exam_name, language_code, version_number)
    WHERE version_number IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_free_mock_sets_one_draft
    ON public.free_mock_sets (exam_name, language_code)
    WHERE status = 'draft';

CREATE UNIQUE INDEX IF NOT EXISTS idx_free_mock_sets_one_published
    ON public.free_mock_sets (exam_name, language_code)
    WHERE status = 'published';

CREATE INDEX IF NOT EXISTS idx_free_mock_sets_exam_lang_status
    ON public.free_mock_sets (exam_name, language_code, status, created_at DESC);


-- ---------------------------------------------------------------------------
-- 2. free_mock_set_items
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.free_mock_set_items (
    id               uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    free_mock_set_id uuid         NOT NULL
                        REFERENCES public.free_mock_sets(id) ON DELETE CASCADE,
    slot_order       integer      NOT NULL,
    question_id      integer      NOT NULL
                        REFERENCES public.questions(id),
    domain_name      text         NOT NULL,
    created_at       timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT free_mock_set_items_slot_range
        CHECK (slot_order >= 1 AND slot_order <= 15),

    CONSTRAINT free_mock_set_items_domain_nonempty
        CHECK (TRIM(domain_name) <> ''),

    CONSTRAINT free_mock_set_items_unique_slot
        UNIQUE (free_mock_set_id, slot_order),

    CONSTRAINT free_mock_set_items_unique_question
        UNIQUE (free_mock_set_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_free_mock_set_items_set_order
    ON public.free_mock_set_items (free_mock_set_id, slot_order);


-- ---------------------------------------------------------------------------
-- 3. Immutability triggers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.guard_free_mock_set_mutation_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('published', 'retired') THEN
            RAISE EXCEPTION 'cannot delete % free_mock_set %', OLD.status, OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF OLD.status = 'published' THEN
            IF NEW.status = 'retired'
               AND NEW.exam_name = OLD.exam_name
               AND NEW.language_code = OLD.language_code
               AND NEW.version_number = OLD.version_number
               AND NEW.created_at = OLD.created_at
               AND NEW.created_by = OLD.created_by
               AND NEW.updated_at = OLD.updated_at
               AND NEW.published_at IS NOT DISTINCT FROM OLD.published_at
               AND NEW.published_by IS NOT DISTINCT FROM OLD.published_by
               AND NEW.publish_reason IS NOT DISTINCT FROM OLD.publish_reason
               AND OLD.retired_at IS NULL
               AND NEW.retired_at IS NOT NULL
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'published free_mock_set % is immutable', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        IF OLD.status = 'retired' THEN
            RAISE EXCEPTION 'retired free_mock_set % is immutable', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.guard_free_mock_set_item_mutation_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT s.status INTO v_status
        FROM public.free_mock_sets s
        WHERE s.id = OLD.free_mock_set_id;
        IF v_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION 'cannot delete items from % free_mock_set', v_status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;

    SELECT s.status INTO v_status
    FROM public.free_mock_sets s
    WHERE s.id = COALESCE(NEW.free_mock_set_id, OLD.free_mock_set_id);

    IF v_status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'cannot modify items on % free_mock_set', v_status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_free_mock_set_mutation ON public.free_mock_sets;
CREATE TRIGGER trg_guard_free_mock_set_mutation
    BEFORE UPDATE OR DELETE ON public.free_mock_sets
    FOR EACH ROW EXECUTE FUNCTION public.guard_free_mock_set_mutation_v1();

DROP TRIGGER IF EXISTS trg_guard_free_mock_set_item_mutation ON public.free_mock_set_items;
CREATE TRIGGER trg_guard_free_mock_set_item_mutation
    BEFORE INSERT OR UPDATE OR DELETE ON public.free_mock_set_items
    FOR EACH ROW EXECUTE FUNCTION public.guard_free_mock_set_item_mutation_v1();


-- ---------------------------------------------------------------------------
-- 4. Blueprint + validation helpers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.free_mock_blueprint_v1(p_exam_name text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path = public, pg_catalog
AS $$
    SELECT CASE TRIM(p_exam_name)
        WHEN 'Salesforce Certified Platform Administrator' THEN jsonb_build_object(
            'Configuration and Setup', 2,
            'Object Manager and Lightning App Builder', 2,
            'Data and Analytics Management', 3,
            'Automation', 2,
            'Sales and Marketing Applications', 2,
            'Service and Support Applications', 2,
            'Productivity and Collaboration', 1,
            'Agentforce AI', 1
        )
        WHEN 'Salesforce Certified Business Analyst' THEN jsonb_build_object(
            'Customer Discovery', 2,
            'Collaboration with Stakeholders', 3,
            'Business Process Mapping', 2,
            'Requirements', 3,
            'User Stories', 3,
            'User Acceptance', 2
        )
        ELSE NULL::jsonb
    END;
$$;


CREATE OR REPLACE FUNCTION public.validate_free_mock_question_eligibility_v1(
    p_question_id integer,
    p_exam_name text,
    p_language_code text
)
RETURNS TABLE (failure_code text, failure_message text)
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_q record;
    v_option_count integer;
    v_correct_count integer;
    v_effective_select integer;
BEGIN
    SELECT q.*
    INTO   v_q
    FROM   public.questions q
    WHERE  q.id = p_question_id;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'QUESTION_NOT_FOUND', format('question %s not found', p_question_id);
        RETURN;
    END IF;

    IF v_q.exam_name IS DISTINCT FROM p_exam_name THEN
        RETURN QUERY SELECT 'EXAM_MISMATCH', format('question %s belongs to %s, expected %s', p_question_id, v_q.exam_name, p_exam_name);
    END IF;

    IF COALESCE(v_q.language_code, 'en') IS DISTINCT FROM p_language_code THEN
        RETURN QUERY SELECT 'LANGUAGE_MISMATCH', format('question %s language %s, expected %s', p_question_id, v_q.language_code, p_language_code);
    END IF;

    IF NOT COALESCE(v_q.is_active, false) THEN
        RETURN QUERY SELECT 'NOT_ACTIVE', format('question %s is not active', p_question_id);
    END IF;

    IF NOT COALESCE(v_q.is_exam_eligible, false) THEN
        RETURN QUERY SELECT 'NOT_EXAM_ELIGIBLE', format('question %s is not exam eligible', p_question_id);
    END IF;

    IF NOT COALESCE(v_q.mock_eligible, false) THEN
        RETURN QUERY SELECT 'NOT_MOCK_ELIGIBLE', format('question %s is not mock eligible', p_question_id);
    END IF;

    IF COALESCE(v_q.quality_status, '') <> 'approved' THEN
        RETURN QUERY SELECT 'NOT_APPROVED', format('question %s quality_status is %s', p_question_id, v_q.quality_status);
    END IF;

    IF COALESCE(TRIM(v_q.explanation), '') = '' THEN
        RETURN QUERY SELECT 'MISSING_EXPLANATION', format('question %s has no explanation', p_question_id);
    END IF;

    IF v_q.question_type NOT IN ('single', 'multiple') THEN
        RETURN QUERY SELECT 'INVALID_QUESTION_TYPE', format('question %s has invalid question_type %s', p_question_id, v_q.question_type);
    END IF;

    IF v_q.question_type = 'single' THEN
        v_effective_select := CASE
            WHEN v_q.select_count IS NULL OR v_q.select_count = 0 THEN 1
            ELSE v_q.select_count
        END;
        IF v_effective_select <> 1 THEN
            RETURN QUERY SELECT 'SINGLE_SELECT_COUNT_MISMATCH', format('question %s single select_count must be 1, got %s', p_question_id, v_q.select_count);
        END IF;
    ELSE
        IF v_q.select_count IS NULL OR v_q.select_count < 2 THEN
            RETURN QUERY SELECT 'INVALID_SELECT_COUNT', format('question %s multiple select_count must be >= 2, got %s', p_question_id, v_q.select_count);
        END IF;
        v_effective_select := v_q.select_count;
    END IF;

    SELECT COUNT(*)::integer,
           COUNT(*) FILTER (WHERE ao.is_correct)::integer
    INTO   v_option_count, v_correct_count
    FROM   public.answer_options ao
    WHERE  ao.question_id = p_question_id;

    IF COALESCE(v_option_count, 0) < 2 THEN
        RETURN QUERY SELECT 'TOO_FEW_OPTIONS', format('question %s has fewer than 2 answer options', p_question_id);
        RETURN;
    END IF;

    IF v_q.question_type = 'single' THEN
        IF COALESCE(v_correct_count, 0) <> 1 THEN
            RETURN QUERY SELECT 'CORRECT_COUNT_MISMATCH', format('question %s must have exactly 1 correct option, found %s', p_question_id, v_correct_count);
        END IF;
    ELSIF COALESCE(v_correct_count, 0) <> v_effective_select THEN
        RETURN QUERY SELECT 'CORRECT_COUNT_MISMATCH', format('question %s must have %s correct options, found %s', p_question_id, v_effective_select, v_correct_count);
    END IF;

    RETURN;
END;
$$;


CREATE OR REPLACE FUNCTION public.collect_free_mock_draft_failures_v1(p_set_id uuid)
RETURNS TABLE (failure_code text, failure_message text)
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_set record;
    v_blueprint jsonb;
    v_item_count integer;
    v_multi_count integer;
    v_domain text;
    v_required integer;
    v_actual integer;
    v_slot integer;
    v_seen_slots integer[];
    v_seen_questions integer[];
    rec record;
BEGIN
    SELECT *
    INTO   v_set
    FROM   public.free_mock_sets s
    WHERE  s.id = p_set_id;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'SET_NOT_FOUND', format('free_mock_set %s not found', p_set_id);
        RETURN;
    END IF;

    IF v_set.status <> 'draft' THEN
        RETURN QUERY SELECT 'NOT_DRAFT', format('free_mock_set %s status is %s; expected draft', p_set_id, v_set.status);
        RETURN;
    END IF;

    v_blueprint := public.free_mock_blueprint_v1(v_set.exam_name);
    IF v_blueprint IS NULL THEN
        RETURN QUERY SELECT 'UNKNOWN_EXAM', format('no free-mock blueprint for exam %s', v_set.exam_name);
        RETURN;
    END IF;

    SELECT COUNT(*)::integer INTO v_item_count
    FROM public.free_mock_set_items i
    WHERE i.free_mock_set_id = p_set_id;

    IF v_item_count <> 15 THEN
        RETURN QUERY SELECT 'ITEM_COUNT', format('expected exactly 15 items, found %s', v_item_count);
    END IF;

    v_seen_slots := ARRAY[]::integer[];
    v_seen_questions := ARRAY[]::integer[];

    FOR rec IN
        SELECT i.slot_order, i.question_id, i.domain_name
        FROM public.free_mock_set_items i
        WHERE i.free_mock_set_id = p_set_id
        ORDER BY i.slot_order
    LOOP
        IF rec.slot_order < 1 OR rec.slot_order > 15 THEN
            RETURN QUERY SELECT 'SLOT_OUT_OF_RANGE', format('slot %s out of range 1..15', rec.slot_order);
        END IF;

        IF rec.slot_order = ANY (v_seen_slots) THEN
            RETURN QUERY SELECT 'DUPLICATE_SLOT', format('duplicate slot_order %s', rec.slot_order);
        ELSE
            v_seen_slots := array_append(v_seen_slots, rec.slot_order);
        END IF;

        IF rec.question_id = ANY (v_seen_questions) THEN
            RETURN QUERY SELECT 'DUPLICATE_QUESTION', format('duplicate question_id %s', rec.question_id);
        ELSE
            v_seen_questions := array_append(v_seen_questions, rec.question_id);
        END IF;

        RETURN QUERY
        SELECT f.failure_code, f.failure_message
        FROM public.validate_free_mock_question_eligibility_v1(
            rec.question_id, v_set.exam_name, v_set.language_code
        ) f;
    END LOOP;

    FOR v_slot IN 1..15 LOOP
        IF NOT (v_slot = ANY (v_seen_slots)) THEN
            RETURN QUERY SELECT 'MISSING_SLOT', format('missing slot_order %s', v_slot);
        END IF;
    END LOOP;

    SELECT COUNT(*)::integer
    INTO   v_multi_count
    FROM   public.free_mock_set_items i
    JOIN   public.questions q ON q.id = i.question_id
    WHERE  i.free_mock_set_id = p_set_id
      AND  q.question_type = 'multiple';

    IF COALESCE(v_multi_count, 0) < 2 THEN
        RETURN QUERY SELECT 'MIN_MULTI_SELECT', format('expected at least 2 multi-select questions, found %s', COALESCE(v_multi_count, 0));
    END IF;

    FOR v_domain, v_required IN
        SELECT key, value::integer
        FROM jsonb_each_text(v_blueprint)
    LOOP
        SELECT COUNT(*)::integer
        INTO   v_actual
        FROM   public.free_mock_set_items i
        WHERE  i.free_mock_set_id = p_set_id
          AND  i.domain_name = v_domain;

        IF COALESCE(v_actual, 0) <> v_required THEN
            RETURN QUERY SELECT 'DOMAIN_COUNT', format('domain %s requires %s, found %s', v_domain, v_required, COALESCE(v_actual, 0));
        END IF;
    END LOOP;

    RETURN;
END;
$$;


-- ---------------------------------------------------------------------------
-- 5. RPCs
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.create_free_mock_draft_v1(
    p_exam_name     text,
    p_language_code text DEFAULT 'en',
    p_actor_email   text DEFAULT NULL
)
RETURNS TABLE (
    free_mock_set_id uuid,
    exam_name        text,
    language_code    text,
    status           text,
    created          boolean
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_existing uuid;
    v_actor text;
    v_new_id uuid;
BEGIN
    v_actor := NULLIF(BTRIM(p_actor_email), '');
    IF v_actor IS NULL THEN
        RAISE EXCEPTION 'p_actor_email must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF public.free_mock_blueprint_v1(p_exam_name) IS NULL THEN
        RAISE EXCEPTION 'unsupported exam_name for free-mock curation: %', p_exam_name
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT s.id
    INTO   v_existing
    FROM   public.free_mock_sets s
    WHERE  s.exam_name = p_exam_name
      AND  s.language_code = COALESCE(NULLIF(BTRIM(p_language_code), ''), 'en')
      AND  s.status = 'draft'
    LIMIT  1;

    IF v_existing IS NOT NULL THEN
        RETURN QUERY
        SELECT v_existing, p_exam_name, COALESCE(NULLIF(BTRIM(p_language_code), ''), 'en'), 'draft', false;
        RETURN;
    END IF;

    INSERT INTO public.free_mock_sets (
        exam_name, language_code, status, created_by, updated_at
    ) VALUES (
        p_exam_name,
        COALESCE(NULLIF(BTRIM(p_language_code), ''), 'en'),
        'draft',
        v_actor,
        now()
    )
    RETURNING id INTO v_new_id;

    RETURN QUERY
    SELECT v_new_id, p_exam_name, COALESCE(NULLIF(BTRIM(p_language_code), ''), 'en'), 'draft', true;
END;
$$;


CREATE OR REPLACE FUNCTION public.replace_free_mock_draft_items_v1(
    p_set_id   uuid,
    p_items    jsonb,
    p_actor_email text DEFAULT NULL
)
RETURNS TABLE (item_count integer)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_set record;
    v_actor text;
    v_item jsonb;
    v_slot integer;
    v_qid integer;
    v_domain text;
    v_count integer := 0;
    v_seen_slots integer[] := ARRAY[]::integer[];
    v_seen_questions integer[] := ARRAY[]::integer[];
    v_q record;
BEGIN
    v_actor := NULLIF(BTRIM(p_actor_email), '');
    IF v_actor IS NULL THEN
        RAISE EXCEPTION 'p_actor_email must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_items must be a JSON array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT *
    INTO   v_set
    FROM   public.free_mock_sets s
    WHERE  s.id = p_set_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'free_mock_set not found: %', p_set_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_set.status <> 'draft' THEN
        RAISE EXCEPTION 'only draft sets can be edited; status=%', v_set.status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Validate the full payload before deleting existing draft items.
    FOR v_item IN SELECT value FROM jsonb_array_elements(p_items)
    LOOP
        BEGIN
            v_slot := (v_item ->> 'slot_order')::integer;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'invalid slot_order in item %', v_item
                USING ERRCODE = 'invalid_parameter_value';
        END;

        BEGIN
            v_qid := (v_item ->> 'question_id')::integer;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'invalid question_id in item %', v_item
                USING ERRCODE = 'invalid_parameter_value';
        END;

        IF v_slot IS NULL OR v_slot < 1 OR v_slot > 15 THEN
            RAISE EXCEPTION 'invalid slot_order % in item %', v_item ->> 'slot_order', v_item
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_qid IS NULL THEN
            RAISE EXCEPTION 'question_id required in item %', v_item
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_slot = ANY (v_seen_slots) THEN
            RAISE EXCEPTION 'duplicate slot_order % in replacement payload', v_slot
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        v_seen_slots := array_append(v_seen_slots, v_slot);

        IF v_qid = ANY (v_seen_questions) THEN
            RAISE EXCEPTION 'duplicate question_id % in replacement payload', v_qid
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        v_seen_questions := array_append(v_seen_questions, v_qid);

        SELECT q.id, q.exam_name, q.category
        INTO   v_q
        FROM   public.questions q
        WHERE  q.id = v_qid;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'question % not found', v_qid
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_q.exam_name IS DISTINCT FROM v_set.exam_name THEN
            RAISE EXCEPTION 'question % belongs to %, expected %', v_qid, v_q.exam_name, v_set.exam_name
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        v_domain := TRIM(v_q.category);
        IF COALESCE(v_domain, '') = '' THEN
            RAISE EXCEPTION 'question % not found or missing category', v_qid
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END LOOP;

    DELETE FROM public.free_mock_set_items
    WHERE free_mock_set_id = p_set_id;

    FOR v_item IN SELECT value FROM jsonb_array_elements(p_items)
    LOOP
        v_slot := (v_item ->> 'slot_order')::integer;
        v_qid := (v_item ->> 'question_id')::integer;

        SELECT TRIM(q.category)
        INTO   v_domain
        FROM   public.questions q
        WHERE  q.id = v_qid;

        INSERT INTO public.free_mock_set_items (
            free_mock_set_id, slot_order, question_id, domain_name
        ) VALUES (
            p_set_id, v_slot, v_qid, v_domain
        );

        v_count := v_count + 1;
    END LOOP;

    UPDATE public.free_mock_sets
    SET updated_at = now()
    WHERE id = p_set_id;

    RETURN QUERY SELECT v_count;
END;
$$;


CREATE OR REPLACE FUNCTION public.validate_free_mock_draft_v1(p_set_id uuid)
RETURNS TABLE (
    valid    boolean,
    failures jsonb
)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_failures jsonb;
BEGIN
    SELECT COALESCE(
        jsonb_agg(jsonb_build_object('code', f.failure_code, 'message', f.failure_message) ORDER BY f.failure_code),
        '[]'::jsonb
    )
    INTO v_failures
    FROM public.collect_free_mock_draft_failures_v1(p_set_id) f;

    RETURN QUERY SELECT (jsonb_array_length(v_failures) = 0), v_failures;
END;
$$;


CREATE OR REPLACE FUNCTION public.publish_free_mock_draft_v1(
    p_set_id        uuid,
    p_actor_email   text,
    p_reason        text
)
RETURNS TABLE (
    free_mock_set_id uuid,
    exam_name        text,
    language_code    text,
    version_number   integer,
    retired_set_id   uuid
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_set record;
    v_actor text;
    v_reason text;
    v_failures jsonb;
    v_next_version integer;
    v_retired uuid;
BEGIN
    v_actor := NULLIF(BTRIM(p_actor_email), '');
    v_reason := NULLIF(BTRIM(p_reason), '');

    IF v_actor IS NULL THEN
        RAISE EXCEPTION 'p_actor_email must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_reason IS NULL THEN
        RAISE EXCEPTION 'p_reason must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT *
    INTO   v_set
    FROM   public.free_mock_sets s
    WHERE  s.id = p_set_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'free_mock_set not found: %', p_set_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_set.status <> 'draft' THEN
        RAISE EXCEPTION 'only draft sets can be published; status=%', v_set.status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtext(v_set.exam_name || '|' || v_set.language_code || '|free_mock_publish')
    );

    SELECT COALESCE(
        jsonb_agg(jsonb_build_object('code', f.failure_code, 'message', f.failure_message) ORDER BY f.failure_code),
        '[]'::jsonb
    )
    INTO v_failures
    FROM public.collect_free_mock_draft_failures_v1(p_set_id) f;

    IF jsonb_array_length(v_failures) > 0 THEN
        RAISE EXCEPTION 'publish blocked: % validation failure(s)', jsonb_array_length(v_failures)
            USING ERRCODE = 'invalid_parameter_value',
                  HINT = v_failures::text;
    END IF;

    SELECT s.id
    INTO   v_retired
    FROM   public.free_mock_sets s
    WHERE  s.exam_name = v_set.exam_name
      AND  s.language_code = v_set.language_code
      AND  s.status = 'published'
    FOR UPDATE;

    IF v_retired IS NOT NULL THEN
        UPDATE public.free_mock_sets
        SET status = 'retired',
            retired_at = now()
        WHERE id = v_retired;
    END IF;

    SELECT COALESCE(MAX(s.version_number), 0) + 1
    INTO   v_next_version
    FROM   public.free_mock_sets s
    WHERE  s.exam_name = v_set.exam_name
      AND  s.language_code = v_set.language_code
      AND  s.version_number IS NOT NULL;

    UPDATE public.free_mock_sets
    SET status = 'published',
        version_number = v_next_version,
        published_at = now(),
        published_by = v_actor,
        publish_reason = v_reason,
        updated_at = now()
    WHERE id = p_set_id;

    RETURN QUERY
    SELECT p_set_id, v_set.exam_name, v_set.language_code, v_next_version, v_retired;
END;
$$;


CREATE OR REPLACE FUNCTION public.get_free_mock_curation_state_v1(
    p_exam_name     text,
    p_language_code text DEFAULT 'en'
)
RETURNS TABLE (
    set_id          uuid,
    status          text,
    version_number  integer,
    published_at    timestamptz,
    published_by    text,
    publish_reason  text,
    items           jsonb
)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
BEGIN
    RETURN QUERY
    SELECT
        s.id,
        s.status,
        s.version_number,
        s.published_at,
        s.published_by,
        s.publish_reason,
        COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'slot_order', i.slot_order,
                        'question_id', i.question_id,
                        'domain_name', i.domain_name
                    )
                    ORDER BY i.slot_order
                )
                FROM public.free_mock_set_items i
                WHERE i.free_mock_set_id = s.id
            ),
            '[]'::jsonb
        ) AS items
    FROM public.free_mock_sets s
    WHERE s.exam_name = p_exam_name
      AND s.language_code = COALESCE(NULLIF(BTRIM(p_language_code), ''), 'en')
      AND s.status IN ('draft', 'published')
    ORDER BY CASE s.status WHEN 'published' THEN 0 ELSE 1 END, s.created_at DESC;
END;
$$;


-- ---------------------------------------------------------------------------
-- 6. Row Level Security
-- ---------------------------------------------------------------------------

ALTER TABLE public.free_mock_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.free_mock_set_items ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.free_mock_sets IS
'Versioned free-mock exam sets (draft/published/retired). Learner runtime not wired in Phase 1.';

COMMENT ON TABLE public.free_mock_set_items IS
'Ordered slots for a free_mock_set. Immutable once parent set is published.';


-- ---------------------------------------------------------------------------
-- 7. Grants — service_role only
-- ---------------------------------------------------------------------------

REVOKE ALL ON TABLE public.free_mock_sets FROM PUBLIC;
REVOKE ALL ON TABLE public.free_mock_set_items FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.free_mock_sets TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.free_mock_set_items TO service_role;

REVOKE ALL ON FUNCTION public.free_mock_blueprint_v1(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.free_mock_blueprint_v1(text) TO service_role;

REVOKE ALL ON FUNCTION public.validate_free_mock_question_eligibility_v1(integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.validate_free_mock_question_eligibility_v1(integer, text, text) TO service_role;

REVOKE ALL ON FUNCTION public.collect_free_mock_draft_failures_v1(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.collect_free_mock_draft_failures_v1(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.create_free_mock_draft_v1(text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.create_free_mock_draft_v1(text, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.create_free_mock_draft_v1(text, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.create_free_mock_draft_v1(text, text, text) TO service_role;

REVOKE ALL ON FUNCTION public.replace_free_mock_draft_items_v1(uuid, jsonb, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.replace_free_mock_draft_items_v1(uuid, jsonb, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.replace_free_mock_draft_items_v1(uuid, jsonb, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.replace_free_mock_draft_items_v1(uuid, jsonb, text) TO service_role;

REVOKE ALL ON FUNCTION public.validate_free_mock_draft_v1(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.validate_free_mock_draft_v1(uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.validate_free_mock_draft_v1(uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.validate_free_mock_draft_v1(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.publish_free_mock_draft_v1(uuid, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.publish_free_mock_draft_v1(uuid, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.publish_free_mock_draft_v1(uuid, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.publish_free_mock_draft_v1(uuid, text, text) TO service_role;

REVOKE ALL ON FUNCTION public.get_free_mock_curation_state_v1(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_free_mock_curation_state_v1(text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.get_free_mock_curation_state_v1(text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_free_mock_curation_state_v1(text, text) TO service_role;
