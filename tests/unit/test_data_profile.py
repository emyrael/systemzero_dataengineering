from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from scripts.data_profile import (
    ANOMALY,
    BUSINESS,
    VIOLATION,
    RawDataset,
    Relation,
    build_profile,
    check_relationship,
    check_transactions,
    classify_date_format,
    generic_checks,
    id_shape,
    infer_type,
    numeric_stats,
    profile_column,
    render_markdown,
    spelling_variants,
    text_issues,
    write_reports,
)
from src.validation.rules import RULES

AS_OF = date(2025, 1, 1)


def dataset(name: str, records: list[dict], key: tuple[str, ...] = ("id",), fmt: str = "json") -> RawDataset:
    return RawDataset(name=name, file=f"{name}.{fmt}", format=fmt, key=key, records=records,
                      frame=pd.json_normalize(records))


def by_check(findings) -> dict:
    return {f.check: f for f in findings}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-03-05", "iso_date"), ("2024-03-05T10:00:00", "iso_datetime"), ("05/03/2024", "nn/nn/yyyy"),
        ("03-05-2024", "nn-nn-yyyy"), ("05-Mar-2024", "dd-Mon-yyyy"), ("2024/03/05", "yyyy/mm/dd"),
        ("1717020000", "epoch_seconds"), ("next tuesday", None),
    ],
)
def test_classify_date_format(value, expected):
    assert classify_date_format(value) == expected


def test_id_shape_separates_well_formed_from_malformed_ids():
    assert id_shape("PRD-000123") == id_shape("prd-999999 ") == "PRD-<6>"
    assert id_shape("INV-C58F93E9") == "INV-<8>"
    assert id_shape("CUS-INVALID-12") != id_shape("CUS-00000012")


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([1, 2, 3], "integer"), ([1, 2.5], "float"), (["1.5", "2", "x"] * 1, "string"),
        (["1.5", "2", "3"], "numeric_string"), (["true", "Yes", "0"], "boolean_string"),
        (["2024-01-01", "05/03/2024"], "date_string"), ([True, "yes"], "mixed:boolean_string"),
        ([], "empty"),
    ],
)
def test_infer_type(values, expected):
    assert infer_type(values)[0] == expected


def test_numeric_stats_counts_negatives_and_iqr_outliers():
    stats = numeric_stats([1, 2, 2, 3, 3, 3, 4, 100, -5])
    assert (stats["min"], stats["max"], stats["median"]) == (-5, 100, 3)
    assert stats["negatives"] == 1 and stats["outliers_iqr"] == 2


def test_profile_column_separates_nulls_placeholders_and_variants():
    profile = profile_column(pd.Series(["Gold", "gold", "GOLD", "silver", None, "N/A"]), AS_OF)
    assert (profile["nulls"], profile["placeholders"], profile["missing_pct"]) == (1, 1, 33.33)
    assert profile["spelling_variants"] == {"gold": {"GOLD": 1, "Gold": 1, "gold": 1}}
    assert profile["top_values"][0] == ["GOLD", 1]  # ties broken alphabetically: deterministic


def test_profile_column_reports_date_formats_and_impossible_dates():
    profile = profile_column(pd.Series(["2024-01-31", "31/01/2024", "31/02/2024", "2026-05-01"]), AS_OF)
    dates = profile["dates"]
    assert dates["formats"] == {"iso_date": 2, "nn/nn/yyyy": 2}
    assert (dates["unparseable"], dates["after_as_of"], dates["min"]) == (1, 1, "2024-01-31")


def test_text_issues_ignore_valid_unicode_but_catch_mojibake_and_whitespace():
    assert text_issues(["café", "東京"]) == {"non_ascii": 2}
    assert text_issues(["cafÃ©", " padded "]) == {"non_ascii": 1, "mojibake": 1, "untrimmed": 1}


def test_spelling_variants_group_case_space_and_underscore():
    assert spelling_variants(["In Progress", "in_progress", "open"]) == {
        "inprogress": {"In Progress": 1, "in_progress": 1}
    }


