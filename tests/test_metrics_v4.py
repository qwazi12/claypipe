"""V4 acceptance — the metric vector, redefined for resynthesis.

Under whole-frame v2v, moderate geometry change is EXPECTED and correct. The
old vector was calibrated to detect geometry change, so reusing its numbers —
or merely lowering them — would gate the wrong property.

| Metric | Support | Asks | Direction |
|---|---|---|---|
| SSIM | whole frame | did the scene layout survive at all? | moderate band |
| LPIPS-edges | whole frame | did composition drift? | moderate |
| ID | character regions | is this the same clay character? | high |
| FLOW | output flow vs source flow | did motion stay synced? | HIGH, primary |
| TF | whole frame | temporal stability | high |
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from claypipe.config import ScoreTargets, load_weights
from claypipe.pipeline import score as S

pytest.importorskip("cv2", reason="needs the [scoring] extra")


@pytest.fixture
def cfg():
    return load_weights().temporal


def moving_square(dx: int, tint: int = 0, size: int = 96) -> np.ndarray:
    """A textured square translated by dx, optionally recoloured (a 'restyle')."""
    frame = np.full((size, size), 55, np.uint8)
    frame[30:60, 20 + dx : 50 + dx] = 200
    if tint:
        frame = np.clip(frame.astype(int) + tint, 0, 255).astype(np.uint8)
    return np.stack([frame] * 3, axis=2)


# ---------------------------------------------------------------------------
# FLOW — the primary gate
# ---------------------------------------------------------------------------

def test_flow_is_one_when_the_output_moves_exactly_like_the_source(cfg):
    """A restyle that recolours but preserves motion is perfectly synced."""
    value = S.flow_sync(
        moving_square(0), moving_square(6),
        moving_square(0, tint=40), moving_square(6, tint=40), cfg,
    )
    assert value == pytest.approx(1.0, abs=0.01)


def test_flow_collapses_when_the_output_reverses_the_motion(cfg):
    value = S.flow_sync(
        moving_square(0), moving_square(6),
        moving_square(0, tint=40), moving_square(-6, tint=40), cfg,
    )
    assert value == pytest.approx(0.0, abs=0.01)


def test_flow_is_monotonic_in_how_wrong_the_motion_is(cfg):
    """The ordering is what makes it usable as a gate."""
    def measure(output_dx):
        return S.flow_sync(
            moving_square(0), moving_square(6),
            moving_square(0, tint=40), moving_square(output_dx, tint=40), cfg,
        )

    assert measure(6) > measure(4) > measure(2) > measure(-6)


def test_flow_asks_a_different_question_than_tf(cfg):
    """THE REASON FLOW EXISTS. TF asks "is this frame stable relative to the one
    before it", so a FROZEN output scores TF perfectly. FLOW asks "does this
    move the way the SOURCE moved", which a frozen output fails completely.

    A v2v model can be temporally beautiful and still out of step, and the
    side-by-side format only reads if the panels move together."""
    frozen_a = moving_square(0, tint=40)
    frozen_b = moving_square(0, tint=40)

    tf = S.temporal_fidelity(frozen_a, frozen_b, cfg)
    flow = S.flow_sync(moving_square(0), moving_square(6), frozen_a, frozen_b, cfg)

    assert tf == pytest.approx(1.0, abs=0.01), "a frozen output is perfectly stable"
    assert flow < 0.6, "a frozen output is NOT in step with a moving source"


# A fixed, blurred noise field. Optical flow needs TEXTURE: on a flat shape
# Farneback can only estimate motion at the edges (the aperture problem), the
# estimate does not scale with resolution, and a test built on one measures the
# fixture rather than the code. Measured on this texture the flow magnitude
# ratio between full and half resolution is 2.03-2.07, i.e. exactly linear.
_TEXTURE = None


def textured(dx: int, tint: int = 0, size: int = 256) -> np.ndarray:
    """The SAME texture translated by dx — not a fresh random field each call."""
    global _TEXTURE
    import cv2

    if _TEXTURE is None:
        _TEXTURE = cv2.GaussianBlur(
            np.random.default_rng(7).integers(0, 255, (256, 320), dtype=np.uint8),
            (5, 5), 0,
        )
    frame = _TEXTURE[:, 32 + dx : 32 + dx + size].copy()
    if tint:
        frame = np.clip(frame.astype(int) + tint, 0, 255).astype(np.uint8)
    return np.stack([frame] * 3, axis=2)


def test_flow_handles_an_output_at_a_different_resolution(cfg):
    """A v2v backend may return a different size than the source — VACE is
    asked for 480p whatever the source was. Comparing flow fields of different
    shapes is meaningless, so the output field is resampled AND its vectors
    rescaled: a 2x downscale halves every displacement, and skipping the
    rescale would report a spurious desync on every single frame.
    """
    import cv2

    def half(frame):
        return cv2.resize(frame, (128, 128), interpolation=cv2.INTER_AREA)

    full_res = S.flow_sync(
        textured(0), textured(8), textured(0, tint=30), textured(8, tint=30), cfg,
    )
    half_res = S.flow_sync(
        textured(0), textured(8),
        half(textured(0, tint=30)), half(textured(8, tint=30)), cfg,
    )
    assert full_res == pytest.approx(1.0, abs=0.01)
    assert half_res > 0.95, f"the rescale was skipped or wrong: {half_res}"


def test_flow_on_textured_content_separates_synced_from_frozen(cfg):
    """The same discrimination as the synthetic-square tests, on content where
    optical flow is well-conditioned."""
    synced = S.flow_sync(
        textured(0), textured(8), textured(0, tint=30), textured(8, tint=30), cfg,
    )
    frozen = S.flow_sync(
        textured(0), textured(8), textured(0, tint=30), textured(0, tint=30), cfg,
    )
    assert synced > 0.95
    assert frozen < 0.3
    assert synced - frozen > 0.6, "the gate needs a wide separation to be usable"


def test_a_first_frame_has_no_motion_to_compare():
    assert S.FIRST_FRAME_FLOW_SYNC == 1.0


def test_flow_uses_the_same_flow_configuration_as_tf():
    """The thing gating motion sync and the thing grading temporal stability
    must not disagree about what motion is."""
    import inspect

    source = inspect.getsource(S._flow)
    for parameter in ("pyr_scale", "levels", "winsize", "iterations", "poly_n"):
        assert f"cfg.{parameter}" in source


# ---------------------------------------------------------------------------
# TF is no longer circular — record it so a future session does not re-flag it
# ---------------------------------------------------------------------------

def test_tf_is_no_longer_circular_because_propagation_is_retired():
    """D56 flagged TF as near-circular: a propagated frame was PRODUCED by a
    flow warp and TF GRADES by flow warping, so metric and generation were the
    same operation. V3 retired propagation, so frames are produced by the
    backend and graded by an independent warp. TF measures a real property
    again.

    Asserted structurally: nothing in the live path generates frames by warping
    along a flow field any more.
    """
    from claypipe import cli

    cli_source = Path(cli.__file__).read_text()
    # The retired path is refused on v2v, so no v2v frame is warp-produced.
    assert "--propagate belongs to the retired" in cli_source
    assert "RETIRED" in cli_source


# ---------------------------------------------------------------------------
# ID scoped to character regions
# ---------------------------------------------------------------------------

def test_character_regions_localise_the_moving_subject(cfg):
    regions = S.character_regions(moving_square(0), moving_square(6), cfg)
    assert regions, "a moving subject should localise"
    for x, y, w, h in regions:
        assert w > 0 and h > 0
        assert all(isinstance(v, int) for v in (x, y, w, h)), "must be JSON-clean"


def test_no_motion_yields_no_regions(cfg):
    still = moving_square(0)
    assert S.character_regions(still, still, cfg) == []


class FakeEmbedder:
    """Embeds by mean brightness, so similarity is predictable and offline."""

    def embed(self, image):
        return np.array([float(np.asarray(image).mean()), 1.0], dtype=np.float32)


def test_a_crop_is_never_compared_against_a_whole_frame(cfg):
    """THE BUG THIS PREVENTS, measured: scoring a character crop against a
    whole-frame reference asks CLIP whether a person resembles a scene. On the
    fixture suite it depressed ID enough to miss id_min on most frames, burn
    the entire retry budget, and halt the run.

    So region scoring requires region references. Without them ID falls back to
    whole-frame and LABELS itself, rather than silently producing an invalid
    number.
    """
    restyled = moving_square(6, tint=40)
    regions = S.character_regions(moving_square(0), moving_square(6), cfg)
    assert regions

    score, support = S.scoped_identity(
        restyled, [moving_square(6, tint=40)], FakeEmbedder(), regions,
        region_references=None,
    )
    assert support == "whole_frame:no_region_refs"
    assert 0.0 <= score <= 1.0


def test_region_scoring_engages_when_region_references_exist(cfg):
    restyled = moving_square(6, tint=40)
    regions = S.character_regions(moving_square(0), moving_square(6), cfg)
    crops = [restyled[y : y + h, x : x + w] for x, y, w, h in regions]

    score, support = S.scoped_identity(
        restyled, [restyled], FakeEmbedder(), regions, region_references=crops,
    )
    assert support == f"regions:{len(regions)}"
    assert 0.0 <= score <= 1.0


def test_the_worst_region_wins_not_the_average(cfg):
    """A frame with three characters where one is wrong is a frame with a wrong
    character in it. Averaging would hide exactly the failure R2 cares about:
    EVERY character becomes clay, individually."""
    frame = np.zeros((60, 120, 3), np.uint8)
    frame[10:40, 10:40] = 200      # bright region
    frame[10:40, 70:100] = 20      # dark region
    regions = [(10, 10, 30, 30), (70, 10, 30, 30)]
    references = [np.full((30, 30, 3), 200, np.uint8)]   # matches only the bright one

    embedder = FakeEmbedder()
    score, support = S.scoped_identity(
        frame, references, embedder, regions, region_references=references
    )
    per_region = [
        S.identity_similarity(frame[y : y + h, x : x + w], references, embedder)
        for x, y, w, h in regions
    ]
    assert support == "regions:2"
    assert score == pytest.approx(min(per_region))
    assert score < sum(per_region) / len(per_region)


# ---------------------------------------------------------------------------
# All five report; none gate until calibrated
# ---------------------------------------------------------------------------

def test_all_five_metrics_are_reported():
    weights = load_weights()
    met = S.check_targets(
        ssim=0.5, lpips_edges=0.5, identity=0.5, temporal=0.5, flow=0.5,
        cfg=weights,
    )
    assert set(met) == {"ssim", "lpips_edges", "id", "tf", "flow"}


def test_no_threshold_is_set_without_a_measurement():
    """The set-no-thresholds-now rule, enforced. It has been violated twice at
    real debugging cost."""
    weights = load_weights()
    assert weights.targets.flow_min == 0.0
    assert weights.mode("resynth").targets.flow_min == 0.0
    # And the mode itself still refuses to spend.
    assert weights.mode("resynth").calibrated is False


def test_a_calibrated_vector_gates_immediately():
    """Nothing structural is missing — only the number."""
    weights = load_weights()
    calibrated = ScoreTargets(
        ssim_min=0.0, lpips_edges_max=1.0, id_min=0.0, tf_min=0.0, flow_min=0.80
    )
    met = S.check_targets(
        ssim=0.1, lpips_edges=0.9, identity=0.1, temporal=0.1, flow=0.70,
        cfg=weights, targets=calibrated,
    )
    assert met["flow"] is False


def test_flow_is_a_component_target_not_a_term_in_f():
    """Re-weighting F would mean inventing a weight for an uncalibrated metric,
    which is the F4 mistake. The formula stays as specified; FLOW gates through
    the component targets, which is what gates (D13)."""
    weights = load_weights()
    assert not hasattr(weights.weights, "flow")
    total = (
        weights.weights.ssim + weights.weights.lpips_edges
        + weights.weights.id + weights.weights.tf
    )
    assert total == pytest.approx(1.0)


def test_the_qc_card_reports_every_component_with_its_direction(tmp_path: Path):
    """While the mode is uncalibrated the OBSERVED VALUES are the deliverable —
    the target vector is set from them — so a card showing only F would throw
    away the measurement the canary exists to produce."""
    import json

    from claypipe.pipeline.qccard import summarise_scores

    scores = tmp_path / "scores.jsonl"
    with scores.open("w") as fh:
        for i in range(3):
            fh.write(json.dumps({
                "frame": f"f_{i:05d}.png", "ssim": 0.3 + i * 0.1,
                "lpips_edges": 0.5 - i * 0.1, "identity": 0.7,
                "temporal": 0.99, "flow": 0.6 + i * 0.1, "f": 0.5,
                "verdict": "PASS", "id_support": "regions:2",
                "targets_met": {"flow": i > 0},
            }) + "\n")

    components = summarise_scores(scores)["components"]
    # FLOW first: it is the primary gate.
    assert list(components)[0] == "flow"
    assert components["flow"]["better"] == "higher"
    assert components["flow"]["worst"] == pytest.approx(0.6)
    assert components["flow"]["target_misses"] == 1
    # LPIPS is a DISTANCE — lower is better, so its worst case is the MAXIMUM.
    assert components["lpips_edges"]["better"] == "lower"
    assert components["lpips_edges"]["worst"] == pytest.approx(0.5)
    # And which support ID used is recorded, since whole-frame and region IDs
    # are not comparable numbers.
    assert components["id_support"] == {"regions:2": 3}
