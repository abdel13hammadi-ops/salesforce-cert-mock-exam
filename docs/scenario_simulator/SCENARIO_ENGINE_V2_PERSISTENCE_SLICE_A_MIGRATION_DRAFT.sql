-- =============================================================================
-- SCENARIO_ENGINE_V2 Persistence — Slice A — MIGRATION DRAFT (NOT EXECUTABLE YET)
--
-- Task: SIM-PERSIST-V2-02A. This is a DRAFT for independent review only. It is
-- NOT an executable migration and does NOT live in supabase/migrations/. Do
-- not apply this file. Do not connect to Supabase. Do not run this against
-- any database, disposable or otherwise.
--
-- If/when this draft is approved (Slice B), the reviewed, unmodified text of
-- this file is expected to be copied into a real migration file named, per
-- this repository's existing "<UTC timestamp>_v<NN>_<description>.sql"
-- convention (see supabase/migrations/), for example:
--   supabase/migrations/<YYYYMMDDHHMMSS>_v69_scenario_v2_attempt_identity_support.sql
--
-- OBJECTIVE
-- Adds ONE additive, optional parameter to the existing
-- public.start_or_resume_scenario_attempt_v1 RPC:
--     p_attempt_id uuid DEFAULT NULL
-- so a caller (the future Engine V2 persistence adapter) can mint the new
-- attempt's UUID BEFORE calling this RPC, and have the database honor that
-- exact id -- required so Engine V2's deterministic option-ordering algorithm
-- (schema spec section 17) can use the attempt's FINAL, persisted identity
-- from the moment the run object is first constructed, not a value learned
-- only after the RPC returns.
--
-- Full rationale, alternatives considered, and the overload/PostgREST-
-- ambiguity investigation this draft is built on are documented in:
--   docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md
--
-- WHY THIS IS *NOT* A "CREATE OR REPLACE FUNCTION ... ADD ONE PARAMETER" DIFF
-- PostgreSQL identifies a function, for CREATE OR REPLACE purposes, by
-- (schema, name, ordered input-argument-TYPE list) -- names and DEFAULTs are
-- not part of that identity. Appending a 7th input type turns
-- (text,uuid,text,jsonb,text,text) into (text,uuid,text,jsonb,text,text,uuid)
-- -- a DIFFERENT type list. CREATE OR REPLACE FUNCTION against that new type
-- list does NOT replace the existing 6-argument function; it silently creates
-- a SECOND, coexisting overload. PostgREST then cannot disambiguate a request
-- that supplies exactly the original six p_* keys (every existing Engine V1
-- call): both the 6-arg function (exact match) and the 7-arg function (valid,
-- because its 7th parameter has a DEFAULT and may be omitted) match equally,
-- and PostgREST raises PGRST203 ("Could not choose the best candidate
-- function"), breaking EVERY existing caller the moment both signatures are
-- visible to PostgREST's schema cache. This is real, previously-reported
-- production behavior, not a theoretical concern -- see the contract doc's
-- citation. This draft therefore explicitly DROPs the exact old signature
-- and CREATEs the new one, atomically, in one transaction, so at most one
-- signature ever exists in the catalog, restores every REVOKE/GRANT and the
-- COMMENT (both are attached to the specific function object and are NOT
-- preserved across a DROP), and explicitly asks PostgREST to reload its
-- schema cache rather than relying only on Supabase's own post-migration
-- reload hook.
--
-- SCOPE -- table/column/index/trigger/RLS: NONE.
-- This draft touches exactly one function: public.start_or_resume_scenario_
-- attempt_v1. No table, column, index, trigger, RLS policy, or any other
-- database object is created, dropped, or altered. public.scenario_attempts,
-- public.scenario_decisions, their triggers, their RLS posture, and the other
-- three RPCs (get_scenario_attempt_v1, submit_scenario_decision_v1,
-- abandon_scenario_attempt_v1) are entirely untouched.
--
-- TRANSACTIONAL SAFETY
-- The DROP, CREATE, COMMENT, and every REVOKE/GRANT below execute inside one
-- BEGIN ... COMMIT block. PostgreSQL DDL is transactional: no concurrent
-- session can ever observe an intermediate state where this function does
-- not exist at all -- a concurrent reader sees either the pre-migration
-- catalog (old 6-arg function only) or the post-migration catalog (new 7-arg
-- function only), atomically, at commit. DROP FUNCTION takes an ACCESS
-- EXCLUSIVE lock on the function's catalog row for the duration of this
-- transaction; a concurrent caller attempting to START a new call to this
-- specific RPC during that (normally sub-second) window blocks until this
-- transaction commits or rolls back. No other function, table, or row is
-- locked by this migration.
--
-- CORRECTIONS ADDENDUM (Task SIM-PERSIST-V2-02C, applied to this same file)
-- Independent security review (SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_
-- SECURITY_REVIEW.md) identified two HIGH findings and two directly related
-- MEDIUM findings against the original SIM-PERSIST-V2-02A draft of this
-- file. All four are corrected in place here (same file, no new draft):
--   * SA-08-1 (HIGH): the unique_violation exception handler (section 3, the
--     INSERT's own inner BEGIN/EXCEPTION block) no longer classifies a
--     collision by trusting GET STACKED DIAGNOSTICS ... CONSTRAINT_NAME to
--     literally equal 'idx_scenario_attempts_one_in_progress' as its SOLE
--     evidence for the ordinary-race branch. It first re-queries, using data
--     this function already fully controls, whether an in_progress attempt
--     exists for the caller's own (user_email, scenario_version_id); only
--     once that is structurally ruled out does it consult CONSTRAINT_NAME,
--     now purely as defense-in-depth. See the inline comments at that
--     EXCEPTION block for the full branch-by-branch proof. A second,
--     directly related fix closes a consistency gap the review's own
--     idempotency analysis (Area 6/11) surfaced: the exception-handler
--     fallthrough path now applies the identical p_attempt_id-vs-resolved-
--     row equality check the early resume branch already applied, so a
--     caller that loses an active-attempt race with a conflicting supplied
--     id is rejected with attempt_id_conflict rather than silently handed a
--     different id than the one it explicitly asked for.
--   * SA-11-1 (MEDIUM): section 1 now captures the six-argument function's
--     EXACT current owner dynamically from pg_proc.proowner (never guessed,
--     never hardcoded) and section 3b restores that exact value onto the
--     new seven-argument function via ALTER FUNCTION ... OWNER TO,
--     verified by a new postcondition check in section 6.
--   * SA-12-1 (MEDIUM): section 1 now also verifies a set of material,
--     verbatim body markers (RAISE EXCEPTION message text, SECURITY
--     INVOKER, the ON CONFLICT DO NOTHING shape, return-shape column
--     names) against the installed six-argument function's own
--     pg_get_functiondef() output, aborting before the DROP if the live
--     body has materially diverged from this draft's assumed baseline --
--     see the v_markers declaration in section 1 for the full rationale on
--     why an exact hash is not hardcoded without live-database access.
-- SA-06-1 (start-idempotency claim) and SA-21-1 (test-plan completeness)
-- are corrected in the contract document
-- (SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CONTRACT.md), not in this SQL
-- file. Full disposition of all six findings is recorded in
-- SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_CORRECTION_REPORT.md.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Preconditions -- fail explicitly and atomically if any prerequisite
--    object is missing, or if the exact object this migration expects to
--    replace does not exist in the exact shape expected. Never silently
--    tolerates either condition. Mirrors the precondition-block convention
--    established in supabase/migrations/20260719130000_v68_scenario_
--    attempt_persistence_foundation.sql section 1.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_old_oid             oid;
    v_old_owner_name      text;
    v_old_definition      text;
    v_old_definition_norm text;
    v_marker              text;
    -- SA-12-1 correction (SIM-PERSIST-V2-02C): material-definition markers
    -- proving the installed six-argument function's BODY -- not merely its
    -- input-type signature (1b below) -- matches the exact baseline this
    -- migration was written against. An exact pg_get_functiondef() hash is
    -- intentionally NOT hardcoded here: PostgreSQL's own DDL deparser
    -- (ruleutils.c) reformats a function's header clauses (argument list,
    -- RETURNS, SET clauses) in ways this draft-only, no-live-database task
    -- cannot safely reproduce byte-for-byte by hand, and a wrong hardcoded
    -- hash would itself cause a false-positive abort on a genuinely correct
    -- baseline. plpgsql function BODIES, however, are stored and returned
    -- verbatim (prosrc is not reparsed/reformatted for the AS $$ ... $$
    -- interior), so every RAISE EXCEPTION message text below is an exact,
    -- reliable fingerprint fragment once whitespace runs are normalized.
    -- Every fragment must be present; a single missing fragment aborts the
    -- migration before the DROP ever executes, on the theory that failing
    -- loudly on an unrecognized body is always safer than silently
    -- overwriting an unreviewed, possibly-hotfixed function (this
    -- project's own V68 migration header records that V66/V67 were, at
    -- least once, manually applied to production outside the tracked
    -- migration runner -- so this is a documented, not hypothetical, risk
    -- for this exact function family).
    v_markers             text[] := ARRAY[
        'SECURITY INVOKER',
        'invalid_user_email: p_user_email must be a non-empty, non-whitespace email address',
        'scenario_version_not_found: scenario_versions % does not exist',
        'scenario_version_not_published: scenario_versions % is not published (status=%)',
        'engine_version_mismatch: supplied engine_version does not match the pinned published scenario_versions.engine_version for %',
        'content_hash_mismatch: supplied scenario_content_sha256 does not match the pinned published scenario_versions.canonical_content_sha256 for %',
        'pg_advisory_xact_lock(hashtext(v_user_email',
        'invalid_initial_state_lifecycle: p_initial_serialized_state.terminalResult must be null for a newly created attempt',
        'ON CONFLICT DO NOTHING',
        'start_or_resume_failed: no in_progress attempt could be found or created for % / %',
        'terminal_result_snapshot',
        'serialized_engine_state'
    ];
BEGIN
    -- 1a. Required V68 foundation objects must already exist.
    IF to_regclass('public.scenario_attempts') IS NULL THEN
        RAISE EXCEPTION 'SLICE-A PRECONDITION FAILED: public.scenario_attempts does not exist. This migration requires the V68 scenario attempt persistence foundation to already be installed.';
    END IF;

    IF to_regclass('public.scenario_decisions') IS NULL THEN
        RAISE EXCEPTION 'SLICE-A PRECONDITION FAILED: public.scenario_decisions does not exist. This migration requires the V68 scenario attempt persistence foundation to already be installed.';
    END IF;

    -- 1b. The EXACT six-argument function this migration replaces must exist,
    -- with no other signature already registered under the same name (which
    -- would mean a prior, unreviewed attempt at this exact change already
    -- partially ran and must be investigated by hand before proceeding).
    v_old_oid := to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)')::oid;
    IF v_old_oid IS NULL THEN
        RAISE EXCEPTION 'SLICE-A PRECONDITION FAILED: public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text) does not exist in the expected six-argument shape. Refusing to proceed without human review.';
    END IF;

    IF (SELECT count(*) FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'start_or_resume_scenario_attempt_v1') <> 1
    THEN
        RAISE EXCEPTION 'SLICE-A PRECONDITION FAILED: public.start_or_resume_scenario_attempt_v1 already has more than one registered overload. Refusing to proceed without human review -- this migration must never run against an already-ambiguous catalog.';
    END IF;

    -- 1c. The new seven-argument signature must NOT already exist (this
    -- migration must never run twice).
    IF to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)') IS NOT NULL THEN
        RAISE EXCEPTION 'SLICE-A PRECONDITION FAILED: public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid) already exists. This migration has already been applied.';
    END IF;

    -- 1d. SA-11-1 correction: capture the EXACT current owner of the
    -- six-argument function, dynamically, from the live pg_proc catalog --
    -- never guessed, never hardcoded to a specific role name. This
    -- repository's own V68 migration header documents the object owner as
    -- "typically postgres" for this Supabase project, but this precondition
    -- does not rely on that documentation being accurate for every
    -- environment this migration might run in; it reads whatever the
    -- actual live value is, at migration time, and section 3b below
    -- restores that EXACT captured value onto the new seven-argument
    -- function -- correct regardless of which role happens to execute this
    -- migration. The captured name is stashed in a transaction-local GUC
    -- (identical pattern to this same function's own
    -- certbound.scenario_attempt_insert_guard convention) so it survives
    -- across this DO block into the later CREATE/ALTER statements below,
    -- all still inside the same migration transaction.
    SELECT pg_get_userbyid(p.proowner) INTO v_old_owner_name
    FROM   pg_proc p
    WHERE  p.oid = v_old_oid;

    IF v_old_owner_name IS NULL THEN
        RAISE EXCEPTION 'SLICE-A PRECONDITION FAILED: could not resolve the current owner of the six-argument start_or_resume_scenario_attempt_v1 function via pg_proc.proowner. Refusing to proceed without human review -- ownership must never be guessed.';
    END IF;

    PERFORM set_config('slice_a.captured_owner', v_old_owner_name, true);

    -- 1e. SA-12-1 correction: material-definition-marker baseline check
    -- (see v_markers declaration above for full rationale). Whitespace runs
    -- are collapsed to a single space before matching so line-break/
    -- indentation differences in how the body happens to be stored cannot
    -- cause a false-negative; the literal message text and '%' RAISE
    -- format placeholders are otherwise matched exactly, byte-for-byte,
    -- via plain substring search (not a LIKE pattern, so no wildcard-
    -- escaping ambiguity for the literal '%' characters below).
    SELECT pg_get_functiondef(v_old_oid) INTO v_old_definition;
    v_old_definition_norm := regexp_replace(v_old_definition, '\s+', ' ', 'g');

    FOREACH v_marker IN ARRAY v_markers LOOP
        IF position(v_marker IN v_old_definition_norm) = 0 THEN
            RAISE EXCEPTION 'SLICE-A PRECONDITION FAILED: baseline-fingerprint marker not found in the installed six-argument function body (material definition drift detected): %. Refusing to proceed without human review -- an unreviewed change may have been applied to this function outside this migration''s own tracked history.', v_marker;
        END IF;
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 2. DROP the exact old six-argument signature.
--
-- This also removes, as an unavoidable side effect of dropping the function
-- object, its COMMENT and every REVOKE/GRANT attached to it -- both are
-- restored explicitly for the new signature in sections 4-5 below. No other
-- object depends on this function (it is a leaf RPC, never called from
-- another function/view/trigger in this schema), so no CASCADE is needed or
-- used -- a plain DROP FUNCTION (no CASCADE) is deliberately used so that,
-- if some undiscovered dependency DOES exist, this migration fails loudly
-- instead of silently cascading a drop through it.
-- ---------------------------------------------------------------------------

