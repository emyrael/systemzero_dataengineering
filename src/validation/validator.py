"""Validate Bronze records against their schemas; split into valid rows and quarantine."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger
from pydantic import ValidationError

from src.ingestion.loaders import MANIFEST_FILE
from src.utils.config import PipelineConfig
from src.utils.io import dumps, read_jsonl, write_csv, write_parquet
from src.validation.base import RecordModel
from src.validation.rules import RULES, Action
from src.validation.schemas import SCHEMAS

# Bronze metadata carried onto Silver and quarantine rows (see src/ingestion/loaders.py).
LINEAGE_COLUMNS = ("_batch_id", "_ingested_at", "_source_file", "_source_row", "_record_key", "_record_hash")
VALIDATED_SUBDIR = "validated"
METRICS_FILE = "_validation_metrics.json"


@dataclass
class EntityMetrics:
    entity: str
    total: int = 0
    valid: int = 0
    quarantined: int = 0
    records_fixed: int = 0
    records_flagged: int = 0
    fixes: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)
    rejections: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("fixes", "flags", "rejections"):
            data[key] = dict(getattr(self, key).most_common())
        return data


@dataclass
class RecordOutcome:
    row: dict[str, Any] | None
    rejections: list[dict[str, Any]]
    fixes: list[str]
    flags: list[str]


def _rejection_rule(entity: str, error: dict[str, Any]) -> str:
    """Map a Pydantic error to a rule id; uncovered errors fail closed under a schema rule."""
    if error["type"] in RULES:
        return error["type"]
    location = ".".join(str(part) for part in error["loc"]) or "record"
    return f"{entity}.schema:{location}:{error['type']}"


def _split_events(events: list[str]) -> tuple[list[str], list[str]]:
    fixes, flags = [], []
    for rule_id in dict.fromkeys(events):  # de-duplicate, keep first-seen order
        rule = RULES.get(rule_id)
        if rule is None:
            raise KeyError(f"Schema raised unregistered rule '{rule_id}'")
        (fixes if rule.action is Action.FIX else flags).append(rule_id)
    return fixes, flags


def validate_record(
    entity: str, model: type[RecordModel], record: dict[str, Any], context: dict[str, Any]
) -> RecordOutcome:
    ctx = {**context, "events": []}
    try:
        parsed = model.model_validate(record, context=ctx)
    except ValidationError as exc:
        rejections = [
            {
                "rule": _rejection_rule(entity, err),
                "field": ".".join(str(p) for p in err["loc"]),
                "message": err["msg"],
            }
            for err in exc.errors(include_url=False, include_input=False)
        ]
        return RecordOutcome(row=None, rejections=rejections, fixes=[], flags=[])

    fixes, flags = _split_events(ctx["events"])
    return RecordOutcome(row=parsed.model_dump(), rejections=[], fixes=fixes, flags=flags)


def validate_entity(
    entity: str, bronze_rows: list[dict[str, Any]], context: dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame, EntityMetrics]:
    model = SCHEMAS[entity]
    metrics = EntityMetrics(entity=entity)
    valid_rows, quarantine_rows = [], []

    for bronze in bronze_rows:
        metrics.total += 1
        lineage = {col: bronze.get(col) for col in LINEAGE_COLUMNS}
        outcome = validate_record(entity, model, bronze["record"], context)

        if outcome.row is None:
            metrics.quarantined += 1
            rules = list(dict.fromkeys(r["rule"] for r in outcome.rejections))
            metrics.rejections.update(rules)
            quarantine_rows.append({
                **lineage,
                "rejection_rules": ";".join(rules),
                "rejection_details": dumps(outcome.rejections),
                "raw_record": dumps(bronze["record"]),
            })
            continue

        metrics.valid += 1
        metrics.fixes.update(outcome.fixes)
        metrics.flags.update(outcome.flags)
        metrics.records_fixed += bool(outcome.fixes)
        metrics.records_flagged += bool(outcome.flags)
        valid_rows.append({**outcome.row, **lineage, "dq_fixes": outcome.fixes, "dq_flags": outcome.flags})

    quarantine_columns = [*LINEAGE_COLUMNS, "rejection_rules", "rejection_details", "raw_record"]
    return pd.DataFrame(valid_rows), pd.DataFrame(quarantine_rows, columns=quarantine_columns), metrics


def load_manifest(bronze_dir: Path) -> list[dict[str, Any]]:
    path = bronze_dir / MANIFEST_FILE
    if not path.is_file():
        raise FileNotFoundError(f"No Bronze manifest at {path}; run the ingest stage first")
    return json.loads(path.read_text())


def validation_context(config: PipelineConfig) -> dict[str, Any]:
    return {
        "as_of": config.run.as_of_date,
        "payment_terms_days": config.reference.payment_terms_days,
    }


def run_validation(config: PipelineConfig) -> dict[str, EntityMetrics]:
    """Validate every successfully ingested Bronze source and persist the results."""
    bronze_dir = config.paths.bronze_dir
    out_dir = config.paths.silver_dir / VALIDATED_SUBDIR
    context = validation_context(config)
    results: dict[str, EntityMetrics] = {}

    for source in load_manifest(bronze_dir):
        entity = source["source"]
        log = logger.bind(stage="validate", source=entity)
        if source["status"] != "ok":
            log.warning("Skipping: source failed ingestion")
            continue

        rows = list(read_jsonl(bronze_dir / f"{entity}.jsonl"))
        valid, quarantine, metrics = validate_entity(entity, rows, context)
        write_parquet(out_dir / f"{entity}.parquet", valid)
        write_csv(config.paths.quarantine_dir / f"{entity}.csv", quarantine)
        results[entity] = metrics
        log.info(
            "Validated",
            total=metrics.total,
            valid=metrics.valid,
            quarantined=metrics.quarantined,
            flagged=metrics.records_flagged,
        )

    payload = {
        "as_of_date": date.isoformat(config.run.as_of_date),
        "entities": {name: m.as_dict() for name, m in results.items()},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / METRICS_FILE).write_text(json.dumps(payload, indent=2))
    return results
