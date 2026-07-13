-- =============================================================================
-- PAB-EXP-03A — Platform App Builder Catalog: Actual-Schema Preflight
-- =============================================================================
--
-- Purpose
-- -------
-- public.certifications and public.certification_domains predate this
-- repository's migration history -- there is no CREATE TABLE migration for
-- either, so the schema this migration will run against has never been
-- directly verified in-repo. This script is a READ-ONLY compatibility check
-- to run against the real Supabase database (local or staging) BEFORE ever
-- applying:
--   supabase/migrations/20260713224500_v61_add_platform_app_builder_certification_catalog.sql
--
-- It queries only information_schema / pg_catalog. It never inserts,
-- updates, deletes, alters, creates any table/view/function/trigger/index,
-- and never touches RLS or policies. It is safe to run against production
-- for inspection purposes only -- though per PAB-EXP-03A scope, it should
-- still only be run against a disposable/local/staging environment until a
-- human explicitly authorizes a production check.
--
-- What it checks
-- --------------
-- 1. Both tables exist in the public schema.
-- 2. Every column the migration writes to exists on each table:
--      certifications:        exam_name, display_name, certification_code,
--                              passing_score, time_limit_minutes,
--                              question_count, is_active
--      certification_domains: exam_name, domain_name, weight,
--                              question_count, display_order, is_active
-- 3. Every OTHER NOT NULL column on either table (one the migration does
--    NOT supply a value for) has a column default, is an identity column,
--    or is a generated column -- otherwise the migration's INSERT would
--    fail on that column with a NOT NULL violation the migration does not
--    anticipate.
-- 4. Reports (RAISE NOTICE, not a hard failure) for both tables:
--      - primary key columns
--      - unique constraints
--      - foreign keys (both outgoing, e.g. certification_domains.exam_name
--        -> certifications.exam_name, and incoming)
--      - check constraints
--      - triggers
--      - every column's data type and default
--
-- Fails clearly
-- -------------
-- Any hard incompatibility (missing table, missing required column, or an
-- unsupplied NOT NULL column with no default/identity/generated source)
-- raises a distinct EXCEPTION naming exactly what is wrong, so the migration
-- must not be applied until reviewed.
--
-- Usage:
--   psql "$DATABASE_URL" -f supabase/tests/v61_platform_app_builder_catalog_schema_preflight.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- P1: both tables exist
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.certifications') IS NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P1): public.certifications does not exist. Migration cannot be applied.';
    END IF;
    IF to_regclass('public.certification_domains') IS NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P1): public.certification_domains does not exist. Migration cannot be applied.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P1): both public.certifications and public.certification_domains exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P2: required columns exist on public.certifications
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_required text[] := ARRAY[
        'exam_name', 'display_name', 'certification_code',
        'passing_score', 'time_limit_minutes', 'question_count', 'is_active'
    ];
    v_col text;
    v_missing text[] := ARRAY[]::text[];
BEGIN
    FOREACH v_col IN ARRAY v_required LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'certifications' AND column_name = v_col
        ) THEN
            v_missing := array_append(v_missing, v_col);
        END IF;
    END LOOP;

    IF array_length(v_missing, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P2): public.certifications is missing required column(s): %. Migration cannot be applied.',
            array_to_string(v_missing, ', ');
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P2): public.certifications has all 7 required columns.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P3: required columns exist on public.certification_domains
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_required text[] := ARRAY[
        'exam_name', 'domain_name', 'weight',
        'question_count', 'display_order', 'is_active'
    ];
    v_col text;
    v_missing text[] := ARRAY[]::text[];
BEGIN
    FOREACH v_col IN ARRAY v_required LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'certification_domains' AND column_name = v_col
        ) THEN
            v_missing := array_append(v_missing, v_col);
        END IF;
    END LOOP;

    IF array_length(v_missing, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P3): public.certification_domains is missing required column(s): %. Migration cannot be applied.',
            array_to_string(v_missing, ', ');
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P3): public.certification_domains has all 6 required columns.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P4: every other NOT NULL column on public.certifications (one the
--     migration does not supply) has a default, is identity, or is
--     generated.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_supplied text[] := ARRAY[
        'exam_name', 'display_name', 'certification_code',
        'passing_score', 'time_limit_minutes', 'question_count', 'is_active'
    ];
    v_bad_cols text[] := ARRAY[]::text[];
    v_rec record;
BEGIN
    FOR v_rec IN
        SELECT column_name, column_default, is_identity, is_generated
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'certifications'
          AND is_nullable = 'NO'
          AND column_name <> ALL (v_supplied)
    LOOP
        IF v_rec.column_default IS NULL
           AND coalesce(v_rec.is_identity, 'NO') <> 'YES'
           AND coalesce(v_rec.is_generated, 'NEVER') = 'NEVER' THEN
            v_bad_cols := array_append(v_bad_cols, v_rec.column_name);
        END IF;
    END LOOP;

    IF array_length(v_bad_cols, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P4): public.certifications has NOT NULL column(s) with no default/identity/generated source that the migration does not supply a value for: %. Migration''s INSERT would fail. Review and update the migration before applying.',
            array_to_string(v_bad_cols, ', ');
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P4): every other NOT NULL column on public.certifications has a default, is identity, or is generated.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P5: every other NOT NULL column on public.certification_domains (one the
--     migration does not supply) has a default, is identity, or is
--     generated.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_supplied text[] := ARRAY[
        'exam_name', 'domain_name', 'weight',
        'question_count', 'display_order', 'is_active'
    ];
    v_bad_cols text[] := ARRAY[]::text[];
    v_rec record;
