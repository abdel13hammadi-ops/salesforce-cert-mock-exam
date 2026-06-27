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
    expected_livemode,
    livemode_matches_config,
)
from utils.billing_mapping import (
    certbound_status_grants_premium,
    map_stripe_subscription_status_to_certbound,
    stripe_status_grants_premium,
    user_has_blocking_stripe_subscription,
)
from utils.billing_stripe import (
    BillingActionError,
    STRIPE_METADATA_USER_KEY,
    create_checkout_session_url,
    create_portal_session_url,
)
from utils.billing_webhook import (
    WebhookVerificationError,
    build_rpc_payload_from_event,
    should_process_event_type,
    validate_rpc_payload,
    verify_stripe_signature,
)
from utils.access_control import PAID_STATUS_VALUES

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO_ROOT / "supabase" / "migrations" / "20260625000000_v46_stripe_billing_foundation.sql"
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

    @patch("utils.billing_stripe._stripe_client")
    @patch("utils.billing_stripe.get_user_profile")
    def test_checkout_uses_server_price_and_metadata(self, mock_profile, mock_stripe_client):
        mock_profile.return_value = _profile()
        stripe = MagicMock()
        mock_stripe_client.return_value = stripe
        customer = MagicMock()
        customer.id = CUSTOMER_ID
        stripe.Customer.create.return_value = customer
        session = MagicMock()
        session.url = "https://checkout.stripe.test/session_123"
        stripe.checkout.Session.create.return_value = session

        with patch("utils.access_control.get_supabase_admin_client") as mock_admin:
            mock_admin.return_value.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()
            url = create_checkout_session_url(
                "learner@example.com",
                secrets_getter=_secrets(
                    STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_PRICE_ID=PRICE_ID,
                    STRIPE_SUCCESS_URL="https://app.example/Account?billing=success",
                    STRIPE_CANCEL_URL="https://app.example/Account?billing=cancel",
                ),
            )

        self.assertEqual(url, "https://checkout.stripe.test/session_123")
        kwargs = stripe.checkout.Session.create.call_args.kwargs
        self.assertEqual(kwargs["line_items"][0]["price"], PRICE_ID)
        self.assertEqual(kwargs["client_reference_id"], USER_ID)
        self.assertEqual(kwargs["metadata"][STRIPE_METADATA_USER_KEY], USER_ID)
        self.assertEqual(kwargs["subscription_data"]["metadata"][STRIPE_METADATA_USER_KEY], USER_ID)
        self.assertTrue(kwargs["idempotency_key"].startswith(f"certbound-checkout-{USER_ID}-"))

    @patch("utils.billing_stripe.get_user_profile")
    def test_existing_customer_is_reused(self, mock_profile):
        mock_profile.return_value = _profile(stripe_customer_id=CUSTOMER_ID)
        with patch("utils.billing_stripe._stripe_client") as mock_stripe_client:
            stripe = MagicMock()
            mock_stripe_client.return_value = stripe
            session = MagicMock()
            session.url = "https://checkout.stripe.test/session_456"
            stripe.checkout.Session.create.return_value = session
            create_checkout_session_url(
                "learner@example.com",
                secrets_getter=_secrets(
                    STRIPE_SECRET_KEY="sk_test_x",
                    STRIPE_PRICE_ID=PRICE_ID,
                    STRIPE_SUCCESS_URL="https://app.example/success",
                    STRIPE_CANCEL_URL="https://app.example/cancel",
                ),
            )
            stripe.Customer.create.assert_not_called()
            self.assertEqual(stripe.checkout.Session.create.call_args.kwargs["customer"], CUSTOMER_ID)

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
        self.assertIn("Manage subscription", text)


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
        self.assertIn("constructEventAsync", text)
        self.assertIn("Stripe-Signature", text)
        self.assertIn("livemode mismatch", text)
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


class TestCompatibility(unittest.TestCase):
    def test_admin_manual_override_preserved(self):
        text = ADMIN_USERS_PATH.read_text(encoding="utf-8")
        self.assertIn("billing_admin_override_at", text)
        self.assertIn("Grant Premium", text)

    def test_account_pending_message_without_url_secrets(self):
        text = ACCOUNT_PATH.read_text(encoding="utf-8")
        self.assertIn("CHECKOUT_PENDING_MESSAGE", text)
        self.assertIn("st.info(CHECKOUT_PENDING_MESSAGE)", text)
        self.assertNotIn("stripe_customer_id=", text)

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


if __name__ == "__main__":
    unittest.main()
