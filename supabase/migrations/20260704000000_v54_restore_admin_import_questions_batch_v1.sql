-- =============================================================================
-- V54 — Restore admin_import_questions_batch_v1 to version control
-- =============================================================================
--
-- This migration restores an already-live production function to version
-- control for the first time. It is a source-control restoration only.
--
-- The function body below reproduces current production behavior exactly,
-- as captured via read-only pg_get_functiondef and pg_proc/ACL introspection
-- against the live database. This migration introduces no intended semantic
-- behavior change: no new validation, no new locking (no FOR UPDATE was
-- added), no fix for the known missing question_type/difficulty NULL
-- handling, no changed defaults, no changed insert/update logic, and no
-- reformatting beyond what is required to paste the live body into a
-- migration file.
--
-- The explicit REVOKE/GRANT statements following the function definition
-- are required because pg_get_functiondef does not reproduce a function's
-- ACL, and PostgreSQL grants EXECUTE to PUBLIC by default on fresh function
-- creation. These statements reproduce the live ACL, which was confirmed via
-- introspection to be service_role-only (PUBLIC, anon, and authenticated all
-- confirmed unable to execute this function in production).
--
-- This migration must not be applied as part of this restoration task.
--
-- =============================================================================

CREATE OR REPLACE FUNCTION public.admin_import_questions_batch_v1(p_questions jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
declare
    v_question jsonb;
    v_option jsonb;
    v_options jsonb;

    v_existing public.questions%rowtype;
    v_question_id integer;

    v_external_key text;
    v_exam_name text;
    v_language_code text;
    v_category text;
    v_difficulty text;
    v_question_text text;
    v_question_type text;
    v_explanation text;
    v_quality_status text;
    v_review_notes text;
    v_source_batch text;
    v_source_file text;
    v_concept_key text;
    v_cognitive_level text;
    v_translation_status text;

    v_question_family_id uuid;
    v_translation_group_id uuid;

    v_select_count integer;
    v_content_version integer;
    v_source_question_id integer;
    v_source_content_version integer;
    v_free_sample_order integer;

    v_is_active boolean;
    v_is_exam_eligible boolean;
    v_free_mock_exam boolean;
    v_practice_eligible boolean;
    v_mock_eligible boolean;

    v_option_count integer;
    v_correct_count integer;
    v_unique_label_count integer;
    v_unique_order_count integer;

    v_is_existing boolean;
    v_has_attempts boolean;

    v_inserted_count integer := 0;
    v_updated_count integer := 0;
    v_option_count_total integer := 0;
begin
    if p_questions is null
       or jsonb_typeof(p_questions) <> 'array'
       or jsonb_array_length(p_questions) = 0 then
        raise exception
            'Import payload must be a non-empty JSON array of questions.';
    end if;

    -- Reject duplicate external keys inside the uploaded batch.
    if exists (
        select 1
        from jsonb_array_elements(p_questions) item
        group by nullif(btrim(item->>'external_key'), '')
        having nullif(btrim(item->>'external_key'), '') is not null
           and count(*) > 1
    ) then
        raise exception
            'Import payload contains duplicate external_key values.';
    end if;

    for v_question in
        select value
        from jsonb_array_elements(p_questions)
    loop
        v_external_key :=
            nullif(btrim(v_question->>'external_key'), '');

        v_exam_name :=
            nullif(btrim(v_question->>'exam_name'), '');

        v_language_code :=
            lower(nullif(btrim(v_question->>'language_code'), ''));

        v_category :=
            nullif(btrim(v_question->>'category'), '');

        v_difficulty :=
            lower(nullif(btrim(v_question->>'difficulty'), ''));

        v_question_text :=
            nullif(btrim(v_question->>'question_text'), '');

        v_question_type :=
            lower(nullif(btrim(v_question->>'question_type'), ''));

        v_explanation :=
            nullif(btrim(v_question->>'explanation'), '');

        v_quality_status :=
            lower(
                coalesce(
                    nullif(btrim(v_question->>'quality_status'), ''),
                    'approved'
                )
            );

        v_review_notes :=
            nullif(btrim(v_question->>'review_notes'), '');

        v_source_batch :=
            nullif(btrim(v_question->>'source_batch'), '');

        v_source_file :=
            nullif(btrim(v_question->>'source_file'), '');

        v_concept_key :=
            nullif(btrim(v_question->>'concept_key'), '');

        v_cognitive_level :=
            lower(nullif(btrim(v_question->>'cognitive_level'), ''));

        v_translation_status :=
            lower(nullif(btrim(v_question->>'translation_status'), ''));

        v_options := v_question->'options';

        if v_external_key is null then
            raise exception 'Question is missing external_key.';
        end if;

        if v_exam_name is null then
            raise exception
                'Question % is missing exam_name.',
                v_external_key;
        end if;

        if v_language_code is null then
            raise exception
                'Question % is missing language_code.',
                v_external_key;
        end if;

        if v_category is null then
            raise exception
                'Question % is missing category.',
                v_external_key;
        end if;

        if v_question_text is null then
            raise exception
                'Question % is missing question_text.',
                v_external_key;
        end if;

        if v_explanation is null then
            raise exception
                'Question % is missing explanation.',
                v_external_key;
        end if;

        if v_source_batch is null then
            raise exception
                'Question % is missing source_batch.',
                v_external_key;
        end if;

        if v_concept_key is null then
            raise exception
                'Question % is missing concept_key.',
                v_external_key;
        end if;

        if v_cognitive_level is null then
            raise exception
                'Question % is missing cognitive_level.',
                v_external_key;
        end if;

        if v_question_type not in ('single', 'multiple') then
            raise exception
                'Question % has invalid question_type: %.',
                v_external_key,
                coalesce(v_question_type, '(missing)');
        end if;

        if v_difficulty not in ('easy', 'medium', 'hard') then
            raise exception
                'Question % has invalid difficulty: %.',
                v_external_key,
                coalesce(v_difficulty, '(missing)');
        end if;

        if v_quality_status not in (
            'approved',
            'needs_edit',
            'practice_only',
            'reject'
        ) then
            raise exception
                'Question % has invalid quality_status: %.',
                v_external_key,
                v_quality_status;
        end if;

        if v_cognitive_level not in (
            'recall',
            'understanding',
            'application',
            'analysis',
            'judgment'
        ) then
            raise exception
                'Question % has invalid cognitive_level: %.',
                v_external_key,
                v_cognitive_level;
        end if;

        if v_translation_status not in (
            'source',
            'machine_translated',
            'reviewed',
            'approved',
            'rejected',
            'outdated'
        ) then
            raise exception
                'Question % has invalid translation_status: %.',
                v_external_key,
                coalesce(v_translation_status, '(missing)');
        end if;

        if not exists (
            select 1
            from public.certifications c
            where c.exam_name = v_exam_name
        ) then
            raise exception
                'Question % references unknown certification: %.',
                v_external_key,
                v_exam_name;
        end if;

        if not exists (
            select 1
            from public.languages l
            where l.language_code = v_language_code
        ) then
            raise exception
                'Question % references unknown language: %.',
                v_external_key,
                v_language_code;
        end if;

        if not exists (
            select 1
            from public.certification_domains cd
            where cd.exam_name = v_exam_name
              and cd.domain_name = v_category
        ) then
            raise exception
                'Question % references unconfigured domain % for %.',
                v_external_key,
                v_category,
                v_exam_name;
        end if;

        begin
            v_question_family_id :=
                nullif(v_question->>'question_family_id', '')::uuid;

            v_translation_group_id :=
                nullif(v_question->>'translation_group_id', '')::uuid;

            v_select_count :=
                nullif(v_question->>'select_count', '')::integer;

            v_content_version :=
                coalesce(
                    nullif(v_question->>'content_version', '')::integer,
                    1
                );

            v_source_question_id :=
                nullif(v_question->>'source_question_id', '')::integer;

            v_source_content_version :=
                nullif(
                    v_question->>'source_content_version',
                    ''
                )::integer;

            v_free_sample_order :=
                nullif(v_question->>'free_sample_order', '')::integer;

            v_is_active :=
                coalesce(
                    nullif(v_question->>'is_active', '')::boolean,
                    true
                );

            v_is_exam_eligible :=
                coalesce(
                    nullif(
                        v_question->>'is_exam_eligible',
                        ''
                    )::boolean,
                    true
                );

            v_free_mock_exam :=
                coalesce(
                    nullif(
                        v_question->>'free_mock_exam',
                        ''
                    )::boolean,
                    false
                );

            v_practice_eligible :=
                coalesce(
                    nullif(
                        v_question->>'practice_eligible',
                        ''
                    )::boolean,
                    true
                );

            v_mock_eligible :=
                coalesce(
                    nullif(
                        v_question->>'mock_eligible',
                        ''
                    )::boolean,
                    true
                );
        exception
            when others then
                raise exception
                    'Question % contains an invalid UUID, integer, or boolean value.',
                    v_external_key;
        end;

        if v_question_family_id is null then
            raise exception
                'Question % is missing question_family_id.',
                v_external_key;
        end if;

        if v_translation_group_id is null then
            raise exception
                'Question % is missing translation_group_id.',
                v_external_key;
        end if;

        if v_content_version < 1 then
            raise exception
                'Question % has invalid content_version.',
                v_external_key;
        end if;

        if jsonb_typeof(v_options) <> 'array' then
            raise exception
                'Question % must contain an options array.',
                v_external_key;
        end if;

        select
            count(*),
            count(*) filter (
                where coalesce(
                    nullif(option_item->>'is_correct', '')::boolean,
                    false
                ) = true
            ),
            count(
                distinct upper(
                    nullif(btrim(option_item->>'option_label'), '')
                )
            ),
            count(
                distinct coalesce(
                    nullif(
                        option_item->>'display_order',
                        ''
                    )::integer,
                    option_position::integer
                )
            )
        into
            v_option_count,
            v_correct_count,
            v_unique_label_count,
            v_unique_order_count
        from jsonb_array_elements(v_options)
             with ordinality as option_rows(
                 option_item,
                 option_position
             );

        if v_option_count < 2 or v_option_count > 6 then
            raise exception
                'Question % must contain between 2 and 6 options.',
                v_external_key;
        end if;

        if v_unique_label_count <> v_option_count then
            raise exception
                'Question % contains duplicate or missing option labels.',
                v_external_key;
        end if;

        if v_unique_order_count <> v_option_count then
            raise exception
                'Question % contains duplicate display_order values.',
                v_external_key;
        end if;

        if v_question_type = 'single' and v_correct_count <> 1 then
            raise exception
                'Question % is single-select but has % correct options.',
                v_external_key,
                v_correct_count;
        end if;

        if v_question_type = 'multiple' then
            if v_correct_count < 2 then
                raise exception
                    'Question % is multi-select but has fewer than 2 correct options.',
                    v_external_key;
            end if;

            if v_select_count is null
               or v_select_count <> v_correct_count then
                raise exception
                    'Question % select_count does not match its % correct options.',
                    v_external_key,
                    v_correct_count;
            end if;
        else
            v_select_count := null;
        end if;

        if exists (
            select 1
            from jsonb_array_elements(v_options) option_item
            where nullif(
                      btrim(option_item->>'option_text'),
                      ''
                  ) is null
        ) then
            raise exception
                'Question % contains a blank option.',
                v_external_key;
        end if;

        select q.*
        into v_existing
        from public.questions q
        where q.external_key = v_external_key;

        v_is_existing := found;

        if v_is_existing then
            v_question_id := v_existing.id;

            if v_existing.exam_name is distinct from v_exam_name
               or v_existing.language_code is distinct from v_language_code then
                raise exception
                    'Question % cannot change certification or language after creation.',
                    v_external_key;
            end if;

            select exists (
                select 1
                from public.question_attempts qa
                where qa.question_id = v_question_id
            )
            into v_has_attempts;

            if v_has_attempts then
                if v_existing.question_text is distinct from v_question_text
                   or v_existing.question_type is distinct from v_question_type
                   or v_existing.select_count is distinct from v_select_count
                   or v_existing.explanation is distinct from v_explanation then
                    raise exception
                        'Question % has student attempts and its tested content cannot be overwritten. Use a new external_key for a materially revised question.',
                        v_external_key;
                end if;

                if exists (
                    (
                        select
                            upper(btrim(ao.option_label::text)),
                            btrim(ao.option_text),
                            coalesce(ao.is_correct, false),
                            coalesce(ao.display_order, 0)
                        from public.answer_options ao
                        where ao.question_id = v_question_id
                    )
                    except
                    (
                        select
                            upper(btrim(option_item->>'option_label')),
                            btrim(option_item->>'option_text'),
                            coalesce(
                                nullif(
                                    option_item->>'is_correct',
                                    ''
                                )::boolean,
                                false
                            ),
                            coalesce(
                                nullif(
                                    option_item->>'display_order',
                                    ''
                                )::integer,
                                option_position::integer
                            )
                        from jsonb_array_elements(v_options)
                             with ordinality as incoming_options(
                                 option_item,
                                 option_position
                             )
                    )
                )
                or exists (
                    (
                        select
                            upper(btrim(option_item->>'option_label')),
                            btrim(option_item->>'option_text'),
                            coalesce(
                                nullif(
                                    option_item->>'is_correct',
                                    ''
                                )::boolean,
                                false
                            ),
                            coalesce(
                                nullif(
                                    option_item->>'display_order',
                                    ''
                                )::integer,
                                option_position::integer
                            )
                        from jsonb_array_elements(v_options)
                             with ordinality as incoming_options(
                                 option_item,
                                 option_position
                             )
                    )
                    except
                    (
                        select
                            upper(btrim(ao.option_label::text)),
                            btrim(ao.option_text),
                            coalesce(ao.is_correct, false),
                            coalesce(ao.display_order, 0)
                        from public.answer_options ao
                        where ao.question_id = v_question_id
                    )
                ) then
                    raise exception
                        'Question % has student attempts and its answer options cannot be overwritten. Use a new external_key for a materially revised question.',
                        v_external_key;
                end if;
            end if;

            update public.questions
            set
                category = v_category,
                difficulty = v_difficulty,
                question_text = v_question_text,
                question_type = v_question_type,
                select_count = v_select_count,
                explanation = v_explanation,
                is_active = v_is_active,
                is_exam_eligible = v_is_exam_eligible,
                quality_status = v_quality_status,
                review_notes = v_review_notes,
                source_batch = v_source_batch,
                source_file = v_source_file,
                free_mock_exam = v_free_mock_exam,
                free_sample_order = v_free_sample_order,
                concept_key = v_concept_key,
                question_family_id = v_question_family_id,
                translation_group_id = v_translation_group_id,
                practice_eligible = v_practice_eligible,
                mock_eligible = v_mock_eligible,
                cognitive_level = v_cognitive_level,
                content_version = v_content_version,
                source_question_id = v_source_question_id,
                source_content_version = v_source_content_version,
                translation_status = v_translation_status,
                updated_at = timezone('utc', now())
            where id = v_question_id;

            v_updated_count := v_updated_count + 1;
        else
            insert into public.questions (
                external_key,
                exam_name,
                language_code,
                category,
                difficulty,
                question_text,
                question_type,
                select_count,
                explanation,
                is_active,
                is_exam_eligible,
                quality_status,
                review_notes,
                source_batch,
                source_file,
                free_mock_exam,
                free_sample_order,
                concept_key,
                question_family_id,
                translation_group_id,
                practice_eligible,
                mock_eligible,
                cognitive_level,
                content_version,
                source_question_id,
                source_content_version,
                translation_status,
                updated_at
            )
            values (
                v_external_key,
                v_exam_name,
                v_language_code,
                v_category,
                v_difficulty,
                v_question_text,
                v_question_type,
                v_select_count,
                v_explanation,
                v_is_active,
                v_is_exam_eligible,
                v_quality_status,
                v_review_notes,
                v_source_batch,
                v_source_file,
                v_free_mock_exam,
                v_free_sample_order,
                v_concept_key,
                v_question_family_id,
                v_translation_group_id,
                v_practice_eligible,
                v_mock_eligible,
                v_cognitive_level,
                v_content_version,
                v_source_question_id,
                v_source_content_version,
                v_translation_status,
                timezone('utc', now())
            )
            returning id into v_question_id;

            v_has_attempts := false;
            v_inserted_count := v_inserted_count + 1;
        end if;

        -- Existing attempted questions have already been verified as
        -- content-identical, so their option rows do not need replacement.
        if not v_is_existing or not v_has_attempts then
            delete from public.answer_options
            where question_id = v_question_id;

            for v_option in
                select jsonb_build_object(
                    'option_label',
                        upper(
                            btrim(
                                option_item->>'option_label'
                            )
                        ),
                    'option_text',
                        btrim(
                            option_item->>'option_text'
                        ),
                    'is_correct',
                        coalesce(
                            nullif(
                                option_item->>'is_correct',
                                ''
                            )::boolean,
                            false
                        ),
                    'display_order',
                        coalesce(
                            nullif(
                                option_item->>'display_order',
                                ''
                            )::integer,
                            option_position::integer
                        )
                )
                from jsonb_array_elements(v_options)
                     with ordinality as option_rows(
                         option_item,
                         option_position
                     )
                order by option_position
            loop
                insert into public.answer_options (
                    question_id,
                    option_label,
                    option_text,
                    is_correct,
                    display_order,
                    language_code
                )
                values (
                    v_question_id,
                    v_option->>'option_label',
                    v_option->>'option_text',
                    (v_option->>'is_correct')::boolean,
                    (v_option->>'display_order')::integer,
                    v_language_code
                );

                v_option_count_total :=
                    v_option_count_total + 1;
            end loop;
        end if;
    end loop;

    return jsonb_build_object(
        'imported_question_count',
            jsonb_array_length(p_questions),
        'inserted_questions',
            v_inserted_count,
        'updated_questions',
            v_updated_count,
        'answer_options_written',
            v_option_count_total
    );
end;
$function$
;


-- =============================================================================
-- Privilege hardening — admin_import_questions_batch_v1
-- =============================================================================
--
-- pg_get_functiondef does not reproduce a function's ACL, and PostgreSQL
-- grants EXECUTE to PUBLIC by default on fresh function creation. The
-- statements below reproduce the live production ACL, which was confirmed
-- via introspection to be service_role-only.

REVOKE ALL ON FUNCTION public.admin_import_questions_batch_v1(jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION public.admin_import_questions_batch_v1(jsonb) FROM anon;

REVOKE ALL ON FUNCTION public.admin_import_questions_batch_v1(jsonb) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.admin_import_questions_batch_v1(jsonb) TO service_role;

COMMENT ON FUNCTION public.admin_import_questions_batch_v1(jsonb) IS
'Atomically upserts a batch of questions by external_key: one invalid row in
the JSON array raises an exception and rolls back every insert, update, and
answer_options replacement performed earlier in the same call. Once a
question has recorded question_attempts, its tested content (question_text,
question_type, select_count, explanation) and its answer_options are
immutable and any attempted change is rejected; administrative metadata
(category, difficulty, is_active, is_exam_eligible, quality_status,
review_notes, and related fields) remains editable regardless of attempts.
SECURITY DEFINER is safe here only because search_path is pinned to
''public'', ''pg_temp'' and execution is restricted to service_role; PUBLIC,
anon, and authenticated are explicitly revoked. This migration restores
existing, unchanged production behavior to version control and introduces no
behavior change.';
