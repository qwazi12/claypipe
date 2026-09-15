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

T9a — THAT RULE ONLY WORKS WHEN THE RESULT FITS, and both reference clips were
widescreen so the case was never exercised. A SQUARE source makes two panels
1080px tall each: 2*1080 + 72 = 2232 against a 1920 canvas. There is no
solution, and "derive the height from the width" quietly produces a negative
margin.

So panel sizing has two branches, and which one applies is DERIVED, not
configured:

  WIDTH-FIT   panels span the full canvas width, height follows the aspect.
              Available when 2*(W/aspect) + gap + 2*min_margin <= H.
  HEIGHT-FIT  panels are sized to the vertical space that is left, width
              follows the aspect, and they are centred horizontally with
              background pillarboxing either side.

The cutoff falls out of the minimum margin rather than being picked:

    aspect_cutoff = W / ((H - gap - 2*min_margin) / 2)

At a 9% minimum margin (172px of 1920) that is 1.436 — so 16:9 (1.778) and
2.00:1 take width-fit unchanged, while 4:3 (1.333) and 1:1 take height-fit.
9% is chosen to sit well above the 96px legibility floor for the header and
well below the 16.6% and 20.6% margins the two reference clips actually use,
so it constrains only the cases the reference format never covered.
"""

from __future__ import annotations

from dataclasses import dataclass

# A header any shorter than this cannot hold legible type at 1080 wide, so a
# source aspect that squeezes the margins below it is refused rather than
# rendered with a clipped title. An absolute floor, in pixels.
MIN_HEADER_HEIGHT = 96

# The margin floor that decides WHICH FIT BRANCH applies (T9a), as a fraction
# of canvas height. 9% of 1920 is 172px.
#
# Not a taste setting and not reverse-engineered from a desired cutoff: it has
# to sit above MIN_HEADER_HEIGHT (96px, legibility) and below the margins the
# reference format actually uses (16.6% on clip A, 20.6% on clip B), so that it
# binds ONLY on aspects the reference clips never covered. 9% satisfies both,
# and the cutoff it implies (1.436) falls where it should: widescreen keeps the
# full-width layout, square and 4:3 do not.
MIN_MARGIN_FRACTION = 0.09

# Below this a panel is too small for the side-by-side comparison to be worth
# making. Refused with the aspect named rather than rendered as a postage stamp.
MIN_PANEL_HEIGHT = 200


class LayoutError(RuntimeError):
    """The requested canvas cannot hold two panels at the source's aspect."""


def even(value: float) -> int:
    """Nearest even integer. yuv420p requires it; ffmpeg will not round for us."""
    return int(round(value / 2.0)) * 2


@dataclass(frozen=True)
class Layout:
    """Resolved geometry for one run. All values in pixels."""

    canvas_width: int
    canvas_height: int
    source_aspect: float
    panel_height: int
    gap_height: int
    top_margin: int
    bottom_margin: int
    # T9a: panels no longer always span the canvas. A height-fit layout makes
    # them narrower and pillarboxes them, so width and x offset are part of the
    # geometry rather than implied.
    panel_width: int = 0
    panel_x: int = 0
    fit_mode: str = "width"

    def __post_init__(self) -> None:
        # Width-fit layouts predate T9a and omit these; fill them in so every
        # consumer can read the same fields regardless of branch.
        if self.panel_width == 0:
            object.__setattr__(self, "panel_width", self.canvas_width)
        if self.panel_x == 0:
            object.__setattr__(
                self, "panel_x", (self.canvas_width - self.panel_width) // 2
            )

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
        return self.panel_width / self.panel_height

    @property
    def is_pillarboxed(self) -> bool:
        return self.panel_width < self.canvas_width

    def closes(self) -> bool:
        total = self.top_margin + 2 * self.panel_height + self.gap_height + self.bottom_margin
        return total == self.canvas_height

    def as_dict(self) -> dict:
        """For the manifest and the QC card — geometry is reported, not implied."""
        return {
            "canvas_width": self.canvas_width,
            "canvas_height": self.canvas_height,
            "source_aspect": round(self.source_aspect, 4),
            "fit_mode": self.fit_mode,
            "panel_width": self.panel_width,
            "panel_height": self.panel_height,
            "panel_x": self.panel_x,
            "panel_aspect": round(self.panel_aspect, 4),
            "pillarboxed": self.is_pillarboxed,
            "gap_height": self.gap_height,
            "top_margin": self.top_margin,
            "bottom_margin": self.bottom_margin,
            "restyled_y": self.restyled_y,
            "gap_y": self.gap_y,
            "original_y": self.original_y,
        }


def min_margin_for(canvas_height: int) -> int:
    """The margin floor, in pixels. Never below the header legibility floor."""
    return max(MIN_HEADER_HEIGHT, even(canvas_height * MIN_MARGIN_FRACTION))


def width_fit_cutoff(canvas_width: int, canvas_height: int, gap_height: int) -> float:
    """The aspect at or above which panels fit at FULL canvas width (T9a).

    Derived, not configured:

        2*(W/aspect) + gap + 2*min_margin <= H
        aspect >= W / ((H - gap - 2*min_margin) / 2)

    Returns infinity when the canvas cannot hold two panels at any aspect,
    which makes every source take the height-fit branch and fail there with a
    message about the real problem.
    """
    usable = canvas_height - gap_height - 2 * min_margin_for(canvas_height)
    if usable <= 0:
        return float("inf")
    return canvas_width / (usable / 2.0)


