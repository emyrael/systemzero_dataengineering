"""Pipeline orchestration: runs stages in order and isolates failures."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.utils.config import PipelineConfig


@dataclass
class PipelineContext:
    """State shared across stages for a single run."""

    config: PipelineConfig
    run_id: str
    artifacts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[PipelineContext], None]
    depends_on: tuple[str, ...] = ()


def _ingest(ctx: PipelineContext) -> None:
    from src.ingestion.loaders import run_ingestion

    ctx.artifacts["manifest"] = run_ingestion(ctx.config, batch_id=ctx.run_id)


def _validate(ctx: PipelineContext) -> None:
    from src.validation.validator import run_validation

    ctx.artifacts["validation"] = run_validation(ctx.config)


def _clean(ctx: PipelineContext) -> None:
    from src.cleaning.clean import run_cleaning

    ctx.artifacts["cleaning"] = run_cleaning(ctx.config)


def _report(ctx: PipelineContext) -> None:
    from src.reporting.quality_report import write_quality_report

    write_quality_report(ctx.config)


# Canonical stage order. Each phase registers its stage in STAGES as it is implemented.
# Stages exchange data through files (Bronze/Silver/Gold), so any stage can be re-run alone.
STAGE_ORDER: tuple[str, ...] = ("ingest", "validate", "clean", "report", "features", "graph", "dashboard")
STAGES: dict[str, Stage] = {
    "ingest": Stage("ingest", _ingest),
    "validate": Stage("validate", _validate, depends_on=("ingest",)),
    "clean": Stage("clean", _clean, depends_on=("validate",)),
    "report": Stage("report", _report, depends_on=("clean",)),
}


@dataclass
class RunSummary:
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def run_pipeline(ctx: PipelineContext, stage_names: Iterable[str] | None = None) -> RunSummary:
    """Run the requested stages (default: all) in canonical order.

    A failing stage does not abort the run: stages that depend on it are skipped,
    independent stages still run, and the summary reports what happened.
    """
    requested = set(stage_names or STAGE_ORDER)
    unknown = requested - set(STAGE_ORDER)
    if unknown:
        raise ValueError(f"Unknown stage(s): {sorted(unknown)}; expected one of {STAGE_ORDER}")

    summary = RunSummary()
    for name in (s for s in STAGE_ORDER if s in requested):
        log = logger.bind(stage=name)
        stage = STAGES.get(name)
        if stage is None:
            log.warning("Stage not implemented yet; skipping")
            summary.skipped.append(name)
            continue

        blocked = [dep for dep in stage.depends_on if dep in summary.failed or dep in summary.skipped]
        if blocked:
            log.warning("Skipping: upstream stage(s) did not succeed", blocked_by=blocked)
            summary.skipped.append(name)
            continue

        started = time.perf_counter()
        try:
            stage.run(ctx)
        except Exception:
            log.exception("Stage failed")
            summary.failed.append(name)
        else:
            log.info("Stage finished", duration_s=round(time.perf_counter() - started, 2))
            summary.succeeded.append(name)

    return summary
