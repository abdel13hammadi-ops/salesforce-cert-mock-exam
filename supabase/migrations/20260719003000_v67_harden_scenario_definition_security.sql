-- =============================================================================
-- SIM-PERSIST-03: V67 — Production Scenario Security Hotfix
-- Created : 2026-07-19 00:30:00 UTC
--
-- WHY THIS MIGRATION EXISTS
-- --------------------------
-- The V66 Scenario Simulator definition foundation
-- (supabase/migrations/20260718170000_v66_scenario_definition_persistence_
-- foundation.sql) was manually applied directly in the production Supabase
-- SQL Editor rather than through the normal migration pipeline. That manual
-- application installed the intended tables, functions, and triggers
-- correctly, but production's actual grant state diverged from what V66
-- specifies -- a production inventory found:
--   * anon and authenticated reporting SELECT, INSERT, UPDATE, and DELETE on
--     both public.scenarios and public.scenario_versions (V66 intends ZERO
--     direct privileges for anon/authenticated).
--   * service_role holding DELETE on both tables (V66 intended service_role
--     to have no DELETE on public.scenarios, and DELETE only on
--     public.scenario_versions for draft cleanup -- this hotfix removes
--     DELETE from BOTH tables entirely, going further than V66, per the
--     SIM-PERSIST-03 production grant spec below).
--   * the pointer-guard trigger permitting current_published_version_id to
--     be cleared to NULL with no publication-guard check at all.
-- Both tables currently contain zero rows, so this hotfix is purely
-- structural (grants + one trigger function) and touches no data.
--
-- This migration is a NARROW, PRODUCTION-SAFE CORRECTIVE HOTFIX. It does
-- NOT recreate, drop, or alter any table; it does not touch
-- public.guard_scenario_version_immutability_v1() or the publication RPC's
-- body; it does not insert, publish, or delete any row.
--
-- MIGRATION-HISTORY WARNING -- READ BEFORE APPLYING
-- ----------------------------------------------------
-- V66 was manually applied in the production SQL Editor, not through
-- `supabase db push` or any tracked migration runner. The production
-- project currently has NO supabase_migrations.schema_migrations table at
-- all (CLI migration-history tracking was never initialized there). This
-- V67 migration deliberately does NOT create, backfill, or repair that
-- schema/table, and does not attempt to reconcile migration history for V66
-- or any other migration. A separate, repository-wide migration-history
-- onboarding task is required after this security hotfix is applied, to
-- decide how (or whether) to retroactively initialize
-- supabase_migrations.schema_migrations for this production project without
-- causing the CLI to attempt to re-run already-applied migrations.
--
-- PRECONDITIONS
-- ---------------
-- This migration fails explicitly and atomically (see "Atomicity" below) if
-- any expected V66 object is missing. It never silently tolerates a missing
-- object and never recreates a table.
--
-- ATOMICITY
-- ----------
-- The entire corrective migration is wrapped in an explicit BEGIN; ... 
-- COMMIT; transaction block (this repository's established convention for
-- an atomic multi-statement migration -- see
-- supabase/migrations/20260623182200_v44_backfill_question_versions.sql).
-- Because the precondition checks below run as the FIRST statements inside
-- that transaction, a precondition failure raises an exception and aborts
-- the entire transaction before any grant or function change is applied --
-- grants can never end up corrected while the trigger function remains
-- uncorrected, or vice versa. Every DDL statement here is ordinary
-- transactional DDL (no CREATE INDEX CONCURRENTLY or other
-- non-transactional operation is used), so the whole file either applies
-- completely or has no effect at all.
--
-- FINAL PRODUCTION GRANT STATE (this migration's target end-state)
-- --------------------------------------------------------------------
-- public.scenarios          : service_role has SELECT, INSERT, UPDATE only.
-- public.scenario_versions  : service_role has SELECT, INSERT, UPDATE only.
-- Neither table grants DELETE, TRUNCATE, REFERENCES, or TRIGGER to
-- service_role (this is a further reduction from V66, which had granted
-- DELETE on scenario_versions for draft cleanup -- that DELETE grant is
-- deliberately removed here as part of this hotfix's minimization).
-- PUBLIC, anon, and authenticated retain ZERO privileges on either table.
-- public.publish_scenario_version_v1(uuid,jsonb,text) remains EXECUTE-only
-- for service_role; PUBLIC, anon, and authenticated retain zero EXECUTE.
-- The object owner and the postgres role are never targeted by any REVOKE
-- in this migration, so ownership privileges are unaffected.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Preconditions -- fail explicitly and atomically if any expected V66
--    object is missing. Never recreates a table, never silently tolerates
--    a missing object.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('public.scenarios') IS NULL THEN
        RAISE EXCEPTION 'V67 PRECONDITION FAILED: public.scenarios does not exist. This corrective migration requires the V66 foundation to already be installed; it will not create tables.';
    END IF;

    IF to_regclass('public.scenario_versions') IS NULL THEN
        RAISE EXCEPTION 'V67 PRECONDITION FAILED: public.scenario_versions does not exist. This corrective migration requires the V66 foundation to already be installed; it will not create tables.';
    END IF;

    IF to_regprocedure('public.publish_scenario_version_v1(uuid,jsonb,text)') IS NULL THEN
        RAISE EXCEPTION 'V67 PRECONDITION FAILED: public.publish_scenario_version_v1(uuid,jsonb,text) does not exist.';
    END IF;

    IF to_regprocedure('public.guard_scenario_current_published_version_v1()') IS NULL THEN
        RAISE EXCEPTION 'V67 PRECONDITION FAILED: public.guard_scenario_current_published_version_v1() does not exist.';
    END IF;

    IF to_regprocedure('public.guard_scenario_version_immutability_v1()') IS NULL THEN
        RAISE EXCEPTION 'V67 PRECONDITION FAILED: public.guard_scenario_version_immutability_v1() does not exist.';
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
        RAISE EXCEPTION 'V67 PRECONDITION FAILED: trg_guard_scenario_current_published_version does not exist on public.scenarios.';
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
        RAISE EXCEPTION 'V67 PRECONDITION FAILED: trg_guard_scenario_version_immutability does not exist on public.scenario_versions.';
    END IF;

    RAISE NOTICE 'V67 PRECONDITIONS PASSED: all expected V66 objects exist.';
END;
$$;


-- ---------------------------------------------------------------------------
-- 2. Correct table grants.
--
-- REVOKE ALL from every role that currently has (or might have via a
-- default/inherited privilege) any privilege on either table, INCLUDING
-- service_role, before re-granting the exact intended minimal set. This
-- guarantees the final state regardless of what undocumented privileges
-- (DELETE, TRUNCATE, REFERENCES, TRIGGER, or anything else) production
-- accumulated from the manual SQL Editor application. This never touches
-- the table owner's implicit privileges -- REVOKE cannot remove an owner's
-- inherent rights, and the owner/postgres role is never named below.
-- ---------------------------------------------------------------------------

REVOKE ALL ON TABLE public.scenarios FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON TABLE public.scenario_versions FROM PUBLIC, anon, authenticated, service_role;

GRANT SELECT, INSERT, UPDATE ON TABLE public.scenarios TO service_role;
GRANT SELECT, INSERT, UPDATE ON TABLE public.scenario_versions TO service_role;


-- ---------------------------------------------------------------------------
-- 3. Correct publication-RPC grants.
--
-- Re-asserts the intended grant state for public.publish_scenario_version_v1
-- exactly as specified. The function body, SECURITY INVOKER declaration,
-- and SET search_path = public, pg_catalog are all unchanged -- only grants
-- are reasserted here, and no correction to the function itself is
-- necessary for this security task.
-- ---------------------------------------------------------------------------

REVOKE ALL ON FUNCTION public.publish_scenario_version_v1(uuid, jsonb, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.publish_scenario_version_v1(uuid, jsonb, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.publish_scenario_version_v1(uuid, jsonb, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.publish_scenario_version_v1(uuid, jsonb, text) TO service_role;


-- ---------------------------------------------------------------------------
-- 4. Harden pointer clearing.
--
-- Replaces ONLY public.guard_scenario_current_published_version_v1(). Its
-- signature, trigger attachment (trg_guard_scenario_current_published_version,
-- BEFORE INSERT OR UPDATE OF current_published_version_id ON public.scenarios),
-- LANGUAGE plpgsql, implicit SECURITY INVOKER (no SECURITY DEFINER clause),
-- and SET search_path = public, pg_catalog are all unchanged.
-- CREATE OR REPLACE FUNCTION preserves the function's OID, so the existing
-- trigger's binding to this function requires no change at all.
--
-- Behavioral change from V66: previously, setting the pointer to NULL was
-- treated identically to any other "new value", which meant clearing an
-- established current-version pointer required no publication guard at
-- all -- an unguarded raw UPDATE could always clear it. This corrected
-- version distinguishes:
--   * INSERT with a NULL pointer (a scenario that has never published
--     anything yet)              -> still allowed, unconditionally.
--   * UPDATE where the pointer is unchanged (including NULL -> NULL)
--                                  -> still allowed as a no-op (this check
--                                     runs first and short-circuits).
--   * UPDATE where a genuinely NON-null pointer is being changed to NULL
--                                  -> now REJECTED with the focused
--                                     exception current_published_version_
--                                     clear_not_allowed. No RPC in this V1
--                                     surface clears an established
--                                     pointer; deactivation/rollback of a
--                                     published scenario requires a
--                                     separately reviewed RPC or schema
--                                     change, not a raw UPDATE to NULL.
--   * UPDATE/INSERT setting or changing to a non-null value
--                                  -> unchanged from V66: must reference an
--                                     existing, same-scenario, published
--                                     scenario_versions row, and the change
--                                     must be authorized by the same
--                                     transaction-local publication guard
--                                     used by guard_scenario_version_
--                                     immutability_v1.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.guard_scenario_current_published_version_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_guard  text;
    v_target record;
BEGIN
    -- No-op: the pointer column is present in the UPDATE's SET list but its
    -- value is unchanged (including NULL -> NULL). Always allowed.
    IF TG_OP = 'UPDATE' AND NEW.current_published_version_id IS NOT DISTINCT FROM OLD.current_published_version_id THEN
        RETURN NEW;
    END IF;

    IF NEW.current_published_version_id IS NULL THEN
        IF TG_OP = 'INSERT' THEN
            -- A brand-new scenario that has never published a version yet.
            RETURN NEW;
        END IF;

        -- TG_OP = 'UPDATE' and the pointer is genuinely changing to NULL
        -- (the NULL -> NULL no-op case was already handled above, so
        -- reaching here means OLD.current_published_version_id was
        -- non-null). Clearing an established current-version pointer is
        -- not authorized in V1.
        RAISE EXCEPTION 'current_published_version_clear_not_allowed: scenarios.current_published_version_id cannot be cleared to NULL once a version has been published for scenario % (previous value %); deactivation or rollback requires a separately reviewed RPC or schema change, not a direct UPDATE',
            OLD.id, OLD.current_published_version_id
            USING ERRCODE = 'feature_not_supported';
    END IF;

    -- From here, NEW.current_published_version_id is a genuinely new,
    -- non-null value (either the scenario's first-ever publication, or a
    -- change from one published version to another).
    SELECT sv.id, sv.scenario_id, sv.lifecycle_status
    INTO   v_target
    FROM   public.scenario_versions AS sv
    WHERE  sv.id = NEW.current_published_version_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'scenarios.current_published_version_id % does not reference an existing scenario_versions row', NEW.current_published_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_target.scenario_id IS DISTINCT FROM NEW.id THEN
        RAISE EXCEPTION 'scenarios.current_published_version_id % belongs to scenario %, not scenario %',
            NEW.current_published_version_id, v_target.scenario_id, NEW.id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_target.lifecycle_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'scenarios.current_published_version_id % is not published (status=%); only a published version may become current',
            NEW.current_published_version_id, v_target.lifecycle_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- The pointer change itself must be guarded. See the comment on this
    -- function, and the V66 migration header, for the exact, honest scope
    -- of this protection: it is an application/RPC mutation-integrity
    -- safeguard for normal service_role API usage, not a defense against
    -- arbitrary trusted SQL in the same transaction.
    v_guard := current_setting('certbound.publish_scenario_version_guard', true);
    IF v_guard IS NULL OR v_guard <> NEW.current_published_version_id::text THEN
        RAISE EXCEPTION 'scenarios.current_published_version_id may only be changed by public.publish_scenario_version_v1 (publication guard not set for %)',
            NEW.current_published_version_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION public.guard_scenario_current_published_version_v1() IS
'BEFORE INSERT OR UPDATE OF current_published_version_id trigger on
public.scenarios. NULL is allowed for a scenario that has never published a
version (initial INSERT, or a no-op UPDATE that leaves an already-NULL
pointer NULL). Once a current published version exists, direct clearing of
the pointer back to NULL is prohibited
(current_published_version_clear_not_allowed) -- deactivation or rollback of
a published scenario requires a separately reviewed RPC or schema change, not
a raw UPDATE. A non-null pointer must reference an existing scenario_versions
row that belongs to this exact scenario AND has lifecycle_status = published,
and setting or changing it to a non-null value requires the same
transaction-local publication guard used by
guard_scenario_version_immutability_v1. That custom transaction guard is an
application/RPC mutation-integrity safeguard for normal service_role API
usage -- it is explicitly NOT a defense against a database administrator,
superuser, or any other actor able to execute arbitrary trusted SQL inside
the same transaction as a legitimate publish call. See the V66 migration
header (supabase/migrations/20260718170000_v66_scenario_definition_
persistence_foundation.sql) for the precise scope of this guarantee.';

COMMIT;
