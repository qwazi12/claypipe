"""Stage: frame + audio extraction (SPEC §1).

The audio is extracted ONCE, losslessly, and is read-only for the rest of the
run — it is never re-encoded, only re-muxed. That is the sync guarantee.
"""

from __future__ import annotations

from pathlib import Path

from .. import ffmpeg
from ..logging import RunLogger

FRAME_GLOB = "f_*.png"
FRAME_PATTERN = "f_%05d.png"


class ExtractError(RuntimeError):
    """Extraction produced nothing usable."""


def frame_paths(directory: Path) -> list[Path]:
    """Frames in strict numeric order — zero-padded names make this trivial."""
    return sorted(directory.glob(FRAME_GLOB))


def count_frames(directory: Path) -> int:
    return len(frame_paths(directory))


def extract_frames(source: Path, out_dir: Path, fps: int, logger: RunLogger) -> int:
    """Extract frames at `fps` as f_00001.png … Resume-safe: a populated
    directory is reused rather than re-extracted."""
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = count_frames(out_dir)
    if existing:
        logger.info("extract.frames.skip", reason="already extracted", frames=existing)
        return existing

    ffmpeg.run(
        ["-i", str(source), "-vf", f"fps={fps}", "-start_number", "1",
         str(out_dir / FRAME_PATTERN)],
        what=f"frame extraction at {fps}fps",
    )
    count = count_frames(out_dir)
    if count == 0:
        raise ExtractError(f"no frames extracted from {source}")
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
