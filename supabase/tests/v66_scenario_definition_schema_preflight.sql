-- =============================================================================
-- V66 Scenario Definition Persistence Foundation — PREFLIGHT (read-only)
--
-- Targets: supabase/migrations/20260718170000_v66_scenario_definition_
--          persistence_foundation.sql
--
-- Artifact identity: this preflight was originally authored under a V64
-- filename and renamed to V66 (SIM-PERSIST-02A) before ever being executed,
-- because V64 and V65 already belong to unrelated, already-applied
-- migrations. See the target migration's own header for the full rename
-- rationale.
--
-- This script is STRICTLY READ-ONLY. It must never create, alter, insert,
-- update, delete, grant, revoke, or execute the migration, and it must never
-- be run as part of, or immediately before, an automated apply step without
-- a human reviewing its NOTICE/EXCEPTION output first.
--
-- Run manually against a target database BEFORE applying the V66 migration:
--   psql "$DATABASE_URL" -f supabase/tests/v66_scenario_definition_schema_preflight.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- P1: gen_random_uuid() is callable.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    PERFORM gen_random_uuid();
    RAISE NOTICE 'PREFLIGHT PASS (P1): gen_random_uuid() is callable in the current search_path.';
EXCEPTION WHEN undefined_function THEN
    RAISE EXCEPTION 'PREFLIGHT FAIL (P1): gen_random_uuid() is not callable. pgcrypto (or an equivalent gen_random_uuid() provider) is likely not enabled or not on search_path. Migration cannot be applied.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P2: public.scenarios must not already exist.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.scenarios') IS NOT NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P2): public.scenarios already exists. Object-name conflict -- the V66 migration cannot be applied without review.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P2): public.scenarios does not exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P3: public.scenario_versions must not already exist.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.scenario_versions') IS NOT NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P3): public.scenario_versions already exists. Object-name conflict -- the V66 migration cannot be applied without review.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P3): public.scenario_versions does not exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P4: public.publish_scenario_version_v1 must not already exist (any overload).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count int;
BEGIN
    SELECT count(*) INTO v_count
    FROM   pg_proc p
    JOIN   pg_namespace n ON n.oid = p.pronamespace
    WHERE  n.nspname = 'public'
    AND    p.proname = 'publish_scenario_version_v1';

    IF v_count > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P4): public.publish_scenario_version_v1 already exists (% overload(s)). Object-name conflict -- the V66 migration cannot be applied without review.', v_count;
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P4): public.publish_scenario_version_v1 does not exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P5: neither trigger function must already exist.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count int;
BEGIN
    SELECT count(*) INTO v_count
    FROM   pg_proc p
    JOIN   pg_namespace n ON n.oid = p.pronamespace
    WHERE  n.nspname = 'public'
    AND    p.proname IN (
        'guard_scenario_version_immutability_v1',
        'guard_scenario_current_published_version_v1'
    );

    IF v_count > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P5): one or more of public.guard_scenario_version_immutability_v1 / public.guard_scenario_current_published_version_v1 already exists. Object-name conflict -- the V66 migration cannot be applied without review.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P5): neither guard_scenario_version_immutability_v1 nor guard_scenario_current_published_version_v1 exists.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P6: neither trigger must already exist on an unrelated object of the same name.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count int;
BEGIN
    SELECT count(*) INTO v_count
    FROM   pg_trigger t
    JOIN   pg_class c ON c.oid = t.tgrelid
    JOIN   pg_namespace n ON n.oid = c.relnamespace
    WHERE  n.nspname = 'public'
    AND    t.tgname IN (
        'trg_guard_scenario_version_immutability',
        'trg_guard_scenario_current_published_version'
    )
    AND    NOT t.tgisinternal;

    IF v_count > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P6): one or more of trg_guard_scenario_version_immutability / trg_guard_scenario_current_published_version already exists. Object-name conflict -- the V66 migration cannot be applied without review.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P6): neither trg_guard_scenario_version_immutability nor trg_guard_scenario_current_published_version exists.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P7: none of the intended index names already exist.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count int;
BEGIN
    SELECT count(*) INTO v_count
    FROM   pg_indexes
    WHERE  schemaname = 'public'
    AND    indexname IN (
        'idx_scenarios_certification_exam_name',
        'idx_scenarios_active',
        'idx_scenarios_current_published_version_id',
        'idx_scenario_versions_scenario_id',
        'idx_scenario_versions_lifecycle_status',
        'idx_scenario_versions_canonical_content_sha256'
    );

    IF v_count > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P7): one or more intended V66 index names already exist. Object-name conflict -- the V66 migration cannot be applied without review.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P7): none of the intended V66 index names exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P8 (informational, non-blocking): report the live shape of
