-- =============================================================================
-- V68 Scenario Attempt Persistence Foundation — PREFLIGHT (read-only)
--
-- Targets: supabase/migrations/20260719130000_v68_scenario_attempt_
--          persistence_foundation.sql
--
-- This script is STRICTLY READ-ONLY. It must never create, alter, insert,
-- update, delete, grant, revoke, or execute the migration, and it must never
-- be run as part of, or immediately before, an automated apply step without
-- a human reviewing its NOTICE/EXCEPTION output first.
--
-- Run manually against a target database BEFORE applying the V68 migration:
--   psql "$DATABASE_URL" -f supabase/tests/v68_scenario_attempt_persistence_preflight.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- P1: gen_random_uuid() is callable (pgcrypto or an equivalent provider is
--     enabled and on search_path). Both new tables default id to it.
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
-- P2 (informational, non-blocking): report whether pgcrypto's digest()
--     function is available. The V68 migration does NOT use digest() or any
--     other hashing function anywhere -- every hash/fingerprint value
--     (canonical_content_sha256, scenario_content_sha256,
--     request_fingerprint) is computed in Python and only FORMAT-validated
--     in SQL (a 64-lowercase-hex regex), never recomputed or independently
--     derived here. This check exists purely so an operator does not need
--     to guess whether the migration secretly depends on it.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    PERFORM digest('preflight-check', 'sha256');
    RAISE NOTICE 'PREFLIGHT INFO (P2): pgcrypto digest() is available in this database, but the V68 migration does not call it -- every hash/fingerprint value is computed in Python and only format-validated (64 lowercase hex) in SQL.';
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'PREFLIGHT INFO (P2): pgcrypto digest() is NOT available in this database. This is NOT a blocker -- the V68 migration does not call digest() or any other hashing function anywhere; all hash/fingerprint values are computed in Python and only format-validated in SQL.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P3: required V66/V67 prerequisite objects already exist.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.scenarios') IS NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P3): public.scenarios does not exist. The V68 migration requires the V66 foundation (and V67 hardening) to already be installed.';
    END IF;

    IF to_regclass('public.scenario_versions') IS NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P3): public.scenario_versions does not exist. The V68 migration requires the V66 foundation (and V67 hardening) to already be installed.';
    END IF;

    IF to_regprocedure('public.publish_scenario_version_v1(uuid,jsonb,text)') IS NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P3): public.publish_scenario_version_v1(uuid,jsonb,text) does not exist.';
    END IF;

    IF to_regprocedure('public.guard_scenario_current_published_version_v1()') IS NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P3): public.guard_scenario_current_published_version_v1() does not exist.';
    END IF;

    IF to_regprocedure('public.guard_scenario_version_immutability_v1()') IS NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P3): public.guard_scenario_version_immutability_v1() does not exist.';
    END IF;

    RAISE NOTICE 'PREFLIGHT PASS (P3): all required V66/V67 prerequisite objects exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P4: public.scenario_attempts must not already exist.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.scenario_attempts') IS NOT NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P4): public.scenario_attempts already exists. Object-name conflict -- the V68 migration cannot be applied without review.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P4): public.scenario_attempts does not exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P5: public.scenario_decisions must not already exist.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.scenario_decisions') IS NOT NULL THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P5): public.scenario_decisions already exists. Object-name conflict -- the V68 migration cannot be applied without review.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P5): public.scenario_decisions does not exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P6: none of the four RPCs or two trigger functions already exist (by
--     name, any overload).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count int;
    v_row   record;
BEGIN
    SELECT count(*) INTO v_count
    FROM   pg_proc p
    JOIN   pg_namespace n ON n.oid = p.pronamespace
    WHERE  n.nspname = 'public'
    AND    p.proname IN (
        'start_or_resume_scenario_attempt_v1',
        'get_scenario_attempt_v1',
        'submit_scenario_decision_v1',
        'abandon_scenario_attempt_v1',
        'guard_scenario_attempt_mutation_v1',
        'guard_scenario_decision_immutability_v1'
    );

    IF v_count > 0 THEN
        FOR v_row IN
            SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
            FROM   pg_proc p
            JOIN   pg_namespace n ON n.oid = p.pronamespace
            WHERE  n.nspname = 'public'
            AND    p.proname IN (
                'start_or_resume_scenario_attempt_v1',
                'get_scenario_attempt_v1',
                'submit_scenario_decision_v1',
                'abandon_scenario_attempt_v1',
                'guard_scenario_attempt_mutation_v1',
                'guard_scenario_decision_immutability_v1'
            )
        LOOP
            RAISE WARNING 'PREFLIGHT P6 CONFLICT: function %(%) already exists.', v_row.proname, v_row.args;
        END LOOP;
        RAISE EXCEPTION 'PREFLIGHT FAIL (P6): % of the six functions this migration would create already exist (see WARNING(s) above). Object-name conflict -- the V68 migration cannot be applied without review.', v_count;
    END IF;

    RAISE NOTICE 'PREFLIGHT PASS (P6): none of the four intended RPCs or two intended trigger functions already exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P7: neither intended trigger name already exists on any table. Both
