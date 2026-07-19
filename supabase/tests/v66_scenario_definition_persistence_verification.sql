-- =============================================================================
-- V66 Scenario Definition Persistence Foundation — VERIFICATION
--
-- Targets: supabase/migrations/20260718170000_v66_scenario_definition_
--          persistence_foundation.sql
--
-- Artifact identity: this verification script was originally authored under
-- a V64 filename and renamed to V66 (SIM-PERSIST-02A) before ever being
-- executed. It exercises exactly the objects created by the V66 migration
-- (public.scenarios, public.scenario_versions,
-- public.publish_scenario_version_v1, and both guard triggers) -- none of
-- those SQL object names changed during the V64 -> V66 rename.
--
-- Intended to run ONLY against an approved test database, AFTER the V66
-- migration has been applied:
--   psql "$TEST_DATABASE_URL" -f supabase/tests/v66_scenario_definition_persistence_verification.sql
--
-- All learner-run-shaped test data is created and destroyed inside a single
-- BEGIN ... ROLLBACK transaction (section "V8-V33"). Sections V1-V7 are
-- read-only schema/privilege introspection and run outside any transaction
-- since they must observe the migration's already-committed state.
--
-- This script must NEVER be executed by this task (SIM-PERSIST-02A) or any
-- prior task -- it is written and reviewed only, not run.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- V1: both tables exist.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.scenarios') IS NULL THEN
        RAISE EXCEPTION 'V1 FAILED: public.scenarios does not exist.';
    END IF;
    IF to_regclass('public.scenario_versions') IS NULL THEN
        RAISE EXCEPTION 'V1 FAILED: public.scenario_versions does not exist.';
    END IF;
    RAISE NOTICE 'V1 PASSED: public.scenarios and public.scenario_versions both exist.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V2: expected columns and types exist.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_missing text;
BEGIN
    SELECT string_agg(expected.col || ':' || expected.typ, ', ')
    INTO   v_missing
    FROM (
        VALUES
            ('scenarios', 'id', 'uuid'),
            ('scenarios', 'simulation_id', 'text'),
            ('scenarios', 'certification_exam_name', 'text'),
            ('scenarios', 'title', 'text'),
            ('scenarios', 'description', 'text'),
            ('scenarios', 'is_active', 'boolean'),
            ('scenarios', 'current_published_version_id', 'uuid'),
            ('scenarios', 'created_at', 'timestamp with time zone'),
            ('scenarios', 'updated_at', 'timestamp with time zone'),
            ('scenario_versions', 'id', 'uuid'),
            ('scenario_versions', 'scenario_id', 'uuid'),
            ('scenario_versions', 'version', 'text'),
            ('scenario_versions', 'lifecycle_status', 'text'),
            ('scenario_versions', 'schema_version', 'text'),
            ('scenario_versions', 'engine_version', 'text'),
            ('scenario_versions', 'source_repository_path', 'text'),
            ('scenario_versions', 'canonical_content_sha256', 'text'),
            ('scenario_versions', 'content_snapshot', 'jsonb'),
            ('scenario_versions', 'created_at', 'timestamp with time zone'),
            ('scenario_versions', 'created_by', 'text'),
            ('scenario_versions', 'published_at', 'timestamp with time zone')
    ) AS expected(tbl, col, typ)
    WHERE NOT EXISTS (
        SELECT 1
        FROM   information_schema.columns c
        WHERE  c.table_schema = 'public'
        AND    c.table_name = expected.tbl
        AND    c.column_name = expected.col
        AND    c.data_type = expected.typ
    );

    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'V2 FAILED: missing or mistyped column(s): %', v_missing;
    END IF;
    RAISE NOTICE 'V2 PASSED: all expected columns and types exist on scenarios and scenario_versions.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V3: RLS is enabled on both tables.
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
    RAISE NOTICE 'V3 PASSED: RLS is enabled on both public.scenarios and public.scenario_versions.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V4: PUBLIC, anon, and authenticated lack direct table/function privileges.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF has_table_privilege('anon', 'public.scenarios', 'SELECT')
       OR has_table_privilege('anon', 'public.scenarios', 'INSERT')
       OR has_table_privilege('anon', 'public.scenarios', 'UPDATE')
       OR has_table_privilege('anon', 'public.scenarios', 'DELETE')
       OR has_table_privilege('authenticated', 'public.scenarios', 'SELECT')
       OR has_table_privilege('authenticated', 'public.scenarios', 'INSERT')
       OR has_table_privilege('authenticated', 'public.scenarios', 'UPDATE')
       OR has_table_privilege('authenticated', 'public.scenarios', 'DELETE')
    THEN
        RAISE EXCEPTION 'V4 FAILED: anon or authenticated has a direct privilege on public.scenarios.';
    END IF;

    IF has_table_privilege('anon', 'public.scenario_versions', 'SELECT')
       OR has_table_privilege('anon', 'public.scenario_versions', 'INSERT')
       OR has_table_privilege('anon', 'public.scenario_versions', 'UPDATE')
       OR has_table_privilege('anon', 'public.scenario_versions', 'DELETE')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'SELECT')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'INSERT')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'UPDATE')
       OR has_table_privilege('authenticated', 'public.scenario_versions', 'DELETE')
    THEN
        RAISE EXCEPTION 'V4 FAILED: anon or authenticated has a direct privilege on public.scenario_versions.';
    END IF;

    IF has_function_privilege('anon', 'public.publish_scenario_version_v1(uuid, jsonb, text)', 'EXECUTE')
       OR has_function_privilege('authenticated', 'public.publish_scenario_version_v1(uuid, jsonb, text)', 'EXECUTE')
    THEN
        RAISE EXCEPTION 'V4 FAILED: anon or authenticated can EXECUTE public.publish_scenario_version_v1.';
    END IF;

    RAISE NOTICE 'V4 PASSED: anon and authenticated have zero direct table or function privileges.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V5: service_role has exactly the reduced SIM-PERSIST-02A privilege set --
