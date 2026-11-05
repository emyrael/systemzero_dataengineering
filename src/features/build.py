"""Gold features: one table per entity, computed from Silver ``clean``.

Definitions, formulas and business rules are documented in ``docs/FEATURES.md``.
Every time-based feature is measured against ``run.as_of_date`` (never "now"), and the
observation window starts at the first transaction in the data, so reruns are identical.

Revenue rule: a transaction counts when its payment was captured (``payment_status ==
"paid"``) and the order was not cancelled. Amounts are merchandise net of discount,
excluding tax and shipping; returns count negatively.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from loguru import logger

from src.cleaning.clean import CLEAN_SUBDIR
from src.utils.config import PipelineConfig
from src.utils.io import write_parquet

REVENUE_PAYMENT_STATUS = "paid"
EXCLUDED_ORDER_STATUSES = frozenset({"cancelled"})
PAID_INVOICE_STATUS = "paid"
PRICE_TIERS = ("Low", "Medium", "High", "Premium")
DAYS_PER_MONTH = 365.25 / 12
COST_TOLERANCE = 0.01
SAMPLE_FILE = "feature_samples.md"

Tables = dict[str, pd.DataFrame]


@dataclass(frozen=True)
class Window:
    start: pd.Timestamp  # first transaction in the data
    as_of: pd.Timestamp

    def months_since(self, start: pd.Series) -> pd.Series:
        """Months from ``start`` (clipped to the window start) to as_of; at least one day."""
        begin = start.fillna(self.start).clip(lower=self.start)
        return ((self.as_of - begin).dt.days.clip(lower=1)) / DAYS_PER_MONTH


def _ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def _quintile(values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """Score 1–5 by rank (ties broken by row order, so scores are deterministic)."""
    ranks = (values if higher_is_better else -values).rank(method="first")
    bins = min(5, len(values))
    if bins == 0:
        return pd.Series(dtype="Int64")
    scores = pd.qcut(ranks, bins, labels=False) + 1
    return (scores * 5 // bins if bins < 5 else scores).astype("Int64")


def recognised_sales(transactions: pd.DataFrame) -> pd.DataFrame:
    """Revenue-bearing lines with ``net_revenue`` (returns negative) and ``net_units``."""
    keep = transactions["payment_status"].eq(REVENUE_PAYMENT_STATUS) & ~transactions["order_status"].isin(
        EXCLUDED_ORDER_STATUSES
    )
    sales = transactions.loc[keep].copy()
    sign = np.where(sales["is_return"], -1.0, 1.0)
    sales["net_revenue"] = sign * (sales["subtotal"] - sales["discount_amount"])
    sales["net_units"] = sales["quantity"]
    sales["transaction_date"] = _ts(sales["transaction_date"])
    return sales


# ── customers ────────────────────────────────────────────────────────────────────


def _rfm_segment(r: int, score: int) -> str:
    if score >= 13:
        return "Champions"
    if r >= 4:
        return "Loyal" if score >= 9 else "Promising"
    if r <= 2:
        return "At risk" if score >= 9 else "Hibernating"
    return "Needs attention"


def customer_features(
    customers: pd.DataFrame, sales: pd.DataFrame, first_seen: pd.Series, window: Window, lifetime_months: int
) -> pd.DataFrame:
    """``first_seen``: customer_id -> date of the customer's first transaction of any status."""
    purchases = sales[~sales["is_return"]].groupby("customer_id")
    f = customers[["customer_id", "segment", "registration_date"]].copy()
    f["total_orders"] = f["customer_id"].map(purchases["order_id"].nunique()).fillna(0).astype("Int64")
    f["total_spend"] = f["customer_id"].map(sales.groupby("customer_id")["net_revenue"].sum()).fillna(0.0).round(2)
    # Customers can transact before their registration date in this data; tenure starts at
    # whichever came first, and never before the observation window.
    started = pd.concat([_ts(f["registration_date"]), f["customer_id"].map(first_seen)], axis=1).min(axis=1)
    tenure_months = window.months_since(started)
    f["tenure_months"] = tenure_months.round(2)
    f["purchase_frequency"] = (f["total_orders"] / tenure_months).astype("float64").round(4)
    f["average_order_value"] = f["customer_id"].map(purchases["net_revenue"].mean()).round(2)
    f["customer_lifetime_value"] = (f["total_spend"].clip(lower=0) / tenure_months * lifetime_months).round(2)
    last = f["customer_id"].map(purchases["transaction_date"].max())
    f["days_since_last_purchase"] = (window.as_of - last).dt.days.astype("Int64")

    buyers = f["total_orders"] > 0
    scored = f.loc[buyers]
    r = _quintile(scored["days_since_last_purchase"].astype("float64"), higher_is_better=False)
    fq = _quintile(scored["total_orders"].astype("float64"))
    m = _quintile(scored["total_spend"])
    f["rfm_recency"], f["rfm_frequency"], f["rfm_monetary"] = r, fq, m
    f["customer_segment_score"] = (r + fq + m).astype("Int64")
    f["rfm_segment"] = "No purchases"
    f.loc[buyers, "rfm_segment"] = [
        _rfm_segment(int(rv), int(score)) for rv, score in zip(r, f.loc[buyers, "customer_segment_score"])
    ]
    return f.drop(columns=["registration_date"])


