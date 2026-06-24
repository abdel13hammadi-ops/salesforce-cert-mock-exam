-- =============================================================================
-- V44 Phase 6A: Audit Runs and Findings Schema
-- Created : 2026-06-23 23:51:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds three additive tables for the audit pipeline:
--
--   audit_runs              — reproducible execution snapshots of one audit
--   audit_findings          — individual findings produced by an audit run
--   audit_finding_evidence  — resource chunks that support or contradict a
--                             finding (evidence must reference exact chunks)
--
-- Design rules
-- ------------
--   * Audit runs are reproducible execution snapshots: their model_name,
--     prompt_version, ruleset_version, and resource_snapshot are immutable
--     after creation.
--   * Findings attach to exact immutable question versions or staged
--     candidates; never to live questions directly.
--   * Evidence must reference exact resource_chunks rows; no free-form URLs.
--   * Service-role / admin access only for this phase.
--
-- No RPCs are added in this phase.
-- =============================================================================


-- =============================================================================
-- 1. audit_runs
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.audit_runs (
    id                          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_type                  text         NOT NULL,
    target_question_version_id  uuid         NULL
                                    REFERENCES public.question_versions(id),
    target_candidate_id         uuid         NULL
                                    REFERENCES public.question_candidates(id),
    run_status                  text         NOT NULL DEFAULT 'pending',
    model_name                  text,
    prompt_version              text,
    ruleset_version             text,
    resource_snapshot           jsonb        NOT NULL DEFAULT '{}'::jsonb,
    started_at                  timestamptz,
    completed_at                timestamptz,
    created_by                  text         NOT NULL,
    created_at                  timestamptz  NOT NULL DEFAULT now(),
    metadata                    jsonb        NOT NULL DEFAULT '{}'::jsonb,

    -- Exactly one target must be non-null.
    CONSTRAINT audit_runs_exactly_one_target
        CHECK (
            num_nonnulls(target_question_version_id, target_candidate_id) = 1
        ),

    -- audit_type valid values.
    CONSTRAINT audit_runs_type_valid
        CHECK (
            audit_type IN (
                'deterministic',
                'llm',
                'hybrid',
                'human'
            )
        ),

    -- run_status valid values.
    CONSTRAINT audit_runs_status_valid
        CHECK (
            run_status IN (
                'pending',
                'running',
                'completed',
                'failed',
                'cancelled'
            )
        ),

    -- completed_at must not precede started_at when both are present.
    CONSTRAINT audit_runs_completed_after_started
        CHECK (
            started_at   IS NULL
            OR completed_at IS NULL
            OR completed_at >= started_at
        )
);

CREATE INDEX IF NOT EXISTS idx_ar_question_version
    ON public.audit_runs (target_question_version_id);

CREATE INDEX IF NOT EXISTS idx_ar_candidate
    ON public.audit_runs (target_candidate_id);

CREATE INDEX IF NOT EXISTS idx_ar_run_status
    ON public.audit_runs (run_status);

CREATE INDEX IF NOT EXISTS idx_ar_audit_type
    ON public.audit_runs (audit_type);

CREATE INDEX IF NOT EXISTS idx_ar_created_at
    ON public.audit_runs (created_at DESC);

ALTER TABLE public.audit_runs ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.audit_runs IS
'Reproducible execution snapshots of one audit.
model_name, prompt_version, ruleset_version, and resource_snapshot are
intended to be immutable after creation (trigger enforcement deferred).
Findings attach to exact immutable question versions or staged candidates;
never to live questions directly.
Service-role / admin access only for this phase.';


