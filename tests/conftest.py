"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.utils.config import DEFAULT_CONFIG_PATH, PipelineConfig, load_config


@pytest.fixture
def config(tmp_path: Path) -> PipelineConfig:
    """Project config with every output path redirected into a temp directory."""
    cfg = load_config(DEFAULT_CONFIG_PATH)
    outputs = cfg.paths.model_copy(
        update={
            name: tmp_path / value.relative_to(cfg.paths.output_dir)
            for name, value in cfg.paths
            if value.is_relative_to(cfg.paths.output_dir)
        }
    )
    return cfg.model_copy(update={"paths": outputs})
