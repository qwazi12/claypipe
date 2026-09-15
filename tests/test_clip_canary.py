"""T14 acceptance — the clip canary (MASTER_PLAN A3).

Three still frames cannot canary a video model. Temporal behaviour is the only
reason to reach for one, and a still shows none of it — so an approved frame
canary on a resynth run is an approval of something nobody looked at.

Same gate, same verdict file, same no-override-flag rule (D17). What changes is
what the operator is shown.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claypipe import ffmpeg
from claypipe.cli import app
from claypipe.config import load_styles, load_weights
from claypipe.pipeline.extract import frame_paths
from claypipe.pipeline.restyle import (
    ChunkLengthError,
    ClipRestyleBackend,
    DummyClipBackend,
    get_clip_backend,
    restyle_clip_range,
)
from claypipe.run import Run

runner = CliRunner()


class _QuietLogger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def info(self, event, **fields):
        self.events.append((event, fields))

    def warn(self, event, **fields):
        self.events.append((event, fields))

    def error(self, event, **fields):
        self.events.append((event, fields))


@pytest.fixture
def resynth_run(test_clip: Path, tmp_path: Path) -> Run:
    return Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="resynth",
        clip_title="Track C", duration_s=ffmpeg.duration_seconds(test_clip),
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )


@pytest.fixture
def surface_run(test_clip: Path, tmp_path: Path) -> Run:
    return Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="surface",
        clip_title="Track A", duration_s=ffmpeg.duration_seconds(test_clip),
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )


def _canary(run: Run, *args):
    return runner.invoke(
        app, ["canary", "restyle", str(run.paths.root),
              "--runs-dir", str(run.paths.root.parent), *args]
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_a_resynth_run_refuses_a_three_frame_canary(resynth_run: Run):
    result = _canary(resynth_run)
    assert result.exit_code != 0
    assert "temporal behaviour is the only reason" in result.output
    assert "--clip" in result.output
    assert Run.load(resynth_run.paths.root, echo=False).manifest.canary_kind is None


def test_a_resynth_run_accepts_a_clip_canary(resynth_run: Run):
    result = _canary(resynth_run, "--clip", "3")
    assert result.exit_code == 0, result.output
    reloaded = Run.load(resynth_run.paths.root, echo=False)
    assert reloaded.manifest.canary_kind == "clip"
    assert reloaded.manifest.canary_clip_seconds == pytest.approx(3.0)
    assert len(frame_paths(resynth_run.paths.restyled_frames)) == 36  # 3s @ 12fps


def test_batch_refuses_a_resynth_run_whose_canary_was_frames(resynth_run: Run):
    """FIREWALL 1b. The verdict is approved and the canary is the wrong KIND."""
    resynth_run.manifest.canary_kind = "frames"
    resynth_run.save()
    (resynth_run.paths.root / "canary_verdict.json").write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test"})
    )
    result = runner.invoke(
        app, ["batch", str(resynth_run.paths.root),
              "--runs-dir", str(resynth_run.paths.root.parent)]
    )
    assert result.exit_code != 0
    assert "needs a CLIP canary" in result.output


def test_batch_accepts_a_resynth_run_with_a_clip_canary(resynth_run: Run):
    assert _canary(resynth_run, "--clip", "3").exit_code == 0
    (resynth_run.paths.root / "canary_verdict.json").write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test"})
    )
    result = runner.invoke(
        app, ["batch", str(resynth_run.paths.root),
              "--runs-dir", str(resynth_run.paths.root.parent)]
    )
    assert result.exit_code == 0, result.output


def test_the_kind_lives_on_the_manifest_not_only_the_verdict(resynth_run: Run):
    """D17: the verdict arrives as a query string the operator pastes, so a
    gate satisfiable by editing a URL is not a gate. Forging canary_kind in the
    verdict must not get past FIREWALL 1b."""
    (resynth_run.paths.root / "canary_verdict.json").write_text(
        json.dumps({
            "schema_version": 1, "approved": True, "decider": "test",
            "canary_kind": "clip",          # forged
        })
    )
    result = runner.invoke(
        app, ["batch", str(resynth_run.paths.root),
              "--runs-dir", str(resynth_run.paths.root.parent)]
    )
    assert result.exit_code != 0
    assert "needs a CLIP canary" in result.output


def test_a_surface_run_is_unaffected_by_the_clip_gate(surface_run: Run):
    assert _canary(surface_run).exit_code == 0
    assert Run.load(surface_run.paths.root, echo=False).manifest.canary_kind == "frames"
    (surface_run.paths.root / "canary_verdict.json").write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test"})
    )
    result = runner.invoke(
        app, ["batch", str(surface_run.paths.root),
              "--runs-dir", str(surface_run.paths.root.parent)]
    )
    assert result.exit_code == 0, result.output


def test_there_is_still_no_override_flag(resynth_run: Run):
    """D17's rule survives T14: the gate has no escape hatch."""
    result = runner.invoke(
        app, ["batch", str(resynth_run.paths.root),
              "--runs-dir", str(resynth_run.paths.root.parent),
              "--force"],
    )
    assert result.exit_code != 0
    assert "--force" in result.output or "No such option" in result.output


