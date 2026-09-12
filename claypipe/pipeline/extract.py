"""Stage: frame + audio extraction (SPEC §1).

The audio is extracted ONCE, losslessly, and is read-only for the rest of the
run — it is never re-encoded, only re-muxed. That is the sync guarantee.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .. import ffmpeg
from ..logging import RunLogger

FRAME_GLOB = "f_*.png"
FRAME_PATTERN = "f_%05d.png"
SENTINEL_NAME = ".extract_complete"


class ExtractError(RuntimeError):
    """Extraction produced nothing usable."""


def frame_paths(directory: Path) -> list[Path]:
    """Frames in strict numeric order — zero-padded names make this trivial."""
    return sorted(directory.glob(FRAME_GLOB))


def count_frames(directory: Path) -> int:
    return len(frame_paths(directory))


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def read_sentinel(out_dir: Path) -> dict | None:
    """The completion record, or None if extraction never finished."""
    path = out_dir / SENTINEL_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def extract_frames(
    source: Path, out_dir: Path, fps: int, logger: RunLogger,
    incident_dir: Path | None = None,
) -> int:
    """Extract frames at `fps` as f_00001.png …

    RESUMABILITY IS DECIDED BY A SENTINEL, NOT BY A FRAME COUNT. A directory
    with frames in it proves only that ffmpeg started — a run killed partway
    leaves a plausible-looking directory that the old count-based check would
    have accepted and silently built a short clip from. The sentinel is written
    only after ffmpeg exits successfully and records the frame count and the
    SOURCE HASH, so a changed input re-extracts instead of reusing frames that
    belong to a different video.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = count_frames(out_dir)
    source_hash = _sha256(source)
    sentinel = read_sentinel(out_dir)

    if sentinel is not None:
        if sentinel.get("frames") == existing and sentinel.get("source_sha256") == source_hash:
            logger.info(
                "extract.frames.skip", reason="sentinel matches", frames=existing,
                extracted_at=sentinel.get("extracted_at"),
            )
            return existing
        logger.warn(
            "extract.frames.stale_sentinel",
            recorded_frames=sentinel.get("frames"), found_frames=existing,
            source_changed=sentinel.get("source_sha256") != source_hash,
        )
    elif existing:
        # Frames without a sentinel: ffmpeg started and never finished. Say so
        # in the record rather than building a short clip from the remains.
        if incident_dir is not None:
            from .retry import write_incident
            from ..run import RunPaths

            write_incident(
                RunPaths(incident_dir), "incomplete_extract",
                {
                    "frames_found": existing, "expected_sentinel": SENTINEL_NAME,
                    "note": (
                        "Frames were present with no completion sentinel, so a "
                        "previous extraction was interrupted. Re-extracting; the "
                        "partial frames would otherwise have produced a short clip."
                    ),
                },
                logger,
            )
        logger.warn("extract.frames.incomplete", frames_found=existing)

    for stale in out_dir.glob(FRAME_GLOB):
        stale.unlink()
    (out_dir / SENTINEL_NAME).unlink(missing_ok=True)

    ffmpeg.run(
        ["-i", str(source), "-vf", f"fps={fps}", "-start_number", "1",
         str(out_dir / FRAME_PATTERN)],
        what=f"frame extraction at {fps}fps",
    )
    count = count_frames(out_dir)
    if count == 0:
        raise ExtractError(f"no frames extracted from {source}")

    (out_dir / SENTINEL_NAME).write_text(
        json.dumps(
            {
                "frames": count,
                "extracted_at": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "source_sha256": source_hash,
                "fps": fps,
            },
            indent=2,
        )
        + "\n"
    )
    logger.info("extract.frames", frames=count, fps=fps, dir=str(out_dir))
    return count


def extract_audio(source: Path, out_path: Path, logger: RunLogger) -> str:
    """Copy the audio track out losslessly. Returns its packet MD5.

    NEVER re-encodes (SPEC Hard Rules) — `-c:a copy` only.
    """
    audio_stream = ffmpeg.stream(source, "audio")  # hard fail if the clip is silent
    if out_path.is_file():
        digest = ffmpeg.stream_md5(out_path, "audio")
        logger.info("extract.audio.skip", reason="already extracted", md5=digest)
        return digest

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        ["-i", str(source), "-vn", "-map", "0:a:0", "-c:a", "copy", str(out_path)],
        what="audio extraction (copy)",
    )
    digest = ffmpeg.stream_md5(out_path, "audio")
    logger.info(
        "extract.audio",
        codec=audio_stream.get("codec_name"),
        sample_rate=audio_stream.get("sample_rate"),
        channels=audio_stream.get("channels"),
        md5=digest,
        path=str(out_path),
    )
    return digest
