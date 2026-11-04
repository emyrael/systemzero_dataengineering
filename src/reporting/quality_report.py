"""Render the data-quality report (Markdown) from stage metrics.

The report is generated purely from metrics files, contains no wall-clock timestamp,
and is therefore identical across reruns on the same input.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from loguru import logger

from src.cleaning.clean import CLEAN_SUBDIR, DUPLICATES_FILE
from src.cleaning.clean import METRICS_FILE as CLEANING_METRICS_FILE
from src.ingestion.loaders import MANIFEST_FILE
from src.utils.config import PipelineConfig
from src.validation.rules import RULES, Action
from src.validation.validator import METRICS_FILE, VALIDATED_SUBDIR

_ACTION_LABEL = {Action.QUARANTINE: "Rejected", Action.DROP: "Removed", Action.FLAG: "Flagged", Action.FIX: "Fixed"}
_COUNTERS = (("rejections", Action.QUARANTINE), ("removals", Action.DROP), ("flags", Action.FLAG), ("fixes", Action.FIX))


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "–"


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def _describe(rule_id: str) -> str:
    rule = RULES.get(rule_id)
    if rule:
        return rule.description
    # Schema-level errors not covered by a named rule: "<entity>.schema:<field>:<type>"
    return "schema violation: " + rule_id.split(".schema:", 1)[-1].replace(":", " → ")


def merge_metrics(validation: dict[str, dict], cleaning: dict[str, dict]) -> dict[str, dict]:
    """Per-entity view over both stages: rule counts summed, record counts after cleaning."""
    merged = {}
    for name, v in validation.items():
        c = cleaning[name]
        merged[name] = {
            "total": v["total"],
            "quarantined": v["quarantined"],
            "removed": sum(c["removals"].values()),
            "rows_out": c["rows_out"],
            "records_fixed": c["records_fixed"],
            "records_flagged": c["records_flagged"],
            "rejections": v["rejections"],
            "removals": c["removals"],
            "flags": dict((Counter(v["flags"]) + Counter(c["flags"])).most_common()),
            "fixes": dict((Counter(v["fixes"]) + Counter(c["fixes"])).most_common()),
        }
    return merged


def _summary(manifest: list[dict], entities: dict[str, dict]) -> list[str]:
    rows = []
    for name, m in entities.items():
        unflagged = m["rows_out"] - m["records_flagged"]
        rows.append([
            f"`{name}`", m["total"], m["quarantined"], m["removed"], m["rows_out"], m["records_fixed"],
            m["records_flagged"], _pct(unflagged, m["rows_out"]),
        ])
    failed = [m for m in manifest if m["status"] != "ok"]
    out = ["## 1. Summary", ""]
    out += _table(
        ["Entity", "Records in", "Quarantined", "Duplicates removed", "Rows out", "With fixes", "Flagged",
         "No flags"],
        rows,
    )
    if failed:
        out += ["", "**Sources that failed to load:**", ""]
        out += [f"- `{m['source']}` ({m['file']}): {m['error']}" for m in failed]
    out += [
        "",
        "*Fixed* = a value was standardised or derived from evidence (`dq_fixes`). *Flagged* = "
        "kept, with the issue recorded in the record's `dq_flags`. *Quarantined* = rejected; see "
        "`outputs/quarantine/<entity>.csv` for the raw record and reasons. *Duplicates removed* = "
        f"repeated primary keys, logged in `outputs/quarantine/{DUPLICATES_FILE}`. *Rows out* = rows in "
        "`outputs/processed/silver/clean/`.",
    ]
    return out


def _completeness(manifest: list[dict]) -> list[str]:
    rows = [
        [f"`{m['source']}`", m["file"], m["rows"], m["expected_rows"] if m["expected_rows"] is not None else "–",
         "; ".join(m["notes"]) or "ok"]
        for m in manifest
    ]
    return ["## 2. Source completeness", "", *_table(["Source", "File", "Rows read", "Expected", "Note"], rows)]


def _entity_section(name: str, m: dict) -> list[str]:
    rows = []
    for key, action in _COUNTERS:
        for rule_id, count in m[key].items():
            rows.append([_ACTION_LABEL[action], f"`{rule_id}`", count, _pct(count, m["total"]), _describe(rule_id)])
    if not rows:
        return [f"### `{name}`", "", "No issues found.", ""]
    return [f"### `{name}`", "", *_table(["Action", "Rule", "Records", "Share", "Description"], rows), ""]


def _not_observed(entities: dict[str, dict]) -> list[str]:
    fired = {rule for m in entities.values() for key, _ in _COUNTERS for rule in m[key]}
    silent = [r for r in RULES.values() if r.id not in fired and r.entity in entities]
    rows = [[f"`{r.id}`", _ACTION_LABEL[r.action], r.description] for r in silent]
    return [
        "## 4. Checks that found nothing",
        "",
        "These rules ran on every record and matched none. Several correspond to issues listed in "
        "the assessment brief that are not present in this extract; the checks stay in place for "
        "future loads.",
        "",
        *_table(["Rule", "Action if hit", "Description"], rows),
    ]


def render_quality_report(manifest: list[dict], validation: dict[str, Any], cleaning: dict[str, Any]) -> str:
    entities = merge_metrics(validation["entities"], cleaning["entities"])
    lines = [
        "# Data Quality Report",
        "",
        f"Reference date (`as_of_date`): **{validation['as_of_date']}**. "
        "Generated by `python run_pipeline.py`; do not edit by hand.",
        "",
        *_summary(manifest, entities),
        "",
        *_completeness(manifest),
        "",
        "## 3. Issues by entity",
        "",
    ]
    for name, m in entities.items():
        lines += _entity_section(name, m)
    lines += _not_observed(entities)
    return "\n".join(lines) + "\n"


def write_quality_report(config: PipelineConfig) -> Path:
    manifest = json.loads((config.paths.bronze_dir / MANIFEST_FILE).read_text())
    validation = json.loads((config.paths.silver_dir / VALIDATED_SUBDIR / METRICS_FILE).read_text())
    cleaning = json.loads((config.paths.silver_dir / CLEAN_SUBDIR / CLEANING_METRICS_FILE).read_text())
    path = config.paths.quality_report
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_quality_report(manifest, validation, cleaning))
    logger.bind(stage="report").info("Quality report written", path=str(path))
    return path
