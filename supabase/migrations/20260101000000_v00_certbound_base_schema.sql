-- =============================================================================
-- V00 — CertBound Base Schema (reconstructed foundation)
-- Created : 2026-08-02 (CERTBOUND-DB-BASELINE-01)
-- Author  : CertBound automated migration (senior DB architect reconstruction)
--
-- Purpose
-- -------
-- The repository's migration history begins at V44
-- (20260623000000_v44_question_version_foundation.sql), which assumes that
-- the original CertBound base schema (public.questions, public.app_users,
-- etc.) already exists. That original base schema was never captured in
-- Git. This migration reconstructs it so that a completely empty Postgres
-- database can bootstrap the entire CertBound schema from Git alone.
--
-- Provenance
-- ----------
-- This migration was derived from a schema-only, read-only inspection of
-- the authoritative CertBound production database (project reference
-- gagrwlcwcfxmrmoseywb), performed over the Supabase Session pooler with
-- an explicit `SET TRANSACTION READ ONLY;` in effect for the entire
-- inspection transaction. No row data, users, or secrets were read, copied,
-- or embedded here. Every table below is a production table that existed
-- prior to V44 (confirmed because no migration under supabase/migrations/
-- creates it) and is NOT one of the 24 tables that later migrations (V44+)
-- do create.
--
-- Scope discipline
-- -----------------
-- Three production tables are altered by later migrations. This baseline
-- intentionally creates each of them in its PRE-ALTER shape so those later
-- migrations remain the sole owners of the change they introduce:
--   * public.app_users             — created WITHOUT the nine Stripe/billing
--                                     columns added by V46
--                                     (20260625000000_v46_stripe_billing_foundation.sql
--                                     and 20260628180000_v46_stripe_subscription_event_ordering.sql).
--   * public.certification_domains — `weight` created as `integer` (V63,
--                                     20260714100000_v63_widen_certification_domain_weight_to_numeric.sql,
--                                     widens it to numeric(5,1) later).
--   * public.exam_attempts         — `chk_exam_attempts_mode` created with
--                                     its original five allowed values only;
--                                     V45 (20260624190000_v45_allow_daily_sprint_exam_attempt_mode.sql)
--                                     drops and recreates it with the sixth
--                                     ('Daily Sprint') added.
-- All other tables, columns, constraints, indexes, RLS, policies, and
-- grants below are reproduced exactly as verified in production (including
-- exact RLS policy `USING`/`WITH CHECK` expressions), because no existing
-- migration touches them.
--
-- Table creation order intentionally places public.user_certification_access
-- before public.questions and public.answer_options: their verified
-- production SELECT policies contain an EXISTS subquery against
-- user_certification_access, and CREATE POLICY validates referenced
-- relations at creation time.
--
-- No production functions or triggers are reconstructed here: read-only
-- inspection confirmed that every function currently in production is
-- already created by an existing migration (V44+), and no trigger exists on
-- any of the tables created below.
--
-- Excludes (by design)
-- ---------------------
--   * Supabase-managed schemas (auth, storage, realtime, extensions,
--     graphql, vault, net, supabase_functions, etc.) are never created here.
--   * No production row data, users, questions, attempts, billing, or
--     Stripe records are included.
--   * No credentials, connection strings, or project-specific URLs.
--   * No IF NOT EXISTS is used to hide schema conflicts: if any target
--     object already exists, this migration fails loudly instead of
--     silently reconciling divergent state.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. Preflight — fail loudly on a partially initialized / conflicting
--    database instead of silently proceeding.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_conflict text;
BEGIN
    SELECT table_name INTO v_conflict
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN (
          'languages', 'certifications', 'certification_domains', 'app_users',
          'user_certification_access', 'questions', 'answer_options',
          'exam_attempts', 'question_attempts', 'readiness_snapshots',
          'support_tickets'
      )
    LIMIT 1;

    IF v_conflict IS NOT NULL THEN
        RAISE EXCEPTION
            'V00: table public.% already exists. Refusing to run the base-schema migration against a non-empty/partially initialized database.',
            v_conflict;
    END IF;
END;
$$;

-- Required for gen_random_uuid(); Supabase provisions the `extensions`
-- schema itself, this only ensures the extension is enabled within it.
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

