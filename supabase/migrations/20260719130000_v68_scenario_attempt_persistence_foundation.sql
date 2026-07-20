-- =============================================================================
-- SIM-PERSIST-04B: V68 — Scenario Simulator learner-attempt persistence foundation
-- Created : 2026-07-19 13:00:00 UTC
--
-- Purpose
-- -------
-- Adds the two tables that persist learner RUNTIME progress against the
-- immutable definitions V66/V67 already established:
--   scenario_attempts   — one row per learner "run" of one permanently
--                          pinned scenario_versions.id
--   scenario_decisions  — append-only, sequence-numbered record of what a
--                          learner actually chose on that run
--
-- utils/scenario_engine.py remains the ONLY implementation of scene
-- transitions, option validity, score/state changes, domain performance,
-- ending selection, and terminal-outcome calculation. Every value this
-- migration's RPCs persist (state, resulting scene, terminal ending,
-- terminal result) is supplied by the caller (the Python persistence
-- adapter, utils/scenario_persistence.py, added in this same task) as
-- already-validated engine output. SQL's job is exclusively:
--   * concurrency-safe persistence (row locking, one-active-attempt
--     uniqueness, advisory locking for the pre-row-exists race)
--   * ownership enforcement (user_email, never auth.uid() -- see below)
--   * append-only decision ordering (sequence_number gap/duplicate
--     prevention)
--   * idempotency (UUIDv4 key + request-fingerprint conflict detection)
--   * permanent immutability of terminal attempts and of every decision
--     once written
-- No scoring, graph-transition, or ending-evaluation logic exists anywhere
-- in this file. SIM-PERSIST-04C added snapshot IDENTITY/LIFECYCLE
-- consistency checks (engineVersion/canonicalContentSha256/simulationId/
-- version/currentSceneId/isComplete/terminalResult) to both RPCs that
-- accept a serialized engine snapshot -- these are pure equality/shape
-- checks against values the CALLER already supplied (never a computation
-- of which scene, score, or ending is correct), so this remains true.
-- SIM-PERSIST-04E further hardened these same checks (see "Implementation
-- addendum (SIM-PERSIST-04E ...)" below) by (a) pinning
-- p_initial_serialized_state.simulationId/.version EXACTLY to the fetched
-- scenarios.simulation_id / scenario_versions.version row, (b) requiring
-- every identity/lifecycle field's JSON TYPE before trusting any ->>
-- textual comparison, and (c) requiring a terminal decision's
-- p_terminal_result_snapshot.endingId to agree with p_terminal_ending_id
-- (terminal_ending_mismatch) -- every one of these remains a pure
-- equality/shape/type check against caller-supplied values, never a
-- computation of which scene, score, or ending is correct.
--
-- Implementation addendum (SIM-PERSIST-04E — final integrity corrections)
-- ---------------------------------------------------------------------
-- Independent review after SIM-PERSIST-04C found four further defects, all
-- corrected in place (same migration file, same timestamp/filename, no new
-- migration):
--   1. start_or_resume_scenario_attempt_v1 now fetches scenarios.
--      simulation_id and scenario_versions.version alongside the
--      already-fetched engine_version/canonical_content_sha256, and pins
--      p_initial_serialized_state.simulationId/.version EXACTLY to those
--      values (previously only checked for being "some" normalized
--      non-empty string, never actually tied to the database row).
--   2. Both RPCs now check every snapshot identity/lifecycle field's JSON
--      TYPE (jsonb_typeof) BEFORE trusting any ->> textual comparison --
--      ->> silently coerces a JSON number or boolean to text, so a caller
--      could previously have smuggled e.g. a JSON number where a string
--      was required.
--   3. submit_scenario_decision_v1 now requires, for a terminal decision,
--      that p_terminal_result_snapshot.endingId is a normalized, non-empty
--      JSON string EXACTLY equal to p_terminal_ending_id
--      (terminal_ending_mismatch) -- these two caller-supplied identities
--      were previously allowed to silently disagree.
--   4. v68_scenario_attempt_persistence_verification.sql's V40 case
--      (renumbered V49) previously supplied a submission that failed an
--      EARLIER validation stage (invalid_expected_scene_id /
--      state_lifecycle_mismatch) before ever reaching the
--      attempt_not_in_progress check it claimed to prove; its inputs are
--      now fully valid through scalar and snapshot validation so the
--      locked attempt-status check is what actually fires.
--
-- Implementation addendum (SIM-PERSIST-04F — concurrency and idempotency
-- closure)
-- ---------------------------------------------------------------------
-- Independent review of the SIM-PERSIST-04E release-candidate bundle found
-- four further defects, all corrected in place (same migration file, same
-- timestamp/filename, no new migration):
--   1. get_scenario_attempt_v1's combined (id, owner) lookup now takes
--      FOR SHARE (never FOR KEY SHARE, which does not conflict with an
--      ordinary non-key UPDATE), held for the rest of the RPC's
--      transaction -- previously, at READ COMMITTED isolation, a concurrent
--      submit_scenario_decision_v1/abandon_scenario_attempt_v1 call could
--      commit between this RPC's attempt SELECT and its decisions SELECT,
--      so it could return the attempt's PRE-commit fields alongside the
--      POST-commit decision history. This RPC remains read-only with
--      respect to stored data.
--   2. submit_scenario_decision_v1's idempotent-retry check previously
--      compared only request_fingerprint -- now every stored request field
--      (sequence_number, expected_scene_id, selected_option_id,
--      state_before, state_after, resulting_scene_id, is_terminal,
--      terminal_ending_id) must also be IS NOT DISTINCT FROM the current
--      call's corresponding parameter, so a matching fingerprint can no
--      longer mask an otherwise-different request; any disagreement raises
--      idempotency_key_conflict instead of silently replaying the original
--      decision's result. utils/scenario_persistence.py now always computes
--      the canonical fingerprint from the validated request itself and
--      rejects (request_fingerprint_mismatch, without calling the RPC) any
--      caller-supplied fingerprint that disagrees with it.
--   3. submit_scenario_decision_v1 now requires a terminal decision's
--      state_after.currentSceneId to be EXPLICITLY present as a JSON null
--      (jsonb_typeof(...) = 'null') -- previously a state_after object that
--      omitted the key entirely was silently accepted as equivalent to an
--      explicit null.
--   4. utils/scenario_persistence.py's _require_nonempty_str (used for
--      scene ids, option ids, ending ids, and engine-version text) no
--      longer calls str(value or "") -- it now requires an actual str
--      up front, so an integer, bool, UUID object, or other non-string
--      value is rejected rather than silently stringified into something
--      that could then pass format validation.
--
-- V1 scope decisions (explicit; see scenario_content/docs/
-- SCENARIO_ATTEMPT_PERSISTENCE_DESIGN.md for the fuller architecture
-- discussion this migration narrows down from)
-- -----------------------------------------------------------------------
-- * Exactly four RPCs: start_or_resume_scenario_attempt_v1,
--   get_scenario_attempt_v1, submit_scenario_decision_v1,
--   abandon_scenario_attempt_v1. There is NO standalone
--   complete_scenario_attempt_v1 -- completion happens atomically inside
--   submit_scenario_decision_v1's terminal branch, in the same statement
--   and transaction as the terminal decision's own INSERT, so no window
--   ever exists where a terminal decision is durably recorded but the
--   attempt is still in_progress. A standalone completion/reconciliation
--   RPC is deferred until a real recovery use case is observed in
--   production.
-- * Ownership is public.scenario_attempts.user_email, normalized exactly
--   as lower(btrim(user_email)) -- the same identity and normalization
--   already used by this application's pre-existing exam_attempts /
--   question_attempts tables (see utils/question_selection.py) and by
--   utils/access_control.py's signed-session model. There is no
--   auth.uid()-based policy anywhere in this migration; this repository's
--   Python backend always connects with the service_role key, which
--   bypasses RLS entirely (see the V66/V67 migration headers for the full
--   reasoning, unchanged here).
-- * Every attempt is permanently pinned to exactly one
--   scenario_versions.id at creation (scenario_version_id is immutable
--   after INSERT). A newer publication of the same scenario never affects
--   an already-created attempt.
-- * At most one in_progress attempt may exist per
--   (user_email, scenario_version_id) pair -- enforced by a partial unique
--   index, not merely application logic.
-- * scenario_attempts.serialized_engine_state is the attempt's current,
--   engine-produced runtime state (a JSON object). It is the same value
--   submit_scenario_decision_v1's caller supplies as "state after" on the
--   most recent decision; the RPC also uses it as an integrity check --
--   a decision submission's supplied "state before" must exactly match
--   the persisted serialized_engine_state before that decision is
--   accepted (see submit_scenario_decision_v1 step 9), which detects a
--   caller acting on a stale/incorrect view of the run.
--
-- Immutability model
-- -------------------
-- scenario_attempts.status is 'in_progress', 'completed', or 'abandoned'.
-- A BEFORE UPDATE OR DELETE trigger (guard_scenario_attempt_mutation_v1)
-- enforces:
--   * DELETE is rejected unconditionally, for every status (V1 defines no
--     hard-delete operation at all; this is also already structurally
--     impossible via grants -- see section 7 -- but the trigger adds
--     defense in depth exactly as this repository's V66/V67 lesson
--     recommends).
--   * Any UPDATE of a row whose CURRENT status is already 'completed' or
--     'abandoned' is rejected unconditionally, with no guard able to
--     override it -- these two states are permanently terminal, including
--     against the RPCs themselves. There is no reopening path in V1.
--   * Every UPDATE of a still-in_progress row (whether it only advances
--     per-decision runtime state, or also performs the one-way transition
--     into 'completed' or 'abandoned') is rejected UNLESS the
--     transaction-local mutation guard (see below) names the exact row
--     being updated.
--   * The immutable identity columns (user_email, scenario_id,
--     scenario_version_id, started_at, scenario_content_sha256,
--     engine_version) may never change, even while still in_progress and
--     even under a valid guard.
--
-- scenario_decisions has no lifecycle at all -- every row is permanently
-- append-only from the moment it is inserted. This is enforced twice,
-- independently: service_role is granted only SELECT/INSERT on this table
-- (no UPDATE, no DELETE grant exists at all -- section 7), and a second
-- BEFORE UPDATE OR DELETE trigger (guard_scenario_decision_immutability_v1)
-- unconditionally rejects both operations regardless of any guard.
--
-- Transaction-local mutation guard -- what it is and is NOT
-- -------------------------------------------------------------
-- submit_scenario_decision_v1 and abandon_scenario_attempt_v1 each call
-- set_config('certbound.scenario_attempt_mutation_guard', <attempt id>,
-- is_local => true) once, immediately before the one UPDATE statement each
-- performs against scenario_attempts, after every validation in that RPC
-- has already passed. guard_scenario_attempt_mutation_v1 reads that same
-- setting. is_local = true means Postgres clears it automatically at the
-- end of the current transaction (commit OR rollback).
--
-- This is an APPLICATION/RPC MUTATION-BOUNDARY SAFEGUARD for normal
-- service_role API usage (the Python backend calling these RPCs through
-- the ordinary Supabase client, the same way every other RPC in this
-- repository is called) -- it makes an accidental or buggy *raw*
-- UPDATE issued over that same service_role connection fail instead of
-- silently corrupting attempt state. It is explicitly NOT a defense
-- against a database administrator, a superuser, or any other actor with
-- the ability to execute arbitrary trusted SQL inside the *same*
-- transaction as a legitimate RPC call -- such an actor could, in
-- principle, issue their own set_config(..., true) call. Real protection
-- against that class of actor is out of scope for a single migration and
-- depends on infrastructure-level controls (who holds service_role
-- credentials, network boundaries, connection auditing), not a custom GUC.
-- This is the identical, honestly-scoped guarantee already documented for
-- certbound.publish_scenario_version_guard in the V66 migration header.
--
-- Transaction-local INSERT guards (added by SIM-PERSIST-04C)
-- ------------------------------------------------------------
-- The UPDATE/DELETE guards above do not, by themselves, stop a direct
-- service_role INSERT into scenario_attempts or scenario_decisions from
-- bypassing all four RPCs entirely -- an accidental or buggy raw INSERT
-- issued over the same service_role connection could otherwise create a row
-- the RPCs never validated. Both guarded tables now also fire their guard
-- trigger BEFORE INSERT:
--   * start_or_resume_scenario_attempt_v1 generates the new attempt's uuid
--     in Python-visible SQL (gen_random_uuid(), assigned to a local
--     variable) BEFORE its INSERT, calls
--     set_config('certbound.scenario_attempt_insert_guard', <that uuid>,
--     is_local => true), and then inserts that exact id explicitly (never
--     relying on the column DEFAULT for this one INSERT).
--   * submit_scenario_decision_v1 does the identical thing for the new
--     decision's uuid via 'certbound.scenario_decision_insert_guard'.
--   * guard_scenario_attempt_mutation_v1 / guard_scenario_decision_
--     immutability_v1 (renamed in intent, not in name -- see section 5A/5B)
--     each reject a BEFORE INSERT firing unless the matching guard names
--     NEW.id exactly, with a focused attempt_insert_guard_violation /
--     decision_insert_guard_violation exception.
-- Exactly the same honest scope as every other guard in this migration:
-- this is an APPLICATION/RPC MUTATION-BOUNDARY SAFEGUARD for normal
-- service_role API usage, catching an accidental/buggy raw INSERT over the
-- ordinary service_role connection -- it is NOT a defense against a
-- database administrator, superuser, or any other actor able to execute
-- arbitrary trusted SQL inside the same transaction as a legitimate RPC
-- call (such an actor could set_config(...) themselves, exactly as already
-- true for the UPDATE/DELETE guards above).
--
-- Idempotency
-- ------------
-- Python generates a UUIDv4 idempotency_key per decision submission
-- attempt. Uniqueness is scoped to (attempt_id, idempotency_key) --
-- meaningful only within the one attempt it protects a mutation on, per
-- SCENARIO_ATTEMPT_PERSISTENCE_DESIGN.md section 7.2. A retry supplying
-- the same (attempt_id, idempotency_key) as an already-committed decision
-- is safe and returns that decision's result unchanged WHEN its
-- request_fingerprint also matches -- AND, as of SIM-PERSIST-04F, when
-- every other stored request field (sequence_number, expected_scene_id,
-- selected_option_id, state_before, state_after, resulting_scene_id,
-- is_terminal, terminal_ending_id) is IS NOT DISTINCT FROM the current
-- call's corresponding parameter, so a coincidentally- or maliciously-
-- matching fingerprint can never mask an otherwise-different request. Any
-- disagreement -- in the fingerprint or in any bound request field -- is
-- rejected with the focused idempotency_key_conflict exception rather than
-- either silently reusing the old result or silently inserting a second
-- row. The fingerprint itself is an opaque, Python-computed
-- 64-lowercase-hex string (see utils/scenario_persistence.py for the exact
-- deterministic formula, which as of SIM-PERSIST-04C also explicitly
-- covers terminal_result_snapshot, and which as of SIM-PERSIST-04F the
-- Python adapter always computes itself from the validated request rather
-- than trusting a caller-supplied value that merely matches FORMAT); this
-- migration only validates its FORMAT (scenario_decisions_fingerprint_format
-- below) and compares it with IS DISTINCT FROM -- it is never recomputed or
-- independently derived in SQL.
--
-- Stable idempotent replay (corrected by SIM-PERSIST-04C)
-- ----------------------------------------------------------
-- A safe retry (matching idempotency_key AND matching request_fingerprint)
-- now returns the ORIGINAL committed post-decision result, derived from the
-- immutable scenario_decisions row itself, NEVER the attempt's current
-- (possibly since-advanced-by-a-later-decision) state. This matters because
-- scenario_attempts is a single mutable row that keeps advancing with every
-- subsequent decision -- a retry of an OLDER decision must keep returning
-- that older decision's own result forever, not whatever the attempt looks
-- like by the time the retry arrives. Concretely: for a non-terminal
-- decision, attempt_status is hardcoded 'in_progress', current_scene_id is
-- the decision's own stored resulting_scene_id, next_sequence_number is the
-- decision's own stored sequence_number + 1, and serialized_engine_state is
-- the decision's own stored state_after -- all read from
-- scenario_decisions, an append-only table no later decision ever touches.
-- For a terminal decision, attempt_status is hardcoded 'completed',
-- current_scene_id is NULL, next_sequence_number is likewise
-- sequence_number + 1, serialized_engine_state is the decision's own stored
-- state_after, and terminal_ending_id/terminal_result_snapshot/completed_at
-- are read from the (permanently immutable, once completed)
-- scenario_attempts row -- safe to read from there specifically because a
-- completed attempt can never change again.
--
-- Security model
-- ---------------
-- Identical posture to V66/V67, corrected for the exact production defect
-- SIM-PERSIST-04C's review found already exists for V66 (scenarios/
-- scenario_versions there revoke only from PUBLIC, silently relying on
-- "no GRANT was ever issued" to keep anon/authenticated/service_role at
-- zero rather than saying so explicitly). Here every one of PUBLIC, anon,
-- authenticated, AND service_role has ALL privileges explicitly revoked on
-- both new tables FIRST, and only then is the minimum needed privilege set
-- explicitly re-granted to service_role (section 7) -- never relying on "no
-- grant was ever issued" as an implicit substitute for a revoke. RLS is
-- enabled on both new tables with ZERO anon/authenticated policies. No
-- auth.uid()-based policy is added. Table/role ownership (the object owner,
-- typically `postgres`) is never touched by any REVOKE/GRANT in this
-- migration -- ownership privileges are implicit and are not affected by
-- revoking/granting privileges to other named roles.
--
-- Migration-history note (repeated from V67; still true)
-- ---------------------------------------------------------
-- V66 and V67 were manually applied to production, not through a tracked
-- migration runner. Production currently has NO
-- supabase_migrations.schema_migrations table at all. This V68 migration
-- deliberately does NOT create, backfill, or repair that schema/table, and
-- does not attempt to reconcile migration history for V66, V67, or itself.
-- Migration-history onboarding remains a separate, repository-wide task,
-- explicitly out of scope here, exactly as stated in the V67 header.
--
-- Atomicity
-- ----------
-- The entire migration is wrapped in an explicit BEGIN; ... COMMIT;
-- transaction block, matching this repository's established convention
-- for an atomic multi-statement migration (see V67 and
-- supabase/migrations/20260623182200_v44_backfill_question_versions.sql).
-- The precondition checks below run as the FIRST statements inside that
-- transaction, so a precondition failure aborts the entire migration
-- before any table, trigger, grant, or function is created -- this file
-- either applies completely or has no effect at all. No CREATE INDEX
-- CONCURRENTLY or other non-transactional DDL is used anywhere below.
--
-- Do not execute this migration. It is local and unexecuted, exactly like
-- every other artifact produced by SIM-PERSIST-02/02A/03/03A/04A/04B.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Preconditions -- fail explicitly and atomically if any prerequisite
--    V66/V67 object is missing, or if any object this migration is about
--    to create already exists under the same name. Never silently
--    tolerates either condition, never recreates or drops anything.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_count int;
BEGIN
    -- 1a. Required V66/V67 foundation objects must already exist.
    IF to_regclass('public.scenarios') IS NULL THEN
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: public.scenarios does not exist. This migration requires the V66 foundation (and V67 hardening) to already be installed.';
    END IF;

    IF to_regclass('public.scenario_versions') IS NULL THEN
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: public.scenario_versions does not exist. This migration requires the V66 foundation (and V67 hardening) to already be installed.';
    END IF;

    IF to_regprocedure('public.publish_scenario_version_v1(uuid,jsonb,text)') IS NULL THEN
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: public.publish_scenario_version_v1(uuid,jsonb,text) does not exist.';
    END IF;

    IF to_regprocedure('public.guard_scenario_current_published_version_v1()') IS NULL THEN
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: public.guard_scenario_current_published_version_v1() does not exist.';
    END IF;

    IF to_regprocedure('public.guard_scenario_version_immutability_v1()') IS NULL THEN
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: public.guard_scenario_version_immutability_v1() does not exist.';
    END IF;

    -- 1b. Nothing this migration creates must already exist.
    IF to_regclass('public.scenario_attempts') IS NOT NULL THEN
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: public.scenario_attempts already exists. Object-name conflict -- this migration cannot be applied without review.';
    END IF;

    IF to_regclass('public.scenario_decisions') IS NOT NULL THEN
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: public.scenario_decisions already exists. Object-name conflict -- this migration cannot be applied without review.';
    END IF;

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
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: one or more of the six functions this migration creates already exists (% found). Object-name conflict -- this migration cannot be applied without review.', v_count;
    END IF;

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
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: one or both intended trigger names already exist. Object-name conflict -- this migration cannot be applied without review.';
    END IF;

    SELECT count(*) INTO v_count
    FROM   pg_indexes
    WHERE  schemaname = 'public'
    AND    indexname IN (
        'idx_scenario_attempts_one_in_progress',
        'idx_scenario_attempts_scenario_version_id',
        'idx_scenario_attempts_user_email_status'
    );
    IF v_count > 0 THEN
        RAISE EXCEPTION 'V68 PRECONDITION FAILED: one or more intended V68 index names already exist. Object-name conflict -- this migration cannot be applied without review.';
    END IF;

    RAISE NOTICE 'V68 PRECONDITIONS PASSED: V66/V67 foundation objects exist, and no intended V68 object name is already in use.';
