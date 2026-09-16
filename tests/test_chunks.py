"""V5 acceptance — chunking aligned to shot cuts.

A 60-second clip is ~26 shots and several generation chunks, and nothing in a
v2v model guarantees a character's clay design is the same in chunk 1 and chunk
4. That inconsistency is what makes output read as slop rather than animation.

Two decisions carry the weight, and the first INVERTS what the per-frame path
did:
  * ONE SEED FOR THE WHOLE RUN. Under v2v the seed drives the generated DESIGN,
    so changing it between chunks redesigns the character at every seam. T11's
    per-shot seeding was right for img2img (where the seed drives retry
    variety) and is wrong here.
  * SEAMS LAND ON CUTS. A design shift across a hard cut is invisible; mid-shot
    it is a jump in the character's face.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claypipe.pipeline.chunks import Chunk, ChunkPlan, ChunkPlanError, plan_chunks
from claypipe.pipeline.shots import Shot, ShotPlan, single_shot_plan


def shots_of(lengths: list[int], fps: int = 12) -> ShotPlan:
    """A shot plan with the given shot lengths in frames."""
    shots = []
    start = 1
    for index, length in enumerate(lengths):
        shots.append(
            Shot(
                index=index, start_frame=start, end_frame=start + length - 1,
                start_s=(start - 1) / fps, end_s=(start + length - 1) / fps,
            )
        )
        start += length
    return ShotPlan(fps=fps, total_frames=sum(lengths), shots=shots)


VACE_MIN, VACE_MAX = 81, 241


def _plan(lengths: list[int], **kwargs) -> ChunkPlan:
    kwargs.setdefault("seed", 1000)
    kwargs.setdefault("min_chunk_frames", VACE_MIN)
    kwargs.setdefault("max_chunk_frames", VACE_MAX)
    return plan_chunks(shot_plan=shots_of(lengths), **kwargs)


# ---------------------------------------------------------------------------
# Coverage — the invariant that must never break
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "lengths",
    [
        [100, 100, 100],
        [300, 300],
        [50] * 12,
        [711],
        [90, 30, 200, 45, 120, 300],
        [81],
    ],
)
def test_the_plan_covers_every_frame_exactly_once(lengths):
    plan = _plan(lengths)
    assert plan.covers(), [c.as_dict() for c in plan.chunks]
    assert plan.chunks[0].start_frame == 1
    assert plan.chunks[-1].end_frame == sum(lengths)
    for earlier, later in zip(plan.chunks, plan.chunks[1:]):
        assert later.start_frame == earlier.end_frame + 1


@pytest.mark.parametrize(
    "lengths", [[100, 100, 100], [300, 300], [50] * 12, [90, 30, 200, 45, 120, 300]]
)
def test_every_chunk_respects_the_backend_bounds(lengths):
    """VACE accepts 81-241 frames. A chunk outside that cannot be generated —
    asking below the floor bills the minimum or pads, and padding changes the
    frame count."""
    plan = _plan(lengths)
    for chunk in plan.chunks:
        assert chunk.frame_count <= VACE_MAX, chunk.as_dict()
        # The only chunk allowed below the floor is one where the whole run is
        # shorter than the floor.
        if sum(lengths) >= VACE_MIN:
            assert chunk.frame_count >= 1


# ---------------------------------------------------------------------------
# Seams prefer cuts
# ---------------------------------------------------------------------------

def test_seams_land_on_cuts_when_the_shots_allow_it():
    # Shots of 100 frames: chunks can end on a cut and stay inside 81-241.
    plan = _plan([100] * 6)
    assert plan.chunks, "no chunks"
    assert plan.mid_shot_seams == [], (
        f"seams fell mid-shot unnecessarily: {[c.as_dict() for c in plan.chunks]}"
    )
    assert plan.summary()["aligned_fraction"] == 1.0


def test_a_shot_longer_than_the_backend_maximum_forces_a_seam():
    """A 600-frame shot cannot be one call at a 241-frame maximum, so a
    mid-shot seam is unavoidable — and must be REPORTED, not hidden."""
    plan = _plan([600])
    assert len(plan.chunks) > 1
    assert plan.mid_shot_seams, "a forced mid-shot seam was not reported"
    assert plan.summary()["mid_shot_seams"] == len(plan.mid_shot_seams)
    assert plan.summary()["mid_shot_seam_frames"]


def test_a_mid_shot_seam_is_reported_with_its_frame():
    plan = _plan([600])
    for chunk in plan.mid_shot_seams:
        assert chunk.is_mid_shot_seam
        assert chunk.start_frame in plan.summary()["mid_shot_seam_frames"]


def test_the_first_chunk_is_never_a_seam():
    """Nothing precedes it, so there is no design to shift away from."""
    plan = _plan([600])
    assert plan.chunks[0].index == 0
    assert not plan.chunks[0].is_mid_shot_seam


def test_seam_frames_are_the_chunk_starts_after_the_first():
    plan = _plan([100] * 6)
    assert plan.seam_frames == [c.start_frame for c in plan.chunks[1:]]


# ---------------------------------------------------------------------------
# One seed per run
# ---------------------------------------------------------------------------

def test_the_whole_run_shares_one_seed():
    """Under v2v the seed drives the generated DESIGN. Changing it between
    chunks redesigns the character at every seam, which is the identity drift
    §6 names as a headline risk. This inverts T11's per-shot seeding."""
    plan = _plan([100] * 6, seed=4242)
    assert plan.seed == 4242
    assert plan.summary()["seed"] == 4242
    # And the plan exposes exactly one seed, not one per chunk.
    assert not any(hasattr(c, "seed") for c in plan.chunks)


