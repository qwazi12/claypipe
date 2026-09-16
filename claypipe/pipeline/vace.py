"""Wan VACE 14B on fal — the video-to-video restyle backend (V2).

THE ARCHITECTURAL SHIFT this implements: the whole frame is restyled in one
pass, characters and environment together, with the source video as a control
signal so motion and shot timing survive. That is what satisfies all three
requirements at once — every character in frame becomes clay because the whole
frame does (R2), and the environment is rebuilt in the medium alongside them
(R3), while aspect is handled upstream in the layout engine (R1).

Why this works for clay where it would not for LEGO: a minifig needs radical
geometry replacement (cylinder head, claw hands, nothing human-proportioned),
while Aardman-style clay characters are human-shaped with limbs and faces where
you expect them. The geometry problem that rules out restyle for LEGO is much
softer for clay.

THE IMPEDANCE MISMATCH this module absorbs: `ClipRestyleBackend` speaks FRAMES
because every other stage of the pipeline does — extraction, scoring,
assembly, the frame-count invariant. VACE speaks VIDEOS. So each call encodes
frames to a clip, uploads it, generates, downloads, and decodes back to exactly
as many frames as it was given. That last word is load-bearing: a chunk that
returns a different count shortens the clip and desyncs the audio in a file
that plays perfectly, which is why the caller checks it (ChunkLengthError).

CONTROL SIGNALS — VERIFIED, and narrower than planned. fal's `task` enum is
`depth | pose | inpainting | outpainting | reframe`, confirmed on the
wan-vace-14b API page and its llms.txt on 2026-09-16, and identical on
wan-22-vace-fun-a14b. There is NO canny or lineart mode and no passthrough for
a pre-processed control video, so the silhouette-locking option the plan
assumed cannot be bought here at any price. Only `depth` and `pose` are
restyle control signals; the other three tasks do different jobs.

NO NETWORK CODE RUNS IN TESTS. The client is a one-method seam, exactly like
FalBackend's, so a stub is trivially faithful and nothing in the pipeline can
reach past it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import ffmpeg
from ..config import BILLED_FRAMES_PER_SECOND, ConfigError

# The endpoint. Verified 2026-09-16.
DEFAULT_VACE_MODEL = "fal-ai/wan-vace-14b"

# num_frames "must be between 81 to 241 (inclusive). Default value: 81" —
# quoted from the API schema. The floor is why a 3-second canary is not
# purchasable: 81 frames is 5.0625 billed seconds whatever you ask for.
MIN_CHUNK_FRAMES = 81
MAX_CHUNK_FRAMES = 241

# "Video seconds are calculated at 16 frames per second."
NATIVE_FPS = BILLED_FRAMES_PER_SECOND

# The two tasks that are restyle control signals. `inpainting`, `outpainting`
# and `reframe` are also in the enum but do different jobs.
CONTROL_SIGNALS = ("depth", "pose")

# Resolutions fal prices. `auto`, `240p` and `360p` exist in the enum but are
# not in the published price table, so they are not offered here — an unpriced
# resolution would be authorised against the wrong rate.
PRICED_RESOLUTIONS = {
    "480p": "wan_vace_480p",
    "580p": "wan_vace_580p",
    "720p": "wan_vace_720p",
}


class VaceError(RuntimeError):
    """A VACE call failed, or returned something unusable."""

    def __init__(self, message: str, *, response_body: str = "") -> None:
        super().__init__(message)
        self.response_body = response_body


@runtime_checkable
class VaceClientLike(Protocol):
    """The seam the real fal client plugs into, and the tests stand in for.

    Deliberately narrow — one method — so a stub cannot drift from the real
    thing, and so no code path can reach the network without going through it.
    """

    def generate_video(
        self,
        *,
        video_path: Path,
        prompt: str,
        negative_prompt: str,
        task: str,
        resolution: str,
        num_frames: int,
        frames_per_second: int,
        seed: int,
        model: str,
    ) -> bytes:
        """Return the generated MP4 bytes, or raise VaceError."""


@dataclass
class VaceBackend:
    """ClipRestyleBackend over fal's Wan VACE 14B.

    Double-locked like every paid backend: `live=True` is the deliberate act,
    and a client must be supplied. Neither can arrive by accident.
    """

    live: bool = False
    client: VaceClientLike | None = None
    control_signal: str = "depth"
    resolution: str = "480p"
    model: str = DEFAULT_VACE_MODEL
    negative_prompt: str = ""
    _cfg: object | None = None
    planned_calls: list[dict] = field(default_factory=list)

    name = "wan_vace"
    min_chunk_frames = MIN_CHUNK_FRAMES
    max_chunk_frames = MAX_CHUNK_FRAMES
    native_fps = NATIVE_FPS

    def __post_init__(self) -> None:
        if self.control_signal not in CONTROL_SIGNALS:
            raise VaceError(
                f"control signal {self.control_signal!r} is not available on "
                f"{self.model}. fal's task enum is depth | pose | inpainting | "
                "outpainting | reframe, of which only "
                f"{' and '.join(CONTROL_SIGNALS)} are restyle control signals. "
                "There is NO canny or lineart mode and no passthrough for a "
                "pre-processed control video — verified on the API page and "
                "llms.txt, 2026-09-16."
            )
        if self.resolution not in PRICED_RESOLUTIONS:
            raise VaceError(
                f"resolution {self.resolution!r} is not priced. fal publishes "
                f"rates for {sorted(PRICED_RESOLUTIONS)} only; 'auto', '240p' "
                "and '360p' exist in the enum but have no published rate, and "
                "authorising a call against an unknown rate is refused by "
                "design."
            )

    # -- naming -----------------------------------------------------------
    @property
    def ledger_backend(self) -> str:
        """Which `firewalls.cost.pricing` entry this call bills against.

        Per RESOLUTION, not per model: 480p and 720p are the same endpoint at
        different rates, and charging both to one ledger key would make the
        run total unreconcilable against the invoice.
        """
        return PRICED_RESOLUTIONS[self.resolution]

    # -- config / cost ----------------------------------------------------
    def _cost_config(self):
        if self._cfg is not None:
            return self._cfg
        from ..config import load_weights

        self._cfg = load_weights().firewalls.cost
        return self._cfg

    def cost_per_video_second_usd(self) -> float:
        """USD per BILLED second, where a billed second is 16 frames.

        The name is kept because that is fal's own wording, but the unit is
        frames/16 — see config.BILLED_FRAMES_PER_SECOND.
        """
        cost = self._cost_config()
        model = cost.pricing.get(self.ledger_backend)
        if model is None:
            raise ConfigError(
                f"backend {self.ledger_backend!r} is not priced in "
                "firewalls.cost.pricing. Refusing to spend against an unknown "
                "price."
            )
        if model.unit != "frames_div_16":
            raise ConfigError(
                f"{self.ledger_backend!r} is priced per {model.unit!r}, but "
                "fal bills VACE per frame-count/16 ('video seconds are "
                "calculated at 16 frames per second'). Fix the unit before "
                "spending — this mismatch is a 2x error."
            )
        return model.rate

    def cost_for_frames(self, frames: int) -> float:
        return self._cost_config().price_for(self.ledger_backend, frames=frames)

    def assert_affordable(self, quoted_usd: float, frames: int) -> None:
        """The configured rate is a CEILING on what a call may cost.

        A quote above it is a pricing change nobody approved, so the run stops
        rather than absorbing it.
        """
        ceiling = self.cost_for_frames(frames)
        if quoted_usd > ceiling:
            raise VaceError(
                f"endpoint quoted ${quoted_usd:.4f} for {frames} frames, above "
                f"the configured ceiling of ${ceiling:.4f} "
                f"(firewalls.cost.pricing.{self.ledger_backend}). Refusing: a "
                "price rise is a decision, not a rounding error."
            )

    # -- the call ---------------------------------------------------------
    def restyle_clip(
        self,
        src_frames: list[Path],
        out_dir: Path,
        *,
        prompt: str,
        strength: float,
        seed: int,
    ) -> list[Path]:
        """Restyle a contiguous frame range through VACE.

        `strength` is accepted to satisfy the protocol but is NOT sent: VACE has
        no img2img strength, and the equivalent lever is the control signal
        (`depth` holds proportions tighter than `pose`). Silently mapping
        strength onto something else would make the operator's dial lie.
        """
        if not self.live or self.client is None:
            raise NotImplementedError(
                "VaceBackend requires --live, a non-empty FAL_KEY, and a "
                "client. No network code path exists without all three."
            )
        count = len(src_frames)
        if not (self.min_chunk_frames <= count <= self.max_chunk_frames):
            raise VaceError(
                f"VACE accepts {self.min_chunk_frames}-{self.max_chunk_frames} "
                f"frames per call and was given {count}. Asking below the floor "
                "does not cost less — it bills the minimum, or pads the range, "
                "and padding changes the frame count."
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        work = out_dir.parent / ".vace_work"
        work.mkdir(parents=True, exist_ok=True)
        control = work / f"control_{src_frames[0].stem}_{src_frames[-1].stem}.mp4"

        # The control clip is encoded at VACE's NATIVE rate, not the pipeline's.
        # Sending 12fps frames while telling VACE 16 would make it interpret the
        # motion as slower than it is, and the output would drift out of step
        # with the audio it has to be re-muxed against.
        _encode(src_frames, control, fps=self.native_fps)

        payload = self.client.generate_video(
            video_path=control,
            prompt=prompt,
            negative_prompt=self.negative_prompt,
            task=self.control_signal,
            resolution=self.resolution,
            num_frames=count,
            frames_per_second=self.native_fps,
            seed=seed,
            model=self.model,
        )
        if not payload:
            raise VaceError("VACE returned an empty response body")

        generated = work / f"out_{src_frames[0].stem}_{src_frames[-1].stem}.mp4"
        generated.write_bytes(payload)

        written = _decode(generated, out_dir, names=[p.name for p in src_frames])
        if len(written) != count:
            raise VaceError(
                f"VACE returned {len(written)} frames for a {count}-frame "
                "request. A chunk of the wrong length shortens the clip and "
                "desyncs the audio, in a file that plays. Refusing."
            )
        return written

    def plan(self, src_frames: list[Path], *, prompt: str, seed: int) -> dict:
        """Dry run: what WOULD be called and charged, without calling it.

        Records the intent so the ledger wiring can be proven end to end with
        no network and no spend.
        """
        count = len(src_frames)
        entry = {
            "model": self.model,
            "ledger_backend": self.ledger_backend,
            "task": self.control_signal,
            "resolution": self.resolution,
            "num_frames": count,
            "frames_per_second": self.native_fps,
            "billed_seconds": round(count / BILLED_FRAMES_PER_SECOND, 4),
            "estimated_usd": round(self.cost_for_frames(count), 6),
            "seed": seed,
            "prompt_chars": len(prompt),
        }
        self.planned_calls.append(entry)
        return entry


def _encode(frames: list[Path], dst: Path, *, fps: int) -> Path:
    """Frames -> a silent MP4 for upload. yuv420p, even dimensions enforced."""
    if not frames:
        raise VaceError("cannot encode an empty frame range")
    listing = dst.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.resolve()}'\nduration {1 / fps:.6f}\n" for p in frames))
    ffmpeg.run(
        ["-f", "concat", "-safe", "0", "-i", str(listing),
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
         "-an", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
         "-r", str(fps), str(dst)],
        what="VACE control-clip encode",
    )
    return dst


def _decode(video: Path, out_dir: Path, *, names: list[str]) -> list[Path]:
    """Generated MP4 -> one PNG per expected frame name.

    Decoded with `-vsync 0` so ffmpeg emits exactly the frames present rather
    than resampling to a target rate — resampling would hide a short chunk by
    duplicating frames to fill the gap, which is the failure this whole check
    exists to catch.
    """
    staging = out_dir.parent / ".vace_decoded"
    if staging.exists():
        for stale in staging.glob("*.png"):
            stale.unlink()
    staging.mkdir(parents=True, exist_ok=True)

    ffmpeg.run(
        ["-i", str(video), "-vsync", "0", str(staging / "d_%05d.png")],
        what="VACE output decode",
    )
    decoded = sorted(staging.glob("d_*.png"))
    written: list[Path] = []
    for name, produced in zip(names, decoded):
        dst = out_dir / name
        dst.write_bytes(produced.read_bytes())
        written.append(dst)
    return written


def get_vace_client(*, live: bool):
    """The real fal client. Constructed only when live, and never in tests."""
    if not live:
        return None
    try:
        import fal_client  # noqa: F401
    except ImportError as exc:  # pragma: no cover - optional extra
        raise VaceError(
            "the fal client is an optional extra: pip install -e '.[fal]'"
        ) from exc
    return _FalVaceClient()


class _FalVaceClient:  # pragma: no cover - requires network and a credential
    """Real network path. Never exercised by the test suite."""

    def generate_video(
        self, *, video_path, prompt, negative_prompt, task, resolution,
        num_frames, frames_per_second, seed, model,
    ) -> bytes:
        import fal_client
        import urllib.request

        uploaded = fal_client.upload_file(str(video_path))
        arguments = {
            "video_url": uploaded,
            "prompt": prompt,
            "task": task,
            "resolution": resolution,
            "num_frames": num_frames,
            "frames_per_second": frames_per_second,
            "seed": seed,
        }
        if negative_prompt:
            arguments["negative_prompt"] = negative_prompt
        try:
            result = fal_client.subscribe(model, arguments=arguments)
        except Exception as exc:
            raise VaceError(f"fal call to {model} failed: {exc}") from exc

        url = (result or {}).get("video", {}).get("url")
        if not url:
            raise VaceError(
                f"fal returned no video url for {model}",
                response_body=json.dumps(result)[:2000],
            )
        with urllib.request.urlopen(url) as response:
            return response.read()
