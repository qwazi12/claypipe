"""Captions as a LAYOUT ELEMENT (MASTER_PLAN §1.4, T12).

The reference clips put captions in the band BETWEEN the two panels — never
over the picture. That is a layout decision, not a subtitle style, and it is
why this module exists instead of an `-vf subtitles=` flag:

  * libass draws over the composited frame. It has no idea the gap band exists,
    so a two-line cue at a large size spills onto a panel and crops a face.
    Nothing downstream catches it, because the file plays fine.
  * The gap is ~72px of a 1920px canvas. Fitting text to it is a measured
    constraint — shrink-to-fit against a known box — and PIL can measure. A
    subtitle filter cannot be asked "did that fit".
  * The header already uses the PIL overlay path (D1). Captions joining it
    means ONE text renderer for the whole canvas, one font resolution path,
    one set of fallbacks.

The output is a PNG SEQUENCE, one image per output frame, composited as a
single finite input at the gap's y offset. Not one overlay per cue: that would
be dozens of inputs and dozens of `enable=between(...)` filters, and every
still input risks the unbounded-input trap that D5 records. A finite sequence
at the run's own fps is bounded by construction.

Transcription is faster-whisper behind an optional extra (D3). Non-dialogue
cues (`[dramatic music]`, `*crunch*`) are PRESERVED and rendered but never
INVENTED — Whisper transcribes speech; labelling a sound effect needs an audio
event classifier, which is not in this pipeline. An operator-authored or
hand-edited cue file carries them through untouched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import RenderConfig, StyleProfile, hex_to_rgb
from ..logging import RunLogger
from .layout import Layout

CUES_NAME = "cues.json"
CAPTION_FRAME_PATTERN = "c_%05d.png"
CAPTION_GLOB = "c_*.png"

# Non-dialogue cue shapes from the reference clips: [music], *sfx*, *action*.
NONDIALOGUE_RE = re.compile(r"^\s*(\[.+\]|\*.+\*)\s*$")

# A phrase breaks at sentence-final punctuation, or when it would outgrow this
# many characters. Measured target is phrase-level, NOT word-by-word karaoke.
MAX_PHRASE_CHARS = 42
# A cue shorter than this is a flash the eye cannot read.
MIN_CUE_SECONDS = 0.5


class CaptionError(RuntimeError):
    """Captioning could not run, or produced something unrenderable."""


@dataclass(frozen=True)
class Cue:
    """One caption, in seconds on the source timeline."""

    start_s: float
    end_s: float
    text: str

    @property
    def is_nondialogue(self) -> bool:
        return bool(NONDIALOGUE_RE.match(self.text))

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def as_dict(self) -> dict:
        return {
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
            "text": self.text,
            "nondialogue": self.is_nondialogue,
        }


def sentence_case(text: str) -> str:
    """Sentence case with terminal punctuation — the measured caption style.

    Non-dialogue cues are left EXACTLY as written: `[dramatic music]` is not a
    sentence and must not be given a full stop or have its brackets recased.
    """
    stripped = text.strip()
    if not stripped or NONDIALOGUE_RE.match(stripped):
        return stripped
    # Whisper returns capitalised, punctuated text already; this normalises the
    # cases where it does not, without destroying proper nouns mid-sentence.
    out = stripped[0].upper() + stripped[1:]
    if out[-1] not in ".?!,:;-—":
        out += "."
    return out


def phrase_cues(
    words: list[tuple[float, float, str]],
    *,
    max_chars: int = MAX_PHRASE_CHARS,
    min_seconds: float = MIN_CUE_SECONDS,
) -> list[Cue]:
    """Group timed words into phrase-level cues.

    `words` is (start_s, end_s, word). Breaks at sentence-final punctuation
    first, then on length. Word-by-word karaoke is explicitly NOT the target
    (§1.4), so the grouping is deliberately coarse.
    """
    cues: list[Cue] = []
    buffer: list[tuple[float, float, str]] = []

    def flush() -> None:
        if not buffer:
            return
        text = sentence_case(" ".join(w for _s, _e, w in buffer).strip())
        if text:
            cues.append(Cue(start_s=buffer[0][0], end_s=buffer[-1][1], text=text))
        buffer.clear()

    for start, end, word in words:
        buffer.append((start, end, word.strip()))
        joined = " ".join(w for _s, _e, w in buffer)
        if word.strip().endswith((".", "?", "!")) or len(joined) >= max_chars:
            flush()
    flush()

    # Stretch cues too short to read, but never past the next one's start.
    adjusted: list[Cue] = []
    for i, cue in enumerate(cues):
        end = cue.end_s
        if cue.duration_s < min_seconds:
            limit = cues[i + 1].start_s if i + 1 < len(cues) else cue.start_s + min_seconds
            end = min(cue.start_s + min_seconds, limit)
            end = max(end, cue.end_s)
        adjusted.append(Cue(start_s=cue.start_s, end_s=end, text=cue.text))
    return adjusted


def transcribe(
    audio: Path,
    *,
    model_size: str = "base",
    language: str | None = None,
    logger: RunLogger | None = None,
) -> list[Cue]:
    """Speech -> phrase cues via faster-whisper (optional extra `[captions]`).

    Word timestamps are requested because phrase grouping needs them: segment
    timestamps alone would make every cue a whole Whisper segment, which runs
    long and drifts off the gap band's two-line budget.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - optional extra
        raise CaptionError(
            "transcription needs faster-whisper, an optional extra:\n"
            "    pip install -e '.[captions]'\n"
            "Or hand-author cues.json and skip transcription entirely."
        ) from exc

    if not audio.is_file():
        raise CaptionError(f"audio track not found: {audio}")

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(audio), language=language, word_timestamps=True, vad_filter=True
    )
    words: list[tuple[float, float, str]] = []
    for segment in segments:
        for word in getattr(segment, "words", None) or []:
            words.append((float(word.start), float(word.end), str(word.word)))
    if logger is not None:
        logger.info(
            "captions.transcribed",
            model=model_size,
            language=getattr(info, "language", None),
            words=len(words),
        )
    if not words:
        raise CaptionError(
            f"faster-whisper returned no word timestamps for {audio}. A silent "
            "or music-only clip has no dialogue to caption — hand-author "
            "cues.json with the non-dialogue cues instead."
        )
    return phrase_cues(words)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def save_cues(cues: list[Cue], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "count": len(cues),
        "nondialogue": sum(1 for c in cues if c.is_nondialogue),
        "cues": [c.as_dict() for c in cues],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)
    return path


