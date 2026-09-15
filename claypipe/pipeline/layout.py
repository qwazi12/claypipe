"""Canvas layout, derived from the SOURCE's aspect ratio (MASTER_PLAN §1.1).

The 9:16 canvas is not a fixed stack of configured heights. Panel height is a
FUNCTION of the source clip's own aspect ratio, the panel pair is centred as a
block, and the header lives in whatever top margin that leaves. Measuring the
reference clips is what settled this: a 16:9 source and a 2.01:1 source produce
completely different panel heights and margins, and both are correct.

    panel_h   = even(canvas_width / source_aspect)
    gap       = even(gap_fraction * canvas_height)
    remainder = canvas_height - 2*panel_h - gap
    top       = even(remainder / 2)      <- the header bar is drawn here
    bottom    = remainder - top

Every dimension is even because yuv420p subsamples chroma 2x2 and an odd height
is a render failure, not a rounding wobble. The four vertical terms sum to the
canvas height exactly, by construction — `top` is the only rounded term and
`bottom` absorbs its remainder.

Captions are a LAYOUT ELEMENT drawn in `gap`, not a subtitle filter burned over
the picture (see captions.py). That is why the gap is a first-class band here
and not a `divider_height` afterthought.
"""

from __future__ import annotations

from dataclasses import dataclass

# A header any shorter than this cannot hold legible type at 1080 wide, so a
# source aspect that squeezes the margins below it is refused rather than
# rendered with a clipped title.
MIN_HEADER_HEIGHT = 96


class LayoutError(RuntimeError):
    """The requested canvas cannot hold two panels at the source's aspect."""


def even(value: float) -> int:
    """Nearest even integer. yuv420p requires it; ffmpeg will not round for us."""
    return int(round(value / 2.0)) * 2


@dataclass(frozen=True)
class Layout:
    """Resolved vertical geometry for one run. All values in pixels."""

    canvas_width: int
    canvas_height: int
    source_aspect: float
    panel_height: int
    gap_height: int
    top_margin: int
    bottom_margin: int

    # -- band origins ----------------------------------------------------
    @property
    def header_height(self) -> int:
        """The header bar fills the top margin — it is the same band."""
        return self.top_margin

    @property
    def restyled_y(self) -> int:
        return self.top_margin

    @property
    def gap_y(self) -> int:
        return self.top_margin + self.panel_height

    @property
    def original_y(self) -> int:
        return self.gap_y + self.gap_height

    @property
    def panel_aspect(self) -> float:
        return self.canvas_width / self.panel_height

    def closes(self) -> bool:
        total = self.top_margin + 2 * self.panel_height + self.gap_height + self.bottom_margin
        return total == self.canvas_height

    def as_dict(self) -> dict:
        """For the manifest and the QC card — geometry is reported, not implied."""
        return {
            "canvas_width": self.canvas_width,
            "canvas_height": self.canvas_height,
            "source_aspect": round(self.source_aspect, 4),
            "panel_height": self.panel_height,
            "panel_aspect": round(self.panel_aspect, 4),
            "gap_height": self.gap_height,
            "top_margin": self.top_margin,
            "bottom_margin": self.bottom_margin,
            "restyled_y": self.restyled_y,
            "gap_y": self.gap_y,
            "original_y": self.original_y,
        }


def compute_layout(
    *,
    canvas_width: int,
    canvas_height: int,
    source_aspect: float,
    gap_fraction: float,
) -> Layout:
    """Derive the vertical stack for one source aspect.

    Raises LayoutError rather than producing a canvas whose header is too short
    to letter, or whose panels do not fit at all. Both are configuration
    mistakes that must surface at startup, not as a clipped render.
    """
    if source_aspect <= 0:
        raise LayoutError(f"source aspect must be positive, got {source_aspect}")
    if not 0.0 <= gap_fraction < 0.5:
        raise LayoutError(f"gap_fraction must be in [0, 0.5), got {gap_fraction}")

    panel_height = even(canvas_width / source_aspect)
    gap_height = even(gap_fraction * canvas_height)
    remainder = canvas_height - 2 * panel_height - gap_height

    if remainder < 2 * MIN_HEADER_HEIGHT:
        raise LayoutError(
            f"a {source_aspect:.3f}:1 source leaves only {remainder}px of margin "
            f"on a {canvas_width}x{canvas_height} canvas after two "
            f"{panel_height}px panels and a {gap_height}px caption gap. "
            f"Need at least {2 * MIN_HEADER_HEIGHT}px (header floor is "
            f"{MIN_HEADER_HEIGHT}px). Either the source is too tall for a "
            "two-panel stack, or render.caption_gap_fraction is too large."
        )

    top_margin = even(remainder / 2.0)
    bottom_margin = remainder - top_margin

    layout = Layout(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        source_aspect=source_aspect,
        panel_height=panel_height,
        gap_height=gap_height,
        top_margin=top_margin,
        bottom_margin=bottom_margin,
    )
    if not layout.closes():
        # Unreachable by construction; asserted anyway because a canvas that
        # does not close renders a black seam and nothing else would catch it.
        raise LayoutError(
            f"derived layout does not close: {top_margin} + 2*{panel_height} + "
            f"{gap_height} + {bottom_margin} != {canvas_height}"
        )
    return layout


def source_aspect_of(width: int, height: int) -> float:
    if width <= 0 or height <= 0:
        raise LayoutError(f"source dimensions must be positive, got {width}x{height}")
    return width / height
