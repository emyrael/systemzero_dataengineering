"""Pipeline entry point.

Usage:
    python run_pipeline.py                         # run every stage
    python run_pipeline.py --stages ingest validate
    python run_pipeline.py --config path/to/config.yaml
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger

from src.pipeline import STAGE_ORDER, PipelineContext, run_pipeline
from src.utils.config import load_config
from src.utils.log import new_run_id, setup_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E-commerce data pipeline")
    parser.add_argument("--config", help="Path to pipeline YAML (default: $PIPELINE_CONFIG or config/)")
    parser.add_argument("--stages", nargs="+", choices=STAGE_ORDER, help="Subset of stages to run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)

    for directory in config.paths.output_dirs():
        directory.mkdir(parents=True, exist_ok=True)

    run_id = new_run_id()
    log_path = setup_logging(run_id, config.paths.log_dir, config.run.log_level)
    logger.info("Pipeline started", as_of_date=str(config.run.as_of_date), log_file=str(log_path))

    summary = run_pipeline(PipelineContext(config=config, run_id=run_id), args.stages)

    logger.info(
        "Pipeline finished",
        succeeded=summary.succeeded,
        failed=summary.failed,
        skipped=summary.skipped,
    )
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
