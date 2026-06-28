"""Database-backed Stripe Checkout claim helpers."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

CHECKOUT_WINDOW_SECONDS = 900
CHECKOUT_CLAIM_TTL_SECONDS = 900


def checkout_idempotency_key(app_user_id: str, *, now: float | None = None) -> str:
    """Deterministic Stripe idempotency key for a short checkout window."""
    ts = int(now or time.time())
    window = ts // CHECKOUT_WINDOW_SECONDS
    return f"certbound-checkout-{app_user_id}-{window}"


def _first_row(data: Any) -> Dict[str, Any]:
    if isinstance(data, list) and data:
        return dict(data[0] or {})
    if isinstance(data, dict):
        return dict(data)
    return {}


def claim_checkout_session(
    *,
    app_user_id: str,
    idempotency_key: str,
    checkout_url: str,
    checkout_session_id: str = "",
    ttl_seconds: int = CHECKOUT_CLAIM_TTL_SECONDS,
    admin_client=None,
) -> Dict[str, Any]:
    client = admin_client
    if client is None:
        from utils.access_control import get_supabase_admin_client  # noqa: PLC0415

        client = get_supabase_admin_client()

    result = client.rpc(
        "claim_billing_checkout_v1",
        {
            "p_app_user_id": app_user_id,
            "p_idempotency_key": idempotency_key,
            "p_checkout_url": checkout_url,
            "p_checkout_session_id": checkout_session_id or None,
            "p_ttl_seconds": ttl_seconds,
        },
    ).execute()
    row = _first_row(result.data)
    return {
        "claim_id": row.get("claim_id"),
        "checkout_url": str(row.get("checkout_url") or checkout_url),
        "outcome": str(row.get("outcome") or "created"),
    }


def release_checkout_claim(
    *,
    app_user_id: str,
    idempotency_key: str | None = None,
    admin_client=None,
) -> None:
    client = admin_client
    if client is None:
        from utils.access_control import get_supabase_admin_client  # noqa: PLC0415

        client = get_supabase_admin_client()

    payload: Dict[str, Any] = {"p_app_user_id": app_user_id}
    if idempotency_key:
        payload["p_idempotency_key"] = idempotency_key
    client.rpc("release_billing_checkout_claim_v1", payload).execute()


def complete_checkout_claim(
    *,
    checkout_session_id: str | None = None,
    app_user_id: str | None = None,
    admin_client=None,
) -> None:
    client = admin_client
    if client is None:
        from utils.access_control import get_supabase_admin_client  # noqa: PLC0415

        client = get_supabase_admin_client()

    client.rpc(
        "complete_billing_checkout_claim_v1",
        {
            "p_checkout_session_id": checkout_session_id,
            "p_app_user_id": app_user_id,
        },
    ).execute()