# ── products ─────────────────────────────────────────────────────────────────────


def product_features(
    products: pd.DataFrame, vendors: pd.DataFrame, sales: pd.DataFrame, window: Window, turnover_days: int
) -> pd.DataFrame:
    """One row per product: sales volume, revenue, velocity, turnover, price tier, weighted score.

    ``sales`` must already be the recognised sales (see ``recognised_sales``), so returns reduce
    units and revenue. Velocity is sale lines per month since the product was created (or the
    window start, if later). Turnover is units sold in the last ``turnover_days`` divided by
    current stock (null when stock <= 0). Price tiers are price quartiles.
    """
    by_product = sales.groupby("product_id")
    f = products[["product_id", "product_name", "category", "vendor_id", "price", "rating", "stock_quantity",
                  "is_active"]].copy()
    f["units_sold"] = f["product_id"].map(by_product["net_units"].sum()).fillna(0).astype("Int64")
    f["revenue_contribution"] = f["product_id"].map(by_product["net_revenue"].sum()).fillna(0.0).round(2)
    total = sales["net_revenue"].sum()
    f["revenue_share_pct"] = (100 * f["revenue_contribution"] / total).round(4) if total else 0.0

    sale_lines = sales[~sales["is_return"]].groupby("product_id").size()
    f["velocity_score"] = (f["product_id"].map(sale_lines).fillna(0) / window.months_since(
        _ts(products["created_date"]))).round(4)

    recent = sales[~sales["is_return"] & (sales["transaction_date"] > window.as_of - pd.Timedelta(days=turnover_days))]
    recent_units = f["product_id"].map(recent.groupby("product_id")["net_units"].sum()).fillna(0)
    stock = f["stock_quantity"].astype("float64").where(f["stock_quantity"] > 0)
    f["stock_turnover_rate"] = (recent_units / stock).round(4)

    f["price_tier"] = pd.qcut(f["price"], len(PRICE_TIERS), labels=list(PRICE_TIERS)).astype("string")
    reliability = f["vendor_id"].map(vendors.set_index("vendor_id")["reliability_score"])
    f["vendor_reliability_weighted_score"] = (f["rating"] * reliability / 100).round(4)
    return f


def price_tier_bounds(products: pd.DataFrame) -> list[float]:
    """Quartile boundaries behind ``price_tier`` (documented in the sample output)."""
    return [round(float(b), 2) for b in pd.qcut(products["price"], len(PRICE_TIERS), retbins=True)[1]]


# ── vendors ──────────────────────────────────────────────────────────────────────