--     triggers now fire BEFORE INSERT OR UPDATE OR DELETE (SIM-PERSIST-04C
--     added a transaction-local INSERT guard alongside the pre-existing
--     UPDATE/DELETE guard on each -- the trigger NAMES here are unchanged).
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
        'trg_guard_scenario_attempt_mutation',
        'trg_guard_scenario_decision_immutability'
    )
    AND    NOT t.tgisinternal;

    IF v_count > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P7): one or both of trg_guard_scenario_attempt_mutation / trg_guard_scenario_decision_immutability already exists. Object-name conflict -- the V68 migration cannot be applied without review.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P7): neither intended trigger name already exists.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P8: none of the intended index names already exist.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count int;
BEGIN
    SELECT count(*) INTO v_count
    FROM   pg_indexes
    WHERE  schemaname = 'public'
    AND    indexname IN (
        'idx_scenario_attempts_one_in_progress',
        'idx_scenario_attempts_scenario_version_id',
        'idx_scenario_attempts_user_email_status'
    );

    IF v_count > 0 THEN
        RAISE EXCEPTION 'PREFLIGHT FAIL (P8): one or more intended V68 index names already exist. Object-name conflict -- the V68 migration cannot be applied without review.';
    END IF;
    RAISE NOTICE 'PREFLIGHT PASS (P8): none of the intended V68 index names exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- P9 (informational, non-blocking): report the CURRENT security shape of
--     scenarios/scenario_versions -- i.e. the actual, already-applied V66/
--     V67 grant and policy state this migration's own tables are meant to
--     match. Never inspects or reports rows, only privileges/policies.
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
        RAISE NOTICE 'PREFLIGHT INFO (P9): relation % has relrowsecurity=%, relforcerowsecurity=%.',
            v_row.relname, v_row.relrowsecurity, v_row.relforcerowsecurity;
    END LOOP;

    IF has_table_privilege('service_role', 'public.scenarios', 'DELETE')
       OR has_table_privilege('service_role', 'public.scenario_versions', 'DELETE')
    THEN
        RAISE NOTICE 'PREFLIGHT INFO (P9): service_role currently holds DELETE on scenarios or scenario_versions -- this is unrelated to V68 (which grants no DELETE on either new table) but is reported for operator context.';
    ELSE
        RAISE NOTICE 'PREFLIGHT INFO (P9): service_role holds no DELETE on scenarios or scenario_versions, consistent with the post-V67 state.';
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- P10 (informational, non-blocking): report any pre-existing policies on
--     the two intended table names, in case they exist under a different
--     owner/state than expected by P4/P5 (defense-in-depth reporting only --
--     P4/P5 already fail the preflight if the tables exist at all).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_row   record;
    v_found boolean := false;
BEGIN
    FOR v_row IN
        SELECT schemaname, tablename, policyname, cmd, roles
        FROM   pg_catalog.pg_policies
        WHERE  schemaname = 'public'
        AND    tablename IN ('scenario_attempts', 'scenario_decisions')
    LOOP
        v_found := true;
        RAISE NOTICE 'PREFLIGHT INFO (P10): pre-existing policy found: table=%.% policy=% cmd=% roles=%',
            v_row.schemaname, v_row.tablename, v_row.policyname, v_row.cmd, v_row.roles;
    END LOOP;

    IF NOT v_found THEN
        RAISE NOTICE 'PREFLIGHT INFO (P10): no pre-existing policies found on scenario_attempts/scenario_decisions (expected, since P4/P5 already confirm neither table exists yet).';
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- P11 (informational, non-blocking): relevant role existence. All four
--     RPCs and both tables grant to service_role only; PUBLIC, anon,
--     authenticated, AND service_role are each the target of an explicit
--     REVOKE ALL FIRST (SIM-PERSIST-04C correction -- the V68 migration no
--     longer relies on "no grant was ever issued" to keep anon/
--     authenticated/service_role at zero the way the original V66 grant
--     section silently did; every role's starting privilege set is made
--     zero explicitly, regardless of history, before service_role's exact
--     minimum grant is re-added).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_row record;
BEGIN
    FOR v_row IN
        SELECT rolname
        FROM   pg_roles
        WHERE  rolname IN ('service_role', 'anon', 'authenticated')
        ORDER  BY rolname
    LOOP
        RAISE NOTICE 'PREFLIGHT INFO (P11): role % exists in this database.', v_row.rolname;
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- P12 (informational, non-blocking): migration-history absence. Reported
--     purely as operator context -- the V68 migration deliberately does
--     not create, backfill, or repair supabase_migrations.schema_migrations,
--     exactly as V67 already documented and left unresolved.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_row record;
BEGIN
    FOR v_row IN
        SELECT version
        FROM   supabase_migrations.schema_migrations
        WHERE  version IN ('20260718170000', '20260719003000', '20260719130000')
        ORDER  BY version
    LOOP
        RAISE NOTICE 'PREFLIGHT INFO (P12): migration version % is already recorded as applied in this database.', v_row.version;
    END LOOP;
EXCEPTION WHEN undefined_table THEN
    RAISE NOTICE 'PREFLIGHT INFO (P12): supabase_migrations.schema_migrations is not present in this database (informational only -- V68 does not create, backfill, or repair this table; migration-history onboarding remains a separate, repository-wide task, as already documented in V67).';
END;
$$;

DO $$
BEGIN
    RAISE NOTICE 'PREFLIGHT SUMMARY: all blocking V68 checks passed. Review PREFLIGHT INFO output above, then apply supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql manually.';
END;
$$;
