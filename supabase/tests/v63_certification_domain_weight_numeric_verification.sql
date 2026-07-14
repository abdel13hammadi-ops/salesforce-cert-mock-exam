-- =============================================================================
-- V63 — certification_domains.weight numeric(5,1) widening verification
-- =============================================================================
--
-- Run as service_role after applying:
--   20260714100000_v63_widen_certification_domain_weight_to_numeric.sql
--
-- Schema/read-only assertions (S1-S4) run unconditionally against whatever
-- data is currently in the table and are safe on any environment. The
-- fractional-representation check (S5) inserts one throwaway row inside a
-- BEGIN...ROLLBACK block so nothing is permanently persisted.
--
-- Usage:
--   psql "$DATABASE_URL" -f supabase/tests/v63_certification_domain_weight_numeric_verification.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- S1: weight column is numeric with scale 1 (precision 5).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_data_type         text;
    v_numeric_precision integer;
    v_numeric_scale     integer;
BEGIN
    SELECT data_type, numeric_precision, numeric_scale
    INTO v_data_type, v_numeric_precision, v_numeric_scale
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'certification_domains'
      AND column_name = 'weight';

    ASSERT v_data_type = 'numeric',
        format('S1: expected weight data_type=numeric, found %L', v_data_type);
    ASSERT v_numeric_precision = 5,
        format('S1: expected weight numeric_precision=5, found %L', v_numeric_precision);
    ASSERT v_numeric_scale = 1,
        format('S1: expected weight numeric_scale=1, found %L', v_numeric_scale);
END;
$$;

-- ---------------------------------------------------------------------------
-- S2: all 19 pre-migration rows still exist (no rows were inserted or
--     deleted by the widening migration itself).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count integer;
BEGIN
    SELECT count(*) INTO v_count FROM public.certification_domains;

    ASSERT v_count = 19,
        format('S2: expected exactly 19 certification_domains rows (pre-migration baseline), found %s', v_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S3: no existing value changed -- every stored weight is still an exact
--     whole number (scale-0 value, e.g. 23.0) and the known min/max from
--     the verified pre-migration baseline (8 and 28) are unchanged. This
--     migration never wrote a fractional value into any existing row.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_non_integral_count integer;
    v_min_weight          numeric(5,1);
    v_max_weight          numeric(5,1);
BEGIN
    SELECT count(*) INTO v_non_integral_count
    FROM public.certification_domains
    WHERE weight <> trunc(weight);

    ASSERT v_non_integral_count = 0,
        format('S3: expected 0 fractional weight values among existing rows (pre-migration baseline had none), found %s', v_non_integral_count);

    SELECT min(weight), max(weight) INTO v_min_weight, v_max_weight
    FROM public.certification_domains;

    ASSERT v_min_weight = 8,
        format('S3: expected minimum weight=8 (unchanged from pre-migration baseline), found %s', v_min_weight);
    ASSERT v_max_weight = 28,
        format('S3: expected maximum weight=28 (unchanged from pre-migration baseline), found %s', v_max_weight);
END;
$$;

-- ---------------------------------------------------------------------------
-- S4: existing integer certifications (Administrator, Business Analyst,
--     Platform App Builder) remain numerically equivalent -- each still
--     totals exactly 100 and no domain row's weight value changed.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_admin_total numeric(6,1);
    v_ba_total    numeric(6,1);
    v_pab_total   numeric(6,1);
BEGIN
    SELECT sum(weight) INTO v_admin_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Platform Administrator';

    ASSERT v_admin_total = 100,
        format('S4: expected Administrator domain weights to total 100, found %s', v_admin_total);

    SELECT sum(weight) INTO v_ba_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Business Analyst';

    ASSERT v_ba_total = 100,
        format('S4: expected Business Analyst domain weights to total 100, found %s', v_ba_total);

    SELECT sum(weight) INTO v_pab_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Platform App Builder';

    ASSERT v_pab_total = 100,
        format('S4: expected Platform App Builder domain weights to total 100, found %s', v_pab_total);
END;
$$;

-- ---------------------------------------------------------------------------
-- S5: the widened column can represent a fractional weight (e.g. 23.3)
--     accurately. Runs inside its own BEGIN...ROLLBACK so the throwaway
--     probe row is never permanently persisted.
-- ---------------------------------------------------------------------------
BEGIN;

DO $$
DECLARE
    v_probe_exam text := 'V63-VERIFY-FRACTIONAL-PROBE';
    v_readback   numeric(5,1);
BEGIN
    INSERT INTO public.certifications (
        exam_name, display_name, certification_code,
        passing_score, time_limit_minutes, question_count, is_active
    ) VALUES (
        v_probe_exam, 'V63 Verify Fractional Probe', 'V63VERIFY',
        70, 90, 0, false
    );

    INSERT INTO public.certification_domains (
        exam_name, domain_name, weight, question_count, display_order, is_active
    ) VALUES (
        v_probe_exam, 'Fractional Probe Domain', 23.3, 0, 1, false
    );

    SELECT weight INTO v_readback
    FROM public.certification_domains
    WHERE exam_name = v_probe_exam
      AND domain_name = 'Fractional Probe Domain';

    ASSERT v_readback = 23.3,
        format('S5: expected fractional probe weight to read back as exactly 23.3, found %s', v_readback);
END;
$$;

ROLLBACK;

-- ---------------------------------------------------------------------------
-- S6: the fractional probe row from S5 was not permanently persisted.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_probe_count integer;
BEGIN
    SELECT count(*) INTO v_probe_count
    FROM public.certification_domains
    WHERE exam_name = 'V63-VERIFY-FRACTIONAL-PROBE';

    ASSERT v_probe_count = 0,
        format('S6: expected the S5 fractional probe row to have been rolled back, found %s remaining row(s)', v_probe_count);
END;
$$;