-- ---------------------------------------------------------------------------
-- 1. languages
-- ---------------------------------------------------------------------------
CREATE TABLE public.languages (
    language_code text        PRIMARY KEY,
    language_name text        NOT NULL,
    native_name   text,
    is_active     boolean     DEFAULT true,
    display_order integer     DEFAULT 100,
    created_at    timestamptz DEFAULT now(),
    updated_at    timestamptz DEFAULT now()
);

ALTER TABLE public.languages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read active languages" ON public.languages
    FOR SELECT TO authenticated
    USING (is_active = true);

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.languages TO anon, authenticated, postgres, service_role;

-- ---------------------------------------------------------------------------
-- 2. certifications
-- ---------------------------------------------------------------------------
CREATE TABLE public.certifications (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    exam_name           text        NOT NULL,
    display_name        text        NOT NULL,
    certification_code  text,
    passing_score       integer     DEFAULT 65,
    time_limit_minutes  integer     DEFAULT 105,
    question_count      integer     DEFAULT 60,
    is_active           boolean     DEFAULT true,
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now(),
    CONSTRAINT certifications_exam_name_key UNIQUE (exam_name)
);

ALTER TABLE public.certifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read active certifications" ON public.certifications
    FOR SELECT TO authenticated
    USING (is_active = true);

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.certifications TO anon, authenticated, postgres, service_role;