BEGIN
    FOR v_rec IN
        SELECT column_name, column_default, is_identity, is_generated
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'certification_domains'
          AND is_nullable = 'NO'
          AND column_name <> ALL (v_supplied)
    LOOP
        IF v_rec.column_default IS NULL
           AND coalesce(v_rec.is_identity, 'NO') <> 'YES'
           AND coalesce(v_rec.is_generated, 'NEVER') = 'NEVER' THEN
            v_bad_cols := array_append(v_bad_cols, v_rec.column_name);
        END IF;
    END LOOP;

    IF array_length(v_bad_cols, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P5): public.certification_domains has NOT NULL column(s) with no default/identity/generated source that the migration does not supply a value for: %. Migration''s INSERT would fail. Review and update the migration before applying.',
            array_to_string(v_bad_cols, ', ');
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P5): every other NOT NULL column on public.certification_domains has a default, is identity, or is generated.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P6: report primary keys, unique constraints, foreign keys, and check
--     constraints on both tables (informational; not a hard-fail gate).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_rec record;
BEGIN
    RAISE NOTICE '--- P6: constraints on public.certifications / public.certification_domains ---';
    FOR v_rec IN
        SELECT tc.table_name, tc.constraint_type, tc.constraint_name,
               string_agg(kcu.column_name, ', ' ORDER BY kcu.ordinal_position) AS columns
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name
         AND kcu.table_schema = tc.table_schema
        WHERE tc.table_schema = 'public'
          AND tc.table_name IN ('certifications', 'certification_domains')
          AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
        GROUP BY tc.table_name, tc.constraint_type, tc.constraint_name
        ORDER BY tc.table_name, tc.constraint_type, tc.constraint_name
    LOOP
        RAISE NOTICE '  % % % (%)', v_rec.table_name, v_rec.constraint_type, v_rec.constraint_name, v_rec.columns;
    END LOOP;

    FOR v_rec IN
        SELECT
            tc.table_name AS from_table,
            kcu.column_name AS from_column,
            ccu.table_name AS to_table,
            ccu.column_name AS to_column,
            tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND (tc.table_name IN ('certifications', 'certification_domains')
               OR ccu.table_name IN ('certifications', 'certification_domains'))
        ORDER BY tc.table_name, tc.constraint_name
    LOOP
        RAISE NOTICE '  FK % : %.% -> %.%', v_rec.constraint_name, v_rec.from_table, v_rec.from_column, v_rec.to_table, v_rec.to_column;
    END LOOP;

    FOR v_rec IN
        SELECT tc.table_name, tc.constraint_name, cc.check_clause
        FROM information_schema.table_constraints tc
        JOIN information_schema.check_constraints cc
          ON cc.constraint_name = tc.constraint_name AND cc.constraint_schema = tc.table_schema
        WHERE tc.table_schema = 'public'
          AND tc.table_name IN ('certifications', 'certification_domains')
          AND tc.constraint_type = 'CHECK'
        ORDER BY tc.table_name, tc.constraint_name
    LOOP
        RAISE NOTICE '  CHECK % on %: %', v_rec.constraint_name, v_rec.table_name, v_rec.check_clause;
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- P7: report triggers on both tables (informational).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_rec record;
    v_found boolean := false;
BEGIN
    RAISE NOTICE '--- P7: triggers on public.certifications / public.certification_domains ---';
    FOR v_rec IN
        SELECT event_object_table AS table_name, trigger_name, action_timing, event_manipulation
        FROM information_schema.triggers
        WHERE event_object_schema = 'public'
          AND event_object_table IN ('certifications', 'certification_domains')
        ORDER BY event_object_table, trigger_name, event_manipulation
    LOOP
        v_found := true;
        RAISE NOTICE '  % % % % ON %', v_rec.trigger_name, v_rec.action_timing, v_rec.event_manipulation, 'trigger', v_rec.table_name;
    END LOOP;
    IF NOT v_found THEN
        RAISE NOTICE '  (no triggers found on either table)';
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- P8: report every column's data type and default on both tables
--     (informational).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_rec record;
BEGIN
    RAISE NOTICE '--- P8: column types and defaults ---';
    FOR v_rec IN
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ('certifications', 'certification_domains')
        ORDER BY table_name, ordinal_position
    LOOP
        RAISE NOTICE '  %.% % nullable=% default=%',
            v_rec.table_name, v_rec.column_name, v_rec.data_type, v_rec.is_nullable, coalesce(v_rec.column_default, '(none)');
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- P9: overall result
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    RAISE NOTICE 'PREFLIGHT COMPLETE: no hard incompatibility detected. Review the P6/P7/P8 NOTICE output above before applying the migration -- this script cannot detect every possible business-logic conflict (see the migration''s own Case 1/2/3 conflict-safety logic for that), only structural incompatibility.';
END;
$$;
