-- =============================================================================
-- V48 AI Quality-Audit — Foundation, RPC, and Completion-Matrix Verification
-- =============================================================================
--
-- Run as service_role after applying, in order:
--   20260630120000_v48_ai_quality_audit_foundation.sql
--   20260630130000_v48_ai_quality_audit_rpcs.sql
--   20260630140000_v48_ai_quality_audit_job_type.sql
--
-- The entire script runs inside one transaction that always ends in ROLLBACK
-- (see the final statement). No row created or modified here is persisted.
-- Contains only standard SQL/PL/pgSQL (BEGIN/ROLLBACK, DO blocks, ASSERT,
-- CREATE [TEMP] TABLE/FUNCTION) — no psql-only meta-commands — so it can be
-- pasted and run as-is in the Supabase SQL Editor.
--
-- Exception-handling policy (applies to every expected-failure test below):
--   Each such test wraps the call in BEGIN ... EXCEPTION WHEN <specific
--   condition> THEN ... END. If the call unexpectedly succeeds, the BEGIN
--   block raises a distinct sentinel exception (a plain RAISE EXCEPTION,
--   SQLSTATE P0001) that is NOT caught by the specific WHEN clause, so it
--   propagates and aborts the script. If the call raises any exception
--   OTHER than the one specifically expected, it also propagates and aborts
--   the script (no bare "WHEN others" catch-alls are used for expected-
--   failure assertions). This guarantees a typo or unrelated regression
--   makes the affected test fail loudly rather than silently "pass".
--
-- Catalog/contract checks (S1-S7) run unconditionally. Behavioral checks
-- (S8 onward) use self-contained fixture rows inserted in S0 within this
-- same BEGIN…ROLLBACK transaction. Fixture creation failures abort the
-- script immediately; no section is silently skipped.
-- =============================================================================

BEGIN;

-- Explicit search_path for this verification session: do not depend on the
-- SQL Editor session's default. `extensions` is included because digest()
-- (pgcrypto) is used directly below, mirroring the search_path used by
-- create_or_get_ai_quality_audit_run_v1 itself. This SET is undone
-- automatically by the guaranteed ROLLBACK at the end of this script.
SET search_path = public, extensions, pg_catalog;

-- =============================================================================
-- S0: Self-contained fixture creation (deterministic IDs) + test helper
-- =============================================================================

CREATE TEMP TABLE v48_ctx (
    question_version_id uuid NOT NULL,
    certification_exam_name text NOT NULL,
    chunk1 uuid NOT NULL,
    chunk2 uuid NOT NULL,
    chunk_hash1 text NOT NULL,
    chunk_hash2 text NOT NULL,
    foreign_chunk uuid NOT NULL
) ON COMMIT DROP;

CREATE TEMP TABLE v48_behavioral_sections (
    section text PRIMARY KEY
) ON COMMIT DROP;

DO $$
DECLARE
    -- Deterministic fixture identifiers (unlikely to collide with production
    -- or migration-seeded rows; explicit IDs avoid depending on empty serials).
    v_question_id   integer := 992480001;
    v_qvid          uuid := 'a4800001-0001-4001-8001-000000000001';
    v_res1          uuid := 'b4800001-0001-4001-8001-000000000001';
    v_res2          uuid := 'b4800001-0001-4001-8001-000000000002';
    v_res_foreign   uuid := 'c4800001-0001-4001-8001-000000000099';
    v_rv1           uuid := 'd4800001-0001-4001-8001-000000000001';
    v_rv2           uuid := 'd4800001-0001-4001-8001-000000000002';
    v_rv_foreign    uuid := 'd4800001-0001-4001-8001-000000000099';
    v_c1            uuid := 'e4800001-0001-4001-8001-000000000001';
    v_c2            uuid := 'e4800001-0001-4001-8001-000000000002';
    v_c_foreign     uuid := 'e4800001-0001-4001-8001-000000000099';
    v_exam          text := 'V48-VERIFY-EXAM';
    v_exam_foreign  text := 'V48-FOREIGN-EXAM';
    v_h1            text := repeat('a', 64);
    v_h2            text := repeat('b', 64);
    v_h_foreign     text := repeat('c', 64);
BEGIN
    -- Parent certification for questions.exam_name (fk_questions_exam_name).
    -- official_resources.certification_exam_name is free text with only a
    -- non-empty CHECK in V44; it does not FK to public.certifications, so
    -- V48-FOREIGN-EXAM does not require a second certifications row.
    INSERT INTO public.certifications (
        exam_name, display_name, certification_code,
        passing_score, time_limit_minutes, question_count, is_active
    ) VALUES (
        v_exam, 'V48 Verification Exam', 'V48VERIFY',
        68, 105, 60, true
    );

    -- Parent language for questions.language_code (fk_questions_language_code).
    INSERT INTO public.languages (
        language_code, language_name, native_name, is_active, display_order
    ) VALUES (
        'en', 'English', 'English', true, 1
    );

    INSERT INTO public.questions (
        id, exam_name, category, difficulty, question_text, question_type,
        select_count, explanation, is_active, is_exam_eligible, language_code
    ) VALUES (
        v_question_id, v_exam, 'Verification', 'medium',
        'V48 verification fixture question stem', 'single',
        1, 'Fixture explanation must not leak via blind context.', true, true, 'en'
    );

    INSERT INTO public.question_versions (
        id, question_id, version_number, question_text, explanation,
        category, difficulty, question_type, select_count, language_code,
        content_hash, source_type, created_by
    ) VALUES (
        v_qvid, v_question_id, 1,
        'V48 verification fixture question stem',
        'Fixture explanation must not leak via blind context.',
        'Verification', 'medium', 'single', 1, 'en',
        repeat('f', 64), 'manual', 'v48-verification-script'
    );

    INSERT INTO public.question_option_versions (
        question_version_id, option_label, option_text, is_correct, display_order
    ) VALUES
        (v_qvid, 'A', 'Correct fixture option', true, 1),
        (v_qvid, 'B', 'Incorrect fixture option', false, 2);

    INSERT INTO public.official_resources (
        id, certification_exam_name, resource_type, title, is_active, created_by
    ) VALUES
        (v_res1, v_exam, 'official_documentation', 'V48 fixture resource 1', true, 'v48-verification-script'),
        (v_res2, v_exam, 'help_article', 'V48 fixture resource 2', true, 'v48-verification-script'),
        (v_res_foreign, v_exam_foreign, 'official_documentation', 'V48 foreign certification resource', true, 'v48-verification-script');

    INSERT INTO public.resource_versions (
        id, resource_id, version_number, content_text, content_hash, created_by
    ) VALUES
        (v_rv1, v_res1, 1, 'Fixture resource version 1 content.', repeat('1', 64), 'v48-verification-script'),
        (v_rv2, v_res2, 1, 'Fixture resource version 2 content.', repeat('2', 64), 'v48-verification-script'),
        (v_rv_foreign, v_res_foreign, 1, 'Foreign certification fixture content.', repeat('9', 64), 'v48-verification-script');

    INSERT INTO public.resource_chunks (
        id, resource_version_id, chunk_index, chunk_text, content_hash
    ) VALUES
        (v_c1, v_rv1, 0, 'Fixture chunk one text.', v_h1),
        (v_c2, v_rv2, 0, 'Fixture chunk two text.', v_h2),
        (v_c_foreign, v_rv_foreign, 0, 'Foreign certification chunk text.', v_h_foreign);

    INSERT INTO v48_ctx VALUES (
        v_qvid, v_exam, v_c1, v_c2, v_h1, v_h2, v_c_foreign
    );
END;
$$;

