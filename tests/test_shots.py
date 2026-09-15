"""T11 acceptance — shot detection (MASTER_PLAN F2/§1.3).

Three problems collapse into this module, and each gets its own test here:
per-shot seeding, boundary-aware temporal scoring, and the canary's
most-motion slot (D30). Plus the coverage invariant, which is the one that
would otherwise blow up hundreds of frames into a paid batch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claypipe import ffmpeg
from claypipe.pipeline.shots import (
    DEFAULT_THRESHOLD,
    SEED_BASE,
    Shot,
    ShotDetectionError,
    ShotPlan,
    detect_shots,
    single_shot_plan,
)

scenedetect = pytest.importorskip("scenedetect", reason="needs the [shots] extra")


@pytest.fixture(scope="module")
def three_cut_clip(tmp_path_factory) -> Path:
    """A 4-second clip made of four visually unmistakable 1-second blocks.

    Hard cuts between saturated flat colours: if a content detector cannot find
    these three, it cannot find anything.
    """
    out = tmp_path_factory.mktemp("shots") / "three_cuts.mp4"
    parts = []
    base = out.parent
    for i, colour in enumerate(("red", "green", "blue", "white")):
        part = base / f"p{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y",
             "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=24:d=1",
             "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(part)],
            check=True, capture_output=True,
        )
        parts.append(part)
    listing = base / "list.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(out)],
        check=True, capture_output=True,
    )
    return out


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_three_cuts_become_four_shots_with_four_seeds(three_cut_clip: Path):
    """The acceptance criterion: a 3-cut clip -> 4 shots, 4 distinct seeds."""
    plan = detect_shots(video=three_cut_clip, fps=12, total_frames=48)
    assert plan.cuts == 3
    assert len(plan.shots) == 4
    seeds = [s.seed for s in plan.shots]
    assert seeds == [SEED_BASE, SEED_BASE + 1, SEED_BASE + 2, SEED_BASE + 3]
    assert len(set(seeds)) == 4


def test_detection_maps_to_extracted_frame_numbering(three_cut_clip: Path):
    """The source runs at 24fps and extraction at 12, so a boundary at 1.0s is
    extracted frame 13, not 25. Conflating the two clocks puts every boundary
    in the wrong place, silently — this is the test that catches it."""
    plan = detect_shots(video=three_cut_clip, fps=12, total_frames=48)
    assert plan.source_fps == pytest.approx(24.0)
    assert plan.shots[0].start_frame == 1
    assert plan.shots[-1].end_frame == 48
    # One second per block at 12fps = 12 frames per shot.
    for shot in plan.shots:
        assert shot.frame_count == pytest.approx(12, abs=2)


def test_plan_covers_every_frame_exactly_once(three_cut_clip: Path):
    """No seam, no overlap. A seam is a KeyError deep inside a paid batch."""
    plan = detect_shots(video=three_cut_clip, fps=12, total_frames=48)
    seen = []
    for idx in range(1, 49):
        seen.append(plan.shot_for_frame(idx).index)
    assert len(seen) == 48
    # Shot indices must be non-decreasing and contiguous from 0.
    assert seen == sorted(seen)
    assert set(seen) == set(range(len(plan.shots)))


def test_frame_outside_the_plan_is_a_hard_error(three_cut_clip: Path):
    plan = detect_shots(video=three_cut_clip, fps=12, total_frames=48)
    with pytest.raises(ShotDetectionError, match="outside every shot"):
        plan.shot_for_frame(999)


def test_a_clip_with_no_cuts_is_one_shot_not_zero(tmp_path: Path):
    flat = tmp_path / "flat.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=24:d=2",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(flat)],
        check=True, capture_output=True,
    )
    plan = detect_shots(video=flat, fps=12, total_frames=24)
    assert len(plan.shots) == 1
    assert plan.cuts == 0
    assert plan.boundary_frames() == set()


def test_missing_source_is_a_hard_error(tmp_path: Path):
    with pytest.raises(ShotDetectionError, match="not found"):
        detect_shots(video=tmp_path / "nope.mp4", fps=12, total_frames=10)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def test_seed_is_fixed_within_a_shot_and_changes_across_a_cut(three_cut_clip: Path):
    """What 'per-shot fixed seed' actually means. It has been per-CLIP since
    step 1, which is the thing T11 fixes."""
    plan = detect_shots(video=three_cut_clip, fps=12, total_frames=48)
    for shot in plan.shots:
        seeds = {plan.seed_for_frame(i) for i in range(shot.start_frame, shot.end_frame + 1)}
        assert seeds == {shot.seed}, f"shot {shot.index} is not seed-stable"
    for earlier, later in zip(plan.shots, plan.shots[1:]):
        assert plan.seed_for_frame(earlier.end_frame) != plan.seed_for_frame(later.start_frame)


# ---------------------------------------------------------------------------
# Boundaries — the D15/D34 fix
# ---------------------------------------------------------------------------

def test_boundary_frames_are_the_shot_openers_except_the_first(three_cut_clip: Path):
    plan = detect_shots(video=three_cut_clip, fps=12, total_frames=48)
    expected = {s.start_frame for s in plan.shots[1:]}
    assert plan.boundary_frames() == expected
    # Frame 1 opens the clip, not a cut: it is already handled as a first frame.
    assert 1 not in plan.boundary_frames()
    assert len(plan.boundary_frames()) == plan.cuts


def test_boundaries_are_skipped_by_temporal_scoring():
    """A hard cut looks exactly like catastrophic temporal failure to a
    flow-warped residual. restyle_frames must hand the scorer None there, so
    the frame is scored as the first frame it effectively is."""
    from claypipe.pipeline import restyle as restyle_mod

    plan = ShotPlan(
        fps=12, total_frames=6,
        shots=[
            Shot(index=0, start_frame=1, end_frame=3, start_s=0.0, end_s=0.25),
            Shot(index=1, start_frame=4, end_frame=6, start_s=0.25, end_s=0.5),
        ],
    )
    assert plan.boundary_frames() == {4}

    seen: list[tuple[str, bool]] = []

    class FakeScore:
        f = 0.9
        class verdict:  # noqa: D106
            value = "pass"
        class reason:  # noqa: D106
            value = "ok"
        def to_dict(self):
            return {"frame": "x"}

    class FakeScorer:
        def score_frame(self, *, frame, source, restyled, references, previous_restyled):
            seen.append((frame, previous_restyled is None))
            return FakeScore()

    class FakeController:
        def observe(self, score):
            pass
        def decide(self, score, *, base_seed):
            class D:
                is_retry = False
                action = type("A", (), {"value": "accept"})
                strength = None
                seed = None
            return D()

    import numpy as np
    from PIL import Image

    # Monkeypatch load_image so no real decoding is needed.
    original = None
    try:
        from claypipe.pipeline import score as score_mod
        original = score_mod.load_image
        score_mod.load_image = lambda p: np.zeros((8, 8, 3), dtype=np.uint8)
    except Exception:  # pragma: no cover
        pytest.skip("scoring extra unavailable")

    try:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            src_dir.mkdir()
            for i in range(1, 7):
                Image.new("RGB", (8, 8), (i * 10, 0, 0)).save(src_dir / f"f_{i:05d}.png")
            restyle_mod.restyle_frames(
                backend=restyle_mod.DummyBackend(),
                source_dir=src_dir,
                out_dir=Path(tmp) / "out",
                prompt="p",
                strength=0.5,
                logger=_QuietLogger(),
                seed_for=plan.seed_for_frame,
                boundary_frames=plan.boundary_frames(),
                scorer=FakeScorer(),
                controller=FakeController(),
            )
    finally:
        if original is not None:
            score_mod.load_image = original

    by_frame = dict(seen)
    assert by_frame["f_00001.png"] is True, "frame 1 has no predecessor"
    assert by_frame["f_00002.png"] is False, "mid-shot frames measure drift"
    assert by_frame["f_00003.png"] is False
    assert by_frame["f_00004.png"] is True, "frame 4 opens a shot — no drift measured"
    assert by_frame["f_00005.png"] is False, "back to measuring inside shot 1"


class _QuietLogger:
    def info(self, event, **fields):
        pass
    def warn(self, event, **fields):
        pass
    def error(self, event, **fields):
        pass


# ---------------------------------------------------------------------------
# Motion ranking — D30
# ---------------------------------------------------------------------------

def test_most_motion_shot_ranks_by_measured_motion():
    plan = ShotPlan(
        fps=12, total_frames=30,
        shots=[
            Shot(index=0, start_frame=1, end_frame=10, start_s=0, end_s=0.83, motion=0.5),
            Shot(index=1, start_frame=11, end_frame=20, start_s=0.83, end_s=1.67, motion=9.1),
            Shot(index=2, start_frame=21, end_frame=30, start_s=1.67, end_s=2.5, motion=2.0),
        ],
    )
    assert plan.most_motion_shot().index == 1
    # The middle of the busiest shot, not its first frame — a cut's opening
    # frame is often the calm instant before the action it was cut to.
    assert plan.most_motion_frame() == 16  # midpoint of frames 11..20


def test_motion_is_measured_when_frames_are_available(three_cut_clip: Path, tmp_path: Path):
    """Flat colour blocks have near-zero intra-shot motion; the ranking must
    still produce a defined answer rather than crashing."""
    frames = tmp_path / "frames"
    frames.mkdir()
    tools = ffmpeg.require_ffmpeg()
    subprocess.run(
        [tools.ffmpeg, "-v", "error", "-y", "-i", str(three_cut_clip),
         "-vf", "fps=12", str(frames / "f_%05d.png")],
        check=True, capture_output=True,
    )
    plan = detect_shots(
        video=three_cut_clip, fps=12, total_frames=len(list(frames.glob("f_*.png"))),
        frames_dir=frames,
    )
    assert all(s.motion >= 0.0 for s in plan.shots)
    assert plan.most_motion_shot() in plan.shots
    assert 1 <= plan.most_motion_frame() <= plan.total_frames


# ---------------------------------------------------------------------------
# Persistence and the explicit single-shot escape hatch
# ---------------------------------------------------------------------------

def test_plan_round_trips_through_json(three_cut_clip: Path, tmp_path: Path):
    plan = detect_shots(video=three_cut_clip, fps=12, total_frames=48)
    path = plan.write(tmp_path / "shots.json")
    again = ShotPlan.read(path)
    assert [s.as_dict() for s in again.shots] == [s.as_dict() for s in plan.shots]
    assert again.fps == plan.fps
    assert again.total_frames == plan.total_frames
    assert again.seed_for_frame(1) == plan.seed_for_frame(1)


def test_single_shot_plan_reproduces_pre_t11_behaviour():
    plan = single_shot_plan(60, 12)
    assert len(plan.shots) == 1
    assert plan.boundary_frames() == set()
    assert {plan.seed_for_frame(i) for i in range(1, 61)} == {SEED_BASE}
    assert plan.detector == "single"


def test_single_shot_plan_refuses_an_empty_clip():
    with pytest.raises(ShotDetectionError, match="at least one frame"):
        single_shot_plan(0, 12)


def test_summary_reports_what_the_operator_needs(three_cut_clip: Path):
    """Rule 40: state, not implication."""
    plan = detect_shots(video=three_cut_clip, fps=12, total_frames=48)
    s = plan.summary()
    for key in ("shots", "cuts", "detector", "threshold", "fps", "source_fps",
                "avg_shot_s", "median_shot_s", "shortest_shot_s", "shots_per_60s"):
        assert key in s, key
    assert s["detector"] == "content"
    assert s["threshold"] == DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# Detector calibration against the real reference clips.
#
# The repo deliberately bundles no real footage (see conftest), so these skip
# unless the operator points the env vars at the two @trevorcarlee clips. The
# measurement they reproduce is recorded in MASTER_PLAN §1.3 and memory.md.
# ---------------------------------------------------------------------------

import os

REFERENCE_CLIPS = {
    # env var -> (expected cuts, tolerance, total extracted frames at 12fps)
    "CLAYPIPE_REFERENCE_CLIP_A": (41, 4, 1047),
    "CLAYPIPE_REFERENCE_CLIP_B": (21, 4, 747),
}


@pytest.mark.parametrize("env_var,expected", sorted(REFERENCE_CLIPS.items()))
def test_detector_reproduces_the_reference_cut_count(env_var, expected):
    """T11's acceptance criterion: 41+/-4 cuts on clip A.

    Clip B is recorded at 21 because that is what ContentDetector finds at the
    default threshold. An independent coarse mean-delta pass over the same clip
    found 14, and the two detectors genuinely disagree — clip B is dark,
    high-contrast action where a coarse luma threshold under-counts cuts
    between similar-looking shots. Clip A, where both agree exactly on 41, is
    the clip the shots-per-60s budget is set against.
    """
    path = os.environ.get(env_var)
    if not path or not Path(path).is_file():
        pytest.skip(f"set {env_var} to a reference clip to run this calibration")
    cuts, tolerance, total_frames = expected
    plan = detect_shots(video=Path(path), fps=12, total_frames=total_frames)
    assert abs(plan.cuts - cuts) <= tolerance, (
        f"{env_var}: detector found {plan.cuts} cuts, expected {cuts}+/-{tolerance}. "
        "Either the clip changed or PySceneDetect's default threshold moved."
    )


def test_canary_slots_never_collapse_on_a_single_shot_clip():
    """Regression: on a clip with one shot, the most-motion frame IS the middle
    frame, and the selection silently returned two frames for a three-frame
    canary — so the operator would approve less than they were quoted for."""
    from claypipe import cli

    plan = single_shot_plan(60, 12)
    frames = [Path(f"f_{i:05d}.png") for i in range(1, 61)]
    chosen, _caption = cli._canary_frame_names(frames, plan)
    assert len(chosen) == 3
    assert len({p.name for p in chosen}) == 3, [p.name for p in chosen]


def test_canary_slots_are_distinct_for_a_multi_shot_clip(three_cut_clip: Path):
    from claypipe import cli

    plan = detect_shots(video=three_cut_clip, fps=12, total_frames=48)
    frames = [Path(f"f_{i:05d}.png") for i in range(1, 49)]
    chosen, caption = cli._canary_frame_names(frames, plan)
    assert len({p.name for p in chosen}) == 3
    assert "most-motion shot" in caption
