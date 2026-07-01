-- =============================================================================
-- V48 Phase 2: AI Quality-Audit RPCs (10-question smoke slice)
-- Created : 2026-06-30 13:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Implements every database write/read path required by the three-pass
-- ai_quality audit (blind Pass A, evidence-backed Pass B, independent
-- dispute Pass C), so that ALL writes for this pipeline go through RPCs:
--
--   get_question_version_blind_context_v1       — Pass A context (redacted)
--   get_question_version_comparison_context_v1   — Pass B/C context (full)
--   list_audit_candidate_resource_chunks_v1      — evidence-chunk candidates
--   create_or_get_ai_quality_audit_run_v1        — idempotent run creation
--   claim_ai_quality_audit_pass_v1               — atomic pass claim/lease
--   record_audit_pass_result_v1                  — atomic pass finalization
--   persist_audit_run_dispute_trigger_v1          — Pass C eligibility gate
--   complete_ai_quality_audit_run_v1              — final completion matrix
--
-- Schema corrections vs. the original plan (documented per repository
-- inspection; see migration footer comment for the full list):
--   * resource_versions has no "status" / "completed" column. Versions are
--     written atomically with all their chunks by ingest_resource_version_v1,
--     so "latest completed version" is implemented as "highest
--     version_number for that resource_id" — there is no partial/incomplete
--     version state in this schema.
--   * Certification name lives on questions.exam_name (joined via
--     question_versions.question_id), not on question_versions itself.
--   * Domain/category lives on question_versions.category.
--
-- Security
-- --------
--   Every function below is SECURITY INVOKER with an explicit search_path.
--   EXECUTE is revoked from PUBLIC, anon, authenticated and granted only
--   to service_role. The blind-context RPC never selects is_correct,
--   stored answers, explanation, or any prior audit data.
-- =============================================================================

