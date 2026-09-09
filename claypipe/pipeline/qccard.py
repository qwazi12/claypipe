"""Stage: QC card (SPEC §7).

One card per clip, always, appended to a project-level history so per-style
tuning insights accumulate. Fields that do not exist yet are `null` — a QC card
never carries a fabricated number or timestamp (Rule 40).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging import utc_now
from ..run import Run

HISTORY_NAME = "history.jsonl"


def build_card(run: Run, *, frames: int, verdict: str, extra: dict[str, Any] | None = None) -> dict:
    """Assemble the QC card from what is actually known.

    `scores` stays null until the scoring module lands (Build Order step 2), and
    the cost breakdown is zero only because DummyBackend genuinely costs nothing.
    """
    card: dict[str, Any] = {
        "schema_version": 1,
        "clip_id": run.run_id,
        "created_at": utc_now(),
        "source_sha256": run.manifest.source_sha256,
        "style": run.manifest.style,
        "backend": run.manifest.backend,
        "fps": run.manifest.fps,
        "frames": frames,
        # Populated by the scoring module (step 2). Not invented here.
        "scores": None,
        "auto_retries": None,
        "human_flags": None,
        "human_overrides": None,
        "cost_usd": {"canary": 0.0, "batch": 0.0, "retries": 0.0, "total": 0.0}
        if run.manifest.backend == "dummy"
        else None,
        "verdict": verdict,
        "lessons": "",
    }
    if extra:
        card.update(extra)
    return card


def write_card(run: Run, card: dict, history_dir: Path) -> Path:
    run.paths.qc_card.write_text(json.dumps(card, indent=2) + "\n")
    history = history_dir / HISTORY_NAME
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a") as fh:
        fh.write(json.dumps(card) + "\n")
    run.logger.info("qc.card", path=str(run.paths.qc_card), verdict=card["verdict"])
    return run.paths.qc_card
