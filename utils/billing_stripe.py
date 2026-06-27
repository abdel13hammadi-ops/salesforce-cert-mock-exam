"""Server-side Stripe Checkout and Customer Portal helpers for Streamlit."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, Optional

from utils.access_control import get_user_profile
from utils.billing_config import (
    ALREADY_SUBSCRIBED_MESSAGE,
    BILLING_UNAVAILABLE_MESSAGE,
    PORTAL_UNAVAILABLE_MESSAGE,
    billing_is_configured,
    get_stripe_cancel_url,
    get_stripe_portal_return_url,
    get_stripe_price_id,
    get_stripe_secret_key,
    get_stripe_success_url,
)
from utils.billing_mapping import user_has_blocking_stripe_subscription

logger = logging.getLogger(__name__)

STRIPE_METADATA_USER_KEY = "certbound_user_id"


class BillingActionError(Exception):
    """Safe billing failure surfaced to the learner UI."""


def _require_profile(email: str) -> Dict[str, Any]:
    profile = get_user_profile(email)
    if not profile:
        raise BillingActionError("Account profile not found. Please log in again.")
    user_id = str(profile.get("id") or "").strip()
    if not user_id:
        raise BillingActionError("Account profile is missing a stable user identifier.")
    return profile


def _stripe_client(*, secrets_getter: Optional[Callable[[str, str], str]] = None):
    secret = get_stripe_secret_key(secrets_getter=secrets_getter)
    if not secret:
        raise BillingActionError(BILLING_UNAVAILABLE_MESSAGE)
    try:
        import stripe  # noqa: PLC0415
    except ImportError as exc:
        logger.exception("stripe package unavailable")
        raise BillingActionError(BILLING_UNAVAILABLE_MESSAGE) from exc

    from utils.billing_config import STRIPE_API_VERSION  # noqa: PLC0415

    stripe.api_key = secret
    stripe.api_version = STRIPE_API_VERSION
    stripe.max_network_retries = 2
    return stripe


def _ensure_checkout_allowed(profile: Dict[str, Any]) -> None:
    if user_has_blocking_stripe_subscription(profile):
        raise BillingActionError(ALREADY_SUBSCRIBED_MESSAGE)


def _get_or_create_customer(
    stripe,
    profile: Dict[str, Any],
    *,
    secrets_getter: Optional[Callable[[str, str], str]] = None,
    idempotency_key: str,
) -> str:
    existing = str(profile.get("stripe_customer_id") or "").strip()
    if existing:
        return existing

    email = str(profile.get("email") or "").strip().lower()
    user_id = str(profile.get("id") or "").strip()
    customer = stripe.Customer.create(
        email=email,
        metadata={STRIPE_METADATA_USER_KEY: user_id},
        idempotency_key=f"{idempotency_key}-customer",
    )
    customer_id = str(getattr(customer, "id", "") or customer.get("id") or "")
    if not customer_id:
        raise BillingActionError(BILLING_UNAVAILABLE_MESSAGE)

    from utils.access_control import get_supabase_admin_client  # noqa: PLC0415

    get_supabase_admin_client().table("app_users").update(
        {"stripe_customer_id": customer_id}
    ).eq("id", user_id).execute()
    profile["stripe_customer_id"] = customer_id
    return customer_id


def create_checkout_session_url(
    email: str,
    *,
    secrets_getter: Optional[Callable[[str, str], str]] = None,
) -> str:
    """Create a hosted Stripe Checkout URL for the authenticated user."""
    if not email:
        raise BillingActionError("Please log in before upgrading.")
    if not billing_is_configured(secrets_getter=secrets_getter):
        raise BillingActionError(BILLING_UNAVAILABLE_MESSAGE)

    profile = _require_profile(email)
    _ensure_checkout_allowed(profile)

    price_id = get_stripe_price_id(secrets_getter=secrets_getter)
    success_url = get_stripe_success_url(secrets_getter=secrets_getter)
    cancel_url = get_stripe_cancel_url(secrets_getter=secrets_getter)
    if not price_id or not success_url or not cancel_url:
        raise BillingActionError(BILLING_UNAVAILABLE_MESSAGE)

    stripe = _stripe_client(secrets_getter=secrets_getter)
    user_id = str(profile.get("id") or "").strip()
    idempotency_key = f"certbound-checkout-{user_id}-{uuid.uuid4()}"

    customer_id = _get_or_create_customer(
        stripe,
        profile,
        secrets_getter=secrets_getter,
        idempotency_key=idempotency_key,
    )

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=user_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={STRIPE_METADATA_USER_KEY: user_id},
        subscription_data={
            "metadata": {STRIPE_METADATA_USER_KEY: user_id},
        },
        idempotency_key=idempotency_key,
    )
    url = str(getattr(session, "url", "") or session.get("url") or "")
    if not url:
        raise BillingActionError(BILLING_UNAVAILABLE_MESSAGE)
    return url


def create_portal_session_url(
    email: str,
    *,
    secrets_getter: Optional[Callable[[str, str], str]] = None,
) -> str:
    """Create a hosted Stripe Customer Portal URL for the authenticated user."""
    if not email:
        raise BillingActionError("Please log in before managing billing.")
    if not billing_is_configured(secrets_getter=secrets_getter):
        raise BillingActionError(BILLING_UNAVAILABLE_MESSAGE)

    profile = _require_profile(email)
    customer_id = str(profile.get("stripe_customer_id") or "").strip()
    if not customer_id:
        raise BillingActionError(PORTAL_UNAVAILABLE_MESSAGE)

    return_url = get_stripe_portal_return_url(secrets_getter=secrets_getter)
    if not return_url:
        raise BillingActionError(BILLING_UNAVAILABLE_MESSAGE)

    stripe = _stripe_client(secrets_getter=secrets_getter)
    user_id = str(profile.get("id") or "").strip()
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
        idempotency_key=f"certbound-portal-{user_id}-{uuid.uuid4()}",
    )
    url = str(getattr(session, "url", "") or session.get("url") or "")
    if not url:
        raise BillingActionError(BILLING_UNAVAILABLE_MESSAGE)
    return url
