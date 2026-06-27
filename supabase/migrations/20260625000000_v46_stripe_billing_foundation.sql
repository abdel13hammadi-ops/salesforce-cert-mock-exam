-- =============================================================================
-- V46 Phase 5A: Stripe billing foundation (test-mode ready)
-- Created : 2026-06-25 00:00:00 UTC
--
-- Adds billing_events, Stripe subscription columns on app_users, and the
-- canonical apply_stripe_billing_event_v1 RPC used exclusively by the
-- stripe-webhook Edge Function (service_role).
-- =============================================================================


-- =============================================================================
-- 1. billing_events
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.billing_events (
    id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id         text        NOT NULL,
    event_type              text        NOT NULL,
    stripe_object_id        text,
    stripe_event_created_at timestamptz NOT NULL,
    livemode                boolean     NOT NULL,
    processing_status       text        NOT NULL DEFAULT 'pending',
    processed_at            timestamptz,
    error_message           text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_events_stripe_event_id_key UNIQUE (stripe_event_id),
    CONSTRAINT billing_events_processing_status_check CHECK (
        processing_status IN ('pending', 'processed', 'skipped', 'failed')
    )
);

CREATE INDEX IF NOT EXISTS idx_billing_events_created_at
    ON public.billing_events (created_at DESC);

COMMENT ON TABLE public.billing_events IS
'Idempotent ledger of verified Stripe webhook events. Written only via apply_stripe_billing_event_v1.';

ALTER TABLE public.billing_events ENABLE ROW LEVEL SECURITY;


-- =============================================================================
-- 2. app_users Stripe / billing columns (additive)
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app_users' AND column_name = 'stripe_customer_id'
    ) THEN
        ALTER TABLE public.app_users ADD COLUMN stripe_customer_id text;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app_users' AND column_name = 'stripe_subscription_id'
    ) THEN
        ALTER TABLE public.app_users ADD COLUMN stripe_subscription_id text;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app_users' AND column_name = 'stripe_subscription_status'
    ) THEN
        ALTER TABLE public.app_users ADD COLUMN stripe_subscription_status text;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app_users' AND column_name = 'stripe_price_id'
    ) THEN
        ALTER TABLE public.app_users ADD COLUMN stripe_price_id text;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app_users' AND column_name = 'stripe_current_period_end'
    ) THEN
        ALTER TABLE public.app_users ADD COLUMN stripe_current_period_end timestamptz;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app_users' AND column_name = 'stripe_cancel_at_period_end'
    ) THEN
        ALTER TABLE public.app_users ADD COLUMN stripe_cancel_at_period_end boolean NOT NULL DEFAULT false;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app_users' AND column_name = 'stripe_last_event_created_at'
    ) THEN
        ALTER TABLE public.app_users ADD COLUMN stripe_last_event_created_at timestamptz;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app_users' AND column_name = 'billing_updated_at'
    ) THEN
        ALTER TABLE public.app_users ADD COLUMN billing_updated_at timestamptz;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app_users' AND column_name = 'billing_admin_override_at'
    ) THEN
        ALTER TABLE public.app_users ADD COLUMN billing_admin_override_at timestamptz;
    END IF;
END $$;


-- =============================================================================
-- 3. Mapping helper
-- =============================================================================

