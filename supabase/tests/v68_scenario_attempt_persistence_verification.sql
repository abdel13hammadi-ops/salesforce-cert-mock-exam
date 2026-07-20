-- =============================================================================
-- V68 Scenario Attempt Persistence Foundation — VERIFICATION
--
-- Targets: supabase/migrations/20260719130000_v68_scenario_attempt_
--          persistence_foundation.sql (applied on top of an already-installed
--          V66 foundation and V67 hotfix), AS CORRECTED BY SIM-PERSIST-04C,
--          SIM-PERSIST-04E, AND SIM-PERSIST-04F (explicit per-role privilege
--          reset, RPC-only INSERT guards, stable idempotent replay, snapshot
--          IDENTITY/LIFECYCLE consistency checks with strict JSON typing,
--          pinned simulationId/version identity, terminal-ending
--          consistency, a FOR SHARE consistent-read lock on
--          get_scenario_attempt_v1, request-field-bound idempotent replay,
--          and an explicit-JSON-null requirement for a terminal decision's
--          state_after.currentSceneId).
--
-- Intended to run ONLY against an approved test database, AFTER V66, V67,
-- and V68 (with the SIM-PERSIST-04C, SIM-PERSIST-04E, and SIM-PERSIST-04F
-- corrections applied in place) have all been applied there:
--   psql "$TEST_DATABASE_URL" -f supabase/tests/v68_scenario_attempt_persistence_verification.sql
--
-- Does not depend on pgTAP. Uses plain DO blocks with explicit
-- RAISE EXCEPTION / RAISE NOTICE, consistent with this repository's existing
-- verification scripts (see supabase/tests/v67_harden_scenario_definition_
-- security_verification.sql, whose conventions this script follows exactly:
-- an execution-context disclosure NOTICE, exact-OID function resolution via
-- to_regprocedure, and focused exception matching -- an exception is only
-- accepted as proof of correct rejection when its message contains the
-- specific substring (or, for a raw constraint violation, the specific
-- SQLSTATE condition name) that check is meant to prove; any other
-- exception is re-raised instead of silently counted as a pass).
--
-- Read-only introspection (V1-V17) runs outside any transaction, against
-- the already-committed post-V68 state. All row-level exercise (V18-V62)
-- happens inside a single BEGIN ... ROLLBACK transaction and leaves no
-- residue (V63).
--
-- SIM-PERSIST-04F note on FOR SHARE: V17 below proves, by introspecting
-- get_scenario_attempt_v1's installed source text against its exact,
-- to_regprocedure-resolved OID, that the FOR SHARE locking clause this
-- correction requires is actually present in the installed function. A REAL
-- two-session concurrency exercise -- one session holding this function's
-- FOR SHARE lock open mid-transaction while a second session attempts a
-- concurrent submit_scenario_decision_v1/abandon_scenario_attempt_v1 FOR
-- UPDATE against the same row, and observing that the second session
-- blocks until the first commits or rolls back -- requires two concurrent
-- database sessions and therefore cannot be exercised inside this script's
-- own single-session BEGIN/ROLLBACK transaction. That exercise belongs in
-- this project's upcoming throwaway-database, multi-session concurrency
-- test gate, not in this single-connection introspection-and-DO-block
-- script.
--
-- This script must NEVER be executed by this task (SIM-PERSIST-04F) -- it
-- is written and reviewed only, not run. It does not execute SQL, does not
-- connect to Supabase, and does not modify the V68 migration, V67, or V66.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- EXECUTION CONTEXT DISCLOSURE (read-only, informational only -- not a
-- pass/fail check). Grant checks below (V5-V13) call has_table_privilege /
-- has_function_privilege for specific named roles, so they report those
-- roles' EFFECTIVE privileges correctly regardless of which role is
-- actually connected to run this script. Row-level trigger/RPC behavior
-- (V18-V62), however, executes AS the role that runs this script -- if that
-- is not service_role, the grant-check results above still correctly
-- describe service_role, but the row-level pass/fail results reflect the
-- CONNECTED role's own privileges and whatever RLS policies would apply to
-- it (none are expected to exist at all -- see V4). This script verifies
-- database-level grants, RLS/policy state, and trigger/function behavior
-- directly over a SQL connection; it does not claim to be an end-to-end
-- Supabase API test and does not observe behavior through PostgREST or the
-- Supabase client libraries. No SET ROLE is used anywhere in this script.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    RAISE NOTICE 'EXECUTION CONTEXT: current_database=%, session_user=%, current_user=%.',
        current_database(), session_user, current_user;
END;
$$;


-- ---------------------------------------------------------------------------
-- V1: expected tables and both new triggers exist.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.scenario_attempts') IS NULL THEN
        RAISE EXCEPTION 'V1 FAILED: public.scenario_attempts does not exist.';
    END IF;
    IF to_regclass('public.scenario_decisions') IS NULL THEN
        RAISE EXCEPTION 'V1 FAILED: public.scenario_decisions does not exist.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'scenario_attempts'
        AND t.tgname = 'trg_guard_scenario_attempt_mutation' AND NOT t.tgisinternal
    ) THEN
        RAISE EXCEPTION 'V1 FAILED: trg_guard_scenario_attempt_mutation does not exist on public.scenario_attempts.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'scenario_decisions'
        AND t.tgname = 'trg_guard_scenario_decision_immutability' AND NOT t.tgisinternal
    ) THEN
        RAISE EXCEPTION 'V1 FAILED: trg_guard_scenario_decision_immutability does not exist on public.scenario_decisions.';
    END IF;

    RAISE NOTICE 'V1 PASSED: both tables and both triggers exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V2: all six relevant functions resolve to exact, non-null OIDs via
--     to_regprocedure(...) -- not merely by proname.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_oids regprocedure[] := ARRAY[
        to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)'),
        to_regprocedure('public.get_scenario_attempt_v1(text,uuid)'),
        to_regprocedure('public.submit_scenario_decision_v1(text,uuid,uuid,integer,text,text,text,jsonb,jsonb,boolean,text,text,jsonb)'),
        to_regprocedure('public.abandon_scenario_attempt_v1(text,uuid)'),
        to_regprocedure('public.guard_scenario_attempt_mutation_v1()'),
        to_regprocedure('public.guard_scenario_decision_immutability_v1()')
    ];
    v_oid regprocedure;
    v_inspected int := 0;
BEGIN
    FOREACH v_oid IN ARRAY v_oids LOOP
        IF v_oid IS NULL THEN
            RAISE EXCEPTION 'V2 FAILED: one of the six expected function signatures does not resolve to an exact function OID.';
        END IF;
        v_inspected := v_inspected + 1;
    END LOOP;

    IF v_inspected <> 6 THEN
        RAISE EXCEPTION 'V2 FAILED: expected exactly 6 resolved function OIDs, got %.', v_inspected;
    END IF;

    RAISE NOTICE 'V2 PASSED: all six expected functions resolve to exact, non-null OIDs.';
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
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname IN ('scenario_attempts', 'scenario_decisions')
    LOOP
        IF NOT v_row.relrowsecurity THEN
            RAISE EXCEPTION 'V3 FAILED: RLS is not enabled on public.%', v_row.relname;
        END IF;
    END LOOP;
    RAISE NOTICE 'V3 PASSED: RLS is enabled on both public.scenario_attempts and public.scenario_decisions.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V4: zero RLS policies exist on either table.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_row record;
    v_found boolean := false;
BEGIN
    FOR v_row IN
        SELECT schemaname, tablename, policyname, cmd, roles
        FROM pg_catalog.pg_policies
        WHERE schemaname = 'public' AND tablename IN ('scenario_attempts', 'scenario_decisions')
    LOOP
        v_found := true;
        RAISE WARNING 'V4 UNEXPECTED POLICY: table=%.% policy=% cmd=% roles=%',
            v_row.schemaname, v_row.tablename, v_row.policyname, v_row.cmd, v_row.roles;
    END LOOP;

    IF v_found THEN
        RAISE EXCEPTION 'V4 FAILED: one or more RLS policies exist on public.scenario_attempts or public.scenario_decisions.';
    END IF;
    RAISE NOTICE 'V4 PASSED: zero RLS policies exist on both tables.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V5: PUBLIC has no table privileges on either table (SIM-PERSIST-04C
