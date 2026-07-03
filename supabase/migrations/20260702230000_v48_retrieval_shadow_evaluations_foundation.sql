-- =============================================================================
-- V48 hybrid_question_match_v2 Stage 1: retrieval_shadow_evaluations foundation
-- Created : 2026-07-02 23:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds one additive table, retrieval_shadow_evaluations, to persist the
-- deterministic Stage 1 shadow-classification result produced offline by
-- workers/ai_quality_audit_shadow.py (classify_question_shadow_from_replay_record)
-- for one question in one evaluation sweep. bm25_question_match_v1 evidence
-- selection and its frozen replay tests are entirely unaffected: this
-- migration only adds a new table.
--
-- Isolation from live audit execution
-- ------------------------------------
-- Shadow evaluations are structurally isolated from Pass A/B/C execution:
--   * No column references audit_runs, audit_run_dedup_keys, or
--     audit_run_evidence_set, and no foreign key targets those tables.
--   * evaluation_run_id is a caller-supplied identifier for one offline
--     classification sweep (analogous in role to pilot_batch_id on
--     audit_run_dedup_keys); it is intentionally NOT a foreign key to
--     audit_runs, so shadow sweeps can be created, replayed, and deleted
--     independently of any live or historical audit run.
--   * Deleting rows here can never cascade into audit_runs or any of its
--     dependent tables, and deleting an audit_runs row can never cascade
--     into this table.
--
-- Scope of this migration (Stage 1 persistence only)
-- ----------------------------------------------------
-- This table stores exactly the Stage 1 fields already produced by
-- classify_question_shadow_from_replay_record: confidence_class,
-- candidate_count, qualified_count_v1, structural_candidate_count, and the
-- full per-candidate decision payload (candidates_json). It intentionally
-- excludes anything belonging to a later additive slice:
--   * semantic similarity / embeddings / embedding model or version
--   * provider errors
--   * qualified_v2 or any L3/L4 semantic-eligibility/qualification decision
--   * audit_run_id or evidence_set_hash
-- No embedding cache, Stage 2 qualification logic, retrieval-method
-- deduplication change, provider integration, or worker wiring is
-- implemented here.
--
-- Security
-- --------
-- Row Level Security is enabled with no anon/authenticated policies, the
-- same service-role-only pattern used by audit_run_dedup_keys,
-- audit_run_evidence_set, audit_run_pass_results, and
-- audit_run_dispute_triggers (20260630120000_v48_ai_quality_audit_foundation.sql).
-- RLS is not relied on alone: table privileges are also explicitly revoked
-- from PUBLIC, anon, and authenticated, with only service_role granted
-- SELECT/INSERT/DELETE, matching audit_finding_decisions
-- (20260624230000_v45_audit_finding_review_workflow.sql) and free_mock_sets /
-- free_mock_set_items (20260629120000_v46_free_mock_curation_foundation.sql).
-- No RPCs are added in this migration, so there is no public insert/update/
-- delete surface for this table.
-- =============================================================================


-- =============================================================================
-- retrieval_shadow_evaluations
--    One deterministic Stage 1 shadow-classification result per
--    (evaluation_run_id, question_version_id, proposed_retrieval_method,
--    schema_version). question_version_id references the immutable
--    question_versions table using the same no-cascade convention as
--    audit_run_dedup_keys.target_question_version_id: question_versions
--    rows are never deleted, so no ON DELETE behavior is required.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.retrieval_shadow_evaluations (
    id                          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    evaluation_run_id           uuid         NOT NULL,
    question_version_id         uuid         NOT NULL
                                    REFERENCES public.question_versions(id),
    certification_exam_name     text         NOT NULL,
    baseline_retrieval_method   text         NOT NULL,
    proposed_retrieval_method   text         NOT NULL,
    schema_version              text         NOT NULL,
    confidence_class            text         NOT NULL,
    candidate_count             integer      NOT NULL,
    qualified_count_v1          integer      NOT NULL,
    structural_candidate_count  integer      NOT NULL,
    candidates_json             jsonb        NOT NULL,
    created_at                  timestamptz  NOT NULL DEFAULT now(),

    -- Unique evaluation identity: at most one Stage 1 result per question
    -- per proposed retrieval method per schema version within one sweep.
    CONSTRAINT retrieval_shadow_evaluations_unique_identity
        UNIQUE (
            evaluation_run_id,
            question_version_id,
            proposed_retrieval_method,
            schema_version
        ),

    CONSTRAINT retrieval_shadow_evaluations_confidence_class_valid
        CHECK (
            confidence_class IN (
                'v1_sufficient',
                'semantic_review_candidate',
                'no_structural_candidate'
            )
        ),

    CONSTRAINT retrieval_shadow_evaluations_candidate_count_nonneg
        CHECK (candidate_count >= 0),

    CONSTRAINT retrieval_shadow_evaluations_qualified_v1_nonneg
        CHECK (qualified_count_v1 >= 0),

    CONSTRAINT retrieval_shadow_evaluations_structural_count_nonneg
        CHECK (structural_candidate_count >= 0),

    -- Counts must be internally consistent: qualified/structural candidates
    -- are a subset of all evaluated candidates.
    CONSTRAINT retrieval_shadow_evaluations_qualified_v1_le_candidates
        CHECK (qualified_count_v1 <= candidate_count),

    CONSTRAINT retrieval_shadow_evaluations_structural_le_candidates
        CHECK (structural_candidate_count <= candidate_count),

    -- Every V1-qualified candidate passed the L1 structural guards first
    -- (see _l1_structural_guards_pass / _candidate_qualifies), so qualified
    -- candidates are always a subset of structural candidates.
    CONSTRAINT retrieval_shadow_evaluations_qualified_v1_le_structural
        CHECK (qualified_count_v1 <= structural_candidate_count),

    CONSTRAINT retrieval_shadow_evaluations_candidates_json_is_array
        CHECK (jsonb_typeof(candidates_json) = 'array'),

    -- candidate_count must exactly equal the number of candidate entries
    -- actually persisted in candidates_json (no undercount/overcount drift).
    CONSTRAINT retrieval_shadow_evaluations_candidate_count_matches_json
        CHECK (candidate_count = jsonb_array_length(candidates_json)),

    -- Confidence-class / count coupling mirrors the exact classification
    -- rules in workers/ai_quality_audit_shadow.py::_classify_confidence:
    --   v1_sufficient            -> qualified_count_v1 > 0
    --   semantic_review_candidate -> qualified_count_v1 = 0 AND structural > 0
    --   no_structural_candidate   -> qualified_count_v1 = 0 AND structural = 0
    CONSTRAINT retrieval_shadow_evaluations_confidence_class_count_coupling
        CHECK (
            (
                confidence_class = 'v1_sufficient'
                AND qualified_count_v1 > 0
            )
            OR (
                confidence_class = 'semantic_review_candidate'
                AND qualified_count_v1 = 0
                AND structural_candidate_count > 0
            )
            OR (
                confidence_class = 'no_structural_candidate'
                AND qualified_count_v1 = 0
                AND structural_candidate_count = 0
            )
        ),

    CONSTRAINT retrieval_shadow_evaluations_exam_name_nonempty
        CHECK (TRIM(certification_exam_name) <> ''),

    CONSTRAINT retrieval_shadow_evaluations_baseline_method_nonempty
        CHECK (TRIM(baseline_retrieval_method) <> ''),

    CONSTRAINT retrieval_shadow_evaluations_proposed_method_nonempty
        CHECK (TRIM(proposed_retrieval_method) <> ''),

    CONSTRAINT retrieval_shadow_evaluations_schema_version_nonempty
        CHECK (TRIM(schema_version) <> '')
);

