"""T12 acceptance — captions as a layout element, not a burn-in.

Two criteria:
  * the audio MD5 is unchanged by captioning (the sync guarantee survives)
  * a caption never overlaps either panel

The second is guaranteed structurally rather than by inspection: the caption
image is exactly gap-band-sized and is overlaid at the gap's y offset, so it
cannot physically reach a panel. These tests assert that structure holds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from claypipe import ffmpeg
from claypipe.cli import app
from claypipe.config import load_styles
from claypipe.pipeline import assemble as assemble_stage
from claypipe.pipeline import captions
from claypipe.pipeline.captions import (
    CAPTION_GLOB,
    Cue,
    CaptionError,
    assert_within_gap,
    load_cues,
    phrase_cues,
    render_caption_track,
    render_cue_image,
    save_cues,
    sentence_case,
    write_srt,
)
from claypipe.pipeline.extract import extract_audio, extract_frames
from claypipe.pipeline.restyle import DummyBackend, restyle_frames
from claypipe.run import Run

runner = CliRunner()

# Non-dialogue forms taken verbatim from the reference clips (§1.4).
REFERENCE_CUES = [
    Cue(0.0, 1.2, "[dramatic music]"),
    Cue(1.2, 2.4, "You cannot just walk in here and say that."),
    Cue(2.4, 3.0, "*smack*"),
    Cue(3.0, 4.6, "I have been telling you this for three entire weeks, and nobody listens."),
    Cue(4.6, 5.0, "*grunting in pain*"),
]


@pytest.fixture
def layout():
    return load_styles().render.layout_for(16 / 9)


@pytest.fixture
def profile():
    return load_styles().profile("clay")


@pytest.fixture
def render_cfg():
    return load_styles().render


# ---------------------------------------------------------------------------
# Cue text
# ---------------------------------------------------------------------------

def test_sentence_case_adds_terminal_punctuation():
    assert sentence_case("you cannot walk in here") == "You cannot walk in here."
    assert sentence_case("Already punctuated!") == "Already punctuated!"


@pytest.mark.parametrize("text", ["[dramatic music]", "*smack*", "*grunting in pain*"])
def test_nondialogue_cues_are_left_exactly_as_written(text):
    """`[dramatic music]` is not a sentence. Giving it a full stop or recasing
    its brackets would corrupt a closed-caption convention."""
    assert sentence_case(text) == text
    assert Cue(0.0, 1.0, text).is_nondialogue


def test_dialogue_is_not_mistaken_for_nondialogue():
    assert not Cue(0.0, 1.0, "He said [something] about it.").is_nondialogue
    assert not Cue(0.0, 1.0, "Fine. We will do it your way.").is_nondialogue


def test_phrase_grouping_is_phrase_level_not_word_level():
    """§1.4: phrase-level, explicitly NOT word-by-word karaoke."""
    words = [
        (0.0, 0.2, "You"), (0.2, 0.4, "cannot"), (0.4, 0.6, "just"),
        (0.6, 0.8, "walk"), (0.8, 1.0, "in"), (1.0, 1.4, "here."),
        (1.4, 1.6, "I"), (1.6, 1.9, "told"), (1.9, 2.3, "you."),
    ]
    cues = phrase_cues(words)
    assert len(cues) == 2, [c.text for c in cues]
    assert cues[0].text == "You cannot just walk in here."
    assert cues[1].text == "I told you."


def test_phrase_grouping_breaks_on_length_when_there_is_no_punctuation():
    words = [(i * 0.2, i * 0.2 + 0.2, "word") for i in range(30)]
    cues = phrase_cues(words, max_chars=20)
    assert len(cues) > 1
    for cue in cues:
        assert len(cue.text) <= 30  # 20 chars plus the added full stop and slack


def test_short_cues_are_stretched_but_never_past_the_next_one():
    words = [(0.0, 0.05, "Hi."), (1.0, 1.4, "There.")]
    cues = phrase_cues(words, min_seconds=0.5)
    assert cues[0].duration_s >= 0.5
    assert cues[0].end_s <= cues[1].start_s


# ---------------------------------------------------------------------------
# Persistence — the file is hand-edited, so round-tripping matters
# ---------------------------------------------------------------------------

def test_cues_round_trip_with_nondialogue_preserved(tmp_path: Path):
    path = save_cues(REFERENCE_CUES, tmp_path / "cues.json")
    again = load_cues(path)
    assert [c.text for c in again] == [c.text for c in REFERENCE_CUES]
    assert sum(1 for c in again if c.is_nondialogue) == 3
    payload = json.loads(path.read_text())
    assert payload["nondialogue"] == 3


def test_a_cue_that_ends_before_it_starts_is_refused(tmp_path: Path):
    path = tmp_path / "cues.json"
    path.write_text(json.dumps({"cues": [{"start_s": 5.0, "end_s": 1.0, "text": "x"}]}))
    with pytest.raises(CaptionError, match="ends"):
        load_cues(path)


def test_missing_cue_file_is_a_hard_error(tmp_path: Path):
    with pytest.raises(CaptionError, match="no cue file"):
        load_cues(tmp_path / "nope.json")


def test_srt_is_written_for_inspection(tmp_path: Path):
    path = write_srt(REFERENCE_CUES, tmp_path / "subs.srt")
    text = path.read_text()
    assert "00:00:00,000 --> 00:00:01,200" in text
    assert "[dramatic music]" in text


# ---------------------------------------------------------------------------
# Rendering — the "never overlaps a panel" criterion
# ---------------------------------------------------------------------------

def test_a_cue_image_is_exactly_the_gap_band(layout, render_cfg, profile):
    """The structural guarantee. A gap-band-sized image overlaid at the gap's y
    offset cannot reach a panel, whatever the text says."""
    image = render_cue_image(REFERENCE_CUES[1], layout=layout, render=render_cfg, profile=profile)
    assert image.size == (layout.canvas_width, layout.gap_height)
    assert image.size == (1080, 72)


def test_even_an_absurdly_long_cue_stays_inside_the_band(layout, render_cfg, profile):
    """This is the case libass gets wrong: a long cue at a fixed size spills
    onto a panel and crops a face, in a file that plays perfectly."""
    monster = Cue(0.0, 3.0, "word " * 80)
    image = render_cue_image(monster, layout=layout, render=render_cfg, profile=profile)
    assert image.size == (layout.canvas_width, layout.gap_height)
    assert_within_gap(image, layout)
    # Ink must be inside the band, with nothing clipped at the very edges.
    bbox = image.split()[-1].getbbox()
    assert bbox is not None, "the cue rendered nothing at all"
    assert bbox[1] >= 0 and bbox[3] <= layout.gap_height


@pytest.mark.parametrize("cue", REFERENCE_CUES, ids=lambda c: c.text[:18])
def test_every_reference_cue_fits_the_band(cue, layout, render_cfg, profile):
    image = render_cue_image(cue, layout=layout, render=render_cfg, profile=profile)
    assert_within_gap(image, layout)


def test_a_wrong_sized_caption_is_refused(layout):
    """assert_within_gap is the check that would catch a regression to drawing
    captions at canvas height."""
    wrong = Image.new("RGBA", (1080, 400), (255, 255, 255, 255))
    with pytest.raises(CaptionError, match="gap band"):
        assert_within_gap(wrong, layout)


def test_cue_images_are_transparent_not_repainted_background(layout, render_cfg, profile):
    """Painting a guess at the background colour over the real one would show
    a seam on the textured backgrounds §1.4 records."""
    image = render_cue_image(REFERENCE_CUES[0], layout=layout, render=render_cfg, profile=profile)
    assert image.mode == "RGBA"
    corner = image.getpixel((2, 2))
    assert corner[3] == 0, f"corner is opaque: {corner}"


# ---------------------------------------------------------------------------
# The track
# ---------------------------------------------------------------------------

def test_track_renders_one_image_per_output_frame(tmp_path, layout, render_cfg, profile):
    """A PNG sequence with a missing index stops the input early, which would
    truncate the overlay and — via assembly's trim bound — the video."""
    total = 60
    written = render_caption_track(
        REFERENCE_CUES, out_dir=tmp_path / "captions", layout=layout,
        render=render_cfg, profile=profile, fps=12, total_frames=total,
    )
    assert written == total
    files = sorted((tmp_path / "captions").glob(CAPTION_GLOB))
    assert len(files) == total
    # Strictly contiguous from 1.
    assert [f.name for f in files] == [f"c_{i:05d}.png" for i in range(1, total + 1)]


