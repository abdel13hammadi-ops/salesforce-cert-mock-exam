-- =============================================================================
-- SIM-PERSIST-02 / SIM-PERSIST-02A: Scenario definition persistence foundation
-- Created : 2026-07-18 17:00:00 UTC
--
-- Artifact identity: V66. This artifact was originally authored under a V64
-- filename and was renamed to V66 (SIM-PERSIST-02A) before ever being applied
-- anywhere, because V64 already belongs to an unrelated, already-applied
-- migration (see supabase/migrations/20260714110000_v64_add_sales_cloud_
-- consultant_certification_catalog.sql) and V65 is also already in use (see
-- supabase/migrations/20260714120000_v65_add_service_cloud_consultant_
-- certification_catalog.sql). No SQL object created by this migration was
-- ever renamed -- only the three artifact filenames and their internal
-- textual labels changed from V64 to V66. public.scenarios,
-- public.scenario_versions, and public.publish_scenario_version_v1 keep the
-- exact names approved in SIM-PERSIST-02.
--
-- Purpose
-- -------
-- Versioned, immutable Scenario Simulator DEFINITIONS only:
--   scenarios          — one row per simulation_id, with a pointer to the
--                         version currently offered to new learners
--   scenario_versions  — immutable, publishable content snapshots
--
-- This migration intentionally does NOT create scenario_attempts or
-- scenario_decisions (learner runtime persistence is a separate,
-- later migration). No Python code, worker code, or Streamlit code is
-- touched. No scoring, state-machine, or ending-evaluation logic is
-- implemented in SQL -- utils/scenario_engine.py remains the only
-- implementation of scenario transitions, scoring, domain performance,
-- state changes, and ending evaluation.
--
-- Content-authority model
-- ------------------------
-- Repository scenario JSON files remain the authoring/review/source-control
-- source of truth. At publication, the *exact* validated document is copied
-- into scenario_versions.content_snapshot as an immutable jsonb runtime
-- snapshot, alongside canonical_content_sha256 identifying that exact
-- content. Once published, a scenario_versions row is a permanent, immutable
-- historical release: it is never edited, retired, or deleted. Publishing a
-- newer version only moves the scenarios.current_published_version_id
-- pointer -- older published versions remain published and immutable, so
-- in-progress/completed learner attempts (added in a later migration) can
-- stay pinned to whatever version they actually started on.
--
-- Certification identity
-- -----------------------
-- scenarios.certification_exam_name is a plain, unconstrained text field.
-- This repository's public.certifications table predates its migration
-- history (see supabase/migrations/20260713224500_v61_add_platform_app_
-- builder_certification_catalog.sql, lines 7-8) and certifications.id is not
-- used as a foreign key anywhere in this codebase -- certifications.exam_name
-- is the established natural key. No foreign key to public.certifications is
-- added here; see supabase/tests/v66_scenario_definition_schema_preflight.sql
-- for a live read-only report of that table's actual shape.
--
-- Immutability model (published scenario_versions rows)
-- --------------------------------------------------------
-- scenario_versions.lifecycle_status is 'draft' or 'published' (no
-- 'retired' status -- published rows are immutable forever, not superseded
-- in place). A BEFORE UPDATE OR DELETE trigger enforces:
--   * any UPDATE or DELETE of a published row is rejected unconditionally.
--   * the one-time draft -> published transition is rejected UNLESS the
--     transaction-local publication guard (see below) names the exact row
--     being transitioned.
--
-- Current-published-version pointer integrity (scenarios.current_published_version_id)
-- -----------------------------------------------------------------------------------
-- The composite foreign key added in section 3 only proves the pointer
-- belongs to the same scenario -- it does NOT prove the referenced version is
-- actually published. A second BEFORE INSERT OR UPDATE OF
-- current_published_version_id trigger on scenarios closes that gap:
--   * NULL is always allowed.
--   * A non-null pointer must reference a scenario_versions row that (a)
--     exists, (b) belongs to this exact scenario, and (c) has
--     lifecycle_status = 'published'.
--   * A pointer *change* (not merely present-and-unchanged) is additionally
--     rejected unless the transaction-local publication guard names the
--     exact target version id.
-- This migration deliberately does NOT add a rollback-to-an-older-version or
-- current-version-selection RPC -- selecting an older already-published
-- version as current again is out of scope here and will require a
-- separate, deliberately controlled RPC in a later migration (it is not
-- simply "point back at an old id" because it has attempt-compatibility and
-- audit implications that belong in their own reviewed change).
--
-- Transaction-local publication guard -- what it is and is NOT
-- -----------------------------------------------------------------
-- public.publish_scenario_version_v1 calls
-- set_config('certbound.publish_scenario_version_guard', <version id>,
-- is_local => true) once, before it performs either of its two writes (the
-- scenario_versions draft -> published UPDATE and the scenarios pointer
-- UPDATE). Both guard-checking triggers below read that same setting.
-- is_local = true means Postgres clears it automatically at the end of the
-- current transaction (commit OR rollback), so it can never leak into a
-- different transaction or session.
--
-- This is an APPLICATION/RPC MUTATION-BOUNDARY SAFEGUARD for normal
-- service_role API usage (i.e. the Python backend calling
-- publish_scenario_version_v1 through the ordinary Supabase client, the same
-- way every other RPC in this repository is called) -- it makes an
-- accidental or buggy *raw* UPDATE/DELETE issued over that same service_role
-- connection fail instead of silently corrupting a published row or the
-- current-version pointer. It is explicitly NOT a defense against a database
-- administrator, a superuser, or any other actor with the ability to execute
-- arbitrary trusted SQL inside the *same* transaction as a legitimate publish
-- call -- such an actor could, for example, issue their own
-- set_config(..., true) call, or (within that one transaction, after a
-- legitimate publish of version X) re-issue a raw pointer UPDATE back to
-- version X itself before the transaction ends, since the guard remains set
-- for X until that transaction concludes. Real protection against that class
-- of actor is out of scope for a single migration and depends on
-- infrastructure-level controls (who holds service_role credentials, network
-- boundaries, connection auditing), not a custom GUC.
--
-- Security model
-- ---------------
-- RLS is enabled on both tables with NO anon/authenticated policies.
-- PUBLIC, anon, and authenticated have all privileges revoked. Only
-- service_role has table/function privileges, minimized per SIM-PERSIST-02A
-- (see section 7 for the exact grant set and its justification). This
-- repository's Python backend always connects with the service_role key (see
-- utils/access_control.py); service_role BYPASSES RLS entirely, so the real
-- security boundary here is "service_role credentials are server-only and
-- never reach a browser" plus "no anon/authenticated grants exist to reach
-- through even if RLS were somehow bypassed another way" -- NOT a
-- browser-facing auth.uid() policy. No auth.uid()-based policy is added in
-- this migration.
--
-- Publication RPC
-- ----------------
-- public.publish_scenario_version_v1(p_scenario_version_id, p_content_snapshot,
-- p_canonical_content_sha256) atomically: validates inputs (including a
-- strict, focused-exception JSON identity check -- see section 8), validates
-- the hash format (64 lowercase hex characters -- it does NOT recompute or
-- verify the hash was actually derived from the JSON; that is the Python
-- publication workflow's responsibility), locks and validates the draft
-- row and its parent scenario, verifies simulationId/version/schemaVersion
-- identity against the immutable columns already on those rows, publishes
-- the draft, and repoints scenarios.current_published_version_id.
--
-- This migration is purely additive. No existing table is modified other
-- than by this migration's own two new tables and the foreign key between
-- them.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. scenarios
--
-- current_published_version_id has no foreign key yet -- scenario_versions
-- does not exist until section 2. The FK (and the composite FK that pins it
-- to the *same* scenario) is added in section 3, after scenario_versions
-- exists.
-- ---------------------------------------------------------------------------

CREATE TABLE public.scenarios (
    id                            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    simulation_id                 text         NOT NULL,
    certification_exam_name       text         NOT NULL,
    title                         text         NOT NULL,
    description                   text,
    is_active                     boolean      NOT NULL DEFAULT true,
    current_published_version_id  uuid,
    created_at                    timestamptz  NOT NULL DEFAULT now(),
    updated_at                    timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT scenarios_simulation_id_unique
        UNIQUE (simulation_id),

    CONSTRAINT scenarios_simulation_id_normalized
        CHECK (simulation_id = BTRIM(simulation_id) AND simulation_id <> ''),

    CONSTRAINT scenarios_certification_exam_name_normalized
        CHECK (certification_exam_name = BTRIM(certification_exam_name) AND certification_exam_name <> ''),

    CONSTRAINT scenarios_title_normalized
        CHECK (title = BTRIM(title) AND title <> '')
);

COMMENT ON TABLE public.scenarios IS
'One row per Scenario Simulator simulation_id. current_published_version_id
points to the immutable scenario_versions row currently offered to NEW
learners; publishing a newer version only moves this pointer -- it never
edits or retires older published versions. No Creative Studio (company,
character, dialogue, image) foreign keys exist on this table.';


-- ---------------------------------------------------------------------------
-- 2. scenario_versions
-- ---------------------------------------------------------------------------

CREATE TABLE public.scenario_versions (
    id                       uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    scenario_id              uuid         NOT NULL REFERENCES public.scenarios (id) ON DELETE RESTRICT,
    version                  text         NOT NULL,
    lifecycle_status         text         NOT NULL DEFAULT 'draft',
    schema_version           text         NOT NULL,
    engine_version           text         NOT NULL,
    source_repository_path   text         NOT NULL,
    canonical_content_sha256 text,
    content_snapshot         jsonb,
    created_at               timestamptz  NOT NULL DEFAULT now(),
    created_by               text,
    published_at             timestamptz,

    -- Required to support the composite foreign key added in section 3
    -- (scenarios.current_published_version_id must reference a
    -- scenario_versions row that belongs to that exact scenario).
    CONSTRAINT scenario_versions_scenario_id_id_unique
        UNIQUE (scenario_id, id),

    CONSTRAINT scenario_versions_unique_version
        UNIQUE (scenario_id, version),

    CONSTRAINT scenario_versions_version_normalized
        CHECK (version = BTRIM(version) AND version <> ''),

    CONSTRAINT scenario_versions_schema_version_normalized
        CHECK (schema_version = BTRIM(schema_version) AND schema_version <> ''),

    CONSTRAINT scenario_versions_engine_version_normalized
        CHECK (engine_version = BTRIM(engine_version) AND engine_version <> ''),

    CONSTRAINT scenario_versions_source_repository_path_normalized
        CHECK (source_repository_path = BTRIM(source_repository_path) AND source_repository_path <> ''),

    CONSTRAINT scenario_versions_lifecycle_status_valid
        CHECK (lifecycle_status IN ('draft', 'published')),

    CONSTRAINT scenario_versions_draft_has_no_published_at
        CHECK (lifecycle_status <> 'draft' OR published_at IS NULL),

    CONSTRAINT scenario_versions_published_requires_snapshot
        CHECK (
            lifecycle_status <> 'published'
            OR (
                content_snapshot IS NOT NULL
                AND canonical_content_sha256 IS NOT NULL
                AND canonical_content_sha256 ~ '^[0-9a-f]{64}$'
                AND published_at IS NOT NULL
            )
        )
);

COMMENT ON TABLE public.scenario_versions IS
'Immutable, versioned Scenario Simulator content snapshots. lifecycle_status
is draft or published only -- there is no retired status; a published row is
a permanent historical release enforced immutable by
trg_guard_scenario_version_immutability. content_snapshot is the exact
validated JSON document copied from the repository at publication time;
canonical_content_sha256 identifies it. Multiple published versions of the
same scenario_id may coexist; scenarios.current_published_version_id (not
this table) determines which one is offered to new learners, and is itself
protected by trg_guard_scenario_current_published_version.';


-- ---------------------------------------------------------------------------
-- 3. Circular foreign key: scenarios.current_published_version_id
--
-- Added via ALTER TABLE now that scenario_versions exists. This is a
-- composite foreign key against scenario_versions (scenario_id, id) rather
-- than a plain FK against scenario_versions (id) alone, so Postgres itself
-- enforces "the referenced current version must belong to the same
-- scenario" as a schema constraint, not merely as an RPC-side check. With
-- default MATCH SIMPLE semantics, the constraint is trivially satisfied
-- whenever current_published_version_id IS NULL (no version published yet).
--
-- This FK does NOT prove the referenced version is published -- that
-- additional guarantee is enforced separately in section 5B by
-- trg_guard_scenario_current_published_version. Both are retained together
-- as defense in depth: this FK is the schema-level "same scenario" guarantee
-- that cannot be bypassed even if the trigger were ever dropped or disabled,
-- while the trigger adds the "must be published" and "guarded" checks the FK
-- structurally cannot express.
--
-- ON DELETE RESTRICT (never CASCADE) -- a published version can never be
-- deleted anyway per the immutability trigger, but this also blocks
-- deleting a still-draft version while it is the current pointer target
-- (which should not happen, since only published versions are ever pointed
-- to, but the constraint is kept for defense-in-depth).
-- ---------------------------------------------------------------------------

ALTER TABLE public.scenarios
    ADD CONSTRAINT scenarios_current_published_version_fk
    FOREIGN KEY (id, current_published_version_id)
    REFERENCES public.scenario_versions (scenario_id, id)
    ON DELETE RESTRICT;


-- ---------------------------------------------------------------------------
-- 4. Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_scenarios_certification_exam_name
    ON public.scenarios (certification_exam_name);

CREATE INDEX IF NOT EXISTS idx_scenarios_active
    ON public.scenarios (certification_exam_name)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_scenarios_current_published_version_id
    ON public.scenarios (current_published_version_id)
    WHERE current_published_version_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_scenario_versions_scenario_id
    ON public.scenario_versions (scenario_id);

CREATE INDEX IF NOT EXISTS idx_scenario_versions_lifecycle_status
    ON public.scenario_versions (lifecycle_status);

CREATE INDEX IF NOT EXISTS idx_scenario_versions_canonical_content_sha256
    ON public.scenario_versions (canonical_content_sha256)
    WHERE canonical_content_sha256 IS NOT NULL;

-- scenario_id + version lookups are already covered by the
-- scenario_versions_unique_version UNIQUE (scenario_id, version) constraint's
-- implicit index.


-- ---------------------------------------------------------------------------
-- 5A. Immutability trigger — published scenario_versions rows are permanent
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.guard_scenario_version_immutability_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_guard text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.lifecycle_status = 'published' THEN
            RAISE EXCEPTION 'published scenario_versions row % is immutable and cannot be deleted', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- TG_OP = 'UPDATE' from here on.

    IF OLD.lifecycle_status = 'published' THEN
        RAISE EXCEPTION 'published scenario_versions row % is immutable and cannot be updated', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- OLD.lifecycle_status = 'draft'. A draft may be freely edited, EXCEPT
    -- the one-time transition into 'published', which is only permitted
    -- through publish_scenario_version_v1's transaction-local guard (see the
    -- migration header for exactly what this guard does and does not
    -- protect against).
    IF NEW.lifecycle_status = 'published' THEN
        v_guard := current_setting('certbound.publish_scenario_version_guard', true);
        IF v_guard IS NULL OR v_guard <> OLD.id::text THEN
            RAISE EXCEPTION 'draft -> published transition for scenario_versions % is only permitted through public.publish_scenario_version_v1', OLD.id
                USING ERRCODE = 'insufficient_privilege';
        END IF;

        IF NEW.scenario_id IS DISTINCT FROM OLD.scenario_id
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
           OR NEW.engine_version IS DISTINCT FROM OLD.engine_version
           OR NEW.source_repository_path IS DISTINCT FROM OLD.source_repository_path
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.created_by IS DISTINCT FROM OLD.created_by
        THEN
            RAISE EXCEPTION 'publication of scenario_versions % must not alter identity or authoring fields', OLD.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        RETURN NEW;
    END IF;

    IF NEW.lifecycle_status <> 'draft' THEN
        RAISE EXCEPTION 'scenario_versions % lifecycle_status must remain draft until publication', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION public.guard_scenario_version_immutability_v1() IS
