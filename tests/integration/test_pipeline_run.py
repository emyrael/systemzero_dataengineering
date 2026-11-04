"""End-to-end run of the implemented stages on the real assessment data."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src.cleaning.clean import CLEAN_SUBDIR, DUPLICATES_FILE
from src.cleaning.clean import METRICS_FILE as CLEANING_METRICS_FILE
from src.ingestion.loaders import BATCH_COLUMNS
from src.pipeline import PipelineContext, run_pipeline
from src.validation.validator import METRICS_FILE, VALIDATED_SUBDIR

pytestmark = pytest.mark.integration
STAGES = ["ingest", "validate", "clean", "report", "features"]


@pytest.fixture
def ran(config):
    summary = run_pipeline(PipelineContext(config=config, run_id="it"), STAGES)
    assert summary.ok, summary
    metrics = json.loads((config.paths.silver_dir / VALIDATED_SUBDIR / METRICS_FILE).read_text())
    return config, metrics["entities"]


def test_every_record_is_accounted_for(ran):
    config, entities = ran
    for name, m in entities.items():
        valid = pd.read_parquet(config.paths.silver_dir / VALIDATED_SUBDIR / f"{name}.parquet")
        quarantine = pd.read_csv(config.paths.quarantine_dir / f"{name}.csv")
        assert len(valid) + len(quarantine) == m["total"], name


def test_cleaning_removes_only_duplicates(ran):
    config, validated = ran
    cleaning = json.loads((config.paths.silver_dir / CLEAN_SUBDIR / CLEANING_METRICS_FILE).read_text())
    duplicates = pd.read_csv(config.paths.quarantine_dir / DUPLICATES_FILE)
    for name, m in cleaning["entities"].items():
        clean = pd.read_parquet(config.paths.silver_dir / CLEAN_SUBDIR / f"{name}.parquet")
        assert m["rows_in"] == validated[name]["valid"]
        assert len(clean) == m["rows_out"] == m["rows_in"] - (duplicates["entity"] == name).sum()
        assert clean["_record_key"].is_unique, name
    assert duplicates["identical_to_kept"].all()  # every duplicate in this extract is an exact copy


def test_cross_table_issues_are_caught(ran):
    config, _ = ran
    m = json.loads((config.paths.silver_dir / CLEAN_SUBDIR / CLEANING_METRICS_FILE).read_text())["entities"]
    assert m["transactions"]["flags"]["transactions.unknown_customer"] == 88
    assert m["transactions"]["flags"]["transactions.unknown_product"] == 108
    assert m["invoices"]["flags"]["invoices.unknown_vendor"] == 16
    assert m["products"]["fixes"]["products.category_from_name"] == 14
    tx = pd.read_parquet(config.paths.silver_dir / CLEAN_SUBDIR / "transactions.parquet")
    assert set(tx["currency"]) == {"USD"} and set(tx["src_currency"]) == {"USD", "EUR", "GBP", "AUD"}


def test_feature_totals_reconcile_with_recognised_revenue(ran):
    config, _ = ran
    from src.features.build import recognised_sales

    gold = {n: pd.read_parquet(config.paths.gold_dir / f"{n}_features.parquet")
            for n in ("customer", "product", "vendor", "invoice")}
    sales = recognised_sales(pd.read_parquet(config.paths.silver_dir / CLEAN_SUBDIR / "transactions.parquet"))
    on_unknown_product = sales.loc[sales["product_id"].str.startswith("PRD-DELETED"), "net_revenue"].sum()
    assert gold["product"]["revenue_contribution"].sum() == pytest.approx(sales["net_revenue"].sum() - on_unknown_product, abs=1)
    assert gold["vendor"]["revenue_generated"].sum() == pytest.approx(gold["product"]["revenue_contribution"].sum(), abs=1)
    assert (len(gold["customer"]), len(gold["product"]), len(gold["invoice"])) == (1000, 500, 800)
    assert (gold["customer"]["customer_lifetime_value"] >= 0).all()
    assert set(gold["invoice"]["discount_rate_achieved"].dropna()) == {0.0}  # vendors bill at catalog cost


def test_known_issues_are_caught(ran):
    _, entities = ran
    assert entities["transactions"]["rejections"] == {"transactions.zero_quantity": 42}
    assert entities["customers"]["fixes"]["customers.registration_from_metadata"] == 213
    assert entities["invoices"]["fixes"]["invoices.due_date_from_terms"] == 805
    assert entities["products"]["flags"]["products.invalid_price"] == 13
    assert entities["support_tickets"]["rejections"] == {"support_tickets.invalid_id": 1}


def test_validated_data_is_typed(ran):
    config, _ = ran
    products = pd.read_parquet(config.paths.silver_dir / VALIDATED_SUBDIR / "products.parquet")
    assert products["is_active"].dropna().map(type).eq(bool).all()
    assert set(products["currency"].dropna()) == {"USD"}
    assert (products["price"].dropna() > 0).all()


def _snapshot(config):
    """Report bytes plus every Silver and quarantine table, minus the per-run batch columns."""
    silver = sorted(config.paths.silver_dir.glob("*/*.parquet"))
    quarantine = sorted(config.paths.quarantine_dir.glob("*.csv"))
    tables = [pd.read_parquet(p) for p in silver] + [pd.read_csv(p) for p in quarantine]
    batch_ids = {b for t in tables for b in t["_batch_id"].dropna()}
    return config.paths.quality_report.read_bytes(), [t.drop(columns=list(BATCH_COLUMNS)) for t in tables], batch_ids


def test_rerun_is_idempotent_apart_from_batch_metadata(ran):
    config, _ = ran
    report, tables, batch_ids = _snapshot(config)

    run_pipeline(PipelineContext(config=config, run_id="it-2"), STAGES)

    report_2, tables_2, batch_ids_2 = _snapshot(config)
    assert report_2 == report
    for before, after in zip(tables, tables_2, strict=True):
        pd.testing.assert_frame_equal(before, after)
    assert (batch_ids, batch_ids_2) == ({"it"}, {"it-2"})


def test_silver_rows_trace_back_to_bronze(ran):
    config, _ = ran
    customers = pd.read_parquet(config.paths.silver_dir / VALIDATED_SUBDIR / "customers.parquet")
    assert customers["_record_key"].notna().all()
    assert (customers["_record_key"].str.upper().str.strip() == customers["customer_id"]).mean() > 0.99
