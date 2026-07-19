-- =============================================================================
-- V67 Production Scenario Security Hotfix — VERIFICATION
--
-- Targets: supabase/migrations/20260719003000_v67_harden_scenario_definition_
--          security.sql (applied on top of an already-installed V66
--          foundation -- see supabase/migrations/20260718170000_v66_scenario_
--          definition_persistence_foundation.sql).
--
-- Intended to run ONLY against an approved test database, AFTER both the
-- V66 migration and the V67 hotfix have been applied there:
--   psql "$TEST_DATABASE_URL" -f supabase/tests/v67_harden_scenario_definition_security_verification.sql
--
-- Does not depend on pgTAP. Uses plain DO blocks with explicit
-- RAISE EXCEPTION / RAISE NOTICE, consistent with this repository's existing
-- verification scripts.
--
-- SIM-PERSIST-03A hardening applied in this revision:
--   * Added a dedicated zero-RLS-policy check (V4).
--   * Replaced broad `WHEN OTHERS THEN v_caught := true` behavioral checks
--     with focused message matching -- an exception is only accepted as
--     proof of correct rejection when its text contains the specific
--     substring that check is meant to prove; any other exception is
--     re-raised instead of silently counted as a pass.
--   * Function-property checks (SECURITY INVOKER, search_path) now resolve
--     exact function OIDs via to_regprocedure(...) rather than querying
--     pg_proc by proname alone.
--   * Added an execution-context disclosure NOTICE at the top of the file.
--   * All checks renumbered V1-V24 to reflect the new check added.
--
-- Read-only introspection (V1-V14) runs outside any transaction, against
-- the already-committed post-V67 state. All row-level exercise (V15-V23)
-- happens inside a single BEGIN ... ROLLBACK transaction and leaves no
-- residue (V24).
--
-- This script must NEVER be executed by this task (SIM-PERSIST-03A) -- it
-- is written and reviewed only, not run. It does not execute SQL, does not
-- connect to Supabase, and does not modify the V67 migration or V66.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- EXECUTION CONTEXT DISCLOSURE (read-only, informational only -- not a
-- pass/fail check).
--
-- Grant checks below (V5-V11) call has_table_privilege / has_function_
-- privilege for specific named roles (PUBLIC, anon, authenticated,
-- service_role), so they report those roles' EFFECTIVE privileges
-- correctly regardless of which role is actually connected to run this
-- script. Row-level trigger and RPC behavior (V15-V23), however, executes
-- AS the role that runs this script (session_user / current_user below) --
-- if that is not service_role, the grant-check results above still
-- correctly describe service_role, but the row-level pass/fail results
-- reflect the CONNECTED role's own privileges and whatever RLS policies
-- would apply to it (none are expected to exist at all -- see V4). This
-- script verifies database-level grants, RLS/policy state, and trigger/
-- function behavior directly over a SQL connection; it does NOT claim to be
-- an end-to-end Supabase API test, and does not observe behavior through
-- PostgREST or the Supabase client libraries, or via actual anon/
-- authenticated network requests. No SET ROLE is used anywhere in this
-- script.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    RAISE NOTICE 'EXECUTION CONTEXT: current_database=%, session_user=%, current_user=%.',
        current_database(), session_user, current_user;
END;
$$;


