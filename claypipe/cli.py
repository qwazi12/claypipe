"""ClayPipe CLI (SPEC §5).

Build Order step 1 ships the offline path: intake -> batch -> assemble, plus
status. The `canary`, `review` and `--backend fal` commands land in steps 3-5;
until then `batch` is offline-only and cannot spend money.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from . import __version__, ffmpeg
from .config import ConfigError, load_all
from .pipeline import assemble as assemble_stage
from .pipeline import qccard
from .pipeline.extract import count_frames, extract_audio, extract_frames, frame_paths
from .pipeline import burnin, captions, propagate, shots
from .pipeline.layout import LayoutError, source_aspect_of
from .pipeline.restyle import (
    ChunkLengthError,
    get_backend,
    get_clip_backend,
    restyle_clip_range,
    restyle_frames,
)
from .pipeline.retry import RetryController
from .pipeline.score import Scorer, ScoringError, load_image
from .pipeline.retry import RunHalted, SpendLedger
from .run import Run, resolve_run
from . import snapshot as snapshot_mod
from .verdi import canary_page, flag_page
from .verdi import loaders as verdi_loaders

app = typer.Typer(
    add_completion=False,
    help="ClayPipe — AI recreation comparison video pipeline.",
    no_args_is_help=True,
)


def _startup() -> tuple:
    """Validate config and the ffmpeg toolchain before any work (Rules 5, 20, 25)."""
    try:
        styles, weights = load_all()
    except ConfigError as exc:
        typer.secho(f"CONFIG ERROR: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    try:
        tools = ffmpeg.require_ffmpeg()
    except ffmpeg.FFmpegError as exc:
        typer.secho(f"STARTUP ERROR: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return styles, weights, tools


def _fail(message: str) -> None:
    typer.secho(f"FAILED: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


# The per-frame img2img track, retired by the v2v architecture and kept behind
# this name for one release. It cannot satisfy the format's requirements: a
# per-frame restyle at a strength high enough to read as clay drifts geometry
# shot to shot, and nothing in it restyles the ENVIRONMENT as clay — which is
# the difference between a clay world and a clay figure standing in a real
# room. Whole-frame v2v does both in one pass.
LEGACY_MODE = "surface"

@app.command()
def version() -> None:
    """Print the ClayPipe version and the ffmpeg it will use."""
    _, _, tools = _startup()
    typer.echo(f"claypipe {__version__}")
    typer.echo(tools.version)


@app.command()
def intake(
    video: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    style: str = typer.Option(..., "--style", "-s", help="Style profile from styles.yaml"),
    title: Optional[str] = typer.Option(None, "--title", help="Header bar text (default: filename)"),
    fps: Optional[int] = typer.Option(None, "--fps", help="Extraction fps (default: styles.yaml)"),
    backend: str = typer.Option("dummy", "--backend", help="Restyle backend: dummy | fal"),
    allow_burned_captions: bool = typer.Option(
        False, "--allow-burned-captions",
        help="Acknowledge that the source carries burned-in text and proceed. "
             "Intake never blocks on it either way — this only records that the "
             "operator saw the warning before spending.",
    ),
    mode: str = typer.Option(
        # Still `surface` by DELIBERATE SEQUENCING, not by preference: resynth
        # is the architecture, but its `batch` path lands with V5 (shot-aligned
        # chunking). Defaulting to a mode that cannot complete a batch would
        # leave the CLI broken for the common case between two commits. The
        # flip happens in V5, alongside the path it needs.
        LEGACY_MODE, "--mode",
        help="Track: resynth (whole-frame video-to-video — THE ARCHITECTURE; "
             "its batch path lands with V5) or surface (per-frame img2img, "
             "RETIRED and kept for one release). Decides the target vector AND "
             "the retry policy, so it is fixed at intake.",
    ),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir", help="Override output directory"),
    ref: list[Path] = typer.Option(
        [], "--ref",
        help="Character reference image to lock for identity scoring. Repeatable. "
             "Required for any backend that scores (i.e. anything but dummy).",
    ),
) -> None:
    """STAGE 0 — register a clip and lock its style + character references."""
    styles, weights, _ = _startup()
    try:
        styles.profile(style)
    except ConfigError as exc:
        _fail(str(exc))
    # T13: an unknown mode is a hard failure here, before any I/O — a run
    # created with a mode weights.yaml does not define could never be scored.
    try:
        mode_cfg = weights.mode(mode)
    except ConfigError as exc:
        _fail(str(exc))
    if mode == LEGACY_MODE:
        typer.secho(
            f"WARNING: --mode {LEGACY_MODE} is RETIRED. The architecture is "
            "whole-frame video-to-video (--mode resynth), which is the only "
            "path that restyles every character AND the environment together. "
            "Per-frame img2img is kept for one release and will be removed.",
            fg=typer.colors.YELLOW, err=True,
        )

    try:
        duration = ffmpeg.duration_seconds(video)
        ffmpeg.stream(video, "audio")  # a clip with no audio cannot be assembled
        # T9: the source's pixel dimensions ARE part of run identity — the
        # layout engine derives panel height and margins from this aspect
        # ratio. Probed once here so assembly never has to guess, and so a
        # source whose aspect cannot make a two-panel stack is refused at
        # intake rather than after the frames have been paid for.
        video_stream = ffmpeg.stream(video, "video")
        source_width, source_height = int(video_stream["width"]), int(video_stream["height"])
    except ffmpeg.FFmpegError as exc:
        _fail(str(exc))

    try:
        layout = styles.render.layout_for(source_aspect_of(source_width, source_height))
    except LayoutError as exc:
        _fail(str(exc))

    for image in ref:
        if not image.is_file():
            _fail(f"reference image not found: {image}")

    run = Run.create(
        source=video,
        style=style,
        fps=fps or styles.render.default_fps,
        backend=backend,
        mode=mode,
        clip_title=title or video.stem,
        duration_s=duration,
        source_width=source_width,
        source_height=source_height,
        styles=styles,
        runs_dir=runs_dir,
    )
    if ref:
        run.manifest.reference_images = [str(p.resolve()) for p in ref]
        run.save()
    # T9b: burned-in text is detected ONCE, here, before any spend. A source
    # that already carries subtitles breaks the format three ways — the text
    # appears in the original panel, gets RESTYLED into the comparison panel as
    # clay-textured glyphs, and is then duplicated by T12's own caption track.
    # None of that is visible until the money is gone.
    try:
        burn_in = burnin.detect_burned_in_captions(video)
    except burnin.BurnInError as exc:
        run.logger.warn("intake.burnin.failed", error=str(exc))
        burn_in = None

    if burn_in is not None:
        run.manifest.burned_in_text = burn_in.as_dict()
        run.manifest.burned_in_acknowledged = allow_burned_captions
        run.save()
        if burn_in.detected:
            run.logger.warn(
                "intake.burned_in_text",
                acknowledged=allow_burned_captions,
                consequence=(
                    "this text will appear in the original panel, be RESTYLED "
                    "into the comparison panel, and be duplicated by the T12 "
                    "caption track. ClayPipe does not remove it."
                ),
                **burn_in.as_dict(),
            )
            typer.secho(f"WARNING: {burn_in.describe()}", fg=typer.colors.YELLOW, err=True)
            typer.secho(
                "  -> it will be restyled into the top panel as textured glyphs, "
                "and T12 captions would duplicate it.\n"
                "  -> ClayPipe does not remove burned-in text. Judge the canary "
                "on character and surface, not on the text.",
                fg=typer.colors.YELLOW, err=True,
            )
            if not allow_burned_captions:
                typer.secho(
                    "  -> pass --allow-burned-captions to record that you saw "
                    "this before spending.",
                    fg=typer.colors.YELLOW, err=True,
                )

    run.logger.info(
        "intake",
        source=str(video),
        style=style,
        fps=run.manifest.fps,
        backend=backend,
        mode=mode,
        mode_calibrated=mode_cfg.calibrated,
        duration_s=duration,
        references=len(ref),
        source_size=f"{source_width}x{source_height}",
        **layout.as_dict(),
    )
    typer.echo(run.paths.root)


def _require_canary_kind_matches_mode(run: Run) -> None:
    """T14/A3: a Track C run needs a CLIP canary, not three stills.

    Three still frames cannot canary a video model. Temporal behaviour is the
    only reason to reach for one, and a still shows none of it — so an approved
    frame canary on a resynth run is an approval of something nobody looked at.

    Checked against the MANIFEST, which `canary restyle` wrote, rather than
    against the verdict: the verdict arrives as a query string the operator
    pastes, and a gate that can be satisfied by editing a URL is not a gate
    (D17's whole point).
    """
    if run.manifest.mode != "resynth":
        return
    kind = run.manifest.canary_kind
    if kind == "clip":
        return
    run.logger.error(
        "batch.blocked", breach="canary_kind_mismatch",
        mode=run.manifest.mode, canary_kind=kind,
    )
    _fail(
        f"this run is mode {run.manifest.mode!r}, which needs a CLIP canary, "
        f"but its canary is {kind or 'absent'!r}.\n"
        "Three still frames cannot canary a video model — temporal behaviour "
        "is the only reason to use one, and a still shows none of it. Run "
        "`claypipe canary restyle --clip <seconds>` and approve that instead."
    )


def _require_canary(run: Run, weights, *, wait: bool) -> dict:
    """STAGE 1 GATE. Nothing downstream of this line may spend money.

    Three outcomes, and only one of them continues:
      approved: true     -> proceed
      approved: false    -> stop, surfacing the operator's reason verbatim
      approved: "adjust" -> record the revised prompt beside the run and stop,
                            because an adjusted prompt means the canary itself
                            has to be re-shot before a batch is worth paying for
    """
    path = run.paths.root / verdi_loaders.CANARY_VERDICT_NAME
    try:
        if wait:
            verdict = verdi_loaders.poll_canary_verdict(
                path,
                timeout_s=weights.review.canary_timeout_s,
                poll_s=weights.review.canary_poll_s,
            )
        else:
            verdict = verdi_loaders.read_canary_verdict(path)
    except verdi_loaders.NoCanaryVerdict:
        run.logger.error("batch.blocked", breach="canary_missing")
        _fail(
            f"canary gate: {verdi_loaders.CANARY_VERDICT_NAME} not found in "
            f"{run.paths.root}. Batch cannot start before a human has reviewed "
            "the canary frames. Run `claypipe canary render` then "
            "`claypipe canary pack`, decide, and submit with "
            "`claypipe canary submit`."
        )
    except verdi_loaders.CanaryTimeout as exc:
        run.logger.error("batch.blocked", breach="canary_timeout")
        _fail(f"canary gate: {exc}")
    except ConfigError as exc:
        run.logger.error("batch.blocked", breach="canary_unreadable")
        _fail(f"canary gate: {exc}")

    if verdict["approved"] is False:
        reason = verdict.get("reason", "") or "(no reason given)"
        run.logger.error("batch.blocked", breach="canary_not_approved", reason=reason)
        _fail(
            f"canary gate: the canary was REJECTED by "
            f"{verdict.get('decider', 'the operator')}.\nReason: {reason}"
        )

    if verdict["approved"] == "adjust":
        written = verdi_loaders.merge_prompt_override(run.paths.manifest, verdict)
        run.logger.warn(
            "batch.blocked",
            breach="canary_adjust",
            prompt_override=str(written) if written else None,
        )
        _fail(
            "canary gate: the canary came back ADJUST, so the prompt changed and "
            "the canary has to be re-shot before a batch is worth paying for.\n"
            f"The revised prompt was written to {written}, and run.json now "
            "points at it. Re-run the canary, then batch."
        )

    run.logger.info(
        "batch.canary.approved",
        decider=verdict.get("decider"),
        decided_at=verdict.get("decided_at"),
    )
    return verdict


def _effective_prompt(run: Run, profile) -> str:
    """The style prompt, unless this run carries an operator override.

    The override lives beside the run and the manifest only POINTS at it, so a
    per-run adjustment can never leak back into styles.yaml (memory.md D12/D8
    discipline: one canonical source, per-run deviations recorded separately).
    """
    source = run.manifest.prompt_override_source
    if not source:
        return profile.prompt
    path = run.paths.root / source
    if not path.is_file():
        _fail(
            f"run.json points at prompt override {source!r} but "
            f"{path} is missing. Refusing to silently fall back to the style "
            "prompt — that would spend money on a prompt nobody approved."
        )
    override = path.read_text().strip()
    run.logger.info("batch.prompt_override", source=source, chars=len(override))
    return override


def _run_with_propagation(
    run: Run, weights, backend, profile, prompt: str, shot_plan, ledger,
    *, residual_max: float | None, max_chain: int | None,
    scorer=None, drift_scoring: bool = False,
    drift_sample: int = propagate.DEFAULT_DRIFT_SAMPLE,
) -> int:
    """T18: plan the keyframes, show the cost, then spend only on those.

    Planning happens BEFORE the ledger authorises anything, so the operator
    sees the paid-frame count and the projected cost before a single call goes
    out. A propagation scheme that only reveals its cost by spending it is not
    a cost reduction.
    """
    sources = frame_paths(run.paths.source_frames)
    keyframe_plan = propagate.plan_keyframes(
        source_frames=sources,
        shot_plan=shot_plan,
        cfg=weights.temporal,
        residual_max=(
            residual_max if residual_max is not None else propagate.DEFAULT_RESIDUAL_MAX
        ),
        max_chain=max_chain if max_chain is not None else propagate.DEFAULT_MAX_CHAIN,
        logger=run.logger,
    )
    keyframe_plan.write(run.paths.keyframe_plan)

    summary = keyframe_plan.summary()
    try:
        unit_price = weights.firewalls.cost.per_call(backend.name)
    except ConfigError as exc:
        _fail(str(exc))
    projected = keyframe_plan.paid_frames * unit_price
    without = keyframe_plan.total_frames * unit_price
    run.logger.info(
        "propagate.projection",
        paid_frames=keyframe_plan.paid_frames,
        total_frames=keyframe_plan.total_frames,
        reduction_factor=summary["reduction_factor"],
        projected_usd=round(projected, 4),
        without_propagation_usd=round(without, 4),
        saved_usd=round(without - projected, 4),
    )
    typer.echo(
        f"propagation: {keyframe_plan.paid_frames} paid of "
        f"{keyframe_plan.total_frames} frames ({summary['reduction_factor']}x) "
        f"— ${projected:.4f} instead of ${without:.4f}"
    )

    def restyle_keyframe(index: int, src: Path, dst: Path) -> None:
        entry_id = ledger.authorize(
            frame=src.name, backend=backend.name, stage="batch"
        )
        backend.restyle(
            src, dst, prompt=prompt, strength=profile.strength,
            seed=shot_plan.seed_for_frame(index),
        )
        ledger.reconcile(entry_id, backend.cost_per_frame_usd())

    result = propagate.execute_plan(
        plan=keyframe_plan, source_frames=sources,
        out_dir=run.paths.restyled_frames, cfg=weights.temporal,
        restyle_keyframe=restyle_keyframe, logger=run.logger,
    )

    # T18a: the INDEPENDENT drift check. Until this existed, propagation had no
    # drift check at all while already being wired into batch — TF cannot serve
    # as one, because a propagated frame is a warp along the optical flow and
    # TF grades by warping along the optical flow. Same operation, so the score
    # is near-circular.
    #
    # Needs the learned metric, so it is skipped on backends that are not
    # scored at all (dummy, D27) unless the operator asks — and when it IS run
    # on dummy the report says the result is a null control, not a validation.
    if scorer is not None or drift_scoring:
        _score_propagation_drift(
            run, weights, keyframe_plan, sources, backend, sample=drift_sample
        )
    else:
        run.logger.info(
            "propagate.drift.skipped",
            reason="backend is unscored (D27); pass --drift-scoring to measure "
                   "the dummy null control anyway",
        )
    return result["total_frames"]


def _score_propagation_drift(
    run: Run, weights, keyframe_plan, sources, backend, *, sample: int
) -> None:
    """Score propagated frames against their SOURCE frames (T18a)."""
    try:
        from .pipeline.score import Scorer, models_are_cached
    except ImportError as exc:
        run.logger.warn("propagate.drift.unavailable", error=str(exc))
        return
    if not models_are_cached():
        run.logger.warn(
            "propagate.drift.unavailable",
            reason="LPIPS/CLIP weights are not cached; drift scoring needs the "
                   "learned metric and must never download mid-run",
        )
        return

    scorer = Scorer.build(weights)
    scores = propagate.score_drift(
        plan=keyframe_plan, source_frames=sources,
        out_dir=run.paths.restyled_frames, scorer=scorer,
        sample=sample, logger=run.logger,
    )
    if not scores:
        return
    propagate.write_drift_scores(scores, run.paths.drift)

    summary = propagate.drift_summary(scores)
    typer.echo(
        f"propagation drift (vs SOURCE, {summary['drift_sampled']} sampled): "
        f"SSIM mean {summary['ssim_mean']} min {summary['ssim_min']}, "
        f"LPIPS-edges mean {summary['lpips_edges_mean']} "
        f"max {summary['lpips_edges_max']}, deepest chain "
        f"{summary['max_depth_sampled']}"
    )
    if backend.name == "dummy":
        typer.secho(
            "  NOTE: this is a NULL CONTROL, not a validation. The dummy "
            "backend outputs flat posterised colour fields, which resample "
            "near-losslessly — it cannot show the smear repeated warping would "
            "cause in real clay texture. Read a flat curve here as 'the "
            "measurement works', not as 'propagation is safe'.",
            fg=typer.colors.YELLOW,
        )
    typer.secho(
        "  NOT gated: no calibrated threshold for source-referenced drift "
        "exists until T16 measures one on a real backend.",
        fg=typer.colors.CYAN,
    )


def _build_shot_plan(run: Run, extracted: int):
    """The run's shot plan (T11): detected once, then reused.

    A cached plan is reused only if it was built for this many frames at this
    fps — otherwise it belongs to a different extraction and its boundaries are
    in the wrong places. PySceneDetect absent is a REFUSAL, not a fallback to
    one whole-clip shot: that fallback would silently restore per-clip seeding
    and the false temporal flags at every cut, which is the bug T11 exists to
    fix. `--single-shot` is the explicit escape hatch.
    """
    path = run.paths.shot_plan
    if path.is_file():
        try:
            cached = shots.ShotPlan.read(path)
        except Exception as exc:
            run.logger.warn("shots.cache_unreadable", path=str(path), error=str(exc))
        else:
            if cached.total_frames == extracted and cached.fps == run.manifest.fps:
                run.logger.info("shots.reused", **cached.summary())
                return cached
            run.logger.warn(
                "shots.cache_stale",
                cached_frames=cached.total_frames, extracted=extracted,
                cached_fps=cached.fps, fps=run.manifest.fps,
            )

    plan = shots.detect_shots(
        video=Path(run.manifest.source_path),
        fps=run.manifest.fps,
        total_frames=extracted,
        frames_dir=run.paths.source_frames,
    )
    plan.write(path)
    run.logger.info("shots.detected", **plan.summary())
    return plan


def _build_scoring(run: Run, weights, backend, frame_count: int, profile):
    """Build the Scorer and RetryController — except on the dummy backend.

    SCORING IS SKIPPED FOR `dummy`, deliberately and permanently (memory.md D27).
    The dummy's whole visual difference from the source is a posterise plus a
    deterministic `1.0 + (seed % 7) * 0.05` saturation nudge; there is no
    generative content to grade. Scoring it would manufacture numbers that look
    like quality measurements while measuring nothing, and would run LPIPS and
    CLIP over every frame of every offline test run to do it.

    On any other backend the scorer is mandatory: a paid run that is not scored
    is a paid run nobody can defend.
    """
    if backend.name == "dummy":
        run.logger.info(
            "batch.scoring.skipped",
            backend=backend.name,
            reason="dummy backend produces no generative content to grade (D27)",
        )
        return None, None, []

    references = [Path(p) for p in run.manifest.reference_images]
    _warn_on_source_origin_references(run, references)
    missing = [str(p) for p in references if not p.is_file()]
    if missing:
        _fail(
            "reference images locked at intake are missing: "
            + ", ".join(missing)
            + "\nRefusing to start a paid run whose identity metric cannot be computed."
        )
    if not references:
        # Fail BEFORE the ledger authorises anything, rather than at frame 1
        # with money already spent.
        #
        # D12 CLOSED by T10: "how does a style with no character (e.g. `logo`)
        # score identity?" — against the canary frames the operator approved.
        # No character is required and no special case is needed, which is why
        # the error below points at the canary rather than at --ref.
        _fail(
            f"backend {backend.name!r} scores every frame, and identity scoring "
            "needs at least one reference image (F weights ID at 0.20 and the "
            "formula is fixed).\n"
            "The reference SHOULD be an approved canary frame (T10/F1): run "
            "`claypipe canary render`, approve it, and `claypipe canary submit` "
            "locks the approved frames into refs/ automatically.\n"
            "`intake --ref <image>` also works, but photoreal stills will miss "
            "id_min on nearly every frame — see the F1 note in MASTER_PLAN."
        )

    try:
        scorer = Scorer.build(weights)
        loaded = [load_image(p) for p in references]
        # V4: region crops, when the approved canary produced any. Missing is
        # normal (a three-frame canary has no consecutive pair) and makes ID
        # fall back to whole-frame with the support recorded on every score.
        region_paths = [Path(p) for p in run.manifest.reference_regions]
        scorer.region_references = [
            load_image(p) for p in region_paths if p.is_file()
        ]
    except ScoringError as exc:
        _fail(str(exc))

    controller = RetryController(
        cfg=weights, paths=run.paths, frame_count=frame_count,
        base_strength=profile.strength, logger=run.logger,
    )
    run.logger.info(
        "batch.scoring.enabled",
        backend=backend.name, references=len(loaded),
        region_references=len(scorer.region_references),
        id_support=(
            "regions" if scorer.region_references else "whole_frame"
        ),
        retry_budget=controller.retry_budget,
    )
    return scorer, controller, loaded


def _warn_on_source_origin_references(run: Run, references: list[Path]) -> None:
    """T10/F1: say so, at startup, when ID is about to measure the wrong thing.

    Warns rather than refuses. An operator may deliberately want to hold a clay
    restyle to a photoreal reference — that is a legitimate (if expensive)
    choice — but it must be a choice made with the consequence stated, not a
    default discovered when the retry budget halts the run at frame ~107.
    """
    if run.manifest.reference_origin == "canary":
        return

    inside_source_frames = [
        str(p) for p in references
        if run.paths.source_frames.resolve() in p.resolve().parents
    ]
    run.logger.warn(
        "batch.references.photoreal_risk",
        origin=run.manifest.reference_origin,
        count=len(references),
        are_source_frames=inside_source_frames,
        consequence=(
            "identity is about to be scored against references that were not "
            "produced by this style. CLIP cosine moves hard under total style "
            "transfer, so a real restyle typically lands 0.65-0.82 against a "
            "photoreal reference, misses targets.id_min (0.85) on nearly every "
            "frame, and exhausts the retry budget mid-run (F1)."
        ),
        fix=(
            "approve a canary first — `claypipe canary submit` locks the "
            "approved restyled frames into refs/ and re-points ID at them. Do "
            "NOT lower id_min instead: that trades a calibration bug for a "
            "blind gate."
        ),
    )
    typer.secho(
        "WARNING: identity references came from `intake --ref`, not from an "
        "approved canary. If they are photoreal, expect near-universal id_min "
        "misses and a halted retry budget (F1). Approve a canary to re-point "
        "them.",
        fg=typer.colors.YELLOW, err=True,
    )
    if inside_source_frames:
        typer.secho(
            "WARNING: "
            + str(len(inside_source_frames))
            + " reference(s) are this run's own SOURCE frames. Identity will "
            "measure how much the restyle failed to change the picture.",
            fg=typer.colors.RED, err=True,
        )


# Backends that cost real money. Everything not listed here is free.
PAID_BACKENDS = {"fal", "wan_vace", "vace"}


def _guard_paid_backend(backend: str, *, live: bool) -> None:
    """Two independent locks in front of any paid endpoint (Rule 5).

    `--live` is the DELIBERATE act: it cannot arrive by accident, by a stale
    shell export, or by a config file someone forgot about. FAL_KEY is the
    CAPABILITY. Requiring both means neither an intentional run without
    credentials nor an unintentional run with them reaches the endpoint — and
    both refusals are loud at startup, not discovered mid-batch.
    """
    if backend not in PAID_BACKENDS:
        return

    if not live:
        _fail(
            f"Refusing to use paid backend {backend!r} without --live.\n"
            "--live is a deliberate act: it says you intend to spend money on "
            "this run. Add it only when you mean it."
        )

    if not os.environ.get("FAL_KEY", "").strip():
        _fail(
            f"backend {backend!r} needs FAL_KEY and it is unset or empty.\n"
            "Export it in the shell that runs claypipe, or put it in .env "
            "(which is gitignored). Never pass a key as a command-line "
            "argument — it lands in your shell history."
        )


@app.command()
def batch(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
    max_cost_usd: Optional[float] = typer.Option(
        None, "--max-cost-usd",
        help="Hard cap on this run's cumulative API spend. Halts before the "
             "call that would breach it.",
    ),
    backend_override: Optional[str] = typer.Option(
        None, "--backend",
        help="Override the backend locked at intake for this run.",
    ),
    live: bool = typer.Option(
        False, "--live",
        help="Permit a PAID backend to make real API calls. Required for "
             "--backend fal, alongside a non-empty FAL_KEY.",
    ),
    wait_for_canary: bool = typer.Option(
        False, "--wait-for-canary",
        help="Block and poll for canary_verdict.json instead of failing "
             "immediately when it is absent. Useful when the operator is "
             "reviewing the page in another window.",
    ),
    propagate_keyframes: bool = typer.Option(
        False, "--propagate",
        help="RETIRED. Keyframe propagation saved money only on a PER-FRAME "
             "backend; a video model bills per frame or per second regardless, "
             "so warping frames forward buys nothing and costs fidelity. Kept "
             "for one release with the retired per-frame path.",
    ),
    drift_scoring: bool = typer.Option(
        False, "--drift-scoring",
        help="T18a: score propagated frames against their SOURCE frames even "
             "on an unscored backend. On dummy this measures the null control "
             "— useful to prove the instrument works, not to validate "
             "propagation.",
    ),
    drift_sample: int = typer.Option(
        propagate.DEFAULT_DRIFT_SAMPLE, "--drift-sample",
        help="How many propagated frames to score for drift, stratified by "
             "warp-chain depth.",
    ),
    residual_max: Optional[float] = typer.Option(
        None, "--keyframe-residual-max",
        help="A frame whose flow-explained residual exceeds this gets a new "
             "paid keyframe. Higher = cheaper and more drift.",
    ),
    max_chain: Optional[int] = typer.Option(
        None, "--keyframe-max-chain",
        help="Hard ceiling on how many warps may be chained before forcing a "
             "paid keyframe. Bounds drift regardless of residual.",
    ),
    single_shot: bool = typer.Option(
        False, "--single-shot",
        help="Treat the whole clip as one shot: one seed throughout, and no "
             "boundary-aware temporal scoring. The explicit escape hatch for "
             "a clip with no cuts, or when PySceneDetect is unavailable. "
             "Logged as a warning — it disables the T11 fixes.",
    ),
) -> None:
    """STAGE 2 — extract frames + audio, then restyle every frame.

    Blocked by the canary gate: without an approved `canary_verdict.json` this
    command refuses to start. There is no override flag, deliberately.
    """
    styles, weights, _ = _startup()
    base = runs_dir or styles.output.runs_dir
    try:
        run = Run.load(resolve_run(run_dir, Path(base)))
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))

    if backend_override and backend_override != run.manifest.backend:
        run.logger.info(
            "batch.backend_override",
            locked_at_intake=run.manifest.backend, using=backend_override,
        )
        run.manifest.backend = backend_override
        run.save()

    # FIREWALL 0: intent and credentials, before anything touches the disk.
    _guard_paid_backend(run.manifest.backend, live=live)

    # FIREWALL 0b (T13/F4): a PAID run in a mode whose target vector was never
    # measured is refused. An uncalibrated mode's numbers have the right shape
    # and no evidence, so the gate would either pass everything or reject
    # everything — both of which look like a working run until the bill lands.
    # Free backends are exempt: an unscored dummy run (D27) gates nothing.
    if run.manifest.backend in PAID_BACKENDS:
        try:
            weights.assert_mode_is_spendable(run.manifest.mode)
        except ConfigError as exc:
            _fail(str(exc))

    # FIREWALL 1: no approved canary verdict -> no spend. Checked before the
    # backend is even constructed, and long before the ledger authorises a call.
    verdict = _require_canary(run, weights, wait=wait_for_canary)

    # FIREWALL 1b (T14/A3): the canary must be the right KIND for the mode.
    _require_canary_kind_matches_mode(run)

    profile = styles.profile(run.manifest.style)
    prompt = _effective_prompt(run, profile)
    try:
        backend = get_backend(run.manifest.backend, live=live)
    except (NotImplementedError, ValueError) as exc:
        _fail(str(exc))

    ledger = SpendLedger(
        paths=run.paths, project_dir=Path(base), cfg=weights.firewalls,
        run_id=run.run_id, logger=run.logger, max_cost_usd_run=max_cost_usd,
    )

    source = Path(run.manifest.source_path)
    if not source.is_file():
        _fail(f"source video has moved or been deleted: {source}")

    try:
        extracted = extract_frames(
            source, run.paths.source_frames, run.manifest.fps, run.logger,
            incident_dir=run.paths.root,
        )
        extract_audio(source, run.paths.audio, run.logger)
    except Exception as exc:
        run.logger.error("batch.failed", error=str(exc))
        _fail(str(exc))

    # T11: shot boundaries drive the seed (one fixed seed per shot) and tell the
    # scorer where NOT to measure temporal drift. Detected after extraction,
    # because motion ranking reads the extracted frames.
    if single_shot:
        plan = shots.single_shot_plan(extracted, run.manifest.fps)
        plan.write(run.paths.shot_plan)
        run.logger.warn(
            "shots.single_shot_forced",
            reason="--single-shot given; per-shot seeding and boundary-aware "
                   "temporal scoring are BOTH disabled for this run",
            **plan.summary(),
        )
    else:
        try:
            plan = _build_shot_plan(run, extracted)
        except shots.ShotDetectionError as exc:
            _fail(str(exc))

    run.logger.info(
        "batch.mode",
        mode=run.manifest.mode,
        calibrated=weights.mode(run.manifest.mode).calibrated
        if run.manifest.mode in weights.modes else None,
        price_unit=weights.firewalls.cost.unit_for(run.manifest.backend),
    )
    scorer, controller, references = _build_scoring(run, weights, backend, extracted, profile)

    # ---- RETIRED: adaptive keyframe propagation -------------------------
    if propagate_keyframes:
        if run.manifest.mode != LEGACY_MODE:
            _fail(
                f"--propagate belongs to the retired {LEGACY_MODE!r} path. A "
                "video model bills per frame or per second whether or not the "
                "frames were warped, so propagation saves nothing here and "
                "costs fidelity — and a clip backend already produces its own "
                "temporal coherence, which warping would fight."
            )
        run.logger.warn(
            "batch.propagate.retired",
            reason="propagation saved money only on a per-frame backend; a "
                   "video model bills per frame or per second regardless",
            removal="scheduled for the release after next",
        )
        typer.secho(
            "WARNING: --propagate is RETIRED and will be removed. It saved "
            "money only on a per-frame backend.",
            fg=typer.colors.YELLOW, err=True,
        )
        try:
            total = _run_with_propagation(
                run, weights, backend, profile, prompt, plan, ledger,
                residual_max=residual_max, max_chain=max_chain,
                scorer=scorer, drift_scoring=drift_scoring,
                drift_sample=drift_sample,
            )
        except (propagate.PropagationError, RunHalted) as exc:
            run.logger.error("batch.failed", error=str(exc))
            _fail(str(exc))
        typer.echo(
            f"{total} frames produced with keyframe propagation — see "
            f"{run.paths.keyframe_plan.name}"
        )
        raise typer.Exit(0)

    try:
        total = restyle_frames(
            backend=backend,
            source_dir=run.paths.source_frames,
            out_dir=run.paths.restyled_frames,
            prompt=prompt,
            strength=profile.strength,
            logger=run.logger,
            seed_for=plan.seed_for_frame,
            boundary_frames=plan.boundary_frames(),
            ledger=ledger,
            scorer=scorer,
            controller=controller,
            references=references,
            scores_path=run.paths.scores,
        )
    except RunHalted as exc:
        run.logger.error("batch.halted", breach=exc.breach.value, error=str(exc))
        _fail(str(exc))
    except Exception as exc:  # surfaced, never swallowed
        run.logger.error("batch.failed", error=str(exc))
        _fail(str(exc))

    if extracted != total or extracted != count_frames(run.paths.restyled_frames):
        _fail(
            f"frame-count mismatch: {extracted} extracted vs "
            f"{count_frames(run.paths.restyled_frames)} restyled"
        )
    if controller is not None:
        summary = controller.summary()
        run.logger.info("batch.scoring.summary", **summary)
        typer.echo(
            f"{extracted} frames restyled with backend '{backend.name}' — "
            f"mean F {summary['mean_f']}, {summary['total_retries']} retries "
            f"of a {summary['retry_budget']} budget"
        )
    else:
        typer.echo(
            f"{extracted} frames restyled with backend '{backend.name}' "
            "(not scored — see memory.md D27)"
        )


@app.command()
def assemble(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
) -> None:
    """STAGE 4 — reassemble, re-mux the original audio, render the 9:16 comparison."""
    styles, _, _ = _startup()
    base = runs_dir or styles.output.runs_dir
    try:
        run = Run.load(resolve_run(run_dir, Path(base)))
    except FileNotFoundError as exc:
        _fail(str(exc))

    profile = styles.profile(run.manifest.style)
    if not run.paths.audio.is_file():
        _fail("audio.aac is missing — run `claypipe batch` first")

    try:
        source_md5 = ffmpeg.stream_md5(run.paths.audio, "audio")
        result = assemble_stage.assemble(run, styles.render, profile, source_md5)
    except (assemble_stage.AssemblyError, ffmpeg.FFmpegError, ConfigError) as exc:
        run.logger.error("assemble.failed", error=str(exc))
        _fail(str(exc))

    card = qccard.build_card(
        run,
        frames=count_frames(run.paths.restyled_frames),
        verdict="assembled",
        extra={"output": {"path": str(run.paths.final), **result}},
    )
    qccard.write_card(run, card, Path(base))
    typer.echo(run.paths.final)


# --------------------------------------------------------------------------
# STAGE 1 — the canary. render -> pack -> (human decides) -> submit
# --------------------------------------------------------------------------

canary_app = typer.Typer(
    help="STAGE 1 — canary review: render the page, open it, record the verdict.",
    no_args_is_help=True,
)
app.add_typer(canary_app, name="canary")


def _load_run_for(run_dir: Path, runs_dir: Optional[Path], styles) -> tuple[Run, Path]:
    base = Path(runs_dir or styles.output.runs_dir)
    try:
        return Run.load(resolve_run(run_dir, base)), base
    except FileNotFoundError as exc:
        _fail(str(exc))


def _canary_frame_names(frames: list[Path], plan=None) -> tuple[list[Path], str]:
    """The three representative frames (SPEC §4.1): first, middle, most-motion.

    D30 CLOSED by T11. The third slot was the LAST frame as a stated stand-in
    because shot detection did not exist. It is now the middle of the busiest
    shot, ranked by mean inter-frame difference over the SOURCE frames — the
    operator is choosing which moment to inspect, and that choice must not
    depend on what the backend already did to it.

    Without a shot plan (a pre-T11 run, or `--single-shot`) the stand-in is
    kept and the caption still says so. It is never silently the wrong frame.
    """
    if not frames:
        return [], CANARY_CAPTION_STANDIN
    if len(frames) <= 3:
        # Three or fewer frames exist, so they ARE the selection — this is the
        # normal state straight after `canary restyle`, which generated exactly
        # the slots this function chose from the source frames. Saying
        # "no shot plan" here would be wrong: the plan was already applied.
        return frames, CANARY_CAPTION_PRESELECTED

    first, middle = frames[0], frames[len(frames) // 2]
    taken = {first.name, middle.name}
    third = frames[-1]
    caption = CANARY_CAPTION_STANDIN

    if plan is not None and plan.shots:
        # The most-motion frame can LAND ON a slot already taken — on a clip
        # with one shot, its midpoint IS the middle frame. Silently returning
        # two frames for a three-frame canary would mean the operator approves
        # less than they were shown a price for, so collisions fall through to
        # the next distinct candidate inside the same busiest shot.
        try:
            busiest = plan.most_motion_shot()
            candidates = [
                plan.most_motion_frame(),
                busiest.end_frame,
                busiest.start_frame,
            ]
        except shots.ShotDetectionError:
            candidates = []
        for index in candidates:
            if not (1 <= index <= len(frames)):
                continue
            candidate = frames[index - 1]
            if candidate.name in taken:
                continue
            third = candidate
            caption = (
                f"most-motion shot ({busiest.index} of {len(plan.shots)}, "
                f"{busiest.duration_s:.2f}s, motion {busiest.motion:.2f})"
            )
            break

    if third.name in taken:
        # Last resort: any frame not already chosen, working backwards.
        for candidate in reversed(frames):
            if candidate.name not in taken:
                third = candidate
                caption = CANARY_CAPTION_STANDIN
                break

    chosen = [first, middle, third]
    assert len({p.name for p in chosen}) == 3, (
        f"canary selection collapsed to {[p.name for p in chosen]} — a "
        "three-frame canary must show three distinct frames"
    )
    return chosen, caption


CANARY_CAPTION_STANDIN = "last frame (stand-in: no shot plan for this run)"
CANARY_CAPTION_PRESELECTED = "third slot, selected by `canary restyle`"


def _canary_restyle_clip(
    run: Run, weights, sources: list[Path], plan, seconds: float,
    prompt: str, profile, ledger, *, base: Path, live: bool,
) -> None:
    """Restyle a contiguous trim through a ClipRestyleBackend (T14/A3).

    The trim is taken from the MOST-MOTION shot, not from the head of the clip.
    A video model's failure mode is temporal — flicker, smearing, identity
    wandering between frames — and the opening seconds of a clip are often a
    static establishing shot where none of that shows. Canarying the calm part
    of a clip is how a temporal model passes a gate it should fail.
    """
    fps = run.manifest.fps
    want = max(1, int(round(seconds * fps)))
    if want > len(sources):
        _fail(
            f"--clip {seconds}s is {want} frames at {fps}fps, but the run only "
            f"has {len(sources)} extracted frames ({len(sources) / fps:.2f}s). "
            "Ask for less."
        )

    start = 1
    anchor = "clip head (no shot plan)"
    if plan is not None and plan.shots:
        try:
            busiest = plan.most_motion_shot()
            # Centre the trim on the busiest shot, clamped into the clip.
            centre = busiest.start_frame + busiest.frame_count // 2
            start = max(1, min(centre - want // 2, len(sources) - want + 1))
            anchor = (
                f"most-motion shot {busiest.index} of {len(plan.shots)} "
                f"(motion {busiest.motion:.2f})"
            )
        except shots.ShotDetectionError:
            pass

    chunk = sources[start - 1 : start - 1 + want]
    try:
        clip_backend = get_clip_backend(run.manifest.backend, live=live)
    except ValueError as exc:
        _fail(str(exc))

    # A clip backend has a MINIMUM CHUNK, and asking below it does not make the
    # call cheaper — the backend bills its minimum, or pads the range, which
    # would change the frame count and break the duration invariant.
    #
    # This bites the plan's own numbers. MASTER_PLAN §A3 prices a 3-second Wan
    # VACE canary at $0.12, but VACE's floor is 81 frames at 16fps native =
    # 5.06 video-seconds = $0.20. A 3-second canary at the pipeline's 12fps is
    # 36 frames, which VACE cannot honour at all. Stated here rather than
    # discovered on the invoice.
    if len(chunk) < clip_backend.min_chunk_frames:
        floor_seconds = clip_backend.min_chunk_frames / clip_backend.native_fps
        # Priced in FRAMES (V1): VACE bills frame-count/16, so pricing the
        # floor as a wall-clock duration would quote the wrong figure in the
        # very warning that exists to stop the operator overpaying.
        billed = weights.firewalls.cost.price_for(
            clip_backend.name, frames=clip_backend.min_chunk_frames
        )
        asked = weights.firewalls.cost.price_for(
            clip_backend.name, frames=len(chunk)
        )
        run.logger.warn(
            "canary.restyle.below_min_chunk",
            asked_frames=len(chunk),
            asked_seconds=round(len(chunk) / fps, 3),
            min_chunk_frames=clip_backend.min_chunk_frames,
            native_fps=clip_backend.native_fps,
            floor_seconds=round(floor_seconds, 3),
            asked_usd=round(asked, 4),
            billed_usd_floor=round(billed, 4),
            consequence=(
                "the backend cannot honour a chunk this short. It will bill its "
                "minimum, or pad the range — and padding changes the frame count, "
                "which breaks the duration invariant. Asking below the floor "
                "buys a smaller canary at the same price."
            ),
        )
        typer.secho(
            f"WARNING: --clip {seconds}s is {len(chunk)} frames, below "
            f"{clip_backend.name}'s {clip_backend.min_chunk_frames}-frame "
            f"minimum ({floor_seconds:.2f}s at its native {clip_backend.native_fps}fps). "
            f"A real backend bills the minimum (${billed:.4f}), not what you "
            f"asked for (${asked:.4f}) — so ask for at least "
            f"{floor_seconds:.2f}s and see more of the clip for the same money.",
            fg=typer.colors.YELLOW, err=True,
        )

    seed = plan.seed_for_frame(start) if plan is not None else 1000
    try:
        written = restyle_clip_range(
            backend=clip_backend, src_frames=chunk,
            out_dir=run.paths.restyled_frames, prompt=prompt,
            strength=profile.strength, seed=seed, logger=run.logger,
            ledger=ledger, fps=fps,
        )
    except ChunkLengthError as exc:
        run.logger.error("canary.restyle.chunk_length", error=str(exc))
        _fail(str(exc))

    run.manifest.canary_kind = "clip"
    run.manifest.canary_clip_seconds = len(written) / fps
    run.save()
    run.logger.info(
        "canary.restyle",
        backend=clip_backend.name, kind="clip",
        frames=len(written), seconds=round(len(written) / fps, 3),
        first_frame=chunk[0].name, last_frame=chunk[-1].name,
        anchor=anchor, seed=seed,
        native_fps=clip_backend.native_fps, pipeline_fps=fps,
        scored=False,
        reason="no identity references exist yet — T10 makes the approved "
               "output of this stage into them",
        est_cost_usd=round(
            clip_backend.cost_per_video_second_usd() * len(written) / fps, 4
        ),
    )
    typer.echo(
        f"{len(written)} frames ({len(written) / fps:.2f}s) restyled as a CLIP "
        f"canary from the {anchor} with backend {clip_backend.name!r} — now run "
        "`claypipe canary render`"
    )


@canary_app.command("restyle")
def canary_restyle(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
    backend_override: Optional[str] = typer.Option(
        None, "--backend", help="Override the backend locked at intake."
    ),
    live: bool = typer.Option(
        False, "--live",
        help="Permit a PAID backend to make real API calls. Required for "
             "--backend fal, alongside a non-empty FAL_KEY.",
    ),
    max_cost_usd: Optional[float] = typer.Option(
        None, "--max-cost-usd", help="Hard cap on this stage's spend."
    ),
    clip: Optional[float] = typer.Option(
        None, "--clip",
        help="Render a CLIP canary of this many seconds instead of three "
             "stills. REQUIRED for --mode resynth: three frames cannot canary "
             "a video model. At Track C prices a 3-second canary is $0.12-0.54 "
             "— the same order as a three-frame canary, so the firewall "
             "economics are unchanged.",
    ),
) -> None:
    """STAGE 1 — restyle ONLY the canary frames (or a short clip), for review.

    This is the stage the canary gate was always meant to sit behind. `batch`
    refuses to start without an approved verdict (D17), and the verdict needs
    frames to look at — so before T10a the only way to get them was to run the
    full batch first, which paid for every frame of a look nobody had approved.

    Three frames, through the ledger, at a cost the firewalls authorise
    up front. On fal Kontext pro that is $0.12 instead of $28.80.

    Deliberately UNSCORED. There are no identity references yet — the whole
    point of T10 is that the approved output of THIS stage becomes them. A
    score with no reference would be a number with a hole in it.
    """
    styles, weights, _ = _startup()
    run, base = _load_run_for(run_dir, runs_dir, styles)

    if backend_override and backend_override != run.manifest.backend:
        run.logger.info(
            "canary.restyle.backend_override",
            locked_at_intake=run.manifest.backend, using=backend_override,
        )
        run.manifest.backend = backend_override
        run.save()

    _guard_paid_backend(run.manifest.backend, live=live)

    profile = styles.profile(run.manifest.style)
    prompt = _effective_prompt(run, profile)
    try:
        backend = get_backend(run.manifest.backend, live=live)
    except (NotImplementedError, ValueError) as exc:
        _fail(str(exc))

    source = Path(run.manifest.source_path)
    if not source.is_file():
        _fail(f"source video has moved or been deleted: {source}")

    try:
        extracted = extract_frames(
            source, run.paths.source_frames, run.manifest.fps, run.logger,
            incident_dir=run.paths.root,
        )
        extract_audio(source, run.paths.audio, run.logger)
    except Exception as exc:
        run.logger.error("canary.restyle.failed", error=str(exc))
        _fail(str(exc))

    # The shot plan picks the most-motion slot (T11/D30) and supplies the
    # per-shot seed, so a canary frame is generated with the SAME seed the full
    # batch would use for it. Otherwise the operator approves a look the batch
    # then does not reproduce.
    plan = None
    try:
        plan = _build_shot_plan(run, extracted)
    except shots.ShotDetectionError as exc:
        run.logger.warn(
            "canary.restyle.no_shot_plan",
            error=str(exc),
            consequence="third slot falls back to the last frame; seeds are per-clip",
        )

    sources = frame_paths(run.paths.source_frames)
    if not sources:
        _fail(f"no source frames in {run.paths.source_frames}")

    ledger = SpendLedger(
        paths=run.paths, project_dir=Path(base), cfg=weights.firewalls,
        run_id=run.run_id, logger=run.logger, max_cost_usd_run=max_cost_usd,
    )

    # ---- T14/A3: the CLIP canary ----------------------------------------
    if clip is not None:
        if clip <= 0:
            _fail(f"--clip needs a positive duration, got {clip}")
        _canary_restyle_clip(
            run, weights, sources, plan, clip, prompt, profile, ledger,
            base=Path(base), live=live,
        )
        return

    if run.manifest.mode == "resynth":
        _fail(
            "this run is mode 'resynth', so a three-frame canary would approve "
            "something nobody looked at — temporal behaviour is the only reason "
            "to use a video model, and a still shows none of it. Use "
            "`canary restyle --clip <seconds>`."
        )

    chosen, third_caption = _canary_frame_names(sources, plan)
    run.paths.restyled_frames.mkdir(parents=True, exist_ok=True)
    seed_for = plan.seed_for_frame if plan is not None else (lambda _i: 1000)
    index_of = {p.name: i for i, p in enumerate(sources, start=1)}

    written = []
    for src in chosen:
        dst = run.paths.restyled_frames / src.name
        if dst.is_file():
            run.logger.info("canary.restyle.skip", frame=src.name, reason="already restyled")
            written.append(dst)
            continue
        seed = seed_for(index_of[src.name])
        entry_id = ledger.authorize(frame=src.name, backend=backend.name, stage="canary")
        backend.restyle(src, dst, prompt=prompt, strength=profile.strength, seed=seed)
        ledger.reconcile(entry_id, backend.cost_per_frame_usd())
        run.logger.info("canary.restyle.frame", frame=src.name, seed=seed)
        written.append(dst)

    run.manifest.canary_kind = "frames"
    run.manifest.canary_clip_seconds = None
    run.save()
    run.logger.info(
        "canary.restyle",
        backend=backend.name, kind="frames",
        frames=len(written), third_slot=third_caption,
        scored=False,
        reason="no identity references exist yet — T10 makes the approved "
               "output of this stage into them",
        est_cost_usd=round(len(written) * backend.cost_per_frame_usd(), 4),
    )
    typer.echo(
        f"{len(written)} canary frame(s) restyled with backend "
        f"{backend.name!r} — now run `claypipe canary render`"
    )


@canary_app.command("render")
def canary_render(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
) -> None:
    """Generate `canary_review.html` from the run's representative frames.

    Also renders the flag page when the run has scores, so an operator reviewing
    a canary can see what the scorer already flagged.
    """
    styles, weights, _ = _startup()
    run, base = _load_run_for(run_dir, runs_dir, styles)

    restyled = frame_paths(run.paths.restyled_frames)
    if not restyled:
        _fail(
            f"no restyled frames in {run.paths.restyled_frames}. Run "
            "`claypipe batch` (or the canary stage) first — there is nothing to review."
        )

    scores = _load_scores(run)
    plan = None
    if run.paths.shot_plan.is_file():
        try:
            plan = shots.ShotPlan.read(run.paths.shot_plan)
        except Exception as exc:
            run.logger.warn("canary.shot_plan_unreadable", error=str(exc))
    chosen, third_caption = _canary_frame_names(restyled, plan)
    captions = ("first frame", "middle frame", third_caption)
    cards = [
        canary_page.CanaryCard(
            name=path.name, image=path, score=scores.get(path.name), caption=caption
        )
        for path, caption in zip(chosen, captions)
    ]
    page = canary_page.write_canary_page(
        run.paths.root,
        run_id=run.run_id,
        style=run.manifest.style,
        backend=run.manifest.backend,
        cards=cards,
        prompt=_effective_prompt(run, styles.profile(run.manifest.style)),
    )
    run.logger.info(
        "canary.render", path=str(page), frames=len(cards), scored=bool(scores),
        third_slot=third_caption, shot_plan=plan is not None,
    )
    typer.echo(page)

    if scores:
        images = {p.name: p for p in restyled}
        selection = flag_page.select_review_frames(
            list(scores.values()), images, weights.review
        )
        flag = flag_page.write_flag_page(
            run.paths.root, run_id=run.run_id, backend=run.manifest.backend,
            selection=selection, cfg=weights.review,
            drift=qccard.summarise_drift(run.paths.drift),
        )
        run.logger.info(
            "flag.render", path=str(flag), cards=selection.shown,
            outliers=selection.outlier_total, audit=selection.audit_total,
        )
        typer.echo(flag)
    else:
        typer.echo("(no scores.jsonl — flag page skipped; nothing has been graded)")


def _load_scores(run: Run) -> dict:
    """Rehydrate FrameScore objects from scores.jsonl, if the run has any."""
    from .pipeline.score import FrameScore, Verdict, VerdictReason

    if not run.paths.scores.is_file():
        return {}
    out = {}
    for line in run.paths.scores.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        out[rec["frame"]] = FrameScore(
            frame=rec["frame"], ssim=rec["ssim"], lpips_edges=rec["lpips_edges"],
            identity=rec["identity"], temporal=rec["temporal"], f=rec["f"],
            verdict=Verdict(rec["verdict"]), reason=VerdictReason(rec["reason"]),
            targets_met=rec["targets_met"],
        )
    return out


@canary_app.command("pack")
def canary_pack(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Open the rendered canary page in the operator's browser.

    `webbrowser.open()` on a local file:// URL. There is no server, and the page
    reaches nothing off the machine (SPEC §5).
    """
    styles, _, _ = _startup()
    run, _ = _load_run_for(run_dir, runs_dir, styles)

    page = run.paths.root / canary_page.PAGE_NAME
    if not page.is_file():
        _fail(f"{page} does not exist. Run `claypipe canary render` first.")

    url = page.resolve().as_uri()
    run.logger.info("canary.pack", url=url, opened=open_browser)
    typer.echo(url)
    if open_browser:
        import webbrowser

        if not webbrowser.open(url):
            typer.secho(
                "could not open a browser automatically — open the URL above by hand",
                fg=typer.colors.YELLOW, err=True,
            )


def _lock_canary_references(run: Run, verdict: dict) -> list[Path]:
    """Copy the APPROVED canary frames into `refs/` and lock them (T10/F1).

    This is the fix for the calibration bug that would have broken the first
    paid run. `intake --ref` locks operator-supplied stills, which are
    photoreal; a real clay restyle scored against a photograph lands ~0.65-0.82
    on CLIP cosine, misses id_min 0.85 on nearly every frame, becomes
    BORDERLINE (D13), retries at strength -0.10 (D18), and exhausts the 15%
    retry budget around frame 107 — at which point the backend gets blamed for
    a reference-selection mistake.

    Pointing ID at the approved canary output changes the question from "does
    this clay puppet resemble a photograph" to "is this the same clay character
    the operator signed off". That is the question the metric exists for, and it
    is why lowering id_min is the WRONG fix: it would trade a calibration bug
    for a blind gate.

    Also closes D12 (how does a style with no character, e.g. `logo`, score
    identity?). The reference is whatever the canary approved — no character
    needed, no special case.

    Frames the operator individually marked `reject` are excluded: approving a
    canary overall while flagging one frame means the other two are the
    reference, not all three.
    """
    frames_verdicts = verdict.get("frames") or {}
    rejected = {
        name for name, fv in frames_verdicts.items()
        if str((fv or {}).get("verdict", "")).lower() == "reject"
    }

    plan = None
    if run.paths.shot_plan.is_file():
        try:
            plan = shots.ShotPlan.read(run.paths.shot_plan)
        except Exception:
            plan = None
    chosen, _caption = _canary_frame_names(frame_paths(run.paths.restyled_frames), plan)

    keep = [p for p in chosen if p.name not in rejected]
    if not keep:
        _fail(
            "every canary frame was individually marked `reject`, so there is "
            "nothing to lock as an identity reference. Reject the canary "
            "outright and adjust the prompt instead."
        )

    run.paths.refs.mkdir(parents=True, exist_ok=True)
    # Clear any previous lock: a re-submitted canary replaces its references
    # rather than accumulating them, or the ID metric would average the
    # approved look against a look that was superseded.
    for stale in run.paths.refs.glob("ref_*.png"):
        stale.unlink()

    locked: list[Path] = []
    for i, src in enumerate(keep, start=1):
        dst = run.paths.refs / f"ref_{i:02d}_{src.name}"
        dst.write_bytes(src.read_bytes())
        locked.append(dst)

    run.manifest.reference_images = [str(p.resolve()) for p in locked]
    run.manifest.reference_origin = "canary"
    run.manifest.reference_regions = [
        str(p.resolve()) for p in _lock_region_references(run, keep)
    ]
    run.save()
    run.logger.info(
        "canary.references.locked",
        origin="canary",
        count=len(locked),
        excluded_rejected=sorted(rejected),
        refs=[p.name for p in locked],
    )
    return locked


def _lock_region_references(run: Run, approved: list[Path]) -> list[Path]:
    """Crop character regions out of the approved canary frames (V4).

    ID under whole-frame v2v asks "is this the same clay CHARACTER", and a
    whole-frame measurement cannot answer it: the environment is being rebuilt
    in clay too, so it dominates the embedding and a wrong character still
    scores well. Scoring a region requires something region-shaped to compare
    against, and it has to come from the same approved output — a crop measured
    against a whole-frame reference asks CLIP whether a person resembles a
    scene.

    Regions come from motion between CONSECUTIVE approved frames. When the
    approved frames are not consecutive (the three-slot frame canary picks
    first/middle/most-motion) there is no motion pair, so none are locked and
    ID honestly falls back to whole-frame. A clip canary, which IS consecutive,
    is the case this exists for.
    """
    try:
        from .pipeline.score import character_regions, load_image
    except ImportError as exc:
        run.logger.warn("canary.region_refs.unavailable", error=str(exc))
        return []

    from PIL import Image

    _styles, weights, _ = _startup()
    ordered = sorted(approved, key=lambda p: p.name)
    pairs = [
        (a, b) for a, b in zip(ordered, ordered[1:])
        if _frame_index(b) - _frame_index(a) == 1
    ]
    if not pairs:
        run.logger.info(
            "canary.region_refs.skipped",
            reason="approved canary frames are not consecutive, so there is no "
                   "motion pair to localise characters from; ID will fall back "
                   "to whole-frame and say so",
            approved=[p.name for p in ordered],
        )
        return []

    region_dir = run.paths.refs / "regions"
    region_dir.mkdir(parents=True, exist_ok=True)
    for stale in region_dir.glob("*.png"):
        stale.unlink()

    written: list[Path] = []
    for previous, current in pairs:
        regions = character_regions(
            load_image(previous), load_image(current), weights.temporal
        )
        image = load_image(current)
        for index, (x, y, w, h) in enumerate(regions, start=1):
            crop = image[y : y + h, x : x + w]
            if crop.size == 0 or min(crop.shape[:2]) < 8:
                continue
            dst = region_dir / f"region_{current.stem}_{index:02d}.png"
            Image.fromarray(crop).save(dst)
            written.append(dst)

    run.logger.info(
        "canary.region_refs.locked",
        count=len(written), pairs=len(pairs),
        note="ID is scored region-against-region while these exist",
    )
    return written


def _frame_index(path: Path) -> int:
    """The 1-based index encoded in an `f_00042.png` style name."""
    digits = "".join(c for c in path.stem if c.isdigit())
    return int(digits) if digits else -1


@canary_app.command("submit")
def canary_submit(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    url: str = typer.Option(
        ..., "--url",
        help="The address-bar URL (or bare query string) from submitting the "
             "canary page. Quote it — it contains & characters.",
    ),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Echo each frame's decision"),
) -> None:
    """Record the operator's canary decision as `canary_verdict.json`.

    The page has no server to POST to, so the decision arrives as the
    form-encoded query string the browser put in the address bar (memory.md D21).
    """
    styles, _, _ = _startup()
    run, _ = _load_run_for(run_dir, runs_dir, styles)

    try:
        verdict = verdi_loaders.canary_verdict_from_form(url)
    except ConfigError as exc:
        _fail(str(exc))

    path = verdi_loaders.write_canary_verdict(run.paths.root, verdict)
    run.logger.info(
        "canary.submit",
        path=str(path), approved=verdict["approved"], decider=verdict.get("decider"),
        frames=len(verdict.get("frames", {})),
        reason=verdict.get("reason") or None,
    )

    # T10/F1: an APPROVED canary is what the identity metric should measure
    # against. Only a full approval locks references — an "adjust" verdict
    # means the look is about to change, so locking it would lock the wrong
    # target, and a rejection has nothing worth locking.
    if verdict["approved"] is True:
        locked = _lock_canary_references(run, verdict)
        typer.secho(
            f"locked {len(locked)} identity reference(s) from the approved "
            f"canary into {run.paths.refs}",
            fg=typer.colors.GREEN,
        )

    approved = verdict["approved"]
    label = {True: "APPROVED", False: "REJECTED"}.get(approved, str(approved).upper())
    typer.echo(f"{label} by {verdict.get('decider', 'operator')} -> {path}")
    if approved is False and verdict.get("reason"):
        typer.echo(f"  reason: {verdict['reason']}")
    if approved == "adjust":
        typer.echo("  prompt_override recorded; re-shoot the canary before batching")
    if verbose:
        for name, entry in sorted(verdict.get("frames", {}).items()):
            note = f" — {entry.get('note')}" if entry.get("note") else ""
            typer.echo(f"  {name}: {entry.get('verdict')}{note}")


@app.command("captions")
def captions_cmd(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
    model_size: str = typer.Option(
        "base", "--model", help="faster-whisper model size: tiny|base|small|medium|large-v3"
    ),
    language: Optional[str] = typer.Option(
        None, "--language", help="Force a language instead of autodetecting."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite",
        help="Replace an existing cues.json. Refused by default, because that "
             "file is hand-edited — it is where the non-dialogue cues live.",
    ),
) -> None:
    """Transcribe the run's audio into phrase-level cues (T12).

    Writes `cues.json`, which `assemble` renders into the gap band between the
    two panels. The file is deliberately hand-editable: non-dialogue cues
    (`[dramatic music]`, `*crunch*`) are PRESERVED and rendered but never
    INVENTED — Whisper transcribes speech, and labelling a sound effect needs
    an audio event classifier this pipeline does not have.

    Runs entirely locally. faster-whisper is an optional extra and costs
    nothing per clip, so this stage is outside the spend firewalls.
    """
    styles, _, _ = _startup()
    run, _ = _load_run_for(run_dir, runs_dir, styles)

    if run.paths.cues.is_file() and not overwrite:
        _fail(
            f"{run.paths.cues} already exists. It is hand-edited — that is "
            "where non-dialogue cues live — so it is not replaced silently. "
            "Pass --overwrite to discard it."
        )
    if not run.paths.audio.is_file():
        _fail(
            f"{run.paths.audio} is missing. Run `claypipe canary restyle` or "
            "`claypipe batch` first — both extract the audio track."
        )

    try:
        cues = captions.transcribe(
            run.paths.audio, model_size=model_size, language=language, logger=run.logger
        )
    except captions.CaptionError as exc:
        _fail(str(exc))

    captions.save_cues(cues, run.paths.cues)
    captions.write_srt(cues, run.paths.subtitles)
    run.logger.info(
        "captions.written",
        path=str(run.paths.cues), cues=len(cues),
        nondialogue=sum(1 for c in cues if c.is_nondialogue),
        model=model_size,
    )
    typer.echo(run.paths.cues)
    typer.secho(
        f"{len(cues)} phrase cues written. Edit {run.paths.cues.name} to fix "
        "transcription or add non-dialogue cues, then run `claypipe assemble`.",
        fg=typer.colors.GREEN,
    )


@app.command()
def export(
    run_dir: Optional[Path] = typer.Argument(None, help="Run directory or run id; omit for all runs"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
    out: Optional[Path] = typer.Option(None, "--out", help="Write to a file instead of stdout"),
) -> None:
    """Serialise run state as JSON — the contract a dashboard consumes.

    With a run id, exports that run in full. With no argument, exports an index
    of every run. The snapshot carries metadata only: no absolute paths, no
    frames, no secrets, because it is built to leave this machine.
    """
    styles, _, _ = _startup()
    base = Path(runs_dir or styles.output.runs_dir)

    if run_dir is None:
        document = snapshot_mod.build_index(base)
    else:
        run, _ = _load_run_for(run_dir, runs_dir, styles)
        document = snapshot_mod.build_snapshot(run)

    try:
        snapshot_mod.assert_no_local_paths(document)
    except ValueError as exc:
        _fail(str(exc))

    text = json.dumps(document, indent=2)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
        typer.echo(out)
    else:
        typer.echo(text)


@app.command()
def status(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
) -> None:
    """Progress for one run: what has happened, and what has not."""
    styles, weights, _ = _startup()
    base = runs_dir or styles.output.runs_dir
    try:
        run = Run.load(resolve_run(run_dir, Path(base)), echo=False)
    except FileNotFoundError as exc:
        _fail(str(exc))

    m = run.manifest
    extracted = count_frames(run.paths.source_frames)
    restyled = count_frames(run.paths.restyled_frames)
    typer.echo(f"run        {m.run_id}")
    typer.echo(f"created    {m.created_at}")
    typer.echo(f"source     {m.source_path}  ({m.source_width}x{m.source_height})")
    typer.echo(f"style/fps  {m.style} @ {m.fps}fps   backend={m.backend}")
    # T13: the mode decides the target vector AND the retry policy, and an
    # uncalibrated mode cannot spend — so it is stated, with its calibration
    # state, rather than left for the operator to infer (Rule 40).
    calibration = "unknown mode"
    if m.mode in weights.modes:
        cfg = weights.mode(m.mode)
        calibration = "calibrated" if cfg.calibrated else "NOT CALIBRATED — paid runs refused"
    typer.echo(f"mode       {m.mode} ({calibration})")
    typer.echo(
        f"pricing    {m.backend} bills per "
        f"{weights.firewalls.cost.unit_for(m.backend)}"
    )
    typer.echo(f"frames     extracted={extracted}  restyled={restyled}")
    typer.echo(f"audio      {'present' if run.paths.audio.is_file() else 'not extracted'}")
    typer.echo(f"final      {run.paths.final if run.paths.final.is_file() else 'not assembled'}")
    if run.paths.qc_card.is_file():
        card = json.loads(run.paths.qc_card.read_text())
        typer.echo(f"verdict    {card.get('verdict')}")
    # Stated plainly rather than shown as an empty field (Rule 40).
    typer.echo("scoring    live on paid backends; skipped on dummy (D27)")
    # T12 replaced the libass burn-in with a caption track composited into the
    # gap band, so this line was stale — Rule 33 drift, fixed with the feature.
    if run.paths.cues.is_file():
        try:
            cue_count = len(captions.load_cues(run.paths.cues))
            typer.echo(
                f"captions   {cue_count} cues in {run.paths.cues.name} "
                "— rendered into the gap band by `assemble`"
            )
        except captions.CaptionError as exc:
            typer.echo(f"captions   {run.paths.cues.name} is UNREADABLE: {exc}")
    else:
        typer.echo(
            "captions   none (run `claypipe captions`, or hand-author cues.json)"
        )
    references = "none — a paid run will be refused"
    if m.reference_images:
        references = f"{len(m.reference_images)} from {m.reference_origin}"
        if m.reference_origin != "canary":
            references += "  [F1 RISK: approve a canary to re-point them]"
    typer.echo(f"identity   {references}")
    burn = m.burned_in_text
    if burn is None:
        typer.echo("burned-in  not checked (run predates T9b)")
    elif burn.get("detected"):
        ack = "acknowledged" if m.burned_in_acknowledged else "NOT acknowledged"
        typer.echo(
            f"burned-in  {burn['kind']} at rows {burn['band_top']}-"
            f"{burn['band_bottom']} of {burn['frame_height']} "
            f"({burn['band_centre_fraction'] * 100:.0f}% down) — {ack}"
        )
    else:
        typer.echo("burned-in  none detected")
    if run.paths.shot_plan.is_file():
        try:
            plan = shots.ShotPlan.read(run.paths.shot_plan)
            typer.echo(
                f"shots      {len(plan.shots)} shots / {plan.cuts} cuts, "
                f"seeds {plan.shots[0].seed}-{plan.shots[-1].seed}"
            )
        except Exception:
            typer.echo("shots      shots.json is unreadable")
    else:
        typer.echo("shots      not detected yet (batch or canary restyle detects them)")


if __name__ == "__main__":
    app()
