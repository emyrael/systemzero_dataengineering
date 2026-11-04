from __future__ import annotations

import json

import pytest

from src.ingestion.loaders import (
    MANIFEST_FILE,
    explode_invoice_lines,
    record_hash,
    record_key,
    run_ingestion,
    to_bronze_rows,
)
from src.utils.io import read_jsonl


FILE_META = {"_batch_id": "b1", "_ingested_at": "t", "_source_file": "f.csv",
             "_source_file_sha256": "abc", "_source_file_modified_at": "m"}


def test_bronze_rows_carry_metadata_and_stable_hash():
    rows = to_bronze_rows("products", [{"id": "P1", "a": "1"}, {"id": "P2", "a": "2"}], FILE_META, ("id",))
    assert [r["_source_row"] for r in rows] == [1, 2]
    assert rows[0]["record"] == {"id": "P1", "a": "1"}
    assert {k: rows[0][k] for k in FILE_META} == FILE_META
    assert [r["_record_key"] for r in rows] == ["P1", "P2"]
    assert rows[0]["_record_hash"] == record_hash({"id": "P1", "a": "1"}) != rows[1]["_record_hash"]


@pytest.mark.parametrize(
    ("record", "expected"),
    [({"a": " X ", "b": 2}, "X|2"), ({"a": "X", "b": None}, None), ({"a": "  ", "b": 1}, None), ({}, None)],
)
def test_record_key(record, expected):
    assert record_key(record, ("a", "b")) == expected


def test_invoice_lines_are_exploded_with_parent_metadata():
    parent = to_bronze_rows("invoices", [
        {"invoice_id": "INV-1", "line_items_json": json.dumps([{"line_number": 1}, {"line_number": 2}])},
        {"invoice_id": "INV-2", "line_items_json": "{not json"},
    ], FILE_META, ("invoice_id",))
    lines = explode_invoice_lines(parent)
    assert [l["_record_key"] for l in lines] == ["INV-1|1", "INV-1|2"]
    assert all(l["_parent_hash"] == parent[0]["_record_hash"] and l["_source_row"] == 1 for l in lines)
    assert all(l["_batch_id"] == "b1" and l["_source_file_sha256"] == "abc" for l in lines)


def test_run_ingestion_writes_bronze_and_manifest(config):
    manifests = {m.source: m for m in run_ingestion(config, batch_id="b1")}

    assert manifests["products"].rows == 505
    assert manifests["invoice_lines"].rows == 3618
    assert manifests["support_tickets"].expected_rows == 250 and manifests["support_tickets"].notes
    assert manifests["products"].batch_id == "b1" and manifests["products"].file_size_bytes > 0
    first = next(read_jsonl(config.paths.bronze_dir / "products.jsonl"))
    assert first["record"]["price"] == "148.13"  # CSV values stay raw strings in Bronze
    assert first["_batch_id"] == "b1" and first["_record_key"] == first["record"]["product_id"]
    assert first["_source_file_sha256"] == manifests["products"].file_sha256
    assert (config.paths.bronze_dir / MANIFEST_FILE).is_file()


def test_one_broken_source_does_not_stop_the_others(config, tmp_path):
    broken = tmp_path / "raw"
    broken.mkdir()
    for name, source in config.sources.items():
        (broken / source.file).write_bytes(config.source_path(name).read_bytes())
    (broken / "vendors.json").write_text("{ not json")
    cfg = config.model_copy(update={"paths": config.paths.model_copy(update={"raw_dir": broken})})

    manifests = {m.source: m for m in run_ingestion(cfg, batch_id="b1")}

    assert manifests["vendors"].status == "failed" and manifests["vendors"].error
    assert manifests["products"].status == "ok"


def test_ingestion_fails_only_when_nothing_loads(config, tmp_path):
    cfg = config.model_copy(update={"paths": config.paths.model_copy(update={"raw_dir": tmp_path})})
    with pytest.raises(RuntimeError, match="No source"):
        run_ingestion(cfg, batch_id="b1")
