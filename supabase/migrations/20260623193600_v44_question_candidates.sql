-- =============================================================================
-- V44 Phase 4A: question_candidates staging table
-- Created : 2026-06-23 19:36:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds public.question_candidates, the staging area for generated or
-- manually authored question candidates before they enter the immutable
-- question_versions lifecycle.
--
-- Candidates are NEVER used directly by exam delivery or student-facing
-- reads.  They exist solely as a pre-approval staging area.
--
-- Promotion into immutable question_versions happens later through a
-- dedicated controlled RPC (Phase 4B).  Until promotion, no row in
-- question_versions, questions, or answer_options is created or modified.
--
-- Service-role and admin access only for this phase.  No anon or
-- authenticated policies are added here.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.question_candidates (

    -- -------------------------------------------------------------------------
    -- Identity
    -- -------------------------------------------------------------------------
    id                          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),

    -- -------------------------------------------------------------------------
    -- Targeting
    -- -------------------------------------------------------------------------

    -- The certification this candidate belongs to (required).
    certification_exam_name     text         NOT NULL,

    -- When non-null, this candidate is intended to update an existing live
    -- question.  Null means the candidate will create a new question on
    -- promotion.
    target_question_id          integer      NULL
                                    REFERENCES public.questions(id),

    -- -------------------------------------------------------------------------
    -- Lifecycle status
    -- -------------------------------------------------------------------------

    -- Allowed values: draft, audit_pending, audit_failed, review_pending,
    -- approved, rejected, promoted.
    -- See constraint question_candidates_status_valid below.
    candidate_status            text         NOT NULL DEFAULT 'draft',

    -- -------------------------------------------------------------------------
    -- Question content (mirrors question_versions fields)
    -- -------------------------------------------------------------------------
    question_text               text         NOT NULL,
    explanation                 text,
    category                    text,
    difficulty                  text,
    cognitive_level             text,
    concept_key                 text,
    question_type               text         NOT NULL,
    select_count                integer      NOT NULL,
    language_code               text,

    -- -------------------------------------------------------------------------
    -- Provenance
    -- -------------------------------------------------------------------------

    -- How the candidate was produced (e.g. generated, human_authored, import).
    source_type                 text         NOT NULL,

    -- Free-form reference to the upstream source (model name, batch id, etc.).
    source_reference            text,

    -- Deterministic content hash of question fields + options.
    -- Used for duplicate detection before promotion.
    content_hash                text         NOT NULL,

    -- Full candidate payload as received (options, metadata, raw model output).
    candidate_payload           jsonb        NOT NULL,

    -- -------------------------------------------------------------------------
    -- Audit
    -- -------------------------------------------------------------------------
    created_by                  text         NOT NULL,
    created_at                  timestamptz  NOT NULL DEFAULT now(),
    updated_at                  timestamptz  NOT NULL DEFAULT now(),

    -- -------------------------------------------------------------------------
    -- Promotion linkage
    -- -------------------------------------------------------------------------

    -- Set when candidate_status = 'promoted'.
    -- Null for all other statuses.
    -- See cross-field constraints below.
    promoted_question_version_id  uuid       NULL
                                    REFERENCES public.question_versions(id),

    -- -------------------------------------------------------------------------
    -- Extensibility
    -- -------------------------------------------------------------------------
    metadata                    jsonb        NOT NULL DEFAULT '{}'::jsonb,


    -- =========================================================================
    -- Constraints
    -- =========================================================================

    -- Non-empty question_text.
    CONSTRAINT question_candidates_text_nonempty
        CHECK (TRIM(question_text) <> ''),

    -- select_count must be positive.
    CONSTRAINT question_candidates_select_count_positive
        CHECK (select_count > 0),

    -- question_type must be single or multiple.
    CONSTRAINT question_candidates_type_valid
        CHECK (question_type IN ('single', 'multiple')),

    -- difficulty valid when provided.
    CONSTRAINT question_candidates_difficulty_valid
        CHECK (
            difficulty IS NULL
            OR difficulty IN ('easy', 'medium', 'hard')
        ),

    -- cognitive_level valid when provided.
    CONSTRAINT question_candidates_cognitive_level_valid
        CHECK (
            cognitive_level IS NULL
            OR cognitive_level IN (
                'recall', 'understanding', 'application', 'analysis', 'judgment'
            )
        ),

    -- candidate_status must be one of the defined lifecycle values.
    CONSTRAINT question_candidates_status_valid
        CHECK (
            candidate_status IN (
                'draft',
                'audit_pending',
                'audit_failed',
                'review_pending',
                'approved',
                'rejected',
                'promoted'
            )
        ),

    -- promoted status requires a promoted_question_version_id.
    CONSTRAINT question_candidates_promoted_requires_version
        CHECK (
            candidate_status <> 'promoted'
            OR promoted_question_version_id IS NOT NULL
        ),

    -- promoted_question_version_id requires promoted status.
    CONSTRAINT question_candidates_version_requires_promoted
        CHECK (
            promoted_question_version_id IS NULL
            OR candidate_status = 'promoted'
        )
);

-- =============================================================================
-- Indexes
-- =============================================================================

-- Status-based filtering (queue processing, review UI).
CREATE INDEX IF NOT EXISTS idx_qc_status
    ON public.question_candidates (candidate_status);

-- Per-certification filtering.
CREATE INDEX IF NOT EXISTS idx_qc_exam_name
    ON public.question_candidates (certification_exam_name);

-- Duplicate detection via content hash.
CREATE INDEX IF NOT EXISTS idx_qc_content_hash
    ON public.question_candidates (content_hash);

-- Lookup by target live question.
CREATE INDEX IF NOT EXISTS idx_qc_target_question
    ON public.question_candidates (target_question_id);

-- Recency ordering.
CREATE INDEX IF NOT EXISTS idx_qc_created_at
    ON public.question_candidates (created_at DESC);

-- Lookup by promoted version (for tracing candidate → version lineage).
CREATE INDEX IF NOT EXISTS idx_qc_promoted_version
    ON public.question_candidates (promoted_question_version_id);

-- =============================================================================
-- Row-Level Security
--
-- RLS is enabled.  No anon or authenticated write/read policies are added in
-- this phase.  Service-role connections bypass RLS.
-- Pipeline and admin policies will be added in a later migration.
-- =============================================================================

ALTER TABLE public.question_candidates ENABLE ROW LEVEL SECURITY;

-- =============================================================================
-- Table comment
-- =============================================================================

COMMENT ON TABLE public.question_candidates IS
'Staging area for generated or manually authored question candidates.
Candidates are NEVER used directly by exam delivery.
Promotion into immutable question_versions happens through a dedicated
controlled RPC (Phase 4B); no live questions or answer_options row is
created or modified until that point.
Service-role / admin access only for this phase.';
