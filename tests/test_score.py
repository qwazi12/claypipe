"""Scoring tests (Build Order step 2).

Fixtures are the deterministic synthetic pairs in `tests/fixtures/`, one per
failure mode. Regenerate them with `python tests/fixtures/generate.py`.

On the expected bands: they are calibrated from measured values on exactly
these pixels, widened for margin — not divined a priori. The bands catch drift.
The ORDERING assertions below them are the semantic ones: they encode what each
metric is FOR, and they are what should fail loudly if a metric stops doing its
job. Where a test pins a known weakness rather than a desired property, it says
so in its name and docstring.

The two learned metrics (LPIPS, identity) need cached model weights. The test
process is locked offline in conftest, so those tests skip rather than download.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from claypipe.config import ScoreWeights, load_weights
from claypipe.pipeline import score as S

FIXTURES = Path(__file__).parent / "fixtures"

requires_models = pytest.mark.skipif(
    not S.models_are_cached(),
    reason="learned-metric model weights are not cached; warm them once with "
    "`python -c \"import lpips,open_clip; lpips.LPIPS(net='alex'); "
    "open_clip.create_model_and_transforms('ViT-B-32', pretrained='openai')\"` "
    "(needs network; tests themselves never download)",
)


@pytest.fixture(scope="session")
def cfg():
    return load_weights()


@pytest.fixture(scope="session")
def scorer(cfg):
    """One Scorer for the session — the models load once, not per test."""
    return S.Scorer.build(cfg)


def pair(case: str) -> tuple[np.ndarray, np.ndarray]:
    d = FIXTURES / case
    return S.load_image(d / "source.png"), S.load_image(d / "restyled.png")


def consecutive(case: str) -> tuple[np.ndarray, np.ndarray]:
    d = FIXTURES / case
    return S.load_image(d / "prev.png"), S.load_image(d / "curr.png")


# --------------------------------------------------------------------------
# Expected bands, per fixture, per metric. (lo, hi) inclusive.
# --------------------------------------------------------------------------
# fmt: off
BANDS: dict[str, dict[str, tuple[float, float]]] = {
    # A perfect restyle. Everything at ceiling.
    "identical":           {"ssim": (0.999, 1.000), "lpips_edges": (0.000, 0.005), "identity": (0.999, 1.000)},
    # Palette completely changed, geometry untouched: the case the whole
    # edge-map design exists to forgive.
    "recolored_only":      {"ssim": (0.980, 1.000), "lpips_edges": (0.000, 0.020), "identity": (0.850, 0.960)},
    # Geometry moved, palette untouched: the case it must catch.
    "edge_shifted":        {"ssim": (0.830, 0.940), "lpips_edges": (0.030, 0.150), "identity": (0.930, 1.000)},
    "gaussian_blurred":    {"ssim": (0.880, 0.980), "lpips_edges": (0.250, 0.420), "identity": (0.800, 0.930)},
    "face_swapped":        {"ssim": (0.920, 0.995), "lpips_edges": (0.020, 0.110), "identity": (0.790, 0.910)},
    "low_light":           {"ssim": (0.450, 0.600), "lpips_edges": (0.000, 0.030), "identity": (0.920, 1.000)},
    "neumorphic":          {"ssim": (0.900, 0.995), "lpips_edges": (0.000, 0.060), "identity": (0.890, 0.985)},
    "structure_destroyed": {"ssim": (0.760, 0.880), "lpips_edges": (0.120, 0.250), "identity": (0.580, 0.710)},
    "wrong_scene":         {"ssim": (0.550, 0.690), "lpips_edges": (0.500, 0.800), "identity": (0.570, 0.700)},
}
TEMPORAL_BANDS: dict[str, tuple[float, float]] = {
    "static_pair":  (0.990, 1.000),
    "high_motion":  (0.900, 0.960),
    "flicker_pair": (0.880, 0.945),
}
EXPECTED_VERDICT = {
    "identical": S.Verdict.PASS, "recolored_only": S.Verdict.PASS,
    "edge_shifted": S.Verdict.PASS, "gaussian_blurred": S.Verdict.PASS,
    "face_swapped": S.Verdict.PASS, "low_light": S.Verdict.PASS,
    "neumorphic": S.Verdict.PASS, "structure_destroyed": S.Verdict.PASS,
    "wrong_scene": S.Verdict.FAIL,
}
# fmt: on


def test_every_fixture_case_exists() -> None:
    for case in list(BANDS) + list(TEMPORAL_BANDS):
        assert (FIXTURES / case).is_dir(), f"missing fixture {case}"
    assert len(BANDS) + len(TEMPORAL_BANDS) >= 8


@pytest.mark.parametrize("case", sorted(BANDS))
def test_ssim_band(case: str) -> None:
    src, res = pair(case)
    lo, hi = BANDS[case]["ssim"]
    value = S.structural_similarity_score(src, res)
    assert lo <= value <= hi, f"{case}: SSIM {value:.3f} outside [{lo}, {hi}]"


@requires_models
@pytest.mark.parametrize("case", sorted(BANDS))
def test_lpips_edges_band(case: str, scorer, cfg) -> None:
    src, res = pair(case)
    lo, hi = BANDS[case]["lpips_edges"]
    value = S.lpips_edge_distance(src, res, scorer.perceptual, cfg.canny)
    assert lo <= value <= hi, f"{case}: LPIPS_edges {value:.3f} outside [{lo}, {hi}]"


@requires_models
@pytest.mark.parametrize("case", sorted(BANDS))
def test_identity_band(case: str, scorer) -> None:
    src, res = pair(case)
    lo, hi = BANDS[case]["identity"]
    value = S.identity_similarity(res, [src], scorer.embedder)
    assert lo <= value <= hi, f"{case}: ID {value:.3f} outside [{lo}, {hi}]"


@pytest.mark.parametrize("case", sorted(TEMPORAL_BANDS))
def test_temporal_band(case: str, cfg) -> None:
    prev, curr = consecutive(case)
    lo, hi = TEMPORAL_BANDS[case]
    value = S.temporal_fidelity(prev, curr, cfg.temporal)
    assert lo <= value <= hi, f"{case}: TF {value:.3f} outside [{lo}, {hi}]"


@requires_models
@pytest.mark.parametrize("case", sorted(EXPECTED_VERDICT))
def test_verdict_across_fixture_set(case: str, scorer) -> None:
    """Threshold logic returns PASS | BORDERLINE | FAIL across the fixtures."""
    src, res = pair(case)
    got = scorer.score_frame(
        frame=case, source=src, restyled=res, references=[src], previous_restyled=None
    )
    assert got.verdict is EXPECTED_VERDICT[case], (
        f"{case}: F={got.f:.3f} -> {got.verdict.value}, expected "
        f"{EXPECTED_VERDICT[case].value}"
    )


# --------------------------------------------------------------------------
# Failure mode 1 — structural drift
# --------------------------------------------------------------------------

@requires_models
def test_lpips_forgives_recolour_but_catches_geometry(scorer, cfg) -> None:
    """The load-bearing property of the whole scoring design.

    A correct claymation/LEGO restyle changes colour and texture completely and
    keeps geometry. Grading perceptual distance on edge maps must therefore
    forgive the recolour and punish the shift.
    """
    recolour = S.lpips_edge_distance(*pair("recolored_only"), scorer.perceptual, cfg.canny)
    shifted = S.lpips_edge_distance(*pair("edge_shifted"), scorer.perceptual, cfg.canny)

    assert recolour <= cfg.targets.lpips_edges_max, "a pure recolour was punished"
    assert shifted > recolour * 5, (
        f"geometry shift ({shifted:.3f}) must register far above a pure "
        f"recolour ({recolour:.3f})"
    )


@requires_models
def test_lpips_is_computed_on_edges_not_raw_colour(scorer, cfg) -> None:
    """Proves the edge-map step is doing the work, not decoration.

    The same recoloured pair scored on RAW COLOUR is a large distance; scored on
    edge maps it is ~zero. Delete the Canny step and this test fails.
    """
    src, res = pair("recolored_only")
    on_edges = S.lpips_edge_distance(src, res, scorer.perceptual, cfg.canny)
    on_colour = scorer.perceptual.distance(src, res)

    assert on_colour > cfg.targets.lpips_edges_max, (
        "fixture is not colour-different enough to prove the point"
    )
    assert on_edges < on_colour / 10, (
        f"edges {on_edges:.3f} vs raw colour {on_colour:.3f}: scoring raw colour "
        "would reject correct restyles"
    )


def test_ssim_ranks_structural_damage(cfg) -> None:
    identical = S.structural_similarity_score(*pair("identical"))
    shifted = S.structural_similarity_score(*pair("edge_shifted"))
    wrong = S.structural_similarity_score(*pair("wrong_scene"))
    assert identical > shifted > wrong


# --------------------------------------------------------------------------
# Failure mode 2 — temporal flicker
# --------------------------------------------------------------------------

def test_temporal_ranks_flicker_below_real_motion(cfg) -> None:
    """Motion is not flicker. Warping along the flow is what separates them."""
    static = S.temporal_fidelity(*consecutive("static_pair"), cfg.temporal)
    motion = S.temporal_fidelity(*consecutive("high_motion"), cfg.temporal)
    flicker = S.temporal_fidelity(*consecutive("flicker_pair"), cfg.temporal)

    assert static > motion, "a still pair must score above a moving one"
    assert flicker < motion, (
        f"flicker ({flicker:.3f}) must rank below real motion ({motion:.3f}); "
        "if it does not, the flow warp is not cancelling motion"
    )


def test_first_frame_of_a_shot_is_not_penalised(scorer) -> None:
    """No predecessor means no temporal evidence — not a temporal failure."""
    src, res = pair("identical")
    got = scorer.score_frame(
        frame="f_00001.png", source=src, restyled=res, references=[src],
        previous_restyled=None,
    ) if S.models_are_cached() else None
    if got is None:
        pytest.skip("learned models not cached")
    assert got.temporal == S.FIRST_FRAME_TEMPORAL_FIDELITY == 1.0


# --------------------------------------------------------------------------
# Failure mode 3 — identity drift
# --------------------------------------------------------------------------

@requires_models
def test_identity_drops_when_the_character_is_swapped(scorer) -> None:
    src, _ = pair("identical")
    _, swapped = pair("face_swapped")
    _, recoloured = pair("recolored_only")

    same = S.identity_similarity(src, [src], scorer.embedder)
    swapped_id = S.identity_similarity(swapped, [src], scorer.embedder)
    recoloured_id = S.identity_similarity(recoloured, [src], scorer.embedder)

    assert same > swapped_id, "swapping the character must reduce identity"
    assert recoloured_id > swapped_id, (
        "a recolour of the SAME character must score above a DIFFERENT character"
    )


@requires_models
def test_identity_takes_the_best_matching_reference(scorer) -> None:
    """Several locked references are poses of one character: best match wins."""
    src, _ = pair("identical")
    _, wrong = pair("wrong_scene")
    best = S.identity_similarity(src, [wrong, src], scorer.embedder)
    assert best == pytest.approx(
        S.identity_similarity(src, [src], scorer.embedder), abs=1e-6
    )


def test_identity_without_references_is_a_hard_error(scorer) -> None:
    """Rule 5: fail fast and say what is missing. Never score a guess."""
    src, _ = pair("identical")
    with pytest.raises(S.ScoringError, match="at least one Stage-0 reference"):
        S.identity_similarity(src, [], scorer.embedder)


# --------------------------------------------------------------------------
# Composite and thresholds
# --------------------------------------------------------------------------

def test_composite_matches_the_specified_formula(cfg) -> None:
    f = S.composite_f(ssim=0.80, lpips_edges=0.20, identity=0.90, temporal=0.85, cfg=cfg)
    expected = 0.40 * 0.80 + 0.25 * (1 - 0.20) + 0.20 * 0.90 + 0.15 * 0.85
    assert f == pytest.approx(expected)


def test_composite_reads_weights_from_config_not_code(cfg) -> None:
    """Rule 25: the FORMULA is fixed in code, the WEIGHTS are config.

    Re-weighting entirely onto SSIM must make F equal SSIM. If this fails,
    someone has hardcoded a weight.
    """
    ssim_only = cfg.model_copy(
        update={"weights": ScoreWeights(ssim=1.0, lpips_edges=0.0, id=0.0, tf=0.0)}
    )
    f = S.composite_f(ssim=0.42, lpips_edges=0.99, identity=0.01, temporal=0.02, cfg=ssim_only)
    assert f == pytest.approx(0.42)


def test_classify_covers_all_three_bands(cfg) -> None:
    assert S.classify(0.95, cfg) is S.Verdict.PASS
    assert S.classify(0.70, cfg) is S.Verdict.BORDERLINE
    assert S.classify(0.10, cfg) is S.Verdict.FAIL


def test_classify_boundaries_are_inclusive_at_the_bottom(cfg) -> None:
    """PASS at exactly 0.75, BORDERLINE at exactly 0.65 — per SPEC §3 bands."""
    p, b = cfg.thresholds.pass_, cfg.thresholds.borderline
    assert S.classify(p, cfg) is S.Verdict.PASS
    assert S.classify(p - 1e-9, cfg) is S.Verdict.BORDERLINE
    assert S.classify(b, cfg) is S.Verdict.BORDERLINE
    assert S.classify(b - 1e-9, cfg) is S.Verdict.FAIL


def test_targets_report_which_component_missed(cfg) -> None:
    """A verdict is far more actionable when it names the offending metric."""
    met = S.check_targets(ssim=0.30, lpips_edges=0.90, identity=0.99, temporal=0.99, cfg=cfg)
    assert met == {"ssim": False, "lpips_edges": False, "id": True, "tf": True}


def test_frames_of_different_sizes_are_rejected(cfg) -> None:
    src, _ = pair("identical")
    with pytest.raises(S.ScoringError, match="never change dimensions"):
        S.structural_similarity_score(src, src[:100, :100])


# --------------------------------------------------------------------------
# FINDINGS — these pin known weaknesses in the SPECIFIED formula.
# They assert what the system currently DOES, not what it should do. If the
# formula or thresholds are ever revised, these fail and force the change to be
# a conscious one. See memory.md D9 and D10.
# --------------------------------------------------------------------------

def test_finding_identity_or_temporal_alone_cannot_drop_below_pass(cfg) -> None:
    """FINDING (D9): with weights 0.40/0.25/0.20/0.15, no single component at
    its worst value can pull F out of the PASS band except SSIM.

    So failure modes 2 (flicker) and 3 (identity drift) are NOT gated by the
    composite. They are visible only through `targets_met`. This test documents
    that gap; it is not an endorsement of it.
    """
    perfect = dict(ssim=1.0, lpips_edges=0.0, identity=1.0, temporal=1.0)

    id_dead = S.composite_f(**{**perfect, "identity": 0.0}, cfg=cfg)
    tf_dead = S.composite_f(**{**perfect, "temporal": 0.0}, cfg=cfg)
    lpips_dead = S.composite_f(**{**perfect, "lpips_edges": 1.0}, cfg=cfg)
    ssim_dead = S.composite_f(**{**perfect, "ssim": 0.0}, cfg=cfg)

    assert S.classify(id_dead, cfg) is S.Verdict.PASS and id_dead == pytest.approx(0.80)
    assert S.classify(tf_dead, cfg) is S.Verdict.PASS and tf_dead == pytest.approx(0.85)
    assert S.classify(lpips_dead, cfg) is S.Verdict.PASS and lpips_dead == pytest.approx(0.75)
    assert S.classify(ssim_dead, cfg) is S.Verdict.FAIL and ssim_dead == pytest.approx(0.60)


@requires_models
def test_finding_per_component_targets_catch_what_the_composite_misses(scorer, cfg) -> None:
    """FINDING (D9), shown on a real fixture rather than in the abstract.

    `structure_destroyed` has visibly mangled geometry and a character the
    embedder no longer recognises (ID ~0.65 against a 0.85 target), yet the
    composite scores it a comfortable PASS.
    """
    src, res = pair("structure_destroyed")
    got = scorer.score_frame(
        frame="structure_destroyed", source=src, restyled=res,
        references=[src], previous_restyled=None,
    )
    assert got.verdict is S.Verdict.PASS
    assert got.identity < cfg.targets.id_min
    assert got.targets_met["id"] is False, "the target check is the only thing that catches it"


def test_finding_temporal_target_is_hard_to_breach(cfg) -> None:
    """FINDING (D10): TF is a whole-frame MEAN of the motion-compensated
    difference, so it saturates near 1.

    Breaching the 0.80 target needs consecutive frames differing by an average
    of >51 grey levels after motion compensation — catastrophic, not flicker.
    The severe-shimmer fixture scores well inside the target.
    """
    flicker = S.temporal_fidelity(*consecutive("flicker_pair"), cfg.temporal)
    assert flicker >= cfg.targets.tf_min, (
        "if this now fails, TF became more sensitive — revisit D10 and the band"
    )
    breach_level = (1.0 - cfg.targets.tf_min) * 255.0
    assert breach_level == pytest.approx(51.0)