END;
$$;


-- ---------------------------------------------------------------------------
-- 2. public.scenario_attempts
-- ---------------------------------------------------------------------------

CREATE TABLE public.scenario_attempts (
    id                        uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_email                text         NOT NULL,
    scenario_id               uuid         NOT NULL REFERENCES public.scenarios (id) ON DELETE RESTRICT,
    scenario_version_id       uuid         NOT NULL,
    status                    text         NOT NULL DEFAULT 'in_progress',
    current_scene_id          text,
    next_sequence_number      integer      NOT NULL DEFAULT 1,
    serialized_engine_state   jsonb        NOT NULL,
    scenario_content_sha256   text         NOT NULL,
    engine_version            text         NOT NULL,
    started_at                timestamptz  NOT NULL DEFAULT now(),
    updated_at                timestamptz  NOT NULL DEFAULT now(),
    completed_at              timestamptz,
    abandoned_at              timestamptz,
    terminal_ending_id        text,
    terminal_result_snapshot  jsonb,

    -- scenario_id/scenario_version_id must refer to the same scenario.
    -- Reuses the exact composite-FK pattern V66 established for
    -- scenarios.current_published_version_id -- scenario_versions
    -- (scenario_id, id) already has a UNIQUE constraint
    -- (scenario_versions_scenario_id_id_unique) supporting this.
    CONSTRAINT scenario_attempts_scenario_version_fk
        FOREIGN KEY (scenario_id, scenario_version_id)
        REFERENCES public.scenario_versions (scenario_id, id)
        ON DELETE RESTRICT,

    CONSTRAINT scenario_attempts_user_email_normalized
        CHECK (user_email = LOWER(BTRIM(user_email)) AND user_email <> ''),

    CONSTRAINT scenario_attempts_status_valid
        CHECK (status IN ('in_progress', 'completed', 'abandoned')),

    CONSTRAINT scenario_attempts_current_scene_id_normalized
        CHECK (current_scene_id IS NULL OR (current_scene_id = BTRIM(current_scene_id) AND current_scene_id <> '')),

    CONSTRAINT scenario_attempts_next_sequence_number_positive
        CHECK (next_sequence_number >= 1),

    CONSTRAINT scenario_attempts_serialized_engine_state_is_object
        CHECK (jsonb_typeof(serialized_engine_state) = 'object'),

    CONSTRAINT scenario_attempts_scenario_content_sha256_format
        CHECK (scenario_content_sha256 ~ '^[0-9a-f]{64}$'),

    CONSTRAINT scenario_attempts_engine_version_normalized
        CHECK (engine_version = BTRIM(engine_version) AND engine_version <> ''),

    CONSTRAINT scenario_attempts_terminal_ending_id_normalized
        CHECK (terminal_ending_id IS NULL OR (terminal_ending_id = BTRIM(terminal_ending_id) AND terminal_ending_id <> '')),

    CONSTRAINT scenario_attempts_terminal_result_snapshot_is_object
        CHECK (terminal_result_snapshot IS NULL OR jsonb_typeof(terminal_result_snapshot) = 'object'),

    -- completed <=> completed_at set; abandoned <=> abandoned_at set.
    -- Together these two equivalences also force in_progress to have
    -- NEITHER timestamp, and force completed/abandoned to be mutually
    -- exclusive (status can only ever hold one value at a time).
    CONSTRAINT scenario_attempts_completed_consistency
        CHECK ((status = 'completed') = (completed_at IS NOT NULL)),

    CONSTRAINT scenario_attempts_abandoned_consistency
        CHECK ((status = 'abandoned') = (abandoned_at IS NOT NULL)),

    CONSTRAINT scenario_attempts_completed_at_after_started
        CHECK (completed_at IS NULL OR completed_at >= started_at),

    CONSTRAINT scenario_attempts_abandoned_at_after_started
        CHECK (abandoned_at IS NULL OR abandoned_at >= started_at),

    -- A completed attempt always carries the terminal result the engine
    -- produced at the moment it completed; abandoned and in_progress
    -- attempts must never carry one.
    CONSTRAINT scenario_attempts_completed_requires_terminal_result
        CHECK (status <> 'completed' OR (terminal_ending_id IS NOT NULL AND terminal_result_snapshot IS NOT NULL)),

    CONSTRAINT scenario_attempts_abandoned_has_no_terminal_result
        CHECK (status <> 'abandoned' OR (terminal_ending_id IS NULL AND terminal_result_snapshot IS NULL)),

    CONSTRAINT scenario_attempts_in_progress_has_no_terminal_result
        CHECK (status <> 'in_progress' OR (terminal_ending_id IS NULL AND terminal_result_snapshot IS NULL))
);

