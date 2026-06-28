"""Sanitized Dahlia invoice.paid fixture matching production payload shape (not real IDs)."""

from __future__ import annotations

DAHLIA_FIXTURE_USER_ID = "3157770c-4260-497a-8bbb-d37023a874"
DAHLIA_FIXTURE_CUSTOMER_ID = "cus_test_dahlia_fixture"
DAHLIA_FIXTURE_DUPLICATE_SUBSCRIPTION_ID = "sub_test_dahlia_duplicate"
DAHLIA_FIXTURE_CANONICAL_SUBSCRIPTION_ID = "sub_test_dahlia_canonical"

DAHLIA_INVOICE_PAID_FIXTURE_EVENT = {
    "id": "evt_test_dahlia_invoice_paid",
    "object": "event",
    "api_version": "2026-06-24.dahlia",
    "type": "invoice.paid",
    "livemode": False,
    "created": 1_751_000_000,
    "data": {
        "object": {
            "id": "in_test_dahlia_fixture",
            "object": "invoice",
            "customer": DAHLIA_FIXTURE_CUSTOMER_ID,
            "metadata": {},
            "parent": {
                "type": "subscription_details",
                "subscription_details": {
                    "metadata": {
                        "certbound_user_id": DAHLIA_FIXTURE_USER_ID,
                    },
                    "subscription": DAHLIA_FIXTURE_DUPLICATE_SUBSCRIPTION_ID,
                },
            },
            "lines": {
                "object": "list",
                "data": [
                    {
                        "id": "il_test_dahlia_fixture",
                        "object": "line_item",
                        "metadata": {
                            "certbound_user_id": DAHLIA_FIXTURE_USER_ID,
                        },
                        "parent": {
                            "type": "subscription_item_details",
                            "subscription_item_details": {
                                "subscription": DAHLIA_FIXTURE_DUPLICATE_SUBSCRIPTION_ID,
                            },
                        },
                    }
                ],
            },
        }
    },
}
