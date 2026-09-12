"""Scorer + RetryController wiring into cli.batch (step 5, commit 2 — D25).

Step 2 built the scorer and Step 3 built the retry policy; until now neither was
consumed by the command that spends money. These tests prove they are.

The non-dummy path is exercised with a STUB backend registered in-process. No
network, no fal.ai, no spend — a stub is enough to prove the wiring, and the
real endpoint is a manual operator step by design.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageFilter

import pytest
from typer.testing import CliRunner

import claypipe.cli as cli
from claypipe.cli import app
from claypipe.pipeline import restyle as R
from claypipe.pipeline.score import models_are_cached
from claypipe.verdi import loaders as L

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"

requires_models = pytest.mark.skipif(
    not models_are_cached(), reason="learned-metric model weights are not cached"
)


class StubBackend:
    """A priced backend that is NOT `dummy`, so the scoring path engages.

    Copies the source frame through, which is what a PERFECT restyle would
    score like: SSIM 1.0, LPIPS 0.0, identity 1.0 against a reference drawn
    from the same clip. That is the point — this stub exists to prove the
    wiring, not to exercise the metrics, which have their own fixtures.

    Records every call so a test can assert what strength and seed the retry
    policy actually asked for.
    """

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def cost_per_frame_usd(self) -> float:
        return 0.01

    def restyle(self, src, dst, *, prompt, strength, seed):
        self.calls.append({"frame": src.name, "strength": strength, "seed": seed})
        shutil.copyfile(src, dst)


class DegradedStub(StubBackend):
    """Fails its targets for the first `bad_attempts` calls on each frame.

    Used to prove the retry decision actually reaches the backend, and that a
    backend which never recovers trips the whole-run retry budget.
    """

    def __init__(self, bad_attempts: int = 1) -> None:
        super().__init__()
        self.bad_attempts = bad_attempts
        self._seen: dict[str, int] = {}

    def restyle(self, src, dst, *, prompt, strength, seed):
        self.calls.append({"frame": src.name, "strength": strength, "seed": seed})
        count = self._seen.get(src.name, 0)
        self._seen[src.name] = count + 1
        if count < self.bad_attempts:
            # Geometry destroyed -> misses SSIM and LPIPS targets.
            with Image.open(src) as img:
                img.convert("RGB").rotate(35).filter(
                    ImageFilter.GaussianBlur(9)
                ).save(dst)
        else:
            shutil.copyfile(src, dst)


@pytest.fixture
def stub(monkeypatch) -> StubBackend:
    """Register a priced, non-dummy backend for the duration of one test.

    The stub has to be PRICED, because Step 3's ledger refuses to authorise a
    call against a backend with no configured cost. That refusal is deliberate,
    so the price is injected into the loaded config here rather than added to
    the shipped weights.yaml — test scaffolding must not leak into the config
    the firewalls enforce against in production.
    """
    backend = StubBackend()
    monkeypatch.setattr(
        cli, "get_backend",
        lambda name, **kw: backend if name == "stub" else R.get_backend(name, **kw),
    )

    real_load_all = cli.load_all

    def priced_load_all():
        styles, weights = real_load_all()
        weights.firewalls.cost.estimated_usd_per_call["stub"] = 0.01
        return styles, weights

    monkeypatch.setattr(cli, "load_all", priced_load_all)
    return backend


def test_batch_refuses_an_unpriced_backend(tmp_path, test_clip, monkeypatch) -> None:
    """An unknown price is never assumed free (SPEC §4.4).

    Registered WITHOUT the price injection above, so the ledger's refusal is
    what this test observes.
    """
    backend = StubBackend()
    monkeypatch.setattr(
        cli, "get_backend",
        lambda name, **kw: backend if name == "stub" else R.get_backend(name, **kw),
    )
    run_path = make_run(tmp_path, test_clip, backend="stub", refs=[FIXTURES / "identical" / "source.png"])
    approve(run_path)
    result = batch(run_path)
    assert result.exit_code == 1
    assert "Refusing to spend against an unknown price" in result.output


@pytest.fixture
def clip_reference(tmp_path: Path, test_clip: Path) -> Path:
    """A Stage-0 reference drawn from the clip itself.

    Identity is measured against what the operator LOCKED, so a reference from
    an unrelated image would (correctly) fail every frame. Using frame 1 of the
    same clip is what locking a character actually looks like.
    """
    dst = tmp_path / "reference.png"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(test_clip),
         "-vf", "fps=12", "-frames:v", "1", str(dst)],
        check=True, capture_output=True,
    )
    return dst


def make_run(tmp_path: Path, test_clip: Path, backend: str = "dummy", refs=()) -> Path:
    args = ["intake", str(test_clip), "--style", "clay",
            "--runs-dir", str(tmp_path / "runs"), "--backend", backend]
    for r in refs:
        args += ["--ref", str(r)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return Path(result.stdout.strip().splitlines()[-1])


def approve(run_path: Path) -> None:
    (run_path / L.CANARY_VERDICT_NAME).write_text(
        json.dumps({"schema_version": 1, "approved": True, "decider": "test", "frames": {}})
    )


def batch(run_path: Path, *extra):
    return runner.invoke(
        app, ["batch", str(run_path), "--runs-dir", str(run_path.parent), *extra]
    )


def log_events(run_path: Path, name: str) -> list[dict]:
    lines = (run_path / "logs" / "run.jsonl").read_text().splitlines()
    return [json.loads(l) for l in lines if l.strip() and json.loads(l)["event"] == name]


def test_dummy_backend_skips_scoring_silently(tmp_path, test_clip) -> None:
    """D27: the dummy produces no generative content, so it is not graded.

    'Silently' means no scores are fabricated — NOT that the skip is hidden.
    The skip is logged and printed, because a run that reads as unscored is
    honest and a run that reads as all-PASS would not be.
    """
    run_path = make_run(tmp_path, test_clip)
    approve(run_path)
    result = batch(run_path)

    assert result.exit_code == 0, result.output
    assert "not scored" in result.output
    assert not (run_path / "scores.jsonl").exists(), "no score file for an ungraded run"

    skipped = log_events(run_path, "batch.scoring.skipped")
    assert len(skipped) == 1
    assert "D27" in skipped[0]["reason"]
    assert not log_events(run_path, "batch.scoring.enabled")


@requires_models
def test_cli_batch_uses_scorer_and_retry_controller_on_non_dummy(
    tmp_path, test_clip, stub, clip_reference
) -> None:
    """D25 proven: a paid backend is scored, retried and recorded."""
    run_path = make_run(tmp_path, test_clip, backend="stub", refs=[clip_reference])
    approve(run_path)
    result = batch(run_path)
    assert result.exit_code == 0, result.output
    assert "mean F" in result.output

    enabled = log_events(run_path, "batch.scoring.enabled")
    assert enabled and enabled[0]["references"] == 1

    scores = [json.loads(l) for l in (run_path / "scores.jsonl").read_text().splitlines() if l.strip()]
    assert len(scores) >= 60, "every frame must be scored (SPEC §3)"
    for record in scores[:3]:
        assert set(record) >= {"frame", "ssim", "lpips_edges", "identity", "temporal",
                               "f", "verdict", "reason", "targets_met", "missed_targets"}
        assert 0.0 <= record["f"] <= 1.0

    summary = log_events(run_path, "batch.scoring.summary")
    assert summary and summary[0]["frames_scored"] >= 60


@requires_models
def test_batch_refuses_a_paid_run_with_no_reference_images(tmp_path, test_clip, stub) -> None:
    """Fail BEFORE the ledger authorises anything, not at frame 1 mid-spend."""
    run_path = make_run(tmp_path, test_clip, backend="stub")
    approve(run_path)
    result = batch(run_path)

    assert result.exit_code == 1
    assert "reference image" in result.output
    assert not (run_path / "spend_ledger.jsonl").exists(), "money was authorised anyway"
    assert not list((run_path / "frames" / "restyled").glob("*.png"))


@requires_models
def test_scorer_plus_retry_integration_with_real_canary_run(
    tmp_path, test_clip, stub, clip_reference
) -> None:
    """End to end on a scored run: scores.jsonl, ledger and QC card agree."""
    run_path = make_run(tmp_path, test_clip, backend="stub", refs=[clip_reference])
    approve(run_path)
    assert batch(run_path).exit_code == 0

    result = runner.invoke(
        app, ["assemble", str(run_path), "--runs-dir", str(run_path.parent)]
    )
    assert result.exit_code == 0, result.output

    card = json.loads((run_path / "qc_card.json").read_text())
    assert card["scores"] is not None, "a scored run must not report null scores"
    assert card["scores"]["scored_frames"] >= 60
    assert 0.0 <= card["scores"]["mean_F"] <= 1.0
    assert card["scores"]["min_F"] <= card["scores"]["mean_F"]
    assert card["cost_usd"]["total"] > 0, "a priced backend must show real spend"


def test_qccard_derives_cost_from_ledger_not_backend_branch(tmp_path) -> None:
    """Cost comes from the ledger the firewalls enforce against — nowhere else.

    Inferring it from which backend was chosen would report a plausible number
    instead of the real one, and would quietly disagree with the ledger.
    """
    from claypipe.pipeline.qccard import cost_from_ledger

    ledger = tmp_path / "spend_ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in [
        {"entry_id": "a", "event": "authorized", "stage": "canary", "estimated_usd": 0.04},
        {"entry_id": "a", "event": "reconciled", "actual_usd": 0.05},
        {"entry_id": "b", "event": "authorized", "stage": "batch", "estimated_usd": 0.03},
        {"entry_id": "b", "event": "reconciled", "actual_usd": 0.03},
        {"entry_id": "c", "event": "authorized", "stage": "batch", "estimated_usd": 0.03},
    ]) + "\n")

    cost = cost_from_ledger(ledger)
    assert cost["canary"] == pytest.approx(0.05), "the reconciled actual supersedes the estimate"
    assert cost["batch"] == pytest.approx(0.06), "an in-flight call is charged at estimate"
    assert cost["total"] == pytest.approx(0.11)

    # No ledger at all is zero, not a guess.
    assert cost_from_ledger(tmp_path / "absent.jsonl")["total"] == 0.0


def test_qccard_reports_null_scores_for_an_unscored_run(tmp_path) -> None:
    """An unmeasured run must never read as a clean one."""
    from claypipe.pipeline.qccard import summarise_scores

    assert summarise_scores(tmp_path / "absent.jsonl") is None
    empty = tmp_path / "scores.jsonl"
    empty.write_text("")
    assert summarise_scores(empty) is None


@requires_models
def test_retry_decision_reaches_the_backend_with_lower_strength(
    tmp_path, test_clip, monkeypatch, clip_reference
) -> None:
    """D18 = A, proven end to end rather than at the policy layer only.

    A frame that misses its targets is retried at a LOWER STRENGTH with the
    shot's SEED RETAINED. This asserts the backend actually received those
    arguments, which is the half a policy unit-test cannot see.
    """
    backend = DegradedStub(bad_attempts=1)
    monkeypatch.setattr(cli, "get_backend", lambda name, **kw: backend)
    real_load_all = cli.load_all

    def priced():
        styles, weights = real_load_all()
        weights.firewalls.cost.estimated_usd_per_call["stub"] = 0.01
        return styles, weights

    monkeypatch.setattr(cli, "load_all", priced)

    run_path = make_run(tmp_path, test_clip, backend="stub", refs=[clip_reference])
    approve(run_path)
    batch(run_path)  # will halt on the retry budget; the first frames are enough

    first_frame = [c for c in backend.calls if c["frame"] == "f_00001.png"]
    assert len(first_frame) >= 2, "a failing frame must have been retried"
    assert first_frame[1]["strength"] < first_frame[0]["strength"], "strength not lowered"
    assert first_frame[1]["seed"] == first_frame[0]["seed"], "D18 = A: the seed is retained"


@requires_models
def test_a_backend_that_never_recovers_trips_the_retry_budget(
    tmp_path, test_clip, monkeypatch, clip_reference
) -> None:
    """SPEC §4.2: a clip needing >15% retries has a systemic problem.

    Retrying it frame by frame is throwing API money at a prompt fault, which is
    the exact failure pattern the budget designs out.
    """
    backend = DegradedStub(bad_attempts=99)
    monkeypatch.setattr(cli, "get_backend", lambda name, **kw: backend)
    real_load_all = cli.load_all

    def priced():
        styles, weights = real_load_all()
        weights.firewalls.cost.estimated_usd_per_call["stub"] = 0.01
        return styles, weights

    monkeypatch.setattr(cli, "load_all", priced)

    run_path = make_run(tmp_path, test_clip, backend="stub", refs=[clip_reference])
    approve(run_path)
    result = batch(run_path)

    assert result.exit_code == 1
    assert "retry budget exhausted" in result.output
    incidents = list((run_path / "incidents").glob("*-retry_budget_exceeded.json"))
    assert len(incidents) == 1, "a paused run must leave an incident a human can read"
