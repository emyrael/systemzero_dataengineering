"""The eight graph queries from the brief, with the reasoning behind each.

Each query is one SurrealQL statement taking named parameters; ``run_graph`` executes all
of them after a load and writes ``outputs/graph_queries.md``. Revenue is always read from
``purchased`` edges with ``recognised = true``, the same rule as the Gold features.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GraphQuery:
    id: str
    question: str
    surql: str
    explanation: str


QUERIES: tuple[GraphQuery, ...] = (
    GraphQuery(
        "q1_reliable_vendor_products",
        "Which products come from vendors with a reliability score above 90?",
        """SELECT name AS vendor, reliability_score,
       array::len(->supplies->product) AS products,
       array::slice(array::sort(->supplies->product.name), 0, 3) AS sample_products
FROM vendor
WHERE reliability_score > 90
ORDER BY reliability_score DESC, vendor""",
        "One hop `vendor->supplies->product`. Filtering on the vendor node first means only reliable "
        "vendors' edges are walked; no join table is needed.",
    ),
    GraphQuery(
        "q2_customer_purchase_history",
        "What has customer $customer bought, from which vendors?",
        """SELECT transaction_date, out.name AS product, out.category AS category,
       (out<-supplies<-vendor.name)[0] AS vendor,
       quantity, net_revenue, recognised, order_status
FROM purchased
WHERE in = $customer
ORDER BY transaction_date, product""",
        "Two hops from the customer: `purchased` edges carry the transaction details, and the product's "
        "incoming `supplies` edge gives the vendor. `recognised` shows which lines count as revenue.",
    ),
    GraphQuery(
        "q3_related_products",
        "Which products are related to $product (same vendor, same category, bought together)?",
        """SELECT name, category,
       array::slice(array::sort(array::complement(<-supplies<-vendor->supplies->product, [id]).name), 0, 3) AS same_vendor,
       array::len(array::complement(->belongs_to->category<-belongs_to<-product, [id])) AS same_category_count,
       (SELECT out.name AS product, shared_customers FROM $parent->similar_to
        ORDER BY shared_customers DESC, product LIMIT 5) AS bought_together
FROM ONLY $product""",
        "Three relatedness signals in one statement: siblings through the vendor "
        "(`<-supplies<-vendor->supplies->product`), siblings through the category, and the precomputed "
        "co-purchase edge `similar_to` weighted by shared customers. The basis for recommendations.",
    ),
    GraphQuery(
        "q4_top_customers_by_vendor",
        "Who are each vendor's top three customers by recognised spend?",
        """SELECT name AS vendor,
       (SELECT * FROM (
            SELECT in AS customer_id, in.name AS customer, math::sum(net_revenue) AS spend
            FROM $parent->supplies->product<-purchased
            WHERE recognised = true
            GROUP BY customer_id, customer)
        ORDER BY spend DESC, customer_id LIMIT 3) AS top_customers
FROM vendor
ORDER BY vendor""",
        "Walks `vendor->supplies->product<-purchased`, groups the edges by customer, then sorts. The "
        "grouping is wrapped in its own SELECT: with ORDER BY on the aggregate in the same statement, "
        "rows came back in customer-name order and LIMIT kept the wrong three. Starting from the vendor "
        "(instead of scanning all purchases with `out IN …`) cut the run time from 78 s to under 1 s.",
    ),
    GraphQuery(
        "q5_vendor_influence",
        "How many customers does each vendor reach, and what share of all buyers is that?",
        """SELECT name AS vendor,
       array::len(array::distinct(->supplies->product<-purchased[WHERE recognised = true]<-customer)) AS customers_reached,
       math::round(100 * array::len(array::distinct(->supplies->product<-purchased[WHERE recognised = true]<-customer))
           / array::len(array::distinct((SELECT VALUE in FROM purchased WHERE recognised = true)))) AS influence_pct,
       math::round(revenue_generated) AS revenue_generated
FROM vendor
ORDER BY customers_reached DESC, vendor""",
        "Influence = distinct customers reached through the vendor's products, as a share of all buying "
        "customers. A three-hop traversal with an edge filter; revenue alone would rank a vendor with "
        "a few large buyers above one with broad reach.",
    ),
    GraphQuery(
        "q6_invoice_reconciliation",
        "Do invoice line costs match the catalog cost of the products they bill?",
        """SELECT (in<-billed<-vendor.name)[0] ?? '(unknown vendor)' AS vendor,
       count() AS lines_checked,
       count(math::abs(math::abs(unit_cost) - out.cost) <= 0.01) AS cost_matches
