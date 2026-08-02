-- =============================================================================
-- V00 — CertBound Base Schema Bootstrap Verification
-- =============================================================================
--
-- Purpose
-- -------
-- Confirms that 20260101000000_v00_certbound_base_schema.sql produced the
-- exact set of base tables, columns, constraints, indexes, RLS state,
-- policies, and grants required before the earliest existing migration
-- (20260623000000_v44_question_version_foundation.sql) can apply, and that
-- no rows or anonymous/public write access were introduced.
--
-- Design notes
-- ------------
-- * Read-only: this script never inserts, updates, or deletes application
--   rows. All checks use catalog/information_schema introspection.
-- * No pgTAP dependency. Assertions use PL/pgSQL ASSERT (PostgreSQL >= 9.6).
-- * Intended to run immediately after the V00 migration and before V44+.
-- =============================================================================

DO $$
DECLARE
    v_count       integer;
    v_base_tables text[] := ARRAY[
        'languages', 'certifications', 'certification_domains', 'app_users',
        'user_certification_access', 'questions', 'answer_options',
        'exam_attempts', 'question_attempts', 'readiness_snapshots',
        'support_tickets'
    ];
    v_table       text;
BEGIN
    RAISE NOTICE '== V00 base schema bootstrap verification ==';

    -- =========================================================================
    -- T1: all 11 base tables exist in public
    -- =========================================================================
    FOREACH v_table IN ARRAY v_base_tables LOOP
        SELECT count(*) INTO v_count
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = v_table;

        ASSERT v_count = 1, format('T1: public.%s must exist exactly once', v_table);
    END LOOP;
    RAISE NOTICE 'T1 OK: all % base tables exist', array_length(v_base_tables, 1);

    -- =========================================================================
    -- T2: RLS is enabled on every base table
    -- =========================================================================
    FOREACH v_table IN ARRAY v_base_tables LOOP
        SELECT count(*) INTO v_count
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = v_table AND c.relrowsecurity = true;

        ASSERT v_count = 1, format('T2: RLS must be enabled on public.%s', v_table);
    END LOOP;
    RAISE NOTICE 'T2 OK: RLS enabled on all base tables';

    -- =========================================================================
    -- T3: pre-V44 prerequisite — public.questions.id is integer (V44 depends
    --     on this exact type per its own header comment)
    -- =========================================================================
    PERFORM 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'questions'
      AND column_name = 'id' AND data_type = 'integer';
    ASSERT FOUND, 'T3: public.questions.id must be integer for V44 compatibility';
    RAISE NOTICE 'T3 OK: public.questions.id is integer';

    -- =========================================================================
    -- T4: pre-V63 prerequisite — certification_domains.weight is integer
    --     (V63 asserts this exact starting type and fails loudly otherwise)
    -- =========================================================================
    PERFORM 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'certification_domains'
      AND column_name = 'weight' AND data_type = 'integer';
    ASSERT FOUND, 'T4: public.certification_domains.weight must be integer pre-V63';
    RAISE NOTICE 'T4 OK: certification_domains.weight is integer';

    -- =========================================================================
    -- T5: pre-V45 prerequisite — chk_exam_attempts_mode exists WITHOUT
    --     'Daily Sprint' (V45 drops+recreates it with that value added)
    -- =========================================================================
    PERFORM 1 FROM pg_constraint con
    JOIN pg_class t ON t.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = 'public' AND t.relname = 'exam_attempts'
      AND con.conname = 'chk_exam_attempts_mode'
      AND pg_get_constraintdef(con.oid) NOT LIKE '%Daily Sprint%';
    ASSERT FOUND, 'T5: chk_exam_attempts_mode must exist without Daily Sprint pre-V45';
    RAISE NOTICE 'T5 OK: chk_exam_attempts_mode is in its pre-V45 shape';

    -- =========================================================================
    -- T6: app_users must NOT yet have any V46 Stripe/billing columns
    -- =========================================================================
    SELECT count(*) INTO v_count
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'app_users'
      AND column_name IN (
          'stripe_customer_id', 'stripe_subscription_id', 'stripe_subscription_status',
          'stripe_price_id', 'stripe_current_period_end', 'stripe_cancel_at_period_end',
          'stripe_last_event_created_at', 'billing_updated_at', 'billing_admin_override_at',
          'stripe_last_subscription_event_created_at'
      );
    ASSERT v_count = 0, 'T6: app_users must not contain any V46 Stripe/billing column before V46 runs';
    RAISE NOTICE 'T6 OK: app_users has no V46 columns yet';

    -- =========================================================================
    -- T7: required primary keys exist
    -- =========================================================================
    FOREACH v_table IN ARRAY v_base_tables LOOP
        SELECT count(*) INTO v_count
        FROM pg_constraint con
        JOIN pg_class t ON t.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public' AND t.relname = v_table AND con.contype = 'p';

        ASSERT v_count = 1, format('T7: public.%s must have exactly one primary key', v_table);
    END LOOP;
    RAISE NOTICE 'T7 OK: all base tables have a primary key';

    -- =========================================================================
    -- T8: required foreign keys exist (spot-check the FK graph)
    -- =========================================================================
    PERFORM 1 FROM pg_constraint WHERE conname = 'fk_certification_domains_exam_name';
    ASSERT FOUND, 'T8: fk_certification_domains_exam_name missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'fk_questions_exam_name';
    ASSERT FOUND, 'T8: fk_questions_exam_name missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'answer_options_question_id_fkey';
    ASSERT FOUND, 'T8: answer_options_question_id_fkey missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'question_attempts_question_id_fkey';
    ASSERT FOUND, 'T8: question_attempts_question_id_fkey missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'readiness_snapshots_exam_attempt_id_fkey';
    ASSERT FOUND, 'T8: readiness_snapshots_exam_attempt_id_fkey missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'fk_user_certification_access_exam_name';
    ASSERT FOUND, 'T8: fk_user_certification_access_exam_name missing';
    RAISE NOTICE 'T8 OK: spot-checked foreign keys exist';

    -- =========================================================================
    -- T9: required unique/check constraints exist
    -- =========================================================================
    PERFORM 1 FROM pg_constraint WHERE conname = 'app_users_email_key';
    ASSERT FOUND, 'T9: app_users_email_key missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'chk_app_users_subscription_status';
    ASSERT FOUND, 'T9: chk_app_users_subscription_status missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'questions_external_key_key';
    ASSERT FOUND, 'T9: questions_external_key_key missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'uq_question_attempts_exam_attempt_question';
    ASSERT FOUND, 'T9: uq_question_attempts_exam_attempt_question missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'uq_readiness_snapshots_attempt_formula';
    ASSERT FOUND, 'T9: uq_readiness_snapshots_attempt_formula missing';

    PERFORM 1 FROM pg_constraint WHERE conname = 'user_certification_access_user_email_exam_name_key';
    ASSERT FOUND, 'T9: user_certification_access_user_email_exam_name_key missing';
    RAISE NOTICE 'T9 OK: spot-checked unique/check constraints exist';

    -- =========================================================================
    -- T10: expected indexes exist
    -- =========================================================================
    PERFORM 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_questions_mock_pool';
    ASSERT FOUND, 'T10: idx_questions_mock_pool missing';

    PERFORM 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_exam_attempts_user_email_completed_at';
    ASSERT FOUND, 'T10: idx_exam_attempts_user_email_completed_at missing';

    PERFORM 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'app_users_email_idx';
    ASSERT FOUND, 'T10: app_users_email_idx missing';
    RAISE NOTICE 'T10 OK: spot-checked indexes exist';

    -- =========================================================================
    -- T11: expected RLS policies exist (count per table)
    -- =========================================================================
    SELECT count(*) INTO v_count FROM pg_policies WHERE schemaname = 'public' AND tablename = 'app_users';
    ASSERT v_count = 3, 'T11: app_users must have exactly 3 policies';

    SELECT count(*) INTO v_count FROM pg_policies WHERE schemaname = 'public' AND tablename = 'exam_attempts';
    ASSERT v_count = 2, 'T11: exam_attempts must have exactly 2 policies';

    SELECT count(*) INTO v_count FROM pg_policies WHERE schemaname = 'public' AND tablename = 'question_attempts';
    ASSERT v_count = 2, 'T11: question_attempts must have exactly 2 policies';

    SELECT count(*) INTO v_count FROM pg_policies WHERE schemaname = 'public' AND tablename = 'support_tickets';
    ASSERT v_count = 2, 'T11: support_tickets must have exactly 2 policies';

    SELECT count(*) INTO v_count
    FROM pg_policies
    WHERE schemaname = 'public' AND tablename IN (
        'languages', 'certifications', 'certification_domains', 'user_certification_access',
        'questions', 'answer_options', 'readiness_snapshots'
    );
    ASSERT v_count = 7, 'T11: single-SELECT-policy tables must total exactly 7 policies';
    RAISE NOTICE 'T11 OK: policy counts match verified production state';

    -- =========================================================================
    -- T12: no anonymous/public write access beyond verified production grants
    --      (anon must never have UPDATE on app_users; PUBLIC must never have
    --      direct table privileges on any base table)
    -- =========================================================================
    SELECT count(*) INTO v_count
    FROM information_schema.role_table_grants
    WHERE table_schema = 'public' AND table_name = 'app_users'
      AND grantee = 'anon' AND privilege_type = 'UPDATE';
    ASSERT v_count = 0, 'T12: anon must not hold UPDATE on app_users';

    SELECT count(*) INTO v_count
    FROM information_schema.role_table_grants
    WHERE table_schema = 'public' AND table_name = ANY (v_base_tables)
      AND grantee = 'PUBLIC';
    ASSERT v_count = 0, 'T12: PUBLIC must not hold any direct grant on base tables';
    RAISE NOTICE 'T12 OK: no unintended anonymous/public write access';

    -- =========================================================================
    -- T13: no application data was seeded by this migration
    -- =========================================================================
    FOREACH v_table IN ARRAY v_base_tables LOOP
        EXECUTE format('SELECT count(*) FROM public.%I', v_table) INTO v_count;
        ASSERT v_count = 0, format('T13: public.%s must contain zero rows after bootstrap', v_table);
    END LOOP;
    RAISE NOTICE 'T13 OK: zero rows in every base table';

    RAISE NOTICE '== V00 base schema bootstrap verification: ALL CHECKS PASSED ==';
END;
$$;