-- Test-local helper mirroring create_or_get_ai_quality_audit_run_v1's exact
-- canonicalization: SHA-256 over the jsonb::text serialization of a JSON
-- array of [retrieval_rank, resource_chunk_id, content_hash] triples
-- ordered by retrieval_rank. Reusing the identical encoding here (rather
-- than a parallel ad hoc computation) also means this test file will itself
-- start failing if the RPC's canonical format is ever changed without a
-- matching update, which is a desirable regression signal.
CREATE FUNCTION pg_temp.v48_hash(p_canonical jsonb) RETURNS text
LANGUAGE sql IMMUTABLE
AS $fn$
    SELECT encode(digest($1::text, 'sha256'), 'hex');
$fn$;

-- =============================================================================
-- S1: Table existence + RLS enabled
-- =============================================================================

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'audit_run_dedup_keys', 'audit_run_evidence_set',
        'audit_run_pass_results', 'audit_run_dispute_triggers'
    ] LOOP
        ASSERT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = v_table
        ), format('S1: table public.%s must exist', v_table);

        ASSERT (
            SELECT c.relrowsecurity FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = v_table
        ), format('S1: RLS must be enabled on public.%s', v_table);
    END LOOP;
END;
$$;

-- =============================================================================
-- S2: Extended audit_runs / background_jobs CHECK constraints preserve prior
--     values and add exactly the new ones
-- =============================================================================

DO $$
DECLARE
    v_type_def   text;
    v_status_def text;
    v_job_def    text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO v_type_def
    FROM   pg_constraint
    WHERE  conname = 'audit_runs_type_valid' AND conrelid = 'public.audit_runs'::regclass;
    ASSERT v_type_def LIKE '%ai_quality%', 'S2: audit_runs_type_valid must allow ai_quality';
    ASSERT v_type_def LIKE '%deterministic%' AND v_type_def LIKE '%hybrid%' AND v_type_def LIKE '%human%',
        'S2: audit_runs_type_valid must preserve all prior values';

    SELECT pg_get_constraintdef(oid) INTO v_status_def
    FROM   pg_constraint
    WHERE  conname = 'audit_runs_status_valid' AND conrelid = 'public.audit_runs'::regclass;
    ASSERT v_status_def LIKE '%inconclusive%', 'S2: audit_runs_status_valid must allow inconclusive';
    ASSERT v_status_def LIKE '%pending%' AND v_status_def LIKE '%completed%' AND v_status_def LIKE '%cancelled%',
        'S2: audit_runs_status_valid must preserve all prior values';

    SELECT pg_get_constraintdef(oid) INTO v_job_def
    FROM   pg_constraint
    WHERE  conname = 'background_jobs_type_valid' AND conrelid = 'public.background_jobs'::regclass;
    ASSERT v_job_def LIKE '%ai_quality_audit_smoke%', 'S2: background_jobs_type_valid must allow ai_quality_audit_smoke';
    ASSERT v_job_def LIKE '%resource_ingestion%' AND v_job_def LIKE '%certification_semantic_cluster_audit%',
        'S2: background_jobs_type_valid must preserve all prior values';
END;
$$;

-- =============================================================================
-- S3: Key constraints exist; redundant indexes were NOT created (N6); the
--     unique-constraint-backed indexes exist in their place
-- =============================================================================

DO $$
DECLARE
    v_name text;
BEGIN
    FOREACH v_name IN ARRAY ARRAY[
        'audit_run_dedup_keys_seven_key_unique',
        'audit_run_dedup_keys_evidence_hash_format',
        'audit_run_evidence_set_unique_chunk',
        'audit_run_evidence_set_unique_rank',
        'audit_run_pass_results_unique_pass',
        'audit_run_pass_results_running_requires_lease',
        'audit_run_pass_results_completed_requires_fields',
        'audit_run_dispute_triggers_reason_coupling'
    ] LOOP
        ASSERT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = v_name),
            format('S3: constraint %s must exist', v_name);
    END LOOP;

    -- idx_ardk_question_version is a genuinely distinct, non-redundant index.
    ASSERT EXISTS (
        SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_ardk_question_version'
    ), 'S3: index idx_ardk_question_version must exist';

    -- N6: idx_ares_run_rank and idx_arpr_run_pass were removed because they
    -- exactly duplicated the implicit indexes already created by
    -- audit_run_evidence_set_unique_rank / audit_run_pass_results_unique_pass.
    -- Assert they were NOT (re-)created, and that the constraints backing
    -- the equivalent lookups still exist.
    FOREACH v_name IN ARRAY ARRAY['idx_ares_run_rank', 'idx_arpr_run_pass'] LOOP
        ASSERT NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = v_name
        ), format('S3: redundant index %s must not exist (N6)', v_name);
    END LOOP;

    ASSERT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE  schemaname = 'public' AND tablename = 'audit_run_evidence_set'
          AND  indexname = 'audit_run_evidence_set_unique_rank'
    ), 'S3: audit_run_evidence_set_unique_rank must back an actual index on (audit_run_id, retrieval_rank)';

    ASSERT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE  schemaname = 'public' AND tablename = 'audit_run_pass_results'
          AND  indexname = 'audit_run_pass_results_unique_pass'
    ), 'S3: audit_run_pass_results_unique_pass must back an actual index on (audit_run_id, pass_code)';
END;
$$;

-- =============================================================================
-- S4: No anon/authenticated table privileges on the four new tables
-- =============================================================================

DO $$
DECLARE
    v_table text;
    v_role  text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'audit_run_dedup_keys', 'audit_run_evidence_set',
        'audit_run_pass_results', 'audit_run_dispute_triggers'
    ] LOOP
        FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            ASSERT NOT has_table_privilege(v_role, format('public.%s', v_table), 'SELECT'),
                format('S4: %s must not have SELECT on public.%s', v_role, v_table);
            ASSERT NOT has_table_privilege(v_role, format('public.%s', v_table), 'INSERT'),
                format('S4: %s must not have INSERT on public.%s', v_role, v_table);
            ASSERT NOT has_table_privilege(v_role, format('public.%s', v_table), 'UPDATE'),
                format('S4: %s must not have UPDATE on public.%s', v_role, v_table);
        END LOOP;
    END LOOP;
END;
$$;

-- =============================================================================
-- S5/S7: New RPC existence + service_role-only execution
-- =============================================================================

DO $$
DECLARE
    v_fn text;
BEGIN
    FOREACH v_fn IN ARRAY ARRAY[
        'public.get_question_version_blind_context_v1(uuid)',
        'public.get_question_version_comparison_context_v1(uuid,uuid)',
        'public.list_audit_candidate_resource_chunks_v1(text,uuid[],integer)',
        'public.create_or_get_ai_quality_audit_run_v1(uuid,text,text,text,text,text,text,jsonb,text,jsonb)',
        'public.claim_ai_quality_audit_pass_v1(uuid,text,integer)',
        'public.record_audit_pass_result_v1(uuid,text,uuid,text,jsonb,text,jsonb,jsonb,text,integer,integer,numeric,text,text,jsonb)',
        'public.persist_audit_run_dispute_trigger_v1(uuid,text,text,text,jsonb)',
        'public.complete_ai_quality_audit_run_v1(uuid,jsonb,jsonb)'
    ] LOOP
        ASSERT has_function_privilege('service_role', v_fn, 'EXECUTE'),
            format('S7: service_role must have EXECUTE on %s', v_fn);
        ASSERT NOT has_function_privilege('anon', v_fn, 'EXECUTE'),
            format('S7: anon must not have EXECUTE on %s', v_fn);
        ASSERT NOT has_function_privilege('authenticated', v_fn, 'EXECUTE'),
            format('S7: authenticated must not have EXECUTE on %s', v_fn);
        ASSERT NOT has_function_privilege('public', v_fn, 'EXECUTE'),
            format('S7: PUBLIC must not have EXECUTE on %s', v_fn);
    END LOOP;
