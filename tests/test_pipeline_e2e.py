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
from tests.conftest import TEST_CLIP

EXPECTED_FPS = 12
EXPECTED_FRAMES = 96  # 8s clip @ 12fps


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
        # T9: the layout engine needs the source's aspect ratio. The test clip
        # is 1280x720, so the derived stack is 316/608/72/608/316.
        source_width=1280,
        source_height=720,
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
            layout=assemble_stage.layout_for_run(run, styles.render),
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


def test_fal_backend_is_unavailable_offline(tmp_path: Path) -> None:
    """Tests must never be able to reach a paid endpoint (Rule 32).

    The fal backend now EXISTS (stubbed), so the refusal moved from
    construction to the call itself: it can be built and inspected, and still
    cannot restyle anything without both --live and an injected client.
    """
    backend = get_backend("fal")
    assert backend.name == "fal" and backend.live is False

    with pytest.raises(NotImplementedError, match="requires --live and FAL_KEY"):
        backend.restyle(
            TEST_CLIP, tmp_path / "out.png", prompt="p", strength=0.6, seed=1
        )

    # --live without a client is still inert: there is nothing to call.
    with pytest.raises(NotImplementedError):
        get_backend("fal", live=True).restyle(
            TEST_CLIP, tmp_path / "out.png", prompt="p", strength=0.6, seed=1
        )

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
        app, ["intake", str(test_clip), "--style", "lego",
              # The per-frame round trip, deliberately on the retired path.
              "--mode", "surface", "--runs-dir", str(run_dir)]
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


# ---------------------------------------------------------------------------
# T9 — the derived layout, asserted on the rendered PIXELS, not on the config.
# ---------------------------------------------------------------------------

EXPECTED_BANDS_16X9 = {
    "top_margin": 316,
    "panel_height": 608,
    "gap_height": 72,
    "bottom_margin": 316,
}


def _background_bands(video: Path, bg_hex: str) -> list[tuple[str, int, int]]:
    """Row runs of one rendered frame, classified as background or content.

    Reads the actual pixels rather than trusting the filtergraph, because the
    failure this guards against — a panel scaled to the wrong height, or an
    overlay landing at the wrong y — produces a file that plays fine and is
    wrong. Only the pixels can tell you.
    """
    import subprocess

    import numpy as np

    width, height = 1080, 1920
    tools = ffmpeg.require_ffmpeg()
    proc = subprocess.run(
        [tools.ffmpeg, "-v", "error", "-ss", "2", "-i", str(video), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
    )
    frame = np.frombuffer(proc.stdout, np.uint8)[: width * height * 3]
    frame = frame.reshape(height, width, 3).astype(int)
    bg = np.array([int(bg_hex[i : i + 2], 16) for i in (1, 3, 5)])
    # h264 at crf 18 moves a flat colour by a few levels; 14 is well inside the
    # gap between "this is the background" and "this is picture".
    is_bg = (np.abs(frame - bg).max(axis=2) < 14).mean(axis=1) > 0.995

    runs: list[tuple[str, int, int]] = []
    y = 0
    while y < height:
        kind = "bg" if is_bg[y] else "content"
        start = y
        while y < height and (("bg" if is_bg[y] else "content") == kind):
            y += 1
        runs.append((kind, start, y - 1))
    return runs


def test_t9_rendered_bands_match_the_derived_layout(test_clip: Path, run_dir: Path) -> None:
    """The T9 acceptance test. A 1280x720 (16:9) source must render as
    316 / 608 / 72 / 608 / 316, measured off the output frame."""
    run, styles = _make_run(test_clip, run_dir)
    audio_md5 = _batch(run, styles, test_clip)
    profile = styles.profile(run.manifest.style)
    assemble_stage.assemble(run, styles.render, profile, audio_md5)

    runs = _background_bands(run.paths.final, profile.background_color)
    # The two panels are the two tallest content bands; the header's lettering
    # is a third, much shorter content band inside the top margin.
    content = sorted(
        ((e - s + 1, s, e) for kind, s, e in runs if kind == "content"), reverse=True
    )
    assert len(content) >= 2, f"expected two panels, got bands {runs}"
    (h_top, top_start, top_end), (h_bot, bot_start, bot_end) = sorted(
        content[:2], key=lambda b: b[1]
    )

    assert h_top == EXPECTED_BANDS_16X9["panel_height"]
    assert h_bot == EXPECTED_BANDS_16X9["panel_height"]
    assert top_start == EXPECTED_BANDS_16X9["top_margin"]
    assert bot_start - top_end - 1 == EXPECTED_BANDS_16X9["gap_height"]
    assert 1920 - 1 - bot_end == EXPECTED_BANDS_16X9["bottom_margin"]
    # And the whole stack closes on the canvas.
    assert (
        top_start
        + h_top
        + EXPECTED_BANDS_16X9["gap_height"]
        + h_bot
        + EXPECTED_BANDS_16X9["bottom_margin"]
        == 1920
    )


def test_t9_header_lettering_sits_inside_the_top_margin(
    test_clip: Path, run_dir: Path
) -> None:
    """The header is the top margin — its type must not spill into the panel."""
    run, styles = _make_run(test_clip, run_dir)
    audio_md5 = _batch(run, styles, test_clip)
    profile = styles.profile(run.manifest.style)
    layout = assemble_stage.layout_for_run(run, styles.render)
    assemble_stage.assemble(run, styles.render, profile, audio_md5)

    runs = _background_bands(run.paths.final, profile.background_color)
    lettering = [
        (s, e) for kind, s, e in runs if kind == "content" and (e - s + 1) < 200
    ]
    assert lettering, f"no header lettering found in {runs}"
    for start, end in lettering:
        assert end < layout.restyled_y, (
            f"header lettering at y={start}-{end} overlaps the restyled panel "
            f"which starts at y={layout.restyled_y}"
        )


def test_t9_layout_is_reported_in_the_verification_record(
    test_clip: Path, run_dir: Path
) -> None:
    """Rule 40: geometry is reported, never implied."""
    run, styles = _make_run(test_clip, run_dir)
    audio_md5 = _batch(run, styles, test_clip)
    result = assemble_stage.assemble(
        run, styles.render, styles.profile(run.manifest.style), audio_md5
    )
    assert result["layout"]["panel_height"] == 608
    assert result["layout"]["top_margin"] == 316
    assert result["layout"]["gap_height"] == 72


def test_t9_pre_t9_run_reprobes_the_source_instead_of_guessing(
    test_clip: Path, run_dir: Path
) -> None:
    """A run.json written before T9 has no source dimensions. Assembly must
    re-probe and warn — never fall back to a canvas-shaped guess, because a
    wrong aspect crops picture away and still produces a plausible video."""
    run, styles = _make_run(test_clip, run_dir)
    run.manifest.source_width = 0
    run.manifest.source_height = 0
    run.save()

    layout = assemble_stage.layout_for_run(run, styles.render)
    assert layout.panel_height == 608  # re-probed 1280x720, not guessed
    logged = [json.loads(line) for line in run.paths.log.read_text().splitlines()]
    assert any(e["event"] == "assemble.layout.reprobed" for e in logged)