-- ---------------------------------------------------------------------------
-- 3. certification_domains
--    NOTE: `weight` is created as integer, its verified pre-V63 production
--    type. V63 widens it to numeric(5,1); do not pre-widen it here.
-- ---------------------------------------------------------------------------
CREATE TABLE public.certification_domains (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    exam_name      text        NOT NULL,
    domain_name    text        NOT NULL,
    weight         integer     NOT NULL,
    question_count integer     NOT NULL,
    display_order  integer     NOT NULL,
    is_active      boolean     DEFAULT true,
    created_at     timestamptz DEFAULT now(),
    updated_at     timestamptz DEFAULT now(),
    CONSTRAINT certification_domains_exam_name_domain_name_key UNIQUE (exam_name, domain_name),
    CONSTRAINT fk_certification_domains_exam_name FOREIGN KEY (exam_name)
        REFERENCES public.certifications(exam_name) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_certification_domains_exam_name
    ON public.certification_domains (exam_name);

ALTER TABLE public.certification_domains ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read active certification domains" ON public.certification_domains
    FOR SELECT TO authenticated
    USING (is_active = true);

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.certification_domains TO anon, authenticated, postgres, service_role;

-- ---------------------------------------------------------------------------
-- 4. app_users
--    NOTE: created WITHOUT the nine Stripe/billing columns added by V46
--    (stripe_customer_id, stripe_subscription_id, stripe_subscription_status,
--    stripe_price_id, stripe_current_period_end, stripe_cancel_at_period_end,
--    stripe_last_event_created_at, billing_updated_at,
--    billing_admin_override_at, stripe_last_subscription_event_created_at).
--    NOTE: UPDATE is verified absent from anon/authenticated table grants in
--    production; this is reproduced exactly rather than widened.
-- ---------------------------------------------------------------------------
CREATE TABLE public.app_users (
    id                       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    email                    text        NOT NULL,
    full_name                text,
    subscription_status      text        DEFAULT 'free',
    created_at               timestamptz DEFAULT now(),
    updated_at               timestamptz DEFAULT now(),
    auth_user_id             uuid,
    preferred_language_code  text        DEFAULT 'en',
    preferred_timezone       text        NOT NULL DEFAULT 'UTC',
    CONSTRAINT app_users_email_key UNIQUE (email),
    CONSTRAINT chk_app_users_subscription_status CHECK (
        subscription_status IS NULL
        OR subscription_status = ANY (ARRAY[
            'free', 'active', 'paid', 'premium', 'subscribed',
            'trialing', 'expired', 'cancelled', 'canceled', 'past_due', 'unpaid'
        ])
    )
);

CREATE UNIQUE INDEX app_users_auth_user_id_idx ON public.app_users (auth_user_id);
CREATE UNIQUE INDEX app_users_email_idx ON public.app_users (lower(email));

ALTER TABLE public.app_users ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read own app user" ON public.app_users
    FOR SELECT TO authenticated
    USING (email = (auth.jwt() ->> 'email') OR auth_user_id = auth.uid());

CREATE POLICY "insert own app user" ON public.app_users
    FOR INSERT TO authenticated
    WITH CHECK (email = (auth.jwt() ->> 'email') OR auth_user_id = auth.uid());

CREATE POLICY "update own app user" ON public.app_users
    FOR UPDATE TO authenticated
    USING (email = (auth.jwt() ->> 'email') OR auth_user_id = auth.uid())
    WITH CHECK (email = (auth.jwt() ->> 'email') OR auth_user_id = auth.uid());

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE
    ON TABLE public.app_users TO anon, authenticated;
GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.app_users TO postgres, service_role;

-- ---------------------------------------------------------------------------
-- 5. user_certification_access
--    Created before questions/answer_options because their verified
--    production SELECT policies reference this table in an EXISTS subquery.
-- ---------------------------------------------------------------------------
CREATE TABLE public.user_certification_access (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_email     text        NOT NULL,
    exam_name      text        NOT NULL,
    access_status  text        DEFAULT 'active',
    access_source  text        DEFAULT 'manual',
    created_at     timestamptz DEFAULT now(),
    updated_at     timestamptz DEFAULT now(),
    CONSTRAINT user_certification_access_user_email_exam_name_key UNIQUE (user_email, exam_name),
    CONSTRAINT fk_user_certification_access_exam_name FOREIGN KEY (exam_name)
        REFERENCES public.certifications(exam_name) ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_user_certification_access_status CHECK (
        access_status IS NULL OR access_status = ANY (ARRAY['active', 'expired', 'revoked'])
    )
);

CREATE INDEX idx_user_certification_access_exam_name ON public.user_certification_access (exam_name);

ALTER TABLE public.user_certification_access ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read own certification access" ON public.user_certification_access
    FOR SELECT TO authenticated
    USING (user_email = (auth.jwt() ->> 'email'));

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.user_certification_access TO anon, authenticated, postgres, service_role;

-- ---------------------------------------------------------------------------
-- 6. questions
--    NOTE: V44's own header comment confirms "These tables are purely
--    additive. No existing table is modified" and no later migration alters
--    `questions`, so every column below (including translation/versioning
--    fields) is reproduced exactly as verified pre-V44 production state.
-- ---------------------------------------------------------------------------
CREATE TABLE public.questions (
    id                      serial      PRIMARY KEY,
    exam_name               varchar(100) NOT NULL,
    category                varchar(150) NOT NULL,
    difficulty              varchar(10) NOT NULL DEFAULT 'medium',
    question_text           text        NOT NULL,
    question_type           varchar(10) NOT NULL,
    select_count            integer,
    explanation             text        NOT NULL,
    is_active               boolean     DEFAULT true,
    created_at              timestamp   DEFAULT now(),
    is_exam_eligible        boolean     DEFAULT true,
    quality_status          varchar(30) DEFAULT 'approved',
    review_notes            text,
    source_batch            varchar(100),
    source_file             varchar(255),
    updated_at              timestamp   DEFAULT now(),
    free_mock_exam          boolean     DEFAULT false,
    language_code           text        DEFAULT 'en',
    free_sample_order       integer,
    concept_key             text,
    question_family_id      uuid,
    translation_group_id    uuid        NOT NULL DEFAULT gen_random_uuid(),
    practice_eligible       boolean     NOT NULL DEFAULT true,
    mock_eligible           boolean     NOT NULL DEFAULT true,
    cognitive_level         text,
    content_version         integer     NOT NULL DEFAULT 1,
    source_question_id      integer,
    source_content_version  integer,
    translation_status      text,
    external_key            text,
    CONSTRAINT questions_external_key_key UNIQUE (external_key),
    CONSTRAINT questions_source_question_id_fkey FOREIGN KEY (source_question_id)
        REFERENCES public.questions(id) ON UPDATE CASCADE ON DELETE SET NULL,
    CONSTRAINT fk_questions_exam_name FOREIGN KEY (exam_name)
        REFERENCES public.certifications(exam_name) ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_questions_language_code FOREIGN KEY (language_code)
        REFERENCES public.languages(language_code) ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_questions_cognitive_level CHECK (
        cognitive_level IS NULL
        OR cognitive_level = ANY (ARRAY['recall', 'understanding', 'application', 'analysis', 'judgment'])
    ),
    CONSTRAINT chk_questions_content_version CHECK (content_version >= 1),
    CONSTRAINT chk_questions_difficulty CHECK (
        difficulty IS NULL OR difficulty IN ('easy', 'medium', 'hard')
    ),
    CONSTRAINT chk_questions_external_key_not_blank CHECK (
        external_key IS NULL OR length(TRIM(BOTH FROM external_key)) > 0
    ),
    CONSTRAINT chk_questions_quality_status CHECK (
        quality_status IS NULL
        OR quality_status IN ('approved', 'needs_edit', 'practice_only', 'reject')
    ),
    CONSTRAINT chk_questions_question_type CHECK (
        question_type IS NULL OR question_type IN ('single', 'multiple')
    ),
    CONSTRAINT chk_questions_source_content_version CHECK (
        source_content_version IS NULL OR source_content_version >= 1
    ),
    CONSTRAINT chk_questions_source_not_self CHECK (
        source_question_id IS NULL OR source_question_id <> id
    ),
    CONSTRAINT chk_questions_translation_status CHECK (
        translation_status IS NULL
        OR translation_status = ANY (ARRAY['source', 'machine_translated', 'reviewed', 'approved', 'rejected', 'outdated'])
    )
);

CREATE INDEX idx_questions_concept_key ON public.questions (exam_name, concept_key) WHERE (concept_key IS NOT NULL);
CREATE INDEX idx_questions_exam_language ON public.questions (exam_name, language_code);
CREATE INDEX idx_questions_family ON public.questions (question_family_id) WHERE (question_family_id IS NOT NULL);
CREATE INDEX idx_questions_mock_pool ON public.questions (exam_name, language_code, category, mock_eligible, is_active, quality_status);
CREATE INDEX idx_questions_practice_pool ON public.questions (exam_name, language_code, category, practice_eligible, is_active, quality_status);
CREATE INDEX idx_questions_source_question ON public.questions (source_question_id) WHERE (source_question_id IS NOT NULL);
CREATE INDEX idx_questions_translation_group ON public.questions (translation_group_id);

ALTER TABLE public.questions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read allowed questions" ON public.questions
    FOR SELECT TO authenticated
    USING (
        is_active = true
        AND is_exam_eligible = true
        AND quality_status::text = 'approved'
        AND (
            free_mock_exam = true
            OR EXISTS (
                SELECT 1 FROM public.user_certification_access uca
                WHERE uca.user_email = (auth.jwt() ->> 'email')
                  AND uca.exam_name = questions.exam_name::text
                  AND uca.access_status = 'active'
            )
        )
    );

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.questions TO anon, authenticated, postgres, service_role;

-- ---------------------------------------------------------------------------
-- 7. answer_options
-- ---------------------------------------------------------------------------
CREATE TABLE public.answer_options (
    id             serial      PRIMARY KEY,
    question_id    integer     NOT NULL,
    option_label   char(1)     NOT NULL,
    option_text    text        NOT NULL,
    is_correct     boolean     DEFAULT false,
    display_order  integer,
    language_code  text        DEFAULT 'en',
    CONSTRAINT answer_options_question_id_fkey FOREIGN KEY (question_id)
        REFERENCES public.questions(id) ON DELETE CASCADE,
    CONSTRAINT answer_options_language_code_fkey FOREIGN KEY (language_code)
        REFERENCES public.languages(language_code) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_answer_options_question_display_order
    ON public.answer_options (question_id, display_order) WHERE (display_order IS NOT NULL);
CREATE UNIQUE INDEX uq_answer_options_question_label
    ON public.answer_options (question_id, option_label);

ALTER TABLE public.answer_options ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read allowed answer options" ON public.answer_options
    FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.questions q
            WHERE q.id = answer_options.question_id
              AND q.is_active = true
              AND q.is_exam_eligible = true
              AND q.quality_status::text = 'approved'
              AND (
                  q.free_mock_exam = true
                  OR EXISTS (
                      SELECT 1 FROM public.user_certification_access uca
                      WHERE uca.user_email = (auth.jwt() ->> 'email')
                        AND uca.exam_name = q.exam_name::text
                        AND uca.access_status = 'active'
                  )
              )
        )
    );

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.answer_options TO anon, authenticated, postgres, service_role;