END;
$$;


-- ---------------------------------------------------------------------------
-- S8: Blind context never exposes is_correct/explanation/stored answers,
--     and the option-shape assertion cannot pass vacuously.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx  v48_ctx;
    v_rec  record;
    v_opt  jsonb;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    SELECT * INTO v_rec
    FROM public.get_question_version_blind_context_v1(v_ctx.question_version_id);

    ASSERT v_rec.question_version_id = v_ctx.question_version_id, 'S8: blind context identity mismatch';

    -- S0 inserts a fixture question_version with two options; assert that
    -- guarantee explicitly so this test cannot pass vacuously against an
    -- empty options array.
    ASSERT jsonb_array_length(v_rec.options) > 0,
        'S8: fixture question_version must have at least one option (S0 insert)';

    FOR v_opt IN SELECT * FROM jsonb_array_elements(v_rec.options) LOOP
        ASSERT NOT (v_opt ? 'is_correct'), 'S8: blind-context option must not contain is_correct';
    END LOOP;

    -- The blind-context RETURNS TABLE shape has no explanation or stored-
    -- correct-label column at all; assert this explicitly against the
    -- actual returned row (not just the function signature) as documentation
    -- and as a guard against a future, careless column addition.
    ASSERT NOT (to_jsonb(v_rec) ? 'explanation'),
        'S8: blind context must not contain an explanation field';
    ASSERT NOT (to_jsonb(v_rec) ? 'stored_correct_option_labels'),
        'S8: blind context must not contain a stored-correct-labels field';
    ASSERT NOT (to_jsonb(v_rec) ? 'is_correct'),
        'S8: blind context row must not contain a top-level is_correct field';

    INSERT INTO v48_behavioral_sections VALUES ('S8');
END;
$$;

-- ---------------------------------------------------------------------------
-- S9: Comparison context denied before Pass A completion (specific SQLSTATE)
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx    v48_ctx;
    v_run    record;
    v_hash   text;
    v_msg    text;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    v_hash := pg_temp.v48_hash(jsonb_build_array(
        jsonb_build_array(1, v_ctx.chunk1::text, v_ctx.chunk_hash1),
        jsonb_build_array(2, v_ctx.chunk2::text, v_ctx.chunk_hash2)
    ));

    SELECT * INTO v_run
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-s9',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );
    ASSERT v_run.created, 'S9: expected a newly created run';

    BEGIN
        PERFORM * FROM public.get_question_version_comparison_context_v1(
            v_ctx.question_version_id, v_run.audit_run_id
        );
        RAISE EXCEPTION 'S9_TEST_DID_NOT_RAISE: comparison context unexpectedly succeeded before Pass A completed';
    EXCEPTION
        WHEN insufficient_privilege THEN
            GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT;
            ASSERT v_msg LIKE '%Pass A is not completed%',
                format('S9: expected a Pass-A-not-completed message, got: %s', v_msg);
    END;

    INSERT INTO v48_behavioral_sections VALUES ('S9');
    RAISE NOTICE 'S9 (comparison context denied pre-Pass-A) passed';
END;
$$;

