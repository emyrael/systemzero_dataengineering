"""Behaviour of each schema's key rules, checked through the validator's record outcome."""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.validation.schemas import SCHEMAS
from src.validation.validator import RecordOutcome, validate_record

CONTEXT = {"as_of": date(2025, 1, 1), "payment_terms_days": {"NET30": 30}}


def run(entity: str, record: dict, **overrides) -> RecordOutcome:
    return validate_record(entity, SCHEMAS[entity], {**record, **overrides}, CONTEXT)


def rejected_by(outcome: RecordOutcome) -> set[str]:
    return {r["rule"] for r in outcome.rejections}


PRODUCT = {
    "product_id": "PRD-000001", "sku": "SKU-1", "product_name": "Compact Speaker",
    "description": "Nice", "category": "Electronics", "subcategory": "Audio", "vendor_id": "VND-001",
    "price": "148.13", "cost": "64.6", "currency": "USD", "weight_kg": "2.4", "stock_quantity": "10",
    "reorder_level": "5", "rating": "4.5", "review_count": "3", "is_active": "True",
    "created_date": "2023-08-23", "last_updated": "2024-02-11T00:00:00", "tags": "a,b",
}

CUSTOMER = {
    "customer_id": "CUS-00000001", "first_name": "Fatima", "last_name": "Rodriguez",
    "full_name": "Fatima Rodriguez", "email": "fatima@example.com", "phone": "(563) 144-7607",
    "gender": "Female", "age": 40, "date_of_birth": "1984-06-01",
    "address": {"city": "Berlin", "country": "Germany"}, "segment": "Gold", "total_orders": 3,
    "total_spend": 100.0, "average_order_value": 33.3, "registration_date": "2021-05-10",
    "last_purchase_date": "2024-07-19", "is_active": True, "email_verified": True,
    "phone_verified": None, "preferences": {"preferred_currency": "EUR"},
    "metadata": {"created_at": "2021-05-10T00:00:00"},
}

TRANSACTION = {
    "transaction_id": "TXN-CB40A5B1", "order_id": "ORD-1", "customer_id": "CUS-00000001",
    "product_id": "PRD-000001", "transaction_date": "2023-11-03",
    "transaction_timestamp": "2023-11-03T00:00:00", "quantity": 2, "unit_price": 10.0,
    "subtotal": 20.0, "discount_percent": 10.0, "discount_amount": 2.0, "tax_rate": 0.1,
    "tax_amount": 1.8, "shipping_cost": 5.0, "total_amount": 24.8, "currency": "USD",
    "payment_method": "paypal", "payment_status": "paid", "order_status": "completed",
    "shipping_method": "standard", "channel": "web", "region": "Europe", "is_gift": "True", "notes": None,
}

INVOICE = {
    "invoice_id": "INV-C58F93E9", "invoice_number": "INV-TECH-2024-00001", "vendor_id": "VND-001",
    "vendor_name": "TechFlow", "invoice_date": "2024-06-09", "due_date": "2024-06-09",
    "payment_terms": "NET30", "po_number": "PO-1", "line_item_count": "1",
    "line_items_json": json.dumps([{"line_number": 1, "product_id": "PRD-000001", "quantity": 2,
                                    "unit_cost": 50.0, "line_total": 100.0}]),
    "subtotal": "100.0", "tax_rate": "0.1", "tax_amount": "10.0", "shipping_handling": "5.0",
    "total_amount": "115.0", "currency": "USD", "payment_status": "paid", "amount_paid": "115.0",
    "balance_due": "0.0", "payment_date": "2024-07-01", "payment_method": "ach",
    "approved_by": "", "notes": "", "created_at": "2024-06-09T00:00:00", "updated_at": "",
}