COMMENT ON TABLE public.scenario_attempts IS
'One row per learner run of one permanently pinned scenario_versions.id.
status is in_progress, completed, or abandoned only -- both completed and
abandoned are permanent terminal states enforced immutable by
trg_guard_scenario_attempt_mutation; there is no reopening path in V1.
serialized_engine_state is the current engine-produced runtime state (a
JSON object) and doubles as the integrity baseline submit_scenario_decision_v1
checks a decision''s "state before" against. user_email (normalized
lower(btrim(...))) is the ownership key -- the same identity already used by
this application''s pre-existing exam_attempts/question_attempts tables; this
migration adds no auth.uid()-based policy. scenario_id and scenario_version_id
are immutable after creation and are cross-checked against each other via
scenario_attempts_scenario_version_fk, the same composite-FK pattern V66 uses
for scenarios.current_published_version_id.';


-- ---------------------------------------------------------------------------
-- 3. public.scenario_decisions
-- ---------------------------------------------------------------------------

CREATE TABLE public.scenario_decisions (
    id                    uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id            uuid         NOT NULL REFERENCES public.scenario_attempts (id) ON DELETE RESTRICT,
    sequence_number       integer      NOT NULL,
    idempotency_key       uuid         NOT NULL,
    request_fingerprint   text         NOT NULL,
    expected_scene_id     text         NOT NULL,
    selected_option_id    text         NOT NULL,
    state_before          jsonb        NOT NULL,
    state_after           jsonb        NOT NULL,
    resulting_scene_id    text,
    is_terminal           boolean      NOT NULL,
    terminal_ending_id    text,
    created_at            timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT scenario_decisions_attempt_id_sequence_number_unique
        UNIQUE (attempt_id, sequence_number),

    CONSTRAINT scenario_decisions_attempt_id_idempotency_key_unique
        UNIQUE (attempt_id, idempotency_key),

    CONSTRAINT scenario_decisions_sequence_number_positive
        CHECK (sequence_number >= 1),

    CONSTRAINT scenario_decisions_request_fingerprint_format
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),

    CONSTRAINT scenario_decisions_expected_scene_id_normalized
        CHECK (expected_scene_id = BTRIM(expected_scene_id) AND expected_scene_id <> ''),

    CONSTRAINT scenario_decisions_selected_option_id_normalized
        CHECK (selected_option_id = BTRIM(selected_option_id) AND selected_option_id <> ''),

    CONSTRAINT scenario_decisions_resulting_scene_id_normalized
        CHECK (resulting_scene_id IS NULL OR (resulting_scene_id = BTRIM(resulting_scene_id) AND resulting_scene_id <> '')),

    CONSTRAINT scenario_decisions_terminal_ending_id_normalized
        CHECK (terminal_ending_id IS NULL OR (terminal_ending_id = BTRIM(terminal_ending_id) AND terminal_ending_id <> '')),

    CONSTRAINT scenario_decisions_state_before_is_object
        CHECK (jsonb_typeof(state_before) = 'object'),

    CONSTRAINT scenario_decisions_state_after_is_object
        CHECK (jsonb_typeof(state_after) = 'object'),

    -- Terminal-field consistency: a terminal decision has no resulting
    -- scene (the run ends) and must carry a terminal_ending_id; a
    -- non-terminal decision must carry a resulting scene and must not
    -- carry a terminal_ending_id.
    CONSTRAINT scenario_decisions_terminal_fields_consistent
        CHECK (
            (is_terminal     AND resulting_scene_id IS NULL     AND terminal_ending_id IS NOT NULL)
            OR
            (NOT is_terminal AND resulting_scene_id IS NOT NULL AND terminal_ending_id IS NULL)
        )
);

