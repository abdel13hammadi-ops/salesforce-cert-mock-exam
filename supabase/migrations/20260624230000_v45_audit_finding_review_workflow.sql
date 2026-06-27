-- =============================================================================
-- V45 Phase 4D: Audit finding human review workflow
-- Created : 2026-06-24 23:00:00 UTC
--
-- Purpose
-- -------
-- Adds append-only human decision history and service-role RPCs for the admin
-- audit review screen.  Updates audit_findings.finding_status atomically.
--
-- Safety guarantees
-- -----------------
--   * Append-only decision log; findings are never deleted.
--   * Question content is not modified.
--   * Idempotent when the requested status already matches.
--   * Invalid transitions rejected in RPC.
--
-- Security
-- --------
--   RLS enabled on audit_finding_decisions.
--   EXECUTE on RPCs: service_role only (application-layer admin gate).
-- =============================================================================


CREATE TABLE IF NOT EXISTS public.audit_finding_decisions (
    id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id      uuid         NOT NULL
                        REFERENCES public.audit_findings(id) ON DELETE CASCADE,
    previous_status   text         NOT NULL,
    new_status        text         NOT NULL,
    reviewer_email    text         NOT NULL,
    reviewer_note     text         NOT NULL,
    created_at        timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT audit_finding_decisions_previous_status_valid
        CHECK (
            previous_status IN (
                'open', 'accepted', 'rejected', 'resolved', 'overridden'
            )
        ),

    CONSTRAINT audit_finding_decisions_new_status_valid
        CHECK (new_status IN ('accepted', 'rejected', 'resolved')),

    CONSTRAINT audit_finding_decisions_reviewer_nonempty
        CHECK (TRIM(reviewer_email) <> ''),

    CONSTRAINT audit_finding_decisions_note_nonempty
        CHECK (TRIM(reviewer_note) <> '')
);

CREATE INDEX IF NOT EXISTS idx_afd_finding_id
    ON public.audit_finding_decisions (finding_id, created_at DESC);

ALTER TABLE public.audit_finding_decisions ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.audit_finding_decisions IS
'Append-only human review decisions for audit findings.
Each row records previous_status, new_status, reviewer identity, and note.
Service-role / admin application access only for this phase.';


