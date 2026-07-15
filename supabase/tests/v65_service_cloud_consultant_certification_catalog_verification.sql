-- =============================================================================
-- SVC-EXP-02 — Service Cloud Consultant Certification Catalog Verification
-- =============================================================================
--
-- Run as service_role after applying:
--   20260714120000_v65_add_service_cloud_consultant_certification_catalog.sql
--
-- Asserts the post-migration catalog state (Case 1: fresh insert, or
-- Case 2: already-exact re-apply no-op -- both converge to the same final
-- shape). Schema-only, read-only assertions; nothing is inserted, updated,
-- or rolled back by this script.
--
-- Conflict-detection (Case 3: a different certification_code, a different
-- passing_score/time_limit_minutes, is_active/question_count drift, a
-- partial or duplicate domain set, a weight/display_order mismatch, or
-- orphaned domain rows) is covered by the migration's own RAISE EXCEPTION
-- guards -- never against this script's target database, because deliberately
-- provoking those failures here would require corrupting the exact-match
-- state this script expects to find.
--
-- Official exam code note
-- -----------------------
-- The verified official Salesforce exam code is 'Service-Con-201'. The
-- public.certifications table has no separate official-exam-code column;
-- certification_code stores the internal identifier 'service_cloud_consultant'
-- only. This script verifies that internal code and documents the official
-- exam code in comments above.
--
-- Usage:
--   psql "$DATABASE_URL" -f supabase/tests/v65_service_cloud_consultant_certification_catalog_verification.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- S1: exactly one Service Cloud Consultant certification row, with the exact
--     canonical exam_name, internal certification code, passing score,
--     time limit, is_active = false, and question_count = 0.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_exam_name text := 'Salesforce Certified Service Cloud Consultant';
    v_row       public.certifications;
    v_count     integer;
BEGIN
    SELECT count(*) INTO v_count
    FROM public.certifications
    WHERE exam_name = v_exam_name;

    ASSERT v_count = 1,
        format('S1: expected exactly 1 certifications row for %L, found %s', v_exam_name, v_count);

    SELECT * INTO v_row FROM public.certifications WHERE exam_name = v_exam_name;

    ASSERT v_row.exam_name = 'Salesforce Certified Service Cloud Consultant',
        format('S1: exam_name must be exactly %L, found %L', v_exam_name, v_row.exam_name);
    ASSERT v_row.certification_code = 'service_cloud_consultant',
        format('S1: certification_code must be service_cloud_consultant (internal code), found %L', v_row.certification_code);
    ASSERT v_row.certification_code IS DISTINCT FROM 'Service-Con-201',
        'S1: certification_code must not be the official exam code Service-Con-201; internal code service_cloud_consultant is required';
    ASSERT v_row.passing_score = 78,
        format('S1: passing_score must be 78, found %L', v_row.passing_score);
    ASSERT v_row.time_limit_minutes = 105,
        format('S1: time_limit_minutes must be 105, found %L', v_row.time_limit_minutes);
    ASSERT v_row.is_active = false,
        'S1: certifications.is_active must be false -- SVC must not be exposed to end users yet';
    ASSERT v_row.question_count = 0,
        format('S1: certifications.question_count must be 0, found %L', v_row.question_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S2: exactly eight certification_domains rows for Service Cloud Consultant,
--     matching the engine profile's domain names, integer weights, and
--     display_order 1 through 8, all is_active = true (so
--     certification_domain_exists() succeeds).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_exam_name          text := 'Salesforce Certified Service Cloud Consultant';
    v_count              integer;
    v_exact_match_count  integer;
    v_inactive_count     integer;
BEGIN
    SELECT count(*) INTO v_count
    FROM public.certification_domains
    WHERE exam_name = v_exam_name;

    ASSERT v_count = 8,
        format('S2: expected exactly 8 certification_domains rows for %L, found %s', v_exam_name, v_count);

    SELECT count(*) INTO v_exact_match_count
    FROM public.certification_domains cd
    JOIN (VALUES
        ('Industry Knowledge',               12::numeric(5,1), 1),
        ('Implementation Strategies',        12::numeric(5,1), 2),
        ('Service Cloud Solution Design',    15::numeric(5,1), 3),
        ('Knowledge Management',             12::numeric(5,1), 4),
        ('Intake and Interaction Channels',  13::numeric(5,1), 5),
        ('Case Management',                  13::numeric(5,1), 6),
        ('Contact Center Analytics',         13::numeric(5,1), 7),
        ('Integrations',                     10::numeric(5,1), 8)
    ) AS expected(domain_name, weight, display_order)
      ON cd.domain_name = expected.domain_name
     AND cd.weight = expected.weight
     AND cd.display_order = expected.display_order
    WHERE cd.exam_name = v_exam_name;

    ASSERT v_exact_match_count = 8,
        format('S2: expected all 8 domain rows to match the engine profile exactly on name/weight/display_order, %s matched', v_exact_match_count);

    SELECT count(*) INTO v_inactive_count
    FROM public.certification_domains
    WHERE exam_name = v_exam_name
      AND is_active = false;

    ASSERT v_inactive_count = 0,
        format('S2: expected all 8 Service Cloud Consultant domain rows to be is_active = true, %s are inactive', v_inactive_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S3: no duplicate-looking domain rows for Service Cloud Consultant.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_dup_count integer;
BEGIN
    SELECT count(*) INTO v_dup_count
    FROM (
        SELECT domain_name
        FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Service Cloud Consultant'
        GROUP BY domain_name
        HAVING count(*) > 1
    ) dups;

    ASSERT v_dup_count = 0,
        format('S3: expected no duplicate domain_name rows for Service Cloud Consultant, found %s duplicated name(s)', v_dup_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S4: the eight integer weights total exactly 100 and each individual
--     weight matches the official blueprint exactly.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_total numeric(6,1);
BEGIN
    SELECT sum(weight) INTO v_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Service Cloud Consultant';

    ASSERT v_total = 100,
        format('S4: expected Service Cloud Consultant domain weights to total exactly 100, found %s', v_total);

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Service Cloud Consultant'
          AND domain_name = 'Industry Knowledge'
          AND weight = 12
          AND display_order = 1
    ), 'S4: Industry Knowledge must be weight=12 display_order=1';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Service Cloud Consultant'
          AND domain_name = 'Implementation Strategies'
          AND weight = 12
          AND display_order = 2
    ), 'S4: Implementation Strategies must be weight=12 display_order=2';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Service Cloud Consultant'
          AND domain_name = 'Service Cloud Solution Design'
          AND weight = 15
          AND display_order = 3
    ), 'S4: Service Cloud Solution Design must be weight=15 display_order=3';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Service Cloud Consultant'
          AND domain_name = 'Knowledge Management'
          AND weight = 12
          AND display_order = 4
    ), 'S4: Knowledge Management must be weight=12 display_order=4';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Service Cloud Consultant'
          AND domain_name = 'Intake and Interaction Channels'
          AND weight = 13
          AND display_order = 5
    ), 'S4: Intake and Interaction Channels must be weight=13 display_order=5';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Service Cloud Consultant'
          AND domain_name = 'Case Management'
          AND weight = 13
          AND display_order = 6
    ), 'S4: Case Management must be weight=13 display_order=6';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Service Cloud Consultant'
          AND domain_name = 'Contact Center Analytics'
          AND weight = 13
          AND display_order = 7
    ), 'S4: Contact Center Analytics must be weight=13 display_order=7';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Service Cloud Consultant'
          AND domain_name = 'Integrations'
          AND weight = 10
          AND display_order = 8
    ), 'S4: Integrations must be weight=10 display_order=8';
