"""Stripe billing event authority and ordering policy (mirrors apply_stripe_billing_event_v1)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, MutableMapping, Optional, Sequence

SUBSCRIPTION_LIFECYCLE_EVENT_TYPES = frozenset({
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
})

CHECKOUT_EVENT_TYPE = "checkout.session.completed"

INVOICE_EVENT_TYPES = frozenset({
    "invoice.paid",
    "invoice.payment_failed",
})

REVOCATION_EVENT_TYPES = frozenset({
    "charge.dispute.created",
    "charge.refunded",
})


def is_subscription_lifecycle_event(event_type: str) -> bool:
    return str(event_type or "").strip() in SUBSCRIPTION_LIFECYCLE_EVENT_TYPES


def is_checkout_event(event_type: str) -> bool:
    return str(event_type or "").strip() == CHECKOUT_EVENT_TYPE


def is_invoice_event(event_type: str) -> bool:
    return str(event_type or "").strip() in INVOICE_EVENT_TYPES


def is_revocation_event(event_type: str) -> bool:
    return str(event_type or "").strip() in REVOCATION_EVENT_TYPES


def parse_event_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def should_skip_stale_subscription_lifecycle_event(
    *,
    event_type: str,
    event_created_at: Any,
    last_subscription_event_created_at: Any,
) -> bool:
    if not is_subscription_lifecycle_event(event_type):
        return False
    event_ts = parse_event_timestamp(event_created_at)
    watermark_ts = parse_event_timestamp(last_subscription_event_created_at)
    if event_ts is None or watermark_ts is None:
        return False
    return event_ts < watermark_ts


def admin_override_blocks_entitlement(
    *,
    event_created_at: Any,
    billing_admin_override_at: Any,
) -> bool:
    override_ts = parse_event_timestamp(billing_admin_override_at)
    if override_ts is None:
        return False
    event_ts = parse_event_timestamp(event_created_at)
    if event_ts is None:
        return False
    return event_ts <= override_ts


def subscription_lifecycle_restores_automated_billing(
    *,
    event_type: str,
    event_created_at: Any,
    billing_admin_override_at: Any,
) -> bool:
    if not is_subscription_lifecycle_event(event_type):
        return False
    override_ts = parse_event_timestamp(billing_admin_override_at)
    event_ts = parse_event_timestamp(event_created_at)
    if override_ts is None or event_ts is None:
        return False
    return event_ts > override_ts


def map_stripe_status_to_certbound_status(stripe_status: str, *, revoke: bool = False) -> str:
    if revoke:
        return "expired"
    from utils.billing_mapping import map_stripe_subscription_status_to_certbound

    return map_stripe_subscription_status_to_certbound(stripe_status)


def apply_billing_event_to_user_state(
    user: MutableMapping[str, Any],
    *,
    event_type: str,
    event_created_at: Any,
    stripe_customer_id: str = "",
    stripe_subscription_id: str = "",
    stripe_subscription_status: str = "",
    stripe_price_id: str = "",
    stripe_current_period_end: Any = None,
    stripe_cancel_at_period_end: bool = False,
    update_entitlement: bool = True,
    revoke_entitlement: bool = False,
) -> str:
    """Apply one normalized billing event to an in-memory user row. Returns outcome."""
    if should_skip_stale_subscription_lifecycle_event(
        event_type=event_type,
        event_created_at=event_created_at,
        last_subscription_event_created_at=user.get("stripe_last_subscription_event_created_at"),
    ):
        return "stale"

    apply_entitlement = bool(update_entitlement)
    if admin_override_blocks_entitlement(
        event_created_at=event_created_at,
        billing_admin_override_at=user.get("billing_admin_override_at"),
    ):
        apply_entitlement = False

    if is_checkout_event(event_type):
        if stripe_customer_id:
            user["stripe_customer_id"] = stripe_customer_id
        if stripe_subscription_id:
            user["stripe_subscription_id"] = stripe_subscription_id
    elif is_subscription_lifecycle_event(event_type):
        if stripe_customer_id:
            user["stripe_customer_id"] = stripe_customer_id
        if stripe_subscription_id:
            user["stripe_subscription_id"] = stripe_subscription_id
        if stripe_subscription_status:
            user["stripe_subscription_status"] = stripe_subscription_status
        if stripe_price_id:
            user["stripe_price_id"] = stripe_price_id
        if stripe_current_period_end is not None:
            user["stripe_current_period_end"] = stripe_current_period_end
        user["stripe_cancel_at_period_end"] = bool(stripe_cancel_at_period_end)
        user["stripe_last_subscription_event_created_at"] = event_created_at
        if subscription_lifecycle_restores_automated_billing(
            event_type=event_type,
            event_created_at=event_created_at,
            billing_admin_override_at=user.get("billing_admin_override_at"),
        ):
            user["billing_admin_override_at"] = None
    elif is_invoice_event(event_type) or is_revocation_event(event_type):
        if stripe_customer_id:
            user["stripe_customer_id"] = stripe_customer_id
        if stripe_subscription_id:
            user["stripe_subscription_id"] = stripe_subscription_id

    if apply_entitlement:
        if revoke_entitlement:
            user["subscription_status"] = "expired"
        elif stripe_subscription_status:
            user["subscription_status"] = map_stripe_status_to_certbound_status(stripe_subscription_status)

    return "processed"


def apply_billing_events_in_delivery_order(
    user: MutableMapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> MutableMapping[str, Any]:
    state = dict(user)
    for event in events:
        apply_billing_event_to_user_state(
            state,
            event_type=str(event.get("event_type") or ""),
            event_created_at=event.get("event_created_at"),
            stripe_customer_id=str(event.get("stripe_customer_id") or ""),
            stripe_subscription_id=str(event.get("stripe_subscription_id") or ""),
            stripe_subscription_status=str(event.get("stripe_subscription_status") or ""),
            stripe_price_id=str(event.get("stripe_price_id") or ""),
            stripe_current_period_end=event.get("stripe_current_period_end"),
            stripe_cancel_at_period_end=bool(event.get("stripe_cancel_at_period_end")),
            update_entitlement=bool(event.get("update_entitlement", True)),
            revoke_entitlement=bool(event.get("revoke_entitlement", False)),
        )
    return state
