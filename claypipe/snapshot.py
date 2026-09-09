"""A run, serialised into one JSON document (the dashboard contract).

ClayPipe runs on the operator's machine: ffmpeg, torch, sixty frames a clip.
Its state is a directory of files on local disk, which nothing hosted can see.
This module is the bridge — it reduces a run directory to a single document a
remote dashboard can store, render and reason about.

TWO RULES GOVERN WHAT GOES IN:

  1. No absolute paths, ever. A snapshot is designed to leave the machine, and
     `/Users/<name>/dev/...` is both useless remotely and needless disclosure.
     Filenames only.
  2. No secrets and no payloads. Metadata only (Rule 6) — counts, scores,
     verdicts, costs. The frames themselves stay local; the dashboard shows
     what happened, not the pixels.

The snapshot is DERIVED, never authoritative: the run directory remains the
source of truth, and a snapshot can always be rebuilt from it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .logging import utc_now
from .pipeline.extract import count_frames
from .pipeline.qccard import cost_from_ledger, summarise_scores
from .run import Run, RunPaths

SCHEMA_VERSION = 1
# A dashboard chart needs shape, not every sample. A 744-frame clip downsamples
# to this many points; the summary statistics above it stay exact.
MAX_SERIES_POINTS = 240


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _downsample(records: list[dict], limit: int = MAX_SERIES_POINTS) -> list[dict]:
    """Even stride, endpoints preserved. Never averages away a bad frame."""
    if len(records) <= limit:
        return records
    step = len(records) / limit
    picked = [records[int(i * step)] for i in range(limit)]
    if picked[-1] is not records[-1]:
        picked[-1] = records[-1]
    return picked


def _stage(paths: RunPaths, extracted: int, restyled: int) -> str:
    """Where the run has actually got to, judged from artefacts on disk."""
    if paths.final.is_file():
        return "assembled"
    if restyled and restyled == extracted:
        return "restyled"
    if restyled:
        return "restyling"
    if extracted:
        return "extracted"
    return "intake"


def _canary_state(paths: RunPaths) -> dict[str, Any]:
    """What the human gate currently says — the thing an operator most wants
    to see remotely, because it is the thing that blocks spending."""
    path = paths.root / "canary_verdict.json"
    if not path.is_file():
        return {"state": "awaiting_review", "approved": None}
    try:
        verdict = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"state": "unreadable", "approved": None}
    approved = verdict.get("approved")
    state = {True: "approved", False: "rejected"}.get(approved, str(approved))
    return {
        "state": state,
        "approved": approved,
        "decider": verdict.get("decider"),
        "decided_at": verdict.get("decided_at"),
        "reason": verdict.get("reason") or None,
        "frames": {
            name: entry.get("verdict")
            for name, entry in (verdict.get("frames") or {}).items()
        },
    }


def _incidents(paths: RunPaths) -> list[dict]:
    """Every firewall trip, newest first. An incident is the whole reason a
    run pauses, so it is never summarised away."""
    directory = paths.root / "incidents"
    if not directory.is_dir():
        return []
    notes = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            note = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        note.pop("run_dir", None)  # absolute path — never leaves the machine
        note["file"] = path.name
        notes.append(note)
    return notes


def build_snapshot(run: Run) -> dict:
    """Reduce one run directory to a single dashboard document."""
    paths = run.paths
    manifest = run.manifest
    extracted = count_frames(paths.source_frames)
    restyled = count_frames(paths.restyled_frames)

    scores = _read_jsonl(paths.scores)
    series = [
        {
            "frame": s["frame"],
            "f": round(s["f"], 4),
            "verdict": s["verdict"],
            "missed": s.get("missed_targets", []),
        }
        for s in _downsample(scores)
    ]

    qc = None
    if paths.qc_card.is_file():
        try:
            qc = json.loads(paths.qc_card.read_text())
        except json.JSONDecodeError:
            qc = None

    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": utc_now(),
        "run_id": manifest.run_id,
        "created_at": manifest.created_at,
        "clip": {
            # Basename only: a snapshot is built to leave the machine.
            "title": manifest.clip_title,
            "source_name": Path(manifest.source_path).name,
            "source_sha256": manifest.source_sha256,
            "duration_s": manifest.source_duration_s,
        },
        "config": {
            "style": manifest.style,
            "fps": manifest.fps,
            "backend": manifest.backend,
            "references": len(manifest.reference_images),
            "prompt_overridden": bool(manifest.prompt_override_source),
        },
        "stage": _stage(paths, extracted, restyled),
        "progress": {
            "extracted": extracted,
            "restyled": restyled,
            "percent": round(100 * restyled / extracted, 1) if extracted else 0.0,
        },
        "canary": _canary_state(paths),
        # None when the run was never scored — an unmeasured run must never
        # render as a clean one (memory.md D29).
        "scores": summarise_scores(paths.scores),
        "score_series": series,
        "cost_usd": cost_from_ledger(paths.root / "spend_ledger.jsonl"),
        "incidents": _incidents(paths),
        "artifacts": {
            "final_video": paths.final.is_file(),
            "canary_page": (paths.root / "canary_review.html").is_file(),
            "flag_page": (paths.root / "flag_review.html").is_file(),
            "qc_card": paths.qc_card.is_file(),
        },
        "verdict": (qc or {}).get("verdict"),
        "lessons": (qc or {}).get("lessons") or "",
    }


def build_index(runs_dir: Path) -> dict:
    """Every run under a directory, newest first — the dashboard's home page."""
    runs = []
    if runs_dir.is_dir():
        for candidate in sorted(runs_dir.iterdir(), reverse=True):
            if not (candidate / "run.json").is_file():
                continue
            try:
                run = Run.load(candidate, echo=False)
            except Exception:
                continue
            snapshot = build_snapshot(run)
            snapshot.pop("score_series", None)  # index stays light
            snapshot.pop("incidents", None)
            runs.append(snapshot)
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": utc_now(),
        "runs": runs,
        "totals": {
            "runs": len(runs),
            "spend_usd": round(sum(r["cost_usd"]["total"] for r in runs), 6),
        },
    }


def assert_no_local_paths(document: dict) -> None:
    """Guard: a snapshot must carry no absolute path before it is published.

    Cheap to check, and the failure mode it prevents — quietly shipping the
    operator's home directory layout to a hosted service — is not cheap.
    """
    blob = json.dumps(document)
    for needle in ("/Users/", "/home/", "/private/var/", "C:\\\\"):
        if needle in blob:
            raise ValueError(
                f"snapshot contains a local path ({needle!r}); refusing to "
                "publish machine layout to a remote service"
            )
