# Raw Data Profile

Raw files profiled read-only before any cleaning; reference date **2025-01-01**. **8 files, 7,393 records** (+3,618 invoice lines nested in `invoices.csv`). **364 checks, 86 with findings**: 10 Critical, 19 High, 38 Medium, 19 Low; 68 violations, 14 anomalies, 4 business exceptions. Every figure, column statistic and sample id is in `raw_data_profile.json`.

## Datasets

| Dataset | Rows | Cols | Critical | High | Medium/Low |
|---|---|---|---|---|---|
| `products` | 505 | 19 | 0 | 5 | 8 |
| `customers` | 1003 | 30 | 0 | 5 | 11 |
| `transactions` | 5010 | 24 | 4 | 1 | 7 |
| `vendors` | 10 | 23 | 0 | 1 | 4 |
| `invoices` | 805 | 25 | 3 | 3 | 5 |
| `support_tickets` | 20 | 15 | 1 | 1 | 9 |
| `product_reviews` | 30 | 15 | 1 | 2 | 6 |
| `call_transcripts` | 10 | 20 | 0 | 0 | 4 |
| `invoice_lines` | 3618 | 7 | 1 | 1 | 3 |

## Critical and high findings

| Sev. | Dataset | Finding | Records | Pipeline rule |
|---|---|---|---|---|
| Crit | invoices | non-USD label, but every line cost equals the USD catalog cost *(anomaly)* | 557 (69.19%) | `invoices.currency_relabelled` |
| Crit | transactions | product_id not found in products | 109 (2.18%) | `transactions.unknown_product` |
| Crit | transactions | customer_id not found in customers | 89 (1.78%) | `transactions.unknown_customer` |
| Crit | transactions | non-USD label, but unit_price equals the USD catalog price *(anomaly)* | 71 (1.42%) | `transactions.currency_relabelled` |
| Crit | invoices | total_amount != subtotal + tax + shipping | 20 (2.48%) | `invoices.total_mismatch` |
| Crit | invoice_lines | record identical to an earlier one (all fields) | 18 (0.5%) | `invoice_lines.duplicate` |
| Crit | transactions | record identical to an earlier one (all fields) | 10 (0.2%) | `transactions.duplicate` |
| Crit | invoices | record identical to an earlier one (all fields) | 5 (0.62%) | `invoices.duplicate` |
| Crit | product_reviews | required field `review_id` missing | 1 (3.33%) | `product_reviews.invalid_id` |
| Crit | support_tickets | required field `ticket_id` missing | 1 (5%) | `support_tickets.invalid_id` |
| High | invoices | due_date equals invoice_date despite NET terms *(anomaly)* | 805 (100%) | `invoices.due_date_from_terms` |
| High | customers | last_purchase_date after as_of_date 2025-01-01 | 387 (38.58%) | `customers.future_last_purchase` |
| High | invoice_lines | product_id not found in products | 46 (1.27%) | `invoice_lines.unknown_product` |
| High | transactions | quantity is 0 (shipping-only line, nothing sold) | 42 (0.84%) | `transactions.zero_quantity` |
| High | customers | required field `email` missing | 34 (3.39%) | `customers.missing_email` |
| High | customers | not a syntactically valid email address | 21 (2.09%) | `customers.invalid_email` |
| High | customers | age outside 0–120 | 16 (1.6%) | `customers.invalid_age` |
| High | invoices | vendor_id not found in vendors | 16 (1.99%) | `invoices.unknown_vendor` |
| High | products | weight_kg < 0 | 16 (3.17%) | `products.invalid_weight` |
| High | products | required field `category` missing | 14 (2.77%) | `products.missing_category` |
| High | products | price is not a positive number | 13 (2.57%) | `products.invalid_price` |
| High | invoices | payment_date earlier than invoice_date | 8 (0.99%) | `invoices.paid_before_invoiced` |
| High | products | stock_quantity < 0 | 6 (1.19%) | `products.negative_stock` |
| High | products | record identical to an earlier one (all fields) | 5 (0.99%) | `products.duplicate` |
| High | customers | record identical to an earlier one (all fields) | 3 (0.3%) | `customers.duplicate` |
| High | product_reviews | required field `rating` missing | 1 (3.33%) | `product_reviews.missing_rating` |
| High | product_reviews | rating outside 1–5 | 1 (3.33%) | `product_reviews.invalid_rating` |
| High | support_tickets | satisfaction_score outside 1–5 | 1 (5%) | `support_tickets.invalid_satisfaction` |
| High | vendors | no contact name, email or phone | 1 (10%) | `vendors.missing_contact` |

## Broken references

| Reference | Orphans | Example ids |
|---|---|---|
| products.vendor_id → vendors | 1 (0.2%) | `VND-INVALID` |
| transactions.customer_id → customers | 89 (1.78%) | `CUS-INVALID-1`, `CUS-INVALID-10` |
| transactions.product_id → products | 109 (2.18%) | `PRD-DELETED-1`, `PRD-DELETED-10` |
| invoices.vendor_id → vendors | 16 (1.99%) | `VND-INVALID-172`, `VND-INVALID-266` |
| invoice_lines.product_id → products | 46 (1.27%) | `PRD-EXTERNAL-1184`, `PRD-EXTERNAL-1289` |
| support_tickets.customer_id → customers | 1 (5%) | `INVALID_ID` |
| support_tickets.product_id → products | 3 (15%) | `PRD-000555`, `PRD-000666` |
| product_reviews.product_id → products | 1 (3.33%) | `PRD-INVALID` |
| call_transcripts.customer_id → customers | 1 (10%) | `INVALID` |

No orphans: invoice_lines.invoice_id, product_reviews.customer_id, call_transcripts.ticket_id.

## Evidence behind key decisions

**Ambiguous dates** vs the ISO `metadata.created_at` copy in customers:

| Format | Values | Day-first agrees | Month-first agrees |
|---|---|---|---|
| nn-nn-yyyy | 203 | 13 | 203 |
| nn/nn/yyyy | 185 | 185 | 10 |

Epoch values agree 0/213 (one day early): metadata wins.

**Currency labels**: amounts equal to the USD catalog value, by label. Labels carry no meaning, so everything is treated as USD and nothing is FX-converted.

| Label | Transactions | Invoice lines |
|---|---|---|
| AUD | 21/21 | 334/334 |
| CAD | – | 333/333 |
| EUR | 27/27 | 1,052/1,052 |
| GBP | 23/23 | 367/367 |
| USD | 4,830/4,830 | 912/912 |

## Kept as business events, not errors

- vendors: pending/inactive vendor still has catalogue products and sales (2)
- transactions: negative quantity: a return, not an error (236)
- invoice_lines: negative quantity: a credit line (67)
- call_transcripts: utterances by a speaker other than agent/customer (system prompts, supervisors) (8)

## Medium and low

53 findings, mostly representations to standardise. Largest by share: product_reviews `unverified_purchase_claim` 96.67%; support_tickets `partial_extract` 92%; products `boolean_spellings:is_active` 80.59%; product_reviews `partial_extract` 80%; customers `mixed_date_formats:registration_date` 78.76%; products `mixed_date_formats:created_date` 77.82%; invoices `mixed_date_formats:invoice_date` 74.41%; invoices `number_year_mismatch` 68.32%; transactions `boolean_spellings:is_gift` 66.25%; call_transcripts `partial_extract` 60%.