COMMENT ON TABLE public.scenario_decisions IS
'Append-only, sequence-numbered record of what a learner chose on one
scenario_attempts row. Every row is permanently immutable from the moment it
is inserted, enforced twice independently: service_role has only
SELECT/INSERT on this table (section 7), and
trg_guard_scenario_decision_immutability unconditionally rejects any UPDATE
or DELETE regardless of that. idempotency_key is a Python-generated UUIDv4;
request_fingerprint is an opaque, Python-computed 64-lowercase-hex string
this migration validates only by FORMAT and by IS DISTINCT FROM comparison
against a retried submission''s own recomputed value -- it is never
recomputed or independently derived in SQL. Does not store state deltas,
domain/correctness audit columns, or scenario content -- state_before/
state_after are the only per-decision state persisted, and the attempt''s
pinned scenario_version_id is the sole reference to immutable content.';


-- ---------------------------------------------------------------------------
-- 4. Indexes
-- ---------------------------------------------------------------------------

-- One-active-attempt guarantee: at most one in_progress attempt may exist
-- per (user_email, scenario_version_id) pair. Structurally identical to
-- idx_free_mock_sets_one_draft / idx_free_mock_sets_one_published in
-- supabase/migrations/20260629120000_v46_free_mock_curation_foundation.sql.
CREATE UNIQUE INDEX IF NOT EXISTS idx_scenario_attempts_one_in_progress
    ON public.scenario_attempts (user_email, scenario_version_id)
    WHERE status = 'in_progress';

CREATE INDEX IF NOT EXISTS idx_scenario_attempts_scenario_version_id
    ON public.scenario_attempts (scenario_version_id);

CREATE INDEX IF NOT EXISTS idx_scenario_attempts_user_email_status
    ON public.scenario_attempts (user_email, status);

-- scenario_decisions needs no additional indexes: both unique constraints
-- above already provide supporting indexes for "all decisions for one
-- attempt, ordered by sequence_number" and for the idempotency-key lookup.


