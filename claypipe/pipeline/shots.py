"""Shot detection (MASTER_PLAN F2/§1.3).

`shots.py` is the keystone three separate problems collapse into:

  1. **Cost.** ~29 shots per 60s in the denser reference clip, ~24 frames per
     shot at 12fps. Restyling shot-first frames and propagating the rest is the
     difference between ~30-60 paid generations and 720 (T18 spends this).
  2. **False temporal flags.** A hard cut looks EXACTLY like catastrophic
     temporal failure to a flow-warped residual — the previous frame is a
     different scene, so the warp is meaningless and TF collapses. That is the
     D15/D34 argument, and the fix is structural: at a shot boundary there is
     no previous frame, so the scorer is handed None and uses the first-frame
     value instead of measuring drift against an unrelated image.
  3. **Seeding.** "Per-shot fixed seed" was specified and has been per-CLIP
     since step 1. A shot is the unit a seed should be stable across: holding
     one seed across a cut buys nothing, and changing it mid-shot is visible.

Detection runs on the SOURCE VIDEO at its own frame rate, then maps to the
run's extracted-frame numbering. Those are different clocks — a 24fps source
extracted at 12fps has half as many frames — and conflating them puts every
boundary in the wrong place, silently.

PySceneDetect is an optional extra (D3, `pip install -e '.[shots]'`). Absent,
this module raises with the install line rather than falling back to a
whole-clip single shot: a silent fallback would quietly restore per-clip
seeding and re-introduce the false TF flags, which is the bug, not the
mitigation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# The seed for shot i is SEED_BASE + i. Stable across a shot, different across
# a cut, and reproducible for a re-run.
SEED_BASE = 1000

# ContentDetector's HSV content threshold. 27.0 is PySceneDetect's default and
# reproduces the reference measurement (41 cuts on clip A, 14 on clip B) within
# tolerance; it lives here rather than in weights.yaml because it is a property
# of the detector, not a tuning knob on the fidelity score.
DEFAULT_THRESHOLD = 27.0

# A "shot" shorter than this is a flash frame or a detector hiccup, not a shot
# worth its own seed. The shortest real shot measured in the reference clips is
# 0.21s = 3 frames at 12fps, so the floor sits below that deliberately.
MIN_SHOT_FRAMES = 2

INSTALL_HINT = (
    "shot detection needs PySceneDetect, which is an optional extra:\n"
    "    pip install -e '.[shots]'\n"
    "Refusing to fall back to a single whole-clip shot — that would silently "
    "restore per-clip seeding and the false temporal flags at every cut."
)


class ShotDetectionError(RuntimeError):
    """Shot detection could not run, or produced an unusable plan."""


@dataclass(frozen=True)
class Shot:
    """One shot, in the run's EXTRACTED-frame numbering (1-based, inclusive)."""

    index: int
    start_frame: int
    end_frame: int
    start_s: float
    end_s: float
    motion: float = 0.0

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame + 1

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def seed(self) -> int:
        return SEED_BASE + self.index

    def contains(self, frame_index: int) -> bool:
        return self.start_frame <= frame_index <= self.end_frame

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "start_s": round(self.start_s, 4),
            "end_s": round(self.end_s, 4),
            "frames": self.frame_count,
            "seed": self.seed,
            "motion": round(self.motion, 5),
        }


