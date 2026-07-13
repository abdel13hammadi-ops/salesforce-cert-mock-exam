-- =============================================================================
-- PAB-EXP-03 — Platform App Builder Certification Catalog Verification
-- =============================================================================
--
-- Run as service_role after applying:
--   20260713224500_v61_add_platform_app_builder_certification_catalog.sql
--
-- Asserts the post-migration catalog state (Case 1: fresh insert, or
-- Case 2: already-exact re-apply no-op — both converge to the same final
-- shape). Schema-only, read-only assertions; nothing is inserted, updated,
-- or rolled back by this script.
--
-- Conflict-detection (Case 3: a different certification_code, a different
-- passing_score/time_limit_minutes, a partial or duplicate domain set, a
-- weight mismatch, or orphaned domain rows) was exercised separately during
-- PAB-EXP-03 / PAB-EXP-03A against disposable, throwaway databases — never
-- against this script's target database — because deliberately provoking
-- those failures here would require corrupting the exact-match state this
-- script expects to find. See the PAB-EXP-03 / PAB-EXP-03A completion
-- reports for that verification's results.
--
-- Usage:
--   psql "$DATABASE_URL" -f supabase/tests/v61_platform_app_builder_certification_catalog_verification.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- S1: exactly one Platform App Builder certification row, with the exact
--     canonical exam_name, the semantic (non-numeric) certification_code,
--     and is_active = false (withheld from end-user exam selection until a
--     human explicitly activates it).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_exam_name text := 'Salesforce Certified Platform App Builder';
    v_row       public.certifications;
    v_count     integer;
BEGIN
    SELECT count(*) INTO v_count
    FROM public.certifications
    WHERE exam_name = v_exam_name;

    ASSERT v_count = 1,
        format('S1: expected exactly 1 certifications row for %L, found %s', v_exam_name, v_count);

    SELECT * INTO v_row FROM public.certifications WHERE exam_name = v_exam_name;

    ASSERT v_row.certification_code = 'platform_app_builder',
        format('S1: certification_code must be the semantic id platform_app_builder, found %L', v_row.certification_code);
    ASSERT v_row.certification_code IS DISTINCT FROM 'APP-401',
        'S1: certification_code must never be APP-401 (no repository evidence for that code; see PAB-EXP-02)';
    ASSERT v_row.is_active = false,
        'S1: certifications.is_active must be false until a human explicitly activates Platform App Builder for end users';
    ASSERT v_row.passing_score = 63,
        format('S1: passing_score must be 63 per the official exam guide, found %L', v_row.passing_score);
    ASSERT v_row.time_limit_minutes = 105,
        format('S1: time_limit_minutes must be 105 per the official exam guide, found %L', v_row.time_limit_minutes);
END;
$$;

-- ---------------------------------------------------------------------------
-- S2: exactly five certification_domains rows for Platform App Builder,
--     matching the engine profile's domain names, weights, and display
--     order exactly, all is_active = true (so certification_domain_exists()
--     succeeds for every one of them).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_exam_name text := 'Salesforce Certified Platform App Builder';
    v_count     integer;
    v_exact_match_count integer;
    v_inactive_count integer;
BEGIN
    SELECT count(*) INTO v_count
    FROM public.certification_domains
    WHERE exam_name = v_exam_name;

    ASSERT v_count = 5,
        format('S2: expected exactly 5 certification_domains rows for %L, found %s', v_exam_name, v_count);

    SELECT count(*) INTO v_exact_match_count
    FROM public.certification_domains cd
    JOIN (VALUES
        ('Salesforce Fundamentals', 23, 1),
        ('User Interface', 17, 2),
        ('Data Modeling and Management', 22, 3),
        ('Business Logic and Process Automation', 28, 4),
        ('App Deployment', 10, 5)
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
        format('S2: expected all 5 Platform App Builder domain rows to be is_active = true, %s are inactive', v_inactive_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S3: no duplicate-looking domain rows for Platform App Builder.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_dup_count integer;
BEGIN
    SELECT count(*) INTO v_dup_count
    FROM (
        SELECT domain_name
        FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Platform App Builder'
        GROUP BY domain_name
        HAVING count(*) > 1
    ) dups;

    ASSERT v_dup_count = 0,
        format('S3: expected no duplicate domain_name rows for Platform App Builder, found %s duplicated name(s)', v_dup_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S4: existing Administrator and Business Analyst certification/domain rows
--     were not modified by this migration (row counts unchanged from any
--     prior known-good baseline is out of scope here without a snapshot,
--     but their existence and is_active status must be intact).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_admin_count integer;
    v_ba_count    integer;
BEGIN
    SELECT count(*) INTO v_admin_count
    FROM public.certifications
    WHERE exam_name = 'Salesforce Certified Platform Administrator'
      AND is_active = true;

    ASSERT v_admin_count = 1,
        format('S4: expected exactly 1 active Administrator certifications row, found %s', v_admin_count);

    SELECT count(*) INTO v_ba_count
    FROM public.certifications
    WHERE exam_name = 'Salesforce Certified Business Analyst'
      AND is_active = true;

    ASSERT v_ba_count = 1,
        format('S4: expected exactly 1 active Business Analyst certifications row, found %s', v_ba_count);
END;
$$;
