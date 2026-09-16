"""Run identity and on-disk layout (Rule 34: one runId, propagated everywhere).

A run directory is the single source of truth for a clip. Every stage reads and
writes only inside it, which is what makes crashed runs resumable (SPEC §2).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .config import StylesConfig
from .logging import RunLogger, utc_now

MANIFEST_NAME = "run.json"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "clip"


def new_run_id(source: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{slugify(source.stem)}-{stamp}-{uuid.uuid4().hex[:6]}"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


class RunManifest(BaseModel):
    """`run.json` — what this run is, decided once at intake."""

    schema_version: int = 1
    run_id: str
    created_at: str
    source_path: str
    source_sha256: str
    source_duration_s: float
    # T9: the layout engine derives panel height from the source's aspect
    # ratio, so the source's pixel dimensions are run identity, decided once at
    # intake. Zero means a run created before T9 — assembly re-probes and warns
    # rather than guessing a geometry.
    source_width: int = 0
    source_height: int = 0
    clip_title: str
    style: str
    fps: int
    backend: str
    # T13: which TRACK this run is. "surface" = per-frame img2img (geometry
    # preserved, structure is the thing graded). "resynth" = video-native
    # resynthesis (geometry legitimately moves; temporal consistency is the
    # claim). The mode decides the target vector AND the retry policy, so it is
    # run identity, fixed at intake — changing it mid-run would score the first
    # half of a clip against a different gate than the second.
    mode: str = "surface"
    # T14/A3: WHICH KIND of canary this run's verdict covers.
    #   "frames" — three stills. Sufficient for Track A.
    #   "clip"   — a short trim. REQUIRED for Track C, because three stills
    #              cannot canary a video model: temporal behaviour is the only
    #              reason to use one, and a still shows none of it.
    # On the MANIFEST rather than only in the verdict, because the verdict
    # arrives as a query string the operator pastes, and a gate that can be
    # satisfied by editing a URL is not a gate (D17).
    canary_kind: str | None = None
    canary_clip_seconds: float | None = None
    # T9b: the source already carries burned-in text. Recorded so a later
    # reviewer can tell a garbled top panel (the restyle rendering subtitle
    # glyphs) from a backend failure. Advisory — it never blocks a run.
    burned_in_text: dict | None = None
    burned_in_acknowledged: bool = False
    # T14/A3: WHICH KIND of canary this run's verdict covers.
    #   "frames" — three stills. Sufficient for Track A.
    #   "clip"   — a short trim. REQUIRED for Track C, because three stills
    #              cannot canary a video model: temporal behaviour is the only
    #              reason to use one, and a still shows none of it.
    # Set by `canary restyle`, read by the gate. On the MANIFEST rather than
    # only in the verdict, so it cannot be forged through the submission URL.
    canary_kind: str | None = None
    canary_clip_seconds: float | None = None
    # T9b: the source already carries burned-in text. Recorded so a later
    # reviewer can tell a garbled top panel (the restyle rendering subtitle
    # glyphs) from a backend failure. Advisory — it never blocks a run.
    burned_in_text: dict | None = None
    burned_in_acknowledged: bool = False
    reference_images: list[str] = Field(default_factory=list)
    # T10/F1: WHERE the identity references came from, which decides whether
    # the ID metric is measuring anything useful.
    #   "intake" — operator-supplied at `intake --ref`. Usually PHOTOREAL stills
    #              of the character. Scoring a clay restyle against those asks
    #              "does this clay puppet look like a photograph", and the answer
    #              is legitimately no (~0.65-0.82), so id_min 0.85 misses on
    #              nearly every frame and the retry budget halts the run.
    #   "canary" — the restyled frames the operator APPROVED. The ID metric then
    #              asks "is this the same clay character the operator signed
    #              off", which is the question it exists for.
    reference_origin: str = "intake"
    # V4: character-region crops taken from the SAME approved canary frames.
    # ID is scored region-against-region under whole-frame v2v, because a
    # whole-frame ID is dominated by the environment — which is also being
    # rebuilt in clay — so a wrong character can score well. A crop must be
    # compared against a crop; empty here means ID falls back to whole-frame
    # and the score says so.
    reference_regions: list[str] = Field(default_factory=list)
    # Set by verdi.loaders.merge_prompt_override when a canary comes back
    # "adjust". Points AT the override file; the original style prompt in
    # styles.yaml is never touched.
    prompt_override_source: str | None = None


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def source_frames(self) -> Path:
        return self.root / "frames" / "source"

    @property
    def restyled_frames(self) -> Path:
        return self.root / "frames" / "restyled"

    @property
    def audio(self) -> Path:
        return self.root / "audio.aac"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def restyled_video(self) -> Path:
        return self.work / "restyled.mp4"

    @property
    def header_png(self) -> Path:
        return self.work / "header.png"

    @property
    def cues(self) -> Path:
        """Phrase-level caption cues (T12). Generated by `claypipe captions`,
        and hand-editable — editing this file is the supported way to add the
        non-dialogue cues (`[dramatic music]`, `*crunch*`) that a speech
        transcriber cannot invent."""
        return self.root / "cues.json"

    @property
    def caption_frames(self) -> Path:
        """The caption band rendered as a PNG sequence, one per output frame."""
        return self.root / "frames" / "captions"

    @property
    def subtitles(self) -> Path:
        """An SRT written alongside the rendered track, for inspection and for
        anything downstream that wants the text. Since T12 it is NOT what gets
        composited — the caption track is."""
        return self.root / "subs.srt"

    @property
    def final(self) -> Path:
        return self.root / "final_comparison.mp4"

    @property
    def refs(self) -> Path:
        """Identity references for this run (T10). Populated from the APPROVED
        canary frames, so they live inside the run rather than pointing at
        operator files that may move."""
        return self.root / "refs"

    @property
    def shot_plan(self) -> Path:
        """Shot boundaries, seeds and motion ranking (T11). Written by `batch`,
        read by the canary renderer and by T18's keyframe propagation."""
        return self.root / "shots.json"

    @property
    def keyframe_plan(self) -> Path:
        """Which frames are paid for and which are warped (T18). Written by
        `batch --propagate` BEFORE any spend, so the projected cost is visible
        before a call goes out."""
        return self.root / "keyframes.json"

    @property
    def restyle_input_frames(self) -> Path:
        """Source frames with the burned-in caption band inpainted out (C4).

        Fed to the GENERATOR only. The original panel keeps its own captions —
        they are part of what the viewer compares against — and the audio is
        untouched. Absent when the source carries no burned-in text, in which
        case the generator reads `source_frames` directly."""
        return self.root / "frames" / "restyle_input"

    @property
    def chunk_plan(self) -> Path:
        """Generation chunks and where their seams fall (V5). A seam is where
        two independently generated chunks meet and the clay design can shift;
        on a cut that is invisible, mid-shot it is a jump in the character's
        face."""
        return self.root / "chunks.json"

    @property
    def drift(self) -> Path:
        """Source-referenced drift scores for propagated frames (T18a).

        SEPARATE from scores.jsonl on purpose: these are not gate scores. There
        is no calibrated threshold for source-referenced drift until T16, so
        folding them into F would be inventing a number — the F4 mistake with a
        new name."""
        return self.root / "drift.jsonl"

    @property
    def scores(self) -> Path:
        """Per-frame score log (SPEC §3): one JSON object per scored frame."""
        return self.root / "scores.jsonl"

    @property
    def qc_card(self) -> Path:
        return self.root / "qc_card.json"

    @property
    def log(self) -> Path:
        return self.root / "logs" / "run.jsonl"

    def ensure(self) -> None:
        for d in (self.source_frames, self.restyled_frames, self.work, self.log.parent):
            d.mkdir(parents=True, exist_ok=True)