-- ---------------------------------------------------------------------------
-- 8. exam_attempts
--    NOTE: chk_exam_attempts_mode is created with its original five allowed
--    values only. V45 drops and recreates this exact constraint, adding
--    'Daily Sprint' as the sixth value.
-- ---------------------------------------------------------------------------
CREATE TABLE public.exam_attempts (
    id                            serial      PRIMARY KEY,
    user_email                    varchar(255),
    mode                          varchar(50),
    category                      varchar(150),
    score                         numeric(5,2),
    total_questions               integer,
    correct_count                 integer,
    started_at                    timestamp   DEFAULT now(),
    completed_at                  timestamp   DEFAULT now(),
    correct_answers               integer,
    domain_breakdown              jsonb,
    difficulty_breakdown          jsonb,
    exam_name                     text,
    language_code                 text        DEFAULT 'en',
    eligible_question_bank_size   integer,
    CONSTRAINT fk_exam_attempts_exam_name FOREIGN KEY (exam_name)
        REFERENCES public.certifications(exam_name) ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_exam_attempts_language_code FOREIGN KEY (language_code)
        REFERENCES public.languages(language_code) ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT chk_exam_attempts_eligible_bank_size CHECK (
        eligible_question_bank_size IS NULL OR eligible_question_bank_size >= 0
    ),
    CONSTRAINT chk_exam_attempts_mode CHECK (
        mode IS NULL
        OR mode::text = ANY (ARRAY[
            'Free Mock Exam', 'Paid Mock Exam', 'Timed Mock Exam',
            'Practice by Category', 'Weak Areas Practice'
        ]::text[])
    )
);

