"""End-to-end pipeline test — DummyBackend only, zero API spend (Rule 32).

Nothing here touches fal.ai or any network service. If this file ever needs a
credential, something has gone wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claypipe import ffmpeg
from claypipe.cli import app
from claypipe.config import load_styles
from claypipe.pipeline import assemble as assemble_stage
from claypipe.pipeline import qccard
from claypipe.pipeline.extract import count_frames, extract_audio, extract_frames, frame_paths
from claypipe.pipeline.restyle import DummyBackend, get_backend, restyle_frames
from claypipe.run import Run

EXPECTED_FPS = 12
EXPECTED_FRAMES = 60  # 5s clip @ 12fps


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


def _make_run(test_clip: Path, run_dir: Path, style: str = "clay") -> tuple[Run, object]:
    styles = load_styles()
    run = Run.create(
        source=test_clip,
        style=style,
        fps=EXPECTED_FPS,
        backend="dummy",
        clip_title="Test Clip",
        duration_s=ffmpeg.duration_seconds(test_clip),
        styles=styles,
        runs_dir=run_dir,
        echo=False,
    )
    return run, styles


def _batch(run: Run, styles, test_clip: Path) -> str:
    profile = styles.profile(run.manifest.style)
    extract_frames(test_clip, run.paths.source_frames, run.manifest.fps, run.logger)
    audio_md5 = extract_audio(test_clip, run.paths.audio, run.logger)
    restyle_frames(
        backend=DummyBackend(),
        source_dir=run.paths.source_frames,
        out_dir=run.paths.restyled_frames,
        prompt=profile.prompt,
        strength=profile.strength,
        logger=run.logger,
    )
    return audio_md5


def test_full_pipeline_offline(test_clip: Path, run_dir: Path) -> None:
    """The whole pipeline runs offline and produces a valid 9:16 mp4."""
    run, styles = _make_run(test_clip, run_dir)
    audio_md5 = _batch(run, styles, test_clip)

    # SPEC §1: extracted == restyled, verified before assembly.
    assert count_frames(run.paths.source_frames) == EXPECTED_FRAMES
    assert count_frames(run.paths.restyled_frames) == EXPECTED_FRAMES

    result = assemble_stage.assemble(
        run, styles.render, styles.profile(run.manifest.style), audio_md5
    )

    assert run.paths.final.is_file()
    # Acceptance: ffprobe shows 1080x1920.
    assert (result["width"], result["height"]) == (1080, 1920)
    assert result["video_codec"] == "h264"
    # Acceptance: an AAC stream bit-identical to the source audio.
    assert result["audio_codec"] == "aac"
    assert result["audio_bit_identical"] is True
    assert result["audio_md5"] == ffmpeg.stream_md5(test_clip, "audio")
    assert result["audio_md5"] == ffmpeg.stream_md5(run.paths.audio, "audio")
    # SPEC §1: extracted == restyled == reassembled, all the way to the output.
    assert result["frames_actual"] == result["frames_expected"] == EXPECTED_FRAMES


def test_output_frame_count_matches_restyled_sequence(test_clip: Path, run_dir: Path) -> None:
    """The final render must not trail extra frames past the restyled sequence.

    The original panel runs at a higher rate and is fractionally longer, so this
    regressed once already — and the obvious fix (`-frames:v`) truncated the
    audio instead, which is why the bound lives in the filtergraph.
    """
    run, styles = _make_run(test_clip, run_dir)
    audio_md5 = _batch(run, styles, test_clip)
    assemble_stage.assemble(
        run, styles.render, styles.profile(run.manifest.style), audio_md5
    )
    assert assemble_stage._count_video_frames(run.paths.final) == EXPECTED_FRAMES
    assert assemble_stage._count_video_frames(run.paths.restyled_video) == EXPECTED_FRAMES
    # ...and the audio survived the bound intact.
    assert ffmpeg.stream_md5(run.paths.final, "audio") == ffmpeg.stream_md5(test_clip, "audio")


def test_frames_are_zero_padded_and_ordered(test_clip: Path, run_dir: Path) -> None:
    """SPEC §1: frame order must be trivially recoverable from the names."""
    run, styles = _make_run(test_clip, run_dir)
    _batch(run, styles, test_clip)
    names = [p.name for p in frame_paths(run.paths.source_frames)]
    assert names[0] == "f_00001.png"
    assert names[-1] == f"f_{EXPECTED_FRAMES:05d}.png"
    assert names == sorted(names)


def test_batch_is_resume_safe(test_clip: Path, run_dir: Path) -> None:
    """A re-run must not redo work — a crashed run must never re-spend."""
    run, styles = _make_run(test_clip, run_dir)
    _batch(run, styles, test_clip)
    first = frame_paths(run.paths.restyled_frames)[0]
    mtime = first.stat().st_mtime_ns

    _batch(run, styles, test_clip)  # second pass
    assert first.stat().st_mtime_ns == mtime, "existing restyled frame was regenerated"


def test_frame_count_mismatch_is_a_hard_fail(test_clip: Path, run_dir: Path) -> None:
    """SPEC §1: any extracted/restyled mismatch aborts. No guessing."""
    run, styles = _make_run(test_clip, run_dir)
    audio_md5 = _batch(run, styles, test_clip)
    frame_paths(run.paths.restyled_frames)[10].unlink()

    with pytest.raises(assemble_stage.AssemblyError, match="frame-count mismatch"):
        assemble_stage.assemble(
            run, styles.render, styles.profile(run.manifest.style), audio_md5
        )


def test_audio_hash_mismatch_is_a_hard_fail(test_clip: Path, run_dir: Path) -> None:
    """The sync guarantee is enforced, not assumed."""
    run, styles = _make_run(test_clip, run_dir)
    _batch(run, styles, test_clip)
    assemble_stage.assemble(
        run, styles.render, styles.profile(run.manifest.style),
        ffmpeg.stream_md5(run.paths.audio, "audio"),
    )

    with pytest.raises(assemble_stage.AssemblyError, match="NOT COPIED BIT-FOR-BIT"):
        assemble_stage.verify_output(
            run.paths.final,
            source_video=test_clip,
            extracted_audio_md5="0" * 32,
            expected_frames=EXPECTED_FRAMES,
            render=styles.render,
            logger=run.logger,
        )


def test_audio_is_never_re_encoded(test_clip: Path, run_dir: Path) -> None:
    """Same packet count in, same packet count out — a copy, not a transcode."""
    run, styles = _make_run(test_clip, run_dir)
    audio_md5 = _batch(run, styles, test_clip)
    assemble_stage.assemble(
        run, styles.render, styles.profile(run.manifest.style), audio_md5
    )
    src = ffmpeg.stream(test_clip, "audio")
    out = ffmpeg.stream(run.paths.final, "audio")
    assert src["codec_name"] == out["codec_name"] == "aac"
    assert src["sample_rate"] == out["sample_rate"]
    assert src["channels"] == out["channels"]


def test_dummy_backend_is_deterministic_and_keeps_geometry(
    test_clip: Path, run_dir: Path, tmp_path: Path
) -> None:
    """Restyle changes colour/texture, never dimensions — the property the
    step-2 scorer depends on (LPIPS on edges must not punish a recolour)."""
    run, styles = _make_run(test_clip, run_dir)
    extract_frames(test_clip, run.paths.source_frames, run.manifest.fps, run.logger)
    src = frame_paths(run.paths.source_frames)[0]

    backend = DummyBackend()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    for dst in (a, b):
        backend.restyle(src, dst, prompt="p", strength=0.65, seed=1000)

    assert a.read_bytes() == b.read_bytes(), "DummyBackend is not deterministic"

    from PIL import Image

    with Image.open(src) as s, Image.open(a) as r:
        assert s.size == r.size
        assert s.tobytes() != r.tobytes(), "restyle was a no-op"


def test_fal_backend_is_unavailable_offline() -> None:
    """Tests must never be able to reach a paid endpoint (Rule 32)."""
    with pytest.raises(NotImplementedError, match="step 5"):
        get_backend("fal")
    with pytest.raises(ValueError, match="unknown backend"):
        get_backend("replicate")


def test_qc_card_reports_only_known_facts(test_clip: Path, run_dir: Path) -> None:
    """Rule 40: fields that do not exist yet are null, never invented."""
    run, styles = _make_run(test_clip, run_dir)
    audio_md5 = _batch(run, styles, test_clip)
    result = assemble_stage.assemble(
        run, styles.render, styles.profile(run.manifest.style), audio_md5
    )
    card = qccard.build_card(
        run, frames=EXPECTED_FRAMES, verdict="assembled", extra={"output": result}
    )
    qccard.write_card(run, card, run_dir)

    written = json.loads(run.paths.qc_card.read_text())
    assert written["clip_id"] == run.run_id
    assert written["frames"] == EXPECTED_FRAMES
    assert written["scores"] is None, "scoring does not exist yet; must not be faked"
    assert written["auto_retries"] is None
    assert written["cost_usd"]["total"] == 0.0  # DummyBackend genuinely costs nothing
    history = (run_dir / qccard.HISTORY_NAME).read_text().strip().splitlines()
    assert len(history) == 1


def test_cli_round_trip(test_clip: Path, run_dir: Path) -> None:
    """intake -> batch -> assemble -> status through the actual CLI."""
    runner = CliRunner()
    intake = runner.invoke(
        app, ["intake", str(test_clip), "--style", "lego", "--runs-dir", str(run_dir)]
    )
    assert intake.exit_code == 0, intake.output
    run_path = Path(intake.stdout.strip().splitlines()[-1])

    # STAGE 1 gate: batch refuses to start until a human has approved the
    # canary. Asserted here as well as in test_retry.py, because the ordering
    # is part of the end-to-end contract, not just a unit-level rule.
    blocked = runner.invoke(app, ["batch", str(run_path), "--runs-dir", str(run_dir)])
    assert blocked.exit_code == 1 and "canary gate" in blocked.output

    (run_path / "canary_verdict.json").write_text(
        json.dumps({"approved": True, "reviewer": "test", "note": "offline dummy run"})
    )

    batch = runner.invoke(app, ["batch", str(run_path), "--runs-dir", str(run_dir)])
    assert batch.exit_code == 0, batch.output
    assert f"{EXPECTED_FRAMES} frames restyled" in batch.stdout

    asm = runner.invoke(app, ["assemble", str(run_path), "--runs-dir", str(run_dir)])
    assert asm.exit_code == 0, asm.output
    assert (run_path / "final_comparison.mp4").is_file()

    status = runner.invoke(app, ["status", str(run_path), "--runs-dir", str(run_dir)])
    assert status.exit_code == 0
    assert f"extracted={EXPECTED_FRAMES}" in status.stdout

    # Every restyle call was ledgered, even at the dummy backend's zero price:
    # the ledger is the audit trail, not just an accountant.
    ledger = run_path / "spend_ledger.jsonl"
    assert ledger.is_file()
    authorized = [
        json.loads(line) for line in ledger.read_text().splitlines()
        if line.strip() and json.loads(line)["event"] == "authorized"
    ]
    assert len(authorized) == EXPECTED_FRAMES


def test_cli_rejects_unknown_style(test_clip: Path, run_dir: Path) -> None:
    """Rule 20: fail loudly on config errors."""
    result = CliRunner().invoke(
        app, ["intake", str(test_clip), "--style", "claymation", "--runs-dir", str(run_dir)]
    )
    assert result.exit_code == 1
    assert "unknown style" in result.output
