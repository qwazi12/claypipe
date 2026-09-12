"""cli.batch canary-gate wiring (Build Order step 5, commit 1).

Step 3 built the gate and Step 4 built the pages that write its verdict. These
tests prove `cli.batch` actually consults the Step-4 loader before it authorises
a single call — a gate the command does not read is not a gate.

Everything here runs on the free dummy backend. No network, no spend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claypipe.cli import app
from claypipe.verdi import loaders as L

runner = CliRunner()


@pytest.fixture
def run_path(tmp_path: Path, test_clip: Path) -> Path:
    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app, ["intake", str(test_clip), "--style", "clay", "--runs-dir", str(runs_dir)]
    )
    assert result.exit_code == 0, result.output
    return Path(result.stdout.strip().splitlines()[-1])


def runs_dir_of(run_path: Path) -> str:
    return str(run_path.parent)


def write_verdict(run_path: Path, **payload) -> Path:
    body = {"schema_version": 1, "decided_at": "2026-09-09T12:00:00.000Z",
            "decider": "operator", "frames": {}}
    body.update(payload)
    path = run_path / L.CANARY_VERDICT_NAME
    path.write_text(json.dumps(body, indent=2))
    return path


def run_batch(run_path: Path, *extra: str):
    return runner.invoke(
        app, ["batch", str(run_path), "--runs-dir", runs_dir_of(run_path), *extra]
    )


def restyled_count(run_path: Path) -> int:
    return len(list((run_path / "frames" / "restyled").glob("*.png")))


def test_cli_batch_blocks_on_canary_missing(run_path: Path) -> None:
    """No verdict -> no spend. The first firewall (SPEC §4.1)."""
    result = run_batch(run_path)
    assert result.exit_code == 1
    assert "canary gate" in result.output
    assert L.CANARY_VERDICT_NAME in result.output
    assert restyled_count(run_path) == 0, "work started before the gate cleared"


def test_cli_batch_blocks_on_canary_not_approved(run_path: Path) -> None:
    """A rejection surfaces the operator's reason verbatim — nothing swallowed."""
    reason = "the set still reads photoreal, not plasticine"
    write_verdict(run_path, approved=False, reason=reason)

    result = run_batch(run_path)
    assert result.exit_code == 1
    assert "REJECTED" in result.output
    assert reason in result.output
    assert restyled_count(run_path) == 0


def test_cli_batch_admits_on_canary_approved(run_path: Path) -> None:
    """The only outcome that lets the pipeline proceed."""
    write_verdict(run_path, approved=True)

    result = run_batch(run_path)
    assert result.exit_code == 0, result.output
    assert "60 frames restyled" in result.output
    assert restyled_count(run_path) == 60


def test_cli_batch_writes_prompt_override_when_adjust(run_path: Path) -> None:
    """'adjust' records the revised prompt and STOPS.

    An adjusted prompt means the canary itself has to be re-shot; batching on a
    prompt no canary ever validated is exactly the spend this gate exists to
    prevent.
    """
    override = "claymation with visible thumbprints, matte finish, no gloss"
    write_verdict(run_path, approved="adjust", prompt_override=override)

    result = run_batch(run_path)
    assert result.exit_code == 1
    assert "ADJUST" in result.output
    assert restyled_count(run_path) == 0, "adjust must not spend"

    written = run_path / L.PROMPT_OVERRIDE_NAME
    assert written.is_file()
    assert written.read_text().strip() == override

    manifest = json.loads((run_path / "run.json").read_text())
    assert manifest["prompt_override_source"] == L.PROMPT_OVERRIDE_NAME
    assert "prompt" not in manifest, "the manifest holds a pointer, not the text"


def test_cli_batch_uses_the_override_prompt_on_the_next_run(run_path: Path) -> None:
    """Once recorded, the override is what actually gets used — not styles.yaml."""
    override = "LEGO brick-built diorama, matte studs"
    write_verdict(run_path, approved="adjust", prompt_override=override)
    run_batch(run_path)

    write_verdict(run_path, approved=True)
    result = run_batch(run_path)
    assert result.exit_code == 0, result.output

    logged = [json.loads(l) for l in (run_path / "logs" / "run.jsonl").read_text().splitlines() if l.strip()]
    events = [r for r in logged if r["event"] == "batch.prompt_override"]
    assert events, "the override was recorded but never used"
    assert events[-1]["chars"] == len(override)