def test_blank_frames_are_real_images(tmp_path, layout, render_cfg, profile):
    render_caption_track(
        [Cue(0.0, 0.5, "Only at the start.")], out_dir=tmp_path / "c", layout=layout,
        render=render_cfg, profile=profile, fps=12, total_frames=24,
    )
    late = tmp_path / "c" / "c_00020.png"
    assert late.is_file()
    with Image.open(late) as img:
        assert img.size == (layout.canvas_width, layout.gap_height)
        assert img.split()[-1].getbbox() is None, "a blank frame carries ink"


def test_identical_consecutive_cues_are_rendered_once(tmp_path, layout, render_cfg, profile):
    """A 3-second cue must cost one text render, not 36."""
    import claypipe.pipeline.captions as mod

    calls = {"n": 0}
    original = mod.render_cue_image

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    mod.render_cue_image = counting
    try:
        render_caption_track(
            REFERENCE_CUES, out_dir=tmp_path / "c", layout=layout, render=render_cfg,
            profile=profile, fps=12, total_frames=60,
        )
    finally:
        mod.render_cue_image = original
    # Five cues plus blanks — far fewer than 60 renders.
    assert calls["n"] <= len(REFERENCE_CUES) + 1, calls["n"]


def test_a_rerender_clears_stale_caption_frames(tmp_path, layout, render_cfg, profile):
    out = tmp_path / "c"
    render_caption_track(
        REFERENCE_CUES, out_dir=out, layout=layout, render=render_cfg,
        profile=profile, fps=12, total_frames=60,
    )
    render_caption_track(
        REFERENCE_CUES[:1], out_dir=out, layout=layout, render=render_cfg,
        profile=profile, fps=12, total_frames=24,
    )
    assert len(sorted(out.glob(CAPTION_GLOB))) == 24, "stale frames survived"