-- ---------------------------------------------------------------------------
-- S10: seven-key dedup, evidence-hash mismatch rejection, foreign-chunk
--      rejection, lease WAIT/reclaim, a genuinely stale reclaimed token, and
--      deterministic attempt-cap exhaustion (attempt_count must stay 2).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx        v48_ctx;
    v_run        record;
    v_hash       text;
    v_claim1     record;
    v_claim2     record;
    v_claim3     record;
    v_claim4     record;
    v_rec        record;
    v_stale_token uuid;
    v_msg        text;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    v_hash := pg_temp.v48_hash(jsonb_build_array(
        jsonb_build_array(1, v_ctx.chunk1::text, v_ctx.chunk_hash1),
        jsonb_build_array(2, v_ctx.chunk2::text, v_ctx.chunk_hash2)
    ));

    -- A deliberately wrong (but well-formed) hash must be rejected.
    BEGIN
        PERFORM * FROM public.create_or_get_ai_quality_audit_run_v1(
            v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
            'verify-model-primary', 'verify-model-dispute', 'v48-verify-badhash',
            repeat('0', 64),
            jsonb_build_array(
                jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
                jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
            ),
            'v48-verification-script'
        );
        RAISE EXCEPTION 'S10_TEST_DID_NOT_RAISE: mismatched evidence_set_hash unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT;
            ASSERT v_msg LIKE '%evidence_set_hash mismatch%',
                format('S10: expected an evidence_set_hash mismatch message, got: %s', v_msg);
    END;

    -- A chunk outside the certification/active/latest-version rule is rejected.
    BEGIN
        PERFORM * FROM public.create_or_get_ai_quality_audit_run_v1(
            v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
            'verify-model-primary', 'verify-model-dispute', 'v48-verify-foreign',
            repeat('1', 64),
            jsonb_build_array(
                jsonb_build_object('resource_chunk_id', v_ctx.foreign_chunk, 'retrieval_rank', 1)
            ),
            'v48-verification-script'
        );
        RAISE EXCEPTION 'S10_TEST_DID_NOT_RAISE: foreign/invalid chunk unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT;
            ASSERT v_msg LIKE '%does not exist, is not on an active resource%',
                format('S10: expected a chunk-eligibility rejection message, got: %s', v_msg);
    END;

    -- Create the real run used for the rest of this section.
    SELECT * INTO v_run
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-lifecycle',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );
    ASSERT v_run.created, 'S10: expected a newly created run for the lifecycle test';

    -- audit_runs.model_name/prompt_version/ruleset_version must be populated
    -- directly on the run row (N5), not only on audit_run_dedup_keys.
    SELECT model_name, prompt_version, ruleset_version INTO v_rec
    FROM public.audit_runs WHERE id = v_run.audit_run_id;
    ASSERT v_rec.model_name = 'verify-model-primary'
       AND v_rec.prompt_version = 'verify-prompt-v1'
       AND v_rec.ruleset_version = 'verify-ruleset-v1',
        'N5: audit_runs.model_name/prompt_version/ruleset_version must be populated at creation';

    -- Duplicate seven-key creation returns the same run, no duplicate evidence.
    SELECT * INTO v_claim1
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-lifecycle',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );
    ASSERT v_claim1.audit_run_id = v_run.audit_run_id, 'S10: duplicate seven-key call must return the same run';
    ASSERT NOT v_claim1.created, 'S10: duplicate seven-key call must report created=false';
    ASSERT (SELECT COUNT(*) FROM public.audit_run_evidence_set WHERE audit_run_id = v_run.audit_run_id) = 2,
        'S10: duplicate call must not duplicate the frozen evidence set';

    -- Claim Pass A; a second worker claiming while the lease is active must WAIT.
    SELECT * INTO v_claim1
    FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-1', 60);
    ASSERT v_claim1.action = 'EXECUTE_PASS_A', format('S10: expected EXECUTE_PASS_A, got %', v_claim1.action);
    ASSERT v_claim1.attempt_count = 1, 'S10: first claim must be attempt_count=1';
    ASSERT v_claim1.is_retry = FALSE, 'S10: first claim must not be a retry';

    SELECT * INTO v_claim2
    FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-2', 60);
    ASSERT v_claim2.action = 'WAIT', format('S10: expected WAIT for a concurrent claim, got %', v_claim2.action);
    ASSERT v_claim2.action <> 'EXECUTE_PASS_B', 'S10: Pass B must not be claimable before Pass A is completed';

    -- An unknown/random lease_token must be rejected.
    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'A', gen_random_uuid(), 'completed',
            jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
        );
        RAISE EXCEPTION 'S10_TEST_DID_NOT_RAISE: an unknown lease_token was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT;
            ASSERT v_msg LIKE '%lease token mismatch%' OR v_msg LIKE '%stale token rejected%',
                format('S10: expected a lease-token-mismatch message, got: %s', v_msg);
    END;

    -- Force the active lease to expire, then reclaim with a new worker and a
    -- new token (attempt 2 of 2). Retain the ORIGINAL (v_claim1) token: it
    -- is now genuinely stale because a real reclaim has superseded it.
    v_stale_token := v_claim1.lease_token;

    UPDATE public.audit_run_pass_results
    SET lease_expires_at = now() - interval '1 second'
    WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'A';

    SELECT * INTO v_claim3
    FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-3', 60);
    ASSERT v_claim3.action = 'EXECUTE_PASS_A', 'S10: expired-lease reclaim must yield EXECUTE_PASS_A';
    ASSERT v_claim3.attempt_count = 2, 'S10: expired-lease reclaim must be attempt 2';
    ASSERT v_claim3.is_retry = TRUE, 'S10: expired-lease reclaim must be flagged as a retry';
    ASSERT v_claim3.lease_token IS DISTINCT FROM v_stale_token, 'S10: reclaim must issue a new lease token';

    -- Genuinely stale reclaimed token (item 11): attempt to record using the
    -- FORMERLY valid token now that a real reclaim has superseded it. Must
    -- be rejected specifically because the token is stale, not because it
    -- was never valid.
    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'A', v_stale_token, 'completed',
            jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
        );
        RAISE EXCEPTION 'S10_TEST_DID_NOT_RAISE: a genuinely reclaimed-stale lease_token was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT;
            ASSERT v_msg LIKE '%lease token mismatch%' OR v_msg LIKE '%stale token rejected%',
                format('S10: expected a stale-lease-token rejection message, got: %s', v_msg);
    END;

    -- Confirm the row is still claimable/recordable under the CURRENT
    -- (v_claim3) token — i.e. rejecting the stale token did not corrupt the
    -- live lease.
    ASSERT (
        SELECT lease_token FROM public.audit_run_pass_results
        WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'A'
    ) = v_claim3.lease_token, 'S10: current lease_token must be unaffected by the rejected stale-token attempt';

    -- Expire this second (and final) lease, then a third claim must perform
    -- no execution: pass becomes failed, attempt_count stays 2 (C1 fix),
    -- prospective_attempt_count=3 recorded, run becomes inconclusive.
    UPDATE public.audit_run_pass_results
    SET lease_expires_at = now() - interval '1 second'
    WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'A';

    SELECT * INTO v_claim4
    FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-4', 60);
    ASSERT v_claim4.action = 'RUN_INCONCLUSIVE', format('S10: expected RUN_INCONCLUSIVE, got %', v_claim4.action);
    ASSERT v_claim4.attempt_count = 2,
        'C1: claim_ai_quality_audit_pass_v1 must report attempt_count=2 (not 3) on cap exhaustion';

    SELECT * INTO v_rec FROM public.audit_run_pass_results
    WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'A';
    ASSERT v_rec.status = 'failed', 'S10: Pass A must be failed after attempt-limit exhaustion';
    ASSERT v_rec.attempt_count = 2,
        'C1: attempt_count column must remain 2 (no third provider attempt occurred)';
    ASSERT (v_rec.last_error ->> 'error_code') = 'PASS_LEASE_ATTEMPT_LIMIT_EXCEEDED',
        'S10: last_error.error_code must be PASS_LEASE_ATTEMPT_LIMIT_EXCEEDED';
    ASSERT (v_rec.last_error ->> 'attempt_count')::integer = 2,
        'C1: last_error.attempt_count must be 2, consistent with the persisted column';
    ASSERT (v_rec.last_error ->> 'prospective_attempt_count')::integer = 3,
        'S10: last_error.prospective_attempt_count must be 3';
    ASSERT v_rec.lease_owner IS NULL AND v_rec.lease_token IS NULL AND v_rec.lease_expires_at IS NULL,
        'S10: lease fields must be cleared';

    SELECT run_status INTO v_rec FROM public.audit_runs WHERE id = v_run.audit_run_id;
    ASSERT v_rec.run_status = 'inconclusive', 'S10: run must be inconclusive';

    -- Further claims on an inconclusive run must short-circuit.
    SELECT * INTO v_claim1
    FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-5', 60);
    ASSERT v_claim1.action = 'RUN_INCONCLUSIVE', 'S10: claiming an inconclusive run must keep returning RUN_INCONCLUSIVE';

    INSERT INTO v48_behavioral_sections VALUES ('S10');
    RAISE NOTICE 'S10 (dedup/lease/stale-token/attempt-cap lifecycle) passed';
END;
$$;

-- ---------------------------------------------------------------------------
-- S11: record_audit_pass_result_v1 malformed-'completed'-payload regression
--      (C2). Each rejected attempt must leave the pass row 'running' with
--      its lease intact so a corrected result can still be recorded.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx    v48_ctx;
    v_run    record;
    v_hash   text;
    v_claim  record;
    v_rec    record;
    v_msg    text;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    v_hash := pg_temp.v48_hash(jsonb_build_array(
        jsonb_build_array(1, v_ctx.chunk1::text, v_ctx.chunk_hash1),
        jsonb_build_array(2, v_ctx.chunk2::text, v_ctx.chunk_hash2)
    ));

    SELECT * INTO v_run
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-malformed',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );

    -- ---- Pass A malformed variants ----
    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-ma', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_A', 'S11: expected EXECUTE_PASS_A';

    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'A', v_claim.lease_token, 'completed', '{}'::jsonb
        );
        RAISE EXCEPTION 'S11_TEST_DID_NOT_RAISE: Pass A completed with {} was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    SELECT status, lease_token INTO v_rec FROM public.audit_run_pass_results
    WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'A';
    ASSERT v_rec.status = 'running' AND v_rec.lease_token = v_claim.lease_token,
        'C2: pass A must remain running with its lease intact after a rejected {} payload';

    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'A', v_claim.lease_token, 'completed',
            jsonb_build_object('selected_option_labels', 'A')
        );
        RAISE EXCEPTION 'S11_TEST_DID_NOT_RAISE: Pass A completed with non-array selected_option_labels was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    -- Corrected valid result must still be recordable against the SAME lease.
    SELECT * INTO v_rec FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'A', v_claim.lease_token, 'completed',
        jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
    );
    ASSERT v_rec.status = 'completed', 'C2: a corrected valid Pass A result must be recordable after prior rejections';

    -- ---- Pass B malformed variants ----
    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-mb', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_B', format('S11: expected EXECUTE_PASS_B, got %', v_claim.action);

    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'B', v_claim.lease_token, 'completed',
            jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
        );
        RAISE EXCEPTION 'S11_TEST_DID_NOT_RAISE: Pass B completed without proposed_findings was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'B', v_claim.lease_token, 'completed',
            jsonb_build_object(
                'selected_option_labels', jsonb_build_array('A'),
                'proposed_findings', jsonb_build_array(jsonb_build_object('finding_ref', ''))
            )
        );
        RAISE EXCEPTION 'S11_TEST_DID_NOT_RAISE: Pass B proposed_findings with an empty finding_ref was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'B', v_claim.lease_token, 'completed',
            jsonb_build_object(
                'selected_option_labels', jsonb_build_array('A'),
                'proposed_findings', jsonb_build_array(
                    jsonb_build_object('finding_ref', 'F1'),
                    jsonb_build_object('finding_ref', 'F1')
                )
            )
        );
        RAISE EXCEPTION 'S11_TEST_DID_NOT_RAISE: Pass B duplicate finding_ref was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    SELECT status, lease_token INTO v_rec FROM public.audit_run_pass_results
    WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'B';
    ASSERT v_rec.status = 'running' AND v_rec.lease_token = v_claim.lease_token,
        'C2: pass B must remain running with its lease intact after rejected payloads';

    SELECT * INTO v_rec FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'B', v_claim.lease_token, 'completed',
        jsonb_build_object('selected_option_labels', jsonb_build_array('A'), 'proposed_findings', '[]'::jsonb)
    );
    ASSERT v_rec.status = 'completed', 'C2: a corrected valid Pass B result must be recordable after prior rejections';

    -- Pass C's malformed-payload regression is covered separately in S11b,
    -- on a fresh run driven through a real normal dispute trigger (this
    -- run's Pass B recorded zero proposed_findings, so no valid finding_ref
    -- exists here for a trigger to reference).
    INSERT INTO v48_behavioral_sections VALUES ('S11');
    RAISE NOTICE 'S11 (Pass A/B malformed-result regression + recoverability) passed';
