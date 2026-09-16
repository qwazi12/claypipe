"""Caption-band inpainting for the restyle INPUT only (C4).

Most sources are Shorts and most carry burned-in captions. On the Young Sheldon
test clip, 65.5% of frames carry caption ink and the text changes 49 times in
59.3 seconds. Under whole-frame v2v those glyphs are restyled along with
everything else, so the comparison panel fills with melted clay lettering — the
model spending its effort on typography.

WHAT THIS DOES, AND ONLY THIS: it removes the caption band from the frames fed
to the generator. It does not touch the ORIGINAL panel, which keeps its own
captions (they are part of what the viewer is comparing against), and it does
not touch the audio.

THE INPAINT DOES NOT NEED TO BE GOOD. Its output is a control signal for a
model that is about to restyle the whole frame into clay, so stylisation covers
the artifacts. That is why this uses OpenCV's Telea inpaint rather than a
generative fill: it is offline, instant, dependency-free, and good enough for
the one job. Reaching for a generative inpaint here would add a paid call per
frame to improve something nobody sees.

The band comes from T9b's detection, which SEARCHES for it rather than assuming
the lower third — on this clip it sits at 51% of frame height, dead centre,
because short-form captions are centred.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The detected band is grown by this fraction of its height before inpainting.
# Glyph outlines and anti-aliasing extend past the rows where the detector
# found ink, and an un-grown mask leaves a ghost of the text for the model to
# faithfully restyle into clay.
BAND_PADDING = 0.25

# Telea's radius, in pixels. Small: the band is a thin horizontal strip, so
# useful context is close by, and a large radius smears distant colour into it.
INPAINT_RADIUS = 5


class InpaintError(RuntimeError):
    """Inpainting could not run."""


@dataclass(frozen=True)
class InpaintReport:
    frames: int
    band_top: int
    band_bottom: int
    frame_height: int

    @property
    def band_height(self) -> int:
        return max(0, self.band_bottom - self.band_top + 1)

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "band_top": self.band_top,
            "band_bottom": self.band_bottom,
            "band_height": self.band_height,
            "frame_height": self.frame_height,
            "method": "opencv_telea",
            "note": (
                "applied to the RESTYLE INPUT only. The original panel keeps "
                "its own captions, and the audio is untouched. The inpaint is "
                "deliberately cheap: its output is a control signal for a model "
                "that restyles the whole frame, so stylisation covers the "
                "artifacts."
            ),
        }


def band_for(report: dict, frame_height: int) -> tuple[int, int]:
    """Scale a T9b report's band onto a frame of `frame_height`, with padding.

    T9b probes at a fixed width, so its rows are in PROBE space, not the
    source's. Using them directly would mask the wrong strip on any frame of a
    different height — which is every frame, since the probe is 640 wide at the
    source aspect.
    """
    probe_height = int(report.get("frame_height") or 0)
    if probe_height <= 0:
        raise InpaintError("the burn-in report carries no frame_height to scale from")

    scale = frame_height / probe_height
    top = int(report["band_top"] * scale)
    bottom = int(report["band_bottom"] * scale)
    pad = int((bottom - top + 1) * BAND_PADDING)
    return max(0, top - pad), min(frame_height - 1, bottom + pad)


def inpaint_band(image, top: int, bottom: int):
    """Telea inpaint over the rows [top, bottom]."""
    import cv2
    import numpy as np

    mask = np.zeros(image.shape[:2], dtype=np.uint8)
    mask[top : bottom + 1, :] = 255
    return cv2.inpaint(image, mask, INPAINT_RADIUS, cv2.INPAINT_TELEA)


def inpaint_frames(
    *,
    frames: list[Path],
    out_dir: Path,
    burn_in_report: dict,
    logger=None,
) -> InpaintReport:
    """Write caption-free copies of `frames` into `out_dir`.

    Resume-safe like every other stage: an existing output is not rewritten.
    """
    import numpy as np
    from PIL import Image

    if not frames:
        raise InpaintError("inpaint_frames got no frames")
    if not burn_in_report.get("detected"):
        raise InpaintError(
            "the burn-in report says no text was detected, so there is nothing "
            "to inpaint. Refusing to blur a band for no reason."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(frames[0]) as probe:
        frame_height = probe.size[1]
    top, bottom = band_for(burn_in_report, frame_height)

    written = 0
    for src in frames:
        dst = out_dir / src.name
        if dst.is_file():
            continue
        with Image.open(src) as img:
            array = np.asarray(img.convert("RGB"))
        Image.fromarray(inpaint_band(array, top, bottom)).save(dst)
        written += 1

    report = InpaintReport(
        frames=len(frames), band_top=top, band_bottom=bottom,
        frame_height=frame_height,
    )
    if logger is not None:
        logger.info("inpaint.band", written=written, **report.as_dict())
    return report