-- ---------------------------------------------------------------------------
-- 5A. Immutability + mutation-boundary trigger — public.scenario_attempts
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.guard_scenario_attempt_mutation_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_guard text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- SIM-PERSIST-04C: reject any INSERT that did not go through
        -- start_or_resume_scenario_attempt_v1, which is the only caller
        -- that ever calls set_config('certbound.scenario_attempt_insert_
        -- guard', <new id>, true) immediately before its own INSERT.
        v_guard := current_setting('certbound.scenario_attempt_insert_guard', true);
        IF v_guard IS NULL OR v_guard <> NEW.id::text THEN
            RAISE EXCEPTION 'attempt_insert_guard_violation: scenario_attempts % may only be inserted by public.start_or_resume_scenario_attempt_v1 (insert guard not set for %)', NEW.id, NEW.id
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'scenario_attempts % cannot be deleted; V1 defines no hard-delete operation for any lifecycle state', OLD.id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- TG_OP = 'UPDATE' from here on.

    IF OLD.status IN ('completed', 'abandoned') THEN
        RAISE EXCEPTION 'scenario_attempts % is % and is permanently immutable; it can never be updated or reopened', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- OLD.status = 'in_progress' from here on. Every legitimate mutation of
    -- an in_progress row -- whether it only advances per-decision runtime
    -- state, or also performs the one-way transition into 'completed' or
    -- 'abandoned' -- must be authorized by the transaction-local guard set
    -- by submit_scenario_decision_v1 or abandon_scenario_attempt_v1
    -- immediately before their UPDATE. See the migration header
    -- ("Transaction-local mutation guard -- what it is and is NOT") for the
    -- exact, honest scope of this protection.
    v_guard := current_setting('certbound.scenario_attempt_mutation_guard', true);
    IF v_guard IS NULL OR v_guard <> OLD.id::text THEN
        RAISE EXCEPTION 'scenario_attempts % may only be mutated by public.submit_scenario_decision_v1 or public.abandon_scenario_attempt_v1 (mutation guard not set for %)', OLD.id, OLD.id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF NEW.user_email IS DISTINCT FROM OLD.user_email
       OR NEW.scenario_id IS DISTINCT FROM OLD.scenario_id
       OR NEW.scenario_version_id IS DISTINCT FROM OLD.scenario_version_id
       OR NEW.started_at IS DISTINCT FROM OLD.started_at
       OR NEW.scenario_content_sha256 IS DISTINCT FROM OLD.scenario_content_sha256
       OR NEW.engine_version IS DISTINCT FROM OLD.engine_version
    THEN
        RAISE EXCEPTION 'scenario_attempts % identity columns (user_email, scenario_id, scenario_version_id, started_at, scenario_content_sha256, engine_version) are immutable after creation', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status = 'in_progress' THEN
        IF NEW.completed_at IS NOT NULL OR NEW.abandoned_at IS NOT NULL
           OR NEW.terminal_ending_id IS NOT NULL OR NEW.terminal_result_snapshot IS NOT NULL
        THEN
            RAISE EXCEPTION 'scenario_attempts % cannot carry terminal fields while status remains in_progress', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status = 'completed' THEN
        IF NEW.completed_at IS NULL OR NEW.abandoned_at IS NOT NULL
           OR NEW.terminal_ending_id IS NULL OR NEW.terminal_result_snapshot IS NULL
        THEN
            RAISE EXCEPTION 'scenario_attempts % completion requires completed_at, terminal_ending_id, and terminal_result_snapshot, and must not set abandoned_at', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status = 'abandoned' THEN
        IF NEW.abandoned_at IS NULL OR NEW.completed_at IS NOT NULL
           OR NEW.terminal_ending_id IS NOT NULL OR NEW.terminal_result_snapshot IS NOT NULL
        THEN
            RAISE EXCEPTION 'scenario_attempts % abandonment requires abandoned_at and must not set completed_at or any terminal result field', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'scenario_attempts % has an unrecognized status transition to %', OLD.id, NEW.status
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

COMMENT ON FUNCTION public.guard_scenario_attempt_mutation_v1() IS
'BEFORE INSERT OR UPDATE OR DELETE trigger on public.scenario_attempts.
INSERT is rejected unless the transaction-local guard
certbound.scenario_attempt_insert_guard names NEW.id exactly (set only
inside start_or_resume_scenario_attempt_v1, immediately before its own
INSERT). DELETE is rejected unconditionally for every status. Any UPDATE of
a row whose CURRENT status is already completed or abandoned is rejected
unconditionally -- both are permanent, with no reopening path. Every UPDATE
of a still-in_progress row (state-advance-only, or the one-way transition
into completed/abandoned) requires the transaction-local guard
certbound.scenario_attempt_mutation_guard (set only inside
submit_scenario_decision_v1/abandon_scenario_attempt_v1) to name the exact
row id, and the identity columns may never change. Every one of these guards
is an application/RPC mutation-boundary safeguard for normal service_role
API usage, not a defense against arbitrary trusted SQL in the same
transaction -- see the migration header for the precise scope of this
guarantee.';

DROP TRIGGER IF EXISTS trg_guard_scenario_attempt_mutation ON public.scenario_attempts;
CREATE TRIGGER trg_guard_scenario_attempt_mutation
    BEFORE INSERT OR UPDATE OR DELETE ON public.scenario_attempts
    FOR EACH ROW EXECUTE FUNCTION public.guard_scenario_attempt_mutation_v1();


-- ---------------------------------------------------------------------------
-- 5B. Append-only trigger — public.scenario_decisions
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.guard_scenario_decision_immutability_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_guard text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- SIM-PERSIST-04C: reject any INSERT that did not go through
        -- submit_scenario_decision_v1, which is the only caller that ever
        -- calls set_config('certbound.scenario_decision_insert_guard',
        -- <new id>, true) immediately before its own INSERT.
        v_guard := current_setting('certbound.scenario_decision_insert_guard', true);
        IF v_guard IS NULL OR v_guard <> NEW.id::text THEN
            RAISE EXCEPTION 'decision_insert_guard_violation: scenario_decisions % may only be inserted by public.submit_scenario_decision_v1 (insert guard not set for %)', NEW.id, NEW.id
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'scenario_decisions % is append-only and can never be deleted', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RAISE EXCEPTION 'scenario_decisions % is append-only and can never be updated', OLD.id
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

COMMENT ON FUNCTION public.guard_scenario_decision_immutability_v1() IS
'BEFORE INSERT OR UPDATE OR DELETE trigger on public.scenario_decisions.
INSERT is rejected unless the transaction-local guard
certbound.scenario_decision_insert_guard names NEW.id exactly (set only
inside submit_scenario_decision_v1, immediately before its own INSERT).
UPDATE and DELETE are unconditionally rejected for every row, with no guard
variable and no exception to that -- unlike scenario_attempts (which has a
legitimate, guarded UPDATE lifecycle), a scenario_decisions row has no
legitimate mutation path at all once inserted. Defense in depth alongside
the grant layer (section 7), which never grants UPDATE or DELETE to
service_role on this table at all.';

DROP TRIGGER IF EXISTS trg_guard_scenario_decision_immutability ON public.scenario_decisions;
CREATE TRIGGER trg_guard_scenario_decision_immutability
    BEFORE INSERT OR UPDATE OR DELETE ON public.scenario_decisions
    FOR EACH ROW EXECUTE FUNCTION public.guard_scenario_decision_immutability_v1();


-- ---------------------------------------------------------------------------
-- 6. Row Level Security — enabled, no anon/authenticated policies.
--
-- service_role BYPASSES RLS entirely (standard Postgres/Supabase behavior).
-- The real security boundary is: service_role credentials are server-only
-- (see utils/access_control.py) and anon/authenticated have zero table or
-- function grants below, so even a client holding only the anon/
-- authenticated key cannot reach these tables or any of the four RPCs at
-- all, RLS aside. No auth.uid()-based policy is defined.
-- ---------------------------------------------------------------------------

ALTER TABLE public.scenario_attempts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scenario_decisions ENABLE ROW LEVEL SECURITY;


-- ---------------------------------------------------------------------------
-- 7. Grants — EXPLICIT revoke from every relevant role, THEN an explicit
--    minimum re-grant to service_role only.
--
-- SIM-PERSIST-04C correction: the original version of this migration only
-- revoked from PUBLIC, then granted to service_role -- relying on anon,
-- authenticated, and service_role never having been otherwise granted a
-- privilege to keep them at zero (beyond the intended service_role grant).
-- That silent reliance is the EXACT production defect already found once
-- in V66 (scenarios/scenario_versions), so this section instead explicitly
-- REVOKEs ALL from PUBLIC, anon, authenticated, AND service_role FIRST
-- (making every role's starting privilege set zero, explicitly, regardless
-- of history), and only THEN grants back the exact minimum service_role
-- needs. Table/role ownership (the object owner) is never targeted by any
-- REVOKE/GRANT below and is therefore never affected.
--
-- scenario_attempts: SELECT, INSERT, UPDATE. No DELETE, no TRUNCATE, no
-- REFERENCES, no TRIGGER -- V1 defines no hard-delete operation at all (see
-- guard_scenario_attempt_mutation_v1, which would reject any DELETE attempt
-- regardless, but the grant is withheld too, exactly as V67's lesson
-- recommends: never rely on a trigger alone when the grant itself can
-- simply not exist), and nothing in V1 needs to reference this table as an
-- FK target, truncate it, or attach a new trigger to it from outside this
-- migration.
--
-- scenario_decisions: SELECT, INSERT only. No UPDATE, no DELETE, ever --
-- there is no draft/mutable phase for a decision the way there is for a
-- draft scenario_versions row, so, unlike scenario_versions, no DELETE
-- grant is ever justified here. No TRUNCATE, REFERENCES, or TRIGGER either,
-- for the same reasoning as scenario_attempts above.
-- ---------------------------------------------------------------------------

REVOKE ALL ON TABLE public.scenario_attempts FROM PUBLIC;
REVOKE ALL ON TABLE public.scenario_attempts FROM anon;
REVOKE ALL ON TABLE public.scenario_attempts FROM authenticated;
REVOKE ALL ON TABLE public.scenario_attempts FROM service_role;

REVOKE ALL ON TABLE public.scenario_decisions FROM PUBLIC;
REVOKE ALL ON TABLE public.scenario_decisions FROM anon;
REVOKE ALL ON TABLE public.scenario_decisions FROM authenticated;
REVOKE ALL ON TABLE public.scenario_decisions FROM service_role;

GRANT SELECT, INSERT, UPDATE ON TABLE public.scenario_attempts TO service_role;
GRANT SELECT, INSERT ON TABLE public.scenario_decisions TO service_role;


-- ---------------------------------------------------------------------------
-- 8. RPC 1 — start_or_resume_scenario_attempt_v1
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.start_or_resume_scenario_attempt_v1(
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
AS $$
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
$$;

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

REVOKE ALL ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.start_or_resume_scenario_attempt_v1(text, uuid, text, jsonb, text, text) TO service_role;


-- ---------------------------------------------------------------------------
-- 9. RPC 2 — get_scenario_attempt_v1 (read-only)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.get_scenario_attempt_v1(
    p_user_email  text,
    p_attempt_id  uuid
)
RETURNS TABLE (
    attempt_id                uuid,
    scenario_id                uuid,
    scenario_version_id        uuid,
    status                     text,
    current_scene_id           text,
    next_sequence_number       integer,
    serialized_engine_state    jsonb,
    engine_version              text,
    scenario_content_sha256    text,
    started_at                  timestamptz,
    updated_at                  timestamptz,
    completed_at                timestamptz,
    abandoned_at                timestamptz,
    terminal_ending_id          text,
    terminal_result_snapshot    jsonb,
    decisions                   jsonb
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_user_email text;
    v_attempt    record;
BEGIN
    v_user_email := NULLIF(BTRIM(LOWER(p_user_email)), '');
    IF v_user_email IS NULL OR v_user_email !~ '@' THEN
        RAISE EXCEPTION 'invalid_user_email: p_user_email must be a non-empty, non-whitespace email address'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_attempt_id IS NULL THEN
        RAISE EXCEPTION 'invalid_attempt_id: p_attempt_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- A single combined (id, owner) lookup is deliberate: it produces the
    -- IDENTICAL attempt_not_found outcome whether p_attempt_id does not
    -- exist at all, or exists but is owned by a different learner. No
    -- second, existence-only query is ever run that would let a caller
    -- distinguish "no such attempt" from "not yours" by probing IDs.
    --
    -- SIM-PERSIST-04F Correction 1: FOR SHARE locks this row for the
    -- remainder of THIS transaction, closing a READ COMMITTED read-skew
    -- window that otherwise existed between this SELECT and the
    -- scenario_decisions SELECT below -- without it, a concurrent
    -- submit_scenario_decision_v1/abandon_scenario_attempt_v1 call (both of
    -- which take FOR UPDATE on this same row) could commit in between the
    -- two SELECTs, so this RPC would return the attempt's PRE-commit
    -- current_scene_id/serialized_engine_state/status alongside the
    -- POST-commit decision history -- an internally inconsistent snapshot.
    -- FOR SHARE (not FOR KEY SHARE) is required: FOR KEY SHARE only
    -- conflicts with a concurrent DELETE or a key-column UPDATE, NOT with an
    -- ordinary non-key UPDATE, which is exactly what
    -- submit_scenario_decision_v1/abandon_scenario_attempt_v1 perform. FOR
    -- SHARE correctly blocks a concurrent FOR UPDATE locker (and vice
    -- versa) for the rest of this transaction, and this RPC never writes to
    -- scenario_attempts or scenario_decisions itself -- it remains
    -- read-only with respect to stored data. A real two-session
    -- concurrency exercise (one session holding this FOR SHARE lock while
    -- another attempts a concurrent FOR UPDATE mutation) cannot be
    -- exercised inside this script's own single-session BEGIN/ROLLBACK
    -- transaction; that belongs in the project's upcoming throwaway-database
    -- two-session concurrency test gate. This function's own verification
    -- script instead proves the locking CONTRACT is actually installed, by
    -- inspecting this function's source text for the FOR SHARE clause
    -- against its exact, to_regprocedure-resolved OID.
    SELECT sa.id, sa.scenario_id, sa.scenario_version_id, sa.status, sa.current_scene_id,
           sa.next_sequence_number, sa.serialized_engine_state, sa.engine_version,
           sa.scenario_content_sha256, sa.started_at, sa.updated_at, sa.completed_at,
           sa.abandoned_at, sa.terminal_ending_id, sa.terminal_result_snapshot
    INTO   v_attempt
    FROM   public.scenario_attempts AS sa
    WHERE  sa.id = p_attempt_id
    AND    sa.user_email = v_user_email
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'attempt_not_found: no scenario_attempts row % is owned by the requesting learner', p_attempt_id
            USING ERRCODE = 'no_data_found';
    END IF;

    RETURN QUERY
    SELECT
        v_attempt.id, v_attempt.scenario_id, v_attempt.scenario_version_id, v_attempt.status,
        v_attempt.current_scene_id, v_attempt.next_sequence_number, v_attempt.serialized_engine_state,
        v_attempt.engine_version, v_attempt.scenario_content_sha256, v_attempt.started_at,
        v_attempt.updated_at, v_attempt.completed_at, v_attempt.abandoned_at,
        v_attempt.terminal_ending_id, v_attempt.terminal_result_snapshot,
        COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'sequenceNumber', sd.sequence_number,
                        'expectedSceneId', sd.expected_scene_id,
                        'selectedOptionId', sd.selected_option_id,
                        'stateBefore', sd.state_before,
                        'stateAfter', sd.state_after,
                        'resultingSceneId', sd.resulting_scene_id,
                        'isTerminal', sd.is_terminal,
                        'terminalEndingId', sd.terminal_ending_id,
                        'createdAt', sd.created_at
                    )
                    ORDER BY sd.sequence_number
                )
                FROM public.scenario_decisions AS sd
                WHERE sd.attempt_id = v_attempt.id
            ),
            '[]'::jsonb
        );
END;
$$;

COMMENT ON FUNCTION public.get_scenario_attempt_v1(text, uuid) IS
'Read-only. Returns exactly one attempt owned by the normalized caller email,
plus its full ordered decision history (idempotency_key/request_fingerprint
deliberately excluded from the returned decisions -- they are internal
integrity metadata, not replay/audit display data) as a compact jsonb array.
Never mutates state. A single combined (id, owner) lookup means an unknown
id and an id owned by a different learner both raise the identical
attempt_not_found exception. SIM-PERSIST-04F: that same combined lookup now
takes FOR SHARE (never FOR KEY SHARE, which would not conflict with an
ordinary non-key UPDATE), held for the rest of this transaction, so a
concurrent submit_scenario_decision_v1/abandon_scenario_attempt_v1 call
(both FOR UPDATE on this same row) cannot commit a decision in between this
function''s attempt SELECT and its decisions SELECT -- the attempt fields and
the decision history returned together are always mutually consistent as of
one instant. This RPC still writes nothing itself. Execute permission:
service_role only.';