END;
$$;

-- ---------------------------------------------------------------------------
-- S11b: Pass C malformed-'completed'-payload regression (C2), on a fresh run
--       driven through a real normal dispute trigger so Pass C is claimable.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx    v48_ctx;
    v_run    record;
    v_hash   text;
    v_claim  record;
    v_rec    record;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    v_hash := pg_temp.v48_hash(jsonb_build_array(
        jsonb_build_array(1, v_ctx.chunk1::text, v_ctx.chunk_hash1),
        jsonb_build_array(2, v_ctx.chunk2::text, v_ctx.chunk_hash2)
    ));

    SELECT * INTO v_run
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-malformed-c',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-mc-a', 60);
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'A', v_claim.lease_token, 'completed',
        jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-mc-b', 60);
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'B', v_claim.lease_token, 'completed',
        jsonb_build_object(
            'selected_option_labels', jsonb_build_array('A'),
            'proposed_findings', jsonb_build_array(jsonb_build_object('finding_ref', 'F1', 'summary', 's'))
        )
    );

    PERFORM * FROM public.persist_audit_run_dispute_trigger_v1(
        v_run.audit_run_id, 'BLOCKING_DEFECT_PROPOSED', 'B', 'malformed-result regression trigger',
        jsonb_build_array('F1')
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-mc-c', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_C', format('S11b: expected EXECUTE_PASS_C, got %', v_claim.action);

    -- Missing discriminator fields entirely.
    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'C', v_claim.lease_token, 'completed', '{}'::jsonb
        );
        RAISE EXCEPTION 'S11b_TEST_DID_NOT_RAISE: Pass C completed with {} was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    -- Invalid resolution_type.
    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'C', v_claim.lease_token, 'completed',
            jsonb_build_object(
                'resolution_type', 'BOGUS', 'resolution_status', 'RESOLVED',
                'substituted_for_passes', '[]'::jsonb, 'confirmed_finding_refs', '[]'::jsonb
            )
        );
        RAISE EXCEPTION 'S11b_TEST_DID_NOT_RAISE: Pass C invalid resolution_type was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    -- Mismatched substituted_for_passes for the claimed resolution_type.
    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'C', v_claim.lease_token, 'completed',
            jsonb_build_object(
                'resolution_type', 'NORMAL_DISPUTE', 'resolution_status', 'RESOLVED',
                'substituted_for_passes', jsonb_build_array('A'), 'confirmed_finding_refs', '[]'::jsonb
            )
        );
        RAISE EXCEPTION 'S11b_TEST_DID_NOT_RAISE: Pass C mismatched substituted_for_passes was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    -- Non-array confirmed_finding_refs.
    BEGIN
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run.audit_run_id, 'C', v_claim.lease_token, 'completed',
            jsonb_build_object(
                'resolution_type', 'NORMAL_DISPUTE', 'resolution_status', 'RESOLVED',
                'substituted_for_passes', '[]'::jsonb, 'confirmed_finding_refs', 'F1'
            )
        );
        RAISE EXCEPTION 'S11b_TEST_DID_NOT_RAISE: Pass C non-array confirmed_finding_refs was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN NULL;
    END;

    SELECT status, lease_token INTO v_rec FROM public.audit_run_pass_results
    WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'C';
    ASSERT v_rec.status = 'running' AND v_rec.lease_token = v_claim.lease_token,
        'C2: pass C must remain running with its lease intact after rejected payloads';

    -- Corrected valid (UNRESOLVED, to keep this test self-contained) result.
    SELECT * INTO v_rec FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'C', v_claim.lease_token, 'completed',
        jsonb_build_object(
            'resolution_type', 'NORMAL_DISPUTE', 'resolution_status', 'UNRESOLVED',
            'substituted_for_passes', '[]'::jsonb, 'confirmed_finding_refs', '[]'::jsonb
        )
    );
    ASSERT v_rec.status = 'completed', 'C2: a corrected valid Pass C result must be recordable after prior rejections';

    INSERT INTO v48_behavioral_sections VALUES ('S11b');
    RAISE NOTICE 'S11b (Pass C malformed-result regression + recoverability) passed';
END;
$$;

-- ---------------------------------------------------------------------------
-- S12: NORMAL_NO_DISPUTE — full completion path, end to end, plus idempotency
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx    v48_ctx;
    v_run    record;
    v_hash   text;
    v_claim  record;
    v_rec    record;
    v_comp   record;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    v_hash := pg_temp.v48_hash(jsonb_build_array(
        jsonb_build_array(1, v_ctx.chunk1::text, v_ctx.chunk_hash1),
        jsonb_build_array(2, v_ctx.chunk2::text, v_ctx.chunk_hash2)
    ));

    SELECT * INTO v_run
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-nodispute',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-a', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_A', 'S12: expected EXECUTE_PASS_A';
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'A', v_claim.lease_token, 'completed',
        jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
    );

    SELECT * INTO v_comp
    FROM public.get_question_version_comparison_context_v1(v_ctx.question_version_id, v_run.audit_run_id);
    ASSERT v_comp.question_version_id = v_ctx.question_version_id, 'S12: comparison context must succeed after Pass A completes';

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-b', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_B', format('S12: expected EXECUTE_PASS_B, got %', v_claim.action);
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'B', v_claim.lease_token, 'completed',
        jsonb_build_object('selected_option_labels', jsonb_build_array('A'), 'proposed_findings', '[]'::jsonb)
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-c', 60);
    ASSERT v_claim.action = 'SKIP_PASS_C', format('S12: expected SKIP_PASS_C, got %', v_claim.action);

    ASSERT (
        SELECT status FROM public.audit_run_pass_results
        WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'C'
    ) = 'skipped', 'S12: SKIP_PASS_C must persist an actual C row with status=skipped';

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-c2', 60);
    ASSERT v_claim.action = 'RUN_READY_TO_COMPLETE', format('S12: expected RUN_READY_TO_COMPLETE, got %', v_claim.action);

    SELECT * INTO v_rec FROM public.complete_ai_quality_audit_run_v1(v_run.audit_run_id, '[]'::jsonb);
    ASSERT v_rec.run_status = 'completed', 'S12: NORMAL_NO_DISPUTE must complete';
    ASSERT v_rec.finding_count = 0, 'S12: NORMAL_NO_DISPUTE with no findings must insert zero findings';

    -- Idempotency: already-completed run is a no-op returning the same counts.
    SELECT * INTO v_rec FROM public.complete_ai_quality_audit_run_v1(v_run.audit_run_id, '[]'::jsonb);
    ASSERT v_rec.run_status = 'completed' AND v_rec.finding_count = 0,
        'S12: re-completing an already-completed run must be an idempotent no-op';

    INSERT INTO v48_behavioral_sections VALUES ('S12');
    RAISE NOTICE 'S12 (NORMAL_NO_DISPUTE full completion) passed';
