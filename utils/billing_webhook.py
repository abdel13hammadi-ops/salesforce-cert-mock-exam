"""Stripe webhook verification and event normalization (Python mirror for tests)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple


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
        payload["p_stripe_subscription_id"] = str(obj.get("subscription") or "")
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
        subscription = obj.get("subscription")
        payload["p_stripe_customer_id"] = str(obj.get("customer") or "")
        payload["p_stripe_subscription_id"] = str(subscription or "")
        payload["p_certbound_user_id"] = certbound_user_id_from_metadata(metadata)
        payload["p_stripe_subscription_status"] = "active"
        payload["p_update_entitlement"] = True
        return payload

    if event_type == "invoice.payment_failed":
        payload["p_stripe_customer_id"] = str(obj.get("customer") or "")
        payload["p_stripe_subscription_id"] = str(obj.get("subscription") or "")
        payload["p_certbound_user_id"] = certbound_user_id_from_metadata(metadata)
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


def validate_rpc_payload(payload: Dict[str, Any]) -> Tuple[bool, str]:
    if not str(payload.get("p_stripe_event_id") or "").strip():
        return False, "missing stripe event id"
    if not str(payload.get("p_certbound_user_id") or "").strip():
        return False, "missing certbound user id"
    return True, ""
