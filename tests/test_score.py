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
# Verdicts under DECISION 1 (PASS = F >= 0.75 AND all component targets met)
# and DECISION 2 (tf_min 0.80 -> 0.95). Three fixtures moved PASS -> BORDERLINE
# when Decision 1 landed; each is annotated with which target it misses.
EXPECTED_VERDICT = {
    "identical": S.Verdict.PASS,
    "recolored_only": S.Verdict.PASS,
    "edge_shifted": S.Verdict.PASS,
    "gaussian_blurred": S.Verdict.BORDERLINE,     # ID 0.845 vs 0.85 target
    "face_swapped": S.Verdict.PASS,
    "low_light": S.Verdict.BORDERLINE,            # SSIM 0.521 vs 0.72 target
    "neumorphic": S.Verdict.PASS,
    "structure_destroyed": S.Verdict.BORDERLINE,  # ID 0.653 vs 0.85 target
    "wrong_scene": S.Verdict.FAIL,
}
EXPECTED_MISSED_TARGETS = {
    "gaussian_blurred": ["id"],
    "low_light": ["ssim"],
    "structure_destroyed": ["id"],
    "wrong_scene": ["id", "lpips_edges", "ssim"],
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
    assert got.missed_targets == EXPECTED_MISSED_TARGETS.get(case, [])


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


ALL_TARGETS_MET = {"ssim": True, "lpips_edges": True, "id": True, "tf": True}


def test_classify_covers_all_three_bands(cfg) -> None:
    assert S.classify(0.95, cfg, ALL_TARGETS_MET)[0] is S.Verdict.PASS
    assert S.classify(0.70, cfg, ALL_TARGETS_MET)[0] is S.Verdict.BORDERLINE
    assert S.classify(0.10, cfg, ALL_TARGETS_MET)[0] is S.Verdict.FAIL


def test_classify_boundaries_are_inclusive_at_the_bottom(cfg) -> None:
    """PASS at exactly 0.75, BORDERLINE at exactly 0.65 — per SPEC §3 bands."""
    p, b = cfg.thresholds.pass_, cfg.thresholds.borderline
    assert S.classify(p, cfg, ALL_TARGETS_MET)[0] is S.Verdict.PASS
    assert S.classify(p - 1e-9, cfg, ALL_TARGETS_MET)[0] is S.Verdict.BORDERLINE
    assert S.classify(b, cfg, ALL_TARGETS_MET)[0] is S.Verdict.BORDERLINE
    assert S.classify(b - 1e-9, cfg, ALL_TARGETS_MET)[0] is S.Verdict.FAIL


def test_targets_report_which_component_missed(cfg) -> None:
    """A verdict is far more actionable when it names the offending metric."""
    met = S.check_targets(ssim=0.30, lpips_edges=0.90, identity=0.99, temporal=0.99, cfg=cfg)
    assert met == {"ssim": False, "lpips_edges": False, "id": True, "tf": True}


def test_frames_of_different_sizes_are_rejected(cfg) -> None:
    src, _ = pair("identical")
    with pytest.raises(S.ScoringError, match="never change dimensions"):
        S.structural_similarity_score(src, src[:100, :100])


# --------------------------------------------------------------------------
# DECISION 1 (memory.md D13) — PASS = F >= 0.75 AND all component targets met.
# DECISION 2 (memory.md D14) — tf_min raised 0.80 -> 0.95.
# These tests pin the decided behaviour by name, so a later step cannot drift
# away from it silently.
# --------------------------------------------------------------------------

def test_decision1_component_miss_downgrades_pass_to_borderline(cfg) -> None:
    """A frame above the pass line that misses one target is BORDERLINE."""
    missed_id = {**ALL_TARGETS_MET, "id": False}
    verdict, reason = S.classify(0.814, cfg, missed_id)
    assert verdict is S.Verdict.BORDERLINE
    assert reason is S.VerdictReason.TARGETS_MISSED


def test_decision1_component_miss_alone_is_never_fail(cfg) -> None:
    """Gate order: targets can push a frame to BORDERLINE, never past it.

    The only route to FAIL is the composite. A frame that misses every target
    but sits above the borderline line stays BORDERLINE.
    """
    all_missed = {k: False for k in ALL_TARGETS_MET}
    verdict, reason = S.classify(cfg.thresholds.borderline, cfg, all_missed)
    assert verdict is S.Verdict.BORDERLINE
    assert reason is S.VerdictReason.TARGETS_MISSED

    # ...and below the borderline line the COMPOSITE is what fails it.
    verdict, reason = S.classify(cfg.thresholds.borderline - 1e-9, cfg, all_missed)
    assert verdict is S.Verdict.FAIL
    assert reason is S.VerdictReason.COMPOSITE_FAIL


def test_decision1_composite_alone_still_cannot_gate_identity_or_temporal(cfg) -> None:
    """Why Decision 1 was needed, kept as executable documentation.

    With component targets switched off, driving any single component to its
    worst value leaves F inside PASS except for SSIM. That gap is exactly what
    Decision 1 closes.
    """
    composite_only = cfg.model_copy(
        update={
            "thresholds": cfg.thresholds.model_copy(
                update={"require_component_targets": False}
            )
        }
    )
    perfect = dict(ssim=1.0, lpips_edges=0.0, identity=1.0, temporal=1.0)
    all_missed = {k: False for k in ALL_TARGETS_MET}

    for component, worst, expected_f in [
        ("identity", 0.0, 0.80),
        ("temporal", 0.0, 0.85),
        ("lpips_edges", 1.0, 0.75),
    ]:
        f = S.composite_f(**{**perfect, component: worst}, cfg=cfg)
        assert f == pytest.approx(expected_f)
        assert S.classify(f, composite_only, all_missed)[0] is S.Verdict.PASS
        # Under Decision 1 the same frame is caught.
        assert S.classify(f, cfg, all_missed)[0] is S.Verdict.BORDERLINE

    ssim_dead = S.composite_f(**{**perfect, "ssim": 0.0}, cfg=cfg)
    assert ssim_dead == pytest.approx(0.60)
    assert S.classify(ssim_dead, composite_only, all_missed)[0] is S.Verdict.FAIL


@requires_models
def test_decision1_structure_destroyed_is_borderline_not_pass(scorer, cfg) -> None:
    """The fixture that motivated Decision 1, pinned end to end.

    Mangled geometry and a character the embedder no longer recognises
    (ID ~0.65 against a 0.85 target). It scored a comfortable PASS at F=0.814
    under the composite alone; it is BORDERLINE now.
    """
    src, res = pair("structure_destroyed")
    got = scorer.score_frame(
        frame="structure_destroyed", source=src, restyled=res,
        references=[src], previous_restyled=None,
    )
    assert got.f >= cfg.thresholds.pass_, "F itself is still above the pass line"
    assert got.identity < cfg.targets.id_min
    assert got.verdict is S.Verdict.BORDERLINE
    assert got.reason is S.VerdictReason.TARGETS_MISSED
    assert got.missed_targets == ["id"]


@requires_models
def test_decision2_flicker_is_no_longer_a_missed_target_that_passes(scorer, cfg) -> None:
    """Severe shimmer must now be caught by the TF target.

    At the old tf_min of 0.80 this fixture scored 0.916 and sailed through the
    very target meant to catch it. At 0.95 it is flagged.
    """
    prev, curr = consecutive("flicker_pair")
    got = scorer.score_frame(
        frame="flicker_pair", source=prev, restyled=curr,
        references=[prev], previous_restyled=prev,
    )
    assert got.temporal < cfg.targets.tf_min
    assert got.verdict is S.Verdict.BORDERLINE
    assert got.reason is S.VerdictReason.TARGETS_MISSED
    assert "tf" in got.missed_targets


def test_decision2_tf_target_is_now_expressive(cfg) -> None:
    """static > real motion > flicker, with the target cutting between them."""
    static = S.temporal_fidelity(*consecutive("static_pair"), cfg.temporal)
    motion = S.temporal_fidelity(*consecutive("high_motion"), cfg.temporal)
    flicker = S.temporal_fidelity(*consecutive("flicker_pair"), cfg.temporal)

    assert static > motion > flicker
    assert cfg.targets.tf_min == 0.95
    assert static >= cfg.targets.tf_min, "a still pair must clear the target"
    assert flicker < cfg.targets.tf_min, "severe shimmer must breach it"


def test_finding_d15_tf_target_also_flags_legitimate_fast_motion(cfg) -> None:
    """FINDING (D15), OPEN: tf_min=0.95 does not separate motion from flicker.

    Real motion scores 0.932 and severe flicker 0.916 — 0.016 apart. Any
    threshold that catches the flicker also catches the motion, so fast action
    shots will generate human-review load. Decision 2 was applied as directed;
    this records the consequence rather than quietly absorbing it.
    """
    motion = S.temporal_fidelity(*consecutive("high_motion"), cfg.temporal)
    flicker = S.temporal_fidelity(*consecutive("flicker_pair"), cfg.temporal)

    assert motion < cfg.targets.tf_min, "legitimate motion is flagged"
    assert abs(motion - flicker) < 0.05, (
        "motion and flicker are too close for one threshold to separate them"
    )
