"""Typed pipeline configuration loaded from YAML."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "pipeline_config.yaml"
CONFIG_ENV_VAR = "PIPELINE_CONFIG"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunConfig(_Strict):
    as_of_date: date
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class PathsConfig(_Strict):
    raw_dir: Path
    output_dir: Path
    bronze_dir: Path
    silver_dir: Path
    gold_dir: Path
    quarantine_dir: Path
    log_dir: Path
    quality_report: Path
    dashboard: Path

    def resolved(self, root: Path) -> PathsConfig:
        """Return a copy with relative paths anchored at ``root``."""
        return self.model_copy(
            update={name: root / value for name, value in self if not value.is_absolute()}
        )

    def output_dirs(self) -> list[Path]:
        return [
            self.output_dir,
            self.bronze_dir,
            self.silver_dir,
            self.gold_dir,
            self.quarantine_dir,
            self.log_dir,
            self.quality_report.parent,
            self.dashboard.parent,
        ]


class SourceConfig(_Strict):
    file: str
    format: Literal["csv", "json", "parquet"]
    primary_key: tuple[str, ...] = Field(min_length=1)
    record_path: str | None = None

    @model_validator(mode="after")
    def _json_needs_record_path(self) -> SourceConfig:
        if self.format == "json" and not self.record_path:
            raise ValueError(f"JSON source '{self.file}' needs a record_path")
        return self


class ReferenceConfig(_Strict):
    base_currency: str
    payment_terms_days: dict[str, int]
    country_regions: dict[str, str]


class FeaturesConfig(_Strict):
    customer_lifetime_months: int = Field(default=36, gt=0)
    turnover_window_days: int = Field(default=365, gt=0)


class SurrealConfig(_Strict):
    url: str
    namespace: str
    database: str
    username_env: str = "SURREAL_USER"
    password_env: str = "SURREAL_PASS"
    batch_size: int = Field(default=500, gt=0)

    def credentials(self) -> tuple[str, str]:
        """Read credentials from the environment; local Docker defaults to root/root."""
        return os.getenv(self.username_env, "root"), os.getenv(self.password_env, "root")


class PipelineConfig(_Strict):
    run: RunConfig
    paths: PathsConfig
    sources: dict[str, SourceConfig]
    reference: ReferenceConfig
    features: FeaturesConfig = FeaturesConfig()
    surrealdb: SurrealConfig

    def source_path(self, name: str) -> Path:
        return self.paths.raw_dir / self.sources[name].file


def load_config(path: Path | str | None = None, root: Path = PROJECT_ROOT) -> PipelineConfig:
    """Load and validate the pipeline config.

    Resolution order: explicit ``path``, then ``$PIPELINE_CONFIG``, then the default file.
    Relative paths inside the config are anchored at ``root``.
    """
    config_path = Path(path or os.getenv(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)
    if not config_path.is_file():
        raise FileNotFoundError(f"Pipeline config not found: {config_path}")

    with config_path.open() as fh:
        raw = yaml.safe_load(fh) or {}

    config = PipelineConfig.model_validate(raw)
    return config.model_copy(update={"paths": config.paths.resolved(root)})
