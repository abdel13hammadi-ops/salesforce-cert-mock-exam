-- =============================================================================
-- V48 Phase 1: AI Quality-Audit Foundation (10-question smoke slice)
-- Created : 2026-06-30 12:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Adds the additive schema required for the AI technical-quality audit
-- (blind Pass A, evidence-backed Pass B, independent dispute Pass C):
--
--   * audit_runs.audit_type  + 'ai_quality'
--   * audit_runs.run_status  + 'inconclusive'
--   * audit_run_dedup_keys      — authoritative seven-key dedup boundary,
--                                 enforced BEFORE any pass executes
--   * audit_run_evidence_set    — frozen, ranked, bounded evidence set
--   * audit_run_pass_results    — durable per-pass state, lease, attempts
--   * audit_run_dispute_triggers — persisted gate for Pass C eligibility
--
-- Design rules
-- ------------
--   * All four new tables are purely additive. No existing table is dropped
--     or renamed. Existing CHECK values on audit_runs are preserved; only
--     one additive value is appended to each of the two extended
--     constraints.
--   * No live questions, answers, explanations, resource chunks, or
--     existing audit_findings rows are modified by this migration.
--   * No RPCs are added in this migration (see the companion RPCs
--     migration). No worker logic is implemented here.
--   * Service-role / admin access only. RLS enabled on all four new
--     tables; no anon or authenticated policies or grants.
-- =============================================================================


-- =============================================================================
-- 1. Extend audit_runs.audit_type and audit_runs.run_status
--    (additive only — every previously allowed value is preserved)
-- =============================================================================

ALTER TABLE public.audit_runs
    DROP CONSTRAINT IF EXISTS audit_runs_type_valid;

ALTER TABLE public.audit_runs
    ADD CONSTRAINT audit_runs_type_valid
        CHECK (
            audit_type IN (
                'deterministic',
                'llm',
                'hybrid',
                'human',
                'ai_quality'
            )
        );

ALTER TABLE public.audit_runs
    DROP CONSTRAINT IF EXISTS audit_runs_status_valid;

ALTER TABLE public.audit_runs
    ADD CONSTRAINT audit_runs_status_valid
        CHECK (
            run_status IN (
                'pending',
                'running',
                'completed',
                'failed',
                'cancelled',
                'inconclusive'
            )
        );

COMMENT ON CONSTRAINT audit_runs_type_valid ON public.audit_runs IS
'V48: added ai_quality for the multi-pass blind/evidence/dispute audit
pipeline. All prior values (deterministic, llm, hybrid, human) preserved.';

COMMENT ON CONSTRAINT audit_runs_status_valid ON public.audit_runs IS
'V48: added inconclusive — a terminal state distinct from failed, used when
all deterministic retry/escalation paths for an ai_quality run are exhausted
without a confirmable resolution. All prior values preserved.';


-- =============================================================================
-- 2. audit_run_dedup_keys
--    Authoritative pre-execution idempotency boundary. A row here can only
--    be inserted once the corresponding audit_runs row exists (FK), so the
--    seven-key UNIQUE constraint is the single source of truth for "has
--    this exact run already been created" — checked BEFORE any pass runs.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.audit_run_dedup_keys (
    id                          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_run_id                uuid         NOT NULL UNIQUE
                                    REFERENCES public.audit_runs(id) ON DELETE CASCADE,
    target_question_version_id  uuid         NOT NULL
                                    REFERENCES public.question_versions(id),
    prompt_version               text         NOT NULL,
    ruleset_version              text         NOT NULL,
    primary_model_name           text         NOT NULL,
    dispute_model_name           text         NOT NULL,
    evidence_set_hash            text         NOT NULL,
    pilot_batch_id                text         NOT NULL,
    created_at                   timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT audit_run_dedup_keys_prompt_version_nonempty
        CHECK (TRIM(prompt_version) <> ''),

    CONSTRAINT audit_run_dedup_keys_ruleset_version_nonempty
        CHECK (TRIM(ruleset_version) <> ''),

    CONSTRAINT audit_run_dedup_keys_primary_model_nonempty
        CHECK (TRIM(primary_model_name) <> ''),

    CONSTRAINT audit_run_dedup_keys_dispute_model_nonempty
        CHECK (TRIM(dispute_model_name) <> ''),

    CONSTRAINT audit_run_dedup_keys_pilot_batch_nonempty
        CHECK (TRIM(pilot_batch_id) <> ''),

    -- Lowercase 64-character SHA-256 hex digest.
    CONSTRAINT audit_run_dedup_keys_evidence_hash_format
        CHECK (evidence_set_hash ~ '^[0-9a-f]{64}$'),

    -- Authoritative seven-key deduplication boundary.
    CONSTRAINT audit_run_dedup_keys_seven_key_unique
        UNIQUE (
            target_question_version_id,
            prompt_version,
            ruleset_version,
            primary_model_name,
            dispute_model_name,
            evidence_set_hash,
            pilot_batch_id
        )
);