REVOKE ALL ON FUNCTION public.get_scenario_attempt_v1(text, uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_scenario_attempt_v1(text, uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.get_scenario_attempt_v1(text, uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_scenario_attempt_v1(text, uuid) TO service_role;


-- ---------------------------------------------------------------------------
-- 10. RPC 3 — submit_scenario_decision_v1
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.submit_scenario_decision_v1(
    p_user_email                text,
    p_attempt_id                uuid,
    p_idempotency_key           uuid,
    p_expected_sequence_number  integer,
    p_expected_scene_id         text,
    p_selected_option_id        text,
    p_request_fingerprint       text,
    p_state_before              jsonb,
    p_state_after               jsonb,
    p_is_terminal               boolean,
    p_resulting_scene_id        text DEFAULT NULL,
    p_terminal_ending_id        text DEFAULT NULL,
    p_terminal_result_snapshot  jsonb DEFAULT NULL
)
RETURNS TABLE (
    decision_id                uuid,
    attempt_id                  uuid,
    sequence_number              integer,
    idempotent_replay            boolean,
    attempt_status                text,
    current_scene_id              text,
    next_sequence_number          integer,
    serialized_engine_state        jsonb,
    completed_at                    timestamptz,
    terminal_ending_id                text,
    terminal_result_snapshot           jsonb
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_user_email    text;
    v_attempt_owner text;
    v_status        text;
    v_scene         text;
    v_seq           integer;
    v_state         jsonb;
    v_existing_id                  uuid;
    v_existing_seq                 integer;
    v_existing_fp                  text;
    v_existing_expected_scene_id   text;
    v_existing_selected_option_id  text;
    v_existing_state_before        jsonb;
    v_existing_state_after         jsonb;
    v_existing_resulting_scene     text;
    v_existing_is_terminal         boolean;
    v_existing_terminal_ending_id  text;
    v_decision_id       uuid;
    v_out_completed_at            timestamptz;
    v_out_terminal_ending_id      text;
    v_out_terminal_result         jsonb;
BEGIN
    -- 1. Validate scalar formats and required JSON object types up front,
    -- before touching any row.
    v_user_email := NULLIF(BTRIM(LOWER(p_user_email)), '');
    IF v_user_email IS NULL OR v_user_email !~ '@' THEN
        RAISE EXCEPTION 'invalid_user_email: p_user_email must be a non-empty, non-whitespace email address'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_attempt_id IS NULL THEN
        RAISE EXCEPTION 'invalid_attempt_id: p_attempt_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_idempotency_key IS NULL THEN
        RAISE EXCEPTION 'invalid_idempotency_key: p_idempotency_key must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_expected_sequence_number IS NULL OR p_expected_sequence_number < 1 THEN
        RAISE EXCEPTION 'invalid_sequence_number: p_expected_sequence_number must be an integer >= 1'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF NULLIF(BTRIM(p_expected_scene_id), '') IS NULL THEN
        RAISE EXCEPTION 'invalid_expected_scene_id: p_expected_scene_id must not be null or empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    p_expected_scene_id := BTRIM(p_expected_scene_id);

    IF NULLIF(BTRIM(p_selected_option_id), '') IS NULL THEN
        RAISE EXCEPTION 'invalid_selected_option_id: p_selected_option_id must not be null or empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    p_selected_option_id := BTRIM(p_selected_option_id);

    IF p_request_fingerprint IS NULL OR p_request_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid_request_fingerprint: p_request_fingerprint must be exactly 64 lowercase hexadecimal characters'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_state_before IS NULL OR jsonb_typeof(p_state_before) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid_state_before: p_state_before must be a JSON object'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_state_after IS NULL OR jsonb_typeof(p_state_after) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid_state_after: p_state_after must be a JSON object'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_is_terminal IS NULL THEN
        RAISE EXCEPTION 'invalid_is_terminal: p_is_terminal must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_is_terminal THEN
        IF p_resulting_scene_id IS NOT NULL THEN
            RAISE EXCEPTION 'invalid_resulting_scene_id: p_resulting_scene_id must be null for a terminal decision'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF NULLIF(BTRIM(p_terminal_ending_id), '') IS NULL THEN
            RAISE EXCEPTION 'invalid_terminal_ending_id: p_terminal_ending_id must not be null or empty for a terminal decision'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        p_terminal_ending_id := BTRIM(p_terminal_ending_id);
        IF p_terminal_result_snapshot IS NULL OR jsonb_typeof(p_terminal_result_snapshot) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'invalid_terminal_result_snapshot: p_terminal_result_snapshot must be a JSON object for a terminal decision'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- SIM-PERSIST-04E terminal-ending-identity consistency: the engine
        -- snapshot and the attempt currently duplicate the terminal
        -- ending's identity across THREE places -- p_terminal_ending_id,
        -- p_terminal_result_snapshot.endingId, and (checked separately,
        -- below in step 2B) p_state_after.terminalResult.endingId (via the
        -- full-object equality already required there). This block only
        -- requires the two duplicated, directly caller-supplied identities
        -- to agree with each other -- it never calculates or judges which
        -- ending is correct.
        IF jsonb_typeof(p_terminal_result_snapshot->'endingId') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'terminal_ending_mismatch: p_terminal_result_snapshot.endingId must be a JSON string for attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF NULLIF(BTRIM(p_terminal_result_snapshot->>'endingId'), '') IS NULL THEN
            RAISE EXCEPTION 'terminal_ending_mismatch: p_terminal_result_snapshot.endingId must be a normalized, non-empty string for attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF (p_terminal_result_snapshot->>'endingId') IS DISTINCT FROM p_terminal_ending_id THEN
            RAISE EXCEPTION 'terminal_ending_mismatch: p_terminal_result_snapshot.endingId does not equal p_terminal_ending_id for attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    ELSE
        IF NULLIF(BTRIM(p_resulting_scene_id), '') IS NULL THEN
            RAISE EXCEPTION 'invalid_resulting_scene_id: p_resulting_scene_id must not be null or empty for a non-terminal decision'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        p_resulting_scene_id := BTRIM(p_resulting_scene_id);
        IF p_terminal_ending_id IS NOT NULL OR p_terminal_result_snapshot IS NOT NULL THEN
            RAISE EXCEPTION 'invalid_terminal_fields: p_terminal_ending_id and p_terminal_result_snapshot must be null for a non-terminal decision'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END IF;

    -- 2B. SIM-PERSIST-04C/04E snapshot IDENTITY/LIFECYCLE integrity
    -- boundary. Pure equality/shape checks between values the caller
    -- already supplied (state_before, state_after, and the scalar
    -- parameters above) -- never a computation of which scene, score, or
    -- ending is correct. state_before's own internal consistency against
    -- the attempt's actually-persisted serialized_engine_state is still
    -- checked separately below (step 9, state_before_mismatch); this step
    -- only checks state_before/state_after against EACH OTHER and against
    -- the scalar parameters already validated above.
    --
    -- SIM-PERSIST-04E: every field's JSON TYPE is checked BEFORE any ->>
    -- textual comparison is trusted -- ->> silently coerces a JSON number
    -- or boolean to its text representation, so relying on ->> alone could
    -- let e.g. a JSON number 5 masquerade as the string "5". Type-checking
    -- first, on BOTH state_before and state_after independently, closes
    -- that gap.
    IF jsonb_typeof(p_state_before->'simulationId') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_state_after->'simulationId')  IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_state_before->'version')              IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_state_after->'version')               IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_state_before->'canonicalContentSha256') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_state_after->'canonicalContentSha256')  IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_state_before->'engineVersion')          IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_state_after->'engineVersion')           IS DISTINCT FROM 'string'
    THEN
        RAISE EXCEPTION 'state_identity_mismatch: state_before/state_after simulationId, version, canonicalContentSha256, and engineVersion must all be JSON strings for attempt %', p_attempt_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF NULLIF(BTRIM(p_state_before->>'simulationId'), '') IS NULL
       OR (p_state_before->>'simulationId') IS DISTINCT FROM BTRIM(p_state_before->>'simulationId')
       OR NULLIF(BTRIM(p_state_before->>'version'), '') IS NULL
       OR (p_state_before->>'version') IS DISTINCT FROM BTRIM(p_state_before->>'version')
       OR NULLIF(BTRIM(p_state_before->>'engineVersion'), '') IS NULL
       OR (p_state_before->>'engineVersion') IS DISTINCT FROM BTRIM(p_state_before->>'engineVersion')
    THEN
        RAISE EXCEPTION 'state_identity_mismatch: state_before.simulationId, version, and engineVersion must already be normalized (trimmed), non-empty strings for attempt %', p_attempt_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF (p_state_before->>'canonicalContentSha256') !~ '^[0-9a-f]{64}$'
       OR (p_state_after->>'canonicalContentSha256') !~ '^[0-9a-f]{64}$'
    THEN
        RAISE EXCEPTION 'state_identity_mismatch: state_before/state_after canonicalContentSha256 must already be exactly 64 lowercase hexadecimal characters for attempt %', p_attempt_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF (p_state_before->>'simulationId')  IS DISTINCT FROM (p_state_after->>'simulationId')
       OR (p_state_before->>'version')              IS DISTINCT FROM (p_state_after->>'version')
       OR (p_state_before->>'canonicalContentSha256') IS DISTINCT FROM (p_state_after->>'canonicalContentSha256')
       OR (p_state_before->>'engineVersion')          IS DISTINCT FROM (p_state_after->>'engineVersion')
    THEN
        RAISE EXCEPTION 'state_identity_mismatch: state_before and state_after immutable identity fields (simulationId, version, canonicalContentSha256, engineVersion) do not match for attempt %', p_attempt_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_state_before->'currentSceneId') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'state_lifecycle_mismatch: state_before.currentSceneId must be a JSON string for attempt %', p_attempt_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_state_before->>'currentSceneId') IS DISTINCT FROM p_expected_scene_id THEN
        RAISE EXCEPTION 'state_lifecycle_mismatch: state_before.currentSceneId does not match p_expected_scene_id for attempt %', p_attempt_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_state_before->'isComplete') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION 'state_lifecycle_mismatch: state_before.isComplete must be a JSON boolean for attempt %', p_attempt_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_state_before->'isComplete') IS DISTINCT FROM 'false'::jsonb THEN
        RAISE EXCEPTION 'state_lifecycle_mismatch: state_before.isComplete must be false for attempt %', p_attempt_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_is_terminal THEN
        -- SIM-PERSIST-04F Correction 3: require currentSceneId to be
        -- EXPLICITLY PRESENT as a JSON null, not merely absent. `->` on a
        -- missing key also evaluates to a SQL NULL, so the previous
        -- "IS NULL OR jsonb_typeof(...) = 'null'" check silently accepted a
        -- state_after object that omitted the key entirely -- indistinguishable
        -- from an intentional `"currentSceneId": null`. jsonb_typeof(...) of a
        -- SQL NULL (missing key) is itself SQL NULL, which is IS DISTINCT FROM
        -- the text 'null', so requiring exact equality now rejects a missing
        -- key with the same state_lifecycle_mismatch exception a wrong-typed
        -- value already gets.
        IF jsonb_typeof(p_state_after -> 'currentSceneId') IS DISTINCT FROM 'null' THEN
            RAISE EXCEPTION 'state_lifecycle_mismatch: state_after.currentSceneId must be explicitly present as a JSON null for a terminal decision on attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF jsonb_typeof(p_state_after->'isComplete') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'state_lifecycle_mismatch: state_after.isComplete must be a JSON boolean for a terminal decision on attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF (p_state_after->'isComplete') IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION 'state_lifecycle_mismatch: state_after.isComplete must be true for a terminal decision on attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF jsonb_typeof(p_state_after->'terminalResult') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'state_lifecycle_mismatch: state_after.terminalResult must be a JSON object for a terminal decision on attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF (p_state_after->'terminalResult') IS DISTINCT FROM p_terminal_result_snapshot THEN
            RAISE EXCEPTION 'terminal_result_mismatch: state_after.terminalResult does not equal p_terminal_result_snapshot for attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    ELSE
        IF jsonb_typeof(p_state_after->'currentSceneId') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'state_lifecycle_mismatch: state_after.currentSceneId must be a JSON string for a non-terminal decision on attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF (p_state_after->>'currentSceneId') IS DISTINCT FROM p_resulting_scene_id THEN
            RAISE EXCEPTION 'state_lifecycle_mismatch: state_after.currentSceneId does not match p_resulting_scene_id for attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF jsonb_typeof(p_state_after->'isComplete') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'state_lifecycle_mismatch: state_after.isComplete must be a JSON boolean for a non-terminal decision on attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF (p_state_after->'isComplete') IS DISTINCT FROM 'false'::jsonb THEN
            RAISE EXCEPTION 'state_lifecycle_mismatch: state_after.isComplete must be false for a non-terminal decision on attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF jsonb_typeof(p_state_after->'terminalResult') IS DISTINCT FROM 'null' THEN
            RAISE EXCEPTION 'state_lifecycle_mismatch: state_after.terminalResult must be null for a non-terminal decision on attempt %', p_attempt_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END IF;

    -- 3. Lock the attempt row FOR UPDATE.
    SELECT sa.user_email, sa.status, sa.current_scene_id, sa.next_sequence_number, sa.serialized_engine_state
    INTO   v_attempt_owner, v_status, v_scene, v_seq, v_state
    FROM   public.scenario_attempts AS sa
    WHERE  sa.id = p_attempt_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'attempt_not_found: scenario_attempts % does not exist', p_attempt_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- 4. Confirm ownership. Reuses the identical attempt_not_found label as
    -- the missing-row case above so a caller cannot distinguish "no such
    -- attempt" from "not yours" by probing IDs.
    IF v_attempt_owner IS DISTINCT FROM v_user_email THEN
        RAISE EXCEPTION 'attempt_not_found: scenario_attempts % does not exist', p_attempt_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- 5. Idempotency check BEFORE ordinary sequence/scene/state rejection --
    -- a safe retry must succeed even if, by the time it arrives, the
    -- attempt has already moved past (or completed beyond) the sequence
    -- number this call would otherwise expect.
    --
    -- SIM-PERSIST-04F Correction 2: a matching request_fingerprint ALONE is
    -- no longer sufficient to treat this as a safe retry -- that previously
    -- permitted the same (attempt_id, idempotency_key, request_fingerprint)
    -- triple to be replayed with a DIFFERENT selected_option_id/state/
    -- sequence/scene/terminal-ending, silently returning the ORIGINAL
    -- decision's result for a request that was not actually identical. Every
    -- stored request field below is now also compared, with IS NOT DISTINCT
    -- FROM (NULL-safe, matching every other equality check in this
    -- function), against the corresponding CURRENT parameter -- terminal_
    -- result_snapshot needs no separate column of its own: for a terminal
    -- decision, p_state_after.terminalResult is already required (by the
    -- IDENTITY/LIFECYCLE checks in step 2B above) to equal
    -- p_terminal_result_snapshot exactly, and state_after itself is already
    -- compared below, so an inconsistent terminal_result_snapshot cannot
    -- hide behind an unchanged state_after.
    SELECT sd.id, sd.sequence_number, sd.request_fingerprint, sd.expected_scene_id,
           sd.selected_option_id, sd.state_before, sd.state_after,
           sd.resulting_scene_id, sd.is_terminal, sd.terminal_ending_id
    INTO   v_existing_id, v_existing_seq, v_existing_fp, v_existing_expected_scene_id,
           v_existing_selected_option_id, v_existing_state_before, v_existing_state_after,
           v_existing_resulting_scene, v_existing_is_terminal, v_existing_terminal_ending_id
    FROM   public.scenario_decisions AS sd
    WHERE  sd.attempt_id = p_attempt_id
    AND    sd.idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_existing_fp IS DISTINCT FROM p_request_fingerprint
           OR v_existing_seq IS DISTINCT FROM p_expected_sequence_number
           OR v_existing_expected_scene_id IS DISTINCT FROM p_expected_scene_id
           OR v_existing_selected_option_id IS DISTINCT FROM p_selected_option_id
           OR v_existing_state_before IS DISTINCT FROM p_state_before
           OR v_existing_state_after IS DISTINCT FROM p_state_after
           OR v_existing_resulting_scene IS DISTINCT FROM p_resulting_scene_id
           OR v_existing_is_terminal IS DISTINCT FROM p_is_terminal
           OR v_existing_terminal_ending_id IS DISTINCT FROM p_terminal_ending_id
        THEN
            RAISE EXCEPTION 'idempotency_key_conflict: idempotency_key % was already used on attempt % with a different request fingerprint', p_idempotency_key, p_attempt_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        -- SIM-PERSIST-04C stable idempotent replay: derive the result
        -- entirely from THIS decision's own immutable scenario_decisions
        -- row (v_existing_*), NEVER from the attempt's current state --
        -- the attempt may have advanced past (or completed beyond) this
        -- decision by the time the retry arrives, and an older retry must
        -- keep returning its OWN original post-decision result forever.
        IF v_existing_is_terminal THEN
            -- terminal_ending_id/terminal_result_snapshot/completed_at are
            -- read from scenario_attempts here specifically because a
            -- completed attempt is permanently immutable (trg_guard_
            -- scenario_attempt_mutation rejects any further UPDATE
            -- unconditionally) -- so reading them from there, rather than
            -- duplicating them, is exactly as stable as reading from the
            -- decision row itself.
            SELECT sa.completed_at, sa.terminal_ending_id, sa.terminal_result_snapshot
            INTO   v_out_completed_at, v_out_terminal_ending_id, v_out_terminal_result
            FROM   public.scenario_attempts AS sa
            WHERE  sa.id = p_attempt_id;

            v_status := 'completed';
            v_scene  := NULL;
        ELSE
            v_status              := 'in_progress';
            v_scene                := v_existing_resulting_scene;
            v_out_completed_at     := NULL;
            v_out_terminal_ending_id := NULL;
            v_out_terminal_result    := NULL;
        END IF;

        v_seq   := v_existing_seq + 1;
        v_state := v_existing_state_after;

        RETURN QUERY SELECT
            v_existing_id, p_attempt_id, v_existing_seq, true,
            v_status, v_scene, v_seq, v_state, v_out_completed_at, v_out_terminal_ending_id, v_out_terminal_result;
        RETURN;
    END IF;

    -- 6. Confirm attempt is in_progress.
    IF v_status IS DISTINCT FROM 'in_progress' THEN
        RAISE EXCEPTION 'attempt_not_in_progress: scenario_attempts % has status % and cannot accept a new decision', p_attempt_id, v_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 7. Confirm expected sequence equals next_sequence_number.
    IF p_expected_sequence_number IS DISTINCT FROM v_seq THEN
        RAISE EXCEPTION 'sequence_mismatch: expected sequence % but attempt % is at sequence %', p_expected_sequence_number, p_attempt_id, v_seq
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 8. Confirm expected current scene matches persisted current_scene_id.
    IF p_expected_scene_id IS DISTINCT FROM v_scene THEN
        RAISE EXCEPTION 'scene_mismatch: expected current scene % but attempt % is at scene %', p_expected_scene_id, p_attempt_id, v_scene
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 9. Confirm state-before exactly matches persisted
    -- serialized_engine_state. jsonb equality in PostgreSQL is already
    -- whitespace- and key-order-insensitive at every nesting level, so a
    -- plain IS DISTINCT FROM comparison is deterministic and appropriate --
    -- no extra canonicalization is performed or required here.
    IF p_state_before IS DISTINCT FROM v_state THEN
        RAISE EXCEPTION 'state_before_mismatch: supplied state_before does not match attempt %''s persisted serialized_engine_state', p_attempt_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 10. Insert one append-only decision. SIM-PERSIST-04C RPC-only INSERT
    -- guard: generate the new decision's id here, name it in the
    -- transaction-local guard, and insert it explicitly -- see the
    -- migration header ("Transaction-local INSERT guards") for the exact,
    -- honest scope of this protection.
    v_decision_id := gen_random_uuid();
    PERFORM set_config('certbound.scenario_decision_insert_guard', v_decision_id::text, true);

    INSERT INTO public.scenario_decisions (
        id, attempt_id, sequence_number, idempotency_key, request_fingerprint,
        expected_scene_id, selected_option_id, state_before, state_after,
        resulting_scene_id, is_terminal, terminal_ending_id
    )
    VALUES (
        v_decision_id, p_attempt_id, p_expected_sequence_number, p_idempotency_key, p_request_fingerprint,
        p_expected_scene_id, p_selected_option_id, p_state_before, p_state_after,
        p_resulting_scene_id, p_is_terminal, p_terminal_ending_id
    );

    -- 11-13. Update attempt state atomically, guarded by the transaction-
    -- local mutation guard. Increments next_sequence_number exactly once,
    -- and -- when terminal -- transitions status to completed and records
    -- the terminal snapshot in the SAME transaction as the decision insert
    -- above, so no window exists where the terminal decision is recorded
    -- but the attempt is not yet completed.
    PERFORM set_config('certbound.scenario_attempt_mutation_guard', p_attempt_id::text, true);

    IF p_is_terminal THEN
        UPDATE public.scenario_attempts AS sa
        SET    status                    = 'completed',
               current_scene_id          = NULL,
               next_sequence_number      = sa.next_sequence_number + 1,
               serialized_engine_state   = p_state_after,
               completed_at              = clock_timestamp(),
               terminal_ending_id        = p_terminal_ending_id,
               terminal_result_snapshot  = p_terminal_result_snapshot,
               updated_at                = now()
        WHERE  sa.id = p_attempt_id;
    ELSE
        UPDATE public.scenario_attempts AS sa
        SET    current_scene_id         = p_resulting_scene_id,
               next_sequence_number     = sa.next_sequence_number + 1,
               serialized_engine_state  = p_state_after,
               updated_at               = now()
        WHERE  sa.id = p_attempt_id;
    END IF;

    -- 14. Return the committed decision plus the updated attempt state.
    SELECT sa.status, sa.current_scene_id, sa.next_sequence_number, sa.serialized_engine_state,
           sa.completed_at, sa.terminal_ending_id, sa.terminal_result_snapshot
    INTO   v_status, v_scene, v_seq, v_state, v_out_completed_at, v_out_terminal_ending_id, v_out_terminal_result
    FROM   public.scenario_attempts AS sa
    WHERE  sa.id = p_attempt_id;

    RETURN QUERY SELECT
        v_decision_id, p_attempt_id, p_expected_sequence_number, false,
        v_status, v_scene, v_seq, v_state, v_out_completed_at, v_out_terminal_ending_id, v_out_terminal_result;
