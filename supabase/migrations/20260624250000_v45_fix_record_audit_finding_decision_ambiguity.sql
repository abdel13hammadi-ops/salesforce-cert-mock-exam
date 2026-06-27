-- =============================================================================
-- V45 Phase 4D corrective: Fix record_audit_finding_decision_v1 PL/pgSQL ambiguity
-- Created : 2026-06-24 25:00:00 UTC
--
-- Purpose
-- -------
-- Corrects SQLSTATE 42702 in record_audit_finding_decision_v1 where unqualified
-- created_at in INSERT ... RETURNING collided with the RETURNS TABLE output
-- variable of the same name.
--
-- Safety guarantees
-- -----------------
--   * Function signature, transition rules, idempotency, locking, and audit
--     trail behavior unchanged.
--   * Only record_audit_finding_decision_v1 is replaced; no table changes.
--
-- Security
-- --------
--   Privilege hardening re-applied: service_role only.
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
    v_previous    text;
    v_decision    text;
    v_email       text;
    v_note        text;
    v_decision_id uuid;
    v_created_at  timestamptz;
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
    FROM   public.audit_findings AS af
    WHERE  af.id = p_finding_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_finding not found: %', p_finding_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_previous = v_decision THEN
        SELECT afd.id, afd.created_at
        INTO   v_decision_id, v_created_at
        FROM   public.audit_finding_decisions AS afd
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

    INSERT INTO public.audit_finding_decisions AS afd (
        finding_id, previous_status, new_status, reviewer_email, reviewer_note
    ) VALUES (
        p_finding_id, v_previous, v_decision, v_email, v_note
    )
    RETURNING afd.id, afd.created_at INTO v_decision_id, v_created_at;

    IF v_decision = 'resolved' THEN
        UPDATE public.audit_findings AS af
        SET    finding_status    = v_decision,
               resolved_at       = now(),
               resolved_by       = v_email,
               resolution_reason = v_note
        WHERE  af.id = p_finding_id;
    ELSE
        UPDATE public.audit_findings AS af
        SET    finding_status    = v_decision,
               resolved_at       = NULL,
               resolved_by       = NULL,
               resolution_reason = NULL
        WHERE  af.id = p_finding_id;
    END IF;

    RETURN QUERY
    SELECT p_finding_id, v_previous, v_decision, v_email, v_note,
           v_decision_id, v_created_at, false;
END;
$$;


REVOKE ALL ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) TO service_role;

COMMENT ON FUNCTION public.record_audit_finding_decision_v1(uuid, text, text, text) IS
'Records an append-only human review decision and updates audit_findings.finding_status.
Allowed decisions: accepted, rejected, resolved.  Idempotent when status unchanged.
Execute permission: service_role only.';