END;
$$;

-- ---------------------------------------------------------------------------
-- S13: NORMAL_DISPUTE, RESOLVED — full completion path: blocking confirmation,
--      SOURCE_SUPPORT_WEAK forcing, evidence-subset rejection, zero-evidence
--      context requirement, and a representative rejected combination.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx       v48_ctx;
    v_run       record;
    v_hash      text;
    v_claim     record;
    v_rec       record;
    v_msg       text;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    v_hash := pg_temp.v48_hash(jsonb_build_array(
        jsonb_build_array(1, v_ctx.chunk1::text, v_ctx.chunk_hash1),
        jsonb_build_array(2, v_ctx.chunk2::text, v_ctx.chunk_hash2)
    ));

    SELECT * INTO v_run
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-normaldispute-resolved',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-a2', 60);
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'A', v_claim.lease_token, 'completed',
        jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-b2', 60);
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'B', v_claim.lease_token, 'completed',
        jsonb_build_object(
            'selected_option_labels', jsonb_build_array('A'),
            'proposed_findings', jsonb_build_array(
                jsonb_build_object('finding_ref', 'F1', 'summary', 'possible blocking defect'),
                jsonb_build_object('finding_ref', 'F2', 'summary', 'weak source support'),
                jsonb_build_object(
                    'finding_ref', 'F4',
                    'finding_code', 'DOMAIN_MISALIGNMENT',
                    'finding_type', 'coverage',
                    'severity', 'medium',
                    'materiality', 'blocking',
                    'title', 'Domain misalignment detected',
                    'description', 'Question domain does not match certification scope'
                ),
                jsonb_build_object('finding_ref', 'F3', 'summary', 'unconfirmed blocking claim')
            )
        )
    );

    PERFORM * FROM public.persist_audit_run_dispute_trigger_v1(
        v_run.audit_run_id, 'BLOCKING_DEFECT_PROPOSED', 'B', 'Pass B proposed a blocking defect',
        jsonb_build_array('F1', 'F3')
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-c2', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_C', 'S13: expected EXECUTE_PASS_C';

    -- C confirms F1 as blocking but explicitly does NOT confirm F3.
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'C', v_claim.lease_token, 'completed',
        jsonb_build_object(
            'resolution_type', 'NORMAL_DISPUTE', 'resolution_status', 'RESOLVED',
            'substituted_for_passes', '[]'::jsonb,
            'confirmed_finding_refs', jsonb_build_array('F1')
        )
    );

    -- F3 as materiality=blocking must be rejected (not in confirmed_finding_refs).
    BEGIN
        PERFORM * FROM public.complete_ai_quality_audit_run_v1(
            v_run.audit_run_id,
            jsonb_build_array(jsonb_build_object(
                'finding_ref', 'F3', 'finding_code', 'AMBIGUOUS_WORDING', 'finding_type', 'ambiguity',
                'severity', 'high', 'materiality', 'blocking', 'title', 't', 'description', 'd'
            ))
        );
        RAISE EXCEPTION 'S13_TEST_DID_NOT_RAISE: an unconfirmed blocking finding was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT;
            ASSERT v_msg LIKE '%not present in Pass C''s confirmed_finding_refs%',
                format('S13: expected an unconfirmed-blocking-finding message, got: %s', v_msg);
    END;

    -- Evidence outside the frozen evidence set must be rejected.
    BEGIN
        PERFORM * FROM public.complete_ai_quality_audit_run_v1(
            v_run.audit_run_id,
            jsonb_build_array(jsonb_build_object(
                'finding_ref', 'F1', 'finding_code', 'WRONG_ANSWER_KEY', 'finding_type', 'correctness',
                'severity', 'high', 'materiality', 'blocking', 'title', 't', 'description', 'd',
                'evidence', jsonb_build_array(jsonb_build_object(
                    'resource_chunk_id', v_ctx.foreign_chunk, 'evidence_role', 'supporting'
                ))
            ))
        );
        RAISE EXCEPTION 'S13_TEST_DID_NOT_RAISE: evidence outside the frozen evidence set was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT;
            ASSERT v_msg LIKE '%outside the frozen evidence set%',
                format('S13: expected an evidence-outside-frozen-set message, got: %s', v_msg);
    END;

    -- SOURCE_SUPPORT_WEAK forced to warning even when blocking requested,
    -- combined with zero-evidence requiring a complete source_support_context.
    BEGIN
        PERFORM * FROM public.complete_ai_quality_audit_run_v1(
            v_run.audit_run_id,
            jsonb_build_array(jsonb_build_object(
                'finding_ref', 'F2', 'finding_code', 'SOURCE_SUPPORT_WEAK', 'finding_type', 'source_support',
                'severity', 'medium', 'materiality', 'blocking', 'title', 't', 'description', 'd'
            ))
        );
        RAISE EXCEPTION 'S13_TEST_DID_NOT_RAISE: zero-evidence SOURCE_SUPPORT_WEAK without source_support_context was unexpectedly accepted';
    EXCEPTION
        WHEN invalid_parameter_value THEN
            GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT;
            ASSERT v_msg LIKE '%source_support_context%',
                format('S13: expected a source_support_context requirement message, got: %s', v_msg);
    END;

    SELECT * INTO v_rec FROM public.complete_ai_quality_audit_run_v1(
        v_run.audit_run_id,
        jsonb_build_array(
            jsonb_build_object(
                'finding_ref', 'F1', 'finding_code', 'WRONG_ANSWER_KEY', 'finding_type', 'correctness',
                'severity', 'high', 'materiality', 'blocking', 'title', 't', 'description', 'd'
            ),
            jsonb_build_object(
                'finding_ref', 'F2', 'finding_code', 'SOURCE_SUPPORT_WEAK', 'finding_type', 'source_support',
                'severity', 'medium', 'materiality', 'blocking', 'title', 't', 'description', 'd',
                'metadata', jsonb_build_object('source_support_context', jsonb_build_object(
                    'attempted_retrieval', 2,
                    'evidence_limitation', 'no official source addressed this claim',
                    'proposed_technical_claim', 'claim text',
                    'insufficiency_reason', 'no matching chunk retrieved'
                ))
            ),
            jsonb_build_object(
                'finding_ref', 'F4', 'finding_code', 'DOMAIN_MISALIGNMENT', 'finding_type', 'coverage',
                'severity', 'medium', 'materiality', 'blocking', 'title', 't', 'description', 'd'
            )
        )
    );
    ASSERT v_rec.run_status = 'completed', 'S13: RESOLVED NORMAL_DISPUTE with valid findings must complete';
    ASSERT v_rec.finding_count = 3, 'S13: all three findings must be inserted';
    ASSERT (
        SELECT materiality FROM public.audit_findings
        WHERE audit_run_id = v_run.audit_run_id AND finding_code = 'SOURCE_SUPPORT_WEAK'
    ) = 'warning', 'S13: SOURCE_SUPPORT_WEAK must be forced to warning materiality';
    ASSERT (
        SELECT materiality FROM public.audit_findings
        WHERE audit_run_id = v_run.audit_run_id AND finding_code = 'DOMAIN_MISALIGNMENT'
    ) = 'warning', 'S13: DOMAIN_MISALIGNMENT must be forced to warning materiality';
    ASSERT (
        SELECT materiality FROM public.audit_findings
        WHERE audit_run_id = v_run.audit_run_id AND finding_code = 'WRONG_ANSWER_KEY'
    ) = 'blocking', 'S13: a properly confirmed blocking finding must persist as blocking';

    -- A representative rejected combination: Pass C still running (not
    -- completed/skipped) must be rejected by the shape detector.
    DECLARE
        v_run2 record;
        v_claim2 record;
    BEGIN
        SELECT * INTO v_run2
        FROM public.create_or_get_ai_quality_audit_run_v1(
            v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
            'verify-model-primary', 'verify-model-dispute', 'v48-verify-rejected-combo',
            v_hash,
            jsonb_build_array(
                jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
                jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
            ),
            'v48-verification-script'
        );

        SELECT * INTO v_claim2 FROM public.claim_ai_quality_audit_pass_v1(v_run2.audit_run_id, 'worker-a3', 60);
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run2.audit_run_id, 'A', v_claim2.lease_token, 'completed',
            jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
        );
        SELECT * INTO v_claim2 FROM public.claim_ai_quality_audit_pass_v1(v_run2.audit_run_id, 'worker-b3', 60);
        PERFORM * FROM public.record_audit_pass_result_v1(
            v_run2.audit_run_id, 'B', v_claim2.lease_token, 'completed',
            jsonb_build_object(
                'selected_option_labels', jsonb_build_array('A'),
                'proposed_findings', jsonb_build_array(jsonb_build_object('finding_ref', 'F1', 'summary', 's'))
            )
        );
        PERFORM * FROM public.persist_audit_run_dispute_trigger_v1(
            v_run2.audit_run_id, 'BLOCKING_DEFECT_PROPOSED', 'B', 'trigger', jsonb_build_array('F1')
        );
        SELECT * INTO v_claim2 FROM public.claim_ai_quality_audit_pass_v1(v_run2.audit_run_id, 'worker-c3', 60);
        ASSERT v_claim2.action = 'EXECUTE_PASS_C', 'S13: expected EXECUTE_PASS_C (left running, not recorded)';

        BEGIN
            PERFORM * FROM public.complete_ai_quality_audit_run_v1(v_run2.audit_run_id, '[]'::jsonb);
            RAISE EXCEPTION 'S13_TEST_DID_NOT_RAISE: completion with Pass C still running was unexpectedly accepted';
        EXCEPTION
            WHEN invalid_parameter_value THEN
                GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT;
                ASSERT v_msg LIKE '%not an accepted completion path%',
                    format('S13: expected a not-an-accepted-completion-path message, got: %s', v_msg);
        END;
    END;

    INSERT INTO v48_behavioral_sections VALUES ('S13');
    RAISE NOTICE 'S13 (NORMAL_DISPUTE RESOLVED full completion + rejection matrix) passed';
