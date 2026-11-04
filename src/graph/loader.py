"""Load the graph into SurrealDB: apply the schema, upsert in batches, prune stale records.

Idempotent: record ids are business keys and every write is an upsert, so a rerun on the
same data leaves the database unchanged. Records not written by the current batch (deleted
upstream) are removed once the whole load has succeeded, so a failed load never prunes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from surrealdb import AsyncSurreal
from surrealdb.cbor import CBORTag

from src.graph.model import EDGE_TABLES, NODE_TABLES, Graph, Record
from src.utils.config import SurrealConfig

SCHEMA_FILE = Path(__file__).with_name("schema.surql")
REMOTE_SCHEMES = ("ws", "wss", "http", "https")
DATETIME_TAG = 12  # SurrealDB's CBOR datetime: [seconds, nanoseconds] since the Unix epoch
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def wire(obj: Any) -> Any:
    """Encode datetimes the way the SurrealDB server accepts them.

    The Python client sends ``datetime`` as CBOR tag 0 (ISO text), which the embedded engine
    accepts but a SurrealDB 4 server rejects. Tag 12 works for both.
    """
    if isinstance(obj, datetime):
        delta = obj - EPOCH  # exact integer arithmetic; obj is timezone-aware (see model.value)
        return CBORTag(DATETIME_TAG, [delta.days * 86_400 + delta.seconds, delta.microseconds * 1_000])
    if isinstance(obj, dict):
        return {k: wire(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [wire(v) for v in obj]
    return obj


def _chunks(items: list[Record], size: int) -> Iterator[list[Record]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def upsert_statement(table: str, records: list[Record], relation: bool) -> str:
    """``INSERT [RELATION] … ON DUPLICATE KEY UPDATE`` over every field the batch carries."""
    fields = sorted({k for r in records for k in r} - {"id", "in", "out"})
    updates = ", ".join(f"{f} = $input.{f}" for f in fields)
    return f"INSERT {'RELATION ' if relation else ''}INTO {table} $rows ON DUPLICATE KEY UPDATE {updates}"


async def connect(config: SurrealConfig, url: str) -> AsyncSurreal:
    db = AsyncSurreal(url)
    if url.split("://", 1)[0] in REMOTE_SCHEMES:
        username, password = config.credentials()
        try:
            await db.signin({"username": username, "password": password})
        except OSError as exc:
            raise ConnectionError(
                f"SurrealDB is not reachable at {url}. Start it with `docker compose up -d surrealdb`, "
                f"or set {config.url_env}=surrealkv://outputs/graph/ecommerce.db to use the embedded engine."
            ) from exc
    await db.use(config.namespace, config.database)
    return db


async def count(db: AsyncSurreal, table: str) -> int:
    result = await db.query(f"SELECT count() AS n FROM {table} GROUP ALL")
    return int(result[0]["n"]) if result else 0


async def load_graph(db: AsyncSurreal, graph: Graph, batch_id: str, batch_size: int) -> dict[str, int]:
    """Write ``graph`` and return the record count per table after pruning."""
    await db.query(SCHEMA_FILE.read_text())
    # Nodes first: an edge can only point at an existing record.
    for tables, source, relation in ((NODE_TABLES, graph.nodes, False), (EDGE_TABLES, graph.edges, True)):
        for table in tables:
            for chunk in _chunks(source[table], batch_size):
                await db.query(upsert_statement(table, chunk, relation), {"rows": wire(chunk)})
    # Edges before nodes, so no edge is left pointing at a deleted node.
    for table in (*EDGE_TABLES, *NODE_TABLES):
        await db.query(f"DELETE {table} WHERE batch_id != $batch", {"batch": batch_id})
    counts = {table: await count(db, table) for table in (*NODE_TABLES, *EDGE_TABLES)}
    expected = {t: len(r) for t, r in (*graph.nodes.items(), *graph.edges.items())}
    if counts != expected:
        raise RuntimeError(f"Graph load mismatch: expected {expected}, found {counts}")
    return counts


def log_load(counts: dict[str, int], graph: Graph) -> None:
    log = logger.bind(stage="graph")
    log.info("Graph loaded", nodes=sum(counts[t] for t in NODE_TABLES), edges=sum(counts[t] for t in EDGE_TABLES))
    for reason, n in sorted(graph.skipped.items()):
        log.warning("Edges skipped: end not in graph", reason=reason, count=n)