-- =============================================================================
-- 2. audit_findings
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.audit_findings (
    id                  uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_run_id        uuid         NOT NULL
                            REFERENCES public.audit_runs(id) ON DELETE CASCADE,
    finding_code        text         NOT NULL,
    finding_type        text         NOT NULL,
    severity            text         NOT NULL,
    finding_status      text         NOT NULL DEFAULT 'open',
    title               text         NOT NULL,
    description         text         NOT NULL,
    field_path          text,
    confidence          numeric,
    detector_name       text,
    detector_version    text,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    resolved_at         timestamptz,
    resolved_by         text,
    resolution_reason   text,
    metadata            jsonb        NOT NULL DEFAULT '{}'::jsonb,

    -- Non-empty string fields.
    CONSTRAINT audit_findings_code_nonempty
        CHECK (TRIM(finding_code) <> ''),

    CONSTRAINT audit_findings_title_nonempty
        CHECK (TRIM(title) <> ''),

    CONSTRAINT audit_findings_description_nonempty
        CHECK (TRIM(description) <> ''),

    -- finding_type valid values.
    CONSTRAINT audit_findings_type_valid
        CHECK (
            finding_type IN (
                'correctness',
                'ambiguity',
                'duplication',
                'outdated',
                'formatting',
                'coverage',
                'difficulty',
                'cognitive_level',
                'answer_quality',
                'explanation_quality',
                'source_support',
                'policy',
                'other'
            )
        ),

    -- severity valid values.
    CONSTRAINT audit_findings_severity_valid
        CHECK (
            severity IN (
                'info',
                'low',
                'medium',
                'high',
                'critical'
            )
        ),

    -- finding_status valid values.
    CONSTRAINT audit_findings_status_valid
        CHECK (
            finding_status IN (
                'open',
                'accepted',
                'rejected',
                'resolved',
                'overridden'
            )
        ),

    -- confidence is nullable but must be in [0, 1] when provided.
    CONSTRAINT audit_findings_confidence_range
        CHECK (
            confidence IS NULL
            OR (confidence >= 0 AND confidence <= 1)
        ),

    -- resolved and overridden statuses require resolved_at, resolved_by,
    -- and resolution_reason.
    CONSTRAINT audit_findings_resolution_fields_required
        CHECK (
            finding_status NOT IN ('resolved', 'overridden')
            OR (
                resolved_at        IS NOT NULL
                AND resolved_by    IS NOT NULL
                AND resolution_reason IS NOT NULL
            )
        ),

    -- Statuses other than resolved/overridden must not have resolved_at set.
    CONSTRAINT audit_findings_unresolved_no_resolved_at
        CHECK (
            finding_status IN ('resolved', 'overridden')
            OR resolved_at IS NULL
        )
);

CREATE INDEX IF NOT EXISTS idx_af_audit_run
    ON public.audit_findings (audit_run_id);

CREATE INDEX IF NOT EXISTS idx_af_finding_code
    ON public.audit_findings (finding_code);

CREATE INDEX IF NOT EXISTS idx_af_severity
    ON public.audit_findings (severity);

CREATE INDEX IF NOT EXISTS idx_af_finding_status
    ON public.audit_findings (finding_status);

CREATE INDEX IF NOT EXISTS idx_af_created_at
    ON public.audit_findings (created_at DESC);

ALTER TABLE public.audit_findings ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.audit_findings IS
'Individual findings produced by an audit run.
Attached to exact audit_runs rows; indirectly tied to immutable question
versions or staged candidates through the parent audit_run.
Service-role / admin access only for this phase.';


-- =============================================================================
-- 3. audit_finding_evidence
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.audit_finding_evidence (
    id                  uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id          uuid         NOT NULL
                            REFERENCES public.audit_findings(id) ON DELETE CASCADE,
    resource_chunk_id   uuid         NOT NULL
                            REFERENCES public.resource_chunks(id),
    evidence_role       text         NOT NULL DEFAULT 'supporting',
    quote_text          text,
    relevance_score     numeric,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    metadata            jsonb        NOT NULL DEFAULT '{}'::jsonb,

    -- Each (finding, chunk, role) combination is unique.
    CONSTRAINT audit_finding_evidence_unique
        UNIQUE (finding_id, resource_chunk_id, evidence_role),

    -- evidence_role valid values.
    CONSTRAINT audit_finding_evidence_role_valid
        CHECK (
            evidence_role IN (
                'supporting',
                'contradicting',
                'contextual'
            )
        ),

    -- quote_text is nullable but must be non-empty when provided.
    CONSTRAINT audit_finding_evidence_quote_nonempty
        CHECK (
            quote_text IS NULL
            OR TRIM(quote_text) <> ''
        ),

    -- relevance_score is nullable but must be in [0, 1] when provided.
    CONSTRAINT audit_finding_evidence_relevance_range
        CHECK (
            relevance_score IS NULL
            OR (relevance_score >= 0 AND relevance_score <= 1)
        )
);

CREATE INDEX IF NOT EXISTS idx_afe_finding
    ON public.audit_finding_evidence (finding_id);

CREATE INDEX IF NOT EXISTS idx_afe_chunk
    ON public.audit_finding_evidence (resource_chunk_id);

CREATE INDEX IF NOT EXISTS idx_afe_role
    ON public.audit_finding_evidence (evidence_role);

ALTER TABLE public.audit_finding_evidence ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.audit_finding_evidence IS
'Resource chunks that support, contradict, or contextualise an audit finding.
Evidence must reference exact resource_chunks rows — no free-form URLs.
Service-role / admin access only for this phase.';
