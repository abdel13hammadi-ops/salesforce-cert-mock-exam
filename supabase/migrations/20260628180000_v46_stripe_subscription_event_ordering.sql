-- =============================================================================
-- V46 corrective: Stripe subscription lifecycle event ordering
-- Created : 2026-06-28 18:00:00 UTC
--
-- Separates subscription lifecycle ordering from checkout/invoice events so a
-- later checkout.session.completed cannot mark an authoritative subscription
-- update stale.
-- =============================================================================


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'app_users'
          AND column_name = 'stripe_last_subscription_event_created_at'
    ) THEN
        ALTER TABLE public.app_users
            ADD COLUMN stripe_last_subscription_event_created_at timestamptz;
    END IF;
END $$;


-- Conservative backfill from processed subscription lifecycle billing events.
UPDATE public.app_users au
SET stripe_last_subscription_event_created_at = sub.max_created_at
FROM (
    SELECT
        be.stripe_object_id,
        MAX(be.stripe_event_created_at) AS max_created_at
    FROM public.billing_events be
    WHERE be.processing_status = 'processed'
      AND be.event_type IN (
          'customer.subscription.created',
          'customer.subscription.updated',
          'customer.subscription.deleted'
      )
      AND COALESCE(BTRIM(be.stripe_object_id), '') <> ''
    GROUP BY be.stripe_object_id
) sub
WHERE au.stripe_last_subscription_event_created_at IS NULL
  AND au.stripe_subscription_id = sub.stripe_object_id;


-- Fallback for rows without processed subscription lifecycle ledger entries.
UPDATE public.app_users
SET stripe_last_subscription_event_created_at = stripe_last_event_created_at
WHERE stripe_last_subscription_event_created_at IS NULL
  AND stripe_last_event_created_at IS NOT NULL
  AND COALESCE(BTRIM(stripe_subscription_id), '') <> '';


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
    v_event_type text := lower(COALESCE(BTRIM(p_event_type), ''));
    v_is_subscription_lifecycle boolean := v_event_type IN (
        'customer.subscription.created',
        'customer.subscription.updated',
        'customer.subscription.deleted'
    );
    v_is_checkout boolean := v_event_type = 'checkout.session.completed';
    v_is_invoice boolean := v_event_type IN ('invoice.paid', 'invoice.payment_failed');
    v_is_revocation boolean := v_event_type IN ('charge.dispute.created', 'charge.refunded');
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

    IF v_is_subscription_lifecycle
       AND v_user.stripe_last_subscription_event_created_at IS NOT NULL
       AND p_event_created_at < v_user.stripe_last_subscription_event_created_at THEN
        UPDATE public.billing_events
        SET    processing_status = 'skipped',
               processed_at = v_now,
               error_message = 'stale stripe subscription event'
        WHERE  id = v_event_id;

        RETURN QUERY SELECT v_event_id, 'stale'::text, v_user.subscription_status;
        RETURN;
    END IF;

    IF v_user.billing_admin_override_at IS NOT NULL
       AND p_event_created_at <= v_user.billing_admin_override_at THEN
        v_apply_subscription := false;
    END IF;

    IF v_is_checkout THEN
        UPDATE public.app_users
        SET    stripe_customer_id = COALESCE(NULLIF(BTRIM(p_stripe_customer_id), ''), stripe_customer_id),
               stripe_subscription_id = COALESCE(NULLIF(BTRIM(p_stripe_subscription_id), ''), stripe_subscription_id),
               billing_updated_at = v_now
        WHERE  id = v_user.id;
    ELSIF v_is_subscription_lifecycle THEN
        UPDATE public.app_users
        SET    stripe_customer_id = COALESCE(NULLIF(BTRIM(p_stripe_customer_id), ''), stripe_customer_id),
               stripe_subscription_id = COALESCE(NULLIF(BTRIM(p_stripe_subscription_id), ''), stripe_subscription_id),
               stripe_subscription_status = COALESCE(NULLIF(BTRIM(p_stripe_subscription_status), ''), stripe_subscription_status),
               stripe_price_id = COALESCE(NULLIF(BTRIM(p_stripe_price_id), ''), stripe_price_id),
               stripe_current_period_end = COALESCE(p_stripe_current_period_end, stripe_current_period_end),
               stripe_cancel_at_period_end = COALESCE(p_stripe_cancel_at_period_end, stripe_cancel_at_period_end),
               stripe_last_subscription_event_created_at = p_event_created_at,
               billing_admin_override_at = CASE
                   WHEN billing_admin_override_at IS NOT NULL
                        AND p_event_created_at > billing_admin_override_at
                   THEN NULL
                   ELSE billing_admin_override_at
               END,
               billing_updated_at = v_now
        WHERE  id = v_user.id;
    ELSIF v_is_invoice OR v_is_revocation THEN
        UPDATE public.app_users
        SET    stripe_customer_id = COALESCE(NULLIF(BTRIM(p_stripe_customer_id), ''), stripe_customer_id),
               stripe_subscription_id = COALESCE(NULLIF(BTRIM(p_stripe_subscription_id), ''), stripe_subscription_id),
               billing_updated_at = v_now
        WHERE  id = v_user.id;
    ELSE
        UPDATE public.app_users
        SET    billing_updated_at = v_now
        WHERE  id = v_user.id;
    END IF;

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
'Canonical transactional Stripe webhook application. Subscription lifecycle ordering uses stripe_last_subscription_event_created_at.';

REVOKE ALL ON FUNCTION public.apply_stripe_billing_event_v1(
    text, text, text, timestamptz, boolean, text, text, text, text, text, timestamptz, boolean, boolean, boolean
) FROM PUBLIC;

REVOKE ALL ON FUNCTION public.apply_stripe_billing_event_v1(
    text, text, text, timestamptz, boolean, text, text, text, text, text, timestamptz, boolean, boolean, boolean
) FROM anon, authenticated;

GRANT EXECUTE ON FUNCTION public.apply_stripe_billing_event_v1(
    text, text, text, timestamptz, boolean, text, text, text, text, text, timestamptz, boolean, boolean, boolean
) TO service_role;