DROP FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text);

-- ---------------------------------------------------------------------------
-- 3. CREATE the new seven-argument signature.
--
-- Every line of the function body below is IDENTICAL to the pre-existing
-- body (supabase/migrations/20260719130000_v68_scenario_attempt_persistence_
-- foundation.sql, lines 867-1138) except for the four changes called out
-- inline with "-- SLICE-A:" comments. RETURNS TABLE is byte-for-byte
-- unchanged (same 15 columns, same names, same order, same types) -- no
-- existing caller's expectation of the return shape changes.
-- ---------------------------------------------------------------------------

CREATE FUNCTION public.start_or_resume_scenario_attempt_v1(
    p_user_email                text,
    p_scenario_version_id       uuid,
    p_initial_current_scene_id  text,
    p_initial_serialized_state  jsonb,
    p_engine_version            text,
    p_scenario_content_sha256   text,
    p_attempt_id                uuid DEFAULT NULL  -- SLICE-A: new, additive, optional
)
RETURNS TABLE (
    attempt_id                uuid,
    created                   boolean,
    scenario_id                uuid,
    scenario_version_id        uuid,
    status                     text,
    current_scene_id           text,
    next_sequence_number       integer,
    serialized_engine_state    jsonb,
    engine_version              text,
    scenario_content_sha256    text,
    started_at                  timestamptz,
    completed_at                timestamptz,
    abandoned_at                timestamptz,
    terminal_ending_id          text,
    terminal_result_snapshot    jsonb
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_user_email      text;
    v_version         record;
    v_existing        record;
    v_new_id          uuid;
    v_inserted        boolean;
    v_constraint_name text;
    v_active_exists   boolean;
BEGIN
    -- 1. Normalize and validate email. (unchanged)
    v_user_email := NULLIF(BTRIM(LOWER(p_user_email)), '');
    IF v_user_email IS NULL OR v_user_email !~ '@' THEN
        RAISE EXCEPTION 'invalid_user_email: p_user_email must be a non-empty, non-whitespace email address'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_scenario_version_id IS NULL THEN
        RAISE EXCEPTION 'invalid_scenario_version_id: p_scenario_version_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- SLICE-A: minimal, defensive validation of the new parameter's shape.
    -- Never accepts the nil UUID as a legitimate caller-supplied identity --
    -- no real client ever mints it, and accepting it silently would only
    -- ever mask a client-side bug.
    IF p_attempt_id IS NOT NULL AND p_attempt_id = '00000000-0000-0000-0000-000000000000'::uuid THEN
        RAISE EXCEPTION 'invalid_attempt_id: p_attempt_id must not be the nil UUID'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 2 & 3. The scenario version must exist, be published, and its pinned
    -- identity must match the caller's expectation. (unchanged)
    SELECT sv.id, sv.scenario_id, sv.lifecycle_status, sv.engine_version, sv.canonical_content_sha256,
           sv.version, s.simulation_id
    INTO   v_version
    FROM   public.scenario_versions AS sv
    JOIN   public.scenarios AS s ON s.id = sv.scenario_id
    WHERE  sv.id = p_scenario_version_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'scenario_version_not_found: scenario_versions % does not exist', p_scenario_version_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_version.lifecycle_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'scenario_version_not_published: scenario_versions % is not published (status=%)', p_scenario_version_id, v_version.lifecycle_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF NULLIF(BTRIM(p_engine_version), '') IS NULL OR p_engine_version IS DISTINCT FROM v_version.engine_version THEN
        RAISE EXCEPTION 'engine_version_mismatch: supplied engine_version does not match the pinned published scenario_versions.engine_version for %', p_scenario_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_scenario_content_sha256 IS NULL OR p_scenario_content_sha256 !~ '^[0-9a-f]{64}$'
       OR p_scenario_content_sha256 IS DISTINCT FROM v_version.canonical_content_sha256
    THEN
        RAISE EXCEPTION 'content_hash_mismatch: supplied scenario_content_sha256 does not match the pinned published scenario_versions.canonical_content_sha256 for %', p_scenario_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 4. Advisory lock covering (learner, scenario version). (unchanged --
    -- still keyed only on user_email + scenario_version_id; p_attempt_id
    -- plays no role in lock acquisition.)
    PERFORM pg_advisory_xact_lock(hashtext(v_user_email || ':' || p_scenario_version_id::text));

    -- 5. Return an existing in_progress attempt for this exact
    -- (learner, scenario_version_id) pair, if one exists. (unchanged query)
    SELECT sa.id, sa.scenario_id, sa.scenario_version_id, sa.status, sa.current_scene_id,
           sa.next_sequence_number, sa.serialized_engine_state, sa.engine_version,
           sa.scenario_content_sha256, sa.started_at, sa.completed_at, sa.abandoned_at,
           sa.terminal_ending_id, sa.terminal_result_snapshot
    INTO   v_existing
    FROM   public.scenario_attempts AS sa
    WHERE  sa.user_email = v_user_email
    AND    sa.scenario_version_id = p_scenario_version_id
    AND    sa.status = 'in_progress'
    FOR UPDATE;

    IF FOUND THEN
        -- SLICE-A: a supplied p_attempt_id that disagrees with the caller's
        -- OWN existing in_progress attempt (v_existing is already scoped to
        -- v_user_email above -- this can never leak another owner's attempt
        -- id) is rejected, fail-closed, before anything is returned. A NULL
        -- or matching p_attempt_id is treated identically to today's
        -- behavior (silently resumes).
        IF p_attempt_id IS NOT NULL AND p_attempt_id IS DISTINCT FROM v_existing.id THEN
            RAISE EXCEPTION 'attempt_id_conflict: supplied p_attempt_id does not match the caller''s existing in_progress attempt for this scenario version'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        RETURN QUERY SELECT
            v_existing.id, false, v_existing.scenario_id, v_existing.scenario_version_id,
            v_existing.status, v_existing.current_scene_id, v_existing.next_sequence_number,
            v_existing.serialized_engine_state, v_existing.engine_version, v_existing.scenario_content_sha256,
            v_existing.started_at, v_existing.completed_at, v_existing.abandoned_at,
            v_existing.terminal_ending_id, v_existing.terminal_result_snapshot;
        RETURN;
    END IF;

    -- 6 & 7. Insert exactly one new attempt. (unchanged validation below)
    IF NULLIF(BTRIM(p_initial_current_scene_id), '') IS NULL THEN
        RAISE EXCEPTION 'invalid_initial_scene: p_initial_current_scene_id must not be null or empty when creating a new attempt'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    p_initial_current_scene_id := BTRIM(p_initial_current_scene_id);

    IF p_initial_serialized_state IS NULL OR jsonb_typeof(p_initial_serialized_state) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid_initial_state: p_initial_serialized_state must be a JSON object when creating a new attempt'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_initial_serialized_state->'simulationId') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.simulationId must be a JSON string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF NULLIF(BTRIM(p_initial_serialized_state->>'simulationId'), '') IS NULL THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.simulationId must be a normalized, non-empty string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_initial_serialized_state->>'simulationId') IS DISTINCT FROM BTRIM(p_initial_serialized_state->>'simulationId') THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.simulationId must already be trimmed (no leading/trailing whitespace)'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_initial_serialized_state->>'simulationId') IS DISTINCT FROM v_version.simulation_id THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.simulationId does not match the pinned scenarios.simulation_id for %', p_scenario_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_initial_serialized_state->'version') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.version must be a JSON string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF NULLIF(BTRIM(p_initial_serialized_state->>'version'), '') IS NULL THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.version must be a normalized, non-empty string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_initial_serialized_state->>'version') IS DISTINCT FROM BTRIM(p_initial_serialized_state->>'version') THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.version must already be trimmed (no leading/trailing whitespace)'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_initial_serialized_state->>'version') IS DISTINCT FROM v_version.version THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.version does not match the pinned scenario_versions.version for %', p_scenario_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_initial_serialized_state->'engineVersion') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.engineVersion must be a JSON string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_initial_serialized_state->>'engineVersion') IS DISTINCT FROM v_version.engine_version THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.engineVersion does not match the pinned engine_version for %', p_scenario_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_initial_serialized_state->'canonicalContentSha256') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.canonicalContentSha256 must be a JSON string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_initial_serialized_state->>'canonicalContentSha256') IS DISTINCT FROM v_version.canonical_content_sha256 THEN
        RAISE EXCEPTION 'invalid_initial_state_identity: p_initial_serialized_state.canonicalContentSha256 does not match the pinned canonical_content_sha256 for %', p_scenario_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_initial_serialized_state->'currentSceneId') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'invalid_initial_state_lifecycle: p_initial_serialized_state.currentSceneId must be a JSON string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_initial_serialized_state->>'currentSceneId') IS DISTINCT FROM p_initial_current_scene_id THEN
        RAISE EXCEPTION 'invalid_initial_state_lifecycle: p_initial_serialized_state.currentSceneId does not match p_initial_current_scene_id'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_initial_serialized_state->'isComplete') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION 'invalid_initial_state_lifecycle: p_initial_serialized_state.isComplete must be a JSON boolean'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_initial_serialized_state->'isComplete') IS DISTINCT FROM 'false'::jsonb THEN
        RAISE EXCEPTION 'invalid_initial_state_lifecycle: p_initial_serialized_state.isComplete must be false for a newly created attempt'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_initial_serialized_state->'terminalResult') IS DISTINCT FROM 'null' THEN
        RAISE EXCEPTION 'invalid_initial_state_lifecycle: p_initial_serialized_state.terminalResult must be null for a newly created attempt'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- SLICE-A: use the caller-supplied id when present; otherwise preserve
    -- exactly today's Engine V1 behavior (a fresh, server-generated id).
    v_new_id := COALESCE(p_attempt_id, gen_random_uuid());
    PERFORM set_config('certbound.scenario_attempt_insert_guard', v_new_id::text, true);

    -- SLICE-A: the original bare "ON CONFLICT DO NOTHING" (no explicit
    -- target -- the original migration's own comment explains this was
    -- required only to sidestep an unrelated PL/pgSQL ambiguity between the
    -- conflict-target column name "scenario_version_id" and this function's
    -- identically-named OUT parameter) is replaced with an explicit
    -- exception handler so a PRIMARY KEY collision (only reachable today via
    -- a caller-supplied p_attempt_id reusing an existing id, or an
    -- astronomically unlikely gen_random_uuid() collision) is distinguished
    -- from, and never silently conflated with, the ordinary "a concurrent
    -- caller already won the (user_email, scenario_version_id) in_progress
    -- race" case. This does not change observable behavior for any existing
    -- Engine V1 caller (which never supplies p_attempt_id): the only
    -- realistically reachable branch for such a caller remains the
    -- idx_scenario_attempts_one_in_progress branch below, handled exactly as
    -- ON CONFLICT DO NOTHING handled it before.
    v_inserted := false;
    BEGIN
        INSERT INTO public.scenario_attempts (
            id, user_email, scenario_id, scenario_version_id, status,
            current_scene_id, next_sequence_number, serialized_engine_state,
            scenario_content_sha256, engine_version, started_at, updated_at
        )
        VALUES (
            v_new_id, v_user_email, v_version.scenario_id, p_scenario_version_id, 'in_progress',
            p_initial_current_scene_id, 1, p_initial_serialized_state,
            v_version.canonical_content_sha256, v_version.engine_version, now(), now()
        );
        v_inserted := true;
    EXCEPTION
        WHEN unique_violation THEN
            -- SA-08-1 (SIM-PERSIST-V2-02C correction): classification no
            -- longer trusts GET STACKED DIAGNOSTICS ... CONSTRAINT_NAME as
            -- the SOLE basis for distinguishing a primary-key collision
            -- from the ordinary partial-index active-attempt race. Whether
            -- a partial unique index's own name is populated verbatim,
            -- unqualified, and unquoted in CONSTRAINT_NAME on every
            -- PostgreSQL major version this project might run on was an
            -- unverified assumption, correctly flagged during independent
            -- security review (SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_
            -- SECURITY_REVIEW.md, finding SA-08-1) that this review was
            -- explicitly barred from empirically confirming (no live
            -- database connection permitted). This handler instead
            -- re-derives the correct classification structurally, from
            -- data this function can already independently trust, using
            -- CONSTRAINT_NAME only as a defense-in-depth secondary signal:
            --
            --   1. Re-query whether an in_progress attempt now exists for
            --      THIS caller's own (v_user_email, p_scenario_version_id)
            --      -- the exact ownership+scenario scope the partial index
            --      idx_scenario_attempts_one_in_progress enforces, and the
            --      SAME scope this function's own resume-branch SELECT
            --      above already trusts. This SELECT is guaranteed to see
            --      a genuine concurrent winner's row: PostgreSQL's own
            --      unique-index conflict-checking blocks a concurrent
            --      inserter until the OTHER transaction's outcome (commit
            --      or rollback) is fully resolved before ever raising
            --      unique_violation, so by the time this SELECT runs, a
            --      real partial-index race's winning row is already
            --      visible under READ COMMITTED (this function's implicit
            --      isolation level, unchanged from V68).
            --   2. If that row EXISTS: this can only be the ordinary,
            --      benign concurrent active-attempt race -- the partial
            --      index's own definition guarantees at most one
            --      in_progress row per (user_email, scenario_version_id),
            --      so an existing in_progress row for THIS exact key is
            --      structurally impossible to explain any other way (it
            --      cannot be a PRIMARY KEY collision coincidence, because a
            --      PRIMARY KEY violation carries no information at all
            --      about whether an in_progress row exists for this
            --      caller's own key). Falls through exactly as
            --      ON CONFLICT DO NOTHING did before -- the ownership-safe
            --      attempt-id conflict check immediately following this
            --      block (identical to the resume branch's own check,
            --      above) then decides whether to return that row or raise
            --      attempt_id_conflict.
            --   3. If that row does NOT exist: the partial index is
            --      structurally ruled out (by the same argument in
            --      reverse), so the violation can only be the PRIMARY KEY
            --      (the ONLY other unique-enforcing object on this table,
            --      independently re-confirmed against the live V68 schema
            --      -- supabase/migrations/20260719130000_v68_scenario_
            --      attempt_persistence_foundation.sql lines 428-526,
            --      613-615 -- a repository-wide check found no third
            --      unique-enforcing object on scenario_attempts) -- or, in
            --      a hypothetical future schema change, something genuinely
            --      unknown. GET STACKED DIAGNOSTICS is consulted in this
            --      branch ONLY, purely as defense-in-depth, to keep a
            --      truly unrecognized future case fail-closed rather than
            --      ever silently reclassified as attempt_id_collision.
            v_active_exists := EXISTS (
                SELECT 1 FROM public.scenario_attempts AS sa
                WHERE  sa.user_email = v_user_email
                AND    sa.scenario_version_id = p_scenario_version_id
                AND    sa.status = 'in_progress'
            );

            IF v_active_exists THEN
                -- Ordinary concurrent active-attempt race. Never a
                -- collision on p_attempt_id -- structurally cannot be the
                -- PRIMARY KEY branch (point 2 above). Falls through to the
                -- re-SELECT below, which deterministically returns the
                -- winner's row, exactly as ON CONFLICT DO NOTHING did.
                v_inserted := false;
            ELSE
                -- No in_progress row exists for this caller's own
                -- (user_email, scenario_version_id) -- the partial index is
                -- structurally ruled out (point 3 above). CONSTRAINT_NAME
                -- is consulted now purely as defense-in-depth.
                GET STACKED DIAGNOSTICS v_constraint_name = CONSTRAINT_NAME;
                IF v_constraint_name = 'idx_scenario_attempts_one_in_progress' THEN
                    -- Structurally unreachable given v_active_exists was
                    -- just proven false immediately above -- if
                    -- PostgreSQL's own diagnostics ever disagreed with this
                    -- function's own re-query, that disagreement is itself
                    -- untrustworthy; fail closed with a generic error
                    -- rather than silently reconciling it either way.
                    RAISE EXCEPTION 'start_or_resume_failed: internal consistency check failed while classifying a unique-constraint violation for % / %', v_user_email, p_scenario_version_id
                        USING ERRCODE = 'internal_error';
                ELSIF v_constraint_name IS NULL OR v_constraint_name = 'scenario_attempts_pkey' THEN
                    -- p_attempt_id (or, astronomically unlikely, a freshly
                    -- generated id) collides with an EXISTING row's id --
                    -- fails loudly and safely. Never reveals anything about
                    -- the colliding row: not its owner, not its scenario,
                    -- not its status -- an owner other than the caller may
                    -- hold that id. Accepting a NULL CONSTRAINT_NAME here
                    -- (rather than requiring an exact string match) is
                    -- deliberate: since the partial index was already
                    -- structurally ruled out above by data this function
                    -- fully controls, the PRIMARY KEY is the only
                    -- remaining known possibility regardless of whether
                    -- CONSTRAINT_NAME happened to populate the expected
                    -- literal string on this PostgreSQL version.
                    RAISE EXCEPTION 'attempt_id_collision: the supplied p_attempt_id is already in use'
                        USING ERRCODE = 'unique_violation';
                ELSE
                    -- A genuinely unrecognized unique-enforcing object
                    -- (none exists today on this table, per the
                    -- repository-wide check above -- this is defense in
                    -- depth for a future schema change only). Fail closed
                    -- -- NEVER mislabel as attempt_id_collision.
                    RAISE EXCEPTION 'start_or_resume_failed: unexpected unique constraint violation (%) while creating a scenario attempt', v_constraint_name
                        USING ERRCODE = 'internal_error';
                END IF;
            END IF;
    END;

    SELECT sa.id, sa.scenario_id, sa.scenario_version_id, sa.status, sa.current_scene_id,
           sa.next_sequence_number, sa.serialized_engine_state, sa.engine_version,
           sa.scenario_content_sha256, sa.started_at, sa.completed_at, sa.abandoned_at,
           sa.terminal_ending_id, sa.terminal_result_snapshot
    INTO   v_existing
    FROM   public.scenario_attempts AS sa
    WHERE  sa.user_email = v_user_email
    AND    sa.scenario_version_id = p_scenario_version_id
    AND    sa.status = 'in_progress'
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'start_or_resume_failed: no in_progress attempt could be found or created for % / %', v_user_email, p_scenario_version_id
            USING ERRCODE = 'internal_error';
    END IF;

    -- SA-08-1 correction (SIM-PERSIST-V2-02C): apply the IDENTICAL
    -- ownership-safe conflict check the early resume branch above already
    -- applies, for this fallthrough (v_inserted = false, i.e. an ordinary
    -- concurrent active-attempt race was detected during the INSERT) case
    -- too. Previously (Slice A draft, pre-correction), a caller supplying
    -- a p_attempt_id that lost such a race silently received the WINNER's
    -- id instead of its own -- safe (never another owner's data), but
    -- inconsistent with the resume branch's own stricter behavior. This
    -- correction makes both branches behave identically: id equality is
    -- never assumed to imply request equality, anywhere in this function.
    -- A caller that omitted p_attempt_id (the overwhelmingly common,
    -- Engine-V1-compatible case) is completely unaffected -- the
    -- IS NOT NULL guard below is a no-op for it, and v_inserted is always
    -- true for any caller that actually won the create race, making this
    -- check a no-op for the ordinary create-succeeds path too.
    IF NOT v_inserted AND p_attempt_id IS NOT NULL AND p_attempt_id IS DISTINCT FROM v_existing.id THEN
        RAISE EXCEPTION 'attempt_id_conflict: supplied p_attempt_id does not match the caller''s existing in_progress attempt for this scenario version'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- SLICE-A: "created" is now the exception handler's own v_inserted flag
    -- rather than an id-equality comparison against a RETURNING-populated
    -- variable (the original ON CONFLICT DO NOTHING approach relied on
    -- RETURNING leaving v_new_id NULL on a skipped insert; this migration's
    -- explicit exception handler makes that inference unnecessary and
    -- strictly clearer).
    RETURN QUERY SELECT
        v_existing.id, v_inserted, v_existing.scenario_id, v_existing.scenario_version_id,
        v_existing.status, v_existing.current_scene_id, v_existing.next_sequence_number,
        v_existing.serialized_engine_state, v_existing.engine_version, v_existing.scenario_content_sha256,
        v_existing.started_at, v_existing.completed_at, v_existing.abandoned_at,
        v_existing.terminal_ending_id, v_existing.terminal_result_snapshot;
END;
$$;

-- ---------------------------------------------------------------------------
-- 3b. SA-11-1 correction (SIM-PERSIST-V2-02C): restore the EXACT owner
--    captured in section 1d onto the newly created function.
--    CREATE FUNCTION assigns ownership to whichever role executes this
--    statement, which is only guaranteed to match the pre-migration owner
--    if this migration happens to run through the identical role/pathway
--    the original V68 migration used. Rather than assuming that, or
--    hardcoding a specific role name (this task explicitly forbids
--    guessing the owner), this statement reads back the name captured
--    dynamically, from the live catalog, in section 1 above, via a
--    transaction-local GUC (identical pattern to this same function's own
--    certbound.scenario_attempt_insert_guard convention) -- correct
--    regardless of what role actually owned the six-argument function in
--    any given environment. Because the function is SECURITY INVOKER (not
--    DEFINER), owner drift has no bearing on the function's own runtime
--    privilege behavior; it only affects who may subsequently ALTER/DROP/
--    re-GRANT this specific object without needing to be a superuser --
--    still a real, independently-flagged gap this section closes exactly.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid) OWNER TO %I',
        current_setting('slice_a.captured_owner')
    );
