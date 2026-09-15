"""Pre-burned caption detection (T9b).

A source that already carries burned-in subtitles breaks the format three ways
at once, and none of them is visible until the money is spent:

  * the text appears in the bottom ORIGINAL panel, where it is merely redundant;
  * it gets RESTYLED into the top panel, so the comparison shows clay-textured
    subtitle glyphs — the restyle spending effort on typography;
  * T12 then draws its own caption track in the gap band, so the finished frame
    carries the same line twice in two different styles.

This module does not remove them and does not block the run. It tells the
operator before they spend, and records a flag on the manifest so a later
reviewer can tell a garbled top panel from a backend failure.

WHERE THE TEXT IS IS NOT ASSUMED. The obvious heuristic looks in the lower
third, which is where broadcast subtitles live. Measured on the Young Sheldon
Shorts rip, the burned-in band peaks at row 326 of 640 — 51% down the frame,
dead centre — because short-form social captions are centred, not lower-third.
A lower-third detector would have reported that clip clean. So the band is
SEARCHED FOR across the full height and reported with its measured position.

The discriminator is not brightness. It is brightness that is HORIZONTALLY
CLUSTERED and VERTICALLY PERSISTENT: subtitle glyphs occupy a similar span of
similar rows frame after frame, while a bright window or a white shirt moves,
changes shape, and does not hold a stable horizontal extent.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import ffmpeg

# Sampling. 32 frames spread across the clip establishes persistence and costs
# well under a second — this runs at intake, before any spend.
DEFAULT_SAMPLES = 32
# Probed at native-ish width: downscaling to 320 thins glyph strokes until the
# outline signal disappears, which is how the first version of this detector
# missed the Sheldon captions entirely.
PROBE_WIDTH = 640

# Subtitle fill is near-white; the outline that makes it legible over any
# background is near-black. Requiring BOTH within a few pixels is the signal —
# brightness alone finds windows and white shirts.
BRIGHT = 200
DARK = 80
OUTLINE_RADIUS = 3

# A row counts as text-bearing above this density of outlined-bright pixels.
ROW_DENSITY = 0.005

# A band must be a plausible overlay height. Below, it is noise; above, it is
# bright picture content with edges (a test pattern, a venetian blind).
MIN_BAND_FRACTION = 0.015
MAX_BAND_FRACTION = 0.30

# A caption spans a wide, roughly centred run of the frame. A watermark is
# narrow and sits off to one side. Both are burned-in ink that will be
# restyled, so both are reported — but they are named differently, because a
# garbled watermark and a garbled subtitle are different problems.
CAPTION_MIN_WIDTH_FRACTION = 0.30


class BurnInError(RuntimeError):
    """Detection could not run."""


@dataclass(frozen=True)
class BurnInReport:
    """What the detector found. ADVISORY — it never blocks a run."""

    detected: bool
    samples: int
    frame_width: int = 0
    frame_height: int = 0
    band_top: int = 0
    band_bottom: int = 0
    band_left: int = 0
    band_right: int = 0
    density: float = 0.0
    kind: str = "none"
    reason: str = ""

    @property
    def band_height(self) -> int:
        return max(0, self.band_bottom - self.band_top + 1)

    @property
    def band_width(self) -> int:
        return max(0, self.band_right - self.band_left + 1)

    @property
    def band_centre_fraction(self) -> float:
        if not self.frame_height:
            return 0.0
        return ((self.band_top + self.band_bottom) / 2.0) / self.frame_height

    def describe(self) -> str:
        if not self.detected:
            return f"no burned-in text detected ({self.reason})"
        return (
            f"burned-in {self.kind} detected: rows {self.band_top}-{self.band_bottom} "
            f"of {self.frame_height} ({self.band_centre_fraction * 100:.0f}% down "
            f"the frame), spanning x={self.band_left}-{self.band_right} "
            f"({self.band_width / max(self.frame_width, 1) * 100:.0f}% of width), "
            f"across {self.samples} sampled frames"
        )

    def as_dict(self) -> dict:
        return {
            "detected": self.detected,
            "kind": self.kind,
            "samples": self.samples,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "band_top": self.band_top,
            "band_bottom": self.band_bottom,
            "band_height": self.band_height,
            "band_left": self.band_left,
            "band_right": self.band_right,
            "band_width": self.band_width,
            "band_centre_fraction": round(self.band_centre_fraction, 4),
            "density": round(self.density, 5),
            "reason": self.reason,
        }


def _sample_frames(video: Path, samples: int, width: int):
    """`samples` grayscale frames spread across the clip, at the source aspect."""
    import numpy as np

    tools = ffmpeg.require_ffmpeg()
    duration = ffmpeg.duration_seconds(video)
    if duration <= 0:
        raise BurnInError(f"{video} reports no duration")
    stream = ffmpeg.stream(video, "video")
    src_w, src_h = int(stream["width"]), int(stream["height"])
    height = max(2, int(round(width * src_h / src_w)) // 2 * 2)

    # An even spread that skips the first and last moments — a fade carries no
    # overlay and would dilute the measurement.
    fps = max(samples / max(duration * 0.9, 0.1), 0.01)
    proc = subprocess.run(
        [tools.ffmpeg, "-v", "error", "-ss", f"{duration * 0.05:.3f}",
         "-i", str(video), "-vf", f"fps={fps:.6f},scale={width}:{height}",
         "-frames:v", str(samples), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
    )
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    count = len(data) // (width * height)
    if count == 0:
        raise BurnInError(f"could not sample any frames from {video}")
    return data[: count * width * height].reshape(count, height, width)


def _outlined_bright(frames):
    """Bright pixels that have a dark pixel within OUTLINE_RADIUS on the row.

    This is the whole discriminator. Subtitle and watermark glyphs are drawn
    with a contrasting outline so they stay legible over any background, which
    makes them the only thing in a frame that is reliably bright AND adjacent
    to dark at that scale. A blown-out window is bright with no dark beside it.
    """
    import numpy as np

    bright = frames >= BRIGHT
    dark = frames <= DARK
    # A 1-D dilation of `dark` along the row, done with shifts so the module
    # needs no scipy.
    near_dark = dark.copy()
    for shift in range(1, OUTLINE_RADIUS + 1):
        near_dark[:, :, shift:] |= dark[:, :, :-shift]
        near_dark[:, :, :-shift] |= dark[:, :, shift:]
    return bright & near_dark


def _longest_run(mask) -> tuple[int, int]:
    """Longest contiguous True run as (start, end); (-1, -1) when empty."""
    best = (-1, -1)
    best_len = 0
    start = None
    for i in range(len(mask) + 1):
        inside = i < len(mask) and bool(mask[i])
        if inside and start is None:
            start = i
        elif not inside and start is not None:
            if i - start > best_len:
                best_len = i - start
                best = (start, i - 1)
            start = None
    return best


def detect_burned_in_captions(
    video: Path,
    *,
    samples: int = DEFAULT_SAMPLES,
    width: int = PROBE_WIDTH,
) -> BurnInReport:
    """Search the full frame for a persistent band of outlined text.

    WHERE THE TEXT IS IS NOT ASSUMED — see the module docstring. The band is
    searched for across the full height and reported with its measured
    position, because the obvious lower-third heuristic reports a centred
    short-form caption as clean.

    Cheap, conservative, and honest about being a heuristic: it runs at intake
    on every clip, a false positive costs one warning, and a false negative
    costs a batch. The report carries the measured numbers so the operator can
    judge it rather than trust it.
    """
    import numpy as np

    if not video.is_file():
        raise BurnInError(f"source video not found: {video}")

    frames = _sample_frames(video, samples, width)
    count, height, frame_width = frames.shape
    outlined = _outlined_bright(frames)

    row_density = outlined.mean(axis=(0, 2))
    rows = row_density >= ROW_DENSITY
    top, bottom = _longest_run(rows)
    if top < 0:
        return BurnInReport(
            detected=False, samples=count,
            frame_width=frame_width, frame_height=height,
            reason="no row carried a persistent band of outlined text",
        )

    band_fraction = (bottom - top + 1) / height
    density = float(row_density[top : bottom + 1].mean())

    if band_fraction < MIN_BAND_FRACTION:
        return BurnInReport(
            detected=False, samples=count, frame_width=frame_width,
            frame_height=height, band_top=top, band_bottom=bottom, density=density,
            reason=(
                f"the strongest band is {band_fraction * 100:.1f}% of frame "
                "height — too thin to be lettering"
            ),
        )
    if band_fraction > MAX_BAND_FRACTION:
        return BurnInReport(
            detected=False, samples=count, frame_width=frame_width,
            frame_height=height, band_top=top, band_bottom=bottom, density=density,
            reason=(
                f"the strongest band spans {band_fraction * 100:.0f}% of frame "
                "height — that is textured picture content, not an overlay"
            ),
        )

    # Horizontal EXTENT, not the longest contiguous run. Glyphs have gaps
    # between letters and words, so the column density of real lettering is
    # periodic — a contiguous-run measure reports the width of one stroke
    # (measured: 3px for a band that actually spanned 62% of the frame) and
    # every caption then gets misclassified as a watermark.
    column_density = outlined[:, top : bottom + 1, :].mean(axis=(0, 1))
    if column_density.max() <= 0:
        left, right = 0, frame_width - 1
    else:
        lit = np.where(column_density >= column_density.max() * 0.15)[0]
        left, right = int(lit.min()), int(lit.max())

    span = (right - left + 1) / frame_width
    kind = "captions" if span >= CAPTION_MIN_WIDTH_FRACTION else "watermark or logo"

    return BurnInReport(
        detected=True, samples=count, frame_width=frame_width, frame_height=height,
        band_top=top, band_bottom=bottom, band_left=left, band_right=right,
        density=density, kind=kind,
        reason="persistent band of outlined text",
    )
