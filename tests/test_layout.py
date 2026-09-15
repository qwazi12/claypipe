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
    Layout,
    LayoutError,
    compute_layout,
    even,
    source_aspect_of,
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


@pytest.mark.parametrize("aspect", [CLIP_A_ASPECT, CLIP_B_ASPECT, 2.0, 4 / 3, 1.5, 2.4])
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


@pytest.mark.parametrize("aspect", [CLIP_A_ASPECT, CLIP_B_ASPECT, 2.0, 4 / 3, 1.5, 2.4])
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


@pytest.mark.parametrize("aspect", [CLIP_A_ASPECT, CLIP_B_ASPECT, 2.0, 4 / 3, 2.4])
def test_panel_pair_is_block_centred(aspect):
    """Measured margin delta on the reference clips was +3px and -3px — noise
    around a centred block, not a rule. Derivation is exact to <=2px, which is
    the most a single even-rounding can cost."""
    layout = compute_layout(**CANVAS, source_aspect=aspect, gap_fraction=GAP)
    assert abs(layout.top_margin - layout.bottom_margin) <= 2


@pytest.mark.parametrize("aspect", [CLIP_A_ASPECT, CLIP_B_ASPECT, 2.0, 4 / 3, 2.4])
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


def test_square_source_is_refused_not_squeezed():
    """Two 1080px panels cannot fit a 1920px canvas. That is a refusal with a
    diagnosis, not a clipped header."""
    with pytest.raises(LayoutError, match="leaves only"):
        compute_layout(**CANVAS, source_aspect=1.0, gap_fraction=GAP)


def test_header_floor_is_enforced():
    """A source tall enough to squeeze the header below legibility is refused."""
    # 1080/1.18 = 915 per panel -> 1830 + gap leaves under 2*96.
    with pytest.raises(LayoutError, match=str(2 * MIN_HEADER_HEIGHT)):
        compute_layout(**CANVAS, source_aspect=1.18, gap_fraction=GAP)


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
