"""Retry policy and cost firewall tests (Build Order step 3).

Every test here defends a specific line in the brief §9 coverage map or a
specific firewall in §7. Names are regression-grade on purpose: when one of
these fails, the name alone should say which protection just stopped working.

Nothing in this file touches a network or a paid API. The only backend that
exists is the free offline one, and the spend figures are configured prices,
not real charges.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claypipe.cli import app
from claypipe.config import load_weights
from claypipe.logging import RunLogger
from claypipe.pipeline import retry as R
from claypipe.pipeline.score import FrameScore, Verdict, VerdictReason
from claypipe.run import RunPaths

BASE_STRENGTH = 0.65
BASE_SEED = 1000


@pytest.fixture
def cfg():
    return load_weights()


@pytest.fixture
def paths(tmp_path: Path) -> RunPaths:
    p = RunPaths(tmp_path / "run")
    p.ensure()
    return p


@pytest.fixture
def logger(paths: RunPaths) -> RunLogger:
    return RunLogger("test-run", paths.log, echo=False)


def make_score(
    frame: str = "f_00001.png",
    *,
    f: float = 0.50,
    verdict: Verdict = Verdict.FAIL,
    reason: VerdictReason = VerdictReason.COMPOSITE_FAIL,
    targets_met: dict[str, bool] | None = None,
) -> FrameScore:
    """A FrameScore with the fields the retry policy actually reads.

    Component values are placeholders; the policy branches on verdict, reason
    and F, so those are what each test sets deliberately.
    """
    return FrameScore(
        frame=frame, ssim=0.5, lpips_edges=0.5, identity=0.5, temporal=0.5,
        f=f, verdict=verdict, reason=reason,
        targets_met=targets_met or {"ssim": True, "lpips_edges": True, "id": True, "tf": True},
    )


def controller(cfg, paths, logger, frame_count: int = 1000) -> R.RetryController:
    return R.RetryController(
        cfg=cfg, paths=paths, frame_count=frame_count,
        base_strength=BASE_STRENGTH, logger=logger,
    )


# --------------------------------------------------------------------------
# Firewall 2 — retry budget
# --------------------------------------------------------------------------

def test_max_retries_per_frame_cap(cfg, paths, logger) -> None:
    """DEFENDS: unbounded spend on one pathological frame (SPEC §4.2).

    A frame gets at most `max_retries_per_frame` attempts. The next time it is
    seen it goes to the human queue instead of consuming more API budget.
    """
    ctl = controller(cfg, paths, logger)
    cap = cfg.firewalls.max_retries_per_frame
    score = make_score()

    for expected_attempt in range(1, cap + 1):
        decision = ctl.decide(score, base_seed=BASE_SEED)
        assert decision.is_retry, f"attempt {expected_attempt} should retry"
        assert decision.attempt == expected_attempt

    escalated = ctl.decide(score, base_seed=BASE_SEED)
    assert escalated.action is R.RetryAction.HUMAN_QUEUE
    assert ctl.attempts_for(score.frame) == cap
    assert ctl.total_retries == cap, "the human queue must not consume more budget"


def test_total_retry_budget_pauses_run(cfg, paths, logger) -> None:
    """DEFENDS: a clip with a systemic prompt problem burning the whole budget
    one frame at a time (SPEC §4.2).

    Total retries are capped at 15% of frame count. Breaching pauses the run and
    writes an incident note for a human, rather than continuing to spend.
    """
    frame_count = 20
    ctl = controller(cfg, paths, logger, frame_count=frame_count)
    budget = ctl.retry_budget
    assert budget == int(frame_count * cfg.firewalls.total_retry_budget_fraction) == 3

    for i in range(budget):
        ctl.decide(make_score(f"f_{i:05d}.png"), base_seed=BASE_SEED)
    assert ctl.total_retries == budget

    with pytest.raises(R.RunHalted) as exc:
        ctl.decide(make_score("f_99999.png"), base_seed=BASE_SEED)

    assert exc.value.breach is R.Breach.RETRY_BUDGET_EXCEEDED
    assert exc.value.incident is not None and exc.value.incident.is_file()
    note = json.loads(exc.value.incident.read_text())
    assert note["breach"] == "retry_budget_exceeded"
    assert note["budget"] == budget and note["frame_count"] == frame_count


def test_retry_budget_scales_with_frame_count(cfg, paths, logger) -> None:
    """The budget is a fraction, not a constant: a 744-frame clip gets 111."""
    assert controller(cfg, paths, logger, frame_count=744).retry_budget == 111
    assert controller(cfg, paths, logger, frame_count=100).retry_budget == 15


# --------------------------------------------------------------------------
# Firewall 3 — kill switch
# --------------------------------------------------------------------------

def test_kill_switch_aborts_at_50_frames(cfg, paths, logger) -> None:
    """DEFENDS: discovering a bad run at frame 700 (SPEC §4.3).

    After the first 50 frames, a running mean F below 0.70 aborts the run with
    a report instead of spending the rest of the clip.
    """
    ctl = controller(cfg, paths, logger)
    ks = cfg.firewalls.kill_switch
    assert ks.after_frames == 50 and ks.min_mean_f == 0.70

    bad = 0.55
    for i in range(ks.after_frames - 1):
        ctl.observe(make_score(f"f_{i:05d}.png", f=bad))
    assert ctl.mean_f() == pytest.approx(bad), "no abort before the 50th frame"

    with pytest.raises(R.RunHalted) as exc:
        ctl.observe(make_score("f_00050.png", f=bad))

    assert exc.value.breach is R.Breach.KILL_SWITCH_MEAN_F
    note = json.loads(exc.value.incident.read_text())
    assert note["frames_scored"] == ks.after_frames
    assert note["mean_f"] == pytest.approx(bad, abs=1e-3)


def test_kill_switch_lets_a_good_run_through(cfg, paths, logger) -> None:
    """The switch must not fire on a healthy run, or it is just an outage."""
    ctl = controller(cfg, paths, logger)
    for i in range(cfg.firewalls.kill_switch.after_frames * 2):
        ctl.observe(make_score(f"f_{i:05d}.png", f=0.82))
    assert ctl.mean_f() == pytest.approx(0.82)


def test_kill_switch_ignores_an_early_bad_patch_that_recovers(cfg, paths, logger) -> None:
    """It is the RUNNING MEAN that gates, not any single frame."""
    ctl = controller(cfg, paths, logger)
    for i in range(10):
        ctl.observe(make_score(f"f_{i:05d}.png", f=0.30))
    for i in range(10, 50):
        ctl.observe(make_score(f"f_{i:05d}.png", f=0.95))
    assert ctl.mean_f() >= cfg.firewalls.kill_switch.min_mean_f


# --------------------------------------------------------------------------
# Firewall 1 — canary gate
# --------------------------------------------------------------------------

def test_batch_refuses_without_canary(tmp_path: Path, test_clip: Path) -> None:
    """DEFENDS: a full batch of paid calls behind an unreviewed prompt
    (SPEC §4.1, brief §7.1).

    `batch` must refuse to start when `canary_verdict.json` is absent. Driven
    through the real CLI, because a gate that the command does not consult is
    not a gate.
    """
    runner = CliRunner()
    runs_dir = tmp_path / "runs"
    intake = runner.invoke(
        app, ["intake", str(test_clip), "--style", "clay", "--runs-dir", str(runs_dir)]
    )
    assert intake.exit_code == 0, intake.output
    run_path = Path(intake.stdout.strip().splitlines()[-1])

    result = runner.invoke(app, ["batch", str(run_path), "--runs-dir", str(runs_dir)])

    assert result.exit_code == 1
    assert "canary gate" in result.output
    assert "canary_verdict.json" in result.output
    # And it refused BEFORE doing any work.
    assert not any((run_path / "frames" / "restyled").glob("*.png"))


def test_canary_gate_rejects_an_unapproved_verdict(paths: RunPaths) -> None:
    """A verdict file that exists but says no is still a refusal."""
    (paths.root / R.CANARY_VERDICT_NAME).write_text(json.dumps({"approved": False}))
    with pytest.raises(R.CanaryGateError) as exc:
        R.require_canary_approval(paths)
    assert exc.value.breach is R.Breach.CANARY_NOT_APPROVED


def test_canary_gate_rejects_an_unreadable_verdict(paths: RunPaths) -> None:
    """Corrupt JSON must never be read as approval."""
    (paths.root / R.CANARY_VERDICT_NAME).write_text("{not json")
    with pytest.raises(R.CanaryGateError) as exc:
        R.require_canary_approval(paths)
    assert exc.value.breach is R.Breach.CANARY_MISSING


def test_canary_gate_admits_an_approved_verdict(paths: RunPaths) -> None:
    (paths.root / R.CANARY_VERDICT_NAME).write_text(
        json.dumps({"approved": True, "reviewer": "operator"})
    )
    assert R.require_canary_approval(paths)["approved"] is True


# --------------------------------------------------------------------------
# Firewalls 4 and 5 — spend caps
# --------------------------------------------------------------------------

def ledger_for(paths, cfg, tmp_path, cap: float | None) -> R.SpendLedger:
    return R.SpendLedger(
        paths=paths, project_dir=tmp_path / "project", cfg=cfg.firewalls,
        run_id="test-run", max_cost_usd_run=cap,
    )


def test_spend_cap_halts_before_overrun(cfg, paths, tmp_path) -> None:
    """DEFENDS: an unbounded bill (SPEC §4.4).

    The cap is checked BEFORE the call is authorised, so the run halts on the
    call that WOULD breach it — the overrun never happens, rather than being
    detected after it is paid for.
    """
    price = cfg.firewalls.cost.per_call("fal")
    cap = price * 3.5           # room for exactly 3 calls
    ledger = ledger_for(paths, cfg, tmp_path, cap)

    for i in range(3):
        ledger.reconcile(
            ledger.authorize(frame=f"f_{i:05d}.png", backend="fal", stage="batch"), price
        )
    assert ledger.run_total() == pytest.approx(price * 3)

    with pytest.raises(R.RunHalted) as exc:
        ledger.authorize(frame="f_00004.png", backend="fal", stage="batch")

    assert exc.value.breach is R.Breach.SPEND_CAP_RUN
    # The blocked call was never charged.
    assert ledger.run_total() == pytest.approx(price * 3)
    note = json.loads(exc.value.incident.read_text())
    assert note["projected_usd"] > note["cap_usd"]


def test_project_spend_cap_halts_across_runs(cfg, paths, tmp_path) -> None:
    """DEFENDS: ten aborted runs at $19 each costing $190 (SPEC Addendum A3).

    The per-run cap cannot see previous runs. The project ledger is shared, so
    spend already booked by earlier runs counts against the project cap.
    """
    price = cfg.firewalls.cost.per_call("fal")
    project_cfg = cfg.firewalls.model_copy(
        update={"cost": cfg.firewalls.cost.model_copy(
            update={"max_cost_usd_project": price * 2.5}
        )}
    )
    project_dir = tmp_path / "project"

    first = R.SpendLedger(paths=paths, project_dir=project_dir, cfg=project_cfg,
                          run_id="run-one")
    for i in range(2):
        first.reconcile(first.authorize(frame=f"f_{i:05d}.png", backend="fal",
                                        stage="batch"), price)

    # A brand-new run with an empty run ledger, but the project total carries.
    second_paths = RunPaths(tmp_path / "run-two")
    second_paths.ensure()
    second = R.SpendLedger(paths=second_paths, project_dir=project_dir,
                           cfg=project_cfg, run_id="run-two")
    assert second.run_total() == 0.0
    assert second.project_total() == pytest.approx(price * 2)

    with pytest.raises(R.RunHalted) as exc:
        second.authorize(frame="f_00001.png", backend="fal", stage="batch")
    assert exc.value.breach is R.Breach.SPEND_CAP_PROJECT


def test_every_call_is_ledgered_before_execution(cfg, paths, tmp_path) -> None:
    """SPEC Hard Rules: every API call logged with cost BEFORE execution.

    `reconcile` needs the id that only `authorize` returns, so an unledgered
    call is not expressible.
    """
    ledger = ledger_for(paths, cfg, tmp_path, None)
    entry_id = ledger.authorize(frame="f_00001.png", backend="fal", stage="batch")

    records = [json.loads(l) for l in ledger.run_ledger.read_text().splitlines() if l.strip()]
    assert records[0]["event"] == "authorized"
    assert records[0]["estimated_usd"] == cfg.firewalls.cost.per_call("fal")
    # In-flight work is charged at estimate, never counted as free.
    assert ledger.run_total() == pytest.approx(cfg.firewalls.cost.per_call("fal"))

    with pytest.raises(ValueError, match="authorize"):
        ledger.reconcile("never-authorized", 1.0)

    ledger.reconcile(entry_id, 0.02)
    assert ledger.run_total() == pytest.approx(0.02), "the actual supersedes the estimate"


def test_unpriced_backend_is_refused_rather_than_assumed_free(cfg, paths, tmp_path) -> None:
    """An unknown price must never default to zero."""
    from claypipe.config import ConfigError

    ledger = ledger_for(paths, cfg, tmp_path, None)
    with pytest.raises(ConfigError, match="Refusing to spend"):
        ledger.authorize(frame="f_00001.png", backend="replicate", stage="batch")


# --------------------------------------------------------------------------
# Decision 1 wiring — the retry policy reads the verdict REASON
# --------------------------------------------------------------------------

def test_decision1_targets_missed_triggers_a_retry(cfg, paths, logger) -> None:
    """DEFENDS: the D9 gap — identity and temporal drift going unretried.

    A frame whose composite is comfortably above the pass line but which misses
    a component target must still be retried, exactly like a frame the composite
    rejected. Before Decision 1 this frame was a silent PASS.
    """
    ctl = controller(cfg, paths, logger)
    targets_missed = make_score(
        "f_00007.png", f=0.814, verdict=Verdict.BORDERLINE,
        reason=VerdictReason.TARGETS_MISSED,
        targets_met={"ssim": True, "lpips_edges": True, "id": False, "tf": True},
    )
    decision = ctl.decide(targets_missed, base_seed=BASE_SEED)

    assert decision.is_retry, "a missed component target must not be accepted"
    assert decision.action is R.RetryAction.RETRY_LOWER_STRENGTH
    assert "id" in decision.note
    assert ctl.total_retries == 1, "it consumes the same retry budget as any other"


def test_decision1_borderline_retries_at_lower_strength(cfg, paths, logger) -> None:
    """SPEC §3: a BORDERLINE frame is retried once at strength -0.10."""
    ctl = controller(cfg, paths, logger)
    decision = ctl.decide(
        make_score(f=0.70, verdict=Verdict.BORDERLINE,
                   reason=VerdictReason.COMPOSITE_BORDERLINE),
        base_seed=BASE_SEED,
    )
    assert decision.action is R.RetryAction.RETRY_LOWER_STRENGTH
    assert decision.strength == pytest.approx(
        BASE_STRENGTH + cfg.firewalls.borderline_strength_delta
    )
    assert decision.seed == BASE_SEED, "a borderline retry keeps the shot's seed"


def test_composite_fail_retries_with_a_new_seed(cfg, paths, logger) -> None:
    """SPEC §3: a FAIL is retried with a NEW SEED, not a nudged strength.

    A frame this far off is not a near miss, so the backend is given a
    genuinely different starting point rather than the same one turned down.
    """
    ctl = controller(cfg, paths, logger)
    decision = ctl.decide(
        make_score(f=0.40, verdict=Verdict.FAIL, reason=VerdictReason.COMPOSITE_FAIL),
        base_seed=BASE_SEED,
    )
    assert decision.action is R.RetryAction.RETRY_NEW_SEED
    assert decision.seed != BASE_SEED
    assert decision.strength == pytest.approx(BASE_STRENGTH), "strength is unchanged"


def test_passing_frames_consume_no_retry_budget(cfg, paths, logger) -> None:
    ctl = controller(cfg, paths, logger)
    decision = ctl.decide(
        make_score(f=0.95, verdict=Verdict.PASS, reason=VerdictReason.ACCEPTED),
        base_seed=BASE_SEED,
    )
    assert decision.action is R.RetryAction.ACCEPT
    assert ctl.total_retries == 0


def test_controller_summary_reports_what_a_human_needs(cfg, paths, logger) -> None:
    """Rule 35 'no silent work': the run must be able to say what it did."""
    ctl = controller(cfg, paths, logger, frame_count=100)
    for i in range(4):
        score = make_score(f"f_{i:05d}.png", f=0.80, verdict=Verdict.BORDERLINE,
                           reason=VerdictReason.COMPOSITE_BORDERLINE)
        ctl.observe(score)
        ctl.decide(score, base_seed=BASE_SEED)

    summary = ctl.summary()
    assert summary["frames_scored"] == 4
    assert summary["total_retries"] == 4
    assert summary["frames_retried"] == 4
    assert summary["retry_budget"] == 15
    assert summary["mean_f"] == pytest.approx(0.80)