CREATE INDEX idx_exam_attempts_exam_language ON public.exam_attempts (exam_name, language_code);
CREATE INDEX idx_exam_attempts_user_email_completed_at ON public.exam_attempts (user_email, completed_at DESC);

ALTER TABLE public.exam_attempts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read own exam attempts" ON public.exam_attempts
    FOR SELECT TO authenticated
    USING (user_email::text = (auth.jwt() ->> 'email'));

CREATE POLICY "insert own exam attempts" ON public.exam_attempts
    FOR INSERT TO authenticated
    WITH CHECK (user_email::text = (auth.jwt() ->> 'email'));

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.exam_attempts TO anon, authenticated, postgres, service_role;

-- ---------------------------------------------------------------------------
-- 9. question_attempts
-- ---------------------------------------------------------------------------
CREATE TABLE public.question_attempts (
    id                          bigserial   PRIMARY KEY,
    exam_attempt_id             integer,
    question_id                 integer     NOT NULL,
    user_email                  text        NOT NULL,
    exam_name                   text        NOT NULL,
    language_code               text        DEFAULT 'en',
    category                    text        NOT NULL,
    difficulty                  text        NOT NULL,
    selected_options             jsonb,
    correct_options              jsonb,
    is_correct                  boolean     NOT NULL,
    time_spent_seconds           numeric,
    answered_at                  timestamp   DEFAULT now(),
    created_at                  timestamp   DEFAULT now(),
    cognitive_level              text,
    concept_key                  text,
    question_family_id           uuid,
    question_content_version     integer,
    question_external_key        text,
    metadata_source               text,
    metadata_capture_version      text,
    CONSTRAINT uq_question_attempts_exam_attempt_question UNIQUE (exam_attempt_id, question_id),
    CONSTRAINT question_attempts_exam_attempt_id_fkey FOREIGN KEY (exam_attempt_id)
        REFERENCES public.exam_attempts(id) ON DELETE CASCADE,
    CONSTRAINT question_attempts_exam_name_fkey FOREIGN KEY (exam_name)
        REFERENCES public.certifications(exam_name),
    CONSTRAINT question_attempts_language_code_fkey FOREIGN KEY (language_code)
        REFERENCES public.languages(language_code),
    CONSTRAINT question_attempts_question_id_fkey FOREIGN KEY (question_id)
        REFERENCES public.questions(id) ON DELETE CASCADE,
    CONSTRAINT chk_question_attempts_cognitive_level CHECK (
        cognitive_level IS NULL
        OR cognitive_level = ANY (ARRAY['recall', 'understanding', 'application', 'analysis', 'judgment'])
    ),
    CONSTRAINT chk_question_attempts_content_version CHECK (
        question_content_version IS NULL OR question_content_version >= 1
    ),
    CONSTRAINT chk_question_attempts_metadata_source CHECK (
        metadata_source IS NULL
        OR metadata_source = ANY (ARRAY['captured_at_attempt', 'backfilled_current_question'])
    )
);

CREATE INDEX idx_question_attempts_readiness_metadata
    ON public.question_attempts (user_email, exam_name, cognitive_level, concept_key);
CREATE INDEX idx_question_attempts_user_exam_answered_at
    ON public.question_attempts (user_email, exam_name, answered_at DESC);
CREATE INDEX idx_question_attempts_user_question
    ON public.question_attempts (user_email, question_id);

ALTER TABLE public.question_attempts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read own question attempts" ON public.question_attempts
    FOR SELECT TO authenticated
    USING (user_email = (auth.jwt() ->> 'email'));

