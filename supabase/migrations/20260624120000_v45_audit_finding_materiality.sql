-- =============================================================================
-- V45 Phase 3: Audit finding materiality verification
-- READ-ONLY: this file must not create, alter, update, insert, or delete anything.
-- =============================================================================

DO $$
DECLARE
v_is_nullable   text;
v_column_default text;
BEGIN
SELECT
c.is_nullable,
c.column_default
INTO
v_is_nullable,
v_column_default
FROM information_schema.columns c
WHERE c.table_schema = 'public'
AND c.table_name = 'audit_findings'
AND c.column_name = 'materiality';

ASSERT FOUND,
    'S1 failed: public.audit_findings.materiality does not exist';

ASSERT v_is_nullable = 'NO',
    'S1 failed: materiality must be NOT NULL';

ASSERT v_column_default IS NOT NULL
       AND v_column_default ILIKE '%warning%',
    'S1 failed: materiality default must be warning';
END;
$$;

DO $$
BEGIN
ASSERT EXISTS (
SELECT 1
FROM pg_constraint pc
WHERE pc.conrelid = 'public.audit_findings'::regclass
AND pc.conname = 'audit_findings_materiality_valid'
AND pc.contype = 'c'
),
'S2 failed: audit_findings_materiality_valid CHECK constraint is missing';
END;
$$;

DO $$
BEGIN
ASSERT EXISTS (
SELECT 1
FROM pg_indexes pi
WHERE pi.schemaname = 'public'
AND pi.tablename = 'audit_findings'
AND pi.indexname = 'idx_af_run_materiality_status'
),
'S3 failed: idx_af_run_materiality_status index is missing';
END;
$$;

DO $$
DECLARE
v_function_definition text;
BEGIN
SELECT pg_get_functiondef(
'public.complete_audit_run_v1(uuid,jsonb,jsonb)'::regprocedure
)
INTO v_function_definition;

ASSERT v_function_definition ILIKE '%v_materiality%',
    'S4 failed: complete_audit_run_v1 does not handle v_materiality';

ASSERT v_function_definition ILIKE '%invalid materiality%',
    'S4 failed: complete_audit_run_v1 does not reject invalid materiality';

ASSERT v_function_definition ILIKE '%warning%',
    'S4 failed: complete_audit_run_v1 does not default materiality to warning';

ASSERT v_function_definition ILIKE '%materiality%',
    'S4 failed: complete_audit_run_v1 does not persist materiality';
END;
$$;

DO $$
BEGIN
ASSERT NOT EXISTS (
SELECT 1
FROM public.audit_findings af
WHERE af.materiality IS NULL
OR af.materiality NOT IN (
'blocking',
'warning',
'informational'
)
),
'S5 failed: audit_findings contains invalid or NULL materiality values';
END;
$$;

DO $$
BEGIN
ASSERT NOT EXISTS (
SELECT 1
FROM public.audit_findings af
WHERE af.finding_code IN (
'WRONG_ANSWER_KEY',
'UNSUPPORTED_ANSWER',
'MULTIPLE_DEFENSIBLE_ANSWERS',
'EXPLANATION_MISSING',
'MISSING_EXPLANATION',
'OUTDATED_CONTENT',
'EMPTY_QUESTION_TEXT',
'INVALID_SELECT_COUNT',
'TOO_FEW_OPTIONS',
'EMPTY_OPTION_TEXT',
'DUPLICATE_OPTION_LABELS',
'DUPLICATE_OPTION_TEXT',
'CORRECT_COUNT_MISMATCH',
'SINGLE_SELECT_COUNT_MISMATCH',
'DUPLICATE_CORRECT_OPTIONS',
'OPTION_DISPLAY_ORDER_ISSUES'
)
AND af.materiality <> 'blocking'
),
'S6 failed: one or more explicit blocking codes were not backfilled as blocking';
END;
$$;

DO $$
DECLARE
v_public_has_execute boolean;
BEGIN
ASSERT has_function_privilege(
'service_role',
'public.complete_audit_run_v1(uuid,jsonb,jsonb)',
'EXECUTE'
),
'S7 failed: service_role does not have EXECUTE permission';

ASSERT NOT has_function_privilege(
    'anon',
    'public.complete_audit_run_v1(uuid,jsonb,jsonb)',
    'EXECUTE'
),
'S7 failed: anon still has EXECUTE permission';

ASSERT NOT has_function_privilege(
    'authenticated',
    'public.complete_audit_run_v1(uuid,jsonb,jsonb)',
    'EXECUTE'
),
'S7 failed: authenticated still has EXECUTE permission';

SELECT EXISTS (
    SELECT 1
    FROM pg_proc p
    CROSS JOIN LATERAL aclexplode(
        COALESCE(p.proacl, acldefault('f', p.proowner))
    ) acl
    WHERE p.oid =
        'public.complete_audit_run_v1(uuid,jsonb,jsonb)'::regprocedure
      AND acl.grantee = 0
      AND acl.privilege_type = 'EXECUTE'
)
INTO v_public_has_execute;

ASSERT NOT v_public_has_execute,
    'S7 failed: PUBLIC still has EXECUTE permission';

END;
$$;

SELECT
1 AS sql_query_number,
'V45_AUDIT_FINDING_MATERIALITY_VERIFIED' AS result_label,
true AS column_verified,
true AS constraint_verified,
true AS index_verified,
true AS rpc_verified,
true AS stored_values_verified,
true AS backfill_verified,
true AS privileges_verified;
