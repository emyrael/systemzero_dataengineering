from __future__ import annotations

import pandas as pd
import pytest

from src.cleaning.clean import (
    CleaningMetrics,
    check_consistency,
    check_references,
    clean_tables,
    deduplicate,
    fix_currencies,
    infer_categories,
    mark,
)


def frame(*rows: dict) -> pd.DataFrame:
    """Silver-shaped rows: given columns plus empty dq lists and minimal lineage."""
    df = pd.DataFrame(list(rows))
    df["_source_row"] = range(1, len(df) + 1)
    for column in ("_batch_id", "_ingested_at", "_source_file", "_record_key"):
        df[column] = "x"
    df["_record_hash"] = [f"h{i}" for i in range(len(df))]
    df["dq_fixes"] = [[] for _ in range(len(df))]
    df["dq_flags"] = [[] for _ in range(len(df))]
    return df


def metrics(*entities: str) -> dict[str, CleaningMetrics]:
    return {name: CleaningMetrics(entity=name) for name in entities}


def test_mark_appends_to_the_list_matching_the_rule_action():
    df, m = frame({"a": 1}, {"a": 2}), CleaningMetrics("products")
    mark(df, df["a"] == 2, "products.sku_conflict", m)
    mark(df, df["a"] == 2, "products.category_from_name", m)
    assert df["dq_flags"].tolist() == [[], ["products.sku_conflict"]]
    assert df["dq_fixes"].tolist() == [[], ["products.category_from_name"]]
    assert m.flags["products.sku_conflict"] == 1


def test_mark_refuses_rules_that_do_not_mark_rows():
    df = frame({"a": 1})
    with pytest.raises(ValueError, match="drop"):
        mark(df, df["a"] == 1, "products.duplicate", CleaningMetrics("products"))
    with pytest.raises(KeyError):
        mark(df, df["a"] == 1, "products.not_a_rule", CleaningMetrics("products"))


def test_deduplicate_keeps_latest_version_and_logs_the_rest():
    df = frame(
        {"customer_id": "C1", "updated_at": pd.Timestamp("2024-01-01")},
        {"customer_id": "C2", "updated_at": pd.Timestamp("2024-01-01")},
        {"customer_id": "C1", "updated_at": pd.Timestamp("2024-06-01")},
    )
    df.loc[2, "_record_hash"] = "h0"  # row 3 has the same content hash as row 1
    kept, log = deduplicate("customers", df, ("customer_id",))

    assert kept["_source_row"].tolist() == [2, 3]  # original order, C1's later version wins
    assert log[["rule", "_source_row", "kept_source_row"]].values.tolist() == [["customers.duplicate", 1, 3]]
    assert log["identical_to_kept"].tolist() == [True]


def test_deduplicate_without_updated_at_keeps_first_seen():
    df = frame({"product_id": "P1"}, {"product_id": "P1"})
    kept, log = deduplicate("products", df, ("product_id",))
    assert kept["_source_row"].tolist() == [1]
    assert log["identical_to_kept"].tolist() == [False]


def test_references_flag_orphans_but_not_missing_values():
    tables = {
        "customers": frame({"customer_id": "C1"}),
        "products": frame({"product_id": "P1", "vendor_id": "V1"}),
        "vendors": frame({"vendor_id": "V1"}),
        "transactions": frame(
            {"customer_id": "C1", "product_id": "P1"},
            {"customer_id": "C9", "product_id": None},
        ),
    }
    m = metrics(*tables)
    check_references(tables, m)
    assert tables["transactions"]["dq_flags"].tolist() == [[], ["transactions.unknown_customer"]]
    assert m["transactions"].flags == {"transactions.unknown_customer": 1}


def test_references_skip_checks_when_the_parent_table_is_missing():
    tables = {"transactions": frame({"customer_id": "C9", "product_id": "P9"})}
    check_references(tables, metrics("transactions"))
    assert tables["transactions"]["dq_flags"].tolist() == [[]]


def test_shared_values_and_unverified_purchases_are_flagged():
    tables = {
        "customers": frame({"email": "a@x.co"}, {"email": "a@x.co"}, {"email": None}, {"email": None}),
        "transactions": frame({"customer_id": "C1", "product_id": "P1"}),
        "product_reviews": frame(
            {"customer_id": "C1", "product_id": "P1", "verified_purchase": True},
            {"customer_id": "C2", "product_id": "P1", "verified_purchase": True},
            {"customer_id": "C3", "product_id": "P1", "verified_purchase": False},
        ),
    }
    check_consistency(tables, metrics(*tables))
    assert tables["customers"]["dq_flags"].map(len).tolist() == [1, 1, 0, 0]
    assert tables["product_reviews"]["dq_flags"].tolist() == [[], ["product_reviews.unverified_purchase"], []]


def test_currency_labels_are_relabelled_when_amounts_match_the_usd_catalog():
    tables = {
        "products": frame({"product_id": "P1", "price": 10.0, "cost": 4.0}),
        "transactions": frame(
            {"product_id": "P1", "unit_price": 10.0, "currency": "EUR"},
            {"product_id": "P9", "unit_price": 10.0, "currency": "GBP"},
            {"product_id": "P1", "unit_price": 10.0, "currency": "USD"},
        ),
        "invoices": frame({"invoice_id": "I1", "currency": "EUR"}, {"invoice_id": "I2", "currency": "CAD"}),
        "invoice_lines": frame(
            {"invoice_id": "I1", "product_id": "P1", "unit_cost": -4.0},  # credit line
            {"invoice_id": "I2", "product_id": None, "unit_cost": 7.0},
        ),
    }
    m = metrics(*tables)
    fix_currencies(tables, "USD", m)

    tx, inv = tables["transactions"], tables["invoices"]
    assert tx["currency"].tolist() == ["USD"] * 3 and tx["src_currency"].tolist() == ["EUR", "GBP", "USD"]
    assert tx["dq_fixes"].tolist() == [["transactions.currency_relabelled"], [], []]
    assert tx["dq_flags"].tolist() == [[], ["transactions.currency_unverified"], []]
    assert inv["currency"].tolist() == ["USD", "USD"]
    assert inv["dq_fixes"].tolist() == [["invoices.currency_relabelled"], []]
    assert inv["dq_flags"].tolist() == [[], ["invoices.currency_unverified"]]


def test_category_is_inferred_only_from_unambiguous_names():
    products = frame(
        {"product_name": "Ultra Power Bank", "category": "Electronics"},
        {"product_name": "Basic Power Bank", "category": None},
        {"product_name": "Smart Lamp", "category": "Home"},
        {"product_name": "Retro Lamp", "category": "Decor"},
        {"product_name": "Cheap Lamp", "category": None},
    )
    infer_categories(products, CleaningMetrics("products"))
    assert products["category"].iloc[:4].tolist() == ["Electronics", "Electronics", "Home", "Decor"]
    assert pd.isna(products.loc[4, "category"])  # "Lamp" maps to two categories


def test_clean_tables_counts_rows_and_records(config):
    tables = {"vendors": frame({"vendor_id": "V1"}, {"vendor_id": "V1"}, {"vendor_id": "V2"})}
    cleaned, removed, m = clean_tables(tables, config)
    assert (m["vendors"].rows_in, m["vendors"].rows_out) == (3, 2)
    assert removed["entity"].tolist() == ["vendors"]
    assert m["vendors"].removals == {"vendors.duplicate": 1}