CREATE POLICY "insert own question attempts" ON public.question_attempts
    FOR INSERT TO authenticated
    WITH CHECK (user_email = (auth.jwt() ->> 'email'));

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.question_attempts TO anon, authenticated, postgres, service_role;

-- ---------------------------------------------------------------------------
-- 10. readiness_snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE public.readiness_snapshots (
    id                            bigint      PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
    user_email                    text        NOT NULL,
    exam_name                     text        NOT NULL,
    exam_attempt_id               integer     NOT NULL,
    formula_version                text        NOT NULL,
    score                         numeric(5,2) NOT NULL,
    label                         text        NOT NULL,
    confidence_score               numeric(5,2),
    eligible_mock_count            integer,
    eligible_question_bank_size    integer,
    component_scores               jsonb       NOT NULL DEFAULT '{}'::jsonb,
    snapshot_data                  jsonb       NOT NULL DEFAULT '{}'::jsonb,
    computed_at                    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_readiness_snapshots_attempt_formula UNIQUE (exam_attempt_id, formula_version),
    CONSTRAINT readiness_snapshots_exam_attempt_id_fkey FOREIGN KEY (exam_attempt_id)
        REFERENCES public.exam_attempts(id) ON DELETE CASCADE,
    CONSTRAINT readiness_snapshots_exam_name_fkey FOREIGN KEY (exam_name)
        REFERENCES public.certifications(exam_name) ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT readiness_snapshots_confidence_score_check CHECK (
        confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 100)
    ),
    CONSTRAINT readiness_snapshots_eligible_mock_count_check CHECK (
        eligible_mock_count IS NULL OR eligible_mock_count >= 0
    ),
    CONSTRAINT readiness_snapshots_eligible_question_bank_size_check CHECK (
        eligible_question_bank_size IS NULL OR eligible_question_bank_size >= 0
    ),
    CONSTRAINT readiness_snapshots_score_check CHECK (score >= 0 AND score <= 100)
);

CREATE INDEX idx_readiness_snapshots_user_exam_time
    ON public.readiness_snapshots (user_email, exam_name, computed_at DESC);

ALTER TABLE public.readiness_snapshots ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read own readiness snapshots" ON public.readiness_snapshots
    FOR SELECT TO authenticated
    USING (user_email = (auth.jwt() ->> 'email'));

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.readiness_snapshots TO anon, authenticated, postgres, service_role;

-- ---------------------------------------------------------------------------
-- 11. support_tickets
-- ---------------------------------------------------------------------------
CREATE TABLE public.support_tickets (
    id                    serial      PRIMARY KEY,
    user_email            varchar(255),
    category              varchar(50),
    description           text,
    attachment            varchar(255),
    status                varchar(20) DEFAULT 'Open',
    created_at            timestamp   DEFAULT now(),
    updated_at            timestamp,
    issue_type            text,
    subject               text,
    message               text,
    question_id           text,
    related_question_id   text,
    admin_notes           text,
    exam_name             text,
    language_code         text        DEFAULT 'en',
    CONSTRAINT fk_support_tickets_exam_name FOREIGN KEY (exam_name)
        REFERENCES public.certifications(exam_name) ON UPDATE CASCADE ON DELETE SET NULL,
    CONSTRAINT fk_support_tickets_language_code FOREIGN KEY (language_code)
        REFERENCES public.languages(language_code) ON UPDATE CASCADE ON DELETE SET NULL,
    CONSTRAINT chk_support_tickets_status CHECK (
        status IS NULL
        OR status IN ('open', 'in_progress', 'resolved', 'closed')
    )
);

CREATE INDEX idx_support_tickets_exam_language ON public.support_tickets (exam_name, language_code);
CREATE INDEX idx_support_tickets_user_email_created_at ON public.support_tickets (user_email, created_at DESC);

ALTER TABLE public.support_tickets ENABLE ROW LEVEL SECURITY;

CREATE POLICY "read own support tickets" ON public.support_tickets
    FOR SELECT TO authenticated
    USING (user_email::text = (auth.jwt() ->> 'email'));

CREATE POLICY "insert own support tickets" ON public.support_tickets
    FOR INSERT TO authenticated
    WITH CHECK (user_email::text = (auth.jwt() ->> 'email'));

GRANT DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE
    ON TABLE public.support_tickets TO anon, authenticated, postgres, service_role;

COMMIT;