CREATE INDEX IF NOT EXISTS idx_ardk_question_version
    ON public.audit_run_dedup_keys (target_question_version_id);

ALTER TABLE public.audit_run_dedup_keys ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.audit_run_dedup_keys IS
'Authoritative seven-key idempotency boundary for ai_quality audit runs.
A duplicate (question_version, prompt_version, ruleset_version,
primary_model_name, dispute_model_name, evidence_set_hash, pilot_batch_id)
combination cannot create a second row here, which is checked BEFORE any
model pass executes (see create_or_get_ai_quality_audit_run_v1).
Service-role / admin access only. No anon or authenticated policies.';


-- =============================================================================
-- 3. audit_run_evidence_set
--    Frozen, ranked, bounded evidence set for the run, persisted
--    independently of any final finding. Real FK to resource_chunks(id).
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.audit_run_evidence_set (
    id                          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_run_id                uuid         NOT NULL
                                    REFERENCES public.audit_runs(id) ON DELETE CASCADE,
    resource_chunk_id            uuid         NOT NULL
                                    REFERENCES public.resource_chunks(id),
    retrieval_rank                integer      NOT NULL,
    relevance_score                numeric      NULL,
    content_hash_at_execution      text         NOT NULL,
    metadata                       jsonb        NOT NULL DEFAULT '{}'::jsonb,
    created_at                     timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT audit_run_evidence_set_rank_positive
        CHECK (retrieval_rank > 0),

    CONSTRAINT audit_run_evidence_set_relevance_range
        CHECK (relevance_score IS NULL OR relevance_score BETWEEN 0 AND 1),

    CONSTRAINT audit_run_evidence_set_content_hash_nonempty
        CHECK (TRIM(content_hash_at_execution) <> ''),

    CONSTRAINT audit_run_evidence_set_unique_chunk
        UNIQUE (audit_run_id, resource_chunk_id),

    CONSTRAINT audit_run_evidence_set_unique_rank
        UNIQUE (audit_run_id, retrieval_rank)
);

-- NOTE: no separate (audit_run_id, retrieval_rank) index is created here.
-- audit_run_evidence_set_unique_rank already creates an implicit unique
-- btree index on exactly this column pair; a second non-unique index on the
-- same columns in the same order would be an exact, wasteful duplicate.

ALTER TABLE public.audit_run_evidence_set ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.audit_run_evidence_set IS
'Frozen, ranked, bounded evidence set captured at audit-run creation time.
Persisted independently of final findings so Pass B retains full evidence
context even when the final result is clean or inconclusive. The run-level
evidence_set_hash on audit_run_dedup_keys is a deterministic SHA-256 over
this table''s rows ordered by retrieval_rank.
Service-role / admin access only. No anon or authenticated policies.';


