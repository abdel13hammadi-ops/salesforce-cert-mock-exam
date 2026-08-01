-- =============================================================================
-- SCENARIO_ENGINE_V2 Persistence -- Slice B -- APPROVED ROLLBACK ARTIFACT
--
-- Task: SIM-PERSIST-V2-03. This is the reviewed, repository-approved rollback
-- artifact for supabase/migrations/20260719140000_v69_scenario_v2_attempt_
-- identity_support.sql, derived exactly from the independently reviewed
-- draft docs/scenario_simulator/SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_
-- ROLLBACK_DRAFT.sql (Task SIM-PERSIST-V2-02A, corrected under
-- SIM-PERSIST-V2-02C, confirmed READY_FOR_EXECUTABLE_MIGRATION by
-- SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_FINAL_REVIEW.md, Task
-- SIM-PERSIST-V2-02D).
--
-- This file intentionally does NOT live in supabase/migrations/. Per this
-- repository's own documented migration convention (supabase/README.md,
-- "Rollback via forward migration. There are no `down` scripts. Reversals
-- are new additive migrations ... that require separate destructive-change
-- review before execution."), a DROP-then-CREATE reversal of a live RPC is
-- exactly the kind of destructive-adjacent change that must be reviewed and
-- applied deliberately, by hand, as a one-off operator action -- never
-- picked up automatically by a forward migration runner. This artifact is
-- the reviewed, approved SQL an operator runs manually (e.g. via `psql` or
-- the Supabase SQL editor) if and only if this specific migration needs to
-- be reversed.
--
-- Reverses exactly, and only,
-- supabase/migrations/20260719140000_v69_scenario_v2_attempt_identity_
-- support.sql: drops the new seven-argument public.start_or_resume_
-- scenario_attempt_v1 and recreates the original six-argument function with
-- its EXACT original body, comment, and grants, byte-for-byte identical to
-- supabase/migrations/20260719130000_v68_scenario_attempt_persistence_
-- foundation.sql lines 867-1167.
--
-- CREATE OR REPLACE FUNCTION cannot be used to "downgrade" a seven-argument
-- function back to six arguments any more than the forward migration could
-- use it to go from six to seven (see the contract doc, section 3.1) --
-- PostgreSQL identifies a function by its ordered input-argument TYPE list,
-- and (text,uuid,text,jsonb,text,text,uuid) is a different type list from
-- (text,uuid,text,jsonb,text,text). This rollback therefore DROPs the new
-- signature and CREATEs the original one, exactly mirroring the forward
-- migration's own DROP-then-CREATE structure.
--
-- Restores ONLY this one function's definition. Does not modify, and is not
-- capable of un-writing, any scenario_attempts/scenario_decisions row data
-- created or resumed by callers of the new signature while it was live --
-- rows created with a caller-supplied p_attempt_id remain exactly as they
-- were, with whatever id they were given; this rollback affects the function
-- definition only, never persisted data.
--
-- CORRECTIONS ADDENDUM (Task SIM-PERSIST-V2-02C, applied in the reviewed
-- draft this artifact is copied from)
-- Independent security review (SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_A_
-- SECURITY_REVIEW.md, Area 13) found the same owner-preservation (SA-11-1)
-- and body-fingerprint (SA-12-1) gaps identified in the forward migration
-- draft apply symmetrically to this rollback: dropping and recreating a
-- function resets ownership to whichever role runs the rollback, and
-- checking only the seven-argument signature's identity (not its body)
-- cannot detect whether some other, unreviewed change was applied to it
-- between the forward migration and this rollback. Both are corrected here:
-- section 1 dynamically captures the seven-argument function's exact
-- current owner and verifies material body-fingerprint markers before the
-- DROP; section 3b restores that exact captured owner onto the recreated
-- six-argument function; section 6 verifies the restoration took effect.
--
-- SLICE B CORRECTION (Task SIM-PERSIST-V2-03, discovered via disposable-
-- database validation -- see docs/scenario_simulator/
-- SCENARIO_ENGINE_V2_PERSISTENCE_SLICE_B_DB_VALIDATION_REPORT.md)
-- Empirically confirmed against a real PostgreSQL 15 instance: pg_get_
-- functiondef() never includes the literal text "SECURITY INVOKER" for a
-- SECURITY INVOKER function -- SECURITY INVOKER is PostgreSQL's default,
-- and the deparser (ruleutils.c) only prints the header clause for the
-- non-default case (SECURITY DEFINER). The reviewed draft's v_markers array
-- therefore contained one marker, 'SECURITY INVOKER', that could never
-- match ANY installed SECURITY INVOKER function's pg_get_functiondef()
-- output, including a perfectly correct, undrifted baseline -- a false-
-- positive-against-a-correct-baseline defect, not a real drift-detection
-- gap. This is corrected below by removing that one non-functional text
-- marker and instead verifying SECURITY INVOKER via the actual pg_proc.
-- prosecdef catalog boolean, immediately after the marker loop. No other
-- line of this file differs from the reviewed draft.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Preconditions -- refuse to run against a catalog that does not actually
--    look like "Slice A's migration is currently applied".
-- ---------------------------------------------------------------------------

