-- =============================================================================
-- V63 — Widen public.certification_domains.weight to numeric(5,1)
-- Created : 2026-07-14 10:00:00 UTC
-- Author  : CertBound automated migration (SCC-EXP-03A)
--
-- Purpose
-- -------
-- Salesforce Certified Sales Cloud Consultant (SCC-EXP-02/03) publishes
-- one-decimal domain weights (e.g. 23.3, 18.3, 13.3) that a pre-existing
-- integer column cannot store without silent, lossy rounding. This
-- migration widens the shared `weight` column so a future, separate
-- catalog migration can insert those exact decimal values -- this
-- migration itself inserts, updates, or deletes no rows and adds no
-- Sales Cloud Consultant (or any other) catalog data.
--
-- Verified pre-migration production state (read-only inspection, SCC-EXP-03A)
-- -----------------------------------------------------------------------
--   * public.certification_domains.weight runtime_type = integer
--   * existing row count                              = 19
--   * minimum weight                                  = 8
--   * maximum weight                                  = 28
--   * existing fractional rows                        = 0
--
-- Schema change
-- -------------
--     ALTER TABLE public.certification_domains
--         ALTER COLUMN weight TYPE numeric(5,1)
--         USING weight::numeric(5,1);
--
-- `numeric(5,1)` accommodates every existing integer value (8-28, stored
-- exactly as e.g. 23.0) and every currently published Sales Cloud
-- Consultant decimal weight (23.3, 20.0, 25.0, 18.3, 13.3) with headroom
-- for values up to 999.9, which is far beyond any plausible single-domain
-- percentage weight. The explicit `USING weight::numeric(5,1)` cast
-- preserves every existing integer value exactly (PostgreSQL performs this
-- as a single in-place type conversion per row; it is not a business-logic
-- UPDATE statement and does not touch any other column).
--
-- Conflict / idempotency safety
-- ------------------------------
-- Before altering anything, this migration reads the column's current type
-- from information_schema:
--   * Already numeric(5,1) (this migration previously applied successfully)
--     -> RAISE NOTICE; safe no-op re-run.
--   * integer (expected pre-migration state)
--     -> performs the ALTER TABLE above.
--   * anything else (a different numeric precision/scale, a type this
--     migration did not anticipate, or a missing column)
--     -> RAISE EXCEPTION naming the unexpected state; aborts and changes
--        nothing. This never silently "succeeds" against an unrecognized
--        starting shape.
--
-- Explicitly out of scope / unaffected
-- -------------------------------------
--   * No rows in certification_domains (or certifications) are inserted,
--     updated, or deleted by this migration.
--   * No Sales Cloud Consultant (or any other) catalog data is added here
--     -- see the separate, later SCC catalog migration.
--   * No RLS policy, GRANT/REVOKE, index, or any other column is touched.
--   * No CHECK constraint is added or removed: inspection of this
--     migration and every prior migration touching certification_domains
--     (20260713224500_v61_add_platform_app_builder_certification_catalog.sql,
--     the only migration that writes to `weight`) found no CHECK
--     constraint referencing `weight` in this repository's migration
--     history; none is introduced here.
-- =============================================================================

DO $$
DECLARE
    v_data_type          text;
    v_numeric_precision  integer;
    v_numeric_scale      integer;
BEGIN
    SELECT data_type, numeric_precision, numeric_scale
    INTO v_data_type, v_numeric_precision, v_numeric_scale
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'certification_domains'
      AND column_name = 'weight';

    IF v_data_type IS NULL THEN
        RAISE EXCEPTION
            'V63: public.certification_domains.weight column not found. Migration cannot be applied.';
    END IF;

    IF v_data_type = 'numeric' AND v_numeric_precision = 5 AND v_numeric_scale = 1 THEN
        RAISE NOTICE
            'V63: public.certification_domains.weight is already numeric(5,1); no changes made.';
        RETURN;
    END IF;

    IF v_data_type <> 'integer' THEN
        RAISE EXCEPTION
            'V63: public.certification_domains.weight has unexpected type % (numeric_precision=%, numeric_scale=%); expected integer (pre-migration) or numeric(5,1) (already migrated). Refusing to proceed -- review manually before re-running this migration.',
            v_data_type, v_numeric_precision, v_numeric_scale;
    END IF;

    ALTER TABLE public.certification_domains
        ALTER COLUMN weight TYPE numeric(5,1)
        USING weight::numeric(5,1);

    RAISE NOTICE
        'V63: widened public.certification_domains.weight from integer to numeric(5,1). No rows were inserted, updated, or deleted.';
END;
$$;
