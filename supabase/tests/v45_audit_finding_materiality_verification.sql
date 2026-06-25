-- =============================================================================
-- V45 Phase 3 — Audit Finding Materiality Verification
-- =============================================================================
--
-- Run as service_role after applying:
--   20260624120000_v45_audit_finding_materiality.sql
--
-- Wraps assertions in BEGIN…ROLLBACK when testing inserts; schema checks are
-- permanent catalog queries.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- S1: materiality column exists with NOT NULL default warning
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_col record;
BEGIN
    SELECT column_name, is_nullable, column_default
    INTO   v_col
    FROM   information_schema.columns
    WHERE  table_schema = 'public'
      AND  table_name   = 'audit_findings'
      AND  column_name  = 'materiality';

    ASSERT v_col.column_name = 'materiality',
        'S1: audit_findings.materiality column must exist';
    ASSERT v_col.is_nullable = 'NO',
        'S1: audit_findings.materiality must be NOT NULL';
    ASSERT v_col.column_default LIKE '%warning%',
        'S1: audit_findings.materiality default must be warning';
END;
$$;

-- ---------------------------------------------------------------------------
-- S2: CHECK constraint audit_findings_materiality_valid exists
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    ASSERT EXISTS (
        SELECT 1
        FROM   pg_constraint
        WHERE  conname = 'audit_findings_materiality_valid'
          AND  conrelid = 'public.audit_findings'::regclass
    ), 'S2: audit_findings_materiality_valid constraint must exist';
END;
$$;

-- ---------------------------------------------------------------------------
-- S3: composite index idx_af_run_materiality_status exists
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    ASSERT EXISTS (
        SELECT 1
        FROM   pg_indexes
        WHERE  schemaname = 'public'
          AND  tablename  = 'audit_findings'
          AND  indexname  = 'idx_af_run_materiality_status'
    ), 'S3: idx_af_run_materiality_status index must exist';
END;
$$;

-- ---------------------------------------------------------------------------
-- S4: complete_audit_run_v1 source validates and persists materiality
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_src text;
BEGIN
    SELECT pg_get_functiondef(p.oid)
    INTO   v_src
    FROM   pg_proc p
    JOIN   pg_namespace n ON n.oid = p.pronamespace
    WHERE  n.nspname = 'public'
      AND  p.proname = 'complete_audit_run_v1';

    ASSERT v_src IS NOT NULL,
        'S4: complete_audit_run_v1 must exist';
    ASSERT v_src LIKE '%invalid materiality%',
        'S4: complete_audit_run_v1 must reject invalid materiality';
    ASSERT v_src LIKE '%v_materiality%',
        'S4: complete_audit_run_v1 must use v_materiality variable';
    ASSERT v_src LIKE '%''warning''%',
        'S4: complete_audit_run_v1 must default missing materiality to warning';
END;
$$;

-- ---------------------------------------------------------------------------
-- S5: invalid materiality rejected by CHECK constraint (no RPC required)
-- ---------------------------------------------------------------------------
BEGIN;

DO $$
DECLARE
    v_run_id uuid;
BEGIN
    SELECT ar.id
    INTO   v_run_id
    FROM   public.audit_runs ar
    LIMIT  1;

    IF v_run_id IS NULL THEN
        RAISE NOTICE 'S5: skipped — no audit_runs row available for CHECK test';
        RETURN;
    END IF;

    BEGIN
        INSERT INTO public.audit_findings (
            audit_run_id, finding_code, finding_type, severity,
            materiality, title, description
        ) VALUES (
            v_run_id, 'TEST_CODE', 'other', 'low',
            'critical', 'test', 'invalid materiality level'
        );
        ASSERT FALSE, 'S5: invalid materiality must be rejected by CHECK';
    EXCEPTION
        WHEN check_violation THEN
            NULL; -- expected
    END;
END;
$$;

ROLLBACK;

-- ---------------------------------------------------------------------------
-- S6: backfill — blocking codes are blocking; unknown codes remain warning
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_bad integer;
BEGIN
    SELECT COUNT(*) INTO v_bad
    FROM   public.audit_findings
    WHERE  finding_code IN (
        'EXPLANATION_MISSING', 'MISSING_EXPLANATION', 'EMPTY_QUESTION_TEXT',
        'CORRECT_COUNT_MISMATCH', 'WRONG_ANSWER_KEY'
    )
      AND  materiality <> 'blocking';

    ASSERT v_bad = 0,
        'S6: explicit blocking finding_code rows must have materiality=blocking';
END;
$$;

-- ---------------------------------------------------------------------------
-- S7: complete_audit_run_v1 privilege hardening
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    ASSERT has_function_privilege(
        'service_role',
        'public.complete_audit_run_v1(uuid,jsonb,jsonb)',
        'EXECUTE'
    ), 'S7: service_role must have EXECUTE on complete_audit_run_v1';

    ASSERT NOT has_function_privilege(
        'anon',
        'public.complete_audit_run_v1(uuid,jsonb,jsonb)',
        'EXECUTE'
    ), 'S7: anon must not have EXECUTE on complete_audit_run_v1';

    ASSERT NOT has_function_privilege(
        'authenticated',
        'public.complete_audit_run_v1(uuid,jsonb,jsonb)',
        'EXECUTE'
    ), 'S7: authenticated must not have EXECUTE on complete_audit_run_v1';

    ASSERT NOT has_function_privilege(
        'public',
        'public.complete_audit_run_v1(uuid,jsonb,jsonb)',
        'EXECUTE'
    ), 'S7: PUBLIC must not have EXECUTE on complete_audit_run_v1';
END;
$$;

DO $$
BEGIN
    RAISE NOTICE '== V45 audit finding materiality verification passed ==';
END;
$$;