--     scenarios: SELECT, INSERT, UPDATE (no DELETE); scenario_versions:
--     SELECT, INSERT, UPDATE, DELETE. Also asserts EXECUTE on the RPC.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT has_table_privilege('service_role', 'public.scenarios', 'SELECT')
       OR NOT has_table_privilege('service_role', 'public.scenarios', 'INSERT')
       OR NOT has_table_privilege('service_role', 'public.scenarios', 'UPDATE')
    THEN
        RAISE EXCEPTION 'V5 FAILED: service_role is missing an expected SELECT/INSERT/UPDATE privilege on public.scenarios.';
    END IF;

    IF has_table_privilege('service_role', 'public.scenarios', 'DELETE') THEN
        RAISE EXCEPTION 'V5 FAILED: service_role unexpectedly has DELETE on public.scenarios (grant minimization violated).';
    END IF;

    IF NOT has_table_privilege('service_role', 'public.scenario_versions', 'SELECT')
       OR NOT has_table_privilege('service_role', 'public.scenario_versions', 'INSERT')
       OR NOT has_table_privilege('service_role', 'public.scenario_versions', 'UPDATE')
       OR NOT has_table_privilege('service_role', 'public.scenario_versions', 'DELETE')
    THEN
        RAISE EXCEPTION 'V5 FAILED: service_role is missing an expected SELECT/INSERT/UPDATE/DELETE privilege on public.scenario_versions.';
    END IF;

    IF NOT has_function_privilege('service_role', 'public.publish_scenario_version_v1(uuid, jsonb, text)', 'EXECUTE') THEN
        RAISE EXCEPTION 'V5 FAILED: service_role cannot EXECUTE public.publish_scenario_version_v1.';
    END IF;

    RAISE NOTICE 'V5 PASSED: service_role has exactly the intended reduced privilege set.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V6: publication RPC exists with SECURITY INVOKER and a hardened search_path.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_prosecdef boolean;
    v_def       text;
    v_config    text[];
    v_normalized text;
