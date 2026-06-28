"""Stripe webhook verification and event normalization (Python mirror for tests)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Callable, Dict, Optional, Tuple


class WebhookVerificationError(Exception):
    """Raised when a webhook request fails signature or mode checks."""


def verify_stripe_signature(
    payload: bytes,
    signature_header: str,
    webhook_secret: str,
    *,
    tolerance_seconds: int = 300,
) -> None:
    if not webhook_secret:
        raise WebhookVerificationError("missing webhook secret")
    if not signature_header:
        raise WebhookVerificationError("missing Stripe-Signature header")

    parts = {}
    for item in signature_header.split(","):
        key, _, value = item.partition("=")
        parts.setdefault(key.strip(), []).append(value.strip())

    timestamp = parts.get("t", [None])[0]
    signatures = parts.get("v1", [])
    if not timestamp or not signatures:
        raise WebhookVerificationError("invalid Stripe-Signature header")

    try:
        ts_int = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("invalid signature timestamp") from exc

    if abs(int(time.time()) - ts_int) > tolerance_seconds:
        raise WebhookVerificationError("signature timestamp outside tolerance")

    signed_payload = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise WebhookVerificationError("invalid webhook signature")


def parse_stripe_event(payload: bytes) -> Dict[str, Any]:
    return json.loads(payload.decode("utf-8"))


def certbound_user_id_from_metadata(metadata: Optional[Dict[str, Any]]) -> str:
    metadata = metadata or {}
    for key in ("certbound_user_id", "certboundUserId"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return ""


def unix_to_iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(value)))
    except (TypeError, ValueError):
        return None


def _nested_stripe_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        return str(value.get("id") or "").strip()
    return str(value).strip()


def extract_invoice_subscription_id(invoice: Dict[str, Any]) -> str:
    """Resolve subscription ID from legacy and 2026-06-24.dahlia invoice payloads."""
    subscription_id = _nested_stripe_id(invoice.get("subscription"))
    if subscription_id:
        return subscription_id

    parent = invoice.get("parent") or {}
    subscription_details = parent.get("subscription_details") or {}
    subscription_id = _nested_stripe_id(subscription_details.get("subscription"))
    if subscription_id:
        return subscription_id

    lines = invoice.get("lines") or {}
    for line in lines.get("data") or []:
        line_parent = (line or {}).get("parent") or {}
        item_details = line_parent.get("subscription_item_details") or {}
        subscription_id = _nested_stripe_id(item_details.get("subscription"))
        if subscription_id:
            return subscription_id
    return ""


def extract_invoice_certbound_user_id(invoice: Dict[str, Any]) -> str:
    """Resolve CertBound user ID from Dahlia invoice metadata locations."""
    user_id = certbound_user_id_from_metadata(invoice.get("metadata"))
    if user_id:
        return user_id

    parent = invoice.get("parent") or {}
    subscription_details = parent.get("subscription_details") or {}
    user_id = certbound_user_id_from_metadata(subscription_details.get("metadata"))
    if user_id:
        return user_id

    lines = invoice.get("lines") or {}
    for line in lines.get("data") or []:
        user_id = certbound_user_id_from_metadata((line or {}).get("metadata"))
        if user_id:
            return user_id
    return ""


def normalize_supabase_scalar_rpc_return(data: Any) -> str:
    """Normalize Supabase scalar RPC return shapes to a plain string."""
    if data in (None, ""):
        return ""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, list) and data:
        return normalize_supabase_scalar_rpc_return(data[0])
    if isinstance(data, dict):
        for key in (
            "resolve_app_user_id_by_stripe_customer_v1",
            "value",
            "result",
        ):
            if key in data:
                return normalize_supabase_scalar_rpc_return(data.get(key))
    return str(data).strip()


CANONICAL_INVOICE_BLOCKING_STATUSES = frozenset({"active", "trialing", "past_due"})


def should_ignore_noncanonical_invoice_event(
    *,
    invoice_subscription_id: str,
    canonical_subscription_id: str,
    canonical_subscription_status: str,
) -> bool:
    """Ignore invoice events for a different subscription when canonical is established."""
    invoice_sub = str(invoice_subscription_id or "").strip()
    canonical_sub = str(canonical_subscription_id or "").strip()
    canonical_status = str(canonical_subscription_status or "").strip().lower()
    if not invoice_sub or not canonical_sub:
        return False
    if invoice_sub == canonical_sub:
        return False
    return canonical_status in CANONICAL_INVOICE_BLOCKING_STATUSES


def apply_noncanonical_invoice_guard(
    payload: Dict[str, Any],
    *,
    canonical_subscription_id: str,
    canonical_subscription_status: str,
) -> Tuple[Dict[str, Any], bool]:
    """Strip entitlement/subscription writes for duplicate noncanonical invoice events."""
    payload = dict(payload)
    event_type = str(payload.get("p_event_type") or "")
    if event_type not in {"invoice.paid", "invoice.payment_failed"}:
        return payload, False

    if not should_ignore_noncanonical_invoice_event(
        invoice_subscription_id=str(payload.get("p_stripe_subscription_id") or ""),
        canonical_subscription_id=canonical_subscription_id,
        canonical_subscription_status=canonical_subscription_status,
    ):
        return payload, False

    payload["p_update_entitlement"] = False
    payload["p_revoke_entitlement"] = False
    payload["p_stripe_subscription_id"] = ""
    payload["p_stripe_subscription_status"] = ""
    payload["p_stripe_price_id"] = ""
    payload["p_stripe_current_period_end"] = None
    payload["p_stripe_cancel_at_period_end"] = False
    return payload, True


def normalize_subscription_fields(subscription: Dict[str, Any]) -> Dict[str, Any]:
    items = subscription.get("items") or {}
    data = items.get("data") or []
    price_id = ""
    if data:
        price = (data[0] or {}).get("price") or {}
        price_id = str(price.get("id") or "")

    return {
        "stripe_customer_id": str(subscription.get("customer") or ""),
        "stripe_subscription_id": str(subscription.get("id") or ""),
        "stripe_subscription_status": str(subscription.get("status") or ""),
        "stripe_price_id": price_id,
        "stripe_current_period_end": unix_to_iso(subscription.get("current_period_end")),
        "stripe_cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
    }


def resolve_certbound_user_id(
    certbound_user_id: str,
    stripe_customer_id: str,
    *,
    lookup_by_customer: Callable[[str], str] | None = None,
) -> Tuple[str, bool]:
    resolved = str(certbound_user_id or "").strip()
    if resolved:
        return resolved, False
    customer_id = str(stripe_customer_id or "").strip()
    if not customer_id or lookup_by_customer is None:
        return "", False
    looked_up = normalize_supabase_scalar_rpc_return(lookup_by_customer(customer_id))
    if looked_up:
        return looked_up, True
    return "", False


def build_rpc_payload_from_event(event: Dict[str, Any]) -> Dict[str, Any]:
    event_type = str(event.get("type") or "")
    obj = event.get("data", {}).get("object") or {}
    metadata = obj.get("metadata") or {}

    payload: Dict[str, Any] = {
        "p_stripe_event_id": str(event.get("id") or ""),
        "p_event_type": event_type,
        "p_stripe_object_id": str(obj.get("id") or ""),
        "p_event_created_at": unix_to_iso(event.get("created")),
        "p_livemode": bool(event.get("livemode")),
        "p_certbound_user_id": "",
        "p_stripe_customer_id": "",
        "p_stripe_subscription_id": "",
        "p_stripe_subscription_status": "",
        "p_stripe_price_id": "",
        "p_stripe_current_period_end": None,
        "p_stripe_cancel_at_period_end": False,
        "p_update_entitlement": False,
        "p_revoke_entitlement": False,
    }

    if event_type == "checkout.session.completed":
        payload["p_certbound_user_id"] = str(obj.get("client_reference_id") or "") or certbound_user_id_from_metadata(metadata)
        payload["p_stripe_customer_id"] = str(obj.get("customer") or "")
        payload["p_stripe_subscription_id"] = _nested_stripe_id(obj.get("subscription"))
        payload["p_update_entitlement"] = False
        return payload

    if event_type.startswith("customer.subscription."):
        sub_fields = normalize_subscription_fields(obj)
        payload.update({
            "p_certbound_user_id": certbound_user_id_from_metadata(metadata),
            "p_update_entitlement": True,
            **{f"p_{k}": v for k, v in sub_fields.items()},
        })
        if event_type == "customer.subscription.deleted":
            payload["p_stripe_subscription_status"] = "canceled"
            payload["p_update_entitlement"] = True
        return payload

    if event_type == "invoice.paid":
        payload["p_stripe_customer_id"] = str(obj.get("customer") or "")
        payload["p_stripe_subscription_id"] = extract_invoice_subscription_id(obj)
        payload["p_certbound_user_id"] = extract_invoice_certbound_user_id(obj)
        payload["p_stripe_subscription_status"] = "active"
        payload["p_update_entitlement"] = True
        return payload

    if event_type == "invoice.payment_failed":
        payload["p_stripe_customer_id"] = str(obj.get("customer") or "")
        payload["p_stripe_subscription_id"] = extract_invoice_subscription_id(obj)
        payload["p_certbound_user_id"] = extract_invoice_certbound_user_id(obj)
        payload["p_stripe_subscription_status"] = "past_due"
        payload["p_update_entitlement"] = True
        return payload

    if event_type in {"charge.dispute.created", "charge.refunded"}:
        payload["p_stripe_customer_id"] = str(obj.get("customer") or "")
        payload["p_certbound_user_id"] = certbound_user_id_from_metadata(metadata)
        payload["p_revoke_entitlement"] = True
        payload["p_update_entitlement"] = True
        return payload

    return payload


def should_process_event_type(event_type: str) -> bool:
    return event_type in {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.paid",
        "invoice.payment_failed",
        "charge.dispute.created",
        "charge.refunded",
    }


def prepare_rpc_payload(
    payload: Dict[str, Any],
    *,
    lookup_by_customer: Callable[[str], str] | None = None,
    canonical_subscription_id: str = "",
    canonical_subscription_status: str = "",
) -> Tuple[Dict[str, Any], bool, str]:
    """Resolve missing user ownership and validate payload for RPC processing."""
    user_id, _ = resolve_certbound_user_id(
        str(payload.get("p_certbound_user_id") or ""),
        str(payload.get("p_stripe_customer_id") or ""),
        lookup_by_customer=lookup_by_customer,
    )
    payload = dict(payload)
    if user_id:
        payload["p_certbound_user_id"] = user_id

    ok, message = validate_rpc_payload(payload)
    if not ok:
        return payload, ok, message

    payload, _ignored = apply_noncanonical_invoice_guard(
        payload,
        canonical_subscription_id=canonical_subscription_id,
        canonical_subscription_status=canonical_subscription_status,
    )
    return payload, True, ""


def validate_rpc_payload(payload: Dict[str, Any]) -> Tuple[bool, str]:
    if not str(payload.get("p_stripe_event_id") or "").strip():
        return False, "missing stripe event id"
    if not str(payload.get("p_certbound_user_id") or "").strip():
        return False, "missing certbound user id"
    return True, ""
