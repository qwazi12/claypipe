"""The operator-facing canary commands (step 5, commits 3-5).

render -> pack -> the human decides -> submit. These three close the loop that
Step 4 deliberately left open: the pages existed but nothing generated them and
nothing recorded what came back.

No network, no server, no spend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claypipe.cli import app
from claypipe.verdi import canary_page, flag_page
from claypipe.verdi import loaders as L

runner = CliRunner()


@pytest.fixture
def batched_run(tmp_path: Path, test_clip: Path) -> Path:
    """An approved, completed dummy run — frames on disk, nothing scored."""
    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app, ["intake", str(test_clip), "--style", "clay", "--runs-dir", str(runs_dir)]
    )
    assert result.exit_code == 0, result.output
    run_path = Path(result.stdout.strip().splitlines()[-1])
    (run_path / L.CANARY_VERDICT_NAME).write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test", "frames": {}})
    )
    assert runner.invoke(
        app, ["batch", str(run_path), "--runs-dir", str(runs_dir)]
    ).exit_code == 0
    return run_path


def canary(run_path: Path, *args):
    return runner.invoke(
        app, ["canary", *args, str(run_path), "--runs-dir", str(run_path.parent)]
    )


# --------------------------------------------------------------------------
# commit 3 — submit
# --------------------------------------------------------------------------

def test_submit_cli_writes_verdict_atomically(batched_run: Path, monkeypatch) -> None:
    """The gate polls this file, so it must appear whole or not at all.

    Proven by intercepting the rename: everything is written to a temp path
    first, and the final path does not exist until the rename lands.
    """
    seen = {}
    real_replace = Path.replace

    def spy(self, target):
        seen["tmp"] = str(self)
        seen["final_existed_before"] = Path(target).exists()
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy)

    # The fixture pre-approved this run so batch could complete; clear it so
    # the "appears whole or not at all" property is observable on a fresh write.
    (batched_run / L.CANARY_VERDICT_NAME).unlink()

    url = (
        "file:///x/canary_review.html?schema_version=1&decider=kwasi&approved=true"
        "&frame:f_00001.png:verdict=approve&frame:f_00001.png:note=clean"
    )
    result = runner.invoke(
        app, ["canary", "submit", str(batched_run), "--url", url,
              "--runs-dir", str(batched_run.parent)]
    )
    assert result.exit_code == 0, result.output
    assert "APPROVED by kwasi" in result.output

    assert seen["tmp"].endswith(".json.tmp"), "verdict was not staged through a temp file"
    assert seen["final_existed_before"] is False, (
        "the final path existed before the rename — it was written in place"
    )
    assert not Path(seen["tmp"]).exists(), "temp file left behind"

    written = json.loads((batched_run / L.CANARY_VERDICT_NAME).read_text())
    assert written["approved"] is True
    assert written["decider"] == "kwasi"
    assert written["frames"]["f_00001.png"]["verdict"] == "approve"


def test_submit_cli_surfaces_a_rejection_reason(batched_run: Path) -> None:
    url = "?approved=false&reason=the+set+still+reads+photoreal&decider=kwasi"
    result = runner.invoke(
        app, ["canary", "submit", str(batched_run), "--url", url,
              "--runs-dir", str(batched_run.parent)]
    )
    assert result.exit_code == 0
    assert "REJECTED" in result.output
    assert "the set still reads photoreal" in result.output


def test_submit_cli_verbose_echoes_each_frame(batched_run: Path) -> None:
    url = (
        "?approved=true&decider=kwasi"
        "&frame:f_00001.png:verdict=approve&frame:f_00001.png:note=clean"
        "&frame:f_00030.png:verdict=reject&frame:f_00030.png:note=melted"
    )
    result = runner.invoke(
        app, ["canary", "submit", str(batched_run), "--url", url,
              "--runs-dir", str(batched_run.parent), "--verbose"]
    )
    assert result.exit_code == 0
    assert "f_00001.png: approve — clean" in result.output
    assert "f_00030.png: reject — melted" in result.output


def test_submit_cli_refuses_a_submission_that_fails_its_schema(batched_run: Path) -> None:
    """A rejection with no reason is not a decision anyone can act on later."""
    result = runner.invoke(
        app, ["canary", "submit", str(batched_run), "--url", "?approved=false&reason=",
              "--runs-dir", str(batched_run.parent)]
    )
    assert result.exit_code == 1
    assert "reason" in result.output
    assert not (batched_run / L.CANARY_VERDICT_NAME).with_suffix(".json.tmp").exists()


# --------------------------------------------------------------------------
# commit 4 — render
# --------------------------------------------------------------------------

def test_render_cli_generates_pages_from_run_state(batched_run: Path) -> None:
    """Three representative frames, inlined, self-contained."""
    result = canary(batched_run, "render")
    assert result.exit_code == 0, result.output

    page = batched_run / canary_page.PAGE_NAME
    assert page.is_file()
    html = page.read_text()

    assert html.count("data:image/png;base64,") == 3
    for offender in ("http://", "https://", "<script", "<link", "@import"):
        assert offender not in html
    assert "first frame" in html and "stand-in for most-motion" in html
    # dummy run -> unscored -> the caveat must be present
    assert "Caveat" in html
    assert "flag page skipped" in result.output


def test_render_cli_refuses_when_there_is_nothing_to_review(
    tmp_path: Path, test_clip: Path
) -> None:
    runs_dir = tmp_path / "runs"
    intake = runner.invoke(
        app, ["intake", str(test_clip), "--style", "clay", "--runs-dir", str(runs_dir)]
    )
    run_path = Path(intake.stdout.strip().splitlines()[-1])
    result = canary(run_path, "render")
    assert result.exit_code == 1
    assert "no restyled frames" in result.output


def test_render_cli_also_renders_the_flag_page_when_scores_exist(batched_run: Path) -> None:
    """A canary reviewer should see what the scorer already flagged."""
    scores = []
    for i in (1, 2, 3, 4):
        failing = i <= 2
        scores.append({
            "frame": f"f_{i:05d}.png", "ssim": 0.8, "lpips_edges": 0.2,
            "identity": 0.9, "temporal": 0.97, "f": 0.60 if failing else 0.92,
            "verdict": "FAIL" if failing else "PASS",
            "reason": "composite_fail" if failing else "accepted",
            "targets_met": {"ssim": not failing, "lpips_edges": True, "id": True, "tf": True},
            "missed_targets": ["ssim"] if failing else [],
        })
    (batched_run / "scores.jsonl").write_text(
        "\n".join(json.dumps(s) for s in scores) + "\n"
    )

    result = canary(batched_run, "render")
    assert result.exit_code == 0, result.output
    flag = batched_run / flag_page.PAGE_NAME
    assert flag.is_file()
    html = flag.read_text()
    assert "f_00001.png" in html and "f_00002.png" in html
    for offender in ("http://", "https://", "<script", "<link"):
        assert offender not in html


# --------------------------------------------------------------------------
# commit 5 — pack
# --------------------------------------------------------------------------

def test_pack_cli_opens_browser_no_server(batched_run: Path, monkeypatch) -> None:
    """webbrowser.open on a local file:// URL. No server is ever started."""
    canary(batched_run, "render")

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    result = canary(batched_run, "pack")
    assert result.exit_code == 0, result.output
    assert len(opened) == 1
    assert opened[0].startswith("file://")
    assert opened[0].endswith(canary_page.PAGE_NAME)
    assert "://localhost" not in opened[0] and "127.0.0.1" not in opened[0]


def test_pack_cli_can_print_without_opening(batched_run: Path, monkeypatch) -> None:
    canary(batched_run, "render")
    monkeypatch.setattr(
        "webbrowser.open", lambda url: pytest.fail("--no-open must not open a browser")
    )
    result = canary(batched_run, "pack", "--no-open")
    assert result.exit_code == 0
    # CliRunner folds the structured stderr log into `output`; the URL is the
    # last line, which is what a shell pipeline would consume.
    assert result.output.strip().splitlines()[-1].startswith("file://")


def test_pack_cli_refuses_before_render(batched_run: Path) -> None:
    result = canary(batched_run, "pack")
    assert result.exit_code == 1
    assert "canary render" in result.output
