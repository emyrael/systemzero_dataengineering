from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.features.build import (
    Window,
    _quintile,
    customer_features,
    invoice_features,
    product_features,
    recognised_sales,
    vendor_features,
)

WINDOW = Window(start=pd.Timestamp("2024-01-01"), as_of=pd.Timestamp("2025-01-01"))


def tx(customer: str, product: str, day: str, subtotal: float, *, qty: int = 1, paid: str = "paid",
       order: str = "completed", discount: float = 0.0) -> dict:
    return {"order_id": f"O-{customer}-{day}-{qty}", "customer_id": customer, "product_id": product,
            "transaction_date": date.fromisoformat(day), "quantity": qty, "is_return": qty < 0,
            "subtotal": subtotal, "discount_amount": discount, "payment_status": paid, "order_status": order}


def test_recognised_sales_keep_captured_payments_and_net_out_returns():
    sales = recognised_sales(pd.DataFrame([
        tx("C1", "P1", "2024-03-01", 100, discount=10),
        tx("C1", "P1", "2024-03-02", 100, paid="failed"),
        tx("C1", "P1", "2024-03-03", 100, order="cancelled"),
        tx("C1", "P1", "2024-03-04", 40, qty=-1),
    ]))
    assert sales["net_revenue"].tolist() == [90.0, -40.0]
    assert sales["net_units"].tolist() == [1, -1]


def test_quintile_scores_are_1_to_5_and_respect_direction():
    values = pd.Series([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    assert _quintile(values).tolist() == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    assert _quintile(values, higher_is_better=False).tolist() == [5, 5, 4, 4, 3, 3, 2, 2, 1, 1]
    assert _quintile(pd.Series([5.0, 1.0])).tolist() == [5, 2]  # fewer than five values still scale to 1–5


def test_customer_features():
    customers = pd.DataFrame({
        "customer_id": ["C1", "C2", "C3"], "segment": ["gold", "bronze", "silver"],
        "registration_date": [date(2024, 7, 1), date(2020, 1, 1), date(2024, 1, 1)],
    })
    transactions = pd.DataFrame([
        tx("C1", "P1", "2024-02-01", 300),  # bought before registering
        tx("C1", "P1", "2024-12-01", 100),
        tx("C2", "P1", "2024-06-01", 50, qty=-1),  # only a return
    ])
    sales = recognised_sales(transactions)
    first_seen = pd.to_datetime(transactions["transaction_date"]).groupby(transactions["customer_id"]).min()
    f = customer_features(customers, sales, first_seen, WINDOW, lifetime_months=12).set_index("customer_id")

    c1 = f.loc["C1"]
    assert c1["tenure_months"] == pytest.approx(335 / 30.4375, abs=0.01)  # from first purchase, not registration
    assert (c1["total_orders"], c1["total_spend"], c1["average_order_value"]) == (2, 400.0, 200.0)
    assert c1["days_since_last_purchase"] == 31
    assert c1["customer_lifetime_value"] == pytest.approx(400 / (335 / 30.4375) * 12, abs=0.01)
    assert f.loc["C2", "tenure_months"] == pytest.approx(366 / 30.4375, abs=0.01)  # clipped to window start
    assert f.loc["C2", "customer_lifetime_value"] == 0  # negative spend never yields negative CLV
    assert f.loc["C3", "rfm_segment"] == "No purchases" and pd.isna(f.loc["C3", "customer_segment_score"])


def products_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "product_id": ["P1", "P2", "P3", "P4"], "product_name": list("abcd"), "category": ["x"] * 4,
        "vendor_id": ["V1", "V1", "V2", "V2"], "price": [10.0, 20.0, 30.0, None], "cost": [5.0, 10.0, 15.0, 1.0],
        "rating": [4.0, 2.0, None, 3.0], "stock_quantity": pd.array([10, 0, 5, -1], dtype="Int64"),
        "is_active": [True, True, False, True], "created_date": [date(2023, 1, 1), date(2024, 7, 1), None, None],
    })


