-- =============================================================================
-- V45 Phase 4E — Publication gate verification
-- =============================================================================
-- Run as service_role inside BEGIN … ROLLBACK.
-- Proves audit finding status/materiality gate publication eligibility.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    v_question_id integer;
    v_version_a uuid := gen_random_uuid();
    v_version_b uuid := gen_random_uuid();
    v_run_id uuid := gen_random_uuid();
    v_finding_id uuid;
    v_publishable boolean;
    v_count integer;
    v_publish_error text;
BEGIN
    RAISE NOTICE '== V45 Phase 4E publication gate verification ==';

    INSERT INTO public.questions (
        exam_name, category, difficulty, question_text, question_type,
        select_count, explanation, is_active, is_exam_eligible, language_code
    ) VALUES (
        'ADM-201', 'Automation', 'medium', 'Gate test stem', 'single',
        1, '', true, true, 'en'
    ) RETURNING id INTO v_question_id;

    INSERT INTO public.question_versions (
        id, question_id, version_number, question_text, explanation,
        category, difficulty, question_type, select_count, language_code,
        content_hash, source_type, created_by
    ) VALUES
    (v_version_a, v_question_id, 1, 'Gate test stem A', '', 'Automation', 'medium', 'single', 1, 'en', 'hash-a', 'manual', 'verification'),
    (v_version_b, v_question_id, 2, 'Gate test stem B', '', 'Automation', 'medium', 'single', 1, 'en', 'hash-b', 'manual', 'verification');

    INSERT INTO public.question_option_versions (
        question_version_id, option_label, option_text, is_correct, display_order
    )
    SELECT v_version_a, 'A', 'Correct', true, 1
    UNION ALL SELECT v_version_a, 'B', 'Wrong', false, 2
    UNION ALL SELECT v_version_b, 'A', 'Correct', true, 1
    UNION ALL SELECT v_version_b, 'B', 'Wrong', false, 2;

    PERFORM public.approve_question_version_v1(v_version_a, 'verification@certbound.test', 'approve A');
    PERFORM public.approve_question_version_v1(v_version_b, 'verification@certbound.test', 'approve B');

    INSERT INTO public.audit_runs (
        id, audit_type, target_question_version_id, run_status,
        created_by, started_at, completed_at
    ) VALUES (
        v_run_id, 'deterministic', v_version_a, 'completed',
        'verification', now(), now()
    );

    INSERT INTO public.audit_findings (
        audit_run_id, finding_code, finding_type, severity, materiality,
        finding_status, title, description, metadata
    ) VALUES (
        v_run_id, 'EXPLANATION_MISSING', 'explanation_quality', 'medium', 'blocking',
        'open', 'Missing explanation', 'Explanation empty', '{}'::jsonb
    ) RETURNING id INTO v_finding_id;

    v_count := public.count_blocking_findings_for_question_version_v1(v_version_a);
    ASSERT v_count = 1, 'T1: open blocking finding must block';

    BEGIN
        PERFORM public.publish_question_version_v1(
            v_version_a, 'verification@certbound.test', 'should fail'
        );
        RAISE EXCEPTION 'T2 failed: publish should have been blocked for open finding';
    EXCEPTION WHEN invalid_parameter_value THEN
        v_publish_error := SQLERRM;
        ASSERT v_publish_error ILIKE '%publication blocked%',
            'T2: publish error must explain publication blocked';
        RAISE NOTICE 'T2: publish rejected while open blocking finding present (%)', v_publish_error;
    END;

    PERFORM public.record_audit_finding_decision_v1(
        v_finding_id, 'accepted', 'verification@certbound.test', 'Confirmed defect'
    );
    v_publishable := public.is_question_version_publishable_v1(v_version_a);
    ASSERT NOT v_publishable, 'T3: accepted blocking finding must still block';

    PERFORM public.record_audit_finding_decision_v1(
        v_finding_id, 'rejected', 'verification@certbound.test', 'False positive'
    );
    v_publishable := public.is_question_version_publishable_v1(v_version_a);
    ASSERT v_publishable, 'T4: rejected finding must release gate';

    INSERT INTO public.audit_findings (
        audit_run_id, finding_code, finding_type, severity, materiality,
        finding_status, title, description, metadata
    ) VALUES (
        v_run_id, 'WEAK_DISTRACTORS', 'answer_quality', 'high', 'warning',
        'open', 'Weak distractors', 'Warning only', '{}'::jsonb
    );
    v_publishable := public.is_question_version_publishable_v1(v_version_a);
    ASSERT v_publishable, 'T5: warning finding must not block';

    v_count := public.count_blocking_findings_for_question_version_v1(v_version_b);
    ASSERT v_count = 0, 'T6: findings on version A must not block version B';

    PERFORM public.publish_question_version_v1(
        v_version_a, 'verification@certbound.test', 'publish after reject'
    );
    ASSERT (
        SELECT COUNT(*) FROM public.question_version_events
        WHERE question_version_id = v_version_a AND event_type = 'published'
    ) = 1, 'T7: successful publish writes one published event';

    PERFORM public.publish_question_version_v1(
        v_version_a, 'verification@certbound.test', 'idempotent publish'
    );
    ASSERT (
        SELECT COUNT(*) FROM public.question_version_events
        WHERE question_version_id = v_version_a AND event_type = 'published'
    ) = 1, 'T8: idempotent publish does not duplicate event';

    RAISE NOTICE 'All publication gate assertions passed.';
END $$;

ROLLBACK;
