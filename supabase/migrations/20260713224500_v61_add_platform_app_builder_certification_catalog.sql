-- =============================================================================
-- PAB-EXP-03 / PAB-EXP-03A: Add Salesforce Certified Platform App Builder
-- certification catalog rows (public.certifications + public.certification_domains)
-- Created : 2026-07-13 22:45:00 UTC
-- Corrected: 2026-07-13 (PAB-EXP-03A) -- passing_score corrected 68 -> 63;
--            exact-state check extended to cover exam metadata.
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Extends the existing database certification catalog so that Platform App
-- Builder generation candidates -- which already pass the engine-profile
-- capability check in workers/certification_registry.py -- can also pass the
-- persistence-authority gate used by generation/audit:
-- QuestionCandidateRepository.certification_domain_exists() in
-- workers/question_candidate_generation.py.
--
-- Two layers, one migration touches only one of them (see PAB-EXP-02):
--   * Engine certification profile (workers/certification_registry.py):
--     unchanged by this migration. This migration inserts a literal,
--     unmodified transcription of the values already frozen there
--     (PAB_EXAM_NAME and _pab_definition()'s domain names / weights) -- it
--     does not duplicate or re-derive that logic in SQL.
--   * Database catalog (public.certifications / public.certification_domains):
--     the only thing this migration adds to.
--
-- Canonical identity
-- ------------------
--   certifications.exam_name = 'Salesforce Certified Platform App Builder'
--   (matches workers.certification_registry.PAB_EXAM_NAME exactly)
--
-- APP-401 / app-401 are intentionally NOT used anywhere below: PAB-EXP-02
-- found no repository evidence that APP-401 is an official Salesforce exam
-- code or an established internal identifier, and removed it from the
-- engine profile. certifications.certification_code is set to the same
-- semantic identifier used there, 'platform_app_builder'.
--
-- PAB-EXP-03A certification_code re-review: every repository read of
-- certifications.certification_code (app.py, pages/Dashboard.py,
-- pages/My_Progress.py, pages/Practice_By_Category.py,
-- pages/Weak_Areas_Practice.py, pages/Admin_Users.py,
-- pages/Admin_Question_Review.py, workers/quality_benchmark_v48_orchestration.py)
-- fetches it into a dict/row but never renders it to an end user or admin,
-- never puts it in a URL, and never routes on it. The only UI text that
-- *looks* related -- pages/Admin_Audit_Review.py's "Certification code"
-- filter and the "Cert: ..." label it renders -- is a completely different,
-- unrelated value: it is the list_audit_runs_for_review_v1 RPC's
-- certification_code *output column* (supabase/migrations/
-- 20260624230000_v45_audit_finding_review_workflow.sql), which is computed
-- as COALESCE(audit_runs.metadata->>'certification_exam_name',
-- audit_runs.metadata->>'certification_id', question.exam_name) -- it never
-- reads public.certifications.certification_code at all, so it cannot be
-- affected by this migration. Separately,
-- workers/quality_benchmark_v48_orchestration.py already writes SHA-256-hash-
-- derived, non-exam-code-shaped strings (e.g. 'BM1A2B3C4D') into this exact
-- column for benchmark-created certification rows, confirming there is no
-- enforced exam-code format for it anywhere in this codebase today.
-- Conclusion: certification_code is an internal, opaque, free-text
-- UI/metadata label with no enforced format and no end-user exposure --
-- 'platform_app_builder' is safe to retain.
--
-- Activation state
-- -----------------
-- The new certifications row is inserted with is_active = false: no
-- human-reviewed Platform App Builder question has been published yet, and
-- this migration must not surface an empty certification choice to end
-- users in the exam-taking UI (app.py, pages/Dashboard.py,
-- pages/Practice_By_Category.py, pages/Weak_Areas_Practice.py, ... all of
-- which filter public.certifications on is_active = true).
-- QuestionCandidateRepository.certification_domain_exists() -- the actual
-- gate exercised by candidate generation/audit persistence -- only checks
-- certification_domains.is_active, never certifications.is_active, so this
-- does not block candidate persistence; it only withholds end-user exam
-- exposure until a human explicitly activates it in a separate, reviewed
-- change. The five new certification_domains rows are inserted with
-- is_active = true so certification_domain_exists() succeeds for them.
--
-- Exam metadata (PAB-EXP-03A correction)
-- ---------------------------------------
-- passing_score = 63 and time_limit_minutes = 105 per the official current
-- Salesforce Platform App Builder exam guide (passing score: 63%; time
-- allotted: 105 minutes). The original PAB-EXP-03 draft of this migration
-- used passing_score = 68 (app.py's generic PASSING_SCORE_DEFAULT fallback
-- constant, not an official value) -- corrected here.
-- question_count is 0 on both the certification and each domain: zero real
-- Platform App Builder questions exist yet, and this migration does not
-- generate, seed, or promote any question content.
--
-- Conflict safety
-- ---------------
-- Before writing anything, this migration inspects existing state:
--   * Case 1 (no certifications row for this exam_name, and no orphaned
--     certification_domains rows referencing it either) -> inserts the
--     certification and all five domains.
--   * Case 2 (a certifications row for this exact exam_name already exists
--     with the expected certification_code, the expected passing_score and
--     time_limit_minutes, and certification_domains for it is exactly the
--     five expected domain names with the five expected weights, no
--     duplicates) -> leaves every row untouched; safe no-op.
--   * Case 3 (anything else -- a different certification_code, a different
--     passing_score/time_limit_minutes, a different domain count, a domain
--     name outside the expected five, a weight that does not match the
--     engine profile, a duplicate-looking domain row, or
--     certification_domains rows referencing this exam_name with no parent
--     certifications row) -> RAISE EXCEPTION; the migration aborts and
--     writes nothing. A pre-existing canonical certification row with the
--     right exam_name but wrong passing_score/time_limit_minutes (e.g. a
--     stray 68 from a manually-applied earlier draft) is explicitly Case 3,
--     never silently treated as exact.
-- ON CONFLICT DO NOTHING / DO UPDATE is intentionally not used anywhere
-- below, because either would silently accept a partially-seeded or
-- conflicting catalog row instead of surfacing it for review.
--
-- Deliberately excluded from the exact-match check: question_count (expected
-- to grow organically as real questions are authored/promoted -- gating on
-- it would make a healthy, growing catalog row look "conflicting") and
-- is_active (a human may deliberately flip this to true once content is
-- ready; this migration never UPDATEs an existing row, so that decision is
-- preserved either way and must not be reverted by re-running this file).
-- =============================================================================

DO $$
DECLARE
    v_exam_name         text := 'Salesforce Certified Platform App Builder';
    v_cert_code         text := 'platform_app_builder';
    v_passing_score     integer := 63;
    v_time_limit_minutes integer := 105;
    v_cert_exists       boolean;
    v_cert_row          public.certifications;
    v_domain_count      integer;
    v_exact_match_count integer;
    v_duplicate_domain  text;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM public.certifications WHERE exam_name = v_exam_name
    ) INTO v_cert_exists;

    SELECT count(*) INTO v_domain_count
    FROM public.certification_domains
    WHERE exam_name = v_exam_name;

    IF NOT v_cert_exists THEN
        -- Case 1 (expected) unless certification_domains rows already
        -- orphan-reference this exam_name, which would be an ambiguous
        -- pre-existing state even if the (assumed) FK should prevent it.
        IF v_domain_count > 0 THEN
            RAISE EXCEPTION
                'PAB-EXP-03: no certifications row for exam_name=%, but % certification_domains row(s) already reference it. Ambiguous pre-existing catalog state -- refusing to proceed. Review manually before re-running this migration.',
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
            (v_exam_name, 'Salesforce Fundamentals', 23, 0, 1, true),
            (v_exam_name, 'User Interface', 17, 0, 2, true),
            (v_exam_name, 'Data Modeling and Management', 22, 0, 3, true),
            (v_exam_name, 'Business Logic and Process Automation', 28, 0, 4, true),
            (v_exam_name, 'App Deployment', 10, 0, 5, true);

        RAISE NOTICE 'PAB-EXP-03: inserted certifications row and 5 certification_domains rows for %.', v_exam_name;
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
                'PAB-EXP-03: existing certifications row for % has certification_code=% (expected %). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.certification_code, v_cert_code;
        END IF;

        IF v_cert_row.passing_score IS DISTINCT FROM v_passing_score
           OR v_cert_row.time_limit_minutes IS DISTINCT FROM v_time_limit_minutes THEN
            RAISE EXCEPTION
                'PAB-EXP-03: existing certifications row for % has passing_score=% time_limit_minutes=% (expected passing_score=% time_limit_minutes=%). Conflicting exam metadata -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_cert_row.passing_score, v_cert_row.time_limit_minutes,
                v_passing_score, v_time_limit_minutes;
        END IF;

        IF v_domain_count <> 5 THEN
            RAISE EXCEPTION
                'PAB-EXP-03: expected exactly 5 certification_domains rows for %, found %. Partial or conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
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
                'PAB-EXP-03: duplicate certification_domains rows detected for % (domain_name=%). Conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_duplicate_domain;
        END IF;

        SELECT count(*) INTO v_exact_match_count
        FROM public.certification_domains cd
        JOIN (VALUES
            ('Salesforce Fundamentals', 23),
            ('User Interface', 17),
            ('Data Modeling and Management', 22),
            ('Business Logic and Process Automation', 28),
            ('App Deployment', 10)
        ) AS expected(domain_name, weight)
          ON cd.domain_name = expected.domain_name
         AND cd.weight = expected.weight
        WHERE cd.exam_name = v_exam_name;

        IF v_exact_match_count <> 5 THEN
            RAISE EXCEPTION
                'PAB-EXP-03: certification_domains rows for % do not exactly match the 5 expected domain names/weights (%/5 matched). Partial or conflicting catalog state -- refusing to proceed. Review manually before re-running this migration.',
                v_exam_name, v_exact_match_count;
        END IF;

        RAISE NOTICE 'PAB-EXP-03: certification % and its 5 domains already exist in the expected shape; no changes made.', v_exam_name;
    END IF;
END;
$$;