BEGIN
    SELECT p.prosecdef, pg_get_functiondef(p.oid), p.proconfig
    INTO   v_prosecdef, v_def, v_config
    FROM   pg_proc p
    JOIN   pg_namespace n ON n.oid = p.pronamespace
    WHERE  n.nspname = 'public'
    AND    p.proname = 'publish_scenario_version_v1';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'V6 FAILED: public.publish_scenario_version_v1 does not exist.';
    END IF;

    IF v_prosecdef THEN
        RAISE EXCEPTION 'V6 FAILED: public.publish_scenario_version_v1 is SECURITY DEFINER, expected SECURITY INVOKER.';
    END IF;

    v_normalized := array_to_string(coalesce(v_config, ARRAY[]::text[]), ';');
    v_normalized := regexp_replace(v_normalized, '\s+', '', 'g');
    IF v_normalized NOT LIKE '%search_path=public,pg_catalog%' THEN
        RAISE EXCEPTION 'V6 FAILED: public.publish_scenario_version_v1 does not have SET search_path = public, pg_catalog (proconfig=%).', v_config;
    END IF;

    RAISE NOTICE 'V6 PASSED: public.publish_scenario_version_v1 is SECURITY INVOKER with a hardened search_path.';
END;
$$;

-- ---------------------------------------------------------------------------
-- V7: both guard trigger functions and triggers exist with the expected
--     firing contract (BEFORE; scenario_versions on UPDATE OR DELETE;
--     scenarios on INSERT OR UPDATE OF current_published_version_id).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count     int;
    v_trigdef   text;
BEGIN
    SELECT count(*) INTO v_count
    FROM   pg_proc p
    JOIN   pg_namespace n ON n.oid = p.pronamespace
    WHERE  n.nspname = 'public'
    AND    p.proname IN (
        'guard_scenario_version_immutability_v1',
        'guard_scenario_current_published_version_v1'
    );
    IF v_count <> 2 THEN
        RAISE EXCEPTION 'V7 FAILED: expected exactly 2 guard trigger functions, found %.', v_count;
    END IF;

    SELECT pg_get_triggerdef(t.oid) INTO v_trigdef
    FROM   pg_trigger t
    JOIN   pg_class c ON c.oid = t.tgrelid
    JOIN   pg_namespace n ON n.oid = c.relnamespace
    WHERE  n.nspname = 'public'
    AND    c.relname = 'scenario_versions'
    AND    t.tgname = 'trg_guard_scenario_version_immutability'
    AND    NOT t.tgisinternal;

    IF v_trigdef IS NULL THEN
        RAISE EXCEPTION 'V7 FAILED: trg_guard_scenario_version_immutability does not exist on public.scenario_versions.';
    END IF;
    IF v_trigdef NOT LIKE '%BEFORE UPDATE OR DELETE%' THEN
        RAISE EXCEPTION 'V7 FAILED: trg_guard_scenario_version_immutability firing contract unexpected: %', v_trigdef;
    END IF;

    SELECT pg_get_triggerdef(t.oid) INTO v_trigdef
    FROM   pg_trigger t
    JOIN   pg_class c ON c.oid = t.tgrelid
    JOIN   pg_namespace n ON n.oid = c.relnamespace
    WHERE  n.nspname = 'public'
    AND    c.relname = 'scenarios'
    AND    t.tgname = 'trg_guard_scenario_current_published_version'
    AND    NOT t.tgisinternal;

    IF v_trigdef IS NULL THEN
        RAISE EXCEPTION 'V7 FAILED: trg_guard_scenario_current_published_version does not exist on public.scenarios.';
    END IF;
    IF v_trigdef NOT LIKE '%BEFORE INSERT OR UPDATE OF current_published_version_id%' THEN
        RAISE EXCEPTION 'V7 FAILED: trg_guard_scenario_current_published_version firing contract unexpected: %', v_trigdef;
    END IF;

    RAISE NOTICE 'V7 PASSED: both guard trigger functions and triggers exist with the expected firing contract.';
END;
$$;


-- =============================================================================
-- V8-V33: full lifecycle exercised inside a single rolled-back transaction.
-- All scenario/version rows created below use a 'v66-verify-' simulation_id
-- prefix so the final rollback-confirmation check (V34) can positively
-- confirm no residue remains under that prefix.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    v_scenario_a_id       uuid;
    v_scenario_b_id       uuid;
    v_draft1_id           uuid;
    v_draft2_id           uuid;
    v_draft_b_id          uuid;
    v_valid_hash          text := repeat('a', 64);
    v_valid_hash_2        text := repeat('b', 64);
    v_valid_hash_b        text := repeat('c', 64);
    v_snapshot            jsonb;
    v_result_scenario_id  uuid;
    v_result_version_id   uuid;
    v_result_hash         text;
    v_result_status       text;
    v_result_became       boolean;
    v_current_ptr         uuid;
    v_stored_snapshot     jsonb;
    v_stored_hash         text;
    v_stored_status       text;
    v_caught              boolean;