def load_cues(path: Path) -> list[Cue]:
    """Read cues, hand-edited or generated.

    An operator editing this file is the supported way to add the non-dialogue
    cues the reference clips carry, and to fix a mis-transcription before
    paying to render.
    """
    if not path.is_file():
        raise CaptionError(f"no cue file at {path}")
    data = json.loads(path.read_text())
    cues = [
        Cue(start_s=float(c["start_s"]), end_s=float(c["end_s"]), text=str(c["text"]))
        for c in data.get("cues", [])
    ]
    for cue in cues:
        if cue.end_s < cue.start_s:
            raise CaptionError(
                f"cue {cue.text!r} ends ({cue.end_s}) before it starts ({cue.start_s})"
            )
    return cues


def write_srt(cues: list[Cue], path: Path) -> Path:
    """An SRT alongside the rendered track — for inspection and for anything
    downstream that wants the text. It is NOT what gets burned in any more."""

    def stamp(seconds: float) -> str:
        ms = int(round(seconds * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, cue in enumerate(cues, start=1):
        lines.append(f"{i}\n{stamp(cue.start_s)} --> {stamp(cue.end_s)}\n{cue.text}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path


# --------------------------------------------------------------------------
# Rendering into the gap band
# --------------------------------------------------------------------------

# Vertical breathing room inside the gap band, as a fraction of its height.
#
# WITHOUT THIS, a two-line cue fitted to the full band height renders edge to
# edge: ink in rows 6..71 of a 72px band, technically inside it and visually
# touching both panels. `assert_within_gap` passed and the frame still looked
# like the caption was crossing into the picture — which is the exact failure
# T12 exists to prevent. Found by looking at a rendered frame, not by a test.
CAPTION_VERTICAL_PADDING = 0.14

# Line spacing multiplier. Used by BOTH the fitter and the renderer — see
# `line_height_for`.
LINE_SPACING = 1.15


def line_height_for(font: ImageFont.FreeTypeFont) -> int:
    """One line's vertical advance.

    ONE definition, shared by the fitter and the renderer. They previously used
    two: the fitter measured `getbbox("Ay")` (ink extent) while the renderer used
    `getmetrics()` (ascent+descent, which is taller). So the fitter approved a
    size that then rendered taller than it had measured, and a two-line cue
    overflowed the band it had just been checked against.
    """
    ascent, descent = font.getmetrics()
    return int((ascent + descent) * LINE_SPACING)


def _fit_font(
    draw: ImageDraw.ImageDraw, text: str, font_path: Path, size: int, box: tuple[int, int]
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest font at or below `size` whose wrapped text fits `box`.

    Returns the font and the wrapped lines. Shrinking is bounded by the band's
    usable height, which is the point of doing this in PIL: the gap is a known
    box and the text is measured against it rather than drawn and hoped for.
    """
    width, height = box
    for candidate in range(size, 9, -2):
        font = ImageFont.truetype(str(font_path), candidate)
        lines = _wrap(draw, text, font, width)
        total = len(lines) * line_height_for(font)
        if total <= height and all(draw.textlength(l, font=font) <= width for l in lines):
            return font, lines
    # Floor. Still measured, so the caller's assert_within_gap can catch a band
    # too short to letter at all rather than silently clipping.
    font = ImageFont.truetype(str(font_path), 10)
    return font, _wrap(draw, text, font, width)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_cue_image(
    cue: Cue,
    *,
    layout: Layout,
    render: RenderConfig,
    profile: StyleProfile,
    padding_fraction: float = 0.06,
) -> Image.Image:
    """One caption, drawn centred in a gap-band-sized RGBA image.

    Transparent, so the composite shows the background through it rather than
    painting a second-guess at the background colour over the real one.
    """
    size = (layout.canvas_width, layout.gap_height)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    if not cue.text.strip():
        return img

    draw = ImageDraw.Draw(img)
    pad_x = int(layout.canvas_width * padding_fraction)
    # Reserve vertical padding so the text CLEARS both panels rather than
    # merely staying inside the band. A caption whose descenders sit on the
    # panel boundary reads as overlapping it.
    pad_y = int(layout.gap_height * CAPTION_VERTICAL_PADDING)
    usable = max(1, layout.gap_height - 2 * pad_y)
    box = (layout.canvas_width - 2 * pad_x, usable)
    font, lines = _fit_font(
        draw, cue.text, render.font_path(), render.caption_font_size, box
    )
    if not lines:
        return img

    line_height = line_height_for(font)
    total = line_height * len(lines)
    y = max(pad_y, pad_y + (usable - total) // 2)
    colour = hex_to_rgb(profile.header_text_color) + (255,)
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((layout.canvas_width - w) / 2, y), line, font=font, fill=colour)
        y += line_height
    return img


def render_caption_track(
    cues: list[Cue],
    *,
    out_dir: Path,
    layout: Layout,
    render: RenderConfig,
    profile: StyleProfile,
    fps: int,
    total_frames: int,
    logger: RunLogger | None = None,
) -> int:
    """Render the caption band as a PNG sequence, one image per output frame.

    Blank frames are real images, not gaps: a PNG sequence input with a missing
    index stops the whole input early, which would truncate the overlay and —
    via the trim bound in assembly — the video.

    Identical consecutive cues are rendered ONCE and hard-linked (copied where
    links are unavailable), so a 3-second cue costs one text render rather than
    36. On a 60s clip this is the difference between ~40 renders and 720.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob(CAPTION_GLOB):
        stale.unlink()

    blank = Image.new("RGBA", (layout.canvas_width, layout.gap_height), (0, 0, 0, 0))
    cache: dict[str, Path] = {}
    rendered = 0

    for index in range(1, total_frames + 1):
        # Frame n covers [(n-1)/fps, n/fps); a cue owns the frame its midpoint
        # falls inside, which keeps a cue from flickering on for a single frame
        # at a boundary.
        t = (index - 0.5) / fps
        active = next((c for c in cues if c.start_s <= t < c.end_s), None)
        key = active.text if active is not None else ""
        dst = out_dir / (CAPTION_FRAME_PATTERN % index)

        if key in cache:
            source = cache[key]
            try:
                dst.hardlink_to(source)
            except (OSError, AttributeError):
                dst.write_bytes(source.read_bytes())
            continue

        image = blank if active is None else render_cue_image(
            active, layout=layout, render=render, profile=profile
        )
        image.save(dst)
        rendered += 1
        cache[key] = dst

    if logger is not None:
        logger.info(
            "captions.track",
            frames=total_frames,
            cues=len(cues),
            nondialogue=sum(1 for c in cues if c.is_nondialogue),
            distinct_renders=rendered,
            band=f"{layout.canvas_width}x{layout.gap_height}",
            band_y=layout.gap_y,
            path=str(out_dir),
        )
    return total_frames


def assert_within_gap(image: Image.Image, layout: Layout) -> None:
    """A rendered cue must not exceed the gap band. Checked, not assumed.

    The failure this guards against is a caption spilling onto a panel and
    cropping a face — a file that plays perfectly and is wrong.
    """
    if image.size != (layout.canvas_width, layout.gap_height):
        raise CaptionError(
            f"caption image is {image.size}, but the gap band is "
            f"{layout.canvas_width}x{layout.gap_height}. A caption that does "
            "not match the band will overlap a panel."
        )
    alpha = image.split()[-1]
    bbox = alpha.getbbox()
    if bbox is None:
        return
    _left, top, _right, bottom = bbox
    if top < 0 or bottom > layout.gap_height:
        raise CaptionError(
            f"caption ink spans y={top}..{bottom} inside a {layout.gap_height}px band"
        )
    # Containment is not enough. Ink touching row 0 or the last row abuts a
    # panel and READS as overlapping it, which is the failure T12 exists to
    # prevent — and a containment-only check passed it.
    clearance = max(1, int(layout.gap_height * CAPTION_VERTICAL_PADDING) // 2)
    if top < clearance or bottom > layout.gap_height - clearance:
        raise CaptionError(
            f"caption ink spans y={top}..{bottom} in a {layout.gap_height}px "
            f"band, leaving less than {clearance}px of clearance from a panel. "
            "It is inside the band but reads as overlapping the picture."
        )
