# Cleaning Rules

How raw source data becomes the Silver tables, and why. Every rule named here is
registered in [`src/validation/rules.py`](../src/validation/rules.py); the
[quality report](../outputs/quality_report.md) is generated from the same registry and gives
the record counts for each run.

## Where the rules run

| Stage | Code | Scope | Output |
|---|---|---|---|
| validate | `src/validation/schemas/` | one record at a time | `silver/validated/<entity>.parquet`, `quarantine/<entity>.csv` |
| clean | `src/cleaning/clean.py` | whole tables, across entities | `silver/clean/<entity>.parquet`, `quarantine/_duplicates.csv` |

Each rule has one action:

| Action | Effect | Recorded in |
|---|---|---|
| **fix** | value standardised, or derived from evidence elsewhere in the data | row's `dq_fixes` |
| **flag** | record kept as is; issue noted | row's `dq_flags` |
| **quarantine** | record rejected | `quarantine/<entity>.csv` with rule, message and raw record |
| **drop** | repeated primary key removed | `quarantine/_duplicates.csv`, pointing at the kept row |

**Quarantine only when the record as a whole cannot be trusted:** no usable identifier,
or an event that cannot have happened (a sale of zero units). A single bad attribute is
nulled and flagged, so the rest of the record, and everything that references it, survives.
Dropping a product over a bad weight would orphan its sales and invoice lines.

Silver never invents values. A value is filled only when other data implies it; otherwise
it stays null, with a flag if the source value was invalid.

## 1. Parsing conventions (validate)

| Kind | Rule | Evidence / reason |
|---|---|---|
| Null tokens | `""`, `N/A`, `NA`, `null`, `none`, `nan`, `unknown`, `unlisted`, `-` → null | seen across sources |
| Dates | `dd/mm/yyyy` (slash = **day first**), `mm-dd-yyyy` (dash = **month first**); a part > 12 overrides; ISO, `dd-Mon-yyyy`, `yyyy/mm/dd` and epoch seconds also accepted | customers ship a second, ISO copy of each date in `metadata.created_at`: the rule matched 203/203 dash and 185/185 slash dates |
| Epoch timestamps | read as UTC; for customers, `metadata.created_at` wins | the 213 epoch values are midnight UTC+1, one day early if read naively |
| Booleans | `true/t/yes/y/1` and `false/f/no/n/0`, any case; anything else is invalid | 8 spellings of `is_active` |
| Currency | aliases (`$`, `usd`, `US Dollar`, `€`…) → ISO code | 6 spellings in products |
| Phone | E.164 via `phonenumbers`, default region US | every number is NANP-formatted regardless of country |
| Email | lower-cased, must match a basic address pattern; otherwise null + flag | `N/A`, names in the email column |
| Text | Unicode NFC, trimmed, inner whitespace collapsed; non-ASCII kept as UTF-8, never stripped | 16 product descriptions carry `中文`, `日本語`, `café`, `résumé`; all arrive intact in Silver |
| Names | re-cased only when all-lower or ALL-UPPER | keeps `McDonald`, `van der Berg` |
| Enums | lower snake_case (`Gold` → `gold`) | mixed-case segments, statuses, priorities |
| Integers | nullable `Int64` | a null must not turn `age` into `44.0` |

## 2. Duplicates (clean)

One row per primary key (`primary_key` in `config/pipeline_config.yaml`; invoice lines use
`invoice_id + line_number`). When a key repeats, the row with the latest `updated_at` is
kept (the first seen if the entity has no `updated_at`). Every removed row is logged with
the kept row's source row and whether it was an exact copy.

In this extract all 41 removed rows are exact copies (5 products, 3 customers, 10
transactions, 5 invoices and their 18 lines), so no information is lost.

## 3. Referential integrity (clean)

A foreign key that matches no record is **flagged, not dropped or nulled**. The raw id is
kept for audit; revenue totals stay complete; the graph loader simply draws no edge.
Missing keys are a validation concern and are not double-counted here.

| Child → parent | Rule | This run |
|---|---|---|
| transactions → customers | `transactions.unknown_customer` | 88 (`CUS-INVALID-*`) |
| transactions → products | `transactions.unknown_product` | 108 (`PRD-DELETED-*`) |
| products → vendors | `products.unknown_vendor` | 1 (`VND-INVALID`) |
| invoices → vendors | `invoices.unknown_vendor` | 16 |
| invoice lines → products | `invoice_lines.unknown_product` | 46 (`PRD-EXTERNAL`) |
| invoice lines: product's vendor ≠ invoice's vendor | `invoice_lines.vendor_mismatch` | 72 |
| tickets, reviews, calls → customers / products / tickets | `*.unknown_*` | 1–3 each |

