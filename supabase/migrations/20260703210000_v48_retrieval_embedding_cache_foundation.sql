-- =============================================================================
-- V48 hybrid_question_match_v2 Stage 2 prerequisite: retrieval_embedding_cache
-- Created : 2026-07-03 21:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds one additive, immutable table, retrieval_embedding_cache, to persist
-- computed text embeddings keyed by exactly what produced them: the content
-- being embedded, and the provider/model/version/dimensionality that
-- produced the vector. This is schema-only persistence infrastructure for a
-- later hybrid_question_match_v2 semantic-reranking slice; no embedding
-- provider is called, no worker reads or writes this table, and no
-- semantic-qualification or retrieval-method-deduplication logic is
-- implemented here.
--
-- Design rationale
-- ----------------
--   * Durable, not in-process: a durable, service-role-only table (rather
--     than an in-process cache) is required so that multiple concurrent
--     workers, and any single worker across restarts, share one
--     provider-call-deduplication surface and one deterministic-replay
--     source of truth.
--   * Vector storage without pgvector: the repository does not currently
--     install or reference the pgvector extension anywhere in
--     supabase/migrations, and this task explicitly forbids introducing it.
--     embedding_vector is therefore stored as a plain PostgreSQL
--     double precision[] (float8[]), which every embedding provider's
--     output can be written into directly with no extension dependency.
--     Cardinality/dimension integrity is enforced with CHECK constraints
--     (see below) rather than a vector-typed column.
--   * Cache key: the UNIQUE identity is
--     (content_scope, content_hash, embedding_provider_name,
--     embedding_model_name, embedding_model_version, embedding_dimensions).
--     Two independent content items are never conflated (content_scope +
--     content_hash), and a content-hash invalidation (source text changes)
--     naturally produces a new, distinct cache key rather than requiring an
--     UPDATE to an existing row. Changing provider, model, model version,
--     or requested output dimensionality is a genuinely different
--     embedding and therefore a distinct cache key, not a mutation of an
--     existing one -- so no UPDATE-based mutation of this table is ever
--     required; rows are write-once and immutable once inserted.
--
-- Isolation from other V48 tables
-- --------------------------------
-- This migration does not modify retrieval_shadow_evaluations, audit_runs,
-- audit_run_dedup_keys, audit_run_evidence_set, or any other existing
-- table. No column here references any of those tables, and no foreign key
-- targets them.
--
-- Security
-- --------
-- Row Level Security is enabled with no anon/authenticated policies. RLS is
-- not relied on alone: table privileges are explicitly revoked from
-- PUBLIC, anon, authenticated, AND service_role (service_role can otherwise
-- retain broader direct ACL grants from the table owner that a plain
-- additive GRANT never strips -- see
-- 20260703200000_v48_retrieval_shadow_evaluations_privilege_correction.sql
-- for the exact failure mode this avoids), and only SELECT, INSERT, DELETE
-- are then re-granted to service_role. No UPDATE, TRUNCATE, REFERENCES,
-- TRIGGER, or MAINTAIN privilege is granted. No RPCs are added in this
-- migration, so there is no public read/write surface for this table and
-- no embedding vector is ever exposed to application users.
-- =============================================================================


