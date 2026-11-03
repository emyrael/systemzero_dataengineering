from __future__ import annotations

import pytest

from src import pipeline
from src.pipeline import PipelineContext, Stage, run_pipeline


@pytest.fixture
def ctx(config) -> PipelineContext:
    return PipelineContext(config=config, run_id="test-run")


@pytest.fixture
def stages(monkeypatch) -> dict[str, Stage]:
    registry: dict[str, Stage] = {}
    monkeypatch.setattr(pipeline, "STAGES", registry)
    return registry


def _ok(calls: list[str], name: str) -> Stage:
    return Stage(name, lambda ctx: calls.append(name))


def _boom(ctx: PipelineContext) -> None:
    raise RuntimeError("boom")


def test_runs_registered_stages_in_canonical_order(ctx, stages):
    calls: list[str] = []
    stages["clean"] = _ok(calls, "clean")
    stages["ingest"] = _ok(calls, "ingest")

    summary = run_pipeline(ctx, ["clean", "ingest"])

    assert calls == ["ingest", "clean"]
    assert summary.succeeded == ["ingest", "clean"] and summary.ok


def test_unimplemented_stages_are_skipped(ctx, stages):
    summary = run_pipeline(ctx, ["ingest"])
    assert summary.skipped == ["ingest"] and summary.ok


def test_failure_skips_dependents_but_not_independent_stages(ctx, stages):
    calls: list[str] = []
    stages["ingest"] = Stage("ingest", _boom)
    stages["clean"] = Stage("clean", lambda ctx: calls.append("clean"), depends_on=("ingest",))
    stages["dashboard"] = _ok(calls, "dashboard")

    summary = run_pipeline(ctx)

    assert summary.failed == ["ingest"]
    assert "clean" in summary.skipped
    assert calls == ["dashboard"]
    assert not summary.ok


def test_unknown_stage_name_raises(ctx, stages):
    with pytest.raises(ValueError, match="nope"):
        run_pipeline(ctx, ["nope"])