def compute_layout(
    *,
    canvas_width: int,
    canvas_height: int,
    source_aspect: float,
    gap_fraction: float,
) -> Layout:
    """Derive the geometry for one source aspect, by whichever branch fits.

    Raises LayoutError rather than producing a canvas whose header is too short
    to letter, or whose panels do not fit at all. Both are configuration
    mistakes that must surface at startup, not as a clipped render.
    """
    if source_aspect <= 0:
        raise LayoutError(f"source aspect must be positive, got {source_aspect}")
    if not 0.0 <= gap_fraction < 0.5:
        raise LayoutError(f"gap_fraction must be in [0, 0.5), got {gap_fraction}")

    gap_height = even(gap_fraction * canvas_height)
    cutoff = width_fit_cutoff(canvas_width, canvas_height, gap_height)

    if source_aspect >= cutoff:
        layout = _width_fit(canvas_width, canvas_height, source_aspect, gap_height)
    else:
        layout = _height_fit(
            canvas_width, canvas_height, source_aspect, gap_height, cutoff
        )

    if not layout.closes():
        # Unreachable by construction; asserted anyway because a canvas that
        # does not close renders a black seam and nothing else would catch it.
        raise LayoutError(
            f"derived layout does not close: {layout.top_margin} + "
            f"2*{layout.panel_height} + {layout.gap_height} + "
            f"{layout.bottom_margin} != {canvas_height}"
        )
    if layout.panel_width > canvas_width:
        raise LayoutError(
            f"derived panel width {layout.panel_width} exceeds the canvas "
            f"({canvas_width}) for a {source_aspect:.3f}:1 source"
        )
    return layout


def _width_fit(
    canvas_width: int, canvas_height: int, source_aspect: float, gap_height: int
) -> Layout:
    """Panels span the full canvas width; height follows the aspect.

    The original rule, unchanged — this is what 16:9 and 2.00:1 take, and the
    values it produces must not move.
    """
    panel_height = even(canvas_width / source_aspect)
    remainder = canvas_height - 2 * panel_height - gap_height
    if remainder < 2 * MIN_HEADER_HEIGHT:
        raise LayoutError(
            f"a {source_aspect:.3f}:1 source leaves only {remainder}px of margin "
            f"on a {canvas_width}x{canvas_height} canvas after two "
            f"{panel_height}px panels and a {gap_height}px caption gap. "
            f"Need at least {2 * MIN_HEADER_HEIGHT}px (header floor is "
            f"{MIN_HEADER_HEIGHT}px)."
        )
    top_margin = even(remainder / 2.0)
    return Layout(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        source_aspect=source_aspect,
        panel_height=panel_height,
        gap_height=gap_height,
        top_margin=top_margin,
        bottom_margin=remainder - top_margin,
        panel_width=canvas_width,
        panel_x=0,
        fit_mode="width",
    )


def _height_fit(
    canvas_width: int,
    canvas_height: int,
    source_aspect: float,
    gap_height: int,
    cutoff: float,
) -> Layout:
    """Panels are sized to the vertical space left over; width follows the aspect.

    The T9a branch. Panels are as tall as the margin floor permits, then
    narrowed to the source's aspect and centred horizontally — the leftover
    width either side is background, the same pillarboxing the canvas already
    uses above and below.

    Note the panels are deliberately NOT stretched to the canvas width. Doing
    so is what a naive fix would do, and it would distort every face in the
    comparison while leaving a file that plays.
    """
    min_margin = min_margin_for(canvas_height)
    available = canvas_height - gap_height - 2 * min_margin
    panel_height = even(available / 2.0)

    if panel_height < MIN_PANEL_HEIGHT:
        raise LayoutError(
            f"a {source_aspect:.3f}:1 source cannot be laid out on a "
            f"{canvas_width}x{canvas_height} canvas: after a {gap_height}px "
            f"caption gap and {min_margin}px minimum margins there is only "
            f"{available}px for two panels, i.e. {panel_height}px each, under "
            f"the {MIN_PANEL_HEIGHT}px floor. Reduce "
            "render.caption_gap_fraction, or use a taller canvas."
        )

    panel_width = even(panel_height * source_aspect)
    if panel_width > canvas_width:
        # Only reachable if the cutoff and this branch disagree, which would be
        # a bug in width_fit_cutoff rather than a bad input.
        raise LayoutError(
            f"height-fit produced a {panel_width}px panel for a "
            f"{source_aspect:.3f}:1 source on a {canvas_width}px canvas, but "
            f"that aspect is below the width-fit cutoff ({cutoff:.3f}). The "
            "two branches disagree; this is a layout-engine bug, not a bad clip."
        )

    remainder = canvas_height - 2 * panel_height - gap_height
    top_margin = even(remainder / 2.0)
    return Layout(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        source_aspect=source_aspect,
        panel_height=panel_height,
        gap_height=gap_height,
        top_margin=top_margin,
        bottom_margin=remainder - top_margin,
        panel_width=panel_width,
        panel_x=(canvas_width - panel_width) // 2,
        fit_mode="height",
    )


def source_aspect_of(width: int, height: int) -> float:
    if width <= 0 or height <= 0:
        raise LayoutError(f"source dimensions must be positive, got {width}x{height}")
    return width / height