-- ---------------------------------------------------------------------------
-- V1: expected tables and triggers exist.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.scenarios') IS NULL THEN
        RAISE EXCEPTION 'V1 FAILED: public.scenarios does not exist.';
    END IF;
    IF to_regclass('public.scenario_versions') IS NULL THEN
        RAISE EXCEPTION 'V1 FAILED: public.scenario_versions does not exist.';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM   pg_trigger t
        JOIN   pg_class c ON c.oid = t.tgrelid
        JOIN   pg_namespace n ON n.oid = c.relnamespace
        WHERE  n.nspname = 'public'
        AND    c.relname = 'scenarios'
        AND    t.tgname = 'trg_guard_scenario_current_published_version'
        AND    NOT t.tgisinternal
    ) THEN
        RAISE EXCEPTION 'V1 FAILED: trg_guard_scenario_current_published_version does not exist on public.scenarios.';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM   pg_trigger t
        JOIN   pg_class c ON c.oid = t.tgrelid
        JOIN   pg_namespace n ON n.oid = c.relnamespace
        WHERE  n.nspname = 'public'
        AND    c.relname = 'scenario_versions'
        AND    t.tgname = 'trg_guard_scenario_version_immutability'
        AND    NOT t.tgisinternal
    ) THEN
        RAISE EXCEPTION 'V1 FAILED: trg_guard_scenario_version_immutability does not exist on public.scenario_versions.';
    END IF;

    RAISE NOTICE 'V1 PASSED: both tables and both triggers exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V2: all three relevant functions resolve to exact, non-null OIDs via
--     to_regprocedure(...) -- not merely by proname.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_oid_publish            regprocedure;
    v_oid_guard_pointer      regprocedure;
    v_oid_guard_immutability regprocedure;
BEGIN
    v_oid_publish            := to_regprocedure('public.publish_scenario_version_v1(uuid,jsonb,text)');
    v_oid_guard_pointer      := to_regprocedure('public.guard_scenario_current_published_version_v1()');
    v_oid_guard_immutability := to_regprocedure('public.guard_scenario_version_immutability_v1()');

    IF v_oid_publish IS NULL THEN
        RAISE EXCEPTION 'V2 FAILED: public.publish_scenario_version_v1(uuid,jsonb,text) does not resolve to an exact function OID.';
    END IF;
    IF v_oid_guard_pointer IS NULL THEN
        RAISE EXCEPTION 'V2 FAILED: public.guard_scenario_current_published_version_v1() does not resolve to an exact function OID.';
    END IF;
    IF v_oid_guard_immutability IS NULL THEN
        RAISE EXCEPTION 'V2 FAILED: public.guard_scenario_version_immutability_v1() does not resolve to an exact function OID.';
    END IF;

    RAISE NOTICE 'V2 PASSED: all three expected functions resolve to exact, non-null OIDs (publish=%, guard_pointer=%, guard_immutability=%).',
        v_oid_publish, v_oid_guard_pointer, v_oid_guard_immutability;
END;
$$;

-- ---------------------------------------------------------------------------
-- V3: RLS remains enabled on both tables.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_row record;
BEGIN
    FOR v_row IN
        SELECT c.relname, c.relrowsecurity
        FROM   pg_class c
        JOIN   pg_namespace n ON n.oid = c.relnamespace
        WHERE  n.nspname = 'public'
        AND    c.relname IN ('scenarios', 'scenario_versions')
    LOOP
        IF NOT v_row.relrowsecurity THEN
            RAISE EXCEPTION 'V3 FAILED: RLS is not enabled on public.%', v_row.relname;
        END IF;
    END LOOP;
    RAISE NOTICE 'V3 PASSED: RLS remains enabled on both public.scenarios and public.scenario_versions.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V4: zero RLS policies exist on either table. The intended V1 security
