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


# ==========================================================================
# T18a — the independent drift comparator.
#
# WHY TF CANNOT DO THIS JOB: a propagated frame IS a warp of its predecessor
# along the optical flow, and temporal_fidelity scores a frame by warping its
# predecessor along the optical flow and differencing. The metric and the
# generation method are THE SAME OPERATION, so TF on a propagated frame is
# near-circular — it measures its own assumption and scores well precisely
# because the frame was made by the process doing the grading.
#
# Until this comparator existed, propagation had NO drift check at all, while
# already being wired into `batch`.
# ==========================================================================

from claypipe.pipeline.propagate import (  # noqa: E402
    DEFAULT_DRIFT_SAMPLE,
    DriftScore,
    FramePlan,
    _stratified_sample,
    drift_summary,
    read_drift_scores,
    score_drift,
    write_drift_scores,
)


def test_tf_and_propagation_are_the_same_operation(panning_frames, temporal, tmp_path):
    """The circularity, demonstrated rather than asserted.

    A frame produced by warping along the flow, then scored by warping along
    the flow, lands near the ceiling — regardless of how far it has drifted
    from its source. The drift comparator exists because of this.
    """
    from claypipe.pipeline.restyle import DummyBackend
    from claypipe.pipeline.score import load_image, structural_similarity_score, temporal_fidelity

    out = tmp_path / "out"
    out.mkdir()
    first = out / panning_frames[0].name
    DummyBackend().restyle(panning_frames[0], first, prompt="clay", strength=0.65, seed=1000)

    # Chain ten warps forward.
    previous = first
    for i in range(1, 11):
        dst = propagate_frame(
            source_previous=panning_frames[i - 1], source_current=panning_frames[i],
            restyled_previous=previous, dst=out / panning_frames[i].name, cfg=temporal,
        )
        previous = dst

    tf = temporal_fidelity(
        load_image(out / panning_frames[9].name),
        load_image(out / panning_frames[10].name),
        temporal,
    )
    source_ssim = structural_similarity_score(
        load_image(panning_frames[10]), load_image(out / panning_frames[10].name)
    )
    # TF is near-perfect by construction. The source-referenced number is the
    # one that can disagree with it, which is the entire point of T18a.
    assert tf > 0.97, tf
    assert source_ssim < tf, (
        f"TF {tf:.4f} vs source-referenced SSIM {source_ssim:.4f} — if these "
        "cannot diverge, the comparator adds nothing"
    )


def test_the_comparator_never_touches_the_flow_field(monkeypatch, panning_frames, temporal, tmp_path):
    """Structural guarantee: if score_drift called the flow estimator, it would
    inherit the circularity it exists to escape."""
    from claypipe.pipeline import propagate as mod

    called = {"flow": 0}
    original = mod.flow_between

    def tracking(*args, **kwargs):
        called["flow"] += 1
        return original(*args, **kwargs)

    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal, max_chain=4,
    )
    from claypipe.pipeline.restyle import DummyBackend

    backend = DummyBackend()
    execute_plan(
        plan=plan, source_frames=panning_frames, out_dir=tmp_path / "out",
        cfg=temporal,
        restyle_keyframe=lambda i, s, d: backend.restyle(
            s, d, prompt="clay", strength=0.65, seed=1000
        ),
        logger=_QuietLogger(),
    )

    pytest.importorskip("lpips", reason="needs the [scoring] extra")
    from claypipe.pipeline.score import Scorer, models_are_cached

    if not models_are_cached():
        pytest.skip("learned-metric weights are not cached")

    from claypipe.config import load_weights

    monkeypatch.setattr(mod, "flow_between", tracking)
    score_drift(
        plan=plan, source_frames=panning_frames, out_dir=tmp_path / "out",
        scorer=Scorer.build(load_weights()), sample=8,
    )
    assert called["flow"] == 0, "the drift comparator used the flow field"


def test_drift_scores_only_propagated_frames(panning_frames, temporal, tmp_path):
    """A keyframe is a fresh generation; measuring it against its source
    measures the BACKEND, not propagation."""
    pytest.importorskip("lpips", reason="needs the [scoring] extra")
    from claypipe.pipeline.score import Scorer, models_are_cached

    if not models_are_cached():
        pytest.skip("learned-metric weights are not cached")

    from claypipe.config import load_weights
    from claypipe.pipeline.restyle import DummyBackend

    plan = plan_keyframes(
        source_frames=panning_frames,
        shot_plan=single_shot_plan(len(panning_frames), 12),
        cfg=temporal, max_chain=4,
    )
    backend = DummyBackend()
    execute_plan(
        plan=plan, source_frames=panning_frames, out_dir=tmp_path / "out",
        cfg=temporal,
        restyle_keyframe=lambda i, s, d: backend.restyle(
            s, d, prompt="clay", strength=0.65, seed=1000
        ),
        logger=_QuietLogger(),
    )
    scores = score_drift(
        plan=plan, source_frames=panning_frames, out_dir=tmp_path / "out",
        scorer=Scorer.build(load_weights()), sample=99,
    )
    keyframes = set(plan.keyframes)
    assert scores
    for score in scores:
        assert score.index not in keyframes
        assert score.chain_length >= 1


def test_the_sample_is_stratified_by_chain_depth():
    """The question the sample must answer is 'does drift grow with depth'. A
    uniform random sample would under-represent the deep chains that are the
    entire risk."""
    frames = []
    for index in range(1, 201):
        depth = 0 if index % 20 == 1 else (index % 20)
        frames.append(
            FramePlan(
                index=index, shot_index=0, is_keyframe=(depth == 0),
                reason="propagated" if depth else "shot_open", chain_length=depth,
            )
        )
    picked = _stratified_sample(frames, 20)
    depths = {f.chain_length for f in picked}
    # Deep and shallow chains are both represented.
    assert max(depths) >= 15, depths
    assert min(depths) <= 3, depths
    assert len(picked) <= 20