-- public.certifications / certifications.exam_name. The V66 migration does
-- not add a foreign key to this table, so this is context only, not a
-- pass/fail gate.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_table_exists   boolean;
    v_column_exists  boolean;
    v_data_type      text;
    v_is_nullable    text;
    v_unique_count   int;
BEGIN
    v_table_exists := to_regclass('public.certifications') IS NOT NULL;

    IF NOT v_table_exists THEN
        RAISE NOTICE 'PREFLIGHT INFO (P8): public.certifications does not exist in this database. exam_name-based reporting skipped.';
        RETURN;
    END IF;

    SELECT true, c.data_type, c.is_nullable
    INTO   v_column_exists, v_data_type, v_is_nullable
    FROM   information_schema.columns c
    WHERE  c.table_schema = 'public'
    AND    c.table_name = 'certifications'
    AND    c.column_name = 'exam_name';

    IF NOT FOUND THEN
        RAISE NOTICE 'PREFLIGHT INFO (P8): public.certifications exists but has no exam_name column.';
        RETURN;
    END IF;

    SELECT count(*) INTO v_unique_count
    FROM   information_schema.table_constraints tc
    JOIN   information_schema.constraint_column_usage ccu
           ON ccu.constraint_name = tc.constraint_name
          AND ccu.table_schema = tc.table_schema
    WHERE  tc.table_schema = 'public'
    AND    tc.table_name = 'certifications'
    AND    ccu.column_name = 'exam_name'
    AND    tc.constraint_type IN ('UNIQUE', 'PRIMARY KEY');

    RAISE NOTICE 'PREFLIGHT INFO (P8): public.certifications.exam_name data_type=%, is_nullable=%, participates_in_unique_or_pk_constraint=% (count=%). scenarios.certification_exam_name in the V66 migration is a plain unconstrained text column with NO foreign key to this table.',
        v_data_type, v_is_nullable, (v_unique_count > 0), v_unique_count;
END;
$$;

-- ---------------------------------------------------------------------------
-- P9 (informational, non-blocking): current RLS state of the two intended
-- table names, in case they exist under a different owner/state than
-- expected by P2/P3 (defense in depth reporting only; P2/P3 already fail
-- the preflight if the tables exist at all).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_row record;
BEGIN
    FOR v_row IN
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
        FROM   pg_class c
        JOIN   pg_namespace n ON n.oid = c.relnamespace
        WHERE  n.nspname = 'public'
        AND    c.relname IN ('scenarios', 'scenario_versions')
    LOOP
        RAISE NOTICE 'PREFLIGHT INFO (P9): relation % exists with relrowsecurity=%, relforcerowsecurity=%.',
            v_row.relname, v_row.relrowsecurity, v_row.relforcerowsecurity;
    END LOOP;

    RAISE NOTICE 'PREFLIGHT INFO (P9): RLS state reported for any pre-existing scenarios/scenario_versions relations above (none expected -- see P2/P3).';
END;
$$;

-- ---------------------------------------------------------------------------
-- P10 (informational, operator context only, non-blocking): this repository
-- already has migrations using the V64 and V65 feature-number labels for
-- unrelated work. They are reported here only so an operator scanning V66
-- preflight output understands why V66 (not V64) was chosen for the
-- Scenario Simulator definition foundation -- this check never blocks and
-- never inspects those migrations' content.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_row record;
BEGIN
    FOR v_row IN
        SELECT version
        FROM   supabase_migrations.schema_migrations
        WHERE  version IN ('20260718170000')
        ORDER  BY version
    LOOP
        RAISE NOTICE 'PREFLIGHT INFO (P10): migration version % is already recorded as applied in this database.', v_row.version;
    END LOOP;
EXCEPTION WHEN undefined_table THEN
    RAISE NOTICE 'PREFLIGHT INFO (P10): supabase_migrations.schema_migrations is not present in this database (not a Supabase-managed project, or migration history tracking is unavailable). Skipping applied-migration lookup.';
END;
$$;

DO $$
BEGIN
    RAISE NOTICE 'PREFLIGHT INFO (P10): V64 and V65 feature-number labels are already used by unrelated, previously-applied certification-catalog migrations in this repository (see supabase/migrations/20260714110000_v64_add_sales_cloud_consultant_certification_catalog.sql and supabase/migrations/20260714120000_v65_add_service_cloud_consultant_certification_catalog.sql). The Scenario Simulator definition foundation therefore uses V66 as its own distinct feature number, while keeping the original 20260718170000 migration timestamp.';
    RAISE NOTICE 'PREFLIGHT SUMMARY: all blocking V66 checks passed. Review PREFLIGHT INFO output above, then apply supabase/migrations/20260718170000_v66_scenario_definition_persistence_foundation.sql manually.';
END;
$$;