END;
$$;

-- ---------------------------------------------------------------------------
-- S5: domain names appear in the official blueprint order when sorted by
--     display_order.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ordered_names text[];
    v_expected      text[] := ARRAY[
        'Industry Knowledge',
        'Implementation Strategies',
        'Service Cloud Solution Design',
        'Knowledge Management',
        'Intake and Interaction Channels',
        'Case Management',
        'Contact Center Analytics',
        'Integrations'
    ];
BEGIN
    SELECT array_agg(domain_name ORDER BY display_order)
    INTO v_ordered_names
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Service Cloud Consultant';

    ASSERT v_ordered_names = v_expected,
        format('S5: domain names must appear in official blueprint order, found %s', v_ordered_names);
END;
$$;

-- ---------------------------------------------------------------------------
-- S6: existing Administrator, Business Analyst, Platform App Builder, and
--     Sales Cloud Consultant certification rows remain intact and unmodified
--     (existence checks only; this migration never wrote to them).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_admin_count integer;
    v_ba_count    integer;
    v_pab_count   integer;
    v_scc_count   integer;
    v_admin_total numeric(6,1);
    v_ba_total    numeric(6,1);
    v_pab_total   numeric(6,1);
    v_scc_total   numeric(6,1);
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

    SELECT count(*) INTO v_scc_count
    FROM public.certifications
    WHERE exam_name = 'Salesforce Certified Sales Cloud Consultant';

    ASSERT v_scc_count = 1,
        format('S6: expected exactly 1 Sales Cloud Consultant certifications row, found %s', v_scc_count);

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

    SELECT sum(weight) INTO v_scc_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Sales Cloud Consultant';

    ASSERT abs(v_scc_total - 99.9) <= 0.15,
        format('S6: expected Sales Cloud Consultant domain weights to still total approximately 99.9, found %s', v_scc_total);
END;
$$;
