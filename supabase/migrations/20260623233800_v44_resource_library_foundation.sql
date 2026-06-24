-- =============================================================================
-- V44 Phase 5A: Resource Library Foundation
-- Created : 2026-06-23 23:38:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds three additive tables that form the immutable resource library:
--
--   official_resources   — catalogue of authoritative reference sources
--   resource_versions    — immutable content snapshots of each resource
--   resource_chunks      — text segments derived from a resource version
--
-- Design rules
-- ------------
--   * Resource versions are immutable snapshots; once written they are not
--     updated or deleted (update/delete triggers are deferred to Phase 5B).
--   * Chunks belong to an exact resource version and cascade on version delete
--     (Phase 5B will enforce immutability before enabling cascades in prod).
--   * No embedding/vector columns are added in this phase.
--   * No direct relation from questions or findings is established yet.
--   * No UUID arrays.
--
-- Security
-- --------
--   RLS is enabled on all three tables.
--   No anon or authenticated policies are added in this phase.
--   Service-role and admin access only for now.
-- =============================================================================


-- =============================================================================
-- 1. official_resources
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.official_resources (
    id                      uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    certification_exam_name text         NOT NULL,
    resource_type           text         NOT NULL,
    title                   text         NOT NULL,
    canonical_url           text,
    publisher               text,
    is_active               boolean      NOT NULL DEFAULT true,
    created_by              text         NOT NULL,
    created_at              timestamptz  NOT NULL DEFAULT now(),
    metadata                jsonb        NOT NULL DEFAULT '{}'::jsonb,

    -- Non-empty certification_exam_name.
    CONSTRAINT official_resources_exam_name_nonempty
        CHECK (TRIM(certification_exam_name) <> ''),

    -- Non-empty title.
    CONSTRAINT official_resources_title_nonempty
        CHECK (TRIM(title) <> ''),

    -- Allowed resource_type values.
    CONSTRAINT official_resources_type_valid
        CHECK (
            resource_type IN (
                'exam_guide',
                'official_documentation',
                'release_notes',
                'help_article',
                'trailhead',
                'policy',
                'other'
            )
        ),

    -- canonical_url is nullable but must be non-empty when provided.
    CONSTRAINT official_resources_canonical_url_nonempty
        CHECK (canonical_url IS NULL OR TRIM(canonical_url) <> '')
);

CREATE INDEX IF NOT EXISTS idx_or_exam_name
    ON public.official_resources (certification_exam_name);

CREATE INDEX IF NOT EXISTS idx_or_resource_type
    ON public.official_resources (resource_type);

CREATE INDEX IF NOT EXISTS idx_or_is_active
    ON public.official_resources (is_active);

CREATE INDEX IF NOT EXISTS idx_or_canonical_url
    ON public.official_resources (canonical_url);

ALTER TABLE public.official_resources ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.official_resources IS
'Catalogue of authoritative reference sources used by the Content Pipeline.
Not directly read by exam delivery or student-facing pages.
Service-role / admin access only for this phase.';


-- =============================================================================
-- 2. resource_versions
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.resource_versions (
    id                      uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id             uuid         NOT NULL
                                REFERENCES public.official_resources(id),
    version_number          integer      NOT NULL,
    source_url              text,
    source_external_version text,
    content_text            text         NOT NULL,
    content_hash            text         NOT NULL,
    effective_at            timestamptz,
    retrieved_at            timestamptz  NOT NULL DEFAULT now(),
    created_by              text         NOT NULL,
    metadata                jsonb        NOT NULL DEFAULT '{}'::jsonb,

    -- One version_number per resource.
    CONSTRAINT resource_versions_unique_version
        UNIQUE (resource_id, version_number),

    -- version_number must be positive.
    CONSTRAINT resource_versions_version_positive
        CHECK (version_number > 0),

    -- Non-empty content_text.
    CONSTRAINT resource_versions_content_nonempty
        CHECK (TRIM(content_text) <> ''),

    -- Non-empty content_hash.
    CONSTRAINT resource_versions_hash_nonempty
        CHECK (TRIM(content_hash) <> ''),

    -- source_url is nullable but must be non-empty when provided.
    CONSTRAINT resource_versions_source_url_nonempty
        CHECK (source_url IS NULL OR TRIM(source_url) <> '')
);

CREATE INDEX IF NOT EXISTS idx_rv_resource_version
    ON public.resource_versions (resource_id, version_number DESC);

CREATE INDEX IF NOT EXISTS idx_rv_content_hash
    ON public.resource_versions (content_hash);

CREATE INDEX IF NOT EXISTS idx_rv_effective_at
    ON public.resource_versions (effective_at DESC);

CREATE INDEX IF NOT EXISTS idx_rv_retrieved_at
    ON public.resource_versions (retrieved_at DESC);

ALTER TABLE public.resource_versions ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.resource_versions IS
'Immutable content snapshots of official resources.
Once written, rows must not be updated or deleted (enforcement via trigger
is deferred to Phase 5B).
Service-role / admin access only for this phase.';


-- =============================================================================
-- 3. resource_chunks
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.resource_chunks (
    id                  uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_version_id uuid         NOT NULL
                            REFERENCES public.resource_versions(id) ON DELETE CASCADE,
    chunk_index         integer      NOT NULL,
    chunk_text          text         NOT NULL,
    token_count         integer,
    start_offset        integer,
    end_offset          integer,
    content_hash        text         NOT NULL,
    metadata            jsonb        NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz  NOT NULL DEFAULT now(),

    -- One chunk_index per resource version.
    CONSTRAINT resource_chunks_unique_index
        UNIQUE (resource_version_id, chunk_index),

    -- chunk_index must be >= 0.
    CONSTRAINT resource_chunks_index_nonnegative
        CHECK (chunk_index >= 0),

    -- Non-empty chunk_text.
    CONSTRAINT resource_chunks_text_nonempty
        CHECK (TRIM(chunk_text) <> ''),

    -- token_count is nullable but non-negative when provided.
    CONSTRAINT resource_chunks_token_count_nonnegative
        CHECK (token_count IS NULL OR token_count >= 0),

    -- start_offset is nullable but non-negative when provided.
    CONSTRAINT resource_chunks_start_offset_nonnegative
        CHECK (start_offset IS NULL OR start_offset >= 0),

    -- end_offset is nullable but non-negative when provided.
    CONSTRAINT resource_chunks_end_offset_nonnegative
        CHECK (end_offset IS NULL OR end_offset >= 0),

    -- When both offsets are present, end_offset must be >= start_offset.
    CONSTRAINT resource_chunks_offset_order
        CHECK (
            start_offset IS NULL
            OR end_offset IS NULL
            OR end_offset >= start_offset
        ),

    -- Non-empty content_hash.
    CONSTRAINT resource_chunks_hash_nonempty
        CHECK (TRIM(content_hash) <> '')
);

CREATE INDEX IF NOT EXISTS idx_rc_version_chunk
    ON public.resource_chunks (resource_version_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_rc_content_hash
    ON public.resource_chunks (content_hash);

ALTER TABLE public.resource_chunks ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.resource_chunks IS
'Text segments derived from an exact resource_version.
Cascades on resource_version delete (immutability enforcement is deferred
to Phase 5B).  No embedding/vector columns in this phase.
Service-role / admin access only for this phase.';
