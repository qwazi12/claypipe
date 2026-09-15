"""T9 acceptance — the layout engine (MASTER_PLAN §1.1).

The numbers here are not taste. They are what the derivation produces for the
two aspect ratios measured off the reference clips, and the reason the old
fixed 882px panel was wrong: it cropped real picture away from both of them.
"""

from __future__ import annotations

import pytest

from claypipe.config import load_styles
from claypipe.pipeline.layout import (
    MIN_HEADER_HEIGHT,
    MIN_MARGIN_FRACTION,
    MIN_PANEL_HEIGHT,
    Layout,
    LayoutError,
    compute_layout,
    even,
    min_margin_for,
    source_aspect_of,
    width_fit_cutoff,
)

CANVAS = {"canvas_width": 1080, "canvas_height": 1920}
GAP = 0.037

# Measured off the two @trevorcarlee reference clips, 576x1024 source:
#   clip A (New Girl) restyled panel h=325 -> 576/325 = 1.772 ~ 16:9
#   clip B (Reacher)  restyled panel h=286 -> 576/286 = 2.014
CLIP_A_ASPECT = 16 / 9
CLIP_B_ASPECT = 2.014


def test_even_rounds_to_nearest_even():
    assert even(607.5) == 608
    assert even(536.25) == 536
    assert even(71.04) == 72
    assert even(0) == 0


@pytest.mark.parametrize(
    "aspect,panel,gap,top,bottom",
    [
        (CLIP_A_ASPECT, 608, 72, 316, 316),
        (CLIP_B_ASPECT, 536, 72, 388, 388),
        (2.0, 540, 72, 384, 384),
    ],
)
def test_derived_geometry(aspect, panel, gap, top, bottom):
    """The acceptance table. 16:9 -> 608/72/316; 2.014:1 -> 536/72/388."""
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    assert (layout.panel_height, layout.gap_height) == (panel, gap)
    assert (layout.top_margin, layout.bottom_margin) == (top, bottom)


@pytest.mark.parametrize(
    "aspect", [CLIP_A_ASPECT, CLIP_B_ASPECT, 2.0, 4 / 3, 1.5, 2.4, 1.0, 9 / 16]
)
def test_the_four_terms_sum_to_the_canvas_exactly(aspect):
    """No seam, no overflow. The old plan table summed to 1921 — one over."""
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    total = (
        layout.top_margin
        + layout.panel_height
        + layout.gap_height
        + layout.panel_height
        + layout.bottom_margin
    )
    assert total == 1920
    assert layout.closes()


@pytest.mark.parametrize(
    "aspect", [CLIP_A_ASPECT, CLIP_B_ASPECT, 2.0, 4 / 3, 1.5, 2.4, 1.0, 9 / 16]
)
def test_every_dimension_is_even(aspect):
    """yuv420p subsamples chroma 2x2; an odd height is a render failure."""
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    for name in ("panel_height", "gap_height", "top_margin", "bottom_margin"):
        value = getattr(layout, name)
        assert value % 2 == 0, f"{name}={value} is odd"
    # The band origins are what ffmpeg's overlay y= receives, so they must be
    # even too or the chroma planes land half a pixel out.
    for name in ("restyled_y", "gap_y", "original_y"):
        assert getattr(layout, name) % 2 == 0


@pytest.mark.parametrize(
    "aspect", [CLIP_A_ASPECT, CLIP_B_ASPECT, 2.0, 4 / 3, 2.4, 1.0, 9 / 16]
)
def test_panel_pair_is_block_centred(aspect):
    """Measured margin delta on the reference clips was +3px and -3px — noise
    around a centred block, not a rule. Derivation is exact to <=2px, which is
    the most a single even-rounding can cost."""
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    assert abs(layout.top_margin - layout.bottom_margin) <= 2


@pytest.mark.parametrize(
    "aspect", [CLIP_A_ASPECT, CLIP_B_ASPECT, 2.0, 4 / 3, 2.4, 1.0, 9 / 16]
)
def test_panel_aspect_equals_source_aspect(aspect):
    """The whole point: the panel never crops the source. Within one even step."""
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    assert layout.panel_aspect == pytest.approx(aspect, abs=0.01)


def test_header_band_is_the_top_margin():
    layout = compute_layout(**CANVAS, source_aspect=CLIP_A_ASPECT, gap_fraction=GAP)
    assert layout.header_height == layout.top_margin == layout.restyled_y


def test_band_origins_stack_in_order():
    layout = compute_layout(**CANVAS, source_aspect=CLIP_A_ASPECT, gap_fraction=GAP)
    assert layout.restyled_y == 316
    assert layout.gap_y == 316 + 608
    assert layout.original_y == 316 + 608 + 72
    # The original panel's bottom edge leaves exactly the bottom margin.
    assert layout.original_y + layout.panel_height + layout.bottom_margin == 1920