CREATE INDEX IF NOT EXISTS idx_rse_question_version
    ON public.retrieval_shadow_evaluations (question_version_id);

CREATE INDEX IF NOT EXISTS idx_rse_evaluation_run
    ON public.retrieval_shadow_evaluations (evaluation_run_id);

ALTER TABLE public.retrieval_shadow_evaluations ENABLE ROW LEVEL SECURITY;


-- =============================================================================
-- Privilege hardening
--    RLS alone is not relied upon: table privileges are explicitly revoked
--    from PUBLIC, anon, and authenticated, and only service_role is granted
--    access, matching the pattern used by audit_finding_decisions
--    (20260624230000_v45_audit_finding_review_workflow.sql) and
--    free_mock_sets / free_mock_set_items
--    (20260629120000_v46_free_mock_curation_foundation.sql). service_role
--    retains SELECT/INSERT for writing and comparing shadow-evaluation
--    sweeps and DELETE so individual sweeps can be rolled back independently
--    for maintenance without touching audit_runs or its dependents. No
--    UPDATE is granted: each row is a deterministic, write-once Stage 1
--    result and is never mutated in place.
-- =============================================================================

REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM PUBLIC;
REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM anon;
REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM authenticated;
GRANT SELECT, INSERT, DELETE ON TABLE public.retrieval_shadow_evaluations TO service_role;

COMMENT ON TABLE public.retrieval_shadow_evaluations IS
'Deterministic Stage 1 hybrid_question_match_v2 shadow-classification result
per (evaluation_run_id, question_version_id, proposed_retrieval_method,
schema_version), produced offline by
workers/ai_quality_audit_shadow.py::classify_question_shadow_from_replay_record.
Structurally isolated from live audit execution: no column or foreign key
references audit_runs, audit_run_dedup_keys, or audit_run_evidence_set, so
shadow sweeps can be inserted, replayed, or deleted independently without
affecting Pass A/B/C or any live evidence_set_hash. Stage 1 only: no
semantic similarity, embedding, provider-error, qualified_v2, or L3/L4
fields are present; those belong to a later additive slice.
Service-role / admin access only: no anon or authenticated RLS policies, and
table privileges are explicitly revoked from PUBLIC, anon, and authenticated
(only service_role holds SELECT/INSERT/DELETE).';

COMMENT ON COLUMN public.retrieval_shadow_evaluations.evaluation_run_id IS
'Caller-supplied identifier for one offline shadow-classification sweep.
Intentionally not a foreign key to audit_runs so shadow sweeps remain
independently creatable, replayable, and deletable.';

COMMENT ON COLUMN public.retrieval_shadow_evaluations.candidates_json IS
'Full per-candidate Stage 1 decision payload (the "candidates" list from
classify_question_shadow_from_replay_record): stable identity fields plus
relevance_score, applicable_threshold, l1_structural_guards_pass,
l2_relevance_gate_pass, qualified_v1, rejection_reason, and match_reasons
for every evaluated candidate. Must be a JSON array.';