def test_a_small_run_is_sampled_exhaustively():
    frames = [
        FramePlan(index=i, shot_index=0, is_keyframe=False, reason="propagated",
                  chain_length=i)
        for i in range(1, 6)
    ]
    assert len(_stratified_sample(frames, 48)) == 5


def test_drift_summary_reports_the_depth_curve():
    scores = [
        DriftScore(frame=f"f_{i:05d}.png", index=i, chain_length=i,
                   ssim=0.9 - i * 0.01, lpips_edges=0.1 + i * 0.01)
        for i in range(1, 13)
    ]
    summary = drift_summary(scores)
    assert summary["drift_sampled"] == 12
    assert summary["max_depth_sampled"] == 12
    assert "by_depth" in summary and len(summary["by_depth"]) >= 3
    # The curve must be readable straight off the summary.
    buckets = list(summary["by_depth"].values())
    assert buckets[0]["ssim_mean"] > buckets[-1]["ssim_mean"]


def test_drift_scores_round_trip(tmp_path: Path):
    scores = [
        DriftScore(frame="f_00005.png", index=5, chain_length=4, ssim=0.81,
                   lpips_edges=0.22)
    ]
    path = write_drift_scores(scores, tmp_path / "drift.jsonl")
    again = read_drift_scores(path)
    assert again[0].as_dict() == scores[0].as_dict()


def test_no_drift_file_reads_as_none_not_as_zero():
    """'This run did not propagate' and 'drift measured at zero' are different
    statements. An unmeasured run must never read as a clean one."""
    from claypipe.pipeline.qccard import summarise_drift

    assert summarise_drift(Path("/nonexistent/drift.jsonl")) is None


def test_the_qc_card_reports_drift_beside_f_not_inside_it(
    panning_frames, temporal, tmp_path
):
    from claypipe.pipeline.qccard import summarise_drift

    scores = [
        DriftScore(frame="f_00002.png", index=2, chain_length=1, ssim=0.8,
                   lpips_edges=0.3)
    ]
    path = write_drift_scores(scores, tmp_path / "drift.jsonl")
    summary = summarise_drift(path)
    assert summary is not None
    # It states, on the artefact itself, that it is not a gate.
    assert "NOT part of" in summary["note"]
    assert "T16" in summary["note"]


def test_the_flag_page_explains_why_tf_cannot_do_this():
    """A reviewer looking at a flagged frame from a propagated run needs to know
    its TF is near-circular."""
    from claypipe.verdi.flag_page import drift_section_html

    html = drift_section_html(
        {
            "drift_sampled": 60, "ssim_mean": 0.34, "ssim_min": 0.03,
            "lpips_edges_mean": 0.45, "lpips_edges_max": 0.67,
            "max_depth_sampled": 12,
            "by_depth": {"depth_0_3": {"n": 15, "ssim_mean": 0.37, "lpips_edges_mean": 0.47}},
        }
    )
    assert "Not part of F" in html
    assert "same operation" in html
    assert "T16" in html
    # And the no-propagation case says so rather than showing zeros.
    empty = drift_section_html(None)
    assert "did not use keyframe propagation" in empty
    assert "Not the same as drift measured at zero" in empty


def test_max_chain_default_is_unchanged_by_t18a():
    """The brief is explicit: do not move it on dummy-backend evidence."""
    assert DEFAULT_MAX_CHAIN == 12


def test_propagate_warns_when_the_source_has_burned_in_text(test_clip: Path, tmp_path: Path):
    """MEASURED on a real 59s sample: propagation carries burned-in text
    forward from each keyframe, so the restyled panel shows captions from the
    wrong moment — 61.1% of propagated frames on the Young Sheldon clip.

    Neither metric catches it. TF is circular on a warped frame, and T18a's
    whole-frame SSIM averages the defect away because the caption band is only
    ~7% of the frame area. So the operator has to be told.
    """
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.run import Run

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="surface",
        clip_title="Ghosting", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    run.manifest.burned_in_text = {
        "detected": True, "kind": "captions", "band_top": 297,
        "band_bottom": 342, "frame_height": 640,
    }
    run.save()
    (run.paths.root / "canary_verdict.json").write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test"})
    )
    result = CliRunner().invoke(
        app, ["batch", str(run.paths.root), "--runs-dir", str(tmp_path / "runs"),
              "--propagate"],
    )
    assert result.exit_code == 0, result.output

    logged = [json.loads(line) for line in run.paths.log.read_text().splitlines()]
    warning = next(e for e in logged if e["event"] == "batch.propagate.burned_in_text")
    assert warning["level"] == "WARN"
    assert "wrong moment" in warning["consequence"]
    # It must name why the existing metrics do not cover it, or the operator
    # will assume the drift score already checked.
    assert "circular" in warning["consequence"]
    assert "--propagate" in warning["fix"]


def test_propagate_is_silent_when_the_source_is_clean(test_clip: Path, tmp_path: Path):
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.run import Run

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="surface",
        clip_title="Clean", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    run.manifest.burned_in_text = {"detected": False, "kind": "none"}
    run.save()
    (run.paths.root / "canary_verdict.json").write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test"})
    )
    CliRunner().invoke(
        app, ["batch", str(run.paths.root), "--runs-dir", str(tmp_path / "runs"),
              "--propagate"],
    )
    logged = [json.loads(line) for line in run.paths.log.read_text().splitlines()]
    assert not any(e["event"] == "batch.propagate.burned_in_text" for e in logged)
