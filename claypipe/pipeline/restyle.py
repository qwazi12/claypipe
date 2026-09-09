"""Stage: AI restyle (SPEC §2).

The defining architectural rule: frames are restyled as IMAGES, one at a time.
No video model, ever — timing and audio are never regenerated.

Backends sit behind the `RestyleBackend` protocol so a real endpoint (fal.ai
Flux Kontext or SDXL+ControlNet — SPEC A2, deferred) can be dropped in without
the pipeline changing. Step 1 ships only DummyBackend: offline, deterministic,
zero API spend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL import Image, ImageEnhance, ImageFilter

from ..logging import RunLogger
from .extract import frame_paths


@runtime_checkable
class RestyleBackend(Protocol):
    """One restyled frame per call. Deterministic given (frame, prompt, seed)."""

    name: str

    def cost_per_frame_usd(self) -> float:
        """Estimated spend per frame. Drives the cost firewalls (SPEC §4)."""

    def restyle(
        self, src: Path, dst: Path, *, prompt: str, strength: float, seed: int
    ) -> None:
        """Read `src`, write the restyled image to `dst`."""


class DummyBackend:
    """PIL-based cartoonizer. Offline, free, deterministic.

    Its job is not to look good — it is to make the ENTIRE pipeline runnable and
    testable with zero API spend (SPEC Tech Stack, Rule 32). It deliberately
    changes colour and texture while preserving geometry, which is exactly the
    property the step-2 scorer must reward.
    """

    name = "dummy"

    def cost_per_frame_usd(self) -> float:
        return 0.0

    def restyle(
        self, src: Path, dst: Path, *, prompt: str, strength: float, seed: int
    ) -> None:
        with Image.open(src) as img:
            frame = img.convert("RGB")
        # Posterise + smooth: flat plasticine-ish colour fields, geometry intact.
        levels = max(2, 8 - int(round(strength * 5)))
        styled = frame.filter(ImageFilter.SMOOTH_MORE)
        styled = Image.eval(styled, lambda v, n=levels: int(v / (256 / n)) * (255 // (n - 1)))
        styled = styled.filter(ImageFilter.EDGE_ENHANCE)
        # Seed shifts saturation deterministically, so a re-seeded retry differs.
        saturation = 1.0 + (seed % 7) * 0.05
        styled = ImageEnhance.Color(styled).enhance(saturation)
        dst.parent.mkdir(parents=True, exist_ok=True)
        styled.save(dst)


def get_backend(name: str) -> RestyleBackend:
    if name == "dummy":
        return DummyBackend()
    if name == "fal":
        # Build Order step 5. Refuse clearly rather than degrade silently (Rule 5).
        raise NotImplementedError(
            "the 'fal' backend lands in Build Order step 5; use --backend dummy"
        )
    raise ValueError(f"unknown backend {name!r} (available: dummy)")


def restyle_frames(
    *,
    backend: RestyleBackend,
    source_dir: Path,
    out_dir: Path,
    prompt: str,
    strength: float,
    logger: RunLogger,
    seed_for: "callable[[int], int] | None" = None,
) -> int:
    """Restyle every source frame. Resume-safe: an existing output is never
    re-generated, so a crashed run never re-spends (SPEC §2).

    `seed_for` maps a 1-based frame index to a seed. Step 1 has no shot
    detection yet, so the default is the whole clip as shot 0 (seed 1000).
    Shot-aware seeding (seed = 1000 + shot_index) lands with shots.py.
    """
    frames = frame_paths(source_dir)
    if not frames:
        raise FileNotFoundError(f"no source frames in {source_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    seed_for = seed_for or (lambda _idx: 1000)

    done = 0
    skipped = 0
    for idx, src in enumerate(frames, start=1):
        dst = out_dir / src.name
        if dst.is_file():
            skipped += 1
            continue
        backend.restyle(src, dst, prompt=prompt, strength=strength, seed=seed_for(idx))
        done += 1

    logger.info(
        "restyle.frames",
        backend=backend.name,
        restyled=done,
        resumed=skipped,
        total=len(frames),
        strength=strength,
        est_cost_usd=round(done * backend.cost_per_frame_usd(), 4),
    )
    return len(frames)
