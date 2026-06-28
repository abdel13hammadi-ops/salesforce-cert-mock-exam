import { createClient } from "https://esm.sh/@supabase/supabase-js@2.49.1";

const WEBHOOK_TOLERANCE_SECONDS = 300;

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

function certboundUserIdFromMetadata(metadata: Record<string, unknown> | null | undefined): string {
  const data = metadata ?? {};
  return String(
    (data as Record<string, string>).certbound_user_id ??
      (data as Record<string, string>).certboundUserId ??
      "",
  ).trim();
}

function unixToIso(value: number | null | undefined): string | null {
  if (value == null) return null;
  return new Date(Number(value) * 1000).toISOString();
}

function nestedStripeId(value: unknown): string {
  if (value == null || value === "") return "";
  if (typeof value === "object" && value !== null && "id" in value) {
    return String((value as Record<string, unknown>).id ?? "").trim();
  }
  return String(value).trim();
}

function extractInvoiceSubscriptionId(invoice: Record<string, unknown>): string {
  const direct = nestedStripeId(invoice.subscription);
  if (direct) return direct;

  const parent = (invoice.parent ?? {}) as Record<string, unknown>;
  const subscriptionDetails = (parent.subscription_details ?? {}) as Record<string, unknown>;
  const parentSubscription = nestedStripeId(subscriptionDetails.subscription);
  if (parentSubscription) return parentSubscription;

  const lines = (invoice.lines ?? {}) as Record<string, unknown>;
  const lineData = (lines.data ?? []) as Record<string, unknown>[];
  for (const line of lineData) {
    const lineParent = (line.parent ?? {}) as Record<string, unknown>;
    const itemDetails = (lineParent.subscription_item_details ?? {}) as Record<string, unknown>;
    const lineSubscription = nestedStripeId(itemDetails.subscription);
    if (lineSubscription) return lineSubscription;
  }
  return "";
}

function extractInvoiceCertboundUserId(invoice: Record<string, unknown>): string {
  let userId = certboundUserIdFromMetadata(invoice.metadata as Record<string, unknown>);
  if (userId) return userId;

  const parent = (invoice.parent ?? {}) as Record<string, unknown>;
  const subscriptionDetails = (parent.subscription_details ?? {}) as Record<string, unknown>;
  userId = certboundUserIdFromMetadata(subscriptionDetails.metadata as Record<string, unknown>);
  if (userId) return userId;

  const lines = (invoice.lines ?? {}) as Record<string, unknown>;
  const lineData = (lines.data ?? []) as Record<string, unknown>[];
  for (const line of lineData) {
    userId = certboundUserIdFromMetadata(line.metadata as Record<string, unknown>);
    if (userId) return userId;
  }
  return "";
}

function normalizeSupabaseScalarRpcData(data: unknown): string {
  if (data == null || data === "") return "";
  if (typeof data === "string") return data.trim();
  if (Array.isArray(data) && data.length > 0) {
    return normalizeSupabaseScalarRpcData(data[0]);
  }
  if (typeof data === "object") {
    const record = data as Record<string, unknown>;
    for (const key of ["resolve_app_user_id_by_stripe_customer_v1", "value", "result"]) {
      if (key in record) {
        return normalizeSupabaseScalarRpcData(record[key]);
      }
    }
  }
  return String(data).trim();
}

const CANONICAL_INVOICE_BLOCKING_STATUSES = new Set(["active", "trialing", "past_due"]);

function shouldIgnoreNoncanonicalInvoiceEvent(
  invoiceSubscriptionId: string,
  canonicalSubscriptionId: string,
  canonicalSubscriptionStatus: string,
): boolean {
  const invoiceSub = String(invoiceSubscriptionId ?? "").trim();
  const canonicalSub = String(canonicalSubscriptionId ?? "").trim();
  const canonicalStatus = String(canonicalSubscriptionStatus ?? "").trim().toLowerCase();
  if (!invoiceSub || !canonicalSub) return false;
  if (invoiceSub === canonicalSub) return false;
  return CANONICAL_INVOICE_BLOCKING_STATUSES.has(canonicalStatus);
}

function applyNoncanonicalInvoiceGuard(
  payload: Record<string, unknown>,
  canonicalSubscriptionId: string,
  canonicalSubscriptionStatus: string,
): { payload: Record<string, unknown>; ignored: boolean } {
  const eventType = String(payload.p_event_type ?? "");
  if (eventType !== "invoice.paid" && eventType !== "invoice.payment_failed") {
    return { payload, ignored: false };
  }

  if (!shouldIgnoreNoncanonicalInvoiceEvent(
    String(payload.p_stripe_subscription_id ?? ""),
    canonicalSubscriptionId,
    canonicalSubscriptionStatus,
  )) {
    return { payload, ignored: false };
  }

  return {
    payload: {
      ...payload,
      p_update_entitlement: false,
      p_revoke_entitlement: false,
      p_stripe_subscription_id: "",
      p_stripe_subscription_status: "",
      p_stripe_price_id: "",
      p_stripe_current_period_end: null,
      p_stripe_cancel_at_period_end: false,
    },
    ignored: true,
  };
}

