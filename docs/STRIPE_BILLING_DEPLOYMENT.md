# CertBound Stripe Billing Deployment (Phase 5A)

Test-mode foundation for self-service subscriptions. Do not paste real secrets into the repository.

## Architecture boundaries

| Action | Boundary |
|---|---|
| Checkout Session | Streamlit server (`utils/billing_stripe.py`) using signed login + service-role profile lookup |
| Customer Portal | Streamlit server (`utils/billing_stripe.py`) |
| Stripe webhooks | Supabase Edge Function `supabase/functions/stripe-webhook` (JWT verification disabled) |
| Entitlement writes | Postgres RPC `apply_stripe_billing_event_v1` (service_role only) |

Stable internal user identifier: `app_users.id` (`certbound_user_id` metadata).

## 1. Apply migration

```bash
psql "$DATABASE_URL" -f supabase/migrations/20260625000000_v46_stripe_billing_foundation.sql
```

Creates `billing_events`, additive Stripe columns on `app_users`, mapping helper, and `apply_stripe_billing_event_v1`.

## 2. Configure Streamlit secrets / Render env

| Name | Purpose |
|---|---|
| `CERTBOUND_STRIPE_MODE` | `test` or `live` |
| `STRIPE_SECRET_KEY` | Server-side Stripe API key |
| `STRIPE_PRICE_ID` | Subscription price (server-controlled) |
| `STRIPE_SUCCESS_URL` | e.g. `https://<app>/Account?billing=success` |
| `STRIPE_CANCEL_URL` | e.g. `https://<app>/Account?billing=cancel` |
| `STRIPE_PORTAL_RETURN_URL` | e.g. `https://<app>/Account` |

Existing Supabase secrets remain required (`SUPABASE_URL`, keys, `COOKIE_PASSWORD`, etc.).

## 3. Deploy Edge Function secrets

Set on the Supabase project (Function secrets):

| Name | Purpose |
|---|---|
| `STRIPE_SECRET_KEY` | Stripe API (same mode as Streamlit) |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for `stripe-webhook` |
| `SUPABASE_URL` | Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | RPC caller |
| `CERTBOUND_STRIPE_MODE` | Must match Streamlit (`test` or `live`) |

## 4. Deploy Edge Function

```bash
supabase functions deploy stripe-webhook --no-verify-jwt
```

`supabase/config.toml` sets `verify_jwt = false` for this function.

## 5. Register Stripe webhook endpoint

Endpoint URL:

`https://<project-ref>.supabase.co/functions/v1/stripe-webhook`

Subscribe at minimum to:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.paid`
- `invoice.payment_failed`
- `charge.dispute.created`
- `charge.refunded`

Use the **test** webhook signing secret in test mode.

## 6. Activate Stripe Customer Portal

In Stripe Dashboard → Settings → Customer portal:

- Enable subscription management
- Set return URL to `STRIPE_PORTAL_RETURN_URL`

## 7. Test-mode verification

1. Set `CERTBOUND_STRIPE_MODE=test` everywhere.
2. Create a free test user in the app.
3. Account → **Upgrade to Premium** → complete Checkout with test card `4242…`.
4. Confirm webhook events appear in Stripe Dashboard (test mode).
5. Confirm `billing_events` row is `processed` and `app_users.subscription_status` becomes `active` or `trialing`.
6. Return to Account with `?billing=success` before webhook: UI shows pending message, not premium.
7. **Manage subscription** opens Customer Portal for mapped customers.

## 8. Switch-to-live checklist

- [ ] Replace all keys with live-mode Stripe keys
- [ ] Set `CERTBOUND_STRIPE_MODE=live` on Streamlit and Edge Function
- [ ] Create live webhook endpoint + signing secret
- [ ] Create live Price and update `STRIPE_PRICE_ID`
- [ ] Verify livemode mismatch rejects test events (400)
- [ ] Run production smoke with a real card in controlled cohort

## Admin override policy

Manual **Grant Premium / Set Free / Mark Expired** in Admin Users sets `billing_admin_override_at`. Stripe webhook events with `event.created` **older than or equal to** that timestamp update Stripe metadata only and do **not** change `subscription_status`. Newer verified Stripe events restore automated billing control.

## Reconciliation

If access and Stripe diverge:

1. Compare Stripe subscription status vs `app_users.stripe_subscription_status`.
2. Inspect `billing_events` for `failed` / `skipped`.
3. Re-send event from Stripe Dashboard or fix user mapping manually via Admin Users.

No payment card data is stored in CertBound tables.
