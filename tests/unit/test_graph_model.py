from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest
from surrealdb import RecordID
from surrealdb.cbor import CBORTag

from src.graph.loader import upsert_statement, wire
from src.graph.model import _Builder, slug, value
from src.graph.queries import QUERIES, pearson_r


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (float("nan"), None), (pd.NA, None), (pd.NaT, None), (np.int64(3), 3), (np.float64(1.5), 1.5),
        (date(2024, 5, 1), datetime(2024, 5, 1, tzinfo=timezone.utc)),
        (pd.Timestamp("2024-05-01 10:00"), datetime(2024, 5, 1, 10, tzinfo=timezone.utc)),
        (np.array(["a", "b"], dtype=object), ["a", "b"]),
        (np.array([{"x": np.int64(1)}], dtype=object), [{"x": 1}]),
    ],
)
def test_value_converts_to_plain_python(raw, expected):
    assert value(raw) == expected


def test_slug():
    assert slug("United Kingdom") == "united_kingdom"
    assert slug(" Sports & Outdoors ") == "sports_outdoors"


def test_edges_need_both_ends_and_skips_are_counted():
    b = _Builder("b1")
    b.node("customer", "C1", {"name": "a"})
    b.node("customer", "C1", {"name": "duplicate is ignored"})
    b.node("product", "P1", {"name": "p"})
    b.edge("purchased", "T1", ("customer", "C1"), ("product", "P1"), {"quantity": 1})
    b.edge("purchased", "T2", ("customer", "C9"), ("product", "P1"))
    b.edge("purchased", "T3", ("customer", "C1"), ("product", None))

    assert len(b.graph.nodes["customer"]) == 1 and b.graph.nodes["customer"][0]["name"] == "a"
    [edge] = b.graph.edges["purchased"]
    assert (edge["id"], edge["in"], edge["out"]) == (RecordID("purchased", "T1"), RecordID("customer", "C1"),
                                                     RecordID("product", "P1"))
    assert edge["batch_id"] == "b1" and edge["quantity"] == 1
    assert dict(b.graph.skipped) == {"purchased:in=customer": 1, "purchased:out=product": 1}


def test_upsert_statement_updates_every_field_but_identity():
    sql = upsert_statement("purchased", [{"id": 1, "in": 2, "out": 3, "b": 1}, {"a": 2}], relation=True)
    assert sql == ("INSERT RELATION INTO purchased $rows ON DUPLICATE KEY UPDATE "
                   "a = $input.a, b = $input.b")


def test_wire_sends_datetimes_as_tag_12():
    # SurrealDB 4 rejects the client's default (tag 0, ISO text); tag 12 is [seconds, nanoseconds].
    dt = datetime(2024, 5, 1, 10, 30, 15, 123456, tzinfo=timezone.utc)
    assert wire({"rows": [{"at": dt, "n": 1}]}) == {"rows": [{"at": CBORTag(12, [1714559415, 123456000]), "n": 1}]}
    assert wire(datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=timezone.utc)) == CBORTag(12, [-1, 500000000])


def test_pearson_r():
    assert pearson_r([1, 2, 3, 4], [2, 4, 6, 8]) == 1.0
    assert pearson_r([1, 2, 3, None], [3, 2, 1, 5]) == -1.0
    assert pearson_r([1, 2], [1, 2]) is None  # too few pairs
    assert pearson_r([1, 1, 1], [1, 2, 3]) is None  # no variance


def test_there_are_eight_uniquely_named_queries():
    assert len({q.id for q in QUERIES}) == len(QUERIES) == 8
