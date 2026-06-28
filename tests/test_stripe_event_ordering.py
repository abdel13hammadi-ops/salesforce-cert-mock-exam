"""Production-shaped Stripe subscription lifecycle ordering tests."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.billing_event_ordering import (
    SUBSCRIPTION_LIFECYCLE_EVENT_TYPES,
    admin_override_blocks_entitlement,
    apply_billing_event_to_user_state,
    apply_billing_events_in_delivery_order,
    is_checkout_event,
    should_skip_stale_subscription_lifecycle_event,
    subscription_lifecycle_restores_automated_billing,
)
from utils.billing_mapping import certbound_status_grants_premium
from utils.billing_webhook import build_rpc_payload_from_event

REPO_ROOT = Path(__file__).resolve().parents[1]
ORDERING_MIGRATION_PATH = (
    REPO_ROOT / "supabase" / "migrations" / "20260628180000_v46_stripe_subscription_event_ordering.sql"
)
FOUNDATION_MIGRATION_PATH = (
    REPO_ROOT / "supabase" / "migrations" / "20260625000000_v46_stripe_billing_foundation.sql"
)

USER_ID = "222b6c70-3fe0-4c1d-8417-514b0761bd8d"
CUSTOMER_ID = "cus_UmxEY0tEtdonxS"
SUBSCRIPTION_ID = "sub_1TnNLk6AZ0kHudAmVQo2g8OS"
PRICE_ID = "price_test_certbound2"


def _base_user(**overrides):
    row = {
        "id": USER_ID,
        "email": "certbound2@gmail.com",
        "subscription_status": "free",
        "stripe_customer_id": "",
        "stripe_subscription_id": "",
        "stripe_subscription_status": "",
        "stripe_last_subscription_event_created_at": None,
        "stripe_last_event_created_at": None,
        "billing_admin_override_at": None,
    }
    row.update(overrides)
    return row


class TestOrderingMigrationShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = ORDERING_MIGRATION_PATH.read_text(encoding="utf-8")

    def test_adds_subscription_lifecycle_watermark_column(self):
        self.assertIn("stripe_last_subscription_event_created_at", self.sql)
        self.assertIn("ADD COLUMN stripe_last_subscription_event_created_at", self.sql)

    def test_subscription_stale_uses_dedicated_watermark(self):
        self.assertIn("stale stripe subscription event", self.sql)
        self.assertIn("stripe_last_subscription_event_created_at = p_event_created_at", self.sql)

    def test_checkout_does_not_advance_subscription_watermark(self):
        checkout_block = self.sql.split("IF v_is_checkout THEN", 1)[1].split("ELSIF v_is_subscription_lifecycle", 1)[0]
        self.assertNotIn("stripe_last_subscription_event_created_at", checkout_block)
        self.assertNotIn("stripe_subscription_status", checkout_block)

    def test_invoice_does_not_advance_subscription_watermark(self):
        invoice_block = self.sql.split("ELSIF v_is_invoice OR v_is_revocation THEN", 1)[1].split("ELSE", 1)[0]
        self.assertNotIn("stripe_last_subscription_event_created_at", invoice_block)
        self.assertNotIn("stripe_subscription_status", invoice_block)

    def test_newer_subscription_event_clears_admin_override(self):
        self.assertIn("billing_admin_override_at = CASE", self.sql)
        self.assertIn("THEN NULL", self.sql)

    def test_foundation_migration_left_unmodified(self):
        foundation = FOUNDATION_MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("stale stripe event", foundation)
        self.assertNotIn("stripe_last_subscription_event_created_at", foundation)


class TestProductionCertbound2Ordering(unittest.TestCase):
    def test_subscription_created_incomplete_processes(self):
        user = _base_user()
        outcome = apply_billing_event_to_user_state(
            user,
            event_type="customer.subscription.created",
            event_created_at="2026-06-28T18:22:38Z",
            stripe_customer_id=CUSTOMER_ID,
            stripe_subscription_id=SUBSCRIPTION_ID,
            stripe_subscription_status="incomplete",
            stripe_price_id=PRICE_ID,
            update_entitlement=True,
        )
        self.assertEqual(outcome, "processed")
        self.assertEqual(user["stripe_subscription_status"], "incomplete")
        self.assertEqual(user["subscription_status"], "expired")
        self.assertEqual(user["stripe_last_subscription_event_created_at"], "2026-06-28T18:22:38Z")

    def test_checkout_does_not_advance_subscription_watermark(self):
        user = _base_user(
            stripe_last_subscription_event_created_at="2026-06-28T18:22:38Z",
            stripe_subscription_status="incomplete",
            subscription_status="expired",
        )
        apply_billing_event_to_user_state(
            user,
            event_type="checkout.session.completed",
            event_created_at="2026-06-28T18:22:41Z",
            stripe_customer_id=CUSTOMER_ID,
            stripe_subscription_id=SUBSCRIPTION_ID,
            update_entitlement=False,
        )
        self.assertEqual(user["stripe_last_subscription_event_created_at"], "2026-06-28T18:22:38Z")
        self.assertEqual(user["stripe_subscription_status"], "incomplete")
        self.assertEqual(user["subscription_status"], "expired")

    def test_subscription_updated_active_still_processes_after_checkout(self):
        user = _base_user()
        events = [
            {
                "event_type": "customer.subscription.created",
                "event_created_at": "2026-06-28T18:22:38Z",
                "stripe_customer_id": CUSTOMER_ID,
                "stripe_subscription_id": SUBSCRIPTION_ID,
                "stripe_subscription_status": "incomplete",
                "stripe_price_id": PRICE_ID,
            },
            {
                "event_type": "checkout.session.completed",
                "event_created_at": "2026-06-28T18:22:41Z",
                "stripe_customer_id": CUSTOMER_ID,
                "stripe_subscription_id": SUBSCRIPTION_ID,
                "update_entitlement": False,
            },
            {
                "event_type": "customer.subscription.updated",
                "event_created_at": "2026-06-28T18:22:40Z",
                "stripe_customer_id": CUSTOMER_ID,
                "stripe_subscription_id": SUBSCRIPTION_ID,
                "stripe_subscription_status": "active",
                "stripe_price_id": PRICE_ID,
            },
        ]
        final = apply_billing_events_in_delivery_order(user, events)
        self.assertEqual(final["subscription_status"], "active")
        self.assertEqual(final["stripe_subscription_status"], "active")
        self.assertTrue(certbound_status_grants_premium(final["subscription_status"]))
        self.assertEqual(final["stripe_last_subscription_event_created_at"], "2026-06-28T18:22:40Z")

    def test_full_production_delivery_order_restores_paid_user(self):
        user = _base_user(billing_admin_override_at="2026-06-28T18:19:45Z")
        events = [
            {
                "event_type": "customer.subscription.created",
                "event_created_at": "2026-06-28T18:22:38Z",
                "stripe_customer_id": CUSTOMER_ID,
                "stripe_subscription_id": SUBSCRIPTION_ID,
                "stripe_subscription_status": "incomplete",
                "stripe_price_id": PRICE_ID,
            },
            {
                "event_type": "customer.subscription.updated",
                "event_created_at": "2026-06-28T18:22:40Z",
                "stripe_customer_id": CUSTOMER_ID,
                "stripe_subscription_id": SUBSCRIPTION_ID,
                "stripe_subscription_status": "active",
                "stripe_price_id": PRICE_ID,
            },
            {
                "event_type": "invoice.paid",
                "event_created_at": "2026-06-28T18:22:40Z",
                "stripe_customer_id": CUSTOMER_ID,
                "stripe_subscription_id": SUBSCRIPTION_ID,
                "stripe_subscription_status": "active",
            },
            {
                "event_type": "checkout.session.completed",
                "event_created_at": "2026-06-28T18:22:41Z",
                "stripe_customer_id": CUSTOMER_ID,
                "stripe_subscription_id": SUBSCRIPTION_ID,
                "update_entitlement": False,
            },
        ]
        final = apply_billing_events_in_delivery_order(user, events)
        self.assertEqual(final["subscription_status"], "active")
        self.assertEqual(final["stripe_subscription_status"], "active")
        self.assertIsNone(final["billing_admin_override_at"])

    def test_invoice_does_not_make_subscription_event_stale(self):
        user = _base_user(
            stripe_last_subscription_event_created_at="2026-06-28T18:22:38Z",
            stripe_subscription_status="incomplete",
            subscription_status="expired",
        )
        apply_billing_event_to_user_state(
            user,
            event_type="invoice.paid",
            event_created_at="2026-06-28T18:22:40Z",
            stripe_customer_id=CUSTOMER_ID,
            stripe_subscription_id=SUBSCRIPTION_ID,
            stripe_subscription_status="active",
        )
        self.assertEqual(user["stripe_last_subscription_event_created_at"], "2026-06-28T18:22:38Z")
        outcome = apply_billing_event_to_user_state(
            user,
            event_type="customer.subscription.updated",
            event_created_at="2026-06-28T18:22:40Z",
            stripe_customer_id=CUSTOMER_ID,
            stripe_subscription_id=SUBSCRIPTION_ID,
            stripe_subscription_status="active",
            stripe_price_id=PRICE_ID,
        )
        self.assertEqual(outcome, "processed")
        self.assertEqual(user["stripe_subscription_status"], "active")

    def test_stale_subscription_lifecycle_event_still_rejected(self):
        self.assertTrue(
            should_skip_stale_subscription_lifecycle_event(
                event_type="customer.subscription.updated",
                event_created_at="2026-06-28T18:22:38Z",
                last_subscription_event_created_at="2026-06-28T18:22:40Z",
            )
        )

    def test_checkout_payload_does_not_grant_premium(self):
        payload = build_rpc_payload_from_event({
            "id": "evt_checkout_certbound2",
            "type": "checkout.session.completed",
            "livemode": False,
            "created": 1_781_640_161,
            "data": {
                "object": {
                    "id": "cs_test_certbound2",
                    "client_reference_id": USER_ID,
                    "customer": CUSTOMER_ID,
                    "subscription": SUBSCRIPTION_ID,
                }
            },
        })
        self.assertFalse(payload["p_update_entitlement"])
        self.assertEqual(payload["p_stripe_subscription_status"], "")


class TestAdminOverrideOrdering(unittest.TestCase):
    def test_older_subscription_event_does_not_override_admin_decision(self):
        user = _base_user(billing_admin_override_at="2026-06-28T18:30:00Z", subscription_status="expired")
        apply_billing_event_to_user_state(
            user,
            event_type="customer.subscription.updated",
            event_created_at="2026-06-28T18:29:00Z",
            stripe_customer_id=CUSTOMER_ID,
            stripe_subscription_id=SUBSCRIPTION_ID,
            stripe_subscription_status="active",
            stripe_price_id=PRICE_ID,
        )
        self.assertEqual(user["subscription_status"], "expired")
        self.assertIsNotNone(user["billing_admin_override_at"])

    def test_newer_subscription_event_restores_automated_entitlement(self):
        user = _base_user(billing_admin_override_at="2026-06-28T18:19:45Z", subscription_status="expired")
        apply_billing_event_to_user_state(
            user,
            event_type="customer.subscription.updated",
            event_created_at="2026-06-28T18:22:40Z",
            stripe_customer_id=CUSTOMER_ID,
            stripe_subscription_id=SUBSCRIPTION_ID,
            stripe_subscription_status="active",
            stripe_price_id=PRICE_ID,
        )
        self.assertEqual(user["subscription_status"], "active")
        self.assertIsNone(user["billing_admin_override_at"])
        self.assertTrue(subscription_lifecycle_restores_automated_billing(
            event_type="customer.subscription.updated",
            event_created_at="2026-06-28T18:22:40Z",
            billing_admin_override_at="2026-06-28T18:19:45Z",
        ))


class TestOrderingPolicyHelpers(unittest.TestCase):
    def test_subscription_lifecycle_event_types(self):
        for event_type in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            self.assertIn(event_type, SUBSCRIPTION_LIFECYCLE_EVENT_TYPES)

    def test_checkout_is_not_subscription_lifecycle(self):
        self.assertTrue(is_checkout_event("checkout.session.completed"))

    def test_admin_override_blocks_only_older_or_equal_events(self):
        self.assertTrue(admin_override_blocks_entitlement(
            event_created_at="2026-06-28T18:19:45Z",
            billing_admin_override_at="2026-06-28T18:19:45Z",
        ))
        self.assertFalse(admin_override_blocks_entitlement(
            event_created_at="2026-06-28T18:22:40Z",
            billing_admin_override_at="2026-06-28T18:19:45Z",
        ))


if __name__ == "__main__":
    unittest.main()