def test_cli_batch_refuses_when_the_override_file_vanishes(run_path: Path) -> None:
    """A dangling override pointer must fail loudly, never fall back silently.

    Falling back to the style prompt would spend money on a prompt nobody
    approved — the quiet failure this whole gate exists to prevent.
    """
    write_verdict(run_path, approved="adjust", prompt_override="something")
    run_batch(run_path)
    (run_path / L.PROMPT_OVERRIDE_NAME).unlink()

    write_verdict(run_path, approved=True)
    result = run_batch(run_path)
    assert result.exit_code == 1
    assert "Refusing to silently fall back" in result.output
    assert restyled_count(run_path) == 0


def test_cli_batch_refuses_an_unreadable_verdict(run_path: Path) -> None:
    """Corrupt JSON is never read as approval."""
    (run_path / L.CANARY_VERDICT_NAME).write_text("{not json")
    result = run_batch(run_path)
    assert result.exit_code == 1
    assert "canary gate" in result.output and "not valid JSON" in result.output


# --------------------------------------------------------------------------
# T4 — two locks in front of any paid backend
# --------------------------------------------------------------------------

def test_cli_batch_refuses_fal_without_live_flag(run_path: Path, monkeypatch) -> None:
    """DEFENDS: an accidental paid run. --live is the deliberate act.

    Proven with FAL_KEY PRESENT, so the refusal is attributable to the missing
    flag alone and not to absent credentials.
    """
    monkeypatch.setenv("FAL_KEY", "key-is-present-but-irrelevant-here")
    write_verdict(run_path, approved=True)

    result = run_batch(run_path, "--backend", "fal")

    assert result.exit_code == 1
    assert "Refusing to use paid backend" in result.output
    assert restyled_count(run_path) == 0


def test_cli_batch_refuses_fal_with_live_but_no_env(run_path: Path, monkeypatch) -> None:
    """DEFENDS: Rule 5 — fail fast and name the missing variable.

    Deliberate intent without the credential must fail at startup, not at the
    first call.
    """
    monkeypatch.setenv("FAL_KEY", "   ")  # whitespace is not a key
    write_verdict(run_path, approved=True)

    result = run_batch(run_path, "--backend", "fal", "--live")

    assert result.exit_code == 1
    assert "FAL_KEY" in result.output
    assert restyled_count(run_path) == 0


def test_paid_guard_runs_before_the_canary_gate(run_path: Path, monkeypatch) -> None:
    """The cheapest, most local check reports first.

    With NO canary verdict and no --live, both gates would refuse. The paid
    guard is the more actionable message, and it touches no files.
    """
    monkeypatch.delenv("FAL_KEY", raising=False)
    result = run_batch(run_path, "--backend", "fal")
    assert result.exit_code == 1
    assert "Refusing to use paid backend" in result.output
    assert "canary gate" not in result.output


def test_free_backend_needs_neither_live_nor_a_key(run_path: Path, monkeypatch) -> None:
    """DEFENDS: the guard leaking onto the offline path it must not gate."""
    monkeypatch.delenv("FAL_KEY", raising=False)
    write_verdict(run_path, approved=True)

    result = run_batch(run_path)
    assert result.exit_code == 0, result.output
    assert restyled_count(run_path) == 60


def test_backend_override_is_recorded_in_the_manifest(run_path: Path, monkeypatch) -> None:
    """An override changes what the run IS, so the run must say so."""
    monkeypatch.setenv("FAL_KEY", "present")
    write_verdict(run_path, approved=True)
    run_batch(run_path, "--backend", "fal")  # refused for want of --live

    manifest = json.loads((run_path / "run.json").read_text())
    assert manifest["backend"] == "fal", "the override was not persisted"
