-- =============================================================================
-- V44 Phase 5B: ingest_resource_version_v1 RPC
-- Created : 2026-06-23 23:46:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds public.ingest_resource_version_v1, which atomically creates a new
-- immutable resource version and its text chunks.
--
-- Safety guarantees
-- -----------------
--   * No existing resource_versions or resource_chunks rows are updated or
--     deleted.
--   * No application code, embeddings, UI, or workers are affected.
--   * Idempotent: re-ingesting the same content_hash for the same resource
--     returns the existing version without creating a duplicate.
--   * Concurrent calls for the same resource serialise via FOR UPDATE on
--     the official_resources row.
--
-- Security
-- --------
--   EXECUTE revoked from PUBLIC, anon, authenticated.
--   service_role is the only granted caller.
-- =============================================================================


CREATE OR REPLACE FUNCTION public.ingest_resource_version_v1(
    p_resource_id             uuid,
    p_source_url              text,
    p_source_external_version text,
    p_content_text            text,
    p_content_hash            text,
    p_effective_at            timestamptz,
    p_created_by              text,
    p_metadata                jsonb DEFAULT '{}'::jsonb,
    p_chunks                  jsonb DEFAULT '[]'::jsonb
)
RETURNS TABLE (
    resource_version_id  uuid,
    resource_id          uuid,
    version_number       integer,
    chunk_count          integer
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_version_id     uuid;
    v_version_number integer;
    v_chunk_count    integer;

    -- Chunk loop variables.
    v_chunk          jsonb;
    v_i              integer;
    v_chunk_index    integer;
    v_chunk_text     text;
    v_chunk_hash     text;
    v_token_count    integer;
    v_start_offset   integer;
    v_end_offset     integer;
    v_index_set      integer[] := '{}';
BEGIN
    -- -------------------------------------------------------------------------
    -- Validate required scalar inputs.
    -- -------------------------------------------------------------------------
    IF COALESCE(TRIM(p_created_by), '') = '' THEN
        RAISE EXCEPTION 'p_created_by must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(TRIM(p_content_text), '') = '' THEN
        RAISE EXCEPTION 'p_content_text must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(TRIM(p_content_hash), '') = '' THEN
        RAISE EXCEPTION 'p_content_hash must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- source_url nullable but non-empty when provided.
    IF p_source_url IS NOT NULL AND TRIM(p_source_url) = '' THEN
        RAISE EXCEPTION 'p_source_url must not be empty when provided'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Validate p_chunks is an array.
    -- -------------------------------------------------------------------------
    IF jsonb_typeof(COALESCE(p_chunks, '[]'::jsonb)) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_chunks must be a JSON array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Validate each chunk before any writes.
    -- -------------------------------------------------------------------------
    FOR v_i IN 0 .. jsonb_array_length(COALESCE(p_chunks, '[]'::jsonb)) - 1 LOOP
        v_chunk      := COALESCE(p_chunks, '[]'::jsonb) -> v_i;
        v_chunk_text := TRIM(v_chunk ->> 'chunk_text');
        v_chunk_hash := TRIM(v_chunk ->> 'content_hash');

        BEGIN
            v_chunk_index  := (v_chunk ->> 'chunk_index')::integer;
            v_token_count  := (v_chunk ->> 'token_count')::integer;    -- nullable
            v_start_offset := (v_chunk ->> 'start_offset')::integer;   -- nullable
            v_end_offset   := (v_chunk ->> 'end_offset')::integer;     -- nullable
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION
                'chunk % has invalid chunk_index, token_count, start_offset, or end_offset',
                v_i
                USING ERRCODE = 'invalid_parameter_value';
        END;

        -- chunk_index required and >= 0.
        IF v_chunk_index IS NULL THEN
            RAISE EXCEPTION 'chunk % is missing chunk_index', v_i
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_chunk_index < 0 THEN
            RAISE EXCEPTION 'chunk % has chunk_index < 0: %', v_i, v_chunk_index
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- Unique chunk_index within this payload.
        IF v_chunk_index = ANY(v_index_set) THEN
            RAISE EXCEPTION 'duplicate chunk_index in p_chunks: %', v_chunk_index
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        v_index_set := v_index_set || v_chunk_index;

        -- Non-empty chunk_text.
        IF COALESCE(v_chunk_text, '') = '' THEN
            RAISE EXCEPTION 'chunk % (index=%) has empty or missing chunk_text',
                v_i, v_chunk_index
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- Non-empty content_hash.
        IF COALESCE(v_chunk_hash, '') = '' THEN
            RAISE EXCEPTION 'chunk % (index=%) has empty or missing content_hash',
                v_i, v_chunk_index
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- Optional numeric fields: non-negative when provided.
        IF v_token_count IS NOT NULL AND v_token_count < 0 THEN
            RAISE EXCEPTION 'chunk % (index=%) has token_count < 0: %',
                v_i, v_chunk_index, v_token_count
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_start_offset IS NOT NULL AND v_start_offset < 0 THEN
            RAISE EXCEPTION 'chunk % (index=%) has start_offset < 0: %',
                v_i, v_chunk_index, v_start_offset
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_end_offset IS NOT NULL AND v_end_offset < 0 THEN
            RAISE EXCEPTION 'chunk % (index=%) has end_offset < 0: %',
                v_i, v_chunk_index, v_end_offset
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_start_offset IS NOT NULL AND v_end_offset IS NOT NULL
           AND v_end_offset < v_start_offset THEN
            RAISE EXCEPTION
                'chunk % (index=%) has end_offset (%) < start_offset (%)',
                v_i, v_chunk_index, v_end_offset, v_start_offset
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END LOOP;

    -- -------------------------------------------------------------------------
    -- Lock the official_resources row.
    -- Serialises concurrent ingestion calls for the same resource, ensuring
    -- version numbers are assigned without gaps or duplicates.
    -- -------------------------------------------------------------------------
    PERFORM 1
    FROM    public.official_resources
    WHERE   id = p_resource_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'official_resource not found: %', p_resource_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- -------------------------------------------------------------------------
    -- Idempotency: if this resource already has a version with the same
    -- content_hash, return it with its existing chunk count.
    -- No new version or chunks are created.
    -- -------------------------------------------------------------------------
    SELECT rv.id, rv.version_number
    INTO   v_version_id, v_version_number
    FROM   public.resource_versions rv
    WHERE  rv.resource_id  = p_resource_id
      AND  rv.content_hash = p_content_hash
    LIMIT  1;

    IF FOUND THEN
        SELECT COUNT(*)::integer
        INTO   v_chunk_count
        FROM   public.resource_chunks
        WHERE  resource_version_id = v_version_id;

        RETURN QUERY
            SELECT v_version_id, p_resource_id, v_version_number, v_chunk_count;
        RETURN;
    END IF;

    -- -------------------------------------------------------------------------
    -- Assign next version_number under the resource-level lock.
    -- -------------------------------------------------------------------------
    SELECT COALESCE(MAX(rv.version_number), 0) + 1
    INTO   v_version_number
    FROM   public.resource_versions rv
    WHERE  rv.resource_id = p_resource_id;

    -- -------------------------------------------------------------------------
    -- Insert the resource_versions row.
    -- -------------------------------------------------------------------------
    v_version_id := gen_random_uuid();

    INSERT INTO public.resource_versions (
        id,
        resource_id,
        version_number,
        source_url,
        source_external_version,
        content_text,
        content_hash,
        effective_at,
        created_by,
        metadata
    ) VALUES (
        v_version_id,
        p_resource_id,
        v_version_number,
        p_source_url,
        p_source_external_version,
        p_content_text,
        p_content_hash,
        p_effective_at,
        p_created_by,
        COALESCE(p_metadata, '{}'::jsonb)
    );

    -- -------------------------------------------------------------------------
    -- Insert all chunks atomically.
    -- -------------------------------------------------------------------------
    v_chunk_count := 0;

    FOR v_i IN 0 .. jsonb_array_length(COALESCE(p_chunks, '[]'::jsonb)) - 1 LOOP
        v_chunk        := COALESCE(p_chunks, '[]'::jsonb) -> v_i;
        v_chunk_index  := (v_chunk ->> 'chunk_index')::integer;
        v_chunk_text   := TRIM(v_chunk ->> 'chunk_text');
        v_chunk_hash   := TRIM(v_chunk ->> 'content_hash');
        v_token_count  := (v_chunk ->> 'token_count')::integer;
        v_start_offset := (v_chunk ->> 'start_offset')::integer;
        v_end_offset   := (v_chunk ->> 'end_offset')::integer;

        INSERT INTO public.resource_chunks (
            id,
            resource_version_id,
            chunk_index,
            chunk_text,
            token_count,
            start_offset,
            end_offset,
            content_hash,
            metadata
        ) VALUES (
            gen_random_uuid(),
            v_version_id,
            v_chunk_index,
            v_chunk_text,
            v_token_count,
            v_start_offset,
            v_end_offset,
            v_chunk_hash,
            COALESCE((v_chunk -> 'metadata')::jsonb, '{}'::jsonb)
        );

        v_chunk_count := v_chunk_count + 1;
    END LOOP;

    RETURN QUERY
        SELECT v_version_id, p_resource_id, v_version_number, v_chunk_count;
END;
$$;


-- =============================================================================
-- Privilege hardening
-- =============================================================================

REVOKE ALL ON FUNCTION public.ingest_resource_version_v1(
    uuid, text, text, text, text, timestamptz, text, jsonb, jsonb
) FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.ingest_resource_version_v1(
    uuid, text, text, text, text, timestamptz, text, jsonb, jsonb
) FROM anon;

REVOKE EXECUTE ON FUNCTION public.ingest_resource_version_v1(
    uuid, text, text, text, text, timestamptz, text, jsonb, jsonb
) FROM authenticated;

GRANT EXECUTE ON FUNCTION public.ingest_resource_version_v1(
    uuid, text, text, text, text, timestamptz, text, jsonb, jsonb
) TO service_role;

COMMENT ON FUNCTION public.ingest_resource_version_v1(
    uuid, text, text, text, text, timestamptz, text, jsonb, jsonb
) IS
'Creates an immutable resource version and its text chunks atomically.
Idempotent: re-ingesting the same content_hash for the same resource returns
the existing version without creating a duplicate.  Concurrent calls for the
same resource serialise via FOR UPDATE on official_resources.
Does not update or delete existing resource_versions or resource_chunks.
Execute permission: service_role only.  PUBLIC, anon, authenticated revoked.';