@dataclass(frozen=True)
class ShotPlan:
    """Every shot in a run, plus the lookups the pipeline actually asks for."""

    fps: int
    total_frames: int
    shots: list[Shot]
    detector: str = "content"
    threshold: float = DEFAULT_THRESHOLD
    source_fps: float = 0.0

    # -- lookups ---------------------------------------------------------
    def shot_for_frame(self, frame_index: int) -> Shot:
        """The shot a 1-based extracted frame belongs to."""
        for shot in self.shots:
            if shot.contains(frame_index):
                return shot
        raise ShotDetectionError(
            f"frame {frame_index} falls outside every shot in a plan covering "
            f"1..{self.total_frames}. The plan and the extracted frames "
            "disagree, which means one of them belongs to a different run."
        )

    def seed_for_frame(self, frame_index: int) -> int:
        """Per-shot fixed seed. This is what `restyle_frames(seed_for=...)` wants."""
        return self.shot_for_frame(frame_index).seed

    def boundary_frames(self) -> set[int]:
        """First frame of every shot AFTER the first.

        These are the frames whose predecessor is a different scene. Temporal
        fidelity must not be measured across them — see the module docstring.
        """
        return {shot.start_frame for shot in self.shots[1:]}

    def is_boundary(self, frame_index: int) -> bool:
        return frame_index in self.boundary_frames()

    def keyframes(self) -> list[int]:
        """One frame per shot: the frame T18 pays to generate."""
        return [shot.start_frame for shot in self.shots]

    def most_motion_shot(self) -> Shot:
        """The busiest shot. Closes D30 — the canary's third slot was the LAST
        frame as a stated stand-in for exactly this."""
        if not self.shots:
            raise ShotDetectionError("no shots in plan")
        return max(self.shots, key=lambda s: (s.motion, s.frame_count))

    def most_motion_frame(self) -> int:
        """A frame from the middle of the busiest shot — not its first frame,
        which is often the calm instant before the action it was cut to."""
        shot = self.most_motion_shot()
        return shot.start_frame + shot.frame_count // 2

    @property
    def cuts(self) -> int:
        return max(0, len(self.shots) - 1)

    def summary(self) -> dict:
        lengths = [s.duration_s for s in self.shots]
        span = self.total_frames / self.fps if self.fps else 0.0
        return {
            "shots": len(self.shots),
            "cuts": self.cuts,
            "detector": self.detector,
            "threshold": self.threshold,
            "fps": self.fps,
            "source_fps": round(self.source_fps, 4),
            "total_frames": self.total_frames,
            "avg_shot_s": round(sum(lengths) / len(lengths), 3) if lengths else 0.0,
            "median_shot_s": round(sorted(lengths)[len(lengths) // 2], 3) if lengths else 0.0,
            "shortest_shot_s": round(min(lengths), 3) if lengths else 0.0,
            "shots_per_60s": round(len(self.shots) / span * 60, 2) if span else 0.0,
            "most_motion_shot": self.most_motion_shot().index if self.shots else None,
        }

    # -- persistence -----------------------------------------------------
    def as_dict(self) -> dict:
        return {"schema_version": 1, **self.summary(), "shot_list": [s.as_dict() for s in self.shots]}

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2) + "\n")
        tmp.replace(path)
        return path

    @classmethod
    def read(cls, path: Path) -> "ShotPlan":
        data = json.loads(path.read_text())
        return cls(
            fps=data["fps"],
            total_frames=data["total_frames"],
            detector=data.get("detector", "content"),
            threshold=data.get("threshold", DEFAULT_THRESHOLD),
            source_fps=data.get("source_fps", 0.0),
            shots=[
                Shot(
                    index=s["index"],
                    start_frame=s["start_frame"],
                    end_frame=s["end_frame"],
                    start_s=s["start_s"],
                    end_s=s["end_s"],
                    motion=s.get("motion", 0.0),
                )
                for s in data["shot_list"]
            ],
        )


def single_shot_plan(total_frames: int, fps: int) -> ShotPlan:
    """A one-shot plan. EXPLICIT fallback only — never an automatic one.

    Used when a clip genuinely has no cuts, and by tests that need a plan
    without a detector. Reproduces step-1 behaviour exactly: seed 1000 for
    every frame, no boundaries.
    """
    if total_frames < 1:
        raise ShotDetectionError(f"a plan needs at least one frame, got {total_frames}")
    return ShotPlan(
        fps=fps,
        total_frames=total_frames,
        detector="single",
        shots=[
            Shot(
                index=0,
                start_frame=1,
                end_frame=total_frames,
                start_s=0.0,
                end_s=total_frames / fps,
            )
        ],
    )


def _scene_list(video: Path, threshold: float) -> tuple[list[tuple[float, float]], float]:
    """PySceneDetect boundaries as (start_s, end_s), plus the source fps."""
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ShotDetectionError(INSTALL_HINT) from exc

    try:
        stream = open_video(str(video))
        manager = SceneManager()
        manager.add_detector(ContentDetector(threshold=threshold))
        manager.detect_scenes(stream, show_progress=False)
        scenes = manager.get_scene_list()
        source_fps = float(stream.frame_rate)
    except Exception as exc:
        raise ShotDetectionError(f"shot detection failed on {video}: {exc}") from exc

    if not scenes:
        # A clip with no detected cut is one shot, not zero. PySceneDetect
        # returns an empty list for that case rather than a single span.
        return [], source_fps
    # `.seconds` — get_seconds() is deprecated in PySceneDetect 0.7.
    return [(s.seconds, e.seconds) for s, e in scenes], source_fps


def _to_extracted_frames(
    spans: list[tuple[float, float]], *, fps: int, total_frames: int
) -> list[tuple[int, int, float, float]]:
    """Map second-spans onto 1-based extracted frame indices.

    The source clock and the extraction clock are different. A boundary at
    t seconds is the FIRST extracted frame at or after t, which is
    `floor(t * fps) + 1` — extracted frame n covers [(n-1)/fps, n/fps).
    """
    mapped: list[tuple[int, int, float, float]] = []
    for start_s, end_s in spans:
        start = int(start_s * fps) + 1
        end = min(total_frames, int(end_s * fps))
        if end < start:
            continue
        mapped.append((start, end, start_s, end_s))
    if not mapped:
        return mapped

    # Force the plan to cover 1..total_frames with no gap and no overlap. The
    # detector works on the source's own timeline and rounding at two different
    # frame rates leaves seams; a seam would make shot_for_frame raise mid-batch.
    merged: list[tuple[int, int, float, float]] = []
    cursor = 1
    for start, end, start_s, end_s in mapped:
        start = max(start, cursor)
        if end < start:
            continue
        merged.append((start, end, start_s, end_s))
        cursor = end + 1
    if merged:
        first = merged[0]
        merged[0] = (1, first[1], first[2], first[3])
        last = merged[-1]
        merged[-1] = (last[0], total_frames, last[2], last[3])
    return merged