--     explicit revoke -- see migration section 7).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('public', 'public.scenario_attempts', 'SELECT')
       OR has_table_privilege('public', 'public.scenario_attempts', 'INSERT')
       OR has_table_privilege('public', 'public.scenario_attempts', 'UPDATE')
       OR has_table_privilege('public', 'public.scenario_attempts', 'DELETE')
       OR has_table_privilege('public', 'public.scenario_attempts', 'TRUNCATE')
       OR has_table_privilege('public', 'public.scenario_attempts', 'REFERENCES')
       OR has_table_privilege('public', 'public.scenario_attempts', 'TRIGGER')
       OR has_table_privilege('public', 'public.scenario_decisions', 'SELECT')
       OR has_table_privilege('public', 'public.scenario_decisions', 'INSERT')
       OR has_table_privilege('public', 'public.scenario_decisions', 'UPDATE')
       OR has_table_privilege('public', 'public.scenario_decisions', 'DELETE')
       OR has_table_privilege('public', 'public.scenario_decisions', 'TRUNCATE')
       OR has_table_privilege('public', 'public.scenario_decisions', 'REFERENCES')
       OR has_table_privilege('public', 'public.scenario_decisions', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V5 FAILED: the PUBLIC pseudo-role has a privilege on scenario_attempts or scenario_decisions.';
    END IF;
    RAISE NOTICE 'V5 PASSED: PUBLIC has zero privileges on both tables (explicitly revoked).';
END;
$$;

-- ---------------------------------------------------------------------------
-- V6: anon has zero privileges on either table (SIM-PERSIST-04C explicit
--     revoke -- no longer relying on "no grant was ever issued").
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('anon', 'public.scenario_attempts', 'SELECT')
       OR has_table_privilege('anon', 'public.scenario_attempts', 'INSERT')
       OR has_table_privilege('anon', 'public.scenario_attempts', 'UPDATE')
       OR has_table_privilege('anon', 'public.scenario_attempts', 'DELETE')
       OR has_table_privilege('anon', 'public.scenario_attempts', 'TRUNCATE')
       OR has_table_privilege('anon', 'public.scenario_attempts', 'REFERENCES')
       OR has_table_privilege('anon', 'public.scenario_attempts', 'TRIGGER')
       OR has_table_privilege('anon', 'public.scenario_decisions', 'SELECT')
       OR has_table_privilege('anon', 'public.scenario_decisions', 'INSERT')
       OR has_table_privilege('anon', 'public.scenario_decisions', 'UPDATE')
       OR has_table_privilege('anon', 'public.scenario_decisions', 'DELETE')
       OR has_table_privilege('anon', 'public.scenario_decisions', 'TRUNCATE')
       OR has_table_privilege('anon', 'public.scenario_decisions', 'REFERENCES')
       OR has_table_privilege('anon', 'public.scenario_decisions', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V6 FAILED: anon has a privilege on scenario_attempts or scenario_decisions.';
    END IF;
    RAISE NOTICE 'V6 PASSED: anon has zero privileges on both tables (explicitly revoked).';
END;
$$;

-- ---------------------------------------------------------------------------
-- V7: authenticated has zero privileges on either table (SIM-PERSIST-04C
--     explicit revoke).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('authenticated', 'public.scenario_attempts', 'SELECT')
       OR has_table_privilege('authenticated', 'public.scenario_attempts', 'INSERT')
       OR has_table_privilege('authenticated', 'public.scenario_attempts', 'UPDATE')
       OR has_table_privilege('authenticated', 'public.scenario_attempts', 'DELETE')
       OR has_table_privilege('authenticated', 'public.scenario_attempts', 'TRUNCATE')
       OR has_table_privilege('authenticated', 'public.scenario_attempts', 'REFERENCES')
       OR has_table_privilege('authenticated', 'public.scenario_attempts', 'TRIGGER')
       OR has_table_privilege('authenticated', 'public.scenario_decisions', 'SELECT')
       OR has_table_privilege('authenticated', 'public.scenario_decisions', 'INSERT')
       OR has_table_privilege('authenticated', 'public.scenario_decisions', 'UPDATE')
       OR has_table_privilege('authenticated', 'public.scenario_decisions', 'DELETE')
       OR has_table_privilege('authenticated', 'public.scenario_decisions', 'TRUNCATE')
       OR has_table_privilege('authenticated', 'public.scenario_decisions', 'REFERENCES')
       OR has_table_privilege('authenticated', 'public.scenario_decisions', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V7 FAILED: authenticated has a privilege on scenario_attempts or scenario_decisions.';
    END IF;
    RAISE NOTICE 'V7 PASSED: authenticated has zero privileges on both tables (explicitly revoked).';
END;
$$;

-- ---------------------------------------------------------------------------
-- V8: service_role has exactly SELECT, INSERT, UPDATE on scenario_attempts,
--     re-granted explicitly AFTER an explicit REVOKE ALL from service_role
--     itself (SIM-PERSIST-04C).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT has_table_privilege('service_role', 'public.scenario_attempts', 'SELECT')
       OR NOT has_table_privilege('service_role', 'public.scenario_attempts', 'INSERT')
       OR NOT has_table_privilege('service_role', 'public.scenario_attempts', 'UPDATE')
    THEN
        RAISE EXCEPTION 'V8 FAILED: service_role is missing SELECT/INSERT/UPDATE on public.scenario_attempts.';
    END IF;
    RAISE NOTICE 'V8 PASSED: service_role has exactly SELECT, INSERT, UPDATE re-granted on scenario_attempts.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V9: service_role has exactly SELECT, INSERT on scenario_decisions (no
--     UPDATE, no DELETE -- append-only), re-granted explicitly AFTER an
--     explicit REVOKE ALL from service_role itself (SIM-PERSIST-04C).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT has_table_privilege('service_role', 'public.scenario_decisions', 'SELECT')
       OR NOT has_table_privilege('service_role', 'public.scenario_decisions', 'INSERT')
    THEN
        RAISE EXCEPTION 'V9 FAILED: service_role is missing SELECT/INSERT on public.scenario_decisions.';
    END IF;
    IF has_table_privilege('service_role', 'public.scenario_decisions', 'UPDATE')
       OR has_table_privilege('service_role', 'public.scenario_decisions', 'DELETE')
    THEN
        RAISE EXCEPTION 'V9 FAILED: service_role unexpectedly has UPDATE or DELETE on public.scenario_decisions.';
    END IF;
    RAISE NOTICE 'V9 PASSED: service_role has exactly SELECT, INSERT on scenario_decisions.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V10: service_role lacks DELETE, TRUNCATE, REFERENCES, TRIGGER on
--      scenario_attempts.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('service_role', 'public.scenario_attempts', 'DELETE')
       OR has_table_privilege('service_role', 'public.scenario_attempts', 'TRUNCATE')
       OR has_table_privilege('service_role', 'public.scenario_attempts', 'REFERENCES')
       OR has_table_privilege('service_role', 'public.scenario_attempts', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V10 FAILED: service_role unexpectedly has DELETE/TRUNCATE/REFERENCES/TRIGGER on public.scenario_attempts.';
    END IF;
    RAISE NOTICE 'V10 PASSED: service_role lacks DELETE, TRUNCATE, REFERENCES, TRIGGER on scenario_attempts.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V11: service_role lacks TRUNCATE, REFERENCES, TRIGGER on
--      scenario_decisions (DELETE/UPDATE already checked in V9).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('service_role', 'public.scenario_decisions', 'TRUNCATE')
       OR has_table_privilege('service_role', 'public.scenario_decisions', 'REFERENCES')
       OR has_table_privilege('service_role', 'public.scenario_decisions', 'TRIGGER')
    THEN
        RAISE EXCEPTION 'V11 FAILED: service_role unexpectedly has TRUNCATE/REFERENCES/TRIGGER on public.scenario_decisions.';
    END IF;
    RAISE NOTICE 'V11 PASSED: service_role lacks TRUNCATE, REFERENCES, TRIGGER on scenario_decisions.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V12: anon and authenticated cannot execute any of the four RPCs.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_function_privilege('anon', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)', 'EXECUTE')
       OR has_function_privilege('authenticated', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)', 'EXECUTE')
       OR has_function_privilege('anon', 'public.get_scenario_attempt_v1(text,uuid)', 'EXECUTE')
       OR has_function_privilege('authenticated', 'public.get_scenario_attempt_v1(text,uuid)', 'EXECUTE')
       OR has_function_privilege('anon', 'public.submit_scenario_decision_v1(text,uuid,uuid,integer,text,text,text,jsonb,jsonb,boolean,text,text,jsonb)', 'EXECUTE')
       OR has_function_privilege('authenticated', 'public.submit_scenario_decision_v1(text,uuid,uuid,integer,text,text,text,jsonb,jsonb,boolean,text,text,jsonb)', 'EXECUTE')
       OR has_function_privilege('anon', 'public.abandon_scenario_attempt_v1(text,uuid)', 'EXECUTE')
       OR has_function_privilege('authenticated', 'public.abandon_scenario_attempt_v1(text,uuid)', 'EXECUTE')
    THEN
        RAISE EXCEPTION 'V12 FAILED: anon or authenticated can EXECUTE one of the four scenario-attempt RPCs.';
    END IF;
    RAISE NOTICE 'V12 PASSED: anon and authenticated cannot execute any of the four RPCs.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V13: service_role can execute all four RPCs.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT has_function_privilege('service_role', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)', 'EXECUTE')
       OR NOT has_function_privilege('service_role', 'public.get_scenario_attempt_v1(text,uuid)', 'EXECUTE')
       OR NOT has_function_privilege('service_role', 'public.submit_scenario_decision_v1(text,uuid,uuid,integer,text,text,text,jsonb,jsonb,boolean,text,text,jsonb)', 'EXECUTE')
       OR NOT has_function_privilege('service_role', 'public.abandon_scenario_attempt_v1(text,uuid)', 'EXECUTE')
    THEN
        RAISE EXCEPTION 'V13 FAILED: service_role cannot execute one of the four scenario-attempt RPCs.';
    END IF;
    RAISE NOTICE 'V13 PASSED: service_role can execute all four RPCs.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V14: all four RPCs remain SECURITY INVOKER (checked against their exact,
--      to_regprocedure-resolved OIDs).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_oids regprocedure[] := ARRAY[
        to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)'),
        to_regprocedure('public.get_scenario_attempt_v1(text,uuid)'),
        to_regprocedure('public.submit_scenario_decision_v1(text,uuid,uuid,integer,text,text,text,jsonb,jsonb,boolean,text,text,jsonb)'),
        to_regprocedure('public.abandon_scenario_attempt_v1(text,uuid)')
    ];
    v_oid regprocedure;
    v_prosecdef boolean;
    v_inspected int := 0;
BEGIN
    FOREACH v_oid IN ARRAY v_oids LOOP
        IF v_oid IS NULL THEN
            RAISE EXCEPTION 'V14 FAILED: one of the four RPC signatures does not resolve to an exact function OID.';
        END IF;
        SELECT p.prosecdef INTO v_prosecdef FROM pg_proc p WHERE p.oid = v_oid::oid;
        IF v_prosecdef THEN
            RAISE EXCEPTION 'V14 FAILED: function with oid % is SECURITY DEFINER, expected SECURITY INVOKER.', v_oid;
        END IF;
        v_inspected := v_inspected + 1;
    END LOOP;

    IF v_inspected <> 4 THEN
        RAISE EXCEPTION 'V14 FAILED: expected exactly 4 RPCs inspected, got %.', v_inspected;
    END IF;

    RAISE NOTICE 'V14 PASSED: all four RPCs remain SECURITY INVOKER.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V15: all six relevant functions (four RPCs plus both trigger functions)
--      retain search_path = public, pg_catalog.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_oids regprocedure[] := ARRAY[
        to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)'),
        to_regprocedure('public.get_scenario_attempt_v1(text,uuid)'),
        to_regprocedure('public.submit_scenario_decision_v1(text,uuid,uuid,integer,text,text,text,jsonb,jsonb,boolean,text,text,jsonb)'),
        to_regprocedure('public.abandon_scenario_attempt_v1(text,uuid)'),
        to_regprocedure('public.guard_scenario_attempt_mutation_v1()'),
        to_regprocedure('public.guard_scenario_decision_immutability_v1()')
    ];
    v_oid regprocedure;
    v_config text[];
    v_normalized text;
    v_inspected int := 0;
