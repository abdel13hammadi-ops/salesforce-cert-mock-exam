"""Canonical Stripe subscription status mapping for CertBound entitlements."""

from __future__ import annotations

from utils.access_control import PAID_STATUS_VALUES

STRIPE_PAID_STATUSES = frozenset({"active", "trialing"})
STRIPE_DENIED_STATUSES = frozenset({
    "past_due",
    "unpaid",
    "canceled",
    "cancelled",
    "incomplete",
    "incomplete_expired",
    "paused",
})


def map_stripe_subscription_status_to_certbound(stripe_status: str) -> str:
    status = str(stripe_status or "").strip().lower()
    if status in STRIPE_PAID_STATUSES:
        return status
    if status in STRIPE_DENIED_STATUSES:
        return "expired"
    return "free"


def certbound_status_grants_premium(certbound_status: str) -> bool:
    return str(certbound_status or "").strip().lower() in PAID_STATUS_VALUES


def stripe_status_grants_premium(stripe_status: str) -> bool:
    return str(stripe_status or "").strip().lower() in STRIPE_PAID_STATUSES


def user_has_blocking_stripe_subscription(profile: dict) -> bool:
    stripe_status = str(profile.get("stripe_subscription_status") or "").strip().lower()
    subscription_id = str(profile.get("stripe_subscription_id") or "").strip()
    if not subscription_id:
        return False
    return stripe_status in STRIPE_PAID_STATUSES or stripe_status in {"past_due", "unpaid"}
