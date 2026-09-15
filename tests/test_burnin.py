"""T9b acceptance — pre-burned caption detection.

A source that already carries burned-in subtitles breaks the format three ways,
and none is visible until the money is spent: the text appears in the original
panel, gets RESTYLED into the comparison panel as clay-textured glyphs, and is
then duplicated by T12's own caption track.

THE POSITION IS NOT ASSUMED. The obvious heuristic looks in the lower third,
where broadcast subtitles live. Measured on the Young Sheldon Shorts rip, the
band peaks at row 327 of 640 — 51% down, dead centre — because short-form
social captions are centred. A lower-third detector reports that clip clean.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from claypipe.pipeline.burnin import (
    BRIGHT,
    CAPTION_MIN_WIDTH_FRACTION,
    DARK,
    MAX_BAND_FRACTION,
    BurnInError,
    BurnInReport,
    _longest_run,
    _outlined_bright,
    detect_burned_in_captions,
)

pytest.importorskip("numpy")


def _clip_with_overlay(
    dst: Path, *, text_rows: range, text_cols: range, frames: int = 48
) -> Path:
    """A synthetic clip carrying an outlined bright bar in a fixed band.

    Imitates what makes real burned-in text detectable — bright fill with a
    dark outline, held in the same rows frame after frame — over moving
    picture content, without shipping any real footage.
    """
    import numpy as np
    from PIL import Image

    work = dst.parent / (dst.stem + "_frames")
    work.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(11)
    for i in range(frames):
        # Moving mid-grey content, so the overlay is the only stable thing.
        frame = np.full((320, 320, 3), 90, dtype=np.uint8)
        frame[:, :, :] = (90 + rng.integers(-25, 25, (320, 320, 1))).clip(0, 255)
        frame[40 + (i % 40) : 120 + (i % 40), 30:200] = 150
        # The overlay: dark outline, bright fill, in glyph-width strokes.
        for row in text_rows:
            for col in range(text_cols.start, text_cols.stop, 12):
                frame[row, col : col + 5] = 250          # fill
                frame[row, col + 5 : col + 8] = 20       # outline
        Image.fromarray(frame).save(work / f"f_{i + 1:05d}.png")

    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-framerate", "12",
         "-i", str(work / "f_%05d.png"),
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4",
         "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(dst)],
        check=True, capture_output=True,
    )
    return dst


@pytest.fixture(scope="module")
def centred_caption_clip(tmp_path_factory) -> Path:
    """Overlay at 50% height, spanning most of the width — the Sheldon shape."""
    out = tmp_path_factory.mktemp("burnin") / "centred.mp4"
    return _clip_with_overlay(out, text_rows=range(150, 172), text_cols=range(60, 260))


@pytest.fixture(scope="module")
def corner_watermark_clip(tmp_path_factory) -> Path:
    """Narrow overlay low and to one side — the TikTok watermark shape."""
    out = tmp_path_factory.mktemp("burnin") / "corner.mp4"
    return _clip_with_overlay(out, text_rows=range(290, 306), text_cols=range(250, 285))


# ---------------------------------------------------------------------------
# The position must be found, not assumed
# ---------------------------------------------------------------------------

def test_a_centred_caption_band_is_found(centred_caption_clip: Path):
    """The case a lower-third heuristic misses."""
    report = detect_burned_in_captions(centred_caption_clip)
    assert report.detected, report.describe()
    assert 0.35 < report.band_centre_fraction < 0.65, report.describe()


def test_a_centred_wide_band_is_called_captions(centred_caption_clip: Path):
    report = detect_burned_in_captions(centred_caption_clip)
    assert report.kind == "captions"
    assert report.band_width / report.frame_width >= CAPTION_MIN_WIDTH_FRACTION


def test_a_narrow_cornered_band_is_called_a_watermark(corner_watermark_clip: Path):
    """Both are burned-in ink that gets restyled, but a garbled watermark and a
    garbled subtitle are different problems, so they are named differently."""
    report = detect_burned_in_captions(corner_watermark_clip)
    assert report.detected, report.describe()
    assert report.kind == "watermark or logo"
    assert report.band_width / report.frame_width < CAPTION_MIN_WIDTH_FRACTION


def test_a_clean_source_stays_quiet(test_clip: Path):
    """The bundled testsrc pattern is full of bright edges. It must NOT fire —
    a detector that flags every textured clip is a detector nobody reads."""
    report = detect_burned_in_captions(test_clip)
    assert not report.detected, report.describe()
    assert report.reason


def test_textured_picture_content_is_rejected_by_band_width(test_clip: Path):
    """The specific way testsrc is rejected: its edges span the whole frame,
    which is picture content, not an overlay."""
    report = detect_burned_in_captions(test_clip)
    span = (report.band_bottom - report.band_top + 1) / max(report.frame_height, 1)
    assert span > MAX_BAND_FRACTION or "too thin" in report.reason


# ---------------------------------------------------------------------------
# The discriminator
# ---------------------------------------------------------------------------

def test_outlined_bright_finds_glyphs_and_not_a_blown_highlight():
    """Brightness alone finds windows and white shirts. Bright ADJACENT TO DARK
    is what a legible overlay always is and picture content usually is not."""
    import numpy as np

    glyph = np.full((1, 4, 40), 120, dtype=np.uint8)
    glyph[0, 1, 10:15] = 250          # bright fill
    glyph[0, 1, 15:18] = 10           # dark outline beside it
    highlight = np.full((1, 4, 40), 120, dtype=np.uint8)
    highlight[0, 1, 5:35] = 250       # a big bright area, no dark beside it

    assert _outlined_bright(glyph).sum() > 0
    assert _outlined_bright(highlight).sum() == 0


def test_longest_run_picks_the_longest_contiguous_band():
    assert _longest_run([False, True, False, True, True, True, False]) == (3, 5)
    assert _longest_run([False, False]) == (-1, -1)
    assert _longest_run([True, True]) == (0, 1)


def test_bright_and_dark_thresholds_do_not_overlap():
    assert DARK < BRIGHT


# ---------------------------------------------------------------------------
# It is advisory, never a block
# ---------------------------------------------------------------------------

def test_intake_warns_but_does_not_block(centred_caption_clip: Path, tmp_path: Path):
    """Advisory: the run is created, and the warning names the consequence.

    Asserted against the RUN LOG rather than stdout — the operator-facing
    warning goes to stderr, which click's runner keeps separate, and the log is
    the durable record a later reviewer actually reads.
    """
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.run import Run

    result = CliRunner().invoke(
        app, ["intake", str(centred_caption_clip), "--style", "clay",
              "--title", "Burned In", "--runs-dir", str(tmp_path / "runs")],
    )
    assert result.exit_code == 0, result.output

    run = Run.load(Path(result.output.strip().splitlines()[-1]), echo=False)
    logged = [json.loads(line) for line in run.paths.log.read_text().splitlines()]
    warning = next(e for e in logged if e["event"] == "intake.burned_in_text")
    assert warning["level"] == "WARN"
    assert warning["detected"] is True
    assert warning["kind"] == "captions"
    assert warning["acknowledged"] is False
    # It says what will actually HAPPEN, not just that something is wrong.
    assert "RESTYLED" in warning["consequence"]
    assert "caption track" in warning["consequence"]


def test_the_flag_records_acknowledgement(centred_caption_clip: Path, tmp_path: Path):
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.run import Run

    runs = tmp_path / "runs"
    result = CliRunner().invoke(
        app, ["intake", str(centred_caption_clip), "--style", "clay",
              "--title", "Acked", "--runs-dir", str(runs),
              "--allow-burned-captions"],
    )
    assert result.exit_code == 0, result.output
    run = Run.load(Path(result.output.strip().splitlines()[-1]), echo=False)
    assert run.manifest.burned_in_text["detected"] is True
    assert run.manifest.burned_in_acknowledged is True

    # Without the flag, detected but NOT acknowledged — the distinction is the
    # whole point of the flag.
    plain = CliRunner().invoke(
        app, ["intake", str(centred_caption_clip), "--style", "clay",
              "--title", "Unacked", "--runs-dir", str(runs)],
    )
    unacked = Run.load(Path(plain.output.strip().splitlines()[-1]), echo=False)
    assert unacked.manifest.burned_in_text["detected"] is True
    assert unacked.manifest.burned_in_acknowledged is False


def test_the_report_is_recorded_on_the_manifest(centred_caption_clip: Path, tmp_path: Path):
    """So a later reviewer can tell a garbled top panel from a backend failure."""
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.run import Run

    result = CliRunner().invoke(
        app, ["intake", str(centred_caption_clip), "--style", "clay",
              "--title", "Recorded", "--runs-dir", str(tmp_path / "runs")],
    )
    run = Run.load(Path(result.output.strip().splitlines()[-1]), echo=False)
    recorded = run.manifest.burned_in_text
    for key in ("detected", "kind", "band_top", "band_bottom", "frame_height",
                "band_centre_fraction", "density", "reason"):
        assert key in recorded, key


def test_a_clean_source_records_a_negative_not_a_null(test_clip: Path, tmp_path: Path):
    """'Checked and clean' and 'never checked' are different states (Rule 40)."""
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.run import Run

    result = CliRunner().invoke(
        app, ["intake", str(test_clip), "--style", "clay", "--title", "Clean",
              "--runs-dir", str(tmp_path / "runs")],
    )
    run = Run.load(Path(result.output.strip().splitlines()[-1]), echo=False)
    assert run.manifest.burned_in_text is not None
    assert run.manifest.burned_in_text["detected"] is False
    assert run.manifest.burned_in_text["reason"]


def test_status_distinguishes_unchecked_from_clean(test_clip: Path, tmp_path: Path):
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.config import load_styles
    from claypipe.run import Run

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy",
        clip_title="Pre T9b", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    result = CliRunner().invoke(
        app, ["status", str(run.paths.root), "--runs-dir", str(tmp_path / "runs")]
    )
    assert "burned-in  not checked" in result.output


def test_a_missing_source_is_a_hard_error(tmp_path: Path):
    with pytest.raises(BurnInError, match="not found"):
        detect_burned_in_captions(tmp_path / "nope.mp4")


# ---------------------------------------------------------------------------
# Calibration against the real clip (skips without it)
# ---------------------------------------------------------------------------

def test_the_sheldon_clip_fires_with_a_centred_band():
    """The measurement this whole design came from: rows 297-342 of 640, 50%
    down the frame, spanning ~35-45% of the width."""
    path = os.environ.get("CLAYPIPE_BURNIN_CLIP")
    if not path or not Path(path).is_file():
        pytest.skip("set CLAYPIPE_BURNIN_CLIP to the Young Sheldon Shorts rip")
    report = detect_burned_in_captions(Path(path))
    assert report.detected, report.describe()
    assert report.kind == "captions", report.describe()
    # Centred, NOT lower-third — the finding that shaped the detector.
    assert 0.40 < report.band_centre_fraction < 0.60, report.describe()


def test_the_reference_panels_report_their_watermark():
    """Clips A and B have no burned-in subtitles in the source plate, but they
    DO carry a TikTok watermark — which is also ink that gets restyled. A true
    positive, correctly named as a watermark rather than as captions."""
    for env_var in ("CLAYPIPE_REFERENCE_CLIP_A", "CLAYPIPE_REFERENCE_CLIP_B"):
        path = os.environ.get(env_var)
        if not path or not Path(path).is_file():
            pytest.skip(f"set {env_var}")
        report = detect_burned_in_captions(Path(path))
        assert report.detected, f"{env_var}: {report.describe()}"
        assert report.kind == "watermark or logo", f"{env_var}: {report.describe()}"
