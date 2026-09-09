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


def summarise_scores(scores_path: Path) -> dict | None:
    """mean / min / p5 of F over a run's scores.jsonl (SPEC §7).

    Returns None when the run was never scored, so the card says "not measured"
    rather than implying a clean sweep.
    """
    if not scores_path.is_file():
        return None
    values = []
    verdicts: dict[str, int] = {}
    for line in scores_path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        values.append(float(record["f"]))
        verdicts[record["verdict"]] = verdicts.get(record["verdict"], 0) + 1
    if not values:
        return None
    ordered = sorted(values)
    p5_index = max(0, int(round(0.05 * (len(ordered) - 1))))
    return {
        "mean_F": round(sum(ordered) / len(ordered), 4),
        "min_F": round(ordered[0], 4),
        "p5_F": round(ordered[p5_index], 4),
        "scored_frames": len(ordered),
        "verdicts": verdicts,
    }


def cost_from_ledger(ledger_path: Path) -> dict:
    """The cost breakdown, read from the SPEND LEDGER — never inferred.

    Deriving cost from which backend was selected would report a plausible
    number instead of the real one, and would quietly disagree with the ledger
    the firewalls actually enforce against. The ledger is the only source of
    truth for money (SPEC §4, Hard Rules).
    """
    stages: dict[str, dict[str, float]] = {}
    if ledger_path.is_file():
        for line in ledger_path.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            entry_id = record.get("entry_id")
            if entry_id is None:
                continue
            if record["event"] == "authorized":
                stages.setdefault(record.get("stage", "batch"), {})[entry_id] = float(
                    record["estimated_usd"]
                )
            elif record["event"] == "reconciled":
                for charged in stages.values():
                    if entry_id in charged:
                        charged[entry_id] = float(record["actual_usd"])
    breakdown = {stage: round(sum(v.values()), 6) for stage, v in stages.items()}
    breakdown.setdefault("canary", 0.0)
    breakdown.setdefault("batch", 0.0)
    breakdown.setdefault("retries", 0.0)
    breakdown["total"] = round(sum(breakdown[k] for k in breakdown if k != "total"), 6)
    return breakdown


def build_card(run: Run, *, frames: int, verdict: str, extra: dict[str, Any] | None = None) -> dict:
    """Assemble the QC card from what is actually known.

    Scores come from `scores.jsonl` and cost comes from the SPEND LEDGER. A run
    that was never scored reports null, not zero — an unmeasured run must never
    read as a clean one.
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
        "scores": summarise_scores(run.paths.scores),
        "auto_retries": None,
        "human_flags": None,
        "human_overrides": None,
        # From the ledger, never from a backend branch.
        "cost_usd": cost_from_ledger(run.paths.root / "spend_ledger.jsonl"),
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
