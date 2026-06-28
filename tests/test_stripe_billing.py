"""Focused tests for Phase 5A Stripe billing foundation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.billing_config import (
    CHECKOUT_PENDING_MESSAGE,
    CHECKOUT_SUCCESS_SIGNIN_MESSAGE,
    STRIPE_WEBHOOK_API_VERSION,
    expected_livemode,
    livemode_matches_config,
)
from utils.billing_checkout import (
    CHECKOUT_WINDOW_SECONDS,
    checkout_idempotency_key,
    release_checkout_claim,
)
from utils.billing_mapping import (
    certbound_status_grants_premium,
    customer_subscriptions_block_checkout,
    map_stripe_subscription_status_to_certbound,
    stripe_status_blocks_new_checkout,
    stripe_status_grants_premium,
    user_has_blocking_stripe_subscription,
)
from utils.billing_stripe import (
    BillingActionError,
    PORTAL_MANAGE_LABEL,
    PORTAL_SESSION_CACHE_SECONDS,
    STRIPE_METADATA_USER_KEY,
    cache_portal_session_url,
    clear_cached_portal_session,
    create_checkout_session_url,
    create_portal_session_url,
    get_cached_portal_session_url,
    render_portal_session_link_markdown,
    resolve_portal_session_url,
    release_pending_checkout_claim,
    validate_stripe_portal_url,
)
from utils.billing_webhook import (
    WebhookVerificationError,
    apply_noncanonical_invoice_guard,
    build_rpc_payload_from_event,
    extract_invoice_certbound_user_id,
    extract_invoice_subscription_id,
    normalize_supabase_scalar_rpc_return,
    prepare_rpc_payload,
    resolve_certbound_user_id,
    should_ignore_noncanonical_invoice_event,
    should_process_event_type,
    validate_rpc_payload,
    verify_stripe_signature,
)
from tests.fixtures.dahlia_invoice_paid_production import (
    DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID,
    DAHLIA_FIXTURE_CUSTOMER_ID,
    DAHLIA_FIXTURE_DUPLICATE_SUBSCRIPTION_ID,
    DAHLIA_FIXTURE_USER_ID,
    DAHLIA_INVOICE_PAID_FIXTURE_EVENT,
)
from utils.access_control import PAID_STATUS_VALUES

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO_ROOT / "supabase" / "migrations" / "20260625000000_v46_stripe_billing_foundation.sql"
CLAIMS_MIGRATION_PATH = REPO_ROOT / "supabase" / "migrations" / "20260625120000_v46_stripe_checkout_claims.sql"
ORDERING_MIGRATION_PATH = REPO_ROOT / "supabase" / "migrations" / "20260628180000_v46_stripe_subscription_event_ordering.sql"
EDGE_FUNCTION_PATH = REPO_ROOT / "supabase" / "functions" / "stripe-webhook" / "index.ts"
ACCOUNT_PATH = REPO_ROOT / "pages" / "Account.py"
ADMIN_USERS_PATH = REPO_ROOT / "pages" / "Admin_Users.py"

USER_ID = "11111111-1111-1111-1111-111111111111"
CUSTOMER_ID = "cus_test_001"
SUBSCRIPTION_ID = "sub_test_001"
PRICE_ID = "price_test_001"


def _secrets(**values: str):
    def getter(name: str, default: str = "") -> str:
        return values.get(name, default)

    return getter


def _signed_payload(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = timestamp or int(time.time())
    signed = f"{ts}.{payload.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def _profile(**overrides):
    base = {
        "id": USER_ID,
        "email": "learner@example.com",
        "subscription_status": "free",
        "stripe_customer_id": "",
        "stripe_subscription_id": "",
        "stripe_subscription_status": "",
    }
    base.update(overrides)
    return base


def _mock_checkout_admin(*, pending_rows=None, claim_url="https://checkout.stripe.test/session_123"):
    admin = MagicMock()

    def rpc_side_effect(name, payload=None):
        result = MagicMock()
        if name == "expire_billing_checkout_claims_v1":
            result.execute.return_value = MagicMock(data=0)
        elif name == "claim_billing_checkout_v1":
            result.execute.return_value = MagicMock(
                data=[{
                    "claim_id": "claim-1",
                    "checkout_url": claim_url,
                    "outcome": "created",
                }]
            )
        elif name == "release_billing_checkout_claim_v1":
            result.execute.return_value = MagicMock(data=1)
        return result

    admin.rpc.side_effect = rpc_side_effect

    def table_side_effect(name):
        table = MagicMock()
        if name == "billing_checkout_claims":
            table.select.return_value.eq.return_value.eq.return_value.gt.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
                data=pending_rows or []
            )
        elif name == "app_users":
            table.update.return_value.eq.return_value.execute.return_value = MagicMock()
        return table

    admin.table.side_effect = table_side_effect
    return admin


def _checkout_secrets():
    return _secrets(
        STRIPE_SECRET_KEY="sk_test_x",
        STRIPE_PRICE_ID=PRICE_ID,
        STRIPE_SUCCESS_URL="https://app.example/Account?billing=success",
        STRIPE_CANCEL_URL="https://app.example/Account?billing=cancel",
    )


class TestMigrationShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def test_billing_events_table_and_unique_event_id(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS public.billing_events", self.sql)
        self.assertIn("billing_events_stripe_event_id_key UNIQUE", self.sql)

    def test_apply_rpc_is_service_role_only(self):
        self.assertIn("apply_stripe_billing_event_v1", self.sql)
        self.assertIn("GRANT EXECUTE ON FUNCTION public.apply_stripe_billing_event_v1", self.sql)
        self.assertIn("REVOKE ALL ON FUNCTION public.apply_stripe_billing_event_v1", self.sql)

    def test_admin_override_and_stale_event_logic_present(self):
        self.assertIn("billing_admin_override_at", self.sql)
        self.assertIn("stale stripe event", self.sql)
        self.assertIn("customer ownership conflict", self.sql)

    def test_mapping_function_matches_python(self):
        self.assertIn("map_stripe_subscription_status_to_certbound_v1", self.sql)
        self.assertEqual(map_stripe_subscription_status_to_certbound("active"), "active")
        self.assertEqual(map_stripe_subscription_status_to_certbound("past_due"), "expired")


class TestStatusMapping(unittest.TestCase):
    def test_active_and_trialing_grant_access(self):
        self.assertTrue(stripe_status_grants_premium("active"))
        self.assertTrue(stripe_status_grants_premium("trialing"))
        self.assertTrue(certbound_status_grants_premium("active"))
        self.assertTrue(certbound_status_grants_premium("trialing"))

    def test_denied_statuses_map_to_expired(self):
        for status in ("past_due", "unpaid", "canceled", "incomplete", "incomplete_expired", "paused"):
            mapped = map_stripe_subscription_status_to_certbound(status)
            self.assertEqual(mapped, "expired")
            self.assertFalse(certbound_status_grants_premium(mapped))

    def test_later_active_restores_access(self):
        self.assertTrue(certbound_status_grants_premium(map_stripe_subscription_status_to_certbound("active")))

    def test_trialing_in_paid_status_values(self):
        self.assertIn("trialing", PAID_STATUS_VALUES)


class TestCheckout(unittest.TestCase):
    def test_unauthenticated_user_cannot_create_checkout(self):
        with self.assertRaises(BillingActionError):
            create_checkout_session_url("", secrets_getter=_secrets(
                STRIPE_SECRET_KEY="sk_test_x",
                STRIPE_PRICE_ID=PRICE_ID,
                STRIPE_SUCCESS_URL="https://app.example/Account?billing=success",
                STRIPE_CANCEL_URL="https://app.example/Account?billing=cancel",
            ))

    @patch("utils.billing_stripe._admin_client")
    @patch("utils.billing_stripe._stripe_client")
    @patch("utils.billing_stripe.get_user_profile")
    def test_checkout_uses_server_price_and_metadata(self, mock_profile, mock_stripe_client, mock_admin_client):
        mock_profile.return_value = _profile()
        mock_admin_client.return_value = _mock_checkout_admin()
        stripe = MagicMock()
        mock_stripe_client.return_value = stripe
        customer = MagicMock()
        customer.id = CUSTOMER_ID
        stripe.Customer.create.return_value = customer
        stripe.Subscription.list.return_value = MagicMock(data=[])
        session = MagicMock()
        session.url = "https://checkout.stripe.test/session_123"
        session.id = "cs_test_123"
        stripe.checkout.Session.create.return_value = session

        url = create_checkout_session_url("learner@example.com", secrets_getter=_checkout_secrets())

        self.assertEqual(url, "https://checkout.stripe.test/session_123")
        kwargs = stripe.checkout.Session.create.call_args.kwargs
        self.assertEqual(kwargs["line_items"][0]["price"], PRICE_ID)
        self.assertEqual(kwargs["client_reference_id"], USER_ID)
        self.assertEqual(kwargs["metadata"][STRIPE_METADATA_USER_KEY], USER_ID)
        self.assertEqual(kwargs["subscription_data"]["metadata"][STRIPE_METADATA_USER_KEY], USER_ID)
        self.assertEqual(kwargs["idempotency_key"], checkout_idempotency_key(USER_ID))

    @patch("utils.billing_stripe._admin_client")
    @patch("utils.billing_stripe._stripe_client")
    @patch("utils.billing_stripe.get_user_profile")
    def test_existing_customer_is_reused(self, mock_profile, mock_stripe_client, mock_admin_client):
        mock_profile.return_value = _profile(stripe_customer_id=CUSTOMER_ID)
        mock_admin_client.return_value = _mock_checkout_admin()
        stripe = MagicMock()
        mock_stripe_client.return_value = stripe
        stripe.Subscription.list.return_value = MagicMock(data=[])
        session = MagicMock()
        session.url = "https://checkout.stripe.test/session_456"
        session.id = "cs_test_456"
        stripe.checkout.Session.create.return_value = session
        create_checkout_session_url("learner@example.com", secrets_getter=_checkout_secrets())
        stripe.Customer.create.assert_not_called()
        self.assertEqual(stripe.checkout.Session.create.call_args.kwargs["customer"], CUSTOMER_ID)

    @patch("utils.billing_stripe._admin_client")
    @patch("utils.billing_stripe._stripe_client")
    @patch("utils.billing_stripe.get_user_profile")
    def test_repeated_upgrade_clicks_reuse_pending_checkout(self, mock_profile, mock_stripe_client, mock_admin_client):
        mock_profile.return_value = _profile(stripe_customer_id=CUSTOMER_ID)
        pending_url = "https://checkout.stripe.test/pending_789"
        mock_admin_client.return_value = _mock_checkout_admin(
            pending_rows=[{"checkout_url": pending_url}],
        )
        stripe = MagicMock()
        mock_stripe_client.return_value = stripe

        url = create_checkout_session_url("learner@example.com", secrets_getter=_checkout_secrets())

        self.assertEqual(url, pending_url)
        stripe.checkout.Session.create.assert_not_called()

    @patch("utils.billing_stripe._admin_client")
    @patch("utils.billing_stripe._stripe_client")
    @patch("utils.billing_stripe.get_user_profile")
    def test_stripe_blocking_subscription_prevents_checkout(self, mock_profile, mock_stripe_client, mock_admin_client):
        mock_profile.return_value = _profile(stripe_customer_id=CUSTOMER_ID)
        mock_admin_client.return_value = _mock_checkout_admin()
        stripe = MagicMock()
        mock_stripe_client.return_value = stripe
        stripe.Subscription.list.return_value = MagicMock(data=[{"status": "active"}])

        with self.assertRaises(BillingActionError):
            create_checkout_session_url("learner@example.com", secrets_getter=_checkout_secrets())

        stripe.checkout.Session.create.assert_not_called()

    @patch("utils.billing_stripe.release_checkout_claim")
    @patch("utils.billing_stripe._admin_client")
    @patch("utils.billing_stripe._stripe_client")
    @patch("utils.billing_stripe.get_user_profile")
    def test_failed_checkout_creation_releases_claim(
        self,
        mock_profile,
        mock_stripe_client,
        mock_admin_client,
        mock_release,
    ):
        mock_profile.return_value = _profile(stripe_customer_id=CUSTOMER_ID)
        mock_admin_client.return_value = _mock_checkout_admin()
        stripe = MagicMock()
        mock_stripe_client.return_value = stripe
        stripe.Subscription.list.return_value = MagicMock(data=[])
        stripe.checkout.Session.create.side_effect = RuntimeError("stripe down")

        with self.assertRaises(BillingActionError):
            create_checkout_session_url("learner@example.com", secrets_getter=_checkout_secrets())

        mock_release.assert_called_once()

    @patch("utils.billing_stripe.get_user_profile")
    def test_active_subscriber_cannot_start_parallel_checkout(self, mock_profile):
        mock_profile.return_value = _profile(
            stripe_customer_id=CUSTOMER_ID,
            stripe_subscription_id=SUBSCRIPTION_ID,
            stripe_subscription_status="active",
        )
        with self.assertRaises(BillingActionError):
            create_checkout_session_url(
                "learner@example.com",
                secrets_getter=_secrets(
                    STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_PRICE_ID=PRICE_ID,
                    STRIPE_SUCCESS_URL="https://app.example/success",
                    STRIPE_CANCEL_URL="https://app.example/cancel",
                ),
            )

    @patch("utils.billing_stripe.get_user_profile")
    def test_checkout_errors_are_sanitized(self, mock_profile):
        mock_profile.return_value = None
        with self.assertRaises(BillingActionError) as ctx:
            create_checkout_session_url(
                "learner@example.com",
                secrets_getter=_secrets(
                    STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_PRICE_ID=PRICE_ID,
                    STRIPE_SUCCESS_URL="https://app.example/success",
                    STRIPE_CANCEL_URL="https://app.example/cancel",
                ),
            )
        self.assertNotIn("sk_test", str(ctx.exception))

    def test_account_ui_does_not_embed_secrets(self):
        text = ACCOUNT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("sk_test", text)
        self.assertNotIn("STRIPE_SECRET_KEY", text.replace("secrets_getter", ""))
        self.assertIn("Upgrade to Premium", text)
        self.assertIn("render_portal_session_link_markdown", text)


class TestWebhookSecurity(unittest.TestCase):
    def test_missing_signature_rejected(self):
        payload = b'{"id":"evt_test"}'
        with self.assertRaises(WebhookVerificationError):
            verify_stripe_signature(payload, "", "whsec_test")

    def test_invalid_signature_rejected(self):
        payload = b'{"id":"evt_test"}'
        with self.assertRaises(WebhookVerificationError):
            verify_stripe_signature(payload, "t=1,v1=bad", "whsec_test")

    def test_valid_signature_accepted(self):
        secret = "whsec_test_secret"
        payload = b'{"id":"evt_test"}'
        header = _signed_payload(payload, secret)
        verify_stripe_signature(payload, header, secret)

    def test_livemode_mismatch_rejected(self):
        with patch.dict(os.environ, {"CERTBOUND_STRIPE_MODE": "test"}, clear=False):
            self.assertFalse(livemode_matches_config(True))
            self.assertTrue(livemode_matches_config(False))
        with patch.dict(os.environ, {"CERTBOUND_STRIPE_MODE": "live"}, clear=False):
            self.assertTrue(livemode_matches_config(True))
            self.assertFalse(livemode_matches_config(False))

    def test_browser_cannot_invoke_entitlement_directly(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_checkout_1",
            "type": "checkout.session.completed",
            "livemode": False,
            "created": 1_700_000_000,
            "data": {
                "object": {
                    "id": "cs_test_1",
                    "client_reference_id": USER_ID,
                    "customer": CUSTOMER_ID,
                    "subscription": SUBSCRIPTION_ID,
                    "metadata": {STRIPE_METADATA_USER_KEY: USER_ID},
                }
            },
        })
        self.assertFalse(payload["p_update_entitlement"])
        self.assertNotEqual(payload.get("p_stripe_subscription_status"), "active")

    def test_edge_function_verifies_signature_and_mode(self):
        text = EDGE_FUNCTION_PATH.read_text(encoding="utf-8")
        self.assertIn("verifyStripeSignature", text)
        self.assertIn("crypto.subtle", text)
        self.assertNotIn("constructEventAsync", text)
        self.assertNotIn("Deno.core.runMicrotasks", text)
        self.assertNotIn("from \"https://esm.sh/stripe@", text)
        self.assertIn("Stripe-Signature", text)
        self.assertIn("livemode mismatch", text)
        self.assertIn("resolve_app_user_id_by_stripe_customer_v1", text)
        self.assertIn("extractInvoiceCertboundUserId", text)
        self.assertIn("normalizeSupabaseScalarRpcData", text)
        self.assertIn("applyNoncanonicalInvoiceGuard", text)
        self.assertIn("complete_billing_checkout_claim_v1", text)
        self.assertIn("verify_jwt = false", (REPO_ROOT / "supabase" / "config.toml").read_text(encoding="utf-8"))


class TestWebhookState(unittest.TestCase):
    def test_duplicate_event_id_processes_once(self):
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("ON CONFLICT (stripe_event_id) DO NOTHING", sql)
        self.assertIn("duplicate_processed", sql)

    def test_checkout_completion_alone_does_not_grant_premium(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_checkout_2",
            "type": "checkout.session.completed",
            "livemode": False,
            "created": 1_700_000_100,
            "data": {
                "object": {
                    "id": "cs_test_2",
                    "client_reference_id": USER_ID,
                    "customer": CUSTOMER_ID,
                    "subscription": SUBSCRIPTION_ID,
                    "metadata": {STRIPE_METADATA_USER_KEY: USER_ID},
                }
            },
        })
        self.assertFalse(payload["p_update_entitlement"])

    def test_subscription_updated_grants_access(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_sub_1",
            "type": "customer.subscription.updated",
            "livemode": False,
            "created": 1_700_000_200,
            "data": {
                "object": {
                    "id": SUBSCRIPTION_ID,
                    "customer": CUSTOMER_ID,
                    "status": "active",
                    "metadata": {STRIPE_METADATA_USER_KEY: USER_ID},
                    "items": {"data": [{"price": {"id": PRICE_ID}}]},
                    "current_period_end": 1_700_100_000,
                    "cancel_at_period_end": False,
                }
            },
        })
        self.assertTrue(payload["p_update_entitlement"])
        self.assertEqual(payload["p_stripe_subscription_status"], "active")

    def test_subscription_deleted_revokes_access(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_sub_del",
            "type": "customer.subscription.deleted",
            "livemode": False,
            "created": 1_700_000_300,
            "data": {
                "object": {
                    "id": SUBSCRIPTION_ID,
                    "customer": CUSTOMER_ID,
                    "status": "canceled",
                    "metadata": {STRIPE_METADATA_USER_KEY: USER_ID},
                    "items": {"data": [{"price": {"id": PRICE_ID}}]},
                }
            },
        })
        self.assertTrue(payload["p_update_entitlement"])
        self.assertEqual(payload["p_stripe_subscription_status"], "canceled")

    def test_invoice_payment_failed_policy(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_inv_fail",
            "type": "invoice.payment_failed",
            "livemode": False,
            "created": 1_700_000_400,
            "data": {
                "object": {
                    "id": "in_test_1",
                    "customer": CUSTOMER_ID,
                    "subscription": SUBSCRIPTION_ID,
                    "metadata": {STRIPE_METADATA_USER_KEY: USER_ID},
                }
            },
        })
        self.assertEqual(payload["p_stripe_subscription_status"], "past_due")
        self.assertTrue(payload["p_update_entitlement"])

    def test_dispute_and_refund_revoke(self):
        for event_type in ("charge.dispute.created", "charge.refunded"):
            payload = build_rpc_payload_from_event({
                "id": f"evt_{event_type}",
                "type": event_type,
                "livemode": False,
                "created": 1_700_000_500,
                "data": {
                    "object": {
                        "id": "ch_test_1",
                        "customer": CUSTOMER_ID,
                        "metadata": {STRIPE_METADATA_USER_KEY: USER_ID},
                    }
                },
            })
            self.assertTrue(payload["p_revoke_entitlement"])

    def test_supported_event_types(self):
        for event_type in (
            "checkout.session.completed",
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
            "invoice.paid",
            "invoice.payment_failed",
            "charge.dispute.created",
            "charge.refunded",
        ):
            self.assertTrue(should_process_event_type(event_type))

    def test_validate_rpc_payload_requires_user_id(self):
        ok, _ = validate_rpc_payload({"p_stripe_event_id": "evt_1", "p_certbound_user_id": USER_ID})
        self.assertTrue(ok)
        ok, message = validate_rpc_payload({"p_stripe_event_id": "evt_1", "p_certbound_user_id": ""})
        self.assertFalse(ok)
        self.assertIn("certbound user id", message)

    def test_dahlia_invoice_paid_normalizes_without_top_level_subscription(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_inv_paid_dahlia",
            "type": "invoice.paid",
            "livemode": False,
            "created": 1_700_000_450,
            "data": {
                "object": {
                    "id": "in_test_dahlia",
                    "customer": CUSTOMER_ID,
                    "metadata": {},
                    "parent": {
                        "subscription_details": {
                            "subscription": SUBSCRIPTION_ID,
                        }
                    },
                }
            },
        })
        self.assertEqual(payload["p_stripe_subscription_id"], SUBSCRIPTION_ID)
        self.assertEqual(payload["p_stripe_customer_id"], CUSTOMER_ID)
        self.assertTrue(payload["p_update_entitlement"])

    def test_invoice_user_id_from_parent_subscription_details_metadata(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_inv_parent_meta",
            "type": "invoice.paid",
            "livemode": False,
            "created": 1_700_000_451,
            "data": {
                "object": {
                    "id": "in_parent_meta",
                    "customer": CUSTOMER_ID,
                    "metadata": {},
                    "parent": {
                        "subscription_details": {
                            "metadata": {STRIPE_METADATA_USER_KEY: USER_ID},
                            "subscription": SUBSCRIPTION_ID,
                        }
                    },
                }
            },
        })
        self.assertEqual(payload["p_certbound_user_id"], USER_ID)

    def test_invoice_user_id_from_line_item_metadata(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_inv_line_meta",
            "type": "invoice.paid",
            "livemode": False,
            "created": 1_700_000_452,
            "data": {
                "object": {
                    "id": "in_line_meta",
                    "customer": CUSTOMER_ID,
                    "metadata": {},
                    "lines": {
                        "data": [{
                            "metadata": {STRIPE_METADATA_USER_KEY: USER_ID},
                            "parent": {
                                "subscription_item_details": {
                                    "subscription": SUBSCRIPTION_ID,
                                }
                            },
                        }]
                    },
                }
            },
        })
        self.assertEqual(payload["p_certbound_user_id"], USER_ID)
        self.assertEqual(payload["p_stripe_subscription_id"], SUBSCRIPTION_ID)

    def test_scalar_rpc_return_shape_is_normalized(self):
        self.assertEqual(
            normalize_supabase_scalar_rpc_return(DAHLIA_FIXTURE_USER_ID),
            DAHLIA_FIXTURE_USER_ID,
        )
        self.assertEqual(
            normalize_supabase_scalar_rpc_return([DAHLIA_FIXTURE_USER_ID]),
            DAHLIA_FIXTURE_USER_ID,
        )

    def test_invoice_paid_resolves_user_from_persisted_customer_mapping(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_inv_paid_lookup",
            "type": "invoice.paid",
            "livemode": False,
            "created": 1_700_000_460,
            "data": {
                "object": {
                    "id": "in_test_lookup",
                    "customer": CUSTOMER_ID,
                    "metadata": {},
                    "parent": {
                        "subscription_details": {
                            "subscription": SUBSCRIPTION_ID,
                        }
                    },
                }
            },
        })
        prepared, ok, _ = prepare_rpc_payload(
            payload,
            lookup_by_customer=lambda customer_id: USER_ID if customer_id == CUSTOMER_ID else "",
        )
        self.assertTrue(ok)
        self.assertEqual(prepared["p_certbound_user_id"], USER_ID)

    def test_customer_lookup_fallback_uses_scalar_rpc_return(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_inv_scalar_lookup",
            "type": "invoice.paid",
            "livemode": False,
            "created": 1_700_000_461,
            "data": {
                "object": {
                    "id": "in_scalar_lookup",
                    "customer": DAHLIA_FIXTURE_CUSTOMER_ID,
                    "metadata": {},
                }
            },
        })
        user_id, looked_up = resolve_certbound_user_id(
            "",
            DAHLIA_FIXTURE_CUSTOMER_ID,
            lookup_by_customer=lambda _customer_id: DAHLIA_FIXTURE_USER_ID,
        )
        self.assertEqual(user_id, DAHLIA_FIXTURE_USER_ID)
        self.assertTrue(looked_up)
        prepared, ok, _ = prepare_rpc_payload(payload, lookup_by_customer=lambda _customer_id: DAHLIA_FIXTURE_USER_ID)
        self.assertTrue(ok)
        self.assertEqual(prepared["p_certbound_user_id"], DAHLIA_FIXTURE_USER_ID)

    def test_missing_safe_ownership_mapping_fails_closed(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_inv_paid_missing",
            "type": "invoice.paid",
            "livemode": False,
            "created": 1_700_000_470,
            "data": {
                "object": {
                    "id": "in_test_missing",
                    "customer": CUSTOMER_ID,
                    "metadata": {},
                }
            },
        })
        _, ok, message = prepare_rpc_payload(payload, lookup_by_customer=lambda _customer_id: "")
        self.assertFalse(ok)
        self.assertIn("certbound user id", message)

    def test_stale_event_protection_remains_in_migration(self):
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("stale stripe event", sql)
        ordering_sql = ORDERING_MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("stale stripe subscription event", ordering_sql)
        self.assertIn("stripe_last_subscription_event_created_at", ordering_sql)


class TestPortal(unittest.TestCase):
    @patch("utils.billing_stripe._stripe_client")
    @patch("utils.billing_stripe.get_user_profile")
    def test_authenticated_mapped_customer_gets_portal_url(self, mock_profile, mock_stripe_client):
        mock_profile.return_value = _profile(stripe_customer_id=CUSTOMER_ID)
        stripe = MagicMock()
        mock_stripe_client.return_value = stripe
        session = MagicMock()
        session.url = "https://billing.stripe.test/portal_123"
        stripe.billing_portal.Session.create.return_value = session

        url = create_portal_session_url(
            "learner@example.com",
            secrets_getter=_secrets(
                STRIPE_SECRET_KEY="sk_test_x",
                STRIPE_PRICE_ID=PRICE_ID,
                STRIPE_SUCCESS_URL="https://app.example/success",
                STRIPE_CANCEL_URL="https://app.example/cancel",
                STRIPE_PORTAL_RETURN_URL="https://app.example/Account",
            ),
        )
        self.assertEqual(url, "https://billing.stripe.test/portal_123")
        self.assertEqual(
            stripe.billing_portal.Session.create.call_args.kwargs["customer"],
            CUSTOMER_ID,
        )

    @patch("utils.billing_stripe.get_user_profile")
    def test_unmapped_free_user_gets_safe_response(self, mock_profile):
        mock_profile.return_value = _profile()
        with self.assertRaises(BillingActionError) as ctx:
            create_portal_session_url(
                "learner@example.com",
                secrets_getter=_secrets(
                    STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_PRICE_ID=PRICE_ID,
                    STRIPE_SUCCESS_URL="https://app.example/success",
                    STRIPE_CANCEL_URL="https://app.example/cancel",
                    STRIPE_PORTAL_RETURN_URL="https://app.example/Account",
                ),
            )
        self.assertIn("Manage subscription", str(ctx.exception))

    @patch("utils.billing_stripe._stripe_client")
    @patch("utils.billing_stripe.get_user_profile")
    def test_portal_uses_persisted_customer_only(self, mock_profile, mock_stripe_client):
        mock_profile.return_value = _profile(stripe_customer_id=CUSTOMER_ID)
        stripe = MagicMock()
        mock_stripe_client.return_value = stripe
        session = MagicMock()
        session.url = "https://billing.stripe.test/portal_456"
        stripe.billing_portal.Session.create.return_value = session
        create_portal_session_url(
            "learner@example.com",
            secrets_getter=_secrets(
                STRIPE_SECRET_KEY="sk_test_x",
                STRIPE_PRICE_ID=PRICE_ID,
                STRIPE_SUCCESS_URL="https://app.example/success",
                STRIPE_CANCEL_URL="https://app.example/cancel",
                STRIPE_PORTAL_RETURN_URL="https://app.example/Account",
            ),
        )
        self.assertEqual(stripe.billing_portal.Session.create.call_args.kwargs["customer"], CUSTOMER_ID)
        self.assertIn(
            "billing=portal",
            stripe.billing_portal.Session.create.call_args.kwargs["return_url"],
        )
        self.assertNotIn("fr_session", stripe.billing_portal.Session.create.call_args.kwargs["return_url"])


class TestAccountPortalControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = ACCOUNT_PATH.read_text(encoding="utf-8")
        cls.portal_block = cls.text.split("if stripe_customer_id:", 1)[1].split(
            'st.caption("Premium access was granted without a Stripe subscription mapping.")',
            1,
        )[0]

    def test_only_one_manage_subscription_control(self):
        self.assertEqual(self.portal_block.count("render_portal_session_link_markdown"), 1)
        self.assertNotIn('st.button("Manage subscription")', self.text)

    def test_open_stripe_customer_portal_removed(self):
        self.assertNotIn("Open Stripe Customer Portal", self.text)

    def test_portal_url_created_before_native_anchor(self):
        resolve_idx = self.portal_block.index("resolve_portal_session_url")
        render_idx = self.portal_block.index("render_portal_session_link_markdown")
        self.assertLess(resolve_idx, render_idx)

    def test_no_javascript_redirect_or_rerun_remains(self):
        self.assertNotIn("redirect_to_external_url", self.text)
        self.assertNotIn("_billing_portal_redirect", self.text)
        self.assertNotIn("window.top.location", self.text)
        self.assertNotIn("window.open", self.portal_block)
        self.assertNotIn("components.html", self.portal_block)

    def test_logout_clears_cached_portal_session(self):
        self.assertIn("clear_cached_portal_session(st.session_state)", self.text)
        self.assertIn("clear_login_state()", self.text)

    def test_unmapped_paid_user_message_preserved(self):
        self.assertIn(
            "Premium access was granted without a Stripe subscription mapping.",
            self.text,
        )

    @patch("utils.billing_stripe._stripe_client")
    @patch("utils.billing_stripe.get_user_profile")
    def test_portal_errors_are_sanitized(self, mock_profile, mock_stripe_client):
        mock_profile.return_value = None
        with self.assertRaises(BillingActionError) as ctx:
            create_portal_session_url(
                "learner@example.com",
                secrets_getter=_secrets(
                    STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_PRICE_ID=PRICE_ID,
                    STRIPE_SUCCESS_URL="https://app.example/success",
                    STRIPE_CANCEL_URL="https://app.example/cancel",
                    STRIPE_PORTAL_RETURN_URL="https://app.example/Account",
                ),
            )
        self.assertNotIn("sk_test", str(ctx.exception))


class TestPortalNativeLink(unittest.TestCase):
    PORTAL_URL = "https://billing.stripe.com/p/session/test_safe"

    def test_rendered_anchor_uses_target_top(self):
        html = render_portal_session_link_markdown(self.PORTAL_URL)
        self.assertIn('target="_top"', html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn(PORTAL_MANAGE_LABEL, html)

    def test_rendered_url_is_html_escaped(self):
        unsafe = 'https://billing.stripe.com/p/session/test?q="onmouseover=alert(1)"'
        html = render_portal_session_link_markdown(unsafe)
        self.assertNotIn('"onmouseover=alert(1)"', html)
        self.assertIn("&quot;", html)

    def test_validate_accepts_https_stripe_portal_url(self):
        self.assertEqual(
            validate_stripe_portal_url(self.PORTAL_URL),
            self.PORTAL_URL,
        )

    def test_validate_rejects_non_https(self):
        with self.assertRaises(BillingActionError):
            validate_stripe_portal_url("http://billing.stripe.com/p/session/test")

    def test_validate_rejects_non_stripe_host(self):
        with self.assertRaises(BillingActionError):
            validate_stripe_portal_url("https://evil.example.com/p/session/test")

    def test_cache_reused_for_same_user_customer(self):
        state = {}
        now = 1_000_000.0
        cache_portal_session_url(
            app_user_id=USER_ID,
            stripe_customer_id=CUSTOMER_ID,
            url=self.PORTAL_URL,
            session_state=state,
            now=now,
        )
        cached = get_cached_portal_session_url(
            app_user_id=USER_ID,
            stripe_customer_id=CUSTOMER_ID,
            session_state=state,
            now=now + 60,
        )
        self.assertEqual(cached, self.PORTAL_URL)

    def test_cache_regenerated_when_expired(self):
        state = {}
        now = 1_000_000.0
        cache_portal_session_url(
            app_user_id=USER_ID,
            stripe_customer_id=CUSTOMER_ID,
            url=self.PORTAL_URL,
            session_state=state,
            now=now,
            ttl_seconds=PORTAL_SESSION_CACHE_SECONDS,
        )
        cached = get_cached_portal_session_url(
            app_user_id=USER_ID,
            stripe_customer_id=CUSTOMER_ID,
            session_state=state,
            now=now + PORTAL_SESSION_CACHE_SECONDS + 1,
        )
        self.assertIsNone(cached)

    def test_cache_regenerated_when_customer_mismatch(self):
        state = {}
        now = 1_000_000.0
        cache_portal_session_url(
            app_user_id=USER_ID,
            stripe_customer_id=CUSTOMER_ID,
            url=self.PORTAL_URL,
            session_state=state,
            now=now,
        )
        cached = get_cached_portal_session_url(
            app_user_id=USER_ID,
            stripe_customer_id="cus_other",
            session_state=state,
            now=now + 10,
        )
        self.assertIsNone(cached)

    @patch("utils.billing_stripe.create_portal_session_url")
    @patch("utils.billing_stripe.get_user_profile")
    def test_resolve_portal_session_url_creates_once_then_reuses_cache(
        self,
        mock_profile,
        mock_create_portal,
    ):
        mock_profile.return_value = _profile(stripe_customer_id=CUSTOMER_ID)
        mock_create_portal.return_value = self.PORTAL_URL
        state = {}
        now = 2_000_000.0
        first = resolve_portal_session_url(
            "learner@example.com",
            session_state=state,
            now=now,
        )
        second = resolve_portal_session_url(
            "learner@example.com",
            session_state=state,
            now=now + 30,
        )
        self.assertEqual(first, self.PORTAL_URL)
        self.assertEqual(second, self.PORTAL_URL)
        mock_create_portal.assert_called_once()

    def test_clear_cached_portal_session_removes_cache(self):
        state = {}
        cache_portal_session_url(
            app_user_id=USER_ID,
            stripe_customer_id=CUSTOMER_ID,
            url=self.PORTAL_URL,
            session_state=state,
        )
        clear_cached_portal_session(state)
        self.assertNotIn("_billing_portal_session_cache", state)


class TestCompatibility(unittest.TestCase):
    def test_admin_manual_override_preserved(self):
        text = ADMIN_USERS_PATH.read_text(encoding="utf-8")
        self.assertIn("billing_admin_override_at", text)
        self.assertIn("Grant Premium", text)

    def test_account_pending_message_without_url_secrets(self):
        text = ACCOUNT_PATH.read_text(encoding="utf-8")
        self.assertIn("CHECKOUT_PENDING_MESSAGE", text)
        self.assertIn("st.info(CHECKOUT_PENDING_MESSAGE)", text)
        self.assertIn("CHECKOUT_SUCCESS_SIGNIN_MESSAGE", text)
        self.assertNotIn("stripe_customer_id=", text)
        self.assertNotIn("fr_session", text)
        self.assertNotIn("billing=success&", text)

    def test_unauthenticated_success_return_shows_sign_in_guidance(self):
        text = ACCOUNT_PATH.read_text(encoding="utf-8")
        self.assertIn('billing_return == "success"', text)
        self.assertIn("CHECKOUT_SUCCESS_SIGNIN_MESSAGE", text)
        self.assertIn("Your payment succeeded.", text)

    def test_no_real_credentials_in_repo_source(self):
        patterns = ("sk_live_", "whsec_", "rk_live_", "sk_test_51")
        for path in (
            REPO_ROOT / "utils",
            REPO_ROOT / "pages" / "Account.py",
            REPO_ROOT / "supabase" / "functions" / "stripe-webhook",
        ):
            files = [path] if path.is_file() else list(path.rglob("*"))
            for file_path in files:
                if file_path.suffix not in {".py", ".ts", ".sql", ".md", ".example", ".toml"}:
                    continue
                text = file_path.read_text(encoding="utf-8")
                for pattern in patterns:
                    if pattern in text and "example" not in str(file_path).lower():
                        if pattern == "sk_test_..." or "sk_test_x" in text:
                            continue
                        self.assertNotIn(pattern, text, msg=str(file_path))

    def test_blocking_subscription_helper(self):
        self.assertTrue(user_has_blocking_stripe_subscription(_profile(
            stripe_subscription_id=SUBSCRIPTION_ID,
            stripe_subscription_status="active",
        )))
        self.assertFalse(user_has_blocking_stripe_subscription(_profile()))

    def test_problem_subscription_states_block_checkout(self):
        for status in ("active", "trialing", "past_due", "unpaid", "incomplete", "paused"):
            self.assertTrue(stripe_status_blocks_new_checkout(status))
            self.assertTrue(customer_subscriptions_block_checkout([{"status": status}]))


class TestCheckoutClaimsMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = CLAIMS_MIGRATION_PATH.read_text(encoding="utf-8")

    def test_claim_table_and_rpcs_exist(self):
        self.assertIn("billing_checkout_claims", self.sql)
        self.assertIn("claim_billing_checkout_v1", self.sql)
        self.assertIn("release_billing_checkout_claim_v1", self.sql)
        self.assertIn("complete_billing_checkout_claim_v1", self.sql)
        self.assertIn("resolve_app_user_id_by_stripe_customer_v1", self.sql)

    def test_checkout_idempotency_key_is_deterministic_per_window(self):
        now = 1_700_000_000.0
        first = checkout_idempotency_key(USER_ID, now=now)
        second = checkout_idempotency_key(USER_ID, now=now + 60)
        third = checkout_idempotency_key(USER_ID, now=now + CHECKOUT_WINDOW_SECONDS)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_pending_claim_release_helper(self):
        admin = MagicMock()
        admin.rpc.return_value.execute.return_value = MagicMock(data=1)
        release_checkout_claim(app_user_id=USER_ID, admin_client=admin)
        admin.rpc.assert_called_with(
            "release_billing_checkout_claim_v1",
            {"p_app_user_id": USER_ID},
        )

    @patch("utils.billing_stripe.get_user_profile")
    @patch("utils.billing_stripe.release_checkout_claim")
    def test_cancel_return_releases_pending_claim(self, mock_release, mock_profile):
        mock_profile.return_value = _profile()
        release_pending_checkout_claim("learner@example.com")
        mock_release.assert_called_once_with(app_user_id=USER_ID)


class TestWebhookApiVersion(unittest.TestCase):
    def test_webhook_api_version_constant_matches_stripe_destination(self):
        self.assertEqual(STRIPE_WEBHOOK_API_VERSION, "2026-06-24.dahlia")

    def test_extract_invoice_subscription_from_line_item_parent(self):
        invoice = {
            "lines": {
                "data": [{
                    "parent": {
                        "subscription_item_details": {
                            "subscription": SUBSCRIPTION_ID,
                        }
                    }
                }]
            }
        }
        self.assertEqual(extract_invoice_subscription_id(invoice), SUBSCRIPTION_ID)

    def test_resolve_certbound_user_id_prefers_metadata(self):
        user_id, looked_up = resolve_certbound_user_id(
            USER_ID,
            CUSTOMER_ID,
            lookup_by_customer=lambda _customer_id: "other-user",
        )
        self.assertEqual(user_id, USER_ID)
        self.assertFalse(looked_up)


class TestDahliaInvoiceProductionFixture(unittest.TestCase):
    def test_production_fixture_resolves_user_and_subscription(self):
        payload = build_rpc_payload_from_event(DAHLIA_INVOICE_PAID_FIXTURE_EVENT)
        self.assertEqual(payload["p_certbound_user_id"], DAHLIA_FIXTURE_USER_ID)
        self.assertEqual(payload["p_stripe_customer_id"], DAHLIA_FIXTURE_CUSTOMER_ID)
        self.assertEqual(payload["p_stripe_subscription_id"], DAHLIA_FIXTURE_DUPLICATE_SUBSCRIPTION_ID)
        prepared, ok, message = prepare_rpc_payload(payload)
        self.assertTrue(ok, msg=message)
        self.assertEqual(prepared["p_certbound_user_id"], DAHLIA_FIXTURE_USER_ID)

    def test_production_fixture_extract_helpers_match_event(self):
        invoice = DAHLIA_INVOICE_PAID_FIXTURE_EVENT["data"]["object"]
        self.assertEqual(extract_invoice_certbound_user_id(invoice), DAHLIA_FIXTURE_USER_ID)
        self.assertEqual(extract_invoice_subscription_id(invoice), DAHLIA_FIXTURE_DUPLICATE_SUBSCRIPTION_ID)

    def test_canonical_matching_invoice_processes_normally(self):
        payload = build_rpc_payload_from_event(DAHLIA_INVOICE_PAID_FIXTURE_EVENT)
        payload["p_stripe_subscription_id"] = DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID
        prepared, ok, _ = prepare_rpc_payload(
            payload,
            canonical_subscription_id=DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID,
            canonical_subscription_status="active",
        )
        self.assertTrue(ok)
        self.assertTrue(prepared["p_update_entitlement"])
        self.assertEqual(prepared["p_stripe_subscription_id"], DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID)

    def test_duplicate_subscription_invoice_is_ignored_without_entitlement_change(self):
        payload = build_rpc_payload_from_event(DAHLIA_INVOICE_PAID_FIXTURE_EVENT)
        prepared, ok, _ = prepare_rpc_payload(
            payload,
            canonical_subscription_id=DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID,
            canonical_subscription_status="active",
        )
        self.assertTrue(ok)
        self.assertFalse(prepared["p_update_entitlement"])
        self.assertEqual(prepared["p_stripe_subscription_id"], "")
        self.assertEqual(prepared["p_stripe_subscription_status"], "")

    def test_duplicate_invoice_does_not_replace_canonical_subscription_id(self):
        payload = build_rpc_payload_from_event(DAHLIA_INVOICE_PAID_FIXTURE_EVENT)
        guarded, ignored = apply_noncanonical_invoice_guard(
            payload,
            canonical_subscription_id=DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID,
            canonical_subscription_status="active",
        )
        self.assertTrue(ignored)
        self.assertEqual(guarded["p_stripe_subscription_id"], "")
        self.assertFalse(guarded["p_update_entitlement"])

    def test_should_ignore_noncanonical_invoice_only_when_canonical_is_established(self):
        self.assertTrue(should_ignore_noncanonical_invoice_event(
            invoice_subscription_id=DAHLIA_FIXTURE_DUPLICATE_SUBSCRIPTION_ID,
            canonical_subscription_id=DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID,
            canonical_subscription_status="active",
        ))
        self.assertFalse(should_ignore_noncanonical_invoice_event(
            invoice_subscription_id=DAHLIA_FIXTURE_DUPLICATE_SUBSCRIPTION_ID,
            canonical_subscription_id=DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID,
            canonical_subscription_status="canceled",
        ))
        self.assertFalse(should_ignore_noncanonical_invoice_event(
            invoice_subscription_id=DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID,
            canonical_subscription_id=DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID,
            canonical_subscription_status="active",
        ))


if __name__ == "__main__":
    unittest.main()