function normalizeSubscription(subscription: Record<string, unknown>) {
  const items = (subscription.items ?? {}) as Record<string, unknown>;
  const data = (items.data ?? []) as Record<string, unknown>[];
  const firstItem = data[0] ?? {};
  const price = (firstItem.price ?? {}) as Record<string, unknown>;
  const priceId = String(price.id ?? "");
  return {
    p_stripe_customer_id: String(subscription.customer ?? ""),
    p_stripe_subscription_id: String(subscription.id ?? ""),
    p_stripe_subscription_status: String(subscription.status ?? ""),
    p_stripe_price_id: priceId,
    p_stripe_current_period_end: unixToIso(subscription.current_period_end as number | undefined),
    p_stripe_cancel_at_period_end: Boolean(subscription.cancel_at_period_end),
  };
}

function buildRpcPayload(event: Record<string, unknown>) {
  const eventType = String(event.type ?? "");
  const obj = ((event.data as Record<string, unknown>)?.object ?? {}) as Record<string, unknown>;
  const metadata = (obj.metadata ?? {}) as Record<string, unknown>;
  const payload: Record<string, unknown> = {
    p_stripe_event_id: event.id,
    p_event_type: eventType,
    p_stripe_object_id: String(obj.id ?? ""),
    p_event_created_at: unixToIso(event.created as number | undefined),
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

  switch (eventType) {
    case "checkout.session.completed": {
      payload.p_certbound_user_id = String(obj.client_reference_id ?? "") ||
        certboundUserIdFromMetadata(metadata);
      payload.p_stripe_customer_id = String(obj.customer ?? "");
      payload.p_stripe_subscription_id = nestedStripeId(obj.subscription);
      payload.p_update_entitlement = false;
      break;
    }
    case "customer.subscription.created":
    case "customer.subscription.updated": {
      Object.assign(payload, normalizeSubscription(obj));
      payload.p_certbound_user_id = certboundUserIdFromMetadata(metadata);
      payload.p_update_entitlement = true;
      break;
    }
    case "customer.subscription.deleted": {
      Object.assign(payload, normalizeSubscription(obj));
      payload.p_certbound_user_id = certboundUserIdFromMetadata(metadata);
      payload.p_stripe_subscription_status = "canceled";
      payload.p_update_entitlement = true;
      break;
    }
    case "invoice.paid": {
      payload.p_stripe_customer_id = String(obj.customer ?? "");
      payload.p_stripe_subscription_id = extractInvoiceSubscriptionId(obj);
      payload.p_certbound_user_id = extractInvoiceCertboundUserId(obj);
      payload.p_stripe_subscription_status = "active";
      payload.p_update_entitlement = true;
      break;
    }
    case "invoice.payment_failed": {
      payload.p_stripe_customer_id = String(obj.customer ?? "");
      payload.p_stripe_subscription_id = extractInvoiceSubscriptionId(obj);
      payload.p_certbound_user_id = extractInvoiceCertboundUserId(obj);
      payload.p_stripe_subscription_status = "past_due";
      payload.p_update_entitlement = true;
      break;
    }
    case "charge.dispute.created":
    case "charge.refunded": {
      payload.p_stripe_customer_id = String(obj.customer ?? "");
      payload.p_certbound_user_id = certboundUserIdFromMetadata(metadata);
      payload.p_revoke_entitlement = true;
      payload.p_update_entitlement = true;
      break;
    }
    default:
      break;
  }

  return payload;
}

function parseSignatureHeader(signatureHeader: string): { timestamp: string; signatures: string[] } {
  const parts: Record<string, string[]> = {};
  for (const item of signatureHeader.split(",")) {
    const [key, value] = item.split("=");
    if (!key || value == null) continue;
    parts[key.trim()] ??= [];
    parts[key.trim()].push(value.trim());
  }
  return {
    timestamp: parts.t?.[0] ?? "",
    signatures: parts.v1 ?? [],
  };
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i += 1) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

async function verifyStripeSignature(
  payload: string,
  signatureHeader: string,
  webhookSecret: string,
): Promise<void> {
  if (!webhookSecret) {
    throw new Error("missing webhook secret");
  }
  if (!signatureHeader) {
    throw new Error("missing Stripe-Signature header");
  }

  const { timestamp, signatures } = parseSignatureHeader(signatureHeader);
  if (!timestamp || signatures.length === 0) {
    throw new Error("invalid Stripe-Signature header");
  }

  const tsInt = Number.parseInt(timestamp, 10);
  if (!Number.isFinite(tsInt)) {
    throw new Error("invalid signature timestamp");
  }
  if (Math.abs(Math.floor(Date.now() / 1000) - tsInt) > WEBHOOK_TOLERANCE_SECONDS) {
    throw new Error("signature timestamp outside tolerance");
  }

  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(webhookSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signedPayload = `${timestamp}.${payload}`;
  const digest = await crypto.subtle.sign("HMAC", key, encoder.encode(signedPayload));
  const expected = Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");

  if (!signatures.some((candidate) => timingSafeEqual(expected, candidate))) {
    throw new Error("invalid webhook signature");
  }
}

function expectedLivemode(): boolean {
  const mode = (Deno.env.get("CERTBOUND_STRIPE_MODE") ?? "test").trim().toLowerCase();
  return mode === "live";
}

async function resolveCertboundUserId(
  supabase: ReturnType<typeof createClient>,
  certboundUserId: string,
  stripeCustomerId: string,
): Promise<string> {
  const direct = String(certboundUserId ?? "").trim();
  if (direct) return direct;

  const customerId = String(stripeCustomerId ?? "").trim();
  if (!customerId) return "";

  const { data, error } = await supabase.rpc("resolve_app_user_id_by_stripe_customer_v1", {
    p_stripe_customer_id: customerId,
  });
  if (error) {
    console.error("resolve_app_user_id_by_stripe_customer_v1 failed", error.message);
    return "";
  }
  return String(normalizeSupabaseScalarRpcData(data));
}

async function fetchUserStripeState(
  supabase: ReturnType<typeof createClient>,
  certboundUserId: string,
): Promise<{ stripe_subscription_id: string; stripe_subscription_status: string }> {
  const { data, error } = await supabase
    .from("app_users")
    .select("stripe_subscription_id,stripe_subscription_status")
    .eq("id", certboundUserId)
    .maybeSingle();

  if (error || !data) {
    return { stripe_subscription_id: "", stripe_subscription_status: "" };
  }

  return {
    stripe_subscription_id: String(data.stripe_subscription_id ?? "").trim(),
    stripe_subscription_status: String(data.stripe_subscription_status ?? "").trim(),
  };
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }

  const webhookSecret = Deno.env.get("STRIPE_WEBHOOK_SECRET") ?? "";
  const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

  if (!webhookSecret || !supabaseUrl || !serviceRoleKey) {
    return new Response("billing webhook not configured", { status: 500 });
  }

  const signature = req.headers.get("Stripe-Signature") ?? "";
  const rawBody = await req.text();

  try {
    await verifyStripeSignature(rawBody, signature, webhookSecret);
  } catch (_err) {
    return new Response("invalid signature", { status: 400 });
  }

  let event: Record<string, unknown>;
  try {
    event = JSON.parse(rawBody) as Record<string, unknown>;
  } catch (_err) {
    return new Response("invalid payload", { status: 400 });
  }

  if (Boolean(event.livemode) !== expectedLivemode()) {
    return new Response("livemode mismatch", { status: 400 });
  }

  const eventType = String(event.type ?? "");
  if (!HANDLED_EVENT_TYPES.has(eventType)) {
    return new Response(JSON.stringify({ received: true, ignored: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  const supabase = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const rpcPayload = buildRpcPayload(event);
  const resolvedUserId = await resolveCertboundUserId(
    supabase,
    String(rpcPayload.p_certbound_user_id ?? ""),
    String(rpcPayload.p_stripe_customer_id ?? ""),
  );
  if (resolvedUserId) {
    rpcPayload.p_certbound_user_id = resolvedUserId;
  }

  if (!String(rpcPayload.p_certbound_user_id ?? "").trim()) {
    return new Response("missing certbound user id", { status: 422 });
  }

  let ignoredNoncanonicalInvoice = false;
  if (eventType === "invoice.paid" || eventType === "invoice.payment_failed") {
    const userState = await fetchUserStripeState(
      supabase,
      String(rpcPayload.p_certbound_user_id ?? ""),
    );
    const guarded = applyNoncanonicalInvoiceGuard(
      rpcPayload,
      userState.stripe_subscription_id,
      userState.stripe_subscription_status,
    );
    Object.assign(rpcPayload, guarded.payload);
    ignoredNoncanonicalInvoice = guarded.ignored;
  }

  const { data, error } = await supabase.rpc("apply_stripe_billing_event_v1", rpcPayload);
  if (error) {
    console.error("apply_stripe_billing_event_v1 failed", error.message);
    return new Response("processing failed", { status: 500 });
  }

  if (eventType === "checkout.session.completed") {
    await supabase.rpc("complete_billing_checkout_claim_v1", {
      p_checkout_session_id: String(rpcPayload.p_stripe_object_id ?? ""),
      p_app_user_id: String(rpcPayload.p_certbound_user_id ?? ""),
    });
  }

  return new Response(JSON.stringify({
    received: true,
    result: data,
    ignored: ignoredNoncanonicalInvoice ? "noncanonical_invoice_subscription" : undefined,
  }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});