-- =============================================================================
-- 4. audit_run_pass_results
--    Durable per-pass state surviving worker crashes, retries, and expired
--    leases. One row per (audit_run_id, pass_code); transitions happen
--    in place under row-level locking, never via a second row.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.audit_run_pass_results (
    id                          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_run_id                 uuid         NOT NULL
                                    REFERENCES public.audit_runs(id) ON DELETE CASCADE,
    pass_code                     text         NOT NULL,
    status                        text         NOT NULL,
    model_name                    text         NOT NULL,
    prompt_version                text         NOT NULL,
    schema_version                 text         NOT NULL,
    input_hash                      text         NOT NULL,
    result_json                     jsonb        NULL,
    raw_response_text                text         NULL,
    schema_validation_errors         jsonb        NULL,
    last_error                        jsonb        NULL,
    provider_request_id                text         NULL,
    input_tokens                        integer      NULL,
    output_tokens                        integer      NULL,
    actual_cost_usd                      numeric      NULL,
    lease_owner                          text         NULL,
    lease_token                          uuid         NULL,
    lease_expires_at                     timestamptz  NULL,
    attempt_count                        integer      NOT NULL DEFAULT 0,
    started_at                           timestamptz  NULL,
    claimed_at                           timestamptz  NULL,
    completed_at                         timestamptz  NULL,
    metadata                             jsonb        NOT NULL DEFAULT '{}'::jsonb,
    created_at                           timestamptz  NOT NULL DEFAULT now(),
    updated_at                           timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT audit_run_pass_results_unique_pass
        UNIQUE (audit_run_id, pass_code),

    CONSTRAINT audit_run_pass_results_pass_code_valid
        CHECK (pass_code IN ('A', 'B', 'C')),

    CONSTRAINT audit_run_pass_results_status_valid
        CHECK (
            status IN (
                'pending', 'running', 'completed',
                'schema_invalid', 'failed', 'skipped'
            )
        ),

    CONSTRAINT audit_run_pass_results_model_name_nonempty
        CHECK (TRIM(model_name) <> ''),

    CONSTRAINT audit_run_pass_results_prompt_version_nonempty
        CHECK (TRIM(prompt_version) <> ''),

    CONSTRAINT audit_run_pass_results_attempt_count_nonnegative
        CHECK (attempt_count >= 0),

    CONSTRAINT audit_run_pass_results_input_tokens_nonnegative
        CHECK (input_tokens IS NULL OR input_tokens >= 0),

    CONSTRAINT audit_run_pass_results_output_tokens_nonnegative
        CHECK (output_tokens IS NULL OR output_tokens >= 0),

    CONSTRAINT audit_run_pass_results_actual_cost_nonnegative
        CHECK (actual_cost_usd IS NULL OR actual_cost_usd >= 0),

    CONSTRAINT audit_run_pass_results_raw_response_bounded
        CHECK (
            raw_response_text IS NULL
            OR char_length(raw_response_text) <= 20000
        ),

    -- Lease-field internal consistency (mirrors background_jobs pattern).
    CONSTRAINT audit_run_pass_results_lease_token_requires_owner
        CHECK (lease_token IS NULL OR lease_owner IS NOT NULL),

    CONSTRAINT audit_run_pass_results_lease_expires_requires_owner
        CHECK (lease_expires_at IS NULL OR lease_owner IS NOT NULL),

    -- Per-status nullability / lease-field rules.
    CONSTRAINT audit_run_pass_results_pending_no_completed_at
        CHECK (status <> 'pending' OR completed_at IS NULL),

    CONSTRAINT audit_run_pass_results_pending_no_lease
        CHECK (
            status <> 'pending'
            OR (lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
        ),

    CONSTRAINT audit_run_pass_results_running_no_completed_at
        CHECK (status <> 'running' OR completed_at IS NULL),

    CONSTRAINT audit_run_pass_results_running_requires_lease
        CHECK (
            status <> 'running'
            OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        ),

    CONSTRAINT audit_run_pass_results_completed_requires_fields
        CHECK (
            status <> 'completed'
            OR (
                completed_at IS NOT NULL
                AND result_json IS NOT NULL
                AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
            )
        ),

    CONSTRAINT audit_run_pass_results_skipped_requires_fields
        CHECK (
            status <> 'skipped'
            OR (
                completed_at IS NOT NULL
                AND result_json IS NOT NULL
                AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
            )
        ),

    CONSTRAINT audit_run_pass_results_schema_invalid_requires_fields
        CHECK (
            status <> 'schema_invalid'
            OR (
                completed_at IS NOT NULL
                AND result_json IS NULL
                AND schema_validation_errors IS NOT NULL
                AND raw_response_text IS NOT NULL
                AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
            )
        ),

    CONSTRAINT audit_run_pass_results_failed_requires_fields
        CHECK (
            status <> 'failed'
            OR (
                completed_at IS NOT NULL
                AND result_json IS NULL
                AND last_error IS NOT NULL
                AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
            )
        )
);

-- NOTE: no separate (audit_run_id, pass_code) index is created here.
-- audit_run_pass_results_unique_pass already creates an implicit unique
-- btree index on exactly this column pair; a second non-unique index on the
-- same columns in the same order would be an exact, wasteful duplicate.

ALTER TABLE public.audit_run_pass_results ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.audit_run_pass_results IS
'Durable per-pass (A/B/C) execution state for ai_quality audit runs.
One row per (audit_run_id, pass_code); transitions happen in place under
row-level locking via claim_ai_quality_audit_pass_v1 / record_audit_pass_result_v1,
never via a second row. completed and skipped are permanently terminal and
may never be overwritten. schema_invalid and failed distinguish model-output
validation failure from provider/transport failure; both may retry once
(attempt_count < 2) before becoming terminal.
Service-role / admin access only. No anon or authenticated policies.';


-- =============================================================================
-- 5. audit_run_dispute_triggers
--    Explicit persisted gate for Pass C eligibility. At most one row per
--    run (audit_run_id is the primary key).
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.audit_run_dispute_triggers (
    audit_run_id     uuid         PRIMARY KEY
                        REFERENCES public.audit_runs(id) ON DELETE CASCADE,
    reason_code       text         NOT NULL,
    source_pass_code   text         NOT NULL,
    trigger_reason      text         NOT NULL,
    finding_refs          jsonb        NOT NULL,
    created_at             timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT audit_run_dispute_triggers_reason_valid
        CHECK (
            reason_code IN (
                'BLIND_ANSWER_MISMATCH',
                'BLOCKING_DEFECT_PROPOSED',
                'AMBIGUITY_PROPOSED',
                'PASS_A_SCHEMA_INVALID',
                'PASS_B_SCHEMA_INVALID',
                'EVIDENCE_STORED_ANSWER_CONFLICT'
            )
        ),

    CONSTRAINT audit_run_dispute_triggers_source_pass_valid
        CHECK (source_pass_code IN ('A', 'B')),

    CONSTRAINT audit_run_dispute_triggers_reason_nonempty
        CHECK (TRIM(trigger_reason) <> ''),

    CONSTRAINT audit_run_dispute_triggers_refs_is_array
        CHECK (jsonb_typeof(finding_refs) = 'array'),

    -- Reason / source-pass / finding_refs coupling.
    CONSTRAINT audit_run_dispute_triggers_reason_coupling
        CHECK (
            (
                reason_code = 'PASS_A_SCHEMA_INVALID'
                AND source_pass_code = 'A'
                AND finding_refs = '[]'::jsonb
            )
            OR (
                reason_code = 'PASS_B_SCHEMA_INVALID'
                AND source_pass_code = 'B'
                AND finding_refs = '[]'::jsonb
            )
            OR (
                reason_code IN (
                    'BLIND_ANSWER_MISMATCH', 'BLOCKING_DEFECT_PROPOSED',
                    'AMBIGUITY_PROPOSED', 'EVIDENCE_STORED_ANSWER_CONFLICT'
                )
                AND source_pass_code = 'B'
                AND jsonb_array_length(finding_refs) > 0
            )
        )
);

ALTER TABLE public.audit_run_dispute_triggers ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.audit_run_dispute_triggers IS
'Persisted, explicit gate for Pass C eligibility. At most one trigger per
audit run (audit_run_id is the primary key). PASS_A_SCHEMA_INVALID and
PASS_B_SCHEMA_INVALID reasons require an empty finding_refs array (Pass A/B
output could not be parsed, so no valid finding_ref exists); all other
reasons require a non-empty finding_refs subset of Pass B''s proposed
finding_ref values.
Service-role / admin access only. No anon or authenticated policies.';