def vendor_features(
    vendors: pd.DataFrame, product_feats: pd.DataFrame, invoices: pd.DataFrame, window: Window
) -> pd.DataFrame:
    """One row per vendor, aggregated from its products and its invoices.

    ``invoice_payment_rate`` is the share of invoices already due at ``window.as_of`` that were
    paid on or before their due date; credit notes are excluded from every invoice measure.
    """
    f = vendors[["vendor_id", "vendor_name", "status", "reliability_score"]].copy()
    by_vendor = product_feats.groupby("vendor_id")
    active = product_feats[product_feats["is_active"].fillna(False)].groupby("vendor_id").size()
    f["total_products_supplied"] = f["vendor_id"].map(active).fillna(0).astype("Int64")
    f["revenue_generated"] = f["vendor_id"].map(by_vendor["revenue_contribution"].sum()).fillna(0.0).round(2)
    f["product_quality_score"] = f["vendor_id"].map(by_vendor["rating"].mean()).round(4)

    bills = invoices[~invoices["is_credit_note"]].copy()
    due, paid_on = _ts(bills["due_date"]), _ts(bills["payment_date"])
    bills["is_due"] = due <= window.as_of
    bills["on_time"] = bills["payment_status"].eq(PAID_INVOICE_STATUS) & (paid_on <= due)
    falling_due = bills[bills["is_due"]].groupby("vendor_id")
    f["invoice_payment_rate"] = (100 * f["vendor_id"].map(falling_due["on_time"].mean())).round(2)
    f["average_invoice_value"] = f["vendor_id"].map(bills.groupby("vendor_id")["total_amount"].mean()).round(2)
    unpaid = bills[bills["payment_status"].ne(PAID_INVOICE_STATUS)].groupby("vendor_id")["balance_due"].sum()
    f["total_outstanding_balance"] = f["vendor_id"].map(unpaid).fillna(0.0).round(2)
    return f


# ── invoices ─────────────────────────────────────────────────────────────────────


def invoice_features(invoices: pd.DataFrame, lines: pd.DataFrame, products: pd.DataFrame, window: Window) -> pd.DataFrame:
    """One row per invoice: payment timing, overdue days, line diversity and reconciliation.

    ``days_overdue`` is counted from the due date to ``window.as_of`` for unpaid invoices only.
    Reconciliation compares each line's unit cost with the catalog cost and uses the amount
    checks from validation; invoices with no catalog lines get no flag
    (``reconciliation_note = no_catalog_lines``).
    """
    f = invoices[["invoice_id", "vendor_id", "invoice_date", "due_date", "payment_status", "total_amount"]].copy()
    invoiced, due, paid_on = _ts(f["invoice_date"]), _ts(f["due_date"]), _ts(invoices["payment_date"])
    days_to_payment = (paid_on - invoiced).dt.days
    f["days_to_payment"] = days_to_payment.where(days_to_payment >= 0).astype("Int64")  # negative: flagged in Silver
    unpaid = f["payment_status"].ne(PAID_INVOICE_STATUS)
    f["days_overdue"] = (window.as_of - due).dt.days.clip(lower=0).where(unpaid).astype("Int64")
    f["line_item_diversity"] = f["invoice_id"].map(lines.groupby("invoice_id")["product_id"].nunique()).fillna(0).astype("Int64")

    cost = lines["product_id"].map(products.set_index("product_id")["cost"])
    checkable = lines.assign(cost=cost).dropna(subset=["cost"])
    checkable = checkable.assign(
        standard=checkable["quantity"].abs() * checkable["cost"],
        billed=checkable["line_total"].abs(),
        off=(checkable["unit_cost"].abs() - checkable["cost"]).abs() > COST_TOLERANCE,
    ).groupby("invoice_id")[["standard", "billed", "off"]].sum()
    standard = f["invoice_id"].map(checkable["standard"])
    discount = (100 * (standard - f["invoice_id"].map(checkable["billed"])) / standard).round(4)
    f["discount_rate_achieved"] = discount + 0.0  # normalise -0.0

    has_lines = f["invoice_id"].isin(checkable.index)
    cost_ok = f["invoice_id"].map(checkable["off"]).eq(0)
    amounts_ok = ~invoices["dq_flags"].map(lambda flags: bool({"invoices.total_mismatch", "invoices.subtotal_mismatch"} & set(flags)))
    f["reconciliation_flag"] = (cost_ok & amounts_ok).where(has_lines).astype("boolean")
    f["reconciliation_note"] = np.select(
        [~has_lines, ~cost_ok, ~amounts_ok], ["no_catalog_lines", "cost_mismatch", "total_mismatch"], "matched"
    )
    return f