'BEFORE UPDATE OR DELETE trigger on public.scenario_versions. Published rows
are permanently immutable (no UPDATE, no DELETE, ever). The one-time draft ->
published transition is only permitted when the transaction-local guard
(certbound.publish_scenario_version_guard, set only inside
publish_scenario_version_v1) names the exact row id. This is an
application/RPC mutation-boundary safeguard for normal service_role API
usage, not a defense against arbitrary trusted SQL in the same transaction --
see the migration header for the precise scope of this guarantee.';

DROP TRIGGER IF EXISTS trg_guard_scenario_version_immutability ON public.scenario_versions;
CREATE TRIGGER trg_guard_scenario_version_immutability
    BEFORE UPDATE OR DELETE ON public.scenario_versions
    FOR EACH ROW EXECUTE FUNCTION public.guard_scenario_version_immutability_v1();


-- ---------------------------------------------------------------------------
-- 5B. Current-published-version pointer trigger — scenarios.
--     current_published_version_id may only point at a published version of
--     THIS scenario, and may only be changed by publish_scenario_version_v1.
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
    -- Ordinary updates that do not touch current_published_version_id never
    -- reach this function at all (see the "UPDATE OF current_published_version_id"
    -- trigger clause below); this additional check only guards against a
    -- statement that names the column in its SET list without actually
    -- changing its value (e.g. SET current_published_version_id = current_published_version_id, title = ...).
    IF TG_OP = 'UPDATE' AND NEW.current_published_version_id IS NOT DISTINCT FROM OLD.current_published_version_id THEN
        RETURN NEW;
    END IF;

    -- Requirement 1: NULL remains allowed unconditionally (covers the
    -- initial INSERT of a brand-new scenario with no published version yet,
    -- and covers explicitly clearing the pointer).
    IF NEW.current_published_version_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT sv.id, sv.scenario_id, sv.lifecycle_status
    INTO   v_target
    FROM   public.scenario_versions AS sv
    WHERE  sv.id = NEW.current_published_version_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'scenarios.current_published_version_id % does not reference an existing scenario_versions row', NEW.current_published_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_target.scenario_id IS DISTINCT FROM NEW.id THEN
        RAISE EXCEPTION 'scenarios.current_published_version_id % belongs to scenario % , not scenario %',
            NEW.current_published_version_id, v_target.scenario_id, NEW.id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_target.lifecycle_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'scenarios.current_published_version_id % is not published (status=%); only a published version may become current',
            NEW.current_published_version_id, v_target.lifecycle_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Requirement 3: the pointer change itself must be guarded. See the
    -- migration header ("Transaction-local publication guard -- what it is
    -- and is NOT") for the exact, honest scope of this protection.
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
public.scenarios. NULL is always allowed. A non-null pointer must reference
an existing scenario_versions row that belongs to this exact scenario AND has
lifecycle_status = published, and the change must be authorized by the same
transaction-local publication guard used by
guard_scenario_version_immutability_v1. Does not implement, and this
migration does not add, any rollback-to-an-older-version or
current-version-selection RPC -- selecting a different already-published
version as current requires a separate, deliberately controlled RPC added in
a later migration.';

DROP TRIGGER IF EXISTS trg_guard_scenario_current_published_version ON public.scenarios;
CREATE TRIGGER trg_guard_scenario_current_published_version
    BEFORE INSERT OR UPDATE OF current_published_version_id ON public.scenarios
    FOR EACH ROW EXECUTE FUNCTION public.guard_scenario_current_published_version_v1();


-- ---------------------------------------------------------------------------
-- 6. Row Level Security — enabled, no anon/authenticated policies.
--
-- service_role BYPASSES RLS entirely (this is standard Postgres/Supabase
-- behavior, not a gap in this migration). The real security boundary is:
--   * service_role credentials are server-only (see utils/access_control.py)
--     and must NEVER be exposed to a browser.
--   * anon/authenticated have zero table or function grants below, so even
--     a client holding only the anon/authenticated key cannot reach these
--     tables or the publication RPC at all, RLS aside.
-- No auth.uid()-based policy is defined in this migration.
-- ---------------------------------------------------------------------------

ALTER TABLE public.scenarios         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scenario_versions ENABLE ROW LEVEL SECURITY;


-- ---------------------------------------------------------------------------
-- 7. Grants — service_role only, minimized per SIM-PERSIST-02A.
--
-- scenarios: SELECT, INSERT, UPDATE. No DELETE -- nothing in this migration
-- or the approved V1 workflow deletes a scenario row, so DELETE is withheld
-- until a specific, justified operation needs it.
--
-- scenario_versions: SELECT, INSERT, UPDATE, DELETE. DELETE is deliberately
-- retained (unlike scenarios) because the approved authoring workflow needs
-- to be able to discard an erroneous or abandoned DRAFT version before it is
-- ever published -- published rows can never actually be deleted regardless
-- of this grant, because trg_guard_scenario_version_immutability
-- unconditionally rejects DELETE once lifecycle_status = 'published'. This
-- grant therefore only ever has effect on draft rows.
-- ---------------------------------------------------------------------------

REVOKE ALL ON TABLE public.scenarios FROM PUBLIC;
REVOKE ALL ON TABLE public.scenario_versions FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON TABLE public.scenarios TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.scenario_versions TO service_role;


-- ---------------------------------------------------------------------------
-- 8. Publication RPC
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.publish_scenario_version_v1(
    p_scenario_version_id      uuid,
    p_content_snapshot         jsonb,
    p_canonical_content_sha256 text
)
RETURNS TABLE (
    scenario_id               uuid,
    scenario_version_id       uuid,
    simulation_id             text,
    version                   text,
    canonical_content_sha256  text,
    lifecycle_status          text,
    published_at              timestamptz,
    became_current            boolean
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_version                  record;
    v_scenario                 record;
    v_hash                     text;
    v_snapshot_simulation_id   text;
    v_snapshot_version         text;
    v_snapshot_schema_version  text;
    v_previous_current_id      uuid;
    v_became_current           boolean;
    v_published_at             timestamptz;
BEGIN
    -- 1. Require non-null inputs.
    IF p_scenario_version_id IS NULL THEN
        RAISE EXCEPTION 'p_scenario_version_id must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF p_canonical_content_sha256 IS NULL THEN
        RAISE EXCEPTION 'p_canonical_content_sha256 must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    v_hash := NULLIF(BTRIM(p_canonical_content_sha256), '');
    IF v_hash IS NULL THEN
        RAISE EXCEPTION 'p_canonical_content_sha256 must not be null or empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 2. Validate the SHA-256 format only. This RPC never recomputes the
    --    hash from p_content_snapshot and never claims the supplied hash
    --    was actually derived from the supplied JSON -- that verification
    --    is the Python publication workflow's responsibility (schema
    --    validation, canonicalization, and hashing all happen there before
    --    this RPC is ever called).
    IF v_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'p_canonical_content_sha256 must be exactly 64 lowercase hexadecimal characters'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 3a. p_content_snapshot must be present and a JSON object.
    --     "snapshot_not_object" is the focused, distinguishable label for
    --     both failure modes (null input and non-object input).
    IF p_content_snapshot IS NULL THEN
        RAISE EXCEPTION 'snapshot_not_object: p_content_snapshot must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF jsonb_typeof(p_content_snapshot) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'snapshot_not_object: p_content_snapshot must be a JSON object'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 3b. All three identity fields are mandatory: present, a JSON string,
    --     and non-empty/non-whitespace once extracted. The adopted Scenario
    --     Simulator schema always includes schemaVersion, so it is required
    --     here too -- never treated as optional.
    IF NOT (p_content_snapshot ? 'simulationId')
       OR jsonb_typeof(p_content_snapshot -> 'simulationId') IS DISTINCT FROM 'string'
    THEN
        RAISE EXCEPTION 'missing_or_invalid_simulation_id: content_snapshot.simulationId must be present and a JSON string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_snapshot_simulation_id := NULLIF(BTRIM(p_content_snapshot ->> 'simulationId'), '');
    IF v_snapshot_simulation_id IS NULL THEN
        RAISE EXCEPTION 'missing_or_invalid_simulation_id: content_snapshot.simulationId must not be empty or whitespace-only'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF NOT (p_content_snapshot ? 'version')
       OR jsonb_typeof(p_content_snapshot -> 'version') IS DISTINCT FROM 'string'
    THEN
        RAISE EXCEPTION 'missing_or_invalid_version: content_snapshot.version must be present and a JSON string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_snapshot_version := NULLIF(BTRIM(p_content_snapshot ->> 'version'), '');
    IF v_snapshot_version IS NULL THEN
        RAISE EXCEPTION 'missing_or_invalid_version: content_snapshot.version must not be empty or whitespace-only'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF NOT (p_content_snapshot ? 'schemaVersion')
       OR jsonb_typeof(p_content_snapshot -> 'schemaVersion') IS DISTINCT FROM 'string'
    THEN
        RAISE EXCEPTION 'missing_or_invalid_schema_version: content_snapshot.schemaVersion must be present and a JSON string'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    v_snapshot_schema_version := NULLIF(BTRIM(p_content_snapshot ->> 'schemaVersion'), '');
    IF v_snapshot_schema_version IS NULL THEN
        RAISE EXCEPTION 'missing_or_invalid_schema_version: content_snapshot.schemaVersion must not be empty or whitespace-only'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 4. Lock the target draft row.
    SELECT sv.id, sv.scenario_id, sv.version, sv.lifecycle_status,
           sv.schema_version, sv.engine_version
    INTO   v_version
    FROM   public.scenario_versions AS sv
    WHERE  sv.id = p_scenario_version_id
    FOR UPDATE;

    -- 5. Reject an unknown version.
    IF NOT FOUND THEN
        RAISE EXCEPTION 'scenario_versions not found: %', p_scenario_version_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- 6. Reject a version that is not draft.
    IF v_version.lifecycle_status <> 'draft' THEN
        RAISE EXCEPTION 'scenario_versions % is not draft (status=%); only a draft version can be published',
            p_scenario_version_id, v_version.lifecycle_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 7. Lock the parent scenarios row.
    SELECT s.id, s.simulation_id, s.current_published_version_id
    INTO   v_scenario
    FROM   public.scenarios AS s
    WHERE  s.id = v_version.scenario_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'parent scenarios row not found for scenario_versions %: scenario_id=%',
            p_scenario_version_id, v_version.scenario_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- 8. Verify identity VALUES using IS DISTINCT FROM (never a plain <>,
    --    which would behave incorrectly if either side were ever NULL).
    --    Only the identity fields the adopted JSON schema defines are
    --    checked here -- scoring, graph shape, and every other content
    --    detail are the Python schema/engine layer's responsibility (see
    --    utils/scenario_schema.py), never re-derived in SQL.
    IF v_snapshot_simulation_id IS DISTINCT FROM v_scenario.simulation_id THEN
        RAISE EXCEPTION 'simulation_id_mismatch: content_snapshot simulationId (%) does not match scenarios.simulation_id (%) for scenario %',
            v_snapshot_simulation_id, v_scenario.simulation_id, v_scenario.id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_snapshot_version IS DISTINCT FROM v_version.version THEN
        RAISE EXCEPTION 'version_mismatch: content_snapshot version (%) does not match scenario_versions.version (%) for scenario_versions %',
            v_snapshot_version, v_version.version, p_scenario_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_snapshot_schema_version IS DISTINCT FROM v_version.schema_version THEN
        RAISE EXCEPTION 'schema_version_mismatch: content_snapshot schemaVersion (%) does not match scenario_versions.schema_version (%) for scenario_versions %',
            v_snapshot_schema_version, v_version.schema_version, p_scenario_version_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- 9. Set the transaction-local publication guard BEFORE either write.
    --    Both guard-checking triggers (guard_scenario_version_immutability_v1
    --    on scenario_versions and guard_scenario_current_published_version_v1
    --    on scenarios) read this same setting. See the migration header for
    --    exactly what this guard does and does not protect against.
    PERFORM set_config(
        'certbound.publish_scenario_version_guard',
        p_scenario_version_id::text,
        true
    );

    v_previous_current_id := v_scenario.current_published_version_id;

    -- 10. Atomically publish the draft row.
    UPDATE public.scenario_versions AS sv
    SET    content_snapshot         = p_content_snapshot,
           canonical_content_sha256 = v_hash,
           lifecycle_status         = 'published',
           published_at             = clock_timestamp()
    WHERE  sv.id = p_scenario_version_id
    RETURNING sv.published_at INTO v_published_at;

    -- 11 & 12. Repoint scenarios.current_published_version_id and touch
    --          updated_at. This repository has no generic updated_at
    --          trigger convention (see
    --          supabase/migrations/20260629120000_v46_free_mock_curation_
    --          foundation.sql's publish_free_mock_draft_v1, which sets
    --          updated_at = now() explicitly inside the RPC) -- the same
    --          RPC-managed pattern is followed here.
    UPDATE public.scenarios AS s
    SET    current_published_version_id = p_scenario_version_id,
           updated_at                   = now()
    WHERE  s.id = v_scenario.id;

    v_became_current := v_previous_current_id IS DISTINCT FROM p_scenario_version_id;

    -- 13. Return a compact result.
    RETURN QUERY
    SELECT
        v_scenario.id,
        p_scenario_version_id,
        v_scenario.simulation_id,
        v_version.version,
        v_hash,
        'published'::text,
        v_published_at,
        v_became_current;
END;
$$;

COMMENT ON FUNCTION public.publish_scenario_version_v1(uuid, jsonb, text) IS
'Publishes a draft scenario_versions row: validates inputs, hash format, and
strict JSON-snapshot identity (simulationId/version/schemaVersion, all
mandatory, all compared with IS DISTINCT FROM, focused exception labels
snapshot_not_object / missing_or_invalid_simulation_id / simulation_id_mismatch
/ missing_or_invalid_version / version_mismatch / missing_or_invalid_schema_version
/ schema_version_mismatch), locks and validates the draft and its parent
scenario, publishes the draft, and repoints
scenarios.current_published_version_id (itself further guarded by
guard_scenario_current_published_version_v1). Never recomputes or
independently verifies that the supplied hash matches the supplied JSON --
that is the Python publication workflow''s responsibility. Never implements
scoring or graph validation. Execute permission: service_role only.';

REVOKE ALL ON FUNCTION public.publish_scenario_version_v1(uuid, jsonb, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.publish_scenario_version_v1(uuid, jsonb, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.publish_scenario_version_v1(uuid, jsonb, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.publish_scenario_version_v1(uuid, jsonb, text) TO service_role;
