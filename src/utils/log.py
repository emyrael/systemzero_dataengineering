"""Structured logging: readable console output plus a JSON-lines file per run."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
    "<cyan>{extra[stage]: <10}</cyan> | {message}"
)
_CONSOLE_HIDDEN_EXTRA = {"run_id", "stage"}


def _console_format(record: dict) -> str:
    """Console line with any extra context rendered as ``key=value`` pairs."""
    context = " ".join(
        f"{key}={value}" for key, value in record["extra"].items() if key not in _CONSOLE_HIDDEN_EXTRA
    )
    # The returned string is itself a loguru template: escape braces and colour-markup tags.
    context = context.replace("{", "{{").replace("}", "}}").replace("<", r"\<")
    suffix = f" <dim>{context}</dim>" if context else ""
    return _CONSOLE_FORMAT + suffix + "\n{exception}"


def new_run_id() -> str:
    """Sortable, unique run identifier, e.g. ``20250101T120000Z-1a2b3c``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def setup_logging(run_id: str, log_dir: Path | None = None, level: str = "INFO") -> Path | None:
    """Configure loguru sinks for one pipeline run.

    Every record carries ``run_id`` and ``stage``; key-value context passed via
    ``logger.bind(...)`` or ``logger.info("...", rows=10)`` lands in the JSON ``extra`` field.
    Returns the JSON log file path, if one was written.
    """
    logger.remove()
    logger.configure(extra={"run_id": run_id, "stage": "pipeline"})
    logger.add(sys.stderr, level=level, format=_console_format, colorize=True)

    if log_dir is None:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"pipeline_{run_id}.jsonl"
    logger.add(log_path, level="DEBUG", serialize=True, enqueue=False)
    return log_path