def test_derived_geometry_tracks_the_measured_reference_within_noise():
    """Against the scaled measurement (1080/576 = 1.875x), panels and margins
    land inside measurement noise. This is the test that would catch a
    regression back to a fixed panel height."""
    scale = 1080 / 576
    for aspect, measured_panel, measured_top in (
        (CLIP_A_ASPECT, 325 * scale, 170 * scale),   # 609.4, 318.8
        (CLIP_B_ASPECT, 286 * scale, 211 * scale),   # 536.3, 395.6
    ):
        layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
        assert abs(layout.panel_height - measured_panel) <= 3
        # Margins carry the gap_fraction mismatch, so the tolerance is wider —
        # clip B's real gap is 0.0312 of height, not 0.037.
        assert abs(layout.top_margin - measured_top) <= 10


# ---------------------------------------------------------------------------
# T9a — the height-fit branch.
#
# Both reference clips were widescreen, so the case where two panels do not fit
# at full canvas width was never exercised. A square source makes them
# 2*1080 + 72 = 2232px tall against a 1920 canvas: no solution, and "derive the
# height from the width" quietly produces a negative margin.
# ---------------------------------------------------------------------------

SHELDON_ASPECT = 1.0  # 640x640 Young Sheldon Shorts rip, verified by cropdetect


def test_the_cutoff_is_derived_from_the_margin_floor_not_hardcoded():
    cutoff = width_fit_cutoff(1080, 1920, even(GAP * 1920))
    # 2*(W/a) + gap + 2*min_margin = H at exactly the cutoff.
    min_margin = min_margin_for(1920)
    panel = 1080 / cutoff
    assert 2 * panel + even(GAP * 1920) + 2 * min_margin == pytest.approx(1920, abs=2)
    # And it lands where the reference aspects need it to.
    assert CLIP_A_ASPECT > cutoff and CLIP_B_ASPECT > cutoff
    assert 4 / 3 < cutoff and SHELDON_ASPECT < cutoff


def test_the_margin_floor_sits_between_legibility_and_the_reference_margins():
    """9% is not reverse-engineered from a desired cutoff: it must clear the
    header legibility floor and stay under the margins the reference format
    actually uses, so it binds ONLY on aspects those clips never covered."""
    floor = min_margin_for(1920)
    assert floor >= MIN_HEADER_HEIGHT
    # Clip A's own margin at full width is 316px; the floor must be below it or
    # 16:9 itself would be pushed onto the height-fit branch.
    widescreen = compute_layout(**CANVAS, source_aspect=CLIP_A_ASPECT, gap_fraction=GAP)
    assert floor < widescreen.top_margin


@pytest.mark.parametrize("aspect", [CLIP_A_ASPECT, CLIP_B_ASPECT, 2.0, 2.4])
def test_widescreen_still_takes_the_width_fit_branch(aspect):
    """The regression that matters: T9a must not move the §1 values."""
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    assert layout.fit_mode == "width"
    assert layout.panel_width == 1080
    assert layout.panel_x == 0
    assert not layout.is_pillarboxed


def test_square_source_gets_two_centred_square_panels():
    """The Sheldon clip. 640x640, true square, no letterbox."""
    layout = compute_layout(**CANVAS, source_aspect=SHELDON_ASPECT, gap_fraction=GAP)
    assert layout.fit_mode == "height"
    assert layout.panel_width == layout.panel_height, "a 1:1 source needs square panels"
    assert layout.is_pillarboxed
    # Horizontally centred, with equal background either side.
    assert layout.panel_x == (1080 - layout.panel_width) // 2
    assert layout.panel_x * 2 + layout.panel_width == 1080
    # The caption gap survives, and both margins clear the floor.
    assert layout.gap_height == 72
    floor = min_margin_for(1920)
    assert layout.top_margin >= floor and layout.bottom_margin >= floor
    assert layout.top_margin >= MIN_HEADER_HEIGHT


@pytest.mark.parametrize(
    "aspect", [SHELDON_ASPECT, 4 / 3, 1.2, 9 / 16, 0.75, 1.43]
)
def test_height_fit_closes_on_the_canvas_exactly(aspect):
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    assert layout.fit_mode == "height"
    total = (
        layout.top_margin
        + layout.panel_height
        + layout.gap_height
        + layout.panel_height
        + layout.bottom_margin
    )
    assert total == 1920
    assert layout.closes()


@pytest.mark.parametrize("aspect", [SHELDON_ASPECT, 4 / 3, 1.2, 9 / 16])
def test_height_fit_never_stretches_the_source(aspect):
    """The naive fix is to stretch panels to the canvas width. It would distort
    every face in the comparison while leaving a file that plays."""
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    assert layout.panel_aspect == pytest.approx(aspect, abs=0.01)
    assert layout.panel_width <= 1080


