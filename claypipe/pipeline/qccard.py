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
        # V4: all five metrics reported per-component, not just the composite.
        # While the mode is uncalibrated the observed values ARE the
        # deliverable — the target vector is set from them, so a card that
        # showed only F would throw away the measurement the canary is for.
        "components": _summarise_components(scores_path),
    }


# The five metrics, and which direction is better. `lpips_edges` is a DISTANCE,
# so lower is better and its worst case is the maximum — getting that backwards
# would report the best frame as the worst.
COMPONENTS = {
    "ssim": "higher",
    "lpips_edges": "lower",
    "id": "higher",
    "tf": "higher",
    "flow": "higher",
}

# scores.jsonl field names, which differ from the target names for historical
# reasons (`identity` vs `id`, `temporal` vs `tf`).
COMPONENT_FIELDS = {
    "ssim": "ssim",
    "lpips_edges": "lpips_edges",
    "id": "identity",
    "tf": "temporal",
    "flow": "flow",
}


def _summarise_components(scores_path: Path) -> dict:
    """Per-metric mean and worst value, plus which support ID used.

    FLOW is listed first in the returned dict because it is the primary gate
    under v2v: the format only reads if the restyled panel moves in step with
    the original, and that is what justifies re-muxing the source audio.
    """
    collected: dict[str, list[float]] = {name: [] for name in COMPONENTS}
    supports: dict[str, int] = {}
    misses: dict[str, int] = {}

    for line in scores_path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        for name, field in COMPONENT_FIELDS.items():
            if field in record and record[field] is not None:
                collected[name].append(float(record[field]))
        support = record.get("id_support")
        if support:
            supports[support] = supports.get(support, 0) + 1
        for name, ok in (record.get("targets_met") or {}).items():
            if not ok:
                misses[name] = misses.get(name, 0) + 1

    out: dict[str, Any] = {}
    for name in ("flow", "tf", "id", "ssim", "lpips_edges"):
        values = collected.get(name) or []
        if not values:
            continue
        worst = max(values) if COMPONENTS[name] == "lower" else min(values)
        out[name] = {
            "mean": round(sum(values) / len(values), 4),
            "worst": round(worst, 4),
            "better": COMPONENTS[name],
            "n": len(values),
            "target_misses": misses.get(name, 0),
        }
    if supports:
        # Which support ID was measured over. A whole-frame ID under
        # whole-frame restyle is dominated by the environment, so the two are
        # not comparable numbers and the card must not blur them.
        out["id_support"] = supports
    return out


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
        # T13: the mode decides which target vector the scores above were
        # graded against, so a card without it cannot be compared to another
        # card. `runs.mode` is also what the dashboard needs to render the
        # right targets (MASTER_PLAN §5).
        "mode": run.manifest.mode,
        "fps": run.manifest.fps,
        "frames": frames,
        "scores": summarise_scores(run.paths.scores),
        # T18a: reported BESIDE the gate scores, never inside them. TF cannot
        # measure propagation drift — a propagated frame is a warp along the
        # optical flow and TF grades by warping along the optical flow, so the
        # metric and the generation method are the same operation. This is the
        # independent check, scored against the SOURCE.
        "propagation_drift": summarise_drift(run.paths.drift),
        # V5: where the generation seams fall, and whether the clay design held
        # ACROSS them. A seam is where two independently generated chunks meet;
        # on a shot cut a design shift is invisible, mid-shot it is a jump in
        # the character's face. Per-chunk ID against the approved canary is
        # what turns "the seams are here" into "and here is where it drifted".
        "chunking": summarise_chunks(run.paths.chunk_plan, run.paths.scores),
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


def summarise_drift(path: Path) -> dict | None:
    """The T18a drift summary, or None when propagation was not used.

    None means "this run did not propagate", which is a different statement
    from "propagation drifted by zero" — an unmeasured run must never read as
    a clean one (the same rule `summarise_scores` follows)."""
    from .propagate import drift_summary, read_drift_scores

    scores = read_drift_scores(path)
    if not scores:
        return None
    summary = drift_summary(scores)
    summary["note"] = (
        "source-referenced SSIM/LPIPS-edges on propagated frames. NOT part of "
        "F and NOT gated: there is no calibrated threshold for source-"
        "referenced drift until T16 measures one on a real backend."
    )
    return summary


def summarise_chunks(chunk_path: Path, scores_path: Path) -> dict | None:
    """Seam positions plus per-chunk identity, so drift is locatable.

    None means the run was not chunked, which is a different statement from
    "chunked with no drift" — the same rule summarise_scores follows.

    The ID-DRIFT REPORT is the point. A 60s clip is several chunks and nothing
    guarantees a character's clay design is the same in chunk 1 and chunk 4.
    Reporting each chunk's identity against the approved canary, and the SPREAD
    between chunks, is what surfaces the inconsistency that makes output read as
    slop rather than animation.
    """
    if not chunk_path.is_file():
        return None
    from .chunks import ChunkPlan

    plan = ChunkPlan.read(chunk_path)
    out: dict[str, Any] = dict(plan.summary())
    out["seam_frames"] = plan.seam_frames
    out["note"] = (
        "one seed for the whole run: under v2v the seed drives the generated "
        "design, so changing it between chunks would redesign the character at "
        "every seam"
    )

    if not scores_path.is_file():
        out["identity_by_chunk"] = None
        return out

    by_chunk: dict[int, list[float]] = {}
    supports: dict[int, set] = {}
    for line in scores_path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        identity = record.get("identity")
        if identity is None:
            continue
        index = _frame_number(record.get("frame", ""))
        if index is None:
            continue
        for chunk in plan.chunks:
            if chunk.start_frame <= index <= chunk.end_frame:
                by_chunk.setdefault(chunk.index, []).append(float(identity))
                support = record.get("id_support")
                if support:
                    supports.setdefault(chunk.index, set()).add(support)
                break

    if not by_chunk:
        out["identity_by_chunk"] = None
        return out

    means = {}
    for index in sorted(by_chunk):
        values = by_chunk[index]
        means[index] = round(sum(values) / len(values), 4)
        
    out["identity_by_chunk"] = [
        {
            "chunk": index,
            "mean_id": means[index],
            "worst_id": round(min(by_chunk[index]), 4),
            "frames": len(by_chunk[index]),
            "id_support": sorted(supports.get(index, [])),
        }
        for index in sorted(by_chunk)
    ]
    spread = max(means.values()) - min(means.values())
    out["identity_spread_across_chunks"] = round(spread, 4)
    out["identity_drift_note"] = (
        "spread between the best and worst chunk's mean identity. There is no "
        "calibrated threshold for it until a canary measures one, so it is "
        "REPORTED, not gated."
    )
    return out


def _frame_number(name: str) -> int | None:
    digits = "".join(c for c in Path(name).stem if c.isdigit())
    return int(digits) if digits else None


def write_card(run: Run, card: dict, history_dir: Path) -> Path:
    run.paths.qc_card.write_text(json.dumps(card, indent=2) + "\n")
    history = history_dir / HISTORY_NAME
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a") as fh:
        fh.write(json.dumps(card) + "\n")
    run.logger.info("qc.card", path=str(run.paths.qc_card), verdict=card["verdict"])
    return run.paths.qc_card