END
$$;

-- ---------------------------------------------------------------------------
-- 4. Restore the COMMENT for the new signature (comments are attached to the
--    specific function object and are not carried over a DROP/CREATE).
-- ---------------------------------------------------------------------------

COMMENT ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid) IS
'Starts a new attempt, or resumes the caller''s existing in_progress attempt,
for one exact (user_email, scenario_version_id) pair. Validates the target
scenario_versions row exists, is published, and matches the caller''s
expected engine_version/canonical_content_sha256 BEFORE deciding whether to
create or resume. When creating, also validates the supplied
p_initial_serialized_state''s IDENTITY fields (simulationId, version,
engineVersion, canonicalContentSha256) against the pinned scenarios/
scenario_versions row and its LIFECYCLE fields (currentSceneId, isComplete,
terminalResult) against p_initial_current_scene_id and the newly-created
attempt''s own starting lifecycle -- invalid_initial_state_identity /
invalid_initial_state_lifecycle respectively. simulationId and version are
pinned EXACTLY to the fetched scenarios.simulation_id / scenario_versions.
version row, and every field''s JSON type is checked before any ->> textual
comparison is trusted. SLICE-A (SIM-PERSIST-V2-02A, corrected under
SIM-PERSIST-V2-02C): accepts an optional p_attempt_id uuid DEFAULT NULL.
When creating a new attempt, uses p_attempt_id when supplied, otherwise
generates a fresh id exactly as before (Engine V1 behavior unchanged). When
resuming -- or when falling through from an ordinary concurrent
active-attempt race during creation -- a supplied p_attempt_id that
disagrees with the resolved row''s own id is rejected with
attempt_id_conflict; a p_attempt_id that collides with ANY existing row''s
id (any owner) is rejected with attempt_id_collision, revealing nothing
about the colliding row. Generates the new attempt''s id itself (or uses
the caller-supplied one) and sets the transaction-local certbound.
scenario_attempt_insert_guard immediately before its own INSERT, so a direct
service_role INSERT bypassing this RPC is rejected by trg_guard_scenario_
attempt_mutation. Uses pg_advisory_xact_lock plus an explicit exception
handler that classifies a unique_violation WITHOUT relying solely on
GET STACKED DIAGNOSTICS ... CONSTRAINT_NAME string-matching a partial
unique index''s name: it first re-queries whether an in_progress attempt
now exists for the caller''s own (user_email, scenario_version_id) --
structurally proving whether the ordinary idx_scenario_attempts_one_in_
progress concurrent-start race occurred -- and only consults
CONSTRAINT_NAME as a defense-in-depth secondary signal once that
possibility is already ruled out, so a scenario_attempts_pkey collision
(attempt_id_collision) is never confused with an ordinary concurrent
duplicate-start race (unchanged Engine V1 behavior) and a genuinely unknown
unique-constraint violation always fails closed with a generic error
instead of ever being mislabeled as attempt_id_collision. Preserves the
exact pre-migration function owner via an explicit ALTER FUNCTION ... OWNER
TO, captured dynamically at migration time rather than assumed. Never
resumes a completed or abandoned attempt. Execute permission: service_role
only.';

