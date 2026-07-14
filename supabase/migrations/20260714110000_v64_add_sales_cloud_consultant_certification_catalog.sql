-- =============================================================================
-- SCC-EXP-03B: Add Salesforce Certified Sales Cloud Consultant certification
-- catalog rows (public.certifications + public.certification_domains)
-- Created : 2026-07-14 11:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Extends the database certification catalog with the inactive Salesforce
-- Certified Sales Cloud Consultant (SCC) row and its five official domain
-- rows, mirroring the established pattern from
-- 20260713224500_v61_add_platform_app_builder_certification_catalog.sql.
-- This migration is the database-catalog counterpart to the already-frozen
-- engine profile in workers/certification_registry.py (SCC_EXAM_NAME and
-- _scc_definition()) -- it inserts a literal, unmodified transcription of
-- those values; it does not duplicate or re-derive that logic in SQL.
--
-- Prerequisite (already satisfied)
-- ---------------------------------
-- public.certification_domains.weight was widened from integer to
-- numeric(5,1) by 20260714100000_v63_widen_certification_domain_weight_to_
-- numeric.sql, verified successfully in production per SCC-EXP-03A. Without
-- that widening, the decimal weights below (23.3, 18.3, 13.3) would be
-- silently truncated to integers. This migration does not touch the column
-- type again; it only inserts rows into the already-widened column.
--
-- Canonical identity
-- ------------------
--   certifications.exam_name         = 'Salesforce Certified Sales Cloud Consultant'
--   certifications.certification_code = 'Sales-Con-201'
--   (matches workers.certification_registry.SCC_EXAM_NAME and
--   CERTIFICATION_CODES[SCC_EXAM_NAME] exactly)
--
-- Exam metadata
-- -------------
-- passing_score = 73, time_limit_minutes = 105, per the current official
-- Salesforce Sales Cloud Consultant exam guide. question_count = 0 on both
-- the certification and each domain: zero real Sales Cloud Consultant
-- questions exist yet, and this migration does not generate, seed, or
-- promote any question content.
--
-- Decimal domain weights
-- -----------------------
-- The five domains below use the exact one-decimal weights already frozen
-- in workers/certification_registry.py's _scc_definition(): 23.3, 20.0,
-- 25.0, 18.3, 13.3. These sum to the officially published total of 99.9 --
-- this is Salesforce's own one-decimal rounding of the published exam
-- guide percentages, not a data-entry error. This migration does not
-- normalize, round, or force the total to 100; it inserts the numeric
-- literals exactly as given, preserving scale via the already-widened
-- numeric(5,1) column.
--
-- Activation state
-- -----------------
-- The new certifications row is inserted with is_active = false: no
-- human-reviewed Sales Cloud Consultant question has been published yet,
-- and this migration must not surface an empty certification choice to end
-- users in the exam-taking UI (app.py, pages/Dashboard.py,
-- pages/Practice_By_Category.py, pages/Weak_Areas_Practice.py, ... all of
-- which filter public.certifications on is_active = true).
-- QuestionCandidateRepository.certification_domain_exists() -- the actual
-- gate exercised by candidate generation/audit persistence -- only checks
-- certification_domains.is_active, never certifications.is_active, so this
-- does not block candidate persistence; it only withholds end-user exam
-- exposure until a human explicitly activates it in a separate, reviewed
-- change. The five new certification_domains rows are inserted with
-- is_active = true so certification_domain_exists() succeeds for them --
-- identical to the PAB-EXP-03 precedent.
--
-- Conflict safety
-- ---------------
-- Before writing anything, this migration inspects existing state:
--   * Case 1 (no certifications row for this exam_name, and no orphaned
--     certification_domains rows referencing it either) -> inserts the
--     certification and all five domains.
--   * Case 2 (a certifications row for this exact exam_name already exists
--     with the expected certification_code, the expected passing_score and
--     time_limit_minutes, the expected is_active=false and question_count=0,
--     and certification_domains for it is exactly the five expected domain
--     names/weights/display_orders, no duplicates) -> leaves every row
--     untouched; safe no-op.
--   * Case 3 (anything else -- a different certification_code, a different
--     exam name casing/spelling, a different passing_score/time_limit_
--     minutes, is_active not false, question_count not 0, a different
--     domain count, a domain name outside the expected five, a renamed
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
-- exam_name = 'Salesforce Certified Sales Cloud Consultant'. It never
-- updates, deletes, or even reads rows for Administrator, Business
-- Analyst, or Platform App Builder (their unrelated-certification checks
-- below are read-only existence/count assertions used purely to decide
-- Case 1 vs Case 3; they never write to those rows). It never touches
-- public.questions, public.answer_options, or any other table.
--
-- Deliberately excluded from the exact-match check: none. Unlike PAB-EXP-03
-- (which excluded question_count and is_active because a human might
-- reasonably flip is_active or grow question_count organically over time
-- for a certification that already has real content), this task's Case 2
-- definition explicitly requires question_count=0 and is_active=false to
-- match exactly, since SCC-EXP-03B intentionally inserts SCC in that exact
-- initial state and no later step has changed it yet at the time this
-- migration is authored.
-- =============================================================================

DO $$
DECLARE
    v_exam_name          text := 'Salesforce Certified Sales Cloud Consultant';
    v_cert_code          text := 'Sales-Con-201';
    v_passing_score      integer := 73;
    v_time_limit_minutes integer := 105;
    v_cert_exists        boolean;
    v_cert_row           public.certifications;
    v_cert_count         integer;
    v_domain_count       integer;
    v_exact_match_count  integer;
    v_duplicate_domain   text;
BEGIN
    SELECT count(*) INTO v_cert_count
    FROM public.certifications
    WHERE exam_name = v_exam_name;

    IF v_cert_count > 1 THEN
        RAISE EXCEPTION
            'SCC-EXP-03B: found % duplicate certifications rows for %. Conflicting pre-existing catalog state -- refusing to proceed. Review manually before re-running this migration.',
            v_cert_count, v_exam_name;
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
                'SCC-EXP-03B: no certifications row for exam_name=%, but % certification_domains row(s) already reference it. Ambiguous pre-existing catalog state -- refusing to proceed. Review manually before re-running this migration.',
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
            (v_exam_name, 'Practical Application of Sales Cloud Expertise', 23.3, 0, 1, true),
            (v_exam_name, 'Sales Lifecycle',                                20.0, 0, 2, true),
            (v_exam_name, 'Consulting & Implementation Strategies',        25.0, 0, 3, true),
            (v_exam_name, 'Data Management',                               18.3, 0, 4, true),
            (v_exam_name, 'Predictive and Generative AI',                  13.3, 0, 5, true);

        RAISE NOTICE 'SCC-EXP-03B: inserted certifications row and 5 certification_domains rows for %.', v_exam_name;
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
                'SCC-EXP-03B: existing certifications row for % has certification_code=% (expected %). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.certification_code, v_cert_code;
        END IF;

        IF v_cert_row.passing_score IS DISTINCT FROM v_passing_score
           OR v_cert_row.time_limit_minutes IS DISTINCT FROM v_time_limit_minutes THEN
            RAISE EXCEPTION
                'SCC-EXP-03B: existing certifications row for % has passing_score=% time_limit_minutes=% (expected passing_score=% time_limit_minutes=%). Conflicting exam metadata -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.passing_score, v_cert_row.time_limit_minutes,
                v_passing_score, v_time_limit_minutes;
        END IF;

        IF v_cert_row.is_active IS DISTINCT FROM false THEN
            RAISE EXCEPTION
                'SCC-EXP-03B: existing certifications row for % has is_active=% (expected false). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.is_active;
        END IF;

        IF v_cert_row.question_count IS DISTINCT FROM 0 THEN
            RAISE EXCEPTION
                'SCC-EXP-03B: existing certifications row for % has question_count=% (expected 0). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.question_count;
        END IF;

        IF v_domain_count <> 5 THEN
            RAISE EXCEPTION
                'SCC-EXP-03B: expected exactly 5 certification_domains rows for %, found %. Partial or conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
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
                'SCC-EXP-03B: duplicate certification_domains rows detected for % (domain_name=%). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_duplicate_domain;
        END IF;

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

        IF v_exact_match_count <> 5 THEN
            RAISE EXCEPTION
                'SCC-EXP-03B: certification_domains rows for % do not exactly match the 5 expected domain names/weights/display_orders (%/5 matched). Partial or conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_exact_match_count;
        END IF;

        RAISE NOTICE 'SCC-EXP-03B: certification % and its 5 domains already exist in the expected shape; no changes made.', v_exam_name;
    END IF;
END;
$$;
