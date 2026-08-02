-- =============================================================================
-- BA-CAT-01 -- Business Analyst Certification Catalog Verification
-- =============================================================================
--
-- Run as service_role after applying:
--   20260802175000_v70_business_analyst_catalog.sql
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
-- contract tests in tests/test_add_business_analyst_catalog_migration.py --
-- never against this script's target database, because deliberately
-- provoking those failures here would require corrupting the exact-match
-- state this script expects to find.
--
-- Usage:
--   psql "$DATABASE_URL" -f supabase/tests/v70_business_analyst_catalog_verification.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- S1: exactly one Business Analyst certification row, with the exact
--     canonical exam_name, certification code, passing score, time limit,
--     is_active = false, and question_count = 0.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_exam_name text := 'Salesforce Certified Business Analyst';
    v_row       public.certifications;
    v_count     integer;
BEGIN
    SELECT count(*) INTO v_count
    FROM public.certifications
    WHERE exam_name = v_exam_name;

    ASSERT v_count = 1,
        format('S1: expected exactly 1 certifications row for %L, found %s', v_exam_name, v_count);

    SELECT * INTO v_row FROM public.certifications WHERE exam_name = v_exam_name;

    ASSERT v_row.exam_name = 'Salesforce Certified Business Analyst',
        format('S1: exam_name must be exactly %L, found %L', v_exam_name, v_row.exam_name);
    ASSERT v_row.certification_code = 'BA-201',
        format('S1: certification_code must be BA-201, found %L', v_row.certification_code);
    ASSERT v_row.passing_score = 65,
        format('S1: passing_score must be 65, found %L', v_row.passing_score);
    ASSERT v_row.time_limit_minutes = 105,
        format('S1: time_limit_minutes must be 105, found %L', v_row.time_limit_minutes);
    ASSERT v_row.is_active = false,
        'S1: certifications.is_active must be false -- Business Analyst must not be exposed to end users yet';
    ASSERT v_row.question_count = 0,
        format('S1: certifications.question_count must be 0, found %L', v_row.question_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S2: exactly six certification_domains rows for Business Analyst, matching
--     the engine profile's domain names, exact integer weights, and exact
--     display_order values, all is_active = true (so
--     certification_domain_exists() succeeds).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_exam_name          text := 'Salesforce Certified Business Analyst';
    v_count              integer;
    v_exact_match_count  integer;
    v_inactive_count     integer;
BEGIN
    SELECT count(*) INTO v_count
    FROM public.certification_domains
    WHERE exam_name = v_exam_name;

    ASSERT v_count = 6,
        format('S2: expected exactly 6 certification_domains rows for %L, found %s', v_exam_name, v_count);

    SELECT count(*) INTO v_exact_match_count
    FROM public.certification_domains cd
    JOIN (VALUES
        ('Customer Discovery',              17::numeric(5,1), 1),
        ('Collaboration with Stakeholders', 17::numeric(5,1), 2),
        ('Business Process Mapping',        17::numeric(5,1), 3),
        ('Requirements',                    17::numeric(5,1), 4),
        ('User Stories',                    16::numeric(5,1), 5),
        ('User Acceptance',                 16::numeric(5,1), 6)
    ) AS expected(domain_name, weight, display_order)
      ON cd.domain_name = expected.domain_name
     AND cd.weight = expected.weight
     AND cd.display_order = expected.display_order
    WHERE cd.exam_name = v_exam_name;

    ASSERT v_exact_match_count = 6,
        format('S2: expected all 6 domain rows to match the engine profile exactly on name/weight/display_order, %s matched', v_exact_match_count);

    SELECT count(*) INTO v_inactive_count
    FROM public.certification_domains
    WHERE exam_name = v_exam_name
      AND is_active = false;

    ASSERT v_inactive_count = 0,
        format('S2: expected all 6 Business Analyst domain rows to be is_active = true, %s are inactive', v_inactive_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S3: no duplicate-looking domain rows, and no unexpected domain names, for
--     Business Analyst.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_dup_count      integer;
    v_unexpected_cnt integer;
BEGIN
    SELECT count(*) INTO v_dup_count
    FROM (
        SELECT domain_name
        FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Business Analyst'
        GROUP BY domain_name
        HAVING count(*) > 1
    ) dups;

    ASSERT v_dup_count = 0,
        format('S3: expected no duplicate domain_name rows for Business Analyst, found %s duplicated name(s)', v_dup_count);

    SELECT count(*) INTO v_unexpected_cnt
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Business Analyst'
      AND domain_name NOT IN (
          'Customer Discovery',
          'Collaboration with Stakeholders',
          'Business Process Mapping',
          'Requirements',
          'User Stories',
          'User Acceptance'
      );

    ASSERT v_unexpected_cnt = 0,
        format('S3: expected no Business Analyst domain rows outside the 6 canonical names, found %s unexpected row(s)', v_unexpected_cnt);
END;
$$;

-- ---------------------------------------------------------------------------
-- S4: the six integer weights are preserved exactly and total exactly 100.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_total numeric(6,1);
BEGIN
    SELECT sum(weight) INTO v_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Business Analyst';

    ASSERT v_total = 100,
        format('S4: expected Business Analyst domain weights to total exactly 100, found %s', v_total);

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Business Analyst'
          AND domain_name = 'Customer Discovery'
          AND weight = 17
    ), 'S4: Customer Discovery weight must be exactly 17';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Business Analyst'
          AND domain_name = 'User Stories'
          AND weight = 16
    ), 'S4: User Stories weight must be exactly 16';

    ASSERT EXISTS (
        SELECT 1 FROM public.certification_domains
        WHERE exam_name = 'Salesforce Certified Business Analyst'
          AND domain_name = 'User Acceptance'
          AND weight = 16
    ), 'S4: User Acceptance weight must be exactly 16';