## 4. Cross-record consistency (clean)

| Rule | Meaning | Why not merge or drop |
|---|---|---|
| `customers.shared_email` | 16 emails used by two customers each | different names, phones and dates: separate people with bad data, not one person |
| `products.sku_conflict` | 17 SKUs shared by unrelated products | `product_id` is the key; SKU is a label |
| `invoices.duplicate_number` | 20 invoice numbers shared, mostly across vendors | `invoice_id` is the key; numbers are only unique per vendor |
| `product_reviews.unverified_purchase` | `verified_purchase = true` with no matching transaction | 28 of 29 reviews: the claim is unsupported by our data |

## 5. Evidence-based fixes (validate and clean)

| Rule | What | Evidence |
|---|---|---|
| `customers.registration_from_metadata` | `registration_date` taken from `metadata.created_at` when they disagree | the metadata timestamp is ISO and unambiguous |
| `customers.age_from_dob` | age recomputed from date of birth when missing or invalid | `age_on(dob, as_of_date)` |
| `invoices.due_date_from_terms` | `due_date = invoice_date + NET days`; source kept in `src_due_date` | source `due_date` equals `invoice_date` on 805/805 invoices |
| `call_transcripts.duration_recomputed` | duration = end − start | timestamps are present on every call |
| `products.category_from_name` | missing category from the product name minus its first word (`Ultra Power Bank` → `Power Bank`) | all 76 such names map to exactly one category; filled 14/14. Subcategory is **not** inferred: it varies within a name |
| `transactions.currency_relabelled` | non-USD label → `USD`; source kept in `src_currency` | `unit_price` equals the USD catalog price on 100% of rows, whatever the label |
| `invoices.currency_relabelled` | same, for invoices | every line cost matches the USD catalog cost (credit lines: its negative), whatever the label |

Rows with a non-USD label that cannot be checked (product not in the catalog, or an invoice
with no catalog lines) are also set to USD and flagged `*.currency_unverified` (2
transactions, 8 invoices): the evidence that labels carry no meaning is dataset-wide.
**Consequence:** all amounts are USD and no FX conversion is applied. Converting an "EUR"
invoice whose numbers are USD catalog costs would distort vendor spend by the exchange rate.

## 6. Missing values

| Column | Missing (clean) | Handling | Reason |
|---|---|---|---|
| `invoices.payment_date` | 361 | left null | exactly the pending, overdue and disputed invoices: not paid yet |
| `invoices.payment_method`, `approved_by`, `po_number` | 71–214 | left null | optional business fields |
| `transactions.shipping_method` | 1009 | left null | spread evenly over channels and statuses; no signal to infer from |
| `customers.last_purchase_date` | 386 | left null; recomputed from transactions in features | source value is unreliable: all 387 non-null raw values are after `as_of_date` (nulled + flagged) |
| `customers.src_total_spend` and other source aggregates | ~21–34 | kept as `src_*`, not used | correlation with actual transactions is −0.008; features recompute from facts |
| `customers.age` | 7 | derived from date of birth where possible | the 7 have no date of birth either |
| `customers.email`, `phone` | 54, 308 | left null (invalid ones flagged) | contact data cannot be inferred |
| `products.price`, `cost` | 13 | left null, flagged `invalid_price` / `missing_cost` | source price negative, cost absent. No transactions for these products; invoice costs vary per line, so neither is recoverable |
| `products.weight_kg` | 39 | left null (negatives flagged) | not needed by any feature; a category median would be fabricated |
| `products.subcategory` | 14 | left null | not implied by name (see above) |
| `invoice_lines.product_id` | 491 | left null, flagged `missing_product` | non-catalog items (services, freight) |
| `product_reviews.rating` | 2 | left null, flagged | rating is the review's core fact |
| `support_tickets.satisfaction_score` | 4 | left null, flagged when the ticket is resolved | not collected |
| `call_transcripts.ticket_id` | 3 | left null, flagged | calls not linked to a ticket |

Consumers decide how to present nulls: features exclude them from averages, the dashboard
labels them "Unknown". Silver does not substitute sentinel values.

## Not done here (by design)

- **Customer aggregates** (orders, spend, recency): recomputed from transactions in the
  features stage, not patched in Silver.
- **Inactive or pending vendors with sales** (VND-009 pending with 71 sales, VND-010
  inactive with 16): a business signal, surfaced as vendor features, not a data error.
- **Text normalisation of transcripts and reviews** beyond Unicode NFC and whitespace:
  the text is valid; GraphRAG consumers get it as written.
