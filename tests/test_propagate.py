"""T18 acceptance — adaptive keyframe propagation (MASTER_PLAN §1.3).

Two of T18's premises did not survive measurement; see propagate.py's module
docstring and memory.md D49. The tests below assert what was MEASURED, not what
was assumed — including the finding that temporal fidelity cannot validate
propagation at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claypipe.config import load_styles, load_weights
from claypipe.pipeline.propagate import (
    DEFAULT_MAX_CHAIN,
    DEFAULT_RESIDUAL_MAX,
    KeyframePlan,
    PropagationError,
    execute_plan,
    flow_residual,
    plan_keyframes,
    propagate_frame,
    warp,
)
from claypipe.pipeline.shots import Shot, ShotPlan, single_shot_plan

pytest.importorskip("cv2", reason="needs the [scoring] extra")


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
def temporal():
    return load_weights().temporal


@pytest.fixture
def panning_frames(tmp_path: Path) -> list[Path]:
    """24 frames of a shape moving steadily — flow explains this perfectly."""
    from PIL import Image

    out = tmp_path / "pan"
    out.mkdir()
    paths = []
    for i in range(24):
        img = Image.new("RGB", (96, 64), (30, 30, 40))
        img.paste(Image.new("RGB", (18, 18), (230, 210, 120)), (5 + i * 2, 20))
        path = out / f"f_{i + 1:05d}.png"
        img.save(path)
        paths.append(path)
    return paths


@pytest.fixture
def teleporting_frames(tmp_path: Path) -> list[Path]:
    """24 frames where the shape jumps randomly — flow cannot explain it."""
    import random

    from PIL import Image

    rng = random.Random(7)
    out = tmp_path / "jump"
    out.mkdir()
    paths = []
    for i in range(24):
        img = Image.new("RGB", (96, 64), (30, 30, 40))
        img.paste(
            Image.new("RGB", (18, 18), (230, 210, 120)),
            (rng.randrange(0, 70), rng.randrange(0, 40)),
        )
        path = out / f"f_{i + 1:05d}.png"
        img.save(path)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# The residual is measured in the SOURCE domain — the core design decision
# ---------------------------------------------------------------------------

def test_flow_explains_smooth_motion_and_not_teleportation(
    panning_frames, teleporting_frames, temporal
):
    """The whole scheme rests on this: a source-domain residual tells you
    whether a warp is trustworthy, and it is available for free BEFORE any
    money is spent."""
    from claypipe.pipeline.propagate import _gray

    smooth = flow_residual(_gray(panning_frames[5]), _gray(panning_frames[6]), temporal)
    jumpy = flow_residual(
        _gray(teleporting_frames[5]), _gray(teleporting_frames[6]), temporal
    )
    assert smooth < DEFAULT_RESIDUAL_MAX < jumpy, f"smooth={smooth}, jumpy={jumpy}"


def test_smooth_motion_propagates_and_teleportation_pays(
    panning_frames, teleporting_frames, temporal
):
    smooth_plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal,
    )
    jumpy_plan = plan_keyframes(
        source_frames=teleporting_frames,
        shot_plan=single_shot_plan(len(teleporting_frames), 12),
        cfg=temporal,
    )
    assert smooth_plan.paid_frames < jumpy_plan.paid_frames
    # Steady panning should need barely more than the shot open plus the chain
    # limit, i.e. nothing should be paid for because flow failed.
    assert smooth_plan.summary()["keyframe_reasons"].get("flow_failed", 0) == 0
    assert jumpy_plan.summary()["keyframe_reasons"]["flow_failed"] > 0


# ---------------------------------------------------------------------------
# Shot boundaries are unconditional keyframes — why T11 comes first
# ---------------------------------------------------------------------------

def test_every_shot_opens_with_a_paid_keyframe(panning_frames, temporal):
    """A warp across a cut produces garbage with a plausible-looking residual,
    because the previous frame is a different scene entirely."""
    shot_plan = ShotPlan(
        fps=12, total_frames=24,
        shots=[
            Shot(index=0, start_frame=1, end_frame=8, start_s=0.0, end_s=0.67),
            Shot(index=1, start_frame=9, end_frame=16, start_s=0.67, end_s=1.33),
            Shot(index=2, start_frame=17, end_frame=24, start_s=1.33, end_s=2.0),
        ],
    )
    plan = plan_keyframes(source_frames=panning_frames, shot_plan=shot_plan, cfg=temporal)
    assert plan.is_keyframe(1)
    assert plan.is_keyframe(9)
    assert plan.is_keyframe(17)
    assert plan.summary()["keyframe_reasons"]["shot_open"] == 3


def test_a_shot_open_is_a_keyframe_even_when_flow_would_have_been_fine(
    panning_frames, temporal
):
    """Unconditional: the residual is not consulted at a boundary."""
    shot_plan = ShotPlan(
        fps=12, total_frames=24,
        shots=[
            Shot(index=0, start_frame=1, end_frame=12, start_s=0.0, end_s=1.0),
            Shot(index=1, start_frame=13, end_frame=24, start_s=1.0, end_s=2.0),
        ],
    )
    plan = plan_keyframes(source_frames=panning_frames, shot_plan=shot_plan, cfg=temporal)
    frame_13 = next(f for f in plan.frames if f.index == 13)
    assert frame_13.is_keyframe
    assert frame_13.reason == "shot_open"
    assert frame_13.residual is None, "the residual must not even be computed"


# ---------------------------------------------------------------------------
# The chain limit
# ---------------------------------------------------------------------------

def test_the_chain_limit_forces_a_keyframe(panning_frames, temporal):
    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal, max_chain=4,
    )
    assert plan.summary()["keyframe_reasons"]["chain_limit"] > 0
    for frame in plan.frames:
        assert frame.chain_length <= 4


def test_a_longer_chain_limit_costs_less(panning_frames, temporal):
    tight = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal, max_chain=3,
    )
    loose = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal, max_chain=20,
    )
    assert loose.paid_frames < tight.paid_frames


# ---------------------------------------------------------------------------
# Planning is free and happens BEFORE any spend
# ---------------------------------------------------------------------------

def test_planning_spends_nothing_and_reports_the_cost_first(panning_frames, temporal):
    """A propagation scheme that only reveals its cost by spending it is not a
    cost reduction."""
    logger = _QuietLogger()
    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal, logger=logger,
    )
    event, fields = next((e, f) for e, f in logger.events if e == "propagate.planned")
    assert fields["paid_frames"] == plan.paid_frames
    assert fields["reduction_factor"] > 1.0
    # No ledger was touched — planning takes no ledger at all.
    assert "cost" not in fields


def test_the_plan_records_why_each_frame_was_paid_for(panning_frames, temporal):
    """Auditable rather than trusted."""
    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal, max_chain=4,
    )
    reasons = {f.reason for f in plan.frames}
    assert reasons <= {"shot_open", "flow_failed", "chain_limit", "propagated"}
    for frame in plan.frames:
        if frame.is_keyframe:
            assert frame.reason != "propagated"
        else:
            assert frame.reason == "propagated"
            assert frame.residual is not None


def test_plan_round_trips_through_json(panning_frames, temporal, tmp_path: Path):
    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal,
    )
    path = plan.write(tmp_path / "keyframes.json")
    again = KeyframePlan.read(path)
    assert again.keyframes == plan.keyframes
    assert again.residual_max == plan.residual_max
    assert again.max_chain == plan.max_chain


def test_a_plan_for_a_different_run_is_refused(panning_frames, temporal):
    with pytest.raises(PropagationError, match="different run"):
        plan_keyframes(
            source_frames=panning_frames,
            shot_plan=single_shot_plan(999, 12),
            cfg=temporal,
        )


def test_no_frames_is_a_hard_error(temporal):
    with pytest.raises(PropagationError, match="no source frames"):
        plan_keyframes(source_frames=[], shot_plan=single_shot_plan(1, 12), cfg=temporal)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def test_execution_pays_only_for_keyframes(panning_frames, temporal, tmp_path: Path):
    from claypipe.pipeline.restyle import DummyBackend

    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal, max_chain=6,
    )
    paid_calls: list[int] = []
    backend = DummyBackend()

    def restyle_keyframe(index, src, dst):
        paid_calls.append(index)
        backend.restyle(src, dst, prompt="clay", strength=0.65, seed=1000)

    result = execute_plan(
        plan=plan, source_frames=panning_frames, out_dir=tmp_path / "out",
        cfg=temporal, restyle_keyframe=restyle_keyframe, logger=_QuietLogger(),
    )
    assert sorted(paid_calls) == plan.keyframes
    assert result["paid_frames"] == plan.paid_frames
    assert result["propagated_frames"] == plan.propagated_frames
    # Every frame exists on disk, paid or warped.
    produced = sorted((tmp_path / "out").glob("f_*.png"))
    assert len(produced) == len(panning_frames)


def test_execution_is_resume_safe(panning_frames, temporal, tmp_path: Path):
    from claypipe.pipeline.restyle import DummyBackend

    backend = DummyBackend()
    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal,
    )

    def restyle_keyframe(index, src, dst):
        backend.restyle(src, dst, prompt="clay", strength=0.65, seed=1000)

    for _ in range(2):
        result = execute_plan(
            plan=plan, source_frames=panning_frames, out_dir=tmp_path / "out",
            cfg=temporal, restyle_keyframe=restyle_keyframe, logger=_QuietLogger(),
        )
    assert result["paid_frames"] == 0, "the second pass re-spent"
    assert result["resumed_frames"] == len(panning_frames)


def test_an_out_of_order_chain_is_refused(panning_frames, temporal, tmp_path: Path):
    """A warp needs its predecessor on disk. Executing out of order would warp
    from a frame that does not exist yet."""
    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal,
    )
    # Hand it a plan whose first entry is a propagated frame.
    broken = KeyframePlan(
        total_frames=plan.total_frames,
        frames=[f for f in plan.frames if not f.is_keyframe][:1],
        residual_max=plan.residual_max, max_chain=plan.max_chain,
    )
    with pytest.raises(PropagationError, match="executed in order"):
        execute_plan(
            plan=broken, source_frames=panning_frames, out_dir=tmp_path / "out",
            cfg=temporal, restyle_keyframe=lambda i, s, d: None,
            logger=_QuietLogger(),
        )


def test_execution_reports_the_chain_depth_it_actually_used(
    panning_frames, temporal, tmp_path: Path
):
    """TF cannot detect propagation drift (FINDING 1), so the depth is reported
    for a reviewer to judge."""
    from claypipe.pipeline.restyle import DummyBackend

    backend = DummyBackend()
    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal, max_chain=5,
    )
    result = execute_plan(
        plan=plan, source_frames=panning_frames, out_dir=tmp_path / "out",
        cfg=temporal,
        restyle_keyframe=lambda i, s, d: backend.restyle(
            s, d, prompt="clay", strength=0.65, seed=1000
        ),
        logger=_QuietLogger(),
    )
    assert result["max_chain_depth_used"] <= 5
    assert result["mean_chain_depth"] > 0


# ---------------------------------------------------------------------------
# The warp itself
# ---------------------------------------------------------------------------

def test_a_warp_follows_the_motion(panning_frames, temporal, tmp_path: Path):
    """The flow is estimated between SOURCE frames, because the restyle changes
    the texture the estimator keys on."""
    import numpy as np
    from PIL import Image

    from claypipe.pipeline.restyle import DummyBackend

    out = tmp_path / "out"
    out.mkdir()
    restyled_prev = out / panning_frames[5].name
    DummyBackend().restyle(
        panning_frames[5], restyled_prev, prompt="clay", strength=0.65, seed=1000
    )
    dst = propagate_frame(
        source_previous=panning_frames[5], source_current=panning_frames[6],
        restyled_previous=restyled_prev, dst=out / panning_frames[6].name,
        cfg=temporal,
    )
    assert dst.is_file()
    with Image.open(dst) as a, Image.open(restyled_prev) as b:
        assert a.size == b.size
        moved = np.abs(
            np.asarray(a.convert("L"), float) - np.asarray(b.convert("L"), float)
        ).mean()
    assert moved > 0.0, "the warp did nothing"


def test_warp_preserves_shape_and_channels(temporal):
    import numpy as np

    image = np.zeros((32, 48, 3), dtype=np.uint8)
    flow = np.zeros((32, 48, 2), dtype=np.float32)
    assert warp(image, flow).shape == image.shape


# ---------------------------------------------------------------------------
# The CLI path
# ---------------------------------------------------------------------------

def test_batch_propagate_refuses_a_resynth_run(test_clip: Path, tmp_path: Path):
    """A clip backend already works on ranges and produces its own temporal
    coherence; warping its output would fight the thing it was bought for."""
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.run import Run

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="resynth",
        clip_title="No Propagate", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    run.manifest.canary_kind = "clip"
    run.save()
    (run.paths.root / "canary_verdict.json").write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test"})
    )
    result = CliRunner().invoke(
        app, ["batch", str(run.paths.root), "--runs-dir", str(tmp_path / "runs"),
              "--propagate"],
    )
    assert result.exit_code != 0
    assert "Track A only" in result.output


def test_batch_propagate_produces_every_frame_and_states_the_saving(
    test_clip: Path, tmp_path: Path
):
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.pipeline.extract import count_frames
    from claypipe.run import Run

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="surface",
        clip_title="Propagate", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    (run.paths.root / "canary_verdict.json").write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test"})
    )
    result = CliRunner().invoke(
        app, ["batch", str(run.paths.root), "--runs-dir", str(tmp_path / "runs"),
              "--propagate"],
    )
    assert result.exit_code == 0, result.output
    assert "propagation:" in result.output
    assert "paid of" in result.output
    # Every frame exists, so assembly's frame-count invariant still holds.
    assert count_frames(run.paths.restyled_frames) == count_frames(run.paths.source_frames)
    assert run.paths.keyframe_plan.is_file()

    plan = KeyframePlan.read(run.paths.keyframe_plan)
    assert plan.paid_frames < plan.total_frames
    assert plan.paid_frames >= 1


def test_the_projection_is_logged_before_the_spend(test_clip: Path, tmp_path: Path):
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.run import Run

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="surface",
        clip_title="Projection", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    (run.paths.root / "canary_verdict.json").write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test"})
    )
    CliRunner().invoke(
        app, ["batch", str(run.paths.root), "--runs-dir", str(tmp_path / "runs"),
              "--propagate"],
    )
    logged = [json.loads(line) for line in run.paths.log.read_text().splitlines()]
    events = [e["event"] for e in logged]
    projection = events.index("propagate.projection")
    # Nothing was authorised before the projection was reported.
    spends = [i for i, e in enumerate(events) if e == "spend.authorized"]
    assert not spends or min(spends) > projection

    entry = logged[projection]
    assert entry["saved_usd"] == pytest.approx(
        entry["without_propagation_usd"] - entry["projected_usd"]
    )
