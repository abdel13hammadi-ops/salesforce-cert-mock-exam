-- =============================================================================
-- BA-CAT-01: Add Salesforce Certified Business Analyst certification catalog
-- rows (public.certifications + public.certification_domains)
-- Created : 2026-08-02 17:50:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Extends the database certification catalog with the inactive Salesforce
-- Certified Business Analyst (BA) row and its six official domain rows,
-- mirroring the established pattern from
-- 20260714120000_v65_add_service_cloud_consultant_certification_catalog.sql
-- (itself following 20260714110000_v64_..._sales_cloud_consultant... and
-- 20260713224500_v61_..._platform_app_builder...). This migration is the
-- database-catalog counterpart to the already-frozen engine profile in
-- workers/certification_registry.py (BA_EXAM_NAME and _ba_definition()) --
-- it inserts a literal, unmodified transcription of those values; it does
-- not duplicate or re-derive that logic in SQL.
--
-- Authoritative source matrix (see also
-- docs/scenario_simulator/CERTBOUND_BUSINESS_ANALYST_CATALOG_MIGRATION_REPORT.md
-- for the full discovery writeup)
-- ------------------------------------------------------------------------
--   certifications.exam_name          = 'Salesforce Certified Business Analyst'
--     Source: workers/certification_registry.py, BA_EXAM_NAME (line 49).
--   certifications.certification_code = 'BA-201'
--     Source: workers/certification_registry.py,
--     CERTIFICATION_CODES[BA_EXAM_NAME] (line 61). Corroborated verbatim by
--     workers/official_evidence_seed.py (CERTIFICATION_CODES[BA_EXAM_NAME] =
--     "BA-201", line 84) and scenario_content/business_analyst/catalog.json
--     ("examCode": "BA-201", both scenario entries).
--   Six domain names/weights (Customer Discovery 17, Collaboration with
--   Stakeholders 17, Business Process Mapping 17, Requirements 17,
--   User Stories 16, User Acceptance 16 -- total 100)
--     Source: workers/certification_registry.py, _ba_definition() (lines
--     185-215). Corroborated verbatim (domain names only, in the same
--     order) by an already-committed, unmodified migration --
--     20260629120000_v46_free_mock_curation_foundation.sql,
--     public.free_mock_blueprint_v1(), the
--     'Salesforce Certified Business Analyst' WHEN-branch (lines 231-238) --
--     and by tests/test_certification_registry.py
--     (test_exact_domain_names, test_domain_weight_total_equals_100,
--     test_business_analyst_identity_and_domains_unchanged), which already
--     assert this exact 6-domain/100-total shape against the registry.
--   passing_score = 65, time_limit_minutes = 105
--     No certification-specific override for Business Analyst exists
--     anywhere in the repository (workers/certification_registry.py does
--     not model exam-timing metadata at all; app.py's PASSING_SCORE_DEFAULT
--     = 68 is an explicitly-documented *generic, non-official* app-wide
--     fallback -- see the V61/PAB-EXP-03 migration header -- and is not
--     used here). The values inserted are the public.certifications table's
--     own column DEFAULTs, defined in
--     20260101000000_v00_certbound_base_schema.sql (passing_score DEFAULT
--     65, time_limit_minutes DEFAULT 105) -- a repository-owned schema
--     artifact reconstructed from, and verified against, live production
--     column defaults during CERTBOUND-DB-BASELINE-01. This migration
--     inserts these two values explicitly (rather than relying on the
--     column DEFAULT silently applying) so the exact expected values are
--     visible in this file and enforced by the Case 2 conflict check below.
--   certifications.id / certification_domains.id
--     No certification in this catalog (Administrator, Platform App
--     Builder, Sales Cloud Consultant, Service Cloud Consultant) uses a
--     hardcoded/stable UUID; every prior catalog migration lets
--     id uuid DEFAULT gen_random_uuid() assign it and treats exam_name
--     (certifications_exam_name_key UNIQUE) as the stable natural
--     identifier. Business Analyst follows the same convention: no
--     certifications.id or certification_domains.id literal is inserted
--     below.
--   Domain "codes" (customer_discovery, collaboration_with_stakeholders,
--   business_process_mapping, requirements, user_stories, user_acceptance)
--     These are workers/certification_registry.py CertificationDomain.
--     domain_id values -- engine-side identifiers only. Mirroring the exact
--     precedent of every prior catalog migration (V61/V64/V65),
--     public.certification_domains has no domain-code/domain-id column
--     (only exam_name, domain_name, weight, question_count, display_order,
--     is_active -- see 20260101000000_v00_certbound_base_schema.sql,
--     lines 158-171); this migration does not add one and does not persist
--     these engine-side codes anywhere.
--   Domain order
--     1=Customer Discovery, 2=Collaboration with Stakeholders,
--     3=Business Process Mapping, 4=Requirements, 5=User Stories,
--     6=User Acceptance -- exactly the order returned by
--     _ba_definition().domains, matching the display_order values inserted
--     below.
--
-- Activation state
-- -----------------
-- The new certifications row is inserted with is_active = false: no
-- human-reviewed Business Analyst question exists in this database's
-- public.questions table (0 rows for any certification at the time this
-- migration is authored), and this migration must not surface an empty
-- certification choice to end users in the exam-taking UI (app.py,
-- pages/Dashboard.py, pages/Practice_By_Category.py,
-- pages/Weak_Areas_Practice.py, ... all of which filter
-- public.certifications on is_active = true). This mirrors the PAB-EXP-03 /
-- SCC-EXP-03B / SVC-EXP-02 precedent exactly.
-- QuestionCandidateRepository.certification_domain_exists() -- the actual
-- gate exercised by candidate generation/audit persistence -- only checks
-- certification_domains.is_active, never certifications.is_active, so this
-- does not block candidate persistence; it only withholds end-user exam
-- exposure until a human explicitly activates it in a separate, reviewed
-- change. The six new certification_domains rows are inserted with
-- is_active = true so certification_domain_exists() succeeds for them.
--
-- Scope note: CB-SC-001 (the Scenario Engine V2 vertical-slice scenario)
-- does not read public.certifications or public.certification_domains at
-- all -- utils/scenario_catalog.py resolves certificationExamName purely
-- from repository JSON (scenario_content/business_analyst/catalog.json),
-- and utils/scenario_streamlit_v2.diagnose_cb_sc001_publication_readiness()
-- only queries public.scenarios / public.scenario_versions. This migration
-- therefore does not, and cannot, change that function's ready/not-ready
-- result; it exists to close a real and separate catalog-completeness gap
-- (Business Analyst is otherwise entirely absent from
-- public.certifications / public.certification_domains despite being a
-- fully-specified engine-profile certification, referenced by an existing
-- committed migration's free_mock_blueprint_v1(), and exercised by the
-- existing certification-registry test suite).
--
-- Conflict safety
-- ---------------
-- Before writing anything, this migration inspects existing state:
--   * Cross-check (always performed first, independent of Case 1/2/3 below):
--     if certification_code = 'BA-201' is already assigned to a
--     certifications row under a *different* exam_name (certification_code
--     has no UNIQUE constraint in the schema, so nothing else prevents
--     this) -> RAISE EXCEPTION immediately.
--   * Case 1 (no certifications row for this exam_name, and no orphaned
--     certification_domains rows referencing it either) -> inserts the
--     certification and all six domains.
--   * Case 2 (a certifications row for this exact exam_name already exists
--     with the expected certification_code, the expected passing_score and
--     time_limit_minutes, the expected is_active=false and question_count=0,
--     and certification_domains for it is exactly the six expected domain
--     names/weights/display_orders, no duplicates) -> leaves every row
--     untouched; safe no-op.
--   * Case 3 (anything else -- a different certification_code, a different
--     exam name casing/spelling, a different passing_score/time_limit_
--     minutes, is_active not false, question_count not 0, a different
--     domain count, a domain name outside the expected six, a renamed
--     domain, a weight or display_order that does not match the engine
--     profile, a duplicate-looking domain row, or certification_domains
--     rows referencing this exam_name with no parent certifications row)
--     -> RAISE EXCEPTION; the migration aborts and writes nothing.
-- ON CONFLICT DO NOTHING / DO UPDATE is intentionally not used anywhere
-- below, because either would silently accept a partially-seeded or
-- conflicting catalog row instead of surfacing it for review.
--
-- Isolation
-- ---------
-- This migration only ever reads or writes rows scoped to
-- exam_name = 'Salesforce Certified Business Analyst'. It never updates,
-- deletes, or even writes to rows for Administrator, Platform App Builder,
-- Sales Cloud Consultant, or Service Cloud Consultant. It never touches
-- public.questions, public.answer_options, public.scenarios,
-- public.scenario_versions, or any other table.
--
-- Deliberately excluded from the exact-match check: none. This migration's
-- Case 2 definition explicitly requires question_count=0 and is_active=false
-- to match exactly, since BA-CAT-01 intentionally inserts Business Analyst
-- in that exact initial state and no later step has changed it yet at the
-- time this migration is authored.
-- =============================================================================

