-- =============================================================================
-- V46 Phase 5A corrective: Stripe checkout claims + customer user lookup
-- Created : 2026-06-25 12:00:00 UTC
--
-- Prevents duplicate Checkout Sessions before webhook entitlement updates.
-- Supports webhook user resolution when invoice metadata is absent.
-- =============================================================================


CREATE TABLE IF NOT EXISTS public.billing_checkout_claims (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    app_user_id         text        NOT NULL,
    idempotency_key     text        NOT NULL,
    checkout_session_id text,
    checkout_url        text        NOT NULL,
    claim_status        text        NOT NULL DEFAULT 'pending',
    expires_at          timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_checkout_claims_idempotency_key_key UNIQUE (idempotency_key),
    CONSTRAINT billing_checkout_claims_status_check CHECK (
        claim_status IN ('pending', 'completed', 'released', 'expired')
    )
);

CREATE INDEX IF NOT EXISTS idx_billing_checkout_claims_user_pending
    ON public.billing_checkout_claims (app_user_id, claim_status, expires_at DESC);

ALTER TABLE public.billing_checkout_claims ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.billing_checkout_claims FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON TABLE public.billing_checkout_claims TO service_role;


CREATE OR REPLACE FUNCTION public.expire_billing_checkout_claims_v1(
    p_app_user_id text DEFAULT NULL
)
RETURNS integer
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_count integer;
BEGIN
    UPDATE public.billing_checkout_claims
    SET    claim_status = 'expired',
           updated_at = now()
    WHERE  claim_status = 'pending'
      AND  expires_at <= now()
      AND  (p_app_user_id IS NULL OR app_user_id = p_app_user_id);

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;


CREATE OR REPLACE FUNCTION public.claim_billing_checkout_v1(
    p_app_user_id         text,
    p_idempotency_key     text,
    p_checkout_url        text,
    p_checkout_session_id text DEFAULT NULL,
    p_ttl_seconds         integer DEFAULT 900
)
RETURNS TABLE (
    claim_id        uuid,
    checkout_url  text,
    outcome         text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_existing public.billing_checkout_claims%ROWTYPE;
    v_new_id uuid;
    v_expires_at timestamptz;
BEGIN
    IF COALESCE(BTRIM(p_app_user_id), '') = '' THEN
        RAISE EXCEPTION 'p_app_user_id must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF COALESCE(BTRIM(p_idempotency_key), '') = '' THEN
        RAISE EXCEPTION 'p_idempotency_key must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF COALESCE(BTRIM(p_checkout_url), '') = '' THEN
        RAISE EXCEPTION 'p_checkout_url must not be empty'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM public.expire_billing_checkout_claims_v1(p_app_user_id);

    SELECT *
    INTO   v_existing
    FROM   public.billing_checkout_claims
    WHERE  idempotency_key = p_idempotency_key
    LIMIT  1;

    IF FOUND THEN
        IF v_existing.claim_status = 'pending' AND v_existing.expires_at > now() THEN
            RETURN QUERY SELECT v_existing.id, v_existing.checkout_url, 'reused'::text;
            RETURN;
        END IF;
    END IF;

    SELECT *
    INTO   v_existing
    FROM   public.billing_checkout_claims
    WHERE  app_user_id = p_app_user_id
      AND  claim_status = 'pending'
      AND  expires_at > now()
    ORDER BY created_at DESC
    LIMIT  1;

    IF FOUND THEN
        RETURN QUERY SELECT v_existing.id, v_existing.checkout_url, 'reused'::text;
        RETURN;
    END IF;

    v_expires_at := now() + make_interval(secs => GREATEST(COALESCE(p_ttl_seconds, 900), 60));
    v_new_id := gen_random_uuid();

    INSERT INTO public.billing_checkout_claims (
        id,
        app_user_id,
        idempotency_key,
        checkout_session_id,
        checkout_url,
        claim_status,
        expires_at
    ) VALUES (
        v_new_id,
        p_app_user_id,
        p_idempotency_key,
        NULLIF(BTRIM(p_checkout_session_id), ''),
        p_checkout_url,
        'pending',
        v_expires_at
    );

    RETURN QUERY SELECT v_new_id, p_checkout_url, 'created'::text;
END;
$$;


CREATE OR REPLACE FUNCTION public.release_billing_checkout_claim_v1(
    p_app_user_id     text,
    p_idempotency_key text DEFAULT NULL
)
RETURNS integer
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_count integer;
BEGIN
    UPDATE public.billing_checkout_claims
    SET    claim_status = 'released',
           updated_at = now()
    WHERE  app_user_id = p_app_user_id
      AND  claim_status = 'pending'
      AND  (p_idempotency_key IS NULL OR idempotency_key = p_idempotency_key);

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;


CREATE OR REPLACE FUNCTION public.complete_billing_checkout_claim_v1(
    p_checkout_session_id text DEFAULT NULL,
    p_app_user_id         text DEFAULT NULL
)
RETURNS integer
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_count integer;
BEGIN
    UPDATE public.billing_checkout_claims
    SET    claim_status = 'completed',
           checkout_session_id = COALESCE(NULLIF(BTRIM(p_checkout_session_id), ''), checkout_session_id),
           updated_at = now()
    WHERE  claim_status = 'pending'
      AND  (
            (p_checkout_session_id IS NOT NULL AND checkout_session_id = p_checkout_session_id)
            OR (p_app_user_id IS NOT NULL AND app_user_id = p_app_user_id)
           );

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;


CREATE OR REPLACE FUNCTION public.resolve_app_user_id_by_stripe_customer_v1(
    p_stripe_customer_id text
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_user_id text;
BEGIN
    IF COALESCE(BTRIM(p_stripe_customer_id), '') = '' THEN
        RETURN NULL;
    END IF;

    SELECT au.id::text
    INTO   v_user_id
    FROM   public.app_users au
    WHERE  au.stripe_customer_id = p_stripe_customer_id
    LIMIT  1;

    RETURN v_user_id;
END;
$$;

REVOKE ALL ON FUNCTION public.expire_billing_checkout_claims_v1(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_billing_checkout_v1(text, text, text, text, integer) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_billing_checkout_claim_v1(text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.complete_billing_checkout_claim_v1(text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.resolve_app_user_id_by_stripe_customer_v1(text) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.expire_billing_checkout_claims_v1(text) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_billing_checkout_v1(text, text, text, text, integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.release_billing_checkout_claim_v1(text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_billing_checkout_claim_v1(text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.resolve_app_user_id_by_stripe_customer_v1(text) TO service_role;