-- =============================================================================
-- 1. list_audit_runs_for_review_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.list_audit_runs_for_review_v1(
    p_limit               integer DEFAULT 50,
    p_run_status          text    DEFAULT NULL,
    p_audit_type          text    DEFAULT NULL,
    p_certification_code  text    DEFAULT NULL,
    p_blocking_only       boolean DEFAULT false
)
RETURNS TABLE (
    audit_run_id                uuid,
    audit_type                  text,
    run_status                  text,
    certification_code          text,
    target_question_version_id  uuid,
    question_id                 integer,
    version_number              integer,
    finding_count               integer,
    blocking_finding_count      integer,
    high_severity_count         integer,
    started_at                  timestamptz,
    completed_at                timestamptz,
    created_at                  timestamptz
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
BEGIN
    IF COALESCE(p_limit, 0) < 1 OR COALESCE(p_limit, 0) > 200 THEN
        RAISE EXCEPTION 'p_limit must be between 1 and 200, got: %', p_limit
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_run_status IS NOT NULL
       AND p_run_status NOT IN (
           'pending', 'running', 'completed', 'failed', 'cancelled'
       ) THEN
        RAISE EXCEPTION 'invalid p_run_status: %', p_run_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_audit_type IS NOT NULL
       AND p_audit_type NOT IN ('deterministic', 'llm', 'hybrid', 'human') THEN
        RAISE EXCEPTION 'invalid p_audit_type: %', p_audit_type
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    RETURN QUERY
    SELECT
        ar.id,
        ar.audit_type,
        ar.run_status,
        COALESCE(
            NULLIF(TRIM(ar.metadata ->> 'certification_exam_name'), ''),
            NULLIF(TRIM(ar.metadata ->> 'certification_id'), ''),
            q.exam_name
        ) AS certification_code,
        ar.target_question_version_id,
        qv.question_id,
        qv.version_number,
        COALESCE(stats.finding_count, 0)::integer,
        COALESCE(stats.blocking_finding_count, 0)::integer,
        COALESCE(stats.high_severity_count, 0)::integer,
        ar.started_at,
        ar.completed_at,
        ar.created_at
    FROM public.audit_runs ar
    LEFT JOIN public.question_versions qv
           ON qv.id = ar.target_question_version_id
    LEFT JOIN public.questions q
           ON q.id = qv.question_id
    LEFT JOIN LATERAL (
        SELECT
            COUNT(*)::integer AS finding_count,
            COUNT(*) FILTER (
                WHERE af.materiality = 'blocking'
            )::integer AS blocking_finding_count,
            COUNT(*) FILTER (
                WHERE af.severity IN ('high', 'critical')
            )::integer AS high_severity_count
        FROM public.audit_findings af
        WHERE af.audit_run_id = ar.id
    ) stats ON true
    WHERE (p_run_status IS NULL OR ar.run_status = p_run_status)
      AND (p_audit_type IS NULL OR ar.audit_type = p_audit_type)
      AND (
          p_certification_code IS NULL
          OR COALESCE(
              NULLIF(TRIM(ar.metadata ->> 'certification_exam_name'), ''),
              NULLIF(TRIM(ar.metadata ->> 'certification_id'), ''),
              q.exam_name
          ) = p_certification_code
      )
      AND (
          NOT COALESCE(p_blocking_only, false)
          OR COALESCE(stats.blocking_finding_count, 0) > 0
      )
    ORDER BY ar.created_at DESC
    LIMIT p_limit;
END;
$$;


-- =============================================================================
-- 2. list_audit_findings_for_review_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.list_audit_findings_for_review_v1(
    p_audit_run_id uuid
)
RETURNS TABLE (
    finding_id              uuid,
    finding_code            text,
    finding_type            text,
    severity                text,
    materiality             text,
    finding_status          text,
    title                   text,
    field_path              text,
    confidence              numeric,
    question_id             integer,
    question_version_id     uuid,
    question_version_number integer,
    audit_source            text,
    created_at              timestamptz
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
BEGIN
    IF p_audit_run_id IS NULL THEN
        RAISE EXCEPTION 'p_audit_run_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM 1 FROM public.audit_runs ar WHERE ar.id = p_audit_run_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_run not found: %', p_audit_run_id
            USING ERRCODE = 'no_data_found';
    END IF;

    RETURN QUERY
    SELECT
        af.id,
        af.finding_code,
        af.finding_type,
        af.severity,
        af.materiality,
        af.finding_status,
        af.title,
        af.field_path,
        af.confidence,
        COALESCE(
            (af.metadata -> 'evidence_contract' ->> 'question_id')::integer,
            qv.question_id,
            (af.metadata ->> 'question_id')::integer
        ) AS question_id,
        COALESCE(
            NULLIF(af.metadata -> 'evidence_contract' ->> 'question_version_id', '')::uuid,
            ar.target_question_version_id,
            NULLIF(af.metadata ->> 'question_version_id_a', '')::uuid
        ) AS question_version_id,
        COALESCE(
            (af.metadata -> 'evidence_contract' ->> 'question_version_number')::integer,
            qv.version_number
        ) AS question_version_number,
        COALESCE(
            NULLIF(TRIM(af.metadata -> 'evidence_contract' ->> 'audit_source'), ''),
            CASE ar.audit_type
                WHEN 'hybrid' THEN 'hybrid'
                ELSE ar.audit_type
            END
        ) AS audit_source,
        af.created_at
    FROM public.audit_findings af
    JOIN public.audit_runs ar
      ON ar.id = af.audit_run_id
    LEFT JOIN public.question_versions qv
      ON qv.id = ar.target_question_version_id
    WHERE af.audit_run_id = p_audit_run_id
    ORDER BY
        CASE af.materiality
            WHEN 'blocking' THEN 0
            WHEN 'warning' THEN 1
            ELSE 2
        END,
        CASE af.severity
            WHEN 'critical' THEN 0
            WHEN 'high' THEN 1
            WHEN 'medium' THEN 2
            WHEN 'low' THEN 3
            ELSE 4
        END,
        af.created_at ASC;
END;
$$;


-- =============================================================================
-- 3. get_audit_finding_review_detail_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_audit_finding_review_detail_v1(
    p_finding_id uuid
)
RETURNS TABLE (
    finding_id              uuid,
    audit_run_id            uuid,
    finding_code            text,
    finding_type            text,
    severity                text,
    materiality             text,
    finding_status          text,
    title                   text,
    description             text,
    field_path              text,
    confidence              numeric,
    detector_name           text,
    detector_version        text,
    metadata                jsonb,
    resolved_at             timestamptz,
    resolved_by             text,
    resolution_reason       text,
    target_question_version_id uuid,
    question_id             integer,
    question_version_number integer,
    question_text           text,
    explanation             text,
    question_type           text,
    select_count            integer,
    options                 jsonb,
    evidence                jsonb,
    decision_history        jsonb
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_version_id uuid;
BEGIN
    IF p_finding_id IS NULL THEN
        RAISE EXCEPTION 'p_finding_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT
        COALESCE(
            NULLIF(af.metadata -> 'evidence_contract' ->> 'question_version_id', '')::uuid,
            ar.target_question_version_id,
            NULLIF(af.metadata ->> 'question_version_id_a', '')::uuid
        )
    INTO v_version_id
    FROM public.audit_findings af
    JOIN public.audit_runs ar ON ar.id = af.audit_run_id
    WHERE af.id = p_finding_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_finding not found: %', p_finding_id
            USING ERRCODE = 'no_data_found';
    END IF;

    RETURN QUERY
    SELECT
        af.id,
        af.audit_run_id,
        af.finding_code,
        af.finding_type,
        af.severity,
        af.materiality,
        af.finding_status,
        af.title,
        af.description,
        af.field_path,
        af.confidence,
        af.detector_name,
        af.detector_version,
        af.metadata,
        af.resolved_at,
        af.resolved_by,
        af.resolution_reason,
        v_version_id,
        qv.question_id,
        qv.version_number,
        qv.question_text,
        qv.explanation,
        qv.question_type,
        qv.select_count,
        COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'option_label', qov.option_label,
                        'option_text', qov.option_text,
                        'is_correct', qov.is_correct,
                        'display_order', qov.display_order
                    )
                    ORDER BY qov.display_order, qov.option_label
                )
                FROM public.question_option_versions qov
                WHERE qov.question_version_id = v_version_id
            ),
            '[]'::jsonb
        ) AS options,
        COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'resource_chunk_id', afe.resource_chunk_id,
                        'evidence_role', afe.evidence_role,
                        'quote_text', afe.quote_text,
                        'relevance_score', afe.relevance_score,
                        'metadata', afe.metadata
                    )
                    ORDER BY afe.created_at
                )
                FROM public.audit_finding_evidence afe
                WHERE afe.finding_id = af.id
            ),
            '[]'::jsonb
        ) AS evidence,
        COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'decision_id', afd.id,
                        'previous_status', afd.previous_status,
                        'new_status', afd.new_status,
                        'reviewer_email', afd.reviewer_email,
                        'reviewer_note', afd.reviewer_note,
                        'created_at', afd.created_at
                    )
                    ORDER BY afd.created_at DESC
                )
                FROM public.audit_finding_decisions afd
                WHERE afd.finding_id = af.id
            ),
            '[]'::jsonb
        ) AS decision_history
    FROM public.audit_findings af
    JOIN public.audit_runs ar ON ar.id = af.audit_run_id
    LEFT JOIN public.question_versions qv ON qv.id = v_version_id
    WHERE af.id = p_finding_id;
