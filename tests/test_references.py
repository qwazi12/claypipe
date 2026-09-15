"""T10 acceptance — identity references come from the approved canary (F1).

This is the finding that would have broken the first paid run. `intake --ref`
locks operator-supplied stills, which are photoreal. Scoring a clay restyle
against a photograph asks the wrong question, CLIP cosine answers it correctly
(low), id_min 0.85 misses on nearly every frame, and the 15% retry budget
halts the run around frame 107 — at which point the backend gets blamed.

Measured on a real 20s clip (see memory.md D41):
    restyled vs CANARY refs : 0.839 - 0.958
    restyled vs SOURCE refs : 0.535 - 0.655
with targets.id_min = 0.85. The DIRECTION and MAGNITUDE are the point; the
absolute numbers are from the dummy backend and are not calibration (F4/T16).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claypipe import ffmpeg
from claypipe.cli import app
from claypipe.config import load_styles
from claypipe.pipeline.extract import extract_audio, extract_frames, frame_paths
from claypipe.pipeline.restyle import DummyBackend, restyle_frames
from claypipe.run import Run

runner = CliRunner()


@pytest.fixture
def batched_run(test_clip: Path, tmp_path: Path) -> Run:
    """A run with restyled frames on disk and no canary decision yet."""
    styles = load_styles()
    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy",
        clip_title="Ref Test", duration_s=ffmpeg.duration_seconds(test_clip),
        source_width=1280, source_height=720,
        styles=styles, runs_dir=tmp_path / "runs", echo=False,
    )
    profile = styles.profile("clay")
    extract_frames(test_clip, run.paths.source_frames, 12, run.logger)
    extract_audio(test_clip, run.paths.audio, run.logger)
    restyle_frames(
        backend=DummyBackend(), source_dir=run.paths.source_frames,
        out_dir=run.paths.restyled_frames, prompt=profile.prompt,
        strength=profile.strength, logger=run.logger,
    )
    return run


def _submit(run: Run, query: str):
    return runner.invoke(
        app, ["canary", "submit", str(run.paths.root), "--url", query,
              "--runs-dir", str(run.paths.root.parent)],
    )


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def test_approval_locks_the_canary_frames_as_references(batched_run: Run):
    result = _submit(batched_run, "approved=true")
    assert result.exit_code == 0, result.output

    reloaded = Run.load(batched_run.paths.root, echo=False)
    assert reloaded.manifest.reference_origin == "canary"
    refs = [Path(p) for p in reloaded.manifest.reference_images]
    assert len(refs) == 3
    for ref in refs:
        assert ref.is_file()
        # Locked INSIDE the run, not pointing at an operator file that can move.
        assert reloaded.paths.refs.resolve() in ref.resolve().parents

    # The locked bytes are the approved frames' bytes, not a re-render.
    restyled = {p.name: p.read_bytes() for p in frame_paths(batched_run.paths.restyled_frames)}
    for ref in refs:
        assert any(ref.read_bytes() == data for data in restyled.values())


def test_rejection_locks_nothing(batched_run: Run):
    result = _submit(batched_run, "approved=false&reason=the+clay+looks+like+plastic")
    assert result.exit_code == 0, result.output
    reloaded = Run.load(batched_run.paths.root, echo=False)
    assert reloaded.manifest.reference_images == []
    assert reloaded.manifest.reference_origin == "intake"
    assert not list(reloaded.paths.refs.glob("ref_*.png"))


def test_adjust_locks_nothing_because_the_look_is_about_to_change(batched_run: Run):
    """An 'adjust' verdict means the prompt changes, so the frames on disk are
    not the target. Locking them would lock the wrong reference."""
    result = _submit(batched_run, "approved=adjust&prompt_override=more+fingerprints")
    assert result.exit_code == 0, result.output
    reloaded = Run.load(batched_run.paths.root, echo=False)
    assert reloaded.manifest.reference_images == []
    assert reloaded.manifest.reference_origin == "intake"


def test_individually_rejected_frames_are_excluded(batched_run: Run):
    """Approving overall while flagging one frame means the OTHER frames are
    the reference — not all three."""
    chosen = frame_paths(batched_run.paths.restyled_frames)
    middle = chosen[len(chosen) // 2].name
    result = _submit(batched_run, f"approved=true&frame:{middle}:verdict=reject")
    assert result.exit_code == 0, result.output

    reloaded = Run.load(batched_run.paths.root, echo=False)
    refs = [Path(p) for p in reloaded.manifest.reference_images]
    assert len(refs) == 2, f"expected the rejected frame excluded, got {[r.name for r in refs]}"
    assert not any(middle in r.name for r in refs)


def test_resubmitting_replaces_references_rather_than_accumulating(batched_run: Run):
    """A superseded look must not be averaged into the ID target."""
    assert _submit(batched_run, "approved=true").exit_code == 0
    first = set(p.name for p in batched_run.paths.refs.glob("ref_*.png"))
    assert len(first) == 3

    chosen = frame_paths(batched_run.paths.restyled_frames)
    middle = chosen[len(chosen) // 2].name
    assert _submit(batched_run, f"approved=true&frame:{middle}:verdict=reject").exit_code == 0
    second = set(p.name for p in batched_run.paths.refs.glob("ref_*.png"))
    assert len(second) == 2, f"stale references survived: {second}"


def test_all_frames_rejected_is_a_refusal_not_an_empty_lock(batched_run: Run):
    chosen = frame_paths(batched_run.paths.restyled_frames)
    plan_names = [chosen[0].name, chosen[len(chosen) // 2].name, chosen[-1].name]
    query = "approved=true&" + "&".join(f"frame:{n}:verdict=reject" for n in plan_names)
    result = _submit(batched_run, query)
    assert result.exit_code != 0
    assert "nothing to lock" in result.output


# ---------------------------------------------------------------------------
# The F1 warning
# ---------------------------------------------------------------------------

def test_intake_origin_references_warn_at_startup(batched_run: Run, tmp_path: Path):
    """F1: warn, don't silently accept. It must be a stated choice, not a
    default discovered when the retry budget halts the run."""
    from claypipe import cli

    src = frame_paths(batched_run.paths.source_frames)[0]
    batched_run.manifest.reference_images = [str(src.resolve())]
    batched_run.manifest.reference_origin = "intake"
    batched_run.save()

    cli._warn_on_source_origin_references(
        batched_run, [Path(p) for p in batched_run.manifest.reference_images]
    )
    logged = [json.loads(line) for line in batched_run.paths.log.read_text().splitlines()]
    warnings = [e for e in logged if e["event"] == "batch.references.photoreal_risk"]
    assert warnings, "no F1 warning was logged"
    entry = warnings[-1]
    assert entry["level"] == "WARN"
    assert entry["origin"] == "intake"
    # The warning must name the consequence AND the fix, not just complain.
    assert "id_min" in entry["consequence"]
    assert "canary" in entry["fix"]
    # A reference that IS one of this run's source frames is called out by name.
    assert entry["are_source_frames"] == [str(src.resolve())]


def test_canary_origin_references_do_not_warn(batched_run: Run):
    from claypipe import cli

    assert _submit(batched_run, "approved=true").exit_code == 0
    reloaded = Run.load(batched_run.paths.root, echo=False)
    before = len(reloaded.paths.log.read_text().splitlines())
    cli._warn_on_source_origin_references(
        reloaded, [Path(p) for p in reloaded.manifest.reference_images]
    )
    after = reloaded.paths.log.read_text().splitlines()
    assert len(after) == before, "canary-origin references must be silent"


# ---------------------------------------------------------------------------
# The measurement that makes T10 worth doing
# ---------------------------------------------------------------------------

def test_canary_references_score_far_higher_than_source_references(tmp_path: Path):
    """The empirical core of F1, measured on REAL footage.

    This test deliberately does NOT use the synthetic test clip. On `testsrc`
    colour bars the dummy posterise barely moves a CLIP embedding, so the
    canary-vs-source gap collapses to 0.001 and the fixture "proves" F1 is a
    non-issue. On real footage the same measurement gives a gap of 0.25-0.35.

    That contrast IS finding F4: thresholds and premises calibrated on
    deterministic synthetic fixtures do not transfer to generative output on
    real frames. Recorded here rather than worked around.

    Asserts the RELATIONSHIP, not a threshold — the dummy backend is not a real
    clay restyle, so the absolute numbers are not calibration (T16 owns that).
    """
    import os

    clip = os.environ.get("CLAYPIPE_REFERENCE_CLIP_A")
    if not clip or not Path(clip).is_file():
        pytest.skip(
            "set CLAYPIPE_REFERENCE_CLIP_A to real footage. The synthetic clip "
            "cannot demonstrate F1 — see this test's docstring (finding F4)."
        )
    pytest.importorskip("open_clip", reason="needs the [scoring] extra")
    from claypipe.pipeline.score import models_are_cached

    if not models_are_cached():
        pytest.skip("learned-metric weights are not cached; never downloads in tests")

    from claypipe.config import load_weights
    from claypipe.pipeline.score import OpenClipEmbedder, identity_similarity, load_image

    styles = load_styles()
    source_clip = Path(clip)
    stream = ffmpeg.stream(source_clip, "video")
    run = Run.create(
        source=source_clip, style="clay", fps=12, backend="dummy",
        clip_title="F1 measurement", duration_s=ffmpeg.duration_seconds(source_clip),
        source_width=int(stream["width"]), source_height=int(stream["height"]),
        styles=styles, runs_dir=tmp_path / "runs", echo=False,
    )
    profile = styles.profile("clay")
    extract_frames(source_clip, run.paths.source_frames, 12, run.logger)
    restyle_frames(
        backend=DummyBackend(), source_dir=run.paths.source_frames,
        out_dir=run.paths.restyled_frames, prompt=profile.prompt,
        strength=profile.strength, logger=run.logger,
    )

    weights = load_weights()
    embedder = OpenClipEmbedder(
        arch=weights.models.identity_model, pretrained=weights.models.identity_pretrained
    )
    source = frame_paths(run.paths.source_frames)
    restyled = frame_paths(run.paths.restyled_frames)
    slots = [0, len(restyled) // 2, len(restyled) - 1]

    canary_refs = [load_image(restyled[i]) for i in slots]
    source_refs = [load_image(source[i]) for i in slots]

    gaps = []
    for probe in (len(restyled) // 4, len(restyled) // 2 + 3, len(restyled) - 5):
        frame = load_image(restyled[probe])
        vs_canary = identity_similarity(frame, canary_refs, embedder)
        vs_source = identity_similarity(frame, source_refs, embedder)
        assert vs_canary > vs_source, (
            f"frame {probe}: canary refs scored {vs_canary:.4f}, source refs "
            f"{vs_source:.4f} — F1's premise does not hold on this footage"
        )
        gaps.append(vs_canary - vs_source)
        # Source-referenced identity must land BELOW the shipped target; that
        # is the whole failure mode — near-universal id_min misses.
        assert vs_source < weights.targets.id_min, (
            f"frame {probe}: source-referenced ID is {vs_source:.4f}, which "
            f"MEETS id_min {weights.targets.id_min}. F1 may no longer apply."
        )
    # A gap, not a nudge. Measured 0.25-0.35 on clip A.
    assert min(gaps) > 0.1, f"gaps were only {[round(g, 4) for g in gaps]}"


def test_d12_a_style_with_no_character_still_gets_references(batched_run: Run):
    """D12 CLOSED: `logo` has no character to photograph, and needed no special
    case — the reference is whatever the canary approved."""
    batched_run.manifest.style = "logo"
    batched_run.save()
    assert _submit(batched_run, "approved=true").exit_code == 0
    reloaded = Run.load(batched_run.paths.root, echo=False)
    assert reloaded.manifest.reference_origin == "canary"
    assert len(reloaded.manifest.reference_images) == 3


# ---------------------------------------------------------------------------
# T10a — `canary restyle`: the stage the gate was always meant to sit behind
# ---------------------------------------------------------------------------

@pytest.fixture
def intaken_run(test_clip: Path, tmp_path: Path) -> Run:
    """A run straight out of intake: no frames, no canary, no references."""
    styles = load_styles()
    return Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy",
        clip_title="Canary Stage", duration_s=ffmpeg.duration_seconds(test_clip),
        source_width=1280, source_height=720,
        styles=styles, runs_dir=tmp_path / "runs", echo=False,
    )


def _canary(run: Run, *args):
    return runner.invoke(
        app, ["canary", *args, str(run.paths.root), "--runs-dir", str(run.paths.root.parent)]
    )


def test_canary_restyle_pays_for_three_frames_not_the_clip(intaken_run: Run):
    """The economics of the gate. On fal Kontext pro this is the difference
    between $0.12 and $28.80."""
    result = _canary(intaken_run, "restyle")
    assert result.exit_code == 0, result.output

    restyled = frame_paths(intaken_run.paths.restyled_frames)
    assert len(restyled) == 3, f"expected 3 canary frames, got {len(restyled)}"
    # Extraction still covers the whole clip — it is free and `batch` needs it.
    assert len(frame_paths(intaken_run.paths.source_frames)) == 60


def test_canary_restyle_is_unscored_because_references_do_not_exist_yet(
    intaken_run: Run,
):
    """A score with no reference is a number with a hole in it. T10's whole
    premise is that THIS stage's approved output becomes the reference."""
    assert _canary(intaken_run, "restyle").exit_code == 0
    logged = [json.loads(line) for line in intaken_run.paths.log.read_text().splitlines()]
    entry = next(e for e in logged if e["event"] == "canary.restyle")
    assert entry["scored"] is False
    assert "references" in entry["reason"]
    assert not intaken_run.paths.scores.is_file()