DO $slice_b_rollback_precheck$
DECLARE
    v_new_oid             oid;
    v_new_owner_name      text;
    v_new_definition      text;
    v_new_definition_norm text;
    v_marker              text;
    -- SA-12-1 correction (SIM-PERSIST-V2-02C), applied symmetrically to the
    -- rollback: material-definition markers proving the installed
    -- seven-argument function is genuinely the Slice-A body this rollback
    -- is written to reverse, not some other, unreviewed change applied to
    -- it after the forward migration ran. Same rationale as the forward
    -- migration draft's own v_markers (no exact pg_get_functiondef() hash
    -- is hardcoded without live-database access -- see that file for the
    -- full explanation); Slice-A-specific markers are included alongside
    -- the markers shared with the pre-Slice-A baseline. SLICE B CORRECTION
    -- (Task SIM-PERSIST-V2-03, discovered via disposable-database
    -- validation): 'SECURITY INVOKER' removed from this list (see file
    -- header) -- verified below via pg_proc.prosecdef instead. A second,
    -- independently discovered defect is also corrected here:
    -- pg_get_functiondef() returns the function's prosrc verbatim, which
    -- means an apostrophe inside a RAISE EXCEPTION message -- itself
    -- written in the original source using SQL's doubled-quote escape
    -- ('' representing one literal apostrophe) -- appears in the deparsed
    -- output as that same literal two-character sequence '' (the escaping
    -- is part of the stored source text, not resolved away). The reviewed
    -- draft's marker for the "caller's existing in_progress attempt"
    -- message used only a single doubled-quote escape (producing one
    -- literal apostrophe at runtime), which can never match the two literal
    -- characters actually present in pg_get_functiondef()'s output. Fixed
    -- below by doubling the escape once more (four quote characters in the
    -- literal, yielding two literal apostrophe characters at runtime) so
    -- the marker's runtime value matches the installed function's source
    -- text exactly.
    v_markers             text[] := ARRAY[
        'invalid_user_email: p_user_email must be a non-empty, non-whitespace email address',
        'attempt_id_collision: the supplied p_attempt_id is already in use',
        'attempt_id_conflict: supplied p_attempt_id does not match the caller''''s existing in_progress attempt for this scenario version',
        'invalid_attempt_id: p_attempt_id must not be the nil UUID',
        'p_attempt_id',
        'terminal_result_snapshot',
        'serialized_engine_state'
    ];
    v_new_prosecdef       boolean;
BEGIN
    v_new_oid := to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)')::oid;
    IF v_new_oid IS NULL THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK PRECONDITION FAILED: the seven-argument public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid) does not exist. Nothing to roll back, or the catalog is in an unexpected state -- refusing to proceed without human review.';
    END IF;

    IF to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)') IS NOT NULL THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK PRECONDITION FAILED: the original six-argument signature already exists alongside the seven-argument one. This is an ambiguous, already-broken catalog state -- refusing to proceed without human review.';
    END IF;

    -- 1b. SA-11-1 correction, applied symmetrically: capture the EXACT
    -- current owner of the seven-argument (Slice-A) function, dynamically,
    -- from the live pg_proc catalog -- never guessed. Section 3b below
    -- restores this exact captured value onto the recreated six-argument
    -- function, correct regardless of which role executes this rollback
    -- and regardless of what role actually owned the function beforehand.
    SELECT pg_get_userbyid(p.proowner) INTO v_new_owner_name
    FROM   pg_proc p
    WHERE  p.oid = v_new_oid;

    IF v_new_owner_name IS NULL THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK PRECONDITION FAILED: could not resolve the current owner of the seven-argument start_or_resume_scenario_attempt_v1 function via pg_proc.proowner. Refusing to proceed without human review -- ownership must never be guessed.';
    END IF;

    PERFORM set_config('slice_a.rollback_captured_owner', v_new_owner_name, true);

    -- 1c. SA-12-1 correction, applied symmetrically: material-definition-
    -- marker check on the installed seven-argument function's body before
    -- it is dropped (see v_markers declaration above for rationale).
    SELECT pg_get_functiondef(v_new_oid) INTO v_new_definition;
    v_new_definition_norm := regexp_replace(v_new_definition, '\s+', ' ', 'g');

    FOREACH v_marker IN ARRAY v_markers LOOP
        IF position(v_marker IN v_new_definition_norm) = 0 THEN
            RAISE EXCEPTION 'SLICE-A ROLLBACK PRECONDITION FAILED: baseline-fingerprint marker not found in the installed seven-argument function body (material definition drift detected): %. Refusing to proceed without human review -- an unreviewed change may have been applied to this function after the forward migration ran.', v_marker;
        END IF;
    END LOOP;

    -- SLICE B CORRECTION: SECURITY INVOKER verified via the actual catalog
    -- boolean (see file header rationale).
    SELECT p.prosecdef INTO v_new_prosecdef FROM pg_proc p WHERE p.oid = v_new_oid;
    IF v_new_prosecdef THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK PRECONDITION FAILED: the installed seven-argument function is SECURITY DEFINER, expected SECURITY INVOKER. Refusing to proceed without human review -- an unreviewed change may have been applied to this function after the forward migration ran.';
    END IF;
END
$slice_b_rollback_precheck$;

-- ---------------------------------------------------------------------------
-- 2. DROP the seven-argument (Slice A) signature.
-- ---------------------------------------------------------------------------

DROP FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text, uuid);

-- ---------------------------------------------------------------------------
-- 3. CREATE the original six-argument signature, with its EXACT original
--    body -- byte-for-byte identical to supabase/migrations/20260719130000_
--    v68_scenario_attempt_persistence_foundation.sql lines 867-1138.
-- ---------------------------------------------------------------------------

CREATE FUNCTION public.start_or_resume_scenario_attempt_v1(
    p_user_email                text,
    p_scenario_version_id       uuid,
    p_initial_current_scene_id  text,
    p_initial_serialized_state  jsonb,
    p_engine_version            text,
    p_scenario_content_sha256   text
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
AS $slice_b_rollback_func$
DECLARE
    v_user_email text;
    v_version    record;
    v_existing   record;
    v_new_id     uuid;
BEGIN
    -- 1. Normalize and validate email.
    v_user_email := NULLIF(BTRIM(LOWER(p_user_email)), '');
    IF v_user_email IS NULL OR v_user_email !~ '@' THEN
        RAISE EXCEPTION 'invalid_user_email: p_user_email must be a non-empty, non-whitespace email address'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_scenario_version_id IS NULL THEN
        RAISE EXCEPTION 'invalid_scenario_version_id: p_scenario_version_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 2 & 3. The scenario version must exist, be published, and its pinned
    -- identity must match the caller's expectation. This RPC never
    -- resolves "the current version" for a simulation itself -- deciding
    -- which scenario_version_id is currently published is
    -- utils/scenario_persistence.py's responsibility, via a direct
    -- service_role SELECT against scenarios/scenario_versions.
    --
    -- SIM-PERSIST-04E: also fetches scenarios.simulation_id and
    -- scenario_versions.version so p_initial_serialized_state.simulationId/
    -- .version can be pinned to the actual database row below, not merely
    -- checked for being "some" non-empty string.
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

    -- 4. Advisory lock covering (learner, scenario version) -- taken before
    -- any scenario_attempts row necessarily exists yet to lock
    -- conventionally with FOR UPDATE.
    PERFORM pg_advisory_xact_lock(hashtext(v_user_email || ':' || p_scenario_version_id::text));

    -- 5. Return an existing in_progress attempt for this exact
    -- (learner, scenario_version_id) pair, if one exists. The
    -- status = 'in_progress' filter structurally guarantees this never
    -- resumes a completed or abandoned attempt.
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
        RETURN QUERY SELECT
            v_existing.id, false, v_existing.scenario_id, v_existing.scenario_version_id,
            v_existing.status, v_existing.current_scene_id, v_existing.next_sequence_number,
            v_existing.serialized_engine_state, v_existing.engine_version, v_existing.scenario_content_sha256,
            v_existing.started_at, v_existing.completed_at, v_existing.abandoned_at,
            v_existing.terminal_ending_id, v_existing.terminal_result_snapshot;
        RETURN;
    END IF;

    -- 6 & 7. Insert exactly one new attempt. ON CONFLICT DO NOTHING
    -- intentionally omits an explicit conflict target because this
    -- RETURNS TABLE function has an output variable named
    -- scenario_version_id; PostgreSQL otherwise treats the conflict-target
    -- identifier as ambiguous between that PL/pgSQL variable and the table
    -- column. If a concurrent caller's INSERT wins the race first, this
    -- INSERT becomes a no-op and the re-SELECT below deterministically
    -- returns the winner's row instead of surfacing a false failure. Any
    -- unrelated unique conflict also falls through to the same re-SELECT
    -- and then raises start_or_resume_failed unless the expected active row
    -- actually exists. The advisory lock above already serializes same-key
    -- callers in the ordinary case; this is defense in depth for that same
    -- guarantee.
    IF NULLIF(BTRIM(p_initial_current_scene_id), '') IS NULL THEN
        RAISE EXCEPTION 'invalid_initial_scene: p_initial_current_scene_id must not be null or empty when creating a new attempt'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    p_initial_current_scene_id := BTRIM(p_initial_current_scene_id);

    IF p_initial_serialized_state IS NULL OR jsonb_typeof(p_initial_serialized_state) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid_initial_state: p_initial_serialized_state must be a JSON object when creating a new attempt'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- SIM-PERSIST-04C/04E snapshot IDENTITY/LIFECYCLE integrity boundary:
    -- pure equality/shape checks against values already validated above,
    -- looked up from the pinned scenario_versions/scenarios row, or
    -- supplied by the caller -- never a computation of which scene, score,
    -- or ending is correct. SIM-PERSIST-04E strengthens this in two ways:
    -- (a) every field's JSON TYPE is checked BEFORE any ->> textual
    -- comparison is trusted (a JSON number or boolean can be silently
    -- coerced to text by ->>, so type-checking first closes that gap), and
    -- (b) simulationId/version are now pinned to the ACTUAL
    -- scenarios.simulation_id / scenario_versions.version row fetched
    -- above, not merely checked for being "some" normalized non-empty
    -- string.
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

    -- SIM-PERSIST-04C RPC-only INSERT guard: generate the new attempt's id
    -- here, name it in the transaction-local guard, and insert it
    -- explicitly -- see the migration header ("Transaction-local INSERT
    -- guards") for the exact, honest scope of this protection.
    v_new_id := gen_random_uuid();
    PERFORM set_config('certbound.scenario_attempt_insert_guard', v_new_id::text, true);

    INSERT INTO public.scenario_attempts (
        id, user_email, scenario_id, scenario_version_id, status,
        current_scene_id, next_sequence_number, serialized_engine_state,
        scenario_content_sha256, engine_version, started_at, updated_at
    )
    VALUES (
        v_new_id, v_user_email, v_version.scenario_id, p_scenario_version_id, 'in_progress',
        p_initial_current_scene_id, 1, p_initial_serialized_state,
        v_version.canonical_content_sha256, v_version.engine_version, now(), now()
    )
    ON CONFLICT DO NOTHING
    RETURNING id INTO v_new_id;

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

    RETURN QUERY SELECT
        v_existing.id, (v_existing.id IS NOT DISTINCT FROM v_new_id), v_existing.scenario_id, v_existing.scenario_version_id,
        v_existing.status, v_existing.current_scene_id, v_existing.next_sequence_number,
        v_existing.serialized_engine_state, v_existing.engine_version, v_existing.scenario_content_sha256,
        v_existing.started_at, v_existing.completed_at, v_existing.abandoned_at,
        v_existing.terminal_ending_id, v_existing.terminal_result_snapshot;
END;
$slice_b_rollback_func$;

-- ---------------------------------------------------------------------------
-- 3b. SA-11-1 correction (SIM-PERSIST-V2-02C), applied symmetrically:
--    restore the EXACT owner captured in section 1b onto the recreated
--    six-argument function -- correct regardless of which role executes
--    this rollback.
-- ---------------------------------------------------------------------------

DO $slice_b_rollback_owner$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) OWNER TO %I',
        current_setting('slice_a.rollback_captured_owner')
    );
END
$slice_b_rollback_owner$;

-- ---------------------------------------------------------------------------
-- 4. Restore the original COMMENT, exactly.
-- ---------------------------------------------------------------------------

COMMENT ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) IS
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
invalid_initial_state_lifecycle respectively. SIM-PERSIST-04E: simulationId
and version are pinned EXACTLY to the fetched scenarios.simulation_id /
scenario_versions.version row (not merely checked for being "some" normalized
non-empty string), and every field''s JSON type is checked before any ->>
textual comparison is trusted. Generates the new attempt''s id itself and
sets the transaction-local certbound.scenario_attempt_insert_guard
immediately before its own INSERT, so a direct service_role INSERT bypassing
this RPC is rejected by trg_guard_scenario_attempt_mutation. Uses
pg_advisory_xact_lock plus an ON CONFLICT DO NOTHING insert against the
partial unique index idx_scenario_attempts_one_in_progress so concurrent
duplicate start requests never surface a false failure. Never resumes a
completed or abandoned attempt. Execute permission: service_role only.';

-- ---------------------------------------------------------------------------
-- 5. Restore the original grants, exactly.
-- ---------------------------------------------------------------------------

REVOKE ALL ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) TO service_role;

-- ---------------------------------------------------------------------------
-- 6. Postconditions.
-- ---------------------------------------------------------------------------

DO $slice_b_rollback_postcheck$
BEGIN
    IF (SELECT count(*) FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'start_or_resume_scenario_attempt_v1') <> 1
    THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK POSTCONDITION FAILED: expected exactly one start_or_resume_scenario_attempt_v1 overload to exist after rollback.';
    END IF;

    IF to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text,uuid)') IS NOT NULL THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK POSTCONDITION FAILED: the seven-argument signature still exists after rollback.';
    END IF;

    IF to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)') IS NULL THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK POSTCONDITION FAILED: the original six-argument signature does not exist after rollback.';
    END IF;

    IF has_function_privilege('anon', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)', 'EXECUTE')
       OR has_function_privilege('authenticated', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)', 'EXECUTE')
    THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK POSTCONDITION FAILED: anon/authenticated must not be able to execute start_or_resume_scenario_attempt_v1.';
    END IF;

    IF NOT has_function_privilege('service_role', 'public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)', 'EXECUTE') THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK POSTCONDITION FAILED: service_role must be able to execute start_or_resume_scenario_attempt_v1.';
    END IF;

    -- SA-11-1 correction, applied symmetrically: the recreated six-argument
    -- function's owner must be byte-for-byte identical to the owner
    -- captured from the seven-argument function in section 1b, proving
    -- section 3b's ALTER FUNCTION ... OWNER TO actually took effect.
    IF (SELECT pg_get_userbyid(p.proowner) FROM pg_proc p
        WHERE p.oid = to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)')::oid)
        IS DISTINCT FROM current_setting('slice_a.rollback_captured_owner')
    THEN
        RAISE EXCEPTION 'SLICE-A ROLLBACK POSTCONDITION FAILED: the recreated six-argument function''s owner (%) does not match the captured pre-rollback owner (%).',
            (SELECT pg_get_userbyid(p.proowner) FROM pg_proc p
             WHERE p.oid = to_regprocedure('public.start_or_resume_scenario_attempt_v1(text,uuid,text,jsonb,text,text)')::oid),
            current_setting('slice_a.rollback_captured_owner');
    END IF;
END
$slice_b_rollback_postcheck$;

-- ---------------------------------------------------------------------------
-- 7. Ask PostgREST to reload its schema cache.
-- ---------------------------------------------------------------------------

NOTIFY pgrst, 'reload schema';

COMMIT;