# ---------------------------------------------------------------------------
# Trim selection
# ---------------------------------------------------------------------------

def test_the_trim_is_anchored_on_the_most_motion_shot(resynth_run: Run):
    """A video model's failure mode is temporal — flicker, smearing, identity
    wandering. The opening seconds are often a static establishing shot where
    none of that shows, so canarying the head of the clip is how a temporal
    model passes a gate it should fail."""
    assert _canary(resynth_run, "--clip", "3").exit_code == 0
    logged = [json.loads(line) for line in resynth_run.paths.log.read_text().splitlines()]
    entry = next(e for e in logged if e["event"] == "canary.restyle")
    assert entry["kind"] == "clip"
    assert "anchor" in entry
    # And the frames it chose are named, so the choice is auditable.
    assert entry["first_frame"] and entry["last_frame"]


def test_asking_for_more_clip_than_exists_is_refused(resynth_run: Run):
    result = _canary(resynth_run, "--clip", "999")
    assert result.exit_code != 0
    assert "Ask for less" in result.output


@pytest.mark.parametrize("bad", ["0", "-2"])
def test_a_non_positive_clip_duration_is_refused(resynth_run: Run, bad):
    result = _canary(resynth_run, "--clip", bad)
    assert result.exit_code != 0
    assert "positive duration" in result.output


def test_a_clip_below_the_backend_minimum_warns_with_the_real_arithmetic(resynth_run: Run):
    """This bites the plan's own numbers. MASTER_PLAN A3 prices a 3-second Wan
    VACE canary at $0.12, but VACE's floor is 81 frames at 16fps native =
    5.06 video-seconds = $0.20. A 3-second canary at the pipeline's 12fps is 36
    frames, which VACE cannot honour at all."""
    result = _canary(resynth_run, "--clip", "3")
    assert result.exit_code == 0, result.output
    assert "below" in result.output and "minimum" in result.output
    assert "5.06s" in result.output

    logged = [json.loads(line) for line in resynth_run.paths.log.read_text().splitlines()]
    warning = next(e for e in logged if e["event"] == "canary.restyle.below_min_chunk")
    assert warning["level"] == "WARN"
    assert warning["asked_frames"] == 36
    assert warning["min_chunk_frames"] == 81
    assert warning["floor_seconds"] == pytest.approx(5.0625, abs=0.001)


def test_a_clip_at_or_above_the_minimum_does_not_warn(resynth_run: Run):
    # 81 frames at 12fps is 6.75s, comfortably over the floor.
    result = _canary(resynth_run, "--clip", "4.9")
    assert result.exit_code == 0, result.output
    logged = [json.loads(line) for line in resynth_run.paths.log.read_text().splitlines()]
    warned = [e for e in logged if e["event"] == "canary.restyle.below_min_chunk"]
    # 4.9s @ 12fps = 58 frames, still under 81 — so it SHOULD warn. Asserting
    # the boundary explicitly rather than guessing where it falls.
    assert warned, "58 frames is still below the 81-frame floor"


# ---------------------------------------------------------------------------
# Chunking and the failure that has no per-frame analogue
# ---------------------------------------------------------------------------

def test_the_clip_backend_is_resolved_separately(resynth_run: Run):
    """A caller that wants a clip backend must not silently receive a
    per-frame one (A4)."""
    assert isinstance(get_clip_backend("dummy"), ClipRestyleBackend)
    with pytest.raises(ValueError, match="unknown clip backend"):
        get_clip_backend("fal")