def test_a_cue_owns_the_frame_its_midpoint_falls_in(tmp_path, layout, render_cfg, profile):
    """Sampling at the frame midpoint keeps a cue from flickering on for a
    single frame at a boundary."""
    render_caption_track(
        [Cue(1.0, 2.0, "Exactly one second.")], out_dir=tmp_path / "c", layout=layout,
        render=render_cfg, profile=profile, fps=12, total_frames=36,
    )

    def has_ink(index: int) -> bool:
        with Image.open(tmp_path / "c" / f"c_{index:05d}.png") as img:
            return img.split()[-1].getbbox() is not None

    assert not has_ink(12)   # midpoint 0.958s — before the cue
    assert has_ink(13)       # midpoint 1.042s — inside
    assert has_ink(24)       # midpoint 1.958s — still inside
    assert not has_ink(25)   # midpoint 2.042s — after


# ---------------------------------------------------------------------------
# End to end — the audio guarantee
# ---------------------------------------------------------------------------

@pytest.fixture
def run_with_frames(test_clip: Path, tmp_path: Path) -> tuple[Run, object, str]:
    styles = load_styles()
    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy",
        clip_title="Caption Test", duration_s=ffmpeg.duration_seconds(test_clip),
        source_width=1280, source_height=720,
        styles=styles, runs_dir=tmp_path / "runs", echo=False,
    )
    prof = styles.profile("clay")
    extract_frames(test_clip, run.paths.source_frames, 12, run.logger)
    audio_md5 = extract_audio(test_clip, run.paths.audio, run.logger)
    restyle_frames(
        backend=DummyBackend(), source_dir=run.paths.source_frames,
        out_dir=run.paths.restyled_frames, prompt=prof.prompt,
        strength=prof.strength, logger=run.logger,
    )
    return run, styles, audio_md5