BEGIN
    ----------------------------------------------------------------------
    -- V8 (setup): create scenario A and its first draft version.
    ----------------------------------------------------------------------
    INSERT INTO public.scenarios (simulation_id, certification_exam_name, title)
    VALUES ('v66-verify-sim-01', 'Business Analyst', 'V66 Verification Scenario A')
    RETURNING id INTO v_scenario_a_id;

    INSERT INTO public.scenario_versions (scenario_id, version, schema_version, engine_version, source_repository_path)
    VALUES (v_scenario_a_id, '1.0.0', '1.0.0', '1.0.0', 'scenario_content/business_analyst/v66-verify-sim-01/1.0.0/scenario.json')
    RETURNING id INTO v_draft1_id;

    RAISE NOTICE 'V8 PASSED: scenario A and draft1 created.';

    ----------------------------------------------------------------------
    -- V9: non-object JSON snapshot fails (snapshot_not_object).
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(v_draft1_id, '"just a string"'::jsonb, v_valid_hash);
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'snapshot_not_object:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V9 FAILED: non-object content_snapshot did not raise snapshot_not_object.';
    END IF;
    RAISE NOTICE 'V9 PASSED: non-object content_snapshot rejected.';

    ----------------------------------------------------------------------
    -- V10: null JSON snapshot fails (snapshot_not_object).
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(v_draft1_id, NULL, v_valid_hash);
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'snapshot_not_object:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V10 FAILED: null content_snapshot did not raise snapshot_not_object.';
    END IF;
    RAISE NOTICE 'V10 PASSED: null content_snapshot rejected.';

    ----------------------------------------------------------------------
    -- V11: missing simulationId fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft1_id,
            jsonb_build_object('version', '1.0.0', 'schemaVersion', '1.0.0'),
            v_valid_hash
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'missing_or_invalid_simulation_id:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V11 FAILED: missing simulationId did not raise missing_or_invalid_simulation_id.';
    END IF;
    RAISE NOTICE 'V11 PASSED: missing simulationId rejected.';

    ----------------------------------------------------------------------
    -- V12: missing version fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft1_id,
            jsonb_build_object('simulationId', 'v66-verify-sim-01', 'schemaVersion', '1.0.0'),
            v_valid_hash
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'missing_or_invalid_version:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V12 FAILED: missing version did not raise missing_or_invalid_version.';
    END IF;
    RAISE NOTICE 'V12 PASSED: missing version rejected.';

    ----------------------------------------------------------------------
    -- V13: missing schemaVersion fails (mandatory, not optional).
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft1_id,
            jsonb_build_object('simulationId', 'v66-verify-sim-01', 'version', '1.0.0'),
            v_valid_hash
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'missing_or_invalid_schema_version:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V13 FAILED: missing schemaVersion did not raise missing_or_invalid_schema_version.';
    END IF;
    RAISE NOTICE 'V13 PASSED: missing schemaVersion rejected.';

    ----------------------------------------------------------------------
    -- V14: empty/whitespace simulationId fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft1_id,
            jsonb_build_object('simulationId', '   ', 'version', '1.0.0', 'schemaVersion', '1.0.0'),
            v_valid_hash
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'missing_or_invalid_simulation_id:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V14 FAILED: whitespace-only simulationId did not raise missing_or_invalid_simulation_id.';
    END IF;
    RAISE NOTICE 'V14 PASSED: whitespace-only simulationId rejected.';

    ----------------------------------------------------------------------
    -- V15: empty/whitespace version fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft1_id,
            jsonb_build_object('simulationId', 'v66-verify-sim-01', 'version', '', 'schemaVersion', '1.0.0'),
            v_valid_hash
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'missing_or_invalid_version:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V15 FAILED: empty version did not raise missing_or_invalid_version.';
    END IF;
    RAISE NOTICE 'V15 PASSED: empty version rejected.';

    ----------------------------------------------------------------------
    -- V16: empty/whitespace schemaVersion fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft1_id,
            jsonb_build_object('simulationId', 'v66-verify-sim-01', 'version', '1.0.0', 'schemaVersion', '  '),
            v_valid_hash
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'missing_or_invalid_schema_version:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V16 FAILED: whitespace-only schemaVersion did not raise missing_or_invalid_schema_version.';
    END IF;
    RAISE NOTICE 'V16 PASSED: whitespace-only schemaVersion rejected.';

    ----------------------------------------------------------------------
    -- V17 (pointer-guard): while draft1 is still a draft, a direct attempt
    -- to point scenarios.current_published_version_id at it fails because
    -- it is not published.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenarios SET current_published_version_id = v_draft1_id WHERE id = v_scenario_a_id;
    EXCEPTION WHEN OTHERS THEN
        v_caught := true;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V17 FAILED: pointing current_published_version_id at a draft version did not fail.';
    END IF;
    RAISE NOTICE 'V17 PASSED: pointer update to a draft version rejected.';

    ----------------------------------------------------------------------
    -- V18: a valid, identity-matching publication succeeds.
    ----------------------------------------------------------------------
    v_snapshot := jsonb_build_object(
        'simulationId', 'v66-verify-sim-01',
        'version', '1.0.0',
        'schemaVersion', '1.0.0',
        'scenes', jsonb_build_array()
    );

    SELECT scenario_id, scenario_version_id, canonical_content_sha256, lifecycle_status, became_current
    INTO   v_result_scenario_id, v_result_version_id, v_result_hash, v_result_status, v_result_became
    FROM   public.publish_scenario_version_v1(v_draft1_id, v_snapshot, v_valid_hash);

    IF v_result_scenario_id IS DISTINCT FROM v_scenario_a_id
       OR v_result_version_id IS DISTINCT FROM v_draft1_id
       OR v_result_hash IS DISTINCT FROM v_valid_hash
       OR v_result_status IS DISTINCT FROM 'published'
       OR v_result_became IS DISTINCT FROM true
    THEN
        RAISE EXCEPTION 'V18 FAILED: valid publication returned unexpected result fields.';
    END IF;
    RAISE NOTICE 'V18 PASSED: valid publication succeeded and returned the expected result.';

    ----------------------------------------------------------------------
    -- V19: scenarios.current_published_version_id now points to draft1.
    ----------------------------------------------------------------------
    SELECT current_published_version_id INTO v_current_ptr FROM public.scenarios WHERE id = v_scenario_a_id;
    IF v_current_ptr IS DISTINCT FROM v_draft1_id THEN
        RAISE EXCEPTION 'V19 FAILED: scenarios.current_published_version_id (%) does not point to draft1 (%).', v_current_ptr, v_draft1_id;
    END IF;
    RAISE NOTICE 'V19 PASSED: current_published_version_id points to the newly published version.';

    ----------------------------------------------------------------------
    -- V20: published content_snapshot and hash are stored verbatim.
    ----------------------------------------------------------------------
    SELECT content_snapshot, canonical_content_sha256, lifecycle_status
    INTO   v_stored_snapshot, v_stored_hash, v_stored_status
    FROM   public.scenario_versions
    WHERE  id = v_draft1_id;

    IF v_stored_snapshot IS DISTINCT FROM v_snapshot
       OR v_stored_hash IS DISTINCT FROM v_valid_hash
       OR v_stored_status IS DISTINCT FROM 'published'
    THEN
        RAISE EXCEPTION 'V20 FAILED: stored content_snapshot/hash/status do not match the publication inputs.';
    END IF;
    RAISE NOTICE 'V20 PASSED: content_snapshot and hash stored verbatim, lifecycle_status=published.';

    ----------------------------------------------------------------------
    -- V21: a second publication of the same (now-published) version fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(v_draft1_id, v_snapshot, v_valid_hash);
    EXCEPTION WHEN OTHERS THEN
        v_caught := true;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V21 FAILED: re-publishing an already-published version did not fail.';
    END IF;
    RAISE NOTICE 'V21 PASSED: re-publication of an already-published version rejected.';

    ----------------------------------------------------------------------
    -- V22: directly editing a published scenario_versions row fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenario_versions SET source_repository_path = 'tampered' WHERE id = v_draft1_id;
    EXCEPTION WHEN OTHERS THEN
        v_caught := true;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V22 FAILED: direct UPDATE of a published scenario_versions row did not fail.';
    END IF;
    RAISE NOTICE 'V22 PASSED: direct UPDATE of a published scenario_versions row rejected.';

    ----------------------------------------------------------------------
    -- V23: directly deleting a published scenario_versions row fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        DELETE FROM public.scenario_versions WHERE id = v_draft1_id;
    EXCEPTION WHEN OTHERS THEN
        v_caught := true;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V23 FAILED: DELETE of a published scenario_versions row did not fail.';
    END IF;
    RAISE NOTICE 'V23 PASSED: DELETE of a published scenario_versions row rejected.';

    ----------------------------------------------------------------------
    -- V24 (setup): create draft2 for scenario A, for mismatch/second-publish tests.
    ----------------------------------------------------------------------
    INSERT INTO public.scenario_versions (scenario_id, version, schema_version, engine_version, source_repository_path)
    VALUES (v_scenario_a_id, '1.1.0', '1.0.0', '1.0.0', 'scenario_content/business_analyst/v66-verify-sim-01/1.1.0/scenario.json')
    RETURNING id INTO v_draft2_id;
    RAISE NOTICE 'V24 PASSED: draft2 created for scenario A.';

    ----------------------------------------------------------------------
    -- V25: simulationId mismatch fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft2_id,
            jsonb_build_object('simulationId', 'wrong-simulation-id', 'version', '1.1.0', 'schemaVersion', '1.0.0'),
            v_valid_hash_2
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'simulation_id_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V25 FAILED: simulationId mismatch did not raise simulation_id_mismatch.';
    END IF;
    RAISE NOTICE 'V25 PASSED: simulationId mismatch rejected.';

    ----------------------------------------------------------------------
    -- V26: version mismatch fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft2_id,
            jsonb_build_object('simulationId', 'v66-verify-sim-01', 'version', 'wrong-version', 'schemaVersion', '1.0.0'),
            v_valid_hash_2
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'version_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V26 FAILED: version mismatch did not raise version_mismatch.';
    END IF;
    RAISE NOTICE 'V26 PASSED: version mismatch rejected.';

    ----------------------------------------------------------------------
    -- V27: schemaVersion mismatch fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft2_id,
            jsonb_build_object('simulationId', 'v66-verify-sim-01', 'version', '1.1.0', 'schemaVersion', 'wrong-schema-version'),
            v_valid_hash_2
        );
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE 'schema_version_mismatch:%' THEN
            v_caught := true;
        ELSE
            RAISE;
        END IF;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V27 FAILED: schemaVersion mismatch did not raise schema_version_mismatch.';
    END IF;
    RAISE NOTICE 'V27 PASSED: schemaVersion mismatch rejected.';

    ----------------------------------------------------------------------
    -- V28: invalid hash format fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        PERFORM public.publish_scenario_version_v1(
            v_draft2_id,
            jsonb_build_object('simulationId', 'v66-verify-sim-01', 'version', '1.1.0', 'schemaVersion', '1.0.0'),
            'not-a-valid-hash'
        );
    EXCEPTION WHEN OTHERS THEN
        v_caught := true;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V28 FAILED: invalid hash format did not fail.';
    END IF;
    RAISE NOTICE 'V28 PASSED: invalid hash format rejected.';

    ----------------------------------------------------------------------
    -- V29 (setup): a second scenario (B) with its own published version, to
    -- test cross-scenario pointer rejection.
    ----------------------------------------------------------------------
    INSERT INTO public.scenarios (simulation_id, certification_exam_name, title)
    VALUES ('v66-verify-sim-02', 'Business Analyst', 'V66 Verification Scenario B')
    RETURNING id INTO v_scenario_b_id;

    INSERT INTO public.scenario_versions (scenario_id, version, schema_version, engine_version, source_repository_path)
    VALUES (v_scenario_b_id, '1.0.0', '1.0.0', '1.0.0', 'scenario_content/business_analyst/v66-verify-sim-02/1.0.0/scenario.json')
    RETURNING id INTO v_draft_b_id;

    PERFORM public.publish_scenario_version_v1(
        v_draft_b_id,
        jsonb_build_object('simulationId', 'v66-verify-sim-02', 'version', '1.0.0', 'schemaVersion', '1.0.0'),
        v_valid_hash_b
    );
    RAISE NOTICE 'V29 PASSED: scenario B created with its own published version.';

    ----------------------------------------------------------------------
    -- V30: pointing scenario A's pointer at scenario B's published version
    -- fails (belongs to a different scenario).
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenarios SET current_published_version_id = v_draft_b_id WHERE id = v_scenario_a_id;
    EXCEPTION WHEN OTHERS THEN
        v_caught := true;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V30 FAILED: pointing scenario A at scenario B''s published version did not fail.';
    END IF;
    RAISE NOTICE 'V30 PASSED: cross-scenario pointer update rejected.';

    ----------------------------------------------------------------------
    -- V31: a second, newer version (draft2) may be published for scenario A,
    -- and this changes only the current pointer.
    ----------------------------------------------------------------------
    PERFORM public.publish_scenario_version_v1(
        v_draft2_id,
        jsonb_build_object('simulationId', 'v66-verify-sim-01', 'version', '1.1.0', 'schemaVersion', '1.0.0'),
        v_valid_hash_2
    );

    SELECT current_published_version_id INTO v_current_ptr FROM public.scenarios WHERE id = v_scenario_a_id;
    IF v_current_ptr IS DISTINCT FROM v_draft2_id THEN
        RAISE EXCEPTION 'V31 FAILED: publishing draft2 did not move current_published_version_id to draft2.';
    END IF;
    RAISE NOTICE 'V31 PASSED: publishing a newer version moved only the current pointer, to the new version.';

    ----------------------------------------------------------------------
    -- V32: the older published version (draft1) remains unchanged, still
    -- published, and still exists.
    ----------------------------------------------------------------------
    SELECT content_snapshot, canonical_content_sha256, lifecycle_status
    INTO   v_stored_snapshot, v_stored_hash, v_stored_status
    FROM   public.scenario_versions
    WHERE  id = v_draft1_id;

    IF NOT FOUND
       OR v_stored_status IS DISTINCT FROM 'published'
       OR v_stored_snapshot IS DISTINCT FROM v_snapshot
       OR v_stored_hash IS DISTINCT FROM v_valid_hash
    THEN
        RAISE EXCEPTION 'V32 FAILED: older published version draft1 was altered, removed, or unpublished after draft2''s publication.';
    END IF;
    RAISE NOTICE 'V32 PASSED: older published version draft1 remains unchanged, published, and available.';

    ----------------------------------------------------------------------
    -- V33 (pointer-guard): a direct attempt to move the pointer back to
    -- draft1 (a validly-published version of the SAME scenario, but outside
    -- the publication guard, which is currently only live for draft2) fails.
    ----------------------------------------------------------------------
    v_caught := false;
    BEGIN
        UPDATE public.scenarios SET current_published_version_id = v_draft1_id WHERE id = v_scenario_a_id;
    EXCEPTION WHEN OTHERS THEN
        v_caught := true;
    END;
    IF NOT v_caught THEN
        RAISE EXCEPTION 'V33 FAILED: direct pointer update outside the publication guard did not fail.';
    END IF;
    RAISE NOTICE 'V33 PASSED: direct pointer update outside the publication guard rejected.';

    ----------------------------------------------------------------------
    -- V34: no scoring, state, attempt, or decision data is created --
    -- this migration defines only scenarios and scenario_versions.
    ----------------------------------------------------------------------
    IF to_regclass('public.scenario_attempts') IS NOT NULL
       OR to_regclass('public.scenario_decisions') IS NOT NULL
    THEN
        RAISE EXCEPTION 'V34 FAILED: scenario_attempts or scenario_decisions unexpectedly exists.';
    END IF;
    RAISE NOTICE 'V34 PASSED: no attempt/decision tables exist; no scoring/state data was created.';

END;
$$;

ROLLBACK;

-- ---------------------------------------------------------------------------
-- V35: the transaction rolled back all V66 test data. Run outside the
-- rolled-back transaction, against the real (unaffected) committed state.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count int;
BEGIN
    SELECT count(*) INTO v_count
    FROM   public.scenarios
    WHERE  simulation_id LIKE 'v66-verify-sim-%';

    IF v_count <> 0 THEN
        RAISE EXCEPTION 'V35 FAILED: % residual v66-verify-sim-% scenario row(s) found after ROLLBACK.', v_count;
    END IF;

    RAISE NOTICE 'V35 PASSED: no residual test data remains after ROLLBACK.';
    RAISE NOTICE 'VERIFICATION SUMMARY: all V1-V35 checks passed for the V66 scenario definition persistence foundation.';
END;
$$;
