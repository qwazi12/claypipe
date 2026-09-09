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
from .pipeline.retry import RunHalted, SpendLedger, require_canary_approval
from .run import Run, resolve_run

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
    backend: str = typer.Option("dummy", "--backend", help="Restyle backend: dummy"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir", help="Override output directory"),
) -> None:
    """STAGE 0 — register a clip and create its run directory."""
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
    run.logger.info(
        "intake",
        source=str(video),
        style=style,
        fps=run.manifest.fps,
        backend=backend,
        duration_s=duration,
    )
    typer.echo(run.paths.root)


@app.command()
def batch(
    run_dir: Path = typer.Argument(..., help="Run directory or run id"),
    runs_dir: Optional[Path] = typer.Option(None, "--runs-dir"),
    max_cost_usd: Optional[float] = typer.Option(
        None, "--max-cost-usd",
        help="Hard cap on this run's cumulative API spend. Halts before the "
             "call that would breach it.",
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
    # backend is even constructed.
    try:
        require_canary_approval(run.paths, run.logger)
    except RunHalted as exc:
        run.logger.error("batch.blocked", breach=exc.breach.value, error=str(exc))
        _fail(str(exc))

    profile = styles.profile(run.manifest.style)
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
        total = restyle_frames(
            backend=backend,
            source_dir=run.paths.source_frames,
            out_dir=run.paths.restyled_frames,
            prompt=profile.prompt,
            strength=profile.strength,
            logger=run.logger,
            ledger=ledger,
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
    typer.echo(f"{extracted} frames restyled with backend '{backend.name}'")


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
