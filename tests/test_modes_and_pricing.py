"""T13 + T15 acceptance — per-mode gates and unit-aware pricing.

T13/A1: SSIM 0.72 is right for Track A (geometry preserved, structure is the
thing graded) and wrong for Track C (a resynthesised frame legitimately moves
geometry). One target vector cannot serve both.

T13/A4: a failed FRAME reseeds one frame; a failed CLIP CHUNK reseeds 81-240
frames at once. Sharing a retry cap would let one chunk failure eat the run.

T15/F3: a flat per-call price mis-prices two thirds of the candidate backends.
The cost table prices per frame; you pay per clip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from claypipe.config import (
    ConfigError,
    CostConfig,
    PriceModel,
    WeightsConfig,
    load_weights,
)
from claypipe.pipeline.restyle import ClipRestyleBackend, RestyleBackend


@pytest.fixture
def weights():
    return load_weights()


@pytest.fixture
def raw_weights():
    return yaml.safe_load(Path("weights.yaml").read_text())


# ---------------------------------------------------------------------------
# T13 — modes
# ---------------------------------------------------------------------------

def test_both_tracks_are_defined(weights):
    assert set(weights.modes) == {"surface", "resynth"}


def test_resynth_relaxes_structure_and_tightens_temporal(weights):
    """The shape of the difference is the whole point, and it is not "looser
    everywhere": temporal consistency is what Track C is bought for, so tf_min
    goes UP. A resynthesis model that flickers has no reason to exist."""
    surface = weights.mode("surface").targets
    resynth = weights.mode("resynth").targets

    assert resynth.ssim_min < surface.ssim_min, "resynth must tolerate moved geometry"
    assert resynth.lpips_edges_max > surface.lpips_edges_max
    assert resynth.id_min < surface.id_min
    assert resynth.tf_min > surface.tf_min, "tf_min must be STRICTER for resynth"


def test_resynth_retry_policy_is_tighter_because_a_chunk_costs_more(weights):
    """A4: one Track C retry can cost more than ten Track A retries."""
    surface = weights.mode("surface")
    resynth = weights.mode("resynth")
    assert resynth.max_retries_per_unit < surface.max_retries_per_unit
    assert resynth.total_retry_budget_fraction < surface.total_retry_budget_fraction


def test_surface_mode_restates_the_canonical_targets_and_cannot_drift(raw_weights):
    """One canonical number per component. The restatement exists so Track C
    can differ, not so the two can diverge — the validator is what makes that
    true rather than aspirational."""
    raw_weights["modes"]["surface"]["targets"]["ssim_min"] = 0.99
    with pytest.raises(Exception, match="does not match targets.ssim_min"):
        WeightsConfig.model_validate(raw_weights)


def test_surface_mode_retry_policy_cannot_drift_from_firewalls(raw_weights):
    raw_weights["modes"]["surface"]["max_retries_per_unit"] = 9
    with pytest.raises(Exception, match="max_retries_per_frame"):
        WeightsConfig.model_validate(raw_weights)


def test_unknown_mode_is_a_hard_error(weights):
    with pytest.raises(ConfigError, match="unknown mode"):
        weights.mode("nope")


# ---------------------------------------------------------------------------
# T13/F4 — the calibration firewall
# ---------------------------------------------------------------------------

def test_resynth_is_not_calibrated_and_says_so(weights):
    """Its numbers have the right SHAPE and no measurement behind them. The
    plan is explicit that these are placeholders, and F4 is the finding that
    shipping guessed thresholds on a paid backend is the failure mode."""
    cfg = weights.mode("resynth")
    assert cfg.calibrated is False
    assert "NEVER MEASURED" in cfg.calibration_note


def test_a_paid_run_in_an_uncalibrated_mode_is_refused(weights):
    with pytest.raises(ConfigError) as exc:
        weights.assert_mode_is_spendable("resynth")
    message = str(exc.value)
    assert "NOT CALIBRATED" in message
    # The refusal must name the way out, not just say no.
    assert "T16" in message
    assert "NEVER MEASURED" in message


def test_surface_is_spendable(weights):
    assert weights.assert_mode_is_spendable("surface").calibrated is True


def test_an_uncalibrated_mode_must_explain_itself(raw_weights):
    """A future session must be able to tell a placeholder from a measurement."""
    raw_weights["modes"]["resynth"]["calibration_note"] = ""
    with pytest.raises(Exception, match="calibration_note"):
        WeightsConfig.model_validate(raw_weights)


def test_batch_refuses_a_paid_run_in_an_uncalibrated_mode(test_clip: Path, tmp_path: Path):
    """End to end through the CLI: FIREWALL 0b."""
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.config import load_styles
    from claypipe.run import Run

    runner = CliRunner()
    styles = load_styles()
    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="fal", mode="resynth",
        clip_title="Uncalibrated", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=styles, runs_dir=tmp_path / "runs", echo=False,
    )
    (run.paths.root / "canary_verdict.json").write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test"})
    )
    result = runner.invoke(
        app, ["batch", str(run.paths.root), "--runs-dir", str(tmp_path / "runs"),
              "--live"],
        env={"FAL_KEY": "test-not-a-real-key"},
    )
    assert result.exit_code != 0
    # It must fail on calibration, not stumble into a later error first.
    assert "NOT CALIBRATED" in result.output


# ---------------------------------------------------------------------------
# T15 — unit-aware pricing
# ---------------------------------------------------------------------------

def test_the_three_units_are_all_representable(weights):
    units = {b: weights.firewalls.cost.unit_for(b) for b in weights.firewalls.cost.pricing}
    assert "image" in units.values()
    assert "megapixel" in units.values()
    assert "video_second" in units.values()


def test_a_video_second_backend_prices_a_whole_clip(weights):
    """MASTER_PLAN §3: Wan VACE 480p is $2.40 for a 60-second clip."""
    cost = weights.firewalls.cost.price_for("wan_vace_480p", video_seconds=60)
    assert cost == pytest.approx(2.40)


def test_the_megapixel_round_up_trap_is_modelled(weights):
    """fal rounds UP to the next whole megapixel, so 640x640 (0.41MP) pays the
    1MP rate. This inverts an optimisation: on a hosted per-MP backend the
    cheap move is to render LARGE, because you pay for 1MP either way."""
    cost = weights.firewalls.cost
    small = cost.price_for("fal_kontext_dev", megapixels=0.41)   # 640x640
    one_mp = cost.price_for("fal_kontext_dev", megapixels=1.0)
    assert small == one_mp == pytest.approx(0.025)
    # And 1.05MP is billed as 2MP, not 1.05.
    assert cost.price_for("fal_kontext_dev", megapixels=1.05) == pytest.approx(0.05)


def test_pricing_a_unit_backend_without_its_dimension_is_refused():
    """Defaulting the dimension to 1 would under-report spend by whatever the
    real size or duration was, and leave the caps not binding."""
    per_second = PriceModel(unit="video_second", rate=0.04)
    with pytest.raises(ConfigError, match="duration must be supplied"):
        per_second.cost()

    per_mp = PriceModel(unit="megapixel", rate=0.025, round_up_to_mp=True)
    with pytest.raises(ConfigError, match="geometry must be supplied"):
        per_mp.cost()


def test_per_call_refuses_a_non_per_image_backend(weights):
    """Answering would hand back a per-image figure for a per-video-second
    charge — a ledger that reconciles to the wrong number."""
    with pytest.raises(ConfigError, match="bills per video_second"):
        weights.firewalls.cost.per_call("wan_vace_480p")


def test_an_unpriced_backend_is_still_refused(weights):
    """The pre-T15 guarantee must survive T15: an unknown price is never
    silently treated as free."""
    with pytest.raises(ConfigError, match="Refusing to spend"):
        weights.firewalls.cost.per_call("some_backend_nobody_priced")
    with pytest.raises(ConfigError, match="Refusing to spend"):
        weights.firewalls.cost.price_for("some_backend_nobody_priced")


def test_round_up_to_mp_is_rejected_on_the_wrong_unit():
    with pytest.raises(Exception, match="meaningless for unit"):
        PriceModel(unit="image", rate=0.04, round_up_to_mp=True)


def test_a_backend_priced_twice_must_agree():
    """One canonical number per backend, or the ledger and the backend quote
    different prices for the same call."""
    with pytest.raises(Exception, match="priced twice and the two disagree"):
        CostConfig.model_validate(
            {
                "estimated_usd_per_call": {"fal": 0.035},
                "pricing": {"fal": {"unit": "image", "rate": 0.08}},
            }
        )


def test_the_shipped_per_image_prices_agree_with_the_pricing_block(weights):
    cost = weights.firewalls.cost
    for backend, model in cost.pricing.items():
        if model.unit == "image" and backend in cost.estimated_usd_per_call:
            assert cost.estimated_usd_per_call[backend] == pytest.approx(model.rate)


def test_ledger_records_the_unit_and_the_billed_dimension(test_clip: Path, tmp_path: Path):
    """Reconciling a bill against a ledger that says only "$0.05" cannot tell a
    2-megapixel image from two 1-megapixel ones."""
    from claypipe.config import load_styles
    from claypipe.pipeline.retry import SpendLedger
    from claypipe.run import Run

    styles = load_styles()
    weights = load_weights()
    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="fal",
        clip_title="Ledger Units", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=styles, runs_dir=tmp_path / "runs", echo=False,
    )
    ledger = SpendLedger(
        paths=run.paths, project_dir=tmp_path / "runs", cfg=weights.firewalls,
        run_id=run.run_id, logger=run.logger,
    )
    ledger.authorize(
        frame="clip_0001-0081", backend="wan_vace_480p", stage="batch",
        video_seconds=6.75,
    )
    records = [
        json.loads(line)
        for line in (run.paths.root / "spend_ledger.jsonl").read_text().splitlines()
    ]
    entry = records[0]
    assert entry["unit"] == "video_second"
    assert entry["video_seconds"] == pytest.approx(6.75)
    assert entry["estimated_usd"] == pytest.approx(0.27)
    assert ledger.run_total() == pytest.approx(0.27)


# ---------------------------------------------------------------------------
# T13/A4 — two protocols, one seam
# ---------------------------------------------------------------------------

def test_the_two_backend_protocols_are_separate():
    """Deliberately NOT a generalisation of one another. They look similar and
    behave nothing alike — retry granularity, timing, and failure modes all
    differ. Collapsing them would hide all three behind a shared signature."""
    assert RestyleBackend is not ClipRestyleBackend
    per_frame = set(RestyleBackend.__protocol_attrs__)
    per_clip = set(ClipRestyleBackend.__protocol_attrs__)
    assert "restyle" in per_frame and "restyle" not in per_clip
    assert "restyle_clip" in per_clip and "restyle_clip" not in per_frame
    # The clip protocol must expose its chunk bounds and native rate: a caller
    # that cannot ask "how many frames will you take" cannot chunk safely.
    for attr in ("min_chunk_frames", "max_chunk_frames", "native_fps"):
        assert attr in per_clip, attr


def test_a_per_frame_backend_does_not_satisfy_the_clip_protocol():
    from claypipe.pipeline.restyle import DummyBackend

    assert isinstance(DummyBackend(), RestyleBackend)
    assert not isinstance(DummyBackend(), ClipRestyleBackend)


# ---------------------------------------------------------------------------
# Reporting — Rule 40
# ---------------------------------------------------------------------------

def test_mode_is_persisted_on_the_run(test_clip: Path, tmp_path: Path):
    from claypipe.config import load_styles
    from claypipe.run import Run

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="resynth",
        clip_title="Mode Persisted", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    assert Run.load(run.paths.root, echo=False).manifest.mode == "resynth"


def test_mode_appears_in_the_qc_card(test_clip: Path, tmp_path: Path):
    from claypipe.config import load_styles
    from claypipe.pipeline import qccard
    from claypipe.run import Run

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="surface",
        clip_title="QC Mode", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    card = qccard.build_card(run, frames=60, verdict="assembled")
    assert card["mode"] == "surface"


def test_status_states_the_mode_and_its_calibration(test_clip: Path, tmp_path: Path):
    from typer.testing import CliRunner

    from claypipe.cli import app
    from claypipe.config import load_styles
    from claypipe.run import Run

    run = Run.create(
        source=test_clip, style="clay", fps=12, backend="dummy", mode="resynth",
        clip_title="Status Mode", duration_s=5.0,
        source_width=1280, source_height=720,
        styles=load_styles(), runs_dir=tmp_path / "runs", echo=False,
    )
    result = CliRunner().invoke(
        app, ["status", str(run.paths.root), "--runs-dir", str(tmp_path / "runs")]
    )
    assert result.exit_code == 0, result.output
    assert "mode       resynth (NOT CALIBRATED" in result.output


def test_intake_refuses_an_unknown_mode(test_clip: Path, tmp_path: Path):
    from typer.testing import CliRunner

    from claypipe.cli import app

    result = CliRunner().invoke(
        app, ["intake", str(test_clip), "--style", "clay", "--mode", "nonsense",
              "--runs-dir", str(tmp_path / "runs")],
    )
    assert result.exit_code != 0
    assert "unknown mode" in result.output
