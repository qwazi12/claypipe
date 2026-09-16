"""C4 acceptance — caption-band inpainting on the restyle INPUT only.

Most sources are Shorts and most carry burned-in captions. On the Young Sheldon
test clip 65.5% of frames carry caption ink and the text changes 49 times in
59.3s. Under whole-frame v2v those glyphs are restyled along with everything
else, so the comparison panel fills with melted clay lettering.

The inpaint is deliberately CHEAP. Its output is a control signal for a model
about to restyle the whole frame into clay, so stylisation covers the
artifacts — reaching for a generative fill would add a paid call per frame to
improve something nobody sees.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from claypipe.pipeline.inpaint import (
    BAND_PADDING,
    InpaintError,
    InpaintReport,
    band_for,
    inpaint_band,
    inpaint_frames,
)

pytest.importorskip("cv2", reason="needs the [scoring] extra")


def captioned_frame(size: int = 320, band: range = range(150, 175)) -> np.ndarray:
    """A textured frame with bright outlined lettering in a band."""
    rng = np.random.default_rng(5)
    frame = (rng.integers(60, 120, (size, size, 3))).astype(np.uint8)
    for row in band:
        for col in range(40, 280, 12):
            frame[row, col : col + 5] = 250
            frame[row, col + 5 : col + 8] = 15
    return frame


REPORT = {
    "detected": True, "kind": "captions",
    "band_top": 150, "band_bottom": 174, "frame_height": 320,
}


# ---------------------------------------------------------------------------
# The band must be scaled out of probe space
# ---------------------------------------------------------------------------

def test_the_band_is_scaled_from_probe_space_to_the_frame():
    """T9b probes at a FIXED WIDTH, so its rows are in probe space. Using them
    directly would mask the wrong strip on any frame of a different height —
    which is every frame, since the probe is 640 wide at the source aspect."""
    top, bottom = band_for(REPORT, frame_height=640)   # 2x the probe height
    # 150..174 at 320 becomes 300..348 at 640, then grows by the padding.
    assert top < 300 and bottom > 348


def test_the_band_is_padded_because_outlines_extend_past_the_ink():
    """An un-grown mask leaves a ghost of the text for the model to faithfully
    restyle into clay."""
    top, bottom = band_for(REPORT, frame_height=320)
    detected_height = REPORT["band_bottom"] - REPORT["band_top"] + 1
    assert top <= REPORT["band_top"]
    assert bottom >= REPORT["band_bottom"]
    assert (bottom - top + 1) >= detected_height * (1 + BAND_PADDING)


def test_the_band_never_leaves_the_frame():
    top, bottom = band_for(
        {"band_top": 0, "band_bottom": 319, "frame_height": 320}, frame_height=320
    )
    assert top == 0 and bottom == 319


def test_a_report_without_a_frame_height_is_refused():
    with pytest.raises(InpaintError, match="frame_height"):
        band_for({"band_top": 1, "band_bottom": 2}, frame_height=320)


# ---------------------------------------------------------------------------
# It actually removes the text
# ---------------------------------------------------------------------------

def test_inpainting_removes_the_caption_ink():
    frame = captioned_frame()
    top, bottom = band_for(REPORT, frame_height=320)

    before = float((frame[top : bottom + 1] >= 240).mean())
    after_image = inpaint_band(frame, top, bottom)
    after = float((after_image[top : bottom + 1] >= 240).mean())

    assert before > 0.01, "the fixture has no caption ink to remove"
    assert after < before / 10, f"ink survived: {before:.4f} -> {after:.4f}"


def test_inpainting_leaves_the_rest_of_the_frame_alone():
    """Only the band is touched. Blurring the whole frame would destroy the
    control signal the generator needs."""
    frame = captioned_frame()
    top, bottom = band_for(REPORT, frame_height=320)
    result = inpaint_band(frame, top, bottom)

    assert np.array_equal(result[:top], frame[:top])
    assert np.array_equal(result[bottom + 1 :], frame[bottom + 1 :])


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

@pytest.fixture
def source_frames(tmp_path: Path) -> list[Path]:
    src = tmp_path / "src"
    src.mkdir()
    paths = []
    for i in range(1, 6):
        path = src / f"f_{i:05d}.png"
        Image.fromarray(captioned_frame()).save(path)
        paths.append(path)
    return paths


def test_the_stage_writes_one_output_per_source_frame(source_frames, tmp_path: Path):
    """Frame-for-frame with the source, or every chunk lands on the wrong
    range."""
    out = tmp_path / "input"
    report = inpaint_frames(
        frames=source_frames, out_dir=out, burn_in_report=REPORT
    )
    written = sorted(out.glob("f_*.png"))
    assert len(written) == len(source_frames)
    assert [p.name for p in written] == [p.name for p in source_frames]
    assert report.frames == len(source_frames)


def test_the_stage_is_resume_safe(source_frames, tmp_path: Path):
    out = tmp_path / "input"
    inpaint_frames(frames=source_frames, out_dir=out, burn_in_report=REPORT)
    stamps = {p.name: p.stat().st_mtime_ns for p in out.glob("f_*.png")}
    inpaint_frames(frames=source_frames, out_dir=out, burn_in_report=REPORT)
    assert {p.name: p.stat().st_mtime_ns for p in out.glob("f_*.png")} == stamps


def test_it_refuses_to_blur_a_band_for_no_reason(source_frames, tmp_path: Path):
    """A clean source has nothing to inpaint, and smearing a strip of it would
    degrade the control signal for nothing."""
    with pytest.raises(InpaintError, match="nothing"):
        inpaint_frames(
            frames=source_frames, out_dir=tmp_path / "x",
            burn_in_report={"detected": False},
        )


def test_no_frames_is_a_hard_error(tmp_path: Path):
    with pytest.raises(InpaintError, match="no frames"):
        inpaint_frames(frames=[], out_dir=tmp_path / "x", burn_in_report=REPORT)


def test_the_report_states_what_was_and_was_not_touched():
    report = InpaintReport(frames=10, band_top=5, band_bottom=15, frame_height=100)
    note = report.as_dict()["note"]
    assert "RESTYLE INPUT only" in note
    assert "original panel keeps" in note
    assert "audio is untouched" in note


# ---------------------------------------------------------------------------
# Wired into the v2v path
# ---------------------------------------------------------------------------

def test_the_generator_reads_the_inpainted_frames_not_the_source():
    import inspect

    from claypipe import cli

    source = inspect.getsource(cli._run_v2v)
    assert "control_frames = frame_paths(run.paths.restyle_input_frames)" in source
    # The chunk window must come from the CONTROL frames.
    assert "window = control_frames[" in source


def test_a_clean_source_feeds_the_generator_the_source_frames():
    import inspect

    from claypipe import cli

    source = inspect.getsource(cli._run_v2v)
    assert "control_frames = sources" in source


def test_the_original_panel_is_never_inpainted():
    """The bottom panel keeps its own captions — they are part of what the
    viewer is comparing against. Assembly reads the SOURCE VIDEO for it, not
    any frame directory, so this is structural."""
    import inspect

    from claypipe.pipeline import assemble

    source = inspect.getsource(assemble.assemble)
    assert "restyle_input" not in source
    assert "source_video=Path(run.manifest.source_path)" in source
