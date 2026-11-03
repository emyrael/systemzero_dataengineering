from __future__ import annotations

import json

from loguru import logger

from src.utils.log import new_run_id, setup_logging


def test_run_ids_are_unique_and_sortable_by_time():
    first, second = new_run_id(), new_run_id()
    assert first != second
    assert first[:16] <= second[:16]


def test_json_log_carries_run_id_stage_and_context(tmp_path):
    log_path = setup_logging("run-123", tmp_path, level="WARNING")

    logger.bind(stage="ingest").info("Loaded source", rows=505)
    logger.complete()

    record = json.loads(log_path.read_text().splitlines()[-1])["record"]
    assert record["message"] == "Loaded source"
    assert record["extra"] == {"run_id": "run-123", "stage": "ingest", "rows": 505}


def test_console_only_when_no_log_dir():
    assert setup_logging("run-456") is None