-- pgcrypto provides digest() for server-side SHA-256 evidence-set hashing.
-- On Supabase this extension is commonly installed into the `extensions`
-- schema rather than `public`; IF NOT EXISTS is a no-op either way.
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- =============================================================================
-- 1. get_question_version_blind_context_v1
--    Pass A (blind solve) context. Must never expose is_correct, stored
--    correct answers, explanation, or any prior audit result/finding.
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_question_version_blind_context_v1(
    p_question_version_id uuid
)
RETURNS TABLE (
    question_version_id      uuid,
    question_id                integer,
    certification_exam_name     text,
    domain_name                  text,
    question_text                 text,
    question_type                  text,
    select_count                     integer,
    options                            jsonb
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_qv record;
BEGIN
    SELECT qv.id, qv.question_id, q.exam_name, qv.category,
           qv.question_text, qv.question_type, qv.select_count
    INTO   v_qv
    FROM   public.question_versions qv
    JOIN   public.questions q ON q.id = qv.question_id
    WHERE  qv.id = p_question_version_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'question_version not found: %', p_question_version_id
            USING ERRCODE = 'no_data_found';
    END IF;

    RETURN QUERY
    SELECT
        v_qv.id,
        v_qv.question_id,
        v_qv.exam_name::text,
        v_qv.category::text,
        v_qv.question_text::text,
        v_qv.question_type::text,
        v_qv.select_count,
        COALESCE(
            (
                SELECT jsonb_agg(
                           jsonb_build_object(
                               'option_label', qov.option_label,
                               'option_text', qov.option_text,
                               'display_order', qov.display_order
                           )
                           ORDER BY qov.display_order
                       )
                FROM   public.question_option_versions qov
                WHERE  qov.question_version_id = v_qv.id
            ),
            '[]'::jsonb
        );
END;
$$;

REVOKE ALL ON FUNCTION public.get_question_version_blind_context_v1(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_question_version_blind_context_v1(uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.get_question_version_blind_context_v1(uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_question_version_blind_context_v1(uuid) TO service_role;

COMMENT ON FUNCTION public.get_question_version_blind_context_v1(uuid) IS
'Returns the redacted Pass-A (blind solve) context for one question_version:
identity, certification, domain/category, question text/type/select_count,
and ordered public options (label/text/display_order only). Never returns
is_correct, stored correct answers, explanation, or any prior audit data.
Execute permission: service_role only.';


-- =============================================================================
-- 2. get_question_version_comparison_context_v1
--    Pass B / Pass C context. Requires the supplied audit_run to target the
--    same question_version AND for Pass A to already be completed.
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_question_version_comparison_context_v1(
    p_question_version_id uuid,
    p_audit_run_id uuid
)
RETURNS TABLE (
    question_version_id          uuid,
    question_id                     integer,
    certification_exam_name           text,
    domain_name                          text,
    question_text                          text,
    question_type                             text,
    select_count                                 integer,
    explanation                                     text,
    options                                            jsonb,
    stored_correct_option_labels                          jsonb
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_run_target    uuid;
    v_pass_a_status text;
    v_qv            record;
BEGIN
    SELECT ar.target_question_version_id
    INTO   v_run_target
    FROM   public.audit_runs ar
    WHERE  ar.id = p_audit_run_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_run not found: %', p_audit_run_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_run_target IS DISTINCT FROM p_question_version_id THEN
        RAISE EXCEPTION
            'audit_run % does not target question_version %',
            p_audit_run_id, p_question_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT arpr.status
    INTO   v_pass_a_status
    FROM   public.audit_run_pass_results arpr
    WHERE  arpr.audit_run_id = p_audit_run_id
      AND  arpr.pass_code    = 'A';

    IF NOT FOUND OR v_pass_a_status IS DISTINCT FROM 'completed' THEN
        RAISE EXCEPTION
            'comparison context denied: Pass A is not completed for audit_run %',
            p_audit_run_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    SELECT qv.id, qv.question_id, q.exam_name, qv.category,
           qv.question_text, qv.question_type, qv.select_count, qv.explanation
    INTO   v_qv
    FROM   public.question_versions qv
    JOIN   public.questions q ON q.id = qv.question_id
    WHERE  qv.id = p_question_version_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'question_version not found: %', p_question_version_id
            USING ERRCODE = 'no_data_found';
    END IF;

    RETURN QUERY
    SELECT
        v_qv.id, v_qv.question_id, v_qv.exam_name::text, v_qv.category::text,
        v_qv.question_text::text, v_qv.question_type::text, v_qv.select_count, v_qv.explanation::text,
        COALESCE(
            (
                SELECT jsonb_agg(
                           jsonb_build_object(
                               'option_label', qov.option_label,
                               'option_text', qov.option_text,
                               'display_order', qov.display_order,
                               'is_correct', qov.is_correct
                           )
                           ORDER BY qov.display_order
                       )
                FROM   public.question_option_versions qov
                WHERE  qov.question_version_id = v_qv.id
            ),
            '[]'::jsonb
        ),
        COALESCE(
            (
                SELECT jsonb_agg(qov.option_label ORDER BY qov.display_order)
                FROM   public.question_option_versions qov
                WHERE  qov.question_version_id = v_qv.id
                  AND  qov.is_correct = TRUE
            ),
            '[]'::jsonb
        );
END;
$$;

REVOKE ALL ON FUNCTION public.get_question_version_comparison_context_v1(uuid, uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_question_version_comparison_context_v1(uuid, uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.get_question_version_comparison_context_v1(uuid, uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_question_version_comparison_context_v1(uuid, uuid) TO service_role;

COMMENT ON FUNCTION public.get_question_version_comparison_context_v1(uuid, uuid) IS
'Returns the full Pass-B/Pass-C comparison context (explanation, options with
is_correct, derived stored-correct option labels) for one question_version.
Rejects access unless the supplied audit_run targets the same question_version
AND its Pass A is status=completed.
Execute permission: service_role only.';


-- =============================================================================
-- 3. list_audit_candidate_resource_chunks_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.list_audit_candidate_resource_chunks_v1(
    p_certification_exam_name text,
    p_resource_ids uuid[],
    p_max_chunks integer DEFAULT 50
)
RETURNS TABLE (
    resource_chunk_id          uuid,
    resource_id                  uuid,
    resource_version_id            uuid,
    resource_version_number           integer,
    certification_exam_name              text,
    resource_type                          text,
    title                                     text,
    canonical_url                              text,
    chunk_index                                  integer,
    content_hash                                   text,
    chunk_text                                       text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
BEGIN
    IF COALESCE(TRIM(p_certification_exam_name), '') = '' THEN
        RAISE EXCEPTION 'p_certification_exam_name must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_resource_ids IS NULL OR array_length(p_resource_ids, 1) IS NULL THEN
        RAISE EXCEPTION 'p_resource_ids must contain at least one resource id'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(p_max_chunks, 0) < 1 OR COALESCE(p_max_chunks, 0) > 200 THEN
        RAISE EXCEPTION 'p_max_chunks must be between 1 and 200, got: %', p_max_chunks
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- NOTE (schema correction): resource_versions has no status/"completed"
    -- column. Every stored version was already written atomically with all
    -- of its chunks by ingest_resource_version_v1, so "latest completed
    -- version" is implemented here as "highest version_number per resource".
    RETURN QUERY
    SELECT
        rc.id,
        orr.id,
        rv.id,
        rv.version_number,
        orr.certification_exam_name::text,
        orr.resource_type::text,
        orr.title::text,
        orr.canonical_url::text,
        rc.chunk_index,
        rc.content_hash::text,
        rc.chunk_text::text
    FROM   public.official_resources orr
    JOIN LATERAL (
        SELECT v.id, v.version_number
        FROM   public.resource_versions v
        WHERE  v.resource_id = orr.id
        ORDER  BY v.version_number DESC, v.id DESC
        LIMIT  1
    ) AS rv ON TRUE
    JOIN   public.resource_chunks rc ON rc.resource_version_id = rv.id
    WHERE  orr.id = ANY(p_resource_ids)
      AND  orr.certification_exam_name = TRIM(p_certification_exam_name)
      AND  orr.is_active = TRUE
    ORDER BY orr.id ASC, rc.chunk_index ASC
    LIMIT  p_max_chunks;
END;
$$;

REVOKE ALL ON FUNCTION public.list_audit_candidate_resource_chunks_v1(text, uuid[], integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.list_audit_candidate_resource_chunks_v1(text, uuid[], integer) FROM anon;
REVOKE EXECUTE ON FUNCTION public.list_audit_candidate_resource_chunks_v1(text, uuid[], integer) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.list_audit_candidate_resource_chunks_v1(text, uuid[], integer) TO service_role;

COMMENT ON FUNCTION public.list_audit_candidate_resource_chunks_v1(text, uuid[], integer) IS
'Returns candidate evidence chunks for one certification from the latest
version (highest version_number; resource_versions has no status column) of
each active official_resource in p_resource_ids, deterministically ordered
by (resource_id, chunk_index), bounded by p_max_chunks (<=200).
Execute permission: service_role only.';


-- =============================================================================
-- 4. create_or_get_ai_quality_audit_run_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.create_or_get_ai_quality_audit_run_v1(
    p_target_question_version_id uuid,
    p_prompt_version text,
    p_ruleset_version text,
    p_primary_model_name text,
    p_dispute_model_name text,
    p_pilot_batch_id text,
    p_evidence_set_hash text,
    p_evidence_chunks jsonb,
    p_created_by text,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    audit_run_id     uuid,
    run_status        text,
    created             boolean,
    evidence_set_hash      text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, extensions, pg_catalog
AS $$
DECLARE
    v_certification_exam_name text;
    v_i                       integer;
    v_chunk                   jsonb;
    v_chunk_id                uuid;
    v_rank                    integer;
    v_seen_chunk_ids          uuid[] := '{}';
    v_seen_ranks              integer[] := '{}';
    v_chunk_row               record;
    -- Canonical evidence-set representation: parallel arrays of
    -- (retrieval_rank, [retrieval_rank, resource_chunk_id, content_hash])
    -- collected in input order, then re-assembled into a single jsonb array
    -- ordered by retrieval_rank (see step 6/7 below for the exact encoding).
    v_canonical_ranks         integer[] := '{}';
    v_canonical_entries       jsonb[] := '{}';
    v_canonical_json          jsonb;
    v_server_hash             text;
    v_dedup_lock_json         jsonb;
    v_existing_run_id         uuid;
    v_existing_run_status     text;
    v_new_run_id              uuid;
BEGIN
    -- -------------------------------------------------------------------------
    -- 1. Validate required scalar text inputs.
    -- -------------------------------------------------------------------------
    IF COALESCE(TRIM(p_prompt_version), '') = '' THEN
        RAISE EXCEPTION 'p_prompt_version must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF COALESCE(TRIM(p_ruleset_version), '') = '' THEN
        RAISE EXCEPTION 'p_ruleset_version must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF COALESCE(TRIM(p_primary_model_name), '') = '' THEN
        RAISE EXCEPTION 'p_primary_model_name must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF COALESCE(TRIM(p_dispute_model_name), '') = '' THEN
        RAISE EXCEPTION 'p_dispute_model_name must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF COALESCE(TRIM(p_pilot_batch_id), '') = '' THEN
        RAISE EXCEPTION 'p_pilot_batch_id must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF COALESCE(TRIM(p_created_by), '') = '' THEN
        RAISE EXCEPTION 'p_created_by must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_evidence_set_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'p_evidence_set_hash must be a lowercase 64-character SHA-256 hex value'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- 2/3. Validate target question_version exists; resolve certification.
    -- -------------------------------------------------------------------------
    SELECT q.exam_name
    INTO   v_certification_exam_name
    FROM   public.question_versions qv
    JOIN   public.questions q ON q.id = qv.question_id
    WHERE  qv.id = p_target_question_version_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'question_version not found: %', p_target_question_version_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- -------------------------------------------------------------------------
    -- 4. Validate p_evidence_chunks shape and every evidence chunk.
    --    Zero evidence chunks is allowed (e.g. SOURCE_SUPPORT_WEAK pilot
    --    questions with no usable official-resource coverage).
    -- -------------------------------------------------------------------------
    IF jsonb_typeof(COALESCE(p_evidence_chunks, '[]'::jsonb)) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_evidence_chunks must be a JSON array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    FOR v_i IN 0 .. jsonb_array_length(COALESCE(p_evidence_chunks, '[]'::jsonb)) - 1 LOOP
        v_chunk := COALESCE(p_evidence_chunks, '[]'::jsonb) -> v_i;

        BEGIN
            v_chunk_id := (v_chunk ->> 'resource_chunk_id')::uuid;
            v_rank     := (v_chunk ->> 'retrieval_rank')::integer;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION
                'evidence chunk % has invalid or missing resource_chunk_id/retrieval_rank',
                v_i
                USING ERRCODE = 'invalid_parameter_value';
        END;

        IF v_chunk_id IS NULL THEN
            RAISE EXCEPTION 'evidence chunk % is missing resource_chunk_id', v_i
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_rank IS NULL OR v_rank <= 0 THEN
            RAISE EXCEPTION
                'evidence chunk % has invalid retrieval_rank (must be a positive integer): %',
                v_i, v_rank
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_chunk_id = ANY(v_seen_chunk_ids) THEN
            RAISE EXCEPTION 'evidence chunk % (resource_chunk_id=%) appears more than once',
                v_i, v_chunk_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_rank = ANY(v_seen_ranks) THEN
            RAISE EXCEPTION 'evidence chunk % has duplicate retrieval_rank: %', v_i, v_rank
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        v_seen_chunk_ids := v_seen_chunk_ids || v_chunk_id;
        v_seen_ranks     := v_seen_ranks || v_rank;

        -- 5. Load the authoritative current content hash and validate the
        --    chunk belongs to an active resource, latest resource version,
        --    and the question's certification.
        SELECT rc.content_hash
        INTO   v_chunk_row
        FROM   public.resource_chunks rc
        JOIN   public.resource_versions rv ON rv.id = rc.resource_version_id
        JOIN   public.official_resources orr ON orr.id = rv.resource_id
        WHERE  rc.id = v_chunk_id
          AND  orr.is_active = TRUE
          AND  orr.certification_exam_name = v_certification_exam_name
          AND  rv.version_number = (
                   SELECT MAX(rv2.version_number)
                   FROM   public.resource_versions rv2
                   WHERE  rv2.resource_id = rv.resource_id
               );

        IF NOT FOUND THEN
            RAISE EXCEPTION
                'evidence chunk % (resource_chunk_id=%) does not exist, is not on an active resource''s latest version, or does not match certification %',
                v_i, v_chunk_id, v_certification_exam_name
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        v_canonical_ranks   := v_canonical_ranks || v_rank;
        v_canonical_entries := v_canonical_entries ||
            jsonb_build_array(v_rank, v_chunk_id::text, v_chunk_row.content_hash);
    END LOOP;

    -- -------------------------------------------------------------------------
    -- 6/7. Recompute the evidence-set hash server-side and reject mismatch.
    --
    --      Canonical form (must be reproduced identically by worker Python):
    --      a single JSON array, ordered ascending by retrieval_rank, whose
    --      elements are each a 3-element JSON array of exactly:
    --        [ retrieval_rank (JSON number, integer),
    --          resource_chunk_id (JSON string, canonical lowercase UUID text),
    --          content_hash (JSON string, the authoritative
    --            resource_chunks.content_hash value read server-side) ]
    --      e.g. [[1, "11111111-1111-1111-1111-111111111111", "abc..."],
    --            [2, "22222222-2222-2222-2222-222222222222", "def..."]].
    --      An empty evidence set canonicalizes to the JSON array [].
    --      The hash is SHA-256 over the UTF-8 bytes of this array's exact
    --      PostgreSQL jsonb text serialization (jsonb::text): elements
    --      separated by ", " (comma-space), integers as bare digits, strings
    --      double-quoted with standard JSON escaping, no extra whitespace —
    --      i.e. exactly what `to_json`/`jsonb` output produces, NOT
    --      Python's default json.dumps() spacing. Workers must serialize
    --      with matching separators (", " and ": ") to reproduce this hash.
    -- -------------------------------------------------------------------------
    SELECT COALESCE(jsonb_agg(entry ORDER BY rnk), '[]'::jsonb)
    INTO   v_canonical_json
    FROM   unnest(v_canonical_ranks, v_canonical_entries) AS t(rnk, entry);

    v_server_hash := encode(digest(v_canonical_json::text, 'sha256'), 'hex');

    IF v_server_hash <> p_evidence_set_hash THEN
        RAISE EXCEPTION
            'evidence_set_hash mismatch: server-computed % does not match proposed %',
            v_server_hash, p_evidence_set_hash
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- 8. Serialize all callers proposing this exact seven-key combination
    --    using a transaction-scoped advisory lock (same pattern already
    --    used by complete_audit_run_v1's per-question-version lock).
    --
    --    The lock source is a canonical JSON array of the seven dedup values
    --    in fixed order (NOT delimiter-joined text), so no combination of
    --    caller-controlled strings can serialize to the same source as a
    --    materially different combination. This lock is purely a liveness/
    --    performance optimization: the authoritative deduplication boundary
    --    is the exact-column SELECT below plus the
    --    audit_run_dedup_keys_seven_key_unique constraint. A hashtext()
    --    collision between two unrelated lock sources can only cause
    --    unrelated callers to serialize against each other; it can never
    --    cause two materially different combinations to be treated as one,
    --    because the actual dedup check compares the real column values,
    --    not the lock source.
    -- -------------------------------------------------------------------------
    v_dedup_lock_json := jsonb_build_array(
        p_target_question_version_id::text,
        p_prompt_version,
        p_ruleset_version,
        p_primary_model_name,
        p_dispute_model_name,
        v_server_hash,
        p_pilot_batch_id
    );

    PERFORM pg_advisory_xact_lock(hashtext(v_dedup_lock_json::text));

    SELECT dk.audit_run_id, ar.run_status
    INTO   v_existing_run_id, v_existing_run_status
    FROM   public.audit_run_dedup_keys dk
    JOIN   public.audit_runs ar ON ar.id = dk.audit_run_id
    WHERE  dk.target_question_version_id = p_target_question_version_id
      AND  dk.prompt_version             = p_prompt_version
      AND  dk.ruleset_version             = p_ruleset_version
      AND  dk.primary_model_name           = p_primary_model_name
      AND  dk.dispute_model_name             = p_dispute_model_name
      AND  dk.evidence_set_hash               = v_server_hash
      AND  dk.pilot_batch_id                   = p_pilot_batch_id;

    -- -------------------------------------------------------------------------
    -- 9/10. Duplicate key: return the existing run untouched. New key:
    --       create audit_runs + audit_run_dedup_keys + frozen evidence set.
    -- -------------------------------------------------------------------------
    IF FOUND THEN
        RETURN QUERY
            SELECT v_existing_run_id, v_existing_run_status::text, FALSE, v_server_hash::text;
        RETURN;
    END IF;

    v_new_run_id := gen_random_uuid();

    INSERT INTO public.audit_runs (
        id, audit_type, run_status, model_name, prompt_version, ruleset_version,
        target_question_version_id, created_by, metadata
    ) VALUES (
        v_new_run_id, 'ai_quality', 'pending', p_primary_model_name, p_prompt_version, p_ruleset_version,
        p_target_question_version_id, p_created_by, COALESCE(p_metadata, '{}'::jsonb)
    );

    INSERT INTO public.audit_run_dedup_keys (
        audit_run_id, target_question_version_id, prompt_version, ruleset_version,
        primary_model_name, dispute_model_name, evidence_set_hash, pilot_batch_id
    ) VALUES (
        v_new_run_id, p_target_question_version_id, p_prompt_version, p_ruleset_version,
        p_primary_model_name, p_dispute_model_name, v_server_hash, p_pilot_batch_id
    );

    FOR v_i IN 0 .. jsonb_array_length(COALESCE(p_evidence_chunks, '[]'::jsonb)) - 1 LOOP
        v_chunk    := COALESCE(p_evidence_chunks, '[]'::jsonb) -> v_i;
        v_chunk_id := (v_chunk ->> 'resource_chunk_id')::uuid;
        v_rank     := (v_chunk ->> 'retrieval_rank')::integer;

        INSERT INTO public.audit_run_evidence_set (
            audit_run_id, resource_chunk_id, retrieval_rank, relevance_score,
            content_hash_at_execution, metadata
        )
        SELECT
            v_new_run_id, v_chunk_id, v_rank,
            (v_chunk ->> 'relevance_score')::numeric,
            rc.content_hash,
            COALESCE((v_chunk -> 'metadata')::jsonb, '{}'::jsonb)
        FROM public.resource_chunks rc
        WHERE rc.id = v_chunk_id;
    END LOOP;

    -- Do not return may_execute_pass_a — every worker must claim a pass
    -- separately via claim_ai_quality_audit_pass_v1.
    RETURN QUERY
        SELECT v_new_run_id, 'pending'::text, TRUE, v_server_hash::text;
END;
$$;

REVOKE ALL ON FUNCTION public.create_or_get_ai_quality_audit_run_v1(
    uuid, text, text, text, text, text, text, jsonb, text, jsonb
) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.create_or_get_ai_quality_audit_run_v1(
    uuid, text, text, text, text, text, text, jsonb, text, jsonb
) FROM anon;
REVOKE EXECUTE ON FUNCTION public.create_or_get_ai_quality_audit_run_v1(
    uuid, text, text, text, text, text, text, jsonb, text, jsonb
) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.create_or_get_ai_quality_audit_run_v1(
    uuid, text, text, text, text, text, text, jsonb, text, jsonb
) TO service_role;

COMMENT ON FUNCTION public.create_or_get_ai_quality_audit_run_v1(
    uuid, text, text, text, text, text, text, jsonb, text, jsonb
) IS
'Atomically creates (or returns the existing) ai_quality audit_run for the
seven-key combination (question_version, prompt_version, ruleset_version,
primary_model_name, dispute_model_name, evidence_set_hash, pilot_batch_id).
Recomputes evidence_set_hash server-side from the authoritative current
resource_chunks.content_hash and rejects any client-proposed mismatch.
Canonical evidence representation: a JSON array ordered by retrieval_rank of
[retrieval_rank, resource_chunk_id, content_hash] triples (empty evidence
canonicalizes to []), SHA-256 hashed over that array''s exact jsonb::text
serialization; see the inline step 6/7 comment for the byte-exact format
workers must reproduce. Persists model_name/prompt_version/ruleset_version
onto audit_runs itself (not only audit_run_dedup_keys). The seven-key
advisory lock is built from a canonical JSON array of the seven values, not
delimiter-joined text, so it cannot silently collapse two materially
different combinations; the exact-column SELECT plus the
audit_run_dedup_keys_seven_key_unique constraint remain the sole
authoritative deduplication backstop regardless of any lock collision.
Does not return may_execute_pass_a; callers must claim a pass separately via
claim_ai_quality_audit_pass_v1.
Execute permission: service_role only.';


-- =============================================================================
-- 5. claim_ai_quality_audit_pass_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.claim_ai_quality_audit_pass_v1(
    p_audit_run_id uuid,
    p_worker_id text,
    p_lease_seconds integer DEFAULT 300
)
RETURNS TABLE (
    audit_run_id     uuid,
    action             text,
    pass_code            text,
    lease_token             uuid,
    attempt_count             integer,
    model_name                  text,
    is_retry                       boolean,
    run_status                       text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_run_status      text;
    v_primary_model   text;
    v_dispute_model   text;
    v_pass_a          public.audit_run_pass_results;
    v_pass_b          public.audit_run_pass_results;
    v_pass_c          public.audit_run_pass_results;
    v_trigger         public.audit_run_dispute_triggers;
    v_new_token       uuid;
    v_prospective     integer;
    v_proceed_to_b    boolean := false;
    v_proceed_to_c    boolean := false;
    v_skip_c_check    boolean := false;
BEGIN
    IF COALESCE(TRIM(p_worker_id), '') = '' THEN
        RAISE EXCEPTION 'p_worker_id must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF COALESCE(p_lease_seconds, -1) < 30 OR COALESCE(p_lease_seconds, -1) > 3600 THEN
        RAISE EXCEPTION 'p_lease_seconds must be between 30 and 3600, got: %', p_lease_seconds
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT ar.run_status, dk.primary_model_name, dk.dispute_model_name
    INTO   v_run_status, v_primary_model, v_dispute_model
    FROM   public.audit_runs ar
    JOIN   public.audit_run_dedup_keys dk ON dk.audit_run_id = ar.id
    WHERE  ar.id = p_audit_run_id
    FOR UPDATE OF ar;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_run not found or missing dedup keys: %', p_audit_run_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_run_status = 'completed' THEN
        RETURN QUERY SELECT p_audit_run_id, 'RUN_COMPLETE'::text, NULL::text, NULL::uuid, NULL::integer, NULL::text, NULL::boolean, v_run_status::text;
        RETURN;
    END IF;
    IF v_run_status = 'inconclusive' THEN
        RETURN QUERY SELECT p_audit_run_id, 'RUN_INCONCLUSIVE'::text, NULL::text, NULL::uuid, NULL::integer, NULL::text, NULL::boolean, v_run_status::text;
        RETURN;
    END IF;

    IF v_run_status = 'pending' THEN
        UPDATE public.audit_runs AS ar
        SET    run_status = 'running', started_at = COALESCE(ar.started_at, now())
        WHERE  ar.id = p_audit_run_id;
        v_run_status := 'running';
    END IF;

    -- -------------------------------------------------------------------------
    -- Pass A
    -- -------------------------------------------------------------------------
    SELECT apr.*
    INTO   v_pass_a
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = 'A'
    FOR UPDATE;

    IF NOT FOUND THEN
        v_new_token := gen_random_uuid();
        INSERT INTO public.audit_run_pass_results (
            audit_run_id, pass_code, status, model_name, prompt_version,
            schema_version, input_hash, attempt_count,
            lease_owner, lease_token, lease_expires_at, started_at, claimed_at
        )
        SELECT p_audit_run_id, 'A', 'running', v_primary_model, dk.prompt_version,
               '', '', 1,
               p_worker_id, v_new_token, now() + (p_lease_seconds || ' seconds')::interval, now(), now()
        FROM   public.audit_run_dedup_keys dk
        WHERE  dk.audit_run_id = p_audit_run_id;

        RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_A'::text, 'A'::text, v_new_token, 1, v_primary_model::text, FALSE, v_run_status::text;
        RETURN;
    END IF;

    IF v_pass_a.status = 'running' THEN
        IF v_pass_a.lease_expires_at >= now() THEN
            RETURN QUERY SELECT p_audit_run_id, 'WAIT'::text, 'A'::text, NULL::uuid, v_pass_a.attempt_count, v_primary_model::text, NULL::boolean, v_run_status::text;
            RETURN;
        END IF;

        v_prospective := v_pass_a.attempt_count + 1;
        IF v_prospective <= 2 THEN
            v_new_token := gen_random_uuid();
            UPDATE public.audit_run_pass_results AS apr
            SET    attempt_count = v_prospective, lease_owner = p_worker_id,
                   lease_token = v_new_token,
                   lease_expires_at = now() + (p_lease_seconds || ' seconds')::interval,
                   claimed_at = now(), updated_at = now()
            WHERE  apr.id = v_pass_a.id;

            RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_A'::text, 'A'::text, v_new_token, v_prospective, v_primary_model::text, TRUE, v_run_status::text;
            RETURN;
        ELSE
            -- Cap exceeded: no third provider attempt is performed. The
            -- stored attempt_count is left unchanged (it already reflects
            -- the two real attempts that occurred); only last_error records
            -- the prospective (never executed) third attempt number.
            UPDATE public.audit_run_pass_results AS apr
            SET    status = 'failed',
                   last_error = jsonb_build_object(
                       'error_code', 'PASS_LEASE_ATTEMPT_LIMIT_EXCEEDED',
                       'previous_lease_owner', v_pass_a.lease_owner,
                       'previous_lease_expires_at', v_pass_a.lease_expires_at,
                       'attempt_count', v_pass_a.attempt_count,
                       'prospective_attempt_count', v_prospective
                   ),
                   lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                   completed_at = now(), updated_at = now()
            WHERE  apr.id = v_pass_a.id;

            UPDATE public.audit_runs AS ar SET run_status = 'inconclusive' WHERE ar.id = p_audit_run_id;

            RETURN QUERY SELECT p_audit_run_id, 'RUN_INCONCLUSIVE'::text, 'A'::text, NULL::uuid, v_pass_a.attempt_count, v_primary_model::text, NULL::boolean, 'inconclusive'::text;
            RETURN;
        END IF;
    ELSIF v_pass_a.status = 'schema_invalid' THEN
        IF v_pass_a.attempt_count < 2 THEN
            v_new_token := gen_random_uuid();
            UPDATE public.audit_run_pass_results AS apr
            SET    status = 'running', attempt_count = apr.attempt_count + 1,
                   result_json = NULL, raw_response_text = NULL,
                   schema_validation_errors = NULL, last_error = NULL,
                   provider_request_id = NULL, input_tokens = NULL,
                   output_tokens = NULL, actual_cost_usd = NULL,
                   lease_owner = p_worker_id, lease_token = v_new_token,
                   lease_expires_at = now() + (p_lease_seconds || ' seconds')::interval,
                   claimed_at = now(), completed_at = NULL, updated_at = now()
            WHERE  apr.id = v_pass_a.id
            RETURNING apr.attempt_count INTO v_prospective;

            RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_A'::text, 'A'::text, v_new_token, v_prospective, v_primary_model::text, TRUE, v_run_status::text;
            RETURN;
        ELSE
            SELECT art.*
            INTO   v_trigger
            FROM   public.audit_run_dispute_triggers AS art
            WHERE  art.audit_run_id = p_audit_run_id
              AND  art.reason_code = 'PASS_A_SCHEMA_INVALID';

            IF NOT FOUND THEN
                RETURN QUERY SELECT p_audit_run_id, 'NEEDS_DISPUTE_TRIGGER_A'::text, 'A'::text, NULL::uuid, v_pass_a.attempt_count, v_primary_model::text, NULL::boolean, v_run_status::text;
                RETURN;
            END IF;

            IF EXISTS (
                SELECT 1
                FROM   public.audit_run_pass_results AS apr
                WHERE  apr.audit_run_id = p_audit_run_id
                  AND  apr.pass_code = 'B'
            ) THEN
                RAISE EXCEPTION
                    'data integrity violation: Pass B row exists for run % under a PASS_A_SCHEMA_INVALID substitution trigger',
                    p_audit_run_id
                    USING ERRCODE = 'data_corrupted';
            END IF;

            v_proceed_to_c := true;
            v_skip_c_check := false;
        END IF;
    ELSIF v_pass_a.status = 'failed' THEN
        IF v_pass_a.attempt_count < 2 THEN
            v_new_token := gen_random_uuid();
            UPDATE public.audit_run_pass_results AS apr
            SET    status = 'running', attempt_count = apr.attempt_count + 1,
                   result_json = NULL, raw_response_text = NULL,
                   schema_validation_errors = NULL, last_error = NULL,
                   provider_request_id = NULL, input_tokens = NULL,
                   output_tokens = NULL, actual_cost_usd = NULL,
                   lease_owner = p_worker_id, lease_token = v_new_token,
                   lease_expires_at = now() + (p_lease_seconds || ' seconds')::interval,
                   claimed_at = now(), completed_at = NULL, updated_at = now()
            WHERE  apr.id = v_pass_a.id
            RETURNING apr.attempt_count INTO v_prospective;

            RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_A'::text, 'A'::text, v_new_token, v_prospective, v_primary_model::text, TRUE, v_run_status::text;
            RETURN;
        ELSE
            UPDATE public.audit_runs AS ar SET run_status = 'inconclusive' WHERE ar.id = p_audit_run_id;
            RETURN QUERY SELECT p_audit_run_id, 'RUN_INCONCLUSIVE'::text, 'A'::text, NULL::uuid, v_pass_a.attempt_count, v_primary_model::text, NULL::boolean, 'inconclusive'::text;
            RETURN;
        END IF;
    ELSIF v_pass_a.status = 'completed' THEN
        v_proceed_to_b := true;
    ELSE
        RAISE EXCEPTION 'unexpected Pass A status for run %: %', p_audit_run_id, v_pass_a.status
            USING ERRCODE = 'data_corrupted';
    END IF;

    -- -------------------------------------------------------------------------
    -- Pass B (only reached when Pass A is completed)
    -- -------------------------------------------------------------------------
    IF v_proceed_to_b THEN
        SELECT apr.*
        INTO   v_pass_b
        FROM   public.audit_run_pass_results AS apr
        WHERE  apr.audit_run_id = p_audit_run_id
          AND  apr.pass_code = 'B'
        FOR UPDATE;

        IF NOT FOUND THEN
            v_new_token := gen_random_uuid();
            INSERT INTO public.audit_run_pass_results (
                audit_run_id, pass_code, status, model_name, prompt_version,
                schema_version, input_hash, attempt_count,
                lease_owner, lease_token, lease_expires_at, started_at, claimed_at
            )
            SELECT p_audit_run_id, 'B', 'running', v_primary_model, dk.prompt_version,
                   '', '', 1,
                   p_worker_id, v_new_token, now() + (p_lease_seconds || ' seconds')::interval, now(), now()
            FROM   public.audit_run_dedup_keys dk
            WHERE  dk.audit_run_id = p_audit_run_id;

            RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_B'::text, 'B'::text, v_new_token, 1, v_primary_model::text, FALSE, v_run_status::text;
            RETURN;
        END IF;

        IF v_pass_b.status = 'running' THEN
            IF v_pass_b.lease_expires_at >= now() THEN
                RETURN QUERY SELECT p_audit_run_id, 'WAIT'::text, 'B'::text, NULL::uuid, v_pass_b.attempt_count, v_primary_model::text, NULL::boolean, v_run_status::text;
                RETURN;
            END IF;

            v_prospective := v_pass_b.attempt_count + 1;
            IF v_prospective <= 2 THEN
                v_new_token := gen_random_uuid();
                UPDATE public.audit_run_pass_results AS apr
                SET    attempt_count = v_prospective, lease_owner = p_worker_id,
                       lease_token = v_new_token,
                       lease_expires_at = now() + (p_lease_seconds || ' seconds')::interval,
                       claimed_at = now(), updated_at = now()
                WHERE  apr.id = v_pass_b.id;

                RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_B'::text, 'B'::text, v_new_token, v_prospective, v_primary_model::text, TRUE, v_run_status::text;
                RETURN;
            ELSE
                -- Cap exceeded: no third provider attempt is performed. The
                -- stored attempt_count is left unchanged (it already reflects
                -- the two real attempts that occurred); only last_error
                -- records the prospective (never executed) third attempt.
                UPDATE public.audit_run_pass_results AS apr
                SET    status = 'failed',
                       last_error = jsonb_build_object(
                           'error_code', 'PASS_LEASE_ATTEMPT_LIMIT_EXCEEDED',
                           'previous_lease_owner', v_pass_b.lease_owner,
                           'previous_lease_expires_at', v_pass_b.lease_expires_at,
                           'attempt_count', v_pass_b.attempt_count,
                           'prospective_attempt_count', v_prospective
                       ),
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                       completed_at = now(), updated_at = now()
                WHERE  apr.id = v_pass_b.id;

                UPDATE public.audit_runs AS ar SET run_status = 'inconclusive' WHERE ar.id = p_audit_run_id;

                RETURN QUERY SELECT p_audit_run_id, 'RUN_INCONCLUSIVE'::text, 'B'::text, NULL::uuid, v_pass_b.attempt_count, v_primary_model::text, NULL::boolean, 'inconclusive'::text;
                RETURN;
            END IF;
        ELSIF v_pass_b.status = 'schema_invalid' THEN
            IF v_pass_b.attempt_count < 2 THEN
                v_new_token := gen_random_uuid();
                UPDATE public.audit_run_pass_results AS apr
                SET    status = 'running', attempt_count = apr.attempt_count + 1,
                       result_json = NULL, raw_response_text = NULL,
                       schema_validation_errors = NULL, last_error = NULL,
                       provider_request_id = NULL, input_tokens = NULL,
                       output_tokens = NULL, actual_cost_usd = NULL,
                       lease_owner = p_worker_id, lease_token = v_new_token,
                       lease_expires_at = now() + (p_lease_seconds || ' seconds')::interval,
                       claimed_at = now(), completed_at = NULL, updated_at = now()
                WHERE  apr.id = v_pass_b.id
                RETURNING apr.attempt_count INTO v_prospective;

                RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_B'::text, 'B'::text, v_new_token, v_prospective, v_primary_model::text, TRUE, v_run_status::text;
                RETURN;
            ELSE
                SELECT art.*
                INTO   v_trigger
                FROM   public.audit_run_dispute_triggers AS art
                WHERE  art.audit_run_id = p_audit_run_id
                  AND  art.reason_code = 'PASS_B_SCHEMA_INVALID';

                IF NOT FOUND THEN
                    RETURN QUERY SELECT p_audit_run_id, 'NEEDS_DISPUTE_TRIGGER_B'::text, 'B'::text, NULL::uuid, v_pass_b.attempt_count, v_primary_model::text, NULL::boolean, v_run_status::text;
                    RETURN;
                END IF;

                v_proceed_to_c := true;
                v_skip_c_check := false;
            END IF;
        ELSIF v_pass_b.status = 'failed' THEN
            IF v_pass_b.attempt_count < 2 THEN
                v_new_token := gen_random_uuid();
                UPDATE public.audit_run_pass_results AS apr
                SET    status = 'running', attempt_count = apr.attempt_count + 1,
                       result_json = NULL, raw_response_text = NULL,
                       schema_validation_errors = NULL, last_error = NULL,
                       provider_request_id = NULL, input_tokens = NULL,
                       output_tokens = NULL, actual_cost_usd = NULL,
                       lease_owner = p_worker_id, lease_token = v_new_token,
                       lease_expires_at = now() + (p_lease_seconds || ' seconds')::interval,
                       claimed_at = now(), completed_at = NULL, updated_at = now()
                WHERE  apr.id = v_pass_b.id
                RETURNING apr.attempt_count INTO v_prospective;

                RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_B'::text, 'B'::text, v_new_token, v_prospective, v_primary_model::text, TRUE, v_run_status::text;
                RETURN;
            ELSE
                UPDATE public.audit_runs AS ar SET run_status = 'inconclusive' WHERE ar.id = p_audit_run_id;
                RETURN QUERY SELECT p_audit_run_id, 'RUN_INCONCLUSIVE'::text, 'B'::text, NULL::uuid, v_pass_b.attempt_count, v_primary_model::text, NULL::boolean, 'inconclusive'::text;
                RETURN;
            END IF;
        ELSIF v_pass_b.status = 'completed' THEN
            v_proceed_to_c := true;
            v_skip_c_check := true;
        ELSE
            RAISE EXCEPTION 'unexpected Pass B status for run %: %', p_audit_run_id, v_pass_b.status
                USING ERRCODE = 'data_corrupted';
        END IF;
    END IF;

    -- -------------------------------------------------------------------------
    -- Pass C (reached via normal A+B completion, or via Pass A/B
    -- schema-invalid substitution once the matching dispute trigger exists)
    -- -------------------------------------------------------------------------
    IF v_proceed_to_c THEN
        IF v_skip_c_check THEN
            SELECT art.*
            INTO   v_trigger
            FROM   public.audit_run_dispute_triggers AS art
            WHERE  art.audit_run_id = p_audit_run_id;

            IF NOT FOUND THEN
                IF EXISTS (
                    SELECT 1
                    FROM   public.audit_run_pass_results AS apr
                    WHERE  apr.audit_run_id = p_audit_run_id
                      AND  apr.pass_code = 'C'
                ) THEN
                    RETURN QUERY SELECT p_audit_run_id, 'RUN_READY_TO_COMPLETE'::text, 'C'::text, NULL::uuid, 0, v_dispute_model::text, NULL::boolean, v_run_status::text;
                    RETURN;
                END IF;

                INSERT INTO public.audit_run_pass_results (
                    audit_run_id, pass_code, status, model_name, prompt_version,
                    schema_version, input_hash, attempt_count,
                    result_json, started_at, claimed_at, completed_at
                )
                SELECT p_audit_run_id, 'C', 'skipped', v_dispute_model, dk.prompt_version,
                       '', '', 0,
                       jsonb_build_object('skip_reason', 'no_dispute_trigger'),
                       now(), now(), now()
                FROM   public.audit_run_dedup_keys dk
                WHERE  dk.audit_run_id = p_audit_run_id;

                RETURN QUERY SELECT p_audit_run_id, 'SKIP_PASS_C'::text, 'C'::text, NULL::uuid, 0, v_dispute_model::text, FALSE, v_run_status::text;
                RETURN;
            END IF;
        END IF;

        SELECT apr.*
        INTO   v_pass_c
        FROM   public.audit_run_pass_results AS apr
        WHERE  apr.audit_run_id = p_audit_run_id
          AND  apr.pass_code = 'C'
        FOR UPDATE;

        IF NOT FOUND THEN
            v_new_token := gen_random_uuid();
            INSERT INTO public.audit_run_pass_results (
                audit_run_id, pass_code, status, model_name, prompt_version,
                schema_version, input_hash, attempt_count,
                lease_owner, lease_token, lease_expires_at, started_at, claimed_at
            )
            SELECT p_audit_run_id, 'C', 'running', v_dispute_model, dk.prompt_version,
                   '', '', 1,
                   p_worker_id, v_new_token, now() + (p_lease_seconds || ' seconds')::interval, now(), now()
            FROM   public.audit_run_dedup_keys dk
            WHERE  dk.audit_run_id = p_audit_run_id;

            RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_C'::text, 'C'::text, v_new_token, 1, v_dispute_model::text, FALSE, v_run_status::text;
            RETURN;
        END IF;

        IF v_pass_c.status = 'running' THEN
            IF v_pass_c.lease_expires_at >= now() THEN
                RETURN QUERY SELECT p_audit_run_id, 'WAIT'::text, 'C'::text, NULL::uuid, v_pass_c.attempt_count, v_dispute_model::text, NULL::boolean, v_run_status::text;
                RETURN;
            END IF;

            v_prospective := v_pass_c.attempt_count + 1;
            IF v_prospective <= 2 THEN
                v_new_token := gen_random_uuid();
                UPDATE public.audit_run_pass_results AS apr
                SET    attempt_count = v_prospective, lease_owner = p_worker_id,
                       lease_token = v_new_token,
                       lease_expires_at = now() + (p_lease_seconds || ' seconds')::interval,
                       claimed_at = now(), updated_at = now()
                WHERE  apr.id = v_pass_c.id;

                RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_C'::text, 'C'::text, v_new_token, v_prospective, v_dispute_model::text, TRUE, v_run_status::text;
                RETURN;
            ELSE
                -- Cap exceeded: no third provider attempt is performed. The
                -- stored attempt_count is left unchanged (it already reflects
                -- the two real attempts that occurred); only last_error
                -- records the prospective (never executed) third attempt.
                UPDATE public.audit_run_pass_results AS apr
                SET    status = 'failed',
                       last_error = jsonb_build_object(
                           'error_code', 'PASS_LEASE_ATTEMPT_LIMIT_EXCEEDED',
                           'previous_lease_owner', v_pass_c.lease_owner,
                           'previous_lease_expires_at', v_pass_c.lease_expires_at,
                           'attempt_count', v_pass_c.attempt_count,
                           'prospective_attempt_count', v_prospective
                       ),
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                       completed_at = now(), updated_at = now()
                WHERE  apr.id = v_pass_c.id;

                UPDATE public.audit_runs AS ar SET run_status = 'inconclusive' WHERE ar.id = p_audit_run_id;

                RETURN QUERY SELECT p_audit_run_id, 'RUN_INCONCLUSIVE'::text, 'C'::text, NULL::uuid, v_pass_c.attempt_count, v_dispute_model::text, NULL::boolean, 'inconclusive'::text;
                RETURN;
            END IF;
        ELSIF v_pass_c.status IN ('schema_invalid', 'failed') THEN
            IF v_pass_c.attempt_count < 2 THEN
                v_new_token := gen_random_uuid();
                UPDATE public.audit_run_pass_results AS apr
                SET    status = 'running', attempt_count = apr.attempt_count + 1,
                       result_json = NULL, raw_response_text = NULL,
                       schema_validation_errors = NULL, last_error = NULL,
                       provider_request_id = NULL, input_tokens = NULL,
                       output_tokens = NULL, actual_cost_usd = NULL,
                       lease_owner = p_worker_id, lease_token = v_new_token,
                       lease_expires_at = now() + (p_lease_seconds || ' seconds')::interval,
                       claimed_at = now(), completed_at = NULL, updated_at = now()
                WHERE  apr.id = v_pass_c.id
                RETURNING apr.attempt_count INTO v_prospective;

                RETURN QUERY SELECT p_audit_run_id, 'EXECUTE_PASS_C'::text, 'C'::text, v_new_token, v_prospective, v_dispute_model::text, TRUE, v_run_status::text;
                RETURN;
            ELSE
                -- No Pass D exists: exhausted Pass C always terminates the run.
                UPDATE public.audit_runs AS ar SET run_status = 'inconclusive' WHERE ar.id = p_audit_run_id;
                RETURN QUERY SELECT p_audit_run_id, 'RUN_INCONCLUSIVE'::text, 'C'::text, NULL::uuid, v_pass_c.attempt_count, v_dispute_model::text, NULL::boolean, 'inconclusive'::text;
                RETURN;
            END IF;
        ELSIF v_pass_c.status IN ('completed', 'skipped') THEN
            RETURN QUERY SELECT p_audit_run_id, 'RUN_READY_TO_COMPLETE'::text, 'C'::text, NULL::uuid, v_pass_c.attempt_count, v_dispute_model::text, NULL::boolean, v_run_status::text;
            RETURN;
        ELSE
            RAISE EXCEPTION 'unexpected Pass C status for run %: %', p_audit_run_id, v_pass_c.status
                USING ERRCODE = 'data_corrupted';
        END IF;
    END IF;

    RAISE EXCEPTION 'claim_ai_quality_audit_pass_v1 reached an unhandled state for run %', p_audit_run_id
        USING ERRCODE = 'data_corrupted';
END;
$$;

REVOKE ALL ON FUNCTION public.claim_ai_quality_audit_pass_v1(uuid, text, integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.claim_ai_quality_audit_pass_v1(uuid, text, integer) FROM anon;
REVOKE EXECUTE ON FUNCTION public.claim_ai_quality_audit_pass_v1(uuid, text, integer) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.claim_ai_quality_audit_pass_v1(uuid, text, integer) TO service_role;

COMMENT ON FUNCTION public.claim_ai_quality_audit_pass_v1(uuid, text, integer) IS
'Atomically claims/advances exactly one ai_quality audit pass under row
locks, enforcing strict A->B->C sequencing, lease assignment/reclaim, and a
hard cap of two actual provider attempts per pass. A third claim against an
already attempt_count=2 expired lease performs no execution: it transitions
the pass to failed (attempt_count stays 2, last_error records
prospective_attempt_count=3) and the run to inconclusive. Never creates a
dispute trigger itself.
Execute permission: service_role only.';


-- =============================================================================
-- 6. record_audit_pass_result_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.record_audit_pass_result_v1(
    p_audit_run_id uuid,
    p_pass_code text,
    p_lease_token uuid,
    p_status text,
    p_result_json jsonb DEFAULT NULL,
    p_raw_response_text text DEFAULT NULL,
    p_schema_validation_errors jsonb DEFAULT NULL,
    p_last_error jsonb DEFAULT NULL,
    p_provider_request_id text DEFAULT NULL,
    p_input_tokens integer DEFAULT NULL,
    p_output_tokens integer DEFAULT NULL,
    p_actual_cost_usd numeric DEFAULT NULL,
    p_schema_version text DEFAULT NULL,
    p_input_hash text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    pass_result_id    uuid,
    audit_run_id        uuid,
    pass_code              text,
    status                    text,
    attempt_count               integer
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_row              public.audit_run_pass_results;
    v_result_id        uuid;
    v_result_run_id    uuid;
    v_result_pass      text;
    v_result_status    text;
    v_result_attempts  integer;

    -- Shape-validation working variables (p_status = 'completed' only).
    v_pi               integer;
    v_pf               jsonb;
    v_pf_ref           text;
    v_seen_refs        text[];
    v_resolution_type  text;
    v_resolution_status text;
    v_substituted      jsonb;
    v_confirmed        jsonb;
BEGIN
    IF p_pass_code NOT IN ('A', 'B', 'C') THEN
        RAISE EXCEPTION 'invalid p_pass_code: %', p_pass_code
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_status NOT IN ('completed', 'schema_invalid', 'failed') THEN
        RAISE EXCEPTION
            'p_status must be one of completed, schema_invalid, failed (got %); skipped is only set by claim_ai_quality_audit_pass_v1',
            p_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT apr.*
    INTO   v_row
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = p_pass_code
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'no pass row for audit_run % pass_code %', p_audit_run_id, p_pass_code
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_row.status <> 'running' THEN
        RAISE EXCEPTION
            'pass % for run % has status %; only a running pass may be recorded (already-terminal rows cannot be overwritten)',
            p_pass_code, p_audit_run_id, v_row.status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_lease_token IS NULL OR v_row.lease_token IS DISTINCT FROM p_lease_token THEN
        RAISE EXCEPTION
            'lease token mismatch for run % pass %; stale token rejected',
            p_audit_run_id, p_pass_code
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Payload validation per status (defense in depth; backstopped by the
    -- table's per-status CHECK constraints). A malformed 'completed' payload
    -- is rejected here, BEFORE the row is written, because 'completed' is
    -- permanently terminal (no later call can ever correct it): letting a
    -- structurally invalid result_json through would strand the run with no
    -- valid claim or completion action available to it.
    IF p_status = 'completed' THEN
        IF p_result_json IS NULL OR jsonb_typeof(p_result_json) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'p_result_json must be a JSON object when p_status=completed'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF p_pass_code IN ('A', 'B') THEN
            IF NOT (p_result_json ? 'selected_option_labels')
               OR jsonb_typeof(p_result_json -> 'selected_option_labels') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION
                    'pass % completed result_json.selected_option_labels must be a JSON array',
                    p_pass_code
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END IF;

        IF p_pass_code = 'B' THEN
            IF NOT (p_result_json ? 'proposed_findings')
               OR jsonb_typeof(p_result_json -> 'proposed_findings') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION
                    'pass B completed result_json.proposed_findings must be a JSON array'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            v_seen_refs := '{}';
            FOR v_pi IN 0 .. jsonb_array_length(p_result_json -> 'proposed_findings') - 1 LOOP
                v_pf := (p_result_json -> 'proposed_findings') -> v_pi;
                IF jsonb_typeof(v_pf) IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION
                        'pass B proposed_findings[%] must be a JSON object', v_pi
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                v_pf_ref := v_pf ->> 'finding_ref';
                IF COALESCE(TRIM(v_pf_ref), '') = '' THEN
                    RAISE EXCEPTION
                        'pass B proposed_findings[%] is missing a non-empty finding_ref', v_pi
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF v_pf_ref = ANY(v_seen_refs) THEN
                    RAISE EXCEPTION
                        'pass B proposed_findings[%] has duplicate finding_ref: %', v_pi, v_pf_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                v_seen_refs := v_seen_refs || v_pf_ref;
            END LOOP;
        END IF;

        IF p_pass_code = 'C' THEN
            v_resolution_type   := p_result_json ->> 'resolution_type';
            v_resolution_status := p_result_json ->> 'resolution_status';
            v_substituted       := p_result_json -> 'substituted_for_passes';
            v_confirmed         := p_result_json -> 'confirmed_finding_refs';

            IF v_resolution_type NOT IN ('NORMAL_DISPUTE', 'PASS_A_SUBSTITUTION', 'PASS_B_SUBSTITUTION') THEN
                RAISE EXCEPTION
                    'pass C completed result_json.resolution_type must be one of NORMAL_DISPUTE, PASS_A_SUBSTITUTION, PASS_B_SUBSTITUTION (got %)',
                    v_resolution_type
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF v_resolution_status NOT IN ('RESOLVED', 'UNRESOLVED') THEN
                RAISE EXCEPTION
                    'pass C completed result_json.resolution_status must be RESOLVED or UNRESOLVED (got %)',
                    v_resolution_status
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF jsonb_typeof(v_substituted) IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION
                    'pass C completed result_json.substituted_for_passes must be a JSON array'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF jsonb_typeof(v_confirmed) IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION
                    'pass C completed result_json.confirmed_finding_refs must be a JSON array'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            -- Discriminator coupling: substituted_for_passes must exactly
            -- match what resolution_type claims happened.
            IF v_resolution_type = 'NORMAL_DISPUTE' AND v_substituted <> '[]'::jsonb THEN
                RAISE EXCEPTION
                    'pass C resolution_type=NORMAL_DISPUTE requires substituted_for_passes=[] (got %)',
                    v_substituted
                    USING ERRCODE = 'invalid_parameter_value';
            ELSIF v_resolution_type = 'PASS_A_SUBSTITUTION' AND v_substituted <> '["A","B"]'::jsonb THEN
                RAISE EXCEPTION
                    'pass C resolution_type=PASS_A_SUBSTITUTION requires substituted_for_passes=["A","B"] (got %)',
                    v_substituted
                    USING ERRCODE = 'invalid_parameter_value';
            ELSIF v_resolution_type = 'PASS_B_SUBSTITUTION' AND v_substituted <> '["B"]'::jsonb THEN
                RAISE EXCEPTION
                    'pass C resolution_type=PASS_B_SUBSTITUTION requires substituted_for_passes=["B"] (got %)',
                    v_substituted
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            v_seen_refs := '{}';
            FOR v_pi IN 0 .. jsonb_array_length(v_confirmed) - 1 LOOP
                v_pf := v_confirmed -> v_pi;
                IF jsonb_typeof(v_pf) IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION
                        'pass C confirmed_finding_refs[%] must be a JSON string', v_pi
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                v_pf_ref := v_pf #>> '{}';
                IF COALESCE(TRIM(v_pf_ref), '') = '' THEN
                    RAISE EXCEPTION
                        'pass C confirmed_finding_refs[%] must be non-empty', v_pi
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF v_pf_ref = ANY(v_seen_refs) THEN
                    RAISE EXCEPTION
                        'pass C confirmed_finding_refs[%] has duplicate value: %', v_pi, v_pf_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                v_seen_refs := v_seen_refs || v_pf_ref;
            END LOOP;
        END IF;
    ELSIF p_status = 'schema_invalid' THEN
        IF p_result_json IS NOT NULL THEN
            RAISE EXCEPTION 'p_result_json must be NULL when p_status=schema_invalid'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_schema_validation_errors IS NULL THEN
            RAISE EXCEPTION 'p_schema_validation_errors is required when p_status=schema_invalid'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(TRIM(p_raw_response_text), '') = '' THEN
            RAISE EXCEPTION 'p_raw_response_text is required when p_status=schema_invalid'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    ELSIF p_status = 'failed' THEN
        IF p_result_json IS NOT NULL THEN
            RAISE EXCEPTION 'p_result_json must be NULL when p_status=failed'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF p_last_error IS NULL THEN
            RAISE EXCEPTION 'p_last_error is required when p_status=failed'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END IF;

    UPDATE public.audit_run_pass_results AS apr
    SET    status = p_status,
           result_json = p_result_json,
           raw_response_text = LEFT(p_raw_response_text, 20000),
           schema_validation_errors = p_schema_validation_errors,
           last_error = p_last_error,
           provider_request_id = COALESCE(p_provider_request_id, apr.provider_request_id),
           input_tokens = COALESCE(p_input_tokens, apr.input_tokens),
           output_tokens = COALESCE(p_output_tokens, apr.output_tokens),
           actual_cost_usd = COALESCE(p_actual_cost_usd, apr.actual_cost_usd),
           schema_version = COALESCE(NULLIF(TRIM(p_schema_version), ''), apr.schema_version),
           input_hash = COALESCE(NULLIF(TRIM(p_input_hash), ''), apr.input_hash),
           lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
           completed_at = now(), updated_at = now(),
           metadata = apr.metadata || COALESCE(p_metadata, '{}'::jsonb)
    WHERE  apr.id = v_row.id
    RETURNING apr.id, apr.audit_run_id, apr.pass_code, apr.status, apr.attempt_count
    INTO     v_result_id, v_result_run_id, v_result_pass, v_result_status, v_result_attempts;

    RETURN QUERY SELECT v_result_id, v_result_run_id, v_result_pass::text, v_result_status::text, v_result_attempts;
END;
$$;

REVOKE ALL ON FUNCTION public.record_audit_pass_result_v1(
    uuid, text, uuid, text, jsonb, text, jsonb, jsonb, text, integer, integer, numeric, text, text, jsonb
) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.record_audit_pass_result_v1(
    uuid, text, uuid, text, jsonb, text, jsonb, jsonb, text, integer, integer, numeric, text, text, jsonb
) FROM anon;
REVOKE EXECUTE ON FUNCTION public.record_audit_pass_result_v1(
    uuid, text, uuid, text, jsonb, text, jsonb, jsonb, text, integer, integer, numeric, text, text, jsonb
) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.record_audit_pass_result_v1(
    uuid, text, uuid, text, jsonb, text, jsonb, jsonb, text, integer, integer, numeric, text, text, jsonb
) TO service_role;

COMMENT ON FUNCTION public.record_audit_pass_result_v1(
    uuid, text, uuid, text, jsonb, text, jsonb, jsonb, text, integer, integer, numeric, text, text, jsonb
) IS
'Atomically finalizes a claimed (status=running) ai_quality pass to
completed, schema_invalid, or failed. Requires an exact matching lease_token
and rejects overwriting an already-terminal row. Clears lease fields and
sets completed_at. schema_version/input_hash are finalized here from their
claim-time placeholders when supplied.
For p_status=completed, result_json is validated to be a JSON object with
the required shape BEFORE the row is written (since completed is
permanently terminal and can never be corrected afterward): pass A/B require
selected_option_labels as a JSON array; pass B additionally requires
proposed_findings as a JSON array of objects each with a non-empty, unique
finding_ref; pass C requires resolution_type in (NORMAL_DISPUTE,
PASS_A_SUBSTITUTION, PASS_B_SUBSTITUTION), resolution_status in (RESOLVED,
UNRESOLVED), substituted_for_passes as a JSON array matching resolution_type
exactly ([] / ["A","B"] / ["B"] respectively), and confirmed_finding_refs as
a JSON array of non-empty, unique strings. Any validation failure raises
before the UPDATE executes, so the pass row is left untouched (still
running, with its lease intact) and a corrected result can still be
recorded against the same lease_token.
Execute permission: service_role only.';


-- =============================================================================
-- 7. persist_audit_run_dispute_trigger_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.persist_audit_run_dispute_trigger_v1(
    p_audit_run_id uuid,
    p_reason_code text,
    p_source_pass_code text,
    p_trigger_reason text,
    p_finding_refs jsonb DEFAULT '[]'::jsonb
)
RETURNS TABLE (
    audit_run_id     uuid,
    reason_code        text,
    source_pass_code     text,
    created                 boolean
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_pass_a    public.audit_run_pass_results;
    v_pass_b    public.audit_run_pass_results;
    v_existing  public.audit_run_dispute_triggers;
    v_refs      jsonb;
    v_proposed_refs jsonb;
BEGIN
    IF p_reason_code NOT IN (
        'BLIND_ANSWER_MISMATCH', 'BLOCKING_DEFECT_PROPOSED', 'AMBIGUITY_PROPOSED',
        'PASS_A_SCHEMA_INVALID', 'PASS_B_SCHEMA_INVALID', 'EVIDENCE_STORED_ANSWER_CONFLICT'
    ) THEN
        RAISE EXCEPTION 'invalid p_reason_code: %', p_reason_code
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_source_pass_code NOT IN ('A', 'B') THEN
        RAISE EXCEPTION 'invalid p_source_pass_code: %', p_source_pass_code
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF COALESCE(TRIM(p_trigger_reason), '') = '' THEN
        RAISE EXCEPTION 'p_trigger_reason must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_refs := COALESCE(p_finding_refs, '[]'::jsonb);
    IF jsonb_typeof(v_refs) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_finding_refs must be a JSON array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT apr.*
    INTO   v_pass_a
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = 'A';
    SELECT apr.*
    INTO   v_pass_b
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = 'B';

    IF p_reason_code = 'PASS_A_SCHEMA_INVALID' THEN
        IF p_source_pass_code <> 'A' OR v_refs <> '[]'::jsonb THEN
            RAISE EXCEPTION 'PASS_A_SCHEMA_INVALID requires source_pass_code=A and empty finding_refs'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_pass_a.status IS DISTINCT FROM 'schema_invalid' OR v_pass_a.attempt_count <> 2 THEN
            RAISE EXCEPTION
                'PASS_A_SCHEMA_INVALID requires Pass A status=schema_invalid with attempt_count=2 (found status=%, attempt_count=%)',
                v_pass_a.status, v_pass_a.attempt_count
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_pass_b.audit_run_id IS NOT NULL THEN
            RAISE EXCEPTION 'PASS_A_SCHEMA_INVALID requires Pass B to not yet exist for run %', p_audit_run_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    ELSIF p_reason_code = 'PASS_B_SCHEMA_INVALID' THEN
        IF p_source_pass_code <> 'B' OR v_refs <> '[]'::jsonb THEN
            RAISE EXCEPTION 'PASS_B_SCHEMA_INVALID requires source_pass_code=B and empty finding_refs'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_pass_a.status IS DISTINCT FROM 'completed' THEN
            RAISE EXCEPTION 'PASS_B_SCHEMA_INVALID requires Pass A status=completed (found %)', v_pass_a.status
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_pass_b.status IS DISTINCT FROM 'schema_invalid' OR v_pass_b.attempt_count <> 2 THEN
            RAISE EXCEPTION
                'PASS_B_SCHEMA_INVALID requires Pass B status=schema_invalid with attempt_count=2 (found status=%, attempt_count=%)',
                v_pass_b.status, v_pass_b.attempt_count
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    ELSE
        IF p_source_pass_code <> 'B' OR jsonb_array_length(v_refs) = 0 THEN
            RAISE EXCEPTION
                'normal dispute reason % requires source_pass_code=B and a non-empty finding_refs array',
                p_reason_code
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_pass_b.status IS DISTINCT FROM 'completed' THEN
            RAISE EXCEPTION 'normal dispute reasons require Pass B status=completed (found %)', v_pass_b.status
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        v_proposed_refs := COALESCE(
            (SELECT jsonb_agg(elem -> 'finding_ref')
             FROM   jsonb_array_elements(COALESCE(v_pass_b.result_json -> 'proposed_findings', '[]'::jsonb)) AS elem),
            '[]'::jsonb
        );

        IF EXISTS (
            SELECT 1 FROM jsonb_array_elements(v_refs) AS ref
            WHERE NOT (v_proposed_refs @> jsonb_build_array(ref))
        ) THEN
            RAISE EXCEPTION
                'finding_refs must be a subset of Pass B''s proposed_findings finding_ref values for run %',
                p_audit_run_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END IF;

    SELECT art.*
    INTO   v_existing
    FROM   public.audit_run_dispute_triggers AS art
    WHERE  art.audit_run_id = p_audit_run_id;

    IF FOUND THEN
        IF v_existing.reason_code = p_reason_code
           AND v_existing.source_pass_code = p_source_pass_code
           AND v_existing.trigger_reason = p_trigger_reason
           AND v_existing.finding_refs = v_refs THEN
            RETURN QUERY SELECT p_audit_run_id, v_existing.reason_code::text, v_existing.source_pass_code::text, FALSE;
            RETURN;
        ELSE
            RAISE EXCEPTION
                'a different dispute trigger already exists for run % (existing reason_code=%); cannot replace',
                p_audit_run_id, v_existing.reason_code
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END IF;

    INSERT INTO public.audit_run_dispute_triggers (
        audit_run_id, reason_code, source_pass_code, trigger_reason, finding_refs
    ) VALUES (
        p_audit_run_id, p_reason_code, p_source_pass_code, p_trigger_reason, v_refs
    );

    RETURN QUERY SELECT p_audit_run_id, p_reason_code::text, p_source_pass_code::text, TRUE;
END;
$$;

REVOKE ALL ON FUNCTION public.persist_audit_run_dispute_trigger_v1(uuid, text, text, text, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.persist_audit_run_dispute_trigger_v1(uuid, text, text, text, jsonb) FROM anon;
REVOKE EXECUTE ON FUNCTION public.persist_audit_run_dispute_trigger_v1(uuid, text, text, text, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.persist_audit_run_dispute_trigger_v1(uuid, text, text, text, jsonb) TO service_role;

COMMENT ON FUNCTION public.persist_audit_run_dispute_trigger_v1(uuid, text, text, text, jsonb) IS
'Creates the (at most one) Pass C eligibility gate for an ai_quality run.
Validates reason/source-pass/finding_refs coupling and the required upstream
pass state for each reason family. Idempotent only when the existing trigger
is byte-for-byte identical; rejects any attempt to replace a different one.
Execute permission: service_role only.';


-- =============================================================================
-- 8. complete_ai_quality_audit_run_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.complete_ai_quality_audit_run_v1(
    p_audit_run_id uuid,
    p_confirmed_findings jsonb DEFAULT '[]'::jsonb,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    audit_run_id     uuid,
    run_status         text,
    finding_count        integer,
    evidence_count          integer
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_run_status        text;
    v_pass_a            public.audit_run_pass_results;
    v_pass_b            public.audit_run_pass_results;
    v_pass_c            public.audit_run_pass_results;
    v_trigger           public.audit_run_dispute_triggers;
    v_shape              text;
    v_resolution_status   text;
    v_proposed_refs        jsonb;
    v_confirmed_refs          jsonb;
    v_finding_count             integer := 0;
    v_evidence_count              integer := 0;

    v_fi          integer;
    v_finding     jsonb;
    v_finding_id  uuid;
    v_finding_code text;
    v_finding_type text;
    v_severity     text;
    v_materiality  text;
    v_title        text;
    v_description  text;
    v_confidence   numeric;
    v_finding_ref  text;

    v_ei        integer;
    v_evidence  jsonb;
    v_chunk_id  uuid;
    v_role      text;
    v_quote     text;
    v_relevance numeric;
BEGIN
    -- -------------------------------------------------------------------------
    -- Lock the run; idempotent no-op for already-completed runs; reject
    -- transitions out of the terminal inconclusive state.
    -- -------------------------------------------------------------------------
    SELECT ar.run_status INTO v_run_status
    FROM   public.audit_runs ar
    WHERE  ar.id = p_audit_run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_run not found: %', p_audit_run_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_run_status = 'completed' THEN
        SELECT COUNT(af.id)::integer, COUNT(afe.id)::integer
        INTO   v_finding_count, v_evidence_count
        FROM   public.audit_findings af
        LEFT JOIN public.audit_finding_evidence afe ON afe.finding_id = af.id
        WHERE  af.audit_run_id = p_audit_run_id;

        RETURN QUERY SELECT p_audit_run_id, 'completed'::text, v_finding_count, v_evidence_count;
        RETURN;
    END IF;

    IF v_run_status = 'inconclusive' THEN
        RAISE EXCEPTION
            'audit_run % is inconclusive (terminal) and cannot transition to completed',
            p_audit_run_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_run_status NOT IN ('pending', 'running') THEN
        RAISE EXCEPTION
            'audit_run % has status %; only pending or running ai_quality runs can be completed',
            p_audit_run_id, v_run_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT apr.*
    INTO   v_pass_a
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = 'A';
    SELECT apr.*
    INTO   v_pass_b
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = 'B';
    SELECT apr.*
    INTO   v_pass_c
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = 'C';
    SELECT art.*
    INTO   v_trigger
    FROM   public.audit_run_dispute_triggers AS art
    WHERE  art.audit_run_id = p_audit_run_id;

    IF v_pass_a.status = 'failed' OR v_pass_b.status = 'failed' OR v_pass_c.status = 'failed' THEN
        RAISE EXCEPTION 'audit_run % has a failed pass; cannot be completed', p_audit_run_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Shape detection: exactly the four accepted completion paths.
    -- -------------------------------------------------------------------------
    IF v_pass_a.status = 'completed' AND v_pass_b.status = 'completed'
       AND v_pass_c.status = 'skipped' AND v_trigger.audit_run_id IS NULL THEN
        v_shape := 'NORMAL_NO_DISPUTE';

    ELSIF v_pass_a.status = 'completed' AND v_pass_b.status = 'completed'
          AND v_pass_c.status = 'completed'
          AND v_trigger.audit_run_id IS NOT NULL
          AND v_trigger.reason_code IN ('BLIND_ANSWER_MISMATCH', 'BLOCKING_DEFECT_PROPOSED', 'AMBIGUITY_PROPOSED', 'EVIDENCE_STORED_ANSWER_CONFLICT')
          AND v_trigger.source_pass_code = 'B'
          AND v_pass_c.result_json ->> 'resolution_type' = 'NORMAL_DISPUTE'
          AND v_pass_c.result_json -> 'substituted_for_passes' = '[]'::jsonb THEN
        v_shape := 'NORMAL_DISPUTE';

    ELSIF v_pass_a.status = 'schema_invalid' AND v_pass_a.attempt_count = 2
          AND v_pass_b.audit_run_id IS NULL
          AND v_trigger.audit_run_id IS NOT NULL
          AND v_trigger.reason_code = 'PASS_A_SCHEMA_INVALID' AND v_trigger.source_pass_code = 'A'
          AND v_pass_c.status = 'completed'
          AND v_pass_c.result_json ->> 'resolution_type' = 'PASS_A_SUBSTITUTION'
          AND v_pass_c.result_json -> 'substituted_for_passes' = '["A","B"]'::jsonb THEN
        v_shape := 'PASS_A_SUBSTITUTION';

    ELSIF v_pass_a.status = 'completed'
          AND v_pass_b.status = 'schema_invalid' AND v_pass_b.attempt_count = 2
          AND v_trigger.audit_run_id IS NOT NULL
          AND v_trigger.reason_code = 'PASS_B_SCHEMA_INVALID' AND v_trigger.source_pass_code = 'B'
          AND v_pass_c.status = 'completed'
          AND v_pass_c.result_json ->> 'resolution_type' = 'PASS_B_SUBSTITUTION'
          AND v_pass_c.result_json -> 'substituted_for_passes' = '["B"]'::jsonb THEN
        v_shape := 'PASS_B_SUBSTITUTION';

    ELSE
        RAISE EXCEPTION
            'audit_run % pass-state combination is not an accepted completion path (A=%, B=%, C=%, trigger=%)',
            p_audit_run_id, v_pass_a.status, v_pass_b.status, v_pass_c.status,
            COALESCE(v_trigger.reason_code, 'none')
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Resolve final run status from Pass C's resolution_status (NORMAL_NO_
    -- DISPUTE never produced a contentful Pass C, so it always completes).
    -- -------------------------------------------------------------------------
    IF v_shape = 'NORMAL_NO_DISPUTE' THEN
        v_resolution_status := 'RESOLVED';
    ELSE
        v_resolution_status := v_pass_c.result_json ->> 'resolution_status';
        IF v_resolution_status NOT IN ('RESOLVED', 'UNRESOLVED') THEN
            RAISE EXCEPTION
                'Pass C resolution_status must be RESOLVED or UNRESOLVED, got: %',
                v_resolution_status
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END IF;

    IF v_resolution_status = 'UNRESOLVED' THEN
        UPDATE public.audit_runs AS ar
        SET    run_status = 'inconclusive', completed_at = now()
        WHERE  ar.id = p_audit_run_id;

        RETURN QUERY SELECT p_audit_run_id, 'inconclusive'::text, 0, 0;
        RETURN;
    END IF;

    -- -------------------------------------------------------------------------
    -- RESOLVED (or NORMAL_NO_DISPUTE): validate and insert confirmed findings.
    -- -------------------------------------------------------------------------
    IF jsonb_typeof(COALESCE(p_confirmed_findings, '[]'::jsonb)) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_confirmed_findings must be a JSON array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Proposed finding_ref universe: Pass B for normal shapes, Pass C for
    -- substitution shapes (Pass C did the full review itself in that case).
    IF v_shape IN ('PASS_A_SUBSTITUTION', 'PASS_B_SUBSTITUTION') THEN
        v_proposed_refs := COALESCE(
            (SELECT jsonb_agg(elem -> 'finding_ref')
             FROM   jsonb_array_elements(COALESCE(v_pass_c.result_json -> 'proposed_findings', '[]'::jsonb)) AS elem),
            '[]'::jsonb
        );
    ELSE
        v_proposed_refs := COALESCE(
            (SELECT jsonb_agg(elem -> 'finding_ref')
             FROM   jsonb_array_elements(COALESCE(v_pass_b.result_json -> 'proposed_findings', '[]'::jsonb)) AS elem),
            '[]'::jsonb
        );
    END IF;

    v_confirmed_refs := CASE
        WHEN v_shape = 'NORMAL_NO_DISPUTE' THEN '[]'::jsonb
        ELSE COALESCE(v_pass_c.result_json -> 'confirmed_finding_refs', '[]'::jsonb)
    END;

    FOR v_fi IN 0 .. jsonb_array_length(COALESCE(p_confirmed_findings, '[]'::jsonb)) - 1 LOOP
        v_finding      := COALESCE(p_confirmed_findings, '[]'::jsonb) -> v_fi;
        v_finding_ref  := TRIM(v_finding ->> 'finding_ref');
        v_finding_code := TRIM(v_finding ->> 'finding_code');
        v_finding_type := TRIM(v_finding ->> 'finding_type');
        v_severity     := TRIM(v_finding ->> 'severity');
        v_title        := TRIM(v_finding ->> 'title');
        v_description  := TRIM(v_finding ->> 'description');
        v_materiality  := COALESCE(NULLIF(TRIM(v_finding ->> 'materiality'), ''), 'warning');

        IF COALESCE(v_finding_ref, '') = '' THEN
            RAISE EXCEPTION 'finding % is missing finding_ref', v_fi
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF NOT (v_proposed_refs @> to_jsonb(v_finding_ref)) THEN
            RAISE EXCEPTION
                'finding % (finding_ref=%) is not present in the upstream proposed_findings for run %',
                v_fi, v_finding_ref, p_audit_run_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_finding_code, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing finding_code', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_finding_type, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing finding_type', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_severity, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing severity', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_title, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing title', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_description, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing description', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_finding_type NOT IN (
            'correctness', 'ambiguity', 'duplication', 'outdated', 'formatting',
            'coverage', 'difficulty', 'cognitive_level', 'answer_quality',
            'explanation_quality', 'source_support', 'policy', 'other'
        ) THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) has invalid finding_type: %',
                v_fi, v_finding_ref, v_finding_type
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_severity NOT IN ('info', 'low', 'medium', 'high', 'critical') THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) has invalid severity: %',
                v_fi, v_finding_ref, v_severity
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_materiality NOT IN ('blocking', 'warning', 'informational') THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) has invalid materiality: %',
                v_fi, v_finding_ref, v_materiality
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- SOURCE_SUPPORT_WEAK and DOMAIN_MISALIGNMENT can never be blocking.
        IF v_finding_code IN ('SOURCE_SUPPORT_WEAK', 'DOMAIN_MISALIGNMENT') THEN
            v_materiality := 'warning';
        END IF;

        -- Blocking findings require completed Pass C confirming this exact ref.
        IF v_materiality = 'blocking' THEN
            IF v_shape = 'NORMAL_NO_DISPUTE' THEN
                RAISE EXCEPTION
                    'finding % (finding_ref=%) cannot be blocking: Pass C did not run for run %',
                    v_fi, v_finding_ref, p_audit_run_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF NOT (v_confirmed_refs @> to_jsonb(v_finding_ref)) THEN
                RAISE EXCEPTION
                    'finding % (finding_ref=%) is materiality=blocking but is not present in Pass C''s confirmed_finding_refs for run %',
                    v_fi, v_finding_ref, p_audit_run_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END IF;

        -- Optional confidence.
        IF (v_finding ->> 'confidence') IS NOT NULL THEN
            BEGIN
                v_confidence := (v_finding ->> 'confidence')::numeric;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION 'finding % (finding_ref=%) has non-numeric confidence', v_fi, v_finding_ref
                    USING ERRCODE = 'invalid_parameter_value';
            END;
            IF v_confidence < 0 OR v_confidence > 1 THEN
                RAISE EXCEPTION
                    'finding % (finding_ref=%) confidence must be in [0,1], got: %',
                    v_fi, v_finding_ref, v_confidence
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END IF;

        -- Evidence must be a JSON array and a strict subset of the run's
        -- frozen audit_run_evidence_set.
        IF (v_finding -> 'evidence') IS NOT NULL
           AND jsonb_typeof(v_finding -> 'evidence') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) evidence must be a JSON array', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        FOR v_ei IN 0 .. jsonb_array_length(COALESCE(v_finding -> 'evidence', '[]'::jsonb)) - 1 LOOP
            v_evidence := COALESCE(v_finding -> 'evidence', '[]'::jsonb) -> v_ei;

            BEGIN
                v_chunk_id := (v_evidence ->> 'resource_chunk_id')::uuid;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION 'finding % evidence % has invalid resource_chunk_id', v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END;
            IF v_chunk_id IS NULL THEN
                RAISE EXCEPTION 'finding % evidence % is missing resource_chunk_id', v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_run_evidence_set ares
                WHERE  ares.audit_run_id = p_audit_run_id AND ares.resource_chunk_id = v_chunk_id
            ) THEN
                RAISE EXCEPTION
                    'finding % evidence % (resource_chunk_id=%) is outside the frozen evidence set for run %',
                    v_fi, v_ei, v_chunk_id, p_audit_run_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            v_role := TRIM(v_evidence ->> 'evidence_role');
            IF v_role NOT IN ('supporting', 'contradicting', 'contextual') THEN
                RAISE EXCEPTION 'finding % evidence % has invalid evidence_role: %', v_fi, v_ei, v_role
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            v_quote := v_evidence ->> 'quote_text';
            IF v_quote IS NOT NULL AND TRIM(v_quote) = '' THEN
                RAISE EXCEPTION 'finding % evidence % has empty quote_text', v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF (v_evidence ->> 'relevance_score') IS NOT NULL THEN
                BEGIN
                    v_relevance := (v_evidence ->> 'relevance_score')::numeric;
                EXCEPTION WHEN others THEN
                    RAISE EXCEPTION 'finding % evidence % has non-numeric relevance_score', v_fi, v_ei
                        USING ERRCODE = 'invalid_parameter_value';
                END;
                IF v_relevance < 0 OR v_relevance > 1 THEN
                    RAISE EXCEPTION 'finding % evidence % relevance_score must be in [0,1]', v_fi, v_ei
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
            END IF;
        END LOOP;

        -- Zero-evidence SOURCE_SUPPORT_WEAK requires a complete
        -- source_support_context block in metadata.
        IF v_finding_code = 'SOURCE_SUPPORT_WEAK'
           AND jsonb_array_length(COALESCE(v_finding -> 'evidence', '[]'::jsonb)) = 0 THEN
            DECLARE
                v_ctx jsonb := v_finding -> 'metadata' -> 'source_support_context';
            BEGIN
                IF v_ctx IS NULL OR jsonb_typeof(v_ctx) IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) is a zero-evidence SOURCE_SUPPORT_WEAK and requires metadata.source_support_context',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF NOT (v_ctx ? 'attempted_retrieval')
                   OR jsonb_typeof(v_ctx -> 'attempted_retrieval') IS DISTINCT FROM 'number'
                   OR (v_ctx ->> 'attempted_retrieval')::numeric < 0
                   OR (v_ctx ->> 'attempted_retrieval')::numeric <> floor((v_ctx ->> 'attempted_retrieval')::numeric) THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) source_support_context.attempted_retrieval must be a nonnegative integer',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF COALESCE(TRIM(v_ctx ->> 'evidence_limitation'), '') = '' THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) source_support_context.evidence_limitation must be non-empty',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF COALESCE(TRIM(v_ctx ->> 'proposed_technical_claim'), '') = '' THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) source_support_context.proposed_technical_claim must be non-empty',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF COALESCE(TRIM(v_ctx ->> 'insufficiency_reason'), '') = '' THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) source_support_context.insufficiency_reason must be non-empty',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
            END;
        END IF;

        v_finding_id := gen_random_uuid();

        INSERT INTO public.audit_findings (
            id, audit_run_id, finding_code, finding_type, severity, materiality,
            title, description, field_path, confidence, detector_name, detector_version, metadata
        ) VALUES (
            v_finding_id, p_audit_run_id, v_finding_code, v_finding_type, v_severity, v_materiality,
            v_title, v_description, v_finding ->> 'field_path', v_confidence,
            'ai_quality_audit', v_shape,
            COALESCE((v_finding -> 'metadata')::jsonb, '{}'::jsonb)
                || jsonb_build_object('finding_ref', v_finding_ref, 'completion_shape', v_shape)
        );

        v_finding_count := v_finding_count + 1;

        FOR v_ei IN 0 .. jsonb_array_length(COALESCE(v_finding -> 'evidence', '[]'::jsonb)) - 1 LOOP
            v_evidence  := COALESCE(v_finding -> 'evidence', '[]'::jsonb) -> v_ei;
            v_chunk_id  := (v_evidence ->> 'resource_chunk_id')::uuid;
            v_role      := TRIM(v_evidence ->> 'evidence_role');
            v_quote     := v_evidence ->> 'quote_text';
            v_relevance := (v_evidence ->> 'relevance_score')::numeric;

            INSERT INTO public.audit_finding_evidence (
                id, finding_id, resource_chunk_id, evidence_role, quote_text, relevance_score, metadata
            ) VALUES (
                gen_random_uuid(), v_finding_id, v_chunk_id, v_role, v_quote, v_relevance,
                COALESCE((v_evidence -> 'metadata')::jsonb, '{}'::jsonb)
            );

            v_evidence_count := v_evidence_count + 1;
        END LOOP;
    END LOOP;

    UPDATE public.audit_runs AS ar
    SET    run_status = 'completed',
           started_at = COALESCE(ar.started_at, now()),
           completed_at = now(),
           metadata = ar.metadata || COALESCE(p_metadata, '{}'::jsonb)
    WHERE  ar.id = p_audit_run_id;

    RETURN QUERY SELECT p_audit_run_id, 'completed'::text, v_finding_count, v_evidence_count;
END;
$$;

REVOKE ALL ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) FROM anon;
REVOKE EXECUTE ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) TO service_role;

COMMENT ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) IS
'Validates the run against exactly four accepted completion shapes (normal
no-dispute, normal dispute, Pass-A substitution, Pass-B substitution),
rejecting every other pass/trigger/discriminator combination. An UNRESOLVED
Pass C marks the run inconclusive and inserts zero findings. On a RESOLVED
path, inserts only confirmed findings whose finding_ref is present upstream,
whose evidence is a subset of audit_run_evidence_set, forces
SOURCE_SUPPORT_WEAK/DOMAIN_MISALIGNMENT to warning materiality, and requires
blocking findings to be present in Pass C''s confirmed_finding_refs.
Already-completed runs are idempotent no-ops; inconclusive runs are terminal.
Execute permission: service_role only.';


-- =============================================================================
-- Footer: schema corrections applied vs. the originally proposed plan
-- =============================================================================
--
--   1. resource_versions has no status/"completed" column in this repository
--      (confirmed via 20260623234600_v44_ingest_resource_version_rpc.sql and
--      20260623233800_v44_resource_library_foundation.sql). Versions are
--      written atomically with all chunks, so list_audit_candidate_resource_
--      chunks_v1 and create_or_get_ai_quality_audit_run_v1 both select the
--      highest version_number per resource_id instead of filtering on a
--      nonexistent status value.
--   2. certification_exam_name lives on public.questions.exam_name, reached
--      via question_versions.question_id, not on question_versions itself.
--   3. domain/category is question_versions.category.
--   4. pgcrypto's digest() is required for server-side SHA-256 verification
--      in create_or_get_ai_quality_audit_run_v1; CREATE EXTENSION IF NOT
--      EXISTS pgcrypto is added defensively, and that one function's
--      search_path additionally includes `extensions` (Supabase's default
--      install schema for this extension) alongside public/pg_catalog.
-- =============================================================================