BEGIN
    FOREACH v_oid IN ARRAY v_oids LOOP
        IF v_oid IS NULL THEN
            RAISE EXCEPTION 'V15 FAILED: one of the six expected function OIDs is null.';
        END IF;

        SELECT p.proconfig INTO v_config FROM pg_proc p WHERE p.oid = v_oid::oid;
        v_normalized := regexp_replace(array_to_string(coalesce(v_config, ARRAY[]::text[]), ';'), '\s+', '', 'g');
        IF v_normalized NOT LIKE '%search_path=public,pg_catalog%' THEN
            RAISE EXCEPTION 'V15 FAILED: function with oid % does not have SET search_path = public, pg_catalog (proconfig=%).', v_oid, v_config;
        END IF;
        v_inspected := v_inspected + 1;
    END LOOP;

    IF v_inspected <> 6 THEN
        RAISE EXCEPTION 'V15 FAILED: expected exactly 6 functions inspected for search_path, got %.', v_inspected;
    END IF;

    RAISE NOTICE 'V15 PASSED: all six functions retain search_path = public, pg_catalog.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V16: both triggers remain installed, enabled, and fire row-level BEFORE
--      INSERT, UPDATE, and DELETE. Validate the trigger event contract through
--      pg_trigger.tgtype rather than pg_get_triggerdef() text because PostgreSQL
--      canonicalizes event order as INSERT OR DELETE OR UPDATE.
--      tgtype = 31 means ROW(1) + BEFORE(2) + INSERT(4) + DELETE(8) + UPDATE(16),
--      with no TRUNCATE or INSTEAD OF bits.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_row record;
BEGIN
    SELECT t.tgenabled, t.tgtype, pg_get_triggerdef(t.oid) AS def INTO v_row
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = 'scenario_attempts'
    AND t.tgname = 'trg_guard_scenario_attempt_mutation' AND NOT t.tgisinternal;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'V16 FAILED: trg_guard_scenario_attempt_mutation does not exist.';
    END IF;
    IF v_row.tgenabled = 'D' THEN
        RAISE EXCEPTION 'V16 FAILED: trg_guard_scenario_attempt_mutation exists but is disabled.';
    END IF;
    IF v_row.tgtype <> 31 THEN
        RAISE EXCEPTION 'V16 FAILED: trg_guard_scenario_attempt_mutation firing contract unexpected (tgtype=%): %',
            v_row.tgtype, v_row.def;
    END IF;

    SELECT t.tgenabled, t.tgtype, pg_get_triggerdef(t.oid) AS def INTO v_row
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = 'scenario_decisions'
    AND t.tgname = 'trg_guard_scenario_decision_immutability' AND NOT t.tgisinternal;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'V16 FAILED: trg_guard_scenario_decision_immutability does not exist.';
    END IF;
    IF v_row.tgenabled = 'D' THEN
        RAISE EXCEPTION 'V16 FAILED: trg_guard_scenario_decision_immutability exists but is disabled.';
    END IF;
    IF v_row.tgtype <> 31 THEN
        RAISE EXCEPTION 'V16 FAILED: trg_guard_scenario_decision_immutability firing contract unexpected (tgtype=%): %',
            v_row.tgtype, v_row.def;
    END IF;

    RAISE NOTICE 'V16 PASSED: both triggers are installed, enabled, and fire row-level BEFORE INSERT, UPDATE, and DELETE.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V17 (SIM-PERSIST-04F): get_scenario_attempt_v1's installed source text
--      contains a FOR SHARE clause on its combined (id, owner) lookup of
--      scenario_attempts, proving the consistent-read locking contract is
--      actually installed. Checked against the function's exact,
--      to_regprocedure-resolved OID (pg_get_functiondef), not merely by
--      proname -- and explicitly proves FOR SHARE is used rather than
--      FOR KEY SHARE, which would NOT conflict with the ordinary non-key
--      UPDATEs performed by submit_scenario_decision_v1/
--      abandon_scenario_attempt_v1 and so would not close the read-skew
--      window this correction exists to close. A real two-session exercise
--      of the resulting blocking behavior belongs in the project's upcoming
--      throwaway-database concurrency test gate (see header note above);
--      this check only proves the lock clause itself is present in the
--      installed function body.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_oid         regprocedure := to_regprocedure('public.get_scenario_attempt_v1(text,uuid)');
    v_def         text;
    v_executable  text;
BEGIN
    IF v_oid IS NULL THEN
        RAISE EXCEPTION 'V17 FAILED: get_scenario_attempt_v1(text,uuid) does not resolve to an exact function OID.';
    END IF;

    SELECT pg_get_functiondef(v_oid::oid) INTO v_def;

    IF v_def IS NULL THEN
        RAISE EXCEPTION 'V17 FAILED: pg_get_functiondef returned no source for get_scenario_attempt_v1.';
    END IF;

    -- pg_get_functiondef includes PL/pgSQL comments. Strip line comments
    -- before inspecting executable locking clauses so documentation that
    -- mentions FOR KEY SHARE cannot create a false failure.
    v_executable := regexp_replace(v_def, E'--[^\\n\\r]*', '', 'g');

    IF v_executable !~* 'FOR[[:space:]]+SHARE' THEN
        RAISE EXCEPTION 'V17 FAILED: get_scenario_attempt_v1 executable source does not contain a FOR SHARE clause.';
    END IF;
    IF v_executable ~* 'FOR[[:space:]]+KEY[[:space:]]+SHARE' THEN
        RAISE EXCEPTION 'V17 FAILED: get_scenario_attempt_v1 executable source uses FOR KEY SHARE, which does not conflict with an ordinary non-key UPDATE.';
    END IF;

    RAISE NOTICE 'V17 PASSED: get_scenario_attempt_v1 executable source locks its combined (id, owner) lookup with FOR SHARE (not FOR KEY SHARE).';
END;
$$;


-- =============================================================================
-- V18-V62: row-level behavior exercised inside a single rolled-back
-- transaction. All fixture rows use a 'v68-verify-' simulation_id prefix.
-- Each expected-failure check accepts only an exception whose message (or,
-- for a raw unique-constraint violation, SQLSTATE condition) proves that
-- check's exact failure mode; any other exception is re-raised rather than
-- counted as a pass.
--
-- SIM-PERSIST-04C note on fixture shape: because the migration now validates
-- snapshot IDENTITY (simulationId, version, canonicalContentSha256,
-- engineVersion) and LIFECYCLE (currentSceneId, isComplete, terminalResult)
-- fields on every serialized-state payload it accepts, every fixture jsonb
-- object below carries all seven fields consistently -- not just the
-- previously-sufficient ad hoc {"projectHealth": N} shape.
--
-- SIM-PERSIST-04E additions (V21-V26, V39-V41, and the V40->V52 fix):
--   * V21-V26 prove start_or_resume_scenario_attempt_v1 now pins
--     p_initial_serialized_state.simulationId/.version EXACTLY to the
--     fetched scenarios.simulation_id / scenario_versions.version row
--     (mismatched and whitespace-padded values are both rejected), and
--     that a non-string simulationId/currentSceneId is rejected by a JSON
--     TYPE check before any ->> textual comparison is even attempted.
--   * V39-V41 prove submit_scenario_decision_v1 applies the identical
--     JSON-type-before-text-comparison discipline to state_before/
--     state_after (non-string simulationId, non-string currentSceneId),
--     and that a terminal decision whose p_terminal_result_snapshot.
--     endingId disagrees with p_terminal_ending_id is rejected with the
--     focused terminal_ending_mismatch exception.
--   * The original V40 check (now V52) supplies a submission that is fully
--     valid through scalar and snapshot validation, so it actually reaches
--     and exercises the locked attempt-status check it claims to prove,
--     instead of failing one stage earlier on invalid_expected_scene_id /
--     state_lifecycle_mismatch.
--
-- SIM-PERSIST-04F additions (V37, V45):
--   * V37 proves a terminal decision's state_after is rejected with
--     state_lifecycle_mismatch when its currentSceneId key is missing
--     entirely, not merely when it is present with the wrong value/type --
--     jsonb_typeof(p_state_after -> 'currentSceneId') = 'null' now requires
--     the key to be EXPLICITLY present as a JSON null.
--   * V45 proves that a retry sharing the same idempotency_key AND the same
--     request_fingerprint as an already-committed decision, but with a
--     DIFFERENT selected_option_id, is rejected with idempotency_key_conflict
--     rather than being treated as a safe replay -- proving the request-
--     field binding this correction adds, not merely the pre-existing
--     fingerprint comparison.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    v_scenario_a_id       uuid;
    v_scenario_b_id       uuid;
    v_draft_a_id          uuid;
    v_draft_b_id          uuid;
    v_hash_a              text := repeat('a', 64);
    v_hash_b              text := repeat('b', 64);
    v_engine_version      text := '1.0.0';
    v_email               text := 'v68-verify-learner@example.com';
    v_other_email         text := 'v68-verify-other-learner@example.com';

    v_attempt_a_id        uuid;
    v_attempt_a_id_2      uuid;
    v_created             boolean;
    v_status               text;
    v_current_scene        text;
    v_next_seq             integer;
    v_state                jsonb;
    v_completed_at         timestamptz;
    v_terminal_ending      text;
    v_terminal_snapshot    jsonb;

    v_decision_id          uuid;
    v_decision_id_2        uuid;
    v_idempotent_replay     boolean;

    v_idem_key_1            uuid := gen_random_uuid();
    v_idem_key_2            uuid := gen_random_uuid();
    v_fp_1                  text := repeat('1', 64);
    v_fp_1_conflicting      text := repeat('2', 64);
    v_fp_2                  text := repeat('3', 64);

    -- SIM-PERSIST-04C: every fixture snapshot carries the full
    -- IDENTITY (simulationId, version, canonicalContentSha256,
    -- engineVersion) and LIFECYCLE (currentSceneId, isComplete,
    -- terminalResult) shape the migration's RPCs now validate.
    v_terminal_payload      jsonb := jsonb_build_object('endingId', 'ending_distinction', 'scoreBand', 'distinction');
    v_state_initial         jsonb;
    v_state_after_1         jsonb;
    v_state_after_2         jsonb;
    v_state_after_3         jsonb;
    v_state_initial_b       jsonb;
    v_state_after_1_b       jsonb;

    v_attempt_b_id          uuid;
    v_attempt_b_id_2        uuid;
    v_abandoned_at          timestamptz;
    v_direct_insert_id      uuid;

    v_caught                boolean;
    v_decision_count        int;
    v_decisions_json         jsonb;
