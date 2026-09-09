"""Verdict loader tests (Build Order step 4).

These loaders are the contract Step 5's `cli.batch` will call before it
authorises a single paid frame. The recurring theme: an unreadable or absent
decision must never resolve to approval, and a human's reason must reach the
caller unedited.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claypipe.config import ConfigError
from claypipe.verdi import loaders as L


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs" / "demo-run"
    d.mkdir(parents=True)
    return d


def write_verdict(run_dir: Path, **overrides) -> Path:
    payload = {
        "schema_version": 1,
        "decided_at": "2026-09-09T12:00:00.000Z",
        "decider": "operator",
        "approved": True,
        "reason": "",
        "prompt_override": "",
        "frames": {"f_00001.png": {"verdict": "approve", "note": ""}},
    }
    payload.update(overrides)
    path = run_dir / L.CANARY_VERDICT_NAME
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


class FakeClock:
    """Deterministic time. A test that waits ten minutes is a test nobody runs."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


def test_read_canary_verdict_round_trips_the_contract(run_dir) -> None:
    path = write_verdict(run_dir)
    loaded = L.read_canary_verdict(path)
    assert loaded["approved"] is True
    assert loaded["decider"] == "operator"
    assert loaded["frames"]["f_00001.png"]["verdict"] == "approve"


def test_read_canary_verdict_raises_when_absent(run_dir) -> None:
    with pytest.raises(L.NoCanaryVerdict):
        L.read_canary_verdict(run_dir / L.CANARY_VERDICT_NAME)


def test_read_canary_verdict_refuses_malformed_json(run_dir) -> None:
    """An unreadable verdict is never approval."""
    path = run_dir / L.CANARY_VERDICT_NAME
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        L.read_canary_verdict(path)


def test_read_canary_verdict_enforces_the_schema(run_dir) -> None:
    path = write_verdict(run_dir, approved=False, reason="")
    with pytest.raises(ConfigError, match="reason"):
        L.read_canary_verdict(path)


def test_poll_canary_verdict_resolves_canarytimeout_on_expiry(run_dir, monkeypatch) -> None:
    """Waiting forever is not an option; giving up must be explicit.

    Uses an injected clock, so this test costs no wall time and cannot flake.
    """
    clock = FakeClock()
    with pytest.raises(L.CanaryTimeout, match="never appeared"):
        L.poll_canary_verdict(
            run_dir / L.CANARY_VERDICT_NAME,
            timeout_s=30, poll_s=1.0,
            sleep=clock.sleep, now=clock.monotonic,
        )
    assert clock.slept >= 30, "should have polled up to the deadline"


