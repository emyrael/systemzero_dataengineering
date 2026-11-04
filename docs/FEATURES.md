# Feature Dictionary

Gold tables built by `src/features/build.py` from the Silver `clean` tables, written to
`outputs/processed/gold/<table>.parquet`. Sample rows: `outputs/feature_samples.md`.

## Conventions

| Topic | Rule | Why |
|---|---|---|
| Reference date | `as_of_date` from config (2025-01-01) for every "days since" / overdue figure | reruns give identical numbers; never `now()` |
| Observation window | starts at the first transaction in the data (2022-01-01) | customers registered from 2018, but there is no sales history before 2022: tenure before that would dilute frequency and CLV |
| Recognised revenue | transaction with `payment_status = paid` and `order_status != cancelled` | 24% of payments failed and 25% were refunded; only captured money is revenue |
| Revenue amount | `net_revenue = sign × (subtotal − discount_amount)`, sign = −1 for returns | merchandise revenue, excluding tax (a pass-through) and shipping |
| Currency | all amounts USD | currency labels proved meaningless (`docs/CLEANING_RULES.md` §5) |
| Orphans | sales on unknown customers/products count in totals but attach to no customer/product row | revenue stays complete; Σ product revenue = total − $39,841 on `PRD-DELETED-*` |
| Nulls | a feature is null when its inputs are missing, never 0 by default | 0 means "measured zero", null means "unknown" |

## Customer features (`customer_features`, one row per customer)

| Feature | Formula | Notes |
|---|---|---|
| `total_orders` | distinct orders among recognised purchase lines (returns excluded) | one line per order in this data |
| `total_spend` | Σ `net_revenue` (returns subtract) | recomputed from transactions; source `total_spend` is unreliable (corr. −0.008) |
| `tenure_months` | (as_of − start) / 30.44, start = earliest of registration and first transaction, clipped to the window start | 308 of 994 customers transacted *before* their registration date, so registration alone understates tenure |
| `purchase_frequency` | `total_orders / tenure_months` | orders per month |
| `average_order_value` | mean `net_revenue` of purchase lines | null without purchases |
| `days_since_last_purchase` | as_of − last purchase date | recency; null without purchases |
| `customer_lifetime_value` | `max(total_spend, 0) / tenure_months × 36` | observed monthly value projected over `customer_lifetime_months` (config, default 36) |
| `rfm_recency`, `rfm_frequency`, `rfm_monetary` | quintile score 1–5 among buyers (5 = most recent / most orders / highest spend) | ties broken by row order, so scores are deterministic |
| `customer_segment_score` | R + F + M (3–15) | null for customers with no recognised purchase |
| `rfm_segment` | Champions (score ≥ 13); R ≥ 4: Loyal (≥ 9) / Promising; R ≤ 2: At risk (≥ 9) / Hibernating; else Needs attention; "No purchases" | readable label for the dashboard and graph |

## Product features (`product_features`, one row per product)

| Feature | Formula | Notes |
|---|---|---|
| `units_sold` | Σ quantity of recognised lines (returns subtract) | |
| `revenue_contribution` | Σ `net_revenue` | total revenue from this product; can be negative when returns exceed sales |
| `revenue_share_pct` | `revenue_contribution / total recognised revenue × 100` | share of the whole business |
| `velocity_score` | recognised sale lines / months on sale, months from max(`created_date`, window start) to as_of | sales per month; fair to products added recently |
| `stock_turnover_rate` | units sold in the last 365 days / `stock_quantity` | stock is a snapshot, used as the inventory proxy; null when stock ≤ 0 (6 products have negative stock, flagged as backorders) |
| `price_tier` | Low / Medium / High / Premium = catalog price quartiles | boundaries are printed in the sample output; null for the 13 products without a valid price |
| `vendor_reliability_weighted_score` | `rating × vendor reliability_score / 100` (0–5) | a 4.0 product from a 72.5%-reliable vendor scores 2.9; null if either input is missing |

## Vendor features (`vendor_features`, one row per vendor)

| Feature | Formula | Notes |
|---|---|---|
| `total_products_supplied` | count of the vendor's products with `is_active = true` | |
| `revenue_generated` | Σ `revenue_contribution` of the vendor's products | |
| `product_quality_score` | mean product `rating` | |
| `invoice_payment_rate` | % of invoices due by as_of that were fully paid on or before `due_date` | due dates are the recomputed ones (invoice date + NET terms); credit notes excluded |
| `average_invoice_value` | mean `total_amount` | credit notes excluded |
| `total_outstanding_balance` | Σ `balance_due` of invoices not `paid` (pending, partial, overdue, disputed) | credit notes excluded |

## Invoice features (`invoice_features`, one row per invoice)

| Feature | Formula | Notes |
|---|---|---|
| `days_to_payment` | `payment_date − invoice_date` | null when unpaid, or when payment precedes the invoice (8 cases, flagged in Silver) |
| `days_overdue` | `max(as_of − due_date, 0)` for invoices not `paid` | null for paid invoices |
| `line_item_diversity` | distinct `product_id` on the invoice's lines | lines without a product (non-catalog items) not counted |
| `discount_rate_achieved` | `(standard − billed) / standard × 100` over lines with a catalog cost; standard = Σ\|qty\| × catalog cost, billed = Σ\|line_total\| | the invoices carry no discount field, so the only measurable discount is price vs catalog cost. **It is 0 on every checkable invoice**: vendors bill exactly at catalog cost. Null when no line has a catalog cost |
| `reconciliation_flag` | true when every catalog line's unit cost equals the catalog cost **and** the invoice's own arithmetic holds (no `total_mismatch` / `subtotal_mismatch` flag) | null when no line can be checked |
| `reconciliation_note` | `matched` / `cost_mismatch` / `total_mismatch` / `no_catalog_lines` | the reason behind the flag |
