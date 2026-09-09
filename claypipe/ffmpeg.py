"""ffmpeg/ffprobe subprocess layer (SPEC Tech Stack).

All video I/O goes through here so retries, logging and the startup capability
check live in exactly one place (Rule 39). ffmpeg is a system binary; a missing
or unusable one is a hard startup failure with an install message, never a
silent degradation (Rule 5).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

INSTALL_HINT = (
    "ffmpeg and ffprobe are required by ClayPipe.\n"
    "  macOS:  brew install ffmpeg\n"
    "  Debian: sudo apt-get install ffmpeg\n"
    "Or point CLAYPIPE_FFMPEG / CLAYPIPE_FFPROBE at the binaries."
)


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed, or the toolchain is unusable."""


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: str
    ffprobe: str
    version: str


def _resolve(env_var: str, name: str) -> str:
    override = os.environ.get(env_var)
    if override:
        if not Path(override).is_file():
            raise FFmpegError(f"{env_var}={override} is not a file.\n{INSTALL_HINT}")
        return override
    found = shutil.which(name)
    if not found:
        raise FFmpegError(f"{name} not found on PATH.\n{INSTALL_HINT}")
    return found


def require_ffmpeg() -> FFmpegTools:
    """Startup check. Call before any command does work."""
    ffmpeg = _resolve("CLAYPIPE_FFMPEG", "ffmpeg")
    ffprobe = _resolve("CLAYPIPE_FFPROBE", "ffprobe")
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-version"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FFmpegError(f"{ffmpeg} is present but not runnable: {exc}\n{INSTALL_HINT}") from exc
    return FFmpegTools(ffmpeg=ffmpeg, ffprobe=ffprobe, version=out.splitlines()[0])


def has_filter(name: str) -> bool:
    """Whether this ffmpeg build ships a given filter.

    ffmpeg builds vary: Homebrew's ffmpeg 8 has no `drawtext`, which is why the
    header bar is composited from a PIL-rendered PNG instead (memory.md D1).
    """
    tools = require_ffmpeg()
    out = subprocess.run(
        [tools.ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True
    ).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines() if len(line.split()) > 1)


# A filtergraph with an unbounded source (a `color` generator, a looped image)
# renders until the disk fills. Every invocation is therefore time-boxed.
DEFAULT_TIMEOUT_S = 900


def run(args: list[str], *, what: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run ffmpeg with the given args. Raises FFmpegError with a stderr tail."""
    tools = require_ffmpeg()
    try:
        proc = subprocess.run(
            [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", *args],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(
            f"{what} exceeded {timeout_s}s and was killed. An unbounded input in "
            "the filtergraph is the usual cause."
        ) from exc
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-20:])
        raise FFmpegError(f"{what} failed (exit {proc.returncode}):\n{tail}")
    return proc


def probe(path: Path) -> dict:
    """ffprobe -show_format -show_streams as a dict."""
    tools = require_ffmpeg()
    proc = subprocess.run(
        [tools.ffprobe, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {path}:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def stream(path: Path, kind: str) -> dict:
    """First stream of the given codec_type ('video'/'audio'). Hard fail if absent."""
    for st in probe(path).get("streams", []):
        if st.get("codec_type") == kind:
            return st
    raise FFmpegError(f"{path} has no {kind} stream")


def duration_seconds(path: Path) -> float:
    fmt = probe(path).get("format", {})
    try:
        return float(fmt["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FFmpegError(f"could not read duration of {path}") from exc


def stream_md5(path: Path, kind: str) -> str:
    """MD5 of a stream's *packet payloads*, container framing excluded.

    This is the audio sync guarantee (SPEC §6, Hard Rules): the same MD5 before
    and after assembly proves the audio was copied bit-for-bit, never re-encoded.

    AAC needs care. The same audio carries a 7-byte ADTS header per frame in a
    raw `.aac` file and none inside MP4, so a naive hash reports a difference
    that is pure framing. `aac_adtstoasc` normalises both to the bare payload —
    it strips the headers from ADTS and is a no-op on MP4 (verified, not
    assumed: identical MD5 with and without it on an MP4 input).
    """
    tools = require_ffmpeg()
    selector = {"audio": "a", "video": "v"}[kind]
    args = [tools.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
            "-map", f"0:{selector}:0", "-c", "copy"]
    if kind == "audio" and stream(path, "audio").get("codec_name") == "aac":
        args += ["-bsf:a", "aac_adtstoasc"]
    args += ["-f", "md5", "-"]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"hashing {kind} stream of {path} failed:\n{proc.stderr.strip()}")
    out = proc.stdout.strip()
    if not out.startswith("MD5="):
        raise FFmpegError(f"unexpected md5 muxer output for {path}: {out!r}")
    return out.removeprefix("MD5=")