-- ---------------------------------------------------------------------------
-- 5. Restore grants for the new signature -- identical policy to the
--    original function: PUBLIC/anon/authenticated have no access;
--    service_role alone may execute this RPC. A newly created function
--    object defaults to EXECUTE granted to PUBLIC unless revoked, so these
--    REVOKE statements are not optional cleanup -- they are load-bearing.
-- ---------------------------------------------------------------------------

REVOKE ALL ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid) TO service_role;

-- ---------------------------------------------------------------------------
-- 6. Postconditions -- verify exactly one signature exists post-migration,
--    with the exact expected privilege posture, before allowing the
--    transaction to commit.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF (SELECT count(*) FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'start_or_resume_scenario_attempt_v1') <> 1
    THEN
        RAISE EXCEPTION 'SLICE-A POSTCONDITION FAILED: expected exactly one start_or_resume_scenario_attempt_v1 overload to exist after this migration.';
    END IF;

    IF to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)') IS NOT NULL THEN
        RAISE EXCEPTION 'SLICE-A POSTCONDITION FAILED: the old six-argument signature still exists after this migration.';
    END IF;

    IF to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)') IS NULL THEN
        RAISE EXCEPTION 'SLICE-A POSTCONDITION FAILED: the new seven-argument signature does not exist after this migration.';
    END IF;

    IF has_function_privilege('anon', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)', 'EXECUTE')
       OR has_function_privilege('authenticated', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)', 'EXECUTE')
    THEN
        RAISE EXCEPTION 'SLICE-A POSTCONDITION FAILED: anon/authenticated must not be able to execute start_or_resume_scenario_attempt_v1.';
    END IF;

    IF NOT has_function_privilege('service_role', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)', 'EXECUTE') THEN
        RAISE EXCEPTION 'SLICE-A POSTCONDITION FAILED: service_role must be able to execute start_or_resume_scenario_attempt_v1.';
    END IF;

    -- SA-11-1 correction: the new function's owner must be byte-for-byte
    -- identical to the owner captured from the old function in section 1d,
    -- proving section 3b's ALTER FUNCTION ... OWNER TO actually took
    -- effect, rather than assuming it silently succeeded.
    IF (SELECT pg_get_userbyid(p.proowner) FROM pg_proc p
        WHERE p.oid = to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)')::oid)
        IS DISTINCT FROM current_setting('slice_a.captured_owner')
    THEN
        RAISE EXCEPTION 'SLICE-A POSTCONDITION FAILED: the new seven-argument function''s owner (%) does not match the captured original owner (%).',
            (SELECT pg_get_userbyid(p.proowner) FROM pg_proc p
             WHERE p.oid = to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)')::oid),
            current_setting('slice_a.captured_owner');
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 7. Ask PostgREST to reload its schema cache. Supabase's own migration
--    pipeline is expected to do this automatically, but this NOTIFY removes
--    any dependency on that automatic behavior (defense in depth against
--    the exact PGRST203 ambiguity/staleness failure mode this migration was
--    designed to avoid -- see the header comment and the contract doc).
--    Harmless and idempotent if PostgREST has already reloaded.
-- ---------------------------------------------------------------------------

NOTIFY pgrst, 'reload schema';

COMMIT;
