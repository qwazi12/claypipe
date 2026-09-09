"""ClayPipe CLI (SPEC §5).

Build Order step 1 ships the offline path: intake -> batch -> assemble, plus
status. The `canary`, `review` and `--backend fal` commands land in steps 3-5;
until then `batch` is offline-only and cannot spend money.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from . import __version__, ffmpeg
from .config import ConfigError, load_all
from .pipeline import assemble as assemble_stage
from .pipeline import qccard
from .pipeline.extract import count_frames, extract_audio, extract_frames
from .pipeline.restyle import get_backend, restyle_frames
from .pipeline.retry import RetryController
from .pipeline.score import Scorer, ScoringError, load_image
from .pipeline.retry import RunHalted, SpendLedger
from .run import Run, resolve_run
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
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir", help="Override output directory"),
    ref: list[Path] = typer.Option(
        [], "--ref",
        help="Character reference image to lock for identity scoring. Repeatable. "
             "Required for any backend that scores (i.e. anything but dummy).",
    ),
) -> None:
    """STAGE 0 — register a clip and lock its style + character references."""
    styles, _, _ = _startup()
    try:
        styles.profile(style)
    except ConfigError as exc:
        _fail(str(exc))

    try:
        duration = ffmpeg.duration_seconds(video)
        ffmpeg.stream(video, "audio")  # a clip with no audio cannot be assembled
    except ffmpeg.FFmpegError as exc:
        _fail(str(exc))

    for image in ref:
        if not image.is_file():
            _fail(f"reference image not found: {image}")

    run = Run.create(
        source=video,
        style=style,
        fps=fps or styles.render.default_fps,
        backend=backend,
        clip_title=title or video.stem,
        duration_s=duration,
        styles=styles,
        runs_dir=runs_dir,
    )
    if ref:
        run.manifest.reference_images = [str(p.resolve()) for p in ref]
        run.save()
    run.logger.info(
        "intake",
        source=str(video),
        style=style,
        fps=run.manifest.fps,
        backend=backend,
        duration_s=duration,
        references=len(ref),
    )
    typer.echo(run.paths.root)


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
    missing = [str(p) for p in references if not p.is_file()]
    if missing:
        _fail(
            "reference images locked at intake are missing: "
            + ", ".join(missing)
            + "\nRefusing to start a paid run whose identity metric cannot be computed."
        )
    if not references:
        # Fail BEFORE the ledger authorises anything, rather than at frame 1
        # with money already spent (memory.md D12 is still open for `logo`).
        _fail(
            f"backend {backend.name!r} scores every frame, and identity scoring "
            "needs at least one Stage-0 reference image (F weights ID at 0.20 "
            "and the formula is fixed). Re-run intake with --ref <image> ... "
            "before spending."
        )

    try:
        scorer = Scorer.build(weights)
        loaded = [load_image(p) for p in references]
    except ScoringError as exc:
        _fail(str(exc))

    controller = RetryController(
        cfg=weights, paths=run.paths, frame_count=frame_count,
        base_strength=profile.strength, logger=run.logger,
    )
    run.logger.info(
        "batch.scoring.enabled",
        backend=backend.name, references=len(loaded),
        retry_budget=controller.retry_budget,
    )
    return scorer, controller, loaded


@app.command()
def batch(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
    max_cost_usd: Optional[float] = typer.Option(
        None, "--max-cost-usd",
        help="Hard cap on this run's cumulative API spend. Halts before the "
             "call that would breach it.",
    ),
    wait_for_canary: bool = typer.Option(
        False, "--wait-for-canary",
        help="Block and poll for canary_verdict.json instead of failing "
             "immediately when it is absent. Useful when the operator is "
             "reviewing the page in another window.",
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

    # FIREWALL 1: no approved canary verdict -> no spend. Checked before the
    # backend is even constructed, and long before the ledger authorises a call.
    verdict = _require_canary(run, weights, wait=wait_for_canary)

    profile = styles.profile(run.manifest.style)
    prompt = _effective_prompt(run, profile)
    try:
        backend = get_backend(run.manifest.backend)
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
        extracted = extract_frames(source, run.paths.source_frames, run.manifest.fps, run.logger)
        extract_audio(source, run.paths.audio, run.logger)
    except Exception as exc:
        run.logger.error("batch.failed", error=str(exc))
        _fail(str(exc))

    scorer, controller, references = _build_scoring(run, weights, backend, extracted, profile)

    try:
        total = restyle_frames(
            backend=backend,
            source_dir=run.paths.source_frames,
            out_dir=run.paths.restyled_frames,
            prompt=prompt,
            strength=profile.strength,
            logger=run.logger,
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


@app.command()
def status(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
) -> None:
    """Progress for one run: what has happened, and what has not."""
    styles, _, _ = _startup()
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
    typer.echo(f"source     {m.source_path}")
    typer.echo(f"style/fps  {m.style} @ {m.fps}fps   backend={m.backend}")
    typer.echo(f"frames     extracted={extracted}  restyled={restyled}")
    typer.echo(f"audio      {'present' if run.paths.audio.is_file() else 'not extracted'}")
    typer.echo(f"final      {run.paths.final if run.paths.final.is_file() else 'not assembled'}")
    if run.paths.qc_card.is_file():
        card = json.loads(run.paths.qc_card.read_text())
        typer.echo(f"verdict    {card.get('verdict')}")
    # Stated plainly rather than shown as an empty field (Rule 40).
    typer.echo("scoring    not built yet (Build Order step 2)")
    typer.echo("captions   not built yet (burned in automatically once subs.srt exists)")


if __name__ == "__main__":
    app()
