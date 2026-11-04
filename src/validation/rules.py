"""Registry of every data-quality rule the pipeline can raise.

Record-level rules are raised by the schemas in ``src/validation/schemas``; dataset-level
rules (duplicates, cross-entity references, evidence from other tables) by
``src/cleaning/clean.py``. Each rule has exactly one action:

* ``fix``        – a value was standardised or derived from evidence; recorded in ``dq_fixes``.
* ``flag``       – the record is kept, but the issue is recorded in its ``dq_flags``.
* ``quarantine`` – the record is rejected and written to the quarantine file.
* ``drop``       – the record repeats another one's key and is removed; logged in
  ``outputs/quarantine/_duplicates.csv``.

Principle: quarantine only when the record as a whole cannot be trusted (no usable
identifier, an impossible business event). A single bad attribute is nulled and flagged
so the rest of the record, and everything that references it, survives.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):  # StrEnum needs 3.11; the brief allows 3.10
    FIX = "fix"
    FLAG = "flag"
    QUARANTINE = "quarantine"
    DROP = "drop"


@dataclass(frozen=True)
class Rule:
    id: str
    action: Action
    description: str

    @property
    def entity(self) -> str:
        return self.id.split(".", 1)[0]


def _rules(action: Action, **descriptions: str) -> list[Rule]:
    return [Rule(rule_id.replace("__", "."), action, text) for rule_id, text in descriptions.items()]


FIX, FLAG, QUARANTINE, DROP = Action.FIX, Action.FLAG, Action.QUARANTINE, Action.DROP

RULES: dict[str, Rule] = {
    rule.id: rule
    for rule in [
        # ── products ──────────────────────────────────────────────────────────
        *_rules(
            QUARANTINE,
            products__invalid_id="product_id missing or not PRD-NNNNNN",
            products__missing_name="product_name missing",
        ),
        *_rules(
            FIX,
            products__name_case="all-lower/ALL-UPPER product name re-cased",
            products__currency="currency alias mapped to ISO code",
            products__boolean="is_active token mapped to true/false",
            products__date_format="created_date parsed from a non-ISO format",
        ),
        *_rules(
            FLAG,
            products__missing_category="category missing",
            products__invalid_price="price <= 0; nulled",
            products__missing_cost="cost missing",
            products__invalid_weight="weight_kg <= 0; nulled",
            products__negative_stock="stock_quantity < 0 (oversold/backorder?)",
            products__invalid_rating="rating outside 0-5; nulled",
            products__invalid_date="created_date unparseable; nulled",
            products__created_after_updated="created_date later than last_updated",
        ),
        # ── customers ─────────────────────────────────────────────────────────
        *_rules(
            QUARANTINE,
            customers__invalid_id="customer_id missing or not CUS-NNNNNNNN",
            customers__missing_name="no first, last or full name",
        ),
        *_rules(
            FIX,
            customers__email_format="email trimmed/lower-cased",
            customers__phone_format="phone converted to E.164",
            customers__gender="gender token mapped to canonical value",
            customers__segment="segment case normalised",
            customers__boolean="boolean token mapped to true/false",
            customers__registration_from_metadata=(
                "registration_date replaced by metadata.created_at (ISO source of truth; "
                "epoch values were emitted in UTC+1)"
            ),
            customers__age_from_dob="missing/invalid age derived from date_of_birth at as_of_date",
        ),
        *_rules(
            FLAG,
            customers__missing_email="email missing",
            customers__invalid_email="email not a valid address; nulled",
            customers__invalid_phone="phone not a possible number; nulled",
            customers__invalid_age="age missing or outside 0-120, and no date_of_birth; nulled",
            customers__future_registration="registration_date after as_of_date; nulled",
            customers__future_last_purchase="last_purchase_date after as_of_date; nulled",
            customers__negative_total_spend="source total_spend < 0",
            customers__invalid_currency_pref="preferred_currency unknown; nulled",
        ),
        # ── transactions ──────────────────────────────────────────────────────
        *_rules(
            QUARANTINE,
            transactions__invalid_id="transaction_id missing or malformed",
            transactions__zero_quantity="quantity is 0 (shipping-only line, no product sold)",
            transactions__invalid_date="transaction date missing or unparseable",
        ),
        *_rules(
            FIX,
            transactions__currency="currency alias mapped to ISO code",
            transactions__boolean="is_gift token mapped to true/false",
            transactions__return="negative quantity classified as a return line",
        ),
        *_rules(
            FLAG,
            transactions__amount_mismatch="total != sign*(subtotal-discount+tax)+shipping",
            transactions__negative_shipping="negative shipping on a sale",
            transactions__junk_notes="placeholder/garbage notes; nulled",
        ),
        # ── vendors ───────────────────────────────────────────────────────────
        *_rules(QUARANTINE, vendors__invalid_id="vendor_id missing or not VND-NNN"),
        *_rules(FIX, vendors__phone_format="contact phone converted to E.164"),
        *_rules(
            FLAG,
            vendors__missing_reliability="reliability_score missing",
            vendors__invalid_reliability="reliability_score outside 0-100; nulled",
            vendors__missing_contact="no contact details at all",
            vendors__incomplete_contact="contact partially missing",
        ),
        # ── invoices ──────────────────────────────────────────────────────────
        *_rules(
            QUARANTINE,
            invoices__invalid_id="invoice_id missing or malformed",
            invoices__invalid_date="invoice_date missing or unparseable",
        ),
        *_rules(
            FIX,
            invoices__currency="currency alias mapped to ISO code",
            invoices__date_format="date parsed from a non-ISO format",
            invoices__due_date_from_terms="due_date recomputed as invoice_date + payment terms",
            invoices__credit_note="negative total classified as a credit note",
        ),
        *_rules(
            FLAG,
            invoices__unknown_terms="payment_terms not in configured terms",
            invoices__unparseable_line_items="line_items_json is not a JSON list",
            invoices__line_count_mismatch="line_item_count != number of lines",
            invoices__subtotal_mismatch="subtotal != sum(line_total)",
            invoices__tax_mismatch="tax_amount != subtotal * tax_rate",
            invoices__total_mismatch="total != subtotal + tax + shipping",
            invoices__balance_mismatch="balance_due != total - amount_paid",
            invoices__paid_before_invoiced="payment_date earlier than invoice_date",
            invoices__status_payment_conflict="payment_status inconsistent with payment_date",
            invoices__number_year_mismatch="year in invoice_number != invoice_date year",
        ),
        # ── invoice lines ─────────────────────────────────────────────────────
        *_rules(
            QUARANTINE,
            invoice_lines__invalid_key="invoice_id or line_number missing",
            invoice_lines__zero_quantity="quantity is 0",
        ),
        *_rules(FIX, invoice_lines__credit="negative quantity classified as a credit line"),
        *_rules(
            FLAG,
            invoice_lines__missing_product="line has no product_id (non-catalog item)",
            invoice_lines__line_total_mismatch="line_total != quantity * unit_cost",
        ),
        # ── support tickets ───────────────────────────────────────────────────
        *_rules(
            QUARANTINE,
            support_tickets__invalid_id="ticket_id missing or malformed",
            support_tickets__invalid_created_at="created_at missing or unparseable",
        ),
        *_rules(
            FIX,
            support_tickets__enum_case="channel/priority/status case normalised",
            support_tickets__date_format="created_at parsed from a non-ISO format",
        ),
        *_rules(
            FLAG,
            support_tickets__invalid_satisfaction="satisfaction_score outside 1-5; nulled",
            support_tickets__missing_satisfaction="resolved/closed ticket without satisfaction_score",
            support_tickets__missing_resolved_at="resolved/closed ticket without resolved_at",
            support_tickets__resolved_before_created="resolved_at earlier than created_at",
            support_tickets__missing_product_ref="product-related ticket without product_id",
        ),
        # ── product reviews ───────────────────────────────────────────────────
        *_rules(QUARANTINE, product_reviews__invalid_id="review_id missing or malformed"),
        *_rules(FIX, product_reviews__date_format="review_date parsed from a non-ISO format"),
        *_rules(
            FLAG,
            product_reviews__missing_rating="rating missing",
            product_reviews__invalid_rating="rating outside 1-5; nulled",
            product_reviews__missing_sentiment="sentiment missing",
        ),
        # ── call transcripts ──────────────────────────────────────────────────
        *_rules(
            QUARANTINE,
            call_transcripts__invalid_id="call_id missing or malformed",
            call_transcripts__invalid_start="call_start missing or unparseable",
        ),
        *_rules(
            FIX,
            call_transcripts__duration_recomputed="duration_seconds recomputed from call_end - call_start",
            call_transcripts__sentiment="compound sentiment split into start/end and normalised",
            call_transcripts__utterances_sorted="utterances re-ordered by timestamp",
        ),
        *_rules(
            FLAG,
            call_transcripts__missing_ticket_ref="call has no ticket_id",
            call_transcripts__missing_agent="agent_id or agent_name missing",
            call_transcripts__invalid_duration="duration negative and not recomputable; nulled",
            call_transcripts__missing_utterances="utterance array empty or missing",
            call_transcripts__invalid_quality_score="quality_score outside 0-100; nulled",
        ),
        # ══ dataset-level rules (src/cleaning/clean.py) ═══════════════════════
        *_rules(
            DROP,
            **{
                f"{entity}__duplicate": "primary key seen before; latest version kept, the rest removed"
                for entity in (
                    "products", "customers", "transactions", "vendors", "invoices", "invoice_lines",
                    "support_tickets", "product_reviews", "call_transcripts",
                )
            },
        ),
        *_rules(
            FIX,
            products__category_from_name="missing category inferred from product name (unambiguous mapping)",
            transactions__currency_relabelled="non-USD label but unit_price equals USD catalog price; set to USD",
            invoices__currency_relabelled="non-USD label but line costs equal USD catalog costs; set to USD",
        ),
        *_rules(
            FLAG,
            products__unknown_vendor="vendor_id not in vendors",
            products__sku_conflict="SKU shared with a different product",
            customers__shared_email="email shared with a different customer",
            transactions__unknown_customer="customer_id not in customers",
            transactions__unknown_product="product_id not in products",
            transactions__currency_unverified="non-USD label not checkable against the catalog; still set to USD",
            invoices__unknown_vendor="vendor_id not in vendors",
            invoices__duplicate_number="invoice_number shared with a different invoice",
            invoices__currency_unverified="non-USD label not checkable against line costs; still set to USD",
            invoice_lines__unknown_invoice="invoice_id not in invoices (header quarantined)",
            invoice_lines__unknown_product="product_id not in products (external item)",
            invoice_lines__vendor_mismatch="product belongs to a different vendor than the invoice",
            support_tickets__unknown_customer="customer_id not in customers",
            support_tickets__unknown_product="product_id not in products",
            product_reviews__unknown_customer="customer_id not in customers",
            product_reviews__unknown_product="product_id not in products",
            product_reviews__unverified_purchase="verified_purchase is true but no matching transaction",
            call_transcripts__unknown_customer="customer_id not in customers",
            call_transcripts__unknown_ticket="ticket_id not in support tickets",
        ),
    ]
}
