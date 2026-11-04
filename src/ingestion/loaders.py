"""Raw source readers and the Bronze layer.

Bronze keeps every source record exactly as delivered (no type coercion, CSV values stay
strings) wrapped in metadata columns (``_``-prefixed, next to ``record``):

- load: ``_batch_id`` (the pipeline run id) and ``_ingested_at`` say which load wrote the row,
  so an append-only Bronze can be filtered, replayed or rolled back per batch;
- file: ``_source_file``, ``_source_file_sha256`` and ``_source_file_modified_at`` identify the
  exact file version, so a re-delivered file can be detected and skipped;
- record: ``_source_row``, ``_record_key`` (raw business key, the merge key for upserts) and
  ``_record_hash`` (content hash: an unchanged hash for a known key means nothing to update).
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from src.utils.config import PipelineConfig, SourceConfig
from src.utils.io import dumps, to_jsonable, write_jsonl

INVOICE_LINES = "invoice_lines"
INVOICE_LINES_KEY = ("invoice_id", "line_number")
MANIFEST_FILE = "_manifest.json"

# Columns describing the load rather than the data; they change on every run.
BATCH_COLUMNS = ("_batch_id", "_ingested_at")
# Load and file columns, shared by every row from one file (and by rows derived from it).
FILE_COLUMNS = (*BATCH_COLUMNS, "_source_file", "_source_file_sha256", "_source_file_modified_at")


@dataclass
class SourceManifest:
    """What was read from one source; feeds the completeness section of the quality report."""

    source: str
    file: str
    batch_id: str
    status: str = "ok"
    rows: int = 0
    expected_rows: int | None = None
    file_sha256: str | None = None
    file_size_bytes: int | None = None
    file_modified_at: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_hash(record: dict[str, Any]) -> str:
    """Content hash of a raw record; identical records hash identically across runs."""
    return hashlib.sha256(dumps(record).encode()).hexdigest()[:16]


def record_key(record: dict[str, Any], key_fields: tuple[str, ...]) -> str | None:
    """Raw business key: trimmed key values joined with ``|``; None if any part is blank."""
    parts = ["" if record.get(name) is None else str(record[name]).strip() for name in key_fields]
    return "|".join(parts) if all(parts) else None


def file_metadata(path: Path, batch_id: str, ingested_at: str) -> dict[str, Any]:
    """The ``FILE_COLUMNS`` values for one source file."""
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return {
        "_batch_id": batch_id,
        "_ingested_at": ingested_at,
        "_source_file": path.name,
        "_source_file_sha256": file_sha256(path),
        "_source_file_modified_at": modified.isoformat(timespec="seconds"),
    }


def read_records(path: Path, source: SourceConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read one file into raw records plus any envelope metadata (JSON sources only)."""
    if source.format == "csv":
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh)), {}

    if source.format == "parquet":
        df = pd.read_parquet(path)
        return [to_jsonable(r) for r in df.astype(object).to_dict("records")], {}

    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    records = payload.get(source.record_path) if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError(f"Expected a list under '{source.record_path}' in {path.name}")
    envelope = {k: v for k, v in payload.items() if k != source.record_path}
    return records, envelope


def to_bronze_rows(
    source_name: str, records: list[dict[str, Any]], file_meta: dict[str, Any], key_fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    return [
        {
            **file_meta,
            "_source": source_name,
            "_source_row": row_number,
            "_record_key": record_key(record, key_fields),
            "_record_hash": record_hash(record),
            "record": record,
        }
        for row_number, record in enumerate(records, start=1)
    ]


def explode_invoice_lines(invoice_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive one Bronze row per invoice line from the nested ``line_items_json`` column.

    Unparseable JSON yields no lines here; the invoice header schema reports it as a rule
    failure, so every data-quality decision lives in the validation layer.
    """
    lines = []
    for parent in invoice_rows:
        invoice = parent["record"]
        try:
            items = json.loads(invoice.get("line_items_json") or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            record = {"invoice_id": invoice.get("invoice_id"), **item}
            lines.append(
                {
                    **{k: parent[k] for k in (*FILE_COLUMNS, "_source_row")},
                    "_source": INVOICE_LINES,
                    "_record_key": record_key(record, INVOICE_LINES_KEY),
                    "_parent_hash": parent["_record_hash"],
                    "_record_hash": record_hash(record),
                    "record": record,
                }
            )
    return lines


def ingest_source(
    name: str, config: PipelineConfig, batch_id: str, ingested_at: str
) -> tuple[list[dict], SourceManifest]:
    source = config.sources[name]
    path = config.source_path(name)
    meta = file_metadata(path, batch_id, ingested_at)
    records, envelope = read_records(path, source)
    manifest = SourceManifest(
        source=name,
        file=source.file,
        batch_id=batch_id,
        rows=len(records),
        file_sha256=meta["_source_file_sha256"],
        file_size_bytes=path.stat().st_size,
        file_modified_at=meta["_source_file_modified_at"],
    )
    if isinstance(envelope.get("total_count"), int):
        manifest.expected_rows = envelope["total_count"]
        if manifest.expected_rows != manifest.rows:
            manifest.notes.append(
                f"envelope total_count={manifest.expected_rows} but file holds {manifest.rows} records"
            )
    return to_bronze_rows(name, records, meta, source.primary_key), manifest


def run_ingestion(config: PipelineConfig, batch_id: str) -> list[SourceManifest]:
    """Load every configured source into Bronze JSON-lines files, tagged with ``batch_id``.

    A source that fails to load is recorded in the manifest and skipped; the others still
    load. The stage only fails if nothing could be ingested.
    """
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    bronze_dir = config.paths.bronze_dir
    manifests: list[SourceManifest] = []

    for name in config.sources:
        log = logger.bind(stage="ingest", source=name)
        try:
            rows, manifest = ingest_source(name, config, batch_id, ingested_at)
        except Exception as exc:  # one bad file must not stop the others
            log.error("Source failed to load: {}", exc)
            manifests.append(
                SourceManifest(source=name, file=config.sources[name].file, batch_id=batch_id, status="failed", error=str(exc))
            )
            continue

        write_jsonl(bronze_dir / f"{name}.jsonl", rows)
        manifests.append(manifest)
        log.info("Loaded source", rows=manifest.rows, expected_rows=manifest.expected_rows)
        for note in manifest.notes:
            log.warning("Completeness: {}", note)

        if name == "invoices":
            lines = explode_invoice_lines(rows)
            write_jsonl(bronze_dir / f"{INVOICE_LINES}.jsonl", lines)
            manifests.append(
                SourceManifest(
                    source=INVOICE_LINES,
                    file=manifest.file,
                    batch_id=batch_id,
                    rows=len(lines),
                    file_sha256=manifest.file_sha256,
                    file_size_bytes=manifest.file_size_bytes,
                    file_modified_at=manifest.file_modified_at,
                )
            )
            log.info("Exploded invoice lines", rows=len(lines))

    if not any(m.status == "ok" for m in manifests):
        raise RuntimeError("No source could be ingested")

    (bronze_dir / MANIFEST_FILE).write_text(json.dumps([asdict(m) for m in manifests], indent=2))
    return manifests
