from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.utils.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_config


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def raw_config() -> dict:
    return yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())


def test_default_config_loads_with_absolute_paths():
    cfg = load_config()
    assert cfg.run.as_of_date == date(2025, 1, 1)
    assert cfg.paths.raw_dir == PROJECT_ROOT / "data"
    assert all(path.is_absolute() for _, path in cfg.paths)


def test_every_source_file_exists():
    cfg = load_config()
    assert len(cfg.sources) == 8
    missing = [name for name in cfg.sources if not cfg.source_path(name).is_file()]
    assert missing == []


def test_env_var_selects_config(tmp_path, monkeypatch, raw_config):
    raw_config["run"]["as_of_date"] = "2026-01-29"
    monkeypatch.setenv("PIPELINE_CONFIG", str(_write(tmp_path, raw_config)))
    assert load_config().run.as_of_date == date(2026, 1, 29)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_unknown_keys_are_rejected(tmp_path, raw_config):
    raw_config["run"]["typo_key"] = 1
    with pytest.raises(ValidationError, match="typo_key"):
        load_config(_write(tmp_path, raw_config))


def test_json_source_requires_record_path(tmp_path, raw_config):
    del raw_config["sources"]["customers"]["record_path"]
    with pytest.raises(ValidationError, match="record_path"):
        load_config(_write(tmp_path, raw_config))


def test_surreal_credentials_come_from_env(monkeypatch):
    cfg = load_config()
    monkeypatch.setenv("SURREAL_USER", "alice")
    monkeypatch.setenv("SURREAL_PASS", "s3cret")
    assert cfg.surrealdb.credentials() == ("alice", "s3cret")