def test_poll_canary_verdict_returns_as_soon_as_the_file_appears(run_dir) -> None:
    clock = FakeClock()
    path = run_dir / L.CANARY_VERDICT_NAME

    def sleep_then_write(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.now >= 3:
            write_verdict(run_dir)

    verdict = L.poll_canary_verdict(
        path, timeout_s=60, poll_s=1.0, sleep=sleep_then_write, now=clock.monotonic
    )
    assert verdict["approved"] is True
    assert clock.now < 60, "returned before the deadline"


def test_poll_canary_verdict_raises_when_the_run_dir_is_gone(tmp_path) -> None:
    """Nothing will ever write there; blocking would be a hang, not a wait."""
    with pytest.raises(L.NoCanaryVerdict, match="does not exist"):
        L.poll_canary_verdict(tmp_path / "gone" / L.CANARY_VERDICT_NAME)


def test_poll_canary_verdict_rejects_when_approved_false_with_reason(run_dir) -> None:
    """The operator's reason reaches the caller verbatim — nothing swallowed."""
    reason = "prompt reads as plasticine but the set is still photoreal"
    write_verdict(run_dir, approved=False, reason=reason)

    with pytest.raises(L.CanaryRejected) as exc:
        L.require_approved_canary(run_dir / L.CANARY_VERDICT_NAME)

    assert exc.value.reason == reason
    assert str(exc.value) == reason
    assert exc.value.verdict["approved"] is False


def test_poll_canary_verdict_picks_up_adjust_prompt_override(run_dir) -> None:
    """The 'adjust' branch flows through polling into merge_prompt_override."""
    override = "claymation with visible thumbprints, matte finish"
    write_verdict(run_dir, approved="adjust", prompt_override=override)
    (run_dir / "run.json").write_text(json.dumps({"run_id": "demo-run", "style": "clay"}))

    verdict = L.require_approved_canary(run_dir / L.CANARY_VERDICT_NAME)
    assert verdict["approved"] == "adjust"

    written = L.merge_prompt_override(run_dir / "run.json", verdict)
    assert written is not None and written.is_file()
    assert written.read_text().strip() == override


def test_merge_prompt_override_creates_a_separate_file_not_in_place(run_dir) -> None:
    """The original prompt is never overwritten — the override is a POINTER.

    A per-run adjustment must not be able to become a project-wide edit, so
    styles.yaml is untouched and run.json only gains a reference.
    """
    manifest_path = run_dir / "run.json"
    original = {"run_id": "demo-run", "style": "clay", "fps": 12, "backend": "fal"}
    manifest_path.write_text(json.dumps(original, indent=2))

    verdict = {"approved": "adjust", "prompt_override": "more clay, fewer highlights"}
    written = L.merge_prompt_override(manifest_path, verdict)

    assert written == run_dir / L.PROMPT_OVERRIDE_NAME
    assert written.read_text().strip() == "more clay, fewer highlights"

    after = json.loads(manifest_path.read_text())
    for key, value in original.items():
        assert after[key] == value, f"{key} was modified"
    assert after["prompt_override_source"] == L.PROMPT_OVERRIDE_NAME
    assert "prompt" not in after, "the manifest must hold a pointer, not the text"

    styles = Path("styles.yaml").read_text()
    assert "more clay, fewer highlights" not in styles, "styles.yaml was edited"


def test_merge_prompt_override_ignores_non_adjust_verdicts(run_dir) -> None:
    manifest_path = run_dir / "run.json"
    manifest_path.write_text(json.dumps({"run_id": "demo-run"}))
    for verdict in (
        {"approved": True, "prompt_override": "ignored"},
        {"approved": False, "reason": "no", "prompt_override": "ignored"},
        {"approved": "adjust", "prompt_override": "   "},
    ):
        assert L.merge_prompt_override(manifest_path, verdict) is None
    assert "prompt_override_source" not in json.loads(manifest_path.read_text())


def test_load_flag_verdicts_missing_file_is_an_empty_review(run_dir) -> None:
    assert L.load_flag_verdicts(run_dir / L.REVIEW_VERDICT_NAME) == {}


def test_load_flag_verdicts_never_silently_drops_entries(run_dir) -> None:
    """A corrupt review must not read as a clean bill of health."""
    path = run_dir / L.REVIEW_VERDICT_NAME
    path.write_text("{oops")
    with pytest.raises(ConfigError, match="corrupt review"):
        L.load_flag_verdicts(path)

    path.write_text(json.dumps({
        "schema_version": 1,
        "decided_at": "2026-09-09T12:00:00.000Z",
        "frames": {
            "f_00007.png": {"category": "outlier_audit", "verdict": "retry", "note": "",
                            "score_summary": {"f": 0.62, "reason": "composite_fail",
                                              "missed": ["ssim"]}},
            "f_00009.png": {"category": "random_audit", "verdict": "accept", "note": "",
                            "score_summary": {"f": 0.91, "reason": "accepted", "missed": []}},
        },
    }))
    loaded = L.load_flag_verdicts(path)
    assert len(loaded["frames"]) == 2
    assert loaded["frames"]["f_00007.png"]["verdict"] == "retry"