def test_canary_restyle_uses_the_seed_the_batch_will_use(intaken_run: Run):
    """Otherwise the operator approves a look the batch does not reproduce."""
    assert _canary(intaken_run, "restyle").exit_code == 0
    assert intaken_run.paths.shot_plan.is_file()

    from claypipe.pipeline.shots import ShotPlan

    plan = ShotPlan.read(intaken_run.paths.shot_plan)
    sources = frame_paths(intaken_run.paths.source_frames)
    index_of = {p.name: i for i, p in enumerate(sources, start=1)}

    logged = [json.loads(line) for line in intaken_run.paths.log.read_text().splitlines()]
    frames = [e for e in logged if e["event"] == "canary.restyle.frame"]
    assert frames, "no per-frame records"
    for entry in frames:
        assert entry["seed"] == plan.seed_for_frame(index_of[entry["frame"]])


def test_canary_frames_are_not_paid_for_twice(intaken_run: Run):
    """Resume-safety across the two stages: the 3 frames the canary already
    paid for must be reused by the full batch, not regenerated."""
    assert _canary(intaken_run, "restyle").exit_code == 0
    assert _canary(intaken_run, "submit", "--url", "approved=true").exit_code == 0

    result = runner.invoke(
        app, ["batch", str(intaken_run.paths.root),
              "--runs-dir", str(intaken_run.paths.root.parent)],
    )
    assert result.exit_code == 0, result.output
    logged = [json.loads(line) for line in intaken_run.paths.log.read_text().splitlines()]
    batch_entry = [e for e in logged if e["event"] == "restyle.frames"][-1]
    assert batch_entry["resumed"] == 3, (
        f"the canary's 3 paid frames were not reused: {batch_entry}"
    )
    assert batch_entry["restyled"] == 57
    assert batch_entry["total"] == 60


def test_canary_restyle_refuses_a_paid_backend_without_live(intaken_run: Run):
    """The two independent locks in front of a paid endpoint apply to this
    stage too — it is the stage that spends first."""
    result = runner.invoke(
        app, ["canary", "restyle", str(intaken_run.paths.root),
              "--runs-dir", str(intaken_run.paths.root.parent), "--backend", "fal"],
    )
    assert result.exit_code != 0
    assert "--live" in result.output
