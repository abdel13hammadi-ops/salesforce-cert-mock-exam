-- =============================================================================
-- SCC-EXP-03B — Sales Cloud Consultant Certification Catalog Verification
-- =============================================================================
--
-- Run as service_role after applying:
--   20260714110000_v64_add_sales_cloud_consultant_certification_catalog.sql
--
-- Asserts the post-migration catalog state (Case 1: fresh insert, or
-- Case 2: already-exact re-apply no-op -- both converge to the same final
-- shape). Schema-only, read-only assertions; nothing is inserted, updated,
-- or rolled back by this script.
--
-- Conflict-detection (Case 3: a different certification_code, a different
-- passing_score/time_limit_minutes, is_active/question_count drift, a
-- partial or duplicate domain set, a weight/display_order mismatch, or
-- orphaned domain rows) is covered separately by the static migration
-- contract tests in tests/test_add_sales_cloud_consultant_catalog_migration.py
-- -- never against this script's target database, because deliberately
-- provoking those failures here would require corrupting the exact-match
-- state this script expects to find.
--
-- Usage:
--   psql "$DATABASE_URL" -f supabase/tests/v64_sales_cloud_consultant_certification_catalog_verification.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- S1: exactly one Sales Cloud Consultant certification row, with the exact
--     canonical exam_name, certification code, passing score, time limit,
--     is_active = false, and question_count = 0.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_exam_name text := 'Salesforce Certified Sales Cloud Consultant';
    v_row       public.certifications;
    v_count     integer;
BEGIN
    SELECT count(*) INTO v_count
    FROM public.certifications
    WHERE exam_name = v_exam_name;

    ASSERT v_count = 1,
        format('S1: expected exactly 1 certifications row for %L, found %s', v_exam_name, v_count);

    SELECT * INTO v_row FROM public.certifications WHERE exam_name = v_exam_name;

    ASSERT v_row.exam_name = 'Salesforce Certified Sales Cloud Consultant',
        format('S1: exam_name must be exactly %L, found %L', v_exam_name, v_row.exam_name);
    ASSERT v_row.certification_code = 'Sales-Con-201',
        format('S1: certification_code must be Sales-Con-201, found %L', v_row.certification_code);
    ASSERT v_row.passing_score = 73,
        format('S1: passing_score must be 73, found %L', v_row.passing_score);
    ASSERT v_row.time_limit_minutes = 105,
        format('S1: time_limit_minutes must be 105, found %L', v_row.time_limit_minutes);
    ASSERT v_row.is_active = false,
        'S1: certifications.is_active must be false -- SCC must not be exposed to end users yet';
    ASSERT v_row.question_count = 0,
        format('S1: certifications.question_count must be 0, found %L', v_row.question_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S2: exactly five certification_domains rows for Sales Cloud Consultant,
--     matching the engine profile's domain names and exact decimal weights,
--     all is_active = true (so certification_domain_exists() succeeds).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_exam_name          text := 'Salesforce Certified Sales Cloud Consultant';
    v_count              integer;
    v_exact_match_count  integer;
    v_inactive_count     integer;
BEGIN
    SELECT count(*) INTO v_count
    FROM public.certification_domains
    WHERE exam_name = v_exam_name;

    ASSERT v_count = 5,
        format('S2: expected exactly 5 certification_domains rows for %L, found %s', v_exam_name, v_count);

    SELECT count(*) INTO v_exact_match_count
    FROM public.certification_domains cd
    JOIN (VALUES
        ('Practical Application of Sales Cloud Expertise', 23.3::numeric(5,1), 1),
        ('Sales Lifecycle',                                20.0::numeric(5,1), 2),
        ('Consulting & Implementation Strategies',        25.0::numeric(5,1), 3),
        ('Data Management',                               18.3::numeric(5,1), 4),
        ('Predictive and Generative AI',                  13.3::numeric(5,1), 5)
    ) AS expected(domain_name, weight, display_order)
      ON cd.domain_name = expected.domain_name
     AND cd.weight = expected.weight
     AND cd.display_order = expected.display_order
    WHERE cd.exam_name = v_exam_name;

    ASSERT v_exact_match_count = 5,
        format('S2: expected all 5 domain rows to match the engine profile exactly on name/weight/display_order, %s matched', v_exact_match_count);

    SELECT count(*) INTO v_inactive_count
    FROM public.certification_domains
    WHERE exam_name = v_exam_name
      AND is_active = false;

    ASSERT v_inactive_count = 0,
        format('S2: expected all 5 Sales Cloud Consultant domain rows to be is_active = true, %s are inactive', v_inactive_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S3: no duplicate-looking domain rows for Sales Cloud Consultant.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_dup_count integer;
BEGIN
    SELECT count(*) INTO v_dup_count
    FROM (
        SELECT domain_name
        FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Sales Cloud Consultant'
        GROUP BY domain_name
        HAVING count(*) > 1
    ) dups;

    ASSERT v_dup_count = 0,
        format('S3: expected no duplicate domain_name rows for Sales Cloud Consultant, found %s duplicated name(s)', v_dup_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S4: the five decimal weights are preserved exactly (no rounding, no
--     truncation to integer) and total to approximately 99.9 -- the
--     officially published one-decimal total -- with a small explicit
--     tolerance, never forced to exactly 100.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_total   numeric(6,1);
    v_delta   numeric(6,1);
BEGIN
    SELECT sum(weight) INTO v_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Sales Cloud Consultant';

    v_delta := abs(v_total - 99.9);

    ASSERT v_delta <= 0.15,
        format('S4: expected Sales Cloud Consultant domain weights to total approximately 99.9 (tolerance 0.15), found %s (delta %s)', v_total, v_delta);

    ASSERT v_total <> 100,
        format('S4: Sales Cloud Consultant domain weights must not be forced/normalized to exactly 100, found %s', v_total);

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Sales Cloud Consultant'
          AND domain_name = 'Practical Application of Sales Cloud Expertise'
          AND weight = 23.3
    ), 'S4: Practical Application of Sales Cloud Expertise weight must be exactly 23.3, not rounded/truncated';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Sales Cloud Consultant'
          AND domain_name = 'Data Management'
          AND weight = 18.3
    ), 'S4: Data Management weight must be exactly 18.3, not rounded/truncated';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Sales Cloud Consultant'
          AND domain_name = 'Predictive and Generative AI'
          AND weight = 13.3
    ), 'S4: Predictive and Generative AI weight must be exactly 13.3, not rounded/truncated';