class TestProduct:
    def test_clean_record_passes_untouched(self):
        outcome = run("products", PRODUCT)
        assert outcome.row is not None and outcome.flags == [] and outcome.fixes == []

    def test_bad_id_is_quarantined(self):
        assert rejected_by(run("products", PRODUCT, product_id="X-1")) == {"products.invalid_id"}

    def test_negative_price_is_nulled_and_flagged(self):
        outcome = run("products", PRODUCT, price="-888.29")
        assert outcome.row["price"] is None and "products.invalid_price" in outcome.flags

    def test_representations_are_standardised(self):
        outcome = run("products", PRODUCT, product_name="SMART SPEAKER", currency="US$",
                      is_active="yes", created_date="23-Aug-2023")
        row = outcome.row
        assert (row["product_name"], row["currency"], row["is_active"], row["created_date"]) == (
            "Smart Speaker", "USD", True, date(2023, 8, 23))
        assert set(outcome.fixes) == {"products.name_case", "products.currency", "products.boolean",
                                      "products.date_format"}

    def test_out_of_range_rating_and_missing_category(self):
        outcome = run("products", PRODUCT, rating="5.5", category="")
        assert outcome.row["rating"] is None and outcome.row["category"] is None
        assert {"products.invalid_rating", "products.missing_category"} <= set(outcome.flags)

    def test_unexpected_garbage_fails_closed(self):
        outcome = run("products", PRODUCT, stock_quantity="lots")
        assert outcome.row is None
        assert rejected_by(outcome) == {"products.schema:stock_quantity:value_error"}


class TestCustomer:
    def test_flattens_nested_objects(self):
        row = run("customers", CUSTOMER).row
        assert row["address_city"] == "Berlin" and row["preferred_currency"] == "EUR"
        assert row["phone"] == "+15631447607" and row["gender"] == "female"

    def test_invalid_email_is_nulled_but_customer_kept(self):
        outcome = run("customers", CUSTOMER, email="JessicaJohnson")
        assert outcome.row["email"] is None and "customers.invalid_email" in outcome.flags

    def test_invalid_age_is_derived_from_birth_date(self):
        outcome = run("customers", CUSTOMER, age=150)
        assert outcome.row["age"] == 40 and "customers.age_from_dob" in outcome.fixes

    def test_metadata_created_at_overrides_epoch_registration(self):
        outcome = run("customers", CUSTOMER, registration_date="1620597600",
                      metadata={"created_at": "2021-05-10T00:00:00"})
        assert outcome.row["registration_date"] == date(2021, 5, 10)

    def test_future_dates_relative_to_as_of_are_nulled(self):
        outcome = run("customers", CUSTOMER, last_purchase_date="2026-07-19")
        assert outcome.row["last_purchase_date"] is None
        assert "customers.future_last_purchase" in outcome.flags

    def test_segment_and_gender_variants(self):
        row = run("customers", CUSTOMER, segment="PLATINUM", gender="M").row
        assert (row["segment"], row["gender"]) == ("platinum", "male")
        assert run("customers", CUSTOMER, gender="").row["gender"] == "unknown"


class TestTransaction:
    def test_clean_sale(self):
        outcome = run("transactions", TRANSACTION)
        assert outcome.row["is_return"] is False and outcome.flags == []

    def test_zero_quantity_is_quarantined(self):
        assert rejected_by(run("transactions", TRANSACTION, quantity=0)) == {"transactions.zero_quantity"}

    def test_return_amounts_follow_the_sign_convention(self):
        outcome = run("transactions", TRANSACTION, quantity=-2, shipping_cost=-5.0, total_amount=-24.8)
        assert outcome.row["is_return"] is True
        assert "transactions.amount_mismatch" not in outcome.flags

    def test_amount_mismatch_is_flagged(self):
        assert "transactions.amount_mismatch" in run("transactions", TRANSACTION, total_amount=99.0).flags

    def test_junk_notes_are_dropped(self):
        outcome = run("transactions", TRANSACTION, notes="Special instructions: " + "x" * 300)
        assert outcome.row["notes"] is None and "transactions.junk_notes" in outcome.flags