END;
$$;

COMMENT ON FUNCTION public.submit_scenario_decision_v1(text, uuid, uuid, integer, text, text, text, jsonb, jsonb, boolean, text, text, jsonb) IS
'Records exactly one append-only scenario_decisions row and atomically
advances (or, when p_is_terminal, completes) the parent scenario_attempts
row in the same transaction. Validation order: scalar/JSON-shape validation
(including, for a terminal decision, SIM-PERSIST-04E terminal_ending_mismatch
-- p_terminal_result_snapshot.endingId must be a normalized, non-empty JSON
string exactly equal to p_terminal_ending_id), then SIM-PERSIST-04C/04E/04F
snapshot IDENTITY/LIFECYCLE checks (every identity/lifecycle field''s JSON
type is checked before any ->> textual comparison is trusted; state_before vs
state_after immutable identity fields; state_before/state_after
currentSceneId/isComplete/terminalResult consistency against the scalar
parameters -- state_identity_mismatch / state_lifecycle_mismatch /
terminal_result_mismatch; SIM-PERSIST-04F: a terminal decision''s
state_after.currentSceneId must be EXPLICITLY present as a JSON null, a
missing key is rejected identically to a wrong-typed value), then
lock-and-own the attempt, then idempotency-key lookup (a safe retry now
requires BOTH a matching request_fingerprint AND every stored request field
-- sequence_number, expected_scene_id, selected_option_id, state_before,
state_after, resulting_scene_id, is_terminal, terminal_ending_id -- to be
IS NOT DISTINCT FROM the current call''s parameters, SIM-PERSIST-04F,
closing the gap where a matching fingerprint alone could previously mask a
changed request; a genuine match returns that SAME decision''s own original
committed post-decision result, derived from the immutable
scenario_decisions row itself -- never the attempt''s possibly-since-advanced
current state; any disagreement raises idempotency_key_conflict), then
in_progress/sequence/scene/state-before validation, then the insert and
update. Generates the new decision''s id itself and sets the
transaction-local certbound.scenario_decision_insert_guard immediately
before its own INSERT, so a direct service_role INSERT bypassing this RPC is
rejected by trg_guard_scenario_decision_immutability. Never calculates
scores, resulting scenes, or endings -- every derived value is supplied by
the caller (utils/scenario_persistence.py, backed by
utils/scenario_engine.py) as already-validated engine output. There is no
separate completion RPC: a terminal decision completes the attempt in this
same call. Execute permission: service_role only.';

