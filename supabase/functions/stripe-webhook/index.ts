import Stripe from "https://esm.sh/stripe@17.4.0?target=deno";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.49.1";

const STRIPE_API_VERSION = "2024-11-20.acacia";

const HANDLED_EVENT_TYPES = new Set([
  "checkout.session.completed",
  "customer.subscription.created",
  "customer.subscription.updated",
  "customer.subscription.deleted",
  "invoice.paid",
  "invoice.payment_failed",
  "charge.dispute.created",
  "charge.refunded",
]);

function certboundUserIdFromMetadata(metadata: Record<string, string> | null | undefined): string {
  const data = metadata ?? {};
  return String(data.certbound_user_id ?? data.certboundUserId ?? "").trim();
}

function unixToIso(value: number | null | undefined): string | null {
  if (value == null) return null;
  return new Date(Number(value) * 1000).toISOString();
}

function normalizeSubscription(subscription: Stripe.Subscription) {
  const firstItem = subscription.items?.data?.[0];
  const priceId = firstItem?.price?.id ?? "";
  return {
    p_stripe_customer_id: String(subscription.customer ?? ""),
    p_stripe_subscription_id: String(subscription.id ?? ""),
    p_stripe_subscription_status: String(subscription.status ?? ""),
    p_stripe_price_id: String(priceId),
    p_stripe_current_period_end: unixToIso(subscription.current_period_end),
    p_stripe_cancel_at_period_end: Boolean(subscription.cancel_at_period_end),
  };
}

function buildRpcPayload(event: Stripe.Event) {
  const obj = event.data.object as Record<string, unknown>;
  const metadata = (obj.metadata ?? {}) as Record<string, string>;
  const payload: Record<string, unknown> = {
    p_stripe_event_id: event.id,
    p_event_type: event.type,
    p_stripe_object_id: String(obj.id ?? ""),
    p_event_created_at: unixToIso(event.created),
    p_livemode: Boolean(event.livemode),
    p_certbound_user_id: "",
    p_stripe_customer_id: "",
    p_stripe_subscription_id: "",
    p_stripe_subscription_status: "",
    p_stripe_price_id: "",
    p_stripe_current_period_end: null,
    p_stripe_cancel_at_period_end: false,
    p_update_entitlement: false,
    p_revoke_entitlement: false,
  };

  switch (event.type) {
    case "checkout.session.completed": {
      const session = obj as Stripe.Checkout.Session;
      payload.p_certbound_user_id = String(session.client_reference_id ?? "") ||
        certboundUserIdFromMetadata(session.metadata as Record<string, string>);
      payload.p_stripe_customer_id = String(session.customer ?? "");
      payload.p_stripe_subscription_id = String(session.subscription ?? "");
      payload.p_update_entitlement = false;
      break;
    }
    case "customer.subscription.created":
    case "customer.subscription.updated": {
      const subscription = obj as Stripe.Subscription;
      Object.assign(payload, normalizeSubscription(subscription));
      payload.p_certbound_user_id = certboundUserIdFromMetadata(subscription.metadata);
      payload.p_update_entitlement = true;
      break;
    }
    case "customer.subscription.deleted": {
      const subscription = obj as Stripe.Subscription;
      Object.assign(payload, normalizeSubscription(subscription));
      payload.p_certbound_user_id = certboundUserIdFromMetadata(subscription.metadata);
      payload.p_stripe_subscription_status = "canceled";
      payload.p_update_entitlement = true;
      break;
    }
    case "invoice.paid": {
      const invoice = obj as Stripe.Invoice;
      payload.p_stripe_customer_id = String(invoice.customer ?? "");
      payload.p_stripe_subscription_id = String(invoice.subscription ?? "");
      payload.p_certbound_user_id = certboundUserIdFromMetadata(invoice.metadata);
      payload.p_stripe_subscription_status = "active";
      payload.p_update_entitlement = true;
      break;
    }
    case "invoice.payment_failed": {
      const invoice = obj as Stripe.Invoice;
      payload.p_stripe_customer_id = String(invoice.customer ?? "");
      payload.p_stripe_subscription_id = String(invoice.subscription ?? "");
      payload.p_certbound_user_id = certboundUserIdFromMetadata(invoice.metadata);
      payload.p_stripe_subscription_status = "past_due";
      payload.p_update_entitlement = true;
      break;
    }
    case "charge.dispute.created":
    case "charge.refunded": {
      const charge = obj as Stripe.Charge;
      payload.p_stripe_customer_id = String(charge.customer ?? "");
      payload.p_certbound_user_id = certboundUserIdFromMetadata(charge.metadata);
      payload.p_revoke_entitlement = true;
      payload.p_update_entitlement = true;
      break;
    }
    default:
      break;
  }

  return payload;
}

function expectedLivemode(): boolean {
  const mode = (Deno.env.get("CERTBOUND_STRIPE_MODE") ?? "test").trim().toLowerCase();
  return mode === "live";
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }

  const webhookSecret = Deno.env.get("STRIPE_WEBHOOK_SECRET") ?? "";
  const stripeSecret = Deno.env.get("STRIPE_SECRET_KEY") ?? "";
  const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

  if (!webhookSecret || !stripeSecret || !supabaseUrl || !serviceRoleKey) {
    return new Response("billing webhook not configured", { status: 500 });
  }

  const signature = req.headers.get("Stripe-Signature") ?? "";
  const rawBody = await req.text();

  const stripe = new Stripe(stripeSecret, {
    apiVersion: STRIPE_API_VERSION,
    httpClient: Stripe.createFetchHttpClient(),
  });

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(rawBody, signature, webhookSecret);
  } catch (_err) {
    return new Response("invalid signature", { status: 400 });
  }

  if (Boolean(event.livemode) !== expectedLivemode()) {
    return new Response("livemode mismatch", { status: 400 });
  }

  if (!HANDLED_EVENT_TYPES.has(event.type)) {
    return new Response(JSON.stringify({ received: true, ignored: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  const rpcPayload = buildRpcPayload(event);
  if (!String(rpcPayload.p_certbound_user_id ?? "").trim()) {
    return new Response("missing certbound user id", { status: 422 });
  }

  const supabase = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const { data, error } = await supabase.rpc("apply_stripe_billing_event_v1", rpcPayload);
  if (error) {
    console.error("apply_stripe_billing_event_v1 failed", error.message);
    return new Response("processing failed", { status: 500 });
  }

  return new Response(JSON.stringify({ received: true, result: data }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});
