"""Stripe billing configuration (names only in repo; values from env/secrets)."""

from __future__ import annotations

import os
from typing import Optional

STRIPE_API_VERSION = "2024-11-20.acacia"

STRIPE_SECRET_ENV = "STRIPE_SECRET_KEY"
STRIPE_WEBHOOK_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"
STRIPE_PRICE_ID_ENV = "STRIPE_PRICE_ID"
STRIPE_SUCCESS_URL_ENV = "STRIPE_SUCCESS_URL"
STRIPE_CANCEL_URL_ENV = "STRIPE_CANCEL_URL"
STRIPE_PORTAL_RETURN_URL_ENV = "STRIPE_PORTAL_RETURN_URL"
CERTBOUND_STRIPE_MODE_ENV = "CERTBOUND_STRIPE_MODE"

CHECKOUT_PENDING_MESSAGE = (
    "Payment received. Premium access will appear after billing confirmation."
)
BILLING_UNAVAILABLE_MESSAGE = (
    "Billing is temporarily unavailable. Please try again later or contact support."
)
PORTAL_UNAVAILABLE_MESSAGE = (
    "Manage subscription is available after you start a subscription checkout."
)
ALREADY_SUBSCRIBED_MESSAGE = (
    "You already have an active subscription. Use Manage subscription instead."
)


def _read_env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def get_stripe_secret_key(*, secrets_getter=None) -> str:
    value = _read_env(STRIPE_SECRET_ENV)
    if value:
        return value
    if secrets_getter is not None:
        return str(secrets_getter(STRIPE_SECRET_ENV, "") or "").strip()
    return ""


def get_stripe_price_id(*, secrets_getter=None) -> str:
    value = _read_env(STRIPE_PRICE_ID_ENV)
    if value:
        return value
    if secrets_getter is not None:
        return str(secrets_getter(STRIPE_PRICE_ID_ENV, "") or "").strip()
    return ""


def get_stripe_success_url(*, secrets_getter=None) -> str:
    value = _read_env(STRIPE_SUCCESS_URL_ENV)
    if value:
        return value
    if secrets_getter is not None:
        return str(secrets_getter(STRIPE_SUCCESS_URL_ENV, "") or "").strip()
    return ""


def get_stripe_cancel_url(*, secrets_getter=None) -> str:
    value = _read_env(STRIPE_CANCEL_URL_ENV)
    if value:
        return value
    if secrets_getter is not None:
        return str(secrets_getter(STRIPE_CANCEL_URL_ENV, "") or "").strip()
    return ""


def get_stripe_portal_return_url(*, secrets_getter=None) -> str:
    value = _read_env(STRIPE_PORTAL_RETURN_URL_ENV)
    if value:
        return value
    if secrets_getter is not None:
        return str(secrets_getter(STRIPE_PORTAL_RETURN_URL_ENV, "") or "").strip()
    return ""


def get_certbound_stripe_mode(*, secrets_getter=None) -> str:
    value = _read_env(CERTBOUND_STRIPE_MODE_ENV).lower()
    if value in {"test", "live"}:
        return value
    if secrets_getter is not None:
        value = str(secrets_getter(CERTBOUND_STRIPE_MODE_ENV, "test") or "test").strip().lower()
        if value in {"test", "live"}:
            return value
    return "test"


def expected_livemode(*, secrets_getter=None) -> bool:
    return get_certbound_stripe_mode(secrets_getter=secrets_getter) == "live"


def livemode_matches_config(event_livemode: bool, *, secrets_getter=None) -> bool:
    return bool(event_livemode) == expected_livemode(secrets_getter=secrets_getter)


def billing_is_configured(*, secrets_getter=None) -> bool:
    return bool(
        get_stripe_secret_key(secrets_getter=secrets_getter)
        and get_stripe_price_id(secrets_getter=secrets_getter)
        and get_stripe_success_url(secrets_getter=secrets_getter)
        and get_stripe_cancel_url(secrets_getter=secrets_getter)
    )
