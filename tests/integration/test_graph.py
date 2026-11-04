"""Build the graph from the real data and load it into an in-memory SurrealDB."""

from __future__ import annotations

import pandas as pd
import pytest
from surrealdb import AsyncSurreal

from src.features.build import recognised_sales
from src.graph.loader import count, load_graph
from src.graph.model import build_graph
from src.graph.queries import QUERIES
from src.graph.stage import query_params, read_tables, run_queries
from src.pipeline import PipelineContext, run_pipeline
from src.utils.config import DEFAULT_CONFIG_PATH, load_config

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def tables(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("graph")
    cfg = load_config(DEFAULT_CONFIG_PATH)
    paths = cfg.paths.model_copy(update={
        name: tmp / value.relative_to(cfg.paths.output_dir)
        for name, value in cfg.paths if value.is_relative_to(cfg.paths.output_dir)
    })
    cfg = cfg.model_copy(update={"paths": paths})
    assert run_pipeline(PipelineContext(config=cfg, run_id="it"), ["ingest", "validate", "clean", "features"]).ok
    silver, gold = read_tables(cfg)
    return cfg, silver, gold


DOCKER_URL = "ws://localhost:8000/rpc"


@pytest.fixture(params=["mem://", DOCKER_URL], ids=["embedded", "docker"])
async def db(request):
    """The embedded engine (SurrealDB 2) always; the Docker server (SurrealDB 4) when it is running."""
    connection = AsyncSurreal(request.param)
    if request.param == DOCKER_URL:
        try:
            await connection.signin({"username": "root", "password": "root"})
        except OSError:
            pytest.skip("SurrealDB container not running (docker compose up -d surrealdb)")
    await connection.use("test", "pytest_graph")  # never the pipeline's database
    yield connection
    await connection.close()


async def test_load_is_idempotent_and_prunes_stale_records(tables, db):
    cfg, silver, gold = tables
    graph = build_graph(silver, gold, cfg.reference.country_regions, "run-1")
    first = await load_graph(db, graph, "run-1", 500)
    assert first["purchased"] == 4764 and first["customer"] == 1000

    again = await load_graph(db, build_graph(silver, gold, cfg.reference.country_regions, "run-2"), "run-2", 500)
    assert again == first  # same data, new batch: upserts only, nothing duplicated

    fewer = {**silver, "products": silver["products"].iloc[1:]}  # a product deleted upstream
    third = await load_graph(db, build_graph(fewer, gold, cfg.reference.country_regions, "run-3"), "run-3", 500)
    assert third["product"] == first["product"] - 1
    assert await count(db, "supplies") == third["supplies"] == first["supplies"] - 1


async def test_queries_agree_with_pandas(tables, db):
    cfg, silver, gold = tables
    await load_graph(db, build_graph(silver, gold, cfg.reference.country_regions, "q"), "q", 500)
    params = query_params(gold, cfg.run.as_of_date)
    results = await run_queries(db, params)
    assert all(results[q.id] for q in QUERIES), "every query returns rows"

    vendors, products = silver["vendors"], silver["products"]
    reliable = vendors.loc[vendors["reliability_score"] > 90, "vendor_id"]
    assert sum(r["products"] for r in results["q1_reliable_vendor_products"]) == products["vendor_id"].isin(reliable).sum()

    known = silver["customers"]["customer_id"]
    sales = recognised_sales(silver["transactions"])
    sales = sales[sales["customer_id"].isin(known)].merge(products[["product_id", "vendor_id"]], on="product_id")
    spend = sales.groupby(["vendor_id", "customer_id"])["net_revenue"].sum()
    best = spend.groupby("vendor_id").idxmax().map(lambda pair: pair[1])
    names = dict(zip(vendors["vendor_name"], vendors["vendor_id"]))
    for row in results["q4_top_customers_by_vendor"]:
        assert row["top_customers"][0]["customer_id"].id == best[names[row["vendor"]]], row["vendor"]

    reach = sales.groupby("vendor_id")["customer_id"].nunique()
    for row in results["q5_vendor_influence"]:
        assert row["customers_reached"] == reach.get(names[row["vendor"]], 0)

    lines = results["q6_invoice_reconciliation"]
    assert all(r["cost_matches"] == r["lines_checked"] for r in lines)  # every catalog line at catalog cost