@pytest.mark.parametrize("aspect", [SHELDON_ASPECT, 4 / 3, 1.2, 9 / 16])
def test_height_fit_dimensions_are_all_even(aspect):
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    for name in ("panel_width", "panel_height", "gap_height", "top_margin", "bottom_margin"):
        assert getattr(layout, name) % 2 == 0, f"{name} is odd"


def test_the_two_branches_meet_continuously_at_the_cutoff():
    """No discontinuity: at the cutoff the height-fit panel is exactly canvas
    width, so a clip either side of it does not jump size."""
    cutoff = width_fit_cutoff(1080, 1920, even(GAP * 1920))
    just_below = compute_layout(**CANVAS, source_aspect=cutoff - 0.005, gap_fraction=GAP)
    just_above = compute_layout(**CANVAS, source_aspect=cutoff + 0.005, gap_fraction=GAP)
    assert just_below.fit_mode == "height"
    assert just_above.fit_mode == "width"
    assert abs(just_below.panel_width - just_above.panel_width) <= 4
    assert abs(just_below.panel_height - just_above.panel_height) <= 4


def test_an_unlayoutable_source_names_the_aspect():
    """A refusal with a diagnosis, not an assert deep in the compositor.

    Note what it takes to get here: on a 1080x1920 canvas the height-fit branch
    always finds room, even at the maximum legal gap fraction (a 308px panel).
    The refusal is reached by a canvas too SHORT to stack two usable panels,
    which is the realistic way an operator hits it.
    """
    with pytest.raises(LayoutError) as exc:
        compute_layout(
            canvas_width=1080, canvas_height=500, source_aspect=1.0, gap_fraction=0.037
        )
    message = str(exc.value)
    assert "1.000:1" in message, message
    assert "floor" in message and str(MIN_PANEL_HEIGHT) in message


def test_the_height_fit_branch_always_finds_room_on_the_shipped_canvas():
    """Documents the above: on 1080x1920 there is no gap fraction inside the
    legal range that makes a 1:1 source unlayoutable."""
    for fraction in (0.0, 0.037, 0.2, 0.4, 0.49):
        layout = compute_layout(
            **CANVAS, source_aspect=1.0, gap_fraction=fraction
        )
        assert layout.panel_height >= MIN_PANEL_HEIGHT


def test_a_source_too_tall_for_width_fit_is_still_refused_there():
    """The width-fit branch keeps its own floor; it is simply no longer reached
    by aspects the height-fit branch can serve."""
    from claypipe.pipeline.layout import _width_fit

    with pytest.raises(LayoutError, match=str(2 * MIN_HEADER_HEIGHT)):
        _width_fit(1080, 1920, 1.18, 72)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_non_positive_aspect_is_refused(bad):
    with pytest.raises(LayoutError, match="aspect must be positive"):
        compute_layout(**CANVAS, source_aspect=bad, gap_fraction=GAP)


@pytest.mark.parametrize("bad", [0.5, 0.9, -0.01])
def test_out_of_range_gap_fraction_is_refused(bad):
    with pytest.raises(LayoutError, match="gap_fraction"):
        compute_layout(**CANVAS, source_aspect=CLIP_A_ASPECT, gap_fraction=bad)


def test_source_aspect_of_rejects_degenerate_dimensions():
    assert source_aspect_of(1920, 1080) == pytest.approx(16 / 9)
    with pytest.raises(LayoutError):
        source_aspect_of(0, 1080)
    with pytest.raises(LayoutError):
        source_aspect_of(1920, -1)


def test_as_dict_reports_every_band():
    layout = compute_layout(**CANVAS, source_aspect=CLIP_A_ASPECT, gap_fraction=GAP)
    d = layout.as_dict()
    for key in (
        "panel_height", "gap_height", "top_margin", "bottom_margin",
        "restyled_y", "gap_y", "original_y", "source_aspect", "panel_aspect",
    ):
        assert key in d, key


def test_shipped_config_has_no_fixed_panel_geometry():
    """T9 retires header_height / panel_height / divider_height. `extra: forbid`
    means a leftover key in styles.yaml is a startup crash, so this asserts the
    fields are gone from the model, not merely unused."""
    render = load_styles().render
    for retired in ("header_height", "panel_height", "divider_height"):
        assert not hasattr(render, retired), f"{retired} survived T9"
    assert 0.0 <= render.caption_gap_fraction < 0.5


def test_shipped_config_resolves_both_reference_aspects():
    render = load_styles().render
    for aspect in (CLIP_A_ASPECT, CLIP_B_ASPECT):
        layout = render.layout_for(aspect)
        assert isinstance(layout, Layout)
        assert layout.closes()