--     model is RLS enabled + zero policies + zero PUBLIC/anon/authenticated
--     table privileges + server-only service_role access. Any policy found
--     is reported by exact name, command, and roles before failing.
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
        AND    tablename IN ('scenarios', 'scenario_versions')
    LOOP
        v_found := true;
        RAISE WARNING 'V4 UNEXPECTED POLICY: table=%.% policy=% cmd=% roles=%',
            v_row.schemaname, v_row.tablename, v_row.policyname, v_row.cmd, v_row.roles;
    END LOOP;

    IF v_found THEN
        RAISE EXCEPTION 'V4 FAILED: one or more RLS policies exist on public.scenarios or public.scenario_versions (see the V4 UNEXPECTED POLICY warning(s) above for the exact name, command, and roles). The intended V1 model has zero policies on either table.';
    END IF;

    RAISE NOTICE 'V4 PASSED: zero RLS policies exist on both public.scenarios and public.scenario_versions.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V5: PUBLIC has no table privileges on either table.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('public', 'public.scenarios', 'SELECT')
       OR has_table_privilege('public', 'public.scenarios', 'INSERT')
       OR has_table_privilege('public', 'public.scenarios', 'UPDATE')
       OR has_table_privilege('public', 'public.scenarios', 'DELETE')
       OR has_table_privilege('public', 'public.scenarios', 'TRUNCATE')
       OR has_table_privilege('public', 'public.scenarios', 'REFERENCES')
       OR has_table_privilege('public', 'public.scenarios', 'TRIGGER')
       OR has_table_privilege('public', 'public.scenario_versions', 'SELECT')
       OR has_table_privilege('public', 'public.scenario_versions', 'INSERT')
       OR has_table_privilege('public', 'public.scenario_versions', 'UPDATE')
       OR has_table_privilege('public', 'public.scenario_versions', 'DELETE')
       OR has_table_privilege('public', 'public.scenario_versions', 'TRUNCATE')
       OR has_table_privilege('public', 'public.scenario_versions', 'REFERENCES')
       OR has_table_privilege('public', 'public.scenario_versions', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V5 FAILED: the PUBLIC pseudo-role has a privilege on scenarios or scenario_versions.';
    END IF;
    RAISE NOTICE 'V5 PASSED: PUBLIC has zero privileges on both tables.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V6: anon has none of SELECT/INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/
--     TRIGGER on either table.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('anon', 'public.scenarios', 'SELECT')
       OR has_table_privilege('anon', 'public.scenarios', 'INSERT')
       OR has_table_privilege('anon', 'public.scenarios', 'UPDATE')
       OR has_table_privilege('anon', 'public.scenarios', 'DELETE')
       OR has_table_privilege('anon', 'public.scenarios', 'TRUNCATE')
       OR has_table_privilege('anon', 'public.scenarios', 'REFERENCES')
       OR has_table_privilege('anon', 'public.scenarios', 'TRIGGER')
       OR has_table_privilege('anon', 'public.scenario_versions', 'SELECT')
       OR has_table_privilege('anon', 'public.scenario_versions', 'INSERT')
       OR has_table_privilege('anon', 'public.scenario_versions', 'UPDATE')
       OR has_table_privilege('anon', 'public.scenario_versions', 'DELETE')
       OR has_table_privilege('anon', 'public.scenario_versions', 'TRUNCATE')
       OR has_table_privilege('anon', 'public.scenario_versions', 'REFERENCES')
       OR has_table_privilege('anon', 'public.scenario_versions', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V6 FAILED: anon has a privilege on scenarios or scenario_versions.';
    END IF;
    RAISE NOTICE 'V6 PASSED: anon has zero privileges on both tables.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V7: authenticated has none of the equivalent privileges on either table.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('authenticated', 'public.scenarios', 'SELECT')
       OR has_table_privilege('authenticated', 'public.scenarios', 'INSERT')
       OR has_table_privilege('authenticated', 'public.scenarios', 'UPDATE')
       OR has_table_privilege('authenticated', 'public.scenarios', 'DELETE')
       OR has_table_privilege('authenticated', 'public.scenarios', 'TRUNCATE')
       OR has_table_privilege('authenticated', 'public.scenarios', 'REFERENCES')
       OR has_table_privilege('authenticated', 'public.scenarios', 'TRIGGER')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'SELECT')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'INSERT')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'UPDATE')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'DELETE')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'TRUNCATE')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'REFERENCES')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V7 FAILED: authenticated has a privilege on scenarios or scenario_versions.';
    END IF;
    RAISE NOTICE 'V7 PASSED: authenticated has zero privileges on both tables.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V8: service_role has exactly SELECT, INSERT, and UPDATE on both tables.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT has_table_privilege('service_role', 'public.scenarios', 'SELECT')
       OR NOT has_table_privilege('service_role', 'public.scenarios', 'INSERT')
       OR NOT has_table_privilege('service_role', 'public.scenarios', 'UPDATE')
    THEN
        RAISE EXCEPTION 'V8 FAILED: service_role is missing SELECT/INSERT/UPDATE on public.scenarios.';
    END IF;

    IF NOT has_table_privilege('service_role', 'public.scenario_versions', 'SELECT')
       OR NOT has_table_privilege('service_role', 'public.scenario_versions', 'INSERT')
       OR NOT has_table_privilege('service_role', 'public.scenario_versions', 'UPDATE')
    THEN
        RAISE EXCEPTION 'V8 FAILED: service_role is missing SELECT/INSERT/UPDATE on public.scenario_versions.';
    END IF;

    RAISE NOTICE 'V8 PASSED: service_role has SELECT, INSERT, UPDATE on both tables.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V9: service_role lacks DELETE, TRUNCATE, REFERENCES, and TRIGGER on