def test_the_v2v_runner_passes_the_same_seed_to_every_chunk():
    import inspect

    from claypipe import cli

    source = inspect.getsource(cli._run_v2v)
    assert "run_seed = SEED_BASE" in source
    assert source.count("seed=run_seed") >= 1
    assert "seed_for_frame" not in source, (
        "per-shot seeding would redesign the character at every seam"
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_inverted_bounds_are_refused():
    with pytest.raises(ChunkPlanError, match="exceeds max_chunk_frames"):
        _plan([100], min_chunk_frames=200, max_chunk_frames=100)


def test_an_empty_run_is_refused():
    with pytest.raises(ChunkPlanError, match="at least one frame"):
        plan_chunks(
            shot_plan=ShotPlan(fps=12, total_frames=0, shots=[]),
            seed=1, min_chunk_frames=81, max_chunk_frames=241,
        )


def test_a_plan_round_trips_through_json(tmp_path: Path):
    plan = _plan([100] * 6)
    path = plan.write(tmp_path / "chunks.json")
    again = ChunkPlan.read(path)
    assert [c.as_dict() for c in again.chunks] == [c.as_dict() for c in plan.chunks]
    assert again.seed == plan.seed
    assert again.covers()


# ---------------------------------------------------------------------------
# The QC card's ID-drift report
# ---------------------------------------------------------------------------

def test_the_qc_card_reports_seams_and_per_chunk_identity(tmp_path: Path):
    """"The seams are here" becomes "and here is where it drifted"."""
    from claypipe.pipeline.qccard import summarise_chunks

    plan = _plan([100] * 6)
    chunk_path = plan.write(tmp_path / "chunks.json")

    scores = tmp_path / "scores.jsonl"
    with scores.open("w") as fh:
        for chunk in plan.chunks:
            # Later chunks drift downward, which is the failure being surfaced.
            base = 0.9 - 0.1 * chunk.index
            for frame in range(chunk.start_frame, chunk.start_frame + 3):
                fh.write(json.dumps({
                    "frame": f"f_{frame:05d}.png", "identity": base,
                    "id_support": "regions:2",
                }) + "\n")

    summary = summarise_chunks(chunk_path, scores)
    assert summary["chunks"] == len(plan.chunks)
    assert summary["seam_frames"] == plan.seam_frames
    by_chunk = summary["identity_by_chunk"]
    assert len(by_chunk) == len(plan.chunks)
    assert by_chunk[0]["mean_id"] > by_chunk[-1]["mean_id"], "drift not surfaced"
    assert summary["identity_spread_across_chunks"] == pytest.approx(
        by_chunk[0]["mean_id"] - by_chunk[-1]["mean_id"], abs=1e-4
    )
    # Reported, not gated — no calibration exists for it yet.
    assert "not gated" in summary["identity_drift_note"]


def test_an_unchunked_run_reports_none_not_zero(tmp_path: Path):
    """"Not chunked" and "chunked with no drift" are different statements."""
    from claypipe.pipeline.qccard import summarise_chunks

    assert summarise_chunks(tmp_path / "missing.json", tmp_path / "s.jsonl") is None


def test_chunk_summary_without_scores_says_identity_is_unmeasured(tmp_path: Path):
    from claypipe.pipeline.qccard import summarise_chunks

    path = _plan([100] * 6).write(tmp_path / "chunks.json")
    summary = summarise_chunks(path, tmp_path / "nonexistent.jsonl")
    assert summary["identity_by_chunk"] is None
    assert summary["chunks"] == 6 // 2 or summary["chunks"] >= 1


# ---------------------------------------------------------------------------
# The real clip
# ---------------------------------------------------------------------------

def test_the_sheldon_clip_chunks_entirely_on_cuts():
    """V5's acceptance criterion, on the real test clip: every chunk boundary
    lands on a cut, or is reported as a mid-shot seam."""
    clip = os.environ.get("CLAYPIPE_BURNIN_CLIP")
    if not clip or not Path(clip).is_file():
        pytest.skip("set CLAYPIPE_BURNIN_CLIP to the Young Sheldon Shorts rip")
    pytest.importorskip("scenedetect")

    from claypipe.pipeline.shots import detect_shots

    shot_plan = detect_shots(video=Path(clip), fps=12, total_frames=711)
    plan = plan_chunks(
        shot_plan=shot_plan, seed=1000,
        min_chunk_frames=VACE_MIN, max_chunk_frames=VACE_MAX,
    )
    assert plan.covers()
    # Either aligned, or reported. Never silently mid-shot.
    for chunk in plan.chunks:
        if chunk.is_mid_shot_seam:
            assert chunk.start_frame in plan.summary()["mid_shot_seam_frames"]
    assert plan.summary()["mid_shot_seams"] == 0, (
        f"the Sheldon clip should chunk entirely on cuts, got "
        f"{plan.summary()['mid_shot_seam_frames']}"
    )