def test_product_features():
    vendors = pd.DataFrame({"vendor_id": ["V1", "V2"], "reliability_score": [50.0, None]})
    sales = recognised_sales(pd.DataFrame([
        tx("C1", "P1", "2024-06-01", 30, qty=3), tx("C1", "P1", "2024-07-01", 10, qty=-1),
        tx("C1", "P2", "2024-08-01", 60, qty=3),
    ]))
    f = product_features(products_frame(), vendors, sales, WINDOW, turnover_days=365).set_index("product_id")

    assert (f.loc["P1", "units_sold"], f.loc["P1", "revenue_contribution"]) == (2, 20.0)
    assert f.loc["P1", "stock_turnover_rate"] == 0.3  # 3 units sold (returns not netted) / 10 in stock
    assert pd.isna(f.loc["P2", "stock_turnover_rate"]) and pd.isna(f.loc["P4", "stock_turnover_rate"])
    assert f.loc["P2", "velocity_score"] > f.loc["P1", "velocity_score"]  # same sales, fewer months on sale
    assert f["revenue_share_pct"].sum() == pytest.approx(100)
    assert f.loc["P1", "vendor_reliability_weighted_score"] == 2.0
    assert pd.isna(f.loc["P3", "vendor_reliability_weighted_score"]) and pd.isna(f.loc["P4", "price_tier"])


def invoices_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "invoice_id": ["I1", "I2", "I3", "I4"], "vendor_id": ["V1", "V1", "V1", "V1"],
        "invoice_date": [date(2024, 1, 1)] * 4,
        "due_date": [date(2024, 1, 31), date(2024, 1, 31), date(2024, 1, 31), date(2025, 3, 1)],
        "payment_date": [date(2024, 1, 20), date(2024, 2, 15), None, date(2023, 12, 1)],
        "payment_status": ["paid", "paid", "overdue", "paid"], "total_amount": [100.0, 200.0, 300.0, 400.0],
        "balance_due": [0.0, 0.0, 120.0, 0.0], "is_credit_note": [False] * 4,
        "dq_flags": [[], ["invoices.total_mismatch"], [], ["invoices.paid_before_invoiced"]],
    })


def test_vendor_features():
    vendors = pd.DataFrame({"vendor_id": ["V1", "V2"], "vendor_name": ["a", "b"], "status": ["active"] * 2,
                            "reliability_score": [90.0, 80.0]})
    product_feats = pd.DataFrame({"vendor_id": ["V1", "V1"], "is_active": [True, False],
                                  "revenue_contribution": [10.0, 5.0], "rating": [4.0, 2.0]})
    f = vendor_features(vendors, product_feats, invoices_frame(), WINDOW).set_index("vendor_id")
    v1 = f.loc["V1"]
    assert (v1["total_products_supplied"], v1["revenue_generated"], v1["product_quality_score"]) == (1, 15.0, 3.0)
    assert v1["invoice_payment_rate"] == pytest.approx(100 / 3, abs=0.01)  # I1 on time of I1–I3 (I4 not yet due)
    assert (v1["average_invoice_value"], v1["total_outstanding_balance"]) == (250.0, 120.0)
    assert f.loc["V2", "total_outstanding_balance"] == 0 and pd.isna(f.loc["V2", "invoice_payment_rate"])


def test_invoice_features():
    lines = pd.DataFrame({
        "invoice_id": ["I1", "I1", "I2", "I3"], "product_id": ["P1", "P2", "P1", None],
        "quantity": [2, -1, 1, 5], "unit_cost": [5.0, -10.0, 4.0, 7.0], "line_total": [10.0, -10.0, 4.0, 35.0],
    })
    f = invoice_features(invoices_frame(), lines, products_frame(), WINDOW).set_index("invoice_id")
    assert f["days_to_payment"].tolist()[:2] == [19, 45] and pd.isna(f.loc["I4", "days_to_payment"])
    assert f.loc["I3", "days_overdue"] == 336 and pd.isna(f.loc["I1", "days_overdue"])
    assert f["line_item_diversity"].tolist() == [2, 1, 0, 0]
    assert f.loc["I1", "discount_rate_achieved"] == 0 and f.loc["I2", "discount_rate_achieved"] == 20
    assert f["reconciliation_note"].tolist() == ["matched", "cost_mismatch", "no_catalog_lines", "no_catalog_lines"]
    assert f.loc["I1", "reconciliation_flag"] and not f.loc["I2", "reconciliation_flag"]
    assert pd.isna(f.loc["I3", "reconciliation_flag"])
