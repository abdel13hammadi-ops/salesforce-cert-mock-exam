-- =============================================================================
-- V44 Backfill Question Versions
-- Created : 2026-06-23 18:22:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Snapshots the current live state of public.questions and public.answer_options
-- into the immutable version tables introduced by Phase 1
-- (20260623000000_v44_question_version_foundation.sql).
--
-- Every question that does not yet have a question_versions row receives one
-- baseline immutable version.  Matching answer options and a governance event
-- are created in the same transaction.
--
-- Safety guarantees
-- -----------------
--  * Purely additive: no row in questions, answer_options, exam_attempts, or
--    question_attempts is touched.
--  * Transactional: any failure rolls back the entire migration.
--  * Idempotent: safe to run twice; duplicate question versions, option
--    versions, and events are never created.
--  * Existing application reads continue unchanged; this migration only
--    writes to the three new version tables.
--
-- Expected columns in public.questions (confirmed from application code):
--   id, question_text, explanation, category, difficulty, question_type,
--   select_count, language_code, cognitive_level, concept_key,
--   content_version, external_key
--
-- Expected columns in public.answer_options (confirmed from application code):
--   id, question_id, option_label, option_text, is_correct, display_order
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Step 1 — Backfill question_versions
--
-- Inserts one baseline row per question that has no existing version row.
-- Idempotency: WHERE NOT EXISTS (... question_versions WHERE question_id = q.id)
-- ensures no duplicate is created if the migration is re-run.
--
-- version_number: uses questions.content_version when it is a positive integer,
-- otherwise falls back to 1.
--
-- content_hash: deterministic md5 over question content fields plus an ordered
-- aggregate of answer options.  Uses control-character separators (SOH = 0x01,
-- STX = 0x02, ETX = 0x03) that are extremely unlikely to appear in question
-- or option text, preventing hash collisions caused by adjacent-field boundary
-- ambiguity.
-- ---------------------------------------------------------------------------

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
    metadata
)
SELECT
    gen_random_uuid(),

    q.id,

    -- Use positive content_version when available, otherwise baseline 1.
    CASE
        WHEN q.content_version IS NOT NULL AND q.content_version > 0
        THEN q.content_version
        ELSE 1
    END,

    q.question_text,
    q.explanation,
    q.category,
    q.difficulty,
    q.cognitive_level,
    q.concept_key,
    q.question_type,
    q.select_count,
    q.language_code,

    -- Deterministic content hash.
    -- Inputs: all content fields + ordered answer options.
    -- Separators: SOH (x01) between fields, STX (x02) between option fields,
    --             ETX (x03) between option rows.
    md5(
        COALESCE(q.question_text,  '')        || E'\x01' ||
        COALESCE(q.explanation,    '')        || E'\x01' ||
        COALESCE(q.category,       '')        || E'\x01' ||
        COALESCE(q.difficulty,     '')        || E'\x01' ||
        COALESCE(q.question_type,  '')        || E'\x01' ||
        COALESCE(q.select_count::text, '')    || E'\x01' ||
        COALESCE(q.language_code,  '')        || E'\x01' ||
        COALESCE(q.cognitive_level,'')        || E'\x01' ||
        COALESCE(q.concept_key,    '')        || E'\x01' ||
        COALESCE(
            (
                SELECT string_agg(
                    ao.option_label                || E'\x02' ||
                    ao.option_text                 || E'\x02' ||
                    ao.is_correct::text,
                    E'\x03'
                    ORDER BY COALESCE(ao.display_order, 0) ASC, ao.option_label ASC
                )
                FROM public.answer_options ao
                WHERE ao.question_id = q.id
            ),
            ''
        )
    ),

    'legacy_backfill',
    'system:v44_backfill',

    jsonb_build_object(
        'original_content_version', q.content_version,
        'original_external_key',    q.external_key,
        'backfill_source',          'questions'
    )

FROM public.questions q
WHERE NOT EXISTS (
    SELECT 1
    FROM public.question_versions qv
    WHERE qv.question_id = q.id
);

-- ---------------------------------------------------------------------------
-- Step 2 — Backfill question_option_versions
--
-- Copies answer_options rows into option-version rows for every legacy_backfill
-- version, whether created in this run or a previous run.
--
-- Display-order strategy: ROW_NUMBER() partitioned by question_version_id,
-- ordered by:
--   1. CASE WHEN ao.display_order > 0 THEN ao.display_order END ASC NULLS LAST
--      — honours existing positive display_order; NULL and non-positive values
--      (the defect that caused the previous rollback) sort to the end
--   2. ao.option_label ASC  — stable alphabetical tie-breaker
--   3. ao.id ASC            — stable final tie-breaker (integer PK)
-- Result: contiguous integers starting at 1 per version, always positive,
-- always unique within a version, never NULL.  Satisfies both
-- CHECK (display_order > 0) and UNIQUE (question_version_id, display_order).
--
-- Idempotency: ON CONFLICT (question_version_id, option_label) DO NOTHING
-- targets the existing unique constraint question_option_versions_unique_label.
-- Each source answer_options row is evaluated independently:
--   * no existing versioned option for that label  → row is inserted
--   * versioned option already exists for that label → row is silently skipped
-- A partially populated version is repaired on re-run: missing options are
-- inserted while already-present options are skipped without error.
-- ---------------------------------------------------------------------------

WITH options_ranked AS (
    SELECT
        qv.id           AS question_version_id,
        ao.option_label,
        ao.option_text,
        ao.is_correct,
        ROW_NUMBER() OVER (
            PARTITION BY qv.id
            ORDER BY
                CASE WHEN ao.display_order > 0 THEN ao.display_order END ASC NULLS LAST,
                ao.option_label ASC,
                ao.id           ASC
        ) AS computed_display_order
    FROM public.question_versions qv
    JOIN public.answer_options ao
        ON ao.question_id = qv.question_id
    WHERE qv.source_type = 'legacy_backfill'
)
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
    question_version_id,
    option_label,
    option_text,
    is_correct,
    computed_display_order
FROM options_ranked
ON CONFLICT (question_version_id, option_label) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Step 3 — Backfill governance events
--
-- Inserts one 'created' event per baseline version that does not already
-- have a backfill event.
--
-- Idempotency: WHERE NOT EXISTS checks for an existing 'created' event
-- from 'system:v44_backfill' for the same question_version_id.
-- ---------------------------------------------------------------------------

INSERT INTO public.question_version_events (
    id,
    question_id,
    question_version_id,
    event_type,
    actor_email,
    reason,
    event_data
)
SELECT
    gen_random_uuid(),
    qv.question_id,
    qv.id,
    'created',
    'system:v44_backfill',
    'Initial immutable version created from live question tables',
    jsonb_build_object(
        'source_tables',        ARRAY['questions', 'answer_options'],
        'baseline_version_number', qv.version_number,
        'backfill_migration',   'v44_backfill_question_versions'
    )
FROM public.question_versions qv
WHERE qv.source_type = 'legacy_backfill'
  AND NOT EXISTS (
      SELECT 1
      FROM public.question_version_events qve
      WHERE qve.question_version_id = qv.id
        AND qve.event_type          = 'created'
        AND qve.actor_email         = 'system:v44_backfill'
  );

COMMIT;