END;
$$;

-- ---------------------------------------------------------------------------
-- S14: NORMAL_DISPUTE, UNRESOLVED — full completion path: run becomes
--      inconclusive and zero findings are inserted even when confirmed
--      findings are supplied.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx    v48_ctx;
    v_run    record;
    v_hash   text;
    v_claim  record;
    v_rec    record;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    v_hash := pg_temp.v48_hash(jsonb_build_array(
        jsonb_build_array(1, v_ctx.chunk1::text, v_ctx.chunk_hash1),
        jsonb_build_array(2, v_ctx.chunk2::text, v_ctx.chunk_hash2)
    ));

    SELECT * INTO v_run
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-normaldispute-unresolved',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-a4', 60);
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'A', v_claim.lease_token, 'completed',
        jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-b4', 60);
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'B', v_claim.lease_token, 'completed',
        jsonb_build_object(
            'selected_option_labels', jsonb_build_array('A'),
            'proposed_findings', jsonb_build_array(
                jsonb_build_object('finding_ref', 'F1', 'summary', 'possible blocking defect')
            )
        )
    );

    PERFORM * FROM public.persist_audit_run_dispute_trigger_v1(
        v_run.audit_run_id, 'BLOCKING_DEFECT_PROPOSED', 'B', 'Pass B proposed a blocking defect',
        jsonb_build_array('F1')
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-c4', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_C', 'S14: expected EXECUTE_PASS_C';

    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'C', v_claim.lease_token, 'completed',
        jsonb_build_object(
            'resolution_type', 'NORMAL_DISPUTE', 'resolution_status', 'UNRESOLVED',
            'substituted_for_passes', '[]'::jsonb,
            'confirmed_finding_refs', '[]'::jsonb
        )
    );

    SELECT * INTO v_rec FROM public.complete_ai_quality_audit_run_v1(
        v_run.audit_run_id,
        jsonb_build_array(jsonb_build_object(
            'finding_ref', 'F1', 'finding_code', 'WRONG_ANSWER_KEY', 'finding_type', 'correctness',
            'severity', 'high', 'materiality', 'blocking', 'title', 't', 'description', 'd'
        ))
    );
    ASSERT v_rec.run_status = 'inconclusive', 'S14: UNRESOLVED Pass C must mark the run inconclusive';
    ASSERT v_rec.finding_count = 0, 'S14: UNRESOLVED Pass C must insert zero findings';
    ASSERT (SELECT COUNT(*) FROM public.audit_findings WHERE audit_run_id = v_run.audit_run_id) = 0,
        'S14: UNRESOLVED Pass C must leave zero rows in audit_findings';

    INSERT INTO v48_behavioral_sections VALUES ('S14');
    RAISE NOTICE 'S14 (NORMAL_DISPUTE UNRESOLVED full completion) passed';
END;
$$;

-- ---------------------------------------------------------------------------
-- S15: PASS_A_SUBSTITUTION — Pass A schema-invalid after exactly two
--      attempts, no Pass B row, correct trigger, Pass C resolves with
--      PASS_A_SUBSTITUTION, full completion end to end.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx             v48_ctx;
    v_run             record;
    v_hash            text;
    v_claim           record;
    v_rec             record;
    v_status          text;
    v_attempt_count   integer;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    v_hash := pg_temp.v48_hash(jsonb_build_array(
        jsonb_build_array(1, v_ctx.chunk1::text, v_ctx.chunk_hash1),
        jsonb_build_array(2, v_ctx.chunk2::text, v_ctx.chunk_hash2)
    ));

    SELECT * INTO v_run
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-pass-a-substitution',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-x1', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_A', 'S15: expected EXECUTE_PASS_A';
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'A', v_claim.lease_token, 'schema_invalid',
        NULL, 'not valid json', jsonb_build_object('error', 'could not parse')
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-x2', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_A' AND v_claim.is_retry AND v_claim.attempt_count = 2,
        'S15: schema_invalid retry must be attempt 2';
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'A', v_claim.lease_token, 'schema_invalid',
        NULL, 'still not valid json', jsonb_build_object('error', 'still could not parse')
    );

    SELECT apr.status, apr.attempt_count
    INTO   v_status, v_attempt_count
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = v_run.audit_run_id
      AND  apr.pass_code = 'A';

    ASSERT v_status = 'schema_invalid'::text,
        'S15: Pass A must be schema_invalid with exactly attempt_count=2';
    ASSERT v_attempt_count = 2,
        'S15: Pass A must be schema_invalid with exactly attempt_count=2';

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-x3', 60);
    ASSERT v_claim.action = 'NEEDS_DISPUTE_TRIGGER_A', format('S15: expected NEEDS_DISPUTE_TRIGGER_A, got %', v_claim.action);

    PERFORM * FROM public.persist_audit_run_dispute_trigger_v1(
        v_run.audit_run_id, 'PASS_A_SCHEMA_INVALID', 'A', 'pass a unparseable twice', '[]'::jsonb
    );

    ASSERT NOT EXISTS (
        SELECT 1 FROM public.audit_run_pass_results WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'B'
    ), 'S15: Pass B must never be created on the Pass-A-substitution path';

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-x4', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_C', format('S15: expected EXECUTE_PASS_C, got %', v_claim.action);
    ASSERT v_claim.model_name = 'verify-model-dispute', 'S15: Pass C must use the dispute model';

    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'C', v_claim.lease_token, 'completed',
        jsonb_build_object(
            'resolution_type', 'PASS_A_SUBSTITUTION', 'resolution_status', 'RESOLVED',
            'substituted_for_passes', jsonb_build_array('A', 'B'),
            'proposed_findings', jsonb_build_array(
                jsonb_build_object('finding_ref', 'F1', 'summary', 'blocking defect found during substitution')
            ),
            'confirmed_finding_refs', jsonb_build_array('F1')
        )
    );

    ASSERT NOT EXISTS (
        SELECT 1 FROM public.audit_run_pass_results WHERE audit_run_id = v_run.audit_run_id AND pass_code = 'B'
    ), 'S15: Pass B must still not exist after Pass C resolves';

    SELECT * INTO v_rec FROM public.complete_ai_quality_audit_run_v1(
        v_run.audit_run_id,
        jsonb_build_array(jsonb_build_object(
            'finding_ref', 'F1', 'finding_code', 'WRONG_ANSWER_KEY', 'finding_type', 'correctness',
            'severity', 'high', 'materiality', 'blocking', 'title', 't', 'description', 'd'
        ))
    );
    ASSERT v_rec.run_status = 'completed', 'S15: PASS_A_SUBSTITUTION must complete end to end';
    ASSERT v_rec.finding_count = 1, 'S15: PASS_A_SUBSTITUTION must insert the confirmed finding';

    INSERT INTO v48_behavioral_sections VALUES ('S15');
    RAISE NOTICE 'S15 (PASS_A_SUBSTITUTION full completion) passed';
