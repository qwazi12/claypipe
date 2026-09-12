"""FalBackend, stubbed — zero network, zero spend.

DEFENDS: the possibility of a paid call happening without an operator meaning
it. Every test here runs with no credentials and no client unless it installs
a stub itself, and no test in this file may ever reach fal.ai.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claypipe.config import ConfigError, load_weights
from claypipe.pipeline.restyle import (
    DEFAULT_FAL_MODEL,
    FalBackend,
    FalCallError,
    FalClientLike,
    get_backend,
)

FIXTURE = Path(__file__).parent / "fixtures" / "identical" / "source.png"


class StubFalClient:
    """Stands in for the endpoint. Returns canned bytes, records the request."""

    def __init__(self, payload: bytes | None = None) -> None:
        self.payload = payload if payload is not None else FIXTURE.read_bytes()
        self.requests: list[dict] = []

    def edit_image(self, *, image_bytes, prompt, strength, seed, model) -> bytes:
        self.requests.append(
            {"bytes": len(image_bytes), "prompt": prompt, "strength": strength,
             "seed": seed, "model": model}
        )
        return self.payload


@pytest.fixture
def firewalls():
    return load_weights().firewalls


def test_fal_backend_refuses_restyle_outside_live_mode(tmp_path: Path) -> None:
    """DEFENDS: a paid call reachable without the deliberate --live act."""
    backend = get_backend("fal")
    assert backend.name == "fal"
    assert backend.live is False

    with pytest.raises(NotImplementedError, match="requires --live and FAL_KEY"):
        backend.restyle(FIXTURE, tmp_path / "out.png", prompt="p", strength=0.6, seed=1)

    # --live alone is not enough: without a client there is nothing to call.
    with pytest.raises(NotImplementedError):
        get_backend("fal", live=True).restyle(
            FIXTURE, tmp_path / "out.png", prompt="p", strength=0.6, seed=1
        )


def test_fal_backend_cost_matches_yaml(firewalls) -> None:
    """DEFENDS: a price drifting out of config into code."""
    backend = FalBackend()
    assert backend.cost_per_frame_usd() == firewalls.cost.estimated_usd_per_call["fal"]
    assert backend.model_name() == firewalls.fal.model == DEFAULT_FAL_MODEL


def test_fal_backend_refuses_when_the_price_is_not_configured() -> None:
    """DEFENDS: an unpriced endpoint being treated as free."""
    cfg = load_weights().firewalls
    stripped = cfg.model_copy(
        update={"cost": cfg.cost.model_copy(update={"estimated_usd_per_call": {"dummy": 0.0}})}
    )
    with pytest.raises(ConfigError, match="Refusing to spend against an unknown price"):
        FalBackend(cfg=stripped).cost_per_frame_usd()


def test_fal_backend_refuses_call_when_estimate_exceeded(firewalls) -> None:
    """DEFENDS: absorbing a price rise nobody approved.

    The configured estimate is a CEILING, not a guess: an endpoint quoting
    above it stops the run.
    """
    backend = FalBackend()
    ceiling = backend.cost_per_frame_usd()

    backend.assert_affordable(ceiling)          # exactly at the ceiling is fine
    backend.assert_affordable(ceiling / 2)

    with pytest.raises(FalCallError, match="above the configured ceiling"):
        backend.assert_affordable(ceiling * 1.5)


def test_stubbed_fal_backend_dry_run_emits_estimate_only(tmp_path: Path) -> None:
    """DEFENDS: a dry run that quietly becomes a real one.

    plan() records what WOULD be sent and charges nothing; no client is touched
    and no output file appears.
    """
    client = StubFalClient()
    backend = FalBackend(live=True, client=client)

    entry = backend.plan(FIXTURE, prompt="claymation", strength=0.65, seed=1000)

    assert entry["frame"] == FIXTURE.name
    assert entry["model"] == DEFAULT_FAL_MODEL
    assert entry["estimated_usd"] == backend.cost_per_frame_usd()
    assert entry["strength"] == 0.65 and entry["seed"] == 1000
    assert client.requests == [], "a dry run must not call the endpoint"
    assert not (tmp_path / "out.png").exists()


def test_fal_backend_dry_run_writes_planned_call_ledger_entry(tmp_path: Path) -> None:
    """DEFENDS: the ledger wiring, proven without a network call.

    Every planned call is authorised against the ledger first, so the run cap
    is enforced on intent — the same path a live call takes.
    """
    from claypipe.pipeline.retry import RunHalted, SpendLedger
    from claypipe.run import RunPaths

    paths = RunPaths(tmp_path / "run")
    paths.ensure()
    backend = FalBackend(live=True, client=StubFalClient())
    price = backend.cost_per_frame_usd()

    ledger = SpendLedger(
        paths=paths, project_dir=tmp_path / "project", cfg=load_weights().firewalls,
        run_id="dry-run", max_cost_usd_run=price * 2.5,
    )

    for index in range(2):
        entry_id = ledger.authorize(frame=f"f_{index:05d}.png", backend="fal", stage="canary")
        planned = backend.plan(FIXTURE, prompt="p", strength=0.65, seed=1000)
        ledger.reconcile(entry_id, planned["estimated_usd"])

    assert ledger.run_total() == pytest.approx(price * 2)
    records = [json.loads(l) for l in ledger.run_ledger.read_text().splitlines() if l.strip()]
    assert [r["event"] for r in records] == [
        "authorized", "reconciled", "authorized", "reconciled"
    ]
    assert all(r.get("backend", "fal") == "fal" for r in records if r["event"] == "authorized")

    # The third planned call would breach the cap, and is refused on intent.
    with pytest.raises(RunHalted):
        ledger.authorize(frame="f_00002.png", backend="fal", stage="canary")
    assert len(backend.planned_calls) == 2, "no call was planned past the cap"


def test_stub_client_is_the_only_way_to_reach_the_endpoint_seam(tmp_path: Path) -> None:
    """DEFENDS: the narrowness of the client seam.

    One method, injected. Nothing in the pipeline can reach past it, which is
    what makes 'no test calls fal.ai' enforceable rather than aspirational.
    """
    client = StubFalClient()
    assert isinstance(client, FalClientLike)

    backend = FalBackend(live=True, client=client)
    out = tmp_path / "out.png"
    backend.restyle(FIXTURE, out, prompt="claymation", strength=0.65, seed=1000)

    assert out.read_bytes() == FIXTURE.read_bytes()
    assert len(client.requests) == 1
    assert client.requests[0]["model"] == DEFAULT_FAL_MODEL
    assert client.requests[0]["seed"] == 1000