--     either table.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('service_role', 'public.scenarios', 'DELETE')
       OR has_table_privilege('service_role', 'public.scenarios', 'TRUNCATE')
       OR has_table_privilege('service_role', 'public.scenarios', 'REFERENCES')
       OR has_table_privilege('service_role', 'public.scenarios', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V9 FAILED: service_role unexpectedly has DELETE/TRUNCATE/REFERENCES/TRIGGER on public.scenarios.';
    END IF;

    IF has_table_privilege('service_role', 'public.scenario_versions', 'DELETE')
       OR has_table_privilege('service_role', 'public.scenario_versions', 'TRUNCATE')
       OR has_table_privilege('service_role', 'public.scenario_versions', 'REFERENCES')
       OR has_table_privilege('service_role', 'public.scenario_versions', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V9 FAILED: service_role unexpectedly has DELETE/TRUNCATE/REFERENCES/TRIGGER on public.scenario_versions.';
    END IF;

    RAISE NOTICE 'V9 PASSED: service_role lacks DELETE, TRUNCATE, REFERENCES, and TRIGGER on both tables.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V10: anon and authenticated cannot execute the publication RPC.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_function_privilege('anon', 'public.publish_scenario_version_v1(uuid, jsonb, text)', 'EXECUTE')
       OR has_function_privilege('authenticated', 'public.publish_scenario_version_v1(uuid, jsonb, text)', 'EXECUTE')
    THEN
        RAISE EXCEPTION 'V10 FAILED: anon or authenticated can EXECUTE public.publish_scenario_version_v1.';
    END IF;
    RAISE NOTICE 'V10 PASSED: anon and authenticated cannot execute the publication RPC.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V11: service_role can execute the publication RPC.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT has_function_privilege('service_role', 'public.publish_scenario_version_v1(uuid, jsonb, text)', 'EXECUTE') THEN
        RAISE EXCEPTION 'V11 FAILED: service_role cannot execute public.publish_scenario_version_v1.';
    END IF;
    RAISE NOTICE 'V11 PASSED: service_role can execute the publication RPC.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V12: the publication RPC remains SECURITY INVOKER, checked against its
--      exact OID (resolved via to_regprocedure, not proname).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_oid       regprocedure;
    v_prosecdef boolean;
BEGIN
    v_oid := to_regprocedure('public.publish_scenario_version_v1(uuid,jsonb,text)');
    IF v_oid IS NULL THEN
        RAISE EXCEPTION 'V12 FAILED: public.publish_scenario_version_v1(uuid,jsonb,text) does not resolve to an exact function OID.';
    END IF;

    SELECT p.prosecdef INTO v_prosecdef FROM pg_proc p WHERE p.oid = v_oid::oid;

    IF v_prosecdef THEN
        RAISE EXCEPTION 'V12 FAILED: public.publish_scenario_version_v1 (oid=%) is SECURITY DEFINER, expected SECURITY INVOKER.', v_oid;
    END IF;

    RAISE NOTICE 'V12 PASSED: public.publish_scenario_version_v1 (oid=%) remains SECURITY INVOKER.', v_oid;
END;
$$;

-- ---------------------------------------------------------------------------
-- V13: all three relevant functions retain search_path = public, pg_catalog,
--      checked against exactly their three resolved OIDs. Exactly three
--      function records must be inspected; any null OID fails immediately.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_oids      regprocedure[] := ARRAY[
        to_regprocedure('public.publish_scenario_version_v1(uuid,jsonb,text)'),
        to_regprocedure('public.guard_scenario_current_published_version_v1()'),
        to_regprocedure('public.guard_scenario_version_immutability_v1()')
    ];
    v_oid        regprocedure;
    v_config     text[];
    v_normalized text;
    v_inspected  int := 0;
BEGIN
    FOREACH v_oid IN ARRAY v_oids
    LOOP
        IF v_oid IS NULL THEN
            RAISE EXCEPTION 'V13 FAILED: one of the three expected function OIDs is null (a function did not resolve via to_regprocedure).';
        END IF;

        SELECT p.proconfig INTO v_config FROM pg_proc p WHERE p.oid = v_oid::oid;

        v_normalized := array_to_string(coalesce(v_config, ARRAY[]::text[]), ';');
        v_normalized := regexp_replace(v_normalized, '\s+', '', 'g');
        IF v_normalized NOT LIKE '%search_path=public,pg_catalog%' THEN
            RAISE EXCEPTION 'V13 FAILED: function with oid % does not have SET search_path = public, pg_catalog (proconfig=%).', v_oid, v_config;
        END IF;

        v_inspected := v_inspected + 1;
    END LOOP;

    IF v_inspected <> 3 THEN
        RAISE EXCEPTION 'V13 FAILED: expected exactly 3 function records inspected for search_path, got %.', v_inspected;
    END IF;

    RAISE NOTICE 'V13 PASSED: exactly 3 expected function OIDs resolved, and all 3 retain search_path = public, pg_catalog.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V14: the pointer trigger remains installed and enabled with the expected
--      firing contract.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_row record;
BEGIN
    SELECT t.tgenabled, pg_get_triggerdef(t.oid) AS def
    INTO   v_row
    FROM   pg_trigger t
    JOIN   pg_class c ON c.oid = t.tgrelid
    JOIN   pg_namespace n ON n.oid = c.relnamespace
    WHERE  n.nspname = 'public'
    AND    c.relname = 'scenarios'
    AND    t.tgname = 'trg_guard_scenario_current_published_version'
    AND    NOT t.tgisinternal;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'V14 FAILED: trg_guard_scenario_current_published_version does not exist on public.scenarios.';
    END IF;

    IF v_row.tgenabled = 'D' THEN
        RAISE EXCEPTION 'V14 FAILED: trg_guard_scenario_current_published_version exists but is disabled.';
    END IF;

    IF v_row.def NOT LIKE '%BEFORE INSERT OR UPDATE OF current_published_version_id%' THEN
        RAISE EXCEPTION 'V14 FAILED: trg_guard_scenario_current_published_version firing contract unexpected: %', v_row.def;
    END IF;

    RAISE NOTICE 'V14 PASSED: the pointer trigger remains installed and enabled with the expected firing contract.';
END;
$$;


-- =============================================================================
-- V15-V23: row-level behavior exercised inside a single rolled-back
-- transaction. All rows use a 'v67-verify-' simulation_id prefix. Each
-- expected-failure check accepts only an exception whose message contains
-- the specific substring proving that check's exact failure mode; any other
-- exception is re-raised rather than counted as a pass.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    v_scenario_a_id  uuid;
    v_scenario_b_id  uuid;
    v_draft1_id      uuid;
    v_draft2_id      uuid;
    v_draft_b_id     uuid;
    v_valid_hash     text := repeat('d', 64);
    v_valid_hash_2   text := repeat('e', 64);
    v_valid_hash_b   text := repeat('f', 64);
    v_current_ptr    uuid;
    v_caught         boolean;
BEGIN
    ----------------------------------------------------------------------
    -- V15: initial INSERT with a NULL pointer succeeds.
    ----------------------------------------------------------------------
    INSERT INTO public.scenarios (simulation_id, certification_exam_name, title)
    VALUES ('v67-verify-sim-01', 'Business Analyst', 'V67 Verification Scenario A')
    RETURNING id INTO v_scenario_a_id;

    SELECT current_published_version_id INTO v_current_ptr FROM public.scenarios WHERE id = v_scenario_a_id;
    IF v_current_ptr IS NOT NULL THEN
        RAISE EXCEPTION 'V15 FAILED: newly inserted scenario did not default current_published_version_id to NULL.';
    END IF;
    RAISE NOTICE 'V15 PASSED: initial INSERT with a NULL pointer succeeded.';

    ----------------------------------------------------------------------
    -- V16: valid draft publication still succeeds.
    -- V17: the current pointer is set by publication.
    ----------------------------------------------------------------------
    INSERT INTO public.scenario_versions (scenario_id, version, schema_version, engine_version, source_repository_path)
    VALUES (v_scenario_a_id, '1.0.0', '1.0.0', '1.0.0', 'scenario_content/business_analyst/v67-verify-sim-01/1.0.0/scenario.json')
    RETURNING id INTO v_draft1_id;

    PERFORM public.publish_scenario_version_v1(
        v_draft1_id,
        jsonb_build_object('simulationId', 'v67-verify-sim-01', 'version', '1.0.0', 'schemaVersion', '1.0.0'),
        v_valid_hash
    );
    RAISE NOTICE 'V16 PASSED: valid draft publication succeeded.';

    SELECT current_published_version_id INTO v_current_ptr FROM public.scenarios WHERE id = v_scenario_a_id;
    IF v_current_ptr IS DISTINCT FROM v_draft1_id THEN
        RAISE EXCEPTION 'V17 FAILED: scenarios.current_published_version_id (%) does not point to draft1 (%) after publication.', v_current_ptr, v_draft1_id;
    END IF;
    RAISE NOTICE 'V17 PASSED: the current pointer was set by publication.';

    ----------------------------------------------------------------------
    -- V18: direct clearing of a non-null current pointer fails with the
    -- focused exception current_published_version_clear_not_allowed.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenarios SET current_published_version_id = NULL WHERE id = v_scenario_a_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'current_published_version_clear_not_allowed:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V18 FAILED: direct clearing of a non-null current pointer did not raise current_published_version_clear_not_allowed.';
    END IF;
    RAISE NOTICE 'V18 PASSED: direct clearing of a non-null current pointer was rejected with the focused exception.';

    ----------------------------------------------------------------------
    -- V19 (setup + check): pointing scenario A's pointer directly at
    -- scenario B's published version fails because it belongs to a
    -- different scenario. Only an exception whose message contains
    -- "belongs to scenario" is accepted; any other exception is re-raised.
    ----------------------------------------------------------------------
    INSERT INTO public.scenario_versions (scenario_id, version, schema_version, engine_version, source_repository_path)
    VALUES (v_scenario_a_id, '1.1.0', '1.0.0', '1.0.0', 'scenario_content/business_analyst/v67-verify-sim-01/1.1.0/scenario.json')
    RETURNING id INTO v_draft2_id;

    INSERT INTO public.scenarios (simulation_id, certification_exam_name, title)
    VALUES ('v67-verify-sim-02', 'Business Analyst', 'V67 Verification Scenario B')
    RETURNING id INTO v_scenario_b_id;

    INSERT INTO public.scenario_versions (scenario_id, version, schema_version, engine_version, source_repository_path)
    VALUES (v_scenario_b_id, '1.0.0', '1.0.0', '1.0.0', 'scenario_content/business_analyst/v67-verify-sim-02/1.0.0/scenario.json')
    RETURNING id INTO v_draft_b_id;

    PERFORM public.publish_scenario_version_v1(
        v_draft_b_id,
        jsonb_build_object('simulationId', 'v67-verify-sim-02', 'version', '1.0.0', 'schemaVersion', '1.0.0'),
        v_valid_hash_b
    );

    v_caught := false;
    BEGIN
        UPDATE public.scenarios SET current_published_version_id = v_draft_b_id WHERE id = v_scenario_a_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%belongs to scenario%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V19 FAILED: direct change of scenario A''s pointer to scenario B''s published version did not raise a "belongs to scenario" mismatch.';
    END IF;
    RAISE NOTICE 'V19 PASSED: cross-scenario pointer change was rejected with a "belongs to scenario" mismatch.';

    ----------------------------------------------------------------------
    -- V20: a direct, same-scenario pointer change to an already-published
    -- (but not currently-guarded) version fails because the publication
    -- guard is not set for that exact target. Only an exception whose
    -- message contains "publication guard not set" is accepted; any other
    -- exception is re-raised.
    ----------------------------------------------------------------------
    PERFORM public.publish_scenario_version_v1(
        v_draft2_id,
        jsonb_build_object('simulationId', 'v67-verify-sim-01', 'version', '1.1.0', 'schemaVersion', '1.0.0'),
        v_valid_hash_2
    );

    v_caught := false;
    BEGIN
        UPDATE public.scenarios SET current_published_version_id = v_draft1_id WHERE id = v_scenario_a_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%publication guard not set%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V20 FAILED: direct same-scenario pointer change outside the publication guard did not raise a "publication guard not set" error.';
    END IF;
    RAISE NOTICE 'V20 PASSED: direct same-scenario pointer change outside the publication guard was rejected.';

    ----------------------------------------------------------------------
    -- V21: an ordinary scenario update that does not change the pointer
    -- still succeeds.
    ----------------------------------------------------------------------
    UPDATE public.scenarios SET description = 'updated by V67 verification' WHERE id = v_scenario_a_id;

    SELECT current_published_version_id INTO v_current_ptr FROM public.scenarios WHERE id = v_scenario_a_id;
    IF v_current_ptr IS DISTINCT FROM v_draft2_id THEN
        RAISE EXCEPTION 'V21 FAILED: an ordinary update not touching the pointer unexpectedly changed it.';
    END IF;
    RAISE NOTICE 'V21 PASSED: an ordinary scenario update that does not change the pointer succeeded.';

    ----------------------------------------------------------------------
    -- V22: direct UPDATE of a published scenario_versions row fails.
    -- Only an exception whose message contains "immutable" is accepted.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenario_versions SET source_repository_path = 'tampered' WHERE id = v_draft1_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%immutable%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V22 FAILED: direct UPDATE of a published scenario_versions row did not raise an "immutable" error.';
    END IF;
    RAISE NOTICE 'V22 PASSED: direct UPDATE of a published scenario_versions row was rejected as immutable.';

    ----------------------------------------------------------------------
    -- V23: direct DELETE of a published scenario_versions row fails.
    -- Only an exception whose message contains "immutable" is accepted.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        DELETE FROM public.scenario_versions WHERE id = v_draft1_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%immutable%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V23 FAILED: DELETE of a published scenario_versions row did not raise an "immutable" error.';
    END IF;
    RAISE NOTICE 'V23 PASSED: direct DELETE of a published scenario_versions row was rejected as immutable.';

END;
$$;

ROLLBACK;

-- ---------------------------------------------------------------------------
-- V24: all verification data was inside BEGIN/ROLLBACK and leaves no
-- residue. Run outside the rolled-back transaction, against the real
-- (unaffected) committed state.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count int;
BEGIN
    SELECT count(*) INTO v_count
    FROM   public.scenarios
    WHERE  simulation_id LIKE 'v67-verify-sim-%';

    IF v_count <> 0 THEN
        RAISE EXCEPTION 'V24 FAILED: % residual scenario row(s) with the v67 verification prefix found after ROLLBACK.', v_count;
    END IF;

    RAISE NOTICE 'V24 PASSED: no residual test data remains after ROLLBACK.';
    RAISE NOTICE 'VERIFICATION SUMMARY: all V1-V24 checks passed for the V67 production scenario security hotfix.';
END;
$$;
