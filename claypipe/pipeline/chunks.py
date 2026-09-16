"""Shot-aligned chunk planning for video-to-video generation (V5).

A 60-second clip is ~26 shots and several generation chunks, and NOTHING in a
v2v model guarantees a character's clay design is the same in chunk 1 and chunk
6. That inconsistency is what makes output read as slop rather than animation,
so it is the risk this module exists to manage.

TWO DECISIONS, and the first inverts what the per-frame path did:

1. ONE SEED FOR THE WHOLE RUN, not one per shot.
   T11 gave every shot its own seed, which was right for per-frame img2img:
   there the seed drives retry variety and a shot is the unit a look should be
   stable across. Under v2v the seed drives the GENERATED DESIGN — change it
   between chunks and the character is redesigned at every seam, which is
   precisely the identity drift §6 names as a headline risk. So the seed is
   fixed per run and reused for every chunk.

2. SEAMS LAND ON SHOT CUTS.
   A seam is where two independently generated chunks meet, and the design can
   shift across it. On a hard cut that shift is invisible, because the viewer
   already expects everything to change. Mid-shot it is a jump-cut in the
   character's face. So chunks are built by accumulating whole shots, and a
   mid-shot seam is only cut when the model's frame bounds force one — and is
   REPORTED rather than silently accepted.

The bounds are the backend's, not ours: VACE accepts 81-241 frames per call, so
a shot shorter than 81 frames cannot be a chunk on its own and a shot longer
than 241 must be split.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .shots import ShotPlan


class ChunkPlanError(RuntimeError):
    """A chunk plan could not be built, or does not cover the run."""


@dataclass(frozen=True)
class Chunk:
    """One generation call: a contiguous frame range, 1-based and inclusive."""

    index: int
    start_frame: int
    end_frame: int
    starts_on_cut: bool
    ends_on_cut: bool
    shot_indices: tuple[int, ...]

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame + 1

    @property
    def is_mid_shot_seam(self) -> bool:
        """A seam the viewer can see. True when this chunk BEGINS mid-shot."""
        return not self.starts_on_cut and self.index > 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "frames": self.frame_count,
            "starts_on_cut": self.starts_on_cut,
            "ends_on_cut": self.ends_on_cut,
            "mid_shot_seam": self.is_mid_shot_seam,
            "shots": list(self.shot_indices),
        }


@dataclass(frozen=True)
class ChunkPlan:
    """Every generation call for a run, plus where its seams fall."""

    total_frames: int
    chunks: list[Chunk]
    seed: int
    min_chunk_frames: int
    max_chunk_frames: int

    @property
    def mid_shot_seams(self) -> list[Chunk]:
        return [c for c in self.chunks if c.is_mid_shot_seam]

    @property
    def seam_frames(self) -> list[int]:
        """The first frame of every chunk after the first — where designs can shift."""
        return [c.start_frame for c in self.chunks[1:]]

    def covers(self) -> bool:
        expected = 1
        for chunk in self.chunks:
            if chunk.start_frame != expected:
                return False
            expected = chunk.end_frame + 1
        return expected == self.total_frames + 1

    def summary(self) -> dict:
        return {
            "chunks": len(self.chunks),
            "total_frames": self.total_frames,
            "seed": self.seed,
            "seams": len(self.seam_frames),
            "mid_shot_seams": len(self.mid_shot_seams),
            "mid_shot_seam_frames": [c.start_frame for c in self.mid_shot_seams],
            "aligned_fraction": (
                round(1.0 - len(self.mid_shot_seams) / max(len(self.seam_frames), 1), 4)
                if self.seam_frames else 1.0
            ),
            "min_chunk_frames": self.min_chunk_frames,
            "max_chunk_frames": self.max_chunk_frames,
        }

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            **self.summary(),
            "chunk_list": [c.as_dict() for c in self.chunks],
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2) + "\n")
        tmp.replace(path)
        return path

    @classmethod
    def read(cls, path: Path) -> "ChunkPlan":
        data = json.loads(path.read_text())
        return cls(
            total_frames=data["total_frames"],
            seed=data["seed"],
            min_chunk_frames=data["min_chunk_frames"],
            max_chunk_frames=data["max_chunk_frames"],
            chunks=[
                Chunk(
                    index=c["index"], start_frame=c["start_frame"],
                    end_frame=c["end_frame"], starts_on_cut=c["starts_on_cut"],
                    ends_on_cut=c["ends_on_cut"],
                    shot_indices=tuple(c["shots"]),
                )
                for c in data["chunk_list"]
            ],
        )


def plan_chunks(
    *,
    shot_plan: ShotPlan,
    seed: int,
    min_chunk_frames: int,
    max_chunk_frames: int,
) -> ChunkPlan:
    """Build generation chunks that prefer to break on shot cuts.

    Greedy over whole shots: accumulate shots while they fit, close the chunk at
    a cut, and only split mid-shot when a single shot exceeds the model's
    maximum or a tail is below its minimum. Every forced mid-shot seam is
    recorded so the QC card can show it rather than the operator discovering it
    in the output.
    """
    if min_chunk_frames > max_chunk_frames:
        raise ChunkPlanError(
            f"min_chunk_frames ({min_chunk_frames}) exceeds max_chunk_frames "
            f"({max_chunk_frames})"
        )
    total = shot_plan.total_frames
    if total < 1:
        raise ChunkPlanError(f"a chunk plan needs at least one frame, got {total}")

    cut_frames = {shot.start_frame for shot in shot_plan.shots}
    chunks: list[Chunk] = []
    start = 1

    while start <= total:
        remaining = total - start + 1
        # The furthest this chunk may reach.
        hard_end = min(total, start + max_chunk_frames - 1)

        # Prefer to end just before a cut, as late as possible while still
        # leaving a viable tail.
        candidate_ends = [
            frame - 1
            for frame in sorted(cut_frames)
            if start + min_chunk_frames - 1 <= frame - 1 <= hard_end
        ]
        # A tail shorter than the model's minimum cannot be generated on its
        # own, so an end that would strand one is not a candidate.
        viable = [
            end for end in candidate_ends
            if total - end == 0 or total - end >= min_chunk_frames
        ]
        if viable:
            end = max(viable)
        elif remaining <= max_chunk_frames:
            # Everything left fits in one call — take it, even though it may
            # start or end mid-shot.
            end = total
        else:
            # Forced mid-shot break: no cut is reachable that leaves a viable
            # tail, so cut at the maximum and leave enough for one more call.
            end = min(hard_end, total - min_chunk_frames)
            if end < start + min_chunk_frames - 1:
                end = min(hard_end, total)

        shot_indices = tuple(
            sorted(
                {
                    shot_plan.shot_for_frame(f).index
                    for f in (start, end, (start + end) // 2)
                }
            )
        )
        chunks.append(
            Chunk(
                index=len(chunks),
                start_frame=start,
                end_frame=end,
                starts_on_cut=(start in cut_frames),
                ends_on_cut=((end + 1) in cut_frames or end == total),
                shot_indices=shot_indices,
            )
        )
        start = end + 1

    plan = ChunkPlan(
        total_frames=total, chunks=chunks, seed=seed,
        min_chunk_frames=min_chunk_frames, max_chunk_frames=max_chunk_frames,
    )
    if not plan.covers():
        raise ChunkPlanError(
            "chunk plan does not cover the run exactly: "
            f"{[c.as_dict() for c in chunks]}"
        )
    return plan