END;
$$;

-- ---------------------------------------------------------------------------
-- S5: every Business Analyst certification_domains row references the
--     Business Analyst certification (FK correctness / no cross-linking).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_orphan_count integer;
BEGIN
    SELECT count(*) INTO v_orphan_count
    FROM public.certification_domains cd
    WHERE cd.exam_name = 'Salesforce Certified Business Analyst'
      AND NOT EXISTS (
          SELECT 1 FROM public.certifications c
          WHERE c.exam_name = cd.exam_name
      );

    ASSERT v_orphan_count = 0,
        format('S5: expected every Business Analyst certification_domains row to reference an existing certifications row, found %s orphan row(s)', v_orphan_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S6: existing non-Business-Analyst certification rows already present in
--     this database (Platform App Builder, Sales Cloud Consultant, Service
--     Cloud Consultant) remain intact, unmodified, and still total 100
--     (or the officially-published SCC total) on their own domains. This
--     migration must not have touched them.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_pab_count integer;
    v_scc_count integer;
    v_svc_count integer;
    v_pab_total numeric(6,1);
    v_scc_total numeric(6,1);
    v_svc_total numeric(6,1);
BEGIN
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

    SELECT count(*) INTO v_svc_count
    FROM public.certifications
    WHERE exam_name = 'Salesforce Certified Service Cloud Consultant';

    ASSERT v_svc_count = 1,
        format('S6: expected exactly 1 Service Cloud Consultant certifications row, found %s', v_svc_count);

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

    SELECT sum(weight) INTO v_svc_total
    FROM public.certification_domains
    WHERE exam_name = 'Salesforce Certified Service Cloud Consultant';

    ASSERT v_svc_total = 100,
        format('S6: expected Service Cloud Consultant domain weights to still total 100, found %s', v_svc_total);
END;
$$;

-- ---------------------------------------------------------------------------
-- S7: no scenario, learner, or attempt rows were inserted by this migration
--     (this migration only ever touches public.certifications /
--     public.certification_domains).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_scenario_count integer;
    v_version_count  integer;
    v_attempt_count  integer;
    v_decision_count integer;
BEGIN
    SELECT count(*) INTO v_scenario_count FROM public.scenarios;
    SELECT count(*) INTO v_version_count FROM public.scenario_versions;
    SELECT count(*) INTO v_attempt_count FROM public.scenario_attempts;
    SELECT count(*) INTO v_decision_count FROM public.scenario_decisions;

    ASSERT v_scenario_count = 0,
        format('S7: expected zero public.scenarios rows after V70, found %s', v_scenario_count);
    ASSERT v_version_count = 0,
        format('S7: expected zero public.scenario_versions rows after V70, found %s', v_version_count);
    ASSERT v_attempt_count = 0,
        format('S7: expected zero public.scenario_attempts rows after V70, found %s', v_attempt_count);
    ASSERT v_decision_count = 0,
        format('S7: expected zero public.scenario_decisions rows after V70, found %s', v_decision_count);
END;
$$;

-- ---------------------------------------------------------------------------
-- S8: RLS remains enabled on both catalog tables (V70 does not touch RLS).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_cert_rls   boolean;
    v_domain_rls boolean;
BEGIN
    SELECT relrowsecurity INTO v_cert_rls
    FROM pg_class
    WHERE oid = 'public.certifications'::regclass;

    SELECT relrowsecurity INTO v_domain_rls
    FROM pg_class
    WHERE oid = 'public.certification_domains'::regclass;

    ASSERT v_cert_rls = true,
        'S8: expected RLS to remain enabled on public.certifications';
    ASSERT v_domain_rls = true,
        'S8: expected RLS to remain enabled on public.certification_domains';
END;
$$;
