"""Wan 2.7 Edit Video on fal — the PROMPT-DRIVEN restyle backend.

WHY THIS EXISTS, AND WHY VACE DID NOT WORK. Wan VACE with `task=depth` or
`task=pose` is a CONTROL model: those modes exist to hold geometry while
something else changes. Pointing it at "turn this into claymation" asks a
preservation tool to transform, and across two paid runs it did exactly what it
is built to do — returned the input with a colour grade, at SSIM 0.905 and
0.744.

`fal-ai/wan/v2.7/edit-video` is the other kind of model: instruction-driven
style transfer with NO control signal to fight. fal's own documentation example
for it is "Transform the entire scene into a beautiful watercolor painting
style. Soft brushstrokes, flowing paint washes, visible paper texture" — a
whole-scene material restyle, which is precisely the job.

THE SHAPE IS DIFFERENT FROM VACE AND THAT MATTERS:
  * It is DURATION-driven, not frame-driven. There is no `num_frames`; you ask
    for 2-10 seconds and it returns whatever frame count it likes.
  * It therefore bills per WALL-CLOCK second of output ($0.10 at 720p, $0.15 at
    1080p), not frames/16 — a different unit from VACE, which is why the price
    model carries both.
  * Because the returned frame count is not under our control, this backend
    RESAMPLES the result to exactly the frame count it was given. The pipeline's
    frame-for-frame invariant is what makes the audio re-mux provably safe, and
    it is not negotiable for a model's convenience. The resample is logged.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import ffmpeg
from ..config import ConfigError

DEFAULT_WAN_EDIT_MODEL = "fal-ai/wan/v2.7/edit-video"

# "Duration: 2-10s." — quoted from the schema. At the pipeline's 12fps that is
# 24 to 120 frames, and those are hard bounds: asking outside them is rejected
# by the endpoint, not silently clamped.
MIN_SECONDS = 2
MAX_SECONDS = 10

# `duration` is an ENUM of whole seconds, not a float.
ALLOWED_DURATIONS = (0, 2, 3, 4, 5, 6, 7, 8, 9, 10)

# The endpoint REJECTS input below 16fps: "Video frame rate is not within the
# allowed range. Minimum is 16.0 fps. Found 12.0 fps." The pipeline extracts at
# 12fps (the measured on-twos cadence of the reference format), so the clip is
# re-encoded at 24fps for upload — each 12fps frame held for two, which
# reconstructs the ~24fps cadence the source had before extraction rather than
# inventing motion. The output is resampled back to our frame count regardless.
MIN_UPLOAD_FPS = 16
UPLOAD_FPS = 24

PRICED_RESOLUTIONS = {"720p": "wan_edit_720p", "1080p": "wan_edit_1080p"}
ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")


class WanEditError(RuntimeError):
    """A Wan edit call failed, or returned something unusable."""


@runtime_checkable
class WanEditClientLike(Protocol):
    """One method, so a stub cannot drift and no path reaches the network."""

    def edit_video(
        self,
        *,
        video_path: Path,
        prompt: str,
        resolution: str,
        aspect_ratio: str,
        duration: int,
        audio_setting: str,
        seed: int,
        model: str,
    ) -> bytes:
        """Return the edited MP4 bytes, or raise WanEditError."""


@dataclass
class WanEditBackend:
    """ClipRestyleBackend over fal's Wan 2.7 Edit Video."""

    live: bool = False
    client: WanEditClientLike | None = None
    resolution: str = "720p"
    aspect_ratio: str = "1:1"
    # The pipeline re-muxes the ORIGINAL audio itself and verifies it by MD5, so
    # the model's audio is discarded either way. "origin" is chosen over "auto"
    # to avoid paying for regenerated audio nobody will hear.
    audio_setting: str = "origin"
    fps: int = 12
    _cfg: object | None = None
    planned_calls: list[dict] = field(default_factory=list)

    name = "wan_edit"
    native_fps = 12

    def __post_init__(self) -> None:
        if self.resolution not in PRICED_RESOLUTIONS:
            raise WanEditError(
                f"resolution {self.resolution!r} is not priced. fal publishes "
                f"rates for {sorted(PRICED_RESOLUTIONS)} only."
            )
        if self.aspect_ratio not in ASPECT_RATIOS:
            raise WanEditError(
                f"aspect_ratio {self.aspect_ratio!r} is not in the endpoint's "
                f"enum {ASPECT_RATIOS}"
            )
        self.native_fps = self.fps

    @property
    def min_chunk_frames(self) -> int:
        return MIN_SECONDS * self.fps

    @property
    def max_chunk_frames(self) -> int:
        return MAX_SECONDS * self.fps

    @property
    def ledger_backend(self) -> str:
        """Per RESOLUTION: 720p and 1080p are one endpoint at two rates."""
        return PRICED_RESOLUTIONS[self.resolution]

    def _cost_config(self):
        if self._cfg is not None:
            return self._cfg
        from ..config import load_weights

        self._cfg = load_weights().firewalls.cost
        return self._cfg

    def cost_per_video_second_usd(self) -> float:
        cost = self._cost_config()
        model = cost.pricing.get(self.ledger_backend)
        if model is None:
            raise ConfigError(
                f"backend {self.ledger_backend!r} is not priced in "
                "firewalls.cost.pricing. Refusing to spend against an unknown "
                "price."
            )
        if model.unit != "video_second":
            raise ConfigError(
                f"{self.ledger_backend!r} is priced per {model.unit!r}, but "
                "this endpoint bills per WALL-CLOCK second of output video. "
                "Fix the unit before spending."
            )
        return model.rate

    def cost_for_frames(self, frames: int) -> float:
        return self._cost_config().price_for(
            self.ledger_backend, video_seconds=frames / self.fps
        )

    def restyle_clip(
        self,
        src_frames: list[Path],
        out_dir: Path,
        *,
        prompt: str,
        strength: float,
        seed: int,
    ) -> list[Path]:
        if not self.live or self.client is None:
            raise NotImplementedError(
                "WanEditBackend requires --live, a non-empty FAL_KEY, and a "
                "client. No network path exists without all three."
            )
        count = len(src_frames)
        seconds = count / self.fps
        if not (MIN_SECONDS <= seconds <= MAX_SECONDS):
            raise WanEditError(
                f"this endpoint accepts {MIN_SECONDS}-{MAX_SECONDS}s and was "
                f"given {seconds:.2f}s ({count} frames at {self.fps}fps)."
            )
        duration = min(
            ALLOWED_DURATIONS[1:], key=lambda d: abs(d - seconds)
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        work = out_dir.parent / ".wanedit_work"
        work.mkdir(parents=True, exist_ok=True)
        source_clip = work / f"in_{src_frames[0].stem}_{src_frames[-1].stem}.mp4"
        # Held at the pipeline's cadence but PRESENTED at UPLOAD_FPS, so the
        # duration is unchanged and the endpoint's 16fps floor is met.
        _encode(src_frames, source_clip, hold_fps=self.fps, upload_fps=UPLOAD_FPS)

        payload = self.client.edit_video(
            video_path=source_clip,
            prompt=prompt,
            resolution=self.resolution,
            aspect_ratio=self.aspect_ratio,
            duration=duration,
            audio_setting=self.audio_setting,
            seed=seed,
            model=DEFAULT_WAN_EDIT_MODEL,
        )
        if not payload:
            raise WanEditError("the endpoint returned an empty response body")

        generated = work / f"out_{src_frames[0].stem}_{src_frames[-1].stem}.mp4"
        generated.write_bytes(payload)

        # The endpoint chooses its own frame count. The pipeline's
        # frame-for-frame invariant is what makes the audio re-mux provably
        # safe, so the result is resampled to EXACTLY the count we supplied
        # rather than the invariant being relaxed for the model's convenience.
        written = _decode_to_count(
            generated, out_dir, names=[p.name for p in src_frames]
        )
        if len(written) != count:
            raise WanEditError(
                f"resampling produced {len(written)} frames for a {count}-frame "
                "request. Refusing: a mismatch here desyncs the audio in a file "
                "that plays."
            )
        return written

    def plan(self, src_frames: list[Path], *, prompt: str, seed: int) -> dict:
        count = len(src_frames)
        entry = {
            "model": DEFAULT_WAN_EDIT_MODEL,
            "ledger_backend": self.ledger_backend,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "frames": count,
            "video_seconds": round(count / self.fps, 4),
            "estimated_usd": round(self.cost_for_frames(count), 6),
            "seed": seed,
            "prompt_chars": len(prompt),
        }
        self.planned_calls.append(entry)
        return entry


def _encode(frames: list[Path], dst: Path, *, hold_fps: int, upload_fps: int) -> Path:
    """Frames -> MP4 at `upload_fps`, each frame HELD for 1/hold_fps seconds.

    Duration is set by `hold_fps` and the container rate by `upload_fps`, so a
    12fps sequence becomes a 24fps clip of the same length with every frame
    doubled. That clears the endpoint's 16fps floor without inventing motion —
    and it reconstructs the cadence the source actually had, since the 12fps
    extraction came off ~24fps footage in the first place.
    """
    if not frames:
        raise WanEditError("cannot encode an empty frame range")
    if upload_fps < MIN_UPLOAD_FPS:
        raise WanEditError(
            f"upload_fps {upload_fps} is below the endpoint's {MIN_UPLOAD_FPS}fps "
            "floor; the call would be rejected before generating."
        )
    listing = dst.with_suffix(".txt")
    listing.write_text(
        "".join(f"file '{p.resolve()}'\nduration {1 / hold_fps:.6f}\n" for p in frames)
    )
    ffmpeg.run(
        ["-f", "concat", "-safe", "0", "-i", str(listing),
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
         "-an", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
         "-r", str(upload_fps), str(dst)],
        what="Wan edit source encode",
    )
    return dst


def _decode_to_count(video: Path, out_dir: Path, *, names: list[str]) -> list[Path]:
    """Decode to EXACTLY len(names) frames, resampling temporally if needed."""
    staging = out_dir.parent / ".wanedit_decoded"
    if staging.exists():
        for stale in staging.glob("*.png"):
            stale.unlink()
    staging.mkdir(parents=True, exist_ok=True)

    wanted = len(names)
    duration = ffmpeg.duration_seconds(video)
    target_fps = wanted / duration if duration > 0 else wanted
    ffmpeg.run(
        ["-i", str(video), "-vf", f"fps={target_fps:.6f}",
         "-frames:v", str(wanted), str(staging / "d_%05d.png")],
        what="Wan edit output decode",
    )
    decoded = sorted(staging.glob("d_*.png"))
    written: list[Path] = []
    for index, name in enumerate(names):
        source = decoded[min(index, len(decoded) - 1)] if decoded else None
        if source is None:
            break
        dst = out_dir / name
        dst.write_bytes(source.read_bytes())
        written.append(dst)
    return written


def get_wan_edit_client(*, live: bool):
    if not live:
        return None
    try:
        import fal_client  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise WanEditError(
            "the fal client is an optional extra: pip install -e '.[fal]'"
        ) from exc
    return _FalWanEditClient()


class _FalWanEditClient:  # pragma: no cover - requires network and a credential
    def edit_video(
        self, *, video_path, prompt, resolution, aspect_ratio, duration,
        audio_setting, seed, model,
    ) -> bytes:
        import fal_client
        import httpx

        uploaded = fal_client.upload_file(str(video_path))
        arguments = {
            "video_url": uploaded,
            "prompt": prompt,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "duration": duration,
            "audio_setting": audio_setting,
            "seed": seed,
        }
        try:
            result = fal_client.subscribe(model, arguments=arguments)
        except Exception as exc:
            raise WanEditError(f"fal call to {model} failed: {exc}") from exc

        url = (result or {}).get("video", {}).get("url")
        if not url:
            raise WanEditError(
                f"fal returned no video url for {model}",
                )
        # httpx, not urllib: urllib uses the SYSTEM trust store and fails behind
        # TLS interception, which once cost a paid generation that had already
        # succeeded (D65).
        try:
            response = httpx.get(url, timeout=600.0, follow_redirects=True)
            response.raise_for_status()
        except Exception as exc:
            raise WanEditError(
                f"the generation SUCCEEDED and was CHARGED but could not be "
                f"downloaded from {url}: {exc}"
            ) from exc
        return response.content