END;
$$;

-- ---------------------------------------------------------------------------
-- S5: the weight column remains numeric(5,1) (SCC data did not require or
--     trigger any further schema change beyond the V63 widening).
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
        format('S5: expected weight data_type=numeric, found %L', v_data_type);
    ASSERT v_numeric_precision = 5,
        format('S5: expected weight numeric_precision=5, found %L', v_numeric_precision);
    ASSERT v_numeric_scale = 1,
        format('S5: expected weight numeric_scale=1, found %L', v_numeric_scale);
END;
$$;

-- ---------------------------------------------------------------------------
-- S6: existing Administrator, Business Analyst, and Platform App Builder
--     certification rows remain intact and unmodified (existence and
--     is_active status; this migration never wrote to them).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_admin_count integer;
    v_ba_count    integer;
    v_pab_count   integer;
    v_admin_total numeric(6,1);
    v_ba_total    numeric(6,1);
    v_pab_total   numeric(6,1);
BEGIN
    SELECT count(*) INTO v_admin_count
    FROM public.certifications
    WHERE exam_name = 'Salesforce Certified Platform Administrator'
      AND is_active = true;

    ASSERT v_admin_count = 1,
        format('S6: expected exactly 1 active Administrator certifications row, found %s', v_admin_count);

    SELECT count(*) INTO v_ba_count
    FROM public.certifications
    WHERE exam_name = 'Salesforce Certified Business Analyst'
      AND is_active = true;

    ASSERT v_ba_count = 1,
        format('S6: expected exactly 1 active Business Analyst certifications row, found %s', v_ba_count);

    SELECT count(*) INTO v_pab_count
    FROM public.certifications
    WHERE exam_name = 'Salesforce Certified Platform App Builder';

    ASSERT v_pab_count = 1,
        format('S6: expected exactly 1 Platform App Builder certifications row, found %s', v_pab_count);

    SELECT sum(weight) INTO v_admin_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Platform Administrator';

    ASSERT v_admin_total = 100,
        format('S6: expected Administrator domain weights to still total 100, found %s', v_admin_total);

    SELECT sum(weight) INTO v_ba_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Business Analyst';

    ASSERT v_ba_total = 100,
        format('S6: expected Business Analyst domain weights to still total 100, found %s', v_ba_total);

    SELECT sum(weight) INTO v_pab_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Platform App Builder';

    ASSERT v_pab_total = 100,
        format('S6: expected Platform App Builder domain weights to still total 100, found %s', v_pab_total);
END;
$$;