-- =============================================================================
-- retrieval_embedding_cache
--    One immutable, durable embedding-provider result per
--    (content_scope, content_hash, embedding_provider_name,
--    embedding_model_name, embedding_model_version, embedding_dimensions).
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.retrieval_embedding_cache (
    id                          uuid              PRIMARY KEY DEFAULT gen_random_uuid(),
    content_scope               text              NOT NULL,
    content_hash                text              NOT NULL,
    embedding_provider_name     text              NOT NULL,
    embedding_model_name        text              NOT NULL,
    embedding_model_version     text              NOT NULL,
    embedding_dimensions        integer           NOT NULL,
    embedding_vector            double precision[] NOT NULL,
    provider_response_hash      text              NOT NULL,
    created_at                  timestamptz       NOT NULL DEFAULT now(),

    -- Cache identity: two independent content items, or two embeddings
    -- produced by a different provider/model/version/dimensionality, are
    -- always distinct rows -- never merged, never updated in place.
    CONSTRAINT retrieval_embedding_cache_unique_identity
        UNIQUE (
            content_scope,
            content_hash,
            embedding_provider_name,
            embedding_model_name,
            embedding_model_version,
            embedding_dimensions
        ),

    CONSTRAINT retrieval_embedding_cache_content_scope_valid
        CHECK (content_scope IN ('query', 'chunk')),

    CONSTRAINT retrieval_embedding_cache_provider_name_nonempty
        CHECK (TRIM(embedding_provider_name) <> ''),

    CONSTRAINT retrieval_embedding_cache_model_name_nonempty
        CHECK (TRIM(embedding_model_name) <> ''),

    CONSTRAINT retrieval_embedding_cache_model_version_nonempty
        CHECK (TRIM(embedding_model_version) <> ''),

    -- Lowercase 64-character SHA-256 hex digest (same format convention as
    -- audit_run_dedup_keys_evidence_hash_format).
    CONSTRAINT retrieval_embedding_cache_content_hash_format
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT retrieval_embedding_cache_provider_response_hash_format
        CHECK (provider_response_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT retrieval_embedding_cache_dimensions_positive
        CHECK (embedding_dimensions > 0),

    -- embedding_vector must be a genuinely one-dimensional array (rejects
    -- e.g. a nested/2-D array written by a provider-integration bug).
    CONSTRAINT retrieval_embedding_cache_vector_is_one_dimensional
        CHECK (array_ndims(embedding_vector) = 1),

    -- Vector cardinality must exactly match the declared dimensionality.
    -- COALESCE guards the zero-length-array edge case, where
    -- array_length() returns NULL (not 0) and a bare equality check would
    -- otherwise be silently satisfied by CHECK's NULL-passes semantics.
    CONSTRAINT retrieval_embedding_cache_vector_cardinality_matches_dimensions
        CHECK (COALESCE(array_length(embedding_vector, 1), 0) = embedding_dimensions),

    -- No NULL elements anywhere in the vector. array_position() compares
    -- with IS NOT DISTINCT FROM semantics, so searching for a NULL needle
    -- correctly finds a NULL element instead of vacuously returning NULL.
    CONSTRAINT retrieval_embedding_cache_vector_has_no_null_elements
        CHECK (array_position(embedding_vector, NULL::double precision) IS NULL)
);

-- Dedicated lookup path for content-hash invalidation: find every cached
-- embedding for a given piece of content regardless of which
-- provider/model/version/dimensionality produced it, without requiring the
-- caller to also know content_scope up front.
CREATE INDEX IF NOT EXISTS idx_rec_content_hash
    ON public.retrieval_embedding_cache (content_hash);

-- Dedicated lookup path for a full model-version change/rollout: find every
-- cached embedding for one provider/model/version combination across all
-- content, independent of content_scope or content_hash.
CREATE INDEX IF NOT EXISTS idx_rec_provider_model_version
    ON public.retrieval_embedding_cache (
        embedding_provider_name,
        embedding_model_name,
        embedding_model_version
    );

ALTER TABLE public.retrieval_embedding_cache ENABLE ROW LEVEL SECURITY;


-- =============================================================================
-- Privilege hardening
--    Table privileges are explicitly revoked from PUBLIC, anon,
--    authenticated, AND service_role before re-granting service_role only
--    SELECT/INSERT/DELETE. Revoking from service_role first (rather than
--    relying on the plain additive GRANT below) prevents service_role from
--    retaining any broader direct ACL grant the table owner may already
--    hold for this role (UPDATE/TRUNCATE/REFERENCES/TRIGGER/MAINTAIN),
--    exactly as corrected for retrieval_shadow_evaluations in
--    20260703200000_v48_retrieval_shadow_evaluations_privilege_correction.sql.
--    No UPDATE is granted: rows are immutable and write-once (a
--    content/provider/model/version/dimension change is a new cache key,
--    never a mutation of an existing row). DELETE remains available to
--    service_role for cache maintenance/rollback (e.g. clearing stale
--    entries after a genuine content-hash invalidation).
-- =============================================================================

REVOKE ALL ON TABLE public.retrieval_embedding_cache FROM PUBLIC;
REVOKE ALL ON TABLE public.retrieval_embedding_cache FROM anon;
REVOKE ALL ON TABLE public.retrieval_embedding_cache FROM authenticated;
REVOKE ALL ON TABLE public.retrieval_embedding_cache FROM service_role;
GRANT SELECT, INSERT, DELETE ON TABLE public.retrieval_embedding_cache TO service_role;

COMMENT ON TABLE public.retrieval_embedding_cache IS
'Durable, immutable embedding-provider result cache keyed by
(content_scope, content_hash, embedding_provider_name, embedding_model_name,
embedding_model_version, embedding_dimensions). Schema-only persistence
infrastructure for a later hybrid_question_match_v2 semantic-reranking
slice: no embedding-provider call, worker wiring, or semantic-qualification
logic is implemented by this migration. Supports multiple workers, worker
restarts, provider-call deduplication, deterministic replay, model-version
changes, dimension-specific embeddings, and content-hash invalidation
(a changed content_hash simply produces a new cache key; rows are never
updated in place). No pgvector dependency: embedding_vector is a plain
double precision[] with cardinality/dimension integrity enforced by CHECK
constraints. Service-role / admin access only: RLS is enabled, table
privileges are explicitly revoked from PUBLIC, anon, authenticated, and
service_role, and only SELECT, INSERT, DELETE are then re-granted to
service_role. No RPCs are added, so no embedding vector is ever exposed to
application users.';

COMMENT ON COLUMN public.retrieval_embedding_cache.content_scope IS
'What kind of content was embedded: "query" (a question''s BM25 query text)
or "chunk" (a resource_chunk''s body/title/metadata text). Constrained to
exactly these two values.';

COMMENT ON COLUMN public.retrieval_embedding_cache.content_hash IS
'Lowercase 64-character SHA-256 hex digest of the exact text that was
embedded. A changed content_hash for the same logical content is a
content-hash invalidation: it produces a new cache key rather than
updating this row.';

COMMENT ON COLUMN public.retrieval_embedding_cache.embedding_vector IS
'The computed embedding as a one-dimensional double precision[] (float8[]).
Cardinality is enforced by CHECK to exactly equal embedding_dimensions, and
no element may be NULL. Never exposed to application users (no RPC reads
this column).';

COMMENT ON COLUMN public.retrieval_embedding_cache.provider_response_hash IS
'Lowercase 64-character SHA-256 hex digest of the raw provider response
that produced embedding_vector, retained for deterministic-replay
provenance and provider-call deduplication auditing.';