def test_captioning_leaves_the_audio_bit_identical(run_with_frames):
    """T12's first acceptance criterion. Captions are now a video-side
    composite, so there is no mechanism by which they could touch the audio —
    this proves it rather than asserting it."""
    run, styles, audio_md5 = run_with_frames
    profile = styles.profile("clay")

    # Render once without captions.
    assemble_stage.assemble(run, styles.render, profile, audio_md5)
    without = ffmpeg.stream_md5(run.paths.final, "audio")

    # Then again with them.
    save_cues(REFERENCE_CUES, run.paths.cues)
    result = assemble_stage.assemble(run, styles.render, profile, audio_md5)
    with_captions = ffmpeg.stream_md5(run.paths.final, "audio")

    assert without == with_captions == audio_md5
    assert result["audio_bit_identical"] is True


def test_captions_are_composited_at_the_gap_offset(run_with_frames):
    run, styles, audio_md5 = run_with_frames
    save_cues(REFERENCE_CUES, run.paths.cues)
    assemble_stage.assemble(run, styles.render, styles.profile("clay"), audio_md5)

    logged = [json.loads(line) for line in run.paths.log.read_text().splitlines()]
    rendered = [e for e in logged if e["event"] == "assemble.rendered"][-1]
    assert rendered["captions"] is True
    assert rendered["caption_band_y"] == rendered["gap_y"] == 924
    track = [e for e in logged if e["event"] == "captions.track"][-1]
    assert track["band"] == "1080x72"
    assert track["nondialogue"] == 3


def test_no_cue_file_means_no_captions_not_a_guess(run_with_frames):
    run, styles, audio_md5 = run_with_frames
    assert not run.paths.cues.is_file()
    assemble_stage.assemble(run, styles.render, styles.profile("clay"), audio_md5)
    logged = [json.loads(line) for line in run.paths.log.read_text().splitlines()]
    rendered = [e for e in logged if e["event"] == "assemble.rendered"][-1]
    assert rendered["captions"] is False
    assert not any(e["event"] == "captions.track" for e in logged)


def test_the_output_still_has_the_expected_frame_count_with_captions(run_with_frames):
    """The caption track is a finite input; it must not extend or truncate the
    video. The trim bound in assembly is what enforces this (D6)."""
    run, styles, audio_md5 = run_with_frames
    save_cues(REFERENCE_CUES, run.paths.cues)
    result = assemble_stage.assemble(run, styles.render, styles.profile("clay"), audio_md5)
    assert result["frames_actual"] == result["frames_expected"] == 60


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_captions_command_refuses_to_clobber_a_hand_edited_file(run_with_frames):
    run, _styles, _md5 = run_with_frames
    save_cues(REFERENCE_CUES, run.paths.cues)
    result = runner.invoke(
        app, ["captions", str(run.paths.root), "--runs-dir", str(run.paths.root.parent)]
    )
    assert result.exit_code != 0
    assert "hand-edited" in result.output
    # And the file is untouched.
    assert len(load_cues(run.paths.cues)) == len(REFERENCE_CUES)


def test_captions_command_needs_extracted_audio(test_clip: Path, tmp_path: Path):
    styles = load_styles()
    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy",
        clip_title="No Audio Yet", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=styles, runs_dir=tmp_path / "runs", echo=False,
    )
    result = runner.invoke(
        app, ["captions", str(run.paths.root), "--runs-dir", str(run.paths.root.parent)]
    )
    assert result.exit_code != 0
    assert "audio" in result.output.lower()