BEGIN
    ----------------------------------------------------------------------
    -- V18: fixture -- scenario A + one published version.
    ----------------------------------------------------------------------
    INSERT INTO public.scenarios (simulation_id, certification_exam_name, title)
    VALUES ('v68-verify-sim-a', 'Business Analyst', 'V68 Verification Scenario A')
    RETURNING id INTO v_scenario_a_id;

    INSERT INTO public.scenario_versions (scenario_id, version, schema_version, engine_version, source_repository_path)
    VALUES (v_scenario_a_id, '1.0.0', '1.0.0', v_engine_version, 'scenario_content/business_analyst/v68-verify-sim-a/1.0.0/scenario.json')
    RETURNING id INTO v_draft_a_id;

    PERFORM public.publish_scenario_version_v1(
        v_draft_a_id,
        jsonb_build_object('simulationId', 'v68-verify-sim-a', 'version', '1.0.0', 'schemaVersion', '1.0.0'),
        v_hash_a
    );

    -- Fixture snapshots for scenario A, now carrying full IDENTITY/LIFECYCLE
    -- shape (SIM-PERSIST-04C).
    v_state_initial := jsonb_build_object(
        'simulationId', 'v68-verify-sim-a', 'version', '1.0.0',
        'canonicalContentSha256', v_hash_a, 'engineVersion', v_engine_version,
        'currentSceneId', 'scene-start', 'isComplete', false, 'terminalResult', NULL,
        'projectHealth', 100
    );
    v_state_after_1 := jsonb_build_object(
        'simulationId', 'v68-verify-sim-a', 'version', '1.0.0',
        'canonicalContentSha256', v_hash_a, 'engineVersion', v_engine_version,
        'currentSceneId', 'scene-2', 'isComplete', false, 'terminalResult', NULL,
        'projectHealth', 90
    );
    v_state_after_2 := jsonb_build_object(
        'simulationId', 'v68-verify-sim-a', 'version', '1.0.0',
        'canonicalContentSha256', v_hash_a, 'engineVersion', v_engine_version,
        'currentSceneId', NULL, 'isComplete', true, 'terminalResult', v_terminal_payload,
        'projectHealth', 85
    );
    v_state_after_3 := jsonb_build_object(
        'simulationId', 'v68-verify-sim-a', 'version', '1.0.0',
        'canonicalContentSha256', v_hash_a, 'engineVersion', v_engine_version,
        'currentSceneId', 'scene-3', 'isComplete', false, 'terminalResult', NULL,
        'projectHealth', 80
    );

    RAISE NOTICE 'V18 PASSED: fixture scenario A + published version created.';

    ----------------------------------------------------------------------
    -- V19: a direct, un-guarded INSERT into scenario_attempts (bypassing
    -- start_or_resume_scenario_attempt_v1 entirely, so the transaction-
    -- local certbound.scenario_attempt_insert_guard has never been set) is
    -- rejected by trg_guard_scenario_attempt_mutation's BEFORE INSERT
    -- firing (SIM-PERSIST-04C). Only an exception whose message starts
    -- with "attempt_insert_guard_violation:" is accepted.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        INSERT INTO public.scenario_attempts (
            user_email, scenario_id, scenario_version_id, status,
            current_scene_id, next_sequence_number, serialized_engine_state,
            scenario_content_sha256, engine_version
        )
        VALUES (
            v_email, v_scenario_a_id, v_draft_a_id, 'in_progress',
            'scene-start', 1, v_state_initial, v_hash_a, v_engine_version
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'attempt_insert_guard_violation:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V19 FAILED: a direct, un-guarded INSERT into scenario_attempts did not raise attempt_insert_guard_violation.';
    END IF;
    RAISE NOTICE 'V19 PASSED: direct un-guarded scenario_attempts INSERT was rejected.';

    ----------------------------------------------------------------------
    -- V20: start_or_resume_scenario_attempt_v1 rejects a mismatched
    -- p_initial_serialized_state.engineVersion with the focused
    -- invalid_initial_state_identity exception, WITHOUT creating any row
    -- (no attempt for scenario A exists yet, so this call would otherwise
    -- take the "create new" branch).
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.start_or_resume_scenario_attempt_v1(
            v_email, v_draft_a_id, 'scene-start',
            jsonb_set(v_state_initial, '{engineVersion}', '"wrong-engine-version"'::jsonb),
            v_engine_version, v_hash_a
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'invalid_initial_state_identity:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V20 FAILED: a mismatched initial_serialized_state.engineVersion did not raise invalid_initial_state_identity.';
    END IF;
    RAISE NOTICE 'V20 PASSED: mismatched initial snapshot engineVersion was rejected before any attempt was created.';

    ----------------------------------------------------------------------
    -- V21 (SIM-PERSIST-04E): start_or_resume_scenario_attempt_v1 rejects a
    -- mismatched p_initial_serialized_state.simulationId with the focused
    -- invalid_initial_state_identity exception -- proving simulationId is
    -- now pinned EXACTLY to the fetched scenarios.simulation_id row, not
    -- merely checked for being "some" normalized non-empty string.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.start_or_resume_scenario_attempt_v1(
            v_email, v_draft_a_id, 'scene-start',
            jsonb_set(v_state_initial, '{simulationId}', '"v68-verify-sim-wrong"'::jsonb),
            v_engine_version, v_hash_a
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'invalid_initial_state_identity:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V21 FAILED: a mismatched initial_serialized_state.simulationId did not raise invalid_initial_state_identity.';
    END IF;
    RAISE NOTICE 'V21 PASSED: mismatched initial snapshot simulationId was rejected (pinned to scenarios.simulation_id).';

    ----------------------------------------------------------------------
    -- V22 (SIM-PERSIST-04E): start_or_resume_scenario_attempt_v1 rejects a
    -- mismatched p_initial_serialized_state.version with the focused
    -- invalid_initial_state_identity exception -- proving version is now
    -- pinned EXACTLY to the fetched scenario_versions.version row.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.start_or_resume_scenario_attempt_v1(
            v_email, v_draft_a_id, 'scene-start',
            jsonb_set(v_state_initial, '{version}', '"9.9.9"'::jsonb),
            v_engine_version, v_hash_a
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'invalid_initial_state_identity:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V22 FAILED: a mismatched initial_serialized_state.version did not raise invalid_initial_state_identity.';
    END IF;
    RAISE NOTICE 'V22 PASSED: mismatched initial snapshot version was rejected (pinned to scenario_versions.version).';

    ----------------------------------------------------------------------
    -- V23 (SIM-PERSIST-04E): a whitespace-padded, otherwise-correct
    -- p_initial_serialized_state.simulationId is rejected -- it must
    -- already equal BTRIM(itself), not merely match after trimming.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.start_or_resume_scenario_attempt_v1(
            v_email, v_draft_a_id, 'scene-start',
            jsonb_set(v_state_initial, '{simulationId}', '"  v68-verify-sim-a  "'::jsonb),
            v_engine_version, v_hash_a
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'invalid_initial_state_identity:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V23 FAILED: a whitespace-padded initial_serialized_state.simulationId did not raise invalid_initial_state_identity.';
    END IF;
    RAISE NOTICE 'V23 PASSED: whitespace-padded initial snapshot simulationId was rejected.';

    ----------------------------------------------------------------------
    -- V24 (SIM-PERSIST-04E): a whitespace-padded, otherwise-correct
    -- p_initial_serialized_state.version is rejected for the identical
    -- reason as V23.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.start_or_resume_scenario_attempt_v1(
            v_email, v_draft_a_id, 'scene-start',
            jsonb_set(v_state_initial, '{version}', '"  1.0.0  "'::jsonb),
            v_engine_version, v_hash_a
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'invalid_initial_state_identity:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V24 FAILED: a whitespace-padded initial_serialized_state.version did not raise invalid_initial_state_identity.';
    END IF;
    RAISE NOTICE 'V24 PASSED: whitespace-padded initial snapshot version was rejected.';

    ----------------------------------------------------------------------
    -- V25 (SIM-PERSIST-04E): a numeric (non-string) simulationId is
    -- rejected by the JSON TYPE check BEFORE any ->> textual comparison is
    -- even attempted -- proving the type check runs first, not merely as
    -- an afterthought that a numeric-but-textually-matching value could
    -- bypass.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.start_or_resume_scenario_attempt_v1(
            v_email, v_draft_a_id, 'scene-start',
            jsonb_set(v_state_initial, '{simulationId}', '12345'::jsonb),
            v_engine_version, v_hash_a
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'invalid_initial_state_identity:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V25 FAILED: a numeric (non-string) initial_serialized_state.simulationId did not raise invalid_initial_state_identity.';
    END IF;
    RAISE NOTICE 'V25 PASSED: a numeric (non-string) initial snapshot simulationId was rejected by the JSON type check.';

    ----------------------------------------------------------------------
    -- V26 (SIM-PERSIST-04E): a numeric (non-string) currentSceneId on the
    -- initial snapshot is rejected by the JSON TYPE check with the
    -- focused invalid_initial_state_lifecycle exception.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.start_or_resume_scenario_attempt_v1(
            v_email, v_draft_a_id, 'scene-start',
            jsonb_set(v_state_initial, '{currentSceneId}', '999'::jsonb),
            v_engine_version, v_hash_a
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'invalid_initial_state_lifecycle:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V26 FAILED: a numeric (non-string) initial_serialized_state.currentSceneId did not raise invalid_initial_state_lifecycle.';
    END IF;
    RAISE NOTICE 'V26 PASSED: a numeric (non-string) initial snapshot currentSceneId was rejected by the JSON type check.';

    ----------------------------------------------------------------------
    -- V27: start_or_resume_scenario_attempt_v1 rejects
    -- p_initial_serialized_state.isComplete = true with the focused
    -- invalid_initial_state_lifecycle exception, again without creating
    -- any row.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.start_or_resume_scenario_attempt_v1(
            v_email, v_draft_a_id, 'scene-start',
            jsonb_set(v_state_initial, '{isComplete}', 'true'::jsonb),
            v_engine_version, v_hash_a
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'invalid_initial_state_lifecycle:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V27 FAILED: initial_serialized_state.isComplete=true did not raise invalid_initial_state_lifecycle.';
    END IF;
    RAISE NOTICE 'V27 PASSED: initial snapshot isComplete=true was rejected before any attempt was created.';

    ----------------------------------------------------------------------
    -- V28: start_or_resume_scenario_attempt_v1 creates a brand-new attempt
    -- once the caller supplies a consistent initial snapshot.
    ----------------------------------------------------------------------
    SELECT attempt_id, created, status, current_scene_id, next_sequence_number, serialized_engine_state
    INTO   v_attempt_a_id, v_created, v_status, v_current_scene, v_next_seq, v_state
    FROM   public.start_or_resume_scenario_attempt_v1(
               v_email, v_draft_a_id, 'scene-start', v_state_initial, v_engine_version, v_hash_a
           );

    IF NOT v_created THEN
        RAISE EXCEPTION 'V28 FAILED: first start_or_resume call did not report created=true.';
    END IF;
    IF v_status IS DISTINCT FROM 'in_progress' OR v_current_scene IS DISTINCT FROM 'scene-start'
       OR v_next_seq IS DISTINCT FROM 1 OR v_state IS DISTINCT FROM v_state_initial
    THEN
        RAISE EXCEPTION 'V28 FAILED: newly created attempt has unexpected initial fields (status=%, scene=%, seq=%, state=%).', v_status, v_current_scene, v_next_seq, v_state;
    END IF;
    RAISE NOTICE 'V28 PASSED: start_or_resume_scenario_attempt_v1 created a new in_progress attempt.';

    ----------------------------------------------------------------------
    -- V29: a second, concurrent-style start_or_resume call for the same
    -- (learner, scenario_version_id) RESUMES the same attempt rather than
    -- creating a duplicate or raising a false failure. (The resume branch
    -- never re-validates initial_serialized_state -- it is only used on
    -- true creation -- so this call intentionally reuses v_state_initial
    -- unchanged.)
    ----------------------------------------------------------------------
    SELECT attempt_id, created
    INTO   v_attempt_a_id_2, v_created
    FROM   public.start_or_resume_scenario_attempt_v1(
               v_email, v_draft_a_id, 'scene-start', v_state_initial, v_engine_version, v_hash_a
           );

    IF v_created THEN
        RAISE EXCEPTION 'V29 FAILED: duplicate start reported created=true instead of resuming.';
    END IF;
    IF v_attempt_a_id_2 IS DISTINCT FROM v_attempt_a_id THEN
        RAISE EXCEPTION 'V29 FAILED: duplicate start returned a different attempt id (% vs %).', v_attempt_a_id_2, v_attempt_a_id;
    END IF;
    RAISE NOTICE 'V29 PASSED: concurrent-style duplicate start resumed the same attempt without a false failure.';

    ----------------------------------------------------------------------
    -- V30: a direct, un-guarded INSERT into scenario_decisions (bypassing
    -- submit_scenario_decision_v1 entirely, so certbound.scenario_decision_
    -- insert_guard has never been set) is rejected by trg_guard_scenario_
    -- decision_immutability's BEFORE INSERT firing (SIM-PERSIST-04C). Only
    -- an exception whose message starts with
    -- "decision_insert_guard_violation:" is accepted.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        INSERT INTO public.scenario_decisions (
            attempt_id, sequence_number, idempotency_key, request_fingerprint,
            expected_scene_id, selected_option_id, state_before, state_after,
            resulting_scene_id, is_terminal
        )
        VALUES (
            v_attempt_a_id, 1, gen_random_uuid(), repeat('0', 64),
            'scene-start', 'opt-a', v_state_initial, v_state_after_1,
            'scene-2', false
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'decision_insert_guard_violation:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V30 FAILED: a direct, un-guarded INSERT into scenario_decisions did not raise decision_insert_guard_violation.';
    END IF;
    RAISE NOTICE 'V30 PASSED: direct un-guarded scenario_decisions INSERT was rejected.';

    ----------------------------------------------------------------------
    -- V31: a direct, un-guarded UPDATE of the still-in_progress attempt is
    -- rejected -- the mutation guard has never been set for this row yet
    -- at this point (only INSERT/SELECT have touched it so far). Only an
    -- exception whose message contains "mutation guard not set" is
    -- accepted.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenario_attempts SET current_scene_id = 'tampered' WHERE id = v_attempt_a_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%mutation guard not set%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V31 FAILED: an un-guarded direct UPDATE of an in_progress attempt did not raise a "mutation guard not set" error.';
    END IF;
    RAISE NOTICE 'V31 PASSED: direct UPDATE outside any RPC guard was rejected.';

    ----------------------------------------------------------------------
    -- V32: submit_scenario_decision_v1 rejects state_identity_mismatch
    -- when state_before and state_after disagree on an immutable identity
    -- field (simulationId here), before ever locking the attempt row --
    -- no decision row is created.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('a', 64),
            v_state_initial, jsonb_set(v_state_after_1, '{simulationId}', '"different-sim"'::jsonb),
            false, 'scene-2', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'state_identity_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V32 FAILED: mismatched state_before/state_after simulationId did not raise state_identity_mismatch.';
    END IF;
    RAISE NOTICE 'V32 PASSED: state_before/state_after identity mismatch was rejected.';

    ----------------------------------------------------------------------
    -- V33: submit_scenario_decision_v1 rejects state_lifecycle_mismatch
    -- when state_before.currentSceneId does not match p_expected_scene_id.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('b', 64),
            jsonb_set(v_state_initial, '{currentSceneId}', '"not-scene-start"'::jsonb), v_state_after_1,
            false, 'scene-2', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'state_lifecycle_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V33 FAILED: state_before.currentSceneId disagreeing with p_expected_scene_id did not raise state_lifecycle_mismatch.';
    END IF;
    RAISE NOTICE 'V33 PASSED: state_before.currentSceneId vs p_expected_scene_id mismatch was rejected.';

    ----------------------------------------------------------------------
    -- V34: submit_scenario_decision_v1 rejects state_lifecycle_mismatch
    -- when state_before.isComplete is true (a decision can never be
    -- submitted "before" an already-complete state).
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('c', 64),
            jsonb_set(v_state_initial, '{isComplete}', 'true'::jsonb), v_state_after_1,
            false, 'scene-2', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'state_lifecycle_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V34 FAILED: state_before.isComplete=true did not raise state_lifecycle_mismatch.';
    END IF;
    RAISE NOTICE 'V34 PASSED: state_before.isComplete=true was rejected.';

    ----------------------------------------------------------------------
    -- V35: submit_scenario_decision_v1 rejects state_lifecycle_mismatch
    -- for a non-terminal decision whose state_after.currentSceneId does
    -- not match p_resulting_scene_id.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('d', 64),
            v_state_initial, jsonb_set(v_state_after_1, '{currentSceneId}', '"scene-9"'::jsonb),
            false, 'scene-2', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'state_lifecycle_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V35 FAILED: non-terminal state_after.currentSceneId disagreeing with p_resulting_scene_id did not raise state_lifecycle_mismatch.';
    END IF;
    RAISE NOTICE 'V35 PASSED: non-terminal state_after.currentSceneId vs p_resulting_scene_id mismatch was rejected.';

    ----------------------------------------------------------------------
    -- V36: submit_scenario_decision_v1 rejects state_lifecycle_mismatch
    -- for a terminal decision whose state_after.isComplete is false.
    -- p_terminal_ending_id ('ending_distinction') is deliberately kept
    -- EQUAL to v_terminal_payload.endingId here, so the SIM-PERSIST-04E
    -- terminal_ending_mismatch check (which runs earlier, during scalar
    -- validation) passes and the real isComplete=false check under test is
    -- what actually fires.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('e', 64),
            v_state_initial, jsonb_set(v_state_after_2, '{isComplete}', 'false'::jsonb),
            true, NULL, 'ending_distinction', v_terminal_payload
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'state_lifecycle_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V36 FAILED: terminal state_after.isComplete=false did not raise state_lifecycle_mismatch.';
    END IF;
    RAISE NOTICE 'V36 PASSED: terminal state_after.isComplete=false was rejected.';

    ----------------------------------------------------------------------
    -- V37 (SIM-PERSIST-04F): submit_scenario_decision_v1 rejects
    -- state_lifecycle_mismatch for a terminal decision whose state_after
    -- omits the currentSceneId key ENTIRELY, not merely when it is present
    -- with the wrong value/type -- proving
    -- jsonb_typeof(p_state_after -> 'currentSceneId') = 'null' now requires
    -- the key to be EXPLICITLY present as a JSON null, rather than treating
    -- a missing key as equivalent to an explicit null. state_after is
    -- otherwise a fully valid terminal snapshot (v_state_after_2 with the
    -- currentSceneId key removed via the `-` jsonb operator), so only the
    -- missing-key defect is under test. p_terminal_ending_id
    -- ('ending_distinction') is deliberately kept EQUAL to
    -- v_terminal_payload.endingId so the earlier terminal_ending_mismatch
    -- scalar check passes and the missing-key check under test is what
    -- actually fires.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('9', 64),
            v_state_initial, (v_state_after_2 - 'currentSceneId'),
            true, NULL, 'ending_distinction', v_terminal_payload
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'state_lifecycle_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V37 FAILED: a terminal state_after with a missing currentSceneId key did not raise state_lifecycle_mismatch.';
    END IF;
    RAISE NOTICE 'V37 PASSED: a terminal state_after with a missing (not merely null-valued) currentSceneId key was rejected.';

    ----------------------------------------------------------------------
    -- V38: submit_scenario_decision_v1 rejects terminal_result_mismatch
    -- when state_after.terminalResult does not equal the separately
    -- supplied p_terminal_result_snapshot. p_terminal_ending_id
    -- ('a-different-ending') is deliberately kept EQUAL to this call's own
    -- p_terminal_result_snapshot.endingId, so the earlier
    -- terminal_ending_mismatch scalar check passes and the real
    -- terminal_result_mismatch check under test (state_after.terminalResult,
    -- still v_terminal_payload, vs this different p_terminal_result_snapshot)
    -- is what actually fires.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('f', 64),
            v_state_initial, v_state_after_2,
            true, NULL, 'a-different-ending', jsonb_build_object('endingId', 'a-different-ending')
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'terminal_result_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V38 FAILED: state_after.terminalResult disagreeing with p_terminal_result_snapshot did not raise terminal_result_mismatch.';
    END IF;
    RAISE NOTICE 'V38 PASSED: terminal_result_snapshot mismatch was rejected.';

    ----------------------------------------------------------------------
    -- V39 (SIM-PERSIST-04E): submit_scenario_decision_v1 rejects a numeric
    -- (non-string) simulationId in state_after with state_identity_mismatch
    -- -- proving the JSON TYPE check on decision identity fields runs
    -- BEFORE any ->> textual comparison. state_before is the correct,
    -- consistent v_state_initial so only the type defect is under test.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('1', 64),
            v_state_initial, jsonb_set(v_state_after_1, '{simulationId}', '999'::jsonb),
            false, 'scene-2', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'state_identity_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V39 FAILED: a numeric (non-string) state_after.simulationId did not raise state_identity_mismatch.';
    END IF;
    RAISE NOTICE 'V39 PASSED: a numeric (non-string) state_after.simulationId was rejected by the JSON type check.';

    ----------------------------------------------------------------------
    -- V40 (SIM-PERSIST-04E): submit_scenario_decision_v1 rejects a numeric
    -- (non-string) currentSceneId in state_before with
    -- state_lifecycle_mismatch -- proving the same JSON-type-before-text
    -- discipline applies to state_before.currentSceneId.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('2', 64),
            jsonb_set(v_state_initial, '{currentSceneId}', '999'::jsonb), v_state_after_1,
            false, 'scene-2', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'state_lifecycle_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V40 FAILED: a numeric (non-string) state_before.currentSceneId did not raise state_lifecycle_mismatch.';
    END IF;
    RAISE NOTICE 'V40 PASSED: a numeric (non-string) state_before.currentSceneId was rejected by the JSON type check.';

    ----------------------------------------------------------------------
    -- V41 (SIM-PERSIST-04E): submit_scenario_decision_v1 rejects
    -- terminal_ending_mismatch when a terminal decision's
    -- p_terminal_result_snapshot.endingId disagrees with the separately
    -- supplied p_terminal_ending_id -- state_after.terminalResult is kept
    -- EQUAL to p_terminal_result_snapshot (so terminal_result_mismatch,
    -- proven separately by V38, does not fire here) and every other
    -- scalar/snapshot field is self-consistent, isolating the contradictory
    -- ending identity as the only defect under test.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 1, 'scene-start', 'opt-a', repeat('3', 64),
            v_state_initial,
            jsonb_set(v_state_after_2, '{terminalResult}', jsonb_build_object('endingId', 'ending_mismatch', 'scoreBand', 'distinction')),
            true, NULL, 'ending_x', jsonb_build_object('endingId', 'ending_mismatch', 'scoreBand', 'distinction')
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'terminal_ending_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V41 FAILED: a terminal_result_snapshot.endingId disagreeing with p_terminal_ending_id did not raise terminal_ending_mismatch.';
    END IF;
    RAISE NOTICE 'V41 PASSED: contradictory terminal ending identities (p_terminal_ending_id vs terminal_result_snapshot.endingId) were rejected.';

    ----------------------------------------------------------------------
    -- V42: submit a valid, non-terminal decision. The attempt advances to
    -- the resulting scene, next_sequence_number increments, and
    -- serialized_engine_state becomes the supplied state_after. None of
    -- the V19-V41 negative checks above altered the attempt's persisted
    -- state (still sequence 1 / scene-start / v_state_initial).
    ----------------------------------------------------------------------
    SELECT decision_id, idempotent_replay, attempt_status, current_scene_id, next_sequence_number, serialized_engine_state
    INTO   v_decision_id, v_idempotent_replay, v_status, v_current_scene, v_next_seq, v_state
    FROM   public.submit_scenario_decision_v1(
               v_email, v_attempt_a_id, v_idem_key_1, 1, 'scene-start', 'opt-a', v_fp_1,
               v_state_initial, v_state_after_1, false, 'scene-2', NULL, NULL
           );

    IF v_idempotent_replay THEN
        RAISE EXCEPTION 'V42 FAILED: a brand-new decision was incorrectly reported as an idempotent replay.';
    END IF;
    IF v_status IS DISTINCT FROM 'in_progress' OR v_current_scene IS DISTINCT FROM 'scene-2'
       OR v_next_seq IS DISTINCT FROM 2 OR v_state IS DISTINCT FROM v_state_after_1
    THEN
        RAISE EXCEPTION 'V42 FAILED: attempt state after a valid decision is unexpected (status=%, scene=%, seq=%, state=%).', v_status, v_current_scene, v_next_seq, v_state;
    END IF;
    RAISE NOTICE 'V42 PASSED: a valid non-terminal decision advanced the attempt correctly.';

    ----------------------------------------------------------------------
    -- V43: a safe idempotent retry -- same attempt, same idempotency key,
    -- same request fingerprint, and every other bound request field
    -- (SIM-PERSIST-04F: sequence, expected scene, selected option,
    -- state_before, state_after, resulting scene, is_terminal, terminal
    -- ending id) unchanged -- returns the original committed result
    -- (idempotent_replay = true, same decision_id) without inserting a
    -- second decision row.
    ----------------------------------------------------------------------
    SELECT count(*) INTO v_decision_count FROM public.scenario_decisions WHERE attempt_id = v_attempt_a_id;

    SELECT decision_id, idempotent_replay, current_scene_id
    INTO   v_decision_id_2, v_idempotent_replay, v_current_scene
    FROM   public.submit_scenario_decision_v1(
               v_email, v_attempt_a_id, v_idem_key_1, 1, 'scene-start', 'opt-a', v_fp_1,
               v_state_initial, v_state_after_1, false, 'scene-2', NULL, NULL
           );

    IF NOT v_idempotent_replay THEN
        RAISE EXCEPTION 'V43 FAILED: retry with the identical idempotency key and fingerprint was not reported as idempotent_replay.';
    END IF;
    IF v_decision_id_2 IS DISTINCT FROM v_decision_id THEN
        RAISE EXCEPTION 'V43 FAILED: idempotent retry returned a different decision_id (% vs %).', v_decision_id_2, v_decision_id;
    END IF;
    IF v_current_scene IS DISTINCT FROM 'scene-2' THEN
        RAISE EXCEPTION 'V43 FAILED: idempotent retry returned unexpected current_scene_id %.', v_current_scene;
    END IF;

    IF (SELECT count(*) FROM public.scenario_decisions WHERE attempt_id = v_attempt_a_id) <> v_decision_count THEN
        RAISE EXCEPTION 'V43 FAILED: idempotent retry changed the number of scenario_decisions rows for the attempt.';
    END IF;
    RAISE NOTICE 'V43 PASSED: safe idempotent retry returned the original result and inserted no second row.';

    ----------------------------------------------------------------------
    -- V44: conflicting reuse -- same attempt, same idempotency key, a
    -- DIFFERENT request fingerprint -- raises idempotency_key_conflict.
    -- Only an exception whose message contains "idempotency_key_conflict"
    -- is accepted.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, v_idem_key_1, 1, 'scene-start', 'opt-a', v_fp_1_conflicting,
            v_state_initial, v_state_after_1, false, 'scene-2', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'idempotency_key_conflict:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V44 FAILED: reusing the same idempotency key with a different fingerprint did not raise idempotency_key_conflict.';
    END IF;
    RAISE NOTICE 'V44 PASSED: conflicting idempotency-key reuse (different fingerprint) was rejected.';

    ----------------------------------------------------------------------
    -- V45 (SIM-PERSIST-04F): reuse of the SAME idempotency key with the
    -- SAME request fingerprint, but a DIFFERENT selected_option_id (all
    -- other fields, including the two snapshots, left internally valid and
    -- otherwise identical to decision 1's original request), is rejected
    -- with idempotency_key_conflict rather than being treated as a safe
    -- replay -- proving the retry-safety check is now bound to every
    -- stored request field, not merely to request_fingerprint matching.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, v_idem_key_1, 1, 'scene-start', 'opt-z', v_fp_1,
            v_state_initial, v_state_after_1, false, 'scene-2', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'idempotency_key_conflict:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V45 FAILED: reusing the same idempotency key and fingerprint with a different selected_option_id did not raise idempotency_key_conflict.';
    END IF;
    RAISE NOTICE 'V45 PASSED: a same-key, same-fingerprint retry with a different selected_option_id was rejected as a conflict, not treated as a safe replay.';

    ----------------------------------------------------------------------
    -- V46: wrong expected sequence number is rejected with
    -- sequence_mismatch. state_before/state_after are mutually
    -- self-consistent (so the SIM-PERSIST-04C snapshot checks pass) and
    -- match the ATTEMPT's real persisted scene ('scene-2') so only the
    -- deliberately-wrong sequence number (99) is under test.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 99, 'scene-2', 'opt-b', repeat('4', 64),
            v_state_after_1, v_state_after_3, false, 'scene-3', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'sequence_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V46 FAILED: a wrong expected sequence number did not raise sequence_mismatch.';
    END IF;
    RAISE NOTICE 'V46 PASSED: wrong expected sequence number was rejected.';

    ----------------------------------------------------------------------
    -- V47: wrong expected current scene is rejected with scene_mismatch.
    -- state_before.currentSceneId is deliberately set to the SAME (wrong)
    -- value as p_expected_scene_id so the SIM-PERSIST-04C snapshot check
    -- passes and the real scene_mismatch check (against the attempt's
    -- actually-persisted current_scene_id, 'scene-2') is what fires.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 2, 'wrong-scene', 'opt-b', repeat('5', 64),
            jsonb_set(v_state_after_1, '{currentSceneId}', '"wrong-scene"'::jsonb), v_state_after_3,
            false, 'scene-3', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'scene_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V47 FAILED: a wrong expected current scene did not raise scene_mismatch.';
    END IF;
    RAISE NOTICE 'V47 PASSED: wrong expected current scene was rejected.';

    ----------------------------------------------------------------------
    -- V48: a state_before that does not match the persisted
    -- serialized_engine_state is rejected with state_before_mismatch.
    -- Its currentSceneId/isComplete still agree with p_expected_scene_id
    -- (so the SIM-PERSIST-04C snapshot check passes) but its projectHealth
    -- payload differs from the real persisted value.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 2, 'scene-2', 'opt-b', repeat('6', 64),
            jsonb_set(v_state_after_1, '{projectHealth}', '999'::jsonb), v_state_after_3, false, 'scene-3', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'state_before_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V48 FAILED: a mismatched state_before did not raise state_before_mismatch.';
    END IF;
    RAISE NOTICE 'V48 PASSED: mismatched state_before was rejected.';

    ----------------------------------------------------------------------
    -- V49: wrong owner. get_scenario_attempt_v1 and
    -- submit_scenario_decision_v1 both raise the identical
    -- attempt_not_found for a real attempt id owned by a different
    -- learner -- never leaking that the id actually exists. The submit
    -- call's state_before/state_after are self-consistent (matching the
    -- real persisted scene) so ownership, not a snapshot check, is what
    -- is proven here.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.get_scenario_attempt_v1(v_other_email, v_attempt_a_id);
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'attempt_not_found:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V49 FAILED: get_scenario_attempt_v1 with the wrong owner did not raise attempt_not_found.';
    END IF;

    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_other_email, v_attempt_a_id, gen_random_uuid(), 2, 'scene-2', 'opt-b', repeat('7', 64),
            v_state_after_1, v_state_after_3, false, 'scene-3', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'attempt_not_found:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V49 FAILED: submit_scenario_decision_v1 with the wrong owner did not raise attempt_not_found.';
    END IF;
    RAISE NOTICE 'V49 PASSED: wrong-owner access is rejected identically to a not-found id on both RPCs.';

    ----------------------------------------------------------------------
    -- V50: a terminal decision completes the attempt atomically -- status
    -- becomes completed, current_scene_id becomes NULL, completed_at and
    -- the terminal snapshot are set, all in the same call as the decision.
    ----------------------------------------------------------------------
    SELECT decision_id, attempt_status, current_scene_id, next_sequence_number, completed_at, terminal_ending_id, terminal_result_snapshot
    INTO   v_decision_id_2, v_status, v_current_scene, v_next_seq, v_completed_at, v_terminal_ending, v_terminal_snapshot
    FROM   public.submit_scenario_decision_v1(
               v_email, v_attempt_a_id, v_idem_key_2, 2, 'scene-2', 'opt-final', v_fp_2,
               v_state_after_1, v_state_after_2, true, NULL, 'ending_distinction', v_terminal_payload
           );

    IF v_status IS DISTINCT FROM 'completed' OR v_current_scene IS NOT NULL
       OR v_next_seq IS DISTINCT FROM 3 OR v_completed_at IS NULL
       OR v_terminal_ending IS DISTINCT FROM 'ending_distinction' OR v_terminal_snapshot IS DISTINCT FROM v_terminal_payload
    THEN
        RAISE EXCEPTION 'V50 FAILED: terminal decision did not correctly complete the attempt (status=%, scene=%, seq=%, completed_at=%, ending=%).',
            v_status, v_current_scene, v_next_seq, v_completed_at, v_terminal_ending;
    END IF;
    RAISE NOTICE 'V50 PASSED: a terminal decision atomically completed the attempt.';

    ----------------------------------------------------------------------
    -- V51: SIM-PERSIST-04C STABLE IDEMPOTENT REPLAY. Retrying decision 1's
    -- exact original (idempotency_key, request_fingerprint) NOW -- after
    -- decision 2 (V50) has completed the attempt -- must still return
    -- decision 1's own ORIGINAL post-decision result (idempotent_replay=
    -- true, same decision_id, attempt_status='in_progress',
    -- current_scene_id='scene-2', next_sequence_number=2,
    -- serialized_engine_state=v_state_after_1, all terminal fields null),
    -- NEVER the attempt's current ('completed' / NULL scene / seq 3 /
    -- v_state_after_2) state.
    ----------------------------------------------------------------------
    SELECT decision_id, idempotent_replay, attempt_status, current_scene_id, next_sequence_number,
           serialized_engine_state, completed_at, terminal_ending_id, terminal_result_snapshot
    INTO   v_decision_id_2, v_idempotent_replay, v_status, v_current_scene, v_next_seq,
           v_state, v_completed_at, v_terminal_ending, v_terminal_snapshot
    FROM   public.submit_scenario_decision_v1(
               v_email, v_attempt_a_id, v_idem_key_1, 1, 'scene-start', 'opt-a', v_fp_1,
               v_state_initial, v_state_after_1, false, 'scene-2', NULL, NULL
           );

    IF NOT v_idempotent_replay THEN
        RAISE EXCEPTION 'V51 FAILED: late retry of decision 1 was not reported as idempotent_replay.';
    END IF;
    IF v_decision_id_2 IS DISTINCT FROM v_decision_id THEN
        RAISE EXCEPTION 'V51 FAILED: late retry of decision 1 returned a different decision_id (% vs %).', v_decision_id_2, v_decision_id;
    END IF;
    IF v_status IS DISTINCT FROM 'in_progress' OR v_current_scene IS DISTINCT FROM 'scene-2'
       OR v_next_seq IS DISTINCT FROM 2 OR v_state IS DISTINCT FROM v_state_after_1
       OR v_completed_at IS NOT NULL OR v_terminal_ending IS NOT NULL OR v_terminal_snapshot IS NOT NULL
    THEN
        RAISE EXCEPTION 'V51 FAILED: late retry of decision 1 returned the attempt''s CURRENT state instead of decision 1''s own original post-decision state (status=%, scene=%, seq=%, state=%, completed_at=%, ending=%, snapshot=%).',
            v_status, v_current_scene, v_next_seq, v_state, v_completed_at, v_terminal_ending, v_terminal_snapshot;
    END IF;
    RAISE NOTICE 'V51 PASSED: a late idempotent retry of an older, non-terminal decision returned its own original post-decision state, not the since-completed attempt''s current state.';

    ----------------------------------------------------------------------
    -- V52 (SIM-PERSIST-04E fix): a further decision submission against the
    -- now-completed attempt is rejected with attempt_not_in_progress. The
    -- submission is otherwise FULLY VALID through scalar and snapshot
    -- validation -- expected_sequence=3 matches next_sequence_number,
    -- expected_scene='scene-2' matches the attempt's real (pre-completion)
    -- current_scene_id, state_before=v_state_after_1 exactly matches the
    -- attempt's real serialized_engine_state at sequence 2, state_after=
    -- v_state_after_3 is a self-consistent non-terminal continuation to
    -- 'scene-3', and all terminal fields are NULL -- so the ONLY thing
    -- that can fail is the locked attempt-status check itself, never an
    -- earlier invalid_expected_scene_id or state_lifecycle_mismatch.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_a_id, gen_random_uuid(), 3, 'scene-2', 'opt-x', repeat('8', 64),
            v_state_after_1, v_state_after_3, false, 'scene-3', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'attempt_not_in_progress:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V52 FAILED: submitting an otherwise-valid decision on a completed attempt did not raise attempt_not_in_progress.';
    END IF;
    RAISE NOTICE 'V52 PASSED: post-completion decision submission was rejected by the locked attempt-status check, after passing all earlier scalar and snapshot validation.';

    ----------------------------------------------------------------------
    -- V53: a direct UPDATE of the now-completed attempt is rejected as
    -- permanently immutable -- unconditionally, even though the mutation
    -- guard is still set to this exact attempt id from V50/V51's RPC calls
    -- (is_local guard state persists for the rest of this transaction; the
    -- "already terminal" check runs BEFORE the guard check and does not
    -- consult it at all).
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenario_attempts SET status = 'in_progress' WHERE id = v_attempt_a_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%permanently immutable%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V53 FAILED: direct UPDATE of a completed attempt did not raise a "permanently immutable" error.';
    END IF;
    RAISE NOTICE 'V53 PASSED: direct UPDATE of a completed attempt was rejected as permanently immutable.';

    ----------------------------------------------------------------------
    -- V54: direct UPDATE and DELETE of a scenario_decisions row are both
    -- rejected as append-only.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenario_decisions SET selected_option_id = 'tampered' WHERE id = v_decision_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%append-only and can never be updated%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V54 FAILED: direct UPDATE of a scenario_decisions row did not raise an append-only error.';
    END IF;

    v_caught := false;
    BEGIN
        DELETE FROM public.scenario_decisions WHERE id = v_decision_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%append-only and can never be deleted%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V54 FAILED: direct DELETE of a scenario_decisions row did not raise an append-only error.';
    END IF;
    RAISE NOTICE 'V54 PASSED: direct UPDATE and DELETE of a scenario_decisions row were both rejected as append-only.';

    ----------------------------------------------------------------------
    -- V55: direct DELETE of a scenario_attempts row is rejected
    -- unconditionally (V1 defines no hard-delete operation at all).
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        DELETE FROM public.scenario_attempts WHERE id = v_attempt_a_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%cannot be deleted%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V55 FAILED: direct DELETE of a scenario_attempts row did not raise a "cannot be deleted" error.';
    END IF;
    RAISE NOTICE 'V55 PASSED: direct DELETE of a scenario_attempts row was rejected.';

    ----------------------------------------------------------------------
    -- V56: fixture -- scenario B + one published version + attempt B, for
    -- the abandonment and one-active-attempt-uniqueness checks below.
    ----------------------------------------------------------------------
    INSERT INTO public.scenarios (simulation_id, certification_exam_name, title)
    VALUES ('v68-verify-sim-b', 'Business Analyst', 'V68 Verification Scenario B')
    RETURNING id INTO v_scenario_b_id;

    INSERT INTO public.scenario_versions (scenario_id, version, schema_version, engine_version, source_repository_path)
    VALUES (v_scenario_b_id, '1.0.0', '1.0.0', v_engine_version, 'scenario_content/business_analyst/v68-verify-sim-b/1.0.0/scenario.json')
    RETURNING id INTO v_draft_b_id;

    PERFORM public.publish_scenario_version_v1(
        v_draft_b_id,
        jsonb_build_object('simulationId', 'v68-verify-sim-b', 'version', '1.0.0', 'schemaVersion', '1.0.0'),
        v_hash_b
    );

    v_state_initial_b := jsonb_build_object(
        'simulationId', 'v68-verify-sim-b', 'version', '1.0.0',
        'canonicalContentSha256', v_hash_b, 'engineVersion', v_engine_version,
        'currentSceneId', 'scene-start-b', 'isComplete', false, 'terminalResult', NULL
    );
    v_state_after_1_b := jsonb_build_object(
        'simulationId', 'v68-verify-sim-b', 'version', '1.0.0',
        'canonicalContentSha256', v_hash_b, 'engineVersion', v_engine_version,
        'currentSceneId', 'scene-2b', 'isComplete', false, 'terminalResult', NULL
    );

    SELECT attempt_id, created
    INTO   v_attempt_b_id, v_created
    FROM   public.start_or_resume_scenario_attempt_v1(
               v_email, v_draft_b_id, 'scene-start-b', v_state_initial_b, v_engine_version, v_hash_b
           );
    IF NOT v_created THEN
        RAISE EXCEPTION 'V56 FAILED: fixture attempt B was not created.';
    END IF;
    RAISE NOTICE 'V56 PASSED: fixture scenario B + published version + attempt B created.';

    ----------------------------------------------------------------------
    -- V57: abandon_scenario_attempt_v1 transitions in_progress -> abandoned.
    ----------------------------------------------------------------------
    SELECT status, abandoned_at INTO v_status, v_abandoned_at
    FROM public.abandon_scenario_attempt_v1(v_email, v_attempt_b_id);

    IF v_status IS DISTINCT FROM 'abandoned' OR v_abandoned_at IS NULL THEN
        RAISE EXCEPTION 'V57 FAILED: abandon_scenario_attempt_v1 did not correctly transition attempt B to abandoned.';
    END IF;
    RAISE NOTICE 'V57 PASSED: attempt B was abandoned.';

    ----------------------------------------------------------------------
    -- V58: abandon is idempotent -- calling it again on an
    -- already-abandoned attempt returns the same final state, not an error.
    ----------------------------------------------------------------------
    SELECT status, abandoned_at INTO v_status, v_completed_at
    FROM public.abandon_scenario_attempt_v1(v_email, v_attempt_b_id);

    IF v_status IS DISTINCT FROM 'abandoned' OR v_completed_at IS DISTINCT FROM v_abandoned_at THEN
        RAISE EXCEPTION 'V58 FAILED: re-calling abandon on an already-abandoned attempt did not return the identical final state.';
    END IF;
    RAISE NOTICE 'V58 PASSED: abandon_scenario_attempt_v1 is idempotent.';

    ----------------------------------------------------------------------
    -- V59: an abandoned attempt can never resume or mutate -- a direct
    -- UPDATE is rejected as permanently immutable, and a decision
    -- submission (with mutually self-consistent state_before/state_after,
    -- so the SIM-PERSIST-04C snapshot checks pass and attempt_not_in_
    -- progress is what is actually proven) is rejected as
    -- attempt_not_in_progress.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenario_attempts SET current_scene_id = 'tampered' WHERE id = v_attempt_b_id;
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%permanently immutable%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V59 FAILED: direct UPDATE of an abandoned attempt did not raise a "permanently immutable" error.';
    END IF;

    v_caught := false;
    BEGIN
        PERFORM * FROM public.submit_scenario_decision_v1(
            v_email, v_attempt_b_id, gen_random_uuid(), 1, 'scene-start-b', 'opt-a', repeat('9', 64),
            v_state_initial_b, v_state_after_1_b, false, 'scene-2b', NULL, NULL
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'attempt_not_in_progress:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V59 FAILED: decision submission against an abandoned attempt did not raise attempt_not_in_progress.';
    END IF;
    RAISE NOTICE 'V59 PASSED: an abandoned attempt rejects both direct mutation and decision submission.';

    ----------------------------------------------------------------------
    -- V60: a retake is still possible after abandonment -- starting again
    -- for the same (learner, scenario_version_id_b) creates a NEW attempt
    -- (the partial unique index only constrains in_progress rows). This
    -- again takes the "create new" branch, so v_state_initial_b's
    -- IDENTITY/LIFECYCLE fields must (and do) still pass the
    -- SIM-PERSIST-04C snapshot checks.
    ----------------------------------------------------------------------
    SELECT attempt_id, created
    INTO   v_attempt_b_id_2, v_created
    FROM   public.start_or_resume_scenario_attempt_v1(
               v_email, v_draft_b_id, 'scene-start-b', v_state_initial_b, v_engine_version, v_hash_b
           );

    IF NOT v_created THEN
        RAISE EXCEPTION 'V60 FAILED: starting again after abandonment did not report created=true.';
    END IF;
    IF v_attempt_b_id_2 = v_attempt_b_id THEN
        RAISE EXCEPTION 'V60 FAILED: the retake reused the abandoned attempt''s id instead of creating a distinct row.';
    END IF;
    RAISE NOTICE 'V60 PASSED: a new attempt was created for the same learner/version after abandonment (retake).';

    ----------------------------------------------------------------------
    -- V61: one-active-attempt uniqueness is enforced even against a direct
    -- INSERT that bypasses the RPC layer -- but, as a direct INSERT, it
    -- must first satisfy the SIM-PERSIST-04C attempt-insert guard (this
    -- verification script deliberately sets
    -- certbound.scenario_attempt_insert_guard to name the EXACT id it is
    -- about to insert, exactly as start_or_resume_scenario_attempt_v1
    -- itself does) before it can even reach
    -- idx_scenario_attempts_one_in_progress -- attempting to insert a
    -- second in_progress row for the same (learner, scenario_version_id)
    -- as the retake (v_attempt_b_id_2, still in_progress) then raises the
    -- specific unique_violation condition.
    ----------------------------------------------------------------------
    v_direct_insert_id := gen_random_uuid();
    PERFORM set_config('certbound.scenario_attempt_insert_guard', v_direct_insert_id::text, true);

    v_caught := false;
    BEGIN
        INSERT INTO public.scenario_attempts (
            id, user_email, scenario_id, scenario_version_id, status,
            current_scene_id, next_sequence_number, serialized_engine_state,
            scenario_content_sha256, engine_version
        )
        VALUES (
            v_direct_insert_id, v_email, v_scenario_b_id, v_draft_b_id, 'in_progress',
            'scene-start-b', 1, v_state_initial_b, v_hash_b, v_engine_version
        );
    EXCEPTION WHEN unique_violation THEN
        v_caught := true;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V61 FAILED: a guarded, direct duplicate in_progress INSERT for an existing (learner, scenario_version_id) did not raise unique_violation.';
    END IF;
    RAISE NOTICE 'V61 PASSED: idx_scenario_attempts_one_in_progress rejects a direct duplicate in_progress INSERT, even once the insert guard is satisfied.';

    ----------------------------------------------------------------------
    -- V62: get_scenario_attempt_v1 returns decisions ordered by
    -- sequence_number, for the owning learner, with exactly the two
    -- decisions recorded against attempt A (non-terminal then terminal).
    -- (This same call, per V17 above, now also takes FOR SHARE on the
    -- attempt row for the rest of this transaction -- harmless here since
    -- nothing else in this session is contending for the lock.)
    ----------------------------------------------------------------------
    SELECT decisions INTO v_decisions_json
    FROM public.get_scenario_attempt_v1(v_email, v_attempt_a_id);

    IF jsonb_array_length(v_decisions_json) <> 2 THEN
        RAISE EXCEPTION 'V62 FAILED: expected exactly 2 decisions for attempt A, got %.', jsonb_array_length(v_decisions_json);
    END IF;
    IF (v_decisions_json -> 0 ->> 'sequenceNumber')::int <> 1
       OR (v_decisions_json -> 1 ->> 'sequenceNumber')::int <> 2
       OR (v_decisions_json -> 1 ->> 'isTerminal')::boolean IS DISTINCT FROM true
    THEN
        RAISE EXCEPTION 'V62 FAILED: decisions were not returned in correct sequence_number order with the expected terminal flag: %', v_decisions_json;
    END IF;
    RAISE NOTICE 'V62 PASSED: get_scenario_attempt_v1 returned exactly 2 correctly ordered decisions.';

END;
$$;

ROLLBACK;

-- ---------------------------------------------------------------------------
-- V63: all verification data was inside BEGIN/ROLLBACK and leaves no
-- residue. Run outside the rolled-back transaction, against the real
-- (unaffected) committed state.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_scenario_count  int;
    v_attempt_count   int;
    v_decision_count  int;
BEGIN
    SELECT count(*) INTO v_scenario_count
    FROM public.scenarios
    WHERE simulation_id LIKE 'v68-verify-sim-%';

    SELECT count(*) INTO v_attempt_count
    FROM public.scenario_attempts sa
    JOIN public.scenarios s ON s.id = sa.scenario_id
    WHERE s.simulation_id LIKE 'v68-verify-sim-%';

    SELECT count(*) INTO v_decision_count
    FROM public.scenario_decisions sd
    JOIN public.scenario_attempts sa ON sa.id = sd.attempt_id
    JOIN public.scenarios s ON s.id = sa.scenario_id
    WHERE s.simulation_id LIKE 'v68-verify-sim-%';

    IF v_scenario_count <> 0 OR v_attempt_count <> 0 OR v_decision_count <> 0 THEN
        RAISE EXCEPTION 'V63 FAILED: residual verification data found after ROLLBACK (scenarios=%, attempts=%, decisions=%).', v_scenario_count, v_attempt_count, v_decision_count;
    END IF;

    RAISE NOTICE 'V63 PASSED: no residual test data remains after ROLLBACK.';
    RAISE NOTICE 'VERIFICATION SUMMARY: all V1-V63 checks passed for the V68 scenario attempt persistence foundation, as corrected by SIM-PERSIST-04F.';
END;
$$;