class TestInvoice:
    def test_due_date_is_recomputed_from_terms(self):
        outcome = run("invoices", INVOICE)
        assert outcome.row["due_date"] == date(2024, 7, 9)
        assert outcome.row["src_due_date"] == date(2024, 6, 9)
        assert "invoices.due_date_from_terms" in outcome.fixes

    def test_arithmetic_and_payment_checks(self):
        outcome = run("invoices", INVOICE, total_amount="120.0", balance_due="5.0", payment_date="2024-06-01")
        assert {"invoices.total_mismatch", "invoices.paid_before_invoiced"} <= set(outcome.flags)

    def test_unparseable_line_items_are_flagged(self):
        assert "invoices.unparseable_line_items" in run("invoices", INVOICE, line_items_json="{oops").flags

    def test_unparseable_invoice_date_is_quarantined(self):
        assert rejected_by(run("invoices", INVOICE, invoice_date="soon")) == {"invoices.invalid_date"}

    def test_line_without_product_is_kept_and_flagged(self):
        line = {"invoice_id": "INV-C58F93E9", "line_number": 3, "product_id": None,
                "quantity": -2, "unit_cost": 10.0, "line_total": -20.0}
        outcome = run("invoice_lines", line)
        assert outcome.row["is_credit"] is True
        assert outcome.flags == ["invoice_lines.missing_product"]


class TestSupportSources:
    TICKET = {"ticket_id": "TKT-000009", "customer_id": "CUS-00000001", "product_id": "PRD-000001",
              "channel": "CHAT", "category": "product_issue", "priority": "HIGH", "status": "Resolved",
              "created_at": "20-03-2025", "resolved_at": "2025-03-21T10:00:00", "agent_id": "AGT-1",
              "satisfaction_score": 6, "transcript": "Agent: hi", "summary": "s", "tags": ["x"],
              "resolution_type": "replacement"}

    def test_ticket_normalisation_and_score_range(self):
        outcome = run("support_tickets", self.TICKET)
        row = outcome.row
        assert (row["channel"], row["status"], row["satisfaction_score"]) == ("chat", "resolved", None)
        assert "support_tickets.invalid_satisfaction" in outcome.flags

    def test_ticket_without_id_is_quarantined(self):
        assert rejected_by(run("support_tickets", self.TICKET, ticket_id="")) == {"support_tickets.invalid_id"}

    def test_review_rating_rules(self):
        review = {"review_id": "REV-000001", "product_id": "PRD-INVALID", "customer_id": "CUS-00000045",
                  "rating": -1, "review_date": "25/03/2025", "sentiment": None, "images": []}
        outcome = run("product_reviews", review)
        assert outcome.row["rating"] is None and outcome.row["review_date"] == date(2025, 3, 25)
        assert {"product_reviews.invalid_rating", "product_reviews.missing_sentiment"} <= set(outcome.flags)

    def test_call_duration_order_and_sentiment(self):
        call = {"call_id": "CALL-000001", "ticket_id": None, "customer_id": "CUS-00000001",
                "agent_id": "AGT-005", "agent_name": "Sarah", "call_start": "2025-03-15T09:00:00",
                "call_end": "2025-03-15T09:10:00", "duration_seconds": -5,
                "sentiment_overall": "frustrated_to_satisfied", "quality_score": 140,
                "utterances": [{"timestamp": "00:00:09", "speaker": "Customer", "text": "b"},
                               {"timestamp": "00:00:02", "speaker": "agent", "text": "a"}]}
        outcome = run("call_transcripts", call)
        row = outcome.row
        assert row["duration_seconds"] == 600 and row["quality_score"] is None
        assert [u["text"] for u in row["utterances"]] == ["a", "b"]
        assert (row["sentiment_start"], row["sentiment"]) == ("negative", "positive")
        assert {"call_transcripts.missing_ticket_ref", "call_transcripts.invalid_quality_score"} <= set(outcome.flags)
        assert {"call_transcripts.duration_recomputed", "call_transcripts.utterances_sorted"} <= set(outcome.fixes)


@pytest.mark.parametrize("entity", sorted(SCHEMAS))
def test_non_dict_record_is_quarantined_not_crashing(entity):
    assert validate_record(entity, SCHEMAS[entity], "not a record", CONTEXT).row is None