def test_generic_checks_find_exact_and_conflicting_duplicates_and_bad_keys():
    ds = dataset("vendors", [
        {"vendor_id": "VND-001", "vendor_name": "A", "status": "active"},
        {"vendor_id": "VND-001", "vendor_name": "A", "status": "active"},
        {"vendor_id": "VND-002", "vendor_name": "B", "status": "active"},
        {"vendor_id": "VND-002", "vendor_name": "B2", "status": "active"},
        {"vendor_id": "BROKEN", "vendor_name": "C", "status": "active"},
    ], key=("vendor_id",))
    columns = {c: profile_column(ds.frame[c], AS_OF) for c in ds.frame.columns}
    found = by_check(generic_checks(ds, columns))
    assert found["exact_duplicate_records"].count == 1
    assert found["conflicting_duplicate_keys"].count == 2
    assert found["malformed_primary_key"].samples == ["BROKEN"]
    assert found["malformed_primary_key"].rule == "vendors.invalid_id"


def test_returns_are_business_exceptions_and_zero_quantity_is_a_violation():
    tx = dataset("transactions", [
        {"transaction_id": "T1", "quantity": -1, "subtotal": 10, "discount_amount": 0, "tax_amount": 1,
         "shipping_cost": 0, "total_amount": -11, "unit_price": 10, "currency": "USD", "product_id": "P1"},
        {"transaction_id": "T2", "quantity": 0, "subtotal": 0, "discount_amount": 0, "tax_amount": 0,
         "shipping_cost": 5, "total_amount": 5, "unit_price": 10, "currency": "USD", "product_id": "P1"},
    ], key=("transaction_id",))
    found = by_check(check_transactions({"transactions": tx}, AS_OF))
    assert (found["returns"].kind, found["returns"].count) == (BUSINESS, 1)
    assert (found["zero_quantity"].kind, found["zero_quantity"].count) == (VIOLATION, 1)
    assert found["amount_mismatch"].count == 0  # sign convention: returns refund subtotal and tax


def test_relationship_counts_orphans_separately_from_null_references():
    data = {
        "customers": dataset("customers", [{"customer_id": "C1"}], key=("customer_id",)),
        "transactions": dataset("transactions", [
            {"transaction_id": "T1", "customer_id": "c1 "}, {"transaction_id": "T2", "customer_id": "C9"},
            {"transaction_id": "T3", "customer_id": None},
        ], key=("transaction_id",)),
    }
    summary, finding = check_relationship(Relation("transactions", "customer_id", "customers", "x.y"), data)
    assert (summary["orphans"], summary["null_references"], summary["samples"]) == (1, 1, ["C9"])
    assert finding.kind == VIOLATION and finding.pct == 33.33


def test_relationship_is_skipped_when_a_side_failed_to_load():
    data = {"customers": RawDataset("customers", "c.json", "json", ("customer_id",), error="boom"),
            "transactions": dataset("transactions", [{"transaction_id": "T1", "customer_id": "C1"}])}
    assert check_relationship(Relation("transactions", "customer_id", "customers", "x.y"), data) is None


@pytest.mark.integration
def test_profile_of_the_real_data(config, tmp_path):
    before = {p: p.read_bytes() for p in config.paths.raw_dir.iterdir() if p.is_file()}
    profile = build_profile(config)
    found = {(f["dataset"], f["check"]): f for f in profile["findings"]}

    assert found[("products", "invalid_price")]["count"] == 13
    assert found[("transactions", "zero_quantity")]["count"] == 42
    assert found[("transactions", "returns")]["kind"] == BUSINESS
    assert found[("customers", "registration_vs_metadata")]["count"] == 213
    assert found[("transactions", "currency_label_contradicted")]["kind"] == ANOMALY
    conv = profile["evidence"]["date_convention"]
    assert conv["nn/nn/yyyy"]["agree_if_day_first"] == conv["nn/nn/yyyy"]["values"]
    assert conv["nn-nn-yyyy"]["agree_if_month_first"] == conv["nn-nn-yyyy"]["values"]
    assert all(0 < f["count"] <= f["total"] for f in profile["findings"])
    assert {f["rule"] for f in profile["findings"] if f["rule"]} <= set(RULES)

    md, js = write_reports(profile, tmp_path)
    assert json.loads(js.read_text())["summary"] == profile["summary"]
    assert md.read_text() == render_markdown(build_profile(config))  # deterministic
    assert {p: p.read_bytes() for p in before} == before  # raw files untouched