# ── stage ────────────────────────────────────────────────────────────────────────


def build_features(tables: Tables, config: PipelineConfig) -> Tables:
    """Build the four Gold feature tables from Silver ``clean`` tables.

    The window runs from the first transaction to ``run.as_of_date``. Every table uses the same
    recognised sales, so revenue agrees across customers, products and vendors.
    """
    tx = tables["transactions"]
    window = Window(start=_ts(tx["transaction_date"]).min(), as_of=pd.Timestamp(config.run.as_of_date))
    sales = recognised_sales(tx)
    first_seen = _ts(tx["transaction_date"]).groupby(tx["customer_id"]).min()
    products = product_features(tables["products"], tables["vendors"], sales, window,
                                config.features.turnover_window_days)
    return {
        "customer_features": customer_features(tables["customers"], sales, first_seen, window,
                                               config.features.customer_lifetime_months),
        "product_features": products,
        "vendor_features": vendor_features(tables["vendors"], products, tables["invoices"], window),
        "invoice_features": invoice_features(tables["invoices"], tables["invoice_lines"], tables["products"], window),
    }


def render_samples(features: Tables, as_of: date, tier_bounds: list[float]) -> str:
    """Markdown sample of each feature table: the five largest rows by its headline metric."""
    headline = {
        "customer_features": "customer_lifetime_value",
        "product_features": "revenue_contribution",
        "vendor_features": "revenue_generated",
        "invoice_features": "total_amount",
    }
    out = [
        "# Feature Samples", "",
        f"Generated by `python run_pipeline.py` (as_of_date **{as_of}**). Top 5 rows of each Gold table by "
        "its headline metric; full tables in `outputs/processed/gold/`. Definitions: `docs/FEATURES.md`.", "",
        "Price tier boundaries (quartiles of catalog price): "
        + ", ".join(f"{t} ≤ {b}" for t, b in zip(PRICE_TIERS, tier_bounds[1:])) + ".", "",
    ]
    for name, df in features.items():
        top = df.sort_values([headline[name], df.columns[0]], ascending=[False, True]).head(5)
        out += [f"## `{name}` ({len(df):,} rows)", "", *_markdown_table(top), ""]
    return "\n".join(out)


def _markdown_table(df: pd.DataFrame) -> list[str]:
    def cell(value: object) -> str:
        if value is None or value is pd.NA or (isinstance(value, float) and np.isnan(value)):
            return "–"
        return f"{value:,.2f}" if isinstance(value, float) else str(value)

    rows = [" | ".join(cell(v) for v in row) for row in df.itertuples(index=False)]
    return ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns), *(f"| {r} |" for r in rows)]


def run_features(config: PipelineConfig) -> Tables:
    """Features stage: read Silver ``clean``, write the Gold Parquet tables and the sample report."""
    clean_dir = config.paths.silver_dir / CLEAN_SUBDIR
    needed = ("customers", "products", "vendors", "transactions", "invoices", "invoice_lines")
    tables = {name: pd.read_parquet(clean_dir / f"{name}.parquet") for name in needed}
    features = build_features(tables, config)
    for name, df in features.items():
        write_parquet(config.paths.gold_dir / f"{name}.parquet", df)
        logger.bind(stage="features", source=name).info("Features written", rows=len(df), columns=len(df.columns))
    samples = render_samples(features, config.run.as_of_date, price_tier_bounds(tables["products"]))
    (config.paths.output_dir / SAMPLE_FILE).write_text(samples)
    return features