def _shot_motion(frames_dir: Path, shots: list[Shot]) -> dict[int, float]:
    """Mean inter-frame absolute difference per shot, from the extracted frames.

    This is the same measurement that produced §1.2's cadence table, at shot
    granularity. It decides the canary's most-motion slot (D30), so it reads
    the SOURCE frames — the operator is choosing which moment to inspect, and
    that must not depend on what the backend did to it.
    """
    from .extract import frame_paths

    paths = frame_paths(frames_dir)
    if not paths:
        return {}
    try:
        import numpy as np
        from PIL import Image
    except ImportError:  # pragma: no cover
        return {}

    def small_gray(path: Path):
        with Image.open(path) as img:
            return np.asarray(img.convert("L").resize((96, 54)), dtype=np.float32)

    motion: dict[int, float] = {}
    for shot in shots:
        # Sample at most 24 frames per shot: motion energy is an ordering
        # signal, and reading every frame of a long clip to rank shots is waste.
        lo, hi = shot.start_frame, min(shot.end_frame, len(paths))
        if hi <= lo:
            motion[shot.index] = 0.0
            continue
        step = max(1, (hi - lo) // 24)
        indices = list(range(lo, hi + 1, step))
        deltas = []
        prev = None
        for i in indices:
            current = small_gray(paths[i - 1])
            if prev is not None:
                deltas.append(float(np.abs(current - prev).mean()))
            prev = current
        motion[shot.index] = sum(deltas) / len(deltas) if deltas else 0.0
    return motion


def detect_shots(
    *,
    video: Path,
    fps: int,
    total_frames: int,
    frames_dir: Path | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    min_shot_frames: int = MIN_SHOT_FRAMES,
) -> ShotPlan:
    """Detect shots in `video` and express them in extracted-frame numbering.

    `frames_dir`, when given, is used to rank shots by motion energy so the
    canary can show a real most-motion frame (D30). Without it the plan is
    still complete — motion is 0.0 everywhere and most_motion_shot falls back
    to the longest shot.
    """
    if not video.is_file():
        raise ShotDetectionError(f"source video not found: {video}")
    if total_frames < 1:
        raise ShotDetectionError(f"a plan needs at least one frame, got {total_frames}")

    spans, source_fps = _scene_list(video, threshold)
    mapped = _to_extracted_frames(spans, fps=fps, total_frames=total_frames)

    if not mapped:
        plan = single_shot_plan(total_frames, fps)
        return ShotPlan(
            fps=fps, total_frames=total_frames, shots=plan.shots,
            detector="content", threshold=threshold, source_fps=source_fps,
        )

    # Absorb runs too short to be a shot into their predecessor, rather than
    # dropping them — dropping one would leave a hole in the frame coverage.
    kept: list[tuple[int, int, float, float]] = []
    for span in mapped:
        start, end, start_s, end_s = span
        if kept and (end - start + 1) < min_shot_frames:
            p_start, _p_end, p_start_s, _p_end_s = kept[-1]
            kept[-1] = (p_start, end, p_start_s, end_s)
            continue
        kept.append(span)

    shots = [
        Shot(index=i, start_frame=start, end_frame=end, start_s=start_s, end_s=end_s)
        for i, (start, end, start_s, end_s) in enumerate(kept)
    ]

    if frames_dir is not None and frames_dir.is_dir():
        motion = _shot_motion(frames_dir, shots)
        shots = [
            Shot(
                index=s.index, start_frame=s.start_frame, end_frame=s.end_frame,
                start_s=s.start_s, end_s=s.end_s, motion=motion.get(s.index, 0.0),
            )
            for s in shots
        ]

    plan = ShotPlan(
        fps=fps, total_frames=total_frames, shots=shots,
        detector="content", threshold=threshold, source_fps=source_fps,
    )
    _assert_covers(plan)
    return plan


def _assert_covers(plan: ShotPlan) -> None:
    """Every extracted frame belongs to exactly one shot, or the plan is a bug.

    Asserted rather than trusted because the failure mode is a KeyError deep
    inside a paid batch, hundreds of frames in.
    """
    expected = 1
    for shot in plan.shots:
        if shot.start_frame != expected:
            raise ShotDetectionError(
                f"shot plan has a seam: shot {shot.index} starts at frame "
                f"{shot.start_frame}, expected {expected}"
            )
        expected = shot.end_frame + 1
    if expected != plan.total_frames + 1:
        raise ShotDetectionError(
            f"shot plan covers frames 1..{expected - 1} but the run has "
            f"{plan.total_frames} frames"
        )