DO $$
DECLARE
    v_exam_name          text := 'Salesforce Certified Business Analyst';
    v_cert_code          text := 'BA-201';
    v_passing_score      integer := 65;
    v_time_limit_minutes integer := 105;
    v_cert_exists        boolean;
    v_cert_row           public.certifications;
    v_cert_count         integer;
    v_domain_count       integer;
    v_exact_match_count  integer;
    v_duplicate_domain   text;
    v_code_conflict_name text;
BEGIN
    SELECT count(*) INTO v_cert_count
    FROM public.certifications
    WHERE exam_name = v_exam_name;

    IF v_cert_count > 1 THEN
        RAISE EXCEPTION
            'BA-CAT-01: found % duplicate certifications rows for %. Conflicting pre-existing catalog state -- refusing to proceed. Review manually before re-running this migration.',
            v_cert_count, v_exam_name;
    END IF;

    -- Guard against the canonical certification_code 'BA-201' having been
    -- claimed by a different exam_name (certification_code has no UNIQUE
    -- constraint in the schema, so this is not otherwise prevented).
    SELECT exam_name INTO v_code_conflict_name
    FROM public.certifications
    WHERE certification_code = v_cert_code
      AND exam_name <> v_exam_name
    LIMIT 1;

    IF v_code_conflict_name IS NOT NULL THEN
        RAISE EXCEPTION
            'BA-CAT-01: certification_code=% is already assigned to a different certification (exam_name=%). Conflicting pre-existing catalog state -- refusing to proceed. Review manually before re-running this migration.',
            v_cert_code, v_code_conflict_name;
    END IF;

    v_cert_exists := (v_cert_count = 1);

    SELECT count(*) INTO v_domain_count
    FROM public.certification_domains
    WHERE exam_name = v_exam_name;

    IF NOT v_cert_exists THEN
        -- Case 1 (expected) unless certification_domains rows already
        -- orphan-reference this exam_name, which would be an ambiguous
        -- pre-existing state even if the (assumed) FK should prevent it.
        IF v_domain_count > 0 THEN
            RAISE EXCEPTION
                'BA-CAT-01: no certifications row for exam_name=%, but % certification_domains row(s) already reference it. Ambiguous pre-existing catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_domain_count;
        END IF;

        INSERT INTO public.certifications (
            exam_name, display_name, certification_code,
            passing_score, time_limit_minutes, question_count, is_active
        ) VALUES (
            v_exam_name, v_exam_name, v_cert_code,
            v_passing_score, v_time_limit_minutes, 0, false
        );

        INSERT INTO public.certification_domains (
            exam_name, domain_name, weight, question_count, display_order, is_active
        ) VALUES
            (v_exam_name, 'Customer Discovery',               17, 0, 1, true),
            (v_exam_name, 'Collaboration with Stakeholders',  17, 0, 2, true),
            (v_exam_name, 'Business Process Mapping',         17, 0, 3, true),
            (v_exam_name, 'Requirements',                     17, 0, 4, true),
            (v_exam_name, 'User Stories',                     16, 0, 5, true),
            (v_exam_name, 'User Acceptance',                  16, 0, 6, true);

        RAISE NOTICE 'BA-CAT-01: inserted certifications row and 6 certification_domains rows for %.', v_exam_name;
    ELSE
        -- A certifications row for this exact exam_name already exists.
        -- Verify it -- and its domains -- are EXACTLY the expected shape
        -- before treating this as a safe no-op (Case 2). Anything less than
        -- an exact match is Case 3 and must fail clearly.
        SELECT * INTO v_cert_row
        FROM public.certifications
        WHERE exam_name = v_exam_name;

        IF v_cert_row.certification_code IS DISTINCT FROM v_cert_code THEN
            RAISE EXCEPTION
                'BA-CAT-01: existing certifications row for % has certification_code=% (expected %). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.certification_code, v_cert_code;
        END IF;

        IF v_cert_row.passing_score IS DISTINCT FROM v_passing_score
           OR v_cert_row.time_limit_minutes IS DISTINCT FROM v_time_limit_minutes THEN
            RAISE EXCEPTION
                'BA-CAT-01: existing certifications row for % has passing_score=% time_limit_minutes=% (expected passing_score=% time_limit_minutes=%). Conflicting exam metadata -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.passing_score, v_cert_row.time_limit_minutes,
                v_passing_score, v_time_limit_minutes;
        END IF;

        IF v_cert_row.is_active IS DISTINCT FROM false THEN
            RAISE EXCEPTION
                'BA-CAT-01: existing certifications row for % has is_active=% (expected false). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.is_active;
        END IF;

        IF v_cert_row.question_count IS DISTINCT FROM 0 THEN
            RAISE EXCEPTION
                'BA-CAT-01: existing certifications row for % has question_count=% (expected 0). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.question_count;
        END IF;

        IF v_domain_count <> 6 THEN
            RAISE EXCEPTION
                'BA-CAT-01: expected exactly 6 certification_domains rows for %, found %. Partial or conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_domain_count;
        END IF;

        SELECT domain_name INTO v_duplicate_domain
        FROM public.certification_domains
        WHERE exam_name = v_exam_name
        GROUP BY domain_name
        HAVING count(*) > 1
        LIMIT 1;

        IF v_duplicate_domain IS NOT NULL THEN
            RAISE EXCEPTION
                'BA-CAT-01: duplicate certification_domains rows detected for % (domain_name=%). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_duplicate_domain;
        END IF;

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

        IF v_exact_match_count <> 6 THEN
            RAISE EXCEPTION
                'BA-CAT-01: certification_domains rows for % do not exactly match the 6 expected domain names/weights/display_orders (%/6 matched). Partial or conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_exact_match_count;
        END IF;

        RAISE NOTICE 'BA-CAT-01: certification % and its 6 domains already exist in the expected shape; no changes made.', v_exam_name;
    END IF;
END;
$$;