CREATE OR REPLACE FUNCTION public.map_stripe_subscription_status_to_certbound_v1(
    p_stripe_status text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = public, pg_catalog
AS $$
    SELECT CASE lower(COALESCE(BTRIM(p_stripe_status), ''))
        WHEN 'active' THEN 'active'
        WHEN 'trialing' THEN 'trialing'
        WHEN 'past_due' THEN 'expired'
        WHEN 'unpaid' THEN 'expired'
        WHEN 'canceled' THEN 'expired'
        WHEN 'cancelled' THEN 'expired'
        WHEN 'incomplete' THEN 'expired'
        WHEN 'incomplete_expired' THEN 'expired'
        WHEN 'paused' THEN 'expired'
        ELSE 'free'
    END;
$$;

COMMENT ON FUNCTION public.map_stripe_subscription_status_to_certbound_v1(text) IS
'Maps verified Stripe subscription.status values to canonical app_users.subscription_status.';


-- =============================================================================
-- 4. apply_stripe_billing_event_v1
-- =============================================================================

CREATE OR REPLACE FUNCTION public.apply_stripe_billing_event_v1(
    p_stripe_event_id             text,
    p_event_type                  text,
    p_stripe_object_id            text,
    p_event_created_at            timestamptz,
    p_livemode                    boolean,
    p_certbound_user_id           text,
    p_stripe_customer_id          text,
    p_stripe_subscription_id      text,
    p_stripe_subscription_status  text,
    p_stripe_price_id             text,
    p_stripe_current_period_end   timestamptz,
    p_stripe_cancel_at_period_end boolean,
    p_update_entitlement          boolean DEFAULT true,
    p_revoke_entitlement          boolean DEFAULT false
)
RETURNS TABLE (
    billing_event_id   uuid,
    outcome            text,
    subscription_status text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_event_id uuid;
    v_existing_status text;
    v_user public.app_users%ROWTYPE;
    v_mapped_status text;
    v_apply_subscription boolean := COALESCE(p_update_entitlement, true);
    v_now timestamptz := now();
BEGIN
    IF COALESCE(BTRIM(p_stripe_event_id), '') = '' THEN
        RAISE EXCEPTION 'p_stripe_event_id must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF COALESCE(BTRIM(p_certbound_user_id), '') = '' THEN
        RAISE EXCEPTION 'p_certbound_user_id must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    INSERT INTO public.billing_events (
        stripe_event_id,
        event_type,
        stripe_object_id,
        stripe_event_created_at,
        livemode,
        processing_status
    ) VALUES (
        p_stripe_event_id,
        p_event_type,
        p_stripe_object_id,
        p_event_created_at,
        p_livemode,
        'pending'
    )
    ON CONFLICT (stripe_event_id) DO NOTHING
    RETURNING id INTO v_event_id;

    IF v_event_id IS NULL THEN
        SELECT be.processing_status
        INTO   v_existing_status
        FROM   public.billing_events be
        WHERE  be.stripe_event_id = p_stripe_event_id;

        IF v_existing_status = 'processed' THEN
            RETURN QUERY SELECT NULL::uuid, 'duplicate_processed'::text, NULL::text;
            RETURN;
        END IF;

        SELECT be.id
        INTO   v_event_id
        FROM   public.billing_events be
        WHERE  be.stripe_event_id = p_stripe_event_id;
    END IF;

    SELECT *
    INTO   v_user
    FROM   public.app_users au
    WHERE  au.id::text = p_certbound_user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        UPDATE public.billing_events
        SET    processing_status = 'failed',
               processed_at = v_now,
               error_message = 'certbound user not found'
        WHERE  id = v_event_id;

        RAISE EXCEPTION 'certbound user not found: %', p_certbound_user_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_user.stripe_customer_id IS NOT NULL
       AND COALESCE(BTRIM(p_stripe_customer_id), '') <> ''
       AND v_user.stripe_customer_id <> p_stripe_customer_id THEN
        UPDATE public.billing_events
        SET    processing_status = 'failed',
               processed_at = v_now,
               error_message = 'stripe customer ownership conflict'
        WHERE  id = v_event_id;

        RAISE EXCEPTION 'stripe customer ownership conflict for user %', p_certbound_user_id
            USING ERRCODE = '23505';
    END IF;

    IF v_user.stripe_subscription_id IS NOT NULL
       AND COALESCE(BTRIM(p_stripe_subscription_id), '') <> ''
       AND v_user.stripe_subscription_id <> p_stripe_subscription_id
       AND lower(COALESCE(v_user.stripe_subscription_status, '')) IN ('active', 'trialing', 'past_due') THEN
        UPDATE public.billing_events
        SET    processing_status = 'failed',
               processed_at = v_now,
               error_message = 'stripe subscription ownership conflict'
        WHERE  id = v_event_id;

        RAISE EXCEPTION 'stripe subscription ownership conflict for user %', p_certbound_user_id
            USING ERRCODE = '23505';
    END IF;

    IF v_user.stripe_last_event_created_at IS NOT NULL
       AND p_event_created_at < v_user.stripe_last_event_created_at THEN
        UPDATE public.billing_events
        SET    processing_status = 'skipped',
               processed_at = v_now,
               error_message = 'stale stripe event'
        WHERE  id = v_event_id;

        RETURN QUERY SELECT v_event_id, 'stale'::text, v_user.subscription_status;
        RETURN;
    END IF;

    IF v_user.billing_admin_override_at IS NOT NULL
       AND p_event_created_at <= v_user.billing_admin_override_at THEN
        v_apply_subscription := false;
    END IF;

    UPDATE public.app_users
    SET    stripe_customer_id = COALESCE(NULLIF(BTRIM(p_stripe_customer_id), ''), stripe_customer_id),
           stripe_subscription_id = COALESCE(NULLIF(BTRIM(p_stripe_subscription_id), ''), stripe_subscription_id),
           stripe_subscription_status = COALESCE(NULLIF(BTRIM(p_stripe_subscription_status), ''), stripe_subscription_status),
           stripe_price_id = COALESCE(NULLIF(BTRIM(p_stripe_price_id), ''), stripe_price_id),
           stripe_current_period_end = COALESCE(p_stripe_current_period_end, stripe_current_period_end),
           stripe_cancel_at_period_end = COALESCE(p_stripe_cancel_at_period_end, stripe_cancel_at_period_end),
           stripe_last_event_created_at = p_event_created_at,
           billing_updated_at = v_now
    WHERE  id = v_user.id;

    IF v_apply_subscription THEN
        IF COALESCE(p_revoke_entitlement, false) THEN
            v_mapped_status := 'expired';
        ELSIF COALESCE(BTRIM(p_stripe_subscription_status), '') <> '' THEN
            v_mapped_status := public.map_stripe_subscription_status_to_certbound_v1(p_stripe_subscription_status);
        ELSE
            v_mapped_status := v_user.subscription_status;
        END IF;

        UPDATE public.app_users
        SET    subscription_status = v_mapped_status
        WHERE  id = v_user.id;
    END IF;

    SELECT au.subscription_status
    INTO   v_mapped_status
    FROM   public.app_users au
    WHERE  au.id = v_user.id;

    UPDATE public.billing_events
    SET    processing_status = 'processed',
           processed_at = v_now,
           error_message = NULL
    WHERE  id = v_event_id;

    RETURN QUERY SELECT v_event_id, 'processed'::text, v_mapped_status;
END;
$$;

COMMENT ON FUNCTION public.apply_stripe_billing_event_v1 IS
'Canonical transactional Stripe webhook application. Idempotent on stripe_event_id.';

REVOKE ALL ON FUNCTION public.apply_stripe_billing_event_v1(
    text, text, text, timestamptz, boolean, text, text, text, text, text, timestamptz, boolean, boolean, boolean
) FROM PUBLIC;

REVOKE ALL ON FUNCTION public.apply_stripe_billing_event_v1(
    text, text, text, timestamptz, boolean, text, text, text, text, text, timestamptz, boolean, boolean, boolean
) FROM anon, authenticated;

GRANT EXECUTE ON FUNCTION public.apply_stripe_billing_event_v1(
    text, text, text, timestamptz, boolean, text, text, text, text, text, timestamptz, boolean, boolean, boolean
) TO service_role;

REVOKE ALL ON TABLE public.billing_events FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON TABLE public.billing_events TO service_role;