END;
$$;

-- ---------------------------------------------------------------------------
-- S16: PASS_B_SUBSTITUTION — Pass A completed normally, Pass B schema-
--      invalid after exactly two attempts, correct trigger, Pass C resolves
--      with PASS_B_SUBSTITUTION, full completion end to end.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ctx             v48_ctx;
    v_run             record;
    v_hash            text;
    v_claim           record;
    v_rec             record;
    v_status          text;
    v_attempt_count   integer;
BEGIN
    SELECT * INTO v_ctx FROM v48_ctx;

    v_hash := pg_temp.v48_hash(jsonb_build_array(
        jsonb_build_array(1, v_ctx.chunk1::text, v_ctx.chunk_hash1),
        jsonb_build_array(2, v_ctx.chunk2::text, v_ctx.chunk_hash2)
    ));

    SELECT * INTO v_run
    FROM public.create_or_get_ai_quality_audit_run_v1(
        v_ctx.question_version_id, 'verify-prompt-v1', 'verify-ruleset-v1',
        'verify-model-primary', 'verify-model-dispute', 'v48-verify-pass-b-substitution',
        v_hash,
        jsonb_build_array(
            jsonb_build_object('resource_chunk_id', v_ctx.chunk1, 'retrieval_rank', 1),
            jsonb_build_object('resource_chunk_id', v_ctx.chunk2, 'retrieval_rank', 2)
        ),
        'v48-verification-script'
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-y1', 60);
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'A', v_claim.lease_token, 'completed',
        jsonb_build_object('selected_option_labels', jsonb_build_array('A'))
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-y2', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_B', format('S16: expected EXECUTE_PASS_B, got %', v_claim.action);
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'B', v_claim.lease_token, 'schema_invalid',
        NULL, 'not valid json', jsonb_build_object('error', 'could not parse')
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-y3', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_B' AND v_claim.is_retry AND v_claim.attempt_count = 2,
        'S16: schema_invalid retry must be attempt 2';
    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'B', v_claim.lease_token, 'schema_invalid',
        NULL, 'still not valid json', jsonb_build_object('error', 'still could not parse')
    );

    SELECT apr.status, apr.attempt_count
    INTO   v_status, v_attempt_count
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = v_run.audit_run_id
      AND  apr.pass_code = 'B';

    ASSERT v_status = 'schema_invalid'::text,
        'S16: Pass B must be schema_invalid with exactly attempt_count=2';
    ASSERT v_attempt_count = 2,
        'S16: Pass B must be schema_invalid with exactly attempt_count=2';

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-y4', 60);
    ASSERT v_claim.action = 'NEEDS_DISPUTE_TRIGGER_B', format('S16: expected NEEDS_DISPUTE_TRIGGER_B, got %', v_claim.action);

    PERFORM * FROM public.persist_audit_run_dispute_trigger_v1(
        v_run.audit_run_id, 'PASS_B_SCHEMA_INVALID', 'B', 'pass b unparseable twice', '[]'::jsonb
    );

    SELECT * INTO v_claim FROM public.claim_ai_quality_audit_pass_v1(v_run.audit_run_id, 'worker-y5', 60);
    ASSERT v_claim.action = 'EXECUTE_PASS_C', format('S16: expected EXECUTE_PASS_C, got %', v_claim.action);

    PERFORM * FROM public.record_audit_pass_result_v1(
        v_run.audit_run_id, 'C', v_claim.lease_token, 'completed',
        jsonb_build_object(
            'resolution_type', 'PASS_B_SUBSTITUTION', 'resolution_status', 'RESOLVED',
            'substituted_for_passes', jsonb_build_array('B'),
            'proposed_findings', jsonb_build_array(
                jsonb_build_object('finding_ref', 'F1', 'summary', 'blocking defect found during substitution')
            ),
            'confirmed_finding_refs', jsonb_build_array('F1')
        )
    );

    SELECT * INTO v_rec FROM public.complete_ai_quality_audit_run_v1(
        v_run.audit_run_id,
        jsonb_build_array(jsonb_build_object(
            'finding_ref', 'F1', 'finding_code', 'WRONG_ANSWER_KEY', 'finding_type', 'correctness',
            'severity', 'high', 'materiality', 'blocking', 'title', 't', 'description', 'd'
        ))
    );
    ASSERT v_rec.run_status = 'completed', 'S16: PASS_B_SUBSTITUTION must complete end to end';
    ASSERT v_rec.finding_count = 1, 'S16: PASS_B_SUBSTITUTION must insert the confirmed finding';

    INSERT INTO v48_behavioral_sections VALUES ('S16');
    RAISE NOTICE 'S16 (PASS_B_SUBSTITUTION full completion) passed';
END;
$$;

DO $$
DECLARE
    v_expected constant text[] := ARRAY[
        'S8', 'S9', 'S10', 'S11', 'S11b', 'S12', 'S13', 'S14', 'S15', 'S16'
    ];
    v_missing text;
BEGIN
    SELECT string_agg(e.section, ', ' ORDER BY e.section)
    INTO   v_missing
    FROM   unnest(v_expected) AS e(section)
    WHERE  e.section NOT IN (SELECT s.section FROM v48_behavioral_sections s);

    ASSERT v_missing IS NULL,
        format(
            'Behavioral coverage incomplete: expected %s sections, ran %s; missing: %s',
            array_length(v_expected, 1),
            (SELECT COUNT(*) FROM v48_behavioral_sections),
            COALESCE(v_missing, '(none)')
        );
END;
$$;

DO $$
BEGIN
    RAISE NOTICE '== V48 ai_quality audit verification complete (rolling back next) ==';
END;
$$;

-- Nothing executed above is persisted.
ROLLBACK;