@dataclass
class Run:
    """A run directory plus its manifest and logger."""

    paths: RunPaths
    manifest: RunManifest
    logger: RunLogger

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    def save(self) -> None:
        self.paths.manifest.write_text(self.manifest.model_dump_json(indent=2) + "\n")

    @classmethod
    def create(
        cls,
        *,
        source: Path,
        style: str,
        fps: int,
        backend: str,
        clip_title: str,
        duration_s: float,
        mode: str = "surface",
        source_width: int = 0,
        source_height: int = 0,
        styles: StylesConfig,
        runs_dir: Path | None = None,
        echo: bool = True,
    ) -> "Run":
        styles.profile(style)  # unknown style is a hard failure, before any I/O
        base = runs_dir if runs_dir is not None else styles.output.runs_dir
        run_id = new_run_id(source)
        paths = RunPaths(Path(base) / run_id)
        paths.ensure()
        manifest = RunManifest(
            run_id=run_id,
            created_at=utc_now(),
            source_path=str(source.resolve()),
            source_sha256=sha256_file(source),
            source_duration_s=duration_s,
            source_width=source_width,
            source_height=source_height,
            clip_title=clip_title,
            style=style,
            fps=fps,
            backend=backend,
            mode=mode,
        )
        run = cls(paths=paths, manifest=manifest, logger=RunLogger(run_id, paths.log, echo))
        run.save()
        return run

    @classmethod
    def load(cls, root: Path, echo: bool = True) -> "Run":
        paths = RunPaths(root)
        if not paths.manifest.is_file():
            raise FileNotFoundError(
                f"{root} is not a ClayPipe run directory (no {MANIFEST_NAME}). "
                "Run `claypipe intake <video>` first."
            )
        manifest = RunManifest.model_validate(json.loads(paths.manifest.read_text()))
        paths.ensure()
        return cls(paths=paths, manifest=manifest, logger=RunLogger(manifest.run_id, paths.log, echo))


def resolve_run(target: Path, runs_dir: Path) -> Path:
    """Accept either a run directory or a bare run id."""
    if (target / MANIFEST_NAME).is_file():
        return target
    candidate = runs_dir / target.name
    if (candidate / MANIFEST_NAME).is_file():
        return candidate
    raise FileNotFoundError(f"no run found at {target} or {candidate}")