END;
$$;


-- =============================================================================
-- 4. record_audit_finding_decision_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.record_audit_finding_decision_v1(
    p_finding_id     uuid,
    p_decision       text,
    p_reviewer_email text,
    p_reviewer_note  text
)
RETURNS TABLE (
    finding_id       uuid,
    previous_status  text,
    new_status       text,
    reviewer_email   text,
    reviewer_note    text,
    decision_id      uuid,
    created_at       timestamptz,
    idempotent       boolean
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_previous   text;
    v_decision   text;
    v_email      text;
    v_note       text;
    v_decision_id uuid;
    v_created_at timestamptz;
BEGIN
    v_decision := lower(trim(COALESCE(p_decision, '')));
    v_email    := lower(trim(COALESCE(p_reviewer_email, '')));
    v_note     := trim(COALESCE(p_reviewer_note, ''));

    IF p_finding_id IS NULL THEN
        RAISE EXCEPTION 'p_finding_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_decision NOT IN ('accepted', 'rejected', 'resolved') THEN
        RAISE EXCEPTION 'invalid decision: %', p_decision
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_email = '' THEN
        RAISE EXCEPTION 'p_reviewer_email must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_note = '' THEN
        RAISE EXCEPTION 'p_reviewer_note must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT af.finding_status
    INTO   v_previous
    FROM   public.audit_findings af
    WHERE  af.id = p_finding_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_finding not found: %', p_finding_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_previous = v_decision THEN
        SELECT afd.id, afd.created_at
        INTO   v_decision_id, v_created_at
        FROM   public.audit_finding_decisions afd
        WHERE  afd.finding_id = p_finding_id
          AND  afd.new_status = v_decision
        ORDER BY afd.created_at DESC
        LIMIT 1;

        RETURN QUERY
        SELECT p_finding_id, v_previous, v_decision, v_email, v_note,
               v_decision_id, v_created_at, true;
        RETURN;
    END IF;

    IF v_previous IN ('resolved', 'overridden') THEN
        RAISE EXCEPTION
            'finding % cannot transition from % to %',
            p_finding_id, v_previous, v_decision
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_previous = 'open'
       AND v_decision NOT IN ('accepted', 'rejected', 'resolved') THEN
        RAISE EXCEPTION 'invalid transition from open to %', v_decision
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_previous = 'accepted'
       AND v_decision NOT IN ('rejected', 'resolved') THEN
        RAISE EXCEPTION 'invalid transition from accepted to %', v_decision
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_previous = 'rejected'
       AND v_decision NOT IN ('accepted', 'resolved') THEN
        RAISE EXCEPTION 'invalid transition from rejected to %', v_decision
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    INSERT INTO public.audit_finding_decisions (
        finding_id, previous_status, new_status, reviewer_email, reviewer_note
    ) VALUES (
        p_finding_id, v_previous, v_decision, v_email, v_note
    )
    RETURNING id, created_at INTO v_decision_id, v_created_at;

    IF v_decision = 'resolved' THEN
        UPDATE public.audit_findings
        SET    finding_status    = v_decision,
               resolved_at       = now(),
               resolved_by       = v_email,
               resolution_reason = v_note
        WHERE  id = p_finding_id;
    ELSE
        UPDATE public.audit_findings
        SET    finding_status    = v_decision,
               resolved_at       = NULL,
               resolved_by       = NULL,
               resolution_reason = NULL
        WHERE  id = p_finding_id;
    END IF;

    RETURN QUERY
    SELECT p_finding_id, v_previous, v_decision, v_email, v_note,
           v_decision_id, v_created_at, false;
END;
$$;


-- =============================================================================
-- Privilege hardening
-- =============================================================================

REVOKE ALL ON TABLE public.audit_finding_decisions FROM PUBLIC;
REVOKE ALL ON TABLE public.audit_finding_decisions FROM anon;
REVOKE ALL ON TABLE public.audit_finding_decisions FROM authenticated;
GRANT SELECT, INSERT ON TABLE public.audit_finding_decisions TO service_role;

REVOKE ALL ON FUNCTION public.list_audit_runs_for_review_v1(integer, text, text, text, boolean) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.list_audit_runs_for_review_v1(integer, text, text, text, boolean) FROM anon;
REVOKE EXECUTE ON FUNCTION public.list_audit_runs_for_review_v1(integer, text, text, text, boolean) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.list_audit_runs_for_review_v1(integer, text, text, text, boolean) TO service_role;

REVOKE ALL ON FUNCTION public.list_audit_findings_for_review_v1(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.list_audit_findings_for_review_v1(uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.list_audit_findings_for_review_v1(uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.list_audit_findings_for_review_v1(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.get_audit_finding_review_detail_v1(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_audit_finding_review_detail_v1(uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.get_audit_finding_review_detail_v1(uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_audit_finding_review_detail_v1(uuid) TO service_role;

REVOKE ALL ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) TO service_role;

COMMENT ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) IS
'Records an append-only human review decision and updates audit_findings.finding_status.
Allowed decisions: accepted, rejected, resolved.  Idempotent when status unchanged.
Execute permission: service_role only.';
