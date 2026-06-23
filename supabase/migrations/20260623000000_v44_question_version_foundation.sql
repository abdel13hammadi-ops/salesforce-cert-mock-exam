-- =============================================================================
-- V44 Question-Version Foundation
-- Created : 2026-06-23 00:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds three additive tables that form the immutable question-version
-- foundation required for the automated Content Pipeline:
--
--   question_versions        — immutable content snapshots per question
--   question_option_versions — immutable option snapshots per version
--   question_version_events  — full audit trail of version lifecycle events
--
-- These tables are purely additive.  No existing table is modified.
-- Current exam delivery, question_attempts, and Admin Import are unaffected.
--
-- Schema note
-- -----------
-- public.questions.id is PostgreSQL integer (int4), confirmed 2026-06-23
-- against the live Supabase schema.  FK columns use the same type.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. question_versions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.question_versions (
    id                   uuid                     PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id          integer                  NOT NULL REFERENCES public.questions(id),
    version_number       integer                  NOT NULL,
    question_text        text                     NOT NULL,
    explanation          text,
    category             text,
    difficulty           text,
    cognitive_level      text,
    concept_key          text,
    question_type        text                     NOT NULL,
    select_count         integer                  NOT NULL,
    language_code        text,
    content_hash         text                     NOT NULL,
    source_type          text,
    created_at           timestamptz              NOT NULL DEFAULT now(),
    created_by           text,
    supersedes_version_id uuid                   REFERENCES public.question_versions(id),
    metadata             jsonb                    NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT question_versions_unique_version
        UNIQUE (question_id, version_number),

    CONSTRAINT question_versions_version_positive
        CHECK (version_number > 0),

    CONSTRAINT question_versions_select_count_positive
        CHECK (select_count > 0),

    CONSTRAINT question_versions_type_valid
        CHECK (question_type IN ('single', 'multiple')),

    CONSTRAINT question_versions_difficulty_valid
        CHECK (difficulty IS NULL OR difficulty IN ('easy', 'medium', 'hard')),

    CONSTRAINT question_versions_cognitive_level_valid
        CHECK (
            cognitive_level IS NULL
            OR cognitive_level IN (
                'recall', 'understanding', 'application', 'analysis', 'judgment'
            )
        )
);

-- ---------------------------------------------------------------------------
-- 2. question_option_versions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.question_option_versions (
    id                   uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    question_version_id  uuid         NOT NULL REFERENCES public.question_versions(id) ON DELETE CASCADE,
    option_label         text         NOT NULL,
    option_text          text         NOT NULL,
    is_correct           boolean      NOT NULL,
    display_order        integer      NOT NULL,
    created_at           timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT question_option_versions_unique_label
        UNIQUE (question_version_id, option_label),

    CONSTRAINT question_option_versions_unique_order
        UNIQUE (question_version_id, display_order),

    CONSTRAINT question_option_versions_display_order_positive
        CHECK (display_order > 0)
);

-- ---------------------------------------------------------------------------
-- 3. question_version_events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.question_version_events (
    id                   uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id          integer              REFERENCES public.questions(id),
    question_version_id  uuid         REFERENCES public.question_versions(id),
    event_type           text         NOT NULL,
    actor_email          text,
    reason               text,
    event_data           jsonb        NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT question_version_events_type_valid
        CHECK (
            event_type IN (
                'created',
                'submitted_for_review',
                'approved',
                'rejected',
                'published',
                'superseded',
                'override_applied',
                'deactivated'
            )
        )
);

-- ---------------------------------------------------------------------------
-- 4. Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_qv_question_version
    ON public.question_versions (question_id, version_number DESC);

CREATE INDEX IF NOT EXISTS idx_qv_content_hash
    ON public.question_versions (content_hash);

CREATE INDEX IF NOT EXISTS idx_qv_cognitive_level
    ON public.question_versions (cognitive_level);

CREATE INDEX IF NOT EXISTS idx_qv_concept_key
    ON public.question_versions (concept_key);

CREATE INDEX IF NOT EXISTS idx_qov_version_id
    ON public.question_option_versions (question_version_id);

CREATE INDEX IF NOT EXISTS idx_qve_question_created
    ON public.question_version_events (question_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_qve_version_created
    ON public.question_version_events (question_version_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 5. Row-Level Security
--
-- Service-role access currently bypasses RLS for all three tables.
-- Pipeline write policies and admin read/write policies will be added in a
-- later migration once the approval workflow is defined.
-- ---------------------------------------------------------------------------

ALTER TABLE public.question_versions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.question_option_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.question_version_events  ENABLE ROW LEVEL SECURITY;
