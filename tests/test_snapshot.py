"""Run-state snapshot — the dashboard contract.

A snapshot is built to LEAVE the machine, so these tests care as much about
what it must not contain as what it must.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claypipe import snapshot as S
from claypipe.cli import app
from claypipe.verdi import loaders as L

runner = CliRunner()


@pytest.fixture
def finished_run(tmp_path: Path, test_clip: Path) -> Path:
    runs_dir = tmp_path / "runs"
    intake = runner.invoke(
        app, ["intake", str(test_clip), "--style", "clay", "--title", "Demo",
              "--runs-dir", str(runs_dir)]
    )
    run_path = Path(intake.stdout.strip().splitlines()[-1])
    (run_path / L.CANARY_VERDICT_NAME).write_text(json.dumps(
        {"schema_version": 1, "approved": True, "decider": "kwasi",
         "decided_at": "2026-09-09T12:00:00.000Z", "frames": {}}
    ))
    assert runner.invoke(app, ["batch", str(run_path), "--runs-dir", str(runs_dir)]).exit_code == 0
    assert runner.invoke(app, ["assemble", str(run_path), "--runs-dir", str(runs_dir)]).exit_code == 0
    return run_path


def export(run_path: Path | None, runs_dir: Path):
    args = ["export", "--runs-dir", str(runs_dir)]
    if run_path is not None:
        args.insert(1, str(run_path))
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    # CliRunner folds stderr logs into output; the JSON document starts at "{".
    text = result.output[result.output.index("{"):]
    return json.loads(text)


def test_snapshot_never_contains_a_local_path(finished_run: Path) -> None:
    """The guard that stops a home-directory layout reaching a hosted service."""
    document = export(finished_run, finished_run.parent)
    blob = json.dumps(document)
    for needle in ("/Users/", "/home/", "/private/var/"):
        assert needle not in blob, f"snapshot leaked {needle}"
    assert document["clip"]["source_name"] == "test_clip.mp4"

    with pytest.raises(ValueError, match="refusing to publish"):
        S.assert_no_local_paths({"path": "/Users/someone/dev/secret"})


def test_snapshot_reports_stage_and_progress(finished_run: Path) -> None:
    document = export(finished_run, finished_run.parent)
    assert document["stage"] == "assembled"
    assert document["progress"] == {"extracted": 60, "restyled": 60, "percent": 100.0}
    assert document["artifacts"]["final_video"] is True
    assert document["config"]["backend"] == "dummy"


def test_snapshot_surfaces_the_canary_gate_state(finished_run: Path) -> None:
    """The thing an operator most wants remotely: what is blocking spend."""
    document = export(finished_run, finished_run.parent)
    assert document["canary"]["state"] == "approved"
    assert document["canary"]["decider"] == "kwasi"

    (finished_run / L.CANARY_VERDICT_NAME).write_text(json.dumps(
        {"schema_version": 1, "approved": False, "reason": "too photoreal", "frames": {}}
    ))
    document = export(finished_run, finished_run.parent)
    assert document["canary"]["state"] == "rejected"
    assert document["canary"]["reason"] == "too photoreal"

    (finished_run / L.CANARY_VERDICT_NAME).unlink()
    assert export(finished_run, finished_run.parent)["canary"]["state"] == "awaiting_review"


def test_snapshot_reports_null_scores_for_an_unscored_run(finished_run: Path) -> None:
    """D29 carried into the dashboard: unmeasured must not render as clean."""
    document = export(finished_run, finished_run.parent)
    assert document["scores"] is None
    assert document["score_series"] == []


def test_snapshot_downsamples_a_long_score_series(tmp_path: Path) -> None:
    """A chart needs shape, not every sample — but never a fabricated one."""
    records = [
        {"frame": f"f_{i:05d}.png", "f": 0.9, "verdict": "PASS", "missed_targets": []}
        for i in range(1000)
    ]
    picked = S._downsample(records)
    assert len(picked) == S.MAX_SERIES_POINTS
    assert picked[0] is records[0], "first frame must survive"
    assert picked[-1] is records[-1], "last frame must survive"
    assert all(p in records for p in picked), "no averaged or invented points"


def test_snapshot_includes_incidents_without_their_absolute_path(
    finished_run: Path
) -> None:
    incidents = finished_run / "incidents"
    incidents.mkdir(exist_ok=True)
    (incidents / "20260909-kill_switch_mean_f.json").write_text(json.dumps({
        "breach": "kill_switch_mean_f", "mean_f": 0.61,
        "run_dir": "/Users/kwasiyeboah/dev/claypipe/runs/demo",
    }))
    document = export(finished_run, finished_run.parent)
    assert len(document["incidents"]) == 1
    note = document["incidents"][0]
    assert note["breach"] == "kill_switch_mean_f" and note["mean_f"] == 0.61
    assert "run_dir" not in note, "the absolute run path must be stripped"
    assert "/Users/" not in json.dumps(document)


def test_snapshot_cost_comes_from_the_ledger(finished_run: Path) -> None:
    document = export(finished_run, finished_run.parent)
    assert set(document["cost_usd"]) >= {"canary", "batch", "retries", "total"}
    assert document["cost_usd"]["total"] == 0.0, "dummy backend genuinely costs nothing"


def test_export_index_lists_every_run(finished_run: Path) -> None:
    index = export(None, finished_run.parent)
    assert index["totals"]["runs"] == 1
    assert index["runs"][0]["run_id"] == finished_run.name
    assert "score_series" not in index["runs"][0], "the index stays light"
    assert index["totals"]["spend_usd"] == 0.0


def test_export_writes_to_a_file_when_asked(finished_run: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "snapshot.json"
    result = runner.invoke(
        app, ["export", str(finished_run), "--runs-dir", str(finished_run.parent),
              "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert json.loads(out.read_text())["run_id"] == finished_run.name