REVOKE ALL ON FUNCTION public.submit_scenario_decision_v1(text, uuid, uuid, integer, text, text, text, jsonb, jsonb, boolean, text, text, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.submit_scenario_decision_v1(text, uuid, uuid, integer, text, text, text, jsonb, jsonb, boolean, text, text, jsonb) FROM anon;
REVOKE EXECUTE ON FUNCTION public.submit_scenario_decision_v1(text, uuid, uuid, integer, text, text, text, jsonb, jsonb, boolean, text, text, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.submit_scenario_decision_v1(text, uuid, uuid, integer, text, text, text, jsonb, jsonb, boolean, text, text, jsonb) TO service_role;


-- ---------------------------------------------------------------------------
-- 11. RPC 4 — abandon_scenario_attempt_v1
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.abandon_scenario_attempt_v1(
    p_user_email  text,
    p_attempt_id  uuid
)
RETURNS TABLE (
    attempt_id    uuid,
    status         text,
    abandoned_at    timestamptz
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_user_email text;
    v_id         uuid;
    v_owner      text;
    v_status     text;
    v_abandoned  timestamptz;
BEGIN
    v_user_email := NULLIF(BTRIM(LOWER(p_user_email)), '');
    IF v_user_email IS NULL OR v_user_email !~ '@' THEN
        RAISE EXCEPTION 'invalid_user_email: p_user_email must be a non-empty, non-whitespace email address'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_attempt_id IS NULL THEN
        RAISE EXCEPTION 'invalid_attempt_id: p_attempt_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT sa.id, sa.user_email, sa.status, sa.abandoned_at
    INTO   v_id, v_owner, v_status, v_abandoned
    FROM   public.scenario_attempts AS sa
    WHERE  sa.id = p_attempt_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'attempt_not_found: scenario_attempts % does not exist', p_attempt_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_owner IS DISTINCT FROM v_user_email THEN
        RAISE EXCEPTION 'attempt_not_found: scenario_attempts % does not exist', p_attempt_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_status = 'abandoned' THEN
        -- Idempotent no-op: re-calling abandon on an already-abandoned
        -- attempt returns its existing final state instead of erroring.
        RETURN QUERY SELECT v_id, v_status, v_abandoned;
        RETURN;
    END IF;

    IF v_status IS DISTINCT FROM 'in_progress' THEN
        RAISE EXCEPTION 'attempt_not_in_progress: scenario_attempts % has status % and can never be abandoned', p_attempt_id, v_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM set_config('certbound.scenario_attempt_mutation_guard', p_attempt_id::text, true);

    UPDATE public.scenario_attempts AS sa
    SET    status        = 'abandoned',
           abandoned_at  = clock_timestamp(),
           updated_at    = now()
    WHERE  sa.id = p_attempt_id
    RETURNING sa.status, sa.abandoned_at INTO v_status, v_abandoned;

    RETURN QUERY SELECT p_attempt_id, v_status, v_abandoned;
END;
$$;

COMMENT ON FUNCTION public.abandon_scenario_attempt_v1(text, uuid) IS
'Transitions exactly one owned, in_progress attempt to abandoned. Idempotent:
re-calling on an already-abandoned attempt returns its existing final state
rather than erroring. Rejects a completed attempt (attempt_not_in_progress)
-- a completed attempt can never be abandoned after the fact. Never deletes
scenario_decisions rows. Execute permission: service_role only.';

REVOKE ALL ON FUNCTION public.abandon_scenario_attempt_v1(text, uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.abandon_scenario_attempt_v1(text, uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.abandon_scenario_attempt_v1(text, uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.abandon_scenario_attempt_v1(text, uuid) TO service_role;

COMMIT;