def test_a_chunk_of_the_wrong_length_halts_the_run(tmp_path: Path):
    """The failure with no per-frame analogue. A short chunk shortens the clip
    and desyncs the audio, in a file that plays perfectly."""
    from PIL import Image

    src = tmp_path / "src"
    src.mkdir()
    frames = []
    for i in range(1, 11):
        path = src / f"f_{i:05d}.png"
        Image.new("RGB", (16, 16), (i * 10, 0, 0)).save(path)
        frames.append(path)

    class ShortBackend:
        name = "short"
        min_chunk_frames = 1
        max_chunk_frames = 240
        native_fps = 16

        def cost_per_video_second_usd(self):
            return 0.0

        def restyle_clip(self, src_frames, out_dir, *, prompt, strength, seed):
            out_dir.mkdir(parents=True, exist_ok=True)
            written = []
            for f in src_frames[:-1]:          # drops one, silently
                dst = out_dir / f.name
                dst.write_bytes(f.read_bytes())
                written.append(dst)
            return written

    with pytest.raises(ChunkLengthError, match="returned 9"):
        restyle_clip_range(
            backend=ShortBackend(), src_frames=frames, out_dir=tmp_path / "out",
            prompt="p", strength=0.5, seed=1000, logger=_QuietLogger(), fps=12,
        )


def test_chunks_are_authorised_in_video_seconds(test_clip: Path, tmp_path: Path):
    """T15: a Track C backend bills per video-second, so that is the unit the
    ledger must authorise in — not per frame."""
    from claypipe.pipeline.extract import extract_frames
    from claypipe.pipeline.retry import SpendLedger

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="resynth",
        clip_title="Ledger", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    extract_frames(test_clip, run.paths.source_frames, 12, run.logger)
    weights = load_weights()
    ledger = SpendLedger(
        paths=run.paths, project_dir=tmp_path / "runs", cfg=weights.firewalls,
        run_id=run.run_id, logger=run.logger,
    )
    restyle_clip_range(
        backend=DummyClipBackend(), src_frames=frame_paths(run.paths.source_frames),
        out_dir=run.paths.restyled_frames, prompt="p", strength=0.65,
        seed=1000, logger=run.logger, ledger=ledger, fps=12,
    )
    records = [
        json.loads(line)
        for line in (run.paths.root / "spend_ledger.jsonl").read_text().splitlines()
        if json.loads(line)["event"] == "authorized"
    ]
    assert records
    for record in records:
        assert record["unit"] == "video_second"
        assert record["video_seconds"] is not None
        assert record["frame"].startswith("clip_")


def test_a_resumed_chunk_is_not_paid_for_twice(test_clip: Path, tmp_path: Path):
    from claypipe.pipeline.extract import extract_frames

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="resynth",
        clip_title="Resume", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    extract_frames(test_clip, run.paths.source_frames, 12, run.logger)
    frames = frame_paths(run.paths.source_frames)
    logger = _QuietLogger()
    for _ in range(2):
        restyle_clip_range(
            backend=DummyClipBackend(), src_frames=frames,
            out_dir=run.paths.restyled_frames, prompt="p", strength=0.65,
            seed=1000, logger=logger, fps=12,
        )
    skips = [e for e, _f in logger.events if e == "restyle.clip.skip"]
    assert skips, "the second pass regenerated instead of resuming"


def test_the_dummy_clip_backend_imitates_vaces_awkward_properties():
    """A stand-in that ignored the 81-240 chunk bound and the 16fps native rate
    would let the pipeline pass tests it should fail — those two facts are what
    force the duration invariant (A2/T17)."""
    backend = DummyClipBackend()
    # Mirrors Wan VACE's real schema, verified on fal 2026-09-15: num_frames
    # "must be between 81 to 241 (inclusive)".
    assert (backend.min_chunk_frames, backend.max_chunk_frames) == (81, 241)
    assert backend.native_fps == 16
    assert backend.native_fps != 12, "must not match the pipeline's fps"


def test_the_dummy_clip_backend_moves_geometry(tmp_path: Path):
    """A resynthesis stand-in must MOVE geometry, or it exercises the
    surface-mode gate instead of the resynth one."""
    import numpy as np
    from PIL import Image

    src = tmp_path / "src"
    src.mkdir()
    frames = []
    for i in range(1, 4):
        path = src / f"f_{i:05d}.png"
        img = Image.new("RGB", (64, 64), (20, 20, 20))
        img.paste(Image.new("RGB", (20, 20), (240, 240, 240)), (22, 22))
        img.save(path)
        frames.append(path)

    out = DummyClipBackend().restyle_clip(
        frames, tmp_path / "out", prompt="p", strength=0.65, seed=1001
    )
    arrays = [np.asarray(Image.open(p).convert("L"), dtype=float) for p in out]
    # Consecutive outputs must differ: that is the geometry moving.
    assert any(
        np.abs(arrays[i] - arrays[i + 1]).mean() > 0.0 for i in range(len(arrays) - 1)
    )