FROM invoice_item
WHERE out.cost != NONE
GROUP BY vendor
ORDER BY vendor""",
        "Each `invoice_item` edge links a line to the product it bills, so the check is a comparison "
        "across the edge (`unit_cost` vs `out.cost`; credit lines carry the negated cost). Every checkable "
        "line matches. Lines whose product is not in the catalog have no edge and are counted at load time.",
    ),
    GraphQuery(
        "q7_overdue_vendors",
        "Which vendors have overdue invoices, and how many of their products are affected?",
        """SELECT name AS vendor,
       count(->billed->invoice[WHERE days_overdue > 0]) AS overdue_invoices,
       math::round(math::sum(->billed->invoice[WHERE days_overdue > 0].balance_due)) AS overdue_balance,
       array::len(array::distinct(->billed->invoice[WHERE days_overdue > 0]->invoice_item->product)) AS affected_products
FROM vendor
WHERE count(->billed->invoice[WHERE days_overdue > 0]) > 0
ORDER BY overdue_balance DESC, vendor""",
        "Vendor -> overdue invoices -> billed products. `days_overdue` is a Gold feature stored on the node, "
        "so the filter is a property check, not a date calculation in the query.",
    ),
    GraphQuery(
        "q8_payment_vs_sales",
        "Is a vendor's on-time payment performance related to how well its products sell?",
        """SELECT name AS vendor,
       count(->billed->invoice[WHERE is_credit_note = false AND due_date <= $as_of]) AS invoices_due,
       math::round(math::mean(->billed->invoice[WHERE is_credit_note = false AND due_date <= $as_of]
           .map(|$i| IF $i.payment_status = 'paid' AND $i.payment_date <= $i.due_date THEN 100 ELSE 0 END))) AS on_time_pct,
       math::round(math::sum(->supplies->product<-purchased[WHERE recognised = true].net_revenue)) AS product_sales
FROM vendor
ORDER BY vendor""",
        "Both measures come from traversals: invoices billed by the vendor, and sales of the products it "
        "supplies. SurrealQL has no correlation function, so the report computes Pearson r from these "
        "rows (`pearson_r`), then leaves out one vendor at a time (`correlation_summary`) to show whether "
        "a single vendor drives the result. With ten vendors the coefficient is indicative, not significant.",
    ),
)


def pearson_r(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation of paired values, rounded to 3 decimals.

    Pairs with a missing value are dropped. Returns ``None`` with fewer than three pairs or
    when either side has no variance.
    """
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    return round(sxy / math.sqrt(sxx * syy), 3) if sxx and syy else None


def leave_one_out(xs: list[float], ys: list[float]) -> list[float | None]:
    """Pearson r recomputed with each point left out in turn."""
    return [pearson_r(xs[:i] + xs[i + 1:], ys[:i] + ys[i + 1:]) for i in range(len(xs))]


def correlation_summary(rows: list[dict[str, Any]], x: str, y: str, label: str, size: str) -> str:
    """Pearson r of ``x`` vs ``y`` and how much it depends on any single row.

    A coefficient over a handful of points can be set by one of them, so the sentence names the
    row whose removal moves r the most, with its sample ``size``, and says whether the sign flips.
    """
    xs, ys = [row.get(x) for row in rows], [row.get(y) for row in rows]
    r = pearson_r(xs, ys)
    if r is None:
        return f"**Pearson r ({x} vs {y}):** not computable from {len(rows)} rows."
    text = f"**Pearson r ({x} vs {y}): {r}** across {len(rows)} {label}s."
    without = [(r_i, row) for r_i, row in zip(leave_one_out(xs, ys), rows) if r_i is not None]
    if not without:
        return text
    r_out, row = max(without, key=lambda pair: abs(pair[0] - r))
    low, high = min(v for v, _ in without), max(v for v, _ in without)
    text += (f" Leaving out one {label} at a time gives r from {low} to {high}; the largest change comes from "
             f"dropping {row.get(label)} ({row.get(size)} {size.replace('_', ' ')}): r = {r_out}.")
    if (r_out > 0) != (r > 0) or abs(r_out - r) > 0.5:
        text += f" One {label} decides the result, so no relationship can be claimed from this data."
    return text


def as_rows(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    return [result] if isinstance(result, dict) else list(result)
