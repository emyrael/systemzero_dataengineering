"""Map Silver ``clean`` and Gold feature tables to graph nodes and edges.

Pure data transformation: no database access, so it is unit-tested directly. Record ids
are business keys; an edge is only built when both ends exist as nodes, and every edge
skipped for a missing end is counted (``Graph.skipped``) rather than silently dropped.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from itertools import permutations
from typing import Any

import numpy as np
import pandas as pd
from surrealdb import RecordID

from src.features.build import recognised_sales

Record = dict[str, Any]

NODE_TABLES = (
    "vendor", "product", "customer", "category", "country", "region", "invoice",
    "support_ticket", "review", "call",
)
EDGE_TABLES = (
    "supplies", "belongs_to", "located_in", "part_of", "purchased", "billed", "invoice_item",
    "similar_to", "opened", "wrote", "about", "has_call", "called",
)


@dataclass
class Graph:
    nodes: dict[str, list[Record]] = field(default_factory=lambda: {t: [] for t in NODE_TABLES})
    edges: dict[str, list[Record]] = field(default_factory=lambda: {t: [] for t in EDGE_TABLES})
    skipped: Counter = field(default_factory=Counter)  # "<edge>:<missing end>" -> count


def value(v: Any) -> Any:
    """Convert pandas/numpy values to plain Python; dates become UTC datetimes; missing -> None."""
    if isinstance(v, np.ndarray):
        v = v.tolist()
    if isinstance(v, (list, tuple)):
        return [value(x) for x in v]
    if isinstance(v, dict):
        return {k: value(x) for k, x in v.items()}
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        v = v.to_pydatetime()
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    return v


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")


def rows(df: pd.DataFrame, columns: dict[str, str]) -> list[Record]:
    """Records with ``columns`` renamed (source -> graph field) and values converted."""
    subset = df[list(columns)].rename(columns=columns)
    return [{k: value(v) for k, v in rec.items()} for rec in subset.to_dict("records")]


def _text_list(v: Any) -> list[str]:
    items = value(v)
    return [str(x) for x in items] if isinstance(items, list) else []


class _Builder:
    def __init__(self, batch_id: str):
        self.graph, self.batch_id = Graph(), batch_id
        self.ids: dict[str, set[str]] = {t: set() for t in NODE_TABLES}

    def node(self, table: str, key: str, record: Record) -> None:
        if key in self.ids[table]:
            return
        self.ids[table].add(key)
        self.graph.nodes[table].append({**record, "id": RecordID(table, key), "batch_id": self.batch_id})

    def edge(self, table: str, key: str, src: tuple[str, Any], dst: tuple[str, Any], record: Record | None = None) -> None:
        for end, (node_table, node_key) in (("in", src), ("out", dst)):
            if node_key is None or node_key not in self.ids[node_table]:
                self.graph.skipped[f"{table}:{end}={node_table}"] += 1
                return
        self.graph.edges[table].append({
            **(record or {}), "id": RecordID(table, key), "in": RecordID(src[0], src[1]),
            "out": RecordID(dst[0], dst[1]), "batch_id": self.batch_id,
        })


def build_graph(silver: dict[str, pd.DataFrame], gold: dict[str, pd.DataFrame], country_regions: dict[str, str],
                batch_id: str) -> Graph:
    b = _Builder(batch_id)
    vendors = silver["vendors"].merge(gold["vendor_features"].drop(columns=["vendor_name", "status", "reliability_score"]),
                                      on="vendor_id", how="left")
    products = silver["products"].merge(
        gold["product_features"][["product_id", "units_sold", "revenue_contribution", "revenue_share_pct",
                                  "velocity_score", "stock_turnover_rate", "price_tier",
                                  "vendor_reliability_weighted_score"]], on="product_id", how="left")
    customers = silver["customers"].merge(gold["customer_features"].drop(columns=["segment"]), on="customer_id", how="left")
    invoices = silver["invoices"].merge(
        gold["invoice_features"][["invoice_id", "days_to_payment", "days_overdue", "line_item_diversity",
                                  "discount_rate_achieved", "reconciliation_flag", "reconciliation_note"]],
        on="invoice_id", how="left")

    # ── reference nodes ──
    for region in sorted(set(country_regions.values())):
        b.node("region", slug(region), {"name": region})
    countries = set(customers["address_country"].dropna()) | set(vendors["country"].dropna())
    for country in sorted(countries):
        b.node("country", slug(country), {"name": country})
        b.edge("part_of", slug(country), ("country", slug(country)), ("region", slug(country_regions.get(country, ""))))
    for category in sorted(products["category"].dropna().unique()):
        b.node("category", slug(category), {"name": category})

    # ── entity nodes ──
    for rec in rows(vendors, {
        "vendor_id": "key", "vendor_name": "name", "status": "status", "country": "country", "region": "region",
        "reliability_score": "reliability_score", "lead_time_days": "lead_time_days",
        "payment_terms": "payment_terms", "certifications": "certifications",
        "total_products_supplied": "total_products_supplied", "revenue_generated": "revenue_generated",
        "product_quality_score": "product_quality_score", "invoice_payment_rate": "invoice_payment_rate",
        "average_invoice_value": "average_invoice_value", "total_outstanding_balance": "total_outstanding_balance",
    }):
        rec["certifications"] = _text_list(rec["certifications"])
        rec["lead_time_days"] = None if rec["lead_time_days"] is None else int(rec["lead_time_days"])
        b.node("vendor", rec.pop("key"), rec)

    for rec in rows(products, {
        "product_id": "key", "product_name": "name", "sku": "sku", "description": "description",
        "category": "category", "subcategory": "subcategory", "tags": "tags", "price": "price", "cost": "cost",
        "rating": "rating", "stock_quantity": "stock_quantity", "is_active": "is_active", "units_sold": "units_sold",
        "revenue_contribution": "revenue_contribution", "revenue_share_pct": "revenue_share_pct",
        "velocity_score": "velocity_score", "stock_turnover_rate": "stock_turnover_rate", "price_tier": "price_tier",
        "vendor_reliability_weighted_score": "vendor_reliability_weighted_score", "dq_flags": "dq_flags",
    }):
        rec["tags"], rec["dq_flags"] = _text_list(rec["tags"]), _text_list(rec["dq_flags"])
        b.node("product", rec.pop("key"), rec)

    for rec in rows(customers, {
        "customer_id": "key", "full_name": "name", "segment": "segment", "gender": "gender", "age": "age",
        "address_city": "city", "is_active": "is_active", "registration_date": "registration_date",
        "total_orders": "total_orders", "total_spend": "total_spend", "tenure_months": "tenure_months",
        "purchase_frequency": "purchase_frequency", "average_order_value": "average_order_value",
        "customer_lifetime_value": "customer_lifetime_value", "days_since_last_purchase": "days_since_last_purchase",
        "customer_segment_score": "customer_segment_score", "rfm_segment": "rfm_segment", "dq_flags": "dq_flags",
    }):
        rec["dq_flags"] = _text_list(rec["dq_flags"])
        b.node("customer", rec.pop("key"), rec)

    for rec in rows(invoices, {
        "invoice_id": "key", "invoice_number": "number", "invoice_date": "invoice_date", "due_date": "due_date",
        "payment_terms": "payment_terms", "payment_status": "payment_status", "payment_date": "payment_date",
        "subtotal": "subtotal", "tax_amount": "tax_amount", "total_amount": "total_amount",
        "amount_paid": "amount_paid", "balance_due": "balance_due", "is_credit_note": "is_credit_note",
        "days_to_payment": "days_to_payment", "days_overdue": "days_overdue",
        "line_item_diversity": "line_item_diversity", "discount_rate_achieved": "discount_rate_achieved",
        "reconciliation_flag": "reconciliation_flag", "reconciliation_note": "reconciliation_note",
        "dq_flags": "dq_flags",
    }):
        rec["dq_flags"] = _text_list(rec["dq_flags"])
        b.node("invoice", rec.pop("key"), rec)

    _text_nodes(b, silver)

    # ── structural edges ──
    for pid, vid, category in products[["product_id", "vendor_id", "category"]].itertuples(index=False):
        b.edge("supplies", pid, ("vendor", value(vid)), ("product", pid))
        if value(category) is not None:
            b.edge("belongs_to", pid, ("product", pid), ("category", slug(category)))
    for cid, country in customers[["customer_id", "address_country"]].itertuples(index=False):
        if value(country) is not None:
            b.edge("located_in", cid, ("customer", cid), ("country", slug(country)))
    for vid, country in vendors[["vendor_id", "country"]].itertuples(index=False):
        if value(country) is not None:
            b.edge("located_in", vid, ("vendor", vid), ("country", slug(country)))
    for iid, vid in invoices[["invoice_id", "vendor_id"]].itertuples(index=False):
        b.edge("billed", iid, ("vendor", value(vid)), ("invoice", iid))

    # ── fact edges ──
    tx = silver["transactions"]
    recognised = set(recognised_sales(tx)["transaction_id"])
    sign = np.where(tx["is_return"], -1.0, 1.0)
    tx = tx.assign(net_revenue=(sign * (tx["subtotal"] - tx["discount_amount"])).round(2),
                   recognised=tx["transaction_id"].isin(recognised))
    for rec in rows(tx, {
        "transaction_id": "key", "customer_id": "customer", "product_id": "product", "order_id": "order_id",
        "transaction_date": "transaction_date", "quantity": "quantity", "unit_price": "unit_price",
        "net_revenue": "net_revenue", "total_amount": "total_amount", "is_return": "is_return",
        "recognised": "recognised", "payment_status": "payment_status", "order_status": "order_status",
        "channel": "channel", "region": "sales_region",
    }):
        key, customer, product = rec.pop("key"), rec.pop("customer"), rec.pop("product")
        b.edge("purchased", key, ("customer", customer), ("product", product), rec)

    for rec in rows(silver["invoice_lines"], {
        "invoice_id": "invoice", "line_number": "line_number", "product_id": "product", "quantity": "quantity",
        "unit_cost": "unit_cost", "line_total": "line_total", "is_credit": "is_credit",
    }):
        invoice, product = rec.pop("invoice"), rec.pop("product")
        b.edge("invoice_item", f"{invoice}_{rec['line_number']}", ("invoice", invoice), ("product", product), rec)

    # Co-purchase: products sharing buyers (recognised, non-return purchases, known products).
    bought = tx[tx["recognised"] & ~tx["is_return"] & tx["product_id"].isin(b.ids["product"])]
    shared: Counter[tuple[str, str]] = Counter()
    for products_of_customer in bought.groupby("customer_id")["product_id"].agg(lambda s: sorted(set(s))):
        shared.update(permutations(products_of_customer, 2))
    for (a, c), n in sorted(shared.items()):
        b.edge("similar_to", f"{a}_{c}", ("product", a), ("product", c), {"shared_customers": n})

    _text_edges(b, silver)
    return b.graph


def _text_nodes(b: _Builder, silver: dict[str, pd.DataFrame]) -> None:
    for rec in rows(silver["support_tickets"], {
        "ticket_id": "key", "channel": "channel", "category": "category", "priority": "priority",
        "status": "status", "created_at": "created_at", "resolved_at": "resolved_at",
        "satisfaction_score": "satisfaction_score", "resolution_type": "resolution_type",
        "summary": "summary", "transcript": "transcript", "tags": "tags",
    }):
        rec["tags"] = _text_list(rec["tags"])
        b.node("support_ticket", rec.pop("key"), rec)

    for rec in rows(silver["product_reviews"], {
        "review_id": "key", "rating": "rating", "title": "title", "review_text": "text", "sentiment": "sentiment",
        "verified_purchase": "verified_purchase", "helpful_votes": "helpful_votes", "review_date": "review_date",
        "response_text": "response_text", "dq_flags": "dq_flags",
    }):
        rec["dq_flags"] = _text_list(rec["dq_flags"])
        b.node("review", rec.pop("key"), rec)

    for rec in rows(silver["call_transcripts"], {
        "call_id": "key", "call_start": "call_start", "call_end": "call_end", "duration_seconds": "duration_seconds",
        "call_type": "call_type", "language": "language", "agent_name": "agent_name", "sentiment": "sentiment",
        "sentiment_start": "sentiment_start", "sentiment_end": "sentiment_end",
        "resolution_achieved": "resolution_achieved", "quality_score": "quality_score",
        "keywords_detected": "keywords", "utterances": "transcript",
    }):
        rec["keywords"] = _text_list(rec["keywords"])
        rec["transcript"] = "\n".join(f"{u.get('speaker')}: {u.get('text')}" for u in rec["transcript"] or [])
        b.node("call", rec.pop("key"), rec)


def _text_edges(b: _Builder, silver: dict[str, pd.DataFrame]) -> None:
    for tid, cid, pid in silver["support_tickets"][["ticket_id", "customer_id", "product_id"]].itertuples(index=False):
        b.edge("opened", tid, ("customer", value(cid)), ("support_ticket", tid))
        if value(pid) is not None:
            b.edge("about", tid, ("support_ticket", tid), ("product", pid))
    for rid, cid, pid in silver["product_reviews"][["review_id", "customer_id", "product_id"]].itertuples(index=False):
        b.edge("wrote", rid, ("customer", value(cid)), ("review", rid))
        b.edge("about", rid, ("review", rid), ("product", value(pid)))
    for call_id, tid, cid in silver["call_transcripts"][["call_id", "ticket_id", "customer_id"]].itertuples(index=False):
        if value(tid) is not None:
            b.edge("has_call", call_id, ("support_ticket", tid), ("call", call_id))
        b.edge("called", call_id, ("customer", value(cid)), ("call", call_id))
